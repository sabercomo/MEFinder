"""Citation formatting helpers for search hits.

The formatter intentionally uses only metadata that is already present in the
index. GB/T output fails safely when required fields are missing; citation
pages are never substituted with PDF physical indexes.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional


CitationMetadata = Mapping[str, object]


def format_citation(document_metadata: CitationMetadata, hit_page: object, citation_style: str) -> str:
    """Format a citation for the current hit page.

    ``hit_page`` may be a string such as ``"53"`` or a mapping with
    ``start``/``end``/``display``/``uncalibrated`` keys. The page is the page
    where the search result actually matched, not a source article page range.
    """

    style = "gb" if str(citation_style).lower() in {"gb", "gbt", "gb/t", "gbt7714", "gb/t 7714"} else "chinese"
    page = _page_info(hit_page)
    if style == "gb":
        return _format_gb(document_metadata, page)
    return _format_chinese(document_metadata, page)


def build_citation_formats(document_metadata: CitationMetadata, hit_page: object) -> Dict[str, object]:
    page = _page_info(hit_page)
    chinese_missing = _missing_fields(document_metadata, page, "chinese")
    gb_missing = _missing_fields(document_metadata, page, "gb")
    return {
        "chinese": format_citation(document_metadata, hit_page, "chinese"),
        "gb": format_citation(document_metadata, hit_page, "gb"),
        "chinese_status": "complete" if not chinese_missing else "metadata_incomplete",
        "gb_status": "complete" if not gb_missing else "metadata_incomplete",
        "chinese_missing_fields": chinese_missing,
        "gb_missing_fields": gb_missing,
    }


def _format_chinese(meta: CitationMetadata, page: Dict[str, object]) -> str:
    if page.get("uncalibrated"):
        return "该文献页码尚未校准，不能生成可靠脚注。"
    doc_type = _document_type(meta)
    if doc_type == "marx_engels_collection":
        return _finish_chinese(_join_nonempty([
            _marx_engels_volume_title_chinese(meta),
            _publication_chinese(meta),
            page["chinese"],
        ]))
    if doc_type == "journal_article":
        return _finish_chinese(_join_nonempty([
            _author_prefix(meta),
            _quoted_title(_title(meta)),
            _journal_chinese(meta),
            page["chinese"],
        ]))
    if doc_type in {"book_chapter", "collection_article"}:
        return _finish_chinese(_join_nonempty([
            _author_prefix(meta),
            _quoted_title(_title(meta)),
            _container_chinese(meta),
            _publisher_year_chinese(meta),
            page["chinese"],
        ]))
    if doc_type == "translated_book":
        return _finish_chinese(_join_nonempty([
            _author_prefix(meta, include_country=True),
            _quoted_title(_book_title(meta)),
            _translator_chinese(meta),
            _publisher_year_chinese(meta),
            page["chinese"],
        ]))
    return _finish_chinese(_join_nonempty([
        _author_prefix(meta, include_country=True),
        _quoted_title(_book_title(meta)),
        _publisher_year_chinese(meta),
        page["chinese"],
    ]))


def _format_gb(meta: CitationMetadata, page: Dict[str, object]) -> str:
    if page.get("uncalibrated"):
        return "该文献页码尚未校准，不能生成 GB/T 引文。"
    doc_type = _document_type(meta)
    if doc_type == "marx_engels_collection":
        return _finish_gb(_marx_engels_volume_gb(meta, page))
    if doc_type == "journal_article":
        title = _title(meta)
        base = _join_gb([_author_plain(meta), f"{title}[J]" if title else ""])
        journal = _first(meta, "journal_name", "journal_title", "journal", "periodical")
        year = _year(meta)
        volume = _first(meta, "volume", "journal_volume")
        issue = _first(meta, "issue", "issue_number", "journal_issue")
        journal_part = _journal_gb(journal, year, volume, issue)
        # GB/T 期刊条目引用文章的起止页码（如 15-27），而非命中页。
        page_range = _clean(_first(meta, "page_range", "pages", "article_pages"))
        if page_range:
            return _finish_gb(_join_gb([base, f"{journal_part}: {page_range}" if journal_part else page_range]))
        return _finish_gb(_join_gb_with_page([base, journal_part], page, separator=": "))
    if doc_type in {"book_chapter", "collection_article"}:
        title = _title(meta)
        base = _join_gb([_author_plain(meta), f"{title}[M]" if title else ""])
        container = _container_plain(meta)
        editor = _first(meta, "editor", "editors", "chief_editor")
        if editor and container:
            container = f"{editor}. {container}"
        if container:
            base = base + "//" + container
        pub = _publisher_year_gb(meta)
        return _finish_gb(_join_gb_with_page([base, pub], page, separator=":"))
    missing = _missing_fields(meta, page, "gb")
    if missing:
        if "citation_page" in missing:
            return "该文献页码尚未校准，不能生成 GB/T 引文。"
        return f"无法生成完整 GB/T 引文：缺少{' / '.join(_field_label(field) for field in missing)}。"
    title = _book_title(meta)
    base = _join_gb([_author_plain(meta, include_country=True), f"{title}[M]" if title else ""])
    translator = _translator_gb(meta) if doc_type == "translated_book" else ""
    pub = _publisher_year_gb(meta)
    return _finish_gb(_join_gb_with_page([base, translator, pub], page))


def _page_info(hit_page: object) -> Dict[str, object]:
    if isinstance(hit_page, Mapping):
        start = _clean(hit_page.get("start") or hit_page.get("page") or hit_page.get("citation_page_start"))
        end = _clean(hit_page.get("end") or hit_page.get("citation_page_end"))
        display = _clean(hit_page.get("display"))
        uncalibrated = bool(hit_page.get("uncalibrated"))
    else:
        start = _clean(hit_page)
        end = ""
        display = ""
        uncalibrated = False

    if uncalibrated:
        warning = display or "页码未验证"
        return {
            "raw": warning,
            "chinese": warning,
            "gb": warning,
            "uncalibrated": True,
        }
    if start:
        label = _page_range(start, end)
        chinese_label = _chinese_page_label(start, end)
        gb_label = _page_range(start, end, separator="-")
        return {"raw": label, "chinese": chinese_label, "gb": gb_label, "uncalibrated": False}
    if display:
        return {"raw": display, "chinese": _display_page_chinese(display), "gb": display, "uncalibrated": False}
    return {"raw": "", "chinese": "页码未验证", "gb": "页码未验证", "uncalibrated": True}


def _document_type(meta: CitationMetadata) -> str:
    raw = _first(meta, "document_type", "citation_type", "publication_type", "type").lower()
    aliases = {
        "journal": "journal_article",
        "article": "journal_article",
        "journal-article": "journal_article",
        "book": "book",
        "monograph": "book",
        "translated": "translated_book",
        "translation": "translated_book",
        "translated_book": "translated_book",
        "book_chapter": "book_chapter",
        "chapter": "book_chapter",
        "collection_article": "collection_article",
        "article_in_book": "collection_article",
        "marx_engels_collection": "marx_engels_collection",
        "marx_engels_volume": "marx_engels_collection",
    }
    if raw in aliases:
        return aliases[raw]
    if _is_marx_engels_collection(meta):
        return "marx_engels_collection"
    if _first(meta, "journal_name", "journal_title", "journal", "periodical"):
        return "journal_article"
    if _first(meta, "container_title", "collection_title", "book_title") and _title(meta) != _book_title(meta):
        return "book_chapter"
    if _first(meta, "translator", "translators", "translated_by"):
        return "translated_book"
    return "book"


def _title(meta: CitationMetadata) -> str:
    return _strip_title_marks(_first(meta, "article_title", "chapter_title", "work_title", "title", "document_title", "display_title"))


def _book_title(meta: CitationMetadata) -> str:
    return _strip_title_marks(_first(meta, "book_title", "monograph_title", "document_title", "display_title", "title", "work_title"))


def _author_prefix(meta: CitationMetadata, include_country: bool = False) -> str:
    author = _author_plain(meta, include_country=include_country)
    return f"{author}：" if author else ""


def _author_plain(meta: CitationMetadata, include_country: bool = False) -> str:
    author = _first(meta, "author", "authors", "author_label", "creator")
    country = _first(meta, "country", "nationality") if include_country else ""
    if author and country and not author.startswith("[") and not author.startswith("［"):
        return f"[{country}]{author}"
    return author


def _is_marx_engels_collection(meta: CitationMetadata) -> bool:
    text = "".join(
        _first(meta, key)
        for key in ("collection_title", "document_title", "display_title", "title", "file_name")
    )
    compact = text.replace("《", "").replace("》", "").replace(" ", "")
    markers = ("马克思恩格斯文集", "马克思恩格斯全集", "马克思恩格斯选集", "马恩文集", "马恩全集", "马恩选集")
    return any(marker in compact for marker in markers)


def _marx_engels_collection_title(meta: CitationMetadata) -> str:
    title = _first(meta, "collection_title", "document_title", "display_title", "title")
    file_name = _first(meta, "file_name", "original_file_name")
    source = title or file_name
    if "全集" in source:
        return "马克思恩格斯全集"
    if "选集" in source:
        return "马克思恩格斯选集"
    return "马克思恩格斯文集"


def _marx_engels_volume(meta: CitationMetadata) -> str:
    volume = _first(meta, "volume_number", "volume")
    return f"第{volume}卷" if volume else ""


def _marx_engels_volume_title_chinese(meta: CitationMetadata) -> str:
    title = _quoted_title(_marx_engels_collection_title(meta))
    volume = _marx_engels_volume(meta)
    return f"{title}{volume}" if volume else title


def _marx_engels_volume_gb(meta: CitationMetadata, page: Dict[str, object]) -> str:
    title = _marx_engels_collection_title(meta)
    volume = _marx_engels_volume(meta)
    head = f"{title}:{volume}[M]" if volume else f"{title}[M]"
    publication = _publication_gb_no_space(meta)
    raw_page = str(page.get("gb") or "")
    if raw_page and not page.get("uncalibrated"):
        return f"{head}.{publication},{raw_page}" if publication else f"{head},{raw_page}"
    if raw_page:
        return f"{head}.{publication}.{raw_page}" if publication else f"{head}.{raw_page}"
    return f"{head}.{publication}" if publication else head


def _translator_chinese(meta: CitationMetadata) -> str:
    translator = _first(meta, "translator", "translators", "translated_by")
    return f"{translator}译" if translator else ""


def _translator_gb(meta: CitationMetadata) -> str:
    translator = _first(meta, "translator", "translators", "translated_by")
    return f"{translator}, 译" if translator else ""


def _journal_chinese(meta: CitationMetadata) -> str:
    journal = _first(meta, "journal_name", "journal_title", "journal", "periodical")
    year = _year(meta)
    issue = _first(meta, "issue", "issue_number", "journal_issue")
    pieces = []
    if journal:
        pieces.append(_quoted_title(journal))
    if year and issue:
        pieces.append(f"{year}年第{issue}期")
    elif year:
        pieces.append(f"{year}年")
    elif issue:
        pieces.append(f"第{issue}期")
    return "".join(pieces)


def _journal_gb(journal: str, year: str, volume: str, issue: str) -> str:
    pieces = []
    if journal:
        pieces.append(journal)
    issue_part = ""
    if volume and issue:
        issue_part = f"{volume}({issue})"
    elif issue:
        issue_part = f"({issue})"
    elif volume:
        issue_part = volume
    if year and issue_part:
        # 有卷次时用逗号分隔（2017, 49(4)），只有期号时紧跟年份（2021(3)）。
        pieces.append(f"{year}, {issue_part}" if volume else f"{year}{issue_part}")
    elif year:
        pieces.append(year)
    elif issue_part:
        pieces.append(issue_part)
    return ", ".join(pieces)


def _container_chinese(meta: CitationMetadata) -> str:
    container = _container_title_chinese(meta)
    if not container:
        return ""
    editor = _first(meta, "editor", "editors", "chief_editor")
    if editor:
        return f"载{editor}：{container}"
    return f"载{container}"


def _container_title_chinese(meta: CitationMetadata) -> str:
    collection = _first(meta, "collection_title")
    volume_number = _first(meta, "volume_number")
    if collection and volume_number:
        return f"{_quoted_title(collection)}第{volume_number}卷"
    container = _first(meta, "container_title", "book_title", "collection_title", "document_title")
    return _quoted_title(_strip_title_marks(container)) if container else ""


def _container_plain(meta: CitationMetadata) -> str:
    collection = _first(meta, "collection_title")
    volume_number = _first(meta, "volume_number")
    if collection and volume_number:
        return f"{collection} 第{volume_number}卷"
    return _strip_title_marks(_first(meta, "container_title", "book_title", "collection_title", "document_title"))


def _publisher_year_chinese(meta: CitationMetadata) -> str:
    publisher = _first(meta, "publisher", "press")
    year = _year(meta)
    if publisher and year:
        return f"{publisher}，{year}年"
    if publisher:
        return publisher
    if year:
        return f"{year}年"
    return ""


def _publication_chinese(meta: CitationMetadata) -> str:
    place = _first(meta, "publication_place", "place", "city", "publisher_place")
    publisher = _first(meta, "publisher", "press")
    year = _year(meta)
    publication = ""
    if place and publisher:
        publication = f"{place}：{publisher}"
    elif publisher:
        publication = publisher
    elif place:
        publication = place
    if publication and year:
        return f"{publication}，{year}年"
    if year:
        return f"{year}年"
    return publication


def _publication_gb_no_space(meta: CitationMetadata) -> str:
    place = _first(meta, "publication_place", "place", "city", "publisher_place")
    publisher = _first(meta, "publisher", "press")
    year = _year(meta)
    if place and publisher and year:
        return f"{place}:{publisher},{year}"
    if publisher and year:
        return f"{publisher},{year}"
    if place and publisher:
        return f"{place}:{publisher}"
    if year:
        return year
    return publisher or place


def _publisher_year_gb(meta: CitationMetadata) -> str:
    place = _first(meta, "publish_place", "publication_place", "place", "city", "publisher_place")
    publisher = _first(meta, "publisher", "press")
    year = _year(meta)
    if place and publisher and year:
        return f"{place}: {publisher}, {year}"
    if publisher and year:
        return f"{publisher}, {year}"
    if place and publisher:
        return f"{place}:{publisher}"
    if year:
        return year
    if publisher:
        return publisher
    return ""


def _join_gb_with_page(parts: object, page: Dict[str, object], separator: str = ": ") -> str:
    body = _join_gb(parts)
    raw = str(page.get("gb") or "")
    if not raw:
        return body
    if page.get("uncalibrated"):
        return _join_gb([body, raw])
    return f"{body}{separator}{raw}" if body else raw


def _year(meta: CitationMetadata) -> str:
    return _first(meta, "publish_year", "publication_year", "year", "date_label", "published_year")


def _page_range(start: str, end: str, separator: str = "-") -> str:
    if end and end != start:
        return f"{start}{separator}{end}"
    return start


def _chinese_page_label(start: str, end: str) -> str:
    if "页" in start:
        if end and end != start and "页" in end:
            return f"{start.rstrip('页')}—{end.rstrip('页')}页"
        return start
    return f"第{_page_range(start, end, separator='—')}页"


def _missing_fields(meta: CitationMetadata, page: Dict[str, object], style: str) -> List[str]:
    doc_type = _document_type(meta)
    if doc_type == "marx_engels_collection":
        return [] if not page.get("uncalibrated") else ["citation_page"]
    if doc_type not in {"book", "translated_book"}:
        return [] if not page.get("uncalibrated") else ["citation_page"]
    required = ["author", "title", "publisher", "publish_year"]
    if style == "gb":
        required.insert(3, "publish_place")
    if doc_type == "translated_book":
        required.insert(2, "translator")
    missing = []
    for field in required:
        if field == "author" and not _author_plain(meta, include_country=False):
            missing.append(field)
        elif field == "title" and not _book_title(meta):
            missing.append(field)
        elif field == "translator" and not _first(meta, "translator", "translators", "translated_by"):
            missing.append(field)
        elif field == "publisher" and not _first(meta, "publisher", "press"):
            missing.append(field)
        elif field == "publish_place" and not _first(meta, "publish_place", "publication_place", "place", "city", "publisher_place"):
            missing.append(field)
        elif field == "publish_year" and not _year(meta):
            missing.append(field)
    if page.get("uncalibrated") or not page.get("raw"):
        missing.append("citation_page")
    return missing


def _field_label(field: str) -> str:
    return {
        "author": "作者",
        "title": "书名",
        "translator": "译者",
        "publisher": "出版社",
        "publish_place": "出版地",
        "publish_year": "出版年份",
        "citation_page": "引用页码",
    }.get(field, field)


def _display_page_chinese(display: str) -> str:
    if "页" in display:
        return display
    return f"第{display}页"


def _quoted_title(title: str) -> str:
    title = _strip_title_marks(title)
    title = title.replace("《", "〈").replace("》", "〉")
    return f"《{title}》" if title else ""


def _strip_title_marks(value: object) -> str:
    text = _clean(value).strip("* ")
    while len(text) >= 2 and ((text[0] == "《" and text[-1] == "》") or (text[0] == "<" and text[-1] == ">")):
        text = text[1:-1].strip()
    return text


def _first(meta: CitationMetadata, *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        text = _clean(value)
        if text:
            return text
    return ""


def _clean(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(_clean(item) for item in value if _clean(item))
    return str(value).strip()


def _join_nonempty(parts: object) -> str:
    return "，".join(str(part).strip("， ") for part in parts if str(part).strip("， ")).replace("：，", "：")


def _join_space(parts: object) -> str:
    return " ".join(str(part).strip() for part in parts if str(part).strip())


def _join_gb(parts: object) -> str:
    return ". ".join(str(part).strip(". ") for part in parts if str(part).strip(". "))


def _finish_chinese(text: str) -> str:
    text = text.strip("，。 ")
    return f"{text}。" if text else "出处元数据不足，页码未验证。"


def _finish_gb(text: str) -> str:
    text = text.strip(". ")
    return f"{text}." if text else "Citation metadata unavailable."
