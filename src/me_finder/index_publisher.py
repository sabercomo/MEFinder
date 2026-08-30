"""Publish configured document sources into the local search index."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Dict, Optional

from .import_config_store import (
    import_config_lock,
    load_import_config,
    save_import_config,
)
from .indexer import build_index
from .mineru_api import MinerUError


ProgressCallback = Callable[[Dict[str, object]], None]


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
        # Public builds ship without Word corpus; PDF-only indexing is normal.
        # Refuse only when Word documents are indexed, since rebuilding without
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
    # file directly. This prevents legacy duplicate content IDs from reaching
    # SQLite's source_files primary key.
    pdf_config_path = root / "config" / "pdf_imports.json"
    with import_config_lock():
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
