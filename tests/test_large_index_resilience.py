from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder import database as database_module
from src.me_finder.database import (
    DATABASE_BACKUP_RETENTION,
    _backup_database,
    build_database,
    replace_source_in_database,
)


def extracted_source(number: int) -> dict[str, list[dict[str, object]]]:
    source_id = f"pdf-{number:04d}"
    volume_id = f"VOL-{number:04d}"
    work_id = f"{volume_id}-W1"
    text = f"source {number} incremental index text"
    return {
        "source_files": [
            {
                "source_file_id": source_id,
                "source_type": "pdf",
                "file_name": f"{source_id}.pdf",
                "relative_path": f"corpus/raw_pdf/{source_id}.pdf",
            }
        ],
        "volumes": [
            {
                "volume_id": volume_id,
                "source_file_id": source_id,
                "source_type": "pdf",
                "display_title": source_id,
            }
        ],
        "works": [
            {
                "work_id": work_id,
                "volume_id": volume_id,
                "source_type": "pdf",
                "title": source_id,
            }
        ],
        "paragraphs": [
            {
                "paragraph_id": f"{source_id}-P1",
                "volume_id": volume_id,
                "work_id": work_id,
                "source_file_id": source_id,
                "source_type": "pdf",
                "paragraph_index": 0,
                "eligible_for_search": True,
                "text_raw": text,
                "normalized_text": text,
                "compact_text": text.replace(" ", ""),
                "plain_text": text.replace(" ", ""),
            }
        ],
        "toc_entries": [],
        "page_anchors": [],
        "pdf_pages": [],
        "pdf_page_mappings": [],
        "pdf_import_runs": [],
        "audit_issues": [],
    }


class LargeIndexResilienceTests(unittest.TestCase):
    def test_more_than_two_hundred_sources_use_incremental_transactions(self) -> None:
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "index.sqlite3"
            build_database({"metadata": {}}, database_path)
            for number in range(220):
                replace_source_in_database(
                    extracted_source(number),
                    database_path,
                    backup_existing=False,
                )

            connection = sqlite3.connect(str(database_path))
            try:
                source_count = connection.execute(
                    "SELECT COUNT(*) FROM source_files"
                ).fetchone()[0]
                paragraph_count = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs"
                ).fetchone()[0]
                integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(source_count, 220)
            self.assertEqual(paragraph_count, 220)
            self.assertEqual(integrity, "ok")

    def test_legacy_backups_are_pruned_before_the_next_full_copy(self) -> None:
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "index.sqlite3"
            database_path.write_bytes(b"current database")
            backup_dir = database_path.parent / "backups"
            backup_dir.mkdir()
            for number in range(8):
                snapshot = backup_dir / f"index-202501010000{number:02d}.sqlite3"
                snapshot.write_bytes(f"snapshot {number}".encode("utf-8"))
                os.utime(snapshot, (number + 1, number + 1))

            real_copy = shutil.copy2
            counts_before_copy: list[int] = []

            def observe_copy(source: Path, target: Path):
                counts_before_copy.append(
                    len(list(backup_dir.glob("index-*.sqlite3")))
                )
                return real_copy(source, target)

            with patch.object(
                database_module.shutil,
                "copy2",
                side_effect=observe_copy,
            ):
                _backup_database(database_path)

            self.assertEqual(
                counts_before_copy,
                [max(0, DATABASE_BACKUP_RETENTION - 1)],
            )
            self.assertLessEqual(
                len(list(backup_dir.glob("index-*.sqlite3"))),
                DATABASE_BACKUP_RETENTION,
            )

    def test_low_disk_error_is_actionable_and_does_not_start_copy(self) -> None:
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "index.sqlite3"
            database_path.write_bytes(b"x" * 1024)
            backup_dir = database_path.parent / "backups"
            backup_dir.mkdir()
            known_good = backup_dir / "index-20250101000000.sqlite3"
            known_good.write_bytes(b"known good snapshot")
            disk_usage = shutil._ntuple_diskusage(
                total=1024,
                used=1024,
                free=0,
            )
            with (
                patch.object(
                    database_module.shutil,
                    "disk_usage",
                    return_value=disk_usage,
                ),
                patch.object(database_module.shutil, "copy2") as copy,
            ):
                with self.assertRaises(OSError) as caught:
                    _backup_database(database_path)
            self.assertEqual(caught.exception.errno, errno.ENOSPC)
            self.assertIn("磁盘空间不足", str(caught.exception))
            self.assertIn("backups", str(caught.exception))
            self.assertEqual(known_good.read_bytes(), b"known good snapshot")
            copy.assert_not_called()

    def test_interrupted_copy_never_exposes_a_partial_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "index.sqlite3"
            database_path.write_bytes(b"current database")
            backup_dir = database_path.parent / "backups"
            backup_dir.mkdir()
            known_good = backup_dir / "index-20250101000000.sqlite3"
            known_good.write_bytes(b"known good snapshot")

            def partial_copy(_source: Path, target: Path):
                Path(target).write_bytes(b"partial")
                raise OSError(errno.EIO, "simulated interrupted copy")

            with patch.object(
                database_module.shutil,
                "copy2",
                side_effect=partial_copy,
            ):
                with self.assertRaises(OSError) as caught:
                    _backup_database(database_path)

            self.assertEqual(caught.exception.errno, errno.EIO)
            self.assertEqual(known_good.read_bytes(), b"known good snapshot")
            self.assertEqual(
                list(backup_dir.glob("index-*.sqlite3")),
                [known_good],
            )
            self.assertEqual(list(backup_dir.glob(".*.tmp")), [])

    def test_backup_fsync_uses_a_windows_compatible_writable_handle(self) -> None:
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "index.sqlite3"
            database_path.write_bytes(b"current database")
            real_open = Path.open
            backup_open_modes: list[str] = []

            def observe_open(path: Path, mode: str = "r", *args, **kwargs):
                if (
                    Path(path).parent.name == "backups"
                    and Path(path).name.startswith(".index-")
                ):
                    backup_open_modes.append(mode)
                return real_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", autospec=True, side_effect=observe_open):
                _backup_database(database_path)

            self.assertEqual(backup_open_modes, ["rb+"])

    def test_full_rebuild_preflights_backup_and_temp_database_space(self) -> None:
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "index.sqlite3"
            original = b"x" * (2 * 1024 * 1024)
            database_path.write_bytes(original)
            only_one_copy_free = shutil._ntuple_diskusage(
                total=10 * 1024 * 1024,
                used=7 * 1024 * 1024,
                free=(
                    len(original)
                    + database_module.DATABASE_BACKUP_FREE_SPACE_MARGIN
                ),
            )
            with (
                patch.object(
                    database_module.shutil,
                    "disk_usage",
                    return_value=only_one_copy_free,
                ),
                patch.object(database_module.shutil, "copy2") as copy,
            ):
                with self.assertRaises(OSError) as caught:
                    build_database(
                        {"metadata": {}},
                        database_path,
                        backup_existing=True,
                    )

            self.assertEqual(caught.exception.errno, errno.ENOSPC)
            self.assertEqual(database_path.read_bytes(), original)
            copy.assert_not_called()

    def test_full_rebuild_preflight_accounts_for_a_growing_new_index(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "index.sqlite3"
            database_path.write_bytes(b"x" * (1024 * 1024))
            old_size = database_path.stat().st_size
            estimated_new_size = 32 * 1024 * 1024
            enough_for_only_two_old_copies = shutil._ntuple_diskusage(
                total=256 * 1024 * 1024,
                used=0,
                free=(
                    old_size * 2
                    + database_module.DATABASE_BACKUP_FREE_SPACE_MARGIN
                ),
            )
            with (
                patch.object(
                    database_module,
                    "_estimate_database_build_size",
                    return_value=estimated_new_size,
                ),
                patch.object(
                    database_module.shutil,
                    "disk_usage",
                    return_value=enough_for_only_two_old_copies,
                ),
                patch.object(database_module.shutil, "copy2") as copy,
            ):
                with self.assertRaises(OSError) as caught:
                    build_database(
                        {"metadata": {}},
                        database_path,
                        backup_existing=True,
                    )

            self.assertEqual(caught.exception.errno, errno.ENOSPC)
            copy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
