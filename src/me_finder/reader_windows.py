"""Native structured readers sharing the desktop application's local backend."""

from __future__ import annotations

import threading
from typing import Mapping


class ReaderWindowBridge:
    """Expose only this reader's initial location to JavaScript."""

    def __init__(self, options: dict) -> None:
        self._options = options

    def reader_options(self) -> dict:
        """Return the original search anchors without converting character offsets."""
        return self._options


class ReaderWindows:
    """Own reader windows; the main window owns their application lifetime."""

    def __init__(self, webview, theme_background) -> None:
        self._webview = webview
        self._theme_background = theme_background
        self._base_url = ""
        self._windows: list = []
        self._lock = threading.RLock()
        self._closing = False

    def set_base_url(self, url: str) -> None:
        """Bind the already-started backend before exposing the main application."""
        self._base_url = url.rstrip("/")

    def open_reader(self, options: object) -> bool:
        """Open an independent reader from a JSON location supplied by the UI."""
        if not isinstance(options, Mapping):
            raise ValueError("阅读位置必须是对象")
        source_id = options.get("sourceId") or options.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 256:
            raise ValueError("缺少有效的文献标识")
        with self._lock:
            if self._closing:
                raise RuntimeError("应用正在退出，无法打开阅读窗口")
            bridge = ReaderWindowBridge(dict(options))
            title = str(options.get("title") or options.get("documentTitle") or "结构化阅读")[:200]
            window = self._webview.create_window(
                "MEFinder · " + title,
                url=self._base_url + "/reader-window",
                js_api=bridge,
                width=1200,
                height=820,
                min_size=(640, 480),
                resizable=True,
                text_select=True,
                background_color=self._theme_background(),
            )
            self._windows.append(window)

            def reader_state_changed(event_type, key, value) -> None:
                # A JS API return value would be sent to the destroyed WebView
                # and can strand pywebview's non-daemon bridge thread on macOS.
                # JS-originated state notifications do not send a reply.
                if event_type == "change" and key == "readerClosed" and value is True:
                    window.destroy()

            def forget_window() -> None:
                with self._lock:
                    self._windows.remove(window)

            window.events.closed += forget_window
            window.state += reader_state_changed
        return True

    def close_all(self) -> None:
        """Close readers after the main window has actually closed."""
        with self._lock:
            self._closing = True
            windows = list(self._windows)
        for window in windows:
            window.destroy()
