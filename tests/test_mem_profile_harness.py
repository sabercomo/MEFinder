"""Unit coverage for the phased-memory measurement harness.

Only the measurement plumbing is tested here (slicing, settling, report
hygiene); the diagnostic scripts themselves run against real fixtures and are
not part of the regression gate.
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import mem_profile_common as common
from scripts.mem_profile_common import (
    MEBIBYTE,
    MemoryProbe,
    linear_slope,
    write_report,
)

try:
    import psutil  # noqa: F401

    _HAVE_PSUTIL = True
except ModuleNotFoundError:  # pragma: no cover - measurement-only dependency
    _HAVE_PSUTIL = False


class LinearSlopeTests(unittest.TestCase):
    def test_flat_series_has_zero_slope(self) -> None:
        self.assertEqual(linear_slope([1, 2, 3], [10, 10, 10]), 0.0)

    def test_growing_series_has_unit_slope(self) -> None:
        self.assertAlmostEqual(linear_slope([1, 2, 3], [0, 5, 10]), 5.0)

    def test_single_point_has_no_slope(self) -> None:
        self.assertIsNone(linear_slope([1], [10]))


class ReportHygieneTests(unittest.TestCase):
    def test_report_rejects_long_strings(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            with self.assertRaises(ValueError):
                write_report({"note": "x" * 600}, path)

    def test_sha256_file_matches_in_memory_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "blob.bin"
            payload = bytes(range(256)) * 9000  # spans several 1 MiB chunks
            path.write_bytes(payload)
            self.assertEqual(
                common.sha256_file(path, chunk_bytes=64 * 1024),
                common.sha256_bytes(payload),
            )

    def test_report_writes_counts_and_hashes(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            write_report(
                {"sha256": "a" * 64, "counts": {"links": 12}, "nested": [1, 2]},
                path,
            )
            self.assertIn('"links": 12', path.read_text(encoding="utf-8"))


@unittest.skipUnless(_HAVE_PSUTIL, "psutil is a measurement-only dependency")
class MemoryProbeTests(unittest.TestCase):
    def test_phase_stats_track_rss_growth_and_settle(self) -> None:
        holder = {"rss": 40 * MEBIBYTE}

        def sample():
            return holder["rss"], 0

        probe = MemoryProbe(interval_ms=5, sample_fn=sample)
        probe.start()
        try:
            with probe.phase("growing", "r1"):
                time.sleep(0.05)
                holder["rss"] = 120 * MEBIBYTE
                time.sleep(0.15)
            stats = {item["phase"]: item for item in probe.phase_stats("r1")}["growing"]
            self.assertLess(stats["rss_start"], 100 * MEBIBYTE)
            self.assertGreaterEqual(stats["rss_max"], 120 * MEBIBYTE)
            self.assertGreaterEqual(stats["duration_s"], 0.15)
            settled = probe.settle(quiescent_s=0.3, max_wait_s=5.0, poll_s=0.05)
            self.assertEqual(settled["rss_stable"], 120 * MEBIBYTE)
            self.assertNotIn("timed_out", settled)
        finally:
            probe.stop()

    def test_children_rss_is_summed_into_stats(self) -> None:
        holder = {"rss": 10 * MEBIBYTE, "children": 3 * MEBIBYTE}

        def sample():
            return holder["rss"], holder["children"]

        probe = MemoryProbe(interval_ms=5, sample_fn=sample)
        probe.start()
        try:
            with probe.phase("tree", "r1"):
                time.sleep(0.1)
            stats = {item["phase"]: item for item in probe.phase_stats("r1")}["tree"]
            self.assertGreaterEqual(stats["children_rss_max"], 3 * MEBIBYTE)
        finally:
            probe.stop()

    def test_traced_mode_records_python_heap_peak(self) -> None:
        holder = {"rss": 10 * MEBIBYTE}

        def sample():
            return holder["rss"], 0

        probe = MemoryProbe(interval_ms=5, sample_fn=sample, traced=True)
        probe.start()
        try:
            with probe.phase("allocating", "r1"):
                time.sleep(0.05)
                ballast = bytearray(8 * MEBIBYTE)
                del ballast
                time.sleep(0.05)
            stats = {item["phase"]: item for item in probe.phase_stats("r1")}[
                "allocating"
            ]
            self.assertGreaterEqual(stats["traced_peak"], 4 * MEBIBYTE)
        finally:
            probe.stop()


class StatsBetweenBoundaryTests(unittest.TestCase):
    """stats_between must honour boundary events and never assume dict samples."""

    def _probe_with(self, samples, events):
        probe = MemoryProbe(sample_fn=lambda: (0, 0))
        probe._samples = list(samples)
        probe._events = list(events)
        return probe

    def _event(self, t, rss, children=0, ru=None):
        return {
            "t": t, "name": "x", "group": "g", "rss": rss,
            "children_rss": children, "ru_maxrss": ru, "ru_maxrss_children": ru,
        }

    def test_rss_max_includes_boundary_events_not_only_timed_samples(self) -> None:
        # A transient captured only by the phase boundary events (200/210) must
        # not be lost just because the interval sampler saw lower values.
        probe = self._probe_with(
            samples=[(1.0, 50, 0), (2.0, 60, 0)],
            events=[self._event(1.0, 200), self._event(2.0, 210)],
        )
        stats = probe.stats_between(1.0, 2.0)
        self.assertEqual(stats["rss_max"], 210)
        self.assertEqual(stats["rss_min"], 50)

    def test_samples_without_events_report_metrics_without_raising(self) -> None:
        probe = self._probe_with(
            samples=[(1.0, 50, 5), (2.0, 60, 7)],
            events=[],
        )
        stats = probe.stats_between(1.0, 2.0)  # must not raise on tuple samples
        self.assertEqual(stats["rss_start"], 50)
        self.assertEqual(stats["rss_end"], 60)
        self.assertEqual(stats["rss_max"], 60)
        self.assertEqual(stats["children_rss_max"], 7)

    def test_empty_window_reports_only_duration(self) -> None:
        probe = self._probe_with(samples=[], events=[])
        stats = probe.stats_between(5.0, 7.0)
        self.assertEqual(stats["duration_s"], 2.0)
        self.assertNotIn("rss_max", stats)


class ResourceOptionalTests(unittest.TestCase):
    """ru_maxrss must degrade to 'unavailable' (never a fake zero) off Unix."""

    def test_ru_maxrss_is_none_when_resource_module_absent(self) -> None:
        with mock.patch.object(common, "resource", None):
            self.assertIsNone(common.ru_maxrss_bytes())
            self.assertIsNone(common.ru_maxrss_children_bytes())

    def test_stats_omit_ru_maxrss_when_unavailable(self) -> None:
        probe = MemoryProbe(sample_fn=lambda: (10, 0))
        probe._samples = [(1.0, 10, 0), (2.0, 10, 0)]
        with mock.patch.object(common, "resource", None):
            record = probe.event("p:start", "g")
            self.assertIsNone(record["ru_maxrss"])
            probe._events = [record, probe.event("p:end", "g")]
            stats = probe.stats_between(record["t"], probe._events[-1]["t"])
        self.assertNotIn("ru_maxrss_delta", stats)


@unittest.skipUnless(_HAVE_PSUTIL, "psutil is a measurement-only dependency")
class TracedNestingTests(unittest.TestCase):
    """Nested/repeated phases must not corrupt each other's tracemalloc peak."""

    def _peak(self, probe, group, name):
        return {item["phase"]: item for item in probe.phase_stats(group)}[name][
            "traced_peak"
        ]

    def test_nested_phase_does_not_erase_outer_peak(self) -> None:
        probe = MemoryProbe(interval_ms=5, sample_fn=lambda: (0, 0), traced=True)
        probe.start()
        try:
            with probe.phase("outer", "r1"):
                ballast = bytearray(8 * MEBIBYTE)  # outer transient, freed pre-inner
                del ballast
                with probe.phase("inner", "r1"):
                    small = bytearray(1 * MEBIBYTE)
                    del small
        finally:
            probe.stop()
        self.assertGreaterEqual(self._peak(probe, "r1", "outer"), 7 * MEBIBYTE)
        self.assertLess(self._peak(probe, "r1", "inner"), 4 * MEBIBYTE)

    def test_repeated_phase_name_across_rounds_keeps_separate_peaks(self) -> None:
        probe = MemoryProbe(interval_ms=5, sample_fn=lambda: (0, 0), traced=True)
        probe.start()
        try:
            probe.current_group = "round1"
            with probe.phase("normalize"):
                big = bytearray(8 * MEBIBYTE)
                del big
            probe.current_group = "round2"
            with probe.phase("normalize"):
                small = bytearray(1 * MEBIBYTE)
                del small
        finally:
            probe.stop()
        self.assertGreaterEqual(self._peak(probe, "round1", "normalize"), 7 * MEBIBYTE)
        self.assertLess(self._peak(probe, "round2", "normalize"), 4 * MEBIBYTE)


if __name__ == "__main__":
    unittest.main()
