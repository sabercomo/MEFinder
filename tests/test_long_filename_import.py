from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from src.me_finder.database import build_database
from src.me_finder.document_deletion import DocumentDeletionService
from src.me_finder import document_file_store
from src.me_finder.extractors import extract_source
from src.me_finder.pdf_extractors import extract_pdf_source
from src.me_finder.pdf_import_service import (
    INTERNAL_DOCUMENT_NAME_MAX_BYTES,
    cleanup_stale_document_storage_files,
    copy_local_document,
    document_storage_target,
    load_import_config,
    register_pdf,
    release_document_storage_target,
)
from src.me_finder.web import make_handler


ISSUE_FILE_NAME = (
    "National Science Education Standards - National Committee on Science "
    "Education Standards and Assessment、Board on Science Education、Division "
    "of Behavioral and Social Sciences and Education 等.pdf"
)


def write_native_pdf(path: Path) -> None:
    import fitz

    document = fitz.open()
    for page_number in range(2):
        page = document.new_page()
        text = (
            f"National science education standards page {page_number + 1}. "
            "This selectable paragraph verifies long filename indexing. "
        ) * 20
        page.insert_textbox(
            fitz.Rect(50, 50, 540, 780),
            text,
            fontsize=10,
        )
    document.save(str(path))
    document.close()


def write_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body>'
                "<w:p><w:r><w:t>长文件名 DOCX 正文</w:t></w:r></w:p>"
                "</w:body></w:document>"
            ),
        )


def fake_extraction(
    path: Path,
    root: Path,
    config: dict[str, object],
    parsed_dir: Path | None = None,
) -> dict[str, list[dict[str, object]]]:
    del parsed_dir
    source_id = str(config["source_file_id"])
    document_id = str(config["document_id"])
    original_name = str(config.get("original_file_name") or path.name)
    text = "long filename HTTP import searchable text"
    return {
        "source_files": [
            {
                "source_file_id": source_id,
                "source_type": "pdf",
                "document_id": document_id,
                "file_name": original_name,
                "stored_file_name": path.name,
                "relative_path": str(path.relative_to(root)).replace("\\", "/"),
            }
        ],
        "volumes": [
            {
                "volume_id": document_id,
                "source_file_id": source_id,
                "source_type": "pdf",
                "display_title": Path(original_name).stem,
            }
        ],
        "works": [
            {
                "work_id": f"{document_id}-W0001",
                "volume_id": document_id,
                "source_type": "pdf",
                "title": Path(original_name).stem,
            }
        ],
        "paragraphs": [
            {
                "paragraph_id": f"{source_id}-P000000",
                "volume_id": document_id,
                "work_id": f"{document_id}-W0001",
                "source_file_id": source_id,
                "source_type": "pdf",
                "paragraph_index": 0,
                "eligible_for_search": True,
                "text_raw": text,
                "normalized_text": text,
                "compact_text": text.replace(" ", ""),
                "plain_text": text.replace(" ", ""),
                "original_file_name": original_name,
            }
        ],
        "toc_entries": [],
        "page_anchors": [],
        "pdf_pages": [],
        "pdf_page_mappings": [],
        "pdf_import_runs": [],
        "audit_issues": [],
    }


class LongFilenameImportTests(unittest.TestCase):
    def test_storage_reservation_is_visible_through_the_filesystem(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = document_storage_target(directory, "same-name.pdf")
            second = document_storage_target(directory, "same-name.pdf")
            try:
                self.assertNotEqual(first, second)
                self.assertEqual(
                    len(list(directory.glob(".mefinder-reserve-*.lock"))),
                    2,
                )
            finally:
                release_document_storage_target(first)
                release_document_storage_target(second)

            self.assertEqual(
                list(directory.glob(".mefinder-reserve-*.lock")),
                [],
            )

    def test_only_stale_hidden_storage_files_are_cleaned(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stale = directory / ".mefinder-upload-old.tmp"
            active = directory / ".mefinder-copy-active.tmp"
            final = directory / "document.pdf"
            stale.write_bytes(b"stale")
            active.write_bytes(b"active")
            final.write_bytes(b"final")
            os.utime(stale, (1, 1))

            removed = cleanup_stale_document_storage_files(
                directory,
                older_than_seconds=60,
            )

            self.assertEqual(removed, [stale])
            self.assertTrue(active.exists())
            self.assertTrue(final.exists())

    def test_concurrent_same_name_copies_never_overwrite_each_other(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "app"
            first_source = base / "first" / "same-name.pdf"
            second_source = base / "second" / "same-name.pdf"
            first_source.parent.mkdir()
            second_source.parent.mkdir()
            first_source.write_bytes(b"first distinct PDF bytes")
            second_source.write_bytes(b"second distinct PDF bytes")
            copy_barrier = threading.Barrier(2)
            real_copy = shutil.copy2

            def synchronized_copy(source: Path, target: Path):
                copy_barrier.wait(timeout=5)
                return real_copy(source, target)

            with (
                patch.object(
                    document_file_store.shutil,
                    "copy2",
                    side_effect=synchronized_copy,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [
                    executor.submit(copy_local_document, root, first_source),
                    executor.submit(copy_local_document, root, second_source),
                ]
                stored = [future.result(timeout=5) for future in futures]

            self.assertNotEqual(stored[0], stored[1])
            self.assertEqual(
                {path.read_bytes() for path in stored},
                {
                    first_source.read_bytes(),
                    second_source.read_bytes(),
                },
            )

    def test_case_variant_names_share_one_cross_platform_reservation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "app"
            first_source = base / "first" / "Case.pdf"
            second_source = base / "second" / "case.pdf"
            first_source.parent.mkdir()
            second_source.parent.mkdir()
            first_source.write_bytes(b"uppercase-name PDF bytes")
            second_source.write_bytes(b"lowercase-name PDF bytes")
            copy_barrier = threading.Barrier(2)
            real_copy = shutil.copy2

            def synchronized_copy(source: Path, target: Path):
                copy_barrier.wait(timeout=5)
                return real_copy(source, target)

            with (
                patch.object(
                    document_file_store.shutil,
                    "copy2",
                    side_effect=synchronized_copy,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [
                    executor.submit(
                        copy_local_document,
                        root,
                        first_source,
                    ),
                    executor.submit(
                        copy_local_document,
                        root,
                        second_source,
                    ),
                ]
                stored = [future.result(timeout=5) for future in futures]

            self.assertNotEqual(
                stored[0].name.casefold(),
                stored[1].name.casefold(),
            )
            self.assertEqual(
                {path.read_bytes() for path in stored},
                {
                    first_source.read_bytes(),
                    second_source.read_bytes(),
                },
            )

    def test_reservation_release_failure_does_not_hide_successful_copy(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "app"
            source = base / "locked-release.pdf"
            source.write_bytes(b"durable PDF bytes")
            real_unlink = Path.unlink

            def fail_reservation_unlink(
                path: Path,
                *args,
                **kwargs,
            ):
                if path.name.startswith(".mefinder-reserve-"):
                    raise PermissionError("reservation temporarily locked")
                return real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_reservation_unlink):
                stored = copy_local_document(root, source)

            self.assertEqual(stored.read_bytes(), source.read_bytes())
            self.assertEqual(
                len(
                    list(
                        stored.parent.glob(
                            ".mefinder-reserve-*.lock"
                        )
                    )
                ),
                1,
            )

    def test_exact_issue_name_uses_short_internal_paths_and_keeps_display_name(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "app"
            source = base / ISSUE_FILE_NAME
            write_native_pdf(source)

            first = copy_local_document(root, source)
            second = copy_local_document(root, source)
            self.assertLessEqual(
                len(first.name.encode("utf-8")),
                INTERNAL_DOCUMENT_NAME_MAX_BYTES,
            )
            self.assertLessEqual(
                len(second.name.encode("utf-8")),
                INTERNAL_DOCUMENT_NAME_MAX_BYTES,
            )
            self.assertNotEqual(first, second)
            self.assertEqual(source.name, ISSUE_FILE_NAME)
            self.assertFalse(
                any(
                    item.name.startswith(".mefinder-")
                    for item in first.parent.iterdir()
                )
            )

            document = register_pdf(
                root,
                first,
                original_file_name=ISSUE_FILE_NAME,
            )
            self.assertEqual(document["file_name"], first.name)
            self.assertEqual(document["original_file_name"], ISSUE_FILE_NAME)
            self.assertEqual(document["title"], Path(ISSUE_FILE_NAME).stem)

            with patch(
                "src.me_finder.pdf_extractors.detect_pdf_type",
                return_value={
                    "detected_pdf_type": "native_text",
                    "pdf_page_count": 2,
                    "parser": "pymupdf",
                },
            ):
                extracted = extract_pdf_source(first, root, document)
            source_record = extracted["source_files"][0]
            self.assertEqual(source_record["file_name"], ISSUE_FILE_NAME)
            self.assertEqual(source_record["stored_file_name"], first.name)
            self.assertTrue(extracted["paragraphs"])
            self.assertTrue(
                all(
                    item["original_file_name"] == ISSUE_FILE_NAME
                    for item in extracted["paragraphs"]
                )
            )

            database_path = root / "data" / "index.sqlite3"
            build_database(extracted, database_path)
            connection = sqlite3.connect(str(database_path))
            try:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM source_files"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            self.assertEqual(payload["file_name"], ISSUE_FILE_NAME)
            self.assertTrue(
                payload["relative_path"].endswith("/" + first.name)
            )

    def test_deletion_staging_name_stays_short_for_long_files(self) -> None:
        with TemporaryDirectory() as temporary:
            original = Path(temporary) / ISSUE_FILE_NAME
            original.write_bytes(b"pdf")
            staged: list[tuple[Path, Path]] = []

            DocumentDeletionService._stage(original, staged)

            self.assertEqual(len(staged), 1)
            self.assertLess(len(staged[0][1].name), 80)
            self.assertFalse(original.exists())
            self.assertEqual(
                DocumentDeletionService._restore_staged(staged),
                [],
            )
            self.assertEqual(original.read_bytes(), b"pdf")

    def test_long_docx_keeps_its_original_name_for_index_display(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "app"
            original_name = ("很长的独立文献标题" * 8) + ".docx"
            source = base / original_name
            write_docx(source)

            stored = copy_local_document(root, source)
            extracted = extract_source(stored, root)

            self.assertEqual(stored.name, original_name)
            self.assertEqual(
                extracted["source_file"]["file_name"],
                original_name,
            )
            self.assertEqual(
                extracted["source_file"]["display_title"],
                Path(original_name).stem,
            )

    def test_http_retry_succeeds_when_legacy_full_name_already_exists(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "app"
            raw_pdf = root / "corpus" / "raw_pdf"
            raw_pdf.mkdir(parents=True)
            legacy = raw_pdf / ISSUE_FILE_NAME
            legacy.write_bytes(b"legacy copy left by an earlier failed import")
            (root / "data").mkdir(parents=True)
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")

            payload = b"%PDF-1.4 long filename upload fixture\n%%EOF\n"
            previous_cwd = Path.cwd()
            server = None
            handler = None
            opener = build_opener(ProxyHandler({}))
            with (
                patch(
                    "src.me_finder.web_runtime.detect_imported_pdf",
                    return_value={
                        "detected_pdf_type": "native_text",
                        "pdf_page_count": 1,
                    },
                ),
                patch(
                    "src.me_finder.web_runtime.extract_pdf_source",
                    side_effect=fake_extraction,
                ),
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
                    request = Request(
                        base_url + "/api/import",
                        data=payload,
                        headers={
                            "Content-Type": "application/pdf",
                            "X-File-Name": quote(ISSUE_FILE_NAME),
                            "X-PDF-Parse-Mode": "auto",
                        },
                        method="POST",
                    )
                    with opener.open(request, timeout=5) as response:
                        result = json.loads(response.read().decode("utf-8"))
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["file_name"], ISSUE_FILE_NAME)

                    deadline = time.monotonic() + 5
                    status: dict[str, object] = {}
                    while time.monotonic() < deadline:
                        with opener.open(
                            base_url
                            + "/api/import-status?job_id="
                            + str(result["job_id"]),
                            timeout=5,
                        ) as response:
                            status = json.loads(response.read().decode("utf-8"))
                        if status.get("status") != "processing":
                            break
                        time.sleep(0.02)
                    self.assertEqual(status.get("status"), "completed", status)
                    self.assertEqual(status.get("file_name"), ISSUE_FILE_NAME)

                    config = load_import_config(
                        root / "config" / "pdf_imports.json"
                    )
                    imported = config["documents"][0]
                    self.assertEqual(
                        imported["original_file_name"],
                        ISSUE_FILE_NAME,
                    )
                    self.assertLessEqual(
                        len(str(imported["file_name"]).encode("utf-8")),
                        INTERNAL_DOCUMENT_NAME_MAX_BYTES,
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
