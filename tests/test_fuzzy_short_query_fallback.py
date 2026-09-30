"""Fuzzy search falls back to the bigram scan when every trigram carries a typo.

A 4-8 character query whose typos break every trigram gets no FTS candidate at
all, so the scorer never sees the intended sentence. For those lengths the
fuzzy pass retries through the bigram scan; 3-character queries stay as they
were (the benchmark showed ~9 of 10 hits would be unrelated "社会X"), and
4-character queries FTS can serve are untouched. From 5 to 8 characters the
bigram scan also runs after FTS found candidates, so a weak FTS hit cannot
hide the sentence (D7). Evidence: reports/fuzzy-search-benchmark-2026-09-30.md
sections 9 and 10.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.application.search_service import SearchRequest, SearchService
from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder import search_recall
from src.me_finder.search import SearchEngine
from src.me_finder.search_contract import FUZZY_BIGRAM_FALLBACK_LENGTHS

TEXTS = [
    "人们通过共同劳动形成制度并检验知识。",  # 0 target of the typo queries
    "历史条件决定了解释的界限。",  # 1
    "社会学的对象是社会事实。",  # 2
    "黠慧的学生在档案里找到了记录。",  # 3
]


def _corpus(texts=TEXTS) -> dict:
    paragraphs = []
    for index, text in enumerate(texts):
        paragraphs.append({
            "paragraph_id": f"P-{index:04d}", "volume_id": "V-a", "work_id": "W-a",
            "source_file_id": "word-a", "source_type": "word", "paragraph_index": index,
            "volume_number": 1, "style_name": "p", "original_page_start": str(index + 1),
            "citation_page_start": str(index + 1), "citation_page_end": str(index + 1),
            "eligible_for_search": True, "text_raw": text,
            "normalized_text": normalize_text(text), "compact_text": compact_text(text),
            "plain_text": punctuationless_text(text), "document_title": "a",
            "work_title": "a", "volume_display": "a",
            "page_display": "引用页码尚未校准", "page_source_type": "uncalibrated",
            "pdf_page_start_index": None, "pdf_page_end_index": None, "source_format": None,
        })
    return {
        "metadata": {},
        "source_files": [{"source_file_id": "word-a", "source_type": "word",
                          "file_name": "a.docx", "file_format": "docx"}],
        "volumes": [{"volume_id": "V-a", "source_file_id": "word-a", "source_type": "word"}],
        "works": [{"work_id": "W-a", "volume_id": "V-a", "source_type": "word", "title": "a"}],
        "paragraphs": paragraphs,
    }


class FuzzyShortQueryFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "index.sqlite3"
        build_database(_corpus(), self.path)
        self.engine = SearchEngine(self.path)
        self.addCleanup(self.engine.close)

    def search(self, query: str, mode: str = "fuzzy") -> dict:
        return SearchService.execute(self.engine, SearchRequest(query=query, mode=mode))

    def ids(self, query: str, mode: str = "fuzzy") -> list:
        return [item["paragraph_id"] for item in self.search(query, mode)["results"]]

    def test_lengths_are_four_to_eight(self) -> None:
        self.assertEqual(FUZZY_BIGRAM_FALLBACK_LENGTHS, range(4, 9))

    def test_typos_breaking_every_trigram_are_recovered(self) -> None:
        # 4 字错第 2 字、5 字错正中、8 字错第 3 与倒数第 3 字:三字片段全含错字。
        for query, original in (("共X劳动", "共同劳动"), ("共同X动形", "共同劳动形"),
                                ("共同X动形Y制度", "共同劳动形成制度")):
            with self.subTest(query=query):
                for mode in ("fuzzy", "auto"):
                    response = self.search(query, mode)
                    self.assertEqual([r["paragraph_id"] for r in response["results"]], ["P-0000"])
                    hit = response["results"][0]
                    self.assertEqual(hit["match_type"], "ngram_fuzzy")
                    self.assertEqual(TEXTS[0][hit["match_start"]:hit["match_end"]], original)

    def test_three_character_typos_stay_unrecalled(self) -> None:
        self.assertEqual(self.ids("社会论"), [])

    def test_nine_characters_never_fall_back(self) -> None:
        with patch("src.me_finder.search_recall.read_gram_passage_rows") as scan:
            self.assertEqual(self.ids("共X劳动Y成Z度并"), [])
        scan.assert_not_called()

    def test_queries_fts_can_serve_skip_the_scan_at_four_and_from_nine(self) -> None:
        for query in ("共同劳动", "人们通过共同劳动形X度"):
            with self.subTest(query=query):
                with patch("src.me_finder.search_recall.read_gram_passage_rows") as scan:
                    self.assertEqual(self.ids(query), ["P-0000"])
                scan.assert_not_called()

    def test_five_to_eight_scan_even_when_fts_found_something(self) -> None:
        with patch("src.me_finder.search_recall.read_gram_passage_rows",
                   wraps=search_recall.read_gram_passage_rows) as scan:
            self.assertEqual(self.ids("共同劳动形X制度"), ["P-0000"])
        scan.assert_called()


class WeakFtsCandidateTest(unittest.TestCase):
    """A weak FTS candidate must not hide the sentence (review case mid68-020)."""

    TEXTS = ["他们以共同劳动形成制度。", "这是异同乙动形的例子。"]

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "index.sqlite3"
        build_database(_corpus(self.TEXTS), path)
        self.engine = SearchEngine(path)
        self.addCleanup(self.engine.close)

    def ids(self, query: str) -> list:
        response = SearchService.execute(self.engine, SearchRequest(query=query, mode="fuzzy"))
        return [item["paragraph_id"] for item in response["results"]]

    def test_five_characters_find_the_sentence_behind_a_weak_fts_hit(self) -> None:
        # FTS matches only the unrelated "同乙动形"; the target shares no trigram.
        self.assertIn("P-0000", self.ids("共同乙动形"))
        with patch("src.me_finder.search_recall.fuzzy_needs_bigram_scan",
                   lambda length, found: 4 <= length <= 8 and not found):
            self.assertEqual(self.ids("共同乙动形"), ["P-0001"])  # the pre-D7 rule missed it

    def test_four_characters_keep_the_fallback_only_rule(self) -> None:
        self.assertEqual(self.ids("同乙动形"), ["P-0001"])

    def test_absent_query_still_returns_nothing(self) -> None:
        self.assertEqual(self.ids("鳞爪星槎"), [])
        self.assertEqual(self.ids("鳞爪星槎渊薮"), [])


if __name__ == "__main__":
    unittest.main()
