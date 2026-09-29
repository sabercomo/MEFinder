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
from src.me_finder.persistence import short_gram_schema as sgs
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
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
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
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
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
        self.assertEqual(_pending(self.path), 3)
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
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            return {
                row[0] for row in connection.execute(
                    f"SELECT rowid FROM {sgi.GRAMS_TABLE} WHERE {sgi.GRAMS_TABLE} MATCH ?",
                    (sgi.short_gram_match_expression([text]),),
                )
            }

    def test_build_queues_every_paragraph_and_drain_empties_the_queue(self) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            total = connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertEqual(_pending(self.path), total)
        _drain_all(self.path)
        self.assertEqual(_pending(self.path), 0)
        self.assertTrue(self._grams_for("麒麟"))

    def test_delete_and_update_queue_rows_and_drain_drops_stale_grams(self) -> None:
        _drain_all(self.path)
        target = self._grams_for("麒麟")
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM paragraphs WHERE text_raw LIKE '麒麟%'")
            connection.execute(
                "UPDATE paragraphs SET text_raw = '已改', normalized_text = '已改', "
                "compact_text = '已改', plain_text = '已改' WHERE text_raw LIKE '驃騎%'"
            )
        self.assertEqual(_pending(self.path), 2)
        _drain_all(self.path)
        self.assertEqual(self._grams_for("麒麟") & target, set())
        self.assertEqual(self._grams_for("驃騎"), set())
        self.assertEqual(_pending(self.path), 0)

    def test_triggers_only_touch_the_ordinary_pending_table(self) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            triggers = dict(connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?, ?)",
                sgs.TRIGGER_NAMES,
            ).fetchall())
        self.assertEqual(set(triggers), set(sgs.TRIGGER_NAMES))
        for name, sql in triggers.items():
            self.assertNotIn(sgi.GRAMS_TABLE + " ", sql + " ", name)
            self.assertIn(sgi.PENDING_TABLE, sql, name)

    def test_large_backlog_falls_back_to_the_plain_scan(self) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertTrue(sgi.short_gram_prefilter_ready(connection))
            with patch.object(sgi, "PENDING_PREFILTER_LIMIT", 3):
                self.assertFalse(sgi.short_gram_prefilter_ready(connection))

    def test_whole_library_prefiltered_scan_keeps_rowid_early_stop(self) -> None:
        _drain_all(self.path)
        clause, args = sgi.short_gram_prefilter(["社会", "社会"])
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
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

        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
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
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(f"DROP TABLE {sgi.GRAMS_TABLE}")
            connection.execute(f"DROP TABLE {sgi.PENDING_TABLE}")
            for name in ("ai", "ad", "au"):
                connection.execute(f"DROP TRIGGER paragraph_short_grams_{name}")
            connection.execute("DELETE FROM metadata WHERE key = ?", (sgi.SHORT_GRAM_METADATA_KEY,))
            connection.execute("PRAGMA user_version = 9")
            total = connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
        self.assertTrue(migrate_index_database(self.path))
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertTrue(sgi.short_gram_prefilter_ready(connection))
        self.assertEqual(_pending(self.path), total)
        _drain_all(self.path)
        migrate_index_database(self.path)
        self.assertEqual(_pending(self.path), 0, "an up-to-date index must not be re-queued")


# The v10 triggers deleted from the grams table on every paragraph write.
_LEGACY_V10_TRIGGERS = (
    f"""CREATE TRIGGER paragraph_short_grams_ad AFTER DELETE ON paragraphs BEGIN
        DELETE FROM {sgi.GRAMS_TABLE} WHERE rowid = old.rowid;
        DELETE FROM {sgi.PENDING_TABLE} WHERE paragraph_rowid = old.rowid;
    END""",
    f"""CREATE TRIGGER paragraph_short_grams_au AFTER UPDATE OF
        text_raw, normalized_text, compact_text, plain_text ON paragraphs BEGIN
        DELETE FROM {sgi.GRAMS_TABLE} WHERE rowid = old.rowid;
        INSERT OR IGNORE INTO {sgi.PENDING_TABLE} VALUES (new.rowid);
    END""",
)


class OlderSqliteOnBuiltIndexTests(_Base):
    """A library indexed on SQLite 3.43+ and then opened by an older build.

    An older FTS5 rejects ``contentless_delete`` whenever it touches the grams
    table ("unrecognized option"). Rewriting the stored declaration with an
    option no build knows reproduces exactly that failure on this SQLite, and
    patching :func:`short_gram_supported` reproduces the capability probe.
    """

    def setUp(self) -> None:
        super().setUp()
        _drain_all(self.path)
        self.expected = self.responses(prefilter=False)

    def _set_grams_declaration(self, old: str, new: str) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA writable_schema = ON")
            with connection:
                connection.execute(
                    "UPDATE sqlite_master SET sql = replace(sql, ?, ?) WHERE name = ?",
                    (old, new, sgi.GRAMS_TABLE),
                )
            connection.execute("PRAGMA writable_schema = OFF")

    @contextlib.contextmanager
    def older_sqlite(self):
        self._set_grams_declaration("contentless_delete=1", "not_in_this_build=1")
        try:
            with patch.object(sgi, "short_gram_supported", return_value=False):
                yield
        finally:
            self._set_grams_declaration("not_in_this_build=1", "contentless_delete=1")

    def _write_paragraphs(self) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE paragraphs SET text_raw = '改成驃騎', normalized_text = '改成驃騎', "
                "compact_text = '改成驃騎', plain_text = '改成驃騎' WHERE paragraph_id = 'P-0001'"
            )
            connection.execute("DELETE FROM paragraphs WHERE paragraph_id = 'P-0003'")

    def test_emulation_really_breaks_the_grams_table(self) -> None:
        with self.older_sqlite():
            with contextlib.closing(sqlite3.connect(self.path)) as connection:
                with self.assertRaisesRegex(sqlite3.OperationalError, "not_in_this_build"):
                    connection.execute(f"SELECT rowid FROM {sgi.GRAMS_TABLE} LIMIT 1").fetchall()

    def test_older_build_searches_writes_and_leaves_the_backlog(self) -> None:
        with self.older_sqlite():
            self.assertEqual(self.responses(prefilter=True), self.expected)
            with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
                self.assertFalse(sgi.short_gram_prefilter_ready(connection))
                self.assertFalse(sgi.install_short_gram_index(connection, rebuild=True))
            self._write_paragraphs()
            self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), 0)
            self.assertEqual(_pending(self.path), 2)
            engine = SearchEngine(self.path)
            try:
                ids = {hit["paragraph_id"] for hit in engine.search("驃騎", limit="all")["results"]}
            finally:
                engine.close()
            self.assertIn("P-0001", ids)
        # Back on a capable build the queued writes drain and nothing is stale.
        _drain_all(self.path)
        self.assertEqual(_pending(self.path), 0)
        self.assertSameAsPlainScan()

    def test_v11_migration_replaces_legacy_triggers_on_an_older_build(self) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            for statement in _LEGACY_V10_TRIGGERS:
                name = statement.split()[2]
                connection.execute(f"DROP TRIGGER {name}")
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 10")
        with self.older_sqlite():
            with self.assertRaisesRegex(sqlite3.OperationalError, "not_in_this_build"):
                self._write_paragraphs()
            self.assertTrue(migrate_index_database(self.path))
            self._write_paragraphs()
            self.assertEqual(_pending(self.path), 2)
            with contextlib.closing(sqlite3.connect(self.path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 11)
        _drain_all(self.path)
        self.assertSameAsPlainScan()


class PackagedSqliteFloorTests(unittest.TestCase):
    def test_every_build_script_refuses_a_runtime_without_the_grams_table(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in ("build_macos.sh", "build_windows_installer.ps1", "build_portable_release.ps1"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertIn(sgi.GRAMS_TABLE, source, name)
            self.assertIn("3.43", source, name)
        self.assertEqual(sgs.MIN_SQLITE_VERSION, (3, 43, 0))


class FuzzyTieOrderTests(unittest.TestCase):
    """More same-overlap candidates than the rescore cut, stored out of order."""

    TIED = 100

    def setUp(self) -> None:
        if not sqlite3.sqlite_version_info >= (3, 43, 0):
            self.skipTest("FTS5 contentless_delete needs SQLite 3.43+")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "index.sqlite3"
        corpus = _corpus()
        tied = [
            _paragraph(1000 + index, f"麒麟在第{index}处出现", "pdf-a", "pdf")
            for index in range(self.TIED)
        ]
        # Physical (rowid) order is the reverse of reading order.
        corpus["paragraphs"] = list(reversed(tied)) + corpus["paragraphs"]
        build_database(corpus, self.path)
        _drain_all(self.path)
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            order = [row[0] for row in connection.execute(
                "SELECT paragraph_index FROM paragraphs WHERE paragraph_index >= 1000 ORDER BY rowid"
            )]
            connection.execute("ANALYZE")
        self.assertEqual(order, sorted(order, reverse=True), "fixture must store out of order")

    def _fuzzy(self, *, prefilter: bool, **scope) -> list:
        engine = SearchEngine(self.path)
        try:
            disabled = patch("src.me_finder.search.short_gram_prefilter_ready", return_value=False)
            with contextlib.nullcontext() if prefilter else disabled:
                response = engine.search("麒麟", mode="fuzzy", limit="all", **scope)
        finally:
            engine.close()
        return [hit["paragraph_id"] for hit in response["results"]]

    def test_rescore_cut_keeps_reading_order_whatever_the_plan(self) -> None:
        from src.me_finder.search_contract import FUZZY_RESCORE_LIMIT

        self.assertGreater(self.TIED, FUZZY_RESCORE_LIMIT)
        for scope in ({"source_file_id": "pdf-a"}, {}):
            with_index = self._fuzzy(prefilter=True, **scope)
            plain = self._fuzzy(prefilter=False, **scope)
            self.assertEqual(with_index, plain, scope)
            tied = sorted(pid for pid in with_index if int(pid[2:]) >= 1000)
            expected = [f"P-{1000 + index:04d}" for index in range(len(tied))]
            self.assertEqual(tied, expected, scope)
            self.assertLess(len(tied), self.TIED, scope)


class ShortGramBackfillLoopTests(_Base):
    def test_backfill_installs_index_after_sqlite_capability_returns(self) -> None:
        with patch.object(sgi, "short_gram_supported", return_value=False):
            build_database(_corpus(), self.path)
            migrate_index_database(self.path)
            self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), 0)
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertFalse(sgs.objects_present(connection))

        # The schema version has already advanced; background work must repair
        # the optional index without another migration or a full DB rebuild.
        with patch.object(sgi, "DRAIN_MAX_ROWS", 1):
            self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), 1)
            pending = _pending(self.path)
            self.assertGreater(pending, 1)
            self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), 1)
            self.assertEqual(_pending(self.path), pending - 1)
        self.assertSameAsPlainScan()
        _drain_all(self.path)
        self.assertEqual(sgi.drain_short_gram_backlog_at(self.path), 0)
        self.assertEqual(_pending(self.path), 0)
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], version)
            self.assertTrue(sgi.short_gram_prefilter_ready(connection))
        self.assertSameAsPlainScan()

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
