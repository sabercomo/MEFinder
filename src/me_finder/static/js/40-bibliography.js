/* IIFE 包裹：书目状态与实现保持私有，仅导出动态事件入口和命名模块 API。
   node 白盒测试走 module.exports；IIFE 实参在 node 下退回 globalThis。 */
(function (global) {  // module: 40-bibliography.js
  function bibNode(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function bibSvg(paths, className) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    if (className) svg.setAttribute('class', className);
    [['viewBox','0 0 20 20'], ['fill','none'], ['stroke','currentColor'], ['stroke-width','1.8'],
      ['stroke-linecap','round'], ['stroke-linejoin','round'], ['aria-hidden','true']].forEach(function(pair) {
      svg.setAttribute(pair[0], pair[1]);
    });
    paths.forEach(function(d) {
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    });
    return svg;
  }

  function bibAction(label, name, sourceId, primary, small) {
    var button = bibNode('button', 'action-btn' + (small ? ' sm' : '') + (primary ? ' primary' : ''), label);
    button.type = 'button';
    button.dataset.action = name;
    button.dataset.sourceId = sourceId;
    return button;
  }

  function bibMissingBadge(meta) {
    var text = bibliographicMissingText(meta);
    if (!text) return null;
    var badge = bibNode('span', 'bibliographic-missing');
    badge.title = 'ISBN、ISSN 与 DOI 不计入引文必需字段';
    var icon = bibSvg(['M12 7.5v5.5', 'M12 16.5h.01']);
    icon.setAttribute('viewBox', '0 0 24 24');
    var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx','12'); circle.setAttribute('cy','12'); circle.setAttribute('r','9');
    icon.insertBefore(circle, icon.firstChild);
    badge.appendChild(icon);
    badge.appendChild(bibNode('span', null, text));
    return badge;
  }
  // bibliographicFieldLabels / bibliographicDocType / bibliographicEditorDocType /
  // bibliographicMissingFields 已抽到 06-pure.js（纯逻辑，可单测）。



  let bibEditorTypeOverride = {};
  let bibLookupSource = {};
  let cnkiLookupState = {};
  let bookLookupState = {};
  let crossrefLookupState = {};
  let bibliographicPendingEvidence = {};
  // 每份文献一个字段缓存：切换文献类型时，不属于新类型的字段（如期刊切图书时的
  // 刊名/卷/期/页/DOI/ISSN）在 DOM 里消失，但仍留在这里，保存时随类型一并回填，
  // 避免“切类型 + 保存”把另一类型字段静默清空（数据丢失）。
  let bibFieldCache = {};
  // 书目编辑器是否有未保存修改：改字段 / 切类型 / 联网回填 / 自动识别都会置脏，
  // 保存或切换到别的文献才清零。离开详情前统一用 guardLeaveDetail 拦一道，
  // 避免关抽屉、点另一条文献、点顶部状态筛选时把手填内容静默丢掉。
  let bibEditorDirty = false;
  // 书目区默认查看态（label:value 只读），点「编辑」才进编辑态（输入表单）。
  // 每份文献一个开关，保存/取消/换文献回到查看态。语义仍是显式保存 + dirty 保护。
  let bibEditMode = {};
  const BIBLIOGRAPHIC_CACHE_FIELDS = ['author','country','title','translator','publish_place','publisher','publish_year','isbn','journal_name','volume','issue','page_range','doi','issn'];
  // 人工语言选项：自动识别失败（如英译本判成未识别）时手动指定。空值＝跟随自动识别。
  const BIB_LANGUAGE_OPTIONS = [['zh-Hans','简体中文'],['zh-Hant','繁体中文'],['en','English'],['de','Deutsch'],['fr','Français'],['es','Español'],['it','Italiano'],['pt','Português'],['ru','Русский'],['ja','日本語'],['ko','한국어']];
  function bibLanguageLabel(code) {
    var hit = BIB_LANGUAGE_OPTIONS.find(function(o){ return o[0] === String(code || ''); });
    return hit ? hit[1] : (code === 'und' ? '未识别语言' : '');
  }
  // 编辑态语言选择：复用 app 自定义下拉（.app-select），主题化、箭头内嵌，
  // 用 fixed 定位菜单（openVersionSelect）避免被抽屉滚动容器裁切；
  // 首项「自动识别」＝清除人工覆盖，并把自动判定的语言标出来。隐藏 input 承载取值。
  function bibLanguageFieldNode(src, full) {
    var manual = String((src && src.language_code_manual) || '');
    var autoLabel = bibLanguageLabel(src && src.language_code_auto) || '未识别语言';
    var autoOptLabel = '自动识别（' + autoLabel + '）';
    var field = bibNode('div', 'bibliographic-field' + (full ? ' full' : ''));
    field.dataset.metadataField = 'language';
    var label = bibNode('label', null, '语言');
    label.htmlFor = 'bib-language-trigger';
    field.appendChild(label);
    var hidden = document.createElement('input');
    hidden.type = 'hidden'; hidden.id = 'bib-language'; hidden.value = manual;
    field.appendChild(hidden);
    var select = bibNode('div', 'app-select bib-language-select');
    select.id = 'bib-language-select';
    var trigger = bibNode('button', 'app-select-trigger');
    trigger.id = 'bib-language-trigger'; trigger.type = 'button';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.dataset.action = 'openBibLanguageSelect';
    trigger.dataset.selectId = select.id;
    var value = bibNode('span', 'app-select-value', manual ? bibLanguageLabel(manual) : autoOptLabel);
    value.id = 'bib-language-value';
    trigger.appendChild(value);
    trigger.appendChild(bibSvg(['m6 8 4 4 4-4']));
    select.appendChild(trigger);
    var menu = bibNode('div', 'app-select-menu bib-language-menu');
    menu.setAttribute('role', 'listbox');
    [['', autoOptLabel]].concat(BIB_LANGUAGE_OPTIONS).forEach(function(option) {
      var code = option[0], text = option[1];
      var button = bibNode('button', 'app-select-option' + (code === manual ? ' is-selected' : ''), text);
      button.type = 'button';
      button.setAttribute('role', 'option');
      button.dataset.action = 'pickBibLanguage';
      button.dataset.code = code;
      button.dataset.label = text;
      menu.appendChild(button);
    });
    select.appendChild(menu);
    field.appendChild(select);
    return field;
  }

  // 选语言：写隐藏 input（供 collectBibliographicForm 读取）+ 更新触发器文案与选中态。
  function pickBibLanguage(event, code, label, button) {
    if (event) event.stopPropagation();
    var hidden = document.getElementById('bib-language');
    if (hidden) hidden.value = code;
    var value = document.getElementById('bib-language-value');
    if (value) value.textContent = label;
    var menu = document.querySelector('#bib-language-select .app-select-menu');
    if (menu) menu.querySelectorAll('.app-select-option').forEach(function(o){ o.classList.remove('is-selected'); });
    button.classList.add('is-selected');
    if (typeof closeAppSelects === 'function') closeAppSelects();
  }
  function bibFieldCacheFromMeta(meta) {
    var out = {};
    BIBLIOGRAPHIC_CACHE_FIELDS.forEach(function(field) { out[field] = String((meta && meta[field]) || '').trim(); });
    return out;
  }

  function bibliographicEditorNode(src) {
    var meta = sourceBibliographicMetadata(src);
    var docType = bibEditorTypeOverride[src.source_file_id] || bibliographicDocType(meta);
    var editorDocType = bibliographicEditorDocType(docType);
    var missingMeta = Object.assign({}, meta, {document_type: docType,
      metadata_missing_fields: docType === bibliographicDocType(meta) ? meta.metadata_missing_fields : null});
    var missing = bibliographicMissingFields(missingMeta);
    var sid = src.source_file_id;
    var isJournal = docType === 'journal_article';
    var isBook = docType === 'book' || docType === 'translated_book';
    var canDetect = src.source_type === 'pdf';
    var editor = bibNode('div');
    editor.id = 'bibliographic-editor';
    editor.appendChild(bibNode('div', 'drawer-section-title', '书目信息'));
    var types = bibNode('div', 'segmented-control bibliographic-type-control');
    types.id = 'bib-doctype-control';
    types.setAttribute('role', 'group');
    types.setAttribute('aria-label', '文献类型');
    [['book','著作'], ['journal_article','期刊论文'], ['thesis','学位论文']].forEach(function(type) {
      var button = bibNode('button', 'seg-btn' + (editorDocType === type[0] ? ' active' : ''), type[1]);
      button.type = 'button';
      button.dataset.doctype = type[0];
      button.dataset.action = 'setBibliographicType';
      button.dataset.sourceId = sid;
      types.appendChild(button);
    });
    editor.appendChild(types);
    var badge = bibMissingBadge(missingMeta);
    if (badge) editor.appendChild(badge);
    var grid = bibNode('div', 'bibliographic-grid');
    function field(id, key, label, full) {
      var isMissing = missing.indexOf(key) >= 0;
      var node = bibNode('div', 'bibliographic-field' + (full ? ' full' : '') + (isMissing ? ' is-missing' : ''));
      node.dataset.metadataField = key;
      var labelNode = bibNode('label', null, label + (isMissing ? ' · 缺少' : ''));
      labelNode.htmlFor = 'bib-' + id;
      node.appendChild(labelNode);
      var input = document.createElement('input');
      input.id = 'bib-' + id;
      input.value = String(meta[key] || '');
      node.appendChild(input);
      grid.appendChild(node);
    }
    var fields = docType === 'thesis'
      ? [['author','author','作者',false], ['title','title','篇名',true], ['publisher','publisher','学校',false], ['publish-year','publish_year','年份',false]]
      : isJournal
        ? [['title','title','标题（篇名）',true], ['author','author','作者',false], ['journal-name','journal_name','出版刊物',false],
          ['volume','volume','卷次',false], ['issue','issue','期号',false], ['publish-year','publish_year','时间（年份）',false],
          ['page-range','page_range','页码（起止页）',false], ['doi','doi','DOI',false], ['issn','issn','ISSN',false]]
        : [['author','author','作者',false], ['country','country','国别',false], ['title','title','书名',true],
          ['translator','translator','译者',false], ['publish-place','publish_place','出版地',false],
          ['publisher','publisher','出版社',false], ['publish-year','publish_year','出版年份',false], ['isbn','isbn','ISBN',false]];
    fields.forEach(function(spec) { field(spec[0], spec[1], spec[2], spec[3]); });
    grid.appendChild(bibLanguageFieldNode(src, docType === 'thesis' || isJournal));
    editor.appendChild(grid);
    var toolbar = bibNode('div', 'bib-toolbar');
    if (isJournal) {
      var lookupSource = bibLookupSource[sid] || 'auto';
      var wrap = bibNode('span', 'bib-menu-wrap');
      var split = bibNode('span', 'bib-split');
      var primary = bibAction(bibPrimaryLabel(lookupSource), 'bibRunLookup', sid, true);
      primary.classList.add('bib-main'); primary.id = 'bib-primary-btn';
      split.appendChild(primary);
      var caret = bibNode('button', 'action-btn primary bib-caret');
      caret.type = 'button';
      caret.setAttribute('aria-label', '选择补全方式');
      caret.setAttribute('aria-haspopup', 'true');
      caret.dataset.action = 'bibToggleMenu';
      caret.dataset.menuId = 'bib-source-menu';
      var arrow = bibSvg(['M6 9l6 6 6-6']);
      arrow.setAttribute('viewBox', '0 0 24 24'); arrow.setAttribute('width','14'); arrow.setAttribute('height','14');
      arrow.setAttribute('stroke-width','2');
      caret.appendChild(arrow);
      split.appendChild(caret);
      wrap.appendChild(split);
      var sourceMenu = bibNode('span', 'bib-menu');
      sourceMenu.id = 'bib-source-menu';
      sourceMenu.setAttribute('role', 'menu');
      sourceMenu.appendChild(bibSourceMenuNode(sid, lookupSource));
      wrap.appendChild(sourceMenu);
      toolbar.appendChild(wrap);
    } else if (isBook) toolbar.appendChild(bibAction('查图书信息', 'lookupGoogleBooks', sid, true));
    if (canDetect) {
      var detect = bibAction('自动识别', 'detectBibliographicMetadata', sid);
      detect.dataset.overwrite = 'false';
      toolbar.appendChild(detect);
      if (meta.metadata_source === 'manual') {
        var redetect = bibAction('重新识别', 'detectBibliographicMetadata', sid);
        redetect.dataset.overwrite = 'true';
        toolbar.appendChild(redetect);
      }
    }
    editor.appendChild(toolbar);
    function lookupArea(prefix, config) {
      var status = bibNode('div', 'cnki-citation-result');
      status.id = prefix + '-lookup-status';
      status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
      editor.appendChild(status);
      var list = bibNode('div', 'cnki-candidate-list');
      list.id = prefix + '-candidate-list';
      list.appendChild(candidateListNode(config, sid));
      editor.appendChild(list);
    }
    if (isJournal) {
      lookupArea('cnki', CNKI_CARD_CONFIG);
      lookupArea('crossref', CROSSREF_CARD_CONFIG);
    } else if (isBook) lookupArea('book', BOOK_CARD_CONFIG);
    if (isJournal) {
      var citation = bibNode('div', 'bib-citation-panel');
      citation.id = 'bib-citation-panel'; citation.hidden = true;
      var textarea = document.createElement('textarea');
      textarea.id = 'bib-cnki-citation'; textarea.maxLength = 8000; textarea.rows = 3;
      textarea.placeholder = '粘贴知网 GB/T 7714 引文，如：作者.篇名[J].刊名,2020,49(04):15-27.';
      textarea.dataset.actionPaste = 'parseCnkiCitationTextAfterPaste';
      citation.appendChild(textarea);
      var citationActions = bibNode('div', 'cnki-citation-actions');
      var parse = bibNode('button', 'action-btn', '从引用文字补全');
      parse.type = 'button'; parse.dataset.action = 'parseCnkiCitationText';
      citationActions.appendChild(parse);
      var result = bibNode('span', 'cnki-citation-result');
      result.id = 'bib-cnki-citation-result'; result.setAttribute('role','status'); result.setAttribute('aria-live','polite');
      citationActions.appendChild(result);
      citation.appendChild(citationActions);
      editor.appendChild(citation);
    }
    var footer = bibNode('div', 'bib-footer');
    footer.appendChild(bibNode('span', 'bibliographic-meta', '状态：' + metadataStatusLabel(meta.metadata_status) + ' · 来源：' + metadataSourceLabel(meta.metadata_source)));
    var footerActions = bibNode('span', 'bib-footer-actions');
    footerActions.appendChild(bibAction('取消', 'exitBibEdit', sid));
    footerActions.appendChild(bibAction('保存书目信息', 'saveBibliographicMetadata', sid, true));
    footer.appendChild(footerActions);
    editor.appendChild(footer);
    return editor;
  }

  // 查看态：书目字段渲染成 label:value 只读行，缺失字段显示「—」并标黄。
  // 直接点任意字段即进入编辑态并聚焦该字段（无需额外「编辑」按钮）；头部只留
  // 按类型的主补全动作。与编辑态共用宿主 #bib-host，就地整块替换。
  function bibliographicReadNode(src) {
    var meta = sourceBibliographicMetadata(src);
    var docType = bibEditorTypeOverride[src.source_file_id] || bibliographicDocType(meta);
    var missingMeta = Object.assign({}, meta, {document_type: docType,
      metadata_missing_fields: docType === bibliographicDocType(meta) ? meta.metadata_missing_fields : null});
    var missing = bibliographicMissingFields(missingMeta);
    var sid = src.source_file_id;
    var langLabel = bibLanguageLabel(src.language_code) || '未识别语言';
    var read = bibNode('div', 'bib-read');
    var head = bibNode('div', 'bib-section-head');
    head.appendChild(bibNode('span', 'drawer-section-title', '书目信息'));
    var tools = bibNode('span', 'bib-section-tools');
    var confirmed = isBibliographicTypeConfirmed(meta);
    tools.appendChild(bibReadPrimaryButtonNode(confirmed ? docType : 'thesis', sid));
    head.appendChild(tools);
    read.appendChild(head);
    if (!confirmed) read.appendChild(bibNode('div', 'bib-unconfirmed',
      '尚未识别文献类型，点「自动识别」或任意字段手动选择类型并填写'));
    if (confirmed) { var badge = bibMissingBadge(missingMeta); if (badge) read.appendChild(badge); }
    var grid = bibNode('div', 'bib-read-grid');
    function row(label, fieldKey, value, full) {
      var isMissing = missing.indexOf(fieldKey) >= 0;
      var node = bibNode('div', 'bib-read-row' + (full ? ' full' : '') + (isMissing ? ' is-missing' : ''));
      node.setAttribute('role', 'button'); node.tabIndex = 0; node.title = '点击编辑';
      node.dataset.action = 'enterBibEdit'; node.dataset.sourceId = sid;
      node.dataset.focusField = fieldKey.replace(/_/g, '-');
      node.appendChild(bibNode('span', 'bib-read-label', label));
      var valueNode = bibNode('span', 'bib-read-value', String(value == null ? '' : value).trim() || '—');
      if (isMissing) {
        valueNode.appendChild(document.createTextNode(' '));
        var warning = bibSvg(['M12 3 2.8 20h18.4L12 3Z', 'M12 9v5', 'M12 17.5h.01'], 'bib-read-warn');
        warning.setAttribute('viewBox', '0 0 24 24'); warning.setAttribute('stroke-width', '1.9');
        valueNode.appendChild(warning);
      }
      node.appendChild(valueNode);
      grid.appendChild(node);
    }
    var fields = docType === 'thesis'
      ? [['作者','author',meta.author], ['篇名','title',meta.title,true], ['学校','publisher',meta.publisher],
        ['年份','publish_year',meta.publish_year], ['语言','language',langLabel,true]]
      : docType === 'journal_article'
        ? [['篇名','title',meta.title,true], ['作者','author',meta.author], ['出版刊物','journal_name',meta.journal_name],
          ['卷次','volume',meta.volume], ['期号','issue',meta.issue], ['年份','publish_year',meta.publish_year],
          ['页码','page_range',meta.page_range], ['DOI','doi',meta.doi], ['ISSN','issn',meta.issn], ['语言','language',langLabel,true]]
        : [['作者','author',meta.author], ['国别','country',meta.country], ['书名','title',meta.title,true],
          ['译者','translator',meta.translator], ['出版地','publish_place',meta.publish_place],
          ['出版社','publisher',meta.publisher], ['出版年份','publish_year',meta.publish_year],
          ['ISBN','isbn',meta.isbn], ['语言','language',langLabel]];
    fields.forEach(function(spec) { row(spec[0], spec[1], spec[2], spec[3]); });
    read.appendChild(grid);
    read.appendChild(bibNode('div', 'bibliographic-meta', '状态：' + metadataStatusLabel(meta.metadata_status)
      + ' · 来源：' + metadataSourceLabel(meta.metadata_source)));
    return read;
  }

  // 查看态头部的主补全按钮：期刊→补全期刊信息，图书→查图书信息，学位→自动识别。
  // 点它先进编辑态再执行（补全/识别本就是编辑动作，回填目标是编辑态的输入框）。
  function bibReadPrimaryButtonNode(docType, sourceId) {
    var mode = docType === 'journal_article' ? 'lookup'
      : docType === 'book' || docType === 'translated_book' ? 'books' : 'detect';
    var button = bibAction(mode === 'lookup' ? '补全期刊信息' : mode === 'books' ? '查图书信息' : '自动识别',
      'bibEditAndRun', sourceId, true, true);
    button.dataset.runMode = mode;
    return button;
  }

  function bibSourceMenuNode(sourceId, active) {
    var fragment = document.createDocumentFragment();
    function item(source, label, note) {
      var button = bibNode('button', 'bib-menu-item' + (active === source ? ' active' : ''), label);
      button.type = 'button'; button.setAttribute('role', 'menuitem');
      button.dataset.action = 'bibSetSource';
      button.dataset.sourceId = sourceId;
      button.dataset.source = source;
      if (note) button.appendChild(bibNode('span', 'bib-menu-note', note));
      fragment.appendChild(button);
    }
    item('auto', '智能补全', '推荐');
    item('cnki', '知网补全', '中文');
    item('crossref', 'Crossref 补全', '外文');
    fragment.appendChild(bibNode('div', 'bib-menu-sep'));
    [['paste','粘贴引文'], ['opencnki','打开知网检索']].forEach(function(action) {
      var button = bibNode('button', 'bib-menu-item', action[1]);
      button.type = 'button'; button.setAttribute('role','menuitem');
      button.dataset.action = 'bibMenuAction';
      button.dataset.menuAction = action[0];
      button.dataset.sourceId = sourceId;
      fragment.appendChild(button);
    });
    return fragment;
  }

  // 书目区渲染分发：查看态 / 编辑态，共用稳定宿主 #bib-host。
  function renderBibliographicSectionNode(src) {
    var host = bibNode('div');
    host.id = 'bib-host';
    host.appendChild(bibEditMode[src.source_file_id] ? bibliographicEditorNode(src) : bibliographicReadNode(src));
    return host;
  }

  function enterBibEdit(sourceId, focusFieldId) {
    var src = libraryStore.sources.find(function(item) { return item.source_file_id === sourceId; });
    var host = document.getElementById('bib-host');
    if (!src || !host) return;
    bibEditMode[sourceId] = true;
    host.replaceChildren(bibliographicEditorNode(src));
    // 点某字段进来的聚焦该字段；否则聚焦第一个。
    var target = (focusFieldId && host.querySelector('#bib-' + focusFieldId)) || host.querySelector('.bibliographic-field input');
    if (target) target.focus();
  }

  // 取消编辑：放弃表单里未保存的输入，清脏，回到查看态（显示当前已保存值）。
  function exitBibEdit(sourceId) {
    var src = libraryStore.sources.find(function(item) { return item.source_file_id === sourceId; });
    var host = document.getElementById('bib-host');
    bibEditMode[sourceId] = false;
    bibEditorDirty = false;
    delete bibEditorTypeOverride[sourceId];
    delete bibliographicPendingEvidence[sourceId];
    if (!src || !host) return;
    bibFieldCache[sourceId] = bibFieldCacheFromMeta(sourceBibliographicMetadata(src));
    host.replaceChildren(bibliographicReadNode(src));
  }

  // 查看态点主补全/识别：先进编辑态（渲染出输入框），再运行对应动作。
  function bibEditAndRun(sourceId, action) {
    enterBibEdit(sourceId);
    if (action === 'lookup') bibRunLookup(sourceId);
    else if (action === 'books') lookupGoogleBooks(sourceId);
    else if (action === 'detect') detectBibliographicMetadata(sourceId, false);
  }

  function toggleCitationPanel() {
    var panel = document.getElementById('bib-citation-panel');
    if (!panel) return;
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      var textarea = document.getElementById('bib-cnki-citation');
      if (textarea) textarea.focus();
    }
  }

  // ── 期刊补全 split 按钮：标签、菜单、按语言智能选源（仅期刊，图书不用）──────
  // source 取值：'auto'（按文献语言智能选）、'cnki'、'crossref'。


  // 'auto' 按文献语言落地：中文期刊→知网，外文期刊→Crossref。
  function bibEffectiveSource(sid) {
    var source = bibLookupSource[sid] || 'auto';
    if (source !== 'auto') return source;
    var src = libraryStore.sources.find(function(item) { return item.source_file_id === sid; });
    var meta = src ? sourceBibliographicMetadata(src) : {};
    return isForeignTitle(meta.title) ? 'crossref' : 'cnki';
  }

  function bibDispatchSource(sid, eff) {
    if (eff === 'crossref') return lookupCrossref(sid);
    return lookupCnkiMetadata(sid);
  }

  function bibRunLookup(sid) {
    bibDispatchSource(sid, bibEffectiveSource(sid));
  }

  function bibSetSource(ev, sid, source) {
    if (ev) ev.stopPropagation();
    bibLookupSource[sid] = source;
    bibCloseMenus();
    var btn = document.getElementById('bib-primary-btn');
    if (btn) btn.textContent = bibPrimaryLabel(source);
    var menu = document.getElementById('bib-source-menu');
    if (menu) menu.replaceChildren(...bibSourceMenuNode(sid, source).childNodes);
    bibDispatchSource(sid, bibEffectiveSource(sid));
  }

  function bibMenuAction(ev, action, sid) {
    if (ev) ev.stopPropagation();
    bibCloseMenus();
    if (action === 'paste') return toggleCitationPanel();
    if (action === 'opencnki') return openCnkiSearch(sid);
  }

  function bibToggleMenu(ev, id) {
    if (ev) ev.stopPropagation();
    var menu = document.getElementById(id);
    if (!menu) return;
    var willOpen = !menu.classList.contains('open');
    bibCloseMenus();
    if (willOpen) {
      menu.classList.add('open');
      var trigger = menu.parentElement && menu.parentElement.querySelector('[aria-controls="' + id + '"]');
      if (trigger) trigger.setAttribute('aria-expanded', 'true');
    }
  }

  function bibCloseMenus() {
    var open = document.querySelectorAll('.bib-menu.open');
    for (var i = 0; i < open.length; i++) {
      open[i].classList.remove('open');
      var trigger = open[i].parentElement && open[i].parentElement.querySelector('[aria-controls="' + open[i].id + '"]');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    }
  }

  if (typeof document !== 'undefined' && !window.__bibMenuOutside) {
    window.__bibMenuOutside = true;
    document.addEventListener('click', function(e) {
      if (!e.target || !e.target.closest || !e.target.closest('.bib-menu-wrap')) bibCloseMenus();
    });
  }

  function setBibliographicType(sourceId, docType) {
    var current = collectBibliographicForm();
    bibEditorDirty = true;
    bibEditorTypeOverride[sourceId] = docType;
    var src = libraryStore.sources.find(function(item) { return item.source_file_id === sourceId; });
    var editor = document.getElementById('bibliographic-editor');
    if (!src || !editor) return;
    editor.replaceWith(bibliographicEditorNode(src));
    // 切换字段集时保留已填写的公共字段。
    Object.keys(current).forEach(function(key) {
      if (key === 'document_type' || !current[key]) return;
      var input = document.getElementById('bib-' + key.replace(/_/g, '-'));
      if (input && !input.value) input.value = current[key];
    });
  }





  function collectBibliographicForm() {
    var cache = (libraryStore.selectedId && bibFieldCache[libraryStore.selectedId]) || {};
    // 可见字段以实时 DOM 值为准（尊重清空）；当前类型不含的字段回退到缓存里上次
    // 已知的值，保存时随类型一并提交，杜绝切类型后另一类型字段被静默写空。
    function value(id, field) {
      var el = document.getElementById('bib-' + id);
      return el ? el.value.trim() : String(cache[field] || '').trim();
    }
    var typeButton = document.querySelector('#bib-doctype-control .seg-btn.active');
    var editorDocType = typeButton ? typeButton.dataset.doctype : 'book';
    var translator = value('translator', 'translator');
    var result = {
      document_type: bibliographicFormDocType(editorDocType, translator),
      author: value('author', 'author'), country: value('country', 'country'), title: value('title', 'title'),
      translator: translator, publish_place: value('publish-place', 'publish_place'),
      publisher: value('publisher', 'publisher'), publish_year: value('publish-year', 'publish_year'), isbn: value('isbn', 'isbn'),
      journal_name: value('journal-name', 'journal_name'), volume: value('volume', 'volume'),
      issue: value('issue', 'issue'), page_range: value('page-range', 'page_range'), doi: value('doi', 'doi'), issn: value('issn', 'issn'),
      metadata_evidence: bibliographicPendingEvidence[libraryStore.selectedId] || {}
    };
    // 仅当语言下拉在场（编辑态）才提交 language，避免其它保存路径误清人工语言。
    var languageEl = document.getElementById('bib-language');
    if (languageEl) result.language = languageEl.value;
    if (libraryStore.selectedId) {
      var store = bibFieldCache[libraryStore.selectedId] || (bibFieldCache[libraryStore.selectedId] = {});
      BIBLIOGRAPHIC_CACHE_FIELDS.forEach(function(field) { store[field] = result[field]; });
    }
    return result;
  }

  function refreshBibliographicMissingDisplay() {
    var editor = document.getElementById('bibliographic-editor');
    if (!editor) return;
    var current = collectBibliographicForm();
    var missing = bibliographicMissingFields(current);
    editor.querySelectorAll('.bibliographic-field[data-metadata-field]').forEach(function(field) {
      var metadataField = field.dataset.metadataField;
      var isMissing = missing.indexOf(metadataField) >= 0;
      field.classList.toggle('is-missing', isMissing);
      var label = field.querySelector('label');
      if (!label) return;
      var baseLabel = label.textContent.replace(/\s*·\s*缺少$/, '');
      label.textContent = baseLabel + (isMissing ? ' · 缺少' : '');
    });
    var badge = editor.querySelector('.bibliographic-missing');
    if (!badge) return;
    if (!missing.length) {
      badge.remove();
      return;
    }
    var badgeText = badge.querySelector('span');
    if (badgeText) badgeText.textContent = '缺少：' + missing.map(function(field) {
      return bibliographicFieldLabels[field] || field;
    }).join('、');
  }

  const bibliographicLookupFields = {
    author:{id:'author',label:'作者'},
    title:{id:'title',label:'篇名'},
    journal_name:{id:'journal-name',label:'出版刊物'},
    publish_year:{id:'publish-year',label:'年份'},
    volume:{id:'volume',label:'卷次'},
    issue:{id:'issue',label:'期号'},
    page_range:{id:'page-range',label:'页码'},
    doi:{id:'doi',label:'DOI'},
    issn:{id:'issn',label:'ISSN'}
  };

  function applyBibliographicLookupMetadata(sourceId, metadata, evidence, fields) {
    var filled = [];
    var preserved = [];
    metadata = metadata || {};
    evidence = evidence || {};
    fields = fields || bibliographicLookupFields;
    Object.keys(fields).forEach(function(key) {
      var incoming = String(metadata[key] || '').trim();
      var field = fields[key];
      var input = document.getElementById('bib-' + field.id);
      if (!incoming || !input) return;
      var existing = input.value.trim();
      if (!existing) {
        input.value = incoming;
        filled.push(field.label);
        var evidenceItem = evidence[key];
        if (evidenceItem) {
          if (!bibliographicPendingEvidence[sourceId]) bibliographicPendingEvidence[sourceId] = {};
          bibliographicPendingEvidence[sourceId][key] = Object.assign({}, evidenceItem, {value:incoming});
        }
      } else if (!bibliographicValuesEquivalent(key, existing, incoming)) {
        preserved.push(field.label);
      }
    });
    refreshBibliographicMissingDisplay();
    if (filled.length) bibEditorDirty = true;  // 联网回填了空字段 = 有未保存修改
    return {filled:filled, preserved:preserved};
  }

  // bibliographicValuesEquivalent 已抽到 06-pure.js（纯逻辑，可单测）。

  function candidateListNode(config, sourceId) {
    var state = config === CNKI_CARD_CONFIG ? cnkiLookupState
      : config === BOOK_CARD_CONFIG ? bookLookupState : crossrefLookupState;
    var candidates = ((state[sourceId] || {}).candidates) || [];
    var fragment = document.createDocumentFragment();
    candidates.forEach(function(candidate, index) {
      var meta = candidate.metadata || {};
      var match = candidate.match || {};
      var levelLabel = match.level === 'high' ? '高匹配' : match.level === 'medium' ? '需核对' : '低匹配';
      var detail = [meta.author, meta[config.detailMidField], candidate.publish_date || meta.publish_year].filter(Boolean).join(' · ');
      var card = bibNode('div', 'cnki-candidate ' + (match.level || 'low'));
      var main = bibNode('div', 'cnki-candidate-main');
      main.appendChild(bibNode('div', 'cnki-candidate-title', meta.title || config.titleFallback));
      var detailNode = bibNode('div', 'cnki-candidate-detail', detail || config.detailFallback);
      if (config.detailExtra && meta[config.detailExtra.field]) {
        detailNode.appendChild(document.createTextNode(' · ' + config.detailExtra.label + ' ' + meta[config.detailExtra.field]));
      }
      main.appendChild(detailNode);
      var matchNode = bibNode('div', 'cnki-candidate-match');
      matchNode.appendChild(bibNode('span', null, levelLabel + (match.score != null ? ' · ' + Math.round(Number(match.score) * 100) + '%' : '')));
      if ((match.reasons || []).length) matchNode.appendChild(bibNode('span', null, match.reasons.join('、')));
      if ((match.conflicts || []).length) matchNode.appendChild(bibNode('span', 'has-warning', '冲突：' + match.conflicts.join('、')));
      main.appendChild(matchNode);
      card.appendChild(main);
      var actions = bibNode('div', 'cnki-candidate-actions');
      config.actions.forEach(function(action) {
        var button = bibNode('button', 'action-btn' + (action.primary ? ' primary' : ''), action.label);
        button.type = 'button'; button.dataset.action = action.handler;
        button.dataset.sourceId = sourceId; button.dataset.index = String(index);
        actions.appendChild(button);
      });
      card.appendChild(actions);
      fragment.appendChild(card);
    });
    return fragment;
  }

  function cnkiCandidateListNode(sourceId) { return candidateListNode(CNKI_CARD_CONFIG, sourceId); }

  // 三套联网补全共用的通用渲染 / 状态函数（原 render*/set*LookupStatus 六个函数已合并）。
  // 差异只有宿主元素 id 与对应的候选列表函数，全部收进下面三个 *_LOOKUP 配置对象；
  // 每个函数只操作 config 指定的宿主，期刊场景下 CNKI 与 Crossref 两组宿主互不影响。
  function renderCandidates(config, sourceId) {
    var host = document.getElementById(config.listElId);
    if (host) host.replaceChildren(config.listNode(sourceId));
  }

  function setLookupStatus(config, message, warning) {
    var status = document.getElementById(config.statusElId);
    if (!status) return;
    status.textContent = message || '';
    status.classList.toggle('has-warning', !!warning);
  }

  // 三个候选列表构造函数由配置引用，函数声明已提升。
  const CNKI_LOOKUP = {
    stateMap: cnkiLookupState, endpoint: '/api/bibliographic-metadata/lookup-cnki',
    listElId: 'cnki-candidate-list', statusElId: 'cnki-lookup-status', listNode: cnkiCandidateListNode,
    loadingMessage: '正在查询知网…', defaultError: '知网查询失败',
    buildRequest: function (form) { return {title: form.title, author: form.author, publish_year: form.publish_year, journal_name: form.journal_name, doi: form.doi, issn: form.issn}; },
    validate: function (metadata) { return (!metadata.title && !metadata.doi) ? '请先填写篇名或 DOI' : null; },
    resetState: function (_form, dependencies) {
      return {candidates: [], open_url: cnkiSearchUrlFromForm(dependencies)};
    },
    saveErrorState: function (data, sourceId) { if (data.open_url) cnkiLookupState[sourceId] = {candidates: [], open_url: data.open_url}; },
    describe: function (data) {
      var notice = data.query_notice ? data.query_notice + '；' : '';
      var c = data.candidates;
      if (!c || !c.length) return {message: notice + '知网未返回候选，可打开知网检索或粘贴引用文字', warning: true};
      if (c.length === 1 && c[0].match && c[0].match.level === 'high') return {message: notice + '找到 1 条高匹配候选，请核对后获取完整题录', warning: false};
      return {message: notice + '找到 ' + c.length + ' 条候选，请选择正确记录', warning: true};
    },
    onError: function (e) { return {message: e.message + '；可打开知网检索或粘贴引用文字', warning: true}; }
  };
  const BOOK_LOOKUP = {
    stateMap: bookLookupState, endpoint: '/api/bibliographic-metadata/lookup-google-books',
    listElId: 'book-candidate-list', statusElId: 'book-lookup-status', listNode: bookCandidateListNode,
    loadingMessage: '正在查询图书目录…', defaultError: '图书查询失败',
    buildRequest: function (form) { return {title: form.title, author: form.author, publish_year: form.publish_year, isbn: form.isbn}; },
    validate: function (metadata) { return (!metadata.isbn && !metadata.title) ? '请先填写 ISBN 或书名' : null; },
    resetState: function () { return {candidates: []}; },
    saveErrorState: function () {},
    describe: function (data) {
      var c = data.candidates;
      if (!c || !c.length) return {message: '未找到匹配图书，可核对 ISBN/书名或手动填写', warning: true};
      if (c.length === 1 && c[0].match && c[0].match.level === 'high') return {message: '找到 1 条高匹配图书，请核对后补全', warning: false};
      return {message: '找到 ' + c.length + ' 条候选，请选择正确的图书', warning: true};
    },
    onError: function (e) { return {message: e.message, warning: true}; }
  };
  const CROSSREF_LOOKUP = {
    stateMap: crossrefLookupState, endpoint: '/api/bibliographic-metadata/lookup-crossref',
    listElId: 'crossref-candidate-list', statusElId: 'crossref-lookup-status', listNode: crossrefCandidateListNode,
    loadingMessage: '正在查询 Crossref…', defaultError: 'Crossref 查询失败',
    buildRequest: function (form) { return {title: form.title, author: form.author, publish_year: form.publish_year, doi: form.doi}; },
    validate: function (metadata) { return (!metadata.doi && !metadata.title) ? '请先填写 DOI 或篇名' : null; },
    resetState: function () { return {candidates: []}; },
    saveErrorState: function () {},
    describe: function (data) {
      var c = data.candidates;
      if (!c || !c.length) return {message: 'Crossref 未找到匹配文献，可核对 DOI/篇名或手动填写', warning: true};
      if (c.length === 1 && c[0].match && c[0].match.level === 'high') return {message: '找到 1 条高匹配文献，请核对后补全', warning: false};
      return {message: '找到 ' + c.length + ' 条候选，请选择正确的文献', warning: true};
    },
    onError: function (e) { return {message: e.message, warning: true}; }
  };

  // 三套联网补全的通用骨架：校验 → 清态 → 渲染 → 置“查询中” → fetch → 判 ok →
  // 存态 → 渲染 → 成功文案；失败先 saveErrorState 再 onError。差异全部由 config 的
  // 回调承接，工厂内没有一个 if (config.*)，也不加统一 toast（三套只写 status）。
  async function runLookup(config, sourceId) {
    var form = collectBibliographicForm();
    var metadata = config.buildRequest(form);
    var invalid = config.validate(metadata);
    if (invalid) {
      setLookupStatus(config, invalid, true);
      return;
    }
    config.stateMap[sourceId] = config.resetState(form);
    renderCandidates(config, sourceId);
    setLookupStatus(config, config.loadingMessage, false);
    try {
      var resp = await MEFinderApi.fetch(config.endpoint, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({metadata: metadata})
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) {
        config.saveErrorState(data, sourceId);
        throw new Error(data.error || config.defaultError);
      }
      config.stateMap[sourceId] = {candidates: data.candidates || [], open_url: data.open_url || ''};
      renderCandidates(config, sourceId);
      var described = config.describe(data);
      setLookupStatus(config, described.message, described.warning);
    } catch (e) {
      renderCandidates(config, sourceId);
      var errd = config.onError(e);
      setLookupStatus(config, errd.message, errd.warning);
    }
  }

  async function lookupCnkiMetadata(sourceId) {
    return runLookup(CNKI_LOOKUP, sourceId);
  }

  function applyCnkiSearchCandidate(sourceId, index) {
    var candidate = ((cnkiLookupState[sourceId] || {}).candidates || [])[index];
    if (!candidate) return;
    var applied = applyBibliographicLookupMetadata(sourceId, candidate.metadata, candidate.evidence);
    var message = applied.filled.length ? '已补全：' + applied.filled.join('、') : '表单已有对应内容，未作覆盖';
    if (applied.preserved.length) message += '；已有值未覆盖：' + applied.preserved.join('、');
    setLookupStatus(CNKI_LOOKUP,message + '。请检查后保存', applied.preserved.length > 0);
  }

  /* ═══ 外文图书联网补全（Open Library / K10plus / LoC，Google 兜底）═══
   * 干净 JSON API，一次返回完整题录；连不上时安全降级，只提示不阻塞。
   * 图书字段与知网期刊字段不同，单独走 applyBookLookupMetadata 只补空字段。 */
  const bookLookupFields = {
    author:{id:'author',label:'作者'},
    title:{id:'title',label:'书名'},
    publisher:{id:'publisher',label:'出版社'},
    publish_place:{id:'publish-place',label:'出版地'},
    publish_year:{id:'publish-year',label:'出版年份'},
    isbn:{id:'isbn',label:'ISBN'}
  };

  function bookCandidateListNode(sourceId) { return candidateListNode(BOOK_CARD_CONFIG, sourceId); }

  async function lookupGoogleBooks(sourceId) {
    return runLookup(BOOK_LOOKUP, sourceId);
  }

  // 图书候选：只把当前为空的图书字段补进表单，绝不覆盖已有值。
  function applyBookCandidate(sourceId, index) {
    var candidate = ((bookLookupState[sourceId] || {}).candidates || [])[index];
    if (!candidate) return;
    // 回填逻辑与期刊完全同构，复用 applyBibliographicLookupMetadata，仅换图书字段集。
    var applied = applyBibliographicLookupMetadata(sourceId, candidate.metadata, candidate.evidence, bookLookupFields);
    var message = applied.filled.length ? '已补全：' + applied.filled.join('、') : '表单已有对应内容，未作覆盖';
    if (applied.preserved.length) message += '；已有值未覆盖：' + applied.preserved.join('、');
    setLookupStatus(BOOK_LOOKUP,message + '。请检查后保存', applied.preserved.length > 0);
  }

  /* ═══ Crossref 外文期刊论文补全 ═══
   * DOI 直连最准，无 DOI 用篇名+作者搜；干净 JSON，一次返回完整题录。
   * 期刊字段与知网一致，复用 applyBibliographicLookupMetadata 只补空字段。 */
  function crossrefCandidateListNode(sourceId) { return candidateListNode(CROSSREF_CARD_CONFIG, sourceId); }

  async function lookupCrossref(sourceId) {
    return runLookup(CROSSREF_LOOKUP, sourceId);
  }

  function applyCrossrefCandidate(sourceId, index) {
    var candidate = ((crossrefLookupState[sourceId] || {}).candidates || [])[index];
    if (!candidate) return;
    var applied = applyBibliographicLookupMetadata(sourceId, candidate.metadata, candidate.evidence);
    var message = applied.filled.length ? '已补全：' + applied.filled.join('、') : '表单已有对应内容，未作覆盖';
    if (applied.preserved.length) message += '；已有值未覆盖：' + applied.preserved.join('、');
    setLookupStatus(CROSSREF_LOOKUP,message + '。请检查后保存', applied.preserved.length > 0);
  }

  async function fetchCnkiCandidate(sourceId, index) {
    var candidate = ((cnkiLookupState[sourceId] || {}).candidates || [])[index];
    if (!candidate || !candidate.record_url) return;
    setLookupStatus(CNKI_LOOKUP,'正在读取知网完整题录…', false);
    try {
      var resp = await MEFinderApi.fetch('/api/bibliographic-metadata/cnki-candidate', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({candidate:{record_url:candidate.record_url}})
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '完整题录获取失败');
      var applied = applyBibliographicLookupMetadata(sourceId, data.metadata, data.evidence);
      var message = applied.filled.length ? '已补全：' + applied.filled.join('、') : '表单已有对应内容，未作覆盖';
      if (applied.preserved.length) message += '；已有值未覆盖：' + applied.preserved.join('、');
      setLookupStatus(CNKI_LOOKUP,message + '。请检查后保存', applied.preserved.length > 0);
      showToast('知网题录已载入，请检查后保存', 'success');
    } catch(e) {
      setLookupStatus(CNKI_LOOKUP,e.message + '；可打开记录后粘贴引用文字', true);
    }
  }

  function cnkiSearchUrlFromForm(dependencies) {
    var deps = dependencies || {collectBibliographicForm: collectBibliographicForm};
    var form = deps.collectBibliographicForm();
    var keyword = form.doi || form.title;
    if (!keyword) return '';
    return 'https://oversea.cnki.net/kns8s/search?classid=R0DPFOXP&kw=' + encodeURIComponent(keyword)
      + '&korder=' + (form.doi ? 'DOI' : 'TI') + '&language=CHS';
  }

  async function openCnkiExternal(url) {
    if (!url) {
      showToast('请先填写篇名或 DOI', 'warning');
      return;
    }
    try {
      var resp = await MEFinderApi.fetch('/api/bibliographic-metadata/open-cnki', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:url})
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '打开失败');
    } catch(e) {
      showToast('打开知网失败：' + e.message, 'danger');
    }
  }

  function openCnkiSearch(sourceId) {
    var state = cnkiLookupState[sourceId] || {};
    openCnkiExternal(state.open_url || cnkiSearchUrlFromForm());
  }

  function openCnkiCandidate(sourceId, index) {
    var candidate = ((cnkiLookupState[sourceId] || {}).candidates || [])[index];
    openCnkiExternal(candidate && candidate.record_url);
  }

  async function parseCnkiCitationText() {
    var textarea = document.getElementById('bib-cnki-citation');
    var result = document.getElementById('bib-cnki-citation-result');
    var citationText = textarea ? textarea.value.trim() : '';
    if (!citationText) {
      if (result) result.textContent = '请先粘贴一条知网期刊引文';
      return;
    }
    if (result) {
      result.classList.remove('has-warning');
      result.textContent = '正在识别…';
    }
    try {
      var resp = await MEFinderApi.fetch('/api/bibliographic-metadata/parse-cnki-citation', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({citation_text:citationText})
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '识别失败');
      var citationEvidence = {};
      Object.keys(data.metadata || {}).forEach(function(key) {
        if (!bibliographicLookupFields[key]) return;
        var value = String(data.metadata[key] || '').trim();
        if (value) citationEvidence[key] = {source:'cnki_citation', evidence_text:citationText.slice(0,500), value:value};
      });
      var applied = applyBibliographicLookupMetadata(libraryStore.selectedId, data.metadata, citationEvidence);
      var filled = applied.filled;
      var preserved = applied.preserved;
      var messages = [];
      if (filled.length) messages.push('已补全：' + filled.join('、'));
      else messages.push('表单已有对应内容，未作覆盖');
      if (preserved.length) messages.push('已有值未覆盖：' + preserved.join('、'));
      if (result) {
        result.textContent = messages.join('；');
        result.classList.toggle('has-warning', preserved.length > 0);
      }
      showToast('已识别知网引用，请检查后保存', 'success');
    } catch(e) {
      if (result) {
        result.classList.add('has-warning');
        result.textContent = e.message;
      }
      showToast('知网引用识别失败：' + e.message, 'danger');
    }
  }

  async function detectBibliographicMetadata(sourceId, force) {
    if (force && !await showAppConfirm(
      '自动识别结果将覆盖当前表单中的人工书目信息',
      {title:'覆盖人工书目信息？', confirmText:'确认覆盖', tone:'warning'}
    )) return;
    try {
      showToast('正在识别封面、书名页、CIP 与版权页…');
      var resp = await MEFinderApi.fetch('/api/bibliographic-metadata/detect', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,force:!!force})});
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '识别失败');
      var src = libraryStore.sources.find(function(item){return item.source_file_id === sourceId;});
      if (src) {
        src.bibliographic_metadata = data.metadata;
        Object.keys(data.metadata).forEach(function(key){src[key]=data.metadata[key];});
        selectLibDoc(sourceId);
        // 识别结果只载入未保存，标脏以便离开时提醒。
        if (data.metadata.metadata_source !== 'manual' || force) bibEditorDirty = true;
      }
      showToast(data.metadata.metadata_source === 'manual' && !force ? '人工元数据已保护，未覆盖' : '识别结果已载入，请检查后保存');
    } catch(e) { showToast('识别失败：' + e.message, 'danger'); }
  }

  async function saveBibliographicMetadata(sourceId) {
    try {
      var resp = await MEFinderApi.fetch('/api/bibliographic-metadata/save', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,metadata:collectBibliographicForm()})});
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '保存失败');
      showToast('书目信息已保存并立即生效', 'success');
      bibEditorDirty = false;
      bibEditMode[sourceId] = false;  // 保存后回到查看态
      delete bibEditorTypeOverride[sourceId];
      delete bibliographicPendingEvidence[sourceId];
      delete bibFieldCache[sourceId];
      await global.MEFinder.library.load(true);
      await selectLibDoc(sourceId);
    } catch(e) { showToast('保存失败：' + e.message, 'danger'); }
  }

  async function openMetadataForSource(sourceId) {
    navigateTo('library');
    // 从检索结果跳来的：给一条返回搜索的路（S-03）。navigateTo 会先清掉横幅，这里再点亮。
    var banner = document.getElementById('library-return-banner');
    if (banner) banner.hidden = false;
    if (!libraryStore.loaded) await global.MEFinder.library.load();
    await selectLibDoc(sourceId);
  }

  function returnToSearch() {
    var banner = document.getElementById('library-return-banner');
    if (banner) banner.hidden = true;
    navigateTo('search');
  }

  // 有未保存修改时先确认；用户放弃才返回 true。仅拦用户主动离开详情的路径，
  // 程序内部的关闭（删除后、保存后重载）不经过这里。
  function guardDirty() {
    if (!bibEditorDirty) return Promise.resolve(true);
    return showAppConfirm(
      '当前文献的书目信息有未保存的修改，离开将丢弃这些修改',
      {title:'放弃未保存的书目修改？', confirmText:'放弃修改', tone:'warning'}
    );
  }

  // 离开详情的统一闸门：确认放弃后清脏并放行。
  async function guardLeaveDetail() {
    if (bibEditorDirty && !await guardDirty()) return false;
    bibEditorDirty = false;
    return true;
  }

  async function requestCloseLibDrawer() {
    if (!await guardLeaveDetail()) return;
    closeLibDrawer();
  }

  function closeLibDrawer() {
    libraryStore.selectedId = null;
    calSelectedSourceId = null;
    bibEditorDirty = false;
    document.getElementById('library-drawer').classList.remove('open');
    var body = document.querySelector('#page-library .library-body');
    if (body) body.classList.remove('detail-open');
    document.querySelectorAll('#library-list .library-entry').forEach(function(row) { row.classList.remove('selected'); });
  }

  function toggleDrawerSection(event, sectionId) {
    var section = document.getElementById(sectionId);
    if (!section) return;
    var body = section.querySelector('.drawer-collapse-body');
    var head = section.querySelector('.cal-collapse-head');
    if (!body) return;
    var open = body.style.display === 'none';
    body.style.display = open ? 'block' : 'none';
    section.classList.toggle('expanded', open);
    if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  async function submitMineruReparse(sourceId) {
    if (!await showAppConfirm(
      '将把这份 PDF 上传到 MinerU 在线服务重新解析。现有结果会保留到新结果成功写入',
      {title:'重新解析 PDF？', confirmText:'上传并重新解析', tone:'warning'}
    )) return;
    var source = (libraryStore.sources || []).find(function(item) { return item.source_file_id === sourceId; }) || {};
    var queueItem = {
      id: 'mineru-reparse-' + Date.now(),
      sourceFileId: sourceId,
      name: source.file_name || source.title || '未命名 PDF',
      size: Number(source.size_bytes || 0),
      type: 'pdf',
      parseMode: 'mineru',
      status: 'processing',
      step: 2,
      route: 'mineru',
      detectedType: source.pdf_profile && source.pdf_profile.detected_pdf_type,
      message: '正在提交 MinerU 在线解析…'
    };
    importStore.queue.push(queueItem);
    navigateTo('import');
    global.MEFinder.imports.renderQueue();
    requestAnimationFrame(function() {
      var queue = document.getElementById('import-queue');
      if (queue) queue.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    try {
      var resp = await MEFinderApi.fetch('/api/mineru-reparse', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({source_id: sourceId})
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '提交失败');
      var tracked = importStore.queue.find(function(item) {
        return item !== queueItem && item.jobId === data.job_id;
      });
      if (tracked) {
        importStore.queue = importStore.queue.filter(function(item) { return item !== queueItem; });
        tracked.status = 'processing';
        tracked.route = 'mineru';
        tracked.message = 'MinerU 解析已在进行中…';
        global.MEFinder.imports.renderQueue();
        global.MEFinder.imports.pollJob(tracked.id);
        return;
      }
      queueItem.jobId = data.job_id;
      queueItem.detectedType = data.detected_pdf_type || queueItem.detectedType;
      queueItem.message = data.already_running
        ? 'MinerU 解析已在进行中，正在读取进度…'
        : '已提交 MinerU，正在等待解析进度…';
      calTransientStatus[sourceId] = 'mapping';
      global.MEFinder.imports.renderQueue();
      global.MEFinder.imports.pollJob(queueItem.id);
    } catch(e) {
      queueItem.status = 'error';
      queueItem.message = '提交 MinerU 解析失败：' + e.message;
      global.MEFinder.imports.renderQueue();
      showToast('提交 MinerU 解析失败：' + e.message, 'danger');
    }
  }

  async function acceptAutoMapping(sourceId) {
    if (!sourceId) return;
    try {
      showToast('正在接受自动映射…');
      var resp = await MEFinderApi.fetch('/api/auto-page-mapping/accept', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({source_id: sourceId})
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '接受失败');
      showToast('自动映射已接受为人工映射', 'success');
      await loadMeta();
      await global.MEFinder.library.load(true);
      await selectLibDoc(sourceId);
    } catch(e) {
      showToast('接受失败：' + e.message, 'danger');
    }
  }

  function showAutoMappingExceptions(sourceId) {
    var src = libraryStore.sources.find(function(s) { return s.source_file_id === sourceId; });
    var autoMap = src && src.pdf_profile ? src.pdf_profile.auto_page_mapping : null;
    var pages = autoMap && autoMap.exception_pages ? autoMap.exception_pages : [];
    if (!pages.length) {
      showToast('没有异常页面');
      return;
    }
    showAppAlert(
      '异常页面（PDF 物理页）：\\n' + pages.slice(0, 80).map(function(p) { return Number(p) + 1; }).join(', ') + (pages.length > 80 ? '\\n…' : ''),
      {title:'页码检测异常'}
    );
  }

  async function openCalibrationForSource(sourceId) {
    navigateTo('library');
    if (!libraryStore.loaded) await global.MEFinder.library.load();
    await selectLibDoc(sourceId);
    await toggleDrawerCalibration(true);
    var host = document.getElementById('library-drawer-calibration');
    if (host) host.scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  async function openCalibrationAndDetect(sourceId) {
    await openCalibrationForSource(sourceId);
    await runAutoDetection(sourceId);
  }

  // node 白盒测试通过显式接口验证配置对象和可注入依赖。
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      lookupConfigs: {
        CNKI_LOOKUP: CNKI_LOOKUP,
        BOOK_LOOKUP: BOOK_LOOKUP,
        CROSSREF_LOOKUP: CROSSREF_LOOKUP
      },
      cnkiSearchUrlFromForm: cnkiSearchUrlFromForm,
      cnkiLookupState: cnkiLookupState
    };
  }

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.bibliography = {
    setEditMode: function (sourceId, enabled) { bibEditMode[sourceId] = enabled; },
    cacheFields: function (sourceId, metadata) {
      bibFieldCache[sourceId] = bibFieldCacheFromMeta(metadata);
    },
    renderSectionNode: renderBibliographicSectionNode,
    markDirty: function () { bibEditorDirty = true; },
    guardLeaveDetail: guardLeaveDetail,
    closeDrawer: closeLibDrawer,
    lookupFields: bibliographicLookupFields
  };

  // 书目详情通过 data-action 委托，不把文献 ID 拼进内联事件代码。
  MEFinderActions.register('enterBibEdit', function(event, target) {
    enterBibEdit(target.dataset.sourceId, target.dataset.focusField);
  });
  MEFinderActions.register('exitBibEdit', function(event, button) {
    exitBibEdit(button.dataset.sourceId);
  });
  MEFinderActions.register('bibEditAndRun', function(event, button) {
    bibEditAndRun(button.dataset.sourceId, button.dataset.runMode);
  });
  MEFinderActions.register('bibRunLookup', function(event, button) {
    bibRunLookup(button.dataset.sourceId);
  });
  MEFinderActions.register('bibSetSource', function(event, button) {
    bibSetSource(event, button.dataset.sourceId, button.dataset.source);
  });
  MEFinderActions.register('bibMenuAction', function(event, button) {
    bibMenuAction(event, button.dataset.menuAction, button.dataset.sourceId);
  });
  MEFinderActions.register('bibToggleMenu', function(event, button) {
    bibToggleMenu(event, button.dataset.menuId);
  });
  MEFinderActions.register('openBibLanguageSelect', function(event, button) {
    openVersionSelect(event, button.dataset.selectId);
  });
  global.bibToggleMenu = bibToggleMenu;
  global.bibCloseMenus = bibCloseMenus;
  MEFinderActions.register('setBibliographicType', function(event, button) {
    setBibliographicType(button.dataset.sourceId, button.dataset.doctype);
  });
  MEFinderActions.register('applyCnkiSearchCandidate', function(event, button) {
    applyCnkiSearchCandidate(button.dataset.sourceId, Number(button.dataset.index));
  });
  MEFinderActions.register('lookupGoogleBooks', function(event, button) {
    lookupGoogleBooks(button.dataset.sourceId);
  });
  MEFinderActions.register('applyBookCandidate', function(event, button) {
    applyBookCandidate(button.dataset.sourceId, Number(button.dataset.index));
  });
  MEFinderActions.register('applyCrossrefCandidate', function(event, button) {
    applyCrossrefCandidate(button.dataset.sourceId, Number(button.dataset.index));
  });
  MEFinderActions.register('fetchCnkiCandidate', function(event, button) {
    fetchCnkiCandidate(button.dataset.sourceId, Number(button.dataset.index));
  });
  global.openCnkiExternal = openCnkiExternal;
  MEFinderActions.register('openCnkiCandidate', function(event, button) {
    openCnkiCandidate(button.dataset.sourceId, Number(button.dataset.index));
  });
  MEFinderActions.register('parseCnkiCitationText', function() {
    parseCnkiCitationText();
  });
  MEFinderActions.register('parseCnkiCitationTextAfterPaste', function() {
    window.setTimeout(parseCnkiCitationText, 0);
  });
  MEFinderActions.register('pickBibLanguage', function(event, button) {
    pickBibLanguage(event, button.dataset.code, button.dataset.label, button);
  });
  MEFinderActions.register('detectBibliographicMetadata', function(event, button) {
    detectBibliographicMetadata(button.dataset.sourceId, button.dataset.overwrite === 'true');
  });
  MEFinderActions.register('saveBibliographicMetadata', function(event, button) {
    saveBibliographicMetadata(button.dataset.sourceId);
  });
  global.returnToSearch = returnToSearch;
  global.requestCloseLibDrawer = requestCloseLibDrawer;
  global.toggleDrawerSection = toggleDrawerSection;
  global.submitMineruReparse = submitMineruReparse;
  global.acceptAutoMapping = acceptAutoMapping;
  global.showAutoMappingExceptions = showAutoMappingExceptions;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
