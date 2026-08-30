"""Adapters that run PDF parsers and publish their structured manifests."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .import_config_store import attach_mineru_manifest, attach_parser_manifest
from .import_resume import COMPLETED_UNIT_STATUSES, resume_summary
from .mineru_api import (
    DEFAULT_MINERU_API_BASE,
    DEFAULT_MINERU_MANIFEST_DIR,
    DEFAULT_MINERU_RESULT_DIR,
    DEFAULT_MINERU_STATE_DIR,
    MinerUConfig,
    MinerUError,
    download_done_results,
    get_batch_status,
    read_mineru_config_data,
    resolve_mineru_config_path,
    save_segment_manifest,
    submit_local_pdf_segments,
)
from .mineru_provider import MinerUCloudProvider
from .mineru_local_provider import (
    MINERU_LOCAL_PROVIDER_ID,
    MinerULocalProvider,
)
from .mineru_local_settings import load_mineru_local_config
from .large_document.engine import LargeDocumentJobEngine
from .large_document.job_ledger import JobLedger
from .large_document.merge import iter_normalized_pages
from .large_document.mineru_accounts import (
    MinerUAccountService,
    resolve_mineru_accounts_path,
)
from .local_ocr_provider import (
    LocalOCRProvider,
    choose_local_ocr_engine,
)
from .local_ocr_settings import (
    LocalOCRCancelled,
    LocalOCRError,
    load_local_ocr_config,
    resolve_local_ocr_config_path,
)
from .vision_api import parse_pdf_with_vision_provider


ProgressCallback = Callable[[Dict[str, object]], None]


def _first_extract_result(result: Dict[str, object]) -> Dict[str, object]:
    data = result.get("data") or {}
    if not isinstance(data, dict):
        return {}
    items = data.get("extract_result") or []
    if isinstance(items, dict):
        return items
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def parse_pdf_with_mineru(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    on_progress: Optional[ProgressCallback] = None,
    poll_seconds: int = 20,
    timeout_minutes: int = 180,
    use_local: bool = False,
) -> Dict[str, object]:
    """Submit all pages in <=200-page precision tasks and download results."""

    root = Path(root)
    pdf_path = Path(pdf_path)
    if use_local:
        return _parse_pdf_with_mineru_local(
            root,
            pdf_path,
            source_file_id,
            on_progress=on_progress,
            timeout_minutes=timeout_minutes,
        )
    accounts_path = resolve_mineru_accounts_path(root)
    if accounts_path.is_file():
        ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
        account_service = MinerUAccountService(
            ledger=ledger,
            config_path=accounts_path,
        )
        accounts = account_service.list_accounts()
        if not accounts:
            raise MinerUError(
                "尚未配置 MinerU 账号，请先在设置中添加账号。",
                allow_parser_fallback=True,
            )
        if not any(item.enabled and item.configured for item in accounts):
            raise MinerUError(
                "已保存的 MinerU 账号全部停用或缺少 Token，请先在设置中启用账号。",
                allow_parser_fallback=True,
            )
        return _parse_pdf_with_mineru_accounts(
            root,
            pdf_path,
            source_file_id,
            ledger=ledger,
            account_service=account_service,
            on_progress=on_progress,
            poll_seconds=poll_seconds,
            timeout_minutes=timeout_minutes,
        )
    config_path = resolve_mineru_config_path(root)
    state_dir = root / DEFAULT_MINERU_STATE_DIR
    manifest_dir = root / DEFAULT_MINERU_MANIFEST_DIR
    result_dir = root / DEFAULT_MINERU_RESULT_DIR
    manifest = submit_local_pdf_segments(
        pdf_path,
        config_path=config_path,
        state_dir=state_dir,
        manifest_dir=manifest_dir,
        result_dir=result_dir,
        data_id_prefix=source_file_id,
    )
    segments = [item for item in manifest.get("segments", []) if isinstance(item, dict)]
    pending = {
        str(item["batch_id"]): item
        for item in segments
        if item.get("batch_id")
        and str(item.get("status") or "").lower() not in COMPLETED_UNIT_STATUSES
    }
    completed = sum(
        1
        for item in segments
        if str(item.get("status") or "").lower() in COMPLETED_UNIT_STATUSES
    )
    save_segment_manifest(str(manifest.get("data_id_prefix") or source_file_id), manifest, manifest_dir)
    if on_progress:
        on_progress({
            "phase": "mineru_processing",
            "completed": completed,
            "total": len(segments),
            "total_pages": manifest.get("total_pages"),
            "completed_pages": manifest.get("completed_pages", []),
            "failed_pages": manifest.get("failed_pages", []),
        })
    deadline = time.time() + timeout_minutes * 60
    while pending and time.time() < deadline:
        for batch_id, segment in list(pending.items()):
            try:
                result = get_batch_status(batch_id, config_path=config_path, state_dir=state_dir)
            except Exception as exc:
                segment["last_error"] = str(exc)
                segment["status"] = (
                    "failed"
                    if isinstance(exc, MinerUError)
                    and exc.retry_with_new_task
                    else "processing"
                )
                if segment["status"] == "failed":
                    segment["error"] = str(exc)
                save_segment_manifest(
                    str(manifest.get("data_id_prefix") or source_file_id),
                    manifest,
                    manifest_dir,
                )
                raise MinerUError(
                    str(exc),
                    retry_with_new_task=bool(
                        isinstance(exc, MinerUError)
                        and exc.retry_with_new_task
                    ),
                    allow_parser_fallback=False,
                ) from exc
            item = _first_extract_result(result)
            state = str(item.get("state") or "unknown").lower()
            segment["last_state"] = state
            if state == "done":
                segment["status"] = "processing"
                segment["phase"] = "downloading"
                save_segment_manifest(
                    str(manifest.get("data_id_prefix") or source_file_id),
                    manifest,
                    manifest_dir,
                )
                try:
                    downloaded = download_done_results(
                        batch_id,
                        config_path=config_path,
                        state_dir=state_dir,
                        result_dir=result_dir,
                    )
                except Exception as exc:
                    segment["status"] = "processing"
                    segment["phase"] = "download_retry"
                    segment["last_error"] = str(exc)
                    save_segment_manifest(
                        str(manifest.get("data_id_prefix") or source_file_id),
                        manifest,
                        manifest_dir,
                    )
                    raise MinerUError(
                        str(exc),
                        allow_parser_fallback=False,
                    ) from exc
                segment["status"] = "completed"
                segment["result_dirs"] = [str(path) for path in downloaded]
                if downloaded:
                    segment["result_dir"] = str(downloaded[0])
                segment.pop("error", None)
                segment.pop("last_error", None)
                segment.pop("phase", None)
                pending.pop(batch_id, None)
                completed += 1
            elif state == "failed":
                segment["status"] = "failed"
                segment["error"] = str(item.get("err_msg") or "MinerU 解析失败")
                segment.pop("phase", None)
                pending.pop(batch_id, None)
            else:
                segment["status"] = "processing"
                segment.pop("last_error", None)
                segment.pop("phase", None)
            save_segment_manifest(
                str(manifest.get("data_id_prefix") or source_file_id),
                manifest,
                manifest_dir,
            )
            if on_progress:
                on_progress({
                    "phase": "mineru_processing",
                    "completed": completed,
                    "total": len(segments),
                    "page_range": segment.get("page_ranges"),
                    "state": state,
                    "total_pages": manifest.get("total_pages"),
                    "completed_pages": manifest.get("completed_pages", []),
                    "failed_pages": manifest.get("failed_pages", []),
                })
        if pending:
            time.sleep(poll_seconds)
    if pending:
        for segment in pending.values():
            segment["last_error"] = "MinerU 解析超时，等待下次继续检查。"
        save_segment_manifest(
            str(manifest.get("data_id_prefix") or source_file_id),
            manifest,
            manifest_dir,
        )
        raise MinerUError(
            "MinerU 解析超时，仍有分段任务未完成。",
            allow_parser_fallback=False,
        )
    if any(item.get("status") == "failed" for item in segments):
        save_segment_manifest(
            str(manifest.get("data_id_prefix") or source_file_id),
            manifest,
            manifest_dir,
        )
        raise MinerUError("MinerU 有分段解析失败，请查看导入状态。")
    manifest_path = save_segment_manifest(str(manifest.get("data_id_prefix") or source_file_id), manifest, manifest_dir)
    attach_mineru_manifest(root, source_file_id, manifest_path)
    return {
        "manifest_path": str(manifest_path),
        "segments": len(segments),
        "status": "completed",
        "resume": resume_summary(manifest, manifest_path=manifest_path),
    }


def _parse_pdf_with_mineru_accounts(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    *,
    ledger: JobLedger,
    account_service: MinerUAccountService,
    on_progress: Optional[ProgressCallback],
    poll_seconds: int,
    timeout_minutes: int,
) -> Dict[str, object]:
    """Run the v0.4.2 physical-slice engine with independent credentials."""

    global_config = read_mineru_config_data(resolve_mineru_config_path(root))
    api_base = str(
        global_config.get("api_base") or DEFAULT_MINERU_API_BASE
    ).rstrip("/")
    provider = MinerUCloudProvider(
        config=MinerUConfig(token="", api_base=api_base),
        max_pages_per_file=200,
        max_concurrency=1,
    )
    pool = account_service.create_pool(
        provider_max_concurrency=provider.capabilities().max_concurrency
    )
    pool.reconcile_in_flight()
    engine = LargeDocumentJobEngine(
        ledger=ledger,
        provider=provider,
        work_dir=root / "corpus" / "processed" / "parser_jobs",
        credential_pool=pool,
    )
    job = engine.prepare(
        source_path=pdf_path,
        source_file_id=source_file_id,
        document_id=source_file_id.upper().replace("-", "_"),
        model="vlm",
        options={"language": "ch", "is_ocr": True},
    )
    deadline = time.time() + max(1, int(timeout_minutes)) * 60
    while job.status not in {"validated", "permanent_failure", "cancelled"}:
        if time.time() >= deadline:
            raise MinerUError(
                "MinerU 大文档解析超时，已保留分片任务，下次可从断点继续。",
                allow_parser_fallback=False,
            )
        job = engine.run_once(job.id)
        if on_progress:
            slices = ledger.list_slice_jobs(job.id)
            waiting_for_credential = any(
                item.status == "waiting" and not item.remote_task_id
                for item in slices
            )
            completed_pages = [
                page
                for item in slices
                if item.status == "completed"
                for page in range(item.page_start, item.page_end + 1)
            ]
            on_progress(
                {
                    "phase": "mineru_processing",
                    "completed": job.completed_slices,
                    "total": job.total_slices,
                    "total_pages": job.total_pages,
                    "completed_pages": completed_pages,
                    "failed_pages": [],
                    "document_job_id": job.id,
                    "waiting_for_credential": waiting_for_credential,
                }
            )
        if job.status in {"validated", "permanent_failure", "cancelled"}:
            break
        time.sleep(max(0, poll_seconds))
    if job.status != "validated":
        raise MinerUError(
            job.error_summary or f"MinerU 大文档任务未完成：{job.status}",
            allow_parser_fallback=False,
        )
    return _publish_mineru_engine_results(
        root,
        source_file_id,
        ledger=ledger,
        document_job_id=job.id,
    )


def _parse_pdf_with_mineru_local(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    *,
    on_progress: Optional[ProgressCallback],
    timeout_minutes: int,
) -> Dict[str, object]:
    """Run an explicitly requested import through the user's local service."""

    config = load_mineru_local_config(resolve_mineru_config_path(root))
    provider = MinerULocalProvider(config)
    ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
    engine = LargeDocumentJobEngine(
        ledger=ledger,
        provider=provider,
        work_dir=root / "corpus" / "processed" / "parser_jobs",
    )
    job = engine.prepare(
        source_path=pdf_path,
        source_file_id=source_file_id,
        document_id=source_file_id.upper().replace("-", "_"),
        model=config.backend,
        options={
            "backend": config.backend,
            "language": config.language,
            "parse_method": config.parse_method,
            "formula_enable": config.formula_enable,
            "table_enable": config.table_enable,
        },
    )
    deadline = time.time() + max(1, int(timeout_minutes)) * 60
    while job.status not in {"validated", "permanent_failure", "cancelled"}:
        if time.time() >= deadline:
            raise MinerUError(
                "本地 MinerU 解析超时，已保留任务进度。",
                allow_parser_fallback=False,
            )
        job = engine.run_once(job.id)
        if on_progress:
            slices = ledger.list_slice_jobs(job.id)
            on_progress(
                {
                    "phase": "mineru_processing",
                    "provider_id": MINERU_LOCAL_PROVIDER_ID,
                    "provider_name": "本地 MinerU",
                    "message": "正在使用本地 MinerU 解析 PDF…",
                    "completed": job.completed_slices,
                    "total": job.total_slices,
                    "total_pages": job.total_pages,
                    "completed_pages": [
                        page
                        for item in slices
                        if item.status == "completed"
                        for page in range(item.page_start, item.page_end + 1)
                    ],
                    "failed_pages": [],
                    "document_job_id": job.id,
                }
            )
        if job.status in {"validated", "permanent_failure", "cancelled"}:
            break
        time.sleep(2)
    if job.status != "validated":
        failed = next(
            (
                item.last_error
                for item in ledger.list_slice_jobs(job.id)
                if item.last_error
            ),
            None,
        )
        raise MinerUError(
            failed or job.error_summary or f"本地 MinerU 任务未完成：{job.status}",
            allow_parser_fallback=False,
        )
    return _publish_mineru_engine_results(
        root,
        source_file_id,
        ledger=ledger,
        document_job_id=job.id,
        provider_id=MINERU_LOCAL_PROVIDER_ID,
        provider_name="本地 MinerU",
    )


def _publish_mineru_engine_results(
    root: Path,
    source_file_id: str,
    *,
    ledger: JobLedger,
    document_job_id: str,
    provider_id: str = "mineru-cloud",
    provider_name: str = "MinerU",
    parser_id: str = "mineru",
) -> Dict[str, object]:
    """Bridge validated normalized slices into the existing indexer contract."""

    job = ledger.get_document_job(document_job_id)
    if job.status != "validated":
        raise MinerUError("只有通过完整页码校验的解析任务才能进入索引。")
    manifest_dir = root / (
        DEFAULT_MINERU_MANIFEST_DIR
        if parser_id == "mineru"
        else "corpus/processed/local_ocr/manifests"
    )
    result_directory = (
        f"engine-{source_file_id}"
        if provider_id == "mineru-cloud"
        else f"engine-{provider_id}-{source_file_id}"
    )
    result_root = root / (
        DEFAULT_MINERU_RESULT_DIR
        if parser_id == "mineru"
        else "corpus/processed/local_ocr/results"
    ) / result_directory
    segments: List[Dict[str, object]] = []
    has_text = False
    for item in ledger.list_slice_jobs(job.id):
        if item.status != "completed" or not item.result_path:
            raise MinerUError("解析任务缺少已验证的切片结果。")
        result_dir = result_root / (
            f"pages-{item.page_start:06d}-{item.page_end:06d}"
        )
        content_path = result_dir / "content_list.json"
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content: List[Dict[str, object]] = []
        for page in iter_normalized_pages(Path(item.result_path)):
            physical_page = int(page["physical_pdf_page"])
            local_page = physical_page - item.page_start
            blocks = page.get("blocks") or []
            if isinstance(blocks, list) and blocks:
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    text = str(block.get("text") or "").strip()
                    if not text:
                        continue
                    has_text = True
                    content.append(
                        {
                            "page_idx": local_page,
                            "text": text,
                            "type": block.get("type"),
                            "text_level": block.get("text_level"),
                            "bbox": block.get("bbox"),
                            "reading_order": block.get("reading_order"),
                        }
                    )
            else:
                text = str(page.get("text") or "").strip()
                if text:
                    has_text = True
                    content.append(
                        {"page_idx": local_page, "text": text, "type": "text"}
                    )
        temporary = content_path.with_name(
            f".{content_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(content_path)
        finally:
            temporary.unlink(missing_ok=True)
        segments.append(
            {
                "status": "completed",
                "page_start": item.page_start,
                "page_end": item.page_end,
                "page_ranges": f"{item.page_start}-{item.page_end}",
                "page_index_offset": item.global_page_offset,
                "result_dir": str(result_dir),
                "credential_id": item.credential_id,
            }
        )
    if parser_id != "mineru" and not has_text:
        raise LocalOCRError("本地 OCR 未在整本文档中识别出文字。")
    manifest: Dict[str, object] = {
        "api": "precision" if parser_id == "mineru" else parser_id,
        "parser": parser_id,
        "provider_id": provider_id,
        "provider_name": provider_name,
        "model": job.parser_model or "vlm",
        "file_hash": job.source_sha256,
        "data_id_prefix": source_file_id,
        "total_pages": job.total_pages,
        "document_job_id": job.id,
        "segments": segments,
    }
    manifest_path = save_segment_manifest(
        source_file_id,
        manifest,
        manifest_dir,
    )
    if parser_id == "mineru":
        attach_mineru_manifest(root, source_file_id, manifest_path)
    else:
        attach_parser_manifest(
            root,
            source_file_id,
            manifest_path,
            provider_id=provider_id,
            provider_name=provider_name,
            model=job.parser_model or "unknown",
            parser=parser_id,
        )
    return {
        "manifest_path": str(manifest_path),
        "segments": len(segments),
        "status": "completed",
        "document_job_id": job.id,
        "resume": resume_summary(manifest, manifest_path=manifest_path),
    }


def parse_pdf_with_local_ocr(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    *,
    on_progress: Optional[ProgressCallback] = None,
    cancel_requested: Optional[Callable[[], bool]] = None,
) -> Dict[str, object]:
    """Parse one PDF with an installed NDL runtime using page images only."""

    root = Path(root)
    pdf_path = Path(pdf_path)
    config = load_local_ocr_config(
        resolve_local_ocr_config_path(root),
        require_available=True,
    )
    probe_dir = (
        root
        / "corpus"
        / "processed"
        / "local_ocr"
        / "probes"
        / f"{source_file_id}-{uuid.uuid4().hex}"
    )
    try:
        selected, selection = choose_local_ocr_engine(
            config.available_engines,
            pdf_path=pdf_path,
            work_dir=probe_dir,
            render_dpi=config.render_dpi,
            probe_pages=config.probe_pages,
            timeout_seconds_per_page=config.timeout_seconds_per_page,
            blank_ink_ratio=config.blank_ink_ratio,
            cancel_requested=cancel_requested,
        )
    except Exception as exc:
        if cancel_requested is not None and cancel_requested():
            raise LocalOCRCancelled("本地 OCR 已取消。") from exc
        if isinstance(exc, LocalOCRError):
            raise
        raise LocalOCRError(f"本地 OCR 探针失败：{exc}") from exc

    completed_pages: set[int] = set()
    total_pages = 0

    def page_completed(physical_page: int) -> None:
        completed_pages.add(int(physical_page))
        if on_progress is not None:
            on_progress(
                {
                    "phase": "local_ocr_processing",
                    "provider_id": selected.provider_id,
                    "provider_name": selected.display_name,
                    "completed": len(completed_pages),
                    "total": total_pages,
                    "total_pages": total_pages,
                    "completed_pages": sorted(completed_pages),
                    "failed_pages": [],
                }
            )

    provider = LocalOCRProvider(
        selected,
        render_dpi=config.render_dpi,
        pages_per_slice=config.pages_per_slice,
        timeout_seconds_per_page=config.timeout_seconds_per_page,
        blank_ink_ratio=config.blank_ink_ratio,
        cancel_requested=cancel_requested,
        page_progress=page_completed,
    )
    ledger = JobLedger(root / "data" / "parser_jobs.sqlite3")
    engine = LargeDocumentJobEngine(
        ledger=ledger,
        provider=provider,
        work_dir=root / "corpus" / "processed" / "parser_jobs",
    )
    job = engine.prepare(
        source_path=pdf_path,
        source_file_id=source_file_id,
        document_id=source_file_id.upper().replace("-", "_"),
        model=selected.version,
        options={
            "input_mode": "page_image",
            "render_dpi": config.render_dpi,
            "blank_ink_ratio": config.blank_ink_ratio,
            "engine_version": selected.version,
            "weights_sha256": selected.weights_sha256,
        },
    )
    total_pages = job.total_pages
    for item in ledger.list_slice_jobs(job.id):
        if item.status == "completed":
            completed_pages.update(range(item.page_start, item.page_end + 1))
    if on_progress is not None and completed_pages:
        on_progress(
            {
                "phase": "local_ocr_processing",
                "provider_id": selected.provider_id,
                "provider_name": selected.display_name,
                "completed": len(completed_pages),
                "total": total_pages,
                "total_pages": total_pages,
                "completed_pages": sorted(completed_pages),
                "failed_pages": [],
            }
        )
    while job.status not in {
        "validated",
        "permanent_failure",
        "cancelled",
    }:
        job = engine.run_once(job.id)
    if job.status == "cancelled":
        raise LocalOCRCancelled("本地 OCR 已取消。")
    if job.status != "validated":
        failed = next(
            (
                item.last_error
                for item in ledger.list_slice_jobs(job.id)
                if item.last_error
            ),
            None,
        )
        raise LocalOCRError(
            failed or job.error_summary or "本地 OCR 未能完成全部页面。"
        )
    published = _publish_mineru_engine_results(
        root,
        source_file_id,
        ledger=ledger,
        document_job_id=job.id,
        provider_id=selected.provider_id,
        provider_name=selected.display_name,
        parser_id=selected.provider_id,
    )
    published["selection"] = selection
    published["provider_id"] = selected.provider_id
    published["provider_name"] = selected.display_name
    return published


def parse_pdf_with_provider(
    root: Path,
    pdf_path: Path,
    source_file_id: str,
    provider_id: str,
    on_progress: Optional[ProgressCallback] = None,
) -> Dict[str, object]:
    """Parse a PDF through a configured OpenAI-compatible vision provider."""

    result = parse_pdf_with_vision_provider(
        root,
        pdf_path,
        source_file_id,
        provider_id,
        on_progress=on_progress,
    )
    attach_parser_manifest(
        root,
        source_file_id,
        Path(str(result["manifest_path"])),
        provider_id=str(result["provider_id"]),
        provider_name=str(result["provider_name"]),
        model=str(result["model"]),
    )
    return result
