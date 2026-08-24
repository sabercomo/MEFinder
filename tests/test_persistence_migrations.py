from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

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

    def test_v3_migration_is_registered_and_idempotent(self) -> None:
        self.assertTrue(migrate_index_database(self.database_path))
        self.assertFalse(migrate_index_database(self.database_path))
        connection = sqlite3.connect(str(self.database_path))
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 3
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


if __name__ == "__main__":
    unittest.main()
