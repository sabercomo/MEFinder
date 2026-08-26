from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import optimize_database_storage
from src.me_finder.persistence.index_schema import SCHEMA
from src.me_finder.persistence.migrations import Migration, migrate_index_database


class PersistenceMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary.name) / "index.sqlite3"
        connection = sqlite3.connect(str(self.database_path))
        connection.execute(
            "CREATE TABLE source_files (source_file_id TEXT PRIMARY KEY)"
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_v5_migrations_are_registered_and_idempotent(self) -> None:
        self.assertTrue(migrate_index_database(self.database_path))
        self.assertFalse(migrate_index_database(self.database_path))
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 5
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertIn("document_groups", tables)
        self.assertIn("document_group_members", tables)
        self.assertIn("segment_sets", tables)
        self.assertIn("alignment_runs", tables)
        self.assertIn("text_segment_paragraph_spans", tables)

    def test_v4_database_with_existing_segment_sets_gets_v5_table(self) -> None:
        connection = sqlite3.connect(str(self.database_path))
        connection.execute(
            "CREATE TABLE segment_sets (segment_set_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE text_segments ("
            "segment_id TEXT PRIMARY KEY, "
            "segment_set_id TEXT NOT NULL REFERENCES segment_sets(segment_set_id) "
            "ON DELETE CASCADE)"
        )
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
        connection.close()

        self.assertTrue(migrate_index_database(self.database_path))
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 5
            )
            columns = [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(text_segment_paragraph_spans)"
                )
            ]
            self.assertEqual(
                columns,
                [
                    "segment_id",
                    "source_file_id",
                    "paragraph_id",
                    "paragraph_index",
                    "paragraph_char_start",
                    "paragraph_char_end",
                    "span_order",
                ],
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='idx_segment_paragraph_spans_source_position'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_current_version_repairs_missing_additive_tables(self) -> None:
        connection = sqlite3.connect(str(self.database_path))
        connection.execute("PRAGMA user_version = 3")
        connection.close()

        self.assertTrue(migrate_index_database(self.database_path))
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='document_group_members'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_failed_migration_rolls_back_ddl_and_version(self) -> None:
        def fail_after_ddl(connection: sqlite3.Connection) -> bool:
            connection.execute("CREATE TABLE must_rollback (value TEXT)")
            raise RuntimeError("injected migration failure")

        with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
            migrate_index_database(
                self.database_path,
                migrations=(Migration(3, fail_after_ddl),),
            )
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 2
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='must_rollback'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_newer_database_fails_fast(self) -> None:
        connection = sqlite3.connect(str(self.database_path))
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        with self.assertRaisesRegex(ValueError, "高于当前应用支持"):
            migrate_index_database(self.database_path)

    def test_storage_optimization_copies_segment_paragraph_spans(self) -> None:
        self.database_path.unlink()
        connection = sqlite3.connect(str(self.database_path))
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO source_files(source_file_id, source_type, payload_json) "
            "VALUES ('epub-en', 'word', '{}')"
        )
        connection.execute(
            "INSERT INTO segment_sets(segment_set_id, source_file_id, "
            "source_text_hash, segmenter, segmenter_version, language_code, "
            "created_at) VALUES ('set-en', 'epub-en', 'hash', 'sentence', "
            "'1', 'en', '2026-08-25T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO text_segments(segment_id, segment_set_id, order_index, "
            "text_raw) VALUES ('segment-en-1', 'set-en', 0, 'Right is actual.')"
        )
        connection.execute(
            "INSERT INTO text_segment_paragraph_spans(segment_id, "
            "source_file_id, paragraph_id, paragraph_index, "
            "paragraph_char_start, paragraph_char_end, span_order) "
            "VALUES ('segment-en-1', 'epub-en', 'paragraph-en-1', 12, 3, 18, 0)"
        )
        connection.commit()
        connection.close()

        self.assertTrue(optimize_database_storage(self.database_path))
        connection = sqlite3.connect(str(self.database_path))
        try:
            row = connection.execute(
                "SELECT source_file_id, paragraph_id, paragraph_index, "
                "paragraph_char_start, paragraph_char_end, span_order "
                "FROM text_segment_paragraph_spans WHERE segment_id = ?",
                ("segment-en-1",),
            ).fetchone()
            self.assertEqual(
                row,
                ("epub-en", "paragraph-en-1", 12, 3, 18, 0),
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
