from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from src.me_finder.backup_service import create_backup, restore_backup
from src.me_finder.database import build_database
from src.me_finder.document_groups import add_group_member, create_document_group
from src.me_finder.pdf_import_service import (
    load_import_config,
    locked_import_config,
    save_import_config,
)


def _seed_runtime(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "pdf_imports.json").write_text(
        json.dumps({"documents": [{"source_file_id": "pdf-x", "page_mapping": {"segments": [1]}}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "config" / "local_ocr.json").write_text(
        json.dumps(
            {
                "engines": {
                    "ndlocr-lite": {
                        "enabled": True,
                        "python_path": "/Users/private-name/ocr/venv/python",
                        "script_path": "/Users/private-name/ocr/src/ocr.py",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    component = root / "components" / "local-ocr" / "ndlocr-lite"
    component.mkdir(parents=True)
    (component / "installed.json").write_text(
        '{"tag":"1.2.3"}', encoding="utf-8"
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


def _backup_with_config(config: object) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "backup.json",
            json.dumps(
                {
                    "marker": "me_finder_backup",
                    "version": 1,
                    "created_at": "2026-08-09T00:00:00+00:00",
                }
            ),
        )
        archive.writestr(
            "config/pdf_imports.json",
            json.dumps(config, ensure_ascii=False),
        )
    return buffer.getvalue()


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
            self.assertIn("config/document_groups.json", names)
            self.assertIn("corpus/processed/mineru/manifests/segments-x.json", names)
            self.assertIn("corpus/processed/vision/manifests/vision-x.json", names)
            self.assertIn("preferences.json", names)
            self.assertIn("backup.json", names)
            self.assertNotIn("corpus/processed/mineru/results/huge.bin", names)
            self.assertNotIn("corpus/processed/vision/manifests/work/active.json", names)
            self.assertNotIn("config/local_ocr.json", names)
            self.assertFalse(any(name.startswith("components/local-ocr/") for name in names))
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

    def test_backup_captures_document_group_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            root.mkdir()
            index_path = root / "data" / "index.sqlite3"
            build_database(
                {
                    "metadata": {},
                    "source_files": [
                        {
                            "source_file_id": "pdf-x",
                            "source_type": "pdf",
                            "file_name": "x.pdf",
                        }
                    ],
                    "volumes": [],
                    "works": [],
                    "paragraphs": [],
                },
                index_path,
            )
            group_id = create_document_group("作品", index_path)[
                "document_group_id"
            ]
            add_group_member(group_id, "pdf-x", index_path, version_label="原版")

            archive = create_backup(root, index_path=index_path)
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                snapshot = json.loads(zipped.read("config/document_groups.json"))
                manifest = json.loads(zipped.read("backup.json"))

            self.assertEqual(manifest["version"], 2)
            self.assertEqual(snapshot["document_groups"][0]["title"], "作品")
            self.assertEqual(
                snapshot["document_group_members"][0]["version_label"], "原版"
            )

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
            self.assertIn("config/document_groups.json", summary["restored"])
            self.assertEqual(
                summary["document_group_snapshot"],
                {"document_groups": [], "document_group_members": []},
            )
            restored = json.loads((dest / "config" / "pdf_imports.json").read_text(encoding="utf-8"))
            self.assertEqual(restored["documents"][0]["source_file_id"], "pdf-x")
            self.assertTrue((dest / "config" / "pdf_imports.json.pre-restore").exists())
            self.assertEqual(
                json.loads(
                    (dest / "config" / "pdf_imports.json.bak").read_text(
                        encoding="utf-8"
                    )
                ),
                restored,
            )
            self.assertTrue((dest / "corpus" / "processed" / "mineru" / "manifests" / "segments-x.json").exists())
            self.assertTrue((dest / "corpus" / "processed" / "vision" / "manifests" / "vision-x.json").exists())
            self.assertEqual(
                json.loads((dest_app_data / "preferences.json").read_text(encoding="utf-8"))["theme"], "midnight"
            )
            self.assertEqual(
                list(dest.rglob("*.restore-*.tmp")),
                [],
            )

    def test_restore_normalizes_duplicate_config_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = _backup_with_config(
                {
                    "documents": [
                        {
                            "source_file_id": "pdf-duplicate",
                            "file_name": "first.pdf",
                            "title": "First",
                        },
                        {
                            "source_file_id": "pdf-duplicate",
                            "file_name": "second.pdf",
                            "author": "Author",
                        },
                    ]
                }
            )

            restore_backup(root, archive)

            restored = load_import_config(
                root / "config" / "pdf_imports.json"
            )
            self.assertEqual(len(restored["documents"]), 1)
            self.assertEqual(restored["documents"][0]["title"], "First")
            self.assertEqual(restored["documents"][0]["author"], "Author")

    def test_restore_participates_in_shared_config_transaction_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config" / "pdf_imports.json"
            save_import_config(
                config_path,
                {"documents": [{"source_file_id": "old"}]},
            )
            archive = _backup_with_config(
                {"documents": [{"source_file_id": "restored"}]}
            )
            mutation_holds_lock = threading.Event()
            release_mutation = threading.Event()
            restore_started = threading.Event()
            restore_finished = threading.Event()
            errors: list[BaseException] = []

            def stale_mutation() -> None:
                try:
                    with locked_import_config(config_path) as config:
                        mutation_holds_lock.set()
                        release_mutation.wait(timeout=2)
                        config["documents"].append(
                            {"source_file_id": "concurrent"}
                        )
                        save_import_config(config_path, config)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            def restore() -> None:
                restore_started.set()
                try:
                    restore_backup(root, archive)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                finally:
                    restore_finished.set()

            mutation_thread = threading.Thread(target=stale_mutation)
            restore_thread = threading.Thread(target=restore)
            mutation_thread.start()
            self.assertTrue(mutation_holds_lock.wait(timeout=2))
            restore_thread.start()
            self.assertTrue(restore_started.wait(timeout=2))
            self.assertFalse(restore_finished.wait(timeout=0.05))
            release_mutation.set()
            mutation_thread.join(timeout=2)
            restore_thread.join(timeout=2)

            self.assertFalse(mutation_thread.is_alive())
            self.assertFalse(restore_thread.is_alive())
            self.assertEqual(errors, [])
            restored = load_import_config(config_path)
            self.assertEqual(
                [item["source_file_id"] for item in restored["documents"]],
                ["restored"],
            )

    def test_restore_rejects_non_object_import_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "JSON 对象"):
                restore_backup(
                    Path(temp_dir),
                    _backup_with_config([{"source_file_id": "invalid"}]),
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

    def test_restore_accepts_v1_backup_without_group_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = restore_backup(
                Path(temp_dir), _backup_with_config({"documents": []})
            )
            self.assertIsNone(summary["document_group_snapshot"])


if __name__ == "__main__":
    unittest.main()
