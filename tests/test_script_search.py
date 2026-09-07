"""Tests for the script-folding dual-track search wrapper (issue #16)."""

import unittest

from src.me_finder import script_conversion
from src.me_finder.application import SearchRequest, SearchService
from src.me_finder.application.script_search import execute_with_script_folding

_FAKE_T2S = {"剩餘價值": "剩余价值", "帝國主義": "帝国主义"}
_FAKE_S2T = {value: key for key, value in _FAKE_T2S.items()}


def _fake_to_simplified(text: str) -> str:
    return _FAKE_T2S.get(text, text)


def _fake_to_traditional(text: str) -> str:
    return _FAKE_S2T.get(text, text)


def _simplified_hit() -> Dict[str, object]:
    return {"source_file_id": "f-sim", "paragraph_id": "p-sim",
            "text": "……剩余价值……", "match_start": 2}


def _traditional_hit() -> Dict[str, object]:
    return {"source_file_id": "f-tra", "paragraph_id": "p-tra",
            "text": "……剩餘價值……", "match_start": 2}


class ScriptFoldingSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: List[str] = []
        self.result_table: Dict[str, Dict[str, object]] = {}

        self._orig_is_available = script_conversion.is_available
        self._orig_to_simplified = script_conversion.to_simplified
        self._orig_to_traditional = script_conversion.to_traditional
        self._orig_query_variants = script_conversion.query_variants
        self._orig_execute = SearchService.execute

        script_conversion.is_available = lambda: True
        script_conversion.to_simplified = _fake_to_simplified
        script_conversion.to_traditional = _fake_to_traditional

        def fake_execute(engine, request):
            self.calls.append(request.query)
            return self.result_table.get(request.query, {"total": 0, "results": []})

        SearchService.execute = staticmethod(fake_execute)

    def tearDown(self) -> None:
        script_conversion.is_available = self._orig_is_available
        script_conversion.to_simplified = self._orig_to_simplified
        script_conversion.to_traditional = self._orig_to_traditional
        script_conversion.query_variants = self._orig_query_variants
        SearchService.execute = self._orig_execute

    def test_simplified_query_also_finds_traditional_paragraph(self) -> None:
        self.result_table = {
            "剩余价值": {"total": 1, "results": [_simplified_hit()]},
            "剩餘價值": {"total": 1, "results": [_traditional_hit()]},
        }
        out = execute_with_script_folding(None, SearchRequest(query="剩余价值"))
        self.assertEqual(self.calls, ["剩余价值", "剩餘價值"])
        self.assertEqual([item["paragraph_id"] for item in out["results"]],
                         ["p-sim", "p-tra"])
        self.assertEqual(out["total"], 2)

    def test_traditional_query_also_finds_simplified_paragraph(self) -> None:
        self.result_table = {
            "剩餘價值": {"total": 1, "results": [_traditional_hit()]},
            "剩余价值": {"total": 1, "results": [_simplified_hit()]},
        }
        out = execute_with_script_folding(None, SearchRequest(query="剩餘價值"))
        self.assertEqual([item["paragraph_id"] for item in out["results"]],
                         ["p-tra", "p-sim"])

    def test_duplicate_paragraph_keeps_first_occurrence(self) -> None:
        shared = {"source_file_id": "f1", "paragraph_id": "p1",
                  "text": "……剩餘價值……", "match_start": 5}
        self.result_table = {
            "剩余价值": {"total": 1, "results": [shared]},
            "剩餘價值": {"total": 1, "results": [dict(shared, match_start=0)]},
        }
        out = execute_with_script_folding(None, SearchRequest(query="剩余价值"))
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["match_start"], 5)

    def test_integer_limit_caps_merged_results(self) -> None:
        self.result_table = {
            "剩余价值": {"total": 1, "results": [_simplified_hit()]},
            "剩餘價值": {"total": 1, "results": [_traditional_hit()]},
        }
        out = execute_with_script_folding(
            None, SearchRequest(query="剩余价值", limit=1)
        )
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["paragraph_id"], "p-sim")

    def test_legacy_all_limit_passes_everything_through(self) -> None:
        self.result_table = {
            "剩余价值": {"total": 1, "results": [_simplified_hit()]},
            "剩餘價值": {"total": 1, "results": [_traditional_hit()]},
        }
        out = execute_with_script_folding(
            None, SearchRequest(query="剩余价值", limit="all")
        )
        self.assertEqual(len(out["results"]), 2)

    def test_toggle_off_is_a_plain_passthrough(self) -> None:
        self.result_table = {"剩余价值": {"total": 1, "results": [_simplified_hit()]}}
        out = execute_with_script_folding(
            None, SearchRequest(query="剩余价值"), enabled=False
        )
        self.assertEqual(self.calls, ["剩余价值"])
        self.assertEqual([item["paragraph_id"] for item in out["results"]], ["p-sim"])

    def test_non_chinese_query_runs_a_single_pass(self) -> None:
        execute_with_script_folding(None, SearchRequest(query="Capital"))
        self.assertEqual(self.calls, ["Capital"])

    def test_variant_expansion_failure_falls_back_to_plain_search(self) -> None:
        def raising(query, *, enabled=True):
            raise RuntimeError("broken converter")

        script_conversion.query_variants = raising
        self.result_table = {"剩余价值": {"total": 1, "results": [_simplified_hit()]}}
        out = execute_with_script_folding(None, SearchRequest(query="剩余价值"))
        self.assertEqual(self.calls, ["剩余价值"])
        self.assertEqual(out["total"], 1)


if __name__ == "__main__":
    unittest.main()
