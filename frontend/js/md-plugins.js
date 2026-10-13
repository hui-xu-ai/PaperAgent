/* md-plugins.js — PaperAgent 领域特有的 mdkit 渲染插件(应用层,非库)。
   业务知识全部住这里,mdkit/ 保持干净。抽成独立模块便于装配层与测试脚本共用同一份定义。 */

/* 插件一:paperDocPlugin —— 论文文档域特化。
   认识 MinerU 解析产物格式与学术引用上标约定,不认识任何 API/状态。 */
export const paperDocPlugin = {
  // 文本预处理:清理 MinerU 图片占位注释 + 还原被转义的内联格式标签
  preprocess(md) {
    // MinerU 残留 `<!-- image -->` / `<!-- image-1 -->`(后端已清洗,这里双保险)
    md = md.replace(/<!--\s*(?:image|img)[\s\-_]*\d*\s*-->/gi, '');
    // 解析管线可能把 <del>/<br>/<em>/<sup> 等以 HTML 实体写入 en.md,还原成真标签才能渲染
    md = md.replace(/&lt;(\/?(?:del|em|br|sup|sub|i|b|mark|u))&gt;/g, '<$1>');
    return md;
  },
  // 拦截 MinerU 伪公式:把 HTML 上标包进 $…$(如 $<sup>[1]</sup>$)。
  // 内含 <sup>/<sub> 且无真 LaTeX 特征字符(\ ^ _ { })→ 脱去 $ 还原成裸标签交 marked 渲染。
  mathInline(tex) {
    if (/<\/?su[bp]>/i.test(tex) && !/[\\^_{}]/.test(tex)) return { raw: tex };
    return null;
  },
  // 追加保护:英文原文里的引用上标是裸的 ^{[34]}(en.md 全篇如此,中文变体由后端包成 $^{[34]}$)。
  // 位置在内置公式保护之后 ⇒ 剩下的 ^{...} 必在公式外,按行内公式送 KaTeX(与中文变体同形)。
  // 只认引用 [n] / 纯数字符号,不碰正文 ^{文字} 与 Obsidian 内联脚注 ^[注]。
  protect(md, prot) {
    return md.replace(/(?<!\$)([\^_])\{([^{}\n]{1,40})\}(?!\$)/g, (m, sign, body) => {
      const t = body.trim();
      const likeCite = /^\[[\d,;\s\u2013\u2014-]+\]$/.test(t);
      const likeNum = /^[\d+-]{1,6}$/.test(t);
      if (!likeCite && !likeNum) return m;
      return prot.math(sign + '{' + t + '}', false);
    });
  },
};

/* 插件二:kbImagePlugin —— 知识库/文献库图片 API 重写(基础设施布线)。
   认识 baseDir/source 与 /api 图片端点,是唯一接触 API 路径的插件。 */
export const kbImagePlugin = {
  postprocess(html, ctx) {
    const baseDir = ctx && ctx.baseDir;
    if (!baseDir) return html;
    const apiBase = ctx.source === 'lib' ? '/api/library/image' : '/api/kb/image';
    return html.replace(/<img src="([^"]+)"([^>]*)>/g, (m, src, rest) => {
      if (/^(https?:|data:|\/)/.test(src)) return m;
      const clean = src.replace(/^\.\//, '');
      if (clean.startsWith('images/')) {
        return `<img src="${apiBase}?path=${encodeURIComponent(baseDir + '/' + clean)}"${rest}>`;
      }
      return m;
    });
  },
};

export const appPlugins = [paperDocPlugin, kbImagePlugin];
