/* graph/renderer3d.js — 3D 立体渲染（3d-force-graph，three.js / WebGL）。
 *
 * 与 renderer2d.js 同构接口。3D 自带 d3-force-3d 物理引擎 → 不需要 FA2 worker：
 *   applyPositions() 为 no-op；布局由库内部驱动（main.js 仅在 2D 模式跑 worker）。
 * 背景样式统一走容器 CSS + 透明场景，与 2D 表现一致。
 */
import { HL_CITING, HL_CITED } from './renderer2d.js';

const ForceGraph3D = window.ForceGraph3D;
const DIM_NODE = '#2c333d';

const BACKGROUNDS = {
  dark: '#0e1116',
  light: '#f4f6fa',
  grid: '#f4f6fa',
  radial: '#f4f6fa',
  gradient: '#667eea',
};
const BG_IMAGES = {
  dark: 'none',
  light: 'none',
  grid:
    'linear-gradient(rgba(0,0,0,.06) 1px, transparent 1px),' +
    'linear-gradient(90deg, rgba(0,0,0,.06) 1px, transparent 1px)',
  radial:
    'radial-gradient(circle at 50% 45%, #ffffff 0%, #e8ecf2 50%, #c8d0dc 100%)',
  gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
};

export class Renderer3D {
  constructor(container) {
    this._container = container;
    this._byId = new Map();
    this._hl = null;
    this._style = { showEdges: true, edgeColor: '#8890a0', background: 'light', bgColor: null, showEdgeDir: false };
    this._clickCb = null;
    this._longCb = null;
    this._hoverNode = null;
    this._longTimer = null;

    const initialTipColor = this._computeTipColor(this._style.background, this._style.bgColor);
    this._g = ForceGraph3D({ controlType: 'orbit' })(container)
      .backgroundColor('rgba(0,0,0,0)')
      .showNavInfo(false)
      .nodeId('id')
      .nodeColor((d) => this._nodeColor(d))
      .nodeVal((d) => Math.max(0.4, (d.size || 2)) ** 2.2)
      .nodeOpacity(0.92)
      .nodeLabel((d) => `<div style="font:12px system-ui;color:${initialTipColor}">${this._esc(d.label || d.title || d.id)}</div>`)
      .nodeResolution(12)
      .linkColor(() => this._style.edgeColor)
      .linkOpacity(0.32)
      .linkWidth(() => this._style.edgeWidth ? this._style.edgeWidth * 0.35 : 0.4)
      .linkDirectionalArrowLength(() => (this._style.showEdgeDir ? 2.2 : 0))
      .linkDirectionalArrowRelPos(1)
      .linkVisibility(() => this._style.showEdges)
      .onNodeClick((node) => { this._clearLongTimer(); if (this._clickCb) this._clickCb(node.id); })
      .onNodeHover((node) => { this._hoverNode = node ? node.id : null; });

    this._g.d3Force('charge').strength(-42);
    this._g.d3Force('link').distance((l) => {
      const srcSize = l.source.size || 2;
      const tgtSize = l.target.size || 2;
      const avgSize = (srcSize + tgtSize) / 2;
      return Math.max(18, avgSize * 6);
    });
    this._wireLongPress();
  }

  /** 根据背景计算标签颜色（深色→白，浅色→黑）。 */
  _computeTipColor(background, bgColor) {
    const hex = bgColor || BACKGROUNDS[background] || BACKGROUNDS.light;
    if (!hex || hex.includes('gradient')) return '#ffffff';
    const c = hex.replace('#', '');
    if (c.length < 6) return '#ffffff';
    const r = parseInt(c.substring(0, 2), 16);
    const g = parseInt(c.substring(2, 4), 16);
    const b = parseInt(c.substring(4, 6), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 ? '#1a1d23' : '#ffffff';
  }

  _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  setData(nodes, edges) {
    this._byId.clear();
    const gnodes = nodes.map((n) => { this._byId.set(n.id, n); return { ...n }; });
    const links = (edges || []).map((e) => ({ source: e.source, target: e.target }));
    this._g.graphData({ nodes: gnodes, links });
  }

  updateNodeAttrs(nodes) {
    for (const n of nodes) this._byId.set(n.id, n);
    // 合并到当前图数据（保留物理坐标）。
    const cur = this._g.graphData();
    const map = new Map(nodes.map((n) => [n.id, n]));
    for (const gn of cur.nodes) {
      const src = map.get(gn.id);
      if (src) Object.assign(gn, { size: src.size, color: src.color, label: src.label, title: src.title });
    }
    this._g.nodeColor(this._g.nodeColor());   // 触发重绘
    this._g.nodeVal(this._g.nodeVal());
    this._g.nodeLabel(this._g.nodeLabel());   // 触发标签重绘
  }

  applyPositions() { /* 3D 用自带物理引擎，忽略 worker 坐标 */ }

  setStyle(style = {}) {
    Object.assign(this._style, style);
    const base = this._style.bgColor || BACKGROUNDS[this._style.background] || BACKGROUNDS.gradient;
    if (base.includes('gradient')) {
      this._container.style.background = '';
      this._container.style.backgroundImage = base;
    } else {
      this._container.style.background = base;
      this._container.style.backgroundImage = BG_IMAGES[this._style.background] || 'none';
    }
    this._container.style.backgroundSize = this._style.background === 'grid' ? '34px 34px' : '';
    const tipColor = this._computeTipColor(this._style.background, this._style.bgColor);
    this._g.nodeLabel((d) => `<div style="font:12px system-ui;color:${tipColor}">${this._esc(d.label || d.title || d.id)}</div>`)
      .linkColor(this._style.edgeColor)
      .linkWidth(this._style.edgeWidth ? this._style.edgeWidth * 0.35 : 0.4)
      .linkVisibility(!!this._style.showEdges)
      .linkDirectionalArrowLength(this._style.showEdgeDir ? 2.2 : 0);
  }

  _nodeColor(d) {
    if (!this._hl) return d.color || '#4f9cf9';
    if (d.id === this._hl.center) return '#ffffff';
    if (this._hl.citing.has(d.id)) return HL_CITING;
    if (this._hl.cited.has(d.id)) return HL_CITED;
    return DIM_NODE;
  }

  highlight(center, citing = [], cited = []) {
    this._hl = { center, citing: new Set(citing), cited: new Set(cited) };
    this._g.nodeColor(this._g.nodeColor());
  }

  clearHighlight() {
    this._hl = null;
    this._g.nodeColor(this._g.nodeColor());
  }

  focus(id) {
    const node = this._byId.get(id);
    if (!node) return false;
    this._g.centerAt(node.x || 0, node.y || 0, 500);
    this._g.zoom(5, 500);
    return true;
  }

  fit() { this._g.zoomToFit(500, 50); }

  onClick(cb) { this._clickCb = cb; return this; }
  onLongPress(cb) { this._longCb = cb; return this; }
  getNodeData(id) { return this._byId.get(id) || null; }

  _wireLongPress() {
    this._container.addEventListener('mousedown', () => {
      if (!this._hoverNode || !this._longCb) return;
      const target = this._hoverNode;
      this._clearLongTimer();
      this._longTimer = setTimeout(() => { this._longTimer = null; if (this._longCb) this._longCb(target); }, 450);
    });
    this._container.addEventListener('mouseup', () => this._clearLongTimer());
    this._container.addEventListener('mouseleave', () => { this._hoverNode = null; this._clearLongTimer(); });
  }
  _clearLongTimer() { if (this._longTimer) { clearTimeout(this._longTimer); this._longTimer = null; } }

  destroy() {
    this._clearLongTimer();
    try { this._g._destructor && this._g._destructor(); } catch (_) {}
    this._container.innerHTML = '';
    this._byId.clear();
  }
}
