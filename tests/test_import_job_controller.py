from __future__ import annotations

import unittest
from pathlib import Path

from src.me_finder.application.document_query_service import (
    DocumentQueryError,
)
from src.me_finder.import_job_controller import ImportJobController
from src.me_finder.mineru_api import MinerUError
from src.me_finder.vision_api import VisionAPIError


class FakeImportOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.status_result: dict[str, object] | None = None
        self.resumable_result: list[dict[str, object]] = []
        self.active_result: dict[str, object] | None = None
        self.retry_inputs: (
            tuple[dict[str, object], dict[str, object]] | None
        ) = None
        self.provider_retry_eligible = True
        self.validated_target = Path("/runtime/document.pdf")
        self.start_result = "new-job"
        self.resume_result: dict[str, object] = {}
        self.dismiss_result = "dismissed"
        self.errors: dict[str, Exception] = {}

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))
        error = self.errors.get(name)
        if error is not None:
            raise error

    def job_status(self, job_id: str):
        self._record("status", job_id)
        return self.status_result

    def resumable_import_jobs(self):
        self._record("resumable")
        return self.resumable_result

    def active_job_for_source(self, source_file_id: str):
        self._record("active", source_file_id)
        return self.active_result

    def start_import_job(self, *args: object, **kwargs: object):
        self._record("start", args, kwargs)
        return self.start_result

    def start_retry_import_job(self, *args: object, **kwargs: object):
        self._record("start_retry", args, kwargs)
        return self.start_result

    def job_and_context(self, job_id: str):
        self._record("job_and_context", job_id)
        return self.retry_inputs

    def is_provider_retry_eligible(self, job, context):
        self._record("eligible", job, context)
        return self.provider_retry_eligible

    def validated_import_target(self, job_id: str, context):
        self._record("validated_target", job_id, context)
        return self.validated_target

    def resume_import_job(self, job_id: str):
        self._record("resume", job_id)
        return self.resume_result

    def dismiss_import_job(self, job_id: str):
        self._record("dismiss", job_id)
        return self.dismiss_result


class ImportJobControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.imports = FakeImportOrchestrator()
        self.source_records: dict[str, dict[str, object]] = {}
        self.source_path_result = Path("/runtime/source.pdf")
        self.detect_result: dict[str, object] = {
            "detected_pdf_type": "scanned",
            "pdf_page_count": 12,
        }
        self.vision_result: dict[str, object] = {"providers": []}
        self.dependency_errors: dict[str, Exception] = {}
        self.dependency_calls: list[tuple[object, ...]] = []
        self.controller = ImportJobController(
            self.imports,
            source_record=self._source_record,
            source_path=self._source_path,
            detect_pdf=self._detect_pdf,
            vision_summary=self._vision_summary,
        )

    def _dependency(self, name: str, *args: object) -> None:
        self.dependency_calls.append((name, *args))
        error = self.dependency_errors.get(name)
        if error is not None:
            raise error

    def _source_record(self, source_file_id: str):
        self._dependency("source_record", source_file_id)
        return self.source_records.get(source_file_id)

    def _source_path(self, source_file_id: str):
        self._dependency("source_path", source_file_id)
        return self.source_path_result

    def _detect_pdf(self, path: Path):
        self._dependency("detect_pdf", path)
        return self.detect_result

    def _vision_summary(self):
        self._dependency("vision_summary")
        return self.vision_result

    def _vision_retry_inputs(self) -> None:
        self.imports.retry_inputs = (
            {
                "job_id": "old-job",
                "status": "failed",
                "parse_route": "vision",
                "failure_stage": "parse",
                "file_name": "显示名称.pdf",
            },
            {
                "target": self.imports.validated_target,
                "source_file_id": "pdf-one",
                "profile": {"detected_pdf_type": "scanned"},
                "is_pdf": True,
            },
        )

    def test_status_and_resumable_preserve_public_shapes(self) -> None:
        self.assertEqual(
            self.controller.status(None),
            (404, {"error": "导入任务不存在。"}),
        )
        self.assertEqual(self.imports.calls, [])

        public_job = {"job_id": "job-one", "status": "processing"}
        self.imports.status_result = public_job
        self.assertEqual(self.controller.status("job-one"), (200, public_job))
        self.assertEqual(self.imports.calls, [("status", "job-one")])

        self.imports.status_result = None
        self.assertEqual(
            self.controller.status("missing"),
            (404, {"error": "导入任务不存在。"}),
        )
        self.imports.resumable_result = [
            {"job_id": "paused-one", "status": "paused"}
        ]
        self.assertEqual(
            self.controller.resumable(),
            (200, {"jobs": self.imports.resumable_result}),
        )

    def test_reparse_rejects_invalid_or_non_pdf_sources(self) -> None:
        for payload in (None, [], {}):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.controller.reparse_with_mineru(payload),
                    (400, {"error": "invalid request"}),
                )
        self.assertEqual(self.dependency_calls, [])

        self.source_records["word-one"] = {"source_type": "docx"}
        self.assertEqual(
            self.controller.reparse_with_mineru(
                {"source_id": "word-one"}
            ),
            (400, {"error": "PDF 文献未找到。"}),
        )
        self.assertNotIn(("active", "word-one"), self.imports.calls)

    def test_reparse_reuses_an_active_job_without_touching_the_file(self) -> None:
        self.source_records["pdf-one"] = {
            "source_type": "pdf",
            "file_name": "论文.pdf",
        }
        self.imports.active_result = {
            "job_id": "running-job",
            "detected_pdf_type": "broken_text",
        }
        self.assertEqual(
            self.controller.reparse_with_mineru(
                {"source_id": "pdf-one"}
            ),
            (
                200,
                {
                    "ok": True,
                    "job_id": "running-job",
                    "already_running": True,
                    "detected_pdf_type": "broken_text",
                },
            ),
        )
        self.assertEqual(
            self.dependency_calls,
            [("source_record", "pdf-one")],
        )
        self.assertEqual(self.imports.calls, [("active", "pdf-one")])

    def test_reparse_detects_and_starts_a_forced_mineru_job(self) -> None:
        self.source_records["pdf-one"] = {
            "source_type": "pdf",
            "file_name": "展示名.pdf",
        }
        self.assertEqual(
            self.controller.reparse_with_mineru(
                {"source_id": "pdf-one"}
            ),
            (
                200,
                {
                    "ok": True,
                    "job_id": "new-job",
                    "already_running": False,
                    "detected_pdf_type": "scanned",
                },
            ),
        )
        self.assertEqual(
            self.dependency_calls,
            [
                ("source_record", "pdf-one"),
                ("source_path", "pdf-one"),
                ("detect_pdf", self.source_path_result),
            ],
        )
        self.assertEqual(
            self.imports.calls,
            [
                ("active", "pdf-one"),
                (
                    "start",
                    (
                        self.source_path_result,
                        self.detect_result,
                        "pdf-one",
                        True,
                    ),
                    {
                        "force_mineru": True,
                        "display_file_name": "展示名.pdf",
                    },
                ),
            ],
        )

    def test_reparse_keeps_existing_error_mapping(self) -> None:
        self.source_records["pdf-one"] = {
            "source_type": "pdf",
            "file_name": "论文.pdf",
        }
        self.dependency_errors["source_path"] = DocumentQueryError(
            "原始文件不存在。"
        )
        self.assertEqual(
            self.controller.reparse_with_mineru(
                {"source_id": "pdf-one"}
            ),
            (400, {"error": "原始文件不存在。"}),
        )

        self.dependency_errors["source_path"] = RuntimeError("读取器崩溃")
        self.assertEqual(
            self.controller.reparse_with_mineru(
                {"source_id": "pdf-one"}
            ),
            (500, {"error": "提交 MinerU 解析失败：读取器崩溃"}),
        )

    def test_retry_with_mineru_validates_server_side_eligibility(self) -> None:
        for payload in (None, [], {}, {"job_id": "  "}):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.controller.retry_with_mineru(payload),
                    (400, {"error": "缺少原导入任务。"}),
                )

        self.assertEqual(
            self.controller.retry_with_mineru({"job_id": "missing"}),
            (404, {"error": "原导入任务不存在。"}),
        )

        invalid_cases = [
            ({"status": "processing", "parse_route": "vision"}, True),
            ({"status": "failed", "parse_route": "vision"}, False),
            ({"status": "failed", "parse_route": "native"}, True),
            (
                {
                    "status": "failed",
                    "parse_route": "vision",
                    "failure_stage": "index",
                },
                True,
            ),
        ]
        for previous_job, is_pdf in invalid_cases:
            with self.subTest(previous_job=previous_job, is_pdf=is_pdf):
                self.imports.retry_inputs = (
                    previous_job,
                    {"is_pdf": is_pdf},
                )
                self.assertEqual(
                    self.controller.retry_with_mineru(
                        {"job_id": "old-job"}
                    ),
                    (
                        400,
                        {
                            "error": (
                                "只有已中断的视觉解析任务可以改用 MinerU。"
                            )
                        },
                    ),
                )

    def test_retry_with_mineru_uses_one_durable_replacement(self) -> None:
        self._vision_retry_inputs()
        previous_job, context = self.imports.retry_inputs
        self.assertEqual(
            self.controller.retry_with_mineru({"job_id": " old-job "}),
            (
                200,
                {
                    "ok": True,
                    "job_id": "new-job",
                    "parse_route": "mineru",
                },
            ),
        )
        self.assertEqual(
            self.imports.calls,
            [
                ("job_and_context", "old-job"),
                ("validated_target", "old-job", context),
                (
                    "start_retry",
                    (
                        "old-job",
                        self.imports.validated_target,
                        dict(context["profile"]),
                        "pdf-one",
                        True,
                    ),
                    {
                        "previous_statuses": ("paused", "failed"),
                        "force_mineru": True,
                        "display_file_name": previous_job["file_name"],
                    },
                ),
            ],
        )

    def test_retry_with_mineru_does_not_dismiss_after_validation_error(self) -> None:
        self._vision_retry_inputs()
        self.imports.errors["validated_target"] = MinerUError(
            "原文件已变化。"
        )
        self.assertEqual(
            self.controller.retry_with_mineru({"job_id": "old-job"}),
            (400, {"error": "原文件已变化。"}),
        )
        self.assertFalse(
            any(
                call[0] in {"start_retry", "dismiss"}
                for call in self.imports.calls
            )
        )

    def test_retry_durable_swap_failure_has_stable_500_response(self) -> None:
        self._vision_retry_inputs()
        self.imports.errors["start_retry"] = OSError("journal read-only")

        self.assertEqual(
            self.controller.retry_with_mineru({"job_id": "old-job"}),
            (500, {"error": "创建重试任务失败：journal read-only"}),
        )

    def test_provider_retry_rejects_missing_ineligible_and_unavailable(self) -> None:
        for payload in (
            None,
            [],
            {},
            {"job_id": "old-job"},
            {"provider_id": "provider-one"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.controller.retry_with_provider(payload),
                    (400, {"error": "缺少原任务或备用解析接口。"}),
                )

        self.assertEqual(
            self.controller.retry_with_provider(
                {"job_id": "missing", "provider_id": "provider-one"}
            ),
            (404, {"error": "原导入任务不存在。"}),
        )

        self._vision_retry_inputs()
        previous_job, context = self.imports.retry_inputs
        previous_job["status"] = "paused"
        self.assertEqual(
            self.controller.retry_with_provider(
                {"job_id": "old-job", "provider_id": "provider-one"}
            ),
            (400, {"error": "只有失败的导入任务可以切换接口重试。"}),
        )

        previous_job["status"] = "failed"
        self.imports.provider_retry_eligible = False
        self.assertEqual(
            self.controller.retry_with_provider(
                {"job_id": "old-job", "provider_id": "provider-one"}
            ),
            (
                400,
                {
                    "error": (
                        "该任务不是可切换接口的 PDF 解析失败；"
                        "索引失败和 Word 导入不能改走视觉解析 API。"
                    )
                },
            ),
        )

        self.imports.provider_retry_eligible = True
        self.assertEqual(
            self.controller.retry_with_provider(
                {"job_id": "old-job", "provider_id": "provider-one"}
            ),
            (400, {"error": "所选备用解析接口不可用。"}),
        )
        self.assertIn(("validated_target", "old-job", context), self.imports.calls)

    def test_provider_retry_filters_provider_and_preserves_order(self) -> None:
        self._vision_retry_inputs()
        previous_job, context = self.imports.retry_inputs
        self.vision_result = {
            "providers": [
                {"id": "provider-one", "enabled": False, "configured": True},
                {"id": "provider-one", "enabled": True, "configured": False},
                {
                    "id": "provider-one",
                    "name": "备用接口",
                    "enabled": True,
                    "configured": True,
                },
            ]
        }
        self.assertEqual(
            self.controller.retry_with_provider(
                {"job_id": "old-job", "provider_id": "provider-one"}
            ),
            (
                200,
                {
                    "ok": True,
                    "job_id": "new-job",
                    "provider_id": "provider-one",
                    "provider_name": "备用接口",
                    "parse_route": "vision",
                },
            ),
        )
        self.assertEqual(
            self.imports.calls,
            [
                ("job_and_context", "old-job"),
                ("eligible", previous_job, context),
                ("validated_target", "old-job", context),
                (
                    "start_retry",
                    (
                        "old-job",
                        self.imports.validated_target,
                        dict(context["profile"]),
                        "pdf-one",
                        True,
                    ),
                    {
                        "previous_statuses": ("failed",),
                        "vision_provider_id": "provider-one",
                        "display_file_name": previous_job["file_name"],
                    },
                ),
            ],
        )
        self.assertEqual(self.dependency_calls, [("vision_summary",)])

    def test_provider_retry_maps_target_summary_and_start_errors(self) -> None:
        self._vision_retry_inputs()
        request = {"job_id": "old-job", "provider_id": "provider-one"}
        self.imports.errors["validated_target"] = MinerUError("原文件已变化。")
        self.assertEqual(
            self.controller.retry_with_provider(request),
            (400, {"error": "原文件已变化。"}),
        )
        self.assertEqual(self.dependency_calls, [])

        self.imports.errors.clear()
        self.imports.calls.clear()
        self.dependency_errors["vision_summary"] = VisionAPIError(
            "配置损坏。"
        )
        self.assertEqual(
            self.controller.retry_with_provider(request),
            (400, {"error": "配置损坏。"}),
        )

        self.dependency_errors.clear()
        self.imports.calls.clear()
        self.vision_result = {
            "providers": [
                {
                    "id": "provider-one",
                    "enabled": True,
                    "configured": True,
                }
            ]
        }
        self.imports.errors["start_retry"] = MinerUError("无法创建任务。")
        self.assertEqual(
            self.controller.retry_with_provider(request),
            (400, {"error": "无法创建任务。"}),
        )
        self.assertFalse(any(call[0] == "dismiss" for call in self.imports.calls))

    def test_resume_and_dismiss_preserve_fields_and_errors(self) -> None:
        for operation, message in (
            (self.controller.resume, "缺少待继续的导入任务。"),
            (self.controller.dismiss, "缺少待移除的导入任务。"),
        ):
            for payload in (None, [], {}, {"job_id": "  "}):
                with self.subTest(operation=operation.__name__, payload=payload):
                    self.assertEqual(
                        operation(payload),
                        (400, {"error": message}),
                    )

        self.imports.resume_result = {
            "parse_route": "vision",
            "provider_id": "provider-one",
            "provider_name": "备用接口",
        }
        self.assertEqual(
            self.controller.resume({"job_id": " resume-one "}),
            (
                200,
                {
                    "ok": True,
                    "job_id": "resume-one",
                    "parse_route": "vision",
                    "provider_id": "provider-one",
                    "provider_name": "备用接口",
                },
            ),
        )
        self.assertEqual(
            self.controller.dismiss({"job_id": " active-one "}),
            (
                200,
                {
                    "ok": True,
                    "job_id": "active-one",
                    "state": "dismissed",
                },
            ),
        )

        self.imports.errors["resume"] = MinerUError("任务不可继续。")
        self.assertEqual(
            self.controller.resume({"job_id": "resume-one"}),
            (400, {"error": "任务不可继续。"}),
        )
        self.imports.errors["dismiss"] = ValueError("任务不存在。")
        self.assertEqual(
            self.controller.dismiss({"job_id": "active-one"}),
            (400, {"error": "任务不存在。"}),
        )


if __name__ == "__main__":
    unittest.main()
