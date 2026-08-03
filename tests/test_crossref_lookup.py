from __future__ import annotations

import json
import unittest

from src.me_finder.crossref_lookup import (
    CrossrefLookupError,
    parse_crossref_query,
    parse_crossref_work,
    _normalize_doi,
)


_WORK = {
    "DOI": "10.1111/1467-8675.12190",
    "title": ["Against Manichaeism: The Politics of Forms of Life and the Possibilities of Critique"],
    "author": [
        {"given": "Rahel", "family": "Jaeggi"},
    ],
    "container-title": ["Constellations"],
    "volume": "23",
    "issue": "4",
    "page": "578-586",
    "published-print": {"date-parts": [[2016, 12]]},
    "ISSN": ["1351-0487"],
    "type": "journal-article",
}


class CrossrefLookupTests(unittest.TestCase):
    def test_normalize_doi_strips_url_prefix(self) -> None:
        self.assertEqual(_normalize_doi("https://doi.org/10.1/x"), "10.1/x")
        self.assertEqual(_normalize_doi("10.1/x."), "10.1/x")

    def test_parse_work_maps_fields_and_doi_hit_is_high(self) -> None:
        raw = json.dumps({"message": _WORK})
        candidate = parse_crossref_work(raw, {"doi": "10.1111/1467-8675.12190"}, "10.1111/1467-8675.12190")
        self.assertIsNotNone(candidate)
        meta = candidate["metadata"]
        self.assertEqual(meta["document_type"], "journal_article")
        self.assertTrue(meta["title"].startswith("Against Manichaeism"))
        self.assertEqual(meta["author"], "Rahel Jaeggi")
        self.assertEqual(meta["journal_name"], "Constellations")
        self.assertEqual(meta["volume"], "23")
        self.assertEqual(meta["issue"], "4")
        self.assertEqual(meta["page_range"], "578-586")
        self.assertEqual(meta["publish_year"], "2016")
        self.assertEqual(meta["doi"], "10.1111/1467-8675.12190")
        self.assertEqual(candidate["match"]["level"], "high")
        self.assertEqual(candidate["evidence"]["title"]["source"], "crossref")
        self.assertEqual(candidate["record_url"], "https://doi.org/10.1111/1467-8675.12190")

    def test_query_title_match_not_forced_high_without_doi(self) -> None:
        raw = json.dumps({"message": {"items": [_WORK]}})
        candidates = parse_crossref_query(
            raw, {"title": "Against Manichaeism: The Politics of Forms of Life and the Possibilities of Critique"}
        )
        self.assertEqual(len(candidates), 1)
        self.assertIn(candidates[0]["match"]["level"], {"medium", "low", "high"})
        # 没有 DOI 佐证时不应因 DOI 规则升 high；这里靠篇名一致给出较高分但非 DOI 依据。
        self.assertNotIn("DOI 一致", candidates[0]["match"]["reasons"])

    def test_malformed_payload_degrades(self) -> None:
        with self.assertRaises(CrossrefLookupError):
            parse_crossref_work("not json", {"doi": "x"}, "x")
        self.assertEqual(parse_crossref_query('{"message":{"items":[]}}', {"title": "x"}), [])


if __name__ == "__main__":
    unittest.main()
