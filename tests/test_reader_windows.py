"""Reader preference persistence, native window ownership and location transfer."""

from pathlib import Path
import importlib.util
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from src.me_finder.preferences import read_preferences, save_preferences
from src.me_finder.reader_windows import ReaderWindows
from src.me_finder.web_assets import render_html


class Event:
    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def fire(self, *args):
        for callback in self.callbacks:
            callback(*args)


class ReaderWindowTests(unittest.TestCase):
    def setUp(self):
        def create_window(*args, **kwargs):
            window = Mock(events=SimpleNamespace(closed=Event()), state=Event())
            window.destroy.side_effect = window.events.closed.fire
            return window

        self.webview = Mock()
        self.webview.create_window.side_effect = create_window
        self.windows = ReaderWindows(self.webview, lambda: "#ffffff")
        self.windows.set_base_url("http://127.0.0.1:1234/")

    @unittest.skipUnless(importlib.util.find_spec("webview"), "pywebview unavailable")
    def test_window_setting_is_at_top_of_pdf_reading_panel(self):
        html = render_html("midnight")
        panel = html[html.index('id="pdf-reader-body"'):]
        self.assertLess(panel.index('id="reader-window-enabled"'), panel.index('aria-label="PDF 打开方式"'))

    def test_close_notification_uses_real_bridge_without_reply_to_destroyed_window(self):
        from webview.state import State
        from webview.util import js_bridge_call

        destroyed = threading.Event()
        closed = Event()
        window = Mock(events=SimpleNamespace(closed=closed))
        window.state = State(window)
        window.destroy.side_effect = lambda: (closed.fire(), destroyed.set())
        self.webview.create_window.side_effect = lambda *args, **kwargs: window
        self.windows.open_reader({"sourceId": "book"})

        # Use pywebview's real JS-to-Python dispatcher, including its worker
        # threads. Closing must not schedule an acknowledgement on the WebView.
        js_bridge_call(window, "pywebviewStateUpdate",
                       {"key": "readerClosed", "value": True}, "close-test")
        self.assertTrue(destroyed.wait(1), "reader close notification was not handled")
        self.assertEqual(self.windows._windows, [])
        window.evaluate_js.assert_not_called()
        window.run_js.assert_not_called()

    def test_readers_keep_separate_locations_and_share_only_backend(self):
        options = {
            "sourceId": "中文书", "anchorId": "p-12", "matchQuote": "𠮷😀原句",
            "pageMatchSpans": [{"pdf_page_id": "p-12", "page_char_start": 2, "page_char_end": 4}],
        }
        self.assertTrue(self.windows.open_reader(options))
        self.windows.open_reader({"sourceId": "english", "targetIndex": 8})
        first, second = self.webview.create_window.call_args_list
        self.assertEqual(first.kwargs["url"], second.kwargs["url"])
        self.assertEqual(first.kwargs["url"], "http://127.0.0.1:1234/reader-window")
        self.assertEqual(first.kwargs["js_api"].reader_options(), options)
        self.assertEqual(second.kwargs["js_api"].reader_options()["targetIndex"], 8)
        self.assertTrue(first.kwargs["text_select"])
        self.assertTrue(first.kwargs["resizable"])
        first_window, second_window = self.windows._windows
        first_window.state.fire("change", "readerClosed", True)
        first_window.destroy.assert_called_once()
        second_window.destroy.assert_not_called()
        self.windows.close_all()
        second_window.destroy.assert_called_once()
        self.assertEqual(self.windows._windows, [])
        with self.assertRaisesRegex(RuntimeError, "退出"):
            self.windows.open_reader({"sourceId": "next"})

    def test_invalid_location_never_creates_a_window(self):
        for options in (None, [], {}, {"sourceId": 12}, {"sourceId": " "}, {"sourceId": "a" * 257}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.windows.open_reader(options)
        self.webview.create_window.assert_not_called()

    def test_failed_native_creation_is_reported_and_can_retry(self):
        create = self.webview.create_window.side_effect
        self.webview.create_window.side_effect = RuntimeError("native failure")
        with self.assertRaisesRegex(RuntimeError, "native failure"):
            self.windows.open_reader({"sourceId": "book"})
        self.assertEqual(self.windows._windows, [])
        self.webview.create_window.side_effect = create
        self.assertTrue(self.windows.open_reader({"sourceId": "book"}))

    def test_preference_defaults_off_survives_restart_and_rejects_non_boolean(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            self.assertFalse(read_preferences(path)["reader_window_enabled"])
            save_preferences({"reader_window_enabled": True}, path)
            save_preferences({"reader_line_mode": "physical"}, path)
            self.assertTrue(read_preferences(path)["reader_window_enabled"])
            for value in ("false", 0, 1, None, [], {}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    save_preferences({"reader_window_enabled": value}, path)
            self.assertTrue(read_preferences(path)["reader_window_enabled"])
            save_preferences({"reader_window_enabled": False}, path)
            self.assertFalse(read_preferences(path)["reader_window_enabled"])

    def test_dedicated_page_contains_reader_without_search_or_settings_initialization(self):
        html = render_html("midnight", reader_window=True)
        self.assertIn('data-reader-window="true"', html)
        self.assertIn('data-theme="midnight"', html)
        self.assertIn("global.MEFinderReader = Object.freeze", html)
        self.assertNotIn("function loadMeta()", html)
        self.assertNotIn('id="page-search"', html)
        self.assertNotIn("//__", html)
