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
from tests.test_markdown_export import markdown_fixture


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

    def test_default_full_mode_keeps_physical_page_without_fabricating_printed(self) -> None:
        page = _page(30, None, [{"text": "只有物理页"}])
        with _archive(build_epub_bytes([page], title="默认")) as archive:
            content = ElementTree.fromstring(archive.read("OEBPS/content.xhtml"))
            pagebreak = content.find(".//x:span", XHTML)
            self.assertEqual(pagebreak.attrib["aria-label"], "31")
            self.assertEqual(pagebreak.attrib["data-pdf-page"], "31")
            self.assertNotIn("data-printed-page", pagebreak.attrib)

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
