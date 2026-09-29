"""Relevance retrieval for ``search_passages`` (never claims a verbatim hit).

Mixed into :class:`search_recall.CandidateRecall`, which supplies the database
handle, the FTS expression builder, the gram helpers and the short-gram
readiness gate.  The SQL itself lives in :mod:`persistence.passage_reads`; this
module only ranks the rows it gets back.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .database import paragraph_from_database_row
from .normalization import punctuationless_text
from .persistence.passage_reads import (
    read_bm25_passage_rows,
    read_gram_passage_rows,
)
from .search_contract import SQL_CANDIDATE_FLOOR, SQL_CANDIDATE_MULTIPLIER

Scope = Optional[frozenset]


class PassageRetrieval:
    """BM25 retrieval with gram-overlap fallbacks for SQLite and memory."""

    def retrieve_passages(
        self,
        query: str,
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        limit: int,
    ) -> Tuple[List[Tuple[Dict[str, object], float]], bool, str]:
        """Relevance retrieval seam: return ``(ranked, truncated, method)``.

        ``ranked`` is a list of ``(paragraph, raw_relevance)`` ordered
        most-relevant first. ``raw_relevance`` follows a "lower is more relevant"
        convention (both SQLite ``bm25`` and the trigram fallback do); callers must
        rely only on the ordering, never on the scale. A future embedding backend
        replaces this method alone.
        """

        q_plain = punctuationless_text(query)
        budget = max(SQL_CANDIDATE_FLOOR, limit * SQL_CANDIDATE_MULTIPLIER)
        if self.backend == "sqlite" and self.db() is not None:
            fts_query = self.fts_match_expression(q_plain, "OR")
            if fts_query:
                rows = read_bm25_passage_rows(
                    self.db(), fts_query, source_type, source_file_id, scope, budget + 1
                )
                truncated = len(rows) > budget
                ranked = [
                    (paragraph_from_database_row(row), float(row["bm25_score"]))
                    for row in rows[:budget]
                ]
                return ranked, truncated, "bm25"
            return self._sql_scan_passages(q_plain, source_type, source_file_id, scope, budget)
        return self._scan_passages(q_plain, source_type, source_file_id, scope, budget)

    def _sql_scan_passages(
        self,
        q_plain: str,
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        budget: int,
    ) -> Tuple[List[Tuple[Dict[str, object], float]], bool, str]:
        """Gram-overlap ranking read from SQLite when FTS cannot serve the query.

        Queries shorter than a trigram (or a library without FTS) land here; the
        SQLite backend never loads the in-memory paragraph list, so the memory
        scan would silently find nothing.
        """

        query_grams = self._ngrams_set(q_plain)
        if not query_grams:
            return [], False, "trigram"
        rows = read_gram_passage_rows(
            self.db(),
            sorted(query_grams),
            source_type,
            source_file_id,
            scope,
            self._short_gram_ready(),
        )
        scored: List[Tuple[tuple, Dict[str, object], int]] = []
        for row in rows:
            plain = str(row["plain_text"] or "")
            overlap = len(query_grams.intersection(self._ngrams_set(plain)))
            if overlap:
                # Ties follow reading order, never the query plan's row order.
                key = (
                    -overlap,
                    str(row["source_file_id"] or ""),
                    int(row["paragraph_index"] or 0),
                    str(row["paragraph_id"]),
                )
                scored.append((key, paragraph_from_database_row(row), overlap))
        scored.sort(key=lambda item: item[0])
        truncated = len(scored) > budget
        # Negate overlap to keep the "lower is more relevant" raw-score convention.
        ranked = [(paragraph, float(-overlap)) for _key, paragraph, overlap in scored[:budget]]
        return ranked, truncated, "trigram"

    def _scan_passages(
        self,
        q_plain: str,
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        budget: int,
    ) -> Tuple[List[Tuple[Dict[str, object], float]], bool, str]:
        """Fallback relevance ranking by trigram overlap (no FTS / JSON backend)."""

        if not q_plain or not self.paragraphs:
            return [], False, "trigram"
        query_grams = self._ngrams_set(q_plain)
        if not query_grams:
            return [], False, "trigram"
        scored: List[Tuple[int, Dict[str, object]]] = []
        for paragraph in self.paragraphs:
            if not self._source_allowed(paragraph, source_type, source_file_id, scope):
                continue
            plain = str(paragraph.get("plain_text") or "")
            overlap = len(query_grams.intersection(self._ngrams_set(plain)))
            if overlap:
                scored.append((overlap, paragraph))
        scored.sort(key=lambda item: item[0], reverse=True)
        truncated = len(scored) > budget
        # Negate overlap to keep the "lower is more relevant" raw-score convention.
        ranked = [
            (paragraph, float(-overlap)) for overlap, paragraph in scored[:budget]
        ]
        return ranked, truncated, "trigram"
