/* ══════════════════════════════════════════════════════════
   PaperAgent 前端 — UI 交互层（从 app.js 拆出）
   toast / askConfirm / askInput / guardBtn / 离线提示 /
   高度拖拽 / 左栏 tab·宽度·折叠 / 阅读模式 / 主题皮肤
   加载顺序：utils.js → ui.js → lit-admin.js → app.js
   ══════════════════════════════════════════════════════════ */
'use strict';

/* ══════════ P1：统一交互层 ══════════════════════════════════
   toast / askConfirm / askInput / guardBtn / 离线提示条
   window.alert 重定向到 toast（非阻塞）。 */

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
  if (!mask) return Promise.resolve(window.confirm(msg));
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

/* 输入对话框：替代 prompt()（prompt 在部分环境被禁用） */
function askInput(msg, defaultValue = '', opts = {}) {
  const mask = $('input-modal');
  if (!mask) return Promise.resolve(prompt(msg, defaultValue));
  $('input-title').textContent = opts.title || '输入';
  $('input-msg').textContent = String(msg ?? '');
  const field = $('input-field');
  field.value = defaultValue;
  $('input-yes').textContent = opts.okText || '确定';
  $('input-no').textContent = opts.cancelText || '取消';
  mask.style.display = 'flex';
  return new Promise((resolve) => {
    const done = (val) => {
      mask.style.display = 'none';
      $('input-yes').removeEventListener('click', onYes);
      $('input-no').removeEventListener('click', onNo);
      mask.removeEventListener('click', onMask);
      document.removeEventListener('keydown', onKey);
      field.removeEventListener('keydown', onFieldKey);
      resolve(val);
    };
    const onYes = () => done(field.value);
    const onNo = () => done(null);
    const onMask = (e) => { if (e.target === mask) done(null); };
    const onKey = (e) => { if (e.key === 'Escape') done(null); };
    const onFieldKey = (e) => { if (e.key === 'Enter') { e.preventDefault(); done(field.value); } };
    $('input-yes').addEventListener('click', onYes);
    $('input-no').addEventListener('click', onNo);
    mask.addEventListener('click', onMask);
    document.addEventListener('keydown', onKey);
    field.addEventListener('keydown', onFieldKey);
    setTimeout(() => { field.focus(); field.select(); }, 30);
  });
}

/* 写操作防重：同一按钮在请求期间禁用 */
async function guardBtn(btn, fn, busyText = '处理\u2026') {
  if (!btn) return fn();
  if (btn.dataset.busy === '1') return null;
  const old = btn.textContent;
  const wasDisabled = btn.disabled;
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

/* 离线提示条 */
const offlineState = { off: false, backendDown: false, browserOffline: false };
function setOfflineBar() {
  const bar = $('offline-bar');
  if (!bar) return;
  const off = offlineState.browserOffline || navigator.onLine === false;
  const down = offlineState.backendDown;
  offlineState.off = off;
  if (!off && !down) { bar.style.display = 'none'; bar.textContent = ''; return; }
  bar.style.display = '';
  bar.textContent = off
    ? '\u26a0 \u7f51\u7edc\u5df2\u65ad\u5f00\uff08\u672c\u673a\u89e3\u6790/\u7ffb\u8bd1\u6682\u65f6\u4e0d\u53ef\u7528\uff1b\u5df2\u5bfc\u5165\u7684\u6587\u732e\u4e0e\u77e5\u8bc6\u5e93\u4e0d\u53d7\u5f71\u54cd\uff09'
    : '\u26a0 \u65e0\u6cd5\u8fde\u63a5\u540e\u7aef\u670d\u52a1\uff08127.0.0.1:8900\uff09\uff1a\u8bf7\u786e\u8ba4\u540e\u7aef\u4ecd\u5728\u8fd0\u884c\uff0c\u6216\u7a0d\u540e\u81ea\u52a8\u91cd\u8bd5';
}
function bindConnectivity() {
  window.addEventListener('online', () => { offlineState.browserOffline = false; setOfflineBar(); });
  window.addEventListener('offline', () => { offlineState.browserOffline = true; setOfflineBar(); });
  offlineState.browserOffline = navigator.onLine === false;
  setOfflineBar();
  setInterval(async () => {
    try {
      await fetch('/api/health', { cache: 'no-store' });
      if (offlineState.backendDown) { offlineState.backendDown = false; setOfflineBar(); }
    } catch (e) {
      if (!offlineState.backendDown) {
        offlineState.backendDown = true;
        setOfflineBar();
        appendEvent({ ts: new Date().toLocaleTimeString(), level: 'error', source: 'system',
                      message: '\u540e\u7aef\u63a2\u6d3b\u5931\u8d25\uff1a\u65e0\u6cd5\u8fde\u63a5 /api/health\uff08\u7f51\u7edc\u6216\u670d\u52a1\u5df2\u505c\uff09' });
      }
    }
  }, 10000);
}
/* 兼容层 */
window.alert = function (msg) { toast(msg, 'info'); };

/* ══════════ 高度拖拽 ══════════ */
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

/* ══════════ 左栏 tab 切换 ══════════ */
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

/* ══════════ 左栏拖拽宽度 + 自动折叠/钉住 ══════════ */
function setPanelWidth(side, w) {
  const clamped = Math.max(440, Math.min(w, window.innerWidth * 0.42));
  document.documentElement.style.setProperty(side === 'left' ? '--side-w' : '--reader-w', clamped + 'px');
  localStorage.setItem('panel-w-' + side, String(clamped));
}

function bindCollapse() {
  const ws = $('workspace');
  const sb = $('sidebar');
  let pinned = localStorage.getItem('side-pinned') === '1';
  let foldTimer = null;
  const setCollapsed = (c) => {
    ws.classList.toggle('side-collapsed', c);
    localStorage.setItem('side-collapsed', c ? '1' : '0');
  };
  setCollapsed(!pinned);
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
  if (sb) {
    sb.addEventListener('mouseenter', () => {
      if (foldTimer) { clearTimeout(foldTimer); foldTimer = null; }
      if (!pinned && !readMode) ws.classList.remove('side-collapsed');
    });
    sb.addEventListener('mouseleave', () => {
      if (pinned || readMode) return;
      if (foldTimer) clearTimeout(foldTimer);
      foldTimer = setTimeout(() => {
        foldTimer = null;
        if (!pinned && !readMode) ws.classList.add('side-collapsed');
      }, 450);
    });
  }
  const pin = $('side-pin');
  if (pin) {
    const render = () => {
      pin.classList.toggle('active', pinned);
      pin.title = pinned ? '\u5df2\u9489\u4f4f\uff08\u5de6\u680f\u5e38\u9a7b\uff0c\u4e0d\u81ea\u52a8\u6298\u53e0\uff09' : '\u70b9\u51fb\u9489\u4f4f\uff08\u5de6\u680f\u5e38\u9a7b\uff09';
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
    if (!handle) return;
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

/* ══════════ 阅读模式 / 主题皮肤 ══════════ */
let readMode = false;
let readWin2 = null;
let readHidden = [];
let theme = localStorage.getItem('theme') || 'default';
let themePref = localStorage.getItem('theme-pref') || 'sepia';
const THEMES = ['default', 'sepia', 'night', 'paper'];
const THEME_LABELS = { default: '\u9ed8\u8ba4', sepia: '\u62a4\u773c \u00b7 \u7c73\u9ec4', night: '\u591c\u95f4 \u00b7 \u6df1\u8272', paper: '\u7eb8\u8d28 \u00b7 \u8c46\u7eff' };

function isReaderWin(w) {
  return w && w.el && (w.el.id === 'reader' || (w.el.id || '').startsWith('fw'));
}

function enterReadMode() {
  const ws = $('workspace');
  ws.classList.add('read-mode');
  floatWins.forEach(w => {
    if (!isReaderWin(w)) return;
    if (!w.docked) dockFloatToDesk(w);
    w.el.style.flex = ''; w.el.style.width = ''; w.el.style.left = '';
    w.el.style.top = ''; w.el.style.height = ''; w.el.style.display = '';
  });
  const readers = floatWins.filter(w => isReaderWin(w) && w.docked);
  readHidden = readers.slice(2);
  readHidden.forEach(w => { w.el.style.display = 'none'; });
  if (readers.length < 2) {
    let win2 = (readWin2 && document.body.contains(readWin2.el)) ? readWin2 : null;
    if (!win2) {
      openReaderWindow(true);
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
  const cp = $('chat-panel');
  if (cp) { cp.dataset.grow = '0.25'; cp.style.flex = ''; }
  resetReaderWeights();
  applyDeskWeights();
}

function exitReadMode() {
  const ws = $('workspace');
  ws.classList.remove('read-mode');
  floatWins.slice().forEach(w => {
    if (!isReaderWin(w)) return;
    if (w.el.id === 'reader') return;
    closeFloatWin(w);
  });
  readHidden = [];
  readWin2 = null;
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
  btn.textContent = readMode ? '\ud83d\udcd6 \u9000\u51fa\u9605\u8bfb' : '\ud83d\udcd6 \u8fdb\u5165\u9605\u8bfb';
  btn.title = readMode ? '\u9000\u51fa\u9605\u8bfb\u6a21\u5f0f' : '\u9605\u8bfb\u6a21\u5f0f\uff1a\u9690\u85cf\u4fa7\u8fb9\u680f\uff0c\u5de6\u5bf9\u8bdd + \u53cc\u9605\u8bfb\u5668\u94fa\u6ee1\u5e73\u5206';
}

function applyTheme() {
  if (theme === 'default') delete document.body.dataset.theme;
  else document.body.dataset.theme = theme;
  const btn = $('eye-care-btn');
  if (btn) {
    btn.classList.toggle('active', theme === 'sepia');
    btn.title = theme === 'sepia'
      ? '\u5f53\u524d\uff1a\u62a4\u773c \u00b7 \u7c73\u9ec4\uff08\u70b9\u6b64\u6062\u590d\u9ed8\u8ba4\uff09'
      : '\u62a4\u773c\u6a21\u5f0f\uff08\u5207\u6362\u5230\u7c73\u9ec4\u62a4\u773c\u5e95\u8272\uff1b\u66f4\u591a\u4e3b\u9898\u5728 \u8bbe\u7f6e \u2192 \u5916\u89c2\uff09';
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
      themePref = theme === 'default' ? themePref : theme;
      localStorage.setItem('theme-pref', themePref);
      localStorage.setItem('theme', theme);
      applyTheme();
    });
  }
  applyTheme();
}
