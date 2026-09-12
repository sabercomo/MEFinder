"""Phased-memory profile of real alignment generation, in one process.

Drives the production ``TextAlignmentCoordinator`` in-process (no HTTP server)
so phase boundaries can be instrumented from a diagnostic script without
touching product code. The same task runs for ``--rounds`` consecutive rounds
in one process, which separates first-run growth (model load, vector cache,
allocator warm-up) from genuine per-round growth of stable resident memory.

Phases per round: ``prep`` (segment sets + folio candidates, write tx 1),
``embed`` (vector cache load + ONNX inference, incl. ``model_load``),
``post_embed_prep`` (folio verification, body bounds, heading anchors),
``compute`` (banded monotonic DP + note overrides), ``publish`` (write tx 2),
plus the derived ``finalize`` gap before the task returns.

Default mode keeps the natural product lifecycle: the ``document-vectors``
``.npy`` cache persists across rounds, so round 1 computes embeddings and
later rounds reuse them — exactly what a force-regenerate does in the app.
``--fresh-vectors-per-round`` is an explicit single-variable control that
deletes the vector cache between rounds; its output is tagged and must never
be mixed with the default series when reasoning about per-round growth.

This script never reads or writes product data roots by itself: pass an
explicit ``--db`` snapshot or ``--synthetic`` scale. Output JSON carries
counts and hashes only (a length guard rejects long strings).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_FIXTURE_BUILD_SNIPPET = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from scripts.performance_fixture import create_fixture
create_fixture(
    Path(sys.argv[2]),
    documents=int(sys.argv[3]),
    paragraphs=int(sys.argv[4]),
    alignment_paragraphs=int(sys.argv[5]),
    seed=int(sys.argv[6]),
)
"""


import fastembed  # noqa: E402

from scripts import mem_profile_common as common  # noqa: E402
from src.me_finder import semantic_alignment as semantic_alignment_module  # noqa: E402
from src.me_finder import text_alignment as text_alignment_module  # noqa: E402
from src.me_finder.app_context import AppPaths  # noqa: E402
from src.me_finder.application import (  # noqa: E402
    text_alignment_coordinator as coordinator_module,
)
from src.me_finder.application.index_runtime import IndexRuntime  # noqa: E402
from src.me_finder.application.text_alignment_coordinator import (  # noqa: E402
    TextAlignmentCoordinator,
)
from src.me_finder.database import replace_source_in_database  # noqa: E402
from src.me_finder.embedding_models import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL_ID,
    EMBEDDING_MODELS,
)
from src.me_finder.preferences import save_preferences  # noqa: E402
from src.me_finder.search import SearchEngine  # noqa: E402


class _DurableOperations:
    @contextmanager
    def operation(self):
        yield


def _default_models_root() -> Path | None:
    candidates = [
        Path.home()
        / "Library/Application Support/MEFinder/runtime"
        / "components/text-alignment/models",
        REPO / "components/text-alignment/models",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _copy_model_cache(models_root: Path, model_id: str, destination: Path) -> None:
    """Copy one model's fastembed cache plus its installed receipt."""

    model = EMBEDDING_MODELS[model_id]
    copied = 0
    source_dir = models_root / model.fastembed_cache_dirname
    if source_dir.is_dir():
        shutil.copytree(
            source_dir, destination / model.fastembed_cache_dirname, symlinks=True
        )
        copied += 1
    receipt = models_root / "installed" / f"{model_id}.json"
    if receipt.is_file():
        (destination / "installed").mkdir(parents=True, exist_ok=True)
        shutil.copy2(receipt, destination / "installed" / receipt.name)
        copied += 1
    if not copied:
        raise FileNotFoundError(
            f"model cache for {model_id} not found in {models_root}"
        )


class _Instrumentation:
    """Module-attribute patches that turn product call sites into phases."""

    def __init__(self, probe: common.MemoryProbe, provider, forced_batch=None) -> None:
        self.probe = probe
        self.forced_batch = forced_batch
        if provider is not None:
            # Stub providers replace embed_text_sequences entirely; give their
            # single call the same "embed" phase name for derived-phase math.
            real_provider = provider

            def probed_provider(texts, cache_dir):
                with probe.phase("embed"):
                    return real_provider(texts, cache_dir)

            provider = probed_provider
        self.provider = provider
        self.last_result: dict | None = None
        self._patches: list[tuple[object, str, object]] = []

    def _install(self, module, attribute: str, wrapper) -> None:
        real = getattr(module, attribute)
        setattr(module, attribute, wrapper)
        self._patches.append((module, attribute, real))

    def __enter__(self) -> "_Instrumentation":
        probe = self.probe

        def wrap_phase(module, attribute: str, phase_name: str) -> None:
            real = getattr(module, attribute)

            def wrapper(*args, **kwargs):
                with probe.phase(phase_name):
                    return real(*args, **kwargs)

            self._install(module, attribute, wrapper)

        real_generate = coordinator_module.generate_alignment

        def task_wrapper(db_path, group, pivot, target, **kwargs):
            if self.provider is not None:
                kwargs["embedding_provider"] = self.provider
            with probe.phase("task_total"):
                result = real_generate(db_path, group, pivot, target, **kwargs)
            self.last_result = result
            return result

        self._install(coordinator_module, "generate_alignment", task_wrapper)
        wrap_phase(
            text_alignment_module, "embed_text_sequences", "embed"
        )
        wrap_phase(semantic_alignment_module, "embed_texts", "embed_compute")
        wrap_phase(
            text_alignment_module, "align_semantic_sequences", "compute"
        )
        wrap_phase(
            text_alignment_module,
            "_generate_alignment_on_connection",
            "publish",
        )

        real_text_embedding = fastembed.TextEmbedding
        forced_batch = self.forced_batch

        class _ProbedTextEmbedding:
            def __init__(self, *args, **kwargs):
                with probe.phase("model_load"):
                    self._impl = real_text_embedding(*args, **kwargs)

            def __getattr__(self, name):
                attribute = getattr(self._impl, name)
                if name in ("embed", "query_embed") and callable(attribute):
                    def bounded(texts, batch_size=None, _call=attribute):
                        if forced_batch is not None:
                            batch_size = forced_batch
                        return _call(texts, batch_size=batch_size)

                    return bounded
                return attribute

            def __repr__(self) -> str:  # diagnostics aid only
                return f"_ProbedTextEmbedding({self._impl!r})"

        self._install(fastembed, "TextEmbedding", _ProbedTextEmbedding)
        return self

    def __exit__(self, *_exc) -> None:
        for module, attribute, real in reversed(self._patches):
            setattr(module, attribute, real)
        self._patches.clear()


def _fake_provider(texts, _cache_dir):
    import numpy as np

    return np.ones((len(texts), 4), dtype=np.float32)


def _build_root(arguments, root: Path) -> dict:
    """Materialise the runtime root: database, preferences, model cache."""

    (root / "data").mkdir(parents=True, exist_ok=True)
    if arguments.db is not None:
        database = Path(arguments.db).resolve()
        shutil.copy2(database, root / "data/index.sqlite3")
        workload = {
            "kind": "real-snapshot",
            "database_bytes": (root / "data/index.sqlite3").stat().st_size,
            "group": arguments.group,
            "pivot": arguments.pivot,
            "target": arguments.target,
        }
    else:
        # Build the fixture in a subprocess so the large index dict never
        # enters this process's memory history (ru_maxrss is monotonic and
        # would otherwise attribute construction peaks to the measured task).
        subprocess.run(
            [
                sys.executable,
                "-c",
                _FIXTURE_BUILD_SNIPPET,
                str(REPO),
                str(root),
                str(arguments.synthetic_documents),
                str(arguments.synthetic_paragraphs),
                str(arguments.synthetic_alignment_paragraphs),
                str(arguments.synthetic_seed),
            ],
            check=True,
        )
        workload = {
            "kind": "synthetic",
            "documents": arguments.synthetic_documents,
            "search_paragraphs_per_document": arguments.synthetic_paragraphs,
            "alignment_paragraphs": arguments.synthetic_alignment_paragraphs,
            "seed": arguments.synthetic_seed,
            "group": "bench-pair",
            "pivot": f"bench-{arguments.synthetic_documents:03d}",
            "target": f"bench-{arguments.synthetic_documents + 1:03d}",
        }
    save_preferences(
        {
            "script_folding": True,
            "alignment_embedding_model_id": arguments.model_id,
        },
        root / "config/preferences.json",
    )
    if arguments.provider == "real":
        models_root = arguments.models or _default_models_root()
        if models_root is None or not models_root.is_dir():
            raise SystemExit(
                "real provider needs --models (a text-alignment models root)"
            )
        _copy_model_cache(
            models_root,
            arguments.model_id,
            root / "components/text-alignment/models",
        )
    return workload


def _extract_counts(result: dict) -> dict:
    keys = (
        "status",
        "reused",
        "pivot_segment_count",
        "target_segment_count",
        "alignment_link_count",
        "accepted_link_count",
        "rejected_link_count",
        "unmatched_link_count",
        "heading_anchor_count",
        "folio_anchor_count",
    )
    return {key: result.get(key) for key in keys}


def _derived_for(entry: dict) -> dict:
    """Gap phases between instrumented phases (prep / post-embed / finalize)."""

    phases = entry["phases"]
    derived: dict[str, dict] = {}
    task = phases.get("task_total")
    embed = phases.get("embed")
    compute = phases.get("compute")
    publish = phases.get("publish")
    if task is None:
        return derived
    if embed is not None:
        derived["prep"] = _slice_between(task["t_start"], embed["t_start"])
        if compute is not None:
            derived["post_embed_prep"] = _slice_between(
                embed["t_end"], compute["t_start"]
            )
    if publish is not None:
        derived["finalize"] = _slice_between(publish["t_end"], task["t_end"])
    return derived


def _slice_between(started: float, ended: float | None) -> dict:
    return _derived_probe.stats_between(started, ended)


_derived_probe: common.MemoryProbe | None = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="index.sqlite3 snapshot to copy")
    parser.add_argument("--group")
    parser.add_argument("--pivot")
    parser.add_argument("--target")
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="build a public synthetic fixture instead of copying --db",
    )
    parser.add_argument("--synthetic-documents", type=int, default=2)
    parser.add_argument("--synthetic-paragraphs", type=int, default=8)
    parser.add_argument("--synthetic-alignment-paragraphs", type=int, default=8)
    parser.add_argument("--synthetic-seed", type=int, default=20260910)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--model-id", default=DEFAULT_EMBEDDING_MODEL_ID)
    parser.add_argument("--models", type=Path, help="text-alignment models root")
    parser.add_argument(
        "--provider",
        choices=("real", "fake"),
        default="real",
        help="fake uses (n,4) stub vectors and skips the ONNX model entirely",
    )
    parser.add_argument(
        "--fresh-vectors-per-round",
        action="store_true",
        help="control: delete document-vectors/*.npy before each round",
    )
    parser.add_argument(
        "--force-batch-size",
        type=int,
        default=None,
        help="control: override the product batch size for the embed call",
    )
    parser.add_argument("--tracemalloc", action="store_true")
    parser.add_argument("--tracemalloc-sites", default="publish")
    parser.add_argument("--interval-ms", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="alignment")
    parser.add_argument("--include-ids", action="store_true")
    parser.add_argument("--timeline", action="store_true", help="dump raw samples/events")
    arguments = parser.parse_args()

    if (arguments.db is None) == (not arguments.synthetic):
        parser.error("pass either --db (+ --group/--pivot/--target) or --synthetic")
    if arguments.db is not None and not all(
        (arguments.group, arguments.pivot, arguments.target)
    ):
        parser.error("--db requires --group, --pivot and --target")

    global _derived_probe
    site_names = [
        name for name in arguments.tracemalloc_sites.split(",") if name
    ]
    probe = common.MemoryProbe(
        interval_ms=arguments.interval_ms,
        traced=arguments.tracemalloc,
        site_phases=site_names if arguments.tracemalloc else [],
    )
    _derived_probe = probe
    provider = _fake_provider if arguments.provider == "fake" else None

    with TemporaryDirectory(prefix="mefinder-mem-align-") as temporary:
        root = Path(temporary)
        workload = _build_root(arguments, root)
        paths = AppPaths.create(root)
        index_runtime = IndexRuntime(
            paths,
            engine_factory=lambda path: SearchEngine(path),
            script_folding_enabled=lambda: True,
            rebuild_index=lambda *_args, **_kwargs: None,
            replace_source=lambda extracted, path, *, backup_existing: (
                replace_source_in_database(
                    extracted, path, backup_existing=backup_existing
                )
            ),
        )
        coordinator = TextAlignmentCoordinator(
            paths, index_runtime, _DurableOperations()
        )
        gate_patches = []
        if arguments.provider == "fake":
            from unittest import mock

            gate_patches = [
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
            for patcher in gate_patches:
                patcher.start()
        probe.start()
        try:
            with probe.phase("backend_idle", "idle"):
                pass
            idle_settle = probe.settle()
            with _Instrumentation(probe, provider, arguments.force_batch_size) as instrumentation:
                rounds = _run_rounds(arguments, probe, coordinator, workload,
                                     instrumentation)
        finally:
            for patcher in gate_patches:
                patcher.stop()
            probe.stop()
            index_runtime.close()

    idle_stats = {item["phase"]: item for item in probe.phase_stats("idle")}
    stable_after_rounds = [
        {"round": entry["round"], "rss": entry["settle_after"]["rss_stable"]}
        for entry in rounds
    ]
    report = {
        "tool": "mem_profile_alignment",
        "label": arguments.label,
        "mode": (
            "fresh-vectors-control"
            if arguments.fresh_vectors_per_round
            else "natural-lifecycle"
        ),
        "provider": arguments.provider,
        "model_id": arguments.model_id,
        "forced_batch_size": arguments.force_batch_size,
        "tracemalloc": arguments.tracemalloc,
        "git": common.git_metadata(),
        "environment": common.environment_info(),
        "embedding_thread_count": _embedding_thread_count(),
        "workload": _public_workload(workload, arguments),
        "idle": {"phase": idle_stats.get("backend_idle", {}), "settle": idle_settle},
        "rounds": [{**entry, "derived": _derived_for(entry)} for entry in rounds],
        "growth": {
            "stable_rss_after_round": stable_after_rounds,
            "slope_bytes_per_round_after_first": common.linear_slope(
                [item["round"] for item in stable_after_rounds],
                [item["rss"] for item in stable_after_rounds],
            ),
        },
    }
    if arguments.tracemalloc:
        report["tracemalloc_sites"] = list(probe._site_reports)
    if arguments.timeline:
        report["timeline"] = common.timeline_payload(probe)
    common.write_report(report, arguments.output)
    print(f"wrote {arguments.output}", flush=True)


def _run_rounds(arguments, probe, coordinator, workload, instrumentation) -> list:
    rounds: list[dict] = []
    vectors_dir = coordinator._paths.runtime_root / (
        "components/text-alignment/models/document-vectors"
    )
    for round_id in range(1, arguments.rounds + 1):
        group = f"round{round_id}"
        probe.current_group = group
        if arguments.fresh_vectors_per_round and vectors_dir.is_dir():
            for cached in vectors_dir.glob("*.npy"):
                cached.unlink()
        settled_before = probe.settle()
        probe.event("round_start", group)
        coordinator.generate(
            workload["group"], workload["pivot"], workload["target"], force=True
        )
        result = instrumentation.last_result or {}
        # Measure the product residue before summarising the result, so the
        # summary's own allocations never inflate the stable-resident number.
        settled_after = probe.settle()
        counts = _extract_counts(result)
        identity = common.sha256_bytes(
            json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        probe.event("round_end", group)
        phases = {item["phase"]: item for item in probe.phase_stats(group)}
        rounds.append(
            {
                "round": round_id,
                "phases": phases,
                "task": {"counts": counts, "identity_sha256": identity},
                "settle_before": settled_before,
                "settle_after": settled_after,
            }
        )
        print(
            f"round {round_id}/{arguments.rounds}: "
            f"stable={settled_after['rss_stable'] / common.MEBIBYTE:.1f}MiB "
            f"links={counts.get('alignment_link_count')}",
            flush=True,
        )
    return rounds


def _public_workload(workload: dict, arguments) -> dict:
    if arguments.include_ids:
        return workload
    return {
        key: value
        for key, value in workload.items()
        if key not in ("group", "pivot", "target")
    }


def _embedding_thread_count() -> int | None:
    try:
        from src.me_finder.embedding_runtime import embedding_thread_count

        return embedding_thread_count()
    except Exception:
        return None


if __name__ == "__main__":
    main()
