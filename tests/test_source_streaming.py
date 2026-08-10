from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.me_finder.app_context import AppContext
from src.me_finder.database import build_database
from src.me_finder.http_range import ByteRange, InvalidByteRange, parse_byte_range
from src.me_finder.web import make_handler


class ByteRangeTests(unittest.TestCase):
    def test_parses_closed_open_and_suffix_ranges(self) -> None:
        self.assertEqual(parse_byte_range("bytes=2-5", 10), ByteRange(2, 5))
        self.assertEqual(parse_byte_range("bytes=7-", 10), ByteRange(7, 9))
        self.assertEqual(parse_byte_range("bytes=-4", 10), ByteRange(6, 9))
        self.assertEqual(parse_byte_range("bytes=2-99", 10), ByteRange(2, 9))

    def test_rejects_multiple_or_unsatisfiable_ranges(self) -> None:
        for value in ("bytes=1-2,4-5", "items=1-2", "bytes=10-", "bytes=-0"):
            with self.subTest(value=value), self.assertRaises(InvalidByteRange):
                parse_byte_range(value, 10)


class SourceStreamingTests(unittest.TestCase):
    @contextmanager
    def _server(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            source = root / "corpus" / "raw_pdf" / "sample.pdf"
            source.parent.mkdir(parents=True)
            content = bytes(range(256)) * 8
            source.write_bytes(content)
            index_path = root / "data" / "index.sqlite3"
            build_database(
                {
                    "metadata": {},
                    "source_files": [
                        {
                            "source_file_id": "pdf-stream",
                            "source_type": "pdf",
                            "file_name": source.name,
                            "relative_path": "corpus/raw_pdf/sample.pdf",
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
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield server.server_port, content
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

    @staticmethod
    def _request(port: int, method: str, *, range_header: str | None = None):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Range": range_header} if range_header else {}
        connection.request(method, "/source/pdf-stream", headers=headers)
        response = connection.getresponse()
        status = response.status
        response_headers = dict(response.getheaders())
        body = response.read()
        connection.close()
        return status, response_headers, body

    def test_get_streams_the_full_source_with_range_capability(self) -> None:
        with self._server() as (port, content):
            status, headers, body = self._request(port, "GET")

        self.assertEqual(status, 200)
        self.assertEqual(body, content)
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(int(headers["Content-Length"]), len(content))

    def test_get_returns_one_partial_range(self) -> None:
        with self._server() as (port, content):
            status, headers, body = self._request(
                port,
                "GET",
                range_header="bytes=100-199",
            )

        self.assertEqual(status, 206)
        self.assertEqual(body, content[100:200])
        self.assertEqual(headers["Content-Range"], f"bytes 100-199/{len(content)}")

    def test_invalid_range_returns_416_and_head_has_no_body(self) -> None:
        with self._server() as (port, content):
            invalid_status, invalid_headers, invalid_body = self._request(
                port,
                "GET",
                range_header=f"bytes={len(content)}-",
            )
            head_status, head_headers, head_body = self._request(port, "HEAD")

        self.assertEqual(invalid_status, 416)
        self.assertEqual(invalid_headers["Content-Range"], f"bytes */{len(content)}")
        self.assertEqual(invalid_body, b"")
        self.assertEqual(head_status, 200)
        self.assertEqual(int(head_headers["Content-Length"]), len(content))
        self.assertEqual(head_body, b"")


if __name__ == "__main__":
    unittest.main()
