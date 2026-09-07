"""学位论文封面的题录抽取。

按“字段标签 + 紧随值”的封面版式识别题名、作者、培养单位与日期，并处理
题名与作者被排版粘连的情况。取值校验与人名清洗来自 ``bibliographic_values``。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .bibliographic_values import (
    _clean_people,
    _is_plausible_person_name,
    is_valid_bibliographic_value,
)


# 学位论文封面用语：识别为独立文献类型。
_THESIS_MARKERS = ("硕士学位论文", "博士学位论文", "专业学位论文", "学位论文")


_THESIS_FIELD_LABEL_RE = re.compile(
    r"^(?:"
    r"(?:中文)?(?:学位)?论文(?:题目|题名)(?:名称)?|题目|题名|"
    r"作者(?:姓名)?|研究生(?:姓名)?|"
    r"学位授予单位|培养单位|授予单位|学校|院校|"
    r"答辩日期|论文日期|提交日期|完成日期|学位授予日期|"
    r"学号|指导教师|导师|学科(?:专业)?|专业(?:名称)?|学院|院系|"
    r"英文(?:题目|题名)|分类号|密级|UDC"
    r")\s*[:：]?"
)


_THESIS_TITLE_LABEL_RE = re.compile(
    r"^(?:(?:中文)?(?:学位)?论文(?:题目|题名)(?:名称)?|题目|题名)\s*[:：]?\s*(?P<value>.*)$"
)


_THESIS_AUTHOR_LABEL_RE = re.compile(
    r"^(?:作者(?:姓名)?|研究生(?:姓名)?)\s*[:：]?\s*(?P<value>.*)$"
)


_THESIS_SCHOOL_LABEL_RE = re.compile(
    r"^(?:学位授予单位|培养单位|授予单位|学校|院校)\s*[:：]?\s*(?P<value>.*)$"
)


_THESIS_DATE_LABEL_RE = re.compile(
    r"^(?:答辩日期|论文日期|提交日期|完成日期|学位授予日期)\s*[:：]?\s*(?P<value>.*)$"
)


_THESIS_INSTITUTION_RE = re.compile(
    r"^[\u3400-\u9fff·]{2,30}(?:大学|学院|研究院|党校)$"
)


def _looks_like_thesis(texts: Sequence[Tuple[int, str]]) -> bool:
    """Report whether the front pages carry degree-thesis cover markers."""

    for page_idx, text in texts:
        if page_idx >= 2:
            continue
        compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
        if any(marker in compact for marker in _THESIS_MARKERS):
            return True
    return False


def _compact_thesis_cover_text(value: object) -> str:
    """Normalize spacing introduced by PDF glyph positioning on thesis covers."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip(" \t:：")


def _following_thesis_cover_value(
    lines: Sequence[Tuple[int, str]],
    index: int,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Read a cover field whose label and value were extracted on separate lines."""

    for next_index in range(index + 1, min(len(lines), index + 4)):
        page_idx, raw_line = lines[next_index]
        value = _compact_thesis_cover_text(raw_line)
        if not value:
            continue
        if _THESIS_FIELD_LABEL_RE.match(value):
            break
        if any(marker in value for marker in _THESIS_MARKERS):
            continue
        return value, page_idx, raw_line
    return None, None, None


# 学位论文文件名分隔符：半角/全角连字符、破折号、下划线、间隔号、冒号。
_THESIS_TITLE_AUTHOR_PREFIX_SEP = re.compile(r"^[\s\-‐-―－_·・:：]+")


def _strip_thesis_author_prefix(
    result: Dict[str, object],
    evidence: Dict[str, object],
) -> None:
    """Drop a leading ``作者 - `` prefix from a thesis title.

    Descriptive filenames often read ``作者 - 篇名``.  Because the author is
    already stored separately, the title must not repeat it.  Only strip when
    the title starts with the exact author name *and* a real separator follows,
    so genuine titles that merely begin with the author's characters survive.
    """

    author = str(result.get("author") or "").strip()
    title = str(result.get("title") or "").strip()
    if not author or not title or not title.startswith(author):
        return
    remainder = title[len(author):]
    separator = _THESIS_TITLE_AUTHOR_PREFIX_SEP.match(remainder)
    if not separator or separator.end() == 0:
        return
    stripped = remainder[separator.end():].strip()
    if not is_valid_bibliographic_value(stripped):
        return
    result["title"] = stripped
    field_evidence = dict(evidence.get("title") or {}) if isinstance(evidence.get("title"), Mapping) else {}
    field_evidence["author_prefix_stripped"] = title
    evidence["title"] = field_evidence


def _extract_thesis_metadata(
    texts: Sequence[Tuple[int, str]],
) -> Tuple[Dict[str, str], Dict[str, object], Dict[str, float]]:
    """Extract thesis title, author, degree-granting school and defense year.

    Thesis covers do not contain book publication statements or journal
    mastheads.  This precision-first path therefore reads only the first two
    pages and only accepts cover labels, a standalone degree-granting
    institution, or a cover date.  The school is stored in ``publisher`` for
    compatibility with the existing bibliographic schema.
    """

    lines: List[Tuple[int, str]] = []
    for page_idx, text in texts:
        if page_idx >= 2:
            continue
        lines.extend(
            (page_idx, line.strip())
            for line in unicodedata.normalize("NFKC", text).splitlines()
            if line.strip()
        )

    candidates: Dict[str, List[Tuple[float, int, int, str, str, str]]] = {
        field: [] for field in ("title", "author", "publisher", "publish_year")
    }

    def record(
        field: str,
        value: object,
        page_idx: int,
        line_index: int,
        evidence_text: str,
        rule: str,
        score: float,
    ) -> None:
        cleaned = _compact_thesis_cover_text(value)
        if field == "author":
            cleaned = _clean_people(cleaned)
        elif field == "publisher":
            cleaned = re.sub(r"\s+", "", cleaned)
        elif field == "title":
            cleaned = cleaned.strip("《》")
        elif field == "publish_year":
            year = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", cleaned)
            cleaned = year.group(1) if year else ""
        if not is_valid_bibliographic_value(cleaned):
            return
        if field == "author" and not _is_plausible_person_name(cleaned):
            return
        candidates[field].append((score, page_idx, line_index, cleaned, evidence_text, rule))

    marker_line_indexes: List[int] = []
    for line_index, (page_idx, raw_line) in enumerate(lines):
        line = _compact_thesis_cover_text(raw_line)
        if any(marker in line for marker in _THESIS_MARKERS):
            marker_line_indexes.append(line_index)

        for field, pattern, rule in (
            ("title", _THESIS_TITLE_LABEL_RE, "thesis_title_label"),
            ("author", _THESIS_AUTHOR_LABEL_RE, "thesis_author_label"),
            ("publisher", _THESIS_SCHOOL_LABEL_RE, "thesis_school_label"),
            ("publish_year", _THESIS_DATE_LABEL_RE, "thesis_defense_date"),
        ):
            match = pattern.match(line)
            if not match:
                continue
            value = match.group("value").strip()
            value_page_idx, value_evidence = page_idx, raw_line
            if not value:
                value, following_page_idx, following_line = _following_thesis_cover_value(
                    lines, line_index
                )
                if value and following_page_idx is not None and following_line is not None:
                    value_page_idx = following_page_idx
                    value_evidence = f"{raw_line} / {following_line}"
            if value:
                record(field, value, value_page_idx, line_index, value_evidence, rule, 0.99)

        institution = re.sub(r"\s+", "", line)
        if (
            _THESIS_INSTITUTION_RE.fullmatch(institution)
            and "出版社" not in institution
            and not _THESIS_FIELD_LABEL_RE.match(line)
        ):
            score = 0.99 if institution.endswith("大学") else 0.92
            record(
                "publisher",
                institution,
                page_idx,
                line_index,
                raw_line,
                "thesis_cover_institution",
                score,
            )

        if re.fullmatch(r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?", line):
            record(
                "publish_year",
                line,
                page_idx,
                line_index,
                raw_line,
                "thesis_cover_date",
                0.93,
            )

    # Some covers put an unlabeled Chinese title directly below the thesis
    # marker.  Accept only the first plausible line before the next field label.
    if not candidates["title"]:
        for marker_index in marker_line_indexes:
            for line_index in range(marker_index + 1, min(len(lines), marker_index + 7)):
                page_idx, raw_line = lines[line_index]
                line = _compact_thesis_cover_text(raw_line)
                if _THESIS_FIELD_LABEL_RE.match(line):
                    break
                if (
                    len(line) >= 5
                    and re.search(r"[\u3400-\u9fff]", line)
                    and not _THESIS_INSTITUTION_RE.fullmatch(re.sub(r"\s+", "", line))
                    and not any(marker in line for marker in _THESIS_MARKERS)
                    and not re.search(r"(?:申请|学位|专业|学科|导师|指导教师)", line)
                    and not re.fullmatch(r"(?:19|20)\d{2}.*", line)
                ):
                    record(
                        "title",
                        line,
                        page_idx,
                        line_index,
                        raw_line,
                        "thesis_title_after_marker",
                        0.93,
                    )
                    break
            if candidates["title"]:
                break

    values: Dict[str, str] = {}
    evidence: Dict[str, object] = {}
    confidence: Dict[str, float] = {}
    for field, items in candidates.items():
        if not items:
            continue
        score, page_idx, _line_index, value, evidence_text, rule = sorted(
            items, key=lambda item: (-item[0], item[1], item[2])
        )[0]
        values[field] = value
        evidence[field] = {
            "source": "thesis_cover_text",
            "source_page": page_idx + 1,
            "evidence_text": evidence_text,
            "rule": rule,
            "confidence": score,
        }
        confidence[field] = score
    return values, evidence, confidence
