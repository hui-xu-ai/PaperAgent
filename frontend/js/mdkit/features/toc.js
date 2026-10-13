/* mdkit/features/toc.js — 目录大纲(纯函数,不进渲染管线 ⇒ 不影响 golden 快照)。
   extractToc:从**渲染后**的 HTML 里按 anchors.js 注入的 h1-h4 id 提取大纲条目;
   renderTocHtml:把条目产出为 <nav> 结构(点击滚动/高亮等交互由调用方在容器上实现)。
   库只负责"提取 + 结构",不碰 DOM/滚动/业务。 */
import { escapeHtml } from '../core/escape.js';

// 取标题纯文本:先剔除 KaTeX 的 <math>(含 annotation 会重复 TeX),再去其余标签、解实体、压空白
function htmlToText(fragment) {
  return String(fragment)
    .replace(/<math[\s\S]*?<\/math>/gi, '')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/\s+/g, ' ')
    .trim();
}

// 从渲染后 HTML 提取 [{ level, id, text }]（依赖 anchors.js 已给 h1-h4 注入 id）
export function extractToc(html) {
  const out = [];
  const re = /<h([1-4])\s+id="([^"]+)">([\s\S]*?)<\/h\1>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const text = htmlToText(m[3]);
    if (text) out.push({ level: +m[1], id: m[2], text });
  }
  return out;
}

// 产出 <nav> 大纲结构;data-toc-target 指向标题 id,供调用方绑定滚动/高亮
export function renderTocHtml(items) {
  if (!items || !items.length) return '';
  const minLevel = items.reduce((a, it) => Math.min(a, it.level), 6);
  const lis = items.map(it => {
    const depth = it.level - minLevel;
    return `<li class="md-toc-item md-toc-l${it.level}" data-toc-target="${escapeHtml(it.id)}" style="--toc-depth:${depth}">`
         + `<span class="md-toc-text">${escapeHtml(it.text)}</span></li>`;
  }).join('');
  return `<nav class="md-toc"><div class="md-toc-head">目录</div><ul class="md-toc-list">${lis}</ul></nav>`;
}
