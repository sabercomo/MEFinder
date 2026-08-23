from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.local_ocr_installer import (
    LOCAL_OCR_MANIFEST_FILE,
    LocalOCRInstaller,
    load_local_ocr_installer_manifest,
)
from src.me_finder.local_ocr_settings import load_local_ocr_config


class _DownloadResponse(io.BytesIO):
    def __init__(self, payload: bytes, status: int) -> None:
        super().__init__(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class LocalOCRInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime_root = self.root / "中文 テスト runtime"
        self.config_path = self.runtime_root / "config/local_ocr.json"

    def _asset(self, *, slow_uv: bool = False) -> tuple[Path, Path]:
        source_tree = self.root / "source-tree/test-engine-1.0"
        (source_tree / "src").mkdir(parents=True)
        (source_tree / "resource").mkdir()
        (source_tree / "LICENCE").write_text("CC BY 4.0\n", encoding="utf-8")
        (source_tree / "resource/sample.png").write_bytes(b"fake-png")
        (source_tree / "src/ocr.py").write_text(
            """from pathlib import Path
import json, sys
if '--help' in sys.argv:
    print('help')
    raise SystemExit(0)
out = Path(sys.argv[sys.argv.index('--output') + 1])
source = Path(sys.argv[sys.argv.index('--sourceimg') + 1])
(out / (source.stem + '.json')).write_text(json.dumps({'contents': []}))
""",
            encoding="utf-8",
        )
        source_archive = self.root / "test-engine.tar.gz"
        with tarfile.open(source_archive, "w:gz") as bundle:
            bundle.add(source_tree, arcname="test-engine-1.0")

        uv_script = self.root / "uv"
        uv_script.write_text(
            """#!/usr/bin/env python3
from pathlib import Path
import os, sys, time
if sys.argv[1] == 'venv':
    if os.environ.get('MEFINDER_TEST_SLOW_UV') == '1':
        time.sleep(30)
    target = Path(sys.argv[-1])
    (target / 'bin').mkdir(parents=True)
    (target / 'bin/python').symlink_to(sys.executable)
elif sys.argv[1:3] == ['pip', 'install']:
    raise SystemExit(0)
else:
    raise SystemExit(2)
""",
            encoding="utf-8",
        )
        uv_archive = self.root / "uv.tar.gz"
        with tarfile.open(uv_archive, "w:gz") as bundle:
            bundle.add(uv_script, arcname="fake-uv/uv")

        manifest = {
            "schema_version": 1,
            "uv_version": "test",
            "engines": {
                "ndlocr-lite": {
                    "display_name": "Test OCR",
                    "version": "1.0",
                    "tag": "1.0",
                    "tarball_url": source_archive.as_uri(),
                    "tarball_size": source_archive.stat().st_size,
                    "tarball_sha256": hashlib.sha256(
                        source_archive.read_bytes()
                    ).hexdigest(),
                    "archive_root": "test-engine-1.0",
                    "script_path": "src/ocr.py",
                    "sample_path": "resource/sample.png",
                    "cli_extra_args": [],
                    "dependencies": [],
                    "license": "CC-BY-4.0",
                    "license_path": "LICENCE",
                    "attribution": "Test OCR",
                    "modification_notice": "unmodified",
                }
            },
            "platforms": {
                "test-platform": {
                    "python": "3.11",
                    "venv_python": "venv/bin/python",
                    "onnxruntime": "onnxruntime==test",
                    "uv": {
                        "url": uv_archive.as_uri(),
                        "size": uv_archive.stat().st_size,
                        "sha256": hashlib.sha256(
                            uv_archive.read_bytes()
                        ).hexdigest(),
                        "archive_type": "tar.gz",
                        "member": "fake-uv/uv",
                    },
                    "notes": "test",
                }
            },
        }
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        if slow_uv:
            self.addCleanup(os.environ.pop, "MEFINDER_TEST_SLOW_UV", None)
            os.environ["MEFINDER_TEST_SLOW_UV"] = "1"
        return manifest_path, source_archive

    def _installer(self, *, slow_uv: bool = False) -> LocalOCRInstaller:
        manifest_path, _archive = self._asset(slow_uv=slow_uv)
        return LocalOCRInstaller(
            self.runtime_root,
            self.config_path,
            manifest_path=manifest_path,
            platform_key="test-platform",
        )

    def _wait(self, installer: LocalOCRInstaller, timeout: float = 15) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            engine = installer.summary()["engines"][0]
            if engine["operation"] is None:
                return engine
            time.sleep(0.02)
        self.fail("installer operation did not finish")

    def test_release_manifest_freezes_all_four_platforms(self) -> None:
        for key, onnxruntime in (
            ("darwin-arm64", "onnxruntime==1.23.2"),
            ("darwin-x86_64", "onnxruntime==1.23.2"),
            ("win32-x86_64", "onnxruntime==1.26.0"),
            ("linux-x86_64", "onnxruntime==1.26.0"),
        ):
            engines, selected = load_local_ocr_installer_manifest(
                LOCAL_OCR_MANIFEST_FILE,
                platform_key=key,
            )
            self.assertIsNotNone(selected)
            self.assertEqual(selected.onnxruntime, onnxruntime)
            self.assertEqual(selected.uv.version, "0.12.1")
            self.assertEqual(engines["ndlocr-lite"].tag, "1.2.3")
            self.assertEqual(engines["ndlkotenocr-lite"].tag, "1.4.3")

    def test_install_runs_every_state_and_writes_machine_local_paths(self) -> None:
        installer = self._installer()
        transitions = []
        original = installer._set_state

        def record(provider_id, state, **values):
            transitions.append(state)
            return original(provider_id, state, **values)

        installer._set_state = record
        installer.perform({"provider_id": "ndlocr-lite", "action": "install"})
        result = self._wait(installer)

        self.assertEqual(result["state"], "installed")
        self.assertTrue(result["managed"])
        for expected in (
            "verifying",
            "extracting",
            "provisioning",
            "validating",
            "installed",
        ):
            self.assertIn(expected, transitions)
        config = load_local_ocr_config(self.config_path)
        engine = config.available_engines[0]
        self.assertTrue(engine.python_path.is_file())
        self.assertTrue(engine.script_path.is_file())
        final = self.runtime_root / "components/local-ocr/ndlocr-lite"
        self.assertTrue((final / "installed.json").is_file())
        self.assertTrue((final / "sbom.spdx.json").is_file())

    def test_bad_digest_rolls_back_without_config_or_staging(self) -> None:
        manifest_path, _archive = self._asset()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["engines"]["ndlocr-lite"]["tarball_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        installer = LocalOCRInstaller(
            self.runtime_root,
            self.config_path,
            manifest_path=manifest_path,
            platform_key="test-platform",
        )

        installer.perform({"provider_id": "ndlocr-lite", "action": "install"})
        result = self._wait(installer)

        self.assertEqual(result["state"], "not_installed")
        self.assertIn("SHA-256", result["error"])
        self.assertFalse(self.config_path.exists())
        component_root = self.runtime_root / "components/local-ocr"
        self.assertFalse(component_root.exists())

    def test_cancel_terminates_provisioning_and_cleans_everything(self) -> None:
        installer = self._installer(slow_uv=True)
        installer.perform({"provider_id": "ndlocr-lite", "action": "install"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = installer.summary()["engines"][0]
            if current["state"] == "provisioning":
                break
            time.sleep(0.02)
        else:
            self.fail("installer never reached provisioning")

        started = time.monotonic()
        installer.perform({"provider_id": "ndlocr-lite", "action": "cancel"})
        result = self._wait(installer)

        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(result["state"], "not_installed")
        self.assertEqual(result["message"], "操作已取消")
        self.assertFalse((self.runtime_root / "components/local-ocr").exists())

    def test_uninstall_removes_runtime_and_only_managed_paths(self) -> None:
        installer = self._installer()
        installer.perform({"provider_id": "ndlocr-lite", "action": "install"})
        self.assertEqual(self._wait(installer)["state"], "installed")

        installer.perform({"provider_id": "ndlocr-lite", "action": "uninstall"})
        result = self._wait(installer)

        self.assertEqual(result["state"], "not_installed")
        self.assertFalse((self.runtime_root / "components/local-ocr").exists())
        self.assertFalse(load_local_ocr_config(self.config_path).available_engines)

    def test_downloader_resumes_when_server_accepts_range(self) -> None:
        installer = self._installer()
        target = self.root / "partial.bin"
        target.write_bytes(b"abc")
        requests = []

        def open_request(request, timeout):
            requests.append((request, timeout))
            return _DownloadResponse(b"def", 206)

        with mock.patch(
            "src.me_finder.local_ocr_installer.urlopen",
            side_effect=open_request,
        ):
            installer._download_file(
                "ndlocr-lite", "https://example.invalid/asset", target, 6
            )

        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertEqual(requests[0][0].get_header("Range"), "bytes=3-")
        self.assertEqual(requests[0][1], 30)

    def test_downloader_keeps_checkpoint_when_server_ignores_range(self) -> None:
        installer = self._installer()
        target = self.root / "partial.bin"
        target.write_bytes(b"abc")

        with mock.patch(
            "src.me_finder.local_ocr_installer.urlopen",
            return_value=_DownloadResponse(b"abcdef", 200),
        ) as opened:
            installer._download_file(
                "ndlocr-lite", "https://example.invalid/asset", target, 6
            )

        self.assertEqual(target.read_bytes(), b"abcdef")
        self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
