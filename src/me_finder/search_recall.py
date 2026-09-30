"""Candidate recall: run the accuracy-stage cascade against one library.

Recall owns where candidates come from (FTS trigram MATCH plus ``instr``
verification on SQLite, or the in-memory scans on the legacy JSON backend).
It never orders the final list and never formats results — scoring lives in
:mod:`search_scoring`, assembly in :mod:`search_assembly`.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

from .database import (
    PARAGRAPH_SELECT_COLUMNS,
    paragraph_from_database_row,
)
from .normalization import (
    compact_text,
    normalize_text,
    normalize_with_spans,
    plain_spans,
    punctuationless_text,
)
from .persistence.paragraph_scope import source_filter_clause
from .persistence.passage_reads import (
    RANK_COLUMNS, read_bm25_passage_rows, read_gram_passage_rows, read_paragraph_rows,
)
from .persistence.short_gram_index import short_gram_prefilter
from .search_contract import (
    FUZZY_RESCORE_LIMIT,
    MAX_FTS_QUERY_TRIGRAMS,
    SQL_CANDIDATE_FLOOR,
    SQL_CANDIDATE_MULTIPLIER,
    fuzzy_needs_bigram_scan,
)
from .search_recall_memory import InMemoryRecallPasses
from .search_recall_passages import PassageRetrieval, gram_overlap
from .search_scoring import best_window_ratio

Scope = Optional[frozenset]


class CandidateRecall(PassageRetrieval, InMemoryRecallPasses):
    """Recall candidates for one query from a SQLite or in-memory index."""

    def __init__(
        self,
        *,
        db_provider: Callable[[], object],
        backend: str,
        paragraphs: List[Dict[str, object]],
        ngram_index: Dict[str, List[int]],
        ensure_fts: Callable[[], bool],
        short_gram_ready: Callable[[], bool] = lambda: False,
    ) -> None:
        self._db_provider = db_provider
        self.backend = backend
        self.paragraphs = paragraphs
        self.ngram_index = ngram_index
        self._ensure_fts = ensure_fts
        self._short_gram_ready = short_gram_ready

    def db(self) -> object:
        """Current backend connection; FTS reinstallation may reopen it."""

        return self._db_provider()

    def candidate_budget(self, limit: Optional[int]) -> Optional[int]:
        if limit is None:
            return None
        return max(SQL_CANDIDATE_FLOOR, limit * SQL_CANDIDATE_MULTIPLIER)

    def collect(
        self,
        query: str,
        mode: str,
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        candidate_budget: Optional[int],
    ) -> Tuple[Dict[str, Dict[str, object]], bool]:
        """Run the exact -> compact -> punctuation -> fuzzy cascade."""

        q_norm = normalize_text(query)
        q_compact = compact_text(query)
        q_plain = punctuationless_text(query)
        candidates: Dict[str, Dict[str, object]] = {}
        truncated = False
        if mode in {"auto", "exact"}:
            if self.backend == "sqlite":
                truncated = self._sql_exact_pass(
                    query, q_norm, q_plain, candidates, source_type, source_file_id, scope, candidate_budget,
                )
            else:
                self._exact_pass(query, q_norm, candidates, source_type, source_file_id, scope)
        if mode in {"auto", "compact"} and (mode != "auto" or not candidates):
            if self.backend == "sqlite":
                truncated = self._sql_mapped_substring_pass(
                    q_compact, q_plain, "compact_text", "space_insensitive", 0.96,
                    candidates, source_type, source_file_id, scope, candidate_budget,
                ) or truncated
            else:
                self._mapped_substring_pass(q_compact, "compact", "space_insensitive", 0.96, candidates, source_type, source_file_id, scope)
        if mode in {"auto", "punctuation"} and (mode != "auto" or not candidates):
            if self.backend == "sqlite":
                truncated = self._sql_mapped_substring_pass(
                    q_plain, q_plain, "plain_text", "punctuation_insensitive", 0.92,
                    candidates, source_type, source_file_id, scope, candidate_budget,
                ) or truncated
            else:
                self._mapped_substring_pass(q_plain, "plain", "punctuation_insensitive", 0.92, candidates, source_type, source_file_id, scope)
        if mode in {"auto", "fuzzy"} and (mode != "auto" or not candidates):
            if self.backend == "sqlite":
                truncated = self._sql_fuzzy_pass(
                    q_plain, candidates, source_type, source_file_id, scope, candidate_budget,
                ) or truncated
            else:
                self._fuzzy_pass(q_plain, candidates, source_type, source_file_id, scope)
        return candidates, truncated

    # ------------------------------------------------------------------
    # SQLite recall passes
    # ------------------------------------------------------------------

    def fts_match_expression(self, text: str, operator: str) -> Optional[str]:
        """Build a bounded trigram query for the detail-free FTS table."""

        if len(text) < 3 or operator not in {"AND", "OR"}:
            return None
        if not self._ensure_fts():
            return None
        grams = list(dict.fromkeys(text[index : index + 3] for index in range(len(text) - 2)))
        if len(grams) > MAX_FTS_QUERY_TRIGRAMS:
            last = len(grams) - 1
            positions = {
                round(index * last / (MAX_FTS_QUERY_TRIGRAMS - 1))
                for index in range(MAX_FTS_QUERY_TRIGRAMS)
            }
            grams = [grams[index] for index in sorted(positions)]
        quoted = ['"' + gram.replace('"', '""') + '"' for gram in grams]
        return f" {operator} ".join(quoted)

    @staticmethod
    def _limit_sql(sql: str, candidate_budget: Optional[int]) -> str:
        if candidate_budget is None:
            return sql
        return sql + f" LIMIT {max(1, int(candidate_budget)) + 1}"

    def _instr_eligibility_clause(
        self, source_type: str, source_file_id: Optional[str], scope: Scope
    ) -> str:
        """Eligibility predicate for the non-FTS ``instr`` substring scans.
        On a whole-library short-query scan a unary ``+`` suppresses the
        non-selective ``idx_paragraphs_searchable`` so SQLite walks the table in
        rowid order and ``LIMIT budget+1`` stops early instead of temp-B-tree
        sorting every match — same rows, faster path; a scoped search keeps the
        selective index. See reports/performance-short-query-recall-2026-09-12.md.
        """

        if source_type == "all" and not source_file_id and scope is None:
            return "+p.eligible_for_search = 1"
        return "p.eligible_for_search = 1"

    def _short_gram_clause(self, strings: List[str]) -> Tuple[str, List[object]]:
        """Superset prefilter for the non-FTS scans; empty until the index is ready."""

        return short_gram_prefilter(strings) if self._short_gram_ready() else ("", [])

    def sql_source_filter(
        self,
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        alias: str = "",
    ) -> Tuple[str, List[object]]:
        """Scope predicate for every SQLite pass; the SQL lives in persistence."""

        return source_filter_clause(source_type, source_file_id, scope, alias)

    def _sql_exact_pass(
        self,
        query: str,
        q_norm: str,
        q_plain: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        candidate_budget: Optional[int],
    ) -> bool:
        if self.db() is None:
            return False
        fts_query = self.fts_match_expression(q_plain, "AND")
        source_clause, source_args = self.sql_source_filter(source_type, source_file_id, scope, "p")
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
            gram_clause, gram_args = self._short_gram_clause([query, q_norm])
            sql = (
                f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p WHERE "
                + self._instr_eligibility_clause(source_type, source_file_id, scope)
                + source_clause
                + gram_clause
                + " AND (instr(p.text_raw, ?) > 0 OR instr(p.normalized_text, ?) > 0) "
                "ORDER BY p.rowid"
            )
            args = [*source_args, *gram_args, query, q_norm]
        processed = 0
        truncated = False
        sql = self._limit_sql(sql, candidate_budget)
        for row in self.db().execute(sql, args):
            if candidate_budget is not None and processed >= candidate_budget:
                truncated = True
                break
            processed += 1
            paragraph = paragraph_from_database_row(row)
            raw = str(row["text_raw"] or "")
            raw_pos = raw.find(query)
            if raw_pos >= 0:
                self._add_candidate(paragraph, "exact", 1.0, raw_pos, raw_pos + len(query), candidates)
                continue
            normalized = str(row["normalized_text"] or "")
            norm_pos = normalized.find(q_norm)
            if norm_pos >= 0:
                span = self._mapped_span(paragraph, raw, q_norm, "normalized")
                self._add_candidate(paragraph, "normalized_exact", 0.985, span[0], span[1], candidates)
        return truncated

    def _sql_mapped_substring_pass(
        self,
        query: str,
        q_plain: str,
        column: str,
        match_type: str,
        score: float,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        candidate_budget: Optional[int],
    ) -> bool:
        if self.db() is None or not query or column not in {"compact_text", "plain_text"}:
            return False
        fts_query = self.fts_match_expression(q_plain, "AND")
        source_clause, source_args = self.sql_source_filter(source_type, source_file_id, scope, "p")
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
            gram_clause, gram_args = self._short_gram_clause([query])
            sql = (
                f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p WHERE "
                + self._instr_eligibility_clause(source_type, source_file_id, scope)
                + source_clause
                + gram_clause
                + f" AND instr(p.{column}, ?) > 0 ORDER BY p.rowid"
            )
            args = [*source_args, *gram_args, query]
        processed = 0
        truncated = False
        sql = self._limit_sql(sql, candidate_budget)
        for row in self.db().execute(sql, args):
            if candidate_budget is not None and processed >= candidate_budget:
                truncated = True
                break
            processed += 1
            paragraph = paragraph_from_database_row(row)
            haystack = str(row[column] or "")
            pos = haystack.find(query)
            if pos < 0:
                continue
            raw = str(row["text_raw"] or "")
            _, spans = self._normalization_spans(
                paragraph,
                raw,
                "compact" if column == "compact_text" else "plain",
            )
            if pos >= len(spans):
                continue
            end_pos = min(pos + len(query) - 1, len(spans) - 1)
            self._add_candidate(
                paragraph,
                match_type,
                score,
                spans[pos][0],
                spans[end_pos][1],
                candidates,
            )
        return truncated

    def _sql_fuzzy_pass(
        self,
        q_plain: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        candidate_budget: Optional[int],
    ) -> bool:
        if self.db() is None or not q_plain:
            return False
        before, truncated = len(candidates), False
        fts_query = self.fts_match_expression(q_plain, "OR")
        if fts_query:
            rows = read_bm25_passage_rows(self.db(), fts_query, source_type, source_file_id, scope, 701)
            truncated = len(rows) >= 701
            query_grams = self._ngrams_set(q_plain)
            prefiltered: List[Tuple[int, object]] = []
            for row in rows:
                overlap = gram_overlap(query_grams, str(row["plain_text"] or ""))
                if overlap:
                    prefiltered.append((overlap, row))
            prefiltered.sort(key=lambda item: item[0], reverse=True)
            if len(prefiltered) > FUZZY_RESCORE_LIMIT:
                truncated = True
            for _overlap, row in prefiltered[:FUZZY_RESCORE_LIMIT]:
                paragraph = paragraph_from_database_row(row)
                plain = str(paragraph.get("plain_text") or "")
                score = self._score_fuzzy_window(q_plain, paragraph, plain)
                if score is None:
                    continue
                self._add_candidate(paragraph, "ngram_fuzzy", score[0], score[1], score[2], candidates)
                if candidate_budget is not None and len(candidates) >= candidate_budget:
                    truncated = True
                    break
            # Typos breaking every trigram leave FTS nothing (or only weak
            # candidates) to score: add the bigram scan where that can happen.
            if not fuzzy_needs_bigram_scan(len(q_plain), len(candidates) > before):
                return truncated

        query_grams = self._ngrams_set(q_plain)
        # Paragraphs sharing no query gram score zero overlap and are dropped
        # below anyway, so the prefilter never changes which rows can rank.
        rows = read_gram_passage_rows(
            self.db(), sorted(query_grams), source_type, source_file_id, scope,
            self._short_gram_ready(), RANK_COLUMNS,
        )
        ranked: List[Tuple[tuple, object]] = []
        for row in rows:
            overlap = gram_overlap(query_grams, str(row["plain_text"] or ""))
            if overlap:
                # Ties before the rescore cut follow reading order, never the
                # query plan's row order (which the prefilter or ANALYZE shift).
                position = (str(row["source_file_id"] or ""), int(row["paragraph_index"] or 0))
                key = (-overlap, *position, str(row["paragraph_id"]))
                ranked.append((key, (row["paragraph_rowid"], row["paragraph_id"])))
        ranked.sort(key=lambda item: item[0])  # keys end in the unique paragraph id
        kept = [row_key for _, row_key in ranked[:FUZZY_RESCORE_LIMIT]]
        for row in read_paragraph_rows(self.db(), kept):
            paragraph = paragraph_from_database_row(row)
            plain = str(paragraph.get("plain_text") or "")
            ratio = self._score_fuzzy_window(q_plain, paragraph, plain)
            if ratio is None:
                continue
            self._add_candidate(paragraph, "ngram_fuzzy", ratio[0], ratio[1], ratio[2], candidates)
        return truncated or len(ranked) > FUZZY_RESCORE_LIMIT

    def _score_fuzzy_window(
        self, q_plain: str, paragraph: Dict[str, object], plain: str
    ) -> Optional[Tuple[float, int, int]]:
        """Map the best fuzzy window onto raw-text offsets, or reject it."""

        ratio, start, end = best_window_ratio(q_plain, plain)
        if ratio < 0.58:
            return None
        spans = plain_spans(str(paragraph.get("text_raw") or ""))
        if not spans:
            return None
        start = max(0, min(start, len(spans) - 1))
        end = max(start, min(end, len(spans) - 1))
        return min(0.9, max(0.58, ratio)), spans[start][0], spans[end][1]

    # ------------------------------------------------------------------
    # Shared candidate bookkeeping
    # ------------------------------------------------------------------

    def _add_candidate(
        self,
        paragraph: Dict[str, object],
        match_type: str,
        score: float,
        start: int,
        end: int,
        candidates: Dict[str, Dict[str, object]],
    ) -> None:
        paragraph_id = str(paragraph["paragraph_id"])
        start = max(0, min(start, len(str(paragraph.get("text_raw") or ""))))
        end = max(start, min(end, len(str(paragraph.get("text_raw") or ""))))
        existing = candidates.get(paragraph_id)
        if existing is not None and float(existing["match_score"]) >= score:
            return
        candidates[paragraph_id] = {
            "paragraph_id": paragraph_id,
            "paragraph": paragraph,
            "match_type": match_type,
            "match_score": float(score),
            "match_start": start,
            "match_end": end,
        }

    def _source_allowed(
        self,
        paragraph: Dict[str, object],
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
    ) -> bool:
        paragraph_type = str(paragraph.get("source_type") or "word")
        paragraph_format = str(paragraph.get("source_format") or "").casefold()
        if source_type == "epub":
            if paragraph_type != "word" or paragraph_format != "epub":
                return False
        elif source_type == "word":
            if paragraph_type != "word" or paragraph_format == "epub":
                return False
        elif source_type != "all" and paragraph_type != source_type:
            return False
        if scope is not None:
            # Explicit set scope; an empty set matches nothing.
            return str(paragraph.get("source_file_id") or "") in scope
        return not source_file_id or str(paragraph.get("source_file_id") or "") == source_file_id

    def _normalization_spans(
        self, paragraph: Dict[str, object], raw: str, mode: str
    ) -> Tuple[str, List[Tuple[int, int]]]:
        return normalize_with_spans(
            raw,
            mode,
            pdf_hyphenation=(
                mode == "normalized"
                and str(paragraph.get("source_type") or "word") == "pdf"
            ),
        )

    def _mapped_span(
        self,
        paragraph: Dict[str, object],
        raw: str,
        query: str,
        mode: str,
    ) -> Tuple[int, int]:
        normalized, spans = self._normalization_spans(paragraph, raw, mode)
        pos = normalized.find(query)
        if pos < 0 or not spans:
            return 0, min(len(raw), 80)
        end_pos = min(pos + len(query) - 1, len(spans) - 1)
        return spans[pos][0], spans[end_pos][1]

    @staticmethod
    def _ngrams_set(text: str, n: int = 2) -> Set[str]:
        if len(text) <= n:
            return {text} if text else set()
        return {text[index : index + n] for index in range(len(text) - n + 1)}

    @staticmethod
    def _ngrams(text: str, n: int = 2) -> List[str]:
        if len(text) <= n:
            return [text] if text else []
        return [text[i : i + n] for i in range(len(text) - n + 1)]
