from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.data_location import (
    DATA_ROOT_MARKER,
    DataLocationError,
    data_location_summary,
    default_macos_data_root,
    migrate_data_root,
    proposed_data_root,
    read_macos_data_root,
)


def _create_current_data_root(root: Path) -> None:
    database = root / "runtime" / "data" / "index.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('原句')")
    config = root / "runtime" / "config"
    config.mkdir(parents=True)
    (config / "pdf_imports.json").write_text(
        '{"documents": []}\n',
        encoding="utf-8",
    )
    corpus = root / "runtime" / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "sample.pdf").write_bytes(b"%PDF-test")
    (root / "preferences.json").write_text(
        '{"theme": "midnight"}\n',
        encoding="utf-8",
    )


class DataLocationTests(unittest.TestCase):
    def test_default_location_and_valid_marker_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            default = default_macos_data_root(home)
            self.assertEqual(
                default,
                home / "Library" / "Application Support" / "MEFinder",
            )
            self.assertEqual(read_macos_data_root(home), default)

            custom = Path(directory) / "OneDrive" / "MEFinder"
            default.mkdir(parents=True)
            (default / DATA_ROOT_MARKER).write_text(
                str(custom) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(read_macos_data_root(home), custom.resolve())

    def test_invalid_relative_marker_falls_back_to_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            default = default_macos_data_root(home)
            default.mkdir(parents=True)
            (default / DATA_ROOT_MARKER).write_text(
                "../relative-location\n",
                encoding="utf-8",
            )

            self.assertEqual(read_macos_data_root(home), default)

    def test_selected_parent_gets_a_mefinder_child(self) -> None:
        self.assertEqual(
            proposed_data_root("/Volumes/PortableSSD"),
            Path("/Volumes/PortableSSD/MEFinder"),
        )
        self.assertEqual(
            proposed_data_root("/Volumes/PortableSSD/MEFinder"),
            Path("/Volumes/PortableSSD/MEFinder"),
        )
        with self.assertRaisesRegex(DataLocationError, "完整"):
            proposed_data_root("OneDrive")

    def test_summary_distinguishes_default_and_custom_locations(self) -> None:
        default = Path("/Users/example/Library/Application Support/MEFinder")
        self.assertFalse(data_location_summary(default, default)["is_custom"])
        self.assertTrue(
            data_location_summary(
                Path("/Volumes/SSD/MEFinder"),
                default,
            )["is_custom"]
        )

    def test_migration_copies_all_data_validates_sqlite_and_retains_old_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = base / "home" / "Library" / "Application Support" / "MEFinder"
            target = base / "OneDrive" / "MEFinder"
            _create_current_data_root(current)
            (current / "runtime/data/index.sqlite3-wal").write_bytes(b"")
            (current / "runtime/data/index.sqlite3-shm").write_bytes(b"")

            result = migrate_data_root(current, target, current)

            self.assertTrue(result["restart_required"])
            self.assertTrue(result["old_data_retained"])
            self.assertTrue((current / "runtime/data/index.sqlite3").is_file())
            self.assertEqual(
                (target / "runtime/corpus/sample.pdf").read_bytes(),
                b"%PDF-test",
            )
            self.assertEqual(
                (target / "preferences.json").read_text(encoding="utf-8"),
                '{"theme": "midnight"}\n',
            )
            with sqlite3.connect(target / "runtime/data/index.sqlite3") as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM sample").fetchone()[0],
                    "原句",
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            self.assertFalse((target / "runtime/data/index.sqlite3-wal").exists())
            self.assertFalse((target / "runtime/data/index.sqlite3-shm").exists())
            self.assertEqual(
                (current / DATA_ROOT_MARKER).read_text(encoding="utf-8").strip(),
                str(target.resolve()),
            )
            self.assertFalse(any(target.parent.glob(".MEFinder.migration-*")))

    def test_migration_refuses_nonempty_or_nested_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = base / "current" / "MEFinder"
            _create_current_data_root(current)

            nonempty = base / "cloud" / "MEFinder"
            nonempty.mkdir(parents=True)
            (nonempty / "existing.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(DataLocationError, "不是空的"):
                migrate_data_root(current, nonempty, current)
            self.assertEqual(
                (nonempty / "existing.txt").read_text(encoding="utf-8"),
                "keep",
            )

            nested = current / "external" / "MEFinder"
            with self.assertRaisesRegex(DataLocationError, "内部"):
                migrate_data_root(current, nested, current)


if __name__ == "__main__":
    unittest.main()
