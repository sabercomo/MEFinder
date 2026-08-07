/* ═══ Windows frameless titlebar ═══ */
function callWindowsWindow(method) {
  if (!window.pywebview || !window.pywebview.api || typeof window.pywebview.api[method] !== 'function') {
    return Promise.resolve(false);
  }
  return window.pywebview.api[method]();
}

function setWindowsMaximized(maximized) {
  document.documentElement.classList.toggle('windows-maximized', !!maximized);
  var button = document.querySelector('.windows-maximize-button');
  if (!button) return;
  button.setAttribute('aria-label', maximized ? '还原窗口' : '最大化窗口');
  button.title = maximized ? '还原' : '最大化';
}

function minimizeWindowsWindow() {
  callWindowsWindow('minimize');
}

function toggleWindowsMaximize() {
  callWindowsWindow('toggle_maximize').then(setWindowsMaximized);
}

function closeWindowsWindow() {
  callWindowsWindow('close');
}

window.addEventListener('pywebviewready', function() {
  if (desktopShell === 'win32') {
    callWindowsWindow('is_maximized').then(setWindowsMaximized);
  }
});

/* ═══ Theme-aware confirmation / information dialog ═══ */
function openAppDialog(message, options) {
  options = options || {};
  var backdrop = document.getElementById('app-dialog-backdrop');
  var dialog = document.getElementById('app-dialog');
  var title = document.getElementById('app-dialog-title');
  var messageElement = document.getElementById('app-dialog-message');
  var cancelButton = document.getElementById('app-dialog-cancel');
  var confirmButton = document.getElementById('app-dialog-confirm');
  if (!backdrop || !dialog || !title || !messageElement || !cancelButton || !confirmButton) {
    return Promise.resolve(false);
  }
  if (appDialogResolve) {
    var previousResolve = appDialogResolve;
    appDialogResolve = null;
    previousResolve(false);
  }
  var showCancel = options.showCancel !== false;
  var tone = ['info', 'warning', 'danger'].indexOf(options.tone) >= 0 ? options.tone : 'info';
  dialog.dataset.tone = tone;
  title.textContent = options.title || (showCancel ? '请确认' : '提示');
  messageElement.textContent = String(message || '');
  cancelButton.hidden = !showCancel;
  cancelButton.textContent = options.cancelText || '取消';
  confirmButton.textContent = options.confirmText || (showCancel ? '确定' : '知道了');
  confirmButton.className = 'action-btn ' + (tone === 'danger' ? '' : 'primary');
  appDialogPreviousFocus = document.activeElement;
  backdrop.classList.add('open');
  backdrop.setAttribute('aria-hidden', 'false');
  return new Promise(function(resolve) {
    appDialogResolve = resolve;
    setTimeout(function() { confirmButton.focus(); }, 0);
  });
}

function showAppConfirm(message, options) {
  return openAppDialog(message, Object.assign({showCancel: true}, options || {}));
}

function showAppAlert(message, options) {
  return openAppDialog(message, Object.assign({showCancel: false}, options || {}));
}

function settleAppDialog(accepted) {
  var backdrop = document.getElementById('app-dialog-backdrop');
  if (!backdrop || !backdrop.classList.contains('open')) return;
  backdrop.classList.remove('open');
  backdrop.setAttribute('aria-hidden', 'true');
  var resolve = appDialogResolve;
  appDialogResolve = null;
  var previousFocus = appDialogPreviousFocus;
  appDialogPreviousFocus = null;
  if (previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
  if (resolve) resolve(!!accepted);
}

function appDialogBackdropClick(event) {
  if (event.target && event.target.id === 'app-dialog-backdrop') settleAppDialog(false);
}

document.addEventListener('keydown', function(event) {
  if (event.key !== 'Escape') return;
  var cnkiBatch = document.getElementById('cnki-batch-modal');
  if (cnkiBatch && cnkiBatch.classList.contains('open')) {
    event.preventDefault();
    resolveCnkiBatchChoice({action:'skip'});
    return;
  }
  var backdrop = document.getElementById('app-dialog-backdrop');
  if (backdrop && backdrop.classList.contains('open')) {
    event.preventDefault();
    settleAppDialog(false);
  }
});

/* ═══ Navigation ═══ */
function navigateTo(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.sidebar-item').forEach(a => a.classList.remove('active'));
  const target = document.getElementById('page-' + page);
  if (target) target.classList.add('active');
  const link = document.querySelector('.sidebar-item[data-page="' + page + '"]');
  if (link) link.classList.add('active');
  if (page === 'library' && !libLoaded) loadLibrary();
  if (page === 'import' && !visionConfigLoaded) loadVisionProviders();
  if (page === 'settings') {
    ensureVisibleSettingsCategory();
    if (!preferencesLoaded) loadPreferences();
    if (!mineruConfigLoaded) loadMineruConfig();
    if (!visionConfigLoaded) loadVisionProviders();
    if (!dataLocationLoaded) loadDataLocation();
  }
}

function toggleSidebar(force) {
  var collapsed = typeof force === 'boolean'
    ? force
    : !document.documentElement.classList.contains('sidebar-collapsed');
  document.documentElement.classList.toggle('sidebar-collapsed', collapsed);
  var btn = document.querySelector('.sidebar-collapse-btn');
  if (btn) {
    var label = collapsed ? '展开侧边栏' : '收起侧边栏';
    btn.setAttribute('aria-label', label);
    btn.setAttribute('title', label);
  }
  try { localStorage.setItem('meFinderSidebarCollapsed', collapsed ? '1' : '0'); } catch (_) {}
}
(function syncSidebarToggle() {
  var collapsed = document.documentElement.classList.contains('sidebar-collapsed');
  var btn = document.querySelector('.sidebar-collapse-btn');
  if (btn) {
    var label = collapsed ? '展开侧边栏' : '收起侧边栏';
    btn.setAttribute('aria-label', label);
    btn.setAttribute('title', label);
  }
})();

