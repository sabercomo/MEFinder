"""Text normalization helpers for reproducible local search."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Sequence, Tuple


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


def normalize_with_map(text: str, mode: str) -> Tuple[str, List[int]]:
    """Return normalized text and a map from normalized chars to source indices.

    ``mode`` can be ``normalized``, ``compact``, or ``plain``. The plain mode
    drops punctuation as well as whitespace and is used for punctuation-tolerant
    highlighting.
    """

    out: List[str] = []
    mapping: List[int] = []
    for source_index, original in enumerate(text or ""):
        chunk = unicodedata.normalize("NFKC", original)
        chunk = strip_invisible_format(chunk)
        chunk = chunk.translate(QUOTE_TRANSLATION).translate(PUNCT_TRANSLATION)
        for ch in chunk.lower():
            if mode in {"compact", "plain"} and ch.isspace():
                continue
            if mode == "plain" and is_ignored_punctuation(ch):
                continue
            out.append(ch)
            mapping.append(source_index)
    if mode == "normalized":
        normalized = re.sub(r"\s+", " ", "".join(out)).strip()
        return normalized, mapping
    return "".join(out), mapping


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
