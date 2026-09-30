from __future__ import annotations

import itertools
import random
import unittest
from unittest.mock import patch

from src.me_finder.normalization import (
    compact_text,
    normalize_pdf_text,
    normalize_text,
    normalize_with_map,
    normalize_with_spans,
    plain_spans,
    punctuationless_text,
)


class NormalizeWithMapTests(unittest.TestCase):
    def test_normalized_map_collapses_and_trims_the_same_whitespace_as_text(self) -> None:
        raw = " \tＡ\u3000 \nBeta  "

        normalized, mapping = normalize_with_map(raw, "normalized")

        self.assertEqual(normalized, "a beta")
        self.assertEqual(normalized, normalize_text(raw))
        self.assertEqual(mapping, [2, 3, 6, 7, 8, 9])
        self.assertEqual(len(mapping), len(normalized))

    def test_all_whitespace_has_an_empty_normalized_map(self) -> None:
        normalized, mapping = normalize_with_map(" \t\r\n\u3000\u00a0", "normalized")

        self.assertEqual(normalized, "")
        self.assertEqual(mapping, [])

    def test_mapped_match_span_covers_the_original_whitespace_run(self) -> None:
        raw = " \tAlpha \n\t  Beta  tail "
        normalized, mapping = normalize_with_map(raw, "normalized")
        query = normalize_text("Alpha Beta")
        match_start = normalized.index(query)
        match_end = match_start + len(query)

        raw_start = mapping[match_start]
        raw_end = mapping[match_end - 1] + 1

        self.assertEqual(raw[raw_start:raw_end], "Alpha \n\t  Beta")

        later_query = normalize_text("Beta")
        later_start = normalized.index(later_query)
        later_end = later_start + len(later_query)
        self.assertEqual(
            raw[mapping[later_start] : mapping[later_end - 1] + 1],
            "Beta",
        )

    def test_span_map_covers_full_collapsed_whitespace_and_unicode_composition(self) -> None:
        raw = "e\u0301 \n\t \u1100\u1161\u11a8"

        normalized, spans = normalize_with_spans(raw, "normalized")

        self.assertEqual(normalized, "é 각")
        self.assertEqual(normalized, normalize_text(raw))
        self.assertEqual(spans[0], (0, 2))
        self.assertEqual(raw[slice(*spans[1])], " \n\t ")
        self.assertEqual(spans[2], (6, 9))

    def test_pdf_dehyphenation_text_and_span_share_one_contract(self) -> None:
        raw = "before inter-\nnational after"

        normalized, spans = normalize_with_spans(
            raw, "normalized", pdf_hyphenation=True
        )
        query = "international"
        start = normalized.index(query)
        end = start + len(query)

        self.assertEqual(normalized, normalize_pdf_text(raw))
        self.assertEqual(raw[spans[start][0] : spans[end - 1][1]], "inter-\nnational")

    def test_generated_whitespace_variants_keep_text_and_map_in_lockstep(self) -> None:
        whitespace_runs = ("", " ", "  ", "\t", "\n\r", "\u3000", "\u00a0 \t")
        expected_for_mode = {
            "normalized": normalize_text,
            "compact": compact_text,
            "plain": punctuationless_text,
        }

        for leading, middle, trailing in itertools.product(whitespace_runs, repeat=3):
            raw = f"{leading}Ａ{middle}“Ｂ…”{trailing}"
            for mode, expected_normalizer in expected_for_mode.items():
                with self.subTest(
                    mode=mode,
                    leading=repr(leading),
                    middle=repr(middle),
                    trailing=repr(trailing),
                ):
                    normalized, mapping = normalize_with_map(raw, mode)
                    self.assertEqual(normalized, expected_normalizer(raw))
                    self.assertEqual(len(mapping), len(normalized))
                    self.assertEqual(mapping, sorted(mapping))
                    self.assertTrue(
                        all(0 <= source_index < len(raw) for source_index in mapping)
                    )


class PlainSpansTests(unittest.TestCase):
    """``plain_spans`` must equal the full pipeline's plain-mode spans exactly."""

    # Fullwidth punctuation, ligatures, compatibility and combining forms,
    # Hangul jamo (compatibility jamo NFKC to conjoining jamo), Indic vowel
    # compositions, kana voicing marks, invisible formats, İ and Σ.
    ALPHABET = list("社会学的历史，。“”‘’（）《》、：；！？ \n\tAbcXYZ09-_.,;:!?\"'()[]") + [
        "\u3000", "\uff21", "\uff0c", "\u00ad", "\u200b", "\ufeff", "\u0301", "\u1100",
        "\u1161", "\u11a8", "\uac00", "\uac01", "\ufb01", "\u0130", "\u03a3", "\u00c5",
        "\u212b", "\u2014", "\u2026", "\u00a0", "\u2002", "\u2160", "\u3231", "\u00bd",
        "\uff66", "\u0e33", "\u3131", "\u314f", "\uffa1", "\u2474", "\u00a8", "\u02dc",
        "\u0344", "\u0f73", "\u0cc6", "\u0cc2", "\u09c7", "\u09be", "\uff9e", "\u309b",
        "\u304b", "\u3099", "\uff76",
    ]

    def test_matches_full_pipeline_on_random_text(self) -> None:
        rng = random.Random(20260930)
        for _ in range(20000):
            text = "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, 24)))
            self.assertEqual(plain_spans(text), normalize_with_spans(text, "plain")[1], repr(text))

    def test_common_chinese_text_takes_the_table_path(self) -> None:
        text = "社会学的对象是社会事实，“历史”与（理论）：ＡＢ。"
        with patch("src.me_finder.normalization.normalize_with_spans",
                   side_effect=AssertionError("full pipeline used")):
            spans = plain_spans(text)
        self.assertEqual(spans, normalize_with_spans(text, "plain")[1])

    def test_composing_text_falls_back_to_full_pipeline(self) -> None:
        for text in ("e\u0301", "\u1100\u1161", "\u3131\u314f", "\u09c7\u09be"):
            with self.subTest(text=repr(text)):
                with patch("src.me_finder.normalization.normalize_with_spans",
                           wraps=normalize_with_spans) as full:
                    plain_spans(text)
                full.assert_called_once()


if __name__ == "__main__":
    unittest.main()
