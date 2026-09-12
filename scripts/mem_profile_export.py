"""Phased-memory profile of indexed-document export, in one process.

Calls the production ``document_export_service`` export functions directly
(the HTTP layer adds no extra process) with module-attribute wrappers that
turn each internal call site into a measured phase. The same export runs for
``--rounds`` consecutive rounds in one process to separate one-off growth
(normalizer caches, allocator warm-up) from genuine per-round growth.

Phases (PDF markdown): ``snapshot_load`` (one BEGIN DEFERRED read transaction,
including page payload materialisation), ``load_pages`` (page list + export
layout attach), ``normalize`` (footnote pairing / page-artifact profile /
export structure), ``render_markdown`` (one big output string), plus the
derived ``finalize`` gap (page-selection stitching, ``.partial`` write,
atomic replace). For EPUB format: ``render_epub_zip`` (in-memory BytesIO zip
inside ``write_epub``) nested in ``epub_write``, whose remainder is the file
write. For EPUB-source markdown the paragraph materialisation happens inside
``snapshot_load`` and is noted in the report.

Defaults reflect the natural product lifecycle; nothing is garbage-collected,
evicted or pre-warmed beyond what a second export in a live backend would see.

Output JSON carries counts and hashes only (a length guard rejects long
strings); pass ``--db`` snapshots explicitly.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
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


from scripts import mem_profile_common as common  # noqa: E402
from src.me_finder import document_export_service as export_module  # noqa: E402
from src.me_finder import epub_export as epub_export_module  # noqa: E402
from src.me_finder.app_context import AppPaths  # noqa: E402
from src.me_finder.application.index_runtime import IndexRuntime  # noqa: E402
from src.me_finder.database import replace_source_in_database  # noqa: E402
from src.me_finder.search import SearchEngine  # noqa: E402

_DERIVED_PROBE: common.MemoryProbe | None = None


class _ExportInstrumentation:
    """Module-attribute patches that turn export call sites into phases."""

    def __init__(self, probe: common.MemoryProbe) -> None:
        self.probe = probe
        self.last_result: dict | None = None
        self._patches: list[tuple[object, str, object]] = []

    def _install(self, module, attribute: str, wrapper) -> None:
        real = getattr(module, attribute)
        setattr(module, attribute, wrapper)
        self._patches.append((module, attribute, real))

    def __enter__(self) -> "_ExportInstrumentation":
        probe = self.probe

        def wrap_phase(module, attribute: str, phase_name: str) -> None:
            real = getattr(module, attribute)

            def wrapper(*args, **kwargs):
                with probe.phase(phase_name):
                    return real(*args, **kwargs)

            self._install(module, attribute, wrapper)

        real_snapshot = export_module._snapshot_connection

        @contextmanager
        def snapshot_wrapper(database):
            with probe.phase("snapshot_load"):
                with real_snapshot(database) as connection:
                    yield connection

        self._install(export_module, "_snapshot_connection", snapshot_wrapper)
        wrap_phase(export_module, "_text_export_pages", "load_pages")
        wrap_phase(export_module, "normalize_document_export", "normalize")
        wrap_phase(export_module, "document_to_markdown", "render_markdown")
        wrap_phase(
            export_module, "epub_paragraphs_to_markdown", "render_markdown_epub"
        )
        wrap_phase(export_module, "write_epub", "epub_write")
        wrap_phase(epub_export_module, "build_epub_bytes", "render_epub_zip")

        for name in ("export_indexed_pdf_markdown", "export_indexed_pdf_epub"):
            real = getattr(export_module, name)

            def task_wrapper(*args, _real=real, _name=name, **kwargs):
                with probe.phase("task_total"):
                    result = _real(*args, **kwargs)
                self.last_result = dict(result, _entry_point=_name)
                return result

            self._install(export_module, name, task_wrapper)
        return self

    def __exit__(self, *_exc) -> None:
        for module, attribute, real in reversed(self._patches):
            setattr(module, attribute, real)
        self._patches.clear()


def _build_root(arguments, root: Path) -> dict:
    (root / "data").mkdir(parents=True, exist_ok=True)
    output_dir = root / "export-out"
    output_dir.mkdir(parents=True, exist_ok=True)
    if arguments.db is not None:
        database = Path(arguments.db).resolve()
        shutil.copy2(database, root / "data/index.sqlite3")
        workload = {
            "kind": "real-snapshot",
            "database_bytes": (root / "data/index.sqlite3").stat().st_size,
            "source": arguments.source_id,
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
            "paragraphs_per_document": arguments.synthetic_paragraphs,
            "alignment_paragraphs": arguments.synthetic_alignment_paragraphs,
            "seed": arguments.synthetic_seed,
            "source": arguments.source_id,
        }
    source_kind = _source_item_count(root / "data/index.sqlite3", arguments.source_id)
    workload.update(source_kind)
    return workload


def _source_item_count(database: Path, source_id: str) -> dict:
    connection = sqlite3.connect(str(database))
    try:
        pdf_pages = connection.execute(
            "SELECT COUNT(*) FROM pdf_pages WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()[0]
        paragraphs = connection.execute(
            "SELECT COUNT(*) FROM paragraphs WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    return {"source_pdf_pages": pdf_pages, "source_paragraphs": paragraphs}


def _default_source(root: Path, fmt: str, synthetic_documents: int) -> str:
    """Synthetic default: the largest generated PDF document."""

    if fmt == "markdown-epub-source":
        return f"bench-{synthetic_documents:03d}"
    return "bench-000"


def _export_once(arguments, root: Path) -> dict:
    common_arguments = dict(
        database_path=root / "data/index.sqlite3",
        source_file_id=arguments.source_id,
        output_dir=root / "export-out",
        runtime_root=Path(root),
    )
    if arguments.format == "epub":
        return export_module.export_indexed_pdf_epub(**common_arguments)
    return export_module.export_indexed_pdf_markdown(**common_arguments)


def _run_rounds(arguments, probe, root: Path) -> list:
    rounds: list[dict] = []
    for round_id in range(1, arguments.rounds + 1):
        group = f"round{round_id}"
        probe.current_group = group
        settled_before = probe.settle()
        probe.event("round_start", group)
        result = _export_once(arguments, root)
        output_file = Path(str(result["path"]))
        identity = common.sha256_bytes(output_file.read_bytes())
        size_bytes = result.get("size_bytes")
        output_file.unlink(missing_ok=True)
        settled_after = probe.settle()
        probe.event("round_end", group)
        phases = {item["phase"]: item for item in probe.phase_stats(group)}
        entry_point = (
            "export_indexed_pdf_epub"
            if arguments.format == "epub"
            else "export_indexed_pdf_markdown"
        )
        rounds.append(
            {
                "round": round_id,
                "phases": phases,
                "task": {
                    "entry_point": entry_point,
                    "size_bytes": size_bytes,
                    "identity_sha256": identity,
                },
                "settle_before": settled_before,
                "settle_after": settled_after,
            }
        )
        print(
            f"round {round_id}/{arguments.rounds}: "
            f"stable={settled_after['rss_stable'] / common.MEBIBYTE:.1f}MiB "
            f"size={size_bytes}",
            flush=True,
        )
    return rounds


def _derived_for(entry: dict) -> dict:
    phases = entry["phases"]
    task = phases.get("task_total")
    if task is None:
        return {}
    covered_end = task["t_start"]
    for name in (
        "render_markdown",
        "render_markdown_epub",
        "epub_write",
        "normalize",
        "snapshot_load",
    ):
        phase = phases.get(name)
        if phase is not None:
            covered_end = max(covered_end, phase["t_end"])
    derived: dict[str, dict] = {}
    if covered_end < task["t_end"]:
        derived["finalize_write"] = _slice_between(covered_end, task["t_end"])
    if "snapshot_load" in phases and "load_pages" in phases:
        derived["snapshot_meta"] = _slice_between(
            phases["snapshot_load"]["t_start"], phases["load_pages"]["t_start"]
        )
    return derived


def _slice_between(started: float, ended: float | None) -> dict:
    assert _DERIVED_PROBE is not None
    return _DERIVED_PROBE.stats_between(started, ended)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="index.sqlite3 snapshot to copy")
    parser.add_argument("--source-id")
    parser.add_argument(
        "--format",
        choices=("markdown", "epub", "markdown-epub-source"),
        default="markdown",
        help="markdown: indexed PDF/EPUB -> Markdown; epub: indexed PDF -> "
        "EPUB 3; markdown-epub-source: EPUB-format source -> Markdown",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="build a public synthetic fixture instead of copying --db",
    )
    parser.add_argument("--synthetic-documents", type=int, default=2)
    parser.add_argument("--synthetic-paragraphs", type=int, default=2000)
    parser.add_argument("--synthetic-alignment-paragraphs", type=int, default=4)
    parser.add_argument("--synthetic-seed", type=int, default=20260910)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--tracemalloc", action="store_true")
    parser.add_argument("--tracemalloc-sites", default="normalize,render_markdown")
    parser.add_argument("--interval-ms", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="export")
    parser.add_argument("--include-ids", action="store_true")
    parser.add_argument("--timeline", action="store_true", help="dump raw samples/events")
    arguments = parser.parse_args()

    if (arguments.db is None) == (not arguments.synthetic):
        parser.error("pass either --db (+ --source-id) or --synthetic")
    if arguments.db is not None and not arguments.source_id:
        parser.error("--db requires --source-id")
    if arguments.synthetic and not arguments.source_id:
        arguments.source_id = _default_source(
            Path("."), arguments.format, arguments.synthetic_documents
        )

    global _DERIVED_PROBE
    site_names = [
        name for name in arguments.tracemalloc_sites.split(",") if name
    ]
    probe = common.MemoryProbe(
        interval_ms=arguments.interval_ms,
        traced=arguments.tracemalloc,
        site_phases=site_names if arguments.tracemalloc else [],
    )
    _DERIVED_PROBE = probe

    with TemporaryDirectory(prefix="mefinder-mem-export-") as temporary:
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
        probe.start()
        try:
            with probe.phase("backend_idle", "idle"):
                pass
            idle_settle = probe.settle()
            with _ExportInstrumentation(probe):
                rounds = _run_rounds(arguments, probe, root)
        finally:
            probe.stop()
            index_runtime.close()

    idle_stats = {item["phase"]: item for item in probe.phase_stats("idle")}
    stable_after_rounds = [
        {"round": entry["round"], "rss": entry["settle_after"]["rss_stable"]}
        for entry in rounds
    ]
    report = {
        "tool": "mem_profile_export",
        "label": arguments.label,
        "format": arguments.format,
        "tracemalloc": arguments.tracemalloc,
        "git": common.git_metadata(),
        "environment": common.environment_info(),
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


def _public_workload(workload: dict, arguments) -> dict:
    if arguments.include_ids:
        return workload
    return {
        key: value
        for key, value in workload.items()
        if key != "source"
    }


if __name__ == "__main__":
    main()
