/* graph/rendererThree.js — 单场景渲染器（three.js r128，替代 sigma + 3d-force-graph 双库）。
 *
 * 设计原则（2026-09-18 单场景重构）：
 *   - 只有一个 THREE.Scene、一套节点/边对象。"2D/3D" 是**显示样式**，不是两个引擎：
 *       2D = 正交相机（顶视）+ 无光照材质（平面圆片观感）+ 屏幕空间标签覆盖层
 *       3D = 透视相机（可旋转）+ 光照材质（球体观感）
 *     切换只换相机/材质/控制器/标签开关，**不搬数据、不重建场景、不复位视角**。
 *   - 统一单位制：节点半径与边宽都是世界单位（= 渲染属性 × this._k），
 *     两个模式下同一滑块值观感一致（根除双库单位制分歧）。
 *   - 布局坐标由外部（FA2 worker / 几何算法）经 applyPositions 写入，本类不算布局。
 *
 * 对外接口（main.js 消费）：
 *   setData(renderData) / setStyle(opts) / applyPositions(pos, ids) /
 *   updateNodeAttrs(nodes) / setDisplayMode(mode) / highlight / clearHighlight /
 *   focus(id) / fit() / onClick(cb) / onLongPress(cb) / destroy()
 */

import { HL_CITING, HL_CITED } from './scales.js';

const DIM_NODE = '#333a45';
const DIM_EDGE = '#1c222b';

// ── 交互手感常量（2026-09-21 用户反馈：左键选择太难点、平移太容易误触、箭头太大）──
const HOVER_COLOR = '#ffc53d';   // 磁力吸附到的节点高亮色（与 2D/3D 材质都能看清）
const HOVER_SCALE = 1.7;         // 悬停节点放大倍数（吸附感）
const PICK_EXTRA_PX = 12;        // 选择宽容度：离节点边缘这么多像素内都算命中（磁力吸附取最近者）
const PAN_GUARD_PX = 22;         // 2D：按下点离最近节点边缘这么近 ⇒ 不立刻平移（先当作选节点）
const PAN_ESCAPE_PX = 18;        // 但持续拖动超过这个距离仍转为平移（避免"按在节点上就拖不动"）
const CLICK_MOVE_PX = 4;         // 位移小于它才算点击（原 2px 太灵敏，手一抖就选不中）
const ARROW_LEN_K = 2.6;         // 箭头尺寸（× 世界单位系数 _k；原 6 ⇒ 大箭头在大图里很乱）
const ARROW_MIN_W = 1.6;         // 箭头相对边宽的下限（× 边宽；原 3）
const ARROW_POS = 0.86;          // 箭头沿边的位置（0=起点，1=终点）

const BACKGROUNDS = {
  dark: { css: '#0e1116' },
  light: { css: '#f4f6fa' },
  grid: {
    css: '#f4f6fa',
    image: 'linear-gradient(rgba(0,0,0,.06) 1px, transparent 1px), linear-gradient(90deg, rgba(0,0,0,.06) 1px, transparent 1px)',
    size: '34px 34px',
  },
  radial: { css: '#f4f6fa', image: 'radial-gradient(circle at 50% 45%, #ffffff 0%, #e8ecf2 50%, #c8d0dc 100%)' },
  gradient: { css: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)' },
};

function _isLightHex(hex) {
  const c = String(hex || '').replace('#', '');
  if (c.length < 6) return true;
  const r = parseInt(c.substring(0, 2), 16);
  const g = parseInt(c.substring(2, 4), 16);
  const b = parseInt(c.substring(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5;
}

const _UP = () => new THREE.Vector3(0, 1, 0);

export class RendererThree {
  constructor(container) {
    this._container = container;
    this._byId = new Map();
    this._nodes = [];       // renderData.nodes（含 x/y/size/color/label）
    this._edges = [];       // renderData.edges
    this._style = { showEdges: true, showEdgeDir: false, edgeColor: '#8890a0', edgeWidth: 0.3, background: 'light', bgColor: null };
    this._mode = '2d';
    this._hl = null;
    this._clickCb = null;
    this._longCb = null;
    this._k = 1;            // 渲染属性 → 世界单位 换算系数（setData 按 bbox 标定）
    this._userMoved = false;  // 用户是否已接管视角（接管后停止自动适配）
    this._rafId = null;
    this._hoverId = null;
    this._longTimer = null;
    this._drag = null;
    this._idxById = new Map();   // nodeId → 实例下标（悬停只改 1~2 个实例，不全量重写）
    this._projPx = null;         // 屏幕投影缓存（拾取 O(N) 逐帧太贵）
    this._projSig = '';
    this._posRev = 0;            // 位置/尺寸版本号：投影缓存的作废依据之一

    this._initScene();
    this._initDom();
    this._wireEvents();
    this._loop();
  }

  // ── 初始化 ─────────────────────────────────────────

  _initScene() {
    this._scene = new THREE.Scene();

    this._camPersp = new THREE.PerspectiveCamera(60, 1, 0.1, 200000);
    this._camOrtho = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 200000);
    this._camera = this._camOrtho;

    // 视角状态（两模式共享 target；缩放用各自参数，切换时换算保持观感）
    this._target = new THREE.Vector3(0, 0, 0);
    this._orthoHalf = 500;              // 正交半视高（世界单位）
    this._sph = { radius: 1500, theta: 0, phi: Math.PI / 2 };  // 3D 球坐标

    this._matBasic = new THREE.MeshBasicMaterial({});       // 2D：无光照平面观感
    this._matLambert = new THREE.MeshLambertMaterial({});   // 3D：光照球体
    this._matEdge = new THREE.MeshBasicMaterial({});        // 边：自发光色（不受光照影响，颜色忠实设置值）

    this._gl = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this._gl.setClearColor(0x000000, 0);

    this._scene.add(new THREE.AmbientLight(0xffffff, 0.75));
    const dir = new THREE.DirectionalLight(0xffffff, 0.55);
    dir.position.set(200, 300, 400);
    this._scene.add(dir);

    this._nodesMesh = null;
    this._edgesMesh = null;
    this._arrowMesh = null;
  }

  _initDom() {
    this._container.innerHTML = '';
    this._container.style.cursor = 'grab';   // 定位由 CSS（.lg-canvas absolute inset:0）负责，勿覆盖

    const canvas = this._gl.domElement;
    canvas.style.display = 'block';
    this._container.appendChild(canvas);

    this._labelCanvas = document.createElement('canvas');
    this._labelCanvas.style.cssText = 'position:absolute;inset:0;pointer-events:none';
    this._container.appendChild(this._labelCanvas);

    this._tooltip = document.createElement('div');
    this._tooltip.style.cssText = 'position:fixed;z-index:9999;pointer-events:none;padding:6px 10px;' +
      'border-radius:6px;font:12px system-ui,sans-serif;max-width:320px;line-height:1.4;' +
      'word-break:break-word;display:none;border:1px solid #c8cdd5;box-shadow:0 2px 8px rgba(0,0,0,.12);' +
      'color:#1a1d23;background:rgba(255,255,255,.92);';
    document.body.appendChild(this._tooltip);

    this._resize();
    if (window.ResizeObserver) {
      this._ro = new ResizeObserver(() => this._resize());
      this._ro.observe(this._container);
    }
  }

  _resize() {
    const w = this._container.offsetWidth || 800;
    const h = this._container.offsetHeight || 600;
    this._gl.setSize(w, h);
    this._labelCanvas.width = w;
    this._labelCanvas.height = h;
    this._camPersp.aspect = w / h;
    this._camPersp.updateProjectionMatrix();
    const aspect = w / h;
    this._camOrtho.left = -this._orthoHalf * aspect;
    this._camOrtho.right = this._orthoHalf * aspect;
    this._camOrtho.top = this._orthoHalf;
    this._camOrtho.bottom = -this._orthoHalf;
    this._camOrtho.updateProjectionMatrix();
  }

  // ── 相机控制（自写：2D 平移+缩放 / 3D 旋转+平移+推拉）──

  _wireEvents() {
    const el = this._gl.domElement;

    el.addEventListener('mousedown', (e) => {
      this._clearLongTimer();
      // 磁力吸附拾取：取宽容圈内**最近**的节点（小节点也点得中）
      const hit = this._nearestNode(e.clientX, e.clientY);
      // 平移门限用**更大**的保护圈（22px）：只有离任何节点都够远，左键才立刻平移
      const nearNode = hit || this._nearestNode(e.clientX, e.clientY, PAN_GUARD_PX);
      const forcePan = e.button === 1 || (e.button === 0 && e.shiftKey);
      let mode = null;
      if (forcePan) {
        mode = 'pan';
      } else if (e.button === 0) {
        if (this._mode === '2d') {
          // 2D：按下点贴着节点 ⇒ 先按"选节点"处理（平移变难触发）；空白处才是平移
          mode = nearNode ? 'node' : 'pan';
        } else {
          mode = 'rotate';
        }
      }
      this._drag = {
        mode, x: e.clientX, y: e.clientY, startX: e.clientX, startY: e.clientY,
        moved: false, button: e.button, picked: hit ? hit.id : null,
      };
      if (mode) el.style.cursor = (mode === 'pan' || !hit) ? 'grabbing' : 'pointer';
      if (this._hoverId && this._longCb) {
        const target = this._hoverId;
        this._longTimer = setTimeout(() => { this._longTimer = null; if (this._longCb) this._longCb(target); }, 450);
      }
    });

    window.addEventListener('mousemove', (e) => {
      this._lastMouse = { x: e.clientX, y: e.clientY };
      if (this._drag && this._drag.mode) {
        const dx = e.clientX - this._drag.x;
        const dy = e.clientY - this._drag.y;
        const total = Math.hypot(e.clientX - this._drag.startX, e.clientY - this._drag.startY);
        if (total > CLICK_MOVE_PX) this._drag.moved = true;
        this._drag.x = e.clientX; this._drag.y = e.clientY;
        if (this._drag.mode === 'pan') {
          this._pan(dx, dy);
        } else if (this._drag.mode === 'node') {
          // 保护圈内按住不放继续拖 ⇒ 仍给平移（否则用户会觉得"拖不动"）
          if (total > PAN_ESCAPE_PX) this._drag.mode = 'pan';
        } else {
          this._rotate(dx, dy);
        }
      } else if (this._hoverId && this._tooltip.style.display === 'block') {
        this._tooltip.style.left = (e.clientX + 14) + 'px';
        this._tooltip.style.top = (e.clientY + 14) + 'px';
      }
    });

    window.addEventListener('mouseup', (e) => {
      if (this._drag && !this._drag.moved && this._drag.button === 0) {
        // 如果 mousedown 时已点在节点上，直接触发点击（不依赖 _pick）
        if (this._drag.picked && this._clickCb) {
          this._clickCb(this._drag.picked);
        } else {
          const id = this._pick(e.clientX, e.clientY);
          if (id && this._clickCb) this._clickCb(id);
          if (!id) this._stageClick();
        }
      }
      this._drag = null;
      this._clearLongTimer();
      el.style.cursor = 'grab';
    });

    el.addEventListener('mousemove', (e) => {
      if (this._drag) return;
      const id = this._pick(e.clientX, e.clientY);
      if (id !== this._hoverId) {
        const prev = this._hoverId;
        this._hoverId = id;
        this._applyHoverVisual(prev);
        this._applyHoverVisual(id);
        if (id) {
          const d = this._byId.get(id);
          this._tooltip.textContent = d ? (d.title || d.id) : id;
          this._tooltip.style.display = 'block';
          el.style.cursor = 'pointer';
        } else {
          this._tooltip.style.display = 'none';
          el.style.cursor = 'grab';
        }
      }
    });

    el.addEventListener('mouseleave', () => {
      this._applyHoverVisual(this._hoverId);
      this._hoverId = null;
      this._tooltip.style.display = 'none';
      this._clearLongTimer();
    });

    el.addEventListener('wheel', (e) => {
      e.preventDefault();
      this._userMoved = true;
      const f = Math.pow(1.0015, -e.deltaY);
      if (this._mode === '2d') {
        this._orthoHalf = Math.min(200000, Math.max(5, this._orthoHalf / f));
      } else {
        this._sph.radius = Math.min(200000, Math.max(5, this._sph.radius / f));
      }
    }, { passive: false });

    window.addEventListener('resize', () => this._resize());
  }

  _stageClick() {
    if (this._hl) this.clearHighlight();
  }

  /** 当前视图下 1 屏幕像素 = 多少世界单位（2D 正交 / 3D 透视统一口径）。 */
  _worldPerPx() {
    const h = this._container.offsetHeight || 600;
    return this._mode === '2d'
      ? (2 * this._orthoHalf) / h
      : (2 * this._sph.radius * Math.tan((this._camPersp.fov * Math.PI) / 360)) / h;
  }

  _pan(dx, dy) {
    this._userMoved = true;
    const worldPerPx = this._worldPerPx();
    const right = new THREE.Vector3();
    const up = new THREE.Vector3();
    this._camera.matrix.extractBasis(right, up, new THREE.Vector3());
    this._target.addScaledVector(right, -dx * worldPerPx);
    this._target.addScaledVector(up, dy * worldPerPx);
  }

  _rotate(dx, dy) {
    this._userMoved = true;
    this._sph.theta -= dx * 0.005;
    this._sph.phi = Math.min(Math.PI - 0.05, Math.max(0.05, this._sph.phi - dy * 0.005));
  }

  _updateCamera() {
    if (this._mode === '2d') {
      const aspect = (this._container.offsetWidth || 800) / (this._container.offsetHeight || 600);
      this._camOrtho.left = -this._orthoHalf * aspect;
      this._camOrtho.right = this._orthoHalf * aspect;
      this._camOrtho.top = this._orthoHalf;
      this._camOrtho.bottom = -this._orthoHalf;
      this._camOrtho.position.set(this._target.x, this._target.y, this._target.z + 5000);
      this._camOrtho.lookAt(this._target);
      this._camOrtho.updateProjectionMatrix();
      this._camera = this._camOrtho;
    } else {
      const { radius, theta, phi } = this._sph;
      this._camPersp.position.set(
        this._target.x + radius * Math.sin(phi) * Math.sin(theta),
        this._target.y + radius * Math.cos(phi),
        this._target.z + radius * Math.sin(phi) * Math.cos(theta),
      );
      this._camPersp.lookAt(this._target);
      this._camPersp.updateProjectionMatrix();
      this._camera = this._camPersp;
    }
  }

  // ── 数据 ───────────────────────────────────────────

  setData(renderData) {
    this._nodes = (renderData && renderData.nodes) || [];
    this._edges = (renderData && renderData.edges) || [];
    this._byId.clear();
    for (const n of this._nodes) this._byId.set(n.id, n._raw || n);

    this._userMoved = false;   // 新数据：恢复自动适配，直到用户接管视角
    this._computeScale();
    this._rebuildMeshes();
    this._writeNodeTransforms();
    this._writeEdgeTransforms();
    if (!this._fittedOnce && this._nodes.length) {
      this._fittedOnce = true;
      this.fit();
    }
  }

  /** 渲染属性 → 世界单位 换算：标定为"适配视图下 1 渲染单位 ≈ 1 像素"。 */
  _computeScale() {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const n of this._nodes) {
      const x = n.x || 0, y = n.y || 0;
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
    const span = Math.max(1, maxX - minX, maxY - minY);
    const viewPx = Math.min(this._container.offsetWidth || 800, this._container.offsetHeight || 600);
    this._k = span / viewPx;   // 1px（适配时）对应的世界单位
  }

  _rebuildMeshes() {
    if (this._nodesMesh) { this._scene.remove(this._nodesMesh); this._nodesMesh.dispose(); }
    if (this._edgesMesh) { this._scene.remove(this._edgesMesh); this._edgesMesh.dispose(); }
    if (this._arrowMesh) { this._scene.remove(this._arrowMesh); this._arrowMesh.dispose(); }

    const nCount = Math.max(1, this._nodes.length);
    const eCount = Math.max(1, this._edges.length);

    this._nodesMesh = new THREE.InstancedMesh(
      new THREE.SphereGeometry(1, 20, 14), this._matForMode(), nCount);
    this._nodesMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

    this._edgesMesh = new THREE.InstancedMesh(
      new THREE.CylinderGeometry(0.5, 0.5, 1, 6, 1, true), this._matEdge, eCount);
    this._edgesMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

    this._arrowMesh = new THREE.InstancedMesh(
      new THREE.ConeGeometry(1, 3, 8), this._matEdge, eCount);
    this._arrowMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

    this._scene.add(this._nodesMesh, this._edgesMesh, this._arrowMesh);
  }

  _matForMode() {
    return this._mode === '3d' ? this._matLambert : this._matBasic;
  }

  applyPositions(positions, ids) {
    if (!positions || !ids) return;
    const map = new Map();
    for (let i = 0; i < ids.length; i++) map.set(ids[i], [positions[2 * i], positions[2 * i + 1]]);
    for (const n of this._nodes) {
      const p = map.get(n.id);
      if (p) { n.x = p[0]; n.y = p[1]; }
    }
    // 坐标到达后重标单位换算（setData 时 bbox 可能还是退化的），并在用户接管视角前自动适配
    this._computeScale();
    this._writeNodeTransforms();
    this._writeEdgeTransforms();
    if (!this._userMoved) this.fit();
  }

  updateNodeAttrs(nodes) {
    const map = new Map(nodes.map(n => [n.id, n]));
    for (const n of this._nodes) {
      const src = map.get(n.id);
      if (!src) continue;
      n.size = src.size; n.color = src.color; n.label = src.label; n.title = src.title;
      const raw = this._byId.get(n.id);
      if (raw && src._raw) this._byId.set(n.id, src._raw);
    }
    this._writeNodeTransforms();
  }

  // ── 实例矩阵写入 ───────────────────────────────────

  _writeNodeTransforms() {
    if (!this._nodesMesh) return;
    this._posRev++;                 // 位置/尺寸变了 → 作废屏幕投影缓存
    const m = new THREE.Matrix4();
    const c = new THREE.Color();
    this._idxById.clear();
    this._nodes.forEach((n, i) => {
      this._idxById.set(n.id, i);
      const r = Math.max(0.2, (n.size || 2)) * this._k;
      const hover = n.id === this._hoverId;
      const s = hover ? r * HOVER_SCALE : r;
      m.makeScale(s, s, s);
      m.setPosition(n.x || 0, n.y || 0, 0);
      this._nodesMesh.setMatrixAt(i, m);
      let color = n.color || '#4f9cf9';
      if (this._hl) color = this._hlNodeColor(n.id);
      if (hover) color = HOVER_COLOR;
      this._nodesMesh.setColorAt(i, c.set(color));
    });
    this._nodesMesh.instanceMatrix.needsUpdate = true;
    if (this._nodesMesh.instanceColor) this._nodesMesh.instanceColor.needsUpdate = true;
  }

  _writeEdgeTransforms() {
    if (!this._edgesMesh) return;
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const dirV = new THREE.Vector3();
    const up = _UP();
    const c = new THREE.Color();
    const posById = new Map(this._nodes.map(n => [n.id, n]));
    const st = this._style;
    const widthWorld = Math.max(0, st.edgeWidth) * this._k;

    this._edges.forEach((e, i) => {
      const s = posById.get(e.source);
      const t = posById.get(e.target);
      const hyp = s && t ? Math.hypot((t.x || 0) - (s.x || 0), (t.y || 0) - (s.y || 0)) : 0;
      // 退化边（端点重合）方向向量为零 → 四元数 NaN → GPU 画破面，必须隐藏
      const hidden = !st.showEdges || st.edgeWidth <= 0 || e.hidden || !s || !t || e.source === e.target || hyp < 1e-4;
      if (hidden) {
        m.makeScale(0, 0, 0);
        this._edgesMesh.setMatrixAt(i, m);
        this._arrowMesh.setMatrixAt(i, m);
        this._edgesMesh.setColorAt(i, c.set('#000000'));
        this._arrowMesh.setColorAt(i, c.set('#000000'));
        return;
      }
      const sx = s.x || 0, sy = s.y || 0, tx = t.x || 0, ty = t.y || 0;
      const len = Math.max(0.001, Math.hypot(tx - sx, ty - sy));
      dirV.set((tx - sx) / len, (ty - sy) / len, 0);
      q.setFromUnitVectors(up, dirV);
      m.compose(new THREE.Vector3((sx + tx) / 2, (sy + ty) / 2, 0), q, new THREE.Vector3(widthWorld, len, widthWorld));
      this._edgesMesh.setMatrixAt(i, m);

      // 边色唯一来源是样式面板（数据层的 per-edge color 会过期）
      let color = st.edgeColor;
      if (this._hl) color = this._hlEdgeColor(e, color);
      this._edgesMesh.setColorAt(i, c.set(color));

      // 箭头：指向 target 端（showEdgeDir 开启时）；尺寸刻意做小——大量节点下大头箭头会糊成一片
      if (st.showEdgeDir) {
        const ax = sx + (tx - sx) * ARROW_POS, ay = sy + (ty - sy) * ARROW_POS;
        const as = Math.max(widthWorld * ARROW_MIN_W, this._k * ARROW_LEN_K);
        m.compose(new THREE.Vector3(ax, ay, 0), q, new THREE.Vector3(as, as, as));
        this._arrowMesh.setMatrixAt(i, m);
        this._arrowMesh.setColorAt(i, c.set(color));
      } else {
        m.makeScale(0, 0, 0);
        this._arrowMesh.setMatrixAt(i, m);
      }
    });
    this._edgesMesh.instanceMatrix.needsUpdate = true;
    this._arrowMesh.instanceMatrix.needsUpdate = true;
    if (this._edgesMesh.instanceColor) this._edgesMesh.instanceColor.needsUpdate = true;
    if (this._arrowMesh.instanceColor) this._arrowMesh.instanceColor.needsUpdate = true;
  }

  // ── 样式 / 模式 ─────────────────────────────────────

  setStyle(styleOpts) {
    Object.assign(this._style, styleOpts || {});
    const st = this._style;

    const bg = BACKGROUNDS[st.background] || BACKGROUNDS.light;
    const base = st.bgColor || bg.css;
    if (base.includes('gradient')) {
      this._container.style.background = '';
      this._container.style.backgroundImage = base;
    } else {
      this._container.style.background = base;
      this._container.style.backgroundImage = bg.image || 'none';
    }
    this._container.style.backgroundSize = bg.size || '';

    this._labelColor = _isLightHex(st.bgColor || bg.css) ? '#1a1d23' : '#ffffff';
    this._writeEdgeTransforms();
  }

  /** 2D/3D = 显示样式：换相机/材质/标签开关；target 与缩放观感保持连续。 */
  setDisplayMode(mode) {
    if (mode === this._mode) return;
    if (mode === '3d') {
      // 正交半视高 → 透视距离：保持画面尺度观感连续
      this._sph.radius = this._orthoHalf / Math.tan((this._camPersp.fov * Math.PI) / 360);
      this._sph.theta = 0;
      this._sph.phi = Math.PI / 2;
    } else {
      // 透视距离 → 正交半视高；顶视归一（2D 约定），位置/缩放不复位
      this._orthoHalf = this._sph.radius * Math.tan((this._camPersp.fov * Math.PI) / 360);
    }
    this._mode = mode;
    if (this._nodesMesh) this._nodesMesh.material = this._matForMode();
  }

  // ── 高亮 ───────────────────────────────────────────

  highlight(center, citing = [], cited = []) {
    this._hl = { center, citing: new Set(citing), cited: new Set(cited) };
    this._writeNodeTransforms();
    this._writeEdgeTransforms();
  }

  clearHighlight() {
    if (!this._hl) return;
    this._hl = null;
    this._writeNodeTransforms();
    this._writeEdgeTransforms();
  }

  _hlNodeColor(id) {
    if (id === this._hl.center) return '#ffffff';
    if (this._hl.citing.has(id)) return HL_CITING;
    if (this._hl.cited.has(id)) return HL_CITED;
    return DIM_NODE;
  }

  _hlEdgeColor(e, fallback) {
    if (e.source === this._hl.center) return HL_CITED;
    if (e.target === this._hl.center) return HL_CITING;
    return DIM_EDGE;
  }

  // ── 拾取 / 视角 ────────────────────────────────────

  /** 视图签名：相机/画布/节点尺寸任一变化就作废投影缓存。 */
  _viewSig() {
    const t = this._target;
    return [this._container.offsetWidth, this._container.offsetHeight, t.x, t.y, t.z,
            this._orthoHalf, this._sph.radius, this._sph.theta, this._sph.phi,
            this._mode, this._k, this._posRev].join('|');
  }

  /** 全节点屏幕投影（含各自屏幕半径）——带缓存，仅在视图变化时重算。 */
  _projectNodes() {
    const sig = this._viewSig();
    if (sig === this._projSig && this._projPx) return this._projPx;
    const rect = this._gl.domElement.getBoundingClientRect();
    const w = rect.width || 1, h = rect.height || 1;
    const wpp = this._worldPerPx() || 1;
    const v = new THREE.Vector3();
    const cam = this._camera;
    cam.updateMatrixWorld();
    const out = [];
    for (const n of this._nodes) {
      v.set(n.x || 0, n.y || 0, 0).project(cam);
      if (v.z > 1) continue;                       // 相机背后（3D 旋转时会出现）
      const rPx = (Math.max(0.2, n.size || 2) * this._k) / wpp;
      out.push({
        id: n.id,
        px: rect.left + (v.x + 1) / 2 * w,
        py: rect.top + (1 - v.y) / 2 * h,
        rPx: Math.max(1.5, rPx),                   // 极小节点也给 1.5px，便于吸附
      });
    }
    this._projPx = out;
    this._projSig = sig;
    return out;
  }

  /**
   * 磁力吸附拾取：返回宽容圈（节点屏幕半径 + extraPx）内**最近**的节点。
   * 比射线精确命中宽容得多——小节点、密集区都点得中；`dist` 供平移门限判断。
   */
  _nearestNode(clientX, clientY, extraPx = PICK_EXTRA_PX) {
    if (!this._nodes.length || !this._nodesMesh) return null;
    let best = null;
    for (const p of this._projectNodes()) {
      const dist = Math.hypot(clientX - p.px, clientY - p.py);
      if (dist > p.rPx + extraPx) continue;
      if (!best || dist < best.dist) best = { id: p.id, dist, rPx: p.rPx };
    }
    return best;
  }

  _pick(clientX, clientY) {
    const hit = this._nearestNode(clientX, clientY);
    return hit ? hit.id : null;
  }

  /** 悬停反馈：只改这 1 个实例的矩阵/颜色（鼠标移动频繁，不能全量重写）。 */
  _applyHoverVisual(id) {
    if (!id || !this._nodesMesh) return;
    const i = this._idxById.get(id);
    if (i == null) return;
    const n = this._nodes[i];
    const r = Math.max(0.2, n.size || 2) * this._k;
    const on = id === this._hoverId;
    const s = on ? r * HOVER_SCALE : r;
    const m = new THREE.Matrix4().makeScale(s, s, s);
    m.setPosition(n.x || 0, n.y || 0, 0);
    this._nodesMesh.setMatrixAt(i, m);
    let color = n.color || '#4f9cf9';
    if (this._hl) color = this._hlNodeColor(id);
    if (on) color = HOVER_COLOR;
    this._nodesMesh.setColorAt(i, new THREE.Color(color));
    this._nodesMesh.instanceMatrix.needsUpdate = true;
    if (this._nodesMesh.instanceColor) this._nodesMesh.instanceColor.needsUpdate = true;
  }

  focus(id) {
    const n = this._nodes.find(x => x.id === id);
    if (!n) return false;
    this._target.set(n.x || 0, n.y || 0, 0);
    if (this._mode === '2d') this._orthoHalf = Math.max(5, this._orthoHalf * 0.35);
    else this._sph.radius = Math.max(5, this._sph.radius * 0.35);
    return true;
  }

  fit() {
    if (!this._nodes.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const n of this._nodes) {
      const x = n.x || 0, y = n.y || 0;
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
    this._target.set((minX + maxX) / 2, (minY + maxY) / 2, 0);
    const spanX = Math.max(1, maxX - minX);
    const spanY = Math.max(1, maxY - minY);
    const aspect = (this._container.offsetWidth || 800) / (this._container.offsetHeight || 600);
    this._orthoHalf = Math.max(spanY / 2, spanX / 2 / aspect) * 1.12;
    this._sph.radius = this._orthoHalf / Math.tan((this._camPersp.fov * Math.PI) / 360);
  }

  // ── 3D 预设视角（球坐标：theta=绕 Y 方位角，phi=从 +Y 起的极角）──
  //   position = target + R·(sinφ·sinθ, cosφ, sinφ·cosθ)
  //   图谱本身在 XY 平面（z=0），因此"前视"（相机在 +Z）= 平铺视图；"顶视"是边沿视角。

  _setSph(theta, phi) {
    this._sph.theta = theta;
    this._sph.phi = Math.min(Math.PI - 0.05, Math.max(0.05, phi));
    this._userMoved = true;   // 阻止自动 fit 覆盖用户选择的视角
  }

  /** 前视：相机在 +Z，正对图谱平面（XY）。 */
  viewFront() { this._setSph(0, Math.PI / 2); }

  /** 顶视：相机在 +Y，沿 -Y 俯视 XZ（z=0 时为边沿视角，可看到图谱厚度为 0）。 */
  viewTop() { this._setSph(0, 0.05); }

  /** 侧视：相机在 +X，沿 -X 看向 YZ。 */
  viewSide() { this._setSph(Math.PI / 2, Math.PI / 2); }

  /** 等轴测：相机在 (1,1,1)/√3 方向（经典 iso，三轴倾角相等）。 */
  viewIsometric() {
    const phi = Math.acos(1 / Math.sqrt(3));   // ≈ 54.7356°
    this._setSph(Math.PI / 4, phi);
  }

  /** 复位：等轴 + 适配视图。 */
  viewReset() {
    this._setSph(Math.PI / 4, Math.acos(1 / Math.sqrt(3)));
    this.fit();
    // fit() 内不设 userMoved，这里保持 true
    this._userMoved = true;
  }

  // ── 渲染循环 + 标签覆盖层 ──────────────────────────

  _loop() {
    const step = () => {
      this._rafId = requestAnimationFrame(step);
      this._updateCamera();
      this._gl.render(this._scene, this._camera);
      this._drawLabels();
    };
    this._rafId = requestAnimationFrame(step);
  }

  /** 2D 模式：屏幕空间标签（描边光晕），3D 模式不画（悬停 tooltip 代替）。 */
  _drawLabels() {
    const ctx = this._labelCanvas.getContext('2d');
    ctx.clearRect(0, 0, this._labelCanvas.width, this._labelCanvas.height);
    if (this._mode !== '2d' || !this._nodes.length) return;

    const w = this._labelCanvas.width, h = this._labelCanvas.height;
    const v = new THREE.Vector3();
    ctx.font = '10px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    const color = this._labelColor || '#1a1d23';
    const halo = color === '#ffffff' ? 'rgba(0,0,0,0.7)' : 'rgba(255,255,255,0.85)';
    let drawn = 0;
    for (const n of this._nodes) {
      if (!n.label || drawn > 500) continue;
      const screenR = ((n.size || 2) * this._k) / (2 * this._orthoHalf) * h;
      if (screenR < 4.5) continue;   // 太小不画标签（同 sigma labelRenderedSizeThreshold 语义）
      v.set(n.x || 0, n.y || 0, 0).project(this._camera);
      const px = (v.x * 0.5 + 0.5) * w;
      const py = (-v.y * 0.5 + 0.5) * h;
      if (px < -50 || px > w + 50 || py < -20 || py > h + 20) continue;
      ctx.save();
      ctx.shadowBlur = 6;
      ctx.shadowColor = halo;
      ctx.fillStyle = color;
      ctx.fillText(n.label, px + screenR + 3, py);
      ctx.fillText(n.label, px + screenR + 3, py);
      ctx.restore();
      drawn++;
    }
  }

  // ── 回调 / 生命周期 ────────────────────────────────

  onClick(cb) { this._clickCb = cb; return this; }
  onLongPress(cb) { this._longCb = cb; return this; }

  getNodeData(id) { return this._byId.get(id) || null; }

  _clearLongTimer() {
    if (this._longTimer) { clearTimeout(this._longTimer); this._longTimer = null; }
  }

  destroy() {
    this._clearLongTimer();
    if (this._rafId) cancelAnimationFrame(this._rafId);
    this._rafId = null;
    if (this._ro) this._ro.disconnect();
    if (this._tooltip && this._tooltip.parentNode) this._tooltip.remove();
    if (this._nodesMesh) this._nodesMesh.dispose();
    if (this._edgesMesh) this._edgesMesh.dispose();
    if (this._arrowMesh) this._arrowMesh.dispose();
    this._gl.dispose();
    this._container.innerHTML = '';
  }
}
