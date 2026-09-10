(function (global) {
  'use strict';

  function notify(message) {
    var toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    document.getElementById('toast-container').appendChild(toast);
    global.setTimeout(function () { toast.remove(); }, 5000);
  }

  async function refreshPreferences() {
    var response = await fetch('/api/preferences');
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '读取设置失败');
    var appearance = data.appearance;
    var custom = {};
    Object.keys(appearance.custom_themes).forEach(function (id) {
      custom[id] = Object.assign({id: id}, appearance.custom_themes[id]);
    });
    settingsStore.appearanceState = {
      mode: appearance.mode, light: appearance.light, dark: appearance.dark,
      customThemes: custom
    };
    global.applyAppearance();
    document.documentElement.dataset.readerLineMode = data.reader_line_mode;
  }

  global.addEventListener('pywebviewready', async function () {
    var status = document.getElementById('reader-window-status');
    try {
      await refreshPreferences();
      global.initAppearanceSystemWatch();
      global.MEFinderReader.configure({
        notify: notify,
        onClose: function () { global.pywebview.state.readerClosed = true; }
      });
      var options = await global.pywebview.api.reader_options();
      if (!await global.MEFinderReader.restore()) await global.MEFinderReader.open(options);
      status.hidden = true;
      global.addEventListener('focus', function () {
        refreshPreferences().catch(function (error) { notify(error.message); });
      });
    } catch (error) {
      status.textContent = '阅读窗口打开失败：' + error.message;
      notify(status.textContent);
    }
  }, {once: true});
}(window));
