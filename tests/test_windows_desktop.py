from __future__ import annotations

import ctypes
import sys
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest import mock

from src.me_finder import windows_desktop
from src.me_finder.windows_desktop import (
    WindowsPDFViewer,
    WindowsWindowController,
    apply_windows_titlebar,
    configure_windows_chromeless,
    pdf_file_url,
)


class _FakeHandle:
    def __init__(self, value: int = 321) -> None:
        self.value = value

    def ToInt64(self) -> int:
        return self.value


class _FakeEvent:
    def __init__(self) -> None:
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self


class _FakeEvents:
    def __init__(self) -> None:
        self.closed = _FakeEvent()
        self.loaded = _FakeEvent()
        self.maximized = _FakeEvent()
        self.moved = _FakeEvent()
        self.restored = _FakeEvent()


class _FakeWindow:
    def __init__(self) -> None:
        self.native = type("Native", (), {"Handle": _FakeHandle()})()
        self.events = _FakeEvents()
        self.titles = []
        self.urls = []
        self.evaluated_scripts = []
        self.show_count = 0
        self.minimize_count = 0
        self.maximize_count = 0
        self.restore_count = 0
        self.destroy_count = 0

    def set_title(self, title: str) -> None:
        self.titles.append(title)

    def load_url(self, url: str) -> None:
        self.urls.append(url)

    def show(self) -> None:
        self.show_count += 1

    def minimize(self) -> None:
        self.minimize_count += 1

    def maximize(self) -> None:
        self.maximize_count += 1

    def restore(self) -> None:
        self.restore_count += 1

    def destroy(self) -> None:
        self.destroy_count += 1

    def evaluate_js(self, script: str) -> None:
        self.evaluated_scripts.append(script)


class _FakeWebview:
    renderer = "edgechromium"

    def __init__(self) -> None:
        self.created = []

    def create_window(self, title: str, **kwargs):
        window = _FakeWindow()
        self.created.append((title, kwargs, window))
        return window


class _BlockingLoadWindow(_FakeWindow):
    def __init__(self) -> None:
        super().__init__()
        self.load_started = threading.Event()
        self.allow_load = threading.Event()

    def load_url(self, url: str) -> None:
        self.load_started.set()
        if not self.allow_load.wait(5):
            raise TimeoutError("test did not release PDF navigation")
        raise RuntimeError("the reused native window closed during navigation")


class _BlockingCreateWebview(_FakeWebview):
    def __init__(self) -> None:
        super().__init__()
        self.create_started = threading.Event()
        self.allow_create = threading.Event()
        self.failure = None

    def create_window(self, title: str, **kwargs):
        self.create_started.set()
        if not self.allow_create.wait(5):
            raise TimeoutError("test did not release PDF window creation")
        if self.failure is not None:
            raise self.failure
        return super().create_window(title, **kwargs)


class _ObservablePDFViewer(WindowsPDFViewer):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.operation_waiting = threading.Event()

    def _begin_operation(self):
        with self._condition:
            if self._operation_active:
                self.operation_waiting.set()
        return super()._begin_operation()


class WindowsNativeLibraryIsolationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "ctypes.windll is Windows-only")
    def test_native_helpers_leave_pywebviews_shared_setwindowpos_callable(self) -> None:
        """Titlebar dragging breaks if we constrain the shared ctypes handle.

        pywebview moves the frameless window through the process-wide
        ``ctypes.windll.user32.SetWindowPos``, passing ``None`` for the size it
        does not use.  When this module declared ``argtypes`` on that same
        cached function object, every drag raised ``ctypes.ArgumentError``
        inside pywebview and the window silently refused to move.
        """

        # Invalid handle: the call is rejected by Windows without side effects,
        # but it still runs our argtypes declarations.
        windows_desktop._refresh_native_window_frame(0)
        windows_desktop._get_native_window_style(0)

        self.assertIsNot(windows_desktop._library("user32"), ctypes.windll.user32)
        try:
            ctypes.windll.user32.SetWindowPos(0, None, 0, 0, None, None, 0)
        except ctypes.ArgumentError as exc:  # pragma: no cover - regression guard
            self.fail(f"shared SetWindowPos no longer accepts pywebview's call: {exc}")


class WindowsTitlebarTests(unittest.TestCase):
    def test_chromeless_style_removes_caption_and_restores_window_controls(self) -> None:
        window = _FakeWindow()
        original_style = windows_desktop._WS_CAPTION | 0x10000000
        style_getter = mock.Mock(return_value=original_style)
        style_setter = mock.Mock(return_value=original_style)
        frame_refresher = mock.Mock(return_value=1)
        attribute_setter = mock.Mock(return_value=0)
        maximize_bounds_preparer = mock.Mock(return_value=True)
        top_inset_remover = mock.Mock(return_value=True)

        with mock.patch.object(windows_desktop.sys, "platform", "win32"):
            configured = configure_windows_chromeless(
                window,
                style_getter=style_getter,
                style_setter=style_setter,
                frame_refresher=frame_refresher,
                attribute_setter=attribute_setter,
                maximize_bounds_preparer=maximize_bounds_preparer,
                top_inset_remover=top_inset_remover,
            )

        expected_style = (
            (original_style & ~windows_desktop._WS_CAPTION)
            | windows_desktop._WS_THICKFRAME
            | windows_desktop._WS_SYSMENU
            | windows_desktop._WS_MINIMIZEBOX
            | windows_desktop._WS_MAXIMIZEBOX
        )
        self.assertTrue(configured)
        style_getter.assert_called_once_with(321)
        style_setter.assert_called_once_with(321, expected_style)
        self.assertEqual(expected_style & windows_desktop._WS_CAPTION, 0)
        frame_refresher.assert_called_once_with(321)
        self.assertEqual(
            attribute_setter.call_args_list,
            [
                mock.call(
                    321,
                    windows_desktop._DWMWA_WINDOW_CORNER_PREFERENCE,
                    windows_desktop._DWMWCP_ROUND,
                ),
                mock.call(
                    321,
                    windows_desktop._DWMWA_BORDER_COLOR,
                    windows_desktop._DWMWA_COLOR_NONE,
                ),
            ],
        )
        maximize_bounds_preparer.assert_called_once_with(window)
        top_inset_remover.assert_called_once_with(321)

    def test_chromeless_configuration_is_a_noop_outside_windows(self) -> None:
        style_getter = mock.Mock()
        with mock.patch.object(windows_desktop.sys, "platform", "darwin"):
            configured = configure_windows_chromeless(
                _FakeWindow(),
                style_getter=style_getter,
            )

        self.assertFalse(configured)
        style_getter.assert_not_called()

    def test_midnight_theme_applies_dark_mode_and_explicit_dwm_colors(self) -> None:
        window = _FakeWindow()
        calls = []

        def setter(hwnd: int, attribute: int, value: int) -> int:
            calls.append((hwnd, attribute, value))
            return 0

        with mock.patch.object(windows_desktop.sys, "platform", "win32"):
            applied = apply_windows_titlebar(
                window,
                "midnight",
                attribute_setter=setter,
            )

        self.assertTrue(applied)
        self.assertEqual(
            calls,
            [
                (321, 20, 1),
                (321, 34, 0x3D3630),
                (321, 35, 0x17110D),
                (321, 36, 0xF3EDE6),
            ],
        )

    def test_older_windows_dark_attribute_falls_back_from_20_to_19(self) -> None:
        window = _FakeWindow()
        calls = []

        def setter(_hwnd: int, attribute: int, _value: int) -> int:
            calls.append(attribute)
            return -1 if attribute == 20 else 0

        with mock.patch.object(windows_desktop.sys, "platform", "win32"):
            applied = apply_windows_titlebar(
                window,
                "midnight",
                attribute_setter=setter,
            )

        self.assertTrue(applied)
        self.assertEqual(calls[:2], [20, 19])
        self.assertEqual(calls[2:], [34, 35, 36])

    def test_non_windows_platform_is_a_noop(self) -> None:
        setter = mock.Mock()
        with mock.patch.object(windows_desktop.sys, "platform", "darwin"):
            applied = apply_windows_titlebar(
                _FakeWindow(),
                "midnight",
                attribute_setter=setter,
            )
        self.assertFalse(applied)
        setter.assert_not_called()


class WindowsWindowControllerTests(unittest.TestCase):
    def test_unbound_controller_actions_are_safe_noops(self) -> None:
        preparer = mock.Mock(return_value=True)
        controller = WindowsWindowController(maximize_bounds_preparer=preparer)

        self.assertFalse(controller.is_maximized())
        self.assertFalse(controller.minimize())
        self.assertFalse(controller.toggle_maximize())
        self.assertFalse(controller.close())
        preparer.assert_not_called()

    def test_private_bind_subscribes_to_native_window_events(self) -> None:
        window = _FakeWindow()
        controller = WindowsWindowController()

        controller._bind(window)

        self.assertIs(controller._bound_window(), window)
        self.assertNotIn("window", vars(controller))
        self.assertEqual(window.events.maximized.callbacks, [controller._on_maximized])
        self.assertEqual(window.events.restored.callbacks, [controller._on_restored])
        self.assertEqual(window.events.loaded.callbacks, [controller._on_loaded])
        self.assertEqual(window.events.moved.callbacks, [controller._on_moved])
        self.assertEqual(window.events.closed.callbacks, [controller._on_closed])

    def test_window_actions_minimize_maximize_restore_and_close(self) -> None:
        window = _FakeWindow()
        preparer = mock.Mock(return_value=True)
        controller = WindowsWindowController(maximize_bounds_preparer=preparer)
        controller._bind(window)

        self.assertTrue(controller.minimize())
        self.assertTrue(controller.toggle_maximize())
        self.assertTrue(controller.is_maximized())
        self.assertFalse(controller.toggle_maximize())
        self.assertFalse(controller.is_maximized())
        self.assertTrue(controller.close())

        self.assertEqual(window.minimize_count, 1)
        self.assertEqual(window.maximize_count, 1)
        self.assertEqual(window.restore_count, 1)
        self.assertEqual(window.destroy_count, 1)
        preparer.assert_called_once_with(window)
        self.assertIn("setWindowsMaximized(true)", window.evaluated_scripts[0])
        self.assertIn("setWindowsMaximized(false)", window.evaluated_scripts[1])

    def test_events_track_state_sync_js_and_refresh_maximize_bounds(self) -> None:
        window = _FakeWindow()
        preparer = mock.Mock(return_value=True)
        controller = WindowsWindowController(maximize_bounds_preparer=preparer)
        controller._bind(window)

        window.events.loaded.callbacks[0]()
        window.events.moved.callbacks[0]()
        window.events.maximized.callbacks[0]()
        window.events.moved.callbacks[0]()
        window.events.restored.callbacks[0]()

        self.assertFalse(controller.is_maximized())
        preparer.assert_called_once_with(window)
        self.assertEqual(
            window.evaluated_scripts,
            [
                "window.setWindowsMaximized && window.setWindowsMaximized(false);",
                "window.setWindowsMaximized && window.setWindowsMaximized(true);",
                "window.setWindowsMaximized && window.setWindowsMaximized(false);",
            ],
        )

        window.events.maximized.callbacks[0]()
        window.events.closed.callbacks[0]()
        self.assertFalse(controller.is_maximized())
        self.assertIsNone(controller._bound_window())


class WindowsPDFViewerTests(unittest.TestCase):
    @staticmethod
    def _record_failure(errors, operation) -> None:
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)

    def test_pdf_url_encodes_path_and_carries_one_based_page_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "中文 文献.pdf"
            target.write_bytes(b"%PDF-test")
            url = pdf_file_url(target, 9)

        self.assertTrue(url.startswith("file:///"))
        self.assertIn("%E4%B8%AD%E6%96%87%20%E6%96%87%E7%8C%AE.pdf", url)
        self.assertTrue(url.endswith("#page=9"))

    def test_viewer_creates_one_webview2_window_clamps_and_reuses_it(self) -> None:
        webview = _FakeWebview()
        applied = []
        viewer = WindowsPDFViewer(
            webview,
            "midnight",
            page_counter=lambda _target: 10,
            titlebar_applier=lambda window, theme: applied.append((window, theme)) or True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.pdf"
            target.write_bytes(b"%PDF-test")

            first = viewer.open(target, 99)
            second = viewer.open(target, 3)

        self.assertEqual(first["page"], 10)
        self.assertEqual(first["page_count"], 10)
        self.assertTrue(str(first["url"]).endswith("#page=10"))
        self.assertEqual(second["page"], 3)
        self.assertEqual(len(webview.created), 1)
        title, kwargs, window = webview.created[0]
        self.assertIn("source.pdf", title)
        self.assertEqual(kwargs["background_color"], "#0D1117")
        self.assertTrue(str(kwargs["url"]).endswith("#page=10"))
        self.assertEqual(applied, [(window, "midnight")])
        self.assertEqual(window.urls, [second["url"]])
        self.assertEqual(window.show_count, 1)
        self.assertEqual(window.restore_count, 1)

        viewer.close()
        self.assertEqual(window.destroy_count, 1)
        self.assertIsNone(viewer.window)

    def test_viewer_requires_edgechromium_and_an_existing_pdf(self) -> None:
        webview = _FakeWebview()
        webview.renderer = "cef"
        viewer = WindowsPDFViewer(webview, "frost-blue")
        with self.assertRaisesRegex(RuntimeError, "WebView2"):
            viewer.open(Path("missing.pdf"), 1)

        webview.renderer = "edgechromium"
        with self.assertRaises(FileNotFoundError):
            viewer.open(Path("missing.pdf"), 1)

    def test_closed_event_forgets_window_and_theme_updates_live_window(self) -> None:
        webview = _FakeWebview()
        applied = []
        viewer = WindowsPDFViewer(
            webview,
            "frost-blue",
            page_counter=lambda _target: None,
            titlebar_applier=lambda window, theme: applied.append((window, theme)) or True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.pdf"
            target.write_bytes(b"%PDF-test")
            viewer.open(target)
            window = viewer.window
            viewer.set_theme("midnight")
            window.events.closed.callbacks[0]()

        self.assertEqual([theme for _window, theme in applied], ["frost-blue", "midnight"])
        self.assertIsNone(viewer.window)

    def test_optional_titlebar_failure_does_not_block_pdf_window(self) -> None:
        webview = _FakeWebview()
        viewer = WindowsPDFViewer(
            webview,
            "midnight",
            page_counter=lambda _target: 4,
            titlebar_applier=mock.Mock(side_effect=RuntimeError("DWM unavailable")),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.pdf"
            target.write_bytes(b"%PDF-test")
            result = viewer.open(target, "invalid")

        self.assertIsNone(result["page"])
        self.assertEqual(len(webview.created), 1)

    def test_close_does_not_wait_for_blocked_reuse_or_recreate_window(self) -> None:
        window = _BlockingLoadWindow()
        webview = _FakeWebview()
        webview.created.append(("existing", {}, window))
        viewer = WindowsPDFViewer(
            webview,
            "frost-blue",
            page_counter=lambda _target: 3,
            titlebar_applier=lambda _window, _theme: True,
        )
        viewer.window = window
        errors = []
        close_finished = threading.Event()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.pdf"
            target.write_bytes(b"%PDF-test")
            opener = threading.Thread(
                target=self._record_failure,
                args=(errors, lambda: viewer.open(target, 2)),
            )
            opener.start()
            self.assertTrue(window.load_started.wait(2))

            closer = threading.Thread(
                target=lambda: (viewer.close(), close_finished.set())
            )
            closer.start()
            closed_without_navigation = close_finished.wait(2)
            window.allow_load.set()
            opener.join(5)
            closer.join(5)

        self.assertTrue(closed_without_navigation)
        self.assertFalse(opener.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(webview.created), 1)
        self.assertEqual(window.destroy_count, 1)
        self.assertIsNone(viewer.window)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CancelledError)

    def test_close_rejects_and_destroys_candidate_created_in_flight(self) -> None:
        webview = _BlockingCreateWebview()
        viewer = WindowsPDFViewer(
            webview,
            "frost-blue",
            page_counter=lambda _target: 3,
            titlebar_applier=lambda _window, _theme: True,
        )
        errors = []

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.pdf"
            target.write_bytes(b"%PDF-test")
            opener = threading.Thread(
                target=self._record_failure,
                args=(errors, lambda: viewer.open(target, 2)),
            )
            opener.start()
            self.assertTrue(webview.create_started.wait(2))
            viewer.close()
            webview.allow_create.set()
            opener.join(5)

        self.assertFalse(opener.is_alive())
        self.assertEqual(len(webview.created), 1)
        candidate = webview.created[0][2]
        self.assertEqual(candidate.destroy_count, 1)
        self.assertIsNone(viewer.window)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CancelledError)

    def test_create_failure_after_close_is_reported_as_cancellation(self) -> None:
        webview = _BlockingCreateWebview()
        webview.failure = RuntimeError("WebView2 host is closing")
        viewer = WindowsPDFViewer(
            webview,
            "frost-blue",
            page_counter=lambda _target: 3,
            titlebar_applier=lambda _window, _theme: True,
        )
        errors = []

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.pdf"
            target.write_bytes(b"%PDF-test")
            opener = threading.Thread(
                target=self._record_failure,
                args=(errors, lambda: viewer.open(target, 2)),
            )
            opener.start()
            self.assertTrue(webview.create_started.wait(2))
            viewer.close()
            webview.allow_create.set()
            opener.join(5)

        self.assertFalse(opener.is_alive())
        self.assertEqual(webview.created, [])
        self.assertIsNone(viewer.window)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CancelledError)
        self.assertIsInstance(errors[0].__cause__, RuntimeError)

    def test_concurrent_opens_share_one_window_after_creation(self) -> None:
        webview = _BlockingCreateWebview()
        viewer = _ObservablePDFViewer(
            webview,
            "frost-blue",
            page_counter=lambda _target: 8,
            titlebar_applier=lambda _window, _theme: True,
        )
        errors = []

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.pdf"
            target.write_bytes(b"%PDF-test")
            first = threading.Thread(
                target=self._record_failure,
                args=(errors, lambda: viewer.open(target, 2)),
            )
            second = threading.Thread(
                target=self._record_failure,
                args=(errors, lambda: viewer.open(target, 7)),
            )
            first.start()
            self.assertTrue(webview.create_started.wait(2))
            second.start()
            self.assertTrue(viewer.operation_waiting.wait(2))
            webview.allow_create.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(webview.created), 1)
        window = webview.created[0][2]
        self.assertEqual(len(window.urls), 1)
        self.assertTrue(window.urls[0].endswith("#page=7"))
        self.assertEqual(window.show_count, 1)
        self.assertEqual(window.restore_count, 1)

    def test_manually_closed_child_can_be_created_again(self) -> None:
        webview = _FakeWebview()
        viewer = WindowsPDFViewer(
            webview,
            "frost-blue",
            page_counter=lambda _target: 3,
            titlebar_applier=lambda _window, _theme: True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "source.pdf"
            target.write_bytes(b"%PDF-test")
            viewer.open(target, 1)
            first_window = viewer.window
            first_window.events.closed.callbacks[0]()
            viewer.open(target, 2)

        self.assertEqual(len(webview.created), 2)
        self.assertIsNot(viewer.window, first_window)


if __name__ == "__main__":
    unittest.main()
