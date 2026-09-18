/* graph/renderer3d.js — 3D 立体渲染器（3d-force-graph + three.js / WebGL）。
 *
 * 新架构原则：
 *   - 纯渲染层，不持有任何业务状态
 *   - setData() 接收已计算好所有属性的数据
 *   - setStyle() 只改渲染参数，不重建图
 *   - applyPositions() 只写坐标，不触发相机重置
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
  grid: 'linear-gradient(rgba(0,0,0,.06) 1px, transparent 1px), linear-gradient(90deg, rgba(0,0,0,.06) 1px, transparent 1px)',
  radial: 'radial-gradient(circle at 50% 45%, #ffffff 0%, #e8ecf2 50%, #c8d0dc 100%)',
  gradient: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
};

export class Renderer3D {
  constructor(container) {
    this._container = container;
    this._byId = new Map(); // id → 原始节点数据
    this._hl = null; // 高亮状态
    this._clickCb = null;
    this._longCb = null;
    this._hoverNode = null;
    this._longTimer = null;
    this._g = null;
    this._pendingData = null;
    this._fitted = false; // 首次数据载入后适配一次视角

    this._initWhenVisible();
  }

  /** 等待容器可见后初始化。 */
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

  /** 初始化 3D 图。 */
  _initGraph() {
    this._g = ForceGraph3D({ controlType: 'orbit' })(this._container)
      .backgroundColor('rgba(0,0,0,0)')
      .showNavInfo(false)
      .nodeId('id')
      .nodeColor((d) => this._nodeColor(d))
      .nodeVal((d) => Math.max(0.4, (d.size || 2)) ** 2.2)
      .nodeOpacity(0.92)
      .nodeLabel((d) => `<div style="font:12px system-ui;color:#ffffff">${this._esc(d.label || d.title || d.id)}</div>`)
      .nodeResolution(24)
      .linkColor((d) => d.color || '#8890a0') // 实时读取边颜色
      .linkOpacity(0.32)
      .linkWidth((d) => d.width != null ? d.width : 1.2) // **无最小值限制！**
      .linkDirectionalArrowLength((d) => (d.showArrow ? 2.2 : 0))
      .linkDirectionalArrowRelPos(1)
      .linkVisibility((d) => !d.hidden)
      .onNodeClick((node) => {
        this._clearLongTimer();
        if (this._clickCb) this._clickCb(node.id);
      })
      .onNodeHover((node) => {
        this._hoverNode = node ? node.id : null;
      });

    // 增强光照
    setTimeout(() => {
      if (this._g && this._g.scene && window.THREE) {
        const ambientLight = new window.THREE.AmbientLight(0xffffff, 0.6);
        this._g.scene.add(ambientLight);
        const dirLight = new window.THREE.DirectionalLight(0xffffff, 0.8);
        dirLight.position.set(200, 300, 400);
        this._g.scene.add(dirLight);
      }
    }, 100);

    // 禁用物理引擎
    const charge = this._g.d3Force('charge');
    if (charge) charge.strength(0);
    const link = this._g.d3Force('link');
    if (link) link.strength(0);
    this._g.d3AlphaDecay(1);

    this._wireLongPress();

    // 应用 pending 数据
    if (this._pendingData) {
      this.setData(this._pendingData);
      this._pendingData = null;
    }
  }

  _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  /** 设置数据（接收已计算好所有属性的格式）。 */
  setData(renderData) {
    if (!this._g) {
      this._pendingData = renderData;
      return;
    }

    this._byId.clear();
    const { nodes, edges } = renderData;

    const gnodes = nodes.map(n => {
      this._byId.set(n.id, n._raw || n);
      return {
        ...n,
        z: 0, // 初始z=0，后续由applyPositions设置
      };
    });

    const links = edges.map(e => ({
      source: e.source,
      target: e.target,
      color: e.color,
      width: e.width,
      hidden: e.hidden,
      showArrow: false, // 默认不显示箭头，可通过setStyle启用
    }));

    // 保存相机状态防止自动缩放
    const camPos = this._g.cameraPosition();
    const zoom = this._g.zoom();

    this._g.graphData({ nodes: gnodes, links });

    // 恢复相机位置
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

    // 新建渲染器首次拿到数据：适配一次视角（后续 setData/applyPositions 保持相机不动）
    if (!this._fitted && nodes.length) {
      this._fitted = true;
      setTimeout(() => {
        if (this._g) this._g.zoomToFit(400, 60);
      }, 120);
    }
  }

  /** 更新节点属性（用于动态样式变更）。 */
  updateNodeAttrs(nodes) {
    if (!this._g) return;
    for (const n of nodes) {
      this._byId.set(n.id, n._raw || n);
    }
    // 合并到当前图数据
    const cur = this._g.graphData();
    const map = new Map(nodes.map(n => [n.id, n]));
    for (const gn of cur.nodes) {
      const src = map.get(gn.id);
      if (src) {
        Object.assign(gn, {
          size: src.size,
          color: src.color,
          label: src.label,
          title: src.title,
        });
      }
    }
    this._g.nodeColor(this._g.nodeColor());
    this._g.nodeVal(this._g.nodeVal());
    this._g.nodeLabel(this._g.nodeLabel());
  }

  /** 应用坐标（只写x/y/z，不触发相机重置）。 */
  applyPositions(positions, ids) {
    if (!positions || !ids || !this._g) return;

    const cur = this._g.graphData();
    if (!cur || !cur.nodes) return;

    // 保存相机状态
    const camPos = this._g.cameraPosition();
    const zoom = this._g.zoom();

    const posMap = new Map();
    for (let i = 0; i < ids.length; i++) {
      posMap.set(ids[i], {
        x: positions[2 * i],
        y: positions[2 * i + 1],
      });
    }

    for (const node of cur.nodes) {
      const p = posMap.get(node.id);
      if (p) {
        node.x = p.x;
        node.y = p.y;
        node.z = 0; // 保持平面布局
      }
    }

    this._g.graphData(cur);

    // 恢复相机位置
    if (camPos && camPos.x != null) {
      this._g.cameraPosition(camPos, undefined, 0);
      this._g.zoom(zoom || 1, 0);
    }

    this._g.nodeColor(this._g.nodeColor());
    this._g.nodeOpacity(this._g.nodeOpacity());
  }

  /** 设置样式（只改渲染参数，不重建图）。 */
  setStyle(styleOpts) {
    if (!this._g) return;

    const {
      showEdges = true,
      background = 'light',
      bgColor = null,
      edgeColor = '#8890a0',
      edgeWidth = 1.2,
      showEdgeDir = false,
    } = styleOpts;

    // 更新背景
    const base = bgColor || BACKGROUNDS[background] || BACKGROUNDS.light;
    if (base.includes('gradient')) {
      this._container.style.background = '';
      this._container.style.backgroundImage = base;
    } else {
      this._container.style.background = base;
      this._container.style.backgroundImage = BG_IMAGES[background] || 'none';
    }
    this._container.style.backgroundSize = background === 'grid' ? '34px 34px' : '';

    // 更新边样式（可见性按每条 link.hidden 判定，宽度 0 = 隐藏）
    this._g.linkColor(edgeColor)
      .linkWidth(edgeWidth)
      .linkVisibility((d) => !d.hidden)
      .linkDirectionalArrowLength(showEdgeDir ? 2.2 : 0);

    // 更新既有边
    const cur = this._g.graphData();
    if (cur && cur.links) {
      for (const link of cur.links) {
        link.color = edgeColor;
        link.width = edgeWidth;
        link.hidden = !showEdges || edgeWidth <= 0;
        link.showArrow = showEdgeDir;
      }
      this._g.graphData(cur);
    }
  }

  /** 高亮节点及其引用关系。 */
  highlight(center, citing = [], cited = []) {
    if (!this._g) return;
    this._hl = {
      center,
      citing: new Set(citing),
      cited: new Set(cited),
    };
    this._g.nodeColor(this._g.nodeColor());
    this._g.nodeOpacity(this._g.nodeOpacity());
  }

  /** 清除高亮。 */
  clearHighlight() {
    if (!this._g) return;
    this._hl = null;
    this._g.nodeColor(this._g.nodeColor());
    this._g.nodeOpacity(this._g.nodeOpacity());
  }

  /** 聚焦到指定节点。 */
  focus(id) {
    if (!this._g) return false;
    const node = this._byId.get(id);
    if (!node) return false;
    this._g.centerAt(node.x || 0, node.y || 0, 500);
    this._g.zoom(5, 500);
    return true;
  }

  /** 适配全图。 */
  fit() {
    if (this._g) this._g.zoomToFit(500, 50);
  }

  /** 3D 视角控制。 */
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

  // ── 内部方法 ───────────────────────────────────────

  _nodeColor(d) {
    let color;
    if (!this._hl) {
      color = d.color || '#4f9cf9';
    } else {
      if (d.id === this._hl.center) {
        color = '#ffffff';
      } else if (this._hl.citing.has(d.id)) {
        color = HL_CITING;
      } else if (this._hl.cited.has(d.id)) {
        color = HL_CITED;
      } else {
        color = DIM_NODE;
      }
    }
    return color;
  }

  _wireLongPress() {
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
    this._container.addEventListener('mouseleave', () => {
      this._hoverNode = null;
      this._clearLongTimer();
    });
  }

  _clearLongTimer() {
    if (this._longTimer) {
      clearTimeout(this._longTimer);
      this._longTimer = null;
    }
  }

  /** 销毁渲染器。 */
  destroy() {
    this._clearLongTimer();
    try {
      this._g && this._g._destructor && this._g._destructor();
    } catch (_) {}
    this._container.innerHTML = '';
    this._byId.clear();
  }
}
