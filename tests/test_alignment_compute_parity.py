"""Parity between the in-process compute and the out-of-process runner.

Requires the local MiniLM model cache. Each run uses an **isolated** compute
cache: the model files are symlinked read-only into a temp directory with a
fresh ``document-vectors`` folder, so the user's real cache is never written and
cold-start inference is genuinely exercised (not served from a shared warm
vector cache). Parity is asserted exactly, with no added tolerance; the only
excluded fields are non-deterministic identifiers/timestamps.
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

EXCLUDED_PERSISTED_FIELDS = {
    "alignment_run_id",  # fresh UUID per run
    "alignment_link_id",  # fresh UUID per link
    "created_at",  # wall-clock
    "completed_at",  # wall-clock
}


def isolated_cache(tmp: Path, *, name: str = "cache") -> Path:
    """A compute cache whose model files are symlinked read-only from the real
    cache but whose document-vectors folder is fresh (cold inference)."""

    cache = tmp / name
    cache.mkdir(parents=True)
    for child in MODEL_CACHE.iterdir():
        if child.name == "document-vectors":
            continue
        (cache / child.name).symlink_to(child)
    (cache / "document-vectors").mkdir()
    return cache


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

    def _kwargs(self, cache: Path) -> dict:
        return dict(
            cache_dir=cache,
            embedding_model_id=DEFAULT_EMBEDDING_MODEL_ID,
            thresholds=embedding_model_config(DEFAULT_EMBEDDING_MODEL_ID).thresholds,
            reusable_sequences=([], []),
            folio_candidates=[],
            source_language="zh",
            target_language="en",
            reviewed_body_ranges=None,
        )

    def test_cold_direct_compute_is_identical(self) -> None:
        # Separate cold caches for each path: both compute embeddings fresh, so
        # this proves cold subprocess inference equals cold in-process inference.
        with tempfile.TemporaryDirectory() as tmp:
            cache_a = isolated_cache(Path(tmp), name="in")
            cache_b = isolated_cache(Path(tmp), name="sub")
            in_links, in_anchors = run_in_process(self.SRC, self.TGT, **self._kwargs(cache_a))
            runner = SubprocessAlignmentComputeRunner(task_id="parity")
            runner.probe()
            sub_links, sub_anchors = runner(self.SRC, self.TGT, **self._kwargs(cache_b))
            self.assertEqual(in_links, sub_links)
            self.assertEqual(in_anchors, sub_anchors)

    def test_warm_cache_reuse_matches_cold(self) -> None:
        # First subprocess run is cold (writes vectors); the second reuses them.
        # Results must be identical, and the vector cache must be populated.
        with tempfile.TemporaryDirectory() as tmp:
            cache = isolated_cache(Path(tmp), name="warm")
            runner = SubprocessAlignmentComputeRunner(task_id="warm")
            cold = runner(self.SRC, self.TGT, **self._kwargs(cache))
            vectors = list((cache / "document-vectors").glob("*.npy"))
            self.assertTrue(vectors, "cold run should populate the vector cache")
            warm = runner(self.SRC, self.TGT, **self._kwargs(cache))
            self.assertEqual(cold, warm)


def _fixture(root: Path) -> Path:
    from scripts.performance_fixture import create_fixture

    create_fixture(root, documents=2, paragraphs=20, alignment_paragraphs=8)
    return root / "data" / "index.sqlite3"


def _run_rows(db: Path, run_id: str):
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
            cache_a = isolated_cache(Path(tmp_a))
            cache_b = isolated_cache(Path(tmp_b))
            res_a = generate_alignment(db_a, "bench-pair", "bench-002", "bench-003",
                                       force=True, model_cache_dir=cache_a)
            runner = SubprocessAlignmentComputeRunner(task_id="e2e")
            res_b = generate_alignment(db_b, "bench-pair", "bench-002", "bench-003",
                                       force=True, model_cache_dir=cache_b,
                                       compute_runner=runner)

            a = {k: v for k, v in res_a.items() if k not in EXCLUDED_PERSISTED_FIELDS}
            b = {k: v for k, v in res_b.items() if k not in EXCLUDED_PERSISTED_FIELDS}
            self.assertEqual(a, b)

            rows_a = _run_rows(db_a, str(res_a["alignment_run_id"]))
            rows_b = _run_rows(db_b, str(res_b["alignment_run_id"]))
            self.assertTrue(rows_a, "fixture produced no links")
            self.assertEqual(rows_a, rows_b)


# The core acceptance combination Astra called out: a main process that CANNOT
# import numpy/fastembed/onnxruntime still generates AND publishes by delegating
# to a real external compute process. Run in a child interpreter that installs an
# import block on the compute stack, then drives generate_alignment through the
# real subprocess runner.
_CLOSURE_SCRIPT = r'''
import importlib.abc, sys
from pathlib import Path
class NoCompute(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy','fastembed','onnxruntime'}:
            raise ModuleNotFoundError(fullname, name=fullname)
sys.meta_path.insert(0, NoCompute())
root = Path(sys.argv[1]); model_cache = Path(sys.argv[2])
from scripts.performance_fixture import create_fixture
from src.me_finder.text_alignment import generate_alignment
from src.me_finder.alignment_compute import SubprocessAlignmentComputeRunner
import sqlite3
# isolated compute cache: symlink models, fresh vectors
cache = root/'cache'; cache.mkdir()
for child in model_cache.iterdir():
    if child.name!='document-vectors':
        (cache/child.name).symlink_to(child)
(cache/'document-vectors').mkdir()
create_fixture(root, documents=2, paragraphs=20, alignment_paragraphs=8)
db = root/'data/index.sqlite3'
runner = SubprocessAlignmentComputeRunner(task_id='closure')
caps = runner.probe()
assert all(caps.values()), caps
result = generate_alignment(db,'bench-pair','bench-002','bench-003',force=True,
                            model_cache_dir=cache, compute_runner=runner)
assert result['status']=='completed', result
assert not result.get('reused'), 'force must recompute'
with sqlite3.connect(db) as c:
    n = c.execute("SELECT COUNT(*) FROM alignment_runs WHERE status='completed'").fetchone()[0]
    links = c.execute("SELECT COUNT(*) FROM alignment_links WHERE alignment_run_id=?",(result['alignment_run_id'],)).fetchone()[0]
assert n>=1 and links>0, (n,links)
# The main process must never have imported the compute stack itself.
assert not any(m in sys.modules for m in ('numpy','fastembed','onnxruntime')), \
    [m for m in ('numpy','fastembed','onnxruntime') if m in sys.modules]
print('CLOSURE_OK', result['alignment_link_count'])
'''


@unittest.skipUnless(MODEL_PRESENT, "requires local MiniLM model cache")
class NumpyFreeMainSuccessClosureTests(unittest.TestCase):
    def test_numpy_free_main_generates_via_external_runtime(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-c", _CLOSURE_SCRIPT, tmp, str(MODEL_CACHE)],
                cwd=str(REPO), capture_output=True, text=True, timeout=180,
                env={**__import__("os").environ, "PYTHONPATH": str(REPO),
                     "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
            )
            self.assertIn("CLOSURE_OK", proc.stdout, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
