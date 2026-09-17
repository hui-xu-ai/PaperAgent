/* graph/api.js — 文献计量图谱后端 API 客户端（自包含 fetch，与 app.js 解耦）。
 *
 * 契约（见 backend/app/api/lit.py，前缀 /api/lit/v1/graph）：
 *   GET /network   引用网络（服务端过滤）→ {nodes, edges, meta}
 *   GET /filters   过滤分面 → {year, library_citations, impact_factor, quartiles, clusters, ...}
 *   GET /neighbors 长按高亮 → {doi, citing[], cited[]}
 *   GET /node      节点详情 → {title, abstract, authors, keywords, ...}
 */
const BASE = '/api/lit/v1/graph';

async function _get(path, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    qs.append(k, String(v));
  }
  const url = `${BASE}${path}${qs.toString() ? '?' + qs : ''}`;
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { const j = await r.json(); detail = j.detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();
}

/** 引用网络。filters 见后端 graph_network 查询参数。 */
export function getNetwork(filters = {}) {
  return _get('/network', filters);
}

/** 过滤器分面（取值范围 / 计数 / 聚类）。 */
export function getFilters() {
  return _get('/filters');
}

/** 长按高亮：引用该文献的（citing）与该文献引用的（cited）。 */
export function getNeighbors(doi) {
  return _get('/neighbors', { doi });
}

/** 节点详情。 */
export function getNode(doi) {
  return _get('/node', { doi });
}
