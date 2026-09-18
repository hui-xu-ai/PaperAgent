/* graph/main.js — 文献计量图谱编排入口（ES 模块，自绑定 #lit-graph-btn）。
 *
 * 数据流：
 *   /graph/filters → populateFacets（校准过滤 UI）
 *   /graph/network → decorateNodes(大小←库内被引/颜色←IF/标签←年份+被引)
 *                  → renderer.setData → 2D 跑 FA2 worker 增量布局 → applyPositions
 * 交互：点击→详情；长按→双色高亮(citing/cited)；过滤→重载；样式→实时；2D/3D→切换渲染器。
 * 与 app.js 完全解耦：自带 fetch（api.js），只读 DOM，不碰全局业务状态。
 */
import * as api from './api.js';
import { decorateNodes, inferIfMax } from './scales.js';
import { Renderer2D } from './renderer2d.js';
import { Renderer3D } from './renderer3d.js';
import { Panel } from './panel.js';
import * as filters from './filters.js';
import * as settings from './settings.js';
import { layoutManager } from './layoutManager.js';

const $ = (id) => document.getElementById(id);
const MAX_ITER = 600;          // FA2 迭代预算（收敛后自动停，省 CPU）

class GraphApp {
  constructor() {
    this.panel = new Panel();
    this.nodes = [];
    this.edges = [];
    this.meta = null;
    this.ids = [];
    this.clusters = {};
    this.mode = '2d';
    this.renderer = null;
    this.worker = null;
    this.paused = false;
    this.style = settings.readStyle();
    this._rafId = null;
    this._waiting = false;
    this._iters = 0;
    this._loaded = false;
    this._savedPositions2d = null;   // 2D→3D→2D 切换时保留已收敛坐标
  }

  // ── 渲染器 ──────────────────────────────────────────
  buildRenderer() {
    const container = $('lg-canvas');
    if (this.renderer) this.renderer.destroy();
    this.renderer = this.mode === '3d' ? new Renderer3D(container) : new Renderer2D(container);
    this.renderer.onClick((id) => this.onNodeClick(id));
    this.renderer.onLongPress((id) => this.onNodeLongPress(id));
    if (this._loaded) this.paint();
  }

  /** 把当前 nodes/edges 画进渲染器（加工视觉字段后 setData）。 */
  paint() {
    if (!this.renderer) return;
    const ifMax = inferIfMax(this.nodes);
    decorateNodes(this.nodes, { ...settings.decorateOpts(this.style), ifMax });
    this.renderer.setData(this.nodes, this.edges);
    this.renderer.setStyle(settings.rendererStyle(this.style));
    this.panel.setHud(this.nodes.length, this.edges.length, this.meta);
    this.panel.renderLegend(this.style.palette, ifMax);
  }

  /** 仅刷新视觉（样式变更，不重建布局）。 */
  repaint() {
    if (!this.renderer) return;
    const ifMax = inferIfMax(this.nodes);
    decorateNodes(this.nodes, { ...settings.decorateOpts(this.style), ifMax });
    this.renderer.updateNodeAttrs(this.nodes);
    this.renderer.setStyle(settings.rendererStyle(this.style));
    this.panel.renderLegend(this.style.palette, ifMax);
  }

  setMode(mode) {
    if (mode === this.mode) return;
    // 切离当前模式时保存 FA2 坐标（2D 和 3D 共享同一套位置）
    if (this.mode === '2d' && this.renderer && this.ids.length) {
      layoutManager.save2dPositions(this._capturePositions2d());
    }

    layoutManager.mode = mode;
    this.mode = mode;
    this.stopLayout();
    this.buildRenderer();

    // 3D 模式：让 d3-force-3d 自然布局，不应用 FA2 坐标
    if (mode === '3d') {
      // 3D 渲染器会自己处理布局，只需等待渲染完成后 zoomToFit
      setTimeout(() => {
        if (this.renderer && this.renderer.fit) this.renderer.fit();
      }, 500);
    } else {
      // 2D 模式：恢复 FA2 坐标
      const saved = layoutManager.restore2dPositions();
      if (saved) {
        this.renderer.applyPositions(saved, this.ids);
        this.paused = true;
      } else {
        this.startLayout(true);
      }
    }
    this._syncLayoutBtn();
  }

  /** 从渲染器当前节点坐标快照（2D 切 3D 前调用）。 */
  _capturePositions2d() {
    if (!this.renderer || !this.renderer._graph) return null;
    const g = this.renderer._graph;
    const pos = new Float32Array(this.ids.length * 2);
    for (let i = 0; i < this.ids.length; i++) {
      const id = this.ids[i];
      if (!g.hasNode(id)) continue;
      const attr = g.getNodeAttributes(id);
      pos[2 * i] = attr.x;
      pos[2 * i + 1] = attr.y;
    }
    return pos;
  }

  // ── 数据加载 ───────────────────────────────────────
  async loadFilters() {
    try {
      const facets = await api.getFilters();
      filters.populateFacets(facets);
      filters.syncLabels();
    } catch (e) {
      this.panel.setMessage('分面加载失败：' + e.message);
    }
  }

  /** 过滤预览：显示过滤后节点数（不实际加载图谱）。 */
  async previewFilter() {
    try {
      const params = { ...filters.readFilters(), limit: 1, preview: true };
      const data = await api.getNetwork(params);
      const count = data.meta?.matched_nodes || 0;
      this.panel.setMessage(`过滤后约 ${count} 个节点${data.meta?.truncated ? '（已截断）' : ''}`);
    } catch (e) {
      this.panel.setMessage('预览失败：' + e.message);
    }
  }

  async loadNetwork(showLoading = true) {
    if (showLoading) this.panel.setLoading(true);
    this.panel.setMessage('');
    try {
      const params = filters.readFilters();
      const data = await api.getNetwork(params);
      this.nodes = data.nodes || [];
      this.edges = data.edges || [];
      this.meta = data.meta || null;
      this.ids = this.nodes.map((n) => n.id);
      this.clusters = {};
      for (const n of this.nodes) this.clusters[n.id] = n.cluster || 0;
      this._loaded = true;
      layoutManager.clearCache();  // 数据变化时清除位置缓存
      layoutManager.init(this.ids, this.edges, this.clusters);
      if (!this.nodes.length) {
        this.panel.setHud(0, 0, this.meta);
        this.panel.setMessage('无匹配节点，放宽过滤条件试试。');
        if (this.renderer) this.renderer.setData([], []);
        return;
      }
      this.paint();
      this.startLayout(true);
    } catch (e) {
      this.panel.setMessage('图谱加载失败：' + e.message);
    } finally {
      this.panel.setLoading(false);
    }
  }

  // ── FA2 worker 布局（2D 和 3D 共用同一套坐标）──────────────
  _ensureWorker() {
    if (this.worker) return;
    this.worker = new Worker('/js/graph/layout.worker.js');
    this.worker.onmessage = (e) => {
      if (e.data.type === 'positions') {
        this._waiting = false;
        if (this.renderer) {
          const pos = new Float32Array(e.data.positions);
          this.renderer.applyPositions(pos, this.ids);
          // 同时保存到 layoutManager，供模式切换时恢复
          layoutManager.save2dPositions(pos);
        }
      }
    };
  }

  startLayout(reinit) {
    if (!this._loaded || !this.nodes.length) return;
    this._ensureWorker();
    if (reinit) {
      this._iters = 0;
      this.paused = false;
      this.worker.postMessage({
        type: 'init',
        ids: this.ids,
        edges: this.edges.map((e) => ({ source: e.source, target: e.target })),
        clusters: this.clusters,
        settings: settings.layoutSettings(this.style),
      });
    } else {
      // 仅更新布局参数（不重置坐标）
      this.worker.postMessage({
        type: 'update-settings',
        settings: settings.layoutSettings(this.style),
      });
    }
    this._syncLayoutBtn();
    this._tick();
  }

  stopLayout() {
    if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = null; }
  }

  _tick() {
    this.stopLayout();
    if (this.paused) return;
    const loop = () => {
      this._rafId = null;
      if (this.paused) return;
      if (this._iters >= MAX_ITER) { this.paused = true; this._syncLayoutBtn(); return; }
      if (!this._waiting && this.worker) {
        this._waiting = true;
        const per = this.ids.length > 5000 ? 2 : 5;
        this._iters += per;
        this.worker.postMessage({ type: 'run', iterations: per });
      }
      this._rafId = requestAnimationFrame(loop);
    };
    this._rafId = requestAnimationFrame(loop);
  }

  togglePause() {
    if (this.paused && this._iters >= MAX_ITER) {
      // 收敛后继续 → 重置迭代计数
      this._iters = 0;
    }
    this.paused = !this.paused;
    if (!this.paused) {
      // 继续布局：如果还没初始化过，先初始化
      if (this._iters === 0 && this._loaded) {
        this.startLayout(true);
      } else {
        this._tick();
      }
    } else {
      this.stopLayout();
    }
    this._syncLayoutBtn();
  }

  _syncLayoutBtn() {
    const b = $('lg-layout-toggle');
    if (!b) return;
    const running = !this.paused;
    b.textContent = running ? '⏸ 暂停布局' : '▶ 恢复布局';
    b.title = running ? '暂停引力布局迭代' : '继续/重新运行引力布局';
    b.disabled = false;
  }

  relayout() {
    this.style = settings.readStyle();
    // 重跑布局：重新初始化 worker（2D 和 3D 共用 FA2）
    this.stopLayout();
    this._iters = 0;
    this.paused = false;
    this.startLayout(true);
  }

  // ── 交互 ────────────────────────────────────────────
  async onNodeClick(id) {
    this.renderer.clearHighlight();
    try {
      const detail = await api.getNode(id);
      this.panel.renderDetail(detail);
    } catch (e) {
      this.panel.renderDetail({ doi: id, title: id });
    }
  }

  async onNodeLongPress(id) {
    try {
      const nb = await api.getNeighbors(id);
      this.renderer.highlight(id, nb.citing || [], nb.cited || []);
      this.panel.toggleDetail(true);
    } catch (e) {
      this.panel.setMessage('邻居高亮失败：' + e.message);
    }
  }

  search(q) {
    q = (q || '').trim().toLowerCase();
    if (!q) { this.renderer.clearHighlight(); return; }
    const hit = this.nodes.find((n) =>
      (n.title && n.title.toLowerCase().includes(q)) || (n.id && n.id.toLowerCase().includes(q)));
    if (hit) this.renderer.focus(hit.id);
    else this.panel.setMessage('未找到匹配节点：' + q);
  }

  fit() { if (this.renderer) this.renderer.fit(); }

  // ── 3D 视角预设 ──────────────────────────────────────
  viewFront() { if (this.mode === '3d' && this.renderer) this.renderer.viewFront(); }
  viewTop() { if (this.mode === '3d' && this.renderer) this.renderer.viewTop(); }
  viewSide() { if (this.mode === '3d' && this.renderer) this.renderer.viewSide(); }
  viewIsometric() { if (this.mode === '3d' && this.renderer) this.renderer.viewIsometric(); }
  viewReset() { if (this.renderer) this.renderer.viewReset ? this.renderer.viewReset() : this.renderer.fit(); }

  setSpacing(factor) {
    if (this.mode === '3d' && this.renderer) this.renderer.setSpacing(factor);
  }

  destroy() {
    this.stopLayout();
    if (this.worker) { this.worker.terminate(); this.worker = null; }
    if (this.renderer) { this.renderer.destroy(); this.renderer = null; }
  }
}

// ────────────────────────────────────────────────── 装配
let app = null;

function openGraph() {
  const modal = $('lit-graph-modal');
  modal.style.display = 'flex';
  if (!app) {
    app = new GraphApp();
    bindUI();
    try {
      app.buildRenderer();
    } catch (e) {
      console.error('[graph] 渲染器初始化失败:', e);
      app.panel.setMessage('渲染器初始化失败，请尝试切换 2D/3D 模式：' + e.message);
    }
    app.loadFilters();
    app.loadNetwork();
  }
}

function closeGraph() {
  const modal = $('lit-graph-modal');
  modal.style.display = 'none';
  // 关窗即释放 worker/渲染器，重开时惰性重建（省显存/CPU）。
  if (app) { app.destroy(); app = null; }
}

function bindUI() {
  // 顶栏：模式 / 布局 / 适配 / 搜索 / 面板 / 详情
  $('lg-mode-seg').addEventListener('click', (e) => {
    const btn = e.target.closest('.lg-seg-btn');
    if (!btn) return;
    for (const b of $('lg-mode-seg').querySelectorAll('.lg-seg-btn')) b.classList.toggle('active', b === btn);
    app.setMode(btn.dataset.mode);
  });
  $('lg-layout-toggle').addEventListener('click', () => app.togglePause());
  $('lg-fit').addEventListener('click', () => app.fit());
  $('lg-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') app.search(e.target.value); });
  $('lg-side-toggle').addEventListener('click', () => app.panel.toggleSide());
  $('lg-detail-toggle').addEventListener('click', () => app.panel.toggleDetail());

  // 侧栏 tab
  for (const tab of document.querySelectorAll('.lg-side-tab')) {
    tab.addEventListener('click', () => {
      for (const t of document.querySelectorAll('.lg-side-tab')) t.classList.toggle('active', t === tab);
      const which = tab.dataset.panel;
      $('lg-panel-filter').style.display = which === 'filter' ? '' : 'none';
      $('lg-panel-style').style.display = which === 'style' ? '' : 'none';
    });
  }

  // 过滤面板
  filters.bindLiveLabels();
  $('lg-apply').addEventListener('click', () => app.loadNetwork());
  $('lg-reset').addEventListener('click', () => { filters.resetFilters(); app.loadNetwork(); });
  // 过滤预览按钮（如果存在）
  const previewBtn = $('lg-preview');
  if (previewBtn) previewBtn.addEventListener('click', () => app.previewFilter());

  // 样式面板（实时预览）
  settings.bindLiveLabels();
  const style = settings.readStyle;
  const live = () => { app.style = style(); app.repaint(); };
  for (const id of ['lg-s-colorscale', 'lg-s-size', 'lg-s-label', 'lg-s-edges',
                    'lg-s-edgelabel', 'lg-s-bg', 'lg-s-bgcolor', 'lg-s-edgecolor',
                    'lg-s-edgewidth']) {
    const el = $(id);
    el.addEventListener('input', live);
    el.addEventListener('change', live);
  }
  // 引力参数：改完点「重跑布局」生效（避免拖动时频繁 reinit）。
  $('lg-s-scaling').addEventListener('change', () => { app.style = style(); });
  $('lg-s-gravity').addEventListener('change', () => { app.style = style(); });
  $('lg-relayout').addEventListener('click', () => { app.style = style(); app.relayout(); });

  // 3D 视角预设按钮
  for (const [id, method] of [
    ['lg-view-front', 'viewFront'], ['lg-view-top', 'viewTop'],
    ['lg-view-side', 'viewSide'], ['lg-view-iso', 'viewIsometric'],
    ['lg-view-reset', 'viewReset'],
  ]) {
    const el = $(id);
    if (el) el.addEventListener('click', () => app[method]());
  }
  const spacingSlider = $('lg-spacing');
  if (spacingSlider) {
    spacingSlider.addEventListener('input', (e) => {
      app.setSpacing(parseFloat(e.target.value));
    });
  }

  // 详情面板关闭
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
  if (mask) mask.addEventListener('click', (e) => { if (e.target === mask) closeGraph(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && mask && mask.style.display === 'flex') closeGraph();
  });
}

bindEntry();
