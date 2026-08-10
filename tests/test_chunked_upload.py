from __future__ import annotations

import io
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from src.me_finder.chunked_upload import ChunkedUploadError, ChunkedUploadStore
from src.me_finder.database import build_database
from src.me_finder.web import make_handler


class ChunkedUploadStoreTests(unittest.TestCase):
    def test_sequential_chunks_are_verified_and_returned_as_one_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = ChunkedUploadStore(
                Path(temp_dir) / "staging",
                max_upload_bytes=32,
                chunk_bytes=4,
                max_chunk_bytes=8,
            )
            started = store.start(
                "通典.pdf",
                7,
                metadata={"parse_mode": "auto"},
            )
            upload_id = str(started["upload_id"])

            first = store.append(upload_id, 0, 4, io.BytesIO(b"abcd"))
            second = store.append(upload_id, 4, 3, io.BytesIO(b"efg"))
            completed = store.finish(upload_id)

            self.assertTrue(first["first_chunk"])
            self.assertFalse(first["complete"])
            self.assertTrue(second["complete"])
            self.assertEqual(completed.filename, "通典.pdf")
            self.assertEqual(completed.temp_path.read_bytes(), b"abcdefg")
            completed.temp_path.unlink()

    def test_out_of_order_and_incomplete_chunks_do_not_advance_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = ChunkedUploadStore(
                Path(temp_dir) / "staging",
                max_upload_bytes=32,
                chunk_bytes=4,
                max_chunk_bytes=8,
            )
            upload_id = str(store.start("paper.pdf", 6)["upload_id"])

            with self.assertRaises(ChunkedUploadError) as out_of_order:
                store.append(upload_id, 2, 2, io.BytesIO(b"cd"))
            self.assertEqual(out_of_order.exception.status, 409)

            with self.assertRaisesRegex(ChunkedUploadError, "不完整"):
                store.append(upload_id, 0, 4, io.BytesIO(b"ab"))

            stored = store.append(upload_id, 0, 4, io.BytesIO(b"abcd"))
            self.assertEqual(stored["received_size"], 4)
            self.assertTrue(store.cancel(upload_id))
            self.assertEqual(list((Path(temp_dir) / "staging").glob("*.part")), [])

    def test_close_removes_incomplete_uploads(self) -> None:
        with TemporaryDirectory() as temp_dir:
            staging = Path(temp_dir) / "staging"
            store = ChunkedUploadStore(staging, max_upload_bytes=32)
            store.start("paper.pdf", 6)
            self.assertEqual(len(list(staging.glob("*.part"))), 1)
            store.close()
            self.assertEqual(list(staging.glob("*.part")), [])


class ChunkedUploadHTTPTests(unittest.TestCase):
    @staticmethod
    def _open_json(request: Request) -> tuple[int, dict[str, object]]:
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    @classmethod
    def _post_json(
        cls,
        base_url: str,
        path: str,
        payload: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return cls._open_json(
            Request(
                base_url + path,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )

    @classmethod
    def _post_chunk(
        cls,
        base_url: str,
        upload_id: str,
        offset: int,
        body: bytes,
    ) -> tuple[int, dict[str, object]]:
        return cls._open_json(
            Request(
                base_url + "/api/import-upload/chunk",
                data=body,
                headers={
                    "Content-Type": "application/pdf",
                    "X-Upload-ID": upload_id,
                    "X-Upload-Offset": str(offset),
                },
                method="POST",
            )
        )

    def test_http_chunks_finalize_through_the_existing_import_pipeline(self) -> None:
        payload = b"%PDF-1.4\nchunked-upload-test\n%%EOF\n"
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            (root / "config" / "pdf_imports.json").write_text(
                '{"documents": []}',
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            server = None
            handler = None
            with (
                patch(
                    "src.me_finder.web.detect_imported_pdf",
                    return_value={
                        "detected_pdf_type": "scanned",
                        "pdf_page_count": 1,
                    },
                ),
                patch("src.me_finder.web.ImportTaskQueue.submit", return_value=None),
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    status, started = self._post_json(
                        base_url,
                        "/api/import-upload/start",
                        {
                            "file_name": "通典.pdf",
                            "size": len(payload),
                            "parse_mode": "auto",
                            "provider_id": "",
                        },
                    )
                    self.assertEqual(status, 200)
                    upload_id = str(started["upload_id"])
                    split = 11
                    first_status, first = self._post_chunk(
                        base_url,
                        upload_id,
                        0,
                        payload[:split],
                    )
                    second_status, second = self._post_chunk(
                        base_url,
                        upload_id,
                        split,
                        payload[split:],
                    )
                    finish_status, finished = self._post_json(
                        base_url,
                        "/api/import-upload/finish",
                        {"upload_id": upload_id},
                    )

                    self.assertEqual(first_status, 200)
                    self.assertFalse(first["complete"])
                    self.assertEqual(second_status, 200)
                    self.assertTrue(second["complete"])
                    self.assertEqual(finish_status, 200)
                    self.assertEqual(finished["detected_pdf_type"], "scanned")
                    self.assertEqual(finished["parse_route"], "mineru")
                    stored = list((root / "corpus" / "raw_pdf").glob("*.pdf"))
                    self.assertEqual(len(stored), 1)
                    self.assertEqual(stored[0].read_bytes(), payload)
                    self.assertEqual(
                        list((root / "corpus" / ".upload-staging").glob("*.part")),
                        [],
                    )
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
