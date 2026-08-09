

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
function bibFieldCacheFromMeta(meta) {
  var out = {};
  BIBLIOGRAPHIC_CACHE_FIELDS.forEach(function(field) { out[field] = String((meta && meta[field]) || '').trim(); });
  return out;
}

function bibliographicEditorHTML(src) {
  var meta = sourceBibliographicMetadata(src);
  var docType = bibEditorTypeOverride[src.source_file_id] || bibliographicDocType(meta);
  var editorDocType = bibliographicEditorDocType(docType);
  var missing = bibliographicMissingFields(Object.assign({}, meta, {document_type: docType, metadata_missing_fields: docType === bibliographicDocType(meta) ? meta.metadata_missing_fields : null}));
  function field(id, metadataField, label, value, full) {
    var isMissing = missing.indexOf(metadataField) >= 0;
    return '<div class="bibliographic-field' + (full ? ' full' : '') + (isMissing ? ' is-missing' : '') + '" data-metadata-field="' + esc(metadataField) + '"><label for="bib-' + id + '">' + label + (isMissing ? ' · 缺少' : '') + '</label><input id="bib-' + id + '" value="' + esc(value || '') + '"></div>';
  }
  function typeButton(value, label) {
    return '<button class="seg-btn' + (editorDocType === value ? ' active' : '') + '" type="button" data-doctype="' + value + '" onclick="setBibliographicType(\'' + esc(src.source_file_id) + '\',\'' + value + '\')">' + label + '</button>';
  }
  var fieldsHTML;
  if (docType === 'thesis') {
    fieldsHTML = field('author','author','作者',meta.author,false)
      + field('title','title','篇名',meta.title,true)
      + field('publisher','publisher','学校',meta.publisher,false)
      + field('publish-year','publish_year','年份',meta.publish_year,false);
  } else if (docType === 'journal_article') {
    fieldsHTML = field('title','title','标题（篇名）',meta.title,true)
      + field('author','author','作者',meta.author,false)
      + field('journal-name','journal_name','出版刊物',meta.journal_name,false)
      + field('volume','volume','卷次',meta.volume,false)
      + field('issue','issue','期号',meta.issue,false)
      + field('publish-year','publish_year','时间（年份）',meta.publish_year,false)
      + field('page-range','page_range','页码（起止页）',meta.page_range,false)
      + field('doi','doi','DOI',meta.doi,false)
      + field('issn','issn','ISSN',meta.issn,false);
  } else {
    fieldsHTML = field('author','author','作者',meta.author,false) + field('country','country','国别',meta.country,false)
      + field('title','title','书名',meta.title,false) + field('translator','translator','译者',meta.translator,false)
      + field('publish-place','publish_place','出版地',meta.publish_place,false)
      + field('publisher','publisher','出版社',meta.publisher,false) + field('publish-year','publish_year','出版年份',meta.publish_year,false)
      + field('isbn','isbn','ISBN',meta.isbn,true);
  }
  var sid = esc(src.source_file_id);
  var isJournal = docType === 'journal_article';
  var isBook = docType === 'book' || docType === 'translated_book';
  // 一条紧凑工具条：主操作收敛成一个 split 按钮（点主体走当前生效源，▼ 换源），
  // 「自动识别」独立次按钮，识别依据/重新识别等低频动作收进 ⋯ 菜单。
  // 主按钮默认按文献语言智能选源（中文→知网、外文→Crossref/图书目录）；
  // 手动选过某个源后主按钮临时改写成该源名，任务导向且路径透明。
  var toolbarHTML;
  if (isJournal) {
    var lookupSource = bibLookupSource[src.source_file_id] || 'auto';
    var chevronSvg = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
    var moreSvg = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>';
    toolbarHTML = '<div class="bib-toolbar">'
      + '<span class="bib-menu-wrap">'
      + '<span class="bib-split">'
      + '<button class="action-btn primary bib-main" id="bib-primary-btn" type="button" onclick="bibRunLookup(\'' + sid + '\')">' + esc(bibPrimaryLabel(lookupSource)) + '</button>'
      + '<button class="action-btn primary bib-caret" type="button" aria-label="选择补全方式" aria-haspopup="true" onclick="bibToggleMenu(event,\'bib-source-menu\')">' + chevronSvg + '</button>'
      + '</span>'
      + '<span class="bib-menu" id="bib-source-menu" role="menu">' + bibSourceMenuHTML(sid, lookupSource) + '</span>'
      + '</span>'
      + '<button class="action-btn" type="button" onclick="detectBibliographicMetadata(\'' + sid + '\',false)">自动识别</button>'
      + '<span class="bib-menu-wrap">'
      + '<button class="action-btn bib-caret-only" type="button" aria-label="更多" aria-haspopup="true" onclick="bibToggleMenu(event,\'bib-more-menu\')">' + moreSvg + '</button>'
      + '<span class="bib-menu bib-menu-end" id="bib-more-menu" role="menu">'
      + '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibMenuAction(event,\'redetect\',\'' + sid + '\')">重新识别</button>'
      + '</span>'
      + '</span>'
      + '</div>';
  } else {
    // 图书 / 学位论文：维持原有平铺工具条，不改交互。
    toolbarHTML = '<div class="bib-toolbar">'
      + (isBook ? '<button class="action-btn primary" type="button" onclick="lookupGoogleBooks(\'' + sid + '\')">查图书信息</button>' : '')
      + '<button class="action-btn" type="button" onclick="detectBibliographicMetadata(\'' + sid + '\',false)">自动识别</button>'
      + (meta.metadata_source === 'manual' ? '<button class="action-btn" type="button" onclick="detectBibliographicMetadata(\'' + sid + '\',true)">重新识别</button>' : '')
      + '</div>';
  }
  var lookupResultsHTML = isJournal
    ? '<div id="cnki-lookup-status" class="cnki-citation-result" role="status" aria-live="polite"></div>'
      + '<div id="cnki-candidate-list" class="cnki-candidate-list">' + cnkiCandidateListHTML(src.source_file_id) + '</div>'
      + '<div id="crossref-lookup-status" class="cnki-citation-result" role="status" aria-live="polite"></div>'
      + '<div id="crossref-candidate-list" class="cnki-candidate-list">' + crossrefCandidateListHTML(src.source_file_id) + '</div>'
    : (isBook
      ? '<div id="book-lookup-status" class="cnki-citation-result" role="status" aria-live="polite"></div>'
        + '<div id="book-candidate-list" class="cnki-candidate-list">' + bookCandidateListHTML(src.source_file_id) + '</div>'
      : '');
  var citationPanelHTML = isJournal
    ? '<div id="bib-citation-panel" class="bib-citation-panel" hidden>'
      + '<textarea id="bib-cnki-citation" maxlength="8000" rows="3" placeholder="粘贴知网 GB/T 7714 引文，如：作者.篇名[J].刊名,2020,49(04):15-27." onpaste="window.setTimeout(parseCnkiCitationText,0)"></textarea>'
      + '<div class="cnki-citation-actions"><button class="action-btn" type="button" onclick="parseCnkiCitationText()">从引用文字补全</button><span id="bib-cnki-citation-result" class="cnki-citation-result" role="status" aria-live="polite"></span></div>'
      + '</div>'
    : '';
  return '<div id="bibliographic-editor">'
    + '<div class="drawer-section-title">书目信息</div>'
    + '<div class="segmented-control bibliographic-type-control" id="bib-doctype-control" role="group" aria-label="文献类型">'
    + typeButton('book','著作') + typeButton('journal_article','期刊论文') + typeButton('thesis','学位论文')
    + '</div>'
    + bibliographicMissingBadge(Object.assign({}, meta, {document_type: docType, metadata_missing_fields: docType === bibliographicDocType(meta) ? meta.metadata_missing_fields : null}))
    + '<div class="bibliographic-grid">'
    + fieldsHTML + '</div>'
    + toolbarHTML
    + lookupResultsHTML
    + citationPanelHTML
    + '<div class="bib-footer"><span class="bibliographic-meta">状态：' + esc(metadataStatusLabel(meta.metadata_status)) + ' · 来源：' + esc(metadataSourceLabel(meta.metadata_source)) + '</span>'
    + '<span class="bib-footer-actions"><button class="action-btn" type="button" onclick="exitBibEdit(\'' + sid + '\')">取消</button>'
    + '<button class="action-btn primary" onclick="saveBibliographicMetadata(\'' + sid + '\')">保存书目信息</button></span></div>'
    + '</div>';
}

// 查看态：书目字段渲染成 label:value 只读行，缺失字段显示「—」并标黄。
// 直接点任意字段即进入编辑态并聚焦该字段（无需额外「编辑」按钮）；头部只留
// 按类型的主补全动作。与编辑态共用宿主 #bib-host，就地整块替换。
function bibliographicReadHTML(src) {
  var meta = sourceBibliographicMetadata(src);
  var docType = bibEditorTypeOverride[src.source_file_id] || bibliographicDocType(meta);
  var missing = bibliographicMissingFields(Object.assign({}, meta, {document_type: docType, metadata_missing_fields: docType === bibliographicDocType(meta) ? meta.metadata_missing_fields : null}));
  var sid = esc(src.source_file_id);
  var warnSvg = '<svg class="bib-read-warn" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg>';
  function row(label, fieldKey, value, full) {
    var isMissing = missing.indexOf(fieldKey) >= 0;
    var text = String(value == null ? '' : value).trim();
    var focusId = fieldKey.replace(/_/g, '-');  // 输入框 id 用连字符
    var edit = 'enterBibEdit(\'' + sid + '\',\'' + focusId + '\')';
    return '<div class="bib-read-row' + (full ? ' full' : '') + (isMissing ? ' is-missing' : '') + '"'
      + ' role="button" tabindex="0" title="点击编辑" onclick="' + edit + '"'
      + ' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();' + edit + ';}">'
      + '<span class="bib-read-label">' + label + '</span>'
      + '<span class="bib-read-value">' + (text ? esc(text) : '—') + (isMissing ? ' ' + warnSvg : '') + '</span></div>';
  }
  var rows;
  if (docType === 'thesis') {
    rows = row('作者','author',meta.author) + row('篇名','title',meta.title,true)
      + row('学校','publisher',meta.publisher) + row('年份','publish_year',meta.publish_year);
  } else if (docType === 'journal_article') {
    rows = row('篇名','title',meta.title,true) + row('作者','author',meta.author)
      + row('出版刊物','journal_name',meta.journal_name) + row('卷次','volume',meta.volume)
      + row('期号','issue',meta.issue) + row('年份','publish_year',meta.publish_year)
      + row('页码','page_range',meta.page_range) + row('DOI','doi',meta.doi) + row('ISSN','issn',meta.issn);
  } else {
    rows = row('作者','author',meta.author) + row('国别','country',meta.country)
      + row('书名','title',meta.title,true) + row('译者','translator',meta.translator)
      + row('出版地','publish_place',meta.publish_place) + row('出版社','publisher',meta.publisher)
      + row('出版年份','publish_year',meta.publish_year) + row('ISBN','isbn',meta.isbn,true);
  }
  // 类型未确认（从未识别过）：不伪装成「著作」红标缺字段，改用一句提示引导，
  // 主按钮固定为「自动识别」；已确认才显示缺失徽标并按类型给主补全按钮（L-05）。
  var confirmed = isBibliographicTypeConfirmed(meta);
  var primaryBtn = confirmed ? bibReadPrimaryButton(docType, sid)
    : '<button class="action-btn sm primary" type="button" onclick="bibEditAndRun(\'' + sid + '\',\'detect\')">自动识别</button>';
  var missingBadge = confirmed
    ? bibliographicMissingBadge(Object.assign({}, meta, {document_type: docType, metadata_missing_fields: docType === bibliographicDocType(meta) ? meta.metadata_missing_fields : null}))
    : '';
  var unconfirmedHint = confirmed ? ''
    : '<div class="bib-unconfirmed">尚未识别文献类型，点「自动识别」或任意字段手动选择类型并填写</div>';
  return '<div class="bib-read">'
    + '<div class="bib-section-head"><span class="drawer-section-title">书目信息</span>'
    + '<span class="bib-section-tools">' + primaryBtn + '</span></div>'
    + unconfirmedHint
    + missingBadge
    + '<div class="bib-read-grid">' + rows + '</div>'
    + '<div class="bibliographic-meta">状态：' + esc(metadataStatusLabel(meta.metadata_status)) + ' · 来源：' + esc(metadataSourceLabel(meta.metadata_source)) + '</div>'
    + '</div>';
}

// 查看态头部的主补全按钮：期刊→补全期刊信息，图书→查图书信息，学位→自动识别。
// 点它先进编辑态再执行（补全/识别本就是编辑动作，回填目标是编辑态的输入框）。
function bibReadPrimaryButton(docType, sid) {
  if (docType === 'journal_article')
    return '<button class="action-btn sm primary" type="button" onclick="bibEditAndRun(\'' + sid + '\',\'lookup\')">补全期刊信息</button>';
  if (docType === 'book' || docType === 'translated_book')
    return '<button class="action-btn sm primary" type="button" onclick="bibEditAndRun(\'' + sid + '\',\'books\')">查图书信息</button>';
  return '<button class="action-btn sm primary" type="button" onclick="bibEditAndRun(\'' + sid + '\',\'detect\')">自动识别</button>';
}

// 书目区渲染分发：查看态 / 编辑态，共用稳定宿主 #bib-host。
function renderBibliographicSection(src) {
  return '<div id="bib-host">'
    + (bibEditMode[src.source_file_id] ? bibliographicEditorHTML(src) : bibliographicReadHTML(src))
    + '</div>';
}

function enterBibEdit(sourceId, focusFieldId) {
  var src = libSources.find(function(item) { return item.source_file_id === sourceId; });
  var host = document.getElementById('bib-host');
  if (!src || !host) return;
  bibEditMode[sourceId] = true;
  host.innerHTML = bibliographicEditorHTML(src);
  // 点某字段进来的聚焦该字段；否则聚焦第一个。
  var target = (focusFieldId && host.querySelector('#bib-' + focusFieldId)) || host.querySelector('.bibliographic-field input');
  if (target) target.focus();
}

// 取消编辑：放弃表单里未保存的输入，清脏，回到查看态（显示当前已保存值）。
function exitBibEdit(sourceId) {
  var src = libSources.find(function(item) { return item.source_file_id === sourceId; });
  var host = document.getElementById('bib-host');
  bibEditMode[sourceId] = false;
  bibEditorDirty = false;
  delete bibEditorTypeOverride[sourceId];
  delete bibliographicPendingEvidence[sourceId];
  if (!src || !host) return;
  bibFieldCache[sourceId] = bibFieldCacheFromMeta(sourceBibliographicMetadata(src));
  host.innerHTML = bibliographicReadHTML(src);
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
  var src = libSources.find(function(item) { return item.source_file_id === sid; });
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
  if (menu) menu.innerHTML = bibSourceMenuHTML(sid, source);
  bibDispatchSource(sid, bibEffectiveSource(sid));
}

function bibMenuAction(ev, action, sid) {
  if (ev) ev.stopPropagation();
  bibCloseMenus();
  if (action === 'paste') return toggleCitationPanel();
  if (action === 'opencnki') return openCnkiSearch(sid);
  if (action === 'redetect') return detectBibliographicMetadata(sid, true);
}

function bibToggleMenu(ev, id) {
  if (ev) ev.stopPropagation();
  var menu = document.getElementById(id);
  if (!menu) return;
  var willOpen = !menu.classList.contains('open');
  bibCloseMenus();
  if (willOpen) menu.classList.add('open');
}

function bibCloseMenus() {
  var open = document.querySelectorAll('.bib-menu.open');
  for (var i = 0; i < open.length; i++) open[i].classList.remove('open');
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
  var src = libSources.find(function(item) { return item.source_file_id === sourceId; });
  var editor = document.getElementById('bibliographic-editor');
  if (!src || !editor) return;
  var template = document.createElement('template');
  template.innerHTML = bibliographicEditorHTML(src).trim();
  editor.replaceWith(template.content.firstElementChild);
  // 切换字段集时保留已填写的公共字段。
  Object.keys(current).forEach(function(key) {
    if (key === 'document_type' || !current[key]) return;
    var input = document.getElementById('bib-' + key.replace(/_/g, '-'));
    if (input && !input.value) input.value = current[key];
  });
}





function collectBibliographicForm() {
  var cache = (libSelectedId && bibFieldCache[libSelectedId]) || {};
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
    metadata_evidence: bibliographicPendingEvidence[libSelectedId] || {}
  };
  if (libSelectedId) {
    var store = bibFieldCache[libSelectedId] || (bibFieldCache[libSelectedId] = {});
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

function cnkiCandidateListHTML(sourceId) {
  var state = cnkiLookupState[sourceId] || {};
  var candidates = Array.isArray(state.candidates) ? state.candidates : [];
  if (!candidates.length) return '';
  return candidates.map(function(candidate, index) {
    return candidateCardHTML(sourceId, candidate, index, CNKI_CARD_CONFIG);
  }).join('');
}

// 三套联网补全共用的通用渲染 / 状态函数（原 render*/set*LookupStatus 六个函数已合并）。
// 差异只有宿主元素 id 与对应的候选列表函数，全部收进下面三个 *_LOOKUP 配置对象；
// 每个函数只操作 config 指定的宿主，期刊场景下 CNKI 与 Crossref 两组宿主互不影响。
function renderCandidates(config, sourceId) {
  var host = document.getElementById(config.listElId);
  if (host) host.innerHTML = config.listHTML(sourceId);
}

function setLookupStatus(config, message, warning) {
  var status = document.getElementById(config.statusElId);
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('has-warning', !!warning);
}

// listHTML 引用的三个候选列表函数为函数声明，已提升，const 初始化时可用。
const CNKI_LOOKUP = {
  stateMap: cnkiLookupState, endpoint: '/api/bibliographic-metadata/lookup-cnki',
  listElId: 'cnki-candidate-list', statusElId: 'cnki-lookup-status', listHTML: cnkiCandidateListHTML,
  loadingMessage: '正在查询知网…', defaultError: '知网查询失败',
  buildRequest: function (form) { return {title: form.title, author: form.author, publish_year: form.publish_year, journal_name: form.journal_name, doi: form.doi, issn: form.issn}; },
  validate: function (metadata) { return (!metadata.title && !metadata.doi) ? '请先填写篇名或 DOI' : null; },
  resetState: function () { return {candidates: [], open_url: cnkiSearchUrlFromForm()}; },
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
  listElId: 'book-candidate-list', statusElId: 'book-lookup-status', listHTML: bookCandidateListHTML,
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
  listElId: 'crossref-candidate-list', statusElId: 'crossref-lookup-status', listHTML: crossrefCandidateListHTML,
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
    var resp = await fetch(config.endpoint, {
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

function bookCandidateListHTML(sourceId) {
  var candidates = ((bookLookupState[sourceId] || {}).candidates) || [];
  if (!candidates.length) return '';
  return candidates.map(function(candidate, index) {
    return candidateCardHTML(sourceId, candidate, index, BOOK_CARD_CONFIG);
  }).join('');
}

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
function crossrefCandidateListHTML(sourceId) {
  var candidates = ((crossrefLookupState[sourceId] || {}).candidates) || [];
  if (!candidates.length) return '';
  return candidates.map(function(candidate, index) {
    return candidateCardHTML(sourceId, candidate, index, CROSSREF_CARD_CONFIG);
  }).join('');
}

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
    var resp = await fetch('/api/bibliographic-metadata/cnki-candidate', {
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

function cnkiSearchUrlFromForm() {
  var form = collectBibliographicForm();
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
    var resp = await fetch('/api/bibliographic-metadata/open-cnki', {
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
    var resp = await fetch('/api/bibliographic-metadata/parse-cnki-citation', {
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
    var applied = applyBibliographicLookupMetadata(libSelectedId, data.metadata, citationEvidence);
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
    var resp = await fetch('/api/bibliographic-metadata/detect', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,force:!!force})});
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '识别失败');
    var src = libSources.find(function(item){return item.source_file_id === sourceId;});
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
    var resp = await fetch('/api/bibliographic-metadata/save', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:sourceId,metadata:collectBibliographicForm()})});
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '保存失败');
    showToast('书目信息已保存并立即生效', 'success');
    bibEditorDirty = false;
    bibEditMode[sourceId] = false;  // 保存后回到查看态
    delete bibEditorTypeOverride[sourceId];
    delete bibliographicPendingEvidence[sourceId];
    delete bibFieldCache[sourceId];
    await loadLibrary(true);
    await selectLibDoc(sourceId);
  } catch(e) { showToast('保存失败：' + e.message, 'danger'); }
}

async function openMetadataForSource(sourceId) {
  navigateTo('library');
  if (!libLoaded) await loadLibrary();
  await selectLibDoc(sourceId);
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
  libSelectedId = null;
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
  try {
    var resp = await fetch('/api/mineru-reparse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '提交失败');
    showToast(data.already_running ? 'MinerU 解析已在进行中' : '已提交 MinerU 解析，完成后自动重建索引');
    calTransientStatus[sourceId] = 'mapping';
    updateLibraryEntry(sourceId);
    if (libSelectedId === sourceId) selectLibDoc(sourceId);
    pollMineruReparse(sourceId, data.job_id);
  } catch(e) {
    showToast('提交 MinerU 解析失败：' + e.message, 'danger');
  }
}

function pollMineruReparse(sourceId, jobId) {
  fetch('/api/import-status?job_id=' + encodeURIComponent(jobId))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data.status === 'completed') {
        delete calTransientStatus[sourceId];
        showToast('MinerU 解析完成，索引已更新', 'success');
        refreshCalibrationSource(sourceId).then(function() {
          if (libSelectedId === sourceId) selectLibDoc(sourceId);
        }).catch(function() {});
        return;
      }
      if (data.status === 'failed' || data.error) {
        delete calTransientStatus[sourceId];
        updateLibraryEntry(sourceId);
        if (libSelectedId === sourceId) selectLibDoc(sourceId);
        showToast('MinerU 解析失败：' + (data.message || data.error || '未知错误'), 'danger');
        return;
      }
      setTimeout(function() { pollMineruReparse(sourceId, jobId); }, 4000);
    })
    .catch(function() {
      setTimeout(function() { pollMineruReparse(sourceId, jobId); }, 8000);
    });
}

async function acceptAutoMapping(sourceId) {
  if (!sourceId) return;
  try {
    showToast('正在接受自动映射…');
    var resp = await fetch('/api/auto-page-mapping/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '接受失败');
    showToast('自动映射已接受为人工映射', 'success');
    await loadMeta();
    await loadLibrary(true);
    await selectLibDoc(sourceId);
  } catch(e) {
    showToast('接受失败：' + e.message, 'danger');
  }
}

function showAutoMappingExceptions(sourceId) {
  var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
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
  if (!libLoaded) await loadLibrary();
  await selectLibDoc(sourceId);
  await toggleDrawerCalibration(true);
  var host = document.getElementById('library-drawer-calibration');
  if (host) host.scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function openCalibrationAndDetect(sourceId) {
  await openCalibrationForSource(sourceId);
  await runAutoDetection(sourceId);
}

