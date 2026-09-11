"""Local deterministic search engine.

``SearchEngine`` is the compatibility facade: it owns the index/backend state
and orchestrates the one-way pipeline stages — candidate recall
(:mod:`search_recall`), scoring and deduplication (:mod:`search_scoring`) and
result assembly (:mod:`search_assembly`, backed by :mod:`search_anchors` and
:mod:`search_citation`). The response payload is frozen by
``tests/test_search_pipeline_contract.py``; match offsets are Unicode
code-point indices into ``text_raw`` and every PDF hit carries page-level
``page_match_spans`` anchors.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path
from typing import (
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from .database import (
    DEFAULT_DATABASE_PATH,
    database_has_fts5_search_index,
    ensure_database_search_index,
    load_database_index,
    open_database,
)
from .indexer import DEFAULT_INDEX_PATH, load_index
from .search_assembly import format_passage, format_result, highlight_html, paragraph_context
from .search_citation import hit_page
from .search_contract import SEARCH_MODES
from .search_recall import CandidateRecall
from .search_scoring import best_window_ratio, merge_candidate_specs, rank_key

# Re-exported for callers that historically imported these helpers from here.
__all__ = ["SearchEngine", "best_window_ratio", "highlight_html"]


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

    @staticmethod
    def _ngrams(text: str, n: int = 2) -> List[str]:
        if len(text) <= n:
            return [text] if text else []
        return [text[i : i + n] for i in range(len(text) - n + 1)]

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

    def _recall(self) -> CandidateRecall:
        return CandidateRecall(
            db_provider=lambda: self.db,
            backend=self.backend,
            paragraphs=self.paragraphs,
            ngram_index=self.ngram_index,
            ensure_fts=self._ensure_fts_ready,
        )

    def search(
        self,
        query: str,
        mode: str = "auto",
        limit: int | str | None = 10,
        source_type: str = "all",
        source_file_id: Optional[str] = None,
        source_file_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, object]:
        query = (query or "").strip()
        source_file_id = str(source_file_id or "").strip() or None
        # A set scope (e.g. a DocumentGroup's members, resolved upstream) is an
        # explicit candidate filter. None = no set scope; an empty set = an
        # explicit empty scope that matches nothing — never widened to the
        # whole library.
        scope = (
            frozenset(str(item) for item in source_file_ids)
            if source_file_ids is not None
            else None
        )
        if mode not in SEARCH_MODES:
            mode = "auto"
        if source_type not in {"all", "word", "epub", "pdf"}:
            source_type = "all"
        return_all = str(limit or "").strip().lower() in {"all", "0"}
        normalized_limit = None if return_all else max(1, min(int(limit or 10), 200))
        if not query:
            return {"query": query, "mode": mode, "total": 0, "results": []}
        recall = self._recall()
        candidates, truncated = recall.collect(
            query, mode, source_type, source_file_id, scope,
            recall.candidate_budget(normalized_limit),
        )
        ranked = sorted(candidates.values(), key=rank_key)
        merged = merge_candidate_specs(ranked)
        selected = merged if normalized_limit is None else merged[:normalized_limit]
        return {
            "query": query,
            "mode": mode,
            "source_type": source_type,
            "source_file_id": source_file_id,
            "total": len(merged),
            "total_is_exact": not truncated,
            "has_more": truncated or (normalized_limit is not None and len(merged) > normalized_limit),
            "results": [self._format_candidate(item) for item in selected],
            "return_all": return_all,
            "index_metadata": self.index.get("metadata", {}),
        }

    def search_passages(
        self,
        query: str,
        limit: int = 10,
        source_type: str = "all",
        source_file_id: Optional[str] = None,
        source_file_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, object]:
        """Rank passages by BM25 keyword relevance to a free description.

        Unlike :meth:`search` (verbatim / near-verbatim location), this ranks by
        how relevant a passage is to a loose description or keywords and does not
        require the query to resemble a contiguous span of the passage — so it can
        never claim a verbatim hit. The retrieval step lives in
        :class:`search_recall.CandidateRecall` so an embedding backend can replace
        it later without touching formatting or the tool contract.
        """

        query = (query or "").strip()
        scope = (
            frozenset(str(item) for item in source_file_ids)
            if source_file_ids is not None
            else None
        )
        if source_type not in {"all", "word", "epub", "pdf"}:
            source_type = "all"
        source_file_id = str(source_file_id or "").strip() or None
        normalized_limit = max(1, min(int(limit or 10), 50))
        if not query:
            return {
                "query": query,
                "total": 0,
                "total_is_exact": True,
                "has_more": False,
                "results": [],
            }
        ranked, truncated, method = self._recall().retrieve_passages(
            query, source_type, source_file_id, scope, normalized_limit
        )
        total = len(ranked)
        selected = ranked[:normalized_limit]
        raw_scores = [raw for _paragraph, raw in selected]
        results = [
            format_passage(paragraph, rank, raw, raw_scores, method, self)
            for rank, (paragraph, raw) in enumerate(selected, start=1)
        ]
        return {
            "query": query,
            "total": total,
            "total_is_exact": not truncated,
            "has_more": truncated or total > normalized_limit,
            "results": results,
        }

    # ------------------------------------------------------------------
    # ResultStore implementation used by the assembly stage
    # ------------------------------------------------------------------

    def pdf_page_record(
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

    def context(self, paragraph: Dict[str, object], before: bool) -> List[Dict[str, str]]:
        return paragraph_context(self.db, self.by_volume, paragraph, before, self.backend)

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

    # ------------------------------------------------------------------
    # Compatibility delegates used directly by tests and tooling
    # ------------------------------------------------------------------

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
        return format_result(paragraph, match_type, score, start, end, self)

    @staticmethod
    def _hit_page(paragraph: Dict[str, object], source_type: str, page_display: object) -> Dict[str, object]:
        return hit_page(paragraph, source_type, page_display)
