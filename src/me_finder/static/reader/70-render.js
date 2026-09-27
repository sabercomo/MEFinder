(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config;

  function applyDecorationVisibility() {
    var show = !!state.showDecorations;
    [state.elements.content, state.elements.comparisonContent].forEach(
      function (root) {
        if (root && root.classList) {
          root.classList.toggle('mef-show-decorations', show);
        }
      }
    );
    if (state.openMenu === 'more') r.renderMenu('more');
  }

  function toggleDecorationVisibility() {
    state.showDecorations = !state.showDecorations;
    applyDecorationVisibility();
  }

  function mergeCodePointRanges(ranges, codePointCount) {
    var normalized = (ranges || [])
      .map(function (range) {
        return {
          start: r.clampInteger(range.start, 0, 0, codePointCount),
          end: r.clampInteger(range.end, 0, 0, codePointCount)
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
    return merged;
  }

  function decorationKindAt(decorations, start, end) {
    for (var i = 0; i < decorations.length; i += 1) {
      if (decorations[i].start <= start && end <= decorations[i].end) {
        return decorations[i].kind || 'decoration';
      }
    }
    return null;
  }

  // Renders `text` (the page's untouched text_raw) into `container`, wrapping
  // highlight ranges in <mark> and page-decoration ranges (running headers /
  // footers / visible folios) in a hidden <span>.  Every branch appends a real
  // text node covering the exact source characters, so the DOM text content
  // stays character-for-character equal to text_raw — the citation/highlight
  // offset coordinate system (see textOffsetWithin) is never disturbed, whether
  // or not the decoration is visually shown.
  function appendHighlightedText(container, text, ranges, decorations, linkedRanges) {
    var codePoints = Array.from(text);
    var highlights = mergeCodePointRanges(ranges, r.codePointLength(text));
    var linked = mergeCodePointRanges(linkedRanges, r.codePointLength(text));
    var decoRanges = (decorations || [])
      .map(function (span) {
        var range = {
          start: r.clampInteger(span.start, 0, 0, r.codePointLength(text)),
          end: r.clampInteger(span.end, 0, 0, r.codePointLength(text)),
          kind: span.kind || 'decoration'
        };
        // Swallow the blank line a hidden header/footer would otherwise leave
        // behind under `white-space: pre-wrap`.  Only whitespace is absorbed, so
        // the concatenated text nodes still equal text_raw and offsets hold.
        var isBlank = function (index) {
          return index >= 0 && index < codePoints.length &&
            /^\s$/.test(codePoints[index]);
        };
        while (range.end < codePoints.length && isBlank(range.end) &&
               codePoints[range.end] !== '\n') {
          range.end += 1;
        }
        if (range.end < codePoints.length && codePoints[range.end] === '\n') {
          range.end += 1;
        } else {
          while (range.start > 0 && isBlank(range.start - 1) &&
                 codePoints[range.start - 1] !== '\n') {
            range.start -= 1;
          }
          if (range.start > 0 && codePoints[range.start - 1] === '\n') {
            range.start -= 1;
          }
        }
        return range;
      })
      .filter(function (span) { return span.end > span.start; });
    if (!highlights.length && !decoRanges.length && !linked.length) {
      container.appendChild(document.createTextNode(text));
      return;
    }

    var codePointCount = r.codePointLength(text);
    var boundarySet = {0: true};
    boundarySet[codePointCount] = true;
    highlights.forEach(function (range) {
      boundarySet[range.start] = true;
      boundarySet[range.end] = true;
    });
    decoRanges.forEach(function (span) {
      boundarySet[span.start] = true;
      boundarySet[span.end] = true;
    });
    linked.forEach(function (range) {
      boundarySet[range.start] = true;
      boundarySet[range.end] = true;
    });
    var boundaries = Object.keys(boundarySet)
      .map(Number)
      .sort(function (left, right) { return left - right; });

    var isHighlighted = function (start, end) {
      return highlights.some(function (range) {
        return range.start <= start && end <= range.end;
      });
    };

    for (var i = 0; i < boundaries.length - 1; i += 1) {
      var start = boundaries[i];
      var end = boundaries[i + 1];
      if (end <= start) continue;
      var slice = text.slice(
        r.codePointToUtf16Index(text, start),
        r.codePointToUtf16Index(text, end)
      );
      if (!slice) continue;
      var node = document.createTextNode(slice);
      if (isHighlighted(start, end)) {
        var mark = document.createElement('mark');
        mark.className = 'mef-reader-highlight';
        mark.appendChild(node);
        node = mark;
      }
      // 选中段落的对应关系：只包一层 span，文本节点不变，偏移坐标系不受影响。
      if (linked.some(function (range) { return range.start <= start && end <= range.end; })) {
        var linkedSpan = document.createElement('span');
        linkedSpan.className = 'mef-reader-linked';
        linkedSpan.appendChild(node);
        node = linkedSpan;
      }
      var kind = decorationKindAt(decoRanges, start, end);
      if (kind) {
        var decoration = document.createElement('span');
        decoration.className = 'mef-reader-decoration';
        decoration.dataset.decorationKind = kind;
        decoration.appendChild(node);
        node = decoration;
      }
      container.appendChild(node);
    }
  }

  function nearestQuoteRange(text, quote, savedCodePointStart) {
    if (!quote) return null;
    var best = null;
    var fromUtf16 = 0;
    while (fromUtf16 <= text.length) {
      var foundUtf16 = text.indexOf(quote, fromUtf16);
      if (foundUtf16 < 0) break;
      var foundStart = r.utf16ToCodePointIndex(text, foundUtf16);
      var distance = Math.abs(foundStart - savedCodePointStart);
      if (!best || distance < best.distance) {
        best = {
          start: foundStart,
          end: foundStart + r.codePointLength(quote),
          distance: distance,
          recoveredByQuote: true,
          matchQuote: quote
        };
      }
      fromUtf16 = foundUtf16 + 1;
    }
    return best;
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
    var recovered = [];
    ranges.forEach(function (range) {
      var quote = range.matchQuote || state.matchQuote;
      var nearest = nearestQuoteRange(text, quote, range.start);
      if (!nearest) return;
      var duplicate = recovered.some(function (existing) {
        return existing.start === nearest.start && existing.end === nearest.end;
      });
      if (!duplicate) recovered.push(nearest);
    });
    if (recovered.length) {
      state.hashRecoveryNotice = '文本内容已变化，已在同一页按原句重新定位';
      r.setAlert(state.hashRecoveryNotice, 'warning');
      return recovered;
    }
    state.hashRecoveryNotice = '文本内容已变化，已跳转到相应页，但无法精确高亮';
    r.setAlert(state.hashRecoveryNotice, 'warning');
    return [];
  }

  function renderItem(item, absoluteIndex) {
    var anchorId = r.itemAnchor(item, absoluteIndex);
    var article = document.createElement('article');
    article.className = 'mef-reader-item';
    article.id = 'mef-reader-anchor-' + r.domSafeId(anchorId);
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
      (item.page_source_type === 'section_break_inferred' ||
       item.page_source_type === 'epub_page_list' ||
       item.page_source_type === 'epub_pagebreak') &&
      !item.anchor_id;
    var showItemPageLabel = !documentOnlyPage && !inferredPageContinuation;
    label.textContent = documentOnlyPage
      ? (item.document_page_range || item.page_display ||
         (item.item_type === 'word_paragraph'
           ? '段落 ' + (absoluteIndex + 1)
           : '页码尚未解析'))
      : ((showItemPageLabel && item.page_display) || (
        item.item_type === 'word_paragraph'
          ? '段落 ' + (absoluteIndex + 1)
          : 'PDF 第 ' + (absoluteIndex + 1) + ' 页，引用页码尚未校准'
      ));
    meta.appendChild(label);

    if (
      showItemPageLabel &&
      item.page_note &&
      item.page_note !== item.page_display
    ) {
      // 页码来源说明放进提示，连续排版里不再与页码并排重复。
      label.title = item.page_note;
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
      state.resolvedHighlights.set(anchorId, ranges);
      appendHighlightedText(body, text, ranges, item.decoration_spans, state.linkedRanges.get(absoluteIndex));
      if (ranges.length) article.classList.add('has-highlight');
      if (state.linkedRanges.has(absoluteIndex)) article.classList.add('is-linked');
    }
    if (r.isPageContinuation(item, state.items.get(absoluteIndex - 1))) {
      article.classList.add('is-continued');
    }

    article.appendChild(meta);
    article.appendChild(body);
    return article;
  }

  function renderWindow(scrollAnchorId) {
    var elements = r.ensureDom();
    r.disconnectObservers();
    state.resolvedHighlights.clear();

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
    applyDecorationVisibility();

    /*
     * Position the requested anchor before observing boundaries.  Observing
     * first lets the newly mounted top sentinel report as visible before
     * the requested focal range is positioned, which can pull an initial jump back toward
     * the beginning of a long document.
     */
    var targetAnchor = scrollAnchorId || state.targetAnchorId;
    var target = targetAnchor ? r.findAnchorNode(targetAnchor) : null;
    if (!target) {
      target = elements.content.querySelector(
        '[data-reader-index="' + state.currentIndex + '"]'
      );
    }
    if (target) {
      r.positionSourceTarget(target);
    }

    state.pageObserver = r.createPageObserver();
    if (state.pageObserver) {
      elements.content.querySelectorAll('.mef-reader-item').forEach(function (node) {
        state.pageObserver.observe(node);
      });
    }

    state.boundaryObserver = r.createBoundaryObserver();
    if (state.boundaryObserver) {
      state.boundaryObserver.observe(beforeBoundary);
      state.boundaryObserver.observe(afterBoundary);
    }
    if (state.comparison.open) r.loadLinkWindow();
  }

  function prepareHighlights(options) {
    state.highlights.clear();
    state.resolvedHighlights.clear();
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


  r.applyDecorationVisibility = applyDecorationVisibility;
  r.toggleDecorationVisibility = toggleDecorationVisibility;
  r.appendHighlightedText = appendHighlightedText;
  r.renderWindow = renderWindow;
  r.prepareHighlights = prepareHighlights;
}(window));
