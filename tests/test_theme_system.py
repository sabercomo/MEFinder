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
    DEFAULT_LIBRARY_LANGUAGE,
    DEFAULT_LIBRARY_VIEW,
    DEFAULT_ONLINE_AUTO_MATCH,
    DEFAULT_PDF_OPEN_MODE,
    DEFAULT_THEME,
    VALID_PDF_OPEN_MODES,
    VALID_THEMES,
    read_preferences,
    save_preferences,
)
from src.me_finder.web import HTML, render_html


class PreferencePersistenceTests(unittest.TestCase):
    @staticmethod
    def default_preferences(theme: str = DEFAULT_THEME) -> dict[str, object]:
        return {
            "theme": theme,
            "library_view": DEFAULT_LIBRARY_VIEW,
            "calibration_view": DEFAULT_CALIBRATION_VIEW,
            "scan_directories": [],
            "pdf_open_mode": DEFAULT_PDF_OPEN_MODE,
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
        "--accent-hover", "--accent-soft", "--accent-contrast", "--success",
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
        expected_order = ["晴蓝", "抹茶", "暖沙", "樱粉", "薰衣草", "午夜"]
        positions = [HTML.index(f"name:'{name}'") for name in expected_order]
        self.assertEqual(positions, sorted(positions))
        for theme in self.THEMES:
            self.assertIn(f"id:'{theme}'", HTML)
        self.assertEqual(HTML.count('function themePreviewMarkup(themeId)'), 1)
        self.assertEqual(HTML.count('class="theme-mini-sidebar"'), 1)
        self.assertEqual(HTML.count('class="theme-mini-search"'), 1)
        self.assertEqual(HTML.count('class="theme-mini-doc-card"'), 3)
        self.assertIn('class="theme-mini-match"', HTML)
        self.assertIn('class="theme-option-tone"', HTML)
        for description in (
            "清爽理性，适合日间使用", "低刺激、安静，适合长时间阅读",
            "温暖柔和，带轻微纸张气质", "清柔克制，带淡粉强调",
            "优雅现代，使用柔和薰衣草紫", "低亮度深色主题，适合夜间使用",
        ):
            self.assertIn(description, HTML)
        self.assertIn('.theme-option:focus-visible', HTML)
        self.assertIn('role="radiogroup"', HTML)
        self.assertIn('role="radio"', HTML)
        self.assertIn("container.innerHTML = THEME_OPTIONS.map(themeOptionMarkup).join('')", HTML)
        self.assertIn("document.documentElement.dataset.theme = theme", HTML)
        self.assertIn("fetch('/api/preferences'", HTML)
        self.assertNotIn('id="theme-select"', HTML)
        self.assertNotIn("location.reload", HTML)
        self.assertNotIn('theme-preview-line', HTML)
        self.assertNotIn('--preview-bg', HTML)
        self.assertNotIn('--preview-accent', HTML)
        self.assertIn('@container (min-width: 640px)', HTML)
        self.assertIn('@container (min-width: 720px)', HTML)
        self.assertIn('grid-template-columns: repeat(3, minmax(0, 1fr));', HTML)
        self.assertRegex(
            HTML,
            r"\.settings-section\s*\{[^}]*max-width:\s*880px;[^}]*margin:\s*0;",
        )
        self.assertRegex(
            HTML,
            r"#appearance-card\.active\s*\{[^}]*max-width:\s*1120px",
        )
        self.assertRegex(HTML, r"\.theme-options\s*\{[^}]*gap:\s*20px")
        self.assertRegex(HTML, r"\.theme-option\s*\{[^}]*padding:\s*16px")
        self.assertRegex(HTML, r"\.theme-preview\s*\{[^}]*height:\s*140px")

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
        self.assertIn("if (pdfOpenModeSaving) return null;", HTML)
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
        preview_rules = re.findall(r"\.theme-mini-[^{]+\{[^}]+\}", HTML, re.S)
        preview_css = "\n".join(preview_rules)
        for token in (
            "--app-bg", "--sidebar-bg", "--surface-primary", "--surface-secondary",
            "--text-primary", "--text-secondary", "--border-default", "--accent",
            "--accent-soft", "--success", "--success-soft", "--danger", "--danger-soft",
            "--match-block-accent", "--match-inline-bg", "--match-inline-border",
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
            "statistics-settings": "statistics-settings-body",
            "vision-api-settings": "vision-api-body",
            "citation-format-settings": "citation-format-body",
            "bib-completion-settings": "bib-completion-body",
            "appearance-card": "appearance-body",
            "data-location-settings": "data-location-body",
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

        show_start = HTML.index("function showSettingsCategory(sectionId)")
        show_end = HTML.index("function ensureVisibleSettingsCategory()", show_start)
        show_block = HTML[show_start:show_end]
        self.assertIn("s.classList.toggle('active', s === section)", show_block)
        self.assertIn("btn.setAttribute('aria-selected', on ? 'true' : 'false')", show_block)
        self.assertIn("function ensureVisibleSettingsCategory()", HTML)
        self.assertIn(".settings-section.active { display: block; }", HTML)

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

    def test_theme_switch_is_immediate_serialized_and_ignores_stale_results(self) -> None:
        self.assertIn("let persistedTheme = currentTheme;", HTML)
        self.assertIn("let themeRevision = 0;", HTML)
        self.assertIn("let themeSaveQueue = Promise.resolve();", HTML)

        theme_start = HTML.index("async function setTheme(theme)")
        theme_end = HTML.index("renderThemeOptions();", theme_start)
        theme_block = HTML[theme_start:theme_end]
        revision_at = theme_block.index("var revision = ++themeRevision;")
        immediate_apply_at = theme_block.index("applyTheme(theme);")
        request_at = theme_block.index("var request = themeSaveQueue")
        fetch_at = theme_block.index("fetch('/api/preferences'")
        self.assertLess(revision_at, immediate_apply_at)
        self.assertLess(immediate_apply_at, request_at)
        self.assertLess(request_at, fetch_at)
        self.assertIn(
            "themeSaveQueue.catch(function() {}).then(async function()",
            theme_block,
        )
        self.assertIn("themeSaveQueue = request.catch(function() {});", theme_block)
        self.assertEqual(theme_block.count("if (revision !== themeRevision) return;"), 2)
        self.assertIn("applyTheme(persistedTheme);", theme_block)

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
            "midnight": "#FBBF24",
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
            "midnight": "#13345B",
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
        self.assertIn("#08111D", loading)
        self.assertIn("#EEF4FB", loading)
        self.assertIn("#08111D", error)
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
            "midnight": "#08111D",
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
