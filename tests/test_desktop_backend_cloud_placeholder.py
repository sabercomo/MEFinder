"""Cloud-placeholder index files must fail startup loudly, not hang forever.

OneDrive Files-On-Demand can demote the shared index.sqlite3 to a cloud-only
placeholder; the first SQLite read then blocks on a multi-gigabyte hydration
and the desktop window sits on the loading splash indefinitely. These tests
pin the detection helper and the start-time guard.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.me_finder import desktop_backend
from src.me_finder.desktop_backend import DesktopBackend, cloud_placeholder_hint

PLACEHOLDER_ATTRIBUTES = 0x00400000 | 0x00100000 | 0x00000400 | 0x00000200 | 0x20


class FakeStatResult:
    def __init__(self, attributes: int) -> None:
        self.st_file_attributes = attributes


class CloudPlaceholderHintTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "index.sqlite3"
        self.path.write_bytes(b"sqlite")

    def test_local_file_returns_none(self) -> None:
        self.assertIsNone(cloud_placeholder_hint(self.path))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(cloud_placeholder_hint(self.path.with_suffix(".gone")))

    def test_recall_attribute_returns_remediation(self) -> None:
        with mock.patch(
            "os.stat", return_value=FakeStatResult(PLACEHOLDER_ATTRIBUTES)
        ):
            hint = cloud_placeholder_hint(self.path)
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertIn(str(self.path), hint)
        self.assertIn("始终保留在此设备", hint)

    def test_stat_without_file_attributes_is_treated_as_local(self) -> None:
        class PosixStatResult:
            pass

        with mock.patch("os.stat", return_value=PosixStatResult()):
            self.assertIsNone(cloud_placeholder_hint(self.path))


class CloudPlaceholderStartGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.index = Path(self._temporary.name) / "index.sqlite3"
        self.index.write_bytes(b"sqlite")

    def test_start_shows_error_without_building_a_handler(self) -> None:
        created: list[str] = []
        errors: list[tuple[str, str]] = []
        backend = DesktopBackend(
            index_path=self.index,
            create_handler=lambda: created.append("handler") or object(),
        )
        with mock.patch.object(
            desktop_backend,
            "cloud_placeholder_hint",
            return_value="placeholder hint",
        ):
            started = backend.start(
                on_ready=lambda url: None,
                load_main_page=lambda url: None,
                show_error=lambda title, detail: errors.append((title, detail)),
            )
        self.assertFalse(started)
        self.assertEqual(created, [])
        self.assertEqual(errors[0][0], "索引数据库在云端,尚未同步到本机")
        self.assertEqual(errors[0][1], "placeholder hint")


if __name__ == "__main__":
    unittest.main()
