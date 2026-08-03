"""Parse journal citations copied from CNKI without making network requests."""

from __future__ import annotations

import re
from typing import Dict

from .bibliographic_metadata import normalize_doi, normalize_issn


MAX_CNKI_CITATION_CHARS = 8_000

_JOURNAL_MARKER_RE = re.compile(
    r"[\[［]\s*[JＪ](?:\s*/\s*[OＯ][LＬ])?\s*[\]］]",
    re.IGNORECASE,
)
_REFERENCE_NUMBER_RE = re.compile(r"(?:^|\s)[\[［]\s*\d+\s*[\]］]\s*")
_HEADER_LINE_RE = re.compile(
    r"^(?:GB\s*/\s*T\s*7714(?:-\d+)?|参考文献|引用格式|引文格式|复制引用|"
    r"APA|MLA|Chicago)\s*[:：]?$",
    re.IGNORECASE,
)
_PUBLICATION_RE = re.compile(
    r"^(?P<journal>.+?)[,，]\s*(?P<year>(?:18|19|20)\d{2})\s*年?(?P<rest>.*)$",
    re.DOTALL,
)
_AUTHOR_TITLE_RE = re.compile(
    r"^(?P<author>.+?)[.．。]\s*(?P<title>.+)$",
    re.DOTALL,
)
_VOLUME_ISSUE_RE = re.compile(
    r"^(?P<volume>\d+)\s*(?:卷)?\s*[\(（]\s*(?P<issue>[^()（）]+?)\s*[\)）]"
)
_ISSUE_ONLY_RE = re.compile(r"^[\(（]\s*(?P<issue>[^()（）]+?)\s*[\)）]")
_CHINESE_VOLUME_ISSUE_RE = re.compile(
    r"^(?:第\s*)?(?P<volume>\d+)\s*卷(?:\s*第\s*(?P<issue>[^,，:：]+?)\s*期)?"
)
_CHINESE_ISSUE_RE = re.compile(r"^(?:第\s*)?(?P<issue>[^,，:：]+?)\s*期")
_COMMA_VOLUME_ISSUE_RE = re.compile(
    r"^(?P<volume>\d+)\s*[,，]\s*(?P<issue>[^,，:：]+)"
)
_PAGE_RE = re.compile(
    r"(?:[:：]|(?<=[)）]))\s*(?P<pages>(?:[A-Za-z]?\d+|[ivxlcdm]+)"
    r"(?:(?:\s*[-–—+－＋]\s*|\s*[,，]\s*)(?:[A-Za-z]?\d+|[ivxlcdm]+))*)",
    re.IGNORECASE,
)
_DOI_OR_URL_RE = re.compile(
    r"(?:[.。;；]\s*)?(?:DOI\s*[:：]|https?://(?:dx\.)?doi\.org/)",
    re.IGNORECASE,
)


def parse_cnki_journal_citation(value: object) -> Dict[str, str]:
    """Return fields from one CNKI GB/T-style journal citation.

    The parser is deliberately conservative: a journal marker, an author/title
    separator, a journal name, and a four-digit year must all be present.  It
    never tries to infer missing values from the title or an external service.
    """

    if not isinstance(value, str):
        raise ValueError("请粘贴知网“引用”窗口中的期刊引文文字。")
    if len(value) > MAX_CNKI_CITATION_CHARS:
        raise ValueError("知网引用文字过长，请只粘贴一条期刊引文。")

    text = value
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\xa0", " ")
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"[\t ]+", " ", raw_line).strip()
        if line and not _HEADER_LINE_RE.fullmatch(line):
            lines.append(line)
    text = " ".join(lines).strip()
    if not text:
        raise ValueError("请先粘贴知网期刊引文。")

    marker = _JOURNAL_MARKER_RE.search(text)
    if marker is None:
        raise ValueError("没有识别到期刊类型标记 [J]，请粘贴知网的期刊引用文字。")

    author_title_text = text[: marker.start()].strip()
    reference_numbers = list(_REFERENCE_NUMBER_RE.finditer(author_title_text))
    if reference_numbers:
        author_title_text = author_title_text[reference_numbers[-1].end() :].strip()
    author_title_text = re.sub(
        r"^(?:参考文献|引用格式|引文格式|复制引用)\s*[:：]?\s*",
        "",
        author_title_text,
        flags=re.IGNORECASE,
    )
    author_title_text = author_title_text.strip(" .．。;；")
    author_title = _AUTHOR_TITLE_RE.match(author_title_text)
    if author_title is None:
        raise ValueError("无法从引用文字中分开作者和篇名，请检查是否复制完整。")

    author = author_title.group("author").strip(" ,，.．。;；")
    title = author_title.group("title").strip(" ,，.．。;；")
    if not author or not title:
        raise ValueError("引用文字缺少作者或篇名，请检查是否复制完整。")

    publication_text = text[marker.end() :].lstrip(" .．。;；")
    publication = _PUBLICATION_RE.match(publication_text)
    if publication is None:
        raise ValueError("没有识别到“刊名,年份”结构，请粘贴完整的知网期刊引文。")

    journal_name = publication.group("journal").strip(" ,，.．。;；")
    if not journal_name or len(journal_name) > 200:
        raise ValueError("没有识别到可靠的刊名，请检查引用文字。")

    metadata: Dict[str, str] = {
        "document_type": "journal_article",
        "author": author,
        "title": title,
        "journal_name": journal_name,
        "publish_year": publication.group("year"),
    }

    rest = _DOI_OR_URL_RE.split(publication.group("rest"), maxsplit=1)[0]
    page_match = _PAGE_RE.search(rest)
    volume_issue_text = rest[: page_match.start()] if page_match else rest
    volume_issue_text = volume_issue_text.strip(" ,，.．。;；")

    volume_issue = _VOLUME_ISSUE_RE.match(volume_issue_text)
    if volume_issue:
        metadata["volume"] = volume_issue.group("volume").strip()
        metadata["issue"] = volume_issue.group("issue").strip()
    else:
        issue_only = _ISSUE_ONLY_RE.match(volume_issue_text)
        chinese_volume_issue = _CHINESE_VOLUME_ISSUE_RE.match(volume_issue_text)
        comma_volume_issue = _COMMA_VOLUME_ISSUE_RE.match(volume_issue_text)
        chinese_issue = _CHINESE_ISSUE_RE.match(volume_issue_text)
        if issue_only:
            metadata["issue"] = issue_only.group("issue").strip()
        elif chinese_volume_issue:
            metadata["volume"] = chinese_volume_issue.group("volume").strip()
            if chinese_volume_issue.group("issue"):
                metadata["issue"] = chinese_volume_issue.group("issue").strip()
        elif comma_volume_issue:
            metadata["volume"] = comma_volume_issue.group("volume").strip()
            metadata["issue"] = comma_volume_issue.group("issue").strip()
        elif chinese_issue:
            metadata["issue"] = chinese_issue.group("issue").strip()

    if page_match:
        page_range = re.sub(
            r"\s*([-–—+－＋,，])\s*",
            r"\1",
            page_match.group("pages").strip(),
        )
        if page_range:
            metadata["page_range"] = page_range

    doi = normalize_doi(publication.group("rest"))
    if doi:
        metadata["doi"] = doi
    issn = normalize_issn(publication.group("rest"))
    if issn:
        metadata["issn"] = issn

    return metadata
