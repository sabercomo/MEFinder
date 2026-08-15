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

async function chooseDesktopExportDirectory() {
  if (desktopShell !== 'macos' && desktopShell !== 'win32') return undefined;
  var response = await fetch('/api/export-directory/choose', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}'
  });
  var data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || '选择导出文件夹失败');
  if (data.cancelled) return null;
  if (!data.path) throw new Error('没有收到所选导出文件夹。');
  return data.path;
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

// 统一 Esc 栈（G-02）：按从最内层到最外层的顺序，一次 Esc 关一层。
document.addEventListener('keydown', function(event) {
  if (event.key !== 'Escape') return;
  // 1. 开着的下拉 / 菜单先关。
  if (document.querySelector('.app-select.is-open, .bib-menu.open')) {
    event.preventDefault();
    if (typeof closeAppSelects === 'function') closeAppSelects();
    if (typeof bibCloseMenus === 'function') bibCloseMenus();
    return;
  }
  // 2. 知网批量选择弹窗。
  var cnkiBatch = document.getElementById('cnki-batch-modal');
  if (cnkiBatch && cnkiBatch.classList.contains('open')) {
    event.preventDefault();
    resolveCnkiBatchChoice({action:'skip'});
    return;
  }
  // 3. 通用确认 / 提示对话框。
  var backdrop = document.getElementById('app-dialog-backdrop');
  if (backdrop && backdrop.classList.contains('open')) {
    event.preventDefault();
    settleAppDialog(false);
    return;
  }
  // 4. 移除确认弹窗（移除进行中不响应 Esc，避免中止事务）。
  var removeModal = document.getElementById('remove-document-modal');
  if (removeModal && removeModal.classList.contains('open')) {
    if (typeof removeRequestController !== 'undefined' && removeRequestController) return;
    event.preventDefault();
    closeRemoveDocumentModal();
    return;
  }
  // 5. 文献库批量选择态：先退选择。
  if (typeof libDeleteSelection !== 'undefined' && libDeleteSelection && libDeleteSelection.size > 0) {
    event.preventDefault();
    clearLibrarySelection();
    return;
  }
  // 6. 文献详情抽屉：带脏检查关闭。
  var drawer = document.getElementById('library-drawer');
  if (drawer && drawer.classList.contains('open')) {
    event.preventDefault();
    requestCloseLibDrawer();
  }
});

/* ═══ Navigation ═══ */
function navigateTo(page) {
  // 「返回搜索」横幅只在从检索结果跳来补书目时点亮；任何导航都先清掉（S-03）。
  var returnBanner = document.getElementById('library-return-banner');
  if (returnBanner) returnBanner.hidden = true;
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
