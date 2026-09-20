/* graph/styleApplier.js — 纯函数式样式计算（无渲染逻辑）。
 *
 * 职责：
 *   - 根据样式选项计算节点的 renderSize/renderColor/renderLabel
 *   - 根据样式选项计算边的 renderColor/renderWidth/hidden
 *   - 返回带渲染属性的图数据（原地修改）
 */

import { inferIfMax } from './scales.js';

/** 颜色映射表（影响因子 → 颜色）。 */
const COLOR_SCALES = {
  viridis: [
    { if: 0, color: '#440154' },
    { if: 10, color: '#3b528b' },
    { if: 20, color: '#21908c' },
    { if: 30, color: '#5ec962' },
    { if: 40, color: '#aadc32' },
    { if: 50, color: '#fde725' },
  ],
  inferno: [
    { if: 0, color: '#000004' },
    { if: 10, color: '#420a68' },
    { if: 20, color: '#932667' },
    { if: 30, color: '#dd513a' },
    { if: 40, color: '#fca50a' },
    { if: 50, color: '#fcffa4' },
  ],
  cool: [
    { if: 0, color: '#00ffff' },
    { if: 25, color: '#ff00ff' },
    { if: 50, color: '#ffff00' },
  ],
  grayscale: [
    { if: 0, color: '#000000' },
    { if: 25, color: '#808080' },
    { if: 50, color: '#ffffff' },
  ],
};

/** 线性插值颜色。 */
function _lerpColor(color1, color2, t) {
  const r1 = parseInt(color1.slice(1, 3), 16);
  const g1 = parseInt(color1.slice(3, 5), 16);
  const b1 = parseInt(color1.slice(5, 7), 16);
  const r2 = parseInt(color2.slice(1, 3), 16);
  const g2 = parseInt(color2.slice(3, 5), 16);
  const b2 = parseInt(color2.slice(5, 7), 16);

  const r = Math.round(r1 + (r2 - r1) * t);
  const g = Math.round(g1 + (g2 - g1) * t);
  const b = Math.round(b1 + (b2 - b1) * t);

  return `#${r.toString(16).padStart(2, '0')}${g.toString(16).padStart(2, '0')}${b.toString(16).padStart(2, '0')}`;
}

/** 根据影响因子获取颜色。 */
function _getColorForIF(ifValue, palette) {
  const scale = COLOR_SCALES[palette] || COLOR_SCALES.viridis;
  if (ifValue == null || isNaN(ifValue)) ifValue = 0;

  // 找到区间
  for (let i = 0; i < scale.length - 1; i++) {
    if (ifValue >= scale[i].if && ifValue <= scale[i + 1].if) {
      const t = (ifValue - scale[i].if) / (scale[i + 1].if - scale[i].if);
      return _lerpColor(scale[i].color, scale[i + 1].color, t);
    }
  }

  // 超出范围
  if (ifValue < scale[0].if) return scale[0].color;
  return scale[scale.length - 1].color;
}

/** 计算节点大小（基于被引次数，根据 citationSource 选择字段）。 */
function _computeNodeSize(node, sizeBase, citationSource) {
  let citeCount;
  if (citationSource === 'wos') {
    citeCount = node.times_cited || 0;
  } else if (citationSource === 'library') {
    citeCount = node.library_citations || 0;
  } else {
    // filtered: 使用后端计算的 filtered_citations
    citeCount = node.filtered_citations || 0;
  }
  node.cite_count = citeCount; // 保存供标签使用
  // 对数缩放，避免过大节点
  const rawSize = 2 + Math.log2(1 + citeCount) * 1.5;
  return Math.max(1, rawSize * sizeBase);
}

/** 计算节点标签。 */
function _computeNodeLabel(node, labelMode) {
  switch (labelMode) {
    case 'year_cited':
      return `${node.year || '?'} · ${node.cite_count || 0}`;
    case 'title':
      const title = node.title || node.id;
      return title.length > 30 ? title.slice(0, 27) + '...' : title;
    case 'none':
      return '';
    default:
      return '';
  }
}

/**
 * 应用样式到图数据（原地修改 nodes/edges）。
 * @param {GraphDataModel} dataModel - 数据模型
 * @param {Object} styleOpts - 样式选项
 * @returns {GraphDataModel} - 同一实例（链式调用）
 */
export function applyStyles(dataModel, styleOpts) {
  const {
    palette = 'viridis',
    sizeBase = 1,
    labelMode = 'year_cited',
    showEdges = true,
    edgeColor = '#8890a0',
    edgeWidth = 0.3,
    citationSource = 'filtered',
  } = styleOpts;

  // 计算 IF 最大值（用于颜色归一化，如果需要）
  const ifMax = inferIfMax(dataModel.nodes);

  // 应用节点样式
  dataModel.nodes.forEach(node => {
    node.renderSize = _computeNodeSize(node, sizeBase, citationSource);
    node.renderColor = _getColorForIF(node.impact_factor || 0, palette);
    node.renderLabel = _computeNodeLabel(node, labelMode);
  });

  // 应用边样式（宽度 0 = 隐藏边）
  dataModel.edges.forEach(edge => {
    edge.renderColor = edgeColor;
    edge.renderWidth = edgeWidth;
    edge.hidden = !showEdges || edgeWidth <= 0;
  });

  return dataModel;
}

/**
 * 生成渲染器样式选项（背景、边显隐等）。
 * @param {Object} styleOpts - 原始样式选项
 * @returns {Object} - 渲染器需要的格式
 */
export function toRendererStyle(styleOpts) {
  return {
    showEdges: styleOpts.showEdges !== false,
    showEdgeDir: styleOpts.showEdgeDir || false,
    background: styleOpts.background || 'light',
    bgColor: styleOpts.bgColor || null,
    edgeColor: styleOpts.edgeColor || '#8890a0',
    edgeWidth: styleOpts.edgeWidth != null ? styleOpts.edgeWidth : 0.3,
  };
}
