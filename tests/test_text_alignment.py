from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.me_finder.backup_service import create_backup, restore_backup
from src.me_finder.database import (
    build_database,
    optimize_database_storage,
    replace_source_in_database,
)
from src.me_finder.document_groups import set_document_group_base
from src.me_finder.persistence.index_schema import SCHEMA
from src.me_finder.text_alignment import (
    InvalidAlignmentRequest,
    align_segment_sequences,
    generate_alignment,
    list_alignment_targets,
    locate_alignment,
    read_alignment_recipe_snapshot,
    segment_pdf_text,
    PageText,
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


class TextAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
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
            (("pdf-de", "德文", 0), ("pdf-zh", "贺麟译本", 1)),
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
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_segmenter_handles_latin_sentences_and_cross_page_spans(self) -> None:
        full_text = "Ein Satz endet.\n跨页句子结束。"
        pages = [
            PageText(0, {}, "Ein Satz endet.", 0, 15),
            PageText(1, {}, "跨页句子结束。", 16, len(full_text)),
        ]
        segments = segment_pdf_text(full_text, pages)
        self.assertEqual([item.text for item in segments], ["Ein Satz endet.", "跨页句子结束。"])
        self.assertEqual(segments[1].spans, ((1, 0, 7),))

    def test_alignment_model_can_emit_many_sided_links(self) -> None:
        links = align_segment_sequences(
            ["A very long source sentence with many words."],
            ["短句。", "另一个短句。"],
        )
        self.assertEqual((links[0][1] - links[0][0]), 1)
        self.assertEqual((links[0][3] - links[0][2]), 2)

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


if __name__ == "__main__":
    unittest.main()
