/* IIFE 包裹：私有化实现，仅下方公共面挂到全局（#7 前端全局作用域收敛）。
   node 白盒测试走 module.exports；IIFE 实参在 node 下退回 globalThis。 */
(function (global) {  // module: 30-library.js
  /* ═══ Library ═══ */
  function libraryNode(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function librarySvg(paths, className, viewBox) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    if (className) svg.setAttribute('class', className);
    [['viewBox', viewBox || '0 0 24 24'], ['fill', 'none'], ['stroke', 'currentColor'],
      ['stroke-width', '1.8'], ['stroke-linecap', 'round'], ['stroke-linejoin', 'round'],
      ['aria-hidden', 'true']].forEach(function(attribute) { svg.setAttribute(attribute[0], attribute[1]); });
    paths.forEach(function(pathSpec) {
      var node = document.createElementNS('http://www.w3.org/2000/svg', pathSpec[0]);
      Object.keys(pathSpec[1]).forEach(function(name) { node.setAttribute(name, pathSpec[1][name]); });
      svg.appendChild(node);
    });
    return svg;
  }

  var LIBRARY_STATUS_SHAPES = {
    calibrated: [['circle', {cx:'12', cy:'12', r:'9'}], ['path', {d:'m8 12 2.6 2.6L16.5 9'}]],
    pending: [['circle', {cx:'12', cy:'12', r:'9'}], ['path', {d:'M12 7v5l3 2'}]],
    review: [['circle', {cx:'12', cy:'12', r:'9'}], ['path', {d:'M12 7.5v5.5'}], ['path', {d:'M12 16.5h.01'}]],
    failed: [['path', {d:'M12 3 2.8 20h18.4L12 3Z'}], ['path', {d:'M12 9v5'}], ['path', {d:'M12 17.5h.01'}]],
    mapping: [['path', {d:'M20 12a8 8 0 1 1-2.34-5.66'}], ['path', {d:'M20 4v5h-5'}]]
  };

  function statusChipNode(group, status, iconOnly) {
    var label = calibrationStatusLabel(status);
    var chip = libraryNode('span', (iconOnly ? 'cal-status-icon ' : 'cal-status-badge status-chip ') + 'status-chip--' + statusSemanticVariant(group) + ' ' + group);
    if (iconOnly) { chip.title = label; chip.setAttribute('aria-label', label); }
    var icon = libraryNode('span', 'status-chip__icon' + (group === 'mapping' ? ' is-spinning' : ''));
    icon.setAttribute('aria-hidden', 'true');
    icon.appendChild(librarySvg(LIBRARY_STATUS_SHAPES[group] || LIBRARY_STATUS_SHAPES.pending));
    chip.appendChild(icon);
    if (!iconOnly) chip.appendChild(document.createTextNode(label));
    return chip;
  }

  var LIBRARY_STAT_SHAPES = {
    document: [['path', {d:'M6 3h9l3 3v15H6z'}], ['path', {d:'M15 3v4h4'}]],
    book: [['path', {d:'M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5z'}], ['path', {d:'M20 5.5A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5z'}]],
    check: LIBRARY_STATUS_SHAPES.calibrated,
    notice: LIBRARY_STATUS_SHAPES.review
  };

  function statusStatNode(status, label, value, variant, iconName) {
    var button = libraryNode('button', 'status-stat status-stat--' + variant + (libraryStore.statusFilter === status ? ' active' : ''));
    button.type = 'button';
    button.dataset.status = status;
    button.dataset.action = 'applyLibStatusFilter';
    var icon = libraryNode('span', 'status-stat__icon');
    icon.setAttribute('aria-hidden', 'true');
    var svg = librarySvg(LIBRARY_STAT_SHAPES[iconName]);
    svg.setAttribute('width', '16'); svg.setAttribute('height', '16');
    icon.appendChild(svg);
    button.appendChild(icon);
    button.appendChild(libraryNode('span', 'status-stat__label', label));
    button.appendChild(libraryNode('span', 'status-stat__count', value));
    return button;
  }
  function applyLibraryCatalog(data) {
    libraryStore.sources = data.items || [];
    libraryStore.volumes = data.volumes || [];
    libraryStore.volumeBySource = buildVolumeIndex(libraryStore.volumes);
    libraryStore.stats = data.stats || null;
    libraryStore.loaded = true;
    // 搜索下拉与文献库共用同一份摘要，避免两处各拉一次。
    searchStore.sourceFiles = libraryStore.sources;
    searchStore.volumes = libraryStore.volumes;
    searchStore.documentsLoaded = true;
  }

  async function loadLibrary(force) {
    try {
      applyLibraryCatalog(await fetchLibraryCatalog(force));
      renderLibraryStats();
      syncLibraryViewButtons();
      syncLibrarySortControls();
      renderLibraryList();
      await loadDocumentGroups();
      renderLibraryList();
    } catch(e) {
      var empty = libraryNode('div', 'empty-state');
      empty.style.minHeight = '200px';
      empty.appendChild(libraryNode('div', 'empty-state-text', e.message || '文献库加载失败'));
      document.getElementById('library-list').replaceChildren(empty);
    }
  }

  // 作品（作品组）列表供检索范围与文献行的作品链接使用；管理在「译本对照」页。
  async function loadDocumentGroups() {
    try {
      var response = await MEFinderApi.fetch('/api/document-groups');
      var data = await response.json();
      libraryStore.documentGroups = (response.ok && !data.error && Array.isArray(data.document_groups))
        ? data.document_groups : [];
    } catch (_) {
      libraryStore.documentGroups = [];
    }
    if (searchStore.groupId && !libraryStore.documentGroups.some(function(g) { return g.document_group_id === searchStore.groupId; })) {
      searchStore.groupId = '';
      updateSearchDocumentLabel();
    }
  }

  // 下拉在可滚动容器（书目抽屉）内，用 fixed 菜单按 trigger 定位，避免被容器 overflow 裁切。
  // fixed 菜单不随外层滚动移动，所以监听滚动/缩放：trigger 还在视野内就跟随重定位，
  // 滚出视野就关闭，避免菜单脱离触发器悬浮在别处。
  var fixedSelectFollowHandler = null;
  function positionFixedSelectMenu(el) {
    var trigger = el.querySelector('.app-select-trigger');
    var menu = el.querySelector('.app-select-menu');
    if (!trigger || !menu) return;
    var rect = trigger.getBoundingClientRect();
    menu.style.position = 'fixed';
    menu.style.top = (rect.bottom + 6) + 'px';
    menu.style.left = rect.left + 'px';
    menu.style.width = Math.max(rect.width, 240) + 'px';
  }
  function detachFixedSelectFollow() {
    if (!fixedSelectFollowHandler) return;
    window.removeEventListener('scroll', fixedSelectFollowHandler, true);
    window.removeEventListener('resize', fixedSelectFollowHandler, true);
    fixedSelectFollowHandler = null;
  }
  function openVersionSelect(event, selectId) {
    if (event) event.stopPropagation();
    var el = document.getElementById(selectId);
    if (!el) return;
    var willOpen = !el.classList.contains('is-open');
    detachFixedSelectFollow();
    closeAppSelects();
    el.classList.toggle('is-open', willOpen);
    if (!willOpen) return;
    var trigger = el.querySelector('.app-select-trigger');
    var menu = el.querySelector('.app-select-menu');
    if (!trigger || !menu) return;
    positionFixedSelectMenu(el);
    fixedSelectFollowHandler = function() {
      var open = document.getElementById(selectId);
      if (!open || !open.classList.contains('is-open')) { detachFixedSelectFollow(); return; }
      var trig = open.querySelector('.app-select-trigger');
      if (!trig) { detachFixedSelectFollow(); return; }
      var r = trig.getBoundingClientRect();
      if (r.bottom <= 0 || r.top >= window.innerHeight) { closeAppSelects(); detachFixedSelectFollow(); return; }
      positionFixedSelectMenu(open);
    };
    // capture=true 才能收到内层滚动容器（抽屉 / 弹窗正文）的 scroll 事件。
    window.addEventListener('scroll', fixedSelectFollowHandler, true);
    window.addEventListener('resize', fixedSelectFollowHandler, true);
  }

  // 去掉下载站噪声（z-library / 1lib / libgen / Anna's Archive 等）括号段，只用于展示，不改数据。
  function cleanSourceLabel(text) {
    return String(text || '')
      .replace(/\s*[（(\[【][^（()\[\]【】]*(?:z-?lib|1lib|zlib|libgen|anna'?s|annas|b-ok|bookos|sci-?hub)[^（()\[\]【】]*[)）\]】]/gi, '')
      .replace(/\s{2,}/g, ' ')
      .trim();
  }

  // 映射区间、识别证据、PDF 剖面和收录作品只在详情抽屉里用，按 source_id 单份读取。
  function ensureLibraryDetail(sourceId) {
    if (!sourceId || libraryStore.detailLoaded[sourceId]) return Promise.resolve();
    if (libraryStore.detailPending[sourceId]) return libraryStore.detailPending[sourceId];
    var request = MEFinderApi.fetch('/api/library/document?source_id=' + encodeURIComponent(sourceId)).then(function(response) {
      return response.json().then(function(data) {
        if (!response.ok || data.error) throw new Error(data.error || '文献详情读取失败');
        applyLibraryDetail(sourceId, data);
      });
    }).then(function() {
      delete libraryStore.detailPending[sourceId];
    }, function(error) {
      delete libraryStore.detailPending[sourceId];
      throw error;
    });
    libraryStore.detailPending[sourceId] = request;
    return request;
  }

  function applyLibraryDetail(sourceId, data) {
    var detail = data.item || {};
    // libraryStore.sources 与 searchStore.sourceFiles 指向同一个数组，就地替换让两处同时拿到完整记录。
    var index = libraryStore.sources.findIndex(function(item) { return item.source_file_id === sourceId; });
    if (index >= 0) libraryStore.sources[index] = Object.assign({}, libraryStore.sources[index], detail);
    if (data.volume && data.volume.source_file_id) {
      var volumeIndex = libraryStore.volumes.findIndex(function(item) { return item.source_file_id === sourceId; });
      if (volumeIndex >= 0) libraryStore.volumes[volumeIndex] = data.volume;
      else libraryStore.volumes.push(data.volume);
      libraryStore.volumeBySource.set(sourceId, data.volume);
    }
    var volumeId = data.volume ? data.volume.volume_id : null;
    if (volumeId) {
      libraryStore.works = libraryStore.works.filter(function(work) { return work.volume_id !== volumeId; }).concat(data.works || []);
    }
    libraryStore.detailLoaded[sourceId] = true;
  }

  function renderLibraryStats() {
    var container = document.getElementById('library-stats');
    if (!container) return;
    var current = {total:0,calibrated:0,page_pending:0,bibliographic:0};
    libraryGroupScopedSources().forEach(function(item) {
      if (item.source_type !== 'pdf') return;
      current.total += 1;
      var group = calibrationStatusGroup(item.status);
      if (group === 'calibrated') current.calibrated += 1;
      else current.page_pending += 1;
      if (bibliographicMissingFields(sourceBibliographicMetadata(item)).length > 0) current.bibliographic += 1;
    });
    // W1：拆成「待处理」行动组（重）+「参考量」组（轻），一眼看出现在该处理什么。
    var pending = libraryNode('div', 'stat-group stat-group--pending');
    pending.appendChild(libraryNode('span', 'stat-group__label', '待处理'));
    pending.appendChild(statusStatNode('page_pending', '页码待处理', current.page_pending, 'warning', 'notice'));
    pending.appendChild(statusStatNode('bibliographic', '书目待补全', current.bibliographic, 'neutral', 'book'));
    var reference = libraryNode('div', 'stat-group stat-group--reference');
    reference.appendChild(statusStatNode('pdf_all', 'PDF 总数', current.total, 'info', 'document'));
    reference.appendChild(statusStatNode('calibrated', '已校准', current.calibrated, 'success', 'check'));
    container.replaceChildren(pending, libraryNode('span', 'library-controls-spacer'), reference);
  }

  // 主流范式（Notion / Linear / Zotero）：三个筛选收进一个「筛选」按钮 + 弹层分面；
  // 只有正在生效的筛选才作为可删 chip 露出来，按钮带数字角标。渲染由 renderLibraryList 触发。
  function libDocTypeLabel(v) {
    return v === 'book' ? '著作' : v === 'journal_article' ? '期刊论文'
      : v === 'thesis' ? '学位论文' : v === 'unknown' ? '未识别' : '全部类型';
  }

  function libraryFileFacet(source) {
    if (source && source.source_type === 'pdf') return 'pdf';
    return sourceFormatLabel(source) === 'EPUB' ? 'epub' : 'word';
  }

  // 当前生效的（非「全部」）筛选，按「类型 → 语言 → 文件」次序，供角标与 chips 使用。
  function libFilterActiveList() {
    var out = [];
    if (libraryStore.documentTypeFilter !== 'all') out.push({kind:'doctype', label:libDocTypeLabel(libraryStore.documentTypeFilter)});
    if (libraryStore.languageFilter !== 'all') out.push({kind:'lang', label:libLangChipLabel(libraryStore.languageFilter)});
    if (libraryStore.typeFilter !== 'all') out.push({kind:'type', label:libraryStore.typeFilter === 'word' ? 'Word' : libraryStore.typeFilter === 'epub' ? 'EPUB' : 'PDF'});
    return out;
  }

  function renderLibraryFilterBar() {
    var scopeSources = libraryGroupScopedSources();
    var allCount = scopeSources.length;
    var wordCount = scopeSources.filter(function(s){ return libraryFileFacet(s) === 'word'; }).length;
    var epubCount = scopeSources.filter(function(s){ return libraryFileFacet(s) === 'epub'; }).length;
    var pdfCount = scopeSources.filter(function(s){ return libraryFileFacet(s) === 'pdf'; }).length;
    var journalCount = scopeSources.filter(function(s){ return libraryDocType(s) === 'journal_article'; }).length;
    var thesisCount = scopeSources.filter(function(s){ return libraryDocType(s) === 'thesis'; }).length;
    // 著作正向计数：已确认类型的图书 PDF；未识别单列一档（L-15）。
    var bookCount = scopeSources.filter(function(s){ return s.source_type === 'pdf' && isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)) && libraryDocType(s) === 'book'; }).length;
    var unknownCount = scopeSources.filter(function(s){ return s.source_type === 'pdf' && !isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)); }).length;

    var doctypeOpts = [
      {v:'all', label:'全部类型', n:allCount},
      {v:'book', label:'著作', n:bookCount},
      {v:'journal_article', label:'期刊论文', n:journalCount},
      {v:'thesis', label:'学位论文', n:thesisCount}
    ];
    if (unknownCount > 0) doctypeOpts.push({v:'unknown', label:'未识别', n:unknownCount});

    // 语言细类只显示库内实际存在的语言；设置项仍只控制中文/外文两大类的先后。
    var languageOptions = libraryLanguageFacetOptions(scopeSources, libraryStore.defaultLanguage);
    if (libraryStore.languageFilter !== 'all' && !languageOptions.some(function(option) { return option.v === libraryStore.languageFilter; })) libraryStore.languageFilter = 'all';
    var langOpts = [{v:'all', label:'全部语言', n:allCount}].concat(languageOptions);

    var typeOpts = [
      {v:'all', label:'全部', n:allCount},
      {v:'word', label:'Word', n:wordCount},
      {v:'epub', label:'EPUB', n:epubCount},
      {v:'pdf', label:'PDF', n:pdfCount}
    ];

    renderLibraryFacet('filter-opts-doctype', doctypeOpts, libraryStore.documentTypeFilter, 'doctype');
    renderLibraryFacet('filter-opts-lang', langOpts, libraryStore.languageFilter, 'lang');
    renderLibraryFacet('filter-opts-type', typeOpts, libraryStore.typeFilter, 'type');

    var active = libFilterActiveList();
    var badge = document.getElementById('library-filter-badge');
    if (badge) { badge.textContent = String(active.length); badge.hidden = active.length === 0; }
    var container = document.getElementById('library-filter');
    if (container) container.classList.toggle('has-active', active.length > 0);
    var chips = document.getElementById('library-filter-chips');
    if (chips) {
      chips.replaceChildren();
      active.forEach(function(a) {
        var chip = libraryNode('button', 'library-filter-chip');
        chip.type = 'button';
        chip.title = '移除筛选：' + a.label;
        chip.setAttribute('aria-label', chip.title);
        chip.dataset.action = 'removeLibraryFacet';
        chip.dataset.kind = a.kind;
        chip.appendChild(libraryNode('span', null, a.label));
        var remove = librarySvg([['path', {d:'M5 5l10 10M15 5L5 15'}]], null, '0 0 20 20');
        remove.setAttribute('stroke-width', '2');
        remove.removeAttribute('stroke-linejoin');
        chip.appendChild(remove);
        chips.appendChild(chip);
      });
    }
  }

  function renderLibraryFacet(containerId, options, active, kind) {
    var el = document.getElementById(containerId);
    if (!el) return;
    el.replaceChildren();
    options.forEach(function(o) {
      var button = libraryNode('button', 'filter-opt' + (o.v === active ? ' is-on' : ''));
      button.type = 'button';
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', o.v === active ? 'true' : 'false');
      button.dataset.action = 'setLibraryFacet';
      button.dataset.kind = kind;
      button.dataset.value = o.v;
      button.appendChild(libraryNode('span', null, o.label));
      button.appendChild(libraryNode('span', 'filter-opt-n', o.n));
      el.appendChild(button);
    });
  }

  // 选中某个分面：立即生效并重绘（弹层保持打开，可连续多选，Notion 式）。
  async function setLibFacet(event, kind, value) {
    if (event) event.stopImmediatePropagation();
    if (!await global.MEFinder.bibliography.guardLeaveDetail()) return;
    if (kind === 'doctype') libraryStore.documentTypeFilter = value;
    else if (kind === 'lang') libraryStore.languageFilter = value;
    else if (kind === 'type') {
      libraryStore.typeFilter = value;
      if (['word','epub'].indexOf(libraryStore.typeFilter) >= 0 && libraryStore.statusFilter !== 'all') { libraryStore.statusFilter = 'all'; renderLibraryStats(); }
    }
    global.MEFinder.bibliography.closeDrawer();
    renderLibraryList();
  }

  // 移除单个生效筛选（点 chip 的 ✕），把该分面复位到「全部」。
  async function removeLibFacet(event, kind) {
    if (event) event.stopImmediatePropagation();
    if (!await global.MEFinder.bibliography.guardLeaveDetail()) return;
    if (kind === 'doctype') libraryStore.documentTypeFilter = 'all';
    else if (kind === 'lang') libraryStore.languageFilter = 'all';
    else if (kind === 'type') libraryStore.typeFilter = 'all';
    global.MEFinder.bibliography.closeDrawer();
    renderLibraryList();
  }

  // 切换排序方向（升/降），合并排序控件里的方向按钮。
  function toggleLibrarySortDirection() {
    libraryStore.sortDirection = libraryStore.sortDirection === 'asc' ? 'desc' : 'asc';
    try { localStorage.setItem('meFinderLibrarySortDirection', libraryStore.sortDirection); } catch (_) {}
    syncLibrarySortControls();
    closeAppSelects();
    renderLibraryList();
  }

  async function applyLibStatusFilter(status) {
    if (!await global.MEFinder.bibliography.guardLeaveDetail()) return;
    var requested = status || 'all';
    libraryStore.statusFilter = requested === libraryStore.statusFilter ? 'all' : requested;
    if (libraryStore.statusFilter !== 'all' && ['word','epub'].indexOf(libraryStore.typeFilter) >= 0) {
      libraryStore.typeFilter = 'all';  // 筛选按钮/chips 由 renderLibraryFilterBar 随列表重绘刷新
    }
    global.MEFinder.bibliography.closeDrawer();
    renderLibraryStats();
    renderLibraryList();
  }

  function setLibDefaultLanguage(btn) {
    var value = btn && btn.dataset ? btn.dataset.deflang : btn;
    value = value === 'foreign' ? 'foreign' : 'chinese';
    if (value === libraryStore.defaultLanguage) return;
    libraryStore.defaultLanguage = value;
    try { localStorage.setItem('meFinderLibDefaultLanguage', value); } catch (_) {}
    persistDisplayPreference('lib_default_language', value);  // 随数据备份/迁移（C-01）
    syncLibDefaultLanguageControl();
    renderLibraryList();  // 重绘以刷新语言筛选条的标签与「本国/外文」归属
  }

  function syncLibDefaultLanguageControl() {
    document.querySelectorAll('#lib-default-lang-control .seg-btn').forEach(function(b) {
      b.classList.toggle('active', b.dataset.deflang === libraryStore.defaultLanguage);
    });
  }

  // 清空所有筛选与搜索，回到全部（空态「清除全部筛选」、筛选弹层「清除全部」用）。
  function clearLibraryFilters() {
    libraryStore.typeFilter = 'all';
    libraryStore.languageFilter = 'all';
    libraryStore.documentTypeFilter = 'all';
    libraryStore.statusFilter = 'all';
    var search = document.getElementById('lib-search');
    if (search) search.value = '';
    renderLibraryStats();
    renderLibraryList();
  }

  function filterLibrary() {
    // 输入法与连续输入下，91 份以上的列表每敲一个字重排一次会明显发涩。
    if (libraryStore.filterTimer) clearTimeout(libraryStore.filterTimer);
    libraryStore.filterTimer = setTimeout(function() {
      libraryStore.filterTimer = null;
      renderLibraryList();
    }, 160);
  }

  function setLibraryView(mode) {
    libraryStore.viewMode = mode === 'grid' ? 'grid' : 'list';
    localStorage.setItem('meFinderLibraryView', libraryStore.viewMode);
    persistDisplayPreference('library_view', libraryStore.viewMode);
    syncLibraryViewButtons();
    renderLibraryList();
  }

  function syncLibraryViewButtons() {
    ['list','grid'].forEach(function(mode) {
      var button = document.getElementById('library-view-' + mode);
      if (!button) return;
      var active = libraryStore.viewMode === mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  function setLibrarySortOption(event, control, value) {
    event.stopPropagation();
    if (control === 'direction') {
      libraryStore.sortDirection = value === 'asc' ? 'asc' : 'desc';
      localStorage.setItem('meFinderLibrarySortDirection', libraryStore.sortDirection);
    } else {
      libraryStore.sortField = ['imported_at','title','author','modified_at','source_type','status'].indexOf(value) >= 0 ? value : 'imported_at';
      localStorage.setItem('meFinderLibrarySortField', libraryStore.sortField);
    }
    syncLibrarySortControls();
    closeAppSelects();
    renderLibraryList();
  }

  function syncLibrarySortControls() {
    var labels = {imported_at:'导入时间',title:'书名',author:'作者',modified_at:'最近修改时间',source_type:'来源类型',status:'校准状态',desc:'降序',asc:'升序'};
    var fieldLabel = document.getElementById('library-sort-field-label');
    if (fieldLabel) fieldLabel.textContent = labels[libraryStore.sortField] || labels.imported_at;
    document.querySelectorAll('#library-sort-field-select .app-select-option').forEach(function(option) {
      option.classList.toggle('is-selected', option.dataset.value === libraryStore.sortField);
    });
    // 方向合并成一个可点按钮：文案随升/降切换，箭头方向靠 .is-asc 翻转。
    var dirBtn = document.getElementById('library-sort-dir');
    var dirLabel = document.getElementById('library-sort-dir-label');
    if (dirLabel) dirLabel.textContent = labels[libraryStore.sortDirection] || labels.desc;
    if (dirBtn) {
      dirBtn.classList.toggle('is-asc', libraryStore.sortDirection === 'asc');
      dirBtn.setAttribute('aria-label', libraryStore.sortDirection === 'asc' ? '升序，点击改为降序' : '降序，点击改为升序');
    }
  }

  function compareLibraryDates(a, b) {
    var av = Date.parse(a || '') || 0;
    var bv = Date.parse(b || '') || 0;
    if (!av && !bv) return 0;
    if (!av) return 1;
    if (!bv) return -1;
    return libraryStore.sortDirection === 'desc' ? bv - av : av - bv;
  }

  function libraryGroupScopedSources() {
    return libraryStore.sources.slice();
  }


  function getFilteredSources() {
    let sources = libraryGroupScopedSources();
    if (libraryStore.typeFilter !== 'all') {
      sources = sources.filter(function(source) { return libraryFileFacet(source) === libraryStore.typeFilter; });
    }
    if (libraryStore.languageFilter !== 'all') {
      sources = sources.filter(s => libraryLanguageCode(s) === libraryStore.languageFilter);
    }
    if (libraryStore.documentTypeFilter === 'unknown') {
      // 未识别：从未跑过书目识别的 PDF（类型只是默认回落成 book，并非真的判定过）。
      sources = sources.filter(s => s.source_type === 'pdf' && !isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)));
    } else if (libraryStore.documentTypeFilter === 'book') {
      // 著作：仅已确认类型的图书 PDF；不再把 Word 文集和未识别 PDF 混进来。
      sources = sources.filter(s => s.source_type === 'pdf' && isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)) && libraryDocType(s) === 'book');
    } else if (libraryStore.documentTypeFilter !== 'all') {
      sources = sources.filter(s => libraryDocType(s) === libraryStore.documentTypeFilter);
    }
    if (libraryStore.statusFilter === 'pdf_all') {
      sources = sources.filter(s => s.source_type === 'pdf');
    } else if (libraryStore.statusFilter === 'page_pending') {
      sources = sources.filter(s => s.source_type === 'pdf' && calibrationStatusGroup(s.status) !== 'calibrated');
    } else if (libraryStore.statusFilter === 'bibliographic') {
      sources = sources.filter(s => s.source_type === 'pdf' && bibliographicMissingFields(sourceBibliographicMetadata(s)).length > 0);
    } else if (libraryStore.statusFilter !== 'all') {
      sources = sources.filter(s => s.source_type === 'pdf' && calibrationStatusGroup(s.status) === libraryStore.statusFilter);
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
      if (libraryStore.sortField === 'imported_at' || libraryStore.sortField === 'modified_at') {
        result = compareLibraryDates(left[libraryStore.sortField], right[libraryStore.sortField]);
      } else if (libraryStore.sortField === 'status') {
        var order = {manual_mapped:0,auto_mapped_high:1,unmapped:2,needs_review:3,auto_mapping_failed:4,source_missing:5,mapping:6};
        var av = a.source_type === 'pdf' && order[a.status] != null ? order[a.status] : 99;
        var bv = b.source_type === 'pdf' && order[b.status] != null ? order[b.status] : 99;
        result = libraryStore.sortDirection === 'desc' ? bv - av : av - bv;
      } else {
        result = calibrationSortText(left[libraryStore.sortField], right[libraryStore.sortField], libraryStore.sortDirection);
      }
      return result || calibrationSortText(left.title, right.title, 'asc');
    });
    return sources;
  }


  function updateLibraryDeleteControls() {
    var bar = document.getElementById('library-selection-bar');
    var page = document.getElementById('page-library');
    var count = document.getElementById('library-selection-count');
    var removeButton = document.getElementById('library-remove-selected-btn');
    var exportButton = document.getElementById('library-export-selected-btn');
    var selectVisibleButton = document.getElementById('library-select-visible-btn');
    var selectedCount = libraryStore.deleteSelection.size;
    var selectedPdfCount = libraryStore.sources.filter(function(item) {
      return item.source_type === 'pdf' && libraryStore.deleteSelection.has(item.source_file_id);
    }).length;
    var active = selectedCount > 0;
    // Selection alone drives the contextual action bar: no persistent mode toggle.
    if (page) page.classList.toggle('library-selecting', active);
    if (bar) bar.hidden = !active;
    if (count) count.textContent = '已选 ' + selectedCount + ' 项';
    if (global.MEFinder && global.MEFinder.works) global.MEFinder.works.syncLibraryAssignButton();
    if (removeButton) removeButton.disabled = selectedCount === 0;
    if (exportButton) {
      exportButton.disabled = libraryStore.exportRunning || selectedPdfCount === 0;
      if (!libraryStore.exportRunning) {
        exportButton.textContent = selectedPdfCount
          ? '导出所选 PDF（' + selectedPdfCount + '）'
          : '导出所选 PDF';
      }
    }
    if (selectVisibleButton) {
      var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
      var allSelected = selectable.length > 0 && selectable.every(function(item) {
        return libraryStore.deleteSelection.has(item.source_file_id);
      });
      selectVisibleButton.textContent = allSelected ? '取消全选' : '全选当前';
      selectVisibleButton.disabled = selectable.length === 0;
    }
  }

  function syncLibraryDeleteSelectionUI() {
    document.querySelectorAll('#library-list .library-entry').forEach(function(entry) {
      var selected = libraryStore.deleteSelection.has(entry.dataset.id);
      entry.classList.toggle('delete-selected', selected);
      entry.setAttribute('aria-selected', selected ? 'true' : 'false');
      var input = entry.querySelector('.library-delete-check');
      if (input) input.checked = selected;
    });
    updateLibraryDeleteControls();
  }

  function clearLibrarySelection() {
    if (libraryStore.deleteSelection.size === 0) return;
    libraryStore.deleteSelection.clear();
    syncLibraryDeleteSelectionUI();
  }

  function toggleLibraryDeleteSelection(sourceId, force) {
    var source = libraryStore.sources.find(function(item) { return item.source_file_id === sourceId; });
    if (!isLibraryDeleteSelectable(source)) {
      showToast('当前来源类型暂不支持从文献库移除', 'warning');
      return;
    }
    var selected = typeof force === 'boolean' ? force : !libraryStore.deleteSelection.has(sourceId);
    if (selected) libraryStore.deleteSelection.add(sourceId);
    else libraryStore.deleteSelection.delete(sourceId);
    syncLibraryDeleteSelectionUI();
  }

  function toggleSelectVisibleLibraryDocuments() {
    var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
    var allSelected = selectable.length > 0 && selectable.every(function(item) {
      return libraryStore.deleteSelection.has(item.source_file_id);
    });
    selectable.forEach(function(item) {
      if (allSelected) libraryStore.deleteSelection.delete(item.source_file_id);
      else libraryStore.deleteSelection.add(item.source_file_id);
    });
    syncLibraryDeleteSelectionUI();
  }

  // 列表键盘导航（L-11）：↑↓ 移动焦点，Enter 打开详情，空格切换勾选，Home/End 跳首尾。
  function handleLibraryListKeydown(event) {
    var target = event.target && event.target.closest ? event.target.closest('.library-entry') : null;
    if (!target) return;
    var entries = Array.prototype.slice.call(document.querySelectorAll('#library-list .library-entry'));
    var idx = entries.indexOf(target);
    if (event.key === 'ArrowDown') { event.preventDefault(); if (entries[idx + 1]) entries[idx + 1].focus(); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); if (entries[idx - 1]) entries[idx - 1].focus(); }
    else if (event.key === 'Home') { event.preventDefault(); if (entries[0]) entries[0].focus(); }
    else if (event.key === 'End') { event.preventDefault(); if (entries.length) entries[entries.length - 1].focus(); }
    else if (event.key === 'Enter') { event.preventDefault(); selectLibDoc(target.dataset.id); }
    else if (event.key === ' ' || event.key === 'Spacebar') { event.preventDefault(); toggleLibraryDeleteSelection(target.dataset.id); }
  }

  function setupLibraryKeyboardNav() {
    var list = document.getElementById('library-list');
    if (!list || list.dataset.keyboardReady === '1') return;
    list.dataset.keyboardReady = '1';
    list.addEventListener('keydown', handleLibraryListKeydown);
  }

  function handleLibraryEntryClick(event, sourceId) {
    if (libraryStore.suppressSelectionClick) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    // Clicking the row/card body always opens details; the checkbox is the only
    // thing that toggles selection, so browsing never accidentally selects.
    selectLibDoc(sourceId);
  }

  function renderLibraryList() {
    const sources = getFilteredSources();
    const listEl = document.getElementById('library-list');
    listEl.className = 'library-list-container library-view-' + libraryStore.viewMode;
    libraryStore.deleteSelection.forEach(function(sourceId) {
      if (!libraryStore.sources.some(function(source) { return source.source_file_id === sourceId; })) {
        libraryStore.deleteSelection.delete(sourceId);
      }
    });
    // 筛选按钮角标 + 生效 chips + 弹层三组分面（含实时计数），一处渲染。
    renderLibraryFilterBar();

    libraryStore.renderToken += 1;
    if (sources.length === 0) {
      // 三态空状态：库为空 → 引导导入；有数据但筛选无果 → 清除筛选（L-13）。
      var isEmpty = libraryStore.sources.length === 0;
      var empty = libraryNode('div', 'empty-state');
      empty.style.minHeight = '220px';
      empty.appendChild(libraryNode('div', 'empty-state-text', isEmpty ? '文献库还是空的' : '当前筛选没有匹配文献'));
      empty.appendChild(libraryNode('div', 'empty-state-hint', isEmpty
        ? '导入 PDF、DOCX 或 EPUB 后即可检索、核对页码与上下文' : '换个筛选条件，或清除全部筛选'));
      var action = libraryNode('button', 'action-btn' + (isEmpty ? ' primary' : ''), isEmpty ? '去导入文献' : '清除全部筛选');
      action.style.marginTop = '14px';
      action.dataset.action = isEmpty ? 'navigateToImport' : 'clearLibraryFilters';
      empty.appendChild(action);
      listEl.replaceChildren(empty);
      updateLibraryDeleteControls();
      return;
    }
    // 首批同步渲染，其余按帧追加，避免大文献库一次性构建整张列表阻塞首屏。
    listEl.replaceChildren(...sources.slice(0, LIBRARY_RENDER_BATCH).map(libraryEntryNode));
    syncLibraryDeleteSelectionUI();
    if (sources.length > LIBRARY_RENDER_BATCH) {
      appendLibraryEntries(sources, LIBRARY_RENDER_BATCH, libraryStore.renderToken);
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
      if (token !== libraryStore.renderToken) return;
      var listEl = document.getElementById('library-list');
      if (!listEl) return;
      var end = Math.min(start + LIBRARY_RENDER_BATCH, sources.length);
      listEl.append(...sources.slice(start, end).map(libraryEntryNode));
      syncLibraryDeleteSelectionUI();
      if (end < sources.length) appendLibraryEntries(sources, end, token);
    });
  }

  function libraryEntryNode(src) {
    var vol = volumeForSource(src.source_file_id);
    var isPdf = src.source_type === 'pdf';
    var title = cleanSourceLabel(src.title || src.file_name || src.source_file_id);
    var author = src.author || '作者信息待完善';
    var bib = sourceBibliographicMetadata(src);
    var missingMetadataText = isPdf ? bibliographicMissingText(bib) : '';
    var isSelected = src.source_file_id === libraryStore.selectedId;
    var isDeleteSelectable = isLibraryDeleteSelectable(src);
    var isDeleteSelected = libraryStore.deleteSelection.has(src.source_file_id);
    var isStructuredPdf = isPdf && /_structured$/.test(src.parser_type || '');
    var typeCls = isPdf ? (isStructuredPdf ? 'json' : 'pdf') : 'word';
    var typeLabel = isPdf ? (isStructuredPdf ? 'JSON' : 'PDF') : sourceFormatLabel(src);
    var itemStatus = isPdf ? (calTransientStatus[src.source_file_id] || src.status) : '';
    var statusGroup = isPdf ? calibrationStatusGroup(itemStatus) : '';
    var wordStructure = !isPdf && vol && vol.primary_structure ? structureLabel(vol.primary_structure) : '';
    var countMeta = isPdf ? (src.page_count ? src.page_count + ' 页' : '页数未知') : ((src.works_count || 1) + ' 篇');
    var work = global.MEFinder && global.MEFinder.works ? global.MEFinder.works.workLinkFor(src.source_file_id) : null;
    var entry = libraryNode(libraryStore.viewMode === 'grid' ? 'article' : 'div',
      (libraryStore.viewMode === 'grid' ? 'library-card' : 'library-row') + ' library-entry'
      + (isSelected ? ' selected' : '') + (isDeleteSelected ? ' delete-selected' : ''));
    entry.tabIndex = 0;
    entry.setAttribute('role', 'option');
    entry.dataset.action = 'openLibraryEntry';
    entry.dataset.id = src.source_file_id;
    entry.dataset.deleteSelectable = isDeleteSelectable ? '1' : '0';
    entry.setAttribute('aria-selected', isDeleteSelected ? 'true' : 'false');

    function selectionControl() {
      if (!isDeleteSelectable) return null;
      var check = libraryNode('input', 'library-delete-check');
      check.type = 'checkbox';
      check.setAttribute('aria-label', '选择 ' + title);
      check.checked = isDeleteSelected;
      check.dataset.action = 'toggleLibraryEntrySelection';
      check.dataset.sourceId = src.source_file_id;
      return check;
    }
    function thesisTitle(className) {
      var node = libraryNode(libraryStore.viewMode === 'grid' ? 'div' : 'span', className);
      if (src.document_type === 'thesis') {
        var svg = librarySvg([['path', {d:'M22 10 12 5 2 10l10 5 10-5Z'}],
          ['path', {d:'M6 12v5c0 1 2.7 2.5 6 2.5s6-1.5 6-2.5v-5'}],
          ['path', {d:'M22 10v5'}]], 'doc-thesis-icon');
        svg.setAttribute('aria-label', '学位论文');
        var svgTitle = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        svgTitle.textContent = '学位论文';
        svg.insertBefore(svgTitle, svg.firstChild);
        node.appendChild(svg);
      }
      node.appendChild(document.createTextNode(title));
      return node;
    }
    function workLink() {
      if (!work) return null;
      var link = libraryNode('button', 'library-row-work', work.title);
      link.type = 'button';
      link.title = '在译本对照中打开《' + work.title + '》';
      link.dataset.action = 'openLibraryWork';
      link.dataset.workId = work.id;
      return link;
    }
    function missingBadge(iconOnly) {
      var badge = libraryNode('span', iconOnly ? 'library-row-missing-icon' : 'bibliographic-missing');
      badge.title = iconOnly ? missingMetadataText : 'ISBN、ISSN 与 DOI 不计入引文必需字段';
      if (iconOnly) badge.setAttribute('aria-label', missingMetadataText);
      badge.appendChild(librarySvg(iconOnly
        ? [['path', {d:'M12 3 2.8 20h18.4L12 3Z'}], ['path', {d:'M12 9v5'}], ['path', {d:'M12 17.5h.01'}]]
        : [['circle', {cx:'12', cy:'12', r:'9'}], ['path', {d:'M12 7.5v5.5'}], ['path', {d:'M12 16.5h.01'}]]));
      if (!iconOnly) badge.appendChild(libraryNode('span', null, missingMetadataText));
      return badge;
    }
    if (libraryStore.viewMode === 'grid') {
      var top = libraryNode('div', 'library-card-top');
      var badges = libraryNode('div', 'library-card-badges');
      badges.appendChild(libraryNode('span', 'type-badge ' + typeCls, typeLabel));
      if (isPdf) badges.appendChild(statusChipNode(statusGroup, itemStatus, false));
      if (wordStructure) badges.appendChild(libraryNode('span', 'library-card-status', wordStructure));
      var secondary = !isPdf ? ((vol && vol.corpus_title) || '') : '';
      if (secondary) badges.appendChild(libraryNode('span', 'library-card-status', secondary));
      top.appendChild(badges);
      var selection = selectionControl();
      if (selection) top.appendChild(selection);
      entry.appendChild(top);
      entry.appendChild(thesisTitle('library-card-title'));
      entry.appendChild(libraryNode('div', 'library-card-author', author));
      var link = workLink();
      if (link) { var wrapper = libraryNode('div', 'library-card-work'); wrapper.appendChild(link); entry.appendChild(wrapper); }
      if (missingMetadataText) entry.appendChild(missingBadge(false));
      entry.appendChild(libraryNode('div', 'library-card-meta', countMeta + ' · ' + formatFileSize(src.size_bytes)));
      entry.appendChild(libraryNode('div', 'library-card-mapping', isPdf
        ? (src.mapping_summary || '尚未建立引用页码映射') : ((vol && vol.version_info) || typeLabel + ' 文献')));
      var footer = libraryNode('div', 'library-card-footer');
      footer.appendChild(libraryNode('span', 'library-card-action', '查看详情'));
      var imported = formatCalDate(src.imported_at || src.last_modified);
      footer.appendChild(libraryNode('span', 'library-card-date', imported === '未知' ? '日期未知' : imported + ' 导入'));
      entry.appendChild(footer);
    } else {
      var check = selectionControl();
      if (check) entry.appendChild(check);
      entry.appendChild(libraryNode('span', 'type-badge ' + typeCls, typeLabel));
      entry.appendChild(thesisTitle('library-row-title'));
      var rowLink = workLink();
      if (rowLink) entry.appendChild(rowLink);
      entry.appendChild(libraryNode('span', 'library-row-author', author));
      var info = libraryNode('span', 'library-row-info');
      if (isPdf) info.appendChild(statusChipNode(statusGroup, itemStatus, true));
      if (missingMetadataText) info.appendChild(missingBadge(true));
      if (wordStructure) info.appendChild(libraryNode('span', 'library-card-status', wordStructure));
      info.appendChild(libraryNode('span', 'works-count', countMeta));
      info.appendChild(libraryNode('span', 'library-row-size', formatFileSize(src.size_bytes)));
      entry.appendChild(info);
    }
    return entry;
  }

  // 详情抽屉按插槽渲染，各部分直接构造节点：
  //   #library-drawer-content = 上一条/下一条 + 标题 + 徽章 + 书目区（读/写态）
  //   #library-drawer-calibration（模板静态卡片，介于两个插槽之间）= 页码校准
  //   #library-drawer-extra = 收录文献 + 文件信息 + 主操作栏
  // 区块顺序落为：书目 → 页码校准 → 收录文献 → 文件信息 → 主操作。
  function drawerNavNode(sourceId) {
    var list = getFilteredSources();
    var idx = list.findIndex(function(s) { return s.source_file_id === sourceId; });
    if (idx < 0 || list.length <= 1) return null;
    var prevId = idx > 0 ? list[idx - 1].source_file_id : '';
    var nextId = idx < list.length - 1 ? list[idx + 1].source_file_id : '';
    function btn(id, label, arrow) {
      var button = libraryNode('button', 'drawer-nav-btn', arrow);
      button.type = 'button';
      button.setAttribute('aria-label', label);
      if (id) { button.dataset.action = 'selectLibDoc'; button.dataset.sourceId = id; }
      else button.disabled = true;
      return button;
    }
    var nav = libraryNode('div', 'drawer-nav');
    nav.appendChild(btn(prevId, '上一条文献', '‹'));
    var position = libraryNode('span', 'drawer-nav-pos', (idx + 1) + ' / ' + list.length);
    position.setAttribute('aria-live', 'polite');
    nav.appendChild(position);
    nav.appendChild(btn(nextId, '下一条文献', '›'));
    return nav;
  }

  function drawerStatusPillNode(src) {
    if (src.source_type !== 'pdf') return null;
    var status = calTransientStatus[src.source_file_id] || src.status;
    var group = calibrationStatusGroup(status);
    return statusChipNode(group, status, false);
  }

  // 收录文献：不再内层滚动（L-14），随抽屉整体滚动，避免滚轮被内层吞掉。
  function drawerWorksNode(works) {
    var fragment = document.createDocumentFragment();
    if (!works.length) return fragment;
    fragment.appendChild(libraryNode('div', 'drawer-section-title', '收录文献 (' + works.length + ')'));
    var list = libraryNode('div', 'drawer-works-list');
    works.forEach(function(w) {
        var meta = [];
        if (w.author_label) meta.push(w.author_label);
        if (w.date_label) meta.push(w.date_label);
        if (w.toc_page_start) meta.push('p.' + w.toc_page_start + (w.toc_page_end ? '–' + w.toc_page_end : ''));
        var item = libraryNode('div', 'drawer-work-item');
        item.appendChild(libraryNode('div', 'drawer-work-title', w.title));
        if (meta.length) item.appendChild(libraryNode('div', 'drawer-work-meta', meta.join(' · ')));
        list.appendChild(item);
      });
    fragment.appendChild(list);
    return fragment;
  }

  function drawerFileInfoNode(src, vol) {
    var info = libraryNode('div', 'drawer-info');
    function row(label, value) {
      var item = libraryNode('div', 'drawer-info-row');
      item.appendChild(libraryNode('span', 'drawer-info-label', label));
      item.appendChild(libraryNode('span', 'drawer-info-value', String(value || '—')));
      info.appendChild(item);
    }
    row('文件类型', sourceFormatLabel(src) + ' 文档');
    row('文件名', src.file_name);
    row('大小', formatFileSize(src.size_bytes));
    if (src.source_type === 'pdf' && src.pdf_profile) {
      row('PDF 页数', src.pdf_profile.pdf_page_count + ' 页');
      row('PDF 类型', pdfTypeLabel(src.pdf_profile.detected_pdf_type));
      // 「用什么解析的」：具体解析器 + 模型，从列表徽标移到此处，徽标只留格式。
      var parserWay = src.parser_type === 'native_text'
        ? '原生文本层（PDF 自带文本）'
        : ((src.parser_label || pdfTypeLabel(src.pdf_profile.detected_pdf_type))
            + (src.pdf_profile.model ? ' · ' + src.pdf_profile.model : '')
            + '（结构化 JSON）');
      row('解析方式', parserWay);
      row('页码状态', mappingStatusLabel(src.pdf_profile.mapping_status));
      if (src.pdf_profile.auto_page_mapping) {
        var autoMap = src.pdf_profile.auto_page_mapping;
        row('自动页码映射', autoMap.method === 'manual_override'
          ? '保留人工映射'
          : '应用 ' + (autoMap.applied_segment_count || 0) + ' 个自动段，候选 ' + (autoMap.candidate_count || 0) + ' 个');
        if (autoMap.applied_segments && autoMap.applied_segments.length) row('自动映射区间', autoMap.applied_segments.map(autoMappingSegmentText).join('；'));
        if (autoMap.exception_pages && autoMap.exception_pages.length) row('异常页面', autoMap.exception_pages.length + ' 页');
      }
    }
    if (src.last_modified) row('修改日期', src.last_modified.split('T')[0]);
    if (vol && vol.version_info) row('版本', vol.version_info);
    var collapse = libraryNode('div', 'drawer-collapse');
    collapse.id = 'drawer-file-info';
    var toggle = libraryNode('button', 'cal-collapse-head');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.dataset.action = 'toggleDrawerFileInfo';
    toggle.appendChild(libraryNode('span', 'drawer-section-title', '文件信息'));
    toggle.appendChild(libraryNode('span', 'cal-collapse-summary', formatFileSize(src.size_bytes)
      + (src.source_type === 'pdf' && src.pdf_profile && src.pdf_profile.pdf_page_count ? ' · ' + src.pdf_profile.pdf_page_count + ' 页' : '')));
    toggle.appendChild(librarySvg([['path', {d:'m6 8 4 4 4-4'}]], 'cal-collapse-chevron', '0 0 20 20'));
    collapse.appendChild(toggle);
    var body = libraryNode('div', 'drawer-collapse-body');
    body.style.display = 'none';
    body.appendChild(info);
    collapse.appendChild(body);
    return collapse;
  }

  // 主操作栏收敛为「打开原文」+ ⋯（重新解析 / 导出 / 页码动作 / 移除）。
  // 「自动检测页码 / 编辑区间」不再在这里重复——页码校准卡片是唯一入口（L-04）。
  function drawerMainActionsNode(src) {
    var sid = src.source_file_id;
    var isPdf = src.source_type === 'pdf';
    var canExportMarkdown = isPdf || sourceFormatLabel(src) === 'EPUB';
    var actions = libraryNode('div', 'drawer-actions');
    function action(label, name, className) {
      var button = libraryNode('button', className || 'action-btn', label);
      button.type = 'button';
      button.dataset.action = name;
      button.dataset.sourceId = sid;
      return button;
    }
    function menuItem(menu, label, name, danger, note, disabled) {
      var button = action(label, name, 'bib-menu-item' + (danger ? ' bib-menu-item-danger' : ''));
      button.setAttribute('role', 'menuitem');
      if (note) button.appendChild(libraryNode('span', 'bib-menu-note', note));
      button.disabled = !!disabled;
      menu.appendChild(button);
    }
    if (sid) {
      actions.appendChild(action('打开原文', 'openLibrarySource', 'action-btn primary'));
      var read = action('阅读', 'readLibrarySource');
      read.title = '在阅读器中阅读结构化正文';
      actions.appendChild(read);
    }
    actions.appendChild(libraryNode('span', 'drawer-actions-spacer'));
    var wrap = libraryNode('span', 'bib-menu-wrap');
    var more = libraryNode('button', 'action-btn bib-caret-only');
    more.type = 'button';
    more.setAttribute('aria-label', '更多操作');
    more.setAttribute('aria-haspopup', 'true');
    more.setAttribute('aria-expanded', 'false');
    more.setAttribute('aria-controls', 'drawer-more-menu');
    more.dataset.action = 'toggleDrawerMoreMenu';
    var icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    [['viewBox','0 0 24 24'], ['width','18'], ['height','18'], ['fill','currentColor'],
      ['aria-hidden','true']].forEach(function(attribute) { icon.setAttribute(attribute[0], attribute[1]); });
    ['5','12','19'].forEach(function(cx) {
      var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', cx); circle.setAttribute('cy', '12'); circle.setAttribute('r', '1.6');
      icon.appendChild(circle);
    });
    more.appendChild(icon);
    wrap.appendChild(more);
    var menu = libraryNode('span', 'bib-menu bib-menu-end drawer-actions-menu');
    menu.id = 'drawer-more-menu';
    menu.setAttribute('role', 'menu');
    var hasParse = false, hasExport = false;
    if (isPdf) {
      hasParse = true;
      menu.appendChild(libraryNode('div', 'bib-menu-head', '解析'));
      var running = calTransientStatus[sid] === 'mapping';
      menuItem(menu, running ? '正在解析…' : src.parser_type === 'mineru_structured' ? '重新 OCR' : 'MinerU 在线解析',
        'submitDrawerMineruReparse', false, null, running);
      var autoMap = src.pdf_profile && src.pdf_profile.auto_page_mapping;
      if (autoMap && autoMap.applied_segments && autoMap.applied_segments.length) menuItem(menu, '接受自动映射', 'acceptDrawerAutoMapping');
      if (autoMap && autoMap.exception_pages && autoMap.exception_pages.length) menuItem(menu, '检查异常', 'showDrawerAutoMappingExceptions');
    }
    if (isPdf || canExportMarkdown) {
      hasExport = true;
      if (hasParse) menu.appendChild(libraryNode('div', 'bib-menu-sep'));
      menu.appendChild(libraryNode('div', 'bib-menu-head', '导出为'));
      if (isPdf) menuItem(menu, 'MEFinder 文档包', 'exportLibraryDocument');
      if (canExportMarkdown) {
        menuItem(menu, 'Markdown', 'exportLibraryDocumentMarkdown');
        menuItem(menu, '按页 Markdown', 'openMarkdownPageExport', false, '选页');
      }
      if (isPdf) menuItem(menu, 'EPUB', 'exportLibraryDocumentEpub');
    }
    if (hasParse || hasExport) menu.appendChild(libraryNode('div', 'bib-menu-sep'));
    menuItem(menu, '从文献库移除', 'openRemoveDocumentModal', true);
    wrap.appendChild(menu);
    actions.appendChild(wrap);
    return actions;
  }

  async function exportLibraryDocument(sourceId) {
    if (!sourceId) return;
    try {
      var outputDirectory = await chooseDesktopExportDirectory();
      if (outputDirectory === null) return;
      showToast('正在导出 MEFinder 文档包…');
      var data = await requestLibraryDocumentExport(sourceId, outputDirectory);
      showToast('已导出 ' + Number(data.page_count || 0).toLocaleString()
        + ' 页' + (data.includes_source_pdf ? '，包含原 PDF' : '')
        + ' 到：' + data.path + '（' + formatFileSize(data.size_bytes) + '）');
    } catch (error) {
      showToast('导出 MEFinder 文档失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
    }
  }

  async function requestLibraryDocumentExport(sourceId, outputDirectory) {
    var payload = {
      source_id: sourceId,
      include_source_pdf: settingsStore.currentDocumentExportMode === 'with_pdf'
    };
    if (outputDirectory) payload.output_dir = outputDirectory;
    var response = await MEFinderApi.fetch('/api/document/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '导出失败');
    return data;
  }

  var markdownPageSource = null;
  var markdownPageBusy = false;
  var markdownPagePreviousFocus = null;

  function markdownPageMode() {
    var physical = document.getElementById('md-page-mode-physical');
    return physical && physical.checked ? 'physical' : 'printed';
  }

  function updateMarkdownPageHelp() {
    var help = document.getElementById('md-page-mode-help');
    if (!help) return;
    help.textContent = markdownPageMode() === 'physical'
      ? '按 PDF 文件顺序计数，第 1 页通常是封面'
      : '按书中印刷页码导出；序言页可输入 iv、v 等页码标签';
  }

  function openMarkdownPageExport(sourceId) {
    if (markdownPageBusy) return;
    markdownPageSource = sourceId;
    markdownPagePreviousFocus = document.activeElement;
    var source = libraryStore.sources.find(function(s) { return s.source_file_id === sourceId; });
    var epub = source && sourceFormatLabel(source) === 'EPUB';
    var printed = document.getElementById('md-page-mode-printed');
    if (printed) printed.checked = true;
    var physical = document.getElementById('md-page-mode-physical');
    if (physical) { physical.checked = false; physical.disabled = !!epub; }
    var physicalItem = document.getElementById('md-page-mode-physical-item');
    if (physicalItem) physicalItem.classList.toggle('is-disabled', !!epub);
    updateMarkdownPageHelp();
    document.getElementById('md-page-input').value = '';
    document.getElementById('md-page-error').textContent = '';
    document.getElementById('md-page-note').textContent = epub
      ? '仅采用出版方页码。EPUB 入库文本未保留脚注链接，选页不能保证带出页外脚注'
      : '原书页码依赖已有页码映射；PDF 物理页码从 1 开始。已配对的脚注随正文导出，原文保持不变';
    document.getElementById('markdown-page-dialog').showModal();
    document.getElementById('md-page-input').focus();
  }

  function closeMarkdownPageExport() {
    if (markdownPageBusy) return;
    document.getElementById('markdown-page-dialog').close();
    markdownPageSource = null;
    if (markdownPagePreviousFocus && markdownPagePreviousFocus.isConnected) markdownPagePreviousFocus.focus();
  }

  async function submitMarkdownPageExport(event) {
    event.preventDefault();
    if (markdownPageBusy || !markdownPageSource) return;
    var pages = document.getElementById('md-page-input').value.trim();
    var errorNode = document.getElementById('md-page-error');
    if (!pages) { errorNode.textContent = '请填写要导出的页码'; return; }
    var selection = {mode: markdownPageMode(), pages: pages};
    markdownPageBusy = true;
    var controls = document.getElementById('md-page-fields');
    controls.disabled = true;
    errorNode.textContent = '';
    try {
      var directory = await chooseDesktopExportDirectory();
      if (directory === null) return;
      var data = await requestLibraryDocumentMarkdownExport(markdownPageSource, directory, selection);
      markdownPageBusy = false;
      closeMarkdownPageExport();
      showToast('已导出 ' + data.page_count + ' 页到：' + data.path);
      if (data.warnings && data.warnings.length) showToast(data.warnings.join('；'), 'warning');
    } catch (error) {
      errorNode.textContent = error && error.message ? error.message : '导出失败';
    } finally {
      markdownPageBusy = false;
      controls.disabled = false;
    }
  }

  function cancelMarkdownPageExport(event) {
    event.preventDefault();
    closeMarkdownPageExport();
  }

  async function exportLibraryDocumentMarkdown(sourceId) {
    if (!sourceId) return;
    try {
      var outputDirectory = await chooseDesktopExportDirectory();
      if (outputDirectory === null) return;
      showToast('正在导出 Markdown…');
      var data = await requestLibraryDocumentMarkdownExport(sourceId, outputDirectory);
      showToast('已导出 Markdown 到：' + data.path + '（' + formatFileSize(data.size_bytes) + '）');
    } catch (error) {
      showToast('导出 Markdown 失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
    }
  }

  async function requestLibraryDocumentMarkdownExport(sourceId, outputDirectory, pageSelection) {
    var payload = {source_id: sourceId};
    if (pageSelection) payload.page_selection = pageSelection;
    if (outputDirectory) payload.output_dir = outputDirectory;
    var response = await MEFinderApi.fetch('/api/document/export-markdown', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '导出失败');
    return data;
  }

  async function exportLibraryDocumentEpub(sourceId) {
    if (!sourceId) return;
    try {
      var outputDirectory = await chooseDesktopExportDirectory();
      if (outputDirectory === null) return;
      showToast('正在导出 EPUB…');
      var data = await requestLibraryDocumentEpubExport(sourceId, outputDirectory);
      showToast('已导出 ' + Number(data.page_count || 0).toLocaleString()
        + ' 页到：' + data.path + '（' + formatFileSize(data.size_bytes) + '）');
    } catch (error) {
      showToast('导出 EPUB 失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
    }
  }

  async function requestLibraryDocumentEpubExport(sourceId, outputDirectory) {
    var payload = {source_id: sourceId};
    if (outputDirectory) payload.output_dir = outputDirectory;
    var response = await MEFinderApi.fetch('/api/document/export-epub', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '导出失败');
    return data;
  }

  async function exportSelectedLibraryDocuments() {
    if (libraryStore.exportRunning) return;
    var items = libraryStore.sources.filter(function(item) {
      return item.source_type === 'pdf' && libraryStore.deleteSelection.has(item.source_file_id);
    });
    if (!items.length) {
      showToast('所选文献中没有可导出的 PDF', 'warning');
      return;
    }

    var outputDirectory;
    try {
      outputDirectory = await chooseDesktopExportDirectory();
    } catch (error) {
      showToast('选择导出文件夹失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
      return;
    }
    if (outputDirectory === null) return;

    libraryStore.exportRunning = true;
    var exportButton = document.getElementById('library-export-selected-btn');
    var exported = [];
    var failures = [];
    var skippedWordCount = libraryStore.deleteSelection.size - items.length;
    updateLibraryDeleteControls();
    try {
      for (var index = 0; index < items.length; index += 1) {
        exportButton.textContent = '正在导出 ' + (index + 1) + ' / ' + items.length;
        try {
          exported.push(await requestLibraryDocumentExport(
            items[index].source_file_id,
            outputDirectory
          ));
        } catch (error) {
          failures.push({
            title: items[index].title || items[index].file_name || items[index].source_file_id,
            message: error && error.message ? error.message : '未知错误'
          });
        }
      }
    } finally {
      libraryStore.exportRunning = false;
      updateLibraryDeleteControls();
    }

    var skippedText = skippedWordCount ? '；已跳过 ' + skippedWordCount + ' 份 Word / EPUB' : '';
    if (failures.length) {
      showToast('批量导出完成：成功 ' + exported.length + ' 本，失败 ' + failures.length + ' 本'
        + skippedText + '。首个失败：' + failures[0].title + '：' + failures[0].message, 'warning');
      return;
    }
    outputDirectory = outputDirectory || exported[0].path.replace(/[\\/][^\\/]+$/, '');
    showToast('已导出 ' + exported.length + ' 个文档包'
      + (settingsStore.currentDocumentExportMode === 'with_pdf' ? '（包含原 PDF）' : '') + skippedText
      + '，每本一个文档包。保存到：' + outputDirectory, 'success');
  }

  async function selectLibDoc(sourceId) {
    // 切到别的文献前拦一道未保存修改；同一文献的重选（识别/保存后刷新）不打扰。
    var switchingDoc = sourceId !== libraryStore.selectedId;
    if (switchingDoc && !await global.MEFinder.bibliography.guardLeaveDetail()) return;
    if (switchingDoc) global.MEFinder.bibliography.setEditMode(sourceId, false);  // 新文献默认查看态
    libraryStore.selectedId = sourceId;
    document.querySelectorAll('#library-list .library-entry').forEach(function(row) {
      row.classList.toggle('selected', row.dataset.id === sourceId);
    });
    if (!libraryStore.sources.some(function(s) { return s.source_file_id === sourceId; })) return;
    try {
      await ensureLibraryDetail(sourceId);
    } catch (error) {
      showToast(error && error.message ? error.message : '文献详情读取失败', 'danger');
      return;
    }
    // 详情是异步补齐的，期间用户可能已经关掉抽屉或换了一份文献。
    if (libraryStore.selectedId !== sourceId) return;
    var src = libraryStore.sources.find(function(s) { return s.source_file_id === sourceId; });
    if (!src) return;
    var vol = volumeForSource(sourceId);
    var works = vol ? libraryStore.works.filter(function(w) { return w.volume_id === vol.volume_id; }) : [];
    var title = vol ? vol.display_title : (src.file_name || sourceId);
    var corpusTitle = vol ? (vol.corpus_title || '') : '';

    var bibliography = null;
    // EPUB（source_type=word + 格式 EPUB）在导入时已从 OPF 填好书目元数据，
    // 与 PDF 共用同一套书目信息面板；Word 暂不参与。
    if (src.source_type === 'pdf' || libraryFileFacet(src) === 'epub') {
      // 选中即以当前元数据初始化字段缓存；切类型只在缓存里保留隐藏字段，不会丢。
      global.MEFinder.bibliography.cacheFields(sourceId, sourceBibliographicMetadata(src));
      bibliography = global.MEFinder.bibliography.renderSectionNode(src);
    }

    var content = document.getElementById('library-drawer-content');
    content.replaceChildren();
    var nav = drawerNavNode(sourceId);
    if (nav) content.appendChild(nav);
    var titleNode = libraryNode('div', 'drawer-title', title);
    titleNode.tabIndex = -1;
    content.appendChild(titleNode);
    if (corpusTitle) content.appendChild(libraryNode('div', 'drawer-subtitle', corpusTitle));
    var pills = libraryNode('div', 'detail-pills');
    pills.style.marginTop = '12px';
    pills.appendChild(libraryNode('span', 'detail-pill', sourceFormatLabel(src)));
    if (vol && vol.primary_structure) pills.appendChild(libraryNode('span', 'detail-pill', structureLabel(vol.primary_structure)));
    var statusPill = drawerStatusPillNode(src);
    if (statusPill) pills.appendChild(statusPill);
    content.appendChild(pills);
    if (bibliography) content.appendChild(bibliography);

    var extra = document.getElementById('library-drawer-extra');
    if (extra) extra.replaceChildren(drawerWorksNode(works), drawerFileInfoNode(src, vol), drawerMainActionsNode(src));

    document.getElementById('library-drawer').classList.add('open');
    var body = document.querySelector('#page-library .library-body');
    if (body) body.classList.add('detail-open');
    renderDrawerCalibration(src);
    // 新开详情时把焦点移到标题，便于读屏播报；同一文献刷新不夺焦点。
    if (switchingDoc) {
      var titleEl = content.querySelector('.drawer-title');
      if (titleEl) titleEl.focus();
    }
  }

  function renderDrawerCalibrationSummary(src) {
    var summary = document.getElementById('cal-collapse-summary');
    if (!summary) return;
    var status = calTransientStatus[src.source_file_id] || src.status;
    var group = calibrationStatusGroup(status);
    summary.replaceChildren(statusChipNode(group, status, false),
      libraryNode('span', 'cal-collapse-mapping', src.mapping_summary || '尚未建立引用页码映射'));
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
    if (open && libraryStore.selectedId && calSelectedSourceId !== libraryStore.selectedId) {
      calSelectedSourceId = libraryStore.selectedId;
      await loadCalibrationDoc(libraryStore.selectedId);
    }
  }

  function updateLibraryEntry(sourceId) {
    renderLibraryStats();
    renderLibraryList();
    if (libraryStore.selectedId === sourceId) {
      var src = libraryStore.sources.find(function(s) { return s.source_file_id === sourceId; });
      if (src && src.source_type === 'pdf') renderDrawerCalibrationSummary(src);
    }
  }

  // node 白盒测试只通过显式导出访问模块内部，不再替换运行时全局符号。
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      requestLibraryDocumentMarkdownExport: requestLibraryDocumentMarkdownExport,
      requestLibraryDocumentEpubExport: requestLibraryDocumentEpubExport
    };
  }

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.library = {
    pageExport: {open: openMarkdownPageExport, close: closeMarkdownPageExport,
      submit: submitMarkdownPageExport, cancel: cancelMarkdownPageExport,
      updateHelp: updateMarkdownPageHelp},
    applyCatalog: applyLibraryCatalog,
    load: loadLibrary,
    loadDocumentGroups: loadDocumentGroups,
    ensureDetail: ensureLibraryDetail,
    syncDefaultLanguageControl: syncLibDefaultLanguageControl,
    syncViewButtons: syncLibraryViewButtons,
    syncDeleteSelectionUI: syncLibraryDeleteSelectionUI,
    setupKeyboardNav: setupLibraryKeyboardNav,
    renderList: renderLibraryList,
    updateEntry: updateLibraryEntry
  };

  MEFinderActions.register('openLibraryEntry', function(event, target) {
    handleLibraryEntryClick(event, target.dataset.id);
  });
  MEFinderActions.register('toggleLibraryEntrySelection', function(event, target) {
    event.stopImmediatePropagation();
    toggleLibraryDeleteSelection(target.dataset.sourceId, target.checked);
  });
  MEFinderActions.register('openLibraryWork', function(event, target) {
    event.stopImmediatePropagation();
    MEFinder.works.open(target.dataset.workId);
  });
  MEFinderActions.register('setLibraryFacet', function(event, target) {
    return setLibFacet(event, target.dataset.kind, target.dataset.value);
  });
  MEFinderActions.register('removeLibraryFacet', function(event, target) {
    return removeLibFacet(event, target.dataset.kind);
  });
  MEFinderActions.register('applyLibStatusFilter', function(event, target) {
    return applyLibStatusFilter(target.dataset.status);
  });
  MEFinderActions.register('navigateToImport', function() { navigateTo('import'); });
  MEFinderActions.register('clearLibraryFilters', function() { clearLibraryFilters(); });
  MEFinderActions.register('selectLibDoc', function(event, target) { selectLibDoc(target.dataset.sourceId); });
  MEFinderActions.registerInline('toggleDrawerFileInfo', function(event) { toggleDrawerSection(event, 'drawer-file-info'); });
  MEFinderActions.register('submitDrawerMineruReparse', function(event, target) {
    bibCloseMenus(); submitMineruReparse(target.dataset.sourceId);
  });
  MEFinderActions.register('acceptDrawerAutoMapping', function(event, target) {
    bibCloseMenus(); acceptAutoMapping(target.dataset.sourceId);
  });
  MEFinderActions.register('showDrawerAutoMappingExceptions', function(event, target) {
    bibCloseMenus(); showAutoMappingExceptions(target.dataset.sourceId);
  });
  MEFinderActions.register('exportLibraryDocument', function(event, target) {
    bibCloseMenus(); exportLibraryDocument(target.dataset.sourceId);
  });
  MEFinderActions.register('exportLibraryDocumentMarkdown', function(event, target) {
    bibCloseMenus(); exportLibraryDocumentMarkdown(target.dataset.sourceId);
  });
  MEFinderActions.register('openMarkdownPageExport', function(event, target) {
    bibCloseMenus(); openMarkdownPageExport(target.dataset.sourceId);
  });
  MEFinderActions.register('exportLibraryDocumentEpub', function(event, target) {
    bibCloseMenus(); exportLibraryDocumentEpub(target.dataset.sourceId);
  });
  MEFinderActions.register('openRemoveDocumentModal', function(event, target) {
    bibCloseMenus(); openRemoveDocumentModal(target.dataset.sourceId);
  });
  MEFinderActions.register('openLibrarySource', function(event, target) {
    openSource(target.dataset.sourceId, null);
  });
  MEFinderActions.register('readLibrarySource', function(event, target) {
    MEFinder.works.readFromLibrary(target.dataset.sourceId);
  });
  MEFinderActions.registerInline('toggleDrawerMoreMenu', function(event) {
    bibToggleMenu(event, 'drawer-more-menu');
  });

  // 浏览器公共面：动态内联处理器只能通过这些命令入口访问本模块。
  global.openVersionSelect = openVersionSelect;
  global.toggleLibrarySortDirection = toggleLibrarySortDirection;
  global.setLibDefaultLanguage = setLibDefaultLanguage;
  global.clearLibraryFilters = clearLibraryFilters;
  global.filterLibrary = filterLibrary;
  global.setLibraryView = setLibraryView;
  global.setLibrarySortOption = setLibrarySortOption;
  global.clearLibrarySelection = clearLibrarySelection;
  global.toggleSelectVisibleLibraryDocuments = toggleSelectVisibleLibraryDocuments;
  global.exportSelectedLibraryDocuments = exportSelectedLibraryDocuments;
  global.selectLibDoc = selectLibDoc;
  global.toggleDrawerCalibration = toggleDrawerCalibration;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
