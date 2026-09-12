"""Candidate-budget boundaries for SQLite recall.

Recall caps work at ``candidate_budget = max(SQL_CANDIDATE_FLOOR, limit*8)`` by
selecting ``LIMIT budget+1`` and marking the result truncated once more than
``budget`` candidates are seen. These tests pin the boundary behaviour (fewer
than / exactly / more than the budget) for the exact, compact and punctuation
passes, under a source-type range filter, and confirm ineligible paragraphs are
never recalled and that returned hits stay ordered by non-increasing score.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.search import SearchEngine
from src.me_finder.search_contract import SQL_CANDIDATE_FLOOR

# limit=5 -> budget = max(64, 40) = 64; keep it small so fixtures stay modest.
LIMIT = 5
BUDGET = max(SQL_CANDIDATE_FLOOR, LIMIT * 8)


def _paragraph(index: int, source_id: str, text: str, eligible: bool) -> dict:
    return {
        "paragraph_id": f"{source_id}-P{index:04d}",
        "volume_id": f"VOL-{source_id}",
        "volume_number": None,
        "work_id": f"WORK-{source_id}",
        "source_file_id": source_id,
        "source_type": "pdf",
        "paragraph_index": index,
        "eligible_for_search": eligible,
        "text_raw": text,
        "normalized_text": normalize_text(text),
        "compact_text": compact_text(text),
        "plain_text": punctuationless_text(text),
        "document_title": "书",
        "work_title": "书",
        "volume_display": "书",
        "page_display": "引用页码尚未校准",
        "page_source_type": "uncalibrated",
        "pdf_page_start_index": index,
        "pdf_page_end_index": index,
        "original_file_name": f"{source_id}.pdf",
    }


def _build(matching_texts, *, second_source_matches=0, ineligible_matches=0):
    """Build a one/two-source SQLite index and return its path (+ temp handle)."""

    temp = tempfile.TemporaryDirectory()
    database_path = Path(temp.name) / "index.sqlite3"
    sources = [{"source_file_id": "pdf-a", "source_type": "pdf", "file_name": "a.pdf"}]
    volumes = [{"volume_id": "VOL-pdf-a", "source_file_id": "pdf-a", "source_type": "pdf", "display_title": "书"}]
    works = [{"work_id": "WORK-pdf-a", "volume_id": "VOL-pdf-a", "source_type": "pdf", "title": "书"}]
    paragraphs = []
    idx = 0
    for text in matching_texts:
        paragraphs.append(_paragraph(idx, "pdf-a", text, eligible=True))
        idx += 1
    for _ in range(ineligible_matches):
        paragraphs.append(_paragraph(idx, "pdf-a", matching_texts[0], eligible=False))
        idx += 1
    # A few decoys that never match.
    for _ in range(3):
        paragraphs.append(_paragraph(idx, "pdf-a", "毫不相干的文本。", eligible=True))
        idx += 1
    if second_source_matches:
        sources.append({"source_file_id": "pdf-b", "source_type": "pdf", "file_name": "b.pdf"})
        volumes.append({"volume_id": "VOL-pdf-b", "source_file_id": "pdf-b", "source_type": "pdf", "display_title": "乙"})
        works.append({"work_id": "WORK-pdf-b", "volume_id": "VOL-pdf-b", "source_type": "pdf", "title": "乙"})
        for _ in range(second_source_matches):
            paragraphs.append(_paragraph(idx, "pdf-b", matching_texts[0], eligible=True))
            idx += 1
    build_database(
        {"metadata": {}, "source_files": sources, "volumes": volumes, "works": works, "paragraphs": paragraphs},
        database_path,
    )
    return temp, database_path


class ExactPassBudgetTests(unittest.TestCase):
    TERM = "查询词"  # 3 chars -> FTS trigram exact path

    def _search(self, n_matching, **build_kwargs):
        texts = [f"{self.TERM}第{i}段落。" for i in range(n_matching)]
        temp, path = _build(texts, **build_kwargs)
        engine = SearchEngine(path)
        try:
            return engine.search(self.TERM, mode="exact", limit=LIMIT)
        finally:
            engine.close()
            temp.cleanup()

    def test_below_budget_reports_exact_total(self) -> None:
        result = self._search(BUDGET - 1)
        self.assertEqual(result["total"], BUDGET - 1)
        self.assertTrue(result["total_is_exact"])
        self.assertTrue(result["has_more"])  # more than LIMIT rendered
        self.assertEqual(len(result["results"]), LIMIT)

    def test_exactly_budget_is_not_truncated(self) -> None:
        result = self._search(BUDGET)
        self.assertEqual(result["total"], BUDGET)
        self.assertTrue(result["total_is_exact"])

    def test_above_budget_is_truncated(self) -> None:
        result = self._search(BUDGET + 6)
        self.assertEqual(result["total"], BUDGET)
        self.assertFalse(result["total_is_exact"])
        self.assertTrue(result["has_more"])

    def test_ineligible_paragraphs_are_never_recalled(self) -> None:
        result = self._search(10, ineligible_matches=5)
        self.assertEqual(result["total"], 10)
        self.assertTrue(result["total_is_exact"])

    def test_source_type_range_filter_bounds_the_candidate_set(self) -> None:
        texts = [f"{self.TERM}第{i}段。" for i in range(BUDGET + 6)]
        temp, path = _build(texts, second_source_matches=4)
        engine = SearchEngine(path)
        try:
            scoped = engine.search(self.TERM, mode="exact", limit=LIMIT, source_file_id="pdf-b")
        finally:
            engine.close()
            temp.cleanup()
        # Only the 4 second-source matches are in scope: exact total, not truncated.
        self.assertEqual(scoped["total"], 4)
        self.assertTrue(scoped["total_is_exact"])
        for hit in scoped["results"]:
            self.assertEqual(hit["source_file_id"], "pdf-b")

    def test_results_are_ordered_by_non_increasing_score(self) -> None:
        result = self._search(BUDGET + 6)
        scores = [float(hit["match_score"]) for hit in result["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))


class CompactAndPunctuationBudgetTests(unittest.TestCase):
    def test_compact_pass_respects_budget(self) -> None:
        # Space-separated so only the compact (space-insensitive) pass matches.
        texts = [f"查 询 词第{i}段。" for i in range(BUDGET + 6)]
        temp, path = _build(texts)
        engine = SearchEngine(path)
        try:
            result = engine.search("查询词", mode="compact", limit=LIMIT)
        finally:
            engine.close()
            temp.cleanup()
        self.assertEqual(result["total"], BUDGET)
        self.assertFalse(result["total_is_exact"])
        self.assertTrue(result["has_more"])

    def test_punctuation_pass_respects_budget(self) -> None:
        # Punctuation inside the term so only the punctuation-insensitive pass matches.
        texts = [f"查，询，词第{i}段。" for i in range(BUDGET + 6)]
        temp, path = _build(texts)
        engine = SearchEngine(path)
        try:
            result = engine.search("查询词", mode="punctuation", limit=LIMIT)
        finally:
            engine.close()
            temp.cleanup()
        self.assertEqual(result["total"], BUDGET)
        self.assertFalse(result["total_is_exact"])
        self.assertTrue(result["has_more"])


if __name__ == "__main__":
    unittest.main()
