/* graph/layoutAlgorithms.js — 纯前端几何布局算法（O(N)，支持10万+节点）。
 *
 * 每个函数签名：(ids, edges, spacing) → Float32Array [x0,y0, x1,y1, ...]
 * spacing：节点表面之间的最小距离（由防重叠滑块控制）。
 * FA2 走 worker，不走本模块。
 */

const BASE_SPACING = 8;

export function circularLayout(ids, _edges, spacing) {
  const n = ids.length;
  const pos = new Float32Array(n * 2);
  const s = BASE_SPACING * (spacing || 1);
  const r = Math.sqrt(n) * s * 0.5;
  for (let i = 0; i < n; i++) {
    const angle = (2 * Math.PI * i) / n;
    pos[2 * i] = r * Math.cos(angle);
    pos[2 * i + 1] = r * Math.sin(angle);
  }
  return pos;
}

export function gridLayout(ids, _edges, spacing) {
  const n = ids.length;
  const pos = new Float32Array(n * 2);
  const s = BASE_SPACING * (spacing || 1);
  const cols = Math.ceil(Math.sqrt(n));
  const halfW = (cols * s) / 2;
  const rows = Math.ceil(n / cols);
  const halfH = (rows * s) / 2;
  for (let i = 0; i < n; i++) {
    const col = i % cols;
    const row = Math.floor(i / cols);
    pos[2 * i] = col * s - halfW;
    pos[2 * i + 1] = row * s - halfH;
  }
  return pos;
}

export function randomLayout(ids, _edges, spacing) {
  const n = ids.length;
  const pos = new Float32Array(n * 2);
  const range = Math.sqrt(n) * BASE_SPACING * (spacing || 1) * 0.5;
  for (let i = 0; i < n; i++) {
    pos[2 * i] = (Math.random() - 0.5) * 2 * range;
    pos[2 * i + 1] = (Math.random() - 0.5) * 2 * range;
  }
  return pos;
}

export const ALGORITHMS = { circular: circularLayout, grid: gridLayout, random: randomLayout };
