"""Parity between the in-process compute and the out-of-process runner.

Requires the local MiniLM model cache (like the performance protocol). Without
it the whole case is skipped. It asserts that moving the compute to a separate
process changes nothing observable: identical links, scores, classification,
page/heading anchors and character intervals — compared exactly, with no added
tolerance. The only excluded fields are non-deterministic identifiers and
timestamps (alignment_run_id / alignment_link_id / created_at / completed_at),
listed explicitly below.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.me_finder.alignment_compute import (
    SubprocessAlignmentComputeRunner,
    run_in_process,
)
from src.me_finder.embedding_models import (
    DEFAULT_EMBEDDING_MODEL_ID,
    embedding_model_config,
    model_component_installed,
)
from src.me_finder.runtime_location import component_runtime_root


def _model_cache() -> Path:
    root = Path.home() / "Library" / "Application Support" / "MEFinder" / "runtime"
    return component_runtime_root(root) / "components" / "text-alignment" / "models"


MODEL_CACHE = _model_cache()
MODEL_PRESENT = model_component_installed(MODEL_CACHE, DEFAULT_EMBEDDING_MODEL_ID)

# Fields excluded from the persisted comparison, with rationale:
#   alignment_run_id / alignment_link_id : fresh UUIDs per run/link
#   created_at / completed_at            : wall-clock timestamps
EXCLUDED_PERSISTED_FIELDS = {
    "alignment_run_id",
    "alignment_link_id",
    "created_at",
    "completed_at",
}


@unittest.skipUnless(MODEL_PRESENT, "requires local MiniLM model cache")
class ComputeParityTests(unittest.TestCase):
    SRC = [
        "第一章 导论",
        "这是关于现代社会理论的详细讨论，涉及资本与劳动。",
        "第二章 方法",
        "本章说明比较研究的方法论基础。",
        "结论。",
    ]
    TGT = [
        "Chapter 1 Introduction",
        "This is a detailed discussion of modern social theory, concerning capital and labour.",
        "Chapter 2 Method",
        "This chapter explains the methodological basis of the comparative study.",
        "Conclusion.",
    ]

    def _kwargs(self) -> dict:
        return dict(
            cache_dir=MODEL_CACHE,
            embedding_model_id=DEFAULT_EMBEDDING_MODEL_ID,
            thresholds=embedding_model_config(DEFAULT_EMBEDDING_MODEL_ID).thresholds,
            reusable_sequences=([], []),
            folio_candidates=[],
            source_language="zh",
            target_language="en",
            reviewed_body_ranges=None,
        )

    def test_direct_compute_is_identical(self) -> None:
        kwargs = self._kwargs()
        in_links, in_anchors = run_in_process(self.SRC, self.TGT, **kwargs)
        runner = SubprocessAlignmentComputeRunner(task_id="parity")
        runner.probe()
        sub_links, sub_anchors = runner(self.SRC, self.TGT, **kwargs)
        # Exact equality of every SemanticLink (char intervals, cost,
        # confidence, review_status, anchor_key) and every HeadingAnchor.
        self.assertEqual(in_links, sub_links)
        self.assertEqual(in_anchors, sub_anchors)


def _fixture(root: Path) -> Path:
    from scripts.performance_fixture import create_fixture

    create_fixture(root, documents=2, paragraphs=20, alignment_paragraphs=8)
    return root / "data" / "index.sqlite3"


def _run_rows(db: Path, run_id: str):
    """Ordered links for a run, with member segment identities, minus excluded
    non-deterministic fields."""

    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        links = connection.execute(
            "SELECT order_index, cost, confidence, anchor_key, review_status, "
            "alignment_link_id FROM alignment_links WHERE alignment_run_id=? "
            "ORDER BY order_index",
            (run_id,),
        ).fetchall()
        rows = []
        for link in links:
            members = connection.execute(
                "SELECT side, member_order, segment_id FROM alignment_link_members "
                "WHERE alignment_link_id=? ORDER BY side, member_order",
                (link["alignment_link_id"],),
            ).fetchall()
            rows.append(
                {
                    "order_index": link["order_index"],
                    "cost": link["cost"],
                    "confidence": link["confidence"],
                    "anchor_key": link["anchor_key"],
                    "review_status": link["review_status"],
                    "members": [
                        (m["side"], m["member_order"], m["segment_id"]) for m in members
                    ],
                }
            )
        return rows


@unittest.skipUnless(MODEL_PRESENT, "requires local MiniLM model cache")
class EndToEndParityTests(unittest.TestCase):
    def test_generate_alignment_matches_across_runners(self) -> None:
        from src.me_finder.text_alignment import generate_alignment

        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            db_a = _fixture(Path(tmp_a))
            db_b = _fixture(Path(tmp_b))
            common = dict(
                document_group_id="bench-pair",
                pivot_source_file_id="bench-002",
                target_source_file_id="bench-003",
                force=True,
                model_cache_dir=MODEL_CACHE,
            )
            res_a = generate_alignment(db_a, common["document_group_id"],
                                       common["pivot_source_file_id"],
                                       common["target_source_file_id"],
                                       force=True, model_cache_dir=MODEL_CACHE)
            runner = SubprocessAlignmentComputeRunner(task_id="e2e")
            res_b = generate_alignment(db_b, common["document_group_id"],
                                       common["pivot_source_file_id"],
                                       common["target_source_file_id"],
                                       force=True, model_cache_dir=MODEL_CACHE,
                                       compute_runner=runner)

            # Return dicts equal except the run id.
            a = {k: v for k, v in res_a.items() if k not in EXCLUDED_PERSISTED_FIELDS}
            b = {k: v for k, v in res_b.items() if k not in EXCLUDED_PERSISTED_FIELDS}
            self.assertEqual(a, b)

            rows_a = _run_rows(db_a, str(res_a["alignment_run_id"]))
            rows_b = _run_rows(db_b, str(res_b["alignment_run_id"]))
            self.assertTrue(rows_a, "fixture produced no links")
            # Persisted scores, classification, ordering, anchors and member
            # segment identities (character-interval basis) match exactly.
            self.assertEqual(rows_a, rows_b)


if __name__ == "__main__":
    unittest.main()
