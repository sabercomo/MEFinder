"""Export and restore the user-curated runtime state (page calibration,
bibliographic metadata, structured-parser manifests, preferences).

Deliberately excludes the large regenerable artifacts — the SQLite index,
the corpus PDFs, and the OCR/VLM page results — so a backup is a few hundred
KB and survives moving to a new machine.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, List, Optional

BACKUP_MARKER = "me_finder_backup"
BACKUP_VERSION = 1

# Paths are relative to the runtime root unless noted. preferences.json lives
# one level up (the LOCALAPPDATA/MEFinder dir), handled via app_data_root.
_CONFIG_FILE = "config/pdf_imports.json"
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


def create_backup(runtime_root: Path, app_data_root: Optional[Path] = None) -> bytes:
    """Return a zip archive of the curated state as raw bytes."""

    runtime_root = Path(runtime_root)
    buffer = io.BytesIO()
    included: List[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        config_path = runtime_root / _CONFIG_FILE
        if config_path.exists():
            archive.write(config_path, _CONFIG_FILE)
            included.append(_CONFIG_FILE)

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


def write_backup(runtime_root: Path, dest_dir: Path, app_data_root: Optional[Path] = None) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / backup_filename()
    target.write_bytes(create_backup(runtime_root, app_data_root))
    return target


def _is_safe_member(name: str) -> bool:
    if name in {"backup.json"}:
        return True
    if name == "preferences.json":
        return True
    normalized = name.replace("\\", "/")
    if normalized.startswith("../") or ".." in normalized.split("/") or normalized.startswith("/"):
        return False
    return normalized == _CONFIG_FILE or any(
        normalized.startswith(f"{relative_dir}/")
        for relative_dir in _MANIFEST_DIRS
    )


def read_backup_bytes(source: Path) -> bytes:
    return Path(source).read_bytes()


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

    names = set(archive.namelist())
    if "backup.json" not in names:
        raise ValueError("这不是 ME_Finder 备份文件（缺少 backup.json）。")
    try:
        meta = json.loads(archive.read("backup.json").decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("备份清单损坏。") from exc
    if meta.get("marker") != BACKUP_MARKER:
        raise ValueError("这不是 ME_Finder 备份文件。")

    for name in names:
        if not _is_safe_member(name):
            raise ValueError(f"备份包含不安全的路径：{name}")

    restored: List[str] = []

    if _CONFIG_FILE in names:
        target = runtime_root / _CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup_copy = target.with_suffix(target.suffix + ".pre-restore")
            backup_copy.write_bytes(target.read_bytes())
        target.write_bytes(archive.read(_CONFIG_FILE))
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
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(member))
        restored.append(member)

    if "preferences.json" in names and app_data_root is not None:
        target = Path(app_data_root) / "preferences.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read("preferences.json"))
        restored.append("preferences.json")

    return {
        "restored": restored,
        "created_at": meta.get("created_at"),
        "count": len(restored),
    }
