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

