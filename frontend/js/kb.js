/* kb.js — 知识库树/列表/编译进度 + 知识库管理（M5） */

/* ══════════ 知识库树（V11：折叠/展开 + 文件操作）══════════ */
const kbUi = { collapsed: {}, folders: [], filter: '' };

async function loadKbTree() {
  try {
    const t = await api('/api/kb/tree');
    kbUi.folders = t.folders;
    renderKbTree();
  } catch (e) { $('kb-tree').innerHTML = '<div class="empty">加载失败：' + escapeHtml(e.message) + '</div>'; }
}

/* G9/P5-E1：知识库搜索/筛选（列表模式 → /kb/list 的 q 参数；目录模式 → 本地过滤） */
function bindKbSearch() {
  const inp = $('kb-search-input');
  if (!inp) return;
  let t = null;
  inp.addEventListener('input', () => {
    clearTimeout(t);
    if (kbView.mode === 'tree') { kbUi.filter = inp.value.trim(); renderKbTree(); return; }
    t = setTimeout(() => { kbView.q = inp.value.trim(); kbView.page = 1; loadKbList(); }, 300);
  });
  const cf = $('kb-filter-compile');
  if (cf) {
    cf.value = kbView.compile;   // T7：默认下拉停在「已编译」
    cf.addEventListener('change', () => { kbView.compile = cf.value; kbView.page = 1; loadKbList(); });
  }
  const jf = $('kb-filter-journal');
  if (jf) jf.addEventListener('change', () => { kbView.journal = jf.value; kbView.page = 1; loadKbList(); });
  const vt = $('kb-view-toggle');
  if (vt) vt.addEventListener('click', toggleKbView);
  // P0-A：同步磁盘（对账 + 重拉列表/树/统计）
  const sb = $('kb-sync-btn');
  if (sb) sb.addEventListener('click', syncKbDisk);
  // 2026-09-12 用户需求：知识库回收站（移出知识库的文献 → 主界面一键恢复）
  const tb = $('kb-trash-btn');
  if (tb) tb.addEventListener('click', openTrashModal);
  const tc = $('trash-close');
  if (tc) tc.addEventListener('click', () => { $('trash-modal').style.display = 'none'; });
  const tm = $('trash-modal');
  if (tm) tm.addEventListener('click', (e) => { if (e.target.id === 'trash-modal') tm.style.display = 'none'; });
  bindTrashModal();
  refreshTrashCount();
  // 2026-09-19：清理幽灵记录（kb 目录已删除但数据库仍有记录）
  const cb = $('kb-cleanup-btn');
  if (cb) cb.addEventListener('click', cleanupStaleRecords);
  // T7：悬停预览弹窗——进入弹窗不关闭，离开行/弹窗 200ms 后消失；列表滚动时收起
  const pop = $('kbl-preview');
  if (pop) {
    pop.addEventListener('mouseenter', () => clearTimeout(kblpHideTimer));
    pop.addEventListener('mouseleave', () => scheduleKbPreviewHide());
  }
  const lw = $('kb-list-wrap');
  if (lw) lw.addEventListener('scroll', scheduleKbPreviewHide);
}


/* ─── kb 列表视图 + 编译进度（从 app.js 合并） ─── */

/* ══════════ P5-E1：知识库列表视图（/api/kb-meta/kb/list 聚合列表 + 分页 + 详情模态）══════════ */
/* T7：compile 默认 'done' → 列表只显示已编译文献（未编译不占开放索引 token） */
const kbView = { mode: 'list', q: '', compile: 'done', journal: '', kind: 'all',
                 page: 1, pageSize: 50, total: 0, items: [], chips: null };

/* ══════════ P5-E2：编译进度全局可见（/api/kb-meta/stats → compile，15s 低频轮询）══════════ */
async function loadKbCompile() {
  const box = $('kb-compile');
  if (!box) return;
  try {
    const s = await api('/api/kb-meta/stats');
    renderKbCompile(s.compile || {});
  } catch (e) { /* 进度不可用不打扰主功能 */ }
}

function renderKbCompile(cp) {
  const box = $('kb-compile');
  if (!box) return;
  const l1 = cp.l1_done || 0, l2 = cp.l2_done || 0, l3 = cp.l3_done || 0;
  const queued = cp.queued || 0, processing = cp.processing || 0, failed = cp.failed || 0;
  const active = queued + processing;
  const done = l1 + l2 + l3;
  const total = done + active + failed;
  const pct = total ? Math.round(done / total * 100) : 0;
  box.style.display = '';
  $('kb-compile-state').innerHTML = active
    ? '<span class="kb-cs active">⚙ 编译进行中</span>'
    : '<span class="kb-cs">已就绪</span>';
  $('kb-compile-fill').style.width = pct + '%';
  $('kb-compile-nums').innerHTML =
    '<span class="kb-cn">L1 <b>' + l1 + '</b></span>' +
    '<span class="kb-cn">L2 <b>' + l2 + '</b></span>' +
    '<span class="kb-cn">L3 <b>' + l3 + '</b></span>' +
    '<span class="kb-cn">⏳ 排队 <b>' + queued + '</b></span>' +
    '<span class="kb-cn">⚙ 处理中 <b>' + processing + '</b></span>' +
    (failed ? '<span class="kb-cn warn">✗ 失败 <b>' + failed + '</b></span>' : '');
}

async function loadKbList() {
  const wrap = $('kb-list-wrap');
  if (!wrap) return;
  loadKbCompile(); // P5-E2：列表刷新时顺带刷新编译进度（fire-and-forget）
  wrap.innerHTML = '<div class="empty">加载中…</div>';
  const ps = new URLSearchParams({ page: kbView.page, page_size: kbView.pageSize, sort: 'value' });
  if (kbView.q) ps.set('q', kbView.q);
  if (kbView.compile) ps.set('compile_status', kbView.compile);
  if (kbView.journal) ps.set('journal', kbView.journal);
  // F1：类型芯片（服务端过滤；'has_attachment' 用独立参数）
  if (kbView.kind === 'has_attachment') ps.set('has_attachment', 'true');
  else if (kbView.kind && kbView.kind !== 'all') ps.set('kind', kbView.kind);
  try {
    const r = await api('/api/kb-meta/kb/list?' + ps.toString());
    kbView.items = r.items || [];
    kbView.total = r.total || 0;
    renderKbList();
  } catch (e) {
    wrap.innerHTML = '<div class="empty">加载失败：' + escapeHtml(e.message) + '</div>';
  }
  loadKbKindChips();   // F1：芯片计数（服务端算；失败不打扰列表）
}

/* F1：知识库类型芯片（/api/kb-meta/kind/options；选中即服务端过滤） */
async function loadKbKindChips() {
  const el = $('kb-kind-chips');
  if (!el) return;
  try {
    kbView.chips = await api('/api/kb-meta/kind/options');
  } catch (e) { return; }
  const chips = (kbView.chips && kbView.chips.chips) || [];
  if (!chips.length) { el.innerHTML = ''; return; }
  el.innerHTML = chips.map(c =>
    `<button class="tc-chip ${c.kind === (kbView.kind || 'all') ? 'active' : ''}" data-kind="${escapeHtml(c.kind)}"
       title="按类型筛选：${escapeHtml(c.label)}（${c.count}）">${escapeHtml(c.label)}<span class="tc-n">${c.count}</span></button>`).join('');
  el.querySelectorAll('.tc-chip').forEach(b => {
    b.addEventListener('click', () => {
      if (b.dataset.kind === (kbView.kind || 'all')) return;
      kbView.kind = b.dataset.kind;
      kbView.page = 1;
      loadKbList();
    });
  });
}

function renderKbList() {
  const wrap = $('kb-list-wrap');
  if (!wrap) return;
  hideKbPreview();   // T7：列表重渲染（翻页/过滤/刷新）时收起悬停预览
  const items = kbView.items;
  // 期刊过滤下拉：选项从当前页聚合（服务端按精确期刊名过滤）
  const jf = $('kb-filter-journal');
  if (jf) {
    const cur = jf.value;
    const journals = [...new Set(items.map(x => x.journal).filter(Boolean))].sort();
    jf.innerHTML = '<option value="">期刊:全部</option>' + journals.map(j =>
      '<option value="' + escapeHtml(j) + '"' + (j === cur ? ' selected' : '') + '>' +
      escapeHtml(shortTitle(j, 22)) + '</option>').join('');
  }
  if (!items.length) {
    wrap.innerHTML = `<div class="empty">无匹配文献（共 ${kbView.total} 篇）</div>`;
    renderKbPager();
    return;
  }
  // T7：行重构——首列=DOI（可换行）、期刊全显示(11px)、年/价值分窄列、状态=编译最高级徽标；去掉操作列与知识库徽标列
  const rows = items.map(it => {
    const compiled = it.compiled || [];
    const top = compiled.slice().sort().pop() || '';
    const lvlBadge = top
      ? `<span class="kba-badge ok" title="已编译：${escapeHtml(compiled.join(', '))}">${escapeHtml(top)}</span>`
      : '<span class="kba-badge">未编译</span>';
    const queued = (it.queued || []).length
      ? `<span class="kba-badge warn" title="排队中：${escapeHtml(it.queued.join(','))}">排队:${escapeHtml(it.queued.join(','))}</span>` : '';
    // P0-A（2026-09-11）磁盘为准：记录仍在（bib 元数据），但磁盘上已无该目录
    // （用户在资源管理器里移动/删除过）→ 标灰 + 明示，避免"文件没了列表还在"。
    const lost = it.on_disk === false;
    const lostBadge = lost
      ? `<span class="kba-badge warn" title="这条记录还在库中，但磁盘上已找不到对应目录（可能被移动或删除）。点「🔄 同步磁盘」查看不一致项。">⚠ 文件已不在</span>` : '';
    const key = it.doi || it.dir || '';
    return `<tr class="kbl-row${lost ? ' kbl-lost' : ''}" data-doi="${escapeHtml(it.doi || '')}" data-key="${escapeHtml(key)}">
      <td class="kbl-doi">${escapeHtml(it.doi || it.dir || '—')}</td>
      <td class="kbl-journal">${escapeHtml(it.journal || '—')}</td>
      <td class="kbl-year">${escapeHtml(it.year || '—')}</td>
      <td class="kbl-value">${it.value_score != null ? Number(it.value_score).toFixed(1) : '—'}</td>
      <td class="kbl-status">${lvlBadge}${queued}${lostBadge}</td>
    </tr>`;
  }).join('');
  const onlyDone = kbView.compile === 'done' ? ' · 仅显示已编译' : '';
  wrap.innerHTML = `<div class="kbl-total muted">共 ${kbView.total} 篇${onlyDone}</div>
    <table class="kba-table kbl-table"><thead><tr><th>DOI</th><th>期刊</th><th>年</th><th>价值分</th><th>状态</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  wrap.querySelectorAll('.kbl-row').forEach(tr => {
    // 无 DOI 的磁盘目录（source=disk）没有详情页可开，只保留悬停预览
    if (tr.dataset.doi) tr.addEventListener('click', () => openKbDetail(tr.dataset.doi));
    tr.addEventListener('mouseenter', () => showKbPreview(tr));
    tr.addEventListener('mouseleave', () => scheduleKbPreviewHide());
  });
  renderKbPager();
}

/* P0-A：手动"同步磁盘"——对账（磁盘↔数据库）+ 重拉列表/文件树/统计。
   用户在资源管理器手改文件后，点这里让界面与磁盘对齐，并列出不一致项。 */
async function syncKbDisk() {
  const info = $('kb-sync-info');
  const btn = $('kb-sync-btn');
  if (info) info.textContent = '同步中…';
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/kb-meta/reconcile');
    const s = (r && r.summary) || {};
    const orphan = Number(s.orphan_dir || 0);
    const ghost = Number(s.ghost_row || 0);
    if (info) {
      const metaRows = Number(s.meta_rows || 0);
      const diskDirs = Number(s.disk_dirs || 0);
      const ts = new Date().toLocaleTimeString();
      if (!orphan && !ghost) {
        info.textContent = `✅ ${ts} 已与磁盘对齐：${diskDirs} 个目录、${metaRows} 条元数据，无不一致。`;
      } else {
        const bits = [];
        if (ghost) bits.push(`⚠ 库里有 ${ghost} 条记录但磁盘上找不到目录（已在列表中标为「⚠ 文件已不在」）`);
        if (orphan) {
          // 未导入 bib 时 meta_rows=0，此时"目录无记录"是正常状态，不该报成不一致
          bits.push(metaRows === 0
            ? `ℹ 磁盘有 ${orphan} 个文献目录尚未登记元数据（未导入 bib 时属正常，不影响解析/翻译/编译）`
            : `ℹ 磁盘有 ${orphan} 个目录不在元数据表中（可在「知识库管理 → 元数据」补录）`);
        }
        info.textContent = `${ts} ` + bits.join('；') + '。';
      }
    }
    // P0-A：同步要覆盖**全部视图**——否则用户在文献库删了文件、点这里却只刷新了知识库面板，
    // 会以为"按钮没效果"（2026-09-11 用户反馈）。
    await loadPapers();
    kbView.chips = null;   // F1：类型计数缓存失效（磁盘可能刚被手改）
    await loadKbList();
    loadKbCompile();
    if (kbView.mode === 'tree') loadKbTree();
  } catch (e) {
    if (info) info.textContent = '❌ 同步失败：' + (e && e.message ? e.message : e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* 2026-09-19：清理幽灵记录——删除 kb 目录已不存在但数据库仍有元数据/编译任务的条目。 */
async function cleanupStaleRecords() {
  const info = $('kb-sync-info');
  const btn = $('kb-cleanup-btn');
  if (!(await askConfirm('确定要清理幽灵记录吗？\n\n将删除 kb 目录已不存在但数据库仍有元数据/编译任务的条目。\n此操作不可撤销。'))) return;
  if (info) info.textContent = '清理中…';
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/kb-meta/cleanup-stale', { method: 'POST' });
    const removed = Number(r.removed_meta || 0) + Number(r.removed_jobs || 0);
    const kept = Number(r.kept || 0);
    if (info) {
      const ts = new Date().toLocaleTimeString();
      if (removed === 0) {
        info.textContent = `✅ ${ts} 无幽灵记录：${kept} 条元数据均正常。`;
      } else {
        info.textContent = `🧹 ${ts} 已清理 ${removed} 条幽灵记录（删除元数据 ${r.removed_meta} 条、编译任务 ${r.removed_jobs} 条），保留 ${kept} 条。`;
      }
    }
    kbView.chips = null;
    await loadKbList();
    loadKbCompile();
    if (kbView.mode === 'tree') loadKbTree();
  } catch (e) {
    if (info) info.textContent = '❌ 清理失败：' + (e && e.message ? e.message : e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* T7：知识库列表行悬停预览——显示标题/期刊/年/价值分/等级/原文层四件/编译状态 + 单篇 kb 操作 + 知识库问答入口 */
let kblpHideTimer = null;

function hideKbPreview() {
  clearTimeout(kblpHideTimer);
  const pop = $('kbl-preview');
  if (pop) pop.style.display = 'none';
}

function scheduleKbPreviewHide() {
  clearTimeout(kblpHideTimer);
  kblpHideTimer = setTimeout(hideKbPreview, 200);
}

/* T7：单篇 kb 后台操作统一执行器（弹窗与详情模态共用） */
/* 2026-09-20 两级编译 + L3 概念关系层：
   L1=`_note.md` 六维笔记 / L2=`_wiki.md` 深度编译 / L3=`_relations.md` 概念关系。 */
const KB_LEVELS = ['L1', 'L2', 'L3'];
const KB_LEVEL_TIP = {
  L1: '\u751f\u6210\u516d\u7ef4\u7b14\u8bb0 _note.md',
  L2: '\u751f\u6210\u6df1\u5ea6\u7f16\u8bd1 _wiki.md\uff08\u65b9\u6cd5\u8bba\u6279\u5224 + \u53ef\u590d\u73b0\u6027 + \u5e94\u7528\u8f6c\u5316\uff09',
  L3: '\u751f\u6210\u6982\u5ff5\u5173\u7cfb _relations.md\uff08\u8de8\u6587\u732e\u6982\u5ff5\u5173\u7cfb\u5206\u6790\uff09',
};

function kbNextLevel(it) {
  const done = (it && it.compiled) || [];
  return KB_LEVELS.find(l => !done.includes(l)) || '';
}

function kbCompileBtnHtml(it) {
  const next = kbNextLevel(it);
  if (!next) {
    return '<button class="btn small" disabled title="\u4e09\u7ea7\u7f16\u8bd1\u5df2\u5168\u90e8\u5b8c\u6210'
      + '\uff08L1 _note.md / L2 _wiki.md / L3 _relations.md\uff09">\u2713 \u4e09\u7ea7\u5df2\u7f16\u8bd1\u5b8c\u6210</button>';
  }
  return `<button class="btn small" data-op="compile" data-level="${next}" `
    + `title="${KB_LEVEL_TIP[next]}">编译${next}</button>`;
}

/* 操作结果文案：`skipped_done` 这类原始状态码直接弹给用户很费解（"操作完成：{...}"）。 */
function kbOpResultText(op, r) {
  const brief = JSON.stringify(r).slice(0, 220);
  if (op === 'trash') {
    const st = r && r.status;
    if (st === 'cancelled') return '已取消';
    if (st === 'already_trashed') return 'ℹ 这篇已在回收站里';
    return '🗑 已移出知识库（产物全部保留）——可在知识库面板「🗑 回收站」一键恢复';
  }
  if (op !== 'compile') return '✅ 操作完成：' + brief;
  const st = r && r.status;
  if (st === 'queued') return `✅ 已入队 ${r.level || ''} 编译（后台执行，完成后本页自动反映）`;
  if (st === 'skipped_done') return `ℹ 该等级已编译完成（${r.level || ''}），无需重复入队`;
  if (st === 'skipped_no_doc') return '⚠ 未找到解析产物 document.json，请先完成解析';
  return '✅ 操作完成：' + brief;
}

function kbOpAction(op, doi, level) {
  const headers = { 'Content-Type': 'application/json' };
  switch (op) {
    case 'trash': return ['移出知识库中…', async () => {
      // 2026-09-12 用户需求：移除知识库＝移入回收站（产物全保留、退出检索、可恢复）
      const ok = await askConfirm(
        '把这篇文章移出知识库（回收站）？\n\n'
        + '编译产物、原文层四件、笔记与附件**全部保留**；只是不再出现在知识库、也不再被检索。'
        + '随时可在「🗑 回收站」一键恢复。', { title: '移出知识库' });
      if (!ok) return { status: 'cancelled' };
      return api('/api/kb-meta/kb/trash', { method: 'POST', headers,
                                            body: JSON.stringify({ key: doi }) });
    }];
    case 'compile': return [`编译 ${level || 'L1'} 入队中…`, () => api('/api/kb-meta/compile/queue', { method: 'POST', headers, body: JSON.stringify({ doi, level: level || 'L1' }) }).then(r => { invalidateKbFileSet(null); return r; })];
    case 'sync': return ['纳入知识库中…', () => api('/api/kb-meta/source/sync', { method: 'POST', headers, body: JSON.stringify({ doi, force: false }) }).then(r => { invalidateKbFileSet(null); return r; })];
    case 'resync': return ['重新同步原文中…', () => api('/api/kb-meta/source/sync', { method: 'POST', headers, body: JSON.stringify({ doi, force: true }) }).then(r => { invalidateKbFileSet(null); return r; })];
    case 'verify': return ['校验中…', () => api('/api/kb-meta/source/verify?doi=' + encodeURIComponent(doi))];
  }
  return null;
}

function showKbPreview(row) {
  const pop = $('kbl-preview');
  if (!pop) return;
  const it = (kbView.items || []).find(x => x.doi === row.dataset.doi);
  if (!it) return;
  clearTimeout(kblpHideTimer);
  pop.innerHTML = buildKbPreviewHtml(it);
  pop.querySelectorAll('.kblp-ops button[data-op]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const op = btn.dataset.op;
      const act = kbOpAction(op, it.doi, btn.dataset.level);
      if (!act) return;
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = act[0].replace('…', '');
      try {
        const r = await act[1]();
        const brief = JSON.stringify(r).slice(0, 220);
        if (op === 'verify') {
          const ok = r.ok;
          alert(ok ? '✅ 一致性校验通过：document.json ' + r.expected_count + ' 段 ↔ en.md ' + r.found_count + ' 段' : '❌ 不一致：' + brief);
        } else {
          alert(kbOpResultText(op, r));
        }
        pop.style.display = 'none';   // 列表行可能因状态变化重排 → 关闭弹窗
        await loadKbList();
        await loadPapers();
      } catch (err) { alert('操作失败：' + err.message); }
      finally { btn.disabled = false; btn.textContent = orig; }
    });
  });
  // 定位：fixed 弹窗放到表格右侧（行数多时下方放不下；超出视口右缘则夹紧到视口内）
  const tbl = document.querySelector('.kbl-table');
  const tr = row.getBoundingClientRect();
  const tblRect = tbl ? tbl.getBoundingClientRect() : tr;
  pop.style.display = 'block';
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let left = tblRect.right + 8;
  if (left + pw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - pw - 8);
  let top = Math.max(8, tr.top);
  if (top + ph > window.innerHeight - 8) top = Math.max(8, window.innerHeight - ph - 8);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}

function buildKbPreviewHtml(it) {
  const sf = it.source_files || {};
  const srcCells = [['source.pdf', sf.source_pdf], ['en.md', sf.en_md], ['document.json', sf.document_json], ['images/', sf.images]]
    .map(([name, ok]) => `<span class="kba-badge ${ok ? 'ok' : ''}">${ok ? '✓' : '✗'} ${name}</span>`).join('');
  const compiled = (it.compiled || []).join(', ') || '未编译';
  const queued = (it.queued || []).length ? `<span class="kba-badge warn">排队:${escapeHtml(it.queued.join(','))}</span>` : '';
  const errHtml = it.last_error ? `<div class="kblp-err">⚠ ${escapeHtml(it.last_error)}</div>` : '';
  return `
    <div class="kblp-title">${escapeHtml(it.title || it.doi || '')}</div>
    <div class="kblp-doi">${escapeHtml(it.doi || '')}</div>
    <div class="kblp-meta">
      <span>期刊：<b>${escapeHtml(it.journal || '—')}</b></span>
      <span>年份：<b>${escapeHtml(it.year || '—')}</b></span>
      <span>价值分：<b>${it.value_score != null ? Number(it.value_score).toFixed(2) : '—'}</b></span>
      <span>等级：<b>${escapeHtml(it.level || '—')}</b></span>
    </div>
    <div class="kblp-sec">
      <h6>原文层四件</h6>
      <div>${srcCells}</div>
    </div>
    <div class="kblp-sec">
      <h6>编译状态</h6>
      <div>已完成：${escapeHtml(compiled)} ${queued}</div>
      ${errHtml}
    </div>
    <div class="kblp-ops">
      ${kbCompileBtnHtml(it)}
      <button class="btn small" data-op="sync" title="纳入知识库（library 原文层四件 → kb，冻结不覆盖）">纳入kb</button>
      <button class="btn small" data-op="resync" title="重新同步原文（force 覆盖 kb 原文层）">重新同步</button>
      <button class="btn small" data-op="verify" title="en.md ↔ document.json 一致性校验">校验</button>
    </div>`;
}

function renderKbPager() {
  const pg = $('kb-pager');
  if (!pg) return;
  const pages = Math.max(1, Math.ceil(kbView.total / kbView.pageSize));
  pg.innerHTML = `<button class="btn small" id="kb-prev"${kbView.page <= 1 ? ' disabled' : ''}>‹ 上一页</button>
    <span class="muted">第 ${kbView.page} / ${pages} 页</span>
    <button class="btn small" id="kb-next"${kbView.page >= pages ? ' disabled' : ''}>下一页 ›</button>`;
  const prev = $('kb-prev');
  if (prev) prev.addEventListener('click', () => { if (kbView.page > 1) { kbView.page--; loadKbList(); } });
  const next = $('kb-next');
  if (next) next.addEventListener('click', () => { if (kbView.page < pages) { kbView.page++; loadKbList(); } });
}

function toggleKbView() {
  kbView.mode = kbView.mode === 'list' ? 'tree' : 'list';
  hideKbPreview();   // T7：切换视图时收起悬停预览
  const btn = $('kb-view-toggle');
  if (btn) btn.textContent = kbView.mode === 'list' ? '📁 目录' : '📋 列表';
  const lw = $('kb-list-wrap'), pg = $('kb-pager'), tr = $('kb-tree');
  if (lw) lw.style.display = kbView.mode === 'list' ? '' : 'none';
  if (pg) pg.style.display = kbView.mode === 'list' ? '' : 'none';
  if (tr) tr.style.display = kbView.mode === 'tree' ? '' : 'none';
  if (kbView.mode === 'tree') {
    kbUi.filter = $('kb-search-input').value.trim();
    loadKbTree();
  } else loadKbList();
}

/* 详情模态（T7）：编译状态 / 原文层四件 / 单篇 kb 操作 + 知识库问答入口（提问分支/复核已移至文献库卡片） */
async function openKbDetail(doi) {
  const modal = $('kb-detail-modal');
  const body = $('kbd-body');
  $('kbd-sub').textContent = '';
  hideKbPreview();   // T7：打开详情时收起悬停预览
  modal.style.display = 'flex';
  body.innerHTML = '<div class="empty">加载中…</div>';
  let it = (kbView.items || []).find(x => x.doi === doi);
  if (!it) {
    try {
      const r = await api('/api/kb-meta/kb/list?q=' + encodeURIComponent(doi) + '&page=1&page_size=10');
      it = (r.items || []).find(x => x.doi === doi);
    } catch (e) { /* 保持 null */ }
  }
  if (!it) { body.innerHTML = '<div class="kba-err">未找到该文献：' + escapeHtml(doi) + '</div>'; return; }
  const paper = Object.values(state.paperById).find(p => p.doi === doi);
  const paperId = paper ? paper.id : null;
  const sf = it.source_files || {};
  const srcCells = [['source.pdf', sf.source_pdf], ['en.md', sf.en_md], ['document.json', sf.document_json], ['images/', sf.images]]
    .map(([name, ok]) => `<span class="kba-badge ${ok ? 'ok' : ''}">${ok ? '✓' : '✗'} ${name}</span>`).join('');
  const compiled = (it.compiled || []).join(', ') || '未编译';
  const queued = (it.queued || []).length ? '<p class="muted">排队：' + escapeHtml(it.queued.join(', ')) + '</p>' : '';
  // T7：提问统一 → 知识库问答会话；新建文献会话/识别复核入口移至文献库卡片
  const paperOps = paperId ? `<button class="btn small" data-op="read" title="在阅读器打开该文献">📖 阅读</button>` : '';
  body.innerHTML = `
    <h4>${escapeHtml(it.title || it.doi || '')}</h4>
    <div class="kbd-doi">${escapeHtml(it.doi || '')}</div>
    <div class="kbd-meta">
      <span>期刊：<b>${escapeHtml(it.journal || '—')}</b></span>
      <span>年份：<b>${escapeHtml(it.year || '—')}</b></span>
      <span>价值分：<b>${it.value_score != null ? Number(it.value_score).toFixed(2) : '—'}</b></span>
      <span>等级：<b>${escapeHtml(it.level || '—')}</b></span>
      <span>知识库：<b>${it.in_kb ? '✓ 已纳入' : '✗ 未纳入'}</b></span>
    </div>
    <div class="kbd-section">
      <h5>library / kb 原文层四件</h5>
      <div class="kbd-srcgrid">${srcCells}</div>
    </div>
    <div class="kbd-section">
      <h5>编译状态</h5>
      <div>已完成：${escapeHtml(compiled)}</div>
      ${queued}
      ${it.last_error ? `<div class="kbd-err">⚠ ${escapeHtml(it.last_error)}</div>` : ''}
    </div>
    <div class="kbd-section kbd-ops">
      ${kbCompileBtnHtml(it)}
      <button class="btn small" data-op="sync" title="纳入知识库（library 原文层四件 → kb，冻结不覆盖）">纳入kb</button>
      <button class="btn small" data-op="resync" title="重新同步原文（force 覆盖 kb 原文层）">重新同步</button>
      <button class="btn small" data-op="verify" title="en.md ↔ document.json 一致性校验">校验</button>
      <button class="btn small danger" data-op="trash" title="移出知识库（回收站）：产物全部保留，只是不再显示/不再被检索，可一键恢复">🗑 移出知识库</button>
      ${paperOps}
      <button class="btn small primary" data-op="qa" title="打开/新建知识库问答会话（基于编译产物与元数据）">💬 知识库问答</button>
    </div>`;
  body.querySelectorAll('.kbd-ops button[data-op]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const op = btn.dataset.op;
      if (op === 'read') { modal.style.display = 'none'; selectPaper(paperId); return; }
      // P1：知识库问答入口会新建会话（POST /api/sessions）→ 防连点
      if (op === 'qa') { modal.style.display = 'none'; await guardBtn(btn, () => createKbSession('qa'), '打开中…'); return; }
      const act = kbOpAction(op, doi, btn.dataset.level);
      if (!act) return;
      btn.disabled = true; const orig = btn.textContent; btn.textContent = act[0].replace('…', '');
      try {
        const r = await act[1]();
        const brief = JSON.stringify(r).slice(0, 220);
        if (op === 'verify') {
          const ok = r.ok;
          alert(ok ? '✅ 一致性校验通过：document.json ' + r.expected_count + ' 段 ↔ en.md ' + r.found_count + ' 段' : '❌ 不一致：' + brief);
        } else {
          alert(kbOpResultText(op, r));
        }
        await loadKbList();
        await loadPapers();
        openKbDetail(doi);   // 刷新详情（原文层/编译状态可能变化）
      } catch (err) { alert('操作失败：' + err.message); }
      finally { btn.disabled = false; btn.textContent = orig; }
    });
  });
}

function renderKbTree() {
  const box = $('kb-tree');
  const kw = kbUi.filter.toLowerCase();
  let folders = kbUi.folders;
  if (kw) {
    folders = kbUi.folders
      .map(f => ({ ...f, files: f.files.filter(x => x.name.toLowerCase().includes(kw)) }))
      .filter(f => f.doi_dir.toLowerCase().includes(kw) || f.files.length > 0);
  }
  if (!kbUi.folders.length) { box.innerHTML = '<div class="empty">知识库为空（翻译论文后自动生成）</div>'; return; }
  if (!folders.length) { box.innerHTML = '<div class="empty">无匹配结果</div>'; return; }
  box.innerHTML = `
    <button class="btn btn-sm kb-new" id="kb-new-file">＋ 新建笔记</button>
    ${folders.map(f => renderKbFolder(f)).join('')}`;
  $('kb-new-file').addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbCreateFile(), '新建中…'));
  box.querySelectorAll('.kb-folder-head').forEach(el => {
    el.addEventListener('click', () => {
      const dir = el.dataset.dir;
      kbUi.collapsed[dir] = !kbUi.collapsed[dir];
      const body = el.nextElementSibling;
      if (body) body.style.display = kbUi.collapsed[dir] ? 'none' : '';
      el.querySelector('.kb-caret').textContent = kbUi.collapsed[dir] ? '▸' : '▾';
    });
  });
  box.querySelectorAll('.kb-file').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.kb-op')) return;
      document.querySelectorAll('.kb-file').forEach(x => x.classList.remove('active'));
      el.classList.add('active');
      showReaderFolder(el.dataset.dir, el.dataset.file);
    });
  });
  box.querySelectorAll('.kb-op[data-op]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const { op, dir, file } = btn.dataset;
      // P1：文件树新建/重命名/删除都是写操作（POST/DELETE /api/kb/*）→ 防连点
      if (op === 'rename') guardBtn(btn, () => kbRename(dir, file), '…');
      else if (op === 'delete') guardBtn(btn, () => kbDelete(dir, file), '…');
    });
  });
}

function renderKbFolder(f) {
  const isCollapsed = !!kbUi.collapsed[f.doi_dir];
  return `
    <div class="kb-folder">
      <div class="kb-folder-head" data-dir="${escapeHtml(f.doi_dir)}">
        <span class="kb-caret">${isCollapsed ? '▸' : '▾'}</span> 📁 ${escapeHtml(f.doi_dir)}
        <span class="kb-ops">
          <button class="kb-op" data-op="rename" data-dir="${escapeHtml(f.doi_dir)}" title="重命名">✎</button>
          <button class="kb-op" data-op="delete" data-dir="${escapeHtml(f.doi_dir)}" title="删除文件夹">✕</button>
        </span>
      </div>
      <div class="kb-folder-files" style="${isCollapsed ? 'display:none' : ''}">
        ${f.files.map(x => `
          <div class="kb-file" data-dir="${escapeHtml(f.doi_dir)}" data-file="${escapeHtml(x.name)}">
            <span class="kb-file-name">${escapeHtml(x.name)}</span>
            <span class="kb-ops">
              <button class="kb-op" data-op="rename" data-dir="${escapeHtml(f.doi_dir)}" data-file="${escapeHtml(x.name)}" title="重命名">✎</button>
              <button class="kb-op" data-op="delete" data-dir="${escapeHtml(f.doi_dir)}" data-file="${escapeHtml(x.name)}" title="删除">✕</button>
            </span>
          </div>`).join('')}
      </div>
    </div>`;
}

async function kbCreateFile() {
  const folder = prompt('新建笔记所属文件夹（输入 DOI 目录名或新的文件夹名）：');
  if (!folder) return;
  const name = prompt('文件名（如 _note.md）：');
  if (!name) return;
  const path = folder.replace(/[/\\]+/g, '_') + '/' + name;
  try {
    await api('/api/kb/file/new', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, content: '' }),
    });
    await loadKbTree();
  } catch (e) { alert('新建失败：' + e.message); }
}

async function kbRename(dir, file) {
  const old = file ? `${dir}/${file}` : dir;
  const newName = prompt('新的名称：', file || dir);
  if (!newName || newName === (file || dir)) return;
  try {
    await api('/api/kb/rename', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: old, new_name: newName }),
    });
    await loadKbTree();
  } catch (e) { alert('重命名失败：' + e.message); }
}

async function kbDelete(dir, file) {
  const target = file ? `${dir}/${file}` : dir;
  if (!(await askConfirm(`删除「${target}」？${file ? '' : '目录须为空。'}此操作不可恢复。`))) return;
  try {
    await api(`/api/kb/file?path=${encodeURIComponent(target)}`, { method: 'DELETE' });
    await loadKbTree();
  } catch (e) { alert('删除失败：' + e.message); }
}


/* ─── 知识库管理 M5（从 app.js 合并） ─── */

/* ══════════ 知识库管理（2-tab：总览 + 文献管理）══════════ */
const kbaPapersState = {
  page: 1, pageSize: 50, total: 0, items: [],
  selected: new Set(),
  chip: 'all',
  sort: 'value',
};

function bindKbAdmin() {
  $('kb-admin-btn').addEventListener('click', openKbAdmin);
  $('kba-close').addEventListener('click', () => { $('kb-admin-modal').style.display = 'none'; });
  document.querySelectorAll('#kb-admin-modal .stab').forEach(btn => {
    btn.addEventListener('click', () => setKbaTab(btn.dataset.stab));
  });
  const on = (id, fn, opts) => { const el = $(id); if (el) el.addEventListener(opts?.ev || 'click', fn); };
  on('kba-papers-search', (e) => { if (e.key === 'Enter') { kbaPapersState.page = 1; kbaPapersList(); } }, { ev: 'keydown' });
  on('kba-papers-go', () => { kbaPapersState.page = 1; kbaPapersList(); });
  document.querySelectorAll('#kba-papers-chips .chip').forEach(c => {
    c.addEventListener('click', () => {
      document.querySelectorAll('#kba-papers-chips .chip').forEach(x => x.classList.remove('active'));
      c.classList.add('active');
      kbaPapersState.chip = c.dataset.filter;
      kbaPapersState.page = 1;
      kbaPapersList();
    });
  });
  ['kba-filter-quartile', 'kba-filter-year', 'kba-filter-score', 'kba-filter-if'].forEach(id => {
    on(id, () => { kbaPapersState.page = 1; kbaPapersList(); }, { ev: 'change' });
  });
  on('kba-check-all', (e) => kbaToggleSelectAll(e.currentTarget.checked));
  on('kba-select-all', (e) => kbaToggleSelectAll(e.currentTarget.checked));
  on('kba-batch-retry', (e) => guardBtn(e.currentTarget, () => kbaBatchRetry(), '\u7f16\u8bd1\u4e2d\u2026'));
  on('kba-batch-l3', (e) => guardBtn(e.currentTarget, () => kbaBatchL3(), 'L3\u4e2d\u2026'));
}

function setKbaTab(tab) {
  document.querySelectorAll('#kb-admin-modal .stab').forEach(b => b.classList.toggle('active', b.dataset.stab === tab));
  ['overview', 'papers'].forEach(p => {
    const el = $('kba-' + p);
    if (el) el.style.display = p === tab ? '' : 'none';
  });
  if (tab === 'overview') kbaOverview();
  if (tab === 'papers') kbaPapersList();
}

async function kbaOverview() {
  const box = $('kba-overview-content');
  box.innerHTML = '<div class="kba-msg">\u52a0\u8f7d\u77e5\u8bc6\u5e93\u603b\u89c8\u2026</div>';
  try {
    const s = await api('/api/kb-meta/stats');
    const sc = s.scale, cp = s.compile;
    box.innerHTML =
      '<h4>\u{1f4c8} \u89c4\u6a21\u4e0e\u7f16\u8bd1\u5b8c\u6210\u5ea6</h4>' +
      '<div class="kba-stats-grid">' +
      '<div class="kba-stat"><b>' + sc.meta_count + '</b><span>\u5143\u6570\u636e\u7bc7(bib)</span></div>' +
      '<div class="kba-stat"><b>' + sc.library_papers + '</b><span>\u89e3\u6790\u5e93(library)</span></div>' +
      '<div class="kba-stat"><b>' + sc.kb_papers + '</b><span>\u77e5\u8bc6\u5e93(kb)</span></div>' +
      '<div class="kba-stat"><b>' + cp.l1_done + '</b><span>L1 \u5b8c\u6210</span></div>' +
      '<div class="kba-stat"><b>' + cp.l2_done + '</b><span>L2 \u5b8c\u6210</span></div>' +
      '<div class="kba-stat"><b>' + cp.l3_done + '</b><span>L3 \u5b8c\u6210</span></div>' +
      '</div>' +
      '<div class="kba-detail" style="margin-top:8px">\u7f16\u8bd1\u961f\u5217\uff1a\u5f85\u5904\u7406 <b>' + cp.queued +
      '</b> \u00b7 \u7f16\u8bd1\u4e2d <b>' + cp.processing + '</b> \u00b7 \u5931\u8d25 <b>' + cp.failed + '</b></div>' +
      '<div class="kba-tools-section">' +
      '<h4>\u{1f6e0} \u7d22\u5f15\u7ef4\u62a4</h4>' +
      '<div class="form-actions">' +
      '<button class="btn small" id="kba-fts-rebuild">\u91cd\u5efa FTS \u7d22\u5f15</button>' +
      '<button class="btn small" id="kba-vector-rebuild">\u91cd\u5efa\u5411\u91cf\u7d22\u5f15</button>' +
      '</div>' +
      '<div id="kba-compile-msg" class="kba-result"></div>' +
      '</div>' +
      '<h4>\u{1f4d6} \u5404\u7ea7\u7f16\u8bd1\u542b\u4e49</h4>' +
      '<div class="kba-detail">' +
      '<p><b>L1 \u77e5\u8bc6\u7b14\u8bb0</b>\uff1a\u5165\u5e93\u81ea\u52a8\u89e6\u53d1\uff0c\u8f93\u51fa _note.md\uff08\u4e00\u53e5\u8bdd\u8d21\u732e + \u516d\u7ef4\u7b14\u8bb0 + \u6982\u5ff5\u6807\u7b7e\uff09\u3002</p>' +
      '<p><b>L2 \u6df1\u5ea6\u7f16\u8bd1</b>\uff1a\u65b9\u6cd5\u8bba\u6279\u5224 + \u53ef\u590d\u73b0\u6027 + \u5e94\u7528\u8f6c\u5316 \u2192 _wiki.md\u3002</p>' +
      '<p><b>L3 \u6982\u5ff5\u5173\u7cfb</b>\uff1a\u8de8\u6587\u732e\u6982\u5ff5\u5173\u7cfb\u5206\u6790 \u2192 _relations.md\uff08\u7701 77% token\uff09\u3002</p>' +
      '</div>';
  } catch (e) { box.innerHTML = kbaErr(e); return; }
  const fb = $('kba-fts-rebuild');
  if (fb) fb.addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaFtsRebuild(), '\u91cd\u5efa\u4e2d\u2026'));
  const vb = $('kba-vector-rebuild');
  if (vb) vb.addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaVectorRebuild(), '\u91cd\u5efa\u4e2d\u2026'));
}

async function openKbAdmin() {
  $('kb-admin-modal').style.display = 'flex';
  const active = document.querySelector('#kb-admin-modal .stab.active');
  setKbaTab(active ? active.dataset.stab : 'overview');
  try {
    const st = await api('/api/kb-meta/status');
    $('kba-status').textContent = '\u00b7 \u5143\u6570\u636e ' + st.meta_count + ' \u7bc7';
  } catch (e) { $('kba-status').textContent = '\u00b7 ' + e.message; }
}

function kbaOut(id, html) { $(id).innerHTML = html; }
function kbaErr(e) { return '<div class="kba-err">\u26a0 ' + escapeHtml(e.message || String(e)) + '</div>'; }

/* ---------------- 文献管理 tab ---------------- */
async function kbaPapersList() {
  const body = $('kba-papers-body');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="8" class="muted">\u52a0\u8f7d\u4e2d\u2026</td></tr>';
  const ps = new URLSearchParams({
    page: kbaPapersState.page, page_size: kbaPapersState.pageSize, sort: 'value',
  });
  const q = ($('kba-papers-search') || {}).value || '';
  if (q.trim()) ps.set('q', q.trim());
  const chip = kbaPapersState.chip;
  if (chip === 'compiled') ps.set('compile_status', 'done');
  else if (chip === 'failed') ps.set('compile_status', 'failed');
  else if (chip === 'l3') ps.set('compile_status', 'l2');
  const quartile = ($('kba-filter-quartile') || {}).value || '';
  if (quartile) ps.set('quartile', quartile);
  const year = ($('kba-filter-year') || {}).value || '';
  if (year) ps.set('year_from', year);
  const scoreMin = ($('kba-filter-score') || {}).value || '';
  if (scoreMin) ps.set('score_min', scoreMin);
  const minIf = ($('kba-filter-if') || {}).value || '';
  if (minIf) ps.set('min_if', minIf);
  try {
    const r = await api('/api/kb-meta/kb/list?' + ps.toString());
    kbaPapersState.items = r.items || [];
    kbaPapersState.total = r.total || 0;
    kbaPapersState.selected.clear();
    const ca = $('kba-check-all'); if (ca) ca.checked = false;
    kbaPapersRender();
  } catch (e) { body.innerHTML = '<tr><td colspan="8">' + escapeHtml(e.message) + '</td></tr>'; }
}

function kbaPapersRender() {
  const body = $('kba-papers-body');
  if (!body) return;
  const items = kbaPapersState.items;
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="8" class="muted">\u65e0\u5339\u914d\u6587\u732e\uff08\u5171 ' + kbaPapersState.total + ' \u7bc7\uff09</td></tr>';
    kbaPapersUpdatePager();
    return;
  }
  const rows = items.map(it => {
    const compiled = it.compiled || [];
    const top = compiled.slice().sort().pop() || '';
    const hasError = !!it.last_error;
    const hasL3 = compiled.includes('L3');
    const failedLv = it.last_failed_level || '';
    let stClass = 'st-none', stText = '\u672a\u7f16\u8bd1';
    if (hasL3) { stClass = 'st-l3'; stText = 'L3 \u5b8c\u6210'; }
    else if (hasError && top) { stClass = 'st-fail'; stText = top + ' \u5b8c\u6210\u00b7' + (failedLv || 'L3') + ' \u5931\u8d25'; }
    else if (hasError) { stClass = 'st-fail'; stText = '\u5931\u8d25'; }
    else if (top === 'L2') { stClass = 'st-l2'; stText = 'L2 \u5b8c\u6210'; }
    else if (top === 'L1') { stClass = 'st-l1'; stText = 'L1 \u5b8c\u6210'; }
    const doi = escapeHtml(it.doi || '');
    const checked = kbaPapersState.selected.has(it.doi) ? ' checked' : '';
    const ifVal = it.impact_factor ? Number(it.impact_factor).toFixed(1) : '\u2014';
    const qVal = it.quartile || '\u2014';
    const scoreVal = it.value_score != null ? Number(it.value_score).toFixed(1) : '\u2014';
    let actions = '';
    if (hasError) actions = '<button class="btn small" data-act="retry" data-doi="' + doi + '">\u91cd\u8bd5</button>';
    else if (hasL3) actions = '<span class="muted">\u2014</span>';
    else if (it.l3_eligible) actions = '<button class="btn small" data-act="l3" data-doi="' + doi + '">\u5347\u7ea7L3</button>';
    else actions = '<span class="muted" title="\u4ef7\u503c\u5206\u672a\u8fbeL3\u95e8\u69db(4.0)\u6216AI\u8bc4\u5206\u7f3a\u5931">\u2014</span>';
    return '<tr>' +
      '<td><input type="checkbox" data-doi="' + doi + '"' + checked + '></td>' +
      '<td title="' + escapeHtml(it.title || '') + '">' + escapeHtml(shortTitle(it.title || it.doi || '', 50)) +
        '<div class="muted" style="font-size:11px">' + doi + '</div></td>' +
      '<td>' + escapeHtml(it.journal || '\u2014') + '<div class="muted" style="font-size:11px">' + escapeHtml(it.year || '') + '</div></td>' +
      '<td>' + ifVal + '</td>' +
      '<td>' + escapeHtml(qVal) + '</td>' +
      '<td>' + scoreVal + '</td>' +
      '<td><span class="kba-status-chip ' + stClass + '">' + stText + '</span></td>' +
      '<td>' + actions + '</td></tr>';
  }).join('');
  body.innerHTML = rows;
  body.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) kbaPapersState.selected.add(cb.dataset.doi);
      else kbaPapersState.selected.delete(cb.dataset.doi);
      kbaUpdateBatchBar();
    });
  });
  body.querySelectorAll('button[data-act]').forEach(btn => {
    btn.addEventListener('click', () => kbaSingleAction(btn.dataset.act, btn.dataset.doi));
  });
  kbaPapersUpdatePager();
  kbaUpdateBatchBar();
}

function kbaPapersUpdatePager() {
  const pg = $('kba-papers-pager');
  if (!pg) return;
  const pages = Math.max(1, Math.ceil(kbaPapersState.total / kbaPapersState.pageSize));
  pg.innerHTML = '<button class="btn small" id="kba-pager-prev"' + (kbaPapersState.page <= 1 ? ' disabled' : '') + '>\u2039 \u4e0a\u4e00\u9875</button>' +
    '<span class="muted">\u7b2c ' + kbaPapersState.page + ' / ' + pages + ' \u9875\uff0c\u5171 ' + kbaPapersState.total + ' \u7bc7</span>' +
    '<button class="btn small" id="kba-pager-next"' + (kbaPapersState.page >= pages ? ' disabled' : '') + '>\u4e0b\u4e00\u9875 \u203a</button>';
  const prev = $('kba-pager-prev');
  if (prev) prev.addEventListener('click', () => { if (kbaPapersState.page > 1) { kbaPapersState.page--; kbaPapersList(); } });
  const next = $('kba-pager-next');
  if (next) next.addEventListener('click', () => { if (kbaPapersState.page < pages) { kbaPapersState.page++; kbaPapersList(); } });
}

function kbaToggleSelectAll(checked) {
  if (checked) kbaPapersState.items.forEach(it => { if (it.doi) kbaPapersState.selected.add(it.doi); });
  else kbaPapersState.selected.clear();
  const body = $('kba-papers-body');
  if (body) body.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = checked; });
  kbaUpdateBatchBar();
}

function kbaUpdateBatchBar() {
  const bar = $('kba-batch-bar');
  if (!bar) return;
  const n = kbaPapersState.selected.size;
  bar.style.display = n > 0 ? '' : 'none';
  const label = $('kba-selected-count');
  if (label) label.textContent = '\u5df2\u9009 ' + n + ' \u7bc7';
}

async function kbaBatchRetry() {
  const dois = [...kbaPapersState.selected];
  if (!dois.length) return;
  try {
    const r = await api('/api/kb-meta/compile/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dois, action: 'retry' }),
    });
    alert('\u2705 \u91cd\u8bd5\u5b8c\u6210\uff1a\u6210\u529f ' + r.success + ' / \u5931\u8d25 ' + r.failed + ' / \u8df3\u8fc7 ' + r.skipped);
    kbaPapersList();
  } catch (e) { alert('\u274c ' + e.message); }
}

async function kbaBatchL3() {
  const dois = [...kbaPapersState.selected];
  if (!dois.length) return;
  try {
    const r = await api('/api/kb-meta/compile/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dois, action: 'l3' }),
    });
    alert('\u2705 L3 \u5b8c\u6210\uff1a\u6210\u529f ' + r.success + ' / \u5931\u8d25 ' + r.failed + ' / \u8df3\u8fc7 ' + r.skipped);
    kbaPapersList();
  } catch (e) { alert('\u274c ' + e.message); }
}

async function kbaSingleAction(action, doi) {
  try {
    const act = action === 'retry' ? 'retry' : 'l3';
    const r = await api('/api/kb-meta/compile/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dois: [doi], action: act }),
    });
    if (r.failed) alert('\u274c \u5931\u8d25');
    else alert('\u2705 \u5b8c\u6210');
    kbaPapersList();
  } catch (e) { alert('\u274c ' + e.message); }
}

async function kbaFtsRebuild() {
  kbaOut('kba-compile-msg', '<div class="kba-msg">\u91cd\u5efa FTS \u7d22\u5f15\u4e2d\u2026</div>');
  try {
    const r = await api('/api/kb-meta/fts/rebuild', { method: 'POST' });
    kbaOut('kba-compile-msg', '<div class="kba-ok">\u2705 \u91cd\u5efa\u5b8c\u6210\uff1a' + escapeHtml(JSON.stringify(r)) + '</div>');
  } catch (e) { kbaOut('kba-compile-msg', kbaErr(e)); }
}

async function kbaVectorRebuild() {
  kbaOut('kba-compile-msg', '<div class="kba-msg">\u91cd\u5efa\u5411\u91cf\u7d22\u5f15\u4e2d\uff08\u8017\u65f6\u8f83\u957f\uff09\u2026</div>');
  try {
    const r = await api('/api/kb-meta/vector/rebuild', { method: 'POST' });
    kbaOut('kba-compile-msg', '<div class="kba-ok">\u2705 \u5411\u91cf\u7d22\u5f15\u91cd\u5efa\u5b8c\u6210\uff1a' + escapeHtml(JSON.stringify(r)) + '</div>');
  } catch (e) { kbaOut('kba-compile-msg', kbaErr(e)); }
}


boot();
