"""HTTP-independent orchestration for durable document import jobs."""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from ..app_context import AppPaths
from ..bibliographic_metadata import metadata_missing_fields
from ..import_job_journal import ImportJobJournal
from ..import_queue import (
    ImportQueueClosedError,
    ImportQueueFullError,
)
from ..import_resume import sha256_file
from ..lifecycle import DurableOperationGate
from ..mineru_api import MinerUError, resolve_mineru_config_path
from ..mineru_local_settings import mineru_local_config_summary
from ..mineru_local_provider import MINERU_LOCAL_PROVIDER_ID
from ..local_ocr_settings import local_ocr_available
from ..pdf_import_service import (
    import_config_lock,
    load_import_config,
    locked_import_config,
    register_pdf,
    reuse_registered_pdf_copy,
)
from ..vision_api import (
    VisionAPIError,
    resolve_vision_config_path,
    vision_config_summary,
)
from .import_batch_executor import ImportBatchExecutor
from .import_job_store import (
    ImportJobCancelled,
    ImportJobStore,
    Job,
    JobContext,
)
from .import_job_lifecycle import ImportJobLifecycle
from .import_parser_executor import ImportParserExecutor


ProgressCallback = Callable[[Dict[str, object]], None]
Parser = Callable[..., object]
PDFExtractor = Callable[..., Dict[str, object]]
MetadataDetector = Callable[[str], Dict[str, object]]
MetadataPersister = Callable[[str, Dict[str, object]], Dict[str, object]]


class IndexRuntimePort(Protocol):
    """The index operations required by import orchestration."""

    def mutation(self) -> AbstractContextManager[None]:
        ...

    def rebuild(
        self,
        on_progress: ProgressCallback,
        expected_source_ids: Sequence[str] = (),
    ) -> set[str]:
        ...

    def replace_source(
        self,
        extracted: Mapping[str, object],
        expected_source_id: str,
        *,
        backup_existing: bool = False,
    ) -> None:
        ...


class TaskQueuePort(Protocol):
    def submit(self, task: Callable[..., None], *args: object) -> None:
        ...


class ImportOrchestrator:
    """Coordinate durable import workflows with the live index."""

    def __init__(
        self,
        paths: AppPaths,
        index_runtime: IndexRuntimePort,
        durable_operations: DurableOperationGate,
        task_queue: TaskQueuePort,
        job_journal: ImportJobJournal,
        *,
        parse_with_mineru: Parser,
        parse_with_provider: Parser,
        extract_pdf: PDFExtractor,
        detect_metadata: MetadataDetector,
        persist_metadata: MetadataPersister,
        parse_with_local_ocr: Optional[Parser] = None,
        job_store: Optional[ImportJobStore] = None,
    ) -> None:
        self._paths = paths
        self._root = paths.runtime_root
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations
        self._task_queue = task_queue
        self._job_journal = job_journal
        self._batch_executor = ImportBatchExecutor(task_queue)
        self._parser_executor = ImportParserExecutor(
            self._root,
            parse_with_mineru=parse_with_mineru,
            parse_with_provider=parse_with_provider,
            parse_with_local_ocr=parse_with_local_ocr,
        )
        self._extract_pdf = extract_pdf
        self._detect_metadata = detect_metadata
        self._persist_metadata = persist_metadata
        self._job_store = job_store if job_store is not None else ImportJobStore()
        self._job_lifecycle = ImportJobLifecycle(
            self._job_journal,
            self._job_store,
            hash_file=lambda path: sha256_file(path),
        )

        self._restore_startup_jobs()

    @staticmethod
    def infer_import_failure_stage(
        job: Mapping[str, object],
        *,
        is_pdf: bool,
    ) -> Optional[str]:
        explicit = str(job.get("failure_stage") or "").strip()
        if explicit:
            return explicit
        if not is_pdf:
            return None
        phase = str(job.get("phase") or "").strip()
        message = str(job.get("message") or "")
        if phase == "index_failed" or any(
            marker in message
            for marker in (
                "文件已解析，但批量重建索引失败",
                "UNIQUE constraint failed: source_files.source_file_id",
            )
        ):
            return "index"
        return None

    def _restore_startup_jobs(self) -> None:
        self._job_lifecycle.restore_startup_jobs(
            self.infer_import_failure_stage,
        )

    def register_background_job(self, job: Mapping[str, object]) -> None:
        """Register a non-durable task shown through the shared job UI."""

        self._job_store.register_background_job(job)

    def register_background_job_unless_processing(
        self,
        job_id_prefix: str,
        job: Mapping[str, object],
    ) -> Optional[Job]:
        return self._job_store.register_background_job_unless_processing(
            job_id_prefix,
            job,
        )

    def submit_background_task(self, task: Callable[..., None], *args: object) -> None:
        self._task_queue.submit(task, *args)

    def update_import_job(self, job_id: str, **updates: object) -> None:
        self._job_lifecycle.update_job(job_id, **updates)

    def replace_imported_source(
        self,
        job_id: str,
        extracted: Mapping[str, object],
        source_file_id: str,
    ) -> None:
        """Publish an already parsed source without invoking any parser."""

        self.update_import_job(
            job_id,
            phase="rebuilding_index",
            message="正在把解析结果写入本地 SQLite 索引…",
        )
        with self._durable_operations.operation():
            self._index_runtime.replace_source(
                extracted,
                source_file_id,
                backup_existing=False,
            )

    def progress_import_job(self, job_id: str, update: Dict[str, object]) -> None:
        self._job_lifecycle.progress_job(
            job_id,
            update,
            self.update_import_job,
        )

    def ensure_import_not_cancelled(self, job_id: str) -> None:
        self._job_lifecycle.ensure_not_cancelled(job_id)

    def finish_cancelled_import_job(self, job_id: str) -> None:
        self._job_lifecycle.finish_cancelled_job(job_id)

    def switch_import_job_route(
        self,
        job_id: str,
        *,
        parse_route: str,
        force_mineru: bool,
        vision_provider_id: Optional[str],
        provider_name: Optional[str],
    ) -> None:
        self._job_lifecycle.switch_route(
            job_id,
            parse_route=parse_route,
            force_mineru=bool(force_mineru),
            vision_provider_id=vision_provider_id,
            provider_name=provider_name,
        )

    def validated_import_target(
        self,
        job_id: str,
        context: Mapping[str, object],
    ) -> Path:
        return self._job_lifecycle.validated_target(
            job_id,
            context,
            self.update_import_job,
        )

    def configured_pdf_for_index(
        self,
        source_file_id: str,
    ) -> Tuple[Path, Dict[str, object]]:
        config_path = self._root / "config" / "pdf_imports.json"
        config = load_import_config(config_path)
        document = next(
            (
                item
                for item in config.get("documents", [])
                if isinstance(item, dict)
                and str(item.get("source_file_id") or "") == source_file_id
            ),
            None,
        )
        if document is None:
            raise MinerUError(f"PDF 配置中找不到该文献：{source_file_id}")
        file_name = str(document.get("file_name") or "").strip()
        if not file_name:
            raise MinerUError("PDF 配置缺少文件名。")
        path = Path(file_name)
        if not path.is_absolute():
            path = self._root / "corpus" / "raw_pdf" / path
        if not path.is_file():
            raise MinerUError(f"PDF 原文件不存在：{path.name}")
        return path, document

    def rebuild_runtime_index(
        self,
        job_id: str,
        expected_source_ids: Optional[List[str]] = None,
    ) -> set[str]:
        self.update_import_job(
            job_id,
            phase="rebuilding_index",
            message="正在重建本地 SQLite 索引…",
        )
        return self._index_runtime.rebuild(
            lambda update: self.progress_import_job(job_id, update),
            expected_source_ids or (),
        )

    def index_registered_pdf(
        self,
        job_id: str,
        source_file_id: str,
        *,
        backup_existing: bool = False,
    ) -> None:
        with self._index_runtime.mutation():
            self.update_import_job(
                job_id,
                phase="text_parsing",
                message="正在读取 PDF 文本并写入本地索引…",
            )
            path, document = self.configured_pdf_for_index(source_file_id)
            display_name = str(document.get("original_file_name") or path.name)
            try:
                extracted = self._extract_pdf(
                    path,
                    self._root,
                    document,
                    parsed_dir=self._root / "corpus" / "parsed" / "pdf",
                )
            except Exception as exc:
                raise MinerUError(
                    f"{display_name} 未能进入索引：{type(exc).__name__}: {exc}"
                ) from exc
            extracted_sources = [
                item
                for item in extracted.get("source_files", [])
                if isinstance(item, dict)
            ]
            if (
                len(extracted_sources) != 1
                or str(extracted_sources[0].get("source_file_id") or "")
                != source_file_id
            ):
                raise MinerUError(
                    f"{display_name} 未能进入索引："
                    "解析结果缺少对应的文献记录。"
                )
            self.update_import_job(
                job_id,
                phase="rebuilding_index",
                message="正在写入本地 SQLite 索引…",
            )
            with self._durable_operations.operation():
                try:
                    self._index_runtime.replace_source(
                        extracted,
                        source_file_id,
                        backup_existing=backup_existing,
                    )
                except RuntimeError as exc:
                    if not str(exc).startswith("写入后未找到文献记录："):
                        raise
                    raise MinerUError(
                        f"{display_name} 未能进入索引：写入后未找到文献记录。"
                    ) from exc

    def fail_import_at_index(
        self,
        job_id: str,
        exc: Exception,
        *,
        parsed: bool = False,
    ) -> None:
        prefix = "文件已解析，但" if parsed else ""
        detail = str(exc).strip() or type(exc).__name__
        self.update_import_job(
            job_id,
            status="failed",
            phase="index_failed",
            failure_stage="index",
            can_resume=True,
            vision_failed=False,
            mineru_failed=False,
            mineru_interrupted=False,
            can_retry_with_provider=False,
            retry_provider_id=None,
            retry_provider_name=None,
            needs_provider_config=False,
            message=(
                f"{prefix}索引更新失败：{detail}。"
                "可点击“重新建立索引”重试，"
                "不会重新上传或调用解析 API。"
            ),
        )

    def finalize_import_job(
        self,
        job_id: str,
        source_file_id: str,
        is_pdf: bool,
    ) -> None:
        metadata_note = ""
        if is_pdf:
            self.update_import_job(
                job_id,
                phase="metadata_recognition",
                message="索引已建立，正在自动识别书目信息…",
            )
            try:
                metadata = self._detect_metadata(source_file_id)
                metadata = self._persist_metadata(source_file_id, metadata)
                missing = list(
                    metadata.get("metadata_missing_fields")
                    or metadata_missing_fields(metadata)
                )
                labels = {
                    "author": "作者",
                    "title": "书名",
                    "translator": "译者",
                    "publisher": "出版社",
                    "publish_place": "出版地",
                    "publish_year": "出版年份",
                    "journal_name": "出版刊物",
                    "volume": "卷次",
                    "issue": "期号",
                    "page_range": "页码",
                }
                if metadata.get("document_type") == "thesis":
                    labels.update(
                        {"title": "篇名", "publisher": "学校", "publish_year": "年份"}
                    )
                missing_labels = [labels[field] for field in missing if field in labels]
                metadata_note = "；书目信息已自动填入"
                if missing_labels:
                    metadata_note += "，缺少" + "、".join(missing_labels)
                self.update_import_job(
                    job_id,
                    bibliographic_metadata=metadata,
                    bibliographic_missing_fields=missing,
                )
            except Exception as exc:
                metadata_note = "；书目信息自动识别未完成，可在文献库中重试"
                self.update_import_job(job_id, bibliographic_error=str(exc))
        self.ensure_import_not_cancelled(job_id)
        self.update_import_job(
            job_id,
            status="completed",
            phase="completed",
            message="导入完成，已自动更新索引" + metadata_note,
        )

    def prepare_import_job(
        self,
        job_id: str,
        target: Path,
        source_file_id: str,
        profile: Dict[str, object],
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> bool:
        return self._parser_executor.execute(
            job_id,
            target,
            source_file_id,
            profile,
            is_pdf,
            force_mineru,
            vision_provider_id,
            jobs=self,
        )

    def run_import_job(
        self,
        job_id: str,
        target: Path,
        source_file_id: str,
        profile: Dict[str, object],
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> None:
        try:
            self.ensure_import_not_cancelled(job_id)
            prepared = self.prepare_import_job(
                job_id,
                target,
                source_file_id,
                profile,
                is_pdf,
                force_mineru,
                vision_provider_id,
            )
            self.ensure_import_not_cancelled(job_id)
            if not prepared:
                return
            if is_pdf:
                self.index_registered_pdf(job_id, source_file_id)
            else:
                self.rebuild_runtime_index(job_id)
            self.ensure_import_not_cancelled(job_id)
            self.finalize_import_job(job_id, source_file_id, is_pdf)
            self.ensure_import_not_cancelled(job_id)
        except ImportJobCancelled:
            self.finish_cancelled_import_job(job_id)
        except Exception as exc:
            try:
                self.fail_import_at_index(
                    job_id,
                    exc,
                    parsed=bool(
                        is_pdf
                        and (
                            force_mineru
                            or vision_provider_id
                            or str(profile.get("detected_pdf_type"))
                            != "native_text"
                        )
                    ),
                )
            except ImportJobCancelled:
                self.finish_cancelled_import_job(job_id)

    def _build_import_job(
        self,
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
        display_file_name: Optional[str] = None,
    ) -> Tuple[Job, JobContext]:
        job_id = f"import-{uuid.uuid4().hex[:12]}"
        parse_route = None
        provider_name = None
        if vision_provider_id:
            try:
                summary = vision_config_summary(resolve_vision_config_path(self._root))
                provider = next(
                    (
                        item
                        for item in summary.get("providers", [])
                        if isinstance(item, dict)
                        and str(item.get("id")) == str(vision_provider_id)
                    ),
                    None,
                )
                provider_name = provider.get("name") if provider else None
            except VisionAPIError:
                provider_name = None
        if is_pdf:
            parse_route = (
                "vision"
                if vision_provider_id
                else "mineru"
                if force_mineru
                else "local_ocr"
                if (
                    str(profile.get("detected_pdf_type")) != "native_text"
                    and local_ocr_available(self._root)
                )
                else "mineru"
                if str(profile.get("detected_pdf_type")) != "native_text"
                else "native"
            )
        job: Job = {
            "job_id": job_id,
            "status": "processing",
            "can_resume": False,
            "phase": "stored",
            "message": "文件已保存，准备处理…",
            "file_name": (
                Path(str(display_file_name)).name
                if display_file_name
                else target.name
            ),
            "size_bytes": target.stat().st_size,
            "source_file_id": source_file_id,
            "detected_pdf_type": (
                profile.get("detected_pdf_type") if is_pdf else None
            ),
            "parse_route": parse_route,
            "force_mineru": bool(force_mineru),
            "provider_id": vision_provider_id,
            "provider_name": provider_name,
            "vision_failed": False,
        }
        context: JobContext = {
            "target": Path(target),
            "source_file_id": source_file_id,
            "profile": dict(profile),
            "is_pdf": is_pdf,
            "force_mineru": bool(force_mineru),
            "vision_provider_id": vision_provider_id,
        }
        return job, context

    def create_import_job(
        self,
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
        consume_reservation: bool = False,
        display_file_name: Optional[str] = None,
    ) -> str:
        job, context = self._build_import_job(
            target,
            profile,
            source_file_id,
            is_pdf,
            force_mineru=force_mineru,
            vision_provider_id=vision_provider_id,
            display_file_name=display_file_name,
        )
        self._job_lifecycle.add_job(
            job,
            context,
            target=target,
            source_file_id=source_file_id,
            profile=profile,
            is_pdf=is_pdf,
            force_mineru=force_mineru,
            provider_id=vision_provider_id,
            total_pages=int(profile.get("pdf_page_count") or 0),
            consume_reservation=consume_reservation,
        )
        return str(job["job_id"])

    def register_pdf_for_import(
        self,
        target: Path,
        *,
        original_file_name: Optional[str] = None,
    ) -> Tuple[Dict[str, object], str, Path]:
        predicted_source_id = f"pdf-import-{sha256_file(target)[:16]}"
        with self._index_runtime.mutation(), import_config_lock():
            with self._job_store.atomic():
                self._job_store.reserve_source(predicted_source_id)
                try:
                    document = register_pdf(
                        self._root,
                        target,
                        original_file_name=original_file_name,
                    )
                    source_file_id = str(document["source_file_id"])
                    if source_file_id != predicted_source_id:
                        self._job_store.replace_reservation(
                            predicted_source_id,
                            source_file_id,
                        )
                except Exception:
                    self._job_store.release_reservation(predicted_source_id)
                    raise
        target = reuse_registered_pdf_copy(self._root, target, document)
        return document, source_file_id, target

    def release_import_reservation(self, source_file_id: str) -> None:
        self._job_store.release_reservation(source_file_id)

    def release_item_reservations(self, items: Sequence[Mapping[str, object]]) -> None:
        self._job_store.release_reservations(
            [
                str(item.get("source_file_id") or "")
                for item in items
                if item.get("source_reserved")
            ]
        )

    def cleanup_unreferenced_import_target(
        self,
        candidate: Optional[Path],
    ) -> bool:
        if candidate is None:
            return False
        target = Path(candidate)
        try:
            resolved = target.resolve()
            allowed_roots = (
                (self._root / "corpus" / "raw_pdf").resolve(),
                (self._root / "corpus" / "raw_docx").resolve(),
            )
            if not any(parent in resolved.parents for parent in allowed_roots):
                return False
        except OSError:
            return False
        if resolved.suffix.lower() == ".pdf":
            config_path = self._root / "config" / "pdf_imports.json"
            with locked_import_config(config_path) as config:
                with self._job_store.atomic():
                    if self._job_store.target_is_referenced(resolved):
                        return False
                    for document in config.get("documents", []):
                        if not isinstance(document, dict):
                            continue
                        configured_name = str(document.get("file_name") or "").strip()
                        if not configured_name:
                            continue
                        configured_path = Path(configured_name)
                        if not configured_path.is_absolute():
                            configured_path = (
                                self._root
                                / "corpus"
                                / "raw_pdf"
                                / configured_path
                            )
                        if configured_path.resolve() == resolved:
                            return False
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        return False
            return True
        with self._job_store.atomic():
            if self._job_store.target_is_referenced(resolved):
                return False
            try:
                target.unlink(missing_ok=True)
            except OSError:
                return False
        return True

    def queue_import_job(
        self,
        job_id: str,
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
    ) -> None:
        self._task_queue.submit(
            self.run_import_job,
            job_id,
            target,
            source_file_id,
            profile,
            is_pdf,
            force_mineru,
            vision_provider_id,
        )

    def fail_import_at_queue(self, job_id: str) -> None:
        self.update_import_job(
            job_id,
            status="failed",
            phase="queue_failed",
            failure_stage="queue",
            can_resume=True,
            vision_failed=False,
            mineru_failed=False,
            mineru_interrupted=False,
            can_retry_with_provider=False,
            retry_provider_id=None,
            retry_provider_name=None,
            needs_provider_config=False,
            message=(
                "导入任务暂时无法进入处理队列。"
                "文件和任务进度已安全保留，可点击“继续导入”重试。"
            ),
        )

    def start_import_job(
        self,
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
        consume_reservation: bool = False,
        display_file_name: Optional[str] = None,
    ) -> str:
        job_id = self.create_import_job(
            target,
            profile,
            source_file_id,
            is_pdf,
            force_mineru=force_mineru,
            vision_provider_id=vision_provider_id,
            consume_reservation=consume_reservation,
            display_file_name=display_file_name,
        )
        try:
            self.queue_import_job(
                job_id,
                target,
                profile,
                source_file_id,
                is_pdf,
                force_mineru=force_mineru,
                vision_provider_id=vision_provider_id,
            )
        except (ImportQueueFullError, ImportQueueClosedError):
            self.fail_import_at_queue(job_id)
        return job_id

    def start_retry_import_job(
        self,
        previous_job_id: str,
        target: Path,
        profile: Dict[str, object],
        source_file_id: str,
        is_pdf: bool,
        *,
        previous_statuses: Sequence[str],
        force_mineru: bool = False,
        vision_provider_id: Optional[str] = None,
        display_file_name: Optional[str] = None,
    ) -> str:
        """Replace one durable retry source, then enqueue its successor."""

        job, context = self._build_import_job(
            target,
            profile,
            source_file_id,
            is_pdf,
            force_mineru=force_mineru,
            vision_provider_id=vision_provider_id,
            display_file_name=display_file_name,
        )
        job_id = str(job["job_id"])
        self._job_lifecycle.replace_job_for_retry(
            previous_job_id,
            job,
            context,
            previous_statuses=previous_statuses,
            target=target,
            source_file_id=source_file_id,
            profile=profile,
            is_pdf=is_pdf,
            force_mineru=force_mineru,
            provider_id=vision_provider_id,
            total_pages=int(profile.get("pdf_page_count") or 0),
        )
        try:
            self.queue_import_job(
                job_id,
                target,
                profile,
                source_file_id,
                is_pdf,
                force_mineru=force_mineru,
                vision_provider_id=vision_provider_id,
            )
        except (ImportQueueFullError, ImportQueueClosedError):
            self.fail_import_at_queue(job_id)
        return job_id

    @staticmethod
    def is_provider_retry_eligible(
        job: Mapping[str, object],
        context: Optional[Mapping[str, object]] = None,
    ) -> bool:
        return bool(
            str(job.get("status") or "") == "failed"
            and str(job.get("failure_stage") or "") != "index"
            and (context is None or bool(context.get("is_pdf")))
            and (
                job.get("mineru_failed")
                or job.get("vision_failed")
                or (
                    str(job.get("parse_route") or "") == "vision"
                    and str(job.get("phase") or "") == "failed"
                )
                or job.get("needs_provider_config")
                or job.get("can_retry_with_provider")
                or job.get("mineru_interrupted")
            )
        )

    def public_import_job(self, job: Mapping[str, object]) -> Job:
        public_job = dict(job)
        local_retry_candidate = bool(
            str(public_job.get("status") or "") == "failed"
            and str(public_job.get("failure_stage") or "") != "index"
            and str(public_job.get("parse_route") or "") == "mineru"
            and str(public_job.get("provider_id") or "")
            != MINERU_LOCAL_PROVIDER_ID
            and (
                public_job.get("mineru_failed")
                or public_job.get("mineru_interrupted")
            )
        )
        local_enabled = False
        if local_retry_candidate:
            try:
                local_enabled = bool(
                    mineru_local_config_summary(
                        resolve_mineru_config_path(self._root)
                    ).get("enabled")
                )
            except (MinerUError, OSError, ValueError):
                local_enabled = False
        public_job["can_retry_with_local_mineru"] = bool(
            local_retry_candidate and local_enabled
        )
        is_legacy_vision_failure = bool(
            str(public_job.get("status") or "") == "failed"
            and str(public_job.get("parse_route") or "") == "vision"
            and str(public_job.get("phase") or "") == "failed"
            and str(public_job.get("failure_stage") or "") != "index"
        )
        if is_legacy_vision_failure:
            public_job["vision_failed"] = True
        is_parser_failure = self.is_provider_retry_eligible(public_job)
        if not is_parser_failure:
            if str(public_job.get("status") or "") == "failed" and (
                str(public_job.get("failure_stage") or "") == "index"
                or public_job.get("mineru_interrupted")
            ):
                public_job.update(
                    can_retry_with_provider=False,
                    retry_provider_id=None,
                    retry_provider_name=None,
                    needs_provider_config=False,
                )
            return public_job
        try:
            summary = vision_config_summary(resolve_vision_config_path(self._root))
        except (OSError, ValueError, VisionAPIError):
            summary = {"providers": []}
        providers = [
            item
            for item in summary.get("providers", [])
            if isinstance(item, dict)
            and item.get("enabled")
            and item.get("configured")
        ]
        preferred_provider_id = str(public_job.get("retry_provider_id") or "")
        provider = next(
            (
                item
                for item in providers
                if str(item.get("id") or "") == preferred_provider_id
            ),
            providers[0] if providers else None,
        )
        if public_job.get("vision_failed"):
            current_provider_id = str(public_job.get("provider_id") or "")
            alternate_provider = next(
                (
                    item
                    for item in providers
                    if str(item.get("id") or "") != current_provider_id
                ),
                None,
            )
            if alternate_provider and (
                provider is None
                or str(provider.get("id") or "") == current_provider_id
            ):
                provider = alternate_provider
        public_job.update(
            can_retry_with_provider=bool(provider),
            retry_provider_id=provider.get("id") if provider else None,
            retry_provider_name=provider.get("name") if provider else None,
            needs_provider_config=not bool(provider),
        )
        return public_job

    def job_status(self, job_id: str) -> Optional[Job]:
        snapshot = self._job_store.job_snapshot(job_id)
        return self.public_import_job(snapshot) if snapshot is not None else None

    def job_and_context(
        self,
        job_id: str,
    ) -> Optional[Tuple[Job, JobContext]]:
        """Return retry inputs as copies, never the synchronized state itself."""

        return self._job_store.job_and_context_snapshot(job_id)

    def job_for_source(
        self,
        source_file_id: str,
        *,
        statuses: Sequence[str],
    ) -> Optional[Job]:
        return self._job_store.job_for_source(
            source_file_id,
            statuses=statuses,
        )

    def active_job_for_source(self, source_file_id: str) -> Optional[Job]:
        return self.job_for_source(
            source_file_id,
            statuses=("processing", "cancelling"),
        )

    def processing_job_with_prefix(self, job_id_prefix: str) -> Optional[Job]:
        return self._job_store.processing_job_with_prefix(job_id_prefix)

    def active_source_ids(self) -> set[str]:
        return self._job_store.active_source_ids()

    def has_active_jobs(self) -> bool:
        return self._job_store.has_active_jobs()

    def resumable_import_jobs(self) -> List[Job]:
        result: List[Job] = []
        for job, context in self._job_store.resumable_snapshots():
            public_job = self.public_import_job(job)
            public_job["file_type"] = "pdf" if context.get("is_pdf") else "docx"
            result.append(public_job)
        return sorted(
            result,
            key=lambda item: str(item.get("last_updated") or ""),
            reverse=True,
        )

    def retry_index_job(
        self,
        job_id: str,
        target: Path,
        source_file_id: str,
        is_pdf: bool,
    ) -> None:
        try:
            if is_pdf:
                self.index_registered_pdf(job_id, source_file_id)
            else:
                self.rebuild_runtime_index(job_id)
            self.finalize_import_job(job_id, source_file_id, is_pdf)
        except ImportJobCancelled:
            self.finish_cancelled_import_job(job_id)
        except Exception as exc:
            try:
                self.fail_import_at_index(job_id, exc, parsed=True)
            except ImportJobCancelled:
                self.finish_cancelled_import_job(job_id)

    def resume_import_job(self, job_id: str) -> Job:
        transition = self._job_lifecycle.begin_resume(
            job_id,
            infer_failure_stage=self.infer_import_failure_stage,
            validate_target=self.validated_import_target,
        )
        try:
            if transition.retry_index_only:
                self._task_queue.submit(
                    self.retry_index_job,
                    job_id,
                    transition.target,
                    str(transition.context.get("source_file_id") or ""),
                    bool(transition.context.get("is_pdf")),
                )
            else:
                self.queue_import_job(
                    job_id,
                    transition.target,
                    dict(transition.context.get("profile") or {}),
                    str(transition.context.get("source_file_id") or ""),
                    bool(transition.context.get("is_pdf")),
                    force_mineru=bool(transition.context.get("force_mineru")),
                    vision_provider_id=(
                        str(transition.context["vision_provider_id"])
                        if transition.context.get("vision_provider_id")
                        else None
                    ),
                )
        except (ImportQueueFullError, ImportQueueClosedError) as exc:
            self.fail_import_at_queue(job_id)
            raise MinerUError(
                "导入任务暂时无法进入处理队列，已保留为可继续任务。"
            ) from exc
        return transition.job

    def dismiss_import_job(self, job_id: str) -> str:
        return self._job_lifecycle.dismiss_job(job_id)

    def begin_source_deletion(self, source_file_id: str) -> None:
        """Atomically reject imports while a document deletion is in flight."""

        self._job_store.begin_source_deletion(source_file_id)

    def end_source_deletion(self, source_file_id: str) -> None:
        self._job_store.end_source_deletion(source_file_id)

    def purge_source_jobs(self, source_file_ids: Sequence[str]) -> List[str]:
        """Remove stale jobs after deletion and return visible cleanup warnings."""

        return self._job_lifecycle.purge_source_jobs(source_file_ids)

    def _rollback_unqueued_batch(self, queued_items: Sequence[Job]) -> None:
        self._job_lifecycle.rollback_unqueued_batch(queued_items)

    def start_native_import_batch(
        self,
        items: List[Job],
    ) -> List[str]:
        queued_items: List[Job] = []
        try:
            for item in items:
                try:
                    job_id = self.create_import_job(
                        Path(item["target"]),
                        dict(item["profile"]),
                        str(item["source_file_id"]),
                        bool(item["is_pdf"]),
                        consume_reservation=bool(item.get("source_reserved")),
                        display_file_name=(
                            str(item["display_file_name"])
                            if item.get("display_file_name")
                            else None
                        ),
                    )
                finally:
                    if item.get("source_reserved"):
                        self.release_import_reservation(str(item["source_file_id"]))
                queued_items.append({**item, "job_id": job_id})
        except Exception:
            self.release_item_reservations(items)
            self._rollback_unqueued_batch(queued_items)
            raise

        if not queued_items:
            return []
        self._batch_executor.submit_native(queued_items, jobs=self)
        return [str(item["job_id"]) for item in queued_items]

    def start_remote_import_batch(
        self,
        items: List[Job],
    ) -> List[str]:
        queued_items: List[Job] = []
        try:
            for item in items:
                vision_provider_id = (
                    str(item["vision_provider_id"])
                    if item.get("vision_provider_id")
                    else None
                )
                try:
                    job_id = self.create_import_job(
                        Path(item["target"]),
                        dict(item["profile"]),
                        str(item["source_file_id"]),
                        bool(item["is_pdf"]),
                        force_mineru=bool(item["force_mineru"]),
                        vision_provider_id=vision_provider_id,
                        consume_reservation=bool(item.get("source_reserved")),
                        display_file_name=(
                            str(item["display_file_name"])
                            if item.get("display_file_name")
                            else None
                        ),
                    )
                finally:
                    if item.get("source_reserved"):
                        self.release_import_reservation(str(item["source_file_id"]))
                queued_items.append(
                    {
                        **item,
                        "job_id": job_id,
                        "vision_provider_id": vision_provider_id,
                    }
                )
        except Exception:
            self.release_item_reservations(items)
            self._rollback_unqueued_batch(queued_items)
            raise

        if not queued_items:
            return []
        self._batch_executor.submit_remote(queued_items, jobs=self)
        return [str(item["job_id"]) for item in queued_items]
