/* ══════════════════════════════════════════════════════════
   PaperAgent v1.0 前端逻辑（原生窗口桌面壳 + 文献 AI 阅读/翻译/知识库管理）
   ══════════════════════════════════════════════════════════ */
'use strict';

function $(id) { return document.getElementById(id); }

/* ---------------- 全局状态 ---------------- */
const state = {
  papers: [],
  sessions: [],
  currentPaperId: null,
  sessionId: null,
  sessionKind: null,   // 'paper' | 'global' | 'chat'
  busy: false,
  // E3 文献库分页：当前页数据在 papers；paperById 合并已见各页（跨页查询用）
  papersPage: 1,
  papersPageSize: 50,
  papersTotal: 0,
  papersQ: '',
  papersDate: 'all',   // O2：按导入日期过滤（'all'=全部，否则某日期 YYYY-MM-DD）
  papersKind: 'all',   // F1：类型芯片过滤（'all'/'none'(无编号)/'has_attachment'/具体 kind）
  papersLoaded: 0,     // 请求序号：轮询/翻页竞态守卫（过期响应丢弃）
  paperById: {},
  paperRidById: {},    // F3：论文 id → 资源目录名（附件 API 的资源键）
  paperSessionOpening: null,   // P1：文献卡会话"单飞锁"（防连点建重复空分支）
  sessDate: 'all',     // O批2：会话按创建日期过滤（'all'=全部）
};

/* ---------------- API helper ---------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const ct = res.headers.get('content-type') || '';
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = j.detail || j.message || JSON.stringify(j);
    } catch (e) { /* 非 JSON 错误体 */ }
    throw new Error(msg);
  }
  return ct.includes('json') ? res.json() : res.text();
}

/* ---------------- 工具 ---------------- */
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function shortTitle(t, n = 46) {
  t = t || '(无标题)';
  return t.length > n ? t.slice(0, n) + '…' : t;
}

/* E3：跨页论文查询（paperById 合并已见各页；当前页加载时已并入） */
function getPaper(id) {
  return (id != null && state.paperById[id]) || null;
}

/* ══════════ P1（2026-09-12）：统一交互层 ══════════════════════════════════
   审计前端 P1：56 处 alert + 14 处 confirm 阻塞式弹窗、无离线提示、
   写操作可重复点。这里提供四个基础设施，业务代码一律走它们：
   - toast(msg, kind, opts)：页内 snackbar（可带操作按钮/撤销），不阻塞；
   - askConfirm(msg, opts)：页内确认（Promise<boolean>，Esc/点遮罩=取消）；
   - guardBtn(btn, fn)：写操作防重（禁用 + busy 样式 + 失败恢复）；
   - 离线提示条（navigator.onLine + 后端探活失败）。
   ⚠️ 兼容：window.alert 被重定向到 toast（非阻塞）；window.confirm 保持原生
   （同步返回，业务里已改造为 await askConfirm 的调用点不再用它）。 */

function toast(msg, kind = 'info', opts = {}) {
  const wrap = $('toast-wrap');
  const text = String(msg ?? '');
  if (!wrap) return null;
  const div = document.createElement('div');
  div.className = `toast toast-${kind}`;
  div.innerHTML = `<span class="toast-msg">${escapeHtml(text)}</span>`;
  if (opts.action && opts.onAction) {
    const b = document.createElement('button');
    b.className = `btn small toast-act`;
    b.textContent = opts.action;
    b.addEventListener('click', () => { try { opts.onAction(); } finally { hide(); } });
    div.appendChild(b);
  }
  wrap.appendChild(div);
  requestAnimationFrame(() => div.classList.add('show'));
  const ttl = Number(opts.ttl ?? (kind === 'error' ? 9000 : 5000));
  let timer = setTimeout(hide, ttl);
  function hide() {
    clearTimeout(timer);
    div.classList.remove('show');
    setTimeout(() => div.remove(), 240);
  }
  div.addEventListener('click', (e) => { if (e.target === div) hide(); });
  return { hide };
}

/* 页内确认：Promise<boolean>；Esc / 点遮罩 / 取消 按钮 → false */
function askConfirm(msg, opts = {}) {
  const mask = $('confirm-modal');
  if (!mask) return Promise.resolve(window.confirm(msg));   // 兜底：HTML 未加载
  $('confirm-title').textContent = opts.title || '确认';
  $('confirm-msg').textContent = String(msg ?? '');
  $('confirm-yes').textContent = opts.okText || '确定';
  $('confirm-no').textContent = opts.cancelText || '取消';
  mask.style.display = 'flex';
  return new Promise((resolve) => {
    const done = (val) => {
      mask.style.display = 'none';
      $('confirm-yes').removeEventListener('click', onYes);
      $('confirm-no').removeEventListener('click', onNo);
      mask.removeEventListener('click', onMask);
      document.removeEventListener('keydown', onKey);
      resolve(val);
    };
    const onYes = () => done(true);
    const onNo = () => done(false);
    const onMask = (e) => { if (e.target === mask) done(false); };
    const onKey = (e) => { if (e.key === 'Escape') done(false); };
    $('confirm-yes').addEventListener('click', onYes);
    $('confirm-no').addEventListener('click', onNo);
    mask.addEventListener('click', onMask);
    document.addEventListener('keydown', onKey);
    setTimeout(() => $('confirm-yes').focus(), 30);
  });
}

/* 写操作防重：同一按钮在请求期间禁用（防"点两次导入两遍"） */
async function guardBtn(btn, fn, busyText = '处理中…') {
  if (!btn) return fn();
  if (btn.dataset.busy === '1') return null;
  const old = btn.textContent;
  const wasDisabled = btn.disabled;     // 尊重业务自己设的禁用态（勿在 finally 里"复活"）
  btn.dataset.busy = '1';
  btn.disabled = true;
  btn.classList.add('busy');
  btn.textContent = busyText;
  try {
    return await fn();
  } finally {
    delete btn.dataset.busy;
    btn.classList.remove('busy');
    btn.textContent = old;
    btn.disabled = wasDisabled;
  }
}

/* 离线提示条：浏览器离线 或 后端探活失败时显示（并说明"数据未丢"） */
const offlineState = { off: false, backendDown: false, browserOffline: false };
function setOfflineBar() {
  const bar = $('offline-bar');
  if (!bar) return;
  // 以 offline/online 事件为准（navigator.onLine 在部分 WebView/注入环境不可靠）
  const off = offlineState.browserOffline || navigator.onLine === false;
  const down = offlineState.backendDown;
  offlineState.off = off;
  if (!off && !down) { bar.style.display = 'none'; bar.textContent = ''; return; }
  bar.style.display = '';
  bar.textContent = off
    ? '⚠ 网络已断开（本机解析/翻译暂时不可用；已导入的文献与知识库不受影响）'
    : '⚠ 无法连接后端服务（127.0.0.1:8900）：请确认后端仍在运行，或稍后自动重试';
}
function bindConnectivity() {
  window.addEventListener('online', () => { offlineState.browserOffline = false; setOfflineBar(); });
  window.addEventListener('offline', () => { offlineState.browserOffline = true; setOfflineBar(); });
  offlineState.browserOffline = navigator.onLine === false;
  setOfflineBar();
  // 后端探活：5s 轮询里顺带看 /api/health（失败即标记，成功清除）
  setInterval(async () => {
    try {
      await fetch('/api/health', { cache: 'no-store' });
      if (offlineState.backendDown) { offlineState.backendDown = false; setOfflineBar(); }
    } catch (e) {
      if (!offlineState.backendDown) {
        offlineState.backendDown = true;
        setOfflineBar();
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'error', source: 'system',
                      message: '后端探活失败：无法连接 /api/health（网络或服务已停）' });
      }
    }
  }, 10000);
}
/* 兼容层：把遗留 alert 重定向为页内提示（不阻塞、不丢信息） */
window.alert = function (msg) { toast(msg, 'info'); };

/* ══════════ 高度拖拽（V11：输入框 / 信息面板）══════════ */
function makeHeightDragger(handleId, targetId, min, max, persistKey) {
  const handle = $(handleId);
  const target = $(targetId);
  const saved = persistKey ? localStorage.getItem(persistKey) : null;
  if (saved) target.style.height = saved + 'px';
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = target.getBoundingClientRect().height || target.offsetHeight;
    const move = (ev) => {
      // 向上拖动（clientY 减小）→ 高度增大（V11 修正）
      let h = startH - (ev.clientY - startY);
      h = Math.max(min, Math.min(h, max));
      target.style.height = h + 'px';
      if (persistKey) localStorage.setItem(persistKey, String(h));
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
}

function bindHeightDrag() {
  makeHeightDragger('input-resizer', 'question-input', 72, 360, 'input-h');
  makeHeightDragger('event-resizer', 'event-body', 80, window.innerHeight * 0.5, 'event-h');
}

/* ══════════ 左栏 tab 切换（会话/文献/知识库，U7/V11）══════════ */
const sideTab = { current: 'sessions' };

function setSideTab(tab) {
  sideTab.current = tab;
  document.querySelectorAll('.side-tab').forEach(b => b.classList.toggle('active', b.dataset.panel === tab));
  ['sessions', 'papers', 'kb', 'diary'].forEach(p => {
    $('panel-' + p).style.display = p === tab ? '' : 'none';
  });
  if (tab === 'kb') (kbView.mode === 'tree' ? loadKbTree() : loadKbList());
  if (tab === 'papers') loadPapers();
  if (tab === 'diary') loadDiary();
}

/* ══════════ U1：拖拽（左栏宽度，T7 最小 440px 以容纳知识库表格）══════════ */
function setPanelWidth(side, w) {
  const clamped = Math.max(440, Math.min(w, window.innerWidth * 0.42));
  document.documentElement.style.setProperty(side === 'left' ? '--side-w' : '--reader-w', clamped + 'px');
  localStorage.setItem('panel-w-' + side, String(clamped));
}

/* P2-8/9：左栏自动折叠 + 钉住（延时响应，防误触发） */
function bindCollapse() {
  const ws = $('workspace');
  const sb = $('sidebar');
  let pinned = localStorage.getItem('side-pinned') === '1';
  let foldTimer = null;
  const setCollapsed = (c) => {
    ws.classList.toggle('side-collapsed', c);
    localStorage.setItem('side-collapsed', c ? '1' : '0');
  };
  // 初始：未钉住 → 折叠（省空间）；钉住 → 展开
  setCollapsed(!pinned);
  // 鼠标移到 workspace 左缘 16px → 延时 120ms 展开（未钉住时）
  ws.addEventListener('mousemove', (e) => {
    if (pinned || readMode || !ws.classList.contains('side-collapsed')) return;
    const r = ws.getBoundingClientRect();
    if (e.clientX - r.left < 16) {
      if (!foldTimer) {
        foldTimer = setTimeout(() => {
          foldTimer = null;
          if (!pinned && !readMode) ws.classList.remove('side-collapsed');
        }, 120);
      }
    }
  });
  // 鼠标进入侧边栏：取消折叠
  if (sb) {
    sb.addEventListener('mouseenter', () => {
      if (foldTimer) { clearTimeout(foldTimer); foldTimer = null; }
      if (!pinned && !readMode) ws.classList.remove('side-collapsed');
    });
    // 鼠标移出侧边栏 → 延时 450ms 折叠（未钉住；移回则取消）
    sb.addEventListener('mouseleave', () => {
      if (pinned || readMode) return;
      if (foldTimer) clearTimeout(foldTimer);
      foldTimer = setTimeout(() => {
        foldTimer = null;
        if (!pinned && !readMode) ws.classList.add('side-collapsed');
      }, 450);
    });
  }
  // 📌 钉住按钮
  const pin = $('side-pin');
  if (pin) {
    const render = () => {
      pin.classList.toggle('active', pinned);
      pin.title = pinned ? '已钉住（左栏常驻，不自动折叠）' : '点击钉住（左栏常驻）';
    };
    pin.addEventListener('click', (e) => {
      e.stopPropagation();
      pinned = !pinned;
      localStorage.setItem('side-pinned', pinned ? '1' : '0');
      if (!pinned) setCollapsed(true);
      render();
    });
    render();
  }
}

function bindDrag() {
  function make(handleId, side) {
    const handle = $(handleId);
    if (!handle) return; // G15：drag-right 已移除
    const saved = localStorage.getItem('panel-w-' + side);
    if (saved) setPanelWidth(side, Number(saved));
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const target = $('sidebar');
      const startW = target.getBoundingClientRect().width;
      const move = (ev) => {
        setPanelWidth(side, startW + (ev.clientX - startX));
      };
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      };
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
  }
  make('drag-left', 'left');
}

/* ══════════ P2-6/7/8：阅读模式（状态化） / 主题皮肤 ══════════ */
// P12 反馈回滚：默认不进入阅读模式（第一次运行只开一个阅读区），用户手动切换
// P12F：启动**恒**单阅读区——不再读取 localStorage 残留（旧会话点过阅读模式会
// 在启动时自动进双阅读器）；boot 时清除旧键（见 boot()）
let readMode = false;
let readWin2 = null;       // 阅读模式第二个阅读器（复用，杜绝每次进入重复新建）
let readHidden = [];       // 进入阅读模式时隐藏的多余阅读面板（退出时恢复显示）
let theme = localStorage.getItem('theme') || 'default';
let themePref = localStorage.getItem('theme-pref') || 'sepia';  // P2-9：护眼偏好（重开护眼用）
const THEMES = ['default', 'sepia', 'night', 'paper'];
const THEME_LABELS = { default: '默认', sepia: '护眼 · 米黄', night: '夜间 · 深色', paper: '纸质 · 豆绿' };

function isReaderWin(w) {
  return w && w.el && (w.el.id === 'reader' || (w.el.id || '').startsWith('fw'));
}

function enterReadMode() {
  const ws = $('workspace');
  ws.classList.add('read-mode');
  // 1) 所有阅读面板（非对话）强制停靠；清除一切 inline 宽度/定位残留
  //    （用户拖过的宽度、浮窗位置都会破坏"铺满桌面"，必须清掉交给 CSS）
  floatWins.forEach(w => {
    if (!isReaderWin(w)) return;
    if (!w.docked) dockFloatToDesk(w);
    w.el.style.flex = ''; w.el.style.width = ''; w.el.style.left = '';
    w.el.style.top = ''; w.el.style.height = ''; w.el.style.display = '';
  });
  // 2) 恰好保留 2 个停靠阅读面板（#reader + 第二阅读器）；多余的隐藏（退出恢复）
  const readers = floatWins.filter(w => isReaderWin(w) && w.docked);
  readHidden = readers.slice(2);
  readHidden.forEach(w => { w.el.style.display = 'none'; });
  // 3) 不足 2 个 → 复用已有第二阅读器，否则创建一次（readWin2 缓存）
  if (readers.length < 2) {
    let win2 = (readWin2 && document.body.contains(readWin2.el)) ? readWin2 : null;
    if (!win2) {
      openReaderWindow(true);   // force：阅读模式下忽略 FLOAT_MAX
      win2 = floatWins[floatWins.length - 1];
      readWin2 = win2;
    }
    win2.paperId = state.currentPaperId || null;
    if (win2.paperId) {
      const sel = win2.el.querySelector('.fw-paper');
      if (sel) sel.value = String(win2.paperId);
    }
    if (!win2.docked) dockFloatToDesk(win2);
    win2.el.style.flex = ''; win2.el.style.display = '';
    loadFloatContent(win2);
  }
  // 4) P2-13 权重模型：阅读模式对话 20%（0.25 单位）、可见阅读器均分铺满
  const cp = $('chat-panel');
  if (cp) { cp.dataset.grow = '0.25'; cp.style.flex = ''; }
  resetReaderWeights();
  applyDeskWeights();
}

function exitReadMode() {
  const ws = $('workspace');
  ws.classList.remove('read-mode');
  // O批-3：退出阅读模式只保留主阅读窗口(#reader)，关闭其余阅读窗口（不再"恢复隐藏窗"）
  floatWins.slice().forEach(w => {
    if (!isReaderWin(w)) return;
    if (w.el.id === 'reader') return;   // 主阅读窗口保留
    closeFloatWin(w);                   // 其余阅读窗口关闭
  });
  readHidden = [];
  readWin2 = null;
  // P2-13 权重模型：对话恢复 1 单位、阅读器均分铺满
  const cp = $('chat-panel');
  if (cp) cp.dataset.grow = '1';
  resetReaderWeights();
  applyDeskWeights();
}

function applyReadMode() {
  const ws = $('workspace');
  const btn = $('read-mode-btn');
  if (readMode) { enterReadMode(); ws.classList.add('read-mode'); }
  else { exitReadMode(); ws.classList.remove('read-mode'); }
  if (!btn) return;
  btn.classList.toggle('active', readMode);
  btn.textContent = readMode ? '📖 退出阅读' : '📖 进入阅读';
  btn.title = readMode ? '退出阅读模式' : '阅读模式：隐藏侧边栏，左对话 + 双阅读器铺满平分';
}

function applyTheme() {
  if (theme === 'default') delete document.body.dataset.theme;
  else document.body.dataset.theme = theme;
  const btn = $('eye-care-btn');
  if (btn) {
    btn.classList.toggle('active', theme === 'sepia');
    btn.title = theme === 'sepia'
      ? '当前：护眼 · 米黄（点此恢复默认）'
      : '护眼模式（切换到米黄护眼底色；更多主题在 设置 → 外观）';
  }
  const sel = $('theme-select');
  if (sel && sel.value !== theme) sel.value = theme;
}

function bindReadModes() {
  $('read-mode-btn').addEventListener('click', () => {
    readMode = !readMode;
    localStorage.setItem('read-mode', readMode ? '1' : '0');
    applyReadMode();
  });
  $('eye-care-btn').addEventListener('click', () => {
    // P2-9：护眼开关——在 default 与「护眼偏好」间切换；偏好 = 设置中选的主题
    // （曾固定切到 sepia 米黄；现关闭时记住当前主题，重开恢复上次主题）
    if (theme === 'default') {
      theme = themePref === 'default' ? 'sepia' : themePref;
    } else {
      themePref = theme;
      theme = 'default';
    }
    localStorage.setItem('theme-pref', themePref);
    localStorage.setItem('theme', theme);
    if (sel) sel.value = theme;
    applyTheme();
  });
  const sel = $('theme-select');
  if (sel) {
    sel.innerHTML = THEMES.map(t => `<option value="${t}">${THEME_LABELS[t]}</option>`).join('');
    sel.addEventListener('change', () => {
      theme = sel.value;
      themePref = theme === 'default' ? themePref : theme;  // 选非默认即更新偏好
      localStorage.setItem('theme-pref', themePref);
      localStorage.setItem('theme', theme);
      applyTheme();
    });
  }
  applyTheme();  // 阅读模式在 boot 末尾（floatWins 就绪后）应用
}

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

/* O批2：会话按创建日期分组 + 日期过滤 */
function sessionDate(s) {
  const c = (s.created_at || '');
  return c ? c.slice(0, 10) : '未标注日期';
}
function renderSessDates(dates, active) {
  const el = $('sess-dates');
  if (!el) return;
  const uniq = [...new Set(dates)].sort((a, b) => b.localeCompare(a));
  if (!uniq.length) { el.innerHTML = ''; return; }
  const chip = (d, label) =>
    `<button class="pd-chip ${d === active ? 'active' : ''}" data-date="${d}" title="${d === 'all' ? '显示全部日期' : '仅显示 ' + d}会话">${label}</button>`;
  el.innerHTML = chip('all', '全部') + uniq.map(d => chip(d, d.slice(5))).join('');
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
  renderSessDates(allVisible.map(s => sessionDate(s)), state.sessDate);
  const filtered = state.sessDate === 'all' ? allVisible
    : allVisible.filter(s => sessionDate(s) === state.sessDate);
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
  box.querySelectorAll('.s-exp').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const det = $('sdet-' + btn.dataset.exp);
      if (!det) return;
      const open = det.style.display !== 'none';
      det.style.display = open ? 'none' : '';
      btn.textContent = open ? '▾' : '▸';
    });
  });
  box.querySelectorAll('.s-arch').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleArchiveSession(Number(btn.dataset.arch));
    });
  });
  box.querySelectorAll('.s-ren').forEach(btn => {
    // P1：重命名（POST）/删除（DELETE）是写操作 → 防连点（图标钮用短忙态文案）
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      guardBtn(btn, () => renameSession(Number(btn.dataset.ren)), '…');
    });
  });
  box.querySelectorAll('.s-del').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      guardBtn(btn, () => deleteSession(Number(btn.dataset.del)), '…');
    });
  });
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
  const name = prompt('重命名对话：', sessionTitle(s));
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

/* ══════════ P12F 复核门控：汇总条 + 确认翻译 ══════════ */
async function refreshReviewGate() {
  const bar = $('rv-gate-bar');
  if (!bar) return;
  try {
    const r = await api('/api/tasks/review-queue');
    if (!r.waiting.length) { bar.style.display = 'none'; return; }
    bar.style.display = '';
    const parts = [];
    if (r.pending_total > 0) parts.push(`⏸ 待复核 ${r.waiting.length} 篇（${r.pending_total} 差异点）`);
    if (r.ready_count > 0) parts.push(`✅ 就绪 ${r.ready_count} 篇`);
    $('rv-gate-info').textContent = parts.join(' · ');
    const start = $('rv-gate-start');
    start.disabled = r.ready_count === 0;
    start.title = r.ready_count
      ? `对 ${r.ready_count} 篇已复核完的文献启动翻译（依次串行进行）`
      : '暂无已复核完的文献：先在复核页处理各篇差异点，处理完自动就绪';
  } catch (e) { /* 队列刷新失败不打扰 */ }
}

async function initReviewGate() {
  const onTranslate = async (force) => {
    try {
      const r = await api(`/api/tasks/translate-ready${force ? '?force=true' : ''}`,
                          { method: 'POST' });
      const how = force ? '跳过审核' : '就绪';
      appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                    message: `已启动 ${r.started} 篇${how}文献的翻译（依次串行进行，篇间有间隔防限流）` });
      refreshReviewGate();
      loadPapers();
    } catch (err) { alert('启动翻译失败：' + err.message); }
  };
  $('rv-gate-start').addEventListener('click', async (e) => {
    // P1：批量启动翻译是排队写操作 → 防连点（busy 期间按钮禁用并显示进度）
    await guardBtn(e.currentTarget, () => onTranslate(false), '启动中…');
  });
  $('rv-gate-force').addEventListener('click', async (e) => {
    if (!(await askConfirm('跳过所有待复核项的审核，用当前主文本直接翻译？\n（AI 误落地可在复核页改选回滚；可在设置-解析默认开启跳过）'))) return;
    await guardBtn(e.currentTarget, () => onTranslate(true), '启动中…');
  });
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

/* ══════════ O3 阅读日记（日历 + 日志 + 每日心得笔记） ══════════ */
const diaryState = { y: new Date().getFullYear(), m: new Date().getMonth() + 1,
                     view: 'calendar', selDate: '', days: {} };

function _dMonthStr() { return `${diaryState.y}-${String(diaryState.m).padStart(2, '0')}`; }

function bindDiaryTabs() {
  document.querySelectorAll('#panel-diary .dtab').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#panel-diary .dtab').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      diaryState.view = b.dataset.dview;
      loadDiary();
    });
  });
  const exp = $('diary-export');
  if (exp && !exp.dataset.bound) {
    exp.dataset.bound = '1';
    exp.addEventListener('click', async () => {
      try {
        const r = await api('/api/diary/export');
        const blob = new Blob([r.text || r.markdown || ''], { type: 'text/markdown' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `阅读日记-${_dMonthStr()}.md`;
        a.click();
        URL.revokeObjectURL(a.href);
      } catch (e) { alert('导出失败：' + e.message); }
    });
  }
}

async function loadDiary() {
  const body = $('diary-body');
  if (!body) return;
  bindDiaryTabs();
  const ms = diaryState.view === 'calendar' ? `?month=${_dMonthStr()}` : '';
  try {
    const r = await api('/api/diary/days' + ms);
    diaryState.days = {};
    (r.days || []).forEach(d => { diaryState.days[d.date] = d; });
  } catch (e) { console.warn('diary days failed', e); diaryState.days = {}; }
  if (diaryState.view === 'calendar') renderDiaryCalendar();
  else renderDiaryLog();
}

function renderDiaryCalendar() {
  const body = $('diary-body');
  const first = new Date(diaryState.y, diaryState.m - 1, 1);
  const firstWd = first.getDay();               // 0=周日
  const daysInMonth = new Date(diaryState.y, diaryState.m, 0).getDate();
  const wds = ['日', '一', '二', '三', '四', '五', '六'];
  let cells = '';
  for (let i = 0; i < firstWd; i++) cells += '<div class="diay-cell empty"></div>';
  for (let d = 1; d <= daysInMonth; d++) {
    const date = `${_dMonthStr()}-${String(d).padStart(2, '0')}`;
    const info = diaryState.days[date];
    const total = info ? (info.events || 0) : 0;
    const hasNote = info && (info.notes || 0) > 0;
    const mark = (total || hasNote) ? '<div class="ddots">●</div>' : '';
    const sel = date === diaryState.selDate ? ' sel' : (total || hasNote ? ' has' : '');
    cells += `<div class="diay-cell${sel}" data-date="${date}" title="${date}${total ? ` · ${total} 条活动` : ''}${hasNote ? ' · 有笔记' : ''}">
      <div class="dnum">${d}</div>${mark}</div>`;
  }
  body.innerHTML = `
    <div class="diay-cal-head">
      <button class="btn small dc-nav" id="dc-prev">‹</button>
      <span class="dc-title">${diaryState.y} 年 ${diaryState.m} 月</span>
      <button class="btn small dc-nav" id="dc-next">›</button>
    </div>
    <div class="diay-grid">${wds.map(w => `<div class="diay-wd">${w}</div>`).join('')}${cells}</div>
    <div class="diay-day" id="diary-day"></div>`;
  $('dc-prev').addEventListener('click', () => { diaryState.m--; if (diaryState.m < 1) { diaryState.m = 12; diaryState.y--; } diaryState.selDate = ''; loadDiary(); });
  $('dc-next').addEventListener('click', () => { diaryState.m++; if (diaryState.m > 12) { diaryState.m = 1; diaryState.y++; } diaryState.selDate = ''; loadDiary(); });
  document.querySelectorAll('#diary-body .diay-cell[data-date]').forEach(el => {
    el.addEventListener('click', () => { diaryState.selDate = el.dataset.date; renderDiaryCalendar(); loadDiaryDay(el.dataset.date); });
  });
  if (diaryState.selDate) loadDiaryDay(diaryState.selDate);
}

async function loadDiaryDay(date) {
  const box = $('diary-day');
  if (!box || !date) { return; }
  try {
    const r = await api('/api/diary/day?date=' + encodeURIComponent(date));
    renderDiaryDayHtml(box, date, r);
  } catch (e) { box.innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`; }
}

function renderDiaryDayHtml(box, date, r) {
  const papers = r.imports || [];
  const paperHtml = papers.length ? papers.map(p => `
    <div class="diay-paper">
      <div class="dp-title">${escapeHtml(p.title || p.doi || '')}</div>
      <div class="dp-badges">
        ${p.parsed ? '<span class="p-status done">已解析</span>' : ''}
        ${p.translated ? '<span class="p-status done">已翻译</span>' : ''}
        ${p.in_kb ? '<span class="kb-badge kb-ok">已纳入</span>' : ''}
      </div>
    </div>`).join('')
    : '<div class="muted" style="font-size:11px">该天无导入文献记录</div>';
  box.innerHTML = `
    <div class="diay-day-head">📅 ${escapeHtml(date)}</div>
    ${paperHtml}
    <div class="diay-note">
      <h4>✍ 阅读心得（该天读了哪些文献、注释）</h4>
      <textarea id="diary-note-input" placeholder="记录当天阅读心得…">${escapeHtml(r.note || '')}</textarea>
      <button class="btn small primary dn-save" id="diary-note-save">保存笔记</button>
    </div>`;
    // P1：保存笔记是写操作（POST /api/diary/note）→ 防连点（成功后仍给"已保存"反馈）
    $('diary-note-save').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const ok = await guardBtn(btn, async () => {
        const text = $('diary-note-input').value;
        try {
          await api('/api/diary/note', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ date, text }) });
          return true;
        } catch (err) { alert('保存失败：' + err.message); return false; }
      }, '保存中…');
      if (ok) {
        btn.textContent = '已保存 ✓';
        setTimeout(() => { if ($('diary-note-save')) $('diary-note-save').textContent = '保存笔记'; }, 1500);
      }
    });
}

async function renderDiaryLog() {
  const body = $('diary-body');
  try {
    const r = await api('/api/diary/days');
    const days = (r.days || []).slice().sort((a, b) => b.date.localeCompare(a.date));
    if (!days.length) { body.innerHTML = '<div class="empty">暂无阅读日志</div>'; return; }
    body.innerHTML = '<div class="diay-log"></div>';
    const log = body.querySelector('.diay-log');
    for (const d of days) {
      const box = document.createElement('div');
      box.className = 'diay-log-item';
      const counts = (d.events || 0);
      box.innerHTML = `<div class="dl-date">📅 ${d.date}</div>
        <div class="dl-sub">${counts ? `导入 ${d.imports || 0} · 纳入 ${d.kb || 0} · 笔记 ${d.notes || 0}` : '无活动'}${(d.notes || 0) > 0 ? ' · ✍ 有笔记' : ''}</div>`;
      log.appendChild(box);
      let detail = null;
      if (d.events || (d.notes || 0) > 0) {
        try {
          const dd = await api('/api/diary/day?date=' + encodeURIComponent(d.date));
          detail = dd;
        } catch (e) { /* 忽略单日 */ }
      }
      if (detail) {
        const papers = detail.imports || [];
        const paperHtml = papers.length ? papers.map(p => `
          <div class="diay-paper">
            <div class="dp-title">${escapeHtml(p.title || p.doi || '')}</div>
            <div class="dp-badges">
              ${p.parsed ? '<span class="p-status done">已解析</span>' : ''}
              ${p.translated ? '<span class="p-status done">已翻译</span>' : ''}
              ${p.in_kb ? '<span class="kb-badge kb-ok">已纳入</span>' : ''}
            </div>
          </div>`).join('') : '';
        const noteHtml = detail.note ? `<div class="diay-note" style="margin-top:4px"><h4>✍ 心得</h4><div style="white-space:pre-wrap;font-size:11px">${escapeHtml(detail.note)}</div></div>` : '';
        const sub = document.createElement('div');
        sub.style.marginTop = '4px';
        sub.innerHTML = paperHtml + noteHtml;
        box.appendChild(sub);
      }
    }
  } catch (e) {
    body.innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

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

/* ══════════ P12-6 识别复核（双通道差异批注 + PDF 高亮 + 选择题）══════════ */
const reviewState = { paperId: null, items: [], page: 1, curPage: 0, activeIdx: -1, zoom: 1, showHandled: false, pageCount: 1 };

// P12F：已处理 = 用户已选 / AI 已自动化解 / AI 高置信已落地 —— 无需人工复核
function isHandled(it) { return !!(it.user_choice || it.auto_resolved || it.applied); }

function bindReview() {
  $('review-close').addEventListener('click', () => { $('review-modal').style.display = 'none'; });
  $('rv-prev').addEventListener('click', () => reviewNav(-1));
  $('rv-next').addEventListener('click', () => reviewNav(1));
  $('rv-zoom-in').addEventListener('click', () => reviewZoom(0.25));
  $('rv-zoom-out').addEventListener('click', () => reviewZoom(-0.25));
  $('rv-zoom-fit').addEventListener('click', () => reviewZoom(0, true));
  // P12 反馈修复：滚轮缩放 + 中键拖拽平移（像 PDF 阅读器）
  const pdfBox = $('review-pdf');
  let drag = null;
  pdfBox.addEventListener('wheel', (e) => {
    e.preventDefault();
    // P13：滚轮缩放以鼠标位置为锚点（不随左上角缩放）
    const rect = pdfBox.getBoundingClientRect();
    reviewZoom(e.deltaY < 0 ? 0.1 : -0.1, false,
               e.clientX - rect.left, e.clientY - rect.top);
  }, { passive: false });
  pdfBox.addEventListener('mousedown', (e) => {
    if (e.button !== 1) return;   // 仅中键
    e.preventDefault();
    drag = { x: e.clientX, y: e.clientY, sl: pdfBox.scrollLeft, st: pdfBox.scrollTop };
    pdfBox.classList.add('dragging');
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    pdfBox.scrollLeft = drag.sl - (e.clientX - drag.x);
    pdfBox.scrollTop = drag.st - (e.clientY - drag.y);
  });
  window.addEventListener('mouseup', (e) => {
    if (e.button === 1 && drag) { drag = null; pdfBox.classList.remove('dragging'); }
  });
  pdfBox.addEventListener('mouseleave', () => {
    if (drag) { drag = null; pdfBox.classList.remove('dragging'); }
  });
}

function reviewZoom(delta, fit = false, ax = null, ay = null) {
  const box = $('review-pdf');
  const img = reviewPageImg();     // F2：统一入口（img 被历史 DOM 操作移除也能自愈）
  const oldZoom = reviewState.zoom;
  const newZoom = fit ? 1 : Math.min(3, Math.max(0.5, oldZoom + delta));
  if (newZoom === oldZoom && !fit) return;
  reviewState.zoom = newZoom;
  $('rv-zoom-info').textContent = Math.round(newZoom * 100) + '%';
  if (!img || !img.src) return;
  // P13：锚点缩放——保持鼠标/容器中心下的图片点不动（默认左上角锚点退化为原行为）
  const ratio = newZoom / (oldZoom || 1);
  if (ax == null) {
    ax = (box.clientWidth || 0) / 2;
    ay = (box.clientHeight || 0) / 2;
  }
  const sx = Math.max(0, (box.scrollLeft + ax) * ratio - ax);
  const sy = Math.max(0, (box.scrollTop + ay) * ratio - ay);
  img.style.width = (100 * newZoom) + '%';
  box.scrollLeft = sx;
  box.scrollTop = sy;
}

function reviewNav(delta) {
  // P12F：左侧 PDF 支持**所有页**预览（翻页范围 1..pageCount，不再限于待处理页）；
  // 当前页若有待处理项仍显示高亮，翻页不丢定位
  const n = reviewState.pageCount || 1;
  const next = Math.min(n, Math.max(1, (reviewState.curPage || 1) + delta));
  if (next !== reviewState.curPage) loadReviewPage(next);
}

async function openReviewModal(paperId) {
  const p = getPaper(paperId);
  reviewState.paperId = paperId;
  reviewState.items = [];
  reviewState.curPage = 0;
  reviewState.activeIdx = -1;
  $('review-modal').style.display = 'flex';
  $('rv-paper-name').textContent = p ? `（${escapeHtml(shortTitle(p.filename || p.title, 40))}）` : '';
  $('review-items').innerHTML = '<div class="empty">加载复核清单…</div>';
  try {
    const r = await api(`/api/papers/${paperId}/review`);
    if (!r.available) { $('review-items').innerHTML = `<div class="empty">${escapeHtml(r.reason || '无复核清单')}</div>`; return; }
    reviewState.items = r.items;
    reviewState.showHandled = false;
    reviewState.pageCount = r.page_count || Math.max(1, ...r.items.map(it => it.page), 1);
    renderReviewItems();
    const first = r.items.find(it => !isHandled(it)) || r.items[0];
    if (first) loadReviewPage(first.page);
    else {
      // F2 修复（2026-09-12 用户实测报障）：**绝不 innerHTML 覆盖 #review-pdf**——
      // 旧写法把它连 `<img id="review-page-img">` 一起销毁，之后任何 loadReviewPage()
      // 都抛 "Cannot set properties of null (setting 'src')"（谁先打开了零差异项的
      // 那篇，之后所有复核页都报错）。
      // F3：零差异项时**仍**给出原文 PDF 第 1 页（知识库/解析库有 source.pdf 就该能看）。
      showReviewPdfEmpty(r.page_count
        ? '🎉 无差异仲裁点（可直接翻阅原文 PDF）'
        : '无差异仲裁点');
      loadReviewPage(1);
    }
  } catch (e) { $('review-items').innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`; }
}

function bindReviewToggle() {
  const btn = $('rv-toggle-handled');
  if (!btn) return;
  btn.addEventListener('click', () => {
    reviewState.showHandled = !reviewState.showHandled;
    renderReviewItems();
  });
}

function renderReviewItems() {
  const box = $('review-items');
  const all = reviewState.items;
  if (!all.length) { box.innerHTML = '<div class="empty">🎉 无差异仲裁点</div>'; return; }
  // P12F：已处理（AI 高置信已落地/自动化解/用户已选）= 无需复核，默认不展示
  const pending = all.filter(it => !isHandled(it));
  const handled = all.filter(isHandled);
  const showHandled = reviewState.showHandled;
  const tip = `<div class="rv-tip">🔍 <b>对照左侧 PDF 高亮（红框）处的原文</b>判断哪版正确（公式已渲染）：
    <b>A</b>=保留 MinerU（与原文一致）· <b>B</b>=用 PaddleOCR（与原文一致）·
    <b>C</b>=两者皆可（仅格式/表示差异，如公式写法）· <b>D</b>=都错。
    <b>每项只影响对应段落（块），不是整篇二选一</b>；选择立即写入最终解析结果，
    已处理项展开后可随时改选（B 落地替换 / A·C·D 还原为 MinerU）。
    不处理也没关系：AI 已保守处理高置信项，低置信项保留 MinerU 主文本（安全）。</div>`;
  const filterBar = (() => {
    if (!handled.length) return '';
    const btn = `<button class="btn small" id="rv-toggle-handled">${showHandled ? '隐藏已处理' : `显示已处理（${handled.length}）`}</button>`;
    if (!pending.length) {
      return `<div class="rv-filter rv-done">🎉 无需复核：${all.length} 个差异点已全部自动处理（AI 高置信已落地/格式差异已化解）${btn}</div>`;
    }
    return `<div class="rv-filter">待处理 <b>${pending.length}</b> 项 · 已处理 ${handled.length} 项 ${btn}</div>`;
  })();
  if (!pending.length && !showHandled) {
    box.innerHTML = filterBar;
    bindReviewToggle();
    return;
  }
  // 保留**原始索引**（data-idx → reviewState.items[i]），过滤后索引会错位
  const list = all.map((it, i) => ({ it, i })).filter(x => showHandled || !isHandled(x.it));
  box.innerHTML = filterBar + (pending.length ? tip : '') + list.map(({ it, i }) => {
    const resolved = isHandled(it);
    const aiText = it.ai_verdict
      ? `AI 建议: <span class="rv-ai">${it.ai_verdict}<span class="conf"> (${it.confidence})</span>${it.ai_reason ? ' · ' + escapeHtml(it.ai_reason) : ''}</span>`
      : '';
    const choiceBtns = ['mineru', 'paddleocr', 'both', 'neither'].map(c => {
      const picked = it.user_choice === c ? ' picked' : '';
      return `<button class="rv-choice${picked}" data-idx="${i}" data-choice="${c}">${{ mineru: 'A 保留 MinerU', paddleocr: 'B 用 PaddleOCR', both: 'C 两者皆可', neither: 'D 都错' }[c]}</button>`;
    }).join('');
    return `<div class="rv-item${resolved ? ' resolved' : ''}" data-idx="${i}">
      <div class="rv-head">
        <span>📄 第 ${it.page} 页</span>
        <span class="rv-state ${resolved ? 'done' : 'pending'}">${resolved ? '已处理' : '待处理'}</span>
        ${aiText}
      </div>
      <div class="rv-cols">
        <div class="rv-col"><b>MinerU（主）</b>${renderDiffHtml(it.mineru_html)}</div>
        <div class="rv-col"><b>PaddleOCR（辅）</b>${renderDiffHtml(it.paddleocr_html)}</div>
      </div>
      <div class="rv-choices">${choiceBtns}</div>
    </div>`;
  }).join('');
  bindReviewToggle();
  box.querySelectorAll('.rv-item').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.rv-choice')) return;
      const i = Number(el.dataset.idx);
      reviewState.activeIdx = i;
      box.querySelectorAll('.rv-item').forEach(x => x.classList.toggle('hl', x.dataset.idx == i));
      loadReviewPage(reviewState.items[i].page);
    });
  });
  box.querySelectorAll('.rv-choice').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const i = Number(btn.dataset.idx);
      btn.disabled = true;
      try {
        const r = await api(`/api/papers/${reviewState.paperId}/review/choose`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_idx: i, choice: btn.dataset.choice }),
        });
        reviewState.items[i].user_choice = btn.dataset.choice;
        renderReviewItems();
        refreshReviewGate();   // P12F：处理完差异点 → 汇总条就绪数更新
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'review',
                      message: `识别复核[${reviewState.paperId}] 第${reviewState.items[i].page}页 ${btn.dataset.choice}（替换段落 ${r.changed_paragraphs} 个）` });
      } catch (err) { alert('应用失败：' + err.message); btn.disabled = false; }
    });
  });
}

/* F2（2026-09-12）：复核页左栏两个常驻节点访问器——img 与空态。
   img 缺失则**重建**（历史会话可能已被旧代码销毁），任何情况下都不返回 null 后再赋值。 */
function reviewPageImg() {
  let img = $('review-page-img');
  const box = $('review-pdf');
  if (!box) return null;
  if (!img) {
    img = document.createElement('img');
    img.id = 'review-page-img';
    img.alt = 'PDF 页（高亮=仲裁点）';
    box.insertBefore(img, box.firstChild);
  }
  return img;
}

function showReviewPdfEmpty(text) {
  const box = $('review-pdf');
  if (!box) return;
  let tip = $('rv-pdf-empty');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'rv-pdf-empty';
    tip.className = 'rv-pdf-empty';
    box.appendChild(tip);
  }
  tip.textContent = text || '';
  tip.style.display = text ? 'block' : 'none';
  const img = $('review-page-img');
  if (img) img.style.display = text ? 'none' : 'block';
}

function loadReviewPage(page) {
  const img = reviewPageImg();
  if (!img) return;                 // 复核模态不在 DOM 里（防御；不抛错）
  showReviewPdfEmpty('');
  reviewState.curPage = page;
  $('rv-page-info').textContent = reviewState.pageCount > 1
    ? `第 ${page} 页 / 共 ${reviewState.pageCount} 页` : `第 ${page} 页`;
  // 高亮：该页全部**待处理**项的行级 bbox（精确到行；无则块级兜底；P12F：已处理不标红）
  const hls = reviewState.items
    .filter(it => it.page === page && !isHandled(it))
    .flatMap(it => (it.line_bboxes && it.line_bboxes.length ? it.line_bboxes : [it.bbox]))
    .map(b => (b || []).join(','));
  const hl = hls.length ? '?hl=' + hls.join(';') : '';
  img.src = `/api/papers/${reviewState.paperId}/review/pdf-page/${page}${hl}`;
  img.style.width = (100 * reviewState.zoom) + '%';
  img.onerror = () => { $('rv-pdf-tip').textContent = 'PDF 页渲染失败（可能源文件缺失）'; };
}

// 2026-09-12 批1：删除 loadPendingRules() / rulesBatch()——
// 「待确认学习规则」卡片为 P12 差异挖掘的僵尸面（清单恒空、批准不影响解析），
// 卡片与 /api/settings/rules{,/batch} 端点一并删除。

async function selectPaper(id) {
  state.currentPaperId = id;
  renderPapers();
  const p = getPaper(id);
  $('chat-title').textContent = shortTitle(p ? (p.filename || p.title) : '', 60);
  const srcBadge = p ? (PARSE_SOURCE_META[p.parse_source] ? PARSE_SOURCE_META[p.parse_source].label : '') : '';
  $('chat-sub').textContent = p
    ? `状态: ${(STATUS_MAP[p.status] || [])[1] || p.status}${srcBadge ? ' · 解析来源: ' + srcBadge : ''}`
    : '';
  if (p && paperReadyForQa(p)) {
    // G12：右槽面板跟随所选文献（若有匹配面板则激活，否则更新当前面板）
    ensureReaderPanel();
    const match = readerPanels.findIndex(x => x.paperId === id);
    if (match >= 0) { activateReaderPanel(match); }
    else { readerState.paperId = id; syncActiveReaderPanel(); renderPaperReader(id); }
    // G10：文献会话严格绑定——有则打开最近，无则自动创建（每篇文献一个独立会话）
    try {
      const mine = state.sessions.filter(s => s.kind === 'paper' && s.paper_id === id);
      if (mine.length) {
        await openSession(mine[mine.length - 1].id);
        return;
      }
      // P1：单飞锁——快速连点文献卡时，避免并发 POST 建出两个空分支会话
      if (state.paperSessionOpening === id) return;
      state.paperSessionOpening = id;
      try {
        const rs = await api(`/api/papers/${id}/sessions`);
        if (rs.sessions.length) {
          await openSession(rs.sessions[rs.sessions.length - 1].id);
          return;
        }
        const created = await api(`/api/papers/${id}/sessions`, { method: 'POST' });
        await loadSessions();
        await openSession(created.id);
      } finally {
        state.paperSessionOpening = null;
      }
    } catch (e) {
      $('messages').innerHTML = '<div class="empty">会话加载失败：' + escapeHtml(e.message) + '</div>';
    }
  } else if (p && p.status === 'failed') {
    $('messages').innerHTML = `<div class="empty">❌ 处理失败：${escapeHtml(p.error || '未知错误')}<br><span class="muted">可在左侧文献卡片点「重试」重新解析（已修复解析来源报错）</span></div>`;
  } else {
    $('messages').innerHTML = '<div class="empty">文献尚未完成翻译，完成后可提问与预览</div>';
  }
}

/* ══════════ 对话 ══════════ */
async function loadHistory(sessionId) {
  try {
    const data = await api(`/api/chat/${sessionId}/history`);
    const box = $('messages');
    box.innerHTML = '';
    if (!data.messages.length) {
      box.innerHTML = '<div class="empty">开始提问吧（回答基于论文片段/知识库笔记，标注来源）</div>';
      return;
    }
    for (const m of data.messages) appendMsg(m.role, m.content);
  } catch (e) { console.warn('loadHistory failed', e); }
}

function appendMsg(role, content, cached = false) {
  const box = $('messages');
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  if (role === 'assistant' && cached) {
    div.innerHTML = '<span class="cache-badge">缓存命中 · 0 token</span>' + renderMarkdown(content, null);
  } else if (role === 'assistant') {
    // 2026-08-27：对话回答 markdown 渲染（表格/标题/公式/代码高亮，复用阅读器管线）
    div.innerHTML = renderMarkdown(content, null);
  } else {
    div.textContent = content;
  }
  // E3 写回飞轮：静态回答（历史/缓存）尾部挂「存 _qa」按钮；流式回答在 sendQuestion 结束后挂
  if (role === 'assistant' && content) attachQaSaveBtn(div, content);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

/* ══════════ E3 写回飞轮：助手回答一键存 _qa ══════════ */
function attachQaSaveBtn(msgEl, answerText) {
  if (!msgEl || msgEl.querySelector('.qa-save-btn')) return;
  // 全局知识库会话（kbmanage/qa）→ 存到全局知识库（_global/cards，不绑定单篇）；
  // 文献会话（paper）→ 存到该篇 cards/（需论文已纳入知识库）。
  const isGlobal = state.sessionKind === 'global';
  const doi = isGlobal ? '' : currentPaperDoi();
  const p = state.currentPaperId ? getPaper(state.currentPaperId) : null;
  const hasPaper = !!doi && !!p;
  const inKb = hasPaper && !!(p._kb && p._kb.in_kb);
  let canSave, scope, title, label, lockedMsg;
  if (isGlobal) {
    canSave = true;
    scope = 'global';
    title = '把这条问答存入全局知识库 _global/cards（所有文献可检索）';
    label = '📥 存 知识库QA';
    lockedMsg = '';
  } else {
    canSave = inKb;
    scope = 'paper';
    title = inKb
      ? '把这条问答写入该篇知识库的 cards/（全局可检索）'
      : (hasPaper ? '需先将该文献纳入知识库并编译，才能保存问答'
                  : '需在文献会话中绑定已纳入知识库的论文，才能保存问答');
    label = inKb ? '📥 存 _qa' : '🔒 存 _qa';
    // 2026-09-12 修：锁定提示必须区分"未入库 / 未绑定论文"两种原因（旧版一律说未纳入，误导）
    lockedMsg = hasPaper
      ? '该文献未纳入知识库，无法保存问答（请在文献库点「📥 导入kb」或知识库「纳入kb」并编译）'
      : '当前会话未绑定文献（或文献记录已不在），无法保存到单篇 _qa';
  }
  const btn = document.createElement('button');
  btn.type = 'button';
  // 锁态不禁用点击（点击时先向服务端实时复核一次 kb 状态，避免旧缓存把人挡在门外）
  btn.className = 'qa-save-btn' + (canSave ? '' : ' disabled');
  btn.title = title;
  btn.textContent = label;
  btn.addEventListener('click', async () => {
    if (btn.dataset.done) return;
    const question = prevUserQuestion(msgEl);
    if (!question) { alert('未找到该回答对应的用户问题，无法保存'); return; }
    if (!canSave) {
      btn.disabled = true;
      const ok = await paperInKbLive(p);      // 实时复核（缓存可能是入库前的旧值）
      btn.disabled = false;
      if (!ok) { alert(lockedMsg); return; }
      canSave = true;
      btn.classList.remove('disabled');
      btn.textContent = '📥 存 _qa';
      renderPapers();                          // 同步列表徽标
    }
    btn.disabled = true;
    try {
      await api('/api/kb-meta/qa/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, answer: answerText, doi, sources: doi ? [doi] : [], scope }),
      });
      btn.textContent = '已存 ✓';
      btn.dataset.done = '1';
    } catch (e) {
      btn.textContent = label;
      alert('保存失败：' + e.message);
    } finally {
      btn.disabled = false;
    }
  });
  msgEl.appendChild(btn);
  return btn;
}

// 取该条助手消息之前最近的一条 user 消息文本作为 question
function prevUserQuestion(msgEl) {
  let el = msgEl.previousElementSibling;
  while (el) {
    if (el.classList && el.classList.contains('msg') && el.classList.contains('user')) {
      return el.textContent.trim();
    }
    el = el.previousElementSibling;
  }
  return '';
}

// paper 会话 → 从 state.papers 取该篇 doi；global/chat 会话 → ''
function currentPaperDoi() {
  if (state.sessionKind === 'paper' || state.currentPaperId) {
    const p = getPaper(state.currentPaperId);
    if (p && p.doi) return p.doi;
  }
  return '';
}

function updateInputContext() {
  const ctx = $('input-context');
  if (!state.sessionId) { ctx.textContent = ''; return; }
  if (state.sessionKind === 'global') {
    const s = state.sessions.find(x => x.id === state.sessionId);
    ctx.innerHTML = (s && s.mode === 'manage')
      ? '⚙️ 知识库管理模式 · 问答 + 编译/元数据/期刊工具'
      : '📚 知识库问答 · 基于编译产物与元数据';
  } else if (state.sessionKind === 'chat') {
    ctx.innerHTML = '💬 普通聊天 · 不绑定论文';
  } else {
    const p = getPaper(state.currentPaperId);
    ctx.innerHTML = `📄 文献会话 · ${escapeHtml(shortTitle(p ? (p.filename || p.title) : '论文', 40))} · 基于原文（en.md）+ 编译笔记`;
  }
}

/* ══════════ 问答思考反馈（2026-09-12 用户反馈）══════════
   ① 等待期可见反馈：计时 + 阶段文案（检索/思考），回答首字到达即停；
   ② 思维链折叠框：供应商返回 delta.reasoning_content 时逐片填充，done 后自动收起。 */
let chatWaitTimer = null;
let chatWaitPh = null;    // 当前等待占位节点（计时器直接更新它，避免脆弱的 DOM 选择器）

function startWaitFeedback() {
  const el = $('chat-status');
  const t0 = Date.now();
  if (chatWaitTimer) clearInterval(chatWaitTimer);
  const tick = () => {
    const s = Math.max(0, Math.round((Date.now() - t0) / 1000));
    if (el) el.textContent = `⏳ 正在处理… 已等待 ${s}s`;
    if (chatWaitPh && !chatWaitPh.dataset.stage) chatWaitPh.textContent = `⏳ 正在处理… ${s}s`;
  };
  tick();
  chatWaitTimer = setInterval(tick, 1000);
}

function stopWaitFeedback() {
  if (chatWaitTimer) { clearInterval(chatWaitTimer); chatWaitTimer = null; }
  chatWaitPh = null;
  const el = $('chat-status');
  if (el) el.textContent = '';
}

function ensureThinkBox(answerBox) {
  let box = answerBox.querySelector('details.think-box');
  if (box) return box;
  box = document.createElement('details');
  box.className = 'think-box';
  box.open = true;    // 流式期间展开给人看；done 时收起
  box.innerHTML = '<summary>🧠 思考过程</summary><div class="think-text"></div>';
  answerBox.insertBefore(box, answerBox.firstChild);
  return box;
}

async function sendQuestion() {
  const input = $('question-input');
  const question = input.value.trim();
  if (!question || state.busy || !state.sessionId) return;
  state.busy = true;
  $('send-btn').disabled = true;
  $('send-btn').textContent = '思考中…';
  input.value = '';
  appendMsg('user', question);

  const answerBox = appendMsg('assistant', '');
  let answerText = '';
  let thinkBox = null;     // 思维链折叠框（首个 reasoning 事件时惰性创建）
  let thinkText = '';
  let cached = false;
  // 2026-09-12 用户反馈：思考期间只有"思考中…"按钮文案，用户不知道还要等多久。
  // 加可见的计时占位（首个 delta/reasoning 到达即撤掉——旧版把占位写进
  // answerBox.textContent，正文一来变成"另建节点"，占位文字会残留在回答上方）。
  const waitPh = document.createElement('div');
  waitPh.className = 'wait-ph muted';
  waitPh.textContent = '⏳ 正在处理…';
  answerBox.appendChild(waitPh);
  chatWaitPh = waitPh;
  startWaitFeedback();
  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, question,
                             effort: ($('chat-effort') || {}).value || 'auto' }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      answerBox.textContent = '⚠️ ' + (j.detail || res.statusText);
      answerText = answerBox.textContent;
    } else {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
          if (!part.startsWith('data: ')) continue;
          const ev = JSON.parse(part.slice(6));
          if (ev.type === 'start') {
            cached = ev.cached;
            // ⚠️ 不能写 answerBox.textContent（会抹掉等待占位与思考框）——只更新占位文案
            if (!ev.cached && ev.retrieved !== undefined) {
              const ph = answerBox.querySelector('.wait-ph');
              if (ph) { ph.textContent = `⏳ 已检索 ${ev.retrieved} 个片段，正在思考…`; ph.dataset.stage = '1'; }
            }
          } else if (ev.type === 'status') {
            const el = $('chat-status');
            if (el) el.textContent = '⏳ ' + (ev.text || '');
          } else if (ev.type === 'reasoning') {
            // 思维链增量 → 折叠框（DeepSeek Harness 同款体验）
            thinkBox = thinkBox || ensureThinkBox(answerBox);
            thinkText += (ev.text || '');
            const tt = thinkBox.querySelector('.think-text');
            if (tt) tt.textContent = thinkText;
            const ph = answerBox.querySelector('.wait-ph');
            if (ph) ph.remove();
          } else if (ev.type === 'tool') {
            // 管理模式工具调用事件（折叠展示）
            const d = document.createElement('div');
            d.className = 'tool-call';
            d.innerHTML = (ev.ok ? '🔧 ' : '⚠️ ') + escapeHtml(ev.name) +
              ' <span class="muted">' + escapeHtml(JSON.stringify(ev.args || {}).slice(0, 100)) + '</span>' +
              '<div class="tool-summary">' + escapeHtml((ev.summary || '').slice(0, 140)) + '</div>';
            answerBox.appendChild(d);
          } else if (ev.type === 'delta') {
            const ph = answerBox.querySelector('.wait-ph');
            if (ph) ph.remove();   // 首个正文 chunk 到达 → 撤掉等待占位（旧版会残留）
            answerText += ev.text;
            let t = answerBox.querySelector('.answer-text');
            if (!t) { t = document.createElement('div'); t.className = 'answer-text'; answerBox.appendChild(t); }
            t.innerHTML = renderMarkdown(answerText, null);
            answerBox.scrollIntoView({ block: 'end' });
          } else if (ev.type === 'done') {
            if (thinkBox) thinkBox.open = false;   // 回答结束 → 收起思考过程
            if (ev.cached) {
              answerBox.innerHTML = '<span class="cache-badge">缓存命中 · 0 token</span>' + renderMarkdown(answerText, null);
              thinkBox = null;
            }
          } else if (ev.type === 'error') {
            answerBox.textContent = '⚠️ ' + ev.message;
          }
        }
      }
    }
    if (!answerText && !thinkText && !answerBox.querySelector('.tool-call')
        && !answerBox.textContent.startsWith('⚠️')) answerBox.textContent = '（无回答）';
  } catch (e) {
    answerBox.textContent = '⚠️ 请求失败：' + e.message;
  } finally {
    state.busy = false;
    $('send-btn').textContent = '发送';
    stopWaitFeedback();
    const ph = answerBox.querySelector('.wait-ph');
    if (ph) ph.remove();
    updateSendBtn();
    input.focus();
    updateTokenBar();
    // E3 写回飞轮：流式回答结束后挂「存 _qa」按钮（报错/无回答不挂）
    if (answerText && !answerBox.textContent.startsWith('⚠️')) attachQaSaveBtn(answerBox, answerText);
    loadSessions(); // 消息数/标题刷新
  }
}

/* G13：发送按钮启用状态（会话就绪 + 输入非空 + 非忙碌） */
function updateSendBtn() {
  const input = $('question-input');
  $('send-btn').disabled = !(state.sessionId && input && input.value.trim() && !state.busy);
}

/* ══════════ 知识库回收站（2026-09-12 用户需求）══════════
   "移除知识库即可，或者说是移除到回收站，编译产物这些都统一保留，只是不再会被知识库检索，
    回收站这些文献在主界面添加一个恢复按钮。在回收站的文献不再知识库内显示。"
   数据源：GET /api/kb-meta/kb/trash；恢复：POST /api/kb-meta/kb/restore。 */
async function refreshTrashCount() {
  const el = $('kb-trash-count');
  if (!el) return;
  try {
    const r = await api('/api/kb-meta/kb/trash');
    const n = (r.items || []).length;
    el.textContent = n ? `(${n})` : '';
  } catch (e) { el.textContent = ''; }
}

async function openTrashModal() {
  const mask = $('trash-modal');
  const body = $('trash-body');
  if (!mask || !body) return;
  mask.style.display = 'flex';
  body.innerHTML = '<div class="empty">加载中…</div>';
  let items = [];
  try {
    const r = await api('/api/kb-meta/kb/trash');
    items = r.items || [];
  } catch (e) {
    body.innerHTML = '<div class="empty">加载失败：' + escapeHtml(e.message) + '</div>';
    return;
  }
  if (!items.length) {
    body.innerHTML = '<div class="empty">回收站是空的</div>';
    return;
  }
  body.innerHTML =
    `<div class="muted trash-hint">共 ${items.length} 篇 · 文件与编译产物全部保留，`
    + '恢复后立即重新进入知识库与检索</div>'
    + items.map(it => `<div class="trash-row" data-key="${escapeHtml(it.key)}">
        <span class="trash-title">${escapeHtml(shortTitle(it.title || it.key, 56))}</span>
        <span class="trash-meta muted">${escapeHtml(
          [it.journal, it.year, it.doi || it.key, it.trashed_at].filter(Boolean).join(' · '))}</span>
        <button class="btn small primary trash-restore" data-key="${escapeHtml(it.key)}">↩ 恢复</button>
      </div>`).join('');
  body.querySelectorAll('.trash-restore').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      guardBtn(btn, async () => {
        try {
          const r = await api('/api/kb-meta/kb/restore', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: btn.dataset.key }),
          });
          toast('↩ 已恢复：' + (r.kb_dir || r.key), 'ok');
          await openTrashModal();
          await loadKbList();
          await loadPapers();
          refreshTrashCount();
        } catch (err) {
          toast('恢复失败：' + err.message, 'err');
        }
      }, '恢复中…');
    });
  });
}

function bindChat() {
  $('send-btn').addEventListener('click', sendQuestion);
  $('question-input').addEventListener('keydown', (e) => {
    // P2 审计 1.6：Ctrl/Alt+Enter 也换行（仅裸 Enter 发送）
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); sendQuestion(); }
  });
  $('question-input').addEventListener('input', updateSendBtn);
  // 思考强度（reasoning_effort）：auto = 不发送，用供应商默认。
  // 存在服务端设置里（chat_reasoning_effort）→ 刷新后仍生效。
  const effBox = $('chat-effort');
  if (effBox) {
    effBox.addEventListener('change', async () => {
      const el = $('chat-status');
      const v = effBox.value || 'auto';
      try {
        await api('/api/settings/chat-reasoning-effort', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ effort: v }),
        });
        if (el) {
          el.textContent = v === 'auto' ? '思考强度：自动（供应商默认）'
            : `思考强度：${v === 'high' ? '高' : '低'}（下一轮提问生效）`;
          setTimeout(() => { if (el.textContent.startsWith('思考强度')) el.textContent = ''; }, 4000);
        }
      } catch (e) {
        if (el) el.textContent = '⚠ 思考强度保存失败：' + (e && e.message ? e.message : e);
      }
    });
  }
  // P2-7：最近对话切换（绑定在 #main 内元素上，随弹出/放回迁移）
  const sw = $('chat-switch');
  if (sw) {
    sw.addEventListener('change', () => {
      const v = sw.value;
      if (v) openSession(Number(v));
    });
  }
}

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
// 中文=zh.md / 图片=images/ / 笔记(_note,L1) / 详解(_details,L2) / 深度(_wiki,L3) / PDF=source.pdf
const KB_FILES = [
  { name: 'en.md', label: '📄 原文' },
  { name: 'source.pdf', label: '📄 PDF' },
  { name: 'en_zh.md', label: '🌐 双语对照' },
  { name: 'zh.md', label: '🇨🇳 中文' },
  { name: '__images__', label: '🖼 图片' },
  { name: '_note.md', label: '📝 笔记(L1)' },
  { name: '_details.md', label: '📑 详解(L2)' },
  { name: '_wiki.md', label: '🧠 深度(L3)' },
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
  md = md.replace(/\$([^$\n]+?)\$/g, (m, tex) => ph('math', 'inline', tex));

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

/* ══════════ P12F：复核区 diff 渲染（绕过 marked，直插 + 逐文本节点 KaTeX）══════════ */
// 后端 _diff_html 输出已转义文本段的安全 HTML（仅 <mark>/<span> 结构）。
// 不经过 marked/整串 KaTeX 正则——旧链路中 $ ^{ 与 } $ 会**跨 <mark> 标签配对**，
// 把标签当 TeX 渲染成乱码（`</mark>a,1<mark class="rv−diff">`，空格塌缩+U+2212）。
function renderDiffHtml(html) {
  const box = document.createElement('div');
  box.className = 'rv-md';
  box.innerHTML = html || '（空）';
  if (typeof katex !== 'undefined') {
    // 逐文本节点做 KaTeX：$ 只在本文本节点内配对，绝不跨元素（杜绝再次乱码）
    const nodes = [];
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, null);
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(mathifyTextNode);
  }
  return box.outerHTML;
}

function mathifyTextNode(node) {
  const v = node.nodeValue;
  if (!v || v.indexOf('$') === -1) return;
  const re = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g;
  const frag = document.createDocumentFragment();
  let last = 0, m, done = false;
  while ((m = re.exec(v))) {
    const isDisplay = m[1] !== undefined;
    const tex = isDisplay ? m[1] : m[2];
    if (m.index > last) frag.appendChild(document.createTextNode(v.slice(last, m.index)));
    try {
      const span = document.createElement('span');
      span.innerHTML = katex.renderToString(tex, { throwOnError: false, displayMode: isDisplay });
      frag.appendChild(span);
    } catch (e) { frag.appendChild(document.createTextNode(m[0])); }
    last = m.index + m[0].length;
    done = true;
  }
  if (!done) return;
  if (last < v.length) frag.appendChild(document.createTextNode(v.slice(last)));
  node.parentNode.replaceChild(frag, node);
}

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
  refreshTrashCount();
  // T7：悬停预览弹窗——进入弹窗不关闭，离开行/弹窗 200ms 后消失；列表滚动时收起
  const pop = $('kbl-preview');
  if (pop) {
    pop.addEventListener('mouseenter', () => clearTimeout(kblpHideTimer));
    pop.addEventListener('mouseleave', () => scheduleKbPreviewHide());
  }
  const lw = $('kb-list-wrap');
  if (lw) lw.addEventListener('scroll', scheduleKbPreviewHide);
}

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
/* 2026-09-12 用户反馈：已完成 L1 的篇仍显示「编译L1」，点了没反应（后端返回 skipped_done）。
   三级产物（paperkb/compile.py）：L1=`_note.md` 六维笔记 / L2=`_details.md` 详解 /
   L3=`_wiki.md` + `_concepts/`（**"按 wiki 知识库标准编译"就是这一级**）。 */
const KB_LEVELS = ['L1', 'L2', 'L3'];
const KB_LEVEL_TIP = {
  L1: '生成六维笔记 _note.md',
  L2: '生成详解 _details.md（在 L1 基础上展开）',
  L3: '生成 wiki 知识库 _wiki.md + _concepts/（"按 wiki 标准编译"= 这一级）',
};

function kbNextLevel(it) {
  const done = (it && it.compiled) || [];
  return KB_LEVELS.find(l => !done.includes(l)) || '';
}

function kbCompileBtnHtml(it) {
  const next = kbNextLevel(it);
  if (!next) {
    return '<button class="btn small" disabled title="三级编译已全部完成'
      + '（L1 _note.md / L2 _details.md / L3 _wiki.md）">✓ 三级已编译完成</button>';
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

/* ══════════ Token 标签栏 ══════════ */
async function updateTokenBar() {
  try {
    const g = await api('/api/usage/summary');
    // 语义：输入=prompt_tokens（含缓存命中），输出=completion_tokens（不含缓存）
    $('tb-global').textContent =
      `累计 ${g.calls} 次 · 输入 ${g.prompt_tokens}(缓存 ${g.cache_hit_tokens}) · 输出 ${g.completion_tokens}`;
    $('tb-cost').textContent = `≈¥${g.cost.toFixed(4)}`;
    if (state.sessionId) {
      const s = await api(`/api/usage/summary?context_prefix=session:${state.sessionId}`);
      $('tb-session').title =
        `输入 ${s.prompt_tokens}(缓存 ${s.cache_hit_tokens}) · 输出 ${s.completion_tokens} · ≈¥${s.cost.toFixed(4)}`;
      $('tb-session').textContent = `会话 ${s.calls} 次 · ${s.total_tokens} tok · ≈¥${s.cost.toFixed(4)}`;
    } else {
      $('tb-session').textContent = '会话 —';
    }
  } catch (e) { /* 服务未就绪 */ }
}

/* ══════════ 信息面板（事件流）══════════ */
const eventState = { es: null, filter: 'all', expanded: false, errorCount: 0, domCount: 0, last: '' };

function bindEventPanel() {
  $('event-toggle').addEventListener('click', () => {
    eventState.expanded = !eventState.expanded;
    $('event-panel').classList.toggle('expanded', eventState.expanded);
    $('event-arrow').textContent = eventState.expanded ? '▼' : '▲';
  });
  // P5 点2：清空信息面板日志（仅清前端 DOM 显示）
  const _ec = $('event-clear');
  if (_ec) _ec.addEventListener('click', () => {
    const list = $('event-list');
    if (list) list.innerHTML = '';
    eventState.errorCount = 0; eventState.domCount = 0; eventState.last = '';
    const b = $('event-badge'); if (b) b.textContent = '';
    const l = $('event-last'); if (l) l.textContent = '—';
  });
  $('event-filter').addEventListener('change', (e) => {
    eventState.filter = e.target.value;
    applyEventFilter();
  });
}

function appendEvent(ev) {
  const list = $('event-list');
  const div = document.createElement('div');
  div.className = `evt ${ev.level}`;
  div.dataset.source = ev.source;
  // 超长消息截断显示（≤240 字符），完整内容放 title 悬停查看（防拉长面板）
  const msg = String(ev.message || '');
  const shown = msg.length > 240 ? msg.slice(0, 240) + '…' : msg;
  div.innerHTML =
    `<span class="evt-ts">${escapeHtml(ev.ts)}</span>` +
    `<span class="evt-lv">${ev.level}</span>` +
    `<span class="evt-src">${escapeHtml(ev.source)}</span>` +
    `<span class="evt-msg" title="${escapeHtml(msg)}">${escapeHtml(shown)}</span>`;
  list.appendChild(div);
  eventState.domCount++;
  while (eventState.domCount > 300) {
    const first = list.firstElementChild;
    if (first) { first.remove(); eventState.domCount--; }
  }
  eventState.last = `[${ev.ts}] ${ev.source}: ${ev.message}`;
  $('event-last').textContent = eventState.last;
  if (ev.level === 'error') {
    eventState.errorCount++;
    $('event-badge').textContent = `⚠ ${eventState.errorCount}`;
    if (!eventState.expanded) {
      eventState.expanded = true;
      $('event-panel').classList.add('expanded');
      $('event-arrow').textContent = '▼';
    }
  }
  div.style.display = eventMatches(ev) ? '' : 'none';
}

function eventMatches(ev) {
  const f = eventState.filter;
  if (f === 'all') return true;
  if (f === 'error') return ev.level === 'error' || ev.level === 'warning';
  return ev.source === f;
}

function applyEventFilter() {
  document.querySelectorAll('#event-list .evt').forEach(el => {
    const src = el.dataset.source;
    const lv = el.classList.contains('error') ? 'error'
      : (el.classList.contains('warning') ? 'warning' : 'info');
    el.style.display = eventMatches({ source: src, level: lv }) ? '' : 'none';
  });
}

function connectEventStream() {
  if (eventState.es) eventState.es.close();
  const es = new EventSource('/api/events/stream');
  eventState.es = es;
  es.onmessage = (ev) => {
    if (!ev.data || ev.data.startsWith(':')) return;
    try {
      const e = JSON.parse(ev.data);
      appendEvent(e);
      maybeRefreshKbFiles(e);      // 2026-09-12：编译/保存事件 → 失效文件集缓存并刷新阅读器标签
    } catch (err) { /* 忽略坏事件 */ }
  };
  es.onerror = () => { /* EventSource 内建重连 */ };
}

/* 编译完成/知识库写盘/任务完成 → 失效 kb 文件集缓存，并（若阅读器开着）重取标签页。
   合并连续事件（编译一次会连发多条），debounce 1.2s；只对**当前打开的目录**重取。 */
let kbFileRefreshTimer = null;

function maybeRefreshKbFiles(ev) {
  const cat = String((ev && ev.category) || '');
  if (!['compile_done', 'file_saved', 'task_done', 'compile_queue'].includes(cat)) return;
  const dir = readerState.dir;
  invalidateKbFileSet(dir || null);
  clearTimeout(kbFileRefreshTimer);
  if (!dir) return;
  kbFileRefreshTimer = setTimeout(() => {
    if (readerState.dir !== dir) return;
    ensureKbFileSet(dir, true).then(set => {
      if (readerState.dir === dir && set) renderKbTabs(dir, readerState.file);
    });
  }, 1200);
}

/* ══════════ 设置中心 ══════════ */
const settingsState = { providers: [], active: '', editing: -1, prices: null };
const KEY_MASK = '••••••••';   // P：密钥编辑框掩码占位（已设置时显示，而非空串）

/* 2026-09-13 IA 重排：设置中心 7 tab → 6 tab，tab 键名冻结
   model / parse / kb / ui / plugin / about（原 display+appearance 合并为 ui）。
   改这里必须同步改三处：本数组、#settings-modal .stab 的 data-stab、stab-<key> 容器 id。 */
const SETTINGS_TABS = ['model', 'parse', 'kb', 'ui', 'plugin', 'about'];

/* 设置弹窗的可聚焦元素集合（焦点陷阱用，仅在 #settings-modal 内查询） */
const FOCUSABLE_SEL = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]),'
  + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/* 未保存变更标记：按「组」记录——组内任一控件 change 即置脏，主保存按钮成功后清脏。
   声明式配置代替"给每个控件单独绑一堆重复代码"；用 #settings-modal 上的事件委托捕获。 */
const SETTINGS_DIRTY_GROUPS = [
  { group: 'parse', btn: 'parse-save', flag: 'parse-dirty', page: 'stab-parse',
    items: ['mineru-key', 'mineru-language', 'mineru-is-ocr', 'mineru-enable-table',
            'po-token', 'po-base', 'po-model',
            'po-restructure-pages', 'po-merge-tables', 'po-relevel-titles',
            'parse-mode', 'parse-gate', 'parse-interval',
            'parse-ai-review', 'parse-skip-review'] },
  { group: 'ui', btn: 'ui-save-all', flag: 'ui-dirty', page: 'stab-ui',
    items: ['disp-md-template', 'sys-prompt-input', 'custom-css-input'] },
  { group: 'kb', btn: 'kb-save', flag: 'kb-dirty', page: 'stab-kb',
    items: ['kb-copy-mode', 'retrieval-mode', 'compile-effort', 'translate-effort'] },
];
const DIRTY_BY_ID = (() => {
  const m = {};
  SETTINGS_DIRTY_GROUPS.forEach(g => g.items.forEach(id => { m[id] = g.flag; }));
  return m;
})();
const DIRTY_BTN_OF = (() => {
  const m = {};
  SETTINGS_DIRTY_GROUPS.forEach(g => { m[g.flag] = g.btn; });
  return m;
})();

function markDirty(flagId, on) {
  const flag = $(flagId);
  if (!flag) return;
  flag.hidden = !on;
  const btn = $(DIRTY_BTN_OF[flagId]);
  if (btn) btn.classList.toggle('dirty', !!on);
}
function isDirty(flagId) { const f = $(flagId); return !!f && !f.hidden; }

function bindSettingsDirtyTracking() {
  const modal = $('settings-modal');
  if (!modal || modal.dataset.dirtyBound === '1') return;
  modal.dataset.dirtyBound = '1';
  // change 只由用户交互触发（脚本改 .value 不触发），因此回填/切 tab 不会误置脏
  // 选择器排除 checkbox/radio/button：四个自动保存开关与各保存按钮不参与"未保存"标记
  modal.addEventListener('change', (ev) => {
    const el = ev.target;
    if (!el || !el.id) return;
    if (el.matches('input[type="checkbox"], input[type="radio"], button')) return;
    const flag = DIRTY_BY_ID[el.id];
    if (flag) markDirty(flag, true);
  });
  // 文案区清空等场景用 input 也能及时亮起（仅 textarea 需要，不重复绑定一堆）
  modal.addEventListener('input', (ev) => {
    const el = ev.target;
    if (!el || !el.id || el.tagName !== 'TEXTAREA') return;
    const flag = DIRTY_BY_ID[el.id];
    if (flag) markDirty(flag, true);
  });
}

/* ── 设置弹窗可访问性：Esc 关闭（仅弹窗可见时）/ 焦点移入 / Tab 陷阱 / 焦点归还 ── */
let settingsOpener = null;
function closeSettings() {
  const modal = $('settings-modal');
  if (!modal || modal.style.display === 'none') return;
  modal.style.display = 'none';
  const back = settingsOpener;
  settingsOpener = null;
  if (back && back.isConnected && typeof back.focus === 'function') back.focus();  // 焦点回到打开它的按钮
}
function settingsKeydown(ev) {
  const modal = $('settings-modal');
  if (!modal || modal.style.display === 'none') return;   // 不劫持全局 Esc/Tab
  if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); closeSettings(); return; }
  if (ev.key !== 'Tab') return;
  const items = Array.from(modal.querySelectorAll(FOCUSABLE_SEL))
    .filter(el => el.offsetParent !== null || el === document.activeElement);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (ev.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
    ev.preventDefault(); last.focus();
  } else if (!ev.shiftKey && document.activeElement === last) {
    ev.preventDefault(); first.focus();
  }
}
function bindSettingsA11y() {
  const modal = $('settings-modal');
  if (!modal || modal.dataset.a11yBound === '1') return;
  modal.dataset.a11yBound = '1';
  modal.addEventListener('keydown', settingsKeydown);
  // 点遮罩空白处关闭（点弹窗内部不关）——与 Esc 一致，且同样归还焦点
  modal.addEventListener('mousedown', (ev) => { if (ev.target === modal) closeSettings(); });
}

function bindSettings() {
  bindSettingsA11y();
  bindSettingsDirtyTracking();
  $('settings-btn').addEventListener('click', (e) => { settingsOpener = e.currentTarget; openSettings(); });
  $('settings-close').addEventListener('click', () => closeSettings());
  $('provider-add').addEventListener('click', () => {
    settingsState.editing = -1;
    ['pf-name', 'pf-base', 'pf-model', 'pf-key', 'pf-max', 'pf-price-in', 'pf-price-cache', 'pf-price-out'].forEach(id => $(id).value = '');
    $('pf-test-result').textContent = '';
    $('provider-form').style.display = 'flex';
  });
  $('pf-key-toggle').addEventListener('click', () => {
    const k = $('pf-key');
    const show = k.type === 'password';
    k.type = show ? 'text' : 'password';
    $('pf-key-toggle').textContent = show ? '隐藏' : '显示';
  });
  $('pf-cancel').addEventListener('click', () => { $('provider-form').style.display = 'none'; });
  // P1：设置中心各「保存」= 写操作（POST /api/settings/*）→ guardBtn 防连点
  $('pf-save').addEventListener('click', (e) => guardBtn(e.currentTarget, () => saveProviders(), '保存中…'));
  $('pf-test').addEventListener('click', (e) => guardBtn(e.currentTarget, () => testProvider(), '测试中…'));
  $('kb-path-save').addEventListener('click', (e) => guardBtn(e.currentTarget, () => saveKbPath(), '保存中…'));
  // 2026-09-13：删除独立「保存 MinerU」#mineru-save —— MinerU Key + 参数并入 #parse-save
  $('parse-save').addEventListener('click', (e) => guardBtn(e.currentTarget, async () => {
    const ok = await saveParse();
    if (ok) markDirty('parse-dirty', false);
  }, '保存中…'));
  // P0-1 修复（2026-09-11）：auto_exit 由复选框自身 change 事件显式保存——
  // 不再借"保存解析设置"顺带隐式写入（那是后端被误杀的根因）。
  const aeBox = $('auto-exit');
  if (aeBox) {
    aeBox.addEventListener('change', async () => {
      const on = !!aeBox.checked;
      const el = $('parse-switch-result');   // 批1：与「保存解析设置」的提示分开，互不覆盖
      try {
        await api('/api/settings/auto-exit', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: on }),
        });
        state.autoExitOn = on;
        if (el) {
          el.classList.add('ok');
          el.textContent = on
            ? '✅ 已开启：关闭窗口/刷新页面会关停后端'
            : '✅ 已关闭：后端保持运行（推荐）';
        }
      } catch (e) {
        aeBox.checked = !on;   // 保存失败回滚勾选，避免界面与服务端不一致
        if (el) el.textContent = '❌ 自动退出开关保存失败：' + (e && e.message ? e.message : e);
      }
    });
  }
  // 2026-09-12：自动编译开关（后端 save_auto_compile 早已存在但无入口，补上）
  const acBox = $('auto-compile');
  if (acBox) {
    acBox.addEventListener('change', async () => {
      const on = !!acBox.checked;
      const el = $('parse-switch-result');   // 批1：独立提示位（原与保存提示共用会互相覆盖）
      try {
        await api('/api/settings/auto-compile', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: on }),
        });
        if (el) {
          el.classList.add('ok');
          el.textContent = on
            ? '✅ 已开启：解析完成后自动把该篇 L1 编译入队'
            : '✅ 已关闭：「解析+编译」将不再自动编译（文献只留在解析库）';
        }
      } catch (e) {
        acBox.checked = !on;   // 失败回滚勾选
        if (el) el.textContent = '❌ 自动编译开关保存失败：' + (e && e.message ? e.message : e);
      }
    });
  }
  // 2026-09-13 IA 重排：kb tab 主保存 = 复制方式 + 检索权限（路径仍为独立端点/独立按钮）
  const kbSaveBtn = $('kb-save');
  if (kbSaveBtn) {
    kbSaveBtn.addEventListener('click', (e) => guardBtn(e.currentTarget, async () => {
      const ok = await saveKbSettings();
      if (ok) markDirty('kb-dirty', false);
    }, '保存中…'));
  }
  const uiSaveBtn = $('ui-save-all');
  if (uiSaveBtn) {
    uiSaveBtn.addEventListener('click', (e) => guardBtn(e.currentTarget, async () => {
      const ok = await saveUiSettings();
      if (ok) markDirty('ui-dirty', false);
    }, '保存中…'));
  }
  // 单卡片保存按钮（模板/提示词/CSS）保留，成功后同样清除本组「未保存」标记
  const mdTplBtn = $('md-template-save');
  if (mdTplBtn) mdTplBtn.addEventListener('click', (e) => guardBtn(e.currentTarget, () => saveMdTemplate(), '保存中…'));
  $('plugin-install-btn').addEventListener('click', () => $('plugin-zip-input').click());
  $('plugin-zip-input').addEventListener('change', installPlugin);
  bindDisplaySettings();
  bindAppearance();
  // 设置分区 tab
  // ⚠️ 2026-09-12 用户反馈（导入弹窗 4 个 tab 点一个全变蓝）：`.stab` 是**三处共用**的类名
  // （设置中心 / 知识库管理 / 导入向导）。旧代码这里用全局 `.stab` 绑定 →
  // 点导入弹窗的 tab 会触发 `setSettingsTab(undefined)`（导入 tab 没有 data-stab），
  // 而 `undefined === undefined` 为真 ⇒ **所有导入 tab 一起被点亮**。
  // 必须限定作用域；`setSettingsTab` 内部同样限定。
  document.querySelectorAll('#settings-modal .stab').forEach(btn => {
    btn.addEventListener('click', () => setSettingsTab(btn.dataset.stab));
  });
}

function setSettingsTab(tab) {
  if (!SETTINGS_TABS.includes(tab)) tab = SETTINGS_TABS[0];   // 防御：未知键不再让所有页面隐藏
  document.querySelectorAll('#settings-modal .stab').forEach(
    b => b.classList.toggle('active', b.dataset.stab === tab));
  SETTINGS_TABS.forEach(p => {
    const el = $('stab-' + p);
    if (el) el.style.display = p === tab ? '' : 'none';
  });
  if (tab === 'plugin') loadPlugins();
  if (tab === 'parse') loadMineruStatus();
}

async function openSettings() {
  $('settings-modal').style.display = 'flex';
  // 2026-09-12 用户反馈：首次打开设置中心**整片空白**——`openSettings` 从不初始化分区 tab，
  // 而所有 `#stab-*` 页面在 HTML 里都是 `display:none`，于是"没有一个 tab 是 active、
  // 也没有一个页面可见"。这里固定打开第一个分区（模型），不做"记忆上次分区"。
  setSettingsTab('model');
  // a11y：焦点移入弹窗（关闭按钮），避免焦点留在背景页
  const firstBtn = document.querySelector('#settings-modal .settings-nav .stab');
  if (firstBtn) firstBtn.focus();
  SETTINGS_DIRTY_GROUPS.forEach(g => markDirty(g.flag, false));   // 每次打开按服务端值为准
  loadMineruStatus();
  try {
    const s = await api('/api/settings');
    settingsState.providers = s.providers;
    settingsState.active = s.active_provider;
    settingsState.prices = s.prices || null;   // T3：完整结构 {default, by_provider_model}
    renderProviders();
    $('kb-path-input').value = s.kb_path || '';
    $('kb-copy-mode').value = s.kb_copy_mode || 'copy';
    if ($('compile-effort')) $('compile-effort').value = s.compile_reasoning_effort || 'auto';
    if ($('translate-effort')) $('translate-effort').value = s.translate_reasoning_effort || 'auto';
    // 2026-09-13：MinerU Key 归属移动到 parse.mineru_api_key（旧字段 mineru.api_key 兼容兜底）
    $('mineru-key').value = s.parse?.mineru_api_key || s.mineru?.api_key || '';
    // P12：PDF 解析设置（双通道 + PaddleOCR-VL 辅通道 + P12F 复核门控）
    $('parse-mode').value = s.parse?.mode || 'dual';
    $('parse-ai-review').checked = s.parse?.ai_review !== false;
    $('parse-gate').value = s.parse?.translate_gate || 'wait';
    $('parse-skip-review').checked = !!s.parse?.skip_review_batch;
    // P0-1 修复（2026-09-11）：仅当服务端**明确**返回 true 才勾选，并记录"已确认"
    // 状态供 beforeunload 使用（旧实现把"未同步"等同于"开"，导致静默开启）。
    state.autoExitOn = (s.auto_exit === true);
    $('auto-exit').checked = state.autoExitOn;
    // 自动编译：服务端默认开（缺省=on），只有明确 false 才取消勾选
    if ($('auto-compile')) $('auto-compile').checked = (s.auto_compile !== false);
    $('parse-interval').value = s.parse?.parse_interval_sec ?? 8;
    $('po-token').value = s.parse?.paddleocr?.access_token || '';
    $('po-base').value = s.parse?.paddleocr?.base_url || '';
    $('po-model').value = s.parse?.paddleocr?.model_version || '';
    applyMineruParams(s.parse?.mineru_params);          // 缺失嵌套对象 → 默认值回填
    applyPaddleOptions(s.parse?.paddleocr?.options);    // 同上（后端可能尚未部署该字段）
    $('sys-prompt-input').value = s.system_prompt_extra || '';
    $('custom-css-input').value = s.custom_css || '';
    $('disp-md-template').value = s.md_template || 'obsidian_bilingual';
    $('retrieval-mode').value = s.retrieval_mode || 'notes';
    SETTINGS_DIRTY_GROUPS.forEach(g => markDirty(g.flag, false));   // 回填后清脏（change 不触发，双保险）
  } catch (e) { alert('加载设置失败：' + e.message); }
}

/* ── MinerU 参数 / PaddleOCR 选项：契约字段缺失时按默认值回填（后端可能未部署完成） ── */
const MINERU_PARAM_DEFAULTS = { language: 'auto', is_ocr: 'auto', enable_table: true };
const PADDLE_OPTION_DEFAULTS = { restructurePages: true, mergeTables: true, relevelTitles: true };

function applyMineruParams(p) {
  const v = Object.assign({}, MINERU_PARAM_DEFAULTS, p || {});
  const lang = ['auto', 'en', 'ch'].includes(v.language) ? v.language : 'auto';
  const ocr = ['auto', 'on', 'off'].includes(v.is_ocr) ? v.is_ocr : 'auto';
  $('mineru-language').value = lang;
  $('mineru-is-ocr').value = ocr;
  $('mineru-enable-table').checked = v.enable_table !== false && v.enable_table !== 0;
}
function applyPaddleOptions(o) {
  const v = Object.assign({}, PADDLE_OPTION_DEFAULTS, o || {});
  const box = { 'po-restructure-pages': 'restructurePages', 'po-merge-tables': 'mergeTables',
                'po-relevel-titles': 'relevelTitles' };
  Object.keys(box).forEach(id => {
    const el = $(id);
    if (el) el.checked = v[box[id]] !== false && v[box[id]] !== 0;
  });
}
function readMineruParams() {
  return {
    language: $('mineru-language').value,
    is_ocr: $('mineru-is-ocr').value,
    enable_table: !!$('mineru-enable-table').checked,
  };
}
function readPaddleOptions() {
  return {
    restructurePages: !!$('po-restructure-pages').checked,
    mergeTables: !!$('po-merge-tables').checked,
    relevelTitles: !!$('po-relevel-titles').checked,
  };
}

/* ── MinerU 状态条：读 /api/health 的 mineru_ready（缺失按 false 处理并警示） ── */
async function loadMineruStatus() {
  const bar = $('mineru-status');
  if (!bar) return;
  const ico = $('mineru-status-ico'), txt = $('mineru-status-text');
  try {
    const h = await api('/api/health');
    // 契约字段 mineru_ready 优先；后端尚未部署时回退 mineru_configured（≤2026-09-13 的旧名）
    const ready = (h.mineru_ready === true)
      || (h.mineru_ready === undefined && h.mineru_configured === true);
    const parser = h.mineru_parser || '—';
    bar.classList.toggle('warn', !ready);
    bar.classList.toggle('ok', ready);
    if (ready) {
      if (ico) ico.textContent = '✅';
      if (txt) txt.textContent = `MinerU 就绪（通道 ${parser}）`;
    } else {
      if (ico) ico.textContent = '⚠️';
      if (txt) txt.textContent = '未配置 MinerU API Key：解析功能已被禁用。请在下方填写 Key 并保存后再导入 PDF';
    }
  } catch (e) {
    bar.classList.remove('ok');
    bar.classList.add('warn');
    if (ico) ico.textContent = '⚠️';
    if (txt) txt.textContent = '无法读取 MinerU 状态（后端未就绪）：解析功能不可用，请稍后重试';
  }
}

function renderProviders() {
  const tbody = document.querySelector('#provider-table tbody');
  tbody.innerHTML = settingsState.providers.map((p, i) => {
    const envTag = p.env ? ` <em class="muted">(.env)</em>` : (p.id ? ' <em class="muted">(自定义·数据库)</em>' : '');
    const noKey = !p.api_key || p.api_key === '未设置';
    const incomplete = !p.base_url || !p.model;   // P5 点1：缺 Base URL/模型不可激活
    const activeBtn = p.id === settingsState.active
      ? '<span class="ok">已激活</span>'
      : (incomplete ? '<span class="muted">配置不完整</span>'
         : (noKey ? '<span class="muted">无 Key</span>'
            : `<button class="btn small" data-act="activate" data-i="${i}">激活</button>`));
    // T3：按供应商-模型组合的单价（来自 by_provider_model，未设置留空=用全局默认）
    const key = `${p.id}::${p.model}`;
    const pp = (settingsState.prices && settingsState.prices.by_provider_model || {})[key] || {};
    const fmt = v => (v === '' || v == null) ? '—' : '¥' + v;
    const priceShow = `输入 ${fmt(pp.input_per_m)} · 缓存 ${fmt(pp.cached_input_per_m)} · 输出 ${fmt(pp.output_per_m)}`;
    return `<tr>
      <td>${escapeHtml(p.name)}${envTag}${p.id === settingsState.active ? ' <b>★</b>' : ''}</td>
      <td title="${escapeHtml(p.base_url)}">${escapeHtml(shortTitle(p.base_url, 28))}</td>
      <td>${escapeHtml(p.model)}</td>
      <td class="provider-price-cell" title="单价：${escapeHtml(priceShow)}">${escapeHtml(priceShow)}</td>
      <td>${activeBtn}</td>
      <td><button class="btn small" data-act="edit" data-i="${i}">编辑</button>
          <button class="btn small danger" data-act="del" data-i="${i}" title="删除该供应商">删除</button></td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('button[data-act]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const i = Number(btn.dataset.i);
      const act = btn.dataset.act;
      // P1：激活/删除供应商是写操作（POST activate / 重写 .env）→ 防连点；编辑只开表单不动后端
      if (act === 'activate') guardBtn(btn, () => activateProvider(i), '激活中…');
      else if (act === 'del') guardBtn(btn, () => deleteProvider(i), '删除中…');
      else if (act === 'edit') editProvider(i);
    });
  });
}

async function deleteProvider(i) {
  const p = settingsState.providers[i];
  if (!p || p.id === settingsState.active) { alert('当前激活的供应商不能删除，请先激活其他供应商。'); return; }
  if (!(await askConfirm(`删除供应商「${p.name || p.id}」？其 .env/数据库记录将被移除。`))) return;
  settingsState.providers.splice(i, 1);
  saveProviders();
}

function editProvider(i) {
  const p = settingsState.providers[i];
  settingsState.editing = i;
  $('pf-name').value = p.name;
  $('pf-base').value = p.base_url;
  $('pf-model').value = p.model;
  // P：密钥回填——已设置显示掩码占位（非空串），未设置留空；回传明文仅在用户改动时
  $('pf-key').value = (p.api_key && p.api_key !== '未设置') ? KEY_MASK : '';
  $('pf-max').value = p.max_tokens ?? '';
  // N4：回填该供应商-模型单价（by_provider_model，无则空）
  const key = `${p.id}::${p.model}`;
  const pp = (settingsState.prices && settingsState.prices.by_provider_model || {})[key] || {};
  $('pf-price-in').value = pp.input_per_m ?? '';
  $('pf-price-cache').value = pp.cached_input_per_m ?? '';
  $('pf-price-out').value = pp.output_per_m ?? '';
  $('pf-test-result').textContent = '';
  $('provider-form').style.display = 'flex';
}

function _syncPriceLocal(providerId, model, vals) {
  // 保存后同步本地 prices（by_provider_model），使编辑重开/表格即时反映（否则用旧缓存仍空）。
  if (!settingsState.prices) return;
  const key = `${providerId}::${model}`;
  const bpm = settingsState.prices.by_provider_model =
    settingsState.prices.by_provider_model || {};
  if (vals.every(v => v === null)) delete bpm[key];   // 三项全空 → 删项回落默认
  else bpm[key] = { input_per_m: vals[0], cached_input_per_m: vals[1], output_per_m: vals[2] };
}

async function saveProviders() {
  // N4：模型信息存 .env；价格（input/cached/output）按 provider+model 写入 by_provider_model
  const formOpen = $('provider-form').style.display !== 'none';
  const adding = formOpen && settingsState.editing < 0;
  const pIn = $('pf-price-in').value, pCache = $('pf-price-cache').value, pOut = $('pf-price-out').value;
  const pMaxTxt = $('pf-max').value.trim();
  const pNum = Number(pMaxTxt);
  const pMax = (pMaxTxt === '' || !Number.isFinite(pNum) || pNum <= 0) ? undefined : pNum;
  const keyVal = $('pf-key').value.trim();
  let providers = settingsState.providers.map((p, i) => {
    if (formOpen && i === settingsState.editing) {
      // 密钥：仅用户改动（非占位）才回传明文；未改动沿用后端掩码串（后端识别后保留原 key）
      const keyPlaceholder = !keyVal || keyVal.startsWith('•') ||
        keyVal.includes('…') || keyVal.includes('*');
      return { ...p, name: $('pf-name').value, base_url: $('pf-base').value,
               model: $('pf-model').value, api_key: keyPlaceholder ? (p.api_key || '') : keyVal,
               max_tokens: pMax ?? p.max_tokens };
    }
    return p;
  });
  if (adding) {
    providers.push({ name: $('pf-name').value || '新供应商',
                     base_url: $('pf-base').value, model: $('pf-model').value,
                     api_key: keyVal, max_tokens: pMax });
  }
  if (adding && (!providers[providers.length - 1].base_url || !providers[providers.length - 1].model)) {
    alert('请填写 Base URL 与模型（新增供应商）'); return;
  }
  try {
    const saved = await api('/api/settings/providers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(providers),
    });
    const savedProviders = saved.providers || providers;
    settingsState.providers = savedProviders;
    settingsState.active = saved.active_provider;
    settingsState.editing = -1;
    // 保存该供应商-模型的单价（by_provider_model；三项全空=删项回落默认）
    if (formOpen) {
      const nm = $('pf-name').value, base = $('pf-base').value, mdl = $('pf-model').value;
      const target = savedProviders.find(x => (x.name === nm || x.base_url === base) && x.model === mdl)
        || savedProviders.find(x => x.base_url === base && x.model === mdl);
      if (target && target.id) {
        const vals = [pIn === '' ? null : Number(pIn),
                      pCache === '' ? null : Number(pCache),
                      pOut === '' ? null : Number(pOut)];
        await api('/api/settings/prices', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider_id: target.id, model: target.model,
                                 input_per_m: vals[0], cached_input_per_m: vals[1],
                                 output_per_m: vals[2] }),
        });
        _syncPriceLocal(target.id, target.model, vals);
      }
    }
    renderProviders();
    $('provider-form').style.display = 'none';
    alert(saved.llm_ready ? '✅ 模型已保存生效（写入 .env）' : '⚠ 未配置有效 Key，LLM 不可用');
  } catch (e) { alert('保存失败：' + e.message); }
}

async function activateProvider(i) {
  const p = settingsState.providers[i];
  try {
    await api(`/api/settings/providers/${encodeURIComponent(p.id)}/activate`, { method: 'POST' });
    settingsState.active = p.id;
    renderProviders();
  } catch (e) { alert('激活失败：' + e.message); }
}

async function testProvider() {
  const keyVal = $('pf-key').value.trim();
  // 掩码占位（编辑态回填 KEY_MASK）= 用户没改过密钥 → 交给后端按 id 取**已存的真实 Key** 测试。
  // （旧实现在这里直接拦住"请先填写真实 API Key"，已保存的 Key 明明可用 —— 用户反馈 bug。）
  const masked = !keyVal || keyVal.startsWith('•') ||
    keyVal.includes('…') || keyVal.includes('*');
  const editing = settingsState.editing >= 0
    ? settingsState.providers[settingsState.editing] : null;
  const hasStored = !!(editing && editing.api_key && editing.api_key !== '未设置');
  const body = { id: editing ? editing.id : '',
                 name: $('pf-name').value || 'test', base_url: $('pf-base').value,
                 model: $('pf-model').value, api_key: masked ? '' : keyVal };
  if (!body.base_url || !body.model) { $('pf-test-result').textContent = '请填写 Base URL 与模型'; return; }
  if (masked && !hasStored) { $('pf-test-result').textContent = '请先填写 API Key 再测试'; return; }
  $('pf-test-result').textContent = (masked && hasStored) ? '使用已保存的 Key 测试中…' : '测试中…';
  try {
    const r = await api('/api/settings/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    $('pf-test-result').textContent = `✅ 连接成功：${r.reply}`;
  } catch (e) { $('pf-test-result').textContent = '❌ ' + e.message; }
}

async function savePricingForProvider(btn, i) {
  // T3：保存单个供应商-模型组合的单价（三项全空 = 删除该项，回落全局默认）
  const p = settingsState.providers[i];
  if (!p) return;
  const tr = btn.closest('tr');
  const num = sel => {
    const v = (tr.querySelector(sel)?.value || '').trim();
    return v === '' ? null : Number(v);
  };
  const vals = [num('input[data-price="input"]'),
                num('input[data-price="cache"]'),
                num('input[data-price="out"]')];
  try {
    await api('/api/settings/prices', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider_id: p.id || '', model: p.model || '',
        input_per_m: vals[0], cached_input_per_m: vals[1], output_per_m: vals[2],
      }),
    });
    // 本地同步 by_provider_model，避免重开设置才生效
    _syncPriceLocal(p.id, p.model, vals);
    renderProviders();
    alert('✅ 已保存单价：' + (p.name || p.id) + ' / ' + (p.model || '(未填模型)'));
  } catch (e) { alert('保存失败：' + e.message); }
}

async function saveKbPath() {
  try {
    await api('/api/settings/kb-path', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: $('kb-path-input').value }),
    });
    alert('✅ 知识库路径已保存');
  } catch (e) { alert('保存失败：' + e.message); }
}

/* 2026-09-13 IA 重排：独立 saveMineru() 删除——MinerU Key/参数并入 saveParse() 一次提交
   （原 #mineru-save 与 #mineru-parser 一并删除，配置写 .env 单一来源，保存即生效）。 */
async function saveParse() {
  // 保存 PDF 解析设置：MinerU 主通道（Key+参数）+ PaddleOCR-VL 辅通道（凭据+选项）
  // + AI 仲裁 + 复核门控 + 批量间隔。返回 true=成功（供未保存标记清除），false=失败。
  const el = $('parse-result');
  try {
    const r = await api('/api/settings/parse', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: $('parse-mode').value,
        ai_review: $('parse-ai-review').checked,
        translate_gate: $('parse-gate').value,
        skip_review_batch: $('parse-skip-review').checked,
        parse_interval_sec: Number($('parse-interval').value || 8),
        // 契约：api_key 放在 mineru_params 内，字段名 mineru_api_key（后端 settings_service 同名字段）
        mineru_params: Object.assign({ mineru_api_key: $('mineru-key').value }, readMineruParams()),
        paddleocr: {
          access_token: $('po-token').value,
          base_url: $('po-base').value,
          model_version: $('po-model').value,
          options: readPaddleOptions(),
        },
      }),
    });
    // 回读校验（.env 落盘）优先：readback.ok === false 必须显式黄色告警，不得报成功。
    const rb = r.readback || {};
    if (rb.ok === false) {
      if (el) {
        el.classList.remove('ok');
        el.classList.add('warn');
        el.textContent = '⚠ 配置已提交但未落盘（.env 回读不一致：'
          + ((rb.mismatch || []).join('、') || '未知字段')
          + '）。请检查应用目录下的 .env 是否只读或被其他程序占用。';
      }
      return false;
    }
    const p = r.parse || {};
    // 以服务端回传为准重绘参数面板（容错：字段缺失时保留界面现值，不覆盖用户选择）
    if (p.mineru_params) applyMineruParams(p.mineru_params);
    if (p.paddleocr && p.paddleocr.options) applyPaddleOptions(p.paddleocr.options);
    if (el) {
      el.classList.remove('warn');
      el.classList.add('ok');
      el.textContent = '✅ 已保存（MinerU Key ' + ($('mineru-key').value ? '已设置' : '未设置')
        + ' · 语言 ' + ($('mineru-language').selectedOptions[0]?.textContent || '')
        + ' · mode=' + (p.mode || $('parse-mode').value)
        + '，AI 仲裁 ' + ((p.ai_review !== false) ? '开' : '关')
        + '，翻译时机 ' + ((p.translate_gate || 'wait') === 'wait' ? '等待复核' : '立即')
        + '，篇间隔 ' + (p.parse_interval_sec ?? $('parse-interval').value) + 's）';
    }
    loadMineruStatus();   // Key 改动后状态条即时刷新
    // P0-1 修复：**不再**在保存"解析设置"时顺带写 auto_exit（跨 tab 隐式写入全局开关
    // 是实测事故根因）。auto_exit 改由复选框自身的 change 事件显式保存，见 bindAutoExit。
    return true;
  } catch (e) {
    if (el) { el.classList.remove('ok'); el.classList.add('warn'); el.textContent = '❌ ' + e.message; }
    return false;
  }
}

/* ══════════ 知识库复制方式（T02）══════════ */
// 2026-09-12 批1：删除「纳入清单」勾选框（KB_INCLUDE_OPTIONS / renderKbInclude /
// kbIncludeChecked）以及「AI 检索文件清单」勾选框（renderRetrievalInclude）——
// 两组都是"存而不读"的装饰控件（后端 kb_include / retrieval_include 已同步删除）。
// kb 产物由数据布局契约固定：_note.md / _details.md / en.md / document.json / images/。

/* 2026-09-13：kb tab 主保存（复制方式 + 检索权限 + 编译思考档三个端点顺序提交，写回 #kb-result） */
async function saveKbSettings() {
  const el = $('kb-result');
  try {
    const r1 = await api('/api/settings/kb-rules', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ copy_mode: $('kb-copy-mode').value }),
    });
    const r2 = await api('/api/settings/retrieval', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: $('retrieval-mode').value }),
    });
    // 批3：编译/翻译思考档（auto=不干预/服务端自适应；其余显式下发）
    let r3 = { effort: 'auto' };
    if ($('compile-effort')) {
      r3 = await api('/api/settings/compile-effort', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ effort: $('compile-effort').value }),
      });
    }
    let r4 = { effort: 'auto' };
    if ($('translate-effort')) {
      r4 = await api('/api/settings/translate-effort', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ effort: $('translate-effort').value }),
      });
    }
    if (el) {
      el.classList.remove('warn');
      el.classList.add('ok');
      el.textContent = `✅ 已保存（${r1.copy_mode === 'link' ? '硬链接' : '复制副本'} · 检索范围 `
        + `${({ notes: '仅笔记', fragments: '片段检索', full: '全文阅读' })[r2.mode] || r2.mode}`
        + ` · 编译思考档 ${r3.effort === 'auto' ? '自动' : r3.effort}`
        + ` · 翻译思考档 ${r4.effort === 'auto' ? '自动' : r4.effort}）`;
    }
    return true;
  } catch (e) {
    if (el) { el.classList.remove('ok'); el.classList.add('warn'); el.textContent = '❌ ' + e.message; }
    return false;
  }
}

/* 2026-09-13：ui tab 主保存 = 模板 / 提示词 / CSS 三项顺序提交（任一失败即停并保留未保存标记） */
async function saveUiSettings() {
  const el = $('ui-result');
  const done = [];
  try {
    await api('/api/settings/md-template', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('disp-md-template').value }),
    });
    done.push('输出模板');
    await api('/api/settings/system-prompt', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('sys-prompt-input').value }),
    });
    done.push('系统提示词');
    await api('/api/settings/appearance', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('custom-css-input').value }),
    });
    injectCustomCss($('custom-css-input').value);
    done.push('自定义 CSS');
    if (el) {
      el.classList.remove('warn');
      el.classList.add('ok');
      el.textContent = '✅ 已保存并生效：' + done.join(' / ');
    }
    return true;
  } catch (e) {
    if (el) {
      el.classList.remove('ok');
      el.classList.add('warn');
      el.textContent = `⚠ 部分保存失败（已完成：${done.join('、') || '无'}）：` + e.message;
    }
    return false;
  }
}

/* ══════════ 输出模板（T06：全局默认 + 每篇覆盖）══════════ */
const MD_TEMPLATE_OPTS = [
  { v: 'obsidian_bilingual', l: 'Obsidian 双语（中文在上）' },
  { v: 'obsidian_bilingual_alt', l: 'EN / ZH 交替式' },
  { v: 'plain', l: '简洁版' },
];

async function saveMdTemplate() {
  // 2026-09-13 ui tab：独立「保存模板」按钮仍可用，同时清除本组「未保存」标记
  try {
    const r = await api('/api/settings/md-template', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('disp-md-template').value }),
    });
    markDirty('ui-dirty', false);
    const el = $('ui-result');
    if (el) { el.classList.remove('warn'); el.classList.add('ok'); el.textContent = `✅ 默认输出模板已设为：${r.template}`; }
    return true;
  } catch (e) {
    const el = $('ui-result');
    if (el) { el.classList.remove('ok'); el.classList.add('warn'); el.textContent = '❌ ' + e.message; }
    return false;
  }
}

function paperTemplateSelect(p) {
  if (!(p.status === 'translated' || p.status === 'parsed')) return '';
  const cur = p.md_template || '';
  return `<select class="p-template" data-id="${p.id}" title="输出模板：切换后本地重渲染（不耗 token）">` +
    MD_TEMPLATE_OPTS.map(o =>
      `<option value="${o.v}" ${cur === o.v ? 'selected' : ''}>${o.l}</option>`).join('') +
    `</select>`;
}

async function setPaperTemplate(id, tpl) {
  try {
    const r = await api(`/api/papers/${id}/template`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template: tpl }),
    });
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'task',
                  message: `论文[${id}] 模板已设为 ${r.template}` + (r.rerendered ? '（已重渲染）' : '') });
    if (readerState.paperId === id && readerState.dir && readerState.file === 'paper.md') {
      loadReaderFile(readerState.dir, readerState.file);
    }
  } catch (e) { alert('切换模板失败：' + e.message); }
}

/* ══════════ G15：阅读面板管理（桌面停靠 + 浮动弹出，统一窗口系统）══════════ */
const FLOAT_MAX = 4;   // 阅读面板总数上限 4（+默认阅读器 = 5，P2 审计 1.5）
const floatWins = [];  // 统一管理（docked=桌面面板 / float=浮动窗）
let zTop = 100;

function readerWinHTML() {
  return `
    <div class="float-head">
      <select class="fw-paper" title="论文"></select>
      <span class="fw-src">
        <button class="src-btn active" data-src="kb">知识库</button>
        <button class="src-btn" data-src="lib">解析库</button>
      </span>
      <span class="fw-title">阅读</span>
      <span class="fw-btns">
        <button class="fw-dock" title="弹出为浮动窗口">⧉ 弹出</button>
        <button class="fw-min" title="最小化">—</button>
        <button class="fw-close" title="关闭">✕</button>
      </span>
    </div>
    <div class="fw-tabs"></div>
    <div class="fw-body"><div class="empty">选择论文后加载</div></div>`;
}

function openReaderWindow(force) {
  if (floatWins.length >= FLOAT_MAX && !force) { alert('最多 5 个阅读区（含默认阅读器）'); return; }
  const id = 'fw-' + Date.now();
  const el = document.createElement('div');
  el.className = 'desk-panel desk-reader';   // G15：新建阅读窗默认贴合桌面（并排）
  el.id = id;                                // P2-9 修复：必须设 id（isReaderWin 依赖它）
  el.dataset.id = id;
  el.innerHTML = readerWinHTML();
  document.querySelector('#desk').appendChild(el);
  const win = { id, el, paperId: null, source: 'kb', file: null, kbDir: null, min: false, docked: true };
  floatWins.push(win);
  bindReaderWin(win);
  loadFloatContent(win);
  persistFloats();
  resetReaderWeights();  // P2-13：新窗加入 → 阅读器均分（前面窗口自动缩小）
  applyDeskWeights();    // 右缘恒贴右
}

/* ══════════ P2-13：桌面宽度统一约束（底层机制）══════════
   所有可见停靠面板的宽度由 flex-grow **权重**决定，任何变更（拖动/新建/关闭/
   弹出放回/阅读模式/窗口缩放）都只调权重再走 applyDeskWeights() 重算——
   权重总和恒定 → 面板宽度总和恒 = 桌面宽 → 右缘永远贴齐桌面右侧，
   从根上杜绝"拖宽飞出/右侧空白"，无需在每处事件点单独打补丁。
   语义：对话区 1 权重单位（默认 50%）；所有阅读器合计 1 单位、按各自权重均分。 */
function applyDeskWeights() {
  const desk = $('desk');
  const chat = $('chat-panel');
  if (!desk || !chat) return;
  const visible = [...desk.children].filter(e => e.style.display !== 'none');
  if (!visible.length) return;
  const chatVisible = chat.style.display !== 'none';
  const readers = visible.filter(e => e !== chat);
  // 对话权重：默认 1（50%）；阅读模式 0.25（20%）
  const chatGrow = chatVisible ? (parseFloat(chat.dataset.grow) || 1) : 0;
  // 阅读器权重归一化：合计恒为 1 单位
  const rSum = readers.reduce((s, r) => s + (parseFloat(r.dataset.grow) || 0), 0);
  const rBase = rSum > 0 ? rSum : Math.max(1, readers.length);
  const total = chatGrow + 1;
  visible.forEach(e => {
    if (e === chat) {
      e.style.setProperty('flex', `${chatGrow} 1 0`, 'important');
    } else {
      const g = (parseFloat(e.dataset.grow) || 0) / rBase;
      e.style.setProperty('flex', `${g} 1 0`, 'important');
    }
  });
}

/* 阅读器权重重置为均分（新建/关闭/放回后调用）：可见阅读器各占 1/n */
function resetReaderWeights() {
  const desk = $('desk');
  const chat = $('chat-panel');
  if (!desk || !chat) return;
  const readers = [...desk.children].filter(e =>
    e !== chat && e.style.display !== 'none' &&
    (e.classList.contains('reader-panel') || e.classList.contains('desk-reader')));
  if (readers.length) {
    const w = 1 / readers.length;
    readers.forEach(r => { r.dataset.grow = String(w); });
  }
}

function bindReaderWin(win) {
  const el = win.el;
  const sel = el.querySelector('.fw-paper');
  // E3：下拉列出"已见页"论文（最新在前；5000 篇不可能全量列出，随翻页扩充）
  const seenPapers = Object.values(state.paperById).sort((a, b) => b.id - a.id);
  sel.innerHTML = `<option value="">— 请选择论文 —</option>` + seenPapers.map(p =>
    `<option value="${p.id}">${escapeHtml(shortTitle(p.filename || p.title, 26))}</option>`).join('');
  if (win.paperId) sel.value = String(win.paperId);
  win.paperId = sel.value ? Number(sel.value) : null;
  sel.addEventListener('change', () => { win.paperId = Number(sel.value); win.file = null; win.kbDir = null; loadFloatContent(win); persistFloats(); });

  el.querySelectorAll('.fw-src .src-btn').forEach(b => b.addEventListener('click', () => {
    win.source = b.dataset.src;
    el.querySelectorAll('.fw-src .src-btn').forEach(x => x.classList.toggle('active', x === b));
    win.file = null; win.kbDir = null;
    loadFloatContent(win);
    persistFloats();
  }));
  el.querySelector('.fw-close').addEventListener('click', () => closeFloatWin(win));
  el.querySelector('.fw-min').addEventListener('click', () => toggleFloatMin(win));
  el.querySelector('.fw-dock').addEventListener('click', () => {
    if (win.docked) popReaderPanel(win); else dockFloatToDesk(win);
  });
  if (win.docked) bindDeskResize(win);
}

/* 桌面面板 → 浮动窗（P2-5：统一外壳，仅切 class + 迁父节点，宽度记忆） */
function popReaderPanel(win) {
  const el = win.el;
  el.classList.remove('desk-panel', 'desk-reader');
  el.classList.add('float-win');
  win.docked = false;
  try { localStorage.setItem('panel-w-' + win.id, String(Math.round(el.getBoundingClientRect().width))); } catch (e) { /* 忽略 */ }
  const btn = el.querySelector('.fw-dock') || $('reader-pop');
  if (btn) { btn.textContent = '⤓ 放回'; btn.title = '放回桌面贴合'; }
  el.style.left = '80px'; el.style.top = '50px';
  el.style.width = '560px'; el.style.height = '480px';
  document.body.appendChild(el);
  bindFloatDrag(el);
  bindFloatResize(el);
  bringToFront(el);
  persistFloats();
  resetReaderWeights();  // P2-13：弹出后剩余阅读器重新均分贴右
  applyDeskWeights();
}

/* 浮动窗 → 桌面面板（贴合并排；清除浮窗定位，恢复停靠宽度 → 不错位） */
function dockFloatToDesk(win) {
  const el = win.el;
  el.classList.remove('float-win');
  el.classList.add('desk-panel', 'desk-reader');
  win.docked = true;
  const btn = el.querySelector('.fw-dock') || $('reader-pop');
  if (btn) { btn.textContent = '⧉ 弹出'; btn.title = '弹出为浮动窗口'; }
  el.style.left = ''; el.style.top = ''; el.style.width = ''; el.style.height = '';
  document.querySelector('#desk').appendChild(el);
  bindDeskResize(win);
  persistFloats();
  resetReaderWeights();  // P2-13：放回后阅读器均分贴右（权重模型接管宽度）
  applyDeskWeights();
}

/* 桌面面板：右缘拖拽调宽（对话/阅读通用，宽度持久化） */
function bindDeskResize(win) {
  const el = win.el;
  let rz = el.querySelector('.desk-rz');
  if (!rz) {
    rz = document.createElement('div');
    rz.className = 'desk-rz';
    rz.title = '拖拽调整宽度';
    el.appendChild(rz);
  }
  rz.onmousedown = (e) => {
    e.preventDefault(); e.stopPropagation();
    const startX = e.clientX;
    const startW = el.getBoundingClientRect().width;
    const move = (ev) => {
      // P2-13 权重模型：拖宽上限 = 桌面宽 - 其他面板最小宽之和（右缘永不飞出）；
      // 拖动只改本面板权重，其他面板由 flex 自动让位（总和恒 = 桌面宽）
      const deskEl = document.querySelector('#desk');
      const deskW = deskEl ? deskEl.getBoundingClientRect().width : window.innerWidth;
      let othersMin = 0;
      if (deskEl) {
        [...deskEl.children].forEach(o => {
          if (o === el || o.style.display === 'none') return;
          const mw = parseFloat(getComputedStyle(o).minWidth) || 0;
          othersMin += mw;
        });
      }
      const maxW = Math.max(240, deskW - othersMin - 4);
      const nw = Math.max(240, Math.min(maxW, startW + (ev.clientX - startX)));
      const chat = $('chat-panel');
      const chatGrow = (chat && chat.style.display !== 'none')
        ? (parseFloat(chat.dataset.grow) || 1) : 0;
      const total = chatGrow + 1;  // 总权重单位
      const newUnit = Math.max(0.05, (nw / deskW) * total);
      el.dataset.grow = String(newUnit);
      applyDeskWeights();
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      try { localStorage.setItem('panel-w-' + win.id, String(Math.round(el.getBoundingClientRect().width))); } catch (e) { /* 忽略 */ }
      persistFloats();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  };
}

function closeFloatWin(win) {
  win.el.remove();
  const i = floatWins.indexOf(win);
  if (i >= 0) floatWins.splice(i, 1);
  persistFloats();
  resetReaderWeights();  // P2-13：关闭后剩余窗口重新均分贴右
  applyDeskWeights();
}

function toggleFloatMin(win) {
  win.min = !win.min;
  win.el.classList.toggle('min', win.min);
  persistFloats();
}

function bringToFront(el) {
  el.style.zIndex = ++zTop;
}

function bindFloatDrag(el) {
  const head = el.querySelector('.float-head');
  head.addEventListener('mousedown', (e) => {
    if (e.target.closest('select, button')) return;
    bringToFront(el);
    const startX = e.clientX, startY = e.clientY;
    const l = el.offsetLeft, t = el.offsetTop;
    const move = (ev) => {
      // P2-5：win10 式边缘吸附——贴近视口左/右缘 12px 内自动贴边
      let x = l + ev.clientX - startX;
      const w = el.offsetWidth;
      if (x < 12) x = 0;
      else if (x + w > window.innerWidth - 12) x = window.innerWidth - w;
      el.style.left = Math.max(0, Math.min(window.innerWidth - 60, x)) + 'px';
      el.style.top = Math.max(0, Math.min(window.innerHeight - 40, t + ev.clientY - startY)) + 'px';
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      // G15 磁吸停靠：阅读窗拖回桌面区域 → 贴合并排；对话窗不放回（走按钮）
      if (el.id === 'chat-float') { persistFloats(); return; }
      const rect = el.getBoundingClientRect();
      const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
      const deskEl = document.querySelector('#desk');
      const dr = deskEl ? deskEl.getBoundingClientRect() : null;
      if (dr && cx >= dr.left && cx <= dr.right && cy >= dr.top && cy <= dr.bottom) {
        const win = floatWins.find(w => w.el === el);
        if (win) { dockFloatToDesk(win); return; }
      }
      persistFloats();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
    e.preventDefault();
  });
}

/* G8：四边/四角缩放柄（注入 .rz-n/.rz-s/.rz-e/.rz-w/.rz-ne/.rz-nw/.rz-se/.rz-sw） */
const RZ_DIRS = {
  'rz-n': ['t'], 'rz-s': ['b'], 'rz-e': ['r'], 'rz-w': ['l'],
  'rz-ne': ['r', 't'], 'rz-nw': ['l', 't'], 'rz-se': ['r', 'b'], 'rz-sw': ['l', 'b'],
};
const RZ_MIN_W = 220, RZ_MIN_H = 140;

function bindFloatResize(el) {
  if (!el.querySelector('.rz')) {
    Object.keys(RZ_DIRS).forEach(p => {
      const d = document.createElement('div');
      d.className = 'rz ' + p;
      el.appendChild(d);
    });
  }
  el.querySelectorAll('.rz').forEach(h => {
    h.addEventListener('mousedown', (e) => {
      e.preventDefault(); e.stopPropagation();
      bringToFront(el);
      const dir = RZ_DIRS[h.className.split(' ')[1]] || ['r', 'b'];
      const sx = e.clientX, sy = e.clientY;
      const rect = el.getBoundingClientRect();
      const start = { l: rect.left, t: rect.top, w: rect.width, h: rect.height };
      const move = (ev) => {
        const dx = ev.clientX - sx, dy = ev.clientY - sy;
        let { l, t, w, h } = start;
        if (dir.includes('r')) w = Math.max(RZ_MIN_W, start.w + dx);
        if (dir.includes('b')) h = Math.max(RZ_MIN_H, start.h + dy);
        if (dir.includes('l')) { const nw = Math.max(RZ_MIN_W, start.w - dx); l = start.l + (start.w - nw); w = nw; }
        if (dir.includes('t')) { const nh = Math.max(RZ_MIN_H, start.h - dy); t = start.t + (start.h - nh); h = nh; }
        el.style.left = l + 'px'; el.style.top = t + 'px';
        el.style.width = w + 'px'; el.style.height = h + 'px';
      };
      const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); persistFloats(); };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
  });
}

async function loadFloatContent(win) {
  const tabs = win.el.querySelector('.fw-tabs');
  const body = win.el.querySelector('.fw-body');
  if (!win.paperId) { tabs.style.display = 'none'; body.innerHTML = '<div class="empty">请选择论文</div>'; return; }
  if (win.source === 'lib') { await loadFloatLib(win, tabs, body); return; }
  try {
    const r = await api(`/api/kb/paper/${win.paperId}/folder`);
    if (!r.doi_dir) {
      tabs.style.display = 'none';
      body.innerHTML = '<div class="empty">知识库未生成，可切「解析库」浏览原文</div>';
      return;
    }
    win.kbDir = r.doi_dir;
    const tree = await api('/api/kb/tree');
    const folder = (tree.folders || []).find(f => f.doi_dir === r.doi_dir);
    const files = (folder && folder.files) || [];
    tabs.style.display = 'flex';
    tabs.innerHTML = KB_FILES.map(k => {
      if (k.name === '__images__') {
        return `<button class="kb-tab ${win.file === '__images__' ? 'active' : ''}" data-wf="__images__">${k.label}</button>`;
      }
      const exists = files.some(f => f.name === k.name);
      return exists
        ? `<button class="kb-tab ${win.file === k.name ? 'active' : ''}" data-wf="${k.name}">${k.label}</button>`
        : '';
    }).join('');
    tabs.querySelectorAll('button[data-wf]').forEach(b => b.addEventListener('click', () => { win.file = b.dataset.wf; loadFloatKbFile(win); persistFloats(); }));
    win.file = (win.file === '__images__' || (win.file && files.some(f => f.name === win.file))) ? win.file : '_note.md';
    loadFloatKbFile(win);
  } catch (e) { body.innerHTML = '<div class="empty">加载失败：' + escapeHtml(e.message) + '</div>'; }
}

async function loadFloatKbFile(win) {
  const body = win.el.querySelector('.fw-body');
  body.innerHTML = '<div class="empty">加载中…</div>';
  try {
    if (win.file === '__images__') {
      const r = await api(`/api/kb/images?dir=${encodeURIComponent(win.kbDir)}`);
      body.innerHTML = r.images.length
        ? `<div class="img-grid">` + r.images.map(im =>
            `<figure class="img-cell"><img src="/api/kb/image?path=${encodeURIComponent(win.kbDir + '/images/' + im.name)}" alt="" loading="lazy"><figcaption>${escapeHtml(im.name)}</figcaption></figure>`).join('') + `</div>`
        : '<div class="empty">无图片</div>';
      bindLightbox();
      return;
    }
    if (win.file === 'source.pdf') {
      const p = getPaper(win.paperId);
      if (!p || !p.doi) { body.innerHTML = '<div class="empty">未找到该文献 DOI，无法读 PDF</div>'; return; }
      renderPdfViewer(body, p.doi);
      return;
    }
    const r = await api(`/api/kb/file?path=${encodeURIComponent(win.kbDir + '/' + win.file)}`);
    body.innerHTML = renderMarkdown(r.content, win.kbDir, 'kb');
  } catch (e) {
    body.innerHTML = `<div class="empty">${escapeHtml(win.file)} 不可用：${escapeHtml(e.message)}</div>`;
  }
}

async function loadFloatLib(win, tabs, body) {
  try {
    const r = await api(`/api/papers/${win.paperId}/files`);
    const files = r.files || [];
    if (!files.length) { tabs.style.display = 'none'; body.innerHTML = '<div class="empty">解析库为空</div>'; return; }
    // G6：顶层文件 + 图片集中标签
    const top = files.filter(f => f.path.split('/').length === 2);
    const images = files.filter(f => isLibImage(f.path));
    if (!top.length) {
      if (images.length) {
        tabs.style.display = 'flex';
        tabs.innerHTML = `<button class="kb-tab ${win.file === '__images__' ? 'active' : ''}" data-wf="__images__">🖼 图片 (${images.length})</button>`;
        tabs.querySelector('button[data-wf]').addEventListener('click', () => { win.file = '__images__'; loadFloatLibFile(win); persistFloats(); });
        win.file = '__images__';
        loadFloatLibFile(win);
        return;
      }
      tabs.style.display = 'none';
      body.innerHTML = '<div class="empty">解析库为空</div>';
      return;
    }
    const entries = [...top];
    if (images.length) entries.push({ path: '__images__', name: '图片' });
    tabs.style.display = 'flex';
    tabs.innerHTML = entries.map(f => {
      const isImgTab = f.path === '__images__';
      return `<button class="kb-tab ${f.path === win.file ? 'active' : ''}" data-wf="${isImgTab ? '__images__' : escapeHtml(f.path)}" title="${isImgTab ? '' : escapeHtml(f.path)}">${isImgTab ? '🖼 图片 (' + images.length + ')' : escapeHtml(shortTitle(f.path.split('/')[1] || f.name, 16))}</button>`;
    }).join('');
    tabs.querySelectorAll('button[data-wf]').forEach(b => b.addEventListener('click', () => { win.file = b.dataset.wf; loadFloatLibFile(win); persistFloats(); }));
    const first = top.find(f => /\.md$/i.test(f.path)) || top[0];
    win.file = (win.file === '__images__' || (win.file && top.some(f => f.path === win.file))) ? win.file : first.path;
    loadFloatLibFile(win);
  } catch (e) { body.innerHTML = '<div class="empty">加载失败：' + escapeHtml(e.message) + '</div>'; }
}

async function loadFloatLibFile(win) {
  const body = win.el.querySelector('.fw-body');
  const path = win.file;
  body.innerHTML = '<div class="empty">加载中…</div>';
  if (path === '__images__') {
    try {
      const r = await api(`/api/papers/${win.paperId}/files`);
      const imgs = (r.files || []).filter(f => isLibImage(f.path));
      body.innerHTML = imgs.length
        ? `<div class="img-grid">` + imgs.map(im =>
            `<figure class="img-cell"><img src="/api/library/image?path=${encodeURIComponent(im.path)}" alt="" loading="lazy"><figcaption>${escapeHtml(im.name)}</figcaption></figure>`).join('') + `</div>`
        : '<div class="empty">该文献没有图片</div>';
      bindLightbox();
      return;
    } catch (e) { body.innerHTML = '<div class="empty">加载失败：' + escapeHtml(e.message) + '</div>'; return; }
  }
  const isImg = /\.(png|jpe?g|gif|webp|svg)$/i.test(path);
  try {
    if (isImg) {
      body.innerHTML = `<div class="img-grid"><figure class="img-cell"><img src="/api/library/image?path=${encodeURIComponent(path)}" alt="" loading="lazy"><figcaption>${escapeHtml(path.split('/').pop())}</figcaption></figure></div>`;
      bindLightbox();
      return;
    }
    const r = await api(`/api/library/file?path=${encodeURIComponent(path)}`);
    body.innerHTML = renderMarkdown(r.content, path.split('/')[0], 'lib');
  } catch (e) {
    body.innerHTML = `<div class="empty">${escapeHtml(path)} 不可用：${escapeHtml(e.message)}</div>`;
  }
}

function persistFloats() {
  // P2-5：默认阅读器（#reader）始终存在，不持久化（避免刷新后重复创建）
  const data = floatWins.filter(w => w.el.id !== 'reader').map(w => {
    const r = w.el.getBoundingClientRect();
    return { paperId: w.paperId, source: w.source, file: w.file, min: w.min, docked: !!w.docked,
             x: r.left, y: r.top, w: r.width, h: r.height };
  });
  localStorage.setItem('reader-floats-v1', JSON.stringify(data));
}

function restoreFloats() {
  let data = [];
  try { data = JSON.parse(localStorage.getItem('reader-floats-v1') || '[]'); } catch (e) { data = []; }
  data.forEach(d => {
    if (floatWins.length >= FLOAT_MAX) return;
    openReaderWindow();
    const win = floatWins[floatWins.length - 1];
    if (d.paperId && getPaper(d.paperId)) {
      win.paperId = d.paperId;
      win.el.querySelector('.fw-paper').value = String(d.paperId);
    }
    win.source = (d.source === 'lib') ? 'lib' : 'kb';
    win.el.querySelectorAll('.fw-src .src-btn').forEach(b => b.classList.toggle('active', b.dataset.src === win.source));
    win.file = d.file || null;
    if (d.min) toggleFloatMin(win);
    if (d.docked === false) { popReaderPanel(win); if (d.x) { win.el.style.left = d.x + 'px'; win.el.style.top = d.y + 'px'; win.el.style.width = (d.w || 560) + 'px'; win.el.style.height = (d.h || 480) + 'px'; } }
    loadFloatContent(win);
  });
}

/* ══════════ G15：对话面板 弹出（紧凑悬浮问答助手）/ 放回 ══════════ */
let chatFloated = false;

function popChat() {
  if (chatFloated) return;
  const main = $('main');
  const w = document.createElement('div');
  w.id = 'chat-float';
  w.className = 'float-win chat-float';
  w.innerHTML = `<div class="float-head"><span class="fw-title">💬 问答助手</span><span class="fw-btns"><button class="fw-min" title="最小化">—</button><button class="fw-dockchat" title="放回桌面">⤓ 放回</button></span></div>`;
  document.body.appendChild(w);
  w.querySelector('.float-head').insertAdjacentElement('afterend', main); // 移动 #main（事件绑定随元素迁移）
  w.querySelector('.fw-min').addEventListener('click', () => w.classList.toggle('min'));
  w.querySelector('.fw-dockchat').addEventListener('click', () => dockChat());
  // P2-5：弹出后桌面彻底不占位（隐藏对话面板，阅读区获得全部空间）
  $('chat-panel').style.display = 'none';
  applyDeskWeights();  // P2-13：对话隐藏 → 阅读器自动铺满贴右
  w.style.left = '110px'; w.style.top = '70px';  w.style.width = '460px'; w.style.height = '520px';
  bindFloatDrag(w);
  bindFloatResize(w);
  bringToFront(w);
  $('chat-pop').textContent = '⤓ 放回';
  chatFloated = true;
}

function dockChat() {
  const w = $('chat-float');
  if (!w) return;
  const main = $('main');
  $('chat-panel').appendChild(main); // 放回桌面对话面板
  $('chat-panel').style.display = ''; // 恢复占位
  applyDeskWeights();  // P2-13：对话恢复 → 阅读器自动让出宽度
  w.remove();
  $('chat-pop').textContent = '⧉ 弹出';
  chatFloated = false;
}

/* ══════════ AI 检索分级（T05）══════════ */
// 批1：删除 renderRetrievalInclude（清单勾选无消费点，后端已删）；只保留真正生效的检索范围。
async function saveRetrieval() {
  try {
    const r = await api('/api/settings/retrieval', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: $('retrieval-mode').value }),
    });
    $('retrieval-result').textContent = `✅ 已保存（模式：${r.mode}）`;
    setTimeout(() => { $('retrieval-result').textContent = ''; }, 3000);
  } catch (e) { alert('保存失败：' + e.message); }
}

/* ══════════ 显示设置（U8/V11：字号/行宽/字体，localStorage 记忆）══════════ */
const FONT_FAMILIES = {
  system: 'system-ui, "Segoe UI", "Microsoft YaHei", sans-serif',
  serif: 'Georgia, "Songti SC", "SimSun", serif',
  sans: '"Helvetica Neue", "PingFang SC", "Microsoft YaHei", sans-serif',
  mono: 'ui-monospace, Consolas, "Courier New", monospace',
};

/* 显示默认值（**单一来源**，2026-09-12 用户拍板）：会话区 15px / 阅读区 16px /
   页边距「窄」24px / 字体「系统默认」。
   注意 `index.html` 下拉里的文案（15px=「特大」、16px=「超大」）只是标签，实际生效值一律取这里；
   旧实现 `localStorage || '13'` 与下拉默认显示第一项(12px)**不一致**（界面显示 12px、实际 13px）。 */
const DISPLAY_DEFAULTS = {
  'disp-chat': '15', 'disp-reader': '16', 'disp-margin': '24', 'disp-font': 'system',
};

function displayValue(id) {
  return localStorage.getItem(id) || DISPLAY_DEFAULTS[id];
}

function applyDisplaySettings() {
  const root = document.documentElement;
  root.style.setProperty('--chat-font', displayValue('disp-chat') + 'px');
  root.style.setProperty('--reader-font', displayValue('disp-reader') + 'px');
  // P5-FB-点6：阅读区行宽 → 页边距（行宽随阅读器窗口拖拽自适应，页边距=左右留白）
  root.style.setProperty('--reader-margin', displayValue('disp-margin') + 'px');
  const font = displayValue('disp-font');
  root.style.setProperty('--font-body', FONT_FAMILIES[font] || FONT_FAMILIES.system);
  root.style.setProperty('--font-reader', FONT_FAMILIES[font] || FONT_FAMILIES.system);
}

function bindDisplaySettings() {
  ['disp-chat', 'disp-reader', 'disp-margin', 'disp-font'].forEach(id => {
    const el = $(id);
    el.value = displayValue(id);      // 下拉显示值 == 实际生效值（含新默认，修掉显示/生效不一致）
    el.addEventListener('change', () => {
      localStorage.setItem(id, el.value);
      applyDisplaySettings();
    });
  });
}

/* ══════════ 外观 CSS + 系统提示词（V11）══════════ */
function injectCustomCss(css) {
  let el = $('custom-css');
  if (!el) {
    el = document.createElement('style');
    el.id = 'custom-css';
    document.head.appendChild(el);
  }
  el.textContent = css || '';
}

async function loadAppearanceSettings() {
  try {
    const s = await api('/api/settings');
    $('sys-prompt-input').value = s.system_prompt_extra || '';
    $('custom-css-input').value = s.custom_css || '';
    injectCustomCss(s.custom_css || '');
    // 思考强度回填（2026-09-12：提问框旁的设置项，用户此前完全无法设置）
    if ($('chat-effort')) $('chat-effort').value = s.chat_reasoning_effort || 'auto';
  } catch (e) { /* 服务未就绪 */ }
}

/* P2-7 / 2026-09-12 发布轮：版本统一——关于页从 /api/version 拉**全组件版本**：
   前端资源（window.PAPERAGENT_FRONTEND_VERSION，随包自报）/ 后端 / 算法包 paperparse /
   知识库库 paperkb / 数据格式 data_format；前后端不一致时显式报警（发布事故防线）。 */
async function loadVersionInfo() {
  const set = (id, text) => { const el = $(id); if (el) el.textContent = text; };
  const fe = window.PAPERAGENT_FRONTEND_VERSION || '?';
  set('about-frontend', 'v' + fe);
  // 运行地址动态取当前页面（批1：原来硬编码 127.0.0.1:8900，换端口/局域网访问时误导用户）
  set('about-host', window.location.host || '—');
  try {
    const h = await api('/api/version');
    set('about-version', h.app ? 'v' + h.app : '—');
    set('about-engine', h.paperparse ? `paper_reader v${h.paperparse}` : '—');
    set('about-kb', h.paperkb ? `paperkb v${h.paperkb}` : '—');
    set('about-dataformat', `data_format=${h.data_format} · layout ${h.layout}`);
    const badge = $('about-mismatch');
    if (badge) {
      const bad = !!h.frontend && h.frontend !== fe;
      badge.style.display = bad ? '' : 'none';
      if (bad) badge.textContent =
        `⚠ 版本不一致：界面 v${fe} / 后端 v${h.app} —— 请重新复制**完整**的新版本（不要只替换部分文件）`;
    }
  } catch (e) {
    const ev = $('about-engine');
    if (ev && ev.textContent === '…') ev.textContent = '—';   // 服务未就绪
  }
}

function bindAppearance() {
  $('sys-prompt-save').addEventListener('click', (e) => guardBtn(e.currentTarget, async () => {
    try {
      await api('/api/settings/system-prompt', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: $('sys-prompt-input').value }),
      });
      markDirty('ui-dirty', false);   // 2026-09-13：单卡片保存也清除本组未保存标记
      $('sys-prompt-result').textContent = '✅ 已保存（对新的提问生效）';
      setTimeout(() => { $('sys-prompt-result').textContent = ''; }, 2500);
    } catch (e) { $('sys-prompt-result').textContent = '❌ ' + e.message; }
  }, '保存中…'));
  $('custom-css-save').addEventListener('click', (e) => guardBtn(e.currentTarget, async () => {
    try {
      await api('/api/settings/appearance', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: $('custom-css-input').value }),
      });
      injectCustomCss($('custom-css-input').value);
      markDirty('ui-dirty', false);   // 2026-09-13：同上
      $('custom-css-result').textContent = '✅ 已保存并生效';
      setTimeout(() => { $('custom-css-result').textContent = ''; }, 2500);
    } catch (e) { $('custom-css-result').textContent = '❌ ' + e.message; }
  }, '保存中…'));
}

/* ══════════ 设置中心 ══════════ */
async function installPlugin() {
  const input = $('plugin-zip-input');
  const file = input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await api('/api/plugins/install', { method: 'POST', body: fd });
    alert(`✅ Skill「${r.plugin.name}」v${r.plugin.version} 已安装并启用`);
    appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'plugin',
                  message: `Skill 已安装: ${r.plugin.name} v${r.plugin.version}` });
    input.value = '';
    await loadPlugins();
  } catch (e) { alert('安装失败：' + e.message); input.value = ''; }
}

async function loadPlugins() {
  const box = $('plugin-list');
  try {
    const r = await api('/api/plugins');
    box.innerHTML = r.plugins.map(p => `
      <div class="plugin-item">
        <div>
          <b>${escapeHtml(p.name)}</b> <span class="muted">v${escapeHtml(p.version)}</span>
          <div class="muted">${escapeHtml(p.description)}</div>
        </div>
        <label class="switch">
          <input type="checkbox" data-pid="${escapeHtml(p.id)}" ${p.enabled ? 'checked' : ''}>
          <span class="slider"></span>
        </label>
      </div>`).join('');
    box.querySelectorAll('input[data-pid]').forEach(cb => {
      cb.addEventListener('change', async () => {
        try {
          await api(`/api/plugins/${encodeURIComponent(cb.dataset.pid)}/toggle`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: cb.checked }),
          });
        } catch (e) { alert('切换失败：' + e.message); cb.checked = !cb.checked; }
      });
    });
  } catch (e) { box.innerHTML = '加载失败：' + escapeHtml(e.message); }
}

function bindLightbox() {
  document.addEventListener('click', (e) => {
    const img = e.target.closest('.md img, .img-cell img');
    if (!img) return;
    openLightbox(img);
  });
  // G9：ESC 关闭 lightbox
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const lb = document.querySelector('.lightbox');
      if (lb) lb.remove();
    }
  });
}

/* G9：图片放大查看：滚轮缩放、点击任意处/ESC 关闭（z-index 置顶） */
function openLightbox(img) {
  const lb = document.createElement('div');
  lb.className = 'lightbox';
  const im = document.createElement('img');
  im.src = img.getAttribute('src') || '';
  im.alt = '';
  lb.appendChild(im);
  document.body.appendChild(lb);
  let scale = 1;
  lb.addEventListener('wheel', (e) => {
    e.preventDefault();
    scale = Math.max(0.4, Math.min(6, scale + (e.deltaY < 0 ? 0.2 : -0.2)));
    im.style.transform = `scale(${scale})`;
  }, { passive: false });
  lb.addEventListener('click', () => lb.remove());
}

/* ══════════ 启动 ══════════ */
async function boot() {
  // P12F 迁移：启动恒单阅读区——清除旧版阅读模式残留与持久化浮动窗
  // （旧会话点过阅读模式/浮出过阅读窗会在启动时自动重建多阅读区；手动切换仅当次会话生效）
  localStorage.removeItem('read-mode');
  localStorage.removeItem('reader-floats-v1');
  applyDisplaySettings();
  loadAppearanceSettings(); // 注入自定义 CSS + 提示词（V11）
  loadVersionInfo();        // P2-7：关于页版本号
  bindCollapse();
  bindReadModes();
  bindDrag();
  bindHeightDrag();
  bindNewSession();
  bindImport();       // P5-E2：统一导入向导（PDF/md/bib/附件 四 tab，替代旧 bindUpload）
  bindAttachments();  // P0-B step3（F3）：附件清单模态（文献卡「📎 附件 N」）
  bindConnectivity(); // P1：离线/后端探活提示条
  bindChat();
  bindEventPanel();
  bindSettings();
  bindKbAdmin();
  bindLitAdmin();
  bindReview();
  initReviewGate();  // P12F：复核门控汇总条
  bindLightbox();
  bindTabManage();
  bindKbSearch(); // G9/P5-E1：知识库搜索/过滤 + 列表/目录切换
  bindSessionBatch(); // P5-E1：会话批量管理（全选/批量删除/清空已归档）
  $('kbd-close').addEventListener('click', () => { $('kb-detail-modal').style.display = 'none'; });
  $('float-win-btn').addEventListener('click', openReaderWindow);
  // P2-5：默认阅读器纳入统一面板管理（与新建窗同构：可弹出/放回、宽度可拖）
  const readerWin = { id: 'reader', el: $('reader'), paperId: null, source: 'kb', file: null, kbDir: null, min: false, docked: true };
  floatWins.push(readerWin);
  bindDeskResize(readerWin);
  $('reader-pop').addEventListener('click', () => {
    if (readerWin.docked) popReaderPanel(readerWin); else dockFloatToDesk(readerWin);
  });
  // 对话区宽度可拖（右缘调宽柄）
  bindDeskResize({ id: 'chat-panel', el: $('chat-panel') });
  document.querySelectorAll('#reader-source .src-btn').forEach(b => {
    b.addEventListener('click', () => setReaderSource(b.dataset.src));
  });
  // G7/G3：托出/放回按钮在对话区（随 #main 移动）→ 用 document 委托，绑定一次永不失效
  document.addEventListener('click', (e) => {
    if (e.target.closest('#chat-pop')) {
      chatFloated ? dockChat() : popChat();
    }
  });
  document.querySelectorAll('.side-tab').forEach(btn => {
    btn.addEventListener('click', () => setSideTab(btn.dataset.panel));
  });
  bindPapersSearch(); // E3：文献库搜索框（q 过滤 + 分页）
  connectEventStream();
  await Promise.all([loadPapers(), loadSessions()]);
  restoreFloats(); // 恢复上次的浮动阅读窗口（依赖 state.papers）
  applyReadMode(); // P2-6：阅读模式应用（floatWins 就绪后，含双阅读器补齐）
  // P2-13：初始化权重（对话 1 单位=50%、阅读器均分）并强制铺满一次
  const _chat = $('chat-panel');
  if (_chat && !_chat.dataset.grow) _chat.dataset.grow = '1';
  const _rd = $('reader');
  if (_rd && !_rd.dataset.grow) _rd.dataset.grow = '1';
  applyDeskWeights();
  setInterval(loadPapers, 5000);
  setInterval(loadSessions, 15000);
  loadKbCompile();        // P5-E2：编译进度全局可见（初始 + 15s 低频轮询）
  setInterval(loadKbCompile, 15000);
  updateTokenBar();
  setInterval(updateTokenBar, 5000);
  // P5 点3：随窗口关闭自动退出——关窗前 sendBeacon 通知后端立即关停（sendBeacon 比 fetch 可靠）
  // P0-1 修复（2026-09-11，fail-closed）：**只认服务端确认过的状态**（state.autoExitOn），
  // 不再读 DOM 复选框、也不再把"没有开关"当作开启。旧实现 `!ae || ae.checked !== false`
  // 在页面尚未加载设置、而 HTML 默认 checked 时 → 刷新/关窗即把后端硬杀
  // （实测：auto_exit 被静默置 1 → POST /api/system/shutdown → 进程 exit 0）。
  window.addEventListener('beforeunload', () => {
    if (state.autoExitOn === true) {
      try { navigator.sendBeacon('/api/system/shutdown'); } catch (e) { /* 忽略 */ }
    }
  });
  // P5 点2：清零 token 累计（重新统计当前用量）
  const _resetBtn = $('tb-reset');
  if (_resetBtn) _resetBtn.addEventListener('click', async (e) => {
    if (!(await askConfirm('清零 token 累计统计？当前全局用量将归零，不可恢复。'))) return;
    // P1：清零是写操作（POST /api/usage/reset）→ 防连点
    await guardBtn(e.currentTarget, async () => {
      try {
        const r = await api('/api/usage/reset', { method: 'POST' });
        updateTokenBar();
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'info', source: 'usage',
                      message: `token 累计已清零（删除 ${r.deleted} 条记录，费用 ¥${r.cost} 已归零）` });
      } catch (e) { alert('清零失败：' + e.message); }
    }, '清零中…');
  });
  // P2-13：窗口尺寸变化时按权重重算（面板宽度比例不变，右缘恒贴右）
  window.addEventListener('resize', () => { applyDeskWeights(); });
}

/* ══════════ 知识库管理（M5：导入/编译/元数据/问答/期刊）══════════ */
const kbaState = { scores: [], kbMap: {} };

function doiDir(doi) {
  /* 复刻 paperkb.doi_to_dirname（目录名=DOI 规范化）——仅用于匹配 kb 状态展示 */
  return String(doi || '').replace(/[/:]/g, '_').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_')
    .replace(/[^\w.\-]/g, '_').slice(0, 120) || 'paper';
}

function bindKbAdmin() {
  $('kb-admin-btn').addEventListener('click', openKbAdmin);
  $('kba-close').addEventListener('click', () => { $('kb-admin-modal').style.display = 'none'; });
  document.querySelectorAll('#kb-admin-modal .stab').forEach(btn => {
    btn.addEventListener('click', () => setKbaTab(btn.dataset.stab));
  });
  // 编译（写操作 → guardBtn 防连点；刷新任务列表为只读，不接）
  $('kba-queue-all').addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaQueueAll(), '入队中…'));
  $('kba-compile-process').addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaProcess(1), '处理中…'));
  $('kba-compile-process5').addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaProcess(5), '处理中…'));
  $('kba-jobs-refresh').addEventListener('click', kbaJobs);
  $('kba-fts-rebuild').addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaFtsRebuild(), '重建中…'));
  // 元数据
  $('kba-meta-go').addEventListener('click', () => kbaMetaList(true));
  $('kba-meta-search').addEventListener('keydown', e => { if (e.key === 'Enter') kbaMetaList(true); });
  $('kba-backfill').addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaBackfill(), '补齐中…'));
  // 期刊（preview/stats 为只读查询，不接防重）
  $('kba-journals-preview').addEventListener('click', () => kbaJournals('preview'));
  $('kba-journals-import').addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaJournals('import'), '导入中…'));
  $('kba-journals-stats').addEventListener('click', kbaJournalStats);
  $('kba-ov-save').addEventListener('click', (e) => guardBtn(e.currentTarget, () => kbaOverride(), '保存中…'));
}

function setKbaTab(tab) {
  document.querySelectorAll('#kb-admin-modal .stab').forEach(b => b.classList.toggle('active', b.dataset.stab === tab));
  ['overview', 'compile', 'meta', 'journals'].forEach(p => {
    $('kba-' + p).style.display = p === tab ? '' : 'none';
  });
  if (tab === 'overview') kbaOverview();
  if (tab === 'compile') kbaJobs();
  if (tab === 'meta') kbaMetaList(false);
  if (tab === 'journals') kbaJournalStats();
}

async function kbaOverview() {
  const box = $('kba-overview-content');
  box.innerHTML = '<div class="kba-msg">加载知识库总览…</div>';
  try {
    const s = await api('/api/kb-meta/stats');
    const esc = escapeHtml;
    const sc = s.scale, cp = s.compile, ms = s.missing;
    const link = (arr, fn) => arr && arr.length
      ? '<ul class="kba-list">' + arr.slice(0, 30).map(fn).join('') +
        (arr.length > 30 ? '<li class="muted">…共 ' + arr.length + ' 条</li>' : '') + '</ul>'
      : '<span class="muted">无</span>';
    box.innerHTML = `
      <h4>🏛️ 知识库架构（卡帕西 LLM Wiki：文档→编译→链接→问答→回灌）</h4>
      <div class="kba-detail ov-arch">输入（bib/JCR/PDF/md）→ 存储（kb/ 自包含四件）→ 编译（L0 元数据 → L1 → L2 → L3 → 概念/主题）→ 检索（编译产物 FTS5）→ 问答（≤8k 注入）→ 回灌（_qa）</div>
      <h4>📈 规模与编译完成度</h4>
      <div class="kba-stats-grid">
        <div class="kba-stat"><b>${sc.meta_count}</b><span>元数据篇(bib)</span></div>
        <div class="kba-stat"><b>${sc.library_papers}</b><span>解析库(library)</span></div>
        <div class="kba-stat"><b>${sc.kb_papers}</b><span>知识库(kb)</span></div>
        <div class="kba-stat"><b>${cp.l1_done}</b><span>L1 完成</span></div>
        <div class="kba-stat"><b>${cp.l2_done}</b><span>L2 完成</span></div>
        <div class="kba-stat"><b>${cp.l3_done}</b><span>L3 完成</span></div>
      </div>
      <div class="kba-detail" style="margin-top:8px">编译队列：待处理 <b>${cp.queued}</b> · 编译中 <b>${cp.processing}</b> · 失败 <b>${cp.failed}</b></div>
      <h4>⚠️ 缺失 / 待补</h4>
      <div class="kba-detail">
        <p><b>缺 bib 元数据</b>（需 WOS 补 bib 后才能编译）：${link(ms.missing_meta, d => '<li>' + esc(d) + '</li>')}</p>
        <p><b>未纳入知识库</b>（library 有但 kb 无）：${link(ms.not_in_kb, d => '<li>' + esc(d) + '</li>')}</p>
        <p><b>已纳入未编译</b>：${link(ms.uncompiled, d => '<li>' + esc(d) + '</li>')}</p>
      </div>
      <h4>📚 各级编译含义（帮助）</h4>
      <div class="kba-detail">
        <p><b>L0 元数据</b>：bib 导入（DOI 权威，0 token），可后补（WOS 检索式批量）。</p>
        <p><b>L1 知识编译</b>：一次调用输出「一句话贡献 + 六维（背景/方法/结果/结论/创新/局限）+ 概念标签 + 段落引用」→ _note.md（全做）。</p>
        <p><b>L2 章节要点</b>：注入 L1 压缩版，只补充章节级细节 → _details.md（中上价值）。</p>
        <p><b>L3 深度 wiki</b>：概念网络 + 批判性分析 + 跨文献链接 → _wiki.md + 概念页（高价值）。</p>
        <p><b>概念页 _concepts</b>：≥3 篇引用同概念自动聚合；<b>主题 MOC _topics</b>、<b>问答回灌 _qa</b> 为飞轮扩展。</p>
      </div>`;
  } catch (e) { box.innerHTML = kbaErr(e); }
}

async function openKbAdmin() {
  $('kb-admin-modal').style.display = 'flex';
  // 打开时按当前激活 tab 加载对应数据（总览默认；若上次停在编译/元数据则恢复）
  const active = document.querySelector('#kb-admin-modal .stab.active');
  setKbaTab(active ? active.dataset.stab : 'overview');
  try {
    const st = await api('/api/kb-meta/status');
    $('kba-status').textContent = '· 元数据 ' + st.meta_count + ' 篇';
  } catch (e) { $('kba-status').textContent = '· ' + e.message; }
}

function kbaOut(id, html) { $(id).innerHTML = html; }
function kbaErr(e) { return '<div class="kba-err">⚠ ' + escapeHtml(e.message || String(e)) + '</div>'; }

/* ---------------- 编译 ---------------- */
async function kbaQueueAll() {
  kbaOut('kba-compile-msg', '<div class="kba-msg">批量入队中…</div>');
  try {
    const r = await api('/api/kb-meta/compile/queue-all', { method: 'POST' });
    kbaOut('kba-compile-msg', '<div class="kba-ok">✅ 入队：' + escapeHtml(JSON.stringify(r)) + '</div>');
    kbaJobs();
  } catch (e) { kbaOut('kba-compile-msg', kbaErr(e)); }
}

async function kbaProcess(n) {
  kbaOut('kba-compile-msg', '<div class="kba-msg">处理队列 ' + n + ' 项中…（LLM 调用可能较慢）</div>');
  try {
    const r = await api('/api/kb-meta/compile/process', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: n })
    });
    const brief = r.map(j => escapeHtml((j.doi || '') + ' ' + (j.level || '') + '→' + (j.status || ''))).join('<br>') || '（队列已空）';
    kbaOut('kba-compile-msg', '<div class="kba-ok">✅ 处理完成：' + r.length + ' 项<br>' + brief + '</div>');
    kbaJobs();
  } catch (e) { kbaOut('kba-compile-msg', kbaErr(e)); }
}

async function kbaJobs() {
  try {
    const jobs = await api('/api/kb-meta/compile/jobs');
    const rows = jobs.map(j =>
      '<tr><td>' + escapeHtml(j.paper_doi || j.doi || '') + '</td><td>' + escapeHtml(j.level || '') + '</td>' +
      '<td>' + escapeHtml(j.status || '') + '</td><td>' + (j.value_score != null ? Number(j.value_score).toFixed(2) : '') + '</td>' +
      '<td class="muted">' + escapeHtml((j.error || '').slice(0, 60)) + '</td>' +
      '<td>' + escapeHtml((j.done_at || j.started_at || '').slice(0, 19)) + '</td></tr>').join('');
    $('kba-jobs-body').innerHTML = rows || '<tr><td colspan="6" class="muted">暂无编译任务（先导入内容并批量入队）</td></tr>';
  } catch (e) { $('kba-jobs-body').innerHTML = '<tr><td colspan="6">' + escapeHtml(e.message) + '</td></tr>'; }
}

async function kbaFtsRebuild() {
  kbaOut('kba-compile-msg', '<div class="kba-msg">重建 FTS 索引中…</div>');
  try {
    const r = await api('/api/kb-meta/fts/rebuild', { method: 'POST' });
    kbaOut('kba-compile-msg', '<div class="kba-ok">✅ 重建完成：' + escapeHtml(JSON.stringify(r)) + '</div>');
  } catch (e) { kbaOut('kba-compile-msg', kbaErr(e)); }
}

/* ---------------- 元数据 ---------------- */
async function kbaMetaList(force) {
  const q = $('kba-meta-search').value.trim();
  try {
    let papers, kb;
    if (force && q) {
      papers = await api('/api/kb-meta/search?q=' + encodeURIComponent(q));
      kb = await api('/api/kb-meta/kb/status');
    } else {
      const [s, k] = await Promise.all([api('/api/kb-meta/scores'), api('/api/kb-meta/kb/status')]);
      papers = s; kb = k;
    }
    kbaState.scores = papers;
    kbaState.kbMap = {};
    kb.forEach(d => { kbaState.kbMap[d.dir] = d; });
    if (!papers.length) { $('kba-meta-body').innerHTML = '<tr><td colspan="5" class="muted">暂无元数据（先导入 bib，或扫描缺失 DOI 补）</td></tr>'; return; }
    const rows = papers.map(p => {
      const d = doiDir(p.doi);
      const k = kbaState.kbMap[d];
      const kbBadge = k
        ? '<span class="kba-badge ok">✓ ' + ['document.json', 'en.md'].filter(f => k[f]).length + '/2 文</span>' +
          (k['source.pdf'] ? '' : ' <span class="kba-badge warn">无PDF</span>') +
          (k['images'] ? '' : ' <span class="kba-badge warn">无图</span>') +
          (k.note ? ' <span class="kba-badge ok">_note</span>' : '')
        : '<span class="kba-badge">未纳入</span>';
      return '<tr><td title="' + escapeHtml(p.title || '') + '">' + escapeHtml(shortTitle(p.title, 52)) +
        '<div class="muted" style="font-size:11px">' + escapeHtml(p.doi || '') + '</div></td>' +
        '<td>' + escapeHtml(p.journal || '') + '<div class="muted" style="font-size:11px">' + escapeHtml(p.year || '') + (p.times_cited ? ' · 被引 ' + p.times_cited : '') + '</div></td>' +
        '<td>' + (p.score != null ? Number(p.score).toFixed(2) : '-') + '<div class="muted" style="font-size:11px">' + escapeHtml(p.level || '') + '</div></td>' +
        '<td>' + kbBadge + '</td>' +
        '<td class="kba-actions"><button class="btn small" data-act="detail" data-doi="' + escapeHtml(p.doi) + '">详情</button></td></tr>';
    }).join('');
    $('kba-meta-body').innerHTML = rows;
    // 委托：行内操作
    $('kba-meta-body').querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.dataset.act === 'detail') kbaMetaDetail(btn.dataset.doi);
      });
    });
  } catch (e) { $('kba-meta-body').innerHTML = '<tr><td colspan="5">' + escapeHtml(e.message) + '</td></tr>'; }
}

async function kbaBackfill() {
  $('kba-meta-msg').innerHTML = '<div class="kba-msg">补齐 kb 原文层中…（扫 library 全部文献）</div>';
  try {
    const r = await api('/api/kb-meta/backfill', { method: 'POST' });
    const lines = (r.done || []).map(d =>
      '✅ ' + escapeHtml(d.doi) + ' 复制 ' + escapeHtml((d.copied || []).join(',')) +
      (d.verify ? ' · 校验一致' : '')).join('<br>');
    const skip = (r.skipped || []).map(s => '⏭ ' + escapeHtml(s.doi) + '（已存在）').join('<br>');
    const fail = (r.failed || []).map(f => '⚠ ' + escapeHtml(f.doi) + ' ' + escapeHtml(f.error || '')).join('<br>');
    $('kba-meta-msg').innerHTML = '<div class="kba-ok">✅ 补齐完成：' + r.count + ' 篇' +
      (lines ? '<br>' + lines : '') + (skip ? '<br>' + skip : '') + (fail ? '<br>' + fail : '') + '</div>';
    kbaMetaList(false);
  } catch (e) { $('kba-meta-msg').innerHTML = kbaErr(e); }
}

async function kbaMetaDetail(doi) {
  const box = $('kba-meta-detail');
  box.innerHTML = '<div class="kba-msg">加载详情…</div>';
  try {
    const [p, c, s] = await Promise.all([
      api('/api/kb-meta/paper?doi=' + encodeURIComponent(doi)),
      api('/api/kb-meta/citations?doi=' + encodeURIComponent(doi)),
      api('/api/kb-meta/source/status?doi=' + encodeURIComponent(doi))
    ]);
    const refs = (c.references || c.cited || []).slice(0, 15).map(r =>
      '<li>' + escapeHtml((r.cited_brief || r.brief || '') + (r.cited_doi ? ' · ' + r.cited_doi : '')) + '</li>').join('');
    const citing = (c.citing || []).slice(0, 10).map(r => '<li>' + escapeHtml(r.citing_doi || '') + '</li>').join('');
    const src = (b) => b && b.exists
      ? '✓ ' + ['source.pdf', 'en.md', 'document.json'].filter(f => b[f]).join(', ') + (b.images ? ' + images/' : '')
      : '—';
    box.innerHTML =
      '<h4>' + escapeHtml(p.title || doi) + '</h4>' +
      '<p class="muted">' + escapeHtml((p.authors || []).join(', ')) + '</p>' +
      '<p><b>期刊：</b>' + escapeHtml(p.journal || '') + ' · ' + escapeHtml(p.year || '') +
      ' · ISSN ' + escapeHtml(p.issn || '') + ' · 被引 ' + (p.times_cited ?? 0) +
      (p.journal_override ? ' · 期刊纠正：' + escapeHtml(p.journal_override) : '') + '</p>' +
      (p.abstract ? '<details><summary>摘要</summary><p class="muted">' + escapeHtml(p.abstract.slice(0, 900)) + '</p></details>' : '') +
      '<div class="kba-detail-grid">' +
      '<div><b>library 原文层：</b>' + escapeHtml(src(s.library)) + '</div>' +
      '<div><b>kb 原文层：</b>' + escapeHtml(src(s.kb)) + '</div>' +
      '</div>' +
      (refs ? '<details><summary>引用的文献（' + (c.references || c.cited || []).length + '）</summary><ul>' + refs + '</ul></details>' : '') +
      (citing ? '<details><summary>被引用（' + (c.citing || []).length + '）</summary><ul>' + citing + '</ul></details>' : '') +
      '<div class="form-actions"><button class="btn small" id="kba-detail-close">收起</button></div>';
    $('kba-detail-close').addEventListener('click', () => { box.innerHTML = ''; });
  } catch (e) { box.innerHTML = kbaErr(e); }
}

/* ---------------- 期刊 ---------------- */
async function kbaJournals(mode) {
  const path = $('kba-journals-path').value.trim();
  if (!path) { $('kba-journals-result').innerHTML = '<div class="kba-err">请输入 xlsx 路径</div>'; return; }
  $('kba-journals-result').innerHTML = '<div class="kba-msg">' + (mode === 'preview' ? '解析中…（只回摘要）' : '导入中…（upsert 幂等）') + '</div>';
  try {
    const r = await api('/api/kb-meta/journals/' + mode, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path })
    });
    $('kba-journals-result').innerHTML = '<div class="kba-ok">' + escapeHtml(JSON.stringify(r)) + '</div>';
    if (mode === 'import') kbaJournalStats();
  } catch (e) { $('kba-journals-result').innerHTML = kbaErr(e); }
}

async function kbaJournalStats() {
  try {
    const s = await api('/api/kb-meta/journals/stats');
    $('kba-journals-result').innerHTML = '<div class="kba-ok">🏛️ ' + escapeHtml(JSON.stringify(s)) + '</div>';
  } catch (e) { $('kba-journals-result').innerHTML = kbaErr(e); }
}

async function kbaOverride() {
  const doi = $('kba-ov-doi').value.trim();
  const name = $('kba-ov-name').value.trim();
  if (!doi || !name) { $('kba-ov-result').innerHTML = '<div class="kba-err">DOI 与期刊标准名都要填</div>'; return; }
  try {
    const r = await api('/api/kb-meta/journals/override', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doi, journal_name: name })
    });
    $('kba-ov-result').innerHTML = '<div class="kba-ok">✅ 已纠正 → ' + escapeHtml(r.journal_override) + '（新评分 ' + (r.score && r.score.score != null ? Number(r.score.score).toFixed(2) : '?') + '）</div>';
  } catch (e) { $('kba-ov-result').innerHTML = kbaErr(e); }
}


boot();
