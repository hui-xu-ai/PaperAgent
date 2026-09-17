/* graph/layout.worker.js — ForceAtlas2 引力布局 Web Worker（经典 worker）。
 *
 * 用 importScripts 加载 vendor 的 graphology + forceatlas2 UMD（无打包步骤）。
 * 主线程增量请求迭代（{type:'run', iterations}），worker 回传 Float32Array 坐标
 * （transferable，10 万节点也低开销）。布局在主线程之外跑 → UI 不卡。
 *
 * 协议：
 *   → {type:'init', ids:[], edges:[{source,target}], clusters:{id:cluster}, settings:{}}
 *   ← {type:'ready', n}
 *   → {type:'run', iterations:N}
 *   ← {type:'positions', positions:Float32Array(2N)}   // 顺序同 init.ids
 */
importScripts(
  '/vendor/graphology/graphology.umd.min.js',
  '/vendor/fa2/graphology-layout-forceatlas2.umd.min.js'
);

const GraphCtor = self.graphology;     // UMD 全局：graphology Graph 类
const fa2 = self.forceatlas2;          // IIFE 全局：{ iterate, inferSettings, ... }

let graph = null;
let ids = [];
let settings = null;

function seedPositions(ids, clusters) {
  // 聚类种子：同簇节点起始位置聚在一处（相关文献自然靠拢），簇心绕大圆分布。
  const clusterIds = [...new Set(ids.map(id => clusters[id] || 0))];
  const clusterAngle = {};
  clusterIds.forEach((c, i) => { clusterAngle[c] = (i / Math.max(1, clusterIds.length)) * Math.PI * 2; });
  const R = Math.max(200, ids.length * 0.6);   // 规模自适应半径
  const pos = {};
  ids.forEach((id, i) => {
    const c = clusters[id] || 0;
    const a = clusterAngle[c];
    // 簇心 + 簇内小幅随机散布（确定性伪随机，避免每次抖动）
    const cx = Math.cos(a) * R, cy = Math.sin(a) * R;
    const jitter = ((i * 2654435761) % 1000) / 1000;   // hash 散列
    const jr = 40 + jitter * 120;
    const ja = jitter * Math.PI * 2;
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
    const seed = seedPositions(ids, clusters);
    for (const id of ids) {
      graph.addNode(id, { x: seed[id].x, y: seed[id].y });
    }
    for (const ed of (msg.edges || [])) {
      if (ed.source === ed.target) continue;            // 自环跳过
      if (!graph.hasNode(ed.source) || !graph.hasNode(ed.target)) continue;
      try { graph.addEdge(ed.source, ed.target); } catch (_) { /* 重复边忽略 */ }
    }
    // FA2 设置：inferSettings 给基线，叠加用户调参 + 大图 Barnes-Hut 优化。
    const inferred = fa2.inferSettings ? fa2.inferSettings(graph) : {};
    settings = Object.assign({}, inferred, {
      barnesHutOptimize: ids.length > 2000,
      slowDown: 5,
      adjustSizes: true,
    }, msg.settings || {});
    self.postMessage({ type: 'ready', n: ids.length });
    return;
  }

  if (msg.type === 'run') {
    if (!graph) return;
    const iters = Math.max(1, msg.iterations | 0);
    for (let i = 0; i < iters; i++) fa2.iterate(settings, graph);
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
