"""Freeze PDF extraction before moving native parsing and anchor construction."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.me_finder.pdf import extractors as pdf
from tests.test_export_page_reconstruction import reconstruction_fixture
from tests.test_pdf_match_anchors import make_page, make_paragraphs
from tests.test_pdf_page_split import content_list

GOLDEN = Path(__file__).with_name("fixtures") / "pdf_extraction_contract.json"


def write_pdf(path: Path) -> None:
    """Write a deterministic two-page PDF both native parsers can open."""
    text = b"This page records social relations and historical knowledge. " * 4
    stream = b"BT /F1 10 Tf 50 750 Td (" + text + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /PageLabels 8 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 2000 800] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 2000 800] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Nums [0 << /S /r >> 1 << /S /D /St 27 >>] >>",
    ]
    payload = b"%PDF-1.4\n"
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(payload))
        payload += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(payload)
    payload += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    payload += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    payload += f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(payload)


def capture() -> dict:
    """Capture full extraction payloads and paragraph anchors on isolated data."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "book.pdf"
        write_pdf(path)
        config = {"source_file_id": "contract", "document_id": "contract", "title": "定位样本"}
        uncalibrated = {"method": "uncalibrated", "selected_segments": [],
                        "applied_segments": [], "failure_reasons": []}
        with patch.object(pdf.PageMappingService, "infer", side_effect=lambda *a, **kw: deepcopy(uncalibrated)):
            captured = {"pymupdf_pages": [asdict(page) for page in pdf.extract_native_pdf_pages(path)]}
            with patch("src.me_finder.pdf.native.load_pymupdf", return_value=None):
                captured["simple_native"] = pdf.extract_pdf_source(path, root, config)
                mapped = {**config, "page_mapping": {"segments": [
                    {"pdf_page_start": 0, "pdf_page_end": 1, "citation_page_start": "27"}
                ]}}
                captured["manual_native"] = pdf.extract_pdf_source(path, root, mapped)
                result_dir = root / "result"
                result_dir.mkdir()
                (result_dir / "content_list.json").write_text(json.dumps(content_list()), encoding="utf-8")
                (result_dir / "layout.json").write_text(json.dumps(reconstruction_fixture()[1]), encoding="utf-8")
                structured = {**mapped, "mineru": {"segments": [
                    {"result_dir": str(result_dir), "page_ranges": "1-2", "page_index_offset": 0}
                ]}}
                captured["mineru_split"] = pdf.extract_pdf_source(path, root, structured)

        first = make_page(0, " \r\n" + "正文𠮷讨论历史与社会。" * 12 + "\r\n ")
        second = make_page(1, "27\r\nHEADER\r\n" + "后页𠮷保留字符位置。" * 12 + "\r\n")
        captured["unicode_cross_page"] = make_paragraphs([first, second])
        first.update(citation_page="27", citation_page_start="27", citation_page_end="28",
                     printed_page_start="27", printed_page_end="28", layout_mode="spread",
                     reading_direction="rtl", gutter_x=0.5, page_mapping_method="manual_segment",
                     page_mapping_confidence=0.9, segment_id="spread", page_scope="body")
        captured["spread_mixed_mapping"] = make_paragraphs([first, second, make_page(2, " \n")])

        def normalize(value, key=""):
            if key in {"last_modified", "started_at", "finished_at", "run_id"}:
                return "<timestamp>"
            if key == "parser_version" and value not in {"builtin", "unknown", None}:
                return "<installed-version>"
            if isinstance(value, dict):
                return {k: normalize(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [normalize(v) for v in value]
            if isinstance(value, str) and value.startswith(str(root)):
                return value.replace(str(root), "<root>", 1).replace("\\", "/")
            return value

        return normalize(captured)


class PDFExtractionContractTests(unittest.TestCase):
    def test_full_payload_matches_pre_split_snapshot(self) -> None:
        self.assertEqual(capture(), json.loads(GOLDEN.read_text(encoding="utf-8")))

    def test_fixture_exercises_native_and_cross_page_paths(self) -> None:
        result = capture()
        self.assertEqual(len(result["pymupdf_pages"]), 2)
        self.assertEqual(result["simple_native"]["source_files"][0]["pdf_profile"]["parser"], "simple_pdf_text")
        self.assertTrue(result["simple_native"]["paragraphs"])
        self.assertEqual(result["manual_native"]["pdf_pages"][0]["citation_page"], "27")
        self.assertEqual(result["mineru_split"]["pdf_pages"][1]["text_raw"], "下一页正文。\n下一段。")
        cross = next(p for p in result["unicode_cross_page"] if p["is_cross_page"])
        self.assertEqual(len(cross["text_source_spans"]), 2)
        self.assertIn("𠮷", cross["text_raw"])


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--regen"]:
        GOLDEN.write_text(json.dumps(capture(), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    else:
        unittest.main()
