"""Parser-route execution for durable document import jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol

from ..mineru_api import MinerUError
from ..vision_api import (
    VisionAPIError,
    resolve_vision_config_path,
    vision_config_summary,
)
from .import_job_store import ImportJobCancelled, Job


Parser = Callable[..., object]


class ImportParserJobPort(Protocol):
    """Job-state operations required while executing one parser route."""

    def job_status(self, job_id: str) -> Optional[Job]:
        ...

    def update_import_job(self, job_id: str, **updates: object) -> None:
        ...

    def progress_import_job(
        self,
        job_id: str,
        update: Dict[str, object],
    ) -> None:
        ...

    def switch_import_job_route(
        self,
        job_id: str,
        *,
        parse_route: str,
        force_mineru: bool,
        vision_provider_id: Optional[str],
        provider_name: Optional[str],
    ) -> None:
        ...


class ImportParserExecutor:
    """Run the selected parser and apply the configured fallback policy."""

    def __init__(
        self,
        root: Path,
        *,
        parse_with_mineru: Parser,
        parse_with_provider: Parser,
    ) -> None:
        self._root = Path(root)
        self._parse_with_mineru = parse_with_mineru
        self._parse_with_provider = parse_with_provider

    def execute(
        self,
        job_id: str,
        target: Path,
        source_file_id: str,
        profile: Mapping[str, object],
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
        *,
        jobs: ImportParserJobPort,
    ) -> bool:
        """Return true after parsing, or false after recording a parser failure."""

        use_vision = bool(is_pdf and vision_provider_id)
        try:
            use_mineru = is_pdf and not use_vision and (
                force_mineru
                or str(profile.get("detected_pdf_type")) != "native_text"
            )
            if use_vision:
                job = jobs.job_status(job_id)
                provider_name = str(
                    (job or {}).get("provider_name") or "其他视觉 API"
                )
                jobs.update_import_job(
                    job_id,
                    phase="vision_processing",
                    message=f"正在使用 {provider_name} 逐页解析 PDF…",
                    parse_route="vision",
                )
                self._parse_with_provider(
                    self._root,
                    target,
                    source_file_id,
                    str(vision_provider_id),
                    on_progress=lambda update: jobs.progress_import_job(
                        job_id,
                        update,
                    ),
                )
            elif use_mineru:
                succeeded = self._execute_mineru(
                    job_id,
                    target,
                    source_file_id,
                    force_mineru=force_mineru,
                    jobs=jobs,
                )
                if not succeeded:
                    return False
            else:
                jobs.update_import_job(
                    job_id,
                    phase="text_parsing",
                    message="原生文本，使用快速解析，正在建立索引…",
                    parse_route="native",
                )
            return True
        except ImportJobCancelled:
            raise
        except Exception as exc:
            if use_vision:
                self._record_vision_failure(
                    job_id,
                    vision_provider_id=vision_provider_id,
                    exc=exc,
                    jobs=jobs,
                )
                return False
            jobs.update_import_job(
                job_id,
                status="failed",
                phase="failed",
                message=str(exc),
                vision_failed=False,
                mineru_failed=False,
                mineru_interrupted=False,
                can_retry_with_provider=False,
                retry_provider_id=None,
                retry_provider_name=None,
                needs_provider_config=False,
            )
            return False

    def _execute_mineru(
        self,
        job_id: str,
        target: Path,
        source_file_id: str,
        *,
        force_mineru: bool,
        jobs: ImportParserJobPort,
    ) -> bool:
        message = (
            "已选择 MinerU 在线解析，正在上传 PDF…"
            if force_mineru
            else "文本层不可靠，正在自动提交 MinerU…"
        )
        jobs.update_import_job(
            job_id,
            phase="mineru_submitting",
            message=message,
            parse_route="mineru",
        )
        try:
            self._parse_with_mineru(
                self._root,
                target,
                source_file_id,
                on_progress=lambda update: jobs.progress_import_job(
                    job_id,
                    update,
                ),
            )
        except ImportJobCancelled:
            raise
        except Exception as exc:
            return self._handle_mineru_failure(
                job_id,
                target,
                source_file_id,
                exc=exc,
                jobs=jobs,
            )
        return True

    def _handle_mineru_failure(
        self,
        job_id: str,
        target: Path,
        source_file_id: str,
        *,
        exc: Exception,
        jobs: ImportParserJobPort,
    ) -> bool:
        transient = not isinstance(exc, MinerUError) or not exc.allow_parser_fallback
        try:
            summary = vision_config_summary(
                resolve_vision_config_path(self._root)
            )
        except VisionAPIError:
            summary = {
                "providers": [],
                "default_provider_id": None,
                "auto_fallback_from_mineru": False,
            }
        providers = self._configured_providers(summary)
        fallback = providers[0] if providers else None
        auto_fallback = bool(
            summary.get("auto_fallback_from_mineru") and fallback
        )
        if not auto_fallback:
            if transient:
                jobs.update_import_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    can_resume=True,
                    message=(
                        f"MinerU 任务暂时中断：{exc}。"
                        "断点已保存，点「继续导入」复用断点、"
                        "不额外计费；不会自动改用其他付费接口，"
                        "如已配置可手动改用"
                        "（放弃断点、从头解析）。"
                    ),
                    mineru_interrupted=True,
                    mineru_failed=False,
                    can_retry_with_provider=bool(fallback),
                    retry_provider_id=(fallback.get("id") if fallback else None),
                    retry_provider_name=(
                        fallback.get("name") if fallback else None
                    ),
                    needs_provider_config=False,
                    original_error=str(exc),
                )
                return False
            message = f"MinerU 解析失败：{exc}"
            if fallback:
                fallback_display_name = fallback.get("name") or "其他解析 API"
                message += (
                    f"。可手动改用 {fallback_display_name}；"
                    "也可在设置中开启失败后自动切换。"
                )
            else:
                message += "。可在设置中配置其他解析 API 后自行切换。"
            jobs.update_import_job(
                job_id,
                status="failed",
                phase="failed",
                message=message,
                mineru_failed=True,
                can_retry_with_provider=bool(fallback),
                retry_provider_id=(fallback.get("id") if fallback else None),
                retry_provider_name=(fallback.get("name") if fallback else None),
                needs_provider_config=not bool(fallback),
                mineru_interrupted=False,
                original_error=str(exc),
            )
            return False

        fallback_id = str(fallback.get("id"))
        fallback_name = str(fallback.get("name") or "其他视觉 API")
        switch_reason = "任务暂时中断" if transient else "解析失败"
        jobs.switch_import_job_route(
            job_id,
            parse_route="vision",
            force_mineru=False,
            vision_provider_id=fallback_id,
            provider_name=fallback_name,
        )
        jobs.update_import_job(
            job_id,
            phase="vision_processing",
            message=(
                f"MinerU {switch_reason}，已按设置自动切换到 "
                f"{fallback_name}…"
            ),
            parse_route="vision",
            provider_id=fallback_id,
            provider_name=fallback_name,
            mineru_failed=True,
            mineru_interrupted=False,
            fallback_used=True,
            original_error=str(exc),
        )
        try:
            self._parse_with_provider(
                self._root,
                target,
                source_file_id,
                fallback_id,
                on_progress=lambda update: jobs.progress_import_job(
                    job_id,
                    update,
                ),
            )
        except ImportJobCancelled:
            raise
        except Exception as fallback_exc:
            jobs.update_import_job(
                job_id,
                status="failed",
                phase="failed",
                message=(
                    f"MinerU {switch_reason}；自动切换到 "
                    f"{fallback_name} 后仍失败：{fallback_exc}"
                ),
                fallback_error=str(fallback_exc),
                vision_failed=True,
                can_retry_with_provider=True,
                retry_provider_id=fallback_id,
                retry_provider_name=fallback_name,
                needs_provider_config=False,
            )
            return False
        return True

    def _record_vision_failure(
        self,
        job_id: str,
        *,
        vision_provider_id: Optional[str],
        exc: Exception,
        jobs: ImportParserJobPort,
    ) -> None:
        try:
            summary = vision_config_summary(
                resolve_vision_config_path(self._root)
            )
        except (OSError, ValueError, VisionAPIError):
            summary = {"providers": []}
        providers = self._configured_providers(summary)
        current_provider_id = str(vision_provider_id or "")
        retry_provider = next(
            (
                item
                for item in providers
                if str(item.get("id") or "") != current_provider_id
            ),
            providers[0] if providers else None,
        )
        jobs.update_import_job(
            job_id,
            status="failed",
            phase="failed",
            message=str(exc),
            vision_failed=True,
            mineru_failed=False,
            mineru_interrupted=False,
            can_retry_with_provider=bool(retry_provider),
            retry_provider_id=(
                retry_provider.get("id") if retry_provider else None
            ),
            retry_provider_name=(
                retry_provider.get("name") if retry_provider else None
            ),
            needs_provider_config=not bool(retry_provider),
            original_error=str(exc),
        )

    @staticmethod
    def _configured_providers(
        summary: Mapping[str, object],
    ) -> list[Mapping[str, object]]:
        return [
            item
            for item in summary.get("providers", [])
            if isinstance(item, dict)
            and item.get("enabled")
            and item.get("configured")
        ]
