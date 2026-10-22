"""Re-publish MinerU books from their parser cache so page text matches pages.

Books indexed before 0.6.0 keep MinerU's cross-page joins: the opening lines
of a page sit under the previous page's number.  A refresh re-extracts each
book from the parser output already on disk (no parser call, no OCR) with the
current import code, and replaces only its page records and search
paragraphs.  The book's own records (title, metadata, mapping record, import
history, audit notes) and reader positions are written back unchanged.  A book
is skipped, never re-numbered, when its pages or citation pages would come out
different.  Its alignment runs are removed with the old text (their spans
point into it) and must be generated again.
"""

from __future__ import annotations

import errno
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Dict, List, Mapping, Optional, Sequence

from .database import replace_source_in_database
from .pdf_extractors import extract_pdf_source, load_pdf_import_config
from .persistence.source_rows_reads import read_reading_positions, read_source_rows
from .persistence.translation_work_store import (
    save_reading_position_row,
    workspace_write_transaction,
)

# Rows a refresh writes back exactly as stored.
KEPT_SOURCE_TABLES = (
    "source_files",
    "volumes",
    "works",
    "toc_entries",
    "pdf_page_mappings",
    "pdf_import_runs",
    "audit_issues",
)
_CITATION_FIELDS = ("citation_page_start", "citation_page_end")

Event = Callable[[Dict[str, object]], None]


def refresh_page_text(
    root: Path,
    database_path: Path,
    *,
    apply: bool = False,
    skip: Collection[str] = (),
    only: Optional[Collection[str]] = None,
    on_event: Optional[Event] = None,
) -> Dict[str, object]:
    """Plan, and with ``apply`` perform, the page-text refresh of MinerU books.

    Every book is reported with a status: ``refresh`` (planned),
    ``refreshed``, ``unchanged``, ``skipped`` (with a reason) or ``failed``.
    Applying copies the index to ``data/backups`` once, before the first write.
    """

    root = Path(root)
    database_path = Path(database_path)
    configs = load_pdf_import_config(root / "config" / "pdf_imports.json")
    books: List[Dict[str, object]] = []
    backup_path: Optional[Path] = None
    for config in configs:
        source_id = str(config.get("source_file_id") or "")
        if not source_id or not _has_parser_results(config):
            continue
        if only is not None and source_id not in only:
            continue
        book: Dict[str, object] = {"source_file_id": source_id, "title": config.get("title")}
        books.append(book)
        if source_id in skip:
            book.update(status="skipped", reason="requested")
            _emit(on_event, book)
            continue
        stored = read_source_rows(database_path, source_id)
        if stored is None:
            book.update(status="skipped", reason="not_indexed")
            _emit(on_event, book)
            continue
        path = Path(str(config.get("file_name") or ""))
        if not path.is_absolute():
            path = root / "corpus" / "raw_pdf" / path
        try:
            extracted = extract_pdf_source(path, root, config, None)
        except Exception as exc:  # report and keep going; nothing was written
            book.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
            _emit(on_event, book)
            continue
        reason = _skip_reason(stored, extracted)
        changed = _changed_pages(stored["pdf_pages"], extracted["pdf_pages"])
        book["changed_pages"] = changed
        if reason is not None:
            book.update(status="skipped", reason=reason)
        elif not changed:
            book["status"] = "unchanged"
        elif not apply:
            book["status"] = "refresh"
        else:
            if backup_path is None:
                backup_path = backup_index_copy(database_path)
                _emit(on_event, {"event": "backup", "path": str(backup_path)})
            replacement = {table: stored[table] for table in KEPT_SOURCE_TABLES}
            replacement["pdf_pages"] = extracted["pdf_pages"]
            replacement["paragraphs"] = extracted["paragraphs"]
            positions = read_reading_positions(database_path, source_id)
            replace_source_in_database(replacement, database_path, backup_existing=False)
            _restore_reading_positions(database_path, positions)
            book["status"] = "refreshed"
        _emit(on_event, book)
    counts: Dict[str, int] = {}
    for book in books:
        counts[str(book["status"])] = counts.get(str(book["status"]), 0) + 1
    return {
        "applied": apply,
        "backup_path": str(backup_path) if backup_path else None,
        "counts": counts,
        "books": books,
    }


def _restore_reading_positions(
    database_path: Path, positions: Sequence[Mapping[str, object]]
) -> None:
    """Write back reader positions the source replacement cascaded away.

    Page indexes do not change in a refresh, so the stored position still
    points at the same page.
    """

    if not positions:
        return
    with workspace_write_transaction(database_path) as connection:
        for row in positions:
            save_reading_position_row(
                connection,
                str(row["document_group_id"]),
                str(row["left_source_file_id"]),
                row["right_source_file_id"],
                int(row["item_index"]),
                int(row["char_offset"]),
                str(row["updated_at"]),
            )


def _has_parser_results(config: Mapping[str, object]) -> bool:
    results = config.get("parser_results") or config.get("mineru") or config.get("mineru_results")
    return isinstance(results, dict) and bool(results.get("manifest") or results.get("segments"))


def _skip_reason(
    stored: Mapping[str, List[Dict[str, object]]], extracted: Mapping[str, object]
) -> Optional[str]:
    old_pages = stored["pdf_pages"]
    new_pages = extracted["pdf_pages"]
    if not new_pages or str(new_pages[0].get("parser") or "") != "mineru":
        return "not_mineru"
    if [p["pdf_page_index"] for p in old_pages] != [p["pdf_page_index"] for p in new_pages]:
        return "page_count_changed"
    for old, new in zip(old_pages, new_pages):
        if any(old.get(field) != new.get(field) for field in _CITATION_FIELDS):
            return "citation_pages_changed"
    if _ids(stored["volumes"], "volume_id") != _ids(extracted["volumes"], "volume_id") or _ids(
        stored["works"], "work_id"
    ) != _ids(extracted["works"], "work_id"):
        return "volume_or_work_id_changed"
    return None


def _ids(rows: Sequence[Mapping[str, object]], key: str) -> List[str]:
    return sorted(str(row.get(key) or "") for row in rows)


def _changed_pages(
    old_pages: Sequence[Mapping[str, object]], new_pages: Sequence[Mapping[str, object]]
) -> int:
    old_text = {page["pdf_page_index"]: page.get("text_raw") for page in old_pages}
    return sum(1 for page in new_pages if old_text.get(page["pdf_page_index"]) != page.get("text_raw"))


def _emit(on_event: Optional[Event], item: Dict[str, object]) -> None:
    if on_event is not None:
        on_event(dict(item))


def backup_index_copy(database_path: Path) -> Path:
    """Durably copy the index to ``backups/page-refresh-…`` before the first write.

    The name stays outside the ``index-*`` retention set, so taking this copy
    never prunes an existing recovery point.
    """

    backup_dir = database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    required = database_path.stat().st_size + 64 * 1024 * 1024
    if shutil.disk_usage(backup_dir).free < required:
        raise OSError(errno.ENOSPC, f"磁盘空间不足，无法在刷新前备份文献库（约需 {required / 1024**3:.1f} GiB）。")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    target = backup_dir / f"page-refresh-{stamp}-{database_path.name}"
    temporary = backup_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(database_path, temporary)
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
