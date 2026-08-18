"""Local PDF import workflow used by the desktop import page."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from .import_resume import COMPLETED_UNIT_STATUSES, resume_summary
from .indexer import build_index
from .mineru_api import (
    DEFAULT_MINERU_API_BASE,
    DEFAULT_MINERU_MANIFEST_DIR,
    DEFAULT_MINERU_RESULT_DIR,
    DEFAULT_MINERU_STATE_DIR,
    MinerUConfig,
    MinerUError,
    download_done_results,
    get_batch_status,
    read_mineru_config_data,
    resolve_mineru_config_path,
    save_segment_manifest,
    submit_local_pdf_segments,
)
from .mineru_provider import MinerUCloudProvider
from .mineru_local_provider import (
    MINERU_LOCAL_PROVIDER_ID,
    MinerULocalProvider,
)
from .mineru_local_settings import load_mineru_local_config
from .large_document.engine import LargeDocumentJobEngine
from .large_document.job_ledger import JobLedger
from .large_document.merge import iter_normalized_pages
from .large_document.mineru_accounts import (
    MinerUAccountService,
    resolve_mineru_accounts_path,
)
from .pdf_extractors import detect_pdf_type, file_sha256
from .vision_api import (
    VisionAPIError,
    parse_pdf_with_vision_provider,
)


ProgressCallback = Callable[[Dict[str, object]], None]

# PDF imports can finish on multiple worker threads.  Keep each config
# read-modify-write transaction together; a re-entrant lock lets the public
# helpers call ``load_import_config``/``save_import_config`` while holding it.
_IMPORT_CONFIG_LOCK = threading.RLock()

# Keep internal corpus paths comfortably below the legacy Windows MAX_PATH
# budget.  The user's full PDF name is stored separately in pdf_imports.json
# and remains the title/file name shown by the application.
INTERNAL_DOCUMENT_NAME_MAX_BYTES = 180
LEGACY_WINDOWS_PATH_BUDGET = 240
STALE_DOCUMENT_STORAGE_SECONDS = 24 * 60 * 60


# Directory bundles that hold a user's media library rather than documents.
# Descending into them makes macOS raise a TCC prompt ("照片"/"Apple Music")
# for data this app has no use for, so the scan never opens them.
SKIPPED_DIRECTORY_SUFFIXES = frozenset({
    ".photoslibrary",
    ".photolibrary",
    ".migratedphotolibrary",
    ".musiclibrary",
    ".tvlibrary",
    ".imovielibrary",
    ".theater",
    ".fcpbundle",
    ".aplibrary",
    ".migratedaplibrary",
    ".logicx",
    ".band",
    ".app",
    ".bundle",
    ".framework",
    ".pkg",
    ".sparsebundle",
})


def _is_skipped_directory(path: Path, home: Path) -> bool:
    """Skip dot-directories, media library bundles and the user's Library."""

    name = path.name
    if name.startswith("."):
        return True
    if path.suffix.lower() in SKIPPED_DIRECTORY_SUFFIXES:
        return True
    return name == "Library" and path.parent == home


def _walk_documents(base: Path) -> List[Path]:
    """Yield candidate files under ``base`` without entering skipped bundles."""

    home = Path.home()
    found: List[Path] = []
    for current_dir, dir_names, file_names in os.walk(base, topdown=True):
        current = Path(current_dir)
        # Pruning in place stops os.walk from ever reading these directories.
        dir_names[:] = sorted(
            name
            for name in dir_names
            if not _is_skipped_directory(current / name, home)
        )
        for file_name in sorted(file_names):
            found.append(current / file_name)
    return found


def _truncate_utf8(text: str, byte_limit: int) -> str:
    encoded = str(text).encode("utf-8")
    if len(encoded) <= byte_limit:
        return str(text)
    return encoded[: max(0, byte_limit)].decode("utf-8", errors="ignore")


def internal_document_name(
    file_name: str,
    *,
    byte_limit: int = INTERNAL_DOCUMENT_NAME_MAX_BYTES,
) -> str:
    """Return a portable, collision-resistant basename for corpus storage."""

    original = Path(str(file_name)).name
    byte_limit = max(48, int(byte_limit))
    if len(original.encode("utf-8")) <= byte_limit:
        return original
    suffix = Path(original).suffix
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    trailer = f"-{digest}{suffix}"
    stem_limit = byte_limit - len(trailer.encode("utf-8"))
    shortened = _truncate_utf8(Path(original).stem, stem_limit).rstrip(" .")
    if not shortened:
        shortened = "document"
    return f"{shortened}{trailer}"


def _document_storage_reservation_path(target: Path) -> Path:
    # Default macOS and Windows volumes compare path components without case,
    # and macOS may also normalize Unicode.  The reservation identity must use
    # the same conservative equivalence or Case.pdf/case.pdf can acquire two
    # locks and atomically replace the same underlying file.
    normalized_name = unicodedata.normalize(
        "NFC",
        Path(target).name,
    ).casefold()
    identity = hashlib.sha256(
        normalized_name.encode("utf-8")
    ).hexdigest()
    return Path(target).parent / f".mefinder-reserve-{identity}.lock"


def cleanup_stale_document_storage_files(
    directory: Path,
    *,
    older_than_seconds: int = STALE_DOCUMENT_STORAGE_SECONDS,
) -> List[Path]:
    """Remove abandoned hidden upload/copy reservations, never final files."""

    directory = Path(directory)
    cutoff = time.time() - max(0, int(older_than_seconds))
    removed: List[Path] = []
    for pattern in (
        ".mefinder-copy-*.tmp",
        ".mefinder-upload-*.tmp",
        ".mefinder-reserve-*.lock",
    ):
        for path in directory.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed.append(path)
            except OSError:
                continue
    return removed


def document_storage_target(
    directory: Path,
    file_name: str,
    *,
    shorten_long_names: bool = True,
) -> Path:
    """Reserve an unused internal path for one incoming document.

    A hidden O_EXCL lock makes the reservation visible across app processes
    while keeping the final pathname invisible until the completed temporary
    file is atomically renamed into place.
    """

    directory = Path(directory)
    name_limit = INTERNAL_DOCUMENT_NAME_MAX_BYTES
    if os.name == "nt":
        name_limit = min(
            name_limit,
            max(48, LEGACY_WINDOWS_PATH_BUDGET - len(str(directory.resolve())) - 1),
        )
    original_name = Path(str(file_name)).name
    stored_name = (
        internal_document_name(original_name, byte_limit=name_limit)
        if shorten_long_names
        else original_name
    )
    for attempt in range(100):
        candidate = stored_name
        if attempt:
            candidate_with_suffix = (
                f"{Path(stored_name).stem} "
                f"(imported-{uuid.uuid4().hex[:8]})"
                f"{Path(stored_name).suffix}"
            )
            candidate = (
                internal_document_name(
                    candidate_with_suffix,
                    byte_limit=name_limit,
                )
                if shorten_long_names
                or len(candidate_with_suffix.encode("utf-8")) > 255
                else candidate_with_suffix
            )
        target = directory / candidate
        reservation = _document_storage_reservation_path(target)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            descriptor = os.open(reservation, flags, 0o600)
        except FileExistsError:
            continue
        try:
            os.close(descriptor)
            if target.exists():
                reservation.unlink(missing_ok=True)
                continue
            return target
        except Exception:
            reservation.unlink(missing_ok=True)
            raise
    raise FileExistsError(
        errno.EEXIST,
        "无法为导入文件分配唯一的内部文件名。",
    )


def release_document_storage_target(target: Path) -> None:
    """Release a path returned by :func:`document_storage_target`."""

    try:
        _document_storage_reservation_path(Path(target)).unlink(
            missing_ok=True
        )
    except OSError:
        # A synchronizer or antivirus can transiently hold the lock on
        # Windows.  The completed document is already durable at this point,
        # so lock cleanup must never replace a successful import result with
        # an error.  The 24-hour stale cleanup will retry later.
        logging.warning(
            "document storage reservation could not be released; "
            "stale cleanup will retry"
        )


def document_storage_error(file_name: str, exc: OSError) -> MinerUError:
    """Translate filesystem failures without exposing the private data path."""

    display_name = Path(str(file_name)).name or "该文件"
    if getattr(exc, "errno", None) == errno.ENAMETOOLONG:
        message = (
            f"无法保存“{display_name}”：文件名或应用数据目录路径过长。"
            "请把应用数据目录移到更短的位置后重试。"
        )
    elif getattr(exc, "errno", None) == errno.ENOSPC:
        message = f"无法保存“{display_name}”：磁盘空间不足。请释放空间后重试。"
    elif getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM}:
        message = f"无法保存“{display_name}”：应用数据目录没有写入权限。"
    else:
        message = f"无法保存“{display_name}”。请检查磁盘空间和目录权限后重试。"
    return MinerUError(message)


def scan_directories_for_documents(
    directories: Sequence[str],
    imported_names: Mapping[str, int],
    *,
    max_entries: int = 500,
    detect_limit: int = 500,
    detect_time_budget: float = 8.0,
) -> Dict[str, object]:
    """List PDF/DOCX files under the configured literature directories.

    ``imported_names`` maps already-imported file names to size in bytes.
    Detection of the PDF text-layer type only runs for new files, and stops
    once ``detect_time_budget`` seconds have been spent probing (opening
    every PDF in a huge folder would make scanning crawl).  A wall-clock
    budget is used rather than a plain file count because probing costs the
    same for a 2 MB article as for a 70 MB scan: a fixed count either cuts
    off ordinary libraries early or lets a huge one stall the scan.
    Undetected files are still listed, just without a text-layer verdict.
    """

    entries: List[Dict[str, object]] = []
    errors: List[Dict[str, object]] = []
    limit_reached = False
    detected_count = 0
    seen_paths: set[str] = set()
    detect_deadline = time.monotonic() + max(0.0, detect_time_budget)
    for directory in directories:
        base = Path(str(directory))
        if not base.is_dir():
            errors.append({"directory": str(directory), "error": "目录不存在或不可访问"})
            continue
        try:
            paths = _walk_documents(base)
        except OSError as exc:
            errors.append({"directory": str(directory), "error": str(exc)})
            continue
        for path in paths:
            suffix = path.suffix.lower()
            if suffix not in {".pdf", ".docx"}:
                continue
            if path.name.startswith(("~$", ".")):
                continue
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
                path_identity = os.path.normcase(str(path.resolve()))
            except OSError:
                continue
            if path_identity in seen_paths:
                continue
            if len(entries) >= max_entries:
                limit_reached = True
                break
            seen_paths.add(path_identity)
            imported_size = imported_names.get(path.name)
            if imported_size is None:
                status = "new"
            elif imported_size and size and imported_size != size:
                status = "name_conflict"
            else:
                status = "imported"
            entry: Dict[str, object] = {
                "path": str(path),
                "name": path.name,
                "directory": str(directory),
                "size_bytes": size,
                "file_type": "pdf" if suffix == ".pdf" else "docx",
                "status": status,
            }
            if suffix == ".pdf" and status == "new":
                if detected_count < detect_limit and time.monotonic() < detect_deadline:
                    detected_count += 1
                    try:
                        profile = detect_pdf_type(path)
                        detected = str(profile.get("detected_pdf_type") or "")
                        entry["detected_pdf_type"] = detected
                        entry["needs_ocr"] = bool(detected) and detected != "native_text"
                    except Exception:
                        entry["detected_pdf_type"] = None
                        entry["needs_ocr"] = None
                else:
                    entry["detected_pdf_type"] = None
                    entry["needs_ocr"] = None
            entries.append(entry)
        if limit_reached:
            break
    return {"entries": entries, "errors": errors, "limit_reached": limit_reached}


def copy_local_document(root: Path, source_path: Path) -> Path:
    """Copy one scanned file into the corpus, never touching the original."""

    root = Path(root)
    source_path = Path(source_path)
    suffix = source_path.suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise MinerUError("只支持 PDF 或 DOCX 文件。")
    directory = root / "corpus" / ("raw_pdf" if suffix == ".pdf" else "raw_docx")
    target: Optional[Path] = None
    temp_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        cleanup_stale_document_storage_files(directory)
        target = document_storage_target(
            directory,
            source_path.name,
            # PDF config stores the user's original name separately, allowing
            # a shorter portable internal path. Word extraction still uses
            # the on-disk basename, so preserve DOCX names until it has an
            # equivalent metadata sidecar.
            shorten_long_names=suffix == ".pdf",
        )
        temp_path = directory / f".mefinder-copy-{uuid.uuid4().hex}.tmp"
        shutil.copy2(source_path, temp_path)
        temp_path.replace(target)
    except OSError as exc:
        raise document_storage_error(source_path.name, exc) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if target is not None:
            release_document_storage_target(target)
    assert target is not None
    return target


def _is_blank_config_value(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_missing_config_values(
    target: Dict[str, object],
    incoming: Mapping[str, object],
) -> None:
    """Fill metadata gaps without replacing an existing user's choices."""

    for key, value in incoming.items():
        if key in {
            "source_file_id",
            "document_id",
            "file_name",
            "enabled",
            "mineru",
            "parser_results",
        }:
            continue
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_missing_config_values(current, value)
        elif key not in target or _is_blank_config_value(current):
            target[key] = copy.deepcopy(value)


def _configured_pdf_path(config_path: Path, document: Mapping[str, object]) -> Optional[Path]:
    file_name = str(document.get("file_name") or "").strip()
    if not file_name:
        return None
    candidate = Path(file_name)
    if candidate.is_absolute():
        return candidate
    # The supported layout is <root>/config/pdf_imports.json.  Inferring the
    # corpus here lets an old duplicate whose first file disappeared fall back
    # to another still-existing copy without deleting either path.
    return config_path.parent.parent / "corpus" / "raw_pdf" / candidate


def reuse_registered_pdf_copy(
    root: Path,
    incoming_path: Path,
    document: Mapping[str, object],
) -> Path:
    """Reuse a configured identical PDF and discard only our new corpus copy."""

    root = Path(root).resolve()
    incoming_path = Path(incoming_path)
    configured_path = _configured_pdf_path(
        root / "config" / "pdf_imports.json",
        document,
    )
    if configured_path is None or not configured_path.is_file():
        return incoming_path
    if configured_path.resolve() == incoming_path.resolve():
        return configured_path

    # ``incoming_path`` was created by this import.  Never unlink a caller's
    # original file even if this helper is accidentally used outside the web
    # import flow.
    raw_pdf_dir = (root / "corpus" / "raw_pdf").resolve()
    try:
        incoming_path.resolve().relative_to(raw_pdf_dir)
    except ValueError:
        return configured_path
    incoming_path.unlink(missing_ok=True)
    return configured_path


def _manifest_path(
    config_path: Path,
    parser_result: Mapping[str, object],
) -> Optional[Path]:
    resume = parser_result.get("resume")
    resume_path = resume.get("manifest_path") if isinstance(resume, Mapping) else None
    raw_path = parser_result.get("manifest") or resume_path
    if not raw_path:
        return None
    candidate = Path(str(raw_path))
    if candidate.is_absolute():
        return candidate
    return config_path.parent.parent / candidate


def _progress_count(
    primary: Mapping[str, object],
    fallback: Mapping[str, object],
    count_key: str,
    pages_key: str,
) -> int:
    raw_count = primary.get(count_key)
    if raw_count is None or raw_count == "":
        raw_count = fallback.get(count_key)
    try:
        explicit_count = int(raw_count or 0)
    except (TypeError, ValueError):
        explicit_count = 0
    raw_pages = primary.get(pages_key)
    if raw_pages is None:
        raw_pages = fallback.get(pages_key)
    page_count = len(raw_pages) if isinstance(raw_pages, list) else 0
    return max(0, explicit_count, page_count)


def _progress_timestamp(
    primary: Mapping[str, object],
    fallback: Mapping[str, object],
) -> float:
    value = next(
        (
            item
            for item in (
                primary.get("last_updated"),
                primary.get("updated_at"),
                primary.get("completed_at"),
                fallback.get("last_updated"),
                fallback.get("updated_at"),
                fallback.get("completed_at"),
            )
            if item is not None and item != ""
        ),
        None,
    )
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0.0
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _parser_result_score(
    config_path: Path,
    parser_result: Mapping[str, object],
) -> tuple:
    """Rank legacy parser results by whether they can actually be resumed/read."""

    resume = parser_result.get("resume")
    fallback = resume if isinstance(resume, Mapping) else {}
    manifest_path = _manifest_path(config_path, parser_result)
    manifest: Mapping[str, object] = {}
    manifest_usable = False
    if manifest_path is not None and manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            manifest = loaded
            manifest_usable = True

    completed = _progress_count(
        manifest,
        fallback,
        "completed_page_count",
        "completed_pages",
    )
    failed = _progress_count(
        manifest,
        fallback,
        "failed_page_count",
        "failed_pages",
    )
    status = str(manifest.get("status") or fallback.get("status") or "").lower()
    status_rank = {
        "completed": 3,
        "reused_completed": 3,
        "processing": 2,
        "pending": 1,
        "failed": 0,
    }.get(status, 0)
    try:
        total = max(
            0,
            int(manifest.get("total_pages") or fallback.get("total_pages") or 0),
        )
    except (TypeError, ValueError):
        total = 0
    completion_ratio = completed / total if total else 0.0
    return (
        int(manifest_usable),
        completed,
        -failed,
        status_rank,
        completion_ratio,
        _progress_timestamp(manifest, fallback),
    )


def _select_best_parser_result(
    canonical: Dict[str, object],
    duplicate: Mapping[str, object],
    config_path: Path,
) -> None:
    candidates = []
    for document in (canonical, duplicate):
        for key in ("mineru", "parser_results"):
            value = document.get(key)
            if isinstance(value, Mapping) and value:
                candidates.append(
                    (
                        _parser_result_score(config_path, value),
                        key,
                        value,
                    )
                )
    canonical.pop("mineru", None)
    canonical.pop("parser_results", None)
    if not candidates:
        return
    # ``max`` keeps the first candidate on a complete tie, preserving the
    # canonical record while allowing a demonstrably better retry to replace it.
    _, selected_key, selected_value = max(candidates, key=lambda item: item[0])
    canonical[selected_key] = copy.deepcopy(selected_value)


def _merge_duplicate_document(
    canonical: Dict[str, object],
    duplicate: Mapping[str, object],
    config_path: Path,
) -> None:
    canonical_path = _configured_pdf_path(config_path, canonical)
    duplicate_path = _configured_pdf_path(config_path, duplicate)
    if (
        (canonical_path is None or not canonical_path.is_file())
        and duplicate_path is not None
        and duplicate_path.is_file()
    ):
        canonical["file_name"] = duplicate.get("file_name")
    canonical["enabled"] = bool(canonical.get("enabled", True)) or bool(
        duplicate.get("enabled", True)
    )
    _select_best_parser_result(canonical, duplicate, config_path)
    _merge_missing_config_values(canonical, duplicate)


def _normalize_import_config(
    path: Path,
    data: Dict[str, object],
) -> Dict[str, object]:
    """Return a copy with legacy duplicate content IDs collapsed.

    Old releases could register the same PDF again after renaming its copied
    file.  Both records then shared a content-derived ``source_file_id`` and
    made SQLite reject the full rebuild.  Keep the first usable record as the
    canonical one and merge missing metadata from later records.  No PDF file
    is removed by this repair.
    """

    normalized = copy.deepcopy(data)
    raw_documents = normalized.get("documents")
    if not isinstance(raw_documents, list):
        normalized["documents"] = []
        return normalized

    documents: List[Dict[str, object]] = []
    by_source_id: Dict[str, Dict[str, object]] = {}
    for raw_document in raw_documents:
        if not isinstance(raw_document, dict):
            continue
        source_file_id = str(raw_document.get("source_file_id") or "").strip()
        if source_file_id:
            raw_document["source_file_id"] = source_file_id
            canonical = by_source_id.get(source_file_id)
            if canonical is not None:
                _merge_duplicate_document(canonical, raw_document, path)
                continue
            by_source_id[source_file_id] = raw_document
        documents.append(raw_document)
    normalized["documents"] = documents
    return normalized


def _import_config_backup_path(path: Path) -> Path:
    return Path(path).with_name(f"{Path(path).name}.bak")


def _atomic_write_import_config_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _decode_import_config_object(text: str) -> Optional[Dict[str, object]]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return (
        value
        if isinstance(value, dict)
        and isinstance(value.get("documents"), list)
        else None
    )


def _recover_concatenated_import_config(
    text: str,
) -> Optional[Dict[str, object]]:
    """Return the last complete object from accidentally concatenated JSON."""

    decoder = json.JSONDecoder()
    cursor = 0
    recovered: Optional[Dict[str, object]] = None
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        try:
            value, cursor = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError:
            break
        if not isinstance(value, dict) or not isinstance(
            value.get("documents"), list
        ):
            break
        recovered = value
    return recovered


def _preserve_corrupt_import_config(path: Path) -> Optional[Path]:
    """Copy a damaged config aside before replacing it with recovered data."""

    path = Path(path)
    if not path.is_file():
        return None
    try:
        damaged_bytes = path.read_bytes()
        for existing in reversed(
            sorted(path.parent.glob(f"{path.name}.corrupt-*"))
        ):
            if existing.is_file() and existing.read_bytes() == damaged_bytes:
                return existing
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")
        counter += 1
    try:
        shutil.copy2(path, target)
    except OSError:
        logging.exception("failed to preserve corrupt PDF import config")
        return None
    return target


def load_import_config(path: Path) -> Dict[str, object]:
    path = Path(path)
    with _IMPORT_CONFIG_LOCK:
        if not path.exists():
            backup_path = _import_config_backup_path(path)
            if not backup_path.is_file():
                return {"documents": []}
            backup_data = _decode_import_config_object(
                backup_path.read_text(encoding="utf-8-sig")
            )
            if backup_data is None:
                raise MinerUError("PDF 导入配置备份已损坏，无法自动恢复。")
            normalized = _normalize_import_config(path, backup_data)
            _atomic_write_import_config_text(
                path,
                json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            )
            logging.warning("restored missing PDF import config from backup")
            return normalized
        raw_text = path.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            data = _recover_concatenated_import_config(raw_text)
            recovery_source = "concatenated snapshot"
            if data is None:
                backup_path = _import_config_backup_path(path)
                if backup_path.is_file():
                    data = _decode_import_config_object(
                        backup_path.read_text(encoding="utf-8-sig")
                    )
                    recovery_source = "rolling backup"
            if data is None:
                _preserve_corrupt_import_config(path)
                raise MinerUError(
                    "PDF 导入配置已损坏，且无法自动恢复。"
                    "损坏文件已保留，请从数据备份恢复。"
                ) from exc
            preserved_path = _preserve_corrupt_import_config(path)
            normalized = _normalize_import_config(path, data)
            _atomic_write_import_config_text(
                path,
                json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            )
            logging.warning(
                "recovered PDF import config from %s; corrupt copy: %s",
                recovery_source,
                preserved_path,
            )
            return normalized
        if not isinstance(data, dict):
            raise MinerUError("PDF 导入配置必须是 JSON 对象。")
        return _normalize_import_config(path, data)


def save_import_config(path: Path, data: Dict[str, object]) -> None:
    """Atomically save a normalized config without sharing a temp filename."""

    path = Path(path)
    with _IMPORT_CONFIG_LOCK:
        normalized = _normalize_import_config(path, data)
        serialized = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
        _atomic_write_import_config_text(
            path,
            serialized,
        )
        try:
            # Mirror the committed snapshot rather than the previous one.  A
            # transaction that later rolls back by saving its original config
            # then restores both the primary file and this recovery copy.
            _atomic_write_import_config_text(
                _import_config_backup_path(path),
                serialized,
            )
        except OSError:
            # The primary config is already durable.  A backup failure must not
            # turn a successful import/config transaction into a false failure.
            logging.exception("failed to update PDF import config backup")


@contextmanager
def import_config_lock() -> Iterator[None]:
    """Hold the process-wide PDF import config transaction lock.

    Most callers should use :func:`locked_import_config`.  This lower-level
    context is for operations such as backup restore that must keep the config
    stable across several files and a subsequent index rebuild.
    """

    with _IMPORT_CONFIG_LOCK:
        yield


@contextmanager
def locked_import_config(path: Path) -> Iterator[Dict[str, object]]:
    """Load a config while holding its shared read-modify-write lock.

    ``load_import_config`` and ``save_import_config`` each lock their own file
    operation, but callers that modify a loaded dictionary must keep the same
    lock across both calls.  Otherwise a parser finishing on another worker can
    save its manifest between the read and write and have that update replaced
    by the caller's stale snapshot.

    The caller decides when to save so multi-file/database operations can also
    roll back while the lock is still held.
    """

    with import_config_lock():
        yield load_import_config(Path(path))


def register_pdf(
    root: Path,
    pdf_path: Path,
    config_path: Optional[Path] = None,
    *,
    original_file_name: Optional[str] = None,
) -> Dict[str, object]:
    """Add or update one PDF in the configured corpus without overwriting originals."""

    root = Path(root)
    pdf_path = Path(pdf_path)
    display_file_name = Path(str(original_file_name or pdf_path.name)).name
    if not display_file_name:
        display_file_name = pdf_path.name
    config_path = Path(config_path or root / "config" / "pdf_imports.json")
    content_sha256 = file_sha256(pdf_path)
    source_file_id = f"pdf-import-{content_sha256[:16]}"
    with _IMPORT_CONFIG_LOCK:
        data = load_import_config(config_path)
        documents = data["documents"]
        existing = next(
            (
                item
                for item in documents
                if item.get("source_file_id") == source_file_id
            ),
            None,
        )
        if existing is None:
            # Older indexes did not always use the content-derived source ID.
            # A disabled removal marker may still carry the full hash, which
            # lets the preserved copy be recovered without storing a duplicate.
            existing = next(
                (
                    item
                    for item in documents
                    if item.get("retained_after_removal") is True
                    and str(item.get("retained_sha256") or "").lower()
                    == content_sha256
                ),
                None,
            )
        if existing is not None:
            old_source_file_id = str(existing.get("source_file_id") or "")
            configured_path = _configured_pdf_path(config_path, existing)
            if configured_path is None or not configured_path.is_file():
                existing["file_name"] = pdf_path.name
            existing.setdefault("original_file_name", display_file_name)
            if not existing.get("title"):
                existing["title"] = Path(display_file_name).stem
            existing["enabled"] = True
            if old_source_file_id != source_file_id:
                existing["source_file_id"] = source_file_id
                existing["document_id"] = source_file_id.upper().replace("-", "_")
                existing.pop("mineru", None)
                existing.pop("parser_results", None)
                existing.pop("mineru_results", None)
                existing["page_mapping"] = {
                    "validated_by": None,
                    "segments": [],
                }
            existing.pop("retained_after_removal", None)
            existing.pop("retained_sha256", None)
            save_import_config(config_path, data)
            return existing

        existing = next(
            (item for item in documents if item.get("file_name") == pdf_path.name),
            None,
        )
        if existing is None:
            existing = {
                "enabled": True,
                "source_file_id": source_file_id,
                "document_id": source_file_id.upper().replace("-", "_"),
                "file_name": pdf_path.name,
                "original_file_name": display_file_name,
                "title": Path(display_file_name).stem,
                "author": None,
                "page_mapping": {"validated_by": None, "segments": []},
            }
            documents.append(existing)
        else:
            old_source_file_id = str(existing.get("source_file_id") or "")
            existing["enabled"] = True
            existing["source_file_id"] = source_file_id
            existing["document_id"] = source_file_id.upper().replace("-", "_")
            existing.setdefault("original_file_name", display_file_name)
            if not existing.get("title"):
                existing["title"] = Path(display_file_name).stem
            if old_source_file_id and old_source_file_id != source_file_id:
                # The file at this exact configured path was replaced.  Parser
                # output and page calibration belong to the old bytes.
                existing.pop("mineru", None)
                existing.pop("parser_results", None)
                existing["page_mapping"] = {
                    "validated_by": None,
                    "segments": [],
                }
        save_import_config(config_path, data)
        return existing


def attach_mineru_manifest(root: Path, source_file_id: str, manifest_path: Path, config_path: Optional[Path] = None) -> None:
    root = Path(root)
    config_path = Path(config_path or root / "config" / "pdf_imports.json")
    relative_manifest = Path(manifest_path)
    try:
        relative_manifest = relative_manifest.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    with _IMPORT_CONFIG_LOCK:
        data = load_import_config(config_path)
        document = next(
            (
                item
                for item in data["documents"]
                if item.get("source_file_id") == source_file_id
            ),
            None,
        )
        if document is None:
            raise MinerUError(f"PDF config not found: {source_file_id}")
        document["mineru"] = {
            "manifest": relative_manifest.as_posix(),
            "resume": resume_summary(manifest, manifest_path=relative_manifest),
        }
        document.pop("parser_results", None)
        save_import_config(config_path, data)


def attach_parser_manifest(
    root: Path,
    source_file_id: str,
    manifest_path: Path,
    *,
    provider_id: str,
    provider_name: str,
    model: str,
    config_path: Optional[Path] = None,
) -> None:
    """Attach one non-MinerU structured parser result to a configured PDF."""

    root = Path(root)
    config_path = Path(config_path or root / "config" / "pdf_imports.json")
    relative_manifest = Path(manifest_path)
    try:
        relative_manifest = relative_manifest.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    with _IMPORT_CONFIG_LOCK:
        data = load_import_config(config_path)
        document = next(
            (
                item
                for item in data["documents"]
                if item.get("source_file_id") == source_file_id
            ),
            None,
        )
        if document is None:
            raise VisionAPIError(f"PDF config not found: {source_file_id}")
        document["parser_results"] = {
            "manifest": relative_manifest.as_posix(),
            "parser": "openai_compatible",
            "provider_id": provider_id,
            "provider_name": provider_name,
            "model": model,
            "resume": resume_summary(manifest, manifest_path=relative_manifest),
        }
        document.pop("mineru", None)
        save_import_config(config_path, data)


def _first_extract_result(result: Dict[str, object]) -> Dict[str, object]:
    data = result.get("data") or {}
    if not isinstance(data, dict):
        return {}
    items = data.get("extract_result") or []
    if isinstance(items, dict):
        return items
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def parse_pdf_with_mineru(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    on_progress: Optional[ProgressCallback] = None,
    poll_seconds: int = 20,
    timeout_minutes: int = 180,
    use_local: bool = False,
) -> Dict[str, object]:
    """Submit all pages in <=200-page precision tasks and download results."""

    root = Path(root)
    pdf_path = Path(pdf_path)
    if use_local:
        return _parse_pdf_with_mineru_local(
            root,
            pdf_path,
            source_file_id,
            on_progress=on_progress,
            timeout_minutes=timeout_minutes,
        )
    accounts_path = resolve_mineru_accounts_path(root)
    if accounts_path.is_file():
        ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
        account_service = MinerUAccountService(
            ledger=ledger,
            config_path=accounts_path,
        )
        accounts = account_service.list_accounts()
        if not accounts:
            raise MinerUError(
                "尚未配置 MinerU 账号，请先在设置中添加账号。",
                allow_parser_fallback=True,
            )
        if not any(item.enabled and item.configured for item in accounts):
            raise MinerUError(
                "已保存的 MinerU 账号全部停用或缺少 Token，请先在设置中启用账号。",
                allow_parser_fallback=True,
            )
        return _parse_pdf_with_mineru_accounts(
            root,
            pdf_path,
            source_file_id,
            ledger=ledger,
            account_service=account_service,
            on_progress=on_progress,
            poll_seconds=poll_seconds,
            timeout_minutes=timeout_minutes,
        )
    config_path = resolve_mineru_config_path(root)
    state_dir = root / DEFAULT_MINERU_STATE_DIR
    manifest_dir = root / DEFAULT_MINERU_MANIFEST_DIR
    result_dir = root / DEFAULT_MINERU_RESULT_DIR
    manifest = submit_local_pdf_segments(
        pdf_path,
        config_path=config_path,
        state_dir=state_dir,
        manifest_dir=manifest_dir,
        result_dir=result_dir,
        data_id_prefix=source_file_id,
    )
    segments = [item for item in manifest.get("segments", []) if isinstance(item, dict)]
    pending = {
        str(item["batch_id"]): item
        for item in segments
        if item.get("batch_id")
        and str(item.get("status") or "").lower() not in COMPLETED_UNIT_STATUSES
    }
    completed = sum(
        1
        for item in segments
        if str(item.get("status") or "").lower() in COMPLETED_UNIT_STATUSES
    )
    save_segment_manifest(str(manifest.get("data_id_prefix") or source_file_id), manifest, manifest_dir)
    if on_progress:
        on_progress({
            "phase": "mineru_processing",
            "completed": completed,
            "total": len(segments),
            "total_pages": manifest.get("total_pages"),
            "completed_pages": manifest.get("completed_pages", []),
            "failed_pages": manifest.get("failed_pages", []),
        })
    deadline = time.time() + timeout_minutes * 60
    while pending and time.time() < deadline:
        for batch_id, segment in list(pending.items()):
            try:
                result = get_batch_status(batch_id, config_path=config_path, state_dir=state_dir)
            except Exception as exc:
                segment["last_error"] = str(exc)
                segment["status"] = (
                    "failed"
                    if isinstance(exc, MinerUError)
                    and exc.retry_with_new_task
                    else "processing"
                )
                if segment["status"] == "failed":
                    segment["error"] = str(exc)
                save_segment_manifest(
                    str(manifest.get("data_id_prefix") or source_file_id),
                    manifest,
                    manifest_dir,
                )
                raise MinerUError(
                    str(exc),
                    retry_with_new_task=bool(
                        isinstance(exc, MinerUError)
                        and exc.retry_with_new_task
                    ),
                    allow_parser_fallback=False,
                ) from exc
            item = _first_extract_result(result)
            state = str(item.get("state") or "unknown").lower()
            segment["last_state"] = state
            if state == "done":
                segment["status"] = "processing"
                segment["phase"] = "downloading"
                save_segment_manifest(
                    str(manifest.get("data_id_prefix") or source_file_id),
                    manifest,
                    manifest_dir,
                )
                try:
                    downloaded = download_done_results(
                        batch_id,
                        config_path=config_path,
                        state_dir=state_dir,
                        result_dir=result_dir,
                    )
                except Exception as exc:
                    segment["status"] = "processing"
                    segment["phase"] = "download_retry"
                    segment["last_error"] = str(exc)
                    save_segment_manifest(
                        str(manifest.get("data_id_prefix") or source_file_id),
                        manifest,
                        manifest_dir,
                    )
                    raise MinerUError(
                        str(exc),
                        allow_parser_fallback=False,
                    ) from exc
                segment["status"] = "completed"
                segment["result_dirs"] = [str(path) for path in downloaded]
                if downloaded:
                    segment["result_dir"] = str(downloaded[0])
                segment.pop("error", None)
                segment.pop("last_error", None)
                segment.pop("phase", None)
                pending.pop(batch_id, None)
                completed += 1
            elif state == "failed":
                segment["status"] = "failed"
                segment["error"] = str(item.get("err_msg") or "MinerU 解析失败")
                segment.pop("phase", None)
                pending.pop(batch_id, None)
            else:
                segment["status"] = "processing"
                segment.pop("last_error", None)
                segment.pop("phase", None)
            save_segment_manifest(
                str(manifest.get("data_id_prefix") or source_file_id),
                manifest,
                manifest_dir,
            )
            if on_progress:
                on_progress({
                    "phase": "mineru_processing",
                    "completed": completed,
                    "total": len(segments),
                    "page_range": segment.get("page_ranges"),
                    "state": state,
                    "total_pages": manifest.get("total_pages"),
                    "completed_pages": manifest.get("completed_pages", []),
                    "failed_pages": manifest.get("failed_pages", []),
                })
        if pending:
            time.sleep(poll_seconds)
    if pending:
        for segment in pending.values():
            segment["last_error"] = "MinerU 解析超时，等待下次继续检查。"
        save_segment_manifest(
            str(manifest.get("data_id_prefix") or source_file_id),
            manifest,
            manifest_dir,
        )
        raise MinerUError(
            "MinerU 解析超时，仍有分段任务未完成。",
            allow_parser_fallback=False,
        )
    if any(item.get("status") == "failed" for item in segments):
        save_segment_manifest(
            str(manifest.get("data_id_prefix") or source_file_id),
            manifest,
            manifest_dir,
        )
        raise MinerUError("MinerU 有分段解析失败，请查看导入状态。")
    manifest_path = save_segment_manifest(str(manifest.get("data_id_prefix") or source_file_id), manifest, manifest_dir)
    attach_mineru_manifest(root, source_file_id, manifest_path)
    return {
        "manifest_path": str(manifest_path),
        "segments": len(segments),
        "status": "completed",
        "resume": resume_summary(manifest, manifest_path=manifest_path),
    }


def _parse_pdf_with_mineru_accounts(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    *,
    ledger: JobLedger,
    account_service: MinerUAccountService,
    on_progress: Optional[ProgressCallback],
    poll_seconds: int,
    timeout_minutes: int,
) -> Dict[str, object]:
    """Run the v0.4.2 physical-slice engine with independent credentials."""

    global_config = read_mineru_config_data(resolve_mineru_config_path(root))
    api_base = str(
        global_config.get("api_base") or DEFAULT_MINERU_API_BASE
    ).rstrip("/")
    provider = MinerUCloudProvider(
        config=MinerUConfig(token="", api_base=api_base),
        max_pages_per_file=200,
        max_concurrency=1,
    )
    pool = account_service.create_pool(
        provider_max_concurrency=provider.capabilities().max_concurrency
    )
    pool.reconcile_in_flight()
    engine = LargeDocumentJobEngine(
        ledger=ledger,
        provider=provider,
        work_dir=root / "corpus" / "processed" / "parser_jobs",
        credential_pool=pool,
    )
    job = engine.prepare(
        source_path=pdf_path,
        source_file_id=source_file_id,
        document_id=source_file_id.upper().replace("-", "_"),
        model="vlm",
        options={"language": "ch", "is_ocr": True},
    )
    deadline = time.time() + max(1, int(timeout_minutes)) * 60
    while job.status not in {"validated", "permanent_failure", "cancelled"}:
        if time.time() >= deadline:
            raise MinerUError(
                "MinerU 大文档解析超时，已保留分片任务，下次可从断点继续。",
                allow_parser_fallback=False,
            )
        job = engine.run_once(job.id)
        if on_progress:
            slices = ledger.list_slice_jobs(job.id)
            completed_pages = [
                page
                for item in slices
                if item.status == "completed"
                for page in range(item.page_start, item.page_end + 1)
            ]
            on_progress(
                {
                    "phase": "mineru_processing",
                    "completed": job.completed_slices,
                    "total": job.total_slices,
                    "total_pages": job.total_pages,
                    "completed_pages": completed_pages,
                    "failed_pages": [],
                    "document_job_id": job.id,
                }
            )
        if job.status in {"validated", "permanent_failure", "cancelled"}:
            break
        time.sleep(max(0, poll_seconds))
    if job.status != "validated":
        raise MinerUError(
            job.error_summary or f"MinerU 大文档任务未完成：{job.status}",
            allow_parser_fallback=False,
        )
    return _publish_mineru_engine_results(
        root,
        source_file_id,
        ledger=ledger,
        document_job_id=job.id,
    )


def _parse_pdf_with_mineru_local(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    *,
    on_progress: Optional[ProgressCallback],
    timeout_minutes: int,
) -> Dict[str, object]:
    """Run an explicitly requested import through the user's local service."""

    config = load_mineru_local_config(resolve_mineru_config_path(root))
    provider = MinerULocalProvider(config)
    ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
    engine = LargeDocumentJobEngine(
        ledger=ledger,
        provider=provider,
        work_dir=root / "corpus" / "processed" / "parser_jobs",
    )
    job = engine.prepare(
        source_path=pdf_path,
        source_file_id=source_file_id,
        document_id=source_file_id.upper().replace("-", "_"),
        model=config.backend,
        options={
            "backend": config.backend,
            "language": config.language,
            "parse_method": config.parse_method,
            "formula_enable": config.formula_enable,
            "table_enable": config.table_enable,
        },
    )
    deadline = time.time() + max(1, int(timeout_minutes)) * 60
    while job.status not in {"validated", "permanent_failure", "cancelled"}:
        if time.time() >= deadline:
            raise MinerUError(
                "本地 MinerU 解析超时，已保留任务进度。",
                allow_parser_fallback=False,
            )
        job = engine.run_once(job.id)
        if on_progress:
            slices = ledger.list_slice_jobs(job.id)
            on_progress(
                {
                    "phase": "mineru_processing",
                    "provider_id": MINERU_LOCAL_PROVIDER_ID,
                    "provider_name": "本地 MinerU",
                    "message": "正在使用本地 MinerU 解析 PDF…",
                    "completed": job.completed_slices,
                    "total": job.total_slices,
                    "total_pages": job.total_pages,
                    "completed_pages": [
                        page
                        for item in slices
                        if item.status == "completed"
                        for page in range(item.page_start, item.page_end + 1)
                    ],
                    "failed_pages": [],
                    "document_job_id": job.id,
                }
            )
        if job.status in {"validated", "permanent_failure", "cancelled"}:
            break
        time.sleep(2)
    if job.status != "validated":
        failed = next(
            (
                item.last_error
                for item in ledger.list_slice_jobs(job.id)
                if item.last_error
            ),
            None,
        )
        raise MinerUError(
            failed or job.error_summary or f"本地 MinerU 任务未完成：{job.status}",
            allow_parser_fallback=False,
        )
    return _publish_mineru_engine_results(
        root,
        source_file_id,
        ledger=ledger,
        document_job_id=job.id,
        provider_id=MINERU_LOCAL_PROVIDER_ID,
        provider_name="本地 MinerU",
    )


def _publish_mineru_engine_results(
    root: Path,
    source_file_id: str,
    *,
    ledger: JobLedger,
    document_job_id: str,
    provider_id: str = "mineru-cloud",
    provider_name: str = "MinerU",
) -> Dict[str, object]:
    """Bridge validated normalized slices into the existing indexer contract."""

    job = ledger.get_document_job(document_job_id)
    if job.status != "validated":
        raise MinerUError("只有通过完整页码校验的 MinerU 任务才能进入索引。")
    manifest_dir = root / DEFAULT_MINERU_MANIFEST_DIR
    result_directory = (
        f"engine-{source_file_id}"
        if provider_id == "mineru-cloud"
        else f"engine-{provider_id}-{source_file_id}"
    )
    result_root = root / DEFAULT_MINERU_RESULT_DIR / result_directory
    segments: List[Dict[str, object]] = []
    for item in ledger.list_slice_jobs(job.id):
        if item.status != "completed" or not item.result_path:
            raise MinerUError("MinerU 大文档任务缺少已验证的切片结果。")
        result_dir = result_root / (
            f"pages-{item.page_start:06d}-{item.page_end:06d}"
        )
        content_path = result_dir / "content_list.json"
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content: List[Dict[str, object]] = []
        for page in iter_normalized_pages(Path(item.result_path)):
            physical_page = int(page["physical_pdf_page"])
            local_page = physical_page - item.page_start
            blocks = page.get("blocks") or []
            if isinstance(blocks, list) and blocks:
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    text = str(block.get("text") or "").strip()
                    if not text:
                        continue
                    content.append(
                        {
                            "page_idx": local_page,
                            "text": text,
                            "type": block.get("type"),
                            "text_level": block.get("text_level"),
                            "bbox": block.get("bbox"),
                            "reading_order": block.get("reading_order"),
                        }
                    )
            else:
                text = str(page.get("text") or "").strip()
                if text:
                    content.append(
                        {"page_idx": local_page, "text": text, "type": "text"}
                    )
        temporary = content_path.with_name(
            f".{content_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(content_path)
        finally:
            temporary.unlink(missing_ok=True)
        segments.append(
            {
                "status": "completed",
                "page_start": item.page_start,
                "page_end": item.page_end,
                "page_ranges": f"{item.page_start}-{item.page_end}",
                "page_index_offset": item.global_page_offset,
                "result_dir": str(result_dir),
                "credential_id": item.credential_id,
            }
        )
    manifest: Dict[str, object] = {
        "api": "precision",
        "parser": "mineru",
        "provider_id": provider_id,
        "provider_name": provider_name,
        "model": job.parser_model or "vlm",
        "file_hash": job.source_sha256,
        "data_id_prefix": source_file_id,
        "total_pages": job.total_pages,
        "document_job_id": job.id,
        "segments": segments,
    }
    manifest_path = save_segment_manifest(
        source_file_id,
        manifest,
        manifest_dir,
    )
    attach_mineru_manifest(root, source_file_id, manifest_path)
    return {
        "manifest_path": str(manifest_path),
        "segments": len(segments),
        "status": "completed",
        "document_job_id": job.id,
        "resume": resume_summary(manifest, manifest_path=manifest_path),
    }


def parse_pdf_with_provider(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    provider_id: str,
    on_progress: Optional[ProgressCallback] = None,
) -> Dict[str, object]:
    """Parse a PDF through a configured OpenAI-compatible vision provider."""

    result = parse_pdf_with_vision_provider(
        root,
        pdf_path,
        source_file_id,
        provider_id,
        on_progress=on_progress,
    )
    attach_parser_manifest(
        root,
        source_file_id,
        Path(str(result["manifest_path"])),
        provider_id=str(result["provider_id"]),
        provider_name=str(result["provider_name"]),
        model=str(result["model"]),
    )
    return result


def indexed_word_source_count(database_path: Path) -> int:
    """How many Word sources the current index holds, 0 when it cannot be read."""

    database_path = Path(database_path)
    if not database_path.exists():
        return 0
    try:
        connection = sqlite3.connect(str(database_path))
    except sqlite3.Error:
        return 0
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM source_files WHERE source_type = 'word'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        connection.close()
    return int(row[0]) if row else 0


def rebuild_local_index(
    root: Path,
    on_progress: Optional[ProgressCallback] = None,
    *,
    database_path: Optional[Path] = None,
) -> Dict[str, object]:
    root = Path(root)
    resolved_database_path = (
        Path(database_path)
        if database_path is not None
        else root / "data" / "index.sqlite3"
    )
    corpus_dir = root / "corpus" / "raw_docx"
    if not corpus_dir.exists():
        # Public builds ship without Word corpus; PDF-only indexing is normal there.
        # Refuse only when Word documents are indexed, since rebuilding without the
        # originals would silently drop them from search.
        if indexed_word_source_count(resolved_database_path):
            raise MinerUError(
                "找不到 Word 原始语料目录 corpus\\raw_docx，但索引中仍有 Word 文献。"
                "为避免它们从索引中消失，本次没有重建；请恢复该目录后重试。"
            )
        corpus_dir.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress({"phase": "rebuilding_index"})
    # Persist the in-memory compatibility repair before the indexer reads this
    # file directly.  This prevents legacy duplicate content IDs from reaching
    # SQLite's source_files primary key.
    pdf_config_path = root / "config" / "pdf_imports.json"
    with _IMPORT_CONFIG_LOCK:
        import_config = load_import_config(pdf_config_path)
        save_import_config(pdf_config_path, import_config)
    return build_index(
        corpus_dir=corpus_dir,
        index_path=root / "data" / "index.json",
        database_path=resolved_database_path,
        include_pdf=True,
        pdf_corpus_dir=root / "corpus" / "raw_pdf",
        pdf_config_path=pdf_config_path,
        parsed_pdf_dir=root / "corpus" / "parsed" / "pdf",
        backup_existing=True,
        root=root,
    )


def detect_imported_pdf(pdf_path: Path) -> Dict[str, object]:
    return detect_pdf_type(Path(pdf_path))
