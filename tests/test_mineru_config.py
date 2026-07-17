from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.me_finder.mineru_api import (
    MinerUError,
    _expiry_summary,
    read_mineru_config_data,
    resolve_mineru_config_path,
    save_mineru_config,
)


class MinerUConfigTests(unittest.TestCase):
    def test_save_and_rotate_token_without_returning_secrets(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "mineru_api.local.json"
            first = save_mineru_config(
                {
                    "token": "token-old",
                    "access_key_id": "access-id",
                    "secret_access_key": "secret-value",
                    "expires_at": "2026-10-01",
                },
                path,
            )
            self.assertTrue(first["configured"])
            self.assertTrue(first["has_token"])
            self.assertEqual(first["expires_at"], "2026-10-01")
            self.assertIn("expiry_status", first)
            self.assertIn("expiry_label", first)
            self.assertNotIn("token-old", first)
            self.assertNotIn("secret-value", first)

            rotated = save_mineru_config({"token": "token-new", "expires_at": "2027-01-01"}, path)
            self.assertTrue(rotated["configured"])
            self.assertEqual(rotated["expires_at"], "2027-01-01")
            stored = read_mineru_config_data(path)
            self.assertEqual(stored["token"], "token-new")
            self.assertEqual(stored["access_key_id"], "access-id")
            self.assertEqual(stored["secret_access_key"], "secret-value")

    def test_invalid_api_address_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mineru.json"
            with self.assertRaises(MinerUError):
                save_mineru_config({"api_base": "file:///secret"}, path)

    def test_expiry_summary_labels_remaining_days(self) -> None:
        today = date(2026, 7, 10)
        future = _expiry_summary("2026-10-08", today)
        self.assertEqual(future["expiry_status"], "valid")
        self.assertEqual(future["expires_days_remaining"], 90)
        self.assertEqual(future["expiry_label"], "2026-10-08（剩余 90 天）")

        expires_today = _expiry_summary("2026-07-10", today)
        self.assertEqual(expires_today["expiry_status"], "expires_today")
        self.assertEqual(expires_today["expiry_label"], "2026-07-10（今天到期）")

        expired = _expiry_summary("2026-07-01", today)
        self.assertEqual(expired["expiry_status"], "expired")
        self.assertEqual(expired["expires_days_remaining"], -9)
        self.assertEqual(expired["expiry_label"], "2026-07-01（已过期 9 天）")

        unset = _expiry_summary("", today)
        self.assertEqual(unset["expiry_status"], "unset")
        self.assertEqual(unset["expiry_label"], "未设置到期时间")

    def test_desktop_config_override_survives_outside_app_directory(self) -> None:
        override = Path("C:/Users/example/AppData/Local/MEFinder/mineru_api.local.json")
        with patch.dict("os.environ", {"ME_FINDER_MINERU_CONFIG": str(override)}):
            self.assertEqual(resolve_mineru_config_path(Path("D:/portable/MEFinder")), override)


if __name__ == "__main__":
    unittest.main()
