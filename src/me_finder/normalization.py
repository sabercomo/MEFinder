"""Text normalization helpers for reproducible local search."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import List, Sequence, Tuple


QUOTE_TRANSLATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "＂": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "＇": "'",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
    }
)

PUNCT_TRANSLATION = str.maketrans(
    {
        "。": ".",
        "，": ",",
        "、": ",",
        "；": ";",
        "：": ":",
        "？": "?",
        "！": "!",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "［": "[",
        "］": "]",
        "《": "<",
        "》": ">",
        "〈": "<",
        "〉": ">",
        "〔": "[",
        "〕": "]",
        "—": "-",
        "–": "-",
        "－": "-",
        "―": "-",
        "…": "...",
        "·": ".",
        "　": " ",
    }
)

SENTENCE_ENDINGS = set("。！？!?；;")


def is_invisible_format(ch: str) -> bool:
    """Report characters that carry no visible text but survive NFKC.

    Typeset PDFs are full of them: a hyphenated book leaves a SOFT HYPHEN
    (U+00AD) inside almost every other word, and exports add zero-width
    spaces, word joiners and byte-order marks.  Unicode classifies these as
    ``Cf`` (format), which is neither punctuation nor whitespace, so they
    used to survive every normalization mode and silently prevented a
    reader's cleanly typed quote from ever matching the indexed sentence.
    """

    return bool(ch) and unicodedata.category(ch) == "Cf"


def strip_invisible_format(text: str) -> str:
    return "".join(ch for ch in (text or "") if not is_invisible_format(ch))


def normalize_text(text: str) -> str:
    """Normalize text for direct search while keeping punctuation visible."""

    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = strip_invisible_format(normalized)
    normalized = normalized.translate(QUOTE_TRANSLATION).translate(PUNCT_TRANSLATION)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.lower()


def compact_text(text: str) -> str:
    """Normalize text and remove all whitespace."""

    return re.sub(r"\s+", "", normalize_text(text))


def is_ignored_punctuation(ch: str) -> bool:
    if not ch:
        return False
    if ch.isspace():
        return True
    category = unicodedata.category(ch)
    return category.startswith("P") or category.startswith("S")


def punctuationless_text(text: str) -> str:
    """Normalize text and remove whitespace, symbols, and punctuation."""

    normalized = normalize_text(text)
    return "".join(ch for ch in normalized if not is_ignored_punctuation(ch))


SourceSpan = Tuple[int, int]


def _span_union(spans: Sequence[SourceSpan], fallback: SourceSpan) -> SourceSpan:
    if not spans:
        return fallback
    return min(span[0] for span in spans), max(span[1] for span in spans)


def _reconcile_transformation(
    source_text: str,
    source_spans: Sequence[SourceSpan],
    target_text: str,
    fallback: SourceSpan,
) -> Tuple[List[str], List[SourceSpan]]:
    """Align a Unicode transformation with the source ranges it consumed.

    Most NFKC transformations are one-character substitutions or expansions.
    Cross-character composition (``e`` + COMBINING ACUTE -> ``é`` and Hangul
    Jamo -> a syllable) is the exception.  SequenceMatcher is only used for
    those short normalization segments; replacement output receives the union
    of the source range that produced it.
    """

    if source_text == target_text:
        return list(target_text), list(source_spans)
    matcher = SequenceMatcher(None, source_text, target_text, autojunk=False)
    characters: List[str] = []
    spans: List[SourceSpan] = []
    for tag, source_start, source_end, target_start, target_end in matcher.get_opcodes():
        if tag == "equal":
            characters.extend(target_text[target_start:target_end])
            spans.extend(source_spans[source_start:source_end])
            continue
        if tag == "delete":
            continue
        nearby = list(source_spans[source_start:source_end])
        if not nearby and source_start:
            nearby = [source_spans[source_start - 1]]
        if not nearby and source_end < len(source_spans):
            nearby = [source_spans[source_end]]
        span = _span_union(nearby, fallback)
        replacement = target_text[target_start:target_end]
        characters.extend(replacement)
        spans.extend([span] * len(replacement))
    return characters, spans


def _is_hangul_leading(ch: str) -> bool:
    value = ord(ch)
    return 0x1100 <= value <= 0x115F or 0xA960 <= value <= 0xA97C


def _is_hangul_vowel(ch: str) -> bool:
    value = ord(ch)
    return 0x1160 <= value <= 0x11A7 or 0xD7B0 <= value <= 0xD7C6


def _is_hangul_trailing(ch: str) -> bool:
    value = ord(ch)
    return 0x11A8 <= value <= 0x11FF or 0xD7CB <= value <= 0xD7FB


def _is_hangul_lv_syllable(ch: str) -> bool:
    value = ord(ch)
    return 0xAC00 <= value <= 0xD7A3 and (value - 0xAC00) % 28 == 0


def _can_continue_normalization_segment(segment: str, following: str) -> bool:
    if unicodedata.combining(following):
        return True
    left = unicodedata.normalize("NFKC", segment)
    right = unicodedata.normalize("NFKC", following)
    if not left or not right:
        return False
    return (
        _is_hangul_leading(left[-1]) and _is_hangul_vowel(right[0])
    ) or (
        _is_hangul_lv_syllable(left[-1]) and _is_hangul_trailing(right[0])
    )


def _nfkc_with_spans(
    characters: Sequence[str], source_spans: Sequence[SourceSpan]
) -> Tuple[List[str], List[SourceSpan]]:
    """Apply full-string NFKC without losing source ranges."""

    normalized_characters: List[str] = []
    normalized_spans: List[SourceSpan] = []
    segment_start = 0
    while segment_start < len(characters):
        segment_end = segment_start + 1
        segment_text = characters[segment_start]
        while segment_end < len(characters) and _can_continue_normalization_segment(
            segment_text, characters[segment_end]
        ):
            segment_text += characters[segment_end]
            segment_end += 1

        provisional_characters: List[str] = []
        provisional_spans: List[SourceSpan] = []
        for ch, span in zip(
            characters[segment_start:segment_end],
            source_spans[segment_start:segment_end],
        ):
            chunk = unicodedata.normalize("NFKC", ch)
            provisional_characters.extend(chunk)
            provisional_spans.extend([span] * len(chunk))
        target = unicodedata.normalize("NFKC", segment_text)
        fallback = _span_union(
            source_spans[segment_start:segment_end],
            source_spans[segment_start],
        )
        reconciled_characters, reconciled_spans = _reconcile_transformation(
            "".join(provisional_characters),
            provisional_spans,
            target,
            fallback,
        )
        normalized_characters.extend(reconciled_characters)
        normalized_spans.extend(reconciled_spans)
        segment_start = segment_end

    # This should only be needed for an exotic Unicode normalization boundary.
    # Preserve correctness first, while retaining the precise provisional map
    # for the overwhelmingly common path above.
    target = unicodedata.normalize("NFKC", "".join(characters))
    if "".join(normalized_characters) != target:
        fallback = _span_union(source_spans, (0, 0))
        return _reconcile_transformation(
            "".join(normalized_characters), normalized_spans, target, fallback
        )
    return normalized_characters, normalized_spans


def _collapse_whitespace_with_spans(
    characters: Sequence[str], source_spans: Sequence[SourceSpan]
) -> Tuple[List[str], List[SourceSpan]]:
    """Collapse and trim whitespace while preserving its complete source run."""

    collapsed: List[str] = []
    collapsed_spans: List[SourceSpan] = []
    pending_space_spans: List[SourceSpan] = []
    for ch, source_span in zip(characters, source_spans):
        if ch.isspace():
            if collapsed:
                pending_space_spans.append(source_span)
            continue
        if pending_space_spans:
            collapsed.append(" ")
            collapsed_spans.append(
                _span_union(pending_space_spans, pending_space_spans[0])
            )
            pending_space_spans = []
        collapsed.append(ch)
        collapsed_spans.append(source_span)
    return collapsed, collapsed_spans


def _lower_with_spans(
    characters: Sequence[str], source_spans: Sequence[SourceSpan]
) -> Tuple[List[str], List[SourceSpan]]:
    source_text = "".join(characters)
    target = source_text.lower()
    provisional_characters: List[str] = []
    provisional_spans: List[SourceSpan] = []
    for ch, span in zip(characters, source_spans):
        lowered = ch.lower()
        provisional_characters.extend(lowered)
        provisional_spans.extend([span] * len(lowered))
    if len(provisional_characters) == len(target):
        # Context-sensitive lowercasing (notably Greek final sigma) changes a
        # code point but not which source code point produced that position.
        return list(target), provisional_spans
    return _reconcile_transformation(
        "".join(provisional_characters),
        provisional_spans,
        target,
        _span_union(source_spans, (0, 0)),
    )


def _source_units(text: str, pdf_hyphenation: bool) -> Tuple[List[str], List[SourceSpan]]:
    raw = text or ""
    if not pdf_hyphenation:
        return list(raw), [(index, index + 1) for index in range(len(raw))]

    characters: List[str] = []
    spans: List[SourceSpan] = []
    cursor = 0
    for match in re.finditer(r"([A-Za-z])-\s+([A-Za-z])", raw):
        for index in range(cursor, match.start()):
            characters.append(raw[index])
            spans.append((index, index + 1))
        for group in (1, 2):
            index = match.start(group)
            characters.append(raw[index])
            spans.append((index, index + 1))
        cursor = match.end()
    for index in range(cursor, len(raw)):
        characters.append(raw[index])
        spans.append((index, index + 1))
    return characters, spans


def normalize_with_spans(
    text: str, mode: str, *, pdf_hyphenation: bool = False
) -> Tuple[str, List[SourceSpan]]:
    """Return normalized text and half-open source spans for every output char.

    ``mode`` can be ``normalized``, ``compact``, or ``plain``.  PDF mode also
    mirrors the indexer's dehyphenation of line-broken Latin words.  Half-open
    spans are necessary because Unicode composition can consume multiple source
    code points and compatibility normalization can expand one source code point
    into several output characters.
    """

    if mode not in {"normalized", "compact", "plain"}:
        raise ValueError(f"Unsupported normalization mode: {mode}")
    characters, spans = _source_units(text or "", pdf_hyphenation)
    characters, spans = _nfkc_with_spans(characters, spans)

    translated_characters: List[str] = []
    translated_spans: List[SourceSpan] = []
    for ch, span in zip(characters, spans):
        if is_invisible_format(ch):
            continue
        translated = ch.translate(QUOTE_TRANSLATION).translate(PUNCT_TRANSLATION)
        translated_characters.extend(translated)
        translated_spans.extend([span] * len(translated))

    characters, spans = _collapse_whitespace_with_spans(
        translated_characters, translated_spans
    )
    characters, spans = _lower_with_spans(characters, spans)
    if mode in {"compact", "plain"}:
        filtered = [
            (ch, span)
            for ch, span in zip(characters, spans)
            if not ch.isspace()
            and not (mode == "plain" and is_ignored_punctuation(ch))
        ]
        characters = [item[0] for item in filtered]
        spans = [item[1] for item in filtered]
    return "".join(characters), spans


def normalize_with_map(text: str, mode: str) -> Tuple[str, List[int]]:
    """Return normalized text and a map from normalized chars to source indices.

    ``mode`` can be ``normalized``, ``compact``, or ``plain``. The plain mode
    drops punctuation as well as whitespace and is used for punctuation-tolerant
    highlighting.
    """

    normalized, spans = normalize_with_spans(text, mode)
    return normalized, [span[0] for span in spans]


def normalize_pdf_text(text: str) -> str:
    normalized, _ = normalize_with_spans(
        text, "normalized", pdf_hyphenation=True
    )
    return normalized


def char_ngrams(text: str, n: int = 2) -> List[str]:
    text = punctuationless_text(text)
    if len(text) <= n:
        return [text] if text else []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def split_sentences(text: str) -> List[str]:
    """Split Chinese prose into sentences without changing the original text."""

    text = (text or "").strip()
    if not text:
        return []
    sentences: List[str] = []
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in SENTENCE_ENDINGS:
            end = i + 1
            while end < len(text) and text[end] in "\"'”’」』）)]":
                end += 1
            piece = text[start:end].strip()
            if piece:
                sentences.append(piece)
            start = end
            i = end
            continue
        i += 1
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def trim_for_display(text: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def cn_volume_number(number: int) -> str:
    digits = {
        1: "一",
        2: "二",
        3: "三",
        4: "四",
        5: "五",
        6: "六",
        7: "七",
        8: "八",
        9: "九",
        10: "十",
    }
    return digits.get(number, str(number))


def parse_int_label(value: str | None) -> int | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", str(value))
    match = re.search(r"\d+", value)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None
