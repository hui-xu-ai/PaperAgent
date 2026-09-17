/* graph/renderer2d.js — 2D 平面渲染（sigma.js v2 + WebGL）。
 *
 * 与 renderer3d.js 同构接口，main.js 可无缝切换：
 *   setData / updateNodeAttrs / applyPositions / setStyle /
 *   highlight / clearHighlight / focus / fit / onClick / onLongPress / destroy
 *
 * 坐标来自 layout.worker.js（ForceAtlas2）：主线程增量迭代 → applyPositions 回写。
 * 节点视觉（size/color/label）由 scales.js 在 main.js 算好；本模块只负责画 + 交互。
 */

// UMD 全局解析（库边界防御：不同 sigma 构建导出形状略有差异）。
const _SigmaGlobal = window.Sigma;
const SigmaClass =
  (_SigmaGlobal && (_SigmaGlobal.Sigma || _SigmaGlobal.default)) || _SigmaGlobal;
const GraphCtor =
  (window.graphology && window.graphology.Graph) || window.graphology;

// 高亮配色：引用了中心节点的（citing）↔ 橙；中心节点引用的（cited）↔ 青。
export const HL_CITING = '#f97316';
export const HL_CITED = '#22d3ee';
const DIM_NODE = '#333a45';
const DIM_EDGE = '#1c222b';

// 画布背景样式（dark/light/grid/radial）。
const BACKGROUNDS = {
  dark: { css: '#0e1116' },
  light: { css: '#f4f6fa' },
  grid: {
    css: '#0e1116',
    image:
      'linear-gradient(rgba(255,255,255,.045) 1px, transparent 1px),' +
      'linear-gradient(90deg, rgba(255,255,255,.045) 1px, transparent 1px)',
    size: '34px 34px',
  },
  radial: {
    css: '#0e1116',
    image:
      'radial-gradient(circle at 50% 45%, #1b2430 0%, #0e1116 60%, #070a0e 100%)',
  },
};

export class Renderer2D {
  constructor(container) {
    this._container = container;
    this._graph = new GraphCtor({ multi: false, type: 'directed' });
    this._byId = new Map();          // id → 原始节点（详情用）
    this._hl = null;                 // { center, citing:Set, cited:Set }
    this._style = { showEdges: true, edgeColor: '#3a4150', background: 'dark', bgColor: null };
    this._clickCb = null;
    this._longCb = null;
    this._hoverNode = null;
    this._longTimer = null;

    this._renderer = new SigmaClass(this._graph, container, {
      renderLabels: true,
      labelRenderedSizeThreshold: 7,
      defaultEdgeType: 'line',
      defaultEdgeColor: this._style.edgeColor,
      defaultNodeColor: '#4f9cf9',
      zIndex: true,
      allowInvalidContainer: false,
    });

    this._wireReducers();
    this._wireEvents();
  }

  // ───────────────────────────────────────────────── 数据
  /** 重建图（nodes 已带 size/color/label；x/y 可缺省→种子由 worker 负责）。 */
  setData(nodes, edges) {
    this._graph.clearGraph();
    this._byId.clear();
    for (const n of nodes) {
      this._byId.set(n.id, n);
      if (!this._graph.hasNode(n.id)) {
        this._graph.addNode(n.id, {
          x: n.x || Math.random() * 100,
          y: n.y || Math.random() * 100,
          size: n.size || 2,
          color: n.color || '#4f9cf9',
          label: n.label || '',
        });
      }
    }
    for (const e of edges || []) {
      if (e.source === e.target) continue;
      if (!this._graph.hasNode(e.source) || !this._graph.hasNode(e.target)) continue;
      try {
        this._graph.addEdge(e.source, e.target, {
          size: 0.6,
          color: this._style.edgeColor,
          hidden: !this._style.showEdges,
          type: 'arrow',
        });
      } catch (_) { /* 重复边忽略 */ }
    }
    this._renderer.refresh();
  }

  /** 仅更新既有节点的 size/color/label（样式变更时用，避免重建丢坐标）。 */
  updateNodeAttrs(nodes) {
    for (const n of nodes) {
      if (!this._graph.hasNode(n.id)) continue;
      this._byId.set(n.id, n);
      this._graph.setNodeAttribute(n.id, 'size', n.size || 2);
      this._graph.setNodeAttribute(n.id, 'color', n.color || '#4f9cf9');
      this._graph.setNodeAttribute(n.id, 'label', n.label || '');
    }
    this._renderer.refresh();
  }

  /** 回写 worker 计算的 FA2 坐标（positions: Float32Array(2N)，顺序同 ids）。 */
  applyPositions(positions, ids) {
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      if (!this._graph.hasNode(id)) continue;
      this._graph.setNodeAttribute(id, 'x', positions[2 * i]);
      this._graph.setNodeAttribute(id, 'y', positions[2 * i + 1]);
    }
    this._renderer.refresh();
  }

  /** 视觉样式（背景 / 边显隐 / 边色）。 */
  setStyle(style = {}) {
    Object.assign(this._style, style);
    // 背景
    const bg = BACKGROUNDS[this._style.background] || BACKGROUNDS.dark;
    const base = this._style.bgColor || bg.css;
    this._container.style.background = base;
    this._container.style.backgroundImage = bg.image || 'none';
    if (bg.size) this._container.style.backgroundSize = bg.size;
    else this._container.style.backgroundSize = '';
    // 边
    const show = !!this._style.showEdges;
    this._renderer.setSetting('defaultEdgeColor', this._style.edgeColor);
    this._renderer.setSetting('renderEdgeLabels', false);
    this._graph.forEachEdge((_e, _a, s, t) => {
      this._graph.setEdgeAttribute(this._graph.edge(s, t), 'hidden', !show);
      this._graph.setEdgeAttribute(this._graph.edge(s, t), 'color', this._style.edgeColor);
    });
    this._renderer.refresh();
  }

  // ───────────────────────────────────────────────── 高亮
  /** 长按高亮：center + 引用它的(citing) + 它引用的(cited)。 */
  highlight(center, citing = [], cited = []) {
    this._hl = {
      center,
      citing: new Set(citing.filter((id) => this._graph.hasNode(id))),
      cited: new Set(cited.filter((id) => this._graph.hasNode(id))),
    };
    this._renderer.refresh();
  }

  clearHighlight() {
    this._hl = null;
    this._renderer.refresh();
  }

  // ───────────────────────────────────────────────── 相机
  focus(id) {
    if (!this._graph.hasNode(id)) return false;
    const data = this._renderer.getNodeDisplayData(id);
    if (!data) return false;
    const bbox = this._renderer.getGraphDimensions();
    const cam = this._renderer.getCamera();
    const cx = bbox.width ? (data.x - bbox.x) / bbox.width : 0.5;
    const cy = bbox.height ? (data.y - bbox.y) / bbox.height : 0.5;
    cam.animate({ x: cx, y: cy, ratio: 0.18 }, { duration: 320 });
    return true;
  }

  fit() {
    this._renderer.getCamera().animatedReset({ duration: 320 });
  }

  // ───────────────────────────────────────────────── 交互
  onClick(cb) { this._clickCb = cb; return this; }
  onLongPress(cb) { this._longCb = cb; return this; }

  /** 原始节点数据（详情面板用）。 */
  getNodeData(id) { return this._byId.get(id) || null; }

  destroy() {
    this._clearLongTimer();
    try { this._renderer.kill(); } catch (_) {}
    this._graph.clearGraph();
    this._byId.clear();
  }

  // ───────────────────────────────────────────────── 内部
  _wireReducers() {
    this._renderer.setNodeReducer((node, data) => {
      if (!this._hl) return data;
      const res = { ...data };
      if (node === this._hl.center) {
        res.color = '#ffffff';
        res.highlighted = true;
      } else if (this._hl.citing.has(node)) {
        res.color = HL_CITING;
      } else if (this._hl.cited.has(node)) {
        res.color = HL_CITED;
      } else {
        res.color = DIM_NODE;
        res.label = '';
      }
      return res;
    });
    this._renderer.setEdgeReducer((edge, data) => {
      const res = { ...data };
      if (!this._hl) return res;
      const [s, t] = this._graph.extremities(edge);
      if (s === this._hl.center) { res.color = HL_CITED; res.size = 1.4; res.hidden = false; }
      else if (t === this._hl.center) { res.color = HL_CITING; res.size = 1.4; res.hidden = false; }
      else { res.hidden = true; res.color = DIM_EDGE; }
      return res;
    });
  }

  _wireEvents() {
    this._renderer.on('clickNode', (e) => {
      this._clearLongTimer();
      if (this._clickCb) this._clickCb(e.node);
    });
    this._renderer.on('enterNode', (e) => { this._hoverNode = e.node; });
    this._renderer.on('leaveNode', () => { this._hoverNode = null; this._clearLongTimer(); });
    this._renderer.on('clickStage', () => { this.clearHighlight(); });

    // 长按（450ms）：基于当前悬停节点 + 容器按下计时。
    this._container.addEventListener('mousedown', () => {
      if (!this._hoverNode || !this._longCb) return;
      const target = this._hoverNode;
      this._clearLongTimer();
      this._longTimer = setTimeout(() => {
        this._longTimer = null;
        if (this._longCb) this._longCb(target);
      }, 450);
    });
    this._container.addEventListener('mouseup', () => this._clearLongTimer());
    this._container.addEventListener('mouseleave', () => this._clearLongTimer());
  }

  _clearLongTimer() {
    if (this._longTimer) { clearTimeout(this._longTimer); this._longTimer = null; }
  }
}
