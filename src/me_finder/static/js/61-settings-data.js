/* 数据位置选择与迁移。 */
(function (global) {  // module: 61-settings-data.js
  function renderDataLocation(data) {
    var badge = document.getElementById('data-location-status');
    var current = document.getElementById('data-location-current');
    if (!badge || !current) return;
    current.textContent = data.current_path || '未知位置';
    current.title = data.current_path || '';
    settingsStore.dataLocationRestartRequired = Boolean(data.restart_required);
    badge.className = 'settings-status' + (data.restart_required ? ' warning' : data.is_custom ? ' ready' : '');
    badge.textContent = data.restart_required ? '重启后生效' : data.is_custom ? '自定义位置' : '默认位置';
    ['data-location-choose', 'data-location-open'].forEach(function(id) {
      document.getElementById(id).disabled = settingsStore.dataLocationRestartRequired;
    });
    if (data.restart_required) {
      renderPendingDataLocation(data.pending_path, 'existing');
      document.getElementById('data-location-pending-label').textContent = '下次启动使用';
      document.getElementById('data-location-pending-note').textContent = '已保存选择，请退出并重新打开应用';
      document.getElementById('data-location-migrate').hidden = true;
    }
  }

  async function loadDataLocation() {
    if (!document.getElementById('data-location-settings')) return;
    var errorBox = document.getElementById('data-location-error');
    var badge = document.getElementById('data-location-status');
    if (badge) { badge.className = 'settings-status'; badge.textContent = '读取中…'; }
    try {
      var resp = await MEFinderApi.fetch('/api/data-location', {cache: 'no-store'});
      var data = await resp.json();
      if (resp.status === 404 || data.available === false) {
        delete document.documentElement.dataset.dataLocationAvailable;
        settingsStore.dataLocationLoaded = true;
        global.ensureVisibleSettingsCategory();
        return;
      }
      if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
      document.documentElement.dataset.dataLocationAvailable = 'true';
      if (errorBox) errorBox.hidden = true;
      renderDataLocation(data);
      settingsStore.dataLocationLoaded = true;
    } catch (e) {
      // No more dead end: surface the reason in the content area with a retry.
      if (badge) { badge.className = 'settings-status warning'; badge.textContent = '读取失败'; }
      var reason = document.getElementById('data-location-error-reason');
      if (reason) reason.textContent = e.message || '读取失败';
      if (errorBox) errorBox.hidden = false;
      var current = document.getElementById('data-location-current');
      if (current) current.textContent = '—';
    }
  }

  function renderPendingDataLocation(targetPath, mode, library) {
    settingsStore.pendingDataLocation = targetPath || '';
    settingsStore.pendingDataLocationMode = mode || 'migrate';
    var existing = settingsStore.pendingDataLocationMode === 'existing';
    var pending = document.getElementById('data-location-pending');
    var target = document.getElementById('data-location-target');
    pending.style.display = settingsStore.pendingDataLocation ? 'flex' : 'none';
    target.textContent = settingsStore.pendingDataLocation;
    target.title = settingsStore.pendingDataLocation;
    document.getElementById('data-location-pending-label').textContent = existing ? '已有资料库' : '迁移到';
    document.getElementById('data-location-pending-note').textContent = existing
      ? (library ? library.document_count + ' 部文献 · ' + library.paragraph_count + ' 个段落。' : '') + '重启后读取此资料库，不复制或覆盖资料'
      : '复制当前资料到新位置，重启后使用；原位置的数据保留';
    var button = document.getElementById('data-location-migrate');
    button.hidden = false;
    button.textContent = existing ? '使用此资料库' : '迁移并切换';
  }

  async function chooseDataLocation(mode) {
    mode = mode || 'migrate';
    if (settingsStore.dataLocationRestartRequired) return;
    var button = document.getElementById(mode === 'existing' ? 'data-location-open' : 'data-location-choose');
    if (button.disabled) return;
    var buttons = ['data-location-choose', 'data-location-open', 'data-location-migrate'].map(function(id) { return document.getElementById(id); });
    buttons.forEach(function(item) { item.disabled = true; });
    button.textContent = '选择并检查…';
    try {
      var resp = await MEFinderApi.fetch('/api/data-location/choose', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode: mode})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '选择位置失败');
      if (!data.cancelled) renderPendingDataLocation(data.target_path, mode, data);
    } catch (e) {
      showToast('选择数据位置失败：' + e.message);
    } finally {
      buttons.forEach(function(item) { item.disabled = false; });
      button.textContent = mode === 'existing' ? '选择已有库' : '选择新位置';
    }
  }

  async function migrateDataLocation() {
    if (!settingsStore.pendingDataLocation || settingsStore.dataLocationRestartRequired) return;
    var existing = settingsStore.pendingDataLocationMode === 'existing';
    var action = existing ? '使用此资料库' : '迁移并切换';
    if (!await showAppConfirm(
      (existing ? '重启后将读取：\n\n' : '将把当前索引、原文与设置复制到：\n\n')
      + settingsStore.pendingDataLocation
      + (existing ? '\n\n不会复制或覆盖两处资料。请先退出另一台电脑上的 MEFinder 并等待同步完成'
        : '\n\n迁移期间请不要关闭应用。完成后需重启，原位置的数据保留'),
      {title: existing ? '切换已有资料库？' : '迁移当前资料库？', confirmText: action}
    )) return;
    var button = document.getElementById('data-location-migrate');
    if (button.disabled) return;
    var buttons = ['data-location-choose', 'data-location-open', 'data-location-migrate'].map(function(id) { return document.getElementById(id); });
    buttons.forEach(function(item) { item.disabled = true; });
    button.textContent = existing ? '正在切换…' : '正在迁移…';
    try {
      var resp = await MEFinderApi.fetch(existing ? '/api/data-location/switch' : '/api/data-location/migrate', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target_path: settingsStore.pendingDataLocation})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || action + '失败');
      renderDataLocation({current_path: data.current_path, pending_path: data.target_path, restart_required: true});
      showToast('数据位置已保存，请重启应用');
    } catch (e) {
      showToast(action + '失败：' + e.message);
      buttons.forEach(function(item) { item.disabled = false; });
      button.textContent = action;
    }
  }

  global.loadDataLocation = loadDataLocation;
  global.chooseDataLocation = chooseDataLocation;
  global.migrateDataLocation = migrateDataLocation;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
