from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import (
    ProxyHandler,
    Request,
    build_opener,
)

from src.me_finder.database import (
    build_database,
    replace_source_in_database as real_replace_source,
)
from src.me_finder.web import make_handler

WEB_SOURCE = Path("src/me_finder/web.py").read_text(encoding="utf-8")
APP_SOURCE = Path("src/me_finder/static/app.js").read_text(encoding="utf-8")
TEMPLATE_SOURCE = Path("src/me_finder/templates/index.html").read_text(encoding="utf-8")


def fake_pdf_extraction(
    path: Path,
    root: Path,
    config: dict[str, object],
    parsed_dir: Path | None = None,
) -> dict[str, list[dict[str, object]]]:
    del root, parsed_dir
    source_id = str(config["source_file_id"])
    document_id = str(config["document_id"])
    text = "MinerU 已完成后的本地索引文本"
    return {
        "source_files": [
            {
                "source_file_id": source_id,
                "source_type": "pdf",
                "document_id": document_id,
                "file_name": path.name,
                "relative_path": f"corpus/raw_pdf/{path.name}",
            }
        ],
        "volumes": [
            {
                "volume_id": document_id,
                "source_file_id": source_id,
                "source_type": "pdf",
                "display_title": path.stem,
            }
        ],
        "works": [
            {
                "work_id": f"{document_id}-W0001",
                "volume_id": document_id,
                "source_type": "pdf",
                "title": path.stem,
            }
        ],
        "paragraphs": [
            {
                "paragraph_id": f"{source_id}-P000000",
                "volume_id": document_id,
                "work_id": f"{document_id}-W0001",
                "source_file_id": source_id,
                "source_type": "pdf",
                "paragraph_index": 0,
                "eligible_for_search": True,
                "text_raw": text,
                "normalized_text": text,
                "compact_text": text,
                "plain_text": text,
            }
        ],
        "toc_entries": [],
        "page_anchors": [],
        "pdf_pages": [],
        "pdf_page_mappings": [],
        "pdf_import_runs": [],
        "audit_issues": [],
    }


class ImportResumeWebWiringTests(unittest.TestCase):
    def test_startup_restores_paused_jobs_without_automatic_queue_submission(self) -> None:
        startup_start = WEB_SOURCE.index(
            "for saved_job in import_job_journal.load_startup_jobs():"
        )
        startup_end = WEB_SOURCE.index(
            "def update_import_job(", startup_start
        )
        startup_block = WEB_SOURCE[startup_start:startup_end]

        self.assertIn("restored_job = {", startup_block)
        self.assertIn(
            "import_jobs[saved_job_id] = restored_job",
            startup_block,
        )
        self.assertIn('import_job_contexts[saved_job_id] = {', startup_block)
        self.assertNotIn("import_task_queue.submit", startup_block)
        self.assertNotIn("queue_import_job(", startup_block)

    def test_resume_requires_an_explicit_api_call_before_queueing(self) -> None:
        self.assertIn('parsed.path == "/api/import-resumable"', WEB_SOURCE)
        self.assertIn('parsed.path == "/api/import-resume"', WEB_SOURCE)
        resume_start = WEB_SOURCE.index("def resume_import_job(")
        resume_end = WEB_SOURCE.index(
            "def start_native_import_batch(", resume_start
        )
        resume_block = WEB_SOURCE[resume_start:resume_end]
        self.assertIn('str(job.get("status") or "") not in {"paused", "failed"}', resume_block)
        self.assertIn("queue_import_job(", resume_block)
        self.assertIn("function loadResumableImports()", APP_SOURCE)
        self.assertIn("function resumeImport(id, options)", APP_SOURCE)
        self.assertIn("可能产生费用", APP_SOURCE)
        self.assertIn("fetch('/api/import-resume'", APP_SOURCE)

    def test_resume_all_button_continues_every_pending_import_serially(self) -> None:
        # 多个中断任务时，提供批量继续入口，同时保留逐项操作。
        self.assertIn('id="import-resume-all-btn"', TEMPLATE_SOURCE)
        self.assertIn("全部继续导入", TEMPLATE_SOURCE)
        self.assertIn("function resumeAllImports()", APP_SOURCE)
        self.assertIn("function resumableImportQueue()", APP_SOURCE)
        self.assertIn("function syncResumeAllButton()", APP_SOURCE)
        # 串行发起，一次汇总确认，逐个不再各弹一次。
        self.assertIn(
            "await resumeImport(pending[index].id, {silent: true, skipConfirm: true});",
            APP_SOURCE,
        )
        # 只有多于一个可继续任务时才显示批量按钮。
        self.assertIn("resumeButton.style.display = count > 1 ? 'inline-flex' : 'none';", APP_SOURCE)
        # 文案改成自然中文，不再是「从断点继续」翻译腔。
        self.assertNotIn("从断点继续", APP_SOURCE)

    def test_cancel_all_button_preserves_item_dismiss_and_cancels_serially(self) -> None:
        self.assertIn('id="import-cancel-all-btn"', TEMPLATE_SOURCE)
        self.assertIn('onclick="cancelAllImports()"', TEMPLATE_SOURCE)
        self.assertIn(">全部取消</button>", TEMPLATE_SOURCE)
        self.assertIn("function cancelAllImports()", APP_SOURCE)
        self.assertIn("全部取消中断任务？", APP_SOURCE)
        self.assertIn("原始文件不会被删除", APP_SOURCE)
        self.assertIn(
            "await removeImport(pending[index].id, {silent: true, deferRender: true})",
            APP_SOURCE,
        )
        self.assertIn("cancelButton.style.display = count > 1 ? 'inline-flex' : 'none';", APP_SOURCE)
        # 每条任务右上角的 × 和逐项继续入口必须继续存在。
        self.assertIn('class="import-item-remove" onclick="removeImport(', APP_SOURCE)
        self.assertIn("onclick=\"resumeImport(\\'", APP_SOURCE)
        self.assertIn("fetch('/api/import-resume-dismiss'", APP_SOURCE)

    def test_resume_revalidates_identity_and_prevents_duplicate_workers(self) -> None:
        resume_start = WEB_SOURCE.index("def resume_import_job(")
        resume_end = WEB_SOURCE.index(
            "def dismiss_import_job(", resume_start
        )
        resume_block = WEB_SOURCE[resume_start:resume_end]
        self.assertIn("validated_import_target(job_id, context)", resume_block)
        self.assertIn("already_running", resume_block)
        self.assertIn("sha256_file(target)", WEB_SOURCE)
        self.assertIn("同一文献已有解析任务正在运行", WEB_SOURCE)

    def test_fallback_route_and_explicit_dismiss_are_durable(self) -> None:
        self.assertIn("def switch_import_job_route(", WEB_SOURCE)
        self.assertIn(
            "switch_import_job_route(\n"
            "                        job_id,\n"
            '                        parse_route="vision",\n'
            "                        force_mineru=False,",
            WEB_SOURCE,
        )
        self.assertIn('parsed.path == "/api/import-resume-dismiss"', WEB_SOURCE)
        self.assertIn("function removeImport(id, options)", APP_SOURCE)
        self.assertIn("fetch('/api/import-resume-dismiss'", APP_SOURCE)

    def test_transient_mineru_interruption_never_starts_paid_fallback(self) -> None:
        prepare_start = WEB_SOURCE.index("def prepare_import_job(")
        prepare_end = WEB_SOURCE.index("def run_import_job(", prepare_start)
        prepare_block = WEB_SOURCE[prepare_start:prepare_end]
        guard = prepare_block.index("not isinstance(mineru_exc, MinerUError)")
        fallback_lookup = prepare_block.index("vision_config_summary(")
        self.assertLess(guard, fallback_lookup)
        guarded_block = prepare_block[guard:fallback_lookup]
        self.assertIn("not mineru_exc.allow_parser_fallback", guarded_block)
        self.assertIn("return False", guarded_block)
        self.assertIn("不会自动改用其他付费接口", guarded_block)

    def test_paid_retry_endpoint_revalidates_mineru_failure_server_side(
        self,
    ) -> None:
        retry_start = WEB_SOURCE.index(
            'if parsed.path == "/api/import-retry":'
        )
        retry_end = WEB_SOURCE.index(
            'if parsed.path == "/api/import-resume":',
            retry_start,
        )
        retry_block = WEB_SOURCE[retry_start:retry_end]
        self.assertIn(
            "is_provider_retry_eligible(previous_job, context)",
            retry_block,
        )
        eligibility_start = WEB_SOURCE.index(
            "def is_provider_retry_eligible("
        )
        eligibility_end = WEB_SOURCE.index(
            "def public_import_job(",
            eligibility_start,
        )
        eligibility = WEB_SOURCE[eligibility_start:eligibility_end]
        self.assertIn('str(job.get("failure_stage") or "") != "index"', eligibility)
        self.assertIn("not job.get(\"mineru_interrupted\")", eligibility)
        self.assertIn('bool(context.get("is_pdf"))', eligibility)
        self.assertIn('job.get("mineru_failed")', eligibility)

    def test_document_removal_blocks_running_parser_and_clears_old_jobs(self) -> None:
        removal_start = WEB_SOURCE.index(
            'if parsed.path == "/api/documents/remove":'
        )
        removal_end = WEB_SOURCE.index(
            'if parsed.path == "/api/bibliographic-metadata/detect":',
            removal_start,
        )
        removal_block = WEB_SOURCE[removal_start:removal_end]
        self.assertIn("status=409", removal_block)
        self.assertIn("仍在解析中", removal_block)
        self.assertIn("sid in deleting_import_sources", removal_block)
        self.assertIn("sid in pending_import_sources", removal_block)
        self.assertIn("deleting_import_sources.add(sid)", removal_block)
        self.assertIn("with rebuild_lock:", removal_block)
        self.assertIn("import_job_journal.delete_job", removal_block)
        self.assertIn("import_job_contexts.pop", removal_block)
        self.assertIn("finally:", removal_block)
        self.assertIn("deleting_import_sources.discard(sid)", removal_block)

    def test_pdf_registration_is_reserved_before_config_mutation(self) -> None:
        helper_start = WEB_SOURCE.index("def register_pdf_for_import(")
        helper_end = WEB_SOURCE.index(
            "def release_import_reservation(", helper_start
        )
        helper_block = WEB_SOURCE[helper_start:helper_end]
        self.assertIn("sha256_file(target)[:16]", helper_block)
        self.assertIn("with rebuild_lock, import_jobs_lock:", helper_block)
        reserve = helper_block.index(
            "_reserve_import_source_locked(predicted_source_id)"
        )
        register = helper_block.index("document = register_pdf(")
        self.assertLess(reserve, register)
        self.assertIn("pending_import_sources.discard", helper_block)
        self.assertIn("register_pdf_for_import(", WEB_SOURCE)
        self.assertIn("original_file_name=source_path.name", WEB_SOURCE)
        self.assertIn(
            "consume_reservation=bool(reserved_source_id)",
            WEB_SOURCE,
        )
        self.assertIn("release_item_reservations(prepared_items)", WEB_SOURCE)

    def test_delete_cleanup_failure_does_not_strand_source_reservation(self) -> None:
        removal_start = WEB_SOURCE.index(
            'if parsed.path == "/api/documents/remove":'
        )
        removal_end = WEB_SOURCE.index(
            'if parsed.path == "/api/bibliographic-metadata/detect":',
            removal_start,
        )
        removal_block = WEB_SOURCE[removal_start:removal_end]
        cleanup = removal_block.index(
            "import_job_journal.delete_job(stale_job_id)"
        )
        release = removal_block.rindex(
            "deleting_import_sources.discard(sid)"
        )
        self.assertLess(cleanup, release)
        self.assertIn("journal_cleanup_warnings", removal_block)
        self.assertIn("logging.warning(", removal_block)


class SinglePDFReservationTests(unittest.TestCase):
    PDF_BYTES = b"%PDF-1.4\n% resumable-import-test\n%%EOF\n"

    def _runtime(self, root: Path):
        (root / "data").mkdir(parents=True)
        (root / "config").mkdir(parents=True)
        build_database({"metadata": {}}, root / "data" / "index.sqlite3")
        (root / "config" / "pdf_imports.json").write_text(
            '{"documents": []}',
            encoding="utf-8",
        )
        handler = make_handler(root / "data" / "index.sqlite3")
        handler.log_message = lambda *_args: None
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return handler, server

    @staticmethod
    def _open(request: Request):
        return build_opener(ProxyHandler({})).open(request, timeout=5)

    def _upload(self, base_url: str):
        request = Request(
            base_url + "/api/import",
            data=self.PDF_BYTES,
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(len(self.PDF_BYTES)),
                "X-File-Name": "reservation.pdf",
                "X-PDF-Parse-Mode": "auto",
            },
            method="POST",
        )
        return self._open(request)

    def _remove(self, base_url: str, source_id: str):
        request = Request(
            base_url + "/api/documents/remove",
            data=json.dumps({"source_id": source_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._open(request)

    def _job_status(self, base_url: str, job_id: str) -> dict[str, object]:
        with self._open(
            Request(base_url + "/api/import-status?job_id=" + job_id)
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _wait_for_status(
        self,
        base_url: str,
        job_id: str,
        expected: set[str],
    ) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self._job_status(base_url, job_id)
            if str(status.get("status") or "") in expected:
                return status
            time.sleep(0.02)
        self.fail(f"job {job_id} did not reach {sorted(expected)}")

    def _post_json(
        self,
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
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_successful_upload_consumes_pending_reservation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            previous_cwd = Path.cwd()
            handler = None
            server = None
            try:
                os.chdir(root.parent)
                root.mkdir()
                os.chdir(root)
                with patch(
                    "src.me_finder.web.detect_imported_pdf",
                    return_value={
                        "detected_pdf_type": "native_text",
                        "pdf_page_count": 1,
                    },
                ), patch(
                    "src.me_finder.import_queue.ImportTaskQueue.submit",
                    return_value=None,
                ):
                    handler, server = self._runtime(root)
                    base_url = (
                        f"http://127.0.0.1:{server.server_port}"
                    )
                    with self._upload(base_url) as response:
                        uploaded = json.loads(
                            response.read().decode("utf-8")
                        )
                    self.assertTrue(uploaded["ok"])
                    with self.assertRaises(HTTPError) as caught:
                        self._remove(base_url, uploaded["source_file_id"])
                    self.assertEqual(caught.exception.code, 409)
                    error = json.loads(
                        caught.exception.read().decode("utf-8")
                    )["error"]
                    self.assertIn("仍在解析中", error)
                    self.assertNotIn("准备导入", error)
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
                os.chdir(previous_cwd)

    def test_queue_failure_is_preserved_as_a_resumable_task(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            previous_cwd = Path.cwd()
            handler = None
            server = None
            source_id = "pdf-import-" + hashlib.sha256(
                self.PDF_BYTES
            ).hexdigest()[:16]
            try:
                os.chdir(root.parent)
                root.mkdir()
                os.chdir(root)
                with patch(
                    "src.me_finder.web.detect_imported_pdf",
                    return_value={
                        "detected_pdf_type": "native_text",
                        "pdf_page_count": 1,
                    },
                ), patch(
                    "src.me_finder.import_queue.ImportTaskQueue.submit",
                    side_effect=RuntimeError("queue unavailable"),
                ):
                    handler, server = self._runtime(root)
                    base_url = (
                        f"http://127.0.0.1:{server.server_port}"
                    )
                    with self._upload(base_url) as response:
                        uploaded = json.loads(
                            response.read().decode("utf-8")
                        )
                    self.assertTrue(uploaded["ok"])
                    job_id = str(uploaded["job_id"])
                    status = self._job_status(base_url, job_id)
                    self.assertEqual(status["status"], "failed")
                    self.assertEqual(status["phase"], "queue_failed")
                    self.assertEqual(status["failure_stage"], "queue")
                    self.assertTrue(status["can_resume"])

                    with self._open(
                        Request(base_url + "/api/import-resumable")
                    ) as response:
                        resumable = json.loads(
                            response.read().decode("utf-8")
                        )
                    self.assertIn(
                        job_id,
                        {
                            str(item["job_id"])
                            for item in resumable["jobs"]
                        },
                    )
                    journal = (
                        root
                        / "corpus"
                        / "processed"
                        / "import_jobs"
                        / f"{job_id}.json"
                    )
                    self.assertTrue(journal.is_file())
                    config = json.loads(
                        (
                            root / "config" / "pdf_imports.json"
                        ).read_text(encoding="utf-8")
                    )
                    document = next(
                        item
                        for item in config["documents"]
                        if item["source_file_id"] == source_id
                    )
                    self.assertTrue(
                        (
                            root
                            / "corpus"
                            / "raw_pdf"
                            / str(document["file_name"])
                        ).is_file()
                    )

                    with self.assertRaises(HTTPError) as remove_error:
                        self._remove(base_url, source_id)
                    self.assertEqual(remove_error.exception.code, 400)
                    payload = json.loads(
                        remove_error.exception.read().decode("utf-8")
                    )
                    self.assertIn("文献不存在", payload["error"])
                    self.assertNotIn("准备导入", payload["error"])
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
                os.chdir(previous_cwd)

    def test_index_only_resume_survives_restart_and_legacy_failure_record(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            previous_cwd = Path.cwd()
            handler = None
            server = None
            replace_calls = 0

            def flaky_replace(*args, **kwargs):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint failed: source_files.source_file_id"
                    )
                return real_replace_source(*args, **kwargs)

            try:
                os.chdir(root.parent)
                root.mkdir()
                os.chdir(root)
                with patch(
                    "src.me_finder.web.detect_imported_pdf",
                    return_value={
                        "detected_pdf_type": "broken_text",
                        "pdf_page_count": 1,
                    },
                ), patch(
                    "src.me_finder.web.parse_pdf_with_mineru",
                    return_value=None,
                ) as mineru, patch(
                    "src.me_finder.web.extract_pdf_source",
                    side_effect=fake_pdf_extraction,
                ), patch(
                    "src.me_finder.web.replace_source_in_database",
                    side_effect=flaky_replace,
                ):
                    handler, server = self._runtime(root)
                    base_url = f"http://127.0.0.1:{server.server_port}"
                    with self._upload(base_url) as response:
                        uploaded = json.loads(
                            response.read().decode("utf-8")
                        )
                    job_id = str(uploaded["job_id"])
                    failed = self._wait_for_status(
                        base_url, job_id, {"failed"}
                    )
                    self.assertEqual(failed["phase"], "index_failed")
                    self.assertEqual(failed["failure_stage"], "index")
                    self.assertEqual(mineru.call_count, 1)

                    journal_path = (
                        root
                        / "corpus"
                        / "processed"
                        / "import_jobs"
                        / f"{job_id}.json"
                    )
                    persisted = json.loads(
                        journal_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(persisted["failure_stage"], "index")
                    self.assertTrue(persisted["can_resume"])

                    server.shutdown()
                    server.server_close()
                    server = None
                    handler.close_runtime()
                    handler = None

                    # v0.2.2 recorded this exact index error without a
                    # failure_stage field. Simulate upgrading with that
                    # unfinished task still on disk.
                    legacy = json.loads(
                        journal_path.read_text(encoding="utf-8")
                    )
                    legacy.pop("failure_stage", None)
                    legacy["phase"] = "failed"
                    legacy["message"] = (
                        "文件已解析，但批量重建索引失败："
                        "UNIQUE constraint failed: "
                        "source_files.source_file_id"
                    )
                    journal_path.write_text(
                        json.dumps(legacy, ensure_ascii=False),
                        encoding="utf-8",
                    )

                    handler = make_handler(
                        root / "data" / "index.sqlite3"
                    )
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(
                        ("127.0.0.1", 0), handler
                    )
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    base_url = f"http://127.0.0.1:{server.server_port}"

                    with self._open(
                        Request(base_url + "/api/import-resumable")
                    ) as response:
                        resumable = json.loads(
                            response.read().decode("utf-8")
                        )
                    restored = next(
                        item
                        for item in resumable["jobs"]
                        if item["job_id"] == job_id
                    )
                    self.assertEqual(restored["failure_stage"], "index")
                    self.assertEqual(restored["phase"], "index_failed")

                    resumed = self._post_json(
                        base_url,
                        "/api/import-resume",
                        {"job_id": job_id},
                    )
                    self.assertTrue(resumed["ok"])
                    completed = self._wait_for_status(
                        base_url, job_id, {"completed"}
                    )
                    self.assertEqual(completed["status"], "completed")
                    self.assertEqual(mineru.call_count, 1)
                    self.assertEqual(replace_calls, 2)
                    self.assertFalse(journal_path.exists())
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
                os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
