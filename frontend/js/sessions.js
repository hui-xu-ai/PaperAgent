/* ══════════════════════════════════════════════════════════
   PaperAgent 前端 — 会话管理（从 app.js 拆出）
   加载顺序：utils.js → ui.js → sessions.js → lit-admin.js → app.js
   ══════════════════════════════════════════════════════════ */
'use strict';

/* ══════════ 会话管理（U1 基础版，U2 完善）══════════ */
async function loadSessions() {
  try {
    const data = await api('/api/sessions');
    state.sessions = data.sessions;
    renderSessions();
    renderChatSwitch();  // P2-7：最近对话切换下拉刷新
  } catch (e) { console.warn('loadSessions failed', e); }
}

/* P2-7：最近对话快速切换（随 #main 移动，弹出后的问答助手同样可用） */
function renderChatSwitch() {
  const sel = $('chat-switch');
  if (!sel) return;
  // P5-E1：排除 paper 会话支线（文献会话经阅读器/知识库详情管理）
  const recent = [...state.sessions].filter(s => s.kind !== 'paper')
    .sort((a, b) => b.id - a.id).slice(0, 12);
  sel.innerHTML = '<option value="">⇄ 最近对话</option>' + recent.map(s =>
    `<option value="${s.id}">${escapeHtml(shortTitle(sessionTitle(s), 26))}</option>`).join('');
  sel.value = state.sessionId ? String(state.sessionId) : '';
}

function sessionTitle(s) {
  if (s.title) return s.title;                 // P2-7：用户自定义名优先
  if (s.display_title) return s.display_title;
  if (s.kind === 'global') return s.mode === 'manage' ? '知识库管理' : '知识库问答';
  if (s.kind === 'lit') return 'AI检索';
  if (s.kind === 'chat') return '普通聊天';
  return s.paper_title || `文献会话 #${s.id}`;
}

/* P5-E1：会话批量管理（前端归档：localStorage 标记隐藏，清空已归档=批量删除） */
const sessSel = new Set();                 // 批量选择（会话 id 字符串集合）
const sessUi = { archOpen: false };        // 已归档组展开态（15s 轮询重建不丢）
const ARCH_KEY = 'p5-archived-sessions';
function archivedIds() {
  try { const a = JSON.parse(localStorage.getItem(ARCH_KEY) || '[]'); return Array.isArray(a) ? a : []; }
  catch (e) { return []; }
}
function setArchived(ids) { localStorage.setItem(ARCH_KEY, JSON.stringify(ids)); }
function toggleArchiveSession(id) {
  const cur = archivedIds();
  const a = cur.includes(String(id)) ? cur.filter(x => x !== String(id)) : cur.concat(String(id));
  setArchived(a);
  renderSessions();
}
function updateBatchBar() {
  const all = $('sess-select-all'), del = $('sess-batch-del'), cnt = $('sess-selected');
  if (!cnt) return;
  cnt.textContent = '已选 ' + sessSel.size;
  if (del) del.disabled = sessSel.size === 0;
  if (all) {
    const arch = new Set(archivedIds());
    const visible = state.sessions.filter(s => s.kind !== 'paper' && !arch.has(String(s.id)));
    all.checked = visible.length > 0 && visible.every(s => sessSel.has(String(s.id)));
  }
}
async function batchDeleteSessions() {
  if (!sessSel.size) return;
  if (!(await askConfirm(`删除所选 ${sessSel.size} 个会话？其全部消息将不可恢复。`))) return;
  const ids = [...sessSel];
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await api(`/api/sessions/${id}`, { method: 'DELETE' }); ok++; }
    catch (e) { fail++; }
  }
  sessSel.clear();
  await loadSessions();
  appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'system',
                message: `批量删除会话：成功 ${ok} 个${fail ? '，失败 ' + fail + ' 个' : ''}` });
  if (fail) alert('部分删除失败：' + fail + ' 个');
}
async function purgeArchivedSessions() {
  const arch = archivedIds();
  if (!arch.length) { alert('没有已归档会话'); return; }
  if (!(await askConfirm(`永久删除全部 ${arch.length} 个已归档会话？其消息不可恢复。`))) return;
  let ok = 0, fail = 0;
  for (const id of arch) {
    try { await api(`/api/sessions/${id}`, { method: 'DELETE' }); ok++; }
    catch (e) { fail++; }
  }
  setArchived([]);
  await loadSessions();
  appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'system',
                message: `清空已归档：成功 ${ok} 个${fail ? '，失败 ' + fail + ' 个' : ''}` });
  if (fail) alert('部分删除失败：' + fail + ' 个');
}
function bindSessionBatch() {
  const all = $('sess-select-all'), del = $('sess-batch-del'), purge = $('sess-purge-arch');
  if (!all || !del || !purge) return;
  all.addEventListener('change', () => {
    const arch = new Set(archivedIds());
    sessSel.clear();
    if (all.checked) {
      state.sessions.filter(s => s.kind !== 'paper' && !arch.has(String(s.id)))
        .forEach(s => sessSel.add(String(s.id)));
    }
    renderSessions();
  });
  // P1：批量删除/清空归档是写操作（DELETE 循环）→ guardBtn 防连点
  // （guardBtn 会恢复点击前的 disabled；删除后选择集已清空，故收尾再按当前选择重算一次）
  del.addEventListener('click', async (e) => {
    await guardBtn(e.currentTarget, () => batchDeleteSessions(), '删除中…');
    updateBatchBar();
  });
  purge.addEventListener('click', (e) => guardBtn(e.currentTarget, () => purgeArchivedSessions(), '清空中…'));
}

/* O批2：会话按创建日期分组 + 日期过滤（固定时间范围 chips，不随使用时长堆积） */
function sessionDate(s) {
  const c = (s.created_at || '');
  return c ? c.slice(0, 10) : '未标注日期';
}

function _sessRangeBoundaries() {
  const now = new Date();
  const today = now.toISOString().slice(0, 10);
  const y = new Date(now); y.setDate(y.getDate() - 1);
  const yesterday = y.toISOString().slice(0, 10);
  const weekStart = new Date(now);
  const dow = weekStart.getDay() || 7; // Mon=1
  weekStart.setDate(weekStart.getDate() - dow + 1);
  const weekStartStr = weekStart.toISOString().slice(0, 10);
  const monthStart = now.toISOString().slice(0, 7) + '-01';
  return { today, yesterday, weekStartStr, monthStart };
}

function sessInRange(key, d) {
  const b = _sessRangeBoundaries();
  switch (key) {
    case 'today': return d === b.today;
    case 'yesterday': return d === b.yesterday;
    case 'week': return d >= b.weekStartStr;
    case 'month': return d >= b.monthStart;
    case 'older': return d < b.monthStart;
    default: return true;
  }
}

const SESS_DATE_RANGES = [
  { key: 'all', label: '全部' },
  { key: 'today', label: '今天' },
  { key: 'yesterday', label: '昨天' },
  { key: 'week', label: '本周' },
  { key: 'month', label: '本月' },
  { key: 'older', label: '更早' },
];

function renderSessDates(active) {
  const el = $('sess-dates');
  if (!el) return;
  const chip = (r) =>
    `<button class="pd-chip ${r.key === active ? 'active' : ''}" data-date="${r.key}" title="${r.key === 'all' ? '显示全部日期' : '仅显示 ' + r.label + ' 会话'}">${r.label}</button>`;
  el.innerHTML = SESS_DATE_RANGES.map(chip).join('');
  el.querySelectorAll('.pd-chip').forEach(c => {
    c.addEventListener('click', () => {
      if (c.dataset.date === state.sessDate) return;
      state.sessDate = c.dataset.date;
      renderSessions();
    });
  });
}

function renderSessions() {
  const box = $('session-list');
  // G14：文献会话由「文献库」/知识库详情管理；会话 tab 只保留 global/chat
  const others = state.sessions.filter(s => s.kind !== 'paper');
  if (!others.length) {
    box.innerHTML = '<div class="empty">暂无普通/知识库会话<br>（文献会话请到「文献库」或知识库详情）</div>';
    updateBatchBar();
    return;
  }
  const arch = new Set(archivedIds());
  const allVisible = others.filter(s => !arch.has(String(s.id)));
  const archivedList = others.filter(s => arch.has(String(s.id)));
  const kindLabel = s => s.kind === 'global' ? (s.mode === 'manage' ? '管' : '知') : { chat: '聊', paper: '文', lit: '检' }[s.kind] || '文';
  // O批2：按创建日期过滤 + 按日期分组（每项仍带类型徽标）
  renderSessDates(state.sessDate);
  const filtered = state.sessDate === 'all' ? allVisible
    : allVisible.filter(s => sessInRange(state.sessDate, sessionDate(s)));
  const byDate = {};
  filtered.forEach(s => { const d = sessionDate(s); (byDate[d] = byDate[d] || []).push(s); });
  let html = Object.keys(byDate).sort((a, b) => b.localeCompare(a)).map(d =>
    `<div class="sess-date-head">📅 ${d}<span class="cnt">${byDate[d].length} 个会话</span></div>${byDate[d].map(s => sessionItemHtml(s, kindLabel(s))).join('')}`
  ).join('') || (state.sessDate !== 'all' ? '<div class="empty">该日期无会话</div>' : '');
  if (archivedList.length) {
    html += `<div class="sess-group">
      <div class="sess-group-title sess-arch-head" id="sess-arch-head" title="点击展开/收起">
        <span id="sess-arch-caret">${sessUi.archOpen ? '▾' : '▸'}</span> 📥 已归档（${archivedList.length}）
      </div>
      <div class="sess-arch-body" id="sess-arch-body" style="${sessUi.archOpen ? '' : 'display:none'}">
        ${archivedList.map(s => sessionItemHtml(s, kindLabel(s))).join('')}
      </div></div>`;
  }
  box.innerHTML = html;
  // 打开会话（排除行内控件）
  box.querySelectorAll('.sess-row').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.s-sel') || e.target.closest('.s-del') || e.target.closest('.s-ren')
          || e.target.closest('.s-exp') || e.target.closest('.s-arch')) return;
      openSession(Number(el.dataset.id));
    });
  });
  box.querySelectorAll('.s-sel').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) sessSel.add(cb.dataset.sel); else sessSel.delete(cb.dataset.sel);
      updateBatchBar();
    });
  });
  // 使用事件委托绑定按钮事件（更可靠，避免 DOM 重渲染后事件丢失）
  box.onclick = (e) => {
    const renBtn = e.target.closest('.s-ren');
    if (renBtn) {
      e.stopPropagation();
      guardBtn(renBtn, () => renameSession(Number(renBtn.dataset.ren)), '…');
      return;
    }
    const delBtn = e.target.closest('.s-del');
    if (delBtn) {
      e.stopPropagation();
      guardBtn(delBtn, () => deleteSession(Number(delBtn.dataset.del)), '…');
      return;
    }
    const expBtn = e.target.closest('.s-exp');
    if (expBtn) {
      e.stopPropagation();
      const det = $('sdet-' + expBtn.dataset.exp);
      if (!det) return;
      const open = det.style.display !== 'none';
      det.style.display = open ? 'none' : '';
      expBtn.textContent = open ? '▾' : '▸';
      return;
    }
    const archBtn = e.target.closest('.s-arch');
    if (archBtn) {
      e.stopPropagation();
      toggleArchiveSession(Number(archBtn.dataset.arch));
      return;
    }
  };
  const ah = $('sess-arch-head');
  if (ah) ah.addEventListener('click', () => {
    sessUi.archOpen = !sessUi.archOpen;
    const body = $('sess-arch-body');
    if (body) body.style.display = sessUi.archOpen ? '' : 'none';
    const caret = $('sess-arch-caret');
    if (caret) caret.textContent = sessUi.archOpen ? '▾' : '▸';
  });
  updateBatchBar();
}

/* P2-7：重命名会话 */
async function renameSession(id) {
  const s = state.sessions.find(x => x.id === id);
  if (!s) return;
  const name = await askInput('重命名对话：', sessionTitle(s), { title: '重命名会话' });
  if (!name || !name.trim()) return;
  const t = name.trim();
  if (t === sessionTitle(s)) return;
  try {
    await api(`/api/sessions/${id}/rename`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: t }),
    });
    await loadSessions();
  } catch (e) { alert('重命名失败：' + e.message); }
}

function sessionItemHtml(s, kindLabel) {
  const isArch = archivedIds().includes(String(s.id));
  const checked = sessSel.has(String(s.id)) ? 'checked' : '';
  const detail = `<div class="session-detail" id="sdet-${s.id}" style="display:none">
    创建 ${escapeHtml(s.created_at || '—')} · ${s.tokens ?? 0} token · ${s.messages ?? 0} 条消息
    ${s.kind === 'global' ? ' · ' + (s.mode === 'manage' ? '管理模式' : '问答模式') : ''}
    ${s.kind === 'lit' ? ' · AI检索会话' : ''}
    ${s.paper_title ? ' · ' + escapeHtml(shortTitle(s.paper_title, 30)) : ''}
  </div>`;
  return `<div class="sess-row" data-id="${s.id}">
    <div class="session-item ${s.id === state.sessionId ? 'active' : ''} ${isArch ? 'archived' : ''}" data-id="${s.id}">
      <input type="checkbox" class="s-sel" data-sel="${s.id}" ${checked} title="选择（批量操作）">
      <span class="s-kind ${s.kind}">${kindLabel || '文'}</span>
      <span class="s-title" title="点击打开">${escapeHtml(shortTitle(sessionTitle(s), 16))}</span>
      <span class="s-msgs">${s.messages}</span>
      <button class="s-exp" data-exp="${s.id}" title="展开/收起详情">▾</button>
      <button class="s-arch" data-arch="${s.id}" title="${isArch ? '取消归档（回到列表）' : '归档（从列表隐藏，可清空）'}">${isArch ? '📤' : '📥'}</button>
      <button class="s-ren" data-ren="${s.id}" title="重命名对话">✎</button>
      <button class="s-del" data-del="${s.id}" title="删除会话">✕</button>
    </div>
    ${detail}
  </div>`;
}

async function deleteSession(id) {
  const s = state.sessions.find(x => x.id === id);
  const label = s ? sessionTitle(s) : String(id);
  if (!(await askConfirm(`删除会话「${label}」？其全部消息将不可恢复。`))) return;
  try {
    await api(`/api/sessions/${id}`, { method: 'DELETE' });
    if (state.sessionId === id) {
      state.sessionId = null;
      state.sessionKind = null;
      $('chat-title').textContent = '选择会话或新建';
      $('chat-sub').textContent = '';
      $('messages').innerHTML = '<div class="empty">会话已删除，点「＋新建会话」开始</div>';
    }
    await loadSessions();
  } catch (e) { alert('删除失败：' + e.message); }
}

async function openSession(id) {
  let s = state.sessions.find(x => x.id === id);
  // P2-4：会话列表可能过期（新建会话后未刷新）→ 重拉一次再找，避免"找不到会话"静默失败
  if (!s) {
    await loadSessions();
    s = state.sessions.find(x => x.id === id);
  }
  if (!s) return;
  state.sessionId = id;
  state.sessionKind = s.kind;
  state.currentPaperId = s.paper_id || null;
  renderSessions();
  renderPapers();
  $('chat-title').textContent = sessionTitle(s);
  $('chat-sub').textContent = s.kind === 'global'
    ? (s.mode === 'manage' ? '⚙️ 知识库管理 · 问答 + 工具调用' : '📚 知识库问答 · 基于编译产物')
    : (s.kind === 'lit'
      ? '🔍 AI检索 · 文献检索/管理/问答'
      : (s.kind === 'paper'
      ? (s.paper_title ? shortTitle(s.paper_title, 40) + ' · ' : '') + '基于原文（en.md）+ 编译笔记'
      : ''));
  updateInputContext();
  await loadHistory(id);
  // 2026-09-12 用户反馈：切换文献提问会话时，右侧 md 阅读区闪烁。
  // 旧代码**无条件** renderPaperReader(s.paper_id) → showReaderFolder 把 tab 重置回
  // `_note.md` → loadReaderFile 先写「加载中…」再整块 innerHTML 替换（marked.parse 重算）。
  // 同一篇既然已经在右侧看着，就不该重载。
  if (s.kind === 'paper' && s.paper_id
      && String(readerState.paperId ?? '') !== String(s.paper_id)) {
    renderPaperReader(s.paper_id);
  }
  renderChatSwitch();  // P2-7：切换后下拉选中同步
  updateSendBtn();
  updateTokenBar();
}

/* ══════════ 新建会话菜单 ══════════ */
function bindNewSession() {
  const menu = $('new-session-menu');
  $('new-session-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    const show = menu.style.display === 'none';
    menu.style.display = show ? 'block' : 'none';
    if (show) {
      // P2-6：菜单跟随左栏「新建会话」按钮（不再固定在顶栏下方）
      const m = menu.querySelector('.menu');
      const r = e.currentTarget.getBoundingClientRect();
      m.style.top = (r.bottom + 6) + 'px';
      m.style.left = r.left + 'px';
      m.style.transform = 'none';
    }
  });
  menu.addEventListener('click', async (e) => {
    const item = e.target.closest('.menu-item');
    if (!item) return;
    menu.style.display = 'none';
    // P1：新建会话是写操作（POST /api/sessions）→ 防连点
    if (item.dataset.kind === 'global') await guardBtn(item, () => createKbSession(item.dataset.mode || 'qa'), '创建中…');
    else if (item.dataset.kind === 'chat') await guardBtn(item, () => createChatSession(), '创建中…');
    else if (item.dataset.kind === 'lit') await guardBtn(item, () => createLitSession(), '创建中…');
  });
  document.addEventListener('click', () => { menu.style.display = 'none'; });
}

async function createKbSession(mode) {
  try {
    const created = await api('/api/sessions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'global', mode }),
    });
    await loadSessions();
    await openSession(created.id);
  } catch (e) { alert('创建知识库会话失败：' + e.message); }
}

async function createChatSession() {
  try {
    const created = await api('/api/sessions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'chat' }),
    });
    await loadSessions();
    await openSession(created.id);
  } catch (e) { alert('创建聊天会话失败：' + e.message); }
}

async function createLitSession() {
  try {
    const created = await api('/api/sessions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'lit' }),
    });
    await loadSessions();
    await openSession(created.id);
  } catch (e) { alert('创建AI检索会话失败：' + e.message); }
}

async function createGlobalSession() {
  try {
    const created = await api('/api/sessions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'global' }),
    });
    await loadSessions();
    await openSession(created.id);
  } catch (e) { alert('创建知识库会话失败：' + e.message); }
}

async function createPaperSession(paperId) {
  try {
    const created = await api(`/api/papers/${paperId}/sessions`, { method: 'POST' });
    await loadSessions();
    await openSession(created.id);
  } catch (e) { alert('创建文献会话失败：' + e.message); }
}

/* P5-FB-点2：打开该篇提问分支——有则开最近，无则新建（多入口快捷问答共用） */
async function openOrCreatePaperSession(paperId) {
  if (!paperId) return;
  const mine = state.sessions.filter(s => s.kind === 'paper' && s.paper_id === paperId);
  if (mine.length) { await openSession(mine[mine.length - 1].id); return; }
  try {
    const rs = await api(`/api/papers/${paperId}/sessions`);
    if (rs.sessions && rs.sessions.length) { await openSession(rs.sessions[rs.sessions.length - 1].id); return; }
  } catch (e) { /* 列表未就绪则直接新建 */ }
  await createPaperSession(paperId);
}

