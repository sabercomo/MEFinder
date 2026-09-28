"""In-memory recall passes for the legacy JSON index backend.

Split out of :mod:`search_recall` so the SQLite recall module stays within its
size budget. :class:`CandidateRecall` mixes these in; they rely on its
``paragraphs``/``ngram_index`` state and its shared candidate helpers.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, Optional

Scope = Optional[frozenset]


class InMemoryRecallPasses:
    """Exact, mapped-substring and fuzzy passes over in-memory paragraphs."""

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
