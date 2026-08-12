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
from ..import_resume import sha256_file
from ..mineru_api import MinerUError
from ..pdf_import_service import (
    cleanup_stale_document_storage_files,
    copy_local_document,
    detect_imported_pdf,
    document_storage_error,
    document_storage_target,
    release_document_storage_target,
)
from ..vision_api import VisionAPIError


class ImportJobsPort(Protocol):
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
            mode, provider_id = self.validate_parse_options(
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
    ) -> Dict[str, object]:
        with self._admission():
            suffix = Path(filename).suffix.lower()
            if suffix not in {".pdf", ".docx"}:
                raise MinerUError("只支持 PDF 或 DOCX 文件。")
            mode, provider_id = self.validate_parse_options(
                pdf_parse_mode,
                vision_provider_id,
            )
            result = self._chunked_uploads.start(
                filename,
                total_size,
                metadata={
                    "is_pdf": "1" if suffix == ".pdf" else "0",
                    "parse_mode": mode,
                    "provider_id": provider_id,
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
                is_pdf = metadata.get("is_pdf") == "1"
                mode, provider_id = self.validate_parse_options(
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
        if mode not in {"auto", "mineru", "vision"}:
            raise MinerUError("PDF 解析方式无效。")
        provider_id = (
            str(vision_provider_id or "").strip()
            if mode == "vision"
            else ""
        )
        if mode == "vision" and not provider_id:
            raise MinerUError("请选择一个其他解析 API。")
        return mode, provider_id

    def _upload_storage_details(
        self,
        filename: str,
        length: int,
        is_pdf: bool,
    ) -> Tuple[str, Path]:
        if length <= 0 or length > 600 * 1024 * 1024:
            raise MinerUError("文件为空或超过 600 MB 限制。")
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
            force_mineru = is_pdf and pdf_parse_mode == "mineru"
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
                    or str(profile.get("detected_pdf_type")) != "native_text"
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
                "provider_id": vision_provider_id or None,
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
        mode, provider_id = self.validate_parse_options(
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
                force_mineru = is_pdf and mode == "mineru"
                parse_route = None
                if is_pdf:
                    parse_route = (
                        "vision"
                        if provider_id
                        else "mineru"
                        if force_mineru
                        or str(profile.get("detected_pdf_type")) != "native_text"
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
                            "provider_id": provider_id or None,
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
