/* graph/renderer3d.js — 3D 立体渲染（3d-force-graph，three.js / WebGL）。
 *
 * 与 renderer2d.js 同构接口。位置由 FA2 worker 统一计算（main.js），
 *   applyPositions() 将 2D 坐标映射到 3D 的 x/y 平面（z=0）。
 * d3-force-3d 物理引擎已极弱化（strength≈0），避免覆盖 FA2 坐标。
 * 背景样式统一走容器 CSS + 透明场景，与 2D 表现一致。
 *
 * 深度视觉线索（方案A）：
 *   - nodeOpacity: 近实远虚（根据z深度调整透明度）
 *   - nodeColor brightness: 近亮远暗（根据z深度调整亮度）
 *   - 透视投影：近大远小（3d-force-graph内置）
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
    this._zRange = { min: 0, max: 0 };
    this._spacingFactor = 1.0;
    this._basePositions = null;
    this._g = null;
    this._pendingData = null;

    this._initWhenVisible();
  }

  _initWhenVisible() {
    const tryInit = () => {
      const w = this._container.offsetWidth;
      const h = this._container.offsetHeight;
      if (w > 0 && h > 0) {
        this._initGraph();
      } else {
        requestAnimationFrame(tryInit);
      }
    };
    tryInit();
  }

  _initGraph() {
    const initialTipColor = this._computeTipColor(this._style.background, this._style.bgColor);
    this._g = ForceGraph3D({ controlType: 'orbit' })(this._container)
      .backgroundColor('rgba(0,0,0,0)')
      .showNavInfo(false)
      .nodeId('id')
      .nodeColor((d) => this._nodeColor(d))
      .nodeVal((d) => Math.max(0.4, (d.size || 2)) ** 2.2)
      .nodeOpacity(0.92)
      .nodeLabel((d) => `<div style="font:12px system-ui;color:${initialTipColor}">${this._esc(d.label || d.title || d.id)}</div>`)
      .nodeResolution(24)
      .linkColor((d) => {
        // 实时读取当前样式，支持动态更新
        return this._style.edgeColor || '#8890a0';
      })
      .linkOpacity(0.32)
      .linkWidth((d) => {
        // 实时读取当前样式，无最小值限制
        return this._style.edgeWidth != null ? this._style.edgeWidth : 1.2;
      })
      .linkDirectionalArrowLength(() => (this._style.showEdgeDir ? 2.2 : 0))
      .linkDirectionalArrowRelPos(1)
      .linkVisibility(() => this._style.showEdges)
      .onNodeClick((node) => { this._clearLongTimer(); if (this._clickCb) this._clickCb(node.id); })
      .onNodeHover((node) => { this._hoverNode = node ? node.id : null; });

    // 增强 3D 光照效果：通过 scene 对象添加环境光和方向光
    setTimeout(() => {
      if (this._g && this._g.scene && window.THREE) {
        const ambientLight = new window.THREE.AmbientLight(0xffffff, 0.6);
        this._g.scene.add(ambientLight);
        const dirLight = new window.THREE.DirectionalLight(0xffffff, 0.8);
        dirLight.position.set(200, 300, 400);
        this._g.scene.add(dirLight);
      }
    }, 100);

    // 完全禁用 d3-force-3d 物理引擎，坐标完全由 FA2/几何布局决定
    const charge = this._g.d3Force('charge');
    if (charge) charge.strength(0);
    const link = this._g.d3Force('link');
    if (link) {
      link.strength(0);
    }
    this._g.d3AlphaDecay(1);  // 立即停止物理模拟
    this._wireLongPress();

    if (this._pendingData) {
      this._g.graphData(this._pendingData);
      this._pendingData = null;
    }
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
    if (!this._g) {
      this._pendingData = { nodes: gnodes, links };
      return;
    }
    // 保存当前相机状态，防止graphData()触发自动缩放
    const camPos = this._g.cameraPosition();
    const zoom = this._g.zoom();
    this._g.graphData({ nodes: gnodes, links });
    // 恢复相机位置（如果之前已有设置）
    if (camPos && camPos.x != null) {
      this._g.cameraPosition(camPos, undefined, 0);
      this._g.zoom(zoom || 1, 0);
    }
    requestAnimationFrame(() => {
      if (this._g) {
        this._g.width(this._container.offsetWidth);
        this._g.height(this._container.offsetHeight);
      }
    });
  }

  updateNodeAttrs(nodes) {
    if (!this._g) return;
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

  /** 回写 FA2 坐标：2D 的 x/y 直接映射到 3D 的 x/y（z=0，保持平面布局）。 */
  applyPositions(positions, ids) {
    if (!positions || !ids || !this._g) return;
    const cur = this._g.graphData();
    if (!cur || !cur.nodes) return;
    // 保存当前相机状态
    const camPos = this._g.cameraPosition();
    const zoom = this._g.zoom();
    
    const posMap = new Map();
    for (let i = 0; i < ids.length; i++) {
      posMap.set(ids[i], { x: positions[2 * i], y: positions[2 * i + 1] });
    }
    for (const node of cur.nodes) {
      const p = posMap.get(node.id);
      if (p) {
        node.x = p.x;
        node.y = p.y;
        node.z = 0;
      }
    }
    this._g.graphData(cur);
    // 恢复相机位置，防止自动缩放
    if (camPos && camPos.x != null) {
      this._g.cameraPosition(camPos, undefined, 0);
      this._g.zoom(zoom || 1, 0);
    }
    this._saveBasePositions();
    this._updateZRange();
    this._g.nodeColor(this._g.nodeColor());
    this._g.nodeOpacity(this._g.nodeOpacity());
  }

  setStyle(style = {}) {
    if (!this._g) {
      Object.assign(this._style, style);
      return;
    }
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
      .linkWidth(this._style.edgeWidth || 1.2)
      .linkVisibility(!!this._style.showEdges)
      .linkDirectionalArrowLength(this._style.showEdgeDir ? 2.2 : 0);
  }

  _nodeColor(d) {
    let color;
    if (!this._hl) {
      color = d.color || '#4f9cf9';
    } else {
      if (d.id === this._hl.center) color = '#ffffff';
      else if (this._hl.citing.has(d.id)) color = HL_CITING;
      else if (this._hl.cited.has(d.id)) color = HL_CITED;
      else color = DIM_NODE;
    }
    return this._adjustBrightness(color, d.z);
  }

  _nodeOpacityForNode(d) {
    const base = 0.92;
    const zNorm = this._normalizeZ(d.z);
    return 0.4 + zNorm * (base - 0.4);
  }

  _normalizeZ(z) {
    const { min, max } = this._zRange;
    if (max === min) return 0.5;
    return (z - min) / (max - min);
  }

  _updateZRange() {
    if (!this._g) return;
    const cur = this._g.graphData();
    if (!cur || !cur.nodes || !cur.nodes.length) {
      this._zRange = { min: 0, max: 0 };
      return;
    }
    let min = Infinity, max = -Infinity;
    for (const n of cur.nodes) {
      const z = n.z || 0;
      if (z < min) min = z;
      if (z > max) max = z;
    }
    this._zRange = { min, max };
  }

  _adjustBrightness(hex, z) {
    const zNorm = this._normalizeZ(z);
    const factor = 0.5 + zNorm * 0.5;
    const c = hex.replace('#', '');
    if (c.length < 6) return hex;
    const r = Math.round(parseInt(c.substring(0, 2), 16) * factor);
    const g = Math.round(parseInt(c.substring(2, 4), 16) * factor);
    const b = Math.round(parseInt(c.substring(4, 6), 16) * factor);
    return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${b.toString(16).padStart(2,'0')}`;
  }

  setSpacing(factor) {
    if (!this._basePositions || !this._g) return;
    this._spacingFactor = factor;
    const cur = this._g.graphData();
    if (!cur || !cur.nodes) return;
    for (const node of cur.nodes) {
      const base = this._basePositions.get(node.id);
      if (base) {
        node.x = base.x * factor;
        node.y = base.y * factor;
        node.z = base.z * factor;
      }
    }
    this._updateZRange();
    this._g.graphData(cur);
    this._g.nodeColor(this._g.nodeColor());
    this._g.nodeOpacity(this._g.nodeOpacity());
  }

  _saveBasePositions() {
    const cur = this._g.graphData();
    if (!cur || !cur.nodes) return;
    this._basePositions = new Map();
    for (const node of cur.nodes) {
      this._basePositions.set(node.id, { x: node.x || 0, y: node.y || 0, z: node.z || 0 });
    }
  }

  viewFront() {
    if (this._g) this._g.cameraPosition({ x: 0, y: 0, z: 800 }, { x: 0, y: 0, z: 0 }, 1000);
  }

  viewTop() {
    if (this._g) this._g.cameraPosition({ x: 0, y: 800, z: 0 }, { x: 0, y: 0, z: 0 }, 1000);
  }

  viewSide() {
    if (this._g) this._g.cameraPosition({ x: 800, y: 0, z: 0 }, { x: 0, y: 0, z: 0 }, 1000);
  }

  viewIsometric() {
    if (this._g) {
      const d = 600;
      this._g.cameraPosition({ x: d, y: d, z: d }, { x: 0, y: 0, z: 0 }, 1000);
    }
  }

  viewReset() {
    if (this._g) this._g.zoomToFit(1000, 80);
  }

  highlight(center, citing = [], cited = []) {
    if (!this._g) return;
    this._hl = { center, citing: new Set(citing), cited: new Set(cited) };
    this._g.nodeColor(this._g.nodeColor());
    this._g.nodeOpacity(this._g.nodeOpacity());
  }

  clearHighlight() {
    if (!this._g) return;
    this._hl = null;
    this._g.nodeColor(this._g.nodeColor());
    this._g.nodeOpacity(this._g.nodeOpacity());
  }

  focus(id) {
    if (!this._g) return false;
    const node = this._byId.get(id);
    if (!node) return false;
    this._g.centerAt(node.x || 0, node.y || 0, 500);
    this._g.zoom(5, 500);
    return true;
  }

  fit() { if (this._g) this._g.zoomToFit(500, 50); }

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
    try { this._g && this._g._destructor && this._g._destructor(); } catch (_) {}
    this._container.innerHTML = '';
    this._byId.clear();
  }
}
