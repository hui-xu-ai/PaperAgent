/* graph/layout.worker.js — ForceAtlas2 引力布局 Web Worker（含节点体积斥力）。
 *
 * 用 importScripts 加载 vendor 的 graphology + forceatlas2 UMD。
 * fa2.forceatlas2.assign() 是高层 API：接受 graphology 图 → 内部转 Float32Array
 * → 迭代 → 回写坐标。比直接调 fa2.iterate（原始数组 API）安全。
 *
 * 防重叠斥力：
 *   - 类似同种电荷斥力：F = k / d^2
 *   - 考虑节点体积：实际距离 = 中心距离 - (r1 + r2)
 *   - 当实际距离 < spacing 时，施加巨大斥力防止重叠
 *
 * 协议：
 *   → {type:'init', ids, edges, clusters, nodeSizes, settings}
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
let nodeSizes = {};
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

/** 计算节点半径（从 size 属性）。 */
function getRadius(nodeId) {
  const size = nodeSizes[nodeId] || 2;
  return size * 0.5; // 半径 = 直径的一半
}

/** 应用防重叠斥力（类似同种电荷，考虑节点体积）。 */
function applyOverlapRepulsion(graph, ids, spacing) {
  const k = 5000; // 斥力常数（越大斥力越强）
  const minDist = spacing || 5; // 最小允许距离（圆弧之间）

  for (let i = 0; i < ids.length; i++) {
    const id1 = ids[i];
    const pos1 = graph.getNodeAttributes(id1);
    const r1 = getRadius(id1);

    for (let j = i + 1; j < ids.length; j++) {
      const id2 = ids[j];
      const pos2 = graph.getNodeAttributes(id2);
      const r2 = getRadius(id2);

      const dx = pos2.x - pos1.x;
      const dy = pos2.y - pos1.y;
      const centerDist = Math.sqrt(dx * dx + dy * dy);

      if (centerDist < 0.001) continue; // 避免除零

      // 实际距离 = 中心距离 - 两个半径（圆弧之间的距离）
      const actualDist = centerDist - (r1 + r2);

      // 当实际距离 < minDist 时，施加斥力
      if (actualDist < minDist) {
        // 斥力大小：F = k / (actualDist + 0.1)^2（+0.1 避免除零）
        const force = k / Math.pow(actualDist + 0.1, 2);
        const fx = (dx / centerDist) * force;
        const fy = (dy / centerDist) * force;

        // 反向推开两个节点
        graph.setNodeAttribute(id1, 'x', pos1.x - fx);
        graph.setNodeAttribute(id1, 'y', pos1.y - fy);
        graph.setNodeAttribute(id2, 'x', pos2.x + fx);
        graph.setNodeAttribute(id2, 'y', pos2.y + fy);
      }
    }
  }
}

self.onmessage = (e) => {
  const msg = e.data;

  if (msg.type === 'init') {
    ids = msg.ids;
    nodeSizes = msg.nodeSizes || {};
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
    const spacing = settings.spacing || 5;

    // FA2 迭代
    fa2.forceatlas2.assign(graph, { iterations: iters, settings });

    // 应用防重叠斥力（每轮迭代后）
    applyOverlapRepulsion(graph, ids, spacing);

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
