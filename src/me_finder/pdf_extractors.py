"""Coordinate PDF imports, parser cache loading and printed-page mapping."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .auto_page_mapping import (
    apply_auto_mapping_to_pages,
    has_manual_mapping,
)
from .bibliographic.metadata import METADATA_FIELDS
from .document_heading import enrich_pdf_headings
from .import_resume import resume_summary
from .normalization import (
    normalize_pdf_text,
)
from .page_mapping_service import PageMappingService
from .pdf_page_mapping import PageMapper
from .pdf_page_split import mineru_layout_paths, split_parser_merged_pages

# Preserve existing import entry points; implementations have a single owner.
from . import pdf_native
from .pdf_extraction_types import (
    PDFExtractionError as PDFExtractionError,
    PDFTextPage as PDFTextPage,
)
from .pdf_paragraphs import (
    CROSS_PAGE_TAIL_CHARS as CROSS_PAGE_TAIL_CHARS,
    CROSS_PAGE_HEAD_CHARS as CROSS_PAGE_HEAD_CHARS,
    PAGE_DECORATION_BLOCK_TYPES as PAGE_DECORATION_BLOCK_TYPES,
    attach_page_block_offsets as attach_page_block_offsets,
    make_pdf_paragraphs as make_pdf_paragraphs,
    stripped_text_bounds as stripped_text_bounds,
    make_text_source_span as make_text_source_span,
    base_pdf_paragraph as base_pdf_paragraph,
    enrich_pdf_paragraph_text as enrich_pdf_paragraph_text,
    pdf_page_text_hash as pdf_page_text_hash,
    page_body_bounds as page_body_bounds,
    strip_pdf_page_header_for_cross as strip_pdf_page_header_for_cross,
)
from .pdf_native import (
    SIMPLE_PDF_MAX_BYTES as SIMPLE_PDF_MAX_BYTES,
    get_pdf_page_labels as get_pdf_page_labels,
    get_pdf_page_dimensions as get_pdf_page_dimensions,
    detect_pdf_type as detect_pdf_type,
    extract_native_pdf_pages as extract_native_pdf_pages,
    load_pymupdf as load_pymupdf,
    pymupdf_probe as pymupdf_probe,
    extract_pages_with_pymupdf as extract_pages_with_pymupdf,
    low_level_count as low_level_count,
    estimate_garbled_ratio as estimate_garbled_ratio,
    SimplePDF as SimplePDF,
    indirect_refs as indirect_refs,
    extract_text_from_pdf_content as extract_text_from_pdf_content,
    pdf_string_tokens_to_text as pdf_string_tokens_to_text,
    find_literal_end as find_literal_end,
    pdf_literal_to_text as pdf_literal_to_text,
    pdf_hex_to_text as pdf_hex_to_text,
    cleanup_pdf_text_piece as cleanup_pdf_text_piece,
)


PDF_TYPES = {"native_text", "scanned", "broken_text", "complex_layout"}

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
        pages = load_mineru_pdf_pages(
            path, source_file_id, document_id, config, structured_segments, root=root
        )
        source_file["pdf_outline"] = enrich_pdf_headings(
            pages, path, structured_segments, root=root
        )
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
    target = parsed_dir / f"{document_id}.json"
    partial = target.with_name(target.name + ".partial")
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    with partial.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("{")
        stream.write('"document_id":')
        for chunk in encoder.iterencode(document_id):
            stream.write(chunk)
        stream.write(',"profile":')
        for chunk in encoder.iterencode(profile):
            stream.write(chunk)
        stream.write(f',"page_count":{len(pages)},"pages":[')
        for index, page in enumerate(pages):
            if index:
                stream.write(",")
            for chunk in encoder.iterencode(page):
                stream.write(chunk)
        stream.write("]}\n")
        stream.flush()
        import os

        os.fsync(stream.fileno())
    partial.replace(target)


def runtime_corpus_path(value: str, root: Optional[Path]) -> Optional[str]:
    """Resolve a recorded parser path against the current data root.

    Manifests keep the absolute path of the machine and data root that wrote
    them (``D:\\ME_Finder\\…``, an earlier OneDrive folder).  When that path
    is not readable here, the part from ``corpus/processed/`` on is looked up
    under ``root``; relative paths resolve against ``root`` as before.
    ``None`` when ``value`` is empty.
    """

    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if root is not None and not path.is_absolute():
        path = Path(root) / path
    if path.exists() or root is None:
        return str(path)
    marker = "corpus/processed/"
    portable = text.replace("\\", "/")
    if marker in portable:
        rebased = Path(root) / "corpus" / "processed" / portable.split(marker, 1)[1]
        if rebased.exists():
            return str(rebased)
    return str(path)


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
        manifest_path = Path(
            runtime_corpus_path(str(raw_manifest_path), root) or str(raw_manifest_path)
        )
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
            item.setdefault("document_job_id", manifest.get("document_job_id"))
            item.setdefault("import_resume", manifest_resume)
            raw_result_dir = str(item.get("result_dir") or "").strip()
            if raw_result_dir:
                item["result_dir"] = runtime_corpus_path(raw_result_dir, root) or raw_result_dir
            segments.append(item)
    for segment in parser_results.get("segments", []):
        if isinstance(segment, dict):
            item = dict(segment)
            item.setdefault("parser", parser_results.get("parser"))
            item.setdefault("provider_id", parser_results.get("provider_id"))
            item.setdefault("provider_name", parser_results.get("provider_name"))
            item.setdefault("model", parser_results.get("model"))
            raw_result_dir = str(item.get("result_dir") or "").strip()
            if raw_result_dir:
                item["result_dir"] = runtime_corpus_path(raw_result_dir, root) or raw_result_dir
            segments.append(item)
    return [segment for segment in segments if segment.get("result_dir")]


def mineru_profile(path: Path, segments: Sequence[Dict[str, object]]) -> Dict[str, object]:
    page_count = 0
    pymupdf = pdf_native.load_pymupdf()
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
    root: Optional[Path] = None,
) -> List[Dict[str, object]]:
    mapper = PageMapper.from_config(config)
    page_texts: Dict[int, List[str]] = {}
    page_blocks: Dict[int, List[Dict[str, object]]] = {}
    parser_version = "unknown"
    parser_name = "mineru"
    provider_name = "MinerU"
    segment_pages: Dict[str, range] = {}
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
        segment_pages[str(segment["result_dir"]).strip()] = range(
            page_index_offset, page_index_offset + page_count
        )
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
                    **(
                        {"bbox_normalized": item["bbox_normalized"]}
                        if isinstance(item.get("bbox_normalized"), list)
                        and len(item["bbox_normalized"]) == 4
                        else {}
                    ),
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
    if parser_name == "mineru":
        layout_paths = mineru_layout_paths(segments, root)
        _, changed = split_parser_merged_pages(
            pages,
            layout_paths,
            checked_pages={
                index
                for key in layout_paths
                for index in segment_pages.get(key, ())
            },
        )
        for page in pages:
            if int(page["pdf_page_index"]) not in changed:
                continue
            raw_text = str(page["text_raw"])
            for block in page["blocks"]:
                for key in ("page_char_start", "page_char_end", "offset_unit"):
                    block.pop(key, None)
            attach_page_block_offsets(raw_text, page["blocks"])
            page["page_text_hash"] = pdf_page_text_hash(raw_text)
            page["normalized_text"] = normalize_pdf_text(raw_text)
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
