"""Import returns MinerU's cross-page paragraphs to their printed pages.

A citation page is the number printed on the page that holds the text, so a
paragraph MinerU joined across a page break must be cut back at the break.
"""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.me_finder.export.page_reconstruction import attach_export_layout, reconstruct_export_pages
from src.me_finder.pdf.extractors import (
    load_mineru_pdf_pages,
    load_mineru_segments,
    make_pdf_paragraphs,
    pdf_page_text_hash,
    runtime_corpus_path,
)
from src.me_finder.pdf.page_split import mineru_layout_paths
from tests.test_export_page_reconstruction import reconstruction_fixture

PREFIX = "前页正文"
TAIL = "下一页正文。"


def content_list():
    """MinerU content_list for the layout of ``reconstruction_fixture``."""

    return [
        {"page_idx": 0, "type": "text", "text_level": 1, "text": "第一章 方法",
         "bbox": [100, 200, 900, 250]},
        {"page_idx": 0, "type": "text", "text": PREFIX + TAIL, "bbox": [100, 400, 900, 800]},
        {"page_idx": 1, "type": "text", "text": "下一段。", "bbox": [100, 250, 900, 300]},
    ]


class ImportPageSplitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.result_dir = self.root / "result"
        self.result_dir.mkdir()
        (self.result_dir / "content_list.json").write_text(
            json.dumps(content_list(), ensure_ascii=False), encoding="utf-8"
        )

    def write_layout(self, directory: Path, layout=None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "layout.json").write_text(
            json.dumps(layout or reconstruction_fixture()[1], ensure_ascii=False),
            encoding="utf-8",
        )

    def load(self, segment=None):
        segment = segment or {"result_dir": str(self.result_dir), "page_ranges": "1-2",
                              "page_index_offset": 0, "parser": "mineru"}
        with patch("src.me_finder.pdf.extractors.get_pdf_page_labels", return_value=[None, None]), \
                patch("src.me_finder.pdf.extractors.get_pdf_page_dimensions",
                      return_value=[(1000.0, 1000.0)] * 2):
            return load_mineru_pdf_pages(
                self.root / "unused.pdf", "book", "book", {}, [segment], root=self.root
            )

    def test_continuation_moves_to_the_page_it_is_printed_on(self):
        self.write_layout(self.result_dir)
        pages = self.load()

        self.assertEqual([p["text_raw"] for p in pages],
                         ["第一章 方法\n" + PREFIX, TAIL + "\n下一段。"])
        moved = pages[1]["blocks"][0]
        self.assertEqual(moved["page_split"], {"method": "mineru_layout_spans",
                                               "part": "continuation", "source_pdf_page_index": 0})
        self.assertEqual((moved["pdf_page_index"], moved["local_page_idx"]), (1, 1))
        self.assertEqual(pages[0]["blocks"][1]["page_split"]["part"], "head")
        self.assertEqual([p["page_text_boundary"] for p in pages], ["layout_checked"] * 2)
        for page in pages:
            self.assertEqual(page["page_text_hash"], pdf_page_text_hash(page["text_raw"]))
            self.assertEqual([b["block_index"] for b in page["blocks"]],
                             list(range(len(page["blocks"]))))
            for block in page["blocks"]:
                self.assertEqual(
                    page["text_raw"][block["page_char_start"]:block["page_char_end"]], block["text"]
                )
                self.assertFalse(any(key.startswith("_export") for key in block))
        json.dumps(pages, ensure_ascii=False)

    def test_export_does_not_split_an_already_split_page_again(self):
        self.write_layout(self.result_dir)
        pages = self.load()
        snapshot = deepcopy(pages)

        attach_export_layout(pages, self.root)
        report = reconstruct_export_pages(pages).report

        self.assertEqual(report["cross_page_block_count"], 0)
        self.assertEqual([p["text_raw"] for p in pages], [p["text_raw"] for p in snapshot])

    def test_without_layout_evidence_pages_stay_as_parsed_and_unmarked(self):
        pages = self.load()

        self.assertEqual(pages[0]["text_raw"], "第一章 方法\n" + PREFIX + TAIL)
        self.assertNotIn("page_text_boundary", pages[0])
        self.assertFalse(any("page_split" in b for p in pages for b in p["blocks"]))

    def test_unprovable_merge_stays_and_marks_the_page(self):
        _, layout = reconstruction_fixture()
        # A second identical span makes the source page of the tail ambiguous.
        layout["pdf_info"][1]["preproc_blocks"].append(
            deepcopy(layout["pdf_info"][1]["preproc_blocks"][0]) | {"index": 9})
        self.write_layout(self.result_dir, layout)
        pages = self.load()

        self.assertEqual(pages[0]["text_raw"], "第一章 方法\n" + PREFIX + TAIL)
        self.assertEqual([p["page_text_boundary"] for p in pages],
                         ["parser_merged", "layout_checked"])

    def test_blank_page_in_a_checked_slice_counts_as_checked(self):
        self.write_layout(self.result_dir)
        segment = {"result_dir": str(self.result_dir), "page_ranges": "1-3",
                   "page_index_offset": 0, "parser": "mineru"}
        with patch("src.me_finder.pdf.extractors.get_pdf_page_labels", return_value=[None] * 3), \
                patch("src.me_finder.pdf.extractors.get_pdf_page_dimensions",
                      return_value=[(1000.0, 1000.0)] * 3):
            pages = load_mineru_pdf_pages(
                self.root / "unused.pdf", "book", "book", {}, [segment], root=self.root
            )

        self.assertEqual(pages[2]["blocks"], [])
        self.assertEqual([p["page_text_boundary"] for p in pages], ["layout_checked"] * 3)

    def test_engine_slice_layout_is_found_through_the_job_ledger(self):
        provider = self.root / "corpus/processed/parser_jobs/job-1/provider/slice-a"
        self.write_layout(provider)
        ledger = self.root / "data" / "parser_jobs.sqlite3"
        ledger.parent.mkdir()
        with sqlite3.connect(ledger) as connection:
            connection.execute("CREATE TABLE slice_jobs (id TEXT, document_job_id TEXT, "
                               "page_start INTEGER, page_end INTEGER)")
            connection.execute("INSERT INTO slice_jobs VALUES ('slice-a', 'job-1', 1, 2)")
        connection.close()
        segment = {"result_dir": str(self.result_dir), "page_start": 1, "page_end": 2,
                   "page_ranges": "1-2", "page_index_offset": 0, "parser": "mineru",
                   "document_job_id": "job-1"}

        self.assertEqual(mineru_layout_paths([segment], self.root),
                         {str(self.result_dir): provider / "layout.json"})
        self.assertEqual(self.load(segment)[1]["text_raw"], TAIL + "\n下一段。")

    def test_recorded_slice_id_needs_no_ledger(self):
        provider = self.root / "corpus/processed/parser_jobs/job-2/provider/slice-b"
        self.write_layout(provider)
        segment = {"result_dir": str(self.result_dir), "document_job_id": "job-2",
                   "parser_slice_id": "slice-b"}

        self.assertEqual(mineru_layout_paths([segment], self.root),
                         {str(self.result_dir): provider / "layout.json"})
        self.assertEqual(mineru_layout_paths([segment], None), {})


class RuntimeCorpusPathTests(unittest.TestCase):
    """Manifests written under another data root still find their cache here."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cache = self.root / "corpus/processed/mineru/results/engine-book/pages-000001-000002"
        self.cache.mkdir(parents=True)

    def test_paths_from_other_machines_and_old_roots_rebase_onto_this_root(self):
        for recorded in (
            "D:\\ME_Finder\\dist\\MEFinderData\\runtime\\corpus\\processed\\mineru\\results"
            "\\engine-book\\pages-000001-000002",
            "corpus\\processed\\mineru\\results\\engine-book\\pages-000001-000002",
            "/Users/someone/Library/CloudStorage/OneDrive/MeFinder/runtime/corpus/processed/"
            "mineru/results/engine-book/pages-000001-000002",
        ):
            with self.subTest(recorded=recorded):
                self.assertEqual(runtime_corpus_path(recorded, self.root), str(self.cache))

    def test_readable_and_unresolvable_paths_keep_their_previous_meaning(self):
        relative = "corpus/processed/mineru/results/engine-book/pages-000001-000002"
        self.assertEqual(runtime_corpus_path(relative, self.root), str(self.root / relative))
        self.assertEqual(runtime_corpus_path(str(self.cache), self.root), str(self.cache))
        self.assertEqual(runtime_corpus_path("missing/dir", self.root), str(self.root / "missing/dir"))
        self.assertEqual(runtime_corpus_path("D:\\x\\y", None), "D:\\x\\y")
        self.assertIsNone(runtime_corpus_path("  ", self.root))

    def test_manifest_segments_are_rebased_when_loaded(self):
        manifest = self.root / "corpus/processed/mineru/manifests/segments-book.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"parser": "mineru", "document_job_id": "job", "segments": [{
            "page_ranges": "1-2", "page_index_offset": 0,
            "result_dir": "E:\\OneDrive\\MeFinder\\runtime\\corpus\\processed\\mineru\\results"
                          "\\engine-book\\pages-000001-000002",
        }]}), encoding="utf-8")
        config = {"mineru": {"manifest": "corpus\\processed\\mineru\\manifests\\segments-book.json"}}

        [segment] = load_mineru_segments(config, root=self.root)

        self.assertEqual(segment["result_dir"], str(self.cache))
        self.assertEqual(segment["document_job_id"], "job")


class CrossPageWindowTests(unittest.TestCase):
    @staticmethod
    def page(index, parts):
        text = "\n".join(text for text, _ in parts)
        blocks, cursor = [], 0
        for text_part, role in parts:
            blocks.append({"text": text_part, "mineru_type": role,
                           "page_char_start": cursor, "page_char_end": cursor + len(text_part)})
            cursor += len(text_part) + 1
        return {"pdf_page_id": f"doc-PAGE-{index:06d}", "pdf_page_index": index,
                "pdf_page_number_1based": index + 1, "citation_page": str(index + 11),
                "text_raw": text, "page_text_hash": pdf_page_text_hash(text), "blocks": blocks}

    def test_window_joins_body_lines_across_running_heads_notes_and_folios(self):
        left_body = "这一页的正文写到页底仍未结束，" * 4 + "句子跨过"
        right_body = "页界继续写下去，" * 4 + "直到句号。"
        pages = [
            self.page(0, [(left_body, "text"), ("学术作为一种志业", "header"),
                          ("① 页下注。", "page_footnote"), ("11", "page_number")]),
            self.page(1, [("学术与政治", "header"), (right_body, "text"), ("12", "page_number")]),
        ]

        cross = [p for p in make_pdf_paragraphs("doc", "doc", "书", None, "doc.pdf", pages, "W")
                 if p["is_cross_page"]]

        self.assertEqual([p["text_raw"] for p in cross], [left_body + "\n" + right_body])


if __name__ == "__main__":
    unittest.main()
