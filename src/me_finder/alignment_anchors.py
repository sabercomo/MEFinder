"""跨语言词汇锚点的抽取与择优。

数字、括注原文、拉丁人名、中日人名四类抽取器，以及按语言脚本与优先级择优的
``AnchorExtractorRegistry``。锚点只在正文段上抽取，非正文段的排除依赖
``alignment_structure``；本模块不感知嵌入与 DP 对齐。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence, Tuple

from .alignment_structure import (
    _CHINESE_HEADING,
    _CHINESE_NUMBER_VALUES,
    _ENDNOTES_HEADING,
    _EXPLICIT_TRANSLATOR_NOTE,
    _INLINE_NOTE_MARKER,
    _collected_note_blocks,
    _latin_toc_titles,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeadingAnchor:
    source_index: int
    target_index: int
    key: str


_ANCHOR_PRIORITY = {"term": 3, "number": 2, "name": 1}


# Surface-matched anchors (a shared Latin term, a shared number, a shared name)
# are trusted only when their two paragraphs also agree semantically.  Structural
# anchors (paragraph/chapter/section/note/folio ordinals) are edition-independent
# and never gated this way.
_CONTEXT_GATED_ANCHOR_PREFIXES = ("term:", "number:", "name:")


# Bare integers below this value are treated as recurring counters (note numbers,
# list items) that reset per chapter, so they never seed a number anchor.  Rare
# years and contextual numbers are captured by dedicated patterns and are exempt.
_MIN_NUMBER_ANCHOR_VALUE = 100


# A soft anchor whose corridor against the previous kept anchor compresses one
# side more than this ratio is a runaway jump (e.g. a note number reused across
# chapters), not a structural landmark.
_MAX_ANCHOR_CORRIDOR_RATIO = 50


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
            if float(match.group(0)) < _MIN_NUMBER_ANCHOR_VALUE:
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
