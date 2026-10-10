"""Measured progress across embedding, matching, worker and HTTP boundaries."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from src.me_finder import alignment_compute, alignment_compute_worker, semantic_alignment
from src.me_finder.alignment_kernel import align_segment_sequences
from src.me_finder.embedding_models import embedding_model_config
from tests import test_text_alignment_controller


class AlignmentProgressTests(unittest.TestCase):
    def test_http_progress_estimates_only_observed_stage_and_survives_reload(self):
        fixture = test_text_alignment_controller.TextAlignmentControllerTests()
        fixture.setUp()
        entered, release = threading.Event(), threading.Event()
        controller = fixture.controller

        def compute(*_args, progress_callback, **_kwargs):
            progress_callback({"stage": "embedding", "completed": 0, "total": 100})
            with mock.patch("src.me_finder.text_alignment_controller.time.monotonic",
                            return_value=controller._progress_stage_started + 10):
                progress_callback({"stage": "embedding", "completed": 25, "total": 100})
            entered.set()
            self.assertTrue(release.wait(5))
            return {"status": "completed"}

        fixture.coordinator.generate = compute
        try:
            _, job = controller.start(fixture._generate_payload())
            self.assertTrue(entered.wait(2))
            status, body = controller.status({"job_id": [job["job_id"]]})
            self.assertEqual(status, 202)
            self.assertEqual(body["progress"]["percent"], 25)
            self.assertEqual(body["progress"]["eta_seconds"], 30)
            self.assertEqual(controller.current()[1]["progress"], body["progress"])
            controller._update_progress({"stage": "matching", "completed": 0, "total": 40})
            self.assertIsNone(controller.current()[1]["progress"]["eta_seconds"])
            controller._update_progress({"stage": "saving"})
            self.assertIsNone(controller.current()[1]["progress"]["percent"])
        finally:
            release.set()
            controller._job_thread.join(5)
        self.assertEqual(controller.current(), (200, {"running": False}))

    def test_embedding_reports_real_batch_counts(self):
        model = SimpleNamespace(embed=lambda texts, **_kw: (np.ones(3) for _ in texts))
        reports = []
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict("sys.modules", {"fastembed": mock.Mock(TextEmbedding=mock.Mock(return_value=model)),
                                                "onnxruntime": mock.Mock()}):
            provider = semantic_alignment.FastEmbedEmbeddingProvider(
                embedding_model_config("minilm-l12-v2"), progress_callback=reports.append)
            result = provider(["text"] * 70, Path(directory))
        self.assertEqual(result.shape, (70, 3))
        self.assertEqual([p["completed"] for p in reports], [0, 64, 70])
        self.assertTrue(all(p["total"] == 70 for p in reports))

    def test_matching_progress_preserves_links_and_cached_vectors_skip_embedding(self):
        texts = [f"A long paragraph about social theory number {i}." for i in range(70)]
        vectors = np.random.default_rng(1).normal(size=(70, 8)).astype(np.float32)
        reports = []
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            path = semantic_alignment._sequence_cache_path(texts, cache, model_id="minilm-l12-v2")
            path.parent.mkdir(parents=True)
            np.save(path, vectors)
            expected = align_segment_sequences(texts, texts, cache_dir=cache)
            with mock.patch.object(semantic_alignment, "embed_texts", side_effect=AssertionError("cached")):
                actual = align_segment_sequences(texts, texts, cache_dir=cache, progress_callback=reports.append)
        self.assertEqual(actual, expected)
        matching = [p for p in reports if p["stage"] == "matching"]
        self.assertGreater(len(matching), 2)
        self.assertEqual(matching[-1]["completed"], matching[-1]["total"])
        self.assertEqual([p["completed"] for p in matching], sorted(p["completed"] for p in matching))
        self.assertNotIn("embedding", [p["stage"] for p in reports])
        self.assertEqual(reports[-1]["stage"], "checking")

    def test_worker_emits_counts_and_runner_forwards_them(self):
        request = alignment_compute.build_request(task_id="progress-test", cache_dir=Path("cache"),
            source_texts=["a"], target_texts=["b"], embedding_model_id="minilm-l12-v2",
            thresholds=embedding_model_config("minilm-l12-v2").thresholds,
            reusable_sequences=(), folio_candidates=(), source_language="en", target_language="de",
            reviewed_body_ranges=None)
        control = io.StringIO()

        def compute(*_args, progress_callback, **_kwargs):
            progress_callback({"stage": "embedding", "completed": 2, "total": 2})
            return [], []

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(alignment_compute, "run_in_process", side_effect=compute):
            result_path = Path(directory) / "result.json"
            self.assertEqual(alignment_compute_worker._run_compute(request, result_path, control), 0)
            messages = [json.loads(line) for line in control.getvalue().splitlines()]
            runner = alignment_compute.SubprocessAlignmentComputeRunner(task_id="progress-test")
            reports = []
            with mock.patch.object(runner, "_spawn") as spawn, \
                    mock.patch.object(runner, "_pump", return_value=iter(messages)), \
                    mock.patch.object(runner, "_consume_result", return_value=([], [])), \
                    mock.patch.object(alignment_compute, "_terminate"):
                spawn.return_value.poll.return_value = 0
                runner(["a"], ["b"], cache_dir=Path(directory), progress_callback=reports.append)
        self.assertEqual(reports[-1]["completed"], 2)
        self.assertEqual(reports[-1]["total"], 2)
