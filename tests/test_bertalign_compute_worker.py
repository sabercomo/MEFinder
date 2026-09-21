"""Offline tests for the Bertalign out-of-process compute seam.

These do not need the Bertalign runtime: they exercise request identity and the
worker's capability gate by launching the worker with the *current* interpreter
(which lacks torch/faiss in CI), so a compute/probe reports COMPONENT_MISSING
rather than silently falling back.
"""

from __future__ import annotations

import sys
import tempfile
from unittest import mock
import unittest
from importlib.util import find_spec
from pathlib import Path

from src.me_finder.alignment_compute import COMPONENT_MISSING, AlignmentComputeError
from src.me_finder.bertalign_backend import BertalignParams
from src.me_finder.bertalign_compute import (
    BERTALIGN_REQUIRED,
    BertalignSubprocessComputeRunner,
    build_bertalign_request,
)


class BertalignRequestIdentityTests(unittest.TestCase):
    def _request(self, **overrides):
        base = dict(
            task_id="t1",
            model_dir=Path("/models/labse"),
            source_texts=["a", "b"],
            target_texts=["x", "y"],
            reviewed_body_ranges={"pivot": [0, 2], "target": [0, 2]},
            source_language="zh",
            target_language="de",
            params=BertalignParams(),
        )
        base.update(overrides)
        return build_bertalign_request(**base)

    def test_identity_is_stable_and_params_sensitive(self) -> None:
        a = self._request()
        b = self._request()
        self.assertEqual(a["input_identity"], b["input_identity"])
        # A parameter change changes the input identity (no stale reuse across
        # a subprocess boundary).
        c = self._request(params=BertalignParams(max_align=6))
        self.assertNotEqual(a["input_identity"], c["input_identity"])
        self.assertEqual(a["backend"], "bertalign-labse-two-pass")
        self.assertEqual(set(a["inputs"]["params"]), {
            "max_align", "top_k", "win", "skip", "margin", "len_penalty"
        })


class BertalignWorkerCapabilityTests(unittest.TestCase):
    def _runner(self):
        # Launch the worker with THIS interpreter; in CI it lacks the Bertalign
        # stack, so the capability gate must trip rather than fall back.
        return BertalignSubprocessComputeRunner(
            task_id="cap",
            launch_command=[sys.executable, "-m", "src.me_finder.alignment_compute_worker", "--bertalign"],
        )

    @unittest.skipIf(
        all(find_spec(n) for n in BERTALIGN_REQUIRED),
        "Bertalign stack present in this interpreter; capability-missing path not exercised here",
    )
    def test_missing_stack_reports_component_missing(self) -> None:
        with self.assertRaises(AlignmentComputeError) as ctx:
            self._runner().probe()
        self.assertEqual(ctx.exception.code, COMPONENT_MISSING)


class BertalignWorkerLifecycleTests(unittest.TestCase):
    def test_cancel_reaps_worker_and_removes_document_temp_files(self):
        import subprocess
        from src.me_finder.alignment_compute import CANCELLED
        processes = []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        before = set(Path(tempfile.gettempdir()).glob("mefinder-bertalign-compute-*"))
        runner = BertalignSubprocessComputeRunner(task_id="cancel", cancel_check=lambda: True,
            launch_command=[sys.executable, "-c", "import time; time.sleep(30)"])
        with mock.patch("src.me_finder.bertalign_compute.subprocess.Popen", side_effect=spawn):
            with self.assertRaises(AlignmentComputeError) as error:
                runner(["private source"], ["private target"], model_dir=Path("unused"))
        self.assertEqual(error.exception.code, CANCELLED)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("mefinder-bertalign-compute-*")), before)

    def test_result_task_and_identity_mismatch_refuse_publication(self):
        import json
        from src.me_finder.alignment_compute import RESULT_MISMATCH
        request = BertalignRequestIdentityTests()._request()
        runner = BertalignSubprocessComputeRunner(task_id="t1")
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "result.json"
            for key, value in (("task_id", "other-task"), ("input_identity", "other-input")):
                payload = {"protocol":1, "task_id":"t1", "input_identity":request["input_identity"], "links":[], "anchors":[]}
                payload[key] = value
                result.write_text(json.dumps(payload))
                with self.assertRaises(AlignmentComputeError) as error:
                    runner._consume_result({"input_identity":request["input_identity"]}, request, result)
                self.assertEqual(error.exception.code, RESULT_MISMATCH)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
