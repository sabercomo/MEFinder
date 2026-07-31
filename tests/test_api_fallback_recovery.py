from __future__ import annotations

import json
import os
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from src.me_finder.database import build_database
from src.me_finder.mineru_api import (
    MinerUError,
    load_segment_manifest,
    save_segment_manifest,
    submit_local_pdf_segments,
)
from src.me_finder.vision_api import VisionAPIError
from src.me_finder.web import make_handler


class MinerUParserFallbackBoundaryTests(unittest.TestCase):
    def test_first_segment_permanent_401_allows_parser_fallback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifests = root / "manifests"

            with (
                patch(
                    "src.me_finder.mineru_api.get_pdf_page_count",
                    return_value=1,
                ),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    side_effect=MinerUError(
                        "MinerU HTTP 401: user authenticate failed",
                        allow_parser_fallback=True,
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    MinerUError,
                    "user authenticate failed",
                ) as raised:
                    submit_local_pdf_segments(
                        pdf,
                        config_path=root / "mineru.json",
                        state_dir=root / "tasks",
                        manifest_dir=manifests,
                        result_dir=root / "results",
                        data_id_prefix="source",
                        segment_size=1,
                    )

            self.assertTrue(raised.exception.allow_parser_fallback)
            checkpoint = load_segment_manifest("source", manifests)
            self.assertIsNotNone(checkpoint)
            segment = checkpoint["segments"][0]
            self.assertEqual(segment["status"], "failed")
            self.assertNotIn("batch_id", segment)

    def test_401_after_an_accepted_batch_preserves_in_place_resume(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifests = root / "manifests"

            with (
                patch(
                    "src.me_finder.mineru_api.get_pdf_page_count",
                    return_value=2,
                ),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    side_effect=[
                        {"batch_id": "accepted-batch"},
                        MinerUError(
                            "MinerU HTTP 401: user authenticate failed",
                            allow_parser_fallback=True,
                        ),
                    ],
                ),
            ):
                with self.assertRaisesRegex(
                    MinerUError,
                    "user authenticate failed",
                ) as raised:
                    submit_local_pdf_segments(
                        pdf,
                        config_path=root / "mineru.json",
                        state_dir=root / "tasks",
                        manifest_dir=manifests,
                        result_dir=root / "results",
                        data_id_prefix="source",
                        segment_size=1,
                    )

            self.assertFalse(raised.exception.allow_parser_fallback)
            checkpoint = load_segment_manifest("source", manifests)
            self.assertIsNotNone(checkpoint)
            first, second = checkpoint["segments"]
            self.assertEqual(first["status"], "submitted")
            self.assertEqual(first["batch_id"], "accepted-batch")
            self.assertEqual(second["status"], "failed")
            self.assertNotIn("batch_id", second)

    def test_failed_future_batch_does_not_block_first_segment_fallback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifests = root / "manifests"

            with (
                patch(
                    "src.me_finder.mineru_api.get_pdf_page_count",
                    return_value=2,
                ),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    side_effect=[
                        {"batch_id": "old-first"},
                        {"batch_id": "old-second"},
                    ],
                ),
            ):
                manifest = submit_local_pdf_segments(
                    pdf,
                    config_path=root / "mineru.json",
                    state_dir=root / "tasks",
                    manifest_dir=manifests,
                    result_dir=root / "results",
                    data_id_prefix="source",
                    segment_size=1,
                )

            for segment in manifest["segments"]:
                segment["status"] = "failed"
            save_segment_manifest("source", manifest, manifests)

            with (
                patch(
                    "src.me_finder.mineru_api.get_pdf_page_count",
                    return_value=2,
                ),
                patch(
                    "src.me_finder.mineru_api.submit_local_pdf",
                    side_effect=MinerUError(
                        "MinerU HTTP 401: user authenticate failed",
                        allow_parser_fallback=True,
                    ),
                ),
            ):
                with self.assertRaises(MinerUError) as raised:
                    submit_local_pdf_segments(
                        pdf,
                        config_path=root / "mineru.json",
                        state_dir=root / "tasks",
                        manifest_dir=manifests,
                        result_dir=root / "results",
                        data_id_prefix="source",
                        segment_size=1,
                    )

            self.assertTrue(raised.exception.allow_parser_fallback)


class LateProviderRecoveryHTTPTests(unittest.TestCase):
    PDF_BYTES = b"%PDF-1.4\n% fallback-recovery-test\n%%EOF\n"

    @staticmethod
    def _open(request: Request):
        return build_opener(ProxyHandler({})).open(request, timeout=5)

    @classmethod
    def _get_json(cls, url: str) -> dict[str, object]:
        with cls._open(Request(url)) as response:
            return json.loads(response.read().decode("utf-8"))

    @classmethod
    def _post_json(
        cls,
        base_url: str,
        route: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        request = Request(
            base_url + route,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with cls._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    @classmethod
    def _upload(cls, base_url: str) -> dict[str, object]:
        request = Request(
            base_url + "/api/import",
            data=cls.PDF_BYTES,
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(len(cls.PDF_BYTES)),
                "X-File-Name": "fallback-recovery.pdf",
                "X-PDF-Parse-Mode": "auto",
            },
            method="POST",
        )
        with cls._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    @classmethod
    def _wait_for_status(
        cls,
        base_url: str,
        job_id: str,
        expected: set[str],
    ) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = cls._get_json(
                base_url + "/api/import-status?job_id=" + job_id
            )
            if str(status.get("status") or "") in expected:
                return status
            time.sleep(0.02)
        raise AssertionError(
            f"import job {job_id} did not reach one of {sorted(expected)}"
        )

    @classmethod
    def _save_provider(
        cls,
        base_url: str,
        *,
        name: str = "恢复接口",
    ) -> dict[str, object]:
        return cls._post_json(
            base_url,
            "/api/vision-providers",
            {
                "action": "save_provider",
                "provider": {
                    "name": name,
                    "api_base": "https://provider.example.test/v1",
                    "model": "vision-test-model",
                    "api_key": "test-secret",
                    "enabled": True,
                },
            },
        )

    @contextmanager
    def _running_runtime(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            root.mkdir()
            (root / "data").mkdir()
            (root / "config").mkdir()
            build_database(
                {"metadata": {}},
                root / "data" / "index.sqlite3",
            )
            (root / "config" / "pdf_imports.json").write_text(
                '{"documents": []}',
                encoding="utf-8",
            )
            config_env = {
                "ME_FINDER_MINERU_CONFIG": str(
                    root / "config" / "mineru_api.local.json"
                ),
                "ME_FINDER_VISION_CONFIG": str(
                    root / "config" / "vision_api.local.json"
                ),
                "ME_FINDER_PREFERENCES": str(
                    root / "config" / "preferences.json"
                ),
            }
            previous_cwd = Path.cwd()
            handler = None
            server = None
            with (
                patch.dict(os.environ, config_env, clear=False),
                patch(
                    "src.me_finder.web.detect_imported_pdf",
                    return_value={
                        "detected_pdf_type": "scanned",
                        "pdf_page_count": 1,
                    },
                ),
                patch(
                    "src.me_finder.web.parse_pdf_with_mineru",
                    side_effect=MinerUError(
                        "MinerU HTTP 401: user authenticate failed",
                        allow_parser_fallback=True,
                    ),
                ),
                patch(
                    "src.me_finder.web.parse_pdf_with_provider",
                    side_effect=VisionAPIError(
                        "test stopped after selecting the provider"
                    ),
                ) as provider_parser,
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(
                        ("127.0.0.1", 0),
                        handler,
                    )
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    yield (
                        f"http://127.0.0.1:{server.server_port}",
                        provider_parser,
                    )
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    def _failed_import(
        self,
        base_url: str,
    ) -> tuple[str, dict[str, object]]:
        uploaded = self._upload(base_url)
        self.assertTrue(uploaded["ok"])
        job_id = str(uploaded["job_id"])
        failed = self._wait_for_status(
            base_url,
            job_id,
            {"failed"},
        )
        self.assertTrue(failed["mineru_failed"])
        self.assertFalse(failed["can_retry_with_provider"])
        self.assertTrue(failed["needs_provider_config"])
        return job_id, failed

    def test_status_and_resumable_discover_provider_saved_after_failure(
        self,
    ) -> None:
        with self._running_runtime() as (base_url, _provider_parser):
            job_id, _failed = self._failed_import(base_url)
            saved = self._save_provider(base_url)
            provider_id = str(saved["default_provider_id"])

            refreshed = self._get_json(
                base_url + "/api/import-status?job_id=" + job_id
            )
            self.assertTrue(refreshed["can_retry_with_provider"])
            self.assertEqual(refreshed["retry_provider_id"], provider_id)
            self.assertEqual(refreshed["retry_provider_name"], "恢复接口")
            self.assertFalse(refreshed["needs_provider_config"])

            resumable = self._get_json(
                base_url + "/api/import-resumable"
            )
            recovered = next(
                job
                for job in resumable["jobs"]
                if str(job.get("job_id")) == job_id
            )
            self.assertTrue(recovered["can_retry_with_provider"])
            self.assertEqual(recovered["retry_provider_id"], provider_id)
            self.assertEqual(recovered["retry_provider_name"], "恢复接口")
            self.assertFalse(recovered["needs_provider_config"])

    def test_import_retry_accepts_provider_saved_after_failure(self) -> None:
        with self._running_runtime() as (base_url, provider_parser):
            job_id, _failed = self._failed_import(base_url)
            saved = self._save_provider(base_url)
            provider_id = str(saved["default_provider_id"])

            retried = self._post_json(
                base_url,
                "/api/import-retry",
                {
                    "job_id": job_id,
                    "provider_id": provider_id,
                },
            )

            self.assertTrue(retried["ok"])
            self.assertNotEqual(retried["job_id"], job_id)
            self.assertEqual(retried["parse_route"], "vision")
            self.assertEqual(retried["provider_id"], provider_id)
            self.assertEqual(retried["provider_name"], "恢复接口")

            deadline = time.monotonic() + 5
            while (
                not provider_parser.called
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue(provider_parser.called)
            self.assertEqual(
                provider_parser.call_args.args[3],
                provider_id,
            )
            retried_status = self._wait_for_status(
                base_url,
                str(retried["job_id"]),
                {"failed"},
            )
            self.assertEqual(retried_status["status"], "failed")


if __name__ == "__main__":
    unittest.main()
