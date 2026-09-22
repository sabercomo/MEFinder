"""Lifecycle tests for the independent alignment compute runtime component.

These exercise install / update / uninstall / validate, cross-process locking,
crash recovery and the launch resolution — all without a real network install.
A fake ``uv`` (a small Python script) builds a venv whose interpreter is the
current one, and validation is pointed at a fake worker module that completes
the protocol handshake, so the lifecycle runs on any machine. One test uses the
*real* compute worker and is skipped when the numeric stack is absent (CI), so
the real runtime-load check is still covered where the stack exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path
from urllib.error import URLError

from src.me_finder.alignment_compute import ALIGNMENT_COMPUTE_PROTOCOL
import src.me_finder.managed_alignment_runtime as align_runtime
from src.me_finder.managed_alignment_runtime import (
    ManagedAlignmentRuntime,
    ManagedAlignmentRuntimeError,
    _TUNA_PYPI_INDEX,
    _TUNA_PYTHON_INSTALL_MIRROR,
    _uv_mirror_url,
    resolve_installed_runtime_launch,
)
from src.me_finder.managed_embedding_models import ManagedEmbeddingModels


class _FakeResponse:
    """Minimal context-manager HTTP response for the download tests."""

    def __init__(self, body: bytes, *, status: int = 200, cut: int | None = None,
                 fail: Exception | None = None):
        self._body = body
        self._pos = 0
        self._cut = cut if cut is not None else len(body)
        self._fail = fail
        self.status = status

    def read(self, size: int) -> bytes:
        if self._pos >= self._cut:
            if self._fail is not None:
                raise self._fail
            return b""
        chunk = self._body[self._pos:min(self._pos + size, self._cut)]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ScriptedOpener:
    """opener(request, timeout=...) driven by a per-URL response factory."""

    def __init__(self, factory):
        self._factory = factory
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, request, timeout=None):
        rng = request.headers.get("Range")
        self.calls.append((request.full_url, rng))
        response = self._factory(request.full_url, rng, len(self.calls))
        if isinstance(response, Exception):
            raise response
        return response


def _process_launcher(command, **kwargs):
    executable = Path(command[0])
    if sys.platform == "win32" and executable.suffix == "" and executable.read_bytes().startswith(b"#!"):
        command = [sys._base_executable, *command]
    return subprocess.Popen(command, **kwargs)


class ManagedAlignmentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime 中文"
        # A fake worker module the staged interpreter can import and that always
        # reports the compute capabilities, so validation does not need the real
        # numeric stack.
        self.worker_root = self.root / "worker-src"
        self.worker_root.mkdir()
        (self.worker_root / "fakeworker.py").write_text(
            "import json, sys\n"
            "positional = [a for a in sys.argv[1:] if not a.startswith('--')]\n"
            "if '--download-model' in sys.argv:\n"
            "    control = positional[2]\n"
            "    open(positional[1] + '.downloaded', 'w').close()\n"
            "    with open(control, 'a', encoding='utf-8') as fh:\n"
            "        fh.write(json.dumps({'type': 'result', 'model_id': positional[0]}) + '\\n')\n"
            "    raise SystemExit(0)\n"
            "control = positional[0]\n"
            "caps = {'numpy': True, 'fastembed': True, 'onnxruntime': True}\n"
            f"protocol = {ALIGNMENT_COMPUTE_PROTOCOL}\n"
            "with open(control, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps({'type': 'hello', 'protocol': protocol, 'capabilities': caps}) + '\\n')\n",
            encoding="utf-8",
        )

    def _worker_context(self):
        return ("fakeworker", self.worker_root)

    def _manifest(self, *, packages=None) -> Path:
        uv = self.root / "uv"
        uv.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1] == 'venv':\n"
            "    venv = Path(sys.argv[-1])\n"
            "    import venv as venv_module\n"
            "    venv_module.EnvBuilder(with_pip=False).create(venv)\n"
            "elif sys.argv[1:3] == ['pip', 'install']:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        uv.chmod(0o755)
        archive = self.root / "uv.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(uv, arcname="fake-uv/uv")
        payload = {
            "schema_version": 1,
            "uv_version": "test",
            "alignment": {
                "version": "1",
                "python": "3.11",
                "packages": packages
                or ["numpy==2.5.2", "onnxruntime==1.29.0", "fastembed==0.8.0"],
            },
            "engines": {},
            "platforms": {
                "test-platform": {
                    "python": "3.11",
                    "venv_python": "venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python",
                    "onnxruntime": "onnxruntime==1.29.0",
                    "uv": {
                        "url": archive.as_uri(),
                        "size": archive.stat().st_size,
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "archive_type": "tar.gz",
                        "member": "fake-uv/uv",
                    },
                    "notes": "test",
                }
            },
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _manager(self, *, manifest=None, models=None, is_compute_active=None):
        return ManagedAlignmentRuntime(
            self.runtime,
            manifest_path=manifest or self._manifest(),
            platform_key="test-platform",
            process_launcher=_process_launcher,
            models_component=models,
            is_compute_active=is_compute_active,
            worker_context=self._worker_context,
        )

    def _wait(self, manager, timeout=20) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            summary = manager.summary()
            if summary["operation"] is None:
                return summary
            time.sleep(0.02)
        self.fail("managed alignment runtime operation did not finish")

    def test_install_publishes_runtime_with_receipt_and_resolves_launch(self) -> None:
        manager = self._manager()
        self.assertFalse(manager.summary()["installed"])
        manager.perform({"action": "install"})
        summary = self._wait(manager)
        self.assertTrue(summary["installed"], summary)
        self.assertEqual(summary["error"], "")
        self.assertFalse(summary["update_available"])
        receipt = json.loads((manager.runtime_dir / "installed.json").read_text("utf-8"))
        self.assertEqual(receipt["protocol"], ALIGNMENT_COMPUTE_PROTOCOL)
        self.assertIn("identity", receipt)
        # The launch resolver now points at the installed interpreter with the
        # worker source on PYTHONPATH.
        launch = resolve_installed_runtime_launch(
            self.runtime,
            platform_key="test-platform",
            manifest_path=self._manifest(),
            worker_context=self._worker_context,
        )
        self.assertIsNotNone(launch)
        self.assertEqual(launch.command[1:], ("-m", "fakeworker"))
        self.assertIn(str(self.worker_root), launch.env["PYTHONPATH"])

    def test_launch_resolves_none_when_not_installed(self) -> None:
        launch = resolve_installed_runtime_launch(
            self.runtime,
            platform_key="test-platform",
            manifest_path=self._manifest(),
            worker_context=self._worker_context,
        )
        self.assertIsNone(launch)

    def test_half_written_directory_is_not_reported_installed(self) -> None:
        manager = self._manager()
        # A runtime dir with an interpreter but no receipt (crash before the
        # receipt was written) must read as not installed.
        python = manager.runtime_dir / "venv" / "bin"
        python.mkdir(parents=True)
        (python / "python").write_text("")
        self.assertFalse(manager.summary()["installed"])
        launch = resolve_installed_runtime_launch(
            self.runtime,
            platform_key="test-platform",
            manifest_path=self._manifest(),
            worker_context=self._worker_context,
        )
        self.assertIsNone(launch)

    def test_receipt_without_interpreter_is_not_installed(self) -> None:
        manager = self._manager()
        manager.runtime_dir.mkdir(parents=True)
        (manager.runtime_dir / "installed.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "protocol": ALIGNMENT_COMPUTE_PROTOCOL,
                    "identity": "abc",
                    "runtime_version": "1",
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(manager.summary()["installed"])

    def test_cross_process_lock_blocks_concurrent_operation(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX flock probe")
        import fcntl

        manager = self._manager()
        manager.component_root.mkdir(parents=True, exist_ok=True)
        lock_path = manager.component_root / ".operation.lock"
        handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            manager.perform({"action": "install"})
            summary = self._wait(manager)
            self.assertFalse(summary["installed"])
            self.assertIn("另一个进程", summary["error"])
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def test_update_available_when_pins_change_and_update_swaps(self) -> None:
        manager = self._manager()
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        # A new manifest with a different pin set makes an update available.
        new_manifest = self._manifest(
            packages=["numpy==2.5.3", "onnxruntime==1.29.0", "fastembed==0.8.0"]
        )
        manager._manifest_path = new_manifest
        manager.refresh_manifest()
        self.assertTrue(manager.summary()["update_available"])
        manager.perform({"action": "update"})
        summary = self._wait(manager)
        self.assertTrue(summary["installed"])
        self.assertFalse(summary["update_available"])

    def test_uninstall_removes_runtime_and_models(self) -> None:
        models = ManagedEmbeddingModels(self.runtime)
        # Populate a models cache so uninstall has something to delete.
        models_dir = models._cache_dir
        (models_dir / "models--fake").mkdir(parents=True)
        (models_dir / "models--fake" / "blob.bin").write_bytes(b"weights")
        manager = self._manager(models=models)
        self.assertTrue(manager.models_dir.exists())
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        manager.perform({"action": "uninstall"})
        summary = self._wait(manager)
        self.assertFalse(summary["installed"], summary)
        self.assertFalse(manager.runtime_dir.exists())
        self.assertFalse((models_dir / "models--fake").exists())

    def test_uninstall_defers_until_compute_task_finishes(self) -> None:
        from src.me_finder.managed_alignment_runtime import compute_admission

        manager = self._manager()
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        # Simulate an in-flight compute task holding the shared compute lease,
        # exactly as the coordinator does around a real computation.
        holding = threading.Event()
        release = threading.Event()

        def _task():
            with compute_admission(self.runtime):
                holding.set()
                release.wait(10)

        worker = threading.Thread(target=_task, daemon=True)
        worker.start()
        self.assertTrue(holding.wait(5))
        manager.perform({"action": "uninstall"})
        # While the task holds the lease, uninstall must wait (pending) and a new
        # task must be refused — the runtime is not torn out mid-compute.
        deadline = time.monotonic() + 5
        saw_pending = False
        while time.monotonic() < deadline:
            summary = manager.summary()
            if summary.get("uninstall_deferred"):
                saw_pending = True
                self.assertEqual(summary["state"], "uninstall_pending")
                break
            time.sleep(0.02)
        self.assertTrue(saw_pending, "uninstall did not defer while task active")
        self.assertTrue(manager.runtime_dir.exists())
        with self.assertRaises(Exception):
            with compute_admission(self.runtime):
                pass
        # Once the task finishes the uninstall completes on its own.
        release.set()
        worker.join(5)
        summary = self._wait(manager)
        self.assertFalse(summary["installed"])
        self.assertFalse(manager.runtime_dir.exists())

    def test_compute_status_reports_builtin_when_stack_present(self) -> None:
        import src.me_finder.managed_alignment_runtime as mod

        manager = self._manager()
        # Not-installed independent runtime, but the app's own stack is present:
        # available via the bundled runtime (an existing user is not told to
        # install anything).
        with mock.patch.object(mod, "_builtin_stack_present", lambda: True):
            status = manager.compute_status()
        self.assertEqual(status, {"available": True, "provider": "builtin", "detail": ""})

    def test_compute_status_unavailable_when_no_runtime_and_no_stack(self) -> None:
        import src.me_finder.managed_alignment_runtime as mod

        manager = self._manager()
        with mock.patch.object(mod, "_builtin_stack_present", lambda: False):
            status = manager.compute_status()
        self.assertEqual(status, {"available": False, "provider": "none", "detail": ""})

    def test_compute_status_prefers_installed_independent_runtime(self) -> None:
        import src.me_finder.managed_alignment_runtime as mod

        manager = self._manager()
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        # Even with no bundled stack, an installed independent runtime is the
        # provider.
        with mock.patch.object(mod, "_builtin_stack_present", lambda: False):
            status = manager.compute_status()
        self.assertEqual(status, {"available": True, "provider": "independent", "detail": ""})

    def test_unsupported_platform_rejects_install(self) -> None:
        manager = ManagedAlignmentRuntime(
            self.runtime,
            manifest_path=self._manifest(),
            platform_key="unknown-platform",
            process_launcher=_process_launcher,
            worker_context=self._worker_context,
        )
        self.assertFalse(manager.summary()["supported"])
        with self.assertRaises(ManagedAlignmentRuntimeError):
            manager.perform({"action": "install"})

    def test_install_cleans_stale_staging_directory(self) -> None:
        manager = self._manager()
        manager.component_root.mkdir(parents=True, exist_ok=True)
        stale = manager.component_root / ".staging-orphan"
        stale.mkdir()
        (stale / "junk").write_text("x")
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        self.assertFalse(stale.exists())

    def test_real_worker_probe_rejects_stackless_runtime(self) -> None:
        # Validation must be a real runtime-load check, not file existence: the
        # *real* compute worker probes the staged interpreter, and an isolated
        # venv without the numeric stack must fail the install (no receipt, no
        # published runtime), proving validation actually loads the runtime.
        manager = ManagedAlignmentRuntime(
            self.runtime,
            manifest_path=self._manifest(),
            platform_key="test-platform",
            process_launcher=_process_launcher,
        )
        manager.perform({"action": "install"})
        summary = self._wait(manager)
        self.assertFalse(summary["installed"], summary)
        self.assertIn("计算依赖", summary["error"])
        self.assertFalse(manager.runtime_dir.exists())

    # --- Astra review regressions --------------------------------------- #
    def _manifest_without_alignment(self) -> Path:
        manifest = json.loads(self._manifest().read_text(encoding="utf-8"))
        manifest.pop("alignment", None)
        path = self.root / "manifest-no-alignment.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_manifest_without_alignment_block_does_not_break_construction(self) -> None:
        # [P1] An older/remote-cached catalog with no `alignment` block must not
        # abort startup: the component constructs, reports unsupported, refuses
        # install, and still reports compute available via the bundled stack.
        import src.me_finder.managed_alignment_runtime as mod

        manager = ManagedAlignmentRuntime(
            self.runtime,
            manifest_path=self._manifest_without_alignment(),
            platform_key="test-platform",
            process_launcher=_process_launcher,
            worker_context=self._worker_context,
        )
        summary = manager.summary()
        self.assertFalse(summary["supported"])
        self.assertFalse(summary["installed"])
        with self.assertRaises(ManagedAlignmentRuntimeError):
            manager.perform({"action": "install"})
        with mock.patch.object(mod, "_builtin_stack_present", lambda: True):
            self.assertTrue(manager.compute_status()["available"])

    def test_recovers_previous_runtime_after_interrupted_swap(self) -> None:
        # [P2] A crash between final→.previous and staging→final leaves no
        # runtime/ but an intact .previous-*; construction must restore it.
        manager = self._manager()
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        # Simulate the interruption: move runtime/ aside to a .previous-* dir.
        previous = manager.component_root / ".previous-deadbeef"
        manager.runtime_dir.replace(previous)
        self.assertFalse(manager.runtime_dir.exists())
        # A fresh component (restart) must recover the previous runtime.
        recovered = self._manager()
        self.assertTrue(recovered.runtime_dir.exists())
        self.assertTrue(recovered.summary()["installed"])
        self.assertFalse(previous.exists())

    def test_recovers_previous_runtime_when_manifest_is_unreadable(self) -> None:
        """Recovery must not strand a valid previous runtime behind a bad catalog."""
        manager = self._manager()
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        previous = manager.component_root / ".previous-corrupt-manifest"
        manager.runtime_dir.replace(previous)
        broken_manifest = self.root / "broken-manifest.json"
        broken_manifest.write_text("{", encoding="utf-8")
        recovered = ManagedAlignmentRuntime(
            self.runtime,
            manifest_path=broken_manifest,
            platform_key="test-platform",
            process_launcher=_process_launcher,
            worker_context=self._worker_context,
        )
        self.assertTrue(recovered.runtime_dir.exists())
        self.assertFalse(previous.exists())

    def test_compute_status_matches_launch_on_incompatible_receipt(self) -> None:
        # [P2] An installed receipt with an incompatible protocol must not read
        # as "可用 · 独立运行时": status must track the real launch condition.
        import src.me_finder.managed_alignment_runtime as mod

        manager = self._manager()
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        receipt_path = manager.runtime_dir / "installed.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["protocol"] = ALIGNMENT_COMPUTE_PROTOCOL + 999
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with mock.patch.object(mod, "_builtin_stack_present", lambda: True):
            status = manager.compute_status()
        self.assertEqual(status["provider"], "builtin")
        self.assertIn("不兼容", status["detail"])
        with mock.patch.object(mod, "_builtin_stack_present", lambda: False):
            status = manager.compute_status()
        self.assertFalse(status["available"])
        self.assertIn("不兼容", status["detail"])

    def test_model_downloader_routes_through_installed_runtime(self) -> None:
        # [P2] With an independent runtime installed, model download runs in that
        # runtime (subprocess), not the main process.
        from src.me_finder.managed_alignment_runtime import make_model_downloader

        manager = self._manager()
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        downloader = make_model_downloader(
            self.runtime,
            platform_key="test-platform",
            manifest_path=self._manifest(),
            worker_context=self._worker_context,
            process_launcher=_process_launcher,
        )
        cache_dir = self.root / "modelcache"
        downloader("minilm-l12-v2", cache_dir)
        # The fake worker records the download via a sibling marker file.
        self.assertTrue(Path(str(cache_dir) + ".downloaded").exists())

    def test_model_downloader_falls_back_in_process_without_runtime(self) -> None:
        import src.me_finder.managed_embedding_models as emod
        from src.me_finder.managed_alignment_runtime import make_model_downloader

        calls = []
        with mock.patch.object(
            emod, "download_embedding_model", lambda model_id, cache: calls.append(model_id)
        ), mock.patch("src.me_finder.managed_alignment_runtime._builtin_stack_present", return_value=True):
            downloader = make_model_downloader(
                self.runtime,
                platform_key="test-platform",
                manifest_path=self._manifest(),
                worker_context=self._worker_context,
                process_launcher=_process_launcher,
            )
            downloader("minilm-l12-v2", self.root / "modelcache")
        self.assertEqual(calls, ["minilm-l12-v2"])

    def test_close_reaps_in_flight_install_subprocess(self) -> None:
        # [P1] Shutdown must cancel and reap an in-flight install subprocess.
        sleeper_cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

        def _sleeping_launcher(command, **kwargs):
            return subprocess.Popen(sleeper_cmd, **kwargs)

        manager = ManagedAlignmentRuntime(
            self.runtime,
            manifest_path=self._manifest(),
            platform_key="test-platform",
            process_launcher=_sleeping_launcher,
            worker_context=self._worker_context,
        )
        manager.perform({"action": "install"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and manager._state.process is None:
            time.sleep(0.02)
        process = manager._state.process
        self.assertIsNotNone(process, "install subprocess did not start")
        thread = manager._state.thread
        manager.close()
        self.assertIsNotNone(process.poll(), "install subprocess was not reaped")
        if thread is not None:
            self.assertFalse(thread.is_alive())

    def test_compute_admission_refused_under_maintenance(self) -> None:
        from src.me_finder.managed_alignment_runtime import (
            ComputeUnavailable,
            compute_admission,
        )

        manager = self._manager()
        manager.component_root.mkdir(parents=True, exist_ok=True)
        (manager.component_root / ".maintenance").write_text("pid=1", encoding="utf-8")
        with self.assertRaises(ComputeUnavailable):
            with compute_admission(self.runtime):
                pass


class AlignmentWorkerVerifyTests(unittest.TestCase):
    def test_verify_rejects_dependency_that_imports_but_fails(self) -> None:
        # [P1] find_spec passing is not enough: a stub whose import raises must
        # fail --verify (real load), proving validation isn't find_spec-only.
        with tempfile.TemporaryDirectory() as directory:
            stub_root = Path(directory)
            for name in ("numpy", "onnxruntime", "fastembed"):
                (stub_root / f"{name}.py").write_text(
                    "raise ImportError('broken wheel')\n", encoding="utf-8"
                )
            control = stub_root / "control.ndjson"
            control.write_text("", encoding="utf-8")
            repo_root = Path(__file__).resolve().parents[1]
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join([str(stub_root), str(repo_root)])
            result = subprocess.run(
                [sys.executable, "-m", "src.me_finder.alignment_compute_worker",
                 "--verify", str(control)],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
            )
            messages = [
                json.loads(line)
                for line in control.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        self.assertEqual(result.returncode, 4, result.stderr.decode(errors="replace"))
        self.assertTrue(any(m.get("type") == "error" for m in messages))
        self.assertFalse(any(m.get("type") == "hello" for m in messages))


class AlignmentDownloadRobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "uv.archive"
        self.payload = b"".join(bytes([i % 256]) for i in range(4096))
        self.size = len(self.payload)
        self.sha = hashlib.sha256(self.payload).hexdigest()

    def _manager(self) -> ManagedAlignmentRuntime:
        # A bare instance is enough for the download seam; the manifest block is
        # absent (unconfigured), which never touches the download path.
        manifest = self.root / "empty-manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "platforms": {}}), encoding="utf-8")
        return ManagedAlignmentRuntime(
            self.root / "runtime",
            manifest_path=manifest,
            platform_key="test-platform",
        )

    def test_resumes_after_transient_read_timeout(self) -> None:
        def factory(url, rng, call_index):
            if rng is None:
                # First attempt: deliver 1500 bytes, then time out mid-stream.
                return _FakeResponse(self.payload, cut=1500,
                                     fail=TimeoutError("The read operation timed out"))
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
            return _FakeResponse(self.payload[start:], status=206)

        opener = _ScriptedOpener(factory)
        manager = self._manager()
        manager.opener = opener
        with mock.patch.object(manager, "_backoff_sleep"):
            manager._download_file(["http://mirror/uv"], self.target, self.size, self.sha)

        self.assertEqual(self.target.read_bytes(), self.payload)
        # Two calls: the failed first read, then a Range-resumed continuation.
        self.assertEqual(len(opener.calls), 2)
        self.assertIsNone(opener.calls[0][1])
        self.assertEqual(opener.calls[1][1], "bytes=1500-")

    def test_falls_back_from_mirror_to_official(self) -> None:
        def factory(url, rng, call_index):
            if "mirror" in url:
                return URLError("mirror unreachable")
            return _FakeResponse(self.payload)

        opener = _ScriptedOpener(factory)
        manager = self._manager()
        manager.opener = opener
        with mock.patch.object(manager, "_backoff_sleep"):
            manager._download_file(
                ["http://mirror/uv", "http://official/uv"], self.target, self.size, self.sha
            )

        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertTrue(any("mirror" in url for url, _ in opener.calls))
        self.assertTrue(any("official" in url for url, _ in opener.calls))

    def test_integrity_mismatch_moves_to_next_candidate_without_retry(self) -> None:
        def factory(url, rng, call_index):
            if "bad" in url:
                # Correct length but wrong bytes: SHA-256 must reject it.
                return _FakeResponse(b"\x00" * self.size)
            return _FakeResponse(self.payload)

        opener = _ScriptedOpener(factory)
        manager = self._manager()
        manager.opener = opener
        with mock.patch.object(manager, "_backoff_sleep") as sleep:
            manager._download_file(
                ["http://bad/uv", "http://good/uv"], self.target, self.size, self.sha
            )

        self.assertEqual(self.target.read_bytes(), self.payload)
        # The bad candidate is not retried on the same URL (integrity, not network).
        bad_calls = [url for url, _ in opener.calls if "bad" in url]
        self.assertEqual(len(bad_calls), 1)
        sleep.assert_not_called()

    def test_all_sources_failing_raises_aggregated_error(self) -> None:
        opener = _ScriptedOpener(lambda url, rng, i: URLError("down"))
        manager = self._manager()
        manager.opener = opener
        with mock.patch.object(manager, "_backoff_sleep"):
            with self.assertRaises(ManagedAlignmentRuntimeError) as ctx:
                manager._download_file(
                    ["http://mirror/uv", "http://official/uv"], self.target, self.size, self.sha
                )
        self.assertIn("镜像与官方源", str(ctx.exception))


class AlignmentMirrorConfigTests(unittest.TestCase):
    def test_uv_mirror_url_maps_official_host(self) -> None:
        official = "https://releases.astral.sh/github/uv/releases/download/0.12.1/uv-x.tar.gz"
        mirror = _uv_mirror_url(official, "0.12.1")
        self.assertEqual(
            mirror,
            "https://mirrors.tuna.tsinghua.edu.cn/github-release/astral-sh/uv/0.12.1/uv-x.tar.gz",
        )

    def test_uv_mirror_url_skips_non_official_and_file_urls(self) -> None:
        self.assertIsNone(_uv_mirror_url("file:///tmp/uv.tar.gz", "0.12.1"))
        self.assertIsNone(_uv_mirror_url("https://example.com/uv.tar.gz", "0.12.1"))

    def test_candidates_prefer_mirror_then_official_when_enabled(self) -> None:
        platform = types.SimpleNamespace(
            uv=types.SimpleNamespace(
                url="https://releases.astral.sh/github/uv/releases/download/0.12.1/uv.tar.gz",
                version="0.12.1",
            )
        )
        manager = ManagedAlignmentRuntime.__new__(ManagedAlignmentRuntime)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEFINDER_ALIGNMENT_MIRROR", None)
            candidates = manager._uv_download_candidates(platform)
        self.assertEqual(len(candidates), 2)
        self.assertIn("tuna", candidates[0])
        self.assertEqual(candidates[1], platform.uv.url)

    def test_candidates_official_only_when_mirror_disabled(self) -> None:
        platform = types.SimpleNamespace(
            uv=types.SimpleNamespace(
                url="https://releases.astral.sh/github/uv/releases/download/0.12.1/uv.tar.gz",
                version="0.12.1",
            )
        )
        manager = ManagedAlignmentRuntime.__new__(ManagedAlignmentRuntime)
        with mock.patch.dict(os.environ, {"MEFINDER_ALIGNMENT_MIRROR": "off"}):
            candidates = manager._uv_download_candidates(platform)
        self.assertEqual(candidates, [platform.uv.url])

    def test_install_environment_sets_mirror_only_when_requested(self) -> None:
        manager = ManagedAlignmentRuntime.__new__(ManagedAlignmentRuntime)
        manager.component_root = Path("/tmp/component-root")
        staging = Path("/tmp/staging")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UV_DEFAULT_INDEX", None)
            os.environ.pop("UV_PYTHON_INSTALL_MIRROR", None)
            mirror_env = manager._install_environment(staging, use_mirror=True)
            official_env = manager._install_environment(staging, use_mirror=False)
        self.assertEqual(mirror_env["UV_DEFAULT_INDEX"], _TUNA_PYPI_INDEX)
        self.assertEqual(mirror_env["UV_PYTHON_INSTALL_MIRROR"], _TUNA_PYTHON_INSTALL_MIRROR)
        self.assertNotIn("UV_DEFAULT_INDEX", official_env)
        self.assertNotIn("UV_PYTHON_INSTALL_MIRROR", official_env)


class AlignmentUvStepFallbackTests(unittest.TestCase):
    def test_uv_step_retries_official_after_mirror_failure(self) -> None:
        manager = ManagedAlignmentRuntime.__new__(ManagedAlignmentRuntime)
        manager.component_root = Path("/tmp/component-root")
        manager._raise_if_cancelled = lambda: None
        seen_mirror_flags: list[bool] = []
        cleanup_calls: list[int] = []

        def fake_run(command, *, cwd, environment, log_path, timeout):
            uses_mirror = "UV_DEFAULT_INDEX" in environment
            seen_mirror_flags.append(uses_mirror)
            if uses_mirror:
                raise ManagedAlignmentRuntimeError("mirror install failed")

        manager._run_command = fake_run
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEFINDER_ALIGNMENT_MIRROR", None)
            manager._run_uv_command(
                ["uv", "pip", "install"],
                staging=Path("/tmp/staging"),
                cwd=Path("/tmp/staging"),
                log_path=Path("/tmp/staging/install.log"),
                timeout=10,
                cleanup=lambda: cleanup_calls.append(1),
            )
        self.assertEqual(seen_mirror_flags, [True, False])
        self.assertEqual(cleanup_calls, [1])


if __name__ == "__main__":
    unittest.main()
