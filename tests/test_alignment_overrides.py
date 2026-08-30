"""Human-confirmed alignment corrections: propose, confirm, revoke, read path.

These exercise the write-back that lets an agent propose a correction, the user
confirm it, and both dual-column reading and the MCP query then prefer it over
the automatic mapping — without ever letting an unconfirmed proposal change what
reads return.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.persistence.index_schema import SCHEMA
from src.me_finder.text_alignment import (
    AlignmentNotFound,
    InvalidAlignmentRequest,
    confirm_override,
    create_override_proposal,
    generate_alignment,
    list_overrides,
    locate_alignment,
    resolve_override_context,
    revoke_override,
)
from tests.test_text_alignment import _fake_embedding_sequences, _page


_FIRST_GERMAN_SENTENCE = "Der Geist ist wirklich."


class AlignmentOverrideTests(unittest.TestCase):
    """A two-version (German pivot, Chinese target) group with one aligned pair."""

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
        self.embedding_patch.stop()

    def _locate_first_sentence(self) -> dict[str, object]:
        return locate_alignment(
            self.db,
            "pdf-de",
            "pdf-zh",
            start_page_index=0,
            end_page_index=0,
            start_offset=0,
            end_offset=len(_FIRST_GERMAN_SENTENCE),
        )

    def _segment_at(self, source_id: str, order_index: int) -> str:
        connection = sqlite3.connect(str(self.db))
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT ts.segment_id FROM text_segments ts "
                "JOIN segment_sets ss ON ss.segment_set_id = ts.segment_set_id "
                "WHERE ss.source_file_id = ? AND ts.order_index = ? "
                "ORDER BY ss.created_at DESC LIMIT 1",
                (source_id, order_index),
            ).fetchone()
            self.assertIsNotNone(row)
            return str(row["segment_id"])
        finally:
            connection.close()

    def _propose_wrong_target(self) -> dict[str, object]:
        auto = self._locate_first_sentence()
        wrong_target = self._segment_at("pdf-zh", 1)  # 真理是全体。
        return create_override_proposal(
            self.db,
            "pdf-de",
            "pdf-zh",
            auto["source_segment_ids"],
            [wrong_target],
            evidence={"reason": "unit-test", "candidate_anchor_distance": 1},
        )

    def test_confirmed_override_supersedes_automatic_mapping(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")

        auto = self._locate_first_sentence()
        self.assertEqual(auto["alignment_source"], "automatic")
        self.assertIsNone(auto["manual_override_id"])
        self.assertEqual(auto["page_match_spans"][0]["match_quote"], "精神是现实的。")
        self.assertTrue(auto["source_segment_ids"])

        proposal = self._propose_wrong_target()
        self.assertEqual(proposal["status"], "pending")
        self.assertTrue(proposal["confirmation_token"])
        self.assertEqual(
            proposal["source_segments"][0]["text"], _FIRST_GERMAN_SENTENCE
        )
        self.assertEqual(proposal["target_segments"][0]["text"], "真理是全体。")

        # A pending proposal must not change what reads return.
        still_auto = self._locate_first_sentence()
        self.assertEqual(still_auto["alignment_source"], "automatic")
        self.assertEqual(
            still_auto["page_match_spans"][0]["match_quote"], "精神是现实的。"
        )

        confirmed = confirm_override(
            self.db, proposal["override_id"], proposal["confirmation_token"]
        )
        self.assertEqual(confirmed["status"], "confirmed")

        after = self._locate_first_sentence()
        self.assertEqual(after["alignment_source"], "manual_review")
        self.assertEqual(after["manual_override_id"], proposal["override_id"])
        self.assertEqual(after["page_match_spans"][0]["match_quote"], "真理是全体。")

        # Foreign-key integrity is preserved by the new table.
        connection = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
        finally:
            connection.close()

    def test_revoke_restores_automatic_mapping(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        proposal = self._propose_wrong_target()
        confirm_override(
            self.db, proposal["override_id"], proposal["confirmation_token"]
        )
        self.assertEqual(
            self._locate_first_sentence()["alignment_source"], "manual_review"
        )

        revoked = revoke_override(self.db, proposal["override_id"])
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["previous_status"], "confirmed")

        restored = self._locate_first_sentence()
        self.assertEqual(restored["alignment_source"], "automatic")
        self.assertEqual(
            restored["page_match_spans"][0]["match_quote"], "精神是现实的。"
        )

    def test_confirm_requires_matching_token(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        proposal = self._propose_wrong_target()
        with self.assertRaisesRegex(InvalidAlignmentRequest, "确认令牌"):
            confirm_override(self.db, proposal["override_id"], "deadbeef")
        # Rejected confirmation leaves reads on the automatic mapping.
        self.assertEqual(
            self._locate_first_sentence()["alignment_source"], "automatic"
        )

    def test_confirm_rejects_unknown_override(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        with self.assertRaises(AlignmentNotFound):
            confirm_override(self.db, "alignment-override-missing", "token")

    def test_proposal_rejects_foreign_segments(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        auto = self._locate_first_sentence()
        with self.assertRaisesRegex(InvalidAlignmentRequest, "目标 Segment"):
            create_override_proposal(
                self.db,
                "pdf-de",
                "pdf-zh",
                auto["source_segment_ids"],
                ["segment-does-not-exist"],
            )

    def test_resolve_context_requires_existing_alignment(self) -> None:
        # No generate_alignment() call: there is no route to correct.
        with self.assertRaises(AlignmentNotFound):
            resolve_override_context(
                self.db, "pdf-de", "pdf-zh", ["segment-a"], ["segment-b"]
            )

    def test_new_proposal_replaces_pending_and_list_reports_status(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        first = self._propose_wrong_target()
        second = self._propose_wrong_target()
        self.assertNotEqual(first["override_id"], second["override_id"])

        pending = list_overrides(self.db, source_file_id="pdf-de", status="pending")
        self.assertEqual(pending["total"], 1)
        self.assertEqual(pending["overrides"][0]["override_id"], second["override_id"])
        self.assertEqual(pending["overrides"][0]["evidence"]["reason"], "unit-test")

        # The superseded pending proposal can no longer be confirmed.
        with self.assertRaises(AlignmentNotFound):
            confirm_override(
                self.db, first["override_id"], first["confirmation_token"]
            )

    def test_stale_target_segment_set_falls_back_to_automatic(self) -> None:
        generate_alignment(self.db, "work-one", "pdf-de", "pdf-zh")
        proposal = self._propose_wrong_target()
        confirm_override(
            self.db, proposal["override_id"], proposal["confirmation_token"]
        )
        # Simulate a later re-segmentation of the target: the confirmed row now
        # points at a segment set the current alignment no longer uses.
        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute(
                "UPDATE alignment_manual_overrides "
                "SET target_segment_set_id = 'segment-set-stale' WHERE override_id = ?",
                (proposal["override_id"],),
            )
            connection.commit()
        finally:
            connection.close()

        located = self._locate_first_sentence()
        self.assertEqual(located["alignment_source"], "automatic")
        self.assertEqual(
            located["page_match_spans"][0]["match_quote"], "精神是现实的。"
        )
