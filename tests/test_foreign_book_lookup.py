from __future__ import annotations

import json
import unittest

from src.me_finder.foreign_book_lookup import (
    BookLookupError,
    parse_google_books_response,
    _build_query,
)


_SAMPLE = json.dumps(
    {
        "kind": "books#volumes",
        "totalItems": 1,
        "items": [
            {
                "id": "vol1",
                "volumeInfo": {
                    "title": "Deep Learning",
                    "authors": ["Ian Goodfellow", "Yoshua Bengio", "Aaron Courville"],
                    "publisher": "MIT Press",
                    "publishedDate": "2016-11-18",
                    "industryIdentifiers": [
                        {"type": "ISBN_13", "identifier": "9780262035613"},
                        {"type": "ISBN_10", "identifier": "0262035618"},
                    ],
                    "pageCount": 800,
                    "language": "en",
                    "canonicalVolumeLink": "https://books.google.com/books/about/Deep_Learning.html",
                },
            }
        ],
    }
)


class ForeignBookLookupTests(unittest.TestCase):
    def test_isbn_query_takes_precedence(self) -> None:
        query, isbn = _build_query({"isbn": "978-0-262-03561-3", "title": "别的"})
        self.assertEqual(query, "isbn:9780262035613")
        self.assertEqual(isbn, "9780262035613")

    def test_title_author_query_when_no_isbn(self) -> None:
        query, isbn = _build_query({"title": "Deep Learning", "author": "Goodfellow"})
        self.assertEqual(isbn, "")
        self.assertIn("intitle:Deep Learning", query)
        self.assertIn("inauthor:Goodfellow", query)

    def test_parse_maps_fields_and_scores_isbn_hit_high(self) -> None:
        candidates = parse_google_books_response(
            _SAMPLE, {"title": "Deep Learning"}, "9780262035613"
        )
        self.assertEqual(len(candidates), 1)
        meta = candidates[0]["metadata"]
        self.assertEqual(meta["title"], "Deep Learning")
        self.assertEqual(meta["author"], "Ian Goodfellow, Yoshua Bengio, Aaron Courville")
        self.assertEqual(meta["publisher"], "MIT Press")
        self.assertEqual(meta["publish_year"], "2016")
        self.assertEqual(meta["isbn"], "9780262035613")
        self.assertEqual(candidates[0]["match"]["level"], "high")
        self.assertEqual(candidates[0]["evidence"]["title"]["source"], "google_books")

    def test_parse_title_only_match_is_not_forced_high(self) -> None:
        candidates = parse_google_books_response(_SAMPLE, {"title": "Deep Learning"}, "")
        self.assertEqual(len(candidates), 1)
        # 无 ISBN 佐证时，仅书名一致不应升到 high。
        self.assertIn(candidates[0]["match"]["level"], {"medium", "low"})

    def test_subtitle_is_appended_to_title(self) -> None:
        raw = json.dumps(
            {"items": [{"volumeInfo": {"title": "Critique", "subtitle": "On the Theory of Forms"}}]}
        )
        candidates = parse_google_books_response(raw, {"title": "Critique"}, "")
        self.assertEqual(candidates[0]["metadata"]["title"], "Critique: On the Theory of Forms")

    def test_empty_and_malformed_payloads_degrade_safely(self) -> None:
        self.assertEqual(parse_google_books_response('{"totalItems":0}', {"title": "x"}, ""), [])
        with self.assertRaises(BookLookupError):
            parse_google_books_response("not json", {"title": "x"}, "")


if __name__ == "__main__":
    unittest.main()
