"""Read-only SQLite queries behind search recall and adjacent context.

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


def _limit_sql(sql: str, candidate_budget: Optional[int]) -> str:
    if candidate_budget is None:
        return sql
    return sql + f" LIMIT {max(1, int(candidate_budget)) + 1}"


def _instr_eligibility_clause(
    source_type: str, source_file_id: Optional[str], scope: Scope
) -> str:
    """Keep unscoped short scans in rowid order without a temporary sort.

    A unary + suppresses the non-selective eligibility index; scoped reads
    retain it. See reports/performance-short-query-recall-2026-09-12.md.
    """
    if source_type == "all" and not source_file_id and scope is None:
        return "+p.eligible_for_search = 1"
    return "p.eligible_for_search = 1"


def read_exact_candidate_rows(
    connection: sqlite3.Connection,
    query: str,
    q_norm: str,
    fts_query: Optional[str],
    source_type: str,
    source_file_id: Optional[str],
    scope: Scope,
    candidate_budget: Optional[int],
    prefilter_ready: bool,
) -> sqlite3.Cursor:
    """Stream exact/raw-normalized candidates in rowid order, with one sentinel."""
    source_clause, source_args = source_filter_clause(source_type, source_file_id, scope, "p")
    if fts_query:
        sql = (
            f"SELECT {PARAGRAPH_SELECT_COLUMNS} "
            "FROM paragraphs_fts JOIN paragraphs p "
            "ON p.rowid = paragraphs_fts.rowid "
            "WHERE paragraphs_fts MATCH ? AND p.eligible_for_search = 1"
            + source_clause
            + " AND (instr(p.text_raw, ?) > 0 OR instr(p.normalized_text, ?) > 0) "
            "ORDER BY p.rowid"
        )
        args: List[object] = [fts_query, *source_args, query, q_norm]
    else:
        gram_clause, gram_args = short_gram_prefilter([query, q_norm]) if prefilter_ready else ("", [])
        sql = (
            f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p WHERE "
            + _instr_eligibility_clause(source_type, source_file_id, scope)
            + source_clause
            + gram_clause
            + " AND (instr(p.text_raw, ?) > 0 OR instr(p.normalized_text, ?) > 0) "
            "ORDER BY p.rowid"
        )
        args = [*source_args, *gram_args, query, q_norm]
    return connection.execute(_limit_sql(sql, candidate_budget), args)


def read_substring_candidate_rows(
    connection: sqlite3.Connection,
    query: str,
    column: str,
    fts_query: Optional[str],
    source_type: str,
    source_file_id: Optional[str],
    scope: Scope,
    candidate_budget: Optional[int],
    prefilter_ready: bool,
) -> sqlite3.Cursor:
    """Stream compact/plain candidates; column is selected by the recall stage."""
    source_clause, source_args = source_filter_clause(source_type, source_file_id, scope, "p")
    if fts_query:
        sql = (
            f"SELECT {PARAGRAPH_SELECT_COLUMNS} "
            "FROM paragraphs_fts JOIN paragraphs p "
            "ON p.rowid = paragraphs_fts.rowid "
            "WHERE paragraphs_fts MATCH ? AND p.eligible_for_search = 1"
            + source_clause
            + f" AND instr(p.{column}, ?) > 0 ORDER BY p.rowid"
        )
        args: List[object] = [fts_query, *source_args, query]
    else:
        gram_clause, gram_args = short_gram_prefilter([query]) if prefilter_ready else ("", [])
        sql = (
            f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p WHERE "
            + _instr_eligibility_clause(source_type, source_file_id, scope)
            + source_clause
            + gram_clause
            + f" AND instr(p.{column}, ?) > 0 ORDER BY p.rowid"
        )
        args = [*source_args, *gram_args, query]
    return connection.execute(_limit_sql(sql, candidate_budget), args)


def read_search_context_rows(
    connection: sqlite3.Connection,
    source_file_id: str,
    volume_id: Optional[str],
    paragraph_index: int,
    before: bool,
    *,
    is_pdf: bool,
) -> sqlite3.Cursor:
    """Stream PDF neighbors or the nearest nonempty text-document paragraph.

    PDF page validation and skipping cross-page windows stay with the caller;
    do not fetch all neighbors into memory.
    """
    source_column = "source_file_id" if source_file_id else "volume_id"
    source_value = source_file_id if source_file_id else volume_id
    source_predicate = (
        f"{source_column} IS NULL" if source_value is None else f"{source_column} = ?"
    )
    source_args = [] if source_value is None else [source_value]
    if before:
        predicate = "paragraph_index < ?"
        order = "DESC"
    else:
        predicate = "paragraph_index > ?"
        order = "ASC"
    if is_pdf:
        position_index = (
            "idx_paragraphs_source_position"
            if source_file_id
            else "idx_paragraphs_volume_position"
        )
        sql = (
            "/* pdf_context */ "
            "SELECT p.paragraph_id, p.text_raw, p.pdf_page_start_index, "
            "p.pdf_page_end_index, p.payload_json "
            f"FROM paragraphs AS p INDEXED BY {position_index} "
            f"WHERE p.{source_predicate} AND p.source_type = 'pdf' "
            f"AND p.{predicate} ORDER BY p.paragraph_index {order}"
        )
    else:
        sql = (
            "SELECT paragraph_id, text_raw FROM paragraphs "
            f"WHERE {source_predicate} AND source_type != 'pdf' "
            f"AND trim(text_raw) != '' AND {predicate} "
            f"ORDER BY paragraph_index {order} LIMIT 1"
        )
    return connection.execute(sql, [*source_args, paragraph_index])
