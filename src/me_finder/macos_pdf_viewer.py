"""Small native PDFKit reader used by the macOS desktop shell.

The local HTTP API runs on worker threads, while every AppKit/PDFKit operation
must run on the Cocoa main thread.  ``MacOSPDFViewer.open`` therefore validates
the request and schedules the actual window work with ``AppHelper.callAfter``.
Framework imports stay inside the main-thread controller factory so importing
the rest of MEFinder remains safe on Windows.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Callable, Optional


MAIN_THREAD_OPEN_TIMEOUT_SECONDS = 20.0
_PDF_VIEWER_CONTROLLER_CLASS: object | None = None


def normalize_page_number(page: object) -> Optional[int]:
    """Return a positive, one-based physical PDF page or ``None``."""

    try:
        page_number = int(page) if page not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if page_number is None or page_number <= 0:
        return None
    return page_number


def _dispatch_to_main(callback: Callable[..., object], *args: object) -> None:
    from PyObjCTools import AppHelper

    AppHelper.callAfter(callback, *args)


def _is_main_thread() -> bool:
    from Foundation import NSThread

    return bool(NSThread.isMainThread())


class MacOSPDFViewer:
    """Own and reuse one native PDFKit reader window."""

    def __init__(self) -> None:
        self._controller: object | None = None
        self._closing = False

    def open(self, path: Path, page: object = None) -> dict[str, int]:
        if sys.platform != "darwin":
            raise RuntimeError("PDFKit 阅读器仅支持 macOS。")
        if self._closing:
            raise RuntimeError("应用正在退出，无法打开 PDF。")

        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise FileNotFoundError(f"PDF 文件不存在：{target}")
        if target.suffix.lower() != ".pdf":
            raise ValueError("PDFKit 阅读器只能打开 PDF 文件。")

        page_number = normalize_page_number(page)
        if _is_main_thread():
            return self._open_on_main(str(target), page_number)

        completed = threading.Event()
        outcome_lock = threading.Lock()
        outcome: dict[str, object] = {"status": "pending"}
        _dispatch_to_main(
            self._open_with_result_on_main,
            str(target),
            page_number,
            completed,
            outcome_lock,
            outcome,
        )
        if not completed.wait(MAIN_THREAD_OPEN_TIMEOUT_SECONDS):
            with outcome_lock:
                timed_out = outcome.get("status") == "pending"
                if timed_out:
                    outcome["status"] = "cancelled"
            if timed_out:
                raise TimeoutError("PDFKit 阅读器启动超时，已改用 macOS 预览。")
        with outcome_lock:
            error = outcome.get("error")
            result = outcome.get("result")
        if isinstance(error, BaseException):
            raise error
        if not isinstance(result, dict):
            raise RuntimeError("PDFKit 阅读器没有返回打开结果。")
        return result  # type: ignore[return-value]

    def close(self) -> None:
        """Close the auxiliary window while the Cocoa run loop is still alive."""

        self._closing = True
        if self._controller is None or sys.platform != "darwin":
            return
        try:
            if _is_main_thread():
                self._close_on_main()
            else:
                _dispatch_to_main(self._close_on_main)
        except Exception:
            logging.exception("failed to close the PDFKit reader")

    def _open_with_result_on_main(
        self,
        path: str,
        page_number: Optional[int],
        completed: threading.Event,
        outcome_lock: threading.Lock,
        outcome: dict[str, object],
    ) -> None:
        close_cancelled_window = False
        try:
            result = self._open_on_main(path, page_number)
            with outcome_lock:
                if outcome.get("status") == "cancelled":
                    close_cancelled_window = True
                else:
                    outcome["result"] = result
                    outcome["status"] = "completed"
        except Exception as exc:
            with outcome_lock:
                if outcome.get("status") == "cancelled":
                    close_cancelled_window = True
                else:
                    outcome["error"] = exc
                    outcome["status"] = "failed"
        finally:
            try:
                if close_cancelled_window:
                    # Keep the controller object/class alive so a later request
                    # can reopen the reader after the timeout fallback.
                    self._close_window_on_main()
            finally:
                completed.set()

    def _open_on_main(
        self,
        path: str,
        page_number: Optional[int],
    ) -> dict[str, int]:
        if self._closing:
            raise RuntimeError("应用正在退出，无法打开 PDF。")
        if self._controller is None:
            self._controller = _make_pdf_viewer_controller()
        return self._controller.open_document(path, page_number)

    def _close_on_main(self) -> None:
        self._close_window_on_main()
        self._controller = None

    def _close_window_on_main(self) -> None:
        controller = self._controller
        if controller is not None:
            controller.close_window()

def _make_pdf_viewer_controller() -> object:
    """Create the Objective-C controller class lazily on the Cocoa main thread."""

    global _PDF_VIEWER_CONTROLLER_CLASS

    import AppKit
    import objc
    import Quartz
    from Quartz.PDFKit import PDFDocument, PDFView

    if _PDF_VIEWER_CONTROLLER_CLASS is not None:
        return _PDF_VIEWER_CONTROLLER_CLASS.alloc().init()

    toolbar_height = 48.0

    class MEFinderPDFViewerController(AppKit.NSObject):
        def init(self):
            self = objc.super(MEFinderPDFViewerController, self).init()
            if self is None:
                return None
            self.window = None
            self.pdf_view = None
            self.document = None
            self.page_field = None
            self.page_total_label = None
            self.previous_button = None
            self.next_button = None
            return self

        @objc.python_method
        def _button(self, title, frame, action):
            button = AppKit.NSButton.alloc().initWithFrame_(frame)
            button.setTitle_(title)
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setTarget_(self)
            button.setAction_(action)
            return button

        @objc.python_method
        def _ensure_window(self):
            if self.window is not None:
                return

            frame = AppKit.NSMakeRect(0.0, 0.0, 1040.0, 780.0)
            style = (
                AppKit.NSWindowStyleMaskTitled
                | AppKit.NSWindowStyleMaskClosable
                | AppKit.NSWindowStyleMaskMiniaturizable
                | AppKit.NSWindowStyleMaskResizable
            )
            self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame,
                style,
                AppKit.NSBackingStoreBuffered,
                False,
            )
            self.window.setReleasedWhenClosed_(False)
            self.window.setDelegate_(self)
            self.window.setMinSize_(AppKit.NSMakeSize(720.0, 520.0))
            self.window.setFrameAutosaveName_("MEFinderPDFReaderWindow")

            content = AppKit.NSView.alloc().initWithFrame_(frame)
            content.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
            )
            self.window.setContentView_(content)

            self.pdf_view = PDFView.alloc().initWithFrame_(
                AppKit.NSMakeRect(
                    0.0,
                    0.0,
                    frame.size.width,
                    frame.size.height - toolbar_height,
                )
            )
            self.pdf_view.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
            )
            self.pdf_view.setDisplayMode_(Quartz.kPDFDisplaySinglePageContinuous)
            self.pdf_view.setDisplayDirection_(Quartz.kPDFDisplayDirectionVertical)
            self.pdf_view.setDisplaysPageBreaks_(True)
            self.pdf_view.setAutoScales_(True)
            content.addSubview_(self.pdf_view)

            toolbar = AppKit.NSView.alloc().initWithFrame_(
                AppKit.NSMakeRect(
                    0.0,
                    frame.size.height - toolbar_height,
                    frame.size.width,
                    toolbar_height,
                )
            )
            toolbar.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewMinYMargin
            )
            content.addSubview_(toolbar)

            separator = AppKit.NSBox.alloc().initWithFrame_(
                AppKit.NSMakeRect(0.0, 0.0, frame.size.width, 1.0)
            )
            separator.setBoxType_(AppKit.NSBoxSeparator)
            separator.setAutoresizingMask_(AppKit.NSViewWidthSizable)
            toolbar.addSubview_(separator)

            self.previous_button = self._button(
                "‹",
                AppKit.NSMakeRect(14.0, 9.0, 34.0, 30.0),
                "previousPage:",
            )
            toolbar.addSubview_(self.previous_button)
            self.next_button = self._button(
                "›",
                AppKit.NSMakeRect(50.0, 9.0, 34.0, 30.0),
                "nextPage:",
            )
            toolbar.addSubview_(self.next_button)

            self.page_field = AppKit.NSTextField.alloc().initWithFrame_(
                AppKit.NSMakeRect(96.0, 12.0, 54.0, 24.0)
            )
            self.page_field.setAlignment_(AppKit.NSTextAlignmentCenter)
            self.page_field.setTarget_(self)
            self.page_field.setAction_("pageFieldChanged:")
            toolbar.addSubview_(self.page_field)

            self.page_total_label = AppKit.NSTextField.labelWithString_("/ 0")
            self.page_total_label.setFrame_(
                AppKit.NSMakeRect(156.0, 15.0, 66.0, 20.0)
            )
            self.page_total_label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            toolbar.addSubview_(self.page_total_label)

            zoom_out = self._button(
                "−",
                AppKit.NSMakeRect(232.0, 9.0, 34.0, 30.0),
                "zoomOut:",
            )
            toolbar.addSubview_(zoom_out)
            fit = self._button(
                "适合窗口",
                AppKit.NSMakeRect(268.0, 9.0, 80.0, 30.0),
                "fitWindow:",
            )
            toolbar.addSubview_(fit)
            zoom_in = self._button(
                "+",
                AppKit.NSMakeRect(350.0, 9.0, 34.0, 30.0),
                "zoomIn:",
            )
            toolbar.addSubview_(zoom_in)

            AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self,
                "pageChanged:",
                Quartz.PDFViewPageChangedNotification,
                self.pdf_view,
            )

        @objc.python_method
        def open_document(self, path, page_number):
            self._ensure_window()
            url = AppKit.NSURL.fileURLWithPath_(path)
            document = PDFDocument.alloc().initWithURL_(url)
            if document is None:
                raise ValueError("文件不是有效的 PDF，或文件已经损坏。")
            if document.isLocked():
                raise ValueError("PDF 已加密，当前轻量阅读器无法解锁。")
            page_count = int(document.pageCount())
            if page_count <= 0:
                raise ValueError("PDF 中没有可显示的页面。")

            self.document = document
            self.pdf_view.setDocument_(document)
            self.pdf_view.setAutoScales_(True)
            self.window.setTitle_(
                f"{Path(path).name} — 文献原句定位器"
            )
            self.page_total_label.setStringValue_(f"/ {page_count}")

            requested = page_number or 1
            actual_page = self._go_to_page_number(requested)
            self.window.makeKeyAndOrderFront_(None)
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            return {"page": actual_page, "page_count": page_count}

        @objc.python_method
        def _go_to_page_number(self, page_number):
            if self.document is None:
                return
            page_count = int(self.document.pageCount())
            actual = min(max(int(page_number), 1), page_count)
            page = self.document.pageAtIndex_(actual - 1)
            if page is not None:
                self.pdf_view.goToPage_(page)
            self._update_page_controls()
            return actual

        @objc.python_method
        def _update_page_controls(self):
            if self.document is None or self.pdf_view is None:
                return
            current_page = self.pdf_view.currentPage()
            if current_page is None:
                current_index = 0
            else:
                current_index = int(self.document.indexForPage_(current_page))
            self.page_field.setStringValue_(str(current_index + 1))
            self.page_total_label.setStringValue_(
                f"/ {int(self.document.pageCount())}"
            )
            self.previous_button.setEnabled_(bool(self.pdf_view.canGoToPreviousPage()))
            self.next_button.setEnabled_(bool(self.pdf_view.canGoToNextPage()))

        def previousPage_(self, sender):
            self.pdf_view.goToPreviousPage_(sender)
            self._update_page_controls()

        def nextPage_(self, sender):
            self.pdf_view.goToNextPage_(sender)
            self._update_page_controls()

        def pageFieldChanged_(self, sender):
            try:
                page_number = int(str(sender.stringValue()).strip())
            except (TypeError, ValueError):
                AppKit.NSBeep()
                self._update_page_controls()
                return
            self._go_to_page_number(page_number)

        def zoomOut_(self, sender):
            self.pdf_view.setAutoScales_(False)
            self.pdf_view.zoomOut_(sender)

        def zoomIn_(self, sender):
            self.pdf_view.setAutoScales_(False)
            self.pdf_view.zoomIn_(sender)

        def fitWindow_(self, sender):
            self.pdf_view.setAutoScales_(True)

        def pageChanged_(self, notification):
            self._update_page_controls()

        def windowWillClose_(self, notification):
            if self.pdf_view is not None:
                AppKit.NSNotificationCenter.defaultCenter().removeObserver_name_object_(
                    self,
                    Quartz.PDFViewPageChangedNotification,
                    self.pdf_view,
                )
            self.window = None
            self.pdf_view = None
            self.document = None
            self.page_field = None
            self.page_total_label = None
            self.previous_button = None
            self.next_button = None

        @objc.python_method
        def close_window(self):
            if self.window is not None:
                self.window.close()

    _PDF_VIEWER_CONTROLLER_CLASS = MEFinderPDFViewerController
    return _PDF_VIEWER_CONTROLLER_CLASS.alloc().init()
