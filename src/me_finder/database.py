"""SQLite storage for the local literature index.

The JSON index remains available as an export and migration fallback. The
desktop application and the default search path use this SQLite database so
the full paragraph corpus is not loaded from one large JSON document.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import (
    Dict,
    List,
    Sequence,
)

from .persistence.connection import (
    open_build_target,
    open_readonly_index,
)
from .persistence.fts_index import (
    _install_fts5_search_index,
    ensure_database_search_index as _ensure_database_search_index,
    database_has_fts5_search_index as database_has_fts5_search_index,
)
from .persistence.storage_optimization import optimize_database_storage as _optimize_database_storage
from .persistence.index_build import (
    _float_or_none as _float_or_none,
    _insert_page_anchors as _insert_page_anchors,
    _int_or_none as _int_or_none,
    _json,
    insert_initial_index_rows,
    insert_remaining_index_rows,
    load_database_index as load_database_index,
)
from .persistence.source_replace import (
    _delete_page_anchors_for_source as _delete_page_anchors_for_source,
    delete_source_rows,
    replace_source_rows,
)
from .persistence.paragraph_payload import (
    PARAGRAPH_PAYLOAD_OMITTED_FIELDS as PARAGRAPH_PAYLOAD_OMITTED_FIELDS,
    PARAGRAPH_SELECT_COLUMNS as PARAGRAPH_SELECT_COLUMNS,
    PARAGRAPH_TYPED_COLUMNS as PARAGRAPH_TYPED_COLUMNS,
    paragraph_from_database_row as paragraph_from_database_row,
    paragraph_payload_for_storage as paragraph_payload_for_storage,
)
from .database_backup import (
    DATABASE_BACKUP_FREE_SPACE_MARGIN,
    DATABASE_BACKUP_RETENTION as DATABASE_BACKUP_RETENTION,
    DATABASE_REBUILD_ESTIMATE_FLOOR,
    _backup_database,
    _prune_database_backups,
    backup_database as backup_database,
)
from .index_identity import (
    IndexIdentityConflictError as IndexIdentityConflictError,
    _deduplicate_keyed_rows,
    _deduplicate_source_files,
)
from .persistence.index_schema import (
    ANCHOR_SPEC_VERSION as ANCHOR_SPEC_VERSION,
    DATABASE_SCHEMA_VERSION,
    DEFAULT_DATABASE_PATH,
    SCHEMA as SCHEMA,
)

DATABASE_REPLACE_ATTEMPTS = 15
DATABASE_REPLACE_INITIAL_DELAY_SECONDS = 0.1
DATABASE_REPLACE_MAX_DELAY_SECONDS = 1.0





def ensure_database_search_index(db_path: Path) -> bool:
    """Upgrade paragraph storage and create FTS once, with scan fallback."""

    return _ensure_database_search_index(db_path, optimize_database_storage)


def optimize_database_storage(db_path: Path) -> bool:
    """Stream a legacy index into a sparse, validated replacement."""

    return _optimize_database_storage(
        db_path,
        replace_database_file=_replace_database_file,
        prune_database_backups=_prune_database_backups,
        backup_free_space_margin=DATABASE_BACKUP_FREE_SPACE_MARGIN,
    )


# Isolated UTF-16 surrogate code points (U+D800–U+DFFF).  Broken PDF text
# layers—and the parser/JSON output derived from them—occasionally smuggle
# these in (often via ``\uD8xx`` escapes that ``json.loads`` accepts verbatim).
# SQLite stores Python ``str`` as UTF-8, which forbids lone surrogates, so a
# single tainted page would otherwise abort the whole index write with
# "'utf-8' codec can't encode characters ... surrogates not allowed".
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _strip_surrogates(text: str) -> str:
    """Replace un-encodable surrogate code points with U+FFFD."""

    if _SURROGATE_RE.search(text) is None:
        return text
    return _SURROGATE_RE.sub("�", text)


def _sanitize_surrogates_in_place(value: object) -> None:
    """Scrub surrogate code points from every string reachable in ``value``.

    Mutates dicts/lists in place so a large index is not deep-copied; clean
    strings are left untouched, so the pass is cheap when nothing is tainted.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                cleaned = _strip_surrogates(item)
                if cleaned is not item:
                    value[key] = cleaned
            elif isinstance(item, (dict, list)):
                _sanitize_surrogates_in_place(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                cleaned = _strip_surrogates(item)
                if cleaned is not item:
                    value[index] = cleaned
            elif isinstance(item, (dict, list)):
                _sanitize_surrogates_in_place(item)


def _estimate_database_build_size(index: Dict[str, object]) -> int:
    """Conservatively estimate a fresh normalized DB without one huge encode."""

    estimated = 0
    metadata = index.get("metadata")
    if isinstance(metadata, dict):
        estimated += len(_json(metadata).encode("utf-8"))
    for table_name in (
        "source_files",
        "volumes",
        "works",
        "toc_entries",
        "paragraphs",
        "page_anchors",
        "pdf_pages",
        "pdf_page_mappings",
        "pdf_import_runs",
        "audit_issues",
    ):
        rows = index.get(table_name)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            stored_payload = (
                paragraph_payload_for_storage(row)
                if table_name == "paragraphs"
                else row
            )
            estimated += len(_json(stored_payload).encode("utf-8")) + 256
            if table_name == "paragraphs":
                # Four typed search columns plus the compact trigram index.
                typed_text_bytes = sum(
                    len(str(row.get(field) or "").encode("utf-8"))
                    for field in (
                        "text_raw",
                        "normalized_text",
                        "compact_text",
                        "plain_text",
                    )
                )
                plain_bytes = len(
                    str(row.get("plain_text") or "").encode("utf-8")
                )
                estimated += typed_text_bytes + (plain_bytes * 2)
    return max(
        DATABASE_REBUILD_ESTIMATE_FLOOR,
        int(estimated * 1.15),
    )


def build_database(index: Dict[str, object], db_path: Path = DEFAULT_DATABASE_PATH, backup_existing: bool = False) -> Dict[str, object]:
    """Build a normalized SQLite database from an extracted index dictionary."""

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # DocumentGroups are user data that live only in the index DB; capture them
    # from the file about to be replaced, then restore after source_files exist.
    from .document_groups import (
        read_document_group_snapshot,
        restore_document_group_snapshot,
    )
    from .alignment_snapshots import (
        read_alignment_recipe_snapshot,
        restore_alignment_recipe_snapshot,
    )

    from .persistence.zotero_sync_store import (
        read_zotero_sync_snapshot,
        restore_zotero_sync_snapshot,
    )

    preserved_document_groups = read_document_group_snapshot(db_path)
    preserved_alignments = read_alignment_recipe_snapshot(db_path)
    preserved_zotero_links = read_zotero_sync_snapshot(db_path)
    # Do this before size estimation and any write: surrogates crash the
    # UTF-8 encode step too, not just the SQLite insert.
    _sanitize_surrogates_in_place(index)
    if backup_existing and db_path.exists():
        # A full rebuild needs both the snapshot and a new database-sized temp
        # file on the same volume.  Reserve that second copy up front so a
        # 1.4GB library fails safely before doing multi-GB work.
        _backup_database(
            db_path,
            additional_required_bytes=max(
                db_path.stat().st_size,
                _estimate_database_build_size(index),
            ),
        )

    raw_source_files = [
        item for item in index.get("source_files", []) if isinstance(item, dict)
    ]
    source_files, duplicate_source_count = _deduplicate_source_files(
        raw_source_files
    )
    volumes, duplicate_volume_count = _deduplicate_keyed_rows(
        [item for item in index.get("volumes", []) if isinstance(item, dict)],
        table_name="volumes",
        key_fields=("volume_id",),
        content_identity_fields=("source_file_id", "source_type"),
    )
    works, duplicate_work_count = _deduplicate_keyed_rows(
        [item for item in index.get("works", []) if isinstance(item, dict)],
        table_name="works",
        key_fields=("work_id",),
        content_identity_fields=("volume_id", "source_file_id", "source_type"),
    )
    paragraphs, duplicate_paragraph_count = _deduplicate_keyed_rows(
        [item for item in index.get("paragraphs", []) if isinstance(item, dict)],
        table_name="paragraphs",
        key_fields=("paragraph_id",),
        content_identity_fields=(
            "source_file_id",
            "source_type",
            "text_raw",
            "pdf_page_start_index",
            "pdf_page_end_index",
        ),
    )
    page_anchors, duplicate_anchor_count = _deduplicate_keyed_rows(
        [item for item in index.get("page_anchors", []) if isinstance(item, dict)],
        table_name="page_anchors",
        key_fields=("page_anchor_id",),
        content_identity_fields=("source_file_id", "start_paragraph_id"),
    )
    pdf_pages, duplicate_pdf_page_count = _deduplicate_keyed_rows(
        [item for item in index.get("pdf_pages", []) if isinstance(item, dict)],
        table_name="pdf_pages",
        key_fields=("source_file_id", "pdf_page_index"),
        content_identity_fields=("page_text_hash", "text_raw"),
    )
    pdf_page_mappings, duplicate_mapping_count = _deduplicate_keyed_rows(
        [
            item
            for item in index.get("pdf_page_mappings", [])
            if isinstance(item, dict)
        ],
        table_name="pdf_page_mappings",
        key_fields=("mapping_id",),
        content_identity_fields=("source_file_id",),
    )
    deduplicated_rows = {
        "source_files": duplicate_source_count,
        "volumes": duplicate_volume_count,
        "works": duplicate_work_count,
        "paragraphs": duplicate_paragraph_count,
        "page_anchors": duplicate_anchor_count,
        "pdf_pages": duplicate_pdf_page_count,
        "pdf_page_mappings": duplicate_mapping_count,
    }
    deduplicated_rows = {
        table: count for table, count in deduplicated_rows.items() if count
    }

    temp_path = db_path.with_name(
        f".{db_path.name}.{os.getpid()}-{threading.get_ident()}.tmp"
    )
    if temp_path.exists():
        temp_path.unlink()
    connection = open_build_target(temp_path)
    fts_installed = False
    try:
        insert_initial_index_rows(connection, index, source_files, paragraphs, deduplicated_rows)

        # SourceFiles now exist in the rebuilt DB; re-apply preserved groups,
        # skipping members whose source is gone and clearing a missing base.
        restore_document_group_snapshot(connection, preserved_document_groups)

        insert_remaining_index_rows(
            connection, index, volumes, works, paragraphs, page_anchors,
            pdf_pages, pdf_page_mappings,
        )

        # Automatic links are derived from PDF text, but they are also a
        # user-requested computation. Recreate the same completed pairs after
        # the replacement index has published its fresh page text.
        connection.row_factory = sqlite3.Row
        restore_alignment_recipe_snapshot(connection, preserved_alignments)
        restore_zotero_sync_snapshot(connection, preserved_zotero_links)

        fts_installed = _install_fts5_search_index(connection, rebuild=True)
        connection.commit()
        # This database was created from an empty temp file, so VACUUM cannot
        # reclaim meaningful fragmentation.  It only creates another
        # database-sized temporary copy, which made large rebuilds require
        # several extra GiB of free disk space.
        connection.close()
        _replace_database_file(temp_path, db_path)
    except Exception as exc:
        connection.close()
        if temp_path.exists():
            temp_path.unlink()
        disk_full = bool(
            getattr(exc, "errno", None) == errno.ENOSPC
            or getattr(exc, "sqlite_errorcode", None)
            == getattr(sqlite3, "SQLITE_FULL", 13)
            or (
                isinstance(exc, sqlite3.OperationalError)
                and "disk" in str(exc).casefold()
                and "full" in str(exc).casefold()
            )
        )
        if disk_full:
            free_gib = shutil.disk_usage(db_path.parent).free / (1024**3)
            raise OSError(
                errno.ENOSPC,
                "磁盘空间不足，无法完成索引重建。"
                f"当前可用约 {free_gib:.2f} GiB；现有索引未被替换，"
                "已创建的安全备份仍保留。请释放空间后重试。",
            ) from exc
        raise

    return {
        "path": str(db_path),
        "schema_version": DATABASE_SCHEMA_VERSION,
        "source_count": len(source_files),
        "paragraph_count": len(paragraphs),
        "eligible_paragraph_count": sum(1 for item in paragraphs if item.get("eligible_for_search")),
        "deduplicated_rows": deduplicated_rows,
        "fts5_search_index": fts_installed,
    }


def _is_retryable_replace_error(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or getattr(exc, "winerror", None) in {5, 32, 33}
        or getattr(exc, "errno", None)
        in {errno.EACCES, errno.EBUSY, errno.EPERM, errno.ETXTBSY}
    )


def _replace_database_file(
    temp_path: Path,
    db_path: Path,
    attempts: int = DATABASE_REPLACE_ATTEMPTS,
) -> None:
    """Replace a live SQLite file after short-lived Windows/cloud locks clear."""

    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        try:
            temp_path.replace(db_path)
            return
        except OSError as exc:
            if not _is_retryable_replace_error(exc) or attempt + 1 >= attempts:
                raise
            delay = min(
                DATABASE_REPLACE_INITIAL_DELAY_SECONDS * (2**attempt),
                DATABASE_REPLACE_MAX_DELAY_SECONDS,
            )
            time.sleep(delay)


def replace_source_in_database(
    extracted: Dict[str, object],
    db_path: Path = DEFAULT_DATABASE_PATH,
    *,
    backup_existing: bool = True,
) -> Dict[str, object]:
    """Atomically replace one imported source without rebuilding the corpus."""

    _sanitize_surrogates_in_place(extracted)
    sources = [item for item in extracted.get("source_files", []) if isinstance(item, dict)]
    if len(sources) != 1 or not sources[0].get("source_file_id"):
        raise ValueError("A targeted database update requires exactly one source file.")
    source = sources[0]
    source_id = str(source["source_file_id"])
    db_path = Path(db_path)
    backup_path = _backup_database(db_path) if backup_existing else None
    return replace_source_rows(extracted, db_path, source, source_id, backup_path)


def delete_sources_from_database(
    source_file_ids: Sequence[str],
    db_path: Path = DEFAULT_DATABASE_PATH,
    *,
    backup_existing: bool = True,
) -> Dict[str, object]:
    """Delete several sources and their search rows in one transaction.

    One snapshot covers the whole batch. Backing up per document turned a
    61-document removal into 61 full copies of a multi-GB index — roughly
    200 GB of disk writes for what is otherwise a millisecond-scale delete.
    """

    ids: List[str] = []
    for value in source_file_ids:
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
    if not ids:
        raise ValueError("source_file_id is required")
    db_path = Path(db_path)
    backup_path = _backup_database(db_path) if backup_existing else None
    return delete_source_rows(ids, db_path, backup_path)


def delete_source_from_database(
    source_file_id: str,
    db_path: Path = DEFAULT_DATABASE_PATH,
    *,
    backup_existing: bool = True,
) -> Dict[str, object]:
    """Delete one source and all source-owned search rows in one transaction."""

    source_file_id = str(source_file_id or "").strip()
    if not source_file_id:
        raise ValueError("source_file_id is required")
    result = delete_sources_from_database(
        [source_file_id], db_path, backup_existing=backup_existing
    )
    return {
        "source_file_id": source_file_id,
        "deleted": result["deleted"][source_file_id],
        "backup_path": result["backup_path"],
        "source_count": result["source_count"],
        "paragraph_count": result["paragraph_count"],
        "eligible_paragraph_count": result["eligible_paragraph_count"],
    }


def open_database(db_path: Path) -> sqlite3.Connection:
    return open_readonly_index(db_path)
