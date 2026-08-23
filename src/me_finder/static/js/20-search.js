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
  } else if (shouldOpen) {
    // Keyboard entry point (N4): land on the selected/first option so ArrowUp/Down
    // works immediately. focus-visible keeps the ring off for mouse users.
    var firstOpt = select.querySelector('.app-select-option.is-selected')
      || select.querySelector('.app-select-option');
    if (firstOpt) requestAnimationFrame(function() { firstOpt.focus(); });
  }
}

async function toggleSearchSelect(event, selectId) { return toggleAppSelect(event, selectId); }

/* ═══ Keyboard for custom .app-select listboxes (N4) ═══
   Options are native <button>s, so Tab+Enter already activates them. The
   role="listbox"/role="option" ARIA, however, promises arrow-key roving that
   never existed. This document-level handler fulfils that contract for every
   .app-select at once — Arrow/Home/End move the roving focus, Esc closes and
   returns focus to the trigger. Menus with a search field keep native typing. */
function appSelectOptionList(select) {
  return Array.prototype.filter.call(
    select.querySelectorAll('.app-select-option'),
    function(o) { return o.offsetParent !== null && !o.disabled; }
  );
}
document.addEventListener('keydown', function(e) {
  var open = document.querySelector('.app-select.is-open');
  if (!open) return;
  var target = e.target;
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) {
    if (e.key === 'Escape') {
      closeAppSelects();
      var t0 = open.querySelector('.app-select-trigger');
      if (t0) t0.focus();
    }
    return;
  }
  if (!open.contains(target)) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    closeAppSelects();
    var trig = open.querySelector('.app-select-trigger');
    if (trig) trig.focus();
    return;
  }
  var opts = appSelectOptionList(open);
  if (!opts.length) return;
  var cur = opts.indexOf(document.activeElement);
  var next = null;
  if (e.key === 'ArrowDown') next = cur < 0 ? 0 : Math.min(cur + 1, opts.length - 1);
  else if (e.key === 'ArrowUp') next = cur < 0 ? opts.length - 1 : Math.max(cur - 1, 0);
  else if (e.key === 'Home') next = 0;
  else if (e.key === 'End') next = opts.length - 1;
  if (next !== null) { e.preventDefault(); opts[next].focus(); }
});

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

/* ═══ Shared library catalog ═══ */
function invalidateLibraryCatalog() {
  libraryCatalog = null;
  libraryCatalogPromise = null;
  libLoaded = false;
  searchDocumentsLoaded = false;
  libDetailLoaded = {};
  libDetailPending = {};
  libWorks = [];
}

// 摘要投影不含映射证据、PDF 剖面与收录作品，详情由 ensureLibraryDetail 按需补齐。
function fetchLibraryCatalog(force) {
  if (force) invalidateLibraryCatalog();
  if (libraryCatalog) return Promise.resolve(libraryCatalog);
  if (libraryCatalogPromise) return libraryCatalogPromise;
  libraryCatalogPromise = fetch('/api/library?view=summary').then(function(response) {
    return response.json().then(function(data) {
      if (!response.ok || data.error) throw new Error(data.error || '文献库加载失败');
      libraryCatalog = data;
      return data;
    });
  }).catch(function(error) {
    libraryCatalogPromise = null;
    throw error;
  });
  return libraryCatalogPromise;
}


function volumeForSource(sourceId) {
  return libVolumeBySource.get(sourceId) || null;
}

async function ensureSearchDocuments(force) {
  if (searchDocumentsLoaded && !force) return;
  var options = document.getElementById('document-options');
  if (options) options.innerHTML = '<div class="document-options-empty">正在读取文献库…</div>';
  try {
    var data = await fetchLibraryCatalog(force);
    searchSourceFiles = data.items || [];
    searchVolumes = data.volumes || [];
    libVolumeBySource = buildVolumeIndex(searchVolumes);
    if (typeof loadDocumentGroups === 'function') await loadDocumentGroups();
    searchDocumentsLoaded = true;
  } catch (error) {
    searchDocumentsLoaded = false;
    if (options) options.innerHTML = '<div class="document-options-empty">文献列表读取失败</div>';
  }
}

function searchDocumentView(source) {
  var volume = volumeForSource(source.source_file_id);
  var bib = source.bibliographic || source.bibliographic_metadata || {};
  var title = source.title || bib.title || (volume && volume.display_title) || source.display_title || source.file_name || source.source_file_id;
  var author = source.author || bib.author || '';
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
  var noScope = !searchDocumentId && !searchGroupId;
  var allOption = '<button class="app-select-option' + (noScope ? ' is-selected' : '') + '" type="button" onclick="selectSearchScopeAll(event)"><span>全部文献</span>' + (noScope ? check : '') + '</button>';
  var groupsHtml = '';
  if (typeof libDocumentGroups !== 'undefined' && libDocumentGroups.length) {
    groupsHtml = '<div class="document-options-head">作品组</div>' + libDocumentGroups.map(function(group) {
      var selected = group.document_group_id === searchGroupId;
      var count = (group.members || []).length;
      return '<button class="app-select-option' + (selected ? ' is-selected' : '') + '" type="button" onclick="selectSearchGroup(event,\'' + esc(group.document_group_id) + '\')"><span class="document-option-main"><span class="document-option-title">' + esc(group.title) + '</span><span class="document-option-meta">' + count + ' 个版本</span></span>' + (selected ? check : '') + '</button>';
    }).join('');
  }
  if (!sources.length && !groupsHtml) {
    options.innerHTML = allOption + '<div class="document-options-empty">没有符合条件的文献</div>';
    return;
  }
  var singleHead = sources.length ? '<div class="document-options-head">单篇文献</div>' : '';
  options.innerHTML = allOption + groupsHtml + singleHead + sources.map(function(source) {
    var view = searchDocumentView(source);
    var selected = !searchGroupId && source.source_file_id === searchDocumentId;
    return '<button class="app-select-option' + (selected ? ' is-selected' : '') + '" type="button" data-value="' + esc(source.source_file_id) + '" onclick="selectSearchDocument(event,this.dataset.value)"><span class="document-option-main"><span class="document-option-title">' + esc(view.title) + '</span><span class="document-option-meta">' + esc([view.sourceType, view.author].filter(Boolean).join(' · ')) + '</span></span>' + (selected ? check : '') + '</button>';
  }).join('');
}

function selectSearchDocument(event, sourceId) {
  event.stopPropagation();
  searchDocumentId = sourceId || '';
  searchGroupId = '';  // single-source and group scope are mutually exclusive
  updateSearchDocumentLabel();
  closeSearchSelects();
  rerunSearchAfterFilterChange();
}

function selectSearchGroup(event, groupId) {
  event.stopPropagation();
  searchGroupId = groupId || '';
  searchDocumentId = '';
  updateSearchDocumentLabel();
  closeSearchSelects();
  rerunSearchAfterFilterChange();
}

function selectSearchScopeAll(event) {
  event.stopPropagation();
  searchGroupId = '';
  searchDocumentId = '';
  updateSearchDocumentLabel();
  closeSearchSelects();
  rerunSearchAfterFilterChange();
}

function updateSearchDocumentLabel() {
  var label = document.getElementById('document-select-label');
  if (!label) return;
  if (searchGroupId && typeof libDocumentGroups !== 'undefined') {
    var group = libDocumentGroups.find(function(g) { return g.document_group_id === searchGroupId; });
    if (group) {
      label.textContent = group.title + ' · ' + (group.members || []).length + ' 个版本';
      label.title = group.title;
      return;
    }
  }
  var source = searchSourceFiles.find(function(item) { return item.source_file_id === searchDocumentId; });
  label.textContent = source ? searchDocumentView(source).title : '全部文献';
  label.title = source ? searchDocumentView(source).title : '';
}

/* ═══ Search ═══ */
async function runSearch() {
  const query = document.getElementById('query').value.trim();
  if (!query) return;
  const seq = ++searchSeq;  // 只有最后一次发起的检索能写回结果，避免慢响应覆盖新结果
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
      body: JSON.stringify(Object.assign(
        {query, mode: currentMode, limit: searchLimit, source_type: searchSourceType},
        searchGroupId
          ? {document_group_id: searchGroupId}
          : {source_file_id: searchDocumentId || null}
      ))
    });
    const data = await resp.json();
    if (seq !== searchSeq) return;  // 已有更新的检索发起，丢弃这次过期响应
    if (!resp.ok || data.error) throw new Error(data.error || ('HTTP ' + resp.status));
    searchResults = data.results || [];
    if (data.total_is_exact === false || data.has_more) {
      statusEl.textContent = '显示前 ' + searchResults.length + ' 条匹配结果，还有更多';
    } else {
      statusEl.textContent = '找到 ' + data.total + ' 条候选，显示 ' + searchResults.length + ' 条';
    }

    if (searchResults.length === 0) {
      listEl.innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">未找到匹配结果</div><div class="empty-state-hint">尝试更短的引文或切换为模糊检索</div></div>';
      return;
    }

    listEl.innerHTML = searchResults.map((item, i) => resultRowHTML(item, i)).join('');
    selectResult(0, false);
  } catch (err) {
    if (seq !== searchSeq) return;
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

function searchResultsArea() {
  return document.querySelector('#page-search .results-area');
}

function showSearchResultsList() {
  closeAppSelects();
  const area = searchResultsArea();
  if (area) area.classList.remove('is-detail-open');
  const row = document.querySelector('.result-row[data-index="' + selectedIndex + '"]');
  if (row) row.scrollIntoView({block: 'nearest'});
}

function showSearchResultDetail() {
  const area = searchResultsArea();
  if (area) area.classList.add('is-detail-open');
}

function selectResult(index, openNarrowDetail) {
  if (index < 0 || index >= searchResults.length) return;
  selectedIndex = index;
  document.querySelectorAll('.result-row').forEach((row, i) => {
    row.classList.toggle('selected', i === index);
  });
  const item = searchResults[index];
  showDetail(item);
  if (openNarrowDetail !== false) showSearchResultDetail();

  const row = document.querySelector('.result-row[data-index="' + index + '"]');
  if (row) row.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}






function refreshDetailContextToggles(panel) {
  if (!panel) return;
  panel.querySelectorAll('.detail-context-toggle').forEach(function(button) {
    if (button.getAttribute('aria-expanded') === 'true') {
      button.hidden = false;
      return;
    }
    const contentId = button.getAttribute('aria-controls');
    const content = contentId ? document.getElementById(contentId) : null;
    const preview = content ? content.querySelector('.detail-context-preview') : null;
    const characterTruncated = button.dataset.characterTruncated === 'true';
    const lineTruncated = !!preview && preview.scrollHeight > preview.clientHeight + 1;
    button.hidden = !(characterTruncated || lineTruncated);
  });
}

function observeDetailContextLayout(panel) {
  if (detailContextResizeObserver) {
    detailContextResizeObserver.disconnect();
    detailContextResizeObserver = null;
  }
  if (!panel || typeof ResizeObserver !== 'function') return;
  const detailScroll = panel.querySelector('.detail-scroll');
  if (!detailScroll) return;
  detailContextResizeObserver = new ResizeObserver(function() {
    refreshDetailContextToggles(panel);
  });
  detailContextResizeObserver.observe(detailScroll);
}

function toggleDetailContext(button) {
  const contentId = button.getAttribute('aria-controls');
  const content = contentId ? document.getElementById(contentId) : null;
  if (!content) return;
  const expanded = button.getAttribute('aria-expanded') !== 'true';
  const preview = content.querySelector('.detail-context-preview');
  const full = content.querySelector('.detail-context-full');
  button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  button.textContent = expanded ? '收起' : '展开';
  button.setAttribute('aria-label', (expanded ? '收起' : '展开') + (button.dataset.contextLabel || '上下文'));
  content.classList.toggle('is-expanded', expanded);
  if (preview) preview.hidden = expanded;
  if (full) full.hidden = !expanded;
  if (!expanded) {
    requestAnimationFrame(function() {
      refreshDetailContextToggles(button.closest('#detail-panel'));
    });
  }
}

function showDetail(item) {
  const panel = document.getElementById('detail-panel');
  const title = esc(item.document_title || item.work_title || item.volume_display || '');
  const author = item.author_label ? esc(item.author_label) : '';
  const pageLabel = formatCitationPageLabel(item);
  const page = esc(pageLabel);
  const sourceLabel = item.source_type === 'pdf' ? 'PDF' : 'Word';

  const contextBefore = detailContextHTML(item.context_before, 'before');
  const contextAfter = detailContextHTML(item.context_after, 'after');

  let pageDetail = '';
  if (item.source_type === 'pdf') {
    pageDetail = '<div class="page-detail-toggle" onclick="togglePageDetail(this)">页码详情 ▸</div>'
      + '<div class="page-detail-body">'
      + pdRow('引用页码', pageLabel)
      + pdRow('PDF 页码标签', item.pdf_page_start_label || '无')
      + pdRow('PDF 物理页', item.pdf_page_start_index != null ? 'PDF 第 ' + (item.pdf_page_start_index + 1) + ' 页' : '—')
      + (item.layout_mode === 'spread' ? pdRow('双开位置', logicalPageSideLabel(item.logical_page_side, item.spread_hit_precision)) : '')
      + pdRow('映射方式', mappingMethodLabel(item.page_mapping_method))
      + (item.mapping_confidence_level ? pdRow('映射置信度', mappingConfidenceLabel(item.mapping_confidence_level, item.page_mapping_confidence)) : '')
      + (item.page_scope ? pdRow('页码范围', pageScopeLabel(item.page_scope)) : '')
      + (item.mapping_evidence ? pdRow('映射依据', mappingEvidenceSummary(item.mapping_evidence)) : '')
      + (item.is_cross_page ? pdRow('跨页命中', '是') : '')
      + '</div>';
  }

  const citationStyleLabel = citationStyleDisplayLabel(citationStyle);
  const detailMenuChevron = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>';

  panel.innerHTML = '<div class="detail-card">'
    + '<div class="detail-mobile-toolbar">'
    + '<button class="detail-back-button" type="button" onclick="showSearchResultsList()"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 5-5 5 5 5"/><path d="M7 10h8"/></svg><span>返回结果列表</span></button>'
    + '</div>'
    + '<div class="detail-scroll">'
    + '<div class="detail-header">'
    + '<div class="detail-title">' + title + '</div>'
    + (author ? '<div class="detail-author">' + author + '</div>' : '')
    + '<div class="detail-pills">'
    + '<span class="detail-pill">' + sourceLabel + '</span>'
    + (item.volume_display ? '<span class="detail-pill">' + esc(item.volume_display) + '</span>' : '')
    + '<span class="detail-pill">' + page + '</span>'
    + '</div>'
    + pageDetail
    + citationAvailabilityMarkup(item)
    + '</div>'
    + '<div class="detail-body">'
    + contextBefore
    + '<div class="detail-hit">' + (item.highlighted_html || esc(item.paragraph_text || '')) + '</div>'
    + contextAfter
    + '</div>'
    + '</div>'
    + '<div class="detail-actions" role="toolbar" aria-label="检索结果操作">'
    + '<span class="app-select detail-format-control" id="detail-format-control">'
    + '<button class="action-btn app-select-trigger detail-format-trigger" type="button" aria-label="选择出处格式" aria-haspopup="menu" aria-expanded="false" onclick="toggleAppSelect(event,\'detail-format-control\')"><span id="detail-citation-style-label">' + citationStyleLabel + '</span>' + detailMenuChevron + '</button>'
    + '<span class="app-select-menu detail-format-menu" role="menu" aria-label="出处格式">'
    + '<span class="detail-citation-style-options" id="citation-style-control">' + citationStyleMenuMarkup() + '</span>'
    + '</span>'
    + '</span>'
    + '<button class="action-btn" type="button" onclick="copySelectedCitation()">复制出处</button>'
    + (item.source_file_id ? '<button class="action-btn" type="button" onclick="openSelectedStructuredReader()">查看结构化文本</button>' : '')
    + (item.source_file_id ? '<button class="action-btn primary" type="button" onclick="openSource(\'' + esc(item.source_file_id) + '\',' + (item.pdf_page_start_index != null ? item.pdf_page_start_index + 1 : 'null') + ')">打开原文</button>' : '')
    + '</div>'
    + '</div>';

  observeDetailContextLayout(panel);
  requestAnimationFrame(function() {
    refreshDetailContextToggles(panel);
    const hit = panel.querySelector('.detail-hit');
    if (!hit) return;
    hit.classList.remove('is-locating');
    void hit.offsetWidth;
    hit.classList.add('is-locating');
    const detailScroll = panel.querySelector('.detail-scroll');
    if (!detailScroll) return;
    const paneRect = detailScroll.getBoundingClientRect();
    const hitRect = hit.getBoundingClientRect();
    if (hitRect.top < paneRect.top + 16 || hitRect.bottom > paneRect.bottom - 16) {
      hit.scrollIntoView({block: 'center', behavior: 'smooth'});
    }
  });
}

window.addEventListener('resize', function() {
  refreshDetailContextToggles(document.getElementById('detail-panel'));
});

function showEmptyDetail() {
  if (detailContextResizeObserver) {
    detailContextResizeObserver.disconnect();
    detailContextResizeObserver = null;
  }
  showSearchResultsList();
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
function isSearchShortcutInteractiveTarget(target) {
  if (!target || typeof target.closest !== 'function') return false;
  return !!target.closest(
    'button, input, textarea, select, summary, a[href], [role="button"], [role="option"], [role="listbox"], [role="menuitem"], [role="switch"], [contenteditable]:not([contenteditable="false"])'
  );
}

document.addEventListener('keydown', function(e) {
  if (currentPage !== 'search') return;
  if (e.target && e.target.id === 'document-filter-query') {
    if (e.key === 'Escape') closeSearchSelects();
    return;
  }
  if ((!e.target || e.target.id !== 'query') && isSearchShortcutInteractiveTarget(e.target)) return;
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

function truncateHTML(html, maxText) {
  const div = document.createElement('div');
  div.innerHTML = html;
  if ((div.textContent || '').length <= maxText) return html;
  // 按可见字符截断，同时保留高亮标签（服务端只产出扁平的 <mark> 包裹）。
  let remaining = maxText;
  let out = '';
  for (const node of div.childNodes) {
    if (remaining <= 0) break;
    if (node.nodeType === Node.TEXT_NODE) {
      const slice = node.nodeValue.slice(0, remaining);
      out += esc(slice);
      remaining -= slice.length;
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = node.tagName.toLowerCase();
      const slice = (node.textContent || '').slice(0, remaining);
      out += '<' + tag + '>' + esc(slice) + '</' + tag + '>';
      remaining -= slice.length;
    }
  }
  return out + '…';
}

// mappingMethodLabel / mappingStatusLabel / mappingConfidenceLabel / pageScopeLabel /
// logicalPageSideLabel / mappingEvidenceSummary / autoMappingSegmentText /
// firstPageValue 已抽到 06-pure.js（纯逻辑，可单测）。

// isUncalibratedPageLabel / formatChinesePageRange / formatCitationPageLabel
// 已抽到 06-pure.js（纯逻辑，可单测）。

function selectedResult() {
  if (selectedIndex < 0 || selectedIndex >= searchResults.length) return null;
  return searchResults[selectedIndex];
}

function citationStyleDisplayLabel(style) {
  var option = CITATION_STYLE_OPTIONS.find(function(item) { return item.id === style; });
  return option ? option.label : '中文脚注';
}

function setCitationStyle(style, persist) {
  citationStyle = enabledCitationStyles.indexOf(style) >= 0 ? style : enabledCitationStyles[0];
  try { localStorage.setItem('meFinderCitationStyle', citationStyle); } catch (_) {}
  if (persist) persistSelectedCitationStyle();
}

function loadLocalSelectedCitationStyle() {
  try {
    var value = localStorage.getItem('meFinderCitationStyle');
    return CITATION_STYLE_IDS.has(value) ? value : null;
  } catch (_) {
    return null;
  }
}

function persistSelectedCitationStyle() {
  fetch('/api/preferences', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({citation_style: citationStyle})
  }).catch(function() {
    // localStorage remains a cross-platform fallback if the backend is old or
    // temporarily unavailable.
  });
}

function citationStyleMenuMarkup() {
  return CITATION_STYLE_OPTIONS.filter(function(option) {
    return enabledCitationStyles.indexOf(option.id) >= 0;
  }).map(function(option) {
    return '<button class="app-select-option' + (citationStyle === option.id ? ' is-selected' : '')
      + '" type="button" data-value="' + option.id + '" onclick="selectCitationStyle(event,\''
      + option.id + '\')">' + option.label + '</button>';
  }).join('');
}

function selectCitationStyle(event, style) {
  event.stopPropagation();
  setCitationStyle(style, true);
  var label = document.getElementById('detail-citation-style-label') || document.getElementById('citation-style-label');
  if (label) label.textContent = citationStyleDisplayLabel(citationStyle);
  document.querySelectorAll('#citation-style-control .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === citationStyle);
  });
  updateDetailCitationAvailability();
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

function citationAvailabilityMarkup(item) {
  const hidden = citationIsComplete(item) ? ' hidden' : '';
  return '<div class="detail-citation-status" id="detail-citation-status" role="status"' + hidden + '>'
    + '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M10 6.5v4.25"/><path d="M10 14h.01"/></svg>'
    + '<span><strong>出处信息不完整</strong><span>暂不可生成完整引文；仍可查看正文和打开原文。</span></span>'
    + '</div>';
}

function updateDetailCitationAvailability() {
  const status = document.getElementById('detail-citation-status');
  const item = selectedResult();
  if (!status || !item) return;
  status.hidden = citationIsComplete(item);
}

function showCitationMetadataError(item) {
  const formats = item && item.citation_formats ? item.citation_formats : {};
  const missing = formats[citationStyle + '_missing_fields'] || [];
  const labels = {author:'作者',title:'书名',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份',journal_name:'出版刊物',issue:'期号',citation_page:'引用页码'};
  showToast('无法复制：缺少' + missing.map(function(x){return labels[x] || x;}).join('、'));
}

function copySelectedCitation() {
  const item = selectedResult();
  if (!item) return;
  if (!citationIsComplete(item)) { showCitationMetadataError(item); return; }
  copyText(citationForItem(item));
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => showToast('已复制', 'success')).catch(() => showToast('复制失败', 'danger'));
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
    if (data.fallback && data.page) showToast('内置阅读器暂时不可用，已改用预览；请手动翻到 PDF 第 ' + data.page + ' 页', 'warning');
    else if (data.page_adjusted) showToast('请求页超出当前 PDF 范围，已定位到第 ' + data.page + ' 页' + (data.page_count ? '（共 ' + data.page_count + ' 页）' : ''), 'warning');
    else if (data.page && data.page_jump) showToast('已打开原文并跳转到 PDF 第 ' + data.page + ' 页', 'success');
    else if (data.page && data.app === 'preview') showToast('已用 macOS 预览打开，请手动翻到 PDF 第 ' + data.page + ' 页', 'warning');
    else if (data.page) showToast('已用系统默认阅读器打开，请手动翻到 PDF 第 ' + data.page + ' 页', 'warning');
    else showToast('已打开原文', 'success');
  } catch(e) {
    showToast(e.message || '打开失败', 'danger');
  }
}

async function openSelectedStructuredReader() {
  const item = selectedResult();
  if (!item || !item.source_file_id) return;
  const reader = window.MEFinderReader;
  if (!reader || typeof reader.openForSearchResult !== 'function') {
    showToast('结构化阅读器暂时不可用', 'warning');
    return;
  }
  try {
    await reader.openForSearchResult(item);
  } catch (error) {
    showToast(error && error.message ? error.message : '结构化文本打开失败', 'danger');
  }
}
