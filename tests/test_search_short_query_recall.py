"""Recall path for high-frequency short (<3 char) queries.

Short queries have no trigram MATCH, so recall falls back to an ``instr``
substring scan ordered ``BY p.rowid``. On a whole-library search this must walk
the table in rowid order (so ``LIMIT budget+1`` can stop early) instead of
funnelling every eligible row through a temp B-tree; a scoped search must keep
the selective ``idx_paragraphs_searchable`` index. Both must return identical
rows — only the access path differs. See
reports/performance-short-query-recall-2026-09-12.md.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.search import SearchEngine
from src.me_finder.search_recall import CandidateRecall


class InstrEligibilityClauseTests(unittest.TestCase):
    def _recall(self) -> CandidateRecall:
        return CandidateRecall(
            db_provider=lambda: None,
            backend="sqlite",
            paragraphs=[],
            ngram_index={},
            ensure_fts=lambda: False,
        )

    def test_whole_library_search_suppresses_the_eligibility_index(self) -> None:
        clause = self._recall()._instr_eligibility_clause("all", None, None)
        self.assertEqual(clause, "+p.eligible_for_search = 1")

    def test_scoped_searches_keep_the_eligibility_index(self) -> None:
        recall = self._recall()
        # source_type filter, single-document filter, and an explicit member set
        # are all selective; none may suppress the index.
        self.assertEqual(
            recall._instr_eligibility_clause("pdf", None, None),
            "p.eligible_for_search = 1",
        )
        self.assertEqual(
            recall._instr_eligibility_clause("all", "pdf-a", None),
            "p.eligible_for_search = 1",
        )
        self.assertEqual(
            recall._instr_eligibility_clause("all", None, frozenset({"pdf-a"})),
            "p.eligible_for_search = 1",
        )


class _RecordingConnection:
    """Wraps a real sqlite3 connection and records executed SQL text."""

    def __init__(self, connection) -> None:
        self._connection = connection
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, parameters=()):
        self.executed.append((sql, parameters))
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class ShortQueryRecallPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        database_path = Path(cls._temp.name) / "index.sqlite3"
        sources = [
            {"source_file_id": "pdf-a", "source_type": "pdf", "file_name": "a.pdf"},
            {"source_file_id": "word-b", "source_type": "word", "file_name": "b.docx"},
        ]
        volumes = [
            {"volume_id": "VOL-A", "source_file_id": "pdf-a", "source_type": "pdf", "display_title": "甲书"},
            {"volume_id": "VOL-B", "source_file_id": "word-b", "source_type": "word", "display_title": "乙书"},
        ]
        works = [
            {"work_id": "WORK-A", "volume_id": "VOL-A", "source_type": "pdf", "title": "甲书"},
            {"work_id": "WORK-B", "volume_id": "VOL-B", "source_type": "word", "title": "乙书"},
        ]
        paragraphs = []
        # Every third paragraph contains the two-char term so it appears across
        # the whole rowid range (an early-terminating scan must still find it).
        for index in range(60):
            has_term = index % 3 == 0
            source_id = "pdf-a" if index % 2 == 0 else "word-b"
            source_type = "pdf" if source_id == "pdf-a" else "word"
            volume_id = "VOL-A" if source_id == "pdf-a" else "VOL-B"
            work_id = "WORK-A" if source_id == "pdf-a" else "WORK-B"
            raw = ("社会与个体第 %d 段。" % index) if has_term else ("无关文本第 %d 段。" % index)
            paragraphs.append(
                {
                    "paragraph_id": f"P-{index:03d}",
                    "volume_id": volume_id,
                    "volume_number": None,
                    "work_id": work_id,
                    "source_file_id": source_id,
                    "source_type": source_type,
                    "paragraph_index": index,
                    "eligible_for_search": True,
                    "text_raw": raw,
                    "normalized_text": normalize_text(raw),
                    "compact_text": compact_text(raw),
                    "plain_text": punctuationless_text(raw),
                    "document_title": "甲书" if source_type == "pdf" else "乙书",
                    "work_title": "甲书" if source_type == "pdf" else "乙书",
                    "volume_display": "甲书" if source_type == "pdf" else "乙书",
                    "page_display": "引用页码尚未校准",
                    "page_source_type": "uncalibrated",
                    "pdf_page_start_index": index if source_type == "pdf" else None,
                    "pdf_page_end_index": index if source_type == "pdf" else None,
                    "original_file_name": f"{source_id}.pdf",
                }
            )
        index_payload = {
            "metadata": {},
            "source_files": sources,
            "volumes": volumes,
            "works": works,
            "paragraphs": paragraphs,
        }
        build_database(index_payload, database_path)
        cls.database_path = database_path
        cls.expected_all = {p["paragraph_id"] for p in paragraphs if "社会" in p["text_raw"]}
        cls.expected_pdf = {
            p["paragraph_id"]
            for p in paragraphs
            if "社会" in p["text_raw"] and p["source_type"] == "pdf"
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def _run_recall(self, source_type: str):
        engine = SearchEngine(self.database_path)
        recorder = _RecordingConnection(engine.db)
        try:
            recall = CandidateRecall(
                db_provider=lambda: recorder,
                backend="sqlite",
                paragraphs=[],
                ngram_index={},
                ensure_fts=lambda: False,  # force the non-FTS instr path
            )
            candidates: dict = {}
            recall._sql_exact_pass(
                "社会", normalize_text("社会"), punctuationless_text("社会"),
                candidates, source_type, None, None, recall.candidate_budget(10),
            )
            instr_sql, instr_args = next(
                (sql, args) for sql, args in recorder.executed if "instr(" in sql
            )
            plan = " | ".join(
                str(row[-1])
                for row in engine.db.execute(
                    "EXPLAIN QUERY PLAN " + instr_sql, instr_args
                )
            )
            return set(candidates), instr_sql, plan
        finally:
            engine.close()

    def test_whole_library_short_query_scans_by_rowid_without_temp_btree(self) -> None:
        found, sql, plan = self._run_recall("all")
        self.assertEqual(found, self.expected_all, "recall must return every match")
        self.assertIn("+p.eligible_for_search = 1", sql)
        # The unary + suppresses idx_paragraphs_searchable, so ORDER BY p.rowid
        # is satisfied by the table scan itself and LIMIT can stop early.
        self.assertNotIn("TEMP B-TREE", plan.upper())

    def test_scoped_short_query_keeps_the_index(self) -> None:
        found, sql, _plan = self._run_recall("pdf")
        self.assertEqual(found, self.expected_pdf)
        self.assertIn("p.eligible_for_search = 1", sql)
        self.assertNotIn("+p.eligible_for_search", sql)


if __name__ == "__main__":
    unittest.main()
