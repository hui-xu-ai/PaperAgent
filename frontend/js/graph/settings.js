/* graph/settings.js — 样式面板：颜色/大小/标签/边/背景 → 渲染选项。
 *
 * readStyle() 读 UI；按消费方拆成三组纯数据，main.js 分发：
 *   decorateOpts()   → scales.decorateNodes（大小/颜色/标签）
 *   rendererStyle()  → renderer.setStyle（背景/边显隐/边色）
 *   layoutSettings() → worker ForceAtlas2（scalingRatio/gravity）
 *
 * readLayout() 读布局面板：算法/FA2参数/间距。
 * readDisplayMode() 读样式面板的 2D/3D 切换。
 * 仅读 DOM，不持状态。
 */
const $ = (id) => document.getElementById(id);

const IDS = {
  colorscale: 'lg-s-colorscale',
  size: 'lg-s-size', sizeVal: 'lg-s-size-val',
  scaleAlgo: 'lg-s-scale-algo',
  label: 'lg-s-label',
  edges: 'lg-s-edges', edgeDir: 'lg-s-edgelabel',
  bg: 'lg-s-bg', bgColor: 'lg-s-bgcolor', edgeColor: 'lg-s-edgecolor',
  edgeWidth: 'lg-s-edgewidth', edgeWidthVal: 'lg-s-edgewidth-val',
  scaling: 'lg-s-scaling', scalingVal: 'lg-s-scaling-val',
  gravity: 'lg-s-gravity', gravityVal: 'lg-s-gravity-val',
  algorithm: 'lg-l-algorithm',
  spacing: 'lg-l-spacing', spacingVal: 'lg-l-spacing-val',
  displayMode: 'lg-display-mode',
};

export function readStyle() {
  const ew = Number($(IDS.edgeWidth).value);
  return {
    palette: $(IDS.colorscale).value || 'viridis',
    sizeBase: Number($(IDS.size).value) || 1,
    scaleAlgo: $(IDS.scaleAlgo).value || 'pow06',
    labelMode: $(IDS.label).value || 'year_cited',
    showEdges: $(IDS.edges).checked,
    showEdgeDir: $(IDS.edgeDir).checked,
    background: $(IDS.bg).value || 'light',
    bgColor: $(IDS.bgColor).value || null,
    edgeColor: $(IDS.edgeColor).value || '#8890a0',
    edgeWidth: Number.isFinite(ew) ? ew : 1.2, // 允许 0（=隐藏边），不能用 || 兜底
  };
}

export function readLayout() {
  return {
    algorithm: $(IDS.algorithm).value || 'fa2',
    scaling: Number($(IDS.scaling).value) || 1,
    gravity: Number($(IDS.gravity).value) || 1,
    spacing: Number($(IDS.spacing).value) || 1,
  };
}

export function readDisplayMode() {
  const active = document.querySelector('#lg-display-mode .lg-seg-btn.active');
  return active ? active.dataset.mode : '2d';
}

export function decorateOpts(s) {
  return { palette: s.palette, sizeBase: s.sizeBase, labelMode: s.labelMode };
}
export function rendererStyle(s) {
  return {
    showEdges: s.showEdges, showEdgeDir: s.showEdgeDir,
    background: s.background, bgColor: s.bgColor, edgeColor: s.edgeColor,
    edgeWidth: s.edgeWidth,
  };
}
/** ForceAtlas2 参数（worker）。
 *  scalingRatio 控制斥力强度（越大节点越散开），gravity 控制向心拉力（越小越不聚拢）。
 *  strongGravityMode=false 避免引力随距离放大（该模式会导致节点向中心堆积）。
 */
export function layoutSettings(layout) {
  return {
    scalingRatio: 30 * (layout.scaling || 1),
    gravity: 0.8 * (layout.gravity || 1),
    strongGravityMode: false,
    spacing: layout.spacing || 5,
  };
}

export function syncLabels() {
  $(IDS.sizeVal).textContent = (+$(IDS.size).value).toFixed(1);
  $(IDS.edgeWidthVal).textContent = (+$(IDS.edgeWidth).value).toFixed(1);
  $(IDS.scalingVal).textContent = (+$(IDS.scaling).value).toFixed(1);
  $(IDS.gravityVal).textContent = (+$(IDS.gravity).value).toFixed(1);
  $(IDS.spacingVal).textContent = (+$(IDS.spacing).value).toFixed(1);
}

export function bindLiveLabels() {
  for (const id of [IDS.size, IDS.edgeWidth, IDS.scaling, IDS.gravity, IDS.spacing]) {
    $(id).addEventListener('input', syncLabels);
  }
}
