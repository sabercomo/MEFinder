"""Backend-as-an-independent-process acceptance.

The native host prototype (and any future UI process) consumes the backend as
a standalone subprocess over 127.0.0.1 HTTP. This test drives the real worker
entry from ``scripts/bench_responsiveness.py`` — the same one the performance
protocol uses — through search, alignment targets, a real offline MiniLM
generation and the graceful stop, asserting exit code 0.

Run markers: requires the local MiniLM model cache (like the perf protocol);
without it the alignment leg is skipped but search/stop still assert.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import bench_responsiveness  # noqa: E402
from scripts.performance_fixture import create_fixture  # noqa: E402


def post_json(port: int, route: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.status, json.loads(response.read())


def get_json(port: int, route: str) -> tuple[int, dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=60) as response:
        return response.status, json.loads(response.read())


class BackendStandaloneProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        create_fixture(self.root, documents=2, paragraphs=12, alignment_paragraphs=8)
        # create_fixture's bilingual pair: bench-002 (pivot) / bench-003 (target).
        self.pivot, self.target = "bench-002", "bench-003"
        self._link_model_cache()

    def _link_model_cache(self) -> None:
        from src.me_finder.embedding_models import (
            EMBEDDING_MODELS,
            DEFAULT_EMBEDDING_MODEL_ID,
        )

        config = EMBEDDING_MODELS[DEFAULT_EMBEDDING_MODEL_ID]
        candidates = [
            Path.home()
            / "Library/Application Support/MEFinder/runtime/components/text-alignment/models",
            REPO / "components/text-alignment/models",
        ]
        target_root = self.root / "components/text-alignment"
        target_root.mkdir(parents=True, exist_ok=True)
        for candidate in candidates:
            if (candidate / config.fastembed_cache_dirname).is_dir():
                shutil.copytree(candidate / config.fastembed_cache_dirname,
                                target_root / "models" / config.fastembed_cache_dirname)
                self.model_available = True
                return
        self.model_available = False

    def test_search_align_and_graceful_exit(self) -> None:
        env = {
            **os.environ,
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "ME_FINDER_DESKTOP_SHELL": "",
            "ME_FINDER_PREFERENCES": str(self.root / "config/preferences.json"),
        }
        process = subprocess.Popen(
            [sys.executable, str(REPO / "scripts/bench_responsiveness.py"),
             "--worker", str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=REPO,
        )
        try:
            ready = self.root / "ready.json"
            deadline = time.monotonic() + 60
            while not ready.exists():
                if process.poll() is not None:
                    self.fail(f"backend died: {process.stderr.read()[:2000]}")
                if time.monotonic() > deadline:
                    process.kill()
                    self.fail("backend never became ready")
                time.sleep(0.02)
            port = json.loads(ready.read_text())["port"]

            # 搜索:exact 与 auto 都带页内锚点地返回。
            status, exact = post_json(port, "/api/search", {"query": "青铜", "mode": "exact", "limit": 5})
            self.assertEqual(status, 200)
            self.assertGreaterEqual(exact["total"], 1)
            status, auto = post_json(port, "/api/search", {"query": "社会", "mode": "auto", "limit": 5})
            self.assertEqual(status, 200)
            self.assertGreaterEqual(auto["total"], 1)
            for result in (*exact["results"], *auto["results"]):
                self.assertLess(result["match_start"], result["match_end"])
                if result["source_type"] == "pdf":
                    self.assertTrue(result["page_match_spans"])

            # 对照目标列表:读取已有成果,不依赖任何窗口。
            status, targets = get_json(
                port, f"/api/text-alignments/targets?source_id={self.pivot}"
            )
            self.assertEqual(status, 200, targets)
            self.assertIn("targets", targets)

            if self.model_available:
                # 对齐:真实 MiniLM 离线生成一个极小 pair。
                status, start = post_json(
                    port,
                    "/api/text-alignments/start",
                    {"document_group_id": "bench-pair",
                     "pivot_source_file_id": self.pivot,
                     "target_source_file_id": self.target,
                     "force": True},
                )
                self.assertEqual(status, 202, start)
                job_route = f"/api/text-alignments/status?job_id={start['job_id']}"
                deadline = time.monotonic() + 240
                status, job = get_json(port, job_route)
                while status == 202 and time.monotonic() < deadline:
                    time.sleep(0.5)
                    status, job = get_json(port, job_route)
                self.assertEqual(status, 200, job)
                self.assertTrue(job.get("ok"), job)

                with sqlite3.connect(self.root / "data/index.sqlite3") as connection:
                    links = connection.execute(
                        "SELECT COUNT(*) FROM alignment_links"
                    ).fetchone()[0]
                self.assertGreater(links, 0)

            # 退出:优雅停止,进程必须以 0 退出。
            process.stdin.write("stop\n")
            process.stdin.flush()
            exit_code = process.wait(timeout=30)
            self.assertEqual(exit_code, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
