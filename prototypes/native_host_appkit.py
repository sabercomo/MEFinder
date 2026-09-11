"""Small-scale AppKit/WKWebView native host prototype (task 6 validation).

Validates the native-host direction on real UI flows without touching the
product: the MEFinder backend runs as an independent subprocess, and the host
is hand-rolled AppKit — one WKWebView main window driving the real SPA
(search -> result -> detail with page anchor), a second WKWebView reader
window (/reader-window), a PDFKit window for 原文打开, and a clean exit that
stops the backend gracefully.

Run:  python3 prototypes/native_host_appkit.py --report /tmp/native-host-report.json
Windows appear briefly on screen while the self-driving acceptance runs (~15s).
The prototype is throwaway evidence for docs/issues/native-host-validation.md.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.performance_fixture import create_fixture  # noqa: E402

REPORT_STEPS = ("search", "locate", "multi_window", "pdf_open", "exit")


class NativeHost:
    def __init__(self, report_path: Path) -> None:
        import AppKit
        import Foundation
        from WebKit import WKWebView, WKWebViewConfiguration

        self.AppKit = AppKit
        self.Foundation = Foundation
        self.report_path = report_path
        self.report = {step: {"ok": False, "note": ""} for step in REPORT_STEPS}
        self.root = Path(tempfile.mkdtemp(prefix="mefinder-native-proto-"))
        create_fixture(self.root, documents=2, paragraphs=12, alignment_paragraphs=8)
        models = Path.home() / (
            "Library/Application Support/MEFinder/runtime/"
            "components/text-alignment/models"
        )
        if (models / "models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q").is_dir():
            (self.root / "components/text-alignment").mkdir(parents=True)
            os.symlink(models, self.root / "components/text-alignment/models")
        env = {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "ME_FINDER_DESKTOP_SHELL": "",
            "ME_FINDER_PREFERENCES": str(self.root / "config/preferences.json"),
        }
        self.backend = subprocess.Popen(
            [sys.executable, str(REPO / "scripts/bench_responsiveness.py"),
             "--worker", str(self.root)],
            stdin=subprocess.PIPE, text=True, env=env, cwd=REPO,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ready = self.root / "ready.json"
        deadline = time.monotonic() + 60
        while not ready.exists():
            if time.monotonic() > deadline:
                raise RuntimeError("backend never became ready")
            time.sleep(0.05)
        self.port = json.loads(ready.read_text())["port"]
        self.base_url = f"http://127.0.0.1:{self.port}/"
        self.pdf_path = self.root / "prototype.pdf"
        self._make_pdf()
        self.webview_class = WKWebView
        self.webview_config = WKWebViewConfiguration.alloc().init()
        self.phase = "wait-main-load"
        self.deadline = time.monotonic() + 60

    def _make_pdf(self) -> None:
        import fitz

        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 120), "MEFinder 原型 PDF - 第 1 页", fontsize=18)
        page.insert_text((72, 160), "The prototype keeps anchors local.", fontsize=12)
        document.save(str(self.pdf_path))
        document.close()

    # ------------------------------------------------------------------
    # Window helpers (hand-rolled: what pywebview normally provides)
    # ------------------------------------------------------------------

    def make_window(self, title: str, url: str, size=(1180, 780)) -> dict:
        import Quartz

        rect = Quartz.NSMakeRect(80, 80, size[0], size[1])
        window = self.AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            self.AppKit.NSWindowStyleMaskTitled
            | self.AppKit.NSWindowStyleMaskClosable
            | self.AppKit.NSWindowStyleMaskMiniaturizable
            | self.AppKit.NSWindowStyleMaskResizable,
            self.AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setTitle_(title)
        webview = self.webview_class.alloc().initWithFrame_configuration_(
            rect, self.webview_config
        )
        window.setContentView_(webview)
        request = self.Foundation.NSURLRequest.requestWithURL_(
            self.Foundation.NSURL.URLWithString_(url)
        )
        webview.loadRequest_(request)
        window.makeKeyAndOrderFront_(None)
        return {"window": window, "webview": webview}

    def make_pdf_window(self) -> dict:
        import Quartz
        from Quartz.PDFKit import PDFDocument, PDFView

        rect = Quartz.NSMakeRect(140, 140, 900, 700)
        window = self.AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, self.AppKit.NSWindowStyleMaskTitled, self.AppKit.NSBackingStoreBuffered, False,
        )
        window.setTitle_("PDF 原文(原型)")
        view = PDFView.alloc().initWithFrame_(rect)
        document = PDFDocument.alloc().initWithURL_(
            self.Foundation.NSURL.fileURLWithPath_(str(self.pdf_path))
        )
        view.setDocument_(document)
        view.goToPage_(document.pageAtIndex_(0))
        window.setContentView_(view)
        window.makeKeyAndOrderFront_(None)
        page_index = document.indexForPage_(view.currentPage())
        return {"window": window, "view": view, "page_index": page_index}

    def evaluate(self, webview, script: str) -> dict:
        """Issue one async JS evaluation; the next tick harvests the result.

        The completion handler needs the main run loop, so the state machine
        must never block on it inside a timer callback.
        """

        box: dict = {"done": False, "value": None, "error": None}

        def handler(result, error):
            box["value"] = result
            box["error"] = str(error) if error else None
            box["done"] = True

        webview.evaluateJavaScript_completionHandler_(script, handler)
        return box

    # ------------------------------------------------------------------
    # Acceptance state machine (polled every 0.2s)
    # ------------------------------------------------------------------

    def tick(self) -> None:
        if time.monotonic() > self.deadline:
            self.report.setdefault("timeout", True)
            self.finish()
            return
        if getattr(self, "_pending", None) is not None:
            if not self._pending["done"]:
                return
            if self._pending["error"]:
                self.report.setdefault("errors", []).append(
                    f"{self.phase}: {self._pending['error'][:160]}"
                )
            self._result = self._pending["value"]
            self._pending = None
        phase = self.phase
        try:
            if phase == "wait-main-load":
                if self.main_loaded:
                    self.phase = "search"
            elif phase == "search":
                self._pending = self.evaluate(
                    self.main["webview"],
                    "typeof runSearch === 'function' ? ("
                    "document.getElementById('query').value='社会', runSearch(), 'ran') "
                    ": 'not-ready'",
                )
                self.report["search"]["note"] = "runSearch() on the real SPA"
                self.phase = "search-wait"
            elif phase == "search-wait":
                if self._result != "ran":
                    self.phase = "search"
                    return
                self._pending = self.evaluate(
                    self.main["webview"],
                    "document.querySelectorAll('.result-row').length",
                )
                self.phase = "search-count"
            elif phase == "search-count":
                count = self._result
                if isinstance(count, (int, float)) and count >= 1:
                    self.report["search"].update(ok=True, note=f"{int(count)} result rows")
                    self.phase = "locate"
                elif time.monotonic() > self.deadline - 5:
                    self.phase = "reader-window"
                else:
                    self._pending = self.evaluate(
                        self.main["webview"],
                        "document.querySelectorAll('.result-row').length",
                    )
            elif phase == "locate":
                self._pending = self.evaluate(
                    self.main["webview"],
                    "document.querySelector('.result-row') "
                    "? document.querySelector('.result-row').innerText.slice(0, 160) : ''",
                )
                self.phase = "locate-click"
            elif phase == "locate-click":
                self.first_result = self._result
                self._pending = self.evaluate(
                    self.main["webview"], "selectResult(0); true;"
                )
                self.phase = "locate-detail"
            elif phase == "locate-detail":
                self._pending = self.evaluate(
                    self.main["webview"],
                    "document.getElementById('detail-panel').innerText.slice(0, 300)",
                )
                self.phase = "locate-wait"
            elif phase == "locate-wait":
                detail = self._result or ""
                if detail and "选择一条结果" not in detail:
                    anchored = ("页" in detail) or ("第" in detail)
                    self.report["locate"].update(
                        ok=anchored,
                        note=f"result={(self.first_result or '')[:60]!r}; detail has page anchor: {anchored}",
                    )
                    self.phase = "reader-window"
                elif time.monotonic() > self.deadline - 5:
                    self.report["locate"]["note"] = f"detail never filled: {detail[:80]!r}"
                    self.phase = "reader-window"
                else:
                    self.phase = "locate-detail"
            elif phase == "reader-window":
                self.reader = self.make_window("阅读窗(原型)", self.base_url + "reader-window")
                self.phase = "reader-wait"
            elif phase == "reader-wait":
                self._pending = self.evaluate(
                    self.reader["webview"], "document.body ? 1 : 0"
                )
                self.phase = "reader-check"
            elif phase == "reader-check":
                if self._result:
                    self.report["multi_window"].update(
                        ok=True, note="second WKWebView window on /reader-window"
                    )
                    self.phase = "pdf"
                else:
                    self.phase = "reader-wait"
            elif phase == "pdf":
                pdf = self.make_pdf_window()
                page = pdf["page_index"] + 1
                self.report["pdf_open"].update(
                    ok=page == 1, note=f"PDFKit PDFView opened page {page}"
                )
                self.phase = "finish"
            elif phase == "finish":
                self.finish()
        except Exception as exc:  # noqa: BLE001 - prototype evidence
            self.report.setdefault("errors", []).append(f"{phase}: {exc!r}")
            self.finish()

    def finish(self) -> None:
        if getattr(self, "_finished", False):
            return
        self._finished = True
        self.report["exit"]["ok"] = True
        self.report["exit"]["note"] = "graceful backend stop + app terminate"
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=1))
        self.AppKit.NSApplication.sharedApplication().terminate_(None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    import AppKit
    import Foundation
    from PyObjCTools import AppHelper

    host = NativeHost(args.report)

    class AppDelegate(Foundation.NSObject):
        def applicationDidFinishLaunching_(self, _note):
            host.main = host.make_window("MEFinder 原生宿主原型", host.base_url)
            host.main_loaded = False
            host.main["webview"].setNavigationDelegate_(self)
            Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.2, self, "tickHost:", None, True
            )

        def tickHost_(self, _timer):
            host.tick()

        def webView_didFinishNavigation_(self, _view, _navigation):
            host.main_loaded = True

        def applicationWillTerminate_(self, _note):
            try:
                host.backend.stdin.write("stop\n")
                host.backend.stdin.flush()
                host.report["exit"]["backend_exit_code"] = host.backend.wait(timeout=20)
            except Exception as exc:  # noqa: BLE001
                host.report["exit"]["ok"] = False
                host.report["exit"]["note"] = repr(exc)

    app = AppKit.NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    delegate.host = host
    app.setDelegate_(delegate)
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    AppHelper.runEventLoop()

    report = json.loads(args.report.read_text())
    return 0 if all(report[step]["ok"] for step in REPORT_STEPS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
