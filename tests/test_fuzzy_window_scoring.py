"""D7 fuzzy window scoring (RapidFuzz) and the 5-8 character bigram union.

Every case here came out of the benchmark rounds and their reviews:
reports/fuzzy-search-benchmark-2026-09-30.md section 10. The committed
prototype ``scripts/fuzzy_d7_ab.py`` is the scorer that passed validation; the
production scorer must stay equal to it, and paragraphs shorter than every
window must keep the legacy difflib result exactly.
"""

from __future__ import annotations

import random
import unittest

from scripts.fuzzy_d7_ab import legacy_best_window_ratio, prototype_best_window_ratio
from src.me_finder.search_contract import fuzzy_needs_bigram_scan
from src.me_finder.search_scoring import best_window_ratio

PAD = "此处是与查询无关的填充文字用来拉长段落"


def window(query: str, plain: str) -> str:
    _score, start, end = best_window_ratio(query, plain)
    return plain[start : end + 1]


class WindowChoiceTest(unittest.TestCase):
    def test_typo_shapes_highlight_exactly_the_original(self) -> None:
        cases = [
            # substitution / omission / insertion inside a long paragraph
            ("人们通过共同劳作形成制度并在实践", "人们通过共同劳动形成制度并在实践"),
            ("人们通过共劳动形成度并在实践经验", "人们通过共同劳动形成制度并在实践经验"),
            ("人们通过共同劳动形成了制度并在实践", "人们通过共同劳动形成制度并在实践"),
            # typo on the last / first character must not shrink the window
            ("伎術真例給業成者於吏部簡試孝經論語共試入", "伎術直例給業成者於吏部簡試孝經論語共試八"),
            ("它于社会产品的分配并作为其前提的生产要素说分", "先于社会产品的分配并作为其前提的生产要素的分"),
            # equal Indel windows: the substitution beats the shifted one
            ("发子恣意", "分子恣意"),
            # a missing character does not outrank a wrong one
            ("学院地讲演", "学院的讲演"),
        ]
        for query, original in cases:
            with self.subTest(query=query):
                self.assertEqual(window(query, PAD + "而是" + original + "的基础之上" + PAD), original)

    def test_edge_windows_are_not_truncated(self) -> None:
        # partial_ratio favours a shorter edge window ("对于教"); D7 keeps the length.
        self.assertEqual(window("对于教其", "对于教皇的权力来说" + PAD + PAD), "对于教皇")

    def test_a_half_match_at_the_end_does_not_hide_the_full_one(self) -> None:
        # Review case 1: RF-4 anchored on the trailing half and returned 0.
        query, plain = "甲乙丙丁戊己庚辛", "甲乙丙壬癸己庚辛" + "子" * 30 + "甲乙丙丁戊"
        score, start, end = best_window_ratio(query, plain)
        self.assertGreaterEqual(score, 0.58)
        self.assertEqual(plain[start : end + 1], "甲乙丙壬癸己庚辛")

    def test_short_paragraph_scores_the_local_window(self) -> None:
        # Review case 2: whole-paragraph Levenshtein dropped it to 0.5.
        score, start, end = best_window_ratio("甲乙丙丁戊己庚辛", "子子甲乙丙壬癸己庚辛子子")
        self.assertEqual((round(score, 4), start, end), (0.75, 2, 9))
        for extra in range(0, 10):
            plain = "子" * (extra // 2) + "甲乙丙壬癸己庚辛" + "子" * (extra - extra // 2)
            with self.subTest(extra=extra):
                self.assertEqual(window("甲乙丙丁戊己庚辛", plain), "甲乙丙壬癸己庚辛")

    def test_queries_over_64_characters_find_the_global_optimum(self) -> None:
        rng = random.Random(3)
        pool = "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发"
        plain = "".join(rng.choice(pool) for _ in range(2500))
        original = plain[1200:1300]
        query = list(original)
        for index in (10, 40, 77):
            query[index] = "鬼"
        score, start, end = best_window_ratio("".join(query), plain)
        self.assertEqual((start, end), (1200, 1299))
        self.assertAlmostEqual(score, 0.97)

    def test_verbatim_hits_keep_their_score(self) -> None:
        self.assertEqual(best_window_ratio("劳动形成", PAD + "共同劳动形成制度"), (0.91, 21, 24))
        self.assertEqual(best_window_ratio("", "甲"), (0.0, 0, 0))
        self.assertEqual(best_window_ratio("甲", ""), (0.0, 0, 0))


class LegacyAndPrototypeEquivalenceTest(unittest.TestCase):
    def test_paragraphs_shorter_than_every_window_keep_the_legacy_result(self) -> None:
        # Review case 3: this branch must be difflib, not Indel.
        self.assertEqual(best_window_ratio("甲乙甲甲乙", "甲甲甲"), legacy_best_window_ratio("甲乙甲甲乙", "甲甲甲"))
        self.assertEqual(best_window_ratio("甲乙甲甲乙", "甲甲甲")[0], 0.5)
        rng = random.Random(7)
        checked = 0
        for _ in range(4000):
            query = "".join(rng.choice("甲乙丙丁") for _ in range(rng.randint(1, 20)))
            delta = min(3, len(query) // 5)
            plain = "".join(rng.choice("甲乙丙丁") for _ in range(rng.randint(1, max(1, len(query) - delta - 1))))
            if query in plain or len(plain) >= len(query) - delta:
                continue
            checked += 1
            self.assertEqual(best_window_ratio(query, plain), legacy_best_window_ratio(query, plain))
        self.assertGreater(checked, 3000)

    def test_production_equals_the_validated_prototype(self) -> None:
        rng = random.Random(11)
        for _ in range(3000):
            query = "".join(rng.choice("甲乙丙丁戊己庚辛") for _ in range(rng.randint(1, 30)))
            plain = "".join(rng.choice("甲乙丙丁戊己庚辛") for _ in range(rng.randint(0, 200)))
            self.assertEqual(best_window_ratio(query, plain), prototype_best_window_ratio(query, plain))


class BigramScanRuleTest(unittest.TestCase):
    def test_union_from_five_to_eight_and_fallback_only_at_four(self) -> None:
        for length in (1, 2, 3, 9, 20):
            self.assertFalse(fuzzy_needs_bigram_scan(length, False))
            self.assertFalse(fuzzy_needs_bigram_scan(length, True))
        self.assertTrue(fuzzy_needs_bigram_scan(4, False))
        self.assertFalse(fuzzy_needs_bigram_scan(4, True))
        for length in range(5, 9):
            self.assertTrue(fuzzy_needs_bigram_scan(length, False))
            self.assertTrue(fuzzy_needs_bigram_scan(length, True))


if __name__ == "__main__":
    unittest.main()
