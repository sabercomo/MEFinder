from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder import onefile_cleanup
from src.me_finder.onefile_cleanup import (
    LOCK_FILE_NAME,
    ONEFILE_MARKER_NAME,
    cleanup_leaked_extractions,
    claim_current_extraction,
    start_background_cleanup,
)

_MARKED_DIR = "_MEI100001"
_LEAKED_DIR = "_MEI100002"
_FOREIGN_DIR = "_MEI100003"


def _make_extraction_dir(
    root: Path,
    name: str,
    *,
    with_marker: bool = True,
    with_lock: bool = False,
    hold_lock: bool = False,
    age_seconds: float = 0.0,
) -> tuple[Path, int | None]:
    """Create a fake PyInstaller onefile extraction directory for sweep tests."""
    directory = root / name
    directory.mkdir()
    if with_marker:
        (directory / ONEFILE_MARKER_NAME).write_text("marker", encoding="ascii")
    handle = None
    if with_lock:
        (directory / LOCK_FILE_NAME).write_text("pid=0\n", encoding="ascii")
    if hold_lock:
        lock_path = directory / LOCK_FILE_NAME
        handle = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
    if age_seconds:
        stale = time.time() - age_seconds
        os.utime(directory, (stale, stale))
    return directory, handle


def _reset_claim_state() -> None:
    if onefile_cleanup._claimed_lock_handle is not None:
        try:
            os.close(onefile_cleanup._claimed_lock_handle)
        except OSError:
            pass
    onefile_cleanup._claimed_lock_path = None
    onefile_cleanup._claimed_lock_handle = None


class CleanupLeakedExtractionsTests(unittest.TestCase):
    def test_deletes_old_marked_dir_with_unheld_lock(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leaked, _ = _make_extraction_dir(
                root, _LEAKED_DIR, with_lock=True, age_seconds=7200.0
            )
            removed = cleanup_leaked_extractions(root)
            self.assertEqual(removed, 1)
            self.assertFalse(leaked.exists())

    def test_deletes_old_marked_dir_without_lock(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leaked, _ = _make_extraction_dir(root, _LEAKED_DIR, age_seconds=7200.0)
            self.assertEqual(cleanup_leaked_extractions(root), 1)
            self.assertFalse(leaked.exists())

    def test_keeps_marked_dir_with_held_lock(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live, handle = _make_extraction_dir(
                root, _MARKED_DIR, hold_lock=True, age_seconds=7200.0
            )
            try:
                self.assertEqual(cleanup_leaked_extractions(root), 0)
                self.assertTrue(live.exists())
            finally:
                os.close(handle)

    def test_keeps_fresh_marked_dir_without_lock(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fresh, _ = _make_extraction_dir(root, _MARKED_DIR)
            self.assertEqual(cleanup_leaked_extractions(root), 0)
            self.assertTrue(fresh.exists())

    def test_never_touches_unmarked_dirs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            foreign, _ = _make_extraction_dir(
                root, _FOREIGN_DIR, with_marker=False, age_seconds=7200.0
            )
            self.assertEqual(cleanup_leaked_extractions(root), 0)
            self.assertTrue(foreign.exists())

    def test_never_touches_own_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            own, handle = _make_extraction_dir(
                root, _MARKED_DIR, hold_lock=True, age_seconds=7200.0
            )
            try:
                self.assertEqual(cleanup_leaked_extractions(root, own_dir=own), 0)
                self.assertTrue(own.exists())
            finally:
                os.close(handle)

    def test_missing_scan_root_is_not_an_error(self) -> None:
        missing = Path(tempfile.gettempdir()) / "mefinder-cleanup-missing-root"
        self.assertEqual(cleanup_leaked_extractions(missing), 0)


class ClaimCurrentExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_claim_state()

    def tearDown(self) -> None:
        _reset_claim_state()

    def test_writes_lock_inside_extraction_and_keeps_it_open(self) -> None:
        with TemporaryDirectory() as temp_dir:
            extraction = Path(temp_dir)
            with patch.object(
                onefile_cleanup.sys, "_MEIPASS", str(extraction), create=True
            ):
                self.assertEqual(claim_current_extraction(), extraction)
            self.assertTrue((extraction / LOCK_FILE_NAME).exists())
            # A second call must be idempotent and keep returning the same dir.
            self.assertEqual(claim_current_extraction(), extraction)
            # Release the claim handle before the temp directory is cleaned up.
            _reset_claim_state()

    def test_returns_none_outside_onefile_extraction(self) -> None:
        with patch.object(onefile_cleanup.sys, "frozen", True, create=True):
            self.assertIsNone(claim_current_extraction())

    def test_returns_none_when_lock_cannot_be_taken(self) -> None:
        missing = Path(tempfile.gettempdir()) / "mefinder-cleanup-missing-extraction"
        with patch.object(onefile_cleanup.sys, "frozen", True, create=True), \
                patch.object(
                    onefile_cleanup.sys, "_MEIPASS", str(missing), create=True
                ):
            self.assertIsNone(claim_current_extraction())


class StartBackgroundCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_claim_state()

    def tearDown(self) -> None:
        _reset_claim_state()

    def test_is_a_noop_for_source_runs(self) -> None:
        self.assertIsNone(start_background_cleanup())

    def test_starts_daemon_sweep_for_onefile_runs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leaked, _ = _make_extraction_dir(root, _LEAKED_DIR, age_seconds=7200.0)
            extraction = root / _MARKED_DIR
            extraction.mkdir()
            (extraction / ONEFILE_MARKER_NAME).write_text("marker", encoding="ascii")
            with patch.object(onefile_cleanup.sys, "frozen", True, create=True), \
                    patch.object(
                        onefile_cleanup.sys, "_MEIPASS", str(extraction), create=True
                    ), \
                    patch.object(
                        onefile_cleanup.tempfile, "gettempdir", return_value=str(root)
                    ):
                thread = start_background_cleanup()
                self.assertIsNotNone(thread)
                assert thread is not None
                self.assertTrue(thread.daemon)
                thread.join(timeout=10.0)
                self.assertFalse(thread.is_alive())
            self.assertFalse(leaked.exists())
            self.assertTrue(extraction.exists())
            self.assertTrue((extraction / LOCK_FILE_NAME).exists())
            _reset_claim_state()

    def test_starts_daemon_sweep_for_frozen_onedir_runs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leaked, _ = _make_extraction_dir(root, _LEAKED_DIR, age_seconds=7200.0)
            with patch.object(onefile_cleanup.sys, "frozen", True, create=True), \
                    patch.object(
                        onefile_cleanup.tempfile, "gettempdir", return_value=str(root)
                    ):
                thread = start_background_cleanup()
                self.assertIsNotNone(thread)
                assert thread is not None
                thread.join(timeout=10.0)
                self.assertFalse(thread.is_alive())
            self.assertFalse(leaked.exists())

    def test_skips_sweep_when_extraction_cannot_be_locked(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            leaked, _ = _make_extraction_dir(root, _LEAKED_DIR, age_seconds=7200.0)
            extraction = root / _MARKED_DIR
            extraction.mkdir()
            (extraction / ONEFILE_MARKER_NAME).write_text("marker", encoding="ascii")
            with patch.object(onefile_cleanup.sys, "frozen", True, create=True), \
                    patch.object(
                        onefile_cleanup.sys, "_MEIPASS", str(extraction), create=True
                    ), \
                    patch.object(
                        onefile_cleanup,
                        "current_extraction_dir",
                        return_value=None,
                    ):
                self.assertIsNone(start_background_cleanup())
            self.assertTrue(leaked.exists())


class SidecarPackagingMarkerTests(unittest.TestCase):
    def test_sidecar_spec_ships_the_cleanup_marker(self) -> None:
        spec = Path("packaging/mcp_sidecar.spec").read_text(encoding="utf-8")
        self.assertIn("mefinder-onefile.marker", spec)
        self.assertTrue(
            Path("packaging/mefinder-onefile.marker").is_file(),
            "marker file referenced by the spec is missing",
        )


if __name__ == "__main__":
    unittest.main()
