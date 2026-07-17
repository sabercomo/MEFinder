from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from desktop import error_html, loading_html
from src.me_finder.preferences import (
    DEFAULT_CALIBRATION_VIEW,
    DEFAULT_LIBRARY_VIEW,
    DEFAULT_THEME,
    VALID_THEMES,
    read_preferences,
    save_preferences,
)
from src.me_finder.web import HTML, render_html


class PreferencePersistenceTests(unittest.TestCase):
    @staticmethod
    def default_preferences(theme: str = DEFAULT_THEME) -> dict[str, str]:
        return {
            "theme": theme,
            "library_view": DEFAULT_LIBRARY_VIEW,
            "calibration_view": DEFAULT_CALIBRATION_VIEW,
        }

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

    def test_unsupported_theme_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                save_preferences({"theme": "pitch-black"}, Path(temp_dir) / "preferences.json")


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
        expected_order = ["清霜蓝", "鼠尾草", "暖砂金", "蔷薇雾", "暮云紫", "深海夜"]
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
            "清爽理性，适合日间使用。", "低刺激、安静，适合长时间阅读。",
            "温暖柔和，带轻微纸张气质。", "清柔克制，带淡粉强调。",
            "优雅现代，使用柔和薰衣草紫。", "低亮度深色主题，适合夜间使用。",
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
        self.assertIn('@container (min-width: 960px)', HTML)
        self.assertIn('grid-template-columns: repeat(3, minmax(0, 1fr));', HTML)

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

    def test_initial_html_is_server_rendered_for_selected_theme(self) -> None:
        for theme in self.THEMES:
            rendered = render_html(theme)
            self.assertIn(f'<html lang="zh-CN" data-theme="{theme}">', rendered)

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
            "rose-mist": "#B8860B",
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


if __name__ == "__main__":
    unittest.main()
