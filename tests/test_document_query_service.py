from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

from src.me_finder.app_context import AppContext, AppPaths
from src.me_finder.application.document_query_service import (
    DocumentQueryError,
    DocumentQueryService,
    DocumentQueryUnavailable,
)
from src.me_finder.database import build_database
from src.me_finder.web import make_handler


class FakeDocumentIndex:
    def __init__(
        self,
        catalog: Dict[str, List[Dict[str, object]]],
        database_path: Path,
    ) -> None:
        self.data = catalog
        self.database_path = database_path
        self.ready = True

    def catalog(self) -> Dict[str, List[Dict[str, object]]]:
        return self.data

    def source(self, source_file_id: str) -> Optional[Dict[str, object]]:
        return next(
            (
                item
                for item in self.data["source_files"]
                if item.get("source_file_id") == source_file_id
            ),
            None,
        )

    def run_when_ready(self, operation):
        if not self.ready:
            return None
        return operation(self.database_path)


class DocumentQueryServiceTests(unittest.TestCase):
    @staticmethod
    def _paths(root: Path) -> AppPaths:
        database = root / "data" / "index.sqlite3"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(str(database))
        try:
            connection.executescript(
                """
                CREATE TABLE pdf_import_runs (
                    row_id INTEGER PRIMARY KEY,
                    source_file_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE pdf_pages (
                    row_id INTEGER PRIMARY KEY,
                    source_file_id TEXT NOT NULL,
                    pdf_page_index INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE paragraphs (
                    row_id INTEGER PRIMARY KEY,
                    source_file_id TEXT NOT NULL,
                    paragraph_index INTEGER NOT NULL,
                    eligible_for_search INTEGER NOT NULL,
                    text_raw TEXT NOT NULL
                );
                CREATE INDEX idx_test_paragraphs_source_position
                ON paragraphs(source_file_id, paragraph_index);
                """
            )
            connection.commit()
        finally:
            connection.close()
        return AppPaths.create(root, index_path=database)

    @staticmethod
    def _write_config(root: Path, documents: List[Dict[str, object]]) -> None:
        config_path = root / "config" / "pdf_imports.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"documents": documents}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_library_queries_combine_runtime_and_transient_active_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self._paths(root)
            raw = root / "corpus" / "raw_pdf"
            raw.mkdir(parents=True)
            for source_id in ("importing", "calibrating"):
                (raw / f"{source_id}.pdf").write_bytes(b"pdf")
            sources = [
                {
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "file_name": f"{source_id}.pdf",
                    "relative_path": f"corpus/raw_pdf/{source_id}.pdf",
                    "pdf_profile": {},
                }
                for source_id in ("importing", "calibrating")
            ]
            self._write_config(
                root,
                [
                    {"source_file_id": "importing", "page_mapping": {}},
                    {"source_file_id": "calibrating", "page_mapping": {}},
                ],
            )
            connection = sqlite3.connect(str(paths.index_path))
            try:
                connection.executemany(
                    "INSERT INTO pdf_import_runs "
                    "(source_file_id, payload_json) VALUES (?, ?)",
                    [
                        (
                            "importing",
                            json.dumps(
                                {
                                    "started_at": "2026-08-11T10:00:00+00:00",
                                    "finished_at": "2026-08-11T10:01:00+00:00",
                                }
                            ),
                        ),
                        ("ignored", "not-json"),
                    ],
                )
                connection.execute(
                    "INSERT INTO paragraphs "
                    "(source_file_id, paragraph_index, eligible_for_search, text_raw) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "importing",
                        0,
                        1,
                        "This is an English document about society and the forms of life.",
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            service = DocumentQueryService(
                paths,
                FakeDocumentIndex(
                    {"source_files": sources, "volumes": [], "works": []},
                    paths.index_path,
                ),
                active_source_ids=lambda: {"importing"},
            )

            library = service.library_data(
                additional_active_source_ids={"calibrating"}
            )
            calibration = service.calibration_library_data(
                additional_active_source_ids={"calibrating"}
            )
            summary = service.library_summary(
                additional_active_source_ids={"calibrating"}
            )
            detail = service.library_detail(
                "importing",
                additional_active_source_ids={"calibrating"},
            )

            library_items = {
                item["source_file_id"]: item for item in library["items"]
            }
            calibration_items = {
                item["source_file_id"]: item
                for item in calibration["items"]
            }
            self.assertEqual(library_items["importing"]["status"], "mapping")
            self.assertEqual(library_items["importing"]["language_code"], "en")
            self.assertEqual(library_items["importing"]["language"], "foreign")
            self.assertEqual(
                library_items["importing"]["imported_at"],
                "2026-08-11T10:00:00+00:00",
            )
            self.assertEqual(
                calibration_items["calibrating"]["status"], "mapping"
            )
            self.assertEqual(
                set(service.latest_pdf_import_runs()), {"importing"}
            )
            self.assertEqual(summary["view"], "summary")
            self.assertEqual(detail["item"]["source_file_id"], "importing")

    def test_source_path_resolves_only_supported_files_inside_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "runtime"
            paths = self._paths(root)
            source = root / "corpus" / "raw_pdf" / "inside.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"pdf")
            outside = base / "outside.pdf"
            outside.write_bytes(b"pdf")
            catalog = FakeDocumentIndex(
                {
                    "source_files": [
                        {
                            "source_file_id": "inside",
                            "relative_path": "corpus/raw_pdf/inside.pdf",
                        },
                        {
                            "source_file_id": "outside",
                            "relative_path": "../outside.pdf",
                        },
                    ],
                    "volumes": [],
                    "works": [],
                },
                paths.index_path,
            )
            service = DocumentQueryService(
                paths, catalog, active_source_ids=lambda: set()
            )

            self.assertEqual(service.source_path("inside"), source.resolve())
            with self.assertRaisesRegex(DocumentQueryError, "应用目录外"):
                service.source_path("outside")
            with self.assertRaisesRegex(DocumentQueryError, "文献未找到"):
                service.source_path("missing")

    def test_database_reads_fail_fast_while_index_is_rebuilding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self._paths(root)
            self._write_config(root, [])
            catalog = FakeDocumentIndex(
                {"source_files": [], "volumes": [], "works": []},
                paths.index_path,
            )
            catalog.ready = False
            service = DocumentQueryService(
                paths, catalog, active_source_ids=lambda: set()
            )

            with self.assertRaisesRegex(DocumentQueryUnavailable, "正在重建"):
                service.library_data()
            with self.assertRaisesRegex(DocumentQueryUnavailable, "正在重建"):
                service.front_matter_pages("source")

    def test_metadata_detection_reads_opening_and_trailing_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self._paths(root)
            source = root / "corpus" / "raw_pdf" / "metadata.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"pdf")
            document = {
                "source_file_id": "metadata",
                "title": "配置标题",
            }
            self._write_config(root, [document])
            connection = sqlite3.connect(str(paths.index_path))
            try:
                connection.executemany(
                    "INSERT INTO pdf_pages "
                    "(source_file_id, pdf_page_index, payload_json) "
                    "VALUES (?, ?, ?)",
                    [
                        (
                            "metadata",
                            page_index,
                            json.dumps(
                                {
                                    "pdf_page_index": page_index,
                                    "text_raw": f"page {page_index}",
                                }
                            ),
                        )
                        for page_index in range(30)
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            calls = []

            def detect(path, pages, configured, *, force=False):
                calls.append((path, pages, configured, force))
                return {"title": "检测标题"}

            service = DocumentQueryService(
                paths,
                FakeDocumentIndex(
                    {
                        "source_files": [
                            {
                                "source_file_id": "metadata",
                                "source_type": "pdf",
                                "relative_path": "corpus/raw_pdf/metadata.pdf",
                            }
                        ],
                        "volumes": [],
                        "works": [],
                    },
                    paths.index_path,
                ),
                active_source_ids=lambda: set(),
                metadata_detector=detect,
            )

            result = service.detect_bibliographic_metadata(
                "metadata", force=True
            )

            self.assertEqual(result, {"title": "检测标题"})
            path, pages, configured, force = calls[0]
            self.assertEqual(path, source.resolve())
            self.assertEqual(configured["title"], "配置标题")
            self.assertTrue(force)
            self.assertEqual(
                [page["pdf_page_index"] for page in pages],
                [*range(20), *range(22, 30)],
            )
            self.assertEqual(
                service.bibliographic_metadata("metadata")["title"],
                "配置标题",
            )

    def test_batch_metadata_candidates_exclude_manual_and_word_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = self._paths(root)
            raw_pdf = root / "corpus" / "raw_pdf"
            raw_word = root / "corpus" / "raw_docx"
            raw_pdf.mkdir(parents=True)
            raw_word.mkdir(parents=True)
            (raw_pdf / "manual.pdf").write_bytes(b"pdf")
            (raw_pdf / "automatic.pdf").write_bytes(b"pdf")
            (raw_word / "word.docx").write_bytes(b"word")
            sources = [
                {
                    "source_file_id": "manual",
                    "source_type": "pdf",
                    "file_name": "manual.pdf",
                    "relative_path": "corpus/raw_pdf/manual.pdf",
                    "bibliographic_metadata": {
                        "title": "人工记录",
                        "metadata_source": "manual",
                    },
                },
                {
                    "source_file_id": "automatic",
                    "source_type": "pdf",
                    "file_name": "automatic.pdf",
                    "relative_path": "corpus/raw_pdf/automatic.pdf",
                    "bibliographic_metadata": {
                        "title": "自动记录",
                        "metadata_source": "filename",
                    },
                },
                {
                    "source_file_id": "word",
                    "source_type": "word",
                    "file_name": "word.docx",
                    "relative_path": "corpus/raw_docx/word.docx",
                },
            ]
            self._write_config(
                root,
                [
                    {"source_file_id": "manual", "page_mapping": {}},
                    {"source_file_id": "automatic", "page_mapping": {}},
                ],
            )
            service = DocumentQueryService(
                paths,
                FakeDocumentIndex(
                    {"source_files": sources, "volumes": [], "works": []},
                    paths.index_path,
                ),
                active_source_ids=lambda: set(),
            )

            candidates = service.batch_metadata_candidates()

            self.assertEqual(
                [item["source_file_id"] for item in candidates],
                ["automatic"],
            )

    def test_http_query_and_shell_error_contracts_survive_service_wiring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "data" / "index.sqlite3"
            build_database(
                {
                    "metadata": {},
                    "source_files": [],
                    "volumes": [],
                    "works": [],
                    "paragraphs": [],
                    "pdf_pages": [],
                    "pdf_page_mappings": [],
                    "pdf_import_runs": [],
                    "audit_issues": [],
                },
                database,
            )
            handler = make_handler(
                database,
                app_context=AppContext.create(root, index_path=database),
            )
            self.assertIsInstance(handler.document_queries, DocumentQueryService)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, payload = self._request(
                    server, "GET", "/api/library?view=summary"
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["view"], "summary")

                handler.index_runtime.suspend()
                try:
                    status, payload = self._request(
                        server, "GET", "/api/library?view=summary"
                    )
                    self.assertEqual(status, 503)
                    self.assertIn("正在重建", payload["error"])
                finally:
                    handler.index_runtime.reopen()

                status, payload = self._request(
                    server,
                    "POST",
                    "/api/open-source",
                    {"source_id": "missing"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "文献未找到。")

                status, payload = self._request(
                    server,
                    "POST",
                    "/api/bibliographic-metadata/detect",
                    {"source_id": "missing"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "PDF 导入配置不存在。")
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

    @staticmethod
    def _request(
        server: ThreadingHTTPServer,
        method: str,
        path: str,
        payload: Optional[Dict[str, object]] = None,
    ):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if body is not None:
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            }
        connection = HTTPConnection(
            "127.0.0.1", server.server_port, timeout=5
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
