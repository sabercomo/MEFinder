"""PDF citation page mapping helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class PageMappingResult:
    citation_page: Optional[str]
    method: str
    confidence: float


ROMAN_VALUES = [
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
]


def int_to_roman(number: int, upper: bool = False) -> str:
    if number <= 0:
        return str(number)
    value = number
    out: List[str] = []
    for amount, roman in ROMAN_VALUES:
        while value >= amount:
            out.append(roman)
            value -= amount
    result = "".join(out)
    return result.upper() if upper else result


def page_range_display(start: Optional[str], end: Optional[str]) -> Optional[str]:
    if not start:
        return None
    if end and end != start:
        return f"{start}-{end}"
    return start


def physical_page_display(start_index: int, end_index: Optional[int] = None) -> str:
    start = start_index + 1
    end = (end_index if end_index is not None else start_index) + 1
    if end != start:
        return f"PDF 第 {start}-{end} 页，引用页码尚未校准"
    return f"PDF 第 {start} 页，引用页码尚未校准"


def mapped_page_display(
    start_index: int,
    end_index: int,
    start_citation: Optional[str],
    end_citation: Optional[str],
) -> str:
    label = page_range_display(start_citation, end_citation)
    if label:
        return f"引用页码：{label}"
    return physical_page_display(start_index, end_index)


class PageMapper:
    """Map PDF physical page indexes to calibrated citation pages."""

    def __init__(self, segments: Optional[List[Dict[str, object]]] = None, use_page_labels: bool = False) -> None:
        self.segments = segments or []
        self.use_page_labels = use_page_labels

    @classmethod
    def from_config(cls, config: Dict[str, object]) -> "PageMapper":
        mapping = config.get("page_mapping") or {}
        if not isinstance(mapping, dict):
            mapping = {}
        segments = mapping.get("segments") or config.get("segments") or []
        if not isinstance(segments, list):
            segments = []
        return cls(segments=segments, use_page_labels=bool(mapping.get("use_page_labels")))

    def map_page(self, pdf_page_index: int, pdf_page_label: Optional[str] = None) -> PageMappingResult:
        for segment in self.segments:
            start = _segment_int(segment, "pdf_page_start", "pdf_start")
            end = _segment_int(segment, "pdf_page_end", "pdf_end")
            if start is None:
                continue
            if end is None:
                end = start
            if not (start <= pdf_page_index <= end):
                continue
            if segment.get("citation") is None and "citation" in segment:
                return PageMappingResult(
                    None,
                    str(segment.get("method") or "uncalibrated"),
                    float(segment.get("confidence") or 0.0),
                )
            citation_start = segment.get("citation_page_start", segment.get("citation_start"))
            if citation_start is None:
                return PageMappingResult(
                    None,
                    str(segment.get("method") or "uncalibrated"),
                    float(segment.get("confidence") or 0.0),
                )
            offset = pdf_page_index - start
            return PageMappingResult(
                _increment_label(str(citation_start), offset, str(segment.get("number_style") or "arabic")),
                str(segment.get("method") or "manual_segment"),
                float(segment.get("confidence") or 0.9),
            )
        if self.use_page_labels and pdf_page_label:
            return PageMappingResult(str(pdf_page_label), "pdf_page_label", 0.75)
        return PageMappingResult(None, "uncalibrated", 0.0)


def _segment_int(segment: Dict[str, object], primary: str, fallback: str) -> Optional[int]:
    value = segment.get(primary, segment.get(fallback))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _increment_label(start: str, offset: int, style: str) -> str:
    try:
        number = int(start) + offset
    except ValueError:
        number = offset + 1
    if style == "roman_lower":
        return int_to_roman(number, upper=False)
    if style == "roman_upper":
        return int_to_roman(number, upper=True)
    return str(number)
