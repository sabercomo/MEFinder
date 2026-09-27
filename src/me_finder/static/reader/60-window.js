(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config;

  function disconnectObservers() {
    if (state.pageObserver) state.pageObserver.disconnect();
    if (state.boundaryObserver) state.boundaryObserver.disconnect();
    state.pageObserver = null;
    state.boundaryObserver = null;
    state.visibleRatios.clear();
  }

  function updateCurrentFromObserver() {
    var bestIndex = null;
    var bestRatio = -1;
    state.visibleRatios.forEach(function (ratio, index) {
      var closerToCurrent = bestIndex === null ||
        Math.abs(index - state.currentIndex) <
          Math.abs(bestIndex - state.currentIndex);
      var stableOrder = bestIndex === null ||
        (
          Math.abs(index - state.currentIndex) ===
            Math.abs(bestIndex - state.currentIndex) &&
          index < bestIndex
        );
      if (
        ratio > bestRatio ||
        (ratio === bestRatio && (closerToCurrent || stableOrder))
      ) {
        bestRatio = ratio;
        bestIndex = index;
      }
    });
    if (bestIndex === null) return;
    setCurrentItem(bestIndex);
  }

  function setCurrentItem(index) {
    var item = state.items.get(index);
    if (!item) return;
    var anchorId = r.itemAnchor(item, index);
    var currentChanged = state.currentAnchorId !== anchorId;
    state.currentIndex = index;
    state.currentAnchorId = anchorId;
    var pageLabel = r.backendPageDisplay(item);
    state.elements.current.textContent = pageLabel;
    state.elements.current.title = '点击复制此页或当前选区的引文';
    r.updateCitationControls();
    if (currentChanged) {
      r.scheduleReaderDeepLink(item, index, anchorId);
      r.scheduleComparisonFollow();
      r.scheduleReadingPositionSave();
    }
    if (typeof state.onCurrentChange === 'function') {
      state.onCurrentChange({
        sourceId: state.sourceId,
        index: index,
        anchorId: anchorId,
        item: item,
        pageDisplay: pageLabel
      });
    }
  }

  function createPageObserver() {
    if (typeof global.IntersectionObserver !== 'function') return null;
    return new global.IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var index = Number(entry.target.dataset.readerIndex);
        if (!Number.isFinite(index)) return;
        if (entry.isIntersecting) state.visibleRatios.set(index, entry.intersectionRatio);
        else state.visibleRatios.delete(index);
      });
      updateCurrentFromObserver();
    }, {
      root: state.elements.viewport,
      threshold: [0, 0.2, 0.45, 0.7, 1]
    });
  }

  function shiftWindow(direction) {
    if (state.loading || !state.open || r.selectionBlocksWindowShift()) return;
    var batchSize = config.batchSize;
    if (direction < 0 && state.previousStart === null) return;
    if (direction > 0 && (!state.hasMore || state.nextStart === null)) return;
    var preserveAnchor = state.currentAnchorId;
    if (direction > 0) {
      loadRange(state.nextStart, batchSize, 'forward', preserveAnchor);
    } else {
      var windowCount = Math.min(
        100,
        (config.radiusBatches * 2 + 1) * config.batchSize
      );
      loadRange(
        state.previousStart,
        windowCount,
        'replace',
        preserveAnchor,
        state.currentIndex
      );
    }
  }

  function createBoundaryObserver() {
    if (typeof global.IntersectionObserver !== 'function') return null;
    return new global.IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        if (entry.target.dataset.readerBoundary === 'before') shiftWindow(-1);
        if (entry.target.dataset.readerBoundary === 'after') shiftWindow(1);
      });
    }, {
      root: state.elements.viewport,
      rootMargin: '220px 0px',
      threshold: 0
    });
  }

  function handleReaderNavigationKey(event) {
    if (
      !state.open ||
      state.loading ||
      r.selectionBlocksWindowShift() ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey
    ) {
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      r.goTo({targetIndex: 0});
    } else if (event.key === 'End' && state.lastPosition !== null) {
      event.preventDefault();
      r.goTo({targetIndex: state.lastPosition});
    }
  }

  function scheduleScrollBoundaryCheck() {
    if (state.scrollBoundaryTimer !== null) return;
    state.scrollBoundaryTimer = global.setTimeout(function () {
      state.scrollBoundaryTimer = null;
      if (
        !state.open ||
        state.loading ||
        !state.elements ||
        r.selectionBlocksWindowShift()
      ) {
        return;
      }
      var viewport = state.elements.viewport;
      var threshold = Math.max(
        48,
        Math.min(config.estimatedItemHeight, viewport.clientHeight / 2)
      );
      if (
        viewport.scrollTop + viewport.clientHeight >=
        viewport.scrollHeight - threshold
      ) {
        shiftWindow(1);
      } else if (viewport.scrollTop <= threshold) {
        shiftWindow(-1);
      }
    }, 40);
  }

  function findAnchorNode(anchorId) {
    var found = null;
    state.elements.content.querySelectorAll('.mef-reader-item').forEach(function (node) {
      if (!found && node.dataset.readerAnchor === anchorId) found = node;
    });
    return found;
  }

  function positionSourceTarget(target) {
    if (!target || !state.elements) return;
    var viewport = state.elements.viewport;
    var focal = target.querySelector('mark') || target;
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
    setCurrentItem(Number(target.dataset.readerIndex));
  }

  function responseItems(payload) {
    if (Array.isArray(payload.items)) return payload.items;
    if (Array.isArray(payload.pages)) return payload.pages;
    return [];
  }

  function responseContainsAnchor(items, responseStart, anchorId) {
    return items.some(function (item, offset) {
      var position = r.itemPosition(item, responseStart + offset);
      return r.itemAnchor(item, position) === anchorId;
    });
  }

  function windowBounds(centerIndex) {
    var batchSize = config.batchSize;
    var start = Math.max(0, centerIndex - config.radiusBatches * batchSize);
    var count = (config.radiusBatches * 2 + 1) * batchSize;
    return {start: start, count: Math.min(100, count)};
  }

  function trimMountedItems(mode) {
    var maximum = Math.min(
      100,
      (config.radiusBatches * 2 + 1) * config.batchSize
    );
    var positions = Array.from(state.items.keys()).sort(function (left, right) {
      return left - right;
    });
    while (positions.length > maximum) {
      var removeAt = mode === 'backward' ? positions.pop() : positions.shift();
      state.items.delete(removeAt);
    }
    positions = Array.from(state.items.keys()).sort(function (left, right) {
      return left - right;
    });
    state.windowStart = positions.length ? positions[0] : 0;
    state.windowEnd = positions.length ? positions[positions.length - 1] + 1 : 0;
  }

  function closestLoadedPosition(target) {
    var positions = Array.from(state.items.keys());
    if (!positions.length) return 0;
    positions.sort(function (left, right) {
      var leftDistance = Math.abs(left - target);
      var rightDistance = Math.abs(right - target);
      return leftDistance === rightDistance ? left - right : leftDistance - rightDistance;
    });
    return positions[0];
  }

  async function loadRange(start, count, mode, scrollAnchorId, centerIndex) {
    if (!state.open || !state.sourceId) return false;
    state.loading = true;
    state.elements.loading.hidden = false;
    var serial = state.requestSerial + 1;
    var priorWindowStart = state.items.size ? state.windowStart : null;
    state.requestSerial = serial;
    if (state.abortController) state.abortController.abort();
    state.abortController = typeof global.AbortController === 'function'
      ? new global.AbortController()
      : null;

    var query = new URLSearchParams({
      source_id: state.sourceId,
      start: String(start),
      count: String(count)
    });

    try {
      var requestOptions = {headers: {'Accept': 'application/json'}};
      if (state.abortController) requestOptions.signal = state.abortController.signal;
      var response = await r.fetchFunction()(
        config.endpoint + '?' + query.toString(),
        requestOptions
      );
      var payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || '结构化文本加载失败');
      }
      if (serial !== state.requestSerial || !state.open) return false;

      var items = responseItems(payload);
      var responseStart = r.clampInteger(payload.start, start, 0, Number.MAX_SAFE_INTEGER);
      if (
        mode === 'replace' &&
        scrollAnchorId &&
        !responseContainsAnchor(items, responseStart, scrollAnchorId)
      ) {
        throw new Error('链接锚点不属于该文献或已失效');
      }
      state.total = r.clampInteger(
        payload.total,
        responseStart + items.length,
        0,
        Number.MAX_SAFE_INTEGER
      );
      state.lastPosition = (
        payload.last_position != null &&
        payload.last_position !== '' &&
        typeof payload.last_position !== 'boolean' &&
        Number.isFinite(Number(payload.last_position))
      )
        ? Math.max(0, Math.floor(Number(payload.last_position)))
        : null;
      state.source = payload.source || state.source;
      // 眉标固定「正在阅读」；解析记录（结构化文本 · MinerU）落在书名 tooltip，不再占眉标、也不做没用的 ⋯。
      state.elements.title.title = state.source && state.source.parser_label
        ? '结构化文本 · ' + state.source.parser_label
        : '结构化文本';
      if (!state.title && state.source) {
        state.title = state.source.display_title ||
          state.source.document_title ||
          state.source.file_name ||
          '';
      }
      if (state.source) {
        state.elements.title.textContent = r.cleanReaderTitle(state.title) || '文献阅读';
        var byline = r.readerByline(state.source);
        state.elements.subtitle.textContent = byline;
        state.elements.subtitle.hidden = !byline;
      }

      if (mode === 'replace') state.items.clear();
      items.forEach(function (item, offset) {
        var position = r.itemPosition(item, responseStart + offset);
        state.items.set(position, item);
      });
      trimMountedItems(mode);
      if (mode === 'forward' && priorWindowStart !== null) {
        state.previousStart = priorWindowStart;
      } else {
        state.previousStart = payload.previous_start != null &&
          payload.previous_start !== '' &&
          typeof payload.previous_start !== 'boolean' &&
          Number.isFinite(Number(payload.previous_start))
          ? Math.max(0, Math.floor(Number(payload.previous_start)))
          : null;
      }
      state.hasPrevious = state.previousStart !== null;
      if (mode !== 'backward') {
        state.hasMore = Boolean(payload.has_more);
        state.nextStart = payload.next_start != null &&
          payload.next_start !== '' &&
          Number.isFinite(Number(payload.next_start))
          ? Math.max(0, Math.floor(Number(payload.next_start)))
          : null;
      }
      if (mode === 'replace') {
        state.currentIndex = closestLoadedPosition(
          Number.isFinite(Number(centerIndex)) ? Number(centerIndex) : responseStart
        );
      }
      r.renderWindow(scrollAnchorId);

      if (!items.length) {
        r.setAlert('这本文献暂时没有可显示的结构化文本', 'info');
      }
      return true;
    } catch (error) {
      if (error && error.name === 'AbortError') return false;
      if (serial === state.requestSerial) {
        r.setAlert(error && error.message ? error.message : '结构化文本加载失败', 'error');
        r.notify(error && error.message ? error.message : '结构化文本加载失败');
      }
      return false;
    } finally {
      if (serial === state.requestSerial) {
        state.loading = false;
        state.elements.loading.hidden = true;
      }
    }
  }

  async function loadWindow(centerIndex, scrollAnchorId) {
    var local = scrollAnchorId ? findAnchorNode(scrollAnchorId) : null;
    if (local) {
      positionSourceTarget(local);
      return true;
    }
    var bounds = windowBounds(centerIndex);
    return loadRange(
      bounds.start,
      bounds.count,
      'replace',
      scrollAnchorId,
      centerIndex
    );
  }


  r.disconnectObservers = disconnectObservers;
  r.createPageObserver = createPageObserver;
  r.createBoundaryObserver = createBoundaryObserver;
  r.handleReaderNavigationKey = handleReaderNavigationKey;
  r.scheduleScrollBoundaryCheck = scheduleScrollBoundaryCheck;
  r.findAnchorNode = findAnchorNode;
  r.positionSourceTarget = positionSourceTarget;
  r.responseItems = responseItems;
  r.loadWindow = loadWindow;
}(window));
