"""SQLite storage for the local literature index.

The JSON index remains available as an export and migration fallback. The
desktop application and the default search path use this SQLite database so
the full paragraph corpus is not loaded from one large JSON document.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from .persistence.connection import open_readonly_index
from .database_backup import (
    DATABASE_BACKUP_FREE_SPACE_MARGIN,
    DATABASE_BACKUP_RETENTION,
    DATABASE_REBUILD_ESTIMATE_FLOOR,
    _backup_database,
    _prune_database_backups,
    backup_database,
)
from .index_identity import (
    IndexIdentityConflictError,
    _deduplicate_keyed_rows,
    _deduplicate_source_files,
    _identity_conflict,
    _is_empty,
    _merge_missing_fields,
    _verify_source_identity,
)
from .persistence.index_schema import (
    ANCHOR_SPEC_VERSION,
    DATABASE_SCHEMA_VERSION,
    DEFAULT_DATABASE_PATH,
    PARAGRAPH_FTS_VERSION,
    SCHEMA,
)

DATABASE_REPLACE_ATTEMPTS = 15
DATABASE_REPLACE_INITIAL_DELAY_SECONDS = 0.1
DATABASE_REPLACE_MAX_DELAY_SECONDS = 1.0


# These four large strings already have authoritative typed columns.  Keeping
# them in payload_json as well made every paragraph carry two complete copies
# of all searchable text representations.  New/rebuilt databases store a
# sparse payload; paragraph_from_database_row transparently hydrates both old
# full payloads and new sparse ones.
PARAGRAPH_PAYLOAD_OMITTED_FIELDS = frozenset(
    {"text_raw", "normalized_text", "compact_text", "plain_text", "sentences"}
)

PARAGRAPH_TYPED_COLUMNS = (
    "paragraph_id",
    "volume_id",
    "work_id",
    "source_file_id",
    "source_type",
    "paragraph_index",
    "eligible_for_search",
    "text_raw",
    "normalized_text",
    "compact_text",
    "plain_text",
    "page_display",
    "page_source_type",
    "page_confidence",
    "citation_page_start",
    "citation_page_end",
    "pdf_page_start_index",
    "pdf_page_end_index",
    "pdf_page_start_label",
    "pdf_page_end_label",
)

PARAGRAPH_SELECT_COLUMNS = ", ".join(
    f"p.{column} AS {column}" for column in PARAGRAPH_TYPED_COLUMNS
) + ", p.payload_json AS payload_json"

_FTS_INSTALL_LOCK = threading.Lock()


def paragraph_payload_for_storage(paragraph: Dict[str, object]) -> Dict[str, object]:
    """Return the non-duplicated JSON portion of one paragraph record."""

    return {
        key: value
        for key, value in paragraph.items()
        if key not in PARAGRAPH_PAYLOAD_OMITTED_FIELDS
    }


def paragraph_from_database_row(row: sqlite3.Row) -> Dict[str, object]:
    """Hydrate one canonical paragraph from a sparse or legacy database row."""

    raw_payload = row["payload_json"]
    try:
        payload = json.loads(raw_payload) if raw_payload else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    available = set(row.keys())
    for column in PARAGRAPH_TYPED_COLUMNS:
        if column not in available:
            continue
        value = row[column]
        # A few pre-v1/import-recovery databases populated optional values in
        # JSON but left the newer typed columns NULL. Preserve that legacy
        # value; all current writers update both representations together.
        if value is None and payload.get(column) not in (None, ""):
            continue
        if column == "eligible_for_search":
            value = bool(value)
        payload[column] = value
    return payload


def _fts_objects_present(connection: sqlite3.Connection) -> bool:
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('paragraphs_fts', 'paragraphs_fts_ai', 'paragraphs_fts_ad', 'paragraphs_fts_au')"
        )
    }
    return names == {
        "paragraphs_fts",
        "paragraphs_fts_ai",
        "paragraphs_fts_ad",
        "paragraphs_fts_au",
    }


def database_has_fts5_search_index(connection: sqlite3.Connection) -> bool:
    """Return whether the versioned trigram FTS index is ready for queries."""

    if not _fts_objects_present(connection):
        return False
    row = connection.execute(
        "SELECT value_json FROM metadata WHERE key = 'paragraph_fts_version'"
    ).fetchone()
    if row is None:
        return False
    try:
        return int(json.loads(row[0])) == PARAGRAPH_FTS_VERSION
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _database_uses_sparse_paragraph_payload(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT value_json FROM metadata WHERE key = 'paragraph_payload_storage'"
    ).fetchone()
    if row is None:
        return False
    try:
        return json.loads(row[0]) == "sparse_text_v1"
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _install_fts5_search_index(
    connection: sqlite3.Connection,
    *,
    rebuild: bool,
) -> bool:
    """Install the external-content trigram index on an open write connection.

    ``detail=none`` and ``columnsize=0`` keep the index materially smaller than
    another stored copy of paragraph text.  Search code submits a bounded set
    of trigram terms and verifies every candidate against the canonical typed
    columns, so positional detail is unnecessary.
    """

    connection.execute("SAVEPOINT install_paragraphs_fts")
    try:
        statements = (
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS paragraphs_fts USING fts5(
                plain_text,
                content='paragraphs',
                content_rowid='rowid',
                tokenize='trigram',
                detail='none',
                columnsize=0
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS paragraphs_fts_ai
            AFTER INSERT ON paragraphs BEGIN
                INSERT INTO paragraphs_fts(rowid, plain_text)
                VALUES (new.rowid, new.plain_text);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS paragraphs_fts_ad
            AFTER DELETE ON paragraphs BEGIN
                INSERT INTO paragraphs_fts(paragraphs_fts, rowid, plain_text)
                VALUES ('delete', old.rowid, old.plain_text);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS paragraphs_fts_au
            AFTER UPDATE OF plain_text ON paragraphs BEGIN
                INSERT INTO paragraphs_fts(paragraphs_fts, rowid, plain_text)
                VALUES ('delete', old.rowid, old.plain_text);
                INSERT INTO paragraphs_fts(rowid, plain_text)
                VALUES (new.rowid, new.plain_text);
            END
            """,
        )
        for statement in statements:
            connection.execute(statement)
        if rebuild:
            connection.execute(
                "INSERT INTO paragraphs_fts(paragraphs_fts) VALUES ('rebuild')"
            )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            ("paragraph_fts_version", _json(PARAGRAPH_FTS_VERSION)),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            ("database_schema_version", _json(DATABASE_SCHEMA_VERSION)),
        )
        connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
        connection.execute("RELEASE SAVEPOINT install_paragraphs_fts")
        return True
    except sqlite3.OperationalError:
        # Some distributor-provided SQLite builds omit FTS5 or the trigram
        # tokenizer.  The caller keeps the legacy scan path available.
        connection.execute("ROLLBACK TO SAVEPOINT install_paragraphs_fts")
        connection.execute("RELEASE SAVEPOINT install_paragraphs_fts")
        return False


def ensure_database_search_index(db_path: Path) -> bool:
    """Upgrade paragraph storage and create FTS once, with scan fallback."""

    db_path = Path(db_path)
    with _FTS_INSTALL_LOCK:
        connection = sqlite3.connect(str(db_path))
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            fts_ready = database_has_fts5_search_index(connection)
            sparse_payload = _database_uses_sparse_paragraph_payload(connection)
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if fts_ready and sparse_payload:
                return True
        finally:
            connection.close()

        if not sparse_payload and user_version <= DATABASE_SCHEMA_VERSION:
            try:
                if optimize_database_storage(db_path):
                    return True
            except (OSError, sqlite3.Error, ValueError):
                # The old file is still authoritative until the final rename.
                # Insufficient space, an active Windows file handle, or an
                # unavailable tokenizer therefore degrades to the additive
                # migration below (or ultimately to the legacy scan path).
                pass

        if fts_ready:
            return True

        connection = sqlite3.connect(str(db_path))
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE")
            installed = _install_fts5_search_index(connection, rebuild=True)
            if installed:
                connection.commit()
            else:
                connection.rollback()
            return installed
        except sqlite3.Error:
            connection.rollback()
            return False
        finally:
            connection.close()


def optimize_database_storage(db_path: Path) -> bool:
    """Stream one legacy database into a sparse, validated replacement.

    The source file is never updated in place.  A complete temporary database
    is built on the same volume, checked, fsynced, and only then swapped in;
    the old file becomes a normal retained backup.  This is what actually
    reclaims duplicated payload bytes without an UPDATE+VACUUM space spike.
    """

    db_path = Path(db_path)
    if not db_path.exists():
        return False
    for suffix in ("-wal", "-shm", "-journal"):
        if db_path.with_name(db_path.name + suffix).exists():
            return False
    required_free = db_path.stat().st_size + DATABASE_BACKUP_FREE_SPACE_MARGIN
    if shutil.disk_usage(db_path.parent).free < required_free:
        return False

    temp_path = db_path.with_name(
        f".{db_path.name}.optimize-{os.getpid()}-{threading.get_ident()}.tmp"
    )
    temp_path.unlink(missing_ok=True)
    connection = sqlite3.connect(str(temp_path))
    try:
        connection.executescript(SCHEMA)
        connection.execute("ATTACH DATABASE ? AS legacy", (str(db_path),))
        connection.execute("BEGIN IMMEDIATE")
        table_names = (
            "metadata",
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
            "document_groups",
            "document_group_members",
            "segment_sets",
            "text_segments",
            "text_segment_spans",
            "text_segment_paragraph_spans",
            "alignment_runs",
            "alignment_links",
            "alignment_link_members",
        )
        for table_name in table_names:
            destination_columns = [
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA main.table_info({table_name})"
                )
            ]
            source_columns = {
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA legacy.table_info({table_name})"
                )
            }
            common_columns = [
                column for column in destination_columns if column in source_columns
            ]
            if not common_columns:
                continue
            select_expressions = list(common_columns)
            if table_name == "paragraphs" and "payload_json" in common_columns:
                payload_index = common_columns.index("payload_json")
                json_paths = ", ".join(
                    repr(f"$.{field}")
                    for field in sorted(PARAGRAPH_PAYLOAD_OMITTED_FIELDS)
                )
                select_expressions[payload_index] = (
                    "CASE WHEN json_valid(payload_json) "
                    f"THEN json_remove(payload_json, {json_paths}) "
                    "ELSE payload_json END"
                )
            columns_sql = ", ".join(common_columns)
            select_sql = ", ".join(select_expressions)
            connection.execute(
                f"INSERT INTO main.{table_name}({columns_sql}) "
                f"SELECT {select_sql} FROM legacy.{table_name}"
            )

        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            ("paragraph_payload_storage", _json("sparse_text_v1")),
        )
        if not _install_fts5_search_index(connection, rebuild=True):
            raise sqlite3.OperationalError("FTS5 trigram tokenizer is unavailable")
        source_count = connection.execute(
            "SELECT COUNT(*) FROM legacy.paragraphs"
        ).fetchone()[0]
        target_count = connection.execute(
            "SELECT COUNT(*) FROM main.paragraphs"
        ).fetchone()[0]
        if source_count != target_count:
            raise ValueError("Paragraph count changed during database optimization.")
        connection.execute(
            "INSERT INTO paragraphs_fts(paragraphs_fts, rank) "
            "VALUES ('integrity-check', 1)"
        )
        integrity = connection.execute("PRAGMA main.integrity_check").fetchone()[0]
        if str(integrity).lower() != "ok":
            raise ValueError(f"Optimized database integrity check failed: {integrity}")
        connection.commit()
        connection.execute("DETACH DATABASE legacy")
        connection.close()
        with temp_path.open("rb+") as stream:
            os.fsync(stream.fileno())

        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        backup_path = backup_dir / f"{db_path.stem}-{stamp}{db_path.suffix}"
        _replace_database_file(db_path, backup_path)
        try:
            _replace_database_file(temp_path, db_path)
        except OSError:
            _replace_database_file(backup_path, db_path)
            raise
        _prune_database_backups(backup_dir, db_path)
        return True
    except Exception:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        try:
            connection.close()
        except sqlite3.Error:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


def _insert_page_anchors(
    connection: sqlite3.Connection,
    anchors: Sequence[Dict[str, object]],
) -> None:
    """Store canonical page anchors in the legacy schema-v2 table.

    The v2 table called its typed lookup column ``paragraph_id``, while the
    page-anchor model has always called that relationship
    ``start_paragraph_id`` and also keeps ``end_paragraph_id`` and
    ``source_file_id`` in its payload.  Treat the legacy column as a typed
    alias for the start paragraph instead of silently writing NULL.
    """

    values = []
    for anchor in anchors:
        start_paragraph_id = anchor.get("start_paragraph_id")
        if start_paragraph_id in (None, ""):
            # Accept an old exported record that used the physical v2 column
            # name, while current extractors use the canonical field name.
            start_paragraph_id = anchor.get("paragraph_id")
        values.append(
            (
                str(start_paragraph_id)
                if start_paragraph_id not in (None, "")
                else None,
                _json(anchor),
            )
        )
    if values:
        connection.executemany(
            "INSERT INTO page_anchors(paragraph_id, payload_json) VALUES (?, ?)",
            values,
        )


def _delete_page_anchors_for_source(
    connection: sqlite3.Connection,
    source_file_id: str,
) -> int:
    """Delete current and legacy-v2 anchors owned by one source.

    Older writers left ``page_anchors.paragraph_id`` NULL because of the
    field-name mismatch.  Their canonical ownership data is still present in
    payload_json, so source deletion must consult source/start/end there as
    well as the repaired typed start-paragraph alias.
    """

    # Keep current typed rows entirely inside SQLite.  Expanding every
    # paragraph id into an ``IN (?, ...)`` list crosses SQLite's variable
    # limit for large books (32,766 on the Windows build).
    typed_anchor_filter = (
        "paragraph_id IN ("
        "SELECT paragraph_id FROM paragraphs WHERE source_file_id = ?"
        ")"
    )
    deleted_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM page_anchors WHERE {typed_anchor_filter}",
            (source_file_id,),
        ).fetchone()[0]
    )
    connection.execute(
        f"DELETE FROM page_anchors WHERE {typed_anchor_filter}",
        (source_file_id,),
    )

    # Legacy NULL-typed rows still need payload inspection.  Materializing the
    # ids for Python membership tests is safe here because they are no longer
    # rebound as one SQL statement's parameters.
    paragraph_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT paragraph_id FROM paragraphs WHERE source_file_id = ?",
            (source_file_id,),
        )
    }

    # Only old buggy v2 rows require payload inspection.  Correctly written
    # anchors are handled by the typed-column delete above, avoiding a
    # full JSON scan for every source in ordinary batch removals.
    owned_row_ids: List[int] = []
    for row_id, raw_payload in connection.execute(
        "SELECT row_id, payload_json FROM page_anchors WHERE paragraph_id IS NULL"
    ):
        belongs_to_source = False
        try:
            payload = json.loads(raw_payload) if raw_payload else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            payload_source_id = str(payload.get("source_file_id") or "")
            start_paragraph_id = str(payload.get("start_paragraph_id") or "")
            end_paragraph_id = str(payload.get("end_paragraph_id") or "")
            belongs_to_source = belongs_to_source or (
                payload_source_id == source_file_id
                or start_paragraph_id in paragraph_ids
                or end_paragraph_id in paragraph_ids
            )
        if belongs_to_source:
            owned_row_ids.append(int(row_id))
    if owned_row_ids:
        connection.executemany(
            "DELETE FROM page_anchors WHERE row_id = ?",
            [(row_id,) for row_id in owned_row_ids],
        )
    return deleted_count + len(owned_row_ids)


def _int_or_none(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# 每份快照都是整个索引的完整副本。真实语料下单份就有 3.5GB，不设上限时
# 一次批量删除就能在数据目录里堆出几百 GB。


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
    from .text_alignment import (
        read_alignment_recipe_snapshot,
        restore_alignment_recipe_snapshot,
    )

    preserved_document_groups = read_document_group_snapshot(db_path)
    preserved_alignments = read_alignment_recipe_snapshot(db_path)
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
    connection = sqlite3.connect(str(temp_path))
    fts_installed = False
    try:
        connection.executescript(SCHEMA)
        metadata = dict(index.get("metadata") or {})
        metadata["database_schema_version"] = DATABASE_SCHEMA_VERSION
        metadata["paragraph_payload_storage"] = "sparse_text_v1"
        metadata.setdefault("anchor_spec_version", ANCHOR_SPEC_VERSION)
        metadata["database_built_at"] = datetime.now(timezone.utc).isoformat()
        metadata["source_count"] = len(source_files)
        metadata["paragraph_count"] = len(paragraphs)
        metadata["eligible_paragraph_count"] = sum(
            1 for item in paragraphs if item.get("eligible_for_search")
        )
        if deduplicated_rows:
            metadata["database_deduplication"] = {
                "strategy": "first_record_wins_and_fills_missing_fields",
                "merged_rows": deduplicated_rows,
            }
        connection.executemany(
            "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
            [(str(key), _json(value)) for key, value in metadata.items()],
        )

        connection.executemany(
            """
            INSERT INTO source_files(
                source_file_id, source_type, file_name, relative_path, volume_number, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(item.get("source_file_id") or ""),
                    str(item.get("source_type") or "word"),
                    item.get("file_name"),
                    item.get("relative_path"),
                    _int_or_none(item.get("volume_number")),
                    _json(item),
                )
                for item in source_files
                if item.get("source_file_id")
            ],
        )

        # SourceFiles now exist in the rebuilt DB; re-apply preserved groups,
        # skipping members whose source is gone and clearing a missing base.
        restore_document_group_snapshot(connection, preserved_document_groups)

        connection.executemany(
            """
            INSERT INTO volumes(volume_id, source_file_id, source_type, volume_number, display_title, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(item.get("volume_id") or ""),
                    item.get("source_file_id"),
                    str(item.get("source_type") or "word"),
                    _int_or_none(item.get("volume_number")),
                    item.get("display_title"),
                    _json(item),
                )
                for item in volumes
                if item.get("volume_id")
            ],
        )

        connection.executemany(
            """
            INSERT OR REPLACE INTO works(work_id, volume_id, source_type, work_order, title, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(item.get("work_id") or ""),
                    item.get("volume_id"),
                    str(item.get("source_type") or "word"),
                    _int_or_none(item.get("work_order")),
                    item.get("title"),
                    _json(item),
                )
                for item in works
                if item.get("work_id")
            ],
        )

        toc_entries = [item for item in index.get("toc_entries", []) if isinstance(item, dict)]
        connection.executemany(
            "INSERT INTO toc_entries(volume_id, work_id, title, payload_json) VALUES (?, ?, ?, ?)",
            [(item.get("volume_id"), item.get("work_id"), item.get("title"), _json(item)) for item in toc_entries],
        )

        paragraph_rows = []
        for item in paragraphs:
            paragraph_id = str(item.get("paragraph_id") or "")
            if not paragraph_id:
                continue
            paragraph_rows.append(
                (
                    paragraph_id,
                    item.get("volume_id"),
                    item.get("work_id"),
                    str(item.get("source_file_id") or ""),
                    str(item.get("source_type") or "word"),
                    int(item.get("paragraph_index") or 0),
                    1 if item.get("eligible_for_search") else 0,
                    str(item.get("text_raw") or ""),
                    str(item.get("normalized_text") or ""),
                    str(item.get("compact_text") or ""),
                    str(item.get("plain_text") or ""),
                    item.get("page_display"),
                    item.get("page_source_type"),
                    _float_or_none(item.get("page_confidence")),
                    item.get("citation_page_start"),
                    item.get("citation_page_end"),
                    _int_or_none(item.get("pdf_page_start_index")),
                    _int_or_none(item.get("pdf_page_end_index")),
                    item.get("pdf_page_start_label"),
                    item.get("pdf_page_end_label"),
                    _json(paragraph_payload_for_storage(item)),
                )
            )
        connection.executemany(
            """
            INSERT INTO paragraphs(
                paragraph_id, volume_id, work_id, source_file_id, source_type, paragraph_index,
                eligible_for_search, text_raw, normalized_text, compact_text, plain_text,
                page_display, page_source_type, page_confidence, citation_page_start, citation_page_end,
                pdf_page_start_index, pdf_page_end_index, pdf_page_start_label, pdf_page_end_label, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            paragraph_rows,
        )
        _insert_page_anchors(connection, page_anchors)
        for table_name, key_fields in (
            ("pdf_pages", ("source_file_id", "pdf_page_index")),
            ("pdf_page_mappings", ("source_file_id", "pdf_page_index")),
            ("pdf_import_runs", ("source_file_id", "status")),
            ("audit_issues", ("source_file_id", "issue_type")),
        ):
            if table_name == "pdf_pages":
                rows = pdf_pages
            elif table_name == "pdf_page_mappings":
                rows = pdf_page_mappings
            else:
                rows = [
                    item
                    for item in index.get(table_name, [])
                    if isinstance(item, dict)
                ]
            columns = ", ".join(key_fields) + ", payload_json"
            placeholders = ", ".join("?" for _ in key_fields) + ", ?"
            sql = f"INSERT INTO {table_name}({columns}) VALUES ({placeholders})"
            values = [tuple(item.get(field) for field in key_fields) + (_json(item),) for item in rows]
            if values:
                connection.executemany(sql, values)

        # Automatic links are derived from PDF text, but they are also a
        # user-requested computation. Recreate the same completed pairs after
        # the replacement index has published its fresh page text.
        connection.row_factory = sqlite3.Row
        restore_alignment_recipe_snapshot(connection, preserved_alignments)

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


def _load_payload_rows(connection: sqlite3.Connection, table: str, order_by: str = "rowid") -> List[Dict[str, object]]:
    return [json.loads(row[0]) for row in connection.execute(f"SELECT payload_json FROM {table} ORDER BY {order_by}")]


def load_database_index(db_path: Path) -> Dict[str, object]:
    """Load the small metadata/catalog portion used by the Web UI."""

    connection = sqlite3.connect(str(db_path))
    try:
        metadata = {str(row[0]): json.loads(row[1]) for row in connection.execute("SELECT key, value_json FROM metadata")}
        result = {
            "metadata": metadata,
            "source_files": _load_payload_rows(connection, "source_files", "source_file_id"),
            "volumes": _load_payload_rows(connection, "volumes", "volume_id"),
            "works": _load_payload_rows(connection, "works", "rowid"),
        }
        return result
    finally:
        connection.close()


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
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        old_volume_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT volume_id FROM volumes WHERE source_file_id = ?", (source_id,)
            ).fetchall()
        ]
        old_work_ids: List[str] = []
        if old_volume_ids:
            placeholders = ",".join("?" for _ in old_volume_ids)
            old_work_ids = [
                str(row[0])
                for row in connection.execute(
                    f"SELECT work_id FROM works WHERE volume_id IN ({placeholders})", old_volume_ids
                ).fetchall()
            ]
        _delete_page_anchors_for_source(connection, source_id)
        if old_volume_ids:
            placeholders = ",".join("?" for _ in old_volume_ids)
            connection.execute(f"DELETE FROM toc_entries WHERE volume_id IN ({placeholders})", old_volume_ids)
        if old_work_ids:
            placeholders = ",".join("?" for _ in old_work_ids)
            connection.execute(f"DELETE FROM toc_entries WHERE work_id IN ({placeholders})", old_work_ids)
            connection.execute(f"DELETE FROM works WHERE work_id IN ({placeholders})", old_work_ids)
        connection.execute("DELETE FROM paragraphs WHERE source_file_id = ?", (source_id,))
        for table in ("pdf_pages", "pdf_page_mappings", "pdf_import_runs", "audit_issues"):
            connection.execute(f"DELETE FROM {table} WHERE source_file_id = ?", (source_id,))
        connection.execute("DELETE FROM volumes WHERE source_file_id = ?", (source_id,))
        connection.execute("DELETE FROM source_files WHERE source_file_id = ?", (source_id,))

        connection.execute(
            """
            INSERT INTO source_files(
                source_file_id, source_type, file_name, relative_path, volume_number, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                str(source.get("source_type") or "word"),
                source.get("file_name"),
                source.get("relative_path"),
                _int_or_none(source.get("volume_number")),
                _json(source),
            ),
        )
        volumes = [item for item in extracted.get("volumes", []) if isinstance(item, dict)]
        connection.executemany(
            """
            INSERT INTO volumes(volume_id, source_file_id, source_type, volume_number, display_title, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(item.get("volume_id") or ""),
                    item.get("source_file_id"),
                    str(item.get("source_type") or "word"),
                    _int_or_none(item.get("volume_number")),
                    item.get("display_title"),
                    _json(item),
                )
                for item in volumes
                if item.get("volume_id")
            ],
        )
        works = [item for item in extracted.get("works", []) if isinstance(item, dict)]
        connection.executemany(
            """
            INSERT INTO works(work_id, volume_id, source_type, work_order, title, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(item.get("work_id") or ""),
                    item.get("volume_id"),
                    str(item.get("source_type") or "word"),
                    _int_or_none(item.get("work_order")),
                    item.get("title"),
                    _json(item),
                )
                for item in works
                if item.get("work_id")
            ],
        )
        toc_entries = [item for item in extracted.get("toc_entries", []) if isinstance(item, dict)]
        connection.executemany(
            "INSERT INTO toc_entries(volume_id, work_id, title, payload_json) VALUES (?, ?, ?, ?)",
            [(item.get("volume_id"), item.get("work_id"), item.get("title"), _json(item)) for item in toc_entries],
        )
        paragraphs = [item for item in extracted.get("paragraphs", []) if isinstance(item, dict)]
        connection.executemany(
            """
            INSERT INTO paragraphs(
                paragraph_id, volume_id, work_id, source_file_id, source_type, paragraph_index,
                eligible_for_search, text_raw, normalized_text, compact_text, plain_text,
                page_display, page_source_type, page_confidence, citation_page_start, citation_page_end,
                pdf_page_start_index, pdf_page_end_index, pdf_page_start_label, pdf_page_end_label, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(item.get("paragraph_id") or ""),
                    item.get("volume_id"),
                    item.get("work_id"),
                    source_id,
                    str(item.get("source_type") or "word"),
                    int(item.get("paragraph_index") or 0),
                    1 if item.get("eligible_for_search") else 0,
                    str(item.get("text_raw") or ""),
                    str(item.get("normalized_text") or ""),
                    str(item.get("compact_text") or ""),
                    str(item.get("plain_text") or ""),
                    item.get("page_display"),
                    item.get("page_source_type"),
                    _float_or_none(item.get("page_confidence")),
                    item.get("citation_page_start"),
                    item.get("citation_page_end"),
                    _int_or_none(item.get("pdf_page_start_index")),
                    _int_or_none(item.get("pdf_page_end_index")),
                    item.get("pdf_page_start_label"),
                    item.get("pdf_page_end_label"),
                    _json(paragraph_payload_for_storage(item)),
                )
                for item in paragraphs
                if item.get("paragraph_id")
            ],
        )
        _insert_page_anchors(
            connection,
            [
                item
                for item in extracted.get("page_anchors", [])
                if isinstance(item, dict)
            ],
        )
        for table_name, key_fields in (
            ("pdf_pages", ("source_file_id", "pdf_page_index")),
            ("pdf_page_mappings", ("source_file_id", "pdf_page_index")),
            ("pdf_import_runs", ("source_file_id", "status")),
            ("audit_issues", ("source_file_id", "issue_type")),
        ):
            rows = [item for item in extracted.get(table_name, []) if isinstance(item, dict)]
            if not rows:
                continue
            columns = ", ".join(key_fields) + ", payload_json"
            placeholders = ", ".join("?" for _ in key_fields) + ", ?"
            connection.executemany(
                f"INSERT INTO {table_name}({columns}) VALUES ({placeholders})",
                [tuple(item.get(field) for field in key_fields) + (_json(item),) for item in rows],
            )

        totals = {
            "source_count": connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
            "paragraph_count": connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0],
            "eligible_paragraph_count": connection.execute(
                "SELECT COUNT(*) FROM paragraphs WHERE eligible_for_search = 1"
            ).fetchone()[0],
            "anchor_spec_version": ANCHOR_SPEC_VERSION,
        }
        connection.executemany(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            [(key, _json(value)) for key, value in totals.items()],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "source_file_id": source_id,
        "paragraph_count": len(paragraphs),
        "eligible_paragraph_count": sum(1 for item in paragraphs if item.get("eligible_for_search")),
        "backup_path": str(backup_path) if backup_path else None,
        **totals,
    }


def _delete_one_source(connection: sqlite3.Connection, source_file_id: str) -> Dict[str, int]:
    """Delete one source's rows on an open transaction and report the counts."""

    source = connection.execute(
        "SELECT source_type FROM source_files WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()
    if source is None:
        raise ValueError("文献不存在。")
    if str(source[0]) not in {"pdf", "word"}:
        raise ValueError("当前移除服务仅允许处理 PDF 或 Word 文献。")
    volume_ids = [
        str(row[0])
        for row in connection.execute(
            "SELECT volume_id FROM volumes WHERE source_file_id = ?", (source_file_id,)
        ).fetchall()
    ]
    work_ids: List[str] = []
    if volume_ids:
        placeholders = ",".join("?" for _ in volume_ids)
        work_ids = [
            str(row[0])
            for row in connection.execute(
                f"SELECT work_id FROM works WHERE volume_id IN ({placeholders})", volume_ids
            ).fetchall()
        ]
    counts: Dict[str, int] = {}
    counts["paragraphs"] = connection.execute(
        "SELECT COUNT(*) FROM paragraphs WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()[0]
    counts["pdf_pages"] = connection.execute(
        "SELECT COUNT(*) FROM pdf_pages WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()[0]
    counts["page_anchors"] = _delete_page_anchors_for_source(
        connection, source_file_id
    )
    if volume_ids:
        placeholders = ",".join("?" for _ in volume_ids)
        connection.execute(f"DELETE FROM toc_entries WHERE volume_id IN ({placeholders})", volume_ids)
    if work_ids:
        placeholders = ",".join("?" for _ in work_ids)
        connection.execute(f"DELETE FROM toc_entries WHERE work_id IN ({placeholders})", work_ids)
        connection.execute(f"DELETE FROM works WHERE work_id IN ({placeholders})", work_ids)
    timestamp = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "UPDATE document_groups SET base_source_file_id = NULL, updated_at = ? "
        "WHERE base_source_file_id = ?",
        (timestamp, source_file_id),
    )
    connection.execute(
        "DELETE FROM document_group_members WHERE source_file_id = ?",
        (source_file_id,),
    )
    connection.execute("DELETE FROM paragraphs WHERE source_file_id = ?", (source_file_id,))
    for table in ("pdf_pages", "pdf_page_mappings", "pdf_import_runs", "audit_issues"):
        connection.execute(f"DELETE FROM {table} WHERE source_file_id = ?", (source_file_id,))
    connection.execute("DELETE FROM volumes WHERE source_file_id = ?", (source_file_id,))
    connection.execute("DELETE FROM source_files WHERE source_file_id = ?", (source_file_id,))
    return counts


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
    connection = sqlite3.connect(str(db_path))
    deleted: Dict[str, Dict[str, int]] = {}
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for source_file_id in ids:
            deleted[source_file_id] = _delete_one_source(connection, source_file_id)
        totals = {
            "source_count": connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
            "paragraph_count": connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0],
            "eligible_paragraph_count": connection.execute(
                "SELECT COUNT(*) FROM paragraphs WHERE eligible_for_search = 1"
            ).fetchone()[0],
        }
        connection.executemany(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            [(key, _json(value)) for key, value in totals.items()],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "source_file_ids": ids,
        "deleted": deleted,
        "backup_path": str(backup_path) if backup_path else None,
        **totals,
    }


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
