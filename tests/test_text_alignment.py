from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import numpy as np

from src.me_finder.backup_service import create_backup, restore_backup
from src.me_finder.database import (
    build_database,
    optimize_database_storage,
    replace_source_in_database,
)
from src.me_finder.document_groups import add_group_member, set_document_group_base
from src.me_finder.persistence.index_schema import SCHEMA
from src.me_finder.persistence.connection import open_readonly_index
from src.me_finder.text_alignment import (
    ALIGNMENT_ALGORITHM_VERSION,
    AlignmentNotFound,
    InvalidAlignmentRequest,
    align_segment_sequences,
    generate_alignment,
    list_alignment_targets,
    locate_alignment,
    read_alignment_recipe_snapshot,
    replace_alignment_recipe_snapshot,
    segment_paragraph_text,
    segment_pdf_text,
    PageText,
    ParagraphText,
    _load_pages,
)
from src.me_finder.semantic_alignment import (
    embed_text_sequences as cache_text_sequences,
    find_heading_anchors,
)


def _page(source_id: str, index: int, text: str, blocks=None):
    return {
        "source_file_id": source_id,
        "source_type": "pdf",
        "pdf_page_id": f"{source_id}-PAGE-{index:06d}",
        "pdf_page_index": index,
        "pdf_page_number_1based": index + 1,
        "text_raw": text,
        "blocks": blocks or [],
    }


def _fake_embeddings(texts, _cache_dir):
    vectors = []
    for text in texts:
        if "Geist" in text or "精神" in text or "Spirit" in text:
            vectors.append((1.0, 0.0, 0.0))
        elif "Wahrheit" in text or "真理" in text or "Truth" in text:
            vectors.append((0.0, 1.0, 0.0))
        else:
            vectors.append((0.0, 0.0, 1.0))
    return np.asarray(vectors, dtype=np.float32)


def _fake_embedding_sequences(sequences, cache_dir, **_kwargs):
    return [_fake_embeddings(texts, cache_dir) for texts in sequences]


class TextAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.embedding_patch = mock.patch(
            "src.me_finder.text_alignment.embed_text_sequences",
            side_effect=_fake_embedding_sequences,
        )
        self.embedding_patch.start()
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "index.sqlite3"
        connection = sqlite3.connect(str(self.db))
        connection.executescript(SCHEMA)
        sources = (
            {
                "source_file_id": "pdf-de",
                "source_type": "pdf",
                "file_name": "phaenomenologie.pdf",
                "title": "Phänomenologie des Geistes",
                "language_code": "de",
            },
            {
                "source_file_id": "pdf-zh",
                "source_type": "pdf",
                "file_name": "精神现象学.pdf",
                "title": "精神现象学",
                "translator": "贺麟",
                "language_code": "zh-Hans",
            },
            {
                "source_file_id": "epub-en",
                "source_type": "word",
                "file_name": "phenomenology.epub",
                "title": "Phenomenology of Spirit",
                "language_code": "en",
                "file_format": "epub",
            },
        )
        connection.executemany(
            "INSERT INTO source_files(source_file_id, source_type, file_name, "
            "relative_path, volume_number, payload_json) VALUES (?, ?, ?, NULL, NULL, ?)",
            [
                (
                    source["source_file_id"],
                    source["source_type"],
                    source["file_name"],
                    json.dumps(source, ensure_ascii=False),
                )
                for source in sources
            ],
        )
        connection.execute(
            "INSERT INTO document_groups(document_group_id, title, "
            "base_source_file_id, created_at, updated_at) "
            "VALUES ('work-one', '精神现象学', 'pdf-de', 't', 't')"
        )
        connection.executemany(
            "INSERT INTO document_group_members(document_group_id, "
            "source_file_id, version_label, member_order, added_at) "
            "VALUES ('work-one', ?, ?, ?, 't')",
            (
                ("pdf-de", "德文", 0),
                ("pdf-zh", "贺麟译本", 1),
                ("epub-en", "English EPUB", 2),
            ),
        )
        german = _page(
            "pdf-de",
            0,
            "Der Geist ist wirklich. Die Wahrheit ist das Ganze.",
            [
                {
                    "block_index": 0,
                    "text": "Der Geist ist wirklich. Die Wahrheit ist das Ganze.",
                    "bbox": [10, 20, 300, 80],
                    "bbox_normalized": [0.01, 0.02, 0.3, 0.08],
                }
            ],
        )
        chinese = _page(
            "pdf-zh",
            0,
            "精神是现实的。真理是全体。",
            [
                {
                    "block_index": 0,
                    "text": "精神是现实的。真理是全体。",
                    "bbox": [12, 24, 280, 72],
                    "bbox_normalized": [0.02, 0.03, 0.4, 0.09],
                }
            ],
        )
        connection.executemany(
            "INSERT INTO pdf_pages(source_file_id, pdf_page_index, payload_json) "
            "VALUES (?, ?, ?)",
            [
                ("pdf-de", 0, json.dumps(german, ensure_ascii=False)),
                ("pdf-zh", 0, json.dumps(chinese, ensure_ascii=False)),
            ],
        )
        english_paragraphs = (
            ("epub-en-p0", 0, "Spirit is actual."),
            ("epub-en-p1", 1, "Truth is the whole."),
        )
        connection.executemany(
            "INSERT INTO paragraphs(paragraph_id, volume_id, work_id, "
            "source_file_id, source_type, paragraph_index, eligible_for_search, "
            "text_raw, normalized_text, compact_text, plain_text, payload_json) "
            "VALUES (?, NULL, NULL, 'epub-en', 'word', ?, 1, ?, ?, ?, ?, ?)",
            [
                (
                    paragraph_id,
                    paragraph_index,
                    text,
                    text.casefold(),
                    text.replace(" ", "").casefold(),
                    text,
                    json.dumps(
                        {
                            "paragraph_id": paragraph_id,
                            "paragraph_index": paragraph_index,
                            "text_raw": text,
                            "source_format": "epub",
                        }
                    ),
                )
                for paragraph_id, paragraph_index, text in english_paragraphs
            ],
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()
        self.embedding_patch.stop()

    def test_segmenter_handles_latin_sentences_and_cross_page_spans(self) -> None:
        full_text = "Ein Satz endet.\n跨页句子结束。"
        pages = [
            PageText(0, {}, "Ein Satz endet.", 0, 15),
            PageText(1, {}, "跨页句子结束。", 16, len(full_text)),
        ]
        segments = segment_pdf_text(full_text, pages)
        self.assertEqual([item.text for item in segments], ["Ein Satz endet.", "跨页句子结束。"])
        self.assertEqual(segments[1].spans, ((1, 0, 7),))

    def test_sentence_continuation_can_span_an_ordinary_page_boundary(self) -> None:
        first = "This sentence continues"
        second = "on the next page."
        full_text = f"{first}\n{second}"
        pages = [
            PageText(0, {}, first, 0, len(first)),
            PageText(1, {}, second, len(first) + 1, len(full_text)),
        ]

        segments = segment_pdf_text(full_text, pages)

        self.assertEqual([segment.text for segment in segments], [full_text])
        self.assertEqual(
            segments[0].spans,
            ((0, 0, len(first)), (1, 0, len(second))),
        )

    def test_page_loader_does_not_double_an_existing_boundary_newline(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE pdf_pages (row_id INTEGER PRIMARY KEY, "
            "source_file_id TEXT, pdf_page_index INTEGER, payload_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO pdf_pages(source_file_id, pdf_page_index, payload_json) "
            "VALUES ('source', ?, ?)",
            (
                (0, json.dumps({"text_raw": "sentence continues\n"})),
                (1, json.dumps({"text_raw": "on this page."})),
            ),
        )

        full_text, pages = _load_pages(connection, "source")
        connection.close()

        self.assertEqual(full_text, "sentence continues\non this page.")
        self.assertEqual(pages[1].global_start, len("sentence continues\n"))

    def test_pdf_page_break_stops_toc_text_from_absorbing_next_page(self) -> None:
        first = "目录行没有句号"
        second = "正文从这里开始。"
        full_text = f"{first}\n\n{second}"
        pages = [
            PageText(0, {}, first, 0, len(first)),
            PageText(1, {}, second, len(first) + 2, len(full_text)),
        ]

        segments = segment_pdf_text(full_text, pages)

        self.assertEqual([segment.text for segment in segments], [first, second])
        self.assertEqual(segments[0].spans, ((0, 0, len(first)),))
        self.assertEqual(segments[1].spans, ((1, 0, len(second)),))

    def test_toc_dot_leaders_do_not_become_hundreds_of_segments(self) -> None:
        text = "Inhalt\nEinleitung  .  .  .  .  .  7\nErstes Kapitel  .  .  .  10"

        segments = segment_pdf_text(
            text,
            [PageText(0, {}, text, 0, len(text))],
        )

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, text)

    def test_pdf_segmenter_omits_only_confirmed_parser_placeholders(self) -> None:
        placeholders = [
            "The image contains no discernible text or characters.",
            'The OCR result "C" is a hallucination.',
            "Therefore, the correct OCR output must reflect the absence of text.",
            "[No text detected]",
        ]
        normal = "The image contains an argument about consumer society."
        full_text = "\n\n".join([*placeholders, normal])

        segments = segment_pdf_text(
            full_text,
            [PageText(0, {}, full_text, 0, len(full_text))],
        )

        self.assertEqual([segment.text for segment in segments], [normal])

    def test_pdf_segmenter_excludes_page_number_blocks_from_semantic_text(self) -> None:
        text = "正文在这里。\n60\n脚注仍然保留。"
        marker_start = text.index("60")
        page = PageText(
            0,
            {
                "blocks": [
                    {
                        "text": "60",
                        "mineru_type": "page_number",
                        "page_char_start": marker_start,
                        "page_char_end": marker_start + 2,
                    }
                ]
            },
            text,
            0,
            len(text),
        )

        segments = segment_pdf_text(text, [page])

        self.assertEqual(
            [segment.text for segment in segments],
            ["正文在这里。", "脚注仍然保留。"],
        )

    def test_pdf_segmenter_excludes_repeated_headers_and_margin_line_numbers(self) -> None:
        page_texts = [
            "Grundlinien · Sittlichkeit 164\n5\nBody one.",
            "Grundlinien · Sittlichkeit 165\n10\nBody two.",
            "Grundlinien · Sittlichkeit 166\n15\nBody three.",
        ]
        full_text = "\n".join(page_texts)
        pages = []
        cursor = 0
        for index, text in enumerate(page_texts):
            header_end = text.index("\n")
            number_start = header_end + 1
            number_end = text.index("\n", number_start)
            pages.append(
                PageText(
                    index,
                    {
                        "page_width": 1000,
                        "page_height": 1000,
                        "blocks": [
                            {
                                "text": text[:header_end],
                                "bbox_normalized": [0.16, 0.06, 0.64, 0.09],
                                "page_char_start": 0,
                                "page_char_end": header_end,
                            },
                            {
                                "text": text[number_start:number_end],
                                "bbox_normalized": [0.88, 0.4, 0.9, 0.42],
                                "page_char_start": number_start,
                                "page_char_end": number_end,
                            },
                        ],
                    },
                    text,
                    cursor,
                    cursor + len(text),
                )
            )
            cursor += len(text) + 1

        segments = segment_pdf_text(full_text, pages)

        self.assertEqual(
            [segment.text for segment in segments],
            ["Body one.", "Body two.", "Body three."],
        )

    def test_pdf_segmenter_keeps_body_heading_that_matches_running_header(self) -> None:
        page_texts = [
            "第一章 主体\n第一章 主体\n正文一。",
            "第一章 主体\n正文二。",
            "第一章 主体\n正文三。",
        ]
        full_text = "\n".join(page_texts)
        pages = []
        cursor = 0
        for index, text in enumerate(page_texts):
            header_end = text.index("\n")
            blocks = [{
                "text": text[:header_end],
                "bbox_normalized": [0.16, 0.06, 0.64, 0.09],
                "page_char_start": 0,
                "page_char_end": header_end,
            }]
            if index == 0:
                body_start = header_end + 1
                body_end = text.index("\n", body_start)
                blocks.append({
                    "text": text[body_start:body_end],
                    "bbox_normalized": [0.16, 0.3, 0.64, 0.34],
                    "page_char_start": body_start,
                    "page_char_end": body_end,
                })
            pages.append(PageText(index, {"blocks": blocks}, text, cursor, cursor + len(text)))
            cursor += len(text) + 1

        segments = segment_pdf_text(full_text, pages)

        self.assertEqual(
            [segment.text for segment in segments],
            ["第一章 主体", "正文一。", "正文二。", "正文三。"],
        )

    def test_structural_markers_are_independent_segments_for_pdf_and_epub(self) -> None:
        pdf_text = "Vorwort\n§ 2l6\nDer Staat ist wirklich."
        pdf_segments = segment_pdf_text(
            pdf_text, [PageText(0, {}, pdf_text, 0, len(pdf_text))]
        )
        self.assertIn("§ 2l6", [segment.text for segment in pdf_segments])

        paragraph = ParagraphText(
            "epub-section", 14, {}, "第lS 节\n伦理是自由的理念。"
        )
        epub_segments = segment_paragraph_text([paragraph])
        self.assertEqual(epub_segments[0].text, "第lS 节")
        self.assertEqual(epub_segments[0].spans, (("epub-section", 14, 0, 5),))

        chapter_text = "第一章 物的形式礼拜仪式\n正文从这里开始。"
        chapter_segments = segment_pdf_text(
            chapter_text,
            [PageText(0, {}, chapter_text, 0, len(chapter_text))],
        )
        self.assertEqual(chapter_segments[0].text, "第一章 物的形式礼拜仪式")
        self.assertEqual(chapter_segments[1].text, "正文从这里开始。")

        english_part = "PART ONE\nTHE FORMAL LITURGY OF THE OBJECT."
        english_segments = segment_pdf_text(
            english_part,
            [PageText(0, {}, english_part, 0, len(english_part))],
        )
        self.assertEqual(english_segments[0].text, "PART ONE")

    def test_alignment_model_can_emit_many_sided_links(self) -> None:
        links, _anchors = align_segment_sequences(
            ["A very long source sentence with many words."],
            ["短句。", "另一个短句。"],
            cache_dir=Path(self.directory.name) / "models",
        )
        self.assertEqual((links[0].source_end - links[0].source_start), 1)
        self.assertEqual((links[0].target_end - links[0].target_start), 2)

    def test_single_version_extra_content_is_unmatched_not_low_confidence(self) -> None:
        links, _anchors = align_segment_sequences(
            ["shared sentence."],
            ["shared sentence."] * 5,
            cache_dir=Path(self.directory.name) / "models",
        )
        self.assertIn("unmatched", {link.review_status for link in links})
        self.assertNotIn("rejected", {link.review_status for link in links})

    def test_heading_anchors_pair_body_chapters_and_sections_not_toc(self) -> None:
        source = [
            "Contents\n1 First\ni. One\nii. Two\n2 Second\ni. One\nii. Two",
            "1 First\ni. One\nOpening sentence.",
            "ii. Two\nNext sentence.",
            "2 Second\ni. One\nAnother sentence.",
        ]
        target = [
            "目录\n第一章 第一\n第一节 一\n第二节 二\n第二章 第二\n第一节 一\n第二节 二",
            "第一章 第一\n第一节 一\n开头。",
            "第二节 二\n下一段。",
            "第二章 第二\n第一节 一\n另一段。",
        ]
        anchors = find_heading_anchors(source, target)
        self.assertEqual(
            [(item.source_index, item.target_index) for item in anchors],
            [(1, 1), (2, 2), (3, 3)],
        )

    def test_low_confidence_link_is_stored_but_refused_for_location(self) -> None:
        def unrelated_embeddings(texts, _cache_dir):
            midpoint = len(texts) // 2
            return np.asarray(
                [
                    (1.0, 0.0) if index < midpoint else (-1.0, 0.0)
                    for index, _text in enumerate(texts)
                ],
                dtype=np.float32,
            )

        result = generate_alignment(
            self.db,
            "work-one",
            "pdf-de",
            "pdf-zh",
            embedding_provider=unrelated_embeddings,
        )
        self.assertGreater(result["rejected_link_count"], 0)
        connection = sqlite3.connect(str(self.db))
        try:
            self.assertGreater(
                connection.execute(
                    "SELECT COUNT(*) FROM alignment_links WHERE review_status = 'rejected'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()
        with self.assertRaisesRegex(AlignmentNotFound, "置信度过低"):
            locate_alignment(
                self.db,
                "pdf-de",
                "pdf-zh",
                start_page_index=0,
                end_page_index=0,
                start_offset=0,
                end_offset=len("Der Geist ist wirklich."),
            )

    def test_untranslated_addition_falls_back_to_shared_section_heading(self) -> None:
        connection = sqlite3.connect(str(self.db))
        german = _page("pdf-de", 0, "§ 158\nDie Familie ist sittlich.\n§ 159\nDer folgende Abschnitt.")
        chinese_text = "家庭\n第\n158 节\n家庭是伦理的。\n补充内容没有德文对应。\n第159 节\n下一节正文。"
        chinese = _page("pdf-zh", 0, chinese_text)
        connection.executemany(
            "UPDATE pdf_pages SET payload_json = ? WHERE source_file_id = ?",
            (
                (json.dumps(german, ensure_ascii=False), "pdf-de"),
                (json.dumps(chinese, ensure_ascii=False), "pdf-zh"),
            ),
        )
        connection.commit()
        connection.close()
        generated = generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        connection = sqlite3.connect(str(self.db))
        addition_segment = connection.execute(
            "SELECT s.segment_id FROM text_segments s "
            "WHERE s.segment_set_id = (SELECT target_segment_set_id FROM alignment_runs "
            "WHERE alignment_run_id = ?) AND s.text_raw LIKE '补充内容%'",
            (generated["alignment_run_id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE alignment_links SET review_status = 'rejected' "
            "WHERE alignment_run_id = ? AND alignment_link_id IN "
            "(SELECT alignment_link_id FROM alignment_link_members WHERE segment_id = ?)",
            (generated["alignment_run_id"], addition_segment),
        )
        connection.commit()
        connection.close()

        start = chinese_text.index("补充内容")
        located = locate_alignment(
            self.db,
            "pdf-zh",
            "pdf-de",
            start_page_index=0,
            end_page_index=0,
            start_offset=start,
            end_offset=start + len("补充内容"),
        )

        self.assertEqual(located["page_match_spans"][0]["match_quote"], "§ 158")

    def test_generate_locate_and_reverse_target_discovery(self) -> None:
        result = generate_alignment(
            self.db, "work-one", "pdf-de", "pdf-zh"
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["pivot_segment_count"], 2)
        self.assertEqual(result["target_segment_count"], 2)
        connection = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()

        forward = list_alignment_targets(self.db, "pdf-de")
        reverse = list_alignment_targets(self.db, "pdf-zh")
        self.assertEqual(forward["targets"][0]["source_file_id"], "pdf-zh")
        self.assertEqual(reverse["targets"][0]["source_file_id"], "pdf-de")

        located = locate_alignment(
            self.db,
            "pdf-de",
            "pdf-zh",
            start_page_index=0,
            end_page_index=0,
            start_offset=0,
            end_offset=len("Der Geist ist wirklich."),
        )
        self.assertEqual(located["target_source_file_id"], "pdf-zh")
        self.assertEqual(located["page_match_spans"][0]["match_quote"], "精神是现实的。")
        self.assertEqual(located["bbox_refs"][0]["bbox"], [12, 24, 280, 72])
        self.assertEqual(located["alignment_run_ids"], [result["alignment_run_id"]])
        self.assertIsNone(located["via_source_file_id"])

        with self.assertRaisesRegex(
            InvalidAlignmentRequest, "end_offset 超出页文本范围"
        ):
            locate_alignment(
                self.db,
                "pdf-de",
                "pdf-zh",
                start_page_index=0,
                end_page_index=0,
                start_offset=0,
                end_offset=10_000,
            )

    def test_same_pdf_reparse_gets_its_own_alignment_without_reusing_old_offsets(self) -> None:
        original = generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        new_id = "pdf-zh-mineru"
        new_text = "\n\n精神是现实的。\n\n真理是全体。"
        with closing(sqlite3.connect(str(self.db))) as connection, connection:
            source = json.loads(connection.execute(
                "SELECT payload_json FROM source_files WHERE source_file_id='pdf-zh'"
            ).fetchone()[0])
            source["sha256"] = "a" * 64
            connection.execute(
                "UPDATE source_files SET payload_json=? WHERE source_file_id='pdf-zh'",
                (json.dumps(source),),
            )
            source["source_file_id"] = new_id
            connection.execute(
                "INSERT INTO source_files(source_file_id, source_type, file_name, payload_json) "
                "VALUES (?, 'pdf', ?, ?)", (new_id, source["file_name"], json.dumps(source)),
            )
            connection.execute(
                "INSERT INTO pdf_pages(source_file_id, pdf_page_index, payload_json) VALUES (?, 0, ?)",
                (new_id, json.dumps(_page(new_id, 0, new_text))),
            )
        self.assertEqual(list_alignment_targets(self.db, new_id)["targets"], [])
        add_group_member("work-one", new_id, self.db)
        self.assertEqual(list_alignment_targets(self.db, new_id)["targets"], [])
        generated = generate_alignment(self.db, "work-one", "pdf-de", new_id)
        self.assertNotEqual(generated["alignment_run_id"], original["alignment_run_id"])
        self.assertIn("pdf-de", {t["source_file_id"] for t in list_alignment_targets(self.db, new_id)["targets"]})
        located = locate_alignment(
            self.db, new_id, "pdf-de", start_page_index=0, end_page_index=0,
            start_offset=new_text.index("精神"), end_offset=new_text.index("。") + 1,
        )
        self.assertEqual(located["alignment_run_ids"], [generated["alignment_run_id"]])
        self.assertEqual(located["page_match_spans"][0]["match_quote"], "Der Geist ist wirklich.")
        self.assertEqual(
            list_alignment_targets(self.db, "pdf-zh")["targets"][0]["alignment_run_id"],
            original["alignment_run_id"],
        )

    def test_numbered_note_link_takes_priority_over_conflicting_monotonic_link(self) -> None:
        generated = generate_alignment(
            self.db, "work-one", "pdf-de", "pdf-zh"
        )
        connection = sqlite3.connect(str(self.db))
        connection.row_factory = sqlite3.Row
        source_segments = connection.execute(
            "SELECT segment_id FROM text_segments "
            "WHERE segment_set_id = (SELECT pivot_segment_set_id FROM alignment_runs "
            "WHERE alignment_run_id = ?) ORDER BY order_index",
            (generated["alignment_run_id"],),
        ).fetchall()
        target_segments = connection.execute(
            "SELECT segment_id FROM text_segments "
            "WHERE segment_set_id = (SELECT target_segment_set_id FROM alignment_runs "
            "WHERE alignment_run_id = ?) ORDER BY order_index",
            (generated["alignment_run_id"],),
        ).fetchall()
        conflicting_link = connection.execute(
            "SELECT l.alignment_link_id FROM alignment_links l "
            "JOIN alignment_link_members m "
            "ON m.alignment_link_id = l.alignment_link_id "
            "WHERE l.alignment_run_id = ? AND m.segment_id = ?",
            (generated["alignment_run_id"], source_segments[0]["segment_id"]),
        ).fetchone()[0]
        connection.execute(
            "UPDATE alignment_links SET review_status = 'rejected' "
            "WHERE alignment_link_id = ?",
            (conflicting_link,),
        )
        connection.execute(
            "INSERT INTO alignment_links(alignment_link_id, alignment_run_id, "
            "order_index, cost, review_status) VALUES "
            "('note-link', ?, 99, 0.1, 'note_automatic')",
            (generated["alignment_run_id"],),
        )
        connection.executemany(
            "INSERT INTO alignment_link_members(alignment_link_id, side, "
            "segment_id, member_order) VALUES ('note-link', ?, ?, 0)",
            (
                ("pivot", source_segments[0]["segment_id"]),
                ("target", target_segments[1]["segment_id"]),
            ),
        )
        connection.commit()
        connection.close()

        forward = locate_alignment(
            self.db,
            "pdf-de",
            "pdf-zh",
            start_page_index=0,
            end_page_index=0,
            start_offset=0,
            end_offset=len("Der Geist ist wirklich."),
        )
        reverse = locate_alignment(
            self.db,
            "pdf-zh",
            "pdf-de",
            start_page_index=0,
            end_page_index=0,
            start_offset=len("精神是现实的。"),
            end_offset=len("精神是现实的。真理是全体。"),
        )

        self.assertEqual(forward["page_match_spans"][0]["match_quote"], "真理是全体。")
        self.assertEqual(reverse["page_match_spans"][0]["match_quote"], "Der Geist ist wirklich.")

    def test_unchanged_completed_pair_is_reused_without_recomputing(self) -> None:
        first = generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        with mock.patch(
            "src.me_finder.text_alignment.align_segment_sequences",
            side_effect=AssertionError("cached pair should not be recomputed"),
        ):
            second = generate_alignment(
                self.db, "work-one", "pdf-de", "pdf-zh"
            )

        self.assertTrue(second["reused"])
        self.assertEqual(second["alignment_run_id"], first["alignment_run_id"])
        connection = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM alignment_runs").fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_old_alignment_requires_regeneration_and_is_not_reused(self) -> None:
        first = generate_alignment(self.db, "work-one", "pdf-de", "epub-en")
        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute(
                "UPDATE alignment_runs SET algorithm_version = 'obsolete'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(AlignmentNotFound, "重新生成对照"):
            locate_alignment(
                self.db, "pdf-de", "epub-en",
                start_page_index=0, end_page_index=0, start_offset=0, end_offset=3,
            )

        second = generate_alignment(self.db, "work-one", "pdf-de", "epub-en")
        self.assertNotEqual(second["alignment_run_id"], first["alignment_run_id"])
        located = locate_alignment(
            self.db, "pdf-de", "epub-en",
            start_page_index=0, end_page_index=0, start_offset=0, end_offset=3,
        )
        self.assertEqual(located["target_item_type"], "word_paragraph")

    def test_version16_alignment_remains_readable_but_regenerates_when_requested(self) -> None:
        first = generate_alignment(self.db, "work-one", "pdf-de", "epub-en")
        with closing(sqlite3.connect(str(self.db))) as connection:
            connection.execute("UPDATE alignment_runs SET algorithm_version = '16'")
            connection.commit()

        located = locate_alignment(
            self.db, "pdf-de", "epub-en",
            start_page_index=0, end_page_index=0, start_offset=0, end_offset=3,
        )
        self.assertEqual(located["alignment_run_id"], first["alignment_run_id"])
        self.assertEqual(located["page_match_spans"][0]["match_quote"], "Spirit is actual.")

        regenerated = generate_alignment(self.db, "work-one", "pdf-de", "epub-en")
        self.assertFalse(regenerated["reused"])
        self.assertNotEqual(regenerated["alignment_run_id"], first["alignment_run_id"])
        self.assertEqual(regenerated["algorithm_version"], ALIGNMENT_ALGORITHM_VERSION)

    def test_version16_recipe_is_restored_using_current_algorithm(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "epub-en")
        with closing(sqlite3.connect(str(self.db))) as connection:
            connection.execute("UPDATE alignment_runs SET algorithm_version = '16'")
            connection.commit()
        snapshot = read_alignment_recipe_snapshot(self.db)

        self.assertEqual(replace_alignment_recipe_snapshot(snapshot, self.db), 1)
        restored = read_alignment_recipe_snapshot(self.db)["alignment_pairs"]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["algorithm_version"], ALIGNMENT_ALGORITHM_VERSION)
        located = locate_alignment(
            self.db, "pdf-de", "epub-en",
            start_page_index=0, end_page_index=0, start_offset=0, end_offset=3,
        )
        self.assertEqual(located["page_match_spans"][0]["match_quote"], "Spirit is actual.")

    def test_pdf_and_epub_alignment_locates_both_directions(self) -> None:
        result = generate_alignment(
            self.db, "work-one", "pdf-de", "epub-en"
        )
        self.assertEqual(result["target_segment_count"], 2)

        located_epub = locate_alignment(
            self.db,
            "pdf-de",
            "epub-en",
            start_page_index=0,
            end_page_index=0,
            start_offset=0,
            end_offset=len("Der Geist ist wirklich."),
        )
        self.assertEqual(located_epub["target_item_type"], "word_paragraph")
        self.assertEqual(located_epub["target_index"], 0)
        self.assertEqual(located_epub["bbox_refs"], [])
        self.assertEqual(
            located_epub["page_match_spans"][0]["paragraph_id"], "epub-en-p0"
        )
        self.assertEqual(
            located_epub["page_match_spans"][0]["match_quote"],
            "Spirit is actual.",
        )

        located_pdf = locate_alignment(
            self.db,
            "epub-en",
            "pdf-de",
            start_page_index=1,
            end_page_index=1,
            start_offset=0,
            end_offset=len("Truth is the whole."),
        )
        self.assertEqual(located_pdf["target_item_type"], "pdf_page")
        self.assertEqual(
            located_pdf["page_match_spans"][0]["match_quote"],
            "Die Wahrheit ist das Ganze.",
        )
        self.assertEqual(
            list_alignment_targets(self.db, "epub-en")["targets"][0][
                "source_file_id"
            ],
            "pdf-de",
        )

    def test_non_pivot_versions_locate_through_the_group_pivot(self) -> None:
        chinese_run = generate_alignment(
            self.db, "work-one", "pdf-de", "pdf-zh"
        )
        english_run = generate_alignment(
            self.db, "work-one", "pdf-de", "epub-en"
        )

        target_by_id = {
            target["source_file_id"]: target
            for target in list_alignment_targets(self.db, "pdf-zh")["targets"]
        }
        self.assertEqual(
            target_by_id["epub-en"]["alignment_run_ids"],
            [chinese_run["alignment_run_id"], english_run["alignment_run_id"]],
        )
        self.assertEqual(
            target_by_id["epub-en"]["via_source_file_id"], "pdf-de"
        )

        located = locate_alignment(
            self.db,
            "pdf-zh",
            "epub-en",
            start_page_index=0,
            end_page_index=0,
            start_offset=0,
            end_offset=len("精神是现实的。"),
        )

        self.assertEqual(located["target_item_type"], "word_paragraph")
        self.assertEqual(located["target_index"], 0)
        self.assertEqual(
            located["page_match_spans"][0]["match_quote"], "Spirit is actual."
        )
        self.assertEqual(
            located["alignment_run_ids"],
            [chinese_run["alignment_run_id"], english_run["alignment_run_id"]],
        )
        self.assertEqual(located["alignment_run_id"], english_run["alignment_run_id"])
        self.assertEqual(located["via_source_file_id"], "pdf-de")

    def test_non_base_versions_can_generate_and_prefer_a_direct_pair(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        generate_alignment(self.db, "work-one", "pdf-de", "epub-en")
        direct = generate_alignment(
            self.db, "work-one", "pdf-zh", "epub-en"
        )

        english_target = {
            target["source_file_id"]: target
            for target in list_alignment_targets(self.db, "pdf-zh")["targets"]
        }["epub-en"]
        self.assertEqual(
            english_target["alignment_run_ids"], [direct["alignment_run_id"]]
        )
        self.assertIsNone(english_target["via_source_file_id"])
        self.assertEqual(english_target["language_code"], "en")
        self.assertEqual(english_target["source_format"], "epub")

        located = locate_alignment(
            self.db,
            "pdf-zh",
            "epub-en",
            start_page_index=0,
            end_page_index=0,
            start_offset=0,
            end_offset=len("精神是现实的。"),
        )
        self.assertEqual(located["alignment_run_ids"], [direct["alignment_run_id"]])
        self.assertEqual(located["page_match_spans"][0]["match_quote"], "Spirit is actual.")

    def test_pivot_route_refuses_rejected_or_unmatched_links(self) -> None:
        chinese_run = generate_alignment(
            self.db, "work-one", "pdf-de", "pdf-zh"
        )
        english_run = generate_alignment(
            self.db, "work-one", "pdf-de", "epub-en"
        )
        connection = sqlite3.connect(str(self.db))
        chinese_segment = connection.execute(
            "SELECT segment_id FROM text_segments WHERE segment_set_id = "
            "(SELECT target_segment_set_id FROM alignment_runs "
            "WHERE alignment_run_id = ?) ORDER BY order_index LIMIT 1",
            (chinese_run["alignment_run_id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE alignment_links SET review_status = 'rejected' "
            "WHERE alignment_run_id = ? AND alignment_link_id IN "
            "(SELECT alignment_link_id FROM alignment_link_members "
            "WHERE side = 'target' AND segment_id = ?)",
            (chinese_run["alignment_run_id"], chinese_segment),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(AlignmentNotFound, "置信度过低"):
            locate_alignment(
                self.db,
                "pdf-zh",
                "epub-en",
                start_page_index=0,
                end_page_index=0,
                start_offset=0,
                end_offset=len("精神是现实的。"),
            )

        connection = sqlite3.connect(str(self.db))
        connection.execute(
            "UPDATE alignment_links SET review_status = 'unmatched' "
            "WHERE alignment_run_id = ? AND alignment_link_id IN "
            "(SELECT alignment_link_id FROM alignment_link_members "
            "WHERE side = 'target' AND segment_id = ?)",
            (chinese_run["alignment_run_id"], chinese_segment),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(AlignmentNotFound, "没有可靠的对应段落"):
            locate_alignment(
                self.db,
                "pdf-zh",
                "epub-en",
                start_page_index=0,
                end_page_index=0,
                start_offset=0,
                end_offset=len("精神是现实的。"),
            )

        connection = sqlite3.connect(str(self.db))
        connection.execute(
            "UPDATE alignment_links SET review_status = 'automatic' "
            "WHERE alignment_run_id = ?",
            (chinese_run["alignment_run_id"],),
        )
        pivot_segment = connection.execute(
            "SELECT segment_id FROM text_segments WHERE segment_set_id = "
            "(SELECT pivot_segment_set_id FROM alignment_runs "
            "WHERE alignment_run_id = ?) ORDER BY order_index LIMIT 1",
            (english_run["alignment_run_id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE alignment_links SET review_status = 'rejected' "
            "WHERE alignment_run_id = ? AND alignment_link_id IN "
            "(SELECT alignment_link_id FROM alignment_link_members "
            "WHERE side = 'pivot' AND segment_id = ?)",
            (english_run["alignment_run_id"], pivot_segment),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(AlignmentNotFound, "置信度过低"):
            locate_alignment(
                self.db,
                "pdf-zh",
                "epub-en",
                start_page_index=0,
                end_page_index=0,
                start_offset=0,
                end_offset=len("精神是现实的。"),
            )

    def test_pivot_route_requires_both_completed_pairs(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")

        target_ids = {
            target["source_file_id"]
            for target in list_alignment_targets(self.db, "pdf-zh")["targets"]
        }
        self.assertNotIn("epub-en", target_ids)
        with self.assertRaisesRegex(AlignmentNotFound, "还没有可用的自动对齐"):
            locate_alignment(
                self.db,
                "pdf-zh",
                "epub-en",
                start_page_index=0,
                end_page_index=0,
                start_offset=0,
                end_offset=len("精神是现实的。"),
            )

    def test_completed_pair_is_recreated_across_full_index_rebuild(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        source_rows = []
        page_rows = []
        connection = sqlite3.connect(str(self.db))
        connection.row_factory = sqlite3.Row
        for row in connection.execute("SELECT payload_json FROM source_files"):
            source_rows.append(json.loads(row["payload_json"]))
        for row in connection.execute("SELECT payload_json FROM pdf_pages"):
            page_rows.append(json.loads(row["payload_json"]))
        connection.close()

        build_database(
            {
                "metadata": {},
                "source_files": source_rows,
                "volumes": [],
                "works": [],
                "toc_entries": [],
                "paragraphs": [],
                "page_anchors": [],
                "pdf_pages": page_rows,
                "pdf_page_mappings": [],
            },
            self.db,
        )
        snapshot = read_alignment_recipe_snapshot(self.db)
        self.assertEqual(len(snapshot["alignment_pairs"]), 1)
        self.assertEqual(
            list_alignment_targets(self.db, "pdf-de")["targets"][0]["source_file_id"],
            "pdf-zh",
        )

    def test_target_reparse_invalidates_derived_segments_and_links(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        replacement_page = _page("pdf-zh", 0, "精神是现实。真理就是整体。")
        replace_source_in_database(
            {
                "source_files": [
                    {
                        "source_file_id": "pdf-zh",
                        "source_type": "pdf",
                        "file_name": "精神现象学.pdf",
                        "title": "精神现象学",
                    }
                ],
                "volumes": [],
                "works": [],
                "toc_entries": [],
                "paragraphs": [],
                "page_anchors": [],
                "pdf_pages": [replacement_page],
                "pdf_page_mappings": [],
                "pdf_import_runs": [],
                "audit_issues": [],
            },
            self.db,
            backup_existing=False,
        )
        self.assertEqual(
            read_alignment_recipe_snapshot(self.db)["alignment_pairs"], []
        )
        self.assertEqual(
            list_alignment_targets(self.db, "pdf-de")["targets"], []
        )

    def test_storage_optimization_copies_segment_and_alignment_tables(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        self.assertTrue(optimize_database_storage(self.db))
        self.assertEqual(
            list_alignment_targets(self.db, "pdf-de")["targets"][0]["source_file_id"],
            "pdf-zh",
        )

    def test_changing_the_group_pivot_invalidates_completed_links(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        set_document_group_base("work-one", "pdf-zh", self.db)
        self.assertEqual(
            read_alignment_recipe_snapshot(self.db)["alignment_pairs"], []
        )

    def test_backup_v3_carries_a_small_regenerable_alignment_recipe(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        runtime_root = Path(self.directory.name) / "runtime"
        runtime_root.mkdir()
        archive = create_backup(runtime_root, index_path=self.db)
        restored = restore_backup(
            Path(self.directory.name) / "restored-runtime", archive
        )
        pairs = restored["alignment_snapshot"]["alignment_pairs"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["pivot_source_file_id"], "pdf-de")
        self.assertEqual(pairs[0]["target_source_file_id"], "pdf-zh")

    def test_embedding_computation_does_not_hold_the_database_write_lock(self) -> None:
        embedding_started = threading.Event()
        release_embedding = threading.Event()
        failures: list[BaseException] = []

        def blocking_embeddings(texts, cache_dir):
            embedding_started.set()
            if not release_embedding.wait(timeout=5):
                raise RuntimeError("embedding test timed out")
            return _fake_embeddings(texts, cache_dir)

        def run_alignment() -> None:
            try:
                generate_alignment(
                    self.db,
                    "work-one",
                    "pdf-de",
                    "pdf-zh",
                    embedding_provider=blocking_embeddings,
                )
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=run_alignment, daemon=True)
        worker.start()
        self.assertTrue(embedding_started.wait(timeout=5))
        competing = sqlite3.connect(str(self.db), timeout=0.2)
        try:
            competing.execute("BEGIN IMMEDIATE")
            competing.rollback()
        finally:
            competing.close()
            release_embedding.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])


class SemanticEmbeddingCacheTests(unittest.TestCase):
    def test_document_vectors_are_reused_across_alignment_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            calls: list[list[str]] = []

            def fake_embed(texts, _cache_dir):
                calls.append(list(texts))
                return np.arange(len(texts) * 3, dtype=np.float32).reshape(-1, 3)

            sequences = [["pivot one", "pivot two"], ["target one"]]
            with mock.patch(
                "src.me_finder.semantic_alignment.embed_texts",
                side_effect=fake_embed,
            ):
                first = cache_text_sequences(sequences, cache_dir)
                second = cache_text_sequences(sequences, cache_dir)

        self.assertEqual(calls, [["pivot one", "pivot two", "target one"]])
        np.testing.assert_array_equal(first[0], second[0])
        np.testing.assert_array_equal(first[1], second[1])

    def test_resegmentation_reuses_unchanged_segment_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            calls: list[list[str]] = []

            def fake_embed(texts, _cache_dir):
                calls.append(list(texts))
                return np.ones((len(texts), 3), dtype=np.float32)

            previous = ["unchanged", "old combined segment"]
            current = ["unchanged", "new split segment"]
            with mock.patch(
                "src.me_finder.semantic_alignment.embed_texts",
                side_effect=fake_embed,
            ):
                cache_text_sequences([previous], cache_dir)
                cache_text_sequences(
                    [current], cache_dir, reusable_sequences=[previous]
                )

        self.assertEqual(calls, [previous, ["new split segment"]])


class ReadonlyConnectionLockTests(unittest.TestCase):
    def test_read_connection_waits_for_a_transient_exclusive_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "locked.sqlite3"
            writer = sqlite3.connect(str(database), check_same_thread=False)
            writer.execute("CREATE TABLE sample(value INTEGER)")
            writer.commit()
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("INSERT INTO sample VALUES (1)")
            opened = threading.Event()
            failures: list[BaseException] = []

            def open_reader() -> None:
                try:
                    reader = open_readonly_index(database)
                    reader.close()
                    opened.set()
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=open_reader, daemon=True)
            thread.start()
            self.assertFalse(opened.wait(timeout=0.1))
            writer.rollback()
            writer.close()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(opened.is_set())
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
