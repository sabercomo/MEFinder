/* 导入界面：扫描、文件选择与渲染；任务状态只从 importJobs 快照读取。
   node 白盒测试走 module.exports；IIFE 实参在 node 下退回 globalThis。 */
(function (global) {  // module: 80-import.js
  var importJobs = global.MEFinder.importJobs;

  /* ═══ Import ═══ */
  function importNode(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function importAction(label, action, id, primary) {
    var button = importNode('button', 'action-btn' + (primary ? ' primary' : ''), label);
    button.type = 'button';
    button.dataset.action = action;
    if (id != null) button.dataset.id = String(id);
    return button;
  }
  function visionRetryProviderFor(q) {
    if (!q || q.status !== 'error') return null;
    // 索引阶段失败不给切换（重解析救不了重建索引错误）。中断态不再一律屏蔽：
    // 后端 public_import_job 会按当前配置回填 canRetryVision，配了接口才显式放行。
    if (q.failureStage === 'index') return null;
    if (!q.canRetryVision && !q.needsProviderConfig && !q.mineruFailed && !q.visionFailed) return null;
    var providers = global.MEFinder.visionProviders.configured();
    var selectedRecoveryId = global.MEFinder.visionProviders.selectedRecoveryProviderId();
    var preferredId = selectedRecoveryId || q.retryProviderId || '';
    return providers.find(function(provider) {
      return provider.id === preferredId;
    }) || providers[0] || null;
  }

  function importQueueNeedsRecoverySelector() {
    return importJobs.snapshot().some(function(q) {
      return q && q.type === 'pdf' && q.status === 'error' && q.failureStage !== 'index'
        && (q.canRetryVision || q.needsProviderConfig || q.mineruFailed || q.visionFailed);
    });
  }

  function syncImportRecoveryPanel() {
    var panel = document.getElementById('import-recovery-panel');
    if (!panel) return;
    var shouldShow = importQueueNeedsRecoverySelector();
    panel.hidden = !shouldShow;
    if (shouldShow) global.MEFinder.visionProviders.syncRecoveryProvider();
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
    zone.addEventListener('keydown', function(e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      zone.click();
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
      var resp = await MEFinderApi.fetch('/api/bibliographic-metadata/batch-detect', {
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
    MEFinderApi.fetch('/api/import-status?job_id=' + encodeURIComponent(jobId))
      .then(function(resp) { return resp.json(); })
      .then(function(data) {
        if (data.status === 'completed') {
          if (button) { button.disabled = false; button.textContent = '补全书目'; }
          showToast(data.message || '批量识别完成');
          global.MEFinder.library.load(true);
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

  const SCAN_IMPORT_BATCH_LIMIT = 50;
  let scanEntries = [];
  let scanDragSelection = null;
  let suppressScanSelectionClick = false;

  async function runDirectoryScan() {
    var statusEl = document.getElementById('scan-status');
    var button = document.getElementById('scan-run-btn');
    if (!settingsStore.scanDirectories.length) {
      statusEl.textContent = desktopShell
        ? '尚未添加文献文件夹：先点击「选择文件夹」'
        : '尚未添加文献文件夹：在上方输入框粘贴路径并回车即可';
      return;
    }
    button.disabled = true;
    statusEl.textContent = '正在扫描 ' + settingsStore.scanDirectories.length + ' 个目录…';
    try {
      var resp = await MEFinderApi.fetch('/api/scan-directories');
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '扫描失败');
      scanEntries = data.entries || [];
      renderScanResults(data);
    } catch (e) {
      document.getElementById('scan-results-head').style.display = 'none';
      document.getElementById('scan-results').replaceChildren();
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
    resultsEl.replaceChildren();
    var autoSelectable = groups.ready.concat(groups.ocr);
    var autoSelected = new Set(autoSelectable.slice(0, SCAN_IMPORT_BATCH_LIMIT));
    function section(title, indexes, checkable, checkedIndexes) {
      if (!indexes.length) return;
      resultsEl.appendChild(importNode('div', 'scan-group-title', title + '（' + indexes.length + '）'));
      indexes.forEach(function(i) {
        var entry = scanEntries[i];
        var row = importNode('div', 'scan-row' + (entry.status === 'imported' ? ' is-imported' : ''));
        if (checkable) {
          var checkbox = importNode('input', 'scan-check');
          checkbox.type = 'checkbox';
          checkbox.id = 'scan-check-' + i;
          checkbox.dataset.index = String(i);
          checkbox.checked = !!checkedIndexes && checkedIndexes.has(i);
          checkbox.dataset.actionChange = 'handleScanCheckChange';
          row.appendChild(checkbox);
        } else row.appendChild(importNode('span', 'scan-check-placeholder'));
        row.appendChild(importNode('span', 'type-badge ' + (entry.file_type === 'pdf' ? 'pdf' : 'word'),
          entry.file_type === 'pdf' ? 'PDF' : entry.file_type === 'epub' ? 'EPUB' : 'DOCX'));
        var name = importNode('label', 'scan-row-name', entry.name);
        if (checkable) name.htmlFor = 'scan-check-' + i;
        name.title = entry.path;
        row.appendChild(name);
        row.appendChild(importNode('span', 'scan-row-size', formatFileSize(entry.size_bytes)));
        var note = entry.status === 'processing' ? '已提交，正在导入…'
          : entry.status === 'name_conflict' ? '与已导入文献同名但大小不同，请重命名后再导入'
            : entry.needs_ocr === true ? '需 OCR'
              : entry.needs_ocr === null && entry.file_type === 'pdf' && entry.status === 'new'
                ? '未预检测，导入时自动判断；非原生文本将提交 MinerU' : '';
        if (note) row.appendChild(importNode('span', 'scan-row-note', note));
        resultsEl.appendChild(row);
      });
    }
    section('可直接导入的新文件', groups.ready, true, autoSelected);
    section('需 OCR 的新文件', groups.ocr, true, autoSelected);
    section('未预检测的新文件', groups.unknown, true, null);
    section('正在导入', groups.processing, false, null);
    section('同名冲突', groups.conflict, false, null);
    section('已导入', groups.imported, false, null);
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
  function beginDragSelectionMarquee(state, container, marqueeClass, event, dependencies) {
    var deps = dependencies || {document: document, window: window};
    state.started = true;
    state.marquee = deps.document.createElement('div');
    state.marquee.className = marqueeClass;
    deps.document.body.appendChild(state.marquee);
    container.classList.add('is-drag-selecting');
    try { container.setPointerCapture(event.pointerId); } catch (e) {}
    var selection = deps.window.getSelection && deps.window.getSelection();
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
    var state = libraryStore.dragSelection;
    if (!state || !state.started) return;
    var box = dragSelectionBox(state);
    paintDragSelectionMarquee(state, box);

    var hitIds = [];
    document.querySelectorAll('#library-list .library-entry[data-delete-selectable="1"]').forEach(function(entry) {
      var hit = dragSelectionHits(entry, box, state.scroller);
      entry.classList.toggle('is-drag-target', hit);
      if (hit) hitIds.push(entry.dataset.id);
    });

    libraryStore.deleteSelection = new Set(state.initial);
    hitIds.forEach(function(sourceId) {
      if (state.targetSelected) libraryStore.deleteSelection.add(sourceId);
      else libraryStore.deleteSelection.delete(sourceId);
    });
    global.MEFinder.library.syncDeleteSelectionUI();
  }

  function setupLibraryDragSelection() {
    var list = document.getElementById('library-list');
    if (!list || list.dataset.dragSelectionReady === '1') return;
    list.dataset.dragSelectionReady = '1';

    list.addEventListener('pointerdown', function(event) {
      // Drag-marquee extends an existing selection; start it with a checkbox
      // click so ordinary browsing (click to open a doc) is never hijacked.
      if (libraryStore.deleteSelection.size === 0 || event.button !== 0 || libraryStore.dragSelection) return;
      var entry = event.target.closest('.library-entry[data-delete-selectable="1"]');
      if (!entry || !list.contains(entry)) return;
      var scroller = libraryScrollContainer();
      if (!scroller) return;
      libraryStore.dragSelection = Object.assign({
        pointerId: event.pointerId,
        scroller: scroller,
        pointerX: event.clientX,
        pointerY: event.clientY,
        startX: event.clientX,
        startY: event.clientY,
        targetSelected: !libraryStore.deleteSelection.has(entry.dataset.id),
        initial: new Set(libraryStore.deleteSelection),
        active: true,
        started: false,
        marquee: null,
        autoScrollFrame: null
      }, dragSelectionAnchor(scroller, event));
    });

    list.addEventListener('click', function(event) {
      if (!libraryStore.suppressSelectionClick) return;
      libraryStore.suppressSelectionClick = false;
      event.preventDefault();
      event.stopPropagation();
    }, true);

    document.addEventListener('pointermove', function(event) {
      var state = libraryStore.dragSelection;
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
      var state = libraryStore.dragSelection;
      if (!state || event.pointerId !== state.pointerId) return;
      stopDragSelectionAutoScroll(state);
      libraryStore.dragSelection = null;
      endDragSelectionMarquee(state, list, '.library-entry', event);
      if (state.started) {
        libraryStore.suppressSelectionClick = true;
        setTimeout(function() { libraryStore.suppressSelectionClick = false; }, 0);
      }
      global.MEFinder.library.syncDeleteSelectionUI();
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
      var resp = await MEFinderApi.fetch('/api/import-local', {
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
          providerName: job.provider_id === 'mineru-local' ? '本地 MinerU'
            : localOCRProviderName(job.provider_id)
            || ((parserStore.visionConfig.providers || []).find(function(item) { return item.id === job.provider_id; }) || {}).name || null,
          message: '文件已复制，正在处理…'
        };
        if (job.file_type === 'pdf' && job.detected_pdf_type) {
          q.detectedType = job.detected_pdf_type;
          q.route = job.parse_route || (job.detected_pdf_type === 'native_text' ? 'native' : 'mineru');
          q.step = 2;
          q.message = '检测结果：' + pdfTypeLabel(job.detected_pdf_type)
            + (q.route === 'vision' ? '，将使用其他视觉 API'
              : q.route === 'local_ocr' ? '，将使用本地 OCR'
              : q.route === 'mineru' && q.providerId === 'mineru-local' ? '，将使用本地 MinerU'
              : q.route === 'mineru' ? '，将使用 MinerU 在线解析' : '，使用本地快速解析');
        } else if (job.file_type !== 'pdf') {
          q.step = 1;
        }
        importJobs.add(q);
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

  function normalizePdfParseMode(mode) {
    return ['auto','mineru','mineru-local','vision','general-local-model'].indexOf(mode) >= 0 ? mode : 'auto';
  }

  function renderPdfParseMode() {
    var zoteroLabel = document.getElementById('zotero-parse-mode');
    document.querySelectorAll('input[name="pdf-parse-mode"]').forEach(function(input) {
      input.checked = input.value === settingsStore.currentPdfParseMode;
      if (input.checked && zoteroLabel) {
        zoteroLabel.textContent = input.parentElement.querySelector('.pdf-parse-card-head strong').textContent;
      }
    });
  }

  async function setPdfParseMode(mode) {
    mode = normalizePdfParseMode(mode);
    if (settingsStore.pdfParseModeSaving || settingsStore.preferencesLoadPromise) {
      renderPdfParseMode();
      return;
    }
    var previousMode = settingsStore.currentPdfParseMode;
    settingsStore.currentPdfParseMode = mode;
    settingsStore.pdfParseModeSaving = true;
    renderPdfParseMode();
    try {
      var resp = await MEFinderApi.fetch('/api/preferences', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pdf_parse_mode: mode})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
      settingsStore.currentPdfParseMode = normalizePdfParseMode(data.pdf_parse_mode);
      settingsStore.preferencesLoaded = true;
      renderPdfParseMode();
    } catch (e) {
      settingsStore.currentPdfParseMode = previousMode;
      renderPdfParseMode();
      showToast('PDF 解析方式保存失败：' + e.message);
    } finally {
      settingsStore.pdfParseModeSaving = false;
    }
  }

  function selectedPdfParseMode() {
    var selected = document.querySelector('input[name="pdf-parse-mode"]:checked');
    return selected ? normalizePdfParseMode(selected.value) : 'auto';
  }

  function selectedVisionProviderId() {
    var container = document.getElementById('import-vision-provider');
    return selectedPdfParseMode() === 'vision' && container ? (container.dataset.value || '') : '';
  }

  function handleFileSelect(files) {
    if (!files || files.length === 0) return;
    var selectedFiles = Array.prototype.slice.call(files);
    var pdfParseMode = selectedPdfParseMode();
    var selectedProviderId = selectedVisionProviderId();
    var selectedProvider = (parserStore.visionConfig.providers || []).find(function(item) { return item.id === selectedProviderId; });
    selectedFiles.forEach(function(file, i) {
      var lowerName = file.name.toLowerCase();
      if (lowerName.startsWith('._')) return; // macOS metadata, not a document.
      var ext = file.name.toLowerCase().slice(file.name.lastIndexOf('.'));
      var isPackage = lowerName.endsWith('.mefinder') || lowerName.endsWith('.mefinder.zip');
      if (!isPackage && ['.pdf', '.docx', '.epub'].indexOf(ext) === -1) {
        showToast('不支持的格式: ' + file.name);
        return;
      }
      var id = 'import-' + Date.now() + '-' + i;
      importJobs.add({
        id: id,
        file: file,
        name: file.name,
        size: file.size,
        type: isPackage ? 'document_package' : ext.slice(1),
        importKind: isPackage ? 'document_package' : 'document',
        parseMode: !isPackage && ext === '.pdf' ? pdfParseMode : null,
        providerId: !isPackage && ext === '.pdf' && pdfParseMode === 'vision' ? selectedProviderId
          : !isPackage && ext === '.pdf' && pdfParseMode === 'mineru-local' ? 'mineru-local'
          : !isPackage && ext === '.pdf' && pdfParseMode === 'general-local-model' ? 'general-local-model' : null,
        providerName: !isPackage && ext === '.pdf' && pdfParseMode === 'vision' && selectedProvider ? selectedProvider.name
          : !isPackage && ext === '.pdf' && pdfParseMode === 'mineru-local' ? '本地 MinerU'
          : !isPackage && ext === '.pdf' && pdfParseMode === 'general-local-model' ? '通用本地模型' : null,
        status: 'queued',
        step: 0,
        message: isPackage ? '等待恢复文档包' : '等待处理'
      });
    });
    document.getElementById('file-input').value = '';
    renderImportQueue();
    importJobs.snapshot().filter(function(q) { return q.status === 'queued'; }).forEach(function(q) {
      uploadImport(q.id);
    });
  }

  function renderImportQueue() {
    var queueEl = document.getElementById('import-queue');
    var itemsEl = document.getElementById('import-items');
    if (importJobs.snapshot().length === 0) {
      queueEl.style.display = 'none';
      syncImportRecoveryPanel();
      syncResumeAllButton();
      return;
    }
    queueEl.style.display = 'block';
    syncImportRecoveryPanel();
    itemsEl.replaceChildren();
    importJobs.snapshot().forEach(function(q) {
      var typeCls = q.type === 'pdf' ? 'pdf' : q.type === 'document_package' ? 'package' : 'word';
      var typeLabel = q.type === 'pdf' ? 'PDF' : q.type === 'epub' ? 'EPUB' : q.type === 'document_package' ? '文档包' : 'DOCX';
      var retryProvider = visionRetryProviderFor(q);
      var steps = importStepsFor(q);
      var item = importNode('div', 'import-item');
      item.dataset.id = q.id;
      var header = importNode('div', 'import-item-header');
      header.appendChild(importNode('span', 'type-badge ' + typeCls, typeLabel));
      header.appendChild(importNode('span', 'import-item-name', q.name));
      if (q.type === 'pdf' && q.detectedType) {
        var routeClass = q.route === 'mineru' ? 'mineru' : q.route === 'vision' ? 'vision' : q.route === 'local_ocr' ? 'local-ocr' : 'native';
        var routeText = q.route === 'mineru' ? (q.providerId === 'mineru-local' ? '本地 MinerU' : '提交 MinerU')
          : q.route === 'vision' ? (q.providerName || '其他视觉 API')
            : q.route === 'local_ocr' ? (q.providerName || '本地 OCR') : '本地解析';
        header.appendChild(importNode('span', 'import-route-badge ' + routeClass, pdfTypeLabel(q.detectedType) + ' · ' + routeText));
      }
      header.appendChild(importNode('span', 'import-item-size', formatFileSize(q.size)));
      var remove = importNode('button', 'import-item-remove', '×');
      remove.dataset.action = 'removeImport';
      remove.dataset.id = q.id;
      remove.title = '移除';
      remove.disabled = !!q.cancelRequested && q.status === 'processing';
      header.appendChild(remove);
      item.appendChild(header);
      var stepList = importNode('div', 'import-steps');
      steps.forEach(function(label, i) {
        var cls = '';
        if (q.status === 'error' && i === q.step) cls = 'error';
        else if (i < q.step) cls = 'done';
        else if (i === q.step && q.status === 'processing') cls = 'active';
        var step = importNode('div', 'import-step ' + cls);
        step.appendChild(importNode('div', 'import-step-bar ' + cls));
        step.appendChild(importNode('span', 'import-step-label', label));
        stepList.appendChild(step);
      });
      item.appendChild(stepList);
      var statusCls = q.status === 'error' ? ' error' : q.status === 'done' ? ' done' : q.status === 'paused' ? ' paused' : '';
      item.appendChild(importNode('div', 'import-item-status' + statusCls, q.message));
      var retry = importNode('div', 'import-item-retry');
      var localMineruButton = q.status === 'error' && q.canRetryLocalMineru;
      if ((q.status === 'paused' || q.status === 'error') && q.canResume) {
        retry.appendChild(importAction(q.failureStage === 'index' ? '重新建立索引' : '继续导入', 'resumeImport', q.id, true));
        if (q.type === 'pdf' && q.route === 'vision' && q.failureStage !== 'index') {
          retry.appendChild(importAction('改用 MinerU（免费）', 'retryImportWithMinerU', q.id));
        }
        if (q.status === 'error' && retryProvider) {
          retry.appendChild(importAction('改用 ' + (retryProvider.name || '其他解析 API'), 'retryImportWithVision', q.id));
        }
        if (localMineruButton) retry.appendChild(importAction('切换到本地部署', 'retryImportWithLocalMinerU', q.id));
        retry.appendChild(importAction('解析设置', 'openVisionSettings'));
      } else if (q.status === 'error' && retryProvider) {
        retry.appendChild(importAction('改用 ' + (retryProvider.name || '其他解析 API'), 'retryImportWithVision', q.id, true));
        if (q.type === 'pdf' && q.route === 'vision') retry.appendChild(importAction('改用 MinerU（免费）', 'retryImportWithMinerU', q.id));
        if (localMineruButton) retry.appendChild(importAction('切换到本地部署', 'retryImportWithLocalMinerU', q.id));
        retry.appendChild(importAction('切换设置', 'openVisionSettings'));
      } else if (q.status === 'error'
          && (q.canRetryLocalMineru || q.canRetryVision || q.needsProviderConfig || q.mineruFailed || q.visionFailed)) {
        if (localMineruButton) retry.appendChild(importAction('切换到本地部署', 'retryImportWithLocalMinerU', q.id));
        retry.appendChild(importAction('配置其他解析 API', 'openVisionSettings'));
      }
      if (retry.childElementCount) item.appendChild(retry);
      itemsEl.appendChild(item);
    });
    syncResumeAllButton();
  }

  // 队列里可继续的任务多于一个时，才值得给一个「全部继续导入」的批量入口。
  function resumableImportQueue() {
    return importJobs.snapshot().filter(function(item) {
      return item.jobId && item.canResume
        && (item.status === 'paused' || item.status === 'error');
    });
  }

  function cancellableImportQueue() {
    return importJobs.snapshot().filter(function(item) {
      return item.status !== 'done' && !(item.cancelRequested && item.status === 'processing');
    });
  }

  function syncResumeAllButton() {
    var resumeButton = document.getElementById('import-resume-all-btn');
    var cancelButton = document.getElementById('import-cancel-all-btn');
    if (!resumeButton && !cancelButton) return;
    var resumeCount = resumableImportQueue().length;
    var cancelCount = cancellableImportQueue().length;
    if (resumeButton) {
      resumeButton.style.display = resumeCount > 1 ? 'inline-flex' : 'none';
      resumeButton.textContent = '全部继续导入（' + resumeCount + '）';
    }
    if (cancelButton) {
      cancelButton.style.display = cancelCount > 0 ? 'inline-flex' : 'none';
      cancelButton.textContent = '全部取消（' + cancelCount + '）';
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
    var pending = cancellableImportQueue();
    if (!pending.length) return;
    if (!await showAppConfirm(
      '将取消 ' + pending.length + ' 个未完成的导入任务。原始文件不会被删除',
      {title:'全部取消导入任务？', confirmText:'全部取消', tone:'danger'}
    )) return;
    var resumeButton = document.getElementById('import-resume-all-btn');
    var cancelButton = document.getElementById('import-cancel-all-btn');
    if (resumeButton) resumeButton.disabled = true;
    if (cancelButton) { cancelButton.disabled = true; cancelButton.textContent = '正在取消…'; }
    var failedCount = 0;
    for (var index = 0; index < pending.length; index += 1) {
      // 复用每条右上角 × 的持久化移除逻辑，串行处理以免并发改写任务日志。
      if (!await removeImport(pending[index].id, {
        silent: true,
        deferRender: true,
        skipConfirm: true
      })) {
        failedCount += 1;
      }
    }
    renderImportQueue();
    if (failedCount) {
      showToast('有 ' + failedCount + ' 个导入任务取消失败，请重试', 'warning');
    } else {
      showToast('已请求取消 ' + pending.length + ' 个导入任务', 'success');
    }
  }

  function removeImport(id, options) { return importJobs.remove(id, options); }
  function uploadImport(id) { return importJobs.upload(id); }
  function pollImportJob(id) { return importJobs.pollJob(id); }
  function loadResumableImports() { return importJobs.loadResumableImports(); }
  function loadActiveImports() { return importJobs.loadActiveImports(); }
  function resumeImport(id, options) { return importJobs.resume(id, options); }
  function retryImportWithVision(id) { return importJobs.retryWithVision(id); }
  function retryImportWithMinerU(id) { return importJobs.retryWithMinerU(id); }
  function retryImportWithLocalMinerU(id) { return importJobs.retryWithLocalMinerU(id); }

  importJobs.configure({
    changed: renderImportQueue,
    askConfirm: showAppConfirm,
    toast: showToast,
    retryProvider: visionRetryProviderFor,
    openSettings: function() { openVisionSettings(); },
    settled: function(sourceId) { delete calTransientStatus[sourceId]; },
    parsing: function(sourceId) { calTransientStatus[sourceId] = 'mapping'; },
    completed: function() {
      global.MEFinder.library.refreshAfterChange().catch(function(error) {
        showToast('文献数据刷新失败：' + error.message, 'danger');
      });
    }
  });

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      beginDragSelectionMarquee: beginDragSelectionMarquee,
      endDragSelectionMarquee: endDragSelectionMarquee
    };
  }

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.imports = {
    initDropZone: initDropZone,
    syncOnlineAutoMatchControl: function() { return global.MEFinder.importBatch.syncOnlineAutoMatchControl(); },
    setupLibraryDragSelection: setupLibraryDragSelection,
    setupScanResultDragSelection: setupScanResultDragSelection,
    normalizePdfParseMode: normalizePdfParseMode,
    renderPdfParseMode: renderPdfParseMode,
    renderQueue: renderImportQueue,
    pollJob: pollImportJob,
    loadResumableImports: loadResumableImports,
    loadActiveImports: loadActiveImports
  };

  MEFinderActions.register('handleScanCheckChange', function(event, target) {
    handleScanCheckChange(target);
  });
  MEFinderActions.register('openCnkiBatchRecord', function(event, target) {
    global.MEFinder.importBatch.openCnkiBatchRecord(Number(target.dataset.index));
  });
  MEFinderActions.register('selectCnkiBatchChoice', function(event, target) {
    global.resolveCnkiBatchChoice({action: 'select', index: Number(target.dataset.index)});
  });
  MEFinderActions.register('retryImportWithLocalMinerU', function(event, target) {
    retryImportWithLocalMinerU(target.dataset.id);
  });
  MEFinderActions.register('resumeImport', function(event, target) {
    resumeImport(target.dataset.id);
  });
  MEFinderActions.register('retryImportWithMinerU', function(event, target) {
    retryImportWithMinerU(target.dataset.id);
  });
  MEFinderActions.register('retryImportWithVision', function(event, target) {
    retryImportWithVision(target.dataset.id);
  });
  MEFinderActions.register('openVisionSettings', function() { openVisionSettings(); });
  MEFinderActions.register('removeImport', function(event, target) {
    removeImport(target.dataset.id);
  });

  // 浏览器公共面：动态内联处理器调用的命令入口。
  global.runBatchMetadataDetection = runBatchMetadataDetection;
  global.runDirectoryScan = runDirectoryScan;
  global.importSelectedScanned = importSelectedScanned;
  global.setPdfParseMode = setPdfParseMode;
  global.handleFileSelect = handleFileSelect;
  global.resumeAllImports = resumeAllImports;
  global.cancelAllImports = cancelAllImports;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
