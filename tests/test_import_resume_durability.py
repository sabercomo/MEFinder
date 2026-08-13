from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.import_job_journal import ImportJobJournal
from src.me_finder.import_resume import (
    atomic_write_json,
    fsync_directory,
    quarantine_corrupt_manifest,
)


class ImportResumeDurabilityTests(unittest.TestCase):
    def test_directory_sync_is_an_explicit_noop_on_windows(self) -> None:
        directory = Path("unused")
        with (
            patch("src.me_finder.import_resume.os.name", "nt"),
            patch("src.me_finder.import_resume.os.open") as open_directory,
        ):
            fsync_directory(directory)

        open_directory.assert_not_called()

    def test_atomic_write_orders_file_sync_replace_and_directory_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "manifests"
            directory.mkdir()
            target = directory / "job.json"
            events: list[str] = []
            real_fsync = os.fsync
            real_replace = Path.replace

            def record_fsync(descriptor: int) -> None:
                mode = os.fstat(descriptor).st_mode
                events.append(
                    "directory_fsync" if stat.S_ISDIR(mode) else "file_fsync"
                )
                real_fsync(descriptor)

            def record_replace(source: Path, destination: Path) -> Path:
                events.append("replace")
                return real_replace(source, destination)

            with (
                patch("src.me_finder.import_resume.os.fsync", side_effect=record_fsync),
                patch.object(Path, "replace", autospec=True, side_effect=record_replace),
            ):
                atomic_write_json(target, {"status": "paused"})

            self.assertEqual(
                events,
                ["file_fsync", "replace", "directory_fsync"],
            )
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"status": "paused"},
            )
            self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_atomic_write_persists_new_parent_before_installing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "new" / "nested" / "job.json"
            events: list[str] = []
            real_fsync = os.fsync
            real_replace = Path.replace

            def record_fsync(descriptor: int) -> None:
                mode = os.fstat(descriptor).st_mode
                events.append(
                    "directory_fsync" if stat.S_ISDIR(mode) else "file_fsync"
                )
                real_fsync(descriptor)

            def record_replace(source: Path, destination: Path) -> Path:
                events.append("replace")
                return real_replace(source, destination)

            with (
                patch("src.me_finder.import_resume.os.fsync", side_effect=record_fsync),
                patch.object(Path, "replace", autospec=True, side_effect=record_replace),
            ):
                atomic_write_json(target, {"status": "queued"})

            self.assertEqual(
                events,
                [
                    "directory_fsync",
                    "directory_fsync",
                    "file_fsync",
                    "replace",
                    "directory_fsync",
                ],
            )
            self.assertTrue(target.is_file())
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_failed_replace_removes_and_persists_temporary_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            target = directory / "job.json"
            events: list[str] = []
            real_fsync = os.fsync
            real_unlink = Path.unlink

            def record_fsync(descriptor: int) -> None:
                mode = os.fstat(descriptor).st_mode
                events.append(
                    "directory_fsync" if stat.S_ISDIR(mode) else "file_fsync"
                )
                real_fsync(descriptor)

            def fail_replace(_source: Path, _destination: Path) -> Path:
                events.append("replace")
                raise OSError("replace failed")

            def record_unlink(path: Path, *args: object, **kwargs: object) -> None:
                events.append("unlink")
                real_unlink(path, *args, **kwargs)

            with (
                patch("src.me_finder.import_resume.os.fsync", side_effect=record_fsync),
                patch.object(Path, "replace", autospec=True, side_effect=fail_replace),
                patch.object(Path, "unlink", autospec=True, side_effect=record_unlink),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    atomic_write_json(target, {"status": "failed"})

            self.assertEqual(
                events,
                [
                    "file_fsync",
                    "replace",
                    "unlink",
                    "directory_fsync",
                ],
            )
            self.assertFalse(target.exists())
            self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_directory_sync_failure_is_not_hidden_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            target = directory / "job.json"

            with patch(
                "src.me_finder.import_resume.fsync_directory",
                side_effect=OSError("directory sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    atomic_write_json(target, {"status": "paused"})

            self.assertTrue(target.is_file())
            self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_quarantine_syncs_directory_after_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            damaged = Path(temp_dir) / "job.json"
            damaged.write_text("{bad json", encoding="utf-8")
            events: list[str] = []
            real_fsync = os.fsync
            real_replace = Path.replace

            def record_fsync(descriptor: int) -> None:
                events.append("directory_fsync")
                real_fsync(descriptor)

            def record_replace(source: Path, destination: Path) -> Path:
                events.append("replace")
                return real_replace(source, destination)

            with (
                patch("src.me_finder.import_resume.os.fsync", side_effect=record_fsync),
                patch.object(Path, "replace", autospec=True, side_effect=record_replace),
            ):
                quarantined = quarantine_corrupt_manifest(damaged)

            self.assertEqual(events, ["replace", "directory_fsync"])
            self.assertIsNotNone(quarantined)
            self.assertFalse(damaged.exists())
            self.assertTrue(quarantined.is_file())

    def test_journal_delete_syncs_directory_and_propagates_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            journal = ImportJobJournal(directory)
            first = directory / "first.json"
            first.write_text("{}", encoding="utf-8")
            events: list[str] = []
            real_fsync = os.fsync
            real_unlink = Path.unlink

            def record_fsync(descriptor: int) -> None:
                events.append("directory_fsync")
                real_fsync(descriptor)

            def record_unlink(path: Path, *args: object, **kwargs: object) -> None:
                events.append("unlink")
                real_unlink(path, *args, **kwargs)

            with (
                patch("src.me_finder.import_resume.os.fsync", side_effect=record_fsync),
                patch.object(Path, "unlink", autospec=True, side_effect=record_unlink),
            ):
                self.assertTrue(journal.delete_job("first"))

            self.assertEqual(events, ["unlink", "directory_fsync"])
            self.assertFalse(first.exists())

            second = directory / "second.json"
            second.write_text("{}", encoding="utf-8")
            with patch(
                "src.me_finder.import_job_journal.fsync_directory",
                side_effect=OSError("directory sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    journal.delete_job("second")
            self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
