"""Candidate-budget boundaries for SQLite recall.

Recall caps work at ``candidate_budget = max(SQL_CANDIDATE_FLOOR, limit*8)`` by
selecting ``LIMIT budget+1`` and marking the result truncated once more than
``budget`` candidates are seen.

Two distinct coverage lanes, kept separate:

- ``ShortQuery*`` — <3-char queries that take the **non-FTS ``instr`` branch**
  (the one round-2's ``_instr_eligibility_clause`` changed). These assert the
  branch actually runs, the budget boundary (fewer than / equal / more than),
  and the **exact ordered candidate ids** kept after truncation — not merely a
  set or a score ordering.
- ``FtsPassBudgetTests`` — >=3-char queries that take the trigram-FTS branch,
  retained as independent budget coverage.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.search import SearchEngine
from src.me_finder.search_contract import SQL_CANDIDATE_FLOOR
from src.me_finder.search_recall import CandidateRecall

# limit=5 -> budget = max(64, 40) = 64; keep it small so fixtures stay modest.
LIMIT = 5
BUDGET = max(SQL_CANDIDATE_FLOOR, LIMIT * 8)


def _paragraph(index: int, source_id: str, text: str, eligible: bool) -> dict:
    return {
        "paragraph_id": f"{source_id}-P{index:04d}",
        "volume_id": f"VOL-{source_id}",
        "volume_number": None,
        "work_id": f"WORK-{source_id}",
        "source_file_id": source_id,
        "source_type": "pdf",
        "paragraph_index": index,
        "eligible_for_search": eligible,
        "text_raw": text,
        "normalized_text": normalize_text(text),
        "compact_text": compact_text(text),
        "plain_text": punctuationless_text(text),
        "document_title": "书",
        "work_title": "书",
        "volume_display": "书",
        "page_display": "引用页码尚未校准",
        "page_source_type": "uncalibrated",
        "pdf_page_start_index": index,
        "pdf_page_end_index": index,
        "original_file_name": f"{source_id}.pdf",
    }


def _build(matching_texts, *, second_source_matches=0, ineligible_matches=0):
    """Build a one/two-source SQLite index. Global paragraph_index == insertion
    order == rowid, so the lowest-rowid matches are the lowest-index ids."""

    temp = tempfile.TemporaryDirectory()
    database_path = Path(temp.name) / "index.sqlite3"
    sources = [{"source_file_id": "pdf-a", "source_type": "pdf", "file_name": "a.pdf"}]
    volumes = [{"volume_id": "VOL-pdf-a", "source_file_id": "pdf-a", "source_type": "pdf", "display_title": "书"}]
    works = [{"work_id": "WORK-pdf-a", "volume_id": "VOL-pdf-a", "source_type": "pdf", "title": "书"}]
    paragraphs = []
    idx = 0
    for text in matching_texts:
        paragraphs.append(_paragraph(idx, "pdf-a", text, eligible=True))
        idx += 1
    for _ in range(ineligible_matches):
        paragraphs.append(_paragraph(idx, "pdf-a", matching_texts[0], eligible=False))
        idx += 1
    for _ in range(3):
        paragraphs.append(_paragraph(idx, "pdf-a", "毫不相干的文本。", eligible=True))
        idx += 1
    if second_source_matches:
        sources.append({"source_file_id": "pdf-b", "source_type": "pdf", "file_name": "b.pdf"})
        volumes.append({"volume_id": "VOL-pdf-b", "source_file_id": "pdf-b", "source_type": "pdf", "display_title": "乙"})
        works.append({"work_id": "WORK-pdf-b", "volume_id": "VOL-pdf-b", "source_type": "pdf", "title": "乙"})
        for _ in range(second_source_matches):
            paragraphs.append(_paragraph(idx, "pdf-b", matching_texts[0], eligible=True))
            idx += 1
    build_database(
        {"metadata": {}, "source_files": sources, "volumes": volumes, "works": works, "paragraphs": paragraphs},
        database_path,
    )
    return temp, database_path


class _RecordingConnection:
    def __init__(self, connection) -> None:
        self._connection = connection
        self.executed: list[str] = []

    def execute(self, sql, parameters=()):
        self.executed.append(sql)
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class ShortQueryInstrPathTests(unittest.TestCase):
    """<3-char queries must run the non-FTS instr branch, honour the budget, and
    return the exact lowest-rowid ids in order after truncation."""

    def _ids(self, result):
        return [hit["paragraph_id"] for hit in result["results"]]

    def test_two_char_query_takes_the_non_fts_instr_branch(self) -> None:
        temp, path = _build([f"社会问题第{i}段。" for i in range(10)])
        engine = SearchEngine(path)
        recorder = _RecordingConnection(engine.db)
        try:
            recall = CandidateRecall(
                db_provider=lambda: recorder, backend="sqlite", paragraphs=[],
                ngram_index={}, ensure_fts=lambda: True,
            )
            self.assertIsNone(recall.fts_match_expression("社会", "AND"))  # <3 chars
            candidates: dict = {}
            recall._sql_exact_pass(
                "社会", normalize_text("社会"), punctuationless_text("社会"),
                candidates, "all", None, None, recall.candidate_budget(LIMIT),
            )
            instr_sql = next(s for s in recorder.executed if "instr(" in s)
            self.assertNotIn("paragraphs_fts MATCH", instr_sql)  # not the FTS branch
            self.assertIn("+p.eligible_for_search = 1", instr_sql)  # unscoped deopt
            self.assertEqual(len(candidates), 10)
        finally:
            engine.close()
            temp.cleanup()

    def test_one_char_query_below_budget_exact_total_and_ordered_ids(self) -> None:
        temp, path = _build([f"社{i:03d}区。" for i in range(BUDGET - 1)])
        engine = SearchEngine(path)
        try:
            result = engine.search("社", mode="exact", limit=LIMIT)
        finally:
            engine.close()
            temp.cleanup()
        self.assertEqual(result["total"], BUDGET - 1)
        self.assertTrue(result["total_is_exact"])
        self.assertTrue(result["has_more"])  # more than LIMIT rendered
        # Exact ordered ids: the LIMIT lowest-index eligible matches, in order.
        self.assertEqual(
            self._ids(result), [f"pdf-a-P{i:04d}" for i in range(LIMIT)]
        )

    def test_two_char_query_exactly_budget_is_not_truncated(self) -> None:
        temp, path = _build([f"社会{i:03d}。" for i in range(BUDGET)])
        engine = SearchEngine(path)
        try:
            result = engine.search("社会", mode="exact", limit=LIMIT)
        finally:
            engine.close()
            temp.cleanup()
        self.assertEqual(result["total"], BUDGET)
        self.assertTrue(result["total_is_exact"])
        self.assertEqual(self._ids(result), [f"pdf-a-P{i:04d}" for i in range(LIMIT)])

    def test_two_char_query_above_budget_is_truncated_keeping_lowest_ids(self) -> None:
        temp, path = _build([f"社会{i:03d}。" for i in range(BUDGET + 6)])
        engine = SearchEngine(path)
        try:
            result = engine.search("社会", mode="exact", limit=LIMIT)  # budget=64
            # Recall directly at a FIXED budget of BUDGET(=64) so the candidate
            # truncation itself is tested — NOT limit=BUDGET, which would raise
            # the budget to BUDGET*8=512 and never truncate 70 candidates at 64.
            recall = CandidateRecall(
                db_provider=lambda: engine.db, backend="sqlite", paragraphs=[],
                ngram_index={}, ensure_fts=lambda: True,
            )
            candidates: dict = {}
            truncated = recall._sql_exact_pass(
                "社会", normalize_text("社会"), punctuationless_text("社会"),
                candidates, "all", None, None, BUDGET,
            )
        finally:
            engine.close()
            temp.cleanup()
        self.assertEqual(result["total"], BUDGET)
        self.assertFalse(result["total_is_exact"])
        self.assertTrue(result["has_more"])
        self.assertEqual(self._ids(result), [f"pdf-a-P{i:04d}" for i in range(LIMIT)])
        # Budget truncation keeps EXACTLY the BUDGET lowest-rowid candidates, in
        # rowid order (candidates dict preserves ORDER BY p.rowid insertion order).
        self.assertTrue(truncated)
        self.assertEqual(len(candidates), BUDGET)
        self.assertEqual(list(candidates), [f"pdf-a-P{i:04d}" for i in range(BUDGET)])

    def test_ineligible_paragraphs_are_never_recalled(self) -> None:
        temp, path = _build([f"社会{i:03d}。" for i in range(8)], ineligible_matches=5)
        engine = SearchEngine(path)
        try:
            result = engine.search("社会", mode="exact", limit=LIMIT)
        finally:
            engine.close()
            temp.cleanup()
        self.assertEqual(result["total"], 8)
        self.assertTrue(result["total_is_exact"])
        self.assertEqual(self._ids(result), [f"pdf-a-P{i:04d}" for i in range(LIMIT)])


class ShortQueryScopeTests(unittest.TestCase):
    """Short-query budget under single-document and member-set scope filters."""

    def _ids(self, result):
        return [hit["paragraph_id"] for hit in result["results"]]

    def test_single_document_scope_bounds_candidates_and_keeps_index(self) -> None:
        temp, path = _build(
            [f"社会{i:03d}。" for i in range(BUDGET + 6)], second_source_matches=4
        )
        engine = SearchEngine(path)
        recorder = _RecordingConnection(engine.db)
        try:
            recall = CandidateRecall(
                db_provider=lambda: recorder, backend="sqlite", paragraphs=[],
                ngram_index={}, ensure_fts=lambda: True,
            )
            candidates: dict = {}
            recall._sql_exact_pass(
                "社会", normalize_text("社会"), punctuationless_text("社会"),
                candidates, "all", "pdf-b", None, recall.candidate_budget(LIMIT),
            )
            instr_sql = next(s for s in recorder.executed if "instr(" in s)
            # A scoped short query keeps the index (no + deopt) — selective there.
            self.assertIn("p.eligible_for_search = 1", instr_sql)
            self.assertNotIn("+p.eligible_for_search", instr_sql)
            scoped = engine.search("社会", mode="exact", limit=LIMIT, source_file_id="pdf-b")
        finally:
            engine.close()
            temp.cleanup()
        self.assertEqual(scoped["total"], 4)  # only the 4 pdf-b matches in scope
        self.assertTrue(scoped["total_is_exact"])
        for hit in scoped["results"]:
            self.assertEqual(hit["source_file_id"], "pdf-b")

    def test_member_set_scope_bounds_candidates(self) -> None:
        temp, path = _build(
            [f"社会{i:03d}。" for i in range(BUDGET + 6)], second_source_matches=4
        )
        engine = SearchEngine(path)
        try:
            scoped = engine.search(
                "社会", mode="exact", limit=LIMIT, source_file_ids=["pdf-b"]
            )
            empty = engine.search(
                "社会", mode="exact", limit=LIMIT, source_file_ids=[]
            )
        finally:
            engine.close()
            temp.cleanup()
        self.assertEqual(scoped["total"], 4)
        self.assertTrue(all(h["source_file_id"] == "pdf-b" for h in scoped["results"]))
        # An explicit empty set matches nothing (never widened to whole library).
        self.assertEqual(empty["total"], 0)


class ShortQueryCompactPunctuationTests(unittest.TestCase):
    """The compact and punctuation instr passes also honour the budget."""

    def _run(self, texts, query, mode):
        temp, path = _build(texts)
        engine = SearchEngine(path)
        try:
            return engine.search(query, mode=mode, limit=LIMIT)
        finally:
            engine.close()
            temp.cleanup()

    def test_compact_pass_short_query_respects_budget(self) -> None:
        # "社 会" only matches after space-insensitive (compact) folding.
        result = self._run([f"社 会{i:03d}。" for i in range(BUDGET + 6)], "社会", "compact")
        self.assertEqual(result["total"], BUDGET)
        self.assertFalse(result["total_is_exact"])
        self.assertTrue(result["has_more"])

    def test_punctuation_pass_short_query_respects_budget(self) -> None:
        # "社，会" only matches after punctuation-insensitive folding.
        result = self._run([f"社，会{i:03d}。" for i in range(BUDGET + 6)], "社会", "punctuation")
        self.assertEqual(result["total"], BUDGET)
        self.assertFalse(result["total_is_exact"])
        self.assertTrue(result["has_more"])


class FtsPassBudgetTests(unittest.TestCase):
    """>=3-char queries take the trigram-FTS branch — independent budget coverage
    kept so the short-query lane never replaces it."""

    TERM = "查询词"  # 3 chars -> FTS trigram exact path

    def _search(self, n_matching, **build_kwargs):
        texts = [f"{self.TERM}第{i}段落。" for i in range(n_matching)]
        temp, path = _build(texts, **build_kwargs)
        engine = SearchEngine(path)
        recorder = _RecordingConnection(engine.db)
        try:
            recall = CandidateRecall(
                db_provider=lambda: recorder, backend="sqlite", paragraphs=[],
                ngram_index={}, ensure_fts=lambda: True,
            )
            self.assertIsNotNone(recall.fts_match_expression(self.TERM, "AND"))
            return engine.search(self.TERM, mode="exact", limit=LIMIT)
        finally:
            engine.close()
            temp.cleanup()

    def test_below_budget(self) -> None:
        result = self._search(BUDGET - 1)
        self.assertEqual(result["total"], BUDGET - 1)
        self.assertTrue(result["total_is_exact"])

    def test_at_budget(self) -> None:
        result = self._search(BUDGET)
        self.assertEqual(result["total"], BUDGET)
        self.assertTrue(result["total_is_exact"])

    def test_above_budget(self) -> None:
        result = self._search(BUDGET + 6)
        self.assertEqual(result["total"], BUDGET)
        self.assertFalse(result["total_is_exact"])
        self.assertTrue(result["has_more"])


if __name__ == "__main__":
    unittest.main()
