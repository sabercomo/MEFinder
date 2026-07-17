"""Local PDF import workflow used by the desktop import page."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from .indexer import build_index
from .mineru_api import (
    DEFAULT_MINERU_MANIFEST_DIR,
    DEFAULT_MINERU_RESULT_DIR,
    DEFAULT_MINERU_STATE_DIR,
    MinerUError,
    download_done_results,
    get_batch_status,
    resolve_mineru_config_path,
    save_segment_manifest,
    submit_local_pdf_segments,
)
from .pdf_extractors import detect_pdf_type, file_sha256


ProgressCallback = Callable[[Dict[str, object]], None]


def load_import_config(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"documents": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MinerUError("PDF 导入配置必须是 JSON 对象。")
    if not isinstance(data.get("documents"), list):
        data["documents"] = []
    return data


def save_import_config(path: Path, data: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def register_pdf(root: Path, pdf_path: Path, config_path: Optional[Path] = None) -> Dict[str, object]:
    """Add or update one PDF in the configured corpus without overwriting originals."""

    root = Path(root)
    pdf_path = Path(pdf_path)
    config_path = Path(config_path or root / "config" / "pdf_imports.json")
    data = load_import_config(config_path)
    documents = data["documents"]
    existing = next((item for item in documents if item.get("file_name") == pdf_path.name), None)
    if existing is None:
        source_file_id = f"pdf-import-{file_sha256(pdf_path)[:16]}"
        existing = {
            "enabled": True,
            "source_file_id": source_file_id,
            "document_id": source_file_id.upper().replace("-", "_"),
            "file_name": pdf_path.name,
            "title": pdf_path.stem,
            "author": None,
            "page_mapping": {"validated_by": None, "segments": []},
        }
        documents.append(existing)
    else:
        existing["enabled"] = True
    save_import_config(config_path, data)
    return existing


def attach_mineru_manifest(root: Path, source_file_id: str, manifest_path: Path, config_path: Optional[Path] = None) -> None:
    root = Path(root)
    config_path = Path(config_path or root / "config" / "pdf_imports.json")
    data = load_import_config(config_path)
    document = next((item for item in data["documents"] if item.get("source_file_id") == source_file_id), None)
    if document is None:
        raise MinerUError(f"PDF config not found: {source_file_id}")
    relative_manifest = Path(manifest_path)
    try:
        relative_manifest = relative_manifest.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    document["mineru"] = {"manifest": relative_manifest.as_posix()}
    save_import_config(config_path, data)


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
) -> Dict[str, object]:
    """Submit all pages in <=200-page precision tasks and download results."""

    root = Path(root)
    pdf_path = Path(pdf_path)
    config_path = resolve_mineru_config_path(root)
    state_dir = root / DEFAULT_MINERU_STATE_DIR
    manifest_dir = root / DEFAULT_MINERU_MANIFEST_DIR
    result_dir = root / DEFAULT_MINERU_RESULT_DIR
    manifest = submit_local_pdf_segments(
        pdf_path,
        config_path=config_path,
        state_dir=state_dir,
        manifest_dir=manifest_dir,
        data_id_prefix=source_file_id,
    )
    segments = [item for item in manifest.get("segments", []) if isinstance(item, dict)]
    pending = {str(item["batch_id"]): item for item in segments if item.get("batch_id")}
    completed = sum(1 for item in segments if item.get("status") == "skipped_existing_result")
    if on_progress:
        on_progress({"phase": "mineru_processing", "completed": completed, "total": len(segments)})
    deadline = time.time() + timeout_minutes * 60
    while pending and time.time() < deadline:
        for batch_id, segment in list(pending.items()):
            result = get_batch_status(batch_id, config_path=config_path, state_dir=state_dir)
            item = _first_extract_result(result)
            state = str(item.get("state") or "unknown").lower()
            segment["last_state"] = state
            if state == "done":
                downloaded = download_done_results(
                    batch_id,
                    config_path=config_path,
                    state_dir=state_dir,
                    result_dir=result_dir,
                )
                segment["status"] = "completed"
                segment["result_dirs"] = [str(path) for path in downloaded]
                if downloaded:
                    segment["result_dir"] = str(downloaded[0])
                pending.pop(batch_id, None)
                completed += 1
            elif state == "failed":
                segment["status"] = "failed"
                segment["error"] = str(item.get("err_msg") or "MinerU 解析失败")
                pending.pop(batch_id, None)
            if on_progress:
                on_progress({
                    "phase": "mineru_processing",
                    "completed": completed,
                    "total": len(segments),
                    "page_range": segment.get("page_ranges"),
                    "state": state,
                })
        if pending:
            time.sleep(poll_seconds)
    if pending:
        raise MinerUError("MinerU 解析超时，仍有分段任务未完成。")
    if any(item.get("status") == "failed" for item in segments):
        raise MinerUError("MinerU 有分段解析失败，请查看导入状态。")
    manifest_path = save_segment_manifest(str(manifest.get("data_id_prefix") or source_file_id), manifest, manifest_dir)
    attach_mineru_manifest(root, source_file_id, manifest_path)
    return {"manifest_path": str(manifest_path), "segments": len(segments), "status": "completed"}


def rebuild_local_index(root: Path, on_progress: Optional[ProgressCallback] = None) -> Dict[str, object]:
    root = Path(root)
    corpus_dir = root / "corpus" / "raw_docx"
    if not corpus_dir.exists():
        raise MinerUError("当前应用包没有附带完整 Word 原始语料，无法自动重建索引。")
    if on_progress:
        on_progress({"phase": "rebuilding_index"})
    return build_index(
        corpus_dir=corpus_dir,
        index_path=root / "data" / "index.json",
        database_path=root / "data" / "index.sqlite3",
        include_pdf=True,
        pdf_corpus_dir=root / "corpus" / "raw_pdf",
        pdf_config_path=root / "config" / "pdf_imports.json",
        parsed_pdf_dir=root / "corpus" / "parsed" / "pdf",
        backup_existing=True,
    )


def detect_imported_pdf(pdf_path: Path) -> Dict[str, object]:
    return detect_pdf_type(Path(pdf_path))
