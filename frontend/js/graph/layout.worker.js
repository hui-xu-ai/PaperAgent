/* graph/layout.worker.js — ForceAtlas2 引力布局 Web Worker（含后处理去重叠）。
 *
 * 用 importScripts 加载 vendor 的 graphology + forceatlas2 UMD。
 * fa2.forceatlas2.assign() 是高层 API：接受 graphology 图 → 内部转 Float32Array
 * → 迭代 → 回写坐标。比直接调 fa2.iterate（原始数组 API）安全。
 *
 * 布局策略：
 *   1. FA2 先跑完全部迭代（不施加斥力），让图谱力学收敛
 *   2. 收敛后发送 desoverlap 消息，施加短程斥力消除重叠
 *
 * 防重叠斥力（短程力，仅在后处理阶段使用）：
 *   - 触发条件：圆心距 < 1.1 * (r1 + r2)
 *   - 范围外力为0，不影响全局布局
 *   - 范围内按 1/d^2 衰减，d = 圆弧间距
 *
 * 协议：
 *   → {type:'init', ids, edges, clusters, nodeSizes, settings}
 *   ← {type:'ready', n}
 *   → {type:'run', iterations:N}
 *   ← {type:'positions', positions:Float32Array(2N)}
 *   → {type:'desoverlap', passes:N}
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
  const angleOffset = randomize ? Math.random() * Math.PI * 2 : 0;
  clusterIds.forEach((c, i) => { clusterAngle[c] = (i / Math.max(1, clusterIds.length)) * Math.PI * 2 + angleOffset; });
  const R = Math.max(200, ids.length * 0.6);
  const pos = {};
  ids.forEach((id, i) => {
    const c = clusters[id] || 0;
    const a = clusterAngle[c];
    const cx = Math.cos(a) * R, cy = Math.sin(a) * R;
    const jitter = randomize ? Math.random() : ((i * 2654435761) % 1000) / 1000;
    const jr = 40 + jitter * 120;
    const ja = randomize ? Math.random() * Math.PI * 2 : jitter * Math.PI * 2;
    pos[id] = { x: cx + Math.cos(ja) * jr, y: cy + Math.sin(ja) * jr };
  });
  return pos;
}

function getRadius(nodeId) {
  const size = nodeSizes[nodeId] || 2;
  return size * 0.7;
}

/** 短程斥力：只在圆心距 < 1.1*(r1+r2) 时触发，范围外为0。 */
function applyOverlapRepulsion() {
  const k = 80000;
  const rangeMultiplier = 1.1;

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

      const radiusSum = r1 + r2;
      if (centerDist >= radiusSum * rangeMultiplier) continue;

      if (centerDist < 0.001) {
        const angle = Math.random() * Math.PI * 2;
        const push = radiusSum * 0.5;
        graph.setNodeAttribute(id1, 'x', pos1.x - Math.cos(angle) * push);
        graph.setNodeAttribute(id1, 'y', pos1.y - Math.sin(angle) * push);
        graph.setNodeAttribute(id2, 'x', pos2.x + Math.cos(angle) * push);
        graph.setNodeAttribute(id2, 'y', pos2.y + Math.sin(angle) * push);
        continue;
      }

      const arcDist = Math.max(0.1, centerDist - radiusSum);
      const force = Math.min(k / (arcDist * arcDist), 150);
      const fx = (dx / centerDist) * force;
      const fy = (dy / centerDist) * force;

      graph.setNodeAttribute(id1, 'x', pos1.x - fx);
      graph.setNodeAttribute(id1, 'y', pos1.y - fy);
      graph.setNodeAttribute(id2, 'x', pos2.x + fx);
      graph.setNodeAttribute(id2, 'y', pos2.y + fy);
    }
  }
}

function extractPositions() {
  const pos = new Float32Array(ids.length * 2);
  for (let i = 0; i < ids.length; i++) {
    const a = graph.getNodeAttributes(ids[i]);
    pos[2 * i] = a.x; pos[2 * i + 1] = a.y;
  }
  return pos;
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
    // FA2 纯迭代，不施加斥力（让力学自然收敛）
    fa2.forceatlas2.assign(graph, { iterations: iters, settings });
    const pos = extractPositions();
    self.postMessage({ type: 'positions', positions: pos.buffer }, [pos.buffer]);
    return;
  }

  if (msg.type === 'desoverlap') {
    if (!graph) return;
    const passes = Math.max(1, msg.passes | 0 || 100);
    for (let p = 0; p < passes; p++) {
      applyOverlapRepulsion();
    }
    const pos = extractPositions();
    self.postMessage({ type: 'positions', positions: pos.buffer }, [pos.buffer]);
    return;
  }

  if (msg.type === 'update-settings') {
    if (settings) Object.assign(settings, msg.settings || {});
    return;
  }
};
