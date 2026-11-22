"""Real-browser regressions for UI states that DOM fakes cannot see.

Opt-in: runs only with ``MEFINDER_BROWSER_TESTS=1`` plus the dev-only
``playwright`` package (``requirements-browser-tests.txt``) and an installed Google Chrome (``channel="chrome"``,
so no browser download).  The regular gate and the main CI job skip it; the
``browser-regression`` CI job runs it without blocking while its stability is
being established.  See docs/issues/architecture-followup-2026-09.md.

Scope is the web layer served by the real backend on a disposable database.
The native pywebview two-window handoff is outside this harness and stays with
``test_reader_windows_native`` plus manual smoke checks.

    MEFINDER_BROWSER_TESTS=1 .venv-macos312-arm64/bin/python -m unittest tests.test_browser_regression
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import unittest
from contextlib import closing
from typing import Callable, List
from unittest import mock

import fitz

from src.me_finder.app_context import AppContext
from src.me_finder.structured_reader import get_document_window
from src.me_finder.alignment.text_alignment import generate_alignment
from src.me_finder.components.alignment_runtime import ManagedAlignmentRuntime
from src.me_finder.web import ManagedThreadingHTTPServer, make_handler
from tests import test_text_alignment

try:
    from playwright.sync_api import Page, Route, sync_playwright
except ImportError:  # dev-only dependency
    sync_playwright = None

ENABLED = os.environ.get("MEFINDER_BROWSER_TESTS") == "1"
# The CI browser job sets this so a missing package or Chrome fails loudly
# instead of turning the whole job into a silent skip.
REQUIRED = os.environ.get("MEFINDER_BROWSER_TESTS_REQUIRED") == "1"
TIMEOUT_MS = 10_000

# Elements carrying ``hidden`` that still take up space on screen: the class of
# bug where a component rule such as ``display: grid`` overrides ``[hidden]``.
VISIBLE_HIDDEN_JS = """() => [...document.querySelectorAll('[hidden]')]
  .filter(node => getComputedStyle(node).display !== 'none' && node.getClientRects().length > 0)
  .map(node => node.tagName.toLowerCase() + (node.id ? '#' + node.id : '')
       + (node.className && typeof node.className === 'string' ? '.' + node.className.trim().split(/\\s+/).join('.') : ''))"""


def _alignment_fixture() -> unittest.TestCase:
    """German, Chinese and English EPUB versions of one work (disposable DB).

    Built lazily so unittest does not collect the borrowed fixture's own tests.
    """

    class Fixture(test_text_alignment.TextAlignmentTests):
        def runTest(self) -> None:  # pragma: no cover - fixture holder only
            pass

    fixture = Fixture()
    fixture.setUp()
    return fixture


@unittest.skipUnless(ENABLED or REQUIRED, "set MEFINDER_BROWSER_TESTS=1 to run real-browser regressions")
class BrowserRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            if REQUIRED:
                raise RuntimeError("playwright is required by MEFINDER_BROWSER_TESTS_REQUIRED")
            raise unittest.SkipTest("playwright is a dev-only dependency")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as error:
            cls.playwright.stop()
            if REQUIRED:
                raise
            raise unittest.SkipTest(f"Google Chrome is not available: {error}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.fixture = _alignment_fixture()
        self.addCleanup(self.fixture.directory.cleanup)
        self.addCleanup(self.fixture.embedding_patch.stop)
        root = self.fixture.db.parent
        preferences = root / "config" / "preferences.json"
        preferences.parent.mkdir()
        environment = mock.patch.dict(os.environ, {"ME_FINDER_PREFERENCES": str(preferences)})
        environment.start()
        self.addCleanup(environment.stop)
        generate_alignment(self.fixture.db, "work-one", "pdf-de", "pdf-zh")
        with fitz.open() as pdf:
            original = pdf.new_page(width=160, height=100)
            original.draw_rect(fitz.Rect(0, 0, 80, 100), fill=(0, 0, 1), color=None)
            second = pdf.new_page(width=100, height=180)
            second.draw_rect(second.rect, fill=(0, 1, 0), color=None)
            pdf.save(root / "material-source.pdf")
        with closing(sqlite3.connect(self.fixture.db)) as connection, connection:
            connection.execute(
                "UPDATE source_files SET relative_path=?, payload_json=json_set(payload_json, '$.relative_path', ?) "
                "WHERE source_file_id='pdf-de'", ("material-source.pdf", "material-source.pdf")
            )

        handler = make_handler(self.fixture.db, app_context=AppContext.create(root, index_path=self.fixture.db))
        handler.log_message = lambda *args: None
        server = ManagedThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server() -> None:
            server.shutdown()
            handler.begin_shutdown()
            server.server_close()
            server.wait_for_handlers(timeout=5.0)
            handler.wait_for_durable_operations()
            handler.close_runtime()
            thread.join(timeout=5.0)

        self.addCleanup(stop_server)
        self.base_url = f"http://127.0.0.1:{server.server_port}"

        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(TIMEOUT_MS)
        self.page_errors: List[str] = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.goto(self.base_url + "/")
        self.page.wait_for_load_state("networkidle")

    def tearDown(self) -> None:
        self.assertEqual(self.page_errors, [], "uncaught page errors")

    def test_default_startup_finishes_catalog_before_alignment_overview(self) -> None:
        events = []
        self.page.route("**/api/zotero/status", lambda route: route.fulfill(json={
            "phase": "idle", "rows": [], "documents": {"linked": 2},
            "last_success_at": "2026-11-21T01:00:00Z",
        }))
        self.page.on("request", lambda request: events.append("overview")
                     if "/api/translation-works/overview?" in request.url else None)
        self.page.on("requestfinished", lambda request: events.append("summary")
                     if "/api/library?view=summary" in request.url else None)
        self.page.goto(self.base_url + "/")
        self.wait_until(lambda: "overview" in events and "summary" in events,
                        "startup must load the shared catalog and works")
        self.assertLess(events.index("summary"), events.index("overview"), events)
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(events, ["summary", "overview"])
        self.navigate("文献库")
        self.page.locator("#library-list .library-entry").first.wait_for()

    def test_translation_entry_is_visible_before_works_load(self) -> None:
        for available in (True, False):
            with self.subTest(available=available), mock.patch.object(
                ManagedAlignmentRuntime, "compute_status", return_value={"available": available}
            ):
                self.page.set_viewport_size({"width": 1280 if available else 780, "height": 800})
                held: list[Route] = []
                pattern = "**/api/library?view=summary"

                def hold_catalog(route: Route) -> None:
                    held.append(route)

                self.page.route(pattern, hold_catalog)
                try:
                    self.page.goto(self.base_url + "/", wait_until="domcontentloaded")
                    self.wait_until(lambda: bool(held), "hold the first catalog response")
                    entry = self.page.locator('.sidebar-item[data-page="works"]')
                    self.assertTrue(entry.is_visible())
                    self.assertEqual(entry.locator(".sidebar-item-tag").is_visible(), not available)
                    imports = self.page.locator('.sidebar-item[data-page="import"]')
                    initial_box = imports.bounding_box()
                    entry.click()
                    self.assertTrue(self.page.get_by_text("正在读取作品…", exact=True).is_visible())
                    held.pop().continue_()
                    self.page.locator(".tw-work-item").first.wait_for()
                    self.assertEqual(imports.bounding_box(), initial_box)
                    self.assertEqual(entry.locator(".sidebar-item-tag").is_visible(), not available)
                finally:
                    for route in held:
                        route.abort()
                    self.page.unroute(pattern, hold_catalog)

    def test_search_renders_mineru_formulas_without_losing_hit_highlights(self) -> None:
        text = (
            r"🔎 Bi/SnO $_{2}$ 的表征用 XRD，衍射角为 $27.16^{\circ}$。"
            r"成本为 $10 至 $20。转义符 \$5。未知公式 $\unknown_{2}$。"
        )
        cut = "上" * 175 + r"$636\ cm^{-1}$"
        late_formula = "文" * 96 + r"$SnO_{2}$，高亮公式"
        xrd = text.index("XRD")
        start = late_formula.index(r"_{2}")
        result = {
            "document_title": "MinerU 解析样例", "source_type": "pdf", "citation_page_start": "12",
            "match_type": "exact", "match_score": 1, "highlighted_html": "present",
            "paragraph_text": text, "match_start": xrd, "match_end": xrd + 3,
            "context_before": [{"text": r"上文 $2\theta$"}], "context_after": [{"text": cut}],
        }
        self.page.route("**/api/search", lambda route: route.fulfill(json={
            "total": 2,
            "results": [result, {
                **result, "paragraph_text": late_formula, "match_start": start, "match_end": start + 4,
            }],
        }))
        query = self.page.locator("#query")
        query.fill("XRD")
        query.press("Enter")
        hit = self.page.locator(".detail-hit")
        hit.wait_for()
        self.assertEqual(hit.locator("math").count(), 2)
        self.assertEqual(hit.locator("mark").inner_text(), "XRD")
        self.assertIn(r"$10 至 $20。转义符 \$5。未知公式 $\unknown_{2}$。", hit.inner_text())
        copied = hit.evaluate("""node => {
          const selection = getSelection();
          selection.selectAllChildren(node);
          const clipboardData = new DataTransfer();
          node.dispatchEvent(new ClipboardEvent('copy', {bubbles: true, cancelable: true, clipboardData}));
          selection.removeAllRanges();
          return clipboardData.getData('text/plain');
        }""")
        self.assertEqual(copied, text)
        self.assertIn("12", self.page.locator(".detail-pills").inner_text())
        self.assertEqual(self.page.locator(".detail-context-before .detail-context-preview math").count(), 1)
        self.assertEqual(self.page.locator(".detail-context-after .detail-context-preview math").count(), 1)
        self.page.get_by_role("button", name="展开下文").click()
        self.assertTrue(self.page.locator(".detail-context-after .detail-context-full math").is_visible())
        self.assertEqual(self.page.locator(".result-snippet").nth(1).locator("mark math").count(), 1)
        self.page.locator(".result-row").nth(1).click()
        self.assertEqual(hit.locator("mark math").count(), 1)

    def test_search_renders_numbered_display_formulas_from_mineru(self) -> None:
        formulas = (
            r"\eta = (1 - C_{t} / C_{0}) \times 100 \%\tag{1}",
            r"(a h v)^{1/n} = A\left(h v - E_{g}\right)\tag{3}",
        )
        texts = [
            "光催化降解效率的计算如公式所示：\n$$\n" + formula + "\n$$\n式中各量的含义见正文"
            for formula in formulas
        ]
        self.page.route("**/api/search", lambda route: route.fulfill(json={
            "total": 2,
            "results": [{
                "document_title": "MinerU 独立公式反馈", "source_type": "pdf",
                "citation_page_start": "12", "match_type": "exact", "match_score": 1,
                "highlighted_html": "present", "paragraph_text": text,
                "match_start": text.index(r"\tag"), "match_end": text.index(r"\tag") + 7,
                "context_before": [{"text": texts[1 - index]}],
                "context_after": [{"text": r"行内 $E_{g}$、角度 $27.16^{\circ}$"}],
            } for index, text in enumerate(texts)],
        }))
        self.page.locator("#query").fill("公式")
        self.page.locator("#query").press("Enter")
        hit = self.page.locator(".detail-hit")
        hit.wait_for()
        for index, number in enumerate(("1", "3")):
            self.page.locator(".result-row").nth(index).click()
            self.assertEqual(hit.locator('math[display="block"]').count(), 1)
            self.assertIn(f"({number})", hit.locator("mtext").all_text_contents())
            self.assertEqual(hit.locator('mark math[display="block"]').count(), 1)
            self.assertNotIn("$$", hit.inner_text())
            self.assertNotIn(r"\tag", hit.inner_text())
            self.assertEqual(
                self.page.locator(".detail-context-before .detail-context-preview math[display=block]").count(),
                1,
            )
            self.assertEqual(self.page.locator(".detail-context-after .detail-context-preview math").count(), 2)
            self.assertEqual(self.page.locator(".detail-context-after math[display=block]").count(), 0)
            copied = hit.evaluate("""node => {
              const selection = getSelection();
              selection.selectAllChildren(node);
              const clipboardData = new DataTransfer();
              node.dispatchEvent(new ClipboardEvent('copy', {bubbles: true, cancelable: true, clipboardData}));
              selection.removeAllRanges();
              return clipboardData.getData('text/plain');
            }""")
            self.assertEqual(copied, texts[index])

    def test_mineru_accounts_can_be_deleted_directly_from_their_rows(self) -> None:
        url = self.base_url + "/api/mineru-accounts"
        for account_id, name, enabled in (("work", "工作账号", True), ("spare", "备用账号", False)):
            reply = self.context.request.post(url, data={
                "account_id": account_id, "display_name": name, "token": "fixture-token",
                "enabled": enabled,
            })
            self.assertEqual(reply.status, 200, reply.text())

        self.navigate("设置")
        self.page.get_by_role("tab", name="MinerU API").click()
        spare = self.page.get_by_role("button", name="删除备用账号")
        work = self.page.get_by_role("button", name="删除工作账号")
        editor = self.page.locator("#mineru-editor-card")
        dialog = self.page.locator("#app-dialog")
        cancel = dialog.get_by_role("button", name="取消", exact=True)
        confirm = dialog.get_by_role("button", name="删除", exact=True)

        spare.click()
        dialog.wait_for()
        self.assertEqual(dialog.locator("#app-dialog-title").inner_text(), "删除「备用账号」？")
        self.assertFalse(editor.is_visible())
        self.wait_until(lambda: cancel.evaluate("node => node === document.activeElement"),
                        "the confirmation should focus Cancel")
        self.page.keyboard.press("Enter")
        dialog.wait_for(state="hidden")
        self.assertTrue(spare.evaluate("node => node === document.activeElement"))
        self.assertTrue(spare.is_visible())
        self.assertEqual(len(self.context.request.get(url).json()["accounts"]), 2)

        spare.click()
        self.page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        self.assertTrue(spare.is_visible())

        def block_delete(route: Route) -> None:
            if route.request.method == "POST":
                route.fulfill(status=400, json={"error": "该账号仍有未完成的解析任务"})
            else:
                route.continue_()

        self.page.route(url, block_delete)
        spare.click()
        confirm.click()
        self.page.get_by_text("删除 MinerU 账号失败：该账号仍有未完成的解析任务").wait_for()
        self.assertTrue(spare.is_enabled())
        self.assertTrue(spare.evaluate("node => node === document.activeElement"))
        self.page.unroute(url, block_delete)

        spare.click()
        confirm.click()
        spare.wait_for(state="detached")
        self.wait_until(lambda: work.evaluate("node => node === document.activeElement"),
                        "focus should move to the remaining account row")
        self.assertFalse(editor.is_visible())
        self.assertEqual([item["account_id"] for item in self.context.request.get(url).json()["accounts"]], ["work"])

        work.click()
        confirm.click()
        work.wait_for(state="detached")
        editor.wait_for()
        self.assertEqual(self.page.locator("#mineru-editor-title").inner_text(), "添加账号")
        self.assertEqual(self.context.request.get(url).json()["accounts"], [])

    def test_vision_provider_editor_expands_in_place_and_delete_preserves_other_draft(self) -> None:
        url = self.base_url + "/api/vision-providers"
        for provider_id, name in (("work", "工作接口"), ("spare", "备用接口")):
            reply = self.context.request.post(url, data={
                "action": "save_provider", "provider": {
                    "id": provider_id, "name": name, "api_base": "https://example.com/v1",
                    "model": "test-model", "api_key": "fixture-key", "enabled": True,
                },
            })
            self.assertEqual(reply.status, 200, reply.text())
        self.page.route("**/api/vision-providers/models", lambda route: route.fulfill(json={"models": []}))
        self.navigate("设置")
        self.page.get_by_role("tab", name="其他解析 API").click()
        self.page.get_by_role("button", name="删除备用接口").wait_for()
        panel = self.page.locator("#vision-api-settings")
        editor = panel.locator("#vision-editor-card")
        self.assertFalse(editor.is_visible())

        add = panel.get_by_role("button", name="添加接口")
        add.click()
        self.assertEqual(editor.locator("#vision-editor-title").inner_text(), "添加接口")
        editor.get_by_role("button", name="取消").click()
        self.assertFalse(editor.is_visible())
        self.assertTrue(add.evaluate("node => document.activeElement === node"))

        edit = panel.get_by_role("button", name="编辑工作接口")
        edit.click()
        edit_row = panel.locator('.vision-ledger-row[data-provider-id="work"]')
        self.assertTrue(edit_row.locator("#vision-editor-card").is_visible())
        self.assertEqual(edit.get_attribute("aria-expanded"), "true")
        self.assertEqual(editor.locator("#vision-api-key").input_value(), "")
        fields = editor.locator(".settings-input, #vision-model-refresh")
        self.assertEqual(fields.evaluate_all(
            "nodes => nodes.map(node => node.getBoundingClientRect().height)"
        ), [36] * 5)
        empty_models = editor.get_by_text("接口未返回模型，请手动填写模型名")
        empty_models.wait_for()
        self.assertNotIn("is-ready", empty_models.get_attribute("class"))
        name = editor.locator("#vision-provider-name")
        name.fill("修改中的接口")

        delete = panel.get_by_role("button", name="删除备用接口")
        delete.click()
        dialog = self.page.locator("#app-dialog")
        self.assertEqual(dialog.locator("#app-dialog-title").inner_text(), "删除「备用接口」？")
        cancel = dialog.get_by_role("button", name="取消")
        self.wait_until(lambda: cancel.evaluate("node => node === document.activeElement"),
                        "delete confirmation should focus Cancel")
        self.page.keyboard.press("Shift+Tab")
        self.assertTrue(dialog.get_by_role("button", name="删除").evaluate("node => node === document.activeElement"))
        self.page.keyboard.press("Escape")
        self.assertTrue(delete.evaluate("node => node === document.activeElement"))
        self.assertTrue(delete.is_visible())

        delete.click()
        dialog.get_by_role("button", name="删除").click()
        delete.wait_for(state="detached")
        self.assertEqual(name.input_value(), "修改中的接口")
        self.assertTrue(edit_row.locator("#vision-editor-card").is_visible())
        editor.get_by_role("button", name="保存接口").click()
        panel.get_by_role("button", name="编辑修改中的接口").wait_for()
        self.assertFalse(editor.is_visible())
        saved = self.context.request.get(url).json()["providers"]
        self.assertEqual([(item["id"], item["name"], item["has_api_key"]) for item in saved],
                         [("work", "修改中的接口", True)])

        add.click()
        editor.locator("#vision-api-base").fill("https://new.example.com/v1")
        editor.locator("#vision-api-key").fill("new-fixture-key")
        editor.locator("#vision-provider-name").fill("新增接口")
        editor.locator("#vision-model").fill("demo-model")
        editor.get_by_role("button", name="保存接口").click()
        panel.get_by_role("button", name="编辑新增接口").wait_for()
        self.assertFalse(editor.is_visible())

    def test_local_ocr_switch_saves_only_enabled_and_paths_save_separately(self) -> None:
        url = self.base_url + "/api/local-ocr"
        root = self.fixture.db.parent
        files = [root / name for name in ("python", "ocr.py", "other_ocr.py")]
        for file in files:
            file.touch()
        python, script, replacement = map(str, files)
        engine = {"enabled": True, "python_path": python, "script_path": script}
        reply = self.context.request.post(url, data={
            "engines": {"ndlocr-lite": engine, "ndlkotenocr-lite": {"enabled": False}},
        })
        self.assertEqual(reply.status, 200, reply.text())
        self.navigate("设置")
        self.page.get_by_role("tab", name="本地 OCR").click()
        panel = self.page.locator("#local-ocr-settings")
        row = panel.locator('[data-local-ocr-engine="ndlocr-lite"]')
        row.get_by_text("手动配置", exact=True).wait_for()
        self.assertFalse(panel.locator("#local-ocr-save").count())
        row.get_by_text("手动指定运行时").click()
        field = row.locator("#local-ocr-modern-script")
        field.fill(replacement)
        switch = row.locator('label.ui-switch')
        with self.page.expect_request(lambda request: request.url == url and request.method == "POST") as sent:
            switch.click()
        self.assertEqual(sent.value.post_data_json, {
            "action": "update_engine", "provider_id": "ndlocr-lite", "enabled": False,
        })
        row.get_by_text("已保存", exact=True).wait_for()
        self.assertEqual(field.input_value(), replacement)
        saved = self.context.request.get(url).json()["engines"][0]
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["script_path"], script)

        row.get_by_role("button", name="保存路径").click()
        row.get_by_text("路径已保存", exact=True).wait_for()
        saved = self.context.request.get(url).json()["engines"][0]
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["script_path"], replacement)

        def reject_save(route: Route) -> None:
            if route.request.method == "POST":
                route.fulfill(status=400, json={"error": "测试保存失败"})
            else:
                route.continue_()

        self.page.route(url, reject_save)
        switch.click()
        row.get_by_text("未能保存，已恢复原设置：测试保存失败").wait_for()
        self.assertFalse(row.get_by_role("switch").is_checked())
        self.assertEqual(switch.inner_text(), "关闭")

    def test_custom_appearance_controls_start_collapsed(self) -> None:
        self.navigate("设置")
        self.page.get_by_role("tab", name="外观").click()
        toggle = self.page.get_by_role("button", name=re.compile("自定义配色"))
        body = self.page.locator("#appearance-custom-body")
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
        self.assertFalse(body.is_visible())
        self.assertEqual(self.page.locator("#appearance-mode-seg button").count(), 3)
        self.assertTrue(self.page.locator("#theme-options").is_visible())
        toggle.click()
        self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
        self.assertTrue(body.is_visible())

    def test_about_links_homebrew_and_platform_visibility(self) -> None:
        self.page.add_init_script("""localStorage.removeItem('meFinderSidebarCollapsed');
        Object.defineProperty(navigator, 'clipboard', {value: {
          writeText: async text => { window.copiedCommand = text; }
        }});""")
        self.context.route("**/api/update/status", lambda route: route.fulfill(json={
            "status": "idle", "current_version": "0.5.9", "can_self_update": True,
            "message": "尚未检查更新",
        }))
        links = {
            "官方网站": "https://sabercomo.github.io/MEFinder/",
            "更新日志": "https://github.com/sabercomo/MEFinder/releases",
            "最近更新": "https://github.com/sabercomo/MEFinder/releases/latest",
            "去点 Star": "https://github.com/sabercomo/MEFinder",
        }
        for shell in ("", "macos", "win32"):
            with self.subTest(shell=shell):
                with mock.patch.dict(os.environ, {"ME_FINDER_DESKTOP_SHELL": shell}):
                    self.page.reload()
                    self.page.wait_for_load_state("networkidle")
                self.navigate("设置")
                self.page.get_by_role("tab", name="关于与更新").click()
                panel = self.page.locator("#software-update-settings")
                self.assertEqual(panel.locator(".about-update-card").evaluate("""node => {
                  const style = getComputedStyle(node);
                  return [style.borderTopWidth, style.borderTopStyle];
                }"""), ["1px", "solid"], "about content has a visible frame")
                for label, url in links.items():
                    link = panel.get_by_role("link", name=label, exact=True)
                    self.assertTrue(link.is_visible())
                    self.assertEqual(link.get_attribute("href"), url)
                    self.assertEqual(link.get_attribute("target"), "_blank")
                    self.assertIn("noopener", link.get_attribute("rel"))
                self.assertEqual(self.page.locator(".about-platform-windows").is_visible(), shell == "win32")
                self.assertEqual(self.page.locator(".about-platform-macos").is_visible(), shell == "macos")
                self.assertEqual(self.page.locator(".about-platform-browser").is_visible(), shell == "")
                if shell == "macos":
                    panel.get_by_role("button", name="复制命令").click()
                    self.assertEqual(self.page.evaluate("window.copiedCommand"),
                                     "brew update && brew upgrade --cask mefinder")
                    self.assertTrue(panel.get_by_role("button", name="已复制").is_visible())
                self.page.get_by_role("button", name="收起侧边栏").click()
                self.page.locator(".app-top-nav .sidebar-nav").wait_for()
                self.page.locator("html.nav-morph").wait_for(state="detached")
                for width in (320, 375, 414, 768, 1280):
                    self.page.set_viewport_size({"width": width, "height": 900})
                    overflow = panel.evaluate("""node => [...node.querySelectorAll('*')]
                      .filter(child => child.getClientRects().length &&
                        child.getBoundingClientRect().right > node.getBoundingClientRect().right + 1)
                      .map(child => child.className.baseVal ?? child.className)""")
                    self.assertEqual(overflow, [], f"about panel overflow: {shell}/{width}")
                    self.assertTrue(panel.get_by_role("link", name="去点 Star").evaluate(
                        "node => node.getBoundingClientRect().right <= innerWidth"))
                self.assert_hidden_really_hidden("about/" + shell)
                self.page.get_by_role("button", name="展开侧边栏").click()

    def test_navigation_moves_to_titlebar_and_preserves_page_and_choice(self) -> None:
        self.page.emulate_media(reduced_motion="reduce")
        self.page.locator("#query").fill("社会关系")
        self.page.evaluate("window.savedNavigation = document.querySelector('.sidebar-nav')")
        self.page.get_by_role("button", name="收起侧边栏").click()
        top = self.page.locator(".app-top-nav .sidebar-nav")
        self.assertTrue(top.is_visible())
        self.assertTrue(top.evaluate("nav => nav === window.savedNavigation"))
        self.assertFalse(self.page.locator(".sidebar").is_visible())
        self.assertEqual(self.page.locator("#query").input_value(), "社会关系")
        self.assertEqual(top.get_by_role("button", name="文献检索").get_attribute("aria-current"), "page")
        self.page.locator(".app-title-actions").get_by_role("button", name="设置").click()
        self.assertTrue(self.page.locator("#page-settings").is_visible())
        self.page.reload()
        self.assertTrue(top.is_visible())
        self.page.get_by_role("button", name="展开侧边栏").click()
        self.assertTrue(self.page.locator(".sidebar .sidebar-nav").is_visible())
        self.assertEqual(self.page.locator(".app-title-page").inner_text(), "文献检索")
        self.page.set_viewport_size({"width": 320, "height": 800})
        self.wait_until(top.is_visible, "narrow windows should use the top navigation")
        self.assertTrue(top.get_by_role("button", name="文献导入").is_visible())
        self.page.set_viewport_size({"width": 1280, "height": 800})
        self.wait_until(self.page.locator(".sidebar .sidebar-nav").is_visible,
                        "widening should restore the user's sidebar choice")

    def test_titlebar_stays_in_one_row_and_keeps_the_sidebar_button_in_a_narrow_window(self) -> None:
        self.page.emulate_media(reduced_motion="reduce")
        self.page.set_viewport_size({"width": 531, "height": 800})
        bar = self.page.locator(".app-titlebar")
        nav = bar.locator(".app-top-nav")
        toggle = bar.get_by_role("button", name="展开侧边栏")
        toggle.wait_for(state="visible")
        self.page.locator('[data-page="works"]').evaluate("node => { node.hidden = false; }")
        self.assertEqual(nav.get_by_role("button").count(), 5)
        self.assertEqual(bar.bounding_box()["height"], 50)
        self.assertTrue(nav.evaluate("""node => {
            const style = getComputedStyle(node);
            return node.scrollWidth === node.clientWidth && parseFloat(style.borderTopRightRadius) > 0;
        }"""))
        self.assertTrue(nav.get_by_text("文献导入", exact=True).is_visible())
        brand = bar.get_by_text("MEFinder", exact=True)
        self.assertEqual(brand.bounding_box()["x"], 12)
        self.assertGreater(toggle.bounding_box()["x"], brand.bounding_box()["x"] + brand.bounding_box()["width"])
        toggle.click()
        self.assertTrue(self.page.locator(".sidebar #app-nav").is_visible())
        self.assertEqual(brand.bounding_box()["x"], 12)
        self.assertTrue(bar.get_by_role("button", name="收起侧边栏").is_visible())

    def test_about_macos_update_failure_and_retry(self) -> None:
        responses = [
            {"status": "error", "message": "无法连接 GitHub，请稍后重试"},
            {"status": "available", "latest_version": "0.6.0", "message": "发现新版本 v0.6.0",
             "release_url": "https://github.com/sabercomo/MEFinder/releases/tag/v0.6.0"},
            {"status": "up_to_date", "message": "当前已是最新版本"},
        ]
        self.context.route("**/api/macos-update", lambda route: route.fulfill(json=responses.pop(0)))
        with mock.patch.dict(os.environ, {"ME_FINDER_DESKTOP_SHELL": "macos"}):
            self.page.reload()
            self.page.wait_for_load_state("networkidle")
        self.navigate("设置")
        self.page.get_by_role("tab", name="关于与更新").click()
        check = self.page.locator("#macos-update-check")
        release = self.page.locator("#macos-update-release")
        for label in ("检查失败", "有新版本", "已是最新"):
            check.click()
            self.wait_until(lambda: self.page.locator("#macos-update-status").inner_text() == label,
                            "update state must settle")
            self.assertTrue(check.is_enabled())
            self.assertEqual(release.is_visible(), label == "有新版本")
            if label == "有新版本":
                self.assertEqual(release.inner_text(), "下载 v0.6.0 DMG")
                self.assertEqual(release.get_attribute("href"),
                                 "https://github.com/sabercomo/MEFinder/releases/tag/v0.6.0")
            else:
                self.assertIsNone(release.get_attribute("href"))

    def test_about_windows_update_download_and_ready(self) -> None:
        state = {"status": "idle", "can_self_update": True, "current_version": "0.5.9",
                 "message": "尚未检查更新"}
        self.context.route("**/api/update/status", lambda route: route.fulfill(json=state))
        self.context.route("**/api/update/check", lambda route: route.fulfill(json={
            **state, "status": "available", "latest_version": "0.6.0", "message": "发现新版本",
            "release_url": "https://github.com/sabercomo/MEFinder/releases/tag/v0.6.0",
        }))
        self.context.route("**/api/update/download", lambda route: route.fulfill(json={
            **state, "status": "ready", "latest_version": "0.6.0", "message": "更新已校验",
            "install_token": "test-token",
        }))
        with mock.patch.dict(os.environ, {"ME_FINDER_DESKTOP_SHELL": "win32"}):
            self.page.reload()
            self.page.wait_for_load_state("networkidle")
        self.navigate("设置")
        self.page.get_by_role("tab", name="关于与更新").click()
        self.page.get_by_role("button", name="检查更新", exact=True).click()
        self.page.get_by_role("button", name="下载 v0.6.0", exact=True).click()
        self.page.get_by_role("button", name="退出并安装", exact=True).wait_for()
        self.assertEqual(self.page.locator("#update-status-badge").inner_text(), "可安装")

    # ── helpers ────────────────────────────────────────────────────
    def navigate(self, label: str) -> None:
        self.page.locator(".sidebar-item", has_text=label).first.click()

    def assert_hidden_really_hidden(self, state: str) -> None:
        self.assertEqual(self.page.evaluate(VISIBLE_HIDDEN_JS), [], f"[hidden] still visible in: {state}")

    def wait_until(self, predicate: Callable[[], bool], description: str) -> None:
        deadline = TIMEOUT_MS
        while deadline > 0:
            if predicate():
                return
            self.page.wait_for_timeout(100)
            deadline -= 100
        self.fail(description)

    def model_radios(self):
        return self.page.locator('input[name="alignment-embedding-model"]')

    def checked_models(self) -> List[str]:
        return self.page.eval_on_selector_all(
            'input[name="alignment-embedding-model"]',
            "inputs => inputs.filter(input => input.checked).map(input => input.value)",
        )

    def saved_model(self) -> str:
        response = self.context.request.get(self.base_url + "/api/preferences")
        return response.json()["alignment_embedding_model_id"]

    def pair_line(self, pattern: str):
        return self.page.locator(".tw-pair-line").filter(has_text=re.compile(pattern))

    def reader_page_labels(self) -> List[str]:
        return self.page.eval_on_selector_all(
            ".mef-reader-source-pane .mef-reader-item-label",
            "nodes => nodes.filter(node => node.getClientRects().length).map(node => node.textContent.trim())",
        )

    def stored_page_labels(self, source_id: str) -> List[str]:
        """Page wording the backend stores for each reading unit of a book."""

        items = get_document_window(self.fixture.db, source_id, count=10)["items"]
        return [str(item["page_display"]) for item in items]

    def give_books_distinct_pages(self) -> None:
        """Calibrate the German PDF to page 38 and give the EPUB publisher pages 27–28.

        With distinct page wording per book, a label left over from the
        previous book cannot pass for the new one.
        """

        with closing(sqlite3.connect(self.fixture.db)) as connection, connection:
            row = connection.execute(
                "SELECT row_id, payload_json FROM pdf_pages WHERE source_file_id = 'pdf-de'"
            ).fetchone()
            payload = json.loads(row[1])
            payload.update(citation_page="38", page_mapping_method="manual_segment",
                           segment_id="MAPSEG-000000-000000")
            connection.execute("UPDATE pdf_pages SET payload_json = ? WHERE row_id = ?",
                               (json.dumps(payload, ensure_ascii=False), row[0]))
            for index, page in ((0, "27"), (1, "28")):
                paragraph_id = f"epub-en-p{index}"
                payload = json.loads(connection.execute(
                    "SELECT payload_json FROM paragraphs WHERE paragraph_id = ?", (paragraph_id,)
                ).fetchone()[0])
                payload.update(page_source_type="epub_page_list", page_display=page,
                               original_page_start=page, original_page_end=page)
                connection.execute(
                    "UPDATE paragraphs SET page_source_type = 'epub_page_list', page_display = ?, "
                    "citation_page_start = ?, citation_page_end = ?, payload_json = ? "
                    "WHERE paragraph_id = ?",
                    (page, page, page, json.dumps(payload, ensure_ascii=False), paragraph_id),
                )

    def reader_texts(self) -> List[str]:
        return self.page.eval_on_selector_all(
            ".mef-reader-source-pane .mef-reader-item-text",
            "nodes => nodes.filter(node => node.getClientRects().length).map(node => node.textContent.trim())",
        )

    # ── scenarios ──────────────────────────────────────────────────
    def test_import_progress_recovers_after_local_status_request_disconnects(self) -> None:
        state = {"calls": 0, "completed": False}

        def status(route: Route) -> None:
            state["calls"] += 1
            if state["calls"] == 1:
                route.abort("connectionreset")
                return
            route.fulfill(json={
                "status": "completed" if state["completed"] else "processing",
                "phase": "completed" if state["completed"] else "rebuilding_index",
                "message": "导入完成" if state["completed"] else "正在写入本地 SQLite 索引…",
                "parse_route": "native",
            })

        self.page.route("**/api/import-status?job_id=browser-import", status)
        self.navigate("文献导入")
        self.page.evaluate("""() => {
          MEFinder.importJobs.add({id: 'browser-import', jobId: 'browser-import',
            name: 'local.pdf', size: 1024, type: 'pdf', status: 'processing',
            detectedType: 'native_text', route: 'native', step: 2});
          MEFinder.imports.pollJob('browser-import');
        }""")
        item = self.page.locator('.import-item[data-id="browser-import"]')
        item.locator(".import-item-status").filter(has_text="重试").wait_for()
        self.assertEqual(item.locator(".error").count(), 0)
        self.assertIn("本地解析", item.locator(".import-step.active").inner_text())
        item.get_by_text("正在写入本地 SQLite 索引…", exact=True).wait_for()
        self.assertIn("建立索引", item.locator(".import-step.active").inner_text())
        state["completed"] = True
        item.locator(".import-item-status.done").wait_for()
        self.assertEqual(item.locator(".import-item-status").inner_text(), "导入完成")
        self.assertEqual(item.locator(".error").count(), 0)

    def test_import_task_survives_navigation_cancels_and_resumes_after_reload(self) -> None:
        state = {"status": "processing"}

        def status(route: Route) -> None:
            if state["status"] == "gone":
                route.fulfill(status=404, json={"error": "导入任务不存在。"})
            else:
                route.fulfill(json={**state, "message": "正在处理", "source_file_id": "pdf-zh"})

        self.page.route("**/api/import-status?*", status)
        self.page.route("**/api/import-active", lambda route: route.fulfill(json={"jobs": [{
            "job_id": "browser-import", "file_name": "队列验收.pdf", "file_type": "pdf",
            "status": "processing", "parse_route": "local_ocr",
        }]}))
        self.page.route("**/api/import-resume-dismiss", lambda route: route.fulfill(json={"state": "cancelling"}))
        self.page.evaluate("() => MEFinder.imports.loadActiveImports()")
        self.navigate("文献导入")
        row = self.page.locator(".import-item").filter(has_text="队列验收.pdf")
        row.locator(".import-item-remove").click()
        self.page.get_by_role("button", name="停止任务", exact=True).click()
        self.wait_until(lambda: "停止" in row.inner_text(), "cancel must remain visible")
        self.assertTrue(row.locator(".import-item-remove").is_disabled())
        self.navigate("文献检索")
        state["status"] = "gone"
        self.wait_until(lambda: self.page.evaluate("MEFinder.importJobs.snapshot().length") == 0,
                        "background polling must finish cancellation away from the import page")
        self.navigate("文献导入")
        self.assertFalse(row.is_visible())

        self.page.unroute("**/api/import-active")
        self.page.route("**/api/import-resumable", lambda route: route.fulfill(json={"jobs": [{
            "job_id": "browser-resume", "file_name": "恢复验收.pdf", "file_type": "pdf",
            "status": "paused", "can_resume": True, "failure_stage": "index", "parse_route": "vision",
        }]}))
        self.page.route("**/api/import-resume", lambda route: route.fulfill(json={"job_id": "browser-resume"}))
        state["status"] = "completed"
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        self.navigate("文献导入")
        row = self.page.locator(".import-item").filter(has_text="恢复验收.pdf")
        row.get_by_role("button", name="重新建立索引", exact=True).click()
        self.wait_until(lambda: self.page.evaluate("MEFinder.importJobs.snapshot()[0].status") == "done",
                        "resumed task must finish through the task owner")
        self.assertEqual(row.locator(".import-item-status.done").count(), 1)
        self.assertEqual(self.page.locator(".app-dialog-backdrop.open").count(), 0)

    def test_zotero_refreshes_search_library_and_versions_across_page_changes(self) -> None:
        catalog = self.context.request.get(self.base_url + "/api/library?view=summary").json()
        source = next(item for item in catalog["items"] if item["source_file_id"] == "pdf-zh")
        status = {"phase": "idle", "rows": [], "documents": {"linked": 0}}
        requests = []

        def summary(route: Route) -> None:
            requests.append(route.request.url)
            route.fulfill(json=catalog)

        self.page.route("**/api/library?view=summary", summary)
        self.page.route("**/api/zotero/status", lambda route: route.fulfill(json=status))
        self.page.evaluate("searchStore.documentId = 'pdf-zh'")
        for count, label in enumerate(("文献检索", "译本对照", "文献库", "文献导入"), 1):
            with self.subTest(view=label):
                self.navigate(label)
                self.page.wait_for_load_state("networkidle")
                title = f"同步后的中文译本 {count}"
                source["title"] = title
                status["documents"]["linked"] = count
                before = len(requests)
                self.page.evaluate("() => MEFinder.zotero.start()")
                self.assertEqual(len(requests) - before, 1, "all views share one catalog request")
                self.navigate("文献检索")
                self.assertEqual(self.page.locator("#document-select-label").inner_text(), title)
                self.page.locator("#document-select-trigger").click()
                self.assertIn(title, self.page.locator("#document-options").inner_text())
                self.page.locator("#document-select-trigger").click()
                self.navigate("译本对照")
                self.wait_until(
                    lambda: any(title in text for text in self.page.locator(".tw-version-sub").all_text_contents()),
                    "version metadata should update after Zotero completion",
                )
                self.navigate("文献库")
                self.page.wait_for_load_state("networkidle")
                self.assertIn(title, self.page.locator("#library-list").inner_text())

    def test_assign_search_keeps_ime_input_and_caret(self) -> None:
        self.navigate("译本对照")
        self.page.get_by_role("button", name="新建作品", exact=True).click()
        field = self.page.locator("#tw-assign-query")
        field.focus()
        self.page.evaluate("window.assignInput = document.getElementById('tw-assign-query')")
        cdp = self.context.new_cdp_session(self.page)
        self.addCleanup(cdp.detach)
        cdp.send("Input.imeSetComposition", {
            "text": "jingshen", "selectionStart": 8, "selectionEnd": 8,
        })
        self.assertTrue(self.page.evaluate(
            "window.assignInput === document.activeElement && window.assignInput.isConnected"
        ), "composition must keep the original input")
        cdp.send("Input.insertText", {"text": "精神"})
        self.assertEqual(field.input_value(), "精神")
        self.assertGreater(self.page.locator(".tw-pick-row").count(), 0)
        # Editing in the middle must retain the same node and insertion point.
        self.page.keyboard.press("Home")
        self.page.keyboard.insert_text("现象")
        self.assertEqual(field.input_value(), "现象精神")
        self.assertEqual(field.evaluate("node => node.selectionStart"), 2)
        self.assertTrue(self.page.evaluate("window.assignInput === document.activeElement"))

    def test_assign_menu_stays_at_trigger_width(self) -> None:
        self.navigate("译本对照")
        self.page.get_by_role("button", name="新建作品", exact=True).click()
        trigger = self.page.locator("#tw-assign-target .app-select-trigger")
        trigger.click()
        menu = self.page.locator(".tw-select-menu")
        menu.wait_for(state="visible")
        self.page.wait_for_timeout(200)  # wait for the existing entrance transition
        bounds = menu.bounding_box()
        anchor = trigger.bounding_box()
        self.assertAlmostEqual(bounds["width"], max(anchor["width"], 240), delta=1)
        self.assertAlmostEqual(bounds["x"], anchor["x"], delta=1)
        self.assertLessEqual(bounds["x"] + bounds["width"], 1280)
        menu.get_by_role("option", name="精神现象学").click()
        self.assertEqual(trigger.inner_text().strip(), "精神现象学")
        self.assertEqual(trigger.get_attribute("aria-expanded"), "false")

    def test_settings_search_keeps_original_controls_and_restores_category(self) -> None:
        page = self.page
        self.navigate("设置")
        page.get_by_role("tab", name="外观").click()
        page.wait_for_load_state("networkidle")
        settings_posts = []
        page.on("request", lambda request: settings_posts.append(request.url) if request.method == "POST" else None)
        search = page.get_by_role("searchbox", name="搜索设置")
        apa = page.locator('input[name="citation-format"][value="apa"]')
        apa.evaluate("control => { window.originalCitationOption = control; }")
        search.fill("引文 APA")
        apa.locator("..").wait_for()
        self.assertTrue(apa.evaluate("control => control === window.originalCitationOption"))
        self.assertFalse(page.locator('[data-citation-style="mla"]').is_visible())
        with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/api/preferences")):
            page.locator('[data-citation-style="apa"]').click()
        self.assertTrue(apa.is_checked())
        post_count = len(settings_posts)
        search.fill("打开 PDF 原文")
        self.assertFalse(page.locator("#pdf-reader-settings").is_visible())
        search.fill("唯一无结果测试")
        page.get_by_text("没有匹配的设置", exact=True).wait_for()
        page.evaluate("""() => {
            const row = document.createElement('div');
            row.className = 'settings-choice-row';
            row.textContent = '唯一无结果测试';
            document.getElementById('backup-settings-body').append(row);
        }""")
        page.get_by_text("唯一无结果测试", exact=True).wait_for()
        page.set_viewport_size({"width": 375, "height": 680})
        self.assertTrue(search.is_visible())
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        search.press("Escape")
        self.assertEqual(search.input_value(), "")
        self.assertTrue(page.get_by_role("tab", name="外观").get_attribute("aria-selected") == "true")
        self.assertTrue(page.locator("#appearance-card").is_visible())
        self.assertEqual(len(settings_posts), post_count)

    def test_material_image_file_drop_and_paste_share_the_import_page(self) -> None:
        page = self.page
        page.locator("#materials-nav").click()
        page.locator("#materials-filter").fill("会隐藏新图片")
        type_trigger = page.get_by_role("button", name="素材类型：全部")
        type_trigger.focus()
        type_trigger.press("ArrowDown")
        page.wait_for_function("document.activeElement?.matches('#materials-type [data-value=all]')")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        self.assertTrue(page.get_by_role("button", name="素材类型：文字").evaluate(
            "button => document.activeElement === button"
        ))
        page.locator("#materials-add-image").click()
        with fitz.open() as pdf:
            pdf.new_page(width=720, height=540)
            png = pdf[0].get_pixmap().tobytes("png")
        page.locator("#materials-image-import input[type=file]").set_input_files(
            {"name": "diagram.png", "mimeType": "image/png", "buffer": png}
        )
        page.get_by_role("textbox", name="图片说明（可选）").fill("可检索图片说明")
        page.get_by_role("button", name="保存到素材库").click()
        image = page.locator("#materials-detail img")
        image.wait_for(state="visible")
        page.wait_for_function("document.querySelector('#materials-detail img').naturalWidth === 720")
        zoom = page.get_by_role("button", name="按原尺寸查看图片")
        self.assertEqual(zoom.get_attribute("aria-expanded"), "false")
        preview_width = image.bounding_box()["width"]
        self.assertLess(preview_width, 720)
        zoom.press("Enter")
        shrink = page.get_by_role("button", name="缩回图片预览")
        self.assertEqual(shrink.get_attribute("aria-expanded"), "true")
        self.assertEqual(image.bounding_box()["width"], 720)
        self.assertTrue(image.locator("../..").evaluate("pane => pane.scrollWidth > pane.clientWidth"))
        shrink.press("Space")
        self.assertEqual(zoom.get_attribute("aria-expanded"), "false")
        self.assertEqual(image.bounding_box()["width"], preview_width)
        self.assertEqual(page.locator("#materials-filter").input_value(), "")
        self.assertTrue(type_trigger.is_visible())
        type_trigger.click()
        page.get_by_role("option", name="文字").click()
        self.assertFalse(page.locator("#materials-excerpts img").is_visible())
        page.get_by_role("button", name="素材类型：文字").click()
        page.get_by_role("option", name="图片").click()
        self.assertTrue(page.locator("#materials-excerpts img").is_visible())
        self.assertTrue(page.get_by_role("button", name="导出").is_disabled())
        image_trigger = page.get_by_role("button", name="素材类型：图片")
        image_trigger.press("ArrowDown")
        page.get_by_role("option", name="图片").wait_for()
        page.keyboard.press("Escape")
        self.assertFalse(page.get_by_role("listbox", name="素材类型").is_visible())
        self.assertTrue(image_trigger.evaluate("button => document.activeElement === button"))
        self.assertIn("未整理", page.locator("#materials-detail").inner_text())

        for event_name in ("drop", "paste"):
            page.locator("#materials-layout").evaluate(
                """(target, {name, bytes}) => {
                  const transfer = new DataTransfer();
                  transfer.items.add(new File([new Uint8Array(bytes)], 'external.png', {type: 'image/png'}));
                  const event = name === 'drop' ? new DragEvent(name, {dataTransfer: transfer, bubbles: true})
                    : new ClipboardEvent(name, {clipboardData: transfer, bubbles: true});
                  target.dispatchEvent(event);
                }""",
                {"name": event_name, "bytes": list(png)},
            )
            page.locator(".image-workbench-image").wait_for(state="visible")
            self.assertTrue(page.get_by_role("button", name="裁剪", exact=True).is_hidden())
            page.get_by_role("button", name="移除", exact=True).click()
            page.get_by_role("button", name="取消", exact=True).click()
        page.get_by_role("button", name="新建标签").click()
        tag_dialog = page.get_by_role("dialog", name="新建标签")
        tag_dialog.wait_for()
        tag_bounds = tag_dialog.bounding_box()
        viewport = page.viewport_size
        self.assertAlmostEqual(tag_bounds["x"] + tag_bounds["width"] / 2, viewport["width"] / 2, delta=1)
        self.assertAlmostEqual(tag_bounds["y"] + tag_bounds["height"] / 2, viewport["height"] / 2, delta=1)
        tag_dialog.get_by_role("button", name="取消").click()

        for width in (1000, 535, 375, 320):
            page.set_viewport_size({"width": width, "height": 700})
            page.locator("#materials-add-image").click()
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            choose_image = page.get_by_role("button", name="选择图片", exact=True)
            self.assertEqual(choose_image.bounding_box()["height"], 36)
            page.get_by_role("tab", name="从 PDF 截取").click()
            choose_pdf = page.get_by_role("button", name="选择 PDF", exact=True)
            self.assertEqual(choose_pdf.bounding_box()["height"], 36)
            choose_pdf.click()
            crop_dialog = page.get_by_role("dialog", name="从 PDF 截图")
            crop_dialog.wait_for()
            crop_bounds = crop_dialog.bounding_box()
            self.assertAlmostEqual(crop_bounds["x"] + crop_bounds["width"] / 2, width / 2, delta=1)
            self.assertAlmostEqual(crop_bounds["y"] + crop_bounds["height"] / 2, 350, delta=1)
            crop_dialog.get_by_role("button", name="PDF 文献", exact=False).click()
            pdf_list = crop_dialog.get_by_role("listbox", name="PDF 文献")
            pdf_list.wait_for()
            options_bounds = pdf_list.bounding_box()
            self.assertGreaterEqual(options_bounds["x"], 0)
            self.assertLessEqual(options_bounds["x"] + options_bounds["width"], width)
            page.keyboard.press("Escape")
            self.assertFalse(pdf_list.is_visible())
            self.assertTrue(crop_dialog.is_visible())
            crop_dialog.get_by_role("button", name="取消", exact=True).click()
            page.get_by_role("button", name="取消", exact=True).click()

        page.locator("#materials-add-image").click()
        page.get_by_role("tab", name="从 PDF 截取").click()
        page.get_by_role("button", name="选择 PDF", exact=True).click()
        trigger = page.get_by_role("button", name=re.compile("^PDF 文献："))
        trigger.wait_for()
        trigger.press("ArrowDown")
        alternative = page.locator('#materials-pdf-options [data-value="pdf-zh"]')
        alternative.click()
        page.get_by_role("spinbutton", name="PDF 页码").fill("2")
        trigger.click()
        page.wait_for_function("document.activeElement?.matches('#materials-pdf-options .is-selected')")
        page.keyboard.press("p")
        target = page.locator('#materials-pdf-options [data-value="pdf-de"]')
        self.assertTrue(target.evaluate("element => document.activeElement === element"))
        page.keyboard.press("Enter")
        self.assertEqual(page.get_by_role("spinbutton", name="PDF 页码").input_value(), "1")
        self.assertTrue(trigger.evaluate("element => document.activeElement === element"))
        page.get_by_role("button", name="预览", exact=True).click()
        canvas = page.locator(".materials-crop-canvas")
        canvas.wait_for(state="visible")
        bounds = canvas.bounding_box()
        page.mouse.move(bounds["x"] + bounds["width"] * .10, bounds["y"] + bounds["height"] * .10)
        page.mouse.down()
        page.mouse.move(bounds["x"] + bounds["width"] * .40, bounds["y"] + bounds["height"] * .70)
        page.mouse.up()
        dialog = page.get_by_role("dialog", name="从 PDF 截图")
        selection = dialog.locator(".materials-crop-selection")
        original = selection.bounding_box()
        handle = dialog.get_by_role("button", name="调整选区右下角").bounding_box()
        x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 35, y - 20, steps=4)
        page.mouse.up()
        resized = selection.bounding_box()
        self.assertAlmostEqual(resized["width"] - original["width"], 35, delta=1)
        self.assertAlmostEqual(resized["height"] - original["height"], -20, delta=1)
        dialog.get_by_role("button", name="使用选区").click()
        image = page.locator(".image-workbench-image")
        selected_url = image.get_attribute("src")
        note = page.get_by_role("textbox", name="图片说明（可选）")
        note.fill("保留说明")
        page.get_by_role("button", name="裁剪", exact=True).click()
        dialog.get_by_role("status").get_by_text("PDF 第 1 / 2 页", exact=False).wait_for()
        self.assertEqual(selection.bounding_box(), resized)
        dialog.get_by_role("button", name="清除选区").click()
        dialog.get_by_role("button", name="取消").click()
        self.assertEqual(image.get_attribute("src"), selected_url)
        self.assertEqual(note.input_value(), "保留说明")
        page.get_by_role("button", name="保存到素材库").click()
        page.get_by_role("button", name="打开原文").wait_for()
        page.wait_for_function("document.querySelector('#materials-detail img')?.naturalWidth > 100")

    def test_material_pdf_preview_can_use_the_whole_page_without_dragging(self) -> None:
        page = self.page
        page.locator("#materials-nav").click()
        page.locator("#materials-add-image").click()
        page.get_by_role("tab", name="从 PDF 截取").click()
        page.get_by_role("button", name="选择 PDF", exact=True).click()
        dialog = page.get_by_role("dialog", name="从 PDF 截图")
        use = dialog.get_by_role("button", name="使用整页", exact=True)
        self.assertTrue(use.is_disabled())
        dialog.get_by_role("button", name=re.compile("^PDF 文献：")).click()
        dialog.locator('#materials-pdf-options [data-value="pdf-de"]').click()
        preview = dialog.get_by_role("button", name="预览", exact=True)
        preview.click()
        canvas = dialog.locator("canvas")
        canvas.wait_for(state="visible")
        first_width = canvas.evaluate("canvas => canvas.width")
        self.assertTrue(use.is_enabled(), "预览后即使没有框选也能使用整页")

        previous = dialog.get_by_role("button", name="上一页")
        next_page = dialog.get_by_role("button", name="下一页")
        page_number = dialog.get_by_role("spinbutton", name="PDF 页码")
        self.assertTrue(previous.is_disabled())
        held = []

        def hold_preview(route: Route) -> None:
            if route.request.method == "POST" and route.request.post_data_json.get("action") == "preview_pdf":
                held.append(route)
            else:
                route.continue_()

        footer = dialog.locator(".materials-form-actions")
        geometry = (dialog.bounding_box(), footer.bounding_box())
        page.route("**/api/materials", hold_preview)
        try:
            next_page.click()
            dialog.get_by_role("status").get_by_text("正在预览第 2 页", exact=False).wait_for()
            self.assertEqual((dialog.bounding_box(), footer.bounding_box()), geometry)
            self.assertTrue(canvas.is_visible())
            self.assertTrue(use.is_disabled())
        finally:
            for route in held:
                route.continue_()
            page.unroute("**/api/materials", hold_preview)
        dialog.get_by_role("status").get_by_text("PDF 第 2 / 2 页", exact=False).wait_for()
        self.assertEqual((dialog.bounding_box(), footer.bounding_box()), geometry)
        self.assertEqual(page_number.input_value(), "2")
        self.assertLess(canvas.evaluate("canvas => canvas.width"), first_width)
        self.assertTrue(next_page.is_disabled())
        previous.click()
        dialog.get_by_role("status").get_by_text("PDF 第 1 / 2 页", exact=False).wait_for()
        self.assertEqual(canvas.evaluate("canvas => canvas.width"), first_width)
        page_number.press("ArrowUp")
        dialog.get_by_role("status").get_by_text("PDF 第 2 / 2 页", exact=False).wait_for()
        use.click()
        dialog.wait_for(state="hidden")
        page.locator(".image-workbench-image").wait_for(state="visible")
        self.assertTrue(page.get_by_role("button", name="裁剪", exact=True).is_visible())
        page.get_by_role("button", name="保存到素材库").click()
        page.get_by_role("button", name="打开原文").wait_for()
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            origin = json.loads(connection.execute("SELECT origin FROM material_images").fetchone()[0])
        self.assertEqual(origin["box"], [0, 0, 1, 1])
        self.assertEqual(origin["page"], 2)

    def test_image_import_shows_existing_tags_and_keeps_draft_on_save_failure(self) -> None:
        page = self.page
        for path in ("理论/异化", "理论/社会学"):
            response = page.request.post(self.base_url + "/api/materials", data={"action": "create_tag", "path": path})
            self.assertTrue(response.ok)
        page.locator("#materials-nav").click()
        page.locator("#materials-add-image").click()
        editor = page.locator("#materials-image-import")
        tag = editor.get_by_role("button", name="理论 / 异化", exact=True)
        search = editor.get_by_role("searchbox", name="搜索或新建标签")
        tag.wait_for(timeout=4000)
        self.assertTrue(search.is_visible())
        self.assertEqual(editor.get_by_text("归类", exact=True).count(), 0)
        tag.click()
        self.assertEqual(tag.get_attribute("aria-pressed"), "true")
        tag.click()
        self.assertEqual(tag.get_attribute("aria-pressed"), "false")
        tag.click()
        search.fill("调研/记录")
        search.press("Enter")
        new_tag = editor.get_by_role("button", name="调研 / 记录", exact=True)
        self.assertEqual(new_tag.get_attribute("aria-pressed"), "true")

        with fitz.open() as pdf:
            png = pdf.new_page(width=260, height=190).get_pixmap().tobytes("png")
        editor.locator("input[type=file]").set_input_files(
            {"name": "observation.png", "mimeType": "image/png", "buffer": png}
        )
        image = editor.locator(".image-workbench-image")
        image.wait_for(state="visible")
        before = image.get_attribute("src")
        note = editor.get_by_role("textbox", name="图片说明（可选）")
        note.fill("观察记录")
        editor.get_by_role("tab", name="本地图片").press("ArrowRight")
        self.assertEqual(editor.get_by_role("tab", name="从 PDF 截取").get_attribute("aria-selected"), "true")
        self.assertTrue(editor.get_by_role("button", name="保存到素材库").is_disabled())
        editor.get_by_role("tab", name="本地图片").click()
        self.assertEqual(image.get_attribute("src"), before)
        self.assertEqual(note.input_value(), "观察记录")
        self.assertEqual(tag.get_attribute("aria-pressed"), "true")

        def fail_save(route: Route) -> None:
            if route.request.method == "POST" and route.request.post_data_json.get("action") == "collect_image":
                route.fulfill(status=500, json={"error": "暂时无法写入"})
            else:
                route.continue_()

        save = editor.get_by_role("button", name="保存到素材库")
        page.route("**/api/materials", fail_save)
        try:
            save.click()
            editor.get_by_role("status").get_by_text("保存失败 · 暂时无法写入").wait_for()
        finally:
            page.unroute("**/api/materials", fail_save)
        self.assertEqual(image.get_attribute("src"), before)
        self.assertEqual(note.input_value(), "观察记录")
        save.click()
        editor.wait_for(state="hidden")
        page.locator("#materials-detail img").wait_for(state="visible")
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            rows = connection.execute("SELECT path FROM material_image_tags ORDER BY path").fetchall()
        self.assertEqual(rows, [("理论/异化",), ("调研/记录",)])

    def test_every_main_view_keeps_hidden_elements_off_screen(self) -> None:
        for label in ("文献检索", "文献库", "译本对照", "文献导入", "设置"):
            with self.subTest(view=label):
                self.navigate(label)
                self.page.wait_for_load_state("networkidle")
                self.assert_hidden_really_hidden(label)

    def test_switching_alignment_model_keeps_exactly_one_choice_and_persists(self) -> None:
        self.navigate("设置")
        radios = self.model_radios()
        radios.first.wait_for()
        self.wait_until(lambda: self.checked_models() == ["minilm-l12-v2"], "default model not selected")

        self.page.locator('input[name="alignment-embedding-model"][value="multilingual-e5-large"]').check()
        self.wait_until(lambda: self.saved_model() == "multilingual-e5-large", "model choice not saved")
        self.wait_until(lambda: not radios.first.is_disabled(), "radios stay disabled after saving")
        self.assertEqual(self.checked_models(), ["multilingual-e5-large"])
        self.assert_hidden_really_hidden("after model switch")

        # A rejected save must restore the previous choice, never leave both unchecked.
        def reject(route: Route) -> None:
            if route.request.method == "POST":
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"error": "模拟保存失败"}))
            else:
                route.continue_()

        self.page.route("**/api/preferences", reject)
        self.page.locator('input[name="alignment-embedding-model"][value="minilm-l12-v2"]').check()
        self.page.get_by_text("译本对齐模型保存失败").first.wait_for()
        self.wait_until(lambda: self.checked_models() == ["multilingual-e5-large"],
                        f"rejected save left {self.checked_models()} checked")
        self.page.unroute("**/api/preferences")
        self.assertEqual(self.saved_model(), "multilingual-e5-large")

    def test_model_pick_refused_while_preferences_load_keeps_the_stored_choice(self) -> None:
        # The Vue pilot defect: the controller refuses the pick without touching
        # the store, Vue sees no change, but the browser already unchecked the
        # old radio — leaving no model selected.  (docs/issues/vue-pilot-alignment-models.md)
        held: List[Route] = []

        def hold_reads(route: Route) -> None:
            if route.request.method == "GET":
                held.append(route)
            else:
                route.continue_()

        # Preferences load once at start-up; hold that read so the pick lands
        # while ``preferencesLoadPromise`` is pending.
        def release() -> None:
            while held:
                route = held.pop()
                try:
                    route.continue_()
                except Exception:  # the page may have dropped the request already
                    pass
            self.page.unroute("**/api/preferences")

        self.page.route("**/api/preferences", hold_reads)
        self.addCleanup(release)
        self.page.reload(wait_until="domcontentloaded")
        self.wait_until(lambda: bool(held), "start-up did not request preferences")
        self.navigate("设置")
        self.model_radios().first.wait_for()
        self.assertTrue(self.page.evaluate("() => !!settingsStore.preferencesLoadPromise"))
        self.wait_until(lambda: self.checked_models() == ["minilm-l12-v2"], "default model not selected")

        self.page.locator('input[name="alignment-embedding-model"][value="multilingual-e5-large"]').click()
        self.page.wait_for_timeout(300)
        self.assertEqual(self.checked_models(), ["minilm-l12-v2"])

        release()
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(self.saved_model(), "minilm-l12-v2")
        self.assertEqual(self.checked_models(), ["minilm-l12-v2"])

    def fake_alignment_jobs(self) -> dict:
        """Report the default model as installed and fake the job endpoints.

        Returns the controller dict: set ``done`` to finish the current job;
        ``started`` counts start requests.  Everything else hits the real backend.
        """

        job = {"done": False, "started": 0}

        def ready_models(route: Route) -> None:
            if route.request.method != "GET":
                route.continue_()
                return
            payload = route.fetch().json()
            for model in payload["models"]:
                if model["id"] == "minilm-l12-v2":
                    model.update(installed=True, state="installed")
            payload["compute"] = {"available": True, "provider": "builtin", "detail": ""}
            route.fulfill(json=payload)

        def start(route: Route) -> None:
            job["started"] += 1
            job["done"] = False
            route.fulfill(json={"ok": True, "job_id": f"browser-job-{job['started']}"})

        def status(route: Route) -> None:
            if job["done"]:
                route.fulfill(json={"ok": True})
            else:
                route.fulfill(status=202, json={"ok": True, "running": True, "progress": job.get("progress")})

        self.page.route("**/api/text-alignment/models", ready_models)
        self.page.route("**/api/text-alignments/start", start)
        self.page.route("**/api/text-alignments/status*", status)
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        self.navigate("译本对照")
        return job

    def test_alignment_progress_updates_and_clears_without_replacing_cancel(self) -> None:
        job = self.fake_alignment_jobs()
        line = self.pair_line(r"^English EPUB.*德文")
        line.get_by_role("button", name="生成对齐").click()
        cancel = line.get_by_role("button", name="取消")
        cancel.wait_for()
        cancel.focus()
        job["progress"] = {"stage": "embedding", "percent": 42, "eta_seconds": 120}
        line.get_by_text("文本计算 42%", exact=False).wait_for()
        self.assertIn("本阶段预计剩余约 2 分钟", line.inner_text())
        self.assertEqual(line.locator("progress").get_attribute("value"), "42")
        self.assertTrue(cancel.evaluate("node => node === document.activeElement"))
        job["progress"] = {"stage": "matching", "percent": 12, "eta_seconds": None}
        line.get_by_text("段落匹配 12%", exact=False).wait_for()
        self.assertIn("正在估算", line.inner_text())
        job["progress"] = {"stage": "saving", "percent": None, "eta_seconds": None}
        line.get_by_text("保存结果", exact=False).wait_for()
        self.assertEqual(line.locator("progress").count(), 0)
        job["done"] = True
        cancel.wait_for(state="detached")

    def test_running_alignment_disables_other_generate_actions_until_it_ends(self) -> None:
        job = self.fake_alignment_jobs()

        english_german = self.pair_line(r"^English EPUB.*德文")
        chinese_english = self.pair_line(r"^贺麟译本.*English EPUB")
        english_german.get_by_role("button", name="生成对齐").wait_for()
        self.assertTrue(english_german.get_by_role("button", name="生成对齐").is_enabled())

        english_german.get_by_role("button", name="生成对齐").click()
        english_german.get_by_role("button", name="取消").wait_for()
        self.assertTrue(chinese_english.get_by_role("button", name="生成对齐").is_disabled())
        for line in (english_german, chinese_english):
            buttons = line.get_by_role("button", name="正文范围")
            if buttons.count():
                self.assertTrue(buttons.first.is_disabled())
        self.assert_hidden_really_hidden("alignment running")

        job["done"] = True
        english_german.get_by_role("button", name="取消").wait_for(state="detached")
        self.wait_until(lambda: chinese_english.get_by_role("button", name="生成对齐").is_enabled(),
                        "generate stays disabled after the job ended")
        self.assertTrue(chinese_english.get_by_role("button", name="正文范围").is_enabled())
        self.assert_hidden_really_hidden("alignment finished")

    def test_batch_realign_runs_stale_pairs_one_by_one_and_reports_once(self) -> None:
        # Alignments computed with another model are stale for the active one.
        for target in ("pdf-zh", "epub-en"):
            generate_alignment(self.fixture.db, "work-one", "pdf-de", target,
                               embedding_model_id="multilingual-e5-large")
        job = self.fake_alignment_jobs()
        notice = self.page.locator(".tw-notice")
        notice.filter(has_text="2 组对齐需重新生成").wait_for()

        notice.get_by_role("button", name="全部重新对齐").click()
        self.page.get_by_role("button", name="开始重新对齐").click()
        notice.filter(has_text="正在重新对齐 1/2").wait_for()
        self.assertEqual(job["started"], 1)
        # While the queue runs, nothing else may start another job.
        self.assertEqual(self.page.get_by_role("button", name="生成对齐").count(),
                         self.page.locator("button:disabled", has_text="生成对齐").count())
        self.assertEqual(self.page.get_by_role("button", name=re.compile(r"^重新对齐 \d+ 组$")).count(), 0)
        self.assert_hidden_really_hidden("batch realign: first job")

        job["done"] = True
        notice.filter(has_text="正在重新对齐 2/2").wait_for()
        self.assertEqual(job["started"], 2)

        job["done"] = True
        self.page.get_by_text("已重新对齐 2 组").first.wait_for()
        notice.filter(has_text="正在重新对齐").wait_for(state="detached")
        self.assertEqual(job["started"], 2)
        self.assert_hidden_really_hidden("batch realign: finished")

    def test_reader_switching_books_shows_only_the_new_book(self) -> None:
        self.give_books_distinct_pages()
        german_pages = self.stored_page_labels("pdf-de")
        english_pages = self.stored_page_labels("epub-en")
        chinese_pages = self.stored_page_labels("pdf-zh")
        # Fixture sanity: each book's page wording differs from the others'.
        self.assertEqual(german_pages, ["引用页码：38"])
        self.assertEqual(english_pages, ["第 27 页", "第 28 页"])
        self.assertTrue(set(chinese_pages).isdisjoint(german_pages + english_pages))

        self.navigate("译本对照")
        versions = self.page.locator(".tw-btn.link", has_text="阅读")
        versions.first.wait_for()
        versions.first.click()  # German base version
        self.page.wait_for_url(re.compile(r"/reader\?source=pdf-de"))
        self.wait_until(lambda: any("Der Geist" in text for text in self.reader_texts()), "German text missing")
        self.assertEqual(self.page.locator("#mef-reader-title").inner_text(), "Phänomenologie des Geistes")
        self.assertEqual(self.reader_page_labels(), german_pages)
        self.assertEqual(self.page.locator(".sidebar").evaluate("node => node.offsetWidth"), 64)
        self.assert_hidden_really_hidden("reader: German")

        self.page.get_by_role("button", name="左栏版本").click()
        self.page.locator('[data-reader-target="epub-en"]').click()
        self.page.wait_for_url(re.compile(r"/reader\?source=epub-en"))
        self.wait_until(
            lambda: (texts := self.reader_texts()) != []
            and all("Der Geist" not in text for text in texts)
            and any("Spirit is actual" in text for text in texts),
            f"reader still shows stale text: {self.reader_texts()}",
        )
        self.assertEqual(self.page.locator("#mef-reader-title").inner_text(), "Phenomenology of Spirit")
        self.assertIn("English EPUB", self.page.get_by_role("button", name="左栏版本").inner_text())
        self.assertEqual(self.reader_page_labels(), english_pages)
        self.assert_hidden_really_hidden("reader: English after switch")

        self.page.locator(".mef-reader-back").click()
        self.page.wait_for_url(re.compile(r"/$"))
        self.page.locator(".tw-work-title").wait_for()
        self.assertEqual(self.page.locator(".mef-reader-source-pane:visible").count(), 0)
        self.assertGreater(self.page.locator(".sidebar").evaluate("node => node.offsetWidth"), 64)

        versions.nth(1).click()  # Chinese translation, reopened from the works page
        self.page.wait_for_url(re.compile(r"/reader\?source=pdf-zh"))
        self.wait_until(
            lambda: (texts := self.reader_texts()) != []
            and all("Spirit is actual" not in text for text in texts)
            and all("Der Geist" not in text for text in texts),
            f"reopened reader shows another book: {self.reader_texts()}",
        )
        self.assertEqual(self.page.locator("#mef-reader-title").inner_text(), "精神现象学")
        self.assertEqual(self.reader_page_labels(), chinese_pages)
        self.assert_hidden_really_hidden("reader: reopened Chinese")


if __name__ == "__main__":
    unittest.main()
