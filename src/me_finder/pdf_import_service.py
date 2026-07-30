"""Local PDF import workflow used by the desktop import page."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .import_resume import COMPLETED_UNIT_STATUSES, resume_summary
from .indexer import build_index
from .mineru_api import (
    DEFAULT_MINERU_MANIFEST_DIR,
    DEFAULT_MINERU_RESULT_DIR,
    DEFAULT_MINERU_STATE_DIR,
    MinerUError,
    download_done_results,
    get_batch_status,
    resolve_mineru_config_path,
    save_segment_manifest,
    submit_local_pdf_segments,
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
            if len(entries) >= max_entries:
                limit_reached = True
                break
            suffix = path.suffix.lower()
            if suffix not in {".pdf", ".docx"}:
                continue
            if path.name.startswith(("~$", ".")):
                continue
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
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
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source_path.name
    if target.exists():
        target = directory / f"{source_path.stem} (imported-{uuid.uuid4().hex[:8]}){suffix}"
    temp_path = directory / f".{target.name}.{uuid.uuid4().hex}.copying"
    shutil.copy2(source_path, temp_path)
    temp_path.replace(target)
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


def load_import_config(path: Path) -> Dict[str, object]:
    path = Path(path)
    with _IMPORT_CONFIG_LOCK:
        if not path.exists():
            return {"documents": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise MinerUError("PDF 导入配置必须是 JSON 对象。")
        return _normalize_import_config(path, data)


def save_import_config(path: Path, data: Dict[str, object]) -> None:
    """Atomically save a normalized config without sharing a temp filename."""

    path = Path(path)
    with _IMPORT_CONFIG_LOCK:
        normalized = _normalize_import_config(path, data)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def register_pdf(root: Path, pdf_path: Path, config_path: Optional[Path] = None) -> Dict[str, object]:
    """Add or update one PDF in the configured corpus without overwriting originals."""

    root = Path(root)
    pdf_path = Path(pdf_path)
    config_path = Path(config_path or root / "config" / "pdf_imports.json")
    source_file_id = f"pdf-import-{file_sha256(pdf_path)[:16]}"
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
        if existing is not None:
            configured_path = _configured_pdf_path(config_path, existing)
            if configured_path is None or not configured_path.is_file():
                existing["file_name"] = pdf_path.name
            existing["enabled"] = True
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
                "title": pdf_path.stem,
                "author": None,
                "page_mapping": {"validated_by": None, "segments": []},
            }
            documents.append(existing)
        else:
            old_source_file_id = str(existing.get("source_file_id") or "")
            existing["enabled"] = True
            existing["source_file_id"] = source_file_id
            existing["document_id"] = source_file_id.upper().replace("-", "_")
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
) -> Dict[str, object]:
    """Submit all pages in <=200-page precision tasks and download results."""

    root = Path(root)
    pdf_path = Path(pdf_path)
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


def rebuild_local_index(root: Path, on_progress: Optional[ProgressCallback] = None) -> Dict[str, object]:
    root = Path(root)
    corpus_dir = root / "corpus" / "raw_docx"
    if not corpus_dir.exists():
        # Public builds ship without Word corpus; PDF-only indexing is normal there.
        # Refuse only when Word documents are indexed, since rebuilding without the
        # originals would silently drop them from search.
        if indexed_word_source_count(root / "data" / "index.sqlite3"):
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
        database_path=root / "data" / "index.sqlite3",
        include_pdf=True,
        pdf_corpus_dir=root / "corpus" / "raw_pdf",
        pdf_config_path=pdf_config_path,
        parsed_pdf_dir=root / "corpus" / "parsed" / "pdf",
        backup_existing=True,
        root=root,
    )


def detect_imported_pdf(pdf_path: Path) -> Dict[str, object]:
    return detect_pdf_type(Path(pdf_path))
