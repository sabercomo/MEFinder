import tempfile
import unittest
from pathlib import Path

import fitz

from src.me_finder.pdf.native import detect_pdf_type, extract_native_pdf_pages


class PDFNativeScriptTests(unittest.TestCase):
    def test_numeric_superscripts_are_preserved_in_native_text_and_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page()
                samples = [
                    ("Density 2329 kg/m", "3", ", tested at nominal conditions."),
                    ("Frequency 9.68 x 10", "-3", " Hz in the second test."),
                    ("Fraction q/", "2", " retains its denominator."),
                ]
                for i, (before, power, after) in enumerate(samples):
                    x, y = 48, 60 + i * 28
                    at_power = x + fitz.get_text_length(before, fontsize=12)
                    page.insert_text((x, y), before, fontsize=12)
                    page.insert_text((at_power, y - 5), power, fontsize=8)
                    at_after = at_power + fitz.get_text_length(power, fontsize=8)
                    page.insert_text((at_after, y), after, fontsize=12)
                pdf.save(path)

            page, = extract_native_pdf_pages(path)

        self.assertEqual(
            page.raw_text,
            "Density 2329 kg/m³, tested at nominal conditions.\n"
            "Frequency 9.68 x 10⁻³ Hz in the second test.\n"
            # A raised numeral without a preceding base is not a power.
            "Fraction q/2 retains its denominator.\n",
        )
        self.assertEqual(page.pdf_page_index, 0)
        self.assertEqual(len(page.blocks), 3)
        for block in page.blocks:
            self.assertEqual(
                page.raw_text[block["page_char_start"]:block["page_char_end"]],
                block["text"],
            )

    def test_image_only_pdf_still_has_no_native_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.pdf"
            with fitz.open() as original, fitz.open() as scanned:
                page = original.new_page()
                page.insert_text((48, 60), "Density 2329 kg/m3")
                scanned.new_page().insert_image(page.rect, pixmap=page.get_pixmap())
                scanned.save(path)

            self.assertEqual(detect_pdf_type(path)["detected_pdf_type"], "scanned")
            page, = extract_native_pdf_pages(path)
        self.assertEqual(page.raw_text, "")
        self.assertEqual(page.blocks, [])
