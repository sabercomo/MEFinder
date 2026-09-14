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
import time
import unittest
from pathlib import Path

from src.me_finder.alignment_compute import ALIGNMENT_COMPUTE_PROTOCOL
from src.me_finder.managed_alignment_runtime import (
    ManagedAlignmentRuntime,
    ManagedAlignmentRuntimeError,
    resolve_installed_runtime_launch,
)
from src.me_finder.managed_embedding_models import ManagedEmbeddingModels


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
            "    (venv / 'bin').mkdir(parents=True, exist_ok=True)\n"
            "    (venv / 'pyvenv.cfg').write_text('include-system-site-packages = false\\n')\n"
            "    target = venv / 'bin' / 'python'\n"
            "    try:\n"
            "        target.symlink_to(sys.executable)\n"
            "    except OSError:\n"
            "        target.write_text(sys.executable)\n"
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
                    "venv_python": "venv/bin/python",
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
        active = {"value": True}
        manager = self._manager(is_compute_active=lambda: active["value"])
        manager.perform({"action": "install"})
        self.assertTrue(self._wait(manager)["installed"])
        manager.perform({"action": "uninstall"})
        # While a compute task is active the uninstall must wait and stop
        # accepting new tasks, showing the pending state.
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
        # Once the task finishes the uninstall completes on its own.
        active["value"] = False
        summary = self._wait(manager)
        self.assertFalse(summary["installed"])
        self.assertFalse(manager.runtime_dir.exists())

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


if __name__ == "__main__":
    unittest.main()
