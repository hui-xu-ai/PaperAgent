/* chat.js — 对话 + 写回飞轮（回答存_qa） + 回收站 */

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
          } else if (ev.type === 'limit_warning') {
            // 软限制预警：接近 TokenGuard 红线
            const w = document.createElement('div');
            w.className = 'limit-warning';
            const u = ev.usage || {};
            w.innerHTML = '⚠️ ' + escapeHtml(ev.message || '接近调用限制') +
              (u.call_pct ? ` <span class="muted">（调用 ${u.call_pct}%，输入 ${u.char_pct}%）</span>` : '');
            answerBox.appendChild(w);
          } else if (ev.type === 'limit_reached') {
            // 硬限制触发：显示反思摘要 + 继续按钮
            const card = document.createElement('div');
            card.className = 'limit-card';
            card.innerHTML = '<div class="limit-body">' + renderMarkdown(ev.message || '已达调用限制') + '</div>' +
              '<div class="limit-actions">' +
              '<button class="btn-sm btn-primary" onclick="sendQuestion_continue()">📝 继续写作</button>' +
              '<button class="btn-sm" onclick="this.closest(\'.limit-card\').remove()">关闭</button>' +
              '</div>';
            answerBox.appendChild(card);
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

/* 写作任务继续：发送「继续」重置 TokenGuard 并续写 */
function sendQuestion_continue() {
  const input = $('question-input');
  if (input) input.value = '继续';
  sendQuestion();
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

/* 回收站（2026-09-19 重设计）：参考 AI检索文献库——kba-table 表格 + 客户端搜索过滤 +
   上一页/下一页翻页 + 行内恢复/彻底删除（事件委托）+ 顶部清空。列表一次拉全（量小），
   搜索/翻页纯前端，浏览/查找/恢复更灵活。 */
let _trashItems = [];
let _trashPage = 0;
let _trashQ = '';
const _TRASH_PAGE_SIZE = 12;

async function openTrashModal() {
  const mask = $('trash-modal');
  if (!mask) return;
  mask.style.display = 'flex';
  _trashPage = 0; _trashQ = '';
  const sb = $('trash-search'); if (sb) sb.value = '';
  const tbody = $('trash-tbody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="muted">加载中…</td></tr>';
  try {
    const r = await api('/api/kb-meta/kb/trash');
    _trashItems = r.items || [];
  } catch (e) {
    _trashItems = [];
    if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="empty">加载失败：' + escapeHtml(e.message) + '</td></tr>';
    return;
  }
  renderTrash();
}

function _trashFiltered() {
  const q = _trashQ.trim().toLowerCase();
  if (!q) return _trashItems;
  return _trashItems.filter(it =>
    [it.title, it.doi, it.journal, it.year, it.key].some(v => String(v || '').toLowerCase().includes(q)));
}

function renderTrash() {
  const tbody = $('trash-tbody');
  if (!tbody) return;
  const filtered = _trashFiltered();
  const totalPages = Math.max(1, Math.ceil(filtered.length / _TRASH_PAGE_SIZE));
  if (_trashPage >= totalPages) _trashPage = totalPages - 1;
  if (_trashPage < 0) _trashPage = 0;
  const start = _trashPage * _TRASH_PAGE_SIZE;
  const pageItems = filtered.slice(start, start + _TRASH_PAGE_SIZE);

  const cnt = $('trash-count');
  if (cnt) cnt.textContent = _trashItems.length
    ? `共 ${_trashItems.length} 篇` + (filtered.length !== _trashItems.length ? ` · 筛选出 ${filtered.length}` : '')
      + ' · 产物全部保留；仅当知识库无同名文献时可恢复，若已有请到回收站文件夹手动挑选覆盖文件'
    : '';
  const emptyBtn = $('trash-empty');
  if (emptyBtn) emptyBtn.style.display = _trashItems.length ? '' : 'none';
  const pager = document.querySelector('.trash-pager');
  if (pager) pager.style.display = filtered.length ? '' : 'none';
  const pg = $('trash-page');
  if (pg) pg.textContent = filtered.length ? `第 ${_trashPage + 1} / ${totalPages} 页` : '';
  const prev = $('trash-prev'), next = $('trash-next');
  if (prev) prev.disabled = _trashPage <= 0;
  if (next) next.disabled = _trashPage >= totalPages - 1;

  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">${_trashItems.length ? '无匹配的文献' : '回收站是空的'}</td></tr>`;
    return;
  }
  tbody.innerHTML = pageItems.map(it => {
    const doi = it.doi || it.key || '';
    const ts = (it.trashed_at || '').replace('T', ' ').slice(0, 16);
    return `<tr data-key="${escapeHtml(it.key)}">
      <td class="trash-cell-title" title="${escapeHtml(it.title || it.key)}">${escapeHtml(shortTitle(it.title || it.key, 64))}</td>
      <td>${escapeHtml(it.journal || '')}</td>
      <td>${escapeHtml(String(it.year || ''))}</td>
      <td class="muted trash-cell-doi" title="${escapeHtml(doi)}">${escapeHtml(doi)}</td>
      <td class="muted">${escapeHtml(ts)}</td>
      <td class="trash-ops">
        <button class="btn small primary" data-act="restore" title="恢复回知识库">↩ 恢复</button>
        <button class="btn small danger" data-act="delete" title="彻底删除（不可恢复）">✕ 彻底删除</button>
      </td></tr>`;
  }).join('');
}

/* 行内操作事件委托（动态渲染的行用委托绑定，避免逐个 addEventListener 失效） */
function _trashRowDelegate(e) {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const tr = btn.closest('tr');
  const key = tr && tr.dataset.key;
  if (!key) return;
  if (btn.dataset.act === 'restore') _trashRestore(key, btn);
  else if (btn.dataset.act === 'delete') _trashDeleteOne(key, btn);
}

function _trashRestore(key, btn) {
  guardBtn(btn, async () => {
    try {
      const r = await api('/api/kb-meta/kb/restore', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }),
      });
      if (r.status === 'conflict') {
        // 设计口径（2026-09-19）：知识库已有同名文献则不恢复——由用户自行到回收站文件夹
        // 手动挑选要覆盖的文件。行保留，仅提示手动合并路径。
        toast('⚠ 未恢复：' + (r.message || '知识库已存在同名文献'), 'warning', { ttl: 12000 });
        return;
      }
      toast('↩ 已恢复：' + (r.kb_dir || key), 'success');
      _trashItems = _trashItems.filter(x => x.key !== key);
      renderTrash();
      await loadKbList();
      await loadPapers();
      refreshTrashCount();
    } catch (err) { toast('恢复失败：' + err.message, 'error'); }
  }, '恢复中…');
}

function _trashDeleteOne(key, btn) {
  const it = _trashItems.find(x => x.key === key) || {};
  const title = shortTitle(it.title || key, 40);
  askConfirm(`确定彻底删除「${title}」？此操作不可恢复（仅删回收站副本，文献库记录与解析产物保留）。`,
             { title: '彻底删除', okText: '彻底删除', cancelText: '取消' }).then(ok => {
    if (!ok) return;
    guardBtn(btn, async () => {
      try {
        await api('/api/kb-meta/kb/trash/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key }),
        });
        toast('🗑 已彻底删除：' + title, 'success');
        _trashItems = _trashItems.filter(x => x.key !== key);
        renderTrash();
        refreshTrashCount();
      } catch (err) { toast('彻底删除失败：' + err.message, 'error'); }
    }, '删除中…');
  });
}

function _trashEmptyAll() {
  if (!_trashItems.length) return;
  askConfirm(`确定清空回收站（彻底删除全部 ${_trashItems.length} 篇）？此操作不可恢复。`,
             { title: '清空回收站', okText: '清空', cancelText: '取消' }).then(ok => {
    if (!ok) return;
    const btn = $('trash-empty');
    guardBtn(btn, async () => {
      try {
        const r = await api('/api/kb-meta/kb/trash/empty', { method: 'POST' });
        toast(`🗑 回收站已清空（${r.removed_keys || 0} 篇）`, 'success');
        _trashItems = [];
        renderTrash();
        refreshTrashCount();
      } catch (err) { toast('清空失败：' + err.message, 'error'); }
    }, '清空中…');
  });
}

/* 回收站静态控件绑定（init 时一次）：搜索/翻页/清空/行委托 */
function bindTrashModal() {
  const sb = $('trash-search');
  if (sb) sb.addEventListener('input', () => { _trashQ = sb.value; _trashPage = 0; renderTrash(); });
  const prev = $('trash-prev');
  if (prev) prev.addEventListener('click', () => { if (_trashPage > 0) { _trashPage--; renderTrash(); } });
  const next = $('trash-next');
  if (next) next.addEventListener('click', () => { _trashPage++; renderTrash(); });
  const emptyBtn = $('trash-empty');
  if (emptyBtn) emptyBtn.addEventListener('click', _trashEmptyAll);
  const tbody = $('trash-tbody');
  if (tbody) tbody.addEventListener('click', _trashRowDelegate);
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

