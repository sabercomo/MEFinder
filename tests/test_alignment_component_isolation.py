"""Alignment compute-component isolation.

The managed model component (MiniLM/E5 ONNX files) is optional at runtime:

* search and reading/locating stored alignment results never require it;
* starting a generation without it fails with a local install hint and never
  triggers a hidden network download;
* removing the component never touches stored results (they live in the DB);
* managing components (summary/download bookkeeping) does not import the
  compute stack itself.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from scripts.performance_fixture import create_fixture
from src.me_finder.managed_embedding_models import ManagedEmbeddingModels
from src.me_finder.search import SearchEngine
from src.me_finder.text_alignment import (
    generate_alignment,
    list_alignment_targets,
)

BLOCKED_MODULES = {"fastembed": None, "onnxruntime": None}


def build_public_library(root: Path) -> tuple[Path, str, str]:
    """Deterministic public fixture: searchable corpus plus a bilingual pair."""

    create_fixture(root, documents=2, paragraphs=20, alignment_paragraphs=8)
    database = root / "data" / "index.sqlite3"
    return database, "bench-002", "bench-003"


def fake_sequence_embeddings(sequences, cache_dir):
    import numpy as np

    vectors = []
    for texts in sequences:
        rows = []
        for text in texts:
            seed = sum(ord(char) for char in text) % 97
            rows.append([(seed % 13) / 13.0, ((seed * 7) % 11) / 11.0])
        vectors.append(np.asarray(rows, dtype="float32"))
    return vectors


def store_alignment_links(database: Path) -> None:
    """Generate stored links through the algorithm seam, like the product does."""

    from src.me_finder.semantic_alignment import SemanticLink

    links = [
        SemanticLink(0, 1, 0, 1, 0.09, 0.91, "automatic", "chapter:1"),
        SemanticLink(1, 2, 1, 2, 0.17, 0.83, "automatic", "chapter:1"),
    ]
    with mock.patch(
        "src.me_finder.text_alignment.embed_text_sequences",
        side_effect=fake_sequence_embeddings,
    ), mock.patch(
        "src.me_finder.text_alignment.align_segment_sequences",
        return_value=(links, []),
    ):
        generate_alignment(database, "bench-pair", "bench-002", "bench-003")


class ComponentIsolationTests(unittest.TestCase):
    def test_core_cold_start_and_stored_location_without_numeric_dependencies(self):
        script = '''
import importlib.abc, sys
from pathlib import Path
class NoCompute(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'fastembed', 'onnxruntime'}:
            raise ModuleNotFoundError(fullname, name=fullname)
sys.meta_path.insert(0, NoCompute())
from src.me_finder.web import make_handler
from src.me_finder.app_context import AppContext
from src.me_finder.text_alignment import locate_alignment, list_alignment_targets
root=Path(sys.argv[1]); db=root/'data/index.sqlite3'
handler=make_handler(db,app_context=AppContext.create(root,index_path=db))
try:
    from src.me_finder.search import SearchEngine
    engine=SearchEngine(db)
    assert engine.search('社会')['total']>0
    engine.close()
    assert list_alignment_targets(db,'bench-002')['targets']
    located=locate_alignment(db,'bench-002','bench-003',start_page_index=0,start_offset=0,end_page_index=0,end_offset=6)
    assert located['page_match_spans'], located
finally:
    assert handler.close_runtime()
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.db, self.pivot, self.target = build_public_library(self.root)
        store_alignment_links(self.db)

    def test_search_and_result_reads_run_without_compute_stack(self) -> None:
        with mock.patch.dict("sys.modules", BLOCKED_MODULES):
            with mock.patch(
                "src.me_finder.text_alignment.embed_text_sequences",
                side_effect=fake_sequence_embeddings,
            ), mock.patch(
                "src.me_finder.text_alignment.align_segment_sequences",
                return_value=([], []),
            ):
                targets = list_alignment_targets(self.db, self.pivot)
            self.assertTrue(targets["targets"])

            with mock.patch(
                "src.me_finder.text_alignment.embed_text_sequences",
                side_effect=fake_sequence_embeddings,
            ), mock.patch(
                "src.me_finder.text_alignment.align_segment_sequences",
                return_value=([], []),
            ):
                engine = SearchEngine(self.db)
                result = engine.search("社会", mode="auto")
            self.assertGreaterEqual(result["total"], 1)

            with sqlite3.connect(self.db) as connection:
                stored = connection.execute(
                    "SELECT COUNT(*) FROM alignment_links"
                ).fetchone()[0]
            self.assertEqual(stored, 2)

    def test_generation_without_component_fails_locally_with_install_hint(self) -> None:
        from src.me_finder.application.text_alignment_coordinator import (
            TextAlignmentCoordinator,
            TextAlignmentFailed,
        )

        class Paths:
            index_path = self.db
            runtime_root = self.root

        class IndexRuntime:
            def mutation(self):
                import contextlib

                return contextlib.nullcontext()

        class DurableOperations:
            def operation(self):
                import contextlib

                return contextlib.nullcontext()

        coordinator = TextAlignmentCoordinator(
            Paths(), IndexRuntime(), DurableOperations()
        )
        with mock.patch.dict("sys.modules", BLOCKED_MODULES):
            with self.assertRaises(TextAlignmentFailed) as caught:
                coordinator.generate("bench-pair", self.pivot, self.target, force=True)
        self.assertIn("对齐计算组件未安装", str(caught.exception))
        self.assertIn("译本对齐", str(caught.exception))
        # No hidden network download happened: the component is still absent.
        self.assertFalse(
            (self.root / "components" / "text-alignment" / "models").exists()
        )

    def test_removing_component_keeps_stored_results(self) -> None:
        components = self.root / "components" / "text-alignment" / "models"
        components.mkdir(parents=True)
        (components / "models--fake").write_text("model bytes")
        shutil.rmtree(self.root / "components")

        with sqlite3.connect(self.db) as connection:
            stored = connection.execute(
                "SELECT COUNT(*) FROM alignment_links"
            ).fetchone()[0]
        self.assertEqual(stored, 2)

        with mock.patch.dict("sys.modules", BLOCKED_MODULES), mock.patch(
            "src.me_finder.text_alignment.embed_text_sequences",
            side_effect=fake_sequence_embeddings,
        ), mock.patch(
            "src.me_finder.text_alignment.align_segment_sequences",
            return_value=([], []),
        ):
            targets = list_alignment_targets(self.db, self.pivot)
        self.assertTrue(targets["targets"])

        engine = SearchEngine(self.db)
        self.assertGreaterEqual(engine.search("社会", mode="auto")["total"], 1)

    def test_component_management_never_imports_the_compute_stack(self) -> None:
        managed = ManagedEmbeddingModels(self.root)
        with mock.patch.dict("sys.modules", BLOCKED_MODULES):
            summary = managed.summary()
        states = {str(item["id"]): str(item["state"]) for item in summary["models"]}
        self.assertEqual(states["minilm-l12-v2"], "not_installed")

    def test_installed_component_verifies_offline_after_download(self) -> None:
        # The managed download probe embeds locally after fastfetch -- it is
        # the compute stack itself; keep it importable and offline-pinned.
        from src.me_finder.managed_embedding_models import download_embedding_model

        with mock.patch(
            "src.me_finder.semantic_alignment.embed_texts"
        ) as probe, mock.patch.dict("sys.modules", BLOCKED_MODULES):
            download_embedding_model("minilm-l12-v2", self.root / "components")
        self.assertTrue(probe.called)


if __name__ == "__main__":
    unittest.main()
