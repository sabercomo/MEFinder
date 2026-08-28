from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from src.me_finder.epub_export import (
    build_epub_bytes,
    safe_epub_filename,
    write_epub,
)
from src.me_finder.document_export_service import (
    IndexedDocumentNotFound,
    UnsupportedDocumentExport,
    export_indexed_pdf_epub,
)
from src.me_finder.markdown_export_normalize import ExportOptions
from tests.test_markdown_export import footnote_fixture, markdown_fixture


XHTML = {"x": "http://www.w3.org/1999/xhtml"}
OPF = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
EPUB = "{http://www.idpf.org/2007/ops}type"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _archive(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data), "r")


def _page(index: int, printed: str | None, blocks: list[dict]) -> dict:
    page = {
        "pdf_page_index": index,
        "pdf_page_number_1based": index + 1,
        "page_width": 1000,
        "page_height": 1000,
        "text_raw": "\n".join(str(block["text"]) for block in blocks),
        "blocks": blocks,
    }
    if printed is not None:
        page["printed_page"] = printed
        page["citation_page"] = printed
    return page


class EPUBExportCoreTests(unittest.TestCase):
    def test_repeated_parser_headings_do_not_pollute_nav(self):
        from tests.test_markdown_export import _block, TOP, MID, BOT
        pages = [_page(i, str(i + 1), [
            _block("第一章 方法", bbox=TOP, level=1),
            _block(f"正文{i}①。", bbox=MID),
            _block("① 同上。", role="page_footnote", bbox=BOT),
        ]) for i in range(100)]
        for page in pages:
            header = page["blocks"][0]
            header["text_level"] = header.pop("document_heading_level")
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
            data = build_epub_bytes(pages)
        with _archive(data) as archive:
            nav = archive.read("OEBPS/nav.xhtml").decode("utf-8")
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
            self.assertNotIn("第一章 方法", nav)
            self.assertEqual([n.text for n in content.iter() if n.attrib.get(EPUB) == "noteref"],
                             [str(i) for i in range(1, 101)])

    def test_explicit_multi_reference_model_renders_all_backlinks_without_text_deduplication(self):
        from src.me_finder.export_footnotes import Footnote, FootnoteReference, FootnoteText, NormalizedDocument
        from src.me_finder.markdown_export import document_to_markdown

        # Already-known semantic relationships; this is NOT evidence to relax
        # the raw OCR duplicate-marker rejection tested by normalization.
        doc = NormalizedDocument([
            FootnoteText("甲①，乙①。", references=(
                FootnoteReference(1, 2, "note-a", "ref-a1", 1),
                FootnoteReference(4, 5, "note-a", "ref-a2", 1),
            )),
            FootnoteText("丙②。", references=(FootnoteReference(1, 2, "note-b", "ref-b", 2),)),
            Footnote("note-a", 1, 1, "同上。", "①", 1, "1", 1, ("ref-a1", "ref-a2")),
            Footnote("note-b", 1, 2, "同上。", "②", 1, "1", 2, ("ref-b",)),
        ], {})
        with _archive(build_epub_bytes([], normalized=doc)) as archive:
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
        self.assertEqual(len(content.findall(".//x:aside", XHTML)), 2)
        references = [n for n in content.iter() if n.attrib.get(EPUB) == "noteref"]
        self.assertEqual([n.attrib["href"] for n in references], ["#note-a", "#note-a", "#note-b"])
        backlinks = [n.attrib["href"] for n in content.iter() if n.attrib.get("role") == "doc-backlink"]
        self.assertEqual(backlinks, ["#ref-a1", "#ref-a2", "#ref-b"])
        markdown = document_to_markdown([], normalized=doc)
        self.assertEqual(markdown.count("[^note-a]"), 3)
        self.assertEqual(markdown.count("[^note-a]:"), 1)
        self.assertEqual(markdown.count("同上。"), 2)

    def test_footnotes_have_chapter_numbers_and_all_links_resolve(self):
        from src.me_finder.export_footnotes import Footnote, normalize_document_footnotes

        pages = footnote_fixture()
        notes = [item for item in normalize_document_footnotes(pages) if isinstance(item, Footnote)]
        with _archive(build_epub_bytes(pages, title="脚注测试")) as archive:
            raw = archive.read("OEBPS/content.xhtml").decode("utf-8")
            content = ElementTree.fromstring(raw)
            references = [n for n in content.findall(".//x:a", XHTML) if n.attrib.get(EPUB) == "noteref"]
            self.assertEqual([n.text for n in references], ["1", "2", "3", "1"])
            asides = content.findall(".//x:aside", XHTML)
            self.assertEqual([n.attrib["id"] for n in asides], [note.note_id for note in notes])
            ids = [n.attrib["id"] for n in content.iter() if "id" in n.attrib]
            self.assertEqual(len(ids), len(set(ids)))
            for link in content.findall(".//x:a", XHTML):
                self.assertIn(link.attrib["href"][1:], ids)
            backlinks = asides[0].findall(".//x:a", XHTML)
            self.assertEqual([n.attrib["href"] for n in backlinks], ["#" + ref for ref in notes[0].reference_ids])
            self.assertEqual(len(asides[-1].findall("./x:p", XHTML)), 3)  # two paragraphs + backlinks
            self.assertIn("第二段 &lt;原样&gt; &amp; 保留。", raw)
            self.assertLess(raw.index("下一页重新出现"), raw.index("<aside"))
            self.assertLess(raw.index(f'id="{notes[2].note_id}"'), raw.index("Chapter II"))
            self.assertLess(raw.index(f'id="{notes[2].note_id}"'), raw.index("第二部"))
            # These are inline body children, so non-popup readers see the same
            # chapter notes before the next part and its ordinary subtitle.
            body_children = list(content.find("x:body", XHTML))
            part = next(n for n in body_children if n.text == "第二部")
            self.assertLess(body_children.index(asides[2]), body_children.index(part))
            self.assertLess(raw.index("第二部的副标题"), raw.index("Chapter II"))
            self.assertNotIn("data-pdf-page", raw)
            nav = ElementTree.fromstring(archive.read("OEBPS/nav.xhtml"))
            self.assertFalse(any("fn-" in n.attrib["href"] for n in nav.findall(".//x:a", XHTML)))

    def test_enriched_heading_precedes_first_body_block_in_content_and_nav(self):
        from tests.test_markdown_export import _block, MID
        from src.me_finder.document_heading import apply_heading_assignments

        pages = [_page(12, "4", [
            _block("为了撰写眼前的这本书……", bbox=MID),
            _block("前言", role="header", bbox=[124, 191, 276, 227]),
        ])]
        apply_heading_assignments(pages, [{"pdf_page_index": 12, "block_index": 1,
            "level": 1, "printed_page": "4", "title": "前言"}], "document_toc")
        with _archive(build_epub_bytes(pages)) as archive:
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
            nav = ElementTree.fromstring(archive.read("OEBPS/nav.xhtml"))
        blocks = [n for n in content.find("x:body", XHTML) if n.tag.endswith(("}h1", "}p"))]
        self.assertEqual([n.text for n in blocks], ["前言", "为了撰写眼前的这本书……"])
        toc_link = next(n for n in nav.findall(".//x:a", XHTML) if n.text == "前言")
        self.assertEqual(toc_link.attrib["href"].split("#")[1], blocks[0].attrib["id"])

    def test_unresolved_notes_are_kept_as_text_in_epub(self):
        pages = [_page(0, "1", [
            {"text": "第一章", "text_level": 1}, {"text": "正文①②。"},
            {"text": "① 注释甲", "mineru_type": "page_footnote"},
            {"text": "③ 无正文引用的注释", "mineru_type": "page_footnote"},
        ])]
        with self.assertLogs("src.me_finder.export_footnotes", level="WARNING"):
            data = build_epub_bytes(pages)
        with _archive(data) as archive:
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
            text = "".join(content.itertext())
            self.assertIn("②", text)
            self.assertIn("③ 无正文引用的注释", text)
            self.assertEqual(len(content.findall(".//x:aside", XHTML)), 1)

    def test_epub3_container_and_metadata_are_valid(self) -> None:
        data = build_epub_bytes(
            [_page(0, "1", [{"text": "正文"}])],
            title="批判理论 & 社会",
            author="作者 <甲>",
            language="zh-Hans",
            identifier="urn:uuid:00000000-0000-0000-0000-000000000001",
            modified="2026-08-26T12:00:00Z",
        )
        with _archive(data) as archive:
            infos = archive.infolist()
            self.assertEqual(infos[0].filename, "mimetype")
            self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
            self.assertEqual(
                {info.filename for info in infos},
                {
                    "mimetype",
                    "META-INF/container.xml",
                    "OEBPS/content.opf",
                    "OEBPS/nav.xhtml",
                    "OEBPS/content.xhtml",
                    "OEBPS/style.css",
                },
            )
            stylesheet = archive.read("OEBPS/style.css").decode("utf-8")
            self.assertIn(
                '@namespace epub "http://www.idpf.org/2007/ops";',
                stylesheet,
            )
            self.assertIn('span[epub|type~="pagebreak"]', stylesheet)
            container = ElementTree.fromstring(
                archive.read("META-INF/container.xml")
            )
            rootfile = container.find(
                ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
            )
            self.assertEqual(rootfile.attrib["full-path"], "OEBPS/content.opf")

            package = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
            self.assertEqual(package.attrib["version"], "3.0")
            self.assertEqual(package.attrib[XML_LANG], "zh-Hans")
            self.assertEqual(package.findtext(".//dc:title", namespaces=OPF), "批判理论 & 社会")
            self.assertEqual(package.findtext(".//dc:creator", namespaces=OPF), "作者 <甲>")
            self.assertEqual(package.findtext(".//dc:language", namespaces=OPF), "zh-Hans")
            self.assertEqual(package.findtext(".//dc:source", namespaces=OPF), "MEFinder")

            for name in ("OEBPS/nav.xhtml", "OEBPS/content.xhtml"):
                document = ElementTree.fromstring(archive.read(name))
                self.assertEqual(document.attrib["lang"], "zh-Hans")
                self.assertEqual(document.attrib[XML_LANG], "zh-Hans")

    def test_heading_hierarchy_navigation_and_body_escape(self) -> None:
        pages = [
            _page(
                0,
                "ix",
                [
                    {"text": "第一部 <批判>", "text_level": 1},
                    {"text": "正文 A & B"},
                    {"text": "第三层", "text_level": 3},
                    {"text": "正文二"},
                ],
            )
        ]
        with _archive(build_epub_bytes(pages, title="书名")) as archive:
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
            self.assertEqual(
                [node.text for node in content.findall(".//x:h1", XHTML)],
                ["第一部 <批判>"],
            )
            self.assertEqual(
                [node.text for node in content.findall(".//x:h3", XHTML)],
                ["第三层"],
            )
            self.assertEqual(
                [node.text for node in content.findall(".//x:p", XHTML)],
                ["正文 A & B", "正文二"],
            )

            nav = ElementTree.fromstring(archive.read("OEBPS/nav.xhtml"))
            toc = next(node for node in nav.findall(".//x:nav", XHTML) if node.attrib[EPUB] == "toc")
            links = toc.findall(".//x:a", XHTML)
            self.assertEqual([link.text for link in links], ["第一部 <批判>", "第三层"])
            self.assertEqual([link.attrib["href"] for link in links], ["content.xhtml#h-0001", "content.xhtml#h-0002"])
            first_item = toc.find("./x:ol/x:li", XHTML)
            self.assertIsNotNone(first_item.find("./x:ol/x:li", XHTML))

    def test_default_cleanup_and_printed_pagebreak_share_markdown_policy(self) -> None:
        header = "重复页眉"
        footer = "重复页脚"
        pages = [
            _page(
                index,
                str(10 + index),
                [
                    {"text": header, "bbox": [100, 10, 400, 40]},
                    {"text": str(10 + index), "bbox": [470, 10, 520, 40]},
                    {"text": f"正文 {index}", "bbox": [100, 480, 900, 520]},
                    {"text": footer, "bbox": [100, 950, 400, 980]},
                ],
            )
            for index in range(4)
        ]
        with _archive(build_epub_bytes(pages, title="清理测试")) as archive:
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
            self.assertEqual(
                [node.text for node in content.findall(".//x:p", XHTML)],
                ["正文 0", "正文 1", "正文 2", "正文 3"],
            )
            pagebreaks = [
                node
                for node in content.findall(".//x:span", XHTML)
                if node.attrib.get(EPUB) == "pagebreak"
            ]
            self.assertEqual(
                [node.attrib["aria-label"] for node in pagebreaks],
                ["10", "11", "12", "13"],
            )
            self.assertTrue(all("data-pdf-page" not in node.attrib for node in pagebreaks))

            nav = ElementTree.fromstring(archive.read("OEBPS/nav.xhtml"))
            page_list = next(
                node
                for node in nav.findall(".//x:nav", XHTML)
                if node.attrib[EPUB] == "page-list"
            )
            self.assertEqual(
                [link.text for link in page_list.findall(".//x:a", XHTML)],
                ["10", "11", "12", "13"],
            )

    def test_default_omits_unknown_printed_pages_without_changing_source(self) -> None:
        page = _page(30, None, [{"text": "只有物理页"}])
        with _archive(build_epub_bytes([page], title="默认")) as archive:
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
            self.assertIsNone(content.find(".//x:span", XHTML))
            self.assertEqual(content.find(".//x:p", XHTML).text, "只有物理页")
            nav = ElementTree.fromstring(archive.read("OEBPS/nav.xhtml"))
            self.assertFalse(any(node.attrib.get(EPUB) == "page-list" for node in nav.findall(".//x:nav", XHTML)))
        self.assertEqual(page["pdf_page_number_1based"], 31)

    def test_full_mode_retains_printed_and_physical_page_metadata(self) -> None:
        page = _page(30, "xiv", [{"text": "正文"}])
        with _archive(
            build_epub_bytes(
                [page],
                title="完整页码",
                options=ExportOptions(page_marker_mode="full"),
            )
        ) as archive:
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
            pagebreak = content.find(".//x:span", XHTML)
            self.assertEqual(pagebreak.attrib["aria-label"], "xiv")
            self.assertEqual(pagebreak.attrib["data-printed-page"], "xiv")
            self.assertEqual(pagebreak.attrib["data-pdf-page"], "31")

    def test_filename_and_atomic_write(self) -> None:
        self.assertEqual(safe_epub_filename(""), "MEFinder-document.epub")
        name = safe_epub_filename('书名：导论?/*<>|"')
        self.assertTrue(name.endswith(".epub"))
        self.assertNotRegex(name, r'[\\/:*?"<>|]')

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "书.epub"
            result = write_epub(
                target,
                [_page(0, "1", [{"text": "正文"}])],
                title="书",
            )
            self.assertEqual(result, target)
            self.assertTrue(target.is_file())
            self.assertFalse(Path(str(target) + ".partial").exists())
            with zipfile.ZipFile(target, "r") as archive:
                self.assertEqual(archive.testzip(), None)


class EPUBExportServiceTests(unittest.TestCase):
    def test_service_exports_epub_with_indexed_metadata_and_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, source_id = markdown_fixture(root)
            result = export_indexed_pdf_epub(
                database_path=database,
                source_file_id=source_id,
                output_dir=root / "exports",
            )

            path = Path(result["path"])
            self.assertEqual(result["epub_version"], "3.0")
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(path.name, "批判理论.epub")
            self.assertTrue(path.is_file())
            self.assertFalse(Path(str(path) + ".partial").exists())
            with zipfile.ZipFile(path, "r") as archive:
                package = ElementTree.fromstring(archive.read("OEBPS/content.opf"))
                self.assertEqual(package.findtext(".//dc:title", namespaces=OPF), "批判理论")
                self.assertEqual(package.findtext(".//dc:creator", namespaces=OPF), "马克斯·霍克海默")
                content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
                self.assertEqual(
                    [node.text for node in content.findall(".//x:h1", XHTML)],
                    ["第一章 法兰克福学派", "第二章 方法问题"],
                )

    def test_service_missing_and_unsupported_sources_have_clear_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, _source_id = markdown_fixture(root, source_type="word")
            with self.assertRaises(IndexedDocumentNotFound):
                export_indexed_pdf_epub(
                    database_path=database,
                    source_file_id="missing",
                    output_dir=root / "exports",
                )
            with self.assertRaises(UnsupportedDocumentExport):
                export_indexed_pdf_epub(
                    database_path=database,
                    source_file_id="source-markdown-1",
                    output_dir=root / "exports",
                )


if __name__ == "__main__":
    unittest.main()
