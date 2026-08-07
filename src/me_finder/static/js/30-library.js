/* ═══ Library ═══ */
function applyLibraryCatalog(data) {
  libSources = data.items || [];
  libVolumes = data.volumes || [];
  libVolumeBySource = buildVolumeIndex(libVolumes);
  libStats = data.stats || null;
  libLoaded = true;
  // 搜索下拉与文献库共用同一份摘要，避免两处各拉一次。
  searchSourceFiles = libSources;
  searchVolumes = libVolumes;
  searchDocumentsLoaded = true;
}

async function loadLibrary(force) {
  try {
    applyLibraryCatalog(await fetchLibraryCatalog(force));
    renderLibraryStats();
    syncLibraryViewButtons();
    syncLibrarySortControls();
    renderLibraryList();
  } catch(e) {
    document.getElementById('library-list').innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">' + esc(e.message || '文献库加载失败') + '</div></div>';
  }
}

// 映射区间、识别证据、PDF 剖面和收录作品只在详情抽屉里用，按 source_id 单份读取。
function ensureLibraryDetail(sourceId) {
  if (!sourceId || libDetailLoaded[sourceId]) return Promise.resolve();
  if (libDetailPending[sourceId]) return libDetailPending[sourceId];
  var request = fetch('/api/library/document?source_id=' + encodeURIComponent(sourceId)).then(function(response) {
    return response.json().then(function(data) {
      if (!response.ok || data.error) throw new Error(data.error || '文献详情读取失败');
      applyLibraryDetail(sourceId, data);
    });
  }).then(function() {
    delete libDetailPending[sourceId];
  }, function(error) {
    delete libDetailPending[sourceId];
    throw error;
  });
  libDetailPending[sourceId] = request;
  return request;
}

function applyLibraryDetail(sourceId, data) {
  var detail = data.item || {};
  // libSources 与 searchSourceFiles 指向同一个数组，就地替换让两处同时拿到完整记录。
  var index = libSources.findIndex(function(item) { return item.source_file_id === sourceId; });
  if (index >= 0) libSources[index] = Object.assign({}, libSources[index], detail);
  if (data.volume && data.volume.source_file_id) {
    var volumeIndex = libVolumes.findIndex(function(item) { return item.source_file_id === sourceId; });
    if (volumeIndex >= 0) libVolumes[volumeIndex] = data.volume;
    else libVolumes.push(data.volume);
    libVolumeBySource.set(sourceId, data.volume);
  }
  var volumeId = data.volume ? data.volume.volume_id : null;
  if (volumeId) {
    libWorks = libWorks.filter(function(work) { return work.volume_id !== volumeId; }).concat(data.works || []);
  }
  libDetailLoaded[sourceId] = true;
}

function renderLibraryStats() {
  var container = document.getElementById('library-stats');
  if (!container) return;
  var current = {total:0,calibrated:0,page_pending:0,bibliographic:0};
  libSources.forEach(function(item) {
    if (item.source_type !== 'pdf') return;
    current.total += 1;
    var group = calibrationStatusGroup(item.status);
    if (group === 'calibrated') current.calibrated += 1;
    else current.page_pending += 1;
    if (bibliographicMissingFields(sourceBibliographicMetadata(item)).length > 0) current.bibliographic += 1;
  });
  container.innerHTML = statusStatButton('pdf_all','PDF 总数',current.total,'info','document',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('calibrated','页码已校准',current.calibrated,'success','check',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('page_pending','页码待处理',current.page_pending,'warning','notice',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('bibliographic','书目待补全',current.bibliographic,'neutral','book',libStatusFilter,'applyLibStatusFilter');
}

function applyLibStatusFilter(status) {
  var requested = status || 'all';
  libStatusFilter = requested === libStatusFilter ? 'all' : requested;
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

function loadLibDefaultLanguage() {
  var raw = null;
  try { raw = localStorage.getItem('meFinderLibDefaultLanguage'); } catch (_) {}
  return raw === 'foreign' ? 'foreign' : 'chinese';
}

function setLibDefaultLanguage(btn) {
  var value = btn && btn.dataset ? btn.dataset.deflang : btn;
  value = value === 'foreign' ? 'foreign' : 'chinese';
  if (value === libDefaultLanguage) return;
  libDefaultLanguage = value;
  try { localStorage.setItem('meFinderLibDefaultLanguage', value); } catch (_) {}
  syncLibDefaultLanguageControl();
  renderLibraryList();  // 重绘以刷新语言筛选条的标签与「本国/外文」归属
}

function syncLibDefaultLanguageControl() {
  document.querySelectorAll('#lib-default-lang-control .seg-btn').forEach(function(b) {
    b.classList.toggle('active', b.dataset.deflang === libDefaultLanguage);
  });
}

// 文字系统事实（'chinese' / 'foreign'）→ 语言筛选条上的显示标签。
// 标签始终是「中文 / 外文」；默认语言不改标签文字，只改两档的排列主次
// （见 renderLibraryList 里对 style.order 的设置）。
function libLangChipLabel(scriptLang) {
  return scriptLang === 'chinese' ? '中文' : '外文';
}

function libraryDocType(source) {
  var value = String((source && source.document_type) || '');
  return value === 'journal_article' || value === 'thesis' ? value : 'book';
}

function setLibDocTypeFilter(btn) {
  libDocTypeFilter = btn.dataset.doctype;
  document.querySelectorAll('#lib-doctype-control .seg-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  closeLibDrawer();
  renderLibraryList();
}

function filterLibrary() {
  // 输入法与连续输入下，91 份以上的列表每敲一个字重排一次会明显发涩。
  if (libFilterTimer) clearTimeout(libFilterTimer);
  libFilterTimer = setTimeout(function() {
    libFilterTimer = null;
    renderLibraryList();
  }, 160);
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
  if (libDocTypeFilter !== 'all') {
    sources = sources.filter(s => libraryDocType(s) === libDocTypeFilter);
  }
  if (libStatusFilter === 'pdf_all') {
    sources = sources.filter(s => s.source_type === 'pdf');
  } else if (libStatusFilter === 'page_pending') {
    sources = sources.filter(s => s.source_type === 'pdf' && calibrationStatusGroup(s.status) !== 'calibrated');
  } else if (libStatusFilter === 'bibliographic') {
    sources = sources.filter(s => s.source_type === 'pdf' && bibliographicMissingFields(sourceBibliographicMetadata(s)).length > 0);
  } else if (libStatusFilter !== 'all') {
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

function isLibraryDeleteSelectable(source) {
  return !!source && (source.source_type === 'pdf' || source.source_type === 'word');
}

function updateLibraryDeleteControls() {
  var bar = document.getElementById('library-selection-bar');
  var page = document.getElementById('page-library');
  var count = document.getElementById('library-selection-count');
  var removeButton = document.getElementById('library-remove-selected-btn');
  var selectVisibleButton = document.getElementById('library-select-visible-btn');
  var selectedCount = libDeleteSelection.size;
  var active = selectedCount > 0;
  // Selection alone drives the contextual action bar: no persistent mode toggle.
  if (page) page.classList.toggle('library-selecting', active);
  if (bar) bar.hidden = !active;
  if (count) count.textContent = '已选 ' + selectedCount + ' 项';
  if (removeButton) removeButton.disabled = selectedCount === 0;
  if (selectVisibleButton) {
    var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
    var allSelected = selectable.length > 0 && selectable.every(function(item) {
      return libDeleteSelection.has(item.source_file_id);
    });
    selectVisibleButton.textContent = allSelected ? '取消全选' : '全选当前';
    selectVisibleButton.disabled = selectable.length === 0;
  }
}

function syncLibraryDeleteSelectionUI() {
  document.querySelectorAll('#library-list .library-entry').forEach(function(entry) {
    var selected = libDeleteSelection.has(entry.dataset.id);
    entry.classList.toggle('delete-selected', selected);
    entry.setAttribute('aria-selected', selected ? 'true' : 'false');
    var input = entry.querySelector('.library-delete-check');
    if (input) input.checked = selected;
  });
  updateLibraryDeleteControls();
}

function clearLibrarySelection() {
  if (libDeleteSelection.size === 0) return;
  libDeleteSelection.clear();
  syncLibraryDeleteSelectionUI();
}

function toggleLibraryDeleteSelection(sourceId, force) {
  var source = libSources.find(function(item) { return item.source_file_id === sourceId; });
  if (!isLibraryDeleteSelectable(source)) {
    showToast('当前来源类型暂不支持从文献库移除', 'warning');
    return;
  }
  var selected = typeof force === 'boolean' ? force : !libDeleteSelection.has(sourceId);
  if (selected) libDeleteSelection.add(sourceId);
  else libDeleteSelection.delete(sourceId);
  syncLibraryDeleteSelectionUI();
}

function toggleSelectVisibleLibraryDocuments() {
  var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
  var allSelected = selectable.length > 0 && selectable.every(function(item) {
    return libDeleteSelection.has(item.source_file_id);
  });
  selectable.forEach(function(item) {
    if (allSelected) libDeleteSelection.delete(item.source_file_id);
    else libDeleteSelection.add(item.source_file_id);
  });
  syncLibraryDeleteSelectionUI();
}

function handleLibraryEntryClick(event, sourceId) {
  if (suppressLibrarySelectionClick) {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  // Clicking the row/card body always opens details; the checkbox is the only
  // thing that toggles selection, so browsing never accidentally selects.
  selectLibDoc(sourceId);
}

function renderLibraryList() {
  const sources = getFilteredSources();
  const listEl = document.getElementById('library-list');
  listEl.className = 'library-list-container library-view-' + libViewMode;
  libDeleteSelection.forEach(function(sourceId) {
    if (!libSources.some(function(source) { return source.source_file_id === sourceId; })) {
      libDeleteSelection.delete(sourceId);
    }
  });
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
    var label = lang === 'all' ? '全部语言' : libLangChipLabel(lang);
    btn.textContent = label + ' (' + count + ')';
    // 默认语言那一档排在「全部语言」之后、另一档之前，让主语言文献靠前。
    btn.style.order = lang === 'all' ? '0' : (lang === libDefaultLanguage ? '1' : '2');
  });
  const journalCount = libSources.filter(s => libraryDocType(s) === 'journal_article').length;
  const thesisCount = libSources.filter(s => libraryDocType(s) === 'thesis').length;
  const bookCount = allCount - journalCount - thesisCount;
  document.querySelectorAll('#lib-doctype-control .seg-btn').forEach(function(btn) {
    var dt = btn.dataset.doctype;
    var count = dt === 'all' ? allCount : dt === 'journal_article' ? journalCount : dt === 'thesis' ? thesisCount : bookCount;
    var label = dt === 'all' ? '全部类型' : dt === 'journal_article' ? '期刊论文' : dt === 'thesis' ? '学位论文' : '著作';
    btn.textContent = label + ' (' + count + ')';
  });

  libraryRenderToken += 1;
  if (sources.length === 0) {
    listEl.innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">未找到匹配文献</div></div>';
    updateLibraryDeleteControls();
    return;
  }
  // 首批同步渲染，其余按帧追加，避免大文献库一次性构建整张列表阻塞首屏。
  listEl.innerHTML = sources.slice(0, LIBRARY_RENDER_BATCH).map(libraryEntryHTML).join('');
  syncLibraryDeleteSelectionUI();
  if (sources.length > LIBRARY_RENDER_BATCH) {
    appendLibraryEntries(sources, LIBRARY_RENDER_BATCH, libraryRenderToken);
  }
}

function scheduleLibraryChunk(callback) {
  // 窗口最小化或隐藏时不会触发 requestAnimationFrame，剩余批次要靠定时器补齐，
  // 否则再显示出来就只剩首批 50 条。
  if (document.hidden || typeof requestAnimationFrame !== 'function') setTimeout(callback, 0);
  else requestAnimationFrame(callback);
}

function appendLibraryEntries(sources, start, token) {
  scheduleLibraryChunk(function() {
    if (token !== libraryRenderToken) return;
    var listEl = document.getElementById('library-list');
    if (!listEl) return;
    var end = Math.min(start + LIBRARY_RENDER_BATCH, sources.length);
    listEl.insertAdjacentHTML('beforeend', sources.slice(start, end).map(libraryEntryHTML).join(''));
    syncLibraryDeleteSelectionUI();
    if (end < sources.length) appendLibraryEntries(sources, end, token);
  });
}

function libraryEntryHTML(src) {
  var vol = volumeForSource(src.source_file_id);
  var isPdf = src.source_type === 'pdf';
  var title = src.title || (src.file_name || src.source_file_id);
  var author = src.author || '作者信息待完善';
  var thesisIcon = src.document_type === 'thesis'
    ? '<svg class="doc-thesis-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-label="学位论文"><title>学位论文</title><path d="M22 10 12 5 2 10l10 5 10-5Z"/><path d="M6 12v5c0 1 2.7 2.5 6 2.5s6-1.5 6-2.5v-5"/><path d="M22 10v5"/></svg>'
    : '';
  var bib = sourceBibliographicMetadata(src);
  var missingMetadataText = isPdf ? bibliographicMissingText(bib) : '';
  var size = formatFileSize(src.size_bytes);
  var isSelected = src.source_file_id === libSelectedId;
  var isDeleteSelectable = isLibraryDeleteSelectable(src);
  var isDeleteSelected = libDeleteSelection.has(src.source_file_id);
  var typeCls = isPdf ? (src.parser_label === 'MinerU' ? 'mineru' : 'pdf') : 'word';
  var typeLabel = isPdf ? (src.parser_label || 'PDF') : 'Word';
  var itemStatus = isPdf ? (calTransientStatus[src.source_file_id] || src.status) : '';
  var statusGroup = isPdf ? calibrationStatusGroup(itemStatus) : '';
  var statusChip = isPdf
    ? '<span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(statusGroup) + ' ' + statusGroup + '">' + statusChipIcon(statusGroup) + esc(calibrationStatusLabel(itemStatus)) + '</span>'
    : '';
  // 列表模式空间窄：校准状态与「缺书目」都收成仅图标（含 title 悬停提示），
  // 让标题成为主列不被文字徽章挤掉。这也是 Zotero 等主流列表视图的做法。
  var statusIconOnly = isPdf
    ? '<span class="cal-status-icon status-chip--' + statusSemanticVariant(statusGroup) + ' ' + statusGroup + '" title="' + esc(calibrationStatusLabel(itemStatus)) + '" aria-label="' + esc(calibrationStatusLabel(itemStatus)) + '">' + statusChipIcon(statusGroup) + '</span>'
    : '';
  var missingIcon = missingMetadataText
    ? '<span class="library-row-missing-icon" title="' + esc(missingMetadataText) + '" aria-label="' + esc(missingMetadataText) + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg></span>'
    : '';
  var wordStructure = !isPdf && vol && vol.primary_structure ? structureLabel(vol.primary_structure) : '';
  var countMeta = isPdf
    ? (src.page_count ? src.page_count + ' 页' : '页数未知')
    : ((src.works_count || 1) + ' 篇');
  // PDF and Word entries both carry a checkbox; CSS reveals it on hover or
  // while a selection is active. Word removal also clears the managed corpus
  // copy so a later full rebuild cannot silently add it back.
  var selectionControl = isDeleteSelectable
    ? '<input class="library-delete-check" type="checkbox" aria-label="选择 ' + esc(title) + '" ' + (isDeleteSelected ? 'checked ' : '') + 'onclick="event.stopPropagation();toggleLibraryDeleteSelection(\'' + esc(src.source_file_id) + '\',this.checked)">'
    : '';
  if (libViewMode === 'grid') {
    var imported = formatCalDate(src.imported_at || src.last_modified);
    var secondary = !isPdf ? ((vol && vol.corpus_title) || '') : '';
    return '<article class="library-card library-entry' + (isSelected ? ' selected' : '') + (isDeleteSelected ? ' delete-selected' : '') + '" data-id="' + esc(src.source_file_id) + '" data-delete-selectable="' + (isDeleteSelectable ? '1' : '0') + '" aria-selected="' + (isDeleteSelected ? 'true' : 'false') + '" onclick="handleLibraryEntryClick(event,\'' + esc(src.source_file_id) + '\')">'
      + '<div class="library-card-top"><div class="library-card-badges"><span class="type-badge ' + typeCls + '">' + typeLabel + '</span>' + statusChip + (wordStructure ? '<span class="library-card-status">' + esc(wordStructure) + '</span>' : '') + (secondary ? '<span class="library-card-status">' + esc(secondary) + '</span>' : '') + '</div>' + selectionControl + '</div>'
      + '<div class="library-card-title">' + thesisIcon + esc(title) + '</div><div class="library-card-author">' + esc(author) + '</div>'
      + (missingMetadataText ? bibliographicMissingBadge(bib) : '')
      + '<div class="library-card-meta">' + esc(countMeta + ' · ' + size) + '</div>'
      + '<div class="library-card-mapping">' + esc(isPdf ? (src.mapping_summary || '尚未建立引用页码映射') : ((vol && vol.version_info) || 'Word 文献')) + '</div>'
      + '<div class="library-card-footer"><span class="library-card-action">查看详情</span><span class="library-card-date">' + esc(imported === '未知' ? '日期未知' : imported + ' 导入') + '</span></div></article>';
  }
  return '<div class="library-row library-entry' + (isSelected ? ' selected' : '') + (isDeleteSelected ? ' delete-selected' : '') + '" data-id="' + esc(src.source_file_id) + '" data-delete-selectable="' + (isDeleteSelectable ? '1' : '0') + '" aria-selected="' + (isDeleteSelected ? 'true' : 'false') + '" onclick="handleLibraryEntryClick(event,\'' + esc(src.source_file_id) + '\')">'
    + selectionControl
    + '<span class="type-badge ' + typeCls + '">' + typeLabel + '</span>'
    + '<span class="library-row-title">' + thesisIcon + esc(title) + '</span>'
    + '<span class="library-row-author">' + esc(author) + '</span>'
    + '<span class="library-row-info">'
    + statusIconOnly
    + missingIcon
    + (wordStructure ? '<span class="library-card-status">' + esc(wordStructure) + '</span>' : '')
    + '<span class="works-count">' + esc(countMeta) + '</span>'
    + '<span class="library-row-size">' + size + '</span>'
    + '</span>'
    + '</div>';
}

async function selectLibDoc(sourceId) {
  libSelectedId = sourceId;
  document.querySelectorAll('#library-list .library-entry').forEach(function(row) {
    row.classList.toggle('selected', row.dataset.id === sourceId);
  });
  if (!libSources.some(function(s) { return s.source_file_id === sourceId; })) return;
  try {
    await ensureLibraryDetail(sourceId);
  } catch (error) {
    showToast(error && error.message ? error.message : '文献详情读取失败', 'danger');
    return;
  }
  // 详情是异步补齐的，期间用户可能已经关掉抽屉或换了一份文献。
  if (libSelectedId !== sourceId) return;
  var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
  if (!src) return;
  var vol = volumeForSource(sourceId);
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
  if (src.source_type === 'pdf') {
    var ocrLabel = src.parser_type === 'mineru_structured' ? '重新 OCR' : 'MinerU 在线解析';
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

  var fileInfoHTML = '<div class="drawer-collapse" id="drawer-file-info">'
    + '<button class="cal-collapse-head" type="button" aria-expanded="false" onclick="toggleDrawerSection(event,\'drawer-file-info\')">'
    + '<span class="drawer-section-title">文件信息</span>'
    + '<span class="cal-collapse-summary">' + esc(formatFileSize(src.size_bytes) + (src.source_type === 'pdf' && src.pdf_profile && src.pdf_profile.pdf_page_count ? ' · ' + src.pdf_profile.pdf_page_count + ' 页' : '')) + '</span>'
    + '<svg class="cal-collapse-chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>'
    + '</button>'
    + '<div class="drawer-collapse-body" style="display:none"><div class="drawer-info">' + info + '</div></div>'
    + '</div>';

  var el = document.getElementById('library-drawer-content');
  el.innerHTML = '<div class="drawer-title">' + esc(title) + '</div>'
    + (corpusTitle ? '<div class="drawer-subtitle">' + esc(corpusTitle) + '</div>' : '')
    + '<div class="detail-pills" style="margin-top:12px">'
    + '<span class="detail-pill">' + (src.source_type === 'pdf' ? 'PDF' : 'Word') + '</span>'
    + (vol && vol.primary_structure ? '<span class="detail-pill">' + structureLabel(vol.primary_structure) + '</span>' : '')
    + '</div>'
    + fileInfoHTML
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

