/* 译本对照：作品—版本页、管理版本抽屉、加入作品与同名核对弹窗。
   界面叫「作品」，数据仍是作品组（document_groups）。DOM 一律用 el() 构造，
   不拼 HTML 字符串；状态文案只陈述 /api/translation-works/overview 能确认的事实。 */
(function (global) {  // module: 35-works.js
  'use strict';

  var LANGUAGE_NAMES = {
    'zh-Hans': '简体中文', 'zh-Hant': '繁体中文', zh: '中文', en: '英文', de: '德文',
    fr: '法文', ja: '日文', ko: '韩文', ru: '俄文', it: '意大利文', es: '西班牙文',
    pt: '葡萄牙文', la: '拉丁文', grc: '古希腊文', el: '希腊文', nl: '荷兰文'
  };
  var DELETE_UNDO_MS = 6000;
  var POSITION_POLL_MS = 1500;

  var works = {
    groups: [],
    pairsByGroup: {},
    languagesByGroup: {},
    availability: {state: 'unknown', compute: null, modelInstalled: null},
    dismissals: [],
    suggestionsHidden: false,
    currentId: '',
    picks: {},
    query: '',
    positions: {},
    running: null,
    hiddenGroupIds: new Set(),
    sheetGroupId: '',
    sheetQuery: '',
    loadSerial: 0,
    loaded: false,
    catalog: null
  };

  /* ── DOM helpers ─────────────────────────────────────────────── */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value == null || value === false) return;
      if (key === 'className') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'dataset') Object.keys(value).forEach(function (k) { node.dataset[k] = value[k]; });
      else if (key.slice(0, 2) === 'on' && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    });
    (Array.isArray(children) ? children : [children]).forEach(function (child) {
      if (child == null || child === false) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function svg(pathData, size) {
    var ns = 'http://www.w3.org/2000/svg';
    var icon = document.createElementNS(ns, 'svg');
    icon.setAttribute('viewBox', '0 0 20 20');
    icon.setAttribute('width', String(size || 16));
    icon.setAttribute('height', String(size || 16));
    icon.setAttribute('fill', 'none');
    icon.setAttribute('stroke', 'currentColor');
    icon.setAttribute('stroke-width', '1.8');
    icon.setAttribute('stroke-linecap', 'round');
    icon.setAttribute('stroke-linejoin', 'round');
    icon.setAttribute('aria-hidden', 'true');
    pathData.split('|').forEach(function (d) {
      var path = document.createElementNS(ns, 'path');
      path.setAttribute('d', d);
      icon.appendChild(path);
    });
    return icon;
  }

  var ICON_SEARCH = 'M8.5 14.5a6 6 0 1 0 0-12 6 6 0 0 0 0 12Z|m13 13 5 5';
  var ICON_CLOSE = 'M5 5l10 10|M15 5 5 15';
  var ICON_CHEVRON = 'm6 8 4 4 4-4';
  var ICON_CHECK = 'm4.5 10.5 3.5 3.5 7.5-8';

  function button(label, className, onClick, extra) {
    var attrs = Object.assign({type: 'button', className: 'tw-btn ' + (className || '')}, extra || {});
    attrs.onclick = onClick;
    return el('button', attrs, label);
  }

  async function requestJSON(url, options) {
    var response = await fetch(url, options);
    var data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok || data.error) {
      var error = new Error(data.error || '请求失败');
      error.status = response.status;
      error.payload = data;
      throw error;
    }
    return data;
  }

  function postJSON(url, payload) {
    return requestJSON(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify(payload || {})
    });
  }

  function toastWithAction(message, actionLabel, action, onExpire) {
    var item = showToast(message, 'success');
    if (!item) return null;
    clearTimeout(Number(item.dataset.timer));
    var done = false;
    var actionButton = el('button', {
      type: 'button', className: 'toast-action',
      onclick: function () {
        if (done) return;
        done = true;
        clearTimeout(timer);
        dismissToast(item, true);
        action();
      }
    }, actionLabel);
    item.appendChild(actionButton);
    var timer = setTimeout(function () {
      if (done) return;
      done = true;
      dismissToast(item);
      if (onExpire) onExpire();
    }, DELETE_UNDO_MS);
    return item;
  }

  /* ── data ─────────────────────────────────────────────────────── */
  function catalogSources() {
    return (works.catalog && works.catalog.items) || [];
  }

  function sourceById(sourceId) {
    return catalogSources().find(function (item) { return item.source_file_id === sourceId; }) || null;
  }

  function visibleGroups() {
    return works.groups.filter(function (group) {
      return !works.hiddenGroupIds.has(group.document_group_id);
    });
  }

  function groupById(groupId) {
    return works.groups.find(function (group) { return group.document_group_id === groupId; }) || null;
  }

  function groupForSource(sourceId) {
    return visibleGroups().find(function (group) {
      return (group.members || []).some(function (member) { return member.source_file_id === sourceId; });
    }) || null;
  }

  function memberName(group, sourceId) {
    var member = (group.members || []).find(function (m) { return m.source_file_id === sourceId; });
    return member ? (member.display_name || sourceId) : sourceId;
  }

  function pairKey(a, b) { return [a, b].sort().join('|'); }

  function pairStatus(groupId, a, b) {
    var running = works.running;
    if (running && running.document_group_id === groupId &&
        pairKey(running.pivot_source_file_id, running.target_source_file_id) === pairKey(a, b)) {
      return {status: 'running'};
    }
    var pairs = works.pairsByGroup[groupId] || {};
    return pairs[pairKey(a, b)] || {status: 'none'};
  }

  function languageName(code) {
    var value = String(code || '');
    if (!value || value === 'und') return '语言未识别';
    return LANGUAGE_NAMES[value] || LANGUAGE_NAMES[value.split('-')[0]] || value;
  }

  function sourceLanguageCode(groupId, sourceId) {
    var source = sourceById(sourceId);
    var fromLibrary = source && source.language_code && source.language_code !== 'und' ? source.language_code : '';
    return fromLibrary || (works.languagesByGroup[groupId] || {})[sourceId] || '';
  }

  function cleanTitle(value) {
    var text = String(value || '').trim();
    var cut = text.indexOf(' (');
    if (cut > 0 && /[A-Za-z]|Library/i.test(text.slice(cut))) text = text.slice(0, cut).trim();
    return text;
  }

  function sourceTitle(sourceId) {
    var source = sourceById(sourceId);
    return cleanTitle(source ? (source.title || source.file_name) : '') || sourceId;
  }

  function sourceFormat(sourceId) {
    var source = sourceById(sourceId);
    return source ? sourceFormatLabel(source) : '';
  }

  function publicationText(sourceId) {
    var source = sourceById(sourceId) || {};
    var bib = source.bibliographic_metadata || {};
    var parts = [source.publisher || bib.publisher, source.publish_year || bib.publish_year || bib.year]
      .filter(function (value) { return value != null && String(value).trim(); })
      .map(String);
    return parts.length ? parts.join(' · ') : '—';
  }

  // 页码来源：PDF 读文献库的映射方式，EPUB 读导入时是否记录「缺出版方页码」；
  // 其余格式没有可确认的页码来源，如实显示「—」，绝不推算。
  function pageSource(member) {
    var source = sourceById(member.source_file_id);
    if (source && source.source_type === 'pdf') {
      var method = source.mapping_method || 'uncalibrated';
      return {label: mappingMethodLabel(method), warn: method === 'uncalibrated'};
    }
    if (member.epub_publisher_pages === true) return {label: '出版方页码', warn: false};
    if (member.epub_publisher_pages === false) return {label: '未校准', warn: true};
    return {label: '—', warn: false};
  }

  function canGenerate() {
    return works.availability.state === 'ready';
  }

  function generateBlockedReason() {
    var state = works.availability.state;
    if (state === 'model_missing') return '需先在设置中下载对齐模型';
    if (state === 'unavailable') return '对齐组件已卸载，重新安装后才能生成';
    if (state === 'unknown') return '对齐组件状态读取失败';
    return '';
  }

  function hasAnyAlignment() {
    return works.groups.some(function (group) { return (group.alignments || []).length > 0; });
  }

  function entryVisible() {
    var state = works.availability.state;
    if (state === 'ready' || state === 'model_missing') return true;
    // 状态未知不等于未安装；已有对齐时照常给入口，生成动作仍按后端结果处理。
    return hasAnyAlignment();
  }

  function isReadOnly() {
    return works.availability.state === 'unavailable' && hasAnyAlignment();
  }

  async function loadAvailability() {
    try {
      var results = await Promise.all([
        requestJSON('/api/text-alignment/models'),
        requestJSON('/api/preferences')
      ]);
      var component = results[0];
      var selectedId = results[1].alignment_embedding_model_id;
      var model = (component.models || []).find(function (item) { return item.id === selectedId; });
      var compute = component.compute || null;
      var available = compute ? compute.available === true : null;
      var state = 'unknown';
      if (available === false) state = 'unavailable';
      else if (available === true) state = model && model.installed ? 'ready' : 'model_missing';
      works.availability = {state: state, compute: compute, modelInstalled: model ? !!model.installed : null};
    } catch (_) {
      works.availability = {state: 'unknown', compute: null, modelInstalled: null};
    }
  }

  async function loadGroupsAndOverview() {
    var results = await Promise.all([
      requestJSON('/api/document-groups'),
      requestJSON('/api/translation-works/overview').catch(function () { return {works: []}; }),
      requestJSON('/api/translation-works/suggestion-dismissals').catch(function () { return {dismissals: []}; }),
      requestJSON('/api/text-alignments/current').catch(function () { return {running: false}; })
    ]);
    works.groups = Array.isArray(results[0].document_groups) ? results[0].document_groups : [];
    libraryStore.documentGroups = works.groups;
    works.pairsByGroup = {};
    works.languagesByGroup = {};
    (results[1].works || []).forEach(function (entry) {
      var map = {};
      (entry.pairs || []).forEach(function (pair) {
        map[pairKey(pair.source_file_ids[0], pair.source_file_ids[1])] = pair;
      });
      works.pairsByGroup[entry.document_group_id] = map;
      works.languagesByGroup[entry.document_group_id] = entry.languages || {};
    });
    works.dismissals = (results[2].dismissals || []).map(function (ids) { return ids.slice().sort().join('\n'); });
    works.running = results[3].running ? results[3] : null;
    if (works.running) watchRunningJob(works.running.job_id);
  }

  async function ensureCatalog(force) {
    try {
      works.catalog = await fetchLibraryCatalog(force);
    } catch (_) {
      works.catalog = works.catalog || {items: []};
    }
  }

  async function load(options) {
    options = options || {};
    var serial = ++works.loadSerial;
    try {
      await Promise.all([loadAvailability(), loadGroupsAndOverview(), ensureCatalog(options.forceCatalog)]);
    } catch (error) {
      if (serial === works.loadSerial) showToast(error.message || '译本对照加载失败', 'danger');
    }
    if (serial !== works.loadSerial) return;
    works.loaded = true;
    if (!works.currentId || !groupById(works.currentId) || works.hiddenGroupIds.has(works.currentId)) {
      var first = visibleGroups()[0];
      works.currentId = first ? first.document_group_id : '';
    }
    renderSidebarEntry();
    if (currentPage === 'works') {
      if (!entryVisible()) { navigateTo('library'); return; }
      render();
      loadPosition(works.currentId);
    }
    if (global.MEFinder.library && libraryStore.loaded) global.MEFinder.library.renderList();
    syncLibraryAssignButton();
  }

  async function refreshAvailability() {
    await loadAvailability();
    renderSidebarEntry();
    syncLibraryAssignButton();
    if (currentPage === 'works') render();
  }

  async function loadPosition(groupId) {
    if (!groupId || works.positions[groupId] !== undefined) return;
    try {
      var data = await requestJSON('/api/translation-works/reading-position?document_group_id=' + encodeURIComponent(groupId));
      works.positions[groupId] = data.position || null;
    } catch (_) {
      works.positions[groupId] = null;
    }
    if (currentPage === 'works' && works.currentId === groupId) render();
  }

  /* ── sidebar & library hooks ─────────────────────────────────── */
  function renderSidebarEntry() {
    var item = document.querySelector('.sidebar-item[data-page="works"]');
    if (!item) return;
    item.hidden = !entryVisible();
    var tag = item.querySelector('.sidebar-item-tag');
    if (tag) tag.hidden = !isReadOnly();
    item.title = isReadOnly() ? '译本对照（只读）' : '译本对照';
  }

  function assignEnabled() {
    return works.loaded && entryVisible();
  }

  function syncLibraryAssignButton() {
    var buttonNode = document.getElementById('library-assign-work-btn');
    if (!buttonNode) return;
    buttonNode.hidden = !assignEnabled();
    buttonNode.disabled = !libraryStore.deleteSelection || libraryStore.deleteSelection.size === 0;
  }

  function workLinkFor(sourceId) {
    if (!assignEnabled()) return null;
    var group = groupForSource(sourceId);
    return group ? {id: group.document_group_id, title: group.title} : null;
  }

  /* ── page rendering ──────────────────────────────────────────── */
  function render() {
    var page = document.getElementById('page-works');
    if (!page) return;
    var listFocus = document.activeElement && document.activeElement.id;
    var workScroll = page.querySelector('.tw-work');
    var keepScroll = workScroll ? workScroll.scrollTop : 0;
    page.replaceChildren(worksList(), workPane());
    var restored = page.querySelector('.tw-work');
    if (restored) restored.scrollTop = keepScroll;
    if (listFocus) {
      var focusTarget = document.getElementById(listFocus);
      if (focusTarget && typeof focusTarget.focus === 'function') {
        focusTarget.focus();
        if (focusTarget.tagName === 'INPUT') {
          var end = focusTarget.value.length;
          try { focusTarget.setSelectionRange(end, end); } catch (_) {}
        }
      }
    }
    if (works.sheetGroupId) renderSheet();
  }

  function worksList() {
    var query = works.query.trim().toLowerCase();
    var groups = visibleGroups().filter(function (group) {
      if (!query) return true;
      var haystack = [group.title].concat((group.members || []).map(function (m) {
        return (m.display_name || '') + ' ' + sourceTitle(m.source_file_id);
      })).join(' ').toLowerCase();
      return haystack.indexOf(query) >= 0;
    });
    var list = el('div', {className: 'tw-work-list', role: 'list'});
    groups.forEach(function (group) {
      var members = group.members || [];
      var languages = [];
      members.forEach(function (member) {
        var name = languageName(sourceLanguageCode(group.document_group_id, member.source_file_id));
        if (languages.indexOf(name) < 0) languages.push(name);
      });
      var running = works.running && works.running.document_group_id === group.document_group_id;
      var current = group.document_group_id === works.currentId;
      list.appendChild(el('button', {
        type: 'button', className: 'tw-work-item', role: 'listitem',
        'aria-current': current ? 'true' : null,
        onclick: function () { selectWork(group.document_group_id); }
      }, [
        el('span', {className: 'tw-work-item-title', text: group.title}),
        el('span', {className: 'tw-work-item-meta', text: running
          ? '对齐生成中'
          : members.length + ' 个版本' + (languages.length ? ' · ' + languages.join('、') : '')})
      ]));
    });
    if (!groups.length) {
      list.appendChild(el('p', {className: 'tw-note tw-list-empty', text: query ? '没有匹配的作品' : '还没有作品'}));
    }
    var head = el('div', {className: 'tw-works-head'}, [
      el('h1', {className: 'tw-works-title', text: '译本对照'}),
      button('新建作品', 'sm', function () { openAssignDialog([], {fromLibrary: false}); })
    ]);
    var search = el('label', {className: 'tw-find'}, [
      svg(ICON_SEARCH, 15),
      el('input', {
        id: 'tw-work-query', type: 'search', placeholder: '搜索作品', 'aria-label': '搜索作品',
        value: works.query, autocomplete: 'off',
        oninput: function (event) { works.query = event.target.value; render(); }
      })
    ]);
    return el('aside', {className: 'tw-works', 'aria-label': '作品'}, [head, search, list, suggestionRow()]);
  }

  function normalizeTitle(value) {
    return cleanTitle(value).toLowerCase().replace(/[\s　·•:：,，.。;；'"“”‘’()（）[\]【】《》<>〈〉!！?？\-—_]/g, '');
  }

  function sameTitleSuggestions() {
    var grouped = new Set();
    works.groups.forEach(function (group) {
      (group.members || []).forEach(function (m) { grouped.add(m.source_file_id); });
    });
    var clusters = {};
    catalogSources().forEach(function (source) {
      if (grouped.has(source.source_file_id)) return;
      var key = normalizeTitle(source.title || source.file_name);
      if (!key) return;
      (clusters[key] = clusters[key] || []).push(source);
    });
    return Object.keys(clusters).map(function (key) { return clusters[key]; })
      .filter(function (sources) {
        if (sources.length < 2) return false;
        var key = sources.map(function (s) { return s.source_file_id; }).sort().join('\n');
        return works.dismissals.indexOf(key) < 0;
      });
  }

  function suggestionRow() {
    if (works.suggestionsHidden || !canGenerate() && works.availability.state !== 'model_missing') return null;
    var suggestion = sameTitleSuggestions()[0];
    if (!suggestion) return null;
    return el('div', {className: 'tw-hint'}, [
      el('p', {className: 'tw-hint-text',
        text: '《' + sourceTitle(suggestion[0].source_file_id) + '》有 ' + suggestion.length + ' 份书名相同的文献，可能是同一作品'}),
      el('div', {className: 'tw-hint-actions'}, [
        button('核对', 'link', function () { openMergeReview(suggestion); }),
        button('忽略', 'link muted', function () { works.suggestionsHidden = true; render(); })
      ])
    ]);
  }

  function selectWork(groupId) {
    works.currentId = groupId;
    render();
    loadPosition(groupId);
  }

  function currentPick(group) {
    var ids = (group.members || []).map(function (m) { return m.source_file_id; });
    var pick = (works.picks[group.document_group_id] || []).filter(function (id) { return ids.indexOf(id) >= 0; });
    works.picks[group.document_group_id] = pick;
    return pick;
  }

  function togglePick(group, sourceId) {
    var pick = currentPick(group);
    var index = pick.indexOf(sourceId);
    if (index >= 0) pick.splice(index, 1);
    else {
      pick.push(sourceId);
      if (pick.length > 2) pick.shift();
    }
    render();
  }

  function workPane() {
    var group = groupById(works.currentId);
    if (!group || works.hiddenGroupIds.has(group.document_group_id)) {
      return el('section', {className: 'tw-work'}, el('div', {className: 'tw-work-inner'},
        el('p', {className: 'tw-note', text: '还没有作品。新建作品，或在文献库里勾选几本后加入作品'})));
    }
    var members = group.members || [];
    var base = members.find(function (m) { return m.is_base; });
    var authorSource = sourceById((base || members[0] || {}).source_file_id);
    var author = authorSource && authorSource.author ? authorSource.author : '';
    var header = el('div', {className: 'tw-work-head'}, [
      el('div', {className: 'tw-work-heading'}, [
        el('h2', {className: 'tw-work-title', text: group.title}),
        el('div', {className: 'tw-work-byline', text: (author ? author + ' · ' : '') + members.length + ' 个版本'})
      ]),
      button('管理版本', '', function () { openSheet(group.document_group_id); })
    ]);
    var inner = el('div', {className: 'tw-work-inner'}, [header]);
    if (isReadOnly()) {
      inner.appendChild(el('p', {className: 'tw-readonly-note'}, [
        '对齐组件已卸载，已有对齐可以阅读 · ',
        button('重新安装', 'link', openAlignmentSettings)
      ]));
    }
    var resume = resumeRow(group);
    if (resume) inner.appendChild(resume);
    inner.appendChild(versionTable(group));
    return el('section', {className: 'tw-work'}, [inner, compareBar(group)]);
  }

  function resumeRow(group) {
    var position = works.positions[group.document_group_id];
    if (!position) return null;
    var ids = (group.members || []).map(function (m) { return m.source_file_id; });
    if (ids.indexOf(position.left_source_file_id) < 0) return null;
    var right = position.right_source_file_id;
    if (right && ids.indexOf(right) < 0) right = null;
    var label = memberName(group, position.left_source_file_id) + (right ? ' 与 ' + memberName(group, right) : '');
    return el('div', {className: 'tw-resume'}, [
      el('div', {className: 'tw-resume-text'}, [
        el('span', {className: 'tw-resume-pair', text: label}),
        el('span', {className: 'tw-resume-at', text: '上次读到' + itemPosition(position.left_source_file_id, position.item_index)})
      ]),
      button('继续阅读', 'primary', function () {
        openReader({
          groupId: group.document_group_id,
          sourceId: position.left_source_file_id,
          compareWith: right || '',
          targetIndex: Number(position.item_index),
          returnLabel: '译本对照'
        });
      })
    ]);
  }

  // 位置按条目序号如实表述（PDF 物理页序或段落序号），不冒充印刷页码。
  function itemPosition(sourceId, index) {
    var source = sourceById(sourceId);
    var number = Number(index) + 1;
    return source && source.source_type === 'pdf' ? ' PDF 第 ' + number + ' 页' : '第 ' + number + ' 段';
  }

  function versionTable(group) {
    var pick = currentPick(group);
    var body = el('tbody');
    (group.members || []).forEach(function (member) {
      var id = member.source_file_id;
      var selected = pick.indexOf(id) >= 0;
      var pages = pageSource(member);
      var checkbox = el('input', {
        type: 'checkbox', className: 'tw-check', checked: selected,
        'aria-label': '选择 ' + (member.display_name || id),
        onchange: function () { togglePick(group, id); }
      });
      checkbox.checked = selected;
      body.appendChild(el('tr', {className: selected ? 'is-selected' : ''}, [
        el('td', {className: 'tw-col-check'}, checkbox),
        el('td', {className: 'tw-col-name'}, [
          el('span', {className: 'tw-version-name'}, [
            member.display_name || sourceTitle(id),
            member.is_base ? el('span', {className: 'tw-base-mark', text: '基准'}) : null
          ]),
          el('span', {className: 'tw-version-sub', text: sourceTitle(id) + ' · ' + sourceFormat(id)})
        ]),
        el('td', {className: 'tw-col-muted tw-col-lang', text: languageName(sourceLanguageCode(group.document_group_id, id))}),
        el('td', {className: 'tw-col-muted tw-col-optional tw-col-pub', text: publicationText(id)}),
        el('td', {className: 'tw-col-muted tw-col-optional tw-col-pages' + (pages.warn ? ' is-warning' : ''), text: pages.label}),
        el('td', {className: 'tw-col-action'}, button('阅读', 'link', function () {
          openReader({groupId: group.document_group_id, sourceId: id, returnLabel: '译本对照'});
        }))
      ]));
    });
    var table = el('table', {className: 'tw-ledger'}, [
      el('caption', {}, ['版本', el('span', {text: '勾选两个进行对照'})]),
      el('thead', {}, el('tr', {}, [
        el('th', {className: 'tw-col-check', scope: 'col'}, el('span', {className: 'tw-visually-hidden', text: '选择'})),
        el('th', {scope: 'col', text: '版本'}),
        el('th', {scope: 'col', className: 'tw-col-lang', text: '语言'}),
        el('th', {scope: 'col', className: 'tw-col-optional', text: '出版'}),
        el('th', {scope: 'col', className: 'tw-col-optional', text: '页码'}),
        el('th', {scope: 'col'}, el('span', {className: 'tw-visually-hidden', text: '操作'}))
      ])),
      body
    ]);
    return el('div', {className: 'tw-ledger-wrap'}, table);
  }

  function statusLine(group, status) {
    var node = el('span', {className: 'tw-state'});
    function put(strong, rest, modifier, title) {
      node.className = 'tw-state' + (modifier ? ' is-' + modifier : '');
      node.appendChild(el('b', {text: strong}));
      if (rest) node.appendChild(document.createTextNode(' · ' + rest));
      if (title) node.title = title;
    }
    if (status.status === 'running') put('生成中', '', 'running');
    else if (status.stale_reason === 'algorithm_unreadable') put('需重新对齐', '算法已更新，旧结果不可读', 'stale');
    else if (status.stale_reason === 'model_changed') put('需重新对齐', '模型已更换，旧结果可读', 'stale');
    else if (status.stale_reason === 'algorithm_updated') put('需重新对齐', '算法已更新，旧结果可读', 'stale');
    else if (status.status === 'direct') {
      var parts = [];
      if (status.matched_segment_ratio != null) parts.push('已匹配段落 ' + Math.round(status.matched_segment_ratio * 100) + '%');
      if (status.review_count) parts.push(status.review_count + ' 处待检查');
      put('直接对齐', parts.join(' · '), 'direct', '已匹配段落是算法在另一版本中找到对应的段落比例，不代表对应一定准确');
    } else if (status.status === 'indirect') {
      put('间接关联', '经「' + memberName(group, status.via_source_file_id) + '」换算，未直接对齐');
    } else put('尚未对齐');
    return node;
  }

  function pairActions(group, a, b, status, compact) {
    var actions = [];
    var blocked = !canGenerate();
    var reason = generateBlockedReason();
    function generate(label, force, style) {
      var node = button(label, style, function () { startAlignment(group, a, b, force); }, {
        disabled: blocked, title: blocked ? reason : null
      });
      actions.push(node);
    }
    if (status.status === 'running') {
      actions.push(button('取消', compact ? 'sm quiet' : 'quiet', cancelAlignment));
      return actions;
    }
    var readable = status.status !== 'none' && status.stale_reason !== 'algorithm_unreadable';
    if (status.status === 'none') generate(compact ? '生成' : '生成对齐', false, compact ? 'sm' : 'primary');
    else if (status.stale_reason) generate('重新对齐', true, compact ? 'sm quiet' : (readable ? 'quiet' : 'primary'));
    else if (status.status === 'indirect') generate('生成直接对齐', false, compact ? 'sm' : 'quiet');
    if (!compact && readable) {
      var hasResume = !!works.positions[group.document_group_id];
      actions.push(button('对照阅读', hasResume ? '' : 'primary', function () {
        openReader({groupId: group.document_group_id, sourceId: a, compareWith: b, returnLabel: '译本对照'});
      }));
    }
    // 禁用必须给出原因：生成类按钮不可用时，在按钮前就地写明并给出设置入口。
    var hasGenerate = status.status === 'none' || !!status.stale_reason || status.status === 'indirect';
    if (blocked && hasGenerate && !compact) {
      actions.unshift(button(works.availability.state === 'model_missing' ? '去设置' : '重新安装', 'link', openAlignmentSettings));
      actions.unshift(el('span', {className: 'tw-state', text: reason}));
    }
    return actions;
  }

  function compareBar(group) {
    var members = group.members || [];
    var pick = currentPick(group);
    var bar = el('div', {className: 'tw-compare-bar', role: 'region', 'aria-label': '对照', 'aria-live': 'polite'});
    if (members.length < 2) {
      bar.appendChild(el('span', {className: 'tw-state', text: '只有一个版本'}));
      bar.appendChild(el('span', {className: 'tw-grow'}));
      bar.appendChild(button('添加版本', '', function () { openSheet(group.document_group_id); }));
      return bar;
    }
    if (pick.length < 2) {
      bar.appendChild(el('span', {className: 'tw-state', text: pick.length ? '再勾选一个版本即可对照' : '勾选两个版本进行对照'}));
      bar.appendChild(el('span', {className: 'tw-grow'}));
      bar.appendChild(button('对照阅读', '', null, {disabled: true}));
      return bar;
    }
    var status = pairStatus(group.document_group_id, pick[0], pick[1]);
    bar.appendChild(el('span', {className: 'tw-pair', text: memberName(group, pick[0]) + ' 与 ' + memberName(group, pick[1])}));
    bar.appendChild(statusLine(group, status));
    bar.appendChild(el('span', {className: 'tw-grow'}));
    pairActions(group, pick[0], pick[1], status, false).forEach(function (node) { bar.appendChild(node); });
    return bar;
  }

  /* ── alignment jobs ──────────────────────────────────────────── */
  function pivotFor(group, a, b) {
    var base = (group.members || []).find(function (m) { return m.is_base; });
    if (base && base.source_file_id === b) return [b, a];
    return [a, b];
  }

  async function startAlignment(group, a, b, force) {
    if (!canGenerate()) { showToast(generateBlockedReason(), 'warning'); return; }
    var order = pivotFor(group, a, b);
    try {
      var data = await postJSON('/api/text-alignments/start', {
        document_group_id: group.document_group_id,
        pivot_source_file_id: order[0],
        target_source_file_id: order[1],
        force: !!force
      });
      works.running = {
        job_id: data.job_id, document_group_id: group.document_group_id,
        pivot_source_file_id: order[0], target_source_file_id: order[1]
      };
      refreshViews();
      watchRunningJob(data.job_id);
    } catch (error) {
      showToast(error.message || '生成对齐失败', 'danger');
    }
  }

  var watchedJobId = '';
  async function watchRunningJob(jobId) {
    if (!jobId || watchedJobId === jobId) return;
    watchedJobId = jobId;
    while (watchedJobId === jobId) {
      await new Promise(function (resolve) { setTimeout(resolve, POSITION_POLL_MS); });
      var response;
      try {
        response = await fetch('/api/text-alignments/status?job_id=' + encodeURIComponent(jobId));
      } catch (_) { continue; }
      if (response.status === 202) continue;
      var payload = {};
      try { payload = await response.json(); } catch (_) {}
      var running = works.running;
      var group = running ? groupById(running.document_group_id) : null;
      watchedJobId = '';
      works.running = null;
      if (response.ok && payload.ok) {
        showToast((group ? '「' + group.title + '」' : '') + '对齐已生成', 'success');
      } else if (payload.cancelled) {
        showToast('已取消生成对齐', 'info');
      } else if (response.status !== 404) {
        showToast(payload.error || '生成对齐失败', 'danger');
      }
      await loadGroupsAndOverview().catch(function () {});
      refreshViews();
      return;
    }
  }

  async function cancelAlignment() {
    try {
      await postJSON('/api/text-alignments/cancel', {});
      showToast('正在取消，当前批次结束后停止', 'info');
    } catch (error) {
      showToast(error.message || '取消失败', 'danger');
    }
  }

  function refreshViews() {
    renderSidebarEntry();
    if (currentPage === 'works') render();
    if (global.MEFinder.library && libraryStore.loaded) global.MEFinder.library.renderList();
    syncLibraryAssignButton();
  }

  /* ── manage sheet ────────────────────────────────────────────── */
  function openSheet(groupId) {
    works.sheetGroupId = groupId;
    works.sheetQuery = '';
    works.sheetReturnFocus = document.activeElement;
    renderSheet();
    var title = document.getElementById('tw-sheet-name');
    if (title) title.focus();
  }

  function closeSheet() {
    works.sheetGroupId = '';
    var scrim = document.getElementById('tw-sheet');
    if (scrim) scrim.remove();
    var back = works.sheetReturnFocus;
    works.sheetReturnFocus = null;
    if (back && back.isConnected && typeof back.focus === 'function') back.focus();
  }

  function renderSheet() {
    var group = groupById(works.sheetGroupId);
    var existing = document.getElementById('tw-sheet');
    if (!group) { if (existing) existing.remove(); works.sheetGroupId = ''; return; }
    var focusId = document.activeElement && document.activeElement.id;
    var scrollTop = existing ? existing.querySelector('.tw-sheet-body').scrollTop : 0;
    var members = group.members || [];
    var body = el('div', {className: 'tw-sheet-body'});

    body.appendChild(el('div', {className: 'tw-sheet-group'}, [
      el('label', {className: 'tw-sheet-label', for: 'tw-sheet-name', text: '作品名称'}),
      el('input', {
        id: 'tw-sheet-name', className: 'tw-field', value: group.title, autocomplete: 'off',
        onchange: function (event) { renameWork(group, event.target.value); },
        onkeydown: function (event) { if (event.key === 'Enter') event.target.blur(); }
      })
    ]));

    var versionGroup = el('div', {className: 'tw-sheet-group', role: 'radiogroup', 'aria-label': '基准版本'},
      el('h4', {className: 'tw-sheet-label', text: '版本'}));
    members.forEach(function (member) {
      var id = member.source_file_id;
      versionGroup.appendChild(el('div', {className: 'tw-member-row'}, [
        el('input', {
          type: 'radio', name: 'tw-base', className: 'tw-radio', checked: member.is_base,
          'aria-label': '设「' + (member.display_name || id) + '」为基准',
          onchange: function () { setBase(group, id); }
        }),
        el('div', {className: 'tw-member-main'}, [
          el('input', {
            id: 'tw-version-' + id, className: 'tw-inline-field', value: member.version_label || '',
            placeholder: member.display_name || sourceTitle(id), 'aria-label': '版本名',
            onchange: function (event) { setVersionLabel(id, event.target.value); },
            onkeydown: function (event) { if (event.key === 'Enter') event.target.blur(); }
          }),
          el('span', {className: 'tw-version-sub',
            text: sourceTitle(id) + ' · ' + languageName(sourceLanguageCode(group.document_group_id, id)) + ' · ' + sourceFormat(id)})
        ]),
        button('移出', 'link muted', function () { removeMember(group, id); })
      ]));
    });
    versionGroup.appendChild(el('p', {className: 'tw-note', text: '圆点标记基准版本，其他版本默认与它直接对齐'}));
    versionGroup.appendChild(el('label', {className: 'tw-find tw-find-spaced'}, [
      svg(ICON_SEARCH, 15),
      el('input', {
        id: 'tw-sheet-add', type: 'search', placeholder: '添加版本：搜索文献库', 'aria-label': '添加版本',
        value: works.sheetQuery, autocomplete: 'off',
        oninput: function (event) { works.sheetQuery = event.target.value; renderSheet(); }
      })
    ]));
    var query = works.sheetQuery.trim().toLowerCase();
    if (query) {
      var memberIds = new Set(members.map(function (m) { return m.source_file_id; }));
      var found = catalogSources().filter(function (source) {
        if (memberIds.has(source.source_file_id)) return false;
        return [source.title, source.author, source.file_name].join(' ').toLowerCase().indexOf(query) >= 0;
      }).slice(0, 30);
      var results = el('div', {className: 'tw-results', role: 'list'});
      found.forEach(function (source) {
        var other = groupForSource(source.source_file_id);
        results.appendChild(el('button', {
          type: 'button', className: 'tw-result', role: 'listitem',
          onclick: function () { addVersion(group, source.source_file_id, other); }
        }, [
          el('span', {className: 'tw-result-title', text: sourceTitle(source.source_file_id)}),
          el('small', {text: other ? '在「' + other.title + '」' : languageName(source.language_code) + ' · ' + sourceFormatLabel(source)})
        ]));
      });
      if (!found.length) results.appendChild(el('p', {className: 'tw-note', text: '没有匹配的文献'}));
      versionGroup.appendChild(results);
    }
    body.appendChild(versionGroup);

    if (members.length > 1) {
      var alignGroup = el('div', {className: 'tw-sheet-group'}, el('h4', {className: 'tw-sheet-label', text: '对齐'}));
      for (var i = 0; i < members.length; i += 1) {
        for (var j = i + 1; j < members.length; j += 1) {
          var a = members[i].source_file_id;
          var b = members[j].source_file_id;
          var status = pairStatus(group.document_group_id, a, b);
          var row = el('div', {className: 'tw-pair-row'}, [
            el('span', {className: 'tw-pair-name', text: memberName(group, a) + ' 与 ' + memberName(group, b)})
          ]);
          var actionBox = el('span', {className: 'tw-pair-actions'});
          pairActions(group, a, b, status, true).forEach(function (node) { actionBox.appendChild(node); });
          row.appendChild(actionBox);
          row.appendChild(el('div', {className: 'tw-pair-state'}, statusLine(group, status)));
          alignGroup.appendChild(row);
        }
      }
      if (!canGenerate()) alignGroup.appendChild(el('p', {className: 'tw-note', text: generateBlockedReason()}));
      body.appendChild(alignGroup);
    }

    body.appendChild(el('div', {className: 'tw-sheet-group'}, [
      button('删除作品', 'danger', function () { deleteWork(group); }),
      el('p', {className: 'tw-note', text: '解除归组，文献保留在文献库。本作品的对齐与人工校正会一并删除'})
    ]));

    var sheet = el('aside', {
      className: 'tw-sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'tw-sheet-title',
      onkeydown: function (event) {
        if (event.key === 'Escape') { event.stopPropagation(); closeSheet(); }
        if (event.key === 'Tab') trapFocus(event, sheet);
      }
    }, [
      el('div', {className: 'tw-sheet-head'}, [
        el('h3', {id: 'tw-sheet-title', text: '管理版本'}),
        el('button', {type: 'button', className: 'tw-icon-btn', 'aria-label': '关闭', onclick: closeSheet}, svg(ICON_CLOSE, 14))
      ]),
      body
    ]);
    // 抽屉只在首次打开时滑入；输入导致的重绘不再重复入场动画。
    if (existing) sheet.classList.add('is-settled');
    var scrim = el('div', {
      id: 'tw-sheet', className: 'tw-sheet-scrim',
      onclick: function (event) { if (event.target === scrim) closeSheet(); }
    }, sheet);
    if (existing) existing.replaceWith(scrim);
    else document.body.appendChild(scrim);
    scrim.querySelector('.tw-sheet-body').scrollTop = scrollTop;
    if (focusId && existing) {
      var focusNode = document.getElementById(focusId);
      if (focusNode) {
        focusNode.focus();
        if (focusNode.tagName === 'INPUT' && focusNode.type === 'search') {
          var end = focusNode.value.length;
          try { focusNode.setSelectionRange(end, end); } catch (_) {}
        }
      }
    }
  }

  function trapFocus(event, container) {
    var focusable = Array.prototype.filter.call(
      container.querySelectorAll('button, input, [tabindex]:not([tabindex="-1"])'),
      function (node) { return !node.disabled && node.offsetParent !== null; }
    );
    if (!focusable.length) return;
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  async function mutate(url, payload, success) {
    try {
      var result = await postJSON(url, payload);
      await loadGroupsAndOverview();
      if (success) showToast(success, 'success');
      refreshViews();
      if (works.sheetGroupId) renderSheet();
      return result;
    } catch (error) {
      showToast(error.message || '操作失败', 'danger');
      return null;
    }
  }

  function renameWork(group, value) {
    var title = String(value || '').trim();
    if (!title || title === group.title) return;
    mutate('/api/document-groups/rename', {document_group_id: group.document_group_id, title: title});
  }

  function setVersionLabel(sourceId, value) {
    mutate('/api/document-groups/version-label', {source_file_id: sourceId, version_label: String(value || '').trim()});
  }

  function alignmentCount(group) {
    return (group.alignments || []).length;
  }

  async function setBase(group, sourceId) {
    var count = alignmentCount(group);
    if (count) {
      var confirmed = await showAppConfirm(
        '更换基准会删除本作品已有的 ' + count + ' 个对齐结果，之后需要重新生成',
        {title: '更换基准版本？', confirmText: '更换基准', tone: 'warning'}
      );
      if (!confirmed) { renderSheet(); return; }
    }
    mutate('/api/document-groups/set-base', {document_group_id: group.document_group_id, base_source_file_id: sourceId});
  }

  function memberAlignmentCount(group, sourceId) {
    return (group.alignments || []).filter(function (run) {
      return run.pivot_source_file_id === sourceId || run.target_source_file_id === sourceId;
    }).length;
  }

  async function removeMember(group, sourceId) {
    var count = memberAlignmentCount(group, sourceId);
    if (count) {
      var confirmed = await showAppConfirm(
        '移出后，这个版本参与的 ' + count + ' 个对齐结果会被删除。文献仍保留在文献库',
        {title: '移出「' + memberName(group, sourceId) + '」？', confirmText: '移出', tone: 'warning'}
      );
      if (!confirmed) return;
    }
    mutate('/api/document-groups/remove-member', {source_file_id: sourceId}, '已移出作品，文献仍在文献库');
  }

  async function addVersion(group, sourceId, otherGroup) {
    if (otherGroup) {
      var count = memberAlignmentCount(otherGroup, sourceId);
      var confirmed = await showAppConfirm(
        '这本书在「' + otherGroup.title + '」中。一本书只属于一个作品，移过来会从原作品移出'
          + (count ? '，并删除它在原作品中的 ' + count + ' 个对齐结果' : ''),
        {title: '移到「' + group.title + '」？', confirmText: '移过来', tone: 'warning'}
      );
      if (!confirmed) return;
    }
    works.sheetQuery = '';
    mutate('/api/document-groups/move-members', {
      document_group_id: group.document_group_id, source_file_ids: [sourceId]
    }, '已添加版本');
  }

  // 删除作品：先在界面上隐藏，提示条消失后才真正删除；期间点「撤销」不会发出删除请求。
  function deleteWork(group) {
    var groupId = group.document_group_id;
    closeSheet();
    works.hiddenGroupIds.add(groupId);
    if (works.currentId === groupId) {
      var next = visibleGroups()[0];
      works.currentId = next ? next.document_group_id : '';
    }
    refreshViews();
    toastWithAction('作品「' + group.title + '」已删除，文献保留', '撤销', function () {
      works.hiddenGroupIds.delete(groupId);
      works.currentId = groupId;
      refreshViews();
    }, async function () {
      try {
        await postJSON('/api/document-groups/delete', {document_group_id: groupId});
      } catch (error) {
        showToast(error.message || '删除作品失败', 'danger');
      }
      works.hiddenGroupIds.delete(groupId);
      await loadGroupsAndOverview().catch(function () {});
      refreshViews();
    });
  }

  /* ── dialogs ─────────────────────────────────────────────────── */
  function openDialog(content, labelId) {
    closeDialog();
    var returnFocus = document.activeElement;
    var dialog = el('div', {
      className: 'tw-dialog', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': labelId,
      onkeydown: function (event) {
        if (event.key === 'Escape' && !document.querySelector('.app-select.is-open')) {
          event.stopPropagation();
          closeDialog();
        }
        if (event.key === 'Tab') trapFocus(event, dialog);
      }
    }, content);
    var scrim = el('div', {
      id: 'tw-dialog', className: 'tw-dialog-scrim',
      onclick: function (event) { if (event.target === scrim) closeDialog(); }
    }, dialog);
    scrim.returnFocus = returnFocus;
    document.body.appendChild(scrim);
    return dialog;
  }

  function closeDialog() {
    var scrim = document.getElementById('tw-dialog');
    if (!scrim) return;
    var back = scrim.returnFocus;
    scrim.remove();
    if (back && back.isConnected && typeof back.focus === 'function') back.focus();
  }

  function openAssignDialog(sourceIds, options) {
    options = options || {};
    var state = {
      target: 'new',
      selected: (sourceIds || []).slice(),
      name: sourceIds && sourceIds.length ? sourceTitle(sourceIds[0]).replace(/[（(].*$/, '').trim() : '',
      query: '',
      fromLibrary: !!options.fromLibrary,
      busy: false
    };
    var dialog = openDialog(el('div'), 'tw-assign-title');
    function draw() {
      var focusId = document.activeElement && document.activeElement.id;
      var movers = state.selected.filter(function (id) {
        var other = groupForSource(id);
        return other && other.document_group_id !== state.target;
      });
      var targetGroup = groupById(state.target);
      var select = el('div', {className: 'app-select tw-select', id: 'tw-assign-target'}, [
        el('button', {
          type: 'button', className: 'app-select-trigger', 'aria-haspopup': 'listbox', 'aria-expanded': 'false',
          'aria-labelledby': 'tw-assign-target-label tw-assign-target-value',
          onclick: function (event) {
            // 弹窗正文可滚动：菜单用 fixed 定位跟随触发器，不被容器裁切。
            openVersionSelect(event, 'tw-assign-target');
            var wrap = document.getElementById('tw-assign-target');
            if (!wrap || !wrap.classList.contains('is-open')) return;
            event.currentTarget.setAttribute('aria-expanded', 'true');
            var current = wrap.querySelector('.app-select-option.is-selected') || wrap.querySelector('.app-select-option');
            if (current) current.focus();
          }
        }, [
          el('span', {className: 'app-select-value', id: 'tw-assign-target-value', text: targetGroup ? targetGroup.title : '新建作品'}),
          svg(ICON_CHEVRON, 14)
        ]),
        (function () {
          var menu = el('div', {className: 'app-select-menu tw-select-menu', role: 'listbox'});
          function option(label, value, meta) {
            menu.appendChild(el('button', {
              type: 'button', className: 'app-select-option' + (state.target === value ? ' is-selected' : ''),
              role: 'option', 'aria-selected': state.target === value ? 'true' : 'false',
              onclick: function (event) {
                event.stopPropagation();
                closeAppSelects();
                state.target = value;
                draw();
                var trigger = dialog.querySelector('#tw-assign-target .app-select-trigger');
                if (trigger) trigger.focus();
              }
            }, [el('span', {className: 'tw-option-label', text: label}), meta ? el('small', {text: meta}) : null]));
          }
          option('新建作品', 'new');
          visibleGroups().forEach(function (group) {
            option(group.title, group.document_group_id, (group.members || []).length + ' 个版本');
          });
          return menu;
        })()
      ]);
      var query = state.query.trim().toLowerCase();
      var listSources = catalogSources().filter(function (source) {
        if (state.selected.indexOf(source.source_file_id) >= 0) return true;
        if (!query) return false;
        return [source.title, source.author, source.file_name].join(' ').toLowerCase().indexOf(query) >= 0;
      }).sort(function (x, y) {
        return (state.selected.indexOf(y.source_file_id) >= 0) - (state.selected.indexOf(x.source_file_id) >= 0);
      }).slice(0, 60);
      var list = el('div', {className: 'tw-pick-list', role: 'group', 'aria-label': '文献'});
      listSources.forEach(function (source) {
        var id = source.source_file_id;
        var checked = state.selected.indexOf(id) >= 0;
        var other = groupForSource(id);
        var box = el('input', {
          type: 'checkbox', className: 'tw-check', id: 'tw-pick-' + id, checked: checked,
          onchange: function () {
            var index = state.selected.indexOf(id);
            if (index >= 0) state.selected.splice(index, 1);
            else {
              state.selected.push(id);
              if (!state.name) state.name = sourceTitle(id).replace(/[（(].*$/, '').trim();
            }
            draw();
          }
        });
        box.checked = checked;
        list.appendChild(el('label', {className: 'tw-pick-row', for: 'tw-pick-' + id}, [
          box,
          el('span', {className: 'tw-pick-main'}, [
            el('span', {className: 'tw-pick-title', text: sourceTitle(id)}),
            el('span', {className: 'tw-version-sub', text: [source.author || '作者信息待完善', languageName(source.language_code), sourceFormatLabel(source)].join(' · ')})
          ]),
          el('span', {className: 'tw-version-sub', text: other ? '在「' + other.title + '」' : ''})
        ]));
      });
      if (!listSources.length) list.appendChild(el('p', {className: 'tw-note', text: query ? '没有匹配的文献' : '搜索文献库，勾选要加入的书'}));
      var ready = state.selected.length > 0 && (state.target !== 'new' || state.name.trim());
      var moverAlignments = movers.reduce(function (sum, id) {
        return sum + memberAlignmentCount(groupForSource(id), id);
      }, 0);
      dialog.replaceChildren(
        el('div', {className: 'tw-dialog-head'}, el('h3', {id: 'tw-assign-title', text: state.fromLibrary ? '加入作品' : '新建作品'})),
        el('div', {className: 'tw-dialog-body'}, [
          el('div', {className: 'tw-assign-grid'}, [
            el('div', {}, [el('span', {className: 'tw-sheet-label', id: 'tw-assign-target-label', text: '加入到'}), select]),
            state.target === 'new' ? el('label', {}, [
              el('span', {className: 'tw-sheet-label', text: '作品名称'}),
              el('input', {
                id: 'tw-assign-name', className: 'tw-field', value: state.name, autocomplete: 'off',
                placeholder: '例如：资本论 · 第一卷',
                oninput: function (event) {
                  state.name = event.target.value;
                  var submit = dialog.querySelector('.tw-dialog-submit');
                  if (submit) submit.disabled = !(state.selected.length && state.name.trim());
                }
              })
            ]) : el('span')
          ]),
          el('p', {className: 'tw-sheet-label tw-spaced',
            text: '文献 · 已选 ' + state.selected.length + ' 本' + (state.target === 'new' && state.selected.length ? '，第一本作为基准' : '')}),
          el('label', {className: 'tw-find'}, [
            svg(ICON_SEARCH, 15),
            el('input', {
              id: 'tw-assign-query', type: 'search', placeholder: '搜索文献库', 'aria-label': '搜索文献库',
              value: state.query, autocomplete: 'off',
              oninput: function (event) { state.query = event.target.value; draw(); }
            })
          ]),
          list,
          movers.length ? el('p', {className: 'tw-note is-warning',
            text: movers.length + ' 本已在其他作品中，确认后会移到这里' + (moverAlignments ? '，并删除它们在原作品中的 ' + moverAlignments + ' 个对齐结果' : '')}) : null
        ]),
        el('div', {className: 'tw-dialog-foot'}, [
          button('取消', 'quiet', closeDialog),
          button(state.target === 'new' ? '新建' : '加入', 'primary tw-dialog-submit', submit, {disabled: !ready || state.busy})
        ])
      );
      var focusNode = focusId && dialog.querySelector('#' + CSS.escape(focusId));
      if (focusNode) {
        focusNode.focus();
        if (focusNode.tagName === 'INPUT' && focusNode.type !== 'checkbox') {
          var end = focusNode.value.length;
          try { focusNode.setSelectionRange(end, end); } catch (_) {}
        }
      } else {
        var first = dialog.querySelector('#tw-assign-name') || dialog.querySelector('.app-select-trigger');
        if (first) first.focus();
      }
    }
    async function submit() {
      if (state.busy) return;
      state.busy = true;
      draw();
      var payload = {source_file_ids: state.selected.slice()};
      if (state.target === 'new') payload.title = state.name.trim();
      else payload.document_group_id = state.target;
      try {
        var result = await postJSON('/api/document-groups/move-members', payload);
        var work = result.result || {};
        closeDialog();
        await loadGroupsAndOverview();
        if (state.fromLibrary) {
          clearLibrarySelection();
          refreshViews();
          toastWithAction('已加入「' + (work.title || '') + '」', '前往译本对照', function () {
            openWork(work.document_group_id);
          });
        } else {
          works.currentId = work.document_group_id;
          refreshViews();
        }
      } catch (error) {
        state.busy = false;
        draw();
        showToast(error.message || '加入作品失败', 'danger');
      }
    }
    draw();
  }

  function openMergeReview(sources) {
    var ids = sources.map(function (s) { return s.source_file_id; });
    var columns = sources.slice(0, 4);
    function value(source, key) {
      if (key === 'title') return sourceTitle(source.source_file_id);
      if (key === 'author') return source.author || '作者信息待完善';
      if (key === 'format') return sourceFormatLabel(source);
      if (key === 'length') return source.source_type === 'pdf'
        ? (source.page_count ? source.page_count + ' 页' : '页数未知')
        : (source.works_count || 1) + ' 篇';
      if (key === 'pages') return pageSource({source_file_id: source.source_file_id, epub_publisher_pages: null}).label;
      return '';
    }
    var rows = [['title', '书名'], ['author', '作者'], ['format', '格式'], ['length', '篇幅'], ['pages', '页码']];
    var tbody = el('tbody');
    rows.forEach(function (row) {
      var values = columns.map(function (source) { return value(source, row[0]); });
      var differs = values.some(function (v) { return v !== values[0]; });
      tbody.appendChild(el('tr', {}, [el('th', {scope: 'row', text: row[1]})].concat(values.map(function (v) {
        return el('td', {className: differs ? 'is-different' : ''}, [v, differs ? el('span', {className: 'tw-visually-hidden', text: '（不一致）'}) : null]);
      }))));
    });
    var busy = false;
    openDialog([
      el('div', {className: 'tw-dialog-head'}, [
        el('h3', {id: 'tw-merge-title', text: '是同一部作品吗？'}),
        el('p', {className: 'tw-note', text: '书名相同只是线索，不一致的项已标出'})
      ]),
      el('div', {className: 'tw-dialog-body'}, el('div', {className: 'tw-ledger-wrap'}, el('table', {className: 'tw-compare-table'}, tbody))),
      el('div', {className: 'tw-dialog-foot'}, [
        button('不是同一作品', 'quiet tw-push-left', async function () {
          if (busy) return;
          busy = true;
          try {
            await postJSON('/api/translation-works/dismiss-suggestion', {source_file_ids: ids});
            works.dismissals.push(ids.slice().sort().join('\n'));
            closeDialog();
            render();
            showToast('已记住，不再提示这组文献', 'success');
          } catch (error) {
            busy = false;
            showToast(error.message || '保存失败', 'danger');
          }
        }),
        button('稍后', '', function () { works.suggestionsHidden = true; closeDialog(); render(); }),
        button('归为一部作品', 'primary', async function () {
          if (busy) return;
          busy = true;
          try {
            var result = await postJSON('/api/document-groups/move-members', {
              title: sourceTitle(ids[0]).replace(/[（(].*$/, '').trim(), source_file_ids: ids
            });
            closeDialog();
            await loadGroupsAndOverview();
            works.currentId = (result.result || {}).document_group_id || works.currentId;
            refreshViews();
          } catch (error) {
            busy = false;
            showToast(error.message || '归组失败', 'danger');
          }
        })
      ])
    ], 'tw-merge-title');
  }

  /* ── navigation & reader ─────────────────────────────────────── */
  function openAlignmentSettings() {
    closeSheet();
    navigateTo('settings');
    if (typeof showSettingsCategory === 'function') showSettingsCategory('text-alignment-settings');
  }

  function openWork(groupId) {
    if (groupId) works.currentId = groupId;
    navigateTo('works');
  }

  function openReader(options) {
    var reader = global.MEFinderReader;
    if (!reader) { showToast('阅读器未加载', 'danger'); return; }
    reader.open(options).catch(function (error) {
      showToast(error.message || '打开阅读器失败', 'danger');
    });
  }

  function openReaderFromWindow(options) {
    if (!options || typeof options !== 'object') return;
    openReader(Object.assign({}, options, {returnLabel: options.returnLabel || '译本对照', noExternal: true}));
  }

  function readFromLibrary(sourceId) {
    if (!sourceId) return;
    openReader({sourceId: sourceId, returnLabel: '文献库'});
  }

  function assignSelection() {
    var ids = Array.from(libraryStore.deleteSelection || []);
    if (!ids.length) return;
    openAssignDialog(ids, {fromLibrary: true});
  }

  // 阅读器（主窗口内）需要的宿主能力：侧栏收成图标栏、管理版本、安装入口、新窗口。
  var sidebarBeforeReader = null;
  function onReaderOpenChange(open) {
    var root = document.documentElement;
    if (open) {
      if (sidebarBeforeReader === null) sidebarBeforeReader = root.classList.contains('sidebar-collapsed');
      root.classList.add('sidebar-collapsed');
    } else {
      if (sidebarBeforeReader !== null) {
        root.classList.toggle('sidebar-collapsed', sidebarBeforeReader);
        sidebarBeforeReader = null;
      }
      // 阅读器关闭时会写入最新位置；稍后重新读取，让「继续阅读」反映这次阅读。
      works.positions = {};
      setTimeout(function () {
        if (currentPage === 'works' && works.currentId) loadPosition(works.currentId);
      }, 400);
    }
  }

  function canOpenNativeWindow() {
    return (desktopShell === 'macos' || desktopShell === 'win32') &&
      !!(global.pywebview && global.pywebview.api && typeof global.pywebview.api.open_reader === 'function');
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (!global.MEFinderReader) return;
    global.MEFinderReader.configure({
      onOpenChange: onReaderOpenChange,
      onManageWork: function (groupId) {
        global.MEFinderReader.close();
        openWork(groupId);
        if (groupId) {
          var wait = works.loaded ? Promise.resolve() : load();
          wait.then(function () { openSheet(groupId); });
        }
      },
      onInstallComponent: function () { global.MEFinderReader.close(); openAlignmentSettings(); },
      onFindInWork: function (groupId) {
        global.MEFinderReader.close();
        searchStore.documentId = '';
        searchStore.groupId = groupId;
        updateSearchDocumentLabel();
        navigateTo('search');
        var input = document.getElementById('query');
        if (input) input.focus();
      },
      openInNewWindow: function (options) {
        if (!canOpenNativeWindow()) return Promise.resolve(false);
        return global.pywebview.api.open_reader(JSON.parse(JSON.stringify(options)));
      },
      canOpenInNewWindow: canOpenNativeWindow,
      availability: function () { return works.availability.state; }
    });
  }, {once: true});

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.works = {
    load: load,
    refreshAvailability: refreshAvailability,
    open: openWork,
    openSheet: openSheet,
    assignSelection: assignSelection,
    readFromLibrary: readFromLibrary,
    openReaderFromWindow: openReaderFromWindow,
    workLinkFor: workLinkFor,
    syncLibraryAssignButton: syncLibraryAssignButton,
    closeOverlays: function () { closeDialog(); if (works.sheetGroupId) closeSheet(); }
  };
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
