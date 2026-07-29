from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.me_finder.database import build_database
from src.me_finder.pdf_import_service import rebuild_local_index
from src.me_finder.preferences import save_preferences
from src.me_finder.web import make_handler


def write_test_docx(path: Path, body: str) -> None:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        f"<w:p><w:r><w:t>{escape(path.stem)}</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>{escape(body)}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)


class BatchDirectoryImportTests(unittest.TestCase):
    def test_two_local_documents_share_one_index_rebuild(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            first = source_dir / "第一份论文.docx"
            second = source_dir / "第二份论文.docx"
            write_test_docx(first, "第一份批量导入测试文献的唯一正文。")
            write_test_docx(second, "第二份批量导入测试文献的唯一正文。")
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            (root / "config" / "pdf_imports.json").write_text(
                '{"documents": []}',
                encoding="utf-8",
            )
            save_preferences(
                {"scan_directories": [str(source_dir)]},
                root / "config" / "preferences.json",
            )

            previous_cwd = Path.cwd()
            server = None
            with patch(
                "src.me_finder.web.rebuild_local_index",
                wraps=rebuild_local_index,
            ) as rebuild:
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    server_thread = threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    )
                    server_thread.start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(first), str(second)]},
                    )
                    self.assertEqual(len(response["jobs"]), 2)
                    job_ids = [str(job["job_id"]) for job in response["jobs"]]
                    statuses = self._wait_for_jobs(base_url, job_ids)

                    self.assertEqual(
                        [status["status"] for status in statuses],
                        ["completed", "completed"],
                    )
                    self.assertEqual(rebuild.call_count, 1)
                    connection = sqlite3.connect(root / "data" / "index.sqlite3")
                    try:
                        indexed_count = connection.execute(
                            "SELECT COUNT(*) FROM source_files WHERE source_type = 'word'"
                        ).fetchone()[0]
                    finally:
                        connection.close()
                    self.assertEqual(indexed_count, 2)
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_more_than_fifty_paths_is_rejected_instead_of_truncated(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            previous_cwd = Path.cwd()
            server = None
            try:
                os.chdir(root)
                handler = make_handler(root / "data" / "index.sqlite3")
                handler.log_message = lambda *_args: None
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base_url = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as caught:
                    self._post_json(
                        base_url + "/api/import-local",
                        {"paths": ["/not-used.docx"] * 51},
                    )
                self.assertEqual(caught.exception.code, 400)
                payload = json.loads(caught.exception.read().decode("utf-8"))
                self.assertIn("最多批量导入 50 个", payload["error"])
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                handler.close_runtime()
                os.chdir(previous_cwd)

    @staticmethod
    def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _wait_for_jobs(
        base_url: str,
        job_ids: list[str],
    ) -> list[dict[str, object]]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            statuses = []
            for job_id in job_ids:
                with urlopen(
                    base_url + "/api/import-status?job_id=" + job_id,
                    timeout=5,
                ) as response:
                    statuses.append(json.loads(response.read().decode("utf-8")))
            if all(
                status.get("status") in {"completed", "failed"}
                for status in statuses
            ):
                return statuses
            time.sleep(0.02)
        raise AssertionError("batch import jobs did not finish")


if __name__ == "__main__":
    unittest.main()
