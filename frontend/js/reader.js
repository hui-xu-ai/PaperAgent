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
  if (readerState.editing) return;              // 编辑态不覆盖阅读位置
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
    if (h) h.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
  if (act) {
    const ar = act.getBoundingClientRect(), er = el.getBoundingClientRect();
    if (ar.top < er.top || ar.bottom > er.bottom) act.scrollIntoView({ block: 'nearest' });
  }
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
    renderIntoReader(renderMarkdown(r.content, dir));
  } catch (e) {
    box.innerHTML = `<div class="empty">${escapeHtml(file)} 不可用：${escapeHtml(e.message)}</div>`;
  }
}

/* renderMarkdown / frontmatter / callout / 公式保护 已抽离到独立库 frontend/js/mdkit/
   （业务插件在 js/md-plugins.js，装配在 js/md-app.js）。全局 renderMarkdown(md, baseDir, source)
   由 md-app.js 模块挂载，本文件直接调用即可——勿在此重复定义。 */
