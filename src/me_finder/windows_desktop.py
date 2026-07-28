"""Windows-only desktop integrations kept separate from the web backend.

The main window uses a frameless, HTML-drawn titlebar so it can share the exact
same theme tokens and paint as the rest of the application.  This module keeps
the small native pieces needed by that shell separate from the web backend:

* a JS API controller for the HTML minimize/maximize/close controls;
* native resize-frame restoration for the frameless WinForms window;
* theme-aware DWM title bars for the separate PDF reader window;
* a reusable Edge WebView2 PDF window that navigates to ``#page=N`` so search
  results open directly at the physical PDF page.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Dict, Optional


PDF_WINDOW_TITLE = "文献原句定位器 · PDF 阅读"

_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_SYSMENU = 0x00080000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

_TITLEBAR_PALETTES = {
    "frost-blue": ("#F5F8FC", "#172033", "#DDE5EF"),
    "sage-ivory": ("#F7F7F1", "#25291F", "#DEE1D4"),
    "warm-sand": ("#FBF7F1", "#34251E", "#E7D9CC"),
    "rose-mist": ("#FDF6F8", "#2C2528", "#EBDCE2"),
    "lavender-purple": ("#F9F7FD", "#282532", "#DED8EB"),
    "midnight": ("#08111D", "#EEF4FB", "#2A394A"),
}


def _hex_to_colorref(value: str) -> int:
    """Convert ``#RRGGBB`` to the BGR COLORREF expected by DWM."""

    text = value.lstrip("#")
    red, green, blue = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    return red | (green << 8) | (blue << 16)


def _window_handle(window: object) -> int:
    native = getattr(window, "native", None)
    handle = getattr(native, "Handle", None)
    if handle is None:
        raise RuntimeError("Windows 原生窗口句柄尚未就绪。")
    if hasattr(handle, "ToInt64"):
        return int(handle.ToInt64())
    if hasattr(handle, "ToInt32"):
        return int(handle.ToInt32())
    return int(handle)


def _set_dwm_attribute(hwnd: int, attribute: int, value: int) -> int:
    setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
    setter.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    setter.restype = ctypes.c_long
    payload = ctypes.c_uint(int(value) & 0xFFFFFFFF)
    return int(setter(hwnd, attribute, ctypes.byref(payload), ctypes.sizeof(payload)))


def _get_native_window_style(hwnd: int) -> int:
    getter = getattr(ctypes.windll.user32, "GetWindowLongPtrW", None)
    if getter is None:
        getter = ctypes.windll.user32.GetWindowLongW
    getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
    getter.restype = ctypes.c_ssize_t
    return int(getter(hwnd, _GWL_STYLE))


def _set_native_window_style(hwnd: int, style: int) -> int:
    setter = getattr(ctypes.windll.user32, "SetWindowLongPtrW", None)
    if setter is None:
        setter = ctypes.windll.user32.SetWindowLongW
    setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    setter.restype = ctypes.c_ssize_t
    return int(setter(hwnd, _GWL_STYLE, int(style)))


def _refresh_native_window_frame(hwnd: int) -> int:
    refresher = ctypes.windll.user32.SetWindowPos
    refresher.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    refresher.restype = ctypes.c_int
    flags = (
        _SWP_NOSIZE
        | _SWP_NOMOVE
        | _SWP_NOZORDER
        | _SWP_NOACTIVATE
        | _SWP_FRAMECHANGED
    )
    return int(refresher(hwnd, None, 0, 0, 0, 0, flags))


def prepare_windows_maximized_bounds(window: object) -> bool:
    """Constrain a frameless maximize operation to the monitor work area."""

    if sys.platform != "win32":
        return False
    native = getattr(window, "native", None)
    if native is None:
        return False

    def update() -> None:
        from System.Drawing import Rectangle
        from System.Windows.Forms import Screen

        hwnd = _window_handle(window)
        outer = wintypes.RECT()
        client = wintypes.RECT()
        user32 = ctypes.windll.user32
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        if not user32.GetWindowRect(hwnd, ctypes.byref(outer)):
            return
        if not user32.GetClientRect(hwnd, ctypes.byref(client)):
            return
        outer_width = outer.right - outer.left
        outer_height = outer.bottom - outer.top
        client_width = client.right - client.left
        client_height = client.bottom - client.top
        border_x = max(0, (outer_width - client_width) // 2)
        border_y = max(0, (outer_height - client_height) // 2)
        work = Screen.FromHandle(native.Handle).WorkingArea
        native.MaximizedBounds = Rectangle(
            work.X - border_x,
            work.Y - border_y,
            work.Width + border_x * 2,
            work.Height + border_y * 2,
        )

    if bool(getattr(native, "InvokeRequired", False)):
        from System import Action

        native.Invoke(Action(update))
    else:
        update()
    return True


def configure_windows_chromeless(
    window: object,
    *,
    style_getter: Optional[Callable[[int], int]] = None,
    style_setter: Optional[Callable[[int, int], int]] = None,
    frame_refresher: Optional[Callable[[int], int]] = None,
    attribute_setter: Optional[Callable[[int, int, int], int]] = None,
    maximize_bounds_preparer: Optional[Callable[[object], bool]] = None,
) -> bool:
    """Keep native resize/snap behavior while the HTML owns the titlebar.

    WinForms removes ``WS_THICKFRAME`` together with its caption when
    ``frameless=True``.  Adding only the non-client resize style back leaves no
    visible system titlebar, but restores edge resizing, Windows snap/maximize,
    the DWM shadow and rounded corners.
    """

    if sys.platform != "win32":
        return False
    hwnd = _window_handle(window)
    getter = style_getter or _get_native_window_style
    setter = style_setter or _set_native_window_style
    refresher = frame_refresher or _refresh_native_window_frame
    current_style = getter(hwnd)
    chromeless_style = (
        (current_style & ~_WS_CAPTION)
        | _WS_THICKFRAME
        | _WS_SYSMENU
        | _WS_MINIMIZEBOX
        | _WS_MAXIMIZEBOX
    )
    setter(hwnd, chromeless_style)
    refresher(hwnd)

    # DWMWA_WINDOW_CORNER_PREFERENCE=33, DWMWCP_ROUND=2 (Windows 11).
    # Unsupported older Windows versions simply retain their normal outline.
    try:
        (attribute_setter or _set_dwm_attribute)(hwnd, 33, 2)
    except Exception:
        logging.debug("rounded frameless corners are unavailable", exc_info=True)
    try:
        (maximize_bounds_preparer or prepare_windows_maximized_bounds)(window)
    except Exception:
        logging.debug("frameless maximize bounds are unavailable", exc_info=True)
    return True


class WindowsWindowController:
    """Small pywebview JS API used by the HTML titlebar controls."""

    def __init__(
        self,
        *,
        maximize_bounds_preparer: Callable[[object], bool] = prepare_windows_maximized_bounds,
    ) -> None:
        # Keep the bound window private. pywebview recursively reflects public
        # js_api attributes and would otherwise walk the native WinForms tree.
        self._window = None
        self._maximized = False
        self._lock = threading.RLock()
        self._maximize_bounds_preparer = maximize_bounds_preparer

    def _bind(self, window: object) -> None:
        with self._lock:
            self._window = window

        events = getattr(window, "events", None)
        if events is None:
            return
        if hasattr(events, "maximized"):
            events.maximized += self._on_maximized
        if hasattr(events, "restored"):
            events.restored += self._on_restored
        if hasattr(events, "loaded"):
            events.loaded += self._on_loaded
        if hasattr(events, "moved"):
            events.moved += self._on_moved
        if hasattr(events, "closed"):
            events.closed += self._on_closed

    def _bound_window(self):
        with self._lock:
            return self._window

    def _sync_html_state(self) -> None:
        window = self._bound_window()
        if window is None or not hasattr(window, "evaluate_js"):
            return
        with self._lock:
            maximized = self._maximized
        try:
            window.evaluate_js(
                "window.setWindowsMaximized && "
                f"window.setWindowsMaximized({'true' if maximized else 'false'});"
            )
        except Exception:
            logging.debug("Windows titlebar state is not ready for sync", exc_info=True)

    def _on_maximized(self, *_args: object) -> None:
        with self._lock:
            self._maximized = True
        self._sync_html_state()

    def _on_restored(self, *_args: object) -> None:
        with self._lock:
            self._maximized = False
        self._sync_html_state()

    def _on_loaded(self, *_args: object) -> None:
        self._sync_html_state()

    def _on_moved(self, *_args: object) -> None:
        with self._lock:
            maximized = self._maximized
        window = self._bound_window()
        if maximized or window is None:
            return
        try:
            self._maximize_bounds_preparer(window)
        except Exception:
            logging.debug("could not refresh maximize bounds after move", exc_info=True)

    def _on_closed(self, *_args: object) -> None:
        with self._lock:
            self._window = None
            self._maximized = False

    def is_maximized(self) -> bool:
        with self._lock:
            return self._maximized

    def minimize(self) -> bool:
        window = self._bound_window()
        if window is None:
            return False
        window.minimize()
        return True

    def toggle_maximize(self) -> bool:
        window = self._bound_window()
        if window is None:
            return False
        with self._lock:
            was_maximized = self._maximized
            self._maximized = not was_maximized
        try:
            if was_maximized:
                window.restore()
            else:
                self._maximize_bounds_preparer(window)
                window.maximize()
        except Exception:
            with self._lock:
                self._maximized = was_maximized
            raise
        self._sync_html_state()
        return not was_maximized

    def close(self) -> bool:
        window = self._bound_window()
        if window is None:
            return False
        window.destroy()
        return True


def apply_windows_titlebar(
    window: object,
    theme: str,
    *,
    attribute_setter: Optional[Callable[[int, int, int], int]] = None,
) -> bool:
    """Apply the selected app theme to a standard Windows title bar.

    Windows 11 accepts explicit caption/text/border colors.  Older Windows 10
    builds may only accept the immersive-dark flag, so unsupported attributes
    are intentionally best-effort and never prevent application startup.
    """

    if sys.platform != "win32":
        return False
    palette = _TITLEBAR_PALETTES.get(theme, _TITLEBAR_PALETTES["frost-blue"])
    caption, text, border = (_hex_to_colorref(color) for color in palette)
    setter = attribute_setter or _set_dwm_attribute
    hwnd = _window_handle(window)
    applied = False

    # DWMWA_USE_IMMERSIVE_DARK_MODE is 20 on current Windows and 19 on a
    # narrow band of older Windows 10 releases.
    dark = 1 if theme == "midnight" else 0
    try:
        result = setter(hwnd, 20, dark)
        applied = result >= 0
        if result < 0:
            applied = setter(hwnd, 19, dark) >= 0
    except Exception:
        try:
            applied = setter(hwnd, 19, dark) >= 0
        except Exception:
            logging.debug("immersive titlebar mode is unavailable", exc_info=True)

    # Windows 11: DWMWA_BORDER_COLOR=34, CAPTION_COLOR=35, TEXT_COLOR=36.
    for attribute, value in ((34, border), (35, caption), (36, text)):
        try:
            applied = setter(hwnd, attribute, value) >= 0 or applied
        except Exception:
            logging.debug("DWM titlebar attribute %s is unavailable", attribute, exc_info=True)
    return applied


def pdf_file_url(target: Path, page: Optional[int]) -> str:
    resolved = Path(target).expanduser().resolve()
    url = resolved.as_uri()
    return f"{url}#page={int(page)}" if page is not None and int(page) > 0 else url


def _pdf_page_count(target: Path) -> Optional[int]:
    try:
        import fitz

        document = fitz.open(str(target))
        try:
            return int(len(document))
        finally:
            document.close()
    except Exception:
        logging.debug("could not read PDF page count for viewer", exc_info=True)
        return None


class WindowsPDFViewer:
    """Reusable pywebview child window backed by the Edge WebView2 PDF viewer."""

    def __init__(
        self,
        webview_module: object,
        theme: str,
        *,
        page_counter: Callable[[Path], Optional[int]] = _pdf_page_count,
        titlebar_applier: Callable[[object, str], bool] = apply_windows_titlebar,
    ) -> None:
        self.webview = webview_module
        self.theme = theme
        self.page_counter = page_counter
        self.titlebar_applier = titlebar_applier
        self.window = None
        self._lock = threading.RLock()

    @property
    def background_color(self) -> str:
        return _TITLEBAR_PALETTES.get(
            self.theme, _TITLEBAR_PALETTES["frost-blue"]
        )[0]

    def set_theme(self, theme: str) -> None:
        self.theme = theme if theme in _TITLEBAR_PALETTES else "frost-blue"
        with self._lock:
            window = self.window
        if window is not None and getattr(window, "native", None) is not None:
            try:
                self.titlebar_applier(window, self.theme)
            except Exception:
                logging.exception("failed to update PDF reader titlebar")

    def _create_window(self, url: str, title: str):
        window = self.webview.create_window(
            title,
            url=url,
            width=1120,
            height=820,
            min_size=(720, 520),
            background_color=self.background_color,
            text_select=True,
            zoomable=True,
        )
        if window is None:
            raise RuntimeError("Edge WebView2 PDF 窗口创建失败。")

        def forget_closed_window() -> None:
            with self._lock:
                if self.window is window:
                    self.window = None

        window.events.closed += forget_closed_window
        self.window = window
        # Runtime child windows have already emitted before_show by the time
        # create_window returns, and their native handle is ready at this point.
        try:
            self.titlebar_applier(window, self.theme)
        except Exception:
            # Titlebar styling is optional and must not make PDF opening fail.
            logging.exception("failed to configure PDF reader titlebar")
        return window

    def open(self, target: Path, page: Optional[int] = None) -> Dict[str, object]:
        if getattr(self.webview, "renderer", None) != "edgechromium":
            raise RuntimeError(
                "应用内 PDF 阅读需要 Microsoft Edge WebView2 Runtime。"
            )
        target = Path(target).expanduser().resolve()
        if not target.is_file() or target.suffix.lower() != ".pdf":
            raise FileNotFoundError(f"PDF 文件不存在：{target}")

        page_count = self.page_counter(target)
        try:
            requested = int(page) if page is not None else None
        except (TypeError, ValueError):
            requested = None
        if requested is not None and requested <= 0:
            requested = None
        actual_page = requested
        if requested is not None and page_count:
            actual_page = min(max(requested, 1), page_count)
        url = pdf_file_url(target, actual_page)
        title = f"{PDF_WINDOW_TITLE} · {target.name}"

        with self._lock:
            window = self.window
            if window is None:
                window = self._create_window(url, title)
            else:
                try:
                    window.set_title(title)
                    window.load_url(url)
                    window.show()
                    window.restore()
                except Exception:
                    logging.exception("recreating a closed WebView2 PDF window")
                    self.window = None
                    try:
                        window.destroy()
                    except Exception:
                        logging.debug("old PDF reader window was already closed", exc_info=True)
                    window = self._create_window(url, title)

        return {"page": actual_page, "page_count": page_count, "url": url}

    def close(self, *_args: object) -> None:
        with self._lock:
            window = self.window
            self.window = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                logging.debug("PDF reader was already closed", exc_info=True)
