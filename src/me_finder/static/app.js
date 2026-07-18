/* ═══════════════════════════════════════════════════════════════
   App State
   ═══════════════════════════════════════════════════════════════ */
let currentPage = 'search';
let currentMode = 'auto';
let searchResults = [];
let selectedIndex = -1;
let citationStyle = localStorage.getItem('meFinderCitationStyle') || 'chinese';
let searchSourceType = 'all';
let searchLimit = 10;
let searchDocumentId = '';
let searchSourceFiles = [];
let searchVolumes = [];
let searchDocumentsLoaded = false;

let libSources = [];
let libVolumes = [];
let libWorks = [];
let libStats = null;
let libLoaded = false;
let libTypeFilter = 'all';
let libLangFilter = 'all';
let libStatusFilter = 'all';
let libSelectedId = null;
let libViewMode = localStorage.getItem('meFinderLibraryView') === 'grid' ? 'grid' : 'list';
let libSortField = ['imported_at','title','author','modified_at','source_type','status'].indexOf(localStorage.getItem('meFinderLibrarySortField')) >= 0 ? localStorage.getItem('meFinderLibrarySortField') : 'imported_at';
let libSortDirection = localStorage.getItem('meFinderLibrarySortDirection') === 'asc' ? 'asc' : 'desc';

let calSegments = [];
let calSelectedDoc = null;
let calSelectedSourceId = null;
let calAutoResult = null;
let calTransientStatus = {};
let removeDocumentTarget = null;
let removeSecondStage = false;
let mineruConfigLoaded = false;
let preferencesLoaded = false;
let currentTheme = document.documentElement.dataset.theme || 'frost-blue';

/* ═══ Navigation ═══ */
function navigateTo(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.sidebar-item').forEach(a => a.classList.remove('active'));
  const target = document.getElementById('page-' + page);
  if (target) target.classList.add('active');
  const link = document.querySelector('.sidebar-item[data-page="' + page + '"]');
  if (link) link.classList.add('active');
  if (page === 'library' && !libLoaded) loadLibrary();
  if (page === 'settings') {
    if (!preferencesLoaded) loadPreferences();
    if (!mineruConfigLoaded) loadMineruConfig();
  }
}

/* ═══ Mode segmented control ═══ */
function setMode(btn) {
  currentMode = btn.dataset.mode;
  document.querySelectorAll('#mode-control .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

/* ═══ Search filters ═══ */
function rerunSearchAfterFilterChange() {
  var query = document.getElementById('query').value.trim();
  var status = document.getElementById('results-status');
  if (query && status && status.style.display !== 'none') runSearch();
}

function setSearchSourceType(sourceType) {
  searchSourceType = ['all','word','pdf'].indexOf(sourceType) >= 0 ? sourceType : 'all';
  document.querySelectorAll('#source-type-control .source-type-btn').forEach(function(button) {
    button.classList.toggle('active', button.dataset.source === searchSourceType);
  });
  if (searchDocumentId) {
    var selected = searchSourceFiles.find(function(item) { return item.source_file_id === searchDocumentId; });
    if (selected && searchSourceType !== 'all' && selected.source_type !== searchSourceType) searchDocumentId = '';
  }
  updateSearchDocumentLabel();
  renderSearchDocumentOptions();
  rerunSearchAfterFilterChange();
}

function closeAppSelects(exceptId) {
  document.querySelectorAll('.app-select').forEach(function(select) {
    if (select.id === exceptId) return;
    select.classList.remove('is-open');
    var trigger = select.querySelector('.app-select-trigger');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  });
}

function closeSearchSelects(exceptId) { closeAppSelects(exceptId); }

async function toggleAppSelect(event, selectId) {
  event.stopPropagation();
  var select = document.getElementById(selectId);
  if (!select) return;
  var shouldOpen = !select.classList.contains('is-open');
  closeAppSelects(selectId);
  select.classList.toggle('is-open', shouldOpen);
  var trigger = select.querySelector('.app-select-trigger');
  if (trigger) trigger.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
  if (shouldOpen && selectId === 'document-select') {
    await ensureSearchDocuments();
    renderSearchDocumentOptions();
    var input = document.getElementById('document-filter-query');
    if (input) { input.value = ''; requestAnimationFrame(function() { input.focus(); }); }
  }
}

async function toggleSearchSelect(event, selectId) { return toggleAppSelect(event, selectId); }

function setSearchLimit(event, limit) {
  event.stopPropagation();
  searchLimit = limit === 'all' ? 'all' : Math.max(1, Math.min(Number(limit) || 10, 200));
  document.getElementById('limit-select-label').textContent = searchLimit === 'all' ? '全部' : searchLimit + ' 条';
  document.querySelectorAll('#limit-options .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', String(option.dataset.value) === String(searchLimit));
  });
  closeAppSelects();
  rerunSearchAfterFilterChange();
}

async function ensureSearchDocuments(force) {
  if (searchDocumentsLoaded && !force) return;
  var options = document.getElementById('document-options');
  if (options) options.innerHTML = '<div class="document-options-empty">正在读取文献库…</div>';
  try {
    var response = await fetch('/api/sources');
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '读取失败');
    searchSourceFiles = data.source_files || [];
    searchVolumes = data.volumes || [];
    searchDocumentsLoaded = true;
  } catch (error) {
    searchDocumentsLoaded = false;
    if (options) options.innerHTML = '<div class="document-options-empty">文献列表读取失败</div>';
  }
}

function searchDocumentView(source) {
  var volume = searchVolumes.find(function(item) { return item.source_file_id === source.source_file_id; });
  var bib = source.bibliographic_metadata || {};
  var title = bib.title || source.title || (volume && volume.display_title) || source.display_title || source.file_name || source.source_file_id;
  var author = bib.author || source.author || '';
  return {title:title, author:author, sourceType:source.source_type === 'pdf' ? 'PDF' : 'Word'};
}

function renderSearchDocumentOptions() {
  var options = document.getElementById('document-options');
  if (!options) return;
  if (!searchDocumentsLoaded) {
    options.innerHTML = '<div class="document-options-empty">打开菜单后读取文献列表</div>';
    return;
  }
  var queryInput = document.getElementById('document-filter-query');
  var query = String(queryInput ? queryInput.value : '').trim().toLowerCase().replace(/\s+/g, '');
  var sources = searchSourceFiles.filter(function(source) {
    if (searchSourceType !== 'all' && source.source_type !== searchSourceType) return false;
    var view = searchDocumentView(source);
    var haystack = [view.title, view.author, source.file_name].join('|').toLowerCase().replace(/\s+/g, '');
    return !query || haystack.indexOf(query) >= 0;
  }).sort(function(a, b) {
    return calPinyinCollator.compare(searchDocumentView(a).title, searchDocumentView(b).title);
  });
  var check = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 10 3 3 7-7"/></svg>';
  var allOption = '<button class="app-select-option' + (!searchDocumentId ? ' is-selected' : '') + '" type="button" data-value="" onclick="selectSearchDocument(event,this.dataset.value)"><span>全部文献</span>' + (!searchDocumentId ? check : '') + '</button>';
  if (!sources.length) {
    options.innerHTML = allOption + '<div class="document-options-empty">没有符合条件的文献</div>';
    return;
  }
  options.innerHTML = allOption + sources.map(function(source) {
    var view = searchDocumentView(source);
    var selected = source.source_file_id === searchDocumentId;
    return '<button class="app-select-option' + (selected ? ' is-selected' : '') + '" type="button" data-value="' + esc(source.source_file_id) + '" onclick="selectSearchDocument(event,this.dataset.value)"><span class="document-option-main"><span class="document-option-title">' + esc(view.title) + '</span><span class="document-option-meta">' + esc([view.sourceType, view.author].filter(Boolean).join(' · ')) + '</span></span>' + (selected ? check : '') + '</button>';
  }).join('');
}

function selectSearchDocument(event, sourceId) {
  event.stopPropagation();
  searchDocumentId = sourceId || '';
  updateSearchDocumentLabel();
  closeSearchSelects();
  rerunSearchAfterFilterChange();
}

function updateSearchDocumentLabel() {
  var label = document.getElementById('document-select-label');
  if (!label) return;
  var source = searchSourceFiles.find(function(item) { return item.source_file_id === searchDocumentId; });
  label.textContent = source ? searchDocumentView(source).title : '全部文献';
  label.title = source ? searchDocumentView(source).title : '';
}

/* ═══ Search ═══ */
async function runSearch() {
  const query = document.getElementById('query').value.trim();
  if (!query) return;
  const statusEl = document.getElementById('results-status');
  const listEl = document.getElementById('results-list');
  statusEl.style.display = 'block';
  statusEl.textContent = '检索中…';
  listEl.innerHTML = '';
  selectedIndex = -1;
  showEmptyDetail();

  try {
    const resp = await fetch('/api/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, mode: currentMode, limit: searchLimit, source_type: searchSourceType, source_file_id: searchDocumentId || null})
    });
    const data = await resp.json();
    searchResults = data.results || [];
    statusEl.textContent = '找到 ' + data.total + ' 条候选，显示 ' + searchResults.length + ' 条';

    if (searchResults.length === 0) {
      listEl.innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">未找到匹配结果</div><div class="empty-state-hint">尝试更短的引文或切换为模糊检索</div></div>';
      return;
    }

    listEl.innerHTML = searchResults.map((item, i) => resultRowHTML(item, i)).join('');
    selectResult(0);
  } catch (err) {
    statusEl.textContent = '检索失败：' + err.message;
  }
}

function resultRowHTML(item, index) {
  const score = Math.round(item.match_score * 100);
  const typeLabel = matchTypeLabel(item.match_type);
  const title = esc(item.document_title || item.work_title || item.volume_display || '');
  const author = item.author_label ? esc(item.author_label) : '';
  const vol = item.volume_display ? esc(item.volume_display) : '';
  const page = esc(formatCitationPageLabel(item));
  const sourceIcon = item.source_type === 'pdf' ? 'PDF' : 'Word';
  const snippet = item.highlighted_html ? truncateHTML(item.highlighted_html, 100) : esc(truncate(item.paragraph_text || '', 100));
  return '<div class="result-row" data-index="' + index + '" onclick="selectResult(' + index + ')">'
    + '<div class="result-row-head">'
    + '<span class="result-score">' + score + '%</span>'
    + '<span class="result-match-type">' + typeLabel + '</span>'
    + '<span class="result-title">' + title + '</span>'
    + '</div>'
    + '<div class="result-meta">'
    + (author ? '<span>' + author + '</span>' : '')
    + (vol ? '<span>' + vol + '</span>' : '')
    + '<span>' + page + '</span>'
    + '<span>' + sourceIcon + '</span>'
    + '</div>'
    + '<div class="result-snippet">' + snippet + '</div>'
    + '</div>';
}

function selectResult(index) {
  if (index < 0 || index >= searchResults.length) return;
  selectedIndex = index;
  document.querySelectorAll('.result-row').forEach((row, i) => {
    row.classList.toggle('selected', i === index);
  });
  const item = searchResults[index];
  showDetail(item);

  const row = document.querySelector('.result-row[data-index="' + index + '"]');
  if (row) row.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

function showDetail(item) {
  const panel = document.getElementById('detail-panel');
  const title = esc(item.document_title || item.work_title || item.volume_display || '');
  const author = item.author_label ? esc(item.author_label) : '';
  const pageLabel = formatCitationPageLabel(item);
  const page = esc(pageLabel);
  const score = Math.round(item.match_score * 100);
  const typeLabel = matchTypeLabel(item.match_type);
  const sourceLabel = item.source_type === 'pdf' ? 'PDF' : 'Word';

  let contextBefore = '';
  if (item.context_before && item.context_before.length) {
    contextBefore = '<div class="detail-context">' + item.context_before.map(c => esc(c.text)).join('\n') + '</div>';
  }
  let contextAfter = '';
  if (item.context_after && item.context_after.length) {
    contextAfter = '<div class="detail-context">' + item.context_after.map(c => esc(c.text)).join('\n') + '</div>';
  }

  let pageDetail = '';
  if (item.source_type === 'pdf') {
    pageDetail = '<div class="page-detail-toggle" onclick="togglePageDetail(this)">页码详情 ▸</div>'
      + '<div class="page-detail-body">'
      + pdRow('引用页码', pageLabel)
      + pdRow('PDF 页码标签', item.pdf_page_start_label || '无')
      + pdRow('PDF 物理页', item.pdf_page_start_index != null ? 'PDF 第 ' + (item.pdf_page_start_index + 1) + ' 页' : '—')
      + pdRow('映射方式', mappingMethodLabel(item.page_mapping_method))
      + (item.mapping_confidence_level ? pdRow('映射置信度', mappingConfidenceLabel(item.mapping_confidence_level, item.page_mapping_confidence)) : '')
      + (item.page_scope ? pdRow('页码范围', pageScopeLabel(item.page_scope)) : '')
      + (item.mapping_evidence ? pdRow('映射依据', mappingEvidenceSummary(item.mapping_evidence)) : '')
      + (item.is_cross_page ? pdRow('跨页命中', '是') : '')
      + '</div>';
  }

  const citationStyleLabel = citationStyle === 'gb' ? 'GB/T 7714' : '中文脚注';
  const citationIncomplete = item.citation_formats && (
    item.citation_formats.chinese_status !== 'complete' || item.citation_formats.gb_status !== 'complete'
  );

  panel.innerHTML = '<div class="detail-card">'
    + '<div class="detail-header">'
    + '<div class="detail-title">' + title + '</div>'
    + (author ? '<div class="detail-author">' + author + '</div>' : '')
    + '<div class="detail-pills">'
    + '<span class="detail-pill">' + sourceLabel + '</span>'
    + (item.volume_display ? '<span class="detail-pill">' + esc(item.volume_display) + '</span>' : '')
    + '<span class="detail-pill">' + page + '</span>'
    + '<span class="detail-pill accent">' + score + '% ' + typeLabel + '</span>'
    + '</div>'
    + pageDetail
    + '</div>'
    + '<div class="detail-body">'
    + contextBefore
    + '<div class="detail-hit">' + (item.highlighted_html || esc(item.paragraph_text || '')) + '</div>'
    + contextAfter
    + '</div>'
    + '<div class="detail-actions">'
    + '<button class="action-btn" onclick="copySelectedOriginal()">复制原文</button>'
    + '<span class="citation-copy-group">'
    + '<span class="app-select citation-style-control" id="citation-style-control">'
    + '<button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,\'citation-style-control\')"><span class="app-select-value" id="citation-style-label">' + citationStyleLabel + '</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>'
    + '<span class="app-select-menu" role="listbox"><button class="app-select-option' + (citationStyle === 'chinese' ? ' is-selected' : '') + '" type="button" data-value="chinese" onclick="selectCitationStyle(event,\'chinese\')">中文脚注</button><button class="app-select-option' + (citationStyle === 'gb' ? ' is-selected' : '') + '" type="button" data-value="gb" onclick="selectCitationStyle(event,\'gb\')">GB/T 7714</button></span>'
    + '</span>'
    + '<button class="action-btn" onclick="copySelectedCitation()">复制出处</button>'
    + '</span>'
    + '<button class="action-btn" onclick="copySelectedOriginalAndCitation()">复制原文与出处</button>'
    + (citationIncomplete && item.source_type === 'pdf' ? '<button class="action-btn" onclick="openMetadataForSource(\'' + esc(item.source_file_id) + '\')">补全书目信息</button>' : '')
    + (item.source_file_id ? '<button class="action-btn primary" onclick="openSource(\'' + esc(item.source_file_id) + '\',' + (item.pdf_page_start_index != null ? item.pdf_page_start_index + 1 : 'null') + ')">打开原文</button>' : '')
    + '</div>'
    + '</div>';

  requestAnimationFrame(function() {
    const hit = panel.querySelector('.detail-hit');
    if (!hit) return;
    hit.classList.remove('is-locating');
    void hit.offsetWidth;
    hit.classList.add('is-locating');
    const detailPane = document.querySelector('.results-detail-pane');
    if (!detailPane) return;
    const paneRect = detailPane.getBoundingClientRect();
    const hitRect = hit.getBoundingClientRect();
    if (hitRect.top < paneRect.top + 16 || hitRect.bottom > paneRect.bottom - 16) {
      hit.scrollIntoView({block: 'center', behavior: 'smooth'});
    }
  });
}

function showEmptyDetail() {
  document.getElementById('detail-panel').innerHTML = '<div class="empty-state"><div class="empty-state-icon"><svg width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.35"><rect x="8" y="6" width="32" height="36" rx="3"/><line x1="16" y1="16" x2="32" y2="16"/><line x1="16" y1="22" x2="32" y2="22"/><line x1="16" y1="28" x2="28" y2="28"/></svg></div><div class="empty-state-text">选择一条结果查看详情</div></div>';
}

/* ═══ Page detail toggle ═══ */
function togglePageDetail(el) {
  const body = el.nextElementSibling;
  if (!body) return;
  const open = body.classList.toggle('open');
  el.textContent = open ? '页码详情 ▾' : '页码详情 ▸';
}

/* ═══ Keyboard shortcuts ═══ */
document.addEventListener('keydown', function(e) {
  if (currentPage !== 'search') return;
  if (e.target && e.target.id === 'document-filter-query') {
    if (e.key === 'Escape') closeSearchSelects();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    runSearch();
    e.preventDefault();
    return;
  }
  if (e.key === 'Enter' && !e.isComposing) {
    runSearch();
    e.preventDefault();
    return;
  }
  if (e.key === 'ArrowDown' && searchResults.length) {
    e.preventDefault();
    selectResult(Math.min(selectedIndex + 1, searchResults.length - 1));
  }
  if (e.key === 'ArrowUp' && searchResults.length) {
    e.preventDefault();
    selectResult(Math.max(selectedIndex - 1, 0));
  }
});

/* ═══ Helpers ═══ */
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function truncate(s, n) {
  s = String(s || '').replace(/\s+/g, ' ');
  return s.length > n ? s.slice(0, n) + '…' : s;
}
function truncateHTML(html, maxText) {
  const div = document.createElement('div');
  div.innerHTML = html;
  const text = div.textContent || '';
  if (text.length <= maxText) return html;
  return esc(text.slice(0, maxText)) + '…';
}
function matchTypeLabel(t) {
  const m = {exact:'精确',normalized_exact:'标准化',space_insensitive:'忽略空格',punctuation_insensitive:'忽略标点',ngram_fuzzy:'模糊'};
  return m[t] || t || '';
}
function mappingMethodLabel(m) {
  const labels = {manual_segment:'人工分段',manual:'人工',manual_override:'人工覆盖',fixed_offset:'固定偏移',manual_page:'逐页校准',pdf_page_label:'PDF标签',numeric_bookmark_sequence:'PDF数字书签',native_pdf_edge_sequence:'页边数字序列',ocr_sequence:'OCR序列',ocr_sequence_with_structure:'OCR序列+结构',combined_sequence:'多来源序列',uncalibrated:'未校准',mixed:'混合'};
  return labels[m] || m || '';
}
function mappingStatusLabel(status) {
  const labels = {manual_mapped:'人工映射',auto_mapped_high:'自动映射 · 高可信',auto_mapped_medium:'自动映射 · 待确认',needs_review:'待确认',unmapped:'未映射',auto_mapping_failed:'自动检测失败',source_missing:'原文件缺失'};
  return labels[status] || status || '未映射';
}
function mappingConfidenceLabel(level, score) {
  const labels = {high:'高', medium:'中', low:'低', mixed:'混合'};
  const pct = score != null ? '（' + Math.round(Number(score) * 100) + '%）' : '';
  return (labels[level] || level || '') + pct;
}
function pageScopeLabel(scope) {
  const labels = {body:'正文', preface:'序言', front_matter:'前置页', appendix:'附录', mixed:'混合'};
  return labels[scope] || scope || '';
}
function mappingEvidenceSummary(evidence) {
  if (!evidence) return '';
  if (typeof evidence === 'string') return evidence;
  const parts = [];
  if (evidence.observed_page_numbers != null) parts.push('识别页码 ' + evidence.observed_page_numbers + ' 个');
  if (evidence.sequence_consistency != null) parts.push('连续性 ' + Math.round(Number(evidence.sequence_consistency) * 100) + '%');
  if (evidence.inferred_offset != null) parts.push('offset ' + evidence.inferred_offset);
  if (evidence.structure_evidence) parts.push('结构：' + pageScopeLabel(evidence.structure_evidence));
  return parts.join('；') || JSON.stringify(evidence).slice(0, 120);
}
function autoMappingSegmentText(seg) {
  if (!seg) return '';
  const pdfStart = Number(seg.pdf_page_start) + 1;
  const pdfEnd = Number(seg.pdf_page_end) + 1;
  const citation = formatCitationPageLabel({
    source_type: 'pdf',
    citation_page_label_start: seg.citation_page_label_start,
    citation_page_label_end: seg.citation_page_label_end,
    citation_page_start: seg.citation_page_start,
    citation_page_end: seg.citation_page_end
  });
  return pageScopeLabel(seg.page_scope) + ' PDF ' + pdfStart + '–' + pdfEnd + ' → ' + citation + ' ' + mappingConfidenceLabel(seg.confidence_level, seg.mapping_confidence);
}

function firstPageValue(values) {
  for (var i = 0; i < values.length; i++) {
    if (values[i] !== undefined && values[i] !== null && String(values[i]).trim() !== '') {
      return String(values[i]).trim();
    }
  }
  return '';
}

function isUncalibratedPageLabel(value) {
  return /(?:页码尚未校准|引用页码尚未校准|页码未验证|未校准)/.test(String(value || ''));
}

function formatChinesePageRange(start, end) {
  start = String(start || '').trim();
  end = String(end || '').trim();
  if (!start || isUncalibratedPageLabel(start)) return '页码尚未校准';
  if (!end || end === start || isUncalibratedPageLabel(end)) end = '';

  var startMatch = start.match(/^(.*?第)([^页]+)页$/);
  var endMatch = end.match(/^(.*?第)([^页]+)页$/);
  if (startMatch && !end) return start;
  if (startMatch && endMatch && startMatch[1] === endMatch[1]) {
    return startMatch[1] + startMatch[2] + '—' + endMatch[2] + '页';
  }
  if (startMatch && end && end.indexOf('页') < 0) {
    return startMatch[1] + startMatch[2] + '—' + end + '页';
  }
  if (start.indexOf('页') >= 0) return end ? start + '—' + end : start;
  if (endMatch && endMatch[1] === '第') return '第' + start + '—' + endMatch[2] + '页';
  return '第' + start + (end ? '—' + end : '') + '页';
}

function formatCitationPageLabel(item) {
  item = item || {};
  var sourceType = String(item.source_type || '').toLowerCase();
  var start;
  var end;
  if (sourceType === 'pdf') {
    start = firstPageValue([
      item.citation_page_label,
      item.citation_page_label_start,
      item.citation_page,
      item.citation_page_start
    ]);
    end = firstPageValue([item.citation_page_label_end, item.citation_page_end]);
  } else {
    start = firstPageValue([
      item.citation_page_label,
      item.citation_page_label_start,
      item.original_page_start,
      item.page
    ]);
    end = firstPageValue([
      item.citation_page_label_end,
      item.original_page_end,
      item.end_page
    ]);
  }
  return formatChinesePageRange(start, end);
}
function pdRow(label, value) {
  return '<div class="page-detail-row"><span class="page-detail-label">' + esc(label) + '</span><span>' + esc(String(value)) + '</span></div>';
}

function selectedResult() {
  if (selectedIndex < 0 || selectedIndex >= searchResults.length) return null;
  return searchResults[selectedIndex];
}

function setCitationStyle(style) {
  citationStyle = style === 'gb' ? 'gb' : 'chinese';
  localStorage.setItem('meFinderCitationStyle', citationStyle);
}

function selectCitationStyle(event, style) {
  event.stopPropagation();
  setCitationStyle(style);
  var label = document.getElementById('citation-style-label');
  if (label) label.textContent = citationStyle === 'gb' ? 'GB/T 7714' : '中文脚注';
  document.querySelectorAll('#citation-style-control .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === citationStyle);
  });
  closeAppSelects();
}

function citationForItem(item) {
  const formats = item && item.citation_formats ? item.citation_formats : {};
  return formats[citationStyle] || formats.chinese || formats.gb || item.copy_text || '';
}

function citationIsComplete(item) {
  const formats = item && item.citation_formats ? item.citation_formats : {};
  return formats[citationStyle + '_status'] === 'complete';
}

function showCitationMetadataError(item) {
  const formats = item && item.citation_formats ? item.citation_formats : {};
  const missing = formats[citationStyle + '_missing_fields'] || [];
  const labels = {author:'作者',title:'书名',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份',citation_page:'引用页码'};
  showToast('无法复制：缺少' + missing.map(function(x){return labels[x] || x;}).join('、'));
}

function copySelectedOriginal() {
  const item = selectedResult();
  if (!item) return;
  copyText(item.paragraph_text || '');
}

function copySelectedCitation() {
  const item = selectedResult();
  if (!item) return;
  if (!citationIsComplete(item)) { showCitationMetadataError(item); return; }
  copyText(citationForItem(item));
}

function copySelectedOriginalAndCitation() {
  const item = selectedResult();
  if (!item) return;
  if (!citationIsComplete(item)) { showCitationMetadataError(item); return; }
  const original = item.paragraph_text || '';
  const citation = citationForItem(item);
  copyText(original + (citation ? '\n\n' + citation : ''));
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => showToast('已复制')).catch(() => showToast('复制失败'));
}

async function openSource(sourceId, page) {
  if (!sourceId) return;
  try {
    const resp = await fetch('/api/open-source', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId, page: page})
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '打开失败');
    if (data.page && data.page_jump) showToast('已打开原文并跳转到 PDF 第 ' + data.page + ' 页');
    else if (data.page) showToast('已用系统默认阅读器打开，请手动翻到 PDF 第 ' + data.page + ' 页');
    else showToast('已打开原文');
  } catch(e) {
    showToast(e.message || '打开失败');
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}

/* ═══ Library ═══ */
async function loadLibrary() {
  try {
    const resp = await fetch('/api/library');
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    libSources = data.items || [];
    libVolumes = data.volumes || [];
    libWorks = data.works || [];
    libStats = data.stats || null;
    libLoaded = true;
    renderLibraryStats();
    syncLibraryViewButtons();
    syncLibrarySortControls();
    renderLibraryList();
  } catch(e) {
    document.getElementById('library-list').innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">' + esc(e.message || '文献库加载失败') + '</div></div>';
  }
}

function renderLibraryStats() {
  var container = document.getElementById('library-stats');
  if (!container) return;
  var current = {total:0,calibrated:0,pending:0,review:0,failed:0,mapping:0};
  libSources.forEach(function(item) {
    if (item.source_type !== 'pdf') return;
    current.total += 1;
    var group = calibrationStatusGroup(item.status);
    if (current[group] != null) current[group] += 1;
  });
  container.innerHTML = statusStatButton('all','PDF 总数',current.total,'info','document',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('calibrated','已校准',current.calibrated,'success','check',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('pending','待校准',current.pending,'neutral','clock',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('review','待确认',current.review,'warning','notice',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('failed','检测失败',current.failed,'danger','danger',libStatusFilter,'applyLibStatusFilter');
}

function applyLibStatusFilter(status) {
  libStatusFilter = status || 'all';
  if (libStatusFilter !== 'all' && libTypeFilter === 'word') {
    libTypeFilter = 'all';
    document.querySelectorAll('#lib-type-control .seg-btn').forEach(function(b) {
      b.classList.toggle('active', b.dataset.type === 'all');
    });
  }
  closeLibDrawer();
  renderLibraryStats();
  renderLibraryList();
}

function setLibFilter(btn) {
  libTypeFilter = btn.dataset.type;
  if (libTypeFilter === 'word' && libStatusFilter !== 'all') {
    libStatusFilter = 'all';
    renderLibraryStats();
  }
  document.querySelectorAll('#lib-type-control .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  closeLibDrawer();
  renderLibraryList();
}

function setLibLangFilter(btn) {
  libLangFilter = btn.dataset.lang;
  document.querySelectorAll('#lib-lang-control .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  closeLibDrawer();
  renderLibraryList();
}

function filterLibrary() {
  renderLibraryList();
}

function setLibraryView(mode) {
  libViewMode = mode === 'grid' ? 'grid' : 'list';
  localStorage.setItem('meFinderLibraryView', libViewMode);
  persistDisplayPreference('library_view', libViewMode);
  syncLibraryViewButtons();
  renderLibraryList();
}

function syncLibraryViewButtons() {
  ['list','grid'].forEach(function(mode) {
    var button = document.getElementById('library-view-' + mode);
    if (!button) return;
    var active = libViewMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function setLibrarySortOption(event, control, value) {
  event.stopPropagation();
  if (control === 'direction') {
    libSortDirection = value === 'asc' ? 'asc' : 'desc';
    localStorage.setItem('meFinderLibrarySortDirection', libSortDirection);
  } else {
    libSortField = ['imported_at','title','author','modified_at','source_type','status'].indexOf(value) >= 0 ? value : 'imported_at';
    localStorage.setItem('meFinderLibrarySortField', libSortField);
  }
  syncLibrarySortControls();
  closeAppSelects();
  renderLibraryList();
}

function syncLibrarySortControls() {
  var labels = {imported_at:'导入时间',title:'书名',author:'作者',modified_at:'最近修改时间',source_type:'来源类型',status:'校准状态',desc:'降序',asc:'升序'};
  var fieldLabel = document.getElementById('library-sort-field-label');
  var directionLabel = document.getElementById('library-sort-direction-label');
  if (fieldLabel) fieldLabel.textContent = labels[libSortField] || labels.imported_at;
  if (directionLabel) directionLabel.textContent = labels[libSortDirection] || labels.desc;
  document.querySelectorAll('#library-sort-field-select .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === libSortField);
  });
  document.querySelectorAll('#library-sort-direction-select .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === libSortDirection);
  });
}

function librarySortProjection(source) {
  return {
    title: source.title || source.file_name || source.source_file_id,
    author: source.author || '',
    imported_at: source.imported_at || source.last_modified || '',
    modified_at: source.modified_at || source.last_modified || '',
    source_type: source.source_type === 'word' ? 'Word' : 'PDF'
  };
}

function compareLibraryDates(a, b) {
  var av = Date.parse(a || '') || 0;
  var bv = Date.parse(b || '') || 0;
  if (!av && !bv) return 0;
  if (!av) return 1;
  if (!bv) return -1;
  return libSortDirection === 'desc' ? bv - av : av - bv;
}

function getFilteredSources() {
  let sources = libSources.slice();
  if (libTypeFilter !== 'all') {
    sources = sources.filter(s => s.source_type === libTypeFilter);
  }
  if (libLangFilter !== 'all') {
    sources = sources.filter(s => (s.language || 'chinese') === libLangFilter);
  }
  if (libStatusFilter !== 'all') {
    sources = sources.filter(s => s.source_type === 'pdf' && calibrationStatusGroup(s.status) === libStatusFilter);
  }
  const q = (document.getElementById('lib-search').value || '').trim().toLowerCase().replace(/\s+/g, '');
  if (q) {
    sources = sources.filter(s => {
      const haystack = [s.title, s.author, s.translator, s.publisher, s.file_name]
        .map(function(value) { return String(value || '').toLowerCase().replace(/\s+/g, ''); })
        .join('|');
      return haystack.indexOf(q) >= 0;
    });
  }
  sources.sort(function(a, b) {
    var left = librarySortProjection(a);
    var right = librarySortProjection(b);
    var result;
    if (libSortField === 'imported_at' || libSortField === 'modified_at') {
      result = compareLibraryDates(left[libSortField], right[libSortField]);
    } else if (libSortField === 'status') {
      var order = {manual_mapped:0,auto_mapped_high:1,unmapped:2,needs_review:3,auto_mapping_failed:4,source_missing:5,mapping:6};
      var av = a.source_type === 'pdf' && order[a.status] != null ? order[a.status] : 99;
      var bv = b.source_type === 'pdf' && order[b.status] != null ? order[b.status] : 99;
      result = libSortDirection === 'desc' ? bv - av : av - bv;
    } else {
      result = calibrationSortText(left[libSortField], right[libSortField], libSortDirection);
    }
    return result || calibrationSortText(left.title, right.title, 'asc');
  });
  return sources;
}

function renderLibraryList() {
  const sources = getFilteredSources();
  const listEl = document.getElementById('library-list');
  listEl.className = 'library-list-container library-view-' + libViewMode;
  const allCount = libSources.length;
  const wordCount = libSources.filter(s => s.source_type === 'word').length;
  const pdfCount = libSources.filter(s => s.source_type === 'pdf').length;
  document.querySelectorAll('#lib-type-control .seg-btn').forEach(function(btn) {
    var t = btn.dataset.type;
    var c = t === 'all' ? allCount : t === 'word' ? wordCount : pdfCount;
    var label = t === 'all' ? '全部' : t === 'word' ? 'Word' : 'PDF';
    btn.textContent = label + ' (' + c + ')';
  });
  const chineseCount = libSources.filter(s => (s.language || 'chinese') === 'chinese').length;
  const foreignCount = libSources.length - chineseCount;
  document.querySelectorAll('#lib-lang-control .seg-btn').forEach(function(btn) {
    var lang = btn.dataset.lang;
    var count = lang === 'all' ? allCount : lang === 'chinese' ? chineseCount : foreignCount;
    var label = lang === 'all' ? '全部语言' : lang === 'chinese' ? '中文文献' : '外文文献';
    btn.textContent = label + ' (' + count + ')';
  });

  if (sources.length === 0) {
    listEl.innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">未找到匹配文献</div></div>';
    return;
  }
  listEl.innerHTML = sources.map(function(src) {
    var vol = libVolumes.find(function(v) { return v.source_file_id === src.source_file_id; });
    var isPdf = src.source_type === 'pdf';
    var title = src.title || (src.file_name || src.source_file_id);
    var author = src.author || '作者信息待完善';
    var bib = sourceBibliographicMetadata(src);
    var missingMetadataText = isPdf ? bibliographicMissingText(bib) : '';
    var size = formatFileSize(src.size_bytes);
    var isSelected = src.source_file_id === libSelectedId;
    var typeCls = isPdf ? (src.parser_label === 'MinerU' ? 'mineru' : 'pdf') : 'word';
    var typeLabel = isPdf ? (src.parser_label || 'PDF') : 'Word';
    var itemStatus = isPdf ? (calTransientStatus[src.source_file_id] || src.status) : '';
    var statusGroup = isPdf ? calibrationStatusGroup(itemStatus) : '';
    var statusChip = isPdf
      ? '<span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(statusGroup) + ' ' + statusGroup + '">' + statusChipIcon(statusGroup) + esc(calibrationStatusLabel(itemStatus)) + '</span>'
      : '';
    var countMeta = isPdf
      ? (src.page_count ? src.page_count + ' 页' : '页数未知')
      : ((src.works_count || 1) + ' 篇');
    if (libViewMode === 'grid') {
      var imported = formatCalDate(src.imported_at || src.last_modified);
      var secondary = !isPdf ? ((vol && vol.corpus_title) || '') : '';
      return '<article class="library-card library-entry' + (isSelected ? ' selected' : '') + '" data-id="' + esc(src.source_file_id) + '" onclick="selectLibDoc(\'' + esc(src.source_file_id) + '\')">'
        + '<div class="library-card-top"><div class="library-card-badges"><span class="type-badge ' + typeCls + '">' + typeLabel + '</span>' + statusChip + (secondary ? '<span class="library-card-status">' + esc(secondary) + '</span>' : '') + '</div></div>'
        + '<div class="library-card-title">' + esc(title) + '</div><div class="library-card-author">' + esc(author) + '</div>'
        + (missingMetadataText ? bibliographicMissingBadge(bib) : '')
        + '<div class="library-card-meta">' + esc(countMeta + ' · ' + size) + '</div>'
        + '<div class="library-card-mapping">' + esc(isPdf ? (src.mapping_summary || '尚未建立引用页码映射') : ((vol && vol.version_info) || 'Word 文献')) + '</div>'
        + '<div class="library-card-footer"><span class="library-card-action">查看详情</span><span class="library-card-date">' + esc(imported === '未知' ? '日期未知' : imported + ' 导入') + '</span></div></article>';
    }
    return '<div class="library-row library-entry' + (isSelected ? ' selected' : '') + '" data-id="' + esc(src.source_file_id) + '" onclick="selectLibDoc(\'' + esc(src.source_file_id) + '\')">'
      + '<span class="type-badge ' + typeCls + '">' + typeLabel + '</span>'
      + '<span class="library-row-title">' + esc(title) + '</span>'
      + '<span class="library-row-info">'
      + statusChip
      + '<span class="works-count">' + esc(countMeta) + '</span>'
      + (missingMetadataText ? '<span class="library-row-missing" title="' + esc(missingMetadataText) + '">' + esc(missingMetadataText) + '</span>' : '')
      + '<span>' + size + '</span>'
      + '</span>'
      + '</div>';
  }).join('');
}

function selectLibDoc(sourceId) {
  libSelectedId = sourceId;
  document.querySelectorAll('#library-list .library-entry').forEach(function(row) {
    row.classList.toggle('selected', row.dataset.id === sourceId);
  });
  var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
  if (!src) return;
  var vol = libVolumes.find(function(v) { return v.source_file_id === sourceId; });
  var works = vol ? libWorks.filter(function(w) { return w.volume_id === vol.volume_id; }) : [];
  var title = vol ? vol.display_title : (src.file_name || sourceId);
  var corpusTitle = vol ? (vol.corpus_title || '') : '';

  var info = '';
  info += drawerInfoRow('文件类型', src.source_type === 'pdf' ? 'PDF 文档' : 'Word 文档');
  info += drawerInfoRow('文件名', src.file_name);
  info += drawerInfoRow('大小', formatFileSize(src.size_bytes));
  if (src.source_type === 'pdf' && src.pdf_profile) {
    info += drawerInfoRow('PDF 页数', src.pdf_profile.pdf_page_count + ' 页');
    info += drawerInfoRow('PDF 类型', pdfTypeLabel(src.pdf_profile.detected_pdf_type));
    info += drawerInfoRow('页码状态', mappingStatusLabel(src.pdf_profile.mapping_status));
    if (src.pdf_profile.auto_page_mapping) {
      var autoMap = src.pdf_profile.auto_page_mapping;
      var autoText = autoMap.method === 'manual_override'
        ? '保留人工映射'
        : '应用 ' + (autoMap.applied_segment_count || 0) + ' 个自动段，候选 ' + (autoMap.candidate_count || 0) + ' 个';
      info += drawerInfoRow('自动页码映射', autoText);
      if (autoMap.applied_segments && autoMap.applied_segments.length) {
        info += drawerInfoRow('自动映射区间', autoMap.applied_segments.map(autoMappingSegmentText).join('；'));
      }
      if (autoMap.exception_pages && autoMap.exception_pages.length) {
        info += drawerInfoRow('异常页面', autoMap.exception_pages.length + ' 页');
      }
    }
  }
  if (src.last_modified) {
    info += drawerInfoRow('修改日期', src.last_modified.split('T')[0]);
  }
  if (vol && vol.version_info) {
    info += drawerInfoRow('版本', vol.version_info);
  }

  var worksHTML = '';
  if (works.length > 0) {
    worksHTML = '<div class="drawer-section-title">收录文献 (' + works.length + ')</div>'
      + '<div class="drawer-works-list">'
      + works.map(function(w) {
        var meta = [];
        if (w.author_label) meta.push(w.author_label);
        if (w.date_label) meta.push(w.date_label);
        if (w.toc_page_start) meta.push('p.' + w.toc_page_start + (w.toc_page_end ? '–' + w.toc_page_end : ''));
        return '<div class="drawer-work-item">'
          + '<div class="drawer-work-title">' + esc(w.title) + '</div>'
          + (meta.length ? '<div class="drawer-work-meta">' + esc(meta.join(' · ')) + '</div>' : '')
          + '</div>';
      }).join('')
      + '</div>';
  }

  var bibliographicHTML = '';
  if (src.source_type === 'pdf') {
    bibliographicHTML = bibliographicEditorHTML(src);
  }

  var autoActions = '';
  if (src.source_type === 'pdf') {
    autoActions += '<button class="action-btn primary" onclick="openCalibrationAndDetect(\'' + esc(src.source_file_id) + '\')">自动检测页码</button>';
  }
  if (src.source_type === 'pdf' && src.pdf_profile && src.pdf_profile.detected_pdf_type && src.pdf_profile.detected_pdf_type !== 'native_text') {
    var ocrLabel = src.parser_type === 'mineru_structured' ? '重新 OCR' : '提交 MinerU 解析';
    var ocrRunning = calTransientStatus[src.source_file_id] === 'mapping';
    autoActions += '<button class="action-btn" id="mineru-reparse-btn"' + (ocrRunning ? ' disabled' : '') + ' onclick="submitMineruReparse(\'' + esc(src.source_file_id) + '\')">' + (ocrRunning ? '正在解析…' : ocrLabel) + '</button>';
  }
  if (src.source_type === 'pdf' && src.pdf_profile && src.pdf_profile.auto_page_mapping) {
    var autoMapForActions = src.pdf_profile.auto_page_mapping;
    if (autoMapForActions.applied_segments && autoMapForActions.applied_segments.length) {
      autoActions += '<button class="action-btn" onclick="acceptAutoMapping(\'' + esc(src.source_file_id) + '\')">接受自动映射</button>';
    }
    if (autoMapForActions.exception_pages && autoMapForActions.exception_pages.length) {
      autoActions += '<button class="action-btn" onclick="showAutoMappingExceptions(\'' + esc(src.source_file_id) + '\')">检查异常</button>';
    }
    autoActions += '<button class="action-btn" onclick="openCalibrationForSource(\'' + esc(src.source_file_id) + '\')">编辑区间</button>';
  }

  var el = document.getElementById('library-drawer-content');
  el.innerHTML = '<div class="drawer-title">' + esc(title) + '</div>'
    + (corpusTitle ? '<div class="drawer-subtitle">' + esc(corpusTitle) + '</div>' : '')
    + '<div class="detail-pills" style="margin-top:12px">'
    + '<span class="detail-pill">' + (src.source_type === 'pdf' ? 'PDF' : 'Word') + '</span>'
    + (vol && vol.primary_structure ? '<span class="detail-pill">' + structureLabel(vol.primary_structure) + '</span>' : '')
    + '</div>'
    + '<div class="drawer-info">' + info + '</div>'
    + bibliographicHTML
    + worksHTML
    + '<div class="drawer-actions">'
    + autoActions
    + (src.source_file_id ? '<button class="action-btn primary" onclick="openSource(\'' + esc(src.source_file_id) + '\', null)">打开原文</button>' : '')
    + '</div>';
  document.getElementById('library-drawer').classList.add('open');
  var body = document.querySelector('#page-library .library-body');
  if (body) body.classList.add('detail-open');
  renderDrawerCalibration(src);
}

function renderDrawerCalibrationSummary(src) {
  var summary = document.getElementById('cal-collapse-summary');
  if (!summary) return;
  var status = calTransientStatus[src.source_file_id] || src.status;
  var group = calibrationStatusGroup(status);
  summary.innerHTML = '<span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(group) + ' ' + group + '">' + statusChipIcon(group) + esc(calibrationStatusLabel(status)) + '</span>'
    + '<span class="cal-collapse-mapping">' + esc(src.mapping_summary || '尚未建立引用页码映射') + '</span>';
}

function renderDrawerCalibration(src) {
  var host = document.getElementById('library-drawer-calibration');
  if (!host) return;
  var isPdf = src.source_type === 'pdf';
  host.style.display = isPdf ? 'block' : 'none';
  host.classList.remove('expanded');
  document.getElementById('cal-section-body').style.display = 'none';
  var toggle = document.getElementById('cal-collapse-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', 'false');
  if (!isPdf) {
    calSelectedSourceId = null;
    return;
  }
  renderDrawerCalibrationSummary(src);
}

async function toggleDrawerCalibration(forceOpen) {
  var host = document.getElementById('library-drawer-calibration');
  var body = document.getElementById('cal-section-body');
  if (!host || !body) return;
  var open = forceOpen === true ? true : body.style.display === 'none';
  body.style.display = open ? 'block' : 'none';
  host.classList.toggle('expanded', open);
  var toggle = document.getElementById('cal-collapse-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open && libSelectedId && calSelectedSourceId !== libSelectedId) {
    calSelectedSourceId = libSelectedId;
    await loadCalibrationDoc(libSelectedId);
  }
}

function updateLibraryEntry(sourceId) {
  renderLibraryStats();
  renderLibraryList();
  if (libSelectedId === sourceId) {
    var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
    if (src && src.source_type === 'pdf') renderDrawerCalibrationSummary(src);
  }
}

function sourceBibliographicMetadata(src) {
  var nested = src && src.bibliographic_metadata ? src.bibliographic_metadata : {};
  var meta = Object.assign({}, nested);
  ['title','author','country','translator','publisher','publish_place','publish_year','isbn','document_type','metadata_status','metadata_source','metadata_confidence','metadata_evidence','metadata_conflicts','metadata_missing_fields'].forEach(function(key) {
    if (src && src[key] != null && src[key] !== '') meta[key] = src[key];
  });
  return meta;
}

const bibliographicFieldLabels = {author:'作者',title:'书名',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份'};

function bibliographicMissingFields(meta) {
  meta = meta || {};
  var listed = Array.isArray(meta.metadata_missing_fields) ? meta.metadata_missing_fields.slice() : null;
  var required = listed || ['author','title','publisher','publish_place','publish_year'];
  if (!listed && meta.document_type === 'translated_book') required.splice(2, 0, 'translator');
  return required.filter(function(field, index, values) {
    if (field === 'isbn' || !bibliographicFieldLabels[field] || values.indexOf(field) !== index) return false;
    if (listed) return true;
    return !String(meta[field] == null ? '' : meta[field]).trim();
  });
}

function bibliographicMissingText(meta) {
  var fields = bibliographicMissingFields(meta);
  return fields.length ? '缺少：' + fields.map(function(field) { return bibliographicFieldLabels[field]; }).join('、') : '';
}

function bibliographicMissingBadge(meta) {
  var text = bibliographicMissingText(meta);
  if (!text) return '';
  return '<span class="bibliographic-missing" title="ISBN 不计入引文必需字段"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/></svg><span>' + esc(text) + '</span></span>';
}

function bibliographicEditorHTML(src) {
  var meta = sourceBibliographicMetadata(src);
  var missing = bibliographicMissingFields(meta);
  function field(id, metadataField, label, value, full) {
    var isMissing = missing.indexOf(metadataField) >= 0;
    return '<div class="bibliographic-field' + (full ? ' full' : '') + (isMissing ? ' is-missing' : '') + '"><label for="bib-' + id + '">' + label + (isMissing ? ' · 缺少' : '') + '</label><input id="bib-' + id + '" value="' + esc(value || '') + '"></div>';
  }
  return '<div class="drawer-section-title">书目信息</div>'
    + bibliographicMissingBadge(meta)
    + '<div class="bibliographic-grid">'
    + field('author','author','作者',meta.author,false) + field('country','country','国别',meta.country,false)
    + field('title','title','书名',meta.title,false) + field('translator','translator','译者',meta.translator,false)
    + field('publish-place','publish_place','出版地',meta.publish_place,false)
    + field('publisher','publisher','出版社',meta.publisher,false) + field('publish-year','publish_year','出版年份',meta.publish_year,false)
    + field('isbn','isbn','ISBN',meta.isbn,true) + '</div>'
    + '<div class="bibliographic-meta">状态：' + esc(metadataStatusLabel(meta.metadata_status)) + ' · 来源：' + esc(metadataSourceLabel(meta.metadata_source)) + '</div>'
    + '<div class="auto-detect-actions">'
    + '<button class="action-btn" onclick="detectBibliographicMetadata(\'' + esc(src.source_file_id) + '\',false)">自动识别书目信息</button>'
    + (meta.metadata_source === 'manual' ? '<button class="action-btn" onclick="detectBibliographicMetadata(\'' + esc(src.source_file_id) + '\',true)">重新识别并覆盖表单</button>' : '')
    + '<button class="action-btn primary" onclick="saveBibliographicMetadata(\'' + esc(src.source_file_id) + '\')">保存</button>'
    + '<button class="action-btn" onclick="showBibliographicEvidence(\'' + esc(src.source_file_id) + '\')">查看识别依据</button>'
    + '</div>';
}

function metadataStatusLabel(status) {
  return ({complete:'完整',partial:'部分缺失',missing:'缺失',needs_review:'待确认',recognition_failed:'识别失败'})[status] || status || '未识别';
}

function metadataSourceLabel(source) {
  return ({manual:'人工维护',auto:'自动识别',automatic_recognition:'自动识别',pdf_metadata:'PDF 元数据'})[source] || source || '未知';
}

function collectBibliographicForm() {
  function value(id) { var el = document.getElementById('bib-' + id); return el ? el.value.trim() : ''; }
  return {author:value('author'),country:value('country'),title:value('title'),translator:value('translator'),publish_place:value('publish-place'),publisher:value('publisher'),publish_year:value('publish-year'),isbn:value('isbn')};
}

async function detectBibliographicMetadata(sourceId, force) {
  if (force && !confirm('确认用自动识别结果覆盖当前表单中的人工书目信息吗？')) return;
  try {
    showToast('正在识别封面、书名页、CIP 与版权页…');
    var resp = await fetch('/api/bibliographic-metadata/detect', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,force:!!force})});
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '识别失败');
    var src = libSources.find(function(item){return item.source_file_id === sourceId;});
    if (src) {
      src.bibliographic_metadata = data.metadata;
      Object.keys(data.metadata).forEach(function(key){src[key]=data.metadata[key];});
      selectLibDoc(sourceId);
    }
    showToast(data.metadata.metadata_source === 'manual' && !force ? '人工元数据已保护，未覆盖' : '识别结果已载入，请检查后保存');
  } catch(e) { showToast('识别失败：' + e.message); }
}

async function saveBibliographicMetadata(sourceId) {
  try {
    var resp = await fetch('/api/bibliographic-metadata/save', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,metadata:collectBibliographicForm()})});
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '保存失败');
    showToast('书目信息已保存并立即生效');
    libLoaded = false;
    await loadLibrary();
    selectLibDoc(sourceId);
  } catch(e) { showToast('保存失败：' + e.message); }
}

function showBibliographicEvidence(sourceId) {
  var src = libSources.find(function(item){return item.source_file_id === sourceId;});
  var evidence = sourceBibliographicMetadata(src).metadata_evidence || {};
  var labels = {title:'书名',author:'作者',country:'国别',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份',isbn:'ISBN'};
  var lines = Object.keys(evidence).map(function(field) {
    var item = evidence[field] || {};
    return (labels[field] || field) + '：' + (item.evidence_text || '无文本依据') + (item.source_page ? '（PDF 第 ' + item.source_page + ' 页）' : '') + (item.source === 'inferred_from_publisher' ? '（由出版社推断）' : '');
  });
  alert(lines.length ? lines.join('\n') : '暂无自动识别依据。');
}

async function openMetadataForSource(sourceId) {
  navigateTo('library');
  if (!libLoaded) await loadLibrary();
  selectLibDoc(sourceId);
}

function closeLibDrawer() {
  libSelectedId = null;
  calSelectedSourceId = null;
  document.getElementById('library-drawer').classList.remove('open');
  var body = document.querySelector('#page-library .library-body');
  if (body) body.classList.remove('detail-open');
  document.querySelectorAll('#library-list .library-entry').forEach(function(row) { row.classList.remove('selected'); });
}

async function submitMineruReparse(sourceId) {
  try {
    var resp = await fetch('/api/mineru-reparse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '提交失败');
    showToast(data.already_running ? 'MinerU 解析已在进行中' : '已提交 MinerU 解析，完成后自动重建索引');
    calTransientStatus[sourceId] = 'mapping';
    updateLibraryEntry(sourceId);
    if (libSelectedId === sourceId) selectLibDoc(sourceId);
    pollMineruReparse(sourceId, data.job_id);
  } catch(e) {
    showToast('提交 MinerU 解析失败：' + e.message);
  }
}

function pollMineruReparse(sourceId, jobId) {
  fetch('/api/import-status?job_id=' + encodeURIComponent(jobId))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data.status === 'completed') {
        delete calTransientStatus[sourceId];
        showToast('MinerU 解析完成，索引已更新');
        refreshCalibrationSource(sourceId).then(function() {
          if (libSelectedId === sourceId) selectLibDoc(sourceId);
        }).catch(function() {});
        return;
      }
      if (data.status === 'failed' || data.error) {
        delete calTransientStatus[sourceId];
        updateLibraryEntry(sourceId);
        if (libSelectedId === sourceId) selectLibDoc(sourceId);
        showToast('MinerU 解析失败：' + (data.message || data.error || '未知错误'));
        return;
      }
      setTimeout(function() { pollMineruReparse(sourceId, jobId); }, 4000);
    })
    .catch(function() {
      setTimeout(function() { pollMineruReparse(sourceId, jobId); }, 8000);
    });
}

async function acceptAutoMapping(sourceId) {
  if (!sourceId) return;
  try {
    showToast('正在接受自动映射…');
    var resp = await fetch('/api/auto-page-mapping/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '接受失败');
    showToast('自动映射已接受为人工映射');
    libLoaded = false;
    await loadMeta();
    await loadLibrary();
    selectLibDoc(sourceId);
  } catch(e) {
    showToast('接受失败：' + e.message);
  }
}

function showAutoMappingExceptions(sourceId) {
  var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
  var autoMap = src && src.pdf_profile ? src.pdf_profile.auto_page_mapping : null;
  var pages = autoMap && autoMap.exception_pages ? autoMap.exception_pages : [];
  if (!pages.length) {
    showToast('没有异常页面');
    return;
  }
  alert('异常页面（PDF 物理页）：\\n' + pages.slice(0, 80).map(function(p) { return Number(p) + 1; }).join(', ') + (pages.length > 80 ? '\\n…' : ''));
}

async function openCalibrationForSource(sourceId) {
  navigateTo('library');
  if (!libLoaded) await loadLibrary();
  selectLibDoc(sourceId);
  await toggleDrawerCalibration(true);
  var host = document.getElementById('library-drawer-calibration');
  if (host) host.scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function openCalibrationAndDetect(sourceId) {
  await openCalibrationForSource(sourceId);
  await runAutoDetection(sourceId);
}

function drawerInfoRow(label, value) {
  return '<div class="drawer-info-row"><span class="drawer-info-label">' + esc(label) + '</span><span class="drawer-info-value">' + esc(String(value || '—')) + '</span></div>';
}

function pdfTypeLabel(type) {
  var labels = {native_text:'原生文本',scanned:'扫描版',broken_text:'文本损坏',complex_layout:'复杂排版',mineru_structured:'MinerU 结构化'};
  return labels[type] || type || '未知';
}

function structureLabel(s) {
  var labels = {article_collection:'文集',monograph:'专著',whole_pdf:'整本',pdf_document:'PDF 文献',manuscript_selection:'手稿选编',mixed:'混合',letters:'书信集'};
  return labels[s] || s || '';
}

function formatFileSize(bytes) {
  if (!bytes) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

/* ═══ Calibration ═══ */
async function refreshCalibrationSource(sourceId) {
  var resp = await fetch('/api/library');
  var data = await resp.json();
  if (!resp.ok || data.error) throw new Error(data.error || '刷新文献状态失败');
  libSources = data.items || [];
  libVolumes = data.volumes || [];
  libWorks = data.works || [];
  libStats = data.stats || null;
  libLoaded = true;
  updateLibraryEntry(sourceId);
  if (calSelectedSourceId === sourceId) await loadCalibrationDoc(sourceId);
}

function statusStatIcon(icon) {
  var paths = {
    document:'<path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4"/>',
    check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    notice:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    danger:'<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>'
  };
  return '<span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (paths[icon] || paths.notice) + '</svg></span>';
}

function statusChipIcon(group) {
  var icons = {
    calibrated:'<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
    pending:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    review:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    failed:'<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>',
    mapping:'<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>'
  };
  var spinning = group === 'mapping' ? ' is-spinning' : '';
  return '<span class="status-chip__icon' + spinning + '" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (icons[group] || icons.pending) + '</svg></span>';
}

function statusStatButton(status, label, value, variant, icon, activeFilter, handlerName) {
  return '<button type="button" data-status="' + status + '" class="status-stat status-stat--' + variant + (activeFilter === status ? ' active' : '') + '" onclick="' + handlerName + '(\'' + status + '\')">'
    + statusStatIcon(icon)
    + '<span class="status-stat__label">' + label + '</span>'
    + '<span class="status-stat__count">' + value + '</span></button>';
}

const calPinyinCollator = new Intl.Collator('zh-CN-u-co-pinyin', {sensitivity:'base', numeric:true});
const calLatinCollator = new Intl.Collator('en', {sensitivity:'base', numeric:true});

function calibrationSortText(a, b, direction) {
  a = String(a || '').trim(); b = String(b || '').trim();
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  var ag = /^[\u3400-\u9fff]/.test(a) ? 0 : (/^[A-Za-z]/.test(a) ? 1 : 2);
  var bg = /^[\u3400-\u9fff]/.test(b) ? 0 : (/^[A-Za-z]/.test(b) ? 1 : 2);
  if (ag !== bg) return ag - bg;
  var value = (ag === 0 ? calPinyinCollator : calLatinCollator).compare(a, b);
  return direction === 'desc' ? -value : value;
}

function calibrationStatusGroup(status) {
  if (status === 'manual_mapped' || status === 'auto_mapped_high') return 'calibrated';
  if (status === 'needs_review') return 'review';
  if (status === 'auto_mapping_failed' || status === 'source_missing') return 'failed';
  if (status === 'mapping') return 'mapping';
  return 'pending';
}

function statusSemanticVariant(group) {
  var variants = {calibrated:'success',pending:'neutral',review:'warning',failed:'danger',mapping:'info'};
  return variants[group] || 'neutral';
}

function calibrationStatusLabel(status) {
  var labels = {manual_mapped:'已校准',auto_mapped_high:'已校准',needs_review:'待确认',unmapped:'待校准',auto_mapping_failed:'检测失败',mapping:'正在检测',source_missing:'原文件缺失'};
  return labels[status] || '待校准';
}

function formatCalDate(value) {
  if (!value) return '未知';
  var date = new Date(value);
  if (isNaN(date.getTime())) return '未知';
  return date.getFullYear() + '-' + String(date.getMonth()+1).padStart(2,'0') + '-' + String(date.getDate()).padStart(2,'0');
}

async function loadCalibrationDoc(sourceId) {
  sourceId = sourceId || calSelectedSourceId;
  var editor = document.getElementById('cal-editor');
  if (!sourceId) {
    editor.style.display = 'none';
    calSelectedDoc = null;
    calSegments = [];
    calAutoResult = null;
    document.getElementById('cal-auto-preview').style.display = 'none';
    return;
  }
  try {
    var resp = await fetch('/api/calibration?source_id=' + encodeURIComponent(sourceId));
    calSelectedDoc = await resp.json();
    if (calSelectedDoc.error) {
      showToast('文献未找到');
      return;
    }
    var mapping = calSelectedDoc.page_mapping || {};
    calSegments = (mapping.segments || []).map(function(s) { return Object.assign({}, s); });
    document.getElementById('cal-detail-actions').innerHTML = '<button class="action-btn primary" id="cal-auto-detect-btn" onclick="runAutoDetection()">自动检测页码</button><button class="action-btn" onclick="scrollToManualMapping()">手动设置</button><button class="action-btn" onclick="showCalibrationEvidence()">查看识别依据</button>'
      + '<span class="detail-pill" style="margin-left:auto">' + (mapping.validated_by ? '已验证' : '未验证') + '</span>';
    editor.style.display = 'block';
    calAutoResult = null;
    document.getElementById('cal-auto-preview').style.display = 'none';
    renderCalSegments();
    updateCalPreview();
  } catch(e) {
    showToast('加载校准数据失败');
  }
}

async function runAutoDetection(sourceId) {
  sourceId = sourceId || calSelectedSourceId;
  if (!sourceId) {
    showToast('请先选择一本文献');
    return;
  }
  calTransientStatus[sourceId] = 'mapping';
  updateLibraryEntry(sourceId);
  var panel = document.getElementById('cal-auto-preview');
  var button = document.getElementById('cal-auto-detect-btn');
  panel.style.display = 'block';
  panel.innerHTML = '<div class="auto-detect-title">正在检测页码…</div><div class="auto-detect-note">正在读取 PDF 标签、数字书签、现有 MinerU 结果和页面边缘文本。</div>';
  if (button) button.disabled = true;
  try {
    var resp = await fetch('/api/auto-page-mapping/detect', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({source_id:sourceId, dry_run:true})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '检测失败');
    calAutoResult = data.result;
    renderAutoDetectionResult(calAutoResult);
    var current = libSources.find(function(item) { return item.source_file_id === sourceId; });
    var segments = (calAutoResult.selected_segments || []).filter(function(item) { return item && item.confidence_level !== 'low'; });
    calTransientStatus[sourceId] = current && current.status === 'manual_mapped' ? 'manual_mapped' : (segments.length ? 'needs_review' : 'auto_mapping_failed');
  } catch(e) {
    calAutoResult = null;
    calTransientStatus[sourceId] = 'auto_mapping_failed';
    panel.innerHTML = '<div class="auto-detect-title">自动检测失败</div><div class="auto-detect-note">' + esc(e.message) + '</div>';
  } finally {
    if (button) button.disabled = false;
    updateLibraryEntry(sourceId);
  }
}

function renderAutoDetectionResult(result) {
  var panel = document.getElementById('cal-auto-preview');
  var segments = (result.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; });
  var html = '<div class="auto-detect-title">检测完成</div>';
  if (result.manual_mapping_present) {
    html += '<div class="auto-detect-note auto-detect-warning">当前文献已有人工页码映射。以下结果仅为预览，不会自动覆盖。</div>';
  }
  if (!segments.length) {
    html += '<div class="auto-detect-note">未能自动识别可靠页码区间。</div>';
    html += '<div class="auto-detect-note">' + autoFailureReasons(result.failure_reasons || []) + '</div>';
    html += '<div class="auto-detect-actions"><button class="action-btn" onclick="cancelAutoDetection()">关闭</button></div>';
    panel.innerHTML = html;
    return;
  }
  html += '<div class="auto-detect-note">识别到 ' + segments.length + ' 个页码区间，当前仍是预览状态。</div>';
  html += '<div class="auto-segment-list">' + segments.map(function(seg, index) {
    var evidence = seg.mapping_evidence || {};
    return '<div class="auto-segment-row"><div class="auto-segment-main">' + (index + 1) + '. ' + esc(autoMappingSegmentText(seg)) + '</div>'
      + '<div class="auto-segment-evidence">依据：' + esc(mappingMethodLabel(seg.method))
      + (evidence.inferred_offset != null ? '；稳定 offset = ' + evidence.inferred_offset : '')
      + (evidence.observed_page_numbers != null ? '；观察到 ' + evidence.observed_page_numbers + ' 个候选' : '')
      + (evidence.sequence_consistency != null ? '；序列一致性 ' + Math.round(Number(evidence.sequence_consistency) * 100) + '%' : '')
      + '</div></div>';
  }).join('') + '</div>';
  html += '<details style="margin-top:10px"><summary class="auto-detect-note">查看检测依据</summary><div class="auto-detect-note" style="margin-top:6px">'
    + 'PDF 标签 ' + Number((result.evidence_counts || {}).pdf_page_labels || 0) + ' 个；数字书签 ' + Number((result.evidence_counts || {}).numeric_bookmarks || 0)
    + ' 个；MinerU 候选 ' + Number((result.evidence_counts || {}).mineru_candidates || 0) + ' 个；页边候选 ' + Number((result.evidence_counts || {}).native_edge_candidates || 0) + ' 个。</div></details>';
  html += '<div class="auto-detect-actions">'
    + '<button class="action-btn primary" onclick="applyAutoDetection()">' + (result.manual_mapping_present ? '用自动结果替换人工映射' : '应用自动映射') + '</button>'
    + '<button class="action-btn" onclick="editAutoDetectionResult()">编辑后应用</button>'
    + '<button class="action-btn" onclick="cancelAutoDetection()">取消</button></div>';
  panel.innerHTML = html;
}

function autoFailureReasons(reasons) {
  var labels = {no_page_labels:'没有 PDF Page Labels',no_bookmarks:'没有数字书签',no_mineru_candidates:'现有 MinerU 结果没有可靠页码候选',no_edge_candidates:'页边区域未发现页码候选',sequence_not_found:'未找到稳定递增页码序列',source_missing:'原始 PDF 文件不存在'};
  return reasons.map(function(reason) { return '• ' + (labels[reason] || reason); }).join('<br>');
}

async function applyAutoDetection() {
  if (!calAutoResult) return;
  var sourceId = calSelectedSourceId;
  var segments = (calAutoResult.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; });
  if (!segments.length) return;
  var replaceManual = false;
  if (calAutoResult.manual_mapping_present) {
    replaceManual = confirm('当前文献已有人工映射。确认用本次自动检测结果替换吗？');
    if (!replaceManual) return;
  }
  try {
    showToast('正在应用自动映射…');
    var resp = await fetch('/api/auto-page-mapping/apply', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source_id:sourceId,segments:segments,auto_mapping:calAutoResult,replace_manual:replaceManual})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '应用失败');
    showToast('自动页码映射已生效');
    delete calTransientStatus[sourceId];
    libLoaded = false;
    await loadMeta();
    await refreshCalibrationSource(sourceId);
  } catch(e) {
    showToast('应用失败：' + e.message);
  }
}

function editAutoDetectionResult() {
  if (!calAutoResult) return;
  calSegments = (calAutoResult.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; }).map(function(s) {
    return Object.assign({}, s, {confidence:s.mapping_confidence || s.confidence});
  });
  renderCalSegments();
  updateCalPreview();
  document.getElementById('cal-auto-preview').style.display = 'none';
  showToast('自动结果已载入手动编辑区');
}

function cancelAutoDetection() {
  calAutoResult = null;
  document.getElementById('cal-auto-preview').style.display = 'none';
}

function segmentNumberStyleLabel(style) {
  return ({arabic:'阿拉伯数字',roman_lower:'罗马数字（小写）',roman_upper:'罗马数字（大写）',none:'无编号'})[style] || '阿拉伯数字';
}

function segmentNumberStyleControl(style, index) {
  var values = ['arabic','roman_lower','roman_upper','none'];
  return '<div class="app-select segment-style-select" id="segment-style-select-' + index + '">'
    + '<button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,\'segment-style-select-' + index + '\')"><span class="app-select-value">' + segmentNumberStyleLabel(style) + '</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>'
    + '<div class="app-select-menu" role="listbox">' + values.map(function(value) {
      return '<button class="app-select-option' + (style === value ? ' is-selected' : '') + '" type="button" data-value="' + value + '" onclick="setSegmentNumberStyle(event,' + index + ',\'' + value + '\')">' + segmentNumberStyleLabel(value) + '</button>';
    }).join('') + '</div></div>';
}

function setSegmentNumberStyle(event, index, value) {
  event.stopPropagation();
  updateCalSeg(index, 'number_style', value);
  closeAppSelects();
  renderCalSegments();
}

function renderCalSegments() {
  var body = document.getElementById('cal-segments-body');
  var noSeg = document.getElementById('cal-no-segments');
  if (calSegments.length === 0) {
    body.innerHTML = '';
    noSeg.style.display = 'block';
    document.querySelector('.segment-table-wrap').style.display = 'none';
    return;
  }
  noSeg.style.display = 'none';
  document.querySelector('.segment-table-wrap').style.display = 'block';
  body.innerHTML = calSegments.map(function(seg, i) {
    var citStart = seg.citation_page_start != null ? seg.citation_page_start : '';
    if (seg.citation === null && !citStart) citStart = '';
    var style = seg.number_style || 'arabic';
    var label = seg.label || seg.evidence || '';
    return '<tr>'
      + '<td><input class="seg-input narrow" type="number" min="1" value="' + (seg.pdf_page_start != null ? seg.pdf_page_start + 1 : '') + '" onchange="updateCalSeg(' + i + ',\'pdf_page_start\',this.value)"></td>'
      + '<td><input class="seg-input narrow" type="number" min="1" value="' + (seg.pdf_page_end != null ? seg.pdf_page_end + 1 : '') + '" onchange="updateCalSeg(' + i + ',\'pdf_page_end\',this.value)"></td>'
      + '<td><input class="seg-input narrow" type="text" value="' + esc(String(citStart)) + '" placeholder="留空=不映射" onchange="updateCalSeg(' + i + ',\'citation_page_start\',this.value)"></td>'
      + '<td>' + segmentNumberStyleControl(style, i) + '</td>'
      + '<td><input class="seg-input" type="text" value="' + esc(label) + '" placeholder="序言、正文或附录" onchange="updateCalSeg(' + i + ',\'label\',this.value)"></td>'
      + '<td><button class="seg-remove" onclick="removeCalSegment(' + i + ')" title="删除分段" aria-label="删除分段"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="m7 7 1 13h8l1-13"/><path d="M10 11v5M14 11v5"/></svg></button></td>'
      + '</tr>';
  }).join('');
}

function updateCalSeg(index, field, value) {
  var seg = calSegments[index];
  if (!seg) return;
  if (field === 'pdf_page_start' || field === 'pdf_page_end') {
    seg[field] = value === '' ? null : Math.max(0, parseInt(value, 10) - 1);
  } else if (field === 'citation_page_start') {
    if (value === '') {
      seg.citation_page_start = undefined;
      seg.citation = null;
    } else {
      seg.citation_page_start = value;
      delete seg.citation;
    }
  } else if (field === 'number_style' && value === 'none') {
    seg.number_style = 'none';
    seg.citation = null;
    delete seg.citation_page_start;
  } else {
    seg[field] = value;
  }
  if (!seg.method) seg.method = 'manual_segment';
  if (seg.confidence == null) seg.confidence = 0.9;
  updateCalPreview();
}

function addCalSegment() {
  var lastEnd = 0;
  if (calSegments.length > 0) {
    var last = calSegments[calSegments.length - 1];
    lastEnd = (last.pdf_page_end != null ? last.pdf_page_end : 0) + 1;
  }
  calSegments.push({
    pdf_page_start: lastEnd,
    pdf_page_end: lastEnd + 49,
    citation_page_start: '1',
    number_style: 'arabic',
    method: 'manual_segment',
    confidence: 0.9,
    label: ''
  });
  renderCalSegments();
  updateCalPreview();
}

function removeCalSegment(index) {
  calSegments.splice(index, 1);
  renderCalSegments();
  updateCalPreview();
}

function updateCalPreview() {
  var input = document.getElementById('cal-preview-input');
  var result = document.getElementById('cal-preview-result');
  var pageIndex = parseInt(input.value, 10) - 1;
  if (isNaN(pageIndex) || pageIndex < 0) {
    result.textContent = '—';
    return;
  }
  var mapped = null;
  var method = 'uncalibrated';
  for (var i = 0; i < calSegments.length; i++) {
    var seg = calSegments[i];
    var start = seg.pdf_page_start != null ? seg.pdf_page_start : -1;
    var end = seg.pdf_page_end != null ? seg.pdf_page_end : start;
    if (pageIndex >= start && pageIndex <= end) {
      if (seg.citation === null && !seg.citation_page_start) {
        method = seg.method || 'uncalibrated';
        mapped = null;
        break;
      }
      if (seg.citation_page_start != null && seg.citation_page_start !== '') {
        var offset = pageIndex - start;
        var style = seg.number_style || 'arabic';
        var citNum;
        try { citNum = parseInt(seg.citation_page_start, 10) + offset; } catch(e) { citNum = offset + 1; }
        if (style === 'roman_lower' || style === 'roman_upper') {
          mapped = intToRoman(citNum, style === 'roman_upper');
        } else {
          mapped = String(citNum);
        }
        method = seg.method || 'manual_segment';
        break;
      }
    }
  }
  if (mapped) {
    result.textContent = '引用' + formatCitationPageLabel({source_type:'pdf', citation_page_start:mapped}) + '（' + mappingMethodLabel(method) + '）';
    result.style.color = 'var(--accent)';
  } else {
    result.textContent = '未校准';
    result.style.color = 'var(--text-tertiary)';
  }
}

function intToRoman(num, upper) {
  if (num <= 0) return String(num);
  var vals = [[1000,'m'],[900,'cm'],[500,'d'],[400,'cd'],[100,'c'],[90,'xc'],[50,'l'],[40,'xl'],[10,'x'],[9,'ix'],[5,'v'],[4,'iv'],[1,'i']];
  var out = '';
  for (var i = 0; i < vals.length; i++) {
    while (num >= vals[i][0]) { out += vals[i][1]; num -= vals[i][0]; }
  }
  return upper ? out.toUpperCase() : out;
}

async function saveCalibration() {
  var sourceId = calSelectedSourceId;
  if (!sourceId) return;
  var hint = document.querySelector('.cal-save-hint');
  var cleanSegs = calSegments.map(function(seg) {
    var clean = {};
    if (seg.pdf_page_start != null) clean.pdf_page_start = seg.pdf_page_start;
    if (seg.pdf_page_end != null) clean.pdf_page_end = seg.pdf_page_end;
    if (seg.citation_page_start != null && seg.citation_page_start !== '') {
      clean.citation_page_start = seg.citation_page_start;
    } else {
      clean.citation = null;
    }
    if (seg.number_style) clean.number_style = seg.number_style;
    if (seg.method) clean.method = seg.method;
    if (seg.confidence != null) clean.confidence = seg.confidence;
    if (seg.label) clean.label = seg.label;
    if (seg.evidence) clean.evidence = seg.evidence;
    return clean;
  });
  try {
    hint.textContent = '正在保存并重建索引，请稍候…';
    var resp = await fetch('/api/calibration', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId, segments: cleanSegs})
    });
    var data = await resp.json();
    if (data.ok) {
      hint.textContent = '校准已生效';
      showToast('校准已保存，索引已更新');
      await loadMeta();
      delete calTransientStatus[sourceId];
      libLoaded = false;
      await refreshCalibrationSource(sourceId);
    } else {
      hint.textContent = '保存失败';
      showToast('保存失败：' + (data.error || '未知错误'));
    }
  } catch(e) {
    hint.textContent = '保存失败';
    showToast('保存失败：' + e.message);
  }
}

function scrollToManualMapping() {
  var table = document.querySelector('#library-drawer .segment-table-wrap');
  if (table) table.scrollIntoView({behavior:'smooth', block:'center'});
}

function showCalibrationEvidence() {
  var item = libSources.find(function(value) { return value.source_file_id === calSelectedSourceId; });
  if (!item) return;
  var panel = document.getElementById('cal-auto-preview');
  var evidence = item.mapping_evidence || [];
  var failures = item.failure_reasons || [];
  var html = '<div class="auto-detect-title">自动映射依据</div>';
  if (item.mapping_summary) html += '<div class="auto-detect-note">当前映射：' + esc(item.mapping_summary) + '</div>';
  html += '<div class="auto-detect-note">映射方式：' + esc(mappingMethodLabel(item.mapping_method)) + '</div>';
  if (item.mapping_confidence) html += '<div class="auto-detect-note">置信度：' + Math.round(Number(item.mapping_confidence) * 100) + '%</div>';
  if (evidence.length) html += '<div class="auto-detect-note" style="margin-top:8px">已保存 ' + evidence.length + ' 组序列、位置或结构证据。</div>';
  if (failures.length) html += '<div class="auto-detect-note" style="margin-top:8px">未使用的证据：<br>' + autoFailureReasons(failures) + '</div>';
  if (!item.mapping_summary && !evidence.length && !failures.length) html += '<div class="auto-detect-note">当前没有可显示的自动识别依据。</div>';
  panel.innerHTML = html;
  panel.style.display = 'block';
  panel.scrollIntoView({behavior:'smooth', block:'center'});
}

function openRemoveDocumentModal(sourceId) {
  if (sourceId && typeof sourceId === 'string') calSelectedSourceId = sourceId;
  var targetId = calSelectedSourceId || libSelectedId;
  var item = libSources.find(function(value) { return value.source_file_id === targetId; });
  if (!item) return;
  removeDocumentTarget = item;
  removeSecondStage = false;
  document.getElementById('remove-modal-title').textContent = '从文献库移除《' + item.title + '》？';
  document.getElementById('remove-modal-copy').textContent = '移除后，该文献将从文献库和搜索结果中消失。默认清理索引、页码映射和元数据，但保留 PDF 文件。';
  document.getElementById('remove-generated').checked = true;
  document.getElementById('remove-internal-copy').checked = false;
  document.getElementById('remove-internal-option').style.display = item.can_delete_internal_copy ? 'flex' : 'none';
  document.getElementById('remove-modal-warning').classList.remove('show');
  document.getElementById('confirm-remove-btn').textContent = '从文献库移除';
  document.getElementById('confirm-remove-btn').disabled = false;
  document.getElementById('remove-document-modal').classList.add('open');
}

function closeRemoveDocumentModal() {
  document.getElementById('remove-document-modal').classList.remove('open');
  removeDocumentTarget = null;
  removeSecondStage = false;
}

function removeModalBackdropClick(event) {
  if (event.target.id === 'remove-document-modal') closeRemoveDocumentModal();
}

async function confirmRemoveDocument() {
  if (!removeDocumentTarget) return;
  var deleteInternal = document.getElementById('remove-internal-copy').checked && removeDocumentTarget.can_delete_internal_copy;
  if (deleteInternal && !removeSecondStage) {
    removeSecondStage = true;
    document.getElementById('remove-modal-warning').classList.add('show');
    document.getElementById('confirm-remove-btn').textContent = '确认移除并删除副本';
    return;
  }
  var button = document.getElementById('confirm-remove-btn');
  button.disabled = true;
  button.textContent = '正在移除…';
  var sourceId = removeDocumentTarget.source_file_id;
  try {
    var resp = await fetch('/api/documents/remove', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,delete_generated_artifacts:document.getElementById('remove-generated').checked,delete_internal_copy:deleteInternal})});
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '移除失败');
    delete calTransientStatus[sourceId];
    closeRemoveDocumentModal();
    closeLibDrawer();
    searchDocumentsLoaded = false;
    if (searchDocumentId === sourceId) searchDocumentId = '';
    await ensureSearchDocuments(true);
    updateSearchDocumentLabel();
    await loadMeta();
    await loadLibrary();
    window.dispatchEvent(new CustomEvent('library_changed', {detail:{source_id:sourceId}}));
    var query = document.getElementById('query').value.trim();
    if (query && searchResults.some(function(item) { return item.source_file_id === sourceId; })) await runSearch();
    showToast(deleteInternal ? '文献及应用内 PDF 副本已移除' : '文献已移除，PDF 文件已保留');
  } catch(e) {
    button.disabled = false;
    button.textContent = deleteInternal ? '确认移除并删除副本' : '从文献库移除';
    showToast('移除失败：' + e.message);
  }
}

/* ═══ Appearance settings ═══ */
const THEME_OPTIONS = [
  {id:'frost-blue', name:'清霜蓝', tone:'浅色', description:'清爽理性，适合日间使用。'},
  {id:'sage-ivory', name:'鼠尾草', tone:'浅色', description:'低刺激、安静，适合长时间阅读。'},
  {id:'warm-sand', name:'暖砂金', tone:'浅色', description:'温暖柔和，带轻微纸张气质。'},
  {id:'rose-mist', name:'蔷薇雾', tone:'浅色', description:'清柔克制，带淡粉强调。'},
  {id:'lavender-purple', name:'暮云紫', tone:'浅色', description:'优雅现代，使用柔和薰衣草紫。'},
  {id:'midnight', name:'深海夜', tone:'深色', description:'低亮度深色主题，适合夜间使用。'}
];
const THEME_IDS = new Set(THEME_OPTIONS.map(function(theme) { return theme.id; }));

function themePreviewMarkup(themeId) {
  return '<span class="theme-preview" data-preview-theme="' + themeId + '" aria-hidden="true">'
    + '<span class="theme-mini-sidebar">'
    + '<span class="theme-mini-brand"><span class="theme-mini-brand-mark"></span><span class="theme-mini-brand-line"></span></span>'
    + '<span class="theme-mini-nav">'
    + '<span class="theme-mini-nav-item"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '<span class="theme-mini-nav-item is-selected"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '<span class="theme-mini-nav-item"><span class="theme-mini-nav-icon"></span><span class="theme-mini-nav-line"></span></span>'
    + '</span></span>'
    + '<span class="theme-mini-main">'
    + '<span class="theme-mini-header"><span class="theme-mini-heading"><i class="theme-mini-title-line"></i><i class="theme-mini-subtitle-line"></i></span><span class="theme-mini-header-status"><i></i><b></b></span></span>'
    + '<span class="theme-mini-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><span class="theme-mini-search-line"></span><span class="theme-mini-search-action"></span></span>'
    + '<span class="theme-mini-cards">'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-success"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-doc-meta"></i></span>'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-danger"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-match"></i></span>'
    + '<span class="theme-mini-doc-card"><span class="theme-mini-card-top"><i class="theme-mini-source"></i><i class="theme-mini-state is-success"></i></span><i class="theme-mini-doc-title"></i><i class="theme-mini-doc-title is-short"></i><i class="theme-mini-doc-meta"></i></span>'
    + '</span></span></span>';
}

function themeOptionMarkup(theme) {
  return '<button class="theme-option" type="button" data-theme-choice="' + theme.id + '" role="radio" aria-checked="false" onclick="setTheme(\'' + theme.id + '\')">'
    + '<span class="theme-option-head"><span class="theme-option-identity"><span class="theme-option-name">' + theme.name + '</span><span class="theme-option-tone">' + theme.tone + '</span></span>'
    + '<span class="theme-option-check" aria-hidden="true"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4L19 6"/></svg></span></span>'
    + themePreviewMarkup(theme.id)
    + '<span class="theme-option-description">' + theme.description + '</span></button>';
}

function renderThemeOptions() {
  var container = document.getElementById('theme-options');
  if (!container) return;
  container.innerHTML = THEME_OPTIONS.map(themeOptionMarkup).join('');
  renderThemeSelection();
}

function renderThemeSelection() {
  document.querySelectorAll('.theme-option').forEach(function(option) {
    var selected = option.dataset.themeChoice === currentTheme;
    option.classList.toggle('selected', selected);
    option.setAttribute('aria-checked', selected ? 'true' : 'false');
  });
}

function applyTheme(theme) {
  if (!THEME_IDS.has(theme)) return;
  currentTheme = theme;
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('meFinderTheme', theme); } catch (_) {}
  renderThemeSelection();
}

async function loadPreferences() {
  renderThemeSelection();
  try {
    var resp = await fetch('/api/preferences');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
    applyTheme(data.theme || 'frost-blue');
    if (data.library_view === 'list' || data.library_view === 'grid') libViewMode = data.library_view;
    else if (data.calibration_view === 'list' || data.calibration_view === 'grid') libViewMode = data.calibration_view;
    syncLibraryViewButtons();
    if (libLoaded) renderLibraryList();
    preferencesLoaded = true;
  } catch (e) {
    showToast('读取外观设置失败：' + e.message);
  }
}

function persistDisplayPreference(key, value) {
  var payload = {};
  payload[key] = value;
  fetch('/api/preferences', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).catch(function() {});
}

async function setTheme(theme) {
  if (!THEME_IDS.has(theme)) return;
  var previousTheme = currentTheme;
  applyTheme(theme);
  try {
    var resp = await fetch('/api/preferences', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({theme: theme})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    preferencesLoaded = true;
    applyTheme(data.theme);
    var selected = THEME_OPTIONS.find(function(option) { return option.id === theme; });
    showToast('已切换到' + (selected ? selected.name : '所选主题'));
  } catch (e) {
    applyTheme(previousTheme);
    showToast('主题保存失败：' + e.message);
  }
}

renderThemeOptions();

/* ═══ MinerU API settings ═══ */
async function loadMineruConfig() {
  var status = document.getElementById('mineru-config-status');
  var expiryDetail = document.getElementById('mineru-expiry-detail');
  if (!status) return;
  status.className = 'settings-status';
  status.textContent = '读取中…';
  if (expiryDetail) {
    expiryDetail.className = 'settings-expiry-detail';
    expiryDetail.textContent = 'API 到期时间：读取中…';
  }
  try {
    var resp = await fetch('/api/mineru-config');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
    document.getElementById('mineru-api-base').value = data.api_base || 'https://mineru.net';
    document.getElementById('mineru-expires-at').value = data.expires_at || '';
    document.getElementById('mineru-token').value = '';
    document.getElementById('mineru-access-key-id').value = '';
    document.getElementById('mineru-secret-access-key').value = '';
    if (data.configured) {
      status.className = 'settings-status ready';
      var parts = ['已配置'];
      if (data.expires_at) parts.push('到期 ' + data.expires_at);
      status.textContent = parts.join(' · ');
    } else {
      status.className = 'settings-status warning';
      status.textContent = '尚未配置 Token';
    }
    updateMineruExpiryDetail(data);
    mineruConfigLoaded = true;
  } catch (e) {
    status.className = 'settings-status warning';
    status.textContent = '读取失败';
    if (expiryDetail) {
      expiryDetail.className = 'settings-expiry-detail warning';
      expiryDetail.textContent = 'API 到期时间：读取失败';
    }
    showToast('读取 MinerU 配置失败：' + e.message);
  }
}

function updateMineruExpiryDetail(data) {
  var el = document.getElementById('mineru-expiry-detail');
  if (!el) return;
  var configured = !!(data && data.configured);
  var status = data ? data.expiry_status : 'unset';
  var label = data && data.expiry_label ? data.expiry_label : '';
  el.className = 'settings-expiry-detail';
  if (!configured) {
    el.classList.add('warning');
    el.textContent = 'API 到期时间：尚未配置 Token';
    return;
  }
  if (status === 'expired' || status === 'invalid') {
    el.classList.add('error');
  } else if (status === 'expires_today' || status === 'unset') {
    el.classList.add('warning');
  } else {
    el.classList.add('ready');
  }
  el.textContent = 'API 到期时间：' + (label || '未设置到期时间');
}

function toggleMineruSecret(inputId, buttonId) {
  var input = document.getElementById(inputId);
  var button = document.getElementById(buttonId);
  if (!input || !button) return;
  var visible = input.type === 'text';
  input.type = visible ? 'password' : 'text';
  button.textContent = visible ? '显示' : '隐藏';
}

async function saveMineruConfig() {
  var hint = document.getElementById('mineru-save-hint');
  var payload = {
    token: document.getElementById('mineru-token').value.trim(),
    access_key_id: document.getElementById('mineru-access-key-id').value.trim(),
    secret_access_key: document.getElementById('mineru-secret-access-key').value.trim(),
    api_base: document.getElementById('mineru-api-base').value.trim(),
    expires_at: document.getElementById('mineru-expires-at').value
  };
  hint.textContent = '正在保存…';
  try {
    var resp = await fetch('/api/mineru-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    hint.textContent = '已保存到本机';
    showToast('MinerU API 配置已保存');
    mineruConfigLoaded = false;
    await loadMineruConfig();
  } catch (e) {
    hint.textContent = '保存失败';
    showToast('保存 MinerU 配置失败：' + e.message);
  }
}

/* ═══ Import ═══ */
let importQueue = [];

function initDropZone() {
  var zone = document.getElementById('drop-zone');
  if (!zone) return;
  zone.addEventListener('dragover', function(e) {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', function() {
    zone.classList.remove('dragover');
  });
  zone.addEventListener('drop', function(e) {
    e.preventDefault();
    zone.classList.remove('dragover');
    handleFileSelect(e.dataTransfer.files);
  });
}

function handleFileSelect(files) {
  if (!files || files.length === 0) return;
  var validExts = ['.pdf', '.docx'];
  for (var i = 0; i < files.length; i++) {
    var file = files[i];
    var ext = file.name.toLowerCase().slice(file.name.lastIndexOf('.'));
    if (validExts.indexOf(ext) === -1) {
      showToast('不支持的格式: ' + file.name);
      continue;
    }
    var id = 'import-' + Date.now() + '-' + i;
    importQueue.push({
      id: id,
      file: file,
      name: file.name,
      size: file.size,
      type: ext === '.pdf' ? 'pdf' : 'docx',
      status: 'queued',
      step: 0,
      message: '等待处理'
    });
  }
  document.getElementById('file-input').value = '';
  renderImportQueue();
  importQueue.filter(function(q) { return q.status === 'queued'; }).forEach(function(q) {
    uploadImport(q.id);
  });
}

function importStepsFor(q) {
  if (q.type !== 'pdf') return ['读取文件', '文本入库', '建立索引'];
  if (q.route === 'mineru') return ['读取文件', '类型检测', 'MinerU 解析', '文本入库', '建立索引'];
  return ['读取文件', '类型检测', '本地解析', '建立索引'];
}

function importRouteBadge(q) {
  if (q.type !== 'pdf' || !q.detectedType) return '';
  var mineru = q.route === 'mineru';
  return '<span class="import-route-badge ' + (mineru ? 'mineru' : 'native') + '">'
    + esc(pdfTypeLabel(q.detectedType)) + (mineru ? ' · 提交 MinerU' : ' · 本地解析')
    + '</span>';
}

function renderImportQueue() {
  var queueEl = document.getElementById('import-queue');
  var itemsEl = document.getElementById('import-items');
  if (importQueue.length === 0) {
    queueEl.style.display = 'none';
    return;
  }
  queueEl.style.display = 'block';
  itemsEl.innerHTML = importQueue.map(function(q) {
    var typeCls = q.type === 'pdf' ? 'pdf' : 'word';
    var steps = importStepsFor(q);
    var stepsHTML = steps.map(function(label, i) {
      var cls = '';
      if (q.status === 'error' && i === q.step) cls = 'error';
      else if (i < q.step) cls = 'done';
      else if (i === q.step && q.status === 'processing') cls = 'active';
      return '<div class="import-step ' + cls + '">'
        + '<div class="import-step-bar ' + cls + '"></div>'
        + '<span class="import-step-label">' + label + '</span>'
        + '</div>';
    }).join('');
    var statusCls = q.status === 'error' ? ' error' : q.status === 'done' ? ' done' : '';
    return '<div class="import-item" data-id="' + q.id + '">'
      + '<div class="import-item-header">'
      + '<span class="type-badge ' + typeCls + '">' + (q.type === 'pdf' ? 'PDF' : 'DOCX') + '</span>'
      + '<span class="import-item-name">' + esc(q.name) + '</span>'
      + importRouteBadge(q)
      + '<span class="import-item-size">' + formatFileSize(q.size) + '</span>'
      + '<button class="import-item-remove" onclick="removeImport(\'' + q.id + '\')" title="移除">&times;</button>'
      + '</div>'
      + '<div class="import-steps">' + stepsHTML + '</div>'
      + '<div class="import-item-status' + statusCls + '">' + esc(q.message) + '</div>'
      + '</div>';
  }).join('');
}

function removeImport(id) {
  importQueue = importQueue.filter(function(q) { return q.id !== id; });
  renderImportQueue();
}

async function uploadImport(id) {
  var q = importQueue.find(function(q) { return q.id === id; });
  if (!q) return;
  q.status = 'processing';
  q.step = 0;
  q.message = '正在读取文件…';
  renderImportQueue();
  try {
    var resp = await fetch('/api/import', {
      method: 'POST',
      headers: {
        'Content-Type': q.file.type || (q.type === 'pdf' ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        'X-File-Name': encodeURIComponent(q.name)
      },
      body: q.file
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
    q.jobId = data.job_id;
    if (q.type === 'pdf' && data.detected_pdf_type) {
      q.detectedType = data.detected_pdf_type;
      q.route = data.detected_pdf_type === 'native_text' ? 'native' : 'mineru';
      q.step = 2;
      q.message = '检测结果：' + pdfTypeLabel(data.detected_pdf_type)
        + (q.route === 'mineru' ? '，将提交 MinerU 解析' : '，本地解析，无需 MinerU');
    } else {
      q.step = 1;
      q.message = '文件已保存，正在建立索引…';
    }
    renderImportQueue();
    if (q.jobId) pollImportJob(q.id);
  } catch (e) {
    q.status = 'error';
    q.message = e.message || '导入失败';
    renderImportQueue();
  }
}

function pollImportJob(id) {
  var q = importQueue.find(function(item) { return item.id === id; });
  if (!q || !q.jobId) return;
  fetch('/api/import-status?job_id=' + encodeURIComponent(q.jobId))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data.error) throw new Error(data.error);
      if (data.phase === 'mineru_submitting' || data.phase === 'mineru_processing') q.route = 'mineru';
      else if (data.phase === 'text_parsing' && q.type === 'pdf') q.route = 'native';
      var steps = importStepsFor(q);
      if (data.phase === 'mineru_submitting' || data.phase === 'mineru_processing') q.step = steps.indexOf('MinerU 解析');
      else if (data.phase === 'text_parsing') q.step = q.type === 'pdf' ? steps.indexOf('本地解析') : steps.indexOf('文本入库');
      else if (data.phase === 'rebuilding_index' || data.phase === 'metadata_recognition') q.step = steps.indexOf('建立索引');
      else if (data.status === 'completed') q.step = steps.length;
      q.message = data.message || q.message;
      if (data.status === 'completed') {
        q.status = 'done';
        q.message = data.message || '导入完成，已自动更新索引';
        libLoaded = false;
        searchDocumentsLoaded = false;
        ensureSearchDocuments(true).then(updateSearchDocumentLabel);
      } else if (data.status === 'failed') {
        q.status = 'error';
        q.message = data.message || '导入失败';
      }
      renderImportQueue();
      if (q.status === 'processing') setTimeout(function() { pollImportJob(id); }, 2500);
    })
    .catch(function(err) {
      q.status = 'error';
      q.message = err.message || '读取导入状态失败';
      renderImportQueue();
    });
}

/* ═══ Load index metadata ═══ */
async function loadMeta() {
  try {
    const resp = await fetch('/api/index-meta');
    const meta = await resp.json();
    document.getElementById('index-count').textContent =
      '索引 ' + (meta.eligible_paragraph_count || 0).toLocaleString() + ' 条';
  } catch (e) {
    document.getElementById('index-count').textContent = '索引状态未知';
  }
}

/* ═══ Init ═══ */
document.addEventListener('click', function(event) {
  if (!event.target.closest('.app-select')) closeAppSelects();
});
loadMeta();
loadPreferences();
syncLibraryViewButtons();
renderSearchDocumentOptions();
ensureSearchDocuments().then(function() { renderSearchDocumentOptions(); updateSearchDocumentLabel(); });
initDropZone();
const requestedInitialPage = new URLSearchParams(window.location.search).get('page');
const initialPage = requestedInitialPage === 'calibration' ? 'library' : requestedInitialPage;
if (['search','library','import','settings'].indexOf(initialPage) >= 0) navigateTo(initialPage);
if (currentPage === 'search') document.getElementById('query').focus();
