/* ═══ Calibration ═══ */
async function refreshCalibrationSource(sourceId) {
  var data;
  try {
    data = await fetchLibraryCatalog(true);
  } catch (error) {
    throw new Error(error && error.message ? error.message : '刷新文献状态失败');
  }
  applyLibraryCatalog(data);
  await ensureLibraryDetail(sourceId);
  updateLibraryEntry(sourceId);
  if (calSelectedSourceId === sourceId) await loadCalibrationDoc(sourceId);
}

function statusStatIcon(icon) {
  var paths = {
    document:'<path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4"/>',
    book:'<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11v16H6.5A2.5 2.5 0 0 0 4 21.5z"/><path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v16h4.5a2.5 2.5 0 0 1 2.5 2.5z"/>',
    check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    notice:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    danger:'<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>'
  };
  return '<span class="status-stat__icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (paths[icon] || paths.notice) + '</svg></span>';
}

function statusChipIcon(group) {
  var icons = {
    calibrated:'<circle cx="12" cy="12" r="9"/><path d="m8 12 2.6 2.6L16.5 9"/>',
    pending:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    review:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    failed:'<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/>',
    mapping:'<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>'
  };
  var spinning = group === 'mapping' ? ' is-spinning' : '';
  return '<span class="status-chip__icon' + spinning + '" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (icons[group] || icons.pending) + '</svg></span>';
}

function statusStatButton(status, label, value, variant, icon, activeFilter, handlerName) {
  return '<button type="button" data-status="' + status + '" class="status-stat status-stat--' + variant + (activeFilter === status ? ' active' : '') + '" onclick="' + handlerName + '(\'' + status + '\')">'
    + statusStatIcon(icon)
    + '<span class="status-stat__label">' + label + '</span>'
    + '<span class="status-stat__count">' + value + '</span></button>';
}

const calPinyinCollator = new Intl.Collator('zh-CN-u-co-pinyin', {sensitivity:'base', numeric:true});
const calLatinCollator = new Intl.Collator('en', {sensitivity:'base', numeric:true});

function calibrationSortText(a, b, direction) {
  a = String(a || '').trim(); b = String(b || '').trim();
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  var ag = /^[\u3400-\u9fff]/.test(a) ? 0 : (/^[A-Za-z]/.test(a) ? 1 : 2);
  var bg = /^[\u3400-\u9fff]/.test(b) ? 0 : (/^[A-Za-z]/.test(b) ? 1 : 2);
  if (ag !== bg) return ag - bg;
  var value = (ag === 0 ? calPinyinCollator : calLatinCollator).compare(a, b);
  return direction === 'desc' ? -value : value;
}

async function loadCalibrationDoc(sourceId) {
  sourceId = sourceId || calSelectedSourceId;
  var editor = document.getElementById('cal-editor');
  if (!sourceId) {
    editor.style.display = 'none';
    calSelectedDoc = null;
    calSegments = [];
    calAutoResult = null;
    document.getElementById('cal-auto-preview').style.display = 'none';
    return;
  }
  try {
    var resp = await fetch('/api/calibration?source_id=' + encodeURIComponent(sourceId));
    calSelectedDoc = await resp.json();
    if (calSelectedDoc.error) {
      showToast('文献未找到');
      return;
    }
    var mapping = calSelectedDoc.page_mapping || {};
    calSegments = (mapping.segments || []).map(function(s) { return Object.assign({}, s); });
    document.getElementById('cal-detail-actions').innerHTML = '<button class="action-btn primary" id="cal-auto-detect-btn" onclick="runAutoDetection()">自动检测页码</button><button class="action-btn" onclick="scrollToManualMapping()">手动设置</button><button class="action-btn" onclick="showCalibrationEvidence()">查看识别依据</button>'
      + '<span class="detail-pill" style="margin-left:auto">' + (mapping.validated_by ? '已验证' : '未验证') + '</span>';
    editor.style.display = 'block';
    calAutoResult = null;
    document.getElementById('cal-auto-preview').style.display = 'none';
    renderCalSegments();
    updateCalPreview();
  } catch(e) {
    showToast('加载校准数据失败', 'danger');
  }
}

async function runAutoDetection(sourceId) {
  sourceId = sourceId || calSelectedSourceId;
  if (!sourceId) {
    showToast('请先选择一本文献');
    return;
  }
  calTransientStatus[sourceId] = 'mapping';
  updateLibraryEntry(sourceId);
  var panel = document.getElementById('cal-auto-preview');
  var button = document.getElementById('cal-auto-detect-btn');
  panel.style.display = 'block';
  panel.innerHTML = '<div class="auto-detect-title">正在检测页码与页面布局…</div><div class="auto-detect-note">正在读取页面尺寸、左右内容分布、中缝、页码位置、PDF 标签、数字书签和现有 MinerU 结果</div>';
  if (button) button.disabled = true;
  try {
    var resp = await fetch('/api/auto-page-mapping/detect', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({source_id:sourceId, dry_run:true})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '页码自动检测失败');
    calAutoResult = data.result;
    renderAutoDetectionResult(calAutoResult);
    var current = libSources.find(function(item) { return item.source_file_id === sourceId; });
    var segments = (calAutoResult.selected_segments || []).filter(function(item) { return item && item.confidence_level !== 'low'; });
    calTransientStatus[sourceId] = current && current.status === 'manual_mapped' ? 'manual_mapped' : (segments.length ? 'needs_review' : 'auto_mapping_failed');
  } catch(e) {
    calAutoResult = null;
    calTransientStatus[sourceId] = 'auto_mapping_failed';
    panel.innerHTML = '<div class="auto-detect-title">页码自动检测失败</div><div class="auto-detect-note">' + esc(e.message) + '</div>';
  } finally {
    if (button) button.disabled = false;
    updateLibraryEntry(sourceId);
  }
}

function renderAutoDetectionResult(result) {
  var panel = document.getElementById('cal-auto-preview');
  var segments = (result.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; });
  var html = '<div class="auto-detect-title">检测完成</div>';
  var layout = result.layout_detection || {};
  if (layout.layout_mode === 'spread') {
    var layoutEvidence = layout.evidence || {};
    html += '<div class="auto-detect-note auto-detect-layout">页面布局：双开页 · '
      + (layout.reading_direction === 'rtl' ? '右→左' : '左→右')
      + ' · 中缝 ' + Math.round(Number(layout.gutter_x || 0.5) * 100) + '%'
      + ' · ' + mappingConfidenceLabel(layout.confidence_level, layout.confidence)
      + '</div>';
    html += '<div class="auto-detect-note">布局依据：' + Number(layoutEvidence.split_pages || 0) + ' 个双栏页面；'
      + Number(layoutEvidence.paired_page_numbers || 0) + ' 页检测到成对页码；双页序列支持 '
      + Number(layoutEvidence.stride_two_support || 0) + ' 页</div>';
  } else if (layout.layout_mode === 'single') {
    html += '<div class="auto-detect-note">页面布局：单页 · ' + mappingConfidenceLabel(layout.confidence_level, layout.confidence) + '</div>';
  }
  if (result.manual_mapping_present) {
    html += '<div class="auto-detect-note auto-detect-warning">当前文献已有人工页码映射。以下结果仅为预览，不会自动覆盖</div>';
  }
  if (!segments.length) {
    html += '<div class="auto-detect-note">未能自动识别可靠页码区间</div>';
    html += '<div class="auto-detect-note">' + autoFailureReasons(result.failure_reasons || []) + '</div>';
    html += '<div class="auto-detect-actions"><button class="action-btn" onclick="cancelAutoDetection()">关闭</button></div>';
    panel.innerHTML = html;
    return;
  }
  html += '<div class="auto-detect-note">识别到 ' + segments.length + ' 个页码区间，当前仍是预览状态</div>';
  html += '<div class="auto-segment-list">' + segments.map(function(seg, index) {
    var evidence = seg.mapping_evidence || {};
    return '<div class="auto-segment-row"><div class="auto-segment-main">' + (index + 1) + '. ' + esc(autoMappingSegmentText(seg)) + '</div>'
      + '<div class="auto-segment-evidence">依据：' + esc(mappingMethodLabel(seg.method))
      + (evidence.inferred_offset != null ? '；稳定 offset = ' + evidence.inferred_offset : '')
      + (evidence.observed_page_numbers != null ? '；观察到 ' + evidence.observed_page_numbers + ' 个候选' : '')
      + (evidence.sequence_consistency != null ? '；序列一致性 ' + Math.round(Number(evidence.sequence_consistency) * 100) + '%' : '')
      + '</div></div>';
  }).join('') + '</div>';
  html += '<details style="margin-top:10px"><summary class="auto-detect-note">查看检测依据</summary><div class="auto-detect-note" style="margin-top:6px">'
    + 'PDF 标签 ' + Number((result.evidence_counts || {}).pdf_page_labels || 0) + ' 个；数字书签 ' + Number((result.evidence_counts || {}).numeric_bookmarks || 0)
    + ' 个；MinerU 候选 ' + Number((result.evidence_counts || {}).mineru_candidates || 0) + ' 个；页边候选 ' + Number((result.evidence_counts || {}).native_edge_candidates || 0) + ' 个</div></details>';
  html += '<div class="auto-detect-actions">'
    + '<button class="action-btn primary" onclick="applyAutoDetection()">' + (result.manual_mapping_present ? '用自动结果替换人工映射' : '应用自动映射') + '</button>'
    + '<button class="action-btn" onclick="editAutoDetectionResult()">编辑后应用</button>'
    + '<button class="action-btn" onclick="cancelAutoDetection()">取消</button></div>';
  panel.innerHTML = html;
}

function autoFailureReasons(reasons) {
  var labels = {no_page_labels:'没有 PDF Page Labels',no_bookmarks:'没有数字书签',no_mineru_candidates:'现有 MinerU 结果没有可靠页码候选',no_edge_candidates:'页边区域未发现页码候选',sequence_not_found:'未找到稳定递增页码序列',spread_sequence_not_found:'识别到双开布局，但未找到可靠的双页页码序列',source_missing:'原始 PDF 文件不存在'};
  return reasons.map(function(reason) { return '• ' + (labels[reason] || reason); }).join('<br>');
}

async function applyAutoDetection() {
  if (!calAutoResult) return;
  var sourceId = calSelectedSourceId;
  var segments = (calAutoResult.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; });
  if (!segments.length) return;
  var replaceManual = false;
  if (calAutoResult.manual_mapping_present) {
    replaceManual = await showAppConfirm(
      '当前文献已有人工映射。本次自动检测结果会替换已有映射',
      {title:'替换人工页码映射？', confirmText:'确认替换', tone:'warning'}
    );
    if (!replaceManual) return;
  }
  try {
    showToast('正在应用自动映射…');
    var resp = await fetch('/api/auto-page-mapping/apply', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source_id:sourceId,segments:segments,auto_mapping:calAutoResult,replace_manual:replaceManual})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '应用失败');
    showToast('自动页码映射已生效', 'success');
    delete calTransientStatus[sourceId];
    await loadMeta();
    await refreshCalibrationSource(sourceId);
  } catch(e) {
    showToast('应用失败：' + e.message, 'danger');
  }
}

function editAutoDetectionResult() {
  if (!calAutoResult) return;
  calSegments = (calAutoResult.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; }).map(function(s) {
    return Object.assign({}, s, {confidence:s.mapping_confidence || s.confidence});
  });
  renderCalSegments();
  updateCalPreview();
  document.getElementById('cal-auto-preview').style.display = 'none';
  showToast('自动结果已载入手动编辑区');
}

function cancelAutoDetection() {
  calAutoResult = null;
  document.getElementById('cal-auto-preview').style.display = 'none';
}

function segmentNumberStyleControl(style, index) {
  var values = ['arabic','roman_lower','roman_upper','none'];
  return '<div class="app-select segment-style-select" id="segment-style-select-' + index + '">'
    + '<button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,\'segment-style-select-' + index + '\')"><span class="app-select-value">' + segmentNumberStyleLabel(style) + '</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>'
    + '<div class="app-select-menu" role="listbox">' + values.map(function(value) {
      return '<button class="app-select-option' + (style === value ? ' is-selected' : '') + '" type="button" data-value="' + value + '" onclick="setSegmentNumberStyle(event,' + index + ',\'' + value + '\')">' + segmentNumberStyleLabel(value) + '</button>';
    }).join('') + '</div></div>';
}

function setSegmentNumberStyle(event, index, value) {
  event.stopPropagation();
  updateCalSeg(index, 'number_style', value);
  closeAppSelects();
  renderCalSegments();
}

function segmentLayoutControl(layout, index) {
  var values = ['single','spread'];
  return '<div class="app-select segment-layout-select" id="segment-layout-select-' + index + '">'
    + '<button class="app-select-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" onclick="toggleAppSelect(event,\'segment-layout-select-' + index + '\')"><span class="app-select-value">' + segmentLayoutLabel(layout) + '</span><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></button>'
    + '<div class="app-select-menu" role="listbox">' + values.map(function(value) {
      return '<button class="app-select-option' + (layout === value ? ' is-selected' : '') + '" type="button" data-value="' + value + '" onclick="setSegmentLayout(event,' + index + ',\'' + value + '\')">' + segmentLayoutLabel(value) + '</button>';
    }).join('') + '</div></div>';
}

function setSegmentLayout(event, index, value) {
  event.stopPropagation();
  updateCalSeg(index, 'layout_mode', value);
  closeAppSelects();
  renderCalSegments();
}

function setSegmentReadingDirection(index, value) {
  var seg = calSegments[index];
  if (!seg) return;
  seg.reading_direction = value === 'rtl' ? 'rtl' : 'ltr';
  updateSpreadPanel(index);
  updateCalPreview();
}

function updateSegmentGutter(index, value) {
  var seg = calSegments[index];
  if (!seg) return;
  var percent = Number(value);
  if (!isFinite(percent)) percent = 50;
  percent = Math.max(30, Math.min(70, percent));
  seg.gutter_x = percent / 100;
  updateSpreadPanel(index);
  updateCalPreview();
}





function updateSpreadPanel(index) {
  var seg = calSegments[index];
  if (!seg) return;
  var diagram = document.getElementById('spread-diagram-' + index);
  if (!diagram) { renderCalSegments(); return; }
  var direction = seg.reading_direction === 'rtl' ? 'rtl' : 'ltr';
  var gp = spreadGutterPercent(seg);
  var pair = spreadCitationPair(seg);
  var leftFirst = direction !== 'rtl';
  document.getElementById('spread-gutter-line-' + index).style.left = gp + '%';
  document.getElementById('spread-half-left-' + index).style.width = gp + '%';
  document.getElementById('spread-half-right-' + index).style.width = (100 - gp) + '%';
  document.getElementById('spread-gutter-out-' + index).textContent = gp + '%';
  document.getElementById('spread-badge-left-' + index).textContent = leftFirst ? '1' : '2';
  document.getElementById('spread-badge-right-' + index).textContent = leftFirst ? '2' : '1';
  document.getElementById('spread-page-left-' + index).textContent = pair.mapped ? '引文 ' + pair.left + ' 页' : '不映射';
  document.getElementById('spread-page-right-' + index).textContent = pair.mapped ? '引文 ' + pair.right + ' 页' : '不映射';
  document.getElementById('spread-summary-' + index).innerHTML = spreadSummaryHtml(seg);
  var ltrBtn = diagram.parentNode.querySelector('.segment-direction-btn[onclick*="\'ltr\'"]');
  var rtlBtn = diagram.parentNode.querySelector('.segment-direction-btn[onclick*="\'rtl\'"]');
  if (ltrBtn && rtlBtn) {
    ltrBtn.classList.toggle('is-active', direction === 'ltr');
    ltrBtn.setAttribute('aria-pressed', direction === 'ltr' ? 'true' : 'false');
    rtlBtn.classList.toggle('is-active', direction === 'rtl');
    rtlBtn.setAttribute('aria-pressed', direction === 'rtl' ? 'true' : 'false');
  }
}

function renderCalSegments() {
  var body = document.getElementById('cal-segments-body');
  var noSeg = document.getElementById('cal-no-segments');
  if (calSegments.length === 0) {
    body.innerHTML = '';
    noSeg.style.display = 'block';
    document.querySelector('.segment-table-wrap').style.display = 'none';
    return;
  }
  noSeg.style.display = 'none';
  document.querySelector('.segment-table-wrap').style.display = 'block';
  body.innerHTML = calSegments.map(function(seg, i) {
    var citStart = seg.citation_page_start != null ? seg.citation_page_start : '';
    if (seg.citation === null && !citStart) citStart = '';
    var style = seg.number_style || 'arabic';
    var layout = seg.layout_mode === 'spread' ? 'spread' : 'single';
    var label = seg.label || seg.evidence || '';
    return '<tr>'
      + '<td><input class="seg-input narrow" type="number" min="1" value="' + (seg.pdf_page_start != null ? seg.pdf_page_start + 1 : '') + '" onchange="updateCalSeg(' + i + ',\'pdf_page_start\',this.value)"></td>'
      + '<td><input class="seg-input narrow" type="number" min="1" value="' + (seg.pdf_page_end != null ? seg.pdf_page_end + 1 : '') + '" onchange="updateCalSeg(' + i + ',\'pdf_page_end\',this.value)"></td>'
      + '<td><input class="seg-input narrow" type="text" value="' + esc(String(citStart)) + '" placeholder="留空=不映射" onchange="updateCalSeg(' + i + ',\'citation_page_start\',this.value)"></td>'
      + '<td>' + segmentLayoutControl(layout, i) + '</td>'
      + '<td>' + segmentNumberStyleControl(style, i) + '</td>'
      + '<td><input class="seg-input" type="text" value="' + esc(label) + '" placeholder="序言、正文或附录" onchange="updateCalSeg(' + i + ',\'label\',this.value)"></td>'
      + '<td><button class="seg-remove" onclick="removeCalSegment(' + i + ')" title="删除分段" aria-label="删除分段"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="m7 7 1 13h8l1-13"/><path d="M10 11v5M14 11v5"/></svg></button></td>'
      + '</tr>'
      + segmentSpreadPanelRow(seg, i);
  }).join('');
}

function updateCalSeg(index, field, value) {
  var seg = calSegments[index];
  if (!seg) return;
  if (field === 'pdf_page_start' || field === 'pdf_page_end') {
    seg[field] = value === '' ? null : Math.max(0, parseInt(value, 10) - 1);
  } else if (field === 'citation_page_start') {
    if (value === '') {
      seg.citation_page_start = undefined;
      seg.citation = null;
    } else {
      seg.citation_page_start = value;
      delete seg.citation;
    }
  } else if (field === 'number_style' && value === 'none') {
    seg.number_style = 'none';
    seg.citation = null;
    delete seg.citation_page_start;
  } else if (field === 'layout_mode') {
    seg.layout_mode = value === 'spread' ? 'spread' : 'single';
    if (seg.layout_mode === 'spread') {
      if (seg.reading_direction !== 'rtl') seg.reading_direction = 'ltr';
      if (!isFinite(Number(seg.gutter_x))) seg.gutter_x = 0.5;
    }
  } else {
    seg[field] = value;
  }
  if (!seg.method) seg.method = 'manual_segment';
  if (seg.confidence == null) seg.confidence = 0.9;
  if (field !== 'layout_mode' && seg.layout_mode === 'spread') updateSpreadPanel(index);
  updateCalPreview();
}

function addCalSegment() {
  var lastEnd = 0;
  if (calSegments.length > 0) {
    var last = calSegments[calSegments.length - 1];
    lastEnd = (last.pdf_page_end != null ? last.pdf_page_end : 0) + 1;
  }
  calSegments.push({
    pdf_page_start: lastEnd,
    pdf_page_end: lastEnd + 49,
    citation_page_start: '1',
    layout_mode: 'single',
    number_style: 'arabic',
    method: 'manual_segment',
    confidence: 0.9,
    label: ''
  });
  renderCalSegments();
  updateCalPreview();
}

function removeCalSegment(index) {
  calSegments.splice(index, 1);
  renderCalSegments();
  updateCalPreview();
}

function updateCalPreview() {
  var input = document.getElementById('cal-preview-input');
  var result = document.getElementById('cal-preview-result');
  var pageIndex = parseInt(input.value, 10) - 1;
  if (isNaN(pageIndex) || pageIndex < 0) {
    result.textContent = '—';
    return;
  }
  // 纯算术已抽到 06-pure.js 的 calibrateCitationForIndex（可单测）；这里只负责读写 DOM。
  var calc = calibrateCitationForIndex(calSegments, pageIndex);
  var mapped = calc.mapped;
  var mappedEnd = calc.mappedEnd;
  var method = calc.method;
  if (mapped) {
    result.textContent = '引用' + formatCitationPageLabel({source_type:'pdf', citation_page_start:mapped, citation_page_end:mappedEnd || mapped}) + '（' + mappingMethodLabel(method) + '）';
    result.style.color = 'var(--accent)';
  } else {
    result.textContent = '未校准';
    result.style.color = 'var(--text-tertiary)';
  }
}

async function saveCalibration() {
  var sourceId = calSelectedSourceId;
  if (!sourceId) return;
  var hint = document.querySelector('.cal-save-hint');
  var cleanSegs = calSegments.map(function(seg) {
    var clean = {};
    if (seg.pdf_page_start != null) clean.pdf_page_start = seg.pdf_page_start;
    if (seg.pdf_page_end != null) clean.pdf_page_end = seg.pdf_page_end;
    if (seg.citation_page_start != null && seg.citation_page_start !== '') {
      clean.citation_page_start = seg.citation_page_start;
    } else {
      clean.citation = null;
    }
    if (seg.number_style) clean.number_style = seg.number_style;
    clean.layout_mode = seg.layout_mode === 'spread' ? 'spread' : 'single';
    if (clean.layout_mode === 'spread') {
      clean.reading_direction = seg.reading_direction === 'rtl' ? 'rtl' : 'ltr';
      var gutter = Number(seg.gutter_x);
      clean.gutter_x = isFinite(gutter) && gutter >= 0.3 && gutter <= 0.7 ? gutter : 0.5;
    }
    if (seg.method) clean.method = seg.method;
    if (seg.confidence != null) clean.confidence = seg.confidence;
    if (seg.label) clean.label = seg.label;
    if (seg.evidence) clean.evidence = seg.evidence;
    return clean;
  });
  try {
    hint.textContent = '正在保存并重建索引，请稍候…';
    var resp = await fetch('/api/calibration', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source_id: sourceId, segments: cleanSegs})
    });
    var data = await resp.json();
    if (data.ok) {
      hint.textContent = '校准已生效';
      showToast('校准已保存，索引已更新', 'success');
      await loadMeta();
      delete calTransientStatus[sourceId];
      await refreshCalibrationSource(sourceId);
    } else {
      hint.textContent = '保存失败';
      showToast('保存失败：' + (data.error || '未知错误'), 'danger');
    }
  } catch(e) {
    hint.textContent = '保存失败';
    showToast('保存失败：' + e.message, 'danger');
  }
}

function scrollToManualMapping() {
  var table = document.querySelector('#library-drawer .segment-table-wrap');
  if (table) table.scrollIntoView({behavior:'smooth', block:'center'});
}

function showCalibrationEvidence() {
  var item = libSources.find(function(value) { return value.source_file_id === calSelectedSourceId; });
  if (!item) return;
  var panel = document.getElementById('cal-auto-preview');
  var evidence = item.mapping_evidence || [];
  var failures = item.failure_reasons || [];
  var html = '<div class="auto-detect-title">自动映射依据</div>';
  if (item.mapping_summary) html += '<div class="auto-detect-note">当前映射：' + esc(item.mapping_summary) + '</div>';
  html += '<div class="auto-detect-note">映射方式：' + esc(mappingMethodLabel(item.mapping_method)) + '</div>';
  if (item.mapping_confidence) html += '<div class="auto-detect-note">置信度：' + Math.round(Number(item.mapping_confidence) * 100) + '%</div>';
  if (evidence.length) html += '<div class="auto-detect-note" style="margin-top:8px">已保存 ' + evidence.length + ' 组序列、位置或结构证据</div>';
  if (failures.length) html += '<div class="auto-detect-note" style="margin-top:8px">未使用的证据：<br>' + autoFailureReasons(failures) + '</div>';
  if (!item.mapping_summary && !evidence.length && !failures.length) html += '<div class="auto-detect-note">当前没有可显示的自动识别依据</div>';
  panel.innerHTML = html;
  panel.style.display = 'block';
  panel.scrollIntoView({behavior:'smooth', block:'center'});
}

function openRemoveDocumentModal(sourceId) {
  if (sourceId && typeof sourceId === 'string') calSelectedSourceId = sourceId;
  var targetId = calSelectedSourceId || libSelectedId;
  var item = libSources.find(function(value) { return value.source_file_id === targetId; });
  if (!item) return;
  openRemoveDocumentsModal([item]);
}

function openRemoveSelectedDocumentsModal() {
  var items = libSources.filter(function(item) {
    return libDeleteSelection.has(item.source_file_id) && isLibraryDeleteSelectable(item);
  });
  if (!items.length) {
    showToast('请先选择要删除的文献', 'warning');
    return;
  }
  openRemoveDocumentsModal(items);
}

function openRemoveDocumentsModal(items) {
  removeDocumentTargets = (items || []).filter(isLibraryDeleteSelectable);
  if (!removeDocumentTargets.length) return;
  removeDocumentTarget = removeDocumentTargets[0];
  removeSecondStage = false;
  var count = removeDocumentTargets.length;
  var pdfCount = removeDocumentTargets.filter(function(item) { return item.source_type === 'pdf'; }).length;
  var wordCount = removeDocumentTargets.filter(function(item) { return item.source_type === 'word'; }).length;
  document.getElementById('remove-modal-title').textContent = count === 1
    ? '从文献库移除《' + (removeDocumentTarget.title || removeDocumentTarget.file_name || '所选文献') + '》？'
    : '从文献库移除所选 ' + count + ' 篇文献？';
  var removalCopy;
  if (wordCount && pdfCount) {
    removalCopy = '移除后，所选文献将从文献库和搜索结果中消失。Word 文献的应用内语料副本会一并删除，外部原文件不受影响；PDF 文件默认保留';
  } else if (wordCount) {
    removalCopy = count === 1
      ? '移除后，该文献将从文献库和搜索结果中消失，应用内保存的 Word 语料副本也会删除；最初导入位置的原文件不受影响'
      : '移除后，这 ' + count + ' 篇文献将从文献库和搜索结果中消失，应用内保存的 Word 语料副本也会删除；最初导入位置的原文件不受影响';
  } else {
    removalCopy = count === 1
      ? '移除后，该文献将从文献库和搜索结果中消失。默认清理索引、页码映射和元数据，但保留 PDF 文件；以后重新导入相同文件时会复用这份副本'
      : '移除后，这 ' + count + ' 篇文献将从文献库和搜索结果中消失。默认清理索引、页码映射和元数据，但保留原 PDF 文件；以后重新导入相同文件时会复用已保留的副本';
  }
  document.getElementById('remove-modal-copy').textContent = removalCopy;
  document.getElementById('remove-generated').checked = true;
  document.getElementById('remove-generated-option').style.display = pdfCount ? 'flex' : 'none';
  document.getElementById('remove-internal-copy').checked = false;
  var internalCount = removeDocumentTargets.filter(function(item) {
    return item.source_type === 'pdf' && item.can_delete_internal_copy;
  }).length;
  document.getElementById('remove-internal-option').style.display = internalCount ? 'flex' : 'none';
  document.getElementById('remove-modal-warning').textContent = wordCount
    ? '删除应用内 Word 语料副本后无法恢复，外部原文件不会删除。请再次确认此操作'
    : '删除应用内 PDF 副本后无法恢复。请再次确认此操作';
  document.getElementById('remove-modal-warning').classList.remove('show');
  document.getElementById('confirm-remove-btn').textContent = count === 1 ? '从文献库移除' : '移除所选 ' + count + ' 篇';
  document.getElementById('confirm-remove-btn').disabled = false;
  document.getElementById('remove-document-modal').classList.add('open');
}

function closeRemoveDocumentModal() {
  // 取消必须真的中止请求：关掉弹窗但让删除继续跑，是之前最容易误解的地方。
  if (removeRequestController) {
    removeRequestController.abort();
    removeRequestController = null;
  }
  document.getElementById('remove-document-modal').classList.remove('open');
  removeDocumentTarget = null;
  removeDocumentTargets = [];
  removeSecondStage = false;
}

function removeModalBackdropClick(event) {
  if (event.target.id !== 'remove-document-modal') return;
  // 移除进行中时不让误触背景关掉对话框——那会连带中止请求。
  if (removeRequestController) return;
  closeRemoveDocumentModal();
}

async function confirmRemoveDocument() {
  var targets = removeDocumentTargets.length ? removeDocumentTargets.slice() : (removeDocumentTarget ? [removeDocumentTarget] : []);
  if (!targets.length) return;
  var deleteInternalRequested = document.getElementById('remove-internal-copy').checked;
  var hasWordTargets = targets.some(function(item) { return item.source_type === 'word'; });
  if ((deleteInternalRequested || hasWordTargets) && !removeSecondStage) {
    removeSecondStage = true;
    document.getElementById('remove-modal-warning').classList.add('show');
    document.getElementById('confirm-remove-btn').textContent = targets.length === 1 ? '确认移除并删除副本' : '确认移除并删除应用内副本';
    return;
  }
  var button = document.getElementById('confirm-remove-btn');
  button.disabled = true;
  button.textContent = targets.length === 1 ? '正在移除…' : '正在移除 ' + targets.length + ' 篇…';
  var removedIds = [];
  var failures = [];
  var deleteGenerated = document.getElementById('remove-generated').checked
    && targets.some(function(item) { return item.source_type === 'pdf'; });
  var sourceIds = targets.map(function(item) { return item.source_file_id; });
  // 一次请求一个事务：逐份删除会为每份文献整份复制索引数据库。
  removeRequestController = typeof AbortController === 'function' ? new AbortController() : null;
  try {
    var resp = await fetch('/api/documents/remove-batch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      signal: removeRequestController ? removeRequestController.signal : undefined,
      body: JSON.stringify({
        source_ids: sourceIds,
        delete_generated_artifacts: deleteGenerated,
        internal_copy_source_ids: deleteInternalRequested
          ? targets.filter(function(item) { return item.can_delete_internal_copy; }).map(function(item) { return item.source_file_id; })
          : []
      })
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) {
      (data.failures || []).forEach(function(item) {
        failures.push({source_id: item.source_id, message: item.error || '移除失败'});
      });
      throw new Error(data.error || '移除失败');
    }
    var result = data.result || {};
    var reported = {};
    (result.failures || []).forEach(function(item) {
      reported[item.source_id] = true;
      failures.push({source_id: item.source_id, message: item.error || '移除失败'});
    });
    (result.removed_source_ids || []).forEach(function(sourceId) {
      removedIds.push(sourceId);
      delete calTransientStatus[sourceId];
      libDeleteSelection.delete(sourceId);
    });
    sourceIds.forEach(function(sourceId) {
      if (removedIds.indexOf(sourceId) < 0 && !reported[sourceId]) {
        failures.push({source_id: sourceId, message: '服务端未确认移除'});
      }
    });
  } catch(e) {
    if (e && e.name === 'AbortError') {
      // 服务端的批量移除是一个整体事务，中止只是不再等待结果。
      removeRequestController = null;
      button.disabled = false;
      button.textContent = targets.length === 1 ? '从文献库移除' : '移除所选 ' + targets.length + ' 篇';
      await loadLibrary(true);
      showToast('已停止等待。移除是一个整体事务，服务端可能已经完成，文献库已刷新', 'warning');
      return;
    }
    if (!failures.length) {
      sourceIds.forEach(function(sourceId) {
        failures.push({source_id: sourceId, message: e.message || '移除失败'});
      });
    }
  }
  removeRequestController = null;

  if (removedIds.length) {
    var removedSet = new Set(removedIds);
    closeRemoveDocumentModal();
    closeLibDrawer();
    if (removedSet.has(searchDocumentId)) searchDocumentId = '';
    await loadMeta();
    // 一次强制刷新同时喂给文献库与搜索下拉。
    await loadLibrary(true);
    updateSearchDocumentLabel();
    window.dispatchEvent(new CustomEvent('library_changed', {detail:{source_ids:removedIds}}));
    var query = document.getElementById('query').value.trim();
    if (query && searchResults.some(function(item) { return removedSet.has(item.source_file_id); })) await runSearch();
  }

  if (failures.length) {
    // Keep the failed items selected so the action bar stays up for a retry.
    failures.forEach(function(item) { libDeleteSelection.add(item.source_id); });
    renderLibraryList();
    if (!removedIds.length) {
      button.disabled = false;
      button.textContent = targets.length === 1 ? '从文献库移除' : '重试删除所选';
    }
    showToast((removedIds.length ? '已移除 ' + removedIds.length + ' 篇；' : '') + failures.length + ' 篇移除失败：' + failures[0].message, 'danger');
  } else {
    libDeleteSelection.clear();
    renderLibraryList();
    var successMessage;
    if (hasWordTargets && deleteInternalRequested) {
      successMessage = '所选文献及应用内副本已移除，外部原文件不受影响';
    } else if (hasWordTargets) {
      successMessage = removedIds.length > 1
        ? '已移除 ' + removedIds.length + ' 篇文献；Word 应用内语料副本已删除，PDF 文件已保留'
        : 'Word 文献及应用内语料副本已移除，外部原文件不受影响';
    } else if (deleteInternalRequested) {
      successMessage = '所选文献及可删除的应用内 PDF 副本已移除';
    } else {
      successMessage = removedIds.length > 1 ? '已移除 ' + removedIds.length + ' 篇文献，原 PDF 文件已保留' : '文献已移除，PDF 文件已保留';
    }
    showToast(successMessage, 'success');
  }

  if (!removedIds.length && !failures.length) {
    button.disabled = false;
    button.textContent = '从文献库移除';
  }
}

