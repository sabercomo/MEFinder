"""期刊论文的判别与题录抽取。

先用正负向标记把期刊论文与图书、学位论文区分开，再抽取刊名、卷期、页码、
DOI 与 ISSN 并归一。取值校验与人名清洗来自 ``bibliographic_values``。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

from .bibliographic_values import (
    _clean_person,
    _is_plausible_person_name,
    is_valid_bibliographic_value,
)


# GB/T 中文期刊首页的固定标记。专著的内容提要同样会出现「摘要」「关键词」，
# 版权页也可能印中图分类号，因此书刊判定必须先看专著独有的版权信息。
_JOURNAL_MARKERS = ("中图分类号", "文献标识码", "文献标志码")


_JOURNAL_WEAK_MARKERS = ("摘要", "关键词", "DOI:", "doi:")


# 版权页/CIP 专有标记，只属于正式出版的专著。含美国国会图书馆 CIP 短语，
# 用于识别没有中文版权页、也没印 ISBN 的外文专著。
# 强图书标记：版权页/CIP 专有，出现在任意页都足以判定为图书。
_BOOK_STRONG_MARKERS = ("图书在版编目", "出版发行", "版次", "定价", "Cataloging-in-Publication")


# 版权页/CIP 语境线索（去空格、casefold 后匹配）。裸 ISBN 只有与这些线索同页时才算
# 图书信号——外文/中文论文的参考文献会引用他书的 ISBN 甚至“出版社”，但绝不会出现
# ©、版权、Identifiers:、Library of Congress 这类版权页专有字样，据此把两者分开。
_BOOK_ISBN_CONTEXT = (
    "版权", "©", "allrightsreserved", "firstpublished",
    "libraryofcongress", "cataloging", "identifiers:", "description:",
)


# 用于期刊 GB/T 判定的排除标记：只在首页文本上判断，故含 ISBN 是安全的。
_BOOK_ONLY_MARKERS = _BOOK_STRONG_MARKERS + ("ISBN",)


# 无版权页/CIP 标记且总页数不超过该阈值的 PDF 视为单篇论文而非专著。学位论文
# 另由封面标记识别、与页数无关；阈值取 60 以容纳较长的外文期刊论文，真实专著
# 通常在 200 页以上，不会被误判。
_JOURNAL_MAX_FALLBACK_PAGES = 60


# 从中文期刊首页版式里认出刊名用的后缀与佐证词。多数近年期刊会把刊名印在首页
# 报头（常与「YYYY 年第 N 期」或英文刊名相邻）。抽印本若没印刊名则保持缺失。
_JOURNAL_NAME_SUFFIXES = (
    "学报", "学刊", "论丛", "季刊", "月刊", "与现实", "战线", "评论", "论坛",
    "研究", "科学", "世界", "文摘", "杂志", "通讯", "动态", "译丛", "丛刊", "前沿", "探索",
)


# 独立成行、缺其它佐证时只接受较强后缀且长度更短，避免把文章标题误当刊名。
_JOURNAL_NAME_SUFFIXES_STANDALONE = (
    "学报", "学刊", "论丛", "季刊", "月刊", "与现实", "战线", "研究", "科学", "世界", "文摘",
)


# 与中文刊名相邻的英文刊名常含这些词，用作强佐证以排除英文文章标题。
_JOURNAL_EN_HINTS = (
    "journal", "university", "review", "studies", "science", "sciences",
    "academic", "bulletin", "acta", "annals", "quarterly", "tribune",
)


_JOURNAL_NAME_CHARS = r"[㐀-鿿()·—\-]"


# 例：文章编号 0439-8041(2020)09-0015-13
#     ISSN 0439-8041、2020 年、第 09 期、起始页 15、共 13 页 → 15-27。
_ARTICLE_NUMBER_RE = re.compile(
    r"文章编\s*号\s*[:：]?\s*"
    r"(?P<issn>[0-9]{4}\s*-\s*[0-9]{3}[0-9Xx])"
    r"\s*\(\s*(?P<year>\d{4})\s*\)\s*"
    r"(?P<issue>\d{1,3})\s*-\s*"
    r"(?P<start>\d{1,5})\s*-\s*"
    r"(?P<length>\d{1,4})"
)


_DOI_RE = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|\bDOI\s*[:：]?\s*)"
    r"(?P<doi>10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.IGNORECASE,
)


_ISSN_RE = re.compile(r"\b(?P<issn>\d{4}(?:\s*-\s*|\s+)\d{3}[\dXx])\b")


# 期刊版式常见的卷期行：「第 52 卷第 9 期」「2020 年第 9 期」。
_VOLUME_ISSUE_RE = re.compile(r"第\s*(?P<volume>\d{1,3})\s*卷\s*第\s*(?P<issue>\d{1,3})\s*期")


_YEAR_ISSUE_RE = re.compile(r"(?P<year>\d{4})\s*年\s*第\s*(?P<issue>\d{1,3})\s*期")


# 「作者张双利，复旦大学哲学学院教授（上海 200433）。」
_AUTHOR_STATEMENT_RE = re.compile(r"^作\s*者\s*[:：]?\s*(?P<author>[^，,。（(]{2,20})")


def looks_like_journal_article(text: str) -> bool:
    """Report whether a page carries the GB/T markers of a journal article."""

    normalized = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"\s+", "", normalized)
    # 文章编号是期刊独有的 GB/T 编码，可直接判定。
    if _ARTICLE_NUMBER_RE.search(compact):
        return True
    # 版权页信息只属于专著；出现即排除期刊，避免内容提要里的“摘要/关键词”误导。
    if any(marker.casefold() in compact.casefold() for marker in _BOOK_ONLY_MARKERS):
        return False
    if any(marker in compact for marker in _JOURNAL_MARKERS):
        return True
    return sum(1 for marker in _JOURNAL_WEAK_MARKERS if marker in compact) >= 2


def normalize_doi(value: object) -> Optional[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    match = _DOI_RE.search(text)
    if match is None:
        match = re.search(r"(?P<doi>10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE)
    if match is None:
        return None
    doi = match.group("doi").rstrip(".,;:。；，）)]}").strip()
    return doi.casefold() if doi else None


def normalize_issn(value: object) -> Optional[str]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    match = _ISSN_RE.search(text)
    if match is None:
        return None
    compact = re.sub(r"\s|-", "", match.group("issn")).upper()
    if len(compact) != 8:
        return None
    total = sum(int(char) * weight for char, weight in zip(compact[:7], range(8, 1, -1)))
    check = 10 if compact[7] == "X" else int(compact[7])
    if (total + check) % 11 != 0:
        return None
    return compact[:4] + "-" + compact[4:]


def _has_book_only_markers(texts: Sequence[Tuple[int, str]]) -> bool:
    """Report whether any scanned page shows a book copyright/CIP marker.

    Strong CIP markers count on any page; a bare ISBN counts only on the front
    pages so a cited book's ISBN in a foreign article's reference list does not
    misclassify the article as a monograph.
    """

    for _page_idx, text in texts:
        folded = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()
        if any(marker.casefold() in folded for marker in _BOOK_STRONG_MARKERS):
            return True
        if "isbn" in folded and any(cue in folded for cue in _BOOK_ISBN_CONTEXT):
            return True
    return False


def _looks_like_english_journal(line: str) -> bool:
    """Report whether a line reads like an English journal title, not prose."""

    stripped = line.strip().lower()
    letters = sum(1 for ch in stripped if ch.isascii() and ch.isalpha())
    if len(stripped) < 6 or letters < len(stripped.replace(" ", "")) * 0.6:
        return False
    return any(word in stripped for word in _JOURNAL_EN_HINTS)


def _extract_journal_name(
    texts: Sequence[Tuple[int, str]],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Read the journal title from the first page's masthead when it is printed.

    Precision-first: only returns a name when the layout gives a strong signal
    (a ``刊名 + 年 + 期`` line, a Chinese name next to an English journal title,
    or a short standalone name line). Offprints that never print the journal
    name return ``None`` rather than a guess -- the field stays missing.
    """

    first = next((text for idx, text in texts if idx == 0), None)
    if not first:
        return None, None, None
    lines = [
        re.sub(r"\s+", " ", unicodedata.normalize("NFKC", line)).strip()
        for line in first.splitlines()
        if line.strip()
    ]
    head = lines[:15]
    # 规则1：同一行「刊名 YYYY 年第 N 期」。
    for line in head:
        match = re.match(
            r"^(" + _JOURNAL_NAME_CHARS + r"{2,20}?)\s*(?:19|20)\d{2}\s*年", line
        )
        if match and any(sfx in match.group(1) for sfx in _JOURNAL_NAME_SUFFIXES):
            return re.sub(r"\s+", "", match.group(1)), line, "masthead_name_year"
    # 规则2：中文刊名行紧跟英文刊名行。
    for i in range(len(head) - 1):
        compact = re.sub(r"\s+", "", head[i])
        if (
            2 <= len(compact) <= 20
            and re.fullmatch(_JOURNAL_NAME_CHARS + r"+", compact)
            and any(sfx in compact for sfx in _JOURNAL_NAME_SUFFIXES)
            and _looks_like_english_journal(head[i + 1])
        ):
            return compact, f"{head[i]} / {head[i + 1]}", "masthead_cn_en"
    # 规则3：首几行里独立成行的短刊名。
    for line in head[:5]:
        compact = re.sub(r"\s+", "", line)
        if (
            2 <= len(compact) <= 8
            and re.fullmatch(_JOURNAL_NAME_CHARS + r"+", compact)
            and any(sfx in compact for sfx in _JOURNAL_NAME_SUFFIXES_STANDALONE)
        ):
            return compact, line, "masthead_suffix_line"
    return None, None, None


def _extract_journal_article(
    texts: Sequence[Tuple[int, str]],
    file_stem: str,
) -> Tuple[Dict[str, str], Dict[str, object], Dict[str, float]]:
    """Read the fields a Chinese journal offprint actually encodes.

    ``文章编号`` is a registered GB/T code rather than a guess: it carries the
    year, issue and the article's own page span.  The journal name and volume
    are deliberately *not* inferred -- CNKI offprints usually print neither,
    and inventing them would be exactly the kind of fabricated citation this
    project refuses to produce.  They stay missing so the user is prompted.
    """

    values: Dict[str, str] = {}
    evidence: Dict[str, object] = {}
    confidence: Dict[str, float] = {}

    def record(
        field: str,
        value: str,
        page_idx: Optional[int],
        source: str,
        score: float,
        text: str,
        rule: str,
    ) -> None:
        if field in values or not is_valid_bibliographic_value(value):
            return
        values[field] = value
        evidence[field] = {
            "source": source,
            "source_page": page_idx + 1 if page_idx is not None else None,
            "evidence_text": text,
            "rule": rule,
        }
        confidence[field] = score

    first_page = next((item for item in texts if item[0] == 0), None)

    for page_idx, raw_text in texts:
        normalized = unicodedata.normalize("NFKC", raw_text)
        compact = re.sub(r"[ \t]+", "", normalized)

        match = _ARTICLE_NUMBER_RE.search(compact)
        if match:
            issn = normalize_issn(match.group("issn"))
            year = match.group("year")
            issue = str(int(match.group("issue")))
            start = int(match.group("start"))
            length = int(match.group("length"))
            if issn:
                record("issn", issn, page_idx, "article_number", 0.99, match.group(0), "article_number_issn")
            record("publish_year", year, page_idx, "article_number", 0.97, match.group(0), "article_number_year")
            record("issue", issue, page_idx, "article_number", 0.97, match.group(0), "article_number_issue")
            if start > 0 and length > 0:
                record(
                    "page_range",
                    f"{start}-{start + length - 1}",
                    page_idx,
                    "article_number",
                    0.95,
                    match.group(0),
                    "article_number_page_span",
                )

        doi_match = _DOI_RE.search(normalized)
        doi = normalize_doi(doi_match.group(0)) if doi_match else None
        if doi:
            record("doi", doi, page_idx, "journal_front_page", 0.98, doi_match.group(0), "explicit_doi")

        issn_match = _ISSN_RE.search(normalized)
        issn = normalize_issn(issn_match.group(0)) if issn_match else None
        if issn:
            record("issn", issn, page_idx, "journal_front_page", 0.96, issn_match.group(0), "explicit_issn")

        volume_match = _VOLUME_ISSUE_RE.search(compact)
        if volume_match:
            record("volume", str(int(volume_match.group("volume"))), page_idx, "masthead", 0.9, volume_match.group(0), "masthead_volume")
            record("issue", str(int(volume_match.group("issue"))), page_idx, "masthead", 0.9, volume_match.group(0), "masthead_issue")
        year_match = _YEAR_ISSUE_RE.search(compact)
        if year_match:
            record("publish_year", year_match.group("year"), page_idx, "masthead", 0.88, year_match.group(0), "masthead_year")
            record("issue", str(int(year_match.group("issue"))), page_idx, "masthead", 0.88, year_match.group(0), "masthead_issue")

    # 篇名与作者：期刊首页顶部依次是篇名、作者，随后才是摘要。
    if first_page is not None:
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in unicodedata.normalize("NFKC", first_page[1]).splitlines()
            if line.strip()
        ]
        for line in lines[:12]:
            statement = _AUTHOR_STATEMENT_RE.match(line)
            if statement:
                record("author", _clean_person(statement.group("author")), 0, "author_statement", 0.93, line, "journal_author_statement")
                break
        heading: List[str] = []
        for line in lines:
            if re.match(r"^(摘\s*要|关\s*键\s*词|中图分类号|文献标[识志]码|文章编\s*号|作\s*者)", line):
                break
            heading.append(line)
        if heading:
            record("title", heading[0], 0, "journal_title_line", 0.9, heading[0], "journal_title_line")
            if len(heading) > 1 and _is_plausible_person_name(heading[1]):
                record("author", _clean_person(heading[1]), 0, "journal_author_line", 0.9, heading[1], "journal_author_line")

    # CNKI 导出件的文件名是「篇名_作者」，可为版面抽取提供独立佐证。
    stem_match = re.match(r"^(?P<title>.+?)_(?P<author>[^_]{2,20})$", file_stem.strip())
    if stem_match:
        record("title", stem_match.group("title").strip(), None, "file_name", 0.75, file_stem, "cnki_underscore_filename")
        candidate = stem_match.group("author").strip()
        if _is_plausible_person_name(candidate):
            record("author", _clean_person(candidate), None, "file_name", 0.75, file_stem, "cnki_underscore_filename")

    return values, evidence, confidence
