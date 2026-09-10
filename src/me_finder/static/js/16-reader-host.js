(function (global) {  // module: 16-reader-host.js
  'use strict';

  var enabled = false;
  var loaded = false;
  var saving = false;

  function renderReaderWindowSetting() {
    var input = document.getElementById('reader-window-enabled');
    input.checked = enabled;
    input.disabled = !loaded || saving;
  }

  function syncReaderWindowPreferences(data) {
    enabled = data.reader_window_enabled === true;
    loaded = true;
    renderReaderWindowSetting();
  }

  async function setReaderWindowEnabled(value) {
    if (!loaded || saving) return;
    saving = true;
    renderReaderWindowSetting();
    try {
      var response = await fetch('/api/preferences', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reader_window_enabled: value === true})
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '保存失败');
      enabled = data.reader_window_enabled === true;
    } catch (error) {
      showToast('独立阅读窗口设置保存失败：' + error.message);
    } finally {
      saving = false;
      renderReaderWindowSetting();
    }
  }

  async function openNativeReader(options) {
    if (!loaded) await loadPreferences();
    if (!loaded) throw new Error('阅读设置尚未加载，请稍后重试');
    if (!enabled || !['macos', 'win32'].includes(desktopShell)) return false;
    if (!global.pywebview || typeof global.pywebview.api.open_reader !== 'function') {
      await new Promise(function (resolve) {
        global.addEventListener('pywebviewready', resolve, {once: true});
      });
    }
    return global.pywebview.api.open_reader(JSON.parse(JSON.stringify(options)));
  }

  document.addEventListener('DOMContentLoaded', function () {
    global.MEFinderReader.configure({openExternal: openNativeReader});
  }, {once: true});

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.readerHost = Object.freeze({
    syncPreferences: syncReaderWindowPreferences,
    setEnabled: setReaderWindowEnabled
  });
}(typeof window !== 'undefined' ? window : globalThis));
