/* graph/main.js — 文献计量图谱编排入口（ES 模块，自绑定 #lit-graph-btn）。
 *
 * 三维度解耦：
 *   数据过滤（哪些节点）→ 过滤面板 → loadNetwork()
 *   布局算法（节点在哪）→ 布局面板 → runLayout()
 *   显示样式（节点长啥样）→ 样式面板 → paint()/repaint()
 *
 * 2D/3D 是显示维度，不影响布局坐标（FA2 产出 2D 坐标，3D 渲染器直接复用）。
 * 布局算法切换自动重算；"重跑布局"按钮全局可用。
 */
import * as api from './api.js';
import { decorateNodes, inferIfMax } from './scales.js';
import { Renderer2D } from './renderer2d.js';
import { Renderer3D } from './renderer3d.js';
import { Panel } from './panel.js';
import * as filters from './filters.js';
import * as settings from './settings.js';
import { layoutManager } from './layoutManager.js';
import { ALGORITHMS } from './layoutAlgorithms.js';

const $ = (id) => document.getElementById(id);
const MAX_ITER = 600;

class GraphApp {
  constructor() {
    this.panel = new Panel();
    this.nodes = [];
    this.edges = [];
    this.meta = null;
    this.ids = [];
    this.clusters = {};
    this.displayMode = '2d';
    this.layoutAlgorithm = 'fa2';
    this.renderer = null;
    this.worker = null;
    this.paused = false;
    this.style = settings.readStyle();
    this.layout = settings.readLayout();
    this._rafId = null;
    this._waiting = false;
    this._iters = 0;
    this._loaded = false;
  }

  // ── 渲染器 ──────────────────────────────────────────
  buildRenderer() {
    const container = $('lg-canvas');
    if (this.renderer) this.renderer.destroy();
    this.renderer = this.displayMode === '3d' ? new Renderer3D(container) : new Renderer2D(container);
    this.renderer.onClick((id) => this.onNodeClick(id));
    this.renderer.onLongPress((id) => this.onNodeLongPress(id));
    if (this._loaded) this.paint();
  }

  paint() {
    if (!this.renderer) return;
    const ifMax = inferIfMax(this.nodes);
    decorateNodes(this.nodes, { ...settings.decorateOpts(this.style), ifMax });
    this.renderer.setData(this.nodes, this.edges);
    this.renderer.setStyle(settings.rendererStyle(this.style));
    this.panel.setHud(this.nodes.length, this.edges.length, this.meta);
    this.panel.renderLegend(this.style.palette, ifMax);
  }

  repaint() {
    if (!this.renderer) return;
    const ifMax = inferIfMax(this.nodes);
    decorateNodes(this.nodes, { ...settings.decorateOpts(this.style), ifMax });
    this.renderer.updateNodeAttrs(this.nodes);
    this.renderer.setStyle(settings.rendererStyle(this.style));
    this.panel.renderLegend(this.style.palette, ifMax);
  }

  setDisplayMode(mode) {
    if (mode === this.displayMode) return;
    if (this.displayMode === '2d' && this.renderer && this.ids.length) {
      layoutManager.save2dPositions(this._capturePositions2d());
    }
    this.displayMode = mode;
    this.stopLayout();
    this.buildRenderer();
    if (mode === '3d') {
      setTimeout(() => {
        if (this.renderer && this.renderer.fit) this.renderer.fit();
      }, 500);
    } else {
      const saved = layoutManager.restore2dPositions();
      if (saved) {
        this.renderer.applyPositions(saved, this.ids);
        this.paused = true;
      } else if (this.layoutAlgorithm === 'fa2') {
        this.startFA2(true);
      }
    }
    this._sync3dViews();
  }
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
      layoutManager.clearCache();
      layoutManager.init(this.ids, this.edges, this.clusters);
      if (!this.nodes.length) {
        this.panel.setHud(0, 0, this.meta);
        this.panel.setMessage('无匹配节点，放宽过滤条件试试。');
        if (this.renderer) this.renderer.setData([], []);
        return;
      }
      this.paint();
      if (this.displayMode === '2d') {
        this.runLayout(true);
      } else {
        setTimeout(() => {
          if (this.renderer && this.renderer.fit) this.renderer.fit();
        }, 500);
      }
    } catch (e) {
      this.panel.setMessage('图谱加载失败：' + e.message);
    } finally {
      this.panel.setLoading(false);
    }
  }

  // ── 布局调度 ───────────────────────────────────────
  runLayout(reinit) {
    if (!this._loaded || !this.nodes.length) return;
    if (this.displayMode === '3d') return;
    this.stopLayout();
    if (this.layoutAlgorithm === 'fa2') {
      this.startFA2(reinit);
    } else {
      this.runGeometricLayout();
    }
  }

  startFA2(reinit) {
    this._ensureWorker();
    if (reinit) {
      this._iters = 0;
      this.paused = false;
      this.worker.postMessage({
        type: 'init',
        ids: this.ids,
        edges: this.edges.map((e) => ({ source: e.source, target: e.target })),
        clusters: this.clusters,
        settings: settings.layoutSettings(this.layout),
      });
    } else {
      this.worker.postMessage({
        type: 'update-settings',
        settings: settings.layoutSettings(this.layout),
      });
    }
    this._tick();
  }

  runGeometricLayout() {
    const fn = ALGORITHMS[this.layoutAlgorithm];
    if (!fn) return;
    const pos = fn(this.ids, this.edges, this.layout.spacing);
    if (this.renderer) {
      this.renderer.applyPositions(pos, this.ids);
      layoutManager.save2dPositions(pos);
    }
    this.paused = true;
  }

  relayout() {
    this.style = settings.readStyle();
    this.layout = settings.readLayout();
    this.runLayout(true);
  }

  setAlgorithm(algo) {
    this.layoutAlgorithm = algo;
    this._toggleFA2Params(algo === 'fa2');
    this.runLayout(true);
  }

  setSpacing(val) {
    this.layout.spacing = val;
    if (this.layoutAlgorithm !== 'fa2') {
      this.runGeometricLayout();
    }
  }

  _toggleFA2Params(show) {
    const el = $('lg-fa2-params');
    if (el) el.style.display = show ? '' : 'none';
  }

  _sync3dViews() {
    const el = $('lg-3d-views');
    if (el) el.style.display = this.displayMode === '3d' ? '' : 'none';
  }

  // ── FA2 worker ──────────────────────────────────────
  _ensureWorker() {
    if (this.worker) return;
    this.worker = new Worker('/js/graph/layout.worker.js');
    this.worker.onmessage = (e) => {
      if (e.data.type === 'positions') {
        this._waiting = false;
        if (this.renderer && this.displayMode === '2d') {
          const pos = new Float32Array(e.data.positions);
          this.renderer.applyPositions(pos, this.ids);
          layoutManager.save2dPositions(pos);
        }
      }
    };
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
      if (this._iters >= MAX_ITER) { this.paused = true; return; }
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

  viewFront() { if (this.displayMode === '3d' && this.renderer) this.renderer.viewFront(); }
  viewTop() { if (this.displayMode === '3d' && this.renderer) this.renderer.viewTop(); }
  viewSide() { if (this.displayMode === '3d' && this.renderer) this.renderer.viewSide(); }
  viewIsometric() { if (this.displayMode === '3d' && this.renderer) this.renderer.viewIsometric(); }
  viewReset() { if (this.renderer) this.renderer.viewReset ? this.renderer.viewReset() : this.renderer.fit(); }

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
      app.panel.setMessage('渲染器初始化失败：' + e.message);
    }
    app.loadFilters();
    app.loadNetwork();
  }
}

function closeGraph() {
  const modal = $('lit-graph-modal');
  modal.style.display = 'none';
  if (app) { app.destroy(); app = null; }
}

function bindUI() {
  // 顶栏
  $('lg-relayout-global').addEventListener('click', () => {
    app.style = settings.readStyle();
    app.layout = settings.readLayout();
    app.runLayout(true);
  });
  $('lg-fit').addEventListener('click', () => app.fit());
  $('lg-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') app.search(e.target.value); });
  $('lg-side-toggle').addEventListener('click', () => app.panel.toggleSide());
  $('lg-detail-toggle').addEventListener('click', () => app.panel.toggleDetail());

  // 侧栏三 tab
  const panels = { filter: 'lg-panel-filter', layout: 'lg-panel-layout', style: 'lg-panel-style' };
  for (const tab of document.querySelectorAll('.lg-side-tab')) {
    tab.addEventListener('click', () => {
      for (const t of document.querySelectorAll('.lg-side-tab')) t.classList.toggle('active', t === tab);
      const which = tab.dataset.panel;
      for (const [key, id] of Object.entries(panels)) {
        $(id).style.display = key === which ? '' : 'none';
      }
    });
  }

  // 过滤面板
  filters.bindLiveLabels();
  $('lg-apply').addEventListener('click', () => app.loadNetwork());
  $('lg-reset').addEventListener('click', () => { filters.resetFilters(); app.loadNetwork(); });
  const previewBtn = $('lg-preview');
  if (previewBtn) previewBtn.addEventListener('click', () => app.previewFilter());

  // 布局面板：算法切换自动重算
  settings.bindLiveLabels();
  $('lg-l-algorithm').addEventListener('change', (e) => {
    app.layout = settings.readLayout();
    app.setAlgorithm(e.target.value);
  });
  $('lg-s-scaling').addEventListener('change', () => { app.layout = settings.readLayout(); });
  $('lg-s-gravity').addEventListener('change', () => { app.layout = settings.readLayout(); });
  $('lg-l-spacing').addEventListener('input', (e) => {
    app.setSpacing(parseFloat(e.target.value));
  });

  // 样式面板
  const live = () => { app.style = settings.readStyle(); app.repaint(); };
  for (const id of ['lg-s-colorscale', 'lg-s-size', 'lg-s-label', 'lg-s-edges',
                    'lg-s-edgelabel', 'lg-s-bg', 'lg-s-bgcolor', 'lg-s-edgecolor',
                    'lg-s-edgewidth']) {
    const el = $(id);
    el.addEventListener('input', live);
    el.addEventListener('change', live);
  }

  // 2D/3D 显示模式切换（样式面板内）
  $('lg-display-mode').addEventListener('click', (e) => {
    const btn = e.target.closest('.lg-seg-btn');
    if (!btn) return;
    for (const b of $('lg-display-mode').querySelectorAll('.lg-seg-btn')) b.classList.toggle('active', b === btn);
    app.setDisplayMode(btn.dataset.mode);
  });

  // 3D 视角按钮
  for (const [id, method] of [
    ['lg-view-front', 'viewFront'], ['lg-view-top', 'viewTop'],
    ['lg-view-side', 'viewSide'], ['lg-view-iso', 'viewIsometric'],
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
  if (mask) mask.addEventListener('click', (e) => { if (e.target === mask) closeGraph(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && mask && mask.style.display === 'flex') closeGraph();
  });
}

bindEntry();
