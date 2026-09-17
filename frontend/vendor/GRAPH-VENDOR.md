# 图谱可视化第三方库（本地 vendor · 离线可用，不依赖 CDN）

文献计量图谱（AI检索 → 图谱 tab）使用的前端库。全部为浏览器 UMD/IIFE 构建，
经 `<script>` 标签加载，挂载到全局变量。

| 目录 | 库 | 版本 | 全局变量 | 用途 |
|---|---|---|---|---|
| `graphology/` | graphology | 0.26.0 | `graphology` | 图数据结构（节点/边） |
| `sigma/` | sigma.js | 2.4.0 | `Sigma` | 2D WebGL 渲染（10万节点级） |
| `fa2/` | graphology-layout-forceatlas2 | 0.10.1 | `forceatlas2` | ForceAtlas2 引力布局（Web Worker 内增量迭代） |
| `3d-force-graph/` | 3d-force-graph（含 three.js） | 1.80.0 | `ForceGraph3D` | 3D 力导向渲染 |

## fa2 构建说明

`graphology-layout-forceatlas2` 上游只发 CommonJS，无浏览器构建。
`fa2/graphology-layout-forceatlas2.umd.min.js` 由 esbuild 打包为 IIFE（global `forceatlas2`），
导出 `{ forceatlas2, iterate, inferSettings, validateSettings, FA2Layout }`。
复现命令（在装有依赖的临时目录）：

```bash
npm i graphology-layout-forceatlas2@0.10.1 esbuild
# entry: module.exports = { forceatlas2: require('graphology-layout-forceatlas2'),
#   iterate: require('graphology-layout-forceatlas2/iterate'), ... }
esbuild entry.js --bundle --format=iife --global-name=forceatlas2 --minify \
  --outfile=graphology-layout-forceatlas2.umd.min.js --target=es2020
```

实际布局在 `frontend/js/graph/layout.worker.js` 内用 `forceatlas2.iterate` 自行驱动，
不依赖 `FA2Layout`（后者会自起 worker URL，打包后失效）。
