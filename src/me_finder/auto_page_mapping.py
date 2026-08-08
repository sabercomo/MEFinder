"""Automatic PDF citation page mapping from structured OCR output.

The mapper is intentionally deterministic: it does not ask a language model to
judge pages.  It extracts short page-number-like candidates from MinerU
structured files, fits stable cross-page sequences, and only applies high
confidence segments.  Medium and low confidence suggestions are kept as
evidence for manual review.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .pdf_page_mapping import int_to_roman


AUTO_PAGE_MAPPING_THRESHOLDS: Dict[str, float] = {
    "edge_ratio": 0.14,
    "outer_margin_ratio": 0.16,
    "vertical_page_number_min_y": 0.64,
    "max_candidate_chars": 18,
    "min_high_support": 4,
    "min_medium_support": 3,
    "max_cluster_gap": 12,
    "max_bookmark_cluster_gap": 250,
    "high_confidence": 0.86,
    "medium_confidence": 0.68,
}

AUTO_LAYOUT_THRESHOLDS: Dict[str, float] = {
    "landscape_ratio": 1.05,
    "left_zone_end": 0.46,
    "right_zone_start": 0.54,
    "min_side_share": 0.22,
    "max_center_share": 0.20,
    "min_eligible_pages": 4,
    "min_high_split_ratio": 0.55,
    "min_medium_split_ratio": 0.45,
    "min_high_pair_pages": 3,
    "min_medium_pair_pages": 1,
    "min_high_stride_support": 6,
    "min_medium_stride_support": 3,
}

STRUCTURE_KEYWORDS = {
    "intro": ["导言", "绪论"],
}

_CJK_PAGE_DIGITS = {
    "〇": "0",
    "零": "0",
    "一": "1",
    "壹": "1",
    "二": "2",
    "两": "2",
    "兩": "2",
    "贰": "2",
    "貳": "2",
    "三": "3",
    "叁": "3",
    "參": "3",
    "四": "4",
    "肆": "4",
    "五": "5",
    "伍": "5",
    "六": "6",
    "陆": "6",
    "陸": "6",
    "七": "7",
    "柒": "7",
    "八": "8",
    "捌": "8",
    "九": "9",
    "玖": "9",
}
_CJK_PAGE_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CJK_PAGE_DIGIT_CHARS = "".join(_CJK_PAGE_DIGITS)
_CJK_PAGE_UNIT_CHARS = "".join(_CJK_PAGE_UNITS)
_CJK_PAGE_DIGIT_RE = re.compile(f"[{re.escape(_CJK_PAGE_DIGIT_CHARS)}]")
_CJK_PAGE_NUMBER_RE = re.compile(
    f"^[{re.escape(_CJK_PAGE_DIGIT_CHARS + _CJK_PAGE_UNIT_CHARS)}]{{1,6}}$"
)


@dataclass(frozen=True)
class PageNumberCandidate:
    page_idx: int
    raw_candidate: str
    normalized_candidate: str
    candidate_type: str
    number_style: str
    number: int
    bbox: Optional[List[float]]
    source: str
    confidence: float = 0.0
    score: float = 0.0
    outline_level: Optional[int] = None
    target_pdf_page_1based: Optional[int] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "page_idx": self.page_idx,
            "raw_candidate": self.raw_candidate,
            "normalized_candidate": self.normalized_candidate,
            "candidate_type": self.candidate_type,
            "number_style": self.number_style,
            "number": self.number,
            "bbox": self.bbox,
            "source": self.source,
            "confidence": self.confidence,
            "score": self.score,
            "outline_level": self.outline_level,
            "target_pdf_page_1based": self.target_pdf_page_1based,
        }


def detect_pdf_page_layout(
    pages: Sequence[Dict[str, object]],
    candidates: Sequence[PageNumberCandidate],
    *,
    thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """Conservatively classify physical PDF pages as single pages or spreads.

    A wide page with two text columns is not enough on its own: automatic
    spread mode additionally requires paired outer-edge page numbers or a
    stable two-logical-pages-per-PDF-page sequence.  This keeps ordinary
    landscape papers and two-column articles on the established single-page
    path.
    """

    limits = {**AUTO_LAYOUT_THRESHOLDS, **(thresholds or {})}
    eligible_pages = 0
    landscape_pages = 0
    split_pages = 0
    gutter_samples: List[float] = []
    page_dimensions: List[float] = []
    for page in pages:
        width = _positive_float(page.get("page_width"))
        height = _positive_float(page.get("page_height"))
        blocks = page.get("blocks")
        if width is None or height is None or not isinstance(blocks, list):
            continue
        bbox_width, bbox_height = _layout_bbox_scale(blocks, width, height)
        weights = {"left": 0, "center": 0, "right": 0}
        left_edges: List[float] = []
        right_edges: List[float] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = str(block.get("text") or "").strip()
            if len(text) < 20:
                continue
            bbox = _normalized_page_bbox(block, bbox_width, bbox_height)
            if bbox is None:
                continue
            x0, _, x1, _ = bbox
            center = (x0 + x1) / 2
            weight = min(len(text), 1000)
            if x0 < float(limits["left_zone_end"]) and x1 > float(limits["right_zone_start"]):
                weights["center"] += weight
            elif center < float(limits["left_zone_end"]):
                weights["left"] += weight
                left_edges.append(x1)
            elif center > float(limits["right_zone_start"]):
                weights["right"] += weight
                right_edges.append(x0)
            else:
                weights["center"] += weight
        total_weight = sum(weights.values())
        if total_weight < 80:
            continue
        eligible_pages += 1
        ratio = width / height
        page_dimensions.append(ratio)
        is_landscape = ratio >= float(limits["landscape_ratio"])
        if is_landscape:
            landscape_pages += 1
        left_share = weights["left"] / total_weight
        right_share = weights["right"] / total_weight
        center_share = weights["center"] / total_weight
        is_split = (
            is_landscape
            and left_share >= float(limits["min_side_share"])
            and right_share >= float(limits["min_side_share"])
            and center_share <= float(limits["max_center_share"])
        )
        if not is_split:
            continue
        split_pages += 1
        if left_edges and right_edges:
            left_edge = max(left_edges)
            right_edge = min(right_edges)
            if 0.30 <= left_edge < right_edge <= 0.70:
                gutter_samples.append((left_edge + right_edge) / 2)

    gutter_x = median(gutter_samples) if gutter_samples else 0.5
    gutter_x = max(0.3, min(0.7, float(gutter_x)))
    pair_evidence = _spread_pair_evidence(candidates, gutter_x)
    pair_pages = int(pair_evidence["paired_pages"])
    direction = str(pair_evidence["reading_direction"])
    direction_confidence = float(pair_evidence["direction_confidence"])
    stride_support = _spread_stride_support(candidates, direction, gutter_x)
    landscape_ratio = landscape_pages / max(eligible_pages, 1)
    split_ratio = split_pages / max(eligible_pages, 1)
    enough_pages = eligible_pages >= int(limits["min_eligible_pages"])
    high_sequence_evidence = (
        pair_pages >= int(limits["min_high_pair_pages"])
        and stride_support >= int(limits["min_high_stride_support"])
        and direction_confidence >= 0.75
    )
    medium_sequence_evidence = (
        pair_pages >= int(limits["min_medium_pair_pages"])
        or stride_support >= int(limits["min_medium_stride_support"])
    )
    high_spread = (
        enough_pages
        and landscape_ratio >= 0.75
        and split_ratio >= float(limits["min_high_split_ratio"])
        and high_sequence_evidence
    )
    medium_spread = (
        enough_pages
        and landscape_ratio >= 0.65
        and split_ratio >= float(limits["min_medium_split_ratio"])
        and medium_sequence_evidence
    )
    if high_spread:
        layout_mode = "spread"
        confidence_level = "high"
        confidence = min(
            0.99,
            0.56
            + 0.16 * min(1.0, split_ratio / 0.75)
            + 0.12 * min(1.0, pair_pages / 6)
            + 0.15 * min(1.0, stride_support / 12),
        )
    elif medium_spread:
        layout_mode = "spread"
        confidence_level = "medium"
        confidence = min(
            0.85,
            0.48
            + 0.14 * min(1.0, split_ratio / 0.65)
            + 0.10 * min(1.0, pair_pages / 4)
            + 0.12 * min(1.0, stride_support / 8),
        )
    else:
        layout_mode = "single"
        confidence_level = "high" if eligible_pages and landscape_ratio <= 0.25 else "medium"
        confidence = 0.96 if confidence_level == "high" else 0.72

    return {
        "layout_mode": layout_mode,
        "confidence": round(confidence, 4),
        "confidence_level": confidence_level,
        "reading_direction": direction if layout_mode == "spread" else "ltr",
        "reading_direction_confidence": round(direction_confidence, 4),
        "gutter_x": round(gutter_x, 4),
        "evidence": {
            "eligible_pages": eligible_pages,
            "landscape_pages": landscape_pages,
            "landscape_ratio": round(landscape_ratio, 4),
            "split_pages": split_pages,
            "split_ratio": round(split_ratio, 4),
            "paired_page_numbers": pair_pages,
            "ltr_pair_votes": pair_evidence["ltr_votes"],
            "rtl_pair_votes": pair_evidence["rtl_votes"],
            "stride_two_support": stride_support,
            "median_page_aspect_ratio": round(median(page_dimensions), 4) if page_dimensions else None,
        },
    }


def _layout_bbox_scale(
    blocks: Sequence[object],
    page_width: float,
    page_height: float,
) -> Tuple[float, float]:
    """Adapt MinerU's common 1000-unit boxes to physical page dimensions."""

    max_x = 0.0
    max_y = 0.0
    for block in blocks:
        if not isinstance(block, dict) or block.get("bbox_normalized") is not None:
            continue
        bbox = _bbox(block.get("bbox"))
        if bbox is None:
            continue
        max_x = max(max_x, bbox[0], bbox[2])
        max_y = max(max_y, bbox[1], bbox[3])
    # MinerU content-list/layout boxes use a normalized 1000 x 1000 canvas,
    # while parsed pages retain the physical PDF dimensions.  The physical
    # page may be either smaller or larger than 1000, so coordinate magnitude
    # alone cannot identify the scale.  Preserve the parser metadata while
    # converting the structured result and use it as the authoritative signal.
    mineru_canvas = any(
        isinstance(block, dict)
        and (
            "mineru_item_index" in block
            or "mineru_type" in block
            or str(block.get("parser_type") or "").lower().startswith("mineru")
        )
        for block in blocks
    )
    if mineru_canvas and max_x <= 1200 and max_y <= 1200:
        return 1000.0, 1000.0
    width = page_width
    height = page_height
    if max_x > page_width * 1.2:
        width = 1000.0 if max_x <= 1200 else max_x
    if max_y > page_height * 1.2:
        height = 1000.0 if max_y <= 1200 else max_y
    return width, height


def _spread_pair_evidence(
    candidates: Sequence[PageNumberCandidate],
    gutter_x: float,
) -> Dict[str, object]:
    by_page_style: Dict[Tuple[int, str], List[PageNumberCandidate]] = defaultdict(list)
    for candidate in candidates:
        if _candidate_side(candidate, gutter_x) is not None:
            by_page_style[(candidate.page_idx, _style_family(candidate.number_style))].append(candidate)
    ltr_pages: set[int] = set()
    rtl_pages: set[int] = set()
    for (page_idx, _), group in by_page_style.items():
        for left in group:
            if _candidate_side(left, gutter_x) != "left":
                continue
            for right in group:
                if _candidate_side(right, gutter_x) != "right":
                    continue
                if right.number == left.number + 1:
                    ltr_pages.add(page_idx)
                elif left.number == right.number + 1:
                    rtl_pages.add(page_idx)
    ltr_votes = len(ltr_pages)
    rtl_votes = len(rtl_pages)
    total = ltr_votes + rtl_votes
    direction = "rtl" if rtl_votes > ltr_votes else "ltr"
    return {
        "paired_pages": max(ltr_votes, rtl_votes),
        "ltr_votes": ltr_votes,
        "rtl_votes": rtl_votes,
        "reading_direction": direction,
        "direction_confidence": max(ltr_votes, rtl_votes) / max(total, 1),
    }


def _spread_stride_support(
    candidates: Sequence[PageNumberCandidate],
    reading_direction: str,
    gutter_x: float,
) -> int:
    pages_by_offset: Dict[Tuple[str, int], set[int]] = defaultdict(set)
    for candidate in _spread_page_candidates(candidates, reading_direction, gutter_x):
        family = _style_family(candidate.number_style)
        pages_by_offset[(family, candidate.number - 2 * candidate.page_idx)].add(candidate.page_idx)
    return max((len(pages) for pages in pages_by_offset.values()), default=0)


def _spread_page_candidates(
    candidates: Sequence[PageNumberCandidate],
    reading_direction: str,
    gutter_x: float,
) -> List[PageNumberCandidate]:
    adjusted: List[PageNumberCandidate] = []
    for candidate in candidates:
        side = _candidate_side(candidate, gutter_x)
        if side is None:
            continue
        lower_side = "right" if reading_direction == "rtl" else "left"
        lower_number = candidate.number if side == lower_side else candidate.number - 1
        if lower_number <= 0:
            continue
        style = candidate.number_style
        normalized = (
            str(lower_number)
            if _style_family(style) == "arabic"
            else int_to_roman(lower_number, upper=style == "roman_upper")
        )
        adjusted.append(
            PageNumberCandidate(
                page_idx=candidate.page_idx,
                raw_candidate=candidate.raw_candidate,
                normalized_candidate=normalized,
                candidate_type=candidate.candidate_type,
                number_style=style,
                number=lower_number,
                bbox=candidate.bbox,
                source=candidate.source,
                confidence=candidate.confidence,
                score=min(1.0, candidate.score + 0.06),
                outline_level=candidate.outline_level,
                target_pdf_page_1based=candidate.target_pdf_page_1based,
            )
        )
    return adjusted


def _candidate_side(candidate: PageNumberCandidate, gutter_x: float) -> Optional[str]:
    if not candidate.bbox or len(candidate.bbox) < 4:
        return None
    try:
        x0 = float(candidate.bbox[0])
        x1 = float(candidate.bbox[2])
    except (TypeError, ValueError):
        return None
    if max(abs(x0), abs(x1)) > 1.5:
        x0 /= 1000.0
        x1 /= 1000.0
    center = (x0 + x1) / 2
    margin = 0.08
    if center < gutter_x - margin:
        return "left"
    if center > gutter_x + margin:
        return "right"
    return None


def has_manual_mapping(config: Dict[str, object]) -> bool:
    mapping = config.get("page_mapping") or {}
    if not isinstance(mapping, dict):
        return False
    segments = mapping.get("segments") or []
    return any(isinstance(segment, dict) for segment in segments)


def _normalize_cjk_page_number(value: str) -> Optional[Tuple[str, int, str]]:
    """Parse Chinese page numerals without confusing digit strings with units.

    Modern editions of vertical classics commonly print ``二三四`` for page
    234.  This is deliberately different from the multiplicative ``二百三十四``.
    Both forms are accepted, but they keep distinct styles in the evidence.
    """

    if not value or not _CJK_PAGE_NUMBER_RE.fullmatch(value):
        return None
    if not any(character in _CJK_PAGE_UNITS for character in value):
        digits = "".join(_CJK_PAGE_DIGITS[character] for character in value)
        number = int(digits)
        if number <= 0:
            return None
        return "cjk_decimal", number, str(number)

    total = 0
    current_digit = 0
    for character in value:
        if character in _CJK_PAGE_DIGITS:
            current_digit = int(_CJK_PAGE_DIGITS[character])
            continue
        unit = _CJK_PAGE_UNITS[character]
        total += (current_digit or 1) * unit
        current_digit = 0
    total += current_digit
    if total <= 0:
        return None
    return "cjk_multiplicative", total, str(total)


def normalize_page_candidate(text: object) -> Optional[Tuple[str, int, str]]:
    """Normalize one OCR text fragment into a page number if possible."""

    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not raw:
        return None
    raw = raw.replace("\u3000", " ")
    raw = re.sub(r"\s+", " ", raw)
    if len(raw) > int(AUTO_PAGE_MAPPING_THRESHOLDS["max_candidate_chars"]):
        return None
    value = raw.strip(" \t\r\n-—–~·•*[]()（）【】〈〉《》")
    page_match = re.fullmatch(r"第\s*([0-9IlIoO|]+|[ivxlcdmIVXLCDM]+)\s*页", value)
    if page_match:
        value = page_match.group(1)
    page_match = re.fullmatch(r"(?:Page|页)\s*([0-9IlIoO|]+)", value, flags=re.IGNORECASE)
    if page_match:
        value = page_match.group(1)
    value = value.strip(" \t\r\n-—–~·•*[]()（）【】")
    value = re.sub(r"^[Pp]\.?\s*", "", value)
    value = re.sub(r"\s+", "", value)
    if not value:
        return None

    cjk_number = _normalize_cjk_page_number(value)
    if cjk_number is not None:
        return cjk_number

    numeric_context = re.sub(r"[Il|]", "1", value)
    numeric_context = re.sub(r"[Oo]", "0", numeric_context)
    if re.fullmatch(r"\d{1,5}", numeric_context):
        number = int(numeric_context)
        if number <= 0:
            return None
        return "arabic", number, str(number)

    roman_value = _roman_to_int(value)
    if roman_value is not None:
        style = "roman_upper" if value.isupper() else "roman_lower"
        return style, roman_value, int_to_roman(roman_value, upper=style == "roman_upper")
    return None


def normalize_numeric_bookmark_title(text: object) -> Optional[Tuple[str, int, str]]:
    """Normalize a numeric page bookmark without accepting chapter numbers."""

    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    raw = re.sub(r"\s+", " ", raw)
    if not raw or len(raw) > int(AUTO_PAGE_MAPPING_THRESHOLDS["max_candidate_chars"]):
        return None
    patterns = (
        r"0*([1-9]\d{0,4})",
        r"第\s*0*([1-9]\d{0,4})\s*页",
        r"[Pp]\.?\s*0*([1-9]\d{0,4})",
        r"[Pp]age\s*0*([1-9]\d{0,4})",
        r"页\s*0*([1-9]\d{0,4})",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, raw, flags=re.IGNORECASE)
        if match:
            number = int(match.group(1))
            return "arabic", number, str(number)
    return None


def extract_pdf_numeric_bookmark_candidates(path: Path) -> List[PageNumberCandidate]:
    """Read numeric PDF outlines as 0-based page mapping candidates.

    PyMuPDF's simple TOC returns a 1-based target page.  The explicit minus
    one conversion here is intentionally covered by tests.
    """

    try:
        import fitz  # type: ignore
    except Exception:
        return []
    try:
        document = fitz.open(str(path))
    except Exception:
        return []
    candidates: List[PageNumberCandidate] = []
    try:
        toc = document.get_toc(simple=True) or []
        page_count = len(document)
        for entry in toc:
            if not isinstance(entry, (list, tuple)) or len(entry) < 3:
                continue
            try:
                outline_level = int(entry[0])
                target_page_1based = int(entry[2])
            except (TypeError, ValueError):
                continue
            page_idx = target_page_1based - 1
            if page_idx < 0 or page_idx >= page_count:
                continue
            normalized = normalize_numeric_bookmark_title(entry[1])
            if normalized is None:
                continue
            style, number, normalized_text = normalized
            candidates.append(
                PageNumberCandidate(
                    page_idx=page_idx,
                    raw_candidate=str(entry[1]).strip(),
                    normalized_candidate=normalized_text,
                    candidate_type="numeric_bookmark",
                    number_style=style,
                    number=number,
                    bbox=None,
                    source="pdf_outline",
                    confidence=1.0,
                    score=1.0,
                    outline_level=outline_level,
                    target_pdf_page_1based=target_page_1based,
                )
            )
    finally:
        document.close()
    return sorted(candidates, key=lambda item: (item.page_idx, item.number))


def extract_pdf_page_label_candidates(path: Path) -> List[PageNumberCandidate]:
    """Read explicit PDF Page Label rules as high-priority candidates."""

    try:
        import fitz  # type: ignore
    except Exception:
        return []
    try:
        document = fitz.open(str(path))
    except Exception:
        return []
    candidates: List[PageNumberCandidate] = []
    try:
        if not (document.get_page_labels() or []):
            return []
        for page_idx in range(len(document)):
            page = document.load_page(page_idx)
            raw_label = page.get_label() if getattr(page, "get_label", None) else None
            normalized = normalize_page_candidate(raw_label)
            if normalized is None:
                continue
            style, number, normalized_text = normalized
            candidates.append(
                PageNumberCandidate(
                    page_idx=page_idx,
                    raw_candidate=str(raw_label).strip(),
                    normalized_candidate=normalized_text,
                    candidate_type="pdf_page_label",
                    number_style=style,
                    number=number,
                    bbox=None,
                    source="pdf_page_labels",
                    confidence=1.0,
                    score=1.0,
                )
            )
    finally:
        document.close()
    return candidates


def extract_native_pdf_edge_candidates(
    pages: Sequence[Dict[str, object]],
    *,
    thresholds: Optional[Dict[str, float]] = None,
) -> List[PageNumberCandidate]:
    """Extract page numbers from horizontal edges and vertical outer margins."""

    thresholds = {**AUTO_PAGE_MAPPING_THRESHOLDS, **(thresholds or {})}
    candidates: List[PageNumberCandidate] = []
    seen: set[Tuple[int, int, str]] = set()
    for page in pages:
        try:
            page_idx = int(page.get("pdf_page_index"))
        except (TypeError, ValueError):
            continue
        width = _positive_float(page.get("page_width"))
        height = _positive_float(page.get("page_height"))
        blocks = page.get("blocks") or []
        if not isinstance(blocks, list):
            continue
        bbox_width, bbox_height = _layout_bbox_scale(
            blocks,
            width or 1000.0,
            height or 1000.0,
        )
        page_items: List[Tuple[str, List[float], bool]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bbox = _normalized_page_bbox(block, bbox_width, bbox_height)
            is_outer_vertical = _is_outer_lower_margin_bbox(bbox, thresholds)
            if not _is_edge_bbox(bbox, thresholds) and not is_outer_vertical:
                continue
            text = str(block.get("text") or "").strip()
            for raw_piece in _candidate_text_pieces(text, "edge_short_text"):
                page_items.append((str(raw_piece), bbox or [], False))
        page_items.extend(
            (text, bbox, True)
            for text, bbox in _merged_vertical_cjk_digit_blocks(
                blocks, bbox_width, bbox_height, thresholds
            )
        )
        for raw_piece, bbox, is_merged_vertical in page_items:
            normalized = normalize_page_candidate(raw_piece)
            if normalized is None:
                continue
            style, number, normalized_text = normalized
            if style == "arabic" and 1800 <= number <= 2099:
                continue
            compact = re.sub(r"\s+", "", str(raw_piece))
            if re.search(r"ISBN", compact, flags=re.IGNORECASE) or len(re.sub(r"\D", "", compact)) > 5:
                continue
            key = (page_idx, number, style)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                PageNumberCandidate(
                    page_idx=page_idx,
                    raw_candidate=str(raw_piece).strip(),
                    normalized_candidate=normalized_text,
                    candidate_type="native_edge_text",
                    number_style=style,
                    number=number,
                    bbox=bbox,
                    source="native_pdf_edge_text",
                    confidence=0.9 if style.startswith("cjk_") else 0.85,
                    score=(
                        0.96
                        if is_merged_vertical
                        else 0.9
                        if _is_outer_lower_margin_bbox(bbox, thresholds)
                        else 0.82
                    ),
                )
            )
    return sorted(candidates, key=lambda item: (item.page_idx, -item.score, item.number))


def extract_mineru_page_number_candidates(
    segments: Sequence[Dict[str, object]],
    *,
    thresholds: Optional[Dict[str, float]] = None,
) -> List[PageNumberCandidate]:
    """Extract page-number candidates from MinerU structured result files."""

    thresholds = {**AUTO_PAGE_MAPPING_THRESHOLDS, **(thresholds or {})}
    candidates: List[PageNumberCandidate] = []
    seen: set[Tuple[int, str, str, str]] = set()
    for segment in segments:
        start_1based, _ = _segment_page_range(segment)
        result_dir = Path(str(segment.get("result_dir") or ""))
        if not result_dir.exists():
            continue
        for path, source in _mineru_structured_paths(result_dir):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            for local_page_idx, block in _iter_page_blocks(data):
                page_idx = start_1based - 1 + local_page_idx
                block_type = str(block.get("type") or "").strip()
                text = _block_text(block)
                if not text:
                    continue
                bbox = _bbox(block.get("bbox"))
                for raw_piece in _candidate_text_pieces(text, block_type):
                    normalized = normalize_page_candidate(raw_piece)
                    if normalized is None:
                        continue
                    style, number, normalized_text = normalized
                    candidate_type = _candidate_type(block_type)
                    if candidate_type == "edge_short_text" and not (
                        _is_edge_bbox(bbox, thresholds)
                        or _is_outer_lower_margin_bbox(bbox, thresholds)
                    ):
                        continue
                    score = _candidate_score(candidate_type, raw_piece, bbox, thresholds)
                    if score <= 0:
                        continue
                    key = (page_idx, normalized_text, candidate_type, source)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        PageNumberCandidate(
                            page_idx=page_idx,
                            raw_candidate=str(raw_piece).strip(),
                            normalized_candidate=normalized_text,
                            candidate_type=candidate_type,
                            number_style=style,
                            number=number,
                            bbox=bbox,
                            source=source,
                            confidence=float(block.get("confidence") or block.get("score") or 0.0),
                            score=score,
                        )
                    )
    return sorted(candidates, key=lambda item: (item.page_idx, -item.score, item.number))


def infer_auto_page_mapping(
    candidates: Sequence[PageNumberCandidate],
    page_count: int,
    *,
    page_texts: Optional[Dict[int, str]] = None,
    thresholds: Optional[Dict[str, float]] = None,
    layout_detection: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Fit stable citation-page segments from extracted candidates."""

    thresholds = {**AUTO_PAGE_MAPPING_THRESHOLDS, **(thresholds or {})}
    layout = dict(layout_detection or {})
    spread_mode = layout.get("layout_mode") == "spread"
    logical_pages_per_pdf = 2 if spread_mode else 1
    sequence_candidates = (
        _spread_page_candidates(
            candidates,
            "rtl" if layout.get("reading_direction") == "rtl" else "ltr",
            float(layout.get("gutter_x") or 0.5),
        )
        if spread_mode
        else list(candidates)
    )
    suggestions = _fit_sequence_segments(
        sequence_candidates,
        max(0, int(page_count)),
        page_texts or {},
        thresholds,
        logical_pages_per_pdf=logical_pages_per_pdf,
        layout_detection=layout,
    )
    selected = _select_non_overlapping_segments(suggestions)
    applied = [segment for segment in selected if segment.get("confidence_level") == "high"]
    mapped_pages = _mapped_page_indices(applied)
    exception_pages = sorted(
        {
            candidate.page_idx
            for candidate in candidates
            if candidate.page_idx not in mapped_pages and candidate.score >= 0.6
        }
    )
    methods = {str(segment.get("method") or "") for segment in applied}
    overall_method = methods.pop() if len(methods) == 1 else ("combined_sequence" if methods else "uncalibrated")
    return {
        "method": overall_method,
        "candidate_count": len(candidates),
        "sequence_candidate_count": len(sequence_candidates),
        "segments": suggestions,
        "selected_segments": selected,
        "applied_segments": applied,
        "applied_segment_count": len(applied),
        "exception_pages": exception_pages[:100],
        "thresholds": thresholds,
        "layout_detection": layout,
    }


def apply_auto_mapping_to_pages(pages: List[Dict[str, object]], auto_mapping: Dict[str, object]) -> None:
    """Apply high-confidence automatic segments to page records in-place."""

    segments = [segment for segment in auto_mapping.get("applied_segments", []) if isinstance(segment, dict)]
    if not segments:
        return
    by_index = {int(page.get("pdf_page_index") or 0): page for page in pages}
    for segment in segments:
        start = int(segment["pdf_page_start"])
        end = int(segment["pdf_page_end"])
        citation_start = int(segment["citation_page_start"])
        style = str(segment.get("number_style") or "arabic")
        layout_mode = "spread" if segment.get("layout_mode") == "spread" else "single"
        logical_pages_per_pdf = 2 if layout_mode == "spread" else 1
        reading_direction = "rtl" if segment.get("reading_direction") == "rtl" else "ltr"
        try:
            gutter_x = float(segment.get("gutter_x") or 0.5)
        except (TypeError, ValueError):
            gutter_x = 0.5
        if not 0.3 <= gutter_x <= 0.7:
            gutter_x = 0.5
        evidence = segment.get("mapping_evidence") or {}
        for page_idx in range(start, end + 1):
            page = by_index.get(page_idx)
            if page is None:
                continue
            offset = (page_idx - start) * logical_pages_per_pdf
            citation_number = citation_start + offset
            citation_number_end = citation_number + logical_pages_per_pdf - 1
            citation_label = _format_citation_label(
                citation_number, style, str(segment.get("page_scope") or "body")
            )
            citation_label_end = _format_citation_label(
                citation_number_end, style, str(segment.get("page_scope") or "body")
            )
            method = str(segment.get("method") or "ocr_sequence")
            confidence = float(segment.get("mapping_confidence") or 0.0)
            page["citation_page"] = citation_label
            page["citation_page_start"] = citation_label
            page["citation_page_end"] = citation_label_end
            page["printed_page"] = citation_label
            page["printed_page_start"] = citation_label
            page["printed_page_end"] = citation_label_end
            page["page_mapping_method"] = method
            page["page_mapping_confidence"] = confidence
            page["layout_mode"] = layout_mode
            page["reading_direction"] = reading_direction
            page["gutter_x"] = gutter_x
            page["page_scope"] = segment.get("page_scope")
            page["citation_page_number"] = citation_number
            page["citation_page_number_start"] = citation_number
            page["citation_page_number_end"] = citation_number_end
            page["citation_page_label"] = citation_label
            page["citation_page_label_start"] = citation_label
            page["citation_page_label_end"] = citation_label_end
            page["mapping_method"] = method
            page["mapping_confidence"] = confidence
            page["mapping_confidence_level"] = segment.get("confidence_level")
            page["mapping_evidence"] = evidence
            page["segment_id"] = segment.get("segment_id")


def _fit_sequence_segments(
    candidates: Sequence[PageNumberCandidate],
    page_count: int,
    page_texts: Dict[int, str],
    thresholds: Dict[str, float],
    *,
    logical_pages_per_pdf: int = 1,
    layout_detection: Optional[Dict[str, object]] = None,
) -> List[Dict[str, object]]:
    best_by_page_style: Dict[Tuple[int, str], PageNumberCandidate] = {}
    for candidate in candidates:
        if candidate.page_idx < 0 or (page_count and candidate.page_idx >= page_count):
            continue
        if candidate.number <= 0:
            continue
        key = (candidate.page_idx, _style_family(candidate.number_style))
        existing = best_by_page_style.get(key)
        if existing is None or candidate.score > existing.score:
            best_by_page_style[key] = candidate

    by_offset: Dict[Tuple[str, int], List[PageNumberCandidate]] = defaultdict(list)
    for candidate in best_by_page_style.values():
        family = _style_family(candidate.number_style)
        by_offset[
            (family, candidate.number - logical_pages_per_pdf * candidate.page_idx)
        ].append(candidate)

    suggestions: List[Dict[str, object]] = []
    for (family, offset), group in by_offset.items():
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda item: item.page_idx)
        numeric_bookmark_group = all(item.candidate_type == "numeric_bookmark" for item in group)
        max_gap = thresholds["max_bookmark_cluster_gap"] if numeric_bookmark_group else thresholds["max_cluster_gap"]
        for cluster in _split_clusters(group, int(max_gap)):
            segment = _cluster_to_segment(
                cluster,
                family,
                offset,
                page_count,
                page_texts,
                thresholds,
                logical_pages_per_pdf=logical_pages_per_pdf,
                layout_detection=layout_detection,
            )
            if segment:
                suggestions.append(segment)
    _trim_conflicting_backfills(suggestions)
    suggestions.sort(
        key=lambda item: (
            -float(item.get("mapping_confidence") or 0.0),
            -int(item.get("observed_page_numbers") or 0),
            int(item.get("pdf_page_start") or 0),
        )
    )
    return suggestions


def _trim_conflicting_backfills(segments: Sequence[Dict[str, object]]) -> None:
    """Keep a late local offset from being extrapolated across earlier evidence."""

    reliable = [
        segment
        for segment in segments
        if segment.get("confidence_level") == "high"
        and isinstance(segment.get("mapping_evidence"), dict)
    ]
    for segment in reliable:
        evidence = segment["mapping_evidence"]
        start = int(segment.get("pdf_page_start") or 0)
        end = int(segment.get("pdf_page_end") or start)
        observed_start = int(evidence.get("observed_page_start") or start)
        if start >= observed_start:
            continue
        offset = int(evidence.get("inferred_offset") or 0)
        family = _style_family(str(segment.get("number_style") or "arabic"))
        same_offset_support = 0
        conflicting_offset_support = 0
        for other in reliable:
            if other is segment:
                continue
            other_evidence = other.get("mapping_evidence") or {}
            if _style_family(str(other.get("number_style") or "arabic")) != family:
                continue
            other_observed_end = int(other_evidence.get("observed_page_end") or -1)
            if not start <= other_observed_end < observed_start:
                continue
            support = int(other.get("observed_page_numbers") or 0)
            if int(other_evidence.get("inferred_offset") or 0) == offset:
                same_offset_support += support
            else:
                conflicting_offset_support += support
        if conflicting_offset_support <= same_offset_support:
            continue

        logical_pages_per_pdf = int(evidence.get("logical_pages_per_pdf") or 1)
        citation_start = logical_pages_per_pdf * observed_start + offset
        citation_end = citation_start + logical_pages_per_pdf * (end - observed_start + 1) - 1
        old_start = start
        segment["pdf_page_start"] = observed_start
        segment["citation_page_start"] = str(citation_start)
        segment["citation_page_end"] = str(citation_end)
        segment["segment_id"] = str(segment.get("segment_id") or "").replace(
            f"-{old_start:06d}-", f"-{observed_start:06d}-", 1
        )
        evidence["sequence_density"] = round(
            int(segment.get("observed_page_numbers") or 0) / max(1, end - observed_start + 1),
            4,
        )
        evidence["backfill_trimmed_due_to_conflicting_offset"] = True
        evidence["same_offset_predecessor_support"] = same_offset_support
        evidence["conflicting_offset_predecessor_support"] = conflicting_offset_support


def _cluster_to_segment(
    cluster: Sequence[PageNumberCandidate],
    family: str,
    offset: int,
    page_count: int,
    page_texts: Dict[int, str],
    thresholds: Dict[str, float],
    *,
    logical_pages_per_pdf: int = 1,
    layout_detection: Optional[Dict[str, object]] = None,
) -> Optional[Dict[str, object]]:
    unique: Dict[int, PageNumberCandidate] = {}
    for candidate in cluster:
        existing = unique.get(candidate.page_idx)
        if existing is None or candidate.score > existing.score:
            unique[candidate.page_idx] = candidate
    observed = sorted(unique.values(), key=lambda item: item.page_idx)
    support = len(observed)
    if support < 2:
        return None
    first = observed[0]
    last = observed[-1]
    first_citation = first.number
    toc_backfill_suppressed = False
    if logical_pages_per_pdf == 1:
        start = first.page_idx - (first_citation - 1)
        if start < 0:
            start = first.page_idx
        elif first_citation > 2 and any(
            _has_toc_heading(page_texts.get(page_idx, ""))
            for page_idx in range(
                max(0, first.page_idx - int(thresholds["max_cluster_gap"])),
                first.page_idx + 1,
            )
        ):
            # Do not extrapolate an OCR sequence backwards across a nearby
            # contents page.  Bound volumes often omit or duplicate a leaf at
            # this transition, so the arithmetic offset is not trustworthy
            # before the first observed page number.
            start = first.page_idx
            toc_backfill_suppressed = True
    else:
        # Spread scans often include unnumbered covers and half-title pages.
        # Start at the first reliable physical spread instead of inventing a
        # mapping back to logical page 1 across those front-matter images.
        start = first.page_idx
    end = last.page_idx
    if page_count:
        end = min(end, page_count - 1)
    if end < start:
        return None

    span = max(1, end - start + 1)
    density = round(support / span, 4)
    consistency = _observed_sequence_consistency(
        observed, logical_pages_per_pdf=logical_pages_per_pdf
    )
    avg_score = sum(candidate.score for candidate in observed) / support
    source_counts = Counter(candidate.source for candidate in observed)
    type_counts = Counter(candidate.candidate_type for candidate in observed)
    structure = _structure_signal(page_texts, start, end, family)
    confidence = _segment_confidence(support, consistency, avg_score, bool(structure), thresholds)
    level = _confidence_level(confidence, support, thresholds)
    has_page_labels = any(candidate.candidate_type == "pdf_page_label" for candidate in observed)
    has_numeric_bookmarks = any(candidate.candidate_type == "numeric_bookmark" for candidate in observed)
    has_native_edges = any(candidate.source == "native_pdf_edge_text" for candidate in observed)
    if has_page_labels:
        method = "pdf_page_label"
    elif has_numeric_bookmarks:
        method = "numeric_bookmark_sequence"
    elif has_native_edges:
        method = "native_pdf_edge_sequence"
    else:
        method = "ocr_sequence_with_structure" if structure else "ocr_sequence"
    citation_start = logical_pages_per_pdf * start + offset
    if citation_start < 1 and logical_pages_per_pdf == 1:
        start += 1 - citation_start
        citation_start = 1
    if end < start:
        return None
    page_scope = structure or ("front_matter" if family.startswith("roman") else "body")
    observed_pairs = [
        {
            "pdf_page_index": c.page_idx,
            "target_pdf_page_1based": c.target_pdf_page_1based,
            "raw_candidate": c.raw_candidate,
            "candidate": c.normalized_candidate,
            "outline_level": c.outline_level,
            "source": c.source,
        }
        for c in observed[:30]
    ]
    evidence = {
        "observed_page_numbers": support,
        "observed_examples": observed_pairs,
        "sequence_consistency": consistency,
        "sequence_density": density,
        "inferred_offset": offset,
        "observed_page_start": first.page_idx,
        "observed_page_end": last.page_idx,
        "average_candidate_score": round(avg_score, 4),
        "candidate_sources": dict(source_counts),
        "candidate_types": dict(type_counts),
        "structure_evidence": page_scope if structure else None,
        "logical_pages_per_pdf": logical_pages_per_pdf,
    }
    if toc_backfill_suppressed:
        evidence["backfill_suppressed_near_toc"] = True
    layout = dict(layout_detection or {})
    if logical_pages_per_pdf == 2:
        evidence["layout_detection"] = layout.get("evidence") or {}
        if layout.get("confidence_level") != "high" and level == "high":
            level = "medium"
            confidence = min(confidence, float(thresholds["high_confidence"]) - 0.01)
    segment_id = (
        f"AUTO-{'spread-' if logical_pages_per_pdf == 2 else ''}"
        f"{family}-{start:06d}-{end:06d}-{offset:+d}"
    )
    segment = {
        "segment_id": segment_id,
        "pdf_page_start": start,
        "pdf_page_end": end,
        "citation_page_start": str(citation_start),
        "citation_page_end": str(
            citation_start
            + logical_pages_per_pdf * (end - start)
            + logical_pages_per_pdf
            - 1
        ),
        "number_style": _segment_number_style(observed, family),
        "page_scope": page_scope,
        "method": method,
        "mapping_method": method,
        "mapping_confidence": round(confidence, 4),
        "confidence_level": level,
        "observed_page_numbers": support,
        "mapping_evidence": evidence,
    }
    if logical_pages_per_pdf == 2:
        segment.update(
            {
                "layout_mode": "spread",
                "reading_direction": "rtl"
                if layout.get("reading_direction") == "rtl"
                else "ltr",
                "gutter_x": float(layout.get("gutter_x") or 0.5),
            }
        )
    return segment


def _segment_confidence(
    support: int,
    consistency: float,
    avg_score: float,
    has_structure: bool,
    thresholds: Dict[str, float],
) -> float:
    support_score = min(0.36, support * 0.055)
    confidence = 0.28 + support_score + 0.28 * min(1.0, consistency * 2.0) + 0.22 * min(1.0, avg_score)
    if has_structure:
        confidence += 0.04
    return min(0.99, round(confidence, 4))


def _observed_sequence_consistency(
    observed: Sequence[PageNumberCandidate],
    *,
    logical_pages_per_pdf: int = 1,
) -> float:
    if len(observed) <= 1:
        return 0.0
    good = 0
    total = 0
    previous = observed[0]
    for candidate in observed[1:]:
        page_gap = candidate.page_idx - previous.page_idx
        number_gap = candidate.number - previous.number
        if page_gap > 0:
            total += 1
            if page_gap * logical_pages_per_pdf == number_gap:
                good += 1
        previous = candidate
    return round(good / max(total, 1), 4)


def _confidence_level(confidence: float, support: int, thresholds: Dict[str, float]) -> str:
    if support >= int(thresholds["min_high_support"]) and confidence >= thresholds["high_confidence"]:
        return "high"
    if support >= int(thresholds["min_medium_support"]) and confidence >= thresholds["medium_confidence"]:
        return "medium"
    return "low"


def _select_non_overlapping_segments(segments: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    selected: List[Dict[str, object]] = []
    occupied: set[int] = set()
    level_rank = {"high": 3, "medium": 2, "low": 1}
    ranked = sorted(
        segments,
        key=lambda item: (
            -level_rank.get(str(item.get("confidence_level")), 0),
            -float(item.get("mapping_confidence") or 0.0),
            -int(item.get("observed_page_numbers") or 0),
        ),
    )
    for segment in ranked:
        start = int(segment.get("pdf_page_start") or 0)
        end = int(segment.get("pdf_page_end") or start)
        pages = set(range(start, end + 1))
        if pages.intersection(occupied):
            continue
        selected.append(segment)
        occupied.update(pages)
    selected.sort(key=lambda item: int(item.get("pdf_page_start") or 0))
    return selected


def _mapped_page_indices(segments: Sequence[Dict[str, object]]) -> set[int]:
    pages: set[int] = set()
    for segment in segments:
        try:
            start = int(segment["pdf_page_start"])
            end = int(segment["pdf_page_end"])
        except (KeyError, TypeError, ValueError):
            continue
        pages.update(range(start, end + 1))
    return pages


def _split_clusters(group: Sequence[PageNumberCandidate], max_gap: int) -> List[List[PageNumberCandidate]]:
    clusters: List[List[PageNumberCandidate]] = []
    current: List[PageNumberCandidate] = []
    previous: Optional[PageNumberCandidate] = None
    for candidate in group:
        if previous is not None and candidate.page_idx - previous.page_idx > max_gap:
            if current:
                clusters.append(current)
            current = []
        current.append(candidate)
        previous = candidate
    if current:
        clusters.append(current)
    return clusters


def _style_family(style: str) -> str:
    return "roman" if str(style).startswith("roman") else "arabic"


def _segment_number_style(observed: Sequence[PageNumberCandidate], family: str) -> str:
    if family == "arabic":
        return "arabic"
    styles = Counter(candidate.number_style for candidate in observed)
    return styles.most_common(1)[0][0] if styles else "roman_lower"


def _format_citation_label(number: int, style: str, scope: str) -> str:
    if style == "roman_lower":
        label = int_to_roman(number, upper=False)
    elif style == "roman_upper":
        label = int_to_roman(number, upper=True)
    else:
        label = str(number)
    if scope == "preface":
        return f"序言第{label}页"
    return label


_BODY_HEADING_RE = re.compile(r"第\s*(?:一|1)\s*[章编部节卷]")
# 整行标题才算序言；正文里的“无序/秩序/顺序”等子串不再误触发。
_PREFACE_HEADING_RE = re.compile(r"^(?:译者|作者)?[自代译]?序言?$|^前言$|^出版说明$")
_NON_PREFACE_SEQUENCE_WORDS = {"无序", "無序", "秩序", "顺序", "順序", "次序", "工序"}
_TOC_HEADING_RE = re.compile(r"^(?:目[录錄]|目录|目錄)$")
_MAX_PREFACE_SPAN_PAGES = 40


def _has_preface_heading(text: str) -> bool:
    for line in text.split("\n")[:8]:
        compact = re.sub(r"[\s　]+", "", line)
        if compact and _PREFACE_HEADING_RE.match(compact):
            return True
        if (
            2 <= len(compact) <= 18
            and compact.endswith("序")
            and compact not in _NON_PREFACE_SEQUENCE_WORDS
        ):
            return True
    return False


def _has_toc_heading(text: str) -> bool:
    lines = [
        re.sub(r"[\s　]+", "", line)
        # Vertical ancient-book OCR often emits the visual title after many
        # catalogue-entry columns even when it is prominent on the page.
        for line in text.split("\n")[:40]
        if re.sub(r"[\s　]+", "", line)
    ]
    candidates = list(lines)
    candidates.extend(lines[index] + lines[index + 1] for index in range(len(lines) - 1))
    return any(
        _TOC_HEADING_RE.fullmatch(candidate)
        or (2 <= len(candidate) <= 18 and candidate.endswith(("目录", "目錄")))
        for candidate in candidates
    )


def _structure_signal(page_texts: Dict[int, str], start: int, end: int, family: str) -> Optional[str]:
    # 只采样分段内部的页面：起点之前的目录/序言页不能替正文分段做分类。
    pages = [str(page_texts.get(page, ""))[:600] for page in range(start, min(end + 1, start + 8))]
    sample = "\n".join(pages)
    if not sample.strip():
        return "front_matter" if family == "roman" else None
    span_ok_for_preface = end - start + 1 <= _MAX_PREFACE_SPAN_PAGES
    # 分段首页的整行"序/序言/前言"扉页标题是最强信号，优先于窗口内
    # 其他页面 OCR 噪声里可能混入的正文标题字样。
    if span_ok_for_preface and any(_has_preface_heading(text) for text in pages[:2]):
        return "preface"
    if span_ok_for_preface and any(_has_toc_heading(text) for text in pages[:2]):
        return "front_matter"
    if family.startswith("roman"):
        # 中文书籍的罗马页码段属于前置部分；目录页上的"第一章……"
        # 等条目不代表正文，不参与正文判定。
        if span_ok_for_preface and any(_has_preface_heading(text) for text in pages):
            return "preface"
        return "front_matter"
    if _BODY_HEADING_RE.search(sample):
        return "body"
    if any(keyword in sample for keyword in STRUCTURE_KEYWORDS["intro"]):
        return "body"
    if span_ok_for_preface and any(_has_preface_heading(text) for text in pages):
        return "preface"
    return None


def _segment_page_range(segment: Dict[str, object]) -> Tuple[int, int]:
    value = str(segment.get("page_ranges") or "")
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", value)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.fullmatch(r"\s*(\d+)\s*", value)
    if match:
        page = int(match.group(1))
        return page, page
    start = int(segment.get("page_start") or segment.get("pdf_page_start_1based") or 1)
    end = int(segment.get("page_end") or segment.get("pdf_page_end_1based") or start)
    return start, end


def _mineru_structured_paths(result_dir: Path) -> List[Tuple[Path, str]]:
    patterns = [
        ("*_content_list_v2.json", "content_list_v2"),
        ("content_list_v2.json", "content_list_v2"),
        ("*_model.json", "model"),
        ("model.json", "model"),
        ("*_middle.json", "middle"),
        ("middle.json", "middle"),
        ("*_content_list.json", "content_list"),
        ("content_list.json", "content_list"),
    ]
    paths: List[Tuple[Path, str]] = []
    seen: set[Path] = set()
    for pattern, source in patterns:
        for path in sorted(result_dir.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            paths.append((path, source))
    return paths


def _iter_page_blocks(data: object) -> Iterable[Tuple[int, Dict[str, object]]]:
    if isinstance(data, list):
        if data and all(isinstance(item, list) for item in data):
            for page_idx, page in enumerate(data):
                for block in page:
                    if isinstance(block, dict):
                        yield page_idx, block
            return
        for index, item in enumerate(data):
            if isinstance(item, dict) and ("layout_dets" in item or "blocks" in item):
                page_idx = _safe_int(item.get("page_idx"), index)
                for block in item.get("layout_dets") or item.get("blocks") or []:
                    if isinstance(block, dict):
                        yield page_idx, block
            elif isinstance(item, dict):
                page_idx = _safe_int(item.get("page_idx"), 0)
                yield page_idx, item
        return
    if isinstance(data, dict):
        pages = data.get("pdf_info") or data.get("pages") or data.get("page_info") or []
        if isinstance(pages, list):
            for index, page in enumerate(pages):
                if not isinstance(page, dict):
                    continue
                page_idx = _safe_int(page.get("page_idx"), index)
                blocks = page.get("layout_dets") or page.get("blocks") or page.get("para_blocks") or []
                for block in blocks:
                    if isinstance(block, dict):
                        yield page_idx, block


def _block_text(block: Dict[str, object]) -> str:
    parts: List[str] = []
    _collect_text(block.get("content"), parts)
    _collect_text(block.get("text"), parts)
    if not parts and isinstance(block.get("lines"), list):
        _collect_text(block.get("lines"), parts)
    return "\n".join(part for part in parts if part).strip()


def _collect_text(value: object, parts: List[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            parts.append(value.strip())
        return
    if isinstance(value, list):
        for item in value:
            _collect_text(item, parts)
        return
    if isinstance(value, dict):
        for key in ("content", "text"):
            if key in value:
                _collect_text(value.get(key), parts)
        for key, item in value.items():
            if key in {"content", "text"}:
                continue
            if key.endswith("_content") or key in {"spans", "lines"}:
                _collect_text(item, parts)


def _candidate_text_pieces(text: str, block_type: str) -> Iterable[str]:
    if not text:
        return []
    pieces = [piece.strip() for piece in re.split(r"[\r\n]+", text) if piece.strip()]
    if _candidate_type(block_type) in {"page_number", "header_footer", "discarded_block"}:
        return pieces or [text]
    short = []
    for piece in pieces or [text]:
        if len(piece) <= int(AUTO_PAGE_MAPPING_THRESHOLDS["max_candidate_chars"]):
            short.append(piece)
    return short


def _candidate_type(block_type: str) -> str:
    value = str(block_type or "").lower()
    if "page_number" in value or value == "index":
        return "page_number"
    if "header" in value or "footer" in value:
        return "header_footer"
    if "discard" in value:
        return "discarded_block"
    return "edge_short_text"


def _candidate_score(
    candidate_type: str,
    raw_piece: object,
    bbox: Optional[List[float]],
    thresholds: Dict[str, float],
) -> float:
    score = {
        "page_number": 0.94,
        "header_footer": 0.72,
        "discarded_block": 0.68,
        "edge_short_text": 0.48,
    }.get(candidate_type, 0.0)
    if _is_edge_bbox(bbox, thresholds):
        score += 0.14
    if _is_outer_lower_margin_bbox(bbox, thresholds):
        score += 0.20
    normalized = _normalize_cjk_page_number(re.sub(r"[\s\u3000]+", "", str(raw_piece)))
    if normalized is not None:
        score += 0.10
    if len(str(raw_piece).strip()) <= 6:
        score += 0.06
    return min(1.0, round(score, 4))


def _is_edge_bbox(bbox: Optional[List[float]], thresholds: Dict[str, float]) -> bool:
    if not bbox or len(bbox) < 4:
        return False
    x0, y0, x1, y1 = bbox[:4]
    values = [float(x0), float(y0), float(x1), float(y1)]
    max_value = max(values)
    if max_value <= 1.5:
        return values[1] <= thresholds["edge_ratio"] or values[3] >= 1 - thresholds["edge_ratio"]
    # MinerU v2 boxes are commonly in page coordinates around 1000x1000.
    y_center = (values[1] + values[3]) / 2
    return y_center <= 160 or y_center >= 840


def _is_outer_lower_margin_bbox(
    bbox: Optional[List[float]], thresholds: Dict[str, float]
) -> bool:
    """Recognize the alternating lower side-margin folios used by classics."""

    if not bbox or len(bbox) < 4:
        return False
    try:
        values = [float(value) for value in bbox[:4]]
    except (TypeError, ValueError):
        return False
    if max(abs(value) for value in values) > 1.5:
        values = [value / 1000.0 for value in values]
    x_center = (values[0] + values[2]) / 2
    y_center = (values[1] + values[3]) / 2
    outer = float(thresholds.get("outer_margin_ratio") or 0.16)
    lower = float(thresholds.get("vertical_page_number_min_y") or 0.64)
    return (x_center <= outer or x_center >= 1.0 - outer) and y_center >= lower


def _merged_vertical_cjk_digit_blocks(
    blocks: Sequence[object],
    bbox_width: Optional[float],
    bbox_height: Optional[float],
    thresholds: Dict[str, float],
) -> List[Tuple[str, List[float]]]:
    """Join one-glyph OCR blocks that form a vertical decimal folio.

    The column grouping mirrors the useful part of Novel Formatter's bbox
    reading-order recovery: cluster by x, then read top to bottom.  Joined
    candidates receive a higher score than their component glyphs, allowing the
    sequence fitter to prefer ``二三四`` over three competing 2/3/4 candidates.
    """

    parts: List[Dict[str, object]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = re.sub(r"[\s\u3000]+", "", str(block.get("text") or ""))
        if not _CJK_PAGE_DIGIT_RE.fullmatch(text):
            continue
        bbox = _normalized_page_bbox(block, bbox_width, bbox_height)
        if not _is_outer_lower_margin_bbox(bbox, thresholds):
            continue
        assert bbox is not None
        x_center = (bbox[0] + bbox[2]) / 2
        parts.append(
            {
                "text": text,
                "bbox": bbox,
                "side": "left" if x_center < 0.5 else "right",
                "x_center": x_center,
                "height": max(0.001, bbox[3] - bbox[1]),
            }
        )

    merged: List[Tuple[str, List[float]]] = []
    for side in ("left", "right"):
        side_parts = sorted(
            (part for part in parts if part["side"] == side),
            key=lambda part: (float(part["x_center"]), float(part["bbox"][1])),
        )
        columns: List[List[Dict[str, object]]] = []
        for part in side_parts:
            if not columns:
                columns.append([part])
                continue
            average_x = sum(float(item["x_center"]) for item in columns[-1]) / len(columns[-1])
            if abs(float(part["x_center"]) - average_x) <= 0.04:
                columns[-1].append(part)
            else:
                columns.append([part])
        for column in columns:
            ordered = sorted(column, key=lambda part: float(part["bbox"][1]))
            run: List[Dict[str, object]] = []
            for part in ordered:
                if run:
                    previous = run[-1]
                    gap = float(part["bbox"][1]) - float(previous["bbox"][3])
                    allowed_gap = max(0.05, 1.8 * max(float(part["height"]), float(previous["height"])))
                    if gap > allowed_gap or len(run) >= 6:
                        if len(run) >= 2:
                            merged.append(_merge_cjk_digit_run(run))
                        run = []
                run.append(part)
            if len(run) >= 2:
                merged.append(_merge_cjk_digit_run(run))
    return merged


def _merge_cjk_digit_run(run: Sequence[Dict[str, object]]) -> Tuple[str, List[float]]:
    boxes = [list(item["bbox"]) for item in run]
    return (
        "".join(str(item["text"]) for item in run),
        [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ],
    )


def _bbox(value: object) -> Optional[List[float]]:
    if not isinstance(value, list) or len(value) < 4:
        return None
    try:
        return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]
    except (TypeError, ValueError):
        return None


def _positive_float(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalized_page_bbox(
    block: Dict[str, object],
    page_width: Optional[float],
    page_height: Optional[float],
) -> Optional[List[float]]:
    normalized = _bbox(block.get("bbox_normalized"))
    if normalized:
        return normalized
    raw = _bbox(block.get("bbox"))
    if not raw:
        return None
    if max(raw) <= 1.5:
        return raw
    if not page_width or not page_height:
        return None
    return [raw[0] / page_width, raw[1] / page_height, raw[2] / page_width, raw[3] / page_height]


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _roman_to_int(value: str) -> Optional[int]:
    text = value.strip()
    if not re.fullmatch(r"[ivxlcdmIVXLCDM]{1,12}", text):
        return None
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    previous = 0
    for char in reversed(text.lower()):
        amount = values[char]
        if amount < previous:
            total -= amount
        else:
            total += amount
            previous = amount
    if total <= 0 or total > 5000:
        return None
    canonical = int_to_roman(total, upper=text.isupper())
    if canonical.lower() != text.lower():
        return None
    return total
