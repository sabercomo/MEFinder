"""可扩展主题引擎（05-theme-engine.js）的纯逻辑回归测试。

引擎的价值在于「由 accent/background/foreground/contrast 派生全部语义 token 且带
可读性守护」。这里用 node 直接 require 该模块（它对 node 暴露 module.exports），
对派生结果做 WCAG 对比度与结构断言，并覆盖导入校验的非法输入。node 不可用时整类跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_JS = ROOT / "src" / "me_finder" / "static" / "js" / "05-theme-engine.js"
NODE = shutil.which("node")

_HARNESS = r"""
const E = require(process.argv[2]);
const expr = require('fs').readFileSync(process.argv[3], 'utf8');
process.stdout.write(JSON.stringify(eval(expr)));
"""


def _eval(expr: str):
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        payload = Path(tmp) / "expr.js"
        harness.write_text(_HARNESS, encoding="utf-8")
        payload.write_text(expr, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(harness), str(ENGINE_JS), str(payload)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return json.loads(result.stdout)


# 派生结果里必须出现的关键色彩 token（与组件消费的契约对齐）。
REQUIRED_TOKENS = (
    "--app-bg", "--sidebar-bg", "--surface-primary", "--surface-secondary",
    "--surface-elevated", "--surface-hover", "--surface-selected",
    "--text-primary", "--text-secondary", "--text-tertiary", "--text-disabled",
    "--border-subtle", "--border-default", "--border-strong", "--border-control",
    "--accent", "--accent-hover", "--accent-soft", "--accent-contrast", "--accent-text",
    "--input-bg", "--menu-bg", "--dialog-bg", "--tooltip-bg", "--focus-ring",
    "--scrollbar-thumb", "--skeleton-base", "--skeleton-highlight",
    "--success", "--warning", "--danger", "--info", "--neutral",
    "--match-block-accent", "--match-inline-bg",
)


@unittest.skipUnless(NODE, "node 不可用，跳过主题引擎执行测试")
class ThemeEngineDerivationTests(unittest.TestCase):
    def test_every_preset_covers_the_token_contract(self) -> None:
        keys = _eval(
            "E.THEME_PRESETS.map(function(p){return Object.keys(E.deriveThemeTokens(p));})"
        )
        for token_keys in keys:
            for token in REQUIRED_TOKENS:
                self.assertIn(token, token_keys)

    def test_every_preset_is_readable(self) -> None:
        # 正文/背景 ≥ 4.5:1，强调按钮文字/强调 ≥ 4.5:1，强调文字/背景 ≥ 4.0:1。
        report = _eval(
            "E.THEME_PRESETS.map(function(p){var t=E.deriveThemeTokens(p);return {"
            "id:p.id,"
            "text:E.teContrast(t['--text-primary'],t['--app-bg']),"
            "btn:E.teContrast(t['--accent-contrast'],t['--accent']),"
            "atext:E.teContrast(t['--accent-text'],t['--app-bg'])};})"
        )
        for row in report:
            self.assertGreaterEqual(row["text"], 4.5, row["id"])
            self.assertGreaterEqual(row["btn"], 4.5, row["id"])
            self.assertGreaterEqual(row["atext"], 4.0, row["id"])

    def test_white_on_white_is_rescued_by_the_readability_guard(self) -> None:
        ratio = _eval(
            "(function(){var t=E.deriveThemeTokens({mode:'light',accent:'#ffffff',"
            "background:'#ffffff',foreground:'#ffffff',contrast:50});"
            "return E.teContrast(t['--text-primary'],t['--app-bg']);})()"
        )
        self.assertGreaterEqual(ratio, 4.5)

    def test_dark_and_light_use_the_right_color_scheme(self) -> None:
        light_css = _eval(
            "E.themeDefToCss(E.THEME_PRESET_MAP['warm-paper'], ':root')"
        )
        dark_css = _eval(
            "E.themeDefToCss(E.THEME_PRESET_MAP['oled-black'], ':root')"
        )
        self.assertIn("color-scheme: light;", light_css)
        self.assertIn("color-scheme: dark;", dark_css)


@unittest.skipUnless(NODE, "node 不可用，跳过主题引擎执行测试")
class ThemeImportExportTests(unittest.TestCase):
    def test_rejects_malformed_definitions_without_throwing(self) -> None:
        cases = [
            "{}",
            "{mode:'sideways'}",
            "{mode:'dark',accent:'not-a-color',background:'#000',foreground:'#fff'}",
            "null",
            "42",
        ]
        for case in cases:
            result = _eval("E.normalizeThemeDef(" + case + ")")
            self.assertFalse(result["ok"], case)
            self.assertIn("error", result)

    def test_accepts_and_clamps_a_valid_definition(self) -> None:
        result = _eval(
            "E.normalizeThemeDef({mode:'dark',accent:'#6EA8FF',background:'#121722',"
            "foreground:'#EEF3FA',contrast:999,name:'   Deep   '})"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["def"]["contrast"], 100)
        self.assertEqual(result["def"]["mode"], "dark")
        self.assertEqual(result["def"]["schemaVersion"], 1)

    def test_export_is_stable_and_versioned(self) -> None:
        payload = _eval("E.themeDefToExport(E.THEME_PRESET_MAP['midnight-blue'])")
        self.assertEqual(payload["schemaVersion"], 1)
        for key in ("name", "mode", "accent", "background", "foreground", "contrast"):
            self.assertIn(key, payload)

    def test_export_then_reimport_round_trips(self) -> None:
        ok = _eval(
            "(function(){var e=E.themeDefToExport(E.THEME_PRESET_MAP['sepia']);"
            "return E.normalizeThemeDef(e).ok;})()"
        )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
