"""Summarise a document's stored page mapping as contiguous runs.

Read-only: it inspects the page evidence already written into the index and
never runs automatic detection or touches source files. Page statuses reuse
``page_display.resolve_citation_page`` so a run is "calibrated" exactly when
search and reader results would report the same pages as citable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .page_display import page_source_note, resolve_citation_page
from .persistence.page_mapping_reads import read_page_mapping_evidence
from .structured_reader import SourceNotFound, StructuredReaderError, _validate_source_id

MAX_SEGMENTS = 200
_ROMAN_RE = re.compile(r"(?i)m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})")
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def describe_page_mapping(db_path: Path, source_id: str) -> Dict[str, object]:
    """Return mapping runs, coverage and the stored mapping record."""

    source_id = _validate_source_id(source_id)
    if not Path(db_path).is_file():
        raise StructuredReaderError(f"索引数据库不存在：{db_path}")
    evidence = read_page_mapping_evidence(Path(db_path), source_id)
    if evidence is None:
        raise SourceNotFound(f"未找到文献：{source_id}")
    source = evidence["source"]
    is_pdf = source["source_type"] == "pdf"
    segments: List[Dict[str, object]] = []
    calibrated_units = 0
    for unit in evidence["units"]:
        entry = _unit_entry(unit, is_pdf)
        if entry["status"] in {"calibrated", "verified"}:
            calibrated_units += 1
        if segments and _extends(segments[-1], entry, is_pdf):
            _extend(segments[-1], entry)
        else:
            segments.append(entry)
    total = len(evidence["units"])
    if calibrated_units == 0:
        coverage = "none"
    elif calibrated_units == total:
        coverage = "full"
    else:
        coverage = "partial"
    return {
        "source": source,
        "unit": "pdf_page" if is_pdf else "word_paragraph",
        "total_units": total,
        "calibrated_units": calibrated_units,
        "coverage": coverage,
        "mapping_record": evidence["mapping_record"],
        "segments": [_public_segment(item) for item in segments[:MAX_SEGMENTS]],
        "segments_truncated": len(segments) > MAX_SEGMENTS,
    }


def _unit_entry(unit: Mapping[str, object], is_pdf: bool) -> Dict[str, object]:
    fields = dict(unit)
    fields["source_type"] = "pdf" if is_pdf else "word"
    if is_pdf:
        # Physical pages always exist; this keeps PDF resolution on the PDF path.
        fields["pdf_page_index"] = unit["position"]
    resolution = resolve_citation_page(fields)
    if resolution.verified:
        status = "calibrated" if is_pdf else "verified"
    else:
        status = "uncalibrated" if is_pdf else "unavailable"
    method = resolution.page_source_type
    position = int(unit["position"])
    start = resolution.start
    end = resolution.end or start
    style = _number_style(start) if start else None
    start_value = _page_value(start, style)
    end_value = _page_value(end, style)
    offset = None
    if is_pdf and start_value is not None and start_value == end_value:
        offset = start_value - (position + 1)
    return {
        "start": position,
        "end": position,
        "status": status,
        "method": method,
        "citation_page_start": start,
        "citation_page_end": end,
        "number_style": style,
        "offset": offset,
        "_last_value": end_value,
        "_uniform_offset": offset is not None,
    }


def _extends(run: Mapping[str, object], entry: Mapping[str, object], is_pdf: bool) -> bool:
    if (
        entry["start"] != int(run["end"]) + 1
        or entry["status"] != run["status"]
        or entry["method"] != run["method"]
    ):
        return False
    if entry["status"] not in {"calibrated", "verified"}:
        return True
    if entry["number_style"] != run["number_style"]:
        return False
    if not is_pdf or entry["number_style"] == "other":
        return True
    # A PDF run stays one run only while printed pages keep counting upward,
    # so a restart or jump (the offset changing mid-book) opens a new run.
    first_value = _page_value(entry["citation_page_start"], entry["number_style"])
    last_value = run["_last_value"]
    return first_value is not None and last_value is not None and first_value == last_value + 1


def _extend(run: Dict[str, object], entry: Mapping[str, object]) -> None:
    run["end"] = entry["end"]
    run["citation_page_end"] = entry["citation_page_end"]
    run["_last_value"] = entry["_last_value"]
    if run["_uniform_offset"] and entry["offset"] != run["offset"]:
        # Spreads and similar layouts do not map one PDF page to one printed page.
        run["_uniform_offset"] = False
        run["offset"] = None


def _public_segment(run: Mapping[str, object]) -> Dict[str, object]:
    return {
        "start": run["start"],
        "end": run["end"],
        "status": run["status"],
        "method": run["method"],
        "method_note": page_source_note(run["method"]),
        "citation_page_start": run["citation_page_start"],
        "citation_page_end": run["citation_page_end"],
        "number_style": run["number_style"],
        "offset": run["offset"],
    }


def _number_style(label: str) -> str:
    text = label.strip()
    if text.isdigit():
        return "arabic"
    if text and _ROMAN_RE.fullmatch(text):
        return "roman"
    return "other"


def _page_value(label: Optional[str], style: Optional[str]) -> Optional[int]:
    if not label or style not in {"arabic", "roman"}:
        return None
    text = label.strip()
    if style == "arabic":
        return int(text) if text.isdigit() else None
    if not _ROMAN_RE.fullmatch(text):
        return None
    total = 0
    previous = 0
    for char in reversed(text.lower()):
        value = _ROMAN_VALUES[char]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total
