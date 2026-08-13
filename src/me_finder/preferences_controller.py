"""Transport-neutral JSON responses for preferences and directory scans."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from .application.index_runtime import IndexRuntime
from .pdf_import_service import scan_directories_for_documents
from .preferences import read_preferences, save_preferences


PreferencesResponse = Tuple[int, Dict[str, object]]
PreferencesReader = Callable[[Path], Dict[str, object]]
PreferencesWriter = Callable[[Mapping[str, object], Path], Dict[str, object]]
DirectoryScanner = Callable[
    [list[str], Dict[str, int]], Dict[str, object]
]
NativeThemeSetter = Callable[[str], None]


class PreferencesController:
    """Coordinate persisted preferences and configured directory scans."""

    def __init__(
        self,
        preferences_path: Path,
        index_runtime: IndexRuntime,
        *,
        native_theme_setter: Optional[NativeThemeSetter] = None,
        read: PreferencesReader = read_preferences,
        save: PreferencesWriter = save_preferences,
        scan_directories: DirectoryScanner = scan_directories_for_documents,
    ) -> None:
        self._preferences_path = preferences_path
        self._index_runtime = index_runtime
        self._native_theme_setter = native_theme_setter
        self._read = read
        self._save = save
        self._scan_directories = scan_directories

    def preferences(self) -> PreferencesResponse:
        return 200, self._read(self._preferences_path)

    def save_preferences(
        self,
        payload: Mapping[str, object],
    ) -> PreferencesResponse:
        try:
            preferences = self._save(payload, self._preferences_path)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except OSError:
            return 500, {
                "error": "应用设置无法保存，请检查配置目录是否可写。"
            }
        if "theme" in payload and self._native_theme_setter is not None:
            try:
                self._native_theme_setter(str(preferences["theme"]))
            except Exception:
                logging.exception("failed to apply native window theme")
        return 200, {"ok": True, **preferences}

    def scan_directories(self) -> PreferencesResponse:
        try:
            preferences = self._read(self._preferences_path)
            directories = list(preferences.get("scan_directories") or [])
            sources = self._index_runtime.catalog()["source_files"]
            imported_names = {
                str(item.get("file_name")): int(item.get("size_bytes") or 0)
                for item in sources
                if item.get("file_name")
            }
            result = self._scan_directories(directories, imported_names)
            result["directories"] = directories
        except (OSError, ValueError) as exc:
            return 500, {"error": f"扫描文献目录失败：{exc}"}
        return 200, result
