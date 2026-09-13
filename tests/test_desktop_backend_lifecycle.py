"""Desktop backend lifecycle is independently testable.

The desktop entry used to inline the backend start/stop sequence inside the
window bootstrap; these tests pin the exact contract with fake handlers and
servers — no pywebview, no real HTTP, no platform APIs.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from src.me_finder.desktop_backend import DesktopBackend


class FakeHandler:
    def __init__(self, calls: list, *, fail_close_runtime: bool = False) -> None:
        self._calls = calls
        self._fail_close_runtime = fail_close_runtime

    def begin_shutdown(self) -> None:
        self._calls.append("handler.begin_shutdown")

    def wait_for_durable_operations(self) -> None:
        self._calls.append("handler.wait_for_durable_operations")

    def close_runtime(self) -> bool:
        self._calls.append("handler.close_runtime")
        return not self._fail_close_runtime


class FakeServer:
    def __init__(self, calls: list, *, handlers_hang: bool = False) -> None:
        self._calls = calls
        self._handlers_hang = handlers_hang
        self.server_address = ("127.0.0.1", 45678)

    def serve_forever(self) -> None:  # pragma: no cover - never started in fakes
        pass

    def shutdown(self) -> None:
        self._calls.append("server.shutdown")

    def server_close(self) -> None:
        self._calls.append("server.server_close")

    def wait_for_handlers(self, timeout: float) -> bool:
        self._calls.append(("server.wait_for_handlers", timeout))
        return not self._handlers_hang


class DesktopBackendLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def build_backend(self, calls: list, **kwargs) -> tuple[DesktopBackend, dict]:
        handler = FakeHandler(
            calls, fail_close_runtime=kwargs.pop("fail_close_runtime", False)
        )
        server = FakeServer(calls, handlers_hang=kwargs.pop("handlers_hang", False))
        created: dict = {}
        on_create = kwargs.pop("on_create", None)

        def create_handler() -> FakeHandler:
            created["handler"] = handler
            if on_create:
                on_create()
            return handler

        def create_server(handler_obj: FakeHandler) -> FakeServer:
            created["server"] = server
            return server

        index = self.root / "index.sqlite3"
        if kwargs.pop("with_index", True):
            index.write_bytes(b"sqlite")
        backend = DesktopBackend(
            index_path=index,
            create_handler=create_handler,
            create_server=create_server,
            **kwargs,
        )
        return backend, created

    def start(
        self,
        backend: DesktopBackend,
        *,
        published: list | None = None,
        errors: list | None = None,
    ) -> bool:
        published_list = published if published is not None else []
        errors_list = errors if errors is not None else []
        return backend.start(
            on_ready=published_list.append,
            load_main_page=published_list.append,
            show_error=lambda title, detail: errors_list.append((title, detail)),
        )

    def test_start_publishes_url_and_stop_runs_full_sequence(self) -> None:
        calls: list = []
        backend, _created = self.build_backend(calls)
        published: list[str] = []

        started = self.start(backend, published=published)
        self.assertTrue(started)
        self.assertEqual(len(published), 2)
        self.assertTrue(published[0].startswith("http://127.0.0.1:"))
        self.assertEqual(published[0], published[1])

        report = backend.stop()
        self.assertTrue(report.handlers_stopped)
        self.assertTrue(report.runtime_closed)
        self.assertEqual(
            calls,
            [
                "handler.begin_shutdown",
                "server.shutdown",
                "server.server_close",
                ("server.wait_for_handlers", 2.0),
                "handler.wait_for_durable_operations",
                "handler.close_runtime",
            ],
        )

    def test_stop_before_start_leaves_no_backend(self) -> None:
        calls: list = []
        backend, created = self.build_backend(calls)
        backend.mark_closing()
        published: list[str] = []

        started = self.start(backend, published=published)
        self.assertFalse(started)
        self.assertEqual(published, [])
        self.assertEqual(calls, [])
        self.assertNotIn("handler", created)

    def test_closing_during_start_tears_the_fresh_handler_down(self) -> None:
        calls: list = []
        released = threading.Event()

        def close_during_start() -> None:
            # The window closed while make_handler was building the runtime.
            backend.mark_closing()
            released.set()

        backend, _created = self.build_backend(calls, on_create=close_during_start)
        published: list[str] = []

        started = self.start(backend, published=published)
        self.assertTrue(released.wait(5))
        self.assertFalse(started)
        self.assertEqual(published, [])
        self.assertEqual(
            calls,
            [
                "handler.begin_shutdown",
                "server.server_close",
                "handler.close_runtime",
            ],
        )

    def test_start_failure_cleans_up_and_renders_error(self) -> None:
        calls: list = []
        errors: list[tuple[str, str]] = []
        (self.root / "index.sqlite3").write_bytes(b"sqlite")
        with mock.patch.object(
            DesktopBackend,
            "_default_server",
            staticmethod(lambda handler: FakeServer(calls)),
        ):
            backend = DesktopBackend(
                index_path=self.root / "index.sqlite3",
                create_handler=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            )
            started = self.start(backend, errors=errors)
        self.assertFalse(started)
        self.assertEqual(errors[0][0], "后台启动失败")
        self.assertIn("RuntimeError: boom", errors[0][1])
        # The handler factory failed before any server existed: nothing to clean.
        self.assertEqual(calls, [])

    def test_missing_index_shows_bootstrap_error_without_a_handler(self) -> None:
        calls: list = []
        backend, created = self.build_backend(calls, with_index=False)
        errors: list[tuple[str, str]] = []

        started = self.start(backend, errors=errors)
        self.assertFalse(started)
        self.assertEqual(errors[0][0], "未找到索引数据库 data/index.sqlite3")
        self.assertIn("build-index", errors[0][1])
        self.assertNotIn("handler", created)

    def test_stop_waits_for_durable_operations_before_closing_runtime(self) -> None:
        calls: list = []
        backend, _created = self.build_backend(calls)
        self.start(backend)
        calls.clear()
        report = backend.stop()
        self.assertTrue(report.handlers_stopped)
        self.assertTrue(report.runtime_closed)
        # A started config+SQLite mutation must reach commit or rollback
        # before the process disappears.
        self.assertLess(
            calls.index("handler.wait_for_durable_operations"),
            calls.index("handler.close_runtime"),
        )

    def test_stop_never_closes_runtime_while_handlers_hang(self) -> None:
        calls: list = []
        backend, _created = self.build_backend(calls, handlers_hang=True)
        self.start(backend)
        calls.clear()
        with mock.patch("src.me_finder.desktop_backend.logging.warning") as warning:
            report = backend.stop()
        self.assertFalse(report.handlers_stopped)
        self.assertFalse(report.runtime_closed)
        self.assertNotIn("handler.close_runtime", calls)
        warning.assert_called_once_with(
            "active backend requests did not finish before desktop exit"
        )

    def test_stop_reports_unclosed_runtime(self) -> None:
        calls: list = []
        backend, _created = self.build_backend(calls, fail_close_runtime=True)
        self.start(backend)
        calls.clear()
        with mock.patch("src.me_finder.desktop_backend.logging.warning") as warning:
            report = backend.stop()
        self.assertTrue(report.handlers_stopped)
        self.assertFalse(report.runtime_closed)
        warning.assert_called_once_with(
            "backend workers did not finish before desktop exit"
        )


if __name__ == "__main__":
    unittest.main()
