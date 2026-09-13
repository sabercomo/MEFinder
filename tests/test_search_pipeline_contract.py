"""Search pipeline contract: golden identity over the fixed public test set.

The engine output for a fixed corpus and fixed queries is captured before the
search pipeline refactor and committed as ``tests/fixtures/search_pipeline_golden.json``.
The refactor must keep hits, order, deduplication, page numbers and character
spans byte-identical; the golden file is regenerated only when the product
contract changes intentionally (see the module ``__main__`` regenerator).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.performance_fixture import create_fixture
from src.me_finder.application.script_search import execute_with_script_folding
from src.me_finder.application.search_service import SearchRequest
from src.me_finder.search import SearchEngine

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "search_pipeline_golden.json"

# The performance-baseline query set plus every match path the pipeline owns:
# exact, normalized (script folding), compact (whitespace insensitive),
# punctuation insensitive, fuzzy, scoping (facet / single source / set scope),
# return_all, no-hit and the relevance (non-verbatim) passage endpoint.
QUERIES = [
    {"id": "zh_exact", "query": "青铜指南针记录了这次独特的观察", "mode": "exact"},
    {"id": "en_normalized", "query": "THE VIOLET COMPASS MARKS A UNIQUE OBSERVATION", "mode": "exact"},
    {"id": "script_variant", "query": "圖書館保留獨特的閱讀記錄", "mode": "auto", "endpoint": "script_folding"},
    {"id": "en_exact", "query": "The violet compass marks a unique observation", "mode": "exact"},
    {"id": "common_zh", "query": "社会", "mode": "auto"},
    {"id": "common_zh_all", "query": "社会", "mode": "auto", "limit": "all"},
    {"id": "common_en", "query": "social relations", "mode": "auto"},
    {"id": "normalized", "query": "青铜指南针，记录了这次独特的观察", "mode": "auto"},
    {"id": "punctuation_mode", "query": "第0001项观察，讨论社会及其历史条件", "mode": "punctuation"},
    {"id": "compact_mode", "query": "Observation0002considers social relations", "mode": "compact"},
    {"id": "fuzzy_mode", "query": "青铜指南针记录了这次独特滴观察", "mode": "fuzzy"},
    {"id": "fuzzy_auto", "query": "Observation 0003 considers socal relations and historical knwledge", "mode": "auto"},
    {"id": "no_hit", "query": "不存在的橙色卫星档案XYZ", "mode": "exact"},
    {"id": "scoped_pdf", "query": "社会", "mode": "auto", "source_type": "pdf"},
    {"id": "single_source", "query": "社会", "mode": "auto", "source_file_id": "bench-000"},
    {"id": "set_scope", "query": "社会", "mode": "auto", "source_file_ids": ["bench-000", "bench-001"]},
    {"id": "empty_set_scope", "query": "社会", "mode": "auto", "source_file_ids": []},
    {"id": "passage_relevance", "query": "社会制度 历史知识", "endpoint": "search_passages"},
]
QUERIES = [{**{"limit": 10, "source_type": "all"}, **query} for query in QUERIES]


def build_engine() -> SearchEngine:
    """Create the deterministic public fixture and open a search engine on it."""

    temporary = tempfile.TemporaryDirectory(prefix="mefinder-search-contract-")
    fixture_root = Path(temporary.name)
    create_fixture(fixture_root, documents=6, paragraphs=120, alignment_paragraphs=8)
    engine = SearchEngine(fixture_root / "data" / "index.sqlite3")
    engine._contract_temporary = temporary  # keep alive with the engine
    return engine


def capture(engine: SearchEngine) -> dict:
    """Run every fixed query and keep the complete, order-sensitive payload."""

    captured = {"queries": []}
    for spec in QUERIES:
        request = {key: value for key, value in spec.items() if key not in {"id", "endpoint"}}
        endpoint = spec.get("endpoint")
        if endpoint == "search_passages":
            response = engine.search_passages(
                request["query"], limit=request["limit"], source_type=request["source_type"]
            )
        elif endpoint == "script_folding":
            response = execute_with_script_folding(engine, SearchRequest(
                query=request["query"], mode=request["mode"], limit=request["limit"],
                source_type=request["source_type"],
            ))
        else:
            response = engine.search(
                request["query"],
                mode=request["mode"],
                limit=request["limit"],
                source_type=request["source_type"],
                source_file_id=request.get("source_file_id"),
                source_file_ids=request.get("source_file_ids"),
            )
        # ``database_built_at`` is a build timestamp, not search behaviour.
        response = {**response, "index_metadata": {
            **(response.get("index_metadata") or {}), "database_built_at": "<timestamp>",
        }}
        captured["queries"].append({"id": spec["id"], "response": response})
    return captured


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1)


class SearchPipelineContractTests(unittest.TestCase):
    """Golden guard: hits, order, dedup, pages and spans stay identical."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = build_engine()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()
        cls.engine._contract_temporary.cleanup()

    def test_engine_output_matches_golden(self) -> None:
        captured = capture(self.engine)
        golden = json.loads(GOLDEN.read_text())
        if _canonical(captured) != _canonical(golden):
            self.fail(self._first_difference(golden, captured))

    def test_every_match_path_is_covered(self) -> None:
        captured = capture(self.engine)
        by_id = {row["id"]: row["response"] for row in captured["queries"]}
        for required in (
            "exact", "normalized_exact", "space_insensitive",
            "punctuation_insensitive", "ngram_fuzzy",
        ):
            hit_types = {
                result["match_type"]
                for row in by_id.values()
                for result in row.get("results", [])
            }
            self.assertIn(required, hit_types, f"fixed test set never exercises {required}")
        self.assertGreater(by_id["common_zh"]["total"], 10)
        self.assertTrue(by_id["common_zh"]["has_more"])
        self.assertEqual(by_id["common_zh_all"]["total"], by_id["common_zh"]["total"])
        self.assertGreaterEqual(len(by_id["common_zh_all"]["results"]), by_id["common_zh"]["total"])
        self.assertEqual(by_id["no_hit"]["total"], 0)
        self.assertEqual(by_id["empty_set_scope"]["total"], 0)
        self.assertTrue(all(
            result["match_start"] < result["match_end"]
            for row in by_id.values()
            for result in row.get("results", [])
            if result["match_type"] not in {"relevance"}
        ))
        # Determinism: identical engine state must reproduce the golden digest.
        self.assertEqual(
            hashlib.sha256(_canonical(captured).encode()).hexdigest(),
            hashlib.sha256(_canonical(json.loads(GOLDEN.read_text())).encode()).hexdigest(),
        )

    @staticmethod
    def _first_difference(golden: dict, captured: dict) -> str:
        golden_rows = {row["id"]: row["response"] for row in golden["queries"]}
        captured_rows = {row["id"]: row["response"] for row in captured["queries"]}
        for query_id in golden_rows:
            if query_id not in captured_rows:
                return f"query {query_id} disappeared from the capture"
            if _canonical(golden_rows[query_id]) == _canonical(captured_rows[query_id]):
                continue
            if golden_rows[query_id].get("total") != captured_rows[query_id].get("total"):
                return (
                    f"query {query_id}: total "
                    f"{golden_rows[query_id].get('total')} -> {captured_rows[query_id].get('total')}"
                )
            before = golden_rows[query_id].get("results", [])
            after = captured_rows[query_id].get("results", [])
            for index, (old, new) in enumerate(zip(before, after)):
                if _canonical(old) != _canonical(new):
                    changed = [key for key in set(old) | set(new) if old.get(key) != new.get(key)]
                    return (
                        f"query {query_id} result #{index} changed fields {changed}: "
                        + "; ".join(
                            f"{key}: {_canonical(old.get(key))[:120]} -> {_canonical(new.get(key))[:120]}"
                            for key in changed
                        )
                    )
            return f"query {query_id}: result count {len(before)} -> {len(after)}"
        return "captured payload differs from the golden fixture"


if __name__ == "__main__":
    import sys

    if "--regen" in sys.argv:
        engine = build_engine()
        try:
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(_canonical(capture(engine)) + "\n")
            print(f"regenerated {GOLDEN}")
        finally:
            engine.close()
            engine._contract_temporary.cleanup()
    else:
        unittest.main()
