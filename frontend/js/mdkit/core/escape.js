/* mdkit/core/escape.js — 库自带 HTML 转义，保证 mdkit 零外部业务依赖（可整体拎出）。
   实现必须与 frontend/js/utils.js 的 escapeHtml 逐字一致，否则渲染输出会漂移。 */
export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
