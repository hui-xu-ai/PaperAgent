/* mdkit/features/anchors.js — 给 h1-h4 生成锚点 id（供 TOC/页内跳转）。 */
export function addHeadingAnchors(html) {
  return html.replace(/<h([1-4])>(.*?)<\/h\1>/g, (m, lv, content) => {
    const id = 'h-' + content.replace(/<[^>]+>/g, '').replace(/[^\w\u4e00-\u9fff-]+/g, '-').toLowerCase().slice(0, 40);
    return `<h${lv} id="${id}">${content}</h${lv}>`;
  });
}
