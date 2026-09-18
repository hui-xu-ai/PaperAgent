/* graph/layoutManager.js — 节点位置管理器（与渲染器解耦）。
 *
 * 职责：
 *   - 管理节点坐标（2D: ForceAtlas2, 3D: d3-force-3d）
 *   - 提供坐标查询/设置接口
 *   - 切换模式时保留/恢复坐标
 *
 * 与渲染器关系：
 *   - Renderer2D/Renderer3D 只负责视觉样式（size/color/label）
 *   - 位置由 LayoutManager 统一管理，通过 applyPositions() 同步到渲染器
 */

export class LayoutManager {
  constructor() {
    this._positions2d = null;  // Float32Array(2N)，顺序同 ids
    this._positions3d = null;  // Map<id, {x,y,z}>
    this._ids = [];
    this._mode = '2d';         // '2d' | '3d'
  }

  /** 初始化布局（首次加载或过滤变化时调用）。 */
  init(ids, edges, clusters) {
    this._ids = ids;
    this._positions2d = null;
    this._positions3d = null;
    // 2D 初始坐标由 worker 的 seedPositions 生成
    // 3D 初始坐标由 d3-force-3d 自动生成
  }

  /** 获取当前模式的坐标快照。 */
  capturePositions(mode) {
    if (mode === '2d') {
      return this._positions2d ? new Float32Array(this._positions2d) : null;
    } else {
      return this._positions3d ? new Map(this._positions3d) : null;
    }
  }

  /** 保存 2D 坐标（切换到 3D 前调用）。 */
  save2dPositions(positions) {
    this._positions2d = positions ? new Float32Array(positions) : null;
  }

  /** 保存 3D 坐标（切换到 2D 前调用）。 */
  save3dPositions(nodeMap) {
    if (!nodeMap) {
      this._positions3d = null;
      return;
    }
    this._positions3d = new Map();
    for (const [id, node] of nodeMap.entries()) {
      if (node.x !== undefined && node.y !== undefined) {
        this._positions3d.set(id, { x: node.x, y: node.y, z: node.z || 0 });
      }
    }
  }

  /** 恢复 2D 坐标（从 3D 切回 2D 时调用）。 */
  restore2dPositions() {
    return this._positions2d ? new Float32Array(this._positions2d) : null;
  }

  /** 恢复 3D 坐标（从 2D 切换到 3D 时调用，返回 Map<id, {x,y,z}>）。 */
  restore3dPositions() {
    if (!this._positions3d) return null;
    const map = new Map();
    for (const [id, pos] of this._positions3d.entries()) {
      map.set(id, { ...pos });
    }
    return map;
  }

  /** 数据变化时清除缓存（过滤/重载时调用）。 */
  clearCache() {
    this._positions2d = null;
    this._positions3d = null;
    this._ids = [];
  }

  get mode() { return this._mode; }
  set mode(m) { this._mode = m; }
}

// 单例导出
export const layoutManager = new LayoutManager();
