"""Transient messages and confirmations use theme-aware in-app surfaces."""

from __future__ import annotations

import unittest
import shutil
import subprocess
from pathlib import Path

from src.me_finder.web import HTML


class ToastPresentationTests(unittest.TestCase):
    def test_toast_dom_preserves_text_and_icon(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用")
        source = Path(__file__).resolve().parents[1] / "src/me_finder/static/js/25-toast.js"
        script = r"""
const fs = require('fs');
const vm = require('vm');
function element(tag) {
  return {tag, children: [], attrs: {}, dataset: {}, classList: {add() {}},
    appendChild(child) { this.children.push(child); },
    setAttribute(name, value) { this.attrs[name] = value; },
    remove() { this.removed = true; }};
}
const stack = element('div');
const context = {document: {
  getElementById(id) { return id === 'toast-stack' ? stack : null; },
  createElement: element,
  createElementNS(ns, tag) { if (ns !== 'http://www.w3.org/2000/svg') throw Error(ns); return element(tag); }
}, setTimeout() { return 1; }, clearTimeout() {}, toastDuration() { return 2400; }};
Object.defineProperty(stack, 'firstElementChild', {get() { return this.children[0]; }});
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const toast = context.showToast('<b>原文</b>', 'success');
const icon = toast.children[0].children[0];
if (toast.children[1].textContent !== '<b>原文</b>' || icon.tag !== 'svg'
    || icon.children.length !== 2 || icon.children[1].attrs.d !== 'm5.8 9.2 2.2 2.2 4.2-4.4'
    || icon.attrs['aria-hidden'] !== 'true') throw Error('toast DOM differs');
"""
        subprocess.run([node, "-e", script, str(source)], check=True,
                       capture_output=True, text=True, encoding="utf-8")

    def test_toast_uses_the_app_surface_instead_of_a_black_pill(self) -> None:
        self.assertIn('id="toast-stack"', HTML)
        self.assertNotIn('<div id="toast" class="toast"></div>', HTML)
        self.assertIn("background: var(--surface-elevated);", HTML)
        self.assertIn("box-shadow: var(--shadow-popover);", HTML)
        # 旧样式是 tooltip 的黑底白字。
        self.assertNotIn("background: var(--tooltip-bg);\n  color: var(--tooltip-text);", HTML)

    def test_toast_has_semantic_tones_wired_to_theme_tokens(self) -> None:
        for tone in ("success", "danger", "warning", "info"):
            self.assertIn(f".toast--{tone}", HTML)
            self.assertIn(f"var(--{tone}-soft)", HTML)
        self.assertIn("const TOAST_TONES = ['success', 'danger', 'warning', 'info'];", HTML)
        self.assertIn("var variant = TOAST_TONES.indexOf(tone) >= 0 ? tone : 'info';", HTML)

    def test_toasts_stack_instead_of_overwriting_each_other(self) -> None:
        self.assertIn("const TOAST_STACK_LIMIT = 3;", HTML)
        self.assertIn("while (stack.children.length >= TOAST_STACK_LIMIT)", HTML)
        self.assertIn("stack.appendChild(item);", HTML)

    def test_duration_scales_with_message_length(self) -> None:
        self.assertIn("function toastDuration(text)", HTML)
        self.assertIn("Math.min(6500, Math.max(2400, 1100 + text.length * 110))", HTML)

    def test_toast_never_blocks_clicks_and_sits_above_dialogs(self) -> None:
        self.assertIn("pointer-events: none;", HTML)
        # 统一层级 token（G-03）：toast 用 --z-toast(500)，高于弹窗 --z-modal(400)。
        self.assertIn("z-index: var(--z-toast);", HTML)
        self.assertIn("--z-toast: 500;", HTML)
        self.assertIn("--z-modal: 400;", HTML)

    def test_showtoast_still_accepts_a_bare_message(self) -> None:
        # reader.js 只传一个参数。
        self.assertIn("function showToast(message, tone)", HTML)

    def test_accidental_backdrop_click_cannot_abort_a_running_removal(self) -> None:
        self.assertIn("if (removeRequestController) return;", HTML)

    def test_confirmations_do_not_use_the_windows_webview_black_system_dialog(self) -> None:
        self.assertIn('id="app-dialog-backdrop"', HTML)
        self.assertIn('id="app-dialog-title"', HTML)
        self.assertIn('id="app-dialog-message"', HTML)
        self.assertIn("function showAppConfirm(message, options)", HTML)
        self.assertIn("function showAppAlert(message, options)", HTML)
        self.assertNotRegex(HTML, r"\b(?:window\.)?(?:confirm|alert|prompt)\s*\(")

    def test_app_dialog_uses_theme_tokens_and_safe_text_content(self) -> None:
        self.assertIn("background: var(--dialog-bg);", HTML)
        self.assertIn("box-shadow: var(--shadow-popover);", HTML)
        self.assertIn("messageElement.textContent = String(message || '');", HTML)
        self.assertIn("backdrop.setAttribute('aria-hidden', 'false');", HTML)
        self.assertIn("if (event.key === 'Escape'", HTML)

    def test_bibliographic_overwrite_uses_the_in_app_confirmation(self) -> None:
        self.assertIn("{title:'覆盖人工书目信息？', confirmText:'确认覆盖', tone:'warning'}", HTML)


if __name__ == "__main__":
    unittest.main()
