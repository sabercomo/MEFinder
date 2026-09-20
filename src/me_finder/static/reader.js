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
    alignmentStatusEndpoint: '/api/text-alignments/status',
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
    alignmentStatusEndpoint: DEFAULTS.alignmentStatusEndpoint,
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
    generation: null,
    pollingJobId: '',
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
    // 后端错误句末常带句号；界面文案句末不加句号（DESIGN.md §5）。
    alert.textContent = String(message || '').replace(/。$/, '');
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

  function createIcon(pathData, size, strokeWidth) {
    var ns = 'http://www.w3.org/2000/svg';
    var icon = document.createElementNS(ns, 'svg');
    icon.setAttribute('viewBox', '0 0 20 20');
    icon.setAttribute('width', String(size || 16));
    icon.setAttribute('height', String(size || 16));
    icon.setAttribute('fill', 'none');
    icon.setAttribute('stroke', 'currentColor');
    icon.setAttribute('stroke-width', String(strokeWidth || 1.8));
    icon.setAttribute('stroke-linecap', 'round');
    icon.setAttribute('stroke-linejoin', 'round');
    icon.setAttribute('aria-hidden', 'true');
    String(pathData).split('|').forEach(function (d) {
      var path = document.createElementNS(ns, 'path');
      path.setAttribute('d', d);
      icon.appendChild(path);
    });
    return icon;
  }

  var ICON_BACK = 'M12.5 4.5 7 10l5.5 5.5';
  var ICON_CHEVRON = 'm6 8 4 4 4-4';
  var ICON_SWAP = 'M6 6h10l-3-3|M14 14H4l3 3';
  var ICON_CLOSE = 'M5.5 5.5l9 9|M14.5 5.5l-9 9';
  var ICON_MORE = 'M4.5 10h.01|M10 10h.01|M15.5 10h.01';

  function createIconButton(label, action, pathData) {
    var button = createButton('', 'mef-reader-icon-btn', action);
    button.setAttribute('aria-label', label);
    button.title = label;
    button.appendChild(createIcon(pathData, 16));
    return button;
  }

  // 自绘下拉：触发器 aria-haspopup=listbox，菜单 role=listbox/option；
  // 键盘由 handleMenuKeydown 统一处理（上下键、Home/End、Enter/Space、Escape）。
  function createDropdown(key, triggerClass, menuRole) {
    var wrap = document.createElement('div');
    wrap.className = 'mef-reader-dd';
    wrap.dataset.readerMenu = key;
    var trigger = createButton('', 'mef-reader-dd-trigger ' + (triggerClass || ''), 'toggle-menu');
    trigger.dataset.readerMenuKey = key;
    trigger.setAttribute('aria-haspopup', menuRole || 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    var value = document.createElement('span');
    value.className = 'mef-reader-dd-value';
    trigger.appendChild(value);
    var menu = document.createElement('div');
    menu.className = 'mef-reader-menu';
    menu.setAttribute('role', menuRole || 'listbox');
    menu.id = 'mef-reader-menu-' + key;
    menu.hidden = true;
    trigger.setAttribute('aria-controls', menu.id);
    wrap.appendChild(trigger);
    wrap.appendChild(menu);
    return {wrap: wrap, trigger: trigger, value: value, menu: menu};
  }

  function ensureDom() {
    if (state.elements && state.elements.root.isConnected) return state.elements;

    var isReaderWindow = document.documentElement.dataset.readerWindow === 'true';
    var root = document.createElement('div');
    root.className = 'mef-structured-reader';
    root.id = 'mef-structured-reader';
    root.hidden = true;
    root.setAttribute('aria-hidden', 'true');

    var panel = document.createElement('section');
    panel.className = 'mef-reader-panel';
    panel.setAttribute('aria-labelledby', 'mef-reader-title');

    var header = document.createElement('header');
    header.className = 'mef-reader-header';

    // ── 工具栏：返回 · 左栏版本 ·（添加对照版本 | 交换 · 右栏版本 · 关闭对照）… 跟随滚动 · 复制引文 · ⋯
    var toolbar = document.createElement('div');
    toolbar.className = 'mef-reader-toolbar';
    toolbar.setAttribute('role', 'toolbar');
    toolbar.setAttribute('aria-label', '阅读工具');

    var back = createButton('', 'mef-reader-back', 'close');
    back.appendChild(createIcon(ICON_BACK, 16));
    var backLabel = document.createElement('span');
    backLabel.className = 'mef-reader-back-label';
    backLabel.textContent = '返回';
    back.appendChild(backLabel);
    back.hidden = isReaderWindow;

    var leftPicker = createDropdown('left', 'is-version');
    leftPicker.trigger.setAttribute('aria-label', '左栏版本');
    leftPicker.trigger.appendChild(createIcon(ICON_CHEVRON, 14));

    var addPicker = createDropdown('add', 'is-add');
    addPicker.value.textContent = '添加对照版本';
    addPicker.wrap.hidden = true;

    var swap = createIconButton('交换左右栏', 'swap-comparison', ICON_SWAP);
    swap.hidden = true;
    var rightPicker = createDropdown('right', 'is-version');
    rightPicker.trigger.setAttribute('aria-label', '右栏版本');
    rightPicker.trigger.appendChild(createIcon(ICON_CHEVRON, 14));
    rightPicker.wrap.hidden = true;
    var closeCompare = createIconButton('关闭对照', 'close-comparison', ICON_CLOSE);
    closeCompare.hidden = true;

    var spacer = document.createElement('span');
    spacer.className = 'mef-reader-toolbar-spacer';

    // 跟随滚动：可立即切换的二元状态 → 开关。
    var comparisonFollow = createButton('', 'mef-reader-follow-switch is-active', 'toggle-comparison-follow');
    comparisonFollow.setAttribute('role', 'switch');
    comparisonFollow.setAttribute('aria-checked', 'true');
    var comparisonFollowTrack = document.createElement('span');
    comparisonFollowTrack.className = 'mef-follow-track';
    comparisonFollowTrack.setAttribute('aria-hidden', 'true');
    var comparisonFollowLabel = document.createElement('span');
    comparisonFollowLabel.className = 'mef-follow-label';
    comparisonFollowLabel.textContent = '跟随滚动';
    comparisonFollow.appendChild(comparisonFollowTrack);
    comparisonFollow.appendChild(comparisonFollowLabel);
    comparisonFollow.setAttribute('aria-label', '跟随滚动');
    comparisonFollow.title = '开启后右栏随左栏滚动到对应段落';
    comparisonFollow.hidden = true;

    var cite = createButton('复制引文', 'mef-reader-tool-btn', 'toggle-citation');
    cite.setAttribute('aria-haspopup', 'true');
    cite.setAttribute('aria-expanded', 'false');

    var morePicker = createDropdown('more', 'is-icon', 'menu');
    morePicker.trigger.setAttribute('aria-label', '更多');
    morePicker.trigger.title = '更多';
    morePicker.trigger.appendChild(createIcon(ICON_MORE, 18, 3));
    morePicker.menu.classList.add('is-right');

    var close = createIconButton('关闭阅读窗口', 'close', ICON_CLOSE);
    close.classList.add('mef-reader-close');
    close.hidden = !isReaderWindow;

    var outlinePicker = createDropdown('outline', 'mef-reader-outline-trigger');
    outlinePicker.value.textContent = '目录';
    outlinePicker.trigger.setAttribute('aria-label', '章节目录');
    outlinePicker.menu.setAttribute('aria-label', '一、二级章节');
    outlinePicker.menu.classList.add('mef-reader-outline-menu');

    toolbar.appendChild(back);
    toolbar.appendChild(outlinePicker.wrap);
    toolbar.appendChild(leftPicker.wrap);
    toolbar.appendChild(addPicker.wrap);
    toolbar.appendChild(swap);
    toolbar.appendChild(rightPicker.wrap);
    toolbar.appendChild(closeCompare);
    toolbar.appendChild(spacer);
    toolbar.appendChild(comparisonFollow);
    toolbar.appendChild(cite);
    toolbar.appendChild(morePicker.wrap);
    toolbar.appendChild(close);
    header.appendChild(toolbar);

    // 跳到页：⋯ 菜单打开的小表单，按当前栏的 PDF 页序或段落序号跳转（不按印刷页码猜）。
    var jumpForm = document.createElement('form');
    jumpForm.className = 'mef-reader-jump';
    jumpForm.hidden = true;
    var jumpLabel = document.createElement('label');
    jumpLabel.className = 'mef-reader-jump-label';
    jumpLabel.setAttribute('for', 'mef-reader-jump-input');
    jumpLabel.textContent = '跳到 PDF 第';
    var jumpInput = document.createElement('input');
    jumpInput.id = 'mef-reader-jump-input';
    jumpInput.className = 'mef-reader-jump-input';
    jumpInput.type = 'number';
    jumpInput.min = '1';
    jumpInput.step = '1';
    jumpInput.required = true;
    var jumpUnit = document.createElement('span');
    jumpUnit.className = 'mef-reader-jump-unit';
    jumpUnit.textContent = '页';
    var jumpSubmit = createButton('跳转', 'mef-reader-tool-btn', 'jump-submit');
    jumpSubmit.type = 'submit';
    var jumpCancel = createButton('取消', 'mef-reader-tool-btn is-quiet', 'jump-cancel');
    jumpForm.appendChild(jumpLabel);
    jumpForm.appendChild(jumpInput);
    jumpForm.appendChild(jumpUnit);
    jumpForm.appendChild(jumpSubmit);
    jumpForm.appendChild(jumpCancel);
    header.appendChild(jumpForm);

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

    // 对照说明条：间接关联 / 需重新对齐 / 粗定位，带就地动作。
    var comparisonNotice = document.createElement('div');
    comparisonNotice.className = 'mef-reader-notice';
    comparisonNotice.hidden = true;
    var comparisonNoticeText = document.createElement('span');
    comparisonNoticeText.className = 'mef-reader-notice-text';
    var comparisonNoticeAction = createButton('', 'mef-reader-tool-btn', 'generate-alignment');
    comparisonNotice.appendChild(comparisonNoticeText);
    comparisonNotice.appendChild(comparisonNoticeAction);

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
    var heading = document.createElement('div');
    heading.className = 'mef-reader-heading';
    var eyebrow = document.createElement('span');
    eyebrow.className = 'mef-reader-eyebrow';
    eyebrow.textContent = '正在阅读';
    var title = document.createElement('h2');
    title.id = 'mef-reader-title';
    title.className = 'mef-reader-title';
    title.textContent = '文献阅读';
    var subtitle = document.createElement('span');
    subtitle.className = 'mef-reader-subtitle';
    subtitle.hidden = true;
    heading.appendChild(eyebrow);
    heading.appendChild(title);
    heading.appendChild(subtitle);
    var current = createButton('正在载入…', 'mef-reader-current', 'toggle-citation');
    current.setAttribute('aria-live', 'polite');
    current.title = '当前页码，点击复制此页或当前选区的引文';
    sourcePaneHeader.appendChild(heading);
    sourcePaneHeader.appendChild(current);
    sourcePane.appendChild(sourcePaneHeader);
    sourcePane.appendChild(viewport);

    var comparisonPane = document.createElement('section');
    comparisonPane.className = 'mef-reader-comparison-pane';
    comparisonPane.dataset.readerComparison = 'true';
    comparisonPane.hidden = true;
    var comparisonHeader = document.createElement('div');
    comparisonHeader.className = 'mef-reader-pane-header';
    var comparisonHeading = document.createElement('div');
    comparisonHeading.className = 'mef-reader-heading';
    var comparisonTitleText = document.createElement('span');
    comparisonTitleText.className = 'mef-reader-pane-title';
    comparisonTitleText.textContent = '对照版本';
    comparisonHeading.appendChild(comparisonTitleText);
    var comparisonNavigation = document.createElement('div');
    comparisonNavigation.className = 'mef-reader-comparison-navigation';
    var comparisonPrevious = createButton('‹', 'mef-reader-pane-action mef-reader-pane-icon', 'comparison-previous');
    comparisonPrevious.setAttribute('aria-label', '向前翻');
    comparisonPrevious.title = '向前翻';
    var comparisonNext = createButton('›', 'mef-reader-pane-action mef-reader-pane-icon', 'comparison-next');
    comparisonNext.setAttribute('aria-label', '向后翻');
    comparisonNext.title = '向后翻';
    comparisonNavigation.appendChild(comparisonPrevious);
    comparisonNavigation.appendChild(comparisonNext);
    comparisonHeader.appendChild(comparisonHeading);
    comparisonHeader.appendChild(comparisonNavigation);
    var comparisonViewport = document.createElement('div');
    comparisonViewport.className = 'mef-reader-viewport mef-reader-comparison-viewport';
    comparisonViewport.tabIndex = 0;
    comparisonViewport.setAttribute('aria-label', '对照版本正文');
    var comparisonContent = document.createElement('div');
    comparisonContent.className = 'mef-reader-content';
    comparisonViewport.appendChild(comparisonContent);

    // 选中一个尚未对齐的版本时，右栏原位显示生成入口；左栏照常阅读。
    var pending = document.createElement('div');
    pending.className = 'mef-reader-pending';
    pending.hidden = true;
    var pendingTitle = document.createElement('h3');
    pendingTitle.className = 'mef-reader-pending-title';
    var pendingText = document.createElement('p');
    pendingText.className = 'mef-reader-pending-text';
    var pendingAction = createButton('生成对齐', 'mef-reader-tool-btn is-primary', 'generate-alignment');
    pending.appendChild(pendingTitle);
    pending.appendChild(pendingText);
    pending.appendChild(pendingAction);

    comparisonPane.appendChild(comparisonHeader);
    comparisonPane.appendChild(comparisonViewport);
    comparisonPane.appendChild(pending);

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
    panel.appendChild(comparisonNotice);
    panel.appendChild(readerBody);
    panel.appendChild(loading);
    root.appendChild(panel);
    (isReaderWindow ? document.body : (document.querySelector('.main-area') || document.body)).appendChild(root);

    root.addEventListener('click', function (event) {
      // 用 closest 兜住按钮内部的子元素（开关的标签/轨道、图标）。
      var trigger = event.target && event.target.closest
        ? event.target.closest('[data-reader-action]')
        : null;
      var action = trigger ? trigger.dataset.readerAction : '';
      if (action !== 'toggle-menu' && (!event.target.closest || !event.target.closest('.mef-reader-menu'))) {
        closeMenus();
      }
      if (!event.target.closest || !event.target.closest('.mef-reader-review')) closeReviewPopover();
      if (action === 'close') closeReader();
      if (action === 'jump-chapter') jumpToChapter(Number(trigger.dataset.readerChapter));
      if (action === 'retry-outline') loadOutline();
      if (action === 'toggle-menu') toggleMenu(trigger.dataset.readerMenuKey);
      if (action === 'pick-left') pickLeftVersion(trigger.dataset.readerTarget || '');
      if (action === 'pick-right' || action === 'add-comparison') {
        closeMenus();
        openComparisonWith(trigger.dataset.readerTarget || '');
      }
      if (action === 'manage-work') runHost('onManageWork', state.work.groupId);
      if (action === 'find-in-work') runHost('onFindInWork', state.work.groupId);
      if (action === 'open-jump') openJumpForm();
      if (action === 'jump-cancel') closeJumpForm();
      if (action === 'open-new-window') openInNewWindow();
      if (action === 'return-main') returnToMainWindow();
      if (action === 'install-component') runHost('onInstallComponent');
      if (action === 'swap-comparison') swapComparison();
      if (action === 'toggle-citation') toggleCitationMenu();
      if (action === 'copy-footnote') copyCachedCitation('chinese');
      if (action === 'copy-gbt7714') copyCachedCitation('gb');
      if (action === 'locate-alignment') {
        locateInAlignedVersion(trigger.dataset.readerTarget || '');
      }
      if (action === 'generate-alignment') startComparisonAlignment(trigger.dataset.readerForce === 'true');
      if (action === 'cancel-alignment') cancelComparisonAlignment();
      if (action === 'toggle-comparison-follow') toggleComparisonFollow();
      if (action === 'close-comparison') closeComparison();
      if (action === 'comparison-previous') loadComparisonPrevious();
      if (action === 'comparison-next') loadComparisonNext();
      if (action === 'clear-selection') clearCitationRange();
      if (action === 'toggle-decorations') { closeMenus(); toggleDecorationVisibility(); }
      if (action === 'review-link') openReviewPopover(trigger);
      if (!action && event.target.closest && event.target.closest('.mef-reader-source-pane .mef-reader-item-text')) {
        selectLinkAtClick(event);
      }
    });
    root.addEventListener('keydown', handleMenuKeydown);
    jumpForm.addEventListener('submit', function (event) {
      event.preventDefault();
      submitJumpForm();
    });
    jumpInput.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      submitJumpForm();
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
    global.addEventListener('resize', scheduleFlagLayout, {passive: true});

    state.elements = {
      root: root,
      panel: panel,
      header: header,
      back: back,
      backLabel: backLabel,
      leftPicker: leftPicker,
      addPicker: addPicker,
      rightPicker: rightPicker,
      morePicker: morePicker,
      outlinePicker: outlinePicker,
      swap: swap,
      closeCompare: closeCompare,
      cite: cite,
      jumpForm: jumpForm,
      jumpLabel: jumpLabel,
      jumpInput: jumpInput,
      jumpUnit: jumpUnit,
      title: title,
      eyebrow: eyebrow,
      subtitle: subtitle,
      current: current,
      citationBar: citationBar,
      citationContext: citationContext,
      copyFootnote: copyFootnote,
      copyGbt: copyGbt,
      alignmentActions: alignmentActions,
      clearSelection: clearSelection,
      alert: alert,
      comparisonNotice: comparisonNotice,
      comparisonNoticeText: comparisonNoticeText,
      comparisonNoticeAction: comparisonNoticeAction,
      readerBody: readerBody,
      sourcePane: sourcePane,
      sourcePaneHeader: sourcePaneHeader,
      viewport: viewport,
      content: content,
      comparisonPane: comparisonPane,
      comparisonTitleText: comparisonTitleText,
      comparisonPrevious: comparisonPrevious,
      comparisonNext: comparisonNext,
      comparisonFollow: comparisonFollow,
      comparisonViewport: comparisonViewport,
      comparisonContent: comparisonContent,
      pending: pending,
      pendingTitle: pendingTitle,
      pendingText: pendingText,
      pendingAction: pendingAction,
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
    ko: '韩语',
    ru: '俄语',
    it: '意大利语',
    es: '西班牙语',
    la: '拉丁语'
  };

  function languageLabel(code) {
    var value = String(code || '');
    return READER_LANGUAGE_LABELS[value] || READER_LANGUAGE_LABELS[value.split('-')[0]] || '';
  }

  function alignmentTargetDisplayLabel(target) {
    var language = languageLabel(target.language_code) || '未识别语言';
    var format = String(target.source_format || '').toUpperCase();
    var displayName = String(target.display_name || '另一版本');
    return language + (format ? ' · ' + format : '') + ' · ' + displayName;
  }

  // 记住每本书上次选择的对照目标；「添加对照版本」菜单把它排在最前。
  // 按源文献 id 存入 localStorage；不可用或读写失败时静默回退，不影响阅读。
  var COMPARISON_TARGET_STORE_PREFIX = 'mef-reader-comparison-target:';

  function rememberComparisonTarget(sourceId, targetId) {
    if (!sourceId || !targetId) return;
    try {
      global.localStorage.setItem(COMPARISON_TARGET_STORE_PREFIX + sourceId, targetId);
    } catch (error) {
      /* 隐私模式或禁用存储：忽略。*/
    }
  }

  function recallComparisonTarget(sourceId) {
    if (!sourceId) return '';
    try {
      return global.localStorage.getItem(COMPARISON_TARGET_STORE_PREFIX + sourceId) || '';
    } catch (error) {
      return '';
    }
  }

  function renderAlignmentActions() {
    if (!state.elements) return;
    state.elements.alignmentActions.replaceChildren();
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
    // 默认对照候选：先选不同语言；正在对照时保留手动选择。
    state.defaultComparisonTarget = '';
    if (targets.length) {
      var remembered = recallComparisonTarget(state.sourceId);
      var sourceLanguage = state.alignmentSourceLanguage.toLowerCase().split('-')[0];
      var otherLanguageTargets = targets.filter(function (target) {
        var language = String(target.language_code || '').toLowerCase().split('-')[0];
        return sourceLanguage && sourceLanguage !== 'und' && language &&
          language !== 'und' && language !== sourceLanguage;
      });
      var defaultTargets = otherLanguageTargets.length ? otherLanguageTargets : targets;
      var rememberedValid = remembered && defaultTargets.some(function (t) {
        return String(t.source_file_id || '') === remembered;
      }) ? remembered : '';
      var defaultTarget = state.comparison.open && state.comparison.targetSourceId
        ? state.comparison.targetSourceId
        : (rememberedValid || String(defaultTargets[0].source_file_id || ''));
      state.defaultComparisonTarget = defaultTarget;
    }
    updateCitationControls();
    renderToolbar();
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
      state.alignmentSourceLanguage = String(payload.source_language_code || '');
      state.alignmentGroupId = String(payload.document_group_id || '');
      renderAlignmentActions();
    } catch (error) {
      if (serial !== state.alignmentRequestSerial) return;
      state.alignmentTargets = [];
      state.alignmentSourceLanguage = '';
      renderAlignmentActions();
      setAlert(
        error && error.message ? error.message : '对齐版本读取失败',
        'warning'
      );
    }
  }

  function alignmentTargetName(targetSourceId) {
    var member = workMember(targetSourceId);
    if (member) return member.name;
    var target = state.alignmentTargets.find(function (candidate) {
      return String(candidate.source_file_id || '') === targetSourceId;
    });
    return target ? String(target.display_name || '') : '';
  }

  function runHost(name, argument) {
    closeMenus();
    if (typeof config[name] === 'function') config[name](argument);
  }

  async function readJSON(url, options) {
    var response = await fetchFunction()(url, options || {headers: {'Accept': 'application/json'}});
    var payload = {};
    try { payload = await response.json(); } catch (_error) { payload = {}; }
    if (!response.ok || payload.error) {
      var error = new Error(payload.error || '请求失败');
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function postJSON(url, body) {
    return readJSON(url, {
      method: 'POST',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    });
  }

  /* ── 作品上下文：成员、每对版本的状态、组件可用性 ─────────────── */
  function pairKey(a, b) { return [String(a), String(b)].sort().join('|'); }

  function workMember(sourceId) {
    return state.work.members.find(function (member) { return member.id === sourceId; }) || null;
  }

  function pairInfo(a, b) {
    var running = state.generation;
    if (running && running.groupId === state.work.groupId && running.key === pairKey(a, b)) {
      return {status: 'running'};
    }
    return state.work.pairs[pairKey(a, b)] || {status: 'none'};
  }

  function pairReadable(info) {
    return (info.status === 'direct' || info.status === 'indirect') &&
      info.stale_reason !== 'algorithm_unreadable';
  }

  function pairStatusWord(info) {
    if (info.status === 'running') return '生成中';
    if (info.stale_reason) return '需重新对齐';
    if (info.status === 'direct') return '直接对齐';
    if (info.status === 'indirect') return '间接关联';
    return '尚未对齐';
  }

  function canGenerate() {
    return state.availability === 'ready';
  }

  function generateBlockedReason() {
    if (state.availability === 'model_missing') return '需先在设置中下载对齐模型';
    if (state.availability === 'unavailable') return '对齐组件已卸载，重新安装后才能生成';
    return '对齐组件状态读取失败';
  }

  async function loadAvailability() {
    try {
      var results = await Promise.all([
        readJSON(config.alignmentModelsEndpoint),
        readJSON(config.preferencesEndpoint)
      ]);
      var compute = results[0].compute || null;
      var selected = (results[0].models || []).find(function (model) {
        return model.id === results[1].alignment_embedding_model_id;
      });
      if (!compute) state.availability = 'unknown';
      else if (compute.available !== true) state.availability = 'unavailable';
      else state.availability = selected && selected.installed ? 'ready' : 'model_missing';
    } catch (_error) {
      state.availability = 'unknown';
    }
  }

  async function loadWorkContext(sourceId) {
    var serial = state.workRequestSerial + 1;
    state.workRequestSerial = serial;
    try {
      var results = await Promise.all([
        readJSON(config.groupsEndpoint),
        readJSON(config.overviewEndpoint + '?include_statistics=0&source_id=' + encodeURIComponent(sourceId)),
        readJSON(config.currentJobEndpoint).catch(function () { return {running: false}; }),
        loadAvailability()
      ]);
      if (serial !== state.workRequestSerial || state.sourceId !== sourceId) return;
      var group = (results[0].document_groups || []).find(function (candidate) {
        return (candidate.members || []).some(function (member) { return member.source_file_id === sourceId; });
      });
      var work = {groupId: '', title: '', baseId: '', members: [], pairs: {}, languages: {}};
      if (group) {
        work.groupId = group.document_group_id;
        work.title = group.title;
        work.baseId = group.base_source_file_id || '';
        work.members = (group.members || []).map(function (member) {
          return {id: member.source_file_id, name: member.display_name || member.source_file_id, isBase: !!member.is_base};
        });
        (results[1].works || []).forEach(function (entry) {
          if (entry.document_group_id !== work.groupId) return;
          work.languages = entry.languages || {};
          (entry.pairs || []).forEach(function (pair) {
            work.pairs[pairKey(pair.source_file_ids[0], pair.source_file_ids[1])] = pair;
          });
        });
      }
      state.work = work;
      var running = results[2];
      if (running && running.running && running.document_group_id === work.groupId) {
        trackGeneration(running.job_id, work.groupId, running.pivot_source_file_id, running.target_source_file_id);
      }
      renderToolbar();
      if (state.pendingCompareWith) {
        var target = state.pendingCompareWith;
        state.pendingCompareWith = '';
        openComparisonWith(target);
      } else if (state.comparison.open) {
        updateComparisonNotice();
      }
    } catch (error) {
      if (serial !== state.workRequestSerial) return;
      state.work = {groupId: '', title: '', baseId: '', members: [], pairs: {}, languages: {}};
      renderToolbar();
    }
  }

  /* ── 自绘下拉与菜单 ─────────────────────────────────────────── */
  async function loadOutline() {
    var outline = state.outline;
    if (outline.loading) return;
    outline.loading = true;
    outline.error = '';
    if (state.openMenu === 'outline') renderMenu('outline');
    try {
      var payload = await readJSON(config.outlineEndpoint + '?source_id=' + encodeURIComponent(state.sourceId));
      if (outline !== state.outline || !state.open) return;
      outline.items = payload.entries;
    } catch (error) {
      if (outline !== state.outline || !state.open) return;
      outline.error = error.message || '目录读取失败';
    } finally {
      outline.loading = false;
      if (outline === state.outline && state.openMenu === 'outline') {
        renderMenu('outline');
        var first = state.elements.outlinePicker.menu.querySelector('.mef-reader-option:not(:disabled)');
        if (first) first.focus();
      }
    }
  }

  async function jumpToChapter(index) {
    var entry = state.outline.items[index];
    state.outline.selectedIndex = index;
    closeMenus('outline');
    var sourceId = state.sourceId;
    var targetId = state.comparison.open ? state.comparison.targetSourceId : '';
    var serial = ++state.outlineJumpSerial;
    var locateSerial = ++state.comparison.locateSerial;
    state.comparison.requestSerial += 1;
    state.outlineNavigating = true;
    if (state.comparison.followTimer !== null) global.clearTimeout(state.comparison.followTimer);
    state.comparison.followTimer = null;
    state.comparison.lastSourceRange = '';
    state.citationRange = null;
    state.citationRequestSerial += 1;
    clearLinkedSelection();
    var anchorId = entry.anchor_id || itemAnchor({}, entry.item_index);
    prepareHighlights({pageMatchSpans: [{anchor_id: anchorId,
      page_char_start: entry.char_start, page_char_end: entry.char_end}]});
    state.targetAnchorId = anchorId;
    state.currentIndex = entry.item_index;
    setAlert('', 'info');
    try {
      var loaded = await loadWindow(entry.item_index, state.targetAnchorId);
      if (!loaded || serial !== state.outlineJumpSerial || !state.open || state.sourceId !== sourceId) return;
      state.elements.viewport.focus();
      if (targetId && state.comparison.open && state.comparison.targetSourceId === targetId &&
          locateSerial === state.comparison.locateSerial) {
        if (!state.elements.pending.hidden) {
          setAlert('已跳到章节；两个版本尚无可用对齐，右栏无法同步', 'info');
          return;
        }
        // An explicit chapter jump synchronizes once even when scroll-follow is paused.
        var located = await locateInAlignedVersion(targetId, {
          startIndex: entry.item_index, endIndex: entry.item_index,
          startOffset: entry.char_start, endOffset: entry.char_end
        }, true);
        if (!located && serial === state.outlineJumpSerial && state.open &&
            state.comparison.targetSourceId === targetId && state.comparison.locateSerial === locateSerial + 1) {
          state.comparison.highlights.clear();
          state.comparison.indexHighlights.clear();
          var previousTop = state.elements.comparisonViewport.scrollTop;
          renderComparisonWindow();
          state.elements.comparisonViewport.scrollTop = previousTop;
          setAlert('已跳到章节；右栏未同步，保留原位置：' + state.elements.alert.textContent, 'warning');
        }
      }
    } finally {
      if (serial === state.outlineJumpSerial) state.outlineNavigating = false;
    }
  }

  function pickerFor(key) {
    if (!state.elements) return null;
    return {left: state.elements.leftPicker, add: state.elements.addPicker,
      right: state.elements.rightPicker, more: state.elements.morePicker,
      outline: state.elements.outlinePicker}[key] || null;
  }

  function closeMenus(returnFocusKey) {
    if (!state.elements) return;
    ['left', 'add', 'right', 'more', 'outline'].forEach(function (key) {
      var picker = pickerFor(key);
      if (!picker || picker.menu.hidden) return;
      picker.menu.hidden = true;
      picker.trigger.setAttribute('aria-expanded', 'false');
      if (returnFocusKey === key) picker.trigger.focus();
    });
    state.openMenu = '';
  }

  function menuOption(label, action, options) {
    options = options || {};
    var item = createButton('', 'mef-reader-option', action);
    item.setAttribute('role', options.role || 'option');
    if (options.role !== 'menuitem') item.setAttribute('aria-selected', options.selected ? 'true' : 'false');
    if (options.target) item.dataset.readerTarget = options.target;
    if (options.disabled) {
      item.disabled = true;
      item.setAttribute('aria-disabled', 'true');
    }
    var tick = document.createElement('span');
    tick.className = 'mef-reader-option-tick';
    tick.setAttribute('aria-hidden', 'true');
    if (options.selected) tick.appendChild(createIcon('m4.5 10.5 3.5 3.5 7.5-8', 14));
    var text = document.createElement('span');
    text.className = 'mef-reader-option-label';
    text.textContent = label;
    item.appendChild(tick);
    item.appendChild(text);
    if (options.meta) {
      var meta = document.createElement('small');
      meta.className = 'mef-reader-option-meta';
      meta.textContent = options.meta;
      item.appendChild(meta);
    }
    return item;
  }

  function menuSeparator() {
    var separator = document.createElement('div');
    separator.className = 'mef-reader-menu-separator';
    separator.setAttribute('role', 'separator');
    return separator;
  }

  function memberLabel(member) {
    var language = languageLabel(state.work.languages[member.id]);
    return member.name + (language ? ' · ' + language : '');
  }

  function renderMenu(key) {
    var picker = pickerFor(key);
    if (!picker) return;
    var menu = picker.menu;
    menu.replaceChildren();
    var targetId = state.comparison.targetSourceId;
    if (key === 'outline') {
      var outline = state.outline;
      if (outline.loading || outline.error || !outline.items || !outline.items.length) {
        var message = document.createElement('p');
        message.className = 'mef-reader-outline-message';
        message.setAttribute('role', 'status');
        message.textContent = outline.loading ? '正在读取章节…' :
          (outline.error || '此文献暂无可定位的一、二级标题');
        menu.appendChild(message);
        if (outline.error) menu.appendChild(menuOption('重试', 'retry-outline'));
      } else {
        outline.items.forEach(function (entry, index) {
          var option = menuOption(entry.title, 'jump-chapter', {
            selected: outline.selectedIndex === index,
            meta: entry.level === 1 ? '一级' : '二级'
          });
          option.dataset.readerChapter = String(index);
          option.classList.toggle('is-subchapter', entry.level === 2);
          menu.appendChild(option);
        });
      }
    } else if (key === 'left') {
      state.work.members.forEach(function (member) {
        menu.appendChild(menuOption(memberLabel(member), 'pick-left', {
          target: member.id, selected: member.id === state.sourceId
        }));
      });
    } else if (key === 'add' || key === 'right') {
      var others = state.work.members.filter(function (member) {
        return member.id !== state.sourceId;
      });
      var preferred = state.defaultComparisonTarget;
      others.sort(function (a, b) { return (b.id === preferred) - (a.id === preferred); });
      others.forEach(function (member) {
        menu.appendChild(menuOption(memberLabel(member), key === 'add' ? 'add-comparison' : 'pick-right', {
          target: member.id,
          selected: key === 'right' && member.id === targetId,
          meta: pairStatusWord(pairInfo(state.sourceId, member.id))
        }));
      });
      if (key === 'add' && typeof config.onManageWork === 'function') {
        menu.appendChild(menuSeparator());
        menu.appendChild(menuOption('管理版本…', 'manage-work'));
      }
    } else if (key === 'more') {
      var unit = state.source && state.source.source_type === 'pdf' ? ' PDF 页' : '段落';
      menu.appendChild(menuOption('跳到' + unit + '…', 'open-jump', {role: 'menuitem'}));
      if (state.work.groupId && typeof config.onFindInWork === 'function') {
        menu.appendChild(menuOption('在作品中查找…', 'find-in-work', {role: 'menuitem'}));
      }
      var decorations = menuOption(state.showDecorations ? '隐藏页眉页脚' : '显示页眉页脚', 'toggle-decorations', {role: 'menuitem'});
      menu.appendChild(decorations);
      var isReaderWindow = document.documentElement.dataset.readerWindow === 'true';
      var canNewWindow = !isReaderWindow && typeof config.canOpenInNewWindow === 'function' && config.canOpenInNewWindow();
      var canReturn = isReaderWindow && global.pywebview && global.pywebview.state;
      if (canNewWindow || canReturn || (state.work.groupId && typeof config.onManageWork === 'function')) {
        menu.appendChild(menuSeparator());
      }
      if (canNewWindow) menu.appendChild(menuOption('在新窗口打开', 'open-new-window', {role: 'menuitem'}));
      if (canReturn) menu.appendChild(menuOption('回到主窗口', 'return-main', {role: 'menuitem'}));
      if (state.work.groupId && typeof config.onManageWork === 'function') {
        menu.appendChild(menuOption('管理版本…', 'manage-work', {role: 'menuitem'}));
      }
    }
  }

  function toggleMenu(key) {
    var picker = pickerFor(key);
    if (!picker) return;
    var willOpen = picker.menu.hidden;
    closeMenus();
    if (!willOpen) return;
    if (key === 'left' && state.work.members.length < 2) return;
    renderMenu(key);
    picker.menu.hidden = false;
    picker.trigger.setAttribute('aria-expanded', 'true');
    state.openMenu = key;
    if (key === 'outline' && state.outline.items === null && !state.outline.error) loadOutline();
    var first = picker.menu.querySelector('.mef-reader-option[aria-selected="true"]:not(:disabled)') ||
      picker.menu.querySelector('.mef-reader-option:not(:disabled)');
    if (first) first.focus();
  }

  function handleMenuKeydown(event) {
    var key = state.openMenu;
    if (!key) {
      if (event.key === 'Escape' && state.review) {
        event.stopPropagation();
        closeReviewPopover(true);
      }
      return;
    }
    var picker = pickerFor(key);
    if (!picker) return;
    var options = Array.prototype.filter.call(
      picker.menu.querySelectorAll('.mef-reader-option'),
      function (option) { return !option.disabled; }
    );
    var index = options.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeMenus(key);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!options.length) return;
      var step = event.key === 'ArrowDown' ? 1 : -1;
      options[(index + step + options.length) % options.length].focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      if (options.length) options[event.key === 'Home' ? 0 : options.length - 1].focus();
    } else if (event.key === 'Tab') {
      closeMenus();
    }
  }

  function renderToolbar() {
    if (!state.elements) return;
    var elements = state.elements;
    var members = state.work.members;
    var comparing = state.comparison.open;
    elements.backLabel.textContent = state.returnLabel || '返回';
    elements.back.setAttribute('aria-label', '返回' + (state.returnLabel || ''));
    var leftMember = workMember(state.sourceId);
    elements.leftPicker.value.textContent = leftMember
      ? memberLabel(leftMember)
      : (cleanReaderTitle(state.title) || '文献阅读');
    elements.leftPicker.trigger.classList.toggle('is-static', members.length < 2);
    elements.leftPicker.trigger.setAttribute('aria-haspopup', members.length < 2 ? 'false' : 'listbox');
    elements.addPicker.wrap.hidden = comparing || members.length < 2;
    elements.swap.hidden = !comparing;
    elements.rightPicker.wrap.hidden = !comparing;
    elements.closeCompare.hidden = !comparing;
    elements.comparisonFollow.hidden = !comparing || !elements.pending.hidden;
    if (comparing) {
      var rightMember = workMember(state.comparison.targetSourceId);
      elements.rightPicker.value.textContent = rightMember
        ? memberLabel(rightMember)
        : (state.comparison.targetDisplayName || '对照版本');
      elements.comparisonTitleText.textContent = elements.rightPicker.value.textContent;
    }
    if (state.openMenu) renderMenu(state.openMenu);
  }

  function pickLeftVersion(targetId) {
    closeMenus();
    if (!targetId || targetId === state.sourceId) return;
    if (state.comparison.open && targetId === state.comparison.targetSourceId) {
      swapComparison();
      return;
    }
    var keepCompare = state.comparison.open ? state.comparison.targetSourceId : '';
    var from = state.sourceId;
    var selection = sourceCenterRange();
    var reopen = function (index) {
      openReader({
        sourceId: targetId,
        targetIndex: index,
        compareWith: keepCompare,
        returnLabel: state.returnLabel,
        noExternal: true,
        keepHostState: true
      });
    };
    if (!selection || !pairReadable(pairInfo(from, targetId))) {
      reopen(0);
      return;
    }
    postJSON(config.alignmentLocateEndpoint, {
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
    openReader({
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
    var info = pairInfo(state.sourceId, targetId);
    rememberComparisonTarget(state.sourceId, targetId);
    if (pairReadable(info)) {
      showPendingPane(false);
      var sourceId = state.sourceId;
      var request = locateInAlignedVersion(targetId, sourceCenterRange(), true);
      var serial = state.comparison.locateSerial;
      request.then(function (located) {
        if (serial !== state.comparison.locateSerial || state.sourceId !== sourceId || !state.open) return;
        if (!located && state.comparison.targetSourceId !== targetId) {
          showComparison({targetSourceId: targetId, targetIndex: 0, pageMatchSpans: []},
            alignmentTargetName(targetId));
          setAlert('当前位置没有可用的对应段落，右栏从开头显示；滚动左栏后会跟随定位', 'info');
        }
      });
      return;
    }
    openPendingComparison(targetId, info);
  }

  function markComparisonOpen(targetId) {
    var comparison = state.comparison;
    if (comparison.targetSourceId !== targetId) {
      comparison.items.clear();
      comparison.highlights.clear();
      comparison.indexHighlights.clear();
      state.links = null;
      clearLinkedSelection();
    }
    comparison.open = true;
    comparison.targetSourceId = targetId;
    comparison.targetDisplayName = alignmentTargetName(targetId);
    state.elements.panel.classList.add('is-comparing');
    state.elements.readerBody.classList.add('is-comparing');
    state.elements.comparisonPane.hidden = false;
  }

  function showPendingPane(show) {
    if (!state.elements) return;
    state.elements.pending.hidden = !show;
    state.elements.comparisonViewport.hidden = show;
    state.elements.comparisonPrevious.hidden = show;
    state.elements.comparisonNext.hidden = show;
    state.elements.readerBody.classList.toggle('is-pending', show);
  }

  function openPendingComparison(targetId, info) {
    markComparisonOpen(targetId);
    showPendingPane(true);
    var elements = state.elements;
    var running = info.status === 'running';
    elements.pendingAction.dataset.readerForce = info.stale_reason ? 'true' : 'false';
    if (running) {
      elements.pendingTitle.textContent = '正在生成对齐';
      elements.pendingText.textContent = '在本机计算，文本不会上传。关闭阅读器也会在后台继续';
      elements.pendingAction.textContent = '取消';
      elements.pendingAction.dataset.readerAction = 'cancel-alignment';
      elements.pendingAction.classList.remove('is-primary');
      elements.pendingAction.disabled = false;
    } else if (canGenerate()) {
      elements.pendingTitle.textContent = info.stale_reason ? '旧对齐已不可读' : '这两版尚未对齐';
      elements.pendingText.textContent = '在本机生成，文本不会上传。生成时左栏照常阅读，完成后译文出现在这里';
      elements.pendingAction.textContent = info.stale_reason ? '重新对齐' : '生成对齐';
      elements.pendingAction.dataset.readerAction = 'generate-alignment';
      elements.pendingAction.classList.add('is-primary');
      elements.pendingAction.disabled = false;
    } else {
      elements.pendingTitle.textContent = '这两版尚未对齐';
      elements.pendingText.textContent = generateBlockedReason();
      var installable = typeof config.onInstallComponent === 'function' && state.availability !== 'unknown';
      elements.pendingAction.textContent = state.availability === 'model_missing' ? '去设置' : '重新安装';
      elements.pendingAction.dataset.readerAction = 'install-component';
      elements.pendingAction.classList.remove('is-primary');
      elements.pendingAction.hidden = !installable;
    }
    if (canGenerate() || running) elements.pendingAction.hidden = false;
    updateComparisonNotice();
    updateComparisonControls();
    renderToolbar();
    scheduleReadingPositionSave();
  }

  function updateComparisonNotice() {
    if (!state.elements) return;
    var elements = state.elements;
    var notice = elements.comparisonNotice;
    var targetId = state.comparison.targetSourceId;
    if (!state.comparison.open || !targetId || !elements.pending.hidden) {
      notice.hidden = true;
      elements.readerBody.classList.remove('is-indirect');
      return;
    }
    var info = pairInfo(state.sourceId, targetId);
    var target = (state.alignmentTargets || []).find(function (t) {
      return String(t.source_file_id || '') === targetId;
    });
    var viaId = (info.status === 'indirect' && info.via_source_file_id) ||
      (target && target.via_source_file_id) || '';
    elements.readerBody.classList.toggle('is-indirect', !!viaId);
    var text = '';
    var actionLabel = '';
    var force = false;
    if (info.status === 'running') {
      text = '正在生成对齐，完成后自动刷新';
      actionLabel = '取消';
    } else if (info.stale_reason) {
      text = info.stale_reason === 'model_changed'
        ? '对齐模型已更换，以下是旧结果'
        : info.stale_reason === 'body_range_changed'
          ? '正文范围识别已修正，以下是旧结果'
          : '对齐算法已更新，以下是旧结果';
      actionLabel = '重新对齐';
      force = true;
    } else if (viaId) {
      var viaName = alignmentTargetName(String(viaId));
      text = '这两版没有直接对齐，位置经' + (viaName ? '「' + viaName + '」' : '基准版本') + '换算，可能错位或漏段';
      actionLabel = '生成直接对齐';
    } else if (state.comparison.lowConfidence) {
      text = '此处为粗定位，可能锚到相邻段落或注释';
    }
    notice.hidden = !text;
    elements.comparisonNoticeText.textContent = text;
    var action = elements.comparisonNoticeAction;
    action.dataset.readerForce = force ? 'true' : 'false';
    if (!actionLabel) {
      action.hidden = true;
    } else if (info.status === 'running') {
      action.hidden = false;
      action.textContent = actionLabel;
      action.dataset.readerAction = 'cancel-alignment';
    } else if (canGenerate()) {
      action.hidden = false;
      action.textContent = actionLabel;
      action.dataset.readerAction = 'generate-alignment';
    } else {
      action.hidden = state.availability !== 'unavailable' || typeof config.onInstallComponent !== 'function';
      action.textContent = '重新安装';
      action.dataset.readerAction = 'install-component';
    }
  }

  function trackGeneration(jobId, groupId, pivotId, targetId) {
    state.generation = {jobId: jobId, groupId: groupId, key: pairKey(pivotId, targetId)};
    pollComparisonAlignment(jobId);
  }

  async function startComparisonAlignment(force) {
    var targetId = state.comparison.targetSourceId;
    var groupId = state.work.groupId || state.alignmentGroupId;
    if (!targetId || !groupId) {
      setAlert('这两个版本不在同一部作品中，无法生成对齐', 'warning');
      return;
    }
    if (!canGenerate()) {
      setAlert(generateBlockedReason(), 'warning');
      return;
    }
    var pivotId = state.sourceId;
    if (state.work.baseId === targetId) {
      pivotId = targetId;
      targetId = state.sourceId;
    }
    try {
      var payload = await postJSON(config.alignmentStartEndpoint, {
        document_group_id: groupId,
        pivot_source_file_id: pivotId,
        target_source_file_id: targetId,
        force: !!force
      });
      trackGeneration(payload.job_id, groupId, pivotId, targetId);
      refreshComparisonAfterStatusChange();
    } catch (error) {
      setAlert(error && error.message ? error.message : '生成对齐失败', 'warning');
    }
  }

  async function cancelComparisonAlignment() {
    try {
      await postJSON(config.alignmentCancelEndpoint, {});
      notify('正在取消，当前批次结束后停止');
    } catch (error) {
      setAlert(error && error.message ? error.message : '取消失败', 'warning');
    }
  }

  function refreshComparisonAfterStatusChange() {
    renderToolbar();
    var targetId = state.comparison.targetSourceId;
    if (!state.comparison.open || !targetId) return;
    if (!state.elements.pending.hidden) {
      var info = pairInfo(state.sourceId, targetId);
      if (pairReadable(info)) openComparisonWith(targetId);
      else openPendingComparison(targetId, info);
    } else {
      updateComparisonNotice();
    }
  }

  async function pollComparisonAlignment(jobId) {
    if (state.pollingJobId === jobId) return;
    state.pollingJobId = jobId;
    // 后台生成期间每 ~1.5s 查询一次任务状态，直到非 202（完成、失败或取消）。
    while (state.pollingJobId === jobId) {
      await new Promise(function (resolve) { global.setTimeout(resolve, 1500); });
      if (state.pollingJobId !== jobId) return;
      var response;
      try {
        response = await fetchFunction()(
          config.alignmentStatusEndpoint + '?job_id=' + encodeURIComponent(jobId),
          {headers: {'Accept': 'application/json'}}
        );
      } catch (_error) {
        continue;
      }
      if (response.status === 202) continue;
      var payload = {};
      try { payload = await response.json(); } catch (_error) { payload = {}; }
      if (state.pollingJobId !== jobId) return;
      var completed = state.generation;
      state.pollingJobId = '';
      state.generation = null;
      if (!state.open) return;
      var sourceId = state.sourceId;
      if (response.ok && payload.ok) notify('对齐已生成');
      else if (payload.cancelled) notify('已取消生成对齐');
      else if (response.status !== 404) setAlert(payload.error || '生成对齐失败', 'warning');
      await loadAlignmentTargets(state.sourceId);
      await loadWorkContext(state.sourceId);
      if (!state.open || state.sourceId !== sourceId) return;
      if (response.ok && payload.ok && completed && completed.groupId === state.work.groupId &&
          state.comparison.open) {
        // A base-leg update also changes indirect pairs within this work.
        state.links = null;
        state.linkRequestSerial += 1;
        clearLinkedSelection();
        state.comparison.lastSourceRange = '';
        openComparisonWith(state.comparison.targetSourceId);
        renderToolbar();
      } else {
        refreshComparisonAfterStatusChange();
      }
      return;
    }
  }

  /* ── 新窗口 / 回到主窗口 / 跳到页 ─────────────────────────────── */
  function currentLocationOptions() {
    return {
      sourceId: state.sourceId,
      title: state.title,
      targetIndex: state.currentIndex,
      compareWith: state.comparison.open ? state.comparison.targetSourceId : ''
    };
  }

  function openInNewWindow() {
    closeMenus();
    if (typeof config.openInNewWindow !== 'function') return;
    Promise.resolve(config.openInNewWindow(currentLocationOptions())).then(function (opened) {
      if (opened) closeReader();
    }).catch(function (error) {
      setAlert(error && error.message ? error.message : '无法打开新窗口', 'warning');
    });
  }

  function returnToMainWindow() {
    closeMenus();
    if (!global.pywebview || !global.pywebview.state) return;
    global.pywebview.state.readerReturn = currentLocationOptions();
  }

  function openJumpForm() {
    closeMenus();
    var elements = state.elements;
    var isPdf = state.source && state.source.source_type === 'pdf';
    elements.jumpLabel.textContent = isPdf ? '跳到 PDF 第' : '跳到第';
    elements.jumpUnit.textContent = isPdf ? '页' : '段';
    if (state.total) elements.jumpInput.max = String(state.total);
    elements.jumpInput.value = String(state.currentIndex + 1);
    elements.jumpForm.hidden = false;
    elements.jumpInput.focus();
    elements.jumpInput.select();
  }

  function closeJumpForm() {
    if (!state.elements || state.elements.jumpForm.hidden) return;
    state.elements.jumpForm.hidden = true;
    state.elements.morePicker.trigger.focus();
  }

  function submitJumpForm() {
    var value = Math.floor(Number(state.elements.jumpInput.value));
    if (!Number.isFinite(value) || value < 1) return;
    var index = state.total ? Math.min(value, state.total) - 1 : value - 1;
    state.elements.jumpForm.hidden = true;
    goTo({targetIndex: index});
    state.elements.viewport.focus();
  }

  /* ── 阅读位置：按作品保存版本对与位置，供「继续阅读」 ─────────── */
  function saveReadingPositionNow() {
    if (state.positionTimer !== null) {
      global.clearTimeout(state.positionTimer);
      state.positionTimer = null;
    }
    if (!state.open || !state.work.groupId || !state.items.has(state.currentIndex)) return;
    postJSON(config.readingPositionEndpoint, {
      document_group_id: state.work.groupId,
      left_source_file_id: state.sourceId,
      right_source_file_id: state.comparison.open ? state.comparison.targetSourceId : null,
      item_index: state.currentIndex,
      char_offset: 0
    }).catch(function () { /* 位置只是便利信息，保存失败不打扰阅读。 */ });
  }

  function scheduleReadingPositionSave() {
    if (!state.open || !state.work.groupId || !state.items.has(state.currentIndex)) return;
    if (state.positionTimer !== null) global.clearTimeout(state.positionTimer);
    state.positionTimer = global.setTimeout(saveReadingPositionNow, 1500);
  }

  /* ── 逐段对应：选中段落高亮对应段，低置信段落标「!」 ───────────── */
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
    readJSON(config.linksEndpoint + '?' + query.toString()).then(function (payload) {
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
      var length = codePointLength(value);
      if (remaining <= length) {
        return {node: node, offset: codePointToUtf16Index(value, remaining)};
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
      var flag = createButton('!', 'mef-reader-flag' + (link.deferred ? ' is-deferred' : ''), 'review-link');
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
    var anchorId = itemAnchor(item, index);
    appendHighlightedText(
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
    var utf16 = textOffsetWithin(body, caretNode, caretOffset);
    var item = state.items.get(index);
    if (utf16 === null || !item) return;
    var offset = utf16ToCodePointIndex(item.text_raw || '', utf16);
    var link = state.links.items.find(function (candidate) {
      return (candidate.source_spans || []).some(function (span) {
        return span.item_index === index && span.char_start <= offset && offset < span.char_end;
      });
    });
    if (!link) return;
    if (state.selectedLinkKey === linkKey(link)) {
      clearLinkedSelection();
      state.comparison.indexHighlights.clear();
      renderComparisonWindow();
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
      renderComparisonWindow();
      setAlert(link.manual === 'no_counterpart' ? '已人工确认：另一版本中没有对应段落' : '这一段在另一版本中没有找到对应段落', 'info');
      return;
    }
    comparison.currentIndex = first.item_index;
    if (comparison.items.has(first.item_index)) renderComparisonWindow();
    else loadComparisonWindow(first.item_index);
  }

  function closeReviewPopover(restoreFocus) {
    var review = state.review;
    if (!review) return;
    state.review = null;
    review.node.remove();
    if (restoreFocus && review.trigger && review.trigger.isConnected) review.trigger.focus();
  }

  function candidateLocation(candidate) {
    var span = (candidate.page_match_spans || [])[0];
    if (!span) return '';
    if (span.pdf_page_index != null) return 'PDF 第 ' + (Number(span.pdf_page_index) + 1) + ' 页';
    if (span.paragraph_index != null) return '第 ' + (Number(span.paragraph_index) + 1) + ' 段';
    return '';
  }

  async function openReviewPopover(trigger) {
    closeReviewPopover();
    var index = Number(trigger.dataset.readerLink);
    var link = state.links && state.links.items[index];
    if (!link) return;
    var near = link.target_segment_ids || [];
    var node = document.createElement('div');
    node.className = 'mef-reader-review';
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-labelledby', 'mef-reader-review-title');
    var heading = document.createElement('h4');
    heading.id = 'mef-reader-review-title';
    heading.textContent = '这一段的对应可能不准';
    var hint = document.createElement('p');
    hint.textContent = '勾选这段原文对应的全部译文段落';
    var list = document.createElement('div');
    list.className = 'mef-reader-review-list';
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', '候选译文段落');
    var loadingText = document.createElement('p');
    loadingText.className = 'mef-reader-review-loading';
    loadingText.textContent = '正在读取候选段落…';
    list.appendChild(loadingText);
    var actions = document.createElement('div');
    actions.className = 'mef-reader-review-actions';
    var save = createButton('保存校正', 'mef-reader-tool-btn is-primary', '');
    save.disabled = true;
    var none = createButton('译本无对应', 'mef-reader-tool-btn is-quiet', '');
    var later = createButton('暂不处理', 'mef-reader-tool-btn is-quiet', '');
    var grow = document.createElement('span');
    grow.className = 'mef-reader-toolbar-spacer';
    actions.appendChild(save);
    actions.appendChild(none);
    actions.appendChild(grow);
    actions.appendChild(later);
    node.appendChild(heading);
    node.appendChild(hint);
    node.appendChild(list);
    node.appendChild(actions);
    state.elements.root.appendChild(node);
    state.review = {node: node, trigger: trigger};
    var rect = trigger.getClientRects()[0];
    var rootRect = state.elements.root.getClientRects()[0];
    var width = Math.min(400, rootRect.width - 32);
    node.style.width = width + 'px';
    node.style.left = Math.max(16, Math.min(rect.right - rootRect.left - width, rootRect.width - width - 16)) + 'px';
    node.style.top = Math.max(16, Math.min(rect.bottom - rootRect.top + 8, rootRect.height - 320)) + 'px';

    var checked = new Set(link.target_segment_ids || []);
    function syncSave() { save.disabled = checked.size === 0; }
    async function submit(kind) {
      [save, none, later].forEach(function (button) { button.disabled = true; });
      try {
        if (kind === 'later') {
          await postJSON(config.correctionDeferEndpoint, {
            source_file_id: state.sourceId,
            target_source_file_id: state.comparison.targetSourceId,
            source_segment_ids: link.source_segment_ids
          });
        } else {
          await postJSON(config.correctionSaveEndpoint, {
            source_file_id: state.sourceId,
            target_source_file_id: state.comparison.targetSourceId,
            source_segment_ids: link.source_segment_ids,
            target_segment_ids: kind === 'none' ? [] : Array.from(checked)
          });
          notify(kind === 'none' ? '已记录：译本无对应' : '已保存校正，对应 ' + checked.size + ' 段译文');
        }
        closeReviewPopover(false);
        state.links = null;
        loadLinkWindow();
      } catch (error) {
        [none, later].forEach(function (button) { button.disabled = false; });
        syncSave();
        setAlert(error && error.message ? error.message : '保存失败', 'warning');
      }
    }
    save.addEventListener('click', function () { submit('save'); });
    none.addEventListener('click', function () { submit('none'); });
    later.addEventListener('click', function () { submit('later'); });
    none.focus();
    try {
      var payload = await postJSON(config.reviewCandidatesEndpoint, {
        source_file_id: state.sourceId,
        target_source_file_id: state.comparison.targetSourceId,
        source_segment_ids: link.source_segment_ids,
        near_target_segment_ids: near,
        radius: 3
      });
      if (!state.review || state.review.node !== node) return;
      list.replaceChildren();
      if (!(payload.candidates || []).length) {
        loadingText.textContent = '附近没有可选的译文段落';
        list.appendChild(loadingText);
      }
      (payload.candidates || []).forEach(function (candidate) {
        var id = String(candidate.segment_id);
        var row = document.createElement('label');
        row.className = 'mef-reader-review-row';
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = checked.has(id);
        box.addEventListener('change', function () {
          if (box.checked) checked.add(id);
          else checked.delete(id);
          syncSave();
        });
        var text = document.createElement('span');
        text.className = 'mef-reader-review-text';
        text.textContent = String(candidate.text || '');
        var where = document.createElement('small');
        where.textContent = candidateLocation(candidate);
        row.appendChild(box);
        row.appendChild(text);
        row.appendChild(where);
        list.appendChild(row);
      });
      syncSave();
    } catch (error) {
      if (!state.review || state.review.node !== node) return;
      loadingText.textContent = error && error.message ? error.message : '候选段落读取失败';
    }
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
      appendHighlightedText(body, text, ranges, item.decoration_spans);
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
    var targetSourceId = String(payload.targetSourceId || payload.target_source_file_id || '');
    var changedTarget = comparison.targetSourceId !== targetSourceId;
    markComparisonOpen(targetSourceId);
    showPendingPane(false);
    rememberComparisonTarget(state.sourceId, targetSourceId);
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
    comparison.indexHighlights.clear();
    setComparisonHighlights(payload.pageMatchSpans || payload.page_match_spans || []);
    // 精确高亮不可用 = 粗定位 → 说明条如实标注。
    comparison.lowConfidence = (payload.preciseHighlightAvailable != null
      ? payload.preciseHighlightAvailable
      : payload.precise_highlight_available) === false;
    updateComparisonNotice();
    updateComparisonControls();
    renderToolbar();
    loadLinkWindow();
    scheduleReadingPositionSave();
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
    closeReviewPopover();
    if (!state.elements) return;
    clearLinkedSelection();
    renderFlags();
    state.elements.panel.classList.remove('is-comparing');
    state.elements.readerBody.classList.remove('is-comparing', 'is-indirect', 'is-pending');
    state.elements.comparisonPane.hidden = true;
    state.elements.comparisonNotice.hidden = true;
    showPendingPane(false);
    state.elements.comparisonContent.replaceChildren();
    renderToolbar();
    scheduleReadingPositionSave();
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
      setAlert('', 'info');
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
    if (pathname !== '/reader' && pathname !== '/reader/' && pathname !== '/reader-window') return null;
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
    var readerPath = document.documentElement.dataset.readerWindow === 'true' ? '/reader-window' : '/reader';
    var url = readerPath + '?' + params.toString();
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
      scheduleReadingPositionSave();
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
    if (state.openMenu === 'more') renderMenu('more');
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
  function appendHighlightedText(container, text, ranges, decorations, linkedRanges) {
    var codePoints = Array.from(text);
    var highlights = mergeCodePointRanges(ranges, codePointLength(text));
    var linked = mergeCodePointRanges(linkedRanges, codePointLength(text));
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
    if (!highlights.length && !decoRanges.length && !linked.length) {
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
    if (isPageContinuation(item, state.items.get(absoluteIndex - 1))) {
      article.classList.add('is-continued');
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
    if (state.comparison.open) loadLinkWindow();
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
    if (!options.noExternal && config.openExternal && await config.openExternal(options)) return true;
    ensureDom();
    closeMenus();
    closeReviewPopover();
    if (state.comparison.open) closeComparison();
    var wasOpen = state.open;

    if (options.config) configure(options.config);
    if (!state.open) {
      state.restoreFocus = document.activeElement;
      state.originalUrl = options.restoringSession
        ? (state.originalUrl || '/')
        : ordinaryUrlBeforeReader();
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
    if (!wasOpen && typeof config.onOpenChange === 'function') config.onOpenChange(true);
    setAlert('', 'info');
    renderAlignmentActions();
    updateCitationControls();

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
    if (loaded && state.open && state.sourceId === sourceId) {
      loadAlignmentTargets(sourceId);
      loadWorkContext(sourceId);
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
    closeMenus();
    closeReviewPopover();
    state.elements.jumpForm.hidden = true;
    // 关闭前立即写一次当前位置（含右栏），再收起对照；收起对照排队的保存随之取消。
    saveReadingPositionNow();
    closeComparison();
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
    disconnectObservers();
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
    if (typeof config.onOpenChange === 'function') config.onOpenChange(false);
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

  document.addEventListener('keydown', function (event) {
    if (!state.open || event.key !== 'Escape' || event.defaultPrevented) return;
    if (state.openMenu) { closeMenus(state.openMenu); return; }
    if (state.review) { closeReviewPopover(true); return; }
    if (state.elements && !state.elements.jumpForm.hidden) { closeJumpForm(); return; }
    // 应用自己的弹窗或抽屉在上层时，Esc 先交给它们。
    if (document.querySelector('.tw-dialog-scrim, .tw-sheet-scrim, .app-dialog-backdrop.open')) return;
    closeReader();
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
    if (document.documentElement.dataset.readerWindow === 'true') return;
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
