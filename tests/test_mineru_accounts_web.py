from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from src.me_finder.app_context import AppContext
from src.me_finder.database import build_database
from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.slicing import SliceDescriptor
from src.me_finder.web import make_handler


class MinerUAccountsWebTests(unittest.TestCase):
    @contextmanager
    def _runtime(self, root: Path):
        context = AppContext.create(root, index_path=Path("data/index.sqlite3"))
        context.paths.config_root.mkdir(parents=True, exist_ok=True)
        build_database({"metadata": {}}, context.paths.index_path)
        handler = make_handler(context.paths.index_path, app_context=context)
        handler.log_message = lambda *_args: None
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, handler
        finally:
            server.shutdown()
            server.server_close()
            handler.close_runtime()
            thread.join(timeout=2)

    @staticmethod
    def _request(
        server: ThreadingHTTPServer,
        method: str,
        path: str,
        payload: object | None = None,
    ) -> tuple[int, dict[str, object]]:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            }
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_ten_accounts_round_trip_without_exposing_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._runtime(root) as (server, _handler):
                for index in range(1, 11):
                    status, payload = self._request(
                        server,
                        "POST",
                        "/api/mineru-accounts",
                        {
                            "account_id": f"account-{index}",
                            "display_name": f"MinerU 账号 {index}",
                            "token": f"private-token-{index}",
                            "api_base": "https://mineru.net",
                            "expires_at": "2026-12-31",
                            "enabled": True,
                        },
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(len(payload["accounts"]), index)

                status, payload = self._request(
                    server, "GET", "/api/mineru-accounts"
                )
                self.assertEqual(status, 200)
                self.assertTrue(payload["configured"])
                self.assertEqual(len(payload["accounts"]), 10)
                self.assertEqual(payload["statistics"]["parsed_page_count"], 0)
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("private-token", serialized)
                self.assertNotIn("secret_ref", serialized)

                status, updated = self._request(
                    server,
                    "POST",
                    "/api/mineru-accounts",
                    {
                        "account_id": "account-1",
                        "display_name": "主账号",
                        "token": "",
                        "api_base": "https://mineru.net",
                        "expires_at": "",
                        "enabled": False,
                    },
                )
                self.assertEqual(status, 200)
                account = next(
                    item
                    for item in updated["accounts"]
                    if item["account_id"] == "account-1"
                )
                self.assertTrue(account["configured"])
                self.assertFalse(account["enabled"])
                self.assertIsNone(account["expires_at"])

            private = json.loads(
                (root / "config" / "mineru_accounts.local.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(private["accounts"]), 10)
            self.assertEqual(
                private["accounts"]["account-1"]["token"], "private-token-1"
            )

    def test_legacy_single_token_is_migrated_as_first_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True)
            legacy_path = config_dir / "mineru_api.local.json"
            legacy_path.write_text(
                json.dumps(
                    {
                        "api_token": "legacy-private-token",
                        "api_base": "https://mineru.net",
                        "expires_at": "2026-10-07",
                    }
                ),
                encoding="utf-8",
            )
            with self._runtime(root) as (server, _handler):
                status, payload = self._request(
                    server, "GET", "/api/mineru-accounts"
                )

            self.assertEqual(status, 200)
            self.assertEqual(len(payload["accounts"]), 1)
            self.assertEqual(payload["accounts"][0]["account_id"], "mineru-default")
            self.assertTrue(payload["accounts"][0]["configured"])
            self.assertNotIn("legacy-private-token", json.dumps(payload))
            private = json.loads(
                (config_dir / "mineru_accounts.local.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                private["accounts"]["mineru-default"]["token"],
                "legacy-private-token",
            )
            self.assertTrue(legacy_path.is_file())

    def test_statistics_endpoint_attributes_original_page_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._runtime(root) as (server, _handler):
                for account_id in ("account-1", "account-2"):
                    status, _ = self._request(
                        server,
                        "POST",
                        "/api/mineru-accounts",
                        {
                            "account_id": account_id,
                            "display_name": account_id,
                            "token": f"token-{account_id}",
                            "enabled": True,
                        },
                    )
                    self.assertEqual(status, 200)

                ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
                source = root / "corpus" / "raw_pdf" / "book.pdf"
                source.parent.mkdir(parents=True)
                source.write_bytes(b"test")
                job = ledger.create_document_job(
                    source_file_id="source-1",
                    document_id="DOCUMENT-1",
                    source_path=source,
                    source_sha256="abc123",
                    provider_id="mineru-cloud",
                    parser_model="vlm",
                    options_fingerprint="options",
                    total_pages=300,
                )
                descriptors = (
                    SliceDescriptor(1, 200, 0, source, "abc123", 4, False),
                    SliceDescriptor(201, 300, 200, source, "abc123", 4, False),
                )
                slices = ledger.add_slices(job.id, descriptors, "mineru-cloud")
                ledger.update_slice(
                    slices[0].id, status="completed", credential_id="account-1"
                )
                ledger.update_slice(
                    slices[1].id, status="completed", credential_id="account-2"
                )
                pending_status, pending_payload = self._request(
                    server, "GET", "/api/mineru-statistics"
                )
                self.assertEqual(pending_status, 200)
                self.assertEqual(pending_payload["parsed_page_count"], 0)
                ledger.refresh_progress(job.id)
                ledger.update_document(job.id, status="validated")

                status, payload = self._request(
                    server, "GET", "/api/mineru-statistics"
                )

            self.assertEqual(status, 200)
            self.assertEqual(payload["parsed_book_count"], 1)
            self.assertEqual(payload["parsed_page_count"], 300)
            by_account = {
                item["account_id"]: item for item in payload["credentials"]
            }
            self.assertEqual(
                by_account["account-1"]["books"][0]["page_ranges"], [[1, 200]]
            )
            self.assertEqual(
                by_account["account-2"]["books"][0]["page_ranges"],
                [[201, 300]],
            )

    def test_connection_test_resolves_only_the_selected_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self._runtime(root) as (server, _handler):
                status, _ = self._request(
                    server,
                    "POST",
                    "/api/mineru-accounts",
                    {
                        "account_id": "selected",
                        "display_name": "Selected",
                        "token": "selected-private-token",
                        "api_base": "https://example.test",
                        "enabled": True,
                    },
                )
                self.assertEqual(status, 200)
                with patch(
                    "src.me_finder.web.test_mineru_credential",
                    return_value={"ok": True, "latency_ms": 12},
                ) as test_credential:
                    status, payload = self._request(
                        server,
                        "POST",
                        "/api/mineru-accounts/test",
                        {"account_id": "selected"},
                    )

            self.assertEqual(status, 200)
            self.assertEqual(payload["account_id"], "selected")
            test_credential.assert_called_once_with(
                "selected-private-token", api_base="https://example.test"
            )


if __name__ == "__main__":
    unittest.main()
