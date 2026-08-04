from __future__ import annotations

import sqlite3
import unittest
import zipfile
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

from src.me_finder.database import build_database
from src.me_finder.extractors import extract_source
from src.me_finder.indexer import build_index
from src.me_finder.mineru_api import MinerUError
from src.me_finder.pdf_extractors import relative_to_root
from src.me_finder.pdf_import_service import (
    copy_local_document,
    indexed_word_source_count,
    rebuild_local_index,
    register_pdf,
)
from src.me_finder.search import SearchEngine

BODY = (
    "本文以马克思主义基本原理为指导，"
    "系统考察了当代社会发展的基本规律"
    "与内在机理。"
)


def write_native_pdf(path: Path) -> None:
    """A dense text-layer PDF, like a journal article downloaded from CNKI."""

    import fitz

    document = fitz.open()
    font = fitz.Font("china-s")
    for number in range(1, 4):
        page = document.new_page()
        writer = fitz.TextWriter(page.rect)
        offset = 90
        for _ in range(9):
            writer.append((60, offset), BODY, font=font, fontsize=11)
            offset += 22
        writer.append((60, offset), f"CNKI page {number} selectable text", fontsize=11)
        writer.write_text(page)
    document.save(str(path))
    document.close()


def write_standalone_docx(path: Path, body: str = "这段唯一文本用于验证普通 DOCX 可以直接进入本地索引。") -> None:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        f"<w:p><w:r><w:t>{escape('没有卷号的独立论文')}</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>{escape(body)}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)


def make_public_build_root(base: Path) -> Path:
    """A release layout: index and config, but no Word corpus."""

    root = base / "MEFinder"
    (root / "data").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    build_database({"metadata": {}}, root / "data" / "index.sqlite3")
    (root / "config" / "pdf_imports.json").write_text(
        '{"documents": []}', encoding="utf-8"
    )
    return root


class PortableIndexRebuildTests(unittest.TestCase):
    def test_pdf_import_works_without_word_corpus(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_public_build_root(base)
            source = base / "CNKI_sample.pdf"
            write_native_pdf(source)

            stored = copy_local_document(root, source)
            register_pdf(root, stored)
            self.assertFalse((root / "corpus" / "raw_docx").exists())

            rebuild_local_index(root)

            connection = sqlite3.connect(str(root / "data" / "index.sqlite3"))
            try:
                sources = connection.execute(
                    "SELECT COUNT(*) FROM source_files WHERE source_type = 'pdf'"
                ).fetchone()[0]
                paragraphs = connection.execute(
                    "SELECT COUNT(*) FROM paragraphs"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(sources, 1)
            self.assertGreater(paragraphs, 0)

    def test_rebuild_refuses_when_indexed_word_documents_would_be_lost(self) -> None:
        with TemporaryDirectory() as tmp:
            root = make_public_build_root(Path(tmp))
            connection = sqlite3.connect(str(root / "data" / "index.sqlite3"))
            try:
                connection.execute(
                    "INSERT INTO source_files(source_file_id, source_type, file_name,"
                    " relative_path, volume_number, payload_json)"
                    " VALUES('w1', 'word', 'vol1.docx', 'corpus/raw_docx/vol1.docx', 1, '{}')"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(MinerUError) as caught:
                rebuild_local_index(root)
            self.assertIn("Word", str(caught.exception))
            self.assertFalse((root / "corpus" / "raw_docx").exists())

    def test_indexed_word_source_count_tolerates_a_missing_database(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(indexed_word_source_count(Path(tmp) / "absent.sqlite3"), 0)

    def test_standalone_docx_import_does_not_require_a_volume_in_the_filename(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_public_build_root(base)
            source = base / "用户下载的普通论文.docx"
            write_standalone_docx(source)

            stored = copy_local_document(root, source)
            first_index = rebuild_local_index(root)
            second_index = rebuild_local_index(root)

            word_sources = [
                item
                for item in first_index["source_files"]
                if item.get("source_type") == "word"
            ]
            self.assertEqual(len(word_sources), 1)
            self.assertEqual(word_sources[0]["file_name"], source.name)
            self.assertIsNone(word_sources[0]["volume_number"])
            self.assertEqual(word_sources[0]["document_title"], source.stem)
            self.assertEqual(
                word_sources[0]["source_file_id"],
                second_index["source_files"][0]["source_file_id"],
            )
            self.assertEqual(stored.parent, root / "corpus" / "raw_docx")

            engine = SearchEngine(root / "data" / "index.sqlite3")
            try:
                result = engine.search("普通 DOCX 可以直接进入本地索引", source_type="word")
            finally:
                engine.close()
            self.assertEqual(result["total"], 1)
            hit = result["results"][0]
            self.assertEqual(hit["volume_display"], source.stem)
            self.assertNotIn("马克思恩格斯文集", hit["copy_text"])
            self.assertIn("页码尚未校准", hit["citation_formats"]["chinese"])

    def test_non_marx_volume_name_stays_a_standalone_document(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_public_build_root(base)
            source = base / "杜威全集 第08卷.docx"
            write_standalone_docx(source)

            stored = copy_local_document(root, source)
            index = rebuild_local_index(root)

            word_source = index["source_files"][0]
            self.assertEqual(word_source["file_name"], source.name)
            self.assertIsNone(word_source["volume_number"])
            self.assertTrue(word_source["source_file_id"].startswith("docx-"))
            self.assertFalse(index["volumes"][0]["volume_id"].startswith("MEWJ-"))
            self.assertEqual(word_source["author"], "杜威")
            self.assertEqual(index["volumes"][0]["primary_structure"], "complete_works")
            self.assertEqual(stored.parent, root / "corpus" / "raw_docx")

    def test_multiple_standalone_docx_files_have_distinct_stable_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_public_build_root(base)
            first = base / "第一份论文.docx"
            second = base / "第二份论文.docx"
            write_standalone_docx(first, "两份文件可以包含相同正文。")
            write_standalone_docx(second, "两份文件可以包含相同正文。")
            copy_local_document(root, first)
            copy_local_document(root, second)

            first_index = rebuild_local_index(root)
            second_index = rebuild_local_index(root)

            source_ids = [item["source_file_id"] for item in first_index["source_files"]]
            volume_ids = [item["volume_id"] for item in first_index["volumes"]]
            work_ids = [item["work_id"] for item in first_index["works"]]
            paragraph_ids = [item["paragraph_id"] for item in first_index["paragraphs"]]
            self.assertEqual(len(source_ids), len(set(source_ids)))
            self.assertEqual(len(volume_ids), len(set(volume_ids)))
            self.assertEqual(len(work_ids), len(set(work_ids)))
            self.assertEqual(len(paragraph_ids), len(set(paragraph_ids)))
            self.assertEqual(
                source_ids,
                [item["source_file_id"] for item in second_index["source_files"]],
            )

    def test_marx_engels_volume_keeps_the_legacy_volume_model(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_public_build_root(base)
            source = root / "corpus" / "raw_docx" / "《马恩文集》第1卷.docx"
            source.parent.mkdir(parents=True)
            write_standalone_docx(source)

            extracted = extract_source(source, root)

            self.assertEqual(extracted["source_file"]["source_file_id"], "source-01")
            self.assertEqual(extracted["volume"]["volume_id"], "MEWJ-01")
            self.assertEqual(extracted["volume"]["corpus_title"], "马克思恩格斯文集")
            self.assertEqual(extracted["volume"]["primary_structure"], "article_collection")
            self.assertEqual(extracted["source_file"]["author"], "马克思、恩格斯")


class DataRootIndependentOfWorkingDirectoryTests(unittest.TestCase):
    def test_pdf_outside_the_working_directory_still_indexes(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_public_build_root(base)
            source = base / "CNKI_sample.pdf"
            write_native_pdf(source)
            register_pdf(root, copy_local_document(root, source))
            (root / "corpus" / "raw_docx").mkdir(parents=True, exist_ok=True)

            # Packaged builds run with a working directory that is not the data
            # root; indexing must not depend on the two matching.
            index = build_index(
                corpus_dir=root / "corpus" / "raw_docx",
                index_path=root / "data" / "index.json",
                database_path=root / "data" / "index.sqlite3",
                include_pdf=True,
                pdf_corpus_dir=root / "corpus" / "raw_pdf",
                pdf_config_path=root / "config" / "pdf_imports.json",
                parsed_pdf_dir=root / "corpus" / "parsed" / "pdf",
                root=root,
            )

            self.assertEqual(len(index["source_files"]), 1)
            self.assertEqual(index["audit_issues"], [])

    def test_relative_path_falls_back_instead_of_failing(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            inside = base / "corpus" / "raw_pdf" / "a.pdf"
            inside.parent.mkdir(parents=True)
            inside.write_bytes(b"%PDF-1.4\n")
            self.assertEqual(relative_to_root(inside, base), "corpus/raw_pdf/a.pdf")

            outside = base / "elsewhere.pdf"
            outside.write_bytes(b"%PDF-1.4\n")
            unrelated_root = base / "corpus"
            self.assertEqual(
                relative_to_root(outside, unrelated_root),
                outside.resolve().as_posix(),
            )


if __name__ == "__main__":
    unittest.main()
