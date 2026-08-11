"""PDF detection and native text extraction for the local MVP."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .auto_page_mapping import (
    apply_auto_mapping_to_pages,
    has_manual_mapping,
)
from .bibliographic_metadata import METADATA_FIELDS
from .import_resume import resume_summary
from .normalization import (
    compact_text,
    normalize_pdf_text,
    normalize_text,
    punctuationless_text,
    split_sentences,
)
from .page_mapping_service import PageMappingService
from .pdf_page_mapping import PageMapper, mapped_page_display


PDF_TYPES = {"native_text", "scanned", "broken_text", "complex_layout"}
CROSS_PAGE_TAIL_CHARS = 900
CROSS_PAGE_HEAD_CHARS = 900

_BIBLIOGRAPHIC_STATE_FIELDS = (
    "document_type",
    "metadata_status",
    "metadata_source",
    "metadata_confidence",
    "metadata_evidence",
    "metadata_conflicts",
    "metadata_missing_fields",
)


def _attach_bibliographic_metadata(
    source_file: Dict[str, object], bibliographic: Mapping[str, object]
) -> None:
    """Copy every canonical bibliographic field into the searchable source record."""
    top_level_fields = (*METADATA_FIELDS, "publication_year", *_BIBLIOGRAPHIC_STATE_FIELDS)
    nested_fields = (*METADATA_FIELDS, *_BIBLIOGRAPHIC_STATE_FIELDS)
    for field in top_level_fields:
        if bibliographic.get(field) not in (None, ""):
            source_file[field] = bibliographic[field]
    source_file["bibliographic_metadata"] = {
        field: bibliographic[field]
        for field in nested_fields
        if bibliographic.get(field) not in (None, "")
    }


class PDFExtractionError(RuntimeError):
    pass


@dataclass
class PDFTextPage:
    pdf_page_index: int
    pdf_page_label: Optional[str]
    raw_text: str
    blocks: List[Dict[str, object]]
    parser: str
    parser_version: str
    page_width: Optional[float] = None
    page_height: Optional[float] = None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pdf_import_config(config_path: Path) -> List[Dict[str, object]]:
    if not Path(config_path).exists():
        return []
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    documents = raw.get("documents", raw if isinstance(raw, list) else [])
    if not isinstance(documents, list):
        return []
    return [doc for doc in documents if isinstance(doc, dict) and doc.get("enabled", True)]


def extract_configured_pdfs(
    root: Path,
    pdf_corpus_dir: Path,
    config_path: Path,
    parsed_dir: Optional[Path] = None,
    limit: Optional[int] = None,
) -> Dict[str, List[Dict[str, object]]]:
    output = {
        "source_files": [],
        "volumes": [],
        "works": [],
        "paragraphs": [],
        "pdf_pages": [],
        "pdf_page_mappings": [],
        "pdf_import_runs": [],
        "audit_issues": [],
    }
    configs = load_pdf_import_config(config_path)
    if limit is not None:
        configs = configs[: max(0, int(limit))]
    for config in configs:
        file_name = str(config.get("file_name") or "")
        path = Path(file_name)
        if not path.is_absolute():
            path = Path(pdf_corpus_dir) / file_name
        if not path.exists():
            output["audit_issues"].append(
                {
                    "severity": "error",
                    "issue_type": "pdf_missing",
                    "message": f"PDF 文件不存在：{file_name}",
                    "source_file_id": config.get("source_file_id"),
                }
            )
            continue
        try:
            extracted = extract_pdf_source(path, root, config, parsed_dir)
        except Exception as exc:  # keep Word indexing usable when one PDF fails
            output["audit_issues"].append(
                {
                    "severity": "error",
                    "issue_type": "pdf_import_failed",
                    "message": f"{path.name}: {exc}",
                    "source_file_id": config.get("source_file_id"),
                }
            )
            continue
        for key in output:
            output[key].extend(extracted.get(key, []))
    return output


def extract_pdf_source(
    path: Path,
    root: Path,
    config: Dict[str, object],
    parsed_dir: Optional[Path] = None,
) -> Dict[str, List[Dict[str, object]]]:
    started_at = datetime.now(timezone.utc).isoformat()
    source_file_id = str(config.get("source_file_id") or f"pdf-{file_sha256(path)[:12]}")
    document_id = str(config.get("document_id") or source_file_id)
    title = str(config.get("title") or path.stem)
    author = config.get("author")
    original_file_name = Path(
        str(config.get("original_file_name") or path.name)
    ).name or path.name
    structured_segments = load_mineru_segments(config, root=root)
    profile = mineru_profile(path, structured_segments) if structured_segments else detect_pdf_type(path)
    source_file = source_file_record(
        path,
        root,
        source_file_id,
        document_id,
        title,
        profile,
        original_file_name=original_file_name,
    )
    bibliographic = config.get("bibliographic_metadata") or config
    if isinstance(bibliographic, dict):
        _attach_bibliographic_metadata(source_file, bibliographic)
    volume = {
        "volume_id": document_id,
        "source_type": "pdf",
        "corpus_title": "PDF 文献",
        "display_title": title,
        "volume_number": None,
        "version_info": config.get("version_info"),
        "primary_structure": "pdf_document",
        "source_file_id": source_file_id,
    }
    work = {
        "work_id": f"{document_id}-W0001",
        "source_type": "pdf",
        "source_file_id": source_file_id,
        "volume_id": document_id,
        "parent_work_id": None,
        "work_order": 1,
        "title": title,
        "document_title": title,
        "subtitle": None,
        "author_label": author,
        "date_label": config.get("publication_year"),
        "title_source": "pdf_import_config" if config.get("title") else "file_name",
        "boundary_source": "whole_pdf",
        "toc_page_start": None,
        "toc_page_end": None,
        "confidence": 0.65 if config.get("title") else 0.45,
        "notes": "PDF MVP 将整本 PDF 作为一个文献单元。",
    }
    audit_issues: List[Dict[str, object]] = []
    if structured_segments:
        pages = load_mineru_pdf_pages(path, source_file_id, document_id, config, structured_segments)
        manual_mapping = has_manual_mapping(config)
        auto_mapping = PageMappingService().infer(
            path,
            pages,
            mineru_segments=structured_segments,
            page_count=int(profile.get("pdf_page_count") or len(pages) or 0),
            manual_mapping_present=manual_mapping,
        )
        if manual_mapping:
            auto_mapping["detected_segments"] = auto_mapping.get("selected_segments", [])
            auto_mapping["applied_segments"] = []
            auto_mapping["applied_segment_count"] = 0
            auto_mapping["method"] = "manual_override"
            auto_mapping["notes"] = ["检测到人工页码映射，自动检测结果未覆盖现有设置。"]
        else:
            apply_auto_mapping_to_pages(pages, auto_mapping)
            if auto_mapping.get("segments") and not auto_mapping.get("applied_segments"):
                audit_issues.append(
                    {
                        "severity": "warning",
                        "issue_type": "pdf_auto_page_mapping_needs_review",
                        "message": f"{original_file_name}: 自动页码映射置信度不足，未自动应用。",
                        "source_file_id": source_file_id,
                    }
                )
        profile["auto_page_mapping"] = auto_mapping
        configured_mapping = config.get("page_mapping") or {}
        profile["mapping_status"] = (
            configured_mapping.get("mapping_status")
            if configured_mapping.get("mapping_origin") == "auto"
            else auto_mapping.get("mapping_status")
        )
        profile["mapping_failure_reasons"] = auto_mapping.get("failure_reasons", [])
        source_file["pdf_profile"] = profile
        paragraphs = make_pdf_paragraphs(
            source_file_id,
            document_id,
            title,
            author,
            original_file_name,
            pages,
            work["work_id"],
        )
        for paragraph in paragraphs:
            paragraph["text_source"] = str(profile.get("parser") or "mineru")
        applied_segments = ((profile.get("auto_page_mapping") or {}).get("applied_segments") or [])
        mapping_method = "manual_segment" if manual_mapping else str(
            (profile.get("auto_page_mapping") or {}).get("method") or "uncalibrated"
        )
        pdf_page_mapping = {
            "mapping_id": f"MAP-{source_file_id}",
            "source_file_id": source_file_id,
            "document_id": document_id,
            "method": mapping_method,
            "segments": (config.get("page_mapping") or {}).get("segments", []),
            "auto_segments": applied_segments,
            "auto_page_mapping": profile.get("auto_page_mapping"),
            "confidence": max([float(p.get("page_mapping_confidence") or 0.0) for p in pages] or [0.0]),
            "validated_by": (config.get("page_mapping") or {}).get("validated_by"),
        }
        if parsed_dir:
            write_parsed_pdf_snapshot(Path(parsed_dir), document_id, profile, pages)
        return {
            "source_files": [source_file],
            "volumes": [volume],
            "works": [work],
            "paragraphs": paragraphs,
            "pdf_pages": pages,
            "pdf_page_mappings": [pdf_page_mapping],
            "pdf_import_runs": [
                import_run_record(
                    source_file_id,
                    profile,
                    started_at,
                    "success",
                    parser=str(profile.get("parser") or "mineru"),
                )
            ],
            "audit_issues": audit_issues,
        }

    if profile["detected_pdf_type"] != "native_text":
        auto_mapping = PageMappingService().infer(
            path,
            [],
            page_count=int(profile.get("pdf_page_count") or 0),
            manual_mapping_present=has_manual_mapping(config),
        )
        profile["auto_page_mapping"] = auto_mapping
        profile["mapping_status"] = auto_mapping.get("mapping_status")
        profile["mapping_failure_reasons"] = auto_mapping.get("failure_reasons", [])
        source_file["pdf_profile"] = profile
        audit_issues.append(
            {
                "severity": "warning",
                "issue_type": "pdf_needs_mineru",
                "message": f"{original_file_name} 分类为 {profile['detected_pdf_type']}，本轮不自动 OCR，需 MinerU 或人工处理。",
                "source_file_id": source_file_id,
            }
        )
        return {
            "source_files": [source_file],
            "volumes": [volume],
            "works": [work],
            "paragraphs": [],
            "pdf_pages": [],
            "pdf_page_mappings": [
                {
                    "mapping_id": f"MAP-{source_file_id}",
                    "source_file_id": source_file_id,
                    "document_id": document_id,
                    "method": auto_mapping.get("method") or "uncalibrated",
                    "segments": (config.get("page_mapping") or {}).get("segments", []),
                    "auto_segments": auto_mapping.get("applied_segments", []),
                    "auto_page_mapping": auto_mapping,
                    "confidence": max(
                        [float(item.get("mapping_confidence") or 0.0) for item in auto_mapping.get("selected_segments", [])]
                        or [0.0]
                    ),
                    "validated_by": (config.get("page_mapping") or {}).get("validated_by"),
                }
            ],
            "pdf_import_runs": [
                import_run_record(source_file_id, profile, started_at, "skipped_needs_mineru")
            ],
            "audit_issues": audit_issues,
        }

    text_pages = extract_native_pdf_pages(path)
    mapper = PageMapper.from_config(config)
    pages: List[Dict[str, object]] = []
    page_mappings: Dict[int, Tuple[Optional[str], str, float]] = {}
    for page in text_pages:
        mapping = mapper.map_page(page.pdf_page_index, page.pdf_page_label)
        page_mappings[page.pdf_page_index] = (
            mapping.citation_page,
            mapping.method,
            mapping.confidence,
        )
        pages.append(
            {
                "pdf_page_id": f"{source_file_id}-PAGE-{page.pdf_page_index:06d}",
                "source_file_id": source_file_id,
                "document_id": document_id,
                "pdf_page_index": page.pdf_page_index,
                "pdf_page_number_1based": page.pdf_page_index + 1,
                "pdf_page_label": page.pdf_page_label,
                "printed_page": mapping.citation_page,
                "printed_page_start": mapping.citation_page_start,
                "printed_page_end": mapping.citation_page_end,
                "citation_page": mapping.citation_page,
                "citation_page_start": mapping.citation_page_start,
                "citation_page_end": mapping.citation_page_end,
                "page_mapping_method": mapping.method,
                "page_mapping_confidence": mapping.confidence,
                "segment_id": mapping.segment_id,
                "layout_mode": mapping.layout_mode,
                "reading_direction": mapping.reading_direction,
                "gutter_x": mapping.gutter_x,
                "text_raw": page.raw_text,
                "page_text_hash": pdf_page_text_hash(page.raw_text),
                "normalized_text": normalize_pdf_text(page.raw_text),
                "text_source": "native_text",
                "blocks": page.blocks,
                "page_width": page.page_width,
                "page_height": page.page_height,
                "parser": page.parser,
                "parser_version": page.parser_version,
            }
        )
    manual_mapping = has_manual_mapping(config)
    auto_mapping = PageMappingService().infer(
        path,
        pages,
        page_count=int(profile.get("pdf_page_count") or len(pages) or 0),
        manual_mapping_present=manual_mapping,
    )
    if manual_mapping:
        auto_mapping["detected_segments"] = auto_mapping.get("selected_segments", [])
        auto_mapping["applied_segments"] = []
        auto_mapping["applied_segment_count"] = 0
        auto_mapping["method"] = "manual_override"
        auto_mapping["notes"] = ["检测到人工页码映射，自动检测结果未覆盖现有设置。"]
    else:
        apply_auto_mapping_to_pages(pages, auto_mapping)
    profile["auto_page_mapping"] = auto_mapping
    configured_mapping = config.get("page_mapping") or {}
    profile["mapping_status"] = (
        configured_mapping.get("mapping_status")
        if configured_mapping.get("mapping_origin") == "auto"
        else auto_mapping.get("mapping_status")
    )
    profile["mapping_failure_reasons"] = auto_mapping.get("failure_reasons", [])
    source_file["pdf_profile"] = profile
    paragraphs = make_pdf_paragraphs(
        source_file_id,
        document_id,
        title,
        author,
        original_file_name,
        pages,
        work["work_id"],
    )
    applied_segments = ((profile.get("auto_page_mapping") or {}).get("applied_segments") or [])
    pdf_page_mapping = {
        "mapping_id": f"MAP-{source_file_id}",
        "source_file_id": source_file_id,
        "document_id": document_id,
        "method": "manual_segment" if manual_mapping else str(
            (profile.get("auto_page_mapping") or {}).get("method") or "uncalibrated"
        ),
        "segments": (config.get("page_mapping") or {}).get("segments", []),
        "auto_segments": applied_segments,
        "auto_page_mapping": profile.get("auto_page_mapping"),
        "confidence": max([float(p.get("page_mapping_confidence") or 0.0) for p in pages] or [0.0]),
        "validated_by": (config.get("page_mapping") or {}).get("validated_by"),
    }
    if parsed_dir:
        write_parsed_pdf_snapshot(Path(parsed_dir), document_id, profile, pages)
    return {
        "source_files": [source_file],
        "volumes": [volume],
        "works": [work],
        "paragraphs": paragraphs,
        "pdf_pages": pages,
        "pdf_page_mappings": [pdf_page_mapping],
        "pdf_import_runs": [
            import_run_record(source_file_id, profile, started_at, "success", parser=pages[0]["parser"] if pages else None)
        ],
        "audit_issues": audit_issues,
    }


def relative_to_root(path: Path, root: Path) -> str:
    """Bookkeeping path for a source file, tolerant of files outside the root.

    The data root and the process working directory are not the same thing in
    packaged builds, so a file living elsewhere must not fail the import.
    """

    resolved_path = Path(path).resolve()
    try:
        return str(resolved_path.relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return resolved_path.as_posix()


def source_file_record(
    path: Path,
    root: Path,
    source_file_id: str,
    document_id: str,
    title: str,
    profile: Dict[str, object],
    *,
    original_file_name: Optional[str] = None,
) -> Dict[str, object]:
    display_file_name = Path(str(original_file_name or path.name)).name or path.name
    record = {
        "source_file_id": source_file_id,
        "source_type": "pdf",
        "document_id": document_id,
        "collection_id": "PDF",
        "relative_path": relative_to_root(path, root),
        "volume_number": None,
        "file_format": "pdf",
        "container_format": "pdf",
        "file_name": display_file_name,
        "display_title": title,
        "size_bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "last_modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "open_source_url": f"/source/{source_file_id}",
        "pdf_profile": profile,
    }
    if display_file_name != path.name:
        record["stored_file_name"] = path.name
    return record


def import_run_record(
    source_file_id: str,
    profile: Dict[str, object],
    started_at: str,
    status: str,
    parser: Optional[str] = None,
) -> Dict[str, object]:
    record = {
        "run_id": f"PDF-RUN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{source_file_id}",
        "source_file_id": source_file_id,
        "parser": parser or profile.get("parser") or "none",
        "parser_version": profile.get("parser_version"),
        "method": profile.get("detected_pdf_type"),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "notes": profile.get("notes", []),
    }
    if isinstance(profile.get("import_resume"), dict):
        record["import_resume"] = dict(profile["import_resume"])
    return record


def write_parsed_pdf_snapshot(
    parsed_dir: Path,
    document_id: str,
    profile: Dict[str, object],
    pages: Sequence[Dict[str, object]],
) -> None:
    parsed_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "document_id": document_id,
        "profile": profile,
        "page_count": len(pages),
        "pages": pages,
    }
    (parsed_dir / f"{document_id}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_mineru_segments(
    config: Dict[str, object],
    *,
    root: Optional[Path] = None,
) -> List[Dict[str, object]]:
    parser_results = (
        config.get("parser_results")
        or config.get("mineru")
        or config.get("mineru_results")
    )
    if not isinstance(parser_results, dict):
        return []
    segments: List[Dict[str, object]] = []
    raw_manifest_path = parser_results.get("manifest")
    if raw_manifest_path:
        manifest_path = Path(str(raw_manifest_path))
        if root is not None and not manifest_path.is_absolute():
            manifest_path = Path(root) / manifest_path
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest_resume = (
            dict(parser_results.get("resume"))
            if isinstance(parser_results.get("resume"), dict)
            else resume_summary(manifest, manifest_path=manifest_path)
        )
        for segment in manifest.get("segments", []):
            if not isinstance(segment, dict):
                continue
            item = dict(segment)
            item.setdefault("data_id", segment.get("data_id"))
            item.setdefault("result_dir", segment.get("result_dir"))
            item.setdefault("parser", manifest.get("parser") or (
                "mineru" if manifest.get("api") == "precision" else manifest.get("api")
            ))
            item.setdefault("provider_id", manifest.get("provider_id"))
            item.setdefault("provider_name", manifest.get("provider_name"))
            item.setdefault("model", manifest.get("model"))
            item.setdefault("import_resume", manifest_resume)
            raw_result_dir = str(item.get("result_dir") or "").strip()
            if root is not None and raw_result_dir:
                result_dir = Path(raw_result_dir)
                if not result_dir.is_absolute():
                    item["result_dir"] = str(Path(root) / result_dir)
            segments.append(item)
    for segment in parser_results.get("segments", []):
        if isinstance(segment, dict):
            item = dict(segment)
            item.setdefault("parser", parser_results.get("parser"))
            item.setdefault("provider_id", parser_results.get("provider_id"))
            item.setdefault("provider_name", parser_results.get("provider_name"))
            item.setdefault("model", parser_results.get("model"))
            raw_result_dir = str(item.get("result_dir") or "").strip()
            if root is not None and raw_result_dir:
                result_dir = Path(raw_result_dir)
                if not result_dir.is_absolute():
                    item["result_dir"] = str(Path(root) / result_dir)
            segments.append(item)
    return [segment for segment in segments if segment.get("result_dir")]


def mineru_profile(path: Path, segments: Sequence[Dict[str, object]]) -> Dict[str, object]:
    page_count = 0
    pymupdf = load_pymupdf()
    if pymupdf:
        doc = pymupdf.open(str(path))
        try:
            page_count = len(doc)
        finally:
            doc.close()
    covered_pages = 0
    for segment in segments:
        page_range = parse_mineru_page_range(str(segment.get("page_ranges") or ""))
        if page_range:
            covered_pages += page_range[1] - page_range[0] + 1
    first = segments[0] if segments else {}
    parser = str(first.get("parser") or "mineru")
    is_mineru = parser in {"mineru", "precision"}
    provider_name = str(first.get("provider_name") or ("MinerU" if is_mineru else "其他视觉 API"))
    model = str(first.get("model") or "")
    return {
        "detected_pdf_type": "mineru_structured" if is_mineru else "api_structured",
        "parser": "mineru" if is_mineru else parser,
        "parser_label": provider_name,
        "provider_id": first.get("provider_id"),
        "provider_name": provider_name,
        "model": model or None,
        "import_resume": first.get("import_resume"),
        "parser_version": "unknown",
        "pdf_page_count": page_count,
        "mineru_segment_count": len(segments),
        "structured_segment_count": len(segments),
        "mineru_covered_pages": covered_pages,
        "structured_covered_pages": covered_pages,
        "has_page_labels": False,
        "image_object_count": None,
        "to_unicode_map_count": None,
        "text_extractable_page_ratio": None,
        "avg_text_chars_per_page": None,
        "garbled_text_ratio": None,
        "layout_complexity_hint": "mineru_layout_available" if is_mineru else "vision_text_available",
        "notes": [
            "使用 MinerU content_list.json/layout.json 作为 PDF 索引来源。"
            if is_mineru
            else f"使用 {provider_name}{(' / ' + model) if model else ''} 的逐页视觉识别结果作为 PDF 索引来源。"
        ],
    }


def load_mineru_pdf_pages(
    path: Path,
    source_file_id: str,
    document_id: str,
    config: Dict[str, object],
    segments: Sequence[Dict[str, object]],
) -> List[Dict[str, object]]:
    mapper = PageMapper.from_config(config)
    page_texts: Dict[int, List[str]] = {}
    page_blocks: Dict[int, List[Dict[str, object]]] = {}
    parser_version = "unknown"
    parser_name = "mineru"
    provider_name = "MinerU"
    for segment in segments:
        page_range = parse_mineru_page_range(str(segment.get("page_ranges") or ""))
        if page_range is None:
            start_1based = int(segment.get("page_start") or segment.get("pdf_page_start_1based") or 1)
            end_1based = int(segment.get("page_end") or segment.get("pdf_page_end_1based") or start_1based)
        else:
            start_1based, end_1based = page_range
        page_count = end_1based - start_1based + 1
        try:
            page_index_offset = int(segment.get("page_index_offset"))
        except (TypeError, ValueError):
            page_index_offset = start_1based - 1
        result_dir = Path(str(segment["result_dir"]))
        segment_parser = str(segment.get("parser") or "mineru")
        is_mineru = segment_parser in {"mineru", "precision"}
        parser_name = "mineru" if is_mineru else segment_parser
        provider_name = str(
            segment.get("provider_name")
            or ("MinerU" if is_mineru else "其他视觉 API")
        )
        content_path = find_mineru_content_list(result_dir)
        layout_path = result_dir / "layout.json"
        if layout_path.exists():
            layout = json.loads(layout_path.read_text(encoding="utf-8-sig"))
            parser_version = str(layout.get("_version_name") or parser_version)
        content = json.loads(content_path.read_text(encoding="utf-8-sig"))
        if not isinstance(content, list):
            continue
        for local_page in range(0, page_count):
            global_index = page_index_offset + local_page
            page_texts.setdefault(global_index, [])
            page_blocks.setdefault(global_index, [])
        for item_index, item in enumerate(content):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            local_page_idx = item.get("page_idx")
            if local_page_idx is None:
                continue
            try:
                local_page_index = int(local_page_idx)
            except (TypeError, ValueError):
                continue
            if local_page_index < 0 or local_page_index >= page_count:
                continue
            global_index = page_index_offset + local_page_index
            page_texts.setdefault(global_index, []).append(text)
            page_blocks.setdefault(global_index, []).append(
                {
                    "block_index": len(page_blocks.get(global_index, [])),
                    "mineru_item_index": item_index,
                    "parser_item_index": item_index,
                    "mineru_type": item.get("type"),
                    "parser_type": item.get("type"),
                    "text_level": item.get("text_level"),
                    "bbox": item.get("bbox"),
                    "text": text,
                    "local_page_idx": local_page_index,
                    "page_index_offset": page_index_offset,
                    "pdf_page_index": global_index,
                    "result_dir": str(result_dir),
                }
            )
    detected_page_count = max(page_texts.keys()) + 1 if page_texts else 0
    labels = get_pdf_page_labels(path, detected_page_count)
    dimensions = get_pdf_page_dimensions(path, detected_page_count)
    pages: List[Dict[str, object]] = []
    for pdf_page_index in sorted(page_texts):
        raw_text = "\n".join(page_texts.get(pdf_page_index, [])).strip()
        blocks = page_blocks.get(pdf_page_index, [])
        attach_page_block_offsets(raw_text, blocks)
        label = labels[pdf_page_index] if pdf_page_index < len(labels) else None
        mapping = mapper.map_page(pdf_page_index, label)
        pages.append(
            {
                "pdf_page_id": f"{source_file_id}-PAGE-{pdf_page_index:06d}",
                "source_file_id": source_file_id,
                "document_id": document_id,
                "pdf_page_index": pdf_page_index,
                "pdf_page_number_1based": pdf_page_index + 1,
                "pdf_page_label": label,
                "printed_page": mapping.citation_page,
                "printed_page_start": mapping.citation_page_start,
                "printed_page_end": mapping.citation_page_end,
                "citation_page": mapping.citation_page,
                "citation_page_start": mapping.citation_page_start,
                "citation_page_end": mapping.citation_page_end,
                "page_mapping_method": mapping.method,
                "page_mapping_confidence": mapping.confidence,
                "segment_id": mapping.segment_id,
                "layout_mode": mapping.layout_mode,
                "reading_direction": mapping.reading_direction,
                "gutter_x": mapping.gutter_x,
                "text_raw": raw_text,
                "page_text_hash": pdf_page_text_hash(raw_text),
                "normalized_text": normalize_pdf_text(raw_text),
                "text_source": parser_name,
                "blocks": blocks,
                "page_width": dimensions[pdf_page_index][0]
                if pdf_page_index < len(dimensions)
                else None,
                "page_height": dimensions[pdf_page_index][1]
                if pdf_page_index < len(dimensions)
                else None,
                "parser": parser_name,
                "parser_label": provider_name,
                "parser_version": parser_version,
            }
        )
    return pages


def find_mineru_content_list(result_dir: Path) -> Path:
    candidates = sorted(Path(result_dir).glob("*_content_list.json"))
    if not candidates:
        direct = Path(result_dir) / "content_list.json"
        if direct.exists():
            candidates = [direct]
    if not candidates:
        raise PDFExtractionError(f"MinerU content_list.json not found: {result_dir}")
    return candidates[0]


def parse_mineru_page_range(value: str) -> Optional[Tuple[int, int]]:
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", value or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.fullmatch(r"\s*(\d+)\s*", value or "")
    if match:
        page = int(match.group(1))
        return page, page
    return None


def get_pdf_page_labels(path: Path, page_count: int) -> List[Optional[str]]:
    labels: List[Optional[str]] = [None] * max(page_count, 0)
    pymupdf = load_pymupdf()
    if not pymupdf:
        return labels
    doc = pymupdf.open(str(path))
    try:
        for index in range(min(len(doc), page_count)):
            page = doc.load_page(index)
            label = page.get_label() if getattr(page, "get_label", None) else None
            labels[index] = str(label) if label else None
    finally:
        doc.close()
    return labels


def get_pdf_page_dimensions(
    path: Path,
    page_count: int,
) -> List[Tuple[Optional[float], Optional[float]]]:
    """Return physical PDF page dimensions for native and MinerU layouts."""

    dimensions: List[Tuple[Optional[float], Optional[float]]] = [
        (None, None)
    ] * max(page_count, 0)
    pymupdf = load_pymupdf()
    if not pymupdf:
        return dimensions
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return dimensions
    try:
        for index in range(min(len(doc), page_count)):
            rect = doc.load_page(index).rect
            dimensions[index] = (float(rect.width), float(rect.height))
    finally:
        doc.close()
    return dimensions


def detect_pdf_type(path: Path, sample_pages: int = 12) -> Dict[str, object]:
    pymupdf = load_pymupdf()
    parser_name = "pymupdf" if pymupdf else "simple_pdf_text"
    parser_version = getattr(pymupdf, "VersionBind", None) if pymupdf else "builtin"
    if pymupdf:
        page_count, sample_texts, image_count, has_labels = pymupdf_probe(path, pymupdf, sample_pages)
        to_unicode_count = low_level_count(path, b"/ToUnicode")
    else:
        simple = SimplePDF(path)
        page_numbers = simple.page_object_numbers()
        page_count = len(page_numbers) or simple.declared_page_count()
        sample_texts = [page.raw_text for page in simple.extract_pages(limit=sample_pages)]
        image_count = simple.image_object_count()
        has_labels = bool(simple.page_labels(page_count))
        to_unicode_count = simple.to_unicode_count()
    char_counts = [len(t.strip()) for t in sample_texts]
    extracted_pages = sum(1 for count in char_counts if count >= 30)
    avg_chars = int(sum(char_counts) / max(len(char_counts), 1))
    sample_text = "\n".join(sample_texts)
    garbled = estimate_garbled_ratio(sample_text)
    image_density = image_count / max(page_count, 1)
    if page_count == 0:
        detected = "complex_layout"
    elif avg_chars >= 180 and garbled < 0.22:
        detected = "native_text"
    elif image_density >= 0.7 and (avg_chars < 120 or to_unicode_count == 0):
        detected = "scanned"
    elif avg_chars >= 40 and garbled >= 0.22:
        detected = "broken_text"
    elif avg_chars >= 120:
        detected = "complex_layout"
    else:
        detected = "scanned" if image_density >= 0.4 else "broken_text"
    return {
        "detected_pdf_type": detected,
        "parser": parser_name,
        "parser_version": parser_version,
        "pdf_page_count": page_count,
        "has_page_labels": has_labels,
        "image_object_count": image_count,
        "to_unicode_map_count": to_unicode_count,
        "text_extractable_page_ratio": round(extracted_pages / max(len(char_counts), 1), 4),
        "avg_text_chars_per_page": avg_chars,
        "garbled_text_ratio": round(garbled, 4),
        "layout_complexity_hint": "unknown_without_layout_engine" if not pymupdf else "pymupdf_blocks_available",
        "notes": [] if pymupdf else ["PyMuPDF 未安装；使用内置简易原生文本解析器。"],
    }


def extract_native_pdf_pages(path: Path) -> List[PDFTextPage]:
    pymupdf = load_pymupdf()
    if pymupdf:
        return extract_pages_with_pymupdf(path, pymupdf)
    return SimplePDF(path).extract_pages()


def load_pymupdf():
    try:
        import fitz  # type: ignore

        return fitz
    except Exception:
        return None


def pymupdf_probe(path: Path, fitz, sample_pages: int) -> Tuple[int, List[str], int, bool]:
    doc = fitz.open(str(path))
    texts = []
    image_count = 0
    has_labels = False
    try:
        for index in range(min(len(doc), sample_pages)):
            page = doc.load_page(index)
            texts.append(page.get_text("text") or "")
            image_count += len(page.get_images(full=True))
            if getattr(page, "get_label", None) and page.get_label():
                has_labels = True
        return len(doc), texts, image_count, has_labels
    finally:
        doc.close()


def extract_pages_with_pymupdf(path: Path, fitz) -> List[PDFTextPage]:
    doc = fitz.open(str(path))
    pages: List[PDFTextPage] = []
    try:
        for index in range(len(doc)):
            page = doc.load_page(index)
            page_rect = page.rect
            label = page.get_label() if getattr(page, "get_label", None) else None
            raw_text = page.get_text("text") or ""
            block_records = []
            for block_index, block in enumerate(page.get_text("blocks") or []):
                if len(block) < 5:
                    continue
                block_records.append(
                    {
                        "block_index": block_index,
                        "bbox": [float(block[0]), float(block[1]), float(block[2]), float(block[3])],
                        "text": block[4],
                        "bbox_normalized": [
                            float(block[0]) / max(float(page_rect.width), 1.0),
                            float(block[1]) / max(float(page_rect.height), 1.0),
                            float(block[2]) / max(float(page_rect.width), 1.0),
                            float(block[3]) / max(float(page_rect.height), 1.0),
                        ],
                    }
                )
            attach_page_block_offsets(raw_text, block_records)
            pages.append(
                PDFTextPage(
                    pdf_page_index=index,
                    pdf_page_label=str(label) if label else None,
                    raw_text=raw_text,
                    blocks=block_records,
                    parser="pymupdf",
                    parser_version=str(getattr(fitz, "VersionBind", "")),
                    page_width=float(page_rect.width),
                    page_height=float(page_rect.height),
                )
            )
    finally:
        doc.close()
    return pages


def attach_page_block_offsets(
    page_text: str,
    blocks: Sequence[Dict[str, object]],
) -> None:
    """Attach exact Unicode-codepoint intervals to layout blocks when possible.

    Layout extraction and page text extraction can disagree for malformed
    PDFs.  Unaligned blocks deliberately keep no offsets so a later search hit
    falls back to the physical page's full citation range instead of guessing
    a left/right side.
    """

    cursor = 0
    for block in blocks:
        raw_block_text = str(block.get("text") or "")
        candidates = [raw_block_text]
        stripped = raw_block_text.strip()
        if stripped and stripped != raw_block_text:
            candidates.append(stripped)
        matched_start = -1
        matched_text = ""
        for candidate in candidates:
            if not candidate:
                continue
            matched_start = page_text.find(candidate, cursor)
            if matched_start >= 0:
                matched_text = candidate
                break
        if matched_start < 0:
            continue
        matched_end = matched_start + len(matched_text)
        block["page_char_start"] = matched_start
        block["page_char_end"] = matched_end
        block["offset_unit"] = "unicode_codepoint"
        cursor = matched_end


def low_level_count(path: Path, needle: bytes) -> int:
    return path.read_bytes().count(needle)


def make_pdf_paragraphs(
    source_file_id: str,
    document_id: str,
    title: str,
    author: object,
    original_file_name: str,
    pages: Sequence[Dict[str, object]],
    work_id: object,
) -> List[Dict[str, object]]:
    paragraphs: List[Dict[str, object]] = []
    for page in pages:
        page_text = str(page.get("text_raw") or "")
        page_start, page_end = stripped_text_bounds(page_text)
        text = page_text[page_start:page_end]
        if not text:
            continue
        text_source_spans = [
            make_text_source_span(
                paragraph_text=text,
                paragraph_char_start=0,
                page=page,
                page_text=page_text,
                page_char_start=page_start,
                page_char_end=page_end,
            )
        ]
        paragraph = base_pdf_paragraph(
            source_file_id,
            document_id,
            title,
            author,
            original_file_name,
            work_id,
            text,
            int(page["pdf_page_index"]),
            int(page["pdf_page_index"]),
            page,
            page,
            paragraph_index=int(page["pdf_page_index"]) * 2,
            paragraph_id=f"{source_file_id}-P{int(page['pdf_page_index']):06d}",
            is_cross_page=False,
            text_source_spans=text_source_spans,
        )
        paragraphs.append(paragraph)
    for left, right in zip(pages, pages[1:]):
        left_page_text = str(left.get("text_raw") or "")
        right_page_text = str(right.get("text_raw") or "")
        left_body_start, left_body_end = stripped_text_bounds(left_page_text)
        right_body_text, right_body_start, right_body_end = strip_pdf_page_header_for_cross(
            right_page_text
        )
        if left_body_start == left_body_end or not right_body_text:
            continue
        left_slice_start = max(left_body_start, left_body_end - CROSS_PAGE_TAIL_CHARS)
        left_slice_end = left_body_end
        right_slice_start = right_body_start
        right_slice_end = min(right_body_end, right_body_start + CROSS_PAGE_HEAD_CHARS)
        left_slice = left_page_text[left_slice_start:left_slice_end]
        right_slice = right_page_text[right_slice_start:right_slice_end]
        cross_text = f"{left_slice}\n{right_slice}"
        if len(punctuationless_text(cross_text)) < 80:
            continue
        text_source_spans = [
            make_text_source_span(
                paragraph_text=cross_text,
                paragraph_char_start=0,
                page=left,
                page_text=left_page_text,
                page_char_start=left_slice_start,
                page_char_end=left_slice_end,
            ),
            make_text_source_span(
                paragraph_text=cross_text,
                paragraph_char_start=len(left_slice) + 1,
                page=right,
                page_text=right_page_text,
                page_char_start=right_slice_start,
                page_char_end=right_slice_end,
            ),
        ]
        paragraph = base_pdf_paragraph(
            source_file_id,
            document_id,
            title,
            author,
            original_file_name,
            work_id,
            cross_text,
            int(left["pdf_page_index"]),
            int(right["pdf_page_index"]),
            left,
            right,
            paragraph_index=int(left["pdf_page_index"]) * 2 + 1,
            paragraph_id=f"{source_file_id}-CROSS-{int(left['pdf_page_index']):06d}-{int(right['pdf_page_index']):06d}",
            is_cross_page=True,
            text_source_spans=text_source_spans,
        )
        paragraphs.append(paragraph)
    return paragraphs


def stripped_text_bounds(text: str) -> Tuple[int, int]:
    """Return the half-open bounds used by ``str.strip()`` without rebuilding text."""

    value = text or ""
    without_leading = value.lstrip()
    if not without_leading:
        return len(value), len(value)
    start = len(value) - len(without_leading)
    end = len(value.rstrip())
    return start, max(start, end)


def make_text_source_span(
    *,
    paragraph_text: str,
    paragraph_char_start: int,
    page: Mapping[str, object],
    page_text: str,
    page_char_start: int,
    page_char_end: int,
) -> Dict[str, object]:
    """Build and validate one Unicode-codepoint source interval."""

    length = page_char_end - page_char_start
    paragraph_char_end = paragraph_char_start + length
    if (
        paragraph_char_start < 0
        or page_char_start < 0
        or length <= 0
        or paragraph_char_end > len(paragraph_text)
        or page_char_end > len(page_text)
        or paragraph_text[paragraph_char_start:paragraph_char_end]
        != page_text[page_char_start:page_char_end]
    ):
        raise PDFExtractionError("PDF paragraph text_source_spans invariant failed")
    return {
        "paragraph_char_start": paragraph_char_start,
        "paragraph_char_end": paragraph_char_end,
        "offset_unit": "unicode_codepoint",
        "pdf_page_id": str(page["pdf_page_id"]),
        "pdf_page_index": int(page["pdf_page_index"]),
        "page_text_hash": str(page.get("page_text_hash") or pdf_page_text_hash(page_text)),
        "page_char_start": page_char_start,
        "page_char_end": page_char_end,
    }


def base_pdf_paragraph(
    source_file_id: str,
    document_id: str,
    title: str,
    author: object,
    original_file_name: str,
    work_id: object,
    text: str,
    start_index: int,
    end_index: int,
    start_page: Dict[str, object],
    end_page: Dict[str, object],
    paragraph_index: int,
    paragraph_id: str,
    is_cross_page: bool,
    text_source_spans: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    start_citation = start_page.get("citation_page_start") or start_page.get("citation_page")
    end_citation = end_page.get("citation_page_end") or end_page.get("citation_page")
    calibrated = bool(start_citation and end_citation)
    method = (
        start_page.get("page_mapping_method")
        if start_page.get("page_mapping_method") == end_page.get("page_mapping_method")
        else "mixed"
    )
    confidence = min(
        float(start_page.get("page_mapping_confidence") or 0.0),
        float(end_page.get("page_mapping_confidence") or 0.0),
    )
    start_segment = start_page.get("segment_id")
    end_segment = end_page.get("segment_id")
    segment_id = start_segment if start_segment == end_segment else None
    start_scope = start_page.get("page_scope")
    end_scope = end_page.get("page_scope")
    page_scope = start_scope if start_scope == end_scope else "mixed" if start_scope or end_scope else None
    start_evidence = start_page.get("mapping_evidence")
    end_evidence = end_page.get("mapping_evidence")
    mapping_evidence = start_evidence if start_evidence == end_evidence else {
        "start_page": start_evidence,
        "end_page": end_evidence,
    } if start_evidence or end_evidence else None
    record: Dict[str, object] = {
        "paragraph_id": paragraph_id,
        "source_type": "pdf",
        "source_file_id": source_file_id,
        "document_id": document_id,
        "volume_id": document_id,
        "volume_number": None,
        "volume_display": title,
        "work_id": work_id,
        "work_title": title,
        "document_title": title,
        "author_label": author,
        "paragraph_index": paragraph_index,
        "source_order": paragraph_index,
        "section_index": None,
        "text_raw": text,
        "style_name": None,
        "alignment": None,
        "font_summary": None,
        "is_title_candidate": False,
        "is_toc_entry": False,
        "is_index_entry": False,
        "original_page_start": str(start_citation) if calibrated else None,
        "original_page_end": str(end_citation) if calibrated else None,
        "page_source_type": str(method or "uncalibrated"),
        "page_confidence": confidence if calibrated else 0.0,
        "page_display": mapped_page_display(
            start_index,
            end_index,
            str(start_citation) if calibrated else None,
            str(end_citation) if calibrated else None,
        ),
        "original_file_name": original_file_name,
        "eligible_for_search": len(punctuationless_text(text)) >= 20,
        "pdf_page_start_index": start_index,
        "pdf_page_end_index": end_index,
        "pdf_page_start_label": start_page.get("pdf_page_label"),
        "pdf_page_end_label": end_page.get("pdf_page_label"),
        "printed_page_start": start_page.get("printed_page_start") or start_page.get("printed_page"),
        "printed_page_end": end_page.get("printed_page_end") or end_page.get("printed_page"),
        "citation_page_start": str(start_citation) if calibrated else None,
        "citation_page_end": str(end_citation) if calibrated else None,
        "citation_page_number_start": start_page.get(
            "citation_page_number_start", start_page.get("citation_page_number")
        ),
        "citation_page_number_end": end_page.get(
            "citation_page_number_end", end_page.get("citation_page_number")
        ),
        "citation_page_label_start": start_page.get(
            "citation_page_label_start", start_page.get("citation_page_label")
        ),
        "citation_page_label_end": end_page.get(
            "citation_page_label_end", end_page.get("citation_page_label")
        ),
        "page_scope": page_scope,
        "page_mapping_method": str(method or "uncalibrated"),
        "page_mapping_confidence": confidence if calibrated else 0.0,
        "mapping_method": str(method or "uncalibrated"),
        "mapping_confidence": confidence if calibrated else 0.0,
        "mapping_confidence_level": start_page.get("mapping_confidence_level")
        if start_page.get("mapping_confidence_level") == end_page.get("mapping_confidence_level")
        else "mixed" if start_page.get("mapping_confidence_level") or end_page.get("mapping_confidence_level") else None,
        "mapping_evidence": mapping_evidence,
        "segment_id": segment_id,
        "layout_mode": start_page.get("layout_mode")
        if start_page.get("layout_mode") == end_page.get("layout_mode")
        else "mixed",
        "reading_direction": start_page.get("reading_direction")
        if start_page.get("reading_direction") == end_page.get("reading_direction")
        else "mixed",
        "gutter_x": start_page.get("gutter_x")
        if start_page.get("gutter_x") == end_page.get("gutter_x")
        else None,
        "is_cross_page": is_cross_page,
        "text_source_spans": [dict(span) for span in text_source_spans],
        "text_source": "native_text",
        "block_index": None,
        "bbox": None,
        "bbox_refs": None,
        "mineru_block_ids": None,
        "open_source_url": f"/source/{source_file_id}#page={start_index + 1}",
    }
    enrich_pdf_paragraph_text(record)
    return record


def enrich_pdf_paragraph_text(record: Dict[str, object]) -> None:
    text = str(record.get("text_raw") or "")
    record["normalized_text"] = normalize_pdf_text(text)
    record["compact_text"] = compact_text(text)
    record["plain_text"] = punctuationless_text(text)
    record["sentences"] = split_sentences(text)
    record["text_hash"] = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def pdf_page_text_hash(text: str) -> str:
    """Return the stable short hash used to validate page-local deep links."""

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def strip_pdf_page_header_for_cross(text: str) -> Tuple[str, int, int]:
    """Return a header-free continuous slice and its bounds in the original page.

    ``splitlines(keepends=True)`` is used only to locate the first retained
    character.  The returned text is sliced directly from ``text`` so CRLF and
    Unicode line separators remain byte-for-byte/character-for-character intact.
    """

    page_text = text or ""
    body_start, body_end = stripped_text_bounds(page_text)
    if body_start == body_end:
        return "", body_end, body_end

    lines = page_text[body_start:body_end].splitlines(keepends=True)
    retained_start = body_start
    for line in lines[:8]:
        value = line.strip()
        if not value:
            retained_start += len(line)
            continue
        compact = re.sub(r"\s+", "", value)
        alpha = [ch for ch in compact if ch.isalpha()]
        uppercase_ratio = (
            sum(1 for ch in alpha if ch.upper() == ch and ch.lower() != ch) / max(len(alpha), 1)
            if alpha
            else 0.0
        )
        if re.fullmatch(r"\d{1,4}|[ivxlcdmIVXLCDM]{1,8}", compact):
            retained_start += len(line)
            continue
        if len(compact) <= 40 and alpha and uppercase_ratio >= 0.75:
            retained_start += len(line)
            continue
        break
    while retained_start < body_end and page_text[retained_start].isspace():
        retained_start += 1
    return page_text[retained_start:body_end], retained_start, body_end


def estimate_garbled_ratio(text: str) -> float:
    text = text or ""
    if not text.strip():
        return 1.0
    considered = 0
    bad = 0.0
    for ch in text:
        if ch.isspace():
            continue
        considered += 1
        code = ord(ch)
        category = unicodedata.category(ch)
        if ch == "\ufffd" or category.startswith("C"):
            bad += 1
        elif 0x4E00 <= code <= 0x9FFF:
            continue
        elif 0x20 <= code <= 0x7E:
            continue
        elif ch in "，。；：？！、“”‘’《》（）【】—…·–-":
            continue
        elif ch in "éèêáàäöüßñçÉÈÊÁÀÄÖÜÑÇ©®™":
            continue
        else:
            bad += 0.45
    return min(1.0, bad / max(considered, 1))


class SimplePDF:
    """A deliberately small parser for simple native-text PDFs.

    It is not a replacement for PyMuPDF. It exists so the local MVP can still
    classify PDFs and validate a small native-text sample when no PDF package is
    installed in the current Python environment.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.objects = self._parse_objects()

    def _parse_objects(self) -> Dict[int, bytes]:
        objects: Dict[int, bytes] = {}
        pattern = re.compile(rb"(\d+)\s+(\d+)\s+obj\s*(.*?)\s*endobj", re.S)
        for match in pattern.finditer(self.data):
            objects[int(match.group(1))] = match.group(3)
        return objects

    def declared_page_count(self) -> int:
        counts = []
        for body in self.objects.values():
            if re.search(rb"/Type\s*/Pages\b|/Type/Pages\b", body[:500]):
                match = re.search(rb"/Count\s+(\d+)", body[:500])
                if match:
                    counts.append(int(match.group(1)))
        return max(counts or [0])

    def image_object_count(self) -> int:
        return sum(
            1
            for body in self.objects.values()
            if b"/Subtype/Image" in body.replace(b" ", b"") or b"/Subtype /Image" in body
        )

    def to_unicode_count(self) -> int:
        return sum(1 for body in self.objects.values() if b"/ToUnicode" in body)

    def page_object_numbers(self) -> List[int]:
        root = self._catalog_pages_ref()
        pages: List[int] = []
        if root is not None:
            self._walk_pages(root, pages, set())
        if pages:
            return pages
        for obj_num, body in self.objects.items():
            if self._is_page(body):
                pages.append(obj_num)
        return sorted(pages)

    def page_labels(self, page_count: int) -> List[Optional[str]]:
        labels: List[Optional[str]] = [None] * max(page_count, 0)
        catalog = self._catalog_body()
        if not catalog:
            return labels
        match = re.search(rb"/PageLabels\s+(\d+)\s+\d+\s+R", catalog)
        if not match:
            return labels
        label_obj = self.objects.get(int(match.group(1)), b"")
        nums_match = re.search(rb"/Nums\s*\[(.*?)\]", label_obj, re.S)
        if not nums_match:
            return labels
        entries = self._parse_label_entries(nums_match.group(1))
        for idx, (start_page, spec) in enumerate(entries):
            end_page = entries[idx + 1][0] if idx + 1 < len(entries) else page_count
            style = self._name_value(spec, b"S")
            prefix = self._literal_value(spec, b"P") or ""
            start_num = self._int_value(spec, b"St") or 1
            for page_index in range(start_page, min(end_page, page_count)):
                number = start_num + (page_index - start_page)
                if style == "D":
                    labels[page_index] = f"{prefix}{number}"
                elif style == "r":
                    from .pdf_page_mapping import int_to_roman

                    labels[page_index] = f"{prefix}{int_to_roman(number, upper=False)}"
                elif style == "R":
                    from .pdf_page_mapping import int_to_roman

                    labels[page_index] = f"{prefix}{int_to_roman(number, upper=True)}"
                else:
                    labels[page_index] = prefix or None
        return labels

    def extract_pages(self, limit: Optional[int] = None) -> List[PDFTextPage]:
        page_objects = self.page_object_numbers()
        if limit is not None:
            page_objects = page_objects[:limit]
        all_labels = self.page_labels(len(self.page_object_numbers()))
        pages: List[PDFTextPage] = []
        for index, page_obj in enumerate(page_objects):
            content = self._page_content(page_obj)
            raw_text = extract_text_from_pdf_content(content)
            pages.append(
                PDFTextPage(
                    pdf_page_index=index,
                    pdf_page_label=all_labels[index] if index < len(all_labels) else None,
                    raw_text=raw_text,
                    blocks=[
                        {
                            "block_index": 0,
                            "bbox": None,
                            "text": raw_text,
                            "page_idx": index,
                            "page_char_start": 0,
                            "page_char_end": len(raw_text),
                            "offset_unit": "unicode_codepoint",
                        }
                    ]
                    if raw_text
                    else [],
                    parser="simple_pdf_text",
                    parser_version="builtin",
                )
            )
        return pages

    def _catalog_body(self) -> Optional[bytes]:
        for body in self.objects.values():
            compact = body[:1000].replace(b" ", b"")
            if b"/Type/Catalog" in compact:
                return body
        return None

    def _catalog_pages_ref(self) -> Optional[int]:
        catalog = self._catalog_body()
        if not catalog:
            return None
        match = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", catalog)
        return int(match.group(1)) if match else None

    def _walk_pages(self, obj_num: int, pages: List[int], seen: set[int]) -> None:
        if obj_num in seen:
            return
        seen.add(obj_num)
        body = self.objects.get(obj_num, b"")
        if self._is_page(body):
            pages.append(obj_num)
            return
        kids_match = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
        if not kids_match:
            return
        for ref in indirect_refs(kids_match.group(1)):
            self._walk_pages(ref, pages, seen)

    def _is_page(self, body: bytes) -> bool:
        compact = body[:1200].replace(b" ", b"")
        return b"/Type/Page" in compact and b"/Type/Pages" not in compact

    def _page_content(self, page_obj: int) -> bytes:
        body = self.objects.get(page_obj, b"")
        refs: List[int] = []
        direct = re.search(rb"/Contents\s+(\d+)\s+\d+\s+R", body)
        if direct:
            refs.append(int(direct.group(1)))
        array = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
        if array:
            refs.extend(indirect_refs(array.group(1)))
        chunks: List[bytes] = []
        for ref in refs:
            chunks.append(self._stream_data(ref))
        return b"\n".join(chunk for chunk in chunks if chunk)

    def _stream_data(self, obj_num: int) -> bytes:
        body = self.objects.get(obj_num, b"")
        match = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.S)
        if not match:
            return b""
        stream = match.group(1)
        dictionary = body[: match.start()]
        if b"FlateDecode" in dictionary:
            try:
                stream = zlib.decompress(stream)
            except Exception:
                return b""
        return stream

    def _parse_label_entries(self, data: bytes) -> List[Tuple[int, bytes]]:
        entries: List[Tuple[int, bytes]] = []
        pattern = re.compile(rb"(\d+)\s+((?:\d+\s+\d+\s+R)|(?:<<.*?>>))", re.S)
        for match in pattern.finditer(data):
            page_index = int(match.group(1))
            spec_token = match.group(2)
            ref = re.match(rb"(\d+)\s+\d+\s+R", spec_token)
            spec = self.objects.get(int(ref.group(1)), b"") if ref else spec_token
            entries.append((page_index, spec))
        return sorted(entries)

    def _name_value(self, spec: bytes, key: bytes) -> Optional[str]:
        match = re.search(rb"/" + re.escape(key) + rb"\s*/([A-Za-z]+)", spec)
        return match.group(1).decode("ascii", "ignore") if match else None

    def _literal_value(self, spec: bytes, key: bytes) -> Optional[str]:
        match = re.search(rb"/" + re.escape(key) + rb"\s*\((.*?)\)", spec, re.S)
        return pdf_literal_to_text(match.group(1)) if match else None

    def _int_value(self, spec: bytes, key: bytes) -> Optional[int]:
        match = re.search(rb"/" + re.escape(key) + rb"\s+(\d+)", spec)
        return int(match.group(1)) if match else None


def indirect_refs(data: bytes) -> List[int]:
    return [int(match.group(1)) for match in re.finditer(rb"(\d+)\s+\d+\s+R", data)]


def extract_text_from_pdf_content(content: bytes) -> str:
    if not content:
        return ""
    texts: List[str] = []
    token_pattern = re.compile(
        rb"(\[(?:[^\[\]]|\((?:\\.|[^\\)])*\)|<[^<>]*>)*\]\s*TJ|"
        rb"\((?:\\.|[^\\)])*\)\s*Tj|"
        rb"<[0-9A-Fa-f\s]+>\s*Tj|"
        rb"\((?:\\.|[^\\)])*\)\s*'|"
        rb"\((?:\\.|[^\\)])*\)\s*\")",
        re.S,
    )
    for match in token_pattern.finditer(content):
        token = match.group(0)
        stripped = token.strip()
        if stripped.endswith(b"TJ"):
            text = "".join(pdf_string_tokens_to_text(token))
        else:
            strings = pdf_string_tokens_to_text(token)
            text = strings[0] if strings else ""
        text = cleanup_pdf_text_piece(text)
        if text:
            texts.append(text)
    return "\n".join(texts)


def pdf_string_tokens_to_text(data: bytes) -> List[str]:
    out: List[str] = []
    pos = 0
    while pos < len(data):
        if data[pos : pos + 1] == b"(":
            end = find_literal_end(data, pos)
            if end is None:
                break
            out.append(pdf_literal_to_text(data[pos + 1 : end]))
            pos = end + 1
            continue
        if data[pos : pos + 1] == b"<" and data[pos : pos + 2] != b"<<":
            end = data.find(b">", pos + 1)
            if end < 0:
                break
            out.append(pdf_hex_to_text(data[pos + 1 : end]))
            pos = end + 1
            continue
        pos += 1
    return out


def find_literal_end(data: bytes, start: int) -> Optional[int]:
    depth = 1
    pos = start + 1
    while pos < len(data):
        ch = data[pos]
        if ch == 92:
            pos += 2
            continue
        if ch == 40:
            depth += 1
        elif ch == 41:
            depth -= 1
            if depth == 0:
                return pos
        pos += 1
    return None


def pdf_literal_to_text(data: bytes) -> str:
    out = bytearray()
    pos = 0
    while pos < len(data):
        ch = data[pos]
        if ch == 92 and pos + 1 < len(data):
            pos += 1
            esc = data[pos]
            escape_map = {
                ord("n"): 10,
                ord("r"): 13,
                ord("t"): 9,
                ord("b"): 8,
                ord("f"): 12,
                ord("("): 40,
                ord(")"): 41,
                ord("\\"): 92,
            }
            if esc in escape_map:
                out.append(escape_map[esc])
                pos += 1
                continue
            if 48 <= esc <= 55:
                octal = bytes([esc])
                pos += 1
                while pos < len(data) and len(octal) < 3 and 48 <= data[pos] <= 55:
                    octal += bytes([data[pos]])
                    pos += 1
                out.append(int(octal, 8) & 0xFF)
                continue
            if esc in (10, 13):
                if esc == 13 and pos + 1 < len(data) and data[pos + 1] == 10:
                    pos += 1
                pos += 1
                continue
            out.append(esc)
            pos += 1
            continue
        out.append(ch)
        pos += 1
    raw = bytes(out)
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    return raw.decode("cp1252", "replace")


def pdf_hex_to_text(data: bytes) -> str:
    clean = re.sub(rb"\s+", b"", data)
    if len(clean) % 2:
        clean += b"0"
    try:
        raw = bytes.fromhex(clean.decode("ascii"))
    except ValueError:
        return ""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    return raw.decode("cp1252", "replace")


def cleanup_pdf_text_piece(text: str) -> str:
    text = text.replace("\x00", "")
    text = "".join(ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C"))
    return text.strip()
