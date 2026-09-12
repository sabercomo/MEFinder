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


if __name__ == "__main__":
    unittest.main()
