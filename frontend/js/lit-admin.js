/* ═══════════════════════════════════════════════════════════════
 * lit-admin.js — AI检索管理中心（paperlit L1）前端逻辑
 * 从 app.js 抽离（2026-09-18，前端模块化）。经典脚本（非 ES module），
 * 与 app.js 共享全局作用域：依赖 app.js 的 $ / api / guardBtn / escapeHtml /
 * shortTitle 等全局；本文件函数声明亦为全局（HTML 内联 onclick 仍可用）。
 * ★ 加载顺序：必须在 app.js 之前（boot() 同步调用 bindLitAdmin）。
 * ═══════════════════════════════════════════════════════════════ */
// ══════════ AI检索管理中心（paperlit L1）══════════

let _litPage = 0;
const _LIT_PAGE_SIZE = 30;
let _litSearchAll = [];      // 当前检索的全部结果（客户端排序/分页）
let _litSearchShown = 0;     // 已显示条数
const _LIT_SEARCH_PAGE = 10; // 检索结果每页条数
let _litSearchSort = 'relevance';

function bindLitAdmin() {
  $('lit-admin-btn').addEventListener('click', openLitAdmin);
  $('lit-close').addEventListener('click', () => { $('lit-admin-modal').style.display = 'none'; });
  document.querySelectorAll('#lit-admin-modal .stab').forEach(btn => {
    btn.addEventListener('click', () => setLitTab(btn.dataset.stab));
  });
  $('lit-search-go').addEventListener('click', litDoSearch);
  $('lit-search-q').addEventListener('keydown', e => { if (e.key === 'Enter') litDoSearch(); });
  $('lit-sort-select').addEventListener('change', e => litSortResults(e.target.value));
  $('lit-load-more').addEventListener('click', litLoadMore);
  $('lit-papers-refresh').addEventListener('click', () => litLoadPapers(0));
  $('lit-papers-prev').addEventListener('click', () => { if (_litPage > 0) litLoadPapers(_litPage - 1); });
  $('lit-papers-next').addEventListener('click', () => litLoadPapers(_litPage + 1));
  $('lit-enrich-pending').addEventListener('click', (e) => guardBtn(e.currentTarget, litEnrichPending, '补全中…'));
  $('lit-export-wos-query').addEventListener('click', (e) => guardBtn(e.currentTarget, litExportWosQuery, '生成中…'));
  // 批次块为动态渲染，用事件委托绑定复制按钮（勿逐个 addEventListener）
  $('lit-wos-batches').onclick = (e) => {
    const btn = e.target.closest('.lit-wos-copy');
    if (!btn) return;
    const ta = btn.closest('.lit-wos-batch').querySelector('textarea');
    if (!ta) return;
    ta.select();
    ta.setSelectionRange(0, 99999);
    try {
      document.execCommand('copy');
      const old = btn.textContent;
      btn.textContent = '已复制';
      setTimeout(() => { btn.textContent = old; }, 1500);
    } catch (_) { alert('复制失败，请手动选中复制'); }
  };
  $('lit-rank-compute').addEventListener('click', (e) => guardBtn(e.currentTarget, litComputeRank, '计算中…'));
  $('lit-rank-refresh').addEventListener('click', litLoadTopPapers);
  $('lit-cluster-compute').addEventListener('click', (e) => guardBtn(e.currentTarget, litComputeClusters, '计算中…'));
  $('lit-topic-compile').addEventListener('click', (e) => guardBtn(e.currentTarget, litCompileTopics, '编译中…'));
  $('lit-bib-import').addEventListener('click', (e) => guardBtn(e.currentTarget, litImportBib, '导入中…'));
  $('lit-bib-dir-import').addEventListener('click', (e) => guardBtn(e.currentTarget, litImportBibDir, '导入中…'));
  $('lit-bib-upload').addEventListener('click', (e) => guardBtn(e.currentTarget, litUploadBib, '上传中…'));
  $('lit-settings-refresh').addEventListener('click', litLoadStatus);
  $('lit-vector-build').addEventListener('click', (e) => guardBtn(e.currentTarget, litBuildVector, '构建中…'));
  $('lit-cache-refresh').addEventListener('click', litLoadCacheStats);
  $('lit-cache-clear').addEventListener('click', litClearCache);
  $('lit-pipeline-run').addEventListener('click', (e) => guardBtn(e.currentTarget, litRunPipeline, '执行中…'));
  $('lit-normalize-run').addEventListener('click', (e) => guardBtn(e.currentTarget, litNormalizeJournals, '规范化中…'));
  $('lit-normalize-preview').addEventListener('click', litPreviewJournals);
  $('lit-attach-metrics').addEventListener('click', (e) => guardBtn(e.currentTarget, litAttachMetrics, '关联中…'));
  // 文献清洗
  $('lit-clean-year-enable').addEventListener('change', (e) => { $('lit-clean-year-min').disabled = !e.target.checked; });
  $('lit-clean-if-enable').addEventListener('change', (e) => { $('lit-clean-if-min').disabled = !e.target.checked; });
  $('lit-clean-quartile-enable').addEventListener('change', (e) => { $('lit-clean-quartile-keep').disabled = !e.target.checked; });
  $('lit-clean-citations-enable').addEventListener('change', (e) => { $('lit-clean-citations-min').disabled = !e.target.checked; });
  $('lit-clean-preview').addEventListener('click', (e) => guardBtn(e.currentTarget, litCleanPreview, '计算中…'));
  $('lit-clean-execute').addEventListener('click', (e) => guardBtn(e.currentTarget, litCleanExecute, '清洗中…'));
  $('lit-clean-recalc').addEventListener('click', (e) => guardBtn(e.currentTarget, litCleanRecalcCitations, '重算中…'));
  $('lit-config-load').addEventListener('click', litLoadConfig);
  $('lit-config-save').addEventListener('click', (e) => guardBtn(e.currentTarget, litSaveConfig, '保存中…'));
  // API 密钥显示/隐藏切换
  document.querySelectorAll('.lit-key-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const targetId = btn.getAttribute('data-target');
      const input = $(targetId);
      if (input.type === 'password') {
        input.type = 'text';
        btn.textContent = '🙈';
      } else {
        input.type = 'password';
        btn.textContent = '👁';
      }
    });
  });
}

async function litLoadConfig() {
  try {
    const cfg = await api('/api/lit/v1/config');
    $('lit-emb-key-status').textContent = cfg.has_embedding_key ? '✓ 已配置' : '未配置';
    $('lit-rer-key-status').textContent = cfg.has_reranker_key ? '✓ 已配置' : '未配置';
    $('lit-emb-model').value = cfg.embedding_model || 'BAAI/bge-m3';
    $('lit-rer-model').value = cfg.reranker_model || 'BAAI/bge-reranker-v2-m3';
    $('lit-config-msg').innerHTML = litOk('配置已加载');
  } catch (e) {
    $('lit-config-msg').innerHTML = litErr('加载失败: ' + e.message);
  }
}

async function litSaveConfig() {
  const embKey = $('lit-emb-key').value.trim();
  const rerKey = $('lit-rer-key').value.trim();
  const embModel = $('lit-emb-model').value.trim();
  const rerModel = $('lit-rer-model').value.trim();
  $('lit-config-msg').innerHTML = '<span class="muted">保存中…</span>';
  try {
    const body = {};
    if (embKey) body.embedding_api_key = embKey;
    if (rerKey) body.reranker_api_key = rerKey;
    if (embModel) body.embedding_model = embModel;
    if (rerModel) body.reranker_model = rerModel;
    const res = await api('/api/lit/v1/config', 'POST', body);
    if (res.env_ok === false && res.env_mismatch && res.env_mismatch.length) {
      $('lit-config-msg').innerHTML = litErr('.env 回读不一致: ' + res.env_mismatch.join(', '));
    } else {
      $('lit-config-msg').innerHTML = litOk('配置已保存到 .env');
    }
    // 用返回的脱敏 key 填充输入框（标准 UX：不暴露明文，但显示已配置状态）
    if (res.config) {
      $('lit-emb-key').value = res.config.embedding_api_key || '';
      $('lit-rer-key').value = res.config.reranker_api_key || '';
      $('lit-emb-key-status').textContent = res.config.has_embedding_key ? '✓ 已配置' : '未配置';
      $('lit-rer-key-status').textContent = res.config.has_reranker_key ? '✓ 已配置' : '未配置';
    }
  } catch (e) {
    $('lit-config-msg').innerHTML = litErr('保存失败: ' + e.message);
  }
}

async function openLitAdmin() {
  $('lit-admin-modal').style.display = 'flex';
  setLitTab('import');
  await litLoadStatus();
}

function setLitTab(tab) {
  document.querySelectorAll('#lit-admin-modal .stab').forEach(b =>
    b.classList.toggle('active', b.dataset.stab === tab));
  ['import', 'search', 'papers', 'graph', 'topics', 'settings'].forEach(p => {
    $('lit-' + p).style.display = p === tab ? '' : 'none';
  });
  if (tab === 'papers') litLoadPapers(_litPage);
  if (tab === 'graph') litLoadTopPapers();
  if (tab === 'topics') litLoadTopics();
  if (tab === 'search') litLoadCacheStats();
  if (tab === 'settings') { litLoadStatus(); litLoadConfig(); }
}

function litOk(msg) { return '<div class="kba-ok">' + escapeHtml(msg) + '</div>'; }
function litErr(msg) { return '<div class="kba-err">' + escapeHtml(msg) + '</div>'; }

async function litLoadStatus() {
  try {
    const s = await api('/api/lit/v1/status');
    $('lit-status').textContent = '· ' + s.paper_count + ' 篇';
    $('lit-s-papers').textContent = s.paper_count;
    $('lit-s-citations').textContent = s.citation_count;
    $('lit-s-unenriched').textContent = s.unenriched_count;
    $('lit-s-vectors').textContent = s.vector_index_size;
    const uc = $('lit-unenriched-count');
    if (uc) uc.textContent = s.unenriched_count > 0 ? ('待补全 ' + s.unenriched_count + ' 篇') : '✓ 已全部补全';
    const mp = $('lit-stat-mapped');
    if (mp) mp.textContent = s.journal_mapping_count || 0;
    const ic = $('lit-stat-ifcov');
    if (ic) ic.textContent = s.if_covered_count || 0;
    litRenderPipelineStatus(s);
  } catch (e) { $('lit-status').textContent = '· ' + e.message; }
}

function _litStepItem(label, state, detail) {
  const icon = state === 'done' ? '✅' : (state === 'partial' ? '🔶' : '⬜');
  const cls = 'lit-step lit-step-' + state;
  return '<div class="' + cls + '"><span class="lit-step-icon">' + icon + '</span>' +
    '<span class="lit-step-label">' + label + '</span>' +
    (detail ? '<span class="lit-step-detail">' + detail + '</span>' : '') + '</div>';
}

function litRenderPipelineStatus(s) {
  const box = $('lit-pipeline-status');
  if (!box) return;
  const pc = s.paper_count || 0;
  const items = [];
  // ① 导入
  items.push(_litStepItem('① 导入文献', pc > 0 ? 'done' : 'todo', pc > 0 ? (pc + ' 篇') : '未导入'));
  // ③ 补全元数据
  const un = s.unenriched_count || 0;
  let enrichState = 'todo', enrichDetail = '未补全';
  if (pc > 0 && un === 0) { enrichState = 'done'; enrichDetail = '已完成'; }
  else if (pc > 0) { enrichState = 'partial'; enrichDetail = '待补 ' + un + ' 篇'; }
  items.push(_litStepItem('③ 补全元数据', enrichState, enrichDetail));
  // ④ 期刊名规范化
  const jm = s.journal_mapping_count || 0;
  items.push(_litStepItem('④ 期刊名规范化', jm > 0 ? 'done' : 'todo', jm > 0 ? ('映射 ' + jm + ' 条') : '未建映射'));
  // ⑤ PaperRank
  const rc = s.ranked_count || 0;
  let rankState = 'todo', rankDetail = '未计算';
  if (rc > 0 && rc >= pc) { rankState = 'done'; rankDetail = '已排名 ' + rc + ' 篇'; }
  else if (rc > 0) { rankState = 'partial'; rankDetail = '已排名 ' + rc + '/' + pc; }
  items.push(_litStepItem('⑤ PaperRank', rankState, rankDetail));
  // ⑥ 向量索引
  const vi = s.vector_index_size || 0;
  let vecState = 'todo', vecDetail = '未构建';
  if (vi > 0 && vi >= pc) { vecState = 'done'; vecDetail = '已索引 ' + vi + ' 篇'; }
  else if (vi > 0) { vecState = 'partial'; vecDetail = '已索引 ' + vi + '/' + pc; }
  items.push(_litStepItem('⑥ 向量索引', vecState, vecDetail));
  // 主题编译（手动）
  const tc = s.topic_count || 0;
  items.push(_litStepItem('🏷️ 主题编译（手动）', tc > 0 ? 'done' : 'todo', tc > 0 ? (tc + ' 个主题') : '未编译'));
  box.innerHTML = items.join('');
}

function _litResultCard(r) {
  const ifStr = r.impact_factor ? 'IF: ' + r.impact_factor.toFixed(1) : '';
  const quartileStr = r.quartile ? r.quartile : '';
  const citedStr = r.times_cited ? '被引: ' + r.times_cited : '';
  const libCitedStr = r.library_citations ? '库内引用: ' + r.library_citations : '';
  const abstract = r.abstract || '';
  return '<div class="lit-result-item lit-result-wos">' +
    '<div class="lit-result-score-line">相关度: <span class="lit-score-red">' + (r.final_score ? r.final_score.toFixed(3) : 'N/A') + '</span></div>' +
    '<div class="lit-result-title">' + escapeHtml(r.title || '(无标题)') + '</div>' +
    '<div class="lit-result-meta">' +
      '<span class="lit-meta-journal">' + escapeHtml(r.journal || '') + '</span>' +
      (ifStr ? '<span class="lit-meta-if">' + ifStr + '</span>' : '') +
      (quartileStr ? '<span class="lit-meta-quartile lit-quartile-' + quartileStr + '">' + quartileStr + '</span>' : '') +
      (r.year ? '<span class="lit-meta-year">' + r.year + '</span>' : '') +
    '</div>' +
    '<div class="lit-result-authors">' + escapeHtml((r.authors || []).slice(0, 5).join(', ')) + ((r.authors || []).length > 5 ? ' et al.' : '') + '</div>' +
    (abstract ? '<div class="lit-result-abstract">' + escapeHtml(abstract) + '</div>' : '') +
    '<div class="lit-result-footer">' +
      '<span class="lit-doi">' + escapeHtml(r.doi) + '</span>' +
      '<span class="lit-result-citations">' +
        (citedStr ? '<span>' + citedStr + '</span>' : '') +
        (libCitedStr ? '<span>' + libCitedStr + '</span>' : '') +
      '</span>' +
    '</div>' +
  '</div>';
}

const _LIT_SORT_KEYS = {
  relevance: r => r.final_score || 0,
  citations: r => r.times_cited || 0,
  year: r => (r.year && /^\d{4}$/.test(String(r.year))) ? parseInt(r.year, 10) : 0,
  paper_rank: r => r.paper_rank || 0,
};

function litRenderSearchResults() {
  const slice = _litSearchAll.slice(0, _litSearchShown);
  $('lit-search-results').innerHTML = slice.map(_litResultCard).join('');
  $('lit-results-count').textContent = '共 ' + _litSearchAll.length + ' 篇，已显示 ' + slice.length + ' 篇';
  $('lit-loadmore-wrap').style.display = (_litSearchShown < _litSearchAll.length) ? '' : 'none';
}

async function litDoSearch() {
  const q = $('lit-search-q').value.trim();
  if (!q) { $('lit-search-msg').innerHTML = litErr('请输入检索词'); return; }
  $('lit-search-msg').innerHTML = '<span class="muted">检索中…</span>';
  $('lit-search-results').innerHTML = '';
  $('lit-results-toolbar').style.display = 'none';
  $('lit-loadmore-wrap').style.display = 'none';
  try {
    const results = await api('/api/lit/v1/search/cached?q=' + encodeURIComponent(q));
    if (!results.length) {
      _litSearchAll = []; _litSearchShown = 0;
      $('lit-search-msg').innerHTML = '<span class="muted">无结果</span>';
      return;
    }
    _litSearchAll = results;
    _litSearchSort = $('lit-sort-select').value || 'relevance';
    const key = _LIT_SORT_KEYS[_litSearchSort] || _LIT_SORT_KEYS.relevance;
    _litSearchAll.sort((a, b) => key(b) - key(a));
    _litSearchShown = Math.min(_LIT_SEARCH_PAGE, _litSearchAll.length);
    const fromCache = results[0] && results[0].match_source === 'cache';
    $('lit-search-msg').innerHTML = litOk('找到 ' + results.length + ' 条结果' + (fromCache ? '（缓存命中）' : ''));
    $('lit-results-toolbar').style.display = '';
    litRenderSearchResults();
  } catch (e) { $('lit-search-msg').innerHTML = litErr(e.message); }
}

function litSortResults(sortBy) {
  if (!_litSearchAll.length) return;
  _litSearchSort = sortBy;
  const key = _LIT_SORT_KEYS[sortBy] || _LIT_SORT_KEYS.relevance;
  _litSearchAll.sort((a, b) => key(b) - key(a));
  _litSearchShown = Math.min(_LIT_SEARCH_PAGE, _litSearchAll.length);
  litRenderSearchResults();
}

function litLoadMore() {
  if (_litSearchShown >= _litSearchAll.length) return;
  _litSearchShown = Math.min(_litSearchShown + _LIT_SEARCH_PAGE, _litSearchAll.length);
  litRenderSearchResults();
}

async function litLoadPapers(page) {
  _litPage = page;
  const offset = page * _LIT_PAGE_SIZE;
  $('lit-papers-msg').innerHTML = '<span class="muted">加载中…</span>';
  try {
    const r = await api('/api/lit/v1/papers?offset=' + offset + '&limit=' + _LIT_PAGE_SIZE);
    const items = r.items || r;
    $('lit-papers-count').textContent = '（共 ' + (r.total || items.length) + ' 篇）';
    $('lit-papers-page').textContent = '第 ' + (page + 1) + ' 页';
    if (!items.length) { $('lit-papers-msg').innerHTML = '<span class="muted">暂无文献</span>'; $('lit-papers-body').innerHTML = ''; return; }
    $('lit-papers-msg').innerHTML = '';
    $('lit-papers-body').innerHTML = items.map(p =>
      '<tr><td>' + escapeHtml(p.doi) + '</td>' +
      '<td>' + escapeHtml(shortTitle(p.title || '', 50)) + '</td>' +
      '<td>' + escapeHtml(p.journal || '') + '</td>' +
      '<td>' + escapeHtml(p.year || '') + '</td>' +
      '<td>' + (p.times_cited || 0) + '</td>' +
      '<td>' + escapeHtml(p.source_main || '') + '</td>' +
      '<td><button class="btn small" onclick="litEnrichOne(\'' + escapeHtml(p.doi) + '\')">补全</button></td></tr>'
    ).join('');
    try {
      const uc = await api('/api/lit/v1/enrich/unenriched-count');
      $('lit-unenriched-count').textContent = '待补全: ' + (uc.count || 0) + ' 篇';
    } catch (_) {}
  } catch (e) { $('lit-papers-msg').innerHTML = litErr(e.message); }
}

async function litEnrichOne(doi) {
  $('lit-papers-msg').innerHTML = '<span class="muted">补全 ' + escapeHtml(doi) + '…</span>';
  try {
    const r = await api('/api/lit/v1/enrich/one', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doi })
    });
    $('lit-papers-msg').innerHTML = litOk(r.ok ? '补全成功（' + (r.filled || 0) + ' 字段）' : '未能补全: ' + (r.reason || ''));
  } catch (e) { $('lit-papers-msg').innerHTML = litErr(e.message); }
}

async function litEnrichPending() {
  $('lit-enrich-msg').innerHTML = '<span class="muted">批量补全中…</span>';
  try {
    const r = await api('/api/lit/v1/enrich/pending', { method: 'POST' });
    $('lit-enrich-msg').innerHTML = litOk('补全完成：成功 ' + (r.enriched || 0) + '，失败 ' + (r.failed || 0));
    litLoadStatus();
  } catch (e) { $('lit-enrich-msg').innerHTML = litErr(e.message); }
}

async function litExportWosQuery() {
  const box = $('lit-wos-batches');
  const countSpan = $('lit-wos-count');
  const parsedBatch = parseInt($('lit-wos-batch-size').value, 10);
  const batchSize = Number.isNaN(parsedBatch) ? 1000 : parsedBatch;  // 用户值原样生效；≤0 = 不分批
  const pendingOnly = $('lit-wos-pending-only').checked;
  box.innerHTML = '';
  countSpan.textContent = '生成中…';
  try {
    const r = await api('/api/lit/v1/papers/dois?mode=' + (pendingOnly ? 'pending' : 'all'));
    const dois = r.dois || [];
    if (!dois.length) {
      countSpan.textContent = pendingOnly ? '无待补全 / 非 WoS 来源文献' : '文献库无 DOI';
      return;
    }
    const batches = [];
    if (batchSize > 0) {
      for (let i = 0; i < dois.length; i += batchSize) batches.push(dois.slice(i, i + batchSize));
    } else {
      batches.push(dois);
    }
    const multi = batches.length > 1;
    const taStyle = 'width:100%;min-height:70px;padding:8px;border:1px solid var(--border);' +
      'border-radius:6px;font-family:monospace;font-size:12px;background:var(--bg);' +
      'color:var(--text);resize:vertical';
    box.innerHTML = batches.map((b, i) =>
      '<div class="lit-wos-batch" style="margin-top:8px">' +
        '<div style="font-size:12px;color:var(--muted);margin-bottom:4px">' +
          (multi ? '第 ' + (i + 1) + '/' + batches.length + ' 批 · ' : '') +
          b.length + ' 个 DOI' +
          '<button class="btn small lit-wos-copy" style="margin-left:8px">' +
            (multi ? '复制本批' : '复制检索式') + '</button>' +
        '</div>' +
        '<textarea readonly style="' + taStyle + '">' +
          escapeHtml('DO=(' + b.join(' OR ') + ')') + '</textarea>' +
      '</div>'
    ).join('');
    countSpan.textContent = '共 ' + dois.length + ' 个 DOI' +
      (multi ? '，分 ' + batches.length + ' 批（每批 ' + batchSize + ' 个）'
             : (batchSize <= 0 ? '（不分批）' : '（未超每批上限，一次导出全部）'));
  } catch (e) {
    countSpan.textContent = '生成失败：' + e.message;
  }
}

async function litComputeRank() {
  $('lit-import-rank-msg').innerHTML = '<span class="muted">计算 PaperRank…</span>';
  try {
    const r = await api('/api/lit/v1/graph/rank', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ damping: 0.85 })
    });
    $('lit-import-rank-msg').innerHTML = litOk('PaperRank 计算完成：' + (r.computed || 0) + ' 篇，' + (r.iterations || 0) + ' 次迭代');
    litLoadStatus();
    litLoadTopPapers();
  } catch (e) { $('lit-import-rank-msg').innerHTML = litErr(e.message); }
}

async function litLoadTopPapers() {
  try {
    const top = await api('/api/lit/v1/graph/top?limit=20');
    $('lit-rank-body').innerHTML = (top.length ? top : []).map((p, i) =>
      '<tr><td>' + (i + 1) + '</td>' +
      '<td>' + escapeHtml(p.doi || '') + '</td>' +
      '<td>' + escapeHtml(shortTitle(p.title || '', 50)) + '</td>' +
      '<td>' + (p.paper_rank ? p.paper_rank.toFixed(4) : '-') + '</td>' +
      '<td>' + (p.times_cited || 0) + '</td></tr>'
    ).join('');
  } catch (e) { $('lit-rank-msg').innerHTML = litErr(e.message); }
}

async function litComputeClusters() {
  $('lit-cluster-msg').innerHTML = '<span class="muted">计算聚类…</span>';
  try {
    const r = await api('/api/lit/v1/graph/clusters', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ min_cocitations: 2 })
    });
    $('lit-cluster-msg').innerHTML = litOk('聚类完成：' + (r.clusters || 0) + ' 个聚类（' + (r.papers || 0) + ' 篇参与）');
    if (r.cluster_list) {
      $('lit-cluster-list').innerHTML = r.cluster_list.map(c =>
        '<div class="lit-cluster-chip" onclick="litShowCluster(' + c.id + ')">' +
        '聚类 ' + c.id + '（' + c.size + ' 篇）</div>'
      ).join('');
    }
  } catch (e) { $('lit-cluster-msg').innerHTML = litErr(e.message); }
}

async function litShowCluster(id) {
  document.querySelectorAll('.lit-cluster-chip').forEach(c => c.classList.remove('active'));
  event.target.classList.add('active');
  try {
    const members = await api('/api/lit/v1/graph/cluster?cluster_id=' + id);
    $('lit-cluster-msg').innerHTML = litOk('聚类 ' + id + '：' + members.length + ' 篇');
  } catch (e) { $('lit-cluster-msg').innerHTML = litErr(e.message); }
}

async function litCompileTopics() {
  const field = $('lit-topic-field').value;
  const minPapers = parseInt($('lit-topic-min').value) || 2;
  $('lit-topic-msg').innerHTML = '<span class="muted">编译主题…</span>';
  try {
    const r = await api('/api/lit/v1/topics/compile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ field, min_papers: minPapers })
    });
    $('lit-topic-msg').innerHTML = litOk('编译完成：' + (r.topics || 0) + ' 个主题');
    litLoadTopics();
  } catch (e) { $('lit-topic-msg').innerHTML = litErr(e.message); }
}

async function litLoadTopics() {
  try {
    const topics = await api('/api/lit/v1/topics?limit=50');
    $('lit-topics-body').innerHTML = (topics || []).map(t =>
      '<tr><td>' + escapeHtml(t.slug || t.name || '') + '</td>' +
      '<td>' + (t.paper_count || 0) + '</td>' +
      '<td><button class="btn small" onclick="litShowTopic(\'' + escapeHtml(t.slug || '') + '\')">详情</button></td></tr>'
    ).join('');
  } catch (e) { $('lit-topic-msg').innerHTML = litErr(e.message); }
}

async function litShowTopic(slug) {
  try {
    const t = await api('/api/lit/v1/topics/topic?slug=' + encodeURIComponent(slug));
    $('lit-topic-detail').innerHTML = '<h4>' + escapeHtml(t.slug || t.name || '') +
      '（' + (t.paper_count || 0) + ' 篇）</h4>' +
      '<div>' + (t.dois || []).map(d => '<span class="lit-cluster-chip">' + escapeHtml(d) + '</span>').join('') + '</div>';
  } catch (e) { $('lit-topic-msg').innerHTML = litErr(e.message); }
}

let _importPollTimer = null;

function _showImportProgress(current, total, phase, doi) {
  const box = $('lit-import-progress');
  const bar = $('lit-import-progress-bar');
  const text = $('lit-import-progress-text');
  const detail = $('lit-import-progress-detail');
  box.style.display = 'block';
  if (total > 0) {
    const pct = Math.round(current / total * 100);
    bar.style.width = pct + '%';
    text.textContent = current + '/' + total + ' (' + pct + '%)';
  } else {
    bar.style.width = '0%';
    text.textContent = phase || '处理中…';
  }
  detail.textContent = [phase, doi ? '当前: ' + doi : ''].filter(Boolean).join(' · ');
}

function _hideImportProgress() {
  $('lit-import-progress').style.display = 'none';
}

function _pollImportProgress(onDone) {
  if (_importPollTimer) clearInterval(_importPollTimer);
  _importPollTimer = setInterval(async () => {
    try {
      const p = await api('/api/lit/v1/ingest/progress');
      if (p.status === 'running') {
        _showImportProgress(p.current, p.total, p.phase, p.doi);
      } else if (p.status === 'done') {
        clearInterval(_importPollTimer);
        _importPollTimer = null;
        _showImportProgress(p.total, p.total, '完成', '');
        const r = p.result || {};
        let msg = '导入完成：' + (r.new_papers || 0) + ' 篇主文献';
        if (r.new_refs) msg += '，' + r.new_refs + ' 篇参考文献';
        if (r.duplicates_skipped) msg += '，跳过重复 ' + r.duplicates_skipped + ' 篇';
        $('lit-import-msg').innerHTML = litOk(msg);
        litLoadStatus();
        setTimeout(_hideImportProgress, 3000);
        if (onDone) onDone();
      } else if (p.status === 'error') {
        clearInterval(_importPollTimer);
        _importPollTimer = null;
        $('lit-import-msg').innerHTML = litErr('导入失败：' + (p.error || '未知错误'));
        _hideImportProgress();
        if (onDone) onDone();
      }
    } catch (e) {
      clearInterval(_importPollTimer);
      _importPollTimer = null;
      $('lit-import-msg').innerHTML = litErr('查询进度失败：' + e.message);
      _hideImportProgress();
      if (onDone) onDone();
    }
  }, 500);
}

async function litImportBib() {
  const path = $('lit-bib-path').value.trim();
  if (!path) { $('lit-import-msg').innerHTML = litErr('请输入 bib 文件路径'); return; }
  $('lit-import-msg').innerHTML = '';
  _showImportProgress(0, 0, '启动导入', '');
  try {
    const body = { path, import_refs: true };
    await api('/api/lit/v1/ingest/bib', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    _pollImportProgress();
  } catch (e) {
    $('lit-import-msg').innerHTML = litErr(e.message);
    _hideImportProgress();
  }
}

async function litImportBibDir() {
  const path = $('lit-bib-path').value.trim();
  if (!path) { $('lit-import-msg').innerHTML = litErr('请输入目录路径'); return; }
  $('lit-import-msg').innerHTML = '<span class="muted">导入目录…</span>';
  try {
    const body = { path, import_refs: true };
    const r = await api('/api/lit/v1/ingest/bib-dir', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    let msg = '目录导入完成：' + (r.new_papers || 0) + ' 篇主文献';
    if (r.new_refs) msg += '，' + r.new_refs + ' 篇参考文献';
    if (r.duplicates_skipped) msg += '，跳过重复 ' + r.duplicates_skipped + ' 篇';
    if (r.filtered_by_year) msg += '，过滤 ' + r.filtered_by_year + ' 篇（年份）';
    $('lit-import-msg').innerHTML = litOk(msg);
    litLoadStatus();
  } catch (e) { $('lit-import-msg').innerHTML = litErr(e.message); }
}

async function litUploadBib() {
  const fileInput = $('lit-bib-file');
  if (!fileInput.files.length) { $('lit-import-msg').innerHTML = litErr('请选择 bib 文件'); return; }
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  $('lit-import-msg').innerHTML = '';
  _showImportProgress(0, 0, '上传中', '');
  try {
    await api('/api/lit/v1/ingest/upload', { method: 'POST', body: fd });
    _showImportProgress(0, 0, '解析导入中', '');
    _pollImportProgress();
  } catch (e) {
    $('lit-import-msg').innerHTML = litErr(e.message);
    _hideImportProgress();
  }
}

async function litBuildVector() {
  $('lit-vector-msg').innerHTML = '<span class="muted">构建向量索引（可能需要几分钟）…</span>';
  try {
    const r = await api('/api/lit/v1/vector/build', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ batch_size: 32 })
    });
    $('lit-vector-msg').innerHTML = litOk('索引构建完成：' + (r.indexed || 0) + ' 篇，维度 ' + (r.dim || '?'));
    litLoadStatus();
  } catch (e) { $('lit-vector-msg').innerHTML = litErr(e.message); }
}

async function litLoadCacheStats() {
  try {
    const s = await api('/api/lit/v1/cache/stats');
    $('lit-cache-info').textContent = '已缓存检索词: ' + (s.total_entries || 0) + ' 条';
  } catch (e) { $('lit-cache-info').textContent = '获取缓存统计失败: ' + e.message; }
}

async function litClearCache() {
  try {
    const r = await api('/api/lit/v1/cache/clear', { method: 'POST' });
    $('lit-cache-info').textContent = '已清空 ' + (r.cleared || 0) + ' 条缓存';
  } catch (e) { $('lit-cache-info').textContent = '清空失败: ' + e.message; }
}

const _LIT_STEP_LABELS = {
  enrich_pending: '元数据补全',
  normalize_journals: '期刊名规范化',
  attach_journal_metrics: '期刊指标关联',
  compute_paper_rank: 'PaperRank 计算',
  build_vector_index: '向量索引构建',
};

async function litRunPipeline() {
  const box = $('lit-pipeline-progress');
  box.innerHTML = '<div class="muted">正在执行全流程，请耐心等待…</div>';
  try {
    const r = await api('/api/lit/v1/pipeline', { method: 'POST' });
    let html = '';
    for (const s of (r.steps || [])) {
      const label = _LIT_STEP_LABELS[s.step] || s.step;
      const icon = s.status === 'ok' ? '✅' : '❌';
      html += '<div>' + icon + ' <b>' + label + '</b>（' + s.seconds + 's）';
      if (s.status === 'ok') {
        html += ' <span class="muted">' + _summarizeStepResult(s.step, s.result) + '</span>';
      } else {
        html += ' <span style="color:#c33">' + (s.error || '未知错误') + '</span>';
      }
      html += '</div>';
    }
    if (r.final_status) {
      const fs = r.final_status;
      html += '<div style="margin-top:6px" class="muted">最终状态：文献 ' +
        (fs.paper_count || 0) + ' 篇，引用 ' + (fs.citation_count || 0) +
        '，向量索引 ' + (fs.vector_index_size || 0) + '</div>';
    }
    box.innerHTML = html;
    litLoadStatus();
  } catch (e) {
    box.innerHTML = '<div style="color:#c33">管线执行失败: ' + e.message + '</div>';
  }
}

function _summarizeStepResult(step, result) {
  if (!result || typeof result !== 'object') return '';
  if (step === 'enrich_pending') return '补全 ' + (result.enriched || 0) + ' 篇';
  if (step === 'normalize_journals') return '规范化 ' + (result.updated || 0) + ' 条';
  if (step === 'attach_journal_metrics') return '匹配 ' + (result.matched || 0) + ' 期刊 / 更新 ' + (result.updated_rows || 0) + ' 条';
  if (step === 'compute_paper_rank') return '计算 ' + (result.ranked || 0) + ' 篇';
  if (step === 'build_vector_index') return '索引 ' + (result.indexed || 0) + ' 篇';
  return JSON.stringify(result).slice(0, 80);
}

let _normalizePollTimer = null;

async function litNormalizeJournals() {
  const box = $('lit-normalize-msg');
  box.innerHTML = '<span class="muted">正在启动规范化任务…</span>';
  try {
    await api('/api/lit/v1/journals/normalize', { method: 'POST' });
    _pollNormalizeProgress();
  } catch (e) {
    box.innerHTML = '<div style="color:#c33">启动失败: ' + e.message + '</div>';
  }
}

function _pollNormalizeProgress() {
  if (_normalizePollTimer) clearInterval(_normalizePollTimer);
  const t0 = Date.now();
  _normalizePollTimer = setInterval(async () => {
    const box = $('lit-normalize-msg');
    try {
      const p = await api('/api/lit/v1/journals/normalize-progress');
      if (p.status === 'running') {
        const secs = Math.round((Date.now() - t0) / 1000);
        const pct = p.total ? Math.round(p.current / p.total * 100) : 0;
        const eta = p.current > 0 ? Math.round(secs / p.current * (p.total - p.current)) : null;
        box.innerHTML =
          '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">' +
            '<div style="flex:1;height:8px;background:var(--bg);border-radius:4px;overflow:hidden;border:1px solid var(--border)">' +
              '<div style="height:100%;width:' + pct + '%;background:var(--primary,#4a90d9);transition:width .3s"></div></div>' +
            '<span class="muted" style="font-size:12px;min-width:110px;text-align:right">' +
              p.current + '/' + p.total + ' (' + pct + '%)</span></div>' +
          '<div class="muted" style="font-size:11px">已耗时 ' + secs + 's' +
            (eta != null ? ' · 预计剩余 ' + eta + 's' : '') +
            ' · 当前解析: ' + escapeHtml(p.journal || '-') + '（已有映射自动跳过）</div>';
      } else if (p.status === 'done') {
        clearInterval(_normalizePollTimer);
        _normalizePollTimer = null;
        const r = p.result || {};
        box.innerHTML = litOk('规范化完成：唯一期刊 ' + (r.unique || 0) + '，解析 ' + (r.resolved || 0) +
          '，更新记录 ' + (r.updated || 0) + '，失败 ' + (r.failed || 0)) +
          '<div style="margin-top:6px;font-size:12px"><b>下一步</b>：点下方「关联期刊指标（写入 IF / 分区）」（秒级），完成后在检索结果卡验证 IF/分区。</div>';
        litLoadStatus();
      } else if (p.status === 'error') {
        clearInterval(_normalizePollTimer);
        _normalizePollTimer = null;
        box.innerHTML = '<div style="color:#c33">规范化失败: ' + escapeHtml(p.error || '未知错误') + '</div>';
      }
    } catch (e) {
      clearInterval(_normalizePollTimer);
      _normalizePollTimer = null;
      box.innerHTML = '<div style="color:#c33">查询进度失败: ' + e.message + '</div>';
    }
  }, 500);
}

async function litAttachMetrics() {
  const box = $('lit-attach-msg');
  box.innerHTML = '<span class="muted">正在关联期刊指标（JCR → 影响因子/分区）…</span>';
  try {
    const r = await api('/api/lit/v1/cleaning/attach-metrics', { method: 'POST' });
    box.innerHTML = litOk('关联完成：匹配 ' + r.matched + '/' + r.total +
      ' 种期刊，更新 ' + (r.updated_rows || 0) + ' 条文献的 IF/分区') +
      '<div style="margin-top:6px;font-size:12px">验证：检索结果卡 IF/分区徽标、清洗预览样本 IF 列。' +
      '未匹配期刊多为缩写/会议名——先跑第 1 步「执行规范化」再重跑本步可提升覆盖。</div>';
    litLoadStatus();
  } catch (e) {
    box.innerHTML = litErr('关联失败: ' + e.message);
  }
}

async function litPreviewJournals() {
  const box = $('lit-normalize-msg');
  box.innerHTML = '<div class="muted">正在加载期刊列表…</div>';
  try {
    const journals = await api('/api/lit/v1/journals/unique');
    let html = '<div>共 ' + journals.length + ' 种期刊（按文献数降序，前 50）</div>';
    html += '<div style="max-height:300px;overflow:auto;margin-top:6px">';
    for (const j of journals.slice(0, 50)) {
      html += '<div><span class="muted">[' + j.count + '篇]</span> ' + j.journal + '</div>';
    }
    if (journals.length > 50) {
      html += '<div class="muted">… 还有 ' + (journals.length - 50) + ' 种</div>';
    }
    html += '</div>';
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = '<div style="color:#c33">加载失败: ' + e.message + '</div>';
  }
}

// ================================================================ 文献清洗

function _getCleaningRules() {
  const rules = {};
  if ($('lit-clean-year-enable').checked) {
    rules.year = { min: parseInt($('lit-clean-year-min').value) || 2015 };
  }
  if ($('lit-clean-if-enable').checked) {
    rules.impact_factor = { min: parseFloat($('lit-clean-if-min').value) || 3.0 };
  }
  if ($('lit-clean-quartile-enable').checked) {
    const sel = $('lit-clean-quartile-keep');
    const keep = Array.from(sel.selectedOptions).map(o => o.value);
    if (keep.length > 0) rules.quartile = { keep };
  }
  if ($('lit-clean-citations-enable').checked) {
    rules.library_citations = { min: parseInt($('lit-clean-citations-min').value) || 2 };
  }
  if ($('lit-clean-nonwos-enable').checked) {
    rules.non_wos_record = true;
  }
  if ($('lit-clean-include-mains').checked) {
    rules.include_mains = true;
  }
  return rules;
}

async function litCleanRecalcCitations() {
  const box = $('lit-clean-msg');
  box.innerHTML = '<div class="muted">正在重算库内引用…</div>';
  try {
    const r2 = await api('/api/lit/v1/cleaning/compute-citations', { method: 'POST' });
    box.innerHTML = '<div>✅ 库内引用重算完成</div>' +
      '<div class="muted">' + r2.with_citations + ' 篇有引用。' +
      'IF/分区不受清洗影响，如需更新请到 ④「关联期刊指标」。</div>';
  } catch (e) {
    box.innerHTML = '<div style="color:#c33">重算失败: ' + e.message + '</div>';
  }
}

let _litLastPreview = null;   // {remove, rulesJson}：确认清洗前核对预览是否过期

async function litCleanPreview() {
  const box = $('lit-clean-msg');
  const rules = _getCleaningRules();
  if (Object.keys(rules).length === 0) {
    box.innerHTML = '<div style="color:#c33">请至少启用一条清洗规则</div>';
    return;
  }
  box.innerHTML = '<div class="muted">正在计算…</div>';
  try {
    const r = await api('/api/lit/v1/cleaning/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rules),
    });
    let html = '<div>📊 清洗预览</div>';
    html += '<div style="margin:6px 0">总文献: <b>' + r.total + '</b>，待筛除: <b style="color:#c33">' +
            r.to_remove + '</b>，保留: <b style="color:#3a3">' + r.to_keep + '</b> (' +
            (100 * r.to_keep / r.total).toFixed(1) + '%)</div>';
    if (r.breakdown && Object.keys(r.breakdown).length > 0) {
      html += '<div class="muted">分项: ';
      const parts = [];
      if (r.breakdown.year_filter) parts.push('年份<' + (rules.year?.min || '?') + ': ' + r.breakdown.year_filter);
      if (r.breakdown.if_filter) parts.push('IF<' + (rules.impact_factor?.min || '?') + ': ' + r.breakdown.if_filter);
      if (r.breakdown.quartile_filter) parts.push('非' + (rules.quartile?.keep?.join('/') || '?') + ': ' + r.breakdown.quartile_filter);
      if (r.breakdown.citation_filter) parts.push('引用<' + (rules.library_citations?.min || '?') + ': ' + r.breakdown.citation_filter);
      if (r.breakdown.non_wos_filter) parts.push('非WoS记录: ' + r.breakdown.non_wos_filter);
      html += parts.join('，');
      html += '</div>';
    }
    if (r.samples && r.samples.length > 0) {
      html += '<details style="margin-top:6px"><summary>查看样本（前 ' + r.samples.length + ' 条）</summary>';
      html += '<div style="max-height:200px;overflow:auto;margin-top:4px;font-size:11.5px">';
      for (const s of r.samples) {
        html += '<div>' + s.year + ' | ' + (s.journal || '').slice(0, 30) +
                ' | IF=' + (s.impact_factor || 0).toFixed(1) +
                ' | ' + (s.quartile || '-') +
                ' | 引用=' + (s.library_citations || 0) +
                ' | ' + escapeHtml(s.source_main || '-') +
                (s.abstract_missing ? ' | 缺摘要' : '') + '</div>';
      }
      html += '</div></details>';
    }
    box.innerHTML = html;
    _litLastPreview = { remove: r.to_remove, rulesJson: JSON.stringify(rules) };
    $('lit-clean-execute').disabled = false;
  } catch (e) {
    box.innerHTML = '<div style="color:#c33">预览失败: ' + e.message + '</div>';
  }
}

async function litCleanExecute() {
  const rules = _getCleaningRules();
  if (Object.keys(rules).length === 0) {
    $('lit-clean-msg').innerHTML = '<div style="color:#c33">请至少启用一条清洗规则</div>';
    return;
  }
  const curJson = JSON.stringify(rules);
  if (!_litLastPreview || _litLastPreview.rulesJson !== curJson) {
    $('lit-clean-msg').innerHTML = '<div style="color:#c33">规则自上次预览后已变化，请先重新「计算预览」核对数量</div>';
    return;
  }
  const n = _litLastPreview.remove;
  const scope = rules.include_mains ? '含主文献（危险）' : '仅参考文献';
  const ok = await askConfirm(
    '将删除命中规则的 ' + n + ' 条文献（范围：' + scope + '），不可撤销。数量与预览一致。',
    { title: '清洗确认', okText: '确认删除 ' + n + ' 条' });
  if (!ok) return;
  const box = $('lit-clean-msg');
  box.innerHTML = '<div class="muted">正在清洗…</div>';
  try {
    const r = await api('/api/lit/v1/cleaning/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...rules, mode: 'delete' }),
    });
    box.innerHTML = '<div>✅ 清洗完成</div>' +
      '<div class="muted">已删除: ' + r.removed + ' 篇，剩余: ' + r.remaining + ' 篇</div>';
    $('lit-clean-execute').disabled = true;
    litLoadStatus();
  } catch (e) {
    box.innerHTML = '<div style="color:#c33">清洗失败: ' + e.message + '</div>';
  }
}
