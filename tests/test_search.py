from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.me_finder.indexer import DEFAULT_INDEX_PATH, build_index
from src.me_finder.search import SearchEngine

from tests.corpus_fixtures import CORPUS_REASON, has_corpus


@unittest.skipUnless(has_corpus() or DEFAULT_INDEX_PATH.exists(), CORPUS_REASON)
class KnownQuoteSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DEFAULT_INDEX_PATH.exists():
            build_index()
        cls.engine = SearchEngine()
        cls.cases = json.loads(Path("tests/known_quotes.json").read_text(encoding="utf-8"))

    def test_known_quotes(self) -> None:
        for case in self.cases:
            with self.subTest(case=case["id"]):
                result = self.engine.search(case["query"], case.get("mode", "auto"), case.get("limit", 10))
                self.assertGreaterEqual(result["total"], case.get("min_results", 1))
                joined = "\n".join(item["paragraph_text"] for item in result["results"])
                self.assertIn(case["expected_contains"], joined)
                if "expected_volume" in case:
                    self.assertTrue(
                        any(item["volume_number"] == case["expected_volume"] for item in result["results"]),
                        f"Expected volume {case['expected_volume']} in results",
                    )
                if "expected_match_type" in case:
                    self.assertTrue(
                        any(item["match_type"] == case["expected_match_type"] for item in result["results"]),
                        f"Expected match type {case['expected_match_type']}",
                    )


if __name__ == "__main__":
    unittest.main()
