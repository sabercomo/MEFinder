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
from src.me_finder.import_queue import ImportQueueFullError
from src.me_finder.import_resume import ResumeManifestError
from src.me_finder.web import make_handler

WEB_SOURCE = "\n".join(
    Path(f"src/me_finder/{name}").read_text(encoding="utf-8")
    for name in ("web.py", "web_runtime.py")
)
ORCHESTRATOR_SOURCE = Path(
    "src/me_finder/application/import_orchestrator.py"
).read_text(encoding="utf-8")
PARSER_EXECUTOR_SOURCE = Path(
    "src/me_finder/application/import_parser_executor.py"
).read_text(encoding="utf-8")
JOB_STORE_SOURCE = Path(
    "src/me_finder/application/import_job_store.py"
).read_text(encoding="utf-8")
IMPORT_JOB_LIFECYCLE_SOURCE = Path(
    "src/me_finder/application/import_job_lifecycle.py"
).read_text(encoding="utf-8")
IMPORT_JOB_CONTROLLER_SOURCE = Path(
    "src/me_finder/import_job_controller.py"
).read_text(encoding="utf-8")
DELETION_COORDINATOR_SOURCE = Path(
    "src/me_finder/application/document_deletion_coordinator.py"
).read_text(encoding="utf-8")
DOCUMENT_IMPORT_COORDINATOR_SOURCE = Path(
    "src/me_finder/application/document_import_coordinator.py"
).read_text(encoding="utf-8")


def _read_app_source() -> str:
    """app.js 已按功能拆分到 static/js/，按文件名排序拼接还原完整源码。"""

    static_dir = Path("src/me_finder/static")
    parts = sorted((static_dir / "js").glob("*.js"), key=lambda path: path.name)
    if parts:
        return "".join(path.read_text(encoding="utf-8") for path in parts)
    return (static_dir / "app.js").read_text(encoding="utf-8")


APP_SOURCE = _read_app_source()
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
        startup_start = IMPORT_JOB_LIFECYCLE_SOURCE.index(
            "def restore_startup_jobs("
        )
        startup_end = IMPORT_JOB_LIFECYCLE_SOURCE.index(
            "def add_job(", startup_start
        )
        startup_block = IMPORT_JOB_LIFECYCLE_SOURCE[startup_start:startup_end]

        self.assertIn("restored_job = {", startup_block)
        self.assertIn("self._store.restore_job(", startup_block)
        self.assertNotIn("self._task_queue.submit", startup_block)
        self.assertNotIn("self.queue_import_job(", startup_block)

    def test_resume_requires_an_explicit_api_call_before_queueing(self) -> None:
        self.assertIn('"/api/import-resumable": (', WEB_SOURCE)
        self.assertIn('"/api/import-resume": import_job_controller.resume', WEB_SOURCE)
        resume_start = ORCHESTRATOR_SOURCE.index("def resume_import_job(")
        resume_end = ORCHESTRATOR_SOURCE.index(
            "def dismiss_import_job(", resume_start
        )
        resume_block = ORCHESTRATOR_SOURCE[resume_start:resume_end]
        self.assertIn("self._job_lifecycle.begin_resume(", resume_block)
        self.assertIn(
            "self._store.resume_candidate(job_id)",
            IMPORT_JOB_LIFECYCLE_SOURCE,
        )
        self.assertIn(
            'str(job.get("status") or "") not in {"paused", "failed"}',
            JOB_STORE_SOURCE,
        )
        self.assertIn("self.queue_import_job(", resume_block)
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
        self.assertIn(
            "resumeButton.style.display = resumeCount > 1 ? 'inline-flex' : 'none';",
            APP_SOURCE,
        )
        # 文案改成自然中文，不再是「从断点继续」翻译腔。
        self.assertNotIn("从断点继续", APP_SOURCE)

    def test_cancel_all_button_preserves_item_dismiss_and_cancels_serially(self) -> None:
        self.assertIn('id="import-cancel-all-btn"', TEMPLATE_SOURCE)
        self.assertIn('onclick="cancelAllImports()"', TEMPLATE_SOURCE)
        self.assertIn(">全部取消</button>", TEMPLATE_SOURCE)
        self.assertIn("function cancelAllImports()", APP_SOURCE)
        self.assertIn("function cancellableImportQueue()", APP_SOURCE)
        self.assertIn("var pending = cancellableImportQueue();", APP_SOURCE)
        self.assertIn("全部取消导入任务？", APP_SOURCE)
        self.assertIn("原始文件不会被删除", APP_SOURCE)
        self.assertIn(
            "await removeImport(pending[index].id, {",
            APP_SOURCE,
        )
        self.assertIn("skipConfirm: true", APP_SOURCE)
        self.assertIn(
            "cancelButton.style.display = cancelCount > 0 ? 'inline-flex' : 'none';",
            APP_SOURCE,
        )
        # 每条任务右上角的 × 和逐项继续入口必须继续存在。
        self.assertIn('class="import-item-remove" onclick="removeImport(', APP_SOURCE)
        self.assertIn("onclick=\"resumeImport(\\'", APP_SOURCE)
        self.assertIn("fetch('/api/import-resume-dismiss'", APP_SOURCE)

    def test_resume_revalidates_identity_and_prevents_duplicate_workers(self) -> None:
        resume_start = ORCHESTRATOR_SOURCE.index("def resume_import_job(")
        resume_end = ORCHESTRATOR_SOURCE.index(
            "def dismiss_import_job(", resume_start
        )
        resume_block = ORCHESTRATOR_SOURCE[resume_start:resume_end]
        self.assertIn(
            "validate_target=self.validated_import_target",
            resume_block,
        )
        self.assertIn(
            "target = validate_target(job_id, context)",
            IMPORT_JOB_LIFECYCLE_SOURCE,
        )
        self.assertIn(
            "self._store.begin_resume(",
            IMPORT_JOB_LIFECYCLE_SOURCE,
        )
        self.assertIn("self._hash_file(target)", IMPORT_JOB_LIFECYCLE_SOURCE)
        self.assertIn(
            "hash_file=lambda path: sha256_file(path)",
            ORCHESTRATOR_SOURCE,
        )
        self.assertIn("同一文献已有解析任务正在运行", JOB_STORE_SOURCE)

    def test_fallback_route_and_explicit_dismiss_are_durable(self) -> None:
        self.assertIn("def switch_import_job_route(", ORCHESTRATOR_SOURCE)
        self.assertIn(
            "jobs.switch_import_job_route(\n"
            "            job_id,\n"
            '            parse_route="vision",\n'
            "            force_mineru=False,",
            PARSER_EXECUTOR_SOURCE,
        )
        self.assertIn("jobs=self", ORCHESTRATOR_SOURCE)
        self.assertIn(
            '"/api/import-resume-dismiss": import_job_controller.dismiss',
            WEB_SOURCE,
        )
        self.assertIn("function removeImport(id, options)", APP_SOURCE)
        self.assertIn("fetch('/api/import-resume-dismiss'", APP_SOURCE)

    def test_active_queue_remove_stops_backend_parser(self) -> None:
        self.assertIn("class ImportJobCancelled(RuntimeError):", JOB_STORE_SOURCE)
        self.assertIn("ImportJobCancelled,", ORCHESTRATOR_SOURCE)
        self.assertIn("self._cancelled_job_ids.add(job_id)", JOB_STORE_SOURCE)
        self.assertIn('status="cancelling"', JOB_STORE_SOURCE)
        self.assertIn("self.ensure_import_not_cancelled(job_id)", ORCHESTRATOR_SOURCE)
        self.assertIn("self.finish_cancelled_import_job(job_id)", ORCHESTRATOR_SOURCE)
        self.assertIn("q.status === 'processing'", APP_SOURCE)
        self.assertIn("当前请求完成后不会再提交新页面", APP_SOURCE)
        self.assertIn(
            "['processing', 'paused', 'error'].indexOf(q.status) >= 0",
            APP_SOURCE,
        )

    def test_interrupted_vision_job_can_switch_to_mineru_without_upload(self) -> None:
        self.assertIn(
            '"/api/import-retry-mineru": import_job_controller.retry_with_mineru',
            WEB_SOURCE,
        )
        self.assertIn("force_mineru=True", IMPORT_JOB_CONTROLLER_SOURCE)
        self.assertIn(
            "self._imports.validated_import_target(", IMPORT_JOB_CONTROLLER_SOURCE
        )
        self.assertIn(
            "self._imports.start_retry_import_job(",
            IMPORT_JOB_CONTROLLER_SOURCE,
        )
        self.assertIn("function retryImportWithMinerU(id)", APP_SOURCE)
        self.assertIn("改用 MinerU（免费）", APP_SOURCE)
        self.assertIn("不需要重新上传文件", APP_SOURCE)
        self.assertIn("fetch('/api/import-retry-mineru'", APP_SOURCE)

    def test_transient_mineru_interruption_respects_auto_switch_setting(self) -> None:
        # Auto-switch is now governed solely by the user's setting; a transient
        # interruption no longer hard-blocks it. But without the setting on, the
        # checkpoint is kept and NO paid fallback ever starts automatically.
        failure_start = PARSER_EXECUTOR_SOURCE.index(
            "def _handle_mineru_failure("
        )
        failure_end = PARSER_EXECUTOR_SOURCE.index(
            "def _record_vision_failure(", failure_start
        )
        block = PARSER_EXECUTOR_SOURCE[failure_start:failure_end]
        # The single auto-switch gate is derived from the user's saved setting.
        self.assertIn("auto_fallback = bool(", block)
        self.assertIn('summary.get("auto_fallback_from_mineru")', block)
        # The decision NOT to auto-switch is reached before any route switch, so
        # an auto-switch can only happen past that gate.
        no_switch = block.index("if not auto_fallback:")
        switch_route = block.index("jobs.switch_import_job_route(")
        self.assertLess(no_switch, switch_route)
        # With auto-switch off, a transient interruption keeps the checkpoint and
        # never spends on a paid provider on its own.
        transient_branch = block[no_switch:switch_route]
        self.assertIn("if transient:", transient_branch)
        self.assertIn("mineru_interrupted=True", transient_branch)
        self.assertIn("can_resume=True", transient_branch)
        self.assertIn("不会自动改用其他付费接口", transient_branch)
        self.assertIn("return False", transient_branch)

    def test_paid_retry_endpoint_revalidates_mineru_failure_server_side(
        self,
    ) -> None:
        retry_start = IMPORT_JOB_CONTROLLER_SOURCE.index(
            "def retry_with_provider("
        )
        retry_end = IMPORT_JOB_CONTROLLER_SOURCE.index(
            "def resume(",
            retry_start,
        )
        retry_block = IMPORT_JOB_CONTROLLER_SOURCE[retry_start:retry_end]
        self.assertIn(
            "self._imports.is_provider_retry_eligible(",
            retry_block,
        )
        eligibility_start = ORCHESTRATOR_SOURCE.index(
            "def is_provider_retry_eligible("
        )
        eligibility_end = ORCHESTRATOR_SOURCE.index(
            "def public_import_job(",
            eligibility_start,
        )
        eligibility = ORCHESTRATOR_SOURCE[eligibility_start:eligibility_end]
        self.assertIn('str(job.get("failure_stage") or "") != "index"', eligibility)
        self.assertIn('bool(context.get("is_pdf"))', eligibility)
        self.assertIn('job.get("mineru_failed")', eligibility)
        # Interruption is now an eligible reason for an EXPLICIT retry (a stuck
        # task must not be a dead end); the endpoint still re-validates it
        # server-side rather than trusting the client. Auto-fallback stays
        # forbidden — see test_transient_mineru_interruption_never_starts_paid_fallback.
        self.assertIn('job.get("mineru_interrupted")', eligibility)

    def test_document_removal_blocks_running_parser_and_clears_old_jobs(self) -> None:
        self.assertIn(
            '"/api/documents/remove": document_lifecycle_controller.remove',
            WEB_SOURCE,
        )
        self.assertIn(
            "self._jobs.begin_source_deletion(source_file_id)",
            DELETION_COORDINATOR_SOURCE,
        )
        self.assertIn(
            "with self._index_runtime.mutation():",
            DELETION_COORDINATOR_SOURCE,
        )
        self.assertIn(
            "warnings = self._jobs.purge_source_jobs(",
            DELETION_COORDINATOR_SOURCE,
        )
        self.assertIn(
            "self._jobs.end_source_deletion(source_file_id)",
            DELETION_COORDINATOR_SOURCE,
        )
        self.assertIn("该文献仍在解析中", JOB_STORE_SOURCE)

    def test_pdf_registration_is_reserved_before_config_mutation(self) -> None:
        helper_start = ORCHESTRATOR_SOURCE.index("def register_pdf_for_import(")
        helper_end = ORCHESTRATOR_SOURCE.index(
            "def release_import_reservation(", helper_start
        )
        helper_block = ORCHESTRATOR_SOURCE[helper_start:helper_end]
        self.assertIn("content_sha256 = sha256_file(target)", helper_block)
        self.assertIn("content_sha256[:16]", helper_block)
        self.assertIn(
            "with self._index_runtime.mutation(), import_config_lock():",
            helper_block,
        )
        config_lock = helper_block.index("import_config_lock():")
        job_store_lock = helper_block.index("with self._job_store.atomic():")
        reserve = helper_block.index(
            "self._job_store.reserve_source(predicted_source_id)"
        )
        register = helper_block.index("document = register_pdf(")
        self.assertLess(config_lock, job_store_lock)
        self.assertLess(job_store_lock, reserve)
        self.assertLess(reserve, register)
        self.assertIn("self._job_store.replace_reservation(", helper_block)
        self.assertIn("self._job_store.release_reservation(", helper_block)
        self.assertIn(
            "self._jobs.register_pdf_for_import(",
            DOCUMENT_IMPORT_COORDINATOR_SOURCE,
        )
        self.assertIn(
            "original_file_name=source_path.name",
            DOCUMENT_IMPORT_COORDINATOR_SOURCE,
        )
        self.assertIn(
            "consume_reservation=bool(reserved_source_id)",
            DOCUMENT_IMPORT_COORDINATOR_SOURCE,
        )
        self.assertIn(
            "self._jobs.release_item_reservations(prepared_items)",
            DOCUMENT_IMPORT_COORDINATOR_SOURCE,
        )

    def test_delete_cleanup_failure_does_not_strand_source_reservation(self) -> None:
        coordinator_start = DELETION_COORDINATOR_SOURCE.index(
            "class DocumentDeletionCoordinator:"
        )
        removal_start = DELETION_COORDINATOR_SOURCE.index(
            "def remove(", coordinator_start
        )
        removal_end = DELETION_COORDINATOR_SOURCE.index(
            "def remove_many(", removal_start
        )
        removal_block = DELETION_COORDINATOR_SOURCE[
            removal_start:removal_end
        ]
        self.assertIn("return self._perform_removal(", removal_block)
        self.assertIn("finally:", removal_block)
        self.assertIn(
            "self._jobs.end_source_deletion(source_file_id)", removal_block
        )
        self.assertIn(
            "warnings = self._jobs.purge_source_jobs(",
            DELETION_COORDINATOR_SOURCE,
        )
        purge_start = IMPORT_JOB_LIFECYCLE_SOURCE.index(
            "def purge_source_jobs("
        )
        purge_end = IMPORT_JOB_LIFECYCLE_SOURCE.index(
            "def rollback_unqueued_batch(", purge_start
        )
        self.assertIn(
            "logging.warning(",
            IMPORT_JOB_LIFECYCLE_SOURCE[purge_start:purge_end],
        )


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

    def test_import_boundary_io_failures_are_json_500_responses(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            previous_cwd = Path.cwd()
            handler = None
            server = None
            try:
                os.chdir(root.parent)
                root.mkdir()
                os.chdir(root)
                handler, server = self._runtime(root)
                base_url = f"http://127.0.0.1:{server.server_port}"
                cases = (
                    (
                        "resume_import_job",
                        "/api/import-resume",
                        "继续导入任务失败：journal read-only",
                    ),
                    (
                        "dismiss_import_job",
                        "/api/import-resume-dismiss",
                        "移除导入任务失败：journal read-only",
                    ),
                )
                for method_name, route, expected_error in cases:
                    with self.subTest(route=route), patch.object(
                        handler.import_orchestrator,
                        method_name,
                        side_effect=OSError("journal read-only"),
                    ):
                        with self.assertRaises(HTTPError) as caught:
                            self._post_json(
                                base_url,
                                route,
                                {"job_id": "job-one"},
                            )
                        self.assertEqual(caught.exception.code, 500)
                        self.assertEqual(
                            caught.exception.headers.get_content_type(),
                            "application/json",
                        )
                        self.assertEqual(
                            json.loads(
                                caught.exception.read().decode("utf-8")
                            ),
                            {"error": expected_error},
                        )

                retry_job = {
                    "job_id": "old-job",
                    "status": "failed",
                    "parse_route": "vision",
                    "failure_stage": "parse",
                    "file_name": "paper.pdf",
                }
                retry_context = {
                    "target": "/runtime/paper.pdf",
                    "source_file_id": "pdf-one",
                    "profile": {"detected_pdf_type": "scanned"},
                    "is_pdf": True,
                }
                for route, payload in (
                    (
                        "/api/import-retry-mineru",
                        {"job_id": "old-job"},
                    ),
                    (
                        "/api/import-retry",
                        {
                            "job_id": "old-job",
                            "provider_id": "provider-one",
                        },
                    ),
                ):
                    for retry_error in (
                        OSError("journal read-only"),
                        ResumeManifestError("前任务清单损坏"),
                    ):
                        with self.subTest(
                            route=route,
                            error=type(retry_error).__name__,
                        ), patch.object(
                            handler.import_orchestrator,
                            "job_and_context",
                            return_value=(retry_job, retry_context),
                        ), patch.object(
                            handler.import_orchestrator,
                            "is_provider_retry_eligible",
                            return_value=True,
                        ), patch.object(
                            handler.import_orchestrator,
                            "validated_import_target",
                            return_value=Path("/runtime/paper.pdf"),
                        ), patch.object(
                            handler.import_orchestrator,
                            "start_retry_import_job",
                            side_effect=retry_error,
                        ), patch.object(
                            handler.import_job_controller,
                            "_vision_summary",
                            return_value={
                                "providers": [
                                    {
                                        "id": "provider-one",
                                        "name": "Provider One",
                                        "enabled": True,
                                        "configured": True,
                                    }
                                ]
                            },
                        ):
                            with self.assertRaises(
                                HTTPError
                            ) as retry_response:
                                self._post_json(base_url, route, payload)
                            self.assertEqual(
                                retry_response.exception.code,
                                500,
                            )
                            self.assertEqual(
                                retry_response.exception.headers.get_content_type(),
                                "application/json",
                            )
                            self.assertEqual(
                                json.loads(
                                    retry_response.exception.read().decode(
                                        "utf-8"
                                    )
                                ),
                                {
                                    "error": (
                                        "创建重试任务失败："
                                        f"{retry_error}"
                                    )
                                },
                            )

                with patch.object(
                    handler.document_imports,
                    "import_stream",
                    side_effect=OSError("journal read-only"),
                ):
                    raw_request = Request(
                        base_url + "/api/import",
                        data=self.PDF_BYTES,
                        headers={
                            "Content-Type": "application/pdf",
                            "X-File-Name": "paper.pdf",
                        },
                        method="POST",
                    )
                    with self.assertRaises(HTTPError) as raw_response:
                        self._open(raw_request)
                self.assertEqual(raw_response.exception.code, 500)
                self.assertEqual(
                    json.loads(
                        raw_response.exception.read().decode("utf-8")
                    ),
                    {"error": "导入失败，请查看 desktop.log。"},
                )

                for method_name, route in (
                    (
                        "finish_chunked",
                        "/api/import-upload/finish",
                    ),
                    ("import_local", "/api/import-local"),
                ):
                    with self.subTest(route=route), patch.object(
                        handler.document_imports,
                        method_name,
                        side_effect=OSError("journal read-only"),
                    ):
                        payload = (
                            {"upload_id": "upload-one"}
                            if method_name == "finish_chunked"
                            else {"paths": ["/library/paper.pdf"]}
                        )
                        with self.assertRaises(HTTPError) as response:
                            self._post_json(base_url, route, payload)
                        self.assertEqual(response.exception.code, 500)
                        self.assertEqual(
                            response.exception.headers.get_content_type(),
                            "application/json",
                        )
                        self.assertEqual(
                            json.loads(
                                response.exception.read().decode("utf-8")
                            ),
                            {"error": "导入失败，请查看 desktop.log。"},
                        )

                invalid_raw = Request(
                    base_url + "/api/import",
                    data=self.PDF_BYTES,
                    headers={
                        "Content-Type": "application/pdf",
                        "X-File-Name": "paper.pdf",
                        "X-PDF-Parse-Mode": "invalid",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as invalid_raw_response:
                    self._open(invalid_raw)
                self.assertEqual(invalid_raw_response.exception.code, 400)

                with patch.object(
                    handler.document_imports,
                    "finish_chunked",
                    side_effect=ValueError("invalid finish request"),
                ):
                    with self.assertRaises(HTTPError) as invalid_finish:
                        self._post_json(
                            base_url,
                            "/api/import-upload/finish",
                            {"upload_id": "upload-one"},
                        )
                self.assertEqual(invalid_finish.exception.code, 400)

                for route, payload in (
                    ("/api/import-local", {"paths": []}),
                    ("/api/import-retry-mineru", {}),
                    ("/api/import-retry", {}),
                ):
                    with self.subTest(invalid_route=route):
                        with self.assertRaises(HTTPError) as invalid_response:
                            self._post_json(base_url, route, payload)
                        self.assertEqual(invalid_response.exception.code, 400)

                with self.assertRaises(HTTPError) as invalid_local:
                    self._post_json(base_url, "/api/import-local", [])
                self.assertEqual(invalid_local.exception.code, 400)
                self.assertEqual(
                    json.loads(invalid_local.exception.read().decode("utf-8")),
                    {"error": "本地导入请求必须是 JSON 对象。"},
                )
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
                os.chdir(previous_cwd)

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
                    "src.me_finder.web_runtime.detect_imported_pdf",
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
                    "src.me_finder.web_runtime.detect_imported_pdf",
                    return_value={
                        "detected_pdf_type": "native_text",
                        "pdf_page_count": 1,
                    },
                ), patch(
                    "src.me_finder.import_queue.ImportTaskQueue.submit",
                    side_effect=ImportQueueFullError("queue unavailable"),
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
                    "src.me_finder.web_runtime.detect_imported_pdf",
                    return_value={
                        "detected_pdf_type": "broken_text",
                        "pdf_page_count": 1,
                    },
                ), patch(
                    "src.me_finder.web_runtime.parse_pdf_with_mineru",
                    return_value=None,
                ) as mineru, patch(
                    "src.me_finder.web_runtime.extract_pdf_source",
                    side_effect=fake_pdf_extraction,
                ), patch(
                    "src.me_finder.web_runtime.replace_source_in_database",
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
