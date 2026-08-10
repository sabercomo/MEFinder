from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from src.me_finder.app_context import AppContext
from src.me_finder.database import build_database
from src.me_finder.bibliographic_metadata import update_metadata_in_database
from src.me_finder.web import (
    MAX_JSON_REQUEST_BYTES,
    ManagedThreadingHTTPServer,
    make_handler,
)


class ApiRequestLimitTests(unittest.TestCase):
    @contextmanager
    def _server(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            index_path = root / "data" / "index.sqlite3"
            config_root = root / "config"
            config_root.mkdir(parents=True)
            build_database({"metadata": {}}, index_path)
            (config_root / "preferences.json").write_text(
                json.dumps({"theme": "midnight"}),
                encoding="utf-8",
            )
            context = AppContext.create(root, index_path=index_path)
            handler = make_handler(index_path, app_context=context)
            handler.log_message = lambda *_args: None
            self._handler = handler
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}"
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

    def test_handler_uses_explicit_runtime_root_after_cwd_changes(self) -> None:
        with self._server() as base_url, tempfile.TemporaryDirectory() as other_dir:
            previous = Path.cwd()
            try:
                os.chdir(other_dir)
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("ME_FINDER_PREFERENCES", None)
                    response = build_opener(ProxyHandler({})).open(
                        base_url + "/api/preferences", timeout=5
                    )
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                os.chdir(previous)

        self.assertEqual(payload["theme"], "midnight")

    def test_non_upload_api_rejects_unbounded_json_body(self) -> None:
        with self._server() as base_url:
            port = int(base_url.rsplit(":", 1)[1])
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                "POST",
                "/api/search",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(MAX_JSON_REQUEST_BYTES + 1),
                },
            )
            response = connection.getresponse()
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()

        self.assertEqual(status, 413)
        self.assertEqual(payload["error"], "JSON 请求内容过大。")

    def test_new_post_is_rejected_with_503_once_shutdown_begins(self) -> None:
        def post_search(port: int):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(
                    "POST",
                    "/api/search",
                    body=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": "2",
                    },
                )
                response = connection.getresponse()
                return response.status, response.read().decode("utf-8")
            finally:
                connection.close()

        with self._server() as base_url:
            port = int(base_url.rsplit(":", 1)[1])

            # Before shutdown the same well-formed POST must be accepted, so the
            # 503 below is attributable to the closing state and nothing else.
            warm_status, _ = post_search(port)

            # Enter the closing/shutdown state and re-issue the POST.
            self._handler.begin_shutdown()
            status, body = post_search(port)
            payload = json.loads(body)

        self.assertNotEqual(warm_status, 503)
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "应用正在关闭。")

    def test_close_keeps_runtime_open_until_accepted_work_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index_path = root / "data" / "index.sqlite3"
            build_database({"metadata": {}}, index_path)
            handler = make_handler(
                index_path,
                app_context=AppContext.create(root, index_path=index_path),
            )
            started = threading.Event()
            release = threading.Event()

            def blocking_task() -> None:
                started.set()
                release.wait(timeout=2)

            handler._submit_background_task(blocking_task)
            self.assertTrue(started.wait(timeout=2))
            self.assertFalse(handler.close_runtime(timeout=0.01))
            release.set()
            self.assertTrue(handler.close_runtime(timeout=2))

    def test_managed_server_request_drain_is_bounded_and_observable(self) -> None:
        started = threading.Event()
        release = threading.Event()
        client_finished = threading.Event()

        class BlockingHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                started.set()
                release.wait(timeout=2)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args) -> None:
                return

        server = ManagedThreadingHTTPServer(("127.0.0.1", 0), BlockingHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def request() -> None:
            connection = HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            try:
                connection.request("GET", "/")
                connection.getresponse().read()
            finally:
                connection.close()
                client_finished.set()

        client_thread = threading.Thread(target=request, daemon=True)
        client_thread.start()
        try:
            self.assertTrue(started.wait(timeout=2))
            server.shutdown()
            server.server_close()
            self.assertFalse(server.wait_for_handlers(timeout=0.01))
            release.set()
            self.assertTrue(server.wait_for_handlers(timeout=2))
            self.assertTrue(client_finished.wait(timeout=2))
        finally:
            release.set()
            server.server_close()
            server_thread.join(timeout=2)
            client_thread.join(timeout=2)

    def test_shutdown_waits_for_metadata_consistency_region_before_exit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index_path = root / "data" / "index.sqlite3"
            config_path = root / "config" / "pdf_imports.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "documents": [
                            {
                                "source_file_id": "pdf-one",
                                "file_name": "one.pdf",
                                "title": "Old",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            build_database(
                {
                    "metadata": {},
                    "source_files": [
                        {
                            "source_file_id": "pdf-one",
                            "source_type": "pdf",
                            "file_name": "one.pdf",
                            "title": "Old",
                        }
                    ],
                },
                index_path,
            )
            handler = make_handler(
                index_path,
                app_context=AppContext.create(root, index_path=index_path),
            )
            handler.log_message = lambda *_args: None
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            mutation_started = threading.Event()
            release_mutation = threading.Event()
            request_finished = threading.Event()
            response_status: list[int] = []

            def blocked_update(*args, **kwargs):
                mutation_started.set()
                release_mutation.wait(timeout=2)
                return update_metadata_in_database(*args, **kwargs)

            body = json.dumps(
                {
                    "source_id": "pdf-one",
                    "metadata": {"title": "New"},
                }
            )

            def send_request() -> None:
                connection = HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=5
                )
                try:
                    connection.request(
                        "POST",
                        "/api/bibliographic-metadata/save",
                        body=body,
                        headers={
                            "Content-Type": "application/json",
                            "Content-Length": str(len(body.encode("utf-8"))),
                        },
                    )
                    response = connection.getresponse()
                    response_status.append(response.status)
                    response.read()
                finally:
                    connection.close()
                    request_finished.set()

            request_thread = threading.Thread(target=send_request, daemon=True)
            try:
                with patch(
                    "src.me_finder.web.update_metadata_in_database",
                    side_effect=blocked_update,
                ):
                    request_thread.start()
                    self.assertTrue(mutation_started.wait(timeout=2))
                    handler.begin_shutdown()
                    self.assertFalse(
                        handler.wait_for_durable_operations(timeout=0.01)
                    )
                    release_mutation.set()
                    self.assertTrue(
                        handler.wait_for_durable_operations(timeout=2)
                    )
                    self.assertTrue(request_finished.wait(timeout=2))
            finally:
                release_mutation.set()
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                server_thread.join(timeout=2)
                request_thread.join(timeout=2)

            stored_config = json.loads(config_path.read_text(encoding="utf-8"))
            database = sqlite3.connect(str(index_path))
            try:
                source_payload = json.loads(
                    database.execute(
                        "SELECT payload_json FROM source_files "
                        "WHERE source_file_id = 'pdf-one'"
                    ).fetchone()[0]
                )
            finally:
                database.close()

        self.assertEqual(response_status, [200])
        self.assertEqual(stored_config["documents"][0]["title"], "New")
        self.assertEqual(source_payload["title"], "New")


if __name__ == "__main__":
    unittest.main()
