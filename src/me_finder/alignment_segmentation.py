"""Pure PDF and EPUB text segmentation with exact source spans."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

from .auto_page_mapping import _layout_bbox_scale, _normalized_page_bbox


MAX_SEGMENT_LENGTH = 1200
_SENTENCE_ENDINGS = frozenset("。！？!?；;")
_CLOSING_PUNCTUATION = frozenset("”’\"'）)]】》〉」』")
_NUMBERED_PARAGRAPH_MARKER_LINE = re.compile(
    r"(?:§\s*[0-9IlOoSs]{1,4}|第\s*[零〇一二两三四五六七八九十百0-9IlOoSs]{1,5}\s*节)\s*[.．、:]?",
    re.IGNORECASE,
)
_STRUCTURAL_MARKER_LINE = re.compile(
    r"(?:"
    r"(?:§\s*[0-9IlOoSs]{1,4}|第\s*[零〇一二两三四五六七八九十百0-9IlOoSs]{1,5}\s*节)"
    r"\s*[.．、:]?"
    r"|第\s*[零〇一二两三四五六七八九十百0-9]{1,5}\s*(?:章|篇|部).*"
    r"|PART\s+(?:ONE|TWO|THREE|FOUR|[IVX]{1,4}|\d{1,2})"
    r")",
    re.IGNORECASE,
)
_ABBREVIATIONS = frozenset(
    {
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "etc",
        "bzw",
        "ca",
        "d.h",
        "u.a",
        "z.b",
    }
)
_NON_ALIGNMENT_BLOCK_ROLES = frozenset(
    {
        "discarded_block",
        "footer",
        "header",
        "page_footnote",
        "page_footer",
        "page_header",
        "page_number",
    }
)
_LAYOUT_NUMBER = re.compile(r"\s*[0-9]{1,4}\s*\Z")


def _is_confirmed_parser_placeholder(text: str) -> bool:
    normalized = " ".join(text.split()).casefold()
    return (
        normalized == "[no text detected]"
        or normalized == "the following table provides the information in english:"
        or normalized.startswith("the image contains no discernible text or characters")
        or (
            normalized.startswith("the ocr result ")
            and " is a hallucination" in normalized
        )
        or normalized.startswith(
            "therefore, the correct ocr output must reflect the absence"
        )
    )


@dataclass(frozen=True)
class PageText:
    page_index: int
    payload: Dict[str, object]
    text: str
    global_start: int
    global_end: int


@dataclass(frozen=True)
class ParagraphText:
    paragraph_id: str
    paragraph_index: int
    payload: Dict[str, object]
    text: str


@dataclass(frozen=True)
class SegmentDraft:
    text: str
    spans: Tuple[Tuple[int, int, int], ...]


@dataclass(frozen=True)
class ParagraphSegmentDraft:
    text: str
    spans: Tuple[Tuple[str, int, int, int], ...]


def _period_ends_sentence(text: str, index: int) -> bool:
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    if before.isdigit() and after.isdigit():
        return False
    if re.search(r"\.\s*\Z", text[:index]) or re.match(r"\s*\.", text[index + 1 :]):
        return False
    word_match = re.search(r"([A-Za-zÀ-ÖØ-öø-ÿ.]+)\Z", text[:index])
    word = word_match.group(1).casefold() if word_match else ""
    if word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
        return False
    return not after or after.isspace() or after in _CLOSING_PUNCTUATION


def _raw_segment_ranges(text: str) -> Iterable[Tuple[int, int]]:
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        boundary = char in _SENTENCE_ENDINGS or (
            char == "." and _period_ends_sentence(text, index)
        )
        if boundary:
            end = index + 1
            while end < len(text) and text[end] in _CLOSING_PUNCTUATION:
                end += 1
            yield start, end
            start = end
            index = end
            continue
        if char == "\n":
            line_start = text.rfind("\n", 0, index) + 1
            if _STRUCTURAL_MARKER_LINE.fullmatch(text[line_start:index].strip()):
                if start < line_start:
                    yield start, line_start
                yield max(start, line_start), index
                index += 1
                start = index
                continue
            match = re.match(r"\n[ \t]*\n+", text[index:])
            if match:
                yield start, index
                index += len(match.group(0))
                start = index
                continue
        index += 1
    yield start, len(text)


def _bounded_ranges(text: str, start: int, end: int) -> Iterable[Tuple[int, int]]:
    cursor = start
    while end - cursor > MAX_SEGMENT_LENGTH:
        limit = cursor + MAX_SEGMENT_LENGTH
        candidates = (
            text.rfind("\n", cursor + 1, limit + 1),
            text.rfind(" ", cursor + 1, limit + 1),
        )
        split = max(candidates)
        if split <= cursor:
            split = limit
        yield cursor, split
        cursor = split
    yield cursor, end


def segment_pdf_text(full_text: str, pages: Sequence[PageText]) -> List[SegmentDraft]:
    """Split reading-order PDF text and retain exact page-codepoint spans."""

    drafts: List[SegmentDraft] = []
    page_cursor = 0
    excluded_ranges: List[Tuple[int, int]] = []
    margin_text_counts: Dict[str, int] = {}
    page_metrics: Dict[int, Tuple[float, float]] = {}
    for page in pages:
        blocks = page.payload.get("blocks")
        if not isinstance(blocks, list):
            continue
        width, height = _layout_bbox_scale(
            blocks,
            float(page.payload.get("page_width") or 1000.0),
            float(page.payload.get("page_height") or 1000.0),
        )
        page_metrics[page.page_index] = (width, height)
        seen: set[str] = set()
        for block in blocks:
            if not isinstance(block, dict):
                continue
            bbox = _normalized_page_bbox(block, width, height)
            text = " ".join(str(block.get("text") or "").split())
            if not text or bbox is None:
                continue
            y_center = (bbox[1] + bbox[3]) / 2.0
            if 0.14 < y_center < 0.86:
                continue
            key = re.sub(r"[0-9]+", "#", text.casefold())
            if len(key) <= 160:
                seen.add(key)
        for key in seen:
            margin_text_counts[key] = margin_text_counts.get(key, 0) + 1
    repeated_margin_texts = {
        key for key, count in margin_text_counts.items() if count >= 3
    }
    for page in pages:
        blocks = page.payload.get("blocks")
        if not isinstance(blocks, list):
            continue
        width, height = page_metrics.get(page.page_index, (1000.0, 1000.0))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            role = str(
                block.get("mineru_type")
                or block.get("parser_type")
                or block.get("type")
                or ""
            ).strip().casefold()
            excluded = role in _NON_ALIGNMENT_BLOCK_ROLES
            text = " ".join(str(block.get("text") or "").split())
            bbox = _normalized_page_bbox(block, width, height)
            if bbox is not None and text:
                x_center = (bbox[0] + bbox[2]) / 2.0
                y_center = (bbox[1] + bbox[3]) / 2.0
                key = re.sub(r"[0-9]+", "#", text.casefold())
                excluded = excluded or bool(
                    key in repeated_margin_texts
                    and _NUMBERED_PARAGRAPH_MARKER_LINE.fullmatch(text) is None
                    and (y_center <= 0.14 or y_center >= 0.86)
                )
                excluded = excluded or bool(
                    _LAYOUT_NUMBER.fullmatch(text)
                    and (
                        x_center < 0.12
                        or x_center > 0.82
                        or y_center < 0.14
                        or y_center > 0.86
                    )
                )
            if not excluded:
                continue
            start = int(block.get("page_char_start") or 0)
            end = int(block.get("page_char_end") or start)
            if 0 <= start < end <= len(page.text):
                excluded_ranges.append(
                    (page.global_start + start, page.global_start + end)
                )
    for raw_start, raw_end in _raw_segment_ranges(full_text):
        clean_ranges = [(raw_start, raw_end)]
        for excluded_start, excluded_end in excluded_ranges:
            next_ranges: List[Tuple[int, int]] = []
            for clean_start, clean_end in clean_ranges:
                if excluded_end <= clean_start or excluded_start >= clean_end:
                    next_ranges.append((clean_start, clean_end))
                    continue
                if clean_start < excluded_start:
                    next_ranges.append((clean_start, excluded_start))
                if excluded_end < clean_end:
                    next_ranges.append((excluded_end, clean_end))
            clean_ranges = next_ranges
        for clean_start, clean_end in clean_ranges:
            for bounded_start, bounded_end in _bounded_ranges(
                full_text, clean_start, clean_end
            ):
                while bounded_start < bounded_end and full_text[bounded_start].isspace():
                    bounded_start += 1
                while bounded_end > bounded_start and full_text[bounded_end - 1].isspace():
                    bounded_end -= 1
                if bounded_start >= bounded_end:
                    continue
                segment_text = full_text[bounded_start:bounded_end]
                if _is_confirmed_parser_placeholder(segment_text):
                    continue
                spans: List[Tuple[int, int, int]] = []
                while (
                    page_cursor < len(pages)
                    and pages[page_cursor].global_end <= bounded_start
                ):
                    page_cursor += 1
                overlap_cursor = page_cursor
                while (
                    overlap_cursor < len(pages)
                    and pages[overlap_cursor].global_start < bounded_end
                ):
                    page = pages[overlap_cursor]
                    overlap_start = max(bounded_start, page.global_start)
                    overlap_end = min(bounded_end, page.global_end)
                    if overlap_end > overlap_start:
                        spans.append(
                            (
                                page.page_index,
                                overlap_start - page.global_start,
                                overlap_end - page.global_start,
                            )
                        )
                    overlap_cursor += 1
                if spans:
                    drafts.append(
                        SegmentDraft(
                            text=segment_text,
                            spans=tuple(spans),
                        )
                    )
    return drafts


def segment_paragraph_text(
    paragraphs: Sequence[ParagraphText],
) -> List[ParagraphSegmentDraft]:
    """Split EPUB paragraphs locally and retain exact paragraph-codepoint spans."""

    drafts: List[ParagraphSegmentDraft] = []
    for paragraph in paragraphs:
        for raw_start, raw_end in _raw_segment_ranges(paragraph.text):
            for start, end in _bounded_ranges(paragraph.text, raw_start, raw_end):
                while start < end and paragraph.text[start].isspace():
                    start += 1
                while end > start and paragraph.text[end - 1].isspace():
                    end -= 1
                if start >= end:
                    continue
                drafts.append(
                    ParagraphSegmentDraft(
                        text=paragraph.text[start:end],
                        spans=((
                            paragraph.paragraph_id,
                            paragraph.paragraph_index,
                            start,
                            end,
                        ),),
                    )
                )
    return drafts
