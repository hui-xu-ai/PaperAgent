/* mdkit/core/protect.js — 占位符保护系统（库的核心资产）。
   LaTeX 里的 `_`(下标)/`~`(间距)/换行会被 marked 当 markdown 语法误处理，把公式切碎。
   策略：先把公式/代码抽成 `@@Mn@@` 占位符让 marked 原样通过，marked 之后再还原为
   KaTeX / 高亮代码。业务插件可通过 math()/code() 往同一保护系统里追加受保护片段。 */

export class Protector {
  constructor() { this.tokens = []; }

  push(token) {
    const id = `@@M${this.tokens.length}@@`;
    this.tokens.push(token);
    return id;
  }

  // display=true → 块级公式($$)，false → 行内($)。lang 字段沿用旧契约存 'block'/'inline'。
  math(tex, display) {
    return this.push({ kind: 'math', lang: display ? 'block' : 'inline', payload: tex });
  }

  code(lang, code) {
    return this.push({ kind: 'code', lang: lang || '', payload: code });
  }

  // 把 html 里的 @@Mn@@ 还原为 KaTeX / <pre><code>+hljs。libs = { katex, hljs }（可缺省降级）。
  restore(html, libs) {
    return html.replace(/@@M(\d+)@@/g, (m, idx) => {
      const it = this.tokens[+idx];
      if (!it) return m;
      if (it.kind === 'math') {
        if (!libs.katex) return `$${it.payload}$`;
        try {
          return libs.katex.renderToString(it.payload, {
            displayMode: it.lang === 'block', throwOnError: false });
        } catch (e) { return `$${it.payload}$`; }
      }
      let code = it.payload;
      if (libs.hljs) {
        try {
          code = it.lang ? libs.hljs.highlight(code, { language: it.lang }).value
                         : libs.hljs.highlightAuto(code).value;
        } catch (e) { /* 高亮失败保持原文 */ }
      }
      const cls = it.lang ? ` class="hljs language-${it.lang}"` : ' class="hljs"';
      return `<pre><code${cls}>${code}</code></pre>`;
    });
  }
}
