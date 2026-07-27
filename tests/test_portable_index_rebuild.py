from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.me_finder.database import build_database
from src.me_finder.indexer import build_index
from src.me_finder.mineru_api import MinerUError
from src.me_finder.pdf_extractors import relative_to_root
from src.me_finder.pdf_import_service import (
    copy_local_document,
    indexed_word_source_count,
    rebuild_local_index,
    register_pdf,
)

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
