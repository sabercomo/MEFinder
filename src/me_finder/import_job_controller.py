"""Transport-neutral JSON responses for import job controls."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from .application.document_query_service import DocumentQueryError
from .application.import_job_lifecycle import ImportJobRetrySwapFailed
from .application.import_orchestrator import ImportOrchestrator
from .import_resume import ResumeManifestError
from .mineru_api import MinerUError
from .vision_api import VisionAPIError


ImportJobResponse = Tuple[int, Dict[str, object]]
SourceRecord = Callable[[str], Optional[Mapping[str, object]]]
SourcePath = Callable[[str], Path]
PDFDetector = Callable[[Path], Dict[str, object]]
VisionSummary = Callable[[], Dict[str, object]]
LocalMinerUSummary = Callable[[], Dict[str, object]]


class ImportJobController:
    """Validate JSON controls and preserve public import-job responses."""

    def __init__(
        self,
        imports: ImportOrchestrator,
        *,
        source_record: SourceRecord,
        source_path: SourcePath,
        detect_pdf: PDFDetector,
        vision_summary: VisionSummary,
        local_mineru_summary: LocalMinerUSummary,
    ) -> None:
        self._imports = imports
        self._source_record = source_record
        self._source_path = source_path
        self._detect_pdf = detect_pdf
        self._vision_summary = vision_summary
        self._local_mineru_summary = local_mineru_summary

    def status(self, job_id: object) -> ImportJobResponse:
        public_job = (
            self._imports.job_status(str(job_id)) if job_id else None
        )
        if not public_job:
            return 404, {"error": "导入任务不存在。"}
        return 200, public_job

    def resumable(self) -> ImportJobResponse:
        return 200, {"jobs": self._imports.resumable_import_jobs()}

    def reparse_with_mineru(self, payload: object) -> ImportJobResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_id = str(payload.get("source_id") or "")
        if not source_file_id:
            return 400, {"error": "invalid request"}
        try:
            record = self._source_record(source_file_id)
            if not record or str(record.get("source_type")) != "pdf":
                raise MinerUError("PDF 文献未找到。")
            running = self._imports.active_job_for_source(source_file_id)
            if running:
                return 200, {
                    "ok": True,
                    "job_id": running["job_id"],
                    "already_running": True,
                    "detected_pdf_type": running.get(
                        "detected_pdf_type"
                    ),
                }
            target = self._source_path(source_file_id)
            profile = self._detect_pdf(target)
            job_id = self._imports.start_import_job(
                target,
                profile,
                source_file_id,
                True,
                force_mineru=True,
                display_file_name=str(record.get("file_name") or ""),
            )
        except (MinerUError, DocumentQueryError) as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"提交 MinerU 解析失败：{exc}"}
        return 200, {
            "ok": True,
            "job_id": job_id,
            "already_running": False,
            "detected_pdf_type": profile.get("detected_pdf_type"),
        }

    def retry_with_mineru(self, payload: object) -> ImportJobResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "缺少原导入任务。"}
        previous_job_id = str(payload.get("job_id") or "").strip()
        if not previous_job_id:
            return 400, {"error": "缺少原导入任务。"}
        retry_inputs = self._imports.job_and_context(previous_job_id)
        if retry_inputs is None:
            return 404, {"error": "原导入任务不存在。"}
        previous_job, context = retry_inputs
        if (
            str(previous_job.get("status") or "")
            not in {"paused", "failed"}
            or not bool(context.get("is_pdf"))
            or str(previous_job.get("parse_route") or "") != "vision"
            or str(previous_job.get("failure_stage") or "") == "index"
        ):
            return 400, {
                "error": "只有已中断的视觉解析任务可以改用 MinerU。"
            }
        try:
            target = self._imports.validated_import_target(
                previous_job_id,
                context,
            )
            job_id = self._imports.start_retry_import_job(
                previous_job_id,
                target,
                dict(context["profile"]),
                str(context["source_file_id"]),
                True,
                previous_statuses=("paused", "failed"),
                force_mineru=True,
                display_file_name=str(
                    previous_job.get("file_name") or ""
                ),
            )
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (
            ImportJobRetrySwapFailed,
            KeyError,
            OSError,
            ValueError,
            ResumeManifestError,
        ) as exc:
            return 500, {"error": f"创建重试任务失败：{exc}"}
        return 200, {
            "ok": True,
            "job_id": job_id,
            "parse_route": "mineru",
        }

    def retry_with_provider(self, payload: object) -> ImportJobResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "缺少原任务或备用解析接口。"}
        previous_job_id = str(payload.get("job_id") or "").strip()
        provider_id = str(payload.get("provider_id") or "").strip()
        if not previous_job_id or not provider_id:
            return 400, {"error": "缺少原任务或备用解析接口。"}
        retry_inputs = self._imports.job_and_context(previous_job_id)
        if retry_inputs is None:
            return 404, {"error": "原导入任务不存在。"}
        previous_job, context = retry_inputs
        if previous_job.get("status") != "failed":
            return 400, {
                "error": "只有失败的导入任务可以切换接口重试。"
            }
        if not self._imports.is_provider_retry_eligible(
            previous_job,
            context,
        ):
            return 400, {
                "error": (
                    "该任务不是可切换接口的 PDF 解析失败；"
                    "索引失败和 Word 导入不能改走视觉解析 API。"
                )
            }
        try:
            target = self._imports.validated_import_target(
                previous_job_id,
                context,
            )
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (KeyError, OSError, ValueError) as exc:
            return 500, {"error": f"创建重试任务失败：{exc}"}
        try:
            summary = self._vision_summary()
            provider = next(
                (
                    item
                    for item in summary.get("providers", [])
                    if isinstance(item, dict)
                    and str(item.get("id")) == provider_id
                    and item.get("enabled")
                    and item.get("configured")
                ),
                None,
            )
            if provider is None:
                raise VisionAPIError("所选备用解析接口不可用。")
            job_id = self._imports.start_retry_import_job(
                previous_job_id,
                target,
                dict(context["profile"]),
                str(context["source_file_id"]),
                bool(context["is_pdf"]),
                previous_statuses=("failed",),
                vision_provider_id=provider_id,
                display_file_name=str(
                    previous_job.get("file_name") or ""
                ),
            )
        except (MinerUError, VisionAPIError) as exc:
            return 400, {"error": str(exc)}
        except (
            ImportJobRetrySwapFailed,
            KeyError,
            OSError,
            ValueError,
            ResumeManifestError,
        ) as exc:
            return 500, {"error": f"创建重试任务失败：{exc}"}
        return 200, {
            "ok": True,
            "job_id": job_id,
            "provider_id": provider_id,
            "provider_name": provider.get("name"),
            "parse_route": "vision",
        }

    def retry_with_local_mineru(
        self,
        payload: object,
    ) -> ImportJobResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "缺少原导入任务。"}
        previous_job_id = str(payload.get("job_id") or "").strip()
        if not previous_job_id:
            return 400, {"error": "缺少原导入任务。"}
        retry_inputs = self._imports.job_and_context(previous_job_id)
        if retry_inputs is None:
            return 404, {"error": "原导入任务不存在。"}
        previous_job, context = retry_inputs
        if not (
            str(previous_job.get("status") or "") == "failed"
            and bool(context.get("is_pdf"))
            and str(previous_job.get("parse_route") or "") == "mineru"
            and str(previous_job.get("provider_id") or "") != "mineru-local"
            and str(previous_job.get("failure_stage") or "") != "index"
            and (
                previous_job.get("mineru_failed")
                or previous_job.get("mineru_interrupted")
            )
        ):
            return 400, {"error": "只有失败的在线 MinerU 任务可以切换到本地部署。"}
        try:
            if not self._local_mineru_summary().get("enabled"):
                raise MinerUError("请先在设置中启用 MinerU 本地部署。")
            target = self._imports.validated_import_target(
                previous_job_id,
                context,
            )
            profile = dict(context["profile"])
            profile["mineru_local_retry"] = True
            job_id = self._imports.start_retry_import_job(
                previous_job_id,
                target,
                profile,
                str(context["source_file_id"]),
                True,
                previous_statuses=("failed",),
                force_mineru=True,
                display_file_name=str(previous_job.get("file_name") or ""),
            )
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (
            ImportJobRetrySwapFailed,
            KeyError,
            OSError,
            ValueError,
            ResumeManifestError,
        ) as exc:
            return 500, {"error": f"创建本地重试任务失败：{exc}"}
        return 200, {
            "ok": True,
            "job_id": job_id,
            "parse_route": "mineru",
            "provider_id": "mineru-local",
            "provider_name": "本地 MinerU",
        }

    def resume(self, payload: object) -> ImportJobResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "缺少待继续的导入任务。"}
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            return 400, {"error": "缺少待继续的导入任务。"}
        try:
            resumed = self._imports.resume_import_job(job_id)
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (KeyError, OSError, ValueError, ResumeManifestError) as exc:
            return 500, {"error": f"继续导入任务失败：{exc}"}
        return 200, {
            "ok": True,
            "job_id": job_id,
            "parse_route": resumed.get("parse_route"),
            "provider_id": resumed.get("provider_id"),
            "provider_name": resumed.get("provider_name"),
        }

    def dismiss(self, payload: object) -> ImportJobResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "缺少待移除的导入任务。"}
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            return 400, {"error": "缺少待移除的导入任务。"}
        try:
            dismiss_state = self._imports.dismiss_import_job(job_id)
        except (MinerUError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except (KeyError, OSError, ResumeManifestError) as exc:
            return 500, {"error": f"移除导入任务失败：{exc}"}
        return 200, {
            "ok": True,
            "job_id": job_id,
            "state": dismiss_state,
        }
