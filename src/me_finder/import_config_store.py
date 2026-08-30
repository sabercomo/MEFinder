"""Persistent storage for PDF import configuration."""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional

from .import_resume import resume_summary
from .mineru_api import MinerUError
from .vision_api import VisionAPIError


# PDF imports can finish on multiple worker threads. Keep each config
# read-modify-write transaction together; a re-entrant lock lets the public
# helpers call ``load_import_config``/``save_import_config`` while holding it.
_IMPORT_CONFIG_LOCK = threading.RLock()


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


def configured_pdf_path(
    config_path: Path,
    document: Mapping[str, object],
) -> Optional[Path]:
    file_name = str(document.get("file_name") or "").strip()
    if not file_name:
        return None
    candidate = Path(file_name)
    if candidate.is_absolute():
        return candidate
    # The supported layout is <root>/config/pdf_imports.json. Inferring the
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
    canonical_path = configured_pdf_path(config_path, canonical)
    duplicate_path = configured_pdf_path(config_path, duplicate)
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
    """Return a copy with legacy duplicate content IDs collapsed."""

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
        if isinstance(value, dict) and isinstance(value.get("documents"), list)
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
        for existing in reversed(sorted(path.parent.glob(f"{path.name}.corrupt-*"))):
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
        _atomic_write_import_config_text(path, serialized)
        try:
            # Mirror the committed snapshot rather than the previous one. A
            # transaction that later rolls back by saving its original config
            # then restores both the primary file and this recovery copy.
            _atomic_write_import_config_text(
                _import_config_backup_path(path),
                serialized,
            )
        except OSError:
            # The primary config is already durable. A backup failure must not
            # turn a successful import/config transaction into a false failure.
            logging.exception("failed to update PDF import config backup")


@contextmanager
def import_config_lock() -> Iterator[None]:
    """Hold the process-wide PDF import config transaction lock."""

    with _IMPORT_CONFIG_LOCK:
        yield


@contextmanager
def locked_import_config(path: Path) -> Iterator[Dict[str, object]]:
    """Load a config while holding its shared read-modify-write lock."""

    with import_config_lock():
        yield load_import_config(Path(path))


def attach_mineru_manifest(
    root: Path,
    source_file_id: str,
    manifest_path: Path,
    config_path: Optional[Path] = None,
) -> None:
    root = Path(root)
    config_path = Path(config_path or root / "config" / "pdf_imports.json")
    relative_manifest = Path(manifest_path)
    try:
        relative_manifest = relative_manifest.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    with import_config_lock():
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
    parser: str = "openai_compatible",
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
    with import_config_lock():
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
            "parser": parser,
            "provider_id": provider_id,
            "provider_name": provider_name,
            "model": model,
            "resume": resume_summary(manifest, manifest_path=relative_manifest),
        }
        document.pop("mineru", None)
        save_import_config(config_path, data)
