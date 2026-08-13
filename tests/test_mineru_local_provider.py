import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.me_finder.mineru_local_provider import (
    MinerULocalConfig,
    MinerULocalProvider,
)
from src.me_finder.mineru_provider import MinerUCloudProvider
from src.me_finder.large_document.slicing import SlicePlanner
from src.me_finder.parser_provider import (
    ParserProviderError,
    ParserRequest,
    ParserTaskStatus,
)


CONTENT = [
    {"page_idx": 0, "type": "text", "text": "第一頁", "bbox": [1, 2, 3, 4]},
    {"page_idx": 1, "type": "text", "text": "第二页"},
]


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class FakeMinerUHandler(BaseHTTPRequestHandler):
    health_ok = True
    task_missing = False
    malformed_json = False
    malformed_result = False
    delay_seconds = 0.0
    submitted_body = b""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.path == "/health":
            if self.health_ok:
                self._json(200, {"protocol_version": "1", "max_concurrent_requests": 2})
            else:
                self._json(503, {"error": "unhealthy"})
            return
        if self.path == "/tasks/local-1":
            if self.task_missing:
                self._json(404, {"detail": "task not found"})
            else:
                self._json(200, {"task_id": "local-1", "status": "completed"})
            return
        if self.path == "/tasks/local-1/result":
            if self.task_missing:
                self._json(404, {"detail": "task not found"})
            elif self.malformed_json:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "9")
                self.end_headers()
                self.wfile.write(b"{not-json")
            elif self.malformed_result:
                self._json(200, {"results": {"slice.pdf": {"md": "only markdown"}}})
            else:
                self._json(200, {"results": {"slice.pdf": {"content_list": CONTENT}}})
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self):
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.path != "/tasks":
            self._json(404, {"detail": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        type(self).submitted_body = self.rfile.read(length)
        self._json(202, {"task_id": "local-1", "status": "queued", "queued_ahead": 0})

    def _json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeMinerUService:
    def __enter__(self):
        for name, value in (
            ("health_ok", True),
            ("task_missing", False),
            ("malformed_json", False),
            ("malformed_result", False),
            ("delay_seconds", 0.0),
            ("submitted_body", b""),
        ):
            setattr(FakeMinerUHandler, name, value)
        self.server = QuietServer(("127.0.0.1", 0), FakeMinerUHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.endpoint = f"http://{host}:{port}"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class MinerULocalProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "slice.pdf"
        self.path.write_bytes(b"%PDF-local-test")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self):
        return ParserRequest(
            source_path=self.path,
            source_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
            document_id="doc-slice",
            page_start=1,
            page_end=2,
            global_page_offset=40,
        )

    def test_health_ok_and_fail(self) -> None:
        with FakeMinerUService() as service:
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            self.assertTrue(provider.health()["ok"])
            FakeMinerUHandler.health_ok = False
            with self.assertRaises(ParserProviderError) as caught:
                provider.health()
            self.assertEqual(caught.exception.status_code, 503)

    def test_submit_poll_fetch_uses_official_async_endpoints(self) -> None:
        with FakeMinerUService() as service:
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            submission = provider.submit(self.request())
            self.assertEqual(submission.remote_task_id, "local-1")
            self.assertEqual(submission.status, ParserTaskStatus.SUBMITTED)
            body = FakeMinerUHandler.submitted_body
            self.assertIn(b'name="files"; filename="slice.pdf"', body)
            self.assertIn(self.path.read_bytes(), body)
            self.assertIn(b'name="return_content_list"', body)
            self.assertEqual(provider.poll("local-1").status, ParserTaskStatus.COMPLETED)
            normalized = provider.normalize_result(
                provider.fetch_result(submission, self.request()), self.request()
            )
            self.assertEqual([p.physical_pdf_page for p in normalized.pages], [41, 42])
            self.assertEqual(normalized.pages[0].blocks[0].bbox, (1, 2, 3, 4))

    def test_service_restart_task_missing_is_explicit_and_reconnects(self) -> None:
        with FakeMinerUService() as service:
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            FakeMinerUHandler.task_missing = True
            with self.assertRaises(ParserProviderError) as caught:
                provider.poll("local-1")
            self.assertTrue(caught.exception.remote_task_missing)
            self.assertTrue(caught.exception.retryable)
            FakeMinerUHandler.task_missing = False
            self.assertEqual(provider.poll("local-1").status, ParserTaskStatus.COMPLETED)

    def test_local_normalization_matches_cloud_page_semantics(self) -> None:
        request = self.request()
        local = MinerULocalProvider(MinerULocalConfig()).normalize_result(
            {"results": {"slice.pdf": {"content_list": CONTENT}}}, request
        )
        cloud = MinerUCloudProvider(client=object()).normalize_result(
            {"content_list": CONTENT}, request
        )
        self.assertEqual(
            [(page.physical_pdf_page, page.text) for page in local.pages],
            [(page.physical_pdf_page, page.text) for page in cloud.pages],
        )

    def test_endpoint_timeout_is_retryable(self) -> None:
        with FakeMinerUService() as service:
            FakeMinerUHandler.delay_seconds = 0.15
            provider = MinerULocalProvider(
                MinerULocalConfig(endpoint=service.endpoint, timeout_seconds=0.03)
            )
            with self.assertRaises(ParserProviderError) as caught:
                provider.health()
            self.assertTrue(caught.exception.retryable)

    def test_malformed_json_and_result_are_rejected(self) -> None:
        with FakeMinerUService() as service:
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            submission = provider.submit(self.request())
            FakeMinerUHandler.malformed_json = True
            with self.assertRaisesRegex(ParserProviderError, "malformed JSON"):
                provider.fetch_result(submission, self.request())
            FakeMinerUHandler.malformed_json = False
            FakeMinerUHandler.malformed_result = True
            with self.assertRaisesRegex(ParserProviderError, "content_list"):
                provider.normalize_result(
                    provider.fetch_result(submission, self.request()), self.request()
                )

    def test_capability_is_config_driven(self) -> None:
        provider = MinerULocalProvider(
            MinerULocalConfig(
                max_pages_per_file=64,
                max_bytes_per_file=128 * 1024 * 1024,
                max_concurrency=3,
            )
        )
        capability = provider.capabilities()
        self.assertEqual(capability.max_pages_per_file, 64)
        self.assertEqual(capability.max_bytes_per_file, 128 * 1024 * 1024)
        self.assertEqual(capability.max_concurrency, 3)
        self.assertTrue(capability.supports_stream_upload)

    def test_default_capability_slices_a_3_5_gib_document(self) -> None:
        capability = MinerULocalProvider(MinerULocalConfig()).capabilities()
        ranges = SlicePlanner().plan(
            total_pages=1000,
            total_bytes=7 * 512 * 1024 * 1024,
            capabilities=capability,
        )

        self.assertGreater(len(ranges), 1)
        self.assertTrue(all(item.page_count <= 200 for item in ranges))
        self.assertTrue(
            all(item.estimated_bytes <= 200 * 1024 * 1024 for item in ranges)
        )


if __name__ == "__main__":
    unittest.main()
