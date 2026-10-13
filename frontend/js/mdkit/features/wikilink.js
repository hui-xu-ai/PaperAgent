/* mdkit/features/wikilink.js — Obsidian 双链 [[name|alias]] → 带样式链接。
   注意：库只负责语法 → <a class="wikilink">，不绑定跳转行为（跳转需解析 RID/DOI，
   属业务知识，由调用方通过事件委托在容器上实现）。 */
import { escapeHtml } from '../core/escape.js';

export function renderWikilinks(html) {
  return html.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (m, name, alias) => {
    const clean = name.replace(/\.md$/, '');
    return `<a class="wikilink" title="Obsidian 双链: ${escapeHtml(clean)}">${escapeHtml(alias || clean)}</a>`;
  });
}
