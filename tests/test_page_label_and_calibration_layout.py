from __future__ import annotations

import re
import unittest

from src.me_finder.citations import format_citation
from src.me_finder.web import HTML


class CitationPageDisplayMarkupTests(unittest.TestCase):
    def setUp(self) -> None:
        # 切出「页码范围拼接 + 引用页码标签」这两个函数。以函数体自身的结尾语句为
        # 下界，而不是靠后面碰巧相邻的某个函数名——模块拆分后两者已不在同一文件。
        start = HTML.index("function formatChinesePageRange")
        end_marker = "return formatChinesePageRange(start, end);"
        end = HTML.index(end_marker, start) + len(end_marker)
        self.formatter = HTML[start:end]

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


class LibraryWorkspaceLayoutMarkupTests(unittest.TestCase):
    def test_detail_workspace_uses_a_44_56_split(self) -> None:
        # L-07：列表:详情由 36:64 调平到 44:56，列表不再被压到只剩标题。
        rule = re.search(r"\.library-body\.detail-open\s*\{([^}]+)\}", HTML, re.S)
        self.assertIsNotNone(rule)
        self.assertIn("grid-template-columns: minmax(360px, 44fr) minmax(0, 56fr);", rule.group(1))

    def test_open_detail_uses_a_single_column_document_list(self) -> None:
        self.assertIn(
            ".library-body.detail-open .library-list-container.library-view-grid { grid-template-columns: minmax(0, 1fr); }",
            HTML,
        )

    def test_narrow_window_switches_to_a_full_width_detail_panel(self) -> None:
        self.assertIn("@media (max-width: 1120px)", HTML)
        self.assertIn(".library-body.detail-open .library-list-scroll { display: none; }", HTML)
        self.assertIn(".library-body.detail-open .library-drawer.open { width: 100%;", HTML)

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
        self.assertIn(".library-row.selected", HTML)
        self.assertIn("background: var(--surface-selected);", HTML)
        self.assertIn("background: var(--accent);", HTML)


if __name__ == "__main__":
    unittest.main()
