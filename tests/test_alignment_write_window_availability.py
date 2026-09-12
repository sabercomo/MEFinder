"""Search availability across alignment write windows.

Reproduces the alignment-time 503 report at the runtime boundary: while a real
alignment generation runs against a real SQLite library, a real
``IndexRuntime``/``SearchEngine`` must keep serving concurrent searches —
including inside the prepare window (``BEGIN IMMEDIATE`` open) and the publish
window — and a failed publish must roll back without taking search down.

Interleaving is controlled with ``threading.Event`` only: each window wrapper
blocks until the search thread has completed a search inside it, so the tests
never rely on timing luck.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

from src.me_finder.app_context import AppPaths
from src.me_finder.application.index_runtime import IndexRuntime
from src.me_finder.application.search_service import SearchRequest
from src.me_finder.application.text_alignment_coordinator import (
    TextAlignmentCancelled,
    TextAlignmentCoordinator,
    TextAlignmentFailed,
)
from src.me_finder.database import replace_source_in_database
from src.me_finder.embedding_runtime import SemanticAlignmentCancelled
from src.me_finder.search import SearchEngine
from src.me_finder import text_alignment as text_alignment_module
from scripts.performance_fixture import create_fixture

SEARCH_QUERY = {"query": "青铜", "mode": "exact", "limit": 5}
WINDOW_WAIT_TIMEOUT = 15.0


def _fake_embeddings(texts, _cache_dir):
    return np.ones((len(texts), 4), dtype=np.float32)


class _DurableOperations:
    @contextmanager
    def operation(self):
        yield


class AlignmentWriteWindowAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        create_fixture(self.root, documents=2, paragraphs=12, alignment_paragraphs=8)
        self.paths = AppPaths.create(self.root)
        self.index_runtime = IndexRuntime(
            self.paths,
            engine_factory=lambda path: SearchEngine(path),
            script_folding_enabled=lambda: True,
            rebuild_index=lambda *_args, **_kwargs: None,
            replace_source=lambda extracted, path, *, backup_existing: (
                replace_source_in_database(
                    extracted, path, backup_existing=backup_existing
                )
            ),
        )
        self.coordinator = TextAlignmentCoordinator(
            self.paths, self.index_runtime, _DurableOperations()
        )
        # The coordinator's model gate is a settings-UI concern covered
        # elsewhere; these tests drive real generation with stub vectors.
        self._gate_patches = [
            mock.patch(
                "src.me_finder.application.text_alignment_coordinator."
                "model_component_installed",
                return_value=True,
            ),
            mock.patch(
                "src.me_finder.application.text_alignment_coordinator.find_spec",
                return_value=object(),
            ),
        ]
        for patcher in self._gate_patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.index_runtime.close()

    # -- helpers -----------------------------------------------------------

    def _search(self) -> dict | None:
        return self.index_runtime.search(SearchRequest.from_payload(SEARCH_QUERY))

    def _warmup_search(self) -> str:
        result = self._search()
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["total"], 1)
        return json.dumps(result, sort_keys=True, ensure_ascii=False)

    def _generate_with(self, *, provider=_fake_embeddings):
        """Run the real generate_alignment through the real coordinator."""

        real_generate = text_alignment_module.generate_alignment

        def forwarded(db_path, group, pivot, target, **kwargs):
            kwargs["embedding_provider"] = provider
            return real_generate(db_path, group, pivot, target, **kwargs)

        with mock.patch(
            "src.me_finder.application.text_alignment_coordinator.generate_alignment",
            side_effect=forwarded,
        ):
            return self.coordinator.generate(
                "bench-pair", "bench-002", "bench-003", force=True
            )

    def _run_generate_in_thread(self, *, provider=_fake_embeddings):
        outcome: dict = {}

        def run():
            try:
                outcome["result"] = self._generate_with(provider=provider)
            except BaseException as exc:  # forwarded to the main-thread asserts
                outcome["error"] = exc

        worker = threading.Thread(target=run, name="probe-alignment")
        worker.start()
        return worker, outcome

    def _assert_no_partial_publish(self) -> None:
        with sqlite3.connect(self.paths.index_path) as connection:
            runs = connection.execute(
                "SELECT COUNT(*) FROM alignment_runs WHERE status = 'completed'"
            ).fetchone()[0]
            links = connection.execute(
                "SELECT COUNT(*) FROM alignment_links"
            ).fetchone()[0]
        self.assertEqual(runs, 0)
        self.assertEqual(links, 0)

    # -- the reproduction --------------------------------------------------

    def test_search_serves_through_prepare_compute_and_publish(self) -> None:
        warmup = self._warmup_search()
        compute_started = threading.Event()
        window1_entered = threading.Event()
        window2_entered = threading.Event()
        search_during_compute = threading.Event()
        search_during_window1 = threading.Event()
        search_during_window2 = threading.Event()
        alignment_done = threading.Event()
        records: list[dict] = []
        stop_search = threading.Event()

        def gated_embeddings(texts, cache_dir):
            compute_started.set()
            if not search_during_compute.wait(timeout=WINDOW_WAIT_TIMEOUT):
                raise RuntimeError("no search completed during compute")
            return _fake_embeddings(texts, cache_dir)

        real_folio = text_alignment_module.detect_folio_boundary_candidates
        real_publish = text_alignment_module._generate_alignment_on_connection

        def gated_folio(*args, **kwargs):
            window1_entered.set()
            if not search_during_window1.wait(timeout=WINDOW_WAIT_TIMEOUT):
                raise RuntimeError("no search completed inside prepare window")
            return real_folio(*args, **kwargs)

        def gated_publish(*args, **kwargs):
            window2_entered.set()
            if not search_during_window2.wait(timeout=WINDOW_WAIT_TIMEOUT):
                raise RuntimeError("no search completed inside publish window")
            return real_publish(*args, **kwargs)

        with mock.patch.object(
            text_alignment_module,
            "detect_folio_boundary_candidates",
            side_effect=gated_folio,
        ), mock.patch.object(
            text_alignment_module,
            "_generate_alignment_on_connection",
            side_effect=gated_publish,
        ):
            worker, outcome = self._run_generate_in_thread(
                provider=gated_embeddings
            )

            def search_worker():
                while not stop_search.is_set() and not alignment_done.is_set():
                    if window2_entered.is_set():
                        phase = "window2"
                    elif compute_started.is_set():
                        phase = "compute"
                    elif window1_entered.is_set():
                        phase = "window1"
                    else:
                        phase = "warmup"
                    result = self._search()
                    records.append(
                        {
                            "phase": phase,
                            "served": result is not None,
                            "payload": (
                                json.dumps(
                                    result, sort_keys=True, ensure_ascii=False
                                )
                                if result is not None
                                else None
                            ),
                        }
                    )
                    if phase == "window1":
                        search_during_window1.set()
                    elif phase == "compute":
                        search_during_compute.set()
                    elif phase == "window2":
                        search_during_window2.set()
                    stop_search.wait(0.002)

            searcher = threading.Thread(
                target=search_worker, name="probe-search", daemon=True
            )
            searcher.start()
            worker.join(timeout=WINDOW_WAIT_TIMEOUT)
            self.assertFalse(worker.is_alive(), "alignment hung")
            alignment_done.set()
            stop_search.set()
            searcher.join(timeout=5)

        self.assertNotIn("error", outcome)
        self.assertGreaterEqual(outcome["result"]["alignment_link_count"], 1)

        by_phase: dict[str, list[dict]] = {}
        for record in records:
            by_phase.setdefault(record["phase"], []).append(record)
        for phase in ("window1", "compute", "window2"):
            self.assertTrue(by_phase.get(phase), f"no search landed in {phase}")
        for record in records:
            self.assertTrue(
                record["served"],
                f"search during {record['phase']} returned None (HTTP 503)",
            )
            self.assertEqual(record["payload"], warmup)
        with sqlite3.connect(self.paths.index_path) as connection:
            runs = connection.execute(
                "SELECT COUNT(*) FROM alignment_runs WHERE status = 'completed'"
            ).fetchone()[0]
        self.assertEqual(runs, 1)

    def test_failed_publish_rolls_back_and_search_stays_available(self) -> None:
        warmup = self._warmup_search()
        real_publish = text_alignment_module._generate_alignment_on_connection

        def exploding_publish(*args, **kwargs):
            # Run the real publish writes, then fail before the commit so the
            # rollback path is exercised against real inserted rows.
            real_publish(*args, **kwargs)
            raise RuntimeError("publish exploded")

        with mock.patch.object(
            text_alignment_module,
            "_generate_alignment_on_connection",
            side_effect=exploding_publish,
        ):
            with self.assertRaises(TextAlignmentFailed):
                self._generate_with()

        self.assertEqual(
            self._warmup_search(), warmup, "search changed after failed publish"
        )
        self._assert_no_partial_publish()

        # Recovery: the next generation completes without any manual cleanup.
        result = self._generate_with()
        self.assertEqual(result["status"], "completed")
        self.assertGreaterEqual(result["alignment_link_count"], 1)
        self._warmup_search()

    def test_cancelled_generation_leaves_no_partial_state(self) -> None:
        warmup = self._warmup_search()

        def cancelled_embeddings(texts, cache_dir):
            raise SemanticAlignmentCancelled("user cancelled")

        with self.assertRaises(TextAlignmentCancelled):
            self._generate_with(provider=cancelled_embeddings)

        self._assert_no_partial_publish()
        self.assertEqual(self._warmup_search(), warmup)

    def test_concurrent_source_change_waits_and_never_corrupts_results(
        self,
    ) -> None:
        warmup = self._warmup_search()
        compute_started = threading.Event()
        change_started = threading.Event()
        timeline: dict[str, float] = {}
        lock = threading.Lock()

        def gated_embeddings(texts, cache_dir):
            compute_started.set()
            if not change_started.wait(timeout=WINDOW_WAIT_TIMEOUT):
                raise RuntimeError("concurrent change never attempted")
            return _fake_embeddings(texts, cache_dir)

        def attempt_change() -> None:
            with lock:
                timeline["change-attempt"] = time.monotonic()
            # Blocks on index_runtime.mutation() until the alignment run ends.
            self.index_runtime.replace_source(
                {
                    "source_files": [
                        {
                            "source_file_id": "bench-003",
                            "source_type": "pdf",
                            "file_name": "bench-003.pdf",
                            "title": "bench-003",
                        }
                    ],
                    "volumes": [],
                    "works": [],
                    "toc_entries": [],
                    "paragraphs": [],
                    "page_anchors": [],
                    "pdf_pages": [
                        {
                            "source_file_id": "bench-003",
                            "source_type": "pdf",
                            "pdf_page_id": "bench-003-PAGE-000000",
                            "pdf_page_index": 0,
                            "physical_pdf_page": 1,
                            "pdf_page_number_1based": 1,
                            "text_raw": "替换后的正文没有原句命中。",
                            "blocks": [],
                        }
                    ],
                    "pdf_page_mappings": [],
                    "pdf_import_runs": [],
                    "audit_issues": [],
                },
                "bench-003",
            )
            with lock:
                timeline["change-done"] = time.monotonic()

        real_publish = text_alignment_module._generate_alignment_on_connection

        def gated_publish(*args, **kwargs):
            result = real_publish(*args, **kwargs)
            with lock:
                timeline["publish-done"] = time.monotonic()
            return result

        with mock.patch.object(
            text_alignment_module,
            "_generate_alignment_on_connection",
            side_effect=gated_publish,
        ):
            worker, outcome = self._run_generate_in_thread(
                provider=gated_embeddings
            )
            self.assertTrue(compute_started.wait(timeout=WINDOW_WAIT_TIMEOUT))
            changer = threading.Thread(target=attempt_change, name="probe-change")
            changer.start()
            change_started.set()
            worker.join(timeout=WINDOW_WAIT_TIMEOUT)
            changer.join(timeout=WINDOW_WAIT_TIMEOUT)
            self.assertFalse(worker.is_alive(), "alignment hung")
            self.assertFalse(changer.is_alive(), "concurrent change hung")

        self.assertNotIn("error", outcome)
        # The change was attempted while the alignment still held the mutation
        # and could only complete after the publish had finished.
        self.assertLess(timeline["change-attempt"], timeline["publish-done"])
        self.assertGreater(timeline["change-done"], timeline["publish-done"])

        # The deferred replacement cascades the derived alignment data away,
        # and the reopened engine serves the changed library.
        with sqlite3.connect(self.paths.index_path) as connection:
            runs = connection.execute(
                "SELECT COUNT(*) FROM alignment_runs"
            ).fetchone()[0]
            orphans = connection.execute(
                "SELECT COUNT(*) FROM alignment_links WHERE alignment_run_id "
                "NOT IN (SELECT alignment_run_id FROM alignment_runs)"
            ).fetchone()[0]
        self.assertEqual(runs, 0)
        self.assertEqual(orphans, 0)
        served = self._search()
        self.assertIsNotNone(served)
        self.assertNotEqual(
            json.dumps(served, sort_keys=True, ensure_ascii=False), warmup
        )


if __name__ == "__main__":
    unittest.main()
