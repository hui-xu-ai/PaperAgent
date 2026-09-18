/* graph/renderer2d.js — 2D 平面渲染器（sigma.js v2 + WebGL）。
 *
 * 新架构原则：
 *   - 纯渲染层，不持有任何业务状态
 *   - setData() 接收已计算好所有属性的数据
 *   - setStyle() 只改渲染参数，不重建图
 *   - applyPositions() 只写坐标，不触发相机重置
 */

// UMD 全局解析
const _SigmaGlobal = window.Sigma;
const SigmaClass = (_SigmaGlobal && (_SigmaGlobal.Sigma || _SigmaGlobal.default)) || _SigmaGlobal;
const GraphCtor = (window.graphology && window.graphology.Graph) || window.graphology;

// 高亮配色
export const HL_CITING = '#f97316';
export const HL_CITED = '#22d3ee';
const DIM_NODE = '#333a45';
const DIM_EDGE = '#1c222b';

// 背景样式
const BACKGROUNDS = {
  dark: { css: '#0e1116' },
  light: { css: '#f4f6fa' },
  grid: {
    css: '#f4f6fa',
    image: 'linear-gradient(rgba(0,0,0,.06) 1px, transparent 1px), linear-gradient(90deg, rgba(0,0,0,.06) 1px, transparent 1px)',
    size: '34px 34px',
  },
  radial: {
    css: '#f4f6fa',
    image: 'radial-gradient(circle at 50% 45%, #ffffff 0%, #e8ecf2 50%, #c8d0dc 100%)',
  },
  gradient: {
    css: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
  },
};

/** 判断 HEX 颜色是否为浅色。 */
function _isLightHex(hex) {
  const c = hex.replace('#', '');
  if (c.length < 6) return false;
  const r = parseInt(c.substring(0, 2), 16);
  const g = parseInt(c.substring(2, 4), 16);
  const b = parseInt(c.substring(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5;
}

/** 自定义标签渲染器：描边光晕确保可读性。 */
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
    this._byId = new Map(); // id → 原始节点数据
    this._hl = null; // 高亮状态 { center, citing:Set, cited:Set }
    this._origAttrs = null; // 高亮前的原始属性
    this._clickCb = null;
    this._longCb = null;
    this._hoverNode = null;
    this._longTimer = null;
    this._panning = false;
    this._panStart = null;
    this._panMoved = false;
    this._tooltip = null;

    this._renderer = new SigmaClass(this._graph, container, {
      renderLabels: true,
      labelRenderedSizeThreshold: 5,
      labelRenderer: _labelRendererWithHalo,
      defaultEdgeType: 'line',
      defaultEdgeColor: '#8890a0',
      defaultNodeColor: '#4f9cf9',
      zIndex: true,
      allowInvalidContainer: true,
      mouseEnabled: false,
      mouseWheelEnabled: true,
    });

    this._container.style.cursor = 'grab';
    this._createTooltip();
    this._wireEvents();
    this._wirePan();
  }

  /** 设置数据（接收已计算好所有属性的格式）。 */
  setData(renderData) {
    this._graph.clear();
    this._byId.clear();

    const { nodes, edges } = renderData;

    // 添加节点
    for (const n of nodes) {
      this._byId.set(n.id, n._raw || n);
      this._graph.addNode(n.id, {
        x: n.x || Math.random() * 100,
        y: n.y || Math.random() * 100,
        size: n.size,
        color: n.color,
        label: n.label,
      });
    }

    // 添加边（**无最小值限制！**）
    for (const e of edges) {
      if (e.source === e.target) continue;
      if (!this._graph.hasNode(e.source) || !this._graph.hasNode(e.target)) continue;
      try {
        this._graph.addEdge(e.source, e.target, {
          size: e.width, // 直接使用用户设置的值
          color: e.color,
          hidden: e.hidden,
          type: 'arrow',
        });
      } catch (_) { /* 重复边忽略 */ }
    }

    this._renderer.refresh();
  }

  /** 更新节点属性（用于动态样式变更）。 */
  updateNodeAttrs(nodes) {
    for (const n of nodes) {
      if (!this._graph.hasNode(n.id)) continue;
      this._byId.set(n.id, n._raw || n);
      this._graph.setNodeAttribute(n.id, 'size', n.size);
      this._graph.setNodeAttribute(n.id, 'color', n.color);
      this._graph.setNodeAttribute(n.id, 'label', n.label);
    }
    this._renderer.refresh();
  }

  /** 应用坐标（只写x/y，不触发相机重置）。 */
  applyPositions(positions, ids) {
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      if (!this._graph.hasNode(id)) continue;
      this._graph.setNodeAttribute(id, 'x', positions[2 * i]);
      this._graph.setNodeAttribute(id, 'y', positions[2 * i + 1]);
    }
    this._renderer.refresh();
  }

  /** 设置样式（只改渲染参数，不重建图）。 */
  setStyle(styleOpts) {
    const {
      showEdges = true,
      background = 'light',
      bgColor = null,
      edgeColor = '#8890a0',
      edgeWidth = 1.2,
    } = styleOpts;

    // 更新背景
    const bg = BACKGROUNDS[background] || BACKGROUNDS.light;
    const base = bgColor || bg.css;
    if (base.includes('gradient')) {
      this._container.style.background = '';
      this._container.style.backgroundImage = base;
    } else {
      this._container.style.background = base;
      this._container.style.backgroundImage = bg.image || 'none';
    }
    if (bg.size) this._container.style.backgroundSize = bg.size;

    // 计算标签颜色
    const isLight = bgColor ? _isLightHex(bgColor) : _isLightHex(bg.css);
    const labelColor = isLight ? '#1a1d23' : '#ffffff';
    this._renderer.setSetting('labelColor', labelColor);

    // 更新边样式（**无最小值限制！**）
    this._renderer.setSetting('defaultEdgeColor', edgeColor);
    this._renderer.setSetting('defaultEdgeSize', edgeWidth);

    // 更新既有边（宽度 0 = 隐藏）
    this._graph.forEachEdge((edge) => {
      this._graph.setEdgeAttribute(edge, 'hidden', !showEdges || edgeWidth <= 0);
      this._graph.setEdgeAttribute(edge, 'color', edgeColor);
      this._graph.setEdgeAttribute(edge, 'size', edgeWidth);
    });

    // 更新 tooltip 样式
    if (this._tooltip) {
      this._tooltip.style.color = isLight ? '#1a1d23' : '#f0f2f5';
      this._tooltip.style.background = isLight ? 'rgba(255,255,255,.92)' : 'rgba(22,27,35,.92)';
      this._tooltip.style.borderColor = isLight ? '#c8cdd5' : '#3a4150';
    }

    this._renderer.refresh();
  }

  /** 高亮节点及其引用关系。 */
  highlight(center, citing = [], cited = []) {
    this._hl = {
      center,
      citing: new Set(citing.filter(id => this._graph.hasNode(id))),
      cited: new Set(cited.filter(id => this._graph.hasNode(id))),
    };
    this._applyHighlight();
  }

  /** 清除高亮。 */
  clearHighlight() {
    this._hl = null;
    this._restoreAttrs();
    this._renderer.refresh();
  }

  /** 聚焦到指定节点。 */
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

  /** 适配全图。 */
  fit() {
    this._renderer.getCamera().animatedReset({ duration: 320 });
  }

  /** 点击回调。 */
  onClick(cb) {
    this._clickCb = cb;
    return this;
  }

  /** 长按回调。 */
  onLongPress(cb) {
    this._longCb = cb;
    return this;
  }

  /** 获取节点原始数据。 */
  getNodeData(id) {
    return this._byId.get(id) || null;
  }

  /** 销毁渲染器。 */
  destroy() {
    this._clearLongTimer();
    if (this._tooltip && this._tooltip.parentNode) this._tooltip.remove();
    this._tooltip = null;
    try {
      this._renderer.kill();
    } catch (_) {}
    this._graph.clear();
    this._byId.clear();
  }

  // ── 内部方法 ───────────────────────────────────────

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

  _applyHighlight() {
    if (!this._hl) return;
    const { center, citing, cited } = this._hl;

    // 保存原始属性
    if (!this._origAttrs) {
      this._origAttrs = new Map();
      this._graph.forEachNode((id, attrs) => {
        this._origAttrs.set(id, {
          label: attrs.label || '',
          color: attrs.color || '#4f9cf9',
          size: attrs.size || 2,
        });
      });
    }

    // 应用高亮样式
    this._graph.forEachNode((id) => {
      const orig = this._origAttrs.get(id);
      let color, size;
      if (id === center) {
        color = '#ffffff';
        size = orig.size * 1.3;
      } else if (citing.has(id)) {
        color = HL_CITING;
        size = orig.size;
      } else if (cited.has(id)) {
        color = HL_CITED;
        size = orig.size;
      } else {
        color = DIM_NODE;
        size = orig.size * 0.5;
      }
      this._graph.setNodeAttribute(id, 'color', color);
      this._graph.setNodeAttribute(id, 'size', size);
    });

    // 高亮边
    this._graph.forEachEdge((edge, _attrs, s, t) => {
      let color, size, hidden;
      if (s === center) {
        color = HL_CITED;
        size = 2;
        hidden = false;
      } else if (t === center) {
        color = HL_CITING;
        size = 2;
        hidden = false;
      } else {
        color = DIM_EDGE;
        size = 0.4;
        hidden = true;
      }
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

  _wirePan() {
    const cam = () => this._renderer.getCamera();
    let moved = false;

    this._container.addEventListener('mousedown', (e) => {
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
    if (this._longTimer) {
      clearTimeout(this._longTimer);
      this._longTimer = null;
    }
  }
}
