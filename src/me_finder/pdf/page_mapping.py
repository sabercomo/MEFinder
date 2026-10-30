"""PDF citation page mapping helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class PageMappingResult:
    citation_page: Optional[str]
    method: str
    confidence: float
    segment_id: Optional[str] = None
    citation_page_end: Optional[str] = None
    layout_mode: str = "single"
    reading_direction: str = "ltr"
    gutter_x: float = 0.5

    @property
    def citation_page_start(self) -> Optional[str]:
        """Return the first logical citation page on the physical PDF page."""

        return self.citation_page


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
            segment_id = mapping_segment_id(segment, start=start, end=end)
            layout_mode = mapping_layout_mode(segment)
            reading_direction = mapping_reading_direction(segment)
            gutter_x = mapping_gutter_x(segment)
            if segment.get("citation") is None and "citation" in segment:
                return PageMappingResult(
                    None,
                    str(segment.get("method") or "uncalibrated"),
                    float(segment.get("confidence") or 0.0),
                    segment_id,
                    None,
                    layout_mode,
                    reading_direction,
                    gutter_x,
                )
            citation_start = segment.get("citation_page_start", segment.get("citation_start"))
            if citation_start is None:
                return PageMappingResult(
                    None,
                    str(segment.get("method") or "uncalibrated"),
                    float(segment.get("confidence") or 0.0),
                    segment_id,
                    None,
                    layout_mode,
                    reading_direction,
                    gutter_x,
                )
            logical_page_count = 2 if layout_mode == "spread" else 1
            offset = (pdf_page_index - start) * logical_page_count
            style = str(segment.get("number_style") or "arabic")
            mapped_start = _increment_label(str(citation_start), offset, style)
            mapped_end = _increment_label(
                str(citation_start), offset + logical_page_count - 1, style
            )
            return PageMappingResult(
                mapped_start,
                str(segment.get("method") or "manual_segment"),
                float(segment.get("confidence") or 0.9),
                segment_id,
                mapped_end,
                layout_mode,
                reading_direction,
                gutter_x,
            )
        if self.use_page_labels and pdf_page_label:
            return PageMappingResult(
                str(pdf_page_label),
                "pdf_page_label",
                0.75,
                "PDF-PAGE-LABELS",
            )
        return PageMappingResult(None, "uncalibrated", 0.0)


def mapping_layout_mode(segment: Dict[str, object]) -> str:
    """Normalize the additive physical-page layout contract.

    Missing or unknown values deliberately remain ``single`` so every mapping
    created by older releases keeps its one-PDF-page-to-one-citation-page
    behavior.
    """

    return "spread" if str(segment.get("layout_mode") or "").strip().lower() == "spread" else "single"


def mapping_reading_direction(segment: Dict[str, object]) -> str:
    """Return the logical reading direction used by a spread segment."""

    return "rtl" if str(segment.get("reading_direction") or "").strip().lower() == "rtl" else "ltr"


def mapping_gutter_x(segment: Dict[str, object]) -> float:
    """Return a safe normalized gutter position for a physical spread page."""

    try:
        gutter_x = float(segment.get("gutter_x") or 0.5)
    except (TypeError, ValueError):
        return 0.5
    return gutter_x if 0.3 <= gutter_x <= 0.7 else 0.5


def normalize_manual_mapping_segments(
    segments: Sequence[Mapping[str, object]],
) -> List[Dict[str, object]]:
    """Validate and normalize manual mapping payloads before persistence."""

    cleaned: List[Dict[str, object]] = []
    allowed_styles = {"arabic", "roman_lower", "roman_upper", "none"}
    for index, item in enumerate(segments):
        if not isinstance(item, Mapping):
            raise ValueError(f"第 {index + 1} 个页码分段格式无效。")
        try:
            start = int(item.get("pdf_page_start"))
            end = int(item.get("pdf_page_end"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {index + 1} 个页码分段缺少有效的 PDF 页范围。") from exc
        if start < 0 or end < start:
            raise ValueError(f"第 {index + 1} 个页码分段的 PDF 页范围无效。")

        layout_mode = mapping_layout_mode(dict(item))
        style = str(item.get("number_style") or "arabic")
        if style not in allowed_styles:
            style = "arabic"
        try:
            confidence = float(item.get("confidence") or 0.9)
        except (TypeError, ValueError):
            confidence = 0.9
        clean: Dict[str, object] = {
            "pdf_page_start": start,
            "pdf_page_end": end,
            "number_style": style,
            "method": str(item.get("method") or "manual_segment"),
            "confidence": max(0.0, min(1.0, confidence)),
            "layout_mode": layout_mode,
        }
        citation_start = item.get("citation_page_start")
        normalized_citation_start = str(citation_start).strip() if citation_start is not None else ""
        if style == "none" or not normalized_citation_start:
            clean["citation"] = None
        else:
            clean["citation_page_start"] = normalized_citation_start
        if layout_mode == "spread":
            clean["reading_direction"] = mapping_reading_direction(dict(item))
            clean["gutter_x"] = mapping_gutter_x(dict(item))
        for key in ("label", "evidence", "page_scope", "mapping_evidence", "segment_id"):
            if item.get(key) not in (None, ""):
                clean[key] = item.get(key)
        cleaned.append(clean)
    return cleaned


def mapping_segment_id(
    segment: Dict[str, object],
    *,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> str:
    """Return a stable ID for one persisted mapping interval."""

    explicit = str(segment.get("segment_id") or "").strip()
    if explicit:
        return explicit
    resolved_start = (
        start
        if start is not None
        else _segment_int(segment, "pdf_page_start", "pdf_start")
    )
    resolved_end = (
        end
        if end is not None
        else _segment_int(segment, "pdf_page_end", "pdf_end")
    )
    if resolved_start is None:
        resolved_start = 0
    if resolved_end is None:
        resolved_end = resolved_start
    return f"MAPSEG-{resolved_start:06d}-{resolved_end:06d}"


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
