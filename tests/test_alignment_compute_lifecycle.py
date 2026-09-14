"""Exercise runtime shutdown with a live probe or compute subprocess."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts.performance_fixture import create_fixture
from src.me_finder import alignment_compute as ac
from src.me_finder import embedding_runtime
from src.me_finder.app_context import AppContext
from src.me_finder.web_runtime import build_application_runtime


_WORKER = """
import json, os, sys, time
from pathlib import Path
phase, ready, *args = sys.argv[1:]
probing = args[0] == '--probe'
control = Path(args[-1])
if probing and phase == 'compute':
    control.write_text(json.dumps({'type':'hello', 'protocol':1,
        'capabilities':dict.fromkeys(('numpy','fastembed','onnxruntime'), True)})+'\\n')
else:
    Path(ready).write_text(str(os.getpid()))
    time.sleep(60)
"""


class ComputeLifecycleTests(unittest.TestCase):
    def test_runtime_close_reaps_probe_and_compute_before_reporting_success(self):
        self.addCleanup(embedding_runtime.begin_embedding_run)
        for phase in ("probe", "compute"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                create_fixture(root, documents=2, paragraphs=20, alignment_paragraphs=8)
                database = root / "data" / "index.sqlite3"
                ready = root / "ready"
                processes = []
                spawn = ac.SubprocessAlignmentComputeRunner._spawn

                def record_spawn(runner, args):
                    process = spawn(runner, args)
                    processes.append(process)
                    return process

                with mock.patch.object(ac, "default_worker_command", return_value=[
                    sys.executable, "-c", _WORKER, phase, str(ready),
                ]), mock.patch.object(ac.SubprocessAlignmentComputeRunner, "_spawn", record_spawn), mock.patch(
                    "src.me_finder.application.text_alignment_coordinator.model_component_installed",
                    return_value=True,
                ):
                    runtime = build_application_runtime(
                        AppContext.create(root, index_path=database),
                        open_pdf_with_platform=lambda *args: None,
                        open_path_with_default_app=lambda *args: None,
                        open_external_cnki_url=lambda *args: None,
                        open_mineru_token_page=lambda *args: None,
                    )
                    start = runtime.controller_post_routes["/api/text-alignments/start"]
                    controller = start.__self__
                    _, job = start({
                        "document_group_id": "bench-pair",
                        "pivot_source_file_id": "bench-002",
                        "target_source_file_id": "bench-003",
                        "force": True,
                    })
                    try:
                        deadline = time.monotonic() + 5
                        while not ready.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(ready.exists(), "worker did not reach target phase")
                        self.assertFalse(runtime.wait_for_durable_operations(timeout=0))
                        self.assertTrue(runtime.close_runtime(timeout=5))
                        self.assertTrue(all(p.returncode is not None for p in processes))
                        self.assertTrue(all(not Path(p.args[-1]).parent.exists() for p in processes))
                        controller._job_thread.join(5)
                        self.assertEqual(controller.status({"job_id": [job["job_id"]]}),
                                         (200, {"ok": False, "cancelled": True}))
                        with sqlite3.connect(database) as connection:
                            count = connection.execute(
                                "SELECT COUNT(*) FROM alignment_runs WHERE status='completed'"
                            ).fetchone()[0]
                        self.assertEqual(count, 0)
                    finally:
                        runtime.begin_shutdown()
                        controller._job_thread.join(5)
                        for process in processes:
                            if process.poll() is None:
                                process.kill()
                            process.wait(timeout=5)
                        runtime.close_runtime(timeout=5)
