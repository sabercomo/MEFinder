from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from src.me_finder.local_ocr_settings import (
    LocalOCRError,
    load_local_ocr_config,
    local_ocr_config_summary,
    save_local_ocr_config,
    test_local_ocr_engine,
)


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
        self.assertEqual(raw["engines"]["ndlocr-lite"]["script_path"], str(script.resolve()))

    def test_engine_test_starts_configured_cli(self) -> None:
        script = self.root / "ocr.py"
        script.write_text(
            "import argparse\nargparse.ArgumentParser().parse_args()\n",
            encoding="utf-8",
        )

        result = test_local_ocr_engine(
            {
                "provider_id": "ndlocr-lite",
                "python_path": sys.executable,
                "script_path": str(script),
            },
            self.config_path,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider_id"], "ndlocr-lite")


if __name__ == "__main__":
    unittest.main()
