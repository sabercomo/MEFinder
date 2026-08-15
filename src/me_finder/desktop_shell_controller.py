"""Transport-neutral controller for desktop-shell operations."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple, Union

from .data_location import DataLocationError, data_location_summary, proposed_data_root
from .lifecycle import DurableOperationGate
from .mineru_api import MinerUError


ShellResponse = Tuple[int, Dict[str, object]]
NativeDirectoryChooser = Callable[[], Optional[Union[str, Path]]]
NativeExportDirectoryChooser = Callable[[], Optional[Union[str, Path]]]
NativeBackupFileChooser = Callable[[], Optional[Union[str, Path]]]
NativeScanDirectoryChooser = Callable[
    [], Optional[Union[str, Path, Sequence[Union[str, Path]]]]
]
CheckMacOSUpdate = Callable[[str], Dict[str, object]]
HasActiveJobs = Callable[[], bool]
HasActiveUploads = Callable[[], bool]
RuntimeMutation = Callable[[], AbstractContextManager[None]]
# The composition root runs migration through IndexRuntime's state/read lock;
# ``None`` means that the runtime cannot currently serve a consistent index.
MigrateDataRoot = Callable[[Path, Path, Path], Optional[Dict[str, object]]]
OpenSource = Callable[[str, object], Dict[str, object]]
OpenCNKI = Callable[[object], None]


class UpdateServicePort(Protocol):
    def status(self) -> Dict[str, object]:
        ...

    def check(self, *, auto_download: bool = False) -> Dict[str, object]:
        ...

    def download(self) -> Dict[str, object]:
        ...

    def install(self, confirm_token: object = None) -> Dict[str, object]:
        ...


class DesktopShellController:
    """Coordinate native desktop actions without knowing their HTTP routes."""

    def __init__(
        self,
        *,
        current_version: str,
        desktop_shell: str,
        check_macos_update: CheckMacOSUpdate,
        open_source: OpenSource,
        open_cnki: OpenCNKI,
        durable_operations: DurableOperationGate,
        data_root_migration: RuntimeMutation,
        has_active_uploads: HasActiveUploads,
        has_active_jobs: HasActiveJobs,
        runtime_mutation: RuntimeMutation,
        migrate_data_root: MigrateDataRoot,
        update_service: Optional[UpdateServicePort] = None,
        native_directory_chooser: Optional[NativeDirectoryChooser] = None,
        native_export_directory_chooser: Optional[NativeExportDirectoryChooser] = None,
        native_scan_directory_chooser: Optional[NativeScanDirectoryChooser] = None,
        native_backup_file_chooser: Optional[NativeBackupFileChooser] = None,
        app_data_root: Optional[Path] = None,
        default_app_data_root: Optional[Path] = None,
    ) -> None:
        self._current_version = current_version
        self._desktop_shell = desktop_shell.strip().lower()
        self._check_macos_update = check_macos_update
        self._open_source = open_source
        self._open_cnki = open_cnki
        self._durable_operations = durable_operations
        self._data_root_migration = data_root_migration
        self._has_active_uploads = has_active_uploads
        self._has_active_jobs = has_active_jobs
        self._runtime_mutation = runtime_mutation
        self._migrate_data_root = migrate_data_root
        self._update_service = update_service
        self._native_directory_chooser = native_directory_chooser
        self._native_export_directory_chooser = native_export_directory_chooser
        self._native_scan_directory_chooser = native_scan_directory_chooser
        self._native_backup_file_chooser = native_backup_file_chooser
        self._app_data_root = app_data_root
        self._default_app_data_root = default_app_data_root

    def update_status(self) -> ShellResponse:
        if self._update_service is None:
            return 200, {
                "status": "unsupported",
                "can_self_update": False,
                "message": "当前运行方式不支持应用内更新。",
            }
        return 200, self._update_service.status()

    def macos_update(self) -> ShellResponse:
        if self._desktop_shell != "macos":
            return 404, {
                "status": "unsupported",
                "current_version": self._current_version,
                "update_available": False,
                "message": "此更新入口仅用于 macOS 应用。",
            }
        return 200, self._check_macos_update(self._current_version)

    def data_location(self) -> ShellResponse:
        if (
            self._desktop_shell not in {"macos", "win32"}
            or self._app_data_root is None
            or self._default_app_data_root is None
        ):
            return 404, {
                "available": False,
                "error": "数据位置选择仅适用于已打包的桌面应用。",
            }
        return 200, data_location_summary(
            self._app_data_root,
            self._default_app_data_root,
        )

    def check_for_updates(self, payload: Mapping[str, object]) -> ShellResponse:
        if self._update_service is None:
            return 400, {"error": "当前运行方式不支持应用内更新。"}
        return 200, self._update_service.check(
            auto_download=payload.get("auto_download") is True
        )

    def download_update(self) -> ShellResponse:
        if self._update_service is None:
            return 400, {"error": "当前运行方式不支持应用内更新。"}
        return 200, self._update_service.download()

    def install_update(self, payload: Mapping[str, object]) -> ShellResponse:
        if self._update_service is None:
            return 400, {"error": "当前运行方式不支持应用内更新。"}
        return 200, self._update_service.install(payload.get("confirm_token"))

    def choose_scan_directories(self) -> ShellResponse:
        if self._native_scan_directory_chooser is None:
            return 400, {"error": "当前运行方式不支持打开文件夹选择器。"}
        try:
            selected_folders = self._native_scan_directory_chooser()
        except Exception as exc:
            return 400, {"error": str(exc) or "打开文件夹选择器失败。"}
        if not selected_folders:
            return 200, {"ok": True, "cancelled": True}
        if isinstance(selected_folders, (str, Path)):
            candidates = [selected_folders]
        else:
            candidates = list(selected_folders)
        folders = []
        seen_folders = set()
        for selected_folder in candidates:
            folder = Path(str(selected_folder))
            if not folder.is_dir():
                return 400, {"error": "所选路径不是文件夹。"}
            normalized = str(folder)
            if normalized in seen_folders:
                continue
            seen_folders.add(normalized)
            folders.append(normalized)
        if not folders:
            return 200, {"ok": True, "cancelled": True}
        return 200, {
            "ok": True,
            "cancelled": False,
            "folder": folders[0],
            "folders": folders,
        }

    def choose_data_location(self) -> ShellResponse:
        if (
            self._app_data_root is None
            or self._default_app_data_root is None
            or self._native_directory_chooser is None
        ):
            return 400, {"error": "当前运行方式不支持选择数据位置。"}
        try:
            selected_folder = self._native_directory_chooser()
            if not selected_folder:
                return 200, {"ok": True, "cancelled": True}
            target = proposed_data_root(selected_folder)
        except (DataLocationError, OSError) as exc:
            return 400, {"error": str(exc)}
        return 200, {
            "ok": True,
            "cancelled": False,
            "selected_folder": str(selected_folder),
            "target_path": str(target),
        }

    def choose_backup_file(self) -> ShellResponse:
        if self._native_backup_file_chooser is None:
            return 400, {"error": "当前运行方式不支持选择备份文件。"}
        try:
            selected_file = self._native_backup_file_chooser()
        except (OSError, RuntimeError) as exc:
            return 500, {"error": f"打开备份文件选择器失败：{exc}"}
        if not selected_file:
            return 200, {"ok": True, "cancelled": True}
        path = Path(str(selected_file))
        try:
            is_file = path.is_file()
        except OSError as exc:
            return 500, {"error": f"读取所选备份文件失败：{exc}"}
        if not is_file:
            return 400, {"error": "所选路径不是文件。"}
        if path.suffix.lower() != ".zip":
            return 400, {"error": "请选择 .zip 备份文件。"}
        return 200, {
            "ok": True,
            "cancelled": False,
            "path": str(path),
            "name": path.name,
        }

    def choose_export_directory(self) -> ShellResponse:
        if (
            self._desktop_shell != "win32"
            or self._native_export_directory_chooser is None
        ):
            return 400, {"error": "当前运行方式不支持选择导出文件夹。"}
        try:
            selected_directory = self._native_export_directory_chooser()
        except (OSError, RuntimeError) as exc:
            return 500, {"error": f"打开导出文件夹选择器失败：{exc}"}
        if not selected_directory:
            return 200, {"ok": True, "cancelled": True}
        path = Path(str(selected_directory))
        try:
            is_directory = path.is_dir()
        except OSError as exc:
            return 500, {"error": f"读取所选导出文件夹失败：{exc}"}
        if not is_directory:
            return 400, {"error": "所选路径不是文件夹。"}
        return 200, {
            "ok": True,
            "cancelled": False,
            "path": str(path),
        }

    def migrate_data_location(
        self,
        payload: Mapping[str, object],
    ) -> ShellResponse:
        if self._app_data_root is None or self._default_app_data_root is None:
            return 400, {"error": "当前运行方式不支持迁移数据位置。"}
        target_value = str(payload.get("target_path") or "").strip()
        if not target_value:
            return 400, {"error": "请先选择新的数据位置。"}
        try:
            with (
                self._data_root_migration(),
                self._durable_operations.operation(),
                self._runtime_mutation(),
            ):
                if self._has_active_uploads():
                    raise MinerUError(
                        "文件正在上传，请完成或取消后再迁移。"
                    )
                if self._has_active_jobs():
                    raise MinerUError(
                        "文献正在导入或索引正在更新，请完成后再迁移。"
                    )
                result = self._migrate_data_root(
                    self._app_data_root,
                    Path(target_value),
                    self._default_app_data_root,
                )
                if result is None:
                    raise MinerUError("索引正在更新，请稍后再迁移。")
        except MinerUError as exc:
            return 409, {"error": str(exc)}
        except DataLocationError as exc:
            return 400, {"error": str(exc)}
        except (OSError, sqlite3.Error) as exc:
            return 500, {"error": f"迁移数据失败：{exc}"}
        return 200, result

    def open_source(self, payload: Mapping[str, object]) -> ShellResponse:
        source_id = str(payload.get("source_id") or "")
        if not source_id:
            return 400, {"error": "invalid request"}
        try:
            result = self._open_source(source_id, payload.get("page"))
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"打开原文失败：{exc}"}
        return 200, result

    def open_cnki(self, payload: object) -> ShellResponse:
        if not isinstance(payload, dict) or set(payload) != {"url"}:
            return 400, {"error": "请求必须只包含 url。"}
        try:
            self._open_cnki(payload.get("url"))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except OSError as exc:
            return 500, {"error": f"打开知网页面失败：{exc}"}
        return 200, {"ok": True}
