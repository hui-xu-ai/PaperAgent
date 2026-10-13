# mdkit — Markdown 渲染库（自包含 · 可整体迁移）

PaperAgent 前端的通用 Markdown 渲染能力,从 `reader.js` 抽出独立封装。

## 边界契约(硬约束,由守卫测试强制)

- 本目录内**禁止**出现任何业务符号:`readerState`、`api(`、`appendEvent`、`state.`、
  具体 `/api/*` 路径、`escapeHtml` 之外的 utils 全局。
- 只允许依赖 vendor 全局:`marked`、`katex`、`hljs`(经 `globalThis` 惰性读取,缺失即降级)。
- 业务/领域特化(MinerU 产物清理、学术引用上标、知识库图片 API 重写、双链跳转)
  **一律不进库**,通过 `createRenderer({ plugins })` 的钩子注入。

## 用法

```js
import { createRenderer } from './mdkit/index.js';

const renderer = createRenderer({ plugins: [/* 业务插件 */] });
const html = renderer.render(md, { baseDir, source });  // ctx 由调用方传入
```

## 插件钩子(全部可选)

| 钩子 | 时机 | 用途 |
|---|---|---|
| `preprocess(md, ctx) -> md` | marked 之前,文本级 | 清理/还原原文 |
| `mathInline(tex, ctx) -> null｜{raw}` | 行内公式候选 | 拦截伪公式,`{raw}` 表示脱壳按原文渲染 |
| `protect(md, protector, ctx) -> md` | 内置保护之后 | 用 `protector.math()/code()` 追加受保护片段 |
| `postprocess(html, ctx) -> html` | 还原之后,HTML 级 | 图片重写、链接绑定 |

## 目录

```
core/escape.js      自带 HTML 转义(须与 utils.js 逐字一致)
core/protect.js     占位符保护系统(公式/代码 + 可扩展令牌)
core/pipeline.js    渲染管线调度(唯一入口 render)
features/           通用能力:frontmatter / callout / math(经 protect) / code / anchors / wikilink
index.js            对外 API
```
