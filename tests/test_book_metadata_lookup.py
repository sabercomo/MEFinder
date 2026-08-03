from __future__ import annotations

import unittest

from src.me_finder.book_metadata_lookup import parse_k10plus_marcxml, _build_cql


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


if __name__ == "__main__":
    unittest.main()
