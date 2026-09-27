(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config;

  function currentReadingSession() {
    return {
      sourceId: state.sourceId,
      title: state.title,
      targetIndex: state.currentIndex,
      anchorId: state.currentAnchorId,
      groupId: state.work.groupId,
      compareWith: state.comparison.open ? state.comparison.targetSourceId : ''
    };
  }

  function openInNewWindow() {
    r.closeMenus();
    if (typeof config.openInNewWindow !== 'function') return;
    Promise.resolve(config.openInNewWindow(currentReadingSession())).then(function (opened) {
      if (opened) r.closeReader();
    }).catch(function (error) {
      r.setAlert(error && error.message ? error.message : '无法打开新窗口', 'warning');
    });
  }

  function returnToMainWindow() {
    r.closeMenus();
    if (!global.pywebview || !global.pywebview.state) return;
    global.pywebview.state.readerReturn = currentReadingSession();
  }

  function saveReadingPositionNow() {
    if (state.positionTimer !== null) {
      global.clearTimeout(state.positionTimer);
      state.positionTimer = null;
    }
    if (!state.open || !state.work.groupId || !state.items.has(state.currentIndex)) {
      return null;
    }
    var session = currentReadingSession();
    var position = {
      left_source_file_id: session.sourceId,
      right_source_file_id: session.compareWith || null,
      item_index: session.targetIndex,
      char_offset: 0
    };
    r.postJSON(config.readingPositionEndpoint, Object.assign(
      {document_group_id: session.groupId}, position
    )).catch(function () { /* 位置只是便利信息，保存失败不打扰阅读。 */ });
    return {document_group_id: session.groupId, position: position};
  }

  function scheduleReadingPositionSave() {
    if (!state.open || !state.work.groupId || !state.items.has(state.currentIndex)) return;
    if (state.positionTimer !== null) global.clearTimeout(state.positionTimer);
    state.positionTimer = global.setTimeout(saveReadingPositionNow, 1500);
  }

  /* ── 逐段对应：选中段落高亮对应段，低置信段落标「!」 ───────────── */
  function truncateCodePoints(value, maximum) {
    return Array.from(String(value || '')).slice(0, maximum).join('');
  }

  function parseDeepLinkOffset(value, quote) {
    var match = /^(\d+)(?:-(\d+))?$/.exec(String(value || ''));
    if (!match) return null;
    var start = r.clampInteger(match[1], 0, 0, Number.MAX_SAFE_INTEGER);
    var end = match[2] == null
      ? start + r.codePointLength(quote)
      : r.clampInteger(match[2], start, start, Number.MAX_SAFE_INTEGER);
    return end > start ? {start: start, end: end} : null;
  }

  function parseReaderDeepLink(locationValue) {
    var locationObject = locationValue || global.location;
    if (!locationObject) return null;
    var pathname = String(locationObject.pathname || '');
    if (pathname !== '/reader' && pathname !== '/reader/' && pathname !== '/reader-window') return null;
    var search = String(locationObject.search || '');
    if (search.length > 1024) return null;
    var params = new URLSearchParams(search);
    var unknownParameter = false;
    params.forEach(function (_value, key) {
      if (!['source', 'page', 'off', 'h', 'q', 'c'].includes(key)) {
        unknownParameter = true;
      }
    });
    if (
      unknownParameter ||
      params.getAll('source').length !== 1 ||
      params.getAll('page').length !== 1 ||
      params.getAll('off').length > 1 ||
      params.getAll('h').length > 1 ||
      params.getAll('q').length > 1 ||
      params.getAll('c').length > 1
    ) {
      return null;
    }
    var sourceId = String(params.get('source') || '');
    var anchorId = String(params.get('page') || '');
    if (
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(sourceId) ||
      anchorId.length > 256 ||
      !/^[A-Za-z0-9._:-]+$/.test(anchorId)
    ) {
      return null;
    }
    var targetIndex = r.inferIndexFromAnchor(anchorId);
    if (targetIndex === null) return null;
    // 右栏属于会话的一部分：刷新或重开独立窗口时一起恢复。
    var compareWith = String(params.get('c') || '');
    if (compareWith && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(compareWith)) return null;
    if (compareWith === sourceId) return null;
    var hashValue = String(params.get('h') || '');
    if (hashValue && !/^[0-9a-f]{16}$/i.test(hashValue)) return null;
    var pageTextHash = hashValue;
    var rawQuote = String(params.get('q') || '');
    if (r.codePointLength(rawQuote) > 50) return null;
    var matchQuote = rawQuote;
    var offsetValue = String(params.get('off') || '');
    if (offsetValue.length > 64) return null;
    var offset = parseDeepLinkOffset(offsetValue, matchQuote);
    if (offsetValue && !offset) return null;
    if (!offsetValue && (hashValue || matchQuote)) return null;
    var spans = offset ? [{
      anchor_id: anchorId,
      page_char_start: offset.start,
      page_char_end: offset.end,
      page_text_hash: pageTextHash,
      match_quote: matchQuote
    }] : [];
    return {
      sourceId: sourceId,
      targetIndex: targetIndex,
      anchorId: anchorId,
      compareWith: compareWith,
      paragraphId: /-P\d+$/.test(anchorId) ? anchorId : '',
      pageMatchSpans: spans,
      matchOffsetUnit: 'unicode_codepoint',
      matchQuote: matchQuote,
      // A page-only link did not request highlighting.  Leave capability
      // unspecified so it is not mistaken for a legacy index that failed to
      // provide precise match anchors.
      preciseHighlightAvailable: spans.length ? true : undefined,
      fromDeepLink: true
    };
  }

  function deepLinkRange(anchorId) {
    var resolved = state.resolvedHighlights.get(anchorId) || [];
    if (resolved.length) return {range: resolved[0], resolved: true};
    var original = state.highlights.get(anchorId) || [];
    return original.length ? {range: original[0], resolved: false} : null;
  }

  function updateReaderDeepLink(item, index, anchorId) {
    var compareWith = state.comparison.open
      ? String(state.comparison.targetSourceId || '')
      : '';
    if (
      !state.open ||
      !global.history ||
      typeof global.history.replaceState !== 'function' ||
      (state.lastHistoryAnchor === anchorId && state.lastHistoryCompare === compareWith)
    ) {
      return;
    }
    var params = new URLSearchParams();
    params.set('source', state.sourceId);
    params.set('page', anchorId);
    if (compareWith) params.set('c', compareWith);
    var linkRange = deepLinkRange(anchorId);
    var quote = '';
    var pageTextHash = '';
    var spans = [];
    if (linkRange) {
      var range = linkRange.range;
      var start = r.clampInteger(range.start, 0, 0, Number.MAX_SAFE_INTEGER);
      var end = r.clampInteger(range.end, start, start, Number.MAX_SAFE_INTEGER);
      if (end > start) {
        params.set('off', start + '-' + end);
        quote = truncateCodePoints(range.matchQuote || state.matchQuote, 50);
        pageTextHash = linkRange.resolved
          ? String(item.page_text_hash || '')
          : String(range.pageTextHash || '');
        spans.push({
          anchor_id: anchorId,
          page_char_start: start,
          page_char_end: end,
          page_text_hash: pageTextHash,
          match_quote: quote
        });
      }
    }
    if (/^[0-9a-f]{16}$/i.test(pageTextHash)) params.set('h', pageTextHash);
    if (quote) params.set('q', quote);
    var readerPath = document.documentElement.dataset.readerWindow === 'true' ? '/reader-window' : '/reader';
    var url = readerPath + '?' + params.toString();
    if (url.length > 1024) return;
    global.history.replaceState(
      {meFinderReader: true, sourceId: state.sourceId, anchorId: anchorId},
      '',
      url
    );
    state.lastHistoryAnchor = anchorId;
    state.lastHistoryCompare = compareWith;
    state.lastDeepLink = url;
    state.lastSession = Object.assign(currentReadingSession(), {
      targetIndex: index,
      anchorId: anchorId,
      pageMatchSpans: spans,
      matchOffsetUnit: 'unicode_codepoint',
      matchQuote: quote,
      preciseHighlightAvailable: spans.length > 0,
      fromDeepLink: true
    });
  }

  function scheduleReaderDeepLink(item, index, anchorId) {
    state.pendingDeepLink = {
      item: item,
      index: index,
      anchorId: anchorId
    };
    if (state.deepLinkTimer !== null) {
      global.clearTimeout(state.deepLinkTimer);
    }
    state.deepLinkTimer = global.setTimeout(function () {
      state.deepLinkTimer = null;
      var pending = state.pendingDeepLink;
      state.pendingDeepLink = null;
      if (!pending || !state.open || state.currentAnchorId !== pending.anchorId) {
        return;
      }
      updateReaderDeepLink(pending.item, pending.index, pending.anchorId);
    }, 80);
  }

  function flushPendingReaderDeepLink() {
    if (state.deepLinkTimer !== null) {
      global.clearTimeout(state.deepLinkTimer);
      state.deepLinkTimer = null;
    }
    var pending = state.pendingDeepLink;
    state.pendingDeepLink = null;
    if (pending && state.open && state.currentAnchorId === pending.anchorId) {
      updateReaderDeepLink(pending.item, pending.index, pending.anchorId);
    }
  }

  function ordinaryUrlBeforeReader() {
    if (!global.location) return '/';
    var pathname = String(global.location.pathname || '/');
    if (pathname === '/reader' || pathname === '/reader/') return '/';
    return pathname + String(global.location.search || '') +
      String(global.location.hash || '');
  }

  function restoreReaderLocation() {
    var options = parseReaderDeepLink(global.location) || state.lastSession;
    if (!options) return Promise.resolve(false);
    return r.openReader(Object.assign({}, options, {restoringSession: true}));
  }


  r.openInNewWindow = openInNewWindow;
  r.returnToMainWindow = returnToMainWindow;
  r.saveReadingPositionNow = saveReadingPositionNow;
  r.scheduleReadingPositionSave = scheduleReadingPositionSave;
  r.parseReaderDeepLink = parseReaderDeepLink;
  r.scheduleReaderDeepLink = scheduleReaderDeepLink;
  r.flushPendingReaderDeepLink = flushPendingReaderDeepLink;
  r.ordinaryUrlBeforeReader = ordinaryUrlBeforeReader;
  r.restoreReaderLocation = restoreReaderLocation;
}(window));
