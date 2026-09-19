/* ══════════════════════════════════════════════════════════
   PaperAgent 前端 — 核心工具函数（从 app.js 拆出）
   加载顺序：utils.js → ui.js → lit-admin.js → app.js
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
  papersPage: 1,
  papersPageSize: 50,
  papersTotal: 0,
  papersQ: '',
  papersDate: 'all',
  papersKind: 'all',
  papersLoaded: 0,
  paperById: {},
  paperRidById: {},
  paperSessionOpening: null,
  sessDate: 'all',
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

function getPaper(id) {
  return (id != null && state.paperById[id]) || null;
}
