from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.me_finder import __version__
from tools.windows_version_info import (
    render_windows_version_info,
    write_windows_version_info,
)


class WindowsVersionInfoTests(unittest.TestCase):
    def test_version_metadata_uses_package_version(self) -> None:
        rendered = render_windows_version_info()

        self.assertIn(f"StringStruct(u'FileVersion', u'{__version__}')", rendered)
        self.assertIn(f"StringStruct(u'ProductVersion', u'{__version__}')", rendered)
        self.assertIn("StringStruct(u'OriginalFilename', u'文献原句定位器.exe')", rendered)

    def test_version_metadata_writes_build_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "version.txt"
            written = write_windows_version_info(target, "1.2.3")

            self.assertEqual(written, target)
            self.assertIn("filevers=(1, 2, 3, 0)", target.read_text(encoding="utf-8"))

    def test_sidecar_metadata_uses_its_executable_name(self) -> None:
        rendered = render_windows_version_info(
            file_description="MEFinder MCP Server",
            internal_name="MEFinderMCP",
            original_filename="MEFinderMCP.exe",
        )

        self.assertIn("StringStruct(u'FileDescription', u'MEFinder MCP Server')", rendered)
        self.assertIn("StringStruct(u'InternalName', u'MEFinderMCP')", rendered)
        self.assertIn("StringStruct(u'OriginalFilename', u'MEFinderMCP.exe')", rendered)

    def test_non_numeric_version_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render_windows_version_info("1.2.3-beta")


if __name__ == "__main__":
    unittest.main()
