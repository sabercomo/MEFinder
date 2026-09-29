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
import threading
import unittest
from typing import Callable, List
from unittest import mock

from src.me_finder.app_context import AppContext
from src.me_finder.text_alignment import generate_alignment
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

    def reader_texts(self) -> List[str]:
        return self.page.eval_on_selector_all(
            ".mef-reader-source-pane .mef-reader-item-text",
            "nodes => nodes.filter(node => node.getClientRects().length).map(node => node.textContent.trim())",
        )

    # ── scenarios ──────────────────────────────────────────────────
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
                route.fulfill(status=202, json={"ok": True, "running": True})

        self.page.route("**/api/text-alignment/models", ready_models)
        self.page.route("**/api/text-alignments/start", start)
        self.page.route("**/api/text-alignments/status*", status)
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        self.navigate("译本对照")
        return job

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
        self.navigate("译本对照")
        versions = self.page.locator(".tw-btn.link", has_text="阅读")
        versions.first.wait_for()
        versions.first.click()  # German base version
        self.page.wait_for_url(re.compile(r"/reader\?source=pdf-de"))
        self.wait_until(lambda: any("Der Geist" in text for text in self.reader_texts()), "German text missing")
        self.assertEqual(self.page.locator("#mef-reader-title").inner_text(), "Phänomenologie des Geistes")
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
        self.assert_hidden_really_hidden("reader: English after switch")

        self.page.locator(".mef-reader-back").click()
        self.page.wait_for_url(re.compile(r"/$"))
        self.page.locator(".tw-work-title").wait_for()
        self.assertEqual(self.page.locator(".mef-reader-source-pane:visible").count(), 0)

        versions.nth(1).click()  # Chinese translation, reopened from the works page
        self.page.wait_for_url(re.compile(r"/reader\?source=pdf-zh"))
        self.wait_until(
            lambda: (texts := self.reader_texts()) != []
            and all("Spirit is actual" not in text for text in texts)
            and all("Der Geist" not in text for text in texts),
            f"reopened reader shows another book: {self.reader_texts()}",
        )
        self.assertEqual(self.page.locator("#mef-reader-title").inner_text(), "精神现象学")
        self.assert_hidden_really_hidden("reader: reopened Chinese")


if __name__ == "__main__":
    unittest.main()
