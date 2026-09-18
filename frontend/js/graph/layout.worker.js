/* graph/layout.worker.js — ForceAtlas2 引力布局 Web Worker。
 *
 * 用 importScripts 加载 vendor 的 graphology + forceatlas2 UMD。
 * fa2.forceatlas2.assign() 是高层 API：接受 graphology 图 → 内部转 Float32Array
 * → 迭代 → 回写坐标。比直接调 fa2.iterate（原始数组 API）安全。
 *
 * 协议：
 *   → {type:'init', ids, edges, clusters, settings}
 *   ← {type:'ready', n}
 *   → {type:'run', iterations:N}
 *   ← {type:'positions', positions:Float32Array(2N)}
 *   → {type:'update-settings', settings}
 */
importScripts(
  '/vendor/graphology/graphology.umd.min.js',
  '/vendor/fa2/graphology-layout-forceatlas2.umd.min.js'
);

const GraphCtor = self.graphology.Graph || self.graphology;
const fa2 = self.forceatlas2;

let graph = null;
let ids = [];
let settings = null;

function seedPositions(ids, clusters, randomize) {
  const clusterIds = [...new Set(ids.map(id => clusters[id] || 0))];
  const clusterAngle = {};
  // randomize（用户点"重跑布局"）时加随机旋转，否则固定 0 保证可复现
  const angleOffset = randomize ? Math.random() * Math.PI * 2 : 0;
  clusterIds.forEach((c, i) => { clusterAngle[c] = (i / Math.max(1, clusterIds.length)) * Math.PI * 2 + angleOffset; });
  const R = Math.max(200, ids.length * 0.6);
  const pos = {};
  ids.forEach((id, i) => {
    const c = clusters[id] || 0;
    const a = clusterAngle[c];
    const cx = Math.cos(a) * R, cy = Math.sin(a) * R;
    // 确定性种子用索引哈希（可复现）；randomize 用真随机（重跑得到新布局）
    const jitter = randomize ? Math.random() : ((i * 2654435761) % 1000) / 1000;
    const jr = 40 + jitter * 120;
    const ja = randomize ? Math.random() * Math.PI * 2 : jitter * Math.PI * 2;
    pos[id] = { x: cx + Math.cos(ja) * jr, y: cy + Math.sin(ja) * jr };
  });
  return pos;
}

self.onmessage = (e) => {
  const msg = e.data;

  if (msg.type === 'init') {
    ids = msg.ids;
    graph = new GraphCtor();
    const clusters = msg.clusters || {};
    const seed = seedPositions(ids, clusters, !!msg.randomize);
    for (const id of ids) {
      graph.addNode(id, { x: seed[id].x, y: seed[id].y });
    }
    for (const ed of (msg.edges || [])) {
      if (ed.source === ed.target) continue;
      if (!graph.hasNode(ed.source) || !graph.hasNode(ed.target)) continue;
      try { graph.addEdge(ed.source, ed.target); } catch (_) {}
    }
    const inferred = fa2.inferSettings ? fa2.inferSettings(graph) : {};
    settings = Object.assign({}, inferred, {
      barnesHutOptimize: ids.length > 2000,
      slowDown: 3,
      adjustSizes: true,
    }, msg.settings || {});
    self.postMessage({ type: 'ready', n: ids.length });
    return;
  }

  if (msg.type === 'run') {
    if (!graph) return;
    const iters = Math.max(1, msg.iterations | 0);
    // fa2.forceatlas2.assign = 高层 API：graph → 内部转数组 → 迭代 → 回写图
    fa2.forceatlas2.assign(graph, { iterations: iters, settings });
    const pos = new Float32Array(ids.length * 2);
    for (let i = 0; i < ids.length; i++) {
      const a = graph.getNodeAttributes(ids[i]);
      pos[2 * i] = a.x; pos[2 * i + 1] = a.y;
    }
    self.postMessage({ type: 'positions', positions: pos.buffer }, [pos.buffer]);
    return;
  }

  if (msg.type === 'update-settings') {
    if (settings) Object.assign(settings, msg.settings || {});
    return;
  }
};
