"""Short-query (<3 char) unigram/bigram prefilter index.

The index only narrows which paragraphs the ``instr`` scans read; every
response must stay byte-identical to the plain scan -- hits, order, per-variant
budgets, totals, truncation flags, offsets and page anchors. Triggers keep it
correct for any writer; a background drain turns queued rowids into grams.
See reports/short-query-gram-index-2026-09-29.md.
"""

from __future__ import annotations

import contextlib
import json
import random
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.performance_fixture import create_fixture
from src.me_finder.application.script_search import execute_with_script_folding
from src.me_finder.application.search_service import SearchRequest
from src.me_finder.database import build_database
from src.me_finder.normalization import compact_text, normalize_text, punctuationless_text
from src.me_finder.persistence import short_gram_index as sgi
from src.me_finder.persistence.migrations import migrate_index_database
from src.me_finder.search import SearchEngine
from src.me_finder.tasks.short_gram_backfill import run_short_gram_backfill


def _paragraph(index: int, text: str, source: str, source_type: str, *, eligible: bool = True) -> dict:
    return {
        "paragraph_id": f"P-{index:04d}", "volume_id": f"V-{source}", "work_id": f"W-{source}",
        "source_file_id": source, "source_type": source_type, "paragraph_index": index,
        "volume_number": 1, "style_name": "p", "original_page_start": str(index + 1),
        "citation_page_start": str(index + 1), "citation_page_end": str(index + 1),
        "eligible_for_search": eligible, "text_raw": text,
        "normalized_text": normalize_text(text), "compact_text": compact_text(text),
        "plain_text": punctuationless_text(text), "document_title": source,
        "work_title": source, "volume_display": source,
        "page_display": "引用页码尚未校准", "page_source_type": "uncalibrated",
        "pdf_page_start_index": index if source_type == "pdf" else None,
        "pdf_page_end_index": index if source_type == "pdf" else None,
        "source_format": "epub" if source == "epub-c" else None,
    }


def _corpus() -> dict:
    """Simplified/traditional mixes, >budget common terms, spacing/punctuation variants."""

    rng = random.Random(20260929)
    filler = "理论实践知识劳动制度生产历史"
    texts = []
    for number in range(150):
        texts.append(f"社会与个体第{number}段，" + "".join(rng.choice(filler) for _ in range(12)))
    texts += [
        "價值與价值在同一段。", "社會的歷史條件。", "社 会 被空格隔开。", "社，会被标点隔开。",
        "他住在乾清宮裡。", "後来他走了。", "麒麟出现于此。", "驃騎將軍。", "单字社在这里",
        "𠮷😀前言：剩餘價值。", "English social history and the relations.", "a,b 与 of 的短词",
    ]
    texts += ["社" + "".join(rng.choice(filler) for _ in range(8)) for _ in range(90)]
    sources = [("pdf-a", "pdf"), ("word-b", "word"), ("epub-c", "word")]
    paragraphs = []
    for index, text in enumerate(texts):
        source, source_type = sources[index % 3]
        paragraphs.append(_paragraph(index, text, source, source_type, eligible=index % 17 != 5))
    return {
        "metadata": {},
        "source_files": [
            {"source_file_id": source, "source_type": source_type, "file_name": source,
             "file_format": "epub" if source == "epub-c" else source_type}
            for source, source_type in sources
        ],
        "volumes": [{"volume_id": f"V-{s}", "source_file_id": s, "source_type": t} for s, t in sources],
        "works": [{"work_id": f"W-{s}", "volume_id": f"V-{s}", "source_type": t, "title": s} for s, t in sources],
        "paragraphs": paragraphs,
    }


QUERIES = [
    {"query": q, "mode": mode}
    for q in ("社会", "社會", "价值", "價值", "乾清", "干清", "後来", "后来", "麒麟", "驃騎",
              "社", "麟", "黠慧", "社 会", "社，会", "of", "a,", "𠮷", "😀")
    for mode in ("auto", "exact", "compact", "punctuation", "fuzzy")
] + [
    {"query": "社会", "limit": "all"},
    {"query": "社", "limit": "all"},
    {"query": "社会", "source_type": "pdf"},
    {"query": "社会", "source_type": "epub"},
    {"query": "社会", "source_type": "word"},
    {"query": "社会", "source_file_id": "word-b"},
    {"query": "社会", "source_file_ids": ("pdf-a", "epub-c")},
    {"query": "社会", "source_file_ids": ()},
    {"query": "社会", "mode": "fuzzy", "source_file_ids": ("pdf-a", "epub-c")},
    {"query": "社会学"},
    {"query": "social relations"},
]


def _drain_all(path: Path) -> None:
    while sgi.drain_short_gram_backlog_at(path):
        pass


def _pending(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {sgi.PENDING_TABLE}").fetchone()[0]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        if not sqlite3.sqlite_version_info >= (3, 43, 0):
            self.skipTest("FTS5 contentless_delete needs SQLite 3.43+")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "index.sqlite3"
        build_database(_corpus(), self.path)

    def responses(self, *, prefilter: bool) -> list:
        engine = SearchEngine(self.path)
        try:
            disabled = patch("src.me_finder.search.short_gram_prefilter_ready", return_value=False)
            with contextlib.nullcontext() if prefilter else disabled:
                return [
                    execute_with_script_folding(engine, SearchRequest(**spec), enabled=True)
                    for spec in QUERIES
                ]
        finally:
            engine.close()

    def assertSameAsPlainScan(self) -> None:
        used = []
        original = sgi.short_gram_prefilter

        def recording(strings):
            used.append(strings)
            return original(strings)

        with patch("src.me_finder.search_recall.short_gram_prefilter", side_effect=recording):
            with_index = self.responses(prefilter=True)
        self.assertTrue(used, "the prefilter was never exercised")
        plain = self.responses(prefilter=False)
        for spec, left, right in zip(QUERIES, with_index, plain):
            self.assertEqual(
                json.dumps(left, sort_keys=True, ensure_ascii=False),
                json.dumps(right, sort_keys=True, ensure_ascii=False),
                f"response changed for {spec}",
            )


class ShortGramTokenTests(unittest.TestCase):
    def test_every_contained_string_matches_its_expression_terms(self) -> None:
        rng = random.Random(7)
        alphabet = "社会價值 ,，。aZ𠮷😀\t"
        for _ in range(300):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 30)))
            tokens = set(sgi.paragraph_gram_tokens([text]).split())
            start = rng.randrange(len(text))
            needle = text[start:start + rng.randrange(1, 4)]
            expression = sgi.short_gram_match_expression([needle])
            terms = {term.strip('"()') for term in expression.split(" AND ")}
            self.assertLessEqual(terms, tokens, (text, needle))

    def test_single_character_uses_its_unigram_and_empties_are_ignored(self) -> None:
        self.assertEqual(sgi.short_gram_match_expression(["社"]), '("' + sgi._code("社") + '")')
        self.assertIsNone(sgi.short_gram_match_expression(["", ""]))
        both = sgi.short_gram_match_expression(["社会", "社会", "價值"])
        self.assertEqual(both.count(" OR "), 1)

    def test_long_strings_keep_a_bounded_term_subset(self) -> None:
        expression = sgi.short_gram_match_expression(["".join(chr(0x4E00 + i) for i in range(200))])
        self.assertEqual(expression.count(" AND ") + 1, sgi.MAX_TERMS_PER_STRING)


class ShortGramEquivalenceTests(_Base):
    def test_responses_match_plain_scan_while_everything_is_pending(self) -> None:
        self.assertGreater(_pending(self.path), 0)
        self.assertSameAsPlainScan()

    def test_responses_match_plain_scan_after_drain(self) -> None:
        _drain_all(self.path)
        self.assertEqual(_pending(self.path), 0)
        self.assertSameAsPlainScan()

    def test_writes_after_drain_stay_visible_through_pending(self) -> None:
        _drain_all(self.path)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT * FROM paragraphs WHERE paragraph_id = 'P-0000'").fetchone()
            columns = [d[0] for d in connection.execute("SELECT * FROM paragraphs LIMIT 0").description]
            fresh = dict(zip(columns, row))
            fresh.update(paragraph_id="P-NEW", text_raw="新增麒麟段落", normalized_text="新增麒麟段落",
                         compact_text="新增麒麟段落", plain_text="新增麒麟段落")
            connection.execute(
                f"INSERT INTO paragraphs({', '.join(fresh)}) VALUES ({', '.join('?' * len(fresh))})",
                list(fresh.values()),
            )
            connection.execute(
                "UPDATE paragraphs SET text_raw = '改成驃騎', normalized_text = '改成驃騎', "
                "compact_text = '改成驃騎', plain_text = '改成驃騎' WHERE paragraph_id = 'P-0001'"
            )
            connection.execute("DELETE FROM paragraphs WHERE paragraph_id = 'P-0003'")
        self.assertEqual(_pending(self.path), 2)
        self.assertSameAsPlainScan()
        engine = SearchEngine(self.path)
        try:
            ids = {hit["paragraph_id"] for hit in engine.search("麒麟", limit="all")["results"]}
            self.assertIn("P-NEW", ids)
            ids = {hit["paragraph_id"] for hit in engine.search("驃騎", limit="all")["results"]}
            self.assertIn("P-0001", ids)
        finally:
            engine.close()

    def test_public_fixture_matches_plain_scan(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        create_fixture(Path(temporary.name), documents=6, paragraphs=120, alignment_paragraphs=8)
        self.path = Path(temporary.name) / "data" / "index.sqlite3"
        _drain_all(self.path)
        self.assertSameAsPlainScan()


class ShortGramLifecycleTests(_Base):
    def _grams_for(self, text: str) -> set[int]:
        with sqlite3.connect(self.path) as connection:
            return {
                row[0] for row in connection.execute(
                    f"SELECT rowid FROM {sgi.GRAMS_TABLE} WHERE {sgi.GRAMS_TABLE} MATCH ?",
                    (sgi.short_gram_match_expression([text]),),
                )
            }

    def test_build_queues_every_paragraph_and_drain_empties_the_queue(self) -> None:
        with sqlite3.connect(self.path) as connection:
            total = connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 10)
        self.assertEqual(_pending(self.path), total)
        _drain_all(self.path)
        self.assertEqual(_pending(self.path), 0)
        self.assertTrue(self._grams_for("麒麟"))

    def test_delete_and_update_drop_stale_grams(self) -> None:
        _drain_all(self.path)
        target = self._grams_for("麒麟")
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM paragraphs WHERE text_raw LIKE '麒麟%'")
            connection.execute(
                "UPDATE paragraphs SET plain_text = '已改' WHERE text_raw LIKE '驃騎%'"
            )
        self.assertEqual(self._grams_for("麒麟") & target, set())
        self.assertEqual(self._grams_for("驃騎"), set())
        self.assertEqual(_pending(self.path), 1)

    def test_large_backlog_falls_back_to_the_plain_scan(self) -> None:
        with sqlite3.connect(self.path) as connection:
            self.assertTrue(sgi.short_gram_prefilter_ready(connection))
            with patch.object(sgi, "PENDING_PREFILTER_LIMIT", 3):
                self.assertFalse(sgi.short_gram_prefilter_ready(connection))

    def test_whole_library_prefiltered_scan_keeps_rowid_early_stop(self) -> None:
        _drain_all(self.path)
        clause, args = sgi.short_gram_prefilter(["社会", "社会"])
        with sqlite3.connect(self.path) as connection:
            plan = " | ".join(str(row[-1]) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT p.rowid FROM paragraphs p WHERE +p.eligible_for_search = 1"
                + clause + " AND instr(p.text_raw, ?) > 0 ORDER BY p.rowid LIMIT 65",
                [*args, "社会"],
            ))
        self.assertNotIn("FOR ORDER BY", plan.upper())

    def test_drain_yields_to_a_busy_writer(self) -> None:
        holder = sqlite3.connect(self.path)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), 0)
        holder.rollback()
        self.assertGreater(sgi.drain_short_gram_backlog_at(self.path), 0)

    def test_unsupported_sqlite_leaves_no_objects_and_search_scans(self) -> None:
        class Refusing:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, sql, *args):
                if "contentless_delete" in sql:
                    raise sqlite3.OperationalError("unknown option")
                return self._connection.execute(sql, *args)

        with sqlite3.connect(self.path) as connection:
            connection.execute(f"DROP TABLE {sgi.GRAMS_TABLE}")
            connection.execute(f"DROP TABLE {sgi.PENDING_TABLE}")
            for name in ("ai", "ad", "au"):
                connection.execute(f"DROP TRIGGER paragraph_short_grams_{name}")
            self.assertFalse(sgi.install_short_gram_index(Refusing(connection)))
            self.assertFalse(sgi.short_gram_prefilter_ready(connection))
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            self.assertNotIn(sgi.PENDING_TABLE, names)
        engine = SearchEngine(self.path)
        try:
            self.assertGreater(engine.search("麒麟")["total"], 0)
        finally:
            engine.close()

    def test_v9_database_migrates_once_and_queues_everything(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(f"DROP TABLE {sgi.GRAMS_TABLE}")
            connection.execute(f"DROP TABLE {sgi.PENDING_TABLE}")
            for name in ("ai", "ad", "au"):
                connection.execute(f"DROP TRIGGER paragraph_short_grams_{name}")
            connection.execute("DELETE FROM metadata WHERE key = ?", (sgi.SHORT_GRAM_METADATA_KEY,))
            connection.execute("PRAGMA user_version = 9")
            total = connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
        self.assertTrue(migrate_index_database(self.path))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertTrue(sgi.short_gram_prefilter_ready(connection))
        self.assertEqual(_pending(self.path), total)
        _drain_all(self.path)
        migrate_index_database(self.path)
        self.assertEqual(_pending(self.path), 0, "an up-to-date index must not be re-queued")


class ShortGramBackfillLoopTests(_Base):
    def test_loop_drains_then_idles_and_stops(self) -> None:
        stop = threading.Event()
        calls = []

        def run_when_ready(operation):
            calls.append(1)
            drained = operation(self.path)
            if not drained:
                stop.set()
            return drained

        run_short_gram_backfill(run_when_ready, stop, idle_seconds=0.01)
        self.assertEqual(_pending(self.path), 0)
        self.assertGreater(len(calls), 1)

    def test_loop_survives_failures_and_a_rebuilding_index(self) -> None:
        stop = threading.Event()
        outcomes = iter([RuntimeError("boom"), None])

        def run_when_ready(_operation):
            outcome = next(outcomes, "stop")
            if outcome == "stop":
                stop.set()
                return None
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with self.assertLogs(level="ERROR"):
            run_short_gram_backfill(run_when_ready, stop, idle_seconds=0.01)
        self.assertTrue(stop.is_set())


if __name__ == "__main__":
    unittest.main()
