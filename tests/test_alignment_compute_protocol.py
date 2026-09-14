"""Protocol, error and boundary behaviour of the alignment compute seam.

These tests need no embedding model: they exercise serialization, the external
capability probe, protocol/version handling, cancellation, worker crashes and
the "never publish a stale or half-built result" guarantees using fault
injection (``MEFINDER_ALIGNMENT_COMPUTE_SIMULATE``) — never the real compute.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.me_finder import alignment_compute as ac
from src.me_finder.alignment_anchors import HeadingAnchor
from src.me_finder.edition_folio_anchors import FolioBoundaryCandidate
from src.me_finder.embedding_models import AlignmentThresholds
from src.me_finder.semantic_alignment import SemanticLink


def _simulate_env(code: str) -> dict:
    return {**os.environ, ac.SIMULATE_ENV: code}


TINY_INPUTS = dict(
    cache_dir=Path("/nonexistent-models"),
    embedding_model_id="minilm-l12-v2",
    thresholds=AlignmentThresholds(0.56, 0.5, 0.05),
    reusable_sequences=([], []),
    folio_candidates=[],
    source_language="zh",
    target_language="en",
    reviewed_body_ranges=None,
)


class SerializationTests(unittest.TestCase):
    def test_request_and_result_round_trip_without_numpy(self) -> None:
        request = ac.build_request(
            task_id="t1",
            cache_dir=Path("/models"),
            embedding_model_id="minilm-l12-v2",
            source_texts=["甲", "乙"],
            target_texts=["a"],
            thresholds=AlignmentThresholds(0.56, 0.5, 0.05),
            reusable_sequences=[[], []],
            folio_candidates=[
                FolioBoundaryCandidate(1, 0, 0, 2, (0.1, 0.2, 0.3, 0.4), 0.9)
            ],
            source_language="zh",
            target_language="en",
            reviewed_body_ranges={"pivot": [0, 2], "target": [0, 1]},
        )
        folio = ac._deserialize_folio(request["inputs"]["folio_candidates"][0])
        self.assertEqual(folio, FolioBoundaryCandidate(1, 0, 0, 2, (0.1, 0.2, 0.3, 0.4), 0.9))
        self.assertIsInstance(folio.target_bbox, tuple)

        computed = (
            [SemanticLink(0, 1, 0, 1, 0.12, 0.9, "automatic", "term:x")],
            [HeadingAnchor(0, 0, "k")],
        )
        payload = ac.serialize_result(
            task_id="t1", identity=request["input_identity"], computed=computed
        )
        links, anchors = ac.deserialize_result(payload)
        self.assertEqual(links, computed[0])
        self.assertEqual(anchors, computed[1])

    def test_input_identity_is_stable_and_input_sensitive(self) -> None:
        base = dict(
            embedding_model_id="minilm-l12-v2",
            source_texts=["a"],
            target_texts=["b"],
            thresholds=AlignmentThresholds(0.56, 0.5, 0.05),
            reusable_sequences=[[], []],
            folio_candidates=[],
            source_language="zh",
            target_language="en",
            reviewed_body_ranges=None,
        )
        one = ac.build_request(task_id="x", cache_dir=Path("/a"), **base)
        two = ac.build_request(task_id="y", cache_dir=Path("/b"), **base)
        # task_id and cache_dir are not part of the input identity.
        self.assertEqual(one["input_identity"], two["input_identity"])
        changed = dict(base)
        changed["target_texts"] = ["c"]
        three = ac.build_request(task_id="x", cache_dir=Path("/a"), **changed)
        self.assertNotEqual(one["input_identity"], three["input_identity"])


class ProbeTests(unittest.TestCase):
    def test_probe_reports_capabilities_in_dev_runtime(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(task_id="probe")
        caps = runner.probe()
        for name in ("numpy", "fastembed", "onnxruntime"):
            self.assertTrue(caps.get(name), f"{name} should be importable in dev venv")

    def test_probe_component_missing_is_a_clear_error(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="probe", env=_simulate_env("component_missing")
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner.probe()
        self.assertEqual(ctx.exception.code, ac.COMPONENT_MISSING)

    def test_probe_protocol_mismatch_is_a_clear_error(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="probe", env=_simulate_env("probe_protocol")
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner.probe()
        self.assertEqual(ctx.exception.code, ac.PROTOCOL_INCOMPATIBLE)


class WorkerProtocolTests(unittest.TestCase):
    def test_worker_rejects_incompatible_request_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "request.json"
            result = Path(tmp) / "result.json"
            request.write_text(
                json.dumps({"protocol": 999, "task_id": "t", "inputs": {}}),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, "-m", "src.me_finder.alignment_compute_worker",
                 str(request), str(result)],
                cwd=str(REPO),
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(REPO)},
            )
            messages = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            errors = [m for m in messages if m.get("type") == "error"]
            self.assertTrue(errors, proc.stdout + proc.stderr)
            self.assertEqual(errors[0]["code"], ac.PROTOCOL_INCOMPATIBLE)
            self.assertFalse(result.exists())


class RunnerFailureTests(unittest.TestCase):
    def test_worker_crash_surfaces_as_clear_error(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="crash", env=_simulate_env("crash")
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner(["a"], ["b"], **TINY_INPUTS)
        self.assertEqual(ctx.exception.code, ac.WORKER_CRASHED)

    def test_cancellation_terminates_the_worker(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="hang",
            cancel_check=lambda: True,
            env=_simulate_env("hang"),
            poll_interval=0.02,
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner(["a"], ["b"], **TINY_INPUTS)
        self.assertEqual(ctx.exception.code, ac.CANCELLED)

    def test_result_identity_mismatch_is_refused(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="tamper", env=_simulate_env("tamper_identity")
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner(["a"], ["b"], **TINY_INPUTS)
        self.assertEqual(ctx.exception.code, ac.RESULT_MISMATCH)

    def test_custom_embedding_provider_is_rejected_not_silently_inprocess(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(task_id="prov")
        with self.assertRaises(ac.AlignmentComputeError):
            runner(["a"], ["b"], embedding_provider=lambda *a, **k: None, **TINY_INPUTS)


class NoPublishOnFailureTests(unittest.TestCase):
    """generate_alignment must not publish when the compute fails or is stale.

    These use a real public fixture but a fault-injected runner, so no model is
    needed: the failure happens before any real compute.
    """

    def _fixture(self, root: Path):
        from scripts.performance_fixture import create_fixture

        create_fixture(root, documents=2, paragraphs=20, alignment_paragraphs=8)
        return root / "data" / "index.sqlite3"

    def _completed_run_count(self, db: Path) -> int:
        import sqlite3

        with sqlite3.connect(db) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM alignment_runs WHERE status='completed'"
            ).fetchone()[0]

    def _assert_no_publish(self, simulate_code: str, expected_code: str) -> None:
        from src.me_finder.text_alignment import generate_alignment

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._fixture(root)
            before = self._completed_run_count(db)
            runner = ac.SubprocessAlignmentComputeRunner(
                task_id="np", env=_simulate_env(simulate_code)
            )
            with self.assertRaises(ac.AlignmentComputeError) as ctx:
                generate_alignment(
                    db,
                    "bench-pair",
                    "bench-002",
                    "bench-003",
                    force=True,
                    model_cache_dir=root / "models",
                    compute_runner=runner,
                )
            self.assertEqual(ctx.exception.code, expected_code)
            after = self._completed_run_count(db)
            self.assertEqual(before, after, "no new completed run may be published")

    def test_crash_publishes_nothing(self) -> None:
        self._assert_no_publish("crash", ac.WORKER_CRASHED)

    def test_stale_result_publishes_nothing(self) -> None:
        self._assert_no_publish("tamper_identity", ac.RESULT_MISMATCH)


if __name__ == "__main__":
    unittest.main()
