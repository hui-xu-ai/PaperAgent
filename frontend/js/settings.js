/* settings.js — 设置中心（模型/解析/知识库/界面/插件/关于 + 显示/外观） */

/* ══════════ 设置中心 ══════════ */
const settingsState = { providers: [], active: '', editing: -1, prices: null, translation_providers: [] };
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
  bindTranslateProviderEvents();  // T1：翻译模型配置
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
    settingsState.translation_providers = s.translation_providers || [];  // T1：翻译模型池
    renderProviders();
    renderTranslateProvider();
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

// ---------------------------------------------------------------- T1：翻译模型池（多模型：切换/并行）
let tpEditingIndex = -1;   // 当前编辑的池下标（-1 = 新增）

function renderTranslateProvider() {
  const pool = settingsState.translation_providers || [];
  const noneEl = $('translate-provider-none');
  const infoEl = $('translate-provider-info');
  const tbody = $('translate-provider-tbody');
  if (!pool.length) {
    noneEl.style.display = '';
    infoEl.style.display = 'none';
    return;
  }
  noneEl.style.display = 'none';
  infoEl.style.display = '';
  const enabledCount = pool.filter(p => p.enabled).length;
  tbody.innerHTML = pool.map((p, i) => `<tr>
    <td><input type="checkbox" data-tp-toggle="${i}" ${p.enabled ? 'checked' : ''}
         title="启用后参与翻译（启用多个=并行轮询）"></td>
    <td>${escapeHtml(p.name)}${p.enabled ? ' <em class="muted">(启用)</em>' : ''}</td>
    <td title="${escapeHtml(p.base_url)}">${escapeHtml(shortTitle(p.base_url, 24))}</td>
    <td>${escapeHtml(p.model)}</td>
    <td><button class="btn small" data-tp-edit="${i}">编辑</button>
        <button class="btn small danger" data-tp-del="${i}" title="从池中删除">删除</button></td>
  </tr>`).join('');
  // 事件委托（动态行）
  tbody.querySelectorAll('[data-tp-toggle]').forEach(cb =>
    cb.addEventListener('change', () => toggleTranslateProvider(Number(cb.dataset.tpToggle))));
  tbody.querySelectorAll('[data-tp-edit]').forEach(b =>
    b.addEventListener('click', () => editTranslateProvider(Number(b.dataset.tpEdit))));
  tbody.querySelectorAll('[data-tp-del]').forEach(b =>
    b.addEventListener('click', () => deleteTranslateProvider(Number(b.dataset.tpDel))));
  if (enabledCount > 1) {
    noneEl.style.display = 'none';
  }
}

function editTranslateProvider(index = -1) {
  tpEditingIndex = index;
  const tp = index >= 0 ? settingsState.translation_providers[index] : null;
  $('tpf-name').value = tp?.name || '';
  $('tpf-base').value = tp?.base_url || '';
  $('tpf-model').value = tp?.model || '';
  $('tpf-key').value = (tp?.api_key && tp.api_key !== '未设置') ? KEY_MASK : '';
  $('tpf-max').value = tp?.max_tokens ?? '';
  $('tpf-test-result').textContent = '';
  $('translate-provider-form').style.display = 'flex';
}

async function toggleTranslateProvider(index) {
  const pool = settingsState.translation_providers;
  if (index < 0 || index >= pool.length) return;
  pool[index].enabled = !pool[index].enabled;
  try {
    const saved = await api('/api/settings/translation-providers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pool),
    });
    settingsState.translation_providers = saved.translation_providers || pool;
    renderTranslateProvider();
  } catch (e) {
    pool[index].enabled = !pool[index].enabled;   // 回滚
    alert('切换失败：' + e.message);
  }
}

async function deleteTranslateProvider(index) {
  const pool = settingsState.translation_providers;
  if (index < 0 || index >= pool.length) return;
  const p = pool[index];
  if (!(await askConfirm(`删除翻译模型「${p.name || p.model}」？`))) return;
  const next = pool.filter((_, i) => i !== index);
  try {
    const saved = await api('/api/settings/translation-providers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next),
    });
    settingsState.translation_providers = saved.translation_providers || next;
    renderTranslateProvider();
  } catch (e) { alert('删除失败：' + e.message); }
}

async function clearTranslateProvider() {
  if (!(await askConfirm('清除全部翻译专用模型？将回落使用主模型翻译。'))) return;
  try {
    await api('/api/settings/translation-providers', { method: 'DELETE' });
    settingsState.translation_providers = [];
    renderTranslateProvider();
    $('translate-provider-form').style.display = 'none';
    alert('已清除（使用主模型翻译）');
  } catch (e) { alert('清除失败：' + e.message); }
}

async function saveTranslateProvider() {
  const keyVal = $('tpf-key').value.trim();
  const keyPlaceholder = !keyVal || keyVal.startsWith('•') ||
    keyVal.includes('…') || keyVal.includes('*');
  const editing = tpEditingIndex >= 0 ? settingsState.translation_providers[tpEditingIndex] : null;
  const apiKey = keyPlaceholder ? (editing?.api_key || '') : keyVal;
  const maxTxt = $('tpf-max').value.trim();
  const maxNum = Number(maxTxt);
  const maxTokens = (maxTxt === '' || !Number.isFinite(maxNum) || maxNum <= 0) ? undefined : maxNum;
  const body = {
    id: editing?.id || '',
    name: $('tpf-name').value || '翻译模型',
    base_url: $('tpf-base').value,
    model: $('tpf-model').value,
    api_key: apiKey,
    max_tokens: maxTokens ?? editing?.max_tokens ?? 64000,
    reasoning_effort: editing?.reasoning_effort ?? null,
    enabled: editing ? !!editing.enabled : true,   // 新增默认启用
  };
  if (!body.base_url || !body.model || !body.api_key) {
    alert('请填写 Base URL、模型和 API Key');
    return;
  }
  const pool = (settingsState.translation_providers || []).map(p => ({ ...p }));
  if (tpEditingIndex >= 0 && tpEditingIndex < pool.length) pool[tpEditingIndex] = body;
  else pool.push(body);
  try {
    const saved = await api('/api/settings/translation-providers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pool),
    });
    settingsState.translation_providers = saved.translation_providers || pool;
    renderTranslateProvider();
    $('translate-provider-form').style.display = 'none';
    alert(saved.llm_ready ? '✅ 翻译模型池已保存生效' : '⚠ 翻译模型配置无效（Key 错误？）');
  } catch (e) { alert('保存失败：' + e.message); }
}

async function testTranslateProvider() {
  const keyVal = $('tpf-key').value.trim();
  const masked = !keyVal || keyVal.startsWith('•') ||
    keyVal.includes('…') || keyVal.includes('*');
  const editing = tpEditingIndex >= 0 ? settingsState.translation_providers[tpEditingIndex] : null;
  const hasStored = !!(editing && editing.api_key && editing.api_key !== '未设置');
  const body = {
    id: editing?.id || '',
    name: $('tpf-name').value || 'test',
    base_url: $('tpf-base').value,
    model: $('tpf-model').value,
    api_key: (masked && hasStored) ? editing.api_key : keyVal,
    max_tokens: 64000,
  };
  if (!body.api_key) {
    $('tpf-test-result').textContent = '⚠ 请先填写 API Key';
    $('tpf-test-result').style.color = '#e67e22';
    return;
  }
  $('tpf-test-result').textContent = '测试中…';
  $('tpf-test-result').style.color = '';
  try {
    const r = await api('/api/settings/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    $('tpf-test-result').textContent = `✅ ${r.reply || 'OK'}`;
    $('tpf-test-result').style.color = '#27ae60';
  } catch (e) {
    $('tpf-test-result').textContent = `❌ ${e.message}`;
    $('tpf-test-result').style.color = '#c0392b';
  }
}

function bindTranslateProviderEvents() {
  const editBtn = $('translate-provider-edit');
  if (editBtn) editBtn.addEventListener('click', () => editTranslateProvider(-1));
  const saveBtn = $('tpf-save');
  if (saveBtn) saveBtn.addEventListener('click', (e) => guardBtn(e.currentTarget, () => saveTranslateProvider(), '保存中…'));
  const cancelBtn = $('tpf-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', () => { $('translate-provider-form').style.display = 'none'; });
  const clearBtn = $('tpf-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => guardBtn(clearBtn, () => clearTranslateProvider(), '清除中…'));
  const testBtn = $('tpf-test');
  if (testBtn) testBtn.addEventListener('click', () => guardBtn(testBtn, () => testTranslateProvider(), '测试中…'));
  const keyToggle = $('tpf-key-toggle');
  if (keyToggle) keyToggle.addEventListener('click', () => {
    const inp = $('tpf-key');
    inp.type = inp.type === 'password' ? 'text' : 'password';
    keyToggle.textContent = inp.type === 'password' ? '显示' : '隐藏';
  });
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
// kb 产物由数据布局契约固定：_note.md / _wiki.md / _relations.md / en.md / document.json / images/。

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


/* ─── 显示设置 + 外观 CSS + 插件安裈（从 app.js 合并） ─── */

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
