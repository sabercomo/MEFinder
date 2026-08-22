/* ── Theme Engine ──────────────────────────────────────────────
   可扩展主题引擎（阶段 3-6）。核心是一组**纯函数**：由用户设定的
   accent / background / foreground / contrast 派生出全部 ~50 个语义色
   token，普通组件因此永远只依赖 token，不需要为新主题改一行组件 CSS。

   设计约束（见 tests/test_theme_engine.py）：
   * 纯函数不碰 DOM，输入完全由参数决定，node 可单测。
   * 内置的 6 套 data-theme CSS 块保持不变（首帧/原生标题栏回退），
     新增预设与自定义主题一律靠这里运行时派生并注入一个样式元素，
     绝不新增散落的 CSS 主题块。
   * 派生带对比度守护：用户设了前景≈背景也不会白字白底。
   ────────────────────────────────────────────────────────────── */

var THEME_ENGINE_SCHEMA = 1;

/* ── 颜色基础运算（纯） ── */
function teClamp(n) { return n < 0 ? 0 : (n > 255 ? 255 : Math.round(n)); }

function teHexToRgb(hex) {
  if (typeof hex !== 'string') return null;
  var s = hex.trim().replace(/^#/, '');
  if (/^[0-9a-fA-F]{3}$/.test(s)) {
    s = s[0] + s[0] + s[1] + s[1] + s[2] + s[2];
  }
  if (!/^[0-9a-fA-F]{6}$/.test(s)) return null;
  return {
    r: parseInt(s.slice(0, 2), 16),
    g: parseInt(s.slice(2, 4), 16),
    b: parseInt(s.slice(4, 6), 16)
  };
}

function teRgbToHex(rgb) {
  function h(v) { var t = teClamp(v).toString(16); return t.length === 1 ? '0' + t : t; }
  return '#' + h(rgb.r) + h(rgb.g) + h(rgb.b);
}

// 是否为合法颜色字符串（#rgb/#rrggbb）。导入校验用。
function teIsHex(hex) { return teHexToRgb(hex) !== null; }

// 线性混合：t=0 全 a，t=1 全 b。
function teMix(a, b, t) {
  var ra = teHexToRgb(a), rb = teHexToRgb(b);
  if (!ra || !rb) return a;
  t = t < 0 ? 0 : (t > 1 ? 1 : t);
  return teRgbToHex({
    r: ra.r + (rb.r - ra.r) * t,
    g: ra.g + (rb.g - ra.g) * t,
    b: ra.b + (rb.b - ra.b) * t
  });
}

function teLighten(hex, t) { return teMix(hex, '#ffffff', t); }
function teDarken(hex, t) { return teMix(hex, '#000000', t); }

// rgba(...) 字符串。alpha 0..1。
function teAlpha(hex, a) {
  var c = teHexToRgb(hex);
  if (!c) return hex;
  var av = Math.round(a * 100) / 100;
  return 'rgba(' + c.r + ',' + c.g + ',' + c.b + ',' + av + ')';
}

// 相对亮度（WCAG）。
function teLuminance(hex) {
  var c = teHexToRgb(hex);
  if (!c) return 0;
  function ch(v) { v = v / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
  return 0.2126 * ch(c.r) + 0.7152 * ch(c.g) + 0.0722 * ch(c.b);
}

// WCAG 对比度（1..21）。
function teContrast(a, b) {
  var la = teLuminance(a), lb = teLuminance(b);
  var hi = Math.max(la, lb), lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

// 在 fg 与 bg 对比不足时，把 fg 朝黑或白推，直到达标或推到极限。
// 保证可读：用户把前景设得几乎等于背景也不会白字白底。
function teEnsureReadable(fg, bg, minRatio) {
  if (teContrast(fg, bg) >= minRatio) return fg;
  // 背景偏亮就把前景压黑，背景偏暗就把前景提白。
  var target = teLuminance(bg) > 0.4 ? '#000000' : '#ffffff';
  var out = fg;
  for (var i = 1; i <= 20; i++) {
    out = teMix(fg, target, i / 20);
    if (teContrast(out, bg) >= minRatio) return out;
  }
  return target;
}

// 在给定背景上取一个可读的强调文字色（accent 太浅/太深时朝可读方向推）。
function teReadableAccentText(accent, bg, minRatio) {
  return teEnsureReadable(accent, bg, minRatio);
}

// accent 上应放黑字还是白字：取对比更高者，保证按钮文字始终清晰。
function teAccentContrast(accent) {
  var white = '#ffffff';
  var dark = '#0b1017';
  return teContrast(accent, white) >= teContrast(accent, dark) ? white : dark;
}

/* ── 由基础色派生完整 token 集（纯） ──
   返回 { '--app-bg': '#...', ... } 覆盖全部语义色 token。
   def: { mode, accent, background, foreground, contrast } */
function deriveThemeTokens(def) {
  var mode = def.mode === 'dark' ? 'dark' : 'light';
  var bg = teHexToRgb(def.background) ? def.background : (mode === 'dark' ? '#0e1420' : '#f6f8fc');
  var accent = teHexToRgb(def.accent) ? def.accent : '#2f6df6';
  var fgRaw = teHexToRgb(def.foreground) ? def.foreground : (mode === 'dark' ? '#eef4fb' : '#172033');
  var contrast = typeof def.contrast === 'number' ? def.contrast : 55;
  contrast = contrast < 0 ? 0 : (contrast > 100 ? 100 : contrast);
  // 对比度旋钮放大分隔强度：0→0.6 倍，100→1.6 倍。
  var k = 0.6 + contrast / 100;
  // 可读性守护：正文与背景至少 4.5:1。
  var fg = teEnsureReadable(fgRaw, bg, 4.5);
  var white = '#ffffff';
  var t = {};

  if (mode === 'dark') {
    t['--app-bg'] = bg;
    t['--sidebar-bg'] = teMix(bg, fg, 0.02 * k);
    t['--surface-primary'] = teMix(bg, fg, 0.055 * k);
    t['--surface-secondary'] = teMix(bg, fg, 0.03 * k);
    t['--surface-elevated'] = teMix(bg, fg, 0.095 * k);
    t['--surface-hover'] = teMix(bg, fg, 0.075 * k);
    t['--surface-selected'] = teMix(bg, accent, 0.28);
    t['--border-subtle'] = teMix(bg, fg, 0.09 * k);
    t['--border-default'] = teMix(bg, fg, 0.15 * k);
    t['--border-strong'] = teMix(bg, fg, 0.24 * k);
    t['--border-control'] = teMix(bg, fg, 0.40);
    t['--text-primary'] = fg;
    t['--text-secondary'] = teMix(fg, bg, 0.28);
    t['--text-tertiary'] = teMix(fg, bg, 0.42);
    t['--text-decorative'] = teMix(fg, bg, 0.58);
    t['--text-disabled'] = teMix(fg, bg, 0.64);
    t['--input-bg'] = teMix(bg, fg, 0.03 * k);
    t['--menu-bg'] = t['--surface-elevated'];
    t['--dialog-bg'] = t['--surface-primary'];
    t['--tooltip-bg'] = fg;
    t['--tooltip-text'] = bg;
    t['--accent-hover'] = teLighten(accent, 0.14);
    t['--accent-soft'] = teAlpha(accent, 0.16);
    t['--skeleton-base'] = teMix(bg, fg, 0.075 * k);
    t['--skeleton-highlight'] = teMix(bg, fg, 0.13 * k);
    t['--dialog-backdrop'] = 'rgba(0,0,0,0.58)';
    t['--shadow-card'] = '0 2px 10px rgba(0,0,0,0.18)';
    t['--shadow-card-hover'] = '0 6px 18px rgba(0,0,0,0.28)';
    t['--shadow-popover'] = '0 16px 38px rgba(0,0,0,0.42)';
    t['--calibration-card-bg'] = teMix(accent, bg, 0.68);
    t['--calibration-card-hover'] = teMix(accent, bg, 0.60);
    t['--calibration-card-border'] = teMix(accent, bg, 0.30);
    t['--calibration-card-text'] = teEnsureReadable(teLighten(accent, 0.40), teMix(accent, bg, 0.68), 4.5);
    t['--calibration-card-shadow'] = '0 5px 18px rgba(0,0,0,0.28)';
    t['--match-block-bg'] = 'rgba(93,63,10,0.42)';
    t['--match-block-border'] = 'rgba(251,191,36,0.62)';
    t['--match-block-accent'] = '#FBBF24';
    t['--match-block-flash-bg'] = 'rgba(122,82,12,0.62)';
    t['--match-inline-bg'] = 'rgba(251,191,36,0.24)';
    t['--match-inline-border'] = 'rgba(253,210,76,0.72)';
    t['--match-inline-text'] = '#FFF8DF';
    t['--match-focus-ring'] = 'rgba(251,191,36,0.22)';
  } else {
    t['--app-bg'] = bg;
    t['--sidebar-bg'] = teMix(bg, fg, 0.05 * k);
    t['--surface-primary'] = teMix(bg, white, 0.6);
    t['--surface-secondary'] = teMix(bg, white, 0.35);
    t['--surface-elevated'] = teMix(bg, white, 0.8);
    t['--surface-hover'] = teMix(bg, fg, 0.055 * k);
    t['--surface-selected'] = teMix(bg, accent, 0.14);
    t['--border-subtle'] = teMix(bg, fg, 0.06 * k);
    t['--border-default'] = teMix(bg, fg, 0.13 * k);
    t['--border-strong'] = teMix(bg, fg, 0.22 * k);
    t['--border-control'] = teMix(bg, fg, 0.48);
    t['--text-primary'] = fg;
    t['--text-secondary'] = teMix(fg, bg, 0.24);
    t['--text-tertiary'] = teMix(fg, bg, 0.42);
    t['--text-decorative'] = teMix(fg, bg, 0.56);
    t['--text-disabled'] = teMix(fg, bg, 0.66);
    t['--input-bg'] = t['--surface-elevated'];
    t['--menu-bg'] = t['--surface-elevated'];
    t['--dialog-bg'] = t['--surface-elevated'];
    t['--tooltip-bg'] = fg;
    t['--tooltip-text'] = teMix(bg, white, 0.85);
    t['--accent-hover'] = teDarken(accent, 0.10);
    t['--accent-soft'] = teMix(accent, bg, 0.86);
    t['--skeleton-base'] = teMix(bg, fg, 0.06 * k);
    t['--skeleton-highlight'] = t['--surface-secondary'];
    t['--dialog-backdrop'] = teAlpha(teDarken(fg, 0.1), 0.34);
    t['--shadow-card'] = '0 2px 8px ' + teAlpha(fg, 0.05);
    t['--shadow-card-hover'] = '0 5px 14px ' + teAlpha(fg, 0.09);
    t['--shadow-popover'] = '0 12px 32px ' + teAlpha(fg, 0.16);
    t['--calibration-card-bg'] = teMix(accent, bg, 0.74);
    t['--calibration-card-hover'] = teMix(accent, bg, 0.66);
    t['--calibration-card-border'] = teMix(accent, bg, 0.42);
    t['--calibration-card-text'] = teEnsureReadable(teDarken(accent, 0.12), teMix(accent, bg, 0.74), 4.5);
    t['--calibration-card-shadow'] = '0 4px 14px ' + teAlpha(accent, 0.14);
    t['--match-block-bg'] = '#FFF8E6';
    t['--match-block-border'] = '#F2C66D';
    t['--match-block-accent'] = '#D99000';
    t['--match-block-flash-bg'] = '#FFEFC4';
    t['--match-inline-bg'] = '#FFE7A8';
    t['--match-inline-border'] = '#E9B644';
    t['--match-inline-text'] = '#30240A';
    t['--match-focus-ring'] = 'rgba(217,144,0,0.22)';
  }

  // accent 通用派生（含守护）。
  t['--accent'] = accent;
  t['--accent-contrast'] = teAccentContrast(accent);
  t['--accent-text'] = teReadableAccentText(accent, t['--app-bg'], 4.5);
  t['--focus-ring'] = '0 0 0 3px ' + teAlpha(accent, 0.22);
  t['--scrollbar-track'] = 'transparent';
  t['--scrollbar-thumb'] = teAlpha(t['--text-tertiary'], 0.30);
  t['--scrollbar-thumb-hover'] = teAlpha(t['--text-tertiary'], 0.46);

  // 状态色：语义固定（不随用户 accent 变），但底色贴合用户背景。
  var status = teStatusTokens(mode, t['--app-bg']);
  for (var key in status) { if (status.hasOwnProperty(key)) t[key] = status[key]; }

  // 用户可选的高级 token 覆盖（阶段 3 的 tokens?）。
  if (def.tokens && typeof def.tokens === 'object') {
    for (var ov in def.tokens) {
      if (def.tokens.hasOwnProperty(ov) && /^--[a-z0-9-]+$/.test(ov)) t[ov] = def.tokens[ov];
    }
  }
  return t;
}

// info/success/warning/danger/neutral 的语义色。图标色固定可读，
// soft/border 在用户背景上混出，避免与页面撞色。
function teStatusTokens(mode, bg) {
  var base = mode === 'dark'
    ? { info: '#60A5FA', success: '#4ADE80', neutral: '#A8B4C4', warning: '#FBBF24', danger: '#FF6673' }
    : { info: '#2563EB', success: '#168A46', neutral: '#667085', warning: '#C96B12', danger: '#D62C3A' };
  var out = {};
  ['info', 'success', 'neutral', 'warning', 'danger'].forEach(function(name) {
    var c = base[name];
    out['--' + name] = c;
    out['--' + name + '-icon'] = c;
    if (mode === 'dark') {
      out['--' + name + '-soft'] = teAlpha(c, 0.15);
      out['--' + name + '-border'] = teAlpha(c, 0.40);
    } else {
      out['--' + name + '-soft'] = teMix(c, bg, 0.88);
      out['--' + name + '-border'] = teMix(c, bg, 0.55);
    }
  });
  out['--danger-contrast'] = '#FFFFFF';
  return out;
}

// 派生结果拼成一段 CSS 规则文本，供运行时注入。
function themeDefToCss(def, selector) {
  var tokens = deriveThemeTokens(def);
  var lines = [];
  for (var key in tokens) {
    if (tokens.hasOwnProperty(key)) lines.push('  ' + key + ': ' + tokens[key] + ';');
  }
  lines.push('  color-scheme: ' + (def.mode === 'dark' ? 'dark' : 'light') + ';');
  if (def.fontUi) lines.push('  --font-ui: ' + def.fontUi + ';');
  if (def.fontCode) lines.push('  --font-code: ' + def.fontCode + ';');
  return selector + ' {\n' + lines.join('\n') + '\n}';
}

/* ── 内置官方预设（纯配置） ──
   builtinCss:true 的 6 套走已有 data-theme CSS 块（首帧一致、测试钉死）；
   其余为新预设，纯配置，运行时经引擎派生渲染，不新增 CSS 块。 */
var THEME_PRESETS = [
  { id: 'frost-blue', name: 'MEFinder Light', label: '晴蓝', mode: 'light', builtinCss: true,
    accent: '#0F62E6', background: '#F5F8FC', foreground: '#172033', contrast: 55,
    desc: '清爽理性，适合日间使用' },
  { id: 'warm-paper', name: 'Warm Paper', label: '暖纸', mode: 'light', builtinCss: false,
    accent: '#9A6A3C', background: '#FBF6EC', foreground: '#2B2620', contrast: 52,
    desc: '柔和纸张质感，长时间阅读不刺眼' },
  { id: 'sepia', name: 'Sepia', label: '棕褐', mode: 'light', builtinCss: false,
    accent: '#8A5A2B', background: '#F1E4CE', foreground: '#3B2F1E', contrast: 58,
    desc: '经典棕褐纸张，仿旧书页' },
  { id: 'sage-ivory', name: '抹茶', label: '抹茶', mode: 'light', builtinCss: true,
    accent: '#4A5F39', background: '#F7F7F1', foreground: '#25291F', contrast: 55,
    desc: '低刺激、安静，适合长时间阅读' },
  { id: 'warm-sand', name: '暖沙', label: '暖沙', mode: 'light', builtinCss: true,
    accent: '#9F4A1E', background: '#FBF7F1', foreground: '#34251E', contrast: 55,
    desc: '温暖柔和，带轻微纸张气质' },
  { id: 'rose-mist', name: '樱粉', label: '樱粉', mode: 'light', builtinCss: true,
    accent: '#B0335A', background: '#FDF6F8', foreground: '#2C2528', contrast: 55,
    desc: '清柔克制，带淡粉强调' },
  { id: 'lavender-purple', name: '薰衣草', label: '薰衣草', mode: 'light', builtinCss: true,
    accent: '#6544B0', background: '#F9F7FD', foreground: '#282532', contrast: 55,
    desc: '优雅现代，使用柔和薰衣草紫' },
  { id: 'midnight', name: 'MEFinder Dark', label: '午夜', mode: 'dark', builtinCss: true,
    accent: '#2485FF', background: '#08111D', foreground: '#EEF4FB', contrast: 55,
    desc: '低亮度深色主题，适合夜间使用' },
  { id: 'oled-black', name: 'OLED Black', label: '纯黑', mode: 'dark', builtinCss: false,
    accent: '#2E90FF', background: '#000000', foreground: '#E9EEF5', contrast: 72,
    desc: '纯黑背景，OLED 省电、极致对比' },
  { id: 'midnight-blue', name: 'Midnight Blue', label: '深海蓝', mode: 'dark', builtinCss: false,
    accent: '#6EA8FF', background: '#121722', foreground: '#EEF3FA', contrast: 58,
    desc: '深海蓝调，冷静沉稳' }
];

var THEME_PRESET_MAP = (function() {
  var m = {};
  THEME_PRESETS.forEach(function(p) { m[p.id] = p; });
  return m;
})();

// 每种模式的默认预设 id（首帧/原生回退用的内置 CSS 主题）。
var THEME_MODE_DEFAULT = { light: 'frost-blue', dark: 'midnight' };
var THEME_BUILTIN_CSS_IDS = ['frost-blue', 'sage-ivory', 'warm-sand', 'rose-mist', 'lavender-purple', 'midnight'];

/* ── 导入校验：把外部 JSON 规整成一份可信 ThemeDef，非法即拒绝 ──
   返回 { ok, def, error }，永不抛出，导入非法文件不会崩溃。 */
function normalizeThemeDef(raw) {
  if (!raw || typeof raw !== 'object') return { ok: false, error: '主题文件不是有效的对象' };
  var mode = raw.mode === 'dark' ? 'dark' : (raw.mode === 'light' ? 'light' : null);
  if (!mode) return { ok: false, error: '主题缺少有效的 mode（light/dark）' };
  if (!teIsHex(raw.accent)) return { ok: false, error: '强调色不是有效的颜色值' };
  if (!teIsHex(raw.background)) return { ok: false, error: '背景色不是有效的颜色值' };
  if (!teIsHex(raw.foreground)) return { ok: false, error: '前景色不是有效的颜色值' };
  var contrast = Number(raw.contrast);
  if (!isFinite(contrast)) contrast = 55;
  contrast = contrast < 0 ? 0 : (contrast > 100 ? 100 : Math.round(contrast));
  var name = (typeof raw.name === 'string' && raw.name.trim()) ? raw.name.trim().slice(0, 60) : '导入的主题';
  var def = {
    schemaVersion: THEME_ENGINE_SCHEMA,
    mode: mode,
    name: name,
    accent: teRgbToHex(teHexToRgb(raw.accent)),
    background: teRgbToHex(teHexToRgb(raw.background)),
    foreground: teRgbToHex(teHexToRgb(raw.foreground)),
    contrast: contrast
  };
  if (typeof raw.fontUi === 'string' && raw.fontUi.length < 200) def.fontUi = raw.fontUi;
  if (typeof raw.fontCode === 'string' && raw.fontCode.length < 200) def.fontCode = raw.fontCode;
  return { ok: true, def: def };
}

// 导出用的最小、稳定、带版本号的 JSON。
function themeDefToExport(def) {
  var out = {
    schemaVersion: THEME_ENGINE_SCHEMA,
    name: def.name || '主题',
    mode: def.mode === 'dark' ? 'dark' : 'light',
    accent: def.accent,
    background: def.background,
    foreground: def.foreground,
    contrast: typeof def.contrast === 'number' ? def.contrast : 55
  };
  if (def.fontUi) out.fontUi = def.fontUi;
  if (def.fontCode) out.fontCode = def.fontCode;
  return out;
}

/* ── 运行时应用层（碰 DOM，不参与 node 纯函数单测） ── */

// 按 id 找到主题定义（自定义优先，其次官方预设）。
function teLookupThemeDef(id) {
  if (typeof appearanceState !== 'undefined' && appearanceState.customThemes && appearanceState.customThemes[id]) {
    return appearanceState.customThemes[id];
  }
  return THEME_PRESET_MAP[id] || null;
}

function teSystemPrefersDark() {
  try {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  } catch (_) { return false; }
}

// 外观模式 + 系统偏好 → 实际生效的主题 id。
function resolveActiveThemeId(state, prefersDark) {
  if (state.mode === 'light') return state.light;
  if (state.mode === 'dark') return state.dark;
  return prefersDark ? state.dark : state.light; // system
}

// 把某个主题 id 真正落到页面：内置 CSS 主题切 data-theme；新预设/自定义
// 主题运行时派生并注入样式元素 id=me-theme-runtime，data-theme=custom。
function applyThemeById(id) {
  var styleEl = document.getElementById('me-theme-runtime');
  var preset = THEME_PRESET_MAP[id];
  if (preset && preset.builtinCss) {
    if (styleEl) styleEl.textContent = '';
    document.documentElement.dataset.theme = id;
    return id;
  }
  var def = teLookupThemeDef(id);
  if (!def) {
    var fb = THEME_MODE_DEFAULT.light;
    if (styleEl) styleEl.textContent = '';
    document.documentElement.dataset.theme = fb;
    return fb;
  }
  if (!styleEl) {
    styleEl = document.createElement('style');
    styleEl.id = 'me-theme-runtime';
    (document.head || document.documentElement).appendChild(styleEl);
  }
  styleEl.textContent = themeDefToCss(def, ':root[data-theme="custom"]');
  document.documentElement.dataset.theme = 'custom';
  return id;
}

// 解析并应用当前外观状态；返回实际生效的主题 id。
function applyAppearance() {
  var activeId = resolveActiveThemeId(appearanceState, teSystemPrefersDark());
  applyThemeById(activeId);
  currentTheme = activeId;
  return activeId;
}

// 活动主题归约成一个内置 CSS 主题 id（POST 给后端做首帧/原生回退）。
function activeBuiltinFallback() {
  var id = resolveActiveThemeId(appearanceState, teSystemPrefersDark());
  var preset = THEME_PRESET_MAP[id];
  if (preset && preset.builtinCss) return id;
  var def = teLookupThemeDef(id);
  var mode = def ? def.mode : (teSystemPrefersDark() ? 'dark' : 'light');
  return THEME_MODE_DEFAULT[mode] || 'frost-blue';
}

// 系统色变化时，若处于「跟随系统」，即时重解析。
function initAppearanceSystemWatch() {
  try {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var handler = function() { if (appearanceState.mode === 'system') applyAppearance(); };
    if (mq.addEventListener) mq.addEventListener('change', handler);
    else if (mq.addListener) mq.addListener(handler);
  } catch (_) {}
}

// node 单测导出（浏览器下无 module）。
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    teHexToRgb: teHexToRgb, teRgbToHex: teRgbToHex, teMix: teMix, teContrast: teContrast,
    teEnsureReadable: teEnsureReadable, teIsHex: teIsHex, teAccentContrast: teAccentContrast,
    deriveThemeTokens: deriveThemeTokens, themeDefToCss: themeDefToCss,
    normalizeThemeDef: normalizeThemeDef, themeDefToExport: themeDefToExport,
    THEME_PRESETS: THEME_PRESETS, THEME_PRESET_MAP: THEME_PRESET_MAP,
    THEME_MODE_DEFAULT: THEME_MODE_DEFAULT, THEME_BUILTIN_CSS_IDS: THEME_BUILTIN_CSS_IDS
  };
}
