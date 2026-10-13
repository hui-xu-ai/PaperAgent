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
  suppressScroll();               // P7：抑制切换瞬间旧内容塌陷产生的伪滚动保存
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
    renderIntoReader(renderMarkdown(r.content, folder, 'lib'));
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

let readerEditor = null;  // EasyMDE 编辑器实例(仅编辑态存在);离开编辑态即销毁

function destroyReaderEditor() {
  if (readerEditor) { try { readerEditor.destroy(); } catch (e) {} readerEditor = null; }
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
      // 取消 = 显式丢弃:连同自动草稿一并清除(草稿仅用于崩溃/误关防丢)
      if (readerEditor) readerEditor.clearAutosave();
      destroyReaderEditor();
      readerState.editing = false;
      renderReaderActions();
      renderIntoReader(renderMarkdown(readerState.content, readerState.dir));
    });
  } else {
    box.innerHTML = `<button class="btn btn-sm${tocState.open ? ' primary' : ''}" id="rd-toc" title="目录大纲">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
      目录</button>
    <button class="btn btn-sm" id="rd-edit" title="编辑该笔记">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>
      编辑</button>`;
    $('rd-toc').addEventListener('click', () => toggleReaderToc());
    $('rd-edit').addEventListener('click', () => {
      closeReaderToc();
      readerState.editing = true;
      renderReaderActions();
      mountReaderEditor();
    });
  }
}

// 挂载 EasyMDE 编辑器(经 MDKit.createEditor):预览接阅读态同一渲染管线,支持 Ctrl+S 保存、
// 分屏预览、编辑态高亮、按文档隔离的自动草稿。MDKit 未就绪时兜底为裸 textarea。
function mountReaderEditor() {
  const body = $('reader-body');
  body.innerHTML = `<textarea id="rd-text"></textarea>`;
  const ta = $('rd-text');
  const mk = window.MDKit;
  if (!mk || !mk.createEditor) {
    ta.className = 'reader-edit';
    ta.value = readerState.content;
    ta.focus();
    readerEditor = { getValue: () => ta.value, clearAutosave: () => {}, destroy: () => {} };
    return;
  }
  readerEditor = mk.createEditor({
    element: ta,
    initialValue: readerState.content,
    baseDir: readerState.dir,
    source: 'kb',
    autosaveId: `reader-edit:${readerState.dir}/${readerState.file}`,
    minHeight: '300px',
    onSave: (content) => saveReaderEdit(content),
  });
  readerEditor.focus();
}

async function saveReaderEdit(content) {
  if (content == null) content = readerEditor ? readerEditor.getValue() : ($('rd-text') ? $('rd-text').value : '');
  if (!readerState.dir || !readerState.file) return;
  try {
    await api('/api/kb/file', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: readerState.dir + '/' + readerState.file, content }),
    });
    readerState.content = content;
    if (readerEditor) readerEditor.clearAutosave();  // 保存成功 → 清除自动草稿
    destroyReaderEditor();
    readerState.editing = false;
    renderReaderActions();
    renderIntoReader(renderMarkdown(content, readerState.dir));
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'kb',
                  message: `已保存 ${readerState.file}（插件自动生成将不再覆盖）` });
  } catch (e) { alert('保存失败：' + e.message); }
}


/* ══════════ 阅读位置记忆 + 进度条（P7）══════════
   位置按「文档 key」存 localStorage(reader-scroll-pos)，切回同一文档自动定位；
   进度条 = #reader-body 已滚动比例。纯前端、零后端依赖；依赖 readerState/$，属业务，
   不进 mdkit 库。编辑态与 PDF（内层 .pdf-scroll 独立滚动）不记位置。 */
const SCROLL_POS_KEY = 'reader-scroll-pos';
let scrollPosMap = null;         // 懒加载：key -> {y, ts}
let scrollSaveTimer = null;
// 切换文档瞬间抑制保存：showReaderFolder 先把 readerState.file 改成新文档，随后
// loadReaderFile 把旧内容替换为「加载中…」→ 旧内容塌陷使 scrollTop 钳到 0 → 触发一次
// scroll 事件，此时 key 已是新文档 → 会把新文档位置误存为 0（竞态，时好时坏）。
// 加载起点短暂置位，覆盖这次伪滚动即可。
let suppressScrollSave = false;
let suppressScrollTimer = null;
function suppressScroll(ms) {
  suppressScrollSave = true;
  clearTimeout(suppressScrollTimer);
  suppressScrollTimer = setTimeout(() => { suppressScrollSave = false; }, ms || 400);
}

function loadScrollPosMap() {
  if (scrollPosMap) return scrollPosMap;
  try { scrollPosMap = JSON.parse(localStorage.getItem(SCROLL_POS_KEY) || '{}'); }
  catch (e) { scrollPosMap = {}; }
  return scrollPosMap;
}
function persistScrollPos() {
  try {
    const m = loadScrollPosMap();
    const keys = Object.keys(m);
    if (keys.length > 60) {   // 只留最近 60 条，防 localStorage 膨胀
      keys.sort((a, b) => (m[a].ts || 0) - (m[b].ts || 0))
          .slice(0, keys.length - 60).forEach(k => delete m[k]);
    }
    localStorage.setItem(SCROLL_POS_KEY, JSON.stringify(m));
  } catch (e) {}
}
function readerScrollKey() {
  if (readerState.source === 'lib')
    return `lib::${readerState.paperId || state.currentPaperId || ''}::${readerState.libFile || ''}`;
  return `kb::${readerState.dir || ''}::${readerState.file || ''}`;
}
function validScrollKey(key) { return key && !key.endsWith('::') && !key.endsWith('lib::::'); }
function saveReaderScroll() {
  if (readerState.editing || suppressScrollSave) return;   // 编辑态/切换瞬间不覆盖阅读位置
  const box = $('reader-body');
  if (!box) return;
  const key = readerScrollKey();
  if (!validScrollKey(key)) return;
  loadScrollPosMap()[key] = { y: box.scrollTop, ts: Date.now() };
  clearTimeout(scrollSaveTimer);
  scrollSaveTimer = setTimeout(persistScrollPos, 400);
}
function updateReaderProgress() {
  const box = $('reader-body'), bar = $('reader-progress');
  if (!box || !bar || !bar.firstElementChild) return;
  const max = box.scrollHeight - box.clientHeight;
  if (max <= 4) { bar.classList.remove('on'); bar.firstElementChild.style.width = '0'; return; }
  bar.classList.add('on');
  bar.firstElementChild.style.width = Math.min(100, Math.max(0, box.scrollTop / max * 100)).toFixed(2) + '%';
}
// 渲染后恢复位置：图片懒加载会撑高内容 → rAF + 延时二次定位（key 未变才生效）
function restoreReaderScroll(key) {
  const box = $('reader-body');
  if (!box) return;
  const rec = validScrollKey(key) ? loadScrollPosMap()[key] : null;
  if (!rec || !rec.y) { box.scrollTop = 0; updateReaderProgress(); return; }
  const apply = () => { if (readerScrollKey() === key) { box.scrollTop = rec.y; updateReaderProgress(); } };
  apply();
  requestAnimationFrame(apply);
  setTimeout(apply, 350);
}
// 统一渲染出口：写入 HTML 后恢复位置并刷新进度条
function renderIntoReader(html) {
  $('reader-body').innerHTML = html;
  restoreReaderScroll(readerScrollKey());
}
function bindReaderScroll() {
  const box = $('reader-body');
  if (!box || box.dataset.scrollBound) return;
  box.dataset.scrollBound = '1';
  let ticking = false;
  box.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; saveReaderScroll(); updateReaderProgress(); });
  }, { passive: true });
  let rzTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(rzTimer);
    rzTimer = setTimeout(updateReaderProgress, 150);
  });
}


/* ══════════ 阅读态 TOC 大纲(P5a)══════════
   复用渲染时已注入的 h1-h4 锚点 id:MDKit.extractToc 从 reader-body 的 HTML 提取大纲、
   MDKit.renderTocHtml 产出结构;这里只做 DOM 挂载 + 点击滚动 + 滚动高亮(scroll-spy)。
   抽屉绝对定位覆盖在阅读区左缘(#reader 是 position:relative 祖先;top 对齐 reader-body、
   bottom:0)。内容切换(切 tab/文件、编辑保存)由 MutationObserver 自动重建;布局变化
   (tab 显隐/窗口缩放)由 ResizeObserver 重新对齐顶部。仅 docked 阅读面板;进入编辑态即关闭。 */
let tocState = { open: false, el: null, observer: null, resizer: null, spy: null, items: [], headEls: [] };

// 只滚 #reader-body，不用 scrollIntoView——后者会连带滚动所有可滚祖先(含 overflow:hidden 的)，
// 把阅读区头部/标签推出可视区（实测复现：目录跳转后上方工具栏消失）。
function scrollReaderBodyTo(target, margin) {
  const body = $('reader-body');
  if (!body || !target) return;
  const top = target.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop - (margin || 8);
  body.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
}
// 只滚 TOC 抽屉自身（.reader-toc overflow-y:auto），同理避免 scrollIntoView 牵连祖先
function scrollDrawerTo(drawer, item) {
  const er = drawer.getBoundingClientRect(), ar = item.getBoundingClientRect();
  if (ar.top < er.top) drawer.scrollTop += ar.top - er.top - 4;
  else if (ar.bottom > er.bottom) drawer.scrollTop += ar.bottom - er.bottom + 4;
}

function toggleReaderToc() { if (tocState.open) closeReaderToc(); else openReaderToc(); }

function openReaderToc() {
  const reader = $('reader'), body = $('reader-body');
  if (!reader || !body || tocState.open) return;
  const el = document.createElement('div');
  el.className = 'reader-toc';
  el.id = 'reader-toc';
  el.style.top = body.offsetTop + 'px';
  reader.appendChild(el);
  tocState.el = el; tocState.open = true;

  el.addEventListener('click', (e) => {
    if (e.target.closest('.md-toc-close')) { closeReaderToc(); return; }
    const li = e.target.closest('[data-toc-target]');
    if (!li) return;
    const h = body.querySelector('[id="' + li.dataset.tocTarget + '"]');
    if (h) scrollReaderBodyTo(h, 8);
  });

  buildReaderToc();

  tocState.observer = new MutationObserver(() => { if (tocState.open) buildReaderToc(); });
  tocState.observer.observe(body, { childList: true });
  tocState.resizer = new ResizeObserver(() => { if (tocState.el) tocState.el.style.top = body.offsetTop + 'px'; });
  tocState.resizer.observe(body);
  tocState.spy = () => updateTocSpy();
  body.addEventListener('scroll', tocState.spy, { passive: true });
  syncTocBtn();
}

function closeReaderToc() {
  const body = $('reader-body');
  if (tocState.observer) { tocState.observer.disconnect(); tocState.observer = null; }
  if (tocState.resizer) { tocState.resizer.disconnect(); tocState.resizer = null; }
  if (tocState.spy && body) body.removeEventListener('scroll', tocState.spy);
  if (tocState.el) { tocState.el.remove(); tocState.el = null; }
  tocState.open = false; tocState.spy = null; tocState.items = []; tocState.headEls = [];
  syncTocBtn();
}

function buildReaderToc() {
  const body = $('reader-body'), el = tocState.el, mk = window.MDKit;
  if (!body || !el) return;
  const items = (mk && mk.extractToc) ? mk.extractToc(body.innerHTML) : [];
  tocState.items = items;
  if (!items.length) {
    el.innerHTML = '<div class="md-toc"><div class="md-toc-head">目录</div><div class="md-toc-empty">本文档无标题大纲</div></div>';
    addTocCloseBtn(el); tocState.headEls = []; return;
  }
  el.innerHTML = mk.renderTocHtml(items);
  addTocCloseBtn(el);
  tocState.headEls = items.map(it => body.querySelector('[id="' + it.id + '"]'));
  updateTocSpy();
}

function addTocCloseBtn(el) {
  const head = el.querySelector('.md-toc-head');
  if (!head || head.querySelector('.md-toc-close')) return;
  const btn = document.createElement('button');
  btn.className = 'md-toc-close'; btn.title = '关闭'; btn.textContent = '×';
  head.appendChild(btn);
}

// 滚动高亮:取正文顶部上方最近的标题为当前章节(headEls 按文档序,位置单调,越过即停)
function updateTocSpy() {
  const body = $('reader-body'), el = tocState.el;
  if (!body || !el || !tocState.items.length) return;
  const bodyTop = body.getBoundingClientRect().top;
  let activeIdx = 0;
  for (let i = 0; i < tocState.headEls.length; i++) {
    const h = tocState.headEls[i];
    if (h && h.getBoundingClientRect().top - bodyTop <= 8) activeIdx = i; else break;
  }
  el.querySelectorAll('[data-toc-target]').forEach((li, i) => li.classList.toggle('active', i === activeIdx));
  const act = el.querySelector('[data-toc-target].active');
  if (act) scrollDrawerTo(el, act);
}

function syncTocBtn() {
  const btn = $('rd-toc');
  if (btn) btn.classList.toggle('primary', tocState.open);
}


/* ══════════ 双链真跳转(P5b)══════════
   [[目标]] 渲染为 <a class="wikilink" data-wikilink="目标">;这里在 #reader-body 上事件
   委托:解析目标 → 校验 KB 内是否存在 → showReaderFolder 导航。目标两种形态:
   跨文档 "<DOI目录>/<文件>"、同文档变体 "<文件>"(用当前 readerState.dir)。
   解析/校验认识 KB 目录与 /api/kb/tree,属业务,不进 mdkit 库。仅 docked 阅读面板。 */
function resolveWikilinkTarget(target) {
  if (!target) return null;
  const slash = target.indexOf('/');
  let dir, filePart;
  if (slash >= 0) { dir = target.slice(0, slash); filePart = target.slice(slash + 1); }
  else { dir = readerState.dir; filePart = target; }
  if (!dir || !filePart) return null;
  let file = filePart;
  if (!/\.(md|pdf)$/i.test(file) && file !== '__images__') file += '.md';
  return { dir, file };
}

function wikilinkMiss(target) {
  appendEvent({ ts: new Date().toLocaleTimeString(), level: 'warn', source: 'kb',
                message: `双链目标未找到：[[${target}]]` });
}

async function handleWikilinkClick(a) {
  const target = a.dataset.wikilink;
  const nav = resolveWikilinkTarget(target);
  if (!nav) { wikilinkMiss(target); return; }
  const set = await ensureKbFileSet(nav.dir);   // 已缓存则瞬时;校验目标文件是否存在
  if (set && !set.has(nav.file)) { wikilinkMiss(target); return; }
  showReaderFolder(nav.dir, nav.file);
}

function bindReaderWikilinks() {
  const body = $('reader-body');
  if (!body) return;
  body.addEventListener('click', (e) => {
    if (readerState.editing) return;              // 编辑态(含预览)不跳转,避免丢弃未保存改动
    const a = e.target.closest('a.wikilink[data-wikilink]');
    if (!a || !body.contains(a)) return;
    e.preventDefault();
    handleWikilinkClick(a);
  });
}


/* ══════════ 段落引用 [Pxxx] 可点解释(P11)══════════
   编译产物(_note/_wiki)里的 [Pxxx] 是原文段落 ID(para_id,见 paperkb/context.py 与
   compile.py 的「结论后附段落 ID」约定);渲染层(md-plugins postprocess)已把它变成
   .para-ref 上标芯片。这里点击时读该 KB 目录 document.json 的 paragraphs,弹出该段的
   章节+正文(中文优先),让用户知道引用具体指哪一段原文。 */
const paraDocCache = {};   // dir -> { paraId -> paragraph } | null(加载失败)
async function loadParaIndex(dir) {
  if (dir in paraDocCache) return paraDocCache[dir];
  try {
    const r = await api(`/api/kb/file?path=${encodeURIComponent(dir + '/document.json')}`);
    const doc = typeof r.content === 'string' ? JSON.parse(r.content) : r;
    const byId = {};
    (doc.paragraphs || []).forEach(p => { if (p.para_id) byId[p.para_id] = p; });
    paraDocCache[dir] = byId;
    return byId;
  } catch (e) { paraDocCache[dir] = null; return null; }
}
function onParaOutside(e) {
  if (!e.target.closest('#para-pop') && !e.target.closest('.para-ref')) closeParaPopover();
}
function onParaEsc(e) { if (e.key === 'Escape') closeParaPopover(); }
function closeParaPopover() {
  const p = document.getElementById('para-pop');
  if (p) p.remove();
  document.removeEventListener('mousedown', onParaOutside, true);
  window.removeEventListener('scroll', closeParaPopover, true);
  document.removeEventListener('keydown', onParaEsc, true);
}
function showParaPopover(anchor, para, paraId) {
  closeParaPopover();
  const pop = document.createElement('div');
  pop.className = 'para-pop'; pop.id = 'para-pop';
  const text = para ? (para.text_zh || para.text_en || '') : '';
  const section = para ? (para.section || '') : '';
  pop.innerHTML = `
    <div class="para-pop-head">
      <span class="para-pop-id">${escapeHtml(paraId)}</span>
      <span class="para-pop-sec" title="${escapeHtml(section)}">${escapeHtml(section)}</span>
      <button class="para-pop-close" title="关闭">×</button>
    </div>
    <div class="para-pop-body">${text ? escapeHtml(text) : '<em>未找到该段落原文（document.json 缺失或无此段）</em>'}</div>`;
  document.body.appendChild(pop);
  const ar = anchor.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(window.innerWidth - 348, ar.left - 8)) + 'px';
  pop.style.top = Math.min(window.innerHeight - 220, ar.bottom + 6) + 'px';
  pop.querySelector('.para-pop-close').onclick = closeParaPopover;
  setTimeout(() => {
    document.addEventListener('mousedown', onParaOutside, true);
    window.addEventListener('scroll', closeParaPopover, true);
    document.addEventListener('keydown', onParaEsc, true);
  }, 0);
}
function bindParaRefs() {
  const body = $('reader-body');
  if (!body) return;
  body.addEventListener('click', async (e) => {
    const ref = e.target.closest('.para-ref[data-para]');
    if (!ref || !body.contains(ref)) return;
    e.preventDefault(); e.stopPropagation();
    const paraId = ref.dataset.para;
    const dir = readerState.dir;
    if (!dir) return;
    const byId = await loadParaIndex(dir);
    showParaPopover(ref, byId ? byId[paraId] : null, paraId);
  });
}


// O批-5(item3)：PDF 原文阅读（pdf.js 连续滚动 + 适合宽度 + 缩放 + Ctrl+滚轮缩放，接近浏览器观感）
/* ══════════ PDF 阅读器（P8 重写：DPR 清晰化 + 懒渲染）══════════
   旧版一次性把全部页渲染成 canvas、每次缩放全量重绘，且 canvas 未乘 devicePixelRatio
   → HiDPI 屏模糊、大文件卡顿吃内存。新版：
   ① 按 dpr(上限2) 渲染，CSS 尺寸不变 → 清晰；
   ② 只渲染视口±缓冲页，远离页释放 canvas 回收内存（懒渲染/虚拟化）；
   ③ 缩放/重排锚定「顶部可见页」保持阅读位置；
   ④ 每次重绘先 cancel 旧 RenderTask 防竞态；
   ⑤ 代际令牌 pdfViewerGen：切走/重载后旧异步回调不再动 DOM。 */
let pdfViewerGen = 0;
let pdfViewerDoc = null;

function renderPdfViewer(box, doi) {
  box.innerHTML = `<div class="pdf-wrap">
    <div class="pdf-toolbar">
      <button class="btn small" id="pdf-zoom-out" title="缩小">−</button>
      <span class="muted" id="pdf-zoom-ind">—</span>
      <button class="btn small" id="pdf-zoom-in" title="放大">＋</button>
      <button class="btn small" id="pdf-fit" title="适合宽度">🗔 适宽</button>
      <button class="btn small" id="pdf-outline-btn" title="书签 / 大纲">🔖 目录</button>
      <button class="btn small" id="pdf-tool" title="拖动平移：关（左键选择文本，中键随时可拖动平移）">🖐 拖动</button>
      <span class="pdf-search">
        <input type="text" id="pdf-q" placeholder="搜索本文…" aria-label="PDF 文内搜索">
        <button class="btn small" id="pdf-q-prev" title="上一处（Shift+Enter）">↑</button>
        <span class="muted" id="pdf-q-count"></span>
        <button class="btn small" id="pdf-q-next" title="下一处（Enter）">↓</button>
      </span>
      <span class="muted" id="pdf-page-ind">加载中…</span>
    </div>
    <div class="pdf-scroll" id="pdf-scroll"></div>
    <div class="pdf-outline" id="pdf-outline-drawer" style="display:none">
      <div class="ol-head">目录 / 书签<button class="ol-close" id="pdf-outline-close" title="关闭">×</button></div>
      <div class="ol-body"></div>
    </div>
  </div>`;
  if (typeof pdfjsLib === 'undefined') {
    box.innerHTML = '<div class="empty">PDF.js 未加载（vendor/pdfjs 缺失）</div>'; return;
  }
  const gen = ++pdfViewerGen;
  if (pdfViewerDoc) { try { pdfViewerDoc.destroy(); } catch (e) {} pdfViewerDoc = null; }
  pdfjsLib.GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.min.js';
  const url = '/api/kb/pdf?doi=' + encodeURIComponent(doi);
  const DPR_CAP = 2;
  pdfjsLib.getDocument(url).promise.then(pdf => {
    const scroll = $('pdf-scroll');
    if (!scroll || gen !== pdfViewerGen) { try { pdf.destroy(); } catch (e) {} return; }
    pdfViewerDoc = pdf;
    const N = pdf.numPages;
    let baseScale = 1.0, fitScale = 1.0, zoom = 1.0;
    const curScale = () => baseScale * zoom;
    const dpr = () => Math.min(window.devicePixelRatio || 1, DPR_CAP);
    // 每页占位：wrap 撑出正确高度 → 滚动条/锚定准确；canvas 仅在渲染时有内容
    const pages = [];
    for (let i = 0; i < N; i++) {
      const wrap = document.createElement('div');
      wrap.className = 'pdf-page';
      wrap.dataset.page = String(i + 1);
      const canvas = document.createElement('canvas');
      const tl = document.createElement('div');      // 文本层：透明文字叠在 canvas 上，供选中/复制/搜索
      tl.className = 'textLayer';
      wrap.appendChild(canvas);
      wrap.appendChild(tl);
      scroll.appendChild(wrap);
      pages.push({ wrap, canvas, tl, page: null, w1: 0, h1: 0,
                   renderedScale: 0, renderedDpr: 0, pendingScale: 0, pendingDpr: 0,
                   task: null, tlTask: null, textContent: null, textDivs: [] });
    }
    let d0 = { w: 612, h: 792 };   // 未取页的兜底比例（Letter），首页取到后覆盖

    function setPlaceholder(i) {
      const p = pages[i], s = curScale();
      const w = (p.w1 || d0.w) * s, h = (p.h1 || d0.h) * s;
      p.wrap.style.width = Math.floor(w) + 'px';
      p.wrap.style.height = Math.floor(h) + 'px';
    }
    function relayout() { for (let i = 0; i < N; i++) setPlaceholder(i); }

    function releasePage(i) {
      const p = pages[i];
      if (p.task) { try { p.task.cancel(); } catch (e) {} p.task = null; }
      if (p.tlTask) { try { p.tlTask.cancel(); } catch (e) {} p.tlTask = null; }
      // 无条件清空：pending 中的页 renderedScale 仍为 0 但 canvas.width 已被 paint 设过，
      // 只按 renderedScale 判断会漏清 → 滚过的页累积不回收（内存泄漏）。
      if (p.canvas.width) { p.canvas.width = 0; p.canvas.height = 0; }
      if (p.tl.firstChild) p.tl.textContent = '';   // 清文本层 DOM；textContent 缓存保留供搜索
      p.textDivs = [];
      p.renderedScale = 0; p.renderedDpr = 0; p.pendingScale = 0; p.pendingDpr = 0;
    }

    function renderPage(i) {
      const p = pages[i], s = curScale(), d = dpr();
      if (p.renderedScale === s && p.renderedDpr === d) return;    // 已是当前规格
      if (p.pendingScale === s && p.pendingDpr === d) return;       // 正在渲染同一规格
      p.pendingScale = s; p.pendingDpr = d;
      const paint = (page) => {
        if (gen !== pdfViewerGen) return;
        if (!p.w1) { const v1 = page.getViewport({ scale: 1 }); p.w1 = v1.width; p.h1 = v1.height; setPlaceholder(i); }
        if (p.task) { try { p.task.cancel(); } catch (e) {} }
        const vpCss = page.getViewport({ scale: s });
        const vpDev = page.getViewport({ scale: s * d });
        p.canvas.width = Math.floor(vpCss.width * d);
        p.canvas.height = Math.floor(vpCss.height * d);
        p.canvas.style.width = Math.floor(vpCss.width) + 'px';
        p.canvas.style.height = Math.floor(vpCss.height) + 'px';
        p.task = page.render({ canvasContext: p.canvas.getContext('2d'), viewport: vpDev });
        p.task.promise.then(() => {
          if (gen !== pdfViewerGen) return;
          p.renderedScale = s; p.renderedDpr = d; p.task = null;
        }).catch(() => { if (gen === pdfViewerGen) { p.task = null; p.pendingScale = 0; } });
        // 文本层：HTML span 叠在 canvas 上，随 CSS 尺寸(vpCss)定位；--scale-factor 必须=当前
        // CSS 缩放，否则 renderTextLayer 的 calc(var(--scale-factor)*Npx) 字号算错。
        p.tl.style.width = Math.floor(vpCss.width) + 'px';
        p.tl.style.height = Math.floor(vpCss.height) + 'px';
        p.tl.style.setProperty('--scale-factor', String(s));
        const paintText = (tc) => {
          if (gen !== pdfViewerGen) return;
          if (p.tlTask) { try { p.tlTask.cancel(); } catch (e) {} }
          p.tl.textContent = '';
          p.textDivs = [];
          try {
            p.tlTask = pdfjsLib.renderTextLayer({ textContentSource: tc, container: p.tl, viewport: vpCss, textDivs: p.textDivs });
            p.tlTask.promise.then(() => {
              if (gen !== pdfViewerGen) return;
              p.tlTask = null;
              if (typeof applySearchHighlight === 'function') applySearchHighlight(i);
            }).catch(() => { if (gen === pdfViewerGen) p.tlTask = null; });
          } catch (e) { p.tlTask = null; }
        };
        if (p.textContent) paintText(p.textContent);
        else page.getTextContent().then(tc => {
          if (gen !== pdfViewerGen) return;
          p.textContent = tc;
          paintText(tc);
        }).catch(() => {});
      };
      if (p.page) paint(p.page);
      else pdf.getPage(i + 1).then(pg => {
        if (gen !== pdfViewerGen) return;
        p.page = pg; paint(pg);
      }).catch(() => { if (gen === pdfViewerGen) p.pendingScale = 0; });
    }

    // 懒渲染（确定性）：由 offsetTop 算「视口 ± 一屏」窗口，进入即渲染、远离即释放。
    // 页高已恒定（.pdf-page flex:0 0 auto + canvas 绝对定位），offsetTop 可靠。
    // 不用 IntersectionObserver——合成滚动/无头环境下 IO 投递不稳定，一旦加载抖动期把
    // 全部页渲染了就再也不投递 release（实测过），窗口会失控。显式在每次滚动/缩放回收。
    function visibleWindow() {
      const st = scroll.scrollTop, ch = scroll.clientHeight, buf = ch;
      let first = -1, last = -1;
      for (let i = 0; i < N; i++) {
        const top = pages[i].wrap.offsetTop, h = pages[i].wrap.offsetHeight;
        if (top + h >= st - buf && top <= st + ch + buf) { if (first < 0) first = i; last = i; }
      }
      return { first, last };
    }
    function syncRender() {
      if (gen !== pdfViewerGen) return;
      const { first, last } = visibleWindow();
      if (first < 0) return;
      for (let i = first; i <= last; i++) renderPage(i);
      for (let i = 0; i < N; i++) if (i < first - 2 || i > last + 2) releasePage(i);   // 远离页回收
      updatePageInd();
    }

    function updatePageInd() {
      const ind = $('pdf-page-ind');
      if (!ind) return;
      const st = scroll.scrollTop;                 // 精确取视口顶部所在页
      let cur = 1;
      for (let i = 0; i < N; i++) { if (pages[i].wrap.offsetTop <= st + 2) cur = i + 1; else break; }
      ind.textContent = `第 ${cur} / ${N} 页`;
    }
    function updateZoomInd() {
      const ind = $('pdf-zoom-ind');
      if (ind) ind.textContent = Math.round(curScale() * 100) + '%';
    }

    /* ── 文内搜索（P10b）──
       全文匹配：逐页 getTextContent 拼成纯文本(缓存)，大小写不敏感找全部命中 →
       命中列表 [{page,pos}]，prev/next 跳到对应页并滚入视口。高亮：已渲染页的文本层里把
       命中子串包 <mark class="pdf-hl">、当前处加 .current。跨 text item 的命中会计数但可能
       不逐字高亮（best-effort，不重造 PDFFindController）。 */
    let searchQ = '', searchMatches = [], searchIdx = -1;
    const pageTextCache = {};

    function clearMarks(tl) {
      tl.querySelectorAll('mark.pdf-hl').forEach(mk => {
        const parent = mk.parentNode;
        parent.replaceChild(document.createTextNode(mk.textContent), mk);
        parent.normalize();
      });
    }
    function highlightSpan(sp, ql, marks) {
      const txt = sp.textContent, low = txt.toLowerCase();
      if (!low.includes(ql)) return;
      const frag = document.createDocumentFragment();
      let from = 0, idx;
      while ((idx = low.indexOf(ql, from)) >= 0) {
        if (idx > from) frag.appendChild(document.createTextNode(txt.slice(from, idx)));
        const mk = document.createElement('mark');
        mk.className = 'pdf-hl'; mk.textContent = txt.slice(idx, idx + ql.length);
        frag.appendChild(mk); marks.push(mk);
        from = idx + ql.length;
      }
      if (from < txt.length) frag.appendChild(document.createTextNode(txt.slice(from)));
      sp.textContent = ''; sp.appendChild(frag);
    }
    // 由 paintText 在文本层渲染完成后调用；也用于翻页后补高亮
    function applySearchHighlight(i) {
      const p = pages[i];
      if (!p || !p.tl) return;
      clearMarks(p.tl);
      if (!searchQ) return;
      const ql = searchQ.toLowerCase(), marks = [];
      p.tl.querySelectorAll('span').forEach(sp => highlightSpan(sp, ql, marks));
      const m = searchMatches[searchIdx];
      if (m && m.page === i && marks.length) {
        let k = 0;
        for (const mm of searchMatches) if (mm.page === i && mm.pos < m.pos) k++;
        marks[Math.min(k, marks.length - 1)].classList.add('current');
      }
    }
    function clearAllHighlights() { for (let i = 0; i < N; i++) if (pages[i].tl) clearMarks(pages[i].tl); }
    function updateSearchCount() {
      const el = $('pdf-q-count');
      if (!el) return;
      el.textContent = !searchQ ? '' : (searchMatches.length ? `${searchIdx + 1}/${searchMatches.length}` : '0/0');
    }
    async function ensurePageText(i) {
      if (pageTextCache[i] != null) return pageTextCache[i];
      const pg = pages[i].page || await pdf.getPage(i + 1);
      pages[i].page = pg;
      const tc = pages[i].textContent || await pg.getTextContent();
      pages[i].textContent = tc;
      pageTextCache[i] = tc.items.map(it => it.str).join('');
      return pageTextCache[i];
    }
    async function buildSearchIndex(q) {
      const ql = q.toLowerCase(), out = [];
      for (let i = 0; i < N; i++) {
        const txt = (await ensurePageText(i)).toLowerCase();
        if (gen !== pdfViewerGen) return out;
        let from = 0, idx;
        while ((idx = txt.indexOf(ql, from)) >= 0) { out.push({ page: i, pos: idx }); from = idx + ql.length; }
      }
      return out;
    }
    function currentPageIdx() {
      const st = scroll.scrollTop; let a = 0;
      for (let i = 0; i < N; i++) { if (pages[i].wrap.offsetTop <= st + 2) a = i; else break; }
      return a;
    }
    function showMatch(idx) {
      if (!searchMatches.length) return;
      searchIdx = ((idx % searchMatches.length) + searchMatches.length) % searchMatches.length;
      const m = searchMatches[searchIdx];
      scroll.scrollTop = Math.max(0, pages[m.page].wrap.offsetTop - 8);
      syncRender();                                   // 确保该页渲染（文本层随之生成）
      applySearchHighlight(m.page);
      updateSearchCount();
      const center = () => {
        if (gen !== pdfViewerGen || searchMatches[searchIdx] !== m) return;
        applySearchHighlight(m.page);
        const c = pages[m.page].tl.querySelector('mark.pdf-hl.current');
        if (c) c.scrollIntoView({ block: 'center' });
      };
      if (pages[m.page].tl.querySelector('mark.pdf-hl.current')) center();
      else setTimeout(center, 450);                   // 文本层异步生成后再定位
    }
    async function runSearch(q) {
      clearAllHighlights();
      searchQ = q.trim();
      if (!searchQ) { searchMatches = []; searchIdx = -1; updateSearchCount(); return; }
      const cnt = $('pdf-q-count'); if (cnt) cnt.textContent = '…';
      searchMatches = await buildSearchIndex(searchQ);
      if (gen !== pdfViewerGen) return;
      if (!searchMatches.length) { searchIdx = -1; updateSearchCount(); return; }
      const curPage = currentPageIdx();
      let start = searchMatches.findIndex(mm => mm.page >= curPage);
      showMatch(start < 0 ? 0 : start);
    }
    function bindSearchUI() {
      const input = $('pdf-q');
      if (!input) return;
      let deb = null;
      input.addEventListener('input', () => { clearTimeout(deb); deb = setTimeout(() => runSearch(input.value), 350); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          if (!searchMatches.length) runSearch(input.value);
          else showMatch(searchIdx + (e.shiftKey ? -1 : 1));
        } else if (e.key === 'Escape') { input.value = ''; runSearch(''); input.blur(); }
      });
      const prev = $('pdf-q-prev'), next = $('pdf-q-next');
      if (prev) prev.onclick = () => { searchMatches.length ? showMatch(searchIdx - 1) : runSearch(input.value); };
      if (next) next.onclick = () => { searchMatches.length ? showMatch(searchIdx + 1) : runSearch(input.value); };
    }

    /* ── 大纲 / 书签（P10c）──
       pdf.getOutline() 取书签树（可能为 null）；点击解析 dest 到页码：显式数组走
       getPageIndex(dest[0])、命名目标先 getDestination 再解析；滚到该页并 syncRender。 */
    let outlineCache = null;
    async function gotoDest(dest) {
      if (!dest) return;
      try {
        let explicit = dest;
        if (typeof dest === 'string') explicit = await pdf.getDestination(dest);
        if (!explicit || !explicit.length) return;
        const idx = await pdf.getPageIndex(explicit[0]);
        if (gen !== pdfViewerGen || idx == null || !pages[idx]) return;
        scroll.scrollTop = Math.max(0, pages[idx].wrap.offsetTop - 8);
        syncRender();
      } catch (e) {}
    }
    function renderOutlineItems(items, depth, container) {
      items.forEach(it => {
        const b = document.createElement('button');
        b.className = 'ol-item';
        b.style.paddingLeft = (6 + depth * 13) + 'px';
        b.textContent = it.title || '(无标题)';
        b.title = it.title || '';
        b.onclick = () => gotoDest(it.dest);
        container.appendChild(b);
        if (it.items && it.items.length) renderOutlineItems(it.items, depth + 1, container);
      });
    }
    async function toggleOutline() {
      const drawer = $('pdf-outline-drawer');
      if (!drawer) return;
      if (drawer.style.display !== 'none') { drawer.style.display = 'none'; return; }
      const btn = $('pdf-outline-btn');
      if (btn) { btn.disabled = true; btn.textContent = '🔖 载入…'; }
      try { if (outlineCache == null) outlineCache = (await pdf.getOutline()) || []; }
      catch (e) { outlineCache = []; }
      if (gen !== pdfViewerGen) return;
      const body = drawer.querySelector('.ol-body');
      body.textContent = '';
      if (!outlineCache.length) body.innerHTML = '<div class="ol-empty">本文档无书签 / 大纲</div>';
      else renderOutlineItems(outlineCache, 0, body);
      const tb = box.querySelector('.pdf-toolbar');
      drawer.style.top = ((tb ? tb.offsetHeight : 0) + 6) + 'px';
      drawer.style.display = 'block';
      if (btn) { btn.disabled = false; btn.textContent = '🔖 目录'; }
    }
    function bindOutlineUI() {
      const btn = $('pdf-outline-btn');
      if (btn) btn.onclick = toggleOutline;
      const close = $('pdf-outline-close');
      if (close) close.onclick = () => { const d = $('pdf-outline-drawer'); if (d) d.style.display = 'none'; };
    }

    // 缩放/重排：锚定顶部可见页（offsetTop，需 .pdf-scroll position:relative）保持阅读位置
    function rescale() {
      const st = scroll.scrollTop;
      let ai = 0, aoff = 0;
      for (let i = 0; i < N; i++) {
        const t = pages[i].wrap.offsetTop;
        if (t <= st + 2) { ai = i; aoff = st - t; } else break;
      }
      const oldH = pages[ai].wrap.offsetHeight || 1;
      relayout();
      const newH = pages[ai].wrap.offsetHeight || 1;
      scroll.scrollTop = pages[ai].wrap.offsetTop + aoff * (newH / oldH);
      updateZoomInd();
      syncRender();
    }
    const zoomTo = (z) => { zoom = Math.min(4, Math.max(0.3, z)); rescale(); };

    // 首页驱动初始布局（不再一次性取全部页）
    pdf.getPage(1).then(p1 => {
      if (gen !== pdfViewerGen) return;
      pages[0].page = p1;
      const v1 = p1.getViewport({ scale: 1 });
      pages[0].w1 = v1.width; pages[0].h1 = v1.height;
      d0 = { w: v1.width, h: v1.height };
      fitScale = Math.max(0.4, scroll.clientWidth / v1.width);
      baseScale = fitScale; zoom = 1.0;
      relayout();
      updateZoomInd();
      syncRender();                               // 布局已定（relayout 同步设高），直接算首屏窗口
      // 滚动即同步重算窗口：scroll 事件浏览器本已按帧节流；renderPage/releasePage 均有早退守卫，
      // 重复调用廉价。不用 rAF——后台标签页/无合成帧环境下 rAF 会被 throttle 导致不释放。
      scroll.addEventListener('scroll', () => { if (gen === pdfViewerGen) syncRender(); }, { passive: true });
    }).catch(e => {
      if (gen === pdfViewerGen) box.innerHTML = '<div class="empty">PDF 首页加载失败：' + escapeHtml(e.message) + '</div>';
    });

    $('pdf-zoom-in').onclick = () => zoomTo(zoom + 0.2);
    $('pdf-zoom-out').onclick = () => zoomTo(zoom - 0.2);
    $('pdf-fit').onclick = () => zoomTo(1.0);
    // 工具切换：默认「选择」——左键拖拽选字/复制（文本层）；开「拖动」后左键拖拽平移。
    // 无论哪种模式，中键拖拽始终平移（power user 顺手）。
    let panMode = false;
    const toolBtn = $('pdf-tool');
    if (toolBtn) toolBtn.onclick = () => {
      panMode = !panMode;
      toolBtn.classList.toggle('active', panMode);
      scroll.classList.toggle('pan-mode', panMode);
      toolBtn.title = panMode
        ? '拖动平移：开（左键拖动平移；切回可选择文本）'
        : '拖动平移：关（左键选择文本，中键随时可拖动平移）';
    };
    bindSearchUI();   // P10b：文内搜索
    bindOutlineUI();  // P10c：书签/大纲
    // Ctrl+滚轮缩放（像浏览器）
    scroll.addEventListener('wheel', (e) => {
      if (e.ctrlKey) { e.preventDefault(); zoomTo(zoom + (e.deltaY < 0 ? 0.15 : -0.15)); }
    }, { passive: false });
    // 拖拽平移（hand 工具）：按住拖动同时滚横纵两轴。move/up 仅在拖动期间挂 window、松手即摘。
    let panning = false, panX = 0, panY = 0, panSL = 0, panST = 0;
    const onPanMove = (e) => {
      if (!panning || gen !== pdfViewerGen) { endPan(); return; }
      scroll.scrollLeft = panSL - (e.clientX - panX);
      scroll.scrollTop = panST - (e.clientY - panY);
    };
    function endPan() {
      if (!panning) return;
      panning = false;
      scroll.classList.remove('panning');
      window.removeEventListener('mousemove', onPanMove);
      window.removeEventListener('mouseup', endPan);
    }
    scroll.addEventListener('mousedown', (e) => {
      const wantPan = (e.button === 1) || (e.button === 0 && panMode);
      if (!wantPan || panning) return;
      panning = true; panX = e.clientX; panY = e.clientY;
      panSL = scroll.scrollLeft; panST = scroll.scrollTop;
      scroll.classList.add('panning');
      window.addEventListener('mousemove', onPanMove);
      window.addEventListener('mouseup', endPan);
      e.preventDefault();               // 阻止原生选中/拖影/中键自动滚动
    });
    // 窗口尺寸变化：重算适宽基准并重排（防抖）
    let rzTimer = null;
    window.addEventListener('resize', () => {
      if (gen !== pdfViewerGen) return;
      clearTimeout(rzTimer);
      rzTimer = setTimeout(() => {
        if (gen !== pdfViewerGen || !pages[0].w1) return;
        fitScale = Math.max(0.4, scroll.clientWidth / pages[0].w1);
        baseScale = fitScale;
        rescale();
      }, 200);
    });
  }).catch(e => {
    if (gen === pdfViewerGen) box.innerHTML = '<div class="empty">PDF 加载失败：' + escapeHtml(e.message) + '</div>';
  });
}

async function loadReaderFile(dir, file) {
  const box = $('reader-body');
  suppressScroll();               // P7：抑制切换瞬间旧内容塌陷产生的伪滚动保存
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
    renderIntoReader(renderMarkdown(r.content, dir));
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(file)} 不可用：${escapeHtml(e.message)}</div>`;
  }
}

/* renderMarkdown / frontmatter / callout / 公式保护 已抽离到独立库 frontend/js/mdkit/
   （业务插件在 js/md-plugins.js，装配在 js/md-app.js）。全局 renderMarkdown(md, baseDir, source)
   由 md-app.js 模块挂载，本文件直接调用即可——勿在此重复定义。 */
