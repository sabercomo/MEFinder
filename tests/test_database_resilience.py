from __future__ import annotations

import errno
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from src.me_finder.database import (
    IndexIdentityConflictError,
    _replace_database_file,
    build_database,
)


def pdf_source(
    source_file_id: str,
    *,
    file_name: str,
    sha256: str,
    relative_path: str | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "source_file_id": source_file_id,
        "source_type": "pdf",
        "file_name": file_name,
        "relative_path": relative_path or f"corpus/raw_pdf/{file_name}",
        "size_bytes": 1024,
        "sha256": sha256,
        **extra,
    }


class DatabaseDuplicateIdentityTests(unittest.TestCase):
    def test_retry_copy_of_same_pdf_is_merged_before_sqlite_insert(self) -> None:
        source_id = "pdf-import-deadbeefdeadbeef"
        digest = "a" * 64
        text = "同一份 PDF 的原生文本。"
        index = {
            "metadata": {"source_count": 2, "paragraph_count": 2},
            "source_files": [
                pdf_source(
                    source_id,
                    file_name="原书.pdf",
                    sha256=digest,
                    display_title="",
                ),
                pdf_source(
                    source_id,
                    file_name="原书 (imported-12345678).pdf",
                    sha256=digest,
                    display_title="原书",
                ),
            ],
            "volumes": [
                {
                    "volume_id": "PDF_IMPORT_DEADBEEFDEADBEEF",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "display_title": "原书",
                },
                {
                    "volume_id": "PDF_IMPORT_DEADBEEFDEADBEEF",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "display_title": "重试副本标题",
                },
            ],
            "works": [
                {
                    "work_id": "PDF_IMPORT_DEADBEEFDEADBEEF-W0001",
                    "volume_id": "PDF_IMPORT_DEADBEEFDEADBEEF",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "title": "原书",
                },
                {
                    "work_id": "PDF_IMPORT_DEADBEEFDEADBEEF-W0001",
                    "volume_id": "PDF_IMPORT_DEADBEEFDEADBEEF",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "title": "重试副本标题",
                },
            ],
            "paragraphs": [
                {
                    "paragraph_id": f"{source_id}-P000000",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "paragraph_index": 0,
                    "eligible_for_search": True,
                    "text_raw": text,
                    "pdf_page_start_index": 0,
                    "pdf_page_end_index": 0,
                    "original_file_name": "原书.pdf",
                },
                {
                    "paragraph_id": f"{source_id}-P000000",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "paragraph_index": 0,
                    "eligible_for_search": True,
                    "text_raw": text,
                    "pdf_page_start_index": 0,
                    "pdf_page_end_index": 0,
                    "original_file_name": "原书 (imported-12345678).pdf",
                },
            ],
            "pdf_pages": [
                {
                    "source_file_id": source_id,
                    "pdf_page_index": 0,
                    "page_text_hash": "page-hash",
                    "text_raw": text,
                },
                {
                    "source_file_id": source_id,
                    "pdf_page_index": 0,
                    "page_text_hash": "page-hash",
                    "text_raw": text,
                },
            ],
            "pdf_page_mappings": [
                {
                    "mapping_id": f"MAP-{source_id}",
                    "source_file_id": source_id,
                    "method": "uncalibrated",
                },
                {
                    "mapping_id": f"MAP-{source_id}",
                    "source_file_id": source_id,
                    "method": "uncalibrated",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.sqlite3"
            summary = build_database(index, db_path)
            connection = sqlite3.connect(str(db_path))
            try:
                counts = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in (
                        "source_files",
                        "volumes",
                        "works",
                        "paragraphs",
                        "pdf_pages",
                        "pdf_page_mappings",
                    )
                }
                source_payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM source_files"
                    ).fetchone()[0]
                )
                metadata = {
                    key: json.loads(value)
                    for key, value in connection.execute(
                        "SELECT key, value_json FROM metadata"
                    )
                }
            finally:
                connection.close()

        self.assertEqual(set(counts.values()), {1})
        self.assertEqual(summary["source_count"], 1)
        self.assertEqual(summary["paragraph_count"], 1)
        self.assertEqual(source_payload["file_name"], "原书.pdf")
        self.assertEqual(source_payload["display_title"], "原书")
        self.assertEqual(metadata["source_count"], 1)
        self.assertEqual(metadata["paragraph_count"], 1)
        self.assertEqual(
            metadata["database_deduplication"]["strategy"],
            "first_record_wins_and_fills_missing_fields",
        )
        self.assertEqual(
            summary["deduplicated_rows"],
            {
                "source_files": 1,
                "volumes": 1,
                "works": 1,
                "paragraphs": 1,
                "pdf_pages": 1,
                "pdf_page_mappings": 1,
            },
        )

    def test_same_source_id_with_different_hash_is_rejected_and_old_db_survives(
        self,
    ) -> None:
        source_id = "pdf-import-collision"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.sqlite3"
            build_database(
                {
                    "source_files": [
                        pdf_source(
                            "pdf-existing",
                            file_name="existing.pdf",
                            sha256="e" * 64,
                        )
                    ]
                },
                db_path,
            )
            with self.assertRaisesRegex(
                IndexIdentityConflictError, "pdf-import-collision.*sha256"
            ):
                build_database(
                    {
                        "source_files": [
                            pdf_source(
                                source_id,
                                file_name="a.pdf",
                                sha256="a" * 64,
                            ),
                            pdf_source(
                                source_id,
                                file_name="b.pdf",
                                sha256="b" * 64,
                            ),
                        ]
                    },
                    db_path,
                )
            connection = sqlite3.connect(str(db_path))
            try:
                source_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT source_file_id FROM source_files"
                    )
                ]
            finally:
                connection.close()
        self.assertEqual(source_ids, ["pdf-existing"])

    def test_duplicate_paragraph_identity_with_different_text_is_rejected(
        self,
    ) -> None:
        source_id = "pdf-import-same-file"
        index = {
            "source_files": [
                pdf_source(
                    source_id,
                    file_name="same.pdf",
                    sha256="c" * 64,
                )
            ],
            "paragraphs": [
                {
                    "paragraph_id": f"{source_id}-P000000",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "text_raw": "第一种正文",
                },
                {
                    "paragraph_id": f"{source_id}-P000000",
                    "source_file_id": source_id,
                    "source_type": "pdf",
                    "text_raw": "另一种正文",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                IndexIdentityConflictError, "paragraphs.*text_raw"
            ):
                build_database(index, Path(temp_dir) / "index.sqlite3")

    def test_hashless_records_at_different_paths_are_not_silently_merged(
        self,
    ) -> None:
        index = {
            "source_files": [
                {
                    "source_file_id": "legacy-id",
                    "source_type": "pdf",
                    "file_name": "a.pdf",
                    "relative_path": "a/a.pdf",
                    "size_bytes": 100,
                },
                {
                    "source_file_id": "legacy-id",
                    "source_type": "pdf",
                    "file_name": "b.pdf",
                    "relative_path": "b/b.pdf",
                    "size_bytes": 100,
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                IndexIdentityConflictError, "缺少 SHA-256"
            ):
                build_database(index, Path(temp_dir) / "index.sqlite3")


class DatabaseReplacementRetryTests(unittest.TestCase):
    def test_retryable_windows_and_cloud_locks_use_bounded_backoff(self) -> None:
        temp_path = Path("/tmp/mefinder-temp.sqlite3")
        db_path = Path("/tmp/mefinder-index.sqlite3")
        failures = [
            PermissionError(errno.EACCES, "sharing violation"),
            OSError(errno.EBUSY, "cloud sync busy"),
            None,
        ]
        with patch.object(Path, "replace", autospec=True, side_effect=failures) as replace:
            with patch("src.me_finder.database.time.sleep") as sleep:
                _replace_database_file(temp_path, db_path, attempts=5)
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.1), call(0.2)])

    def test_non_retryable_replace_error_is_not_hidden(self) -> None:
        temp_path = Path("/tmp/mefinder-temp.sqlite3")
        db_path = Path("/tmp/mefinder-index.sqlite3")
        failure = OSError(errno.ENOSPC, "disk full")
        with patch.object(Path, "replace", autospec=True, side_effect=failure) as replace:
            with patch("src.me_finder.database.time.sleep") as sleep:
                with self.assertRaises(OSError) as raised:
                    _replace_database_file(temp_path, db_path, attempts=5)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()

    def test_retryable_lock_is_raised_after_attempt_budget(self) -> None:
        temp_path = Path("/tmp/mefinder-temp.sqlite3")
        db_path = Path("/tmp/mefinder-index.sqlite3")
        failure = PermissionError(errno.EACCES, "still locked")
        with patch.object(Path, "replace", autospec=True, side_effect=failure) as replace:
            with patch("src.me_finder.database.time.sleep") as sleep:
                with self.assertRaises(PermissionError):
                    _replace_database_file(temp_path, db_path, attempts=3)
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.1), call(0.2)])


if __name__ == "__main__":
    unittest.main()
