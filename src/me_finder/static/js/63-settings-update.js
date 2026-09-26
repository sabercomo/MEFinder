/* macOS 与 Windows 桌面更新状态和操作。 */
(function (global) {  // module: 63-settings-update.js
  function renderMacosUpdateState(state) {
    var badge = document.getElementById('macos-update-status');
    var message = document.getElementById('macos-update-message');
    var release = document.getElementById('macos-update-release');
    if (badge) {
      var labels = {
        checking: '检查中',
        up_to_date: '已是最新',
        available: '有新版本',
        unavailable: '暂无更新',
        error: '检查失败',
        unsupported: '不支持'
      };
      badge.className = 'settings-status';
      if (state.status === 'up_to_date') badge.classList.add('ready');
      if (state.status === 'available' || state.status === 'unavailable') badge.classList.add('warning');
      if (state.status === 'error') badge.classList.add('error');
      badge.textContent = labels[state.status] || '未检查';
    }
    if (message) message.textContent = state.message || '更新状态未知';
    if (release) {
      var canOpen = state.status === 'available' && !!state.release_url;
      release.style.display = canOpen ? '' : 'none';
      if (canOpen) release.href = state.release_url;
      else release.removeAttribute('href');
    }
  }

  async function checkMacosUpdate() {
    var button = document.getElementById('macos-update-check');
    if (button && button.disabled) return;
    if (button) button.disabled = true;
    renderMacosUpdateState({
      status: 'checking',
      message: '正在检查 GitHub Releases 中适用于当前 Mac 的 DMG…'
    });
    try {
      var resp = await MEFinderApi.fetch('/api/macos-update', {cache: 'no-store'});
      var state = await resp.json();
      if (!resp.ok && state.status !== 'unsupported') {
        throw new Error(state.message || '检查更新失败');
      }
      renderMacosUpdateState(state);
      if (state.status === 'available') {
        showToast('发现 Mac 新版本 v' + state.latest_version);
      } else if (state.status === 'up_to_date') {
        showToast('当前已是最新 Mac 版本');
      }
    } catch (e) {
      renderMacosUpdateState({
        status: 'error',
        message: e.message || '检查更新失败，请稍后重试'
      });
    } finally {
      if (button) button.disabled = false;
    }
  }

  function renderUpdateState(state) {
    if (!state) return;
    settingsStore.updateState = state;
    var current = document.getElementById('update-current-version');
    var message = document.getElementById('update-message');
    var badge = document.getElementById('update-status-badge');
    var check = document.getElementById('update-check-btn');
    var action = document.getElementById('update-action-btn');
    var release = document.getElementById('update-release-link');
    var autoInput = document.getElementById('auto-update-enabled');
    var autoDescription = document.getElementById('update-auto-description');
    if (current && state.current_version) current.textContent = 'v' + state.current_version;
    if (message) message.textContent = state.message || '更新状态未知';
    if (badge) {
      var labels = {
        idle: '未检查', checking: '检查中', up_to_date: '已是最新', available: '有新版本',
        downloading: '下载中', ready: '可安装', installing: '安装中', error: '检查失败', unsupported: '不支持'
      };
      badge.textContent = labels[state.status] || '更新状态';
      badge.classList.toggle('ready', ['up_to_date','ready'].indexOf(state.status) >= 0);
      badge.classList.toggle('warning', state.status === 'available');
      badge.classList.toggle('error', state.status === 'error');
    }
    var busy = ['checking','downloading','installing'].indexOf(state.status) >= 0;
    if (check) check.disabled = busy;
    if (action) {
      var actionable = !!state.can_self_update && (state.status === 'available' || state.status === 'ready');
      action.style.display = actionable ? '' : 'none';
      action.disabled = busy;
      action.textContent = state.status === 'ready' ? '退出并安装' : '下载更新';
    }
    if (release) {
      release.style.display = state.release_url ? '' : 'none';
      if (state.release_url) release.href = state.release_url;
    }
    if (autoInput) {
      autoInput.disabled = !state.can_self_update;
      var autoOption = autoInput.closest('.update-auto-option');
      if (autoOption) autoOption.classList.toggle('is-disabled', !state.can_self_update);
    }
    if (autoDescription) {
      autoDescription.textContent = state.can_self_update
        ? '仅下载带 SHA-256 校验的官方安装包；安装前仍由你确认'
        : '自动更新只在 Windows 安装版中启用；绿色版和源码模式不会覆盖自身';
    }
  }

  async function loadUpdateStatus() {
    if (desktopShell !== 'win32') return null;
    try {
      var resp = await MEFinderApi.fetch('/api/update/status');
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
      renderUpdateState(data);
      return data;
    } catch (e) {
      renderUpdateState({status:'error', can_self_update:false, message:'读取更新状态失败：' + e.message});
      return null;
    }
  }

  async function checkForUpdates(automatic) {
    if (desktopShell !== 'win32') return;
    renderUpdateState(Object.assign({}, settingsStore.updateState, {status:'checking', message:'正在检查 GitHub Releases…'}));
    try {
      var resp = await MEFinderApi.fetch('/api/update/check', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({auto_download: automatic === true})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '检查失败');
      renderUpdateState(data);
      if (!automatic) showToast(data.message || '更新检查完成');
    } catch (e) {
      renderUpdateState(Object.assign({}, settingsStore.updateState, {status:'error', message:'检查更新失败：' + e.message}));
      if (!automatic) showToast('检查更新失败：' + e.message);
    }
  }

  async function runUpdateAction() {
    if (settingsStore.updateState.status === 'available') {
      renderUpdateState(Object.assign({}, settingsStore.updateState, {status:'downloading', message:'正在下载并校验更新…'}));
      try {
        var downloadResp = await MEFinderApi.fetch('/api/update/download', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
        var downloadData = await downloadResp.json();
        if (!downloadResp.ok || downloadData.error) throw new Error(downloadData.error || '下载失败');
        renderUpdateState(downloadData);
        showToast(downloadData.message || '更新已下载');
      } catch (e) {
        renderUpdateState(Object.assign({}, settingsStore.updateState, {status:'error', message:'下载更新失败：' + e.message}));
        showToast('下载更新失败：' + e.message);
      }
      return;
    }
    if (settingsStore.updateState.status !== 'ready') return;
    if (!await showAppConfirm(
      '安装更新会关闭 MEFinder，完成后自动重新打开',
      {title:'现在安装更新？', confirmText:'安装并重启', tone:'warning'}
    )) return;
    var installToken = settingsStore.updateState.install_token;
    if (!installToken) {
      showToast('安装确认已失效，请重新下载更新');
      return;
    }
    renderUpdateState(Object.assign({}, settingsStore.updateState, {
      status:'installing', install_token:null, message:'正在重新校验安装包并启动安装程序…'
    }));
    try {
      var installResp = await MEFinderApi.fetch('/api/update/install', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({confirm_token:installToken})
      });
      var installData = await installResp.json();
      if (!installResp.ok || installData.error) throw new Error(installData.error || '安装失败');
      renderUpdateState(installData);
    } catch (e) {
      renderUpdateState(Object.assign({}, settingsStore.updateState, {status:'error', message:'启动安装程序失败：' + e.message}));
      showToast('启动安装程序失败：' + e.message);
    }
  }

  async function setAutoUpdate(enabled) {
    var previous = settingsStore.autoUpdateEnabled;
    settingsStore.autoUpdateEnabled = enabled === true;
    try {
      var resp = await MEFinderApi.fetch('/api/preferences', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({auto_update:settingsStore.autoUpdateEnabled})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
      settingsStore.autoUpdateEnabled = data.auto_update === true;
      document.getElementById('auto-update-enabled').checked = settingsStore.autoUpdateEnabled;
      // Visible success: the switch itself已是反馈，无需 Toast。
      if (settingsStore.autoUpdateEnabled) {
        settingsStore.updateAutoStarted = true;
        checkForUpdates(true);
      } else {
        settingsStore.updateAutoStarted = false;
      }
    } catch (e) {
      settingsStore.autoUpdateEnabled = previous;
      document.getElementById('auto-update-enabled').checked = previous;
      showToast('自动更新设置保存失败：' + e.message);
    }
  }


  global.checkMacosUpdate = checkMacosUpdate;
  global.loadUpdateStatus = loadUpdateStatus;
  global.checkForUpdates = checkForUpdates;
  global.runUpdateAction = runUpdateAction;
  global.setAutoUpdate = setAutoUpdate;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
