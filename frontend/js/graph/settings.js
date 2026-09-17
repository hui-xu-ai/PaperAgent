/* graph/settings.js — 样式面板：颜色/大小/标签/边/背景/引力参数 → 渲染与布局选项。
 *
 * readStyle() 读 UI；按消费方拆成三组纯数据，main.js 分发：
 *   decorateOpts()   → scales.decorateNodes（大小/颜色/标签）
 *   rendererStyle()  → renderer.setStyle（背景/边显隐/边色）
 *   layoutSettings() → worker ForceAtlas2（scalingRatio/gravity）
 * 仅读 DOM，不持状态。
 */
const $ = (id) => document.getElementById(id);

const IDS = {
  colorscale: 'lg-s-colorscale',
  size: 'lg-s-size', sizeVal: 'lg-s-size-val',
  label: 'lg-s-label',
  edges: 'lg-s-edges', edgeDir: 'lg-s-edgelabel',
  bg: 'lg-s-bg', bgColor: 'lg-s-bgcolor', edgeColor: 'lg-s-edgecolor',
  scaling: 'lg-s-scaling', scalingVal: 'lg-s-scaling-val',
  gravity: 'lg-s-gravity', gravityVal: 'lg-s-gravity-val',
};

export function readStyle() {
  return {
    palette: $(IDS.colorscale).value || 'viridis',
    sizeBase: Number($(IDS.size).value) || 1,
    labelMode: $(IDS.label).value || 'year_cited',
    showEdges: $(IDS.edges).checked,
    showEdgeDir: $(IDS.edgeDir).checked,
    background: $(IDS.bg).value || 'dark',
    bgColor: $(IDS.bgColor).value || null,
    edgeColor: $(IDS.edgeColor).value || '#3a4150',
    scaling: Number($(IDS.scaling).value) || 1,
    gravity: Number($(IDS.gravity).value) || 1,
  };
}

export function decorateOpts(s) {
  return { palette: s.palette, sizeBase: s.sizeBase, labelMode: s.labelMode };
}
export function rendererStyle(s) {
  return {
    showEdges: s.showEdges, showEdgeDir: s.showEdgeDir,
    background: s.background, bgColor: s.bgColor, edgeColor: s.edgeColor,
  };
}
/** ForceAtlas2 参数（worker）。scalingRatio=斥力强度，gravity=向心。 */
export function layoutSettings(s) {
  return { scalingRatio: 8 * s.scaling, gravity: s.gravity, gravityScaling: 1 };
}

export function syncLabels() {
  $(IDS.sizeVal).textContent = (+$(IDS.size).value).toFixed(1);
  $(IDS.scalingVal).textContent = (+$(IDS.scaling).value).toFixed(1);
  $(IDS.gravityVal).textContent = (+$(IDS.gravity).value).toFixed(1);
}

export function bindLiveLabels() {
  for (const id of [IDS.size, IDS.scaling, IDS.gravity]) {
    $(id).addEventListener('input', syncLabels);
  }
}
