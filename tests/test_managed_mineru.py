from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.managed_mineru import (
    ManagedMinerU,
    ManagedMinerUError,
    detect_mineru_hardware,
    load_managed_mineru_manifest,
    resolve_managed_mineru_version,
)
from src.me_finder.mineru_local_settings import mineru_local_config_summary


def _test_process_launcher(command, **kwargs):
    executable = Path(command[0])
    if sys.platform == "win32" and executable.read_bytes().startswith(b"#!"):
        # Launch the interpreter directly, not the venv redirector whose child
        # can retain service.log briefly after the tracked process is stopped.
        command = [sys._base_executable, *command]
    return subprocess.Popen(command, **kwargs)


class ManagedMinerUTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime 中文"
        self.config = self.runtime / "config/mineru.json"

    def _manifest(self) -> Path:
        uv = self.root / "uv"
        uv.write_text(
            """#!/usr/bin/env python3
from pathlib import Path
import os, stat, sys
if sys.argv[1] == 'venv':
    venv = Path(sys.argv[-1])
    (venv / 'bin').mkdir(parents=True)
    (venv / 'bin/python').symlink_to(sys.executable)
elif sys.argv[1:3] == ['pip', 'install']:
    python = Path(sys.argv[sys.argv.index('--python') + 1])
    bindir = python.parent
    downloader = bindir / 'mineru-models-download'
    downloader.write_text('''#!/usr/bin/env python3
from pathlib import Path
import json, os
home = os.environ.get("MINERU_HOME")
if home:
    # MinerU 4.x：一切路径从 MINERU_HOME 派生，不写 mineru.json
    models = Path(home) / "models" / "fake"
    models.mkdir(parents=True, exist_ok=True)
    (models / "weights.bin").write_bytes(b"weights")
else:
    config = Path(os.environ["MINERU_TOOLS_CONFIG_JSON"])
    models = config.parent / "models" / "fake"
    models.mkdir(parents=True, exist_ok=True)
    (models / "weights.bin").write_bytes(b"weights")
    config.write_text(json.dumps({"models-dir": {"pipeline": str(models), "vlm": str(models)}}))
''')
    api = bindir / 'mineru-api'
    api.write_text('''#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
if "--help" in sys.argv:
    raise SystemExit(0)
port = int(sys.argv[sys.argv.index("--port") + 1])
# 4.x 只有 /v1/health，3.x 只有 /health：按安装形态决定，逼真到能被探测
healthy = "/v1/health" if os.environ.get("MINERU_HOME") else "/health"
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_GET(self):
        raw = json.dumps({"protocol_version": "test", "version": "test"}).encode()
        self.send_response(200 if self.path == healthy else 404)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
''')
    for script in (downloader, api):
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
else:
    raise SystemExit(2)
""",
            encoding="utf-8",
        )
        uv.chmod(0o755)
        archive = self.root / "uv.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(uv, arcname="fake-uv/uv")
        payload = {
            "schema_version": 1,
            "uv_version": "test",
            "engines": {},
            "mineru": {
                "version": "3.4.5",
                "python": "3.12",
                "auto_upgrade": {
                    "minimum": "3.4.5",
                    "below": "5.0.0",
                    "metadata_url": "https://pypi.example/mineru/json",
                },
                "series": {
                    "3": {
                        "config_style": "tools_json",
                        "model_download_args": ["-s", "auto", "-m", "{model_type}"],
                        "packages": {
                            "pipeline": {
                                "default": ["mineru[pipeline]=={version}"]
                            },
                            "vlm": {
                                "test-platform": ["mineru[core,vllm]=={version}"]
                            },
                        },
                    },
                    "4": {
                        "config_style": "mineru_home",
                        "model_download_args": ["-s", "auto", "--tier", "{tier}"],
                        "packages": {
                            "pipeline": {"default": ["mineru=={version}"]},
                            "vlm": {
                                "test-platform": [
                                    "mineru[full]=={version}",
                                    "mlx-vlm>=0.7.0,<0.8.0",
                                ]
                            },
                        },
                    },
                },
                "profiles": {
                    "pipeline": {
                        "display_name": "Pipeline",
                        "model_type": "pipeline",
                        "backend": "pipeline",
                        "tier": "basic",
                        "minimum_memory_gb": 16,
                        "minimum_disk_gb": 20,
                    },
                    "vlm": {
                        "display_name": "VLM",
                        "model_type": "vlm",
                        "backend": "vlm-auto-engine",
                        "tier": "standard",
                        "minimum_memory_gb": 16,
                        "minimum_disk_gb": 20,
                        "minimum_vram_gb": 8,
                    },
                },
                "platforms": {"test-platform": ["pipeline", "vlm"]},
            },
            "platforms": {
                "test-platform": {
                    "python": "3.12",
                    "venv_python": "venv/bin/python",
                    "onnxruntime": "unused",
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

    def _manager(self, **overrides) -> ManagedMinerU:
        # Windows keeps service.log open until the service is stopped, so the
        # temporary directory cannot be removed while one is still running.
        manager = ManagedMinerU(
            self.runtime,
            self.config,
            manifest_path=self._manifest(),
            platform_key="test-platform",
            process_launcher=_test_process_launcher,
            hardware_detector=lambda: {
                "kind": "nvidia",
                "name": "Test GPU",
                "vlm_supported": True,
                "recommended_profile": "vlm",
                "vram_mb": 24576,
                "compute_capability": 8.9,
            },
            **overrides,
        )
        self.addCleanup(manager.close)
        return manager

    def _wait(self, manager: ManagedMinerU, profile: str, timeout: float = 20) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = next(
                value
                for value in manager.summary()["profiles"]
                if value["profile"] == profile
            )
            if item["operation"] is None:
                return item
            time.sleep(0.05)
        self.fail("managed MinerU operation did not finish")

    def test_manifest_is_version_pinned_and_exposes_both_profiles(self) -> None:
        manifest = load_managed_mineru_manifest(
            self._manifest(), platform_key="test-platform"
        )
        self.assertEqual(manifest.version, "3.4.5")
        self.assertEqual(manifest.supported_profiles, ("pipeline", "vlm"))
        self.assertEqual(manifest.profiles["vlm"].backend, "vlm-auto-engine")
        self.assertGreater(manifest.profiles["vlm"].model_download_bytes, 2 * 1024**3)

    def test_series_recipes_resolve_per_version_and_platform(self) -> None:
        path = self._manifest()
        three = load_managed_mineru_manifest(path, platform_key="test-platform")
        self.assertEqual(three.version, "3.4.5")
        self.assertEqual(three.series, "3")
        self.assertEqual(three.config_style, "tools_json")
        self.assertEqual(
            three.profiles["vlm"].packages, ("mineru[core,vllm]==3.4.5",)
        )
        four = load_managed_mineru_manifest(
            path, platform_key="test-platform", version="4.0.4"
        )
        self.assertEqual(four.series, "4")
        self.assertEqual(four.config_style, "mineru_home")
        self.assertEqual(
            four.profiles["vlm"].packages,
            ("mineru[full]==4.0.4", "mlx-vlm>=0.7.0,<0.8.0"),
        )
        self.assertEqual(four.profiles["pipeline"].packages, ("mineru==4.0.4",))
        self.assertEqual(
            four.model_download_args, ("-s", "auto", "--tier", "{tier}")
        )

    def test_unsupported_major_series_is_refused(self) -> None:
        with self.assertRaises(ManagedMinerUError):
            load_managed_mineru_manifest(
                self._manifest(), platform_key="test-platform", version="9.0.0"
            )

    def test_version_resolution_picks_newest_in_range(self) -> None:
        payload = json.dumps(
            {
                "releases": {
                    "3.4.4": [{"filename": "a"}],
                    "3.4.5": [{"filename": "b"}],
                    "4.0.4": [{"filename": "c"}],
                    "4.9.9": [{"filename": "d", "yanked": True}],
                    "5.0.0": [{"filename": "e"}],
                    "nightly": [{"filename": "f"}],
                }
            }
        ).encode("utf-8")
        resolution = resolve_managed_mineru_version(
            self._manifest(),
            platform_key="test-platform",
            fetch=lambda url: payload,
        )
        self.assertEqual(resolution["resolved"], "4.0.4")
        self.assertEqual(resolution["source"], "pypi")

    def test_version_resolution_falls_back_to_pinned_when_offline(self) -> None:
        def offline(url: str) -> bytes:
            raise OSError("network unreachable")

        resolution = resolve_managed_mineru_version(
            self._manifest(), platform_key="test-platform", fetch=offline
        )
        self.assertEqual(resolution["resolved"], "3.4.5")
        self.assertEqual(resolution["source"], "manifest")
        self.assertIn("PyPI", str(resolution["detail"]))

    def test_check_updates_retargets_manifest_without_touching_installs(self) -> None:
        payload = json.dumps(
            {"releases": {"3.4.5": [{"filename": "a"}], "4.0.4": [{"filename": "b"}]}}
        ).encode("utf-8")
        manager = ManagedMinerU(
            self.runtime,
            self.config,
            manifest_path=self._manifest(),
            platform_key="test-platform",
            process_launcher=_test_process_launcher,
            hardware_detector=lambda: {
                "kind": "nvidia",
                "name": "Test GPU",
                "vlm_supported": True,
                "recommended_profile": "vlm",
                "vram_mb": 24576,
                "compute_capability": 8.9,
            },
            version_fetch=lambda url: payload,
        )
        self.assertEqual(manager.summary()["version"], "3.4.5")
        summary = manager.perform({"action": "check-updates"})
        self.assertEqual(summary["version"], "4.0.4")
        self.assertEqual(summary["pinned_version"], "3.4.5")
        self.assertEqual(summary["series"], "4")
        self.assertEqual(summary["version_source"], "pypi")

    def _fake_receipt(self, profile: str, payload: dict) -> Path:
        root = self.runtime / "components/mineru" / profile
        (root / "venv/bin").mkdir(parents=True, exist_ok=True)
        (root / "venv/bin/python").write_text("", encoding="utf-8")
        (root / "venv/bin/mineru-api").write_text("", encoding="utf-8")
        (root / "installed.json").write_text(json.dumps(payload), encoding="utf-8")
        return root

    def test_installed_component_is_read_back_by_its_own_receipt(self) -> None:
        """A 4.x install must stay recognized while the target is still 3.x."""

        root = self._fake_receipt(
            "pipeline",
            {
                "schema_version": 1,
                "profile": "pipeline",
                "version": "4.0.4",
                "series": "4",
                "config_style": "mineru_home",
                "backend": "pipeline",
                "packages": ["mineru==4.0.4"],
                "platform": "test-platform",
            },
        )
        manager = self._manager()
        self.assertEqual(manager.manifest.version, "3.4.5")
        # No mineru.json exists, because MinerU 4.x never writes one.
        self.assertFalse((root / "mineru.json").is_file())
        self.assertTrue(manager._installed("pipeline"))
        environment = manager._runtime_environment(
            root, manager._installed_config_style("pipeline")
        )
        self.assertEqual(environment["MINERU_HOME"], str(root))
        self.assertNotIn("MINERU_TOOLS_CONFIG_JSON", environment)

    def test_retargeted_manifest_keeps_launching_installed_3x_the_old_way(self) -> None:
        root = self._fake_receipt(
            "pipeline",
            {
                "schema_version": 1,
                "profile": "pipeline",
                "version": "3.4.5",
                "series": "3",
                "config_style": "tools_json",
                "backend": "pipeline",
                "packages": ["mineru[pipeline]==3.4.5"],
                "platform": "test-platform",
            },
        )
        (root / "mineru.json").write_text("{}", encoding="utf-8")
        payload = json.dumps(
            {"releases": {"3.4.5": [{"filename": "a"}], "4.0.4": [{"filename": "b"}]}}
        ).encode("utf-8")
        manager = self._manager(version_fetch=lambda url: payload)
        manager.perform({"action": "check-updates"})
        self.assertEqual(manager.manifest.version, "4.0.4")
        # The install itself did not move, so it must still be launched as 3.x.
        self.assertTrue(manager._installed("pipeline"))
        environment = manager._runtime_environment(
            root, manager._installed_config_style("pipeline")
        )
        self.assertEqual(
            environment["MINERU_TOOLS_CONFIG_JSON"], str(root / "mineru.json")
        )
        self.assertNotIn("MINERU_HOME", environment)

    def test_legacy_receipt_without_series_is_treated_as_3x(self) -> None:
        root = self._fake_receipt(
            "pipeline",
            {"schema_version": 1, "profile": "pipeline", "version": "3.4.5"},
        )
        (root / "mineru.json").write_text("{}", encoding="utf-8")
        manager = self._manager()
        self.assertEqual(manager._installed_config_style("pipeline"), "tools_json")
        self.assertTrue(manager._installed("pipeline"))

    def test_check_updates_cannot_retarget_a_running_operation(self) -> None:
        payload = json.dumps(
            {"releases": {"3.4.5": [{"filename": "a"}], "4.0.4": [{"filename": "b"}]}}
        ).encode("utf-8")
        manager = self._manager(version_fetch=lambda url: payload)
        with manager._lock:
            manager._states["pipeline"].operation = "install"
        try:
            summary = manager.perform({"action": "check-updates"})
        finally:
            with manager._lock:
                manager._states["pipeline"].operation = None
        self.assertEqual(manager.manifest.version, "3.4.5")
        self.assertEqual(summary["version"], "3.4.5")
        self.assertIn("暂不检查", str(summary["version_detail"]))

    def test_install_freezes_one_recipe_and_records_it(self) -> None:
        """An install writes the version and series it actually installed."""

        payload = json.dumps(
            {"releases": {"3.4.5": [{"filename": "a"}], "4.0.4": [{"filename": "b"}]}}
        ).encode("utf-8")
        manager = self._manager(version_fetch=lambda url: payload)
        manager.perform({"profile": "pipeline", "action": "install"})
        item = self._wait(manager, "pipeline")
        self.assertFalse(item["error"], item["error"])
        receipt = json.loads(
            (self.runtime / "components/mineru/pipeline/installed.json").read_text(
                encoding="utf-8"
            )
        )
        # Plain install resolved the newest compatible release by itself, and the
        # receipt records one coherent recipe (version, series, packages).
        self.assertEqual(receipt["version"], "4.0.4")
        self.assertEqual(receipt["series"], "4")
        self.assertEqual(receipt["config_style"], "mineru_home")
        self.assertEqual(receipt["packages"], ["mineru==4.0.4"])
        self.assertEqual(manager._installed_config_style("pipeline"), "mineru_home")

    def test_plain_install_without_network_stays_on_the_pinned_version(self) -> None:
        def offline(url: str) -> bytes:
            raise OSError("network unreachable")

        manager = self._manager(version_fetch=offline)
        manager.perform({"profile": "pipeline", "action": "install"})
        item = self._wait(manager, "pipeline")
        self.assertFalse(item["error"], item["error"])
        receipt = json.loads(
            (self.runtime / "components/mineru/pipeline/installed.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["version"], "3.4.5")
        self.assertEqual(receipt["series"], "3")

    def test_download_progress_reports_speed_and_eta(self) -> None:
        manager = self._manager()
        with mock.patch(
            "src.me_finder.managed_mineru.time.monotonic",
            side_effect=[0.0, 2.0],
        ):
            manager._begin_download_progress("vlm", 0, 10, total_is_estimate=True)
            manager._set_download_progress(
                "vlm", 4, 10, total_is_estimate=True
            )
        profile = next(
            item for item in manager.summary()["profiles"]
            if item["profile"] == "vlm"
        )
        self.assertEqual(profile["downloaded_bytes"], 4)
        self.assertEqual(profile["total_bytes"], 10)
        self.assertTrue(profile["total_is_estimate"])
        self.assertEqual(profile["download_speed_bps"], 2)
        self.assertEqual(profile["eta_seconds"], 3)

    def test_download_speed_keeps_rolling_average_during_short_pause(self) -> None:
        manager = self._manager()
        with mock.patch(
            "src.me_finder.managed_mineru.time.monotonic",
            side_effect=[0.0, 2.0, 12.0],
        ):
            manager._begin_download_progress("vlm", 0, 100)
            manager._set_download_progress("vlm", 20, 100)
            manager._set_download_progress("vlm", 20, 100)
        profile = next(
            item for item in manager.summary()["profiles"]
            if item["profile"] == "vlm"
        )
        self.assertGreater(profile["download_speed_bps"], 0)
        self.assertIsNotNone(profile["eta_seconds"])

    def test_uv_log_progress_uses_announced_download_sizes(self) -> None:
        manager = self._manager()
        log = self.root / "install.log"
        log.write_text(
            "Downloading torch (2.0MiB)\n"
            "Downloading mineru (1.0MiB)\n"
            " Downloaded torch\n",
            encoding="utf-8",
        )
        with mock.patch(
            "src.me_finder.managed_mineru.time.monotonic",
            side_effect=[0.0, 2.0],
        ):
            manager._update_uv_download_progress("vlm", log, start_offset=0)
        profile = next(
            item for item in manager.summary()["profiles"]
            if item["profile"] == "vlm"
        )
        self.assertEqual(profile["downloaded_bytes"], 2 * 1024**2)
        self.assertEqual(profile["total_bytes"], 3 * 1024**2)
        self.assertEqual(profile["download_speed_bps"], 1024**2)
        self.assertEqual(profile["eta_seconds"], 1)

    def test_install_environment_inherits_system_proxy_for_uv(self) -> None:
        manager = self._manager()
        staging = self.root / "staging"
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch(
                "src.me_finder.managed_mineru.getproxies",
                return_value={
                    "http": "http://127.0.0.1:1082",
                    "https": "http://127.0.0.1:1082",
                },
            ),
        ):
            environment = manager._install_environment(staging, manager.manifest)
        self.assertEqual(environment["HTTP_PROXY"], "http://127.0.0.1:1082")
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:1082")

    def test_missing_uv_archive_member_reports_failure_instead_of_staying_busy(self) -> None:
        manifest_path = self._manifest()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["platforms"]["test-platform"]["uv"]["member"] = "missing/uv"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        manager = ManagedMinerU(
            self.runtime,
            self.config,
            manifest_path=manifest_path,
            platform_key="test-platform",
            process_launcher=_test_process_launcher,
            hardware_detector=lambda: {
                "kind": "nvidia",
                "name": "Test GPU",
                "vlm_supported": True,
                "recommended_profile": "vlm",
                "vram_mb": 24576,
                "compute_capability": 8.9,
            },
        )

        manager.perform({"profile": "pipeline", "action": "install"})
        failed = self._wait(manager, "pipeline")

        self.assertEqual(failed["state"], "not_installed")
        self.assertEqual(failed["error"], "uv 归档缺少清单指定的可执行文件。")
        self.assertFalse(any(manager.component_root.iterdir()))

    def test_model_download_retries_in_same_staging_directory(self) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)
        original_run_command = manager._run_command
        model_attempts = []

        def run_command(profile_id, command, **kwargs):
            if Path(command[0]).name == "mineru-models-download":
                model_attempts.append(kwargs["cwd"])
                if len(model_attempts) == 1:
                    partial = kwargs["cwd"] / "models/resume-marker.incomplete"
                    partial.parent.mkdir(parents=True, exist_ok=True)
                    partial.write_bytes(b"partial")
                    raise ManagedMinerUError(
                        "模型下载连续 2 分钟无数据变化。"
                    )
            return original_run_command(profile_id, command, **kwargs)

        with (
            mock.patch.object(manager, "_run_command", side_effect=run_command),
            mock.patch(
                "src.me_finder.managed_mineru._MODEL_DOWNLOAD_RETRY_DELAYS",
                (0, 0),
            ),
        ):
            manager.perform({"profile": "vlm", "action": "install"})
            installed = self._wait(manager, "vlm")

        self.assertTrue(installed["installed"])
        self.assertEqual(len(model_attempts), 2)
        self.assertEqual(model_attempts[0], model_attempts[1])
        self.assertTrue(
            (
                manager.component_root
                / "vlm/models/resume-marker.incomplete"
            ).is_file()
        )

    def test_model_download_reports_concise_error_after_three_failures(self) -> None:
        manager = self._manager()
        original_run_command = manager._run_command
        attempts = 0

        def run_command(profile_id, command, **kwargs):
            nonlocal attempts
            if Path(command[0]).name == "mineru-models-download":
                attempts += 1
                raise ManagedMinerUError(
                    "OSError: I/O error: error decoding response body"
                )
            return original_run_command(profile_id, command, **kwargs)

        with (
            mock.patch.object(manager, "_run_command", side_effect=run_command),
            mock.patch(
                "src.me_finder.managed_mineru._MODEL_DOWNLOAD_RETRY_DELAYS",
                (0, 0),
            ),
        ):
            manager.perform({"profile": "vlm", "action": "install"})
            failed = self._wait(manager, "vlm")

        self.assertEqual(attempts, 3)
        self.assertEqual(
            failed["error"],
            "模型下载网络连接中断，已自动重试 2 次。请检查网络或代理后重试。",
        )
        self.assertFalse(any(manager.component_root.glob(".staging-*")))

    def test_model_payload_size_excludes_xet_logs_and_locks(self) -> None:
        manager = self._manager()
        models = self.root / "models"
        blob = models / "huggingface/hub/blobs/model.incomplete"
        log = models / "huggingface/xet/logs/xet.log"
        lock = models / "huggingface/hub/.locks/model.lock"
        for path, content in (
            (blob, b"payload"),
            (log, b"log growth should not count"),
            (lock, b"lock growth should not count"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        self.assertEqual(manager._model_payload_size(models), len(b"payload"))

    def test_run_command_stops_model_download_after_payload_stalls(self) -> None:
        manager = self._manager()

        class StalledProcess:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout):
                return self.returncode

        process = StalledProcess()
        manager.process_launcher = lambda *_args, **_kwargs: process
        models = self.root / "models"
        models.mkdir()

        with self.assertRaisesRegex(
            ManagedMinerUError,
            "模型下载连续 2 分钟无数据变化",
        ):
            manager._run_command(
                "vlm",
                ["fake-model-downloader"],
                cwd=self.root,
                environment={},
                log_path=self.root / "models.log",
                timeout=10,
                track_directory=models,
                estimated_total_bytes=100,
                stall_timeout=0.05,
            )

        self.assertEqual(process.returncode, -15)

    def test_run_command_stops_pypi_install_after_output_and_cache_stall(self) -> None:
        manager = self._manager()

        class StalledProcess:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout):
                return self.returncode

        process = StalledProcess()
        manager.process_launcher = lambda *_args, **_kwargs: process
        cache = self.root / ".uv-cache"
        cache.mkdir()

        with self.assertRaisesRegex(
            ManagedMinerUError,
            "PyPI 安装依赖连续 5 分钟无数据变化",
        ):
            manager._run_command(
                "vlm",
                ["fake-uv", "pip", "install"],
                cwd=self.root,
                environment={},
                log_path=self.root / "install.log",
                timeout=10,
                track_uv_downloads=True,
                track_directory=cache,
                stall_timeout=0.05,
                stall_message="从 PyPI 安装依赖连续 5 分钟无数据变化。",
            )

        self.assertEqual(process.returncode, -15)

    def test_install_removes_staging_directory_left_by_interrupted_run(self) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)
        stale = manager.component_root / ".staging-vlm-interrupted"
        stale.mkdir(parents=True)
        (stale / "partial-model.bin").write_bytes(b"partial")

        manager.perform({"profile": "pipeline", "action": "install"})
        installed = self._wait(manager, "pipeline")

        self.assertTrue(installed["installed"])
        self.assertFalse(stale.exists())

    def test_pipeline_install_starts_loopback_service_and_uninstalls(self) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)
        manager.perform({"profile": "pipeline", "action": "install"})
        installed = self._wait(manager, "pipeline")
        self.assertTrue(installed["installed"])
        service = manager.summary()["service"]
        self.assertTrue(service["running"])
        self.assertTrue(str(service["endpoint"]).startswith("http://127.0.0.1:"))
        local = mineru_local_config_summary(self.config)
        self.assertTrue(local["managed"])
        self.assertEqual(local["managed_profile"], "pipeline")
        self.assertEqual(local["backend"], "pipeline")

        manager.perform({"profile": "pipeline", "action": "uninstall"})
        removed = self._wait(manager, "pipeline")
        self.assertFalse(removed["installed"])
        self.assertFalse(mineru_local_config_summary(self.config)["enabled"])

    def test_auto_selects_vlm_on_qualified_gpu(self) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)
        manager.perform({"profile": "auto", "action": "install"})
        installed = self._wait(manager, "vlm")
        self.assertTrue(installed["installed"])
        self.assertEqual(mineru_local_config_summary(self.config)["backend"], "vlm-auto-engine")

    def test_nvidia_detection_requires_both_architecture_and_memory(self) -> None:
        class Result:
            stdout = "RTX small, 6144, 8.9\nTesla V100, 16384, 7.0\n"

        detected = detect_mineru_hardware(
            platform_key="linux-x86_64",
            command_runner=lambda *_args, **_kwargs: Result(),
        )
        self.assertTrue(detected["vlm_supported"])
        self.assertEqual(detected["name"], "Tesla V100")

    def test_apple_silicon_detection_requires_macos_14_and_16gb_memory(self) -> None:
        class Result:
            stdout = str(16 * 1024 * 1024 * 1024)

        with mock.patch(
            "src.me_finder.managed_mineru.platform.mac_ver",
            return_value=("14.6.1", ("", "", ""), ""),
        ):
            detected = detect_mineru_hardware(
                platform_key="darwin-arm64",
                command_runner=lambda *_args, **_kwargs: Result(),
            )

        self.assertTrue(detected["vlm_supported"])
        self.assertEqual(detected["recommended_profile"], "vlm")
        self.assertEqual(detected["memory_mb"], 16 * 1024)


if __name__ == "__main__":
    unittest.main()
