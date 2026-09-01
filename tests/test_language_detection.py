"""Body-language detection regressions for the library projection.

The detector reads a bounded opening sample plus a title fallback.  Script
short-circuits (kana / hangul / Cyrillic) must fire only when that script is
*dominant* — a lone stray character from another script inside an otherwise
English (or Chinese) body must not flip or void the result.
"""

import unittest

from src.me_finder.calibration_library import _item_language_code


class BodyLanguageDetectionTests(unittest.TestCase):
    def test_english_body_with_stray_cyrillic_stays_english(self) -> None:
        # Real regression: an English Hegel translation whose 14k-char sample
        # carried a single Cyrillic glyph short-circuited into the Russian
        # branch and returned "und" instead of "en".
        english = (
            "Elements of the Philosophy of Right. Edited by Allen W. Wood, "
            "translated by H. B. Nisbet. This is the work in which Hegel sets "
            "out the philosophy of right and the state, and the way that "
            "freedom is realised in the modern world. "
        ) * 6
        self.assertEqual(_item_language_code(english + " Достоевский", "Elements of the Philosophy of Right", "", "hegel.pdf"), "en")

    def test_dominant_cyrillic_still_detects_russian(self) -> None:
        russian = (
            "Преступление и наказание. Роман в шести частях с эпилогом. "
            "Это был не то чтобы очень трусливый и забитый человек, а совсем "
            "даже напротив, но с некоторого времени он был в раздражительном "
            "и напряжённом состоянии, похожем на ипохондрию."
        )
        self.assertEqual(_item_language_code(russian, "Преступление и наказание", "Достоевский", "d.pdf"), "ru")

    def test_english_title_only_detects_english(self) -> None:
        self.assertEqual(_item_language_code(None, "Elements of the Philosophy of Right", "", "x.pdf"), "en")

    def test_chinese_body_detects_simplified(self) -> None:
        self.assertEqual(_item_language_code("这是一本关于法哲学原理的中文著作，讨论自由与国家。", "法哲学原理", "黑格尔", "f.pdf"), "zh-Hans")

    def test_empty_input_is_unidentified(self) -> None:
        self.assertEqual(_item_language_code(None, "", "", ""), "und")


if __name__ == "__main__":
    unittest.main()
