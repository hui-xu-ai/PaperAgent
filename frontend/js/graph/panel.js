/* graph/panel.js — 详情面板 / HUD / 图例 / 面板显隐（纯 DOM，无图谱库依赖）。
 *
 * 职责单一：把数据画进右侧详情区、左下角 HUD、右下角图例，并控制面板折叠。
 * 详情数据来自 api.getNode(doi)（标题/摘要/关键词/作者/被引/IF/分区/度数）。
 */
import { paletteGradient } from './scales.js';
import { HL_CITING, HL_CITED } from './renderer2d.js';

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export class Panel {
  constructor() {
    this.detail = $('lg-detail');
    this.side = $('lg-side');
    this.body = $('lg-detail-body');
    this.hud = $('lg-hud');
    this.legend = $('lg-legend');
    this.msg = $('lg-filter-msg');
    this.loading = $('lg-loading');
  }

  setLoading(on) { if (this.loading) this.loading.style.display = on ? 'flex' : 'none'; }

  setHud(nodes, edges, meta) {
    if (!this.hud) return;
    let txt = `节点 ${nodes} · 边 ${edges}`;
    if (meta && meta.truncated) txt += ` · 已截断(${meta.matched_nodes})`;
    this.hud.textContent = txt;
  }

  setMessage(txt) { if (this.msg) this.msg.textContent = txt || ''; }

  /** 右下角图例：配色渐变 + 大小含义 + 高亮双色说明。 */
  renderLegend(palette = 'viridis', ifMax = 10) {
    if (!this.legend) return;
    this.legend.innerHTML = `
      <div style="font-weight:700;margin-bottom:4px;color:#3a3f48">图例</div>
      <div class="lg-gradient" style="background:${paletteGradient(palette)}"></div>
      <div class="lg-lg-row" style="justify-content:space-between">
        <span>影响因子 低</span><span>高 (${ifMax.toFixed(1)})</span>
      </div>
      <div class="lg-lg-row"><span class="lg-swatch" style="background:#9aa"></span>节点大小 ∝ 库内被引</div>
      <div style="margin-top:5px;border-top:1px solid #d4d8e0;padding-top:4px;font-weight:600;color:#3a3f48">长按高亮</div>
      <div class="lg-lg-row"><span class="lg-swatch" style="background:${HL_CITING}"></span>引用了该文献</div>
      <div class="lg-lg-row"><span class="lg-swatch" style="background:${HL_CITED}"></span>该文献所引用</div>`;
  }

  toggleSide(force) {
    if (!this.side) return;
    const collapsed = force !== undefined ? !force : this.side.classList.toggle('collapsed');
    this.side.classList.toggle('collapsed', collapsed);
  }

  toggleDetail(force) {
    if (!this.detail) return;
    const collapsed = force !== undefined ? !force : this.detail.classList.toggle('collapsed');
    this.detail.classList.toggle('collapsed', collapsed);
    return !collapsed;
  }

  showDetailPlaceholder() {
    if (this.body) {
      this.body.innerHTML =
        '<p class="lg-hint">点击节点查看详情；长按节点高亮其引用/被引关系。</p>';
    }
  }

  /** 渲染节点详情（detail = api.getNode 返回值，含 in_kb / 度数）。 */
  renderDetail(d) {
    if (!this.body || !d) return;
    this.toggleDetail(true);
    const tags = [];
    if (d.quartile) tags.push(`<span class="lg-d-tag lg-q1">${esc(d.quartile)}</span>`);
    if (d.year) tags.push(`<span class="lg-d-tag">${esc(d.year)}</span>`);
    if (d.impact_factor) tags.push(`<span class="lg-d-tag">IF ${(+d.impact_factor).toFixed(2)}</span>`);
    if (d.in_kb) tags.push(`<span class="lg-d-tag lg-inkb">📚 在知识库</span>`);
    if (d.is_reference) tags.push(`<span class="lg-d-tag">参考节点</span>`);

    const kw = (d.keywords || []).slice(0, 12)
      .map((k) => `<span>${esc(k)}</span>`).join('');
    const allAuthors = (d.authors || []).join('、');
    const affiliations = (d.affiliations || []);
    const affHtml = affiliations.length
      ? `<div class="lg-d-sec">研究单位</div><div class="lg-d-authors">${esc(affiliations.join('；'))}</div>`
      : '';

    this.body.innerHTML = `
      <div class="lg-d-title">${esc(d.title || d.doi)}</div>
      <div class="lg-d-doi" style="margin-top:0;margin-bottom:8px">DOI: <a href="https://doi.org/${esc(d.doi)}" target="_blank" rel="noopener">${esc(d.doi)}</a></div>
      <div class="lg-d-meta">${tags.join('')}</div>
      <div class="lg-d-stats">
        <div class="lg-d-stat"><div class="k">库内被引</div><div class="v">${d.library_citations || 0}</div></div>
        <div class="lg-d-stat"><div class="k">文献被引</div><div class="v">${d.times_cited || 0}</div></div>
        <div class="lg-d-stat"><div class="k">引用它(入度)</div><div class="v">${d.in_degree || 0}</div></div>
        <div class="lg-d-stat"><div class="k">它引用(出度)</div><div class="v">${d.out_degree || 0}</div></div>
      </div>
      ${d.journal ? `<div class="lg-d-sec">期刊</div><div class="lg-d-text">${esc(d.journal)}</div>` : ''}
      ${allAuthors ? `<div class="lg-d-sec">作者</div><div class="lg-d-authors">${esc(allAuthors)}</div>` : ''}
      ${affHtml}
      ${kw ? `<div class="lg-d-sec">关键词</div><div class="lg-d-kw">${kw}</div>` : ''}
      ${d.abstract ? `<div class="lg-d-sec">摘要</div><div class="lg-d-text">${esc(d.abstract)}</div>` : ''}
      ${d.research_areas && d.research_areas.length ? `<div class="lg-d-sec">研究方向</div><div class="lg-d-text">${esc(d.research_areas.join('、'))}</div>` : ''}`;
  }
}
