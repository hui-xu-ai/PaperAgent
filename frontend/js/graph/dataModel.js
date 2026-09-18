/* graph/dataModel.js — 纯数据容器（无渲染逻辑）。
 *
 * 职责：
 *   - 存储 nodes/edges/ids/clusters
 *   - 提供过滤、属性映射、格式转换
 *   - 不可变操作（filter返回新实例）
 */

export class GraphDataModel {
  constructor() {
    this.nodes = [];
    this.edges = [];
    this.ids = [];
    this.clusters = {};
    this.meta = null;
  }

  /** 加载原始数据。 */
  load(rawNodes, rawEdges, meta) {
    this.nodes = rawNodes || [];
    this.edges = rawEdges || [];
    this.ids = this.nodes.map(n => n.id);
    this.clusters = Object.fromEntries(
      this.nodes.map(n => [n.id, n.cluster || 0])
    );
    this.meta = meta || null;
    return this;
  }

  /** 过滤节点和边，返回新实例。 */
  filter(opts = {}) {
    const { yearMin, yearMax, libCitMin, citedMin, ifMin, quartiles, inKbOnly, excludeRefs, limit, sortBy } = opts;

    // 过滤节点
    let filteredNodes = this.nodes.filter(n => {
      if (yearMin != null && n.year < yearMin) return false;
      if (yearMax != null && n.year > yearMax) return false;
      if (libCitMin != null && (n.library_citations || 0) < libCitMin) return false;
      if (citedMin != null && (n.times_cited || 0) < citedMin) return false;
      if (ifMin != null && (n.impact_factor || 0) < ifMin) return false;
      if (quartiles && quartiles.length > 0) {
        const q = n.quartile || 'N/A';
        if (!quartiles.includes(q)) return false;
      }
      if (inKbOnly && !n.in_kb) return false;
      if (excludeRefs && n.is_reference) return false;
      return true;
    });

    // 排序并截断
    if (sortBy) {
      filteredNodes.sort((a, b) => {
        const va = a[sortBy] || 0;
        const vb = b[sortBy] || 0;
        return vb - va; // 降序
      });
    }
    if (limit && filteredNodes.length > limit) {
      filteredNodes = filteredNodes.slice(0, limit);
    }

    // 构建ID集合
    const idSet = new Set(filteredNodes.map(n => n.id));

    // 过滤边（两端都在过滤后的节点中）
    const filteredEdges = this.edges.filter(e =>
      idSet.has(e.source) && idSet.has(e.target)
    );

    // 返回新实例
    const model = new GraphDataModel();
    model.load(filteredNodes, filteredEdges, this.meta);
    return model;
  }

  /** 批量转换节点属性（用于样式计算前的预处理）。 */
  mapAttrs(fn) {
    this.nodes.forEach(n => {
      const transformed = fn(n);
      Object.assign(n, transformed);
    });
    return this;
  }

  /** 输出渲染器需要的格式（nodes带render前缀属性，edges带render前缀属性）。 */
  toRenderFormat() {
    return {
      nodes: this.nodes.map(n => ({
        id: n.id,
        x: n.x,
        y: n.y,
        size: n.renderSize || n.size || 2,
        color: n.renderColor || n.color || '#4f9cf9',
        label: n.renderLabel || n.label || '',
        title: n.title || n.id,
        cluster: n.cluster || 0,
        // 保留原始数据供详情面板使用
        _raw: n,
      })),
      edges: this.edges.map(e => ({
        source: e.source,
        target: e.target,
        color: e.renderColor || e.color || '#8890a0',
        width: e.renderWidth != null ? e.renderWidth : (e.width || 1.2),
        hidden: e.hidden || false,
      })),
      meta: this.meta,
    };
  }

  /** 获取节点数量。 */
  get nodeCount() {
    return this.nodes.length;
  }

  /** 获取边数量。 */
  get edgeCount() {
    return this.edges.length;
  }

  /** 清空数据。 */
  clear() {
    this.nodes = [];
    this.edges = [];
    this.ids = [];
    this.clusters = {};
    this.meta = null;
  }
}
