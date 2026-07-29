"""Durable, parser-neutral checkpoints for long-running PDF imports.

The JSON manifest is the source of truth while parsing is in progress.  The
SQLite ``pdf_import_runs`` table is rebuilt from parser output later and only
stores a summary, so it must never be used to decide what work can be resumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional


RESUME_SPEC_VERSION = 1
COMPLETED_UNIT_STATUSES = frozenset(
    {"completed", "reused_completed", "skipped_existing_result"}
)


class ResumeManifestError(RuntimeError):
    """Raised when an existing resume manifest cannot be trusted."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def options_fingerprint(options: Mapping[str, object]) -> str:
    """Return a stable fingerprint for parser options that affect output."""

    encoded = json.dumps(
        dict(options),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> Path:
    """Atomically replace one JSON file without exposing a half-written file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def load_json_object(path: Path) -> Optional[Dict[str, object]]:
    """Load an object manifest, returning ``None`` when it does not exist."""

    path = Path(path)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResumeManifestError(f"断点清单损坏：{path}") from exc
    if not isinstance(value, dict):
        raise ResumeManifestError(f"断点清单不是 JSON 对象：{path}")
    return value


def quarantine_corrupt_manifest(path: Path) -> Optional[Path]:
    """Keep a damaged manifest for diagnosis instead of overwriting it."""

    path = Path(path)
    if not path.exists():
        return None
    stamp = int(time.time())
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")
        counter += 1
    path.replace(target)
    return target


def manifest_matches(
    manifest: Mapping[str, object],
    *,
    file_hash: str,
    parser: str,
    parse_options_fingerprint: str,
) -> bool:
    """Whether an active checkpoint is safe to reuse for this exact parse."""

    return bool(
        int(manifest.get("resume_spec_version") or 0) == RESUME_SPEC_VERSION
        and str(manifest.get("file_hash") or "") == file_hash
        and str(manifest.get("parser") or "") == parser
        and str(manifest.get("parse_options_fingerprint") or "")
        == parse_options_fingerprint
    )


def page_numbers_for_unit(unit: Mapping[str, object]) -> list[int]:
    """Return 1-based physical pages covered by a segment or page checkpoint."""

    raw_page = unit.get("page_number")
    if raw_page not in {None, ""}:
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            return []
        return [page] if page > 0 else []

    page_range = str(unit.get("page_ranges") or "").strip()
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", page_range)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        return list(range(start, end + 1)) if 0 < start <= end else []
    if re.fullmatch(r"\d+", page_range):
        page = int(page_range)
        return [page] if page > 0 else []

    start_value = (
        unit.get("page_start")
        or unit.get("page_start_1based")
        or unit.get("pdf_page_start_1based")
    )
    end_value = (
        unit.get("page_end")
        or unit.get("page_end_1based")
        or unit.get("pdf_page_end_1based")
        or start_value
    )
    try:
        start, end = int(start_value), int(end_value)
    except (TypeError, ValueError):
        return []
    return list(range(start, end + 1)) if 0 < start <= end else []


def refresh_manifest_progress(
    manifest: Dict[str, object],
    *,
    units: Optional[Iterable[Mapping[str, object]]] = None,
) -> Dict[str, object]:
    """Recompute the common progress summary from parser-specific work units."""

    work_units = list(
        units
        if units is not None
        else (
            item
            for item in manifest.get("segments", [])
            if isinstance(item, dict)
        )
    )
    completed: set[int] = set()
    failures: Dict[int, Dict[str, object]] = {}
    for unit in work_units:
        status = str(unit.get("status") or "").strip().lower()
        pages = page_numbers_for_unit(unit)
        if status in COMPLETED_UNIT_STATUSES:
            completed.update(pages)
            continue
        if status != "failed":
            continue
        error = str(unit.get("error") or "解析失败")
        try:
            attempts = max(1, int(unit.get("attempts") or 1))
        except (TypeError, ValueError):
            attempts = 1
        for page in pages:
            failures[page] = {
                "page": page,
                "error": error,
                "attempts": attempts,
            }

    total_pages = max(0, int(manifest.get("total_pages") or 0))
    completed_pages = sorted(page for page in completed if page <= total_pages)
    failed_pages = [
        failures[page] for page in sorted(failures) if page <= total_pages
    ]
    if total_pages and len(completed_pages) == total_pages:
        status = "completed"
    elif failed_pages:
        status = "failed"
    elif completed_pages or any(
        str(unit.get("status") or "").lower()
        in {"submitted", "processing", "pending", "submitting"}
        for unit in work_units
    ):
        status = "processing"
    else:
        status = "pending"

    manifest.update(
        {
            "resume_spec_version": RESUME_SPEC_VERSION,
            "completed_pages": completed_pages,
            "completed_page_count": len(completed_pages),
            "failed_pages": failed_pages,
            "failed_page_count": len(failed_pages),
            "status": status,
            "last_updated": utc_now_iso(),
        }
    )
    return manifest


def resume_summary(
    manifest: Mapping[str, object],
    *,
    manifest_path: Optional[Path] = None,
) -> Dict[str, object]:
    """Small, secret-free summary suitable for ``pdf_import_runs`` payloads."""

    summary: Dict[str, object] = {
        "resume_spec_version": int(manifest.get("resume_spec_version") or 0),
        "file_hash": manifest.get("file_hash"),
        "total_pages": int(manifest.get("total_pages") or 0),
        "completed_pages": list(manifest.get("completed_pages") or []),
        "completed_page_count": int(manifest.get("completed_page_count") or 0),
        "failed_pages": list(manifest.get("failed_pages") or []),
        "failed_page_count": int(manifest.get("failed_page_count") or 0),
        "last_updated": manifest.get("last_updated"),
        "status": manifest.get("status"),
        "resume_count": int(manifest.get("resume_count") or 0),
    }
    if manifest_path is not None:
        summary["manifest_path"] = str(manifest_path)
    return summary
