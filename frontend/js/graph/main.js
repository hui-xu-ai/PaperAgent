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
    const ifMax = inferIfMax(this.nodes);
    decorateNodes(this.nodes, { ...settings.decorateOpts(this.style), ifMax });
    this.renderer.setData(this.nodes, this.edges);
    this.renderer.setStyle(settings.rendererStyle(this.style));
    this.panel.setHud(this.nodes.length, this.edges.length, this.meta);
    this.panel.renderLegend(this.style.palette, ifMax);
  }

  /** 仅刷新视觉（样式变更，不重建布局）。 */
  repaint() {
    const ifMax = inferIfMax(this.nodes);
    decorateNodes(this.nodes, { ...settings.decorateOpts(this.style), ifMax });
    this.renderer.updateNodeAttrs(this.nodes);
    this.renderer.setStyle(settings.rendererStyle(this.style));
    this.panel.renderLegend(this.style.palette, ifMax);
  }

  setMode(mode) {
    if (mode === this.mode) return;
    this.mode = mode;
    this.stopLayout();
    this.buildRenderer();
    if (mode === '2d') this.startLayout(true);
    else this.paused = true;       // 3D 用自带物理，无需 worker
    this._syncLayoutBtn();
  }

  // ── 数据加载 ────────────────────────────────────────
  async loadFilters() {
    try {
      const facets = await api.getFilters();
      filters.populateFacets(facets);
      filters.syncLabels();
    } catch (e) {
      this.panel.setMessage('分面加载失败：' + e.message);
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
      if (!this.nodes.length) {
        this.panel.setHud(0, 0, this.meta);
        this.panel.setMessage('无匹配节点，放宽过滤条件试试。');
        if (this.renderer) this.renderer.setData([], []);
        return;
      }
      this.paint();
      if (this.mode === '2d') this.startLayout(true);
    } catch (e) {
      this.panel.setMessage('图谱加载失败：' + e.message);
    } finally {
      this.panel.setLoading(false);
    }
  }

  // ── FA2 worker 布局 ─────────────────────────────────
  _ensureWorker() {
    if (this.worker) return;
    this.worker = new Worker('/js/graph/layout.worker.js');
    this.worker.onmessage = (e) => {
      if (e.data.type === 'positions') {
        this._waiting = false;
        if (this.mode === '2d' && this.renderer) {
          this.renderer.applyPositions(new Float32Array(e.data.positions), this.ids);
        }
      }
    };
  }

  startLayout(reinit) {
    if (this.mode !== '2d' || !this._loaded || !this.nodes.length) return;
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
    }
    this._syncLayoutBtn();
    this._tick();
  }

  stopLayout() {
    if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = null; }
  }

  _tick() {
    this.stopLayout();
    if (this.mode !== '2d' || this.paused) return;
    const loop = () => {
      this._rafId = null;
      if (this.mode !== '2d' || this.paused) return;
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
    if (this.mode !== '2d') return;
    if (this._iters >= MAX_ITER) this._iters = 0;   // 收敛后再点 → 继续迭代
    this.paused = !this.paused;
    if (!this.paused) this._tick();
    else this.stopLayout();
    this._syncLayoutBtn();
  }

  _syncLayoutBtn() {
    const b = $('lg-layout-toggle');
    if (!b) return;
    b.textContent = this.paused || this.mode === '3d' ? '▶ 继续布局' : '⏸ 暂停布局';
    b.disabled = this.mode === '3d';
  }

  relayout() {
    if (this.mode === '2d') this.startLayout(true);
    else if (this.renderer && this.renderer._g) this.renderer._g.d3ReheatSimulation();
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
    app.buildRenderer();
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

  // 样式面板（实时预览）
  settings.bindLiveLabels();
  const style = settings.readStyle;
  const live = () => { app.style = style(); app.repaint(); };
  for (const id of ['lg-s-colorscale', 'lg-s-size', 'lg-s-label', 'lg-s-edges',
                    'lg-s-edgelabel', 'lg-s-bg', 'lg-s-bgcolor', 'lg-s-edgecolor']) {
    const el = $(id);
    el.addEventListener('input', live);
    el.addEventListener('change', live);
  }
  // 引力参数：改完点「重跑布局」生效（避免拖动时频繁 reinit）。
  $('lg-s-scaling').addEventListener('change', () => { app.style = style(); });
  $('lg-s-gravity').addEventListener('change', () => { app.style = style(); });
  $('lg-relayout').addEventListener('click', () => { app.style = style(); app.relayout(); });

  // 详情面板关闭
  $('lg-detail-close').addEventListener('click', () => {
    app.panel.toggleDetail(false);
    app.panel.showDetailPlaceholder();
    if (app.renderer) app.renderer.clearHighlight();
  });
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
