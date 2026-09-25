"""Apply one PDF page mapping to the live SQLite index without a full rebuild."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .persistence.paragraph_payload import (
    paragraph_from_database_row,
    paragraph_payload_for_storage,
)
from .persistence.page_mapping_store import (
    page_mapping_transaction,
    paragraph_rows,
    pdf_page_rows,
    source_payload_json,
    write_page_mapping,
    write_paragraph,
    write_pdf_page,
    write_source_payload,
)
from .pdf_page_mapping import (
    PageMapper,
    mapping_gutter_x,
    mapping_layout_mode,
    mapping_reading_direction,
    mapped_page_display,
    mapping_segment_id,
)


def normalize_auto_segments(segments: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    cleaned: List[Dict[str, object]] = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item["pdf_page_start"])
            end = int(item["pdf_page_end"])
            citation_start = str(item["citation_page_start"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < 0 or end < start or not citation_start:
            continue
        confidence = float(item.get("mapping_confidence") or item.get("confidence") or 0.8)
        cleaned.append(
            {
                "pdf_page_start": start,
                "pdf_page_end": end,
                "citation_page_start": citation_start,
                "number_style": str(item.get("number_style") or "arabic"),
                "method": str(item.get("method") or item.get("mapping_method") or "auto_sequence"),
                "confidence": confidence,
                "confidence_level": str(item.get("confidence_level") or _confidence_level(confidence)),
                "page_scope": item.get("page_scope"),
                "segment_id": mapping_segment_id(item, start=start, end=end),
                "mapping_evidence": item.get("mapping_evidence") or item.get("evidence"),
                "label": item.get("label") or "自动检测页码",
                "layout_mode": mapping_layout_mode(item),
                "reading_direction": mapping_reading_direction(item),
                "gutter_x": mapping_gutter_x(item),
            }
        )
    cleaned.sort(key=lambda item: int(item["pdf_page_start"]))
    return cleaned


def apply_mapping_to_database(
    database_path: Path,
    source_file_id: str,
    segments: Sequence[Dict[str, object]],
    *,
    auto_mapping: Optional[Dict[str, object]] = None,
    mapping_status: str = "auto_mapped_high",
) -> Dict[str, int]:
    """Update pages, paragraphs, source metadata, and mapping payload atomically."""

    database_path = Path(database_path)
    cleaned = normalize_auto_segments(segments)
    if not cleaned:
        raise ValueError("没有可应用的自动页码区间。")
    _backup_database(database_path)
    mapper = PageMapper(cleaned)
    page_updates = 0
    paragraph_updates = 0
    with page_mapping_transaction(database_path) as connection:
        pages_by_index: Dict[int, Dict[str, object]] = {}
        for row_id, page_idx_value, payload_json in pdf_page_rows(connection, source_file_id):
            page = json.loads(payload_json)
            page_idx = int(page_idx_value)
            _apply_page_mapping(page, page_idx, mapper, cleaned)
            pages_by_index[page_idx] = page
            write_pdf_page(connection, row_id, _json(page))
            page_updates += 1

        for row in paragraph_rows(connection, source_file_id):
            paragraph = paragraph_from_database_row(row)
            paragraph_id = str(row["paragraph_id"])
            start_idx = int(paragraph.get("pdf_page_start_index") or 0)
            end_idx = int(paragraph.get("pdf_page_end_index") or start_idx)
            start_page = pages_by_index.get(start_idx, {})
            end_page = pages_by_index.get(end_idx, {})
            _apply_paragraph_mapping(paragraph, start_idx, end_idx, start_page, end_page)
            write_paragraph(
                connection,
                paragraph_id,
                paragraph,
                _json(paragraph_payload_for_storage(paragraph)),
            )
            paragraph_updates += 1

        source_json = source_payload_json(connection, source_file_id)
        if source_json is not None:
            source = json.loads(source_json)
            profile = source.setdefault("pdf_profile", {})
            profile["mapping_status"] = mapping_status
            if auto_mapping is not None:
                stored_mapping = dict(auto_mapping)
                stored_mapping["mapping_status"] = mapping_status
                stored_mapping["applied_segments"] = cleaned
                stored_mapping["applied_segment_count"] = len(cleaned)
                profile["auto_page_mapping"] = stored_mapping
                profile["mapping_failure_reasons"] = stored_mapping.get("failure_reasons", [])
            write_source_payload(connection, source_file_id, _json(source))

        mapping_payload = {
            "mapping_id": f"MAP-{source_file_id}",
            "source_file_id": source_file_id,
            "method": _overall_method(cleaned),
            "segments": cleaned,
            "auto_segments": cleaned,
            "auto_page_mapping": auto_mapping or {},
            "confidence": max(float(item.get("confidence") or 0.0) for item in cleaned),
            "validated_by": "auto_mapping_ui",
            "mapping_status": mapping_status,
        }
        write_page_mapping(connection, source_file_id, _json(mapping_payload))
    return {"pages": page_updates, "paragraphs": paragraph_updates, "segments": len(cleaned)}


def _apply_page_mapping(
    page: Dict[str, object],
    page_idx: int,
    mapper: PageMapper,
    segments: Sequence[Dict[str, object]],
) -> None:
    result = mapper.map_page(page_idx, page.get("pdf_page_label"))
    segment = _segment_for_page(segments, page_idx)
    citation = result.citation_page_start
    citation_end = result.citation_page_end or citation
    page["citation_page"] = citation
    page["citation_page_start"] = citation
    page["citation_page_end"] = citation_end
    page["printed_page"] = citation
    page["printed_page_start"] = citation
    page["printed_page_end"] = citation_end
    page["page_mapping_method"] = result.method
    page["page_mapping_confidence"] = result.confidence
    page["mapping_method"] = result.method
    page["mapping_confidence"] = result.confidence
    page["citation_page_label"] = citation
    page["citation_page_label_start"] = citation
    page["citation_page_label_end"] = citation_end
    try:
        page["citation_page_number"] = int(str(citation)) if citation is not None else None
    except ValueError:
        page["citation_page_number"] = None
    try:
        page["citation_page_number_start"] = int(str(citation)) if citation is not None else None
    except ValueError:
        page["citation_page_number_start"] = None
    try:
        page["citation_page_number_end"] = int(str(citation_end)) if citation_end is not None else None
    except ValueError:
        page["citation_page_number_end"] = None
    page["layout_mode"] = result.layout_mode
    page["reading_direction"] = result.reading_direction
    page["gutter_x"] = result.gutter_x
    page["page_scope"] = segment.get("page_scope") if segment else None
    page["mapping_confidence_level"] = segment.get("confidence_level") if segment else None
    page["mapping_evidence"] = segment.get("mapping_evidence") if segment else None
    page["segment_id"] = result.segment_id


def _apply_paragraph_mapping(
    paragraph: Dict[str, object],
    start_idx: int,
    end_idx: int,
    start_page: Dict[str, object],
    end_page: Dict[str, object],
) -> None:
    start_citation = start_page.get("citation_page_start") or start_page.get("citation_page")
    end_citation = end_page.get("citation_page_end") or end_page.get("citation_page")
    calibrated = bool(start_citation and end_citation)
    method = start_page.get("page_mapping_method")
    if method != end_page.get("page_mapping_method"):
        method = "mixed"
    confidence = min(
        float(start_page.get("page_mapping_confidence") or 0.0),
        float(end_page.get("page_mapping_confidence") or 0.0),
    )
    paragraph["original_page_start"] = str(start_citation) if calibrated else None
    paragraph["original_page_end"] = str(end_citation) if calibrated else None
    paragraph["citation_page_start"] = str(start_citation) if calibrated else None
    paragraph["citation_page_end"] = str(end_citation) if calibrated else None
    paragraph["citation_page_number_start"] = start_page.get(
        "citation_page_number_start", start_page.get("citation_page_number")
    )
    paragraph["citation_page_number_end"] = end_page.get(
        "citation_page_number_end", end_page.get("citation_page_number")
    )
    paragraph["citation_page_label_start"] = start_page.get(
        "citation_page_label_start", start_page.get("citation_page_label")
    )
    paragraph["citation_page_label_end"] = end_page.get(
        "citation_page_label_end", end_page.get("citation_page_label")
    )
    paragraph["printed_page_start"] = start_page.get("printed_page_start") or start_page.get("printed_page")
    paragraph["printed_page_end"] = end_page.get("printed_page_end") or end_page.get("printed_page")
    paragraph["page_source_type"] = str(method or "uncalibrated")
    paragraph["page_mapping_method"] = str(method or "uncalibrated")
    paragraph["mapping_method"] = str(method or "uncalibrated")
    paragraph["page_confidence"] = confidence if calibrated else 0.0
    paragraph["page_mapping_confidence"] = confidence if calibrated else 0.0
    paragraph["mapping_confidence"] = confidence if calibrated else 0.0
    paragraph["page_display"] = mapped_page_display(
        start_idx,
        end_idx,
        str(start_citation) if calibrated else None,
        str(end_citation) if calibrated else None,
    )
    paragraph["page_scope"] = _same_or_mixed(start_page.get("page_scope"), end_page.get("page_scope"))
    paragraph["mapping_confidence_level"] = _same_or_mixed(
        start_page.get("mapping_confidence_level"), end_page.get("mapping_confidence_level")
    )
    paragraph["mapping_evidence"] = start_page.get("mapping_evidence")
    paragraph["segment_id"] = _same_or_none(start_page.get("segment_id"), end_page.get("segment_id"))
    paragraph["layout_mode"] = _same_or_mixed(start_page.get("layout_mode"), end_page.get("layout_mode"))
    paragraph["reading_direction"] = _same_or_mixed(
        start_page.get("reading_direction"), end_page.get("reading_direction")
    )
    paragraph["gutter_x"] = _same_or_none(start_page.get("gutter_x"), end_page.get("gutter_x"))


def _segment_for_page(segments: Sequence[Dict[str, object]], page_idx: int) -> Optional[Dict[str, object]]:
    return next(
        (
            segment
            for segment in segments
            if int(segment.get("pdf_page_start") or 0) <= page_idx <= int(segment.get("pdf_page_end") or 0)
        ),
        None,
    )


def _same_or_mixed(left: object, right: object) -> object:
    if left == right:
        return left
    return "mixed" if left or right else None


def _same_or_none(left: object, right: object) -> object:
    return left if left == right else None


def _confidence_level(confidence: float) -> str:
    return "high" if confidence >= 0.86 else "medium" if confidence >= 0.68 else "low"


def _overall_method(segments: Sequence[Dict[str, object]]) -> str:
    methods = {str(item.get("method") or "auto_sequence") for item in segments}
    return next(iter(methods)) if len(methods) == 1 else "combined_sequence"


def _backup_database(database_path: Path) -> Path:
    backup_dir = database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    target = backup_dir / f"{database_path.stem}-page-mapping-{stamp}{database_path.suffix}"
    shutil.copy2(database_path, target)
    return target


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
