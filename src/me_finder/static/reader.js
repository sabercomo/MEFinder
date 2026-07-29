(function (global) {
  'use strict';

  /*
   * The structured-text reader deliberately lives outside app.js.  Its public
   * surface is intentionally small so the search UI can opt into it without
   * changing the user's PDFKit / WebView2 / system-reader preference.
   */
  var DEFAULTS = {
    endpoint: '/api/document/pages',
    batchSize: 20,
    radiusBatches: 1,
    estimatedItemHeight: 360
  };

  var config = {
    endpoint: DEFAULTS.endpoint,
    batchSize: DEFAULTS.batchSize,
    radiusBatches: DEFAULTS.radiusBatches,
    estimatedItemHeight: DEFAULTS.estimatedItemHeight,
    fetch: null,
    notify: null
  };

  var state = {
    open: false,
    sourceId: '',
    source: null,
    title: '',
    total: 0,
    windowStart: 0,
    windowEnd: 0,
    hasPrevious: false,
    hasMore: false,
    previousStart: null,
    nextStart: null,
    currentIndex: 0,
    currentAnchorId: '',
    items: new Map(),
    highlights: new Map(),
    targetAnchorId: '',
    preciseHighlight: true,
    matchQuote: '',
    hashRecoveryNotice: '',
    requestSerial: 0,
    abortController: null,
    loading: false,
    pageObserver: null,
    boundaryObserver: null,
    visibleRatios: new Map(),
    restoreFocus: null,
    onCurrentChange: null,
    elements: null
  };

  function clampInteger(value, fallback, minimum, maximum) {
    if (value == null || value === '' || typeof value === 'boolean') {
      return fallback;
    }
    var parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    parsed = Math.floor(parsed);
    return Math.max(minimum, Math.min(maximum, parsed));
  }

  /*
   * Python offsets are Unicode code points; String#slice consumes UTF-16 code
   * units.  Count the UTF-16 width of each full code point before slicing.
   */
  function codePointToUtf16Index(text, codePointOffset) {
    var target = clampInteger(codePointOffset, 0, 0, Number.MAX_SAFE_INTEGER);
    var codePointsSeen = 0;
    var utf16Index = 0;
    var character;
    for (character of String(text || '')) {
      if (codePointsSeen >= target) break;
      utf16Index += character.length;
      codePointsSeen += 1;
    }
    return utf16Index;
  }

  function codePointLength(text) {
    var length = 0;
    var character;
    for (character of String(text || '')) length += 1;
    return length;
  }

  function utf16ToCodePointIndex(text, utf16Offset) {
    return codePointLength(String(text || '').slice(
      0,
      clampInteger(utf16Offset, 0, 0, String(text || '').length)
    ));
  }

  function domSafeId(value) {
    return String(value || 'item')
      .replace(/[^A-Za-z0-9_.:-]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'item';
  }

  function itemAnchor(item, absoluteIndex) {
    return String(
      item.anchor_id ||
      item.pdf_page_id ||
      item.paragraph_id ||
      (state.sourceId + '-ITEM-' + String(absoluteIndex).padStart(6, '0'))
    );
  }

  function itemPosition(item, fallback) {
    var candidates = [
      item.pdf_page_index,
      item.paragraph_index,
      item.item_index
    ];
    var index;
    for (index = 0; index < candidates.length; index += 1) {
      var candidate = candidates[index];
      if (
        candidate != null &&
        candidate !== '' &&
        typeof candidate !== 'boolean' &&
        Number.isFinite(Number(candidate))
      ) {
        return Math.max(0, Math.floor(Number(candidate)));
      }
    }
    return fallback;
  }

  function inferIndexFromAnchor(anchorId) {
    var match = /-PAGE-(\d+)$/.exec(String(anchorId || ''));
    return match ? Number(match[1]) : null;
  }

  function resolveTargetIndex(options) {
    var candidates = [
      options.targetIndex,
      options.itemIndex,
      options.item_index,
      options.pdfPageIndex,
      options.pdf_page_index,
      options.pdf_page_start_index,
      options.paragraphIndex,
      options.paragraph_index
    ];
    var index;
    for (index = 0; index < candidates.length; index += 1) {
      var candidate = candidates[index];
      if (
        candidate != null &&
        candidate !== '' &&
        typeof candidate !== 'boolean' &&
        Number.isFinite(Number(candidate))
      ) {
        return Math.max(0, Math.floor(Number(candidate)));
      }
    }
    return Math.max(0, inferIndexFromAnchor(options.anchorId || options.anchor_id) || 0);
  }

  function notify(message) {
    if (!message) return;
    if (typeof config.notify === 'function') {
      config.notify(message);
    } else if (typeof global.showToast === 'function') {
      global.showToast(message);
    }
  }

  function setAlert(message, kind) {
    ensureDom();
    var alert = state.elements.alert;
    alert.textContent = message || '';
    alert.hidden = !message;
    alert.dataset.kind = kind || 'info';
  }

  function createButton(label, className, action) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.dataset.readerAction = action;
    return button;
  }

  function ensureDom() {
    if (state.elements && state.elements.root.isConnected) return state.elements;

    var root = document.createElement('div');
    root.className = 'mef-structured-reader';
    root.id = 'mef-structured-reader';
    root.hidden = true;
    root.setAttribute('aria-hidden', 'true');

    var backdrop = document.createElement('button');
    backdrop.type = 'button';
    backdrop.className = 'mef-reader-backdrop';
    backdrop.dataset.readerAction = 'close';
    backdrop.setAttribute('aria-label', '关闭结构化阅读器');

    var panel = document.createElement('section');
    panel.className = 'mef-reader-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'mef-reader-title');

    var header = document.createElement('header');
    header.className = 'mef-reader-header';

    var heading = document.createElement('div');
    heading.className = 'mef-reader-heading';
    var eyebrow = document.createElement('span');
    eyebrow.className = 'mef-reader-eyebrow';
    eyebrow.textContent = '结构化文本';
    var title = document.createElement('h2');
    title.id = 'mef-reader-title';
    title.className = 'mef-reader-title';
    title.textContent = '文献阅读';
    heading.appendChild(eyebrow);
    heading.appendChild(title);

    var current = document.createElement('div');
    current.className = 'mef-reader-current';
    current.setAttribute('aria-live', 'polite');
    current.textContent = '正在载入…';

    var close = createButton('关闭', 'mef-reader-close', 'close');
    close.setAttribute('aria-label', '关闭结构化阅读器');

    header.appendChild(heading);
    header.appendChild(current);
    header.appendChild(close);

    var alert = document.createElement('div');
    alert.className = 'mef-reader-alert';
    alert.setAttribute('role', 'status');
    alert.setAttribute('aria-live', 'polite');
    alert.hidden = true;

    var viewport = document.createElement('div');
    viewport.className = 'mef-reader-viewport';
    viewport.tabIndex = 0;
    viewport.setAttribute('aria-label', '结构化文献正文');

    var content = document.createElement('div');
    content.className = 'mef-reader-content';
    viewport.appendChild(content);

    var loading = document.createElement('div');
    loading.className = 'mef-reader-loading';
    loading.setAttribute('role', 'status');
    loading.textContent = '正在载入文本…';
    loading.hidden = true;

    panel.appendChild(header);
    panel.appendChild(alert);
    panel.appendChild(viewport);
    panel.appendChild(loading);
    root.appendChild(backdrop);
    root.appendChild(panel);
    document.body.appendChild(root);

    root.addEventListener('click', function (event) {
      var action = event.target && event.target.dataset
        ? event.target.dataset.readerAction
        : '';
      if (action === 'close') closeReader();
    });

    state.elements = {
      root: root,
      panel: panel,
      title: title,
      current: current,
      alert: alert,
      viewport: viewport,
      content: content,
      loading: loading,
      close: close
    };
    return state.elements;
  }

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
      if (ratio > bestRatio || (ratio === bestRatio && index < bestIndex)) {
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
    var anchorId = itemAnchor(item, index);
    state.currentIndex = index;
    state.currentAnchorId = anchorId;
    var documentOnlyPage = item.item_type === 'word_paragraph' &&
      (item.page_source_type === 'toc_range_bound' ||
       item.page_source_type === 'unknown');
    var pageLabel = documentOnlyPage
      ? (item.document_page_range ||
         '段落 ' + (index + 1))
      : (item.page_display || item.page_note || '');
    if (!pageLabel) {
      pageLabel = item.item_type === 'word_paragraph'
        ? '段落 ' + (index + 1)
        : 'PDF 第 ' + (index + 1) + ' 页';
    }
    state.elements.current.textContent = pageLabel;
    if (typeof state.onCurrentChange === 'function') {
      state.onCurrentChange({
        sourceId: state.sourceId,
        index: index,
        anchorId: item.anchor_id || null,
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
    if (state.loading || !state.open) return;
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

  function appendHighlightedText(container, text, ranges) {
    if (!ranges || !ranges.length) {
      container.appendChild(document.createTextNode(text));
      return;
    }
    var codePointCount = codePointLength(text);
    var normalized = ranges
      .map(function (range) {
        return {
          start: clampInteger(range.start, 0, 0, codePointCount),
          end: clampInteger(range.end, 0, 0, codePointCount)
        };
      })
      .filter(function (range) { return range.end > range.start; })
      .sort(function (left, right) { return left.start - right.start; });

    var merged = [];
    normalized.forEach(function (range) {
      var previous = merged.length ? merged[merged.length - 1] : null;
      if (previous && range.start <= previous.end) {
        previous.end = Math.max(previous.end, range.end);
      } else {
        merged.push({start: range.start, end: range.end});
      }
    });

    var cursor = 0;
    merged.forEach(function (range) {
      var beforeStart = codePointToUtf16Index(text, cursor);
      var beforeEnd = codePointToUtf16Index(text, range.start);
      if (beforeEnd > beforeStart) {
        container.appendChild(document.createTextNode(text.slice(beforeStart, beforeEnd)));
      }
      var mark = document.createElement('mark');
      mark.className = 'mef-reader-highlight';
      mark.textContent = text.slice(
        codePointToUtf16Index(text, range.start),
        codePointToUtf16Index(text, range.end)
      );
      container.appendChild(mark);
      cursor = range.end;
    });
    var tailStart = codePointToUtf16Index(text, cursor);
    if (tailStart < text.length) {
      container.appendChild(document.createTextNode(text.slice(tailStart)));
    }
  }

  function highlightRangesForItem(item, anchorId) {
    if (!state.preciseHighlight) return [];
    var ranges = state.highlights.get(anchorId) || [];
    var matchingHash = ranges.filter(function (range) {
      return !range.pageTextHash ||
        !item.page_text_hash ||
        range.pageTextHash === item.page_text_hash;
    });
    if (matchingHash.length || !ranges.length) return matchingHash;

    /*
     * A document may have been reparsed after the search.  The stored offsets
     * are then unsafe, so first try the short match_quote on this same page.
     * If it is absent we retain the page jump but deliberately do not mark an
     * unrelated range.
     */
    var text = typeof item.text_raw === 'string' ? item.text_raw : '';
    var quotes = ranges
      .map(function (range) { return range.matchQuote; })
      .filter(function (quote, index, allQuotes) {
        return quote && allQuotes.indexOf(quote) === index;
      });
    if (state.matchQuote && quotes.indexOf(state.matchQuote) < 0) {
      quotes.push(state.matchQuote);
    }
    var recovered = [];
    quotes.forEach(function (quote) {
      var quoteUtf16Start = text.indexOf(quote);
      if (quoteUtf16Start < 0) return;
      var quoteStart = utf16ToCodePointIndex(text, quoteUtf16Start);
      recovered.push({
        start: quoteStart,
        end: quoteStart + codePointLength(quote),
        recoveredByQuote: true
      });
    });
    if (recovered.length) {
      state.hashRecoveryNotice = '文本内容已变化，已在同一页按原句重新定位。';
      setAlert(state.hashRecoveryNotice, 'warning');
      return recovered;
    }
    state.hashRecoveryNotice = '文本内容已变化，已跳转到相应页，但无法精确高亮。';
    setAlert(state.hashRecoveryNotice, 'warning');
    return [];
  }

  function renderItem(item, absoluteIndex) {
    var anchorId = itemAnchor(item, absoluteIndex);
    var article = document.createElement('article');
    article.className = 'mef-reader-item';
    article.id = 'mef-reader-anchor-' + domSafeId(anchorId);
    article.dataset.readerIndex = String(absoluteIndex);
    article.dataset.readerAnchor = anchorId;

    var meta = document.createElement('header');
    meta.className = 'mef-reader-item-meta';

    var label = document.createElement('span');
    label.className = 'mef-reader-item-label';
    var documentOnlyPage = item.item_type === 'word_paragraph' &&
      (item.page_source_type === 'toc_range_bound' ||
       item.page_source_type === 'unknown');
    var inferredPageContinuation = item.item_type === 'word_paragraph' &&
      item.page_source_type === 'section_break_inferred' &&
      !item.anchor_id;
    var showItemPageLabel = !documentOnlyPage && !inferredPageContinuation;
    label.textContent = (showItemPageLabel && item.page_display) || (
      item.item_type === 'word_paragraph'
        ? '段落 ' + (absoluteIndex + 1)
        : 'PDF 第 ' + (absoluteIndex + 1) + ' 页'
    );
    meta.appendChild(label);

    if (
      showItemPageLabel &&
      item.page_note &&
      item.page_note !== item.page_display
    ) {
      var note = document.createElement('span');
      note.className = 'mef-reader-item-note';
      note.textContent = item.page_note;
      meta.appendChild(note);
    }

    var body = document.createElement('div');
    body.className = 'mef-reader-item-text';
    var text = typeof item.text_raw === 'string' ? item.text_raw : '';
    if (item.is_empty || !text) {
      body.classList.add('is-empty');
      body.textContent = item.item_type === 'word_paragraph'
        ? '本段无可显示文本'
        : '本页无文本层';
    } else {
      var ranges = highlightRangesForItem(item, anchorId);
      appendHighlightedText(body, text, ranges);
      if (ranges.length) article.classList.add('has-highlight');
    }

    article.appendChild(meta);
    article.appendChild(body);
    return article;
  }

  function renderWindow(scrollAnchorId) {
    var elements = ensureDom();
    disconnectObservers();

    /*
     * Exactly two spacers represent every unloaded item.  We never create a
     * hidden DOM node per page, so a 900-page book still has only the current
     * window (at most (2 * radiusBatches + 1) batches) mounted.
     */
    var fragment = document.createDocumentFragment();
    var beforeSpacer = document.createElement('div');
    beforeSpacer.className = 'mef-reader-spacer';
    beforeSpacer.style.height = (
      (state.hasPrevious ? config.batchSize : 0) * config.estimatedItemHeight
    ) + 'px';
    beforeSpacer.setAttribute('aria-hidden', 'true');
    fragment.appendChild(beforeSpacer);

    var beforeBoundary = document.createElement('div');
    beforeBoundary.className = 'mef-reader-boundary';
    beforeBoundary.dataset.readerBoundary = 'before';
    beforeBoundary.setAttribute('aria-hidden', 'true');
    fragment.appendChild(beforeBoundary);

    Array.from(state.items.keys())
      .sort(function (left, right) { return left - right; })
      .forEach(function (index) {
        fragment.appendChild(renderItem(state.items.get(index), index));
      });

    var afterBoundary = document.createElement('div');
    afterBoundary.className = 'mef-reader-boundary';
    afterBoundary.dataset.readerBoundary = 'after';
    afterBoundary.setAttribute('aria-hidden', 'true');
    fragment.appendChild(afterBoundary);

    var afterSpacer = document.createElement('div');
    afterSpacer.className = 'mef-reader-spacer';
    afterSpacer.style.height = (
      (state.hasMore ? config.batchSize : 0) * config.estimatedItemHeight
    ) + 'px';
    afterSpacer.setAttribute('aria-hidden', 'true');
    fragment.appendChild(afterSpacer);

    elements.content.replaceChildren(fragment);

    /*
     * Position the requested anchor before observing boundaries.  Observing
     * first lets the newly mounted top sentinel report as visible before
     * scrollIntoView runs, which can pull an initial deep jump back toward
     * the beginning of a long document.
     */
    var targetAnchor = scrollAnchorId || state.targetAnchorId;
    var target = targetAnchor ? findAnchorNode(targetAnchor) : null;
    if (!target) {
      target = elements.content.querySelector(
        '[data-reader-index="' + state.currentIndex + '"]'
      );
    }
    if (target) {
      target.scrollIntoView({block: 'center'});
      setCurrentItem(Number(target.dataset.readerIndex));
    }

    state.pageObserver = createPageObserver();
    if (state.pageObserver) {
      elements.content.querySelectorAll('.mef-reader-item').forEach(function (node) {
        state.pageObserver.observe(node);
      });
    }

    state.boundaryObserver = createBoundaryObserver();
    if (state.boundaryObserver) {
      state.boundaryObserver.observe(beforeBoundary);
      state.boundaryObserver.observe(afterBoundary);
    }
  }

  function findAnchorNode(anchorId) {
    var found = null;
    state.elements.content.querySelectorAll('.mef-reader-item').forEach(function (node) {
      if (!found && node.dataset.readerAnchor === anchorId) found = node;
    });
    return found;
  }

  function responseItems(payload) {
    if (Array.isArray(payload.items)) return payload.items;
    if (Array.isArray(payload.pages)) return payload.pages;
    return [];
  }

  function fetchFunction() {
    var candidate = config.fetch || global.fetch;
    if (typeof candidate !== 'function') {
      throw new Error('当前环境不支持读取结构化文本');
    }
    return candidate.bind(global);
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
      var response = await fetchFunction()(
        config.endpoint + '?' + query.toString(),
        requestOptions
      );
      var payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || '结构化文本加载失败');
      }
      if (serial !== state.requestSerial || !state.open) return false;

      var items = responseItems(payload);
      var responseStart = clampInteger(payload.start, start, 0, Number.MAX_SAFE_INTEGER);
      state.total = clampInteger(
        payload.total,
        responseStart + items.length,
        0,
        Number.MAX_SAFE_INTEGER
      );
      state.source = payload.source || state.source;
      if (!state.title && state.source) {
        state.title = state.source.display_title ||
          state.source.document_title ||
          state.source.file_name ||
          '';
        state.elements.title.textContent = state.title || '文献阅读';
      }

      if (mode === 'replace') state.items.clear();
      items.forEach(function (item, offset) {
        var position = itemPosition(item, responseStart + offset);
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
      renderWindow(scrollAnchorId);

      if (!items.length) {
        setAlert('这本文献暂时没有可显示的结构化文本。', 'info');
      }
      return true;
    } catch (error) {
      if (error && error.name === 'AbortError') return false;
      if (serial === state.requestSerial) {
        setAlert(error && error.message ? error.message : '结构化文本加载失败', 'error');
        notify(error && error.message ? error.message : '结构化文本加载失败');
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
      local.scrollIntoView({block: 'center'});
      setCurrentItem(Number(local.dataset.readerIndex));
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

  function prepareHighlights(options) {
    state.highlights.clear();
    state.matchQuote = String(options.matchQuote || options.match_quote || '');
    state.hashRecoveryNotice = '';
    var spans = options.pageMatchSpans || options.page_match_spans || [];
    var paragraphAnchor = String(
      options.paragraphId || options.paragraph_id || ''
    );
    var paragraphStart = Number(
      options.matchStart != null ? options.matchStart : options.match_start
    );
    var paragraphEnd = Number(
      options.matchEnd != null ? options.matchEnd : options.match_end
    );
    if (
      Array.isArray(spans) &&
      spans.length === 0 &&
      paragraphAnchor &&
      Number.isFinite(paragraphStart) &&
      Number.isFinite(paragraphEnd) &&
      paragraphEnd > paragraphStart
    ) {
      spans = [{
        anchor_id: paragraphAnchor,
        page_char_start: paragraphStart,
        page_char_end: paragraphEnd
      }];
    }
    var offsetUnit = options.matchOffsetUnit ||
      options.match_offset_unit ||
      'unicode_codepoint';
    state.preciseHighlight = options.preciseHighlightAvailable !== false &&
      options.precise_highlight_available !== false &&
      offsetUnit === 'unicode_codepoint' &&
      Array.isArray(spans) &&
      spans.length > 0;

    if (!state.preciseHighlight) return;
    spans.forEach(function (span) {
      var anchorId = String(span.pdf_page_id || span.anchor_id || '');
      var start = Number(span.page_char_start);
      var end = Number(span.page_char_end);
      if (!anchorId || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;
      if (!state.highlights.has(anchorId)) state.highlights.set(anchorId, []);
      state.highlights.get(anchorId).push({
        start: start,
        end: end,
        pageTextHash: span.page_text_hash || '',
        matchQuote: String(span.match_quote || span.page_match_quote || '')
      });
    });
    if (!state.highlights.size) state.preciseHighlight = false;
  }

  function configure(options) {
    options = options || {};
    if (options.endpoint) config.endpoint = String(options.endpoint);
    if (typeof options.fetch === 'function') config.fetch = options.fetch;
    if (typeof options.notify === 'function') config.notify = options.notify;
    if (options.notify === null) config.notify = null;
    config.batchSize = clampInteger(options.batchSize, config.batchSize, 5, 100);
    config.radiusBatches = clampInteger(
      options.radiusBatches,
      config.radiusBatches,
      0,
      4
    );
    while ((config.radiusBatches * 2 + 1) * config.batchSize > 100) {
      config.radiusBatches -= 1;
    }
    config.estimatedItemHeight = clampInteger(
      options.estimatedItemHeight,
      config.estimatedItemHeight,
      120,
      1200
    );
  }

  async function openReader(options) {
    options = options || {};
    var sourceId = String(options.sourceId || options.source_id || '');
    if (!sourceId) throw new Error('缺少文献标识，无法打开结构化文本');
    ensureDom();

    if (options.config) configure(options.config);
    if (!state.open) state.restoreFocus = document.activeElement;
    state.open = true;
    state.sourceId = sourceId;
    state.source = null;
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
    state.windowStart = 0;
    state.windowEnd = 0;
    state.hasPrevious = false;
    state.hasMore = false;
    state.previousStart = null;
    state.nextStart = null;
    state.currentIndex = resolveTargetIndex(options);
    prepareHighlights(options);

    state.elements.title.textContent = state.title || '文献阅读';
    state.elements.current.textContent = '正在载入…';
    state.elements.root.hidden = false;
    state.elements.root.setAttribute('aria-hidden', 'false');
    document.body.classList.add('mef-reader-open');
    setAlert('', 'info');

    if (!state.preciseHighlight && (
      options.preciseHighlightAvailable === false ||
      options.precise_highlight_available === false ||
      options.legacyIndex === true ||
      options.legacy_index === true
    )) {
      var legacyMessage = '此文献使用旧索引，已跳转到相应位置，但无法精确高亮。重新导入后可启用精确定位。';
      setAlert(legacyMessage, 'warning');
      notify(legacyMessage);
    }

    var loaded = await loadWindow(state.currentIndex, state.targetAnchorId);
    if (loaded) state.elements.close.focus();
    return loaded;
  }

  async function goTo(target) {
    if (!state.open) return false;
    var options = typeof target === 'object' && target !== null
      ? target
      : (typeof target === 'number' ? {targetIndex: target} : {anchorId: target});
    var anchorId = String(options.anchorId || options.anchor_id || '');
    var index = resolveTargetIndex(options);
    if (anchorId) {
      var local = findAnchorNode(anchorId);
      if (local) {
        local.scrollIntoView({block: 'center'});
        setCurrentItem(Number(local.dataset.readerIndex));
        return true;
      }
      var inferred = inferIndexFromAnchor(anchorId);
      if (inferred !== null) index = inferred;
    }
    state.targetAnchorId = anchorId;
    state.currentIndex = index;
    return loadWindow(index, anchorId);
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
    state.open = false;
    state.requestSerial += 1;
    if (state.abortController) state.abortController.abort();
    disconnectObservers();
    state.items.clear();
    state.highlights.clear();
    state.elements.content.replaceChildren();
    state.elements.root.hidden = true;
    state.elements.root.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('mef-reader-open');
    if (state.restoreFocus && typeof state.restoreFocus.focus === 'function') {
      state.restoreFocus.focus();
    }
    state.restoreFocus = null;
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
      windowStart: state.windowStart,
      windowEnd: state.windowEnd,
      hasPrevious: state.hasPrevious,
      hasMore: state.hasMore,
      previousStart: state.previousStart,
      nextStart: state.nextStart,
      mountedItemCount: state.items.size,
      currentIndex: state.currentIndex,
      currentAnchorId: state.currentAnchorId
    };
  }

  document.addEventListener('keydown', function (event) {
    if (state.open && event.key === 'Escape') closeReader();
  });

  global.MEFinderReader = Object.freeze({
    open: openReader,
    openForSearchResult: openForSearchResult,
    close: closeReader,
    goTo: goTo,
    configure: configure,
    destroy: destroy,
    isOpen: function () { return state.open; },
    getState: getState,
    codePointToUtf16Index: codePointToUtf16Index
  });
}(window));
