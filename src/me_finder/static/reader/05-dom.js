(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state;

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
        r.closeMenus();
      }
      if (!event.target.closest || !event.target.closest('.mef-reader-review')) r.closeReviewPopover();
      if (action === 'close') r.closeReader();
      if (action === 'jump-chapter') r.jumpToChapter(Number(trigger.dataset.readerChapter));
      if (action === 'retry-outline') r.loadOutline();
      if (action === 'toggle-menu') r.toggleMenu(trigger.dataset.readerMenuKey);
      if (action === 'pick-left') r.pickLeftVersion(trigger.dataset.readerTarget || '');
      if (action === 'pick-right' || action === 'add-comparison') {
        r.closeMenus();
        r.openComparisonWith(trigger.dataset.readerTarget || '');
      }
      if (action === 'manage-work') r.runHost('onManageWork', state.work.groupId);
      if (action === 'find-in-work') r.runHost('onFindInWork', state.work.groupId);
      if (action === 'open-jump') r.openJumpForm();
      if (action === 'jump-cancel') r.closeJumpForm();
      if (action === 'open-new-window') r.openInNewWindow();
      if (action === 'return-main') r.returnToMainWindow();
      if (action === 'install-component') r.runHost('onInstallComponent');
      if (action === 'swap-comparison') r.swapComparison();
      if (action === 'toggle-citation') r.toggleCitationMenu();
      if (action === 'copy-footnote') r.copyCachedCitation('chinese');
      if (action === 'copy-gbt7714') r.copyCachedCitation('gb');
      if (action === 'locate-alignment') {
        r.locateInAlignedVersion(trigger.dataset.readerTarget || '');
      }
      if (action === 'generate-alignment') r.startComparisonAlignment(trigger.dataset.readerForce === 'true');
      if (action === 'cancel-alignment') r.cancelComparisonAlignment();
      if (action === 'toggle-comparison-follow') r.toggleComparisonFollow();
      if (action === 'close-comparison') r.closeComparison();
      if (action === 'comparison-previous') r.loadComparisonPrevious();
      if (action === 'comparison-next') r.loadComparisonNext();
      if (action === 'clear-selection') r.clearCitationRange();
      if (action === 'toggle-decorations') { r.closeMenus(); r.toggleDecorationVisibility(); }
      if (action === 'review-link') r.openReviewPopover(trigger);
      if (!action && event.target.closest && event.target.closest('.mef-reader-source-pane .mef-reader-item-text')) {
        r.selectLinkAtClick(event);
      }
    });
    root.addEventListener('keydown', r.handleMenuKeydown);
    jumpForm.addEventListener('submit', function (event) {
      event.preventDefault();
      r.submitJumpForm();
    });
    jumpInput.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      r.submitJumpForm();
    });
    viewport.addEventListener('mousedown', function () {
      r.beginSelectionDrag();
    });
    viewport.addEventListener('keyup', r.scheduleSelectionCapture);
    viewport.addEventListener('keydown', r.handleReaderNavigationKey);
    viewport.addEventListener('scroll', r.scheduleScrollBoundaryCheck, {
      passive: true
    });
    viewport.addEventListener('scroll', r.scheduleComparisonFollow, {
      passive: true
    });
    global.addEventListener('resize', r.scheduleFlagLayout, {passive: true});

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

  function destroyDom() {
    if (state.elements && state.elements.root.isConnected) state.elements.root.remove();
    state.elements = null;
  }


  r.createButton = createButton;
  r.createIcon = createIcon;
  r.ensureDom = ensureDom;
  r.destroyDom = destroyDom;
}(window));
