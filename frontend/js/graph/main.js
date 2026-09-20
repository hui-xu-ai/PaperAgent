/* graph/main.js — GraphController（新架构编排层）。
 *
 * 数据流：Data → Layout → Style → Render
 *   - GraphDataModel: 纯数据容器
 *   - LayoutEngine: 坐标计算
 *   - applyStyles: 样式应用
 *   - Renderer2D/3D: 同构渲染接口
 */

import * as api from './api.js';
import { GraphDataModel } from './dataModel.js';
import { LayoutEngine } from './layoutEngine.js';
import { applyStyles, toRendererStyle } from './styleApplier.js';
import { RendererThree } from './rendererThree.js';
import { Panel } from './panel.js';
import * as filters from './filters.js';
import * as settings from './settings.js';

const $ = (id) => document.getElementById(id);
const MAX_ITER = 600;

/** 聚类调色板（与 styleApplier.js 一致）。 */
const CLUSTER_PALETTE = [
  '#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f',
  '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac',
  '#86bcb6', '#8cd17d', '#b6992d', '#499894', '#d37295',
  '#a0cbe8', '#ffbe7d', '#8b8b8b', '#d4a6c8', '#d37295',
];

class GraphController {
  constructor() {
    // 数据层
    this.data = new GraphDataModel();
    this.layout = new LayoutEngine(this.data);

    // UI层
    this.panel = new Panel();

    // 状态
    this.displayMode = '2d';
    this.layoutAlgorithm = 'fa2';
    this.currentStyle = settings.readStyle();
    this.currentLayoutOpts = settings.readLayout();

    // 渲染器
    this.renderer = null;

    // FA2 worker
    this.worker = null;
    this.paused = false;
    this._rafId = null;
    this._waiting = false;
    this._iters = 0;

    // 加载标志
    this._loaded = false;
  }

  // ─ 渲染器管理 ───────────────────────────────────────

  /** 单场景渲染器只建一次；2D/3D 是其显示样式，不是两个引擎。 */
  _ensureRenderer() {
    if (this.renderer) return;
    this.renderer = new RendererThree($('lg-canvas'));
    this.renderer
      .onClick((id) => this._onNodeClick(id))
      .onLongPress((id) => this._onNodeLongPress(id));
  }

  /** 设置显示模式（2D↔3D）：场景内切相机/材质，位置与缩放观感保持连续。 */
  setDisplayMode(mode) {
    if (mode === this.displayMode) return;
    this.displayMode = mode;
    if (this.renderer) this.renderer.setDisplayMode(mode);
    this._sync3dViews();
  }

  /** 把布局坐标写回数据节点（renderData 的 x/y 来源）。 */
  _syncPositionsToData(pos) {
    const nodes = this.data.nodes;
    for (let i = 0; i < nodes.length; i++) {
      nodes[i].x = pos[2 * i];
      nodes[i].y = pos[2 * i + 1];
    }
  }

  // ── 数据加载 ───────────────────────────────────────

  /** 加载过滤分面。 */
  async loadFilters() {
    try {
      const facets = await api.getFilters();
      filters.populateFacets(facets);
      settings.syncLabels();

      // 加载聚类列表填充下拉
      try {
        const clusters = await api.getClusters();
        filters.populateClusterFilter(clusters);
      } catch (e) {
        console.warn('聚类列表加载失败:', e);
      }
    } catch (e) {
      this.panel.setMessage('分面加载失败：' + e.message);
    }
  }

  /** 预览过滤结果。 */
  async previewFilter() {
    try {
      const params = { ...filters.readFilters(), limit: 1, preview: true };
      const data = await api.getNetwork(params);
      // 优先使用 actual_nodes（排除孤立节点后的数量），否则用 matched_nodes
      const count = data.meta?.actual_nodes ?? data.meta?.matched_nodes ?? 0;
      const truncated = data.meta?.truncated || false;
      const hasIsolated = data.meta?.matched_nodes !== data.meta?.actual_nodes;
      let msg = `过滤后约 ${count} 个节点`;
      if (hasIsolated) msg += `（已排除孤立节点）`;
      if (truncated) msg += `，按排序保留 Top-N`;
      this.panel.setMessage(msg);
    } catch (e) {
      this.panel.setMessage('预览失败：' + e.message);
    }
  }

  /** 加载网络数据。 */
  async loadNetwork(showLoading = true) {
    if (showLoading) this.panel.setLoading(true);
    this.panel.setMessage('');

    try {
      // 1. 读取过滤条件
      const filterOpts = filters.readFilters();

      // 2. 获取原始数据
      const raw = await api.getNetwork(filterOpts);

      // 3. 加载到数据模型
      this.data.load(raw.nodes || [], raw.edges || [], raw.meta || null);

      // 4. 应用过滤
      this.data = this.data.filter(filterOpts);

      // 5. 标记已加载
      this._loaded = true;

      // 6. 清空布局缓存
      this.layout.clearCache();

      // 7. 检查空数据
      if (this.data.nodeCount === 0) {
        this.panel.setHud(0, 0, this.data.meta);
        this.panel.setMessage('无匹配节点，放宽过滤条件试试。');
        if (this.renderer) this.renderer.setData({ nodes: [], edges: [], meta: null });
        return;
      }

      // 8. 渲染
      this._render();

      // 9. 启动布局计算
      this._runLayout(true);

    } catch (e) {
      this.panel.setMessage('图谱加载失败：' + e.message);
    } finally {
      this.panel.setLoading(false);
    }
  }

  // ── 核心渲染流程 ───────────────────────────────────

  /** 完整渲染流程：Data → Layout → Style → Render。 */
  _render() {
    if (!this._loaded || !this.renderer) return;

    // 1. 应用样式（计算 renderSize/renderColor/renderLabel）
    // 合并过滤选项中的被引来源到样式选项
    const filterOpts = filters.readFilters();
    const styleWithCitSource = { ...this.currentStyle, citationSource: filterOpts.citation_source || 'filtered' };
    applyStyles(this.data, styleWithCitSource);

    // 2. 转换为渲染格式
    const renderData = this.data.toRenderFormat();

    // 3. 设置数据
    this.renderer.setData(renderData);

    // 4. 应用样式选项（背景、边显隐等）
    this.renderer.setStyle(toRendererStyle(this.currentStyle));

    // 5. 更新HUD和图例
    this.panel.setHud(this.data.nodeCount, this.data.edgeCount, this.data.meta);
    this.panel.renderLegend(this.currentStyle.palette, this._inferIfMax());

    // 6. 渲染聚类图例
    this._renderClusterLegend();
  }

  /** 仅更新样式（不重建图）。 */
  repaint() {
    if (!this.renderer || !this._loaded) return;

    // 1. 重新应用样式
    const filterOpts = filters.readFilters();
    const styleWithCitSource = { ...this.currentStyle, citationSource: filterOpts.citation_source || 'filtered' };
    applyStyles(this.data, styleWithCitSource);

    // 2. 转换为渲染格式
    const renderData = this.data.toRenderFormat();

    // 3. 更新节点属性
    this.renderer.updateNodeAttrs(renderData.nodes);

    // 4. 更新样式选项
    this.renderer.setStyle(toRendererStyle(this.currentStyle));

    // 5. 更新图例
    this.panel.renderLegend(this.currentStyle.palette, this._inferIfMax());

    // 6. 更新聚类图例
    this._renderClusterLegend();
  }

  /** 渲染聚类图例（从后端获取聚类统计）。 */
  async _renderClusterLegend() {
    try {
      const clusters = await api.getClusters();
      const container = document.getElementById('lg-cluster-legend');
      if (!container) return;

      container.innerHTML = '';
      const colorMode = this.currentStyle.colorMode || 'if';

      // 只在聚类颜色模式下显示图例
      if (colorMode !== 'cluster') {
        document.getElementById('lg-panel-clusters').style.display = 'none';
        return;
      }

      document.getElementById('lg-panel-clusters').style.display = '';

      // 按大小排序
      clusters.sort((a, b) => b.size - a.size);

      for (const c of clusters) {
        const clusterId = parseInt(c.id) || 0;
        const color = CLUSTER_PALETTE[clusterId % CLUSTER_PALETTE.length];
        const item = document.createElement('div');
        item.className = 'lg-cluster-item';
        item.dataset.clusterId = c.id;
        item.innerHTML = `
          <span class="lg-cluster-color" style="background:${color}"></span>
          <span class="lg-cluster-info">
            <span class="lg-cluster-id">聚类 ${c.id}</span>
            <span class="lg-cluster-size">${c.size} 篇</span>
          </span>
          ${c.keywords && c.keywords.length ? `<span class="lg-cluster-kw">${c.keywords.slice(0, 3).join(', ')}</span>` : ''}
        `;
        item.addEventListener('click', () => this._highlightCluster(c.id));
        container.appendChild(item);
      }
    } catch (e) {
      console.warn('聚类图例加载失败:', e);
    }
  }

  /** 高亮指定聚类（其他节点变暗）。 */
  _highlightCluster(clusterId) {
    if (!this.renderer || !this._loaded) return;

    const targetCluster = clusterId;
    const nodes = this.renderer.graphData?.nodes || [];

    // 更新节点透明度
    for (const node of nodes) {
      const nodeCluster = this.data.clusters[node.id] || 0;
      node.opacity = nodeCluster === targetCluster ? 1 : 0.15;
    }

    this.renderer.updateNodeAttrs(nodes);

    // 更新图例项的高亮状态
    const items = document.querySelectorAll('.lg-cluster-item');
    for (const item of items) {
      const isActive = parseInt(item.dataset.clusterId) === targetCluster;
      item.classList.toggle('active', isActive);
    }
  }

  /** 取消聚类高亮（恢复所有节点）。 */
  _clearClusterHighlight() {
    if (!this.renderer || !this._loaded) return;

    const nodes = this.renderer.graphData?.nodes || [];
    for (const node of nodes) {
      node.opacity = 1;
    }
    this.renderer.updateNodeAttrs(nodes);

    const items = document.querySelectorAll('.lg-cluster-item');
    for (const item of items) {
      item.classList.remove('active');
    }
  }

  // ── 布局计算 ──────────────────────────────────────

  /** 运行布局（根据算法选择FA2或几何布局）。 */
  _runLayout(reinit, randomize = false) {
    if (!this._loaded || this.data.nodeCount === 0) return;

    this._stopLayout();

    if (this.layoutAlgorithm === 'fa2') {
      this._startFA2(reinit, randomize);
    } else {
      this._runGeometricLayout();
    }
  }

  /** 启动FA2 worker。 */
  _startFA2(reinit, randomize = false) {
    this._ensureWorker();

    if (reinit) {
      this._iters = 0;
      this.paused = false;
      this._desoverlapSent = false;

      // 传递节点大小信息（用于防重叠斥力计算）
      const nodeSizes = {};
      for (const n of this.data.nodes) {
        nodeSizes[n.id] = n.renderSize || n.size || 2;
      }

      this.worker.postMessage({
        type: 'init',
        ids: this.data.ids,
        edges: this.data.edges.map(e => ({ source: e.source, target: e.target })),
        clusters: this.data.clusters,
        nodeSizes: nodeSizes,
        settings: settings.layoutSettings(this.currentLayoutOpts),
        randomize: !!randomize,
      });
    } else {
      this.worker.postMessage({
        type: 'update-settings',
        settings: settings.layoutSettings(this.currentLayoutOpts),
      });
    }

    this._tick();
  }

  /** 运行几何布局（circular/grid/random）。 */
  _runGeometricLayout() {
    const positions = this.layout.compute(this.layoutAlgorithm, this.currentLayoutOpts);
    this._syncPositionsToData(positions);
    this.layout.save(positions);
    if (this.renderer) {
      this.renderer.applyPositions(positions, this.data.ids);
    }
    this.paused = true;
  }

  /** 重跑布局（用户点击"重跑布局"按钮）：随机种子重播种，得到新布局。 */
  relayout() {
    this.currentStyle = settings.readStyle();
    this.currentLayoutOpts = settings.readLayout();
    this._runLayout(true, true);
  }

  /** 设置布局算法。 */
  setAlgorithm(algo) {
    this.layoutAlgorithm = algo;
    this._toggleFA2Params(algo === 'fa2');
    this._runLayout(true);
  }

  /** 设置间距（仅影响几何布局）。 */
  setSpacing(val) {
    this.currentLayoutOpts.spacing = val;
    if (this.layoutAlgorithm !== 'fa2') {
      this._runGeometricLayout();
    }
  }

  // ── FA2 Worker ─────────────────────────────────────

  /** 确保worker已创建。 */
  _ensureWorker() {
    if (this.worker) return;

    this.worker = new Worker('/js/graph/layout.worker.js');
    this.worker.onmessage = (e) => {
      if (e.data.type === 'positions') {
        this._waiting = false;
        const pos = new Float32Array(e.data.positions);
        this._syncPositionsToData(pos);
        this.layout.save(pos);
        if (this.renderer) {
          this.renderer.applyPositions(pos, this.data.ids);
        }
      }
    };
  }

  /** 停止布局计算。 */
  _stopLayout() {
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
  }

  /** FA2 tick循环。 */
  _tick() {
    this._stopLayout();
    if (this.paused) return;

    const loop = () => {
      this._rafId = null;
      if (this.paused) return;

      if (this._iters >= MAX_ITER) {
        if (!this._desoverlapSent && this.worker) {
          // FA2 收敛，发送去重叠后处理
          this._desoverlapSent = true;
          this._waiting = true;
          this.worker.postMessage({ type: 'desoverlap', passes: 100 });
        } else if (!this._waiting) {
          // 去重叠完成，暂停
          this.paused = true;
        }
        if (!this.paused) {
          this._rafId = requestAnimationFrame(loop);
        }
        return;
      }

      if (!this._waiting && this.worker) {
        this._waiting = true;
        const per = this.data.ids.length > 5000 ? 2 : 5;
        this._iters += per;
        this.worker.postMessage({ type: 'run', iterations: per });
      }

      this._rafId = requestAnimationFrame(loop);
    };

    this._rafId = requestAnimationFrame(loop);
  }

  // ── 交互 ───────────────────────────────────────────

  /** 节点点击。 */
  async _onNodeClick(id) {
    this.renderer.clearHighlight();
    try {
      const detail = await api.getNode(id);
      this.panel.renderDetail(detail);
    } catch (e) {
      this.panel.renderDetail({ doi: id, title: id });
    }
  }

  /** 节点长按。 */
  async _onNodeLongPress(id) {
    try {
      const nb = await api.getNeighbors(id);
      this.renderer.highlight(id, nb.citing || [], nb.cited || []);
      this.panel.toggleDetail(true);
    } catch (e) {
      this.panel.setMessage('邻居高亮失败：' + e.message);
    }
  }

  /** 搜索节点。 */
  search(q) {
    q = (q || '').trim().toLowerCase();
    if (!q) {
      this.renderer.clearHighlight();
      return;
    }

    const hit = this.data.nodes.find(n =>
      (n.title && n.title.toLowerCase().includes(q)) ||
      (n.id && n.id.toLowerCase().includes(q))
    );

    if (hit) {
      this.renderer.focus(hit.id);
    } else {
      this.panel.setMessage('未找到匹配节点：' + q);
    }
  }

  /** 适配全图。 */
  fit() {
    if (this.renderer) this.renderer.fit();
  }

  // ── 3D视角控制 ────────────────────────────────────

  viewFront() {
    if (this.displayMode === '3d' && this.renderer) this.renderer.viewFront();
  }

  viewTop() {
    if (this.displayMode === '3d' && this.renderer) this.renderer.viewTop();
  }

  viewSide() {
    if (this.displayMode === '3d' && this.renderer) this.renderer.viewSide();
  }

  viewIsometric() {
    if (this.displayMode === '3d' && this.renderer) this.renderer.viewIsometric();
  }

  viewReset() {
    if (this.renderer) {
      this.renderer.viewReset ? this.renderer.viewReset() : this.renderer.fit();
    }
  }

  _sync3dViews() {
    const el = $('lg-3d-views');
    if (el) el.style.display = this.displayMode === '3d' ? '' : 'none';
  }

  _toggleFA2Params(show) {
    const el = $('lg-fa2-params');
    if (el) el.style.display = show ? '' : 'none';
  }

  _inferIfMax() {
    const ifs = this.data.nodes.map(n => n.impact_factor || 0).filter(if_ => if_ > 0);
    return ifs.length ? Math.max(...ifs) : 50;
  }

  // ── 生命周期 ───────────────────────────────────────

  destroy() {
    this._stopLayout();
    if (this.worker) {
      this.worker.terminate();
      this.worker = null;
    }
    if (this.renderer) {
      this.renderer.destroy();
      this.renderer = null;
    }
    this.data.clear();
  }
}

// ────────────────────────────────────────────────── 装配

let app = null;

function openGraph() {
  const modal = $('lit-graph-modal');
  modal.style.display = 'flex';

  if (!app) {
    app = new GraphController();
    window.__graphApp = app;   // 调试钩子：控制台可直接检查控制器/渲染器状态
    bindUI();

    try {
      app._ensureRenderer();
    } catch (e) {
      console.error('[graph] 渲染器初始化失败:', e);
      app.panel.setMessage('渲染器初始化失败：' + e.message);
    }

    app.loadFilters();
    app.loadNetwork();
  }
}

function closeGraph() {
  const modal = $('lit-graph-modal');
  modal.style.display = 'none';
  if (app) {
    app.destroy();
    app = null;
  }
}

function bindUI() {
  // 顶栏
  $('lg-relayout-global').addEventListener('click', () => app.relayout());
  $('lg-fit').addEventListener('click', () => app.fit());
  $('lg-search').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') app.search(e.target.value);
  });
  $('lg-side-toggle').addEventListener('click', () => app.panel.toggleSide());
  $('lg-detail-toggle').addEventListener('click', () => app.panel.toggleDetail());

  // 侧栏三tab
  const panels = {
    filter: 'lg-panel-filter',
    layout: 'lg-panel-layout',
    style: 'lg-panel-style',
  };

  for (const tab of document.querySelectorAll('.lg-side-tab')) {
    tab.addEventListener('click', () => {
      for (const t of document.querySelectorAll('.lg-side-tab')) {
        t.classList.toggle('active', t === tab);
      }
      const which = tab.dataset.panel;
      for (const [key, id] of Object.entries(panels)) {
        $(id).style.display = key === which ? '' : 'none';
      }
    });
  }

  // 过滤面板
  filters.bindLiveLabels();
  $('lg-apply').addEventListener('click', () => app.loadNetwork());
  $('lg-reset').addEventListener('click', () => {
    filters.resetFilters();
    app.loadNetwork();
  });
  const previewBtn = $('lg-preview');
  if (previewBtn) previewBtn.addEventListener('click', () => app.previewFilter());

  // 布局面板
  settings.bindLiveLabels();
  $('lg-l-algorithm').addEventListener('change', (e) => {
    app.currentLayoutOpts = settings.readLayout();
    app.setAlgorithm(e.target.value);
  });
  $('lg-s-scaling').addEventListener('change', () => {
    app.currentLayoutOpts = settings.readLayout();
  });
  $('lg-s-gravity').addEventListener('change', () => {
    app.currentLayoutOpts = settings.readLayout();
  });
  $('lg-l-spacing').addEventListener('input', (e) => {
    app.setSpacing(parseFloat(e.target.value));
  });

  // 样式面板
  const live = () => {
    app.currentStyle = settings.readStyle();
    app.repaint();
  };

  for (const id of [
    'lg-s-colorscale', 'lg-s-size', 'lg-s-label', 'lg-s-edges',
    'lg-s-edgelabel', 'lg-s-bg', 'lg-s-bgcolor', 'lg-s-edgecolor',
    'lg-s-edgewidth',
  ]) {
    const el = $(id);
    el.addEventListener('input', live);
    el.addEventListener('change', live);
  }

  // 颜色模式切换（影响因子/聚类）
  $('lg-s-color-mode').addEventListener('change', (e) => {
    const mode = e.target.value;
    // 聚类模式时隐藏配色下拉框
    const colorscaleField = $('lg-s-colorscale-field');
    if (colorscaleField) {
      colorscaleField.style.display = mode === 'cluster' ? 'none' : '';
    }
    live();
  });

  // 2D/3D显示模式切换
  $('lg-display-mode').addEventListener('click', (e) => {
    const btn = e.target.closest('.lg-seg-btn');
    if (!btn) return;
    for (const b of $('lg-display-mode').querySelectorAll('.lg-seg-btn')) {
      b.classList.toggle('active', b === btn);
    }
    app.setDisplayMode(btn.dataset.mode);
  });

  // 3D视角按钮
  for (const [id, method] of [
    ['lg-view-front', 'viewFront'],
    ['lg-view-top', 'viewTop'],
    ['lg-view-side', 'viewSide'],
    ['lg-view-iso', 'viewIsometric'],
    ['lg-view-reset', 'viewReset'],
  ]) {
    const el = $(id);
    if (el) el.addEventListener('click', () => app[method]());
  }

  // 详情面板
  $('lg-detail-close').addEventListener('click', () => {
    app.panel.toggleDetail(false);
    app.panel.showDetailPlaceholder();
    if (app.renderer) app.renderer.clearHighlight();
  });

  // 详情面板拖动调宽
  const handle = $('lg-resize-handle');
  const detail = $('lg-detail');
  if (handle && detail) {
    let dragging = false;
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragging = true;
      handle.classList.add('active');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const modalRect = detail.parentElement.getBoundingClientRect();
      let w = modalRect.right - e.clientX;
      w = Math.max(200, Math.min(600, w));
      detail.style.width = w + 'px';
      detail.classList.remove('collapsed');
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove('active');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    });
  }
}

function bindEntry() {
  const btn = $('lit-graph-btn');
  if (btn) btn.addEventListener('click', openGraph);
  const close = $('lit-graph-close');
  if (close) close.addEventListener('click', closeGraph);
  const mask = $('lit-graph-modal');
  if (mask) mask.addEventListener('click', (e) => {
    if (e.target === mask) closeGraph();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && mask && mask.style.display === 'flex') closeGraph();
  });
}

bindEntry();
