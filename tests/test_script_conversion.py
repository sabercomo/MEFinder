"""Tests for the OpenCC-backed script conversion leaf module (issue #16)."""

import unittest

from src.me_finder import script_conversion
from src.me_finder.script_conversion import (
    fold_to_simplified_with_map,
    is_available,
    query_variants,
    to_simplified,
    to_traditional,
)


def _clear_caches() -> None:
    script_conversion._convert_cached.cache_clear()


@unittest.skipUnless(is_available(), "OpenCC is not installed")
class PhraseLevelConversionTests(unittest.TestCase):
    def test_traditional_to_simplified(self) -> None:
        self.assertEqual(to_simplified("剩餘價值"), "剩余价值")

    def test_phrase_dictionary_entry_is_used(self) -> None:
        # 軟體 -> 软件 only happens via the phrase dictionaries; pure
        # character-level conversion would produce 软体 instead.
        self.assertEqual(to_simplified("軟體"), "软件")

    def test_simplified_to_traditional(self) -> None:
        self.assertEqual(to_traditional("剩余价值"), "剩餘價值")

    def test_non_chinese_text_is_unchanged(self) -> None:
        self.assertEqual(to_simplified("Capital p. 152"), "Capital p. 152")


@unittest.skipUnless(is_available(), "OpenCC is not installed")
class FoldWithMapTests(unittest.TestCase):
    def test_pure_traditional_folds_one_to_one(self) -> None:
        folded, source_map = fold_to_simplified_with_map("剩餘價值論")
        self.assertEqual(folded, "剩余价值论")
        self.assertEqual(source_map, [0, 1, 2, 3, 4])

    def test_fold_uses_phrase_dictionaries(self) -> None:
        text = "這個軟體很好用"
        folded, source_map = fold_to_simplified_with_map(text)
        self.assertEqual(folded, "这个软件很好用")
        self.assertEqual(source_map, list(range(len(text))))

    def test_mixed_text_keeps_shared_chars_at_their_index(self) -> None:
        # 剩 is already Simplified-shaped and must stay put.
        folded, source_map = fold_to_simplified_with_map("A剩b餘")
        self.assertEqual(folded, "A剩b余")
        self.assertEqual(source_map, [0, 1, 2, 3])

    def test_map_is_identity_and_lengths_match_for_a_longer_sentence(self) -> None:
        text = "帝國主義是資本主義的最高階段"
        folded, source_map = fold_to_simplified_with_map(text)
        self.assertEqual(folded, "帝国主义是资本主义的最高阶段")
        self.assertEqual(len(folded), len(text))
        self.assertEqual(source_map, list(range(len(text))))

    def test_empty_text(self) -> None:
        self.assertEqual(fold_to_simplified_with_map(""), ("", []))


class LengthChangeFallbackTests(unittest.TestCase):
    """Length-changing phrase conversions opt their segment out of folding."""

    def setUp(self) -> None:
        self._original_to_simplified = script_conversion.to_simplified
        self._original_is_available = script_conversion.is_available
        # Simulate a phrase entry that maps three chars to two (the way
        # locale dictionaries map 記憶體 -> 内存).  The generic t2s dictionary
        # rarely changes length, so a deterministic stand-in keeps the test
        # independent of dictionary contents.
        script_conversion.to_simplified = (
            lambda s: s.replace("記憶體", "内存").replace("價值", "价值")
        )
        script_conversion.is_available = lambda: True

    def tearDown(self) -> None:
        script_conversion.to_simplified = self._original_to_simplified
        script_conversion.is_available = self._original_is_available

    def test_length_changed_segment_falls_back_but_neighbors_still_fold(self) -> None:
        text = "使用記憶體。剩餘價值"
        folded, source_map = script_conversion.fold_to_simplified_with_map(text)
        self.assertEqual(folded, "使用記憶體。剩餘价值")
        self.assertEqual(len(folded), len(text))
        self.assertEqual(source_map, list(range(len(text))))

    def test_all_segments_fold_when_lengths_are_preserved(self) -> None:
        text = "價值。價值"
        folded, source_map = script_conversion.fold_to_simplified_with_map(text)
        self.assertEqual(folded, "价值。价值")
        self.assertEqual(source_map, list(range(len(text))))


@unittest.skipUnless(is_available(), "OpenCC is not installed")
class QueryVariantTests(unittest.TestCase):
    def test_simplified_query_gains_traditional_variant(self) -> None:
        variants = query_variants("剩余价值")
        self.assertEqual(variants[0], "剩余价值")
        self.assertIn("剩餘價值", variants)

    def test_traditional_query_gains_simplified_variant(self) -> None:
        variants = query_variants("剩餘價值")
        self.assertEqual(variants[0], "剩餘價值")
        self.assertIn("剩余价值", variants)

    def test_ascii_query_produces_no_extra_variants(self) -> None:
        self.assertEqual(query_variants("Capital"), ["Capital"])

    def test_variants_are_deduplicated(self) -> None:
        self.assertEqual(query_variants("剩"), ["剩"])

    def test_disabled_toggle_returns_original_only(self) -> None:
        self.assertEqual(query_variants("剩余价值", enabled=False), ["剩余价值"])


class IdentityFallbackTests(unittest.TestCase):
    """With no usable OpenCC every public function degrades to identity."""

    def setUp(self) -> None:
        self._original_converter = script_conversion._converter
        script_conversion._converter = lambda config: None
        _clear_caches()

    def tearDown(self) -> None:
        script_conversion._converter = self._original_converter
        _clear_caches()

    def test_phrase_level_is_identity(self) -> None:
        self.assertEqual(to_simplified("剩餘價值"), "剩餘價值")

    def test_fold_is_identity_map(self) -> None:
        folded, source_map = fold_to_simplified_with_map("剩餘價值")
        self.assertEqual(folded, "剩餘價值")
        self.assertEqual(source_map, [0, 1, 2, 3])

    def test_query_variants_collapse_to_original(self) -> None:
        self.assertEqual(query_variants("剩余价值"), ["剩余价值"])


if __name__ == "__main__":
    unittest.main()
