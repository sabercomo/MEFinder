"""Export and restore the user-curated runtime state (page calibration,
bibliographic metadata, MinerU page-offset manifests, preferences).

Deliberately excludes the large regenerable artifacts — the SQLite index,
the corpus PDFs, and the MinerU OCR results — so a backup is a few hundred
KB and survives moving to a new machine.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

BACKUP_MARKER = "me_finder_backup"
BACKUP_VERSION = 1

# Paths are relative to the runtime root unless noted. preferences.json lives
# one level up (the LOCALAPPDATA/MEFinder dir), handled via app_data_root.
_CONFIG_FILE = "config/pdf_imports.json"
_MANIFEST_DIR = "corpus/processed/mineru/manifests"


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

        manifest_dir = runtime_root / _MANIFEST_DIR
        if manifest_dir.is_dir():
            for manifest in sorted(manifest_dir.glob("*.json")):
                arcname = f"{_MANIFEST_DIR}/{manifest.name}"
                archive.write(manifest, arcname)
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
    return normalized == _CONFIG_FILE or normalized.startswith(f"{_MANIFEST_DIR}/")


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

    manifest_members = [n for n in names if n.startswith(f"{_MANIFEST_DIR}/") and n.endswith(".json")]
    if manifest_members:
        manifest_dir = runtime_root / _MANIFEST_DIR
        manifest_dir.mkdir(parents=True, exist_ok=True)
        for member in manifest_members:
            (runtime_root / member).write_bytes(archive.read(member))
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
