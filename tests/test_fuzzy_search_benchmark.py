"""The fuzzy-search benchmark driver labels its own cases correctly.

Experiment driver: ``scripts/fuzzy_search_benchmark.py`` (committed with this
test). The numbers it measures live in ``reports/``; these tests only pin that
every sampled case carries a truthful answer key and a sane diagnosis.
"""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts import fuzzy_search_benchmark as bench
from scripts.performance_fixture import create_fixture
from src.me_finder.normalization import punctuationless_text
from src.me_finder.search import SearchEngine


class PerturbTest(unittest.TestCase):
    def test_each_kind_changes_the_original_as_named(self) -> None:
        rng = random.Random(1)
        original = "人们通过共同劳动形成制度并在实践经验中检验知识"
        self.assertEqual(bench.perturb(rng, original, "none"), original)
        for kind, delta in (("sub1", 0), ("sub2", 0), ("omit2", -2), ("insert1", 1)):
            with self.subTest(kind=kind):
                query = bench.perturb(rng, original, kind)
                self.assertNotEqual(query, original)
                self.assertEqual(len(query), len(original) + delta)
        ocr = bench.perturb(rng, "自己的日记", "ocr")
        changed = [(a, b) for a, b in zip("自己的日记", ocr) if a != b]
        self.assertTrue(changed)
        self.assertTrue(all(b in bench.OCR_MAP[a] for a, b in changed))
        self.assertIsNone(bench.perturb(rng, "没有形近字", "ocr"))

    def test_positioned_substitutions_break_every_trigram(self) -> None:
        # FTS 三字片段全都含错字,正是短查询退回两字扫描要覆盖的情形。
        rng = random.Random(2)
        for kind, original in (("sub_center", "共同劳动形"), ("sub2_spread", "共同劳动形成"),
                               ("sub2_spread", "共同劳动形成制"), ("sub2_spread", "共同劳动形成制度")):
            with self.subTest(kind=kind, length=len(original)):
                query = bench.perturb(rng, original, kind)
                self.assertEqual(len(query), len(original))
                trigrams = {original[i:i + 3] for i in range(len(original) - 2)}
                self.assertFalse(trigrams & {query[i:i + 3] for i in range(len(query) - 2)})
        self.assertIsNone(bench.perturb(rng, "共同劳动", "sub_center"))


class BenchmarkCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        create_fixture(root, documents=2, paragraphs=40, alignment_paragraphs=8)
        cls.path = root / "data" / "index.sqlite3"
        with closing(sqlite3.connect(cls.path)) as connection:
            cls.cases = bench.build_cases(connection, per_category=2, seed=7)
            cls.again = bench.build_cases(connection, per_category=2, seed=7)
            cls.texts = dict(connection.execute("SELECT paragraph_id, text_raw FROM paragraphs"))
            cls.plains = [row[0] for row in connection.execute(
                "SELECT plain_text FROM paragraphs WHERE eligible_for_search = 1")]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_cases_are_deterministic_and_truthful(self) -> None:
        self.assertEqual(self.cases, self.again)
        labelled = [case for case in self.cases if case["target_paragraph_id"]]
        self.assertTrue({"exact_control", "long_sub", "long_omit"} <= {c["category"] for c in labelled})
        for case in labelled:
            with self.subTest(case=case["id"]):
                text = self.texts[case["target_paragraph_id"]]
                self.assertEqual(text[case["target_start"]:case["target_end"]], case["original"])
                self.assertIn(case["target_paragraph_id"], case["acceptable_ids"])
                if case["category"] != "exact_control":
                    plain_query = punctuationless_text(case["query"])
                    self.assertNotEqual(plain_query, punctuationless_text(case["original"]))
                    self.assertFalse(any(plain_query in plain for plain in self.plains))

    def test_negative_cases_never_occur_in_the_library(self) -> None:
        negatives = [case for case in self.cases if case["category"].startswith("negative")]
        lengths = (*bench.NEGATIVE_LENGTHS, *bench.EXTRA_NEGATIVE_LENGTHS)
        self.assertEqual(len(negatives), 2 * len(lengths))
        for case in negatives:
            self.assertFalse(any(case["query"] in plain for plain in self.plains))

    def test_run_case_diagnoses_found_and_negative(self) -> None:
        exact = next(case for case in self.cases if case["category"] == "exact_control")
        negative = next(case for case in self.cases if case["category"].startswith("negative"))
        engine = SearchEngine(self.path)
        try:
            found = bench.run_case(engine, exact, "auto", limit=10, repeats=1)
            missing = bench.run_case(engine, negative, "fuzzy", limit=10, repeats=1)
        finally:
            engine.close()
        self.assertEqual(found["stage"], "found")
        self.assertEqual(found["found_rank"], 1)
        self.assertEqual(found["highlight_ratio"], 1.0)
        self.assertEqual(missing["stage"], "negative")
        self.assertEqual(missing["noise"], missing["returned"])
        rows = bench.summarize(self.cases, [{"case_id": exact["id"], **found},
                                            {"case_id": negative["id"], **missing}])
        self.assertEqual([(row["category"], row["mode"]) for row in rows],
                         [("exact_control", "auto"), (negative["category"], "fuzzy")])
        self.assertIn("| exact_control | auto | 1 | 1 | 1 |", bench.render_summary(rows))


class ExpectedPagesTest(unittest.TestCase):
    def test_pages_follow_text_source_spans_and_repeats(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE paragraphs (paragraph_id, text_raw, pdf_page_start_index, "
                           "pdf_page_end_index, payload_json)")
        spans = [{"paragraph_char_start": 0, "paragraph_char_end": 6, "pdf_page_index": 56},
                 {"paragraph_char_start": 7, "paragraph_char_end": 14, "pdf_page_index": 57}]
        connection.executemany("INSERT INTO paragraphs VALUES (?, ?, ?, ?, ?)", [
            ("cross", "甲乙丙丁戊己 庚辛壬癸子丑寅", 56, 57, json.dumps({"text_source_spans": spans})),
            ("single", "甲乙丙丁甲乙丙丁", 9, 9, "{}"),
            ("word", "甲乙丙丁", None, None, "{}"),
        ])
        engine = type("Engine", (), {"db": connection})()
        self.assertEqual(bench._expected_pages(engine, "cross", "己 庚辛"), {(56, 57)})
        self.assertEqual(bench._expected_pages(engine, "cross", "甲乙丙"), {(56, 56)})
        self.assertEqual(bench._expected_pages(engine, "cross", "辛壬癸"), {(57, 57)})
        self.assertEqual(bench._expected_pages(engine, "single", "甲乙丙丁"), {(9, 9)})
        self.assertIsNone(bench._expected_pages(engine, "word", "甲乙丙丁"))
        self.assertIsNone(bench._expected_pages(engine, "single", "不在此处"))

    def test_page_checks_use_stored_pages_not_the_result(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE paragraphs (paragraph_id, source_file_id, text_raw, "
                           "pdf_page_start_index, pdf_page_end_index, payload_json)")
        connection.execute("CREATE TABLE pdf_pages (source_file_id, pdf_page_index, payload_json)")
        spans = [{"paragraph_char_start": 0, "paragraph_char_end": 6, "pdf_page_index": 485},
                 {"paragraph_char_start": 7, "paragraph_char_end": 14, "pdf_page_index": 486}]
        connection.executemany("INSERT INTO paragraphs VALUES (?, ?, ?, ?, ?, ?)", [
            ("cross", "f", "甲乙丙丁戊己 庚辛壬癸子丑寅", 485, 486, json.dumps({"text_source_spans": spans})),
            ("legacy", "f", "甲乙丙丁戊己", 485, 485, "{}"),
        ])
        connection.executemany("INSERT INTO pdf_pages VALUES (?, ?, ?)", [
            ("f", 485, json.dumps({"citation_page_start": "463", "citation_page_end": "463"})),
            ("f", 486, json.dumps({"citation_page_start": "464", "citation_page_end": "464"})),
        ])
        engine = type("Engine", (), {"db": connection})()
        hit = {"paragraph_id": "cross", "pdf_page_start_index": 485, "pdf_page_end_index": 486,
               "citation_page_start": "463", "citation_page_end": "463",
               "page_match_spans": [{"pdf_page_index": 485}]}
        self.assertEqual(bench._page_checks(engine, hit, "甲乙丙"), (True, True))
        # 段落跨两页、原句只在一页,却报出整段范围:引用页码错。
        self.assertEqual(bench._page_checks(engine, {**hit, "citation_page_end": "464"}, "甲乙丙"),
                         (True, False))
        # 入库记录是 463,结果被改成 999:必须判错,不能用结果自证。
        self.assertEqual(bench._page_checks(engine, {**hit, "citation_page_start": "999",
                                                     "citation_page_end": "999"}, "甲乙丙"), (True, False))
        self.assertEqual(bench._page_checks(engine, {**hit, "page_match_spans": [
            {"pdf_page_index": 485}, {"pdf_page_index": 486}], "citation_page_end": "464"}, "己 庚辛"),
            (True, True))
        # 库里有页面对照、结果却没带锚点:失败;库里本无对照(旧库):无法核对。
        self.assertEqual(bench._page_checks(engine, {**hit, "page_match_spans": []}, "甲乙丙")[0], False)
        legacy = {**hit, "paragraph_id": "legacy", "pdf_page_end_index": 485, "page_match_spans": []}
        self.assertEqual(bench._page_checks(engine, legacy, "甲乙丙"), (None, True))


if __name__ == "__main__":
    unittest.main()
