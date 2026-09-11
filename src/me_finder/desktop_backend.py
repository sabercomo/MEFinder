"""Desktop backend lifecycle: start and stop the local HTTP backend.

Extracted from the desktop entry so the shutdown sequence is independently
testable. The module never imports pywebview and never touches platform APIs —
the window layer (``desktop.py``) stays responsible for windows, and the
business pages only ever see an ``http://127.0.0.1`` URL.
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

HandlerFactory = Callable[[], object]
"""Build the HTTP handler; may close over platform capability hooks."""

ServerFactory = Callable[[object], object]
"""Wrap one handler in a managed, bound server on 127.0.0.1."""

ErrorRenderer = Callable[[str, str], None]
"""Render a fatal bootstrap error page: ``(title, detail)``."""

ReadyPublisher = Callable[[str], None]
"""Hand the backend URL to the shell (reader windows, logging)."""

PageLoader = Callable[[str], None]
"""Point the main window at the backend URL."""


@dataclass(frozen=True)
class DesktopBackendStopReport:
    """Result of the graceful stop sequence."""

    handlers_stopped: bool
    runtime_closed: bool


class DesktopBackend:
    """Own backend start/stop with the historical closing-race semantics.

    ``start`` publishes nothing and tears the freshly built handler down again
    when the window already closed during startup; ``stop`` always runs
    ``begin_shutdown`` -> ``shutdown`` -> ``server_close`` -> wait for handlers
    -> wait for durable operations (a started config+SQLite mutation must
    reach commit or rollback) -> ``close_runtime`` only once handlers stopped.
    """

    def __init__(
        self,
        *,
        index_path: Path,
        create_handler: HandlerFactory,
        create_server: Optional[ServerFactory] = None,
        stop_timeout: float = 2.0,
        index_missing_detail: Optional[Callable[[], str]] = None,
    ) -> None:
        self._index_path = Path(index_path)
        self._create_handler = create_handler
        self._create_server = create_server or self._default_server
        self._stop_timeout = stop_timeout
        self._index_missing_detail = index_missing_detail or (
            self._default_index_missing_detail
        )
        self._lock = threading.Lock()
        self._closing = False
        self._handler: Optional[object] = None
        self._server: Optional[object] = None

    def _default_index_missing_detail(self) -> str:
        return (
            "请把 index.sqlite3 放到：\n%s\n\n"
            "索引数据库可在项目目录用命令生成：\n"
            "python -m src.me_finder build-index" % self._index_path
        )

    @staticmethod
    def _default_server(handler: object) -> object:
        from .web import ManagedThreadingHTTPServer

        return ManagedThreadingHTTPServer(("127.0.0.1", 0), handler)

    def mark_closing(self) -> None:
        """Seal against late starts once the window layer is going away."""

        with self._lock:
            self._closing = True

    def start(
        self,
        *,
        on_ready: ReadyPublisher,
        load_main_page: PageLoader,
        show_error: ErrorRenderer,
    ) -> bool:
        """Build and start the backend; return ``True`` when it is serving."""

        handler = None
        server = None
        server_started = False
        try:
            with self._lock:
                if self._closing:
                    return False
            if not self._index_path.exists():
                logging.error("index not found: %s", self._index_path)
                with self._lock:
                    closing = self._closing
                if not closing:
                    show_error(
                        "未找到索引数据库 data/index.sqlite3",
                        self._index_missing_detail(),
                    )
                return False
            logging.info("loading index from %s", self._index_path)
            handler = self._create_handler()
            server = self._create_server(handler)
            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            # BaseServer.shutdown() blocks until serve_forever() has entered
            # its loop.  Publish the pair only after Thread.start succeeds,
            # while serializing with the close path so an immediately closed
            # WebView cannot leave behind a late-starting backend.
            with self._lock:
                if self._closing:
                    handler.begin_shutdown()
                else:
                    server_thread.start()
                    server_started = True
                    self._handler = handler
                    self._server = server
            if not server_started:
                server.server_close()
                handler.close_runtime()
                return False
            port = int(server.server_address[1])
            url = "http://127.0.0.1:%d/" % port
            on_ready(url)
            logging.info("backend ready at %s", url)
            with self._lock:
                closing = self._closing
            if not closing:
                load_main_page(url)
            return True
        except Exception:
            logging.exception("backend failed to start")
            if not server_started:
                if handler is not None:
                    handler.begin_shutdown()
                if server is not None:
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
            with self._lock:
                closing = self._closing
            if not closing:
                show_error("后台启动失败", traceback.format_exc())
            return False

    def stop(self) -> DesktopBackendStopReport:
        """Run the graceful stop sequence and report what completed."""

        with self._lock:
            self._closing = True
            server = self._server
            handler = self._handler
        if handler is not None:
            handler.begin_shutdown()
        if server is not None:
            server.shutdown()
            server.server_close()
        handlers_stopped = server is None or server.wait_for_handlers(
            timeout=self._stop_timeout
        )
        if handler is not None:
            # A half-open request must not block Windows/WebView2 shutdown forever,
            # but a config+SQLite mutation that already started must reach either
            # commit or rollback before the process is allowed to disappear.
            handler.wait_for_durable_operations()
        if not handlers_stopped and server is not None:
            handlers_stopped = server.wait_for_handlers(timeout=self._stop_timeout)
        if not handlers_stopped:
            logging.warning("active backend requests did not finish before desktop exit")
        if handler is None:
            runtime_closed = True
        elif handlers_stopped:
            runtime_closed = bool(handler.close_runtime())
            if not runtime_closed:
                logging.warning("backend workers did not finish before desktop exit")
        else:
            # Handlers are still serving; the runtime was never closed.
            runtime_closed = False
        return DesktopBackendStopReport(
            handlers_stopped=handlers_stopped,
            runtime_closed=runtime_closed,
        )
