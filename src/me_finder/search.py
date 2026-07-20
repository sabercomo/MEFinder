"""Local deterministic search engine."""

from __future__ import annotations

import difflib
import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .citations import build_citation_formats
from .database import DEFAULT_DATABASE_PATH, load_database_index, open_database
from .indexer import DEFAULT_INDEX_PATH, load_index
from .normalization import (
    compact_text,
    normalize_text,
    normalize_with_map,
    punctuationless_text,
    trim_for_display,
)


SEARCH_MODES = {"auto", "exact", "compact", "punctuation", "fuzzy"}


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
        if self.backend == "sqlite":
            self.db = open_database(self.index_path)
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
        for paragraph in self.paragraphs:
            self.by_volume[str(paragraph["volume_id"])].append(paragraph)
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
        merged = self._merge_results(ranked)
        return {
            "query": query,
            "mode": mode,
            "source_type": source_type,
            "source_file_id": source_file_id,
            "total": len(merged),
            "results": merged if normalized_limit is None else merged[:normalized_limit],
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
        if mode in {"auto", "exact"}:
            self._sql_exact_pass(query, q_norm, candidates, source_type, source_file_id)
        if mode in {"auto", "compact"} and (mode != "auto" or not candidates):
            self._sql_mapped_substring_pass(q_compact, "compact_text", "space_insensitive", 0.96, candidates, source_type, source_file_id)
        if mode in {"auto", "punctuation"} and (mode != "auto" or not candidates):
            self._sql_mapped_substring_pass(q_plain, "plain_text", "punctuation_insensitive", 0.92, candidates, source_type, source_file_id)
        if mode in {"auto", "fuzzy"} and (mode != "auto" or not candidates):
            self._sql_fuzzy_pass(q_plain, candidates, source_type, source_file_id)
        ranked = sorted(candidates.values(), key=self._rank_key)
        merged = self._merge_results(ranked)
        return {
            "query": query,
            "mode": mode,
            "source_type": source_type,
            "source_file_id": source_file_id,
            "total": len(merged),
            "results": merged if limit is None else merged[:limit],
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

    def _sql_exact_pass(
        self,
        query: str,
        q_norm: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
    ) -> None:
        if self.db is None:
            return
        source_clause, source_args = self._sql_source_filter(source_type, source_file_id)
        sql = (
            "SELECT payload_json, text_raw, normalized_text FROM paragraphs "
            "WHERE eligible_for_search = 1"
            + source_clause
            + " AND (instr(text_raw, ?) > 0 OR instr(normalized_text, ?) > 0)"
        )
        args = source_args + [query, q_norm]
        for row in self.db.execute(sql, args):
            paragraph = json.loads(row["payload_json"])
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

    def _sql_mapped_substring_pass(
        self,
        query: str,
        column: str,
        match_type: str,
        score: float,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
    ) -> None:
        if self.db is None or not query or column not in {"compact_text", "plain_text"}:
            return
        source_clause, source_args = self._sql_source_filter(source_type, source_file_id)
        sql = (
            f"SELECT payload_json, text_raw, {column} FROM paragraphs "
            "WHERE eligible_for_search = 1"
            + source_clause
            + f" AND instr({column}, ?) > 0"
        )
        for row in self.db.execute(sql, source_args + [query]):
            paragraph = json.loads(row["payload_json"])
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

    def _sql_fuzzy_pass(
        self,
        q_plain: str,
        candidates: Dict[str, Dict[str, object]],
        source_type: str,
        source_file_id: Optional[str],
    ) -> None:
        if self.db is None or not q_plain:
            return
        source_clause, source_args = self._sql_source_filter(source_type, source_file_id, "p")
        rows = self.db.execute(
            "SELECT p.paragraph_id, p.payload_json, p.plain_text FROM paragraphs p "
            "WHERE p.eligible_for_search = 1" + source_clause,
            source_args,
        )
        query_grams = set(self._ngrams(q_plain))
        ranked: List[Tuple[int, str, Dict[str, object]]] = []
        for row in rows:
            plain = str(row["plain_text"] or "")
            overlap = len(query_grams.intersection(self._ngrams_set(plain)))
            if overlap:
                ranked.append((overlap, str(row["paragraph_id"]), json.loads(row["payload_json"])))
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
        result = self._format_result(paragraph, match_type, score, start, end)
        candidates[paragraph_id] = result

    def _format_result(
        self,
        paragraph: Dict[str, object],
        match_type: str,
        score: float,
        start: int,
        end: int,
    ) -> Dict[str, object]:
        raw = str(paragraph.get("text_raw") or "")
        matched = raw[start:end] if end > start else trim_for_display(raw, 80)
        source_type = str(paragraph.get("source_type") or "word")
        page = paragraph.get("page_display") or "页码未验证"
        page_note = page_source_note(str(paragraph.get("page_source_type") or "unknown"))
        if source_type == "pdf":
            copy_text = f"{paragraph.get('document_title') or paragraph.get('work_title') or 'PDF 文献'}，{page}：{raw}"
            volume_display = str(paragraph.get("volume_display") or paragraph.get("document_title") or "PDF 文献")
        else:
            copy_text = f"《马克思恩格斯文集》第{paragraph.get('volume_number')}卷，{paragraph.get('work_title') or '未识别文献'}，第{page}页：{raw}"
            volume_display = f"《马克思恩格斯文集》第{paragraph.get('volume_number')}卷"
        citation_metadata = self._citation_metadata(paragraph, source_type)
        citation_formats = build_citation_formats(citation_metadata, self._hit_page(paragraph, source_type, page))
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
            "page_source_type": paragraph.get("page_source_type"),
            "page_note": page_note,
            "page_confidence": paragraph.get("page_confidence"),
            "open_source_url": paragraph.get("open_source_url"),
            "pdf_page_start_index": paragraph.get("pdf_page_start_index"),
            "pdf_page_end_index": paragraph.get("pdf_page_end_index"),
            "pdf_page_start_label": paragraph.get("pdf_page_start_label"),
            "pdf_page_end_label": paragraph.get("pdf_page_end_label"),
            "printed_page_start": paragraph.get("printed_page_start"),
            "printed_page_end": paragraph.get("printed_page_end"),
            "citation_page_start": paragraph.get("citation_page_start"),
            "citation_page_end": paragraph.get("citation_page_end"),
            "citation_page_number_start": paragraph.get("citation_page_number_start"),
            "citation_page_number_end": paragraph.get("citation_page_number_end"),
            "citation_page_label_start": paragraph.get("citation_page_label_start"),
            "citation_page_label_end": paragraph.get("citation_page_label_end"),
            "page_scope": paragraph.get("page_scope"),
            "page_mapping_method": paragraph.get("page_mapping_method"),
            "page_mapping_confidence": paragraph.get("page_mapping_confidence"),
            "mapping_method": paragraph.get("mapping_method"),
            "mapping_confidence": paragraph.get("mapping_confidence"),
            "mapping_confidence_level": paragraph.get("mapping_confidence_level"),
            "mapping_evidence": paragraph.get("mapping_evidence"),
            "segment_id": paragraph.get("segment_id"),
            "is_cross_page": paragraph.get("is_cross_page", False),
            "matched_text": matched,
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
        if source_type == "word":
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
    def _hit_page(paragraph: Dict[str, object], source_type: str, page_display: object) -> Dict[str, object]:
        if source_type == "pdf":
            start = paragraph.get("citation_page_start")
            end = paragraph.get("citation_page_end")
            if start:
                return {"start": start, "end": end}
            return {
                "display": page_display or "引用页码尚未校准",
                "uncalibrated": True,
            }
        return {
            "start": paragraph.get("original_page_start") or page_display,
            "end": paragraph.get("original_page_end"),
            "display": page_display,
        }

    def _context(self, paragraph: Dict[str, object], before: bool) -> List[Dict[str, str]]:
        if self.backend == "sqlite":
            return self._sql_context(paragraph, before)
        volume_id = str(paragraph["volume_id"])
        pidx = int(paragraph.get("paragraph_index", 0))
        plist = self.by_volume[volume_id]
        nearby = []
        if before:
            source = [p for p in plist if int(p.get("paragraph_index", 0)) < pidx][-2:]
        else:
            source = [p for p in plist if int(p.get("paragraph_index", 0)) > pidx][:2]
        for p in source:
            nearby.append({"paragraph_id": str(p["paragraph_id"]), "text": str(p.get("text_raw") or "")})
        return nearby

    def _sql_context(self, paragraph: Dict[str, object], before: bool) -> List[Dict[str, str]]:
        if self.db is None:
            return []
        volume_id = paragraph.get("volume_id")
        paragraph_index = int(paragraph.get("paragraph_index") or 0)
        if before:
            order = "DESC"
            predicate = "paragraph_index < ?"
        else:
            order = "ASC"
            predicate = "paragraph_index > ?"
        if volume_id is None:
            sql = (
                "SELECT paragraph_id, text_raw FROM paragraphs "
                f"WHERE volume_id IS NULL AND {predicate} ORDER BY paragraph_index {order} LIMIT 2"
            )
            args = [paragraph_index]
        else:
            sql = (
                "SELECT paragraph_id, text_raw FROM paragraphs "
                f"WHERE volume_id = ? AND {predicate} ORDER BY paragraph_index {order} LIMIT 2"
            )
            args = [volume_id, paragraph_index]
        rows = list(self.db.execute(sql, args))
        if before:
            rows.reverse()
        return [{"paragraph_id": str(row["paragraph_id"]), "text": str(row["text_raw"] or "")} for row in rows]

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
        volume_number = item.get("volume_number")
        try:
            volume_sort = int(volume_number) if volume_number is not None else 9999
        except (TypeError, ValueError):
            volume_sort = 9999
        paragraph_index = item.get("paragraph_index")
        try:
            paragraph_sort = int(paragraph_index) if paragraph_index is not None else 0
        except (TypeError, ValueError):
            paragraph_sort = 0
        uncalibrated_sort = 1 if item.get("source_type") == "pdf" and not item.get("citation_page_start") else 0
        cross_sort = 1 if item.get("is_cross_page") else 0
        return (
            -float(item["match_score"]),
            uncalibrated_sort,
            cross_sort,
            str(item.get("source_type") or "word"),
            volume_sort,
            str(item.get("original_file_name") or ""),
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


def page_source_note(source_type: str) -> str:
    notes = {
        "section_break_inferred": "分节推断页码，尚未人工验证",
        "section_break_verified": "分节页码，已验证",
        "word_rendered_page": "排版引擎页码",
        "printed_page_marker": "印刷页码锚点",
        "toc_range_bound": "目录页码范围，非段落级精确页码",
        "manual_segment": "PDF 页码来自人工分段映射",
        "fixed_offset": "PDF 页码来自固定偏移映射",
        "manual_page": "PDF 页码来自人工逐页校准",
        "pdf_page_label": "PDF Page Label，需抽样验证",
        "uncalibrated": "PDF 引用页码尚未校准",
        "mixed": "PDF 跨页命中涉及不同页码来源",
        "unknown": "页码未验证",
    }
    return notes.get(source_type, "页码来源未说明")


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
