"""``search_passages`` recall for queries too short for the trigram FTS.

Two-character queries cannot form an FTS trigram, so relevance retrieval falls
back to a gram-overlap scan. The SQLite backend never loads the in-memory
paragraph list, so that fallback used to return nothing ("社会" found no
passage while "社会学" did). The SQLite scan must read the database, honour
every filter, and stay identical with or without the short-gram prefilter.
"""

from __future__ import annotations

import contextlib
import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.database import build_database, paragraph_from_database_row
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.persistence import passage_reads
from src.me_finder.persistence import short_gram_index as sgi
from src.me_finder.search import SearchEngine
from src.me_finder.search_recall import CandidateRecall
from src.me_finder.search_recall_passages import gram_overlap

TEXTS = [
    "社会学的对象是社会事实。",  # 0 pdf-a
    "这里只谈历史。",  # 1 word-b: no match
    "社会与个人。",  # 2 epub-c
    "社 会被空格隔开，社会仍在。",  # 3 pdf-a
    "社会关系的总和。",  # 4 word-b: ineligible, never returned
    "社会学史。",  # 5 epub-c
]
SOURCES = [("pdf-a", "pdf"), ("word-b", "word"), ("epub-c", "word")]


def _corpus() -> dict:
    paragraphs = []
    for index, text in enumerate(TEXTS):
        source, source_type = SOURCES[index % 3]
        paragraphs.append({
            "paragraph_id": f"P-{index:04d}", "volume_id": f"V-{source}", "work_id": f"W-{source}",
            "source_file_id": source, "source_type": source_type, "paragraph_index": index,
            "volume_number": 1, "style_name": "p", "original_page_start": str(index + 1),
            "citation_page_start": str(index + 1), "citation_page_end": str(index + 1),
            "eligible_for_search": index != 4, "text_raw": text,
            "normalized_text": normalize_text(text), "compact_text": compact_text(text),
            "plain_text": punctuationless_text(text), "document_title": source,
            "work_title": source, "volume_display": source,
            "page_display": "引用页码尚未校准", "page_source_type": "uncalibrated",
            "pdf_page_start_index": index if source_type == "pdf" else None,
            "pdf_page_end_index": index if source_type == "pdf" else None,
            "source_format": "epub" if source == "epub-c" else None,
        })
    return {
        "metadata": {},
        "source_files": [
            {"source_file_id": source, "source_type": source_type, "file_name": source,
             "file_format": "epub" if source == "epub-c" else source_type}
            for source, source_type in SOURCES
        ],
        "volumes": [{"volume_id": f"V-{s}", "source_file_id": s, "source_type": t} for s, t in SOURCES],
        "works": [{"work_id": f"W-{s}", "volume_id": f"V-{s}", "source_type": t, "title": s} for s, t in SOURCES],
        "paragraphs": paragraphs,
    }


class SearchPassagesShortQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "index.sqlite3"
        build_database(_corpus(), self.path)

    def passages(self, query: str, *, prefilter: bool = True, **kwargs) -> dict:
        engine = SearchEngine(self.path)
        try:
            disabled = patch("src.me_finder.search.short_gram_prefilter_ready", return_value=False)
            with contextlib.nullcontext() if prefilter else disabled:
                return engine.search_passages(query, **kwargs)
        finally:
            engine.close()

    @staticmethod
    def ids(response: dict) -> list:
        return [item["paragraph_id"] for item in response["results"]]

    def test_two_character_query_finds_passages_on_sqlite(self) -> None:
        response = self.passages("社会")
        # A two-character query is a single bigram, so every hit ties on
        # overlap and falls back to reading order (source, paragraph index).
        self.assertEqual(self.ids(response), ["P-0002", "P-0005", "P-0000", "P-0003"])
        self.assertEqual(response["total"], 4)
        self.assertTrue(response["total_is_exact"])
        self.assertFalse(response["has_more"])

    def test_filters_apply_to_short_query_scan(self) -> None:
        self.assertEqual(self.ids(self.passages("社会", source_type="epub")), ["P-0002", "P-0005"])
        self.assertEqual(self.ids(self.passages("社会", source_type="pdf")), ["P-0000", "P-0003"])
        self.assertEqual(self.ids(self.passages("社会", source_file_id="pdf-a")), ["P-0000", "P-0003"])
        self.assertEqual(self.ids(self.passages("社会", source_file_ids=["epub-c"])), ["P-0002", "P-0005"])
        self.assertEqual(self.ids(self.passages("社会", source_file_ids=[])), [])

    def test_limit_marks_more_results(self) -> None:
        response = self.passages("社会", limit=2)
        self.assertEqual(self.ids(response), ["P-0002", "P-0005"])
        self.assertEqual(response["total"], 4)
        self.assertTrue(response["has_more"])

    @unittest.skipUnless(sgi.short_gram_supported(), "short-gram FTS unavailable")
    def test_prefilter_does_not_change_results(self) -> None:
        self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), len(TEXTS))
        # All-pending fixtures admit every row even if the FTS predicate is
        # broken. Exercise the completed index and require its actual use.
        with patch("src.me_finder.persistence.passage_reads.short_gram_prefilter",
                   wraps=passage_reads.short_gram_prefilter) as prefilter:
            with_index = self.passages("社会")
        prefilter.assert_called_once_with(["社会"])
        without_index = self.passages("社会", prefilter=False)
        self.assertEqual(json.dumps(with_index, ensure_ascii=False, sort_keys=True),
                         json.dumps(without_index, ensure_ascii=False, sort_keys=True))

    @unittest.skipUnless(sgi.short_gram_supported(), "short-gram FTS unavailable")
    def test_indexed_and_pending_rows_keep_current_results(self) -> None:
        self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), len(TEXTS))
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            for pid, text in (("P-0000", "历史研究"), ("P-0001", "社会历史")):
                connection.execute(
                    "UPDATE paragraphs SET text_raw=?, normalized_text=?, compact_text=?, plain_text=? "
                    "WHERE paragraph_id=?", (text, text, text, text, pid),
                )
            connection.execute("DELETE FROM paragraphs WHERE paragraph_id='P-0002'")
        pending_response = self.passages("社会")
        self.assertEqual(self.ids(pending_response), ["P-0005", "P-0003", "P-0001"])
        self.assertEqual(pending_response, self.passages("社会", prefilter=False))
        self.assertEqual(self.ids(self.passages("社会", source_type="word")), ["P-0001"])
        self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), 3)
        self.assertEqual(self.passages("社会"), pending_response)

    def test_candidate_budget_and_ties_are_independent_of_insertion_order(self) -> None:
        for count in (64, 65):
            with self.subTest(count=count):
                corpus = _corpus()
                paragraph = corpus["paragraphs"][0]
                corpus["paragraphs"] = [
                    {**paragraph, "paragraph_id": f"P-{index:04d}", "paragraph_index": index}
                    for index in reversed(range(count))
                ]
                build_database(corpus, self.path)
                with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute("ANALYZE")
                response = self.passages("社会", limit=1)
                self.assertEqual(self.ids(response), ["P-0000"])
                self.assertEqual(response["total"], 64)
                self.assertEqual(response["total_is_exact"], count == 64)
                self.assertTrue(response["has_more"])
                self.assertEqual(response, self.passages("社会", limit=1, prefilter=False))

    def test_results_keep_location_anchors(self) -> None:
        first = self.passages("社会")["results"][0]
        self.assertEqual(first["paragraph_id"], "P-0002")
        self.assertEqual(first["source_file_id"], "epub-c")
        self.assertEqual(first["page_source_type"], "uncalibrated")
        self.assertEqual(first["relevance"]["method"], "trigram")

    def test_three_character_query_still_uses_fts(self) -> None:
        response = self.passages("社会学")
        self.assertEqual(sorted(self.ids(response)), ["P-0000", "P-0005"])


    def test_only_ranked_rows_are_parsed(self) -> None:
        # Common two-character words match most of a library; decoding every
        # hit's payload before the budget cut dominated the scan.
        corpus = _corpus()
        paragraph = corpus["paragraphs"][0]
        corpus["paragraphs"] = [
            {**paragraph, "paragraph_id": f"P-{index:04d}", "paragraph_index": index}
            for index in range(65)
        ]
        build_database(corpus, self.path)
        with patch("src.me_finder.search_recall_passages.paragraph_from_database_row",
                   wraps=paragraph_from_database_row) as parse:
            response = self.passages("社会", limit=1)
        self.assertEqual(parse.call_count, 64)
        self.assertEqual(self.ids(response), ["P-0000"])
        self.assertFalse(response["total_is_exact"])


    def test_rowid_fetch_drops_keys_that_no_longer_match(self) -> None:
        # A delete between the rank scan and the full-row fetch may free a
        # rowid; the fetch must drop that key rather than return a stranger.
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            rowids = dict(connection.execute("SELECT paragraph_id, rowid FROM paragraphs"))
            keys = [(rowids["P-0005"], "P-0005"), (rowids["P-0000"], "P-0003"),
                    (10_000, "P-9999"), (rowids["P-0002"], "P-0002")]
            rows = passage_reads.read_paragraph_rows(connection, keys)
        self.assertEqual([row["paragraph_id"] for row in rows], ["P-0005", "P-0002"])


class GramOverlapTest(unittest.TestCase):
    def test_matches_bigram_set_intersection(self) -> None:
        rng = random.Random(20260930)
        alphabet = "社会学史历"
        for _ in range(4000):
            query = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6)))
            plain = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 9)))
            grams = CandidateRecall._ngrams_set(query)
            expected = len(grams.intersection(CandidateRecall._ngrams_set(plain)))
            self.assertEqual(gram_overlap(grams, plain), expected, (query, plain))


if __name__ == "__main__":
    unittest.main()
