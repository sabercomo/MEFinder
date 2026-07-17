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
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .pdf_page_mapping import int_to_roman


AUTO_PAGE_MAPPING_THRESHOLDS: Dict[str, float] = {
    "edge_ratio": 0.14,
    "max_candidate_chars": 18,
    "min_high_support": 4,
    "min_medium_support": 3,
    "max_cluster_gap": 12,
    "max_bookmark_cluster_gap": 250,
    "high_confidence": 0.86,
    "medium_confidence": 0.68,
}

STRUCTURE_KEYWORDS = {
    "toc": ["目录"],
    "preface": ["序", "序言", "前言", "译序", "出版说明"],
    "intro": ["导言", "绪论"],
    "body": ["第一章", "第一编", "第一部", "第一节"],
    "appendix": ["附录", "后记", "索引"],
}


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


def has_manual_mapping(config: Dict[str, object]) -> bool:
    mapping = config.get("page_mapping") or {}
    if not isinstance(mapping, dict):
        return False
    segments = mapping.get("segments") or []
    return any(isinstance(segment, dict) for segment in segments)


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
    """Extract short page-number text from the top/bottom 15% of native PDF pages."""

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
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bbox = _normalized_page_bbox(block, width, height)
            if not _is_edge_bbox(bbox, thresholds):
                continue
            text = str(block.get("text") or "").strip()
            for raw_piece in _candidate_text_pieces(text, "edge_short_text"):
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
                        confidence=0.85,
                        score=0.82,
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
                    if candidate_type == "edge_short_text" and not _is_edge_bbox(bbox, thresholds):
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
) -> Dict[str, object]:
    """Fit stable citation-page segments from extracted candidates."""

    thresholds = {**AUTO_PAGE_MAPPING_THRESHOLDS, **(thresholds or {})}
    suggestions = _fit_sequence_segments(candidates, max(0, int(page_count)), page_texts or {}, thresholds)
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
        "segments": suggestions,
        "selected_segments": selected,
        "applied_segments": applied,
        "applied_segment_count": len(applied),
        "exception_pages": exception_pages[:100],
        "thresholds": thresholds,
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
        evidence = segment.get("mapping_evidence") or {}
        for page_idx in range(start, end + 1):
            page = by_index.get(page_idx)
            if page is None:
                continue
            offset = page_idx - start
            citation_number = citation_start + offset
            citation_label = _format_citation_label(citation_number, style, str(segment.get("page_scope") or "body"))
            method = str(segment.get("method") or "ocr_sequence")
            confidence = float(segment.get("mapping_confidence") or 0.0)
            page["citation_page"] = citation_label
            page["printed_page"] = citation_label
            page["page_mapping_method"] = method
            page["page_mapping_confidence"] = confidence
            page["page_scope"] = segment.get("page_scope")
            page["citation_page_number"] = citation_number
            page["citation_page_label"] = citation_label
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
        by_offset[(family, candidate.number - candidate.page_idx)].append(candidate)

    suggestions: List[Dict[str, object]] = []
    for (family, offset), group in by_offset.items():
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda item: item.page_idx)
        numeric_bookmark_group = all(item.candidate_type == "numeric_bookmark" for item in group)
        max_gap = thresholds["max_bookmark_cluster_gap"] if numeric_bookmark_group else thresholds["max_cluster_gap"]
        for cluster in _split_clusters(group, int(max_gap)):
            segment = _cluster_to_segment(cluster, family, offset, page_count, page_texts, thresholds)
            if segment:
                suggestions.append(segment)
    suggestions.sort(
        key=lambda item: (
            -float(item.get("mapping_confidence") or 0.0),
            -int(item.get("observed_page_numbers") or 0),
            int(item.get("pdf_page_start") or 0),
        )
    )
    return suggestions


def _cluster_to_segment(
    cluster: Sequence[PageNumberCandidate],
    family: str,
    offset: int,
    page_count: int,
    page_texts: Dict[int, str],
    thresholds: Dict[str, float],
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
    start = first.page_idx - (first_citation - 1)
    if start < 0:
        start = first.page_idx
    end = last.page_idx
    if page_count:
        end = min(end, page_count - 1)
    if end < start:
        return None

    span = max(1, end - start + 1)
    density = round(support / span, 4)
    consistency = _observed_sequence_consistency(observed)
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
    citation_start = start + offset
    if citation_start < 1:
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
        "average_candidate_score": round(avg_score, 4),
        "candidate_sources": dict(source_counts),
        "candidate_types": dict(type_counts),
        "structure_evidence": page_scope if structure else None,
    }
    segment_id = f"AUTO-{family}-{start:06d}-{end:06d}-{offset:+d}"
    return {
        "segment_id": segment_id,
        "pdf_page_start": start,
        "pdf_page_end": end,
        "citation_page_start": str(citation_start),
        "citation_page_end": str(citation_start + (end - start)),
        "number_style": _segment_number_style(observed, family),
        "page_scope": page_scope,
        "method": method,
        "mapping_method": method,
        "mapping_confidence": round(confidence, 4),
        "confidence_level": level,
        "observed_page_numbers": support,
        "mapping_evidence": evidence,
    }


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


def _observed_sequence_consistency(observed: Sequence[PageNumberCandidate]) -> float:
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
            if page_gap == number_gap:
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


def _structure_signal(page_texts: Dict[int, str], start: int, end: int, family: str) -> Optional[str]:
    sample = "\n".join(str(page_texts.get(page, ""))[:600] for page in range(max(0, start - 2), min(end + 3, start + 8)))
    if not sample:
        return "front_matter" if family == "roman" else None
    if any(keyword in sample for keyword in STRUCTURE_KEYWORDS["body"]):
        return "body"
    if any(keyword in sample for keyword in STRUCTURE_KEYWORDS["intro"]):
        return "body"
    if any(keyword in sample for keyword in STRUCTURE_KEYWORDS["preface"]):
        return "preface"
    if family == "roman":
        return "front_matter"
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
