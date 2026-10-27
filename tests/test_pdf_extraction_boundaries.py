"""Keep PDF parsing and paragraph construction independent of import policy."""

import ast
from pathlib import Path
import unittest

from src.me_finder import pdf_extraction_types, pdf_extractors, pdf_native, pdf_paragraphs

ROOT = Path(__file__).resolve().parents[1] / "src/me_finder"


class PDFExtractionBoundaryTests(unittest.TestCase):
    def test_extraction_stages_have_one_way_dependencies(self) -> None:
        allowed = {
            "pdf_extraction_types": set(),
            "pdf_paragraphs": {"normalization", "pdf_extraction_types", "pdf_page_mapping"},
            "pdf_native": {"pdf_extraction_types", "pdf_paragraphs", "pdf_page_mapping"},
        }
        for module, dependencies in allowed.items():
            with self.subTest(module=module):
                tree = ast.parse((ROOT / f"{module}.py").read_text(encoding="utf-8"))
                imports = {n.module for n in ast.walk(tree)
                           if isinstance(n, ast.ImportFrom) and n.level}
                self.assertEqual(imports, dependencies)

    def test_original_import_entry_points_reexport_the_owned_implementation(self) -> None:
        for owner, names in [
            (pdf_extraction_types, ("PDFExtractionError", "PDFTextPage")),
            (pdf_native, ("SimplePDF", "detect_pdf_type", "extract_native_pdf_pages",
                          "get_pdf_page_labels", "get_pdf_page_dimensions")),
            (pdf_paragraphs, ("make_pdf_paragraphs", "make_text_source_span",
                              "pdf_page_text_hash", "page_body_bounds", "attach_page_block_offsets")),
        ]:
            for name in names:
                with self.subTest(name=name):
                    self.assertIs(getattr(pdf_extractors, name), getattr(owner, name))

    def test_paragraph_construction_does_not_read_source_files(self) -> None:
        tree = ast.parse((ROOT / "pdf_paragraphs.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"open", "Path"})
                elif isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, {"open", "read_text", "read_bytes"})


if __name__ == "__main__":
    unittest.main()
