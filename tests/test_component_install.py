import hashlib
import io
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest import mock

from src.me_finder.components.install import (
    CANCELLED_MESSAGE,
    CANCELLING_MESSAGE,
    begin_download_progress,
    cancel_and_join_operations,
    check_cancelled,
    copy_response_to_file,
    extract_archive_member,
    install_uv_executable,
    record_download_progress,
    remove_tree_if_present,
    request_cancel,
    resolve_manifest_path,
    stop_if_cancelled,
    stop_process,
    uv_download_totals,
    verify_download,
    wait_unless_cancelled,
)


class _InstallError(RuntimeError):
    pass


@dataclass
class _State:
    downloaded_bytes: int = 0
    total_bytes: int = 0
    progress: Optional[float] = None
    download_speed_bps: float = 0.0
    eta_seconds: Optional[int] = None
    download_samples: list = field(default_factory=list)
    total_is_estimate: bool = False


@dataclass
class _Operation:
    """Only the fields install.OperationState needs."""

    state: str = "downloading"
    operation: Optional[str] = "install"
    message: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process: Optional[object] = None
    thread: Optional[threading.Thread] = None


@dataclass
class _Uv:
    version: str = "0.9.0"
    url: str = "https://example.invalid/uv"
    size: int = 0
    sha256: str = ""
    archive_type: str = "tar.gz"
    member: str = "uv-dist/uv"


def _tar_with(path: Path, member: str, payload: bytes, *, directory: bool = False) -> None:
    with tarfile.open(path, "w:gz") as bundle:
        info = tarfile.TarInfo(member)
        if directory:
            info.type = tarfile.DIRTYPE
            bundle.addfile(info)
        else:
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))


class UvLogTests(unittest.TestCase):
    def test_totals_count_only_finished_announced_downloads(self):
        log = (
            "Downloading torch (2.0MiB)\n"
            "Downloading numpy (512KiB)\r\n"
            " Downloaded numpy\n"
        )
        self.assertEqual(uv_download_totals(log), (512 * 1024, 2.5 * 1024 * 1024))

    def test_log_without_announcements_has_no_totals(self):
        self.assertIsNone(uv_download_totals("Resolved 12 packages\n"))


class DownloadProgressTests(unittest.TestCase):
    def test_speed_and_eta_follow_growth_and_report_stall(self):
        state = _State()
        begin_download_progress(state, 0, 10, 100.0)
        record_download_progress(state, 4, 10, 102.0, window_seconds=8)
        self.assertEqual(state.download_speed_bps, 2)
        self.assertEqual(state.eta_seconds, 3)
        self.assertEqual(state.progress, 0.4)
        record_download_progress(state, 4, 10, 111.0, window_seconds=8)
        self.assertEqual(state.download_speed_bps, 0)
        self.assertIsNone(state.eta_seconds)

    def test_window_drops_old_samples_but_keeps_two(self):
        state = _State()
        begin_download_progress(state, 0, 100, 0.0)
        record_download_progress(state, 50, 100, 1.0, window_seconds=8)
        record_download_progress(state, 60, 100, 20.0, window_seconds=8)
        self.assertEqual([sample[1] for sample in state.download_samples], [50, 60])
        record_download_progress(state, 70, 100, 30.0, window_seconds=None)
        self.assertEqual(len(state.download_samples), 3)

    def test_short_span_keeps_previous_estimate(self):
        state = _State()
        begin_download_progress(state, 0, 10, 0.0)
        record_download_progress(state, 5, 10, 0.2, window_seconds=8)
        self.assertEqual(state.download_speed_bps, 0.0)
        self.assertIsNone(state.eta_seconds)

    def test_unknown_total_has_no_progress_or_eta(self):
        state = _State()
        begin_download_progress(state, 0, 0, 0.0, total_is_estimate=True)
        self.assertTrue(state.total_is_estimate)
        record_download_progress(state, 8, 0, 2.0, window_seconds=30)
        self.assertIsNone(state.progress)
        self.assertEqual(state.download_speed_bps, 4)
        self.assertIsNone(state.eta_seconds)


class CopyResponseTests(unittest.TestCase):
    def test_streams_chunks_reports_totals_and_checks_cancel_each_chunk(self):
        output = io.BytesIO()
        seen = []
        checks = []
        total = copy_response_to_file(
            io.BytesIO(b"abcdefg"),
            output,
            downloaded=3,
            raise_if_cancelled=lambda: checks.append(True),
            on_progress=seen.append,
            chunk_bytes=3,
        )
        self.assertEqual(output.getvalue(), b"abcdefg")
        self.assertEqual(seen, [6, 9, 10])
        self.assertEqual(total, 10)
        self.assertEqual(len(checks), 4)


class VerifyDownloadTests(unittest.TestCase):
    def test_size_then_digest_are_checked_with_caller_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "file"
            path.write_bytes(b"payload")
            digest = hashlib.sha256(b"payload").hexdigest()
            verify_download(
                path, expected_size=7, expected_sha256=digest.upper(),
                error=_InstallError, size_message="size", digest_message="digest",
            )
            with self.assertRaisesRegex(_InstallError, "^size$"):
                verify_download(
                    path, expected_size=8, expected_sha256=digest,
                    error=_InstallError, size_message="size", digest_message="digest",
                )
            with self.assertRaisesRegex(_InstallError, "^digest$"):
                verify_download(
                    path, expected_size=7, expected_sha256="0" * 64,
                    error=_InstallError, size_message="size", digest_message="digest",
                )


class ExtractArchiveMemberTests(unittest.TestCase):
    def test_zip_and_tar_members_are_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            zipped = root / "uv.zip"
            with zipfile.ZipFile(zipped, "w") as bundle:
                bundle.writestr("uv.exe", b"zip-bytes")
            extract_archive_member(
                zipped, archive_type="zip", member="uv.exe",
                target=root / "a", error=_InstallError,
            )
            self.assertEqual((root / "a").read_bytes(), b"zip-bytes")
            tarred = root / "uv.tar.gz"
            _tar_with(tarred, "uv-dist/uv", b"tar-bytes")
            extract_archive_member(
                tarred, archive_type="tar.gz", member="uv-dist/uv",
                target=root / "b", error=_InstallError,
            )
            self.assertEqual((root / "b").read_bytes(), b"tar-bytes")

    def test_invalid_archives_raise_caller_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            zipped = root / "uv.zip"
            with zipfile.ZipFile(zipped, "w") as bundle:
                bundle.writestr("other", b"")
            with self.assertRaisesRegex(_InstallError, "缺少清单指定"):
                extract_archive_member(
                    zipped, archive_type="zip", member="uv.exe",
                    target=root / "a", error=_InstallError,
                )
            tarred = root / "uv.tar.gz"
            _tar_with(tarred, "uv-dist/uv", b"", directory=True)
            with self.assertRaisesRegex(_InstallError, "入口无效"):
                extract_archive_member(
                    tarred, archive_type="tar.gz", member="uv-dist/uv",
                    target=root / "b", error=_InstallError,
                )
            with self.assertRaisesRegex(_InstallError, "格式不受支持"):
                extract_archive_member(
                    tarred, archive_type="7z", member="uv-dist/uv",
                    target=root / "c", error=_InstallError,
                )


class InstallUvExecutableTests(unittest.TestCase):
    def _remove_tree(self, path: Path) -> None:
        import shutil

        if path.exists():
            shutil.rmtree(path)

    def test_publishes_executable_and_receipt_then_cleans_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tool_dir = root / "_tools" / "uv"
            (tool_dir).mkdir(parents=True)
            (tool_dir / "stale").write_text("old")
            staging = root / ".uv-staging"

            def download(archive: Path) -> None:
                self.assertTrue(staging.is_dir())
                _tar_with(archive, "uv-dist/uv", b"binary")

            executable = install_uv_executable(
                uv=_Uv(), tool_dir=tool_dir, staging=staging,
                archive_name="uv-archive", download=download,
                remove_tree=self._remove_tree, error=_InstallError,
                receipt={"version": "0.9.0"},
            )
            self.assertEqual(executable, tool_dir / "uv")
            self.assertEqual(executable.read_bytes(), b"binary")
            self.assertTrue((tool_dir / "receipt.json").is_file())
            self.assertFalse((tool_dir / "uv-archive").exists())
            self.assertFalse((tool_dir / "stale").exists())
            self.assertFalse(staging.exists())

    def test_failed_download_leaves_existing_tool_dir_and_no_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tool_dir = root / "_tools" / "uv"
            tool_dir.mkdir(parents=True)
            (tool_dir / "keep").write_text("old")
            staging = root / ".uv-staging"

            def download(_archive: Path) -> None:
                raise _InstallError("network down")

            with self.assertRaisesRegex(_InstallError, "network down"):
                install_uv_executable(
                    uv=_Uv(), tool_dir=tool_dir, staging=staging,
                    archive_name="archive", download=download,
                    remove_tree=self._remove_tree, error=_InstallError,
                )
            self.assertTrue((tool_dir / "keep").is_file())
            self.assertFalse(staging.exists())


class StopProcessTests(unittest.TestCase):
    def test_finished_process_is_left_alone(self):
        process = mock.Mock()
        process.poll.return_value = 0
        stop_process(process)
        process.terminate.assert_not_called()

    def test_terminate_escalates_to_kill_after_timeout(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("x", 5), 0]
        stop_process(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=5)] * 2)

    def test_process_that_vanished_during_terminate_is_ignored(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.terminate.side_effect = ProcessLookupError()
        stop_process(process)
        process.kill.assert_not_called()


class CancelSignalTests(unittest.TestCase):
    def test_check_cancelled_raises_the_callers_error_once_signalled(self):
        state = _Operation()
        check_cancelled(state, _InstallError)  # not signalled: no-op
        state.cancel_event.set()
        with self.assertRaises(_InstallError) as caught:
            check_cancelled(state, _InstallError)
        self.assertEqual(str(caught.exception), CANCELLED_MESSAGE)

    def test_wait_unless_cancelled_returns_after_the_delay(self):
        state = _Operation()
        started = time.monotonic()
        wait_unless_cancelled(state, 0.01, _InstallError)
        self.assertGreaterEqual(time.monotonic() - started, 0.005)

    def test_wait_unless_cancelled_does_not_sit_out_a_backoff(self):
        state = _Operation()
        state.cancel_event.set()
        started = time.monotonic()
        with self.assertRaises(_InstallError):
            wait_unless_cancelled(state, 30, _InstallError)
        self.assertLess(time.monotonic() - started, 1)

    def test_stop_if_cancelled_reaps_the_child_before_unwinding(self):
        state = _Operation()
        process = mock.Mock()
        process.poll.return_value = None
        stop_if_cancelled(state, process, 0.01, _InstallError)
        process.terminate.assert_not_called()
        state.cancel_event.set()
        with self.assertRaises(_InstallError):
            stop_if_cancelled(state, process, 0.01, _InstallError)
        process.terminate.assert_called_once_with()

    def test_request_cancel_signals_and_stops_only_when_active(self):
        process = mock.Mock()
        process.poll.return_value = None
        state = _Operation(state="downloading", process=process)
        self.assertTrue(request_cancel(state, threading.RLock(), {"downloading"}))
        self.assertTrue(state.cancel_event.is_set())
        self.assertEqual(state.message, CANCELLING_MESSAGE)
        process.terminate.assert_called_once_with()

    def test_request_cancel_reports_nothing_to_cancel_for_an_idle_component(self):
        state = _Operation(state="installed")
        self.assertFalse(request_cancel(state, threading.RLock(), {"downloading"}))
        self.assertFalse(state.cancel_event.is_set())
        self.assertEqual(state.message, "")


class CancelAndJoinTests(unittest.TestCase):
    def _running(self) -> tuple[_Operation, threading.Event]:
        """A worker that only ever finishes because it was cancelled.

        没有超时自解:否则"忘了发取消信号"这个退化也会让 join 成功，断言就白写了。
        """

        state = _Operation()
        released = threading.Event()

        def worker() -> None:
            state.cancel_event.wait()
            released.set()

        state.thread = threading.Thread(target=worker, daemon=True)
        state.thread.start()
        self.addCleanup(state.cancel_event.set)
        return state, released

    def test_cancels_each_worker_stops_children_and_joins(self):
        first, first_done = self._running()
        second, second_done = self._running()
        process = mock.Mock()
        process.poll.return_value = None
        first.process = process

        self.assertTrue(
            cancel_and_join_operations([first, second], threading.RLock(), 5)
        )
        self.assertTrue(first_done.is_set())
        self.assertTrue(second_done.is_set())
        process.terminate.assert_called_once_with()

    def test_idle_states_are_left_alone(self):
        idle = _Operation(operation=None)
        self.assertTrue(cancel_and_join_operations([idle], threading.RLock(), 5))
        self.assertFalse(idle.cancel_event.is_set())

    def test_before_join_runs_after_children_are_stopped(self):
        state, done = self._running()
        process = mock.Mock()
        process.poll.return_value = None
        state.process = process
        order = []
        process.terminate.side_effect = lambda: order.append("stop")

        cancel_and_join_operations(
            [state], threading.RLock(), 5, before_join=lambda: order.append("service")
        )
        self.assertEqual(order, ["stop", "service"])
        self.assertTrue(done.is_set())

    def test_worker_that_outlives_the_timeout_is_reported_unstopped(self):
        state = _Operation()
        blocked = threading.Event()
        state.thread = threading.Thread(target=lambda: blocked.wait(5), daemon=True)
        state.thread.start()
        try:
            self.assertFalse(
                cancel_and_join_operations([state], threading.RLock(), 0.05)
            )
        finally:
            blocked.set()
            state.thread.join(5)


class ManifestPathAndTreeTests(unittest.TestCase):
    def test_manifest_path_accepts_a_path_or_a_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "manifest.json"
            self.assertEqual(resolve_manifest_path(target), target)
            self.assertEqual(resolve_manifest_path(lambda: target), target)
            self.assertEqual(resolve_manifest_path(str(target)), target)

    def test_remove_tree_tolerates_a_missing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tool"
            remove_tree_if_present(target)  # missing: no error
            (target / "inner").mkdir(parents=True)
            remove_tree_if_present(target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
