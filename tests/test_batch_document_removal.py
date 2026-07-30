"""Removing many documents must not copy the whole index once per document.

On the real corpus the index is ~3.5 GB. The old per-document path took a full
snapshot each time, so removing 61 volumes meant ~214 GB of disk writes and
roughly 26 minutes for what is otherwise a millisecond-scale delete.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder import database as database_module
from src.me_finder.database import (
    DATABASE_BACKUP_RETENTION,
    build_database,
    delete_source_from_database,
    delete_sources_from_database,
)
from src.me_finder.document_deletion import DocumentDeletionService
from src.me_finder.web import HTML


def _index(source_ids) -> dict:
    sources, volumes, works, paragraphs = [], [], [], []
    for source_id in source_ids:
        sources.append(
            {
                "source_file_id": source_id,
                "source_type": "pdf",
                "file_name": f"{source_id}.pdf",
                "relative_path": f"corpus/raw_pdf/{source_id}.pdf",
                "document_id": f"DOC_{source_id}",
            }
        )
        volumes.append(
            {
                "volume_id": f"VOL_{source_id}",
                "source_file_id": source_id,
                "source_type": "pdf",
                "display_title": source_id,
            }
        )
        works.append(
            {
                "work_id": f"WORK_{source_id}",
                "volume_id": f"VOL_{source_id}",
                "source_file_id": source_id,
                "source_type": "pdf",
                "title": source_id,
            }
        )
        for number in range(3):
            paragraphs.append(
                {
                    "paragraph_id": f"{source_id}-p{number}",
                    "source_file_id": source_id,
                    "volume_id": f"VOL_{source_id}",
                    "work_id": f"WORK_{source_id}",
                    "source_type": "pdf",
                    "text_raw": "马克思恩格斯全集",
                    "eligible_for_search": 1,
                }
            )
    return {
        "metadata": {},
        "source_files": sources,
        "volumes": volumes,
        "works": works,
        "paragraphs": paragraphs,
    }


class BatchDatabaseDeletionTests(unittest.TestCase):
    def _build(self, root: Path, source_ids) -> Path:
        raw = root / "corpus" / "raw_pdf"
        raw.mkdir(parents=True, exist_ok=True)
        for source_id in source_ids:
            (raw / f"{source_id}.pdf").write_bytes(b"pdf")
        database_path = root / "data" / "index.sqlite3"
        build_database(_index(source_ids), database_path)
        return database_path

    def test_one_snapshot_covers_the_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ids = [f"pdf-{i}" for i in range(8)]
            database_path = self._build(root, ids)
            with patch.object(
                database_module,
                "_backup_database",
                wraps=database_module._backup_database,
            ) as backup:
                result = delete_sources_from_database(ids, database_path)
            self.assertEqual(backup.call_count, 1)
            self.assertEqual(result["source_file_ids"], ids)
            self.assertEqual(result["source_count"], 0)
            self.assertEqual(result["paragraph_count"], 0)
            for source_id in ids:
                self.assertEqual(result["deleted"][source_id]["paragraphs"], 3)

    def test_batch_is_one_transaction_and_rolls_back_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ids = ["pdf-0", "pdf-1", "pdf-2"]
            database_path = self._build(root, ids)
            with self.assertRaises(ValueError):
                delete_sources_from_database(
                    ["pdf-0", "missing-source", "pdf-2"], database_path
                )
            connection = sqlite3.connect(str(database_path))
            try:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM source_files"
                ).fetchone()[0]
                paragraphs = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(remaining, 3)
            self.assertEqual(paragraphs, 9)

    def test_single_delete_keeps_its_result_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = self._build(root, ["pdf-0", "pdf-1"])
            result = delete_source_from_database("pdf-0", database_path)
            self.assertEqual(result["source_file_id"], "pdf-0")
            self.assertEqual(result["deleted"]["paragraphs"], 3)
            self.assertEqual(result["source_count"], 1)
            self.assertIsNotNone(result["backup_path"])

    def test_backups_are_pruned_to_the_retention_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = self._build(root, [f"pdf-{i}" for i in range(6)])
            backup_dir = database_path.parent / "backups"
            for round_number in range(5):
                delete_sources_from_database([f"pdf-{round_number}"], database_path)
            snapshots = sorted(backup_dir.glob("index-*.sqlite3"))
            self.assertLessEqual(len(snapshots), DATABASE_BACKUP_RETENTION)
            self.assertGreaterEqual(len(snapshots), 1)

    def test_pruning_only_touches_this_database_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = self._build(root, ["pdf-0", "pdf-1", "pdf-2", "pdf-3"])
            backup_dir = database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            unrelated = backup_dir / "other-20260101000000.sqlite3"
            unrelated.write_bytes(b"keep me")
            for round_number in range(4):
                delete_sources_from_database([f"pdf-{round_number}"], database_path)
            self.assertTrue(unrelated.exists())
            self.assertEqual(unrelated.read_bytes(), b"keep me")


class BatchDocumentDeletionServiceTests(unittest.TestCase):
    def _service_root(self, source_ids):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        raw = root / "corpus" / "raw_pdf"
        raw.mkdir(parents=True, exist_ok=True)
        parsed = root / "corpus" / "parsed" / "pdf"
        parsed.mkdir(parents=True, exist_ok=True)
        for source_id in source_ids:
            (raw / f"{source_id}.pdf").write_bytes(b"pdf")
            (parsed / f"DOC_{source_id}.json").write_text("{}", encoding="utf-8")
        database_path = root / "data" / "index.sqlite3"
        build_database(_index(source_ids), database_path)
        config_path = root / "config" / "pdf_imports.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "documents": [
                        {"source_file_id": source_id, "document_id": f"DOC_{source_id}"}
                        for source_id in source_ids
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return temp_dir, root, database_path

    def test_remove_many_takes_one_snapshot_and_clears_config_once(self) -> None:
        ids = [f"pdf-{i}" for i in range(6)]
        temp_dir, root, database_path = self._service_root(ids)
        with temp_dir:
            with patch.object(
                database_module,
                "_backup_database",
                wraps=database_module._backup_database,
            ) as backup:
                result = DocumentDeletionService(root, database_path).remove_many(
                    ids[:4]
                )
            self.assertEqual(backup.call_count, 1)
            self.assertEqual(result["removed_source_ids"], ids[:4])
            self.assertEqual(result["failures"], [])
            config = json.loads(
                (root / "config" / "pdf_imports.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["source_file_id"] for item in config["documents"]], ids[4:]
            )
            for source_id in ids[:4]:
                self.assertFalse(
                    (root / "corpus" / "parsed" / "pdf" / f"DOC_{source_id}.json").exists()
                )
            # 默认保留原 PDF。
            for source_id in ids:
                self.assertTrue((root / "corpus" / "raw_pdf" / f"{source_id}.pdf").exists())

    def test_unknown_source_is_reported_without_blocking_the_rest(self) -> None:
        ids = ["pdf-0", "pdf-1"]
        temp_dir, root, database_path = self._service_root(ids)
        with temp_dir:
            result = DocumentDeletionService(root, database_path).remove_many(
                ["pdf-0", "does-not-exist"]
            )
            self.assertEqual(result["removed_source_ids"], ["pdf-0"])
            self.assertEqual(
                [item["source_id"] for item in result["failures"]], ["does-not-exist"]
            )
            connection = sqlite3.connect(str(database_path))
            try:
                remaining = [
                    row[0]
                    for row in connection.execute(
                        "SELECT source_file_id FROM source_files"
                    ).fetchall()
                ]
            finally:
                connection.close()
            self.assertEqual(remaining, ["pdf-1"])

    def test_internal_copy_deletion_is_per_document(self) -> None:
        ids = ["pdf-0", "pdf-1"]
        temp_dir, root, database_path = self._service_root(ids)
        with temp_dir:
            DocumentDeletionService(root, database_path).remove_many(
                ids, internal_copy_ids=["pdf-1"]
            )
            self.assertTrue((root / "corpus" / "raw_pdf" / "pdf-0.pdf").exists())
            self.assertFalse((root / "corpus" / "raw_pdf" / "pdf-1.pdf").exists())

    def test_single_remove_still_works_through_the_batch_path(self) -> None:
        ids = ["pdf-0", "pdf-1"]
        temp_dir, root, database_path = self._service_root(ids)
        with temp_dir:
            result = DocumentDeletionService(root, database_path).remove("pdf-0")
            self.assertEqual(result["source_file_id"], "pdf-0")
            self.assertEqual(result["deleted"]["paragraphs"], 3)
            self.assertTrue(result["removed_from_config"])
            self.assertTrue(result["original_pdf_preserved"])
            self.assertEqual(result["source_count"], 1)

    def test_single_remove_still_raises_for_unknown_source(self) -> None:
        temp_dir, root, database_path = self._service_root(["pdf-0"])
        with temp_dir:
            with self.assertRaises(ValueError):
                DocumentDeletionService(root, database_path).remove("missing")


class BatchRemovalWiringTests(unittest.TestCase):
    def test_web_exposes_the_batch_endpoint(self) -> None:
        source = Path("src/me_finder/web.py").read_text(encoding="utf-8")
        self.assertIn('if parsed.path == "/api/documents/remove-batch":', source)
        self.assertIn(".remove_many(", source)
        self.assertIn('payload.get("internal_copy_source_ids")', source)
        # 一次批量只重新载入一次搜索索引。
        self.assertEqual(source.count('if parsed.path == "/api/documents/remove-batch":'), 1)

    def test_frontend_sends_one_request_and_can_stop_waiting(self) -> None:
        self.assertIn("fetch('/api/documents/remove-batch'", HTML)
        self.assertIn("source_ids: sourceIds,", HTML)
        self.assertIn("internal_copy_source_ids:", HTML)
        self.assertNotIn("fetch('/api/documents/remove',", HTML)
        # 取消要真的中止请求，而不是只关掉弹窗。
        self.assertIn("let removeRequestController = null;", HTML)
        self.assertIn("removeRequestController.abort();", HTML)
        self.assertIn("e.name === 'AbortError'", HTML)


if __name__ == "__main__":
    unittest.main()
