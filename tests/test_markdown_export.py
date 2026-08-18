from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.me_finder.database import build_database
from src.me_finder.document_export_service import (
    IndexedDocumentNotFound,
    UnsupportedDocumentExport,
    export_indexed_pdf_markdown,
)
from src.me_finder.markdown_export import (
    document_to_markdown,
    page_to_markdown,
    safe_markdown_filename,
)


class MarkdownExportCoreTests(unittest.TestCase):
    def test_heading_levels_and_body_order(self) -> None:
        pages = [
            {
                "pdf_page_index": 0,
                "text_raw": "第一章 法兰克福学派\n正文一",
                "blocks": [
                    {"text_level": 1, "text": "第一章 法兰克福学派"},
                    {"text_level": None, "text": "正文一"},
                ],
            },
            {
                "pdf_page_index": 1,
                "text_raw": "核心集体\n正文二\n小节\n正文三",
                "blocks": [
                    {"text_level": 2, "text": "核心集体"},
                    {"text_level": None, "text": "正文二"},
                    {"text_level": 3, "text": "小节"},
                    {"text_level": None, "text": "正文三"},
                ],
            },
        ]
        markdown = document_to_markdown(pages)
        self.assertIn("# 第一章 法兰克福学派", markdown)
        self.assertIn("## 核心集体", markdown)
        self.assertIn("### 小节", markdown)
        self.assertLess(
            markdown.index("正文一"),
            markdown.index("正文二"),
        )
        self.assertLess(
            markdown.index("正文二"),
            markdown.index("正文三"),
        )
        self.assertEqual(markdown.count("正文一"), 1)
        self.assertEqual(markdown.count("正文二"), 1)

    def test_heading_levels_1_to_6(self) -> None:
        page = {
            "pdf_page_index": 0,
            "text_raw": "\n".join(f"标题{level}" for level in range(1, 7)),
            "blocks": [
                {"text_level": level, "text": f"标题{level}"}
                for level in range(1, 7)
            ],
        }
        markdown = page_to_markdown(page)
        for level in range(1, 7):
            self.assertIn(f"{'#' * level} 标题{level}", markdown)
        self.assertNotIn("####### ", markdown)

    def test_utf8_chinese_round_trip(self) -> None:
        markdown = document_to_markdown(
            [
                {
                    "pdf_page_index": 0,
                    "text_raw": "牛津通识读本·批判理论 正文",
                    "blocks": [{"text_level": None, "text": "牛津通识读本·批判理论 正文"}],
                }
            ],
            title="牛津通识读本·批判理论",
            author="斯蒂芬·埃里克·布朗纳",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "批判理论.md"
            path.write_text(markdown, encoding="utf-8")
            loaded = path.read_text(encoding="utf-8")
        self.assertIn("牛津通识读本·批判理论", loaded)
        self.assertIn("斯蒂芬·埃里克·布朗纳", loaded)

    def test_page_markers_use_existing_mapping(self) -> None:
        page = {
            "pdf_page_index": 30,
            "pdf_page_number_1based": 31,
            "printed_page": "23",
            "citation_page": "23",
            "text_raw": "正文",
            "blocks": [{"text": "正文"}],
        }
        markdown = page_to_markdown(page)
        self.assertIn("<!-- pdf_page: 31 | printed_page: 23 -->", markdown)
        self.assertLess(markdown.index("<!--"), markdown.index("正文"))

        plain = page_to_markdown(
            {
                "pdf_page_index": 31,
                "text_raw": "另一页",
                "blocks": [{"text": "另一页"}],
            }
        )
        self.assertIn("<!-- pdf_page: 32 -->", plain)

    def test_missing_author_and_title_are_omitted_from_frontmatter(self) -> None:
        markdown = document_to_markdown([], title="只有标题")
        self.assertIn("title: \"只有标题\"", markdown)
        self.assertNotIn("author:", markdown)
        self.assertNotIn("None", markdown)
        self.assertNotIn("null", markdown)

        sparse = document_to_markdown([])
        self.assertIn("source: MEFinder", sparse)
        self.assertNotIn("title:", sparse)
        self.assertNotIn("author:", sparse)

    def test_filename_sanitizes_windows_illegal_characters(self) -> None:
        name = safe_markdown_filename('批判理论：导论?/*<>|"测试')
        self.assertTrue(name.endswith(".md"))
        self.assertNotRegex(name, r'[\\/:*?"<>|]')
        self.assertEqual(safe_markdown_filename(""), "MEFinder-document.md")

    def test_unaligned_blocks_fall_back_to_whole_page_text(self) -> None:
        page = {
            "pdf_page_index": 0,
            "text_raw": "不可靠的整页正文",
            "blocks": [
                {"text_level": 1, "text": "猜出来的标题"},
            ],
        }
        markdown = page_to_markdown(page)
        self.assertIn("不可靠的整页正文", markdown)
        self.assertNotIn("# 猜出来的标题", markdown)
        self.assertNotIn("猜出来的标题", markdown)

    def test_aligned_blocks_keep_exact_text_and_headings(self) -> None:
        page = {
            "pdf_page_index": 0,
            "text_raw": "第一章 标题\n正文",
            "blocks": [
                {"text_level": 1, "text": "第一章 标题"},
                {"text_level": None, "text": "正文"},
            ],
        }
        markdown = page_to_markdown(page)
        self.assertIn("# 第一章 标题", markdown)
        self.assertIn("正文", markdown)

    def test_header_type_without_text_level_is_plain_text(self) -> None:
        page = {
            "pdf_page_index": 0,
            "text_raw": "无层级标题\n正文",
            "blocks": [
                {"type": "header", "text": "无层级标题"},
                {"type": "text", "text": "正文"},
            ],
        }
        markdown = page_to_markdown(page)
        self.assertNotIn("# 无层级标题", markdown)
        self.assertIn("无层级标题", markdown)
        self.assertIn("正文", markdown)


def markdown_fixture(
    root: Path,
    *,
    source_type: str = "pdf",
) -> tuple[Path, str]:
    source_id = "source-markdown-1"
    source_path = root / "corpus" / "raw_pdf" / "批判理论.pdf"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source bytes")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    pages = [
        {
            "source_file_id": source_id,
            "pdf_page_index": 0,
            "physical_pdf_page": 1,
            "pdf_page_number_1based": 1,
            "printed_page": "1",
            "citation_page": "1",
            "text_raw": "第一章 法兰克福学派\n正文第一页",
            "blocks": [
                {"text_level": 1, "text": "第一章 法兰克福学派"},
                {"text_level": None, "text": "正文第一页"},
            ],
            "parser": "mineru",
            "parser_version": "v1",
        },
        {
            "source_file_id": source_id,
            "pdf_page_index": 1,
            "physical_pdf_page": 2,
            "pdf_page_number_1based": 2,
            "text_raw": "第二章 方法问题\n正文第二页",
            "blocks": [
                {"text_level": 1, "text": "第二章 方法问题"},
                {"text_level": None, "text": "正文第二页"},
            ],
            "parser": "mineru",
            "parser_version": "v1",
        },
    ]
    database = root / "data" / "index.sqlite3"
    build_database(
        {
            "metadata": {},
            "source_files": [
                {
                    "source_file_id": source_id,
                    "source_type": source_type,
                    "document_id": "DOCUMENT_MARKDOWN_1",
                    "file_name": source_path.name,
                    "relative_path": "corpus/raw_pdf/批判理论.pdf",
                    "file_format": source_type,
                    "size_bytes": source_path.stat().st_size,
                    "sha256": digest,
                    "display_title": "批判理论",
                    "bibliographic_metadata": {
                        "title": "批判理论",
                        "author": "马克斯·霍克海默",
                    },
                    "pdf_profile": {
                        "pdf_page_count": 2,
                        "parser": "mineru",
                        "detected_pdf_type": "mineru_structured",
                    },
                }
            ],
            "volumes": [
                {
                    "volume_id": "VOLUME_MARKDOWN_1",
                    "source_file_id": source_id,
                    "source_type": source_type,
                    "volume_number": 1,
                    "display_title": "批判理论",
                }
            ],
            "pdf_pages": pages if source_type == "pdf" else [],
        },
        database,
    )
    return database, source_id


class MarkdownExportServiceTests(unittest.TestCase):
    def test_service_exports_utf8_markdown_with_metadata_and_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = markdown_fixture(root)
            result = export_indexed_pdf_markdown(
                database_path=database,
                source_file_id=source_id,
                output_dir=root / "exports",
            )

            self.assertEqual(result["page_count"], 2)
            self.assertTrue(Path(result["path"]).is_file())
            self.assertEqual(
                Path(result["path"]).name,
                "批判理论.md",
            )
            content = Path(result["path"]).read_text(encoding="utf-8")
            self.assertIn('title: "批判理论"', content)
            self.assertIn('author: "马克斯·霍克海默"', content)
            self.assertIn("source: MEFinder", content)
            self.assertIn("# 第一章 法兰克福学派", content)
            self.assertIn("正文第一页", content)
            self.assertIn("# 第二章 方法问题", content)
            self.assertIn("正文第二页", content)
            self.assertIn(
                "<!-- pdf_page: 1 | printed_page: 1 -->",
                content,
            )
            self.assertIn("<!-- pdf_page: 2 -->", content)
            self.assertFalse(Path(str(result["path"]) + ".partial").exists())

    def test_service_missing_and_unsupported_sources_have_clear_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, _source_id = markdown_fixture(root, source_type="word")
            with self.assertRaises(IndexedDocumentNotFound):
                export_indexed_pdf_markdown(
                    database_path=database,
                    source_file_id="missing",
                    output_dir=root / "exports",
                )
            with self.assertRaises(UnsupportedDocumentExport):
                export_indexed_pdf_markdown(
                    database_path=database,
                    source_file_id="source-markdown-1",
                    output_dir=root / "exports",
                )


if __name__ == "__main__":
    unittest.main()
