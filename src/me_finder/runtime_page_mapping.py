"""Apply one PDF page mapping to the live SQLite index without a full rebuild."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .pdf_page_mapping import PageMapper, mapped_page_display


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
                "segment_id": item.get("segment_id"),
                "mapping_evidence": item.get("mapping_evidence") or item.get("evidence"),
                "label": item.get("label") or "自动检测页码",
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
    connection = sqlite3.connect(str(database_path))
    page_updates = 0
    paragraph_updates = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        pages_by_index: Dict[int, Dict[str, object]] = {}
        page_rows = connection.execute(
            "SELECT row_id, pdf_page_index, payload_json FROM pdf_pages WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchall()
        for row_id, page_idx_value, payload_json in page_rows:
            page = json.loads(payload_json)
            page_idx = int(page_idx_value)
            _apply_page_mapping(page, page_idx, mapper, cleaned)
            pages_by_index[page_idx] = page
            connection.execute("UPDATE pdf_pages SET payload_json = ? WHERE row_id = ?", (_json(page), row_id))
            page_updates += 1

        paragraph_rows = connection.execute(
            "SELECT paragraph_id, payload_json FROM paragraphs WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchall()
        for paragraph_id, payload_json in paragraph_rows:
            paragraph = json.loads(payload_json)
            start_idx = int(paragraph.get("pdf_page_start_index") or 0)
            end_idx = int(paragraph.get("pdf_page_end_index") or start_idx)
            start_page = pages_by_index.get(start_idx, {})
            end_page = pages_by_index.get(end_idx, {})
            _apply_paragraph_mapping(paragraph, start_idx, end_idx, start_page, end_page)
            connection.execute(
                """
                UPDATE paragraphs
                   SET page_display = ?, page_source_type = ?, page_confidence = ?,
                       citation_page_start = ?, citation_page_end = ?, payload_json = ?
                 WHERE paragraph_id = ?
                """,
                (
                    paragraph.get("page_display"),
                    paragraph.get("page_source_type"),
                    paragraph.get("page_confidence"),
                    paragraph.get("citation_page_start"),
                    paragraph.get("citation_page_end"),
                    _json(paragraph),
                    paragraph_id,
                ),
            )
            paragraph_updates += 1

        source_row = connection.execute(
            "SELECT payload_json FROM source_files WHERE source_file_id = ?", (source_file_id,)
        ).fetchone()
        if source_row:
            source = json.loads(source_row[0])
            profile = source.setdefault("pdf_profile", {})
            profile["mapping_status"] = mapping_status
            if auto_mapping is not None:
                stored_mapping = dict(auto_mapping)
                stored_mapping["mapping_status"] = mapping_status
                stored_mapping["applied_segments"] = cleaned
                stored_mapping["applied_segment_count"] = len(cleaned)
                profile["auto_page_mapping"] = stored_mapping
                profile["mapping_failure_reasons"] = stored_mapping.get("failure_reasons", [])
            connection.execute(
                "UPDATE source_files SET payload_json = ? WHERE source_file_id = ?",
                (_json(source), source_file_id),
            )

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
        existing = connection.execute(
            "SELECT row_id FROM pdf_page_mappings WHERE source_file_id = ? LIMIT 1", (source_file_id,)
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE pdf_page_mappings SET payload_json = ? WHERE source_file_id = ?",
                (_json(mapping_payload), source_file_id),
            )
        else:
            connection.execute(
                "INSERT INTO pdf_page_mappings(source_file_id, pdf_page_index, payload_json) VALUES (?, NULL, ?)",
                (source_file_id, _json(mapping_payload)),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"pages": page_updates, "paragraphs": paragraph_updates, "segments": len(cleaned)}


def _apply_page_mapping(
    page: Dict[str, object],
    page_idx: int,
    mapper: PageMapper,
    segments: Sequence[Dict[str, object]],
) -> None:
    result = mapper.map_page(page_idx, page.get("pdf_page_label"))
    segment = _segment_for_page(segments, page_idx)
    citation = result.citation_page
    page["citation_page"] = citation
    page["printed_page"] = citation
    page["page_mapping_method"] = result.method
    page["page_mapping_confidence"] = result.confidence
    page["mapping_method"] = result.method
    page["mapping_confidence"] = result.confidence
    page["citation_page_label"] = citation
    try:
        page["citation_page_number"] = int(str(citation)) if citation is not None else None
    except ValueError:
        page["citation_page_number"] = None
    page["page_scope"] = segment.get("page_scope") if segment else None
    page["mapping_confidence_level"] = segment.get("confidence_level") if segment else None
    page["mapping_evidence"] = segment.get("mapping_evidence") if segment else None
    page["segment_id"] = segment.get("segment_id") if segment else None


def _apply_paragraph_mapping(
    paragraph: Dict[str, object],
    start_idx: int,
    end_idx: int,
    start_page: Dict[str, object],
    end_page: Dict[str, object],
) -> None:
    start_citation = start_page.get("citation_page")
    end_citation = end_page.get("citation_page")
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
    paragraph["citation_page_number_start"] = start_page.get("citation_page_number")
    paragraph["citation_page_number_end"] = end_page.get("citation_page_number")
    paragraph["citation_page_label_start"] = start_page.get("citation_page_label")
    paragraph["citation_page_label_end"] = end_page.get("citation_page_label")
    paragraph["printed_page_start"] = start_page.get("printed_page")
    paragraph["printed_page_end"] = end_page.get("printed_page")
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
