#!/usr/bin/env python3
"""Whole-application memory acceptance for a real alignment task.

``mem_profile_alignment.py`` measures the *task* phase by phase inside one
process. This script answers the other question: what does the **running
application** cost, peak and drop back to, when a user generates an alignment.
So it drives the product entry points and samples from outside:

* backend: ``python -m me_finder serve`` (the same handler the desktop shell
  hosts), pointed at a throwaway copy of a library in an isolated runtime root;
* task: ``POST /api/text-alignments/start`` + status polling, i.e. the exact
  request the reader UI makes, with ``force`` so the compute really runs;
* sampling: an external thread that walks the whole process tree
  (backend + the per-task alignment compute worker it spawns) and tracks RSS
  per pid, so the peak is attributed to whichever process owns it.

Measurement only: no product code is patched, imported into the sampled
process, or modified. Raw JSON (with private ids and the full timeline) goes to
``--output``; ``--summary-output`` holds the publishable summary (counts and
digests only, no ids) for ``reports/``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts import mem_profile_common as common  # noqa: E402

MIB = 1024 * 1024
PROTOCOL = "app-memory-alignment-1"


# --------------------------------------------------------------------------- #
# Plumbing: port, HTTP, isolated runtime root.
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def request(port: int, route: str, payload: dict | None = None) -> Tuple[int, dict]:
    """One real localhost HTTP round trip, like the browser does."""

    connection = HTTPConnection("127.0.0.1", port, timeout=600)
    try:
        connection.request(
            "GET" if payload is None else "POST",
            route,
            body=None if payload is None else json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def page_status(port: int, route: str = "/") -> int:
    """Read a non-JSON route (the shell page) for liveness only."""

    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", route)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _stage_model(models_root: Path, model_id: str, destination: Path) -> int:
    """Materialise one model's cache + installed receipt, never its vector cache.

    Hard links keep the 240 MB weight tree off the clock; the compute worker only
    reads them, and ``document-vectors/`` stays empty so round 1 really embeds.
    """

    from src.me_finder.embedding_models import EMBEDDING_MODELS

    model = EMBEDDING_MODELS[model_id]
    source_dir = models_root / model.fastembed_cache_dirname
    if not source_dir.is_dir():
        raise SystemExit(f"model cache missing for {model_id} in {models_root}")
    staged = 0
    for path in sorted(source_dir.rglob("*")):
        if path.is_file():
            _link_or_copy(
                path, destination / model.fastembed_cache_dirname / path.relative_to(source_dir)
            )
            staged += 1
    receipt = models_root / "installed" / f"{model_id}.json"
    if receipt.is_file():
        _link_or_copy(receipt, destination / "installed" / receipt.name)
    return staged


def prepare_root(
    root: Path,
    *,
    database: Optional[Path],
    synthetic: bool,
    model_id: str,
    models_root: Optional[Path],
) -> dict:
    """Build the runtime root the backend will run against (cwd == runtime root)."""

    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"refusing to reuse a non-empty workdir: {root}")
    (root / "data").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    if synthetic:
        snippet = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from pathlib import Path;"
            "from scripts.performance_fixture import create_fixture;"
            "create_fixture(Path(sys.argv[2]), documents=8, paragraphs=40,"
            " alignment_paragraphs=120, seed=20260910)"
        )
        # Built in a subprocess: index construction must not land in any
        # measured process's memory history.
        subprocess.run([sys.executable, "-c", snippet, str(REPO), str(root)], check=True)
        workload = {
            "kind": "synthetic",
            "seed": 20260910,
            "pair": "synthetic bench pair (10x120 segments)",
        }
    else:
        assert database is not None
        shutil.copy2(database, root / "data" / "index.sqlite3")
        copied = root / "data" / "index.sqlite3"
        workload = {
            "kind": "library-snapshot-copy",
            "database_bytes": copied.stat().st_size,
            "database_sha256": common.sha256_file(copied),
            "source_sha256": common.sha256_file(database),
        }
    from src.me_finder.preferences import save_preferences

    save_preferences(
        {"script_folding": True, "alignment_embedding_model_id": model_id},
        root / "config" / "preferences.json",
    )
    staged = 0
    if models_root is not None:
        staged = _stage_model(
            Path(models_root), model_id, root / "components/text-alignment/models"
        )
    workload["model_files_staged"] = staged
    return workload


# --------------------------------------------------------------------------- #
# External process-tree sampler.
# --------------------------------------------------------------------------- #
class TreeSampler:
    """Sample RSS of the backend process and of every descendant, per pid."""

    def __init__(self, root_pid: int, *, interval_ms: int = 20) -> None:
        import psutil

        self._psutil = psutil
        self.root_pid = root_pid
        self.interval_s = interval_ms / 1000.0
        self.origin = time.monotonic()
        self.samples: List[dict] = []
        self.pids: Dict[int, dict] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.enumeration_errors = 0

    # -- collection --------------------------------------------------------

    def snapshot(self) -> dict:
        psutil = self._psutil
        try:
            backend = psutil.Process(self.root_pid)
            rows = [backend] + backend.children(recursive=True)
        except psutil.NoSuchProcess:
            self.enumeration_errors += 1
            return {"t": time.monotonic() - self.origin, "backend_rss": 0, "worker_rss": 0,
                    "tree_rss": 0, "workers": [], "backend_alive": False}
        observed = []
        for process in rows:
            try:
                rss = process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.enumeration_errors += 1
                continue
            observed.append((process.pid, rss, process.name()[:40]))
        now = time.monotonic() - self.origin
        backend_rss = 0
        worker_rss = 0
        for pid, rss, name in observed:
            entry = self.pids.setdefault(
                pid, {"pid": pid, "name": name, "first_seen": now, "last_seen": now, "peak_rss": 0}
            )
            entry["last_seen"] = now
            entry["peak_rss"] = max(entry["peak_rss"], rss)
            if pid == self.root_pid:
                backend_rss = rss
            else:
                worker_rss += rss
        return {
            "t": round(now, 4),
            "backend_rss": backend_rss,
            "worker_rss": worker_rss,
            "tree_rss": backend_rss + worker_rss,
            "workers": [[pid, rss] for pid, rss, _name in observed if pid != self.root_pid],
            "backend_alive": bool(backend_rss),
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            sample = self.snapshot()
            with self._lock:
                self.samples.append(sample)
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="app-tree-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # -- derived views -----------------------------------------------------

    def window(self, started: float, ended: float) -> dict:
        """Peak attribution inside a wall-clock window (seconds since origin)."""

        rows = [sample for sample in self.samples if started <= sample["t"] <= ended]
        rows.append({"tree_rss": 0, "backend_rss": 0, "worker_rss": 0})
        return {
            "peak_tree_rss": max(row["tree_rss"] for row in rows),
            "peak_backend_rss": max(row["backend_rss"] for row in rows),
            "peak_worker_rss": max(row["worker_rss"] for row in rows),
        }

    def workers_within(self, started: float, ended: float) -> List[dict]:
        return [
            entry for entry in self.pids.values()
            if entry["pid"] != self.root_pid
            and entry["last_seen"] >= started
            and entry["first_seen"] <= ended
        ]

    def settle(self, *, threshold: int = MIB, quiescent_s: float = 2.0,
               max_wait_s: float = 120.0) -> dict:
        """Wait for the tree RSS to hold still; touches nothing (no gc, no unload)."""

        started = time.monotonic()
        baseline: Optional[dict] = None
        last_change = started
        while time.monotonic() - started < max_wait_s:
            sample = self.snapshot()
            now = time.monotonic()
            if baseline is None or abs(sample["tree_rss"] - baseline["tree_rss"]) > threshold:
                baseline = sample
                last_change = now
            elif now - last_change >= quiescent_s:
                return {**baseline, "waited_s": round(now - started, 2)}
            time.sleep(0.1)
        return {**(baseline or {}), "waited_s": round(time.monotonic() - started, 2),
                "timed_out": True}


# --------------------------------------------------------------------------- #
# System-level pressure context (so "peak" is read against the machine).
# --------------------------------------------------------------------------- #
def system_memory() -> dict:
    import psutil

    virtual = psutil.virtual_memory()
    info = {
        "total_bytes": virtual.total,
        "available_bytes": virtual.available,
        "used_bytes": virtual.used,
        "compressed_bytes": getattr(virtual, "compressed", 0),
        "swap_used_bytes": psutil.swap_memory().used,
    }
    if sys.platform == "darwin":
        try:
            raw = subprocess.check_output(["sysctl", "-n", "vm.swapusage"], text=True).strip()
            info["vm_swapusage"] = raw[:200]
        except (subprocess.SubprocessError, OSError):
            info["vm_swapusage"] = None
    return info


# --------------------------------------------------------------------------- #
# The acceptance run.
# --------------------------------------------------------------------------- #
def run_alignment(port: int, payload: dict, *, timeout_s: float) -> dict:
    """Start one background alignment and poll it the way the reader UI does."""

    started = time.monotonic()
    status, response = request(port, "/api/text-alignments/start", payload)
    if status != 202 or "job_id" not in response:
        raise RuntimeError(f"alignment start failed: {status} {str(response)[:200]}")
    route = "/api/text-alignments/status?job_id=" + str(response["job_id"])
    while True:
        status, response = request(port, route)
        if status == 200:
            break
        if status != 202:
            raise RuntimeError(f"alignment failed: {status} {str(response)[:200]}")
        if time.monotonic() - started > timeout_s:
            raise TimeoutError(f"alignment exceeded {timeout_s}s")
        time.sleep(0.2)
    return {"seconds": round(time.monotonic() - started, 2), "response": response}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True, help="isolated runtime root to create")
    parser.add_argument("--db", type=Path, help="index.sqlite3 to copy (omit with --synthetic)")
    parser.add_argument("--synthetic", action="store_true", help="build a tiny fixture instead")
    parser.add_argument("--manifest", type=Path, help="snapshot manifest.json holding alignment_request")
    parser.add_argument("--group", help="--manifest alternative: document_group_id")
    parser.add_argument("--pivot")
    parser.add_argument("--target")
    parser.add_argument("--pair-a-title", default="pair A")
    parser.add_argument("--cross-series", type=Path, help="JSON array of extra real pairs, each run once (cold)")
    parser.add_argument("--model-id", default="minilm-l12-v2")
    parser.add_argument("--models", type=Path, help="text-alignment models root")
    parser.add_argument("--warmup-query", default="社会", help="search run once before the idle baseline")
    parser.add_argument("--rounds", type=int, default=2, help="repeats of pair A (round 1 cold, later warm)")
    parser.add_argument("--interval-ms", type=int, default=20)
    parser.add_argument("--task-timeout-s", type=float, default=600.0)
    parser.add_argument("--output", type=Path, required=True, help="raw JSON (private: ids + timeline)")
    parser.add_argument("--summary-output", type=Path, required=True, help="publishable JSON summary")
    parser.add_argument("--keep-root", action="store_true")
    arguments = parser.parse_args()

    root = arguments.workdir.resolve()
    if arguments.synthetic:
        workload = prepare_root(root, database=None, synthetic=True,
                                model_id=arguments.model_id, models_root=arguments.models)
        request_a = {"document_group_id": "bench-pair", "pivot_source_file_id": "bench-008",
                     "target_source_file_id": "bench-009", "force": True}
    else:
        if arguments.db is None:
            parser.error("--db is required unless --synthetic")
        workload = prepare_root(root, database=arguments.db, synthetic=False,
                                model_id=arguments.model_id, models_root=arguments.models)
        if arguments.manifest is not None:
            payload = json.loads(arguments.manifest.read_text())["alignment_request"]
        else:
            if not (arguments.group and arguments.pivot and arguments.target):
                parser.error("--manifest, or --group/--pivot/--target, is required")
            payload = {"document_group_id": arguments.group, "pivot_source_file_id": arguments.pivot,
                       "target_source_file_id": arguments.target, "force": True}
        request_a = dict(payload)
    workload["pair_a_sha256"] = common.sha256_bytes(
        json.dumps({k: request_a[k] for k in ("document_group_id", "pivot_source_file_id",
                                              "target_source_file_id")}, sort_keys=True).encode()
    )[:16]
    workload["model_id"] = arguments.model_id
    workload["force"] = True
    cross_series = []
    if arguments.cross_series is not None:
        cross_series = json.loads(arguments.cross_series.read_text(encoding="utf-8"))
        workload["cross_series_sha256"] = [
            common.sha256_bytes(
                json.dumps({k: entry[k] for k in ("document_group_id", "pivot_source_file_id",
                                                   "target_source_file_id")}, sort_keys=True).encode()
            )[:16] for entry in cross_series
        ]
        workload["cross_series_titles"] = [str(entry.get("title", ""))[:80] for entry in cross_series]

    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO / "src"),
        "PYTHONUTF8": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "ME_FINDER_PREFERENCES": str(root / "config/preferences.json"),
    }
    log_path = root / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "me_finder", "serve", "--host", "127.0.0.1",
             "--port", str(port), "--index", str(root / "data/index.sqlite3")],
            cwd=str(root), stdout=log, stderr=log, env=env, start_new_session=True,
        )
        sampler = TreeSampler(server.pid, interval_ms=arguments.interval_ms)
        sampler.start()
        rounds: List[dict] = []
        outcome = {"status": "ok"}
        warmup_ms = None
        try:
            waited = 0.0
            while True:
                try:
                    if page_status(port) == 200:
                        break
                except OSError:
                    pass
                if server.poll() is not None or waited > 120:
                    raise RuntimeError(f"backend did not start (exit={server.poll()}); see {log_path}")
                time.sleep(0.5)
                waited += 0.5
            began = time.monotonic()
            status, searched = request(
                port, "/api/search",
                {"query": arguments.warmup_query, "mode": "auto", "limit": 10, "source_type": "all"},
            )
            if status != 200:
                raise RuntimeError(f"warmup search failed: {status} {str(searched)[:200]}")
            warmup_ms = round((time.monotonic() - began) * 1000, 1)
            system_before = system_memory()
            idle = sampler.settle(max_wait_s=30)
            for index in range(arguments.rounds):
                label = f"{arguments.pair_a_title} round {index + 1} " + (
                    "cold (embeddings computed)" if index == 0 else "warm (vector cache reuse)"
                )
                begin = sampler.snapshot()["t"]
                job = run_alignment(port, request_a, timeout_s=arguments.task_timeout_s)
                end = sampler.snapshot()["t"]
                rounds.append(_round(sampler, label, job, begin, end))
            for index, extra in enumerate(cross_series):
                payload = {key: extra[key] for key in
                           ("document_group_id", "pivot_source_file_id", "target_source_file_id")}
                payload["force"] = True
                begin = sampler.snapshot()["t"]
                job = run_alignment(port, payload, timeout_s=arguments.task_timeout_s)
                end = sampler.snapshot()["t"]
                rounds.append(_round(sampler, f"{extra.get('title', 'extra')} "
                                   f"cross-task {index + 1} cold (embeddings computed)", job, begin, end))
        except (RuntimeError, TimeoutError) as exc:
            outcome = {"status": "failed", "error": str(exc)[:200]}
        finally:
            tail = sampler.settle(max_wait_s=90)
            live_workers = sampler.snapshot()["workers"]
            system_after = system_memory()
            server.terminate()
            try:
                exit_code = server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
                exit_code = server.wait(timeout=30)
            sampler.stop()
    resident = [round((row["resident_tree_rss_bytes"] or 0) / MIB, 1) for row in rounds]
    raw = {
        "protocol_version": PROTOCOL,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "outcome": outcome,
        "warmup_search_ms": warmup_ms,
        "environment": {**common.environment_info(), **common.git_metadata()},
        "workload": {**workload, "rounds": len(rounds)},
        "configuration": {"interval_ms": arguments.interval_ms, "rss_metric": "psutil rss (bytes)",
                          "driver": "me_finder serve + /api/text-alignments",
                          "settle": "1 MiB quiescent for 2 s, no gc.collect, no cache eviction"},
        "idle": {"tree_rss_bytes": idle.get("tree_rss"), "backend_rss_bytes": idle.get("backend_rss"),
                 "waited_s": idle.get("waited_s")},
        "rounds": rounds,
        "final": {"resident_after_last_round": tail,
                  "live_worker_processes_after_settle": [[pid, rss] for pid, rss in live_workers],
                  "server_exit_code": exit_code, "enumeration_errors": sampler.enumeration_errors},
        "system": {"before": system_before, "after": system_after},
        "timeline": sampler.samples,
        "per_pid": [
            {**{key: (round(value, 3) if isinstance(value, float) else value)
                for key, value in entry.items() if key != "name"}, "name": entry["name"]}
            for entry in sorted(sampler.pids.values(), key=lambda item: item["first_seen"])
        ],
    }
    private_path = arguments.output
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    summary = {key: raw[key] for key in ("protocol_version", "measured_at", "outcome", "environment",
                                         "workload", "configuration", "idle", "system",
                                         "warmup_search_ms")}
    summary["rounds"] = [{key: value for key, value in row.items() if key != "response"} for row in rounds]
    summary["final"] = {"resident_after_last_round_mib": _mib(tail.get("tree_rss")),
                        "backend_mib": _mib(tail.get("backend_rss")),
                        "worker_mib": _mib(tail.get("worker_rss")),
                        "live_worker_processes_after_settle": raw["final"]["live_worker_processes_after_settle"],
                        "server_exit_code": exit_code}
    summary["resident_tree_mib_series"] = resident
    summary["resident_slope_mib_per_round"] = (
        None if len(rounds) < 3 else _mib(
            common.linear_slope(range(1, len(rounds) + 1),
                                [int(value * MIB) for value in resident]) or 0
        )
    )
    common.write_report(summary, arguments.summary_output)
    print(json.dumps({"outcome": outcome, "rounds": summary["rounds"],
                      "final": summary["final"]}, ensure_ascii=False, indent=2))
    if not arguments.keep_root:
        shutil.rmtree(root, ignore_errors=True)


def _mib(value: Optional[int]) -> Optional[float]:
    return None if value is None else round(value / MIB, 1)


def _round(sampler: TreeSampler, label: str, job: dict, begin: float, end: float) -> dict:
    result = job["response"].get("result") or {}
    counts = {key: result.get(key) for key in
              ("status", "pivot_segment_count", "target_segment_count", "alignment_link_count",
               "accepted_link_count", "rejected_link_count", "unmatched_link_count",
               "heading_anchor_count", "embedding_model_id", "cached") if key in result}
    window = sampler.window(begin, end)
    settled = sampler.settle()
    workers = sampler.workers_within(begin, end)
    return {
        "label": label,
        "task_seconds": job["seconds"],
        "counts": counts,
        "peak_tree_rss_bytes": window["peak_tree_rss"],
        "peak_backend_rss_bytes": window["peak_backend_rss"],
        "peak_worker_rss_bytes": window["peak_worker_rss"],
        "resident_tree_rss_bytes": settled.get("tree_rss"),
        "resident_backend_rss_bytes": settled.get("backend_rss"),
        "resident_worker_rss_bytes": settled.get("worker_rss"),
        "settle_waited_s": settled.get("waited_s"),
        "settle_timed_out": bool(settled.get("timed_out")),
        "drop_from_peak_tree_bytes": (window["peak_tree_rss"] - (settled.get("tree_rss") or 0)),
        "worker_processes": [
            {"pid": entry["pid"], "peak_rss_bytes": entry["peak_rss"],
             "alive_s": round(entry["last_seen"] - entry["first_seen"], 2)}
            for entry in workers
        ],
        "response": job["response"],
    }


if __name__ == "__main__":
    main()
