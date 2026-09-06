"""Cross-language embeddings, heading anchors, and monotonic semantic DP."""

from __future__ import annotations

import math
import os
import re
import hashlib
import json
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Protocol, Sequence, Tuple

import numpy as np

from .embedding_models import (
    AlignmentThresholds,
    DEFAULT_EMBEDDING_MODEL_ID,
    EmbeddingModelConfig,
    embedding_model_config,
)

EMBEDDING_RUNTIME_VERSION = "fastembed-0.8.0-mean-pooling"
_DEFAULT_THRESHOLDS = embedding_model_config(DEFAULT_EMBEDDING_MODEL_ID).thresholds
LOW_CONFIDENCE_THRESHOLD = _DEFAULT_THRESHOLDS.low
NOTE_BLOCK_CONFIDENCE_THRESHOLD = _DEFAULT_THRESHOLDS.note_block
NOTE_CANDIDATE_MARGIN = _DEFAULT_THRESHOLDS.margin
SEMANTIC_ALIGNMENT_VERSION = "17"
ALIGNMENT_REGION_VERSION = "1"
_MAX_HEADING_NUMBER = 30
_MAX_PARAGRAPH_NUMBER = 9999
_MIN_COLLECTED_NOTE_RUN = 5
_MIN_STRUCTURAL_NOTE_CHAIN = 3
_MAX_INLINE_NOTE_WINDOW = 24
_SEARCH_BAND = 96
_TRANSITIONS: Tuple[Tuple[int, int, float], ...] = (
    (1, 1, 0.0),
    (1, 2, 0.15),
    (2, 1, 0.15),
    (2, 2, 0.25),
    (1, 3, 0.35),
    (3, 1, 0.35),
    (2, 3, 0.45),
    (3, 2, 0.45),
    (3, 3, 0.55),
    (1, 0, 2.2),
    (0, 1, 2.2),
)
_CHINESE_NUMBER_VALUES = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
_CHINESE_HEADING = re.compile(
    r"^第([零〇一二两三四五六七八九十百\d]{1,5})(章|节|篇|部)\s*(.*)$"
)
_LATIN_CHAPTER = re.compile(
    r"^(?:chapter\s+)?(\d{1,2})\s+(.{3,})$", re.IGNORECASE
)
_PADDED_CHAPTER = re.compile(r"^(0[1-9])\s*(\D.{2,})$")
_DECIMAL_SECTION = re.compile(r"^(\d{1,2})\.\s*(\d{1,2})\.?\s+(\D.*)$")
_LATIN_SECTION = re.compile(r"^([ivxlc]{1,5})[.)]\s+(.{3,})$", re.IGNORECASE)
_LATIN_TOC_SECTION = re.compile(r"^([ivxlc]{1,5})[.)]?\s+(.{3,})$", re.IGNORECASE)
_FRENCH_PART = re.compile(
    r"^(première|deuxième|troisième)\s+partie$", re.IGNORECASE
)
_FRENCH_PART_NUMBERS = {"première": 1, "deuxième": 2, "troisième": 3}
_ENGLISH_PART = re.compile(
    r"^part\s+(one|two|three|four|[ivx]{1,4}|\d{1,2})$", re.IGNORECASE
)
_ENGLISH_PART_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4}
_OCR_DIGIT_CHARACTERS = "0-9IlOoSs"
_OCR_NUMBER_TOKEN = (
    rf"[{_OCR_DIGIT_CHARACTERS}](?:\s*[{_OCR_DIGIT_CHARACTERS}]){{0,4}}"
)
_CHINESE_PARAGRAPH_TOKEN = (
    rf"[零〇一二两三四五六七八九十百{_OCR_DIGIT_CHARACTERS}]"
    rf"(?:\s*[零〇一二两三四五六七八九十百{_OCR_DIGIT_CHARACTERS}]){{0,4}}"
)
_SECTION_SIGN_HEADING = re.compile(
    rf"^\s*§+\s*({_OCR_NUMBER_TOKEN})(?=$|[\s.:：。—–-])",
)
_CHINESE_PARAGRAPH_HEADING = re.compile(
    rf"^\s*第\s*({_CHINESE_PARAGRAPH_TOKEN})\s*节(?=$|[\s.:：。—–-])",
)
_TOC_LEADER = re.compile(
    r"(?:\.{3,}|…{2,}|·{3,})\s*[\[(（]?\d*[\])）]?\s*$"
)
_CONTENTS_HEADING = re.compile(r"^(?:contents|table of contents|目\s*[录錄次])$", re.IGNORECASE)
_TOC_SLASH_ENTRY = re.compile(r"^\s*\d{1,3}\s*/\s*(.+?)\s*$")
_TOC_ENGLISH_PART = re.compile(
    r"^part\s+(one|two|three|four|[ivx]{1,4}|\d{1,2})\s+(.+)$",
    re.IGNORECASE,
)
_AUTHOR_PREFACE_HEADING = re.compile(
    r"^(?:author(?:'s|’s)?\s+preface|preface|préface|avant-propos|vorwort"
    r"|作者序|前言|序言)$",
    re.IGNORECASE,
)
_ENDNOTES_HEADING = re.compile(
    r"^(?:(?:editorial|translator(?:'s|’s)?)\s+notes?|endnotes?|notes?"
    r"|anmerkungen|尾注|尾註|注释|註釋|参考文献|參考文獻|bibliography|index|索引)$",
    re.IGNORECASE,
)
_NOTE_STREAM_TERMINATOR = re.compile(
    r"^(?:bibliography|index|参考文献|參考文獻|索引)$", re.IGNORECASE
)
_LATIN_NOTE_MARKER = re.compile(r"(\d{1,3})\.")
_INLINE_NOTE_MARKER = re.compile(r"(?m)^\s*\[(\d{1,3})\][ \t]+\S")
_EXPLICIT_TRANSLATOR_NOTE = re.compile(
    r"^\s*(?:\[\d{1,3}\]|\d{1,3}\.)?\s*"
    r"(?:译者(?:注|按)|譯者(?:註|按)|译注(?=[:：\s])|譯註(?=[:：\s])"
    r"|translator(?:'s|’s)?\s+note|trans\.\s*note)",
    re.IGNORECASE,
)

LOGGER = logging.getLogger(__name__)


class SemanticAlignmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class SemanticLink:
    source_start: int
    source_end: int
    target_start: int
    target_end: int
    cost: float
    confidence: float
    review_status: str
    anchor_key: str = ""


@dataclass(frozen=True)
class HeadingAnchor:
    source_index: int
    target_index: int
    key: str


@dataclass(frozen=True)
class _NumberedHeading:
    index: int
    number: int
    body_weight: int


@dataclass(frozen=True)
class _NumberedNoteBlock:
    chapter_number: int
    note_number: int
    start: int
    content_start: int
    end: int


@dataclass(frozen=True)
class _InlineNoteCandidate:
    chapter_number: int
    note_number: int
    start: int


class EmbeddingProvider(Protocol):
    def __call__(self, texts: Sequence[str], cache_dir: Path) -> np.ndarray: ...


@dataclass(frozen=True)
class FastEmbedEmbeddingProvider:
    model: EmbeddingModelConfig

    def __call__(self, texts: Sequence[str], cache_dir: Path) -> np.ndarray:
        from fastembed import TextEmbedding

        embedding = TextEmbedding(
            model_name=self.model.hf_name,
            cache_dir=str(cache_dir),
            threads=max(1, min(8, os.cpu_count() or 1)),
        )
        method = (
            embedding.query_embed
            if self.model.prefix_mode == "query"
            else embedding.embed
        )
        prepared_texts = (
            [f"query: {text}" for text in texts]
            if self.model.prefix_mode == "query"
            else list(texts)
        )
        vectors = np.asarray(
            # E5's large ONNX activations at batch 64 can exhaust desktop RAM.
            list(method(prepared_texts, batch_size=4 if self.model.prefix_mode == "query" else 64)), dtype=np.float32
        )
        _write_model_receipt(cache_dir, self.model)
        return vectors


def _write_model_receipt(cache_dir: Path, model: EmbeddingModelConfig) -> None:
    receipt = cache_dir / "installed" / f"{model.id}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"id": model.id, "hf_name": model.hf_name},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(receipt)


def embed_texts(
    texts: Sequence[str],
    cache_dir: Path,
    *,
    model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
) -> np.ndarray:
    """Embed text with FastEmbed's CPU ONNX multilingual model."""

    try:
        return FastEmbedEmbeddingProvider(embedding_model_config(model_id))(
            texts, cache_dir
        )
    except Exception as exc:
        raise SemanticAlignmentError(
            "跨语言语义模型加载失败；请检查网络后重试生成对齐。"
        ) from exc


def _sequence_cache_path(
    texts: Sequence[str],
    cache_dir: Path,
    *,
    model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
) -> Path:
    digest = hashlib.sha256()
    digest.update(model_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(EMBEDDING_RUNTIME_VERSION.encode("ascii"))
    for text in texts:
        encoded = text.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return cache_dir / "document-vectors" / f"{digest.hexdigest()}.npy"


def embed_text_sequences(
    sequences: Sequence[Sequence[str]],
    cache_dir: Path,
    *,
    reusable_sequences: Sequence[Sequence[str]] = (),
    model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
) -> List[np.ndarray]:
    """Cache document vectors and reuse unchanged segments after re-segmentation."""

    paths = [
        _sequence_cache_path(texts, cache_dir, model_id=model_id)
        for texts in sequences
    ]
    results: List[np.ndarray | None] = [None] * len(sequences)
    missing: List[int] = []
    for index, path in enumerate(paths):
        if path.is_file():
            results[index] = np.load(path, allow_pickle=False)
        else:
            missing.append(index)
    if missing:
        vectors_by_text: Dict[str, np.ndarray] = {}
        for reusable_texts in reusable_sequences:
            reusable_path = _sequence_cache_path(
                reusable_texts, cache_dir, model_id=model_id
            )
            if not reusable_path.is_file():
                continue
            reusable_vectors = np.load(reusable_path, allow_pickle=False)
            if len(reusable_vectors) != len(reusable_texts):
                raise SemanticAlignmentError("已有语义向量缓存与 Segment 数量不一致。")
            for text, vector in zip(reusable_texts, reusable_vectors):
                vectors_by_text.setdefault(text, vector)
        uncached_texts = list(
            dict.fromkeys(
                text
                for index in missing
                for text in sequences[index]
                if text not in vectors_by_text
            )
        )
        if uncached_texts:
            uncached_vectors = embed_texts(
                uncached_texts, cache_dir, model_id=model_id
            )
            vectors_by_text.update(zip(uncached_texts, uncached_vectors))
        for index in missing:
            vectors = np.stack(
                [vectors_by_text[text] for text in sequences[index]], axis=0
            ).astype(np.float32, copy=False)
            path = paths[index]
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp.npy")
            np.save(temporary, vectors, allow_pickle=False)
            os.replace(temporary, path)
            results[index] = vectors
    return [vectors for vectors in results if vectors is not None]


def cached_text_sequence_vectors(
    texts: Sequence[str],
    cache_dir: Path,
    *,
    model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
) -> np.ndarray | None:
    """Load vectors already produced for one complete segment sequence."""

    path = _sequence_cache_path(texts, cache_dir, model_id=model_id)
    if not path.is_file():
        return None
    vectors = np.load(path, allow_pickle=False, mmap_mode="r")
    if len(vectors) != len(texts):
        raise SemanticAlignmentError("已有语义向量缓存与 Segment 数量不一致。")
    return _normalized_rows(np.asarray(vectors, dtype=np.float32))


def mutual_nearest_target_index(
    source_vectors: np.ndarray,
    target_vectors: np.ndarray,
    selected_source_indices: Sequence[int],
    *,
    low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
) -> int | None:
    """Return the strongest target that also chooses the selected source."""

    if not len(source_vectors) or not len(target_vectors):
        return None
    selected = np.asarray(sorted(set(selected_source_indices)), dtype=np.int64)
    similarities = source_vectors @ target_vectors.T
    reverse_owners = np.argmax(similarities, axis=0)
    candidates = np.flatnonzero(np.isin(reverse_owners, selected))
    if not len(candidates):
        return None
    selected_vector = source_vectors[selected].sum(axis=0)
    selected_vector /= max(float(np.linalg.norm(selected_vector)), 1e-12)
    candidate_scores = target_vectors[candidates] @ selected_vector
    best_offset = int(np.argmax(candidate_scores))
    if float(candidate_scores[best_offset]) < low_confidence_threshold:
        return None
    return int(candidates[best_offset])


def _chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    total = 0
    current = 0
    for character in value:
        if character in _CHINESE_NUMBER_VALUES:
            current = _CHINESE_NUMBER_VALUES[character]
        elif character == "十":
            total += (current or 1) * 10
            current = 0
        elif character == "百":
            total += (current or 1) * 100
            current = 0
    return total + current


def _roman_number(value: str) -> int:
    total = 0
    previous = 0
    for character in reversed(value.casefold()):
        current = _ROMAN_VALUES[character]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total


def _number_token(value: str) -> int:
    """Read OCR-confused digits without changing the surrounding heading text."""

    normalized = re.sub(r"\s+", "", value).translate(
        str.maketrans({"I": "1", "l": "1", "O": "0", "o": "0", "S": "5", "s": "5"})
    )
    if normalized.isdigit():
        return int(normalized)
    if all(character in "零〇一二两三四五六七八九十百" for character in normalized):
        return _chinese_number(normalized)
    return 0


def _paragraph_number(line: str) -> int:
    section_sign = _SECTION_SIGN_HEADING.match(line)
    if section_sign is not None:
        match = section_sign
    else:
        chinese = _CHINESE_PARAGRAPH_HEADING.match(line)
        if chinese is None:
            return 0
        match = chinese
    suffix = line[match.end() :].strip()
    suffix = suffix.lstrip(".:：。—–-").lstrip()
    if suffix and (suffix[0].isdigit() or suffix[0].islower()):
        return 0
    return _number_token(match.group(1))


def _paragraph_heading_positions(texts: Sequence[str]) -> Dict[str, int]:
    """Choose the body sequence of numbered paragraphs, not its TOC copy."""

    candidates: List[_NumberedHeading] = []
    have_numbered_body = False
    for index, text in enumerate(texts):
        lines = [
            re.sub(r"\s+", " ", raw_line).strip(" \t/｜|")
            for raw_line in text.splitlines()
        ]
        lines = [line for line in lines if line]
        if have_numbered_body and any(_ENDNOTES_HEADING.fullmatch(line) for line in lines):
            break
        numbered = [
            (line, _paragraph_number(line))
            for line in lines
            if not _TOC_LEADER.search(line)
        ]
        numbered.extend(
            (f"第{lines[line_index + 1]}", _paragraph_number(f"第{lines[line_index + 1]}"))
            for line_index, line in enumerate(lines[:-1])
            if line == "第"
        )
        numbered = [
            (line, number)
            for line, number in numbered
            if 0 < number <= _MAX_PARAGRAPH_NUMBER
        ]
        if not numbered or len(numbered) > 4:
            continue
        have_numbered_body = True
        body_weight = min(500, sum(not character.isspace() for character in text))
        for _line, number in numbered:
            candidates.append(_NumberedHeading(index, number, body_weight))

    if not candidates:
        return {}

    # A TOC and the body commonly contain the same complete number range.  A
    # longest increasing sequence keeps only one occurrence of each number;
    # body text length and then later position resolve equal-length copies in
    # favour of the body instead of the first setdefault occurrence.
    best_paths: List[Tuple[int, ...]] = []
    best_scores: List[Tuple[int, int, int]] = []
    for candidate_index, candidate in enumerate(candidates):
        path = (candidate_index,)
        score = (1, candidate.body_weight, candidate.index)
        for previous_index, previous in enumerate(candidates[:candidate_index]):
            if previous.number >= candidate.number:
                continue
            previous_path = best_paths[previous_index]
            previous_score = best_scores[previous_index]
            proposed_path = previous_path + (candidate_index,)
            proposed_score = (
                previous_score[0] + 1,
                previous_score[1] + candidate.body_weight,
                previous_score[2] + candidate.index,
            )
            if proposed_score > score:
                path = proposed_path
                score = proposed_score
        best_paths.append(path)
        best_scores.append(score)

    selected_path = best_paths[max(range(len(candidates)), key=best_scores.__getitem__)]
    return {
        f"paragraph:{candidates[candidate_index].number}": candidates[candidate_index].index
        for candidate_index in selected_path
    }


def _heading_lines(text: str, *, numbered_sections: bool = False) -> List[Tuple[str, int]]:
    matches: List[Tuple[str, int]] = []
    for line_index, raw_line in enumerate(text.splitlines()):
        line = re.sub(r"\s+", " ", raw_line).strip(" \t/｜|")
        if not line or len(line) > 100:
            continue
        if _TOC_LEADER.search(line):
            continue
        chinese = _CHINESE_HEADING.fullmatch(line)
        if chinese is not None:
            if chinese.group(3).rstrip().endswith("。"):
                continue
            number = _chinese_number(chinese.group(1))
            kind = "chapter" if chinese.group(2) in {"章", "篇", "部"} else "section"
            if 0 < number <= _MAX_HEADING_NUMBER:
                matches.append((kind, number))
            continue
        french_part = _FRENCH_PART.fullmatch(line)
        if french_part is not None:
            matches.append(
                ("chapter", _FRENCH_PART_NUMBERS[french_part.group(1).casefold()])
            )
            continue
        english_part = _ENGLISH_PART.fullmatch(line)
        if english_part is not None:
            token = english_part.group(1).casefold()
            if token.isdigit():
                number = int(token)
            elif token in _ENGLISH_PART_NUMBERS:
                number = _ENGLISH_PART_NUMBERS[token]
            else:
                number = _roman_number(token)
            matches.append(("chapter", number))
            continue
        chapter = _LATIN_CHAPTER.fullmatch(line) or _PADDED_CHAPTER.fullmatch(line)
        if (
            chapter is not None
            and line_index == 0
            and not line.endswith((".", ";", ":", "。", "；", "："))
            and not any(ord(character) < 32 and not character.isspace() for character in raw_line)
        ):
            number = int(chapter.group(1))
            if 0 < number <= _MAX_HEADING_NUMBER:
                # A bare number below explicit CJK chapters is a subsection,
                # not a new chapter (including Japanese full-width digits).
                kind = (
                    "section"
                    if numbered_sections and not line.casefold().startswith("chapter ")
                    else "chapter"
                )
                matches.append((kind, number))
            continue
        section = _LATIN_SECTION.fullmatch(line)
        if section is not None and re.search(r"[A-Za-z]{3}", section.group(2)):
            number = _roman_number(section.group(1))
            if 0 < number <= _MAX_HEADING_NUMBER:
                matches.append(("section", number))
    return matches


def _normalized_heading_title(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)


def _repeated_chapter_toc_indices(
    texts: Sequence[str], *, numbered_sections: bool
) -> set[int]:
    """Confirm a split TOC by the body repeating its first full chapter heading."""

    indices: set[int] = set()
    contents_start: int | None = None
    first_chapter: Tuple[str, int] | None = None
    for index, text in enumerate(texts):
        if any(_CONTENTS_HEADING.fullmatch(line.strip()) for line in text.splitlines()):
            contents_start = index
            first_chapter = None
        if contents_start is None:
            continue
        chapters = [
            _normalized_heading_title(line)
            for line in text.splitlines()
            if any(
                kind == "chapter"
                for kind, _number in _heading_lines(
                    line, numbered_sections=numbered_sections
                )
            )
        ]
        if not chapters:
            continue
        if first_chapter is None:
            first_chapter = (chapters[0], index)
        elif index > first_chapter[1] and first_chapter[0] in chapters:
            indices.update(range(contents_start, index))
            contents_start = None
            first_chapter = None
    return indices


def _compact_split_toc_indices(
    texts: Sequence[str], *, numbered_sections: bool
) -> set[int]:
    """Recognize a multi-segment TOC even when OCR drops leaders and repeats."""

    indices: set[int] = set()
    for start, text in enumerate(texts):
        if not any(
            _CONTENTS_HEADING.fullmatch(line.strip())
            for line in text.splitlines()
        ):
            continue
        chapter_numbers: List[int] = []
        end = min(len(texts), start + 128)
        for index in range(start + 1, end):
            probe = texts[index]
            chapters = [
                number
                for kind, number in _heading_lines(
                    probe, numbered_sections=numbered_sections
                )
                if kind == "chapter"
            ]
            if (
                chapter_numbers
                and max(chapter_numbers) >= 2
                and 1 in chapters
            ):
                end = index
                break
            compact_length = sum(not character.isspace() for character in probe)
            prose_starts = (
                len(chapter_numbers) >= 3
                and not chapters
                and (
                    compact_length > 120
                    or (
                        compact_length >= 36
                        and re.search(r"[。！？!?；;]", probe) is not None
                    )
                )
            )
            if prose_starts:
                end = index
                break
            chapter_numbers.extend(chapters)
        if len(chapter_numbers) >= 3:
            indices.update(range(start, end))
    return indices


def _latin_toc_titles(
    texts: Sequence[str], *, numbered_sections: bool = False
) -> Tuple[Dict[str, str], set[int]]:
    titles: Dict[str, str] = {}
    toc_indices: set[int] = set()
    in_toc = False
    current_chapter = 0
    pending_chapter_title = False
    split_toc_indices = _repeated_chapter_toc_indices(
        texts, numbered_sections=numbered_sections
    )
    split_toc_indices.update(
        _compact_split_toc_indices(
            texts, numbered_sections=numbered_sections
        )
    )
    for index, text in enumerate(texts):
        lines = [
            re.sub(r"\s+", " ", raw_line).strip(" \t/｜|")
            for raw_line in text.splitlines()
        ]
        has_contents_heading = any(
            _CONTENTS_HEADING.fullmatch(line) for line in lines
        )
        if has_contents_heading or index in split_toc_indices:
            in_toc = True
        elif in_toc and not any(
            _TOC_SLASH_ENTRY.fullmatch(line) or _TOC_LEADER.search(line)
            for line in lines
        ):
            in_toc = False
        if not in_toc:
            continue
        toc_indices.add(index)
        for line in lines:
            if _CONTENTS_HEADING.fullmatch(line):
                continue
            slash_entry = _TOC_SLASH_ENTRY.fullmatch(line)
            entry = slash_entry.group(1).strip() if slash_entry is not None else line
            entry = _TOC_LEADER.sub("", entry).strip()
            chinese = _CHINESE_HEADING.fullmatch(entry)
            if chinese is not None:
                number = _chinese_number(chinese.group(1))
                kind = chinese.group(2)
                if kind in {"章", "篇", "部"}:
                    current_chapter = number
                    pending_chapter_title = True
                    titles[_normalized_heading_title(entry)] = (
                        f"chapter:{current_chapter}"
                    )
                elif current_chapter:
                    section_number = number
                    titles[_normalized_heading_title(entry)] = (
                        f"chapter:{current_chapter}:section:{section_number}"
                    )
                continue
            english_part = _TOC_ENGLISH_PART.fullmatch(entry)
            if english_part is not None:
                token = english_part.group(1).casefold()
                if token.isdigit():
                    current_chapter = int(token)
                elif token in _ENGLISH_PART_NUMBERS:
                    current_chapter = _ENGLISH_PART_NUMBERS[token]
                else:
                    current_chapter = _roman_number(token)
                titles[_normalized_heading_title(english_part.group(2))] = (
                    f"chapter:{current_chapter}"
                )
                pending_chapter_title = False
                continue
            if pending_chapter_title and slash_entry is not None:
                titles[_normalized_heading_title(entry)] = (
                    f"chapter:{current_chapter}"
                )
                pending_chapter_title = False
                continue
            if slash_entry is not None:
                continue
            chapter = _LATIN_CHAPTER.fullmatch(line) or _PADDED_CHAPTER.fullmatch(line)
            if chapter is not None:
                if numbered_sections and not line.casefold().startswith("chapter "):
                    if current_chapter:
                        titles[_normalized_heading_title(line)] = (
                            f"chapter:{current_chapter}:section:{int(chapter.group(1))}"
                        )
                    continue
                current_chapter = int(chapter.group(1))
                titles[_normalized_heading_title(chapter.group(2))] = (
                    f"chapter:{current_chapter}"
                )
                continue
            section = _LATIN_TOC_SECTION.fullmatch(line)
            if section is None or not current_chapter:
                continue
            section_number = _roman_number(section.group(1))
            titles[_normalized_heading_title(section.group(2))] = (
                f"chapter:{current_chapter}:section:{section_number}"
            )
    return titles, toc_indices


def _document_heading_positions(texts: Sequence[str]) -> Dict[str, int]:
    positions: Dict[str, int] = {}
    decimal_positions: Dict[str, int] = {}
    numbered_sections = any(
        match is not None and match.group(2) in {"章", "篇", "部"}
        for text in texts
        for line in text.splitlines()
        for match in [_CHINESE_HEADING.fullmatch(line.strip())]
    )
    toc_titles, toc_indices = _latin_toc_titles(
        texts, numbered_sections=numbered_sections
    )
    current_chapter = 0
    last_section = 0
    for index, text in enumerate(texts):
        normalized_lines = [
            re.sub(r"\s+", " ", raw_line).strip(" \t/｜|")
            for raw_line in text.splitlines()
        ]
        if index in toc_indices:
            continue
        if (positions or decimal_positions) and any(
            _ENDNOTES_HEADING.fullmatch(line) for line in normalized_lines
        ):
            break
        # Decimal chapter.section ordinals survive even when the parser omits
        # chapter titles. Only use a title line, never a TOC page number or prose.
        first_line = normalized_lines[0] if normalized_lines else ""
        # A trailing numeric footnote marker may share the segment with the
        # next heading; it does not change that heading's structural ordinal.
        if re.fullmatch(r"\$\^\{\d+\}\$", first_line) and len(normalized_lines) > 1:
            first_line = normalized_lines[1]
        decimal = _DECIMAL_SECTION.fullmatch(first_line)
        if (
            decimal is not None and len(first_line) <= 100
            and not first_line.endswith((".", ";", ":", "。", "；", "："))
            and not re.search(r"\d$", first_line)
            and not _TOC_LEADER.search(first_line)
        ):
            chapter_number, section_number = int(decimal[1]), int(decimal[2])
            if 0 < chapter_number <= _MAX_HEADING_NUMBER and 0 < section_number <= _MAX_HEADING_NUMBER:
                decimal_positions.setdefault(f"chapter:{chapter_number}:section:{section_number}", index)
        if not decimal_positions and any(
            _AUTHOR_PREFACE_HEADING.fullmatch(line) for line in normalized_lines
        ):
            positions.setdefault("preface:author", index)
        matches = _heading_lines(text, numbered_sections=numbered_sections)
        for line in normalized_lines:
            toc_key = toc_titles.get(_normalized_heading_title(line))
            if toc_key is not None:
                positions.setdefault(toc_key, index)
                parts = toc_key.split(":")
                if not matches or (
                    len(parts) == 2 and not any(kind == "chapter" for kind, _ in matches)
                ):
                    current_chapter = int(parts[1])
                    last_section = int(parts[3]) if len(parts) == 4 else 0
        if len(matches) > 4:
            continue
        for kind, number in matches:
            if kind == "chapter":
                if number == current_chapter:
                    positions.setdefault(f"chapter:{number}", index)
                    continue
                if number != current_chapter + 1:
                    continue
                current_chapter = number
                last_section = 0
                key = f"chapter:{number}"
            else:
                if number == 1 and (current_chapter == 0 or last_section > 1):
                    current_chapter += 1
                    last_section = 0
                if not current_chapter:
                    continue
                key = f"chapter:{current_chapter}:section:{number}"
                last_section = number
            positions.setdefault(key, index)
    positions.update(decimal_positions)
    positions.update(_paragraph_heading_positions(texts))
    return positions


def alignment_body_bounds(texts: Sequence[str]) -> Tuple[int, int]:
    """Bound the main text using existing TOC-aware chapter detection.

    A missing body heading is not evidence that the whole document is frontmatter.
    Only leading title lines open backmatter; a prose mention never does.
    Inline footnotes inside the body are left to the existing note workflow.
    """
    positions = _document_heading_positions(texts)
    body_positions = [
        index for key, index in positions.items()
        if key.startswith("chapter:")
        # Roman-numbered prose and CIP entries are not body boundaries.
        and (key.count(":") == 1 or _DECIMAL_SECTION.fullmatch(texts[index].splitlines()[0].strip()))
        and not re.match(r"^\d+\s+[a-z]", texts[index].strip())
    ]
    if "paragraph:1" in positions and re.match(
        r"^(?:§|第\s*1\s*节)", texts[positions["paragraph:1"]].strip()
    ):
        body_positions = [positions["paragraph:1"]]
    start = min(body_positions, default=0)
    end = len(texts)
    backmatter_titles = {
        "译后记", "譯後記", "译者后记", "譯者後記", "后记", "後記",
        "致谢", "致謝", "鸣谢", "鳴謝", "索引", "尾注", "尾註",
        "注释", "註釋", "参考文献", "參考文獻",
        "afterword", "translator'safterword", "translator’safterword",
        "acknowledgments", "acknowledgements", "index", "notes", "endnotes",
        "bibliography", "references", "anmerkungen", "nachwort", "register",
        "bibliographie", "remerciements",
    }
    note_titles = {"注释", "註釋", "notes", "anmerkungen"}
    note_positions = [
        index for index in range(start + 1, len(texts))
        if texts[index].strip().splitlines()
        and re.sub(r"\s+", "", texts[index].strip().splitlines()[0]).casefold() in note_titles
    ]
    for index in range(start + 1, len(texts)):
        lines = texts[index].strip().splitlines()
        title = re.sub(r"\s+", "", lines[0]).casefold() if lines else ""
        if title in backmatter_titles:
            # Repeated chapter-note blocks are not a single end-of-book region.
            # The existing anchor reader stops at Notes, so inspect later
            # explicit chapter headings here without changing anchor/DP logic.
            if title in note_titles and (
                len(note_positions) > 1
                or any(
                    any(
                        (match := _CHINESE_HEADING.fullmatch(line.strip())) is not None
                        and match.group(2) in {"章", "篇", "部"}
                        for line in text.splitlines()
                    )
                    for text in texts[index + 1:]
                )
            ):
                continue
            end = index
            break
    return start, end


def find_heading_anchors(
    source_texts: Sequence[str], target_texts: Sequence[str]
) -> List[HeadingAnchor]:
    """Pair chapter/section headings by structural ordinal, excluding TOC blocks."""

    source = _document_heading_positions(source_texts)
    target = _document_heading_positions(target_texts)
    candidates = sorted(
        (
            HeadingAnchor(source[key], target[key], key)
            for key in source.keys() & target.keys()
        ),
        key=lambda item: (
            item.source_index,
            item.target_index,
            not item.key.startswith("paragraph:"),
            item.key,
        ),
    )
    unique_candidates: List[HeadingAnchor] = []
    seen_coordinates: set[Tuple[int, int]] = set()
    for candidate in candidates:
        coordinates = (candidate.source_index, candidate.target_index)
        if coordinates in seen_coordinates:
            continue
        seen_coordinates.add(coordinates)
        unique_candidates.append(candidate)
    if not unique_candidates:
        return []

    # Paragraph ordinals are edition-independent hard anchors.  Both documents'
    # paragraph detectors already return monotonic number sequences, so their
    # shared subset must be frozen before weaker chapter/preface candidates are
    # considered.
    paragraph_anchors = [
        candidate
        for candidate in unique_candidates
        if candidate.key.startswith("paragraph:")
    ]
    secondary_candidates = [
        candidate
        for candidate in unique_candidates
        if not candidate.key.startswith("paragraph:")
        and all(
            candidate.source_index != paragraph.source_index
            and candidate.target_index != paragraph.target_index
            and (candidate.source_index < paragraph.source_index)
            == (candidate.target_index < paragraph.target_index)
            for paragraph in paragraph_anchors
        )
    ]
    if not secondary_candidates:
        return paragraph_anchors

    # Add the longest monotonic chain of weaker headings that is compatible
    # with every frozen paragraph anchor.
    paths: List[Tuple[int, ...]] = []
    scores: List[Tuple[int, int]] = []
    for candidate_index, candidate in enumerate(secondary_candidates):
        path = (candidate_index,)
        score = (1, candidate.source_index + candidate.target_index)
        for previous_index, previous in enumerate(
            secondary_candidates[:candidate_index]
        ):
            if (
                previous.source_index >= candidate.source_index
                or previous.target_index >= candidate.target_index
            ):
                continue
            previous_path = paths[previous_index]
            previous_score = scores[previous_index]
            proposed_path = previous_path + (candidate_index,)
            proposed_score = (
                previous_score[0] + 1,
                previous_score[1] + candidate.source_index + candidate.target_index,
            )
            if proposed_score > score:
                path = proposed_path
                score = proposed_score
        paths.append(path)
        scores.append(score)
    selected = paths[max(range(len(secondary_candidates)), key=scores.__getitem__)]
    return sorted(
        [
            *paragraph_anchors,
            *(secondary_candidates[index] for index in selected),
        ],
        key=lambda item: (item.source_index, item.target_index, item.key),
    )


_ANCHOR_PRIORITY = {"term": 3, "number": 2, "name": 1}
_LATIN_ANCHOR_LANGUAGES = frozenset({"de", "en", "fr"})
_LATIN_WORD = re.compile(r"[^\W\d_]+(?:[’'-][^\W\d_]+)*", re.UNICODE)
_HAN_RUN = re.compile(r"[\u3400-\u9fff]+")
_PARENTHETICAL_ORIGINAL = re.compile(r"[（(]([^（）()]{2,120})[）)]")
_PAGE_NUMBER_BLOCK = re.compile(
    r"^\s*(?:(?:p(?:age)?|s(?:eite)?|页|頁)\.?\s*)?"
    r"(?:[-—–]\s*)?(?:\d{1,4}|[ivxlcdm]{1,10})(?:\s*[-—–])?\s*$",
    re.IGNORECASE,
)
_LATIN_NAME_STOPWORDS = frozenset(
    {
        "after",
        "also",
        "avant",
        "chapter",
        "conclusion",
        "das",
        "der",
        "die",
        "dies",
        "ein",
        "eine",
        "first",
        "for",
        "from",
        "gender",
        "introduction",
        "les",
        "notes",
        "part",
        "preface",
        "second",
        "the",
        "this",
        "une",
        "with",
    }
)
_HAN_VARIANT_GROUPS = (
    "國国",
    "學学",
    "體体",
    "會会",
    "發発发",
    "圖図图",
    "條条",
    "東东",
    "門门",
    "間间",
    "觀観观",
    "點点",
    "實実实",
    "權権权",
    "戰戦战",
    "變変变",
    "廣広广",
    "樂楽乐",
    "機机",
    "讀読读",
    "寫写",
    "齋斎斉斋",
    "籐藤",
    "邊辺边",
    "澤沢泽",
    "龍竜龙",
    "島岛",
    "氣気气",
    "關関关",
    "係系",
    "勞労劳",
    "榮栄荣",
    "應応应",
    "經経经",
    "濟済济",
    "總総总",
    "術术",
    "歷歴历",
    "當当",
    "與与",
    "專専专",
    "業业",
    "產産产",
    "壓圧压",
    "惡悪恶",
    "圍囲围",
    "團団团",
    "員员",
    "聯联",
    "續続续",
    "覺覚觉",
    "說説说",
    "證証证",
    "讓譲让",
    "轉転转",
    "輕軽轻",
    "達达",
    "違违",
    "遺遗",
    "選选",
    "錄録录",
    "鐵鉄铁",
    "長长",
    "開开",
    "險険险",
    "際际",
    "雜雑杂",
    "靈霊灵",
    "領领",
    "類类",
    "驗験验",
)
_HAN_VARIANT_TRANSLATION = str.maketrans(
    {
        character: group[-1]
        for group in _HAN_VARIANT_GROUPS
        for character in group[:-1]
    }
)


def _base_language_code(language: str) -> str:
    return str(language or "und").strip().casefold().split("-", 1)[0]


def _language_script(language: str) -> str:
    base = _base_language_code(language)
    if base == "zh":
        return "han"
    if base == "ja":
        return "japanese"
    if base in _LATIN_ANCHOR_LANGUAGES:
        return "latin"
    return "unknown"


def _normalized_chinese_number(value: str) -> str:
    if not any(character in "十百千万" for character in value):
        return "".join(str(_CHINESE_NUMBER_VALUES[character]) for character in value)
    unit_values = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    section = 0
    current = 0
    for character in value:
        if character in _CHINESE_NUMBER_VALUES:
            current = _CHINESE_NUMBER_VALUES[character]
            continue
        unit = unit_values[character]
        if unit == 10000:
            section = (section + current) * unit
            total += section
            section = 0
        else:
            section += (current or 1) * unit
        current = 0
    return str(total + section + current)


def normalize_numeric_text(value: str, language: str) -> str:
    """Normalize language-specific number spelling before anchor extraction."""

    normalized = unicodedata.normalize("NFKC", value)
    base = _base_language_code(language)
    if base == "zh":
        normalized = re.sub(
            r"百分之([零〇一二两三四五六七八九十百千万]+)",
            lambda match: f"{_normalized_chinese_number(match.group(1))}%",
            normalized,
        )
        normalized = re.sub(
            r"[零〇一二两三四五六七八九十百千万]+"
            r"(?=\s*(?:年|世纪|世紀|个百分点|個百分點))",
            lambda match: _normalized_chinese_number(match.group(0)),
            normalized,
        )
        normalized = re.sub(
            r"[零〇一二两三四五六七八九]{2,}",
            lambda match: (
                _normalized_chinese_number(match.group(0))
                if "零" in match.group(0) or "〇" in match.group(0)
                else match.group(0)
            ),
            normalized,
        )
    if base in {"de", "fr"}:
        normalized = re.sub(r"(?<=\d),(?=\d)", ".", normalized)
    if base == "fr":
        while re.search(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?:\D|$))", normalized):
            normalized = re.sub(
                r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?:\D|$))",
                "",
                normalized,
            )
    return normalized


def _canonical_decimal(value: str) -> str:
    whole, separator, fraction = value.partition(".")
    whole = str(int(whole))
    if not separator:
        return whole
    fraction = fraction.rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


def _excluded_anchor_indices(texts: Sequence[str]) -> set[int]:
    numbered_sections = any(
        match is not None and match.group(2) in {"章", "篇", "部"}
        for text in texts
        for line in text.splitlines()
        for match in [_CHINESE_HEADING.fullmatch(line.strip())]
    )
    _titles, excluded = _latin_toc_titles(
        texts, numbered_sections=numbered_sections
    )
    excluded = set(excluded)
    for index, text in enumerate(texts):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if _PAGE_NUMBER_BLOCK.fullmatch(text):
            excluded.add(index)
        if index not in excluded and any(
            _ENDNOTES_HEADING.fullmatch(line) for line in lines
        ):
            excluded.update(range(index, len(texts)))
            break
        if _EXPLICIT_TRANSLATOR_NOTE.search(text) or _INLINE_NOTE_MARKER.search(text):
            excluded.add(index)
    for block in _collected_note_blocks(texts):
        excluded.update(range(block.start, block.end))
    return excluded


def _number_occurrences(
    texts: Sequence[str], language: str
) -> Dict[str, List[int]]:
    occurrences: Dict[str, List[int]] = {}
    excluded = _excluded_anchor_indices(texts)
    contextual_patterns = (
        (
            re.compile(
                r"(?:公元前\s*(\d+(?:\.\d+)?)\s*年?"
                r"|(\d+(?:\.\d+)?)\s*(?:b\.?\s*c\.?\s*e?\.?"
                r"|v\.?\s*chr\.?|av\.?\s*j\.?-?c\.?))",
                re.IGNORECASE,
            ),
            "bce-",
        ),
        (
            re.compile(
                r"(\d{1,2})(?:st|nd|rd|th|er|e|ème)?\s*"
                r"(?:世纪|世紀|century|jahrhundert|siècle)",
                re.IGNORECASE,
            ),
            "century-",
        ),
        (
            re.compile(
                r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s+cent|prozent|pour\s+cent)",
                re.IGNORECASE,
            ),
            "percent-",
        ),
    )
    for index, raw_text in enumerate(texts):
        if index in excluded:
            continue
        text = normalize_numeric_text(raw_text, language)
        occupied: List[Tuple[int, int]] = []
        for pattern, prefix in contextual_patterns:
            for match in pattern.finditer(text):
                raw_number = next(
                    group for group in match.groups() if group is not None
                )
                key = prefix + _canonical_decimal(raw_number)
                occurrences.setdefault(key, []).append(index)
                occupied.append(match.span())
        for match in re.finditer(r"(?<!\d)(?:1\d{3}|20\d{2})(?!\d)", text):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            key = match.group(0)
            occurrences.setdefault(key, []).append(index)
            occupied.append(match.span())
        for match in re.finditer(r"(?<![\w.])\d{2,}(?:\.\d+)?(?![\w.])", text):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            key = _canonical_decimal(match.group(0))
            occurrences.setdefault(key, []).append(index)
    return occurrences


def _paired_occurrence_anchors(
    source: Dict[str, List[int]],
    target: Dict[str, List[int]],
    *,
    key_prefix: str,
    maximum_frequency: int = 3,
) -> List[HeadingAnchor]:
    anchors: List[HeadingAnchor] = []
    for value in sorted(source.keys() & target.keys()):
        source_indices = source[value]
        target_indices = target[value]
        if (
            len(source_indices) != len(target_indices)
            or not source_indices
            or len(source_indices) > maximum_frequency
        ):
            continue
        anchors.extend(
            HeadingAnchor(
                source_index,
                target_index,
                f"{key_prefix}:{value.replace(' ', '-')}",
            )
            for source_index, target_index in zip(source_indices, target_indices)
        )
    return anchors


def extract_number_anchors(
    source_language: str,
    target_language: str,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
) -> List[HeadingAnchor]:
    """Pair rare years and contextual numbers shared by both editions."""

    return _paired_occurrence_anchors(
        _number_occurrences(source_texts, source_language),
        _number_occurrences(target_texts, target_language),
        key_prefix="number",
    )


def _fold_latin(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _latin_phrase_occurrences(
    texts: Sequence[str],
    phrase: str,
    *,
    excluded: set[int] | None = None,
    normalized_texts: Sequence[str] | None = None,
) -> List[int]:
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])")
    excluded_indices = (
        _excluded_anchor_indices(texts) if excluded is None else excluded
    )
    folded = (
        [_fold_latin(text) for text in texts]
        if normalized_texts is None
        else normalized_texts
    )
    return [
        index
        for index, text in enumerate(folded)
        if index not in excluded_indices
        for _match in pattern.finditer(text)
    ]


def extract_parenthetical_term_anchors(
    source_language: str,
    target_language: str,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
) -> List[HeadingAnchor]:
    """Match a Latin original in Chinese parentheses to the Latin edition."""

    source_is_chinese = _base_language_code(source_language) == "zh"
    target_is_chinese = _base_language_code(target_language) == "zh"
    if source_is_chinese == target_is_chinese:
        return []
    chinese_texts = source_texts if source_is_chinese else target_texts
    latin_texts = target_texts if source_is_chinese else source_texts
    chinese_excluded = _excluded_anchor_indices(chinese_texts)
    chinese_occurrences: Dict[str, List[int]] = {}
    for index, text in enumerate(chinese_texts):
        if index in chinese_excluded:
            continue
        for match in _PARENTHETICAL_ORIGINAL.finditer(text):
            prefix = text[max(0, match.start() - 24) : match.start()].rstrip()
            original = match.group(1)
            if (
                not prefix
                or re.search(
                    r"[\u3400-\u9fff][”’\"'》〉」』】〕］]*\s*$", prefix
                )
                is None
                or _HAN_RUN.search(original)
                or not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", original)
            ):
                continue
            normalized = _fold_latin(original)
            if len(normalized) >= 4:
                chinese_occurrences.setdefault(normalized, []).append(index)
    latin_excluded = _excluded_anchor_indices(latin_texts)
    normalized_latin_texts = [_fold_latin(text) for text in latin_texts]
    latin_occurrences = {
        phrase: _latin_phrase_occurrences(
            latin_texts,
            phrase,
            excluded=latin_excluded,
            normalized_texts=normalized_latin_texts,
        )
        for phrase in chinese_occurrences
    }
    anchors: List[HeadingAnchor] = []
    for phrase in sorted(chinese_occurrences.keys() & latin_occurrences.keys()):
        chinese_indices = chinese_occurrences[phrase]
        latin_indices = latin_occurrences[phrase]
        if len(chinese_indices) == len(latin_indices) <= 3:
            pairs = zip(chinese_indices, latin_indices)
        elif len(chinese_indices) == 1 and len(latin_indices) <= 8:
            pairs = (
                (chinese_indices[0], latin_index)
                for latin_index in latin_indices
            )
        else:
            continue
        anchors.extend(
            HeadingAnchor(
                chinese_index,
                latin_index,
                f"term:{phrase.replace(' ', '-')}",
            )
            for chinese_index, latin_index in pairs
        )
    if source_is_chinese:
        return anchors
    return [
        HeadingAnchor(anchor.target_index, anchor.source_index, anchor.key)
        for anchor in anchors
    ]


def _titlecase_name_occurrences(
    texts: Sequence[str], language: str
) -> Dict[str, List[int]]:
    occurrences: Dict[str, List[int]] = {}
    excluded = _excluded_anchor_indices(texts)
    for index, text in enumerate(texts):
        if index in excluded:
            continue
        words = list(_LATIN_WORD.finditer(text))
        runs: List[List[re.Match[str]]] = []
        current: List[re.Match[str]] = []
        for word in words:
            token = word.group(0)
            titlecase = token[0].isupper()
            contiguous = (
                not current
                or re.fullmatch(r"[\s·]*", text[current[-1].end() : word.start()])
                is not None
            )
            if titlecase and contiguous:
                current.append(word)
            else:
                if current:
                    runs.append(current)
                current = [word] if titlecase else []
        if current:
            runs.append(current)
        for run in runs:
            for word in run:
                normalized = _fold_latin(word.group(0))
                if (
                    len(normalized) >= 5
                    and normalized not in _LATIN_NAME_STOPWORDS
                ):
                    occurrences.setdefault(normalized, []).append(index)
            if 2 <= len(run) <= 4:
                normalized = _fold_latin(" ".join(word.group(0) for word in run))
                occurrences.setdefault(normalized, []).append(index)
    return occurrences


def extract_latin_name_anchors(
    source_language: str,
    target_language: str,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
) -> List[HeadingAnchor]:
    """Match low-frequency title-case names across Latin-script editions."""

    if {
        _base_language_code(source_language),
        _base_language_code(target_language),
    } - _LATIN_ANCHOR_LANGUAGES:
        return []
    return _paired_occurrence_anchors(
        _titlecase_name_occurrences(source_texts, source_language),
        _titlecase_name_occurrences(target_texts, target_language),
        key_prefix="name",
    )


def _normalize_han(value: str) -> str:
    return unicodedata.normalize("NFKC", value).translate(_HAN_VARIANT_TRANSLATION)


def _han_phrase_occurrences(
    texts: Sequence[str],
    phrase: str,
    *,
    excluded: set[int] | None = None,
    normalized_texts: Sequence[str] | None = None,
) -> List[int]:
    excluded_indices = (
        _excluded_anchor_indices(texts) if excluded is None else excluded
    )
    folded = (
        [_normalize_han(text) for text in texts]
        if normalized_texts is None
        else normalized_texts
    )
    return [
        index
        for index, text in enumerate(folded)
        if index not in excluded_indices
        for _match in re.finditer(re.escape(phrase), text)
    ]


def extract_chinese_japanese_name_anchors(
    source_language: str,
    target_language: str,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
) -> List[HeadingAnchor]:
    """Match rare 2–6 Han-character runs after common glyph folding."""

    source_is_japanese = _base_language_code(source_language) == "ja"
    target_is_japanese = _base_language_code(target_language) == "ja"
    source_is_chinese = _base_language_code(source_language) == "zh"
    target_is_chinese = _base_language_code(target_language) == "zh"
    if not (
        (source_is_japanese and target_is_chinese)
        or (target_is_japanese and source_is_chinese)
    ):
        return []
    japanese_texts = source_texts if source_is_japanese else target_texts
    chinese_texts = target_texts if source_is_japanese else source_texts
    japanese_excluded = _excluded_anchor_indices(japanese_texts)
    japanese_occurrences: Dict[str, List[int]] = {}
    for index, text in enumerate(japanese_texts):
        if index in japanese_excluded:
            continue
        for match in _HAN_RUN.finditer(_normalize_han(text)):
            phrase = match.group(0)
            if 2 <= len(phrase) <= 6:
                japanese_occurrences.setdefault(phrase, []).append(index)
    chinese_excluded = _excluded_anchor_indices(chinese_texts)
    normalized_chinese_texts = [_normalize_han(text) for text in chinese_texts]
    chinese_occurrences = {
        phrase: _han_phrase_occurrences(
            chinese_texts,
            phrase,
            excluded=chinese_excluded,
            normalized_texts=normalized_chinese_texts,
        )
        for phrase in japanese_occurrences
    }
    anchors = _paired_occurrence_anchors(
        japanese_occurrences,
        chinese_occurrences,
        key_prefix="name",
        maximum_frequency=1,
    )
    if source_is_japanese:
        return anchors
    return [
        HeadingAnchor(anchor.target_index, anchor.source_index, anchor.key)
        for anchor in anchors
    ]


AnchorExtractor = Callable[
    [str, str, Sequence[str], Sequence[str]], List[HeadingAnchor]
]


@dataclass(frozen=True)
class _RegisteredAnchorExtractor:
    name: str
    priority: int
    script_pairs: frozenset[Tuple[str, str]] | None
    extractor: AnchorExtractor


def _anchors_compatible(left: HeadingAnchor, right: HeadingAnchor) -> bool:
    return (
        left.source_index != right.source_index
        and left.target_index != right.target_index
        and (left.source_index < right.source_index)
        == (left.target_index < right.target_index)
    )


def _longest_compatible_anchor_chain(
    candidates: Sequence[HeadingAnchor],
    fixed_anchors: Sequence[HeadingAnchor],
) -> List[HeadingAnchor]:
    unique: List[HeadingAnchor] = []
    seen_coordinates: set[Tuple[int, int]] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (
            item.source_index,
            item.target_index,
            -len(item.key),
            item.key,
        ),
    ):
        coordinates = (candidate.source_index, candidate.target_index)
        if coordinates in seen_coordinates or not all(
            _anchors_compatible(candidate, fixed) for fixed in fixed_anchors
        ):
            continue
        seen_coordinates.add(coordinates)
        unique.append(candidate)
    if not unique:
        return []
    paths: List[Tuple[int, ...]] = []
    for candidate_index, candidate in enumerate(unique):
        path = (candidate_index,)
        for previous_index, previous in enumerate(unique[:candidate_index]):
            if not _anchors_compatible(previous, candidate):
                continue
            proposed = paths[previous_index] + (candidate_index,)
            if len(proposed) > len(path):
                path = proposed
        paths.append(path)
    selected = max(paths, key=len)
    return [unique[index] for index in selected]


class AnchorExtractorRegistry:
    """Activate exact anchor extractors by language-script pair and priority."""

    def __init__(self) -> None:
        self._extractors: List[_RegisteredAnchorExtractor] = []

    def register(
        self,
        name: str,
        priority: int,
        script_pairs: frozenset[Tuple[str, str]] | None,
        extractor: AnchorExtractor,
    ) -> None:
        self._extractors.append(
            _RegisteredAnchorExtractor(name, priority, script_pairs, extractor)
        )

    def extract(
        self,
        source_language: str,
        target_language: str,
        texts: Tuple[Sequence[str], Sequence[str]],
        *,
        fixed_anchors: Sequence[HeadingAnchor] = (),
    ) -> List[HeadingAnchor]:
        source_texts, target_texts = texts
        script_pair = (
            _language_script(source_language),
            _language_script(target_language),
        )
        candidates_by_priority: Dict[int, List[HeadingAnchor]] = {}
        candidate_counts: Dict[str, int] = {}
        for registered in self._extractors:
            if (
                registered.script_pairs is not None
                and script_pair not in registered.script_pairs
            ):
                continue
            candidates = registered.extractor(
                source_language,
                target_language,
                source_texts,
                target_texts,
            )
            candidate_counts[registered.name] = len(candidates)
            candidates_by_priority.setdefault(registered.priority, []).extend(
                candidates
            )
        selected: List[HeadingAnchor] = []
        conflict_count = 0
        for priority in sorted(candidates_by_priority, reverse=True):
            candidates = candidates_by_priority[priority]
            higher_anchors = [*fixed_anchors, *selected]
            candidates = [
                candidate
                for candidate in candidates
                if all(
                    _anchors_compatible(candidate, fixed)
                    for fixed in higher_anchors
                )
            ]
            coordinate_keys: Dict[Tuple[int, int], set[str]] = {}
            for candidate in candidates:
                coordinate_keys.setdefault(
                    (candidate.source_index, candidate.target_index), set()
                ).add(candidate.key)
            source_choices: Dict[int, Dict[int, int]] = {}
            target_choices: Dict[int, Dict[int, int]] = {}
            for (source_index, target_index), keys in coordinate_keys.items():
                support = len(keys)
                source_choices.setdefault(source_index, {})[target_index] = support
                target_choices.setdefault(target_index, {})[source_index] = support
            source_winners: Dict[int, int] = {}
            for source_index, choices in source_choices.items():
                strongest = max(choices.values())
                winners = [
                    target_index
                    for target_index, support in choices.items()
                    if support == strongest
                ]
                if len(winners) == 1:
                    source_winners[source_index] = winners[0]
            target_winners: Dict[int, int] = {}
            for target_index, choices in target_choices.items():
                strongest = max(choices.values())
                winners = [
                    source_index
                    for source_index, support in choices.items()
                    if support == strongest
                ]
                if len(winners) == 1:
                    target_winners[target_index] = winners[0]
            candidates = [
                candidate
                for candidate in candidates
                if source_winners.get(candidate.source_index)
                == candidate.target_index
                and target_winners.get(candidate.target_index)
                == candidate.source_index
            ]
            chain = _longest_compatible_anchor_chain(
                candidates, higher_anchors
            )
            conflict_count += len(candidates_by_priority[priority]) - len(chain)
            selected.extend(chain)
        selected.sort(
            key=lambda item: (item.source_index, item.target_index, item.key)
        )
        selected_counts = {
            kind: sum(anchor.key.startswith(f"{kind}:") for anchor in selected)
            for kind in _ANCHOR_PRIORITY
        }
        LOGGER.info(
            "semantic anchors source=%s target=%s candidates=%s selected=%s "
            "conflict_dropped=%d",
            source_language,
            target_language,
            candidate_counts,
            selected_counts,
            conflict_count,
        )
        return selected


def _default_anchor_extractor_registry() -> AnchorExtractorRegistry:
    registry = AnchorExtractorRegistry()
    registry.register("number", _ANCHOR_PRIORITY["number"], None, extract_number_anchors)
    registry.register(
        "term",
        _ANCHOR_PRIORITY["term"],
        frozenset({("han", "latin"), ("latin", "han")}),
        extract_parenthetical_term_anchors,
    )
    registry.register(
        "latin_name",
        _ANCHOR_PRIORITY["name"],
        frozenset({("latin", "latin")}),
        extract_latin_name_anchors,
    )
    registry.register(
        "cjk_name",
        _ANCHOR_PRIORITY["name"],
        frozenset({("han", "japanese"), ("japanese", "han")}),
        extract_chinese_japanese_name_anchors,
    )
    return registry


DEFAULT_ANCHOR_EXTRACTOR_REGISTRY = _default_anchor_extractor_registry()


def _normalized_rows(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def _group_vector(prefix: np.ndarray, start: int, end: int) -> np.ndarray:
    vector = prefix[end] - prefix[start]
    return vector / max(float(np.linalg.norm(vector)), 1e-12)


def _group_rows(prefix: np.ndarray) -> Dict[int, np.ndarray]:
    groups: Dict[int, np.ndarray] = {}
    for count in range(1, 4):
        if len(prefix) <= count:
            groups[count] = np.empty((0, prefix.shape[1]), dtype=np.float32)
            continue
        vectors = prefix[count:] - prefix[:-count]
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        groups[count] = vectors / np.maximum(norms, 1e-12)
    return groups


def _similarity(
    source_prefix: np.ndarray,
    target_prefix: np.ndarray,
    source_start: int,
    source_end: int,
    target_start: int,
    target_end: int,
) -> float:
    if source_start == source_end or target_start == target_end:
        return 0.0
    source = _group_vector(source_prefix, source_start, source_end)
    target = _group_vector(target_prefix, target_start, target_end)
    return float(np.clip(source @ target, -1.0, 1.0))


def _transition_cost(
    source_prefix: np.ndarray,
    target_prefix: np.ndarray,
    source_lengths: Sequence[int],
    target_lengths: Sequence[int],
    source_start: int,
    target_start: int,
    source_count: int,
    target_count: int,
    length_ratio: float,
    penalty: float,
) -> Tuple[float, float]:
    source_length = sum(source_lengths[source_start : source_start + source_count])
    target_length = sum(target_lengths[target_start : target_start + target_count])
    if source_count == 0 or target_count == 0:
        return penalty + math.log1p(source_length + target_length) / 12.0, 0.0
    similarity = _similarity(
        source_prefix,
        target_prefix,
        source_start,
        source_start + source_count,
        target_start,
        target_start + target_count,
    )
    expected = max(length_ratio * source_length, 1.0)
    length_cost = 0.18 * abs(math.log(max(target_length, 1) / expected))
    return penalty + (1.0 - similarity) * 3.0 + length_cost, similarity


def _align_partition(
    source_prefix: np.ndarray,
    target_prefix: np.ndarray,
    source_lengths: Sequence[int],
    target_lengths: Sequence[int],
    source_offset: int,
    source_end: int,
    target_offset: int,
    target_end: int,
    source_groups: Dict[int, np.ndarray],
    target_groups: Dict[int, np.ndarray],
    low_confidence_threshold: float,
) -> List[SemanticLink]:
    source_count = source_end - source_offset
    target_count = target_end - target_offset
    if not source_count and not target_count:
        return []
    local_source_lengths = source_lengths[source_offset:source_end]
    local_target_lengths = target_lengths[target_offset:target_end]
    length_ratio = sum(local_target_lengths) / max(sum(local_source_lengths), 1)
    source_length_prefix = np.concatenate(
        ([0], np.cumsum(source_lengths, dtype=np.int64))
    )
    target_length_prefix = np.concatenate(
        ([0], np.cumsum(target_lengths, dtype=np.int64))
    )
    # The centre line already scales by target/source count.  Expanding the
    # band by the absolute document-size difference turns a 6k x 10k book pair
    # back into an almost quadratic matrix.  Only the largest per-row jump has
    # to fit inside adjacent bands.
    target_step = math.ceil(target_count / max(source_count, 1))
    band = max(_SEARCH_BAND, target_step + 3)
    row_bounds: List[Tuple[int, int]] = []
    back_rows: List[bytearray] = []
    recent_costs: Dict[int, Tuple[int, List[float]]] = {}

    def bounds(source_index: int) -> Tuple[int, int]:
        if not source_count:
            return 0, target_count
        expected = round(source_index * target_count / source_count)
        return max(0, expected - band), min(target_count, expected + band)

    for source_index in range(source_count + 1):
        row_start, row_end = bounds(source_index)
        row_bounds.append((row_start, row_end))
        costs = [math.inf] * (row_end - row_start + 1)
        backs = bytearray([255]) * len(costs)
        if source_index == 0 and row_start == 0:
            costs[0] = 0.0
        recent_costs[source_index] = (row_start, costs)
        target_positions = np.arange(row_start, row_end + 1, dtype=np.int64)
        transition_rows: List[np.ndarray] = []
        for di, dj, penalty in _TRANSITIONS:
            transition_costs = np.full(len(costs), math.inf, dtype=np.float32)
            previous_source = source_index - di
            previous_targets = target_positions - dj
            valid = (previous_source >= 0) & (previous_targets >= 0)
            if not np.any(valid):
                transition_rows.append(transition_costs)
                continue
            absolute_source = source_offset + previous_source
            absolute_targets = target_offset + previous_targets[valid]
            source_length = int(
                source_length_prefix[absolute_source + di]
                - source_length_prefix[absolute_source]
            )
            target_length = (
                target_length_prefix[absolute_targets + dj]
                - target_length_prefix[absolute_targets]
            )
            if di == 0 or dj == 0:
                transition_costs[valid] = (
                    penalty + np.log1p(source_length + target_length) / 12.0
                )
            else:
                similarities = (
                    target_groups[dj][absolute_targets]
                    @ source_groups[di][absolute_source]
                )
                expected = max(length_ratio * source_length, 1.0)
                length_cost = 0.18 * np.abs(
                    np.log(np.maximum(target_length, 1) / expected)
                )
                transition_costs[valid] = (
                    penalty + (1.0 - similarities) * 3.0 + length_cost
                )
            transition_rows.append(transition_costs)
        for target_index in range(row_start, row_end + 1):
            cell_offset = target_index - row_start
            if source_index == 0 and target_index == 0:
                continue
            best = math.inf
            best_transition = 255
            for transition_index, (di, dj, penalty) in enumerate(_TRANSITIONS):
                previous_source = source_index - di
                previous_target = target_index - dj
                if previous_source < 0 or previous_target < 0:
                    continue
                previous_row = recent_costs.get(previous_source)
                if previous_row is None:
                    continue
                previous_start, previous_costs = previous_row
                previous_offset = previous_target - previous_start
                if previous_offset < 0 or previous_offset >= len(previous_costs):
                    continue
                previous_cost = previous_costs[previous_offset]
                if math.isinf(previous_cost):
                    continue
                transition_cost = float(transition_rows[transition_index][cell_offset])
                candidate = previous_cost + transition_cost
                if candidate < best:
                    best = candidate
                    best_transition = transition_index
            costs[cell_offset] = best
            backs[cell_offset] = best_transition
        back_rows.append(backs)
        for expired in tuple(recent_costs):
            if expired < source_index - 3:
                del recent_costs[expired]

    destination_start, destination_costs = recent_costs[source_count]
    destination_offset = target_count - destination_start
    if (
        destination_offset < 0
        or destination_offset >= len(destination_costs)
        or math.isinf(destination_costs[destination_offset])
    ):
        raise SemanticAlignmentError("语义对齐路径超出章节搜索带。")

    links: List[SemanticLink] = []
    source_index = source_count
    target_index = target_count
    while source_index or target_index:
        row_start, _row_end = row_bounds[source_index]
        transition_index = back_rows[source_index][target_index - row_start]
        if transition_index == 255:
            raise SemanticAlignmentError("无法回溯完整的语义对齐路径。")
        di, dj, penalty = _TRANSITIONS[transition_index]
        previous_source = source_index - di
        previous_target = target_index - dj
        cost, confidence = _transition_cost(
            source_prefix,
            target_prefix,
            source_lengths,
            target_lengths,
            source_offset + previous_source,
            target_offset + previous_target,
            di,
            dj,
            length_ratio,
            penalty,
        )
        links.append(
            SemanticLink(
                source_offset + previous_source,
                source_offset + source_index,
                target_offset + previous_target,
                target_offset + target_index,
                cost,
                confidence,
                (
                    "unmatched"
                    if not di or not dj
                    else (
                        "automatic"
                        if confidence >= low_confidence_threshold
                        else "rejected"
                    )
                ),
            )
        )
        source_index = previous_source
        target_index = previous_target
    links.reverse()
    return links


def _align_monotonic_sequences(
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    source_vectors: np.ndarray,
    target_vectors: np.ndarray,
    boundary_anchors: Sequence[HeadingAnchor] = (),
    *,
    source_language: str = "und",
    target_language: str = "und",
    anchor_registry: AnchorExtractorRegistry | None = DEFAULT_ANCHOR_EXTRACTOR_REGISTRY,
    thresholds: AlignmentThresholds = _DEFAULT_THRESHOLDS,
) -> Tuple[List[SemanticLink], List[HeadingAnchor]]:
    source_count = len(source_texts)
    target_count = len(target_texts)
    source_prefix = np.vstack(
        [np.zeros((1, source_vectors.shape[1]), dtype=np.float32), np.cumsum(source_vectors, axis=0)]
    )
    target_prefix = np.vstack(
        [np.zeros((1, target_vectors.shape[1]), dtype=np.float32), np.cumsum(target_vectors, axis=0)]
    )
    source_groups = _group_rows(source_prefix)
    target_groups = _group_rows(target_prefix)
    source_lengths = [max(1, sum(not char.isspace() for char in text)) for text in source_texts]
    target_lengths = [max(1, sum(not char.isspace() for char in text)) for text in target_texts]
    structural_anchors = find_heading_anchors(source_texts, target_texts)
    paragraph_anchors = [
        anchor for anchor in structural_anchors if anchor.key.startswith("paragraph:")
    ]
    boundary_candidates: List[HeadingAnchor] = []
    seen_boundary_coordinates: set[Tuple[int, int]] = set()
    for boundary in sorted(
        boundary_anchors,
        key=lambda item: (item.source_index, item.target_index, item.key),
    ):
        coordinates = (boundary.source_index, boundary.target_index)
        if (
            not boundary.key.startswith("folio:")
            or coordinates in seen_boundary_coordinates
            or not 0 <= boundary.source_index <= source_count
            or not 0 <= boundary.target_index <= target_count
            or any(
                boundary.source_index == anchor.source_index
                or boundary.target_index == anchor.target_index
                or (boundary.source_index < anchor.source_index)
                != (boundary.target_index < anchor.target_index)
                for anchor in paragraph_anchors
            )
        ):
            continue
        seen_boundary_coordinates.add(coordinates)
        boundary_candidates.append(boundary)

    boundary_paths: List[Tuple[int, ...]] = []
    for candidate_index, candidate in enumerate(boundary_candidates):
        path = (candidate_index,)
        for previous_index, previous in enumerate(
            boundary_candidates[:candidate_index]
        ):
            if (
                previous.source_index >= candidate.source_index
                or previous.target_index >= candidate.target_index
            ):
                continue
            proposed = boundary_paths[previous_index] + (candidate_index,)
            if len(proposed) > len(path):
                path = proposed
        boundary_paths.append(path)
    boundaries = (
        [
            boundary_candidates[index]
            for index in boundary_paths[
                max(range(len(boundary_paths)), key=lambda index: len(boundary_paths[index]))
            ]
        ]
        if boundary_paths
        else []
    )
    fixed_anchors = [*paragraph_anchors, *boundaries]
    secondary_anchors = [
        anchor
        for anchor in structural_anchors
        if not anchor.key.startswith("paragraph:")
        and all(
            anchor.source_index != fixed.source_index
            and anchor.target_index != fixed.target_index
            and (anchor.source_index < fixed.source_index)
            == (anchor.target_index < fixed.target_index)
            for fixed in fixed_anchors
        )
    ]
    anchors = sorted(
        [*fixed_anchors, *secondary_anchors],
        key=lambda item: (item.source_index, item.target_index, item.key),
    )
    if anchor_registry is not None:
        anchors = sorted(
            [
                *anchors,
                *anchor_registry.extract(
                    source_language,
                    target_language,
                    (source_texts, target_texts),
                    fixed_anchors=anchors,
                ),
            ],
            key=lambda item: (item.source_index, item.target_index, item.key),
        )
    links: List[SemanticLink] = []
    source_cursor = 0
    target_cursor = 0
    for anchor in anchors:
        links.extend(
            _align_partition(
                source_prefix,
                target_prefix,
                source_lengths,
                target_lengths,
                source_cursor,
                anchor.source_index,
                target_cursor,
                anchor.target_index,
                source_groups,
                target_groups,
                thresholds.low,
            )
        )
        if anchor.key.startswith("folio:"):
            source_cursor = anchor.source_index
            target_cursor = anchor.target_index
            continue
        confidence = _similarity(
            source_prefix,
            target_prefix,
            anchor.source_index,
            anchor.source_index + 1,
            anchor.target_index,
            anchor.target_index + 1,
        )
        links.append(
            SemanticLink(
                anchor.source_index,
                anchor.source_index + 1,
                anchor.target_index,
                anchor.target_index + 1,
                1.0 - confidence,
                confidence,
                "automatic",
                anchor.key,
            )
        )
        source_cursor = anchor.source_index + 1
        target_cursor = anchor.target_index + 1
    links.extend(
        _align_partition(
            source_prefix,
            target_prefix,
            source_lengths,
            target_lengths,
            source_cursor,
            source_count,
            target_cursor,
            target_count,
            source_groups,
            target_groups,
            thresholds.low,
        )
    )
    return links, anchors


def _trailing_latin_note_number(text: str) -> int:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 0
    match = _LATIN_NOTE_MARKER.fullmatch(lines[-1])
    return int(match.group(1)) if match is not None else 0


def _collected_note_blocks(texts: Sequence[str]) -> List[_NumberedNoteBlock]:
    candidates: List[Tuple[int, int]] = []
    for index, text in enumerate(texts):
        number = _trailing_latin_note_number(text)
        if not number:
            continue
        if candidates and number == candidates[-1][1] and index <= candidates[-1][0] + 1:
            candidates[-1] = (index, number)
        else:
            candidates.append((index, number))

    runs: List[List[int]] = []
    for candidate_index, (_index, number) in enumerate(candidates):
        if number != 1:
            continue
        run = [candidate_index]
        previous = number
        for following_index in range(candidate_index + 1, len(candidates)):
            following_number = candidates[following_index][1]
            if following_number <= previous or following_number > previous + 3:
                break
            run.append(following_index)
            previous = following_number
        if len(run) >= _MIN_COLLECTED_NOTE_RUN:
            runs.append(run)

    if not runs:
        return []
    first_run_start = candidates[runs[0][0]][0]
    if not any(
        _ENDNOTES_HEADING.fullmatch(line.strip())
        for text in texts[: first_run_start + 1]
        for line in text.splitlines()
    ):
        return []

    blocks: List[_NumberedNoteBlock] = []
    for chapter_number, run in enumerate(runs, start=1):
        for position in run:
            start, note_number = candidates[position]
            if position + 1 < len(candidates):
                end = candidates[position + 1][0]
            else:
                end = min(len(texts), start + 32)
                for index in range(start + 1, end):
                    if any(
                        _NOTE_STREAM_TERMINATOR.fullmatch(line.strip())
                        for line in texts[index].splitlines()
                    ):
                        end = index
                        break
            content_start = (
                start + 1
                if _LATIN_NOTE_MARKER.fullmatch(texts[start].strip()) is not None
                else start
            )
            if content_start < end:
                blocks.append(
                    _NumberedNoteBlock(
                        chapter_number,
                        note_number,
                        start,
                        content_start,
                        end,
                    )
                )
    return blocks


def _body_chapter_positions(texts: Sequence[str]) -> List[Tuple[int, int]]:
    return sorted(
        (index, int(key.split(":")[1]))
        for key, index in _document_heading_positions(texts).items()
        if re.fullmatch(r"chapter:\d+", key)
    )


def _inline_note_candidates(
    texts: Sequence[str], *, include_translator: bool = False
) -> List[_InlineNoteCandidate]:
    chapters = _body_chapter_positions(texts)
    if not chapters:
        return []
    candidates: List[_InlineNoteCandidate] = []
    seen: set[Tuple[int, int, int]] = set()
    chapter_cursor = 0
    current_chapter = 0
    for index, text in enumerate(texts):
        while chapter_cursor < len(chapters) and chapters[chapter_cursor][0] <= index:
            current_chapter = chapters[chapter_cursor][1]
            chapter_cursor += 1
        if not current_chapter:
            continue
        if not include_translator and _EXPLICIT_TRANSLATOR_NOTE.search(text):
            continue
        for match in _INLINE_NOTE_MARKER.finditer(text):
            key = (current_chapter, int(match.group(1)), index)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(_InlineNoteCandidate(*key))
    return candidates


def _note_marker_inventory(
    texts: Sequence[str],
) -> Tuple[List[_InlineNoteCandidate], set[int]]:
    blocks = _collected_note_blocks(texts)
    if blocks:
        points: List[_InlineNoteCandidate] = []
        translator_chapters: set[int] = set()
        for block in blocks:
            probe = " ".join(texts[block.start : min(block.end, block.start + 3)])
            if _EXPLICIT_TRANSLATOR_NOTE.search(probe):
                translator_chapters.add(block.chapter_number)
                continue
            points.append(
                _InlineNoteCandidate(
                    block.chapter_number,
                    block.note_number,
                    block.start,
                )
            )
        return points, translator_chapters

    raw = _inline_note_candidates(texts, include_translator=True)
    translator_chapters = {
        point.chapter_number
        for point in raw
        if _EXPLICIT_TRANSLATOR_NOTE.search(texts[point.start])
    }
    return [
        point
        for point in raw
        if not _EXPLICIT_TRANSLATOR_NOTE.search(texts[point.start])
    ], translator_chapters


def _exact_structural_note_chain(
    source_points: Sequence[_InlineNoteCandidate],
    target_points: Sequence[_InlineNoteCandidate],
) -> List[Tuple[_InlineNoteCandidate, _InlineNoteCandidate]]:
    source_by_number: Dict[int, List[_InlineNoteCandidate]] = {}
    target_by_number: Dict[int, List[_InlineNoteCandidate]] = {}
    for point in source_points:
        source_by_number.setdefault(point.note_number, []).append(point)
    for point in target_points:
        target_by_number.setdefault(point.note_number, []).append(point)
    candidates = sorted(
        [
            (source_by_number[number][0], target_by_number[number][0])
            for number in source_by_number.keys() & target_by_number.keys()
            if len(source_by_number[number]) == len(target_by_number[number]) == 1
        ],
        key=lambda pair: (pair[0].start, pair[1].start),
    )
    if not candidates:
        return []
    paths: List[Tuple[int, ...]] = []
    for candidate_index, (source, target) in enumerate(candidates):
        path = (candidate_index,)
        for previous_index, (previous_source, previous_target) in enumerate(
            candidates[:candidate_index]
        ):
            if (
                previous_source.start >= source.start
                or previous_target.start >= target.start
                or previous_source.note_number >= source.note_number
            ):
                continue
            proposed = paths[previous_index] + (candidate_index,)
            if len(proposed) > len(path):
                path = proposed
        paths.append(path)
    selected = paths[max(range(len(paths)), key=lambda index: len(paths[index]))]
    return [candidates[index] for index in selected]


def _structural_note_marker_overrides(
    source_texts: Sequence[str], target_texts: Sequence[str]
) -> List[SemanticLink]:
    source_points, source_translator_chapters = _note_marker_inventory(source_texts)
    target_points, target_translator_chapters = _note_marker_inventory(target_texts)
    links: List[SemanticLink] = []
    chapters = sorted(
        {point.chapter_number for point in source_points}
        & {point.chapter_number for point in target_points}
    )
    for chapter_number in chapters:
        source_chapter = sorted(
            (point for point in source_points if point.chapter_number == chapter_number),
            key=lambda point: point.start,
        )
        target_chapter = sorted(
            (point for point in target_points if point.chapter_number == chapter_number),
            key=lambda point: point.start,
        )
        have_explicit_translator_note = chapter_number in (
            source_translator_chapters | target_translator_chapters
        )
        source_increasing = all(
            left.note_number < right.note_number
            for left, right in zip(source_chapter, source_chapter[1:])
        )
        target_increasing = all(
            left.note_number < right.note_number
            for left, right in zip(target_chapter, target_chapter[1:])
        )
        if (
            have_explicit_translator_note
            and len(source_chapter) == len(target_chapter)
            and source_increasing
            and target_increasing
        ):
            pairs = list(zip(source_chapter, target_chapter))
        else:
            pairs = _exact_structural_note_chain(source_chapter, target_chapter)
        if len(pairs) < _MIN_STRUCTURAL_NOTE_CHAIN:
            continue
        links.extend(
            SemanticLink(
                source.start,
                source.start + 1,
                target.start,
                target.start + 1,
                0.0,
                1.0,
                "note_automatic",
                (
                    f"note-marker:{chapter_number}:"
                    f"{source.note_number}:{target.note_number}"
                ),
            )
            for source, target in pairs
        )
    return links


def _best_inline_note_window(
    block: _NumberedNoteBlock,
    candidate: _InlineNoteCandidate,
    source_prefix: np.ndarray,
    target_prefix: np.ndarray,
    target_count: int,
) -> Tuple[int, float]:
    limit = min(target_count, candidate.start + _MAX_INLINE_NOTE_WINDOW)
    scores = [
        _similarity(
            source_prefix,
            target_prefix,
            block.content_start,
            block.end,
            candidate.start,
            end,
        )
        for end in range(candidate.start + 1, limit + 1)
    ]
    peak = max(scores)
    chosen_offset = next(
        offset for offset, score in enumerate(scores, start=1) if score >= peak - 0.01
    )
    return candidate.start + chosen_offset, peak


def _directional_note_overrides(
    collected_texts: Sequence[str],
    inline_texts: Sequence[str],
    collected_vectors: np.ndarray,
    inline_vectors: np.ndarray,
    thresholds: AlignmentThresholds,
) -> List[SemanticLink]:
    blocks = _collected_note_blocks(collected_texts)
    candidates = _inline_note_candidates(inline_texts)
    if not blocks or not candidates:
        return []

    collected_prefix = np.vstack(
        [
            np.zeros((1, collected_vectors.shape[1]), dtype=np.float32),
            np.cumsum(collected_vectors, axis=0),
        ]
    )
    inline_prefix = np.vstack(
        [
            np.zeros((1, inline_vectors.shape[1]), dtype=np.float32),
            np.cumsum(inline_vectors, axis=0),
        ]
    )
    proposals: List[
        Tuple[float, _NumberedNoteBlock, _InlineNoteCandidate, int]
    ] = []
    for block in blocks:
        matching = [
            candidate
            for candidate in candidates
            if candidate.chapter_number == block.chapter_number
            and candidate.note_number == block.note_number
        ]
        scored = [
            (
                score,
                candidate,
                end,
            )
            for candidate in matching
            for end, score in [
                _best_inline_note_window(
                    block,
                    candidate,
                    collected_prefix,
                    inline_prefix,
                    len(inline_texts),
                )
            ]
        ]
        scored.sort(key=lambda item: item[0])
        if not scored or scored[-1][0] < thresholds.note_block:
            continue
        if len(scored) > 1 and scored[-1][0] - scored[-2][0] < thresholds.margin:
            continue
        score, candidate, end = scored[-1]
        proposals.append((score, block, candidate, end))

    occupied_inline_segments: set[int] = set()
    selected: List[
        Tuple[float, _NumberedNoteBlock, _InlineNoteCandidate, int]
    ] = []
    for score, block, candidate, end in sorted(
        proposals, key=lambda item: item[0], reverse=True
    ):
        candidate_segments = set(range(candidate.start, end))
        if occupied_inline_segments.intersection(candidate_segments):
            continue
        occupied_inline_segments.update(candidate_segments)
        selected.append((score, block, candidate, end))

    return [
        SemanticLink(
            block.start,
            block.end,
            candidate.start,
            end,
            1.0 - score,
            score,
            "note_automatic",
            f"note:{block.chapter_number}:{block.note_number}",
        )
        for score, block, candidate, end in selected
    ]


def _note_override_links(
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    source_vectors: np.ndarray,
    target_vectors: np.ndarray,
    thresholds: AlignmentThresholds,
) -> List[SemanticLink]:
    forward = _directional_note_overrides(
        source_texts, target_texts, source_vectors, target_vectors, thresholds
    )
    if forward:
        content = forward
    else:
        reverse = _directional_note_overrides(
            target_texts, source_texts, target_vectors, source_vectors, thresholds
        )
        content = [
            SemanticLink(
                link.target_start,
                link.target_end,
                link.source_start,
                link.source_end,
                link.cost,
                link.confidence,
                link.review_status,
                link.anchor_key,
            )
            for link in reverse
        ]
    markers = _structural_note_marker_overrides(source_texts, target_texts)
    return [
        *content,
        *[
            marker
            for marker in markers
            if not any(
                link.source_start <= marker.source_start < link.source_end
                and link.target_start <= marker.target_start < link.target_end
                for link in content
            )
        ],
    ]


def _apply_note_overrides(
    links: Sequence[SemanticLink], overrides: Sequence[SemanticLink]
) -> List[SemanticLink]:
    if not overrides:
        return list(links)
    source_segments = {
        index
        for link in overrides
        for index in range(link.source_start, link.source_end)
    }
    target_segments = {
        index
        for link in overrides
        for index in range(link.target_start, link.target_end)
    }
    result: List[SemanticLink] = []
    for link in links:
        conflicts = any(
            index in source_segments for index in range(link.source_start, link.source_end)
        ) or any(
            index in target_segments for index in range(link.target_start, link.target_end)
        )
        result.append(
            SemanticLink(
                link.source_start,
                link.source_end,
                link.target_start,
                link.target_end,
                link.cost,
                link.confidence,
                "rejected" if conflicts else link.review_status,
                link.anchor_key,
            )
        )
    result.extend(overrides)
    return sorted(
        result,
        key=lambda link: (
            link.source_start,
            link.target_start,
            link.source_end,
            link.target_end,
        ),
    )


def align_semantic_sequences(
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    embeddings: np.ndarray,
    boundary_anchors: Sequence[HeadingAnchor] = (),
    *,
    source_language: str = "und",
    target_language: str = "und",
    anchor_registry: AnchorExtractorRegistry | None = DEFAULT_ANCHOR_EXTRACTOR_REGISTRY,
    thresholds: AlignmentThresholds = _DEFAULT_THRESHOLDS,
) -> Tuple[List[SemanticLink], List[HeadingAnchor]]:
    """Align segments with structural links and partition-only folio boundaries."""

    source_count = len(source_texts)
    target_count = len(target_texts)
    if not source_count or not target_count:
        raise SemanticAlignmentError("两本文献都必须至少包含一个 Segment。")
    normalized = _normalized_rows(np.asarray(embeddings, dtype=np.float32))
    source_vectors = normalized[:source_count]
    target_vectors = normalized[source_count:]
    links, anchors = _align_monotonic_sequences(
        source_texts,
        target_texts,
        source_vectors,
        target_vectors,
        boundary_anchors,
        source_language=source_language,
        target_language=target_language,
        anchor_registry=anchor_registry,
        thresholds=thresholds,
    )
    overrides = _note_override_links(
        source_texts, target_texts, source_vectors, target_vectors, thresholds
    )
    aligned = _apply_note_overrides(links, overrides)
    rejected_keys = {
        link.anchor_key
        for link in aligned
        if link.review_status == "rejected" and link.anchor_key
    }
    rejected_counts = {
        kind: sum(key.startswith(f"{kind}:") for key in rejected_keys)
        for kind in ("chapter", "paragraph", "preface", "term", "number", "name")
    }
    LOGGER.info("semantic anchors rejected_after_alignment=%s", rejected_counts)
    return aligned, anchors


def alignment_transitions() -> List[List[int]]:
    return [[di, dj] for di, dj, _penalty in _TRANSITIONS]
