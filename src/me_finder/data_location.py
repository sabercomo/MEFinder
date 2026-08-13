"""Desktop application-data location selection and safe migration."""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Optional


DATA_ROOT_MARKER = "data_root.txt"
DATA_ROOT_FOLDER_NAME = "MEFinder"


class DataLocationError(ValueError):
    """A user-facing data-location validation or migration failure."""


def default_macos_data_root(home: Path | None = None) -> Path:
    user_home = Path(home) if home is not None else Path.home()
    return user_home / "Library" / "Application Support" / DATA_ROOT_FOLDER_NAME


def default_windows_data_root(
    home: Path | None = None,
    *,
    local_app_data: str | Path | None = None,
) -> Path:
    if local_app_data is not None:
        base = Path(local_app_data).expanduser()
    else:
        user_home = Path(home) if home is not None else Path.home()
        base = user_home / "AppData" / "Local"
    return base / DATA_ROOT_FOLDER_NAME


def data_root_marker_path(default_root: Path) -> Path:
    return Path(default_root) / DATA_ROOT_MARKER


def read_data_root(
    default_root: Path,
    *,
    fallback_root: Path | None = None,
) -> Path:
    """Read the stable pointer while retaining the platform fallback."""

    default_root = Path(default_root).expanduser()
    fallback = (
        Path(fallback_root).expanduser()
        if fallback_root is not None
        else default_root
    )
    marker = data_root_marker_path(default_root)
    try:
        configured = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback
    if not configured:
        return fallback
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        return fallback
    return candidate.resolve()


def read_macos_data_root(home: Path | None = None) -> Path:
    return read_data_root(default_macos_data_root(home))


def proposed_data_root(selected_folder: str | Path) -> Path:
    """Turn a chosen parent folder into the exact MEFinder data directory."""

    raw = str(selected_folder or "").strip()
    if not raw:
        raise DataLocationError("没有选择文件夹。")
    selected = Path(raw).expanduser()
    if not selected.is_absolute():
        raise DataLocationError("请选择完整的文件夹路径。")
    selected = selected.resolve()
    if selected.name.casefold() == DATA_ROOT_FOLDER_NAME.casefold():
        return selected
    return selected / DATA_ROOT_FOLDER_NAME


def data_location_summary(
    current_root: Path,
    default_root: Path,
    *,
    available: bool = True,
) -> dict[str, object]:
    current = Path(current_root).expanduser().resolve()
    default = Path(default_root).expanduser().resolve()
    return {
        "available": available,
        "current_path": str(current),
        "default_path": str(default),
        "is_custom": current != default,
        "restart_required": False,
    }


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _validate_migration_paths(current_root: Path, target_root: Path) -> None:
    if current_root == target_root:
        raise DataLocationError("所选位置就是当前数据位置。")
    if target_root.name.casefold() != DATA_ROOT_FOLDER_NAME.casefold():
        raise DataLocationError("目标数据文件夹必须命名为 MEFinder。")
    if _is_within(target_root, current_root):
        raise DataLocationError("不能把新数据位置放在当前 MEFinder 数据文件夹内部。")
    if _is_within(current_root, target_root):
        raise DataLocationError("不能把当前 MEFinder 数据文件夹的上级目录作为新位置。")
    if not current_root.is_dir():
        raise DataLocationError("当前数据位置不存在，无法迁移。")
    index_path = current_root / "runtime" / "data" / "index.sqlite3"
    if not index_path.is_file():
        raise DataLocationError("当前索引数据库不存在，无法迁移。")
    if target_root.exists():
        if not target_root.is_dir():
            raise DataLocationError("所选目标已存在，但不是文件夹。")
        if any(target_root.iterdir()):
            raise DataLocationError(
                "目标 MEFinder 文件夹不是空的。为避免覆盖数据，请选择其他位置。"
            )


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise DataLocationError("迁移后的索引数据库校验失败。")
    finally:
        destination_connection.close()
        source_connection.close()


def _write_root_marker(marker: Path, target_root: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(str(target_root) + "\n", encoding="utf-8")
        temporary.replace(marker)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def migrate_data_root(
    current_root: Path,
    target_root: Path,
    default_root: Path,
) -> dict[str, object]:
    """Copy all mutable data, verify SQLite, then atomically switch next launch.

    The current directory is intentionally retained as a recoverable fallback.
    """

    current = Path(current_root).expanduser().resolve()
    target = Path(target_root).expanduser().resolve()
    default = Path(default_root).expanduser().resolve()
    _validate_migration_paths(current, target)

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.migration-{uuid.uuid4().hex}"
    database_source = current / "runtime" / "data" / "index.sqlite3"
    database_relative = Path("runtime/data/index.sqlite3")

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        directory_path = Path(directory).resolve()
        if directory_path == current and DATA_ROOT_MARKER in names:
            ignored.add(DATA_ROOT_MARKER)
        if directory_path == database_source.parent:
            for database_name in (
                database_source.name,
                database_source.name + "-wal",
                database_source.name + "-shm",
            ):
                if database_name in names:
                    ignored.add(database_name)
        return ignored

    try:
        shutil.copytree(current, staging, symlinks=True, ignore=ignore)
        database_target = staging / database_relative
        _copy_sqlite_database(database_source, database_target)
        if target.exists():
            target.rmdir()
        staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    marker = data_root_marker_path(default)
    try:
        _write_root_marker(marker, target)
    except OSError as exc:
        raise DataLocationError(
            f"数据已复制，但切换位置失败：{exc}。当前应用仍会使用旧位置。"
        ) from exc

    return {
        "ok": True,
        "current_path": str(current),
        "target_path": str(target),
        "restart_required": True,
        "old_data_retained": True,
        "message": "迁移完成。请重启应用以使用新位置；旧位置的数据已保留。",
    }
