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
from importlib.util import find_spec
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.me_finder import alignment_compute as ac
from src.me_finder.alignment_anchors import HeadingAnchor
from src.me_finder.edition_folio_anchors import FolioBoundaryCandidate
from src.me_finder.embedding_models import AlignmentThresholds
from src.me_finder.semantic_alignment import SemanticLink

# The compute stack is optional at runtime: a core-only CI env has none of it.
# Only capability-present assertions are gated on it; the "reports missing"
# behaviour is exercised regardless via fault injection.
_DEPS_PRESENT = all(find_spec(name) is not None for name in ("numpy", "fastembed", "onnxruntime"))


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
    @unittest.skipUnless(_DEPS_PRESENT, "requires numpy/fastembed/onnxruntime installed")
    def test_probe_reports_capabilities_when_present(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(task_id="probe")
        caps = runner.probe()
        for name in ("numpy", "fastembed", "onnxruntime"):
            self.assertTrue(caps.get(name))

    def test_probe_not_blocked_by_large_worker_diagnostics(self) -> None:
        # A worker that floods its std fds before answering must not dead-lock
        # the probe (the transport uses DEVNULL std streams + a control file,
        # never a pipe that could back-pressure).
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="noisy", env=_simulate_env("noisy"), poll_interval=0.02
        )
        if _DEPS_PRESENT:
            caps = runner.probe()
            self.assertTrue(caps.get("numpy"))
        else:
            with self.assertRaises(ac.AlignmentComputeError):
                runner.probe()

    @unittest.skipIf(_DEPS_PRESENT, "only meaningful when a dep is genuinely missing")
    def test_probe_rejects_when_a_dep_is_missing(self) -> None:
        # In a core-only environment (e.g. CI without the compute stack) the
        # probe must fail clearly rather than assume availability.
        runner = ac.SubprocessAlignmentComputeRunner(task_id="probe")
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner.probe()
        self.assertEqual(ctx.exception.code, ac.COMPONENT_MISSING)

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
            control = Path(tmp) / "control.ndjson"
            request.write_text(
                json.dumps({"protocol": 999, "task_id": "t", "inputs": {}}),
                encoding="utf-8",
            )
            # Control goes to a file (not stdout/stderr), so this also verifies
            # the worker never depends on inherited std streams.
            subprocess.run(
                [sys.executable, "-m", "src.me_finder.alignment_compute_worker",
                 str(request), str(result), str(control)],
                cwd=str(REPO),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "PYTHONPATH": str(REPO)},
            )
            messages = [
                json.loads(line)
                for line in control.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            errors = [m for m in messages if m.get("type") == "error"]
            self.assertTrue(errors, control.read_text(encoding="utf-8"))
            self.assertEqual(errors[0]["code"], ac.PROTOCOL_INCOMPATIBLE)
            self.assertFalse(result.exists())

    @unittest.skipUnless(_DEPS_PRESENT, "reaches the version check after capability check")
    def test_worker_rejects_wrong_algorithm_version(self) -> None:
        # A request declaring a version this worker does not implement must be
        # refused — the worker checks its own algorithm/model versions, so a
        # mismatched independent-component upgrade cannot pass off wrong-version
        # results as current.
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "request.json"
            result = Path(tmp) / "result.json"
            control = Path(tmp) / "control.ndjson"
            good = ac.build_request(
                task_id="t", cache_dir=Path("/models"), embedding_model_id="minilm-l12-v2",
                source_texts=["a"], target_texts=["b"],
                thresholds=AlignmentThresholds(0.56, 0.5, 0.05),
                reusable_sequences=[[], []], folio_candidates=[],
                source_language="zh", target_language="en", reviewed_body_ranges=None,
            )
            good["model_identity"]["alignment_algorithm_version"] = "999"
            request.write_text(json.dumps(good), encoding="utf-8")
            subprocess.run(
                [sys.executable, "-m", "src.me_finder.alignment_compute_worker",
                 str(request), str(result), str(control)],
                cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "PYTHONPATH": str(REPO)},
            )
            messages = [json.loads(x) for x in control.read_text(encoding="utf-8").splitlines() if x.strip()]
            errors = [m for m in messages if m.get("type") == "error"]
            self.assertTrue(errors, control.read_text(encoding="utf-8"))
            self.assertEqual(errors[0]["code"], ac.PROTOCOL_INCOMPATIBLE)
            self.assertFalse(result.exists())

    def test_worker_does_not_depend_on_inherited_std_streams(self) -> None:
        # Simulate a windowed frozen app where sys.stdout/sys.stderr are None
        # (PyInstaller console=False on Windows). The worker must still emit its
        # protocol to the control file without an AttributeError.
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "control.ndjson"
            script = (
                "import sys; sys.stdout=None; sys.stderr=None; "
                "from src.me_finder.alignment_compute_worker import main; "
                f"raise SystemExit(main(['--probe', {str(control)!r}]))"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "PYTHONPATH": str(REPO)},
            )
            self.assertEqual(proc.returncode, 0)
            messages = [json.loads(x) for x in control.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertTrue(any(m.get("type") == "hello" for m in messages))


class RunnerFailureTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX worker can ignore SIGTERM")
    def test_forced_termination_reaps_running_worker(self) -> None:
        import signal
        import time

        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready"
            script = (
                "import signal,sys,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "Path(sys.argv[1]).touch(); time.sleep(60)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(ready)],
                start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "worker never became ready")
                ac._terminate(process)
                self.assertEqual(process.returncode, -signal.SIGKILL)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_failed_temp_cleanup_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("shutil.rmtree", side_effect=PermissionError("request still locked")):
                with self.assertRaisesRegex(PermissionError, "request still locked"):
                    ac._rmtree(Path(tmp))

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

    def test_result_protocol_mismatch_is_refused(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="tamper", env=_simulate_env("tamper_protocol")
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner(["a"], ["b"], **TINY_INPUTS)
        self.assertEqual(ctx.exception.code, ac.PROTOCOL_INCOMPATIBLE)

    def test_result_task_mismatch_is_refused(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="tamper", env=_simulate_env("tamper_task")
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner(["a"], ["b"], **TINY_INPUTS)
        self.assertEqual(ctx.exception.code, ac.RESULT_MISMATCH)

    def test_custom_embedding_provider_is_rejected_not_silently_inprocess(self) -> None:
        runner = ac.SubprocessAlignmentComputeRunner(task_id="prov")
        with self.assertRaises(ac.AlignmentComputeError):
            runner(["a"], ["b"], embedding_provider=lambda *a, **k: None, **TINY_INPUTS)

    def test_worker_start_failure_leaves_no_request_temp_files(self) -> None:
        # The request file holds document text; a launch failure must not leave
        # it (or its temp dir) behind.
        import glob

        pattern = os.path.join(tempfile.gettempdir(), "mefinder-align-compute-*")
        before = set(glob.glob(pattern))
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="nostart", launch_command=["/nonexistent/mefinder-worker-xyz-404"]
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner(["机密正文文本"], ["secret body"], **TINY_INPUTS)
        self.assertEqual(ctx.exception.code, ac.WORKER_START_FAILED)
        self.assertEqual(set(glob.glob(pattern)), before, "temp dir with document text leaked")

    def test_probe_start_failure_leaves_no_temp_files(self) -> None:
        import glob

        pattern = os.path.join(tempfile.gettempdir(), "mefinder-align-probe-*")
        before = set(glob.glob(pattern))
        runner = ac.SubprocessAlignmentComputeRunner(
            task_id="nostart", launch_command=["/nonexistent/mefinder-worker-xyz-404"]
        )
        with self.assertRaises(ac.AlignmentComputeError) as ctx:
            runner.probe()
        self.assertEqual(ctx.exception.code, ac.WORKER_START_FAILED)
        self.assertEqual(set(glob.glob(pattern)), before, "probe temp dir leaked")


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
