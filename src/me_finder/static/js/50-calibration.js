/* IIFE 包裹：私有化实现，仅下方公共面挂到全局（#7 前端全局作用域收敛）。
   模式同 reader.js；IIFE 实参在 node 下退回 globalThis。 */
(function (global) {  // module: 50-calibration.js
  /* ═══ Calibration ═══ */
  async function refreshCalibrationSource(sourceId) {
    var data;
    try {
      data = await fetchLibraryCatalog(true);
    } catch (error) {
      throw new Error(error && error.message ? error.message : '刷新文献状态失败');
    }
    global.MEFinder.library.applyCatalog(data);
    await global.MEFinder.library.ensureDetail(sourceId);
    global.MEFinder.library.updateEntry(sourceId);
    if (calSelectedSourceId === sourceId) await loadCalibrationDoc(sourceId);
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

  // 页码校准两级深度：默认只出解释 + 自动检测 + 预览（L1，覆盖 90% 情况）；
  // 7 列专家分段表（L2）只在「手动调整」、载入自动结果编辑、检测失败或已有分段时展开。
  function setCalExpertVisible(show) {
    var expert = document.getElementById('cal-expert');
    if (expert) expert.style.display = show ? 'block' : 'none';
  }

  function calibrationNode(tag, className, content) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = String(content);
    return node;
  }

  function calibrationNote(panel, content, extraClass) {
    panel.appendChild(calibrationNode('div', 'auto-detect-note' + (extraClass ? ' ' + extraClass : ''), content));
  }

  function calibrationAction(panel, label, action, primary) {
    var button = calibrationNode('button', 'action-btn' + (primary ? ' primary' : ''), label);
    button.dataset.action = action;
    panel.appendChild(button);
  }

  function calibrationFailureNotes(panel, reasons) {
    var note = calibrationNode('div', 'auto-detect-note');
    autoFailureReasons(reasons).split('\n').forEach(function(label, index) {
      if (index) note.appendChild(document.createElement('br'));
      note.appendChild(document.createTextNode(label));
    });
    panel.appendChild(note);
  }

  function calibrationSvg(shapes, viewBox) {
    var namespace = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(namespace, 'svg');
    [['viewBox', viewBox || '0 0 24 24'], ['fill', 'none'], ['stroke', 'currentColor'],
      ['stroke-width', '1.8'], ['stroke-linecap', 'round'], ['stroke-linejoin', 'round'],
      ['aria-hidden', 'true']].forEach(function(attribute) { svg.setAttribute(attribute[0], attribute[1]); });
    shapes.forEach(function(shape) {
      var child = document.createElementNS(namespace, shape[0]);
      Object.keys(shape[1]).forEach(function(name) { child.setAttribute(name, shape[1][name]); });
      svg.appendChild(child);
    });
    return svg;
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
      var resp = await MEFinderApi.fetch('/api/calibration?source_id=' + encodeURIComponent(sourceId));
      calSelectedDoc = await resp.json();
      if (calSelectedDoc.error) {
        showToast('文献未找到');
        return;
      }
      var mapping = calSelectedDoc.page_mapping || {};
      calSegments = (mapping.segments || []).map(function(s) { return Object.assign({}, s); });
      var actions = document.getElementById('cal-detail-actions');
      actions.replaceChildren();
      calibrationAction(actions, '自动检测页码', 'runAutoDetection', true);
      actions.firstElementChild.id = 'cal-auto-detect-btn';
      calibrationAction(actions, '手动调整', 'scrollToManualMapping');
      calibrationAction(actions, '查看识别依据', 'showCalibrationEvidence');
      var pill = calibrationNode('span', 'detail-pill', mapping.validated_by ? '已验证' : '未验证');
      pill.style.marginLeft = 'auto';
      actions.appendChild(pill);
      editor.style.display = 'block';
      calAutoResult = null;
      document.getElementById('cal-auto-preview').style.display = 'none';
      renderCalSegments();
      updateCalPreview();
      // 已有分段（配置过的文献）直接展开专家表；否则收起，先走自动检测 L1。
      setCalExpertVisible(calSegments.length > 0);
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
    global.MEFinder.library.updateEntry(sourceId);
    var panel = document.getElementById('cal-auto-preview');
    var button = document.getElementById('cal-auto-detect-btn');
    panel.style.display = 'block';
    panel.replaceChildren(calibrationNode('div', 'auto-detect-title', '正在检测页码与页面布局…'));
    calibrationNote(panel, '正在读取页面尺寸、左右内容分布、中缝、页码位置、PDF 标签、数字书签和现有 MinerU 结果');
    if (button) button.disabled = true;
    try {
      var resp = await MEFinderApi.fetch('/api/auto-page-mapping/detect', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({source_id:sourceId, dry_run:true})
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '页码自动检测失败');
      calAutoResult = data.result;
      renderAutoDetectionResult(calAutoResult);
      var current = libraryStore.sources.find(function(item) { return item.source_file_id === sourceId; });
      var segments = (calAutoResult.selected_segments || []).filter(function(item) { return item && item.confidence_level !== 'low'; });
      calTransientStatus[sourceId] = current && current.status === 'manual_mapped' ? 'manual_mapped' : (segments.length ? 'needs_review' : 'auto_mapping_failed');
    } catch(e) {
      calAutoResult = null;
      calTransientStatus[sourceId] = 'auto_mapping_failed';
      panel.replaceChildren(calibrationNode('div', 'auto-detect-title', '页码自动检测失败'));
      calibrationNote(panel, e.message);
    } finally {
      if (button) button.disabled = false;
      global.MEFinder.library.updateEntry(sourceId);
    }
  }

  function renderAutoDetectionResult(result) {
    var panel = document.getElementById('cal-auto-preview');
    var segments = (result.selected_segments || []).filter(function(s) { return s && s.confidence_level !== 'low'; });
    panel.replaceChildren(calibrationNode('div', 'auto-detect-title', '检测完成'));
    var layout = result.layout_detection || {};
    if (layout.layout_mode === 'spread') {
      var layoutEvidence = layout.evidence || {};
      calibrationNote(panel, '页面布局：双开页 · '
        + (layout.reading_direction === 'rtl' ? '右→左' : '左→右')
        + ' · 中缝 ' + Math.round(Number(layout.gutter_x || 0.5) * 100) + '%'
        + ' · ' + mappingConfidenceLabel(layout.confidence_level, layout.confidence), 'auto-detect-layout');
      calibrationNote(panel, '布局依据：' + Number(layoutEvidence.split_pages || 0) + ' 个双栏页面；'
        + Number(layoutEvidence.paired_page_numbers || 0) + ' 页检测到成对页码；双页序列支持 '
        + Number(layoutEvidence.stride_two_support || 0) + ' 页');
    } else if (layout.layout_mode === 'single') {
      calibrationNote(panel, '页面布局：单页 · ' + mappingConfidenceLabel(layout.confidence_level, layout.confidence));
    }
    if (result.manual_mapping_present) {
      calibrationNote(panel, '当前文献已有人工页码映射。以下结果仅为预览，不会自动覆盖', 'auto-detect-warning');
    }
    if (!segments.length) {
      calibrationNote(panel, '未能自动识别可靠页码区间');
      calibrationFailureNotes(panel, result.failure_reasons || []);
      var closeActions = calibrationNode('div', 'auto-detect-actions');
      calibrationAction(closeActions, '关闭', 'cancelAutoDetection');
      panel.appendChild(closeActions);
      setCalExpertVisible(true);  // 检测失败：展开专家表让用户手动设置
      return;
    }
    calibrationNote(panel, '识别到 ' + segments.length + ' 个页码区间，当前仍是预览状态');
    var list = calibrationNode('div', 'auto-segment-list');
    segments.forEach(function(seg, index) {
      var evidence = seg.mapping_evidence || {};
      var row = calibrationNode('div', 'auto-segment-row');
      row.appendChild(calibrationNode('div', 'auto-segment-main', (index + 1) + '. ' + autoMappingSegmentText(seg)));
      row.appendChild(calibrationNode('div', 'auto-segment-evidence', '依据：' + mappingMethodLabel(seg.method)
        + (evidence.inferred_offset != null ? '；稳定 offset = ' + evidence.inferred_offset : '')
        + (evidence.observed_page_numbers != null ? '；观察到 ' + evidence.observed_page_numbers + ' 个候选' : '')
        + (evidence.sequence_consistency != null ? '；序列一致性 ' + Math.round(Number(evidence.sequence_consistency) * 100) + '%' : '')));
      list.appendChild(row);
    });
    panel.appendChild(list);
    var details = calibrationNode('details');
    details.style.marginTop = '10px';
    details.appendChild(calibrationNode('summary', 'auto-detect-note', '查看检测依据'));
    var evidenceNote = calibrationNode('div', 'auto-detect-note',
      'PDF 标签 ' + Number((result.evidence_counts || {}).pdf_page_labels || 0) + ' 个；数字书签 ' + Number((result.evidence_counts || {}).numeric_bookmarks || 0)
      + ' 个；MinerU 候选 ' + Number((result.evidence_counts || {}).mineru_candidates || 0) + ' 个；页边候选 ' + Number((result.evidence_counts || {}).native_edge_candidates || 0) + ' 个');
    evidenceNote.style.marginTop = '6px';
    details.appendChild(evidenceNote);
    panel.appendChild(details);
    var actions = calibrationNode('div', 'auto-detect-actions');
    calibrationAction(actions, result.manual_mapping_present ? '用自动结果替换人工映射' : '应用自动映射', 'applyAutoDetection', true);
    calibrationAction(actions, '编辑后应用', 'editAutoDetectionResult');
    calibrationAction(actions, '取消', 'cancelAutoDetection');
    panel.appendChild(actions);
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
      var resp = await MEFinderApi.fetch('/api/auto-page-mapping/apply', {
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
    setCalExpertVisible(true);  // 载入自动结果到手动编辑区：展开专家表
    document.getElementById('cal-auto-preview').style.display = 'none';
    showToast('自动结果已载入手动编辑区');
  }

  function cancelAutoDetection() {
    calAutoResult = null;
    document.getElementById('cal-auto-preview').style.display = 'none';
  }



  function setSegmentNumberStyle(event, index, value) {
    event.stopImmediatePropagation();
    updateCalSeg(index, 'number_style', value);
    closeAppSelects();
    renderCalSegments();
  }



  function setSegmentLayout(event, index, value) {
    event.stopImmediatePropagation();
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
    renderSpreadSummary(document.getElementById('spread-summary-' + index), seg);
    var ltrBtn = diagram.parentNode.querySelector('.segment-direction-btn[data-direction="ltr"]');
    var rtlBtn = diagram.parentNode.querySelector('.segment-direction-btn[data-direction="rtl"]');
    if (ltrBtn && rtlBtn) {
      ltrBtn.classList.toggle('is-active', direction === 'ltr');
      ltrBtn.setAttribute('aria-pressed', direction === 'ltr' ? 'true' : 'false');
      rtlBtn.classList.toggle('is-active', direction === 'rtl');
      rtlBtn.setAttribute('aria-pressed', direction === 'rtl' ? 'true' : 'false');
    }
  }

  function renderSpreadSummary(host, seg) {
    var firstPdf = seg.pdf_page_start != null ? seg.pdf_page_start + 1 : 1;
    var pair = spreadCitationPair(seg);
    var icon = calibrationSvg([
      ['path', {d: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z'}],
      ['circle', {cx: '12', cy: '12', r: '3'}]
    ]);
    icon.setAttribute('width', '14');
    icon.setAttribute('height', '14');
    host.replaceChildren(icon, document.createTextNode(' PDF 第 ' + firstPdf + ' 页 → '));
    if (!pair.mapped) {
      host.appendChild(document.createTextNode('该分段未设引用页码，仅按双开切分'));
      return;
    }
    host.appendChild(document.createTextNode('左半页 '));
    host.appendChild(calibrationNode('b', null, '引文 ' + pair.left + ' 页'));
    host.appendChild(document.createTextNode('，右半页 '));
    host.appendChild(calibrationNode('b', null, '引文 ' + pair.right + ' 页'));
  }

  function segmentSelectControl(kind, value, index) {
    var isLayout = kind === 'layout';
    var id = 'segment-' + (isLayout ? 'layout' : 'style') + '-select-' + index;
    var values = isLayout ? ['single', 'spread'] : ['arabic', 'roman_lower', 'roman_upper', 'none'];
    var label = isLayout ? segmentLayoutLabel : segmentNumberStyleLabel;
    var control = calibrationNode('div', 'app-select segment-' + (isLayout ? 'layout' : 'style') + '-select');
    control.id = id;
    var trigger = calibrationNode('button', 'app-select-trigger');
    trigger.type = 'button';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.dataset.action = 'toggleSegmentSelect';
    trigger.dataset.selectId = id;
    trigger.appendChild(calibrationNode('span', 'app-select-value', label(value)));
    trigger.appendChild(calibrationSvg([['path', {d: 'm6 8 4 4 4-4'}]], '0 0 20 20'));
    control.appendChild(trigger);
    var menu = calibrationNode('div', 'app-select-menu');
    menu.setAttribute('role', 'listbox');
    values.forEach(function(option) {
      var button = calibrationNode('button', 'app-select-option' + (option === value ? ' is-selected' : ''), label(option));
      button.type = 'button';
      button.dataset.value = option;
      button.dataset.index = String(index);
      button.dataset.action = isLayout ? 'setSegmentLayout' : 'setSegmentNumberStyle';
      menu.appendChild(button);
    });
    control.appendChild(menu);
    return control;
  }

  function segmentInputCell(index, field, value, type, className, placeholder) {
    var cell = document.createElement('td');
    var input = calibrationNode('input', className);
    input.type = type;
    if (type === 'number') input.min = '1';
    input.value = value == null ? '' : String(value);
    if (placeholder) input.placeholder = placeholder;
    input.dataset.actionChange = 'updateCalSeg';
    input.dataset.index = String(index);
    input.dataset.field = field;
    cell.appendChild(input);
    return cell;
  }

  function spreadPanelRow(seg, index) {
    var direction = seg.reading_direction === 'rtl' ? 'rtl' : 'ltr';
    var gutter = spreadGutterPercent(seg);
    var pair = spreadCitationPair(seg);
    var row = calibrationNode('tr', 'segment-spread-row');
    var cell = document.createElement('td');
    cell.colSpan = 7;
    var panel = calibrationNode('div', 'segment-spread-panel');
    var main = calibrationNode('div', 'spread-panel-main');
    var diagram = calibrationNode('div', 'spread-diagram');
    diagram.id = 'spread-diagram-' + index;
    [['left', gutter, direction !== 'rtl' ? '1' : '2', pair.mapped ? '引文 ' + pair.left + ' 页' : '不映射'],
      ['right', 100 - gutter, direction !== 'rtl' ? '2' : '1', pair.mapped ? '引文 ' + pair.right + ' 页' : '不映射']].forEach(function(halfData) {
      var side = halfData[0];
      var half = calibrationNode('div', 'spread-half ' + side);
      half.id = 'spread-half-' + side + '-' + index;
      half.style.width = halfData[1] + '%';
      var badge = calibrationNode('span', 'spread-badge' + (side === 'right' ? ' alt' : ''), halfData[2]);
      badge.id = 'spread-badge-' + side + '-' + index;
      half.appendChild(badge);
      half.appendChild(calibrationNode('span', 'spread-half-name', side === 'left' ? '左半页' : '右半页'));
      var page = calibrationNode('span', 'spread-half-page', halfData[3]);
      page.id = 'spread-page-' + side + '-' + index;
      half.appendChild(page);
      diagram.appendChild(half);
    });
    var line = calibrationNode('div', 'spread-gutter-line');
    line.id = 'spread-gutter-line-' + index;
    line.style.left = gutter + '%';
    diagram.appendChild(line);
    main.appendChild(diagram);
    var controls = calibrationNode('div', 'spread-controls');
    var directionField = calibrationNode('div', 'spread-field');
    directionField.appendChild(calibrationNode('span', 'spread-field-label', '阅读方向'));
    var directionControl = calibrationNode('div', 'segment-direction-control');
    directionControl.setAttribute('role', 'group');
    directionControl.setAttribute('aria-label', '双开页阅读方向');
    [['ltr', '左→右'], ['rtl', '右→左']].forEach(function(option) {
      var active = direction === option[0];
      var button = calibrationNode('button', 'segment-direction-btn' + (active ? ' is-active' : ''), option[1]);
      button.type = 'button';
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.dataset.action = 'setSegmentReadingDirection';
      button.dataset.index = String(index);
      button.dataset.direction = option[0];
      directionControl.appendChild(button);
    });
    directionField.appendChild(directionControl);
    controls.appendChild(directionField);
    var gutterField = calibrationNode('div', 'spread-field');
    var gutterHead = calibrationNode('div', 'spread-field-row');
    gutterHead.appendChild(calibrationNode('span', 'spread-field-label', '中缝位置'));
    var output = calibrationNode('span', 'spread-gutter-out', gutter + '%');
    output.id = 'spread-gutter-out-' + index;
    gutterHead.appendChild(output);
    gutterField.appendChild(gutterHead);
    var range = calibrationNode('input', 'spread-gutter-range');
    range.type = 'range';
    range.min = '30'; range.max = '70'; range.step = '1'; range.value = String(gutter);
    range.setAttribute('aria-label', '中缝横向位置');
    range.dataset.actionInput = 'updateSegmentGutter';
    range.dataset.index = String(index);
    gutterField.appendChild(range);
    controls.appendChild(gutterField);
    main.appendChild(controls);
    panel.appendChild(main);
    var summary = calibrationNode('div', 'spread-summary');
    summary.id = 'spread-summary-' + index;
    renderSpreadSummary(summary, seg);
    panel.appendChild(summary);
    cell.appendChild(panel);
    row.appendChild(cell);
    return row;
  }

  function renderCalSegments() {
    var body = document.getElementById('cal-segments-body');
    var noSeg = document.getElementById('cal-no-segments');
    if (calSegments.length === 0) {
      body.replaceChildren();
      noSeg.style.display = 'block';
      document.querySelector('.segment-table-wrap').style.display = 'none';
      return;
    }
    noSeg.style.display = 'none';
    document.querySelector('.segment-table-wrap').style.display = 'block';
    body.replaceChildren();
    calSegments.forEach(function(seg, i) {
      var citStart = seg.citation_page_start != null ? seg.citation_page_start : '';
      if (seg.citation === null && !citStart) citStart = '';
      var style = seg.number_style || 'arabic';
      var layout = seg.layout_mode === 'spread' ? 'spread' : 'single';
      var label = seg.label || seg.evidence || '';
      var row = document.createElement('tr');
      row.appendChild(segmentInputCell(i, 'pdf_page_start', seg.pdf_page_start != null ? seg.pdf_page_start + 1 : '', 'number', 'seg-input narrow'));
      row.appendChild(segmentInputCell(i, 'pdf_page_end', seg.pdf_page_end != null ? seg.pdf_page_end + 1 : '', 'number', 'seg-input narrow'));
      row.appendChild(segmentInputCell(i, 'citation_page_start', citStart, 'text', 'seg-input narrow', '留空=不映射'));
      var layoutCell = document.createElement('td');
      layoutCell.appendChild(segmentSelectControl('layout', layout, i));
      row.appendChild(layoutCell);
      var styleCell = document.createElement('td');
      styleCell.appendChild(segmentSelectControl('style', style, i));
      row.appendChild(styleCell);
      row.appendChild(segmentInputCell(i, 'label', label, 'text', 'seg-input', '序言、正文或附录'));
      var removeCell = document.createElement('td');
      var removeButton = calibrationNode('button', 'seg-remove');
      removeButton.dataset.action = 'removeCalSegment';
      removeButton.dataset.index = String(i);
      removeButton.title = '删除分段';
      removeButton.setAttribute('aria-label', '删除分段');
      removeButton.appendChild(calibrationSvg([
        ['path', {d: 'M4 7h16'}], ['path', {d: 'M9 7V4h6v3'}],
        ['path', {d: 'm7 7 1 13h8l1-13'}], ['path', {d: 'M10 11v5M14 11v5'}]
      ]));
      removeCell.appendChild(removeButton);
      row.appendChild(removeCell);
      body.appendChild(row);
      if (layout === 'spread') body.appendChild(spreadPanelRow(seg, i));
    });
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
      var resp = await MEFinderApi.fetch('/api/calibration', {
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
    setCalExpertVisible(true);  // 「手动设置」：展开专家分段表（L2）
    var table = document.querySelector('#library-drawer .segment-table-wrap');
    if (table) table.scrollIntoView({behavior:'smooth', block:'center'});
  }

  function showCalibrationEvidence() {
    var item = libraryStore.sources.find(function(value) { return value.source_file_id === calSelectedSourceId; });
    if (!item) return;
    var panel = document.getElementById('cal-auto-preview');
    var evidence = item.mapping_evidence || [];
    var failures = item.failure_reasons || [];
    panel.replaceChildren(calibrationNode('div', 'auto-detect-title', '自动映射依据'));
    if (item.mapping_summary) calibrationNote(panel, '当前映射：' + item.mapping_summary);
    calibrationNote(panel, '映射方式：' + mappingMethodLabel(item.mapping_method));
    if (item.mapping_confidence) calibrationNote(panel, '置信度：' + Math.round(Number(item.mapping_confidence) * 100) + '%');
    if (evidence.length) {
      var saved = calibrationNode('div', 'auto-detect-note', '已保存 ' + evidence.length + ' 组序列、位置或结构证据');
      saved.style.marginTop = '8px';
      panel.appendChild(saved);
    }
    if (failures.length) {
      var unused = calibrationNode('div', 'auto-detect-note', '未使用的证据：');
      unused.style.marginTop = '8px';
      panel.appendChild(unused);
      autoFailureReasons(failures).split('\n').forEach(function(label, index) {
        if (index) unused.appendChild(document.createElement('br'));
        unused.appendChild(document.createTextNode(label));
      });
    }
    if (!item.mapping_summary && !evidence.length && !failures.length) calibrationNote(panel, '当前没有可显示的自动识别依据');
    panel.style.display = 'block';
    panel.scrollIntoView({behavior:'smooth', block:'center'});
  }

  function openRemoveDocumentModal(sourceId) {
    if (sourceId && typeof sourceId === 'string') calSelectedSourceId = sourceId;
    var targetId = calSelectedSourceId || libraryStore.selectedId;
    var item = libraryStore.sources.find(function(value) { return value.source_file_id === targetId; });
    if (!item) return;
    openRemoveDocumentsModal([item]);
  }

  function openRemoveSelectedDocumentsModal() {
    var items = libraryStore.sources.filter(function(item) {
      return libraryStore.deleteSelection.has(item.source_file_id) && isLibraryDeleteSelectable(item);
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
      var resp = await MEFinderApi.fetch('/api/documents/remove-batch', {
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
        libraryStore.deleteSelection.delete(sourceId);
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
        await global.MEFinder.library.load(true);
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
      global.MEFinder.bibliography.closeDrawer();
      if (removedSet.has(searchStore.documentId)) searchStore.documentId = '';
      await loadMeta();
      // 一次强制刷新同时喂给文献库与搜索下拉。
      await global.MEFinder.library.load(true);
      updateSearchDocumentLabel();
      window.dispatchEvent(new CustomEvent('library_changed', {detail:{source_ids:removedIds}}));
      var query = document.getElementById('query').value.trim();
      if (query && searchStore.results.some(function(item) { return removedSet.has(item.source_file_id); })) await runSearch();
    }

    if (failures.length) {
      // Keep the failed items selected so the action bar stays up for a retry.
      failures.forEach(function(item) { libraryStore.deleteSelection.add(item.source_id); });
      global.MEFinder.library.renderList();
      if (!removedIds.length) {
        button.disabled = false;
        button.textContent = targets.length === 1 ? '从文献库移除' : '重试删除所选';
      }
      showToast((removedIds.length ? '已移除 ' + removedIds.length + ' 篇；' : '') + failures.length + ' 篇移除失败：' + failures[0].message, 'danger');
    } else {
      libraryStore.deleteSelection.clear();
      global.MEFinder.library.renderList();
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


  MEFinderActions.register('setSegmentNumberStyle', function(event, target) {
    setSegmentNumberStyle(event, Number(target.dataset.index), target.dataset.value);
  });
  MEFinderActions.register('setSegmentLayout', function(event, target) {
    setSegmentLayout(event, Number(target.dataset.index), target.dataset.value);
  });
  MEFinderActions.register('setSegmentReadingDirection', function(event, target) {
    setSegmentReadingDirection(Number(target.dataset.index), target.dataset.direction);
  });
  MEFinderActions.register('updateSegmentGutter', function(event, target) {
    updateSegmentGutter(Number(target.dataset.index), target.value);
  });
  MEFinderActions.register('runAutoDetection', function() { runAutoDetection(); });
  MEFinderActions.register('scrollToManualMapping', function() { scrollToManualMapping(); });
  MEFinderActions.register('showCalibrationEvidence', function() { showCalibrationEvidence(); });
  MEFinderActions.register('cancelAutoDetection', function() { cancelAutoDetection(); });
  MEFinderActions.register('applyAutoDetection', function() { applyAutoDetection(); });
  MEFinderActions.register('editAutoDetectionResult', function() { editAutoDetectionResult(); });
  MEFinderActions.register('updateCalSeg', function(event, target) {
    updateCalSeg(Number(target.dataset.index), target.dataset.field, target.value);
  });
  MEFinderActions.register('removeCalSegment', function(event, target) {
    removeCalSegment(Number(target.dataset.index));
  });

  // 浏览器公共面：仅这些符号可被其它 static/js 文件与模板动作访问。
  global.calPinyinCollator = calPinyinCollator;
  global.calibrationSortText = calibrationSortText;
  global.loadCalibrationDoc = loadCalibrationDoc;
  global.runAutoDetection = runAutoDetection;
  global.addCalSegment = addCalSegment;
  global.updateCalPreview = updateCalPreview;
  global.saveCalibration = saveCalibration;
  global.openRemoveDocumentModal = openRemoveDocumentModal;
  global.openRemoveSelectedDocumentsModal = openRemoveSelectedDocumentsModal;
  global.closeRemoveDocumentModal = closeRemoveDocumentModal;
  global.removeModalBackdropClick = removeModalBackdropClick;
  global.confirmRemoveDocument = confirmRemoveDocument;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
