from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.persistence.connection import (
    PROJECT_BUSY_TIMEOUT_MS,
    backup_readonly_into,
    connect_index,
    open_build_target,
    open_read,
    open_readonly_index,
    open_readonly_snapshot,
    open_writable_index,
    open_write,
    table_exists,
)


def _pragma(connection: sqlite3.Connection, name: str) -> int:
    return int(connection.execute(f"PRAGMA {name}").fetchone()[0])


class PersistenceConnectionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "index.sqlite3"
        with open_write(self.db) as connection:
            connection.executescript(
                "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER "
                "REFERENCES parent(id) ON DELETE CASCADE);"
                "INSERT INTO parent(id) VALUES (1);"
                "INSERT INTO child(id, parent_id) VALUES (10, 1);"
            )
            connection.commit()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_keeps_python_default_timeout_and_foreign_keys_off(self) -> None:
        with open_read(self.db) as connection:
            self.assertEqual(_pragma(connection, "foreign_keys"), 0)
            self.assertEqual(_pragma(connection, "busy_timeout"), 5000)
            self.assertIs(connection.row_factory, sqlite3.Row)

    def test_read_can_keep_plain_tuple_rows(self) -> None:
        with open_read(self.db, row_factory=None) as connection:
            row = connection.execute("SELECT id FROM parent").fetchone()
        self.assertIsInstance(row, tuple)

    def test_write_enables_foreign_keys_and_cascades(self) -> None:
        with open_write(self.db) as connection:
            self.assertEqual(_pragma(connection, "foreign_keys"), 1)
            connection.execute("DELETE FROM parent WHERE id = 1")
            connection.commit()
        with open_read(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM child").fetchone()[0], 0
            )

    def test_write_rolls_back_uncommitted_work_on_error(self) -> None:
        with self.assertRaises(RuntimeError):
            with open_write(self.db, immediate=True) as connection:
                self.assertTrue(connection.in_transaction)
                connection.execute("INSERT INTO parent(id) VALUES (2)")
                raise RuntimeError("boom")
        with open_read(self.db) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM parent").fetchone()[0], 1
            )

    def test_explicit_busy_timeout_is_applied(self) -> None:
        with open_read(self.db, busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS) as connection:
            self.assertEqual(_pragma(connection, "busy_timeout"), 30000)

    def test_readonly_snapshot_refuses_writes(self) -> None:
        with open_readonly_snapshot(self.db) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO parent(id) VALUES (3)")

    def test_readonly_uri_cannot_be_writable(self) -> None:
        with self.assertRaises(ValueError):
            connect_index(self.db, write=True, readonly_uri=True)

    def test_legacy_openers_keep_project_policy(self) -> None:
        connection = open_writable_index(self.db)
        try:
            self.assertEqual(_pragma(connection, "foreign_keys"), 1)
            self.assertEqual(_pragma(connection, "busy_timeout"), 30000)
        finally:
            connection.close()
        connection = open_readonly_index(self.db)
        try:
            self.assertEqual(_pragma(connection, "query_only"), 1)
            self.assertEqual(_pragma(connection, "busy_timeout"), 30000)
        finally:
            connection.close()

    def test_build_target_stays_policy_free(self) -> None:
        connection = open_build_target(Path(self._tmp.name) / "build.tmp")
        try:
            self.assertEqual(_pragma(connection, "foreign_keys"), 0)
            self.assertIsNone(connection.row_factory)
        finally:
            connection.close()

    def test_backup_readonly_into_copies_and_reports_integrity(self) -> None:
        destination = Path(self._tmp.name) / "copy" / "index.sqlite3"
        self.assertEqual(backup_readonly_into(self.db, destination), "ok")
        with open_read(destination) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM child").fetchone()[0], 1
            )

    def test_table_exists(self) -> None:
        with open_read(self.db) as connection:
            self.assertTrue(table_exists(connection, "parent"))
            self.assertFalse(table_exists(connection, "missing"))


if __name__ == "__main__":
    unittest.main()
