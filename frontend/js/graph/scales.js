/* graph/scales.js — 视觉映射（前端按用户设置计算，后端只给原始属性）。
 *
 * 节点大小 ← 库内被引次数（library_citations）
 * 节点颜色 ← 影响因子（impact_factor），可切换配色方案
 * 节点标签 ← 年份 + 被引（可切换）
 */

// 配色方案：0→1 的 RGB 控制点（线性插值）。
const PALETTES = {
  viridis: [[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]],
  inferno: [[0, 0, 4], [87, 16, 110], [188, 55, 90], [249, 142, 9], [252, 255, 164]],
  cool: [[34, 224, 230], [120, 130, 220], [230, 34, 200]],
  grayscale: [[70, 74, 82], [228, 232, 240]],
};

function _lerp(a, b, t) { return a + (b - a) * t; }

function _ramp(stops, t) {
  t = Math.max(0, Math.min(1, t));
  const n = stops.length - 1;
  const i = Math.min(n - 1, Math.floor(t * n));
  const f = t * n - i;
  const c0 = stops[i], c1 = stops[i + 1];
  return [Math.round(_lerp(c0[0], c1[0], f)),
          Math.round(_lerp(c0[1], c1[1], f)),
          Math.round(_lerp(c0[2], c1[2], f))];
}

export function rgbToHex([r, g, b]) {
  return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('');
}

/** 影响因子 → 颜色。ifMax 为当前数据集 IF 上限（用于归一化，开方压缩长尾）。 */
export function colorForImpact(impact, ifMax, paletteName = 'viridis') {
  const stops = PALETTES[paletteName] || PALETTES.viridis;
  if (!impact || impact <= 0 || !ifMax || ifMax <= 0) {
    return rgbToHex(stops[0]);           // 无 IF → 配色最冷端
  }
  const t = Math.sqrt(impact / ifMax);   // sqrt 压缩：避免个别超高 IF 拉平其余
  return rgbToHex(_ramp(stops, t));
}

/** 配色方案的渐变 CSS（图例用）。 */
export function paletteGradient(paletteName = 'viridis') {
  const stops = PALETTES[paletteName] || PALETTES.viridis;
  const parts = stops.map((c, i) =>
    `${rgbToHex(c)} ${(i / (stops.length - 1) * 100).toFixed(0)}%`);
  return `linear-gradient(90deg, ${parts.join(', ')})`;
}

/** 库内被引 → 节点半径（pow(0.6) 缩放，base 为用户大小基准）。
 *  相比 sqrt，pow(0.6) 使高被引节点更大、低被引节点更小，视觉差异更明显。 */
export function sizeForCitations(libCitations, base = 1.0) {
  const c = Math.max(0, libCitations || 0);
  return (1.2 + Math.pow(c, 0.6) * 1.8) * base;
}

/** 节点标签。mode: 'year_cited' | 'title' | 'none'。 */
export function labelFor(node, mode = 'year_cited') {
  if (mode === 'none') return '';
  if (mode === 'title') {
    const t = node.title || node.id;
    return t.length > 28 ? t.slice(0, 27) + '…' : t;
  }
  // year_cited：年份 + 库内被引
  const y = node.year || '—';
  return `${y} · 引${node.library_citations || 0}`;
}

/** 把原始节点数组加工出 size/color/label（就地补充视觉字段）。 */
export function decorateNodes(nodes, opts = {}) {
  const { ifMax = 10, palette = 'viridis', sizeBase = 1.0, labelMode = 'year_cited' } = opts;
  for (const n of nodes) {
    n.size = sizeForCitations(n.library_citations, sizeBase);
    n.color = colorForImpact(n.impact_factor, ifMax, palette);
    n.label = labelFor(n, labelMode);
  }
  return nodes;
}

/** 从节点集推断 IF 上限（用于颜色归一化）。 */
export function inferIfMax(nodes) {
  let m = 0;
  for (const n of nodes) if ((n.impact_factor || 0) > m) m = n.impact_factor;
  return m || 10;
}

/** 长按高亮配色（渲染器与图例共用）。 */
export const HL_CITING = '#f97316';
export const HL_CITED = '#22d3ee';
