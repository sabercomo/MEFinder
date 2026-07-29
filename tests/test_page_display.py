import unittest

from src.me_finder.page_display import (
    build_page_display,
    page_source_note,
)


class PageDisplayTests(unittest.TestCase):
    def test_calibrated_pdf_methods_use_only_citation_pages(self) -> None:
        cases = {
            "calibrated": "PDF 引用页码已校准",
            "manual_page": "PDF 页码来自人工逐页校准",
            "fixed_offset": "PDF 页码来自固定偏移映射",
            "manual_segment": "PDF 页码来自人工分段映射",
            "printed_page_ocr": "PDF 页码来自视觉印刷页码识别，已验证",
        }
        for source_type, note in cases.items():
            with self.subTest(source_type=source_type):
                result = build_page_display(
                    {
                        "source_type": "pdf",
                        "page_source_type": source_type,
                        "citation_page_start": "38",
                        "citation_page_end": "39",
                        # This must remain context only, never the citation page.
                        "pdf_page_start_index": 51,
                        "pdf_page_end_index": 52,
                    }
                )
                self.assertEqual(result.display, "引用页码：38–39")
                self.assertEqual(result.note, note)
                self.assertEqual(result.page_source_type, source_type)

    def test_pdf_page_label_without_citation_is_explicitly_unverified(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_source_type": "pdf_page_label",
                "pdf_page_label": "vii",
                "pdf_page_index": 11,
            }
        )
        self.assertEqual(result.display, "PDF 标签页：vii，引用页码尚未校准")
        self.assertEqual(result.note, "PDF Page Label 尚未验证，不能作为引用页码")

    def test_verified_pdf_page_label_must_have_a_citation_page(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_mapping_method": "pdf_page_label",
                "citation_page": "38",
                "pdf_page_label": "38",
                "pdf_page_index": 51,
            }
        )
        self.assertEqual(result.display, "引用页码：38")
        self.assertEqual(result.note, "PDF Page Label，已抽样验证")

    def test_explicit_verification_flag_prevents_page_label_promotion(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_source_type": "pdf_page_label",
                "citation_page": "38",
                "pdf_page_label": "38",
                "citation_page_verified": False,
            }
        )
        self.assertEqual(result.display, "PDF 标签页：38，引用页码尚未校准")

    def test_explicit_unverified_mapping_never_keeps_a_verified_note(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_source_type": "printed_page_ocr",
                "citation_page": "38",
                "citation_page_verified": False,
                "pdf_page_index": 51,
            }
        )
        self.assertEqual(result.display, "PDF 第 52 页，引用页码尚未校准")
        self.assertEqual(result.note, "页码映射尚未验证，不能作为引用页码")

    def test_uncalibrated_pdf_shows_physical_page_with_warning(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_source_type": "uncalibrated",
                "pdf_page_start_index": 11,
                "pdf_page_end_index": 12,
                # A stale display string must not be reused as a citation page.
                "page_display": "引用页码：999",
            }
        )
        self.assertEqual(result.display, "PDF 第 12–13 页，引用页码尚未校准")
        self.assertEqual(result.note, "PDF 引用页码尚未校准")

    def test_uncalibrated_pdf_prefers_unverified_label_to_physical_page(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_source_type": "uncalibrated",
                "pdf_page_start_label": "7",
                "pdf_page_start_index": 11,
            }
        )
        self.assertEqual(result.display, "PDF 标签页：7，引用页码尚未校准")
        self.assertEqual(result.note, "PDF Page Label 尚未验证，不能作为引用页码")

    def test_docx_section_break_is_never_presented_as_verified(self) -> None:
        result = build_page_display(
            {
                "source_type": "word",
                "page_source_type": "section_break_inferred",
                "original_page_start": "38",
            }
        )
        self.assertEqual(result.display, "第 38 页（分节推断，未验证）")
        self.assertEqual(result.note, "分节推断页码，尚未人工验证")

    def test_docx_legacy_wrapped_page_display_remains_compatible(self) -> None:
        result = build_page_display(
            {
                "source_type": "word",
                "page_source_type": "section_break_inferred",
                "page_display": "第197页",
            }
        )
        self.assertEqual(result.display, "第 197 页（分节推断，未验证）")

    def test_legacy_doc_toc_range_is_not_paragraph_precision(self) -> None:
        result = build_page_display(
            {
                "source_type": "word",
                "page_source_type": "toc_range_bound",
                "toc_page_start": "38",
                "toc_page_end": "45",
            }
        )
        self.assertEqual(result.display, "目录范围 38–45（非段落精确页码）")
        self.assertEqual(result.note, "目录页码范围，非段落级精确页码")

    def test_legacy_doc_existing_page_display_is_supported(self) -> None:
        result = build_page_display(
            {
                "source_type": "word",
                "page_source_type": "toc_range_bound",
                "page_display": "38-45",
            }
        )
        self.assertEqual(result.display, "目录范围 38–45（非段落精确页码）")

    def test_unknown_page_source_has_no_bare_number(self) -> None:
        result = build_page_display(
            {
                "source_type": "word",
                "page_source_type": "unknown",
                "page_display": "38",
            }
        )
        self.assertEqual(result.display, "页码尚未解析")
        self.assertEqual(result.note, "页码尚未解析")

    def test_mixed_mapping_marks_even_candidate_citation_as_unverified(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_source_type": "mixed",
                "citation_page_start": "38",
                "citation_page_end": "39",
                "pdf_page_start_index": 51,
                "pdf_page_end_index": 52,
            }
        )
        self.assertEqual(result.display, "引用页码候选：38–39（来源混合，需核验）")
        self.assertEqual(result.note, "跨页命中涉及不同页码来源，须分别核验")

    def test_mixed_mapping_without_citations_only_shows_physical_context(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_source_type": "mixed",
                "pdf_page_start_index": 51,
                "pdf_page_end_index": 52,
            }
        )
        self.assertEqual(result.display, "PDF 第 52–53 页，页码来源混合且尚未验证")

    def test_page_mapping_method_alias_is_supported(self) -> None:
        result = build_page_display(
            {
                "source_type": "pdf",
                "page_mapping_method": "manual_segment",
                "citation_page": "序言第4页",
            }
        )
        self.assertEqual(result.display, "引用页码：序言第4页")
        self.assertEqual(result.page_source_type, "manual_segment")

    def test_standard_note_has_a_safe_unknown_fallback(self) -> None:
        self.assertEqual(page_source_note("section_break_inferred"), "分节推断页码，尚未人工验证")
        self.assertEqual(page_source_note("future_mapping"), "页码来源未说明")

if __name__ == "__main__":
    unittest.main()
