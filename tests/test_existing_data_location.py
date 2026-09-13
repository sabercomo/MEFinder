"""An existing library must be selected without copying over either corpus."""

import hashlib
import os
from unittest.mock import patch
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.me_finder.data_location import (
    DataLocationError, inspect_existing_data_root, switch_data_root, read_data_root,
)
from src.me_finder.application.data_root_admission import DataRootAdmissionError
from src.me_finder.database import build_database
from src.me_finder.persistence.index_schema import DATABASE_SCHEMA_VERSION
from tests.test_data_location import _make_web_runtime, _request_json


class ExistingDataLocationTests(unittest.TestCase):
    def test_switch_changes_only_the_pointer_and_accepts_named_existing_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current, target = base / "current", base / "synced-windows-library"
            database = target / "runtime/data/index.sqlite3"
            build_database({"metadata": {}, "source_files": [{"source_file_id": "book", "source_type": "pdf"}]}, database)
            preferences = target / "preferences.json"
            preferences.write_text('{"theme":"midnight"}')
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            inspected = inspect_existing_data_root(target)
            self.assertEqual(inspected["document_count"], 1)
            self.assertFalse(current.exists())
            switched = switch_data_root(current, target, current)
            self.assertTrue(switched["restart_required"])
            self.assertEqual(read_data_root(current), target.resolve())
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
            self.assertEqual(preferences.read_text(), '{"theme":"midnight"}')
            self.assertEqual(list(current.iterdir()), [current / "data_root.txt"])

    def test_invalid_missing_foreign_and_future_database_never_change_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = base / "current"
            current.mkdir()
            marker = current / "data_root.txt"
            marker.write_text(str(current))
            with self.assertRaises(DataLocationError):
                switch_data_root(current, base / "missing", current)
            database = base / "foreign/runtime/data/index.sqlite3"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
            with self.assertRaises(DataLocationError):
                switch_data_root(current, base / "foreign", current)
            build_database({"metadata": {}}, database)
            with sqlite3.connect(database) as connection:
                connection.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(DataLocationError, "更新版本"):
                switch_data_root(current, base / "foreign", current)
            self.assertEqual(marker.read_text(), str(current))

    @patch.dict(os.environ, {"ME_FINDER_DESKTOP_SHELL": "macos"})
    def test_http_existing_selection_and_switch_use_real_library_without_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "OneDrive/MEFinder"
            db = target / "runtime/data/index.sqlite3"
            build_database({"metadata": {}, "source_files": []}, db)
            app, runtime, handler, server, thread = _make_web_runtime(
                base, native_directory_chooser=lambda: str(target),
            )
            try:
                thread.start()
                status, selected = _request_json(server, "POST", "/api/data-location/choose", {"mode": "existing"})
                self.assertEqual(status, 200)
                self.assertEqual(selected["target_path"], str(target.resolve()))
                self.assertIn("document_count", selected)
                code, upload = _request_json(server, "POST", "/api/import-upload/start",
                    {"file_name": "pending.pdf", "size": 10, "parse_mode": "auto", "provider_id": ""})
                self.assertEqual(code, 200, upload)
                code, blocked = _request_json(server, "POST", "/api/data-location/switch", {"target_path": str(target)})
                self.assertEqual(code, 409, blocked)
                self.assertFalse((app / "data_root.txt").exists())
                code, cancelled = _request_json(server, "POST", "/api/import-upload/cancel", {"upload_id": upload["upload_id"]})
                self.assertEqual(code, 200, cancelled)
                for payload in ([], {"mode": []}, {"mode": "unknown"}):
                    code, error = _request_json(server, "POST", "/api/data-location/choose", payload)
                    self.assertEqual(code, 400, error)
                status, response = _request_json(server, "POST", "/api/data-location/switch", {"target_path": str(target)})
                self.assertEqual(status, 200, response)
                self.assertEqual(read_data_root(app), target.resolve())
                self.assertTrue((runtime / "data/index.sqlite3").exists())
                self.assertTrue(response["restart_required"])
                code, reloaded = _request_json(server, "GET", "/api/data-location")
                self.assertEqual(code, 200)
                self.assertTrue(reloaded["restart_required"])
                self.assertEqual(reloaded["pending_path"], str(target.resolve()))
                with self.assertRaisesRegex(DataRootAdmissionError, "重启"):
                    with handler.data_root_admission.operation():
                        self.fail("old runtime accepted a write after switching")
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
