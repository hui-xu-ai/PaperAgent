/* graph/filters.js — 过滤面板：UI ↔ 后端查询参数（服务端过滤，10 万级不卡）。
 *
 * readFilters() → /graph/network 的查询参数对象（空值省略）。
 * populateFacets(facets) → 用 /graph/filters 分面校准滑杆上限与提示。
 * 仅读 DOM，不持有图谱状态；与 main.js 解耦。
 */
const $ = (id) => document.getElementById(id);

const IDS = {
  yearMin: 'lg-f-year-min', yearMax: 'lg-f-year-max',
  libcit: 'lg-f-libcit', libcitVal: 'lg-f-libcit-val',
  cited: 'lg-f-cited', citedVal: 'lg-f-cited-val',
  if: 'lg-f-if', ifVal: 'lg-f-if-val',
  quartile: 'lg-f-quartile',
  inkb: 'lg-f-inkb', exref: 'lg-f-exref',
  limit: 'lg-f-limit', limitVal: 'lg-f-limit-val',
  sort: 'lg-f-sort', yearRange: 'lg-year-range',
  citSource: 'lg-f-cit-source', exisol: 'lg-f-exisol',
};

const DEFAULTS = { limit: 2000, sort: 'library_citations' };

function _num(v) {
  if (v === '' || v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** 读取过滤 UI → 后端查询参数（空值/全选用省略，减小请求）。 */
export function readFilters() {
  const p = {};
  const ymin = _num($(IDS.yearMin).value);
  const ymax = _num($(IDS.yearMax).value);
  if (ymin != null) p.year_min = ymin;
  if (ymax != null) p.year_max = ymax;

  const libcit = _num($(IDS.libcit).value);
  if (libcit) p.min_library_citations = libcit;
  const cited = _num($(IDS.cited).value);
  if (cited) p.min_times_cited = cited;
  const iff = _num($(IDS.if).value);
  if (iff) p.min_impact_factor = iff;

  // 分区：勾选子集才发；全选/全不选 → 省略（全选=不过滤，全不选无意义按不过滤处理）。
  const boxes = [...$(IDS.quartile).querySelectorAll('input[type=checkbox]')];
  const checked = boxes.filter((b) => b.checked).map((b) => b.value);
  if (checked.length && checked.length < boxes.length) p.quartiles = checked.join(',');

  if ($(IDS.inkb).checked) p.in_kb_only = true;
  if ($(IDS.exref).checked) p.exclude_references = true;

  // 被引来源
  const citSourceRadio = $(IDS.citSource).querySelector('input[type=radio]:checked');
  if (citSourceRadio) p.citation_source = citSourceRadio.value;

  // 孤立节点过滤
  if (!$(IDS.exisol).checked) p.exclude_isolated = false;

  const limit = _num($(IDS.limit).value);
  p.limit = limit || DEFAULTS.limit;
  p.sort_by = $(IDS.sort).value || DEFAULTS.sort;
  return p;
}

/** 用分面校准滑杆上限/提示（不覆盖用户已设的当前值，除非超界）。 */
export function populateFacets(f) {
  if (!f) return;
  if (f.year) {
    const yr = $(IDS.yearRange);
    if (yr) yr.textContent = f.year.min && f.year.max ? `${f.year.min}–${f.year.max}` : '';
    $(IDS.yearMin).placeholder = f.year.min || '最小';
    $(IDS.yearMax).placeholder = f.year.max || '最大';
  }
  _setRangeMax(IDS.libcit, f.library_citations && f.library_citations.max, 10);
  _setRangeMax(IDS.cited, f.times_cited && f.times_cited.max, 100);
  _setRangeMax(IDS.if, f.impact_factor && f.impact_factor.max, 50, 0.5);
  // 分区计数写进 chip 文本。
  if (f.quartiles) {
    for (const b of $(IDS.quartile).querySelectorAll('input[type=checkbox]')) {
      const chip = b.closest('label');
      if (chip) chip.lastChild.textContent = `${b.value}${f.quartiles[b.value] != null ? ' (' + f.quartiles[b.value] + ')' : ''}`;
    }
  }
}

function _setRangeMax(id, max, fallback, step) {
  const el = $(id);
  const m = Number(max) > 0 ? Number(max) : fallback;
  el.max = m;
  if (step != null) el.step = step;
  if (_num(el.value) > m) el.value = m;
}

/** 恢复默认过滤值。 */
export function resetFilters() {
  $(IDS.yearMin).value = '';
  $(IDS.yearMax).value = '';
  $(IDS.libcit).value = 0;
  $(IDS.cited).value = 0;
  $(IDS.if).value = 0;
  for (const b of $(IDS.quartile).querySelectorAll('input[type=checkbox]')) b.checked = true;
  $(IDS.inkb).checked = false;
  $(IDS.exref).checked = false;
  $(IDS.limit).value = DEFAULTS.limit;
  $(IDS.sort).value = DEFAULTS.sort;
  // 被引来源默认选 filtered
  const citSourceRadios = $(IDS.citSource).querySelectorAll('input[type=radio]');
  for (const r of citSourceRadios) r.checked = (r.value === 'filtered');
  // 孤立节点默认隐藏
  $(IDS.exisol).checked = true;
  syncLabels();
}

/** 同步滑杆旁的数值标签。 */
export function syncLabels() {
  $(IDS.libcitVal).textContent = $(IDS.libcit).value;
  $(IDS.citedVal).textContent = $(IDS.cited).value;
  $(IDS.ifVal).textContent = $(IDS.if).value;
  $(IDS.limitVal).textContent = $(IDS.limit).value;
}

/** 绑定滑杆 input 事件 → 实时刷新数值标签。 */
export function bindLiveLabels() {
  for (const [rangeId] of [[IDS.libcit], [IDS.cited], [IDS.if], [IDS.limit]]) {
    const el = $(rangeId);
    el.addEventListener('input', syncLabels);
  }
}
