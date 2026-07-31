from __future__ import annotations

from io import BytesIO
import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import patch

from src.me_finder.mineru_api import (
    MinerUClient,
    MinerUConfig,
    MinerUError,
    _SafeAuthorizationRedirectHandler,
    _expiry_summary,
    load_mineru_config,
    mineru_config_summary,
    read_mineru_config_data,
    resolve_mineru_config_path,
    save_mineru_config,
    submit_local_pdf_segments,
)


class MinerUConfigTests(unittest.TestCase):
    def test_generic_local_error_does_not_authorize_paid_parser_fallback(
        self,
    ) -> None:
        self.assertFalse(
            MinerUError("local manifest write failed").allow_parser_fallback
        )

    def test_pasted_bearer_token_is_normalized_before_save_and_load(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "mineru_api.local.json"

            summary = save_mineru_config(
                {"token": "  Bearer token-from-mineru-settings  "},
                path,
            )

            self.assertTrue(summary["configured"])
            self.assertTrue(summary["has_token"])
            self.assertEqual(
                read_mineru_config_data(path)["token"],
                "token-from-mineru-settings",
            )
            self.assertEqual(
                load_mineru_config(path).token,
                "token-from-mineru-settings",
            )

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

    def test_access_keys_without_token_are_not_reported_as_configured(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "mineru_api.local.json"
            save_mineru_config(
                {
                    "access_key_id": "access-id",
                    "secret_access_key": "secret-value",
                },
                path,
            )

            with self.subTest("safe summary distinguishes keys from API token"):
                summary = mineru_config_summary(path)
                self.assertFalse(summary["configured"])
                self.assertFalse(summary["has_token"])
                self.assertTrue(summary["has_access_key_id"])
                self.assertTrue(summary["has_secret_access_key"])

            with self.subTest("runtime refuses to use Secret Key as Bearer Token"):
                with self.assertRaisesRegex(MinerUError, r"(?i)token"):
                    load_mineru_config(path)

    def test_invalid_token_shapes_are_never_reported_as_configured(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mineru.json"
            for invalid in ("Bearer ", "abc def", ["x"]):
                with self.subTest(token=invalid):
                    path.write_text(
                        json.dumps({"token": invalid}),
                        encoding="utf-8",
                    )
                    self.assertFalse(mineru_config_summary(path)["configured"])
                    with self.assertRaisesRegex(MinerUError, r"(?i)token"):
                        load_mineru_config(path)

            with self.assertRaisesRegex(MinerUError, r"(?i)token"):
                save_mineru_config({"token": ["x"]}, path)

    def test_valid_legacy_alias_is_not_shadowed_by_placeholder_token(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mineru.json"
            path.write_text(
                '{"token":"PASTE_TOKEN_HERE","api_token":"legacy-valid-token"}',
                encoding="utf-8",
            )

            self.assertTrue(mineru_config_summary(path)["configured"])
            self.assertEqual(load_mineru_config(path).token, "legacy-valid-token")

    def test_cross_origin_redirect_strips_authorization_header(self) -> None:
        handler = _SafeAuthorizationRedirectHandler()
        request = Request(
            "https://mineru.net/api/v4/file-urls/batch",
            headers={"Authorization": "Bearer private-token"},
        )

        cross_origin = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://redirect.example.test/upload",
        )
        same_origin = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://mineru.net/next",
        )

        self.assertIsNotNone(cross_origin)
        self.assertIsNone(cross_origin.get_header("Authorization"))
        self.assertEqual(
            same_origin.get_header("Authorization"),
            "Bearer private-token",
        )

    def test_http_401_is_safe_to_show_and_allows_parser_fallback(self) -> None:
        raw_error = (
            b'{"traceId":"private-trace-id","msgCode":"A0202",'
            b'"msg":"user authenticate failed","data":null,'
            b'"success":false,"total":0}'
        )
        response_error = HTTPError(
            "https://mineru.net/api/v4/file-urls/batch",
            401,
            "Unauthorized",
            None,
            BytesIO(raw_error),
        )
        client = MinerUClient(MinerUConfig(token="expired-token"))

        with patch.object(client.opener, "open", side_effect=response_error):
            with self.assertRaises(MinerUError) as caught:
                client.apply_upload_urls([{"name": "document.pdf", "data_id": "doc"}])

        error = caught.exception
        message = str(error)
        self.assertTrue(error.allow_parser_fallback)
        self.assertIn("401", message)
        self.assertRegex(message, r"Token|令牌|认证|鉴权")
        self.assertNotIn("private-trace-id", message)
        self.assertNotIn("traceId", message)
        self.assertNotIn("msgCode", message)
        self.assertNotIn("{", message)

    def test_initial_submit_401_remains_eligible_for_parser_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "document.pdf"
            pdf_path.write_bytes(b"%PDF synthetic test fixture")
            auth_error = MinerUError(
                "MinerU HTTP 401: Token 无效或已过期。",
                allow_parser_fallback=True,
            )

            with (
                patch(
                    "src.me_finder.mineru_api.get_pdf_page_count",
                    return_value=1,
                ),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    side_effect=auth_error,
                ),
            ):
                with self.assertRaises(MinerUError) as caught:
                    submit_local_pdf_segments(
                        pdf_path,
                        config_path=root / "mineru.json",
                        state_dir=root / "tasks",
                        manifest_dir=root / "manifests",
                        result_dir=root / "results",
                    )

            self.assertTrue(caught.exception.allow_parser_fallback)

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
