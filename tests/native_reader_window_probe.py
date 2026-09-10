"""Opt-in native reader regression, with hidden windows and a disposable database.

Run through test_reader_windows_native so the parent enforces a process deadline.
--legacy-close recreates the original reply-after-destroy defect for diagnosis.
"""

import faulthandler
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import traceback
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webview  # noqa: E402

from desktop import create_main_window  # noqa: E402
from src.me_finder.app_context import AppContext  # noqa: E402
from src.me_finder.preferences import save_preferences  # noqa: E402
from src.me_finder.reader_windows import ReaderWindows  # noqa: E402
from src.me_finder.text_alignment import generate_alignment  # noqa: E402
from src.me_finder.web import ManagedThreadingHTTPServer, make_handler  # noqa: E402
from tests.test_text_alignment import TextAlignmentTests  # noqa: E402


def wait_until(predicate, description, timeout=10):
    """Bound asynchronous GUI assertions without relying on a fixed load delay."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(description)


def main():
    """Exercise the real frontend, native bridge and production backend cleanup."""
    faulthandler.enable()
    faulthandler.dump_traceback_later(30)
    fixture = TextAlignmentTests()
    fixture.setUp()
    root = fixture.db.parent
    preferences = root / "config/preferences.json"
    preferences.parent.mkdir()
    os.environ["ME_FINDER_DESKTOP_SHELL"] = "macos" if sys.platform == "darwin" else "win32"
    os.environ["ME_FINDER_PREFERENCES"] = str(preferences)
    with sqlite3.connect(fixture.db) as connection:
        for rowid, raw in connection.execute("SELECT rowid,payload_json FROM paragraphs").fetchall():
            payload = json.loads(raw)
            payload.update(volume_number=1, document_title="Phenomenology of Spirit")
            connection.execute("UPDATE paragraphs SET payload_json=? WHERE rowid=?",
                               (json.dumps(payload), rowid))
    generate_alignment(fixture.db, "work-one", "pdf-zh", "epub-en")
    handler = make_handler(fixture.db, app_context=AppContext.create(root, index_path=fixture.db))
    handler.log_message = lambda *args: None
    server = ManagedThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}"

    def create_hidden(*args, **kwargs):
        kwargs.update(hidden=True, focus=False)
        return webview.create_window(*args, **kwargs)

    shell = SimpleNamespace(create_window=create_hidden)
    manager = ReaderWindows(shell, lambda: "#ffffff")
    manager.set_base_url(url)
    window, _ = create_main_window(shell, "frost-blue")
    window.expose(manager.open_reader)
    window.events.closed += manager.close_all
    errors = []

    def readers():
        with manager._lock:
            return list(manager._windows)

    def open_reader(index):
        window.evaluate_js(
            "window.MEFinderReader.open({sourceId:'epub-en',anchorId:'epub-en-p%d',targetIndex:%d}); void 0"
            % (index, index)
        )
        wait_until(lambda: len(readers()) == index + 1, "reader did not open")
        reader = readers()[-1]
        wait_until(lambda: reader.evaluate_js(
            "!!(window.MEFinderReader && MEFinderReader.isOpen() && "
            "document.getElementById('reader-window-status').hidden)"
        ), "reader did not finish loading")
        return reader

    def drive():
        try:
            window.load_url(url)
            wait_until(lambda: window.evaluate_js(
                "!!(window.pywebview && window.pywebview.api.open_reader && window.MEFinderReader)"
            ), "main page not ready")
            # The persisted default must continue using the existing overlay.
            window.evaluate_js("window.MEFinderReader.open({sourceId:'epub-en'}); void 0")
            wait_until(lambda: window.evaluate_js("MEFinderReader.isOpen()"), "default overlay missing")
            assert not readers(), "default preference opened a native reader"
            window.evaluate_js("MEFinderReader.close(); void 0")
            save_preferences({"reader_window_enabled": True}, preferences)
            window.evaluate_js("window.loadPreferences(); void 0")
            wait_until(lambda: window.evaluate_js(
                "document.getElementById('reader-window-enabled').checked"
            ), "reader setting did not refresh")
            first = open_reader(0)
            second = open_reader(1)
            assert "Spirit is actual" in first.evaluate_js("document.body.textContent")
            assert "Truth is the whole" in second.evaluate_js("document.body.textContent")
            assert first.evaluate_js("MEFinderReader.getState().currentAnchorId") == "epub-en-p0"
            assert second.evaluate_js("MEFinderReader.getState().currentAnchorId") == "epub-en-p1"
            wait_until(lambda: first.evaluate_js(
                "!!document.querySelector('[data-reader-action=\"open-comparison\"]')"
            ), "comparison action missing")
            first.evaluate_js("document.querySelector('[data-reader-action=\"open-comparison\"]').click()")
            wait_until(lambda: first.evaluate_js(
                "document.querySelector('.mef-reader-comparison-pane').textContent.includes('精神是现实的')"
            ), "Chinese comparison did not load")
            assert not second.evaluate_js("MEFinderReader.getState().comparisonOpen")
            first_url = first.get_current_url()
            second.move(80, 80)
            second.resize(800, 600)
            wait_until(lambda: second.width == 800 and second.height == 600, "native resize failed")
            wait_until(lambda: second.x == 80 and second.y == 80, "native move failed")
            assert second.evaluate_js("""(function(){
                var panel=document.querySelector('.mef-reader-panel').getBoundingClientRect();
                var body=document.querySelector('.mef-reader-body').getBoundingClientRect();
                return Math.abs(panel.height-innerHeight)<1 && Math.abs(body.bottom-panel.bottom)<1;
            }())"""), "reader body did not fill the native viewport"
            print("PASS: default overlay, separate anchors/comparison, native move/resize and layout", flush=True)

            if "--legacy-close" in sys.argv:
                # Recreate the previous API without modifying installed pywebview.
                def close_reader():
                    second.destroy()
                second.expose(close_reader)
                close_script = "window.pywebview.api.close_reader()"
            else:
                close_script = "document.querySelector('.mef-reader-close').click()"
            # Let this test's evaluate_js return before initiating destruction;
            # the product close path itself contains no timer or forced exit.
            second.evaluate_js("setTimeout(function(){" + close_script + ";}, 200); void 0")
            wait_until(lambda: len(readers()) == 1, "in-page close did not destroy reader")
            assert first.get_current_url() == first_url, "closing a sibling changed reading position"
            assert first.evaluate_js("MEFinderReader.getState().comparisonOpen")
            print("PASS: in-page close preserves sibling", flush=True)
            reopened = open_reader(1)
            assert "Truth is the whole" in reopened.evaluate_js("document.body.textContent")
            # Also exercise closing through the native window API.
            reopened.destroy()
            wait_until(lambda: len(readers()) == 1, "native close did not destroy reader")
            open_reader(1)
            print("PASS: reopen and native close", flush=True)
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            window.destroy()

    webview.start(drive, storage_path=str(root / "webview"))
    handler.begin_shutdown()
    server.shutdown()
    server.server_close()
    handler.wait_for_durable_operations()
    assert server.wait_for_handlers(timeout=2), "HTTP handlers did not stop"
    assert handler.close_runtime(), "backend did not close"
    server_thread.join(timeout=2)
    wait_until(lambda: not readers(), "readers remained after main close")
    fixture.tearDown()
    if errors:
        raise AssertionError("\n".join(errors))
    print("PASS: GUI and backend cleanup; awaiting natural interpreter exit", flush=True)
    # Keep faulthandler armed: it also diagnoses interpreter shutdown waiting
    # for the old non-daemon bridge thread. The parent checks actual exit.


if __name__ == "__main__":
    main()
