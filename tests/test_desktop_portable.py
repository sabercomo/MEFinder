from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import desktop
from src.me_finder.search import SearchEngine
from tools.create_empty_index import create_empty_index
from tools.create_portable_zip import create_portable_zip


class DesktopPortableTests(unittest.TestCase):
    def test_optional_vision_credentials_use_local_private_config(self) -> None:
        desktop_source = Path("desktop.py").read_text(encoding="utf-8")
        release_source = Path("build_portable_release.ps1").read_text(encoding="utf-8")
        self.assertIn("ME_FINDER_VISION_CONFIG", desktop_source)
        self.assertIn("vision_api.local.json", desktop_source)
        self.assertIn('"vision_api.local.json"', release_source)

    def test_portable_marker_keeps_frozen_runtime_beside_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / desktop.PORTABLE_MARKER).touch()
            with mock.patch.object(desktop.sys, "frozen", True, create=True):
                self.assertTrue(desktop.is_portable_bundle(bundle))
                self.assertEqual(desktop.prepare_runtime_root(bundle), bundle)
                self.assertEqual(desktop.webview_storage_path(bundle, True), str(bundle / "webview-data"))

    def test_public_blank_index_opens_as_an_empty_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            create_empty_index(database)
            engine = SearchEngine(database)
            try:
                self.assertEqual(engine.index["metadata"]["source_count"], 0)
                self.assertEqual(engine.index["source_files"], [])
                self.assertEqual(engine.search("任意文本")["total"], 0)
            finally:
                engine.close()

    def test_portable_zip_uses_standard_paths_and_one_top_level_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "MEFinder-test-windows-portable"
            (source / "config").mkdir(parents=True)
            (source / "portable.flag").touch()
            (source / "config" / "pdf_imports.json").write_text('{"documents": []}', encoding="utf-8")
            target = root / "release.zip"
            create_portable_zip(source, target)

            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
            self.assertTrue(all("\\" not in name for name in names))
            self.assertTrue(all(name.startswith(source.name + "/") for name in names))


if __name__ == "__main__":
    unittest.main()
