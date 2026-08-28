from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from xml.etree import ElementTree as ET

from src.me_finder.epub_export import build_epub_bytes
from src.me_finder.document_export import DocumentExportError
from src.me_finder.export_footnotes import FootnoteText, normalize_document_export
from src.me_finder.export_page_reconstruction import (
    ReconstructedPages, attach_export_layout, reconstruct_export_pages, reconstruction_invariant,
)
from src.me_finder.markdown_export import document_to_markdown
from tests.test_markdown_export import _block, _page, footnote_fixture


def reconstruction_fixture(prefix="前页正文", tail="下一页正文。", *, note=False):
    def native(index, text, bbox, role="text"):
        return {"index": index, "type": role, "bbox": bbox,
                "lines": [{"spans": [{"type": "text", "content": text, "bbox": bbox}]}]}

    heading = native(0, "第一章 方法", [100, 200, 900, 250])
    first = native(1, prefix, [100, 400, 900, 800])
    second = native(1, tail, [100, 100, 900, 160])
    following = native(2, "下一段。", [100, 250, 900, 300])
    merged = deepcopy(first)
    merged["lines"] += deepcopy(second["lines"])
    merged["lines"][1]["spans"][0]["cross_page"] = True
    layout = {"pdf_info": [
        {"page_idx": 0, "page_size": [1000, 1000], "preproc_blocks": [heading, first],
         "para_blocks": [heading, merged]},
        {"page_idx": 1, "page_size": [1000, 1000], "preproc_blocks": [second, following],
         "para_blocks": [{"index": 1, "bbox": second["bbox"], "lines_deleted": True}, following]},
    ]}
    pages = [
        _page(10, "1", [_block(heading["lines"][0]["spans"][0]["content"], bbox=heading["bbox"], level=1, role="text"),
                        _block(prefix + tail, bbox=first["bbox"], role="text")]),
        _page(11, "2", [_block("下一段。", bbox=following["bbox"], role="text")]),
    ]
    if note:
        body = native(3, "① 注释原文。", [100, 850, 900, 910], "page_footnote")
        layout["pdf_info"][1]["discarded_blocks"] = [body]
        pages[1]["blocks"].append(_block("① 注释原文。", bbox=body["bbox"], role="page_footnote"))
    for local, page in enumerate(pages):
        page["source_file_id"] = "reconstruction-book"
        page["text_raw"] = "\n".join(b["text"] for b in page["blocks"])
        for index, block in enumerate(page["blocks"]):
            block.update(local_page_idx=local, pdf_page_index=10 + local, page_index_offset=10,
                         block_index=index, parser_item_index=40 + 10 * local + index,
                         page_char_start=10 * index, page_char_end=10 * index + len(block["text"]))
    return pages, layout


class ExportPageReconstructionTests(unittest.TestCase):
    def enrich(self, pages, layout):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "layout.json").write_text(json.dumps(layout, ensure_ascii=False), encoding="utf-8")
        for page in pages:
            for block in page["blocks"]:
                block["result_dir"] = str(root)
        attach_export_layout(pages, root)

    def test_text_without_notes_reconstructs_before_anchors_and_preserves_source_identity(self):
        pages, layout = reconstruction_fixture()
        self.enrich(pages, layout)
        snapshot = deepcopy(pages)
        rebuilt = reconstruct_export_pages(pages)
        self.assertEqual(pages, snapshot)
        self.assertEqual(rebuilt.report["reconstructed_block_count"], 1)
        self.assertEqual(rebuilt.report["content_invariant"], {
            "checked_block_count": 1, "checked_span_count": 2, "missing_span_count": 0,
            "duplicated_span_count": 0, "unexpected_span_count": 0, "content_order_invariant_failure_count": 0,
        })
        first, second = rebuilt.pages[0]["blocks"][1], rebuilt.pages[1]["blocks"][0]
        self.assertEqual(first["text"] + second["text"], pages[0]["blocks"][1]["text"])
        for piece in (first, second):
            for key in ("block_index", "parser_item_index", "pdf_page_index", "page_char_start", "page_char_end"):
                self.assertEqual(piece[key], pages[0]["blocks"][1][key])
            fragment = piece["_export_source_fragment"]
            self.assertEqual(fragment.source_block_id, "reconstruction-book:p10:b1")
            self.assertEqual(fragment.source_page_index, 10)
            self.assertEqual(piece["text"], pages[0]["blocks"][1]["text"][fragment.source_char_start:fragment.source_char_end])
        self.assertEqual(second["_export_source_fragment"].target_physical_page, 12)
        self.assertEqual(second["_export_source_fragment"].target_printed_page, "2")
        doc = normalize_document_export(pages)
        markdown = document_to_markdown([], normalized=doc)
        self.assertLess(markdown.index("# 第一章 方法"), markdown.index("前页正文"))
        self.assertLess(markdown.index("前页正文"), markdown.index("<!-- printed_page: 2 -->"))
        self.assertLess(markdown.index("<!-- printed_page: 2 -->"), markdown.index("下一页正文。"))
        self.assertLess(markdown.index("下一页正文。"), markdown.index("下一段。"))
        self.assertEqual(markdown.count("printed_page:"), 2)
        self.assertNotIn("pdf_page", markdown)

    def test_reference_moves_to_proven_page_then_unchanged_matcher_pairs_it(self):
        pages, layout = reconstruction_fixture(tail="下一页引用①。", note=True)
        before = normalize_document_export(pages)
        self.assertEqual(before.footnote_report["matched_ref_count"], 0)
        self.enrich(pages, layout)
        doc = normalize_document_export(pages)
        report = doc.footnote_report
        self.assertEqual(doc.reconstruction_report["content_invariant"]["checked_span_count"], 2)
        self.assertEqual(doc.reconstruction_report["content_invariant"]["content_order_invariant_failure_count"], 0)
        self.assertEqual(report["candidate_ref_count"], before.footnote_report["candidate_ref_count"])
        self.assertEqual(report["candidate_note_count"], before.footnote_report["candidate_note_count"])
        self.assertEqual(report["match_reason"]["ref"], {"SAME_PAGE_UNIQUE_MARKER": 1})
        ref = next(c for c in report["candidates"] if c["kind"] == "ref")
        self.assertEqual((ref["source_page_index"], ref["export_page_index"]), (10, 11))
        original = pages[0]["blocks"][1]["text"]
        self.assertEqual(original[ref["source_start"]:ref["source_end"]], "①")
        note = next(c for c in report["candidates"] if c["kind"] == "note")
        self.assertEqual(note["source_block_index"], 1)  # insertion must not renumber the source note
        self.assertTrue(any(isinstance(i, FootnoteText) and i.source_fragment for i in doc.items))
        markdown = document_to_markdown([], normalized=doc)
        with zipfile.ZipFile(io.BytesIO(build_epub_bytes([], normalized=doc))) as archive:
            body = ET.fromstring(archive.read("OEBPS/content.xhtml"))
        ids = {e.attrib["id"] for e in body.iter() if "id" in e.attrib}
        for element in body.iter():
            if element.attrib.get("href", "").startswith("#"):
                self.assertIn(element.attrib["href"][1:], ids)
        self.assertEqual(markdown.count("[^" + note["note_id"] + "]"), 2)
        self.assertEqual([e.attrib["aria-label"] for e in body.iter() if e.attrib.get("role") == "doc-pagebreak"], ["1", "2"])

    def test_sentence_split_uses_span_boundary_and_preserves_whitespace(self):
        pages, layout = reconstruction_fixture("这个句子尚未", "结束。")
        pages[0]["blocks"][1]["text"] = "这个句子尚未 \n 结束。"
        pages[0]["text_raw"] = "第一章 方法\n" + pages[0]["blocks"][1]["text"]
        self.enrich(pages, layout)
        rebuilt = reconstruct_export_pages(pages)
        first = rebuilt.pages[0]["blocks"][1]
        second = rebuilt.pages[1]["blocks"][0]
        self.assertEqual(first["text"], "这个句子尚未 \n ")
        self.assertEqual(second["text"], "结束。")
        self.assertEqual(first["text"] + second["text"], pages[0]["blocks"][1]["text"])
        self.assertTrue(rebuilt.report["blocks"][0]["content_invariant"]["source_characters_preserved"])

    def test_cross_page_note_body_is_retained_without_continuation_inference(self):
        pages, layout = reconstruction_fixture("① 页末注释尚未", "结束。")
        pages[0]["blocks"][1]["mineru_type"] = "page_footnote"
        self.enrich(pages, layout)
        rebuilt = reconstruct_export_pages(pages)
        self.assertEqual(rebuilt.report["reason_counts"], {"UNSUPPORTED_BLOCK_TYPE": 1})
        self.assertEqual(rebuilt.pages[0]["blocks"][1]["text"], "① 页末注释尚未结束。")
        doc = normalize_document_export(pages)
        self.assertEqual(doc.footnote_report["matched_note_count"], 0)
        self.assertIn("① 页末注释尚未结束。", document_to_markdown([], normalized=doc))

    def test_multiple_spans_and_inline_equation_keep_exact_ranges(self):
        pages, layout = reconstruction_fixture("前半甲乙", "后半$^{①}$。")
        first, second = layout["pdf_info"]
        a = [{"type": "text", "content": "前半甲", "bbox": [100, 400, 500, 450]},
             {"type": "text", "content": "乙", "bbox": [100, 500, 500, 550]}]
        b = [{"type": "text", "content": "后半", "bbox": [100, 100, 300, 160]},
             {"type": "inline_equation", "content": "^{①}", "bbox": [310, 100, 350, 160]},
             {"type": "text", "content": "。", "bbox": [360, 100, 380, 160]}]
        first["preproc_blocks"][1]["lines"] = [{"spans": a}]
        second["preproc_blocks"][0]["lines"] = [{"spans": b}]
        first["para_blocks"][1]["lines"] = [{"spans": deepcopy(a)}, {"spans": [{**s, "cross_page": True} for s in b]}]
        self.enrich(pages, layout)
        rebuilt = reconstruct_export_pages(pages)
        fragments = rebuilt.report["blocks"][0]["fragments"]
        self.assertEqual([len(f["spans"]) for f in fragments], [2, 3])
        check = rebuilt.report["blocks"][0]["content_invariant"]
        self.assertEqual(check["checked_span_count"], 5)
        self.assertEqual(check["content_order_invariant_failure_count"], 0)
        self.assertEqual(rebuilt.pages[1]["blocks"][0]["text"], "后半$^{①}$。")

    def test_missing_or_conflicting_span_evidence_never_splits(self):
        for case, reason in (
            ("no_native", "NATIVE_SPAN_NOT_UNIQUE"),
            ("no_span_bbox", "NATIVE_SPAN_NOT_UNIQUE"),
            ("ambiguous_native", "NATIVE_SPAN_NOT_UNIQUE"),
            ("changed_text", "SOURCE_TEXT_MISMATCH"),
            ("not_deleted", "TARGET_NOT_DELETED_BY_MERGE"),
            ("flag_conflict", "SOURCE_FLAG_CONFLICT"),
            ("heading", "HEADING_BLOCK"),
            ("no_target", "TARGET_PAGE_UNAVAILABLE"),
            ("missing_block_bbox", "MISSING_NATIVE_BLOCK_BBOX"),
            ("duplicate_native_index", "NATIVE_BLOCK_INDEX_NOT_UNIQUE"),
        ):
            with self.subTest(case=case):
                pages, layout = reconstruction_fixture()
                first, second = layout["pdf_info"]
                if case == "no_native":
                    second["preproc_blocks"] = []
                elif case == "no_span_bbox":
                    first["para_blocks"][1]["lines"][1]["spans"][0].pop("bbox")
                elif case == "ambiguous_native":
                    second["preproc_blocks"].append(deepcopy(second["preproc_blocks"][0]))
                elif case == "changed_text":
                    pages[0]["blocks"][1]["text"] += "无法解释的文本"
                elif case == "not_deleted":
                    second["para_blocks"][0]["lines_deleted"] = False
                elif case == "flag_conflict":
                    first["para_blocks"][1]["lines"][0]["spans"][0]["cross_page"] = True
                elif case == "heading":
                    pages[0]["blocks"][1]["text_level"] = 1
                elif case == "no_target":
                    pages.pop()
                elif case == "missing_block_bbox":
                    second["preproc_blocks"][0].pop("bbox")
                elif case == "duplicate_native_index":
                    other = deepcopy(second["preproc_blocks"][0])
                    other["lines"][0]["spans"][0]["content"] = "不同文本却重用了同一 block index"
                    second["preproc_blocks"].append(other)
                self.enrich(pages, layout)
                rebuilt = reconstruct_export_pages(pages)
                self.assertEqual(rebuilt.report["reason_counts"], {reason: 1})
                self.assertEqual(rebuilt.report["reconstructed_block_count"], 0)
                self.assertEqual(rebuilt.report["content_invariant"]["checked_block_count"], 0)
                self.assertEqual([p["text_raw"] for p in rebuilt.pages], [p["text_raw"] for p in pages])

    def test_boolean_flag_alone_does_not_reconstruct(self):
        pages, _ = reconstruction_fixture()
        pages[0]["blocks"][1]["cross_page"] = True
        report = reconstruct_export_pages(pages).report
        self.assertEqual(report["reason_counts"], {"NO_SPAN_EVIDENCE": 1})

    def test_empty_target_span_is_not_a_body_fragment(self):
        pages, layout = reconstruction_fixture(tail="  ")
        self.enrich(pages, layout)
        self.assertEqual(reconstruct_export_pages(pages).report["reason_counts"], {"EMPTY_PAGE_FRAGMENT": 1})

    def test_three_page_merge_stays_whole(self):
        pages, layout = reconstruction_fixture()
        third = deepcopy(layout["pdf_info"][1]["preproc_blocks"][0])
        third["lines"][0]["spans"][0]["content"] = "第三页尾句。"
        layout["pdf_info"].append({"page_idx": 2, "page_size": [1000, 1000], "preproc_blocks": [third]})
        layout["pdf_info"][0]["para_blocks"][1]["lines"].append(
            {"spans": [{**third["lines"][0]["spans"][0], "cross_page": True}]})
        pages[0]["blocks"][1]["text"] += "第三页尾句。"
        pages[0]["text_raw"] = "\n".join(b["text"] for b in pages[0]["blocks"])
        self.enrich(pages, layout)
        snapshot = deepcopy(pages)
        rebuilt = reconstruct_export_pages(pages)
        self.assertEqual(rebuilt.report["reason_counts"], {"NOT_TWO_CONSECUTIVE_EVIDENCED_PAGES": 1})
        self.assertEqual(rebuilt.report["content_invariant"]["checked_span_count"], 0)
        self.assertEqual(pages, snapshot)
        self.assertEqual([p["text_raw"] for p in rebuilt.pages], [p["text_raw"] for p in pages])
        self.assertEqual(rebuilt.pages[0]["blocks"][1]["text"], pages[0]["blocks"][1]["text"])

    def test_invariant_detects_missing_duplicate_reordered_and_changed_fragments(self):
        pages, layout = reconstruction_fixture()
        self.enrich(pages, layout)
        rebuilt = reconstruct_export_pages(pages)
        original = pages[0]["blocks"][1]
        first, second = rebuilt.pages[0]["blocks"][1], rebuilt.pages[1]["blocks"][0]
        for case, blocks, metric, expected in (
            ("missing", [first], "missing_span_count", 1),
            ("duplicate", [first, first, second], "duplicated_span_count", 1),
            ("order", [second, first], "span_order_preserved", False),
            ("character", [{**first, "text": first["text"] + "改"}, second], "source_characters_preserved", False),
            ("wrong_fragment_boundary", [{**first, "text": first["text"] + second["text"][0]},
                                         {**second, "text": second["text"][1:]}], "fragment_text_matches_spans", False),
        ):
            with self.subTest(case=case):
                check = reconstruction_invariant(original, blocks)
                self.assertEqual(check[metric], expected)
                self.assertEqual(check["content_order_invariant_failure_count"], 1)

    def test_export_stops_when_actual_fragment_content_is_not_conserved(self):
        pages, layout = reconstruction_fixture()
        self.enrich(pages, layout)
        block = pages[0]["blocks"][1]
        first, second = block["_export_fragments"]
        block["_export_fragments"] = (replace(first, source_char_end=first.source_char_end - 1), second)
        snapshot = deepcopy(pages)
        with self.assertRaisesRegex(DocumentExportError, "页面重建内容守恒失败.*page_index=10.*block_index=1"):
            normalize_document_export(pages)
        self.assertEqual(pages, snapshot)

    def test_target_order_and_raw_text_trust_gate(self):
        for case, reason in (("order", "TARGET_BLOCK_ORDER_UNPROVEN"),
                             ("text", "SOURCE_OR_TARGET_TEXT_UNALIGNED")):
            with self.subTest(case=case):
                pages, layout = reconstruction_fixture()
                self.enrich(pages, layout)
                if case == "order":
                    pages[1]["blocks"][0].pop("_export_layout_order")
                else:
                    pages[1]["text_raw"] = "用户修改后的正文"
                rebuilt = reconstruct_export_pages(pages)
                self.assertEqual(rebuilt.report["reason_counts"], {reason: 1})

    def test_media_formula_table_and_existing_body_order_are_untouched(self):
        pages, layout = reconstruction_fixture()
        for order, role in enumerate(("image", "equation", "table"), 3):
            bbox = [100, order * 100, 900, order * 100 + 50]
            text = {"image": "![图](image.png)", "equation": "$$x=1$$", "table": "|甲|乙|\n|--|--|"}[role]
            block = _block(text, bbox=bbox, role=role)
            block.update(local_page_idx=1, parser_item_index=order + 60, block_index=order)
            pages[1]["blocks"].append(block)
            layout["pdf_info"][1]["para_blocks"].append({"index": order, "type": role, "bbox": bbox})
        pages[1]["text_raw"] = "\n".join(b["text"] for b in pages[1]["blocks"])
        self.enrich(pages, layout)
        rebuilt = reconstruct_export_pages(pages)
        self.assertEqual(rebuilt.report["reconstructed_block_count"], 1)
        for old, new in zip(pages[1]["blocks"], rebuilt.pages[1]["blocks"][1:]):
            self.assertEqual(old, {k: v for k, v in new.items() if k != "_export_index"})
        markdown = document_to_markdown(pages)
        for text in ("![图](image.png)", "$$x=1$$", "|甲|乙|"):
            self.assertIn(text, markdown)

    def test_non_cross_page_document_has_identical_rendered_output(self):
        pages = footnote_fixture()
        with patch("src.me_finder.export_footnotes.reconstruct_export_pages", return_value=ReconstructedPages(pages, {})):
            before = normalize_document_export(pages)
        after = normalize_document_export(pages)
        self.assertEqual(after.items, before.items)
        self.assertEqual(document_to_markdown([], normalized=after), document_to_markdown([], normalized=before))
        with zipfile.ZipFile(io.BytesIO(build_epub_bytes([], normalized=before, identifier="same", modified="2026-08-28T00:00:00Z"))) as a:
            with zipfile.ZipFile(io.BytesIO(build_epub_bytes([], normalized=after, identifier="same", modified="2026-08-28T00:00:00Z"))) as b:
                self.assertEqual({name: a.read(name) for name in a.namelist()}, {name: b.read(name) for name in b.namelist()})


if __name__ == "__main__":
    unittest.main()
