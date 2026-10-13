/* mdkit/core/pipeline.js — 渲染管线调度（库唯一入口）。
   固定阶段顺序（与旧 renderMarkdown 逐字对齐，保证输出不漂移）：
     1. frontmatter 抽出 → 属性面板 HTML
     2. callout 预处理
     3. plugin.preprocess(md, ctx)        —— 文本级业务预处理
     4. 保护：围栏代码 → 块级公式 → 行内公式（含 plugin.mathInline 拦截）→ plugin.protect 追加保护
     5. marked.parse
     6. 还原保护块（KaTeX / hljs）
     7. wikilink → 标题锚点
     8. plugin.postprocess(html, ctx)     —— HTML 级业务后处理
   插件钩子（全部可选）：
     preprocess(md, ctx) -> md
     mathInline(tex, ctx) -> null | { raw }   // 拦截行内公式候选；返回 {raw} 则脱壳按原文渲染
     protect(md, protector, ctx) -> md        // 用 protector.math()/code() 追加受保护片段
     postprocess(html, ctx) -> html
   库对 vendor（marked/katex/hljs）只做 globalThis 惰性读取，不认识任何业务符号。 */
import { escapeHtml } from './escape.js';
import { Protector } from './protect.js';
import { splitFrontmatter, parseFrontmatterEntries, renderPropPanel } from '../features/frontmatter.js';
import { transformCallouts } from '../features/callout.js';
import { renderWikilinks } from '../features/wikilink.js';
import { addHeadingAnchors } from '../features/anchors.js';

function libs() {
  return { marked: globalThis.marked, katex: globalThis.katex, hljs: globalThis.hljs };
}

export function createRenderer(opts = {}) {
  const plugins = opts.plugins || [];

  function render(md, ctx = {}) {
    const L = libs();
    if (!L.marked) return `<pre>${escapeHtml(md)}</pre>`;
    md = String(md);

    // 1. frontmatter（抽出失败 = 保持旧行为：整块剥离、不渲染，不把 --- 泄漏到正文）
    let propsHtml = '';
    const fm = splitFrontmatter(md.replace(/^\uFEFF/, ''));
    if (fm) {
      md = fm.body;
      propsHtml = renderPropPanel(parseFrontmatterEntries(fm.raw));
    }

    // 2. callout
    md = transformCallouts(md).md;

    // 3. 业务预处理
    for (const p of plugins) if (p.preprocess) md = p.preprocess(md, ctx);

    // 4. 保护
    const prot = new Protector();
    md = md.replace(/```([\w+-]*)\s*\r?\n([\s\S]*?)```/g, (m, lang, code) => prot.code(lang || '', code));
    md = md.replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => prot.math(tex, true));
    md = md.replace(/\$([^$\n]+?)\$/g, (m, tex) => {
      for (const p of plugins) {
        if (p.mathInline) { const r = p.mathInline(tex, ctx); if (r && r.raw != null) return r.raw; }
      }
      return prot.math(tex, false);
    });
    for (const p of plugins) if (p.protect) md = p.protect(md, prot, ctx);

    // 5. marked
    let html = L.marked.parse(md, { breaks: true, gfm: true });

    // 6. 还原
    html = prot.restore(html, L);

    // 7. wikilink → 锚点
    html = renderWikilinks(html);
    html = addHeadingAnchors(html);

    // 8. 业务后处理
    for (const p of plugins) if (p.postprocess) html = p.postprocess(html, ctx);

    return `<div class="md">${propsHtml}${html}</div>`;
  }

  return { render };
}
