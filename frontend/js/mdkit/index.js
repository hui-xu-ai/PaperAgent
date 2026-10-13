/* mdkit/index.js — 库对外入口。
   边界契约见 ./README.md：本目录内禁止 import 或引用任何业务符号
   （阅读器状态 / 全局 api() / 事件日志 / 具体接口路径等），只依赖 vendor 全局
   （marked / katex / hljs）。业务特化通过 createRenderer({ plugins }) 注入。 */
export { createRenderer } from './core/pipeline.js';
export { Protector } from './core/protect.js';
export { escapeHtml } from './core/escape.js';
export { splitFrontmatter, parseFrontmatterEntries, renderPropPanel } from './features/frontmatter.js';
export { transformCallouts } from './features/callout.js';
export { renderWikilinks } from './features/wikilink.js';
export { addHeadingAnchors } from './features/anchors.js';
