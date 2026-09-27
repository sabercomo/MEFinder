(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config, alignmentJobs = r.alignmentJobs;

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
      var button = r.createButton(
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
    r.updateCitationControls();
    renderToolbar();
  }

  async function loadAlignmentTargets(sourceId) {
    var serial = state.alignmentRequestSerial + 1;
    state.alignmentRequestSerial = serial;
    try {
      var response = await r.fetchFunction()(
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
      r.setAlert(
        error && error.message ? error.message : '对齐版本读取失败',
        'warning'
      );
    }
  }

  function resetWorkContext(compareWith) {
    state.workRequestSerial += 1;
    state.outline = {items: null, loading: false, error: ''};
    state.outlineJumpSerial += 1;
    state.outlineNavigating = false;
    state.alignmentRequestSerial += 1;
    state.alignmentTargets = [];
    state.alignmentSourceLanguage = '';
    state.alignmentGroupId = '';
    state.alignmentLoading = false;
    state.availability = 'unknown';
    state.defaultComparisonTarget = '';
    state.pendingCompareWith = String(compareWith || '');
    state.work = {groupId: '', title: '', baseId: '', members: [], pairs: {}, languages: {}};
  }

  function invalidateOutlineJump() {
    state.outlineJumpSerial += 1;
    state.outlineNavigating = false;
  }

  function setAlignmentLoading(loading) {
    state.alignmentLoading = loading;
    r.updateCitationControls();
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

  /* 普通 JSON 请求走统一客户端；需要看原始状态码的请求直接用 fetchFunction()。 */
  var api = global.MEFinderApi.withFetch(r.fetchFunction);

  function readJSON(url, options) {
    return api.requestJSON(url, options || {headers: {'Accept': 'application/json'}});
  }

  function postJSON(url, body) {
    return api.postJSON(url, body);
  }

  /* ── 作品上下文：成员、每对版本的状态、组件可用性 ─────────────── */
  function pairKey(a, b) { return [String(a), String(b)].sort().join('|'); }

  function workMember(sourceId) {
    return state.work.members.find(function (member) { return member.id === sourceId; }) || null;
  }

  function pairInfo(a, b) {
    var running = alignmentJobs.running();
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
      if (!compute) return 'unknown';
      if (compute.available !== true) return 'unavailable';
      return selected && selected.installed ? 'ready' : 'model_missing';
    } catch (_error) {
      return 'unknown';
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
      state.availability = results[3];
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
        // 页面刷新或任务由别处发起：认领它，不改已有认领者的归属。
        alignmentJobs.watch(running.job_id, {
          origin: 'reader',
          groupId: work.groupId,
          key: pairKey(running.pivot_source_file_id, running.target_source_file_id)
        });
      }
      renderToolbar();
      if (state.pendingCompareWith) {
        var target = state.pendingCompareWith;
        state.pendingCompareWith = '';
        r.openComparisonWith(target);
      } else if (state.comparison.open) {
        r.updateComparisonNotice();
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
    var locateSerial = r.pauseComparisonForChapter();
    state.outlineNavigating = true;
    r.invalidateCitationForJump();
    r.clearLinkedSelection();
    var anchorId = entry.anchor_id || r.itemAnchor({}, entry.item_index);
    r.prepareHighlights({pageMatchSpans: [{anchor_id: anchorId,
      page_char_start: entry.char_start, page_char_end: entry.char_end}]});
    r.setWindowTarget(entry.item_index, anchorId);
    r.setAlert('', 'info');
    try {
      var loaded = await r.loadWindow(entry.item_index, state.targetAnchorId);
      if (!loaded || serial !== state.outlineJumpSerial || !state.open || state.sourceId !== sourceId) return;
      state.elements.viewport.focus();
      if (targetId && state.comparison.open && state.comparison.targetSourceId === targetId &&
          locateSerial === state.comparison.locateSerial) {
        if (!state.elements.pending.hidden) {
          r.setAlert('已跳到章节；两个版本尚无可用对齐，右栏无法同步', 'info');
          return;
        }
        // An explicit chapter jump synchronizes once even when scroll-follow is paused.
        var located = await r.locateInAlignedVersion(targetId, {
          startIndex: entry.item_index, endIndex: entry.item_index,
          startOffset: entry.char_start, endOffset: entry.char_end
        }, true);
        if (!located && serial === state.outlineJumpSerial && state.open &&
            state.comparison.targetSourceId === targetId && state.comparison.locateSerial === locateSerial + 1) {
          r.clearComparisonHighlights();
          r.setAlert('已跳到章节；右栏未同步，保留原位置：' + state.elements.alert.textContent, 'warning');
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
    var item = r.createButton('', 'mef-reader-option', action);
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
    if (options.selected) tick.appendChild(r.createIcon('m4.5 10.5 3.5 3.5 7.5-8', 14));
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
        r.closeReviewPopover(true);
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
    r.goTo({targetIndex: index});
    state.elements.viewport.focus();
  }

  /* ── 阅读位置：按作品保存版本对与位置，供「继续阅读」 ─────────── */
  // 返回刚写出的位置（没写则 null），形状与 GET 的响应一致：关闭阅读器时
  // 宿主直接拿它更新「继续阅读」，不必再等一次往返或靠定时器猜写入完成。

  r.cleanReaderTitle = cleanReaderTitle;
  r.readerByline = readerByline;
  r.rememberComparisonTarget = rememberComparisonTarget;
  r.renderAlignmentActions = renderAlignmentActions;
  r.loadAlignmentTargets = loadAlignmentTargets;
  r.resetWorkContext = resetWorkContext;
  r.invalidateOutlineJump = invalidateOutlineJump;
  r.setAlignmentLoading = setAlignmentLoading;
  r.alignmentTargetName = alignmentTargetName;
  r.runHost = runHost;
  r.readJSON = readJSON;
  r.postJSON = postJSON;
  r.pairKey = pairKey;
  r.pairInfo = pairInfo;
  r.pairReadable = pairReadable;
  r.canGenerate = canGenerate;
  r.generateBlockedReason = generateBlockedReason;
  r.loadWorkContext = loadWorkContext;
  r.loadOutline = loadOutline;
  r.jumpToChapter = jumpToChapter;
  r.closeMenus = closeMenus;
  r.renderMenu = renderMenu;
  r.toggleMenu = toggleMenu;
  r.handleMenuKeydown = handleMenuKeydown;
  r.renderToolbar = renderToolbar;
  r.openJumpForm = openJumpForm;
  r.closeJumpForm = closeJumpForm;
  r.submitJumpForm = submitJumpForm;
}(window));
