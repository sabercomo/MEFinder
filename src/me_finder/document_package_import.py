"""Restore a versioned MEFinder document package into index records."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .bibliographic_metadata import METADATA_FIELDS
from .document_export import (
    DocumentExportError,
    ExportedDocument,
    normalize_export_page,
    read_document_export,
)
from .normalization import normalize_pdf_text
from .pdf_extractors import (
    import_run_record,
    make_pdf_paragraphs,
    pdf_page_text_hash,
    relative_to_root,
)


_ROMAN_RE = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)


class DocumentPackageImportError(ValueError):
    """The selected file is not a valid MEFinder document package."""


@dataclass(frozen=True)
class DocumentPackage:
    pages: Sequence[Dict[str, object]]
    title: str
    bibliographic_metadata: Dict[str, object]
    parser_provider: str
    parser_model: Optional[str]
    parser_version: Optional[str]
    parsed_at: Optional[str]
    source_sha256: str
    source_file_name: str
    source_size_bytes: Optional[int]
    source_last_modified: Optional[str]
    warnings: Sequence[object]
    missing_ranges: Sequence[object]


def read_document_package(path: Path) -> DocumentPackage:
    """Validate and materialize one ``mefinder.document.v1`` package."""

    source = Path(path)
    if not source.name.lower().endswith(".mefinder.zip"):
        raise DocumentPackageImportError("文档包文件名必须以 .mefinder.zip 结尾。")
    if not zipfile.is_zipfile(source):
        raise DocumentPackageImportError("MEFinder 文档包不是有效的 ZIP 文件。")
    try:
        exported = read_document_export(source)
    except (DocumentExportError, OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise DocumentPackageImportError(f"MEFinder 文档包格式无效：{exc}") from exc
    return _from_export(exported)


def _from_export(exported: ExportedDocument) -> DocumentPackage:
    manifest = exported.manifest
    document = manifest.get("document")
    document = document if isinstance(document, Mapping) else {}
    source_file = manifest.get("source_file")
    source_file = source_file if isinstance(source_file, Mapping) else {}
    parser = manifest.get("parser")
    parser = parser if isinstance(parser, Mapping) else {}
    metadata = manifest.get("bibliographic_metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    external_ids = manifest.get("external_ids")
    if isinstance(external_ids, Mapping):
        for key in ("isbn", "doi", "issn"):
            if external_ids.get(key) not in (None, ""):
                metadata.setdefault(key, external_ids[key])
    title = str(
        metadata.get("title")
        or document.get("title")
        or Path(str(source_file.get("file_name") or "未命名文献")).stem
    ).strip()
    pages = [normalize_export_page(page) for page in exported.pages]
    if not pages or not any(str(page.get("text") or "").strip() for page in pages):
        raise DocumentPackageImportError("文档包没有可导入的页级文本。")
    digest = str(manifest.get("source_sha256") or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise DocumentPackageImportError("文档包的 source_sha256 无效。")
    raw_size = source_file.get("size_bytes")
    try:
        source_size = int(raw_size) if raw_size not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise DocumentPackageImportError("文档包记录的原 PDF 大小无效。") from exc
    return DocumentPackage(
        pages=pages,
        title=title or "未命名文献",
        bibliographic_metadata=metadata,
        parser_provider=str(parser.get("provider") or "mefinder-document-import"),
        parser_model=_optional_text(parser.get("model")),
        parser_version=_optional_text(parser.get("version")),
        parsed_at=_optional_text(manifest.get("parsed_at")),
        source_sha256=digest,
        source_file_name=Path(str(source_file.get("file_name") or "原文.pdf")).name,
        source_size_bytes=source_size,
        source_last_modified=_optional_text(source_file.get("last_modified")),
        warnings=list(manifest.get("warnings") or []),
        missing_ranges=list(manifest.get("missing_ranges") or []),
    )


def build_document_package_records(
    package: DocumentPackage,
    *,
    package_path: Path,
    source_file_id: str,
    document_id: str,
    runtime_root: Path,
    source_path: Optional[Path] = None,
) -> Tuple[Dict[str, List[Dict[str, object]]], List[Dict[str, object]]]:
    """Build the one-source payload consumed by ``replace_source``."""

    metadata = dict(package.bibliographic_metadata)
    title = str(metadata.get("title") or package.title).strip()
    author = metadata.get("author")
    now = datetime.now(timezone.utc).isoformat()
    mapping_segments = mapping_segments_from_pages(package.pages)
    mapping_status = "manual_mapped" if mapping_segments else "unmapped"
    profile = {
        "detected_pdf_type": "api_structured",
        "parser": package.parser_provider,
        "parser_label": package.parser_provider,
        "provider_name": package.parser_provider,
        "model": package.parser_model,
        "parser_version": package.parser_version,
        "pdf_page_count": max(
            int(page["physical_pdf_page"]) for page in package.pages
        ),
        "mapping_status": mapping_status,
        "notes": ["从 MEFinder 文档包恢复索引，未重新运行 OCR。"],
        "auto_page_mapping": {
            "method": "document_package",
            "mapping_status": mapping_status,
            "applied_segments": mapping_segments,
            "selected_segments": mapping_segments,
            "failure_reasons": [],
        },
    }
    source: Dict[str, object] = {
        "source_file_id": source_file_id,
        "source_type": "pdf",
        "document_id": document_id,
        "collection_id": "PDF",
        "relative_path": (
            relative_to_root(source_path, runtime_root)
            if source_path is not None
            else ""
        ),
        "volume_number": None,
        "file_format": "pdf",
        "container_format": "pdf",
        "file_name": package.source_file_name,
        "display_title": title,
        "size_bytes": (
            source_path.stat().st_size
            if source_path is not None
            else package.source_size_bytes or 0
        ),
        "sha256": package.source_sha256,
        "last_modified": package.source_last_modified or now,
        "imported_at": now,
        "pdf_profile": profile,
        "imported_document_package": Path(package_path).name,
    }
    _attach_metadata(source, metadata)
    volume = {
        "volume_id": document_id,
        "source_type": "pdf",
        "corpus_title": "PDF 文献",
        "display_title": title,
        "volume_number": None,
        "primary_structure": "pdf_document",
        "source_file_id": source_file_id,
    }
    work_id = f"{document_id}-W0001"
    work = {
        "work_id": work_id,
        "source_type": "pdf",
        "source_file_id": source_file_id,
        "volume_id": document_id,
        "parent_work_id": None,
        "work_order": 1,
        "title": title,
        "document_title": title,
        "subtitle": None,
        "author_label": author,
        "date_label": metadata.get("publish_year"),
        "title_source": "imported_metadata" if metadata.get("title") else "file_name",
        "boundary_source": "whole_document",
        "confidence": 1.0,
    }
    pages = _index_pages(package, source_file_id, document_id)
    paragraphs = make_pdf_paragraphs(
        source_file_id,
        document_id,
        title,
        author,
        package.source_file_name,
        pages,
        work_id,
    )
    for paragraph in paragraphs:
        paragraph["eligible_for_search"] = bool(paragraph.get("plain_text"))
        paragraph["text_source"] = package.parser_provider
        if source_path is None:
            paragraph.pop("open_source_url", None)
    mapping = {
        "mapping_id": f"MAP-{source_file_id}",
        "source_file_id": source_file_id,
        "document_id": document_id,
        "method": "document_package",
        "segments": mapping_segments,
        "auto_segments": mapping_segments,
        "auto_page_mapping": profile["auto_page_mapping"],
        "confidence": 1.0 if mapping_segments else 0.0,
        "validated_by": "document_package",
    }
    return {
        "source_files": [source],
        "volumes": [volume],
        "works": [work],
        "paragraphs": paragraphs,
        "pdf_pages": pages,
        "pdf_page_mappings": [mapping],
        "pdf_import_runs": [
            import_run_record(
                source_file_id,
                profile,
                package.parsed_at or now,
                "success",
                parser=package.parser_provider,
            )
        ],
        "audit_issues": _audit_issues(package, source_file_id),
    }, mapping_segments


def _index_pages(
    package: DocumentPackage,
    source_file_id: str,
    document_id: str,
) -> List[Dict[str, object]]:
    pages: List[Dict[str, object]] = []
    for raw in package.pages:
        physical = int(raw["physical_pdf_page"])
        index = physical - 1
        logical = raw.get("logical_page")
        calibrated = logical not in (None, "")
        text = str(raw.get("text") or "")
        provenance = raw.get("parser_provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        pages.append(
            {
                "pdf_page_id": f"{source_file_id}-PAGE-{index:06d}",
                "source_file_id": source_file_id,
                "document_id": document_id,
                "pdf_page_index": index,
                "pdf_page_number_1based": physical,
                "pdf_page_label": raw.get("pdf_page_label"),
                "printed_page": str(logical) if calibrated else None,
                "printed_page_start": str(logical) if calibrated else None,
                "printed_page_end": str(logical) if calibrated else None,
                "citation_page": str(logical) if calibrated else None,
                "citation_page_start": str(logical) if calibrated else None,
                "citation_page_end": str(logical) if calibrated else None,
                "page_mapping_method": "document_package" if calibrated else "uncalibrated",
                "page_mapping_confidence": 1.0 if calibrated else 0.0,
                "mapping_confidence_level": "high" if calibrated else "unknown",
                "text_raw": text,
                "page_text_hash": pdf_page_text_hash(text),
                "normalized_text": normalize_pdf_text(text),
                "text_source": package.parser_provider,
                "blocks": raw.get("blocks"),
                "bbox": raw.get("bbox"),
                "reading_order": raw.get("reading_order"),
                "parser": provenance.get("provider") or package.parser_provider,
                "parser_version": provenance.get("version") or package.parser_version,
                "parser_provenance": dict(provenance),
                "warnings": list(raw.get("warnings") or []),
            }
        )
    return pages


def mapping_segments_from_pages(
    pages: Sequence[Mapping[str, object]],
) -> List[Dict[str, object]]:
    segments: List[Dict[str, object]] = []
    for page in pages:
        logical = page.get("logical_page")
        if logical in (None, ""):
            continue
        physical_index = int(page["physical_pdf_page"]) - 1
        label = str(logical).strip()
        style, number = _number_style(label)
        if (
            segments
            and style != "label"
            and segments[-1].get("number_style") == style
            and int(segments[-1]["pdf_page_end"]) + 1 == physical_index
            and int(segments[-1]["logical_page_end_number"]) + 1 == number
        ):
            segments[-1]["pdf_page_end"] = physical_index
            segments[-1]["logical_page_end_number"] = number
            continue
        segments.append(
            {
                "pdf_page_start": physical_index,
                "pdf_page_end": physical_index,
                "citation_page_start": label,
                "number_style": style if style != "label" else "arabic",
                "method": "document_package",
                "confidence": 1.0,
                "layout_mode": "single",
                "logical_page_end_number": number,
            }
        )
    for segment in segments:
        segment.pop("logical_page_end_number", None)
    return segments


def _number_style(label: str) -> Tuple[str, Optional[int]]:
    if label.isdigit():
        return "arabic", int(label)
    if _ROMAN_RE.fullmatch(label):
        return ("roman_upper" if label.isupper() else "roman_lower"), _roman_value(label)
    return "label", None


def _roman_value(label: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    previous = 0
    for character in reversed(label.lower()):
        value = values[character]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total


def _audit_issues(
    package: DocumentPackage,
    source_file_id: str,
) -> List[Dict[str, object]]:
    result: List[Dict[str, object]] = []
    for warning in package.warnings:
        if isinstance(warning, Mapping):
            item = dict(warning)
            item["source_file_id"] = source_file_id
            item.setdefault("severity", "warning")
            item.setdefault("issue_type", "imported_parser_warning")
            result.append(item)
        else:
            result.append(
                {
                    "source_file_id": source_file_id,
                    "severity": "warning",
                    "issue_type": "imported_parser_warning",
                    "message": str(warning),
                }
            )
    if package.missing_ranges:
        result.append(
            {
                "source_file_id": source_file_id,
                "severity": "warning",
                "issue_type": "imported_missing_page_ranges",
                "message": "导入的文档包包含缺页区间。",
                "missing_ranges": list(package.missing_ranges),
            }
        )
    return result


def _attach_metadata(source: Dict[str, object], metadata: Mapping[str, object]) -> None:
    allowed = (*METADATA_FIELDS, "document_type", "metadata_status", "metadata_source")
    for key in allowed:
        if metadata.get(key) not in (None, ""):
            source[key] = metadata[key]
    source["bibliographic_metadata"] = {
        key: metadata[key]
        for key in allowed
        if metadata.get(key) not in (None, "")
    }


def _optional_text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None
