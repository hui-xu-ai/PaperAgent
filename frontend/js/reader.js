/* reader.js — 上传导入 + 右栏阅读器 + 多阅读面板 + 来源切换 + Markdown/KaTeX */

/* ══════════ 上传（V11 修正：上传=完整流水线，仅解析=独立按钮）══════════ */
/* ══════════ P0-B step3（F3）：依附资料清单（支撑信息 SI / 审稿意见 / 数据）══════════
   依附资料挂父资源目录 library/<文献目录>/attachments/{si,review,data}/——
   删/备份/迁移成组不会漏。资源键用目录名（back end 的 library_candidates 兼容
   RID 与目录名两种写法）。 */
let attState = { rid: '', title: '' };

function resourceKeyOf(p) {
  if (!p) return '';
  return state.paperRidById[p.id] || _paperDirName(p) || p.doi || '';
}
function fmtSize(n) {
  const b = Number(n || 0);
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1024 / 1024).toFixed(1) + ' MB';
}

async function openAttachments(paperId) {
  const p = state.paperById[paperId];
  const rid = resourceKeyOf(p);
  const title = (p && (p.title || p.filename)) || rid;
  if (!rid) {
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'warn', source: 'kb',
                  message: '该文献缺少资源键（目录名/DOI），无法列附件' });
    return;
  }
  attState = { rid, title };
  $('att-modal').style.display = 'flex';
  $('att-modal-title').textContent = '📎 ' + shortTitle(title, 40);
  $('att-body').innerHTML = '<div class="empty">加载中…</div>';
  try {
    const out = await api('/api/kb-meta/attachments?rid=' + encodeURIComponent(rid));
    renderAttachments(out);
  } catch (e) {
    $('att-body').innerHTML = '<div class="imp-err">⚠ ' + escapeHtml(e.message || String(e)) + '</div>';
  }
}

function renderAttachments(out) {
  const files = (out && out.files) || [];
  const head = `<div class="att-head">资源键：<code>${escapeHtml(attState.rid)}</code> ·
    存放：${out && out.parented ? '随该文献目录' : '独立附件根'} · 共 ${files.length} 个文件</div>`;
  if (!files.length) {
    $('att-body').innerHTML = head +
      `<div class="empty">该文献还没有依附资料（支撑信息 / 审稿意见 / 数据）。<br>
        用顶栏「＋ 导入」→「📎 支撑信息·审稿意见」页签添加（纯本地处理，0 token）。</div>`;
    return;
  }
  const rows = files.map(f => {
    const url = `/api/kb-meta/attachment/file?rid=${encodeURIComponent(attState.rid)}&path=${encodeURIComponent(f.path)}`;
    return `<tr>
      <td>${KIND_META[f.kind] ? KIND_META[f.kind].icon + ' ' : ''}<code>${escapeHtml(f.path)}</code></td>
      <td class="muted">${escapeHtml(f.kind)}</td>
      <td class="muted">${fmtSize(f.size)}</td>
      <td>${f.indexed ? '<span class="kba-badge ok" title="已进检索索引，可被知识库问答召回">已索引</span>'
        : '<span class="kba-badge warn" title="未进索引（二进制或抽取失败）">未索引</span>'}</td>
      <td><a class="btn small" href="${url}" target="_blank" rel="noopener">打开</a>
          <a class="btn small" href="${url}" download>下载</a></td>
    </tr>`;
  }).join('');
  $('att-body').innerHTML = head +
    `<table class="kba-table att-table"><thead><tr>
       <th>文件</th><th>类型</th><th>大小</th><th>检索</th><th>操作</th></tr></thead>
     <tbody>${rows}</tbody></table>`;
}

function bindAttachments() {
  const close = () => { $('att-modal').style.display = 'none'; };
  $('att-close').addEventListener('click', close);
  $('att-modal').addEventListener('click', (e) => { if (e.target.id === 'att-modal') close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('att-modal').style.display === 'flex') close();
  });
}

/* F5（2026-09-12 重做）：导入弹窗「父资源」选择器。
   旧实现：原生 <select> 把 state.papers **全量**塞进去 —— 文献上量后（几百上千篇）
   用户只能肉眼滚，无法按标题/DOI/文件名检索（实测 count=2、hasSearch=false）。
   新实现：**搜索型选择器**——输入即过滤，行内显示 类型图标 + 标题 + 资源键 + 导入日期，
   选中显示 chip 可一键清除。纯本地过滤（数据源 = 已加载的 state.papers），0 新接口。
   `#imp-att-parent`（隐藏 input）仍是唯一的键载体 → 提交逻辑零改动。 */
function attParentCandidates() {
  const seen = new Set();
  const out = [];
  (state.papers || []).forEach(p => {
    const key = resourceKeyOf(p);
    if (!key || seen.has(key)) return;
    seen.add(key);
    out.push({ key, kind: kindOfPaper(p),
               title: String(p.title || p.filename || key),
               file: String(p.filename || ''),
               date: String(p.created_at || '').slice(0, 10) });
  });
  return out;
}

function attParentMatches(q) {
  const all = attParentCandidates();
  const s = String(q || '').trim().toLowerCase();
  if (!s) return all.slice(0, 8);   // 空查询 → 先给最近的 8 篇（不逼用户先想关键词）
  return all.filter(c => (c.title + ' ' + c.key + ' ' + c.file).toLowerCase().includes(s))
            .slice(0, 20);
}

function setAttParent(key) {
  const hidden = $('imp-att-parent');
  if (hidden) hidden.value = key || '';
  const chosen = $('imp-att-parent-chosen');
  if (chosen) {
    if (!key) {
      chosen.textContent = '（不挂父资源：单独存放）';
      chosen.classList.remove('on');
    } else {
      const c = attParentCandidates().find(x => x.key === key);
      chosen.textContent = '📎 已挂：' + (c ? shortTitle(c.title, 40) + ' · ' + key : key) + '　✕ 清除';
      chosen.classList.add('on');
    }
  }
  const list = $('imp-att-parent-list');
  if (list) list.style.display = 'none';
  const q = $('imp-att-parent-q');
  if (q) q.value = '';
}

function renderAttParentList(query) {
  const box = $('imp-att-parent-list');
  if (!box) return;
  const items = attParentMatches(query);
  box.innerHTML = items.length
    ? items.map(c => `<div class="attp-item" data-key="${escapeHtml(c.key)}">
        <span class="attp-icon">${(KIND_META[c.kind] || {}).icon || '📄'}</span>
        <span class="attp-title">${escapeHtml(shortTitle(c.title, 44))}</span>
        <span class="attp-key">${escapeHtml(c.key)}</span>
        <span class="attp-date">${escapeHtml(c.date)}</span>
      </div>`).join('')
    : '<div class="attp-empty">没有匹配的文献（留空 = 不挂父资源，按内容指纹单独存放）</div>';
  box.style.display = 'block';
  box.querySelectorAll('.attp-item').forEach(el => {
    el.addEventListener('click', () => setAttParent(el.dataset.key));
  });
}

function bindAttParentPicker() {
  const q = $('imp-att-parent-q');
  const list = $('imp-att-parent-list');
  const chosen = $('imp-att-parent-chosen');
  if (!q || !list) return;
  q.addEventListener('focus', () => renderAttParentList(q.value));
  q.addEventListener('input', () => renderAttParentList(q.value));
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { list.style.display = 'none'; q.blur(); }
    if (e.key === 'Enter') {
      e.preventDefault();
      const first = list.querySelector('.attp-item');
      if (first) setAttParent(first.dataset.key);
    }
  });
  if (chosen) chosen.addEventListener('click', () => { if (chosen.classList.contains('on')) setAttParent(''); });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#itab-att')) list.style.display = 'none';
  });
}

/* 兼容旧调用点（setImportTab / 打开导入弹窗）：重置选择器状态 */
function refreshAttParentOptions() {
  setAttParent($('imp-att-parent') ? $('imp-att-parent').value : '');
  const list = $('imp-att-parent-list');
  if (list) list.style.display = 'none';
}

/* 文献卡「⋯ 更多 → 📎 添加附件」：**在卡片上直接挂**——用户此刻天然知道挂哪篇，
   把"去下拉里检索父资源"这一步从流程里删掉（bug4 的真正解痛点）。 */
function openAttachImport(paperId) {
  const p = getPaper(paperId);
  const key = p ? resourceKeyOf(p) : '';
  $('import-modal').style.display = 'flex';
  setImportTab('att');
  if (key) setAttParent(key);   // 必须在 setImportTab 之后（后者会重置选择器）
}

/* ══════════ P5-E2：统一导入向导（PDF / Markdown / Bib 引用三 tab）══════════
   顶栏「＋ 导入」#import-btn 打开模态；原 #upload-btn/#parse-btn/#md-import-btn
   事件已迁移至此（旧按钮已从顶栏移除，无遗留失效入口）。 */
function setImportTab(tab) {
  document.querySelectorAll('#import-modal .stab').forEach(b => b.classList.toggle('active', b.dataset.itab === tab));
  ['pdf', 'md', 'bib', 'att'].forEach(p => { const el = $('itab-' + p); if (el) el.style.display = p === tab ? '' : 'none'; });
  if (tab === 'att') refreshAttParentOptions();
}
function impOut(id, html) { $(id).innerHTML = html; }
function impErr(e) { return '<div class="imp-err">⚠ ' + escapeHtml(e.message || String(e)) + '</div>'; }

function bindImport() {
  $('import-btn').addEventListener('click', () => {
    $('import-modal').style.display = 'flex';
    setAttParent('');   // 从顶栏「＋导入」进入 → 父资源默认空（不沿用上次）
  });
  $('import-close').addEventListener('click', () => { $('import-modal').style.display = 'none'; });
  bindAttParentPicker();   // F5：父资源搜索型选择器
  document.querySelectorAll('#import-modal .stab').forEach(btn => {
    btn.addEventListener('click', () => setImportTab(btn.dataset.itab));
  });
  // 选中文件 → 启用对应「导入」按钮
  [['imp-pdf-input', 'imp-pdf-go'], ['imp-md-input', 'imp-md-go'], ['imp-bib-input', 'imp-bib-go'],
   ['imp-att-input', 'imp-att-go']]
    .forEach(([inputId, goId]) => {
      $(inputId).addEventListener('change', () => { $(goId).disabled = !$(inputId).files.length; });
    });

  // 写操作统一防重（P1）：guardBtn 在请求期间禁用按钮并显示进度文案，
  // 避免"连点两次 = 导入两遍/删两条"。四个 tab 的提交按钮都走它。
  $('imp-pdf-go').addEventListener('click', (e) => guardBtn(e.currentTarget, () => doImportPdf(), '提交中…'));
  $('imp-md-go').addEventListener('click', (e) => guardBtn(e.currentTarget, () => doImportMd(), '导入中…'));
  $('imp-bib-go').addEventListener('click', (e) => guardBtn(e.currentTarget, () => doImportBib(), '导入中…'));
  $('imp-att-go').addEventListener('click', (e) => guardBtn(e.currentTarget, () => doImportAtt(), '导入中…'));
}

// ── PDF tab：批量上传（全流水线 / 解析+编译 / 仅解析） ──
async function doImportPdf() {
  const inp = $('imp-pdf-input');
  const files = [...inp.files];
  if (!files.length) return;
  const bad = files.filter(f => !f.name.toLowerCase().endsWith('.pdf'));
  if (bad.length) { impOut('imp-pdf-result', impErr({ message: '请选择 PDF 文件：' + bad.map(f => f.name).join('、') })); return; }
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  fd.append('mode', $('imp-pdf-mode').value || 'parse_compile');
  impOut('imp-pdf-result', '<div class="imp-msg">⏳ 正在提交 ' + files.length + ' 个 PDF…</div>');
  try {
    let r = await api('/api/papers/batch', { method: 'POST', body: fd });
    // 2026-09-12（用户拍板）：同一 PDF 重复导入 → 检测提醒；用户坚持 → 覆盖重解析
    const dups = r.duplicates || [];
    if (dups.length) {
      const names = dups.map(d => '· ' + shortTitle(d.filename, 40)
        + (d.doc_title ? '（' + shortTitle(d.doc_title, 30) + '）' : '')
        + ' 已导入，状态：' + d.status).join('\n');
      const ok = await askConfirm(
        `以下 ${dups.length} 个 PDF 之前已导入：\n\n${names}\n\n`
        + '继续将**覆盖**之前的解析/编译结果（旧记录与文献会话会被替换，'
        + '知识库笔记与卡片保留并随重编译更新）。是否覆盖？', { title: '重复导入' });
      if (ok) {
        const dupNames = new Set(dups.map(d => d.filename));
        const fd2 = new FormData();
        files.filter(f => dupNames.has(f.name)).forEach(f => fd2.append('files', f));
        fd2.append('mode', $('imp-pdf-mode').value || 'parse_compile');
        fd2.append('overwrite', 'true');
        impOut('imp-pdf-result', '<div class="imp-msg">⏳ 正在覆盖重解析 ' + dupNames.size + ' 个 PDF…</div>');
        const r2 = await api('/api/papers/batch', { method: 'POST', body: fd2 });
        r = { processed: (r.processed || []).concat(r2.processed || []),
              skipped: (r.skipped || []).filter(x => !dupNames.has(x.filename))
                        .concat(r2.skipped || []),
              duplicates: [] };
      } else {
        // 用户放弃覆盖：skipped 里已带"待确认是否覆盖重解析"的原因，原样汇报
        r.skipped = r.skipped || [];
      }
    }
    const lines = [];
    r.processed.forEach(x => lines.push('✅ ' + x.filename + ' → ' + (x.note || '已提交处理')));
    r.skipped.forEach(x => lines.push('⏭ ' + x.filename + ' → ' + x.reason));
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                  message: '批量上传完成：处理 ' + r.processed.length + ' 篇，跳过 ' + r.skipped.length + ' 篇' });
    impOut('imp-pdf-result',
      '<div class="imp-ok">✅ 已提交处理 ' + r.processed.length + ' 篇' + (r.skipped.length ? '，跳过 ' + r.skipped.length + ' 篇' : '') + '</div>' +
      lines.map(l => '<div class="imp-line">' + escapeHtml(l) + '</div>').join(''));
    toast(`已提交 ${r.processed.length} 篇 PDF` + (r.skipped.length ? `（跳过 ${r.skipped.length} 篇）` : ''), 'success');
    inp.value = ''; $('imp-pdf-go').disabled = true;
    setSideTab('papers');
    await loadPapers();
    loadKbList();
  } catch (e) { impOut('imp-pdf-result', impErr(e)); toast('PDF 提交失败：' + e.message, 'error'); }
}

// ── Markdown tab：官方 md 直接进知识库 ──
async function doImportMd() {
  const inp = $('imp-md-input');
  const f = inp.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  impOut('imp-md-result', '<div class="imp-msg">⏳ 正在导入 ' + escapeHtml(f.name) + ' …</div>');
  try {
    const r = await api('/api/kb-meta/markdown/upload', { method: 'POST', body: fd });
    const sync = r.sync || {};
    const v = sync.verify;
    const parts = ['✅ md 导入成功：' + (r.doi || ''),
      '标题：' + (r.title || ''),
      '段落 ' + r.paragraphs + ' · kb 复制：' + ((sync.copied || []).join(', ') || '（无）')];
    if (sync.skipped && sync.skipped.length) parts.push('冻结跳过（已存在）：' + sync.skipped.join(', '));
    if (v) parts.push('一致性校验：' + (v.ok ? '✅ 一致 (' + v.expected_count + ' 段)' : '❌ 不一致'));
    if (r.queued && r.queued.error) parts.push('⚠ 未入队：' + r.queued.error);
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'kb',
                  message: 'md 导入：' + (r.doi || '') });
    impOut('imp-md-result', '<div class="imp-ok">' + parts.map(escapeHtml).join('<br>') + '</div>');
    toast('md 导入成功' + (r.doi ? '：' + r.doi : ''), 'success');
    inp.value = ''; $('imp-md-go').disabled = true;
    loadKbList();
    loadPapers();
  } catch (e) { impOut('imp-md-result', impErr(e)); toast('md 导入失败：' + e.message, 'error'); }
}

// ── Bib tab：/api/kb-meta/bib/upload（preview + import）──
async function doImportBib() {
  const inp = $('imp-bib-input');
  const f = inp.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  impOut('imp-bib-result', '<div class="imp-msg">⏳ 正在解析并导入 ' + escapeHtml(f.name) + ' …</div>');
  try {
    const r = await api('/api/kb-meta/bib/upload', { method: 'POST', body: fd });
    const p = r.preview || {};
    const warn = [];
    const noDoi = (p.no_doi || []).length;
    if (noDoi) warn.push('⚠ ' + noDoi + ' 条无 DOI，已跳过' + (r.skipped_no_doi != null ? '（跳过 ' + r.skipped_no_doi + ' 条）' : ''));
    if ((p.doi_dups || []).length) warn.push('⚠ ' + p.doi_dups.length + ' 条 DOI 已存在（按更新处理）');
    if ((p.missing_fields || []).length) warn.push('⚠ ' + p.missing_fields.length + ' 条字段缺失（title/abstract/journal/year/authors）');
    const html = [
      '<div class="imp-ok">✅ bib 导入完成：新增 ' + (r.imported || 0) + ' · 更新 ' + (r.updated || 0) + ' · 引用 ' + (r.citations || 0) + '</div>',
      '<div class="imp-meta">共 ' + (p.records || 0) + ' 条记录 · 有效 ' + (p.valid || 0) + '</div>',
      warn.length ? '<div class="imp-warn">' + warn.map(escapeHtml).join('<br>') + '</div>' : '',
      (p.sample && p.sample.length)
        ? '<div class="imp-preview"><div class="imp-preview-title">预览（前 ' + Math.min(5, p.sample.length) + ' 条）：</div>' +
          p.sample.slice(0, 5).map(s => '<div class="imp-line">' + escapeHtml((s.title || s.doi || '') + (s.journal ? ' — ' + s.journal : '') + (s.year ? ' (' + s.year + ')' : '')) + '</div>').join('') + '</div>'
        : '',
    ].join('');
    impOut('imp-bib-result', html);
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'kb',
                  message: 'bib 导入：新增 ' + (r.imported || 0) + ' 更新 ' + (r.updated || 0) + ' 引用 ' + (r.citations || 0) });
    toast('bib 导入完成：新增 ' + (r.imported || 0) + ' · 更新 ' + (r.updated || 0), 'success');
    inp.value = ''; $('imp-bib-go').disabled = true;
    loadKbList();
    loadPapers();
  } catch (e) { impOut('imp-bib-result', impErr(e)); toast('bib 导入失败：' + e.message, 'error'); }
}

// ── 📎 tab：依附资料（支撑信息/审稿意见/数据）轻量导入（F5，0 API 成本）──
async function doImportAtt() {
  const inp = $('imp-att-input');
  const files = [...inp.files];
  if (!files.length) return;
  const rid = $('imp-att-parent').value || '';
  const kind = $('imp-att-kind').value || 'data';
  impOut('imp-att-result', '<div class="imp-msg">⏳ 正在导入 ' + files.length + ' 个附件…</div>');
  const lines = [];
  let ok = 0, bad = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append('file', f);
    try {
      const r = await api('/api/kb-meta/attachments/import?rid=' + encodeURIComponent(rid)
        + '&kind=' + encodeURIComponent(kind) + '&index=true', { method: 'POST', body: fd });
      ok += 1;
      lines.push('✅ ' + f.name + ' → ' + (r.parented ? '' : '独立根 ') + r.path
        + (r.indexed ? '（已进检索索引）' : '（未索引：非文本或抽取失败）'));
    } catch (e) {
      bad += 1;
      lines.push('⚠ ' + f.name + ' → ' + (e.message || String(e)));
    }
  }
  appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'kb',
                message: '附件导入：成功 ' + ok + ' 个' + (bad ? '，失败 ' + bad : '') });
  impOut('imp-att-result',
    '<div class="' + (bad ? 'imp-warn' : 'imp-ok') + '">' + (bad ? '⚠ ' : '✅ ')
    + '完成 ' + ok + ' 个' + (bad ? '，失败 ' + bad + ' 个' : '') + '</div>'
    + lines.map(l => '<div class="imp-line">' + escapeHtml(l) + '</div>').join(''));
  toast('附件导入：成功 ' + ok + ' 个' + (bad ? '，失败 ' + bad : ''), bad ? 'warning' : 'success');
  inp.value = ''; $('imp-att-go').disabled = true;
  loadPapers();
  loadKbList();
}

/* ══════════ 右栏阅读器（F4 图片 + F6 卡片管理）══════════ */
// O批-5：标签明确对应知识库产物 —— 原文=en.md / 双语对照=en_zh.md(英上中下) /
// 中文=zh.md / 图片=images/ / 笔记(_note,L1) / 深度(_wiki,L2) / 概念关系(_relations,L3) / PDF=source.pdf
const KB_FILES = [
  { name: 'en.md', label: '📄 原文' },
  { name: 'source.pdf', label: '📄 PDF' },
  { name: 'en_zh.md', label: '🌐 双语对照' },
  { name: 'zh.md', label: '🇨🇳 中文' },
  { name: '__images__', label: '🖼 图片' },
  { name: '_note.md', label: '📝 笔记(L1)' },
  { name: '_wiki.md', label: '📑 深度(L2)' },
  { name: '_relations.md', label: '🔗 概念关系(L3)' },
];
const readerState = { dir: null, file: null, content: '', editing: false, source: 'kb', libFile: null, paperId: null };
let kbFileSetCache = {};   // dir -> Set(顶层文件名)（存在性过滤用，缓存整棵目录树一次）

/* 2026-09-12 修复（用户实测报障）：缓存**只写不失效** ⇒ 刚编译出的 `_note.md` 永远不出标签页
   （导入/编译流程会在编译完成前就渲染过该目录的标签）。失效触发点见：
   ① SSE `compile_done`/`file_saved`/`task_done`（`maybeRefreshKbFiles`）② 打开的目标文件不在
   缓存集合里（`showReaderFolder` 强制重取一次）③ 知识库写操作（同步/保存）。 */
function invalidateKbFileSet(dir) {
  if (dir) delete kbFileSetCache[dir];
  else kbFileSetCache = {};
}

async function ensureKbFileSet(dir, force) {
  if (!dir) return null;
  if (!force && kbFileSetCache[dir]) return kbFileSetCache[dir];
  try {
    const r = await api('/api/kb/tree');
    const folder = (r.folders || []).find(f => f.doi_dir === dir);
    const s = new Set((folder ? folder.files : []).map(f => f.name));
    kbFileSetCache[dir] = s;
    return s;
  } catch (e) { return null; }   // 失败不写缓存（下次自动重试）
}

function kbTabOrder() {
  try { return JSON.parse(localStorage.getItem('kb-tab-order') || '[]'); }
  catch (e) { return []; }
}
function kbTabHidden() {
  try { return new Set(JSON.parse(localStorage.getItem('kb-tab-hidden') || '[]')); }
  catch (e) { return new Set(); }
}

/* ══════════ G12：右槽多阅读面板（tab 化，槽位+浮动模型）══════════ */
const readerPanels = [];   // {id, paperId, source, file, libFile}
let activeReaderPanel = 0;

function ensureReaderPanel() {
  if (!readerPanels.length) {
    readerPanels.push({ id: 'rp-default', paperId: null, source: 'kb', file: '_note.md', libFile: null });
  }
}

function renderReaderPanelTabs() {
  const bar = $('reader-panel-tabs');
  if (!bar) return;
  if (readerPanels.length <= 1) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  bar.innerHTML = readerPanels.map((p, i) => {
    const title = (getPaper(p.paperId) || {}).title || p.paperId || '未选';
    return `<div class="rp-tab ${i === activeReaderPanel ? 'active' : ''}" data-rp="${i}">
      <span>📄 ${escapeHtml(shortTitle(title, 12))}</span>
      <button class="rp-close" data-rp="${i}" title="关闭面板">✕</button>
    </div>`;
  }).join('');
  bar.querySelectorAll('.rp-tab').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.rp-close')) return;
      activateReaderPanel(Number(el.dataset.rp));
    });
  });
  bar.querySelectorAll('.rp-close').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      closeReaderPanel(Number(btn.dataset.rp));
    });
  });
}

function activateReaderPanel(i) {
  ensureReaderPanel();
  if (i < 0 || i >= readerPanels.length) i = 0;
  activeReaderPanel = i;
  const p = readerPanels[i];
  readerState.paperId = p.paperId;
  readerState.source = p.source;
  readerState.file = p.file;
  readerState.libFile = p.libFile;
  renderReaderPanelTabs();
  if (p.source === 'lib') {
    loadLibraryIntoDocked();
  } else if (p.paperId) {
    renderPaperReader(p.paperId);
  } else {
    $('reader-body').innerHTML = '<div class="empty">选中文献后在此预览笔记/原文</div>';
    $('kb-tabs').style.display = 'none';
  }
}

function syncActiveReaderPanel() {
  ensureReaderPanel();
  const p = readerPanels[activeReaderPanel] || readerPanels[0];
  activeReaderPanel = readerPanels.indexOf(p);
  p.paperId = readerState.paperId;
  p.source = readerState.source;
  p.file = readerState.file;
  p.libFile = readerState.libFile;
}

function closeReaderPanel(i) {
  if (readerPanels.length <= 1) return; // 保留最后一个面板
  readerPanels.splice(i, 1);
  if (activeReaderPanel >= readerPanels.length) activeReaderPanel = readerPanels.length - 1;
  activateReaderPanel(activeReaderPanel);
}

/* G15：浮动阅读窗拖回桌面 → 贴合并排（由 dockFloatToDesk 处理，见阅读面板管理区） */

function renderPaperReader(paperId) {
  // T04/G12：解析库来源 → 直接浏览解析/翻译全部产物
  if (readerState.source === 'lib') { loadLibraryIntoDocked(); return; }
  api(`/api/kb/paper/${paperId}/folder`).then(r => {
    if (!r.doi_dir) { $('reader-body').innerHTML = '<div class="empty">知识库未生成（翻译完成后自动生成，可切「解析库」浏览原文）</div>'; return; }
    showReaderFolder(r.doi_dir, '_note.md');
  }).catch(() => {
    $('reader-body').innerHTML = '<div class="empty">阅读器不可用</div>';
  });
}

/* ══════════ T04：停靠阅读器来源切换（知识库 / 解析库）══════════ */
function setReaderSource(src) {
  readerState.source = src;
  document.querySelectorAll('#reader-source .src-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.src === src));
  syncActiveReaderPanel();
  if (src === 'lib') {
    loadLibraryIntoDocked();
  } else {
    $('kb-tabs').style.display = 'flex';
    if (readerState.dir) { renderKbTabs(readerState.dir, readerState.file); loadReaderFile(readerState.dir, readerState.file); }
    else if (readerState.paperId) renderPaperReader(readerState.paperId);
  }
}

async function loadLibraryIntoDocked() {
  const pid = readerState.paperId || state.currentPaperId;
  const tabs = $('kb-tabs');
  const box = $('reader-body');
  if (!pid) { box.innerHTML = '<div class="empty">先选择一篇论文</div>'; return; }
  try {
    const r = await api(`/api/papers/${pid}/files`);
    const files = r.files || [];
    if (!files.length) { tabs.style.display = 'none'; box.innerHTML = '<div class="empty">解析库为空（解析完成后生成）</div>'; return; }
    // G6：只显示 <DOI>/ 顶层文件（不递归子目录）；图片集中到"图片"标签
    const top = files.filter(f => f.path.split('/').length === 2);
    const images = files.filter(f => isLibImage(f.path));
    if (!top.length) {
      // 顶层无文件：只有图片也给出图片标签；否则空提示
      if (images.length) {
        tabs.style.display = 'flex';
        tabs.innerHTML = `<button class="kb-tab ${readerState.libFile === '__images__' ? 'active' : ''}" data-libpath="__images__">🖼 图片 (${images.length})</button>`;
        tabs.querySelector('button[data-libpath]').addEventListener('click', () => {
          readerState.libFile = '__images__';
          loadLibraryFile('__images__');
        });
        readerState.libFile = '__images__';
        loadLibraryFile('__images__');
        return;
      }
      tabs.style.display = 'none';
      box.innerHTML = '<div class="empty">解析库为空（解析完成后生成）</div>';
      return;
    }
    const entries = [...top];
    if (images.length) entries.push({ path: '__images__', name: '图片' });
    tabs.style.display = 'flex';
    tabs.innerHTML = entries.map(f => {
      const isImgTab = f.path === '__images__';
      return `<button class="kb-tab ${f.path === (readerState.libFile || '') ? 'active' : ''}" data-libpath="${isImgTab ? '__images__' : escapeHtml(f.path)}" title="${isImgTab ? '' : escapeHtml(f.path)}">${isImgTab ? '🖼 图片 (' + images.length + ')' : escapeHtml(shortTitle(f.path.split('/')[1] || f.name, 18))}</button>`;
    }).join('');
    tabs.querySelectorAll('button[data-libpath]').forEach(el => {
      el.addEventListener('click', () => { readerState.libFile = el.dataset.libpath; loadLibraryFile(el.dataset.libpath); });
    });
    const first = top.find(f => /\.md$/i.test(f.path)) || top[0];
    readerState.libFile = readerState.libFile && top.some(f => f.path === readerState.libFile) ? readerState.libFile : first.path;
    loadLibraryFile(readerState.libFile);
  } catch (e) { box.innerHTML = '<div class="empty">加载失败：' + escapeHtml(e.message) + '</div>'; }
}

function isLibImage(path) {
  const parts = path.split('/');
  return parts.length === 3 && parts[1] === 'images' && /\.(png|jpe?g|gif|webp|svg)$/i.test(parts[2]);
}

async function loadLibraryFile(path) {
  const box = $('reader-body');
  box.innerHTML = '<div class="empty">加载中…</div>';
  // G6：图片集中标签
  if (path === '__images__') {
    const pid = state.currentPaperId;
    try {
      const r = await api(`/api/papers/${pid}/files`);
      const imgs = (r.files || []).filter(f => isLibImage(f.path));
      if (!imgs.length) { box.innerHTML = '<div class="empty">该文献没有图片</div>'; return; }
      box.innerHTML = `<div class="img-grid">` + imgs.map(im =>
        `<figure class="img-cell"><img src="/api/library/image?path=${encodeURIComponent(im.path)}" alt="" loading="lazy"><figcaption>${escapeHtml(im.name)}</figcaption></figure>`).join('') + `</div>`;
      bindLightbox();
      return;
    } catch (e) { box.innerHTML = '<div class="empty">加载失败：' + escapeHtml(e.message) + '</div>'; return; }
  }
  const isImg = /\.(png|jpe?g|gif|webp|svg)$/i.test(path);
  try {
    if (isImg) {
      box.innerHTML = `<div class="img-grid"><figure class="img-cell"><img src="/api/library/image?path=${encodeURIComponent(path)}" alt="" loading="lazy"><figcaption>${escapeHtml(path.split('/').pop())}</figcaption></figure></div>`;
      bindLightbox();
      return;
    }
    const r = await api(`/api/library/file?path=${encodeURIComponent(path)}`);
    const folder = path.split('/')[0]; // library/<DOI>/
    box.innerHTML = renderMarkdown(r.content, folder, 'lib');
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(path)} 不可用：${escapeHtml(e.message)}</div>`;
  }
}

function showReaderFolder(dir, file) {
  readerState.dir = dir;
  readerState.file = file;
  readerState.editing = false;
  $('reader-title').textContent = dir;
  renderReaderActions();
  renderKbTabs(dir, file);
  loadReaderFile(dir, file);
  ensureKbFileSet(dir).then(set => {
    if (readerState.dir !== dir) return;
    // 目标文件不在缓存集合里 ⇒ 缓存可能过旧（刚编译/刚保存）→ **强制重取一次**再渲染
    if (set && file && file !== '__images__' && !set.has(file)) {
      return ensureKbFileSet(dir, true).then(() => {
        if (readerState.dir === dir) renderKbTabs(dir, readerState.file);
      });
    }
    renderKbTabs(dir, readerState.file);
  });
}

function renderKbTabs(dir, active) {
  const hidden = kbTabHidden();
  const order = kbTabOrder();
  let tabs = KB_FILES.filter(f => !hidden.has(f.name));
  // O批-4：存在性过滤——已知目录文件集时隐藏缺失文件的标签（图片/未加载时兜底全显）
  if (kbFileSetCache[dir]) {
    tabs = tabs.filter(f => f.name === '__images__' || kbFileSetCache[dir].has(f.name));
  }
  // 用户自定义顺序优先（未在 order 中的保持默认相对位置）
  tabs.sort((a, b) => {
    const ia = order.indexOf(a.name), ib = order.indexOf(b.name);
    if (ia >= 0 && ib >= 0) return ia - ib;
    if (ia >= 0) return -1;
    if (ib >= 0) return 1;
    return KB_FILES.indexOf(a) - KB_FILES.indexOf(b);
  });
  $('kb-tabs').style.display = 'flex';
  $('kb-tabs').innerHTML = tabs.map(f =>
    `<button class="kb-tab ${f.name === active ? 'active' : ''}" data-file="${f.name}"
       draggable="true" title="拖动排序">${f.label}</button>`).join('');
  $('kb-tabs').querySelectorAll('.kb-tab').forEach(el => {
    el.addEventListener('click', () => showReaderFolder(dir, el.dataset.file));
    el.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/plain', el.dataset.file);
      el.classList.add('dragging');
    });
    el.addEventListener('dragend', () => el.classList.remove('dragging'));
    el.addEventListener('dragover', (e) => e.preventDefault());
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      const from = e.dataTransfer.getData('text/plain');
      if (from && from !== el.dataset.file) {
        const order = kbTabOrder().filter(x => x !== from);
        order.splice(order.indexOf(el.dataset.file) < 0 ? order.length : order.indexOf(el.dataset.file), 0, from);
        localStorage.setItem('kb-tab-order', JSON.stringify(order));
        renderKbTabs(dir, active);
      }
    });
  });
}

function renderTabManage(dir, active) {
  const box = $('tab-manage');
  const hidden = kbTabHidden();
  box.innerHTML = KB_FILES.map(f => `
    <label class="tm-item">
      <input type="checkbox" data-tm="${f.name}" ${hidden.has(f.name) ? '' : 'checked'}>
      <span>${f.label}</span>
    </label>`).join('');
  box.style.display = 'block';
  box.querySelectorAll('input[data-tm]').forEach(cb => {
    cb.addEventListener('change', () => {
      const hidden = new Set(kbTabHidden());
      if (cb.checked) hidden.delete(cb.dataset.tm);
      else hidden.add(cb.dataset.tm);
      localStorage.setItem('kb-tab-hidden', JSON.stringify([...hidden]));
      renderKbTabs(dir, active);
    });
  });
}

function bindTabManage() {
  $('tab-manage-btn').addEventListener('click', () => {
    const box = $('tab-manage');
    if (box.style.display === 'block') { box.style.display = 'none'; return; }
    if (readerState.dir) renderTabManage(readerState.dir, readerState.file);
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#tab-manage') && !e.target.closest('#tab-manage-btn')) {
      $('tab-manage').style.display = 'none';
    }
  });
}

function renderReaderActions() {
  const box = $('reader-actions');
  if (readerState.editing) {
    box.innerHTML = `
      <button class="btn btn-sm primary" id="rd-save">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>
        保存</button>
      <button class="btn btn-sm" id="rd-cancel">取消</button>`;
    $('rd-save').addEventListener('click', (e) => guardBtn(e.currentTarget, () => saveReaderEdit(), '保存中…'));
    $('rd-cancel').addEventListener('click', () => {
      readerState.editing = false;
      renderReaderActions();
      $('reader-body').innerHTML = renderMarkdown(readerState.content, readerState.dir);
    });
  } else {
    box.innerHTML = `<button class="btn btn-sm" id="rd-edit" title="编辑该笔记">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>
      编辑</button>`;
    $('rd-edit').addEventListener('click', () => {
      readerState.editing = true;
      renderReaderActions();
      const box = $('reader-body');
      box.innerHTML = `<textarea class="reader-edit" id="rd-text">${escapeHtml(readerState.content)}</textarea>`;
      $('rd-text').focus();
    });
  }
}

async function saveReaderEdit() {
  const content = $('rd-text').value;
  if (!readerState.dir || !readerState.file) return;
  try {
    await api('/api/kb/file', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: readerState.dir + '/' + readerState.file, content }),
    });
    readerState.content = content;
    readerState.editing = false;
    renderReaderActions();
    $('reader-body').innerHTML = renderMarkdown(content, readerState.dir);
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'kb',
                  message: `已保存 ${readerState.file}（插件自动生成将不再覆盖）` });
  } catch (e) { alert('保存失败：' + e.message); }
}

// O批-5(item3)：PDF 原文阅读（pdf.js 连续滚动 + 适合宽度 + 缩放 + Ctrl+滚轮缩放，接近浏览器观感）
function renderPdfViewer(box, doi) {
  box.innerHTML = `<div class="pdf-wrap">
    <div class="pdf-toolbar">
      <button class="btn small" id="pdf-zoom-out" title="缩小">−</button>
      <span class="muted" id="pdf-zoom-ind">—</span>
      <button class="btn small" id="pdf-zoom-in" title="放大">＋</button>
      <button class="btn small" id="pdf-fit" title="适合宽度">🗔 适宽</button>
      <span class="muted" id="pdf-page-ind">加载中…</span>
    </div>
    <div class="pdf-scroll" id="pdf-scroll"></div>
  </div>`;
  if (typeof pdfjsLib === 'undefined') {
    box.innerHTML = '<div class="empty">PDF.js 未加载（vendor/pdfjs 缺失）</div>'; return;
  }
  pdfjsLib.GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.min.js';
  const url = '/api/kb/pdf?doi=' + encodeURIComponent(doi);
  pdfjsLib.getDocument(url).promise.then(pdf => {
    const scroll = $('pdf-scroll');
    let baseScale = 1.0, fitScale = 1.0, zoom = 1.0;
    const pagesEl = [];
    for (let i = 1; i <= pdf.numPages; i++) {
      const wrap = document.createElement('div');
      wrap.className = 'pdf-page';
      const canvas = document.createElement('canvas');
      wrap.appendChild(canvas);
      scroll.appendChild(wrap);
      pagesEl.push({ wrap, canvas, page: null });
    }
    const renderAll = () => {
      const scale = baseScale * zoom;
      pagesEl.forEach((pce, i) => {
        const target = pce.page;
        const vp = target.getViewport({ scale });
        pce.canvas.width = vp.width; pce.canvas.height = vp.height;
        target.render({ canvasContext: pce.canvas.getContext('2d'), viewport: vp });
      });
      const ind = $('pdf-zoom-ind');
      if (ind) ind.textContent = Math.round(scale * 100) + '%';
    };
    // 并行取页（保序渲染）
    Promise.all(Array.from({ length: pdf.numPages }, (_, i) => pdf.getPage(i + 1)))
      .then(pgs => {
        pgs.forEach((pg, i) => { pagesEl[i].page = pg; });
        fitScale = Math.max(0.4, scroll.clientWidth / pgs[0].getViewport({ scale: 1 }).width);
        baseScale = fitScale;
        renderAll();
        const ind = $('pdf-page-ind');
        if (ind) ind.textContent = `共 ${pdf.numPages} 页`;
      });
    const setZoom = (z) => { zoom = Math.min(4, Math.max(0.3, z)); renderAll(); };
    $('pdf-zoom-in').onclick = () => setZoom(zoom + 0.2);
    $('pdf-zoom-out').onclick = () => setZoom(zoom - 0.2);
    $('pdf-fit').onclick = () => { zoom = 1.0; baseScale = fitScale; renderAll(); };
    // Ctrl+滚轮缩放（像浏览器）
    scroll.addEventListener('wheel', (e) => {
      if (e.ctrlKey) { e.preventDefault(); setZoom(zoom + (e.deltaY < 0 ? 0.15 : -0.15)); }
    }, { passive: false });
  }).catch(e => {
    box.innerHTML = '<div class="empty">PDF 加载失败：' + escapeHtml(e.message) + '</div>';
  });
}

async function loadReaderFile(dir, file) {
  const box = $('reader-body');
  box.innerHTML = '<div class="empty">加载中…</div>';
  try {
    if (file === '__images__') {
      // 图片 tab：列出该文献 images/ 下的全部图片
      const r = await api(`/api/kb/images?dir=${encodeURIComponent(dir)}`);
      if (!r.images.length) { box.innerHTML = '<div class="empty">该文献没有图片</div>'; return; }
      box.innerHTML = `<div class="img-grid">` + r.images.map(im =>
        `<figure class="img-cell">
          <img src="/api/kb/image?path=${encodeURIComponent(dir + '/images/' + im.name)}" alt="${escapeHtml(im.name)}" loading="lazy">
          <figcaption>${escapeHtml(im.name)}</figcaption>
        </figure>`).join('') + `</div>`;
      bindLightbox();
      return;
    }
    if (file === 'source.pdf') {
      const p = getPaper(state.currentPaperId);
      if (!p || !p.doi) { box.innerHTML = '<div class="empty">未找到该文献 DOI，无法读 PDF</div>'; return; }
      renderPdfViewer(box, p.doi);
      return;
    }
    const r = await api(`/api/kb/file?path=${encodeURIComponent(dir + '/' + file)}`);
    readerState.content = r.content;
    box.innerHTML = renderMarkdown(r.content, dir);
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(file)} 不可用：${escapeHtml(e.message)}</div>`;
  }
}

/* ══════════ Obsidian 风格：YAML 属性面板 + callout（2026-09-17）══════════
   设计约束：**不改渲染管线顺序**。frontmatter 仍在 marked.parse 之前从原文抽出
   （抽出失败 = 保持旧行为"整块剥离、不渲染"），面板 HTML 直接拼在正文 HTML 最前面，
   因此公式占位符/图片重写/标题锚点等后续环节完全不受影响。 */

// 切分文件开头的 `---` 块 → { raw: 元数据行[], body: 正文 }；非 frontmatter 返回 null
function splitFrontmatter(md) {
  const lines = String(md).split(/\r?\n/);
  if (!lines.length || lines[0].trim() !== '---') return null;
  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    const t = lines[i].trim();
    if (t === '---' || t === '...') { end = i; break; }
  }
  if (end < 0) return null;                     // 没有收尾分隔符 → 不是 frontmatter
  return { raw: lines.slice(1, end), body: lines.slice(end + 1).join('\n') };
}

function fmUnquote(v) {
  const s = String(v).trim();
  if (s.length >= 2) {
    const q = s[0];
    if ((q === '"' || q === "'") && s[s.length - 1] === q) {
      let inner = s.slice(1, -1);
      if (q === '"') inner = inner.replace(/\\"/g, '"').replace(/\\n/g, '\n').replace(/\\\\/g, '\\');
      else inner = inner.replace(/''/g, "'");
      return inner;
    }
  }
  return s;
}

// 单个字段的值解析：标量 / 流式数组 / 块状列表（key: 换行 + "- item"）/ 块标量 / 空值
// 返回 { values: string[], isList: bool, consumed: number }；空值 → values=[] （不出行）
function fmParseValue(rawVal, rawLines, idx) {
  const val = String(rawVal).trim();
  if (!val) {
    const items = [];
    let k = idx + 1;
    while (k < rawLines.length) {
      const li = /^\s*[-*]\s+(.*)$/.exec(rawLines[k]);
      if (!li) break;
      items.push(fmUnquote(li[1].trim()));
      k++;
    }
    if (items.length) return { values: items, isList: true, consumed: k - idx - 1 };
    return { values: [], isList: false, consumed: 0 };
  }
  if (val === '|' || val === '>') {             // 块标量：吃掉后续缩进行
    const buf = [];
    let k = idx + 1;
    while (k < rawLines.length && (!rawLines[k].trim() || /^\s+\S/.test(rawLines[k]))) {
      buf.push(rawLines[k].replace(/^\s{1,4}/, ''));
      k++;
    }
    while (buf.length && !buf[buf.length - 1].trim()) buf.pop();
    return { values: [buf.join(' ').trim()], isList: false, consumed: k - idx - 1 };
  }
  if (val.startsWith('[') && val.endsWith(']')) {
    const inner = val.slice(1, -1).trim();
    let arr = null;
    try { arr = JSON.parse(val); } catch (e) { /* 非严格 JSON：tags: [paper, xxx] 裸值数组 */ }
    if (!Array.isArray(arr)) arr = inner ? inner.split(',').map(s => fmUnquote(s.trim())) : [];
    return { values: arr.map(x => (x === null || x === undefined) ? '' : String(x)).filter(s => s !== ''),
             isList: true, consumed: 0 };
  }
  return { values: [fmUnquote(val)], isList: false, consumed: 0 };
}

function parseFrontmatterEntries(rawLines) {
  const out = [];
  for (let i = 0; i < rawLines.length; i++) {
    const line = rawLines[i];
    if (!line.trim() || /^\s*#/.test(line)) continue;
    if (/^\s*[-*]\s+/.test(line)) continue;                  // 已被上一字段消费的列表项
    const m = /^([^:\s][^:]{0,63}?)\s*:\s*(.*)$/.exec(line);
    if (!m) continue;
    const { values, isList, consumed } = fmParseValue(m[2], rawLines, i);
    i += consumed;
    const clean = values.map(v => String(v).trim()).filter(v => v !== '');
    if (!clean.length) continue;                             // 空字段不出行（与新模板契约一致）
    out.push({ key: fmUnquote(m[1]), values: clean, isList });
  }
  return out;
}

const FM_DOI_RE = /^10\.\d{4,9}\/\S+$/;

function fmRenderValue(v) {
  const s = String(v);
  if (FM_DOI_RE.test(s)) {
    return `<a class="prop-link" href="https://doi.org/${escapeHtml(s)}" target="_blank" rel="noopener">${escapeHtml(s)}</a>`;
  }
  if (/^https?:\/\/[^\s]+$/i.test(s)) {
    return `<a class="prop-link" href="${escapeHtml(s)}" target="_blank" rel="noopener">${escapeHtml(s)}</a>`;
  }
  return escapeHtml(s);
}

function fmValueLines(v) {
  // 标量字段**原样一行**：`通讯作者: "A；B"` / `研究单位: "X；Y"` 在 YAML 里是**一个字符串**
  // （分号只是人读分隔），Obsidian 同样按单值显示。曾按分号拆成多行 ⇒ 视觉好看但
  // `textContent` 拼接后变成 "AB"（选取/复制粘连，真实文件实测发现）⇒ 不拆，保留原分隔符。
  return [String(v)];
}

function renderPropPanel(entries) {
  if (!entries || !entries.length) return '';
  const rows = entries.map(e => {
    let valHtml;
    if (e.isList) {
      valHtml = '<span class="prop-chips">' + e.values.map(v =>
        `<span class="prop-chip">${fmRenderValue(v)}</span>`).join('') + '</span>';
    } else {
      const lines = e.values.reduce((acc, v) => acc.concat(fmValueLines(v)), []);
      valHtml = lines.map(l => `<span class="prop-line">${fmRenderValue(l)}</span>`).join('');
    }
    return `<div class="prop-row"><div class="prop-key" title="${escapeHtml(e.key)}">${escapeHtml(e.key)}</div>`
         + `<div class="prop-val">${valHtml}</div></div>`;
  }).join('');
  return `<div class="md-props"><div class="props-head">属性</div><div class="props-table">${rows}</div></div>`;
}

const CALLOUT_DEFAULT_TITLE = { info: 'Info', summary: 'Summary', warning: 'Warning', note: 'Note', tip: 'Tip' };

// 旧文件残留的 Obsidian callout：`> [!type] 标题` 起始的连续引用块 → 容器 HTML。
// 逐行预处理（不解析 marked 输出）：注入的原始 HTML 块用空行与正文隔开，
// marked 的 HTML 块遇空行即结束 ⇒ 块内正文仍按正常 markdown 流程渲染（列表/链接/公式都有效）。
function transformCallouts(md) {
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

function renderMarkdown(md, baseDir, source) {
  if (typeof marked === 'undefined') return `<pre>${escapeHtml(md)}</pre>`;
  md = String(md);
  // Obsidian 风格属性面板：文件开头 `---` 块是元数据而非正文。抽出原文 → 面板渲染；
  // 解析失败/无 frontmatter 时退回旧行为（不报错、不把 `---` 泄漏到正文）。
  let propsHtml = '';
  const fm = splitFrontmatter(md.replace(/^\uFEFF/, ''));
  if (fm) {
    md = fm.body;
    propsHtml = renderPropPanel(parseFrontmatterEntries(fm.raw));
  }
  // 旧文件残留 callout（新模板不再生成，历史文件仍需好看）：预处理成容器 HTML
  md = transformCallouts(md).md;
  // P2-C：兜底清理 MinerU 图片占位符（`<!-- image -->` / `<!-- image-1 -->` 等），
  // 避免残留注释让图片行缺失；后端已清洗，这里双保险（含已被翻译写进正文的旧产物）
  md = md.replace(/<!--\s*(?:image|img)[\s\-_]*\d*\s*-->/gi, '');
  // 还原被转义的常见内联格式标签（解析管线可能以 HTML 实体形式写入 en.md，
  // 如 &lt;del&gt; / &lt;br&gt; / &lt;em&gt; / &lt;sup&gt; 等——marked 会把它们当字面文本显示，
  // 还原成真标签才能正确渲染）
  md = md.replace(/&lt;(\/?(?:del|em|br|sup|sub|i|b|mark|u))&gt;/g, '<$1>');

  // ★ 公式/代码保护（关键修复）：LaTeX 里的 `_`(下标)/`~`(间距)/换行 会被 marked 当 markdown
  //   语法误处理（_→<em>、~~→<del>、\n→<br>），把公式切开（如 O_{3}H → O</em>3H）。
  //   先抽成占位符让 marked 原样通过，再还原为 KaTeX / 高亮代码。
  const prot = [];
  const ph = (kind, lang, payload) => {
    const id = `@@M${prot.length}@@`;
    prot.push({ kind, lang, payload });
    return id;
  };
  // 1) 围栏代码块（含语言；先保护，避免其内部 $...$ 被当公式）
  md = md.replace(/```([\w+-]*)\s*\r?\n([\s\S]*?)```/g, (m, lang, code) => ph('code', lang || '', code));
  // 2) 块级公式 $$...$$（可多行，含 \tag 编号）
  md = md.replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => ph('math', 'block', tex));
  // 3) 行内公式 $...$（不含换行）
  md = md.replace(/\$([^$\n]+?)\$/g, (m, tex) => {
    // MinerU 伪公式：把 HTML 上标/下标包进 $…$（如 $<sup>[1]</sup>$、$<sup>[2–4]</sup>$）。
    // 内含 <sup>/<sub> 且无真 LaTeX 特征字符（\ ^ _ { }）→ 脱去 $ 还原成裸标签，
    // 交 marked 当内联 HTML 渲染（否则被当公式送 KaTeX，标签被转义成可见文本，用户报"无法渲染"）。
    if (/<\/?su[bp]>/i.test(tex) && !/[\\^_{}]/.test(tex)) return tex;
    return ph('math', 'inline', tex);
  });

  // ★2026-09-23（用户报障"原文上标显示成字面 ^{[34]}"）：英文原文里的引用上标是**裸的** `^{[34]}`
  //   （en.md 全篇如此；中文变体由后端包成 `$^{[34]}$`），而渲染只把 `$...$` 交 KaTeX
  //   ⇒ 裸上标只能当普通文字显示（字面出现 ^{[34]}）。
  //   位置很关键：**放在公式保护之后**——此时 `$...$`/```代码``` 都已换成占位符，
  //   剩下的 `^{...}` 必定在公式外，直接按行内公式送 KaTeX（与中文变体渲染同形）。
  //   只认"引用 [n] / 纯数字符号"，不碰正文的 `^{文字}` 与 Obsidian 内联脚注 `^[注]`。
  md = md.replace(/(?<!\$)([\^_])\{([^{}\n]{1,40})\}(?!\$)/g, (m, sign, body) => {
    const t = body.trim();
    const likeCite = /^\[[\d,;\s\u2013\u2014-]+\]$/.test(t);
    const likeNum = /^[\d+-]{1,6}$/.test(t);
    if (!likeCite && !likeNum) return m;
    return ph('math', 'inline', sign + '{' + t + '}');
  });

  let html = marked.parse(md, { breaks: true, gfm: true });

  // 还原保护块：公式 → KaTeX（容错）；代码 → <pre><code> + hljs 高亮
  html = html.replace(/@@M(\d+)@@/g, (m, idx) => {
    const it = prot[+idx];
    if (!it) return m;
    if (it.kind === 'math') {
      if (typeof katex === 'undefined') return `$${it.payload}$`;
      try {
        return katex.renderToString(it.payload, {
          displayMode: it.lang === 'block', throwOnError: false });
      } catch (e) { return `$${it.payload}$`; }
    }
    // code：直接对原文高亮（不再走解码转义正则）
    let code = it.payload;
    if (typeof hljs !== 'undefined') {
      try {
        code = it.lang ? hljs.highlight(code, { language: it.lang }).value
                       : hljs.highlightAuto(code).value;
      } catch (e) { /* 高亮失败保持原文 */ }
    }
    const cls = it.lang ? ` class="hljs language-${it.lang}"` : ' class="hljs"';
    return `<pre><code${cls}>${code}</code></pre>`;
  });

  // Obsidian 双链 [[name|alias]] → 可点击（U4）
  html = html.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (m, name, alias) => {
    const clean = name.replace(/\.md$/, '');
    return `<a class="wikilink" title="Obsidian 双链: ${escapeHtml(clean)}">${escapeHtml(alias || clean)}</a>`;
  });

  // 标题锚点（U4）
  html = html.replace(/<h([1-4])>(.*?)<\/h\1>/g, (m, lv, content) => {
    const id = 'h-' + content.replace(/<[^>]+>/g, '').replace(/[^\w\u4e00-\u9fff-]+/g, '-').toLowerCase().slice(0, 40);
    return `<h${lv} id="${id}">${content}</h${lv}>`;
  });

  // 图片路径重写（F4/T04）：相对 images/xxx → 按来源重写
  //   source='lib' → /api/library/image?path=<库目录>/images/xxx
  //   source 默认 kb → /api/kb/image?path=<kb目录>/images/xxx
  if (baseDir) {
    const apiBase = source === 'lib' ? '/api/library/image' : '/api/kb/image';
    html = html.replace(/<img src="([^"]+)"([^>]*)>/g, (m, src, rest) => {
      if (/^(https?:|data:|\/)/.test(src)) return m;
      const clean = src.replace(/^\.\//, '');
      if (clean.startsWith('images/')) {
        return `<img src="${apiBase}?path=${encodeURIComponent(baseDir + '/' + clean)}"${rest}>`;
      }
      return m;
    });
  }
  return `<div class="md">${propsHtml}${html}</div>`;
}

