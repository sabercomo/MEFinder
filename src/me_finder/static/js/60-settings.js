/* ═══ Appearance settings（可扩展主题引擎，阶段 3-6） ═══
   外观模式（系统/浅/深）+ 浅色与深色各自独立保存的主题选择 + 自定义配色。
   预设与派生逻辑在 05-theme-engine.js；这里只做 DOM 编排与持久化。 */
const THEME_IDS = new Set(THEME_BUILTIN_CSS_IDS);

var appearanceSaveTimer = null;

// 组装当前编辑模式下可选的主题（官方预设 + 该模式的自定义主题）。
function themeChoicesForMode(mode) {
  var list = THEME_PRESETS.filter(function(p) { return p.mode === mode; });
  var custom = settingsStore.appearanceState.customThemes || {};
  Object.keys(custom).forEach(function(id) {
    var def = custom[id];
    if (def && def.mode === mode) {
      list.push({ id: id, name: def.name, label: def.name, mode: mode,
        builtinCss: false, desc: '自定义主题',
        accent: def.accent, highlight: def.highlight,
        background: def.background, foreground: def.foreground,
        contrast: typeof def.contrast === 'number' ? def.contrast : 55, custom: true });
    }
  });
  return list;
}

// 画廊当前编辑的是哪一套（浅/深）：由外观模式直接决定，绝不再是独立的第二维度。
// 只有「跟随系统」需要同时管两套，此时才用 settingsStore.appearanceEditMode 作为「白天/夜晚」子开关。
function currentSlot() {
  if (settingsStore.appearanceState.mode === 'light') return 'light';
  if (settingsStore.appearanceState.mode === 'dark') return 'dark';
  return settingsStore.appearanceEditMode === 'dark' ? 'dark' : 'light';
}

function renderThemeOptions() {
  var container = document.getElementById('theme-options');
  if (!container) return;
  container.innerHTML = themeChoicesForMode(currentSlot()).map(themeOptionMarkup).join('');
  renderThemeSelection();
}

function renderThemeSelection() {
  var selectedId = settingsStore.appearanceState[currentSlot()];
  document.querySelectorAll('.theme-option').forEach(function(option) {
    var selected = option.dataset.themeChoice === selectedId;
    option.classList.toggle('selected', selected);
    option.setAttribute('aria-checked', selected ? 'true' : 'false');
  });
}

// 主题元数据（名字）用于摘要显示：预设查表，自定义查 customThemes。
function themeDisplayName(id) {
  if (settingsStore.appearanceState.customThemes && settingsStore.appearanceState.customThemes[id]) {
    return settingsStore.appearanceState.customThemes[id].name || '自定义主题';
  }
  var p = THEME_PRESET_MAP[id];
  return p ? (p.label || p.name) : id;
}

function updateAppearanceSummary() {
  var el = document.getElementById('appearance-current');
  if (!el) return;
  var modeLabel = settingsStore.appearanceState.mode === 'system' ? '跟随系统'
    : (settingsStore.appearanceState.mode === 'dark' ? '深色' : '浅色');
  var activeId = resolveActiveThemeId(settingsStore.appearanceState, teSystemPrefersDark());
  el.innerHTML = '<span class="settings-theme-dot"></span>' + esc(modeLabel + ' · ' + themeDisplayName(activeId));
}

// 把某个模式预览 mock 涂成对应主题的真实配色（复用引擎派生，token 驱动）。
function paintModePreview(elId, themeId) {
  var el = document.getElementById(elId);
  if (!el) return;
  var def = teLookupThemeDef(themeId);
  if (!def && THEME_PRESET_MAP[themeId]) def = THEME_PRESET_MAP[themeId];
  if (!def) return;
  el.style.cssText = (typeof themePreviewInlineStyle === 'function') ? themePreviewInlineStyle(def) : '';
}

// 三张模式卡：浅色卡显示用户当前浅色主题、深色卡显示深色主题，系统卡两者斜切并置。
function renderModePreviews() {
  paintModePreview('mode-mock-light', settingsStore.appearanceState.light);
  paintModePreview('mode-mock-dark', settingsStore.appearanceState.dark);
  paintModePreview('mode-mock-system-light', settingsStore.appearanceState.light);
  paintModePreview('mode-mock-system-dark', settingsStore.appearanceState.dark);
}

// 只读回显（模式卡预览、白天/夜晚名字与选中态、摘要）——不碰自定义输入框，
// 因此在拖拽调色途中调用也不会把用户正在改的值刷掉。
function refreshAppearanceReadouts() {
  renderModePreviews();
  var pair = document.getElementById('appearance-pair');
  if (pair) {
    var slot = currentSlot();
    var lightName = document.getElementById('appearance-pair-light');
    var darkName = document.getElementById('appearance-pair-dark');
    if (lightName) lightName.textContent = themeDisplayName(settingsStore.appearanceState.light);
    if (darkName) darkName.textContent = themeDisplayName(settingsStore.appearanceState.dark);
    pair.querySelectorAll('.appearance-pair-slot').forEach(function(btn) {
      var on = btn.dataset.appearanceEdit === slot;
      btn.classList.toggle('is-active', on);
      btn.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }
  updateAppearanceSummary();
}

// 外观模式卡、白天/夜晚配对、自定义输入全部与状态对齐。
function syncAppearanceControls() {
  document.querySelectorAll('#appearance-mode-seg .appearance-mode-card').forEach(function(btn) {
    var on = btn.dataset.appearanceMode === settingsStore.appearanceState.mode;
    btn.classList.toggle('is-active', on);
    btn.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  // 「白天/夜晚各用哪套」只在跟随系统时才有意义——只有它需要同时管两套主题。
  var pair = document.getElementById('appearance-pair');
  if (pair) pair.hidden = settingsStore.appearanceState.mode !== 'system';
  refreshAppearanceReadouts();
  syncCustomInputs();
}

// 把当前编辑模式选中主题的基础色回填到自定义输入。
function syncCustomInputs() {
  var def = activeEditDef();
  var accent = document.getElementById('appearance-accent');
  var highlight = document.getElementById('appearance-highlight');
  var background = document.getElementById('appearance-background');
  var foreground = document.getElementById('appearance-foreground');
  var contrast = document.getElementById('appearance-contrast');
  var contrastValue = document.getElementById('appearance-contrast-value');
  if (accent) accent.value = normalizeHexForInput(def.accent);
  if (highlight) highlight.value = normalizeHexForInput(def.highlight || THEME_DEFAULT_HIGHLIGHT[def.mode]);
  if (background) background.value = normalizeHexForInput(def.background);
  if (foreground) foreground.value = normalizeHexForInput(def.foreground);
  if (contrast) contrast.value = String(typeof def.contrast === 'number' ? def.contrast : 55);
  if (contrastValue) contrastValue.textContent = String(typeof def.contrast === 'number' ? def.contrast : 55);
  syncCustomDeleteButton();
}

function syncCustomDeleteButton() {
  var deleteButton = document.getElementById('appearance-delete-custom');
  if (!deleteButton) return;
  var selectedId = settingsStore.appearanceState[currentSlot()];
  deleteButton.hidden = !(settingsStore.appearanceState.customThemes && settingsStore.appearanceState.customThemes[selectedId]);
}

// <input type=color> 只吃 #rrggbb；把 #rgb 或异常值补齐。
function normalizeHexForInput(hex) {
  var rgb = teHexToRgb(hex);
  return rgb ? teRgbToHex(rgb) : '#000000';
}

// 当前编辑模式实际选中的主题定义（预设或自定义），始终返回一份可读对象。
function activeEditDef() {
  var slot = currentSlot();
  var id = settingsStore.appearanceState[slot];
  if (settingsStore.appearanceState.customThemes && settingsStore.appearanceState.customThemes[id]) {
    return settingsStore.appearanceState.customThemes[id];
  }
  var p = THEME_PRESET_MAP[id];
  if (p) return p;
  return THEME_PRESET_MAP[THEME_MODE_DEFAULT[slot]];
}

/* ── 交互 ── */
function setAppearanceMode(mode) {
  if (['system', 'light', 'dark'].indexOf(mode) < 0) return;
  settingsStore.appearanceState.mode = mode;
  // 切进「跟随系统」时，把白天/夜晚子开关对准系统此刻的明暗，画廊即显示当下生效的那套。
  if (mode === 'system') settingsStore.appearanceEditMode = teSystemPrefersDark() ? 'dark' : 'light';
  applyAppearance();
  renderThemeOptions();
  syncAppearanceControls();
  persistAppearance();
}

// 仅「跟随系统」下有效：切换正在查看/编辑的是白天还是夜晚那一套。
function setAppearanceEdit(mode) {
  if (mode !== 'light' && mode !== 'dark') return;
  settingsStore.appearanceEditMode = mode;
  renderThemeOptions();
  syncAppearanceControls();
}

// 选择某个预设/自定义主题作为当前编辑模式的主题。
function selectThemeChoice(id) {
  settingsStore.appearanceState[currentSlot()] = id;
  renderThemeSelection();
  syncCustomInputs();
  if (isEditModeLive()) applyAppearance();
  refreshAppearanceReadouts();
  persistAppearance();
}

// 当前编辑的那一套是否正是屏幕上生效的（据外观模式与系统偏好）。
function isEditModeLive() {
  if (settingsStore.appearanceState.mode === 'light') return currentSlot() === 'light';
  if (settingsStore.appearanceState.mode === 'dark') return currentSlot() === 'dark';
  return currentSlot() === (teSystemPrefersDark() ? 'dark' : 'light');
}

// 展开/收起自定义配色（默认收起——常用路径是选预设，微调是进阶）。
function toggleAppearanceCustom() {
  var toggle = document.getElementById('appearance-custom-toggle');
  var body = document.getElementById('appearance-custom-body');
  if (!toggle || !body) return;
  var open = toggle.getAttribute('aria-expanded') === 'true';
  toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
  body.hidden = open;
  if (!open) syncCustomInputs();
}

// 稳定的自定义主题 id：每个模式一份「快速自定义」槽，避免无限增生。
function quickCustomId(mode) { return 'custom-' + mode; }

// 改动任一颜色/对比度：把当前编辑主题派生成一份自定义副本并即时生效。
function onCustomColorInput() {
  var accent = document.getElementById('appearance-accent');
  var highlight = document.getElementById('appearance-highlight');
  var background = document.getElementById('appearance-background');
  var foreground = document.getElementById('appearance-foreground');
  var contrast = document.getElementById('appearance-contrast');
  var contrastValue = document.getElementById('appearance-contrast-value');
  var slot = currentSlot();
  var base = activeEditDef();
  var id = quickCustomId(slot);
  var wasCustom = settingsStore.appearanceState.customThemes && settingsStore.appearanceState.customThemes[settingsStore.appearanceState[slot]]
    && settingsStore.appearanceState[slot].indexOf('custom') === 0;
  var name = wasCustom ? (activeEditDef().name || '自定义主题') : ('自定义 · ' + (base.label || base.name || ''));
  var def = {
    schemaVersion: THEME_ENGINE_SCHEMA,
    id: id,
    name: name,
    mode: slot,
    accent: accent ? accent.value : base.accent,
    highlight: highlight ? highlight.value : (base.highlight || THEME_DEFAULT_HIGHLIGHT[slot]),
    background: background ? background.value : base.background,
    foreground: foreground ? foreground.value : base.foreground,
    contrast: contrast ? Number(contrast.value) : (base.contrast || 55)
  };
  if (contrastValue && contrast) contrastValue.textContent = String(contrast.value);
  if (!settingsStore.appearanceState.customThemes) settingsStore.appearanceState.customThemes = {};
  settingsStore.appearanceState.customThemes[id] = def;
  settingsStore.appearanceState[slot] = id;
  syncCustomDeleteButton();
  renderThemeOptions();
  if (isEditModeLive()) applyAppearance();
  refreshAppearanceReadouts();
  persistAppearance();
}

// 复制当前主题为一份带时间戳的自定义主题（不改内置定义）。
function duplicateCurrentTheme() {
  var slot = currentSlot();
  var base = activeEditDef();
  var id = 'custom-' + slot + '-' + Date.now().toString(36);
  var def = {
    schemaVersion: THEME_ENGINE_SCHEMA, id: id,
    name: (base.label || base.name || '主题') + ' 副本',
    mode: slot,
    accent: base.accent, highlight: base.highlight || THEME_DEFAULT_HIGHLIGHT[slot],
    background: base.background,
    foreground: base.foreground, contrast: base.contrast || 55
  };
  if (!settingsStore.appearanceState.customThemes) settingsStore.appearanceState.customThemes = {};
  settingsStore.appearanceState.customThemes[id] = def;
  settingsStore.appearanceState[slot] = id;
  renderThemeOptions();
  syncCustomInputs();
  if (isEditModeLive()) applyAppearance();
  refreshAppearanceReadouts();
  persistAppearance();
  showToast('已复制为自定义主题');
}

// 只删除当前选中的自定义主题；内置主题没有删除入口。删除后回到该模式默认主题。
async function deleteCurrentCustomTheme() {
  var slot = currentSlot();
  var id = settingsStore.appearanceState[slot];
  var custom = settingsStore.appearanceState.customThemes || {};
  var def = custom[id];
  if (!def) return;
  var confirmed = await showAppConfirm(
    '将删除自定义主题「' + (def.name || '自定义主题') + '」。此操作无法撤销。',
    { title: '删除自定义主题？', confirmText: '删除', tone: 'danger' }
  );
  if (!confirmed) return;
  delete custom[id];
  settingsStore.appearanceState[slot] = THEME_MODE_DEFAULT[slot];
  if (isEditModeLive()) applyAppearance();
  renderThemeOptions();
  syncAppearanceControls();
  persistAppearance();
  showToast('自定义主题已删除');
}

// 导出当前编辑主题为带版本号的 JSON 文件（用户主动触发的应用内下载）。
function exportCurrentTheme() {
  var def = activeEditDef();
  var payload = themeDefToExport(def);
  var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = 'mefinder-theme-' + (payload.mode) + '.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
}

function triggerThemeImport() {
  var input = document.getElementById('appearance-import-file');
  if (input) { input.value = ''; input.click(); }
}

// 导入主题：解析→校验→存为自定义并选中。非法文件只提示不崩溃。
function onThemeImportFile(event) {
  var file = event && event.target && event.target.files && event.target.files[0];
  if (!file) return;
  var reader = new FileReader();
  reader.onload = function() {
    var raw;
    try { raw = JSON.parse(String(reader.result)); }
    catch (e) { showToast('主题文件不是有效的 JSON'); return; }
    var result = normalizeThemeDef(raw);
    if (!result.ok) { showToast('导入失败：' + result.error); return; }
    var def = result.def;
    var id = 'custom-' + def.mode + '-' + Date.now().toString(36);
    def.id = id;
    if (!settingsStore.appearanceState.customThemes) settingsStore.appearanceState.customThemes = {};
    settingsStore.appearanceState.customThemes[id] = def;
    settingsStore.appearanceState[def.mode] = id;
    // 切到导入主题所属的明暗模式，用户立刻能在画廊里看到并用上它，而不是被藏在另一套里。
    settingsStore.appearanceState.mode = def.mode;
    settingsStore.appearanceEditMode = def.mode;
    applyAppearance();
    renderThemeOptions();
    syncAppearanceControls();
    persistAppearance();
    showToast('已导入主题「' + def.name + '」');
  };
  reader.onerror = function() { showToast('读取主题文件失败'); };
  reader.readAsText(file);
}

// 把 settingsStore.appearanceState 序列化成后端 appearance 结构。
function serializeAppearance() {
  var custom = {};
  var src = settingsStore.appearanceState.customThemes || {};
  Object.keys(src).forEach(function(id) {
    var d = src[id];
    custom[id] = {
      schemaVersion: THEME_ENGINE_SCHEMA, name: d.name, mode: d.mode,
      accent: d.accent,
      highlight: d.highlight || THEME_DEFAULT_HIGHLIGHT[d.mode === 'dark' ? 'dark' : 'light'],
      background: d.background, foreground: d.foreground,
      contrast: typeof d.contrast === 'number' ? d.contrast : 55
    };
    if (d.fontUi) custom[id].fontUi = d.fontUi;
    if (d.fontCode) custom[id].fontCode = d.fontCode;
  });
  return {
    schemaVersion: THEME_ENGINE_SCHEMA,
    mode: settingsStore.appearanceState.mode,
    light: settingsStore.appearanceState.light,
    dark: settingsStore.appearanceState.dark,
    custom_themes: custom
  };
}

// 去抖持久化：同时写 appearance（完整状态）与 legacy theme（内置回退，供首帧/原生）。
function persistAppearance() {
  if (!settingsStore.appearanceReady) return;
  try { localStorage.setItem('meFinderTheme', activeBuiltinFallback()); } catch (_) {}
  if (appearanceSaveTimer) clearTimeout(appearanceSaveTimer);
  appearanceSaveTimer = setTimeout(function() {
    fetch('/api/preferences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ appearance: serializeAppearance(), theme: activeBuiltinFallback() })
    }).then(function(response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
    }).catch(function(error) {
      showToast('主题设置保存失败：' + error.message, 'danger');
    });
  }, 250);
}

function showSettingsCategory(sectionId) {
  var section = document.getElementById(sectionId);
  if (!section) return;
  document.querySelectorAll('.settings-content .settings-section').forEach(function(s) {
    s.classList.toggle('active', s === section);
  });
  document.querySelectorAll('.settings-nav-item').forEach(function(btn) {
    var on = btn.getAttribute('data-target') === sectionId;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  var content = document.querySelector('.settings-content');
  if (content) content.scrollTop = 0;
  if (sectionId === 'statistics-settings' && typeof loadParserStatistics === 'function') {
    loadParserStatistics();
  }
}

// Fall back to the first platform-visible category when the active one is hidden
// (e.g. a macOS-only entry while running the Windows shell, or a plain browser).
function ensureVisibleSettingsCategory() {
  var active = document.querySelector('.settings-nav-item.active');
  if (active && active.offsetParent !== null) return;
  var items = document.querySelectorAll('.settings-nav-item');
  for (var i = 0; i < items.length; i++) {
    if (items[i].offsetParent !== null) {
      showSettingsCategory(items[i].getAttribute('data-target'));
      return;
    }
  }
}

// Backwards-compatible shims for callers that predate the two-pane layout.
function setSettingsSection(sectionId, open) {
  if (open !== false) showSettingsCategory(sectionId);
}

function toggleSettingsSection(sectionId) {
  showSettingsCategory(sectionId);
}

function toggleAppearance() {
  showSettingsCategory('appearance-card');
}

function openVisionSettings() {
  navigateTo('settings');
  showSettingsCategory('vision-api-settings');
  var card = document.getElementById('vision-api-settings');
  if (card) requestAnimationFrame(function() {
    card.scrollIntoView({behavior: 'smooth', block: 'start'});
  });
}


function normalizeCitationStyles(styles) {
  var requested = Array.isArray(styles) ? styles : DEFAULT_CITATION_STYLES;
  var normalized = CITATION_STYLE_OPTIONS.filter(function(option) {
    return requested.indexOf(option.id) >= 0;
  }).map(function(option) { return option.id; });
  return normalized.length ? normalized : DEFAULT_CITATION_STYLES.slice();
}

function loadLocalCitationStyles() {
  try {
    var raw = localStorage.getItem(CITATION_STYLES_STORAGE_KEY);
    if (raw === null) return null;
    var parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    var selected = CITATION_STYLE_OPTIONS.filter(function(option) {
      return parsed.indexOf(option.id) >= 0;
    }).map(function(option) { return option.id; });
    return selected.length ? selected : null;
  } catch (_) {
    return null;
  }
}

function saveLocalCitationStyles(styles) {
  try {
    localStorage.setItem(CITATION_STYLES_STORAGE_KEY, JSON.stringify(normalizeCitationStyles(styles)));
  } catch (_) {}
}

function ensureEnabledCitationStyle() {
  if (enabledCitationStyles.indexOf(citationStyle) < 0) {
    setCitationStyle(enabledCitationStyles[0]);
  }
}

function setCitationStyleControlsDisabled(disabled) {
  var options = document.getElementById('citation-format-options');
  if (options) {
    options.classList.toggle('is-busy', disabled);
    options.setAttribute('aria-busy', disabled ? 'true' : 'false');
  }
  document.querySelectorAll('input[name="citation-format"]').forEach(function(input) {
    input.disabled = disabled;
  });
}

function renderCitationStylePreferences() {
  document.querySelectorAll('.citation-format-option').forEach(function(option) {
    var style = option.dataset.citationStyle;
    var selected = enabledCitationStyles.indexOf(style) >= 0;
    option.classList.toggle('selected', selected);
    var input = option.querySelector('input[name="citation-format"]');
    if (input) input.checked = selected;
  });
  var status = document.getElementById('citation-formats-current');
  if (status) status.textContent = '已启用 ' + enabledCitationStyles.length + ' 种';
}

async function setCitationStyleEnabled(style, checked) {
  if (!CITATION_STYLE_IDS.has(style)) return;
  if (settingsStore.citationStylesSaving || settingsStore.preferencesLoadPromise) {
    renderCitationStylePreferences();
    return;
  }
  var previous = enabledCitationStyles.slice();
  var next = previous.filter(function(item) { return item !== style; });
  if (checked) next.push(style);
  next = normalizeCitationStyles(next);
  if (!checked && previous.length === 1 && previous[0] === style) {
    showToast('至少保留一种引文格式');
    renderCitationStylePreferences();
    return;
  }
  enabledCitationStyles = next;
  saveLocalCitationStyles(enabledCitationStyles);
  ensureEnabledCitationStyle();
  renderCitationStylePreferences();
  if (selectedResult()) showDetail();
  settingsStore.citationStylesSaving = true;
  setCitationStyleControlsDisabled(true);
  try {
    var resp = await fetch('/api/preferences', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({citation_styles: enabledCitationStyles})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    // Keep the just-saved local selection authoritative. This also preserves
    // the setting when an older desktop backend omits citation_styles.
    enabledCitationStyles = normalizeCitationStyles(enabledCitationStyles);
    saveLocalCitationStyles(enabledCitationStyles);
    ensureEnabledCitationStyle();
    renderCitationStylePreferences();
    if (selectedResult()) showDetail();
  } catch (e) {
    enabledCitationStyles = previous;
    saveLocalCitationStyles(enabledCitationStyles);
    ensureEnabledCitationStyle();
    renderCitationStylePreferences();
    if (selectedResult()) showDetail();
    showToast('引文格式保存失败：' + e.message);
  } finally {
    settingsStore.citationStylesSaving = false;
    if (!settingsStore.preferencesLoadPromise) setCitationStyleControlsDisabled(false);
  }
}

function setPdfOpenModeControlsDisabled(disabled) {
  var options = document.querySelector('.pdf-open-options');
  if (options) {
    options.classList.toggle('is-busy', disabled);
    options.setAttribute('aria-busy', disabled ? 'true' : 'false');
  }
  document.querySelectorAll('input[name="pdf-open-mode"]').forEach(function(input) {
    input.disabled = disabled;
  });
}

function renderPdfOpenMode() {
  document.querySelectorAll('.pdf-open-option').forEach(function(option) {
    var selected = option.dataset.pdfOpenChoice === settingsStore.currentPdfOpenMode;
    option.classList.toggle('selected', selected);
    var input = option.querySelector('input[name="pdf-open-mode"]');
    if (input) input.checked = selected;
  });
  var current = document.getElementById('pdf-reader-current');
  if (current) {
    current.className = 'settings-status';
    var systemName = desktopShell === 'win32' ? 'Windows 默认阅读器' : 'macOS 预览';
    current.textContent = settingsStore.currentPdfOpenMode === 'system' ? systemName : '应用内阅读器';
  }
}

function setDocumentExportModeControlsDisabled(disabled) {
  var options = document.getElementById('document-export-options');
  if (options) {
    options.classList.toggle('is-busy', disabled);
    options.setAttribute('aria-busy', disabled ? 'true' : 'false');
  }
  document.querySelectorAll('input[name="document-export-mode"]').forEach(function(input) {
    input.disabled = disabled;
  });
}

function renderDocumentExportMode() {
  document.querySelectorAll('[data-document-export-choice]').forEach(function(option) {
    var selected = option.dataset.documentExportChoice === settingsStore.currentDocumentExportMode;
    option.classList.toggle('selected', selected);
    var input = option.querySelector('input[name="document-export-mode"]');
    if (input) input.checked = selected;
  });
  var current = document.getElementById('document-export-current');
  if (current) {
    current.className = 'settings-status';
    current.textContent = settingsStore.currentDocumentExportMode === 'with_pdf'
      ? '包含原 PDF'
      : '仅文档数据';
  }
}

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
    var resp = await fetch('/api/macos-update', {cache: 'no-store'});
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

function renderDataLocation(data) {
  var badge = document.getElementById('data-location-status');
  var current = document.getElementById('data-location-current');
  if (!badge || !current) return;
  current.textContent = data.current_path || '未知位置';
  current.title = data.current_path || '';
  badge.className = 'settings-status' + (data.is_custom ? ' ready' : '');
  badge.textContent = data.is_custom ? '自定义位置' : '默认位置';
}

async function loadDataLocation() {
  if (!document.getElementById('data-location-settings')) return;
  var errorBox = document.getElementById('data-location-error');
  var badge = document.getElementById('data-location-status');
  if (badge) { badge.className = 'settings-status'; badge.textContent = '读取中…'; }
  try {
    var resp = await fetch('/api/data-location', {cache: 'no-store'});
    var data = await resp.json();
    if (resp.status === 404 || data.available === false) {
      delete document.documentElement.dataset.dataLocationAvailable;
      settingsStore.dataLocationLoaded = true;
      ensureVisibleSettingsCategory();
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

function renderPendingDataLocation(targetPath) {
  settingsStore.pendingDataLocation = targetPath || '';
  var pending = document.getElementById('data-location-pending');
  var target = document.getElementById('data-location-target');
  if (pending) pending.style.display = settingsStore.pendingDataLocation ? 'flex' : 'none';
  if (target) {
    target.textContent = settingsStore.pendingDataLocation;
    target.title = settingsStore.pendingDataLocation;
  }
}

async function chooseDataLocation() {
  var button = document.getElementById('data-location-choose');
  if (button && button.disabled) return;
  if (button) {
    button.disabled = true;
    button.textContent = '正在选择…';
  }
  try {
    var resp = await fetch('/api/data-location/choose', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '选择位置失败');
    if (!data.cancelled) {
      renderPendingDataLocation(data.target_path);
      showToast('已选择新位置，确认后开始迁移');
    }
  } catch (e) {
    showToast('选择数据位置失败：' + e.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = '选择位置';
    }
  }
}

async function migrateDataLocation() {
  if (!settingsStore.pendingDataLocation) return;
  if (!await showAppConfirm(
    '将把索引、语料和本机设置复制到：\n\n'
    + settingsStore.pendingDataLocation
    + '\n\n迁移期间请不要关闭应用。完成后需要重启，旧位置的数据会保留',
    {title:'迁移数据位置？', confirmText:'开始迁移', tone:'warning'}
  )) return;
  var button = document.getElementById('data-location-migrate');
  var choose = document.getElementById('data-location-choose');
  if (button) {
    button.disabled = true;
    button.textContent = '正在迁移…';
  }
  if (choose) choose.disabled = true;
  try {
    var resp = await fetch('/api/data-location/migrate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({target_path: settingsStore.pendingDataLocation})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '迁移失败');
    var badge = document.getElementById('data-location-status');
    if (badge) {
      badge.className = 'settings-status warning';
      badge.textContent = '重启后生效';
    }
    var pending = document.getElementById('data-location-pending');
    var hint = pending ? pending.querySelector('small') : null;
    if (hint) hint.textContent = '迁移完成。退出并重新打开应用后将使用此位置；旧位置的数据仍保留';
    if (button) button.style.display = 'none';
    showToast('数据迁移完成，请重启应用');
  } catch (e) {
    showToast('迁移数据失败：' + e.message);
    if (button) {
      button.disabled = false;
      button.textContent = '迁移并切换';
    }
    if (choose) choose.disabled = false;
  }
}

function applyPreferencesData(data, requestedThemeRevision) {
  // 从后端 appearance 恢复外观引擎状态，并即时解析生效（覆盖服务端首帧回退）。
  loadAppearanceFromPreferences(data.appearance);
  if (data.library_view === 'list' || data.library_view === 'grid') libraryStore.viewMode = data.library_view;
  else if (data.calibration_view === 'list' || data.calibration_view === 'grid') libraryStore.viewMode = data.calibration_view;
  // 后端为准（随数据迁移）：文献默认语言与联网自动匹配阈值（C-01）。
  if (data.lib_default_language === 'chinese' || data.lib_default_language === 'foreign') {
    libraryStore.defaultLanguage = data.lib_default_language;
    try { localStorage.setItem('meFinderLibDefaultLanguage', libraryStore.defaultLanguage); } catch (_) {}
    syncLibDefaultLanguageControl();
  }
  if (typeof data.online_auto_match_threshold === 'number') {
    onlineMetadataAutoMatchThreshold = data.online_auto_match_threshold;
    try { localStorage.setItem('meFinderOnlineAutoMatchThreshold', String(Math.round(onlineMetadataAutoMatchThreshold * 100))); } catch (_) {}
    syncOnlineAutoMatchControl();
  }
  settingsStore.currentPdfOpenMode = data.pdf_open_mode === 'system' ? 'system' : 'native';
  settingsStore.currentPdfParseMode = normalizePdfParseMode(data.pdf_parse_mode);
  settingsStore.currentDocumentExportMode = data.document_export_mode === 'with_pdf'
    ? 'with_pdf'
    : 'data_only';
  settingsStore.autoUpdateEnabled = data.auto_update === true;
  enabledCitationStyles = normalizeCitationStyles(loadLocalCitationStyles() || data.citation_styles);
  saveLocalCitationStyles(enabledCitationStyles);
  setCitationStyle(loadLocalSelectedCitationStyle() || data.citation_style || enabledCitationStyles[0], false);
  ensureEnabledCitationStyle();
  var autoUpdateInput = document.getElementById('auto-update-enabled');
  if (autoUpdateInput) autoUpdateInput.checked = settingsStore.autoUpdateEnabled;
  renderPdfOpenMode();
  renderPdfParseMode();
  renderDocumentExportMode();
  renderCitationStylePreferences();
  settingsStore.scanDirectories = Array.isArray(data.scan_directories) ? data.scan_directories : [];
  renderScanDirectories();
  syncLibraryViewButtons();
  if (libraryStore.loaded) renderLibraryList();
  settingsStore.preferencesLoaded = true;
}

function configureDesktopPlatformOptions() {
  var nativeDescription = document.getElementById('pdf-native-description');
  var systemTitle = document.getElementById('pdf-system-title');
  var systemDescription = document.getElementById('pdf-system-description');
  if (desktopShell === 'win32') {
    if (nativeDescription) nativeDescription.textContent = '使用 Microsoft Edge WebView2，在应用内直接跳到搜索命中的物理页码';
    if (systemTitle) systemTitle.textContent = 'Windows 默认 PDF 阅读器';
    if (systemDescription) systemDescription.textContent = '默认阅读器为 Adobe Acrobat 或 Reader 时直接跳到命中页；WPS 等其他阅读器按 Windows 设置打开';
  } else if (desktopShell === 'macos') {
    if (nativeDescription) nativeDescription.textContent = '使用 macOS PDFKit，直接跳到搜索命中的物理页码';
    if (systemTitle) systemTitle.textContent = 'macOS 预览';
    if (systemDescription) systemDescription.textContent = '在预览.app 中打开；命中页码需要手动翻到';
  }
}

async function setPdfOpenMode(mode) {
  if (mode !== 'native' && mode !== 'system') return;
  if (settingsStore.pdfOpenModeSaving || settingsStore.preferencesLoadPromise) {
    renderPdfOpenMode();
    return;
  }
  var previousMode = settingsStore.currentPdfOpenMode;
  settingsStore.currentPdfOpenMode = mode;
  settingsStore.pdfOpenModeSaving = true;
  setPdfOpenModeControlsDisabled(true);
  renderPdfOpenMode();
  try {
    var resp = await fetch('/api/preferences', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pdf_open_mode: mode})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    settingsStore.currentPdfOpenMode = data.pdf_open_mode === 'system' ? 'system' : 'native';
    settingsStore.preferencesLoaded = true;
    renderPdfOpenMode();
    // Visible success: the radio已经跳过去了，无需再弹 Toast。只在失败时提示。
  } catch (e) {
    settingsStore.currentPdfOpenMode = previousMode;
    renderPdfOpenMode();
    showToast('PDF 打开方式保存失败：' + e.message);
  } finally {
    settingsStore.pdfOpenModeSaving = false;
    if (!settingsStore.preferencesLoadPromise) setPdfOpenModeControlsDisabled(false);
  }
}

async function setDocumentExportMode(mode) {
  if (mode !== 'data_only' && mode !== 'with_pdf') return;
  if (settingsStore.documentExportModeSaving || settingsStore.preferencesLoadPromise) {
    renderDocumentExportMode();
    return;
  }
  var previousMode = settingsStore.currentDocumentExportMode;
  settingsStore.currentDocumentExportMode = mode;
  settingsStore.documentExportModeSaving = true;
  setDocumentExportModeControlsDisabled(true);
  renderDocumentExportMode();
  try {
    var resp = await fetch('/api/preferences', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({document_export_mode: mode})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    settingsStore.currentDocumentExportMode = data.document_export_mode === 'with_pdf'
      ? 'with_pdf'
      : 'data_only';
    settingsStore.preferencesLoaded = true;
    renderDocumentExportMode();
  } catch (e) {
    settingsStore.currentDocumentExportMode = previousMode;
    renderDocumentExportMode();
    showToast('文档包导出设置保存失败：' + e.message);
  } finally {
    settingsStore.documentExportModeSaving = false;
    if (!settingsStore.preferencesLoadPromise) setDocumentExportModeControlsDisabled(false);
  }
}

async function loadPreferences() {
  if (settingsStore.preferencesLoadPromise) return settingsStore.preferencesLoadPromise;
  if (settingsStore.pdfOpenModeSaving || settingsStore.documentExportModeSaving) return null;
  var requestedThemeRevision = settingsStore.themeRevision;
  renderThemeSelection();
  renderPdfOpenMode();
  renderPdfParseMode();
  renderDocumentExportMode();
  renderCitationStylePreferences();
  syncOnlineAutoMatchControl();
  syncLibDefaultLanguageControl();
  setPdfOpenModeControlsDisabled(true);
  setDocumentExportModeControlsDisabled(true);
  setCitationStyleControlsDisabled(true);
  var current = document.getElementById('pdf-reader-current');
  if (current) {
    current.className = 'settings-status';
    current.textContent = '读取中…';
  }
  settingsStore.preferencesLoadPromise = (async function() {
    try {
      var resp = await fetch('/api/preferences');
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
      applyPreferencesData(data, requestedThemeRevision);
      if (desktopShell === 'win32') {
        loadUpdateStatus().then(function(state) {
          if (settingsStore.autoUpdateEnabled && state && state.can_self_update && !settingsStore.updateAutoStarted) {
            settingsStore.updateAutoStarted = true;
            checkForUpdates(true);
          }
        });
      }
    } catch (e) {
      var failedStatus = document.getElementById('pdf-reader-current');
      if (failedStatus) {
        failedStatus.className = 'settings-status warning';
        failedStatus.textContent = '读取失败';
      }
      showToast('读取应用设置失败：' + e.message);
    } finally {
      settingsStore.preferencesLoadPromise = null;
      if (!settingsStore.pdfOpenModeSaving) setPdfOpenModeControlsDisabled(false);
      if (!settingsStore.documentExportModeSaving) setDocumentExportModeControlsDisabled(false);
      if (!settingsStore.citationStylesSaving) setCitationStyleControlsDisabled(false);
    }
  })();
  return settingsStore.preferencesLoadPromise;
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
    var resp = await fetch('/api/update/status');
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
    var resp = await fetch('/api/update/check', {
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
      var downloadResp = await fetch('/api/update/download', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
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
    var installResp = await fetch('/api/update/install', {
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
    var resp = await fetch('/api/preferences', {
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


function renderScanDirectories() {
  var container = document.getElementById('scan-dir-list');
  if (!container) return;
  if (!settingsStore.scanDirectories.length) {
    container.innerHTML = '<div class="scan-dir-empty">还没有添加文献文件夹</div>';
    return;
  }
  container.innerHTML = settingsStore.scanDirectories.map(function(dir, index) {
    return '<div class="scan-dir-row" title="' + esc(dir) + '">'
      + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>'
      + '<span class="scan-dir-row-path">' + esc(dir) + '</span>'
      + '<button class="scan-dir-remove" type="button" aria-label="移除目录" onclick="removeScanDirectory(' + index + ')">移除</button>'
      + '</div>';
  }).join('');
}

// The desktop shells expose a real folder picker; a plain browser session has
// no way to resolve an absolute path, so it keeps the manual path field.
function setupScanDirectoryControls() {
  var pick = document.getElementById('scan-dir-pick');
  var input = document.getElementById('scan-dir-input');
  if (!pick || !input) return;
  if (desktopShell) {
    input.hidden = true;
  } else {
    pick.hidden = true;
    input.placeholder = '粘贴文件夹路径后回车…';
  }
}

function revealScanPathFallback() {
  var input = document.getElementById('scan-dir-input');
  if (input) input.hidden = false;
}

async function chooseScanDirectory() {
  var button = document.getElementById('scan-dir-pick');
  if (button && button.disabled) return;
  if (button) button.disabled = true;
  try {
    var resp = await fetch('/api/scan-directories/choose', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '选择文件夹失败');
    if (data.cancelled) return;
    var folders = Array.isArray(data.folders) ? data.folders : [data.folder];
    await addScanDirectoryPaths(folders);
  } catch (e) {
    revealScanPathFallback();
    showToast('选择文件夹失败：' + e.message);
  } finally {
    if (button) button.disabled = false;
  }
}

async function addScanDirectoryPaths(values) {
  var candidates = Array.isArray(values) ? values : [values];
  var added = [];
  candidates.forEach(function(value) {
    var path = String(value || '').trim();
    if (!path || settingsStore.scanDirectories.indexOf(path) !== -1 || added.indexOf(path) !== -1) return;
    added.push(path);
  });
  if (!added.length) {
    showToast('该文件夹已在列表中');
    return;
  }
  var previous = settingsStore.scanDirectories.slice();
  settingsStore.scanDirectories = settingsStore.scanDirectories.concat(added);
  try {
    await persistScanDirectories();
    var savedCount = settingsStore.scanDirectories.filter(function(path) {
      return previous.indexOf(path) === -1;
    }).length;
    var omittedCount = added.length - savedCount;
    if (omittedCount > 0) {
      var message = savedCount > 0
        ? '已添加 ' + savedCount + ' 个；另 ' + omittedCount + ' 个超过目录数量上限，未保存'
        : '目录数量已达上限，本次选择未保存';
      showToast(message, 'danger');
    } else {
      showToast(savedCount === 1 ? '已添加文献文件夹' : '已添加 ' + savedCount + ' 个文献文件夹');
    }
  } catch (e) {
    settingsStore.scanDirectories = previous;
    renderScanDirectories();
    throw e;
  }
}

async function addScanDirectoryPath(value) {
  await addScanDirectoryPaths([value]);
}

async function persistScanDirectories() {
  var resp = await fetch('/api/preferences', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({scan_directories: settingsStore.scanDirectories})
  });
  var data = await resp.json();
  if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
  settingsStore.scanDirectories = Array.isArray(data.scan_directories) ? data.scan_directories : settingsStore.scanDirectories;
  renderScanDirectories();
}

async function addScanDirectory() {
  var input = document.getElementById('scan-dir-input');
  var value = (input.value || '').trim();
  if (!value) return;
  try {
    await addScanDirectoryPath(value);
    input.value = '';
  } catch (e) {
    showToast('保存失败：' + e.message, 'danger');
  }
}

async function removeScanDirectory(index) {
  var removed = settingsStore.scanDirectories[index];
  settingsStore.scanDirectories = settingsStore.scanDirectories.filter(function(_, i) { return i !== index; });
  try {
    await persistScanDirectories();
    showToast('已移除目录');
  } catch (e) {
    if (removed != null) settingsStore.scanDirectories.splice(index, 0, removed);
    renderScanDirectories();
    showToast('移除失败：' + e.message);
  }
}

// 显式保存区块的“未保存”提示（C-02）：把对应 hint 文案改成提醒；
// 保存/重载函数写自己的文案时会覆盖它，无需单独清除。
function markSettingsSectionDirty(hintId) {
  var el = document.getElementById(hintId);
  if (el) el.textContent = '有未保存的修改，记得点保存';
}

function persistDisplayPreference(key, value) {
  var payload = {};
  payload[key] = value;
  fetch('/api/preferences', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).catch(function() {});
}

// 从后端 appearance 恢复外观状态。缺字段则退回安全默认，绝不因坏数据崩溃。
function loadAppearanceFromPreferences(appearance) {
  if (appearance && typeof appearance === 'object') {
    settingsStore.appearanceState.mode = ['system', 'light', 'dark'].indexOf(appearance.mode) >= 0 ? appearance.mode : 'system';
    settingsStore.appearanceState.light = typeof appearance.light === 'string' && appearance.light ? appearance.light : 'frost-blue';
    settingsStore.appearanceState.dark = typeof appearance.dark === 'string' && appearance.dark ? appearance.dark : 'midnight';
    var custom = {};
    var src = appearance.custom_themes;
    if (src && typeof src === 'object') {
      Object.keys(src).forEach(function(id) {
        var result = normalizeThemeDef(src[id]);
        if (result.ok) { result.def.id = id; custom[id] = result.def; }
      });
    }
    settingsStore.appearanceState.customThemes = custom;
  }
  // 选中主题若已不存在（如被清理），退回该模式默认，避免空引用。
  ['light', 'dark'].forEach(function(m) {
    var id = settingsStore.appearanceState[m];
    if (!THEME_PRESET_MAP[id] && !(settingsStore.appearanceState.customThemes && settingsStore.appearanceState.customThemes[id])) {
      settingsStore.appearanceState[m] = THEME_MODE_DEFAULT[m];
    }
  });
  settingsStore.appearanceReady = true;
  // 编辑标签默认对准当前生效的模式。
  settingsStore.appearanceEditMode = settingsStore.appearanceState.mode === 'dark'
    ? 'dark'
    : (settingsStore.appearanceState.mode === 'light' ? 'light' : (teSystemPrefersDark() ? 'dark' : 'light'));
  applyAppearance();
  renderThemeOptions();
  syncAppearanceControls();
}

// 首帧后先按当前 data-theme 渲染一份，偏好载入后再校正。
initAppearanceSystemWatch();
renderThemeOptions();
syncAppearanceControls();
