"""The A/B search comparator must catch a change to ANY response field.

The previous canonicaliser used a ~10-field whitelist, so a change to
citation_formats / copy_text / PDF page index / matched_text (and more) slipped
through silently. These tests mutate each such field on a full response and
require the canonical comparison to differ — no whitelist, no field renaming,
no default-filling, no rounding may hide a real change.
"""

from __future__ import annotations

import copy
import json
import unittest

from scripts.ab_search_compare import canonical_response


def _digest(response: dict) -> str:
    return json.dumps(canonical_response(response), sort_keys=True, ensure_ascii=False)


def _full_hit() -> dict:
    # A realistic subset of the 57 product fields, including the ones the old
    # whitelist dropped.
    return {
        "paragraph_id": "p0", "source_file_id": "book", "match_type": "exact",
        "match_score": 1.0, "match_start": 3, "match_end": 5,
        "matched_text": "社会", "match_quote": "……社会……",
        "page": "第 1 页", "page_match_spans": [{"page_index": 0, "start": 3, "end": 5}],
        "pdf_page_start_index": 8, "pdf_page_end_index": 8,
        "pdf_page_start_label": "1", "pdf_page_end_label": "1",
        "citation_page_start": "1", "citation_page_end": "1",
        "context_before": [{"text": "前文"}], "context_after": [{"text": "后文"}],
        "highlighted_html": "……<mark>社会</mark>……",
        "copy_text": "社会（书, 第 1 页）",
        "citation_formats": {"gbt7714": "作者. 书. 出版社, 2020: 1.", "apa": "Author (2020)."},
        "paragraph_text": "这一段讨论社会问题。",
    }


def _response(hits=None, **top) -> dict:
    base = {
        "query": "社会", "mode": "exact", "source_type": "all", "source_file_id": None,
        "total": len(hits) if hits is not None else 1, "total_is_exact": True,
        "has_more": False, "return_all": False,
        "index_metadata": {"app": "ME_Finder", "built_at": "2026-08-24T13:37:51+00:00"},
        "results": hits if hits is not None else [_full_hit()],
    }
    base.update(top)
    return base


class FieldSensitivityTests(unittest.TestCase):
    def test_identical_responses_match(self) -> None:
        self.assertEqual(_digest(_response()), _digest(_response()))

    def _assert_hit_field_change_is_caught(self, mutate) -> None:
        base = _response()
        other = copy.deepcopy(base)
        mutate(other["results"][0])
        self.assertNotEqual(
            _digest(base), _digest(other),
            "a change to this field was hidden by the comparator",
        )

    def test_citation_formats_change_is_caught(self) -> None:
        self._assert_hit_field_change_is_caught(
            lambda hit: hit["citation_formats"].__setitem__("apa", "DIFFERENT")
        )

    def test_copy_text_change_is_caught(self) -> None:
        self._assert_hit_field_change_is_caught(
            lambda hit: hit.__setitem__("copy_text", "改动后的复制文本")
        )

    def test_pdf_page_index_change_is_caught(self) -> None:
        self._assert_hit_field_change_is_caught(
            lambda hit: hit.__setitem__("pdf_page_start_index", 99)
        )

    def test_matched_text_change_is_caught(self) -> None:
        self._assert_hit_field_change_is_caught(
            lambda hit: hit.__setitem__("matched_text", "社會")
        )

    def test_char_range_change_is_caught(self) -> None:
        self._assert_hit_field_change_is_caught(
            lambda hit: hit.__setitem__("match_end", 6)
        )

    def test_context_change_is_caught(self) -> None:
        self._assert_hit_field_change_is_caught(
            lambda hit: hit["context_before"].append({"text": "多出的上下文"})
        )

    def test_highlighted_html_change_is_caught(self) -> None:
        self._assert_hit_field_change_is_caught(
            lambda hit: hit.__setitem__("highlighted_html", "<mark>x</mark>")
        )

    def test_page_anchor_change_is_caught(self) -> None:
        self._assert_hit_field_change_is_caught(
            lambda hit: hit["page_match_spans"][0].__setitem__("end", 9)
        )

    def test_result_order_change_is_caught(self) -> None:
        a = _full_hit()
        b = copy.deepcopy(a)
        b["paragraph_id"] = "p1"
        forward = _response(hits=[a, b])
        reversed_ = _response(hits=[b, a])
        self.assertNotEqual(_digest(forward), _digest(reversed_))

    def test_total_change_is_caught(self) -> None:
        self.assertNotEqual(_digest(_response()), _digest(_response(total=80)))

    def test_total_is_exact_change_is_caught(self) -> None:
        self.assertNotEqual(
            _digest(_response()), _digest(_response(total_is_exact=False))
        )

    def test_index_metadata_change_is_caught(self) -> None:
        other = _response()
        other["index_metadata"]["built_at"] = "1999-01-01T00:00:00+00:00"
        self.assertNotEqual(_digest(_response()), _digest(other))


if __name__ == "__main__":
    unittest.main()
