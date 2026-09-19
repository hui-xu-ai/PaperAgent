/* diary.js — 阅读日记（日历 + 日志 + 心得笔记） */

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
