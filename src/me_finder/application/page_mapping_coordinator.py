"""Coordinate durable PDF page-mapping workflows outside HTTP handlers."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from ..app_context import AppPaths
from ..auto_page_mapping import has_manual_mapping
from ..lifecycle import DurableOperationGate
from ..mineru_api import MinerUError
from ..import_config_store import (
    import_config_lock,
    load_import_config,
    save_import_config,
)
from ..pdf_page_mapping import normalize_manual_mapping_segments
from ..runtime_page_mapping import (
    apply_mapping_to_database,
    normalize_auto_segments,
)
from .document_query_service import DocumentQueryError


Config = Dict[str, object]
ConfigLock = Callable[[], AbstractContextManager[None]]
ConfigLoader = Callable[[Path], Config]
ConfigSaver = Callable[[Path, Config], None]
PDFExtractor = Callable[..., Dict[str, object]]
DatabaseMappingApplier = Callable[..., Dict[str, int]]


class PageMappingIndexPort(Protocol):
    def mutation(self) -> AbstractContextManager[None]:
        ...

    def source(self, source_file_id: str) -> Optional[Dict[str, object]]:
        ...

    def suspend(self) -> None:
        ...

    def reopen(self, *, attempts: int = 1) -> bool:
        ...


class DocumentQueryPort(Protocol):
    def source_path(self, source_file_id: str) -> Path:
        ...


class ImportJobPort(Protocol):
    def register_background_job(self, job: Mapping[str, object]) -> None:
        ...

    def rebuild_runtime_index(
        self,
        job_id: str,
        expected_source_ids: Optional[List[str]] = None,
    ) -> set[str]:
        ...

    def update_import_job(self, job_id: str, **updates: object) -> None:
        ...


class PageMappingCoordinator:
    """Apply and detect page mappings while preserving index consistency."""

    def __init__(
        self,
        paths: AppPaths,
        index_runtime: PageMappingIndexPort,
        durable_operations: DurableOperationGate,
        document_queries: DocumentQueryPort,
        import_jobs: ImportJobPort,
        *,
        extract_pdf: PDFExtractor,
        config_lock: ConfigLock = import_config_lock,
        load_config: ConfigLoader = load_import_config,
        save_config: ConfigSaver = save_import_config,
        apply_mapping: DatabaseMappingApplier = apply_mapping_to_database,
    ) -> None:
        self._paths = paths
        self._index_runtime = index_runtime
        self._durable_operations = durable_operations
        self._document_queries = document_queries
        self._import_jobs = import_jobs
        self._extract_pdf = extract_pdf
        self._config_lock = config_lock
        self._load_config = load_config
        self._save_config = save_config
        self._apply_mapping = apply_mapping
        self._active_lock = threading.Lock()
        self._active_source_ids: set[str] = set()

    def active_source_ids(self) -> set[str]:
        """Return a concurrency-safe snapshot of calibrations in progress."""

        with self._active_lock:
            return set(self._active_source_ids)

    def apply_manual_page_mapping(
        self,
        source_file_id: str,
        segments: Sequence[Mapping[str, object]],
    ) -> None:
        """Persist a manual mapping and rebuild the index as one operation."""

        with self._durable_operations.operation():
            cleaned_segments = normalize_manual_mapping_segments(segments)
            with self._index_runtime.mutation():
                config_path = self._config_path
                if not config_path.exists():
                    raise MinerUError("PDF 导入配置不存在。")
                with self._config_lock():
                    config = self._load_config(config_path)
                    document = self._configured_document(config, source_file_id)
                    document.setdefault("page_mapping", {})
                    document["page_mapping"]["segments"] = cleaned_segments
                    document["page_mapping"]["validated_by"] = "manual_ui"
                    document["page_mapping"]["mapping_origin"] = "manual"
                    document["page_mapping"]["mapping_status"] = (
                        "manual_mapped" if cleaned_segments else "unmapped"
                    )
                    document["page_mapping"]["updated_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    self._save_config(config_path, config)

                job_id = f"calibration-{uuid.uuid4().hex[:12]}"
                self._import_jobs.register_background_job(
                    {
                        "job_id": job_id,
                        "status": "processing",
                        "phase": "rebuilding_index",
                        "message": "正在应用页码校准并重建索引…",
                    }
                )
                try:
                    self._import_jobs.rebuild_runtime_index(job_id)
                    self._import_jobs.update_import_job(
                        job_id,
                        status="completed",
                        phase="completed",
                        message="页码校准已生效",
                    )
                except (
                    MinerUError,
                    OSError,
                    ValueError,
                    RuntimeError,
                    sqlite3.Error,
                    json.JSONDecodeError,
                ) as exc:
                    self._import_jobs.update_import_job(
                        job_id,
                        status="failed",
                        phase="failed",
                        message=str(exc),
                    )
                    raise

    def detect_auto_page_mapping(
        self,
        source_file_id: str,
    ) -> Dict[str, object]:
        """Dry-run automatic mapping detection under the index mutation lock."""

        try:
            with self._index_runtime.mutation():
                with self._active_lock:
                    self._active_source_ids.add(source_file_id)
                return self._detect_auto_page_mapping(source_file_id)
        finally:
            with self._active_lock:
                self._active_source_ids.discard(source_file_id)

    def apply_live_auto_mapping(
        self,
        source_file_id: str,
        segments: Sequence[Dict[str, object]],
        auto_mapping: Dict[str, object],
        replace_manual: bool,
    ) -> Dict[str, int]:
        """Apply mapping directly to config and SQLite without a rebuild."""

        with self._index_runtime.mutation():
            with self._durable_operations.operation():
                return self._apply_live_auto_mapping(
                    source_file_id,
                    segments,
                    auto_mapping,
                    replace_manual,
                )

    def accept_auto_page_mapping(self, source_file_id: str) -> int:
        """Promote indexed automatic segments to manual config and rebuild."""

        job_id = f"auto-map-{uuid.uuid4().hex[:12]}"
        job_registered = False
        try:
            with (
                self._durable_operations.operation(),
                self._index_runtime.mutation(),
            ):
                segment_count = self._persist_accepted_mapping(source_file_id)
                self._import_jobs.register_background_job(
                    {
                        "job_id": job_id,
                        "status": "processing",
                        "phase": "rebuilding_index",
                        "message": "正在接受自动页码映射并重建索引…",
                    }
                )
                job_registered = True
                self._import_jobs.rebuild_runtime_index(job_id)
                self._import_jobs.update_import_job(
                    job_id,
                    status="completed",
                    phase="completed",
                    message="自动页码映射已接受",
                )
        except (
            MinerUError,
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            if job_registered:
                self._import_jobs.update_import_job(
                    job_id,
                    status="failed",
                    phase="failed",
                    message=str(exc),
                )
            raise
        return segment_count

    @property
    def _config_path(self) -> Path:
        return self._paths.config_root / "pdf_imports.json"

    @staticmethod
    def _configured_document(
        config: Config,
        source_file_id: str,
    ) -> Dict[str, object]:
        document = next(
            (
                item
                for item in config.get("documents", [])
                if item.get("source_file_id") == source_file_id
            ),
            None,
        )
        if not document:
            raise MinerUError("PDF 配置中找不到该文献。")
        return document

    def _detect_auto_page_mapping(
        self,
        source_file_id: str,
    ) -> Dict[str, object]:
        config_path = self._config_path
        if not config_path.exists():
            raise MinerUError("PDF 导入配置不存在。")
        config = self._load_config(config_path)
        document = self._configured_document(config, source_file_id)
        try:
            path = self._document_queries.source_path(source_file_id)
        except DocumentQueryError as exc:
            if "不存在" in str(exc):
                return {
                    "source_id": source_file_id,
                    "mapping_status": "source_missing",
                    "failure_reasons": ["source_missing"],
                    "selected_segments": [],
                    "applied_segments": [],
                    "manual_mapping_present": has_manual_mapping(document),
                    "dry_run": True,
                }
            raise MinerUError(str(exc)) from exc
        if path.suffix.lower() != ".pdf":
            raise MinerUError("自动页码检测只支持 PDF。")

        manual_present = has_manual_mapping(document)
        detection_config = copy.deepcopy(document)
        detection_config.setdefault("page_mapping", {})
        detection_config["page_mapping"]["segments"] = []
        detection_config["page_mapping"]["validated_by"] = None
        extracted = self._extract_pdf(
            path,
            self._paths.runtime_root,
            detection_config,
            parsed_dir=None,
        )
        sources = extracted.get("source_files", [])
        if not sources:
            raise MinerUError("无法读取文献页码证据。")
        profile = sources[0].get("pdf_profile") or {}
        result = dict(profile.get("auto_page_mapping") or {})
        result["manual_mapping_present"] = manual_present
        result["dry_run"] = True
        result["source_id"] = source_file_id
        result["source_file"] = path.name
        result["current_mapping"] = document.get("page_mapping") or {}
        return result

    def _apply_live_auto_mapping(
        self,
        source_file_id: str,
        segments: Sequence[Dict[str, object]],
        auto_mapping: Dict[str, object],
        replace_manual: bool,
    ) -> Dict[str, int]:
        config_path = self._config_path
        with self._config_lock():
            config = self._load_config(config_path)
            document = self._configured_document(config, source_file_id)
            if has_manual_mapping(document) and not replace_manual:
                raise MinerUError(
                    "当前文献已有人工页码映射，"
                    "必须明确确认后才能替换。"
                )
            cleaned = normalize_auto_segments(segments)
            if not cleaned:
                raise MinerUError("没有可应用的自动页码区间。")
            original_config = copy.deepcopy(config)
            document.setdefault("page_mapping", {})
            document["page_mapping"]["segments"] = cleaned
            document["page_mapping"]["validated_by"] = "auto_mapping_ui"
            document["page_mapping"]["mapping_origin"] = "auto"
            document["page_mapping"]["updated_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            confidence_levels = {
                str(item.get("confidence_level") or "") for item in cleaned
            }
            mapping_status = (
                "auto_mapped_high"
                if confidence_levels == {"high"}
                else "auto_mapped_medium"
            )
            document["page_mapping"]["mapping_status"] = mapping_status
            self._save_config(config_path, config)
            self._index_runtime.suspend()
            database_updated = False
            try:
                updated = self._apply_mapping(
                    self._paths.index_path,
                    source_file_id,
                    cleaned,
                    auto_mapping=auto_mapping,
                    mapping_status=mapping_status,
                )
                database_updated = True
                self._index_runtime.reopen()
                return updated
            except (
                MinerUError,
                OSError,
                ValueError,
                RuntimeError,
                sqlite3.Error,
                json.JSONDecodeError,
            ):
                if not database_updated:
                    self._save_config(config_path, original_config)
                self._index_runtime.reopen()
                raise

    def _persist_accepted_mapping(self, source_file_id: str) -> int:
        config_path = self._config_path
        if not config_path.exists():
            raise MinerUError("PDF 导入配置不存在。")
        source = self._index_runtime.source(source_file_id)
        if not source:
            raise MinerUError("文献未找到。")
        auto_mapping = (
            (source.get("pdf_profile") or {}).get("auto_page_mapping") or {}
        )
        applied = [
            segment
            for segment in auto_mapping.get("applied_segments", [])
            if isinstance(segment, dict)
        ]
        if not applied:
            raise MinerUError("没有可接受的高置信度自动映射段。")

        with self._config_lock():
            config = self._load_config(config_path)
            document = self._configured_document(config, source_file_id)
            manual_segments: List[Dict[str, object]] = []
            for segment in applied:
                clean: Dict[str, object] = {
                    "pdf_page_start": int(segment["pdf_page_start"]),
                    "pdf_page_end": int(segment["pdf_page_end"]),
                    "citation_page_start": str(segment["citation_page_start"]),
                    "number_style": str(
                        segment.get("number_style") or "arabic"
                    ),
                    "method": "manual_segment",
                    "confidence": float(
                        segment.get("mapping_confidence") or 0.95
                    ),
                    "label": "已接受自动页码映射",
                    "evidence": segment.get("mapping_evidence"),
                    "layout_mode": (
                        "spread"
                        if segment.get("layout_mode") == "spread"
                        else "single"
                    ),
                }
                if clean["layout_mode"] == "spread":
                    clean["reading_direction"] = (
                        "rtl"
                        if segment.get("reading_direction") == "rtl"
                        else "ltr"
                    )
                    clean["gutter_x"] = segment.get("gutter_x") or 0.5
                manual_segments.append(clean)
            document.setdefault("page_mapping", {})
            document["page_mapping"]["segments"] = manual_segments
            document["page_mapping"][
                "validated_by"
            ] = "auto_mapping_accepted"
            document["page_mapping"]["mapping_status"] = "manual_mapped"
            document["page_mapping"]["updated_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            self._save_config(config_path, config)
        return len(manual_segments)
