/* ═══ Import ═══ */
let importQueue = [];

function visionRetryProviderFor(q) {
  if (!q || q.status !== 'error') return null;
  if (q.failureStage === 'index' || q.mineruInterrupted) return null;
  if (!q.canRetryVision && !q.needsProviderConfig && !q.mineruFailed) return null;
  var providers = configuredVisionProviders();
  var preferredId = q.retryProviderId || '';
  return providers.find(function(provider) {
    return provider.id === preferredId;
  }) || providers[0] || null;
}

function initDropZone() {
  var zone = document.getElementById('drop-zone');
  if (!zone) return;
  zone.addEventListener('dragover', function(e) {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', function() {
    zone.classList.remove('dragover');
  });
  zone.addEventListener('drop', function(e) {
    e.preventDefault();
    zone.classList.remove('dragover');
    handleFileSelect(e.dataTransfer.files);
  });
}

async function runBatchMetadataDetection() {
  var button = document.getElementById('batch-metadata-btn');
  try {
    var resp = await fetch('/api/bibliographic-metadata/batch-detect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '启动失败');
    if (!data.job_id) {
      // 「补全书目」只做本地识别（读 PDF 封面/版权页/CIP），不联网。
      showToast('没有需要本地识别的文献');
      if (button) { button.disabled = false; button.textContent = '补全书目'; }
      return;
    }
    showToast(data.already_running ? '批量识别已在进行中' : '已开始批量识别 ' + (data.candidates || '') + ' 部文献');
    if (button) { button.disabled = true; button.textContent = '识别中…'; }
    pollBatchMetadata(data.job_id, button);
  } catch (e) {
    showToast('批量识别失败：' + e.message);
    if (button) { button.disabled = false; button.textContent = '补全书目'; }
  }
}

function pollBatchMetadata(jobId, button) {
  fetch('/api/import-status?job_id=' + encodeURIComponent(jobId))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data.status === 'completed') {
        if (button) { button.disabled = false; button.textContent = '补全书目'; }
        showToast(data.message || '批量识别完成');
        loadLibrary(true);
        return;
      }
      if (data.status === 'failed' || data.error) {
        if (button) { button.disabled = false; button.textContent = '补全书目'; }
        showToast('批量识别失败：' + (data.message || data.error || '未知错误'));
        return;
      }
      if (button && data.message) button.textContent = data.message.length > 18 ? '识别中…' : data.message;
      setTimeout(function() { pollBatchMetadata(jobId, button); }, 2000);
    })
    .catch(function() {
      setTimeout(function() { pollBatchMetadata(jobId, button); }, 4000);
    });
}

/* ═══ 联网知网批量补全（茉莉花式候选选择）═══
 * 复用单篇详情里的 lookup-cnki / cnki-candidate / save 端点：顺序处理每一篇
 * 缺信息的期刊论文，天然满足知网单并发与“只补空字段”约束。高匹配唯一候选
 * 自动补，其余弹出候选选择框由用户决定，全程可随时停止。 */
let cnkiBatchActive = false;
let cnkiBatchChoiceResolve = null;
let cnkiBatchCandidates = [];
let cnkiBatchOpenUrl = '';

// isForeignTitle / batchLookupSourceFor 已抽到 06-pure.js（纯逻辑，可单测）。

function cnkiBatchLookupTargets() {
  return (libSources || []).filter(function(src) {
    if (!src || String(src.source_type || '') !== 'pdf') return false;
    var meta = sourceBibliographicMetadata(src);
    // 人工维护的文献也纳入：applyBatchCandidateToSource 只补当前为空的字段，
    // 绝不覆盖已手动填写的值，因此不会破坏人工维护的内容。
    if (!batchLookupSourceFor(meta)) return false;
    var hasQueryKey = String(meta.title || '').trim() || String(meta.doi || '').trim() || String(meta.isbn || '').trim();
    if (!hasQueryKey) return false;
    return bibliographicMissingFields(meta).length > 0;
  });
}

async function runCnkiBatchButton() {
  await startCnkiBatchCompletion(document.getElementById('batch-cnki-btn'));
}

// 由「联网补全期刊信息」按钮触发：点击按钮本身即为联网授权，不再逐次弹确认框。
async function startCnkiBatchCompletion(button) {
  if (cnkiBatchActive) return;
  var buttonLabel = button ? button.textContent : '';
  var targets = cnkiBatchLookupTargets();
  if (!targets.length) {
    showToast('没有需要联网补全的文献');
    return;
  }
  cnkiBatchActive = true;
  var stats = {auto:0, manual:0, notfound:0, skipped:0, failed:0};
  var stopped = false;
  var abortReason = '';
  for (var i = 0; i < targets.length; i++) {
    var src = targets[i];
    if (button) { button.disabled = true; button.textContent = '联网补全 ' + (i + 1) + '/' + targets.length + '…'; }
    var outcome;
    try {
      outcome = await processCnkiBatchItem(src, sourceBibliographicMetadata(src), i + 1, targets.length);
    } catch (e) {
      stats.failed++;
      continue;
    }
    if (outcome.action === 'stop') { stopped = true; break; }
    if (outcome.action === 'abort') { stopped = true; abortReason = outcome.reason || ''; break; }
    stats[outcome.result] = (stats[outcome.result] || 0) + 1;
  }
  closeCnkiBatchModal();
  cnkiBatchActive = false;
  if (button) { button.disabled = false; button.textContent = buttonLabel || '联网补全'; }
  await loadLibrary(true);
  var parts = [];
  if (stats.auto) parts.push('自动补全 ' + stats.auto + ' 篇');
  if (stats.manual) parts.push('手动选择 ' + stats.manual + ' 篇');
  if (stats.notfound) parts.push('未找到 ' + stats.notfound + ' 篇');
  if (stats.skipped) parts.push('跳过 ' + stats.skipped + ' 篇');
  if (stats.failed) parts.push('失败 ' + stats.failed + ' 篇');
  var summary = parts.join('、') || '无变化';
  if (abortReason) {
    showToast('联网源暂时不可用（' + abortReason + '），已停止。已处理：' + summary, 'warning');
  } else {
    showToast((stopped ? '已停止联网补全：' : '联网补全完成：') + summary, stats.failed ? 'warning' : 'success');
  }
}

var _BATCH_SOURCE_META = {
  cnki: {endpoint:'/api/bibliographic-metadata/lookup-cnki', label:'知网', evSource:'cnki_lookup'},
  crossref: {endpoint:'/api/bibliographic-metadata/lookup-crossref', label:'Crossref', evSource:'crossref'},
  google_books: {endpoint:'/api/bibliographic-metadata/lookup-google-books', label:'图书目录', evSource:'k10plus'}
};

function loadOnlineAutoMatchThreshold() {
  var raw = null;
  try { raw = localStorage.getItem('meFinderOnlineAutoMatchThreshold'); } catch (_) {}
  var pct = Math.round(Number(raw));
  if (!Number.isFinite(pct)) return ONLINE_METADATA_AUTO_MATCH_THRESHOLD_DEFAULT;
  pct = Math.min(100, Math.max(ONLINE_METADATA_AUTO_MATCH_MIN_PERCENT, pct));
  return pct / 100;
}

function setOnlineAutoMatchThreshold(pct) {
  var value = Math.round(Number(pct));
  if (!Number.isFinite(value)) return;
  value = Math.min(100, Math.max(ONLINE_METADATA_AUTO_MATCH_MIN_PERCENT, value));
  onlineMetadataAutoMatchThreshold = value / 100;
  try { localStorage.setItem('meFinderOnlineAutoMatchThreshold', String(value)); } catch (_) {}
  syncOnlineAutoMatchControl();
}

function syncOnlineAutoMatchControl() {
  var pct = Math.round(onlineMetadataAutoMatchThreshold * 100);
  var slider = document.getElementById('online-auto-match-range');
  var label = document.getElementById('online-auto-match-value');
  if (slider && String(slider.value) !== String(pct)) slider.value = String(pct);
  if (label) label.textContent = pct + '%';
}

function automaticBatchCandidateIndex(candidates) {
  var bestIndex = -1;
  var bestScore = -1;
  (candidates || []).forEach(function(candidate, index) {
    var score = Number(candidate && candidate.match && candidate.match.score);
    if (Number.isFinite(score) && score >= onlineMetadataAutoMatchThreshold && score > bestScore) {
      bestIndex = index;
      bestScore = score;
    }
  });
  return bestIndex;
}

async function processCnkiBatchItem(src, meta, index, total) {
  var sourceId = src.source_file_id;
  var source = batchLookupSourceFor(meta);
  var info = _BATCH_SOURCE_META[source];
  var resp = await fetch(info.endpoint, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({metadata:batchQueryFor(source, meta)})
  });
  var data = await resp.json();
  if (!resp.ok || !data.ok) {
    // 验证码或限流表示站点在拦截：立即停止整批，不要继续冲击。
    if (data.code === 'verification_required' || data.code === 'rate_limited') {
      return {action:'abort', reason: info.label + (data.code === 'rate_limited' ? '限流' : '需要验证')};
    }
    throw new Error(data.error || (info.label + '查询失败'));
  }
  var candidates = data.candidates || [];
  if (!candidates.length) return {action:'next', result:'notfound'};
  var automaticIndex = automaticBatchCandidateIndex(candidates);
  if (automaticIndex >= 0) {
    var ok = await applyBatchCandidateToSource(sourceId, meta, candidates[automaticIndex], source);
    return {action:'next', result: ok ? 'auto' : 'failed'};
  }
  var choice = await promptCnkiBatchChoice(src, meta, candidates, data.open_url, index, total, info.label);
  if (choice.action === 'stop') return {action:'stop'};
  if (choice.action !== 'select') return {action:'next', result:'skipped'};
  var applied = await applyBatchCandidateToSource(sourceId, meta, candidates[choice.index], source);
  return {action:'next', result: applied ? 'manual' : 'failed'};
}

// 仅把当前为空的字段补进去，其余保持原样后整份保存。知网需再取详情页完整题录；
// Crossref / Google Books 的候选一次即完整。图书补图书字段，期刊补期刊字段。
async function applyBatchCandidateToSource(sourceId, currentMeta, candidate, source) {
  var fullMeta = candidate.metadata || {};
  var evidence = candidate.evidence || {};
  if (source === 'cnki' && candidate.record_url) {
    try {
      var resp = await fetch('/api/bibliographic-metadata/cnki-candidate', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({candidate:{record_url:candidate.record_url}})
      });
      var data = await resp.json();
      if (resp.ok && data.ok && data.metadata) {
        fullMeta = data.metadata;
        evidence = data.evidence || evidence;
      }
    } catch (e) { /* 详情读取失败时退回列表级字段 */ }
  }
  var payload = {};
  ['author','country','title','translator','publish_place','publisher','publish_year','isbn','journal_name','volume','issue','page_range','doi','issn'].forEach(function(k) {
    payload[k] = String(currentMeta[k] || '').trim();
  });
  payload.document_type = bibliographicDocType(currentMeta);
  var fillKeys = source === 'google_books'
    ? ['author','title','publisher','publish_place','publish_year','isbn']
    : Object.keys(bibliographicLookupFields);
  var defaultEvSource = _BATCH_SOURCE_META[source].evSource;
  var evidenceOut = {};
  var filledAny = false;
  fillKeys.forEach(function(k) {
    var incoming = String(fullMeta[k] || '').trim();
    if (!incoming || payload[k]) return;  // 只补当前为空的字段
    payload[k] = incoming;
    filledAny = true;
    var ev = evidence[k] || {source:defaultEvSource, evidence_text: incoming};
    evidenceOut[k] = Object.assign({}, ev, {value: incoming});
  });
  if (!filledAny) return false;
  payload.metadata_evidence = evidenceOut;
  var saveResp = await fetch('/api/bibliographic-metadata/save', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({source_id:sourceId, metadata:payload})
  });
  var saveData = await saveResp.json();
  return saveResp.ok && !!saveData.ok;
}

function promptCnkiBatchChoice(src, meta, candidates, openUrl, index, total, sourceLabel) {
  var backdrop = document.getElementById('cnki-batch-modal');
  var docEl = document.getElementById('cnki-batch-doc');
  var progressEl = document.getElementById('cnki-batch-progress');
  var listEl = document.getElementById('cnki-batch-list');
  if (!backdrop || !docEl || !listEl) return Promise.resolve({action:'skip'});
  cnkiBatchCandidates = candidates;
  cnkiBatchOpenUrl = openUrl || '';
  if (progressEl) progressEl.textContent = '第 ' + index + '/' + total + ' 条 · 请从' + (sourceLabel || '联网结果') + '选择正确记录';
  var docTitle = meta.title || (src.file_name || src.source_file_id);
  var docMeta = [meta.author, meta.publish_year, meta.journal_name || meta.publisher].filter(Boolean).join(' · ');
  docEl.innerHTML = '<div class="cnki-batch-doc-title">' + esc(docTitle) + '</div>'
    + (docMeta ? '<div class="cnki-batch-doc-meta">本地信息：' + esc(docMeta) + '</div>' : '');
  listEl.innerHTML = candidates.map(function(candidate, i) {
    var m = candidate.metadata || {};
    var match = candidate.match || {};
    var levelLabel = match.level === 'high' ? '高匹配' : (match.level === 'medium' ? '需核对' : '低匹配');
    var detail = [m.author, m.journal_name || m.publisher, candidate.publish_date || m.publish_year].filter(Boolean).join(' · ');
    var reasons = (match.reasons || []).join('、');
    var conflicts = (match.conflicts || []).join('、');
    return '<div class="cnki-candidate ' + esc(match.level || 'low') + '">'
      + '<div class="cnki-candidate-main"><div class="cnki-candidate-title">' + esc(m.title || '未识别篇名') + '</div>'
      + '<div class="cnki-candidate-detail">' + esc(detail || '联网记录') + '</div>'
      + '<div class="cnki-candidate-match"><span>' + esc(levelLabel) + (match.score != null ? ' · ' + Math.round(Number(match.score) * 100) + '%' : '') + '</span>'
      + (reasons ? '<span>' + esc(reasons) + '</span>' : '')
      + (conflicts ? '<span class="has-warning">冲突：' + esc(conflicts) + '</span>' : '') + '</div></div>'
      + '<div class="cnki-candidate-actions">'
      + '<button class="action-btn" type="button" onclick="openCnkiBatchRecord(' + i + ')">打开记录</button>'
      + '<button class="action-btn primary" type="button" onclick="resolveCnkiBatchChoice({action:\'select\',index:' + i + '})">选择这条</button>'
      + '</div></div>';
  }).join('');
  backdrop.classList.add('open');
  backdrop.setAttribute('aria-hidden', 'false');
  return new Promise(function(resolve) { cnkiBatchChoiceResolve = resolve; });
}

function openCnkiBatchRecord(i) {
  var candidate = (cnkiBatchCandidates || [])[i];
  openCnkiExternal((candidate && candidate.record_url) || cnkiBatchOpenUrl);
}

function resolveCnkiBatchChoice(choice) {
  var resolve = cnkiBatchChoiceResolve;
  cnkiBatchChoiceResolve = null;
  closeCnkiBatchModal();
  if (resolve) resolve(choice || {action:'skip'});
}

function closeCnkiBatchModal() {
  var backdrop = document.getElementById('cnki-batch-modal');
  if (!backdrop) return;
  backdrop.classList.remove('open');
  backdrop.setAttribute('aria-hidden', 'true');
}

function cnkiBatchBackdropClick(event) {
  if (event.target && event.target.id === 'cnki-batch-modal') resolveCnkiBatchChoice({action:'skip'});
}

const SCAN_IMPORT_BATCH_LIMIT = 50;
let scanEntries = [];
let scanDragSelection = null;
let suppressScanSelectionClick = false;

async function runDirectoryScan() {
  var statusEl = document.getElementById('scan-status');
  var button = document.getElementById('scan-run-btn');
  if (!scanDirectories.length) {
    statusEl.textContent = desktopShell
      ? '尚未添加文献文件夹：先点击“选择文件夹”'
      : '尚未添加文献文件夹：在上方输入框粘贴路径并回车即可';
    return;
  }
  button.disabled = true;
  statusEl.textContent = '正在扫描 ' + scanDirectories.length + ' 个目录…';
  try {
    var resp = await fetch('/api/scan-directories');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '扫描失败');
    scanEntries = data.entries || [];
    renderScanResults(data);
  } catch (e) {
    document.getElementById('scan-results-head').style.display = 'none';
    document.getElementById('scan-results').innerHTML = '';
    statusEl.textContent = '扫描失败：' + e.message;
  } finally {
    button.disabled = false;
  }
}



function renderScanResults(data) {
  var statusEl = document.getElementById('scan-status');
  var resultsEl = document.getElementById('scan-results');
  var groups = {ready: [], ocr: [], unknown: [], processing: [], imported: [], conflict: []};
  scanEntries.forEach(function(entry, index) {
    if (entry.status === 'processing') groups.processing.push(index);
    else if (entry.status === 'imported') groups.imported.push(index);
    else if (entry.status === 'name_conflict') groups.conflict.push(index);
    else if (entry.needs_ocr === true) groups.ocr.push(index);
    else if (entry.file_type === 'pdf' && entry.needs_ocr === null) groups.unknown.push(index);
    else groups.ready.push(index);
  });
  var pieces = [];
  var autoSelectable = groups.ready.concat(groups.ocr);
  var autoSelected = new Set(autoSelectable.slice(0, SCAN_IMPORT_BATCH_LIMIT));
  function section(title, indexes, checkable, checkedIndexes) {
    if (!indexes.length) return;
    pieces.push('<div class="scan-group-title">' + title + '（' + indexes.length + '）</div>');
    pieces.push(indexes.map(function(i) {
      return scanEntryRow(scanEntries[i], i, checkable, !!checkedIndexes && checkedIndexes.has(i));
    }).join(''));
  }
  section('可直接导入的新文件', groups.ready, true, autoSelected);
  section('需 OCR 的新文件', groups.ocr, true, autoSelected);
  section('未预检测的新文件', groups.unknown, true, null);
  section('正在导入', groups.processing, false, null);
  section('同名冲突', groups.conflict, false, null);
  section('已导入', groups.imported, false, null);
  resultsEl.innerHTML = pieces.join('');
  var newCount = groups.ready.length + groups.ocr.length + groups.unknown.length;
  var parts = ['新文件 ' + newCount];
  if (groups.processing.length) parts.push('正在导入 ' + groups.processing.length);
  if (groups.imported.length) parts.push('已导入 ' + groups.imported.length);
  if (groups.conflict.length) parts.push('冲突 ' + groups.conflict.length);
  document.getElementById('scan-results-summary').textContent = parts.join(' · ');
  document.getElementById('scan-results-head').style.display = 'flex';
  // Only warnings stay in the status line; the counts live in the results head.
  var warnings = [];
  var deferredNew = Math.max(0, autoSelectable.length - SCAN_IMPORT_BATCH_LIMIT);
  if (deferredNew) {
    warnings.push('每批最多导入 ' + SCAN_IMPORT_BATCH_LIMIT + ' 个，已自动勾选前 ' + SCAN_IMPORT_BATCH_LIMIT + ' 个；提交后会自动勾选剩余 ' + deferredNew + ' 个');
  }
  if (data.limit_reached) warnings.push('数量超出上限，仅显示前 ' + scanEntries.length + ' 个');
  (data.errors || []).forEach(function(err) { warnings.push(err.directory + '：' + err.error + ''); });
  statusEl.textContent = warnings.join(' ');
  updateScanImportButton();
}

function handleScanCheckChange(input) {
  var checked = document.querySelectorAll('#scan-results .scan-check:checked').length;
  if (input.checked && checked > SCAN_IMPORT_BATCH_LIMIT) {
    input.checked = false;
    showToast('每批最多导入 ' + SCAN_IMPORT_BATCH_LIMIT + ' 个；请先提交当前批次，剩余文件会保留到下一批');
  }
  updateScanImportButton();
}

/* ═══ Drag selection geometry ═══
   框选一次只能选一屏，是因为锚点和命中判定都用视口坐标、拖到边缘又不滚动。
   下面这组函数把两者都换算到滚动容器的内容坐标系，并提供边缘自动滚动，
   文献库列表和扫描结果共用同一套实现。 */




// 选框裁到滚动容器可视区，免得画到工具栏和侧边栏上。
function paintDragSelectionMarquee(state, box) {
  var scroller = state.scroller;
  var viewport = box.viewport;
  var left = Math.max(box.left - scroller.scrollLeft + viewport.left, viewport.left);
  var right = Math.min(box.right - scroller.scrollLeft + viewport.left, viewport.right);
  var top = Math.max(box.top - scroller.scrollTop + viewport.top, viewport.top);
  var bottom = Math.min(box.bottom - scroller.scrollTop + viewport.top, viewport.bottom);
  state.marquee.style.left = left + 'px';
  state.marquee.style.top = top + 'px';
  state.marquee.style.width = Math.max(0, right - left) + 'px';
  state.marquee.style.height = Math.max(0, bottom - top) + 'px';
}



// 指针压在容器上下边缘时持续滚动，否则一屏之外的条目根本够不着。
function runDragSelectionAutoScroll(state, apply) {
  if (!state.started || state.autoScrollFrame) return;
  function step() {
    if (!state.active || !state.started) {
      state.autoScrollFrame = null;
      return;
    }
    var scroller = state.scroller;
    var viewport = scroller.getBoundingClientRect();
    var delta = 0;
    if (state.pointerY < viewport.top + DRAG_SELECT_EDGE_ZONE) {
      delta = -dragSelectionEdgeSpeed(viewport.top + DRAG_SELECT_EDGE_ZONE - state.pointerY);
    } else if (state.pointerY > viewport.bottom - DRAG_SELECT_EDGE_ZONE) {
      delta = dragSelectionEdgeSpeed(state.pointerY - (viewport.bottom - DRAG_SELECT_EDGE_ZONE));
    }
    if (!delta) {
      state.autoScrollFrame = null;
      return;
    }
    var before = scroller.scrollTop;
    scroller.scrollTop = before + delta;
    if (scroller.scrollTop !== before) apply();
    state.autoScrollFrame = requestAnimationFrame(step);
  }
  state.autoScrollFrame = requestAnimationFrame(step);
}

function stopDragSelectionAutoScroll(state) {
  state.active = false;
  if (state.autoScrollFrame) cancelAnimationFrame(state.autoScrollFrame);
  state.autoScrollFrame = null;
}

// 首次越过拖动阈值：建 marquee、进入拖选态、接管指针、清掉误触的文本选区。
// 两套框选此处唯一差异是宿主容器与 marquee 类名，其余逐字相同。
function beginDragSelectionMarquee(state, container, marqueeClass, event) {
  state.started = true;
  state.marquee = document.createElement('div');
  state.marquee.className = marqueeClass;
  document.body.appendChild(state.marquee);
  container.classList.add('is-drag-selecting');
  try { container.setPointerCapture(event.pointerId); } catch (e) {}
  var selection = window.getSelection && window.getSelection();
  if (selection) selection.removeAllRanges();
}

// 收尾：退出拖选态、清掉命中高亮、移除 marquee、释放指针。
// 差异仅宿主容器与命中条目选择器（其后固定拼 .is-drag-target）。
function endDragSelectionMarquee(state, container, targetSelector, event) {
  container.classList.remove('is-drag-selecting');
  container.querySelectorAll(targetSelector + '.is-drag-target').forEach(function(el) {
    el.classList.remove('is-drag-target');
  });
  if (state.marquee) state.marquee.remove();
  try { container.releasePointerCapture(event.pointerId); } catch (e) {}
}

function libraryScrollContainer() {
  return document.querySelector('#page-library .library-list-scroll');
}

function updateLibraryDragSelection() {
  var state = libraryDragSelection;
  if (!state || !state.started) return;
  var box = dragSelectionBox(state);
  paintDragSelectionMarquee(state, box);

  var hitIds = [];
  document.querySelectorAll('#library-list .library-entry[data-delete-selectable="1"]').forEach(function(entry) {
    var hit = dragSelectionHits(entry, box, state.scroller);
    entry.classList.toggle('is-drag-target', hit);
    if (hit) hitIds.push(entry.dataset.id);
  });

  libDeleteSelection = new Set(state.initial);
  hitIds.forEach(function(sourceId) {
    if (state.targetSelected) libDeleteSelection.add(sourceId);
    else libDeleteSelection.delete(sourceId);
  });
  syncLibraryDeleteSelectionUI();
}

function setupLibraryDragSelection() {
  var list = document.getElementById('library-list');
  if (!list || list.dataset.dragSelectionReady === '1') return;
  list.dataset.dragSelectionReady = '1';

  list.addEventListener('pointerdown', function(event) {
    // Drag-marquee extends an existing selection; start it with a checkbox
    // click so ordinary browsing (click to open a doc) is never hijacked.
    if (libDeleteSelection.size === 0 || event.button !== 0 || libraryDragSelection) return;
    var entry = event.target.closest('.library-entry[data-delete-selectable="1"]');
    if (!entry || !list.contains(entry)) return;
    var scroller = libraryScrollContainer();
    if (!scroller) return;
    libraryDragSelection = Object.assign({
      pointerId: event.pointerId,
      scroller: scroller,
      pointerX: event.clientX,
      pointerY: event.clientY,
      startX: event.clientX,
      startY: event.clientY,
      targetSelected: !libDeleteSelection.has(entry.dataset.id),
      initial: new Set(libDeleteSelection),
      active: true,
      started: false,
      marquee: null,
      autoScrollFrame: null
    }, dragSelectionAnchor(scroller, event));
  });

  list.addEventListener('click', function(event) {
    if (!suppressLibrarySelectionClick) return;
    suppressLibrarySelectionClick = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);

  document.addEventListener('pointermove', function(event) {
    var state = libraryDragSelection;
    if (!state || event.pointerId !== state.pointerId) return;
    state.pointerX = event.clientX;
    state.pointerY = event.clientY;
    var distance = Math.hypot(event.clientX - state.startX, event.clientY - state.startY);
    if (!state.started && distance < 6) return;
    if (!state.started) {
      beginDragSelectionMarquee(state, list, 'library-selection-marquee', event);
    }
    event.preventDefault();
    updateLibraryDragSelection();
    runDragSelectionAutoScroll(state, updateLibraryDragSelection);
  }, {passive:false});

  function finishLibraryDragSelection(event) {
    var state = libraryDragSelection;
    if (!state || event.pointerId !== state.pointerId) return;
    stopDragSelectionAutoScroll(state);
    libraryDragSelection = null;
    endDragSelectionMarquee(state, list, '.library-entry', event);
    if (state.started) {
      suppressLibrarySelectionClick = true;
      setTimeout(function() { suppressLibrarySelectionClick = false; }, 0);
    }
    syncLibraryDeleteSelectionUI();
  }

  document.addEventListener('pointerup', finishLibraryDragSelection);
  document.addEventListener('pointercancel', finishLibraryDragSelection);
}

function scanScrollContainer() {
  return document.querySelector('#page-import .import-content');
}

function updateScanDragSelection() {
  var state = scanDragSelection;
  if (!state || !state.started) return;
  var box = dragSelectionBox(state);
  paintDragSelectionMarquee(state, box);

  var hitInputs = [];
  document.querySelectorAll('#scan-results .scan-row').forEach(function(item) {
    var input = item.querySelector('.scan-check');
    var hit = !!input && dragSelectionHits(item, box, state.scroller);
    item.classList.toggle('is-drag-target', hit);
    if (hit) hitInputs.push(input);
  });

  state.initial.forEach(function(wasChecked, item) { item.checked = wasChecked; });
  var checkedCount = Array.from(state.initial.values()).filter(Boolean).length;
  var blockedByLimit = false;
  hitInputs.forEach(function(item) {
    if (!state.targetChecked) {
      item.checked = false;
    } else if (!item.checked && checkedCount < SCAN_IMPORT_BATCH_LIMIT) {
      item.checked = true;
      checkedCount += 1;
    } else if (!item.checked) {
      blockedByLimit = true;
    }
  });
  if (blockedByLimit && !state.limitShown) {
    state.limitShown = true;
    showToast('每批最多导入 ' + SCAN_IMPORT_BATCH_LIMIT + ' 个；请先提交当前批次，剩余文件会保留到下一批');
  }
  updateScanImportButton();
}

function setupScanResultDragSelection() {
  var results = document.getElementById('scan-results');
  if (!results || results.dataset.dragSelectionReady === '1') return;
  results.dataset.dragSelectionReady = '1';

  results.addEventListener('pointerdown', function(event) {
    if (event.button !== 0 || scanDragSelection) return;
    var row = event.target.closest('.scan-row');
    var input = row && row.querySelector('.scan-check');
    if (!input) return;
    var scroller = scanScrollContainer();
    if (!scroller) return;
    var inputs = Array.from(results.querySelectorAll('.scan-check'));
    scanDragSelection = Object.assign({
      pointerId: event.pointerId,
      scroller: scroller,
      pointerX: event.clientX,
      pointerY: event.clientY,
      startX: event.clientX,
      startY: event.clientY,
      targetChecked: !input.checked,
      initial: new Map(inputs.map(function(item) { return [item, item.checked]; })),
      active: true,
      started: false,
      marquee: null,
      autoScrollFrame: null,
      limitShown: false
    }, dragSelectionAnchor(scroller, event));
  });

  results.addEventListener('click', function(event) {
    if (!suppressScanSelectionClick) return;
    suppressScanSelectionClick = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);

  document.addEventListener('pointermove', function(event) {
    var state = scanDragSelection;
    if (!state || event.pointerId !== state.pointerId) return;
    state.pointerX = event.clientX;
    state.pointerY = event.clientY;
    var distance = Math.hypot(event.clientX - state.startX, event.clientY - state.startY);
    if (!state.started && distance < 6) return;
    if (!state.started) {
      beginDragSelectionMarquee(state, results, 'scan-selection-marquee', event);
    }
    event.preventDefault();
    updateScanDragSelection();
    runDragSelectionAutoScroll(state, updateScanDragSelection);
  }, {passive: false});

  function finishScanDragSelection(event) {
    var state = scanDragSelection;
    if (!state || event.pointerId !== state.pointerId) return;
    stopDragSelectionAutoScroll(state);
    scanDragSelection = null;
    endDragSelectionMarquee(state, results, '.scan-row', event);
    if (state.started) {
      suppressScanSelectionClick = true;
      setTimeout(function() { suppressScanSelectionClick = false; }, 0);
    }
    updateScanImportButton();
  }

  document.addEventListener('pointerup', finishScanDragSelection);
  document.addEventListener('pointercancel', finishScanDragSelection);
}

function updateScanImportButton() {
  var button = document.getElementById('scan-import-btn');
  var checked = document.querySelectorAll('#scan-results .scan-check:checked').length;
  button.style.display = checked ? 'inline-flex' : 'none';
  button.textContent = '导入所选 (' + checked + ')';
}

async function importSelectedScanned() {
  var checks = Array.from(document.querySelectorAll('#scan-results .scan-check:checked'));
  if (!checks.length) return;
  if (checks.length > SCAN_IMPORT_BATCH_LIMIT) {
    showToast('每批最多导入 ' + SCAN_IMPORT_BATCH_LIMIT + ' 个文件');
    return;
  }
  var paths = checks.map(function(box) { return scanEntries[Number(box.dataset.index)].path; });
  var button = document.getElementById('scan-import-btn');
  button.disabled = true;
  try {
    var resp = await fetch('/api/import-local', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        paths: paths,
        pdf_parse_mode: selectedPdfParseMode(),
        vision_provider_id: selectedVisionProviderId()
      })
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
    (data.jobs || []).forEach(function(job, i) {
      var q = {
        id: 'scan-' + job.job_id,
        name: job.file_name,
        size: job.size_bytes,
        type: job.file_type,
        status: 'processing',
        step: 0,
        jobId: job.job_id,
        providerId: job.provider_id || null,
        providerName: ((visionConfig.providers || []).find(function(item) { return item.id === job.provider_id; }) || {}).name || null,
        message: '文件已复制，正在处理…'
      };
      if (job.file_type === 'pdf' && job.detected_pdf_type) {
        q.detectedType = job.detected_pdf_type;
        q.route = job.parse_route || (job.detected_pdf_type === 'native_text' ? 'native' : 'mineru');
        q.step = 2;
        q.message = '检测结果：' + pdfTypeLabel(job.detected_pdf_type)
          + (q.route === 'vision' ? '，将使用其他视觉 API'
            : q.route === 'mineru' ? '，将使用 MinerU 在线解析' : '，使用本地快速解析');
      } else if (job.file_type !== 'pdf') {
        q.step = 1;
      }
      importQueue.push(q);
      pollImportJob(q.id);
    });
    renderImportQueue();
    var failed = (data.errors || []);
    var failureNote = failed.length
      ? '；' + failed.length + ' 个未导入：' + (failed[0].error || '请检查文件')
      : '';
    failed.forEach(function(err) { console.warn('import-local failed:', err.path, err.error); });
    var submittedPaths = new Set((data.jobs || []).map(function(job) { return job.path; }));
    scanEntries.forEach(function(entry) {
      if (submittedPaths.has(entry.path)) entry.status = 'processing';
    });
    renderScanResults({errors: [], limit_reached: false});
    var nextBatchCount = document.querySelectorAll('#scan-results .scan-check:checked').length;
    var nextBatchNote = nextBatchCount
      ? '；下一批 ' + nextBatchCount + ' 个已自动勾选，可继续导入'
      : '';
    showToast('已开始导入 ' + (data.jobs || []).length + ' 个文件' + failureNote + nextBatchNote);
  } catch (e) {
    showToast('批量导入失败：' + e.message);
  } finally {
    button.disabled = false;
  }
}

function selectedPdfParseMode() {
  var selected = document.querySelector('input[name="pdf-parse-mode"]:checked');
  return selected && ['auto','mineru','vision'].indexOf(selected.value) >= 0 ? selected.value : 'auto';
}

function selectedVisionProviderId() {
  var container = document.getElementById('import-vision-provider');
  return selectedPdfParseMode() === 'vision' && container ? (container.dataset.value || '') : '';
}

function handleFileSelect(files) {
  if (!files || files.length === 0) return;
  var validExts = ['.pdf', '.docx'];
  var pdfParseMode = selectedPdfParseMode();
  var selectedProviderId = selectedVisionProviderId();
  var selectedProvider = (visionConfig.providers || []).find(function(item) { return item.id === selectedProviderId; });
  for (var i = 0; i < files.length; i++) {
    var file = files[i];
    var ext = file.name.toLowerCase().slice(file.name.lastIndexOf('.'));
    if (validExts.indexOf(ext) === -1) {
      showToast('不支持的格式: ' + file.name);
      continue;
    }
    var id = 'import-' + Date.now() + '-' + i;
    importQueue.push({
      id: id,
      file: file,
      name: file.name,
      size: file.size,
      type: ext === '.pdf' ? 'pdf' : 'docx',
      parseMode: ext === '.pdf' ? pdfParseMode : null,
      providerId: ext === '.pdf' && pdfParseMode === 'vision' ? selectedProviderId : null,
      providerName: ext === '.pdf' && pdfParseMode === 'vision' && selectedProvider ? selectedProvider.name : null,
      status: 'queued',
      step: 0,
      message: '等待处理'
    });
  }
  document.getElementById('file-input').value = '';
  renderImportQueue();
  importQueue.filter(function(q) { return q.status === 'queued'; }).forEach(function(q) {
    uploadImport(q.id);
  });
}

function renderImportQueue() {
  var queueEl = document.getElementById('import-queue');
  var itemsEl = document.getElementById('import-items');
  if (importQueue.length === 0) {
    queueEl.style.display = 'none';
    return;
  }
  queueEl.style.display = 'block';
  itemsEl.innerHTML = importQueue.map(function(q) {
    var typeCls = q.type === 'pdf' ? 'pdf' : 'word';
    var retryProvider = visionRetryProviderFor(q);
    var steps = importStepsFor(q);
    var stepsHTML = steps.map(function(label, i) {
      var cls = '';
      if (q.status === 'error' && i === q.step) cls = 'error';
      else if (i < q.step) cls = 'done';
      else if (i === q.step && q.status === 'processing') cls = 'active';
      return '<div class="import-step ' + cls + '">'
        + '<div class="import-step-bar ' + cls + '"></div>'
        + '<span class="import-step-label">' + label + '</span>'
        + '</div>';
    }).join('');
    var statusCls = q.status === 'error' ? ' error' : q.status === 'done' ? ' done' : q.status === 'paused' ? ' paused' : '';
    var retryHTML = '';
    if ((q.status === 'paused' || q.status === 'error') && q.canResume) {
      retryHTML = '<div class="import-item-retry"><button class="action-btn primary" type="button" onclick="resumeImport(\''
        + q.id + '\')">' + (q.failureStage === 'index' ? '重新建立索引' : '继续导入') + '</button>';
      if (q.status === 'error' && retryProvider) {
        retryHTML += '<button class="action-btn" type="button" onclick="retryImportWithVision(\''
          + q.id + '\')">改用 ' + esc(retryProvider.name || '其他解析 API') + '</button>';
      }
      retryHTML += '<button class="action-btn" type="button" onclick="openVisionSettings()">解析设置</button></div>';
    } else if (q.status === 'error' && retryProvider) {
      retryHTML = '<div class="import-item-retry"><button class="action-btn primary" type="button" onclick="retryImportWithVision(\''
        + q.id + '\')">改用 ' + esc(retryProvider.name || '其他解析 API') + '</button>'
        + '<button class="action-btn" type="button" onclick="openVisionSettings()">切换设置</button></div>';
    } else if (q.status === 'error'
        && (q.canRetryVision || q.needsProviderConfig || q.mineruFailed)) {
      retryHTML = '<div class="import-item-retry"><button class="action-btn" type="button" onclick="openVisionSettings()">配置其他解析 API</button></div>';
    }
    return '<div class="import-item" data-id="' + q.id + '">'
      + '<div class="import-item-header">'
      + '<span class="type-badge ' + typeCls + '">' + (q.type === 'pdf' ? 'PDF' : 'DOCX') + '</span>'
      + '<span class="import-item-name">' + esc(q.name) + '</span>'
      + importRouteBadge(q)
      + '<span class="import-item-size">' + formatFileSize(q.size) + '</span>'
      + '<button class="import-item-remove" onclick="removeImport(\'' + q.id + '\')" title="移除">&times;</button>'
      + '</div>'
      + '<div class="import-steps">' + stepsHTML + '</div>'
      + '<div class="import-item-status' + statusCls + '">' + esc(q.message) + '</div>'
      + retryHTML
      + '</div>';
  }).join('');
  syncResumeAllButton();
}

// 队列里可继续的任务多于一个时，才值得给一个「全部继续导入」的批量入口。
function resumableImportQueue() {
  return importQueue.filter(function(item) {
    return item.jobId && item.canResume
      && (item.status === 'paused' || item.status === 'error');
  });
}

function syncResumeAllButton() {
  var resumeButton = document.getElementById('import-resume-all-btn');
  var cancelButton = document.getElementById('import-cancel-all-btn');
  if (!resumeButton && !cancelButton) return;
  var count = resumableImportQueue().length;
  if (resumeButton) {
    resumeButton.style.display = count > 1 ? 'inline-flex' : 'none';
    resumeButton.textContent = '全部继续导入（' + count + '）';
  }
  if (cancelButton) {
    cancelButton.style.display = count > 1 ? 'inline-flex' : 'none';
    cancelButton.textContent = '全部取消（' + count + '）';
  }
}

async function resumeAllImports() {
  var pending = resumableImportQueue();
  if (!pending.length) return;
  var button = document.getElementById('import-resume-all-btn');
  // 只要有一个联网解析任务，就用一次汇总确认代替逐个弹窗。
  var hasPaidRoute = pending.some(function(item) {
    return item.failureStage !== 'index' && item.type === 'pdf' && item.route !== 'native';
  });
  if (hasPaidRoute && !await showAppConfirm(
    '将从上次断点继续 ' + pending.length + ' 个任务，其中包含需要联网解析的文献，未完成部分可能产生费用',
    {title:'继续联网解析任务？', confirmText:'继续任务', tone:'warning'}
  )) return;
  if (button) { button.disabled = true; button.textContent = '正在继续…'; }
  for (var index = 0; index < pending.length; index += 1) {
    // 串行发起，避免同时唤起多个解析任务把本地或额度打满。
    await resumeImport(pending[index].id, {silent: true, skipConfirm: true});
  }
  if (button) button.disabled = false;
  syncResumeAllButton();
}

async function cancelAllImports() {
  var pending = resumableImportQueue();
  if (!pending.length) return;
  if (!await showAppConfirm(
    '将取消 ' + pending.length + ' 个中断任务。原始文件不会被删除',
    {title:'全部取消中断任务？', confirmText:'全部取消', tone:'danger'}
  )) return;
  var resumeButton = document.getElementById('import-resume-all-btn');
  var cancelButton = document.getElementById('import-cancel-all-btn');
  if (resumeButton) resumeButton.disabled = true;
  if (cancelButton) { cancelButton.disabled = true; cancelButton.textContent = '正在取消…'; }
  var failedCount = 0;
  for (var index = 0; index < pending.length; index += 1) {
    // 复用每条右上角 × 的持久化移除逻辑，串行处理以免并发改写任务日志。
    if (!await removeImport(pending[index].id, {silent: true, deferRender: true})) {
      failedCount += 1;
    }
  }
  renderImportQueue();
  if (failedCount) {
    showToast('有 ' + failedCount + ' 个中断任务取消失败，请重试', 'warning');
  } else {
    showToast('已取消 ' + pending.length + ' 个中断任务', 'success');
  }
}

async function removeImport(id, options) {
  options = options || {};
  var q = importQueue.find(function(item) { return item.id === id; });
  if (q && q.uploadId) {
    var activeUploadId = q.uploadId;
    q.uploadId = null;
    try {
      await fetch('/api/import-upload/cancel', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({upload_id: activeUploadId})
      });
    } catch (cancelError) {
      console.warn('cancel chunked upload failed:', cancelError);
    }
  }
  if (q && q.jobId && (q.status === 'paused' || q.status === 'error')) {
    try {
      var resp = await fetch('/api/import-resume-dismiss', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({job_id: q.jobId})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '移除任务失败');
    } catch (e) {
      if (!options.silent) showToast('移除导入任务失败：' + e.message);
      return false;
    }
  }
  importQueue = importQueue.filter(function(item) { return item.id !== id; });
  if (!options.deferRender) renderImportQueue();
  return true;
}

var IMPORT_UPLOAD_FALLBACK_CHUNK_BYTES = 4 * 1024 * 1024;

async function uploadImport(id) {
  var q = importQueue.find(function(q) { return q.id === id; });
  if (!q) return;
  q.status = 'processing';
  q.step = 0;
  q.message = '正在读取文件…';
  renderImportQueue();
  var uploadId = null;
  try {
    var startResp = await fetch('/api/import-upload/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        file_name: q.name,
        size: q.file.size,
        parse_mode: q.parseMode || 'auto',
        provider_id: q.providerId || ''
      })
    });
    var startData = await startResp.json();
    if (!startResp.ok || startData.error) throw new Error(startData.error || '无法开始读取文件');
    uploadId = startData.upload_id;
    if (!uploadId) throw new Error('上传任务编号缺失');
    q.uploadId = uploadId;
    var totalSize = Number(q.file.size || 0);
    var chunkSize = Number(startData.chunk_size || IMPORT_UPLOAD_FALLBACK_CHUNK_BYTES);
    if (!Number.isFinite(chunkSize) || chunkSize <= 0) chunkSize = IMPORT_UPLOAD_FALLBACK_CHUNK_BYTES;
    chunkSize = Math.min(chunkSize, 8 * 1024 * 1024);
    var offset = 0;
    while (offset < totalSize) {
      var end = Math.min(offset + chunkSize, totalSize);
      var chunkResp = await fetch('/api/import-upload/chunk', {
        method: 'POST',
        headers: {
          'Content-Type': q.file.type || 'application/octet-stream',
          'X-Upload-ID': uploadId,
          'X-Upload-Offset': String(offset)
        },
        body: q.file.slice(offset, end)
      });
      var chunkData = await chunkResp.json();
      if (!chunkResp.ok || chunkData.error) throw new Error(chunkData.error || '读取文件失败');
      var received = Number(chunkData.received_size);
      if (!Number.isFinite(received) || received !== end) {
        throw new Error('上传分块位置校验失败');
      }
      offset = received;
      q.uploadProgress = totalSize ? Math.round(offset * 100 / totalSize) : 0;
      q.message = '正在读取文件… ' + q.uploadProgress + '%';
      renderImportQueue();
    }
    var resp = await fetch('/api/import-upload/finish', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({upload_id: uploadId})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
    uploadId = null;
    q.uploadId = null;
    q.jobId = data.job_id;
    q.providerId = data.provider_id || q.providerId;
    if (q.type === 'pdf' && data.detected_pdf_type) {
      q.detectedType = data.detected_pdf_type;
      q.route = data.parse_route || (data.detected_pdf_type === 'native_text' ? 'native' : 'mineru');
      q.step = 2;
      q.message = '检测结果：' + pdfTypeLabel(data.detected_pdf_type)
        + (q.route === 'vision' ? '，将使用其他视觉 API'
          : q.route === 'mineru' ? '，将使用 MinerU 在线解析' : '，使用本地快速解析');
    } else {
      q.step = 1;
      q.message = '文件已保存，正在建立索引…';
    }
    renderImportQueue();
    if (q.jobId) pollImportJob(q.id);
  } catch (e) {
    if (uploadId) {
      try {
        await fetch('/api/import-upload/cancel', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({upload_id: uploadId})
        });
      } catch (cancelError) {
        console.warn('cancel chunked upload failed:', cancelError);
      }
    }
    q.uploadId = null;
    q.status = 'error';
    q.message = e.message || '导入失败';
    renderImportQueue();
  }
}

function pollImportJob(id) {
  var q = importQueue.find(function(item) { return item.id === id; });
  if (!q || !q.jobId) return;
  fetch('/api/import-status?job_id=' + encodeURIComponent(q.jobId))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data.error) throw new Error(data.error);
      if (data.parse_route) q.route = data.parse_route;
      if (data.provider_id) q.providerId = data.provider_id;
      if (data.provider_name) q.providerName = data.provider_name;
      q.mineruFailed = !!data.mineru_failed;
      q.mineruInterrupted = !!data.mineru_interrupted;
      if (data.phase === 'mineru_submitting' || data.phase === 'mineru_processing') q.route = 'mineru';
      else if (data.phase === 'vision_processing') q.route = 'vision';
      else if (data.phase === 'text_parsing' && q.type === 'pdf') q.route = 'native';
      var steps = importStepsFor(q);
      if (data.phase === 'mineru_submitting' || data.phase === 'mineru_processing') q.step = steps.indexOf('MinerU 解析');
      else if (data.phase === 'vision_processing') q.step = 2;
      else if (data.phase === 'text_parsing') q.step = q.type === 'pdf' ? steps.indexOf('本地解析') : steps.indexOf('文本入库');
      else if (data.phase === 'rebuilding_index' || data.phase === 'metadata_recognition') q.step = steps.indexOf('建立索引');
      else if (data.status === 'completed') q.step = steps.length;
      q.message = data.message || q.message;
      if (data.status === 'completed') {
        q.status = 'done';
        q.message = data.message || '导入完成，已自动更新索引';
        invalidateLibraryCatalog();
        ensureSearchDocuments(true).then(updateSearchDocumentLabel);
      } else if (data.status === 'failed') {
        q.status = 'error';
        q.message = data.message || '导入失败';
        q.failureStage = data.failure_stage || null;
        q.canResume = !!data.can_resume;
        q.canRetryVision = !!data.can_retry_with_provider;
        q.retryProviderId = data.retry_provider_id || q.providerId || null;
        q.retryProviderName = data.retry_provider_name || q.providerName || null;
        q.needsProviderConfig = !!data.needs_provider_config;
      } else if (data.status === 'paused') {
        q.status = 'paused';
        q.canResume = !!data.can_resume;
        q.message = data.message || '上次导入已暂停，可继续导入';
      }
      renderImportQueue();
      if (q.status === 'processing') setTimeout(function() { pollImportJob(id); }, 2500);
    })
    .catch(function(err) {
      q.status = 'error';
      q.message = err.message || '读取导入状态失败';
      renderImportQueue();
    });
}

async function loadResumableImports() {
  try {
    var resp = await fetch('/api/import-resumable');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '读取恢复任务失败');
    (data.jobs || []).forEach(function(job) {
      if (importQueue.some(function(item) { return item.jobId === job.job_id; })) return;
      var isPaused = job.status === 'paused';
      importQueue.push({
        id: 'resume-' + job.job_id,
        jobId: job.job_id,
        name: job.file_name || '未命名文献',
        size: Number(job.size_bytes || 0),
        type: job.file_type === 'pdf' ? 'pdf' : 'docx',
        status: isPaused ? 'paused' : 'error',
        step: job.file_type === 'pdf' ? 2 : 1,
        route: job.parse_route || null,
        providerId: job.provider_id || null,
        providerName: job.provider_name || null,
        detectedType: job.detected_pdf_type || null,
        message: job.message || (isPaused ? '上次导入已暂停，可继续导入' : '上次导入未完成'),
        failureStage: job.failure_stage || null,
        canResume: !!job.can_resume,
        canRetryVision: !!job.can_retry_with_provider,
        retryProviderId: job.retry_provider_id || job.provider_id || null,
        retryProviderName: job.retry_provider_name || job.provider_name || null,
        needsProviderConfig: !!job.needs_provider_config,
        mineruFailed: !!job.mineru_failed,
        mineruInterrupted: !!job.mineru_interrupted,
        fromJournal: true
      });
    });
    renderImportQueue();
  } catch (e) {
    console.warn('load resumable imports failed:', e);
  }
}

async function resumeImport(id, options) {
  options = options || {};
  var q = importQueue.find(function(item) { return item.id === id; });
  if (!q || !q.jobId || !q.canResume) return;
  var serviceName = q.route === 'mineru' ? 'MinerU' : (q.providerName || '视觉解析 API');
  if (!options.skipConfirm && q.failureStage !== 'index' && q.type === 'pdf' && q.route !== 'native'
      && !await showAppConfirm(
        '将从上次断点继续调用 ' + serviceName + '，未完成部分可能产生费用',
        {title:'继续联网解析？', confirmText:'继续任务', tone:'warning'}
      )) return;
  try {
    var resp = await fetch('/api/import-resume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({job_id: q.jobId})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '继续任务失败');
    q.status = 'processing';
    q.canResume = false;
    q.message = q.failureStage === 'index'
      ? '正在重新建立索引，不会再次调用解析 API…'
      : '正在继续导入…';
    renderImportQueue();
    pollImportJob(q.id);
  } catch (e) {
    showToast((options.silent ? esc(q.name) + '：' : '') + '继续导入失败：' + e.message);
  }
}

async function retryImportWithVision(id) {
  var q = importQueue.find(function(item) { return item.id === id; });
  if (!q || !q.jobId) return;
  var provider = visionRetryProviderFor(q);
  if (!provider) {
    openVisionSettings();
    showToast('请先配置一个其他解析 API');
    return;
  }
  var providerId = provider.id;
  var providerName = provider.name || '其他解析 API';
  if (!await showAppConfirm(
    '将改用“' + providerName + '”重新解析这份 PDF，可能产生费用',
    {title:'切换解析接口？', confirmText:'切换并重试', tone:'warning'}
  )) return;
  try {
    var resp = await fetch('/api/import-retry', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({job_id: q.jobId, provider_id: providerId})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '重试失败');
    q.jobId = data.job_id;
    q.status = 'processing';
    q.route = 'vision';
    q.providerId = data.provider_id;
    q.providerName = data.provider_name || providerName;
    q.step = 2;
    q.message = '正在切换到 ' + q.providerName + '…';
    q.canRetryVision = false;
    q.needsProviderConfig = false;
    renderImportQueue();
    pollImportJob(q.id);
  } catch (e) {
    showToast('切换解析接口失败：' + e.message);
  }
}

