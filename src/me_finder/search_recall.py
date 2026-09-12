"""Candidate recall: run the accuracy-stage cascade against one library.

Recall owns where candidates come from (FTS trigram MATCH plus ``instr``
verification on SQLite, or the in-memory scans on the legacy JSON backend).
It never orders the final list and never formats results — scoring lives in
:mod:`search_scoring`, assembly in :mod:`search_assembly`.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable, Dict, List, Optional, Set, Tuple

from .database import (
    PARAGRAPH_SELECT_COLUMNS,
    paragraph_from_database_row,
)
from .normalization import (
    compact_text,
    normalize_text,
    normalize_with_spans,
    punctuationless_text,
)
from .search_contract import (
    FUZZY_RESCORE_LIMIT,
    MAX_FTS_QUERY_TRIGRAMS,
    SQL_CANDIDATE_FLOOR,
    SQL_CANDIDATE_MULTIPLIER,
)
from .search_scoring import best_window_ratio

Scope = Optional[frozenset]


class CandidateRecall:
    """Recall candidates for one query from a SQLite or in-memory index."""

    def __init__(
        self,
        *,
        db_provider: Callable[[], object],
        backend: str,
        paragraphs: List[Dict[str, object]],
        ngram_index: Dict[str, List[int]],
        ensure_fts: Callable[[], bool],
    ) -> None:
        self._db_provider = db_provider
        self.backend = backend
        self.paragraphs = paragraphs
        self.ngram_index = ngram_index
        self._ensure_fts = ensure_fts

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

    @staticmethod
    def _is_unscoped(
        source_type: str, source_file_id: Optional[str], scope: Scope
    ) -> bool:
        return source_type == "all" and not source_file_id and scope is None

    def _instr_eligibility_clause(
        self, source_type: str, source_file_id: Optional[str], scope: Scope
    ) -> str:
        """Eligibility predicate for the non-FTS ``instr`` substring scans.

        Short queries (< 3 chars) have no trigram MATCH, so recall falls back to
        an ``instr`` scan ordered ``BY p.rowid`` with ``LIMIT budget+1``. Left to
        its own devices SQLite drives that scan through
        ``idx_paragraphs_searchable(eligible_for_search, source_type)``; because
        that index is not rowid-ordered it must funnel every matching row into a
        temp B-tree to satisfy the ORDER BY, i.e. scan *all* eligible paragraphs
        even though only the first ``budget`` are kept. On a whole-library search
        (no source filter) the leading index column is non-selective — nearly
        every paragraph is eligible — so we suppress the index with a unary
        ``+``. SQLite then walks the table in rowid order, which satisfies the
        ORDER BY for free and lets ``LIMIT budget+1`` stop as soon as enough
        matches are found: for a high-frequency short query like "社会" this
        turns a full-table scan into an early exit. The returned rows are
        identical (the same lowest-rowid matches, same order) — only the access
        path changes. A *scoped* search keeps the index, where ``source_type`` /
        ``source_file_id`` is selective and a full rowid scan would be slower.
        """

        if self._is_unscoped(source_type, source_file_id, scope):
            return "+p.eligible_for_search = 1"
        return "p.eligible_for_search = 1"

    def sql_source_filter(
        self,
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
        alias: str = "",
    ) -> Tuple[str, List[object]]:
        prefix = f"{alias}." if alias else ""
        clauses: List[str] = []
        args: List[object] = []
        if source_type == "epub":
            clauses.append(f"{prefix}source_type = 'word'")
            clauses.append(
                f"json_extract({prefix}payload_json, '$.source_format') = 'epub'"
            )
        elif source_type == "word":
            clauses.append(f"{prefix}source_type = 'word'")
            clauses.append(
                f"COALESCE(json_extract({prefix}payload_json, '$.source_format'), 'word') <> 'epub'"
            )
        elif source_type != "all":
            clauses.append(f"{prefix}source_type = ?")
            args.append(source_type)
        if scope is not None:
            # Explicit set scope (DocumentGroup members). An empty set matches
            # nothing; it must never fall through to an unscoped whole-library search.
            if not scope:
                clauses.append("1 = 0")
            else:
                ordered = list(scope)
                placeholders = ", ".join("?" for _ in ordered)
                clauses.append(f"{prefix}source_file_id IN ({placeholders})")
                args.extend(ordered)
        elif source_file_id:
            clauses.append(f"{prefix}source_file_id = ?")
            args.append(source_file_id)
        return (" AND " + " AND ".join(clauses), args) if clauses else ("", args)

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
            sql = (
                f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p "
                "WHERE "
                + self._instr_eligibility_clause(source_type, source_file_id, scope)
                + source_clause
                + " AND (instr(p.text_raw, ?) > 0 OR instr(p.normalized_text, ?) > 0) "
                "ORDER BY p.rowid"
            )
            args = [*source_args, query, q_norm]
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
            sql = (
                f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p "
                "WHERE "
                + self._instr_eligibility_clause(source_type, source_file_id, scope)
                + source_clause
                + f" AND instr(p.{column}, ?) > 0 ORDER BY p.rowid"
            )
            args = [*source_args, query]
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
        source_clause, source_args = self.sql_source_filter(source_type, source_file_id, scope, "p")
        fts_query = self.fts_match_expression(q_plain, "OR")
        if fts_query:
            rows = self.db().execute(
                f"SELECT {PARAGRAPH_SELECT_COLUMNS} "
                "FROM paragraphs_fts JOIN paragraphs p "
                "ON p.rowid = paragraphs_fts.rowid "
                "WHERE paragraphs_fts MATCH ? AND p.eligible_for_search = 1"
                + source_clause
                + " ORDER BY bm25(paragraphs_fts) LIMIT 701",
                [fts_query, *source_args],
            ).fetchall()
            truncated = len(rows) >= 701
            query_grams = self._ngrams_set(q_plain)
            prefiltered: List[Tuple[int, Dict[str, object], str]] = []
            for row in rows:
                paragraph = paragraph_from_database_row(row)
                plain = str(paragraph.get("plain_text") or "")
                overlap = len(query_grams.intersection(self._ngrams_set(plain)))
                if overlap:
                    prefiltered.append((overlap, paragraph, plain))
            prefiltered.sort(key=lambda item: item[0], reverse=True)
            if len(prefiltered) > FUZZY_RESCORE_LIMIT:
                truncated = True
            for _overlap, paragraph, plain in prefiltered[:FUZZY_RESCORE_LIMIT]:
                score = self._score_fuzzy_window(q_plain, paragraph, plain)
                if score is None:
                    continue
                self._add_candidate(paragraph, "ngram_fuzzy", score[0], score[1], score[2], candidates)
                if candidate_budget is not None and len(candidates) >= candidate_budget:
                    truncated = True
                    break
            return truncated

        rows = self.db().execute(
            f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p "
            "WHERE p.eligible_for_search = 1" + source_clause,
            source_args,
        )
        query_grams = self._ngrams_set(q_plain)
        ranked: List[Tuple[int, str, Dict[str, object]]] = []
        for row in rows:
            plain = str(row["plain_text"] or "")
            overlap = len(query_grams.intersection(self._ngrams_set(plain)))
            if overlap:
                ranked.append(
                    (
                        overlap,
                        str(row["paragraph_id"]),
                        paragraph_from_database_row(row),
                    )
                )
        ranked.sort(key=lambda item: item[0], reverse=True)
        for _, _, paragraph in ranked[:FUZZY_RESCORE_LIMIT]:
            plain = str(paragraph.get("plain_text") or "")
            ratio = self._score_fuzzy_window(q_plain, paragraph, plain)
            if ratio is None:
                continue
            self._add_candidate(paragraph, "ngram_fuzzy", ratio[0], ratio[1], ratio[2], candidates)
        return len(ranked) > FUZZY_RESCORE_LIMIT

    def _score_fuzzy_window(
        self, q_plain: str, paragraph: Dict[str, object], plain: str
    ) -> Optional[Tuple[float, int, int]]:
        """Map the best fuzzy window onto raw-text offsets, or reject it."""

        ratio, start, end = best_window_ratio(q_plain, plain)
        if ratio < 0.58:
            return None
        raw = str(paragraph.get("text_raw") or "")
        _, spans = self._normalization_spans(paragraph, raw, "plain")
        if not spans:
            return None
        start = max(0, min(start, len(spans) - 1))
        end = max(start, min(end, len(spans) - 1))
        return min(0.9, max(0.58, ratio)), spans[start][0], spans[end][1]

    # ------------------------------------------------------------------
    # In-memory recall passes (legacy JSON backend)
    # ------------------------------------------------------------------

    def _exact_pass(
        self,
        query: str,
        q_norm: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
    ) -> None:
        for paragraph in self.paragraphs:
            if not self._source_allowed(paragraph, source_type, source_file_id, scope):
                continue
            raw = str(paragraph.get("text_raw") or "")
            normalized = str(paragraph.get("normalized_text") or "")
            raw_pos = raw.find(query)
            if raw_pos >= 0:
                self._add_candidate(paragraph, "exact", 1.0, raw_pos, raw_pos + len(query), candidates)
                continue
            norm_pos = normalized.find(q_norm)
            if norm_pos >= 0:
                span = self._mapped_span(paragraph, raw, q_norm, "normalized")
                self._add_candidate(paragraph, "normalized_exact", 0.985, span[0], span[1], candidates)

    def _mapped_substring_pass(
        self,
        query: str,
        mode: str,
        match_type: str,
        score: float,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
    ) -> None:
        if not query:
            return
        field = "compact_text" if mode == "compact" else "plain_text"
        for paragraph in self.paragraphs:
            if not self._source_allowed(paragraph, source_type, source_file_id, scope):
                continue
            haystack = str(paragraph.get(field) or "")
            pos = haystack.find(query)
            if pos < 0:
                continue
            raw = str(paragraph.get("text_raw") or "")
            _, spans = self._normalization_spans(paragraph, raw, mode)
            if pos >= len(spans):
                continue
            end_pos = min(pos + len(query) - 1, len(spans) - 1)
            start_raw = spans[pos][0]
            end_raw = spans[end_pos][1]
            self._add_candidate(paragraph, match_type, score, start_raw, end_raw, candidates)

    def _fuzzy_pass(
        self,
        q_plain: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
        scope: Scope,
    ) -> None:
        if not q_plain:
            return
        grams = self._ngrams(q_plain)
        counts: Counter[int] = Counter()
        for gram in grams:
            counts.update(self.ngram_index.get(gram, []))
        if not counts:
            search_space = list(range(min(len(self.paragraphs), 800)))
        else:
            search_space = [idx for idx, _ in counts.most_common(700)]
        for idx in search_space:
            paragraph = self.paragraphs[idx]
            if not self._source_allowed(paragraph, source_type, source_file_id, scope):
                continue
            plain = str(paragraph.get("plain_text") or "")
            score = self._score_fuzzy_window(q_plain, paragraph, plain)
            if score is None:
                continue
            self._add_candidate(paragraph, "ngram_fuzzy", score[0], score[1], score[2], candidates)

    # ------------------------------------------------------------------
    # Relevance retrieval (passage search; never claims a verbatim hit)
    # ------------------------------------------------------------------

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
                source_clause, source_args = self.sql_source_filter(source_type, source_file_id, scope, "p")
                rows = self.db().execute(
                    f"SELECT {PARAGRAPH_SELECT_COLUMNS}, "
                    "bm25(paragraphs_fts) AS bm25_score "
                    "FROM paragraphs_fts JOIN paragraphs p "
                    "ON p.rowid = paragraphs_fts.rowid "
                    "WHERE paragraphs_fts MATCH ? AND p.eligible_for_search = 1"
                    + source_clause
                    + " ORDER BY bm25(paragraphs_fts) LIMIT ?",
                    [fts_query, *source_args, budget + 1],
                ).fetchall()
                truncated = len(rows) > budget
                ranked = [
                    (paragraph_from_database_row(row), float(row["bm25_score"]))
                    for row in rows[:budget]
                ]
                return ranked, truncated, "bm25"
        return self._scan_passages(q_plain, source_type, source_file_id, scope, budget)

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
