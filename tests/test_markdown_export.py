from __future__ import annotations

from copy import deepcopy
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

    def test_page_markers_default_to_known_printed_pages(self) -> None:
        page = {
            "pdf_page_index": 30,
            "pdf_page_number_1based": 31,
            "printed_page": "23",
            "citation_page": "23",
            "text_raw": "正文",
            "blocks": [{"text": "正文"}],
        }
        markdown = page_to_markdown(page)
        self.assertIn("<!-- printed_page: 23 -->", markdown)
        self.assertNotIn("pdf_page", markdown)
        self.assertLess(markdown.index("<!--"), markdown.index("正文"))
        self.assertEqual(page["pdf_page_number_1based"], 31)

        # Unknown printed pages are not fabricated from physical PDF pages.
        plain = page_to_markdown(
            {
                "pdf_page_index": 31,
                "text_raw": "另一页",
                "blocks": [{"text": "另一页"}],
            }
        )
        self.assertNotIn("<!--", plain)
        self.assertIn("另一页", plain)

    def test_full_marker_mode_keeps_pdf_page(self) -> None:
        from src.me_finder.markdown_export_normalize import ExportOptions

        page = {
            "pdf_page_index": 30,
            "pdf_page_number_1based": 31,
            "printed_page": "23",
            "citation_page": "23",
            "text_raw": "正文",
            "blocks": [{"text": "正文"}],
        }
        markdown = page_to_markdown(
            page, options=ExportOptions(page_marker_mode="full")
        )
        self.assertIn("<!-- pdf_page: 31 | printed_page: 23 -->", markdown)

    def test_none_marker_mode_emits_no_anchor(self) -> None:
        from src.me_finder.markdown_export_normalize import ExportOptions

        page = {
            "pdf_page_index": 30,
            "pdf_page_number_1based": 31,
            "printed_page": "23",
            "text_raw": "正文",
            "blocks": [{"text": "正文"}],
        }
        markdown = page_to_markdown(
            page, options=ExportOptions(page_marker_mode="none")
        )
        self.assertNotIn("<!--", markdown)
        self.assertIn("正文", markdown)

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
            self.assertIn("<!-- printed_page: 1 -->", content)
            self.assertNotIn("pdf_page", content)
            self.assertFalse(Path(str(result["path"]) + ".partial").exists())

    def test_service_exports_indexed_epub_as_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "data" / "index.sqlite3"
            source_id = "epub-markdown-1"
            metadata = {"title": "电子书测试", "author": "测试作者"}
            paragraphs = [
                {
                    "paragraph_id": f"{source_id}-P{index:06d}",
                    "source_file_id": source_id,
                    "source_type": "word",
                    "source_format": "epub",
                    "volume_id": "EPUB_VOLUME_1",
                    "work_id": "EPUB_WORK_1",
                    "paragraph_index": index,
                    "text_raw": text,
                    "style_name": style,
                    "original_page_start": page,
                    "eligible_for_search": True,
                }
                for index, (text, style, page) in enumerate(
                    [
                        ("第一章", "h1", "1"),
                        ("第一页正文。", "p", "1"),
                        ("第二章", "h2", "2"),
                        ("第二页正文。", "p", "2"),
                    ]
                )
            ]
            build_database(
                {
                    "metadata": {},
                    "source_files": [
                        {
                            "source_file_id": source_id,
                            "source_type": "word",
                            "file_name": "电子书测试.epub",
                            "file_format": "epub",
                            "display_title": "电子书测试",
                            "epub_page_count": 2,
                            "bibliographic_metadata": metadata,
                        }
                    ],
                    "volumes": [
                        {
                            "volume_id": "EPUB_VOLUME_1",
                            "source_file_id": source_id,
                            "source_type": "word",
                            "display_title": "电子书测试",
                        }
                    ],
                    "works": [
                        {
                            "work_id": "EPUB_WORK_1",
                            "volume_id": "EPUB_VOLUME_1",
                            "source_type": "word",
                            "work_order": 1,
                            "title": "电子书测试",
                        }
                    ],
                    "paragraphs": paragraphs,
                },
                database,
            )

            result = export_indexed_pdf_markdown(
                database_path=database,
                source_file_id=source_id,
                output_dir=root / "exports",
            )

            self.assertEqual(result["page_count"], 2)
            self.assertEqual(result["paragraph_count"], 4)
            content = Path(result["path"]).read_text(encoding="utf-8")
            self.assertIn('title: "电子书测试"', content)
            self.assertIn('author: "测试作者"', content)
            self.assertIn("# 第一章", content)
            self.assertIn("## 第二章", content)
            self.assertIn("<!-- printed_page: 1 -->", content)
            self.assertIn("<!-- printed_page: 2 -->", content)
            self.assertLess(content.index("第一页正文。"), content.index("第二章"))
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


def _block(text, *, bbox=None, level=None, role=None):
    block = {"text": text}
    if bbox is not None:
        block["bbox"] = bbox
    if level is not None:
        block["document_heading_level"] = level
    if role is not None:
        block["mineru_type"] = role
    return block


def _page(index, printed, blocks, *, pdf_page=None):
    page = {
        "pdf_page_index": index,
        "pdf_page_number_1based": (pdf_page if pdf_page is not None else index + 1),
        "page_width": 1000,
        "page_height": 1000,
        "text_raw": "\n".join(str(b["text"]).strip() for b in blocks),
        "blocks": blocks,
    }
    if printed is not None:
        page["printed_page"] = printed
        page["citation_page"] = printed
    return page


# bbox bands on the 1000-unit canvas: top ~0.02, middle ~0.5, bottom ~0.95.
TOP = [100, 10, 400, 40]
MID = [100, 480, 900, 520]
MID2 = [100, 560, 900, 600]
BOT = [100, 950, 400, 980]


class MarkdownExportNormalizationTests(unittest.TestCase):
    def test_case1_visible_page_number_and_running_header_removed(self) -> None:
        header = "德国哲学1760—1860：观念论的遗产"
        pages = [
            _page(
                i,
                str(135 + i),
                [
                    _block(header, bbox=TOP),
                    _block(str(135 + i), bbox=[470, 10, 520, 40]),
                    _block(f"正文 A 第{i}页", bbox=MID),
                    _block(f"正文 B 第{i}页", bbox=MID2),
                ],
                pdf_page=138 + i,
            )
            for i in range(4)
        ]
        markdown = document_to_markdown(pages)
        # Running header repeated on every page is gone.
        self.assertNotIn(header, markdown)
        # The visible folio copies are gone; the printed-page anchor stays.
        self.assertIn("<!-- printed_page: 135 -->", markdown)
        self.assertIn("正文 A 第0页", markdown)
        self.assertIn("正文 B 第0页", markdown)
        # The standalone "135" line no longer appears on its own.
        self.assertNotIn("\n135\n", markdown)

    def test_case2_only_pdf_page_does_not_fabricate_printed_anchor(self) -> None:
        page = _page(2, None, [_block("封面正文", bbox=MID)], pdf_page=3)
        markdown = document_to_markdown([page])
        self.assertNotIn("pdf_page", markdown)
        self.assertNotIn("printed_page", markdown)
        self.assertIn("封面正文", markdown)

    def test_case3_roman_printed_page_is_preserved(self) -> None:
        page = _page(
            8,
            "ix",
            [_block("ix", bbox=TOP), _block("导言正文", bbox=MID)],
        )
        markdown = document_to_markdown([page])
        self.assertIn("<!-- printed_page: ix -->", markdown)
        self.assertIn("导言正文", markdown)
        # The visible "ix" folio at the top is removed, not the body.
        self.assertNotIn("\nix\n", markdown)

    def test_case4_body_year_is_not_removed(self) -> None:
        page = _page(
            10,
            "135",
            [
                _block("正文开始", bbox=MID),
                _block("1848", bbox=MID2),
                _block("正文结束", bbox=[100, 640, 900, 680]),
            ],
        )
        markdown = document_to_markdown([page])
        self.assertIn("1848", markdown)

    def test_case5_inline_number_is_not_removed(self) -> None:
        page = _page(
            11,
            "20",
            [_block("康德在这里区分了 12 个范畴。", bbox=MID)],
        )
        markdown = document_to_markdown([page])
        self.assertIn("康德在这里区分了 12 个范畴。", markdown)

    def test_case6_real_heading_survives_running_header_rule(self) -> None:
        title = "第一部分 康德与哲学革命"
        # A bookmark identifies this particular occurrence; parser level alone
        # cannot distinguish it from identically styled running headers.
        start = _page(
            0,
            "1",
            [_block(title, bbox=TOP, level=1), _block("部分正文", bbox=MID)],
        )
        start["blocks"][0]["document_heading_source"] = "pdf_outline"
        start["blocks"][0]["document_heading_level"] = 1
        # Later pages repeat the title as a plain running header at the top.
        followers = [
            _page(
                i,
                str(i + 1),
                [_block(title, bbox=TOP), _block(f"后续正文{i}", bbox=MID)],
            )
            for i in range(1, 4)
        ]
        markdown = document_to_markdown([start, *followers])
        # The real heading is kept exactly once; the plain copies are removed.
        self.assertIn(f"# {title}", markdown)
        self.assertEqual(markdown.count(title), 1)
        self.assertIn("部分正文", markdown)
        self.assertIn("后续正文1", markdown)

    def test_case7_heading_folio_prefix_stripped_only_when_matching(self) -> None:
        matching = _page(
            23,
            "24",
            [_block("24 纯粹直观", bbox=TOP, level=2), _block("小节正文", bbox=MID)],
        )
        markdown = document_to_markdown([matching])
        self.assertIn("## 纯粹直观", markdown)
        self.assertNotIn("## 24 纯粹直观", markdown)

        # A real title that merely starts with a year is never altered.
        yearish = _page(
            50,
            "51",
            [_block("1844年经济学哲学手稿", bbox=TOP, level=1)],
        )
        markdown2 = document_to_markdown([yearish])
        self.assertIn("# 1844年经济学哲学手稿", markdown2)

    def test_case8_frontmatter_without_printed_page_has_no_anchor(self) -> None:
        page = _page(
            0,
            None,
            [_block("版权页内容", bbox=MID), _block("ISBN 7-5366-0898-5", bbox=MID2)],
            pdf_page=1,
        )
        markdown = document_to_markdown([page])
        self.assertNotIn("printed_page", markdown)
        self.assertNotIn("pdf_page", markdown)
        self.assertIn("版权页内容", markdown)
        self.assertIn("ISBN 7-5366-0898-5", markdown)

    def test_parser_tagged_page_number_role_is_removed(self) -> None:
        page = _page(
            5,
            "6",
            [
                _block("6", bbox=MID, role="page_number"),
                _block("正文内容", bbox=MID2),
            ],
        )
        markdown = document_to_markdown([page])
        self.assertIn("正文内容", markdown)
        self.assertNotIn("\n6\n", markdown)

    def test_old_library_pages_export_without_enrichment(self) -> None:
        # Pages with neither document_heading_* nor bbox still export cleanly via
        # the reading-order fallback for top/bottom regions.
        pages = [
            {
                "pdf_page_index": 0,
                "pdf_page_number_1based": 1,
                "printed_page": "1",
                "citation_page": "1",
                "text_raw": "第一章 标题\n正文",
                "blocks": [
                    {"text_level": 1, "text": "第一章 标题"},
                    {"text": "正文"},
                ],
            }
        ]
        markdown = document_to_markdown(pages)
        self.assertIn("<!-- printed_page: 1 -->", markdown)
        self.assertIn("# 第一章 标题", markdown)
        self.assertIn("正文", markdown)


def footnote_fixture():
    return [
        _page(9, "1", [
            _block("第一部", level=1),
            _block("第一章 起点", level=2),
            _block("先引用②，再引用①。", bbox=MID),
            _block("① 相同文献。", role="page_footnote", bbox=BOT),
            _block("② 另一条注释。", role="page_footnote", bbox=BOT),
        ]),
        _page(10, "2", [
            _block("第一节 小节不重置", level=3),
            _block("下一页重新出现①。", bbox=MID),
            _block("① 相同文献。", role="page_footnote", bbox=BOT),
        ]),
        _page(11, "3", [
            _block("第二部", level=1),
            _block("第二部的副标题"),
            _block("Chapter II Next", level=2),
            _block("新章正文[1]。", bbox=MID),
            _block("[1] 多段注释第一段。\n\n第二段 <原样> & 保留。", role="page_footnote", bbox=BOT),
        ]),
    ]


class FootnoteNormalizationTests(unittest.TestCase):
    def test_parent_boundary_flushes_notes_even_without_a_following_chapter(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_export

        for parent in ("篇", "部"):
            for next_chapter in (False, True):
                with self.subTest(parent=parent, next_chapter=next_chapter):
                    pages = [
                        _page(0, "1", [
                            _block(f"第一{parent}", level=1), _block("第一章", level=2),
                            _block("上一章正文①。", bbox=MID),
                            _block("① 上一章注释。", role="page_footnote", bbox=BOT),
                        ]),
                        _page(1, "2", [_block(f"第二{parent}", level=1), _block("父级副标题")]),
                        _page(2, "3", []),
                    ]
                    if next_chapter:
                        pages.append(_page(3, "4", [_block("第一章 下一篇", level=2)]))
                    doc = normalize_document_export(pages)
                    note, = [i for i in doc.items if isinstance(i, Footnote)]
                    order = [i.text for i in doc.items if hasattr(i, "text")]
                    self.assertLess(order.index("上一章正文①。"), order.index(note.text))
                    self.assertLess(order.index(note.text), order.index(f"第二{parent}"))
                    markdown = document_to_markdown(pages, normalized=doc)
                    self.assertLess(markdown.index(f"[^{note.note_id}]:"), markdown.index("<!-- printed_page: 2 -->"))
                    if next_chapter:
                        self.assertLess(markdown.index("父级副标题"), markdown.index("## 第一章 下一篇"))

    def test_located_heading_moves_before_its_body_without_sorting_paragraphs(self):
        from src.me_finder.document_heading import apply_heading_assignments
        from src.me_finder.export_footnotes import Footnote, normalize_document_export

        for source, title in (("parser", "第一章 方法"), ("document_toc", "前言")):
            with self.subTest(source=source):
                heading = _block(title, bbox=[124, 250, 276, 280])
                if source == "parser":
                    heading["text_level"] = 1
                else:
                    heading["mineru_type"] = "header"
                pages = [_page(12, "4", [
                    _block("前一节结束。", bbox=[100, 100, 900, 180]),
                    _block("本节第一段①。", bbox=MID2),
                    _block("本节第二段，保留解析相对顺序。", bbox=MID),
                    heading,
                    _block("① 注释原文。", role="page_footnote", bbox=BOT),
                ])]
                if source == "document_toc":
                    apply_heading_assignments(pages, [{"pdf_page_index": 12, "block_index": 3,
                        "level": 1, "printed_page": "4", "title": title}], source)
                before = deepcopy(pages)
                doc = normalize_document_export(pages)
                self.assertEqual([i.text for i in doc.items if hasattr(i, "text")], [
                    "前一节结束。", title, "本节第一段①。", "本节第二段，保留解析相对顺序。", "注释原文。",
                ])
                note, = [i for i in doc.items if isinstance(i, Footnote)]
                self.assertEqual(note.source_block_index, 4)
                self.assertTrue(note.reference_ids[0].endswith("-ref-1-5"))
                self.assertEqual(pages, before)
                markdown = document_to_markdown(pages, normalized=doc)
                self.assertLess(markdown.index("前一节结束。"), markdown.index("# " + title))
                self.assertLess(markdown.index("# " + title), markdown.index("本节第一段"))

    def test_heading_without_unambiguous_geometry_does_not_move(self):
        from src.me_finder.export_footnotes import normalize_document_export

        for bbox in (None, [100, 490, 400, 530], [910, 200, 980, 240]):
            with self.subTest(bbox=bbox):
                pages = [_page(0, "1", [
                    _block("保持正文位置。", bbox=MID), _block("第一章", level=1, bbox=bbox),
                ])]
                doc = normalize_document_export(pages)
                self.assertEqual([i.text for i in doc.items if hasattr(i, "text")], ["保持正文位置。", "第一章"])

    def test_part_flush_does_not_pull_later_references_forward_or_reset_numbers(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_export

        pages = [_page(i, str(i + 1), [
            _block(title, level=1), _block(f"正文{i}①。", bbox=MID),
            _block(f"① 注释{i}。", role="page_footnote", bbox=BOT),
        ]) for i, title in enumerate(("第一篇", "第二篇"))]
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
            doc = normalize_document_export(pages)
        self.assertEqual([i.text for i in doc.items if hasattr(i, "text")], [
            "第一篇", "正文0①。", "注释0。", "第二篇", "正文1①。", "注释1。",
        ])
        self.assertEqual([i.display_number for i in doc.items if isinstance(i, Footnote)], [1, 2])

    def test_explicit_cross_page_source_is_rejected_without_splitting_or_pairing(self):
        from src.me_finder.export_footnotes import normalize_document_export
        for position in (1, 2):
            pages = [_page(0, "1", [
                _block("第一章", level=1), _block("本页正文和合并来的文字①。", bbox=MID),
                _block("① 注释原文", bbox=BOT, role="page_footnote"),
            ])]
            pages[0]["blocks"][position]["cross_page"] = True
            with self.subTest(position=position), self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
                doc = normalize_document_export(pages)
            self.assertEqual(doc.footnote_report["matched_ref_count"], 0)
            self.assertEqual(doc.footnote_report["unresolved_reason"], {
                "ref": {"CROSS_PAGE_SOURCE_BLOCK": 1}, "note": {"CROSS_PAGE_SOURCE_BLOCK": 1},
            })
            self.assertEqual([i.text for i in doc.items if hasattr(i, "text")], [b["text"] for b in pages[0]["blocks"]])

    def test_100_parser_headings_are_running_headers_not_chapters(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_export
        from src.me_finder.markdown_export_normalize import ExportOptions

        for role in ("header", "text"):
            pages = [_page(i, str(i + 1), [
                _block("第一章 方法", bbox=TOP, level=1, role=role),
                _block(f"正文{i}①。", bbox=MID),
                _block("① 同上。", bbox=BOT, role="page_footnote"),
            ]) for i in range(100)]
            for page in pages:
                header = page["blocks"][0]
                header["text_level"] = header.pop("document_heading_level")
            original = deepcopy(pages)
            with self.subTest(role=role), self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
                doc = normalize_document_export(pages)
                kept = normalize_document_export(pages, options=ExportOptions(remove_running_headers=False))
            self.assertEqual([n.display_number for n in doc.items if isinstance(n, Footnote)], list(range(1, 101)))
            self.assertEqual(doc.footnote_report["numbering_scope_count"], 1)
            self.assertEqual(len(doc.footnote_report["heading_issues"]), 100)
            self.assertNotIn("第一章 方法", document_to_markdown(pages, normalized=doc))
            self.assertFalse(any(b.is_heading for b in kept.items if hasattr(b, "is_heading")))
            self.assertEqual(doc.footnote_report, kept.footnote_report)
            self.assertEqual(pages, original)

    def test_report_counts_and_locations_preserve_unresolved_entities(self):
        from src.me_finder.export_footnotes import normalize_document_export

        pages = [_page(10, "4", [
            _block("第一章 方法", level=1),
            _block("正文①。缺注②。重复③③。", bbox=MID),
            _block("① 可靠注释", role="page_footnote", bbox=BOT),
            _block("③ 不猜多引用", role="page_footnote", bbox=BOT),
            _block("④ 没有引用", role="page_footnote", bbox=BOT),
        ])]
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
            doc = normalize_document_export(pages)
        report = doc.footnote_report
        self.assertEqual([report[k] for k in ("candidate_ref_count", "matched_ref_count", "unresolved_ref_count")], [4, 1, 3])
        self.assertEqual([report[k] for k in ("candidate_note_count", "matched_note_count", "unresolved_note_count")], [3, 1, 2])
        self.assertEqual(report["unresolved_reason"], {
            "ref": {"NO_NOTE_BODY": 1, "DUPLICATE_MARKER_ON_PAGE": 2},
            "note": {"DUPLICATE_MARKER_ON_PAGE": 1, "NO_REFERENCE": 1},
        })
        self.assertEqual(report["match_reason"], {"ref": {"SAME_PAGE_UNIQUE_MARKER": 1}, "note": {"SAME_PAGE_UNIQUE_MARKER": 1}})
        self.assertEqual(report["scopes"][0]["number_range"], [1, 1])
        self.assertEqual(report["scopes"][0]["source_printed_page"], "4")
        for record in report["candidates"]:
            self.assertEqual(record["source_physical_page"], 11)
            self.assertEqual(record["text"], pages[0]["blocks"][record["source_block_index"]]["text"])
            if record["kind"] == "ref":
                self.assertEqual(record["text"][record["start"]:record["end"]], record["marker"])
            if record["status"] == "unresolved":
                self.assertTrue(any(getattr(item, "text", None) == record["text"] for item in doc.items))
        json.dumps(report, ensure_ascii=False)  # complete report is transport-ready

    def test_scope_policy_uses_chapters_not_heading_depth_or_note_count(self):
        from src.me_finder.export_footnotes import normalize_document_export

        pages = footnote_fixture()
        # Deliberately flat parser styling for parts, chapters and sections.
        for page in pages:
            for block in page["blocks"]:
                if block.get("document_heading_level"):
                    block.pop("document_heading_level")
                    block["text_level"] = 1
        doc = normalize_document_export(pages)
        scopes = doc.footnote_report["scopes"]
        self.assertEqual([(s["part_title"], s["number_range"]) for s in scopes], [("第一部", [1, 3]), ("第二部", [1, 1])])
        self.assertEqual(doc.footnote_report["numbering_scope_count"], 2)
        headings = [(i.text, i.level) for i in doc.items if getattr(i, "is_heading", False)]
        self.assertIn(("第一章 起点", 2), headings)
        self.assertIn(("第一节 小节不重置", 3), headings)

    def test_heading_folio_cleanup_and_scope_use_the_same_title(self):
        from src.me_finder.export_footnotes import normalize_document_export
        pages = [_page(i, str(i+1), [
            _block(f"{i+1} 第{number}章 方法", level=1), _block("正文①。", bbox=MID),
            _block("① 注释", role="page_footnote", bbox=BOT),
        ]) for i, number in enumerate(("一", "二"))]
        doc = normalize_document_export(pages)
        self.assertEqual(doc.footnote_report["numbering_scope_count"], 2)
        self.assertEqual([s["number_range"] for s in doc.footnote_report["scopes"]], [[1, 1], [1, 1]])
        markdown = document_to_markdown(pages, normalized=doc)
        self.assertIn("# 第一章 方法", markdown)
        self.assertIn("# 第二章 方法", markdown)

    def test_repeated_notes_report_four_candidates_even_when_text_is_identical(self):
        from src.me_finder.export_footnotes import normalize_document_export

        for text in ("① 同上。", "① 完全相同的文献说明。"):
            for tagged in (True, False):
                pages = [_page(i, str(i+1), [
                    _block("正文①。", bbox=MID),
                    _block(text, bbox=BOT, role="page_footnote" if tagged else "text"),
                ]) for i in range(4)]
                with self.subTest(text=text, tagged=tagged), self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
                    report = normalize_document_export(pages).footnote_report
                self.assertEqual(report["candidate_note_count"], 4)
                self.assertEqual(report["matched_note_count"], 4)
                self.assertEqual(len({r["note_id"] for r in report["candidates"] if r["kind"] == "note"}), 4)

    def test_report_and_matching_do_not_depend_on_exported_page_markers(self):
        from src.me_finder.export_footnotes import normalize_document_export
        from src.me_finder.markdown_export_normalize import ExportOptions

        reports = [normalize_document_export(footnote_fixture(), options=ExportOptions(page_marker_mode=mode)).footnote_report
                   for mode in ("none", "printed", "full")]
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])
        self.assertEqual(reports[1]["candidates"][0]["source_physical_page"], 10)

    def test_page_resets_become_chapter_numbers_in_reference_order(self):
        from src.me_finder.export_footnotes import Footnote, FootnoteText, normalize_document_footnotes
        from src.me_finder.markdown_export_normalize import ExportOptions

        pages = footnote_fixture()
        before = deepcopy(pages)
        items = normalize_document_footnotes(pages)
        notes = [item for item in items if isinstance(item, Footnote)]
        self.assertEqual([note.display_number for note in notes], [1, 2, 3, 1])
        self.assertEqual([note.source_marker for note in notes], ["②", "①", "①", "[1]"])
        self.assertEqual([note.source_physical_page for note in notes], [10, 10, 11, 12])
        self.assertEqual([note.source_printed_page for note in notes], ["1", "1", "2", "3"])
        self.assertEqual(len({note.note_id for note in notes}), 4)
        self.assertEqual(notes[1].text, notes[2].text)  # repeated content is not deduplicated
        refs = [ref for item in items if isinstance(item, FootnoteText) for ref in item.references]
        self.assertEqual([ref.display_number for ref in refs], [1, 2, 3, 1])
        self.assertEqual(len({ref.reference_id for ref in refs}), len(refs))
        self.assertEqual(pages, before)
        for mode in ("none", "full"):
            with self.subTest(mode=mode):
                other = normalize_document_footnotes(pages, options=ExportOptions(page_marker_mode=mode))
                self.assertEqual([item for item in other if isinstance(item, Footnote)], notes)

    def test_markdown_definitions_move_to_chapter_end_and_preserve_paragraphs(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        pages = footnote_fixture()
        notes = [item for item in normalize_document_footnotes(pages) if isinstance(item, Footnote)]
        markdown = document_to_markdown(pages)
        for note in notes:
            self.assertEqual(markdown.count(f"[^{note.note_id}]:"), 1)
            self.assertIn(f"[^{note.note_id}]", markdown)
        self.assertEqual(markdown.count(f"[^{notes[0].note_id}]"), 2)  # reference + definition
        self.assertLess(markdown.index("下一页重新出现"), markdown.index(f"[^{notes[0].note_id}]:"))
        self.assertLess(markdown.index(f"[^{notes[2].note_id}]:"), markdown.index("## Chapter II"))
        self.assertLess(markdown.index(f"[^{notes[2].note_id}]:"), markdown.index("# 第二部"))
        self.assertLess(markdown.index(f"[^{notes[2].note_id}]:"), markdown.index("<!-- printed_page: 3 -->"))
        self.assertIn("\n    \n    第二段 <原样> & 保留。", markdown)
        self.assertNotIn("pdf_page", markdown)

    def test_repeated_footnotes_are_protected_before_artifact_detection(self):
        from src.me_finder.markdown_export_normalize import build_page_artifact_profile, iter_export_page_blocks

        for tagged in (True, False):
            with self.subTest(tagged=tagged):
                pages = [_page(i, str(i + 1), [
                    _block(f"正文{i}①。", bbox=MID),
                    _block("① 同上。", role="page_footnote" if tagged else None, bbox=BOT),
                ]) for i in range(4)]
                profile = build_page_artifact_profile(pages)
                self.assertFalse(profile.running_footers)
                kept = [b.text for p in pages for b in iter_export_page_blocks(p, profile=profile)]
                self.assertEqual(kept.count("① 同上。"), 4)
                with self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
                    markdown = document_to_markdown(pages)
                self.assertEqual(markdown.count("同上。"), 4)

    def test_missing_note_is_not_paired_by_order(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        page = _page(0, "1", [
            _block("第一章", level=1), _block("正文①②③。"),
            _block("① 注甲", role="page_footnote"), _block("③ 注丙", role="page_footnote"),
        ])
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
            items = normalize_document_footnotes([page])
            markdown = document_to_markdown([page])
        self.assertIn("orphan_ref", " ".join(logs.output))
        self.assertEqual([item.source_marker for item in items if isinstance(item, Footnote)], ["①", "③"])
        self.assertIn("②", markdown)
        self.assertEqual(markdown.count("注甲"), 1)
        self.assertEqual(markdown.count("注丙"), 1)

    def test_repeated_raw_markers_are_ambiguous_without_explicit_link_identity(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        page = _page(0, "1", [_block("正文①。合并进来的另一页正文①。"), _block("① 本页注释", role="page_footnote")])
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
            items = normalize_document_footnotes([page])
        self.assertFalse(any(isinstance(item, Footnote) for item in items))
        self.assertIn("ambiguous_references", " ".join(logs.output))

    def test_mineru_superscript_wrapper_is_replaced_whole_but_math_is_untouched(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        page = _page(0, "1", [
            _block("第一章", level=1), _block("正文 $^{①}$。公式 $x^{②}$ 不变。"),
            _block("$^{①}$ 注释", role="page_footnote"),
        ])
        notes = [i for i in normalize_document_footnotes([page]) if isinstance(i, Footnote)]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].source_marker, "①")
        markdown = document_to_markdown([page])
        self.assertIn(f"正文 [^{notes[0].note_id}]。", markdown)
        self.assertIn("公式 $x^{②}$ 不变。", markdown)

    def test_following_note_only_page_indicates_uncertain_page_flow(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        pages = [
            _page(0, "1", [_block("第一章", level=1), _block("正文①。"), _block("① 注释", role="page_footnote")]),
            _page(1, "2", [_block("导论", role="header"), _block("② 只有注释，没有正文", role="page_footnote")]),
        ]
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
            items = normalize_document_footnotes(pages)
        self.assertFalse(any(isinstance(item, Footnote) for item in items))
        self.assertIn("uncertain_page_flow", " ".join(logs.output))

    def test_ambiguous_and_unsupported_notes_stay_in_place(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        cases = [
            ("ambiguous_definitions", [_block("① 注甲", role="page_footnote"), _block("① 注乙", role="page_footnote")]),
            ("unconfirmed_note_layout", [_block("① 像列表项的内容", bbox=MID)]),
            ("unsupported_note_body", [_block("① 注甲\n② 注乙", role="page_footnote")]),
            ("unsupported_note_body", [_block("①", role="page_footnote")]),
            ("unmarked_note", [_block("没有编号的续注", role="page_footnote")]),
        ]
        for reason, definitions in cases:
            with self.subTest(reason=reason):
                page = _page(0, "1", [_block("第一章", level=1), _block("正文①。"), *definitions])
                with self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
                    items = normalize_document_footnotes([page])
                self.assertFalse(any(isinstance(item, Footnote) for item in items))
                self.assertIn(reason, " ".join(logs.output))
                self.assertEqual([item.text for item in items if hasattr(item, "text")], [b["text"] for b in page["blocks"]])

    def test_no_matching_across_physical_pages_even_with_repeated_printed_page(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        pages = [
            _page(0, "1", [_block("第一章", level=1), _block("前页正文①。")]),
            _page(1, "1", [_block("后页正文。"), _block("① 无对应标记", role="page_footnote")]),
        ]
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
            items = normalize_document_footnotes(pages)
        self.assertFalse(any(isinstance(item, Footnote) for item in items))
        self.assertIn("orphan_ref", " ".join(logs.output))
        self.assertIn("orphan_note", " ".join(logs.output))

    def test_cross_page_continuation_is_preserved_without_relocation(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        pages = [
            _page(0, "1", [_block("第一章", level=1), _block("正文①。"), _block("① 注释前半", role="page_footnote")]),
            _page(1, "2", [_block("下一页正文。"), _block("续注后半。", role="page_footnote")]),
        ]
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
            items = normalize_document_footnotes(pages)
        self.assertFalse(any(isinstance(item, Footnote) for item in items))
        self.assertIn("possible_continuation", " ".join(logs.output))
        self.assertIn("① 注释前半", [item.text for item in items if hasattr(item, "text")])
        self.assertIn("续注后半。", [item.text for item in items if hasattr(item, "text")])

    def test_same_page_chapter_boundary_uses_reference_owner(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        page = _page(0, "1", [
            _block("第一章", level=1), _block("前章正文①。"),
            _block("第二章", level=1), _block("后章正文②。"),
            _block("① 前章注释", role="page_footnote"), _block("② 后章注释", role="page_footnote"),
        ])
        items = normalize_document_footnotes([page])
        notes = [item for item in items if isinstance(item, Footnote)]
        self.assertEqual([note.display_number for note in notes], [1, 1])
        self.assertLess(items.index(notes[0]), next(i for i, item in enumerate(items) if getattr(item, "text", "") == "第二章"))
        page["blocks"][3]["text"] = "后章正文①。"
        page["text_raw"] = "\n".join(b["text"] for b in page["blocks"])
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
            items = normalize_document_footnotes([page])
        self.assertFalse(any(isinstance(item, Footnote) for item in items))
        self.assertIn("ambiguous_chapter", " ".join(logs.output))

    def test_unknown_chapters_do_not_reset_at_arbitrary_headings(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        pages = [_page(i, None, [
            _block(f"普通标题{i}", level=1), _block(f"正文{i}①。"),
            _block("① 注释", role="page_footnote"),
        ]) for i in range(2)]
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
            notes = [item for item in normalize_document_footnotes(pages) if isinstance(item, Footnote)]
        self.assertEqual([note.display_number for note in notes], [1, 2])
        self.assertIn("chapter boundaries unavailable", " ".join(logs.output))

    def test_note_identity_does_not_depend_on_earlier_note_numbers(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        pages = footnote_fixture()
        original = [i for i in normalize_document_footnotes(pages) if isinstance(i, Footnote)]
        pages[0]["blocks"][2]["text"] = "只引用①。"
        pages[0]["text_raw"] = "\n".join(b["text"] for b in pages[0]["blocks"])
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
            changed = [i for i in normalize_document_footnotes(pages) if isinstance(i, Footnote)]
        later = next(note for note in changed if note.source_physical_page == 11)
        self.assertEqual(later.note_id, original[2].note_id)
        self.assertEqual(later.display_number, 2)

    def test_stale_blocks_and_missing_physical_page_are_not_guessed(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        page = _page(0, "1", [_block("正文①。"), _block("① 注释", role="page_footnote")])
        for reason in ("unstructured_page", "missing_physical_page"):
            broken = deepcopy(page)
            if reason == "unstructured_page":
                broken["text_raw"] += "\n未对齐原文"
            else:
                del broken["pdf_page_index"]
                del broken["pdf_page_number_1based"]
            with self.subTest(reason=reason), self.assertLogs("src.me_finder.export_footnotes", level="WARNING") as logs:
                items = normalize_document_footnotes([broken])
            self.assertFalse(any(isinstance(item, Footnote) for item in items))
            self.assertIn(reason, " ".join(logs.output))

    def test_plain_numbers_exponents_and_list_markers_are_not_references(self):
        page = _page(0, "1", [
            _block("第一章", level=1), _block("1848年，面积12m²，1+2=3。"),
            _block("① 列表第一项", bbox=MID), _block("① 页底注释", role="page_footnote"),
        ])
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
            markdown = document_to_markdown([page])
        self.assertNotIn("[^fn-", markdown)
        self.assertIn("1848年，面积12m²，1+2=3。", markdown)
        self.assertIn("① 列表第一项", markdown)


if __name__ == "__main__":
    unittest.main()
