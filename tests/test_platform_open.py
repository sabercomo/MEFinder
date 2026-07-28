from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.preferences import save_preferences
from src.me_finder import web


class PlatformOpenTests(unittest.TestCase):
    def test_macos_uses_open_command(self) -> None:
        target = Path("/Users/example/Documents/source.pdf")
        with (
            mock.patch.object(web.sys, "platform", "darwin"),
            mock.patch.object(web.subprocess, "Popen") as popen,
        ):
            web.open_path_with_default_app(target)
        popen.assert_called_once_with(["open", str(target)], close_fds=True)

    def test_windows_uses_startfile(self) -> None:
        target = Path("C:/Documents/source.pdf")
        with (
            mock.patch.object(web.sys, "platform", "win32"),
            mock.patch.object(web.os, "startfile", create=True) as startfile,
        ):
            web.open_path_with_default_app(target)
        startfile.assert_called_once_with(str(target))

    def test_linux_uses_xdg_open(self) -> None:
        target = Path("/home/example/source.pdf")
        with (
            mock.patch.object(web.sys, "platform", "linux"),
            mock.patch.object(web.subprocess, "Popen") as popen,
        ):
            web.open_path_with_default_app(target)
        popen.assert_called_once_with(["xdg-open", str(target)], close_fds=True)

    def test_adobe_probe_is_windows_only(self) -> None:
        with mock.patch.object(web.sys, "platform", "darwin"):
            self.assertIsNone(web.find_adobe_pdf_app())

    def test_macos_native_reader_receives_one_based_physical_page(self) -> None:
        target = Path("/Users/example/Documents/中文 文献.pdf")
        opener = mock.Mock(return_value={"page": 17, "page_count": 80})
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(web.sys, "platform", "darwin"),
        ):
            result = web.open_pdf_with_platform(
                target,
                "17",
                preferences_path=Path(temp_dir) / "preferences.json",
                native_pdf_opener=opener,
            )

        opener.assert_called_once_with(target, 17)
        self.assertEqual(result["app"], "pdfkit")
        self.assertEqual(result["viewer_mode"], "native")
        self.assertTrue(result["page_jump"])
        self.assertEqual(result["page"], 17)
        self.assertEqual(result["page_count"], 80)
        self.assertFalse(result["page_adjusted"])

    def test_native_reader_reports_the_actual_clamped_page(self) -> None:
        target = Path("/Users/example/Documents/source.pdf")
        opener = mock.Mock(return_value={"page": 10, "page_count": 10})
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(web.sys, "platform", "darwin"),
        ):
            result = web.open_pdf_with_platform(
                target,
                99,
                preferences_path=Path(temp_dir) / "preferences.json",
                native_pdf_opener=opener,
            )

        self.assertEqual(result["requested_page"], 99)
        self.assertEqual(result["page"], 10)
        self.assertEqual(result["page_count"], 10)
        self.assertTrue(result["page_adjusted"])

    def test_macos_system_mode_opens_preview_without_claiming_page_jump(self) -> None:
        target = Path("/Users/example/Documents/source.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            preferences_path = Path(temp_dir) / "preferences.json"
            save_preferences({"pdf_open_mode": "system"}, preferences_path)
            with (
                mock.patch.object(web.sys, "platform", "darwin"),
                mock.patch.object(web, "open_path_in_macos_preview") as preview,
            ):
                result = web.open_pdf_with_platform(
                    target,
                    8,
                    preferences_path=preferences_path,
                    native_pdf_opener=mock.Mock(),
                )

        preview.assert_called_once_with(target)
        self.assertEqual(result["app"], "preview")
        self.assertFalse(result["page_jump"])
        self.assertEqual(result["page"], 8)

    def test_macos_native_reader_failure_falls_back_to_preview(self) -> None:
        target = Path("/Users/example/Documents/source.pdf")
        opener = mock.Mock(side_effect=RuntimeError("reader failed"))
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(web.sys, "platform", "darwin"),
            mock.patch.object(web, "open_path_in_macos_preview") as preview,
        ):
            result = web.open_pdf_with_platform(
                target,
                3,
                preferences_path=Path(temp_dir) / "preferences.json",
                native_pdf_opener=opener,
            )

        preview.assert_called_once_with(target)
        self.assertTrue(result["fallback"])
        self.assertFalse(result["page_jump"])

    def test_windows_native_mode_keeps_adobe_page_jump_fallback(self) -> None:
        target = Path("C:/Documents/source.pdf")
        adobe = Path("C:/Program Files/Adobe/Acrobat.exe")
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(web.sys, "platform", "win32"),
            mock.patch.object(web, "find_adobe_pdf_app", return_value=adobe),
            mock.patch.object(web.subprocess, "Popen") as popen,
        ):
            result = web.open_pdf_with_platform(
                target,
                29,
                preferences_path=Path(temp_dir) / "preferences.json",
            )

        popen.assert_called_once_with(
            [str(adobe), "/A", "page=29", str(target)],
            close_fds=True,
        )
        self.assertTrue(result["page_jump"])
        self.assertEqual(result["viewer_mode"], "adobe")

    def test_windows_native_reader_uses_webview2_before_adobe_fallback(self) -> None:
        target = Path("C:/Documents/source.pdf")
        opener = mock.Mock(return_value={"page": 12, "page_count": 240})
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            mock.patch.object(web.sys, "platform", "win32"),
            mock.patch.object(web, "find_adobe_pdf_app") as find_adobe,
        ):
            result = web.open_pdf_with_platform(
                target,
                "12",
                preferences_path=Path(temp_dir) / "preferences.json",
                native_pdf_opener=opener,
            )

        opener.assert_called_once_with(target, 12)
        find_adobe.assert_not_called()
        self.assertEqual(result["app"], "webview2")
        self.assertEqual(result["viewer_mode"], "native")
        self.assertTrue(result["page_jump"])
        self.assertEqual(result["page"], 12)
        self.assertEqual(result["page_count"], 240)

    def test_windows_system_preference_uses_default_pdf_app_without_page_jump(self) -> None:
        target = Path("C:/Documents/source.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            preferences_path = Path(temp_dir) / "preferences.json"
            save_preferences({"pdf_open_mode": "system"}, preferences_path)
            with (
                mock.patch.object(web.sys, "platform", "win32"),
                mock.patch.object(web, "open_path_with_default_app") as open_default,
                mock.patch.object(web, "find_adobe_pdf_app") as find_adobe,
            ):
                result = web.open_pdf_with_platform(
                    target,
                    12,
                    preferences_path=preferences_path,
                    native_pdf_opener=mock.Mock(),
                )

        open_default.assert_called_once_with(target)
        find_adobe.assert_not_called()
        self.assertEqual(result["app"], "system_default")
        self.assertEqual(result["viewer_mode"], "system")
        self.assertFalse(result["page_jump"])
        self.assertEqual(result["page"], 12)


if __name__ == "__main__":
    unittest.main()
