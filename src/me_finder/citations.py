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

    raw_style = str(citation_style).lower()
    if raw_style in {"chicago", "cms", "cmos", "chicago-note"}:
        style = "chicago"
    elif raw_style in {"apa", "apa7", "apa-7"}:
        style = "apa"
    elif raw_style in {"mla", "mla9", "mla-9"}:
        style = "mla"
    elif raw_style in {"gb", "gbt", "gb/t", "gbt7714", "gb/t 7714"}:
        style = "gb"
    else:
        style = "chinese"
    page = _page_info(hit_page)
    if style == "gb":
        return _format_gb(document_metadata, page)
    if style == "chicago":
        return _format_chicago(document_metadata, page)
    if style == "apa":
        return _format_apa(document_metadata)
    if style == "mla":
        return _format_mla(document_metadata)
    return _format_chinese(document_metadata, page)


def build_citation_formats(document_metadata: CitationMetadata, hit_page: object) -> Dict[str, object]:
    page = _page_info(hit_page)
    chinese_missing = _missing_fields(document_metadata, page, "chinese")
    gb_missing = _missing_fields(document_metadata, page, "gb")
    chicago_missing = _missing_fields(document_metadata, page, "chicago")
    apa_missing = _missing_fields(document_metadata, page, "apa")
    mla_missing = _missing_fields(document_metadata, page, "mla")
    return {
        "chinese": format_citation(document_metadata, hit_page, "chinese"),
        "gb": format_citation(document_metadata, hit_page, "gb"),
        "chicago": format_citation(document_metadata, hit_page, "chicago"),
        "apa": format_citation(document_metadata, hit_page, "apa"),
        "mla": format_citation(document_metadata, hit_page, "mla"),
        "chinese_status": "complete" if not chinese_missing else "metadata_incomplete",
        "gb_status": "complete" if not gb_missing else "metadata_incomplete",
        "chicago_status": "complete" if not chicago_missing else "metadata_incomplete",
        "apa_status": "complete" if not apa_missing else "metadata_incomplete",
        "mla_status": "complete" if not mla_missing else "metadata_incomplete",
        "chinese_missing_fields": chinese_missing,
        "gb_missing_fields": gb_missing,
        "chicago_missing_fields": chicago_missing,
        "apa_missing_fields": apa_missing,
        "mla_missing_fields": mla_missing,
    }


def _format_chinese(meta: CitationMetadata, page: Dict[str, object]) -> str:
    doc_type = _document_type(meta)
    if doc_type == "thesis":
        return _finish_chinese(_join_nonempty([
            _author_prefix(meta),
            _quoted_title(_title(meta)),
            _publisher_year_chinese(meta),
        ]))
    if page.get("uncalibrated"):
        return "该文献页码尚未校准，不能生成可靠脚注。"
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
    doc_type = _document_type(meta)
    if doc_type == "thesis":
        missing = _missing_fields(meta, page, "gb")
        if missing:
            labels = ["学校" if field == "publisher" else _field_label(field) for field in missing]
            return f"无法生成完整 GB/T 引文：缺少{' / '.join(labels)}。"
        title = _title(meta)
        base = _join_gb([_author_plain(meta), f"{title}[D]" if title else ""])
        school_year = f"{_first(meta, 'publisher', 'press')}, {_year(meta)}"
        return _finish_gb(_join_gb([base, school_year]))
    if page.get("uncalibrated"):
        return "该文献页码尚未校准，不能生成 GB/T 引文。"
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


def _format_chicago(meta: CitationMetadata, page: Dict[str, object]) -> str:
    """Chicago 17th notes-bibliography *footnote* form.

    Chinese source names are not inverted (Chinese personal names have no
    first/last separation to reorder).  Article/chapter/thesis titles take
    double quotes; book/journal titles are left plain (they would be italic in
    a word processor, which a copied plain string cannot carry).
    """

    doc_type = _document_type(meta)
    author = _author_plain(meta)

    if doc_type == "thesis":
        title = _title(meta)
        paren = _chicago_paren(["学位论文", _first(meta, "publisher", "press"), _year(meta)])
        head = _join_chicago([author, _chicago_quoted(title)])
        body = f"{head} {paren}".strip() if paren else head
        return _finish_chicago(_chicago_with_page(body, page, thesis=True))

    if page.get("uncalibrated"):
        return "该文献页码尚未校准，不能生成 Chicago 引文。"

    if doc_type == "marx_engels_collection":
        volume = _marx_engels_volume(meta)
        book_title = _marx_engels_collection_title(meta) + (volume if volume else "")
        paren = _chicago_place_pub_year(meta)
        head = _join_chicago([author, book_title]) if author else book_title
        body = f"{head} {paren}".strip() if paren else head
        return _finish_chicago(_chicago_with_page(body, page))

    if doc_type == "journal_article":
        head = _join_chicago([author, _chicago_quoted(_title(meta), trailing_comma=True)])
        journal_part = _chicago_journal(
            _first(meta, "journal_name", "journal_title", "journal", "periodical"),
            _first(meta, "volume", "journal_volume"),
            _first(meta, "issue", "issue_number", "journal_issue"),
            _year(meta),
        )
        body = f"{head} {journal_part}".strip() if journal_part else head
        raw = str(page.get("raw") or "")
        return _finish_chicago(f"{body}: {raw}" if raw else body)

    if doc_type in {"book_chapter", "collection_article"}:
        container = _container_plain(meta)
        editor = _first(meta, "editor", "editors", "chief_editor")
        container_part = ""
        if container:
            container_part = f"in {container}"
            if editor:
                container_part += f", ed. {editor}"
        paren = _chicago_place_pub_year(meta)
        head = _join_chicago([author, _chicago_quoted(_title(meta), trailing_comma=bool(container_part))])
        body = _join_chicago([head, container_part]) if container_part else head
        body = f"{body} {paren}".strip() if paren else body
        return _finish_chicago(_chicago_with_page(body, page))

    if doc_type == "translated_book":
        translator = _first(meta, "translator", "translators", "translated_by")
        trans_part = f"trans. {translator}" if translator else ""
        paren = _chicago_place_pub_year(meta)
        head = _join_chicago([author, _book_title(meta), trans_part])
        body = f"{head} {paren}".strip() if paren else head
        return _finish_chicago(_chicago_with_page(body, page))

    paren = _chicago_place_pub_year(meta)
    head = _join_chicago([author, _book_title(meta)])
    body = f"{head} {paren}".strip() if paren else head
    return _finish_chicago(_chicago_with_page(body, page))


def _chicago_quoted(title: str, trailing_comma: bool = False) -> str:
    title = _strip_title_marks(title)
    if not title:
        return ""
    return f'"{title},"' if trailing_comma else f'"{title}"'


def _chicago_journal(journal: str, volume: str, issue: str, year: str) -> str:
    if not journal:
        return f"({year})" if year else ""
    parts = journal
    if volume:
        parts += f" {volume}"
    if issue:
        parts += f", no. {issue}"
    if year:
        parts += f" ({year})"
    return parts


def _chicago_place_pub_year(meta: CitationMetadata) -> str:
    place = _first(meta, "publish_place", "publication_place", "place", "city", "publisher_place")
    publisher = _first(meta, "publisher", "press")
    year = _year(meta)
    head = f"{place}: {publisher}" if place and publisher else (publisher or place)
    return _chicago_paren([head, year])


def _chicago_paren(parts: object) -> str:
    inner = ", ".join(str(part).strip() for part in parts if str(part).strip())
    return f"({inner})" if inner else ""


def _chicago_with_page(body: str, page: Dict[str, object], thesis: bool = False) -> str:
    raw = str(page.get("raw") or "")
    if not raw or page.get("uncalibrated"):
        return body
    return f"{body}, {raw}" if body else raw


def _join_chicago(parts: object) -> str:
    return ", ".join(str(part).strip().strip(",") for part in parts if str(part).strip().strip(","))


def _finish_chicago(text: str) -> str:
    text = text.strip().strip(",").strip()
    return f"{text}." if text else "出处元数据不足，无法生成 Chicago 引文。"


def _format_apa(meta: CitationMetadata) -> str:
    """APA 7 reference-list form (plain text, without typography)."""

    doc_type = _document_type(meta)
    author = _author_plain(meta)
    year = _year(meta)
    date = f"({year})." if year else "(n.d.)."
    doi = _doi_url(meta)

    if doc_type == "journal_article":
        journal = _first(meta, "journal_name", "journal_title", "journal", "periodical")
        volume = _first(meta, "volume", "journal_volume")
        issue = _first(meta, "issue", "issue_number", "journal_issue")
        pages = _first(meta, "page_range", "pages", "article_pages")
        journal_part = journal
        if volume:
            journal_part += (", " if journal_part else "") + volume
        if issue:
            journal_part += f"({issue})"
        if pages:
            journal_part += (", " if journal_part else "") + pages
        return _finish_reference(_join_reference([author, date, _title(meta), journal_part, doi]))

    if doc_type == "thesis":
        title = _title(meta)
        school = _first(meta, "publisher", "press")
        thesis = f"{title} [学位论文, {school}]" if school else f"{title} [学位论文]"
        return _finish_reference(_join_reference([author, date, thesis, doi]))

    if doc_type in {"book_chapter", "collection_article"}:
        editor = _first(meta, "editor", "editors", "chief_editor")
        container = _container_plain(meta)
        container_part = f"In {editor} (Ed.), {container}" if editor and container else (f"In {container}" if container else "")
        pages = _first(meta, "page_range", "pages", "article_pages")
        if pages and container_part:
            container_part += f" (pp. {pages})"
        return _finish_reference(_join_reference([
            author, date, _title(meta), container_part, _first(meta, "publisher", "press"), doi
        ]))

    translator = _first(meta, "translator", "translators", "translated_by")
    title = _book_title(meta)
    if doc_type == "translated_book" and translator:
        title += f" ({translator}, Trans.)"
    return _finish_reference(_join_reference([
        author, date, title, _first(meta, "publisher", "press"), doi
    ]))


def _format_mla(meta: CitationMetadata) -> str:
    """MLA 9 works-cited form (plain text, without typography)."""

    doc_type = _document_type(meta)
    author = _author_plain(meta)
    year = _year(meta)
    doi = _doi_url(meta)

    if doc_type == "journal_article":
        journal = _first(meta, "journal_name", "journal_title", "journal", "periodical")
        volume = _first(meta, "volume", "journal_volume")
        issue = _first(meta, "issue", "issue_number", "journal_issue")
        pages = _first(meta, "page_range", "pages", "article_pages")
        container_parts = [journal]
        if volume:
            container_parts.append(f"vol. {volume}")
        if issue:
            container_parts.append(f"no. {issue}")
        if year:
            container_parts.append(year)
        if pages:
            container_parts.append(f"pp. {pages}")
        if doi:
            container_parts.append(doi)
        head = _join_reference([author, _mla_quoted(_title(meta))])
        return _finish_reference(_join_reference([head, ", ".join(filter(None, container_parts))]))

    if doc_type == "thesis":
        school = _first(meta, "publisher", "press")
        head = _join_reference([author, _book_title(meta)])
        details = ", ".join(filter(None, [year, school, "学位论文", doi]))
        return _finish_reference(_join_reference([head, details]))

    if doc_type in {"book_chapter", "collection_article"}:
        container = _container_plain(meta)
        editor = _first(meta, "editor", "editors", "chief_editor")
        if editor:
            container += (", " if container else "") + f"edited by {editor}"
        pages = _first(meta, "page_range", "pages", "article_pages")
        head = _join_reference([author, _mla_quoted(_title(meta))])
        details = ", ".join(filter(None, [
            container, _first(meta, "publisher", "press"), year,
            f"pp. {pages}" if pages else "", doi,
        ]))
        return _finish_reference(_join_reference([head, details]))

    translator = _first(meta, "translator", "translators", "translated_by")
    translated_by = f"Translated by {translator}" if doc_type == "translated_book" and translator else ""
    head = _join_reference([author, _book_title(meta), translated_by])
    details = ", ".join(filter(None, [_first(meta, "publisher", "press"), year, doi]))
    return _finish_reference(_join_reference([head, details]))


def _mla_quoted(title: str) -> str:
    title = _strip_title_marks(title)
    return f'“{title}”' if title else ""


def _doi_url(meta: CitationMetadata) -> str:
    doi = _first(meta, "doi", "DOI")
    if not doi:
        return ""
    normalized = doi.strip().removeprefix("doi:").removeprefix("DOI:").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/", "https://dx.doi.org/"):
        if normalized.lower().startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return f"https://doi.org/{normalized}" if normalized else ""


def _join_reference(parts: object) -> str:
    return ". ".join(str(part).strip(" .") for part in parts if str(part).strip(" ."))


def _finish_reference(text: str) -> str:
    text = text.strip(" .")
    return f"{text}." if text else "Citation metadata unavailable."


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
    if _is_marx_engels_collection(meta):
        return "marx_engels_collection"
    aliases = {
        "journal": "journal_article",
        "article": "journal_article",
        "journal-article": "journal_article",
        "journal_article": "journal_article",
        "thesis": "thesis",
        "dissertation": "thesis",
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
    if not volume:
        return ""
    text = str(volume).strip()
    if text.startswith("第"):
        return text
    if "卷" in text:
        return f"第{text}"
    return f"第{text}卷"


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
    place = _first(meta, "publish_place", "publication_place", "place", "city", "publisher_place")
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
    place = _first(meta, "publish_place", "publication_place", "place", "city", "publisher_place")
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
    if doc_type == "thesis":
        required = ["author", "title", "publisher", "publish_year"]
        missing = []
        for field in required:
            if field == "author" and not _author_plain(meta, include_country=False):
                missing.append(field)
            elif field == "title" and not _title(meta):
                missing.append(field)
            elif field == "publisher" and not _first(meta, "publisher", "press"):
                missing.append(field)
            elif field == "publish_year" and not _year(meta):
                missing.append(field)
        return missing
    if doc_type == "journal_article":
        # Chicago 脚注引命中页；APA/MLA 是参考文献表体例，不要求命中页。
        # GB/中文仍需期号，且有文章起止页时可兜底。
        required = ["author", "title", "journal_name", "publish_year"]
        if style in {"chinese", "gb"}:
            required.append("issue")
        missing = []
        for field in required:
            if field == "author" and not _author_plain(meta, include_country=False):
                missing.append(field)
            elif field == "title" and not _title(meta):
                missing.append(field)
            elif field == "journal_name" and not _first(meta, "journal_name", "journal_title", "journal", "periodical"):
                missing.append(field)
            elif field == "publish_year" and not _year(meta):
                missing.append(field)
            elif field == "issue" and not _first(meta, "issue", "issue_number", "journal_issue"):
                missing.append(field)
        has_range_fallback = style in {"chinese", "gb"} and _first(meta, "page_range", "pages", "article_pages")
        needs_hit_page = style not in {"apa", "mla"}
        if needs_hit_page and not has_range_fallback and (page.get("uncalibrated") or not page.get("raw")):
            missing.append("citation_page")
        return missing
    if doc_type not in {"book", "translated_book"}:
        if style in {"apa", "mla"}:
            return []
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
    if style not in {"apa", "mla"} and (page.get("uncalibrated") or not page.get("raw")):
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
        "journal_name": "出版刊物",
        "issue": "期号",
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
