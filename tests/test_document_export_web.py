from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.me_finder.app_context import AppContext
from src.me_finder.database import build_database
from src.me_finder.document_export import (
    extract_embedded_source_pdf,
    read_document_export,
)
from src.me_finder.document_export_service import (
    IndexedDocumentNotFound,
    UnsupportedDocumentExport,
    export_indexed_pdf,
)
from src.me_finder.web import make_handler


def export_fixture(root: Path, *, source_type: str = "pdf") -> tuple[Path, str]:
    source_id = "source-export-1"
    source_path = root / "corpus" / "raw_pdf" / "导出测试.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source bytes")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    pages = [
        {
            "source_file_id": source_id,
            "pdf_page_index": index,
            "physical_pdf_page": index + 1,
            "text_raw": text,
            "blocks": [{"text": text, "type": "text"}],
            "parser": "mineru",
            "parser_version": "v1",
        }
        for index, text in ((0, "第一页简体"), (1, "第二頁繁體"), (2, "page three"))
    ]
    database = root / "data" / "index.sqlite3"
    build_database(
        {
            "metadata": {},
            "source_files": [
                {
                    "source_file_id": source_id,
                    "source_type": source_type,
                    "document_id": "DOCUMENT_EXPORT_1",
                    "file_name": source_path.name,
                    "relative_path": "corpus/raw_pdf/导出测试.pdf",
                    "file_format": source_type,
                    "size_bytes": source_path.stat().st_size,
                    "sha256": digest,
                    "display_title": "导出测试：简繁體",
                    "bibliographic_metadata": {
                        "title": "导出测试：简繁體",
                        "author": "测试者",
                        "isbn": "978-7-0000-0000-1",
                    },
                    "pdf_profile": {
                        "pdf_page_count": 3,
                        "parser": "mineru",
                        "provider_id": "mineru-cloud",
                        "provider_name": "MinerU",
                        "detected_pdf_type": "mineru_structured",
                        "model": "vlm",
                    },
                }
            ],
            "volumes": [
                {
                    "volume_id": "VOLUME_EXPORT_1",
                    "source_file_id": source_id,
                    "source_type": source_type,
                    "volume_number": 1,
                    "display_title": "导出测试：简繁體",
                }
            ],
            "pdf_pages": pages if source_type == "pdf" else [],
            "pdf_import_runs": [
                {
                    "run_id": "RUN-1",
                    "source_file_id": source_id,
                    "status": "success",
                    "started_at": "2026-08-11T08:00:00+00:00",
                    "finished_at": "2026-08-11T08:01:00+00:00",
                }
            ],
            "audit_issues": [
                {
                    "source_file_id": source_id,
                    "issue_type": "fixture_warning",
                    "message": "仅供测试",
                }
            ],
        },
        database,
    )
    return database, source_id


class IndexedDocumentExportTests(unittest.TestCase):
    def test_indexed_pdf_exports_streaming_protocol_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = export_fixture(root)
            result = export_indexed_pdf(
                database_path=database,
                runtime_root=root,
                source_file_id=source_id,
                output_dir=root / "exports",
            )
            exported = read_document_export(Path(result["path"]))

            self.assertEqual(result["schema_version"], "mefinder.document.v1")
            self.assertEqual(result["page_count"], 3)
            self.assertEqual(result["warning_count"], 1)
            self.assertEqual(exported.manifest["parser"]["provider"], "mineru-cloud")
            self.assertEqual(
                exported.manifest["external_ids"]["isbn"], "978-7-0000-0000-1"
            )
            self.assertEqual(
                [page["text"] for page in exported.pages],
                ["第一页简体", "第二頁繁體", "page three"],
            )
            self.assertFalse(Path(str(result["path"]) + ".partial").exists())

    def test_missing_and_unsupported_sources_have_clear_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, _source_id = export_fixture(root, source_type="word")
            with self.assertRaises(IndexedDocumentNotFound):
                export_indexed_pdf(
                    database_path=database,
                    runtime_root=root,
                    source_file_id="missing",
                    output_dir=root / "exports",
                )
            with self.assertRaises(UnsupportedDocumentExport):
                export_indexed_pdf(
                    database_path=database,
                    runtime_root=root,
                    source_file_id="source-export-1",
                    output_dir=root / "exports",
                )

    def test_indexed_pdf_can_be_embedded_in_the_document_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = export_fixture(root)
            result = export_indexed_pdf(
                database_path=database,
                runtime_root=root,
                source_file_id=source_id,
                output_dir=root / "exports",
                include_source_pdf=True,
            )
            restored = root / "restored.pdf"

            self.assertTrue(result["includes_source_pdf"])
            self.assertEqual(
                extract_embedded_source_pdf(Path(result["path"]), restored),
                restored,
            )
            self.assertEqual(restored.read_bytes(), b"source bytes")


class DocumentExportHTTPTests(unittest.TestCase):
    @staticmethod
    def _post(
        server: ThreadingHTTPServer,
        payload: object,
        path: str = "/api/document/export",
    ):
        body = json.dumps(payload).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_http_endpoint_exports_and_frontend_exposes_pdf_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = export_fixture(root)
            selected_output = root / "selected-output"
            selected_output.mkdir()
            context = AppContext.create(root, index_path=database)
            handler = make_handler(database, app_context=context)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, payload = self._post(
                    server,
                    {
                        "source_id": source_id,
                        "include_source_pdf": True,
                        "output_dir": str(selected_output),
                    },
                )
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

            self.assertEqual(status, 200)
            self.assertEqual(payload["schema_version"], "mefinder.document.v1")
            self.assertTrue(payload["includes_source_pdf"])
            self.assertTrue(Path(payload["path"]).is_file())
            self.assertEqual(Path(payload["path"]).parent, selected_output.resolve())

        source = Path("src/me_finder/static/js/30-library.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("导出 MEFinder 文档", source)
        self.assertIn("function exportLibraryDocument(sourceId)", source)
        self.assertIn("function exportSelectedLibraryDocuments()", source)
        self.assertIn(
            "function requestLibraryDocumentExport(sourceId, outputDirectory)",
            source,
        )
        self.assertIn("fetch('/api/document/export'", source)
        self.assertIn(
            "include_source_pdf: settingsStore.currentDocumentExportMode === 'with_pdf'",
            source,
        )
        self.assertIn("payload.output_dir = outputDirectory", source)

    def test_http_markdown_endpoint_exports_utf8_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = export_fixture(root)
            selected_output = root / "selected-output"
            selected_output.mkdir()
            context = AppContext.create(root, index_path=database)
            handler = make_handler(database, app_context=context)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, payload = self._post(
                    server,
                    {"source_id": source_id, "output_dir": str(selected_output)},
                    path="/api/document/export-markdown",
                )
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

            self.assertEqual(status, 200)
            self.assertEqual(payload["page_count"], 3)
            self.assertTrue(Path(payload["path"]).is_file())
            self.assertEqual(Path(payload["path"]).parent, selected_output.resolve())
            content = Path(payload["path"]).read_text(encoding="utf-8")
            self.assertIn("第一页简体", content)
            self.assertIn("第二頁繁體", content)

        source = Path("src/me_finder/static/js/30-library.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("导出 Markdown", source)
        self.assertIn("function exportLibraryDocumentMarkdown(sourceId)", source)
        self.assertIn("fetch('/api/document/export-markdown'", source)

    def test_http_epub_endpoint_exports_epub3_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = export_fixture(root)
            selected_output = root / "selected-output"
            selected_output.mkdir()
            context = AppContext.create(root, index_path=database)
            handler = make_handler(database, app_context=context)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, payload = self._post(
                    server,
                    {"source_id": source_id, "output_dir": str(selected_output)},
                    path="/api/document/export-epub",
                )
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

            path = Path(payload["path"])
            self.assertEqual(status, 200)
            self.assertEqual(payload["epub_version"], "3.0")
            self.assertEqual(payload["page_count"], 3)
            self.assertEqual(path.parent, selected_output.resolve())
            with zipfile.ZipFile(path, "r") as archive:
                content = archive.read("OEBPS/content.xhtml").decode("utf-8")
                self.assertIn("第一页简体", content)
                self.assertIn("第二頁繁體", content)

        source = Path("src/me_finder/static/js/30-library.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("导出 EPUB", source)
        self.assertIn("function exportLibraryDocumentEpub(sourceId)", source)
        self.assertIn("fetch('/api/document/export-epub'", source)


if __name__ == "__main__":
    unittest.main()
