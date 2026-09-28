(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config;

  function citationTargetRange() {
    if (state.citationRange) return state.citationRange;
    var item = state.items.get(state.currentIndex);
    if (!item) return null;
    return {
      startIndex: state.currentIndex,
      endIndex: state.currentIndex,
      startAnchorId: r.itemAnchor(item, state.currentIndex),
      endAnchorId: r.itemAnchor(item, state.currentIndex),
      startOffset: 0,
      endOffset: r.codePointLength(item.text_raw || ''),
      startDisplay: r.backendPageDisplay(item),
      endDisplay: r.backendPageDisplay(item),
      selectedText: '',
      citationPayload: {
        page_range: {verified: item.page_verified === true},
        citation_formats: item.citation_formats || {}
      }
    };
  }

  function citationCanCopy(target) {
    var payload = target && target.citationPayload;
    var formats = payload && payload.citation_formats;
    var pageRange = payload && payload.page_range;
    return Boolean(
      formats &&
      formats.can_copy === true &&
      (!pageRange || pageRange.verified === true)
    );
  }

  function citationStyleCanCopy(target, style) {
    var payload = target && target.citationPayload;
    var formats = payload && payload.citation_formats;
    var pageRange = payload && payload.page_range;
    return Boolean(
      formats &&
      formats.can_copy === true &&
      formats[style + '_status'] === 'complete' &&
      (!pageRange || pageRange.verified === true)
    );
  }

  function updateCitationControls() {
    if (!state.elements) return;
    var target = citationTargetRange();
    state.elements.current.disabled = !state.items.has(state.currentIndex);
    state.elements.cite.disabled = !state.items.has(state.currentIndex);
    state.elements.cite.setAttribute(
      'aria-expanded',
      state.citationMenuOpen ? 'true' : 'false'
    );
    state.elements.citationBar.hidden = !state.citationMenuOpen;
    var canCopy = citationCanCopy(target);
    state.elements.copyFootnote.disabled = state.citationLoading ||
      !citationStyleCanCopy(target, 'chinese');
    state.elements.copyGbt.disabled = state.citationLoading ||
      !citationStyleCanCopy(target, 'gb');
    state.elements.clearSelection.hidden = !state.citationRange;
    state.elements.alignmentActions.hidden = !state.citationRange ||
      !state.alignmentTargets.length;
    Array.from(state.elements.alignmentActions.querySelectorAll('button')).forEach(
      function (button) { button.disabled = state.alignmentLoading; }
    );
    if (!target) {
      state.elements.citationContext.textContent = '当前没有可引用的页码';
      return;
    }
    var context = state.citationRange
      ? '已选择：' + target.startDisplay + (
        target.endIndex === target.startIndex
          ? ''
          : ' → ' + target.endDisplay
      )
      : '当前页：' + target.startDisplay;
    if (state.citationLoading) context += '（正在生成引文…）';
    else if (!canCopy) context += '（页码未验证，暂不可复制）';
    state.elements.citationContext.textContent = context;
  }

  // 清标题：去掉文件名里常见的来源/作者括号垃圾（如「 (…(Judith Butler)) (Z-Library)」）。
  function toggleCitationMenu() {
    if (!state.open || !state.items.has(state.currentIndex)) return;
    state.citationMenuOpen = !state.citationMenuOpen;
    updateCitationControls();
  }

  function clearCitationRange() {
    state.citationRequestSerial += 1;
    state.citationRange = null;
    state.selectionDragging = false;
    state.citationLoading = false;
    var selection = typeof global.getSelection === 'function'
      ? global.getSelection()
      : null;
    if (selection && typeof selection.removeAllRanges === 'function') {
      selection.removeAllRanges();
    }
    updateCitationControls();
  }

  function beginSelectionDrag() {
    state.selectionDragging = true;
  }

  function resetCitation() {
    state.citationRequestSerial += 1;
    state.citationRange = null;
    state.selectionDragging = false;
    state.citationMenuOpen = false;
    state.citationLoading = false;
  }

  function invalidateCitationForJump() {
    state.citationRequestSerial += 1;
    state.citationRange = null;
  }

  function elementForRangeNode(node) {
    if (!node) return null;
    return node.nodeType === 1 ? node : node.parentElement;
  }

  function textOffsetWithin(container, node, offset) {
    if (!container || !node || typeof document.createTreeWalker !== 'function') {
      return null;
    }
    var element = elementForRangeNode(node);
    if (!element || !(element === container || container.contains(element))) {
      return null;
    }
    var walker = document.createTreeWalker(
      container,
      global.NodeFilter ? global.NodeFilter.SHOW_TEXT : 4
    );
    var utf16Total = 0;
    var textNode = walker.nextNode();
    while (textNode) {
      if (textNode === node) {
        return utf16Total + r.clampInteger(
          offset,
          0,
          0,
          String(textNode.nodeValue || '').length
        );
      }
      utf16Total += String(textNode.nodeValue || '').length;
      textNode = walker.nextNode();
    }
    if (typeof document.createRange !== 'function') return null;
    // Element-node boundaries are uncommon but valid; Range supplies their
    // DOM boundary while all ordinary text/mark boundaries use TreeWalker.
    try {
      var prefix = document.createRange();
      prefix.selectNodeContents(container);
      prefix.setEnd(node, offset);
      return prefix.toString().length;
    } catch (_error) {
      return null;
    }
  }

  function captureMountedSelection() {
    if (!state.open || typeof global.getSelection !== 'function') return null;
    var selection = global.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    var range = selection.getRangeAt(0);
    var startElement = elementForRangeNode(range.startContainer);
    var endElement = elementForRangeNode(range.endContainer);
    var startBody = startElement && startElement.closest
      ? startElement.closest('.mef-reader-item-text')
      : null;
    var endBody = endElement && endElement.closest
      ? endElement.closest('.mef-reader-item-text')
      : null;
    if (
      !startBody ||
      !endBody ||
      !state.elements.content.contains(startBody) ||
      !state.elements.content.contains(endBody)
    ) {
      return null;
    }
    var startArticle = startBody.closest('.mef-reader-item');
    var endArticle = endBody.closest('.mef-reader-item');
    if (!startArticle || !endArticle) return null;

    var startIndex = Number(startArticle.dataset.readerIndex);
    var endIndex = Number(endArticle.dataset.readerIndex);
    if (!Number.isFinite(startIndex) || !Number.isFinite(endIndex)) return null;
    var startItem = state.items.get(startIndex);
    var endItem = state.items.get(endIndex);
    if (!startItem || !endItem) return null;

    var startUtf16 = textOffsetWithin(
      startBody,
      range.startContainer,
      range.startOffset
    );
    var endUtf16 = textOffsetWithin(
      endBody,
      range.endContainer,
      range.endOffset
    );
    if (startUtf16 === null || endUtf16 === null) return null;
    var selectedText = selection.toString();
    if (!selectedText) return null;

    return {
      startIndex: startIndex,
      endIndex: endIndex,
      startOffset: r.utf16ToCodePointIndex(startItem.text_raw || '', startUtf16),
      endOffset: r.utf16ToCodePointIndex(endItem.text_raw || '', endUtf16),
      startAnchorId: r.itemAnchor(startItem, startIndex),
      endAnchorId: r.itemAnchor(endItem, endIndex),
      startDisplay: r.backendPageDisplay(startItem),
      endDisplay: r.backendPageDisplay(endItem),
      selectedText: selectedText
    };
  }

  function scheduleSelectionCapture() {
    global.setTimeout(function () {
      var captured = captureMountedSelection();
      state.selectionDragging = false;
      if (!captured) {
        var selection = typeof global.getSelection === 'function'
          ? global.getSelection()
          : null;
        if (selection && !selection.isCollapsed) {
          state.citationRequestSerial += 1;
          state.citationRange = null;
          state.citationLoading = false;
          updateCitationControls();
          var warning = '选区端点必须都在当前已载入的文本窗口内，请缩小选区后重试';
          r.setAlert(warning, 'warning');
          r.notify(warning);
        }
        return;
      }
      state.citationRange = captured;
      state.citationMenuOpen = true;
      updateCitationControls();
      prefetchCitationRange(captured);
    }, 0);
  }

  function selectionBlocksWindowShift() {
    if (state.selectionDragging) return true;
    var selection = typeof global.getSelection === 'function'
      ? global.getSelection()
      : null;
    return Boolean(selection && !selection.isCollapsed);
  }

  async function writeClipboard(text) {
    var value = String(text || '');
    if (!value) throw new Error('后端没有返回可复制的引文');
    if (
      global.navigator &&
      global.navigator.clipboard &&
      typeof global.navigator.clipboard.writeText === 'function'
    ) {
      try {
        await global.navigator.clipboard.writeText(value);
        return;
      } catch (_clipboardError) {
        // Continue to the local textarea fallback below.
      }
    }
    var textarea = document.createElement('textarea');
    textarea.className = 'mef-reader-clipboard-fallback';
    textarea.value = value;
    textarea.setAttribute('readonly', 'readonly');
    document.body.appendChild(textarea);
    textarea.select();
    var copied = false;
    try {
      copied = typeof document.execCommand === 'function' &&
        document.execCommand('copy');
    } finally {
      textarea.remove();
    }
    if (!copied) throw new Error('无法写入剪贴板，请检查系统剪贴板权限');
  }

  async function prefetchCitationRange(target) {
    var serial = state.citationRequestSerial + 1;
    state.citationRequestSerial = serial;
    state.citationLoading = true;
    updateCitationControls();
    try {
      var response = await r.fetchFunction()(config.citationEndpoint, {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          source_id: state.sourceId,
          start_anchor_id: target.startAnchorId,
          end_anchor_id: target.endAnchorId
        })
      });
      var payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || '引文生成失败');
      }
      if (
        serial !== state.citationRequestSerial ||
        state.citationRange !== target
      ) {
        return false;
      }
      target.citationPayload = payload;
      if (!citationCanCopy(target)) {
        r.setAlert(
          (payload.page_range && payload.page_range.note) ||
          '所选页码尚未验证，暂不能复制带页码引文',
          'warning'
        );
      }
      return true;
    } catch (error) {
      if (serial !== state.citationRequestSerial) return false;
      var message = error && error.message
        ? error.message
        : '引文生成失败';
      r.setAlert(message, 'error');
      r.notify(message);
      return false;
    } finally {
      if (serial === state.citationRequestSerial) {
        state.citationLoading = false;
        updateCitationControls();
      }
    }
  }

  function copyCachedCitation(style) {
    var target = citationTargetRange();
    if (!citationStyleCanCopy(target, style)) {
      var warning = state.citationLoading
        ? '所选页码范围的引文仍在生成，请稍候'
        : (
          citationCanCopy(target)
            ? '当前引文缺少该格式所需的书目信息，暂不能复制'
            : '当前页码尚未验证，暂不能复制带页码引文'
        );
      r.setAlert(warning, 'warning');
      r.notify(warning);
      return Promise.resolve(false);
    }
    var formats = target.citationPayload.citation_formats || {};
    var citation = String(formats[style] || '');
    return writeClipboard(citation).then(function () {
      r.notify(style === 'gb' ? 'GB/T 7714 引文已复制' : '中文脚注已复制');
      return true;
    }).catch(function (error) {
      var message = error && error.message
        ? error.message
        : '引文复制失败';
      r.setAlert(message, 'error');
      r.notify(message);
      return false;
    });
  }


  r.updateCitationControls = updateCitationControls;
  r.toggleCitationMenu = toggleCitationMenu;
  r.clearCitationRange = clearCitationRange;
  r.beginSelectionDrag = beginSelectionDrag;
  r.resetCitation = resetCitation;
  r.invalidateCitationForJump = invalidateCitationForJump;
  r.elementForRangeNode = elementForRangeNode;
  r.textOffsetWithin = textOffsetWithin;
  r.scheduleSelectionCapture = scheduleSelectionCapture;
  r.selectionBlocksWindowShift = selectionBlocksWindowShift;
  r.copyCachedCitation = copyCachedCitation;
}(window));
