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
    (manifests / "segments-x.json").write_text(
        json.dumps(
            {
                "pdf_path": "/Users/private-name/paper.pdf",
                "manifest_path": str(manifests / "segments-x.json"),
                "segments": [
                    {
                        "state_file": "/Users/private-name/task.json",
                        "result_dir": "/Users/private-name/result",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    vision_manifests = root / "corpus" / "processed" / "vision" / "manifests"
    vision_manifests.mkdir(parents=True)
    (vision_manifests / "vision-x.json").write_text('{"segments": []}', encoding="utf-8")
    (vision_manifests / "work").mkdir()
    (vision_manifests / "work" / "active.json").write_text(
        '{"status": "processing"}',
        encoding="utf-8",
    )
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
            self.assertIn("corpus/processed/vision/manifests/vision-x.json", names)
            self.assertIn("preferences.json", names)
            self.assertIn("backup.json", names)
            self.assertNotIn("corpus/processed/mineru/results/huge.bin", names)
            self.assertNotIn("corpus/processed/vision/manifests/work/active.json", names)
            backed_manifest = zipfile.ZipFile(
                io.BytesIO(archive)
            ).read(
                "corpus/processed/mineru/manifests/segments-x.json"
            )
            self.assertNotIn(b"/Users/private-name", backed_manifest)
            portable = json.loads(backed_manifest)
            self.assertIsNone(portable["pdf_path"])
            self.assertEqual(
                portable["manifest_path"],
                "corpus/processed/mineru/manifests/segments-x.json",
            )
            self.assertIsNone(portable["segments"][0]["state_file"])
            self.assertIsNone(portable["segments"][0]["result_dir"])

    def test_backup_scrubs_foreign_absolute_paths_and_filters_lists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            root.mkdir()
            manifests = (
                root / "corpus" / "processed" / "mineru" / "manifests"
            )
            manifests.mkdir(parents=True)
            inside_result = (
                root / "corpus" / "processed" / "mineru" / "results" / "safe"
            )
            manifest = manifests / "segments-paths.json"
            manifest.write_text(
                json.dumps(
                    {
                        "pdf_path": r"C:\Users\private-name\paper.pdf",
                        "state_file": r"\\server\private-name\task.json",
                        "manifest_path": str(manifest),
                        "result_dirs": [
                            str(inside_result),
                            "/Users/private-name/result",
                            r"D:\private-name\result",
                            r"\\server\private-name\result",
                            "relative/result",
                            None,
                            {"not": "a path"},
                        ],
                        "nested": {
                            "work_manifest": "/home/private-name/work.json",
                            "downloaded_result_dirs": [
                                "portable/download",
                                r"E:\private-name\download",
                            ],
                            "result_dirs": r"F:\private-name\not-a-list",
                        },
                    }
                ),
                encoding="utf-8",
            )

            archive = create_backup(root)
            backed_manifest = zipfile.ZipFile(
                io.BytesIO(archive)
            ).read(
                "corpus/processed/mineru/manifests/segments-paths.json"
            )
            portable = json.loads(backed_manifest)

            self.assertIsNone(portable["pdf_path"])
            self.assertIsNone(portable["state_file"])
            self.assertEqual(
                portable["manifest_path"],
                "corpus/processed/mineru/manifests/segments-paths.json",
            )
            self.assertEqual(
                portable["result_dirs"],
                [
                    "corpus/processed/mineru/results/safe",
                    "relative/result",
                ],
            )
            self.assertIsNone(portable["nested"]["work_manifest"])
            self.assertEqual(
                portable["nested"]["downloaded_result_dirs"],
                ["portable/download"],
            )
            self.assertEqual(portable["nested"]["result_dirs"], [])
            self.assertNotIn(b"private-name", backed_manifest)

    def test_corrupt_manifest_is_replaced_by_anonymous_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            root.mkdir()
            manifests = (
                root / "corpus" / "processed" / "vision" / "manifests"
            )
            manifests.mkdir(parents=True)
            (manifests / "private-document.json").write_text(
                '{"secret": "private-name"',
                encoding="utf-8",
            )

            archive = create_backup(root)
            backed_manifest = zipfile.ZipFile(
                io.BytesIO(archive)
            ).read(
                "corpus/processed/vision/manifests/private-document.json"
            )

            self.assertEqual(
                json.loads(backed_manifest),
                {"backup_manifest_unreadable": True},
            )
            self.assertNotIn(b"private-name", backed_manifest)
            self.assertNotIn(b"private-document", backed_manifest)

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
            self.assertTrue((dest / "corpus" / "processed" / "vision" / "manifests" / "vision-x.json").exists())
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
