"""「上次导出」记录：只在导出成功后写入，且只保留后端能确认的事实。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.me_finder.archive_transfer_controller import build_archive_transfer_controller
from src.me_finder.preferences import (
    read_preferences,
    record_backup_export,
    save_preferences,
)


class FakeBackup:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def export(self, *, output_dir=None):
        if self.error is not None:
            raise self.error
        return {"ok": True, "path": "/data/backups/library.zip", "size_bytes": 2048}

    def start_restore(self, source_path):  # pragma: no cover - 未在本用例使用
        raise AssertionError("restore not expected")


class BackupExportRecordTests(unittest.TestCase):
    def _controller(self, backup: FakeBackup, recorded: list[dict]):
        return build_archive_transfer_controller(
            backup,
            database_path=Path("/index.sqlite3"),
            runtime_root=Path("/runtime"),
            document_output_dir=Path("/exports"),
            record_backup_export=lambda record: recorded.append(dict(record)),
        )

    def test_successful_export_records_path_and_timestamp(self) -> None:
        recorded: list[dict] = []
        status, payload = self._controller(FakeBackup(), recorded).export_backup(None)
        self.assertEqual(status, 200)
        self.assertIsInstance(payload["exported_at"], int)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["path"], "/data/backups/library.zip")
        self.assertEqual(recorded[0]["size_bytes"], 2048)
        self.assertEqual(recorded[0]["exported_at"], payload["exported_at"])

    def test_failed_export_records_nothing(self) -> None:
        recorded: list[dict] = []
        backup = FakeBackup()
        backup.error = OSError("磁盘已满")
        status, payload = self._controller(backup, recorded).export_backup(None)
        self.assertEqual(status, 500)
        self.assertIn("导出备份失败", payload["error"])
        self.assertEqual(recorded, [])

    def test_recording_failure_does_not_fail_the_export(self) -> None:
        def explode(record):
            raise OSError("偏好文件不可写")

        controller = build_archive_transfer_controller(
            FakeBackup(),
            database_path=Path("/index.sqlite3"),
            runtime_root=Path("/runtime"),
            document_output_dir=Path("/exports"),
            record_backup_export=explode,
        )
        status, payload = controller.export_backup(None)
        # 文件已经写出去了，记录失败不能反过来把导出报成失败
        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], "/data/backups/library.zip")

    def test_preference_round_trip_keeps_only_confirmed_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            self.assertIsNone(read_preferences(path)["last_backup_export"])
            record_backup_export(
                {"path": "/data/backups/library.zip", "exported_at": 1789000000, "size_bytes": 2048},
                path,
            )
            stored = read_preferences(path)["last_backup_export"]
            self.assertEqual(stored["file_name"], "library.zip")
            self.assertEqual(stored["exported_at"], 1789000000)
            self.assertEqual(stored["size_bytes"], 2048)

    def test_incomplete_record_is_rejected_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preferences.json"
            with self.assertRaises(ValueError):
                save_preferences({"last_backup_export": {"path": "/x.zip"}}, path)
            with self.assertRaises(ValueError):
                save_preferences({"last_backup_export": {"exported_at": 1789000000}}, path)
            self.assertIsNone(read_preferences(path)["last_backup_export"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
