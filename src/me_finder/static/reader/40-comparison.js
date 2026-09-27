(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config;

  function pickLeftVersion(targetId) {
    r.closeMenus();
    if (!targetId || targetId === state.sourceId) return;
    if (state.comparison.open && targetId === state.comparison.targetSourceId) {
      swapComparison();
      return;
    }
    var keepCompare = state.comparison.open ? state.comparison.targetSourceId : '';
    var from = state.sourceId;
    var selection = sourceCenterRange();
    var reopen = function (index) {
      r.openReader({
        sourceId: targetId,
        targetIndex: index,
        compareWith: keepCompare,
        returnLabel: state.returnLabel,
        noExternal: true,
        keepHostState: true
      });
    };
    if (!selection || !r.pairReadable(r.pairInfo(from, targetId))) {
      reopen(0);
      return;
    }
    r.postJSON(config.alignmentLocateEndpoint, {
      source_file_id: from,
      target_source_file_id: targetId,
      start_page_index: selection.startIndex,
      end_page_index: selection.endIndex,
      start_offset: selection.startOffset,
      end_offset: selection.endOffset
    }).then(function (payload) {
      reopen(Number(payload.target_index) || 0);
    }).catch(function () { reopen(0); });
  }

  function swapComparison() {
    if (!state.comparison.open || !state.comparison.targetSourceId) return;
    var nextSource = state.comparison.targetSourceId;
    var nextTarget = state.sourceId;
    var index = state.comparison.items.size ? state.comparison.currentIndex : 0;
    r.openReader({
      sourceId: nextSource,
      targetIndex: index,
      compareWith: nextTarget,
      returnLabel: state.returnLabel,
      noExternal: true,
      keepHostState: true
    });
  }

  function openComparisonWith(targetId) {
    if (!targetId || targetId === state.sourceId) return;
    var info = r.pairInfo(state.sourceId, targetId);
    r.rememberComparisonTarget(state.sourceId, targetId);
    if (r.pairReadable(info)) {
      r.showPendingPane(false);
      var sourceId = state.sourceId;
      var request = locateInAlignedVersion(targetId, sourceCenterRange(), true);
      var serial = state.comparison.locateSerial;
      request.then(function (located) {
        if (serial !== state.comparison.locateSerial || state.sourceId !== sourceId || !state.open) return;
        if (!located && state.comparison.targetSourceId !== targetId) {
          showComparison({targetSourceId: targetId, targetIndex: 0, pageMatchSpans: []},
            r.alignmentTargetName(targetId));
          r.setAlert('当前位置没有可用的对应段落，右栏从开头显示；滚动左栏后会跟随定位', 'info');
        }
      });
      return;
    }
    r.openPendingComparison(targetId, info);
  }

  function markComparisonOpen(targetId) {
    var comparison = state.comparison;
    if (comparison.targetSourceId !== targetId) {
      comparison.items.clear();
      comparison.highlights.clear();
      comparison.indexHighlights.clear();
      state.links = null;
      r.clearLinkedSelection();
    }
    comparison.open = true;
    comparison.targetSourceId = targetId;
    comparison.targetDisplayName = r.alignmentTargetName(targetId);
    state.elements.panel.classList.add('is-comparing');
    state.elements.readerBody.classList.add('is-comparing');
    state.elements.comparisonPane.hidden = false;
  }

  function nearestTextOffset(text, requestedOffset) {
    var characters = Array.from(String(text || ''));
    if (!characters.length) return null;
    var start = r.clampInteger(requestedOffset, 0, 0, characters.length - 1);
    var distance;
    for (distance = 0; distance < characters.length; distance += 1) {
      var after = start + distance;
      if (after < characters.length && /\S/.test(characters[after])) return after;
      var before = start - distance;
      if (before >= 0 && /\S/.test(characters[before])) return before;
    }
    return null;
  }

  function sourceCenterRange() {
    if (!state.elements || !state.open) return null;
    var index = state.currentIndex;
    var item = state.items.get(index);
    var body = state.elements.content.querySelector(
      '[data-reader-index="' + index + '"] .mef-reader-item-text'
    );
    var utf16Offset = null;
    var viewportRect = state.elements.viewport.getClientRects()[0];
    var pointX = viewportRect.left + viewportRect.width / 2;
    var pointY = viewportRect.top + viewportRect.height / 2;
    var caretNode = null;
    var caretOffset = null;
    if (typeof document.caretPositionFromPoint === 'function') {
      var position = document.caretPositionFromPoint(pointX, pointY);
      if (position) {
        caretNode = position.offsetNode;
        caretOffset = position.offset;
      }
    } else if (typeof document.caretRangeFromPoint === 'function') {
      var caretRange = document.caretRangeFromPoint(pointX, pointY);
      if (caretRange) {
        caretNode = caretRange.startContainer;
        caretOffset = caretRange.startOffset;
      }
    }
    var caretElement = r.elementForRangeNode(caretNode);
    var caretBody = caretElement && caretElement.closest
      ? caretElement.closest('.mef-reader-item-text')
      : null;
    if (caretBody && state.elements.content.contains(caretBody)) {
      var caretArticle = caretBody.closest('.mef-reader-item');
      var caretIndex = caretArticle ? Number(caretArticle.dataset.readerIndex) : NaN;
      if (Number.isFinite(caretIndex) && state.items.has(caretIndex)) {
        index = caretIndex;
        item = state.items.get(index);
        body = caretBody;
        utf16Offset = r.textOffsetWithin(body, caretNode, caretOffset);
      }
    }
    if (!item || !body) return null;
    var text = String(item.text_raw || '');
    if (!text) return null;
    var codePointOffset = utf16Offset === null
      ? Math.floor(r.codePointLength(text) / 2)
      : r.utf16ToCodePointIndex(text, utf16Offset);
    codePointOffset = nearestTextOffset(text, codePointOffset);
    if (codePointOffset === null) return null;
    return {
      startIndex: index,
      endIndex: index,
      startOffset: codePointOffset,
      endOffset: codePointOffset + 1
    };
  }

  function visibleSourceHighlightRange() {
    if (!state.elements || !state.open || !state.resolvedHighlights.size) return null;
    var viewportRect = state.elements.viewport.getClientRects()[0];
    if (!viewportRect) return null;
    var visibleMark = Array.from(
      state.elements.content.querySelectorAll('.mef-reader-item.has-highlight mark')
    ).some(function (mark) {
      return Array.from(mark.getClientRects()).some(function (rect) {
        return rect.bottom > viewportRect.top && rect.top < viewportRect.bottom;
      });
    });
    if (!visibleMark) return null;

    var boundaries = [];
    state.items.forEach(function (item, index) {
      var ranges = state.resolvedHighlights.get(r.itemAnchor(item, index)) || [];
      ranges.forEach(function (range) {
        boundaries.push({index: index, start: range.start, end: range.end});
      });
    });
    boundaries.sort(function (left, right) {
      return left.index === right.index
        ? left.start - right.start
        : left.index - right.index;
    });
    if (!boundaries.length) return null;
    var first = boundaries[0];
    var last = boundaries[boundaries.length - 1];
    return {
      startIndex: first.index,
      endIndex: last.index,
      startOffset: first.start,
      endOffset: last.end
    };
  }

  function setComparisonHighlights(spans) {
    state.comparison.highlights.clear();
    (Array.isArray(spans) ? spans : []).forEach(function (span) {
      var anchorId = String(
        span.pdf_page_id || span.paragraph_id || span.anchor_id || ''
      );
      var start = Number(
        span.paragraph_char_start != null
          ? span.paragraph_char_start
          : span.page_char_start
      );
      var end = Number(
        span.paragraph_char_end != null
          ? span.paragraph_char_end
          : span.page_char_end
      );
      if (!anchorId || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
        return;
      }
      if (!state.comparison.highlights.has(anchorId)) {
        state.comparison.highlights.set(anchorId, []);
      }
      state.comparison.highlights.get(anchorId).push({start: start, end: end});
    });
  }

  function comparisonItemAnchor(item, absoluteIndex) {
    return String(
      item.anchor_id ||
      item.pdf_page_id ||
      item.paragraph_id ||
      (state.comparison.targetSourceId + '-ITEM-' + String(absoluteIndex).padStart(6, '0'))
    );
  }

  function renderComparisonItem(item, absoluteIndex, previousItem) {
    var anchorId = comparisonItemAnchor(item, absoluteIndex);
    var article = document.createElement('article');
    article.className = 'mef-reader-item';
    article.dataset.readerIndex = String(absoluteIndex);
    article.dataset.readerAnchor = anchorId;

    var meta = document.createElement('header');
    meta.className = 'mef-reader-item-meta';
    var label = document.createElement('span');
    label.className = 'mef-reader-item-label is-source-page';
    var isParagraph = item.item_type === 'word_paragraph';
    label.textContent = item.page_display ||
      (isParagraph
        ? '段落 ' + (absoluteIndex + 1)
        : 'PDF 第 ' + (absoluteIndex + 1) + ' 页，引用页码尚未校准');
    meta.appendChild(label);

    var body = document.createElement('div');
    body.className = 'mef-reader-item-text';
    var text = String(item.text_raw || '');
    if (item.is_empty || !text) {
      body.classList.add('is-empty');
      body.textContent = isParagraph ? '本段无可显示文本' : '本页无文本层';
    } else {
      var ranges = (state.comparison.highlights.get(anchorId) || [])
        .concat(state.comparison.indexHighlights.get(absoluteIndex) || []);
      r.appendHighlightedText(body, text, ranges, item.decoration_spans);
      if (ranges.length) article.classList.add('has-highlight');
    }
    if (isPageContinuation(item, previousItem)) article.classList.add('is-continued');
    article.appendChild(meta);
    article.appendChild(body);
    return article;
  }

  // 连续段落排版：同一页的相邻段落不重复页码，只在页码变化处标出。
  function isPageContinuation(item, previousItem) {
    if (!previousItem || item.item_type !== 'word_paragraph') return false;
    var label = String(item.page_display || '');
    return !!label && label === String(previousItem.page_display || '');
  }

  function updateComparisonControls() {
    if (!state.elements) return;
    var comparison = state.comparison;
    state.elements.comparisonPrevious.disabled = comparison.loading ||
      comparison.previousStart === null;
    state.elements.comparisonNext.disabled = comparison.loading ||
      !comparison.hasMore || comparison.nextStart === null;
    state.elements.comparisonFollow.classList.toggle('is-active', comparison.autoFollow);
    state.elements.comparisonFollow.setAttribute(
      'aria-checked',
      comparison.autoFollow ? 'true' : 'false'
    );
  }

  function renderComparisonWindow() {
    var fragment = document.createDocumentFragment();
    Array.from(state.comparison.items.keys())
      .sort(function (left, right) { return left - right; })
      .forEach(function (index) {
        fragment.appendChild(renderComparisonItem(
          state.comparison.items.get(index),
          index,
          state.comparison.items.get(index - 1)
        ));
      });
    state.elements.comparisonContent.replaceChildren(fragment);
    r.applyDecorationVisibility();
    var target = state.elements.comparisonContent.querySelector(
      '[data-reader-index="' + state.comparison.currentIndex + '"]'
    );
    var focal = target && (target.querySelector('mark') || target);
    var viewport = state.elements.comparisonViewport;
    if (focal && viewport) {
      var focalRect = focal.getClientRects()[0];
      var viewportRect = viewport.getClientRects()[0];
      if (focalRect && viewportRect) {
        viewport.scrollTop = Math.max(
          0,
          viewport.scrollTop + focalRect.top + focalRect.height / 2 -
            viewportRect.top - viewportRect.height / 2
        );
      }
      viewport.scrollLeft = 0;
    }
    state.elements.viewport.scrollLeft = 0;
  }

  async function loadComparisonWindow(centerIndex, requestedStart) {
    var comparison = state.comparison;
    if (!comparison.open || !comparison.targetSourceId) return false;
    var count = Math.min(100, config.batchSize * 3);
    var start = requestedStart == null
      ? Math.max(0, centerIndex - config.batchSize)
      : Math.max(0, requestedStart);
    var serial = comparison.requestSerial + 1;
    comparison.requestSerial = serial;
    comparison.loading = true;
    updateComparisonControls();
    var query = new URLSearchParams({
      source_id: comparison.targetSourceId,
      start: String(start),
      count: String(count)
    });
    try {
      var response = await r.fetchFunction()(
        config.endpoint + '?' + query.toString(),
        {headers: {'Accept': 'application/json'}}
      );
      var payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || '对照文本加载失败');
      }
      if (serial !== comparison.requestSerial || !comparison.open) return false;
      var items = r.responseItems(payload);
      var responseStart = r.clampInteger(payload.start, start, 0, Number.MAX_SAFE_INTEGER);
      comparison.items.clear();
      items.forEach(function (item, offset) {
        var position = r.itemPosition(item, responseStart + offset);
        comparison.items.set(position, item);
      });
      comparison.currentIndex = comparison.items.has(centerIndex)
        ? centerIndex
        : (comparison.items.size ? Array.from(comparison.items.keys())[0] : 0);
      comparison.previousStart = payload.previous_start == null
        ? null
        : Number(payload.previous_start);
      comparison.hasMore = Boolean(payload.has_more);
      comparison.nextStart = comparison.hasMore && payload.next_start != null
        ? Number(payload.next_start)
        : null;
      renderComparisonWindow();
      return true;
    } catch (error) {
      if (serial === comparison.requestSerial) {
        var message = error && error.message ? error.message : '对照文本加载失败';
        r.setAlert(message, 'error');
        r.notify(message);
      }
      return false;
    } finally {
      if (serial === comparison.requestSerial) {
        comparison.loading = false;
        updateComparisonControls();
      }
    }
  }

  function showComparison(payload, targetDisplayName) {
    var comparison = state.comparison;
    var targetSourceId = String(payload.targetSourceId || payload.target_source_file_id || '');
    var changedTarget = comparison.targetSourceId !== targetSourceId;
    markComparisonOpen(targetSourceId);
    r.showPendingPane(false);
    r.rememberComparisonTarget(state.sourceId, targetSourceId);
    comparison.targetDisplayName = targetDisplayName ||
      String(payload.targetTitle || payload.target_title || '对齐版本');
    comparison.targetTitle = String(payload.targetTitle || payload.target_title || '');
    if (changedTarget) comparison.autoFollow = true;
    if (changedTarget) comparison.lastSourceRange = '';
    comparison.currentIndex = r.clampInteger(
      payload.targetIndex != null ? payload.targetIndex : payload.target_index,
      0,
      0,
      Number.MAX_SAFE_INTEGER
    );
    comparison.indexHighlights.clear();
    setComparisonHighlights(payload.pageMatchSpans || payload.page_match_spans || []);
    // 精确高亮不可用 = 粗定位 → 说明条如实标注。
    comparison.lowConfidence = (payload.preciseHighlightAvailable != null
      ? payload.preciseHighlightAvailable
      : payload.precise_highlight_available) === false;
    r.updateComparisonNotice();
    updateComparisonControls();
    r.renderToolbar();
    r.loadLinkWindow();
    noteReadingSessionChanged();
    return loadComparisonWindow(comparison.currentIndex);
  }

  function closeComparison() {
    var comparison = state.comparison;
    comparison.open = false;
    comparison.locateSerial += 1;
    comparison.requestSerial += 1;
    if (comparison.followTimer !== null) global.clearTimeout(comparison.followTimer);
    comparison.followTimer = null;
    comparison.targetSourceId = '';
    comparison.targetDisplayName = '';
    comparison.targetTitle = '';
    comparison.lastSourceRange = '';
    comparison.lowConfidence = false;
    comparison.items.clear();
    comparison.highlights.clear();
    comparison.indexHighlights.clear();
    comparison.previousStart = null;
    comparison.nextStart = null;
    comparison.hasMore = false;
    comparison.loading = false;
    state.links = null;
    state.linkRequestSerial += 1;
    r.closeReviewPopover();
    if (!state.elements) return;
    r.clearLinkedSelection();
    r.renderFlags();
    state.elements.panel.classList.remove('is-comparing');
    state.elements.readerBody.classList.remove('is-comparing', 'is-indirect', 'is-pending');
    state.elements.comparisonPane.hidden = true;
    state.elements.comparisonNotice.hidden = true;
    r.showPendingPane(false);
    state.elements.comparisonContent.replaceChildren();
    r.renderToolbar();
    noteReadingSessionChanged();
  }

  // 开关右栏同样改变会话：地址栏深链与服务端位置一起更新，否则刷新后右栏
  // 丢失、「继续阅读」记的还是上一次的版本对。
  function noteReadingSessionChanged() {
    var item = state.items.get(state.currentIndex);
    if (item && state.currentAnchorId) {
      r.scheduleReaderDeepLink(item, state.currentIndex, state.currentAnchorId);
    }
    r.scheduleReadingPositionSave();
  }

  function toggleComparisonFollow() {
    if (!state.comparison.open) return;
    state.comparison.autoFollow = !state.comparison.autoFollow;
    state.comparison.lastSourceRange = '';
    updateComparisonControls();
    if (state.comparison.autoFollow) scheduleComparisonFollow();
  }

  function loadComparisonPrevious() {
    if (state.comparison.previousStart === null) return;
    loadComparisonWindow(
      state.comparison.previousStart,
      state.comparison.previousStart
    );
  }

  function loadComparisonNext() {
    if (!state.comparison.hasMore || state.comparison.nextStart === null) return;
    loadComparisonWindow(state.comparison.nextStart, state.comparison.nextStart);
  }

  function scheduleComparisonFollow() {
    var comparison = state.comparison;
    if (!comparison.open || !comparison.autoFollow || state.outlineNavigating) return;
    if (comparison.followTimer !== null) global.clearTimeout(comparison.followTimer);
    comparison.followTimer = global.setTimeout(function () {
      comparison.followTimer = null;
      var selection = sourceCenterRange();
      if (!selection || !comparison.open || !comparison.autoFollow || state.outlineNavigating) return;
      var rangeKey = [
        selection.startIndex,
        selection.startOffset,
        selection.endOffset
      ].join(':');
      if (rangeKey === comparison.lastSourceRange) return;
      comparison.lastSourceRange = rangeKey;
      locateInAlignedVersion(comparison.targetSourceId, selection, true);
    }, 280);
  }

  async function locateInAlignedVersion(targetSourceId, requestedSelection, automatic) {
    var selection = requestedSelection || state.citationRange ||
      visibleSourceHighlightRange() || sourceCenterRange();
    if (!selection || !targetSourceId || (!automatic && state.alignmentLoading)) {
      return false;
    }
    var serial = state.comparison.locateSerial + 1;
    state.comparison.locateSerial = serial;
    if (!automatic) {
      state.alignmentLoading = true;
      r.updateCitationControls();
    }
    try {
      var response = await r.fetchFunction()(config.alignmentLocateEndpoint, {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          source_file_id: state.sourceId,
          target_source_file_id: targetSourceId,
          start_page_index: selection.startIndex,
          end_page_index: selection.endIndex,
          start_offset: selection.startOffset,
          end_offset: selection.endOffset
        })
      });
      var payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || '跨版本定位失败');
      }
      if (serial !== state.comparison.locateSerial || !state.open) return false;
      r.setAlert('', 'info');
      return showComparison({
        targetSourceId: payload.target_source_file_id,
        targetTitle: payload.target_title,
        targetIndex: payload.target_index,
        pageMatchSpans: payload.page_match_spans || [],
        matchOffsetUnit: payload.match_offset_unit,
        preciseHighlightAvailable: payload.precise_highlight_available
      }, r.alignmentTargetName(targetSourceId));
    } catch (error) {
      if (serial !== state.comparison.locateSerial) return false;
      var message = error && error.message ? error.message : '跨版本定位失败';
      r.setAlert(message, automatic ? 'warning' : 'error');
      if (!automatic) r.notify(message);
      return false;
    } finally {
      if (!automatic) {
        state.alignmentLoading = false;
        r.updateCitationControls();
      }
    }
  }


  r.pickLeftVersion = pickLeftVersion;
  r.swapComparison = swapComparison;
  r.openComparisonWith = openComparisonWith;
  r.markComparisonOpen = markComparisonOpen;
  r.isPageContinuation = isPageContinuation;
  r.updateComparisonControls = updateComparisonControls;
  r.renderComparisonWindow = renderComparisonWindow;
  r.loadComparisonWindow = loadComparisonWindow;
  r.closeComparison = closeComparison;
  r.noteReadingSessionChanged = noteReadingSessionChanged;
  r.toggleComparisonFollow = toggleComparisonFollow;
  r.loadComparisonPrevious = loadComparisonPrevious;
  r.loadComparisonNext = loadComparisonNext;
  r.scheduleComparisonFollow = scheduleComparisonFollow;
  r.locateInAlignedVersion = locateInAlignedVersion;
}(window));
