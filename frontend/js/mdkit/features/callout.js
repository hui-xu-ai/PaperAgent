/* mdkit/features/callout.js — Obsidian callout `> [!type] 标题` 引用块 → 容器 HTML。
   逐行预处理（不解析 marked 输出）：注入的 HTML 块用空行与正文隔开，marked 的 HTML 块
   遇空行即结束 ⇒ 块内正文仍按正常 markdown 渲染（列表/链接/公式都有效）。 */
import { escapeHtml } from '../core/escape.js';

const CALLOUT_DEFAULT_TITLE = { info: 'Info', summary: 'Summary', warning: 'Warning', note: 'Note', tip: 'Tip' };

export function transformCallouts(md) {
  const lines = String(md).split('\n');
  const out = [];
  let i = 0, count = 0;
  while (i < lines.length) {
    const m = /^ {0,3}> ?\[!([A-Za-z][\w-]*)\]([+-]?)[ \t]*(.*)$/.exec(lines[i]);
    if (!m) { out.push(lines[i]); i++; continue; }
    const type = m[1].toLowerCase().replace(/[^a-z0-9-]/g, '') || 'note';
    const title = m[3].trim() || CALLOUT_DEFAULT_TITLE[type] || m[1];
    const body = [];
    let j = i + 1;
    while (j < lines.length) {
      const q = /^ {0,3}> ?(.*)$/.exec(lines[j]);
      if (!q) break;
      body.push(q[1]);
      j++;
    }
    while (body.length && !body[body.length - 1].trim()) body.pop();
    out.push(`<div class="callout callout-${type}"><div class="callout-title">${escapeHtml(title)}</div><div class="callout-body">`);
    out.push('');
    for (const b of body) out.push(b);
    out.push('');
    out.push('</div></div>');
    count++;
    i = j;
  }
  return { md: out.join('\n'), count };
}
