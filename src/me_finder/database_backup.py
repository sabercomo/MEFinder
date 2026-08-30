"""Rotating, space-checked snapshots of the SQLite index.

Split out of :mod:`database` so that module stays about building and publishing
the index.  This one is only about files on disk: how many snapshots to keep,
whether there is room to write another, and pruning the oldest.
"""

from __future__ import annotations

import errno
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .persistence.index_schema import DEFAULT_DATABASE_PATH


DATABASE_BACKUP_RETENTION = 3

DATABASE_BACKUP_FREE_SPACE_MARGIN = 64 * 1024 * 1024

DATABASE_REBUILD_ESTIMATE_FLOOR = 16 * 1024 * 1024

def _prune_database_backups(
    backup_dir: Path, db_path: Path, keep: int = DATABASE_BACKUP_RETENTION
) -> List[Path]:
    """Drop all but the newest ``keep`` snapshots of ``db_path``."""

    if keep < 0:
        return []
    snapshots = sorted(
        (
            path
            for path in backup_dir.glob(f"{db_path.stem}-*{db_path.suffix}")
            if path.is_file()
        ),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    removed: List[Path] = []
    for path in snapshots[: max(0, len(snapshots) - keep)]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            continue
    return removed

def _backup_database(
    db_path: Path,
    *,
    additional_required_bytes: int = 0,
) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    required_bytes = (
        db_path.stat().st_size
        + max(0, int(additional_required_bytes))
        + DATABASE_BACKUP_FREE_SPACE_MARGIN
    )
    snapshots = sorted(
        (
            path
            for path in backup_dir.glob(f"{db_path.stem}-*{db_path.suffix}")
            if path.is_file()
        ),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    free_bytes = shutil.disk_usage(backup_dir).free

    # Old releases never pruned snapshots.  Work out whether deleting older
    # snapshots can make enough room *before* removing any of them, and always
    # retain the newest known-good backup until its replacement is complete.
    pre_copy_keep = max(1, DATABASE_BACKUP_RETENTION - 1)
    delete_count = max(0, len(snapshots) - pre_copy_keep)
    planned_removals = list(snapshots[:delete_count])
    remaining = list(snapshots[delete_count:])

    def reclaimable_bytes(path: Path) -> int:
        stat = path.stat()
        blocks = int(getattr(stat, "st_blocks", 0) or 0)
        return blocks * 512 if blocks else int(stat.st_size)

    projected_free = free_bytes + sum(
        reclaimable_bytes(path) for path in planned_removals
    )
    for candidate in remaining[:-1]:
        if projected_free >= required_bytes:
            break
        planned_removals.append(candidate)
        projected_free += reclaimable_bytes(candidate)
    if projected_free < required_bytes:
        free_gib = free_bytes / (1024**3)
        needed_gib = required_bytes / (1024**3)
        raise OSError(
            errno.ENOSPC,
            "磁盘空间不足，无法创建索引安全备份："
            f"至少需要约 {needed_gib:.2f} GiB，当前可用约 {free_gib:.2f} GiB。"
            "为避免丢失现有恢复点，本次没有删除旧备份。"
            "请清理数据目录的 backups 文件夹或释放磁盘空间后重试。",
        )
    for snapshot in planned_removals:
        snapshot.unlink()
    free_bytes = shutil.disk_usage(backup_dir).free
    if free_bytes < required_bytes:
        needed_gib = required_bytes / (1024**3)
        free_gib = free_bytes / (1024**3)
        raise OSError(
            errno.ENOSPC,
            "磁盘空间不足，无法创建索引安全备份："
            f"至少需要约 {needed_gib:.2f} GiB，当前可用约 {free_gib:.2f} GiB。"
            "已保留最近一次可用备份。"
            "请清理数据目录的 backups 文件夹或释放磁盘空间后重试。",
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup_path = backup_dir / f"{db_path.stem}-{stamp}{db_path.suffix}"
    temp_path = backup_dir / f".{backup_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(db_path, temp_path)
        # A successful copy call is not enough to call a multi-GB snapshot
        # durable.  Flush the temporary file before its atomic rename so a
        # power loss cannot expose a partially persisted file as a backup.
        # Windows maps fsync to FlushFileBuffers, which requires a handle
        # opened with write access even though no more bytes are changed.
        with temp_path.open("rb+") as stream:
            os.fsync(stream.fileno())
        temp_path.replace(backup_path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        if getattr(exc, "errno", None) == errno.ENOSPC:
            free_gib = shutil.disk_usage(backup_dir).free / (1024**3)
            needed_gib = required_bytes / (1024**3)
            raise OSError(
                errno.ENOSPC,
                "磁盘空间不足，索引安全备份未完成："
                f"需要约 {needed_gib:.2f} GiB，当前可用约 {free_gib:.2f} GiB。"
                "最近一次可用备份仍已保留。",
            ) from exc
        raise
    _prune_database_backups(backup_dir, db_path)
    return backup_path

def backup_database(db_path: Path = DEFAULT_DATABASE_PATH) -> Path:
    """Create one durable, retention-managed snapshot of the index."""

    return _backup_database(Path(db_path))
