/* Alignment model and compute runtime settings. */
(function (global) {  // module: 64-settings-model.js
  function renderAlignmentEmbeddingModel() {
    var modelId = settingsStore.currentAlignmentEmbeddingModel;
    document.querySelectorAll('.embedding-model-option').forEach(function(option) {
      var selected = option.dataset.embeddingModelChoice === modelId;
      option.classList.toggle('selected', selected);
      var input = option.querySelector('input[name="alignment-embedding-model"]');
      if (input) {
        input.checked = selected;
        input.disabled = settingsStore.alignmentEmbeddingModelSaving;
      }
    });
  }

  async function setAlignmentEmbeddingModel(modelId) {
    if (['minilm-l12-v2', 'multilingual-e5-large'].indexOf(modelId) < 0) return;
    if (settingsStore.alignmentEmbeddingModelSaving || settingsStore.preferencesLoadPromise) {
      renderAlignmentEmbeddingModel();
      return;
    }
    var previous = settingsStore.currentAlignmentEmbeddingModel;
    if (previous === modelId) return;
    settingsStore.currentAlignmentEmbeddingModel = modelId;
    settingsStore.alignmentEmbeddingModelSaving = true;
    renderAlignmentEmbeddingModel();
    try {
      var resp = await MEFinderApi.fetch('/api/preferences', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({alignment_embedding_model_id: modelId})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
      settingsStore.currentAlignmentEmbeddingModel = data.alignment_embedding_model_id;
      // 换模型后旧对齐标为「需重新对齐」：作品页状态要重新读取，不能沿用旧快照。
      if (global.MEFinder && global.MEFinder.works) {
        global.MEFinder.works.refreshAvailability();
        global.MEFinder.works.invalidate();
      }
      showToast('译本对齐模型已切换，已有对齐可在「译本对照」全部重新对齐');
    } catch (e) {
      settingsStore.currentAlignmentEmbeddingModel = previous;
      showToast('译本对齐模型保存失败：' + e.message, 'danger');
    } finally {
      settingsStore.alignmentEmbeddingModelSaving = false;
      renderAlignmentEmbeddingModel();
      renderAlignmentModelComponent(settingsStore.alignmentModelComponent);
    }
  }

  function renderAlignmentComputeStatus(compute) {
    // Honest availability line: an app that bundles the compute stack shows
    // "可用（随应用提供）", not "未安装" — the independent runtime is optional
    // until the main package is slimmed. Missing deps是唯一需要用户处理的状态。
    var status = document.getElementById('alignment-compute-status');
    if (!status) return;
    var provider = compute && compute.provider;
    var detail = (compute && compute.detail) || '';
    if (compute && compute.available) {
      // A mismatched-but-installed runtime carries a detail even while available
      // via the bundled stack; show it so the line matches the real launch path.
      status.className = 'settings-status' + (detail ? ' warning' : ' ready');
      var base = provider === 'independent' ? '可用 · 独立运行时' : '可用 · 随应用提供';
      status.textContent = detail ? (base + ' · ' + detail) : base;
    } else {
      status.className = 'settings-status warning';
      status.textContent = '不可用 · ' + (detail || '缺少计算依赖，请更新应用');
    }
  }

  function renderAlignmentModelComponent(component) {
    if (!component || !Array.isArray(component.models)) return;
    var installedSignature = component.models.filter(function(model) { return model.installed; })
      .map(function(model) { return model.id; }).join(',');
    if (global.MEFinder && global.MEFinder.works && settingsStore.alignmentModelInstalledSignature !== undefined
        && settingsStore.alignmentModelInstalledSignature !== installedSignature) {
      global.MEFinder.works.refreshAvailability();
    }
    settingsStore.alignmentModelInstalledSignature = installedSignature;
    settingsStore.alignmentModelComponent = component;
    if (component.compute && !settingsStore.alignmentRuntime) renderAlignmentComputeStatus(component.compute);
    var downloading = false;
    component.models.forEach(function(model) {
      var button = document.getElementById('embedding-model-download-' + model.id);
      var state = document.getElementById('embedding-model-state-' + model.id);
      var hint = document.getElementById('embedding-model-hint-' + model.id);
      var progress = document.getElementById('embedding-model-progress-' + model.id);
      if (!button || !state || !hint || !progress) return;
      var progressFill = progress.querySelector('span');
      button.title = model.error || '';
      if (model.state === 'downloading') {
        var transfer = alignmentModelDownloadProgress(model);
        downloading = true;
        state.className = 'settings-status';
        state.textContent = transfer ? '下载中 ' + transfer.percent + '%' : '下载中';
        hint.textContent = transfer ? transfer.text : (model.message || '正在下载模型…');
        progress.hidden = false;
        progress.classList.toggle('indeterminate', !transfer);
        if (progressFill) progressFill.style.width = transfer ? (transfer.ratio * 100) + '%' : '0%';
        progress.setAttribute('aria-valuemin', '0');
        progress.setAttribute('aria-valuemax', '100');
        if (transfer) {
          progress.setAttribute('aria-valuenow', String(transfer.percent));
          progress.setAttribute('aria-valuetext', transfer.text);
        } else {
          progress.removeAttribute('aria-valuenow');
          progress.removeAttribute('aria-valuetext');
        }
        button.hidden = false;
        button.disabled = true;
        button.textContent = '正在下载…';
      } else if (model.state === 'verifying') {
        // 字节已齐、安装回执未落：校验/落盘阶段。此时若仍显示"正在下载 99%"，
        // 用户无法区分"快好了"与"卡死"（2026-09-13 E5 卡 99% 反馈）。
        downloading = true;
        state.className = 'settings-status';
        state.textContent = '校验中';
        hint.textContent = '模型文件已就绪，正在校验并写入安装记录…';
        progress.hidden = false;
        progress.classList.remove('indeterminate');
        if (progressFill) progressFill.style.width = '99%';
        progress.setAttribute('aria-valuemin', '0');
        progress.setAttribute('aria-valuemax', '100');
        progress.setAttribute('aria-valuenow', '99');
        progress.setAttribute('aria-valuetext', '校验中');
        button.hidden = false;
        button.disabled = true;
        button.textContent = '校验中…';
      } else if (model.installed) {
        state.className = 'settings-status ready';
        state.textContent = '已下载';
        hint.textContent = '模型保存在 MEFinder 组件目录，可离线使用';
        progress.hidden = true;
        progress.classList.remove('indeterminate');
        if (progressFill) progressFill.style.width = '100%';
        button.hidden = false;
        button.disabled = false;
        button.textContent = '删除模型';
        button.classList.add('danger');
        button.onclick = function() { deleteAlignmentModel(model.id, button); };
      } else {
        state.className = 'settings-status' + (model.state === 'failed' ? ' warning' : '');
        state.textContent = model.state === 'failed' ? '下载失败' : '未下载';
        hint.textContent = model.error ? '上次下载失败：' + model.error : '首次使用前需下载，文件只保存在本机';
        progress.hidden = true;
        progress.classList.remove('indeterminate');
        if (progressFill) progressFill.style.width = '0%';
        button.hidden = false;
        button.disabled = false;
        button.textContent = model.state === 'failed' ? '重试下载' : '下载安装';
        button.classList.remove('danger');
        button.onclick = function() { downloadAlignmentModel(model.id, button); };
      }
    });
    var selected = component.models.find(function(model) {
      return model.id === settingsStore.currentAlignmentEmbeddingModel;
    });
    var status = document.getElementById('alignment-model-status');
    if (status && selected) {
      // 当前用哪个模型由选中的单选行表达，标题右侧不再重复模型名
      // （DESIGN.md §5：避免徽章、选中底色、单选圆点多重重复强调）。
      var selectedTransfer = alignmentModelDownloadProgress(selected);
      var installedCount = component.models.filter(function(model) { return model.installed; }).length;
      status.className = 'settings-status' + (selected.installed ? ' ready' : (selected.state === 'failed' ? ' warning' : ''));
      if (selected.state === 'downloading') {
        status.textContent = '下载中' + (selectedTransfer ? ' ' + selectedTransfer.percent + '%' : '');
      } else if (selected.state === 'verifying') {
        status.textContent = '校验中';
      } else if (selected.installed) {
        status.textContent = '当前模型已下载';
      } else {
        status.textContent = '当前模型未下载 · 已下载 ' + installedCount + ' / ' + component.models.length;
      }
    }
    if (settingsStore.alignmentModelPollTimer) {
      clearTimeout(settingsStore.alignmentModelPollTimer);
      settingsStore.alignmentModelPollTimer = null;
    }
    if (downloading) {
      settingsStore.alignmentModelPollTimer = setTimeout(loadAlignmentModelComponent, 1000);
    }
  }

  async function loadAlignmentModelComponent() {
    var revision = (settingsStore.alignmentModelLoadRevision || 0) + 1;
    settingsStore.alignmentModelLoadRevision = revision;
    try {
      var resp = await MEFinderApi.fetch('/api/text-alignment/models');
      var data = await resp.json();
      if (revision !== settingsStore.alignmentModelLoadRevision) return;
      if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
      renderAlignmentModelComponent(data);
    } catch (e) {
      if (revision !== settingsStore.alignmentModelLoadRevision) return;
      var status = document.getElementById('alignment-model-status');
      if (status) {
        status.className = 'settings-status warning';
        status.textContent = '读取失败';
      }
    }
  }

  async function downloadAlignmentModel(modelId, button) {
    if (button) {
      button.disabled = true;
      button.textContent = '正在启动…';
    }
    try {
      var resp = await MEFinderApi.fetch('/api/text-alignment/models', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model_id: modelId, action: 'download'})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '下载启动失败');
      renderAlignmentModelComponent(data);
    } catch (e) {
      showToast('译本对齐模型下载失败：' + e.message, 'danger');
      loadAlignmentModelComponent();
    }
  }

  async function deleteAlignmentModel(modelId, button) {
    var component = settingsStore.alignmentModelComponent || {};
    var model = (component.models || []).find(function(item) { return item.id === modelId; });
    var name = (model && model.display_name) || '该模型';
    var isCurrent = modelId === settingsStore.currentAlignmentEmbeddingModel;
    var consequence = isCurrent
      ? '这是当前使用的对齐模型，删除后需要重新下载才能继续运行对齐。已有对照不受影响。'
      : '删除后需要重新下载才能用它运行对齐。已有对照不受影响。';
    if (!await showAppConfirm('将删除「' + name + '」的本地模型文件。' + consequence, {
      title: '删除模型文件',
      confirmText: '删除',
      tone: 'danger'
    })) return;
    if (button) { button.disabled = true; button.textContent = '删除中…'; }
    try {
      var resp = await MEFinderApi.fetch('/api/text-alignment/models', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model_id: modelId, action: 'delete'})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '删除失败');
      renderAlignmentModelComponent(data);
      // 后端回报实际释放的字节数，不用目录里的估算值冒充
      showToast('已删除「' + name + '」的模型文件' + (data.freed_bytes
        ? '，释放 ' + formatFileSize(data.freed_bytes) : ''));
    } catch (e) {
      showToast('删除模型文件失败：' + e.message, 'danger');
      loadAlignmentModelComponent();
    }
  }

  function renderAlignmentRuntimeComponent(runtime) {
    var card = document.getElementById('alignment-runtime-component');
    if (!card || !runtime) return;
    var previous = settingsStore.alignmentRuntime;
    settingsStore.alignmentRuntime = runtime;
    var compute = runtime.compute || {};
    renderAlignmentComputeStatus(compute);
    var busyStates = {provisioning: 1, validating: 1, cleaning: 1, uninstall_pending: 1, upgrade_pending: 1};
    var busy = !!busyStates[runtime.state];
    if (global.MEFinder && global.MEFinder.works && (!previous || previous.state !== runtime.state || previous.installed !== runtime.installed)) {
      global.MEFinder.works.refreshAvailability();
    }
    if (!busy && previous && (previous.state !== runtime.state
        || previous.installed !== runtime.installed || previous.operation)) {
      loadAlignmentModelComponent();
    }
    // 自带栈的老用户(provider==='builtin' 且未装独立运行时)无需安装，卡片保持隐藏、
    // 不打扰——维持 2B 的产品判断；精简包缺栈(provider==='none')或已装独立运行时时才
    // 呈现管理入口。
    var show = !!runtime.supported && (runtime.installed || busy || compute.provider === 'none');
    card.hidden = !show;
    var note = document.getElementById('alignment-compute-note');
    if (note) {
      if (compute.available && compute.provider === 'builtin') {
        note.textContent = '对齐计算运行时随应用提供，无需单独安装即可离线生成对齐';
      } else if (compute.available) {
        note.textContent = '对齐计算运行时已独立安装在本机，离线生成对齐';
      } else if (!runtime.supported) {
        note.textContent = '当前平台或组件清单不支持安装对齐计算运行时；搜索、阅读和已有对齐成果不受影响';
      } else if (compute.detail) {
        note.textContent = compute.detail;
      } else {
        note.textContent = '需安装独立计算运行时后才能生成对齐；搜索、阅读和已有对齐成果不受影响';
      }
    }
    if (!show) {
      if (settingsStore.alignmentRuntimePollTimer) {
        clearTimeout(settingsStore.alignmentRuntimePollTimer);
        settingsStore.alignmentRuntimePollTimer = null;
      }
      return;
    }
    var stateEl = document.getElementById('alignment-runtime-state');
    var hintEl = document.getElementById('alignment-runtime-hint');
    var progress = document.getElementById('alignment-runtime-progress');
    var progressFill = progress ? progress.querySelector('span') : null;
    var actionBtn = document.getElementById('alignment-runtime-action');
    var uninstallBtn = document.getElementById('alignment-runtime-uninstall');
    var cancelBtn = document.getElementById('alignment-runtime-cancel');
    if (!stateEl || !hintEl || !progress || !actionBtn || !uninstallBtn || !cancelBtn) return;
    // 每次渲染先收起可选控件，再按状态点亮，避免上一次状态的按钮残留。
    actionBtn.hidden = true; actionBtn.disabled = false; actionBtn.classList.remove('danger');
    uninstallBtn.hidden = true; uninstallBtn.disabled = false; uninstallBtn.classList.remove('danger');
    cancelBtn.hidden = true; cancelBtn.disabled = false;
    progress.hidden = true; progress.classList.remove('indeterminate');
    actionBtn.onclick = null; uninstallBtn.onclick = null; cancelBtn.onclick = null;

    function showProgress(indeterminate) {
      progress.hidden = false;
      progress.classList.toggle('indeterminate', !!indeterminate);
      var pct = 0;
      if (!indeterminate && runtime.total_bytes) {
        pct = Math.max(0, Math.min(100, Math.round(runtime.downloaded_bytes / runtime.total_bytes * 100)));
      }
      if (progressFill) progressFill.style.width = indeterminate ? '0%' : pct + '%';
      progress.setAttribute('aria-valuemin', '0');
      progress.setAttribute('aria-valuemax', '100');
      if (indeterminate) { progress.removeAttribute('aria-valuenow'); }
      else { progress.setAttribute('aria-valuenow', String(pct)); }
      return pct;
    }

    if (busy) {
      cancelBtn.hidden = false;
      cancelBtn.onclick = function() { manageAlignmentRuntime('cancel', cancelBtn); };
      stateEl.className = 'settings-status';
      if (runtime.state === 'provisioning') {
        var pct = showProgress(!runtime.total_bytes);
        var verb = runtime.operation === 'update' ? '升级中' : '安装中';
        stateEl.textContent = verb + (runtime.total_bytes ? ' ' + pct + '%' : '');
        hintEl.textContent = runtime.message || '正在准备独立计算环境…';
      } else if (runtime.state === 'validating') {
        showProgress(true);
        stateEl.textContent = '验证中';
        hintEl.textContent = runtime.message || '正在验证独立运行时…';
      } else if (runtime.state === 'cleaning') {
        showProgress(true);
        stateEl.textContent = '卸载中';
        hintEl.textContent = runtime.message || '正在移除运行时与所属模型…';
      } else if (runtime.state === 'upgrade_pending') {
        showProgress(true);
        stateEl.textContent = '等待升级';
        hintEl.textContent = runtime.message || '当前有对齐任务在运行，任务结束后自动升级';
      } else {
        showProgress(true);
        stateEl.textContent = '等待卸载';
        hintEl.textContent = runtime.message || '当前有对齐任务在运行，任务结束后自动卸载';
      }
    } else if (runtime.installed) {
      stateEl.className = 'settings-status' + (compute.available ? ' ready' : ' warning');
      stateEl.textContent = !compute.available ? '已安装 · 不可用'
        : (runtime.installed_version ? '已安装 · v' + runtime.installed_version : '已安装');
      uninstallBtn.hidden = false;
      uninstallBtn.classList.add('danger');
      uninstallBtn.textContent = '卸载';
      uninstallBtn.onclick = function() { uninstallAlignmentRuntime(uninstallBtn); };
      if (runtime.update_available) {
        actionBtn.hidden = false;
        actionBtn.textContent = '升级';
        actionBtn.onclick = function() { manageAlignmentRuntime('update', actionBtn); };
        hintEl.textContent = '有新版本可升级；升级会等待当前对齐任务结束';
      } else if (runtime.error) {
        hintEl.textContent = '上次操作失败：' + runtime.error;
      } else if (!compute.available) {
        hintEl.textContent = compute.detail || '运行时不可用，请卸载后重新安装';
      } else {
        hintEl.textContent = '运行时已就绪，可离线生成对齐';
      }
    } else {
      var failed = !!runtime.error;
      stateEl.className = 'settings-status' + (failed ? ' warning' : '');
      stateEl.textContent = failed ? '安装失败' : '未安装';
      hintEl.textContent = failed
        ? '上次安装失败：' + runtime.error
        : '首次生成对齐前需安装，运行时只保存在本机';
      actionBtn.hidden = false;
      actionBtn.textContent = failed ? '重试安装' : '安装';
      actionBtn.onclick = function() { manageAlignmentRuntime('install', actionBtn); };
    }

    if (settingsStore.alignmentRuntimePollTimer) {
      clearTimeout(settingsStore.alignmentRuntimePollTimer);
      settingsStore.alignmentRuntimePollTimer = null;
    }
    if (busy) {
      settingsStore.alignmentRuntimePollTimer = setTimeout(loadAlignmentRuntime, 1000);
    }
  }

  async function loadAlignmentRuntime() {
    var revision = (settingsStore.alignmentRuntimeLoadRevision || 0) + 1;
    settingsStore.alignmentRuntimeLoadRevision = revision;
    try {
      var resp = await MEFinderApi.fetch('/api/text-alignment/runtime', {cache: 'no-store'});
      var data = await resp.json();
      if (revision !== settingsStore.alignmentRuntimeLoadRevision) return;
      // 成功响应就是 summary，其顶层 error 是「上次操作失败」的业务字段（渲染时呈现），
      // 不是请求失败；只有 HTTP 非 2xx（后端 400/500）才算读取失败。
      if (!resp.ok) throw new Error(data.error || '读取失败');
      renderAlignmentRuntimeComponent(data);
    } catch (e) {
      if (revision !== settingsStore.alignmentRuntimeLoadRevision) return;
      var card = document.getElementById('alignment-runtime-component');
      if (card) {
        card.hidden = false;
        var stateEl = document.getElementById('alignment-runtime-state');
        if (stateEl) { stateEl.className = 'settings-status warning'; stateEl.textContent = '读取失败'; }
        document.getElementById('alignment-runtime-hint').textContent = '无法读取计算组件状态：' + e.message;
        document.getElementById('alignment-compute-note').textContent = '计算组件状态未能刷新，请重试';
        document.getElementById('alignment-runtime-progress').hidden = true;
        document.getElementById('alignment-runtime-uninstall').hidden = true;
        document.getElementById('alignment-runtime-cancel').hidden = true;
        var retry = document.getElementById('alignment-runtime-action');
        retry.hidden = false;
        retry.disabled = false;
        retry.textContent = '重新读取';
        retry.onclick = loadAlignmentRuntime;
      }
      if (settingsStore.alignmentRuntimePollTimer) {
        clearTimeout(settingsStore.alignmentRuntimePollTimer);
        settingsStore.alignmentRuntimePollTimer = null;
      }
    }
  }

  async function manageAlignmentRuntime(action, button) {
    var revision = (settingsStore.alignmentRuntimeLoadRevision || 0) + 1;
    settingsStore.alignmentRuntimeLoadRevision = revision;
    clearTimeout(settingsStore.alignmentRuntimePollTimer);
    settingsStore.alignmentRuntimePollTimer = null;
    if (button) { button.disabled = true; }
    try {
      var resp = await MEFinderApi.fetch('/api/text-alignment/runtime', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: action})
      });
      var data = await resp.json();
      if (revision !== settingsStore.alignmentRuntimeLoadRevision) return;
      // 成功响应是 {ok, ...summary}；summary 顶层 error 是业务字段（上次操作失败），
      // 由渲染呈现，不当作请求失败。只有 HTTP 非 2xx 才是操作失败。
      if (!resp.ok) throw new Error(data.error || '操作失败');
      renderAlignmentRuntimeComponent(data);
    } catch (e) {
      if (revision !== settingsStore.alignmentRuntimeLoadRevision) return;
      var labels = {install: '安装', update: '升级', uninstall: '卸载', cancel: '取消'};
      showToast('对齐计算组件' + (labels[action] || '操作') + '失败：' + e.message, 'danger');
      loadAlignmentRuntime();
    }
  }

  async function uninstallAlignmentRuntime(button) {
    // 已确认的产品规则:卸载删除组件所属模型，但保留文献、已有对齐成果与人工修正。
    if (!await showAppConfirm(
      '将删除独立计算运行时及其所属的对齐模型文件。文献、已有对齐成果与人工修正都会保留；'
        + '重新生成新的对齐前需要再次安装运行时与模型。',
      {title: '卸载对齐计算组件', confirmText: '卸载', tone: 'danger'}
    )) return;
    manageAlignmentRuntime('uninstall', button);
  }

  global.renderAlignmentEmbeddingModel = renderAlignmentEmbeddingModel;
  global.renderAlignmentModelComponent = renderAlignmentModelComponent;
  global.loadAlignmentModelComponent = loadAlignmentModelComponent;
  global.loadAlignmentRuntime = loadAlignmentRuntime;
  global.setAlignmentEmbeddingModel = setAlignmentEmbeddingModel;
  global.downloadAlignmentModel = downloadAlignmentModel;
  global.deleteAlignmentModel = deleteAlignmentModel;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
