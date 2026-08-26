from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.me_finder.extractors import extract_epub
from src.me_finder.indexer import build_index
from src.me_finder.page_display import build_page_display, resolve_citation_page
from src.me_finder.pdf_import_service import scan_directories_for_documents
from src.me_finder.search import SearchEngine
from src.me_finder.structured_reader import (
    get_document_citation,
    get_document_window,
)


CONTAINER_XML = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def write_epub(
    path: Path,
    *,
    with_pages: bool = True,
    epub2: bool = False,
    with_page_list: bool = True,
) -> None:
    if epub2:
        navigation_item = (
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        )
        spine_attribute = ' toc="ncx"'
    else:
        navigation_item = (
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
        spine_attribute = ""
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="{'2.0' if epub2 else '3.0'}"
         unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">978-7-0000-0000-1</dc:identifier>
    <dc:title>带页码的电子书</dc:title>
    <dc:creator>测试作者</dc:creator>
    <dc:publisher>测试出版社</dc:publisher>
    <dc:date>2024-05-01</dc:date>
    <dc:language>zh-CN</dc:language>
  </metadata>
  <manifest>
    {navigation_item}
    <item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine{spine_attribute}><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>
"""
    nav = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <body><nav epub:type="page-list"><ol>
    <li><a href="chapter1.xhtml#page1">1</a></li>
    <li><a href="chapter1.xhtml#page2">2</a></li>
    <li><a href="chapter2.xhtml#page3">3</a></li>
  </ol></nav></body>
</html>
"""
    ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <pageList>
    <pageTarget value="1"><navLabel><text>1</text></navLabel><content src="chapter1.xhtml#page1"/></pageTarget>
    <pageTarget value="2"><navLabel><text>2</text></navLabel><content src="chapter1.xhtml#page2"/></pageTarget>
    <pageTarget value="3"><navLabel><text>3</text></navLabel><content src="chapter2.xhtml#page3"/></pageTarget>
  </pageList>
</ncx>
"""
    marker = (
        '<span id="page2" epub:type="pagebreak" title="2"></span>'
        if with_pages
        else ""
    )
    page1_id = ' id="page1"' if with_pages else ""
    page3_id = ' id="page3"' if with_pages else ""
    chapter1 = f"""<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><h1{page1_id}>第一章</h1><p>第一页正文。</p><p>分页之前。{marker}第二页正文。</p></body>
</html>"""
    chapter2 = f"""<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1{page3_id}>第二章</h1><p>第三页正文。</p></body></html>"""

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER_XML)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr(
            "OEBPS/toc.ncx" if epub2 else "OEBPS/nav.xhtml",
            (ncx if epub2 else nav) if with_pages and with_page_list else (
                '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap/></ncx>'
                if epub2
                else '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol/></nav></body></html>'
            ),
        )
        archive.writestr("OEBPS/chapter1.xhtml", chapter1)
        archive.writestr("OEBPS/chapter2.xhtml", chapter2)


class EpubExtractorTests(unittest.TestCase):
    def test_epub3_imports_metadata_spine_text_and_publisher_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            epub = root / "book.epub"
            write_epub(epub)

            extracted = extract_epub(epub, root)

        source = extracted["source_file"]
        self.assertEqual(source["file_format"], "epub")
        self.assertEqual(source["title"], "带页码的电子书")
        self.assertEqual(source["author"], "测试作者")
        self.assertEqual(source["publish_year"], "2024")
        self.assertEqual(source["epub_page_count"], 3)
        paragraphs = extracted["paragraphs"]
        self.assertEqual(
            [item["text_raw"] for item in paragraphs],
            ["第一章", "第一页正文。", "分页之前。", "第二页正文。", "第二章", "第三页正文。"],
        )
        self.assertEqual(
            [item["original_page_start"] for item in paragraphs],
            ["1", "1", "1", "2", "3", "3"],
        )
        self.assertTrue(
            all(item["page_source_type"] == "epub_page_list" for item in paragraphs)
        )
        self.assertEqual(
            [item["original_page_label"] for item in extracted["page_anchors"]],
            ["1", "2", "3"],
        )
        self.assertEqual(extracted["page_anchors"][0]["validated_by"], "epub_publisher")
        self.assertEqual(extracted["audit_issues"], [])

    def test_epub2_ncx_page_list_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            epub = root / "legacy.epub"
            write_epub(epub, epub2=True)
            extracted = extract_epub(epub, root)

        self.assertEqual(extracted["source_file"]["epub_page_count"], 3)
        self.assertEqual(extracted["paragraphs"][3]["original_page_start"], "2")
        self.assertEqual(extracted["paragraphs"][4]["original_page_start"], "3")

    def test_epub_without_print_pages_does_not_invent_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            epub = root / "flow.epub"
            write_epub(epub, with_pages=False)
            extracted = extract_epub(epub, root)

        self.assertTrue(extracted["paragraphs"])
        self.assertTrue(
            all(item["original_page_start"] is None for item in extracted["paragraphs"])
        )
        self.assertEqual(extracted["page_anchors"], [])
        self.assertEqual(extracted["audit_issues"][0]["issue_type"], "epub_page_list_missing")

    def test_pagebreak_only_epub_counts_pages_found_in_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            epub = root / "pagebreak-only.epub"
            write_epub(epub, with_page_list=False)
            extracted = extract_epub(epub, root)

        self.assertEqual(extracted["source_file"]["epub_page_count"], 1)
        page_two = extracted["paragraphs"][3]
        self.assertEqual(page_two["original_page_start"], "2")
        self.assertEqual(page_two["page_source_type"], "epub_pagebreak")
        self.assertEqual(extracted["audit_issues"], [])

    def test_epub_pages_are_citation_safe_word_pages(self) -> None:
        fields = {
            "source_type": "word",
            "page_source_type": "epub_page_list",
            "original_page_start": "27",
            "original_page_end": "27",
        }
        display = build_page_display(fields)
        resolution = resolve_citation_page(fields)

        self.assertEqual(display.display, "第 27 页")
        self.assertEqual(display.note, "EPUB 出版方页码表")
        self.assertTrue(resolution.verified)
        self.assertEqual(resolution.start, "27")


class EpubIndexIntegrationTests(unittest.TestCase):
    def test_epub_is_discovered_by_full_index_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            corpus = root / "corpus" / "raw_docx"
            corpus.mkdir(parents=True)
            write_epub(corpus / "indexed.epub")
            database = root / "data" / "index.sqlite3"

            result = build_index(
                corpus_dir=corpus,
                index_path=root / "data" / "index.json",
                database_path=database,
                root=root,
            )
            connection = sqlite3.connect(database)
            try:
                row = connection.execute(
                    "SELECT source_type, file_name FROM source_files"
                ).fetchone()
                paragraph_count = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs WHERE page_source_type = 'epub_page_list'"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(result["source_files"][0]["file_format"], "epub")
        self.assertEqual(row, ("word", "indexed.epub"))
        self.assertEqual(paragraph_count, 6)

    def test_epub_and_word_search_filters_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            corpus = root / "corpus" / "raw_docx"
            corpus.mkdir(parents=True)
            write_epub(corpus / "filtered.epub")
            database = root / "data" / "index.sqlite3"
            build_index(
                corpus_dir=corpus,
                index_path=root / "data" / "index.json",
                database_path=database,
                root=root,
            )
            engine = SearchEngine(database)
            try:
                epub_result = engine.search("第三页正文", source_type="epub")
                word_result = engine.search("第三页正文", source_type="word")
            finally:
                engine.close()

        self.assertEqual(epub_result["total"], 1)
        self.assertEqual(word_result["total"], 0)

    def test_epub_publisher_pages_reach_structured_reader_and_citations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            corpus = root / "corpus" / "raw_docx"
            corpus.mkdir(parents=True)
            write_epub(corpus / "reader.epub")
            database = root / "data" / "index.sqlite3"
            result = build_index(
                corpus_dir=corpus,
                index_path=root / "data" / "index.json",
                database_path=database,
                root=root,
            )
            source_id = result["source_files"][0]["source_file_id"]
            window = get_document_window(database, source_id, start=0, count=6)
            page_two = window["items"][3]
            citation = get_document_citation(
                database,
                source_id,
                start_anchor_id=page_two["paragraph_id"],
                end_anchor_id=page_two["paragraph_id"],
            )

        self.assertEqual(window["source"]["file_format"], "epub")
        self.assertEqual(
            window["items"][0]["anchor_id"],
            window["items"][0]["paragraph_id"],
        )
        self.assertIsNone(window["items"][1]["anchor_id"])
        self.assertEqual(page_two["page_display"], "第 2 页")
        self.assertTrue(page_two["page_verified"])
        self.assertEqual(page_two["citation_page_start"], "2")
        self.assertEqual(citation["page_range"]["citation_page_start"], "2")

    def test_directory_scan_lists_epub_for_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_epub(root / "scan.epub")
            result = scan_directories_for_documents([str(root)], {})

        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["file_type"], "epub")


if __name__ == "__main__":
    unittest.main()
