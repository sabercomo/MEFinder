"""Shared phased-memory instrumentation for alignment/export diagnostics.

Measurement-only tooling; never imported by product code. Channels, kept
strictly separate in every report:

- current RSS of this process and of the whole child tree (psutil, sampled);
- OS historical peak (``resource.getrusage`` ``ru_maxrss``; monotonic, never
  interpreted as a leak indicator on its own);
- optional ``tracemalloc`` counters per phase (Python/numpy allocations only;
  ONNX Runtime native allocations are invisible to it, so native evidence is
  derived from RSS-vs-traced differences, never from tracemalloc alone).

Settling after a task waits for a naturally stable RSS; it never calls
``gc.collect()`` or evicts caches, so numbers reflect product behaviour.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import threading
import time
import tracemalloc

try:
    import resource  # Unix-only; absent on Windows.
except ImportError:  # pragma: no cover - platform-dependent
    resource = None  # type: ignore[assignment]
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]

SampleFn = Callable[[], Tuple[int, int]]  # (self_rss_bytes, children_rss_bytes)
Clock = Callable[[], float]

MEBIBYTE = 1024 * 1024


def psutil_samples() -> Tuple[int, int]:
    """Current RSS of this process plus the sum over its child tree."""

    import psutil

    process = psutil.Process()
    children_rss = 0
    for child in process.children(recursive=True):
        try:
            children_rss += child.memory_info().rss
        except psutil.Error:
            continue
    return process.memory_info().rss, children_rss


def ru_maxrss_bytes() -> int | None:
    """OS high-water RSS of this process (bytes on macOS, KiB units elsewhere).

    Returns ``None`` where ``resource`` is unavailable (Windows) so the metric
    is reported as missing rather than a fabricated zero.
    """

    if resource is None:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def ru_maxrss_children_bytes() -> int | None:
    """OS high-water RSS accumulated by waited-for children (same unit rule).

    ``None`` when ``resource`` is unavailable (Windows).
    """

    if resource is None:
        return None
    value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


@dataclass
class PhaseSpan:
    name: str
    group: str
    started: float
    ended: float | None = None
    # tracemalloc counters attached to this exact span (not shared by name), so
    # repeated same-name phases across rounds keep independent statistics.
    traced_current: int | None = None
    traced_peak: int | None = None


class MemoryProbe:
    """Background RSS sampler plus explicit phase/event boundary marks."""

    def __init__(
        self,
        *,
        interval_ms: int = 25,
        sample_fn: SampleFn | None = None,
        clock: Clock = time.monotonic,
        traced: bool = False,
        site_phases: Sequence[str] = (),
    ) -> None:
        self.interval_s = interval_ms / 1000.0
        self.sample_fn: SampleFn = sample_fn or psutil_samples
        self.clock = clock
        self.traced = traced
        self.site_phases = set(site_phases)
        self._samples: List[Tuple[float, int, int]] = []
        self._events: List[dict] = []
        self._phases: List[PhaseSpan] = []
        # Stack of currently-open traced phases (innermost last). tracemalloc's
        # peak is a single global counter, so nested phases hand their peak up to
        # every enclosing span before resetting — the outer peak is never lost.
        self._trace_stack: List[PhaseSpan] = []
        self._site_reports: List[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_site_snapshot = None
        # Instrumentation wrappers are installed once but phases must be
        # attributed to the current repetition; scripts set this per round.
        self.current_group = ""
        self.keep_timeline = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.traced:
            tracemalloc.start(1)
        self._thread = threading.Thread(
            target=self._run, name="mem-profile-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self.traced:
            tracemalloc.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            rss, children = self.sample_fn()
            with self._lock:
                self._samples.append((self.clock(), rss, children))
            self._stop.wait(self.interval_s)

    # -- marks -------------------------------------------------------------

    def event(self, name: str, group: str = "") -> dict:
        rss, children = self.sample_fn()
        record = {
            "t": self.clock(),
            "name": name,
            "group": group,
            "rss": rss,
            "children_rss": children,
            "ru_maxrss": ru_maxrss_bytes(),
            "ru_maxrss_children": ru_maxrss_children_bytes(),
        }
        with self._lock:
            self._events.append(record)
        return record

    @contextmanager
    def phase(self, name: str, group: str = "") -> Iterator[PhaseSpan]:
        group = group or self.current_group
        span = PhaseSpan(name=name, group=group, started=self.clock())
        with self._lock:
            self._phases.append(span)
        if self.traced:
            # The peak reached so far belongs to whatever phases are already
            # open; absorb it into them before this phase resets the counter.
            _current, peak = tracemalloc.get_traced_memory()
            for frame in self._trace_stack:
                frame.traced_peak = max(frame.traced_peak or 0, peak)
            span.traced_peak = 0
            self._trace_stack.append(span)
            tracemalloc.reset_peak()
        self.event(f"{name}:start", group)
        try:
            yield span
        finally:
            end_event = self.event(f"{name}:end", group)
            # Anchor the window on the end event's own timestamp so the end
            # boundary event itself (and samples up to it) are inside.
            span.ended = end_event["t"]
            if self.traced:
                current, peak = tracemalloc.get_traced_memory()
                if self._trace_stack and self._trace_stack[-1] is span:
                    self._trace_stack.pop()
                span.traced_current = current
                span.traced_peak = max(span.traced_peak or 0, peak)
                # Hand this window's peak up to still-open enclosing phases and
                # reset so a parent keeps measuring without losing what we saw.
                for frame in self._trace_stack:
                    frame.traced_peak = max(frame.traced_peak or 0, span.traced_peak)
                tracemalloc.reset_peak()
                if name in self.site_phases:
                    self._record_site_snapshot(name, group)

    def _record_site_snapshot(self, name: str, group: str) -> None:
        snapshot = tracemalloc.take_snapshot()
        report: dict = {"phase": name, "group": group, "top": []}
        if self._last_site_snapshot is not None:
            comparison = snapshot.compare_to(self._last_site_snapshot, "lineno")
            report["top"] = [
                {
                    "site": str(item.traceback),
                    "size_delta": item.size_diff,
                    "count_delta": item.count_diff,
                }
                for item in comparison[:10]
            ]
        self._last_site_snapshot = snapshot
        with self._lock:
            self._site_reports.append(report)

    # -- stats -------------------------------------------------------------

    def _snapshot_locked(self) -> tuple[list, list]:
        # Copy under the lock: the sampler thread appends to _samples
        # concurrently, so an unlocked copy can raise mid-iteration.
        with self._lock:
            return list(self._samples), list(self._events)

    def stats_between(self, started: float, ended: float | None) -> dict:
        samples, events = self._snapshot_locked()
        limit = ended if ended is not None else self.clock()
        inside = [sample for sample in samples if started <= sample[0] <= limit]
        boundary = [event for event in events if started <= event["t"] <= limit]
        # Both interval samples and boundary-event RSS are valid observations;
        # the max/min must span both so a transient captured only at a boundary
        # is not dropped.
        rss_obs = [sample[1] for sample in inside] + [
            event["rss"] for event in boundary
        ]
        children_obs = [sample[2] for sample in inside] + [
            event["children_rss"] for event in boundary
        ]
        base = {
            "t_start": started,
            "t_end": limit,
            "duration_s": max(0.0, limit - started),
            "samples": len(inside),
            "events": len(boundary),
        }
        if not rss_obs:
            # A window with neither samples nor events: report the duration and
            # leave the RSS metrics absent rather than fabricating values.
            return base
        # Anchor start/end on boundary events when present (they bracket the
        # phase exactly); otherwise use the first/last timed sample.
        if boundary:
            rss_start, rss_end = boundary[0]["rss"], boundary[-1]["rss"]
        else:
            rss_start, rss_end = inside[0][1], inside[-1][1]
        base.update(
            {
                "rss_start": rss_start,
                "rss_end": rss_end,
                "rss_min": min(rss_obs),
                "rss_max": max(rss_obs),
                "children_rss_max": max(children_obs) if children_obs else 0,
            }
        )
        ru_values = [
            event["ru_maxrss"]
            for event in boundary
            if event.get("ru_maxrss") is not None
        ]
        if ru_values:
            base["ru_maxrss_end"] = ru_values[-1]
            base["ru_maxrss_delta"] = ru_values[-1] - ru_values[0]
        return base

    def phase_stats(self, group: str) -> List[dict]:
        with self._lock:
            spans = [span for span in self._phases if span.group == group]
        results = []
        for span in spans:
            stats = self.stats_between(span.started, span.ended)
            stats["phase"] = span.name
            # Traced counters live on the span itself, so repeated same-name
            # phases across rounds never share a statistics record.
            if self.traced and span.traced_peak is not None:
                stats["traced_current_end"] = span.traced_current
                stats["traced_peak"] = span.traced_peak
            results.append(stats)
        return results

    def settle(
        self,
        *,
        threshold_bytes: int = MEBIBYTE,
        quiescent_s: float = 2.0,
        max_wait_s: float = 20.0,
        poll_s: float = 0.1,
    ) -> dict:
        """Wait until RSS holds within ``threshold`` for ``quiescent_s``.

        Pure observation: no ``gc.collect()``, no cache eviction, no model
        unloading. Returns the last stable RSS and how long settling took.
        """

        started = self.clock()
        baseline: int | None = None
        last_change = started
        while self.clock() - started < max_wait_s:
            rss, _children = self.sample_fn()
            now = self.clock()
            if baseline is None or abs(rss - baseline) > threshold_bytes:
                baseline = rss
                last_change = now
            elif now - last_change >= quiescent_s:
                return {"rss_stable": baseline, "waited_s": now - started}
            time.sleep(poll_s)
        return {
            "rss_stable": baseline,
            "waited_s": self.clock() - started,
            "timed_out": True,
        }


def linear_slope(rounds: Sequence[int], values: Sequence[int]) -> float | None:
    """Descriptive per-round slope (bytes/round) for points after the first."""

    points = [(index, value) for index, value in zip(rounds, values)][1:]
    if len(points) < 2:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator == 0:
        return None
    numerator = sum(
        (point[0] - mean_x) * (point[1] - mean_y) for point in points
    )
    return numerator / denominator


def environment_info() -> dict:
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "ru_maxrss_available": resource is not None,
        "ru_maxrss_unit": (
            None
            if resource is None
            else ("bytes" if sys.platform == "darwin" else "kib")
        ),
    }
    if sys.platform == "darwin":
        info["cpu_model"] = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip()
    try:
        import psutil

        info["physical_memory_bytes"] = psutil.virtual_memory().total
        info["packages"] = {
            name: importlib.metadata.version(name)
            for name in ("psutil", "numpy", "fastembed", "onnxruntime")
        }
    except Exception as exc:  # measurement must degrade without psutil details
        info["packages_error"] = type(exc).__name__
    return info


def git_metadata() -> dict:
    def git(*arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=REPO, text=True
        ).strip()

    dirty = bool(git("diff", "HEAD", "--name-only"))
    return {"revision": git("rev-parse", "HEAD"), "tracked_dirty": dirty}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path, chunk_bytes: int = 1024 * 1024) -> str:
    """Stream-hash a file in bounded chunks.

    Never materialises the whole file, so summarising a large export cannot
    inflate this process's RSS/ru_maxrss and contaminate a resident-memory
    measurement. Returns the same digest as ``sha256_bytes`` over the contents.
    """

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(block)
    return digest.hexdigest()


def _assert_report_safe(payload: object, path: str = "report") -> None:
    """Refuse to write long strings so private text can never leak into JSON."""

    if isinstance(payload, str):
        if len(payload) > 512:
            raise ValueError(f"{path}: string too long for a public report")
    elif isinstance(payload, dict):
        for key, value in payload.items():
            _assert_report_safe(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            _assert_report_safe(value, f"{path}[{index}]")


def write_report(payload: dict, path: Path) -> None:
    _assert_report_safe(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def timeline_payload(probe: "MemoryProbe") -> dict:
    """Raw samples and boundary events for post-hoc peak attribution."""

    with probe._lock:
        samples = [
            {"t": t, "rss": rss, "children": children}
            for t, rss, children in probe._samples
        ]
        events = [dict(event) for event in probe._events]
    origin = samples[0]["t"] if samples else 0.0
    for record in samples:
        record["t"] = round(record["t"] - origin, 4)
    for event in events:
        event["t"] = round(event["t"] - origin, 4)
    return {"origin_is_monotonic_zero": True, "samples": samples, "events": events}


def summary_line(label: str, rounds: Sequence[dict]) -> str:
    """One-line console digest: per-round stable RSS after settling."""

    parts = []
    for record in rounds:
        parts.append(
            f"r{record['round']}:stable={record['settle']['rss_stable'] / MEBIBYTE:.1f}MiB"
        )
    return f"{label} " + " ".join(parts)
