(function (global) {
  'use strict';
  /*
   * The structured-text reader deliberately lives outside app.js.  Its public
   * surface is intentionally small so the search UI can opt into it without
   * changing the user's PDFKit / WebView2 / system-reader preference.
   */
  var DEFAULTS = {
    endpoint: '/api/document/pages',
    outlineEndpoint: '/api/document/outline',
    citationEndpoint: '/api/document/citation',
    alignmentTargetsEndpoint: '/api/text-alignments/targets',
    alignmentLocateEndpoint: '/api/text-alignments/locate',
    alignmentStartEndpoint: '/api/text-alignments/start',
    alignmentCancelEndpoint: '/api/text-alignments/cancel',
    alignmentModelsEndpoint: '/api/text-alignment/models',
    preferencesEndpoint: '/api/preferences',
    groupsEndpoint: '/api/document-groups',
    overviewEndpoint: '/api/translation-works/overview',
    currentJobEndpoint: '/api/text-alignments/current',
    linksEndpoint: '/api/text-alignments/links',
    reviewCandidatesEndpoint: '/api/text-alignments/review-candidates',
    correctionSaveEndpoint: '/api/text-alignments/corrections/save',
    correctionDeferEndpoint: '/api/text-alignments/corrections/defer',
    readingPositionEndpoint: '/api/translation-works/reading-position',
    batchSize: 20,
    radiusBatches: 1,
    estimatedItemHeight: 360
  };

  var config = {
    endpoint: DEFAULTS.endpoint,
    outlineEndpoint: DEFAULTS.outlineEndpoint,
    citationEndpoint: DEFAULTS.citationEndpoint,
    alignmentTargetsEndpoint: DEFAULTS.alignmentTargetsEndpoint,
    alignmentLocateEndpoint: DEFAULTS.alignmentLocateEndpoint,
    alignmentStartEndpoint: DEFAULTS.alignmentStartEndpoint,
    alignmentCancelEndpoint: DEFAULTS.alignmentCancelEndpoint,
    alignmentModelsEndpoint: DEFAULTS.alignmentModelsEndpoint,
    preferencesEndpoint: DEFAULTS.preferencesEndpoint,
    groupsEndpoint: DEFAULTS.groupsEndpoint,
    overviewEndpoint: DEFAULTS.overviewEndpoint,
    currentJobEndpoint: DEFAULTS.currentJobEndpoint,
    linksEndpoint: DEFAULTS.linksEndpoint,
    reviewCandidatesEndpoint: DEFAULTS.reviewCandidatesEndpoint,
    correctionSaveEndpoint: DEFAULTS.correctionSaveEndpoint,
    correctionDeferEndpoint: DEFAULTS.correctionDeferEndpoint,
    readingPositionEndpoint: DEFAULTS.readingPositionEndpoint,
    batchSize: DEFAULTS.batchSize,
    radiusBatches: DEFAULTS.radiusBatches,
    estimatedItemHeight: DEFAULTS.estimatedItemHeight,
    fetch: null,
    notify: null,
    openExternal: null,
    onClose: null,
    // 主窗口宿主提供的能力；独立阅读窗口里没有这些回调，对应入口随之隐藏。
    onOpenChange: null,
    // 人工校正立即改变作品页的「N 处待检查」，宿主据此失效自己的缓存。
    onAlignmentDataChanged: null,
    onManageWork: null,
    onInstallComponent: null,
    onFindInWork: null,
    openInNewWindow: null,
    canOpenInNewWindow: null
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
    alignmentSourceLanguage: '',
    alignmentGroupId: '',
    alignmentLoading: false,
    alignmentRequestSerial: 0,
    defaultComparisonTarget: '',
    returnLabel: '',
    work: {groupId: '', title: '', baseId: '', members: [], pairs: {}, languages: {}},
    workRequestSerial: 0,
    availability: 'unknown',
    pendingCompareWith: '',
    openMenu: '',
    outline: {items: null, loading: false, error: ''},
    outlineJumpSerial: 0,
    outlineNavigating: false,
    links: null,
    linkRequestSerial: 0,
    linkedRanges: new Map(),
    selectedLinkKey: '',
    review: null,
    flagLayoutTimer: null,
    positionTimer: null,
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
      indexHighlights: new Map(),
      lowConfidence: false,
      currentIndex: 0,
      previousStart: null,
      nextStart: null,
      hasMore: false,
      loading: false
    },
    lastSession: null,
    lastHistoryCompare: '',
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

  var alignmentJobs = global.MEFinderAlignmentJobs;
  var r = global.__MEFinderReaderInternal = {state: state, config: config, DEFAULTS: DEFAULTS, alignmentJobs: alignmentJobs};

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
    r.ensureDom();
    var alert = state.elements.alert;
    // 后端错误句末常带句号；界面文案句末不加句号（DESIGN.md §5）。
    alert.textContent = String(message || '').replace(/。$/, '');
    alert.hidden = !message;
    alert.dataset.kind = kind || 'info';
  }

  function fetchFunction() {
    var candidate = config.fetch || global.MEFinderApi.fetch;
    if (typeof candidate !== 'function') {
      throw new Error('当前环境不支持读取结构化文本');
    }
    return candidate.bind(global);
  }


  r.clampInteger = clampInteger;
  r.codePointToUtf16Index = codePointToUtf16Index;
  r.codePointLength = codePointLength;
  r.utf16ToCodePointIndex = utf16ToCodePointIndex;
  r.domSafeId = domSafeId;
  r.itemAnchor = itemAnchor;
  r.itemPosition = itemPosition;
  r.inferIndexFromAnchor = inferIndexFromAnchor;
  r.resolveTargetIndex = resolveTargetIndex;
  r.backendPageDisplay = backendPageDisplay;
  r.notify = notify;
  r.setAlert = setAlert;
  r.fetchFunction = fetchFunction;
}(window));
