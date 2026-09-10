"""Correctness gates for the benchmark; never assert machine-dependent timings."""

import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.bench_responsiveness import compare_results, distribution, overlaps, result_identity, summarize
from scripts.performance_fixture import create_fixture, QUERIES
from src.me_finder.application.script_search import execute_with_script_folding
from src.me_finder.search import SearchEngine
from src.me_finder.application.search_service import SearchRequest


class PerformanceMeasurementTests(unittest.TestCase):
    def test_nearest_rank_percentiles_include_the_tail_for_small_samples(self):
        self.assertEqual(distribution([1, 2, 3, 4, 100]),
                         {"n": 5, "p50": 3, "p95": 100, "p99": 100, "max": 100})
        with self.assertRaises(ValueError):
            distribution([])

    def test_queued_or_finished_work_is_not_counted_as_overlap(self):
        events = [{"start": 2, "end": 4}, {"start": 6, "end": 8}]
        self.assertFalse(overlaps(0, 2, events))
        self.assertFalse(overlaps(4, 6, events))
        self.assertFalse(overlaps(8, 9, events))
        self.assertTrue(overlaps(1, 3, events))
        self.assertTrue(overlaps(3, 7, events))

    def test_identity_detects_anchor_changes_but_ignores_elapsed_time(self):
        result = {"total": 1, "results": [{"paragraph_id": "p", "match_start": 2,
                                          "match_end": 4, "page_match_spans": [{"page_char_start": 9}]}]}
        changed = copy.deepcopy(result)
        changed["elapsed_ms"] = 7
        self.assertEqual(result_identity(result), result_identity(changed))
        changed["results"][0]["page_match_spans"][0]["page_char_start"] = 10
        self.assertNotEqual(result_identity(result), result_identity(changed))

    def test_fast_503_is_not_misreported_as_a_fast_success(self):
        run = {"scenario": "alignment", "startup_ms": 10, "shutdown_ms": 20,
               "peak_rss_bytes": 1024, "peak_tree_rss_bytes": 1024,
               "work_peak_rss_bytes": 1024, "os_peak_rss_bytes": 1024,
               "samples": [
                   {"query_id": "q", "latency_ms": 100, "status": 200, "correct": True, "overlap": True},
                   {"query_id": "q", "latency_ms": 1, "status": 503, "correct": None, "overlap": True},
                   {"query_id": "q", "latency_ms": 3, "status": 200, "correct": True, "overlap": False},
               ]}
        result = summarize([run])["alignment"]
        self.assertEqual(result["search_ms"]["p50"], 100)
        self.assertEqual(result["failure_ms"]["p50"], 1)
        self.assertEqual(result["error_rate"], .5)
        self.assertEqual(result["identity_mismatches"], 0)
        self.assertEqual(result["covered_query_ids"], ["q"])

    def test_comparison_rejects_different_data_protocol_environment_or_failed_runs(self):
        baseline = {"protocol_version": 1, "harness_sha256": "script", "configuration": {"repeats": 5},
                    "environment": {"python": "3.12"}, "model_files": {"model": "sha"},
                    "fixture": {"content_sha256": "data", "queries": QUERIES},
                    "valid": True, "summary": {}, "rounds": [{"warmup_identities": {"q": "sha"}}]}
        self.assertEqual(compare_results(baseline, copy.deepcopy(baseline)), {})
        for key in ("protocol_version", "harness_sha256", "configuration", "environment", "model_files", "valid"):
            with self.subTest(key=key):
                changed = copy.deepcopy(baseline)
                changed[key] = None
                with self.assertRaises(ValueError):
                    compare_results(baseline, changed)
        changed = copy.deepcopy(baseline)
        changed["fixture"]["content_sha256"] = "other data"
        with self.assertRaises(ValueError):
            compare_results(baseline, changed)
        changed = copy.deepcopy(baseline)
        changed["rounds"][0]["warmup_identities"]["q"] = "different anchors"
        with self.assertRaisesRegex(ValueError, "search results or anchors"):
            compare_results(baseline, changed)


class PerformanceFixtureTests(unittest.TestCase):
    def test_fixture_is_reproducible_and_real_search_preserves_pdf_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = create_fixture(root / "a", documents=2, paragraphs=12, alignment_paragraphs=8)
            second = create_fixture(root / "b", documents=2, paragraphs=12, alignment_paragraphs=8)
            self.assertEqual(first, second)
            self.assertEqual(first["paragraphs"], 40)
            db = root / "a/data/index.sqlite3"
            engine = SearchEngine(db)
            try:
                for query in QUERIES:
                    with self.subTest(query=query["id"]):
                        result = execute_with_script_folding(
                            engine, SearchRequest(**{k: v for k, v in query.items() if k != "id"}), enabled=True,
                        )
                        self.assertEqual(result["total"] == 0, query["id"] == "no_hit")
                        for row in result["results"]:
                            self.assertLess(row["match_start"], row["match_end"])
                            if row["source_type"] == "pdf":
                                self.assertTrue(row["page_match_spans"])
                                self.assertIn("pdf_page_id", row["page_match_spans"][0])
            finally:
                engine.close()
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT count(*) FROM document_group_members").fetchone()[0], 2)
                source = json.loads(connection.execute(
                    "SELECT payload_json FROM source_files WHERE source_file_id='bench-002'"
                ).fetchone()[0])
                self.assertEqual(source["file_format"], "epub")

    def test_rejects_invalid_fixture_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_fixture(Path(directory), documents=1)


if __name__ == "__main__":
    unittest.main()
