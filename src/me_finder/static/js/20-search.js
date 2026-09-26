/* IIFE 包裹：私有化实现，仅下方公共面挂到全局（#7 前端全局作用域收敛）。
   模式同 reader.js；IIFE 实参在 node 下退回 globalThis。 */
(function (global) {  // module: 20-search.js
  function searchNode(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function searchSvg(paths, viewBox) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    [['viewBox', viewBox || '0 0 20 20'], ['fill', 'none'], ['stroke', 'currentColor'],
      ['stroke-width', '1.8'], ['stroke-linecap', 'round'], ['stroke-linejoin', 'round'],
      ['aria-hidden', 'true']].forEach(function(attribute) { svg.setAttribute(attribute[0], attribute[1]); });
    paths.forEach(function(d) {
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    });
    return svg;
  }

  function searchScopeOption(title, meta, selected, action, idField, id) {
    var button = searchNode('button', 'app-select-option' + (selected ? ' is-selected' : ''));
    button.type = 'button';
    button.dataset.action = action;
    if (idField) button.dataset[idField] = id;
    if (meta) {
      var content = searchNode('span', 'document-option-main');
      content.appendChild(searchNode('span', 'document-option-title', title));
      content.appendChild(searchNode('span', 'document-option-meta', meta));
      button.appendChild(content);
    } else button.appendChild(searchNode('span', null, title));
    if (selected) {
      var check = searchSvg(['m5 10 3 3 7-7']);
      check.setAttribute('stroke-width', '2');
      button.appendChild(check);
    }
    return button;
  }
  /* ═══ Mode segmented control ═══ */
  function setMode(btn) {
    searchStore.currentMode = btn.dataset.mode;
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
    searchStore.sourceType = ['all','word','epub','pdf'].indexOf(sourceType) >= 0 ? sourceType : 'all';
    document.querySelectorAll('#source-type-control .source-type-btn').forEach(function(button) {
      button.classList.toggle('active', button.dataset.source === searchStore.sourceType);
    });
    if (searchStore.documentId) {
      var selected = searchStore.sourceFiles.find(function(item) { return item.source_file_id === searchStore.documentId; });
      if (selected && searchStore.sourceType !== 'all' && searchSourceFacet(selected) !== searchStore.sourceType) searchStore.documentId = '';
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
    if (shouldOpen) syncAppSelectAria(select);
    if (shouldOpen && selectId === 'document-select') {
      await ensureSearchDocuments();
      renderSearchDocumentOptions();
      syncAppSelectAria(select);
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
  /* role="listbox" 承诺每个选项都是 role="option" 且带 aria-selected，但选项分散在
     多处渲染（搜索范围、条数、引文格式、书目语言、阅读器分段样式…）。此处在打开时
     统一补齐，读屏只在菜单展开时需要该语义，避免 29 处渲染点各写一遍而漏掉。 */
  function syncAppSelectAria(select) {
    var menu = select.querySelector('[role="listbox"]');
    if (!menu) return;
    menu.querySelectorAll('.app-select-option').forEach(function(option) {
      if (option.getAttribute('role') !== 'option') option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', option.classList.contains('is-selected') ? 'true' : 'false');
    });
  }

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
    searchStore.limit = limit === 'all' ? 'all' : Math.max(1, Math.min(Number(limit) || 10, 200));
    document.getElementById('limit-select-label').textContent = searchStore.limit === 'all' ? '全部' : searchStore.limit + ' 条';
    document.querySelectorAll('#limit-options .app-select-option').forEach(function(option) {
      option.classList.toggle('is-selected', String(option.dataset.value) === String(searchStore.limit));
    });
    closeAppSelects();
    rerunSearchAfterFilterChange();
  }

  /* ═══ Shared library catalog ═══ */
  function invalidateLibraryCatalog() {
    searchStore.libraryCatalog = null;
    searchStore.libraryCatalogPromise = null;
    libraryStore.loaded = false;
    searchStore.documentsLoaded = false;
    libraryStore.detailLoaded = {};
    libraryStore.detailPending = {};
    libraryStore.works = [];
  }

  // 摘要投影不含映射证据、PDF 剖面与收录作品，详情由 ensureLibraryDetail 按需补齐。
  function fetchLibraryCatalog(force) {
    if (force) invalidateLibraryCatalog();
    if (searchStore.libraryCatalog) return Promise.resolve(searchStore.libraryCatalog);
    if (searchStore.libraryCatalogPromise) return searchStore.libraryCatalogPromise;
    searchStore.libraryCatalogPromise = MEFinderApi.fetch('/api/library?view=summary').then(function(response) {
      return response.json().then(function(data) {
        if (!response.ok || data.error) throw new Error(data.error || '文献库加载失败');
        searchStore.libraryCatalog = data;
        return data;
      });
    }).catch(function(error) {
      searchStore.libraryCatalogPromise = null;
      throw error;
    });
    return searchStore.libraryCatalogPromise;
  }


  function volumeForSource(sourceId) {
    return libraryStore.volumeBySource.get(sourceId) || null;
  }

  async function ensureSearchDocuments(force) {
    if (searchStore.documentsLoaded && !force) return;
    var options = document.getElementById('document-options');
    if (options) options.replaceChildren(searchNode('div', 'document-options-empty', '正在读取文献库…'));
    try {
      var data = await fetchLibraryCatalog(force);
      searchStore.sourceFiles = data.items || [];
      searchStore.volumes = data.volumes || [];
      libraryStore.volumeBySource = buildVolumeIndex(searchStore.volumes);
      await global.MEFinder.library.loadDocumentGroups();
      searchStore.documentsLoaded = true;
    } catch (error) {
      searchStore.documentsLoaded = false;
      if (options) options.replaceChildren(searchNode('div', 'document-options-empty', '文献列表读取失败'));
    }
  }

  function searchDocumentView(source) {
    var volume = volumeForSource(source.source_file_id);
    var bib = source.bibliographic || source.bibliographic_metadata || {};
    var title = source.title || bib.title || (volume && volume.display_title) || source.display_title || source.file_name || source.source_file_id;
    var author = source.author || bib.author || '';
    return {title:title, author:author, sourceType:sourceFormatLabel(source)};
  }

  function searchSourceFacet(source) {
    if (source && source.source_type === 'pdf') return 'pdf';
    return sourceFormatLabel(source) === 'EPUB' ? 'epub' : 'word';
  }

  function renderSearchDocumentOptions() {
    var options = document.getElementById('document-options');
    if (!options) return;
    if (!searchStore.documentsLoaded) {
      options.replaceChildren(searchNode('div', 'document-options-empty', '打开菜单后读取文献列表'));
      return;
    }
    var queryInput = document.getElementById('document-filter-query');
    var query = String(queryInput ? queryInput.value : '').trim().toLowerCase().replace(/\s+/g, '');
    var sources = searchStore.sourceFiles.filter(function(source) {
      if (searchStore.sourceType !== 'all' && searchSourceFacet(source) !== searchStore.sourceType) return false;
      var view = searchDocumentView(source);
      var haystack = [view.title, view.author, source.file_name].join('|').toLowerCase().replace(/\s+/g, '');
      return !query || haystack.indexOf(query) >= 0;
    }).sort(function(a, b) {
      return calPinyinCollator.compare(searchDocumentView(a).title, searchDocumentView(b).title);
    });
    var noScope = !searchStore.documentId && !searchStore.groupId;
    options.replaceChildren(searchScopeOption('全部文献', '', noScope, 'selectSearchScopeAll'));
    if (typeof libraryStore.documentGroups !== 'undefined' && libraryStore.documentGroups.length) {
      options.appendChild(searchNode('div', 'document-options-head', '作品'));
      libraryStore.documentGroups.forEach(function(group) {
        var selected = group.document_group_id === searchStore.groupId;
        var count = (group.members || []).length;
        options.appendChild(searchScopeOption(group.title, count + ' 个版本', selected, 'selectSearchGroup', 'groupId', group.document_group_id));
      });
    }
    if (!sources.length && !(libraryStore.documentGroups || []).length) {
      options.appendChild(searchNode('div', 'document-options-empty', '没有符合条件的文献'));
      return;
    }
    if (sources.length) options.appendChild(searchNode('div', 'document-options-head', '单篇文献'));
    sources.forEach(function(source) {
      var view = searchDocumentView(source);
      var selected = !searchStore.groupId && source.source_file_id === searchStore.documentId;
      options.appendChild(searchScopeOption(view.title, [view.sourceType, view.author].filter(Boolean).join(' · '),
        selected, 'selectSearchDocument', 'value', source.source_file_id));
    });
  }

  function selectSearchDocument(event, sourceId) {
    event.stopPropagation();
    searchStore.documentId = sourceId || '';
    searchStore.groupId = '';  // single-source and group scope are mutually exclusive
    updateSearchDocumentLabel();
    closeSearchSelects();
    rerunSearchAfterFilterChange();
  }

  function selectSearchGroup(event, groupId) {
    event.stopPropagation();
    searchStore.groupId = groupId || '';
    searchStore.documentId = '';
    updateSearchDocumentLabel();
    closeSearchSelects();
    rerunSearchAfterFilterChange();
  }

  function selectSearchScopeAll(event) {
    event.stopPropagation();
    searchStore.groupId = '';
    searchStore.documentId = '';
    updateSearchDocumentLabel();
    closeSearchSelects();
    rerunSearchAfterFilterChange();
  }

  function updateSearchDocumentLabel() {
    var label = document.getElementById('document-select-label');
    if (!label) return;
    if (searchStore.groupId && typeof libraryStore.documentGroups !== 'undefined') {
      var group = libraryStore.documentGroups.find(function(g) { return g.document_group_id === searchStore.groupId; });
      if (group) {
        label.textContent = group.title + ' · ' + (group.members || []).length + ' 个版本';
        label.title = group.title;
        return;
      }
    }
    var source = searchStore.sourceFiles.find(function(item) { return item.source_file_id === searchStore.documentId; });
    label.textContent = source ? searchDocumentView(source).title : '全部文献';
    label.title = source ? searchDocumentView(source).title : '';
  }

  /* ═══ Search ═══ */
  async function runSearch() {
    const query = document.getElementById('query').value.trim();
    if (!query) return;
    const seq = ++searchStore.sequence;  // 只有最后一次发起的检索能写回结果，避免慢响应覆盖新结果
    const statusEl = document.getElementById('results-status');
    const listEl = document.getElementById('results-list');
    statusEl.style.display = 'block';
    statusEl.textContent = '检索中…';
    listEl.replaceChildren();
    searchStore.selectedIndex = -1;
    showEmptyDetail();

    try {
      const resp = await MEFinderApi.fetch('/api/search', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(Object.assign(
          {query, mode: searchStore.currentMode, limit: searchStore.limit, source_type: searchStore.sourceType},
          searchStore.groupId
            ? {document_group_id: searchStore.groupId}
            : {source_file_id: searchStore.documentId || null}
        ))
      });
      const data = await resp.json();
      if (seq !== searchStore.sequence) return;  // 已有更新的检索发起，丢弃这次过期响应
      if (!resp.ok || data.error) throw new Error(data.error || ('HTTP ' + resp.status));
      searchStore.results = data.results || [];
      if (data.total_is_exact === false || data.has_more) {
        statusEl.textContent = '显示前 ' + searchStore.results.length + ' 条匹配结果，还有更多';
      } else {
        statusEl.textContent = '找到 ' + data.total + ' 条候选，显示 ' + searchStore.results.length + ' 条';
      }

      if (searchStore.results.length === 0) {
        var empty = searchNode('div', 'empty-state');
        empty.style.minHeight = '200px';
        empty.appendChild(searchNode('div', 'empty-state-text', '未找到匹配结果'));
        empty.appendChild(searchNode('div', 'empty-state-hint', '尝试更短的引文或切换为模糊检索'));
        listEl.replaceChildren(empty);
        return;
      }

      listEl.replaceChildren(...searchStore.results.map((item, i) => resultRowNode(item, i)));
      selectResult(0, false);
    } catch (err) {
      if (seq !== searchStore.sequence) return;
      statusEl.textContent = '检索失败：' + err.message;
    }
  }

  function appendHighlighted(host, item, limit) {
    var characters = Array.from(String(item.paragraph_text || ''));
    var visible = limit == null ? characters : characters.slice(0, limit);
    var start = item.highlighted_html ? Number(item.match_start || 0) : visible.length;
    var end = item.highlighted_html ? Number(item.match_end || 0) : visible.length;
    start = Math.max(0, Math.min(start, visible.length));
    end = Math.max(start, Math.min(end, visible.length));
    host.appendChild(document.createTextNode(visible.slice(0, start).join('')));
    if (end > start) host.appendChild(searchNode('mark', null, visible.slice(start, end).join('')));
    host.appendChild(document.createTextNode(visible.slice(end).join('') + (limit != null && characters.length > limit ? '…' : '')));
  }

  function resultRowNode(item, index) {
    const score = Math.round(item.match_score * 100);
    const typeLabel = matchTypeLabel(item.match_type);
    const sourceIcon = sourceFormatLabel(item);
    var row = searchNode('div', 'result-row');
    row.dataset.index = String(index);
    row.dataset.action = 'selectSearchResult';
    var head = searchNode('div', 'result-row-head');
    head.appendChild(searchNode('span', 'result-score', score + '%'));
    head.appendChild(searchNode('span', 'result-match-type', typeLabel));
    head.appendChild(searchNode('span', 'result-title', item.document_title || item.work_title || item.volume_display || ''));
    row.appendChild(head);
    var meta = searchNode('div', 'result-meta');
    if (item.author_label) meta.appendChild(searchNode('span', null, item.author_label));
    if (item.volume_display) meta.appendChild(searchNode('span', null, item.volume_display));
    meta.appendChild(searchNode('span', null, formatCitationPageLabel(item)));
    meta.appendChild(searchNode('span', null, sourceIcon));
    row.appendChild(meta);
    var snippet = searchNode('div', 'result-snippet');
    appendHighlighted(snippet, item, 100);
    row.appendChild(snippet);
    return row;
  }

  function searchResultsArea() {
    return document.querySelector('#page-search .results-area');
  }

  function showSearchResultsList() {
    closeAppSelects();
    const area = searchResultsArea();
    if (area) area.classList.remove('is-detail-open');
    const row = document.querySelector('.result-row[data-index="' + searchStore.selectedIndex + '"]');
    if (row) row.scrollIntoView({block: 'nearest'});
  }

  function showSearchResultDetail() {
    const area = searchResultsArea();
    if (area) area.classList.add('is-detail-open');
  }

  function selectResult(index, openNarrowDetail) {
    if (index < 0 || index >= searchStore.results.length) return;
    searchStore.selectedIndex = index;
    document.querySelectorAll('.result-row').forEach((row, i) => {
      row.classList.toggle('selected', i === index);
    });
    const item = searchStore.results[index];
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

  function detailContextNode(items, side) {
    var fullText = detailContextText(items);
    if (!fullText) return null;
    var label = side === 'before' ? '上文' : '下文';
    var contentId = 'detail-context-' + side;
    var truncated = Array.from(fullText).length > DETAIL_CONTEXT_PREVIEW_CHARS;
    var section = searchNode('section', 'detail-context-section detail-context-' + side);
    var heading = searchNode('div', 'detail-context-heading');
    heading.appendChild(searchNode('span', 'detail-context-label', label));
    var toggle = searchNode('button', 'detail-context-toggle', '展开');
    toggle.type = 'button';
    toggle.setAttribute('aria-label', '展开' + label);
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', contentId);
    toggle.dataset.contextLabel = label;
    toggle.dataset.characterTruncated = truncated ? 'true' : 'false';
    toggle.hidden = !truncated;
    toggle.dataset.action = 'toggleDetailContext';
    heading.appendChild(toggle);
    section.appendChild(heading);
    var content = searchNode('div', 'detail-context');
    content.id = contentId;
    content.setAttribute('role', 'region');
    content.setAttribute('aria-label', label);
    content.appendChild(searchNode('span', 'detail-context-preview', detailContextPreview(fullText, side)));
    var expanded = searchNode('span', 'detail-context-full', fullText);
    expanded.hidden = true;
    content.appendChild(expanded);
    section.appendChild(content);
    return section;
  }

  function detailPageRow(parent, label, value) {
    var row = searchNode('div', 'page-detail-row');
    row.appendChild(searchNode('span', 'page-detail-label', label));
    row.appendChild(searchNode('span', null, value));
    parent.appendChild(row);
  }

  function detailAction(label, action, primary) {
    var button = searchNode('button', 'action-btn' + (primary ? ' primary' : ''), label);
    button.type = 'button';
    button.dataset.action = action;
    return button;
  }

  function showDetail(item) {
    const panel = document.getElementById('detail-panel');
    const pageLabel = formatCitationPageLabel(item);
    const sourceLabel = sourceFormatLabel(item);
    var card = searchNode('div', 'detail-card');
    var mobileToolbar = searchNode('div', 'detail-mobile-toolbar');
    var back = detailAction('', 'showSearchResultsList');
    back.className = 'detail-back-button';
    back.appendChild(searchSvg(['m12 5-5 5 5 5', 'M7 10h8']));
    back.appendChild(searchNode('span', null, '返回结果列表'));
    mobileToolbar.appendChild(back);
    card.appendChild(mobileToolbar);
    var scroll = searchNode('div', 'detail-scroll');
    var header = searchNode('div', 'detail-header');
    header.appendChild(searchNode('div', 'detail-title', item.document_title || item.work_title || item.volume_display || ''));
    if (item.author_label) header.appendChild(searchNode('div', 'detail-author', item.author_label));
    var pills = searchNode('div', 'detail-pills');
    pills.appendChild(searchNode('span', 'detail-pill', sourceLabel));
    if (item.volume_display) pills.appendChild(searchNode('span', 'detail-pill', item.volume_display));
    pills.appendChild(searchNode('span', 'detail-pill', pageLabel));
    header.appendChild(pills);
    if (item.source_type === 'pdf') {
      var pageToggle = searchNode('div', 'page-detail-toggle', '页码详情 ▸');
      pageToggle.dataset.action = 'togglePageDetail';
      header.appendChild(pageToggle);
      var pageDetail = searchNode('div', 'page-detail-body');
      detailPageRow(pageDetail, '引用页码', pageLabel);
      detailPageRow(pageDetail, 'PDF 页码标签', item.pdf_page_start_label || '无');
      detailPageRow(pageDetail, 'PDF 物理页', item.pdf_page_start_index != null ? 'PDF 第 ' + (item.pdf_page_start_index + 1) + ' 页' : '—');
      if (item.layout_mode === 'spread') detailPageRow(pageDetail, '双开位置', logicalPageSideLabel(item.logical_page_side, item.spread_hit_precision));
      detailPageRow(pageDetail, '映射方式', mappingMethodLabel(item.page_mapping_method));
      if (item.mapping_confidence_level) detailPageRow(pageDetail, '映射置信度', mappingConfidenceLabel(item.mapping_confidence_level, item.page_mapping_confidence));
      if (item.page_scope) detailPageRow(pageDetail, '页码范围', pageScopeLabel(item.page_scope));
      if (item.mapping_evidence) detailPageRow(pageDetail, '映射依据', mappingEvidenceSummary(item.mapping_evidence));
      if (item.is_cross_page) detailPageRow(pageDetail, '跨页命中', '是');
      header.appendChild(pageDetail);
    }
    header.appendChild(citationAvailabilityNode(item));
    scroll.appendChild(header);
    var body = searchNode('div', 'detail-body');
    var before = detailContextNode(item.context_before, 'before');
    if (before) body.appendChild(before);
    var hit = searchNode('div', 'detail-hit');
    appendHighlighted(hit, item);
    body.appendChild(hit);
    var after = detailContextNode(item.context_after, 'after');
    if (after) body.appendChild(after);
    scroll.appendChild(body);
    card.appendChild(scroll);
    var actions = searchNode('div', 'detail-actions');
    actions.setAttribute('role', 'toolbar');
    actions.setAttribute('aria-label', '检索结果操作');
    var format = searchNode('span', 'app-select detail-format-control');
    format.id = 'detail-format-control';
    var trigger = detailAction('', 'toggleDetailFormatSelect');
    trigger.className = 'action-btn app-select-trigger detail-format-trigger';
    trigger.setAttribute('aria-label', '选择出处格式');
    trigger.setAttribute('aria-haspopup', 'menu');
    trigger.setAttribute('aria-expanded', 'false');
    var styleLabel = searchNode('span', null, citationStyleDisplayLabel(citationStyle));
    styleLabel.id = 'detail-citation-style-label';
    trigger.appendChild(styleLabel);
    trigger.appendChild(searchSvg(['m6 8 4 4 4-4']));
    format.appendChild(trigger);
    var formatMenu = searchNode('span', 'app-select-menu detail-format-menu');
    formatMenu.setAttribute('role', 'menu');
    formatMenu.setAttribute('aria-label', '出处格式');
    var choices = searchNode('span', 'detail-citation-style-options');
    choices.id = 'citation-style-control';
    choices.appendChild(citationStyleMenuNode());
    formatMenu.appendChild(choices);
    format.appendChild(formatMenu);
    actions.appendChild(format);
    actions.appendChild(detailAction('复制出处', 'copySelectedCitation'));
    if (item.source_file_id) {
      actions.appendChild(detailAction('查看结构化文本', 'openSelectedStructuredReader'));
      var open = detailAction('打开原文', 'openSearchSource', true);
      open.dataset.sourceId = item.source_file_id;
      open.dataset.pdfPage = item.pdf_page_start_index != null ? String(item.pdf_page_start_index + 1) : '';
      actions.appendChild(open);
    }
    card.appendChild(actions);
    panel.replaceChildren(card);

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
    var empty = searchNode('div', 'empty-state');
    var icon = searchNode('div', 'empty-state-icon');
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    [['width', '48'], ['height', '48'], ['viewBox', '0 0 48 48'], ['fill', 'none'],
      ['stroke', 'currentColor'], ['stroke-width', '1.5'], ['opacity', '0.35']].forEach(function(attribute) {
      svg.setAttribute(attribute[0], attribute[1]);
    });
    [['rect', {x: '8', y: '6', width: '32', height: '36', rx: '3'}],
      ['line', {x1: '16', y1: '16', x2: '32', y2: '16'}],
      ['line', {x1: '16', y1: '22', x2: '32', y2: '22'}],
      ['line', {x1: '16', y1: '28', x2: '28', y2: '28'}]].forEach(function(shape) {
      var node = document.createElementNS('http://www.w3.org/2000/svg', shape[0]);
      Object.keys(shape[1]).forEach(function(key) { node.setAttribute(key, shape[1][key]); });
      svg.appendChild(node);
    });
    icon.appendChild(svg);
    empty.appendChild(icon);
    empty.appendChild(searchNode('div', 'empty-state-text', '选择一条结果查看详情'));
    document.getElementById('detail-panel').replaceChildren(empty);
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
    if (e.key === 'ArrowDown' && searchStore.results.length) {
      e.preventDefault();
      selectResult(Math.min(searchStore.selectedIndex + 1, searchStore.results.length - 1));
    }
    if (e.key === 'ArrowUp' && searchStore.results.length) {
      e.preventDefault();
      selectResult(Math.max(searchStore.selectedIndex - 1, 0));
    }
  });

  /* ═══ Helpers ═══ */

  // mappingMethodLabel / mappingStatusLabel / mappingConfidenceLabel / pageScopeLabel /
  // logicalPageSideLabel / mappingEvidenceSummary / autoMappingSegmentText /
  // firstPageValue 已抽到 06-pure.js（纯逻辑，可单测）。

  // isUncalibratedPageLabel / formatChinesePageRange / formatCitationPageLabel
  // 已抽到 06-pure.js（纯逻辑，可单测）。

  function selectedResult() {
    if (searchStore.selectedIndex < 0 || searchStore.selectedIndex >= searchStore.results.length) return null;
    return searchStore.results[searchStore.selectedIndex];
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


  function persistSelectedCitationStyle() {
    MEFinderApi.fetch('/api/preferences', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({citation_style: citationStyle})
    }).catch(function() {
      // localStorage remains a cross-platform fallback if the backend is old or
      // temporarily unavailable.
    });
  }

  function citationStyleMenuNode() {
    var fragment = document.createDocumentFragment();
    CITATION_STYLE_OPTIONS.filter(function(option) {
      return enabledCitationStyles.indexOf(option.id) >= 0;
    }).forEach(function(option) {
      var button = searchNode('button', 'app-select-option' + (citationStyle === option.id ? ' is-selected' : ''), option.label);
      button.type = 'button';
      button.dataset.value = option.id;
      button.dataset.action = 'selectCitationStyle';
      fragment.appendChild(button);
    });
    return fragment;
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

  function citationAvailabilityNode(item) {
    var status = searchNode('div', 'detail-citation-status');
    status.id = 'detail-citation-status';
    status.setAttribute('role', 'status');
    status.hidden = citationIsComplete(item);
    var icon = searchSvg(['M10 6.5v4.25', 'M10 14h.01']);
    var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', '10'); circle.setAttribute('cy', '10'); circle.setAttribute('r', '7.5');
    icon.insertBefore(circle, icon.firstChild);
    status.appendChild(icon);
    var copy = document.createElement('span');
    copy.appendChild(searchNode('strong', null, '出处信息不完整'));
    copy.appendChild(searchNode('span', null, '暂不可生成完整引文；仍可查看正文和打开原文。'));
    status.appendChild(copy);
    return status;
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
      const resp = await MEFinderApi.fetch('/api/open-source', {
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
      await reader.openForSearchResult(item, {returnLabel: '检索结果'});
    } catch (error) {
      showToast(error && error.message ? error.message : '结构化文本打开失败', 'danger');
    }
  }

  MEFinderActions.registerInline('toggleSegmentSelect', function(event, target) {
    toggleAppSelect(event, target.dataset.selectId);
  });
  MEFinderActions.register('toggleDetailContext', function(event, target) {
    toggleDetailContext(target);
  });
  MEFinderActions.registerInline('selectSearchScopeAll', function(event) {
    selectSearchScopeAll(event);
  });
  MEFinderActions.registerInline('selectSearchGroup', function(event, target) {
    selectSearchGroup(event, target.dataset.groupId);
  });
  MEFinderActions.registerInline('selectSearchDocument', function(event, target) {
    selectSearchDocument(event, target.dataset.value);
  });
  MEFinderActions.register('selectSearchResult', function(event, target) {
    selectResult(Number(target.dataset.index));
  });
  MEFinderActions.register('togglePageDetail', function(event, target) {
    togglePageDetail(target);
  });
  MEFinderActions.register('showSearchResultsList', function() {
    showSearchResultsList();
  });
  MEFinderActions.registerInline('toggleDetailFormatSelect', function(event) {
    toggleAppSelect(event, 'detail-format-control');
  });
  MEFinderActions.register('copySelectedCitation', function() {
    copySelectedCitation();
  });
  MEFinderActions.register('openSelectedStructuredReader', function() {
    openSelectedStructuredReader();
  });
  MEFinderActions.register('openSearchSource', function(event, target) {
    openSource(target.dataset.sourceId, target.dataset.pdfPage ? Number(target.dataset.pdfPage) : null);
  });
  MEFinderActions.registerInline('selectCitationStyle', function(event, target) {
    selectCitationStyle(event, target.dataset.value);
  });

  // 浏览器公共面：仅这些符号可被其它 static/js 文件与模板动作访问。
  global.setMode = setMode;
  global.setSearchSourceType = setSearchSourceType;
  global.closeAppSelects = closeAppSelects;
  global.toggleAppSelect = toggleAppSelect;
  global.toggleSearchSelect = toggleSearchSelect;
  global.setSearchLimit = setSearchLimit;
  global.invalidateLibraryCatalog = invalidateLibraryCatalog;
  global.fetchLibraryCatalog = fetchLibraryCatalog;
  global.volumeForSource = volumeForSource;
  global.ensureSearchDocuments = ensureSearchDocuments;
  global.renderSearchDocumentOptions = renderSearchDocumentOptions;
  global.updateSearchDocumentLabel = updateSearchDocumentLabel;
  global.runSearch = runSearch;
  global.showDetail = showDetail;
  global.selectedResult = selectedResult;
  global.setCitationStyle = setCitationStyle;
  global.openSource = openSource;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
