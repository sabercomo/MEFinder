from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.pdf_extractors import (
    PDFTextPage,
    extract_pdf_source,
    load_mineru_pdf_pages,
    make_pdf_paragraphs,
    pdf_page_text_hash,
    strip_pdf_page_header_for_cross,
)


def make_page(index: int, text: str) -> dict[str, object]:
    return {
        "pdf_page_id": f"pdf-anchor-PAGE-{index:06d}",
        "source_file_id": "pdf-anchor",
        "document_id": "pdf-anchor",
        "pdf_page_index": index,
        "pdf_page_number_1based": index + 1,
        "pdf_page_label": None,
        "citation_page": None,
        "page_mapping_method": "uncalibrated",
        "page_mapping_confidence": 0.0,
        "text_raw": text,
        "page_text_hash": pdf_page_text_hash(text),
    }


def make_paragraphs(pages: list[dict[str, object]]) -> list[dict[str, object]]:
    return make_pdf_paragraphs(
        "pdf-anchor",
        "pdf-anchor",
        "Anchor test",
        None,
        "anchor.pdf",
        pages,
        "pdf-anchor-W0001",
    )


class PDFTextSourceSpanTests(unittest.TestCase):
    def assert_spans_match_pages(
        self,
        paragraphs: list[dict[str, object]],
        pages: list[dict[str, object]],
    ) -> None:
        pages_by_id = {str(page["pdf_page_id"]): page for page in pages}
        for paragraph in paragraphs:
            paragraph_text = str(paragraph["text_raw"])
            for span in paragraph["text_source_spans"]:
                page_text = str(pages_by_id[str(span["pdf_page_id"])]["text_raw"])
                paragraph_slice = paragraph_text[
                    int(span["paragraph_char_start"]) : int(span["paragraph_char_end"])
                ]
                page_slice = page_text[int(span["page_char_start"]) : int(span["page_char_end"])]
                self.assertEqual(paragraph_slice, page_slice)
                self.assertEqual(span["offset_unit"], "unicode_codepoint")
                self.assertEqual(
                    span["page_text_hash"],
                    pages_by_id[str(span["pdf_page_id"])]["page_text_hash"],
                )

    def test_single_page_span_uses_original_codepoint_offsets(self) -> None:
        raw_text = "\r\n  开😀始\u2028下一行  \t"
        pages = [make_page(0, raw_text)]

        paragraphs = make_paragraphs(pages)

        self.assertEqual(len(paragraphs), 1)
        paragraph = paragraphs[0]
        self.assertEqual(paragraph["text_raw"], raw_text.strip())
        self.assertEqual(len(paragraph["text_source_spans"]), 1)
        span = paragraph["text_source_spans"][0]
        expected_start = len(raw_text) - len(raw_text.lstrip())
        expected_end = len(raw_text.rstrip())
        self.assertEqual(span["paragraph_char_start"], 0)
        self.assertEqual(span["paragraph_char_end"], len(raw_text.strip()))
        self.assertEqual(span["page_char_start"], expected_start)
        self.assertEqual(span["page_char_end"], expected_end)
        self.assert_spans_match_pages(paragraphs, pages)

    def test_cross_spans_preserve_crlf_unicode_separator_and_emoji_offsets(self) -> None:
        left_text = "  " + ("左页内容" * 280) + "😀结尾\r\n"
        header = "\r\n12\r\nRUNNING😀HEAD\u2028"
        right_body = ("右页正文😀" * 60) + "\r\n保留 CRLF\u2028也保留分行符"
        right_text = header + right_body + "\t"
        pages = [make_page(0, left_text), make_page(1, right_text)]

        paragraphs = make_paragraphs(pages)
        cross = next(item for item in paragraphs if item["is_cross_page"])
        left_span, right_span = cross["text_source_spans"]

        self.assertEqual(len(cross["text_source_spans"]), 2)
        left_body_end = len(left_text.rstrip())
        self.assertEqual(
            left_span["page_char_start"],
            max(len(left_text) - len(left_text.lstrip()), left_body_end - 900),
        )
        self.assertEqual(left_span["page_char_end"], left_body_end)
        self.assertEqual(right_span["page_char_start"], len(header))
        # Python offsets count the supplementary-plane emoji as one codepoint.
        self.assertEqual(len(header), right_text.index(right_body))
        self.assertEqual(
            cross["text_raw"][left_span["paragraph_char_end"]],
            "\n",
            "The synthetic separator intentionally has no page mapping.",
        )
        right_slice = cross["text_raw"][
            right_span["paragraph_char_start"] : right_span["paragraph_char_end"]
        ]
        self.assertIn("\r\n", right_slice)
        self.assertIn("\u2028", right_slice)
        self.assert_spans_match_pages(paragraphs, pages)

    def test_header_strip_returns_a_contiguous_original_slice(self) -> None:
        header = " \r\n9\r\nRUNNING HEAD\u2028"
        body = "正文第一行\r\n正文第二行\u2028正文第三行"
        page_text = header + body + "\t "

        stripped, start, end = strip_pdf_page_header_for_cross(page_text)

        self.assertEqual(start, len(header))
        self.assertEqual(end, len(page_text.rstrip()))
        self.assertEqual(stripped, page_text[start:end])
        self.assertIn("\r\n", stripped)
        self.assertIn("\u2028", stripped)

    def test_blank_page_keeps_no_paragraph_and_blocks_cross_windows(self) -> None:
        pages = [
            make_page(0, "甲" * 100),
            make_page(1, " \r\n\u2028\t"),
            make_page(2, "乙" * 100),
        ]

        paragraphs = make_paragraphs(pages)

        self.assertEqual([item["paragraph_id"] for item in paragraphs], [
            "pdf-anchor-P000000",
            "pdf-anchor-P000002",
        ])
        self.assertFalse(any(item["is_cross_page"] for item in paragraphs))
        self.assert_spans_match_pages(paragraphs, pages)


class PDFPageTextHashTests(unittest.TestCase):
    def test_page_text_hash_is_utf8_sha256_prefix(self) -> None:
        text = "正文😀\r\n第二行\u2028"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(pdf_page_text_hash(text), expected)

    def test_native_page_record_contains_page_text_hash(self) -> None:
        raw_text = "原生 PDF 😀 文本" * 20
        profile = {
            "detected_pdf_type": "native_text",
            "parser": "pymupdf",
            "parser_version": "test",
            "pdf_page_count": 1,
            "notes": [],
        }
        auto_mapping = {
            "segments": [],
            "selected_segments": [],
            "applied_segments": [],
            "method": "uncalibrated",
            "mapping_status": "uncalibrated",
            "failure_reasons": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "native.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            native_page = PDFTextPage(
                pdf_page_index=0,
                pdf_page_label=None,
                raw_text=raw_text,
                blocks=[],
                parser="pymupdf",
                parser_version="test",
            )
            with (
                patch("src.me_finder.pdf_extractors.detect_pdf_type", return_value=profile),
                patch(
                    "src.me_finder.pdf_extractors.extract_native_pdf_pages",
                    return_value=[native_page],
                ),
                patch(
                    "src.me_finder.pdf_extractors.PageMappingService.infer",
                    return_value=auto_mapping,
                ),
            ):
                extracted = extract_pdf_source(
                    pdf_path,
                    root,
                    {
                        "source_file_id": "pdf-native-stable",
                        "document_id": "pdf-native-stable",
                    },
                )
            result_dir = root / "structured-result"
            result_dir.mkdir()
            (result_dir / "content_list.json").write_text(
                json.dumps(
                    [{"page_idx": 0, "type": "text", "text": "MinerU 重解析文本"}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch(
                "src.me_finder.pdf_extractors.get_pdf_page_labels",
                return_value=[None],
            ):
                reparsed_pages = load_mineru_pdf_pages(
                    pdf_path,
                    "pdf-native-stable",
                    "pdf-native-stable",
                    {},
                    [
                        {
                            "result_dir": str(result_dir),
                            "page_ranges": "1-1",
                            "parser": "mineru",
                            "provider_name": "mineru",
                        }
                    ],
                )

        page = extracted["pdf_pages"][0]
        self.assertEqual(page["page_text_hash"], pdf_page_text_hash(raw_text))
        self.assertEqual(
            page["pdf_page_id"],
            "pdf-native-stable-PAGE-000000",
            "The persisted config ID must win over any recomputed file hash.",
        )
        self.assertEqual(page["pdf_page_id"], reparsed_pages[0]["pdf_page_id"])
        self.assertNotEqual(
            page["page_text_hash"],
            reparsed_pages[0]["page_text_hash"],
        )

    def test_mineru_and_visual_page_records_contain_hash_including_blank_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "structured.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            result_dir = root / "result"
            result_dir.mkdir()
            (result_dir / "content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "page_idx": 0,
                            "type": "text",
                            "text": "结构化正文😀",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            for parser in ("mineru", "vision_api"):
                with self.subTest(parser=parser), patch(
                    "src.me_finder.pdf_extractors.get_pdf_page_labels",
                    return_value=[None, None],
                ):
                    pages = load_mineru_pdf_pages(
                        pdf_path,
                        f"pdf-{parser}",
                        f"pdf-{parser}",
                        {},
                        [
                            {
                                "result_dir": str(result_dir),
                                "page_ranges": "1-2",
                                "parser": parser,
                                "provider_name": parser,
                            }
                        ],
                    )

                self.assertEqual(len(pages), 2)
                self.assertEqual(
                    pages[0]["page_text_hash"],
                    pdf_page_text_hash(str(pages[0]["text_raw"])),
                )
                self.assertEqual(pages[1]["text_raw"], "")
                self.assertEqual(pages[1]["page_text_hash"], pdf_page_text_hash(""))


if __name__ == "__main__":
    unittest.main()
