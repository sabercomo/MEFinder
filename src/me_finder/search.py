"""Local deterministic search engine."""

from __future__ import annotations

import difflib
import html
import json
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .citations import build_citation_formats
from .database import (
    DEFAULT_DATABASE_PATH,
    PARAGRAPH_SELECT_COLUMNS,
    database_has_fts5_search_index,
    ensure_database_search_index,
    load_database_index,
    open_database,
    paragraph_from_database_row,
)
from .indexer import DEFAULT_INDEX_PATH, load_index
from .normalization import (
    compact_text,
    normalize_text,
    normalize_with_map,
    punctuationless_text,
    trim_for_display,
)
from .page_display import build_page_display, resolve_citation_page


SEARCH_MODES = {"auto", "exact", "compact", "punctuation", "fuzzy"}
MAX_FTS_QUERY_TRIGRAMS = 48
SQL_CANDIDATE_FLOOR = 64
SQL_CANDIDATE_MULTIPLIER = 8


class SearchEngine:
    def __init__(self, index_path: Optional[Path] = None) -> None:
        requested_path = Path(index_path) if index_path is not None else None
        if requested_path is None:
            requested_path = DEFAULT_DATABASE_PATH if DEFAULT_DATABASE_PATH.exists() else DEFAULT_INDEX_PATH
        elif requested_path == DEFAULT_INDEX_PATH and DEFAULT_DATABASE_PATH.exists():
            # Keep old callers that pass data/index.json on the SQLite backend.
            requested_path = DEFAULT_DATABASE_PATH
        self.index_path = requested_path
        if not self.index_path.exists():
            raise FileNotFoundError(f"Index not found: {self.index_path}")
        self.backend = "sqlite" if self.index_path.suffix.lower() in {".sqlite", ".sqlite3", ".db"} else "json"
        self.db: Optional[sqlite3.Connection] = None
        self._db_init_lock = threading.RLock()
        self._fts_install_attempted = False
        self._fts_ready = False
        if self.backend == "sqlite":
            self.db = open_database(self.index_path)
            self._fts_ready = database_has_fts5_search_index(self.db)
            self.index = load_database_index(self.index_path)
            self._init_catalog_maps()
            self.paragraphs = []
            self.by_id = {}
            self.by_volume = defaultdict(list)
            self.ngram_index = defaultdict(list)
            return

        self.index = load_index(self.index_path)
        self._init_catalog_maps()
        self.paragraphs: List[Dict[str, object]] = [
            p for p in self.index.get("paragraphs", []) if p.get("eligible_for_search") and p.get("text_raw")
        ]
        self.by_id = {p["paragraph_id"]: p for p in self.paragraphs}
        self.by_volume: Dict[str, List[Dict[str, object]]] = defaultdict(list)
        for paragraph in self.index.get("paragraphs", []):
            if not isinstance(paragraph, dict) or not paragraph.get("text_raw"):
                continue
            self.by_volume[str(paragraph.get("volume_id"))].append(paragraph)
        for plist in self.by_volume.values():
            plist.sort(key=lambda p: int(p.get("paragraph_index", 0)))
        self.ngram_index: Dict[str, List[int]] = defaultdict(list)
        for idx, paragraph in enumerate(self.paragraphs):
            grams = set(self._ngrams(str(paragraph.get("plain_text") or "")))
            for gram in grams:
                self.ngram_index[gram].append(idx)

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    def _init_catalog_maps(self) -> None:
        self.sources_by_id = {
            str(item.get("source_file_id")): item
            for item in self.index.get("source_files", [])
            if isinstance(item, dict) and item.get("source_file_id")
        }
        self.volumes_by_id = {
            str(item.get("volume_id")): item
            for item in self.index.get("volumes", [])
            if isinstance(item, dict) and item.get("volume_id")
        }
        self.works_by_id = {
            str(item.get("work_id")): item
            for item in self.index.get("works", [])
            if isinstance(item, dict) and item.get("work_id")
        }
        self._pdf_page_cache: Dict[Tuple[str, int], Optional[Dict[str, object]]] = {
            (str(item.get("source_file_id")), int(item.get("pdf_page_index"))): item
            for item in self.index.get("pdf_pages", [])
            if isinstance(item, dict)
            and item.get("source_file_id")
            and isinstance(item.get("pdf_page_index"), int)
        }

    def search(
        self,
        query: str,
        mode: str = "auto",
        limit: int | str | None = 10,
        source_type: str = "all",
        source_file_id: Optional[str] = None,
    ) -> Dict[str, object]:
        query = (query or "").strip()
        source_file_id = str(source_file_id or "").strip() or None
        if mode not in SEARCH_MODES:
            mode = "auto"
        if source_type not in {"all", "word", "pdf"}:
            source_type = "all"
        return_all = str(limit or "").strip().lower() in {"all", "0"}
        normalized_limit = None if return_all else max(1, min(int(limit or 10), 200))
        if not query:
            return {"query": query, "mode": mode, "total": 0, "results": []}
        if self.backend == "sqlite":
            return self._search_sql(query, mode, normalized_limit, source_type, source_file_id)
        q_norm = normalize_text(query)
        q_compact = compact_text(query)
        q_plain = punctuationless_text(query)
        candidates: Dict[str, Dict[str, object]] = {}
        if mode in {"auto", "exact"}:
            self._exact_pass(query, q_norm, candidates, source_type, source_file_id)
        if mode in {"auto", "compact"} and (mode != "auto" or not candidates):
            self._mapped_substring_pass(q_compact, "compact", "space_insensitive", 0.96, candidates, source_type, source_file_id)
        if mode in {"auto", "punctuation"} and (mode != "auto" or not candidates):
            self._mapped_substring_pass(q_plain, "plain", "punctuation_insensitive", 0.92, candidates, source_type, source_file_id)
        if mode in {"auto", "fuzzy"} and (mode != "auto" or not candidates):
            self._fuzzy_pass(q_plain, candidates, source_type, source_file_id)
        ranked = sorted(candidates.values(), key=self._rank_key)
        merged = self._merge_candidate_specs(ranked)
        selected = merged if normalized_limit is None else merged[:normalized_limit]
        return {
            "query": query,
            "mode": mode,
            "source_type": source_type,
            "source_file_id": source_file_id,
            "total": len(merged),
            "total_is_exact": True,
            "has_more": normalized_limit is not None and len(merged) > normalized_limit,
            "results": [self._format_candidate(item) for item in selected],
            "return_all": return_all,
            "index_metadata": self.index.get("metadata", {}),
        }

    def _search_sql(
        self,
        query: str,
        mode: str,
        limit: Optional[int],
        source_type: str,
        source_file_id: Optional[str],
    ) -> Dict[str, object]:
        q_norm = normalize_text(query)
        q_compact = compact_text(query)
        q_plain = punctuationless_text(query)
        candidates: Dict[str, Dict[str, object]] = {}
        candidate_budget = (
            None
            if limit is None
            else max(SQL_CANDIDATE_FLOOR, limit * SQL_CANDIDATE_MULTIPLIER)
        )
        truncated = False
        if mode in {"auto", "exact"}:
            truncated = self._sql_exact_pass(
                query,
                q_norm,
                q_plain,
                candidates,
                source_type,
                source_file_id,
                candidate_budget,
            )
        if mode in {"auto", "compact"} and (mode != "auto" or not candidates):
            truncated = self._sql_mapped_substring_pass(
                q_compact,
                q_plain,
                "compact_text",
                "space_insensitive",
                0.96,
                candidates,
                source_type,
                source_file_id,
                candidate_budget,
            )
        if mode in {"auto", "punctuation"} and (mode != "auto" or not candidates):
            truncated = self._sql_mapped_substring_pass(
                q_plain,
                q_plain,
                "plain_text",
                "punctuation_insensitive",
                0.92,
                candidates,
                source_type,
                source_file_id,
                candidate_budget,
            )
        if mode in {"auto", "fuzzy"} and (mode != "auto" or not candidates):
            truncated = self._sql_fuzzy_pass(
                q_plain,
                candidates,
                source_type,
                source_file_id,
                candidate_budget,
            )
        ranked = sorted(candidates.values(), key=self._rank_key)
        merged = self._merge_candidate_specs(ranked)
        selected = merged if limit is None else merged[:limit]
        return {
            "query": query,
            "mode": mode,
            "source_type": source_type,
            "source_file_id": source_file_id,
            "total": len(merged),
            "total_is_exact": not truncated,
            "has_more": truncated or (limit is not None and len(merged) > limit),
            "results": [self._format_candidate(item) for item in selected],
            "return_all": limit is None,
            "index_metadata": self.index.get("metadata", {}),
        }

    def _sql_source_filter(
        self,
        source_type: str,
        source_file_id: Optional[str],
        alias: str = "",
    ) -> Tuple[str, List[object]]:
        prefix = f"{alias}." if alias else ""
        clauses: List[str] = []
        args: List[object] = []
        if source_type != "all":
            clauses.append(f"{prefix}source_type = ?")
            args.append(source_type)
        if source_file_id:
            clauses.append(f"{prefix}source_file_id = ?")
            args.append(source_file_id)
        return (" AND " + " AND ".join(clauses), args) if clauses else ("", args)

    def _ensure_fts_ready(self) -> bool:
        if self.backend != "sqlite":
            return False
        if self._fts_ready:
            return True
        with self._db_init_lock:
            if self._fts_ready:
                return True
            if self._fts_install_attempted:
                return False
            self._fts_install_attempted = True
            if self.db is not None:
                self.db.close()
                self.db = None
            self._fts_ready = ensure_database_search_index(self.index_path)
            self.db = open_database(self.index_path)
            return self._fts_ready

    def _fts_match_expression(self, text: str, operator: str) -> Optional[str]:
        """Build a bounded trigram query for the detail-free FTS table."""

        if len(text) < 3 or operator not in {"AND", "OR"}:
            return None
        if not self._ensure_fts_ready():
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

    def _sql_exact_pass(
        self,
        query: str,
        q_norm: str,
        q_plain: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
        candidate_budget: Optional[int],
    ) -> bool:
        if self.db is None:
            return False
        fts_query = self._fts_match_expression(q_plain, "AND")
        source_clause, source_args = self._sql_source_filter(
            source_type, source_file_id, "p"
        )
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
                "WHERE p.eligible_for_search = 1"
                + source_clause
                + " AND (instr(p.text_raw, ?) > 0 OR instr(p.normalized_text, ?) > 0) "
                "ORDER BY p.rowid"
            )
            args = [*source_args, query, q_norm]
        processed = 0
        truncated = False
        sql = self._limit_sql(sql, candidate_budget)
        for row in self.db.execute(sql, args):
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
                span = self._mapped_span(raw, q_norm, "normalized")
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
        candidate_budget: Optional[int],
    ) -> bool:
        if self.db is None or not query or column not in {"compact_text", "plain_text"}:
            return False
        fts_query = self._fts_match_expression(q_plain, "AND")
        source_clause, source_args = self._sql_source_filter(
            source_type, source_file_id, "p"
        )
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
                "WHERE p.eligible_for_search = 1"
                + source_clause
                + f" AND instr(p.{column}, ?) > 0 ORDER BY p.rowid"
            )
            args = [*source_args, query]
        processed = 0
        truncated = False
        sql = self._limit_sql(sql, candidate_budget)
        for row in self.db.execute(sql, args):
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
            normalized, mapping = normalize_with_map(raw, "compact" if column == "compact_text" else "plain")
            if pos >= len(mapping):
                continue
            end_pos = min(pos + len(query) - 1, len(mapping) - 1)
            self._add_candidate(paragraph, match_type, score, mapping[pos], mapping[end_pos] + 1, candidates)
        return truncated

    def _sql_fuzzy_pass(
        self,
        q_plain: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
        candidate_budget: Optional[int],
    ) -> bool:
        if self.db is None or not q_plain:
            return False
        source_clause, source_args = self._sql_source_filter(source_type, source_file_id, "p")
        fts_query = self._fts_match_expression(q_plain, "OR")
        if fts_query:
            rows = self.db.execute(
                f"SELECT {PARAGRAPH_SELECT_COLUMNS} "
                "FROM paragraphs_fts JOIN paragraphs p "
                "ON p.rowid = paragraphs_fts.rowid "
                "WHERE paragraphs_fts MATCH ? AND p.eligible_for_search = 1"
                + source_clause
                + " ORDER BY bm25(paragraphs_fts) LIMIT 701",
                [fts_query, *source_args],
            )
            processed = 0
            truncated = False
            for row in rows:
                if processed >= 700:
                    truncated = True
                    break
                processed += 1
                paragraph = paragraph_from_database_row(row)
                plain = str(paragraph.get("plain_text") or "")
                ratio, start, end = best_window_ratio(q_plain, plain)
                if ratio < 0.58:
                    continue
                raw = str(paragraph.get("text_raw") or "")
                _, mapping = normalize_with_map(raw, "plain")
                if not mapping:
                    continue
                start = max(0, min(start, len(mapping) - 1))
                end = max(start, min(end, len(mapping) - 1))
                score = min(0.9, max(0.58, ratio))
                self._add_candidate(
                    paragraph,
                    "ngram_fuzzy",
                    score,
                    mapping[start],
                    mapping[end] + 1,
                    candidates,
                )
                if candidate_budget is not None and len(candidates) >= candidate_budget:
                    truncated = True
                    break
            return truncated

        rows = self.db.execute(
            f"SELECT {PARAGRAPH_SELECT_COLUMNS} FROM paragraphs p "
            "WHERE p.eligible_for_search = 1" + source_clause,
            source_args,
        )
        query_grams = set(self._ngrams(q_plain))
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
        for _, _, paragraph in ranked[:700]:
            plain = str(paragraph.get("plain_text") or "")
            ratio, start, end = best_window_ratio(q_plain, plain)
            if ratio < 0.58:
                continue
            raw = str(paragraph.get("text_raw") or "")
            _, mapping = normalize_with_map(raw, "plain")
            if not mapping:
                continue
            start = max(0, min(start, len(mapping) - 1))
            end = max(start, min(end, len(mapping) - 1))
            score = min(0.9, max(0.58, ratio))
            self._add_candidate(paragraph, "ngram_fuzzy", score, mapping[start], mapping[end] + 1, candidates)
        return len(ranked) > 700

    @staticmethod
    def _ngrams_set(text: str, n: int = 2) -> set[str]:
        if len(text) <= n:
            return {text} if text else set()
        return {text[index : index + n] for index in range(len(text) - n + 1)}

    def _exact_pass(
        self,
        query: str,
        q_norm: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
    ) -> None:
        for paragraph in self.paragraphs:
            if not self._source_allowed(paragraph, source_type, source_file_id):
                continue
            raw = str(paragraph.get("text_raw") or "")
            normalized = str(paragraph.get("normalized_text") or "")
            raw_pos = raw.find(query)
            if raw_pos >= 0:
                self._add_candidate(paragraph, "exact", 1.0, raw_pos, raw_pos + len(query), candidates)
                continue
            norm_pos = normalized.find(q_norm)
            if norm_pos >= 0:
                span = self._mapped_span(raw, q_norm, "normalized")
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
    ) -> None:
        if not query:
            return
        field = "compact_text" if mode == "compact" else "plain_text"
        for paragraph in self.paragraphs:
            if not self._source_allowed(paragraph, source_type, source_file_id):
                continue
            haystack = str(paragraph.get(field) or "")
            pos = haystack.find(query)
            if pos < 0:
                continue
            raw = str(paragraph.get("text_raw") or "")
            normalized, mapping = normalize_with_map(raw, mode)
            if pos >= len(mapping):
                continue
            end_pos = min(pos + len(query) - 1, len(mapping) - 1)
            start_raw = mapping[pos]
            end_raw = mapping[end_pos] + 1
            self._add_candidate(paragraph, match_type, score, start_raw, end_raw, candidates)

    def _fuzzy_pass(
        self,
        q_plain: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
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
            if not self._source_allowed(paragraph, source_type, source_file_id):
                continue
            plain = str(paragraph.get("plain_text") or "")
            ratio, start, end = best_window_ratio(q_plain, plain)
            if ratio < 0.58:
                continue
            raw = str(paragraph.get("text_raw") or "")
            _, mapping = normalize_with_map(raw, "plain")
            if not mapping:
                continue
            start = max(0, min(start, len(mapping) - 1))
            end = max(start, min(end, len(mapping) - 1))
            score = min(0.9, max(0.58, ratio))
            self._add_candidate(paragraph, "ngram_fuzzy", score, mapping[start], mapping[end] + 1, candidates)

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

    def _format_candidate(self, candidate: Dict[str, object]) -> Dict[str, object]:
        paragraph = candidate.get("paragraph")
        if not isinstance(paragraph, dict):
            raise ValueError("Invalid search candidate payload.")
        return self._format_result(
            paragraph,
            str(candidate.get("match_type") or "exact"),
            float(candidate.get("match_score") or 0.0),
            int(candidate.get("match_start") or 0),
            int(candidate.get("match_end") or 0),
        )

    def _format_result(
        self,
        paragraph: Dict[str, object],
        match_type: str,
        score: float,
        start: int,
        end: int,
    ) -> Dict[str, object]:
        raw = str(paragraph.get("text_raw") or "")
        # Search offsets are Python string offsets, i.e. Unicode code points.
        # Keep that contract explicit because JavaScript string offsets use
        # UTF-16 code units and must be converted by the reader UI.
        start = max(0, min(start, len(raw)))
        end = max(start, min(end, len(raw)))
        matched = raw[start:end] if end > start else trim_for_display(raw, 80)
        match_quote = raw[start:end][:50] if end > start else ""
        source_type = str(paragraph.get("source_type") or "word")
        page_match_spans = self._page_match_spans(paragraph, source_type, start, end, len(raw))
        spread_hit = self._resolve_spread_hit(paragraph, page_match_spans)
        page_fields = dict(paragraph)
        if spread_hit:
            page_fields["citation_page_start"] = spread_hit["citation_page_start"]
            page_fields["citation_page_end"] = spread_hit["citation_page_end"]
            page_fields["printed_page_start"] = spread_hit["citation_page_start"]
            page_fields["printed_page_end"] = spread_hit["citation_page_end"]
            page_fields["citation_page_label_start"] = spread_hit["citation_page_start"]
            page_fields["citation_page_label_end"] = spread_hit["citation_page_end"]
            try:
                page_fields["citation_page_number_start"] = int(
                    str(spread_hit["citation_page_start"])
                )
                page_fields["citation_page_number_end"] = int(
                    str(spread_hit["citation_page_end"])
                )
            except ValueError:
                page_fields["citation_page_number_start"] = None
                page_fields["citation_page_number_end"] = None
        page_display = build_page_display(page_fields)
        page = page_display.display
        page_note = page_display.note
        if source_type == "pdf":
            copy_text = f"{paragraph.get('document_title') or paragraph.get('work_title') or 'PDF 文献'}，{page}：{raw}"
            volume_display = str(paragraph.get("volume_display") or paragraph.get("document_title") or "PDF 文献")
        elif self._is_marx_engels_volume(paragraph):
            copy_text = f"《马克思恩格斯文集》第{paragraph.get('volume_number')}卷，{paragraph.get('work_title') or '未识别文献'}，{page}：{raw}"
            volume_display = f"《马克思恩格斯文集》第{paragraph.get('volume_number')}卷"
        else:
            volume_display = str(
                paragraph.get("volume_display")
                or paragraph.get("document_title")
                or paragraph.get("work_title")
                or "Word 文献"
            )
            copy_text = f"{volume_display}，{page}：{raw}"
        citation_metadata = self._citation_metadata(paragraph, source_type)
        citation_formats = build_citation_formats(citation_metadata, self._hit_page(page_fields, source_type, page))
        return {
            "paragraph_id": paragraph["paragraph_id"],
            "volume_id": paragraph["volume_id"],
            "volume_number": paragraph["volume_number"],
            "volume_display": volume_display,
            "work_id": paragraph.get("work_id"),
            "work_title": paragraph.get("work_title") or "未识别文献",
            "document_title": paragraph.get("document_title"),
            "author_label": paragraph.get("author_label"),
            "source_type": source_type,
            "source_file_id": paragraph.get("source_file_id"),
            "page": page,
            "page_source_type": page_display.page_source_type,
            "page_note": page_note,
            "page_confidence": paragraph.get("page_confidence"),
            "open_source_url": paragraph.get("open_source_url"),
            "pdf_page_start_index": paragraph.get("pdf_page_start_index"),
            "pdf_page_end_index": paragraph.get("pdf_page_end_index"),
            "pdf_page_start_label": paragraph.get("pdf_page_start_label"),
            "pdf_page_end_label": paragraph.get("pdf_page_end_label"),
            "printed_page_start": page_fields.get("printed_page_start"),
            "printed_page_end": page_fields.get("printed_page_end"),
            "citation_page_start": page_fields.get("citation_page_start"),
            "citation_page_end": page_fields.get("citation_page_end"),
            "citation_page_number_start": page_fields.get("citation_page_number_start"),
            "citation_page_number_end": page_fields.get("citation_page_number_end"),
            "citation_page_label_start": page_fields.get("citation_page_label_start"),
            "citation_page_label_end": page_fields.get("citation_page_label_end"),
            "page_scope": paragraph.get("page_scope"),
            "page_mapping_method": paragraph.get("page_mapping_method"),
            "page_mapping_confidence": paragraph.get("page_mapping_confidence"),
            "mapping_method": paragraph.get("mapping_method"),
            "mapping_confidence": paragraph.get("mapping_confidence"),
            "mapping_confidence_level": paragraph.get("mapping_confidence_level"),
            "mapping_evidence": paragraph.get("mapping_evidence"),
            "segment_id": paragraph.get("segment_id"),
            "layout_mode": paragraph.get("layout_mode"),
            "logical_page_side": spread_hit.get("logical_page_side") if spread_hit else None,
            "spread_hit_precision": (
                spread_hit.get("precision")
                if spread_hit
                else "range_fallback" if paragraph.get("layout_mode") == "spread" else None
            ),
            "is_cross_page": paragraph.get("is_cross_page", False),
            "matched_text": matched,
            "match_quote": match_quote,
            "match_start": start,
            "match_end": end,
            "match_offset_unit": "unicode_codepoint",
            "page_match_spans": page_match_spans,
            "precise_highlight_available": bool(page_match_spans),
            "paragraph_text": raw,
            "highlighted_html": highlight_html(raw, start, end),
            "context_before": self._context(paragraph, before=True),
            "context_after": self._context(paragraph, before=False),
            "match_score": round(float(score), 4),
            "match_type": match_type,
            "original_file_name": paragraph.get("original_file_name"),
            "paragraph_index": paragraph.get("paragraph_index"),
            "copy_text": copy_text,
            "citation_formats": citation_formats,
        }

    @staticmethod
    def _page_match_spans(
        paragraph: Dict[str, object],
        source_type: str,
        match_start: int,
        match_end: int,
        paragraph_length: int,
    ) -> List[Dict[str, object]]:
        """Map a paragraph match onto the exact source-page text ranges.

        ``text_source_spans`` belongs only to the PDF anchor contract.  Word
        records may have paragraph/page metadata, but they must never claim
        exact PDF-page highlighting.  Gaps between spans (notably the newline
        joining the two halves of a CROSS paragraph) intentionally have no
        page mapping.
        """

        if source_type != "pdf" or match_end <= match_start:
            return []
        source_spans = paragraph.get("text_source_spans")
        if not isinstance(source_spans, list):
            return []
        paragraph_raw = str(paragraph.get("text_raw") or "")

        mapped: List[Dict[str, object]] = []
        for span in source_spans:
            if not isinstance(span, dict):
                continue
            offset_unit = span.get("offset_unit")
            if offset_unit not in (None, "unicode_codepoint"):
                continue
            page_id = span.get("pdf_page_id")
            paragraph_start = span.get("paragraph_char_start")
            paragraph_end = span.get("paragraph_char_end")
            page_start = span.get("page_char_start")
            page_end = span.get("page_char_end")
            if not page_id or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (paragraph_start, paragraph_end, page_start, page_end)
            ):
                continue
            if not (
                0 <= paragraph_start <= paragraph_end <= paragraph_length
                and 0 <= page_start <= page_end
                and paragraph_end - paragraph_start == page_end - page_start
            ):
                continue

            overlap_start = max(match_start, paragraph_start)
            overlap_end = min(match_end, paragraph_end)
            if overlap_start >= overlap_end:
                continue
            mapped_start = page_start + (overlap_start - paragraph_start)
            mapped_end = page_start + (overlap_end - paragraph_start)
            mapped_span: Dict[str, object] = {
                "pdf_page_id": str(page_id),
                "page_char_start": mapped_start,
                "page_char_end": mapped_end,
                # Keep a page-local recovery quote.  CROSS matches need a
                # different fragment for each physical page; the paragraph-
                # level quote can include the unmapped joiner and therefore
                # cannot occur verbatim on either page.  Slice the original
                # paragraph text with the same Unicode-codepoint overlap used
                # for offset mapping, and keep the existing 50-codepoint bound.
                "match_quote": paragraph_raw[overlap_start:overlap_end][:50],
            }
            if "page_text_hash" in span:
                # Preserve the producer's value verbatim.  A future reader
                # compares it with the current page hash before trusting the
                # saved offsets and falls back to searching ``match_quote``.
                mapped_span["page_text_hash"] = span.get("page_text_hash")
            page_index = span.get("pdf_page_index")
            if isinstance(page_index, int) and not isinstance(page_index, bool):
                mapped_span["pdf_page_index"] = page_index
            mapped.append(mapped_span)
        return mapped

    def _resolve_spread_hit(
        self,
        paragraph: Dict[str, object],
        page_match_spans: List[Dict[str, object]],
    ) -> Optional[Dict[str, object]]:
        """Resolve a hit to logical spread pages, or fall back to its range."""

        if paragraph.get("layout_mode") != "spread" or not page_match_spans:
            return None
        source_file_id = str(paragraph.get("source_file_id") or "")
        if not source_file_id:
            return None

        citation_pages: List[str] = []
        resolved_sides: List[str] = []
        for span in page_match_spans:
            page_index = self._span_pdf_page_index(span)
            if page_index is None:
                return None
            page = self._pdf_page_record(source_file_id, page_index)
            if not page or page.get("layout_mode") != "spread":
                return None
            sides = self._spread_sides_for_span(page, span)
            if not sides:
                return None
            start_label = page.get("citation_page_start") or page.get("citation_page")
            end_label = page.get("citation_page_end") or start_label
            if not start_label or not end_label:
                return None
            direction = "rtl" if page.get("reading_direction") == "rtl" else "ltr"
            side_order = ("right", "left") if direction == "rtl" else ("left", "right")
            page_labels = {
                side_order[0]: str(start_label),
                side_order[1]: str(end_label),
            }
            ordered_sides = [side for side in side_order if side in sides]
            labels = [page_labels[side] for side in ordered_sides]
            for label in labels:
                if not citation_pages or citation_pages[-1] != label:
                    citation_pages.append(label)
            side_label = ordered_sides[0] if len(ordered_sides) == 1 else "both"
            resolved_sides.append(side_label)
            span["logical_page_side"] = side_label
            span["citation_page_start"] = labels[0]
            span["citation_page_end"] = labels[-1]

        if not citation_pages:
            return None
        unique_sides = set(resolved_sides)
        return {
            "citation_page_start": citation_pages[0],
            "citation_page_end": citation_pages[-1],
            "logical_page_side": next(iter(unique_sides)) if len(unique_sides) == 1 else "both",
            "precision": "exact_region",
        }

    @staticmethod
    def _span_pdf_page_index(span: Dict[str, object]) -> Optional[int]:
        value = span.get("pdf_page_index")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        match = re.search(r"-PAGE-(\d+)\Z", str(span.get("pdf_page_id") or ""))
        return int(match.group(1)) if match else None

    def _pdf_page_record(
        self,
        source_file_id: str,
        page_index: int,
    ) -> Optional[Dict[str, object]]:
        key = (source_file_id, page_index)
        if key in self._pdf_page_cache:
            return self._pdf_page_cache[key]
        page: Optional[Dict[str, object]] = None
        if self.db is not None:
            row = self.db.execute(
                "SELECT payload_json FROM pdf_pages "
                "WHERE source_file_id = ? AND pdf_page_index = ? LIMIT 1",
                (source_file_id, page_index),
            ).fetchone()
            if row is not None:
                try:
                    payload = json.loads(row[0])
                except (TypeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    page = payload
        self._pdf_page_cache[key] = page
        return page

    @classmethod
    def _spread_sides_for_span(
        cls,
        page: Dict[str, object],
        span: Dict[str, object],
    ) -> set[str]:
        start = span.get("page_char_start")
        end = span.get("page_char_end")
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (start, end)
        ):
            return set()
        gutter_x = page.get("gutter_x")
        try:
            gutter = float(gutter_x)
        except (TypeError, ValueError):
            gutter = 0.5
        if not 0.3 <= gutter <= 0.7:
            gutter = 0.5

        sides: set[str] = set()
        blocks = page.get("blocks")
        if not isinstance(blocks, list):
            return sides
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_start = block.get("page_char_start")
            block_end = block.get("page_char_end")
            if not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (block_start, block_end)
            ):
                continue
            if max(int(start), block_start) >= min(int(end), block_end):
                continue
            bbox = cls._normalized_block_bbox(page, block)
            if bbox is None:
                return set()
            x0, _, x1, _ = bbox
            if x0 < gutter < x1 and min(gutter - x0, x1 - gutter) > 0.05:
                return set()
            sides.add("left" if (x0 + x1) / 2 < gutter else "right")
        return sides

    @staticmethod
    def _normalized_block_bbox(
        page: Dict[str, object],
        block: Dict[str, object],
    ) -> Optional[Tuple[float, float, float, float]]:
        raw = block.get("bbox_normalized")
        if isinstance(raw, (list, tuple)) and len(raw) == 4:
            try:
                values = tuple(float(value) for value in raw)
            except (TypeError, ValueError):
                values = ()
            if len(values) == 4 and 0 <= values[0] <= values[2] <= 1:
                return values[0], values[1], values[2], values[3]
        raw = block.get("bbox")
        try:
            width = float(page.get("page_width") or 0)
            height = float(page.get("page_height") or 0)
            values = (
                tuple(float(value) for value in raw)
                if isinstance(raw, (list, tuple))
                else ()
            )
        except (TypeError, ValueError):
            return None
        if len(values) != 4 or width <= 0 or height <= 0:
            return None
        return (
            values[0] / width,
            values[1] / height,
            values[2] / width,
            values[3] / height,
        )

    def _citation_metadata(self, paragraph: Dict[str, object], source_type: str) -> Dict[str, object]:
        metadata: Dict[str, object] = {}
        source_record = self.sources_by_id.get(str(paragraph.get("source_file_id")))
        for record in (
            source_record,
            self.volumes_by_id.get(str(paragraph.get("volume_id"))),
            self.works_by_id.get(str(paragraph.get("work_id"))),
            paragraph,
        ):
            if isinstance(record, dict):
                for key, value in record.items():
                    if value not in (None, ""):
                        metadata[key] = value
        if isinstance(source_record, dict):
            bibliographic = source_record.get("bibliographic_metadata")
            if not isinstance(bibliographic, dict):
                bibliographic = source_record
            for key in (
                "title",
                "author",
                "country",
                "translator",
                "publisher",
                "publish_place",
                "publish_year",
                "isbn",
                "journal_name",
                "volume",
                "issue",
                "page_range",
                "document_type",
                "metadata_status",
                "metadata_source",
                "metadata_confidence",
                "metadata_evidence",
            ):
                if bibliographic.get(key) not in (None, ""):
                    metadata[key] = bibliographic[key]
            if bibliographic.get("title"):
                metadata["document_title"] = bibliographic["title"]
        if source_type == "word" and self._is_marx_engels_volume(metadata):
            metadata.setdefault("document_type", "marx_engels_collection")
            metadata.setdefault("collection_title", self._infer_marx_engels_collection_title(metadata))
            if metadata.get("collection_title") == "马克思恩格斯文集":
                metadata.setdefault("publication_place", "北京")
                metadata.setdefault("publisher", "人民出版社")
                metadata.setdefault("publication_year", "2009")
        else:
            metadata.setdefault("document_type", metadata.get("citation_type") or "book")
        metadata.setdefault("author", paragraph.get("author_label"))
        metadata.setdefault("title", paragraph.get("work_title") or paragraph.get("document_title"))
        metadata.setdefault("document_title", paragraph.get("document_title") or paragraph.get("work_title"))
        return metadata

    @staticmethod
    def _infer_marx_engels_collection_title(metadata: Dict[str, object]) -> str:
        text = "".join(
            str(metadata.get(key) or "")
            for key in ("collection_title", "document_title", "display_title", "title", "file_name", "original_file_name")
        )
        if "全集" in text:
            return "马克思恩格斯全集"
        if "选集" in text:
            return "马克思恩格斯选集"
        return "马克思恩格斯文集"

    @staticmethod
    def _is_marx_engels_volume(record: Dict[str, object]) -> bool:
        return (
            record.get("volume_number") is not None
            and str(record.get("volume_id") or "").upper().startswith("MEWJ-")
        )

    @staticmethod
    def _hit_page(paragraph: Dict[str, object], source_type: str, page_display: object) -> Dict[str, object]:
        resolved = resolve_citation_page(paragraph)
        if resolved.verified and resolved.start:
            return {
                "start": resolved.start,
                "end": resolved.end,
                "display": page_display,
            }
        return {
            "display": page_display
            or (
                "引用页码尚未校准"
                if source_type == "pdf"
                else "页码未验证"
            ),
            "uncalibrated": True,
        }

    def _context(self, paragraph: Dict[str, object], before: bool) -> List[Dict[str, str]]:
        if self.backend == "sqlite":
            return self._sql_context(paragraph, before)
        volume_id = str(paragraph.get("volume_id"))
        pidx = int(paragraph.get("paragraph_index", 0))
        source_file_id = str(paragraph.get("source_file_id") or "")
        plist = [
            item
            for item in self.by_volume[volume_id]
            if not source_file_id
            or str(item.get("source_file_id") or "") == source_file_id
        ]
        if str(paragraph.get("source_type") or "word") == "pdf":
            bounds = self._pdf_page_bounds(paragraph)
            if bounds is None:
                return []
            start_page, end_page = bounds
            positioned = [
                item
                for item in plist
                if (
                    int(item.get("paragraph_index") or 0) < pidx
                    if before
                    else int(item.get("paragraph_index") or 0) > pidx
                )
            ]
            if before:
                positioned.reverse()
            for item in positioned:
                item_bounds = self._pdf_page_bounds(item)
                if not self._is_real_pdf_page(item, item_bounds):
                    continue
                item_start, item_end = item_bounds
                if (before and item_end < start_page) or (
                    not before and item_start > end_page
                ):
                    return [self._context_item(item)]
            return []

        candidates = [
            item
            for item in plist
            if str(item.get("source_type") or "word") != "pdf"
            and str(item.get("text_raw") or "").strip()
            and (
                int(item.get("paragraph_index") or 0) < pidx
                if before
                else int(item.get("paragraph_index") or 0) > pidx
            )
        ]
        if not candidates:
            return []
        selected = (
            max(candidates, key=lambda item: int(item.get("paragraph_index") or 0))
            if before
            else min(candidates, key=lambda item: int(item.get("paragraph_index") or 0))
        )
        return [self._context_item(selected)]

    def _sql_context(self, paragraph: Dict[str, object], before: bool) -> List[Dict[str, str]]:
        if self.db is None:
            return []
        source_file_id = str(paragraph.get("source_file_id") or "")
        volume_id = paragraph.get("volume_id")
        source_column = "source_file_id" if source_file_id else "volume_id"
        source_value = source_file_id if source_file_id else volume_id
        source_predicate = (
            f"{source_column} IS NULL" if source_value is None else f"{source_column} = ?"
        )
        source_args = [] if source_value is None else [source_value]

        if str(paragraph.get("source_type") or "word") == "pdf":
            bounds = self._pdf_page_bounds(paragraph)
            if bounds is None:
                return []
            start_page, end_page = bounds
            paragraph_index = int(paragraph.get("paragraph_index") or 0)
            if before:
                position_predicate = "paragraph_index < ?"
                order = "DESC"
            else:
                position_predicate = "paragraph_index > ?"
                order = "ASC"
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
                f"AND p.{position_predicate} ORDER BY p.paragraph_index {order}"
            )
            for row in self.db.execute(sql, [*source_args, paragraph_index]):
                candidate = self._sql_context_candidate(row)
                candidate_bounds = self._pdf_page_bounds(candidate)
                if not self._is_real_pdf_page(candidate, candidate_bounds):
                    continue
                candidate_start, candidate_end = candidate_bounds
                if (before and candidate_end < start_page) or (
                    not before and candidate_start > end_page
                ):
                    return [self._context_item(candidate)]
            return []

        paragraph_index = int(paragraph.get("paragraph_index") or 0)
        if before:
            order = "DESC"
            predicate = "paragraph_index < ?"
        else:
            order = "ASC"
            predicate = "paragraph_index > ?"
        sql = (
            "SELECT paragraph_id, text_raw FROM paragraphs "
            f"WHERE {source_predicate} AND source_type != 'pdf' "
            f"AND trim(text_raw) != '' AND {predicate} "
            f"ORDER BY paragraph_index {order} LIMIT 1"
        )
        row = self.db.execute(sql, [*source_args, paragraph_index]).fetchone()
        if row is None:
            return []
        return [
            {
                "paragraph_id": str(row["paragraph_id"]),
                "text": str(row["text_raw"] or ""),
            }
        ]

    @staticmethod
    def _context_item(paragraph: Dict[str, object]) -> Dict[str, str]:
        return {
            "paragraph_id": str(paragraph["paragraph_id"]),
            "text": str(paragraph.get("text_raw") or ""),
        }

    @staticmethod
    def _sql_context_candidate(row: sqlite3.Row) -> Dict[str, object]:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "paragraph_id": row["paragraph_id"],
            "text_raw": row["text_raw"],
            "pdf_page_start_index": row["pdf_page_start_index"],
            "pdf_page_end_index": row["pdf_page_end_index"],
            "is_cross_page": payload.get("is_cross_page", False),
        }

    @staticmethod
    def _pdf_page_bounds(
        paragraph: Dict[str, object],
    ) -> Optional[Tuple[int, int]]:
        start = paragraph.get("pdf_page_start_index")
        end = paragraph.get("pdf_page_end_index")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end < start
        ):
            return None
        return start, end

    @staticmethod
    def _is_real_pdf_page(
        paragraph: Dict[str, object], bounds: Optional[Tuple[int, int]]
    ) -> bool:
        if (
            bounds is None
            or bounds[0] != bounds[1]
            or not str(paragraph.get("text_raw") or "").strip()
        ):
            return False
        paragraph_id = str(paragraph.get("paragraph_id") or "").upper()
        return not paragraph.get("is_cross_page") and "-CROSS-" not in paragraph_id

    def _mapped_span(self, raw: str, query: str, mode: str) -> Tuple[int, int]:
        normalized, mapping = normalize_with_map(raw, mode)
        pos = normalized.find(query)
        if pos < 0 or not mapping:
            return 0, min(len(raw), 80)
        end_pos = min(pos + len(query) - 1, len(mapping) - 1)
        return mapping[pos], mapping[end_pos] + 1

    @staticmethod
    def _ngrams(text: str, n: int = 2) -> List[str]:
        if len(text) <= n:
            return [text] if text else []
        return [text[i : i + n] for i in range(len(text) - n + 1)]

    def _merge_candidate_specs(
        self, ranked: Sequence[Dict[str, object]]
    ) -> List[Dict[str, object]]:
        """Deduplicate lightweight candidates before expensive formatting."""

        merged: List[Dict[str, object]] = []
        seen = set()
        for item in ranked:
            paragraph = item.get("paragraph")
            if not isinstance(paragraph, dict):
                continue
            if paragraph.get("is_cross_page") and self._cross_candidate_duplicate(
                item, ranked
            ):
                continue
            key = (
                paragraph.get("volume_id"),
                paragraph.get("work_id"),
                punctuationless_text(str(paragraph.get("text_raw") or ""))[:180],
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    @staticmethod
    def _cross_candidate_duplicate(
        cross_item: Dict[str, object], ranked: Sequence[Dict[str, object]]
    ) -> bool:
        cross = cross_item.get("paragraph")
        if not isinstance(cross, dict):
            return False
        raw = str(cross.get("text_raw") or "")
        start = int(cross_item.get("match_start") or 0)
        end = int(cross_item.get("match_end") or 0)
        matched = punctuationless_text(raw[start:end])
        if not matched:
            return False
        start_page = cross.get("pdf_page_start_index")
        end_page = cross.get("pdf_page_end_index")
        for item in ranked:
            if item is cross_item:
                continue
            paragraph = item.get("paragraph")
            if not isinstance(paragraph, dict) or paragraph.get("is_cross_page"):
                continue
            if paragraph.get("source_file_id") != cross.get("source_file_id"):
                continue
            page = paragraph.get("pdf_page_start_index")
            if page is None or start_page is None or end_page is None:
                continue
            if not (int(start_page) <= int(page) <= int(end_page)):
                continue
            page_text = punctuationless_text(str(paragraph.get("text_raw") or ""))
            if matched in page_text:
                return True
        return False

    def _merge_results(self, ranked: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
        merged: List[Dict[str, object]] = []
        seen = set()
        for item in ranked:
            if item.get("is_cross_page") and self._cross_page_duplicate(item, ranked):
                continue
            key = (
                item.get("volume_id"),
                item.get("work_id"),
                punctuationless_text(str(item.get("paragraph_text") or ""))[:180],
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    @staticmethod
    def _cross_page_duplicate(
        cross_item: Dict[str, object], ranked: Sequence[Dict[str, object]]
    ) -> bool:
        """Drop a cross-page helper hit when its match is wholly on one real page."""

        matched = punctuationless_text(str(cross_item.get("matched_text") or ""))
        if not matched:
            return False
        start_page = cross_item.get("pdf_page_start_index")
        end_page = cross_item.get("pdf_page_end_index")
        for item in ranked:
            if item is cross_item or item.get("is_cross_page"):
                continue
            if item.get("source_file_id") != cross_item.get("source_file_id"):
                continue
            page = item.get("pdf_page_start_index")
            if page is None or start_page is None or end_page is None:
                continue
            if not (int(start_page) <= int(page) <= int(end_page)):
                continue
            page_text = punctuationless_text(str(item.get("paragraph_text") or ""))
            if matched in page_text:
                return True
        return False

    def _source_allowed(
        self,
        paragraph: Dict[str, object],
        source_type: str,
        source_file_id: Optional[str],
    ) -> bool:
        if source_type != "all" and str(paragraph.get("source_type") or "word") != source_type:
            return False
        return not source_file_id or str(paragraph.get("source_file_id") or "") == source_file_id

    def _rank_key(self, item: Dict[str, object]) -> Tuple[float, int, int, str, int, str, int]:
        nested = item.get("paragraph")
        record = nested if isinstance(nested, dict) else item
        volume_number = record.get("volume_number")
        try:
            volume_sort = int(volume_number) if volume_number is not None else 9999
        except (TypeError, ValueError):
            volume_sort = 9999
        paragraph_index = record.get("paragraph_index")
        try:
            paragraph_sort = int(paragraph_index) if paragraph_index is not None else 0
        except (TypeError, ValueError):
            paragraph_sort = 0
        uncalibrated_sort = 1 if record.get("source_type") == "pdf" and not record.get("citation_page_start") else 0
        cross_sort = 1 if record.get("is_cross_page") else 0
        return (
            -float(item["match_score"]),
            uncalibrated_sort,
            cross_sort,
            str(record.get("source_type") or "word"),
            volume_sort,
            str(record.get("original_file_name") or ""),
            paragraph_sort,
        )


def best_window_ratio(query_plain: str, plain: str) -> Tuple[float, int, int]:
    if not query_plain or not plain:
        return 0.0, 0, 0
    if query_plain in plain:
        start = plain.find(query_plain)
        return 0.91, start, start + len(query_plain) - 1
    q_len = len(query_plain)
    if len(plain) <= q_len + 8:
        return difflib.SequenceMatcher(None, query_plain, plain).ratio(), 0, max(0, len(plain) - 1)
    window_sizes = sorted(set([q_len, int(q_len * 1.25) + 1, int(q_len * 1.6) + 1, q_len + 8]))
    best = (0.0, 0, min(len(plain) - 1, q_len))
    step = max(1, q_len // 3)
    for size in window_sizes:
        if size <= 0:
            continue
        for start in range(0, max(1, len(plain) - size + 1), step):
            window = plain[start : start + size]
            ratio = difflib.SequenceMatcher(None, query_plain, window).ratio()
            if ratio > best[0]:
                best = (ratio, start, start + len(window) - 1)
        tail_start = max(0, len(plain) - size)
        window = plain[tail_start:]
        ratio = difflib.SequenceMatcher(None, query_plain, window).ratio()
        if ratio > best[0]:
            best = (ratio, tail_start, len(plain) - 1)
    return best


def highlight_html(text: str, start: int, end: int) -> str:
    text = text or ""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    return (
        html.escape(text[:start])
        + "<mark>"
        + html.escape(text[start:end])
        + "</mark>"
        + html.escape(text[end:])
    )
