(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config;

  function loadLinkWindow() {
    var comparison = state.comparison;
    if (!state.open || !comparison.open || !comparison.targetSourceId ||
        !state.elements.pending.hidden || !state.items.size) return;
    var positions = Array.from(state.items.keys()).sort(function (a, b) { return a - b; });
    var start = positions[0];
    var end = positions[positions.length - 1];
    var targetId = comparison.targetSourceId;
    var key = [state.sourceId, targetId, start, end].join(':');
    if (state.links && state.links.key === key) {
      renderFlags();
      return;
    }
    var serial = state.linkRequestSerial + 1;
    state.linkRequestSerial = serial;
    var query = new URLSearchParams({
      source_file_id: state.sourceId,
      target_source_file_id: targetId,
      start_index: String(start),
      end_index: String(end)
    });
    r.readJSON(config.linksEndpoint + '?' + query.toString()).then(function (payload) {
      if (serial !== state.linkRequestSerial || comparison.targetSourceId !== targetId) return;
      state.links = {key: key, viaId: payload.via_source_file_id || '', items: payload.links || []};
      renderFlags();
    }).catch(function () {
      if (serial !== state.linkRequestSerial) return;
      state.links = {key: key, viaId: '', items: []};
      renderFlags();
    });
  }

  // 低置信：算法给出了对应段落但置信度低于门槛。没有对应的（副文本、漏段）不标。
  // 待检查与否由后端判定（与作品页「N 处待检查」同一口径），前端不另立规则。
  function linkNeedsReview(link) {
    return link.needs_review === true;
  }

  function linkKey(link) {
    return (link.source_segment_ids || []).slice().sort().join('|');
  }

  function textPointAt(body, codePointOffset) {
    var walker = document.createTreeWalker(body, global.NodeFilter ? global.NodeFilter.SHOW_TEXT : 4);
    var remaining = codePointOffset;
    var node = walker.nextNode();
    while (node) {
      var value = String(node.nodeValue || '');
      var length = r.codePointLength(value);
      if (remaining <= length) {
        return {node: node, offset: r.codePointToUtf16Index(value, remaining)};
      }
      remaining -= length;
      node = walker.nextNode();
    }
    return null;
  }

  function renderFlags() {
    if (!state.elements) return;
    state.elements.content.querySelectorAll('.mef-reader-flag').forEach(function (flag) { flag.remove(); });
    var links = state.links;
    // 只在直接对齐上标注：间接关联的置信度来自两段换算，不适合逐段人工校正。
    if (!links || links.viaId || !state.comparison.open) return;
    links.items.forEach(function (link, index) {
      if (!linkNeedsReview(link)) return;
      var span = (link.source_spans || [])[0];
      if (!span) return;
      var article = state.elements.content.querySelector('[data-reader-index="' + span.item_index + '"]');
      var body = article && article.querySelector('.mef-reader-item-text');
      if (!body) return;
      var top = 0;
      var point = textPointAt(body, span.char_start);
      if (point && typeof document.createRange === 'function') {
        var range = document.createRange();
        range.setStart(point.node, point.offset);
        range.collapse(true);
        var rect = range.getClientRects()[0];
        var articleRect = article.getClientRects()[0];
        if (rect && articleRect) top = Math.max(0, rect.top - articleRect.top);
      }
      var flag = r.createButton('!', 'mef-reader-flag' + (link.deferred ? ' is-deferred' : ''), 'review-link');
      flag.dataset.readerLink = String(index);
      flag.style.top = top + 'px';
      flag.setAttribute('aria-label', link.deferred ? '已暂不处理的对应，点击重新检查' : '对应可能不准，点击检查');
      flag.title = link.deferred ? '已暂不处理' : '对应可能不准';
      flag.setAttribute('aria-haspopup', 'dialog');
      article.appendChild(flag);
    });
  }

  function scheduleFlagLayout() {
    if (state.flagLayoutTimer !== null) return;
    state.flagLayoutTimer = global.setTimeout(function () {
      state.flagLayoutTimer = null;
      if (state.open) renderFlags();
    }, 120);
  }

  function clearLinkedSelection() {
    var changed = Array.from(state.linkedRanges.keys());
    state.linkedRanges.clear();
    state.selectedLinkKey = '';
    changed.forEach(refreshSourceItem);
  }

  function refreshSourceItem(index) {
    if (!state.elements) return;
    var article = state.elements.content.querySelector('[data-reader-index="' + index + '"]');
    var item = state.items.get(index);
    if (!article || !item) return;
    var body = article.querySelector('.mef-reader-item-text');
    var text = typeof item.text_raw === 'string' ? item.text_raw : '';
    if (!body || item.is_empty || !text) return;
    body.replaceChildren();
    var anchorId = r.itemAnchor(item, index);
    r.appendHighlightedText(
      body, text, state.resolvedHighlights.get(anchorId) || [], item.decoration_spans,
      state.linkedRanges.get(index) || []
    );
    article.classList.toggle('is-linked', state.linkedRanges.has(index));
  }

  function selectLinkAtClick(event) {
    if (!state.comparison.open || !state.links || !state.links.items.length) return;
    var selection = typeof global.getSelection === 'function' ? global.getSelection() : null;
    if (selection && !selection.isCollapsed) return;
    var body = event.target.closest('.mef-reader-item-text');
    var article = body && body.closest('.mef-reader-item');
    var index = article ? Number(article.dataset.readerIndex) : NaN;
    if (!Number.isFinite(index)) return;
    var caretNode = null;
    var caretOffset = null;
    if (typeof document.caretPositionFromPoint === 'function') {
      var position = document.caretPositionFromPoint(event.clientX, event.clientY);
      if (position) { caretNode = position.offsetNode; caretOffset = position.offset; }
    } else if (typeof document.caretRangeFromPoint === 'function') {
      var caretRange = document.caretRangeFromPoint(event.clientX, event.clientY);
      if (caretRange) { caretNode = caretRange.startContainer; caretOffset = caretRange.startOffset; }
    }
    var utf16 = r.textOffsetWithin(body, caretNode, caretOffset);
    var item = state.items.get(index);
    if (utf16 === null || !item) return;
    var offset = r.utf16ToCodePointIndex(item.text_raw || '', utf16);
    var link = state.links.items.find(function (candidate) {
      return (candidate.source_spans || []).some(function (span) {
        return span.item_index === index && span.char_start <= offset && offset < span.char_end;
      });
    });
    if (!link) return;
    if (state.selectedLinkKey === linkKey(link)) {
      clearLinkedSelection();
      state.comparison.indexHighlights.clear();
      r.renderComparisonWindow();
      return;
    }
    highlightLink(link);
  }

  function highlightLink(link) {
    var previous = Array.from(state.linkedRanges.keys());
    state.linkedRanges.clear();
    state.selectedLinkKey = linkKey(link);
    (link.source_spans || []).forEach(function (span) {
      if (!state.linkedRanges.has(span.item_index)) state.linkedRanges.set(span.item_index, []);
      state.linkedRanges.get(span.item_index).push({start: span.char_start, end: span.char_end});
    });
    previous.concat(Array.from(state.linkedRanges.keys())).forEach(refreshSourceItem);
    var comparison = state.comparison;
    comparison.indexHighlights.clear();
    comparison.highlights.clear();
    (link.target_spans || []).forEach(function (span) {
      if (!comparison.indexHighlights.has(span.item_index)) comparison.indexHighlights.set(span.item_index, []);
      comparison.indexHighlights.get(span.item_index).push({start: span.char_start, end: span.char_end});
    });
    var first = (link.target_spans || [])[0];
    if (!first) {
      r.renderComparisonWindow();
      r.setAlert(link.manual === 'no_counterpart' ? '已人工确认：另一版本中没有对应段落' : '这一段在另一版本中没有找到对应段落', 'info');
      return;
    }
    comparison.currentIndex = first.item_index;
    if (comparison.items.has(first.item_index)) r.renderComparisonWindow();
    else r.loadComparisonWindow(first.item_index);
  }


  r.loadLinkWindow = loadLinkWindow;
  r.renderFlags = renderFlags;
  r.scheduleFlagLayout = scheduleFlagLayout;
  r.clearLinkedSelection = clearLinkedSelection;
  r.selectLinkAtClick = selectLinkAtClick;
}(window));
