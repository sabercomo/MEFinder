"""标题行、目录与注释块的结构识别。

锚点抽取（``alignment_anchors``）与正文区域推断（``semantic_alignment``）都要先
判定“哪些段不是正文”，这一层就是两者共用的底层：中文/拉丁标题行解析、目录页
识别、编号注释块归并。本模块不依赖包内其他模块，是对齐域的最底层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


_MAX_HEADING_NUMBER = 30


_MIN_COLLECTED_NOTE_RUN = 5


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


_TOC_LEADER = re.compile(
    r"(?:\.{3,}|…{2,}|·{3,})\s*[\[(（]?\d*[\])）]?\s*$"
)


_CONTENTS_HEADING = re.compile(r"^(?:contents|table of contents|目\s*[录錄次])$", re.IGNORECASE)


_TOC_SLASH_ENTRY = re.compile(r"^\s*\d{1,3}\s*/\s*(.+?)\s*$")


_TOC_ENGLISH_PART = re.compile(
    r"^part\s+(one|two|three|four|[ivx]{1,4}|\d{1,2})\s+(.+)$",
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


@dataclass(frozen=True)
class _NumberedNoteBlock:
    chapter_number: int
    note_number: int
    start: int
    content_start: int
    end: int


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
