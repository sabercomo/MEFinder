"""隐藏运行的 AppKit/WKWebView 验收原型，不替代产品宿主。

父进程确认宿主实际退出后才发布报告。子进程使用临时合成库，验证真实
搜索锚点、reader-window bootstrap、两个阅读窗独立状态、PDFKit 跳页和
后台进程退出；不显示窗口、不读取用户库或模型，不据此判定原生体验收益。

Run: .venv-macos312-arm64/bin/python prototypes/native_host_appkit.py --report /tmp/native-host.json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.performance_fixture import create_fixture  # noqa: E402

REPORT_STEPS = ("search", "locate", "multi_window", "pdf_open", "exit")


def check_reader(payload: dict, options: dict, quote: str) -> None:
    """Reject an empty shell, wrong source/page, or absent match highlighting."""
    state = payload['state']
    assert payload['ready'] and state['open'], payload
    assert state['sourceId'] == options['sourceId'], payload
    assert state['currentIndex'] == options['targetIndex'], payload
    assert options['anchorId'] in payload['anchors'], payload
    assert quote in ''.join(payload['marks']), payload


READER_SNAPSHOT = """JSON.stringify({
  ready:document.getElementById('reader-window-status').hidden,
  state:MEFinderReader.getState(),
  anchors:Array.from(document.querySelectorAll('[data-reader-anchor]'),x=>x.dataset.readerAnchor),
  marks:Array.from(document.querySelectorAll('.mef-reader-highlight'),x=>x.textContent)
})"""


class NativeHost:
    def __init__(self, root: Path) -> None:
        import AppKit
        import Foundation
        self.AppKit, self.Foundation = AppKit, Foundation
        self.report = {step: {"ok": False, "note": ""} for step in REPORT_STEPS}
        self.root = root
        self.windows = []
        self.backend = None
        self.pending = None
        self.pdf_request = None
        self.deadline = time.monotonic() + 80
        self.finished = False

    def start_backend(self) -> None:
        create_fixture(self.root, documents=2, paragraphs=12, alignment_paragraphs=8)
        env = {**os.environ, "ME_FINDER_DESKTOP_SHELL": "",
               "ME_FINDER_PREFERENCES": str(self.root / "config/preferences.json"),
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        self.backend = subprocess.Popen(
            [sys.executable, str(REPO / 'scripts/bench_responsiveness.py'), '--worker', str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, env=env, cwd=REPO)
        ready = self.root / 'ready.json'
        while not ready.exists():
            if self.backend.poll() is not None:
                raise RuntimeError(f'backend startup exit={self.backend.returncode}')
            if time.monotonic() > self.deadline:
                raise TimeoutError('backend readiness')
            time.sleep(.05)
        self.base_url = f"http://127.0.0.1:{json.loads(ready.read_text())['port']}/"

    def make_window(self, route: str) -> dict:
        from WebKit import WKWebView, WKWebViewConfiguration
        import Quartz
        rect = Quartz.NSMakeRect(80, 80, 1100, 760)
        appkit = self.AppKit
        window = appkit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, appkit.NSWindowStyleMaskTitled | appkit.NSWindowStyleMaskClosable |
            appkit.NSWindowStyleMaskResizable, appkit.NSBackingStoreBuffered, False)
        window.setReleasedWhenClosed_(False)
        config = WKWebViewConfiguration.alloc().init()
        config.userContentController().addScriptMessageHandler_name_(self.delegate, 'readerClose')
        config.userContentController().addScriptMessageHandler_name_(self.delegate, 'openPdf')
        webview = WKWebView.alloc().initWithFrame_configuration_(rect, config)
        window.setContentView_(webview)
        webview.loadRequest_(self.Foundation.NSURLRequest.requestWithURL_(
            self.Foundation.NSURL.URLWithString_(self.base_url + route)))
        entry = {'window': window, 'webview': webview, 'closed': False}
        self.windows.append(entry)
        return entry

    def evaluate(self, view, script):
        box = {'done': False}
        def complete(value, error):
            box.update(done=True, value=value, error=str(error) if error else None)
        view.evaluateJavaScript_completionHandler_(script, complete)
        return box

    def drive(self):
        main = self.make_window('')
        while not (yield main, "typeof runSearch === 'function'"):
            pass
        yield main, "document.getElementById('query').value='青铜指南针记录了这次独特的观察';runSearch();true"
        result = None
        while result is None:
            raw = yield main, "JSON.stringify(searchStore.results[0] || null)"
            result = json.loads(raw)
        spans = result['page_match_spans']
        assert result['source_type'] == 'pdf' and spans
        assert result['match_start'] < result['match_end']
        quote = result['match_quote']
        assert quote == '青铜指南针记录了这次独特的观察', result
        self.report['search'].update(ok=True, note='真实 SPA 搜索，核对命中文本与字符区间')
        yield main, 'selectResult(0);true'
        marked = yield main, "Array.from(document.querySelectorAll('#detail-panel .detail-hit mark'),x=>x.textContent).join('')"
        assert marked == quote, marked
        options = {'sourceId': result['source_file_id'], 'targetIndex': result['pdf_page_start_index'],
                   'anchorId': spans[0]['pdf_page_id'], 'pageMatchSpans': spans,
                   'matchStart': result['match_start'], 'matchEnd': result['match_end'],
                   'matchQuote': quote, 'matchOffsetUnit': result['match_offset_unit']}
        reader = yield from self.open_reader(options)
        payload = json.loads((yield reader, READER_SNAPSHOT))
        check_reader(payload, options, quote)
        self.report['locate'].update(ok=True, note='阅读正文、sourceId、页索引、锚点 ID 和高亮均匹配搜索结果')
        # Another document/window cannot overwrite the first window's location.
        second_options = {'sourceId': 'bench-001', 'targetIndex': 3,
                          'anchorId': 'bench-001-p000003', 'paragraphIndex': 3}
        second = yield from self.open_reader(second_options)
        second_state = json.loads((yield second, 'JSON.stringify(MEFinderReader.getState())'))
        assert second_state['sourceId'] == 'bench-001' and second_state['currentIndex'] == 3, second_state
        import Quartz
        reader['window'].setFrameOrigin_(Quartz.NSMakePoint(200, 160))
        assert reader['window'].frame().origin.x == 200
        check_reader(json.loads((yield reader, READER_SNAPSHOT)), options, quote)
        yield second, 'MEFinderReader.close();true'
        while not second['closed']:
            yield main, 'true'
        reopened = yield from self.open_reader(options)
        check_reader(json.loads((yield reopened, READER_SNAPSHOT)), options, quote)
        self.report['multi_window'].update(ok=True, note='双窗不同文献独立定位、原生移动、bridge 关闭与重开定位通过（隐藏窗口）')
        yield main, "window.webkit.messageHandlers.openPdf.postMessage({page:2});true"
        while not self.report['pdf_open']['ok']:
            yield main, 'true'

    def open_reader(self, options):
        reader = self.make_window('reader-window')
        while not (yield reader, "typeof MEFinderReader === 'object' && !!document.getElementById('reader-window-status')"):
            pass
        # Compatibility adapter for the actual reader-window.js bootstrap.
        script = """window.pywebview={api:{reader_options:()=>Promise.resolve(OPTIONS)},
          state:new Proxy({}, {set:(object,key,value)=>{
            if(key==='readerClosed' && value) window.webkit.messageHandlers.readerClose.postMessage(true);
            object[key]=value;return true;
          }})};window.dispatchEvent(new Event('pywebviewready'));true""".replace('OPTIONS', json.dumps(options))
        yield reader, script
        while not (yield reader, "document.getElementById('reader-window-status').hidden"):
            status = yield reader, "document.getElementById('reader-window-status').textContent"
            if '失败' in status:
                raise RuntimeError(status)
        return reader

    def check_pdf(self, requested_page):
        import fitz
        import Quartz
        from Quartz.PDFKit import PDFDocument, PDFView
        path = self.root / 'prototype.pdf'
        with fitz.open() as document:
            for number in range(3):
                document.new_page().insert_text((72, 120), f'Native prototype page {number + 1}')
            document.save(path)
        document = PDFDocument.alloc().initWithURL_(self.Foundation.NSURL.fileURLWithPath_(str(path)))
        view = PDFView.alloc().initWithFrame_(Quartz.NSMakeRect(0, 0, 900, 700))
        view.setDocument_(document)
        view.goToPage_(document.pageAtIndex_(requested_page - 1))
        window = self.AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Quartz.NSMakeRect(140, 140, 900, 700), self.AppKit.NSWindowStyleMaskTitled,
            self.AppKit.NSBackingStoreBuffered, False)
        window.setReleasedWhenClosed_(False)
        window.setContentView_(view)
        self.windows.append({'window': window, 'webview': None, 'closed': False})
        assert document.pageCount() == 3 and document.indexForPage_(view.currentPage()) == 1
        assert 'page 2' in view.currentPage().string()
        self.report['pdf_open'].update(ok=True, note='WK 消息请求原生 PDF 窗口打开合成三页 PDF，跳转第 2 页且核对正文')

    def tick(self):
        if self.finished:
            return
        try:
            if time.monotonic() > self.deadline:
                raise TimeoutError('native acceptance timeout')
            if self.pdf_request is not None:
                requested_page, self.pdf_request = self.pdf_request, None
                self.check_pdf(requested_page)
            if self.pending is not None and not self.pending['done']:
                return
            value = None
            if self.pending is not None:
                if self.pending['error']:
                    raise RuntimeError(self.pending['error'])
                value = self.pending['value']
            entry, script = self.flow.send(value)
            self.pending = self.evaluate(entry['webview'], script)
        except StopIteration:
            self.finished = True
        except Exception as exc:
            self.report.setdefault('errors', []).append(repr(exc))
            self.finished = True
        if self.finished:
            app = self.AppKit.NSApplication.sharedApplication()
            app.stop_(None)
            event = self.AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                self.AppKit.NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0)
            app.postEvent_atStart_(event, True)

    def shutdown(self):
        """Called after the native event loop returns, before serializing evidence."""
        for entry in self.windows:
            entry['window'].close()
        if self.backend is None:
            return
        try:
            self.backend.communicate('stop\n', timeout=20)
            code = self.backend.returncode
            self.report['exit'].update(ok=code == 0, backend_exit_code=code,
                                       note='原生事件循环已返回；后台 stop 后实际退出码')
        except subprocess.TimeoutExpired:
            self.backend.kill()
            self.backend.communicate()
            self.report['exit'].update(ok=False, note='后台超时，已清理测试进程')


def run_child(report_path: Path) -> int:
    import AppKit
    import Foundation
    with tempfile.TemporaryDirectory(prefix='mefinder-native-proto-') as temporary:
        host = NativeHost(Path(temporary))
        class Delegate(Foundation.NSObject):
            def applicationDidFinishLaunching_(self, note):
                host.flow = host.drive()
                self.timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    .1, self, 'tick:', None, True)
            def tick_(self, timer):
                host.tick()
            def userContentController_didReceiveScriptMessage_(self, controller, message):
                if message.name() == 'openPdf':
                    host.pdf_request = message.body()['page']
                    return
                for entry in host.windows:
                    if entry['webview'] == message.webView():
                        entry['closed'] = True
                        entry['window'].close()
                        return
        try:
            host.start_backend()
            app = AppKit.NSApplication.sharedApplication()
            host.delegate = Delegate.alloc().init()
            app.setDelegate_(host.delegate)
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
            app.run()
        except Exception as exc:
            host.report.setdefault('errors', []).append(repr(exc))
        finally:
            host.shutdown()
            report_path.write_text(json.dumps(host.report, ensure_ascii=False, indent=2)+'\n')
        return 0 if all(host.report[step]['ok'] for step in REPORT_STEPS) and not host.report.get('errors') else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return run_child(args.report)
    with tempfile.TemporaryDirectory(prefix='mefinder-native-evidence-') as temporary:
        child_report = Path(temporary) / 'child.json'
        process = subprocess.Popen([sys.executable, __file__, '--child', '--report', str(child_report)],
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            _, stderr = process.communicate(timeout=115)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            _, stderr = process.communicate()
        report = json.loads(child_report.read_text()) if child_report.exists() else {
            step: {'ok': False, 'note': '宿主未完成验收报告'} for step in REPORT_STEPS}
        report['exit']['host_exit_code'] = process.returncode
        report['exit']['ok'] = report['exit']['ok'] and process.returncode == 0
        if process.returncode != 0:
            report['host_stderr'] = stderr[-4000:]
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        return 0 if all(report[step]['ok'] for step in REPORT_STEPS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
