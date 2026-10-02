from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.me_finder.mineru_api import MinerUError
from src.me_finder.mineru_local_settings import (
    clear_managed_mineru,
    configure_managed_mineru,
    load_mineru_local_config,
    mineru_local_config_summary,
    save_mineru_local_config,
    test_mineru_local_connection as run_mineru_local_connection,
)


class MinerULocalSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "config" / "mineru.local.json"

    def _machine_env(self):
        machine = Path(self.temp_dir.name) / "machine" / "mineru_local.machine.json"
        return machine, patch.dict(
            "os.environ", {"ME_FINDER_MINERU_LOCAL_CONFIG": str(machine)}
        )

    def test_per_machine_settings_never_touch_the_shared_config(self) -> None:
        """A cloud-synced data root must not carry this computer's endpoint."""

        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"token": "shared"}), encoding="utf-8")
        machine, env = self._machine_env()
        with env:
            save_mineru_local_config(
                {"enabled": True, "endpoint": "http://127.0.0.1:8000", "backend": "pipeline"},
                self.path,
            )
            summary = mineru_local_config_summary(self.path)
        self.assertTrue(summary["enabled"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"token": "shared"})
        self.assertTrue(json.loads(machine.read_text(encoding="utf-8"))["local_deployment_enabled"])

    def test_per_machine_settings_start_from_shared_self_hosted_settings(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "token": "shared",
                    "local_deployment_enabled": True,
                    "local_deployment_endpoint": "http://127.0.0.1:9000",
                    "local_deployment_api_key": "local-key",
                }
            ),
            encoding="utf-8",
        )
        machine, env = self._machine_env()
        with env:
            summary = mineru_local_config_summary(self.path)
            self.assertEqual(load_mineru_local_config(self.path).api_key, "local-key")
            clear_managed_mineru(self.path)
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["endpoint"], "http://127.0.0.1:9000")
        self.assertFalse(machine.exists())

    def test_shared_managed_settings_are_not_inherited_by_default(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "local_deployment_enabled": True,
                    "local_deployment_managed": True,
                    "local_deployment_managed_profile": "pipeline",
                    "local_deployment_endpoint": "http://127.0.0.1:1407",
                }
            ),
            encoding="utf-8",
        )
        _machine, env = self._machine_env()
        with env:
            summary = mineru_local_config_summary(self.path)
        self.assertFalse(summary["enabled"])
        self.assertFalse(summary["managed"])
        self.assertEqual(summary["endpoint"], "http://127.0.0.1:8000")

    def test_default_is_disabled_and_save_preserves_online_config(self) -> None:
        self.assertEqual(
            mineru_local_config_summary(self.path),
            {
                "enabled": False,
                "managed": False,
                "managed_profile": "",
                "endpoint": "http://127.0.0.1:8000",
                "backend": "pipeline",
                "protocol": "auto",
                "tier": "",
                "has_api_key": False,
            },
        )
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"token": "online-secret", "api_base": "https://mineru.net"}),
            encoding="utf-8",
        )

        summary = save_mineru_local_config(
            {
                "enabled": True,
                "endpoint": "http://127.0.0.1:9000/",
                "backend": "pipeline",
            },
            self.path,
        )

        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["endpoint"], "http://127.0.0.1:9000")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["token"], "online-secret")
        config = load_mineru_local_config(self.path)
        self.assertEqual(config.endpoint, "http://127.0.0.1:9000")

    def test_disabled_config_cannot_be_used_for_retry(self) -> None:
        save_mineru_local_config(
            {
                "enabled": False,
                "endpoint": "http://127.0.0.1:8000",
                "backend": "pipeline",
            },
            self.path,
        )
        with self.assertRaisesRegex(MinerUError, "尚未.*启用"):
            load_mineru_local_config(self.path)

    def test_save_validates_boundary_and_test_uses_unsaved_form_values(self) -> None:
        with self.assertRaisesRegex(MinerUError, "布尔值"):
            save_mineru_local_config(
                {"enabled": "yes", "endpoint": "http://127.0.0.1:8000"},
                self.path,
            )
        with self.assertRaisesRegex(MinerUError, "http"):
            save_mineru_local_config(
                {"enabled": True, "endpoint": "127.0.0.1:8000"},
                self.path,
            )

        with patch(
            "src.me_finder.mineru_local_settings.MinerULocalProvider.health",
            return_value={"ok": True, "protocol_version": "1"},
        ) as health:
            result = run_mineru_local_connection(
                {
                    "enabled": False,
                    "endpoint": "http://127.0.0.1:8123",
                    "backend": "pipeline",
                },
                self.path,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["endpoint"], "http://127.0.0.1:8123")
        health.assert_called_once_with()

    def test_managed_runtime_configuration_is_explicit_and_reversible(self) -> None:
        configured = configure_managed_mineru(
            self.path,
            endpoint="http://127.0.0.1:18432",
            backend="vlm-auto-engine",
            profile="vlm",
        )
        self.assertTrue(configured["enabled"])
        self.assertTrue(configured["managed"])
        self.assertEqual(configured["managed_profile"], "vlm")

        cleared = clear_managed_mineru(self.path)
        self.assertFalse(cleared["enabled"])
        self.assertFalse(cleared["managed"])


if __name__ == "__main__":
    unittest.main()
