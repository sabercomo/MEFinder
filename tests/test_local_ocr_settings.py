from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from src.me_finder.local_ocr_settings import (
    LocalOCRError,
    clear_managed_local_ocr_engine,
    configure_managed_local_ocr_engine,
    load_local_ocr_config,
    local_ocr_config_summary,
    save_local_ocr_config,
    test_local_ocr_engine as run_local_ocr_engine,
)
from src.me_finder.local_ocr_runtime import local_ocr_engine_lock


class LocalOCRSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config" / "local_ocr.json"

    def _payload(self, *, enabled: bool, script: Path | None = None):
        script_path = script or Path()
        engine = {
            "enabled": enabled,
            "python_path": sys.executable if enabled else "",
            "script_path": str(script_path) if enabled else "",
            "weights_sha256": "weights-digest" if enabled else "",
        }
        return {
            "render_dpi": 240,
            "probe_pages": 5,
            "pages_per_slice": 8,
            "timeout_seconds_per_page": 120,
            "blank_ink_ratio": 0.002,
            "engines": {
                "ndlocr-lite": engine,
                "ndlkotenocr-lite": {
                    "enabled": False,
                    "python_path": "",
                    "script_path": "",
                },
            },
        }

    def test_missing_config_has_two_disabled_engines(self) -> None:
        config = load_local_ocr_config(self.config_path)

        self.assertEqual(
            [item.provider_id for item in config.engines],
            ["ndlocr-lite", "ndlkotenocr-lite"],
        )
        self.assertFalse(config.available_engines)
        self.assertFalse(local_ocr_config_summary(self.config_path)["available"])

    def test_enabled_engine_requires_existing_absolute_paths(self) -> None:
        with self.assertRaisesRegex(LocalOCRError, "有效的 Python"):
            save_local_ocr_config(
                self._payload(enabled=True, script=self.root / "missing.py"),
                self.config_path,
            )

    def test_round_trip_preserves_runtime_tuning_and_digest(self) -> None:
        script = self.root / "ocr.py"
        script.write_text("print('ok')\n", encoding="utf-8")

        summary = save_local_ocr_config(
            self._payload(enabled=True, script=script),
            self.config_path,
        )

        self.assertTrue(summary["available"])
        self.assertEqual(summary["render_dpi"], 240)
        self.assertEqual(summary["blank_ink_ratio"], 0.002)
        self.assertEqual(summary["engines"][0]["weights_sha256"], "weights-digest")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["engines"]["ndlocr-lite"]["script_path"], str(script))

    def test_managed_venv_symlink_is_not_resolved_to_base_python(self) -> None:
        python_path = (
            self.root
            / "components/local-ocr/ndlocr-lite/venv/bin/python"
        )
        python_path.parent.mkdir(parents=True)
        python_path.symlink_to(Path(sys.executable).resolve())
        script = self.root / "components/local-ocr/ndlocr-lite/source/src/ocr.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('ok')\n", encoding="utf-8")

        configure_managed_local_ocr_engine(
            self.config_path,
            "ndlocr-lite",
            python_path=python_path,
            script_path=script,
        )

        engine = load_local_ocr_config(self.config_path).available_engines[0]
        self.assertEqual(engine.python_path, python_path)
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["engines"]["ndlocr-lite"]["python_path"],
            str(python_path),
        )

    def test_legacy_resolved_managed_python_path_is_normalized(self) -> None:
        managed_python = (
            self.root
            / "components/local-ocr/ndlocr-lite/venv/bin/python"
        )
        managed_python.parent.mkdir(parents=True)
        managed_python.symlink_to(Path(sys.executable).resolve())
        script = self.root / "components/local-ocr/ndlocr-lite/source/src/ocr.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('ok')\n", encoding="utf-8")
        payload = self._payload(enabled=True, script=script)
        payload["engines"]["ndlocr-lite"]["python_path"] = str(
            managed_python.resolve()
        )

        summary = save_local_ocr_config(payload, self.config_path)

        self.assertEqual(
            summary["engines"][0]["python_path"],
            str(managed_python),
        )
        self.assertEqual(
            load_local_ocr_config(self.config_path).available_engines[0].python_path,
            managed_python,
        )

    def test_engine_test_starts_configured_cli(self) -> None:
        script = self.root / "ocr.py"
        script.write_text(
            "import argparse\nargparse.ArgumentParser().parse_args()\n",
            encoding="utf-8",
        )

        result = run_local_ocr_engine(
            {
                "provider_id": "ndlocr-lite",
                "python_path": sys.executable,
                "script_path": str(script),
            },
            self.config_path,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider_id"], "ndlocr-lite")

    def test_engine_test_is_blocked_during_install_or_ocr(self) -> None:
        script = self.root / "ocr.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        engine_lock = local_ocr_engine_lock("ndlocr-lite")
        engine_lock.acquire()
        try:
            with self.assertRaisesRegex(LocalOCRError, "正在安装"):
                run_local_ocr_engine(
                    {
                        "provider_id": "ndlocr-lite",
                        "python_path": sys.executable,
                        "script_path": str(script),
                    },
                    self.config_path,
                )
        finally:
            engine_lock.release()

    def test_managed_install_and_uninstall_update_only_matching_paths(self) -> None:
        python_path = Path(sys.executable).resolve()
        script = self.root / "managed" / "src" / "ocr.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('ok')\n", encoding="utf-8")

        configure_managed_local_ocr_engine(
            self.config_path,
            "ndlocr-lite",
            python_path=python_path,
            script_path=script,
        )
        self.assertTrue(load_local_ocr_config(self.config_path).available_engines)

        clear_managed_local_ocr_engine(
            self.config_path,
            "ndlocr-lite",
            python_path=python_path,
            script_path=script,
        )
        self.assertFalse(load_local_ocr_config(self.config_path).available_engines)


if __name__ == "__main__":
    unittest.main()
