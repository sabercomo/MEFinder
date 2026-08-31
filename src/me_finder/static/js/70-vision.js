/* IIFE 包裹：私有化本文件实现，仅下方显式列出的公共面挂到全局。
   —— #7 前端全局作用域收敛（模式同 reader.js / 05-theme-engine.js）。
   IIFE 实参在 node 下退回 globalThis。 */
(function (global) {  // module: 70-vision.js
  /* ═══ MinerU API settings ═══ */
  function localOCREngineFields(providerId) {
    var prefix = providerId === 'ndlocr-lite' ? 'local-ocr-modern' : 'local-ocr-ancient';
    return {
      section: document.querySelector('[data-local-ocr-engine="' + providerId + '"]'),
      enabled: document.getElementById(prefix + '-enabled'),
      python: document.getElementById(prefix + '-python'),
      script: document.getElementById(prefix + '-script'),
      hint: document.getElementById(prefix + '-hint'),
      managedState: document.getElementById(prefix + '-managed-state'),
      installHint: document.getElementById(prefix + '-install-hint'),
      progress: document.getElementById(prefix + '-progress'),
      install: document.getElementById(prefix + '-install'),
      validate: document.getElementById(prefix + '-validate'),
      uninstall: document.getElementById(prefix + '-uninstall'),
      cancel: document.getElementById(prefix + '-cancel')
    };
  }

  function localOCRByteSize(value) {
    var bytes = Number(value) || 0;
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
    if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function localOCREstimatedWait(value) {
    var seconds = Math.max(0, Number(value) || 0);
    if (!seconds) return '即将完成';
    if (seconds < 60) return '预计剩余约 ' + Math.max(10, Math.ceil(seconds / 10) * 10) + ' 秒';
    if (seconds < 3600) return '预计剩余约 ' + Math.ceil(seconds / 60) + ' 分钟';
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.ceil((seconds % 3600) / 60);
    return '预计剩余约 ' + hours + ' 小时' + (minutes ? ' ' + minutes + ' 分钟' : '');
  }

  function localOCRTransferSummary(managed) {
    if (!managed.total_bytes) return '';
    var summary = localOCRByteSize(managed.downloaded_bytes) + ' / ' + localOCRByteSize(managed.total_bytes);
    if (managed.downloaded_bytes >= managed.total_bytes) return summary + ' · 即将完成';
    if (!managed.download_speed_bps || managed.eta_seconds == null) return summary + ' · 正在检测网速…';
    return summary + ' · ' + localOCRByteSize(managed.download_speed_bps) + '/s · ' + localOCREstimatedWait(managed.eta_seconds);
  }

  function renderLocalOCRInstaller(config) {
    var installer = config.installer || {supported:false, engines:[]};
    var managedEngines = {};
    (installer.engines || []).forEach(function(engine) {
      managedEngines[engine.provider_id] = engine;
    });
    var active = false;
    (config.engines || []).forEach(function(engine) {
      var fields = localOCREngineFields(engine.provider_id);
      var managed = managedEngines[engine.provider_id] || {state:'not_installed', managed:false};
      var busy = ['downloading','verifying','extracting','provisioning','validating','cleaning'].indexOf(managed.state) >= 0;
      active = active || busy;
      var installed = !!managed.managed;
      var labels = {
        downloading:'下载中', verifying:'校验中', extracting:'解压中',
        provisioning:'配置环境中', validating:'验证中', cleaning:'清理中'
      };
      if (fields.managedState) {
        fields.managedState.className = 'settings-status ' + (installed ? 'ready' : 'warning');
        fields.managedState.textContent = labels[managed.state] || (managed.update_available ? '可更新 · tag ' + managed.tag : installed ? '已安装 · tag ' + managed.tag : '未安装');
      }
      if (fields.installHint) {
        var detail = managed.error ? '上次操作失败：' + managed.error : (managed.message || '');
        if (busy && managed.total_bytes) {
          detail += (detail ? ' · ' : '') + localOCRTransferSummary(managed);
        } else if (busy && managed.state === 'provisioning') {
          detail += (detail ? ' · ' : '') + '预计时间：正在估算…';
        }
        fields.installHint.textContent = detail || (installed ? '运行时和模型都装在 MEFinder 组件目录，不改动系统 Python 环境' : '安装时会一并下载模型，首次耗时取决于网络');
      }
      if (fields.progress) {
        fields.progress.hidden = !busy;
        var bar = fields.progress.firstElementChild;
        if (bar) bar.style.width = managed.progress == null ? '18%' : Math.round(managed.progress * 100) + '%';
        fields.progress.classList.toggle('indeterminate', busy && managed.progress == null);
      }
      if (fields.install) {
        fields.install.hidden = (installed && !managed.update_available) || busy;
        fields.install.disabled = !installer.supported;
        fields.install.textContent = managed.update_available ? '更新组件' : '下载安装';
        fields.install.onclick = function() {
          manageLocalOCRComponent(engine.provider_id, managed.update_available ? 'update' : 'install', fields.install);
        };
      }
      if (fields.validate) fields.validate.hidden = !installed || busy;
      if (fields.uninstall) fields.uninstall.hidden = !installed || busy;
      if (fields.cancel) fields.cancel.hidden = !busy;
      if (fields.enabled) fields.enabled.disabled = busy;
      if (fields.python) fields.python.disabled = busy;
      if (fields.script) fields.script.disabled = busy;
    });
    if (!installer.supported) {
      var status = document.getElementById('local-ocr-status');
      if (status) status.title = '当前平台不在安装矩阵中：' + (installer.platform || '未知');
    }
    var catalog = installer.catalog || {};
    var catalogStatus = document.getElementById('local-ocr-status');
    if (catalogStatus && catalog.last_checked_at) {
      catalogStatus.title = catalog.last_error
        ? '组件更新检查失败：' + catalog.last_error
        : '组件清单已自动检查；24 小时内不会重复请求';
    }
    if (parserStore.localOCRPollTimer) clearTimeout(parserStore.localOCRPollTimer);
    parserStore.localOCRPollTimer = active ? setTimeout(loadLocalOCRConfig, 700) : null;
  }

  function renderLocalOCRConfig(config) {
    parserStore.localOCRConfig = config;
    (config.engines || []).forEach(function(engine) {
      var fields = localOCREngineFields(engine.provider_id);
      if (fields.enabled) fields.enabled.checked = !!engine.enabled;
      if (fields.python) fields.python.value = engine.python_path || '';
      if (fields.script) fields.script.value = engine.script_path || '';
      if (fields.hint) fields.hint.textContent = engine.configured ? '入口已配置' : '';
    });
    renderLocalOCRInstaller(config);
    var available = (config.engines || []).filter(function(engine) {
      return engine.enabled && engine.configured;
    }).length;
    var status = document.getElementById('local-ocr-status');
    if (status) {
      status.className = 'settings-status ' + (available ? 'ready' : 'warning');
      status.textContent = available ? '可用 ' + available + ' 个组件' : '尚未启用';
    }
  }

  async function loadLocalOCRConfig() {
    var status = document.getElementById('local-ocr-status');
    if (status && !parserStore.localOCRConfig) { status.className = 'settings-status'; status.textContent = '读取中…'; }
    try {
      var response = await fetch('/api/local-ocr');
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '读取失败');
      renderLocalOCRConfig(data);
    } catch (error) {
      if (status) { status.className = 'settings-status warning'; status.textContent = '读取失败'; }
      showToast('读取本地 OCR 设置失败：' + error.message, 'danger');
    }
    loadGeneralModelConfig();
  }

  function localOCRPayload() {
    var modern = localOCREngineFields('ndlocr-lite');
    var ancient = localOCREngineFields('ndlkotenocr-lite');
    var current = parserStore.localOCRConfig || {};
    var currentEngines = {};
    (current.engines || []).forEach(function(engine) {
      currentEngines[engine.provider_id] = engine;
    });
    return {
      render_dpi: current.render_dpi || 200,
      probe_pages: current.probe_pages || 3,
      pages_per_slice: current.pages_per_slice || 10,
      timeout_seconds_per_page: current.timeout_seconds_per_page || 300,
      blank_ink_ratio: current.blank_ink_ratio == null ? 0.001 : current.blank_ink_ratio,
      engines: {
        'ndlocr-lite': {
          enabled: !!modern.enabled.checked,
          python_path: modern.python.value.trim(),
          script_path: modern.script.value.trim(),
          weights_sha256: (currentEngines['ndlocr-lite'] || {}).weights_sha256 || ''
        },
        'ndlkotenocr-lite': {
          enabled: !!ancient.enabled.checked,
          python_path: ancient.python.value.trim(),
          script_path: ancient.script.value.trim(),
          weights_sha256: (currentEngines['ndlkotenocr-lite'] || {}).weights_sha256 || ''
        }
      }
    };
  }

  async function saveLocalOCRConfig() {
    var button = document.getElementById('local-ocr-save');
    var hint = document.getElementById('local-ocr-save-hint');
    button.disabled = true;
    button.textContent = '保存中…';
    if (hint) hint.textContent = '';
    try {
      var response = await fetch('/api/local-ocr', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(localOCRPayload())
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '保存失败');
      renderLocalOCRConfig(data);
      if (hint) hint.textContent = data.available ? '已保存；扫描类 PDF 将优先使用本地 OCR' : '已保存；当前不会改变导入路由';
    } catch (error) {
      if (hint) hint.textContent = '未保存：' + error.message;
    } finally {
      button.disabled = false;
      button.textContent = '保存设置';
    }
  }

  async function manageLocalOCRComponent(providerId, action, button) {
    if (action === 'uninstall' && !await showAppConfirm(
      '将删除该组件的模型、独立 Python 环境和自动填入的路径。',
      {title:'卸载本地 OCR？', tone:'warning', confirmText:'卸载'}
    )) return;
    if (button) button.disabled = true;
    try {
      var response = await fetch('/api/local-ocr/component', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({provider_id:providerId, action:action})
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '操作失败');
      await loadLocalOCRConfig();
    } catch (error) {
      showToast('本地 OCR 组件操作失败：' + error.message, 'danger');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function testLocalOCREngine(providerId, button) {
    var payload = localOCRPayload().engines[providerId];
    var fields = localOCREngineFields(providerId);
    button.disabled = true;
    button.textContent = '测试中…';
    if (fields.hint) fields.hint.textContent = '正在启动 CLI…';
    try {
      var response = await fetch('/api/local-ocr/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider_id: providerId, python_path: payload.python_path, script_path: payload.script_path})
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '启动失败');
      if (fields.hint) fields.hint.textContent = '启动成功 · ' + data.latency_ms + ' ms';
    } catch (error) {
      if (fields.hint) fields.hint.textContent = '启动失败：' + error.message;
    } finally {
      button.disabled = false;
      button.textContent = '测试手动入口';
    }
  }

  async function loadMineruConfig() {
    var status = document.getElementById('mineru-config-status');
    if (!status) return;
    status.className = 'settings-status';
    status.textContent = '读取中…';
    try {
      var resp = await fetch('/api/mineru-accounts');
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
      parserStore.mineruAccounts = Array.isArray(data.accounts) ? data.accounts : [];
      parserStore.mineruAccounts.sort(function(left, right) {
        return String(left.display_name || left.account_id || '').localeCompare(
          String(right.display_name || right.account_id || ''),
          'zh-CN',
          {numeric:true, sensitivity:'base'}
        );
      });
      parserStore.mineruStatistics = data.statistics || {parsed_book_count:0, parsed_page_count:0, credentials:[]};
      document.getElementById('mineru-api-base').value = data.api_base || 'https://mineru.net';
      renderMineruLocalSettings(data.local_deployment || {});
      renderMineruAccountList();
      var addButton = document.getElementById('mineru-add-account');
      if (addButton) addButton.hidden = !parserStore.mineruAccounts.length;
      if (!parserStore.mineruAccounts.length) startAddMineruAccount(false);
      var enabledCount = parserStore.mineruAccounts.filter(function(item) { return item.enabled && item.configured; }).length;
      if (enabledCount) {
        status.className = 'settings-status ready';
        status.textContent = '已配置 ' + enabledCount + ' 个可用账号';
      } else {
        status.className = 'settings-status warning';
        status.textContent = parserStore.mineruAccounts.length ? '账号均未启用' : '尚未添加账号';
      }
      parserStore.mineruConfigLoaded = true;
    } catch (e) {
      status.className = 'settings-status warning';
      status.textContent = '读取失败';
      showToast('读取 MinerU 配置失败：' + e.message);
    }
  }

  function renderMineruLocalSettings(config) {
    parserStore.mineruLocalConfig = config;
    var endpoint = document.getElementById('mineru-local-endpoint');
    var backend = document.getElementById('mineru-local-backend');
    var enabled = document.getElementById('mineru-local-enabled');
    if (endpoint) endpoint.value = config.endpoint || 'http://127.0.0.1:8000';
    if (backend) backend.value = config.backend || 'pipeline';
    if (enabled) enabled.checked = !!config.enabled;
    renderManagedMineru(config.managed_runtime || {});
    syncMineruLocalImportOption(!!config.enabled);
    var label = managedMineruSummaryLabel(config, config.managed_runtime || {});
    updateMineruLocalStatus(!!config.enabled, label);
  }

  function managedMineruSummaryLabel(config, runtime) {
    if (!config.managed) return config.enabled ? '自部署已配置' : '';
    var service = (runtime || {}).service || {};
    if (!service.running) return '已配置，未启动';
    var profile = service.profile || config.managed_profile;
    return (profile === 'vlm' ? 'VLM' : 'Pipeline') + ' 运行中';
  }

  function managedMineruFields(profileId) {
    var prefix = 'managed-mineru-' + profileId;
    return {
      state: document.getElementById(prefix + '-state'),
      install: document.getElementById(prefix + '-install'),
      start: document.getElementById(prefix + '-start'),
      stop: document.getElementById(prefix + '-stop'),
      uninstall: document.getElementById(prefix + '-uninstall'),
      cancel: document.getElementById(prefix + '-cancel'),
      progressHint: document.getElementById(prefix + '-progress'),
      progress: document.getElementById(prefix + '-progress-bar')
    };
  }

  function managedMineruTransferSummary(profile) {
    if (!profile.total_bytes) return '';
    var total = (profile.total_is_estimate ? '约 ' : '') + localOCRByteSize(profile.total_bytes);
    var summary = '已下载 ' + localOCRByteSize(profile.downloaded_bytes) + ' / ' + total;
    if (profile.downloaded_bytes >= profile.total_bytes) return summary + ' · 即将完成';
    if (!profile.download_speed_bps || profile.eta_seconds == null) {
      return summary + (profile.downloaded_bytes ? ' · 网络波动或正在处理分片…' : ' · 正在检测网速…');
    }
    return summary + ' · ' + localOCRByteSize(profile.download_speed_bps) + '/s · ' + localOCREstimatedWait(profile.eta_seconds);
  }

  function managedMineruErrorText(value) {
    var message = String(value || '').replace(/\s+/g, ' ').trim();
    if (/pypi\.org\/simple\/mineru/i.test(message) && /(failed to fetch|tunnel error|connect)/i.test(message)) {
      return '无法连接 PyPI，请检查网络或代理后重试。';
    }
    if (/(huggingface_hub|hf_hub_download|xet_get|aws\.cdn\.hf\.co)/i.test(message) && /(connectionerror|network error|request middleware error|timeout|connect|readerror|i\/o error|decoding response body)/i.test(message)) {
      return '模型下载网络中断，请检查网络或代理后重试。';
    }
    return message.length > 180 ? message.slice(0, 177) + '…' : message;
  }

  function renderManagedMineru(runtime) {
    var externalConfigured = !!parserStore.mineruLocalConfig.enabled && !parserStore.mineruLocalConfig.managed;
    var hardware = runtime.hardware || {};
    var hardwareText = document.getElementById('managed-mineru-hardware');
    if (hardwareText) {
      var memory = hardware.vram_mb ? ' · ' + (hardware.vram_mb / 1024).toFixed(0) + 'GB 显存' : '';
      hardwareText.textContent = hardware.detection_error
        ? hardware.detection_error + ' · 默认推荐 Pipeline'
        : hardware.name
        ? '当前设备：' + hardware.name + memory + ' · 推荐 ' + (hardware.recommended_profile === 'vlm' ? 'VLM' : 'Pipeline')
        : '未检测到可用的本地推理硬件';
    }
    var service = runtime.service || {};
    var vlmProfile = (runtime.profiles || []).find(function(item) { return item.profile === 'vlm'; });
    var vlmSection = document.querySelector('[data-mineru-profile="vlm"]');
    if (vlmSection) vlmSection.hidden = !hardware.vlm_supported && !(vlmProfile && vlmProfile.installed);
    var active = false;
    var errors = [];
    (runtime.profiles || []).forEach(function(profile) {
      var fields = managedMineruFields(profile.profile);
      var busy = ['provisioning','downloading_models','validating','starting','cleaning'].indexOf(profile.state) >= 0;
      var running = !!service.running && service.profile === profile.profile;
      active = active || busy;
      if (profile.error) errors.push(profile.display_name);
      var labels = {
        provisioning:'安装依赖中', downloading_models:'下载模型中', validating:'验证中',
        starting:'启动中', cleaning:'清理中'
      };
      if (fields.state) {
        fields.state.className = 'settings-status ' + (profile.installed ? 'ready' : 'warning');
        fields.state.textContent = labels[profile.state]
          || (running ? '运行中' : profile.error ? '安装失败' : profile.update_available ? '可更新' : profile.installed ? '已安装' : profile.supported ? (externalConfigured ? '未由 MEFinder 安装' : '未安装') : '平台不支持');
      }
      if (fields.install) {
        fields.install.hidden = (profile.installed && !profile.update_available) || busy;
        fields.install.disabled = !profile.supported || (profile.profile === 'vlm' && !hardware.vlm_supported);
        fields.install.textContent = profile.update_available ? '更新组件' : externalConfigured ? '改用托管安装' : '下载安装';
        fields.install.onclick = function() {
          manageMineruComponent(profile.profile, profile.update_available ? 'update' : 'install', fields.install);
        };
      }
      if (fields.start) fields.start.hidden = !profile.installed || busy || running;
      if (fields.stop) fields.stop.hidden = !running || busy;
      if (fields.uninstall) fields.uninstall.hidden = !profile.installed || busy || running;
      if (fields.cancel) fields.cancel.hidden = !busy;
      if (fields.progressHint) {
        var detail = running && service.endpoint
          ? '运行于 ' + service.endpoint
          : profile.error ? '安装失败：' + managedMineruErrorText(profile.error) : (profile.message || '');
        var transfer = busy ? managedMineruTransferSummary(profile) : '';
        if (transfer) detail += (detail ? ' · ' : '') + transfer;
        fields.progressHint.hidden = !detail;
        fields.progressHint.textContent = detail;
      }
      if (fields.progress) {
        fields.progress.hidden = !busy;
        var bar = fields.progress.firstElementChild;
        if (bar) bar.style.width = profile.progress == null ? '18%' : Math.round(profile.progress * 100) + '%';
        fields.progress.classList.toggle('indeterminate', busy && profile.progress == null);
      }
    });
    var autoButton = document.getElementById('managed-mineru-auto-install');
    if (autoButton) {
      var recommended = hardware.recommended_profile || 'pipeline';
      var recommendedProfile = (runtime.profiles || []).find(function(item) { return item.profile === recommended; });
      autoButton.disabled = active || !runtime.supported || !!(recommendedProfile && recommendedProfile.installed);
      autoButton.textContent = recommendedProfile && recommendedProfile.installed
        ? '已安装'
        : externalConfigured ? '改用推荐托管配置' : '安装推荐配置';
    }
    var hint = document.getElementById('managed-mineru-hint');
    if (hint) hint.textContent = errors.length ? '安装失败，未改动现有本地部署设置。' : (service.running
      ? ''
      : active ? '安装需要约 20GB 可用空间，请保持应用开启'
      : externalConfigured ? '已配置自部署服务 ' + parserStore.mineruLocalConfig.endpoint + '；无需重复下载。下方托管运行时为可选方案'
      : '组件按需下载，不会随主程序更新自动安装');
    if (parserStore.mineruLocalConfig.managed) {
      updateMineruLocalStatus(
        !!parserStore.mineruLocalConfig.enabled,
        managedMineruSummaryLabel(parserStore.mineruLocalConfig, runtime)
      );
    }
    if (parserStore.managedMineruPollTimer) clearTimeout(parserStore.managedMineruPollTimer);
    parserStore.managedMineruPollTimer = active ? setTimeout(loadManagedMineruStatus, 900) : null;
    if (parserStore.managedMineruWasBusy && !active) loadMineruConfig();
    parserStore.managedMineruWasBusy = active;
  }

  async function loadManagedMineruStatus() {
    try {
      var response = await fetch('/api/mineru-local/component');
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '读取失败');
      renderManagedMineru(data);
    } catch (error) {
      var hint = document.getElementById('managed-mineru-hint');
      if (hint) hint.textContent = '读取托管运行时失败：' + error.message;
    }
  }

  async function manageMineruComponent(profile, action, button) {
    if ((action === 'install' || action === 'update') && !await showAppConfirm(
      '将创建独立 Python 环境并下载 MinerU 模型，最多可能占用约 20GB 磁盘。',
      {title:'下载安装本地 MinerU？', confirmText:'开始安装'}
    )) return;
    if (action === 'uninstall' && !await showAppConfirm(
      '将删除该配置的 MinerU 运行时、依赖和本地模型。',
      {title:'卸载本地 MinerU？', tone:'warning', confirmText:'卸载'}
    )) return;
    if (button) button.disabled = true;
    try {
      var response = await fetch('/api/mineru-local/component', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({profile:profile, action:action})
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '操作失败');
      renderManagedMineru(data.managed_runtime || {});
    } catch (error) {
      showToast('本地 MinerU 组件操作失败：' + error.message, 'danger');
    } finally {
      if (button) button.disabled = false;
    }
  }

  function syncMineruLocalImportOption(enabled) {
    var option = document.getElementById('mineru-local-parse-option');
    if (!option) return;
    option.hidden = !enabled;
    var input = option.querySelector('input[name="pdf-parse-mode"]');
    if (!enabled && input && input.checked) {
      var automatic = document.querySelector('input[name="pdf-parse-mode"][value="auto"]');
      if (automatic) automatic.checked = true;
    } else if (enabled && input && settingsStore.currentPdfParseMode === 'mineru-local') {
      input.checked = true;
    }
  }

  function updateMineruLocalStatus(enabled, label) {
    var status = document.getElementById('mineru-local-status');
    if (!status) return;
    status.className = 'settings-status' + (enabled ? ' ready' : '');
    status.textContent = label || (enabled ? '已启用' : '未启用');
  }

  function mineruLocalPayload() {
    return {
      endpoint: document.getElementById('mineru-local-endpoint').value.trim(),
      backend: document.getElementById('mineru-local-backend').value.trim(),
      enabled: document.getElementById('mineru-local-enabled').checked
    };
  }

  async function saveMineruLocalSettings() {
    var button = document.getElementById('mineru-local-save');
    var hint = document.getElementById('mineru-local-hint');
    button.disabled = true;
    button.textContent = '保存中…';
    if (hint) hint.textContent = '';
    try {
      var response = await fetch('/api/mineru-local', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(mineruLocalPayload())
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '保存失败');
      renderMineruLocalSettings(data);
      importStore.queue.filter(function(item) {
        return item.jobId && (item.status === 'failed' || item.status === 'paused');
      }).forEach(function(item) { global.MEFinder.imports.pollJob(item.id); });
      if (hint) hint.textContent = data.enabled ? '已保存；导入时可直接选择「本地 MinerU」' : '已关闭本地部署选项';
    } catch (error) {
      if (hint) hint.textContent = '未保存：' + error.message;
    } finally {
      button.disabled = false;
      button.textContent = '保存设置';
    }
  }

  async function testMineruLocalConnection() {
    var button = document.getElementById('mineru-local-test');
    var hint = document.getElementById('mineru-local-hint');
    button.disabled = true;
    button.textContent = '检测中…';
    if (hint) hint.textContent = '正在连接本地服务…';
    try {
      var response = await fetch('/api/mineru-local/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(mineruLocalPayload())
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '连接失败');
      if (hint) hint.textContent = '连接成功 · ' + data.latency_ms + ' ms';
      var runtime = parserStore.mineruLocalConfig.managed_runtime || {};
      var service = runtime.service || {};
      updateMineruLocalStatus(true, parserStore.mineruLocalConfig.managed
        ? managedMineruSummaryLabel(parserStore.mineruLocalConfig, {service:{running:true, profile:service.profile || parserStore.mineruLocalConfig.managed_profile}})
        : '自部署运行中');
    } catch (error) {
      if (hint) hint.textContent = '连接失败：' + error.message;
      var status = document.getElementById('mineru-local-status');
      if (status) { status.className = 'settings-status warning'; status.textContent = parserStore.mineruLocalConfig.managed ? '托管未连接' : '自部署未连接'; }
    } finally {
      button.disabled = false;
      button.textContent = '检测连接';
    }
  }

  function renderMineruAccountList() {
    // Original table: 账号 / 状态 / 到期日期 / 本地解析 / 操作. Edit opens the inline
    // editor panel below (no modal).
    var list = document.getElementById('mineru-account-list');
    if (!list) return;
    var count = document.getElementById('mineru-account-count');
    if (count) count.textContent = parserStore.mineruAccounts.length.toLocaleString() + ' 个账号';
    if (!parserStore.mineruAccounts.length) { list.innerHTML = ''; return; }
    var usageByAccount = {};
    (Array.isArray(parserStore.mineruStatistics.credentials) ? parserStore.mineruStatistics.credentials : []).forEach(function(item) {
      usageByAccount[item.account_id] = item;
    });
    var rows = parserStore.mineruAccounts.map(function(item) {
      var usage = usageByAccount[item.account_id] || {};
      var healthy = item.health_status === 'healthy' || !item.health_status;
      var state = !item.configured ? '缺少 Token' : !item.enabled ? '已停用'
        : item.health_status === 'unauthorized' ? '认证失效'
        : item.health_status === 'cooldown' ? '冷却中' : '可用';
      var stateClass = item.enabled && item.configured && healthy ? 'ready' : 'warning';
      var expires = item.expires_at ? esc(item.expires_at.replace(/-/g, '/')) : '—';
      var usageLabel = Number(usage.parsed_book_count || 0).toLocaleString() + ' 本 · ' + Number(usage.parsed_page_count || 0).toLocaleString() + ' 页';
      return '<tr><td data-label="账号"><span class="mineru-account-identity"><span class="mineru-account-avatar" aria-hidden="true"><span class="mineru-brand-glyph"></span></span><span class="mineru-account-copy"><strong>' + esc(item.display_name) + '</strong><small>' + (item.configured ? 'Token 已保存' : '需要 Token') + '</small></span></span></td>' +
        '<td data-label="状态"><span class="mineru-account-status-cell"><span class="mineru-account-state ' + stateClass + '">' + esc(state) + '</span><label class="ui-switch mineru-row-switch" title="' + (item.enabled ? '停用账号' : '启用账号') + '"><input type="checkbox" data-account-id="' + esc(item.account_id) + '" ' + (item.enabled ? 'checked ' : '') + 'onchange="toggleMineruAccountEnabled(this)"><span class="ui-switch-track" aria-hidden="true"></span><span class="visually-hidden">' + (item.enabled ? '停用' : '启用') + ' ' + esc(item.display_name) + '</span></label></span></td>' +
        '<td data-label="到期日期"><span class="mineru-table-date">' + expires + '</span></td>' +
        '<td data-label="本地解析"><span class="mineru-table-usage">' + usageLabel + '</span></td>' +
        '<td data-label="操作"><span class="mineru-row-actions"><button class="mineru-text-action" type="button" data-account-id="' + esc(item.account_id) + '" onclick="testMineruConnection(this.dataset.accountId, this)">测试</button><button class="mineru-text-action" type="button" data-account-id="' + esc(item.account_id) + '" onclick="selectMineruAccount(this.dataset.accountId)">编辑</button><button class="mineru-text-action danger" type="button" data-account-id="' + esc(item.account_id) + '" onclick="deleteMineruAccount(this.dataset.accountId)">删除</button></span></td></tr>';
    }).join('');
    list.innerHTML = '<div class="mineru-account-table-scroll"><table class="mineru-account-table"><thead><tr><th>账号</th><th>状态</th><th>到期日期</th><th>本地解析</th><th><span class="visually-hidden">操作</span></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  // The inline editor panel expands below the table on 添加账号 / 编辑, and folds
  // away on save / cancel — no modal.
  function showMineruEditor() {
    var card = document.getElementById('mineru-editor-card');
    if (card) { card.hidden = false; card.scrollIntoView({behavior: 'smooth', block: 'nearest'}); }
  }
  function hideMineruEditor() {
    var card = document.getElementById('mineru-editor-card');
    if (card) card.hidden = true;
    parserStore.mineruSelectedAccountId = '';
  }

  function mineruEditorPrep() {
    var error = document.getElementById('mineru-dialog-error');
    if (error) { error.hidden = true; error.textContent = ''; }
    var token = document.getElementById('mineru-token'); if (token) token.type = 'password';
    var toggle = document.getElementById('mineru-token-toggle');
    if (toggle) {
      if (toggle.classList.contains('settings-secret-toggle')) {
        toggle.classList.remove('is-visible');
        toggle.setAttribute('aria-label', '显示');
      } else {
        toggle.textContent = '显示';
      }
    }
    var editing = !!document.getElementById('mineru-account-id').value.trim();
    var test = document.getElementById('mineru-account-test'); if (test) test.hidden = !editing;
  }

  async function openMineruTokenPage() {
    try {
      var resp = await fetch('/api/open-mineru-token', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '打开失败');
    } catch (e) {
      showToast('打开 MinerU 失败：' + e.message, 'danger');
    }
  }

  function startAddMineruAccount(shouldFocus) {
    var firstAccount = parserStore.mineruAccounts.length === 0;
    parserStore.mineruSelectedAccountId = '';
    document.getElementById('mineru-account-id').value = '';
    document.getElementById('mineru-account-name').value = firstAccount ? 'MinerU 账号' : 'MinerU 账号 ' + (parserStore.mineruAccounts.length + 1);
    document.getElementById('mineru-token').value = '';
    document.getElementById('mineru-expires-at').value = '';
    document.getElementById('mineru-account-enabled').checked = true;
    document.getElementById('mineru-editor-title').textContent = '添加账号';
    document.getElementById('mineru-token-help').textContent = '新账号必填，只存本机';
    document.getElementById('mineru-account-save').textContent = firstAccount ? '保存配置' : '保存账号';
    document.getElementById('mineru-account-cancel').hidden = firstAccount;
    mineruEditorPrep();
    showMineruEditor();
    if (shouldFocus !== false) setTimeout(function() { var n = document.getElementById('mineru-account-name'); if (n) n.focus(); }, 0);
  }

  function selectMineruAccount(accountId) {
    var item = parserStore.mineruAccounts.find(function(account) { return account.account_id === accountId; });
    if (!item) return;
    parserStore.mineruSelectedAccountId = item.account_id;
    document.getElementById('mineru-account-id').value = item.account_id;
    document.getElementById('mineru-account-name').value = item.display_name || '';
    document.getElementById('mineru-token').value = '';
    document.getElementById('mineru-expires-at').value = item.expires_at || '';
    document.getElementById('mineru-account-enabled').checked = !!item.enabled;
    document.getElementById('mineru-editor-title').textContent = '编辑 ' + item.display_name;
    document.getElementById('mineru-token-help').textContent = '留空即保留原 Token';
    document.getElementById('mineru-account-save').textContent = '保存更改';
    document.getElementById('mineru-account-cancel').hidden = false;
    mineruEditorPrep();
    showMineruEditor();
  }

  // Kept for existing call sites. Cancel/close just folds the inline editor away.
  function openMineruAccountDialog() { showMineruEditor(); }
  function closeMineruAccountDialog() { hideMineruEditor(); }

  function mineruDialogError(message) {
    var error = document.getElementById('mineru-dialog-error');
    if (!error) return;
    error.textContent = message;
    error.hidden = false;
  }

  function mineruPageRangesLabel(ranges) {
    if (!Array.isArray(ranges) || !ranges.length) return '—';
    return ranges.map(function(range) {
      if (!Array.isArray(range) || range.length < 2) return '';
      return Number(range[0]).toLocaleString() + '–' + Number(range[1]).toLocaleString();
    }).filter(Boolean).join('、');
  }

  function parserDateLabel(value) {
    if (!value) return '—';
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(String(value));
    return date.toLocaleDateString('zh-CN', {year:'numeric', month:'2-digit', day:'2-digit'});
  }

  function renderParserProviderBooks(provider) {
    var rows = (Array.isArray(provider.books) ? provider.books : []).map(function(book) {
      var model = book.model ? esc(book.model) : '—';
      return '<tr><td data-label="文献"><span class="parser-book-title"><strong>' + esc(book.title || book.file_name || '未命名文献') + '</strong><small>' + esc(book.file_name || book.source_file_id || '') + '</small></span></td><td data-label="模型">' + model + '</td><td data-label="完成日期">' + parserDateLabel(book.completed_at) + '</td><td data-label="页数"><b>' + Number(book.parsed_page_count || 0).toLocaleString() + '</b> 页</td></tr>';
    }).join('');
    return '<div class="parser-detail-label">解析文献</div><div class="parser-book-table-scroll"><table class="parser-book-table"><thead><tr><th>文献</th><th>模型</th><th>完成日期</th><th>页数</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  function renderMineruCredentialAttribution(credentials) {
    if (!Array.isArray(credentials) || !credentials.length) return '<div class="parser-credential-empty">这些 MinerU 文献没有可匹配的本地账号归属记录</div>';
    return '<div class="parser-detail-label mineru-attribution-label">MinerU 账号归属 <small>本地记录，不是官网用量或计费数据</small></div><div class="parser-credential-list">' + credentials.map(function(item) {
      var bookRows = (Array.isArray(item.books) ? item.books : []).map(function(book) {
        return '<div class="parser-credential-book"><span><strong>' + esc(book.source_file_name || book.document_id || '未命名文献') + '</strong><small>原书页 ' + esc(mineruPageRangesLabel(book.page_ranges)) + '</small></span><b>' + Number(book.parsed_page_count || 0).toLocaleString() + ' 页</b></div>';
      }).join('');
      return '<details class="parser-credential-account"><summary><span><strong>' + esc(item.display_name || item.account_id) + '</strong><small>' + Number(item.parsed_book_count || 0).toLocaleString() + ' 本文献</small></span><b>' + Number(item.parsed_page_count || 0).toLocaleString() + ' 页</b><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></summary><div class="parser-credential-books">' + bookRows + '</div></details>';
    }).join('') + '</div>';
  }

  function renderParserStatistics() {
    var total = parserStore.parserStatistics.total || {};
    document.getElementById('parser-stat-books').textContent = Number(total.parsed_book_count || 0).toLocaleString();
    document.getElementById('parser-stat-pages').textContent = Number(total.parsed_page_count || 0).toLocaleString();
    document.getElementById('parser-stat-providers').textContent = Number(total.provider_count || 0).toLocaleString();
    var list = document.getElementById('parser-provider-list');
    var providers = Array.isArray(parserStore.parserStatistics.providers) ? parserStore.parserStatistics.providers : [];
    if (!providers.length) {
      list.innerHTML = '<div class="parser-statistics-empty"><strong>还没有解析统计</strong><small>导入并完成一本 PDF 的页级解析后，这里会按解析服务显示文献和页数</small></div>';
      return;
    }
    var orderedProviders = providers.slice().sort(function(a, b) {
      var aLocal = a.provider_kind === 'local' ? 0 : 1;
      var bLocal = b.provider_kind === 'local' ? 0 : 1;
      return aLocal - bLocal;
    });
    list.innerHTML = orderedProviders.map(function(provider, index) {
      var isMineru = provider.provider_id === 'mineru-cloud' || provider.provider_id === 'mineru-local';
      var isCloudMineru = provider.provider_id === 'mineru-cloud';
      var kind = provider.provider_kind === 'local' ? '本地' : 'API';
      var details = renderParserProviderBooks(provider);
      if (isCloudMineru) details += renderMineruCredentialAttribution(provider.credentials || []);
      var providerMark = isMineru ? '<span class="mineru-brand-glyph"></span>' : esc(String(provider.provider_name || '?').charAt(0).toUpperCase());
      return '<details class="parser-provider-group" open><summary><span class="parser-provider-identity"><span class="parser-provider-mark ' + (isMineru ? 'mineru' : '') + '" aria-hidden="true">' + providerMark + '</span><span><strong>' + esc(provider.provider_name || provider.provider_id) + '</strong><small>' + kind + '</small></span></span><span class="parser-provider-number"><b>' + Number(provider.parsed_book_count || 0).toLocaleString() + '</b> 本</span><span class="parser-provider-number"><b>' + Number(provider.parsed_page_count || 0).toLocaleString() + '</b> 页</span><svg class="parser-provider-chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></summary><div class="parser-provider-detail">' + details + '</div></details>';
    }).join('');
  }

  async function loadParserStatistics() {
    var status = document.getElementById('parser-statistics-status');
    if (status) {
      status.className = 'settings-status';
      status.textContent = '刷新中…';
    }
    try {
      var response = await fetch('/api/parser-statistics');
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '读取失败');
      parserStore.parserStatistics = data || {total:{parsed_book_count:0, parsed_page_count:0, provider_count:0}, providers:[]};
      renderParserStatistics();
      global.MEFinder.visionProviders.render();
      if (status) {
        status.className = 'settings-status ready';
        status.textContent = '已刷新';
      }
    } catch (error) {
      if (status) {
        status.className = 'settings-status warning';
        status.textContent = '读取失败';
      }
      showToast('读取本地解析统计失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
    }
  }

  function loadMineruStatistics() {
    return loadParserStatistics();
  }

  async function exportBackup() {
    var hint = document.getElementById('backup-export-hint');
    try {
      var outputDirectory = await chooseDesktopExportDirectory();
      if (outputDirectory === null) return;
      if (hint) hint.textContent = '正在导出…';
      var payload = {};
      if (outputDirectory) payload.output_dir = outputDirectory;
      var resp = await fetch('/api/backup/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '导出失败');
      if (hint) hint.textContent = '已导出到：' + data.path;
      showToast('备份已导出（' + formatFileSize(data.size_bytes) + '）');
    } catch (e) {
      if (hint) hint.textContent = '仅备份页码、书目和偏好，不含 PDF';
      showToast('导出备份失败：' + e.message);
    }
  }

  async function importBackup() {
    var button = document.getElementById('backup-import-choose');
    if (button && button.disabled) return;
    if (button) { button.disabled = true; button.textContent = '正在选择…'; }
    try {
      var chooseResp = await fetch('/api/backup/import/choose', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      var chosen = await chooseResp.json();
      if (!chooseResp.ok || chosen.error) throw new Error(chosen.error || '选择备份失败');
      if (chosen.cancelled) return;
      if (!await showAppConfirm(
        '将从「' + (chosen.name || '所选备份') + '」恢复，并覆盖当前的页码映射与书目信息。',
        {title:'导入并覆盖当前数据？', confirmText:'确认导入', tone:'danger'}
      )) return;
      if (button) button.textContent = '正在导入…';
      var resp = await fetch('/api/backup/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: chosen.path})});
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
      showToast('已恢复备份，正在重建索引…');
      pollBackupRestore(data.job_id);
    } catch (e) {
      showToast('导入备份失败：' + e.message);
    } finally {
      if (button) { button.disabled = false; button.textContent = '选择备份并恢复'; }
    }
  }

  function pollBackupRestore(jobId) {
    fetch('/api/import-status?job_id=' + encodeURIComponent(jobId))
      .then(function(resp) { return resp.json(); })
      .then(function(data) {
        if (data.status === 'completed') {
          showToast(data.message || '备份已恢复');
          invalidateLibraryCatalog();
          loadMeta();
          return;
        }
        if (data.status === 'failed' || data.error) {
          showToast('恢复失败：' + (data.message || data.error || '未知错误'));
          return;
        }
        setTimeout(function() { pollBackupRestore(jobId); }, 2000);
      })
      .catch(function() { setTimeout(function() { pollBackupRestore(jobId); }, 4000); });
  }

  function toggleMineruSecret(inputId, buttonId) {
    var input = document.getElementById(inputId);
    var button = document.getElementById(buttonId);
    if (!input || !button) return;
    var visible = input.type === 'text';
    input.type = visible ? 'password' : 'text';
    if (button.classList.contains('settings-secret-toggle')) {
      button.classList.toggle('is-visible', !visible);
      button.setAttribute('aria-label', visible ? '显示' : '隐藏');
    } else {
      button.textContent = visible ? '显示' : '隐藏';
    }
  }

  async function saveMineruConfig(event) {
    if (event) event.preventDefault();
    var accountId = document.getElementById('mineru-account-id').value.trim();
    var saveButton = document.getElementById('mineru-account-save');
    var payload = {
      account_id: accountId || null,
      display_name: document.getElementById('mineru-account-name').value.trim(),
      token: document.getElementById('mineru-token').value.trim(),
      api_base: document.getElementById('mineru-api-base').value.trim(),
      expires_at: document.getElementById('mineru-expires-at').value,
      enabled: document.getElementById('mineru-account-enabled').checked
    };
    if (!payload.display_name) { mineruDialogError('请填写账号名称。'); return; }
    if (!accountId && !payload.token) { mineruDialogError('新账号必须填写 API Token。'); return; }
    var idleLabel = accountId ? '保存更改' : (parserStore.mineruAccounts.length ? '添加账号' : '保存配置');
    saveButton.disabled = true;
    saveButton.textContent = '保存中…';
    try {
      var resp = await fetch('/api/mineru-accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
      parserStore.mineruSelectedAccountId = data.saved_account_id || accountId;
      parserStore.mineruConfigLoaded = false;
      await loadMineruConfig();
      hideMineruEditor();
    } catch (e) {
      mineruDialogError('账号未保存。' + e.message);
    } finally {
      saveButton.disabled = false;
      saveButton.textContent = idleLabel;
    }
  }

  async function saveMineruServiceAddress() {
    var button = document.getElementById('mineru-service-save');
    var status = document.getElementById('mineru-config-status');
    var apiBase = document.getElementById('mineru-api-base').value.trim();
    button.disabled = true;
    button.textContent = '保存中…';
    try {
      var resp = await fetch('/api/mineru-accounts/service', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({api_base: apiBase})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
      document.getElementById('mineru-api-base').value = data.api_base || apiBase;
      if (status) { status.className = 'settings-status ready'; status.textContent = '地址已保存'; }
    } catch (e) {
      showToast('MinerU 服务地址未保存：' + e.message, 'danger');
    } finally {
      button.disabled = false;
      button.textContent = '保存地址';
    }
  }

  async function toggleMineruAccountEnabled(input) {
    var item = parserStore.mineruAccounts.find(function(account) { return account.account_id === input.dataset.accountId; });
    if (!item) return;
    var previous = !!item.enabled;
    item.enabled = !!input.checked;
    renderMineruAccountList();
    try {
      var resp = await fetch('/api/mineru-accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          account_id: item.account_id,
          display_name: item.display_name,
          token: '',
          api_base: document.getElementById('mineru-api-base').value.trim(),
          expires_at: item.expires_at || '',
          enabled: item.enabled
        })
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
      parserStore.mineruAccounts = Array.isArray(data.accounts) ? data.accounts : parserStore.mineruAccounts;
      parserStore.mineruStatistics = data.statistics || parserStore.mineruStatistics;
      renderMineruAccountList();
    } catch (e) {
      item.enabled = previous;
      renderMineruAccountList();
      showToast('账号状态未保存：' + e.message, 'danger');
    }
  }

  async function deleteMineruAccount(accountId) {
    var item = parserStore.mineruAccounts.find(function(account) { return account.account_id === accountId; });
    if (!item || !await showAppConfirm(
      '将删除 MinerU 账号「' + item.display_name + '」及其在本机保存的 Token。已完成的解析统计会保留。',
      {title:'删除 MinerU 账号？', confirmText:'删除', tone:'danger'}
    )) return;
    try {
      var resp = await fetch('/api/mineru-accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'delete_account', account_id: accountId})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '删除失败');
      if (parserStore.mineruSelectedAccountId === accountId) hideMineruEditor();
      parserStore.mineruConfigLoaded = false;
      await loadMineruConfig();
      showToast('MinerU 账号已删除');
    } catch (e) {
      showToast('删除 MinerU 账号失败：' + e.message, 'danger');
    }
  }

  async function testMineruConnection(accountId, button) {
    if (!accountId) return;
    var status = document.getElementById('mineru-config-status');
    // Button may be an icon or a text button — preserve its content.
    var buttonHTML = button ? button.innerHTML : null;
    if (button) { button.disabled = true; }
    if (status) { status.className = 'settings-status'; status.textContent = '测试连接中…'; }
    try {
      var resp = await fetch('/api/mineru-accounts/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({account_id: accountId})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '测试失败');
      if (status) { status.className = 'settings-status ready'; status.textContent = '连接正常 · ' + data.latency_ms + ' ms'; }
    } catch (e) {
      if (status) { status.className = 'settings-status warning'; status.textContent = '连接失败'; }
      showToast('MinerU 连接失败：' + e.message, 'danger');
    } finally {
      if (button) { button.disabled = false; if (buttonHTML !== null) button.innerHTML = buttonHTML; }
    }
  }

  function bindMineruAccountDialogDismissal() {
    // The MinerU editor is inline now (no modal). Kept as a no-op so the
    // existing init call site stays valid.
  }

  // -- 通用本地模型：单个自部署 OpenAI 兼容端点，复用 vision 后端，配置独立 --

  function generalModelFieldValues() {
    return {
      api_base: (document.getElementById('general-model-base').value || '').trim(),
      api_key: (document.getElementById('general-model-key').value || '').trim(),
      name: (document.getElementById('general-model-name').value || '').trim(),
      model: (document.getElementById('general-model-model').value || '').trim(),
      enabled: !!document.getElementById('general-model-enabled').checked
    };
  }

  function updateGeneralModelStatus(summary) {
    var status = document.getElementById('general-model-status');
    if (!status) return;
    var enabled = !!(summary && summary.enabled);
    var configured = !!(summary && summary.configured);
    status.className = 'settings-status' + (enabled ? ' ready' : (configured ? ' warning' : ''));
    status.textContent = enabled ? '已启用' : (configured ? '已配置（未启用）' : '未配置');
  }

  function syncGeneralModelImportOption(enabled) {
    var option = document.getElementById('general-model-parse-option');
    if (!option) return;
    option.hidden = !enabled;
    var input = option.querySelector('input[name="pdf-parse-mode"]');
    if (!enabled && input && input.checked) {
      var automatic = document.querySelector('input[name="pdf-parse-mode"][value="auto"]');
      if (automatic) automatic.checked = true;
    } else if (enabled && input && settingsStore.currentPdfParseMode === 'general-local-model') {
      input.checked = true;
    }
  }

  function renderGeneralModel(summary) {
    if (!summary) return;
    var base = document.getElementById('general-model-base');
    var name = document.getElementById('general-model-name');
    var model = document.getElementById('general-model-model');
    var enabled = document.getElementById('general-model-enabled');
    var key = document.getElementById('general-model-key');
    if (key) key.placeholder = summary.has_key ? '已保存（留空即保留）' : '本地部署通常留空';
    if (base && document.activeElement !== base) base.value = summary.api_base || '';
    if (name && document.activeElement !== name) name.value = (summary.name && summary.name !== '通用本地模型') ? summary.name : '';
    if (model && document.activeElement !== model) model.value = summary.model || '';
    if (enabled) enabled.checked = !!summary.enabled;
    updateGeneralModelStatus(summary);
    syncGeneralModelImportOption(!!summary.enabled);
  }

  async function loadGeneralModelConfig() {
    try {
      var response = await fetch('/api/general-model');
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '读取失败');
      renderGeneralModel(data);
    } catch (error) {
      updateGeneralModelStatus(null);
    }
  }

  async function saveGeneralModel() {
    var hint = document.getElementById('general-model-hint');
    var button = document.getElementById('general-model-save');
    if (button) button.disabled = true;
    if (hint) { hint.className = 'settings-hint'; hint.textContent = '保存中…'; }
    try {
      var response = await fetch('/api/general-model', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(generalModelFieldValues())
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '保存失败');
      var keyField = document.getElementById('general-model-key');
      if (keyField) keyField.value = '';
      renderGeneralModel(data);
      if (hint) { hint.className = 'settings-hint success'; hint.textContent = '已保存'; }
    } catch (error) {
      if (hint) { hint.className = 'settings-hint danger'; hint.textContent = error.message; }
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function testGeneralModel() {
    var hint = document.getElementById('general-model-hint');
    var button = document.getElementById('general-model-test');
    if (button) button.disabled = true;
    if (hint) { hint.className = 'settings-hint'; hint.textContent = '连接中…'; }
    try {
      var response = await fetch('/api/general-model/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(generalModelFieldValues())
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '连接失败');
      if (hint) { hint.className = 'settings-hint success'; hint.textContent = '连接成功（' + (data.latency_ms || 0) + ' ms）'; }
    } catch (error) {
      if (hint) { hint.className = 'settings-hint danger'; hint.textContent = error.message; }
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function fetchGeneralModelModels() {
    var hint = document.getElementById('general-model-model-hint');
    var button = document.getElementById('general-model-fetch');
    if (button) button.disabled = true;
    if (hint) { hint.className = 'vision-model-hint'; hint.textContent = '读取模型列表…'; }
    try {
      var response = await fetch('/api/general-model/models', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(generalModelFieldValues())
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '读取失败');
      var models = (data.models || []).map(function(m) { return typeof m === 'string' ? m : (m && m.id) || ''; }).filter(Boolean);
      var list = document.getElementById('general-model-model-list');
      if (list) {
        list.innerHTML = '';
        models.forEach(function(id) { var opt = document.createElement('option'); opt.value = id; list.appendChild(opt); });
      }
      if (hint) { hint.className = 'vision-model-hint'; hint.textContent = models.length ? ('读到 ' + models.length + ' 个模型，点击输入框选择') : '未读到模型，请手动输入型号'; }
    } catch (error) {
      if (hint) { hint.className = 'vision-model-hint'; hint.textContent = error.message + '（可手动输入型号）'; }
    } finally {
      if (button) button.disabled = false;
    }
  }

  var parserRuntimeAPI = {
    loadGeneralModelConfig: loadGeneralModelConfig,
    loadMineruConfig: loadMineruConfig,
    bindMineruAccountDialogDismissal: bindMineruAccountDialogDismissal
  };
  global.MEFinder = global.MEFinder || {};
  global.MEFinder.parserRuntime = parserRuntimeAPI;

  // 内联处理器仍需浏览器全局可见；模块间调用统一走 MEFinder.parserRuntime。
  global.saveGeneralModel = saveGeneralModel;
  global.testGeneralModel = testGeneralModel;
  global.fetchGeneralModelModels = fetchGeneralModelModels;
  global.loadLocalOCRConfig = loadLocalOCRConfig;
  global.saveLocalOCRConfig = saveLocalOCRConfig;
  global.manageLocalOCRComponent = manageLocalOCRComponent;
  global.testLocalOCREngine = testLocalOCREngine;
  global.manageMineruComponent = manageMineruComponent;
  global.saveMineruLocalSettings = saveMineruLocalSettings;
  global.testMineruLocalConnection = testMineruLocalConnection;
  global.openMineruTokenPage = openMineruTokenPage;
  global.startAddMineruAccount = startAddMineruAccount;
  global.selectMineruAccount = selectMineruAccount;
  global.toggleMineruAccountEnabled = toggleMineruAccountEnabled;
  global.deleteMineruAccount = deleteMineruAccount;
  global.closeMineruAccountDialog = closeMineruAccountDialog;
  global.loadParserStatistics = loadParserStatistics;
  global.exportBackup = exportBackup;
  global.importBackup = importBackup;
  global.toggleMineruSecret = toggleMineruSecret;
  global.saveMineruConfig = saveMineruConfig;
  global.saveMineruServiceAddress = saveMineruServiceAddress;
  global.testMineruConnection = testMineruConnection;

  if (typeof module !== "undefined" && module.exports) {
    Object.assign(module.exports, {
      managedMineruErrorText: managedMineruErrorText,
      managedMineruSummaryLabel: managedMineruSummaryLabel,
      managedMineruTransferSummary: managedMineruTransferSummary,
      renderManagedMineru: renderManagedMineru
    });
  }
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
