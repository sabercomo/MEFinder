(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config, alignmentJobs = r.alignmentJobs;

  function configure(options) {
    options = options || {};
    if (options.endpoint) config.endpoint = String(options.endpoint);
    if (options.outlineEndpoint) config.outlineEndpoint = String(options.outlineEndpoint);
    if (options.citationEndpoint) {
      config.citationEndpoint = String(options.citationEndpoint);
    }
    if (options.alignmentTargetsEndpoint) {
      config.alignmentTargetsEndpoint = String(options.alignmentTargetsEndpoint);
    }
    if (options.alignmentLocateEndpoint) {
      config.alignmentLocateEndpoint = String(options.alignmentLocateEndpoint);
    }
    if (typeof options.openExternal === 'function') config.openExternal = options.openExternal;
    if (typeof options.onClose === 'function') config.onClose = options.onClose;
    [
      'onOpenChange', 'onManageWork', 'onInstallComponent', 'onFindInWork',
      'openInNewWindow', 'canOpenInNewWindow'
    ].forEach(function (name) {
      if (typeof options[name] === 'function') config[name] = options[name];
    });
    if (typeof options.fetch === 'function') {
      config.fetch = options.fetch;
      alignmentJobs.configure({fetch: options.fetch});
    }
    if (typeof options.notify === 'function') config.notify = options.notify;
    if (options.notify === null) config.notify = null;
    config.batchSize = r.clampInteger(options.batchSize, config.batchSize, 5, 100);
    config.radiusBatches = r.clampInteger(
      options.radiusBatches,
      config.radiusBatches,
      0,
      4
    );
    while ((config.radiusBatches * 2 + 1) * config.batchSize > 100) {
      config.radiusBatches -= 1;
    }
    config.estimatedItemHeight = r.clampInteger(
      options.estimatedItemHeight,
      config.estimatedItemHeight,
      120,
      1200
    );
  }

  async function openReader(options) {
    options = options || r.parseReaderDeepLink(global.location) || state.lastSession || {};
    var sourceId = String(options.sourceId || options.source_id || '');
    if (!sourceId) throw new Error('缺少文献标识，无法打开结构化文本');
    if (!options.noExternal && config.openExternal && await config.openExternal(options)) return true;
    r.ensureDom();
    r.closeMenus();
    r.closeReviewPopover();
    if (state.comparison.open) r.closeComparison();
    var wasOpen = state.open;

    if (options.config) configure(options.config);
    if (!state.open) {
      state.restoreFocus = document.activeElement;
      state.originalUrl = options.restoringSession
        ? (state.originalUrl || '/')
        : r.ordinaryUrlBeforeReader();
    }
    state.open = true;
    state.sourceId = sourceId;
    state.outline = {items: null, loading: false, error: ''};
    state.outlineJumpSerial += 1;
    state.outlineNavigating = false;
    state.source = null;
    if (options.returnLabel || !wasOpen) state.returnLabel = String(options.returnLabel || '');
    state.pendingCompareWith = String(options.compareWith || '');
    state.work = {groupId: '', title: '', baseId: '', members: [], pairs: {}, languages: {}};
    state.links = null;
    state.linkedRanges.clear();
    state.selectedLinkKey = '';
    state.elements.jumpForm.hidden = true;
    state.title = String(options.title || options.documentTitle || options.document_title || '');
    state.targetAnchorId = String(
      options.anchorId ||
      options.anchor_id ||
      options.pdfPageId ||
      options.pdf_page_id ||
      ((options.pageMatchSpans || options.page_match_spans || [])[0] || {}).pdf_page_id ||
      ''
    );
    state.onCurrentChange = typeof options.onCurrentChange === 'function'
      ? options.onCurrentChange
      : null;
    state.items.clear();
    state.total = 0;
    state.lastPosition = null;
    state.windowStart = 0;
    state.windowEnd = 0;
    state.hasPrevious = false;
    state.hasMore = false;
    state.previousStart = null;
    state.nextStart = null;
    state.currentAnchorId = '';
    state.lastHistoryAnchor = '';
    state.lastHistoryCompare = '';
    if (state.deepLinkTimer !== null) global.clearTimeout(state.deepLinkTimer);
    if (state.scrollBoundaryTimer !== null) {
      global.clearTimeout(state.scrollBoundaryTimer);
    }
    state.deepLinkTimer = null;
    state.pendingDeepLink = null;
    state.scrollBoundaryTimer = null;
    state.citationRequestSerial += 1;
    state.alignmentRequestSerial += 1;
    state.citationRange = null;
    state.selectionDragging = false;
    state.citationMenuOpen = false;
    state.citationLoading = false;
    state.alignmentTargets = [];
    state.alignmentSourceLanguage = '';
    state.alignmentLoading = false;
    state.currentIndex = r.resolveTargetIndex(options);
    r.prepareHighlights(options);

    state.elements.title.textContent = r.cleanReaderTitle(state.title) || '文献阅读';
    state.elements.eyebrow.textContent = '正在阅读';
    state.elements.subtitle.textContent = '';
    state.elements.subtitle.hidden = true;
    state.elements.current.textContent = '正在载入…';
    state.elements.current.disabled = true;
    state.elements.root.hidden = false;
    state.elements.root.setAttribute('aria-hidden', 'false');
    document.body.classList.add('mef-reader-open');
    if (!wasOpen && typeof config.onOpenChange === 'function') config.onOpenChange(true);
    r.setAlert('', 'info');
    r.renderAlignmentActions();
    r.updateCitationControls();

    if (!state.preciseHighlight && (
      options.preciseHighlightAvailable === false ||
      options.precise_highlight_available === false ||
      options.legacyIndex === true ||
      options.legacy_index === true
    )) {
      var legacyMessage = '此文献使用旧索引，已跳转到相应位置，但无法精确高亮。重新导入后可启用精确定位';
      r.setAlert(legacyMessage, 'warning');
      r.notify(legacyMessage);
    }

    var loaded = await r.loadWindow(state.currentIndex, state.targetAnchorId);
    if (loaded && state.open && state.sourceId === sourceId) {
      r.loadAlignmentTargets(sourceId);
      r.loadWorkContext(sourceId);
    }
    if (loaded && !wasOpen) {
      var focusTarget = state.elements.back.hidden ? state.elements.viewport : state.elements.back;
      focusTarget.focus();
    }
    return loaded;
  }

  async function goTo(target) {
    if (!state.open) return false;
    state.outlineJumpSerial += 1;
    state.outlineNavigating = false;
    var options = typeof target === 'object' && target !== null
      ? target
      : (typeof target === 'number' ? {targetIndex: target} : {anchorId: target});
    var anchorId = String(options.anchorId || options.anchor_id || '');
    var index = r.resolveTargetIndex(options);
    if (anchorId) {
      var local = r.findAnchorNode(anchorId);
      if (local) {
        r.positionSourceTarget(local);
        return true;
      }
      var inferred = r.inferIndexFromAnchor(anchorId);
      if (inferred !== null) index = inferred;
    }
    state.targetAnchorId = anchorId;
    state.currentIndex = index;
    return r.loadWindow(index, anchorId);
  }

  function openForSearchResult(item, overrides) {
    item = item || {};
    overrides = overrides || {};
    var spans = item.page_match_spans || [];
    var firstSpan = spans.length ? spans[0] : {};
    var sourceType = String(item.source_type || '').toLowerCase();
    var options = {
      sourceId: item.source_file_id,
      title: item.document_title || item.work_title || item.original_file_name || '',
      targetIndex: sourceType === 'word'
        ? item.paragraph_index
        : item.pdf_page_start_index,
      anchorId: firstSpan.pdf_page_id ||
        item.pdf_page_id ||
        (sourceType === 'word' ? item.paragraph_id : '') ||
        '',
      pdfPageIndex: item.pdf_page_start_index,
      paragraphIndex: item.paragraph_index,
      paragraphId: item.paragraph_id,
      pageMatchSpans: spans,
      matchStart: item.match_start,
      matchEnd: item.match_end,
      matchOffsetUnit: item.match_offset_unit,
      matchQuote: item.match_quote
    };
    if (sourceType === 'pdf') {
      options.preciseHighlightAvailable = item.precise_highlight_available;
      options.legacyIndex = item.precise_highlight_available === false;
    }
    Object.keys(overrides).forEach(function (key) {
      options[key] = overrides[key];
    });
    return openReader(options);
  }

  function closeReader() {
    if (!state.elements || !state.open) return;
    r.flushPendingReaderDeepLink();
    r.closeMenus();
    r.closeReviewPopover();
    state.elements.jumpForm.hidden = true;
    // 关闭前立即写一次当前位置（含右栏），再收起对照；收起对照排队的保存随之取消。
    var savedPosition = r.saveReadingPositionNow();
    r.closeComparison();
    if (state.positionTimer !== null) {
      global.clearTimeout(state.positionTimer);
      state.positionTimer = null;
    }
    state.open = false;
    state.outlineJumpSerial += 1;
    state.outlineNavigating = false;
    state.requestSerial += 1;
    state.citationRequestSerial += 1;
    state.alignmentRequestSerial += 1;
    if (state.abortController) state.abortController.abort();
    if (state.deepLinkTimer !== null) global.clearTimeout(state.deepLinkTimer);
    if (state.scrollBoundaryTimer !== null) {
      global.clearTimeout(state.scrollBoundaryTimer);
    }
    state.deepLinkTimer = null;
    state.pendingDeepLink = null;
    state.scrollBoundaryTimer = null;
    r.disconnectObservers();
    state.items.clear();
    state.highlights.clear();
    state.resolvedHighlights.clear();
    state.citationRange = null;
    state.alignmentTargets = [];
    state.alignmentSourceLanguage = '';
    state.alignmentLoading = false;
    state.selectionDragging = false;
    state.citationMenuOpen = false;
    state.elements.content.replaceChildren();
    state.elements.root.hidden = true;
    state.elements.root.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('mef-reader-open');
    state.work = {groupId: '', title: '', baseId: '', members: [], pairs: {}, languages: {}};
    state.pendingCompareWith = '';
    // 把刚写出的位置交给宿主：它不必重新查询，也就没有「写入是否已落库」的赌博。
    if (typeof config.onOpenChange === 'function') config.onOpenChange(false, savedPosition);
    if (
      global.history &&
      typeof global.history.replaceState === 'function' &&
      global.location &&
      (
        global.location.pathname === '/reader' ||
        global.location.pathname === '/reader/'
      )
    ) {
      global.history.replaceState(
        {meFinderReader: false},
        '',
        state.originalUrl || '/'
      );
    }
    if (state.restoreFocus && typeof state.restoreFocus.focus === 'function') {
      state.restoreFocus.focus();
    }
    state.restoreFocus = null;
    if (config.onClose) config.onClose();
  }

  function destroy() {
    closeReader();
    if (state.elements && state.elements.root.isConnected) state.elements.root.remove();
    state.elements = null;
  }

  function getState() {
    return {
      open: state.open,
      sourceId: state.sourceId,
      total: state.total,
      lastPosition: state.lastPosition,
      windowStart: state.windowStart,
      windowEnd: state.windowEnd,
      hasPrevious: state.hasPrevious,
      hasMore: state.hasMore,
      previousStart: state.previousStart,
      nextStart: state.nextStart,
      mountedItemCount: state.items.size,
      currentIndex: state.currentIndex,
      currentAnchorId: state.currentAnchorId,
      citationRange: state.citationRange ? {
        startIndex: state.citationRange.startIndex,
        endIndex: state.citationRange.endIndex,
        startOffset: state.citationRange.startOffset,
        endOffset: state.citationRange.endOffset
      } : null,
      alignmentTargetCount: state.alignmentTargets.length,
      comparisonOpen: state.comparison.open,
      comparisonTargetSourceId: state.comparison.targetSourceId,
      comparisonAutoFollow: state.comparison.autoFollow,
      comparisonPending: !!(state.elements && state.comparison.open && !state.elements.pending.hidden),
      workGroupId: state.work.groupId,
      linkCount: state.links ? state.links.items.length : 0,
      lastDeepLink: state.lastDeepLink
    };
  }

  function restoreInitialDeepLink() {
    if (document.documentElement.dataset.readerWindow === 'true') return;
    if (!state.open && r.parseReaderDeepLink(global.location)) {
      r.restoreReaderLocation();
    }
  }

  /* ── 对齐任务：监听在 MEFinderAlignmentJobs，阅读器只处理结局 ─────── */
  // 同一窗口只有一份任务监听（15-alignment-jobs.js），阅读器和作品页都只认领与订阅；
  // 结局提示只由发起方给出。阅读器关闭不停止本窗口的任务监听。
  alignmentJobs.subscribe(function (event) {
    Promise.resolve(r.applyAlignmentJobEnd(event)).catch(function () { /* 刷新失败不打断阅读。*/ });
  });


  document.addEventListener('keydown', function (event) {
    if (!state.open || event.key !== 'Escape' || event.defaultPrevented) return;
    if (state.openMenu) { r.closeMenus(state.openMenu); return; }
    if (state.review) { r.closeReviewPopover(true); return; }
    if (state.elements && !state.elements.jumpForm.hidden) { r.closeJumpForm(); return; }
    // 应用自己的弹窗或抽屉在上层时，Esc 先交给它们。
    if (document.querySelector('.tw-dialog-scrim, .tw-sheet-scrim, .app-dialog-backdrop.open')) return;
    closeReader();
  });
  document.addEventListener('mouseup', function () {
    if (state.open && state.selectionDragging) r.scheduleSelectionCapture();
  });
  global.addEventListener('popstate', function () {
    var deepLink = r.parseReaderDeepLink(global.location);
    if (deepLink && !state.open) {
      r.restoreReaderLocation();
    } else if (!deepLink && state.open) {
      closeReader();
    }
  });

  global.MEFinderReader = Object.freeze({
    // 兼容入口：转发到 MEFinderAlignmentJobs，新代码直接用服务。
    alignmentJobs: Object.freeze({
      watch: alignmentJobs.watch,
      subscribe: alignmentJobs.subscribe,
      running: alignmentJobs.running
    }),
    open: openReader,
    openForSearchResult: openForSearchResult,
    close: closeReader,
    goTo: goTo,
    restore: r.restoreReaderLocation,
    copyCitation: r.copyCachedCitation,
    configure: configure,
    destroy: destroy,
    isOpen: function () { return state.open; },
    getState: getState,
    codePointToUtf16Index: r.codePointToUtf16Index
  });


  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restoreInitialDeepLink, {once: true});
  } else {
    global.setTimeout(restoreInitialDeepLink, 0);
  }
  delete global.__MEFinderReaderInternal;

  r.openReader = openReader;
  r.goTo = goTo;
  r.closeReader = closeReader;
}(window));
