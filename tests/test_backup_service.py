from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.me_finder.backup_service import create_backup, restore_backup


def _seed_runtime(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "pdf_imports.json").write_text(
        json.dumps({"documents": [{"source_file_id": "pdf-x", "page_mapping": {"segments": [1]}}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    manifests = root / "corpus" / "processed" / "mineru" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "segments-x.json").write_text('{"segments": []}', encoding="utf-8")
    # a large regenerable artifact that must NOT be captured
    results = root / "corpus" / "processed" / "mineru" / "results"
    results.mkdir(parents=True)
    (results / "huge.bin").write_bytes(b"0" * 4096)


class BackupServiceTests(unittest.TestCase):
    def test_backup_captures_curated_state_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            root.mkdir()
            _seed_runtime(root)
            app_data = Path(temp_dir)
            (app_data / "preferences.json").write_text('{"theme": "midnight"}', encoding="utf-8")

            archive = create_backup(root, app_data_root=app_data)
            names = set(zipfile.ZipFile(io.BytesIO(archive)).namelist())
            self.assertIn("config/pdf_imports.json", names)
            self.assertIn("corpus/processed/mineru/manifests/segments-x.json", names)
            self.assertIn("preferences.json", names)
            self.assertIn("backup.json", names)
            self.assertNotIn("corpus/processed/mineru/results/huge.bin", names)

    def test_restore_round_trip_and_pre_restore_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "runtime"
            src.mkdir()
            _seed_runtime(src)
            app_data = Path(temp_dir)
            (app_data / "preferences.json").write_text('{"theme": "midnight"}', encoding="utf-8")
            archive = create_backup(src, app_data_root=app_data)

            dest = Path(temp_dir) / "restored"
            (dest / "config").mkdir(parents=True)
            (dest / "config" / "pdf_imports.json").write_text('{"documents": []}', encoding="utf-8")
            dest_app_data = Path(temp_dir) / "dest_appdata"
            dest_app_data.mkdir()

            summary = restore_backup(dest, archive, app_data_root=dest_app_data)
            self.assertIn("config/pdf_imports.json", summary["restored"])
            restored = json.loads((dest / "config" / "pdf_imports.json").read_text(encoding="utf-8"))
            self.assertEqual(restored["documents"][0]["source_file_id"], "pdf-x")
            self.assertTrue((dest / "config" / "pdf_imports.json.pre-restore").exists())
            self.assertTrue((dest / "corpus" / "processed" / "mineru" / "manifests" / "segments-x.json").exists())
            self.assertEqual(
                json.loads((dest_app_data / "preferences.json").read_text(encoding="utf-8"))["theme"], "midnight"
            )

    def test_restore_rejects_non_backup_zip(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("random.txt", "not a backup")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                restore_backup(Path(temp_dir), buffer.getvalue())

    def test_restore_rejects_path_traversal(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("backup.json", json.dumps({"marker": "me_finder_backup", "version": 1}))
            archive.writestr("../evil.json", "pwned")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                restore_backup(Path(temp_dir), buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
