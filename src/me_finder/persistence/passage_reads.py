"""Read-only SQLite queries behind relevance retrieval (``search_passages``).

Every function takes the caller's connection and typed filters; the scope
predicate comes from :mod:`persistence.paragraph_scope`, the same one the
recall cascade uses.  Rows are returned as stored — gram overlap scoring,
tie-breaking and truncation stay in ``search_recall_passages``.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence

from .paragraph_payload import PARAGRAPH_SELECT_COLUMNS
from .paragraph_scope import Scope, source_filter_clause
from .short_gram_index import short_gram_prefilter


def read_bm25_passage_rows(
    connection: sqlite3.Connection,
    fts_query: str,
    source_type: str,
    source_file_id: Optional[str],
    scope: Scope,
    limit: int,
) -> List[sqlite3.Row]:
    """Return paragraphs matching ``fts_query``, best BM25 first, plus ``bm25_score``."""

    source_clause, source_args = source_filter_clause(source_type, source_file_id, scope, "p")
    return connection.execute(
        f"SELECT {PARAGRAPH_SELECT_COLUMNS}, "
        "bm25(paragraphs_fts) AS bm25_score "
        "FROM paragraphs_fts JOIN paragraphs p "
        "ON p.rowid = paragraphs_fts.rowid "
        "WHERE paragraphs_fts MATCH ? AND p.eligible_for_search = 1"
        + source_clause
        + " ORDER BY bm25(paragraphs_fts) LIMIT ?",
        [fts_query, *source_args, limit],
    ).fetchall()


def read_gram_passage_rows(
    connection: sqlite3.Connection,
    grams: Sequence[str],
    source_type: str,
    source_file_id: Optional[str],
    scope: Scope,
    prefilter_ready: bool,
) -> sqlite3.Cursor:
    """Stream eligible paragraphs for a query FTS cannot serve.

    The caller ranks every row, so the cursor is streamed rather than fetched:
    a two-character query can match most of a library.  ``grams`` only narrows
    the scan — a paragraph sharing no query gram scores zero overlap and is
    dropped anyway, so the prefilter never changes which rows can rank.
    """

    source_clause, source_args = source_filter_clause(source_type, source_file_id, scope, "p")
    gram_clause, gram_args = short_gram_prefilter(grams) if prefilter_ready else ("", [])
    return connection.execute(
        f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p "
        "WHERE p.eligible_for_search = 1" + source_clause + gram_clause,
        [*source_args, *gram_args],
    )
