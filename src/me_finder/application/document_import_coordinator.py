"""Coordinate document ingress before the durable import workflow."""

from __future__ import annotations

import logging
import uuid
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import (
    BinaryIO,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from ..app_context import AppPaths
from ..chunked_upload import ChunkedUploadStore
from ..document_export import DocumentExportError, extract_embedded_source_pdf
from ..document_package_import import (
    DocumentPackageImportError,
    build_document_package_records,
    read_document_package,
)
from ..import_resume import sha256_file
from ..import_queue import ImportQueueClosedError, ImportQueueFullError
from ..mineru_api import MinerUError, resolve_mineru_config_path
from ..mineru_local_settings import mineru_local_config_summary
from ..local_ocr_settings import local_ocr_available
from ..pdf_import_service import (
    cleanup_stale_document_storage_files,
    copy_local_document,
    detect_imported_pdf,
    document_storage_error,
    document_storage_target,
    load_import_config,
    locked_import_config,
    release_document_storage_target,
    save_import_config,
)
from ..vision_api import VisionAPIError


class ImportJobsPort(Protocol):
    def register_background_job(self, job: Mapping[str, object]) -> None:
        ...

    def submit_background_task(
        self, task: Callable[..., None], *args: object
    ) -> None:
        ...

    def update_import_job(self, job_id: str, **updates: object) -> None:
        ...

    def replace_imported_source(
        self,
        job_id: str,
        extracted: Mapping[str, object],
        source_file_id: str,
    ) -> None:
        ...

    def register_pdf_for_import(
        self,
        target: Path,
        *,
        original_file_name: Optional[str] = None,
    ) -> Tuple[Dict[str, object], str, Path]:
        ...

    def release_import_reservation(self, source_file_id: str) -> None:
        ...

    def release_item_reservations(
        self,
        items: Sequence[Mapping[str, object]],
    ) -> None:
        ...

    def cleanup_unreferenced_import_target(
        self,
        candidate: Optional[Path],
    ) -> bool:
        ...

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
        ...

    def job_for_source(
        self,
        source_file_id: str,
        *,
        statuses: Sequence[str],
    ) -> Optional[Dict[str, object]]:
        ...

    def start_native_import_batch(
        self,
        items: List[Dict[str, object]],
    ) -> List[str]:
        ...

    def start_remote_import_batch(
        self,
        items: List[Dict[str, object]],
    ) -> List[str]:
        ...


Admission = Callable[[], AbstractContextManager[None]]
PDFDetector = Callable[[Path], Dict[str, object]]
LocalDocumentCopier = Callable[[Path, Path], Path]
FileHasher = Callable[[Path], str]


class DocumentImportCoordinator:
    """Own upload storage and hand completed documents to import jobs."""

    def __init__(
        self,
        paths: AppPaths,
        jobs: ImportJobsPort,
        *,
        chunked_uploads: Optional[ChunkedUploadStore] = None,
        admission: Admission = nullcontext,
        detect_pdf: PDFDetector = detect_imported_pdf,
        copy_local: LocalDocumentCopier = copy_local_document,
        hash_file: FileHasher = sha256_file,
    ) -> None:
        self._paths = paths
        self._jobs = jobs
        self._chunked_uploads = (
            chunked_uploads
            if chunked_uploads is not None
            else ChunkedUploadStore(
                paths.corpus_root / ".upload-staging"
            )
        )
        self._admission = admission
        self._detect_pdf = detect_pdf
        self._copy_local = copy_local
        self._hash_file = hash_file

    def import_stream(
        self,
        filename: str,
        length: int,
        reader: BinaryIO,
        *,
        pdf_parse_mode: object = "auto",
        vision_provider_id: object = "",
    ) -> Dict[str, object]:
        with self._admission():
            suffix = Path(filename).suffix.lower()
            if suffix not in {".pdf", ".docx"}:
                raise MinerUError("只支持 PDF 或 DOCX 文件。")
            mode, provider_id = self._validated_parse_options(
                pdf_parse_mode,
                vision_provider_id,
            )
            logging.info(
                "legacy import request received file=%r size=%d",
                Path(filename).name,
                length,
            )
            target = self._store_stream(
                filename,
                length,
                suffix == ".pdf",
                reader,
            )
            logging.info(
                "legacy import upload completed file=%r size=%d",
                Path(filename).name,
                length,
            )
            return self._start_stored_import(
                target,
                filename=filename,
                is_pdf=suffix == ".pdf",
                pdf_parse_mode=mode,
                vision_provider_id=provider_id or None,
            )

    def start_chunked(
        self,
        filename: str,
        total_size: int,
        *,
        pdf_parse_mode: object = "auto",
        vision_provider_id: object = "",
        import_kind: object = "document",
    ) -> Dict[str, object]:
        with self._admission():
            suffix = Path(filename).suffix.lower()
            kind = str(import_kind or "document").strip().lower()
            allowed = {
                "document": {".pdf", ".docx"},
                "document_package": {".zip"},
            }
            if kind not in allowed or suffix not in allowed[kind]:
                raise MinerUError("导入文件类型与导入方式不匹配。")
            if kind == "document_package" and not filename.lower().endswith(
                ".mefinder.zip"
            ):
                raise MinerUError("文档包文件名必须以 .mefinder.zip 结尾。")
            if kind == "document":
                mode, provider_id = self._validated_parse_options(
                    pdf_parse_mode,
                    vision_provider_id,
                )
            else:
                mode, provider_id = "provided", ""
            result = self._chunked_uploads.start(
                filename,
                total_size,
                metadata={
                    "is_pdf": "1" if suffix == ".pdf" else "0",
                    "parse_mode": mode,
                    "provider_id": provider_id,
                    "import_kind": kind,
                },
            )
            result.update({"file_name": Path(filename).name})
            logging.info(
                "chunked import session started upload_id=%s file=%r size=%d",
                result["upload_id"],
                Path(filename).name,
                total_size,
            )
            return {"ok": True, **result}

    def append_chunk(
        self,
        upload_id: str,
        offset: int,
        length: int,
        reader: BinaryIO,
    ) -> Dict[str, object]:
        with self._admission():
            if offset == 0:
                logging.info(
                    "chunked import first chunk request upload_id=%s size=%d",
                    upload_id,
                    length,
                )
            progress = self._chunked_uploads.append(
                upload_id,
                offset,
                length,
                reader,
            )
            if progress["first_chunk"]:
                logging.info(
                    "chunked import first chunk stored upload_id=%s received=%d",
                    upload_id,
                    progress["received_size"],
                )
            if progress["complete"]:
                logging.info(
                    "chunked import upload completed upload_id=%s size=%d",
                    upload_id,
                    progress["received_size"],
                )
            else:
                logging.debug(
                    "chunked import progress upload_id=%s received=%d total=%d",
                    upload_id,
                    progress["received_size"],
                    progress["total_size"],
                )
            return {"ok": True, **progress}

    def cancel_chunked(self, upload_id: str) -> Dict[str, object]:
        with self._admission():
            return {
                "ok": True,
                "cancelled": self._chunked_uploads.cancel(upload_id),
            }

    def finish_chunked(self, upload_id: str) -> Dict[str, object]:
        with self._admission():
            completed = self._chunked_uploads.finish(upload_id)
            staged_path: Optional[Path] = completed.temp_path
            try:
                metadata = dict(completed.metadata)
                import_kind = str(metadata.get("import_kind") or "document")
                if import_kind == "document_package":
                    artifact_path = self._store_artifact_completed(
                        completed.filename,
                        completed.total_size,
                        completed.temp_path,
                    )
                    staged_path = None
                    try:
                        return self._start_document_package_import(
                            artifact_path,
                            display_file_name=completed.filename,
                        )
                    except Exception:
                        artifact_path.unlink(missing_ok=True)
                        raise
                is_pdf = metadata.get("is_pdf") == "1"
                mode, provider_id = self._validated_parse_options(
                    metadata.get("parse_mode", "auto"),
                    metadata.get("provider_id", ""),
                )
                logging.info(
                    "chunked import finalization started upload_id=%s file=%r size=%d",
                    completed.upload_id,
                    completed.filename,
                    completed.total_size,
                )
                target = self._store_completed(
                    completed.filename,
                    completed.total_size,
                    is_pdf,
                    completed.temp_path,
                )
                staged_path = None
                return self._start_stored_import(
                    target,
                    filename=completed.filename,
                    is_pdf=is_pdf,
                    pdf_parse_mode=mode,
                    vision_provider_id=provider_id,
                    upload_id=completed.upload_id,
                )
            finally:
                if staged_path is not None:
                    try:
                        staged_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _store_artifact_completed(
        self,
        filename: str,
        length: int,
        staged_path: Path,
    ) -> Path:
        if length <= 0 or staged_path.stat().st_size != length:
            raise MinerUError("上传文件大小校验失败。")
        safe_name = Path(filename).name
        if not safe_name or safe_name in {".", ".."}:
            raise MinerUError("无法识别文件名。")
        directory = self._paths.corpus_root / "parsed" / "imports"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"import-{uuid.uuid4().hex[:12]}-{safe_name}"
        try:
            staged_path.replace(target)
        except OSError as exc:
            raise document_storage_error(safe_name, exc) from exc
        return target

    def _start_document_package_import(
        self,
        artifact_path: Path,
        *,
        display_file_name: str,
    ) -> Dict[str, object]:
        job_id = f"artifact-import-{uuid.uuid4().hex[:12]}"
        self._jobs.register_background_job(
            {
                "job_id": job_id,
                "status": "processing",
                "phase": "validating_result",
                "message": "正在校验 MEFinder 文档包…",
                "file_name": Path(display_file_name).name,
                "file_type": "document_package",
                "size_bytes": artifact_path.stat().st_size,
                "parse_route": "provided",
                "can_resume": False,
            }
        )
        try:
            self._jobs.submit_background_task(
                self._run_document_package_import,
                job_id,
                artifact_path,
            )
        except (ImportQueueFullError, ImportQueueClosedError) as exc:
            self._jobs.update_import_job(
                job_id,
                status="failed",
                phase="queue_failed",
                message="文档包导入任务未能进入队列。",
            )
            artifact_path.unlink(missing_ok=True)
            raise MinerUError("导入任务暂时无法启动，请稍后重试。") from exc
        return {
            "ok": True,
            "job_id": job_id,
            "file_name": Path(display_file_name).name,
            "file_type": "document_package",
            "parse_route": "provided",
        }

    def _run_document_package_import(
        self,
        job_id: str,
        artifact_path: Path,
    ) -> None:
        reserved_source_id = ""
        imported_pdf: Optional[Path] = None
        embedded_pdf = artifact_path.with_name(
            f".embedded-{uuid.uuid4().hex[:12]}.pdf"
        )
        completed = False
        try:
            package = read_document_package(artifact_path)
            self._jobs.update_import_job(
                job_id,
                phase="text_parsing",
                message="格式校验通过，正在恢复页级文本、书目和页码…",
            )
            extracted_pdf = extract_embedded_source_pdf(
                artifact_path,
                embedded_pdf,
            )
            if extracted_pdf is not None:
                profile = self._detect_pdf(extracted_pdf)
                actual_page_count = int(profile.get("pdf_page_count") or 0)
                package_last_page = max(
                    int(page["physical_pdf_page"]) for page in package.pages
                )
                if actual_page_count and package_last_page > actual_page_count:
                    raise DocumentPackageImportError(
                        "文档包的物理页码超出了包内原 PDF 的总页数。"
                    )

            source_path: Optional[Path] = None
            if extracted_pdf is not None:
                imported_pdf = self._copy_local(
                    self._paths.runtime_root,
                    extracted_pdf,
                )
                document, source_file_id, source_path = (
                    self._jobs.register_pdf_for_import(
                        imported_pdf,
                        original_file_name=package.source_file_name,
                    )
                )
                reserved_source_id = source_file_id
                document_id = str(document.get("document_id") or source_file_id)
                original_file_name = str(
                    document.get("original_file_name") or source_path.name
                )
            else:
                source_file_id = f"pdf-import-{package.source_sha256[:16]}"
                document_id = source_file_id.upper().replace("-", "_")
                source_path = self._configured_pdf_path(
                    source_file_id,
                    expected_sha256=package.source_sha256,
                )
                original_file_name = package.source_file_name

            extracted, mapping_segments = build_document_package_records(
                package,
                package_path=artifact_path,
                source_file_id=source_file_id,
                document_id=document_id,
                runtime_root=self._paths.runtime_root,
                source_path=source_path,
            )
            source = extracted["source_files"][0]
            if source.get("source_type") == "pdf":
                self._persist_imported_pdf_config(
                    source_file_id=source_file_id,
                    document_id=document_id,
                    file_name=(
                        source_path.name
                        if source_path is not None
                        else original_file_name
                    ),
                    title=package.title,
                    metadata=package.bibliographic_metadata,
                    mapping_segments=mapping_segments,
                )
            self._jobs.update_import_job(
                job_id,
                source_file_id=source_file_id,
                page_count=len(package.pages),
            )
            self._jobs.replace_imported_source(
                job_id,
                extracted,
                source_file_id,
            )
            self._jobs.update_import_job(
                job_id,
                status="completed",
                phase="completed",
                source_file_id=source_file_id,
                message=(
                    f"已恢复 {len(package.pages)} 页文档数据"
                    + ("和原 PDF" if extracted_pdf is not None else "")
                    + "，未运行 OCR。"
                ),
            )
            completed = True
        except (
            DocumentPackageImportError,
            DocumentExportError,
            MinerUError,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            self._jobs.update_import_job(
                job_id,
                status="failed",
                phase="failed",
                failure_stage=(
                    "validation"
                    if isinstance(exc, (DocumentPackageImportError, DocumentExportError))
                    else "index"
                ),
                can_resume=False,
                message=f"导入 MEFinder 文档包失败：{exc}",
            )
        except Exception as exc:
            logging.exception("document package import failed")
            self._jobs.update_import_job(
                job_id,
                status="failed",
                phase="failed",
                failure_stage="index",
                can_resume=False,
                message=f"导入 MEFinder 文档包失败：{type(exc).__name__}: {exc}",
            )
        finally:
            if reserved_source_id:
                self._jobs.release_import_reservation(reserved_source_id)
            embedded_pdf.unlink(missing_ok=True)
            artifact_path.unlink(missing_ok=True)
            if imported_pdf is not None and not completed:
                self._jobs.cleanup_unreferenced_import_target(imported_pdf)

    def _configured_pdf_path(
        self,
        source_file_id: str,
        *,
        expected_sha256: Optional[str],
    ) -> Optional[Path]:
        config = load_import_config(
            self._paths.config_root / "pdf_imports.json"
        )
        document = next(
            (
                item
                for item in config.get("documents", [])
                if isinstance(item, Mapping)
                and str(item.get("source_file_id") or "") == source_file_id
            ),
            None,
        )
        if document is None:
            return None
        raw_name = str(document.get("file_name") or "").strip()
        if not raw_name:
            return None
        candidate = Path(raw_name)
        if not candidate.is_absolute():
            candidate = self._paths.corpus_root / "raw_pdf" / candidate
        if not candidate.is_file():
            return None
        if expected_sha256 and self._hash_file(candidate) != expected_sha256:
            return None
        return candidate

    def _persist_imported_pdf_config(
        self,
        *,
        source_file_id: str,
        document_id: str,
        file_name: str,
        title: str,
        metadata: Mapping[str, object],
        mapping_segments: Sequence[Mapping[str, object]],
    ) -> None:
        config_path = self._paths.config_root / "pdf_imports.json"
        with locked_import_config(config_path) as config:
            documents = config["documents"]
            document = next(
                (
                    item
                    for item in documents
                    if isinstance(item, dict)
                    and str(item.get("source_file_id") or "") == source_file_id
                ),
                None,
            )
            if document is None:
                document = {
                    "enabled": True,
                    "source_file_id": source_file_id,
                    "document_id": document_id,
                    "file_name": Path(file_name).name,
                    "original_file_name": Path(file_name).name,
                }
                documents.append(document)
            document["enabled"] = True
            document["title"] = str(metadata.get("title") or title)
            for key, value in metadata.items():
                if value not in (None, ""):
                    document[key] = value
            if mapping_segments:
                document["page_mapping"] = {
                    "validated_by": "document_package",
                    "mapping_origin": "document_package",
                    "segments": [dict(item) for item in mapping_segments],
                }
            else:
                document.setdefault(
                    "page_mapping",
                    {"validated_by": None, "segments": []},
                )
            save_import_config(config_path, config)

    def import_local(
        self,
        raw_paths: Sequence[object],
        allowed_bases: Sequence[Path],
        *,
        pdf_parse_mode: object = "auto",
        vision_provider_id: object = "",
    ) -> Dict[str, object]:
        with self._admission():
            return self._import_local(
                raw_paths,
                allowed_bases,
                pdf_parse_mode=pdf_parse_mode,
                vision_provider_id=vision_provider_id,
            )

    def active_session_count(self) -> int:
        return self._chunked_uploads.active_session_count()

    def has_active_uploads(self) -> bool:
        return self.active_session_count() > 0

    def close(self) -> None:
        self._chunked_uploads.close()

    @staticmethod
    def validate_parse_options(
        pdf_parse_mode: object,
        vision_provider_id: object,
    ) -> Tuple[str, str]:
        mode = str(pdf_parse_mode or "auto").strip().lower()
        if mode not in {"auto", "mineru", "mineru-local", "vision"}:
            raise MinerUError("PDF 解析方式无效。")
        provider_id = (
            str(vision_provider_id or "").strip()
            if mode == "vision"
            else ""
        )
        if mode == "vision" and not provider_id:
            raise MinerUError("请选择一个其他解析 API。")
        return mode, provider_id

    def _validated_parse_options(
        self,
        pdf_parse_mode: object,
        vision_provider_id: object,
    ) -> Tuple[str, str]:
        mode, provider_id = self.validate_parse_options(
            pdf_parse_mode,
            vision_provider_id,
        )
        if mode == "mineru-local":
            summary = mineru_local_config_summary(
                resolve_mineru_config_path(self._paths.runtime_root)
            )
            if not summary.get("enabled"):
                raise MinerUError("请先在设置中启用 MinerU 本地部署。")
        return mode, provider_id

    def _upload_storage_details(
        self,
        filename: str,
        length: int,
        is_pdf: bool,
    ) -> Tuple[str, Path]:
        if length <= 0:
            raise MinerUError("文件为空。")
        safe_name = Path(filename).name
        if not safe_name or safe_name in {".", ".."}:
            raise MinerUError("无法识别文件名。")
        suffix = Path(safe_name).suffix.lower()
        expected = ".pdf" if is_pdf else ".docx"
        if suffix != expected:
            raise MinerUError(f"导入文件必须是 {expected}。")
        directory = self._paths.corpus_root / (
            "raw_pdf" if is_pdf else "raw_docx"
        )
        return safe_name, directory

    def _store_stream(
        self,
        filename: str,
        length: int,
        is_pdf: bool,
        reader: BinaryIO,
    ) -> Path:
        safe_name, directory = self._upload_storage_details(
            filename,
            length,
            is_pdf,
        )
        target: Optional[Path] = None
        temp_path: Optional[Path] = None
        remaining = length
        try:
            directory.mkdir(parents=True, exist_ok=True)
            cleanup_stale_document_storage_files(directory)
            target = document_storage_target(
                directory,
                safe_name,
                shorten_long_names=is_pdf,
            )
            temp_path = directory / f".mefinder-upload-{uuid.uuid4().hex}.tmp"
            with temp_path.open("wb") as stream:
                while remaining > 0:
                    chunk = reader.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise MinerUError("上传数据不完整。")
                    stream.write(chunk)
                    remaining -= len(chunk)
            temp_path.replace(target)
        except OSError as exc:
            raise document_storage_error(safe_name, exc) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if target is not None:
                release_document_storage_target(target)
        assert target is not None
        return target

    def _store_completed(
        self,
        filename: str,
        length: int,
        is_pdf: bool,
        staged_path: Path,
    ) -> Path:
        safe_name, directory = self._upload_storage_details(
            filename,
            length,
            is_pdf,
        )
        target: Optional[Path] = None
        try:
            if staged_path.stat().st_size != length:
                raise MinerUError("上传文件大小校验失败。")
            directory.mkdir(parents=True, exist_ok=True)
            cleanup_stale_document_storage_files(directory)
            target = document_storage_target(
                directory,
                safe_name,
                shorten_long_names=is_pdf,
            )
            staged_path.replace(target)
        except OSError as exc:
            raise document_storage_error(safe_name, exc) from exc
        finally:
            if target is not None:
                release_document_storage_target(target)
        assert target is not None
        return target

    def _start_stored_import(
        self,
        target: Path,
        *,
        filename: str,
        is_pdf: bool,
        pdf_parse_mode: str,
        vision_provider_id: Optional[str],
        upload_id: str = "legacy",
    ) -> Dict[str, object]:
        reserved_source_id = ""
        owned_target: Optional[Path] = target
        try:
            if is_pdf:
                logging.info(
                    "import type detection started upload_id=%s file=%r size=%d",
                    upload_id,
                    Path(filename).name,
                    target.stat().st_size,
                )
                profile = self._detect_pdf(target)
                logging.info(
                    "import type detection completed upload_id=%s detected_type=%s",
                    upload_id,
                    profile.get("detected_pdf_type"),
                )
                (
                    _document,
                    source_file_id,
                    target,
                ) = self._jobs.register_pdf_for_import(
                    target,
                    original_file_name=filename,
                )
                reserved_source_id = source_file_id
            else:
                profile = {"detected_pdf_type": "docx"}
                source_file_id = f"docx-import-{uuid.uuid4().hex[:16]}"
            use_local_mineru = is_pdf and pdf_parse_mode == "mineru-local"
            if use_local_mineru:
                profile["mineru_local"] = True
            force_mineru = is_pdf and pdf_parse_mode in {
                "mineru",
                "mineru-local",
            }
            job_id = self._jobs.start_import_job(
                target,
                profile,
                source_file_id,
                is_pdf,
                force_mineru=force_mineru,
                vision_provider_id=vision_provider_id or None,
                consume_reservation=bool(reserved_source_id),
                display_file_name=filename,
            )
            if reserved_source_id:
                self._jobs.release_import_reservation(reserved_source_id)
                reserved_source_id = ""
            parse_route = None
            if is_pdf:
                parse_route = (
                    "vision"
                    if vision_provider_id
                    else "mineru"
                    if force_mineru
                    else "local_ocr"
                    if (
                        str(profile.get("detected_pdf_type")) != "native_text"
                        and local_ocr_available(self._paths.runtime_root)
                    )
                    else "mineru"
                    if str(profile.get("detected_pdf_type")) != "native_text"
                    else "native"
                )
            logging.info(
                "import job queued upload_id=%s job_id=%s route=%s",
                upload_id,
                job_id,
                parse_route or "docx",
            )
            return {
                "ok": True,
                "job_id": job_id,
                "file_name": Path(filename).name,
                "source_file_id": source_file_id,
                "detected_pdf_type": (
                    profile.get("detected_pdf_type") if is_pdf else None
                ),
                "parse_route": parse_route,
                "provider_id": (
                    "mineru-local"
                    if use_local_mineru
                    else vision_provider_id or None
                ),
            }
        finally:
            if reserved_source_id:
                self._jobs.release_import_reservation(reserved_source_id)
            self._jobs.cleanup_unreferenced_import_target(owned_target)

    def _import_local(
        self,
        raw_paths: Sequence[object],
        allowed_bases: Sequence[Path],
        *,
        pdf_parse_mode: object,
        vision_provider_id: object,
    ) -> Dict[str, object]:
        mode, provider_id = self._validated_parse_options(
            pdf_parse_mode,
            vision_provider_id,
        )
        resolved_bases = [Path(item).resolve() for item in allowed_bases]
        prepared_items: List[Dict[str, object]] = []
        prepared_source_ids: set[str] = set()
        import_errors: List[Dict[str, object]] = []
        for raw in raw_paths:
            item_reserved_source_id = ""
            owned_target: Optional[Path] = None
            try:
                source_path = Path(str(raw)).resolve()
                if not any(
                    base == source_path or base in source_path.parents
                    for base in resolved_bases
                ):
                    raise MinerUError("不在已配置的文献目录内。")
                if not source_path.is_file():
                    raise MinerUError("文件不存在。")
                target = self._copy_local(
                    self._paths.runtime_root,
                    source_path,
                )
                owned_target = target
                is_pdf = target.suffix.lower() == ".pdf"
                if is_pdf:
                    profile = self._detect_pdf(target)
                    predicted_source_id = (
                        f"pdf-import-{self._hash_file(target)[:16]}"
                    )
                    if predicted_source_id in prepared_source_ids:
                        raise MinerUError("同一批次中已有内容相同的文献。")
                    (
                        _document,
                        source_file_id,
                        target,
                    ) = self._jobs.register_pdf_for_import(
                        target,
                        original_file_name=source_path.name,
                    )
                    item_reserved_source_id = source_file_id
                else:
                    profile = {"detected_pdf_type": "docx"}
                    source_file_id = f"docx-import-{uuid.uuid4().hex[:16]}"
                if source_file_id in prepared_source_ids:
                    raise MinerUError("同一批次中已有内容相同的文献。")
                if self._jobs.job_for_source(
                    source_file_id,
                    statuses=("processing",),
                ):
                    raise MinerUError("同一文献已有解析任务正在运行。")
                use_local_mineru = is_pdf and mode == "mineru-local"
                if use_local_mineru:
                    profile["mineru_local"] = True
                force_mineru = is_pdf and mode in {
                    "mineru",
                    "mineru-local",
                }
                parse_route = None
                if is_pdf:
                    parse_route = (
                        "vision"
                        if provider_id
                        else "mineru"
                        if force_mineru
                        else "local_ocr"
                        if (
                            str(profile.get("detected_pdf_type")) != "native_text"
                            and local_ocr_available(self._paths.runtime_root)
                        )
                        else "mineru"
                        if str(profile.get("detected_pdf_type")) != "native_text"
                        else "native"
                    )
                prepared_items.append(
                    {
                        "target": target,
                        "owned_target": owned_target,
                        "profile": profile,
                        "source_file_id": source_file_id,
                        "source_reserved": is_pdf,
                        "is_pdf": is_pdf,
                        "force_mineru": force_mineru,
                        "vision_provider_id": provider_id or None,
                        "parse_route": parse_route,
                        "display_file_name": source_path.name,
                        "response": {
                            "path": str(raw),
                            "file_name": source_path.name,
                            "size_bytes": target.stat().st_size,
                            "source_file_id": source_file_id,
                            "detected_pdf_type": (
                                profile.get("detected_pdf_type")
                                if is_pdf
                                else None
                            ),
                            "file_type": "pdf" if is_pdf else "docx",
                            "parse_route": parse_route,
                            "provider_id": (
                                "mineru-local"
                                if use_local_mineru
                                else provider_id or None
                            ),
                        },
                    }
                )
                prepared_source_ids.add(source_file_id)
                item_reserved_source_id = ""
            except (MinerUError, VisionAPIError, OSError, ValueError) as exc:
                if item_reserved_source_id:
                    self._jobs.release_import_reservation(
                        item_reserved_source_id
                    )
                self._jobs.cleanup_unreferenced_import_target(owned_target)
                import_errors.append({"path": str(raw), "error": str(exc)})
            except Exception:
                if item_reserved_source_id:
                    self._jobs.release_import_reservation(
                        item_reserved_source_id
                    )
                self._jobs.cleanup_unreferenced_import_target(owned_target)
                self._jobs.release_item_reservations(prepared_items)
                raise

        try:
            native_pdf_items = [
                item
                for item in prepared_items
                if item["is_pdf"] and item["parse_route"] == "native"
            ]
            word_items = [
                item for item in prepared_items if not item["is_pdf"]
            ]
            remote_items = [
                item
                for item in prepared_items
                if item["is_pdf"] and item["parse_route"] != "native"
            ]
            native_pdf_job_ids = self._jobs.start_native_import_batch(
                native_pdf_items
            )
            for item, job_id in zip(native_pdf_items, native_pdf_job_ids):
                item["response"]["job_id"] = job_id
            word_job_ids = self._jobs.start_native_import_batch(word_items)
            for item, job_id in zip(word_items, word_job_ids):
                item["response"]["job_id"] = job_id
            remote_job_ids = self._jobs.start_remote_import_batch(
                remote_items
            )
            for item, job_id in zip(remote_items, remote_job_ids):
                item["response"]["job_id"] = job_id
        finally:
            self._jobs.release_item_reservations(prepared_items)
            for item in prepared_items:
                self._jobs.cleanup_unreferenced_import_target(
                    Path(item["owned_target"])
                    if item.get("owned_target")
                    else None
                )
        jobs = [dict(item["response"]) for item in prepared_items]
        return {"ok": True, "jobs": jobs, "errors": import_errors}
