from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from src.me_finder.app_context import AppPaths
from src.me_finder.application.backup_coordinator import (
    BackupCoordinator,
    BackupQueueError,
)
from src.me_finder.import_queue import ImportQueueFullError
from src.me_finder.mineru_api import MinerUError


class FakeIndex:
    def __init__(self, events):
        self.events = events

    @contextmanager
    def mutation(self):
        self.events.append("index-enter")
        try:
            yield
        finally:
            self.events.append("index-exit")


class FakeDurable:
    def __init__(self, events):
        self.events = events

    @contextmanager
    def operation(self):
        self.events.append("durable-enter")
        try:
            yield
        finally:
            self.events.append("durable-exit")


class FakeJobs:
    def __init__(self, events):
        self.events = events
        self.registered = []
        self.updates = []
        self.submit_error = None

    def register_background_job(self, job):
        self.registered.append(dict(job))

    def submit_background_task(self, task, *args):
        if self.submit_error:
            raise self.submit_error
        task(*args)

    def rebuild_runtime_index(self, job_id):
        self.events.append("rebuild")
        return set()

    def update_import_job(self, job_id, **updates):
        self.updates.append((job_id, dict(updates)))


class BackupCoordinatorTests(unittest.TestCase):
    def _coordinator(
        self,
        root,
        *,
        write=None,
        restore=None,
        restore_groups=None,
        restore_alignments=None,
    ):
        events = []
        jobs = FakeJobs(events)

        @contextmanager
        def config_lock():
            events.append("config-enter")
            try:
                yield
            finally:
                events.append("config-exit")

        kwargs = {
            "app_data_root": lambda: root / "app-data",
            "config_lock": config_lock,
        }
        if write is not None:
            kwargs["write"] = write
        if restore is not None:
            kwargs["restore"] = restore
        if restore_groups is not None:
            kwargs["restore_groups"] = restore_groups
        if restore_alignments is not None:
            kwargs["restore_alignments"] = restore_alignments
        coordinator = BackupCoordinator(
            AppPaths.create(root),
            FakeIndex(events),
            FakeDurable(events),
            jobs,
            **kwargs,
        )
        return coordinator, jobs, events

    def test_export_uses_app_data_backup_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app-data" / "backups" / "backup.zip"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"zip")
            calls = []

            def write(runtime_root, destination, *, app_data_root, index_path):
                calls.append((runtime_root, destination, app_data_root, index_path))
                return target

            coordinator, _jobs, _events = self._coordinator(
                root,
                write=write,
            )
            result = coordinator.export()

            self.assertEqual(result["path"], str(target))
            self.assertEqual(result["size_bytes"], 3)
            self.assertEqual(calls[0][0], root.resolve())
            self.assertEqual(calls[0][1], target.parent)
            self.assertEqual(calls[0][2], root / "app-data")
            self.assertEqual(
                calls[0][3],
                (root / "data" / "index.sqlite3").resolve(),
            )

    def test_export_uses_selected_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            selected.mkdir()
            target = selected / "backup.zip"
            target.write_bytes(b"zip")
            destinations = []

            def write(_runtime_root, destination, *, app_data_root, index_path):
                destinations.append((destination, app_data_root, index_path))
                return target

            coordinator, _jobs, _events = self._coordinator(root, write=write)
            result = coordinator.export(output_dir=selected)

            self.assertEqual(result["path"], str(target))
            self.assertEqual(
                destinations,
                [
                    (
                        selected,
                        root / "app-data",
                        (root / "data" / "index.sqlite3").resolve(),
                    )
                ],
            )

    def test_restore_preserves_lock_order_and_job_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup.zip"
            source.write_bytes(b"archive")

            def restore(runtime_root, payload, *, app_data_root):
                events.append("restore")
                self.assertEqual(payload, b"archive")
                return {
                    "count": 3,
                    "document_group_snapshot": {
                        "document_groups": [],
                        "document_group_members": [],
                    },
                    "alignment_snapshot": {"alignment_pairs": []},
                }

            def restore_groups(snapshot, index_path):
                events.append("restore-groups")
                self.assertEqual(snapshot["document_groups"], [])
                self.assertEqual(
                    index_path,
                    (root / "data" / "index.sqlite3").resolve(),
                )

            def restore_alignments(snapshot, index_path):
                events.append("restore-alignments")
                self.assertEqual(snapshot["alignment_pairs"], [])
                self.assertEqual(
                    index_path,
                    (root / "data" / "index.sqlite3").resolve(),
                )
                return 0

            coordinator, jobs, events = self._coordinator(
                root,
                restore=restore,
                restore_groups=restore_groups,
                restore_alignments=restore_alignments,
            )
            job_id = coordinator.start_restore(str(source))

            self.assertTrue(job_id.startswith("restore-"))
            self.assertEqual(
                events,
                [
                    "durable-enter",
                    "index-enter",
                    "config-enter",
                    "restore",
                    "rebuild",
                    "restore-groups",
                    "restore-alignments",
                    "config-exit",
                    "index-exit",
                    "durable-exit",
                ],
            )
            self.assertEqual(jobs.updates[-1][1]["status"], "completed")

    def test_v1_restore_keeps_existing_document_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup.zip"
            source.write_bytes(b"archive")
            calls = []

            def restore(*_args, **_kwargs):
                return {"count": 1, "document_group_snapshot": None}

            def restore_groups(*args):
                calls.append(args)

            coordinator, _jobs, _events = self._coordinator(
                root, restore=restore, restore_groups=restore_groups
            )
            coordinator.start_restore(str(source))

            self.assertEqual(calls, [])

    def test_restore_failure_is_visible_on_background_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup.zip"
            source.write_bytes(b"archive")

            def restore(*_args, **_kwargs):
                raise ValueError("invalid backup")

            coordinator, jobs, _events = self._coordinator(
                root,
                restore=restore,
            )
            coordinator.start_restore(str(source))

            self.assertEqual(jobs.updates[-1][1]["status"], "failed")
            self.assertIn("invalid backup", jobs.updates[-1][1]["message"])

    def test_queue_failure_marks_job_failed_and_keeps_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backup.zip"
            source.write_bytes(b"archive")
            coordinator, jobs, _events = self._coordinator(root)
            jobs.submit_error = ImportQueueFullError("full")

            with self.assertRaises(BackupQueueError) as raised:
                coordinator.start_restore(str(source))

            self.assertEqual(
                str(raised.exception),
                "备份恢复任务暂时无法启动，文件未更改。",
            )
            self.assertEqual(jobs.updates[-1][1]["phase"], "queue_failed")

    def test_restore_validates_file_and_extension_before_registering_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator, jobs, _events = self._coordinator(root)
            with self.assertRaisesRegex(MinerUError, "不存在"):
                coordinator.start_restore(str(root / "missing.zip"))
            text = root / "backup.txt"
            text.write_text("not zip", encoding="utf-8")
            with self.assertRaisesRegex(MinerUError, "\\.zip"):
                coordinator.start_restore(str(text))
            self.assertEqual(jobs.registered, [])


if __name__ == "__main__":
    unittest.main()
