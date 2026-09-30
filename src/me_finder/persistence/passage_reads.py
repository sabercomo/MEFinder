"""Read-only SQLite queries behind gram-overlap scans.

Used by relevance retrieval (``search_passages``) and the short-query fuzzy
pass.  Every function takes the caller's connection and typed filters; the
scope predicate comes from :mod:`persistence.paragraph_scope`, the same one
the recall cascade uses.  Rows are returned as stored — gram overlap scoring,
tie-breaking and truncation stay with the callers.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence, Tuple

from .paragraph_payload import PARAGRAPH_SELECT_COLUMNS
from .paragraph_scope import Scope, source_filter_clause
from .short_gram_index import short_gram_prefilter

# Just what a gram-overlap ranking reads; full rows follow by rowid for the
# few that survive the cut (``payload_json`` dominates a whole-library scan).
RANK_COLUMNS = (
    "p.rowid AS paragraph_rowid, p.paragraph_id AS paragraph_id, "
    "p.source_file_id AS source_file_id, p.paragraph_index AS paragraph_index, "
    "p.plain_text AS plain_text"
)


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
    columns: str = PARAGRAPH_SELECT_COLUMNS,
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
        f"SELECT {columns} FROM paragraphs p "
        "WHERE p.eligible_for_search = 1" + source_clause + gram_clause,
        [*source_args, *gram_args],
    )


def read_paragraph_rows(
    connection: sqlite3.Connection, keys: Sequence[Tuple[int, str]]
) -> List[sqlite3.Row]:
    """Full rows for ``(rowid, paragraph_id)`` keys from a rank scan, in order.

    A key whose rowid is gone or now holds another paragraph (a concurrent
    delete between the two reads) is dropped, never swapped for a stranger.
    """

    found = {}
    for offset in range(0, len(keys), 500):
        chunk = [rowid for rowid, _paragraph_id in keys[offset : offset + 500]]
        placeholders = ", ".join("?" for _ in chunk)
        for row in connection.execute(
            f"SELECT {PARAGRAPH_SELECT_COLUMNS}, p.rowid AS paragraph_rowid "
            f"FROM paragraphs p WHERE p.rowid IN ({placeholders})",
            chunk,
        ):
            found[(row["paragraph_rowid"], row["paragraph_id"])] = row
    return [found[key] for key in keys if key in found]
