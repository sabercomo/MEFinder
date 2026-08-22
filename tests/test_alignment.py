"""Alignment data layer (D1).

Covers the confirmed schema + the four late corrections: review_status and
is_stale are orthogonal; rebuild keeps a segment whose paragraph anchor drifted
(marking the group stale) and only prunes when the source_file itself is gone;
single-sided groups are legal; and active segments may not overlap across
alignment_groups within one (document_group, source_file).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from me_finder import alignment as al
from me_finder import document_groups as dg
from me_finder.database import DATABASE_SCHEMA_VERSION, build_database


def _para(source_id: str, index: int, text: str) -> dict:
    return {
        "paragraph_id": f"{source_id}-P{index:06d}",
        "source_file_id": source_id,
        "source_type": "pdf",
        "paragraph_index": index,
        "eligible_for_search": 1,
        "text_raw": text,
        "normalized_text": text,
        "compact_text": text.replace(" ", ""),
        "plain_text": text,
    }


def _index(sources: dict) -> dict:
    source_files = [
        {"source_file_id": sid, "source_type": "pdf", "file_name": f"{sid}.pdf", "title": sid}
        for sid in sources
    ]
    paragraphs = []
    for source_id, texts in sources.items():
        for i, text in enumerate(texts):
            paragraphs.append(_para(source_id, i, text))
    return {
        "metadata": {},
        "source_files": source_files,
        "volumes": [],
        "works": [],
        "paragraphs": paragraphs,
        "page_anchors": [],
        "pdf_pages": [],
        "pdf_page_mappings": [],
    }


def _pid(source_id: str, index: int) -> str:
    return f"{source_id}-P{index:06d}"


class AlignmentDataLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "index.sqlite3"
        self._sources = {
            "zh": ["承认 是 精神 的 运动", "第二段 中文", "第三段 中文", "第四段 中文"],
            "en": ["recognition moves spirit", "second en", "third en", "fourth en"],
            "de": ["Anerkennung", "zweiter de", "dritter de", "vierter de"],
        }
        build_database(_index(self._sources), self.db, backup_existing=False)
        self.gid = dg.create_document_group("精神现象学", self.db)["document_group_id"]
        for sid in ("zh", "en", "de"):
            dg.add_group_member(self.gid, sid, self.db)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _rebuild(self, sources: dict) -> None:
        build_database(_index(sources), self.db, backup_existing=True)

    def _groups(self):
        return al.list_alignment_groups(self.gid, self.db)

    # ── create / list ──
    def test_migration_and_one_to_one(self) -> None:
        result = al.create_alignment_group(
            self.gid,
            [
                {"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)},
                {"source_file_id": "en", "start_paragraph_id": _pid("en", 0)},
            ],
            self.db,
        )
        self.assertEqual(result["segment_count"], 2)
        groups = self._groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["review_status"], "proposed")
        self.assertFalse(groups[0]["is_stale"])
        self.assertEqual(len(groups[0]["segments"]), 2)
        for seg in groups[0]["segments"]:
            self.assertTrue(seg["text_fingerprint"])
        connection = sqlite3.connect(str(self.db))
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_one_to_many_and_segment_order(self) -> None:
        al.create_alignment_group(
            self.gid,
            [
                {"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)},
                {"source_file_id": "en", "start_paragraph_id": _pid("en", 0)},
                {"source_file_id": "en", "start_paragraph_id": _pid("en", 2),
                 "end_paragraph_id": _pid("en", 3)},
            ],
            self.db,
        )
        segs = [s for s in self._groups()[0]["segments"] if s["source_file_id"] == "en"]
        self.assertEqual([s["segment_order"] for s in segs], [0, 1])
        merged = next(s for s in segs if s["segment_order"] == 1)
        self.assertEqual(merged["start_paragraph_index"], 2)
        self.assertEqual(merged["end_paragraph_index"], 3)

    def test_single_sided_group_is_legal(self) -> None:
        # A translation omitting a passage: only zh has a segment. Legal, fresh.
        al.create_alignment_group(
            self.gid,
            [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 1)}],
            self.db,
        )
        groups = self._groups()
        self.assertEqual(len(groups[0]["segments"]), 1)
        self.assertFalse(groups[0]["is_stale"])

    def test_add_third_translation_segment(self) -> None:
        gid = al.create_alignment_group(
            self.gid,
            [
                {"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)},
                {"source_file_id": "en", "start_paragraph_id": _pid("en", 0)},
            ],
            self.db,
        )["alignment_group_id"]
        al.add_alignment_segment(gid, "de", _pid("de", 0), None, self.db)
        sources = {s["source_file_id"] for s in self._groups()[0]["segments"]}
        self.assertEqual(sources, {"zh", "en", "de"})

    # ── validation ──
    def test_membership_required(self) -> None:
        dg2 = dg.create_document_group("其他", self.db)["document_group_id"]
        with self.assertRaises(ValueError):
            al.create_alignment_group(
                dg2, [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)}], self.db
            )

    def test_paragraph_must_exist_in_source(self) -> None:
        with self.assertRaises(ValueError):
            al.create_alignment_group(
                self.gid, [{"source_file_id": "zh", "start_paragraph_id": _pid("en", 0)}], self.db
            )
        with self.assertRaises(ValueError):
            al.create_alignment_group(
                self.gid,
                [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 2),
                  "end_paragraph_id": _pid("zh", 0)}],  # start > end
                self.db,
            )

    def test_overlap_within_group_rejected(self) -> None:
        with self.assertRaises(ValueError):
            al.create_alignment_group(
                self.gid,
                [
                    {"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0),
                     "end_paragraph_id": _pid("zh", 2)},
                    {"source_file_id": "zh", "start_paragraph_id": _pid("zh", 1),
                     "end_paragraph_id": _pid("zh", 3)},
                ],
                self.db,
            )

    def test_cross_group_overlap_rejected_unless_rejected_status(self) -> None:
        gid1 = al.create_alignment_group(
            self.gid,
            [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0),
              "end_paragraph_id": _pid("zh", 2)}],
            self.db,
        )["alignment_group_id"]
        # Overlaps group1's zh P0–P2 while active → rejected.
        with self.assertRaises(ValueError):
            al.create_alignment_group(
                self.gid,
                [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 1),
                  "end_paragraph_id": _pid("zh", 3)}],
                self.db,
            )
        # Once group1 is rejected it no longer blocks the overlap.
        al.set_alignment_review_status(gid1, "rejected", self.db)
        al.create_alignment_group(
            self.gid,
            [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 1),
              "end_paragraph_id": _pid("zh", 3)}],
            self.db,
        )
        self.assertEqual(len(self._groups()), 2)

    def test_review_status_and_stale_are_orthogonal(self) -> None:
        gid = al.create_alignment_group(
            self.gid, [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)}], self.db
        )["alignment_group_id"]
        al.set_alignment_review_status(gid, "confirmed", self.db)
        # Drift the confirmed alignment via rebuild; confirmed must survive + go stale.
        changed = dict(self._sources)
        changed["zh"] = ["改动 的 中文", "第二段 中文", "第三段 中文", "第四段 中文"]
        self._rebuild(changed)
        group = self._groups()[0]
        self.assertEqual(group["review_status"], "confirmed")
        self.assertTrue(group["is_stale"])

    # ── delete / remove ──
    def test_delete_group_and_remove_segment_keep_sources(self) -> None:
        gid = al.create_alignment_group(
            self.gid,
            [
                {"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)},
                {"source_file_id": "en", "start_paragraph_id": _pid("en", 0)},
            ],
            self.db,
        )["alignment_group_id"]
        seg_id = self._groups()[0]["segments"][0]["alignment_segment_id"]
        al.remove_alignment_segment(seg_id, self.db)
        self.assertEqual(len(self._groups()[0]["segments"]), 1)
        al.delete_alignment_group(gid, self.db)
        self.assertEqual(self._groups(), [])
        connection = sqlite3.connect(str(self.db))
        try:
            sources = connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
            paras = connection.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(sources, 3)
        self.assertGreater(paras, 0)

    def test_prune_for_source_marks_stale_and_drops_empty(self) -> None:
        multi = al.create_alignment_group(
            self.gid,
            [
                {"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)},
                {"source_file_id": "en", "start_paragraph_id": _pid("en", 0)},
            ],
            self.db,
        )["alignment_group_id"]
        only_zh = al.create_alignment_group(
            self.gid,
            [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 2)}],
            self.db,
        )["alignment_group_id"]
        result = al.prune_alignment_for_source(self.gid, "zh", self.db)
        self.assertEqual(result["stale_groups"], 1)
        self.assertEqual(result["deleted_groups"], 1)
        by_id = {g["alignment_group_id"]: g for g in self._groups()}
        self.assertNotIn(only_zh, by_id)  # 0 segments left → dropped
        self.assertTrue(by_id[multi]["is_stale"])  # en segment remains → kept, stale
        self.assertEqual(
            {s["source_file_id"] for s in by_id[multi]["segments"]}, {"en"}
        )

    # ── rebuild preservation ──
    def test_rebuild_fresh_when_text_unchanged(self) -> None:
        al.create_alignment_group(
            self.gid,
            [
                {"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)},
                {"source_file_id": "en", "start_paragraph_id": _pid("en", 0)},
            ],
            self.db,
        )
        self._rebuild(self._sources)  # identical text
        group = self._groups()[0]
        self.assertEqual(len(group["segments"]), 2)
        self.assertFalse(group["is_stale"])

    def test_rebuild_keeps_segment_when_source_exists_but_anchor_drifts(self) -> None:
        al.create_alignment_group(
            self.gid,
            [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)},
             {"source_file_id": "en", "start_paragraph_id": _pid("en", 0)}],
            self.db,
        )
        # zh reparsed with an extra leading paragraph: same source, anchors shifted.
        changed = dict(self._sources)
        changed["zh"] = ["新增 序 段"] + list(self._sources["zh"])
        self._rebuild(changed)
        group = self._groups()[0]
        self.assertEqual(len(group["segments"]), 2)  # nothing dropped
        self.assertTrue(group["is_stale"])

    def test_rebuild_prunes_only_when_source_file_gone(self) -> None:
        multi = al.create_alignment_group(
            self.gid,
            [{"source_file_id": "zh", "start_paragraph_id": _pid("zh", 0)},
             {"source_file_id": "en", "start_paragraph_id": _pid("en", 0)}],
            self.db,
        )["alignment_group_id"]
        only_de = al.create_alignment_group(
            self.gid,
            [{"source_file_id": "de", "start_paragraph_id": _pid("de", 0)}],
            self.db,
        )["alignment_group_id"]
        # Rebuild without "de": its source_file is gone.
        remaining = {k: v for k, v in self._sources.items() if k != "de"}
        self._rebuild(remaining)
        by_id = {g["alignment_group_id"]: g for g in self._groups()}
        self.assertNotIn(only_de, by_id)  # only source gone → group dropped
        self.assertIn(multi, by_id)  # zh + en survive unchanged
        self.assertFalse(by_id[multi]["is_stale"])
        self.assertEqual(len(by_id[multi]["segments"]), 2)


if __name__ == "__main__":
    unittest.main()
