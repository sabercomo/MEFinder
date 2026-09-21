"""End-to-end (DB) test of the Bertalign backend without heavy deps.

Seeds a default-backend run, then runs ``generate_bertalign_alignment`` with an
injected compute runner (so no torch/faiss/numba/model is needed) and asserts:

* the Bertalign run persists with its own algorithm identity and honest params;
* links/members are written with the same schema, so the reader route resolves
  and locating still returns target spans (page anchors + char ranges);
* the pre-existing default run is NOT superseded and stays readable — the two
  backends coexist and never mix caches or vector spaces;
* re-running reuses the stored Bertalign run.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from src.me_finder.bertalign_backend import (
    BERTALIGN_ALGORITHM,
    BERTALIGN_MODEL_ID,
    beads_to_semantic_links,
)
from src.me_finder.bertalign_alignment import generate_bertalign_alignment
from src.me_finder.persistence.index_schema import SCHEMA
from src.me_finder.text_alignment import (
    ALIGNMENT_ALGORITHM,
    generate_alignment,
    locate_alignment,
)


def _fake_embeddings(texts, _cache_dir):
    vectors = []
    for text in texts:
        if "Geist" in text or "精神" in text:
            vectors.append((1.0, 0.0, 0.0))
        elif "Wahrheit" in text or "真理" in text:
            vectors.append((0.0, 1.0, 0.0))
        else:
            vectors.append((0.0, 0.0, 1.0))
    return np.asarray(vectors, dtype=np.float32)


def _fake_embedding_sequences(sequences, cache_dir, **_kwargs):
    return [_fake_embeddings(texts, cache_dir) for texts in sequences]


def _page(source_id, index, text):
    return {
        "source_file_id": source_id,
        "source_type": "pdf",
        "pdf_page_id": f"{source_id}-PAGE-{index:06d}",
        "pdf_page_index": index,
        "pdf_page_number_1based": index + 1,
        "text_raw": text,
        "blocks": [
            {
                "block_index": 0,
                "text": text,
                "bbox": [10, 20, 300, 80],
                "bbox_normalized": [0.01, 0.02, 0.3, 0.08],
            }
        ],
    }


def _stub_bertalign_runner(source_texts, target_texts, **_kwargs):
    """Deterministic cover: one 2-2 many-to-many bead, then 1-1, then tails."""

    src_n = len(source_texts)
    tgt_n = len(target_texts)
    n = min(src_n, tgt_n)
    beads = []
    i = 0
    if n >= 2:
        beads.append(([0, 1], [0, 1]))
        i = 2
    while i < n:
        beads.append(([i], [i]))
        i += 1
    beads.extend(([s], []) for s in range(n, src_n))
    beads.extend(([], [t]) for t in range(n, tgt_n))
    links = beads_to_semantic_links(
        beads,
        src_n,
        tgt_n,
        source_start=0,
        source_end=src_n,
        target_start=0,
        target_end=tgt_n,
        confidence=lambda s, t: 0.77,
    )
    return links, []


class BertalignBackendIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.embedding_patch = mock.patch(
            "src.me_finder.alignment_kernel.embed_text_sequences",
            side_effect=_fake_embedding_sequences,
        )
        self.embedding_patch.start()
        self.addCleanup(self.embedding_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "index.sqlite3"
        connection = sqlite3.connect(str(self.db))
        try:
            connection.executescript(SCHEMA)
            sources = (
                ("pdf-de", "de", "Phänomenologie"),
                ("pdf-zh", "zh-Hans", "精神现象学"),
            )
            connection.executemany(
                "INSERT INTO source_files(source_file_id, source_type, file_name, "
                "relative_path, volume_number, payload_json) VALUES (?, 'pdf', ?, NULL, NULL, ?)",
                [
                    (
                        sid,
                        f"{sid}.pdf",
                        json.dumps(
                            {"source_file_id": sid, "language_code": lang, "title": title},
                            ensure_ascii=False,
                        ),
                    )
                    for sid, lang, title in sources
                ],
            )
            connection.execute(
                "INSERT INTO document_groups(document_group_id, title, "
                "base_source_file_id, created_at, updated_at) "
                "VALUES ('work', '精神现象学', 'pdf-de', 't', 't')"
            )
            connection.executemany(
                "INSERT INTO document_group_members(document_group_id, source_file_id, "
                "version_label, member_order, added_at) VALUES ('work', ?, ?, ?, 't')",
                (("pdf-de", "德文", 0), ("pdf-zh", "中译", 1)),
            )
            de = (
                "Der Geist ist wirklich. Die Wahrheit ist das Ganze. "
                "Das Wissen ist frei. Die Vernunft ist alles."
            )
            zh = "精神是现实的。真理是全体。知识是自由的。理性是一切。"
            connection.executemany(
                "INSERT INTO pdf_pages(source_file_id, pdf_page_index, payload_json) VALUES (?, ?, ?)",
                [
                    ("pdf-de", 0, json.dumps(_page("pdf-de", 0, de), ensure_ascii=False)),
                    ("pdf-zh", 0, json.dumps(_page("pdf-zh", 0, zh), ensure_ascii=False)),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def _run_default(self):
        return generate_alignment(self.db, "work", "pdf-de", "pdf-zh")

    def test_bertalign_run_persists_coexists_and_locates(self) -> None:
        default = self._run_default()
        self.assertEqual(default["algorithm"], ALIGNMENT_ALGORITHM)

        result = generate_bertalign_alignment(
            self.db, "work", "pdf-de", "pdf-zh", compute_runner=_stub_bertalign_runner
        )
        self.assertEqual(result["algorithm"], BERTALIGN_ALGORITHM)
        self.assertEqual(result["embedding_model_id"], BERTALIGN_MODEL_ID)
        self.assertFalse(result["reused"])
        self.assertGreater(result["alignment_link_count"], 0)

        with sqlite3.connect(str(self.db)) as con:
            con.row_factory = sqlite3.Row
            runs = {
                r["algorithm"]: r["status"]
                for r in con.execute(
                    "SELECT algorithm, status FROM alignment_runs "
                    "WHERE pivot_source_file_id='pdf-de' AND target_source_file_id='pdf-zh'"
                )
            }
            # Both backends' runs are 'completed' — neither superseded the other.
            self.assertEqual(runs.get(ALIGNMENT_ALGORITHM), "completed")
            self.assertEqual(runs.get(BERTALIGN_ALGORITHM), "completed")
            berta_run = con.execute(
                "SELECT alignment_run_id, parameters_json FROM alignment_runs "
                "WHERE algorithm=?",
                (BERTALIGN_ALGORITHM,),
            ).fetchone()
            params = json.loads(berta_run["parameters_json"])
            self.assertEqual(params["upstream"], "bertalign")
            self.assertEqual(
                params["score_meaning"],
                "algorithmic_similarity_not_calibrated_accuracy",
            )
            # Members reference real segment ids (index-based mapping landed).
            member_count = con.execute(
                "SELECT COUNT(*) FROM alignment_link_members m JOIN alignment_links l "
                "USING(alignment_link_id) WHERE l.alignment_run_id=?",
                (berta_run["alignment_run_id"],),
            ).fetchone()[0]
            self.assertGreater(member_count, 0)

        # The reader route now resolves the Bertalign run (latest) and locating
        # returns target spans with page + char information.
        located = locate_alignment(
            self.db,
            "pdf-de",
            "pdf-zh",
            start_page_index=0,
            end_page_index=0,
            start_offset=0,
            end_offset=20,
        )
        self.assertTrue(located.get("target_segment_ids"))
        self.assertTrue(located.get("page_match_spans"))

    def test_reuse_returns_stored_run(self) -> None:
        self._run_default()
        first = generate_bertalign_alignment(
            self.db, "work", "pdf-de", "pdf-zh", compute_runner=_stub_bertalign_runner
        )
        second = generate_bertalign_alignment(
            self.db, "work", "pdf-de", "pdf-zh", compute_runner=_stub_bertalign_runner
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["alignment_run_id"], second["alignment_run_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
