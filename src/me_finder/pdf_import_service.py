"""Local PDF import workflow used by the desktop import page."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from .document_file_store import (
    INTERNAL_DOCUMENT_NAME_MAX_BYTES,
    cleanup_stale_document_storage_files,
    copy_local_document,
    document_storage_target,
    release_document_storage_target,
    reuse_registered_pdf_copy as _reuse_registered_pdf_copy,
)
from .import_config_store import (
    attach_mineru_manifest,
    configured_pdf_path as _configured_pdf_path,
    import_config_lock,
    load_import_config,
    locked_import_config,
    save_import_config,
)
from .index_publisher import indexed_word_source_count, rebuild_local_index
from .pdf_extractors import detect_pdf_type, file_sha256
from .pdf_parser_adapters import (
    _publish_mineru_engine_results,
    parse_pdf_with_local_ocr,
    parse_pdf_with_mineru,
    parse_pdf_with_provider,
)


__all__ = [
    "INTERNAL_DOCUMENT_NAME_MAX_BYTES",
    "_publish_mineru_engine_results",
    "attach_mineru_manifest",
    "cleanup_stale_document_storage_files",
    "copy_local_document",
    "detect_imported_pdf",
    "document_storage_target",
    "import_config_lock",
    "indexed_word_source_count",
    "load_import_config",
    "locked_import_config",
    "parse_pdf_with_local_ocr",
    "parse_pdf_with_mineru",
    "parse_pdf_with_provider",
    "rebuild_local_index",
    "register_pdf",
    "release_document_storage_target",
    "reuse_registered_pdf_copy",
    "save_import_config",
    "scan_directories_for_documents",
]

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
    """List PDF/DOCX/EPUB files under configured literature directories.

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
            if suffix not in {".pdf", ".docx", ".epub"}:
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
                "file_type": suffix.lstrip("."),
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


def reuse_registered_pdf_copy(
    root: Path,
    incoming_path: Path,
    document: Mapping[str, object],
) -> Path:
    """Reuse a configured identical PDF and discard only our new corpus copy."""

    root = Path(root)
    configured_path = _configured_pdf_path(
        root / "config" / "pdf_imports.json",
        document,
    )
    return _reuse_registered_pdf_copy(root, incoming_path, configured_path)


def register_pdf(
    root: Path,
    pdf_path: Path,
    config_path: Optional[Path] = None,
    *,
    original_file_name: Optional[str] = None,
    existing_source_file_id: Optional[str] = None,
) -> Dict[str, object]:
    """Add or update one PDF in the configured corpus without overwriting originals."""

    root = Path(root)
    pdf_path = Path(pdf_path)
    display_file_name = Path(str(original_file_name or pdf_path.name)).name
    if not display_file_name:
        display_file_name = pdf_path.name
    config_path = Path(config_path or root / "config" / "pdf_imports.json")
    content_sha256 = file_sha256(pdf_path)
    source_file_id = existing_source_file_id or f"pdf-import-{content_sha256[:16]}"
    with import_config_lock():
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


def detect_imported_pdf(pdf_path: Path) -> Dict[str, object]:
    return detect_pdf_type(Path(pdf_path))
