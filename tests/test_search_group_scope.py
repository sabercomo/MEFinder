"""Full-text search DocumentGroup scope (C3).

The search engine only ever receives source_file_id / source_file_ids — it never
knows about DocumentGroups. These tests verify the set scope on BOTH engine
backends (sqlite/FTS and in-memory/json), the None-vs-empty distinction, the
single/set mutual exclusion, and the request DTO parsing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.me_finder.application.search_service import SearchRequest
from src.me_finder.database import build_database
from src.me_finder.search import SearchEngine

QUERY = "相互承认"


def _paragraph(source_id: str) -> dict:
    text = "在相互承认的斗争中，" + source_id
    return {
        "paragraph_id": source_id + "-p0",
        "volume_id": source_id + "-v",
        "volume_number": 1,
        "work_id": source_id + "-w",
        "source_file_id": source_id,
        "source_type": "pdf",
        "paragraph_index": 0,
        "eligible_for_search": True,
        "text_raw": text,
        "normalized_text": text,
        "compact_text": text,
        "plain_text": text,
    }


def _index(source_ids) -> dict:
    return {
        "metadata": {},
        "source_files": [
            {"source_file_id": s, "source_type": "pdf", "file_name": s + ".pdf", "title": s}
            for s in source_ids
        ],
        "volumes": [
            {"volume_id": s + "-v", "source_file_id": s, "source_type": "pdf"}
            for s in source_ids
        ],
        "works": [],
        "paragraphs": [_paragraph(s) for s in source_ids],
        "page_anchors": [],
        "pdf_pages": [],
        "pdf_page_mappings": [],
    }


class _EngineScopeContract:
    """Shared assertions run against both backends."""

    def _engine(self, source_ids) -> SearchEngine:
        raise NotImplementedError

    def _sources(self, **kwargs) -> set:
        result = self._engine(["s1", "s2", "s3"]).search(QUERY, **kwargs)
        # results carry the matched source; fall back to total when absent.
        ids = set()
        for row in result.get("results", []):
            sid = row.get("source_file_id") or (row.get("paragraph") or {}).get("source_file_id")
            if sid:
                ids.add(sid)
        return ids, result["total"]

    def test_no_scope_searches_all(self) -> None:
        _, total = self._sources()
        self.assertEqual(total, 3)

    def test_single_source_file_id_regression(self) -> None:
        _, total = self._sources(source_file_id="s1")
        self.assertEqual(total, 1)

    def test_set_scope_two_sources(self) -> None:
        _, total = self._sources(source_file_ids=["s1", "s2"])
        self.assertEqual(total, 2)

    def test_set_scope_single_member(self) -> None:
        _, total = self._sources(source_file_ids=["s2"])
        self.assertEqual(total, 1)

    def test_empty_set_scope_matches_nothing(self) -> None:
        _, total = self._sources(source_file_ids=[])
        self.assertEqual(total, 0)

    def test_none_scope_is_not_empty_scope(self) -> None:
        _, total = self._sources(source_file_ids=None)
        self.assertEqual(total, 3)


class FtsBackendScopeTests(_EngineScopeContract, unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self._engines = []

    def tearDown(self) -> None:
        # Close SQLite handles before removing the temp dir (Windows file locks).
        for engine in self._engines:
            try:
                engine.close()
            except Exception:
                pass
        self._dir.cleanup()

    def _engine(self, source_ids) -> SearchEngine:
        db = Path(self._dir.name) / "index.sqlite3"
        build_database(_index(source_ids), db)
        engine = SearchEngine(db)
        self._engines.append(engine)
        return engine


class InMemoryBackendScopeTests(_EngineScopeContract, unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _engine(self, source_ids) -> SearchEngine:
        path = Path(self._dir.name) / "index.json"
        path.write_text(json.dumps(_index(source_ids), ensure_ascii=False), encoding="utf-8")
        return SearchEngine(path)


class SearchRequestScopeTests(unittest.TestCase):
    def test_source_file_ids_parsed_to_tuple(self) -> None:
        request = SearchRequest.from_payload({"query": "x", "source_file_ids": ["a", "b"]})
        self.assertEqual(request.source_file_ids, ("a", "b"))
        self.assertIsNone(request.source_file_id)

    def test_empty_list_is_empty_scope_not_none(self) -> None:
        request = SearchRequest.from_payload({"query": "x", "source_file_ids": []})
        self.assertEqual(request.source_file_ids, ())
        self.assertIsNotNone(request.source_file_ids)

    def test_absent_is_none(self) -> None:
        request = SearchRequest.from_payload({"query": "x"})
        self.assertIsNone(request.source_file_ids)

    def test_single_and_set_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ValueError):
            SearchRequest.from_payload(
                {"query": "x", "source_file_id": "a", "source_file_ids": ["b"]}
            )

    def test_non_list_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SearchRequest.from_payload({"query": "x", "source_file_ids": "a"})


if __name__ == "__main__":
    unittest.main()
