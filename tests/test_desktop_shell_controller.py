from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src.me_finder.application.data_root_admission import (
    DataRootAdmissionError,
    DataRootAdmissionGate,
)
from src.me_finder.data_location import DataLocationError
from src.me_finder.desktop_shell_controller import DesktopShellController
from src.me_finder.mineru_api import MinerUError


class DesktopShellControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.update_service = mock.Mock()
        self.update_service.status.return_value = {"status": "idle"}
        self.update_service.check.return_value = {"status": "checked"}
        self.update_service.download.return_value = {"status": "downloaded"}
        self.update_service.install.return_value = {"status": "installing"}
        self.check_macos_update = mock.Mock(
            return_value={"status": "current", "current_version": "0.4.2"}
        )
        self.open_source = mock.Mock(
            return_value={"ok": True, "file": "source.pdf"}
        )
        self.open_cnki = mock.Mock()
        self.durable_operations = mock.Mock()
        self.durable_operations.operation.return_value = nullcontext()
        self.data_root_gate = DataRootAdmissionGate()
        self.has_active_uploads = mock.Mock(return_value=False)
        self.has_active_jobs = mock.Mock(return_value=False)
        self.runtime_mutation = mock.Mock(return_value=nullcontext())
        self.migrate_data_root = mock.Mock(
            return_value={"ok": True, "restart_required": True}
        )

    def _controller(self, **overrides: object) -> DesktopShellController:
        arguments = {
            "current_version": "0.4.2",
            "desktop_shell": "macos",
            "check_macos_update": self.check_macos_update,
            "open_source": self.open_source,
            "open_cnki": self.open_cnki,
            "durable_operations": self.durable_operations,
            "data_root_migration": self.data_root_gate.migration,
            "has_active_uploads": self.has_active_uploads,
            "has_active_jobs": self.has_active_jobs,
            "runtime_mutation": self.runtime_mutation,
            "migrate_data_root": self.migrate_data_root,
            "update_service": self.update_service,
            "app_data_root": Path("/current/MEFinder"),
            "default_app_data_root": Path("/default/MEFinder"),
        }
        arguments.update(overrides)
        return DesktopShellController(**arguments)

    def test_update_operations_forward_payloads_without_http_knowledge(self) -> None:
        controller = self._controller()

        self.assertEqual(controller.update_status(), (200, {"status": "idle"}))
        self.assertEqual(
            controller.check_for_updates({"auto_download": True}),
            (200, {"status": "checked"}),
        )
        self.assertEqual(
            controller.download_update(),
            (200, {"status": "downloaded"}),
        )
        self.assertEqual(
            controller.install_update({"confirm_token": "confirm-one"}),
            (200, {"status": "installing"}),
        )
        self.update_service.check.assert_called_once_with(auto_download=True)
        self.update_service.install.assert_called_once_with("confirm-one")

    def test_update_operations_keep_existing_unsupported_responses(self) -> None:
        controller = self._controller(update_service=None)
        unsupported = {"error": "当前运行方式不支持应用内更新。"}

        self.assertEqual(
            controller.update_status(),
            (
                200,
                {
                    "status": "unsupported",
                    "can_self_update": False,
                    "message": "当前运行方式不支持应用内更新。",
                },
            ),
        )
        self.assertEqual(controller.check_for_updates({}), (400, unsupported))
        self.assertEqual(controller.download_update(), (400, unsupported))
        self.assertEqual(controller.install_update({}), (400, unsupported))

    def test_macos_update_is_available_only_in_the_macos_shell(self) -> None:
        unsupported = self._controller(desktop_shell="win32").macos_update()

        self.assertEqual(
            unsupported,
            (
                404,
                {
                    "status": "unsupported",
                    "current_version": "0.4.2",
                    "update_available": False,
                    "message": "此更新入口仅用于 macOS 应用。",
                },
            ),
        )
        self.check_macos_update.assert_not_called()
        self.assertEqual(
            self._controller().macos_update(),
            (200, {"status": "current", "current_version": "0.4.2"}),
        )
        self.check_macos_update.assert_called_once_with("0.4.2")

    def test_data_location_is_available_for_configured_desktop_shells(self) -> None:
        current = Path("/current/MEFinder")
        default = Path("/default/MEFinder")

        status, payload = self._controller().data_location()

        self.assertEqual(status, 200)
        self.assertEqual(payload["current_path"], str(current.resolve()))
        self.assertEqual(payload["default_path"], str(default.resolve()))
        self.assertTrue(payload["is_custom"])
        windows_status, windows_payload = self._controller(
            desktop_shell="win32"
        ).data_location()
        self.assertEqual(windows_status, 200)
        self.assertEqual(windows_payload["current_path"], str(current.resolve()))
        self.assertEqual(
            self._controller(desktop_shell="").data_location(),
            (
                404,
                {
                    "available": False,
                    "error": "数据位置选择仅适用于已打包的桌面应用。",
                },
            ),
        )

    def test_portable_windows_keeps_data_location_disabled(self) -> None:
        controller = self._controller(
            desktop_shell="win32",
            app_data_root=None,
            default_app_data_root=None,
            native_directory_chooser=lambda: Path("C:/unused"),
        )

        self.assertEqual(controller.data_location()[0], 404)
        self.assertEqual(controller.choose_data_location()[0], 400)
        self.assertEqual(
            controller.migrate_data_location(
                {"target_path": "C:/external/MEFinder"}
            )[0],
            400,
        )
        self.migrate_data_root.assert_not_called()

    def test_scan_directory_picker_deduplicates_existing_folders(self) -> None:
        with TemporaryDirectory() as temporary:
            first = Path(temporary) / "马克思"
            second = Path(temporary) / "恩格斯"
            first.mkdir()
            second.mkdir()
            controller = self._controller(
                native_scan_directory_chooser=lambda: [first, first, second]
            )

            status, payload = controller.choose_scan_directories()

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "ok": True,
                "cancelled": False,
                "folder": str(first),
                "folders": [str(first), str(second)],
            },
        )

    def test_scan_directory_picker_maps_cancel_unavailable_and_errors(self) -> None:
        self.assertEqual(
            self._controller(
                native_scan_directory_chooser=lambda: None
            ).choose_scan_directories(),
            (200, {"ok": True, "cancelled": True}),
        )
        self.assertEqual(
            self._controller(
                native_scan_directory_chooser=None
            ).choose_scan_directories(),
            (400, {"error": "当前运行方式不支持打开文件夹选择器。"}),
        )
        chooser = mock.Mock(side_effect=RuntimeError("窗口尚未就绪"))
        self.assertEqual(
            self._controller(
                native_scan_directory_chooser=chooser
            ).choose_scan_directories(),
            (400, {"error": "窗口尚未就绪"}),
        )
        self.assertEqual(
            self._controller(
                native_scan_directory_chooser=lambda: "/missing/folder"
            ).choose_scan_directories(),
            (400, {"error": "所选路径不是文件夹。"}),
        )

    def test_data_location_picker_returns_the_selected_mefinder_folder(self) -> None:
        with TemporaryDirectory() as temporary:
            selected = Path(temporary)
            controller = self._controller(
                native_directory_chooser=lambda: selected
            )

            status, payload = controller.choose_data_location()

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "ok": True,
                "cancelled": False,
                "selected_folder": str(selected),
                "target_path": str(selected.resolve() / "MEFinder"),
            },
        )
        self.assertEqual(
            self._controller(
                native_directory_chooser=lambda: None
            ).choose_data_location(),
            (200, {"ok": True, "cancelled": True}),
        )

    def test_backup_picker_validates_selection_and_reports_cancel(self) -> None:
        with TemporaryDirectory() as temporary:
            backup = Path(temporary) / "MEFinder-backup.zip"
            backup.write_bytes(b"PK")
            controller = self._controller(
                native_backup_file_chooser=lambda: backup
            )

            self.assertEqual(
                controller.choose_backup_file(),
                (
                    200,
                    {
                        "ok": True,
                        "cancelled": False,
                        "path": str(backup),
                        "name": backup.name,
                    },
                ),
            )

        self.assertEqual(
            self._controller(
                native_backup_file_chooser=lambda: None
            ).choose_backup_file(),
            (200, {"ok": True, "cancelled": True}),
        )
        self.assertEqual(
            self._controller(
                native_backup_file_chooser=None
            ).choose_backup_file(),
            (400, {"error": "当前运行方式不支持选择备份文件。"}),
        )

    def test_backup_picker_rejects_invalid_files_and_maps_native_errors(self) -> None:
        with TemporaryDirectory() as temporary:
            text = Path(temporary) / "backup.txt"
            text.write_text("not a backup", encoding="utf-8")
            self.assertEqual(
                self._controller(
                    native_backup_file_chooser=lambda: text
                ).choose_backup_file(),
                (400, {"error": "请选择 .zip 备份文件。"}),
            )
            self.assertEqual(
                self._controller(
                    native_backup_file_chooser=lambda: Path(temporary) / "missing.zip"
                ).choose_backup_file(),
                (400, {"error": "所选路径不是文件。"}),
            )

        chooser = mock.Mock(side_effect=RuntimeError("窗口尚未就绪"))
        self.assertEqual(
            self._controller(
                native_backup_file_chooser=chooser
            ).choose_backup_file(),
            (500, {"error": "打开备份文件选择器失败：窗口尚未就绪"}),
        )

        with mock.patch.object(
            Path,
            "is_file",
            side_effect=OSError("network share unavailable"),
        ):
            self.assertEqual(
                self._controller(
                    native_backup_file_chooser=lambda: Path("Z:/backup.zip")
                ).choose_backup_file(),
                (
                    500,
                    {"error": "读取所选备份文件失败：network share unavailable"},
                ),
            )

    def test_desktop_export_directory_picker_returns_folder_and_cancel(self) -> None:
        with TemporaryDirectory() as temporary:
            selected = Path(temporary)
            for desktop_shell in ("macos", "win32"):
                with self.subTest(desktop_shell=desktop_shell):
                    controller = self._controller(
                        desktop_shell=desktop_shell,
                        native_export_directory_chooser=lambda: selected,
                    )
                    self.assertEqual(
                        controller.choose_export_directory(),
                        (
                            200,
                            {
                                "ok": True,
                                "cancelled": False,
                                "path": str(selected),
                            },
                        ),
                    )

        for desktop_shell in ("macos", "win32"):
            with self.subTest(desktop_shell=desktop_shell, selected=None):
                self.assertEqual(
                    self._controller(
                        desktop_shell=desktop_shell,
                        native_export_directory_chooser=lambda: None,
                    ).choose_export_directory(),
                    (200, {"ok": True, "cancelled": True}),
                )
        self.assertEqual(
            self._controller(
                desktop_shell="linux",
                native_export_directory_chooser=lambda: Path("/unused"),
            ).choose_export_directory(),
            (400, {"error": "当前运行方式不支持选择导出文件夹。"}),
        )

    def test_migration_closes_admission_before_rechecking_uploads_and_jobs(
        self,
    ) -> None:
        events = []

        @contextmanager
        def region(name: str):
            events.append(name + "-enter")
            try:
                yield
            finally:
                events.append(name + "-exit")

        durable_operations = mock.Mock()
        durable_operations.operation.side_effect = lambda: region("durable")

        def has_active_uploads() -> bool:
            events.append("active-uploads")
            return False

        def has_active_jobs() -> bool:
            events.append("active-jobs")
            return False

        def migrate(current: Path, target: Path, default: Path):
            events.append("migrate")
            self.assertEqual(current, Path("/current/MEFinder"))
            self.assertEqual(target, Path("/target/MEFinder"))
            self.assertEqual(default, Path("/default/MEFinder"))
            return {"ok": True, "restart_required": True}

        controller = self._controller(
            data_root_migration=lambda: region("admission"),
            durable_operations=durable_operations,
            runtime_mutation=lambda: region("runtime"),
            has_active_uploads=has_active_uploads,
            has_active_jobs=has_active_jobs,
            migrate_data_root=migrate,
        )

        response = controller.migrate_data_location(
            {"target_path": "/target/MEFinder"}
        )

        self.assertEqual(response, (200, {"ok": True, "restart_required": True}))
        self.assertEqual(
            events,
            [
                "admission-enter",
                "durable-enter",
                "runtime-enter",
                "active-uploads",
                "active-jobs",
                "migrate",
                "runtime-exit",
                "durable-exit",
                "admission-exit",
            ],
        )

    def test_migration_rejects_active_upload_and_reopens_admission(self) -> None:
        controller = self._controller(has_active_uploads=lambda: True)

        response = controller.migrate_data_location(
            {"target_path": "/target/MEFinder"}
        )

        self.assertEqual(
            response,
            (409, {"error": "文件正在上传，请完成或取消后再迁移。"}),
        )
        self.migrate_data_root.assert_not_called()
        with self.data_root_gate.operation():
            pass

    def test_successful_migration_seals_old_root_until_restart(self) -> None:
        controller = self._controller()

        response = controller.migrate_data_location(
            {"target_path": "/target/MEFinder"}
        )

        self.assertEqual(response[0], 200)
        with self.assertRaisesRegex(DataRootAdmissionError, "请重启应用"):
            with self.data_root_gate.operation():
                pass

    def test_migration_preserves_active_job_and_domain_error_statuses(self) -> None:
        migrate = mock.Mock()
        self.assertEqual(
            self._controller(
                has_active_jobs=lambda: True,
                migrate_data_root=migrate,
            ).migrate_data_location({"target_path": "/target/MEFinder"}),
            (409, {"error": "文献正在导入或索引正在更新，请完成后再迁移。"}),
        )
        migrate.assert_not_called()

        cases = (
            (DataLocationError("目标无效"), 400, "目标无效"),
            (OSError("磁盘不可写"), 500, "迁移数据失败：磁盘不可写"),
            (sqlite3.Error("数据库忙"), 500, "迁移数据失败：数据库忙"),
        )
        for error, expected_status, expected_message in cases:
            with self.subTest(error=error):
                status, payload = self._controller(
                    migrate_data_root=mock.Mock(side_effect=error)
                ).migrate_data_location({"target_path": "/target/MEFinder"})
                self.assertEqual(status, expected_status)
                self.assertEqual(payload, {"error": expected_message})

    def test_migration_maps_unavailable_runtime_to_conflict(self) -> None:
        migrate = mock.Mock(return_value=None)

        response = self._controller(
            migrate_data_root=migrate
        ).migrate_data_location({"target_path": "/target/MEFinder"})

        self.assertEqual(
            response,
            (409, {"error": "索引正在更新，请稍后再迁移。"}),
        )
        self.runtime_mutation.assert_called_once_with()
        migrate.assert_called_once_with(
            Path("/current/MEFinder"),
            Path("/target/MEFinder"),
            Path("/default/MEFinder"),
        )

    def test_open_source_preserves_success_and_error_responses(self) -> None:
        self.assertEqual(
            self._controller().open_source(
                {"source_id": "pdf-one", "page": 12}
            ),
            (200, {"ok": True, "file": "source.pdf"}),
        )
        self.open_source.assert_called_once_with("pdf-one", 12)
        self.assertEqual(
            self._controller().open_source({}),
            (400, {"error": "invalid request"}),
        )
        self.assertEqual(
            self._controller(
                open_source=mock.Mock(side_effect=MinerUError("文献不存在"))
            ).open_source({"source_id": "missing"}),
            (400, {"error": "文献不存在"}),
        )
        self.assertEqual(
            self._controller(
                open_source=mock.Mock(side_effect=RuntimeError("窗口失败"))
            ).open_source({"source_id": "pdf-one"}),
            (500, {"error": "打开原文失败：窗口失败"}),
        )

    def test_open_cnki_preserves_validation_and_os_error_responses(self) -> None:
        url = "https://oversea.cnki.net/kns8s/search?kw=test"
        self.assertEqual(
            self._controller().open_cnki({"url": url}),
            (200, {"ok": True}),
        )
        self.open_cnki.assert_called_once_with(url)
        self.assertEqual(
            self._controller().open_cnki({"url": url, "extra": True}),
            (400, {"error": "请求必须只包含 url。"}),
        )
        self.assertEqual(
            self._controller(
                open_cnki=mock.Mock(side_effect=ValueError("知网页面地址无效。"))
            ).open_cnki({"url": "https://invalid.example"}),
            (400, {"error": "知网页面地址无效。"}),
        )
        self.assertEqual(
            self._controller(
                open_cnki=mock.Mock(side_effect=OSError("无法启动浏览器"))
            ).open_cnki({"url": url}),
            (500, {"error": "打开知网页面失败：无法启动浏览器"}),
        )


if __name__ == "__main__":
    unittest.main()
