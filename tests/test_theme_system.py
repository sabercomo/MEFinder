from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop import error_html, loading_html
from src.me_finder import __version__
from src.me_finder.preferences import (
    DEFAULT_AUTO_UPDATE,
    DEFAULT_CALIBRATION_VIEW,
    DEFAULT_CITATION_STYLE,
    DEFAULT_CITATION_STYLES,
    DEFAULT_DOCUMENT_EXPORT_MODE,
    DEFAULT_LIBRARY_LANGUAGE,
    DEFAULT_LIBRARY_VIEW,
    DEFAULT_ONLINE_AUTO_MATCH,
    DEFAULT_PDF_OPEN_MODE,
    DEFAULT_PDF_PARSE_MODE,
    DEFAULT_THEME,
    VALID_PDF_OPEN_MODES,
    VALID_DOCUMENT_EXPORT_MODES,
    VALID_THEMES,
    read_preferences,
    resolve_native_shell_theme,
    save_preferences,
)
from src.me_finder.web import HTML, render_html


class NativeShellThemeResolutionTests(unittest.TestCase):
    """原生首帧/标题栏须跟随「实际生效」的明暗，避免深色 HTML 周围露白边。"""

    @staticmethod
    def _prefs(mode: str, light: str = "frost-blue", dark: str = "gruvbox-dark", custom=None):
        return {
            "theme": "frost-blue",
            "appearance": {
                "schemaVersion": 2,
                "mode": mode,
                "light": light,
                "dark": dark,
                "custom_themes": custom or {},
            },
        }

    def test_system_mode_with_os_dark_resolves_to_dark_builtin(self) -> None:
        theme = resolve_native_shell_theme(
            self._prefs("system"), os_prefers_dark=True
        )
        self.assertEqual(theme, "midnight")

    def test_system_mode_with_os_light_resolves_to_light_builtin(self) -> None:
        theme = resolve_native_shell_theme(
            self._prefs("system"), os_prefers_dark=False
        )
        self.assertEqual(theme, "frost-blue")

    def test_system_mode_unknown_os_falls_back_to_light(self) -> None:
        theme = resolve_native_shell_theme(
            self._prefs("system"), os_prefers_dark=None
        )
        self.assertEqual(theme, "frost-blue")

    def test_explicit_dark_preset_reduces_to_dark_builtin(self) -> None:
        theme = resolve_native_shell_theme(self._prefs("dark"))
        self.assertEqual(theme, "midnight")

    def test_custom_dark_theme_reduces_to_dark_builtin(self) -> None:
        prefs = self._prefs(
            "system",
            dark="custom-x",
            custom={"custom-x": {"mode": "dark"}},
        )
        self.assertEqual(
            resolve_native_shell_theme(prefs, os_prefers_dark=True), "midnight"
        )

    def test_missing_appearance_uses_legacy_theme(self) -> None:
        self.assertEqual(
            resolve_native_shell_theme({"theme": "midnight"}), "midnight"
        )
        self.assertEqual(
            resolve_native_shell_theme({"theme": "not-a-theme"}), DEFAULT_THEME
        )


class PreferencePersistenceTests(unittest.TestCase):
    @staticmethod
    def default_appearance(
        mode: str = "light", light: str = "frost-blue", dark: str = "midnight"
    ) -> dict[str, object]:
        return {
            "schemaVersion": 2,
            "mode": mode,
            "light": light,
            "dark": dark,
            "custom_themes": {},
        }

    @staticmethod
    def default_preferences(theme: str = DEFAULT_THEME) -> dict[str, object]:
        return {
            "theme": theme,
            # 一旦文件里存在 appearance，保存单独 theme 不会改动它；这些用例始终从
            # 空文件（frost-blue）起步，因此 appearance 恒为浅色迁移结果。
            "appearance": PreferencePersistenceTests.default_appearance(),
            "library_view": DEFAULT_LIBRARY_VIEW,
            "calibration_view": DEFAULT_CALIBRATION_VIEW,
            "scan_directories": [],
            "pdf_open_mode": DEFAULT_PDF_OPEN_MODE,
            "pdf_parse_mode": DEFAULT_PDF_PARSE_MODE,
            "document_export_mode": DEFAULT_DOCUMENT_EXPORT_MODE,
            "auto_update": DEFAULT_AUTO_UPDATE,
            "citation_styles": list(DEFAULT_CITATION_STYLES),
            "citation_style": DEFAULT_CITATION_STYLE,
            "lib_default_language": DEFAULT_LIBRARY_LANGUAGE,
            "online_auto_match_threshold": DEFAULT_ONLINE_AUTO_MATCH,
        }

    def test_scan_directories_round_trip_and_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            saved = save_preferences(
                {"scan_directories": ["D:\\文献\\哲学", "  ", "D:\\文献\\哲学", "E:/papers"]},
                path,
            )
            expected = [str(Path("D:\\文献\\哲学")), str(Path("E:/papers"))]
            self.assertEqual(saved["scan_directories"], expected)
            self.assertEqual(read_preferences(path)["scan_directories"], expected)
            with self.assertRaises(ValueError):
                save_preferences({"scan_directories": "D:\\单个路径"}, path)

    def test_missing_or_invalid_preference_uses_frost_blue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            self.assertEqual(read_preferences(path), self.default_preferences())
            path.write_text('{"theme":"unknown"}', encoding="utf-8")
            self.assertEqual(read_preferences(path), self.default_preferences())

    def test_theme_is_saved_atomically_and_can_be_read_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            for theme in VALID_THEMES:
                saved = save_preferences({"theme": theme}, path)
                self.assertEqual(saved, self.default_preferences(theme))
                self.assertEqual(read_preferences(path), self.default_preferences(theme))
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["theme"], theme)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_display_modes_are_saved_with_theme_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            saved = save_preferences({"library_view": "grid", "calibration_view": "list"}, path)
            self.assertEqual(saved["library_view"], "grid")
            self.assertEqual(saved["calibration_view"], "list")
            self.assertEqual(read_preferences(path), saved)

    def test_custom_theme_highlight_round_trips_and_old_entries_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            appearance = self.default_appearance(light="custom-light")
            appearance["custom_themes"] = {
                "custom-light": {
                    "schemaVersion": 1,
                    "name": "旧自定义",
                    "mode": "light",
                    "accent": "#9F4A1E",
                    "background": "#FBF7F1",
                    "foreground": "#34251E",
                    "contrast": 55,
                }
            }
            saved = save_preferences({"appearance": appearance}, path)
            migrated = saved["appearance"]["custom_themes"]["custom-light"]
            self.assertEqual(migrated["schemaVersion"], 2)
            self.assertEqual(migrated["highlight"], "#2563B8")

            appearance = saved["appearance"]
            appearance["custom_themes"]["custom-light"]["highlight"] = "#56949F"
            saved = save_preferences({"appearance": appearance}, path)
            self.assertEqual(
                saved["appearance"]["custom_themes"]["custom-light"]["highlight"],
                "#56949F",
            )

    def test_legacy_calibration_view_migrates_when_library_view_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            path.write_text('{"theme":"frost-blue","calibration_view":"grid"}', encoding="utf-8")
            migrated = read_preferences(path)
            self.assertEqual(migrated["library_view"], "grid")
            saved = save_preferences({"theme": "rose-mist"}, path)
            self.assertEqual(saved["library_view"], "grid")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["library_view"], "grid")

    def test_unsupported_theme_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                save_preferences({"theme": "pitch-black"}, Path(temp_dir) / "preferences.json")

    def test_pdf_open_mode_defaults_to_native_and_round_trips(self) -> None:
        self.assertEqual(VALID_PDF_OPEN_MODES, frozenset({"native", "system"}))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            self.assertEqual(read_preferences(path)["pdf_open_mode"], "native")
            self.assertEqual(
                save_preferences({"pdf_open_mode": "system"}, path)["pdf_open_mode"],
                "system",
            )
            self.assertEqual(read_preferences(path)["pdf_open_mode"], "system")
            with self.assertRaises(ValueError):
                save_preferences({"pdf_open_mode": "browser"}, path)

    def test_auto_update_defaults_off_and_requires_a_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            self.assertFalse(read_preferences(path)["auto_update"])
            saved = save_preferences({"auto_update": True}, path)
            self.assertTrue(saved["auto_update"])
            self.assertTrue(read_preferences(path)["auto_update"])
            with self.assertRaises(ValueError):
                save_preferences({"auto_update": "true"}, path)

    def test_document_export_mode_defaults_to_data_only_and_round_trips(self) -> None:
        self.assertEqual(
            VALID_DOCUMENT_EXPORT_MODES,
            frozenset({"data_only", "with_pdf"}),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            self.assertEqual(
                read_preferences(path)["document_export_mode"],
                "data_only",
            )
            saved = save_preferences({"document_export_mode": "with_pdf"}, path)
            self.assertEqual(saved["document_export_mode"], "with_pdf")
            with self.assertRaises(ValueError):
                save_preferences({"document_export_mode": "pdf_only"}, path)

    def test_citation_styles_default_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            self.assertEqual(read_preferences(path)["citation_styles"], ["chinese", "gb"])
            saved = save_preferences({"citation_styles": ["mla", "chinese", "apa", "mla"]}, path)
            self.assertEqual(saved["citation_styles"], ["chinese", "apa", "mla"])
            saved = save_preferences({"citation_style": "apa"}, path)
            self.assertEqual(saved["citation_style"], "apa")
            self.assertEqual(read_preferences(path)["citation_styles"], ["chinese", "apa", "mla"])
            self.assertEqual(read_preferences(path)["citation_style"], "apa")
            with self.assertRaises(ValueError):
                save_preferences({"citation_styles": []}, path)
            with self.assertRaises(ValueError):
                save_preferences({"citation_styles": ["unknown"]}, path)
            with self.assertRaises(ValueError):
                save_preferences({"citation_style": "gb"}, path)


class ThemeMarkupTests(unittest.TestCase):
    REQUIRED_TOKENS = (
        "--app-bg", "--sidebar-bg", "--surface-primary", "--surface-secondary",
        "--surface-elevated", "--surface-hover", "--surface-selected",
        "--text-primary", "--text-secondary", "--text-tertiary", "--text-disabled",
        "--border-subtle", "--border-default", "--border-strong", "--accent",
        "--accent-hover", "--accent-soft", "--accent-contrast", "--highlight", "--success",
        "--success-soft", "--success-border", "--success-icon", "--warning",
        "--warning-soft", "--warning-border", "--warning-icon", "--danger",
        "--danger-soft", "--danger-border", "--danger-icon", "--info", "--info-soft",
        "--info-border", "--info-icon", "--neutral", "--neutral-soft",
        "--neutral-border", "--neutral-icon", "--input-bg", "--menu-bg", "--dialog-bg",
        "--tooltip-bg", "--shadow-card", "--shadow-popover", "--focus-ring",
        "--scrollbar-track", "--scrollbar-thumb", "--skeleton-base", "--skeleton-highlight",
    )
    MATCH_TOKENS = (
        "--match-block-bg", "--match-block-border", "--match-block-accent",
        "--match-block-flash-bg", "--match-inline-bg", "--match-inline-border",
        "--match-inline-text", "--match-focus-ring",
    )
    THEMES = (
        "frost-blue", "sage-ivory", "warm-sand", "rose-mist",
        "lavender-purple", "midnight",
    )

    def test_six_themes_share_one_dom_and_complete_token_contract(self) -> None:
        self.assertEqual(HTML.count('id="page-settings"'), 1)
        self.assertEqual(HTML.count('id="page-library"'), 1)
        self.assertEqual(HTML.count('id="page-calibration"'), 0)
        self.assertEqual(VALID_THEMES, frozenset(self.THEMES))
        for theme in self.THEMES:
            self.assertIn(f'html[data-theme="{theme}"]', HTML)
        for token in self.REQUIRED_TOKENS + self.MATCH_TOKENS:
            self.assertIn(token + ":", HTML)
        self.assertEqual(HTML.count("--app-bg:"), 6)
        self.assertEqual(HTML.count("--match-focus-ring:"), 6)

    def test_settings_uses_preview_cards_and_switches_without_reload(self) -> None:
        # 主题预设现由引擎的 THEME_PRESETS 驱动（每项 id/label/mode/desc）。
        expected_order = ["晴蓝", "雾灰", "棕褐", "抹茶", "暖沙", "樱粉", "薰衣草", "午夜"]
        positions = [HTML.index(f"label: '{name}'") for name in expected_order]
        self.assertEqual(positions, sorted(positions))
        for theme in self.THEMES:
            self.assertIn(f"id: '{theme}'", HTML)
        # 六套内置 CSS 主题之外，新官方预设仅是配置，不新增 CSS 主题块。
        for preset in ("warm-paper", "sepia", "oled-black", "midnight-blue"):
            self.assertIn(f"id: '{preset}'", HTML)
        self.assertEqual(HTML.count('function themePreviewMarkup(themeId, styleAttr)'), 1)
        # 预览缩略图现为「Aa 色板样张」：背景=纸、Aa=墨，并分开展示按钮色与正文强调色。
        self.assertIn('class="theme-swatch-aa"', HTML)
        self.assertIn('class="theme-swatch-accent"', HTML)
        self.assertIn('class="theme-swatch-highlight"', HTML)
        self.assertIn('class="theme-swatch-card"', HTML)
        for description in (
            "清爽理性，适合日间使用", "低刺激、安静，适合长时间阅读",
            "温暖柔和，带轻微纸张气质", "清柔克制，带淡粉强调",
            "优雅现代，使用柔和薰衣草紫", "沉静蓝黑，克制专业（GitHub Dark）",
        ):
            self.assertIn(description, HTML)
        self.assertIn('.theme-option:focus-visible', HTML)
        self.assertIn('role="radiogroup"', HTML)
        self.assertIn('role="radio"', HTML)
        self.assertIn('<span>按钮色</span><input type="color" id="appearance-accent"', HTML)
        self.assertIn('<span>强调色</span><input type="color" id="appearance-highlight"', HTML)
        self.assertIn('id="appearance-delete-custom"', HTML)
        self.assertIn("async function deleteCurrentCustomTheme()", HTML)
        self.assertIn("appearanceState[slot] = THEME_MODE_DEFAULT[slot];", HTML)
        # 网格由当前生效的那一套（浅/深，由外观模式派生）筛选出的预设 + 自定义主题渲染。
        self.assertIn("container.innerHTML = themeChoicesForMode(currentSlot()).map(themeOptionMarkup).join('')", HTML)
        # 引擎把选中主题真正落到 data-theme（内置切 id、自定义切 custom）。
        self.assertIn("document.documentElement.dataset.theme = id", HTML)
        self.assertIn("fetch('/api/preferences'", HTML)
        # 外观模式：跟随系统 / 浅 / 深。
        self.assertIn("data-appearance-mode=\"system\"", HTML)
        self.assertIn("(prefers-color-scheme: dark)", HTML)
        self.assertIn("function setAppearanceMode(mode)", HTML)
        self.assertNotIn('id="theme-select"', HTML)
        self.assertNotIn("location.reload", HTML)
        self.assertNotIn('theme-preview-line', HTML)
        self.assertNotIn('--preview-bg', HTML)
        self.assertNotIn('--preview-accent', HTML)
        # 画廊改用 auto-fill 自适应列数（天然 2–3 列），不再依赖容器查询断点，
        # 也就绝不会塌成一张巨卡。
        self.assertIn('grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));', HTML)
        self.assertRegex(
            HTML,
            r"\.settings-section\s*\{[^}]*max-width:\s*none;[^}]*margin:\s*0;",
        )
        self.assertRegex(
            HTML,
            r"#appearance-card\.active\s*\{[^}]*max-width:\s*none",
        )
        self.assertRegex(HTML, r"\.theme-options\s*\{[^}]*gap:\s*16px")
        self.assertRegex(HTML, r"\.theme-option\s*\{[^}]*padding:\s*12px")
        # 样张按固定比例（而非写死高度）自适应卡片宽度。
        self.assertRegex(HTML, r"\.theme-preview\s*\{[^}]*aspect-ratio:\s*8 / 5")

    def test_large_desktop_settings_trade_density_for_legibility(self) -> None:
        self.assertIn(
            "@media (min-width: 1500px) and (min-height: 800px)",
            HTML,
        )
        self.assertRegex(
            HTML,
            re.compile(
                r"@media \(min-width: 1500px\) and \(min-height: 800px\)\s*\{.*?"
                r"\.settings-section,\s*#appearance-card\.active\s*\{[^}]*"
                r"max-width:\s*none;[^}]*\}.*?"
                r"\.settings-section-title\s*\{\s*font-size:\s*20px;\s*\}",
                re.S,
            ),
        )
        self.assertIn(".pdf-open-option-copy strong { font-size: 16px; }", HTML)
        self.assertIn("#bib-completion-settings .auto-match-hint { font-size: 15px; }", HTML)

    def test_macos_settings_offer_native_pdfkit_and_preview_modes(self) -> None:
        self.assertIn(
            'class="settings-section desktop-pdf-settings active" id="pdf-reader-settings"',
            HTML,
        )
        self.assertIn('data-target="pdf-reader-settings"', HTML)
        self.assertIn(
            "showSettingsCategory('pdf-reader-settings')",
            HTML,
        )
        self.assertIn('id="pdf-reader-body"', HTML)
        self.assertIn('data-pdf-open-choice="native"', HTML)
        self.assertIn('data-pdf-open-choice="system"', HTML)
        self.assertIn("使用 macOS PDFKit", HTML)
        self.assertIn("macOS 预览", HTML)
        self.assertIn("function setPdfOpenMode(mode)", HTML)
        self.assertIn("var preferencesLoadPromise = null;", HTML)
        self.assertIn("if (preferencesLoadPromise) return preferencesLoadPromise;", HTML)
        self.assertIn(
            "if (pdfOpenModeSaving || documentExportModeSaving) return null;",
            HTML,
        )
        self.assertIn("setPdfOpenModeControlsDisabled(true);", HTML)
        self.assertIn("if (pdfOpenModeSaving || preferencesLoadPromise)", HTML)
        self.assertIn("function applyPreferencesData(data, requestedThemeRevision)", HTML)
        self.assertIn("applyPreferencesData(data, requestedThemeRevision);", HTML)
        self.assertIn("failedStatus.textContent = '读取失败';", HTML)
        self.assertIn(".pdf-open-options.is-busy", HTML)
        self.assertIn("pdf_open_mode: mode", HTML)
        self.assertIn('html[data-desktop-shell="macos"] .settings-nav-item.plat-desktop', HTML)
        self.assertIn('html[data-desktop-shell="win32"] .settings-nav-item.plat-desktop', HTML)

    def test_windows_settings_offer_webview2_system_reader_and_updates(self) -> None:
        for marker in (
            'id="pdf-reader-settings"',
            'id="pdf-native-description"',
            'id="pdf-system-title"',
            'id="software-update-settings"',
            'id="auto-update-enabled"',
            "Edge WebView2",
            "系统默认 PDF 阅读器",
            "async function checkForUpdates(automatic)",
            "auto_update:autoUpdateEnabled",
            "confirm_token:installToken",
            "autoInput.disabled = !state.can_self_update",
        ):
            self.assertIn(marker, HTML)
        self.assertIn(
            'html[data-desktop-shell="win32"] .settings-nav-item.plat-win',
            HTML,
        )

    def test_macos_settings_offer_manual_updates_and_data_location_migration(self) -> None:
        self.assertIn('id="macos-update-settings"', HTML)
        self.assertIn('id="macos-update-body"', HTML)
        self.assertIn("function checkMacosUpdate()", HTML)
        self.assertIn("fetch('/api/macos-update'", HTML)
        self.assertIn("打开 Releases 下载 DMG", HTML)
        self.assertIn("不会后台下载或自动替换应用", HTML)
        self.assertIn(f"当前版本 v{__version__}", HTML)
        self.assertNotIn("__APP_VERSION__", HTML)

        self.assertIn('id="data-location-settings"', HTML)
        self.assertIn('id="data-location-body"', HTML)
        self.assertIn('class="settings-nav-item cap-data-location"', HTML)
        self.assertIn("dataset.dataLocationAvailable = 'true'", HTML)
        self.assertIn(
            'html[data-data-location-available="true"] .settings-nav-item.cap-data-location',
            HTML,
        )
        self.assertIn(
            "showSettingsCategory('data-location-settings')",
            HTML,
        )
        self.assertIn("外接硬盘、移动固态硬盘、NAS、iCloud Drive 或 OneDrive", HTML)
        self.assertIn("function chooseDataLocation()", HTML)
        self.assertIn("function migrateDataLocation()", HTML)
        self.assertIn("fetch('/api/data-location/choose'", HTML)
        self.assertIn("fetch('/api/data-location/migrate'", HTML)
        self.assertIn("旧位置的数据会保留", HTML)
        self.assertIn("不要让两台电脑同时打开同一份云盘或 NAS 数据库", HTML)
        self.assertIn(
            'html[data-desktop-shell="macos"] .settings-nav-item.plat-macos',
            HTML,
        )

    def test_theme_previews_reuse_the_real_design_tokens(self) -> None:
        for theme in self.THEMES:
            self.assertIn(f'.theme-preview[data-preview-theme="{theme}"]', HTML)
        # 色板样张（.theme-preview / .theme-swatch-*）只用语义 token 上色，绝不写死颜色，
        # 因此任意主题（含运行时派生的自定义主题）都能自动正确显示。
        preview_rules = re.findall(r"\.theme-(?:preview|swatch)[^{]*\{[^}]+\}", HTML, re.S)
        preview_css = "\n".join(preview_rules)
        for token in (
            "--app-bg", "--surface-primary", "--text-primary",
            "--text-secondary", "--accent",
        ):
            self.assertIn(f"var({token})", preview_css)

    def test_rose_and_lavender_cards_use_tinted_near_white_surfaces(self) -> None:
        # Pink/purple card surfaces are a faint hue-tinted near-white, matching the
        # warm themes, so cards lift gently off the tinted page instead of reading as
        # a stark white rectangle. They must not fall back to pure #FFFFFF.
        expected_surfaces = {
            "rose-mist": "#FFFBFC",
            "lavender-purple": "#FDFBFE",
        }
        for theme, expected in expected_surfaces.items():
            block = re.search(
                rf'[^{{}}]*html\[data-theme="{re.escape(theme)}"\][^{{}}]*\{{([^}}]+)\}}',
                HTML,
            )
            self.assertIsNotNone(block, theme)
            surface = re.search(
                r"--surface-primary:\s*([^;]+);", block.group(1)
            ).group(1).strip()
            self.assertEqual(surface, expected)
            self.assertNotEqual(surface.upper(), "#FFFFFF", theme)
        self.assertIn(".library-card {", HTML)
        self.assertIn("background: var(--surface-primary);", HTML)

    def test_settings_sidebar_uses_a_gear_icon(self) -> None:
        self.assertIn('class="sidebar-settings-gear"', HTML)
        self.assertIn('<circle cx="12" cy="12" r="3"/>', HTML)
        self.assertNotIn(
            'M10 1v3M10 16v3M1 10h3M16 10h3M3.5 3.5l2 2',
            HTML,
        )

    def test_initial_html_is_server_rendered_for_selected_theme(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            for theme in self.THEMES:
                rendered = render_html(theme)
                self.assertIn(f'<html lang="zh-CN" data-theme="{theme}">', rendered)

    def test_macos_desktop_shell_adds_a_theme_driven_titlebar(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ME_FINDER_DESKTOP_SHELL": "macos"},
            clear=True,
        ):
            rendered = render_html("midnight")

        self.assertIn(
            '<html lang="zh-CN" data-theme="midnight" data-desktop-shell="macos">',
            rendered,
        )
        self.assertIn(
            'class="macos-titlebar pywebview-drag-region"',
            rendered,
        )
        self.assertIn('class="macos-titlebar-title">文献原句定位器</span>', rendered)
        self.assertIn('html[data-desktop-shell="macos"] .macos-titlebar', rendered)
        self.assertIn("var(--sidebar-bg) var(--sidebar-width)", rendered)
        self.assertIn("var(--app-bg) 100%", rendered)
        self.assertIn("height: calc(100vh - var(--macos-titlebar-height));", rendered)

    def test_windows_desktop_shell_is_marked_before_first_paint(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ME_FINDER_DESKTOP_SHELL": "win32"},
            clear=True,
        ):
            rendered = render_html("midnight")

        self.assertIn(
            '<html lang="zh-CN" data-theme="midnight" data-desktop-shell="win32">',
            rendered,
        )

    def test_windows_shell_uses_a_theme_driven_html_titlebar(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ME_FINDER_DESKTOP_SHELL": "win32"},
            clear=True,
        ):
            rendered = render_html("sage-ivory")

        for marker in (
            'class="windows-titlebar"',
            'class="windows-titlebar-drag pywebview-drag-region"',
            'onclick="minimizeWindowsWindow()"',
            'onclick="toggleWindowsMaximize()"',
            'onclick="closeWindowsWindow()"',
            "window.pywebview.api[method]()",
            "callWindowsWindow('is_maximized').then(setWindowsMaximized)",
        ):
            self.assertIn(marker, rendered)
        self.assertIn(
            'html[data-desktop-shell="win32"] .windows-titlebar',
            rendered,
        )
        self.assertIn("var(--sidebar-bg) var(--sidebar-width)", rendered)
        self.assertIn("var(--app-bg) 100%", rendered)
        self.assertIn(
            "height: calc(100vh - var(--windows-titlebar-height));",
            rendered,
        )
        self.assertIn("margin-top: var(--windows-titlebar-height);", rendered)

    def test_all_top_level_settings_sections_are_two_pane_panels(self) -> None:
        sections = {
            "pdf-reader-settings": "pdf-reader-body",
            "mineru-api-settings": "mineru-api-body",
            "local-ocr-settings": "local-ocr-body",
            "statistics-settings": "statistics-settings-body",
            "vision-api-settings": "vision-api-body",
            "citation-format-settings": "citation-format-body",
            "bib-completion-settings": "bib-completion-body",
            "appearance-card": "appearance-body",
            "data-location-settings": "data-location-body",
            "document-transfer-settings": "document-transfer-body",
            "backup-settings": "backup-settings-body",
            "software-update-settings": "software-update-body",
            "macos-update-settings": "macos-update-body",
        }
        # One left-rail entry per category switches which panel is active.
        self.assertEqual(HTML.count("onclick=\"showSettingsCategory('"), len(sections))
        for section_id, body_id in sections.items():
            self.assertRegex(
                HTML,
                rf'<section class="[^"]*\bsettings-section\b[^"]*" id="{section_id}" role="tabpanel">',
            )
            self.assertIn(f'data-target="{section_id}"', HTML)
            self.assertIn(f"showSettingsCategory('{section_id}')", HTML)
            self.assertRegex(
                HTML,
                rf'class="settings-collapse-body" id="{body_id}">',
            )

        settings_source = Path("src/me_finder/templates/index.html").read_text(
            encoding="utf-8"
        )
        panels = [
            (
                settings_source.index(f'id="{section_id}" role="tabpanel"'),
                section_id,
            )
            for section_id in sections
        ]
        panels.sort()
        settings_end = settings_source.index(
            '</div>\n      </div>\n    </div>', panels[-1][0]
        )
        for index, (panel_start, section_id) in enumerate(panels):
            panel_end = (
                panels[index + 1][0]
                if index + 1 < len(panels)
                else settings_end
            )
            panel = settings_source[panel_start:panel_end]
            self.assertEqual(
                panel.count("<div"),
                panel.count("</div>"),
                f"{section_id} 的 div 层级不平衡",
            )

        show_start = HTML.index("function showSettingsCategory(sectionId)")
        show_end = HTML.index("function ensureVisibleSettingsCategory()", show_start)
        show_block = HTML[show_start:show_end]
        self.assertIn("s.classList.toggle('active', s === section)", show_block)
        self.assertIn("btn.setAttribute('aria-selected', on ? 'true' : 'false')", show_block)
        self.assertIn("function ensureVisibleSettingsCategory()", HTML)
        self.assertIn(".settings-section.active { display: block; }", HTML)
        self.assertIn("#statistics-settings.active { max-width: none; }", HTML)
        self.assertIn(".parser-overview-metrics dd { margin: 0; color: var(--text-primary); font-size: 32px;", HTML)
        self.assertIn(".parser-provider-identity strong { overflow: hidden; color: var(--text-primary); font-size: 14px;", HTML)

    def test_update_heading_uses_one_baseline_for_title_and_note(self) -> None:
        heading_rule = re.search(
            r"\.settings-section-heading-copy\s*\{([^}]+)\}",
            HTML,
            re.S,
        )
        self.assertIsNotNone(heading_rule)
        self.assertIn("align-items: baseline;", heading_rule.group(1))

        update_start = HTML.index('id="software-update-settings"')
        update_end = HTML.index('id="macos-update-settings"', update_start)
        update_markup = HTML[update_start:update_end]
        self.assertRegex(
            update_markup,
            r'<span class="settings-section-heading-copy">\s*'
            r'<span class="settings-section-title">软件更新</span>\s*'
            r'<span class="settings-section-note">适用于 Windows 安装版</span>\s*'
            r'</span>',
        )
        self.assertIn('id="update-status-badge"', update_markup)

    def test_theme_switch_is_immediate_and_persisted(self) -> None:
        # 引擎状态：外观模式 + 浅/深各自选择 + 自定义主题。
        self.assertIn("let appearanceState = {", HTML)
        self.assertIn("let appearanceEditMode = 'light';", HTML)

        # 选主题：先即时应用（若正是生效模式），再持久化。
        choice_start = HTML.index("function selectThemeChoice(id)")
        choice_end = HTML.index("function isEditModeLive()", choice_start)
        choice_block = HTML[choice_start:choice_end]
        apply_at = choice_block.index("if (isEditModeLive()) applyAppearance();")
        persist_at = choice_block.index("persistAppearance();")
        self.assertLess(apply_at, persist_at)

        # applyAppearance 解析当前模式与系统偏好后落到 data-theme，且更新 currentTheme。
        self.assertIn("function applyAppearance()", HTML)
        self.assertIn("currentTheme = activeId;", HTML)

        # 持久化去抖，且同时写 appearance 完整状态与 legacy theme 内置回退。
        persist_start = HTML.index("function persistAppearance()")
        persist_end = HTML.index("function loadAppearanceFromPreferences", persist_start)
        persist_block = HTML[persist_start:persist_end]
        self.assertIn("setTimeout(function()", persist_block)
        self.assertIn("appearance: serializeAppearance()", persist_block)
        self.assertIn("theme: activeBuiltinFallback()", persist_block)
        self.assertIn("if (!response.ok) throw new Error", persist_block)
        self.assertIn("showToast('主题设置保存失败：' + error.message, 'danger')", persist_block)
        # 切主题不重载页面。
        self.assertNotIn("location.reload", HTML)

    def test_document_group_dialog_uses_theme_tokens(self) -> None:
        self.assertIn("background: var(--dialog-backdrop);", HTML)
        self.assertIn(".group-manage-card", HTML)
        group_card = HTML.split(".group-manage-card {", 1)[1].split("}", 1)[0]
        self.assertIn("background: var(--dialog-bg)", group_card)
        self.assertIn("box-shadow: var(--shadow-popover)", group_card)

    def test_browser_shell_does_not_show_the_macos_titlebar(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            rendered = render_html("midnight")

        self.assertIn('<html lang="zh-CN" data-theme="midnight">', rendered)
        self.assertNotIn('data-desktop-shell="macos"', rendered.splitlines()[1])
        self.assertRegex(
            rendered,
            r"\.macos-titlebar,\s*\.windows-titlebar\s*\{\s*display:\s*none;\s*\}",
        )

    def test_svg_icons_do_not_have_fixed_color_values(self) -> None:
        icon_color = re.compile(r'<(?:svg|path|circle|rect|line|polyline|polygon)[^>]*(?:stroke|fill)="#[0-9a-f]{3,8}"', re.I)
        self.assertIsNone(icon_color.search(HTML))
        self.assertIn('stroke="currentColor"', HTML)

    def test_reference_business_content_was_not_hardcoded(self) -> None:
        for sample in ("食人资本主义", "PDF 总数 11", "已校准 10"):
            self.assertNotIn(sample, HTML)

    def test_match_highlight_uses_theme_specific_semantic_tokens(self) -> None:
        expected_match_accents = {
            "frost-blue": "#D99000",
            "sage-ivory": "#7656B8",
            "warm-sand": "#2563B8",
            "rose-mist": "#1B8A99",
            "lavender-purple": "#B86C08",
            "midnight": "#58A6FF",
        }
        for theme, match_accent in expected_match_accents.items():
            block = re.search(
                rf'[^{{}}]*html\[data-theme="{re.escape(theme)}"\][^{{}}]*\{{([^}}]+)\}}',
                HTML,
            )
            self.assertIsNotNone(block, theme)
            css = block.group(1)
            accent = re.search(r"--accent:\s*([^;]+);", css).group(1).strip()
            actual_match = re.search(r"--match-block-accent:\s*([^;]+);", css).group(1).strip()
            self.assertEqual(actual_match, match_accent)
            highlight = re.search(r"--highlight:\s*([^;]+);", css).group(1).strip()
            self.assertEqual(highlight, actual_match)
            self.assertNotEqual(accent.lower(), actual_match.lower())
        self.assertIn("border-left: 3px solid var(--match-block-accent);", HTML)
        self.assertIn("background: var(--match-block-bg);", HTML)
        self.assertIn("box-shadow: 0 0 0 4px var(--match-focus-ring);", HTML)
        self.assertIn("animation: match-locate-pulse 620ms ease-out 1;", HTML)
        self.assertIn("hit.classList.add('is-locating')", HTML)
        self.assertNotIn("--highlight-soft", HTML)

        mark_blocks = re.findall(r"\.(?:result-snippet|detail-hit) mark\s*\{[^}]+\}", HTML, re.S)
        self.assertGreaterEqual(len(mark_blocks), 2)
        for block in mark_blocks:
            self.assertIn("background: var(--match-inline-bg);", block)
            self.assertIn("color: var(--match-inline-text);", block)
            self.assertIn("border: 1px solid var(--match-inline-border);", block)
            self.assertIsNone(re.search(r"255,204,0|234,179,8", block))

    def test_calibration_card_has_a_distinct_palette_in_every_theme(self) -> None:
        expected_backgrounds = {
            "frost-blue": "#DCEBFF",
            "sage-ivory": "#E1EBD5",
            "warm-sand": "#F9DDC8",
            "rose-mist": "#F8DCE6",
            "lavender-purple": "#E8DFF7",
            "midnight": "#17233A",
        }
        for theme, expected_background in expected_backgrounds.items():
            block = re.search(
                rf'[^{{}}]*html\[data-theme="{re.escape(theme)}"\][^{{}}]*\{{([^}}]+)\}}',
                HTML,
            )
            self.assertIsNotNone(block, theme)
            css = block.group(1)
            background = re.search(r"--calibration-card-bg:\s*([^;]+);", css).group(1).strip()
            surface = re.search(r"--surface-secondary:\s*([^;]+);", css).group(1).strip()
            self.assertEqual(background, expected_background)
            self.assertNotEqual(background.lower(), surface.lower())
            for token in (
                "--calibration-card-hover", "--calibration-card-border",
                "--calibration-card-text", "--calibration-card-shadow",
            ):
                self.assertRegex(css, rf"{re.escape(token)}:\s*[^;]+;")
        self.assertIn("background: var(--calibration-card-bg);", HTML)
        self.assertIn("border: 1px solid var(--calibration-card-border);", HTML)
        self.assertIn("color: var(--calibration-card-text);", HTML)
        self.assertIn("background: var(--calibration-card-hover);", HTML)


class DesktopThemeShellTests(unittest.TestCase):
    def test_midnight_loading_and_error_pages_start_dark(self) -> None:
        loading = loading_html("midnight")
        error = error_html("测试", "详情", "midnight")
        self.assertIn("#0D1117", loading)
        self.assertIn("#E6EDF3", loading)
        self.assertIn("#0D1117", error)
        self.assertNotIn("background: #F5F8FC", loading)

    def test_frost_blue_is_the_default_shell_theme(self) -> None:
        self.assertIn("#F5F8FC", loading_html())

    def test_every_theme_has_a_matching_first_paint_palette(self) -> None:
        expected = {
            "frost-blue": "#F5F8FC",
            "sage-ivory": "#F7F7F1",
            "warm-sand": "#FBF7F1",
            "rose-mist": "#FDF6F8",
            "lavender-purple": "#F9F7FD",
            "midnight": "#0D1117",
        }
        for theme, background in expected.items():
            self.assertIn(background, loading_html(theme))

    def test_windows_loading_and_error_pages_include_working_html_titlebars(self) -> None:
        pages = (
            loading_html("midnight", "win32"),
            error_html("测试", "详情", "midnight", "win32"),
        )
        for page in pages:
            self.assertIn(
                '<html lang="zh-CN" data-desktop-shell="win32">',
                page,
            )
            self.assertIn('<body class="windows-shell">', page)
            self.assertEqual(page.count('class="windows-titlebar"'), 1)
            self.assertIn('class="windows-titlebar-drag pywebview-drag-region"', page)
            self.assertIn('onclick="minimizeWindowsWindow()"', page)
            self.assertIn('onclick="toggleWindowsMaximize()"', page)
            self.assertIn('onclick="closeWindowsWindow()"', page)
            self.assertIn(
                "body.windows-shell { box-sizing: border-box; padding-top: var(--windows-titlebar-height); }",
                page,
            )

    def test_non_windows_bootstrap_pages_keep_their_native_shell_behavior(self) -> None:
        for shell in (None, "darwin", "linux"):
            loading = loading_html("midnight", shell)
            error = error_html("测试", "详情", "midnight", shell)
            self.assertNotIn('class="windows-titlebar"', loading)
            self.assertNotIn('class="windows-titlebar"', error)
            self.assertNotIn('data-desktop-shell="win32"', loading)
            self.assertNotIn('data-desktop-shell="win32"', error)


if __name__ == "__main__":
    unittest.main()
