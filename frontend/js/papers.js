/* papers.js — 论文列表·类型工具·加载/失效清理（从 app.js 拆出） */

/* ══════════ 论文列表 ══════════ */
const STATUS_MAP = {
  pending: ['pending', '排队中'], parsing: ['parsing', '解析中'],
  translating: ['translating', '翻译中'], translated: ['done', '已译'],
  parsed: ['done', '已解析'], failed: ['failed', '失败'],
  waiting_review: ['pending', '⏸ 待复核'],
};

/* P2-B：解析来源徽标（与后端 engine_service.PARSE_SOURCE_LABELS 保持一致） */
const PARSE_SOURCE_META = {
  'mineru-v4':     { cls: 'src-hi',   label: '🔬 v4 精准' },
  'mineru':        { cls: 'src-mid',  label: '☁ v1 免费' },
  'pymupdf-local': { cls: 'src-low',  label: '💻 本地低精度' },
  'auto-local':    { cls: 'src-low',  label: '💻 本地低精度' },
  'dual':          { cls: 'src-dual', label: '🔀 双通道+AI' },
  'p14':           { cls: 'src-dual', label: '🔀 双通道OCR+AI' },
};

/* P5-FB-点5：文献可提问 = 解析完成即可（translated 已译 / parsed 仅解析；不强求翻译） */
function paperReadyForQa(p) {
  if (!p) return false;
  return p.status === 'translated' || p.status === 'parsed' || p.task_state === 'done';
}
function parseSourceBadge(p) {
  const m = PARSE_SOURCE_META[p.parse_source];
  if (!m) return '';
  return `<span class="p-source ${m.cls}" title="该文献解析来源：${m.label}（设置中心可调整解析通道）">${m.label}</span>`;
}

/* P5-FB-点1：导入流水线模式徽标（parse_compile=编译 / parse=仅解析；full 不显示避免噪音） */
const PMODE_META = {
  parse_compile: { cls: 'pm-pc', label: '编译' },
  parse: { cls: 'pm-pp', label: '仅解析' },
};
function pipelineModeBadge(p) {
  const m = PMODE_META[p.pipeline_mode];
  if (!m) return '';
  const tip = p.pipeline_mode === 'parse_compile'
    ? '导入模式：解析 → 纳入知识库 → 编译L1，跳过翻译'
    : '导入模式：仅解析，不翻译';
  return `<span class="p-source ${m.cls}" title="${tip}">${m.label}</span>`;
}

/* ══════════ P0-B step3：资源类型（kind）与附件共用工具 ══════════
   用户模型：资源类型在目录名（RID）前缀里，也在 papers_meta.kind 里；
   论文无前缀（`10.1002_adma…`）/ `book__` 书 / `thesis__` 学位论文 / `std__` 标准 /
   `patent__` 专利 / `chapter__` 章节 / `nd-<指纹>` 无编号资料。 */
const KIND_META = {
  paper:    { icon: '📄', label: '论文' },
  thesis:   { icon: '🎓', label: '学位论文' },
  book:     { icon: '📚', label: '书' },
  chapter:  { icon: '📖', label: '章节' },
  patent:   { icon: '🔧', label: '专利' },
  standard: { icon: '📐', label: '标准' },
  report:   { icon: '📊', label: '报告' },
  note:     { icon: '📝', label: '笔记' },
  si:       { icon: '📎', label: '支撑信息' },
  review:   { icon: '🧾', label: '审稿意见' },
  data:     { icon: '🗄', label: '数据' },
};
const CHIP_KINDS = ['paper', 'thesis', 'book', 'chapter', 'patent', 'standard', 'report', 'note'];
// 搜索框语法糖（F4）：中文词 / kind:xxx → 类型过滤
const KIND_ALIASES = {
  '论文': 'paper', '文献': 'paper', 'paper': 'paper',
  '书': 'book', '书籍': 'book', 'book': 'book',
  '学位论文': 'thesis', '硕博': 'thesis', '毕业论文': 'thesis', 'thesis': 'thesis',
  '标准': 'standard', 'std': 'standard', 'standard': 'standard',
  '专利': 'patent', 'patent': 'patent',
  '章节': 'chapter', 'chapter': 'chapter',
  '报告': 'report', 'report': 'report',
  '无编号': 'none', '无标识': 'none', 'none': 'none',
};
/* 类型：后端 p.kind 优先，其次目录名（doc_json 父目录）前缀推导 */
const KIND_PREFIXES = ['book__', 'thesis__', 'std__', 'patent__', 'chapter__', 'si__', 'review__', 'note__'];
function _paperDirName(p) {
  const d = (p && p.doc_json) || '';
  const parts = d.replace(/\\/g, '/').split('/');
  // doc_json = <资源目录>/document.json → 取倒数第二段
  return parts.length >= 2 ? parts[parts.length - 2] : '';
}
function kindOfPaper(p) {
  const k = (p && p.kind) ? String(p.kind).toLowerCase() : '';
  if (k) return k;
  const name = _paperDirName(p);
  for (const pref of KIND_PREFIXES) {
    if (name.startsWith(pref)) return kindOfRid(pref);
  }
  return 'paper';
}
function kindOfRid(pref) {
  const m = { 'book__': 'book', 'thesis__': 'thesis', 'std__': 'standard', 'patent__': 'patent',
              'chapter__': 'chapter', 'si__': 'si', 'review__': 'review', 'note__': 'note' };
  return m[pref] || 'paper';
}
function isNoIdPaper(p) {
  const name = _paperDirName(p);
  return name.startsWith('nd-') || /^[0-9a-f]{32}$/i.test(name);
}
function kindBadge(p) {
  const k = kindOfPaper(p);
  if (k === 'paper') return '';   // 论文是默认类型，不占徽标位
  const m = KIND_META[k] || { icon: '❓', label: k };
  return `<span class="p-kind p-kind-${k}" title="资源类型：${m.label}">${m.icon} ${m.label}</span>`;
}
function attBadge(p) {
  const n = Number(p.attachments || 0);
  if (!n) return '';
  return `<button class="btn small p-att" data-id="${p.id}" title="查看该文献的依附资料（支撑信息/审稿意见/数据）：${n} 个文件">📎 附件 ${n}</button>`;
}
/* F4：搜索框语法糖解析 → {kind, q}（`书 电化学` / `kind:thesis 磁性`） */
function parseSearchSyntax(raw) {
  const s = (raw || '').trim();
  if (!s) return { kind: '', q: '' };
  let kind = '';
  const tokens = s.split(/\s+/);
  const rest = [];
  tokens.forEach(t => {
    const lower = t.toLowerCase();
    if (lower.startsWith('kind:')) {
      const v = lower.slice(5);
      kind = KIND_ALIASES[v] || v || '';
      return;
    }
    if (!kind && Object.prototype.hasOwnProperty.call(KIND_ALIASES, t)) {
      kind = KIND_ALIASES[t];
      return;
    }
    rest.push(t);
  });
  return { kind, q: rest.join(' ') };
}
/* F6：类型分组（类型内保持日期降序；只有一种类型时不加分组头） */
function groupPapersByKind(list) {
  const map = {};
  const order = [];
  (list || []).forEach(p => {
    const k = isNoIdPaper(p) ? 'none' : kindOfPaper(p);
    if (!map[k]) { map[k] = []; order.push(k); }
    map[k].push(p);
  });
  order.sort((a, b) => {
    const ia = CHIP_KINDS.indexOf(a === 'none' ? 'note' : a);
    const ib = CHIP_KINDS.indexOf(b === 'none' ? 'note' : b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return order.map(k => ({ kind: k, items: map[k] }));
}
function kindGroupLabel(k) {
  if (k === 'none') return '❓ 无编号资料';
  const m = KIND_META[k] || { icon: '❓', label: k };
  return `${m.icon} ${m.label}`;
}

/* N3：文献卡三态状态标签（解析方式 / 已翻译 / 已纳入），统一放卡片上方 */
function paperStatusBadges(p) {
  // 解析方式：有 parse_source 显示解析来源；未解析/失败显示"未解析"
  const src = parseSourceBadge(p) || (p.status === 'failed'
    ? '<span class="p-source src-none">未解析</span>' : '');
  // 已翻译 / 未翻译（text_zh 有无）
  const translated = (p.translated_paragraphs || 0) > 0;
  const trBadge = `<span class="p-source ${translated ? 'src-hi' : 'src-low'}" title="${translated ? '已翻译' : '未翻译（仅解析，可点「仅翻译」）'}">${translated ? '已翻译' : '未翻译'}</span>`;
  // 已纳入 / 未纳入（kb 状态）
  const inKb = p._kb && p._kb.in_kb;
  const kbBadge = `<span class="p-source ${inKb ? 'src-dual' : 'src-low'}" title="${inKb ? '已纳入知识库' : '未纳入知识库（可点「导入kb」）'}">${inKb ? '已纳入' : '未纳入'}</span>`;
  // P0-A（2026-09-11）磁盘为准：解析产物已被移走/删除（用户在资源管理器手删
  // library/<dir>/）→ 显式提示，避免"文件没了卡片还在"让人以为还有文件。
  // on_disk 由后端 /api/papers 提供（doc_json 是否真实存在）。
  const _inflight = ['pending', 'running'].includes(p.task_state)
    || ['pending', 'parsing'].includes(p.status);
  const lostBadge = (p.on_disk === false && !_inflight)
    ? `<span class="p-source src-low" title="解析产物已不在磁盘上（library/ 下该目录已被移动或删除）。可用「⋯ 更多 → 删除导入记录」清理这条记录。">⚠ 产物已丢失</span>`
    : '';
  return src + trBadge + kbBadge + lostBadge;
}

/* E3：文献库分页加载（page/page_size/q 随请求传；5s 轮询保持当前页）
   papersLoaded 序号守卫：翻页/搜索/轮询并发时，过期响应直接丢弃 */
async function loadPapers() {
  const seq = ++state.papersLoaded;
  try {
    const ps = new URLSearchParams({ page: state.papersPage, page_size: state.papersPageSize });
    ps.set('all', 'true');   // O2：一次取全，前端按导入日期分组跳转（不再翻页）
    if (state.papersQ) ps.set('q', state.papersQ);
    // F1/F4：类型芯片与搜索语法糖 → 服务端过滤（q 已去掉类型词，避免搜不到）
    const syn = parseSearchSyntax(state.papersQ);
    const kindSel = state.papersKind || 'all';
    if (kindSel === 'has_attachment') ps.set('has_attachment', 'true');
    else if (kindSel && kindSel !== 'all') ps.set('kind', kindSel);
    else if (syn.kind) ps.set('kind', syn.kind);
    if (syn.q !== (state.papersQ || '').trim()) ps.set('q', syn.q);
    const data = await api('/api/papers?' + ps.toString());
    if (seq !== state.papersLoaded) return; // 已被更新的请求取代
    state.papers = data.papers || [];
    state.papersTotal = data.total || 0;
    (data.papers || []).forEach(p => {
      const prev = state.paperById[p.id];
      // T7：复用已查的 kb 状态缓存（轮询刷新不重查）。
      // 2026-09-12 修：**只复用正值**——负值（in_kb=false）曾在"编译侧还没同步进 kb"的
      // 瞬间被缓存，之后每 5s 轮询都原样拷贝 ⇒ 永不自愈，用户看到「🔒 存 _qa / 未纳入」
      // 只有整页刷新才恢复。负值交给 enrichPapersKbStatus 带节流地重查。
      if (prev && prev._kb && prev._kb.in_kb) {
        p._kb = prev._kb; p._kbChecked = true; p._kbCheckedAt = prev._kbCheckedAt || 0;
      }
      state.paperById[p.id] = p;
    });
    // F3：资源目录键（doc_json 父目录名；重解析后 doc_json 可能变化）
    (data.papers || []).forEach(p => {
      const k = _paperDirName(p);
      if (k) state.paperRidById[p.id] = k;
    });
    enrichPapersKbStatus();   // T7：异步补查"已解析未入 kb"状态（不阻塞渲染）
    // 页数越界（如过滤后 total 收缩 / 数据被删）→ 回退末页重载
    const pages = Math.max(1, Math.ceil(state.papersTotal / state.papersPageSize));
    if (state.papersPage > pages) { state.papersPage = pages; loadPapers(); return; }
    renderPapers();
    renderPapersLost();   // P0-A：失效记录提示条（产物已被手删/移走）
    refreshReviewGate();   // P12F：顺带刷新复核门控汇总条（5s 轮询内）
  } catch (e) { console.warn('loadPapers failed', e); }
}

/* P0-A（2026-09-11）磁盘为准：文献库里的"记录还在、产物已不在"提示 + 一键清理。
   用户会在资源管理器手删/移动 library/<dir>/，此时卡片仍会显示（DB 有记录），
   这里显式汇总并提供"只删记录、不动磁盘文件"的清理入口（复用 DELETE /api/papers/{id}）。 */
function renderPapersLost() {
  const bar = $('papers-lost');
  if (!bar) return;
  // 2026-09-12 用户反馈：解析**进行中**不该报"产物已丢失"（后端已按任务态修正，
  // 这里做前端双保险——旧版后端或缓存响应也不会再闪这条提示）。
  const lost = (state.papers || []).filter(p => p.on_disk === false
    && !['pending', 'running'].includes(p.task_state)
    && !['pending', 'parsing'].includes(p.status));
  if (!lost.length) {
    bar.style.display = 'none';
    bar.innerHTML = '';
    return;
  }
  bar.style.display = '';
  bar.innerHTML = '<span>⚠ <b>' + lost.length + '</b> 条记录的解析产物已不在磁盘上' +
    '（library/ 下对应目录被删除或移动过）。</span>' +
    '<button class="btn small" id="papers-lost-clean" title="只删除数据库记录，不会删除磁盘上的任何文件">🧹 清理这些记录</button>';
  const btn = $('papers-lost-clean');
  if (btn) {
    btn.addEventListener('click', async () => {
      if (!(await askConfirm('将删除 ' + lost.length + ' 条失效记录（仅删记录，不动磁盘文件）。继续？'))) return;
      btn.disabled = true;
      btn.textContent = '清理中…';
      let ok = 0, fail = 0;
      for (const p of lost) {
        try { await api('/api/papers/' + p.id, { method: 'DELETE' }); ok++; }
        catch (e) { fail++; }
      }
      btn.textContent = '已清理 ' + ok + (fail ? '（失败 ' + fail + '）' : '');
      await loadPapers();
    });
  }
}


/* N2：文献卡片下方列出该文献全部提问分支（最新在前），可点击切换/重命名/删除 */
function paperBranchesHtml(p) {
  const mine = (state.sessions || []).filter(s => s.kind === 'paper' && s.paper_id === p.id)
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  if (!mine.length) return '';
  const items = mine.map(s => `<div class="p-branch ${s.id === state.sessionId ? 'active' : ''}" data-pid="${p.id}" data-sid="${s.id}" title="打开该提问分支：${escapeHtml(sessionTitle(s))}">
    <span class="p-branch-title">💬 ${escapeHtml(shortTitle(sessionTitle(s), 26))}</span>
    <button class="p-branch-ren" data-ren="${s.id}" title="重命名分支">✎</button>
    <button class="p-branch-del" data-del="${s.id}" title="删除分支">✕</button>
  </div>`).join('');
  return `<div class="p-branches"><div class="p-branches-head muted">对话分支</div>${items}</div>`;
}

/* O2：文献库按导入日期分组 + 侧边日期快速跳转 */
function paperDate(p) {
  const c = (p.created_at || '');
  return c ? c.slice(0, 10) : '未标注日期';
}
function groupPapersByDate(list) {
  const map = {};
  (list || []).forEach(p => {
    const d = paperDate(p);
    (map[d] = map[d] || []).push(p);
  });
  return Object.keys(map).sort((a, b) => b.localeCompare(a))
    .map(d => ({ date: d, items: map[d] }));
}
/* F1：文献库类型芯片（计数来自当前筛选结果集 state.papers；点击即筛） */
function renderPaperKindChips() {
  const el = $('papers-kind-chips');
  if (!el) return;
  const cur = state.papersKind || 'all';
  const base = (state.papers || []).filter(p => !state.papersDate || state.papersDate === 'all'
    || paperDate(p) === state.papersDate);
  const counts = { all: base.length, has_attachment: 0, none: 0 };
  base.forEach(p => {
    const k = kindOfPaper(p);
    counts[k] = (counts[k] || 0) + 1;
    if (isNoIdPaper(p)) counts.none += 1;
    if (Number(p.attachments || 0) > 0) counts.has_attachment += 1;
  });
  const chips = [{ k: 'all', label: '全部' }];
  CHIP_KINDS.forEach(k => { if (counts[k]) chips.push({ k, label: (KIND_META[k] || {}).label || k }); });
  Object.keys(counts).forEach(k => {
    if (!CHIP_KINDS.includes(k) && !['all', 'none', 'has_attachment'].includes(k) && counts[k]) {
      chips.push({ k, label: (KIND_META[k] || {}).label || k });
    }
  });
  if (counts.none) chips.push({ k: 'none', label: '无编号' });
  if (counts.has_attachment) chips.push({ k: 'has_attachment', label: '📎 有附件' });
  el.innerHTML = chips.map(c =>
    `<button class="tc-chip ${c.k === cur ? 'active' : ''}" data-kind="${c.k}"
       title="按类型筛选：${escapeHtml(c.label)}（${counts[c.k] || 0} 篇）">${escapeHtml(c.label)}<span class="tc-n">${counts[c.k] || 0}</span></button>`).join('');
  el.querySelectorAll('.tc-chip').forEach(b => {
    b.addEventListener('click', () => {
      const k = b.dataset.kind;
      if (k === (state.papersKind || 'all')) return;
      state.papersKind = k;
      loadPapers();   // 服务端过滤（含 has_attachment/kind=none），保证总数与徽标一致
    });
  });
}

function renderPapersDates(dates, active) {
  const el = $('papers-dates');
  if (!el) return;
  if (!dates.length) { el.innerHTML = ''; return; }
  const chip = (d, label) =>
    `<button class="pd-chip ${d === active ? 'active' : ''}" data-date="${d}" title="${d === 'all' ? '显示全部日期' : '仅显示 ' + d}">${label}</button>`;
  el.innerHTML = chip('all', '全部')
    + dates.map(d => chip(d, d.slice(5))).join('');
  el.querySelectorAll('.pd-chip').forEach(c => {
    c.addEventListener('click', () => {
      if (c.dataset.date === state.papersDate) return;
      state.papersDate = c.dataset.date;
      renderPapers();
    });
  });
}


/* ─── 论文列表渲染 + kb 状态补查 + 搜索/分页（从 app.js 合并） ─── */

function renderPapers() {
  const box = $('papers-list');
  renderPaperKindChips();
  if (!state.papers.length) {
    box.innerHTML = (state.papersQ || (state.papersKind && state.papersKind !== 'all'))
      ? '<div class="empty">无匹配文献（共 0 篇），可清除搜索或点「全部」芯片</div>'
      : '<div class="empty">暂无文献，上传 PDF 后自动解析+翻译</div>';
    renderPapersPager();
    renderPapersDates([], state.papersDate);
    return;
  }
  const list = paperFiltered();
  const groups = groupPapersByKind(list);
  // F6：多类型时按类型分组（类型内保持日期降序）；单类型时维持原来的日期分组视图
  box.innerHTML = groups.length > 1
    ? groups.map(g =>
      `<div class="p-kindwrap" data-kind="${g.kind}">
         <div class="p-kindgroup">${kindGroupLabel(g.kind)}<span class="cnt">${g.items.length} 篇</span></div>
         ${groupPapersByDate(g.items).map(d =>
        `<div class="p-datewrap" data-date="${d.date}">
             <div class="p-dategroup">📅 ${d.date}<span class="cnt">${d.items.length} 篇</span></div>
             ${d.items.map(paperCardHtml).join('')}
           </div>`).join('')}
       </div>`).join('')
    : groupPapersByDate(list).map(g =>
      `<div class="p-datewrap" data-date="${g.date}">
         <div class="p-dategroup">📅 ${g.date}<span class="cnt">${g.items.length} 篇</span></div>
         ${g.items.map(paperCardHtml).join('')}
       </div>`).join('');
  renderPapersDates(groupPapersByDate(state.papers).map(g => g.date), state.papersDate);
  bindPaperCardEvents(box);
}

function paperFiltered() {
  let list = state.papers;
  if (state.papersDate && state.papersDate !== 'all') list = list.filter(p => paperDate(p) === state.papersDate);
  // F1/F4：本地兜底过滤（服务端已按 kind/q 过滤；这里保证轮询竞态/旧响应下界面仍自洽）
  const syn = parseSearchSyntax(state.papersQ);
  const kindSel = (state.papersKind && state.papersKind !== 'all') ? state.papersKind : syn.kind;
  if (kindSel === 'has_attachment') list = list.filter(p => Number(p.attachments || 0) > 0);
  else if (kindSel === 'none') list = list.filter(isNoIdPaper);
  else if (kindSel) list = list.filter(p => kindOfPaper(p) === kindSel);
  if (syn.q) {
    const q = syn.q.toLowerCase();
    list = list.filter(p => ((p.title || '') + ' ' + (p.filename || '')).toLowerCase().includes(q));
  }
  return list;
}

function paperCardHtml(p) {
  const [cls, label] = STATUS_MAP[p.status] || ['pending', p.status];
    const fname = p.filename || p.title || p.doc_title || '';
    const retry = (p.status === 'parsing')
      ? `<button class="btn small paper-cancel" data-id="${p.id}" title="取消当前解析（官方云队列繁忙等待中可随时取消；取消后任务标记为已取消）">✕ 取消解析</button>`
      : (p.status === 'failed' && p.doc_json)
      ? `<button class="btn small paper-retry" data-id="${p.id}" title="翻译已完成，仅重试导出（不耗 token）">重试导出</button>`
      : (p.status === 'failed'
        ? `<button class="btn small paper-reparse" data-id="${p.id}" title="重新解析+翻译（会重新消耗 MinerU/LLM 额度）">重试</button>`
        : (p.status === 'waiting_review'
          ? `<button class="btn small paper-tnow" data-id="${p.id}" title="跳过待复核项，立即用当前主文本翻译">⏭ 立即翻译</button>`
          : ''));
    // T7：识别复核常驻文献库卡片（双通道解析的文献，已解析/已译/待复核均可随时打开复核）
    const reviewBtn = (p.parse_source === 'dual' || p.parse_source === 'p14')
      && (p.status === 'waiting_review' || p.status === 'parsed' || p.status === 'translated')
      ? `<button class="btn small paper-review" data-id="${p.id}" title="识别复核：双通道差异仲裁，处理差异点后再确认翻译">🔍 复核</button>`
      : '';
    // T7：仅翻译（已解析未翻译 → 翻译写 text_zh，不纳入知识库）
    const translateOnly = (p.status === 'parsed' && p.doi)
      ? `<button class="btn small paper-tonly" data-id="${p.id}" title="仅翻译（写 document.json 的 text_zh，不纳入知识库）">🌐 仅翻译</button>`
      : '';
    // T7：已在知识库 → 显示 ✓ 已纳入徽标（知识库操作请到「知识库」面板）
    const kbBadge = p._kb && p._kb.in_kb
      ? `<span class="kb-badge kb-ok" title="已在知识库${(p._kb.compiled || []).length ? '，已编译：' + (p._kb.compiled || []).join(',') : '（未编译）'}">✓ 已纳入</span>`
      : '';
    // T7：快速导入知识库（已解析/已译 + 有 doi + 未纳入 kb → sync 纳入 + 编译 L1；可选附带翻译）
    const importable = p.doi && (p.status === 'parsed' || p.status === 'translated')
      && p._kb && !p._kb.in_kb;
    const kbImport = importable
      ? `<button class="btn small paper-kbimp" data-id="${p.id}" title="纳入知识库并编译 L1（不翻译）">📥 导入kb</button>`
        + (p.status === 'parsed'
          ? `<button class="btn small paper-kbimp-t" data-id="${p.id}" title="纳入知识库 + 编译 L1 + 立即翻译">📥 导入kb+译</button>` : '')
      : '';
    // P5-E1：文献库=导入任务队列——只保留 标题+状态+关键操作；kb/会话操作移入知识库详情模态
    const qaBtn = paperReadyForQa(p)
      ? `<button class="btn small paper-chat" data-id="${p.id}" title="该文献新建独立提问分支">💬 问答</button>`
      : '';
    // N3：批量操作（仅翻译/导入kb/导入kb+译）汇总到「更多」下拉（retry 等条件性按钮保留在状态区）
    const moreItems = [];
    if (translateOnly) moreItems.push({ cls: 'paper-tonly', id: p.id, label: '🌐 仅翻译', tip: '仅翻译（写 text_zh，不纳入知识库）' });
    if (kbImport) {
      moreItems.push({ cls: 'paper-kbimp', id: p.id, label: '📥 导入kb（仅编译）', tip: '纳入知识库并编译 L1，不翻译' });
      if (p.status === 'parsed') moreItems.push({ cls: 'paper-kbimp-t', id: p.id, label: '📥 导入kb+译', tip: '纳入知识库 + 编译 L1 + 立即翻译' });
    }
    // 2026-09-12：在文献卡上直接挂附件（SI/审稿意见/数据）——上下文入口，
    // 打开导入弹窗并**预选/锁定该篇为父资源**，省掉"去下拉里找父资源"。
    moreItems.push({ cls: 'paper-att-add', id: p.id, label: '📎 添加附件/审稿意见',
                     tip: '把 SI / 审稿意见 / 原始数据挂到该文献下（纯本地处理，0 token）' });
    // T5：删除导入记录——始终提供（任何状态的文献都可删）。只删导入记录，保留知识库/翻译产物；
    // 删除后同 md5 PDF 可重新导入（解除"该 PDF 已处理过"硬拦截）。
    moreItems.push({ cls: 'paper-del', id: p.id, label: '🗑 删除导入记录', tip: '删除该 PDF 的导入记录（保留知识库/翻译产物），删除后同 PDF 可重新导入' });
    const moreBtn = moreItems.length
      ? `<div class="p-more"><button class="btn small p-more-btn" data-id="${p.id}" title="更多操作">⋯ 更多</button>
           <div class="p-more-menu">${moreItems.map(m =>
             `<button class="btn small ${m.cls}" data-id="${m.id}" data-more="${m.cls}" title="${m.tip}">${m.label}</button>`).join('')}</div></div>`
      : '';
    return `<div class="paper-card ${p.id === state.currentPaperId ? 'active' : ''}" data-id="${p.id}" title="点击打开阅读器">
      <div class="p-title">${escapeHtml(shortTitle(fname, 46))}</div>
      <div class="p-meta">${kindBadge(p)}${attBadge(p)}${qaBtn}${reviewBtn}${moreBtn}${retry}</div>
      ${paperBranchesHtml(p)}
    </div>`;
}

function bindPaperCardEvents(box) {
  box.querySelectorAll('.paper-card').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.paper-retry') || e.target.closest('.paper-reparse')
          || e.target.closest('.paper-review') || e.target.closest('.paper-tnow')
          || e.target.closest('.paper-cancel') || e.target.closest('.paper-chat')
          || e.target.closest('.paper-kbimp') || e.target.closest('.paper-kbimp-t')
          || e.target.closest('.paper-tonly') || e.target.closest('.p-branch')
          || e.target.closest('.p-more-btn') || e.target.closest('.p-more-menu')
          || e.target.closest('.p-att') || e.target.closest('.paper-att-add')
          || e.target.closest('.p-branch-ren') || e.target.closest('.p-branch-del')) return;
      selectPaper(Number(el.dataset.id));
    });
  });
  // 2026-09-12：文献卡「📎 添加附件/审稿意见」→ 打开导入弹窗并预选该篇为父资源
  box.querySelectorAll('.paper-att-add').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openAttachImport(Number(btn.dataset.id));
    });
  });
  // F3：附件入口（📎 附件 N）→ 列出该文献的依附资料（SI/审稿意见/数据）
  box.querySelectorAll('.paper-card .p-att').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openAttachments(Number(btn.dataset.id));
    });
  });
  // P5-FB-点2：文献卡片快捷问答——每次点击新建该篇独立对话分支（N2：互不干扰）
  box.querySelectorAll('.paper-chat').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      // P1：新建文献会话（POST）→ 防连点
      guardBtn(btn, () => createPaperSession(Number(btn.dataset.id)), '新建中…');
    });
  });
  // N2：文献卡片下方会话分支——点击打开 / 重命名 / 删除
  box.querySelectorAll('.p-branch[data-sid]').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.p-branch-ren') || e.target.closest('.p-branch-del')) return;
      e.stopPropagation();
      openSession(Number(el.dataset.sid));
    });
  });
  box.querySelectorAll('.p-branch-ren').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      guardBtn(btn, () => renameSession(Number(btn.dataset.ren)), '…');   // P1：写操作防连点
    });
  });
  box.querySelectorAll('.p-branch-del').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      guardBtn(btn, async () => { await deleteSession(Number(btn.dataset.del)); loadPapers(); }, '…');
    });
  });
  // P15：解析中任务取消（官方云队列等待期间可随时取消）
  box.querySelectorAll('.paper-cancel').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!(await askConfirm('取消当前解析？\n（官方云队列繁忙等待中可随时取消；取消后任务标记为已取消，可稍后重试）'))) return;
      btn.disabled = true;
      btn.textContent = '取消中…';
      try {
        await api(`/api/tasks/${btn.dataset.id}/cancel`, { method: 'POST' });
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                      message: `论文[${btn.dataset.id}] 已请求取消（等待解析链响应）` });
        setTimeout(loadPapers, 2500);
      } catch (err) {
        alert('取消失败：' + err.message);
        btn.disabled = false;
        btn.textContent = '✕ 取消解析';
      }
    });
  });
  // P12F 逃生口：挂起待复核文献 → 立即翻译（跳过审核）
  box.querySelectorAll('.paper-tnow').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!(await askConfirm('跳过该文献的待复核项，立即用当前主文本翻译？（AI 误落地可在复核页改选回滚）'))) return;
      btn.disabled = true;
      btn.textContent = '启动中…';
      try {
        await api(`/api/papers/${btn.dataset.id}/translate-now`, { method: 'POST' });
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                      message: `论文[${btn.dataset.id}] 已强制启动翻译` });
        refreshReviewGate();
      } catch (err) {
        alert('启动失败：' + err.message);
        btn.disabled = false;
        btn.textContent = '⏭ 立即翻译';
      }
    });
  });
  // 重试导出（翻译已完成仅导出失败）
  box.querySelectorAll('.paper-retry').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      btn.disabled = true;
      btn.textContent = '导出中…';
      try {
        await api(`/api/papers/${btn.dataset.id}/retry-export`, { method: 'POST' });
        await loadPapers();
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                      message: `论文[${btn.dataset.id}] 导出完成` });
      } catch (err) {
        alert('重试导出失败：' + err.message);
        btn.disabled = false;
        btn.textContent = '重试导出';
      }
    });
  });
  // P2-B：失败论文一键重试（重新走完整流水线）
  box.querySelectorAll('.paper-reparse').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!(await askConfirm('重新解析该论文？将重新消耗 MinerU/LLM 额度。'))) return;
      btn.disabled = true;
      btn.textContent = '重排中…';
      try {
        await api(`/api/papers/${btn.dataset.id}/retry`, { method: 'POST' });
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                      message: `论文[${btn.dataset.id}] 已重新排队` });
        await loadPapers();
      } catch (err) {
        alert('重试失败：' + err.message);
        btn.disabled = false;
        btn.textContent = '重试';
      }
    });
  });
  // P12-6：识别复核入口（T7：常驻文献库卡片，知识库弹窗不再提供）
  box.querySelectorAll('.paper-review').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openReviewModal(Number(btn.dataset.id));
    });
  });
  // T7：文献卡片 → 快速导入知识库（导入kb=仅编译；导入kb+译=附带翻译）
  box.querySelectorAll('.paper-kbimp, .paper-kbimp-t').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      // P1：纳入知识库是写操作（会建 note/入队编译）→ 防连点
      guardBtn(btn, () => importPaperToKb(Number(btn.dataset.id),
                                          btn.classList.contains('paper-kbimp-t')), '导入中…');
    });
  });
  // T7：文献卡片 → 仅翻译（已解析未翻译，不纳入知识库）
  box.querySelectorAll('.paper-tonly').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const p = getPaper(Number(btn.dataset.id));
      if (!p || !(await askConfirm(`对「${shortTitle(p.filename || p.title, 40)}」仅执行翻译？（不纳入知识库）`))) return;
      btn.disabled = true;
      btn.textContent = '翻译中…';
      try {
        await api(`/api/papers/${btn.dataset.id}/translate-now`, { method: 'POST' });
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                      message: `论文[${btn.dataset.id}] 已启动翻译（仅翻译，未纳入知识库）` });
        setTimeout(loadPapers, 2500);
      } catch (err) {
        alert('启动翻译失败：' + err.message);
        btn.disabled = false;
        btn.textContent = '🌐 仅翻译';
      }
    });
  });
  // T5：删除导入记录——移除该文献的导入记录，同 md5 PDF 可重新导入（保留 kb/翻译产物）
  box.querySelectorAll('.paper-del').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const p = getPaper(Number(btn.dataset.id));
      if (!p) return;
      const nm = shortTitle(p.filename || p.title, 40);
      if (!(await askConfirm(`删除「${nm}」的导入记录？\n\n删除后此 PDF 可重新导入。\n（只删导入记录，知识库/翻译产物保留）\n\n若该文献任务进行中，删除后不再跟踪其进度。`))) return;
      btn.disabled = true;
      btn.textContent = '删除中…';
      try {
        await api(`/api/papers/${btn.dataset.id}`, { method: 'DELETE' });
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                      message: `论文[${btn.dataset.id}] 导入记录已删除（同 PDF 可重新导入）` });
        if (state.currentPaperId === Number(btn.dataset.id)) state.currentPaperId = null;
        await loadPapers();
      } catch (err) {
        alert('删除失败：' + err.message);
        btn.disabled = false;
        btn.textContent = '🗑 删除导入记录';
      }
    });
  });
  // N3：「⋯ 更多」下拉——点按钮 toggle，点菜单项/外部关闭
  box.querySelectorAll('.p-more-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const menu = btn.nextElementSibling;
      const all = box.querySelectorAll('.p-more-menu');
      all.forEach(m => { if (m !== menu) m.style.display = 'none'; });
      menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
    });
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.p-more')) box.querySelectorAll('.p-more-menu').forEach(m => m.style.display = 'none');
  });
  box.querySelectorAll('.p-more-menu button').forEach(btn => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    let m = btn.closest('.p-more-menu'); if (m) m.style.display = 'none';
  }));
  renderPapersPager();
}

/* T7：已解析/已译文献 → 快速导入知识库（sync 纳入 + 编译 L1 入队；可选附带翻译） */
async function importPaperToKb(paperId, withTranslate) {
  const p = getPaper(paperId);
  if (!p || !p.doi) return;
  const doi = p.doi;
  const how = withTranslate ? '纳入知识库 + 编译 L1 + 翻译' : '纳入知识库并编译 L1（不翻译）';
  if (!(await askConfirm(`将文献「${shortTitle(p.filename || p.title, 40)}」${how}？`))) return;
  try {
    await api('/api/kb-meta/source/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ doi, force: false }) });
    await api('/api/kb-meta/compile/queue', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ doi, level: 'L1' }) });
    if (withTranslate && p.status === 'parsed') {
      await api(`/api/papers/${paperId}/translate-now`, { method: 'POST' });
    }
    p._kb = p._kb || { in_kb: false, compiled: [] };   // 2026-09-12 修：旧版 `if (p._kb)` 在未查过时是空操作
    p._kb.in_kb = true;                                // 本地缓存置为已纳入 → 卡片导入按钮消失
    p._kbChecked = true; p._kbCheckedAt = Date.now();
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                  message: `论文[${paperId}] 已纳入知识库并排队编译 L1${withTranslate ? '（附带翻译）' : ''}` });
    await loadPapers();
    await loadKbList();
  } catch (err) { alert('导入知识库失败：' + err.message); }
}

/* T7：文献库加载后异步补查 kb 状态（q=<doi> 查 in_kb/compiled，缓存到 paper._kb；并发 6 池，不阻塞渲染） */
const PAPER_KB_RECHECK_MS = 30000;   // 负值重查节流（正值长期复用）

/* 单篇 kb 状态查询（唯一入口）：返回 {in_kb, compiled} 或 null（查询失败→保留旧值） */
async function fetchPaperKbState(p) {
  if (!p || !p.doi) return null;
  try {
    const r = await api('/api/kb-meta/kb/list?q=' + encodeURIComponent(p.doi) + '&page=1&page_size=5');
    const it = (r.items || []).find(x => x.doi === p.doi) || null;
    return it ? { in_kb: !!it.in_kb, compiled: it.compiled || [] } : { in_kb: false, compiled: [] };
  } catch (e) { return null; }
}

/* 点击「存 _qa」前的实时复核：即使本地缓存说"未纳入"，也再问一次服务端 */
async function paperInKbLive(p) {
  if (!p) return false;
  if (p._kb && p._kb.in_kb) return true;
  const st = await fetchPaperKbState(p);
  if (st) { p._kb = st; p._kbChecked = true; p._kbCheckedAt = Date.now(); }
  return !!(p._kb && p._kb.in_kb);
}

async function enrichPapersKbStatus() {
  const now = Date.now();
  const todo = (state.papers || []).filter(p => p.doi
    && (p.status === 'parsed' || p.status === 'translated')
    && (!p._kbChecked
        || (!(p._kb && p._kb.in_kb) && now - (p._kbCheckedAt || 0) > PAPER_KB_RECHECK_MS)));
  if (!todo.length) return;
  let i = 0;
  async function worker() {
    while (i < todo.length) {
      const p = todo[i++];
      const st = await fetchPaperKbState(p);
      if (st) {
        const was = p._kb && p._kb.in_kb;
        p._kb = st;
        // 由"未纳入"变为"已纳入" → 重绘列表（徽标/「存 _qa」锁态跟着更新）
        if (!was && st.in_kb) setTimeout(renderPapers, 0);
      }
      p._kbChecked = true;
      p._kbCheckedAt = Date.now();
    }
  }
  await Promise.all(Array.from({ length: Math.min(6, todo.length) }, worker));
  renderPapers();
}

/* E3：文献库分页控件（上一页/下一页 + 第 X / Y 页 · 共 N 篇） */
function renderPapersPager() {
  const pg = $('papers-pager');
  if (!pg) return;
  if (!state.papersTotal) { pg.innerHTML = ''; return; }
  // O2：不再翻页，改为日期分组跳转；此处仅显示总量信息
  pg.innerHTML = `<span class="muted">共 ${state.papersTotal} 篇 · 按导入日期分组（左侧日期条可快速跳转）</span>`;
}

/* E3：文献库搜索（q 过滤 + 重置回第 1 页） */
function applyPapersSearch() {
  const inp = $('papers-search-input');
  const q = (inp ? inp.value : '').trim();
  if (q === state.papersQ) return;
  state.papersQ = q;
  state.papersPage = 1;
  loadPapers();
}

function bindPapersSearch() {
  const inp = $('papers-search-input');
  if (!inp) return;
  inp.value = state.papersQ;
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') applyPapersSearch(); });
  const clear = $('papers-search-clear');
  if (clear) clear.addEventListener('click', () => { inp.value = ''; applyPapersSearch(); });
}

