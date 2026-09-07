"""Export and restore curated runtime state and alignment recipes.

Deliberately excludes the large regenerable artifacts — the SQLite index,
the corpus PDFs, and the OCR/VLM page results — so a backup is a few hundred
KB and survives moving to a new machine.
"""

from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, List, Optional

from .document_groups import read_document_group_snapshot
from .import_config_store import import_config_lock, save_import_config
from .alignment_snapshots import read_alignment_recipe_snapshot

BACKUP_MARKER = "me_finder_backup"
BACKUP_VERSION = 3

# Paths are relative to the runtime root unless noted. preferences.json lives
# one level up (the LOCALAPPDATA/MEFinder dir), handled via app_data_root.
_CONFIG_FILE = "config/pdf_imports.json"
_DOCUMENT_GROUPS_FILE = "config/document_groups.json"
_ALIGNMENTS_FILE = "config/text_alignments.json"
_MANIFEST_DIRS = (
    "corpus/processed/mineru/manifests",
    "corpus/processed/vision/manifests",
)
_MANIFEST_PATH_FIELDS = frozenset(
    {
        "pdf_path",
        "manifest_path",
        "work_manifest",
        "state_file",
        "result_dir",
    }
)
_MANIFEST_PATH_LIST_FIELDS = frozenset(
    {"result_dirs", "downloaded_result_dirs"}
)


def _portable_manifest_bytes(path: Path, runtime_root: Path) -> bytes:
    """Remove machine-specific absolute prefixes from backed-up manifests."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return (
            json.dumps(
                {
                    "backup_manifest_unreadable": True,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    runtime_root_resolved = runtime_root.resolve()

    def portable_path(value: object) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value
        candidate = Path(text)
        if candidate.is_absolute():
            try:
                return candidate.resolve().relative_to(
                    runtime_root_resolved
                ).as_posix()
            except (OSError, ValueError):
                # Parser results are intentionally not in the lightweight
                # backup. Do not retain even the basename of an external
                # absolute path because it may contain private information.
                return None

        # pathlib follows the host OS. Explicitly recognize paths produced on
        # the other platform so a Windows manifest backed up on macOS (or the
        # reverse) cannot leak an absolute machine-specific path.
        if (
            PurePosixPath(text).is_absolute()
            or PureWindowsPath(text).is_absolute()
        ):
            return None
        return text

    def scrub(value: object) -> object:
        if isinstance(value, dict):
            cleaned: Dict[str, object] = {}
            for key, item in value.items():
                if key in _MANIFEST_PATH_FIELDS:
                    cleaned[key] = portable_path(item)
                elif key in _MANIFEST_PATH_LIST_FIELDS:
                    if isinstance(item, list):
                        portable_entries = [
                            portable_path(entry) for entry in item
                        ]
                        cleaned[key] = [
                            entry
                            for entry in portable_entries
                            if entry is not None
                        ]
                    else:
                        cleaned[key] = []
                else:
                    cleaned[key] = scrub(item)
            return cleaned
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return (
        json.dumps(scrub(payload), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def create_backup(
    runtime_root: Path,
    app_data_root: Optional[Path] = None,
    index_path: Optional[Path] = None,
) -> bytes:
    """Return a zip archive of the curated state as raw bytes."""

    runtime_root = Path(runtime_root)
    buffer = io.BytesIO()
    included: List[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        config_path = runtime_root / _CONFIG_FILE
        if config_path.exists():
            archive.write(config_path, _CONFIG_FILE)
            included.append(_CONFIG_FILE)

        group_snapshot = (
            read_document_group_snapshot(Path(index_path))
            if index_path is not None
            else {"document_groups": [], "document_group_members": []}
        )
        archive.writestr(
            _DOCUMENT_GROUPS_FILE,
            json.dumps(group_snapshot, ensure_ascii=False, indent=2),
        )
        included.append(_DOCUMENT_GROUPS_FILE)

        alignment_snapshot = (
            read_alignment_recipe_snapshot(Path(index_path))
            if index_path is not None
            else {"alignment_pairs": []}
        )
        archive.writestr(
            _ALIGNMENTS_FILE,
            json.dumps(alignment_snapshot, ensure_ascii=False, indent=2),
        )
        included.append(_ALIGNMENTS_FILE)

        for relative_dir in _MANIFEST_DIRS:
            manifest_dir = runtime_root / relative_dir
            if manifest_dir.is_dir():
                for manifest in sorted(manifest_dir.glob("*.json")):
                    arcname = f"{relative_dir}/{manifest.name}"
                    archive.writestr(
                        arcname,
                        _portable_manifest_bytes(manifest, runtime_root),
                    )
                    included.append(arcname)

        if app_data_root is not None:
            preferences = Path(app_data_root) / "preferences.json"
            if preferences.exists():
                archive.write(preferences, "preferences.json")
                included.append("preferences.json")

        manifest_json = {
            "marker": BACKUP_MARKER,
            "version": BACKUP_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": included,
        }
        archive.writestr("backup.json", json.dumps(manifest_json, ensure_ascii=False, indent=2))
    return buffer.getvalue()


def backup_filename() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"MEFinder-backup-{stamp}.zip"


def write_backup(
    runtime_root: Path,
    dest_dir: Path,
    app_data_root: Optional[Path] = None,
    index_path: Optional[Path] = None,
) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / backup_filename()
    target.write_bytes(
        create_backup(runtime_root, app_data_root, index_path=index_path)
    )
    return target


def _is_safe_member(name: str) -> bool:
    if name in {"backup.json"}:
        return True
    if name == "preferences.json":
        return True
    normalized = name.replace("\\", "/")
    if normalized.startswith("../") or ".." in normalized.split("/") or normalized.startswith("/"):
        return False
    return normalized in {
        _CONFIG_FILE,
        _DOCUMENT_GROUPS_FILE,
        _ALIGNMENTS_FILE,
    } or any(
        normalized.startswith(f"{relative_dir}/")
        for relative_dir in _MANIFEST_DIRS
    )


def read_backup_bytes(source: Path) -> bytes:
    return Path(source).read_bytes()


def _atomic_restore_bytes(target: Path, payload: bytes) -> None:
    """Replace one restored file without exposing a partial snapshot."""

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.restore-{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def restore_backup(
    runtime_root: Path,
    archive_bytes: bytes,
    app_data_root: Optional[Path] = None,
) -> Dict[str, object]:
    """Restore curated state from a backup zip, backing up the current
    pdf_imports.json before overwriting. Returns a summary."""

    runtime_root = Path(runtime_root)
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("备份文件不是有效的 zip 归档。") from exc

    with archive:
        names = set(archive.namelist())
        if "backup.json" not in names:
            raise ValueError("这不是 ME_Finder 备份文件（缺少 backup.json）。")
        try:
            meta = json.loads(archive.read("backup.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("备份清单损坏。") from exc
        if not isinstance(meta, dict) or meta.get("marker") != BACKUP_MARKER:
            raise ValueError("这不是 ME_Finder 备份文件。")
        version = meta.get("version")
        if version not in {1, 2, BACKUP_VERSION}:
            raise ValueError("不支持此备份版本。")
        if version in {2, BACKUP_VERSION} and _DOCUMENT_GROUPS_FILE not in names:
            raise ValueError("备份缺少作品组快照。")
        if version == BACKUP_VERSION and _ALIGNMENTS_FILE not in names:
            raise ValueError("备份缺少文本对齐快照。")

        for name in names:
            if not _is_safe_member(name):
                raise ValueError(f"备份包含不安全的路径：{name}")

        restored: List[str] = []
        group_snapshot = None
        alignment_snapshot = None

        if _DOCUMENT_GROUPS_FILE in names:
            try:
                group_snapshot = json.loads(
                    archive.read(_DOCUMENT_GROUPS_FILE).decode("utf-8")
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("作品组快照损坏。") from exc
            if (
                not isinstance(group_snapshot, dict)
                or not isinstance(group_snapshot.get("document_groups"), list)
                or not isinstance(
                    group_snapshot.get("document_group_members"), list
                )
            ):
                raise ValueError("作品组快照格式无效。")
            restored.append(_DOCUMENT_GROUPS_FILE)

        if _ALIGNMENTS_FILE in names:
            try:
                alignment_snapshot = json.loads(
                    archive.read(_ALIGNMENTS_FILE).decode("utf-8")
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("文本对齐快照损坏。") from exc
            if (
                not isinstance(alignment_snapshot, dict)
                or not isinstance(alignment_snapshot.get("alignment_pairs"), list)
            ):
                raise ValueError("文本对齐快照格式无效。")
            restored.append(_ALIGNMENTS_FILE)

        if _CONFIG_FILE in names:
            target = runtime_root / _CONFIG_FILE
            try:
                restored_config = json.loads(
                    archive.read(_CONFIG_FILE).decode("utf-8-sig")
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("PDF 导入配置损坏。") from exc
            if not isinstance(restored_config, dict):
                raise ValueError("PDF 导入配置必须是 JSON 对象。")
            with import_config_lock():
                if target.exists():
                    backup_copy = target.with_suffix(
                        target.suffix + ".pre-restore"
                    )
                    _atomic_restore_bytes(backup_copy, target.read_bytes())
                save_import_config(target, restored_config)
            restored.append(_CONFIG_FILE)

        manifest_members = [
            name
            for name in names
            if name.endswith(".json")
            and any(
                name.startswith(f"{relative_dir}/")
                for relative_dir in _MANIFEST_DIRS
            )
        ]
        for member in manifest_members:
            target = runtime_root / member
            _atomic_restore_bytes(target, archive.read(member))
            restored.append(member)

        if "preferences.json" in names and app_data_root is not None:
            target = Path(app_data_root) / "preferences.json"
            _atomic_restore_bytes(target, archive.read("preferences.json"))
            restored.append("preferences.json")

    return {
        "restored": restored,
        "created_at": meta.get("created_at"),
        "count": len(restored),
        "document_group_snapshot": group_snapshot,
        "alignment_snapshot": alignment_snapshot,
    }
