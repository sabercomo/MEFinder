from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.me_finder.preferences_controller import PreferencesController


class FakeIndexRuntime:
    def catalog(self):
        return {
            "source_files": [
                {"file_name": "existing.pdf", "size_bytes": 123},
                {"source_file_id": "without-name"},
            ],
            "volumes": [],
            "works": [],
        }


class PreferencesControllerTests(unittest.TestCase):
    def test_read_and_scan_use_the_same_persisted_directories(self) -> None:
        scanner = Mock(return_value={"documents": [{"name": "new.pdf"}]})
        reader = Mock(
            return_value={
                "theme": "frost-blue",
                "scan_directories": ["/books"],
            }
        )
        controller = PreferencesController(
            Path("/runtime/config/preferences.json"),
            FakeIndexRuntime(),
            read=reader,
            save=Mock(),
            scan_directories=scanner,
        )

        self.assertEqual(controller.preferences()[0], 200)
        status, payload = controller.scan_directories()

        self.assertEqual(status, 200)
        self.assertEqual(payload["directories"], ["/books"])
        scanner.assert_called_once_with(
            ["/books"], {"existing.pdf": 123}
        )

    def test_save_returns_preferences_and_applies_native_theme(self) -> None:
        setter = Mock()
        saver = Mock(
            return_value={
                "theme": "midnight",
                "scan_directories": [],
            }
        )
        path = Path("/runtime/config/preferences.json")
        controller = PreferencesController(
            path,
            FakeIndexRuntime(),
            native_theme_setter=setter,
            read=Mock(),
            save=saver,
            scan_directories=Mock(),
        )

        status, payload = controller.save_preferences({"theme": "midnight"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["theme"], "midnight")
        self.assertTrue(payload["ok"])
        saver.assert_called_once_with({"theme": "midnight"}, path)
        setter.assert_called_once_with("midnight")

    def test_save_keeps_validation_and_storage_error_responses(self) -> None:
        invalid = PreferencesController(
            Path("preferences.json"),
            FakeIndexRuntime(),
            read=Mock(),
            save=Mock(side_effect=ValueError("不支持的主题")),
            scan_directories=Mock(),
        )
        unwritable = PreferencesController(
            Path("preferences.json"),
            FakeIndexRuntime(),
            read=Mock(),
            save=Mock(side_effect=OSError("read only")),
            scan_directories=Mock(),
        )

        self.assertEqual(
            invalid.save_preferences({"theme": "unknown"}),
            (400, {"error": "不支持的主题"}),
        )
        self.assertEqual(
            unwritable.save_preferences({"theme": "midnight"}),
            (
                500,
                {
                    "error": (
                        "应用设置无法保存，请检查配置目录"
                        "是否可写。"
                    )
                },
            ),
        )

    def test_native_theme_failure_is_logged_without_changing_saved_result(self) -> None:
        controller = PreferencesController(
            Path("preferences.json"),
            FakeIndexRuntime(),
            native_theme_setter=Mock(side_effect=RuntimeError("window closed")),
            read=Mock(),
            save=Mock(return_value={"theme": "midnight"}),
            scan_directories=Mock(),
        )

        with patch(
            "src.me_finder.preferences_controller.logging.exception"
        ) as logged:
            response = controller.save_preferences({"theme": "midnight"})

        self.assertEqual(response, (200, {"ok": True, "theme": "midnight"}))
        logged.assert_called_once_with("failed to apply native window theme")

    def test_scan_failure_keeps_existing_error_message(self) -> None:
        controller = PreferencesController(
            Path("preferences.json"),
            FakeIndexRuntime(),
            read=Mock(side_effect=OSError("unreadable")),
            save=Mock(),
            scan_directories=Mock(),
        )

        self.assertEqual(
            controller.scan_directories(),
            (500, {"error": "扫描文献目录失败：unreadable"}),
        )


if __name__ == "__main__":
    unittest.main()
