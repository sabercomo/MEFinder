

// bibliographicFieldLabels / bibliographicDocType / bibliographicEditorDocType /
// bibliographicMissingFields 已抽到 06-pure.js（纯逻辑，可单测）。



let bibEditorTypeOverride = {};
let bibLookupSource = {};
let cnkiLookupState = {};
let bookLookupState = {};
let crossrefLookupState = {};
let bibliographicPendingEvidence = {};

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
      + '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibMenuAction(event,\'evidence\',\'' + sid + '\')">查看识别依据</button>'
      + '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibMenuAction(event,\'redetect\',\'' + sid + '\')">重新识别</button>'
      + '</span>'
      + '</span>'
      + '</div>';
  } else {
    // 图书 / 学位论文：维持原有平铺工具条，不改交互。
    toolbarHTML = '<div class="bib-toolbar">'
      + (isBook ? '<button class="action-btn primary" type="button" onclick="lookupGoogleBooks(\'' + sid + '\')">查图书信息</button>' : '')
      + '<button class="action-btn" type="button" onclick="detectBibliographicMetadata(\'' + sid + '\',false)">自动识别</button>'
      + '<button class="action-btn" type="button" onclick="showBibliographicEvidence(\'' + sid + '\')">识别依据</button>'
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
    + '<button class="action-btn primary" onclick="saveBibliographicMetadata(\'' + sid + '\')">保存</button></div>'
    + '</div>';
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
  if (action === 'evidence') return showBibliographicEvidence(sid);
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
  function value(id) { var el = document.getElementById('bib-' + id); return el ? el.value.trim() : ''; }
  var typeButton = document.querySelector('#bib-doctype-control .seg-btn.active');
  var editorDocType = typeButton ? typeButton.dataset.doctype : 'book';
  var translator = value('translator');
  return {
    document_type: bibliographicFormDocType(editorDocType, translator),
    author: value('author'), country: value('country'), title: value('title'),
    translator: translator, publish_place: value('publish-place'),
    publisher: value('publisher'), publish_year: value('publish-year'), isbn: value('isbn'),
    journal_name: value('journal-name'), volume: value('volume'),
    issue: value('issue'), page_range: value('page-range'), doi: value('doi'), issn: value('issn'),
    metadata_evidence: bibliographicPendingEvidence[libSelectedId] || {}
  };
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

function applyBibliographicLookupMetadata(sourceId, metadata, evidence) {
  var filled = [];
  var preserved = [];
  metadata = metadata || {};
  evidence = evidence || {};
  Object.keys(bibliographicLookupFields).forEach(function(key) {
    var incoming = String(metadata[key] || '').trim();
    var field = bibliographicLookupFields[key];
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
  return {filled:filled, preserved:preserved};
}

// bibliographicValuesEquivalent 已抽到 06-pure.js（纯逻辑，可单测）。

function cnkiCandidateListHTML(sourceId) {
  var state = cnkiLookupState[sourceId] || {};
  var candidates = Array.isArray(state.candidates) ? state.candidates : [];
  if (!candidates.length) return '';
  return candidates.map(function(candidate, index) {
    var meta = candidate.metadata || {};
    var match = candidate.match || {};
    var levelLabel = match.level === 'high' ? '高匹配' : (match.level === 'medium' ? '需核对' : '低匹配');
    var detail = [meta.author, meta.journal_name, candidate.publish_date || meta.publish_year].filter(Boolean).join(' · ');
    var reasons = (match.reasons || []).join('、');
    var conflicts = (match.conflicts || []).join('、');
    return '<div class="cnki-candidate ' + esc(match.level || 'low') + '">'
      + '<div class="cnki-candidate-main"><div class="cnki-candidate-title">' + esc(meta.title || '未识别篇名') + '</div>'
      + '<div class="cnki-candidate-detail">' + esc(detail || '联网记录') + '</div>'
      + '<div class="cnki-candidate-match"><span>' + esc(levelLabel) + (match.score != null ? ' · ' + Math.round(Number(match.score) * 100) + '%' : '') + '</span>'
      + (reasons ? '<span>' + esc(reasons) + '</span>' : '')
      + (conflicts ? '<span class="has-warning">冲突：' + esc(conflicts) + '</span>' : '') + '</div></div>'
      + '<div class="cnki-candidate-actions"><button class="action-btn" type="button" onclick="applyCnkiSearchCandidate(\'' + esc(sourceId) + '\',' + index + ')">先补列表字段</button>'
      + '<button class="action-btn primary" type="button" onclick="fetchCnkiCandidate(\'' + esc(sourceId) + '\',' + index + ')">获取完整题录</button>'
      + '<button class="action-btn" type="button" onclick="openCnkiCandidate(\'' + esc(sourceId) + '\',' + index + ')">打开记录</button></div>'
      + '</div>';
  }).join('');
}

function renderCnkiCandidates(sourceId) {
  var host = document.getElementById('cnki-candidate-list');
  if (host) host.innerHTML = cnkiCandidateListHTML(sourceId);
}

function setCnkiLookupStatus(message, warning) {
  var status = document.getElementById('cnki-lookup-status');
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('has-warning', !!warning);
}

async function lookupCnkiMetadata(sourceId) {
  var form = collectBibliographicForm();
  var metadata = {
    title:form.title, author:form.author, publish_year:form.publish_year,
    journal_name:form.journal_name, doi:form.doi, issn:form.issn
  };
  if (!metadata.title && !metadata.doi) {
    setCnkiLookupStatus('请先填写篇名或 DOI', true);
    return;
  }
  cnkiLookupState[sourceId] = {candidates:[], open_url:cnkiSearchUrlFromForm()};
  renderCnkiCandidates(sourceId);
  setCnkiLookupStatus('正在查询知网…', false);
  try {
    var resp = await fetch('/api/bibliographic-metadata/lookup-cnki', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({metadata:metadata})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) {
      if (data.open_url) cnkiLookupState[sourceId] = {candidates:[], open_url:data.open_url};
      throw new Error(data.error || '知网查询失败');
    }
    cnkiLookupState[sourceId] = {candidates:data.candidates || [], open_url:data.open_url || ''};
    renderCnkiCandidates(sourceId);
    var notice = data.query_notice ? data.query_notice + '；' : '';
    if (!data.candidates || !data.candidates.length) {
      setCnkiLookupStatus(notice + '知网未返回候选，可打开知网检索或粘贴引用文字', true);
    } else if (data.candidates.length === 1 && data.candidates[0].match && data.candidates[0].match.level === 'high') {
      setCnkiLookupStatus(notice + '找到 1 条高匹配候选，请核对后获取完整题录', false);
    } else {
      setCnkiLookupStatus(notice + '找到 ' + data.candidates.length + ' 条候选，请选择正确记录', true);
    }
  } catch(e) {
    renderCnkiCandidates(sourceId);
    setCnkiLookupStatus(e.message + '；可打开知网检索或粘贴引用文字', true);
  }
}

function applyCnkiSearchCandidate(sourceId, index) {
  var candidate = ((cnkiLookupState[sourceId] || {}).candidates || [])[index];
  if (!candidate) return;
  var applied = applyBibliographicLookupMetadata(sourceId, candidate.metadata, candidate.evidence);
  var message = applied.filled.length ? '已补全：' + applied.filled.join('、') : '表单已有对应内容，未作覆盖';
  if (applied.preserved.length) message += '；已有值未覆盖：' + applied.preserved.join('、');
  setCnkiLookupStatus(message + '。请检查后保存', applied.preserved.length > 0);
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
    var meta = candidate.metadata || {};
    var match = candidate.match || {};
    var levelLabel = match.level === 'high' ? '高匹配' : (match.level === 'medium' ? '需核对' : '低匹配');
    var detail = [meta.author, meta.publisher, candidate.publish_date || meta.publish_year].filter(Boolean).join(' · ');
    var reasons = (match.reasons || []).join('、');
    var conflicts = (match.conflicts || []).join('、');
    return '<div class="cnki-candidate ' + esc(match.level || 'low') + '">'
      + '<div class="cnki-candidate-main"><div class="cnki-candidate-title">' + esc(meta.title || '未识别书名') + '</div>'
      + '<div class="cnki-candidate-detail">' + esc(detail || '图书目录记录') + (meta.isbn ? ' · ISBN ' + esc(meta.isbn) : '') + '</div>'
      + '<div class="cnki-candidate-match"><span>' + esc(levelLabel) + (match.score != null ? ' · ' + Math.round(Number(match.score) * 100) + '%' : '') + '</span>'
      + (reasons ? '<span>' + esc(reasons) + '</span>' : '')
      + (conflicts ? '<span class="has-warning">冲突：' + esc(conflicts) + '</span>' : '') + '</div></div>'
      + '<div class="cnki-candidate-actions"><button class="action-btn primary" type="button" onclick="applyBookCandidate(\'' + esc(sourceId) + '\',' + index + ')">补全书目字段</button></div>'
      + '</div>';
  }).join('');
}

function renderBookCandidates(sourceId) {
  var host = document.getElementById('book-candidate-list');
  if (host) host.innerHTML = bookCandidateListHTML(sourceId);
}

function setBookLookupStatus(message, warning) {
  var status = document.getElementById('book-lookup-status');
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('has-warning', !!warning);
}

async function lookupGoogleBooks(sourceId) {
  var form = collectBibliographicForm();
  var metadata = {title:form.title, author:form.author, publish_year:form.publish_year, isbn:form.isbn};
  if (!metadata.isbn && !metadata.title) {
    setBookLookupStatus('请先填写 ISBN 或书名', true);
    return;
  }
  bookLookupState[sourceId] = {candidates:[]};
  renderBookCandidates(sourceId);
  setBookLookupStatus('正在查询图书目录…', false);
  try {
    var resp = await fetch('/api/bibliographic-metadata/lookup-google-books', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({metadata:metadata})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '图书查询失败');
    bookLookupState[sourceId] = {candidates:data.candidates || [], open_url:data.open_url || ''};
    renderBookCandidates(sourceId);
    if (!data.candidates || !data.candidates.length) {
      setBookLookupStatus('未找到匹配图书，可核对 ISBN/书名或手动填写', true);
    } else if (data.candidates.length === 1 && data.candidates[0].match && data.candidates[0].match.level === 'high') {
      setBookLookupStatus('找到 1 条高匹配图书，请核对后补全', false);
    } else {
      setBookLookupStatus('找到 ' + data.candidates.length + ' 条候选，请选择正确的图书', true);
    }
  } catch(e) {
    renderBookCandidates(sourceId);
    setBookLookupStatus(e.message, true);
  }
}

// 图书候选：只把当前为空的图书字段补进表单，绝不覆盖已有值。
function applyBookCandidate(sourceId, index) {
  var candidate = ((bookLookupState[sourceId] || {}).candidates || [])[index];
  if (!candidate) return;
  var meta = candidate.metadata || {};
  var evidence = candidate.evidence || {};
  var filled = [];
  var preserved = [];
  Object.keys(bookLookupFields).forEach(function(key) {
    var incoming = String(meta[key] || '').trim();
    var field = bookLookupFields[key];
    var input = document.getElementById('bib-' + field.id);
    if (!incoming || !input) return;
    var existing = input.value.trim();
    if (!existing) {
      input.value = incoming;
      filled.push(field.label);
      if (evidence[key]) {
        if (!bibliographicPendingEvidence[sourceId]) bibliographicPendingEvidence[sourceId] = {};
        bibliographicPendingEvidence[sourceId][key] = Object.assign({}, evidence[key], {value:incoming});
      }
    } else if (!bibliographicValuesEquivalent(key, existing, incoming)) {
      preserved.push(field.label);
    }
  });
  refreshBibliographicMissingDisplay();
  var message = filled.length ? '已补全：' + filled.join('、') : '表单已有对应内容，未作覆盖';
  if (preserved.length) message += '；已有值未覆盖：' + preserved.join('、');
  setBookLookupStatus(message + '。请检查后保存', preserved.length > 0);
}

/* ═══ Crossref 外文期刊论文补全 ═══
 * DOI 直连最准，无 DOI 用篇名+作者搜；干净 JSON，一次返回完整题录。
 * 期刊字段与知网一致，复用 applyBibliographicLookupMetadata 只补空字段。 */
function crossrefCandidateListHTML(sourceId) {
  var candidates = ((crossrefLookupState[sourceId] || {}).candidates) || [];
  if (!candidates.length) return '';
  return candidates.map(function(candidate, index) {
    var meta = candidate.metadata || {};
    var match = candidate.match || {};
    var levelLabel = match.level === 'high' ? '高匹配' : (match.level === 'medium' ? '需核对' : '低匹配');
    var detail = [meta.author, meta.journal_name, candidate.publish_date || meta.publish_year].filter(Boolean).join(' · ');
    var reasons = (match.reasons || []).join('、');
    var conflicts = (match.conflicts || []).join('、');
    return '<div class="cnki-candidate ' + esc(match.level || 'low') + '">'
      + '<div class="cnki-candidate-main"><div class="cnki-candidate-title">' + esc(meta.title || '未识别篇名') + '</div>'
      + '<div class="cnki-candidate-detail">' + esc(detail || 'Crossref 记录') + (meta.doi ? ' · DOI ' + esc(meta.doi) : '') + '</div>'
      + '<div class="cnki-candidate-match"><span>' + esc(levelLabel) + (match.score != null ? ' · ' + Math.round(Number(match.score) * 100) + '%' : '') + '</span>'
      + (reasons ? '<span>' + esc(reasons) + '</span>' : '')
      + (conflicts ? '<span class="has-warning">冲突：' + esc(conflicts) + '</span>' : '') + '</div></div>'
      + '<div class="cnki-candidate-actions"><button class="action-btn primary" type="button" onclick="applyCrossrefCandidate(\'' + esc(sourceId) + '\',' + index + ')">补全书目字段</button></div>'
      + '</div>';
  }).join('');
}

function renderCrossrefCandidates(sourceId) {
  var host = document.getElementById('crossref-candidate-list');
  if (host) host.innerHTML = crossrefCandidateListHTML(sourceId);
}

function setCrossrefLookupStatus(message, warning) {
  var status = document.getElementById('crossref-lookup-status');
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('has-warning', !!warning);
}

async function lookupCrossref(sourceId) {
  var form = collectBibliographicForm();
  var metadata = {title:form.title, author:form.author, publish_year:form.publish_year, doi:form.doi};
  if (!metadata.doi && !metadata.title) {
    setCrossrefLookupStatus('请先填写 DOI 或篇名', true);
    return;
  }
  crossrefLookupState[sourceId] = {candidates:[]};
  renderCrossrefCandidates(sourceId);
  setCrossrefLookupStatus('正在查询 Crossref…', false);
  try {
    var resp = await fetch('/api/bibliographic-metadata/lookup-crossref', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({metadata:metadata})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'Crossref 查询失败');
    crossrefLookupState[sourceId] = {candidates:data.candidates || [], open_url:data.open_url || ''};
    renderCrossrefCandidates(sourceId);
    if (!data.candidates || !data.candidates.length) {
      setCrossrefLookupStatus('Crossref 未找到匹配文献，可核对 DOI/篇名或手动填写', true);
    } else if (data.candidates.length === 1 && data.candidates[0].match && data.candidates[0].match.level === 'high') {
      setCrossrefLookupStatus('找到 1 条高匹配文献，请核对后补全', false);
    } else {
      setCrossrefLookupStatus('找到 ' + data.candidates.length + ' 条候选，请选择正确的文献', true);
    }
  } catch(e) {
    renderCrossrefCandidates(sourceId);
    setCrossrefLookupStatus(e.message, true);
  }
}

function applyCrossrefCandidate(sourceId, index) {
  var candidate = ((crossrefLookupState[sourceId] || {}).candidates || [])[index];
  if (!candidate) return;
  var applied = applyBibliographicLookupMetadata(sourceId, candidate.metadata, candidate.evidence);
  var message = applied.filled.length ? '已补全：' + applied.filled.join('、') : '表单已有对应内容，未作覆盖';
  if (applied.preserved.length) message += '；已有值未覆盖：' + applied.preserved.join('、');
  setCrossrefLookupStatus(message + '。请检查后保存', applied.preserved.length > 0);
}

async function fetchCnkiCandidate(sourceId, index) {
  var candidate = ((cnkiLookupState[sourceId] || {}).candidates || [])[index];
  if (!candidate || !candidate.record_url) return;
  setCnkiLookupStatus('正在读取知网完整题录…', false);
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
    setCnkiLookupStatus(message + '。请检查后保存', applied.preserved.length > 0);
    showToast('知网题录已载入，请检查后保存', 'success');
  } catch(e) {
    setCnkiLookupStatus(e.message + '；可打开记录后粘贴引用文字', true);
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
    delete bibEditorTypeOverride[sourceId];
    delete bibliographicPendingEvidence[sourceId];
    await loadLibrary(true);
    await selectLibDoc(sourceId);
  } catch(e) { showToast('保存失败：' + e.message, 'danger'); }
}

function showBibliographicEvidence(sourceId) {
  var src = libSources.find(function(item){return item.source_file_id === sourceId;});
  var metadata = sourceBibliographicMetadata(src);
  var evidence = metadata.metadata_evidence || {};
  var labels = {title:'书名',author:'作者',country:'国别',translator:'译者',publisher:'出版社',publish_place:'出版地',publish_year:'出版年份',isbn:'ISBN',journal_name:'出版刊物',volume:'卷次',issue:'期号',page_range:'页码',doi:'DOI',issn:'ISSN'};
  if (bibliographicDocType(metadata) === 'thesis') {
    labels.title = '篇名';
    labels.publisher = '学校';
    labels.publish_year = '年份';
  }
  var lines = Object.keys(evidence).map(function(field) {
    var item = evidence[field] || {};
    return (labels[field] || field) + '：' + (item.evidence_text || '无文本依据') + (item.source_page != null ? '（PDF 第 ' + item.source_page + ' 页）' : '') + (item.source === 'inferred_from_publisher' ? '（由出版社推断）' : '') + (item.record_url ? '\n知网记录：' + item.record_url : '');
  });
  showAppAlert(lines.length ? lines.join('\n') : '暂无自动识别依据', {title:'自动识别依据'});
}

async function openMetadataForSource(sourceId) {
  navigateTo('library');
  if (!libLoaded) await loadLibrary();
  await selectLibDoc(sourceId);
}

function closeLibDrawer() {
  libSelectedId = null;
  calSelectedSourceId = null;
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

