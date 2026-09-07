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
    script_conversion._fold_char.cache_clear()


@unittest.skipUnless(is_available(), "OpenCC is not installed")
class PhraseLevelConversionTests(unittest.TestCase):
    def test_traditional_to_simplified(self) -> None:
        self.assertEqual(to_simplified("剩餘價值"), "剩余价值")

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

    def test_mixed_text_keeps_shared_chars_at_their_index(self) -> None:
        # 剩 is already Simplified-shaped and must stay put.
        folded, source_map = fold_to_simplified_with_map("A剩b餘")
        self.assertEqual(folded, "A剩b余")
        self.assertEqual(source_map, [0, 1, 2, 3])

    def test_map_contract_holds_for_a_longer_sentence(self) -> None:
        text = "帝國主義是資本主義的最高階段"
        folded, source_map = fold_to_simplified_with_map(text)
        self.assertEqual(folded, "帝国主义是资本主义的最高阶段")
        self.assertEqual(len(folded), len(source_map))
        self.assertTrue(all(0 <= index < len(text) for index in source_map))

    def test_empty_text(self) -> None:
        self.assertEqual(fold_to_simplified_with_map(""), ("", []))


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
