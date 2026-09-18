/* graph/layoutEngine.js — 独立坐标计算引擎（无渲染逻辑）。
 *
 * 职责：
 *   - 根据算法计算节点坐标（FA2/circular/grid/random）
 *   - 缓存/恢复坐标（避免重复计算）
 *   - 返回 Float32Array(2N) 格式：[x0, y0, x1, y1, ...]
 */

import { ALGORITHMS } from './layoutAlgorithms.js';

const CACHE_KEY = 'graph_layout_positions_v2';

export class LayoutEngine {
  constructor(dataModel) {
    this.data = dataModel;
    this.positions = null; // Float32Array
    this.algorithm = 'fa2';
    this.opts = {};
  }

  /** 计算布局，返回 Float32Array。 */
  compute(algorithm, opts = {}) {
    this.algorithm = algorithm;
    this.opts = opts;

    const ids = this.data.ids;
    const edges = this.data.edges.map(e => ({ source: e.source, target: e.target }));
    const clusters = this.data.clusters;

    let positions;
    switch (algorithm) {
      case 'fa2':
        positions = this._computeFA2(ids, edges, clusters, opts);
        break;
      case 'circular':
        positions = ALGORITHMS.circular(ids, edges, opts.spacing || 1);
        break;
      case 'grid':
        positions = ALGORITHMS.grid(ids, edges, opts.spacing || 1);
        break;
      case 'random':
        positions = ALGORITHMS.random(ids, edges, opts.spacing || 1);
        break;
      default:
        throw new Error(`Unknown layout algorithm: ${algorithm}`);
    }

    this.positions = positions;
    return positions;
  }

  /** FA2 通过 Web Worker 异步计算（返回 Promise）。 */
  _computeFA2(ids, edges, clusters, opts) {
    // 同步版本：使用预计算的缓存或占位符
    // 实际FA2计算由 main.js 中的 worker 处理，这里只返回占位符
    // 真实场景中，应该返回一个 Promise 或在 compute() 中处理异步
    const cached = this.restore();
    if (cached && cached.length === ids.length * 2) {
      return cached;
    }

    // 无缓存时返回初始随机位置（worker 会后续更新）
    const pos = new Float32Array(ids.length * 2);
    for (let i = 0; i < ids.length; i++) {
      pos[2 * i] = (Math.random() - 0.5) * 100;
      pos[2 * i + 1] = (Math.random() - 0.5) * 100;
    }
    this.positions = pos;
    return pos;
  }

  /** 保存坐标到 sessionStorage。 */
  save(positions = this.positions) {
    if (!positions) return;
    try {
      const data = {
        algorithm: this.algorithm,
        ids: this.data.ids,
        positions: Array.from(positions),
        timestamp: Date.now(),
      };
      sessionStorage.setItem(CACHE_KEY, JSON.stringify(data));
    } catch (e) {
      console.warn('[LayoutEngine] Failed to save positions:', e);
    }
  }

  /** 从 sessionStorage 恢复坐标。 */
  restore() {
    try {
      const raw = sessionStorage.getItem(CACHE_KEY);
      if (!raw) return null;

      const data = JSON.parse(raw);
      // 验证ID匹配
      if (data.ids && data.ids.length === this.data.ids.length) {
        const sameIds = data.ids.every((id, i) => id === this.data.ids[i]);
        if (sameIds) {
          this.algorithm = data.algorithm || 'fa2';
          return new Float32Array(data.positions);
        }
      }
    } catch (e) {
      console.warn('[LayoutEngine] Failed to restore positions:', e);
    }
    return null;
  }

  /** 清除缓存。 */
  clearCache() {
    try {
      sessionStorage.removeItem(CACHE_KEY);
    } catch (_) {}
  }

  /** 获取当前算法。 */
  get currentAlgorithm() {
    return this.algorithm;
  }

  /** 获取当前坐标。 */
  get currentPositions() {
    return this.positions;
  }
}
