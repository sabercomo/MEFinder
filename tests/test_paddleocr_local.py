from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from src.me_finder.components.local_ocr_installer import LOCAL_OCR_MANIFEST_FILE
from src.me_finder.components.paddleocr import ManagedPaddleOCR
from src.me_finder.errors import UserFacingError
from src.me_finder.parsers.paddleocr_local import (
    PADDLE_RUNTIME_LOCK, PaddleOCRProvider, normalize_paddle_pages, paddle_service_url,
)
from src.me_finder.parsers.provider import ParserProviderError, ParserRequest


class PaddleOCRTests(unittest.TestCase):
    def test_uninstall_preserves_runtime_while_import_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            component = ManagedPaddleOCR(Path(temp), LOCAL_OCR_MANIFEST_FILE)
            models = component.runtime / "cpu"
            models.mkdir(parents=True)
            with PADDLE_RUNTIME_LOCK, self.assertRaisesRegex(UserFacingError, "正在解析文献"):
                component.perform({"action": "uninstall"})
            self.assertTrue(models.is_dir())
            component.perform({"action": "uninstall"})
            self.assertFalse(models.exists())

    def test_installer_is_opt_in_and_shutdown_cancels_its_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            component = ManagedPaddleOCR(Path(temp), LOCAL_OCR_MANIFEST_FILE)
            self.assertFalse(component.runtime.exists())
            self.assertFalse(component.summary()["enabled"])
            started = threading.Event()

            def install() -> None:
                started.set()
                component.state.cancel_event.wait()

            with mock.patch.object(component, "_install", side_effect=install), mock.patch(
                "src.me_finder.components.paddleocr.current_platform_key", return_value="win32-x86_64",
            ):
                component.perform({"action": "install"})
                try:
                    self.assertTrue(started.wait(timeout=1))
                    self.assertTrue(component.close(timeout=1))
                    self.assertTrue(component.state.cancel_event.is_set())
                finally:
                    component.state.cancel_event.set()
                    component.state.thread.join()

    def test_official_service_uses_pdf_slices_and_preserves_physical_pages(self) -> None:
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                response = {"errorCode": 0, "result": {"layoutParsingResults": [
                    {"prunedResult": {"parsing_res_list": blocks}}
                    for blocks in [
                        [{"block_label": "formula", "block_content": r"x^{2}"}], [],
                    ]
                ]}}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "slice.pdf"
            source.write_bytes(b"%PDF")
            provider = PaddleOCRProvider(endpoint=f"http://127.0.0.1:{server.server_port}")
            request = ParserRequest(source, "test", "doc", 1, 2, 12)
            result = provider.normalize_result(provider.submit(request).raw_result, request)
        self.assertEqual(requests[0][0], "/layout-parsing")
        self.assertEqual(requests[0][1]["fileType"], 0)
        self.assertEqual([page.physical_pdf_page for page in result.pages], [13, 14])
        self.assertEqual(result.pages[0].text, "$$\nx^{2}\n$$")
        self.assertEqual(result.pages[1].text, "")

    def test_rejects_incomplete_results_instead_of_guessing_page_alignment(self) -> None:
        with self.assertRaises(ParserProviderError):
            normalize_paddle_pages([{"page_index": 0, "parsing_res_list": []}], 2, 0)
        with self.assertRaises(ParserProviderError):
            normalize_paddle_pages([{"page_index": 2, "parsing_res_list": []}], 1, 0)
        with self.assertRaises(ParserProviderError):
            normalize_paddle_pages([{"page_index": 0, "parsing_res_list": []}, {"parsing_res_list": []}], 2, 0)

    def test_address_boundary(self) -> None:
        self.assertEqual(paddle_service_url("http://192.168.1.6:8080"), "http://192.168.1.6:8080/layout-parsing")
        for endpoint in ("https://example.org", "http://169.254.169.254", "http://localhost@evil.org", "http://127.0.0.1/a"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                paddle_service_url(endpoint)
