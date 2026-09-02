(function (global) {
  'use strict';

  /*
   * The structured-text reader deliberately lives outside app.js.  Its public
   * surface is intentionally small so the search UI can opt into it without
   * changing the user's PDFKit / WebView2 / system-reader preference.
   */
  var DEFAULTS = {
    endpoint: '/api/document/pages',
    citationEndpoint: '/api/document/citation',
    alignmentTargetsEndpoint: '/api/text-alignments/targets',
    alignmentLocateEndpoint: '/api/text-alignments/locate',
    batchSize: 20,
    radiusBatches: 1,
    estimatedItemHeight: 360
  };

  var config = {
    endpoint: DEFAULTS.endpoint,
    citationEndpoint: DEFAULTS.citationEndpoint,
    alignmentTargetsEndpoint: DEFAULTS.alignmentTargetsEndpoint,
    alignmentLocateEndpoint: DEFAULTS.alignmentLocateEndpoint,
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
    lastPosition: null,
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
    resolvedHighlights: new Map(),
    targetAnchorId: '',
    preciseHighlight: true,
    showDecorations: false,
    matchQuote: '',
    hashRecoveryNotice: '',
    requestSerial: 0,
    abortController: null,
    loading: false,
    pageObserver: null,
    boundaryObserver: null,
    visibleRatios: new Map(),
    citationRange: null,
    selectionDragging: false,
    citationMenuOpen: false,
    citationLoading: false,
    citationRequestSerial: 0,
    alignmentTargets: [],
    alignmentLoading: false,
    alignmentRequestSerial: 0,
    comparison: {
      open: false,
      targetSourceId: '',
      targetDisplayName: '',
      targetTitle: '',
      autoFollow: true,
      locateSerial: 0,
      requestSerial: 0,
      followTimer: null,
      lastSourceRange: '',
      items: new Map(),
      highlights: new Map(),
      currentIndex: 0,
      previousStart: null,
      nextStart: null,
      hasMore: false,
      loading: false
    },
    lastSession: null,
    lastDeepLink: '',
    lastHistoryAnchor: '',
    deepLinkTimer: null,
    pendingDeepLink: null,
    scrollBoundaryTimer: null,
    originalUrl: '',
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
    var match = /(?:-PAGE-|-P)(\d+)$/.exec(String(anchorId || ''));
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

  function backendPageDisplay(item) {
    var display = item && typeof item.page_display === 'string'
      ? item.page_display.trim()
      : '';
    return display || '页码信息不可用';
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

    // ── 第一行：正在阅读 · 书名 · 作者 ·········· ⋯ ×
    var headRow = document.createElement('div');
    headRow.className = 'mef-reader-headrow';

    var heading = document.createElement('div');
    heading.className = 'mef-reader-heading';
    var eyebrow = document.createElement('span');
    eyebrow.className = 'mef-reader-eyebrow';
    eyebrow.textContent = '正在阅读';
    var title = document.createElement('h2');
    title.id = 'mef-reader-title';
    title.className = 'mef-reader-title';
    title.textContent = '文献阅读';
    var subtitle = document.createElement('div');
    subtitle.className = 'mef-reader-subtitle';
    subtitle.hidden = true;
    heading.appendChild(eyebrow);
    heading.appendChild(title);
    heading.appendChild(subtitle);

    var close = createButton('', 'mef-reader-close', 'close');
    close.setAttribute('aria-label', '关闭结构化阅读器');
    var closeGlyph = document.createElement('span');
    closeGlyph.setAttribute('aria-hidden', 'true');
    closeGlyph.textContent = '✕';
    close.appendChild(closeGlyph);

    headRow.appendChild(heading);
    headRow.appendChild(close);

    // ── 第二行：[阅读 | 双栏对照] ·········· 页眉页脚开关  引用页
    var toolRow = document.createElement('div');
    toolRow.className = 'mef-reader-toolrow';

    // 模式轴：阅读 / 双栏对照（分段控件）。无对齐版本时整体隐藏。
    var modeSeg = document.createElement('div');
    modeSeg.className = 'mef-reader-mode-seg';
    modeSeg.hidden = true;

    var current = createButton(
      '正在载入…',
      'mef-reader-current',
      'toggle-citation'
    );
    current.className = 'mef-reader-current';
    current.setAttribute('aria-live', 'polite');
    current.setAttribute('aria-haspopup', 'true');
    current.setAttribute('aria-expanded', 'false');
    current.textContent = '正在载入…';

    // 页眉页脚：开／关状态 → 开关（复用对照栏那套开关样式）。
    var toggleDecorations = createButton(
      '',
      'mef-reader-follow-switch mef-reader-decoration-switch',
      'toggle-decorations'
    );
    toggleDecorations.setAttribute('aria-pressed', 'false');
    toggleDecorations.setAttribute('aria-label', '显示页眉页脚');
    toggleDecorations.setAttribute(
      'title',
      '页眉、页脚与页码默认隐藏；开启以对照原书'
    );
    var decoTrack = document.createElement('span');
    decoTrack.className = 'mef-follow-track';
    decoTrack.setAttribute('aria-hidden', 'true');
    var decoLabel = document.createElement('span');
    decoLabel.className = 'mef-follow-label';
    decoLabel.textContent = '页眉页脚';
    toggleDecorations.appendChild(decoTrack);
    toggleDecorations.appendChild(decoLabel);

    // 右侧控件自成一组、靠右——无论模式轴是否显示，页眉页脚开关与引用页始终贴右。
    var toolRight = document.createElement('div');
    toolRight.className = 'mef-reader-toolrow-right';
    toolRight.appendChild(toggleDecorations);
    toolRight.appendChild(current);
    toolRow.appendChild(modeSeg);
    toolRow.appendChild(toolRight);

    header.appendChild(headRow);
    header.appendChild(toolRow);

    var citationBar = document.createElement('div');
    citationBar.className = 'mef-reader-citation-bar';
    citationBar.hidden = true;

    var citationContext = document.createElement('span');
    citationContext.className = 'mef-reader-citation-context';
    citationContext.textContent = '选择引文格式';

    var copyFootnote = createButton(
      '复制中文脚注',
      'mef-reader-citation-action',
      'copy-footnote'
    );
    var copyGbt = createButton(
      '复制 GB/T 7714',
      'mef-reader-citation-action',
      'copy-gbt7714'
    );
    var clearSelection = createButton(
      '清除选区',
      'mef-reader-citation-clear',
      'clear-selection'
    );
    clearSelection.hidden = true;
    var alignmentActions = document.createElement('div');
    alignmentActions.className = 'mef-reader-alignment-actions';
    alignmentActions.hidden = true;
    citationBar.appendChild(citationContext);
    citationBar.appendChild(copyFootnote);
    citationBar.appendChild(copyGbt);
    citationBar.appendChild(alignmentActions);
    citationBar.appendChild(clearSelection);

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

    var readerBody = document.createElement('div');
    readerBody.className = 'mef-reader-body';

    var sourcePane = document.createElement('section');
    sourcePane.className = 'mef-reader-source-pane';
    var sourcePaneHeader = document.createElement('div');
    sourcePaneHeader.className = 'mef-reader-pane-header';
    sourcePaneHeader.hidden = true;
    var sourcePaneTitle = document.createElement('strong');
    sourcePaneTitle.className = 'mef-reader-pane-title';
    sourcePaneTitle.textContent = '当前版本';
    sourcePaneHeader.appendChild(sourcePaneTitle);
    sourcePane.appendChild(sourcePaneHeader);
    sourcePane.appendChild(viewport);

    var comparisonPane = document.createElement('section');
    comparisonPane.className = 'mef-reader-comparison-pane';
    comparisonPane.dataset.readerComparison = 'true';
    comparisonPane.hidden = true;
    var comparisonHeader = document.createElement('div');
    comparisonHeader.className = 'mef-reader-pane-header';
    // 对照版本：语言/格式落在这个选择器里（拆胶囊后的去处）。
    // 多译本时点开是真下拉，列出全部译本（语言·格式·版本名）供切换；单译本则只是纯标签。
    var comparisonVersionPicker = document.createElement('div');
    comparisonVersionPicker.className = 'mef-reader-version-picker';
    var comparisonTitle = createButton('', 'mef-reader-version-select', 'toggle-version-menu');
    comparisonTitle.setAttribute('aria-haspopup', 'listbox');
    comparisonTitle.setAttribute('aria-expanded', 'false');
    comparisonTitle.setAttribute('aria-label', '切换对照版本');
    var comparisonTitleText = document.createElement('span');
    comparisonTitleText.className = 'mef-reader-version-name';
    comparisonTitleText.textContent = '对齐版本';
    var comparisonTitleCaret = document.createElement('span');
    comparisonTitleCaret.className = 'mef-reader-version-caret';
    comparisonTitleCaret.setAttribute('aria-hidden', 'true');
    comparisonTitleCaret.textContent = '▾';
    comparisonTitle.appendChild(comparisonTitleText);
    comparisonTitle.appendChild(comparisonTitleCaret);
    var comparisonVersionMenu = document.createElement('div');
    comparisonVersionMenu.className = 'mef-reader-version-menu';
    comparisonVersionMenu.setAttribute('role', 'listbox');
    comparisonVersionMenu.hidden = true;
    comparisonVersionPicker.appendChild(comparisonTitle);
    comparisonVersionPicker.appendChild(comparisonVersionMenu);
    var comparisonNavigation = document.createElement('div');
    comparisonNavigation.className = 'mef-reader-comparison-navigation';
    // 自动跟随是一个开／关状态 → 用开关，而不是文字按钮。
    var comparisonFollow = createButton(
      '',
      'mef-reader-follow-switch is-active',
      'toggle-comparison-follow'
    );
    var comparisonFollowTrack = document.createElement('span');
    comparisonFollowTrack.className = 'mef-follow-track';
    comparisonFollowTrack.setAttribute('aria-hidden', 'true');
    var comparisonFollowLabel = document.createElement('span');
    comparisonFollowLabel.className = 'mef-follow-label';
    comparisonFollowLabel.textContent = '自动跟随';
    comparisonFollow.appendChild(comparisonFollowTrack);
    comparisonFollow.appendChild(comparisonFollowLabel);
    comparisonFollow.setAttribute('aria-pressed', 'true');
    comparisonFollow.setAttribute('aria-label', '自动跟随源文滚动');
    comparisonFollow.setAttribute('title', '开启后对照栏随源文自动滚动到对应段落');
    // 翻页是独立动作 → 一对 ‹ › 图标按钮。
    var comparisonPrevious = createButton(
      '‹',
      'mef-reader-pane-action mef-reader-pane-icon',
      'comparison-previous'
    );
    comparisonPrevious.setAttribute('aria-label', '向前翻');
    comparisonPrevious.setAttribute('title', '向前翻');
    var comparisonNext = createButton(
      '›',
      'mef-reader-pane-action mef-reader-pane-icon',
      'comparison-next'
    );
    comparisonNext.setAttribute('aria-label', '向后翻');
    comparisonNext.setAttribute('title', '向后翻');
    var comparisonClose = createButton(
      '收起',
      'mef-reader-pane-action',
      'close-comparison'
    );
    comparisonNavigation.appendChild(comparisonFollow);
    comparisonNavigation.appendChild(comparisonPrevious);
    comparisonNavigation.appendChild(comparisonNext);
    comparisonNavigation.appendChild(comparisonClose);
    comparisonHeader.appendChild(comparisonVersionPicker);
    comparisonHeader.appendChild(comparisonNavigation);
    var comparisonViewport = document.createElement('div');
    comparisonViewport.className = 'mef-reader-viewport mef-reader-comparison-viewport';
    comparisonViewport.tabIndex = 0;
    comparisonViewport.setAttribute('aria-label', '对齐版本正文');
    var comparisonContent = document.createElement('div');
    comparisonContent.className = 'mef-reader-content';
    comparisonViewport.appendChild(comparisonContent);
    // 低置信对齐提示：命中不精确（precise_highlight_available=false）时显式标注 + 校正入口。
    var comparisonWarn = createButton(
      '对齐可能不准 · 校正',
      'mef-reader-align-warn',
      'report-misalignment'
    );
    comparisonWarn.hidden = true;
    comparisonWarn.setAttribute(
      'title',
      '此处对齐为粗定位，可能锚到相邻段落或注释；点此了解如何校正'
    );
    comparisonPane.appendChild(comparisonHeader);
    comparisonPane.appendChild(comparisonWarn);
    comparisonPane.appendChild(comparisonViewport);

    readerBody.appendChild(sourcePane);
    readerBody.appendChild(comparisonPane);

    var loading = document.createElement('div');
    loading.className = 'mef-reader-loading';
    loading.setAttribute('role', 'status');
    loading.textContent = '正在载入文本…';
    loading.hidden = true;

    panel.appendChild(header);
    panel.appendChild(citationBar);
    panel.appendChild(alert);
    panel.appendChild(readerBody);
    panel.appendChild(loading);
    root.appendChild(backdrop);
    root.appendChild(panel);
    document.body.appendChild(root);

    root.addEventListener('click', function (event) {
      // 用 closest 兜住按钮内部的子元素（开关的标签/轨道、分段按钮的文字）。
      var trigger = event.target && event.target.closest
        ? event.target.closest('[data-reader-action]')
        : null;
      var action = trigger ? trigger.dataset.readerAction : '';
      // 点选择器以外任意处，收起版本下拉。
      if (action !== 'toggle-version-menu'
          && (!event.target.closest || !event.target.closest('.mef-reader-version-picker'))) {
        closeVersionMenu();
      }
      if (action === 'close') closeReader();
      if (action === 'toggle-citation') toggleCitationMenu();
      if (action === 'copy-footnote') copyCachedCitation('chinese');
      if (action === 'copy-gbt7714') copyCachedCitation('gb');
      if (action === 'locate-alignment') {
        locateInAlignedVersion(trigger.dataset.readerTarget || '');
      }
      if (action === 'reader-single') closeComparison();
      if (action === 'toggle-version-menu') toggleVersionMenu();
      if (action === 'pick-comparison-target') {
        closeVersionMenu();
        locateInAlignedVersion(trigger.dataset.readerTarget || '', sourceCenterRange());
      }
      if (action === 'report-misalignment') reportMisalignment();
      if (action === 'open-comparison') {
        locateInAlignedVersion(
          trigger.dataset.readerTarget || '',
          sourceCenterRange()
        );
      }
      if (action === 'toggle-comparison-follow') toggleComparisonFollow();
      if (action === 'close-comparison') closeComparison();
      if (action === 'comparison-previous') loadComparisonPrevious();
      if (action === 'comparison-next') loadComparisonNext();
      if (action === 'clear-selection') clearCitationRange();
      if (action === 'toggle-decorations') toggleDecorationVisibility();
    });
    viewport.addEventListener('mousedown', function () {
      state.selectionDragging = true;
    });
    viewport.addEventListener('keyup', scheduleSelectionCapture);
    viewport.addEventListener('keydown', handleReaderNavigationKey);
    viewport.addEventListener('scroll', scheduleScrollBoundaryCheck, {
      passive: true
    });
    viewport.addEventListener('scroll', scheduleComparisonFollow, {
      passive: true
    });

    state.elements = {
      root: root,
      panel: panel,
      title: title,
      eyebrow: eyebrow,
      subtitle: subtitle,
      current: current,
      toggleDecorations: toggleDecorations,
      modeSeg: modeSeg,
      citationBar: citationBar,
      citationContext: citationContext,
      copyFootnote: copyFootnote,
      copyGbt: copyGbt,
      alignmentActions: alignmentActions,
      clearSelection: clearSelection,
      alert: alert,
      readerBody: readerBody,
      sourcePane: sourcePane,
      sourcePaneHeader: sourcePaneHeader,
      sourcePaneTitle: sourcePaneTitle,
      viewport: viewport,
      content: content,
      comparisonPane: comparisonPane,
      comparisonWarn: comparisonWarn,
      comparisonTitle: comparisonTitle,
      comparisonTitleText: comparisonTitleText,
      comparisonVersionMenu: comparisonVersionMenu,
      comparisonPrevious: comparisonPrevious,
      comparisonNext: comparisonNext,
      comparisonFollow: comparisonFollow,
      comparisonViewport: comparisonViewport,
      comparisonContent: comparisonContent,
      loading: loading,
      close: close
    };
    return state.elements;
  }

  function citationTargetRange() {
    if (state.citationRange) return state.citationRange;
    var item = state.items.get(state.currentIndex);
    if (!item) return null;
    return {
      startIndex: state.currentIndex,
      endIndex: state.currentIndex,
      startAnchorId: itemAnchor(item, state.currentIndex),
      endAnchorId: itemAnchor(item, state.currentIndex),
      startOffset: 0,
      endOffset: codePointLength(item.text_raw || ''),
      startDisplay: backendPageDisplay(item),
      endDisplay: backendPageDisplay(item),
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
    state.elements.current.setAttribute(
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
    Array.from(state.elements.modeSeg.querySelectorAll('button')).forEach(
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
  function cleanReaderTitle(raw) {
    var t = String(raw || '').trim();
    var idx = t.indexOf(' (');
    if (idx > 0 && /[A-Za-z]|Library|z-?lib/i.test(t.slice(idx))) {
      t = t.slice(0, idx).trim();
    }
    return t || String(raw || '');
  }

  function readerByline(source) {
    if (!source) return '';
    var author = String(source.author || source.authors || source.creator || '').trim();
    var translator = String(source.translator || source.translators || '').trim();
    var parts = [];
    if (author) parts.push(author + ' 著');
    if (translator) parts.push(translator + ' 译');
    return parts.join(' · ');
  }

  var READER_LANGUAGE_LABELS = {
    'zh-Hans': '简体中文',
    'zh-Hant': '繁体中文',
    en: '英语',
    de: '德语',
    fr: '法语',
    ja: '日语',
    ko: '韩语'
  };

  // 紧凑版本标签：语言 · 格式（拆胶囊后落在选择器/栏头里）。
  function compactVersionLabel(languageCode, format) {
    var language = READER_LANGUAGE_LABELS[String(languageCode || '')] || '未识别语言';
    var fmt = String(format || '').toUpperCase();
    return language + (fmt ? ' · ' + fmt : '');
  }

  function shortAlignLabel(target) {
    return compactVersionLabel(target.language_code, target.source_format);
  }

  function sourceVersionLabel() {
    var s = state.source || {};
    if (!s.language_code) return cleanReaderTitle(state.title) || '当前版本';
    return compactVersionLabel(s.language_code, s.source_format || s.format);
  }

  // 低置信对齐的「校正」入口：先说明成因，并把当前源栏可见范围重新定位一次（多为最有效的自助校正）。
  function reportMisalignment() {
    notify('这处为粗定位，可能锚到相邻段落或注释。已按当前可见段落重新定位；如仍不准，可用页脚「在…中定位」精确校正。');
    if (state.comparison.open && state.comparison.targetSourceId) {
      locateInAlignedVersion(state.comparison.targetSourceId, sourceCenterRange());
    }
  }

  // 多译本时，右栏版本选择器点开是真下拉：列出全部对齐目标供切换。
  function renderVersionMenu() {
    if (!state.elements) return;
    var menu = state.elements.comparisonVersionMenu;
    menu.replaceChildren();
    var currentId = state.comparison.targetSourceId;
    (state.alignmentTargets || []).forEach(function (t) {
      var tid = String(t.source_file_id || '');
      var isCur = tid === currentId;
      var item = createButton(
        alignmentTargetDisplayLabel(t),
        'mef-reader-version-option' + (isCur ? ' is-current' : ''),
        'pick-comparison-target'
      );
      item.dataset.readerTarget = tid;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', isCur ? 'true' : 'false');
      menu.appendChild(item);
    });
  }

  function toggleVersionMenu() {
    if (!state.elements) return;
    if ((state.alignmentTargets || []).length < 2) return; // 单译本不弹菜单
    var menu = state.elements.comparisonVersionMenu;
    var willOpen = menu.hidden;
    if (willOpen) renderVersionMenu();
    menu.hidden = !willOpen;
    state.elements.comparisonTitle.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  }

  function closeVersionMenu() {
    if (!state.elements || state.elements.comparisonVersionMenu.hidden) return;
    state.elements.comparisonVersionMenu.hidden = true;
    state.elements.comparisonTitle.setAttribute('aria-expanded', 'false');
  }

  function alignmentTargetDisplayLabel(target) {
    var language = READER_LANGUAGE_LABELS[String(target.language_code || '')] || '未识别语言';
    var format = String(target.source_format || '').toUpperCase();
    var displayName = String(target.display_name || '另一版本');
    return language + (format ? ' · ' + format : '') + ' · ' + displayName;
  }

  function renderAlignmentActions() {
    if (!state.elements) return;
    state.elements.alignmentActions.replaceChildren();
    var seg = state.elements.modeSeg;
    seg.replaceChildren();
    var targets = state.alignmentTargets;
    targets.forEach(function (target) {
      var button = createButton(
        '在' + alignmentTargetDisplayLabel(target) + '中定位',
        'mef-reader-alignment-action',
        'locate-alignment'
      );
      button.dataset.readerTarget = String(target.source_file_id || '');
      state.elements.alignmentActions.appendChild(button);
    });
    // 模式轴：[阅读 | 双栏对照]。「双栏对照」默认对齐首个（或当前打开的）版本。
    if (targets.length) {
      var defaultTarget = state.comparison.open && state.comparison.targetSourceId
        ? state.comparison.targetSourceId
        : String(targets[0].source_file_id || '');
      var readBtn = createButton('阅读', 'mef-reader-mode-btn', 'reader-single');
      var compBtn = createButton('双栏对照', 'mef-reader-mode-btn', 'open-comparison');
      compBtn.dataset.readerTarget = defaultTarget;
      // 语言/格式落在提示里（拆开原来「双栏对照 · 英语 · EPUB · 英文」那颗胶囊）。
      compBtn.title = '双栏对照 · ' + alignmentTargetDisplayLabel(targets[0]);
      seg.appendChild(readBtn);
      seg.appendChild(compBtn);
      seg.hidden = false;
      syncModeSegment();
    } else {
      seg.hidden = true;
    }
    updateCitationControls();
  }

  // 让模式分段的选中态跟随「是否正在对照」。
  function syncModeSegment() {
    if (!state.elements || !state.elements.modeSeg || state.elements.modeSeg.hidden) return;
    var comparing = !!state.comparison.open;
    Array.from(state.elements.modeSeg.querySelectorAll('.mef-reader-mode-btn')).forEach(
      function (btn) {
        var active = btn.dataset.readerAction === 'open-comparison' ? comparing : !comparing;
        btn.classList.toggle('is-active', active);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      }
    );
  }

  async function loadAlignmentTargets(sourceId) {
    var serial = state.alignmentRequestSerial + 1;
    state.alignmentRequestSerial = serial;
    try {
      var response = await fetchFunction()(
        config.alignmentTargetsEndpoint + '?source_id=' + encodeURIComponent(sourceId),
        {headers: {'Accept': 'application/json'}}
      );
      var payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || '对齐版本读取失败');
      }
      if (serial !== state.alignmentRequestSerial || state.sourceId !== sourceId) {
        return;
      }
      state.alignmentTargets = Array.isArray(payload.targets) ? payload.targets : [];
      renderAlignmentActions();
    } catch (error) {
      if (serial !== state.alignmentRequestSerial) return;
      state.alignmentTargets = [];
      renderAlignmentActions();
      setAlert(
        error && error.message ? error.message : '对齐版本读取失败',
        'warning'
      );
    }
  }

  function alignmentTargetName(targetSourceId) {
    var target = state.alignmentTargets.find(function (candidate) {
      return String(candidate.source_file_id || '') === targetSourceId;
    });
    return target ? String(target.display_name || '') : '';
  }

  function nearestTextOffset(text, requestedOffset) {
    var characters = Array.from(String(text || ''));
    if (!characters.length) return null;
    var start = clampInteger(requestedOffset, 0, 0, characters.length - 1);
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
    var caretElement = elementForRangeNode(caretNode);
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
        utf16Offset = textOffsetWithin(body, caretNode, caretOffset);
      }
    }
    if (!item || !body) return null;
    var text = String(item.text_raw || '');
    if (!text) return null;
    var codePointOffset = utf16Offset === null
      ? Math.floor(codePointLength(text) / 2)
      : utf16ToCodePointIndex(text, utf16Offset);
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
      var ranges = state.resolvedHighlights.get(itemAnchor(item, index)) || [];
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

  function renderComparisonItem(item, absoluteIndex) {
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
      var ranges = state.comparison.highlights.get(anchorId) || [];
      appendHighlightedText(body, text, ranges, item.decoration_spans);
      if (ranges.length) article.classList.add('has-highlight');
    }
    article.appendChild(meta);
    article.appendChild(body);
    return article;
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
      'aria-pressed',
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
          index
        ));
      });
    state.elements.comparisonContent.replaceChildren(fragment);
    applyDecorationVisibility();
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
      var response = await fetchFunction()(
        config.endpoint + '?' + query.toString(),
        {headers: {'Accept': 'application/json'}}
      );
      var payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || '对照文本加载失败');
      }
      if (serial !== comparison.requestSerial || !comparison.open) return false;
      var items = responseItems(payload);
      var responseStart = clampInteger(payload.start, start, 0, Number.MAX_SAFE_INTEGER);
      comparison.items.clear();
      items.forEach(function (item, offset) {
        var position = itemPosition(item, responseStart + offset);
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
        setAlert(message, 'error');
        notify(message);
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
    var sourceHighlight = visibleSourceHighlightRange();
    var targetSourceId = String(payload.targetSourceId || payload.target_source_file_id || '');
    var changedTarget = comparison.targetSourceId !== targetSourceId;
    comparison.open = true;
    comparison.targetSourceId = targetSourceId;
    comparison.targetDisplayName = targetDisplayName ||
      String(payload.targetTitle || payload.target_title || '对齐版本');
    comparison.targetTitle = String(payload.targetTitle || payload.target_title || '');
    if (changedTarget) comparison.autoFollow = true;
    if (changedTarget) comparison.lastSourceRange = '';
    comparison.currentIndex = clampInteger(
      payload.targetIndex != null ? payload.targetIndex : payload.target_index,
      0,
      0,
      Number.MAX_SAFE_INTEGER
    );
    setComparisonHighlights(payload.pageMatchSpans || payload.page_match_spans || []);
    state.elements.panel.classList.add('is-comparing');
    state.elements.readerBody.classList.add('is-comparing');
    state.elements.comparisonPane.hidden = false;
    state.elements.sourcePaneHeader.hidden = false;
    state.elements.sourcePaneTitle.textContent = sourceVersionLabel();
    var targetObj = (state.alignmentTargets || []).filter(function (t) {
      return String(t.source_file_id || '') === targetSourceId;
    })[0];
    state.elements.comparisonTitleText.textContent = targetObj
      ? shortAlignLabel(targetObj)
      : comparison.targetDisplayName;
    state.elements.comparisonTitle.classList.toggle(
      'is-switchable',
      (state.alignmentTargets || []).length > 1
    );
    // 精确高亮不可用 = 粗定位 → 显式标注低置信，给「校正」入口。
    var lowConfidence = (payload.preciseHighlightAvailable != null
      ? payload.preciseHighlightAvailable
      : payload.precise_highlight_available) === false;
    comparison.lowConfidence = lowConfidence;
    state.elements.comparisonWarn.hidden = !lowConfidence;
    if (sourceHighlight) {
      positionSourceTarget(state.elements.content.querySelector(
        '[data-reader-index="' + sourceHighlight.startIndex + '"]'
      ));
    }
    updateComparisonControls();
    syncModeSegment();
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
    comparison.items.clear();
    comparison.highlights.clear();
    comparison.previousStart = null;
    comparison.nextStart = null;
    comparison.hasMore = false;
    comparison.loading = false;
    if (!state.elements) return;
    state.elements.panel.classList.remove('is-comparing');
    state.elements.readerBody.classList.remove('is-comparing');
    state.elements.comparisonPane.hidden = true;
    state.elements.sourcePaneHeader.hidden = true;
    state.elements.comparisonWarn.hidden = true;
    closeVersionMenu();
    state.elements.comparisonContent.replaceChildren();
    syncModeSegment();
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
    if (!comparison.open || !comparison.autoFollow) return;
    if (comparison.followTimer !== null) global.clearTimeout(comparison.followTimer);
    comparison.followTimer = global.setTimeout(function () {
      comparison.followTimer = null;
      var selection = sourceCenterRange();
      if (!selection || !comparison.open || !comparison.autoFollow) return;
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
      updateCitationControls();
    }
    try {
      var response = await fetchFunction()(config.alignmentLocateEndpoint, {
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
      return showComparison({
        targetSourceId: payload.target_source_file_id,
        targetTitle: payload.target_title,
        targetIndex: payload.target_index,
        pageMatchSpans: payload.page_match_spans || [],
        matchOffsetUnit: payload.match_offset_unit,
        preciseHighlightAvailable: payload.precise_highlight_available
      }, alignmentTargetName(targetSourceId));
    } catch (error) {
      if (serial !== state.comparison.locateSerial) return false;
      var message = error && error.message ? error.message : '跨版本定位失败';
      setAlert(message, automatic ? 'warning' : 'error');
      if (!automatic) notify(message);
      return false;
    } finally {
      if (!automatic) {
        state.alignmentLoading = false;
        updateCitationControls();
      }
    }
  }

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
        return utf16Total + clampInteger(
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
      startOffset: utf16ToCodePointIndex(startItem.text_raw || '', startUtf16),
      endOffset: utf16ToCodePointIndex(endItem.text_raw || '', endUtf16),
      startAnchorId: itemAnchor(startItem, startIndex),
      endAnchorId: itemAnchor(endItem, endIndex),
      startDisplay: backendPageDisplay(startItem),
      endDisplay: backendPageDisplay(endItem),
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
          setAlert(warning, 'warning');
          notify(warning);
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
      var response = await fetchFunction()(config.citationEndpoint, {
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
        setAlert(
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
      setAlert(message, 'error');
      notify(message);
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
      setAlert(warning, 'warning');
      notify(warning);
      return Promise.resolve(false);
    }
    var formats = target.citationPayload.citation_formats || {};
    var citation = String(formats[style] || '');
    return writeClipboard(citation).then(function () {
      notify(style === 'gb' ? 'GB/T 7714 引文已复制' : '中文脚注已复制');
      return true;
    }).catch(function (error) {
      var message = error && error.message
        ? error.message
        : '引文复制失败';
      setAlert(message, 'error');
      notify(message);
      return false;
    });
  }

  function truncateCodePoints(value, maximum) {
    return Array.from(String(value || '')).slice(0, maximum).join('');
  }

  function parseDeepLinkOffset(value, quote) {
    var match = /^(\d+)(?:-(\d+))?$/.exec(String(value || ''));
    if (!match) return null;
    var start = clampInteger(match[1], 0, 0, Number.MAX_SAFE_INTEGER);
    var end = match[2] == null
      ? start + codePointLength(quote)
      : clampInteger(match[2], start, start, Number.MAX_SAFE_INTEGER);
    return end > start ? {start: start, end: end} : null;
  }

  function parseReaderDeepLink(locationValue) {
    var locationObject = locationValue || global.location;
    if (!locationObject) return null;
    var pathname = String(locationObject.pathname || '');
    if (pathname !== '/reader' && pathname !== '/reader/') return null;
    var search = String(locationObject.search || '');
    if (search.length > 1024) return null;
    var params = new URLSearchParams(search);
    var unknownParameter = false;
    params.forEach(function (_value, key) {
      if (!['source', 'page', 'off', 'h', 'q'].includes(key)) {
        unknownParameter = true;
      }
    });
    if (
      unknownParameter ||
      params.getAll('source').length !== 1 ||
      params.getAll('page').length !== 1 ||
      params.getAll('off').length > 1 ||
      params.getAll('h').length > 1 ||
      params.getAll('q').length > 1
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
    var targetIndex = inferIndexFromAnchor(anchorId);
    if (targetIndex === null) return null;
    var hashValue = String(params.get('h') || '');
    if (hashValue && !/^[0-9a-f]{16}$/i.test(hashValue)) return null;
    var pageTextHash = hashValue;
    var rawQuote = String(params.get('q') || '');
    if (codePointLength(rawQuote) > 50) return null;
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
    if (
      !state.open ||
      !global.history ||
      typeof global.history.replaceState !== 'function' ||
      state.lastHistoryAnchor === anchorId
    ) {
      return;
    }
    var params = new URLSearchParams();
    params.set('source', state.sourceId);
    params.set('page', anchorId);
    var linkRange = deepLinkRange(anchorId);
    var quote = '';
    var pageTextHash = '';
    var spans = [];
    if (linkRange) {
      var range = linkRange.range;
      var start = clampInteger(range.start, 0, 0, Number.MAX_SAFE_INTEGER);
      var end = clampInteger(range.end, start, start, Number.MAX_SAFE_INTEGER);
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
    var url = '/reader?' + params.toString();
    if (url.length > 1024) return;
    global.history.replaceState(
      {meFinderReader: true, sourceId: state.sourceId, anchorId: anchorId},
      '',
      url
    );
    state.lastHistoryAnchor = anchorId;
    state.lastDeepLink = url;
    state.lastSession = {
      sourceId: state.sourceId,
      title: state.title,
      targetIndex: index,
      anchorId: anchorId,
      pageMatchSpans: spans,
      matchOffsetUnit: 'unicode_codepoint',
      matchQuote: quote,
      preciseHighlightAvailable: spans.length > 0,
      fromDeepLink: true
    };
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
    return openReader(Object.assign({}, options, {restoringSession: true}));
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
    var anchorId = itemAnchor(item, index);
    var currentChanged = state.currentAnchorId !== anchorId;
    state.currentIndex = index;
    state.currentAnchorId = anchorId;
    var pageLabel = backendPageDisplay(item);
    state.elements.current.textContent = pageLabel;
    state.elements.current.title = '点击复制此页或当前选区的引文';
    updateCitationControls();
    if (currentChanged) {
      scheduleReaderDeepLink(item, index, anchorId);
      scheduleComparisonFollow();
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
    if (state.loading || !state.open || selectionBlocksWindowShift()) return;
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
      selectionBlocksWindowShift() ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey
    ) {
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      goTo({targetIndex: 0});
    } else if (event.key === 'End' && state.lastPosition !== null) {
      event.preventDefault();
      goTo({targetIndex: state.lastPosition});
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
        selectionBlocksWindowShift()
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

  function applyDecorationVisibility() {
    var show = !!state.showDecorations;
    [state.elements.content, state.elements.comparisonContent].forEach(
      function (root) {
        if (root && root.classList) {
          root.classList.toggle('mef-show-decorations', show);
        }
      }
    );
    var button = state.elements.toggleDecorations;
    if (button) {
      button.classList.toggle('is-active', show);
      button.setAttribute('aria-pressed', show ? 'true' : 'false');
      button.setAttribute('aria-label', show ? '隐藏页眉页脚' : '显示页眉页脚');
    }
  }

  function toggleDecorationVisibility() {
    state.showDecorations = !state.showDecorations;
    applyDecorationVisibility();
  }

  function mergeCodePointRanges(ranges, codePointCount) {
    var normalized = (ranges || [])
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
  function appendHighlightedText(container, text, ranges, decorations) {
    var codePoints = Array.from(text);
    var highlights = mergeCodePointRanges(ranges, codePointLength(text));
    var decoRanges = (decorations || [])
      .map(function (span) {
        var range = {
          start: clampInteger(span.start, 0, 0, codePointLength(text)),
          end: clampInteger(span.end, 0, 0, codePointLength(text)),
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
    if (!highlights.length && !decoRanges.length) {
      container.appendChild(document.createTextNode(text));
      return;
    }

    var codePointCount = codePointLength(text);
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
        codePointToUtf16Index(text, start),
        codePointToUtf16Index(text, end)
      );
      if (!slice) continue;
      var node = document.createTextNode(slice);
      if (isHighlighted(start, end)) {
        var mark = document.createElement('mark');
        mark.className = 'mef-reader-highlight';
        mark.appendChild(node);
        node = mark;
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
      var foundStart = utf16ToCodePointIndex(text, foundUtf16);
      var distance = Math.abs(foundStart - savedCodePointStart);
      if (!best || distance < best.distance) {
        best = {
          start: foundStart,
          end: foundStart + codePointLength(quote),
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
      setAlert(state.hashRecoveryNotice, 'warning');
      return recovered;
    }
    state.hashRecoveryNotice = '文本内容已变化，已跳转到相应页，但无法精确高亮';
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
      state.resolvedHighlights.set(anchorId, ranges);
      appendHighlightedText(body, text, ranges, item.decoration_spans);
      if (ranges.length) article.classList.add('has-highlight');
    }

    article.appendChild(meta);
    article.appendChild(body);
    return article;
  }

  function renderWindow(scrollAnchorId) {
    var elements = ensureDom();
    disconnectObservers();
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
    var target = targetAnchor ? findAnchorNode(targetAnchor) : null;
    if (!target) {
      target = elements.content.querySelector(
        '[data-reader-index="' + state.currentIndex + '"]'
      );
    }
    if (target) {
      positionSourceTarget(target);
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
      var position = itemPosition(item, responseStart + offset);
      return itemAnchor(item, position) === anchorId;
    });
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
      if (
        mode === 'replace' &&
        scrollAnchorId &&
        !responseContainsAnchor(items, responseStart, scrollAnchorId)
      ) {
        throw new Error('链接锚点不属于该文献或已失效');
      }
      state.total = clampInteger(
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
        state.elements.title.textContent = cleanReaderTitle(state.title) || '文献阅读';
        var byline = readerByline(state.source);
        state.elements.subtitle.textContent = byline;
        state.elements.subtitle.hidden = !byline;
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
        setAlert('这本文献暂时没有可显示的结构化文本', 'info');
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

  function configure(options) {
    options = options || {};
    if (options.endpoint) config.endpoint = String(options.endpoint);
    if (options.citationEndpoint) {
      config.citationEndpoint = String(options.citationEndpoint);
    }
    if (options.alignmentTargetsEndpoint) {
      config.alignmentTargetsEndpoint = String(options.alignmentTargetsEndpoint);
    }
    if (options.alignmentLocateEndpoint) {
      config.alignmentLocateEndpoint = String(options.alignmentLocateEndpoint);
    }
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
    options = options || parseReaderDeepLink(global.location) || state.lastSession || {};
    var sourceId = String(options.sourceId || options.source_id || '');
    if (!sourceId) throw new Error('缺少文献标识，无法打开结构化文本');
    ensureDom();
    if (state.comparison.open) closeComparison();

    if (options.config) configure(options.config);
    if (!state.open) {
      state.restoreFocus = document.activeElement;
      state.originalUrl = options.restoringSession
        ? (state.originalUrl || '/')
        : ordinaryUrlBeforeReader();
    }
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
    state.lastPosition = null;
    state.windowStart = 0;
    state.windowEnd = 0;
    state.hasPrevious = false;
    state.hasMore = false;
    state.previousStart = null;
    state.nextStart = null;
    state.currentAnchorId = '';
    state.lastHistoryAnchor = '';
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
    state.alignmentLoading = false;
    state.currentIndex = resolveTargetIndex(options);
    prepareHighlights(options);

    state.elements.title.textContent = cleanReaderTitle(state.title) || '文献阅读';
    state.elements.eyebrow.textContent = '正在阅读';
    state.elements.subtitle.textContent = '';
    state.elements.subtitle.hidden = true;
    state.elements.current.textContent = '正在载入…';
    state.elements.current.disabled = true;
    state.elements.root.hidden = false;
    state.elements.root.setAttribute('aria-hidden', 'false');
    document.body.classList.add('mef-reader-open');
    setAlert('', 'info');
    renderAlignmentActions();
    updateCitationControls();
    loadAlignmentTargets(sourceId);

    if (!state.preciseHighlight && (
      options.preciseHighlightAvailable === false ||
      options.precise_highlight_available === false ||
      options.legacyIndex === true ||
      options.legacy_index === true
    )) {
      var legacyMessage = '此文献使用旧索引，已跳转到相应位置，但无法精确高亮。重新导入后可启用精确定位';
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
        positionSourceTarget(local);
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
    flushPendingReaderDeepLink();
    closeComparison();
    state.open = false;
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
    disconnectObservers();
    state.items.clear();
    state.highlights.clear();
    state.resolvedHighlights.clear();
    state.citationRange = null;
    state.alignmentTargets = [];
    state.alignmentLoading = false;
    state.selectionDragging = false;
    state.citationMenuOpen = false;
    state.elements.content.replaceChildren();
    state.elements.root.hidden = true;
    state.elements.root.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('mef-reader-open');
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
      lastDeepLink: state.lastDeepLink
    };
  }

  document.addEventListener('keydown', function (event) {
    if (state.open && event.key === 'Escape') closeReader();
  });
  document.addEventListener('mouseup', function () {
    if (state.open && state.selectionDragging) scheduleSelectionCapture();
  });
  global.addEventListener('popstate', function () {
    var deepLink = parseReaderDeepLink(global.location);
    if (deepLink && !state.open) {
      restoreReaderLocation();
    } else if (!deepLink && state.open) {
      closeReader();
    }
  });

  global.MEFinderReader = Object.freeze({
    open: openReader,
    openForSearchResult: openForSearchResult,
    close: closeReader,
    goTo: goTo,
    restore: restoreReaderLocation,
    copyCitation: copyCachedCitation,
    configure: configure,
    destroy: destroy,
    isOpen: function () { return state.open; },
    getState: getState,
    codePointToUtf16Index: codePointToUtf16Index
  });

  function restoreInitialDeepLink() {
    if (!state.open && parseReaderDeepLink(global.location)) {
      restoreReaderLocation();
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restoreInitialDeepLink, {once: true});
  } else {
    global.setTimeout(restoreInitialDeepLink, 0);
  }
}(window));
