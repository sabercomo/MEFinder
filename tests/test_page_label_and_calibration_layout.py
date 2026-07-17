from __future__ import annotations

import re
import unittest

from src.me_finder.citations import format_citation
from src.me_finder.web import HTML


class CitationPageDisplayMarkupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.formatter = HTML.split("function formatChinesePageRange", 1)[1].split("function pdRow", 1)[0]

    def test_word_numeric_page_is_formatted_as_chinese_page_label(self) -> None:
        self.assertIn("return '第' + start + (end ? '—' + end : '') + '页';", self.formatter)
        self.assertIn("item.page", self.formatter)

    def test_cross_page_range_uses_chinese_dash_and_deduplicates_same_page(self) -> None:
        self.assertIn("end === start", self.formatter)
        self.assertIn("'—'", self.formatter)
        self.assertNotIn("'–'", self.formatter)

    def test_existing_scoped_page_label_is_not_double_wrapped(self) -> None:
        self.assertIn("startMatch", self.formatter)
        self.assertIn("if (startMatch && !end) return start;", self.formatter)

    def test_missing_pdf_citation_never_falls_back_to_physical_page(self) -> None:
        self.assertIn("if (sourceType === 'pdf')", self.formatter)
        self.assertNotIn("pdf_page_index", self.formatter)
        self.assertNotIn("pdf_page_start_index", self.formatter)
        self.assertIn("return '页码尚未校准';", self.formatter)

    def test_result_list_and_detail_share_one_formatter(self) -> None:
        self.assertGreaterEqual(HTML.count("formatCitationPageLabel(item)"), 2)
        self.assertIn("pdRow('引用页码', pageLabel)", HTML)
        self.assertNotIn("esc(String(item.page || ''))", HTML)

    def test_citation_exports_keep_the_current_hit_page(self) -> None:
        metadata = {
            "document_type": "marx_engels_collection",
            "collection_title": "马克思恩格斯文集",
            "volume_number": 1,
            "publication_place": "北京",
            "publisher": "人民出版社",
            "publication_year": "2009",
        }
        self.assertEqual(
            format_citation(metadata, {"start": "4"}, "chinese"),
            "《马克思恩格斯文集》第1卷，北京：人民出版社，2009年，第4页。",
        )
        self.assertEqual(
            format_citation(metadata, {"start": "4"}, "gb"),
            "马克思恩格斯文集:第1卷[M].北京:人民出版社,2009,4.",
        )


class CalibrationWorkspaceLayoutMarkupTests(unittest.TestCase):
    def test_detail_workspace_uses_a_36_64_split(self) -> None:
        rule = re.search(r"\.calibration-body\.detail-open\s*\{([^}]+)\}", HTML, re.S)
        self.assertIsNotNone(rule)
        self.assertIn("grid-template-columns: minmax(320px, 36fr) minmax(0, 64fr);", rule.group(1))

    def test_open_detail_uses_a_single_column_document_list(self) -> None:
        self.assertIn(
            ".calibration-body.detail-open .cal-card-grid { grid-template-columns: minmax(0, 1fr);",
            HTML,
        )

    def test_narrow_window_switches_to_a_full_width_detail_panel(self) -> None:
        self.assertIn("@media (max-width: 1120px)", HTML)
        self.assertIn(".calibration-body.detail-open .cal-library-pane { display: none; }", HTML)
        self.assertIn(".calibration-body.detail-open .cal-detail-drawer.open { width: 100%;", HTML)

    def test_mapping_table_is_fixed_width_without_horizontal_scroll(self) -> None:
        table_wrap = re.search(r"\.segment-table-wrap\s*\{([^}]+)\}", HTML, re.S)
        table = re.search(r"\.segment-table\s*\{([^}]+)\}", HTML, re.S)
        self.assertIsNotNone(table_wrap)
        self.assertIsNotNone(table)
        self.assertIn("overflow: hidden;", table_wrap.group(1))
        self.assertIn("table-layout: fixed;", table.group(1))
        self.assertIn("<th>PDF 起始页</th>", HTML)
        self.assertIn("<th>范围说明</th>", HTML)
        self.assertIn('class="seg-remove"', HTML)
        self.assertIn('aria-label="删除分段"><svg', HTML)

    def test_auto_detection_has_one_primary_button(self) -> None:
        self.assertEqual(HTML.count('id="cal-auto-detect-btn"'), 1)

    def test_selected_document_has_background_border_and_accent_bar(self) -> None:
        self.assertIn(".cal-doc-card.selected::before", HTML)
        self.assertIn("background: var(--surface-selected);", HTML)
        self.assertIn("background: var(--accent);", HTML)


if __name__ == "__main__":
    unittest.main()
