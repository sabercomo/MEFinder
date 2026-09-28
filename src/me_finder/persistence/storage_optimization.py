"""Stream a legacy index into a sparse, validated replacement."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .connection import open_build_target
from .fts_index import _install_fts5_search_index
from .index_build import _json
from .index_schema import SCHEMA
from .paragraph_payload import PARAGRAPH_PAYLOAD_OMITTED_FIELDS


def optimize_database_storage(
    db_path: Path,
    *,
    replace_database_file: Callable[[Path, Path], None],
    prune_database_backups: Callable[[Path, Path], object],
    backup_free_space_margin: int,
) -> bool:
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
    required_free = db_path.stat().st_size + backup_free_space_margin
    if shutil.disk_usage(db_path.parent).free < required_free:
        return False

    temp_path = db_path.with_name(
        f".{db_path.name}.optimize-{os.getpid()}-{threading.get_ident()}.tmp"
    )
    temp_path.unlink(missing_ok=True)
    connection = open_build_target(temp_path)
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
        replace_database_file(db_path, backup_path)
        try:
            replace_database_file(temp_path, db_path)
        except OSError:
            replace_database_file(backup_path, db_path)
            raise
        prune_database_backups(backup_dir, db_path)
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
