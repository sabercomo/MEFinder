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

from src.me_finder import database as database_module
from src.me_finder.database import build_database
from src.me_finder.database import replace_source_in_database as real_replace_source
from src.me_finder.mineru_api import MinerUError
from src.me_finder.pdf_import_service import rebuild_local_index
from src.me_finder.preferences import save_preferences
from src.me_finder.search import SearchEngine as RealSearchEngine
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


def write_native_pdf(path: Path, marker: str) -> None:
    path.write_bytes(
        (
            "%PDF-1.4\n"
            f"% {marker} native PDF batch import regression fixture\n"
            "1 0 obj << /Type /Catalog >> endobj\n"
            "trailer << /Root 1 0 R >>\n"
            "%%EOF\n"
        ).encode("utf-8")
    )


def fake_native_extraction(
    path: Path,
    root: Path,
    config: dict[str, object],
    parsed_dir: Path | None = None,
) -> dict[str, list[dict[str, object]]]:
    del root, parsed_dir
    if path.name == "broken-native.pdf":
        raise ValueError("damaged PDF text layer")
    source_id = str(config["source_file_id"])
    document_id = str(config["document_id"])
    text = f"{path.stem} searchable native PDF text"
    return {
        "source_files": [
            {
                "source_file_id": source_id,
                "source_type": "pdf",
                "document_id": document_id,
                "file_name": path.name,
                "relative_path": f"corpus/raw_pdf/{path.name}",
            }
        ],
        "volumes": [
            {
                "volume_id": document_id,
                "source_file_id": source_id,
                "source_type": "pdf",
                "display_title": path.stem,
            }
        ],
        "works": [
            {
                "work_id": f"{document_id}-W0001",
                "volume_id": document_id,
                "source_type": "pdf",
                "title": path.stem,
            }
        ],
        "toc_entries": [],
        "paragraphs": [
            {
                "paragraph_id": f"{source_id}-P000001",
                "volume_id": document_id,
                "work_id": f"{document_id}-W0001",
                "source_file_id": source_id,
                "source_type": "pdf",
                "paragraph_index": 0,
                "volume_number": None,
                "volume_display": path.stem,
                "work_title": path.stem,
                "document_title": path.stem,
                "eligible_for_search": True,
                "text_raw": text,
                "normalized_text": text,
                "compact_text": text.replace(" ", ""),
                "plain_text": text.replace(" ", ""),
            }
        ],
        "page_anchors": [],
        "pdf_pages": [
            {
                "pdf_page_id": f"{source_id}-PAGE-000000",
                "source_file_id": source_id,
                "pdf_page_index": 0,
                "text_raw": text,
            }
        ],
        "pdf_page_mappings": [],
        "pdf_import_runs": [],
        "audit_issues": [],
    }


class BatchDirectoryImportTests(unittest.TestCase):
    def test_direct_upload_repairs_a_concatenated_pdf_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source = Path(temp_dir) / "paper.pdf"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            write_native_pdf(source, "CONCATENATED-CONFIG")
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            config_path = root / "config" / "pdf_imports.json"
            config_path.write_text(
                json.dumps(
                    {
                        "documents": [
                            {
                                "source_file_id": "stale-source",
                                "file_name": "stale.pdf",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
                + json.dumps({"documents": []}, indent=2)
                + "\n",
                encoding="utf-8",
            )

            previous_cwd = Path.cwd()
            server = None
            handler = None
            with patch(
                "src.me_finder.web.detect_imported_pdf",
                return_value={
                    "detected_pdf_type": "native_text",
                    "pdf_page_count": 1,
                },
            ), patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/import",
                        data=source.read_bytes(),
                        headers={
                            "Content-Type": "application/pdf",
                            "X-File-Name": source.name,
                        },
                        method="POST",
                    )
                    with urlopen(request, timeout=5) as response:
                        imported = json.loads(response.read().decode("utf-8"))
                    status = self._wait_for_jobs(
                        f"http://127.0.0.1:{server.server_port}",
                        [str(imported["job_id"])],
                    )[0]
                    self.assertEqual(status["status"], "completed")
                    repaired = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(len(repaired["documents"]), 1)
                    self.assertEqual(
                        repaired["documents"][0]["source_file_id"],
                        imported["source_file_id"],
                    )
                    self.assertEqual(
                        len(list(config_path.parent.glob("pdf_imports.json.corrupt-*"))),
                        1,
                    )
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_failed_pdf_detection_removes_the_unreferenced_copy(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            paper = source_dir / "detection-fails.pdf"
            write_native_pdf(paper, "FAIL-DETECTION")
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
            handler = None
            with patch(
                "src.me_finder.web.detect_imported_pdf",
                side_effect=MinerUError("PDF detection failed"),
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    response = self._post_json(
                        f"http://127.0.0.1:{server.server_port}/api/import-local",
                        {"paths": [str(paper)]},
                    )
                    self.assertEqual(response["jobs"], [])
                    self.assertEqual(len(response["errors"]), 1)
                    raw_pdf = root / "corpus" / "raw_pdf"
                    self.assertEqual(list(raw_pdf.glob("*.pdf")), [])
                    self.assertEqual(list(raw_pdf.glob(".mefinder-*")), [])
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_failed_docx_job_journal_removes_the_unreferenced_upload(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source = Path(temp_dir) / "orphan.docx"
            write_test_docx(source, "journal failure fixture")
            payload = source.read_bytes()
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            (root / "config" / "pdf_imports.json").write_text(
                '{"documents": []}',
                encoding="utf-8",
            )

            previous_cwd = Path.cwd()
            server = None
            handler = None
            try:
                os.chdir(root)
                handler = make_handler(root / "data" / "index.sqlite3")
                handler.log_message = lambda *_args: None
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                threading.Thread(
                    target=server.serve_forever,
                    daemon=True,
                ).start()
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/import",
                    data=payload,
                    headers={
                        "Content-Type": (
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                        "Content-Length": str(len(payload)),
                        "X-File-Name": source.name,
                    },
                    method="POST",
                )
                with patch(
                    "src.me_finder.web.ImportJobJournal.save_job",
                    side_effect=OSError("journal unavailable"),
                ):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 400)
                raw_docx = root / "corpus" / "raw_docx"
                deadline = time.monotonic() + 2
                while (
                    list(raw_docx.glob("*.docx"))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertEqual(list(raw_docx.glob("*.docx")), [])
                self.assertEqual(list(raw_docx.glob(".mefinder-*")), [])
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
                os.chdir(previous_cwd)

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

    def test_pdf_batch_uses_atomic_transactions_without_copying_the_index(self) -> None:
        """单篇事务可自行回滚，不能再为 3.5GB 索引制作整库导入快照。"""

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            paths = []
            for number in range(4):
                path = source_dir / f"批量导入{number}.pdf"
                write_native_pdf(path, f"batch-backup-{number}")
                paths.append(str(path))
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
                "src.me_finder.web.detect_imported_pdf",
                return_value={"detected_pdf_type": "native_text", "pdf_page_count": 1},
            ), patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ), patch(
                "src.me_finder.database._backup_database",
                wraps=database_module._backup_database,
            ) as backup:
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": paths},
                    )
                    job_ids = [str(job["job_id"]) for job in response["jobs"]]
                    statuses = self._wait_for_jobs(base_url, job_ids)
                    self.assertEqual(
                        [status["status"] for status in statuses],
                        ["completed"] * len(paths),
                        [status.get("message") for status in statuses],
                    )
                    self.assertEqual(backup.call_count, 0)
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_batch_queue_failure_keeps_native_and_remote_jobs_resumable(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            native = source_dir / "native.pdf"
            remote = source_dir / "remote.pdf"
            write_native_pdf(native, "NATIVE-QUEUE")
            write_native_pdf(remote, "REMOTE-QUEUE")
            build_database(
                {"metadata": {}},
                root / "data" / "index.sqlite3",
            )
            (root / "config" / "pdf_imports.json").write_text(
                '{"documents": []}',
                encoding="utf-8",
            )
            save_preferences(
                {"scan_directories": [str(source_dir)]},
                root / "config" / "preferences.json",
            )

            def detected(path: Path) -> dict[str, object]:
                return {
                    "detected_pdf_type": (
                        "native_text"
                        if Path(path).name == native.name
                        else "scanned"
                    ),
                    "pdf_page_count": 1,
                }

            previous_cwd = Path.cwd()
            server = None
            handler = None
            with (
                patch(
                    "src.me_finder.web.detect_imported_pdf",
                    side_effect=detected,
                ),
                patch(
                    "src.me_finder.import_queue.ImportTaskQueue.submit",
                    side_effect=RuntimeError("queue unavailable"),
                ),
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(
                        root / "data" / "index.sqlite3"
                    )
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(
                        ("127.0.0.1", 0),
                        handler,
                    )
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = (
                        f"http://127.0.0.1:{server.server_port}"
                    )

                    response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(native), str(remote)]},
                    )
                    self.assertEqual(response["errors"], [])
                    self.assertEqual(len(response["jobs"]), 2)
                    statuses = [
                        self._job_status(
                            base_url,
                            str(item["job_id"]),
                        )
                        for item in response["jobs"]
                    ]
                    self.assertEqual(
                        [item["status"] for item in statuses],
                        ["failed", "failed"],
                    )
                    self.assertEqual(
                        [item["failure_stage"] for item in statuses],
                        ["queue", "queue"],
                    )
                    self.assertTrue(
                        all(item["can_resume"] for item in statuses)
                    )
                    self.assertEqual(
                        len(
                            list(
                                (
                                    root / "corpus" / "raw_pdf"
                                ).glob("*.pdf")
                            )
                        ),
                        2,
                    )
                    config = json.loads(
                        (
                            root / "config" / "pdf_imports.json"
                        ).read_text(encoding="utf-8")
                    )
                    self.assertEqual(len(config["documents"]), 2)
                    self.assertEqual(
                        len(
                            list(
                                (
                                    root
                                    / "corpus"
                                    / "processed"
                                    / "import_jobs"
                                ).glob("*.json")
                            )
                        ),
                        2,
                    )
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
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

    def test_native_pdf_failure_does_not_hide_or_fail_the_other_pdf(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            good = source_dir / "good-native.pdf"
            bad = source_dir / "broken-native.pdf"
            write_native_pdf(good, "GOOD")
            bad.write_bytes(b"%PDF-1.4\nthis file is deliberately truncated")
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
            handler = None
            with patch(
                "src.me_finder.web.detect_imported_pdf",
                return_value={
                    "detected_pdf_type": "native_text",
                    "pdf_page_count": 1,
                },
            ), patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ), patch(
                "src.me_finder.web.rebuild_local_index",
                wraps=rebuild_local_index,
            ) as rebuild:
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(good), str(bad)]},
                    )
                    self.assertEqual(response["errors"], [])
                    self.assertEqual(len(response["jobs"]), 2)
                    statuses = self._wait_for_jobs(
                        base_url,
                        [str(job["job_id"]) for job in response["jobs"]],
                    )
                    by_name = {
                        str(status["file_name"]): status for status in statuses
                    }
                    self.assertEqual(
                        by_name["good-native.pdf"]["status"],
                        "completed",
                    )
                    self.assertEqual(
                        by_name["broken-native.pdf"]["status"],
                        "failed",
                    )
                    self.assertEqual(
                        by_name["broken-native.pdf"]["phase"],
                        "index_failed",
                    )
                    self.assertEqual(
                        by_name["broken-native.pdf"]["failure_stage"],
                        "index",
                    )
                    self.assertIn(
                        "未能进入索引",
                        str(by_name["broken-native.pdf"]["message"]),
                    )
                    # A PDF-only batch is updated one document at a time, so
                    # one malformed file cannot invalidate the whole library.
                    self.assertEqual(rebuild.call_count, 0)

                    connection = sqlite3.connect(root / "data" / "index.sqlite3")
                    try:
                        indexed_names = {
                            row[0]
                            for row in connection.execute(
                                "SELECT file_name FROM source_files "
                                "WHERE source_type = 'pdf'"
                            )
                        }
                    finally:
                        connection.close()
                    self.assertEqual(indexed_names, {"good-native.pdf"})
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_native_pdf_is_not_failed_by_a_word_batch_rebuild_error(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            paper = source_dir / "independent.pdf"
            document = source_dir / "word-document.docx"
            write_native_pdf(paper, "INDEPENDENT-PDF")
            write_test_docx(document, "Word batch fixture")
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
            handler = None
            with patch(
                "src.me_finder.web.detect_imported_pdf",
                return_value={
                    "detected_pdf_type": "native_text",
                    "pdf_page_count": 1,
                },
            ), patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ), patch(
                "src.me_finder.web.rebuild_local_index",
                side_effect=RuntimeError("Word index rebuild failed"),
            ) as rebuild:
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(paper), str(document)]},
                    )
                    statuses = self._wait_for_jobs(
                        base_url,
                        [str(job["job_id"]) for job in response["jobs"]],
                    )
                    by_name = {
                        str(status["file_name"]): status for status in statuses
                    }
                    self.assertEqual(
                        by_name[paper.name]["status"],
                        "completed",
                    )
                    self.assertEqual(
                        by_name[document.name]["phase"],
                        "index_failed",
                    )
                    self.assertEqual(rebuild.call_count, 1)

                    connection = sqlite3.connect(
                        root / "data" / "index.sqlite3"
                    )
                    try:
                        indexed_pdf = connection.execute(
                            "SELECT COUNT(*) FROM source_files "
                            "WHERE source_type = 'pdf'"
                        ).fetchone()[0]
                    finally:
                        connection.close()
                    self.assertEqual(indexed_pdf, 1)
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_same_pdf_reimport_is_idempotent_across_different_names(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            first = source_dir / "first-name.pdf"
            second = source_dir / "renamed-copy.pdf"
            write_native_pdf(first, "SAME-CONTENT")
            second.write_bytes(first.read_bytes())
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
            handler = None
            with patch(
                "src.me_finder.web.detect_imported_pdf",
                return_value={
                    "detected_pdf_type": "native_text",
                    "pdf_page_count": 1,
                },
            ), patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    first_response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(first)]},
                    )
                    first_status = self._wait_for_jobs(
                        base_url,
                        [str(first_response["jobs"][0]["job_id"])],
                    )[0]
                    self.assertEqual(first_status["status"], "completed")

                    second_response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(second)]},
                    )
                    self.assertEqual(second_response["errors"], [])
                    second_status = self._wait_for_jobs(
                        base_url,
                        [str(second_response["jobs"][0]["job_id"])],
                    )[0]
                    self.assertEqual(second_status["status"], "completed")

                    config = json.loads(
                        (root / "config" / "pdf_imports.json").read_text("utf-8")
                    )
                    source_ids = [
                        str(item.get("source_file_id"))
                        for item in config.get("documents", [])
                    ]
                    self.assertEqual(len(source_ids), 1)
                    self.assertEqual(len(set(source_ids)), 1)
                    connection = sqlite3.connect(root / "data" / "index.sqlite3")
                    try:
                        indexed_count = connection.execute(
                            "SELECT COUNT(*) FROM source_files "
                            "WHERE source_type = 'pdf'"
                        ).fetchone()[0]
                    finally:
                        connection.close()
                    self.assertEqual(indexed_count, 1)
                    self.assertEqual(
                        len(list((root / "corpus" / "raw_pdf").glob("*.pdf"))),
                        1,
                    )

                    removal = self._post_json(
                        base_url + "/api/documents/remove-batch",
                        {
                            "source_ids": source_ids,
                            "delete_generated_artifacts": True,
                            "internal_copy_source_ids": [],
                        },
                    )
                    self.assertTrue(removal["ok"])
                    retained_config = json.loads(
                        (root / "config" / "pdf_imports.json").read_text("utf-8")
                    )
                    self.assertEqual(len(retained_config["documents"]), 1)
                    self.assertFalse(retained_config["documents"][0]["enabled"])
                    self.assertTrue(
                        retained_config["documents"][0]["retained_after_removal"]
                    )
                    self.assertEqual(
                        len(list((root / "corpus" / "raw_pdf").glob("*.pdf"))),
                        1,
                    )

                    reimport_response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(second)]},
                    )
                    self.assertEqual(reimport_response["errors"], [])
                    reimport_status = self._wait_for_jobs(
                        base_url,
                        [str(reimport_response["jobs"][0]["job_id"])],
                    )[0]
                    self.assertEqual(reimport_status["status"], "completed")

                    reactivated_config = json.loads(
                        (root / "config" / "pdf_imports.json").read_text("utf-8")
                    )
                    self.assertEqual(len(reactivated_config["documents"]), 1)
                    self.assertTrue(reactivated_config["documents"][0]["enabled"])
                    self.assertNotIn(
                        "retained_after_removal",
                        reactivated_config["documents"][0],
                    )
                    self.assertEqual(
                        len(list((root / "corpus" / "raw_pdf").glob("*.pdf"))),
                        1,
                    )
                    with sqlite3.connect(
                        root / "data" / "index.sqlite3"
                    ) as connection:
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM source_files "
                                "WHERE source_type = 'pdf'"
                            ).fetchone()[0],
                            1,
                        )
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_parsed_remote_pdfs_are_committed_independently(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            good = source_dir / "cnki-good.pdf"
            bad = source_dir / "broken-native.pdf"
            write_native_pdf(good, "CNKI-GOOD")
            write_native_pdf(bad, "CNKI-BAD")
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
            handler = None
            with patch(
                "src.me_finder.web.detect_imported_pdf",
                return_value={
                    "detected_pdf_type": "broken_text",
                    "pdf_page_count": 1,
                },
            ), patch(
                "src.me_finder.web.parse_pdf_with_mineru",
                return_value=None,
            ) as parse, patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ), patch(
                "src.me_finder.web.rebuild_local_index",
                wraps=rebuild_local_index,
            ) as rebuild:
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(good), str(bad)]},
                    )
                    statuses = self._wait_for_jobs(
                        base_url,
                        [str(job["job_id"]) for job in response["jobs"]],
                    )
                    by_name = {
                        str(status["file_name"]): status for status in statuses
                    }
                    self.assertEqual(by_name["cnki-good.pdf"]["status"], "completed")
                    self.assertEqual(by_name["broken-native.pdf"]["status"], "failed")
                    self.assertEqual(
                        by_name["broken-native.pdf"]["phase"],
                        "index_failed",
                    )
                    self.assertIn(
                        "文件已解析，但索引更新失败",
                        str(by_name["broken-native.pdf"]["message"]),
                    )
                    self.assertEqual(parse.call_count, 2)
                    self.assertEqual(rebuild.call_count, 0)

                    connection = sqlite3.connect(root / "data" / "index.sqlite3")
                    try:
                        indexed_names = {
                            row[0]
                            for row in connection.execute(
                                "SELECT file_name FROM source_files "
                                "WHERE source_type = 'pdf'"
                            )
                        }
                    finally:
                        connection.close()
                    self.assertEqual(indexed_names, {"cnki-good.pdf"})
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_remote_pdf_is_indexed_while_another_parser_is_still_blocked(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            ready = source_dir / "first-ready.pdf"
            blocked = source_dir / "second-blocked.pdf"
            write_native_pdf(ready, "READY")
            write_native_pdf(blocked, "BLOCKED")
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            (root / "config" / "pdf_imports.json").write_text(
                '{"documents": []}',
                encoding="utf-8",
            )
            save_preferences(
                {"scan_directories": [str(source_dir)]},
                root / "config" / "preferences.json",
            )

            blocked_started = threading.Event()
            release_blocked = threading.Event()

            def parse_one_at_a_time(
                _root: Path,
                path: Path,
                _source_file_id: str,
                **_kwargs,
            ) -> None:
                if path.name != blocked.name:
                    return
                blocked_started.set()
                if not release_blocked.wait(5):
                    raise TimeoutError("test did not release the blocked parser")

            previous_cwd = Path.cwd()
            server = None
            handler = None
            with patch(
                "src.me_finder.web.detect_imported_pdf",
                return_value={
                    "detected_pdf_type": "broken_text",
                    "pdf_page_count": 1,
                },
            ), patch(
                "src.me_finder.web.parse_pdf_with_mineru",
                side_effect=parse_one_at_a_time,
            ) as parse, patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(ready), str(blocked)]},
                    )
                    jobs_by_name = {
                        str(job["file_name"]): str(job["job_id"])
                        for job in response["jobs"]
                    }
                    self.assertTrue(
                        blocked_started.wait(2),
                        "second parser never reached its blocking point",
                    )

                    ready_status = self._wait_for_job_status(
                        base_url,
                        jobs_by_name[ready.name],
                        {"completed"},
                    )
                    self.assertEqual(ready_status["status"], "completed")
                    blocked_status = self._job_status(
                        base_url,
                        jobs_by_name[blocked.name],
                    )
                    self.assertEqual(blocked_status["status"], "processing")

                    connection = sqlite3.connect(
                        root / "data" / "index.sqlite3"
                    )
                    try:
                        indexed_names = {
                            row[0]
                            for row in connection.execute(
                                "SELECT file_name FROM source_files "
                                "WHERE source_type = 'pdf'"
                            )
                        }
                    finally:
                        connection.close()
                    self.assertEqual(indexed_names, {ready.name})

                    release_blocked.set()
                    final_statuses = self._wait_for_jobs(
                        base_url,
                        list(jobs_by_name.values()),
                    )
                    self.assertEqual(
                        {status["status"] for status in final_statuses},
                        {"completed"},
                    )
                    self.assertEqual(parse.call_count, 2)
                finally:
                    release_blocked.set()
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_unique_constraint_retry_only_rebuilds_index(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            paper = source_dir / "cnki-journal.pdf"
            write_native_pdf(paper, "CNKI-RETRY")
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            (root / "config" / "pdf_imports.json").write_text(
                '{"documents": []}',
                encoding="utf-8",
            )
            save_preferences(
                {"scan_directories": [str(source_dir)]},
                root / "config" / "preferences.json",
            )

            replace_calls = 0

            def flaky_replace(*args, **kwargs):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint failed: source_files.source_file_id"
                    )
                return real_replace_source(*args, **kwargs)

            previous_cwd = Path.cwd()
            server = None
            handler = None
            with patch(
                "src.me_finder.web.detect_imported_pdf",
                return_value={
                    "detected_pdf_type": "broken_text",
                    "pdf_page_count": 1,
                },
            ), patch(
                "src.me_finder.web.parse_pdf_with_mineru",
                return_value=None,
            ) as parse, patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ), patch(
                "src.me_finder.web.replace_source_in_database",
                side_effect=flaky_replace,
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    response = self._post_json(
                        base_url + "/api/import-local",
                        {"paths": [str(paper)]},
                    )
                    job_id = str(response["jobs"][0]["job_id"])
                    failed = self._wait_for_jobs(base_url, [job_id])[0]
                    self.assertEqual(failed["phase"], "index_failed")
                    self.assertIn(
                        "UNIQUE constraint failed",
                        str(failed["message"]),
                    )
                    self.assertEqual(parse.call_count, 1)

                    resumed = self._post_json(
                        base_url + "/api/import-resume",
                        {"job_id": job_id},
                    )
                    self.assertTrue(resumed["ok"])
                    completed = self._wait_for_jobs(base_url, [job_id])[0]
                    self.assertEqual(completed["status"], "completed")
                    self.assertEqual(parse.call_count, 1)
                    self.assertEqual(replace_calls, 2)
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def test_transient_runtime_reopen_failure_is_retried_after_pdf_write(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "library"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            source_dir.mkdir()
            paper = source_dir / "reopen-retry.pdf"
            write_native_pdf(paper, "REOPEN-RETRY")
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
            handler = None
            reopen_calls = 0

            def transient_reopen(index_path: Path) -> RealSearchEngine:
                nonlocal reopen_calls
                reopen_calls += 1
                if reopen_calls == 1:
                    raise sqlite3.OperationalError(
                        "database is temporarily busy"
                    )
                return RealSearchEngine(index_path)

            with patch(
                "src.me_finder.web.detect_imported_pdf",
                return_value={
                    "detected_pdf_type": "native_text",
                    "pdf_page_count": 1,
                },
            ), patch(
                "src.me_finder.web.extract_pdf_source",
                side_effect=fake_native_extraction,
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    with patch(
                        "src.me_finder.web.SearchEngine",
                        side_effect=transient_reopen,
                    ):
                        response = self._post_json(
                            base_url + "/api/import-local",
                            {"paths": [str(paper)]},
                        )
                        completed = self._wait_for_jobs(
                            base_url,
                            [str(response["jobs"][0]["job_id"])],
                        )[0]

                    self.assertEqual(completed["status"], "completed")
                    self.assertGreaterEqual(reopen_calls, 2)
                    search = self._post_json(
                        base_url + "/api/search",
                        {"query": "reopen-retry searchable"},
                    )
                    self.assertGreaterEqual(int(search["total"]), 1)
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
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
    def _job_status(
        base_url: str,
        job_id: str,
    ) -> dict[str, object]:
        with urlopen(
            base_url + "/api/import-status?job_id=" + job_id,
            timeout=5,
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    @classmethod
    def _wait_for_job_status(
        cls,
        base_url: str,
        job_id: str,
        statuses: set[str],
    ) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = cls._job_status(base_url, job_id)
            if status.get("status") in statuses:
                return status
            time.sleep(0.02)
        raise AssertionError(
            f"import job {job_id} did not reach one of {sorted(statuses)}"
        )

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
