"""Indexed headings must retain real text anchors and reject page decorations."""

from pathlib import Path
import tempfile
import unittest

from src.me_finder.database import build_database
from src.me_finder.document_outline import get_document_outline
from src.me_finder.structured_reader import InvalidSourceId, SourceNotFound


class DocumentOutlineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "index.sqlite3"

    def build(self, pages=(), paragraphs=()):
        build_database({
            "metadata": {}, "volumes": [], "works": [],
            "source_files": [
                {"source_file_id": "pdf-book", "source_type": "pdf", "file_name": "missing.pdf"},
                {"source_file_id": "epub-book", "source_type": "word", "source_format": "epub", "file_name": "missing.epub"},
            ], "pdf_pages": list(pages), "paragraphs": list(paragraphs),
        }, self.db)

    def page(self, index, blocks):
        raw = "\n".join(block["text"] for block in blocks)
        cursor = 0
        for block in blocks:
            block.update(page_char_start=cursor, page_char_end=cursor + len(block["text"]))
            cursor += len(block["text"]) + 1
        return {"source_file_id": "pdf-book", "pdf_page_index": index,
                "pdf_page_id": f"pdf-book-PAGE-{index:06d}", "text_raw": raw, "blocks": blocks}

    def test_pdf_levels_and_codepoint_anchors_without_source_file(self):
        page = self.page(7, [
            {"text": "😀正文"},
            {"text": "第一章 导论", "document_heading_level": 1, "text_level": 4},
            {"text": "第一节 范围", "text_level": 2},
            {"text": "细目", "text_level": 3},
            {"text": "页脚", "text_level": 1, "mineru_type": "footer"},
            {"text": "注释", "text_level": 1, "mineru_type": "footnote"},
        ])
        self.build([page])
        result = get_document_outline(self.db, "pdf-book")
        self.assertEqual(result["offset_unit"], "unicode_codepoint")
        self.assertEqual([e["level"] for e in result["entries"]], [1, 2])
        for entry in result["entries"]:
            self.assertEqual(entry["item_index"], 7)
            self.assertEqual(entry["anchor_id"], page["pdf_page_id"])
            self.assertEqual(page["text_raw"][entry["char_start"]:entry["char_end"]], entry["title"])

    def test_repeated_header_excluded_but_verified_outline_occurrence_kept(self):
        pages = [self.page(i, [
            {"text": "第一章", "text_level": 1, **({"document_heading_source": "pdf_outline"} if i == 0 else {})},
            {"text": f"各页正文 {i}"}, {"text": str(i)},
        ]) for i in range(5)]
        self.build(pages)
        entries = get_document_outline(self.db, "pdf-book")["entries"]
        self.assertEqual([e["item_index"] for e in entries], [0])

    def test_unaligned_blocks_are_not_navigable(self):
        page = self.page(0, [{"text": "第一章", "text_level": 1}])
        page["text_raw"] = "另一个解析版本的正文"
        self.build([page])
        self.assertEqual(get_document_outline(self.db, "pdf-book")["entries"], [])

    def test_legacy_blocks_without_offsets_keep_sequential_exact_positions(self):
        page = self.page(0, [{"text": "标题", "text_level": 1}, {"text": "正文"}, {"text": "标题", "text_level": 2}])
        for block in page["blocks"]:
            del block["page_char_start"], block["page_char_end"]
        self.build([page])
        self.assertEqual([e["char_start"] for e in get_document_outline(self.db, "pdf-book")["entries"]], [0, 6])

    def test_epub_and_word_styles_without_invented_pages(self):
        paragraphs = [{"source_file_id": "epub-book", "source_type": "word",
                       "paragraph_id": f"epub-book-P{i:06d}", "paragraph_index": i * 3,
                       "text_raw": "  😀章节  ", "style_name": style}
                      for i, style in enumerate(("h1", "h2", "h3", "Heading 1", "标题 2", "normal"))]
        self.build(paragraphs=paragraphs)
        entries = get_document_outline(self.db, "epub-book")["entries"]
        self.assertEqual([e["item_index"] for e in entries], [0, 3, 9, 12])
        for entry in entries:
            self.assertEqual(entry["char_start"], 2)
            self.assertEqual(entry["char_end"], 5)
            self.assertNotIn("page_number", entry)

    def test_empty_outline_and_invalid_sources(self):
        self.build()
        self.assertEqual(get_document_outline(self.db, "pdf-book")["entries"], [])
        with self.assertRaises(InvalidSourceId):
            get_document_outline(self.db, "../book")
        with self.assertRaises(SourceNotFound):
            get_document_outline(self.db, "missing")
