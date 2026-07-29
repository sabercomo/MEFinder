from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder.pdf_import_service import scan_directories_for_documents


def touch_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")


class ScanSkipsMediaLibrariesTests(unittest.TestCase):
    """Scanning must not open the media libraries macOS guards behind TCC.

    Walking into them makes macOS raise "照片"/"Apple Music" permission
    prompts for data this app never uses.
    """

    def _scan(self, base: Path) -> list[str]:
        result = scan_directories_for_documents([str(base)], {})
        return [str(entry["name"]) for entry in result["entries"]]

    def test_photo_and_music_libraries_are_not_descended_into(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            touch_pdf(base / "论文.pdf")
            touch_pdf(base / "Pictures" / "Photos Library.photoslibrary" / "藏在图库里.pdf")
            touch_pdf(base / "Music" / "Music.musiclibrary" / "藏在音乐库里.pdf")
            touch_pdf(base / "影片.tvlibrary" / "藏在影片库里.pdf")

            names = self._scan(base)

        self.assertEqual(names, ["论文.pdf"])

    def test_dot_directories_and_app_bundles_are_skipped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            touch_pdf(base / "论文.pdf")
            touch_pdf(base / ".git" / "隐藏.pdf")
            touch_pdf(base / "某程序.app" / "Contents" / "内嵌.pdf")

            names = self._scan(base)

        self.assertEqual(names, ["论文.pdf"])

    def test_user_library_folder_is_skipped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            touch_pdf(home / "论文.pdf")
            touch_pdf(home / "Library" / "Caches" / "缓存.pdf")
            # A Library folder that is not directly in the home directory is
            # an ordinary folder and must still be scanned.
            touch_pdf(home / "文献" / "Library" / "正常子目录.pdf")

            with patch("src.me_finder.pdf_import_service.Path.home", return_value=home):
                names = self._scan(home)

        self.assertEqual(sorted(names), ["正常子目录.pdf", "论文.pdf"])

    def test_ordinary_nested_folders_are_still_scanned(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            touch_pdf(base / "顶层.pdf")
            touch_pdf(base / "一级" / "二级" / "深层.pdf")
            touch_pdf(base / "一级" / "文档.docx")

            names = self._scan(base)

        self.assertEqual(sorted(names), sorted(["顶层.pdf", "深层.pdf", "文档.docx"]))


if __name__ == "__main__":
    unittest.main()
