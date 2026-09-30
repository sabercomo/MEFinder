"""The fuzzy-search benchmark driver labels its own cases correctly.

Experiment driver: ``scripts/fuzzy_search_benchmark.py`` (committed with this
test). The numbers it measures live in ``reports/``; these tests only pin that
every sampled case carries a truthful answer key and a sane diagnosis.
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
