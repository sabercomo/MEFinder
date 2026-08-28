from __future__ import annotations

import hashlib
from contextlib import closing
import json
import shutil
import sqlite3
import subprocess
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


NODE = shutil.which("node")


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
    def test_full_export_entry_enriches_before_shared_normalization_and_returns_report(self):
        from tests.test_markdown_export import footnote_fixture
        from tests.test_document_heading import _v2_title
        from src.me_finder.document_heading import DOCUMENT_HEADING_VERSION

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = export_fixture(root)
            pages = footnote_fixture()
            cache = root / "cached-parser"
            cache.mkdir()
            v2 = [[] for _ in range(12)]
            for page in pages:
                page["source_file_id"] = source_id
                for block in page["blocks"]:
                    block.update(result_dir=str(cache), pdf_page_index=page["pdf_page_index"],
                                 local_page_idx=page["pdf_page_index"])
                    if block.get("document_heading_level"):
                        v2[page["pdf_page_index"]].append(_v2_title(block["text"], block.pop("document_heading_level"), block.get("bbox")))
            (cache / "content_list_v2.json").write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DELETE FROM pdf_pages WHERE source_file_id=?", (source_id,))
                for page in pages:
                    connection.execute("INSERT INTO pdf_pages(source_file_id,pdf_page_index,payload_json) VALUES(?,?,?)",
                                       (source_id, page["pdf_page_index"], json.dumps(page, ensure_ascii=False)))
                connection.commit()
            output = root / "exports"
            output.mkdir()
            context = AppContext.create(root, index_path=database)
            handler = make_handler(database, app_context=context)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                responses = []
                for kind in ("epub", "markdown"):
                    status, response = self._post(server, {"source_id": source_id, "output_dir": str(output)},
                                                  path="/api/document/export-" + kind)
                    self.assertEqual(status, 200)
                    responses.append(response)
                # Existing profile is complete, but source-page provenance is
                # still checked on every export. No inferred destination/split.
                (cache / "layout.json").write_text(json.dumps({"pdf_info": [{
                    "page_idx": 9, "page_size": [1000, 1000],
                    "para_blocks": [{"bbox": pages[0]["blocks"][2]["bbox"],
                                     "lines": [{"spans": [{"cross_page": True}]}]}],
                }]}), encoding="utf-8")
                status, guarded = self._post(server, {"source_id": source_id, "output_dir": str(output)},
                                             path="/api/document/export-epub")
                self.assertEqual(status, 200)
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)
            self.assertEqual(responses[0]["footnote_report"], responses[1]["footnote_report"])
            report = responses[0]["footnote_report"]
            self.assertEqual(report["matched_ref_count"], 4)
            self.assertEqual([s["number_range"] for s in report["scopes"]], [[1, 3], [1, 1]])
            self.assertEqual([s["boundary_reason"] for s in report["scopes"]], ["MINERU_V2", "MINERU_V2"])
            self.assertEqual(guarded["footnote_report"]["matched_note_count"], 2)
            self.assertEqual(guarded["footnote_report"]["unresolved_reason"]["ref"], {"CROSS_PAGE_SOURCE_BLOCK": 2})
            self.assertEqual(guarded["footnote_report"]["unresolved_reason"]["note"], {"CROSS_PAGE_SOURCE_BLOCK": 2})
            with zipfile.ZipFile(guarded["path"]) as archive:
                guarded_text = archive.read("OEBPS/content.xhtml").decode("utf-8")
                self.assertIn("先引用②，再引用①。", guarded_text)
                self.assertIn("① 相同文献。", guarded_text)
            with closing(sqlite3.connect(database)) as connection:
                source = json.loads(connection.execute("SELECT payload_json FROM source_files WHERE source_file_id=?", (source_id,)).fetchone()[0])
                stored = [json.loads(r[0]) for r in connection.execute("SELECT payload_json FROM pdf_pages WHERE source_file_id=? ORDER BY pdf_page_index", (source_id,))]
            self.assertEqual(source["document_heading_profile"]["version"], DOCUMENT_HEADING_VERSION)
            self.assertEqual([p["text_raw"] for p in stored], [p["text_raw"] for p in pages])
            self.assertFalse(any("_export_source_cross_page" in b for p in stored for b in p["blocks"]))
            self.assertIn("## 第一章 起点", Path(responses[1]["path"]).read_text(encoding="utf-8"))

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

    @unittest.skipUnless(NODE, "node is required for frontend execution tests")
    def test_reading_exports_do_not_forward_legacy_page_preferences(self) -> None:
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const requests = [];
const context = {
  settingsStore: {},
  fetch: async (url, options) => {
    requests.push({url, payload: JSON.parse(options.body)});
    return {ok: true, json: async () => ({ok: true})};
  }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  for (const mode of ['full', 'none']) {
    context.settingsStore.exportPageCleanup = {
      page_marker_mode: mode, remove_running_headers: false
    };
    for (const [method, format] of [
      ['requestLibraryDocumentMarkdownExport', 'markdown'],
      ['requestLibraryDocumentEpubExport', 'epub']
    ]) {
      await context[method]('source-one', 'C:/exports');
      assert.deepEqual(requests.pop(), {
        url: '/api/document/export-' + format,
        payload: {source_id: 'source-one', output_dir: 'C:/exports'}
      });
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        source = Path(__file__).resolve().parents[1] / "src/me_finder/static/js/30-library.js"
        result = subprocess.run(
            [NODE, "-e", script, str(source)], capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
