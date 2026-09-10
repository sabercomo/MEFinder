"""Repeatable HTTP search, concurrent work, RSS and backend lifecycle baseline.

Run from the repository root; see docs/performance-baseline.md for the protocol.
No production data is opened. All writes live in a fresh temporary directory.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SCENARIOS = ("normal", "export_markdown", "export_epub", "alignment")
PROTOCOL_VERSION = 1


def distribution(values: list[float]) -> dict:
    """Use nearest-rank percentiles; retain small sample sizes explicitly."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("No measured samples")
    return {"n": len(ordered), **{
        label: ordered[math.ceil(len(ordered) * percentile) - 1]
        for label, percentile in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))
    }}


def overlaps(start: float, end: float, events: list[dict]) -> bool:
    """Count only requests intersecting actual server-side work intervals."""
    return any(start < event["end"] and end > event["start"] for event in events)


def result_identity(payload: dict) -> str:
    """Fingerprint results, order and citation anchors independently of timing."""
    keys = ("paragraph_id", "source_file_id", "paragraph_text", "match_start", "match_end",
            "page_match_spans", "page_display", "page_source_type", "match_type")
    value = {"total": payload["total"], "results": [
        {key: row.get(key) for key in keys} for row in payload["results"]
    ]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def request(port: int, route: str, payload: dict | None = None) -> tuple[int, dict]:
    """Measure real localhost HTTP, including serialization and transport."""
    connection = HTTPConnection("127.0.0.1", port, timeout=300)
    try:
        connection.request("GET" if payload is None else "POST", route,
                           body=None if payload is None else json.dumps(payload),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def worker(root: Path) -> None:
    """Host the real runtime with timing probes and its graceful close sequence."""
    # Enforce the baseline's offline boundary even if a model is incomplete.
    def offline(event, args):
        if event == "socket.connect":
            address = args[1]
            if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
                raise RuntimeError(f"Benchmark forbids external network: {address[0]}")

    sys.addaudithook(offline)
    from src.me_finder.app_context import AppContext
    from src.me_finder.application import text_alignment_coordinator
    from src.me_finder.web import ManagedThreadingHTTPServer, make_handler

    events = []
    lock = threading.Lock()

    def timed(name, operation):
        def run(*args, **kwargs):
            started = time.perf_counter()
            (root / "work-started").touch()
            try:
                return operation(*args, **kwargs)
            finally:
                with lock:
                    events.append({"kind": name, "start": started, "end": time.perf_counter()})
        return run

    text_alignment_coordinator.generate_alignment = timed(
        "alignment", text_alignment_coordinator.generate_alignment,
    )
    db = root / "data" / "index.sqlite3"
    handler = make_handler(db, app_context=AppContext.create(root, index_path=db))
    controller = handler.archive_transfer_controller
    controller._export_document_markdown = timed("export_markdown", controller._export_document_markdown)
    controller._export_document_epub = timed("export_epub", controller._export_document_epub)
    server = ManagedThreadingHTTPServer(("127.0.0.1", 0), handler)
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    (root / "ready.json").write_text(json.dumps({"port": server.server_port}))
    try:
        if sys.stdin.readline().strip() != "stop":
            raise RuntimeError("Expected graceful stop command")
    finally:
        handler.begin_shutdown()
        server.shutdown()
        serving.join()
        server.server_close()
        stopped = server.wait_for_handlers(timeout=5)
        handler.wait_for_durable_operations()
        if not stopped or not handler.close_runtime():
            raise RuntimeError("Backend did not close cleanly")
        (root / "events.json").write_text(json.dumps(events))
        if sys.platform == "win32":
            import psutil
            high_water = psutil.Process().memory_info().peak_wset
        else:
            import resource
            high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform != "darwin":
                high_water *= 1024
        (root / "memory.json").write_text(json.dumps({"os_peak_rss_bytes": high_water}))


def run_round(root: Path, fixture: dict, scenario: str, repeats: int, round_id: int) -> dict:
    """Run an isolated process; sample its RSS without charging the driver to it."""
    import psutil

    env = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
           "TOKENIZERS_PARALLELISM": "false", "ME_FINDER_DESKTOP_SHELL": "",
           "ME_FINDER_PREFERENCES": str(root / "config/preferences.json")}
    started = time.perf_counter()
    samples, memory, jobs = [], [], []
    monitor_stop = threading.Event()
    work_stop = threading.Event()
    first_work_done = threading.Event()
    with (root / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(root)],
            stdin=subprocess.PIPE, stdout=log, stderr=log, text=True, env=env, cwd=REPO,
        )
        tracked = psutil.Process(process.pid)

        def monitor():
            while not monitor_stop.is_set():
                try:
                    rss = tracked.memory_info().rss
                    children = tracked.children(recursive=True)
                    tree_rss = rss + sum(child.memory_info().rss for child in children)
                    memory.append({"at": time.perf_counter(), "rss_bytes": rss,
                                   "tree_rss_bytes": tree_rss})
                except psutil.NoSuchProcess:
                    if process.poll() is not None:
                        break
                    # A short-lived child may exit between enumeration and RSS read.
                monitor_stop.wait(.05)

        monitoring = threading.Thread(target=monitor)
        monitoring.start()
        try:
            ready = root / "ready.json"
            deadline = started + 60
            while not ready.exists():
                if process.poll() is not None or time.perf_counter() > deadline:
                    raise RuntimeError(f"Backend startup failed: {root / 'server.log'}")
                time.sleep(.01)
            port = json.loads(ready.read_text())["port"]
            queries = fixture["queries"]
            status, first = request(port, "/api/search", {k: v for k, v in queries[0].items() if k != "id"})
            if status != 200 or not first["results"]:
                raise RuntimeError(f"Backend readiness query failed: {status} {first}")
            startup_ms = (time.perf_counter() - started) * 1000
            identities = {}
            for query in queries:
                status, result = request(port, "/api/search", {k: v for k, v in query.items() if k != "id"})
                if status != 200 or (result["total"] == 0) != (query["id"] == "no_hit"):
                    raise RuntimeError(f"Warmup correctness failed: {query['id']} {status} {result}")
                for row in result["results"]:
                    if row["match_start"] >= row["match_end"] or (row["source_type"] == "pdf" and not row["page_match_spans"]):
                        raise RuntimeError("Fixture search lost character/page anchors")
                identities[query["id"]] = result_identity(result)

            def background():
                while not work_stop.is_set():
                    begin = time.perf_counter()
                    if scenario == "alignment":
                        code, response = request(port, "/api/text-alignments/start", fixture["alignment_request"])
                        if code != 202:
                            raise RuntimeError(f"Alignment start failed: {code} {response}")
                        route = "/api/text-alignments/status?job_id=" + response["job_id"]
                        job_deadline = begin + 300
                        while code == 202:
                            if time.perf_counter() > job_deadline:
                                raise TimeoutError("Alignment exceeded 300 seconds")
                            time.sleep(.05)
                            code, response = request(port, route)
                        if code != 200 or not response.get("ok"):
                            raise RuntimeError(f"Alignment failed: {code} {response}")
                        result = response["result"]
                        evidence = {k: v for k, v in result.items()
                                    if k.endswith("count") or k in ("cached", "embedding_model_id")}
                    else:
                        route = "/api/document/export-" + scenario.removeprefix("export_")
                        code, response = request(port, route, {"source_id": "bench-000"})
                        if code != 200:
                            raise RuntimeError(f"Export failed: {code} {response}")
                        artifact = Path(response["path"])
                        evidence = {"bytes": artifact.stat().st_size,
                                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
                        if not evidence["bytes"]:
                            raise RuntimeError("Empty export artifact")
                        artifact.unlink()
                    jobs.append({"start": begin, "end": time.perf_counter(), "evidence": evidence})
                    first_work_done.set()

            measurement_start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=1) as pool:
                work = pool.submit(background) if scenario != "normal" else None
                try:
                    if work is not None:
                        while not (root / "work-started").exists():
                            if work.done():
                                work.result()
                                raise RuntimeError("Workload ended without executing")
                            if time.perf_counter() - measurement_start > 60:
                                raise TimeoutError("Workload did not start")
                            time.sleep(.01)
                    repetition = 0
                    while repetition < repeats or (work is not None and not first_work_done.is_set()):
                        # Rotate the deterministic order to reduce position bias.
                        ordered = queries[repetition % len(queries):] + queries[:repetition % len(queries)]
                        for query in ordered:
                            if work is not None and work.done():
                                work.result()
                            begin = time.perf_counter()
                            code, result = request(port, "/api/search", {k: v for k, v in query.items() if k != "id"})
                            end = time.perf_counter()
                            identity = result_identity(result) if code == 200 else None
                            samples.append({"query_id": query["id"], "repetition": repetition,
                                            "start": begin, "end": end, "latency_ms": (end - begin) * 1000,
                                            "status": code, "identity": identity,
                                            "correct": identity == identities[query["id"]] if code == 200 else None})
                            # A bounded interactive client, including during fast 503 responses.
                            time.sleep(max(0, .1 - (time.perf_counter() - begin)))
                        repetition += 1
                finally:
                    work_stop.set()
                    if work is not None:
                        work.result()
            measurement_end = time.perf_counter()
            for query in queries:
                code, result = request(port, "/api/search", {k: v for k, v in query.items() if k != "id"})
                if code != 200 or result_identity(result) != identities[query["id"]]:
                    raise RuntimeError(f"Search did not recover unchanged after work: {query['id']}")
            exit_started = time.perf_counter()
            process.stdin.write("stop\n")
            process.stdin.flush()
            process.wait(timeout=30)
            shutdown_ms = (time.perf_counter() - exit_started) * 1000
            if process.returncode:
                raise RuntimeError(f"Backend failed: {root / 'server.log'}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            monitor_stop.set()
            monitoring.join()
    events = json.loads((root / "events.json").read_text())
    for sample in samples:
        sample["overlap"] = overlaps(sample["start"], sample["end"], events)
    measured_memory = [sample for sample in memory if measurement_start <= sample["at"] <= measurement_end]
    # All timestamps share the host monotonic clock; store process-relative seconds.
    for row in samples + events + jobs + memory:
        for key in ("start", "end", "at"):
            if key in row:
                row[key] -= started
    return {"scenario": scenario, "round": round_id, "startup_ms": startup_ms,
            "shutdown_ms": shutdown_ms, "exit_code": process.returncode,
            "peak_rss_bytes": max(sample["rss_bytes"] for sample in memory),
            "peak_tree_rss_bytes": max(sample["tree_rss_bytes"] for sample in memory),
            "work_peak_rss_bytes": max(sample["rss_bytes"] for sample in measured_memory),
            "os_peak_rss_bytes": json.loads((root / "memory.json").read_text())["os_peak_rss_bytes"],
            "samples": samples, "memory_samples": memory, "work_intervals": events,
            "jobs": jobs, "warmup_identities": identities}


def summarize(rounds: list[dict]) -> dict:
    """Expose overlap coverage and correctness alongside timing distributions."""
    summary = {}
    for scenario in SCENARIOS:
        runs = [run for run in rounds if run["scenario"] == scenario]
        if not runs:
            continue
        samples = [sample for run in runs for sample in run["samples"]]
        covered = [sample for sample in samples if scenario == "normal" or sample["overlap"]]
        measured = [sample for sample in covered if sample["status"] == 200]
        failed = [sample for sample in covered if sample["status"] != 200]
        summary[scenario] = {
            "search_ms": distribution([sample["latency_ms"] for sample in measured]) if measured else None,
            "per_query_ms": {key: distribution([sample["latency_ms"] for sample in measured
                                                if sample["query_id"] == key])
                             for key in sorted({sample["query_id"] for sample in measured})},
            "requests": len(samples), "overlapping_requests": sum(sample["overlap"] for sample in samples),
            "covered_query_ids": sorted({sample["query_id"] for sample in covered}),
            "error_rate": len(failed) / len(covered),
            "failure_ms": distribution([sample["latency_ms"] for sample in failed]) if failed else None,
            "http_status_counts": {str(status): sum(sample["status"] == status for sample in covered)
                                   for status in sorted({sample["status"] for sample in covered})},
            "errors": sum(sample["status"] != 200 for sample in samples),
            "identity_mismatches": sum(sample["correct"] is False for sample in samples),
            "startup_ms": distribution([run["startup_ms"] for run in runs]),
            "shutdown_ms": distribution([run["shutdown_ms"] for run in runs]),
            "peak_rss_mib": max(run["peak_rss_bytes"] for run in runs) / 2**20,
            "peak_tree_rss_mib": max(run["peak_tree_rss_bytes"] for run in runs) / 2**20,
            "work_peak_rss_mib": max(run["work_peak_rss_bytes"] for run in runs) / 2**20,
            "os_peak_rss_mib": max(run["os_peak_rss_bytes"] for run in runs) / 2**20,
        }
    return summary


def compare_results(baseline: dict, current: dict) -> dict:
    """Reject unlike protocols/data/environments before comparing measurements."""
    for key in ("protocol_version", "harness_sha256", "configuration", "environment", "model_files"):
        if baseline[key] != current[key]:
            raise ValueError(f"Cannot compare different {key}")
    for key in ("content_sha256", "queries"):
        if baseline["fixture"][key] != current["fixture"][key]:
            raise ValueError(f"Cannot compare different fixture {key}")
    if not baseline["valid"] or not current["valid"]:
        raise ValueError("Cannot compare an invalid run")
    if baseline["rounds"][0]["warmup_identities"] != current["rounds"][0]["warmup_identities"]:
        raise ValueError("Cannot compare changed search results or anchors")
    changes = {}
    for scenario, metrics in current["summary"].items():
        before = baseline["summary"][scenario]
        rows = {}
        for key in metrics["covered_query_ids"]:
            if key not in before["per_query_ms"] or key not in metrics["per_query_ms"]:
                rows[key] = {"comparable": False, "reason": "No successful samples in one run"}
                continue
            rows[key] = {percentile: {
                "before_ms": before["per_query_ms"][key][percentile],
                "after_ms": metrics["per_query_ms"][key][percentile],
                "ratio": metrics["per_query_ms"][key][percentile] / before["per_query_ms"][key][percentile],
            } for percentile in ("p50", "p95", "p99")}
        changes[scenario] = {"queries": rows, "peak_rss_mib_delta": metrics["peak_rss_mib"] - before["peak_rss_mib"],
                             "error_rate_delta": metrics["error_rate"] - before["error_rate"],
                             "os_peak_rss_mib_delta": metrics["os_peak_rss_mib"] - before["os_peak_rss_mib"],
                             "startup_ms_delta": metrics["startup_ms"]["p50"] - before["startup_ms"]["p50"],
                             "shutdown_ms_delta": metrics["shutdown_ms"]["p50"] - before["shutdown_ms"]["p50"]}
    return changes


def main() -> None:
    """Generate a fixture, run fresh processes and publish machine-readable evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--models", type=Path, help="Existing local MiniLM cache root; copied, never modified")
    parser.add_argument("--documents", type=int, default=32)
    parser.add_argument("--paragraphs", type=int, default=2000)
    parser.add_argument("--alignment-paragraphs", type=int, default=320)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    if not args.output or args.rounds < 1 or args.repeats < 1:
        parser.error("--output is required; rounds/repeats must be positive")
    if args.output.exists():
        parser.error("Output already exists; use a new filename to preserve evidence")
    from scripts.performance_fixture import create_fixture
    from src.me_finder.embedding_models import EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL_ID
    from src.me_finder.preferences import save_preferences
    import psutil

    scenarios = list(dict.fromkeys(args.scenario or SCENARIOS))
    model = EMBEDDING_MODELS[DEFAULT_EMBEDDING_MODEL_ID]
    model_files = {}
    model_source = args.models / model.fastembed_cache_dirname if args.models else None
    if "alignment" in scenarios:
        if model_source is None or not model_source.is_dir():
            parser.error("Alignment requires --models pointing to an existing local MiniLM cache")
        for path in sorted(model_source.rglob("*")):
            if path.is_file():
                with path.open("rb") as model_file:
                    model_files[str(path.relative_to(model_source))] = hashlib.file_digest(model_file, "sha256").hexdigest()
    configuration = {"rounds": args.rounds, "repeats": args.repeats, "scenarios": scenarios,
                     "script_folding": True, "model_id": model.id, "rss_interval_ms": 50,
                     "request_interval_ms": 100,
                     "load": "one sequential search client; continuous single background job; minimum repeats and first job completion",
                     "cache": "fresh runtime and vector cache per scenario/round; one search warmup per query"}
    environment = {"platform": platform.platform(), "machine": platform.machine(),
                   "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                   "physical_memory_bytes": psutil.virtual_memory().total,
                   "python": platform.python_version(),
                   "sqlite": sqlite3.sqlite_version,
                   "packages": {name: importlib.metadata.version(name) for name in
                                ("psutil", "numpy", "fastembed", "onnxruntime", "opencc-python-reimplemented")}}
    if sys.platform == "darwin":
        environment["cpu_model"] = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True,
        ).strip()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="mefinder-perf-") as temporary:
        root = Path(temporary)
        fixture_root = root / "fixture"
        fixture = create_fixture(fixture_root, documents=args.documents, paragraphs=args.paragraphs,
                                 alignment_paragraphs=args.alignment_paragraphs)
        runs = []
        for round_id in range(args.rounds):
            # Rotate scenario order across fresh processes to reduce drift bias.
            for scenario in scenarios[round_id % len(scenarios):] + scenarios[:round_id % len(scenarios)]:
                run_root = root / f"{round_id}-{scenario}"
                (run_root / "data").mkdir(parents=True)
                shutil.copy2(fixture_root / "data/index.sqlite3", run_root / "data/index.sqlite3")
                save_preferences({"script_folding": True, "alignment_embedding_model_id": model.id},
                                 run_root / "config/preferences.json")
                if scenario == "alignment":
                    shutil.copytree(model_source, run_root / "components/text-alignment/models" / model.fastembed_cache_dirname)
                print(f"round {round_id + 1}/{args.rounds}: {scenario}", flush=True)
                try:
                    runs.append(run_round(run_root, fixture, scenario, args.repeats, round_id))
                except (OSError, RuntimeError, TimeoutError, ValueError):
                    # Preserve the actual failure evidence before temporary cleanup.
                    if (run_root / "server.log").exists():
                        print((run_root / "server.log").read_text(), file=sys.stderr)
                    raise
                print(json.dumps(summarize([runs[-1]]), ensure_ascii=False), flush=True)
        summary = summarize(runs)
        reference = runs[0]["warmup_identities"]
        # HTTP unavailability is a measured product outcome, not a broken benchmark.
        valid = all(not metrics["identity_mismatches"]
                    and len(metrics["covered_query_ids"]) == len(fixture["queries"]) for metrics in summary.values())
        valid = valid and all(run["warmup_identities"] == reference for run in runs)
        valid = valid and all(
            {sample["query_id"] for sample in run["samples"]
             if run["scenario"] == "normal" or sample["overlap"]} == {query["id"] for query in fixture["queries"]}
            for run in runs
        )
        result = {"protocol_version": PROTOCOL_VERSION, "revision": revision,
                  "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "tracked_dirty": bool(subprocess.check_output(["git", "diff", "HEAD", "--name-only"], cwd=REPO)),
                  "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "configuration": configuration, "environment": environment, "model_files": model_files,
                  "fixture": fixture, "summary": summary, "rounds": runs, "valid": valid}
        if args.compare:
            result["comparison"] = compare_results(json.loads(args.compare.read_text()), result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"wrote {args.output}; valid={valid}", flush=True)
        if not valid:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
