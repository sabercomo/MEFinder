from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.me_finder.app_context import AppContext, AppPaths
from src.me_finder.application.document_import_coordinator import DocumentImportCoordinator
from src.me_finder.database import build_database, replace_source_in_database
from src.me_finder.document_export import (
    DocumentExportError,
    document_manifest,
    export_document_zip,
    extract_embedded_source_pdf,
)
from src.me_finder.document_package_import import (
    build_document_package_records,
    read_document_package,
)
from src.me_finder.import_resume import sha256_file
from src.me_finder.mineru_api import MinerUError
from src.me_finder.web import make_handler


class PackageJobs:
    def __init__(self) -> None:
        self.jobs = {}
        self.replacements = []
        self.released = []

    def register_background_job(self, job):
        self.jobs[str(job["job_id"])] = dict(job)

    def submit_background_task(self, task, *args):
        task(*args)

    def update_import_job(self, job_id, **updates):
        self.jobs[job_id].update(updates)

    def replace_imported_source(self, job_id, extracted, source_file_id):
        self.replacements.append((job_id, extracted, source_file_id))

    def register_pdf_for_import(self, target, *, original_file_name=None):
        source_id = f"pdf-import-{sha256_file(Path(target))[:16]}"
        return (
            {
                "source_file_id": source_id,
                "document_id": source_id.upper().replace("-", "_"),
                "original_file_name": original_file_name,
            },
            source_id,
            Path(target),
        )

    def release_import_reservation(self, source_file_id):
        self.released.append(source_file_id)

    def cleanup_unreferenced_import_target(self, candidate):
        return False


def make_package(
    path: Path,
    *,
    source_pdf: Path | None = None,
    digest: str | None = None,
) -> Path:
    source_digest = digest or hashlib.sha256(b"original-pdf").hexdigest()
    manifest = document_manifest(
        document={"source_file_id": "old-id", "title": "分享文献"},
        source_sha256=source_digest,
        source_file={
            "file_name": "原书.pdf",
            "file_format": "pdf",
            "size_bytes": source_pdf.stat().st_size if source_pdf else 123,
        },
        bibliographic_metadata={"title": "分享文献", "author": "作者丙"},
        parser_provider="mineru",
        page_count=2,
    )
    return export_document_zip(
        path,
        manifest,
        [
            {"physical_pdf_page": 1, "logical_page": "10", "text": "第十页正文。"},
            {"physical_pdf_page": 2, "logical_page": "11", "text": "第十一页正文。"},
        ],
        source_pdf_path=source_pdf,
    )


class DocumentPackageImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_data_only_package_restores_text_metadata_and_page_mapping(self) -> None:
        package_path = make_package(self.root / "shared.mefinder.zip")
        package = read_document_package(package_path)
        source_id = f"pdf-import-{package.source_sha256[:16]}"
        extracted, mappings = build_document_package_records(
            package,
            package_path=package_path,
            source_file_id=source_id,
            document_id="PDF_IMPORTED",
            runtime_root=self.root,
        )
        database = self.root / "index.sqlite3"
        build_database(
            {"source_files": [], "volumes": [], "works": [], "paragraphs": []},
            database,
        )
        replace_source_in_database(extracted, database, backup_existing=False)

        source = extracted["source_files"][0]
        self.assertEqual(source["bibliographic_metadata"]["author"], "作者丙")
        self.assertEqual(source["relative_path"], "")
        self.assertEqual(
            [page["citation_page"] for page in extracted["pdf_pages"]],
            ["10", "11"],
        )
        self.assertEqual(mappings[0]["pdf_page_start"], 0)
        self.assertEqual(mappings[0]["pdf_page_end"], 1)
        self.assertEqual(mappings[0]["citation_page_start"], "10")
        self.assertEqual(len(extracted["paragraphs"]), 2)

    def test_package_can_embed_and_verify_original_pdf(self) -> None:
        source_pdf = self.root / "source.pdf"
        source_pdf.write_bytes(b"original-pdf")
        package_path = make_package(
            self.root / "with-pdf.mefinder.zip",
            source_pdf=source_pdf,
            digest=sha256_file(source_pdf),
        )
        destination = self.root / "restored.pdf"

        self.assertEqual(
            extract_embedded_source_pdf(package_path, destination),
            destination,
        )
        self.assertEqual(destination.read_bytes(), source_pdf.read_bytes())
        with zipfile.ZipFile(package_path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"manifest.json", "pages.ndjson", "source/original.pdf"},
            )
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["source_pdf"]["sha256"], sha256_file(source_pdf))

    def test_tampered_embedded_pdf_fails_before_publication(self) -> None:
        source_pdf = self.root / "source.pdf"
        source_pdf.write_bytes(b"original-pdf")
        package_path = make_package(
            self.root / "valid.mefinder.zip",
            source_pdf=source_pdf,
            digest=sha256_file(source_pdf),
        )
        tampered = self.root / "tampered.mefinder.zip"
        with zipfile.ZipFile(package_path) as source, zipfile.ZipFile(
            tampered, "w"
        ) as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "source/original.pdf":
                    payload = b"tampered-pdf"
                target.writestr(name, payload)

        destination = self.root / "must-not-exist.pdf"
        with self.assertRaisesRegex(DocumentExportError, "大小|SHA-256"):
            extract_embedded_source_pdf(tampered, destination)
        self.assertFalse(destination.exists())

    def test_only_document_packages_use_the_direct_import_task(self) -> None:
        paths = AppPaths.create(self.root / "runtime")
        coordinator = DocumentImportCoordinator(paths, PackageJobs())
        try:
            with self.assertRaises(MinerUError):
                coordinator.start_chunked(
                    "third-party.json",
                    10,
                    import_kind="parsed_result",
                )
        finally:
            coordinator.close()

    def test_chunked_package_with_pdf_restores_file_without_ocr(self) -> None:
        source_pdf = self.root / "source.pdf"
        source_pdf.write_bytes(b"original-pdf")
        package_path = make_package(
            self.root / "roundtrip.mefinder.zip",
            source_pdf=source_pdf,
            digest=sha256_file(source_pdf),
        )
        payload = package_path.read_bytes()
        paths = AppPaths.create(self.root / "runtime")
        jobs = PackageJobs()
        coordinator = DocumentImportCoordinator(paths, jobs)
        coordinator._detect_pdf = lambda _path: {"pdf_page_count": 2}
        try:
            started = coordinator.start_chunked(
                package_path.name,
                len(payload),
                import_kind="document_package",
            )
            upload_id = str(started["upload_id"])
            coordinator.append_chunk(
                upload_id,
                0,
                len(payload),
                io.BytesIO(payload),
            )
            result = coordinator.finish_chunked(upload_id)

            job = jobs.jobs[str(result["job_id"])]
            self.assertEqual(job["status"], "completed")
            self.assertIn("原 PDF", job["message"])
            self.assertIn("未运行 OCR", job["message"])
            extracted = jobs.replacements[0][1]
            restored_source = extracted["source_files"][0]
            self.assertTrue(restored_source["relative_path"])
            restored_pdf = paths.runtime_root / str(restored_source["relative_path"])
            self.assertEqual(restored_pdf.read_bytes(), source_pdf.read_bytes())
            self.assertEqual(extracted["pdf_pages"][0]["citation_page"], "10")
        finally:
            coordinator.close()


class DocumentPackageImportHTTPTests(unittest.TestCase):
    def test_package_upload_completes_and_appears_in_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_path = make_package(root / "http.mefinder.zip")
            payload = package_path.read_bytes()
            database = root / "data" / "index.sqlite3"
            build_database(
                {"source_files": [], "volumes": [], "works": [], "paragraphs": []},
                database,
            )
            handler = make_handler(
                database,
                app_context=AppContext.create(root, index_path=database),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, started = self._json_request(
                    server,
                    "POST",
                    "/api/import-upload/start",
                    {
                        "file_name": package_path.name,
                        "size": len(payload),
                        "import_kind": "document_package",
                    },
                )
                self.assertEqual(status, 200)
                upload_id = str(started["upload_id"])
                status, progress = self._raw_request(
                    server,
                    "/api/import-upload/chunk",
                    payload,
                    {
                        "Content-Type": "application/zip",
                        "X-Upload-ID": upload_id,
                        "X-Upload-Offset": "0",
                    },
                )
                self.assertEqual(status, 200)
                self.assertTrue(progress["complete"])
                status, finished = self._json_request(
                    server,
                    "POST",
                    "/api/import-upload/finish",
                    {"upload_id": upload_id},
                )
                self.assertEqual(status, 200)
                job_id = str(finished["job_id"])
                job = {}
                for _ in range(100):
                    status, job = self._json_request(
                        server,
                        "GET",
                        f"/api/import-status?job_id={job_id}",
                        None,
                    )
                    if job.get("status") != "processing":
                        break
                    time.sleep(0.02)
                self.assertEqual(status, 200)
                self.assertEqual(job.get("status"), "completed")

                status, library = self._json_request(server, "GET", "/api/library", None)
                self.assertEqual(status, 200)
                item = next(
                    entry
                    for entry in library["items"]
                    if entry["source_file_id"] == job["source_file_id"]
                )
                self.assertEqual(item["title"], "分享文献")
                self.assertEqual(item["author"], "作者丙")
                with closing(sqlite3.connect(database)) as connection:
                    page = json.loads(
                        connection.execute(
                            "SELECT payload_json FROM pdf_pages WHERE source_file_id = ?",
                            (job["source_file_id"],),
                        ).fetchone()[0]
                    )
                self.assertEqual(page["citation_page"], "10")
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)

    @staticmethod
    def _json_request(server, method, path, payload):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Length": str(len(body))}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        return DocumentPackageImportHTTPTests._request(
            server, method, path, body, headers
        )

    @staticmethod
    def _raw_request(server, path, body, headers):
        request_headers = dict(headers)
        request_headers["Content-Length"] = str(len(body))
        return DocumentPackageImportHTTPTests._request(
            server, "POST", path, body, request_headers
        )

    @staticmethod
    def _request(server, method, path, body, headers):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
