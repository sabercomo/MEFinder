from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.me_finder import book_metadata_lookup as bml
from src.me_finder.book_metadata_lookup import (
    lookup_book,
    parse_k10plus_marcxml,
    parse_marcxml,
    parse_openlibrary_isbn,
    parse_openlibrary_search,
    _build_cql,
)
from src.me_finder.foreign_book_lookup import BookLookupError


_SRU = """<?xml version="1.0" encoding="UTF-8"?>
<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">
<zs:numberOfRecords>1</zs:numberOfRecords>
<zs:records><zs:record><zs:recordData>
<record xmlns="http://www.loc.gov/MARC21/slim">
  <datafield tag="020" ind1=" " ind2=" "><subfield code="a">3518290681</subfield><subfield code="9">3-518-29068-1</subfield></datafield>
  <datafield tag="100" ind1="1" ind2=" "><subfield code="a">Kalb, Christof</subfield><subfield code="d">1963-</subfield><subfield code="4">aut</subfield></datafield>
  <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Desintegration</subfield><subfield code="b">Studien zu Friedrich Nietzsches Leib- und Sprachphilosophie</subfield><subfield code="c">Christof Kalb</subfield></datafield>
  <datafield tag="264" ind1=" " ind2="1"><subfield code="a">Frankfurt am Main</subfield><subfield code="b">Suhrkamp</subfield><subfield code="c">2000</subfield></datafield>
</record>
</zs:recordData></zs:record></zs:records>
</zs:searchRetrieveResponse>"""


class BookMetadataLookupTests(unittest.TestCase):
    def test_isbn_cql_takes_precedence(self) -> None:
        query, isbn = _build_cql({"isbn": "978-3-518-29068-2", "title": "别的"})
        self.assertEqual(query, "pica.isb=9783518290682")
        self.assertEqual(isbn, "9783518290682")

    def test_title_cql_when_no_isbn(self) -> None:
        query, isbn = _build_cql({"title": "Desintegration"})
        self.assertEqual(isbn, "")
        self.assertEqual(query, 'pica.tit="Desintegration"')

    def test_parse_marcxml_maps_book_fields(self) -> None:
        candidates = parse_k10plus_marcxml(_SRU, {"isbn": "3518290681"}, "3518290681")
        self.assertEqual(len(candidates), 1)
        meta = candidates[0]["metadata"]
        self.assertEqual(
            meta["title"],
            "Desintegration: Studien zu Friedrich Nietzsches Leib- und Sprachphilosophie",
        )
        self.assertEqual(meta["author"], "Christof Kalb")  # 姓,名 → 名 姓
        self.assertEqual(meta["publisher"], "Suhrkamp")
        self.assertEqual(meta["publish_place"], "Frankfurt am Main")
        self.assertEqual(meta["publish_year"], "2000")
        self.assertEqual(meta["isbn"], "3518290681")
        self.assertEqual(candidates[0]["match"]["level"], "high")  # ISBN 命中
        self.assertEqual(candidates[0]["evidence"]["title"]["source"], "k10plus")

    def test_empty_result_returns_no_candidates(self) -> None:
        empty = (
            '<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">'
            "<zs:numberOfRecords>0</zs:numberOfRecords></zs:searchRetrieveResponse>"
        )
        self.assertEqual(parse_k10plus_marcxml(empty, {"isbn": "x"}, "x"), [])

    def test_loc_marcxml_is_tagged_with_loc_source(self) -> None:
        candidates = parse_marcxml(
            _SRU, {"isbn": "3518290681"}, "3518290681", source_key="loc", source_label="LoC（美国国会图书馆）"
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["evidence"]["title"]["source"], "loc")
        self.assertIn("LoC", candidates[0]["evidence"]["title"]["evidence_text"])


class OpenLibraryParseTests(unittest.TestCase):
    _ISBN = json.dumps(
        {
            "ISBN:9783518290682": {
                "title": "Desintegration",
                "subtitle": "Studien zu Nietzsche",
                "authors": [{"name": "Christof Kalb"}],
                "publishers": [{"name": "Suhrkamp"}],
                "publish_places": [{"name": "Frankfurt am Main"}],
                "publish_date": "2000",
                "identifiers": {"isbn_13": ["9783518290682"], "isbn_10": ["3518290681"]},
                "url": "https://openlibrary.org/books/OL1M/Desintegration",
            }
        }
    )
    _SEARCH = json.dumps(
        {
            "docs": [
                {
                    "title": "Desintegration",
                    "author_name": ["Christof Kalb"],
                    "first_publish_year": 2000,
                    "publisher": ["Suhrkamp", "Suhrkamp Verlag"],
                    "isbn": ["3518290681", "9783518290682"],
                    "key": "/works/OL1W",
                }
            ]
        }
    )

    def test_isbn_endpoint_maps_fields(self) -> None:
        candidates = parse_openlibrary_isbn(self._ISBN, {"isbn": "9783518290682"}, "9783518290682")
        self.assertEqual(len(candidates), 1)
        meta = candidates[0]["metadata"]
        self.assertEqual(meta["title"], "Desintegration: Studien zu Nietzsche")
        self.assertEqual(meta["author"], "Christof Kalb")
        self.assertEqual(meta["publisher"], "Suhrkamp")
        self.assertEqual(meta["publish_place"], "Frankfurt am Main")
        self.assertEqual(meta["publish_year"], "2000")
        self.assertEqual(meta["isbn"], "9783518290682")
        self.assertEqual(candidates[0]["match"]["level"], "high")  # ISBN 命中
        self.assertEqual(candidates[0]["evidence"]["title"]["source"], "open_library")

    def test_search_endpoint_maps_fields(self) -> None:
        candidates = parse_openlibrary_search(self._SEARCH, {"title": "Desintegration"}, "")
        self.assertEqual(len(candidates), 1)
        meta = candidates[0]["metadata"]
        self.assertEqual(meta["title"], "Desintegration")
        self.assertEqual(meta["author"], "Christof Kalb")
        self.assertEqual(meta["publisher"], "Suhrkamp")  # 只取第一个出版社
        self.assertEqual(meta["publish_year"], "2000")
        self.assertEqual(candidates[0]["record_url"], "https://openlibrary.org/works/OL1W")

    def test_malformed_json_degrades_to_error(self) -> None:
        with self.assertRaises(BookLookupError):
            parse_openlibrary_isbn("not json", {"isbn": "x"}, "x")


class LookupBookOrderingTests(unittest.TestCase):
    """lookup_book 的多源顺序与降级语义。"""

    def _cand(self, source: str) -> dict:
        return {"metadata": {"title": source}, "match": {"score": 0.9, "level": "medium"},
                "record_url": "", "publish_date": "", "evidence": {}}

    def test_open_library_is_tried_first_and_google_last(self) -> None:
        calls = []
        def ol(_m):
            calls.append("ol"); return [self._cand("ol")]
        def k10(_m):
            calls.append("k10"); return [self._cand("k10")]
        def loc(_m):
            calls.append("loc"); return [self._cand("loc")]
        def g(_m):
            calls.append("google"); return [self._cand("google")]
        with patch.object(bml, "lookup_open_library", ol), patch.object(bml, "lookup_k10plus", k10), \
             patch.object(bml, "lookup_loc", loc), patch.object(bml, "_google_books_candidates", g):
            result = lookup_book({"isbn": "9783518290682"})
        self.assertEqual(calls, ["ol"])  # 首源命中即返回，不再触碰后续源
        self.assertEqual(result["candidates"][0]["metadata"]["title"], "ol")

    def test_falls_through_to_next_source_when_empty(self) -> None:
        with patch.object(bml, "lookup_open_library", lambda _m: []), \
             patch.object(bml, "lookup_k10plus", lambda _m: [self._cand("k10")]), \
             patch.object(bml, "lookup_loc", lambda _m: [self._cand("loc")]), \
             patch.object(bml, "_google_books_candidates", lambda _m: []):
            result = lookup_book({"isbn": "9783518290682"})
        self.assertEqual(result["candidates"][0]["metadata"]["title"], "k10")

    def test_network_error_does_not_block_a_later_hit(self) -> None:
        def ol(_m):
            raise BookLookupError("timeout", "连接失败")
        with patch.object(bml, "lookup_open_library", ol), \
             patch.object(bml, "lookup_k10plus", lambda _m: [self._cand("k10")]), \
             patch.object(bml, "lookup_loc", lambda _m: []), \
             patch.object(bml, "_google_books_candidates", lambda _m: []):
            result = lookup_book({"isbn": "9783518290682"})
        self.assertEqual(result["candidates"][0]["metadata"]["title"], "k10")

    def test_all_sources_offline_raises_network_error(self) -> None:
        def boom(_m):
            raise BookLookupError("timeout", "连接失败")
        with patch.object(bml, "lookup_open_library", boom), patch.object(bml, "lookup_k10plus", boom), \
             patch.object(bml, "lookup_loc", boom), patch.object(bml, "_google_books_candidates", boom):
            with self.assertRaises(BookLookupError) as ctx:
                lookup_book({"isbn": "9783518290682"})
        self.assertEqual(ctx.exception.code, "timeout")

    def test_reachable_but_no_match_returns_empty_not_error(self) -> None:
        with patch.object(bml, "lookup_open_library", lambda _m: []), patch.object(bml, "lookup_k10plus", lambda _m: []), \
             patch.object(bml, "lookup_loc", lambda _m: []), patch.object(bml, "_google_books_candidates", lambda _m: []):
            result = lookup_book({"isbn": "9783518290682"})
        self.assertEqual(result["candidates"], [])


if __name__ == "__main__":
    unittest.main()
