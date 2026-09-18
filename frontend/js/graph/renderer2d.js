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

// 画布背景样式（dark/light/grid/radial/gradient）。
const BACKGROUNDS = {
  dark: { css: '#0e1116' },
  light: { css: '#f4f6fa' },
  grid: {
    css: '#f4f6fa',
    image:
      'linear-gradient(rgba(0,0,0,.06) 1px, transparent 1px),' +
      'linear-gradient(90deg, rgba(0,0,0,.06) 1px, transparent 1px)',
    size: '34px 34px',
  },
  radial: {
    css: '#f4f6fa',
    image:
      'radial-gradient(circle at 50% 45%, #ffffff 0%, #e8ecf2 50%, #c8d0dc 100%)',
  },
  gradient: {
    css: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
  },
};

// 背景 → 标签颜色映射（深色背景用白字，浅色背景用黑字）。
const BG_LABEL_COLOR = {
  dark: '#ffffff',
  light: '#1a1d23',
  grid: '#1a1d23',
  radial: '#1a1d23',
  gradient: '#ffffff',
};

/** 判断 HEX 颜色是否为浅色（用于标签颜色自适应）。 */
function _isLightHex(hex) {
  const c = hex.replace('#', '');
  const r = parseInt(c.substring(0, 2), 16);
  const g = parseInt(c.substring(2, 4), 16);
  const b = parseInt(c.substring(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5;
}

/** 自定义标签渲染器：先画描边（shadowBlur），再画文字，确保任何背景下都可读。 */
function _labelRendererWithHalo(ctx, node, settings) {
  const label = node.label;
  if (!label) return;
  const fontSize = settings.labelSize || 10;
  const font = settings.labelFont || 'Arial';
  const weight = settings.labelWeight || 'normal';
  const lc = settings.labelColor;
  const color = typeof lc === 'string' ? lc : (lc?.color || '#000');
  const isLight = _isLightHex(color.replace('#', '').length === 6 ? color : '#ffffff');
  const haloColor = isLight ? 'rgba(0,0,0,0.7)' : 'rgba(255,255,255,0.8)';

  ctx.font = `${weight} ${fontSize}px ${font}`;
  const x = node.x + node.size + 3;
  const y = node.y + fontSize / 3;

  ctx.save();
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = 0;
  ctx.shadowBlur = 6;
  ctx.shadowColor = haloColor;
  ctx.fillStyle = color;
  ctx.fillText(label, x, y);
  ctx.fillText(label, x, y);
  ctx.restore();
}

export class Renderer2D {
  constructor(container) {
    this._container = container;
    this._graph = new GraphCtor({ multi: false, type: 'directed' });
    this._byId = new Map();          // id → 原始节点（详情用）
    this._hl = null;                 // { center, citing:Set, cited:Set }
    this._style = { showEdges: true, edgeColor: '#8890a0', background: 'light', bgColor: null };
    this._clickCb = null;
    this._longCb = null;
    this._hoverNode = null;
    this._longTimer = null;
    this._panning = false;
    this._panStart = null;
    this._panMoved = false;

    this._renderer = new SigmaClass(this._graph, container, {
      renderLabels: true,
      labelRenderedSizeThreshold: 5,
      labelRenderer: _labelRendererWithHalo,
      defaultEdgeType: 'line',
      defaultEdgeColor: this._style.edgeColor,
      defaultNodeColor: '#4f9cf9',
      zIndex: true,
      allowInvalidContainer: true,
      mouseEnabled: false,
      mouseWheelEnabled: true,
    });
    this._container.style.cursor = 'grab';

    this._origAttrs = null;
    this._tooltip = null;
    this._wireEvents();
    this._wirePan();
    this._createTooltip();
  }

  // ───────────────────────────────────────────────── 数据
  /** 重建图（nodes 已带 size/color/label；x/y 可缺省→种子由 worker 负责）。 */
  setData(nodes, edges) {
    this._graph.clear();
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
        const edgeSize = this._style.edgeWidth != null ? this._style.edgeWidth : 1.2;
        const edgeId = this._graph.addEdge(e.source, e.target, {
          size: edgeSize,
          color: this._style.edgeColor || '#8890a0',
          hidden: !this._style.showEdges,
          type: 'arrow',
        });
        // 确保边线属性正确设置（sigma 可能覆盖）
        this._graph.setEdgeAttribute(edgeId, 'size', edgeSize);
        this._graph.setEdgeAttribute(edgeId, 'color', this._style.edgeColor || '#8890a0');
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

  /** 视觉样式（背景 / 边显隐 / 边色 / 标签颜色）。 */
  setStyle(style = {}) {
    Object.assign(this._style, style);
    const bg = BACKGROUNDS[this._style.background] || BACKGROUNDS.light;
    const base = this._style.bgColor || bg.css;
    if (base.includes('gradient')) {
      this._container.style.background = '';
      this._container.style.backgroundImage = base;
    } else {
      this._container.style.background = base;
      this._container.style.backgroundImage = bg.image || 'none';
    }
    if (bg.size) this._container.style.backgroundSize = bg.size;
    else this._container.style.backgroundSize = '';
    const isLight = this._isCurrentBgLight();
    let labelColor;
    if (this._style.bgColor) {
      labelColor = _isLightHex(this._style.bgColor) ? '#1a1d23' : '#ffffff';
    } else {
      labelColor = BG_LABEL_COLOR[this._style.background] || BG_LABEL_COLOR.light;
    }
    this._renderer.setSetting('labelColor', labelColor);
    this._renderer.setSetting('labelSize', 10);
    const edgeColor = this._style.edgeColor || (isLight ? '#5a6270' : '#a0aab8');
    const edgeWidth = this._style.edgeWidth != null ? this._style.edgeWidth : 1.2;
    this._renderer.setSetting('defaultEdgeColor', edgeColor);
    this._renderer.setSetting('defaultEdgeSize', edgeWidth);
    this._renderer.setSetting('renderEdgeLabels', false);
    if (this._tooltip) {
      this._tooltip.style.color = isLight ? '#1a1d23' : '#f0f2f5';
      this._tooltip.style.background = isLight ? 'rgba(255,255,255,.92)' : 'rgba(22,27,35,.92)';
      this._tooltip.style.borderColor = isLight ? '#c8cdd5' : '#3a4150';
    }
    const show = !!this._style.showEdges;
    this._graph.forEachEdge((edge) => {
      this._graph.setEdgeAttribute(edge, 'hidden', !show);
      this._graph.setEdgeAttribute(edge, 'color', edgeColor);
      this._graph.setEdgeAttribute(edge, 'size', edgeWidth);
    });
    this._renderer.refresh();
  }

  _isCurrentBgLight() {
    const bg = BACKGROUNDS[this._style.background] || BACKGROUNDS.light;
    const hex = this._style.bgColor || bg.css;
    if (!hex) return false;
    if (hex.includes('gradient')) {
      // 从渐变中提取第一个颜色值判断
      const match = hex.match(/#[0-9a-fA-F]{6}/);
      if (match) return _isLightHex(match[0]);
      return false; // 无法解析的渐变默认视为深色
    }
    return _isLightHex(hex);
  }

  _createTooltip() {
    const tip = document.createElement('div');
    tip.className = 'lg-tooltip';
    tip.style.cssText = 'position:fixed;z-index:9999;pointer-events:none;padding:6px 10px;' +
      'border-radius:6px;font:12px system-ui,sans-serif;max-width:320px;line-height:1.4;' +
      'word-break:break-word;display:none;border:1px solid;box-shadow:0 2px 8px rgba(0,0,0,.12);';
    document.body.appendChild(tip);
    this._tooltip = tip;
  }

  _showTooltip(nodeId, x, y) {
    if (!this._tooltip) return;
    const data = this._byId.get(nodeId);
    const title = data ? (data.title || data.id) : nodeId;
    this._tooltip.textContent = title;
    this._tooltip.style.display = 'block';
    this._tooltip.style.left = (x + 14) + 'px';
    this._tooltip.style.top = (y + 14) + 'px';
  }

  _hideTooltip() {
    if (this._tooltip) this._tooltip.style.display = 'none';
  }

  // ───────────────────────────────────────────────── 高亮
  /** 长按高亮：center + 引用它的(citing) + 它引用的(cited)。 */
  highlight(center, citing = [], cited = []) {
    this._hl = {
      center,
      citing: new Set(citing.filter((id) => this._graph.hasNode(id))),
      cited: new Set(cited.filter((id) => this._graph.hasNode(id))),
    };
    this._applyHighlight();
  }

  clearHighlight() {
    this._hl = null;
    this._restoreAttrs();
    const edgeWidth = this._style.edgeWidth != null ? this._style.edgeWidth : 1.2;
    this._graph.forEachEdge((edge) => {
      this._graph.setEdgeAttribute(edge, 'hidden', !this._style.showEdges);
      this._graph.setEdgeAttribute(edge, 'color', this._style.edgeColor);
      this._graph.setEdgeAttribute(edge, 'size', edgeWidth);
    });
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
    if (this._tooltip && this._tooltip.parentNode) this._tooltip.remove();
    this._tooltip = null;
    try { this._renderer.kill(); } catch (_) {}
    this._graph.clear();
    this._byId.clear();
  }

  // ───────────────────────────────────────────────── 内部
  /** 高亮：直接修改节点/边属性（vendor sigma 无 setNodeReducer API）。 */
  _applyHighlight() {
    if (!this._hl) return;
    const { center, citing, cited } = this._hl;
    if (!this._origAttrs) {
      this._origAttrs = new Map();
      this._graph.forEachNode((id, attrs) => {
        this._origAttrs.set(id, { label: attrs.label || '', color: attrs.color || '#4f9cf9', size: attrs.size || 2 });
      });
    }
    this._graph.forEachNode((id) => {
      const orig = this._origAttrs.get(id);
      let color, size;
      if (id === center) {
        color = '#ffffff'; size = orig.size * 1.3;
      } else if (citing.has(id)) {
        color = HL_CITING; size = orig.size;
      } else if (cited.has(id)) {
        color = HL_CITED; size = orig.size;
      } else {
        color = DIM_NODE; size = orig.size * 0.5;
      }
      this._graph.setNodeAttribute(id, 'color', color);
      this._graph.setNodeAttribute(id, 'size', size);
      // 保留原始标签（年份+被引），不清空
    });
    this._graph.forEachEdge((edge, _attrs, s, t) => {
      let color, size, hidden;
      if (s === center) { color = HL_CITED; size = 2; hidden = false; }
      else if (t === center) { color = HL_CITING; size = 2; hidden = false; }
      else { color = DIM_EDGE; size = 0.4; hidden = true; }
      this._graph.setEdgeAttribute(edge, 'color', color);
      this._graph.setEdgeAttribute(edge, 'size', size);
      this._graph.setEdgeAttribute(edge, 'hidden', hidden);
    });
    this._renderer.refresh();
  }

  _restoreAttrs() {
    if (!this._origAttrs) return;
    this._graph.forEachNode((id) => {
      const o = this._origAttrs.get(id);
      if (!o) return;
      this._graph.setNodeAttribute(id, 'label', o.label);
      this._graph.setNodeAttribute(id, 'color', o.color);
      this._graph.setNodeAttribute(id, 'size', o.size);
    });
    this._origAttrs = null;
  }

  /** 平移：中键拖拽 或 Shift+左键拖拽（左键单击留给节点选择）。 */
  _wirePan() {
    const cam = () => this._renderer.getCamera();
    let moved = false;
    this._container.addEventListener('mousedown', (e) => {
      // 中键 或 Shift+左键 → 平移
      if (e.button === 1 || (e.button === 0 && e.shiftKey)) {
        e.preventDefault();
        this._panning = true;
        moved = false;
        this._panStart = { x: e.clientX, y: e.clientY };
        this._container.style.cursor = 'grabbing';
      }
    });
    window.addEventListener('mousemove', (e) => {
      if (!this._panning || !this._panStart) return;
      const dx = e.clientX - this._panStart.x;
      const dy = e.clientY - this._panStart.y;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
      const c = cam();
      const ratio = c.ratio || 1;
      c.x -= (dx / this._container.offsetWidth) * ratio * 2;
      c.y += (dy / this._container.offsetHeight) * ratio * 2;
      this._panStart = { x: e.clientX, y: e.clientY };
    });
    window.addEventListener('mouseup', () => {
      if (this._panning) {
        this._panning = false;
        this._panStart = null;
        this._container.style.cursor = 'grab';
        this._panMoved = moved;
      }
    });
    this._container.addEventListener('wheel', () => {}, { passive: true });
  }

  _wireEvents() {
    this._renderer.on('clickNode', (e) => {
      this._clearLongTimer();
      if (this._clickCb) this._clickCb(e.node);
    });
    this._renderer.on('enterNode', (e) => {
      this._hoverNode = e.node;
      if (this._tooltip && this._lastMouse) {
        this._showTooltip(e.node, this._lastMouse.x, this._lastMouse.y);
      }
    });
    this._renderer.on('leaveNode', () => {
      this._hoverNode = null;
      this._hideTooltip();
      this._clearLongTimer();
    });
    this._renderer.on('clickStage', () => {
      if (!this._panMoved) this.clearHighlight();
    });
    this._container.addEventListener('mousemove', (e) => {
      this._lastMouse = { x: e.clientX, y: e.clientY };
      if (this._hoverNode) this._showTooltip(this._hoverNode, e.clientX, e.clientY);
    });
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
