"""Zotero-style book lookup across several keyless library catalogs.

Zotero resolves books against library catalogs (Library of Congress, GBV,
WorldCat), not Google Books — that is why it avoids Google's anonymous rate
limit and reaches titles Google misses.  We mirror that with a sequence of
keyless sources, tried in order until one returns candidates:

1. **Open Library** — keyless JSON, globally reachable (including mainland
   China without a proxy), good ISBN coverage.  Tried first so users behind the
   Great Firewall get a working source instead of a timeout.
2. **K10plus (GBV)** — SRU/MARCXML union catalog, excellent German/European
   coverage.
3. **Library of Congress** — SRU/MARCXML, strong English-language coverage.
4. **Google Books** — last resort only; unreachable from mainland China without
   a proxy, so it must never be the primary path.

Every source degrades to a structured :class:`BookLookupError`; a real "not
found" (all sources reachable, none matched) returns an empty candidate list
rather than raising, so the UI can tell "no match" apart from "network problem".
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Mapping, Optional

from .foreign_book_lookup import (
    BookLookupError,
    _clean,
    _match,
    _normalize_isbn,
    _open_url,
    lookup_google_books,
)

K10PLUS_ENDPOINT = "https://sru.k10plus.de/gvk"
OPENLIBRARY_ISBN_ENDPOINT = "https://openlibrary.org/api/books"
OPENLIBRARY_SEARCH_ENDPOINT = "https://openlibrary.org/search.json"
LOC_SRU_ENDPOINT = "http://lx2.loc.gov:210/lcdb"
_TIMEOUT_SECONDS = 12
_MAX_RECORDS = 5
_USER_AGENT = "MEFinder/0.3 (bibliographic lookup)"
_MARC = "{http://www.loc.gov/MARC21/slim}"


def lookup_book(metadata: Mapping[str, object]) -> Dict[str, object]:
    """Return book candidates from the first source that matches.

    Sources are tried in reachability order (Open Library → K10plus → LoC →
    Google Books).  If every source raises, the most relevant error is
    re-raised; if sources are reachable but nothing matches, an empty candidate
    list is returned so the caller can distinguish "no match" from "offline".
    """

    isbn = _normalize_isbn(metadata.get("isbn"))
    open_url = _openlibrary_open_url(metadata, isbn) or _open_url(metadata, isbn)

    sources: tuple[Callable[[Mapping[str, object]], List[Dict[str, object]]], ...] = (
        lookup_open_library,
        lookup_k10plus,
        lookup_loc,
        _google_books_candidates,
    )
    network_error: Optional[BookLookupError] = None
    invalid_error: Optional[BookLookupError] = None
    for source in sources:
        try:
            candidates = source(metadata)
        except BookLookupError as exc:
            if exc.code == "invalid_query":
                invalid_error = invalid_error or exc
            else:
                network_error = network_error or exc
            continue
        if candidates:
            return {"candidates": candidates, "open_url": open_url}
    # 没有任何源命中：优先报联网错误（让用户知道是网络/代理问题），
    # 其次报查询无效，最后才是"确实查不到"（返回空列表，不算错误）。
    if network_error is not None:
        raise network_error
    if invalid_error is not None:
        raise invalid_error
    return {"candidates": [], "open_url": open_url}


# ── Open Library (keyless JSON, 中国大陆可达) ──────────────────────────────────


def _openlibrary_open_url(metadata: Mapping[str, object], isbn: str) -> str:
    if isbn:
        return f"https://openlibrary.org/isbn/{isbn}"
    title = _clean(metadata.get("title"))
    if title:
        return "https://openlibrary.org/search?q=" + urllib.parse.quote(title)
    return ""


def _fetch(url: str, *, accept: str, source_label: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise BookLookupError("rate_limited", f"{source_label} 暂时限流，请稍后重试。")
        raise BookLookupError("http_error", f"{source_label} 请求失败（HTTP {exc.code}）。")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BookLookupError("timeout", f"连接{source_label}失败，请检查网络后重试。")


def lookup_open_library(metadata: Mapping[str, object]) -> List[Dict[str, object]]:
    isbn = _normalize_isbn(metadata.get("isbn"))
    if isbn:
        params = urllib.parse.urlencode(
            {"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"}
        )
        raw = _fetch(
            f"{OPENLIBRARY_ISBN_ENDPOINT}?{params}", accept="application/json", source_label="Open Library"
        )
        return parse_openlibrary_isbn(raw, metadata, isbn)
    title = _clean(metadata.get("title"))
    if not title:
        raise BookLookupError("invalid_query", "请先填写 ISBN 或书名。")
    query = {"title": title, "limit": _MAX_RECORDS, "fields": "title,subtitle,author_name,first_publish_year,publisher,isbn,key"}
    author = _clean(metadata.get("author"))
    if author:
        query["author"] = author
    raw = _fetch(
        f"{OPENLIBRARY_SEARCH_ENDPOINT}?{urllib.parse.urlencode(query)}",
        accept="application/json",
        source_label="Open Library",
    )
    return parse_openlibrary_search(raw, metadata, "")


def parse_openlibrary_isbn(
    raw: str, requested: Mapping[str, object], isbn: str
) -> List[Dict[str, object]]:
    try:
        data = json.loads(raw)
    except ValueError:
        raise BookLookupError("site_changed", "Open Library 返回内容无法解析。")
    if not isinstance(data, dict):
        return []
    candidates: List[Dict[str, object]] = []
    for entry in data.values():
        if not isinstance(entry, Mapping):
            continue
        candidate = _candidate_from_openlibrary(entry, requested, isbn)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda c: -float(c["match"]["score"]))
    return candidates


def parse_openlibrary_search(
    raw: str, requested: Mapping[str, object], isbn: str
) -> List[Dict[str, object]]:
    try:
        data = json.loads(raw)
    except ValueError:
        raise BookLookupError("site_changed", "Open Library 返回内容无法解析。")
    docs = data.get("docs") if isinstance(data, dict) else None
    if not isinstance(docs, list):
        return []
    candidates: List[Dict[str, object]] = []
    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        candidate = _candidate_from_openlibrary_doc(doc, requested, isbn)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda c: -float(c["match"]["score"]))
    return candidates


def _candidate_from_openlibrary(
    entry: Mapping[str, object], requested: Mapping[str, object], isbn: str
) -> Optional[Dict[str, object]]:
    title = _clean(entry.get("title"))
    if not title:
        return None
    subtitle = _clean(entry.get("subtitle"))
    full_title = f"{title}: {subtitle}" if subtitle else title
    author = _join_names(entry.get("authors"), key="name")
    publisher = _join_names(entry.get("publishers"), key="name", first_only=True)
    place = _join_names(entry.get("publish_places"), key="name", first_only=True)
    year = _year4(_clean(entry.get("publish_date")))
    isbns = _openlibrary_isbns(entry.get("identifiers"))
    record_url = _clean(entry.get("url"))
    return _assemble_candidate(
        full_title, author, publisher, place, year, isbns, record_url,
        requested, isbn, source_key="open_library", source_label="Open Library",
    )


def _candidate_from_openlibrary_doc(
    doc: Mapping[str, object], requested: Mapping[str, object], isbn: str
) -> Optional[Dict[str, object]]:
    title = _clean(doc.get("title"))
    if not title:
        return None
    subtitle = _clean(doc.get("subtitle"))
    full_title = f"{title}: {subtitle}" if subtitle else title
    author = _join_list(doc.get("author_name"))
    publisher = _first_of(doc.get("publisher"))
    year = _year4(str(doc.get("first_publish_year") or ""))
    isbns = [v for v in (_normalize_isbn(x) for x in _as_list(doc.get("isbn"))) if v]
    key = _clean(doc.get("key"))
    record_url = f"https://openlibrary.org{key}" if key.startswith("/") else ""
    return _assemble_candidate(
        full_title, author, publisher, "", year, isbns, record_url,
        requested, isbn, source_key="open_library", source_label="Open Library",
    )


def _openlibrary_isbns(identifiers: object) -> List[str]:
    if not isinstance(identifiers, Mapping):
        return []
    result: List[str] = []
    for code in ("isbn_13", "isbn_10"):
        for value in _as_list(identifiers.get(code)):
            normalized = _normalize_isbn(value)
            if normalized and normalized not in result:
                result.append(normalized)
    return result


# ── Library of Congress (SRU / MARCXML) ──────────────────────────────────────


def lookup_loc(metadata: Mapping[str, object]) -> List[Dict[str, object]]:
    isbn = _normalize_isbn(metadata.get("isbn"))
    if isbn:
        query = f"bath.isbn={isbn}"
    else:
        title = _clean(metadata.get("title"))
        if not title:
            raise BookLookupError("invalid_query", "请先填写 ISBN 或书名。")
        query = f'bath.title="{title}"'
    params = urllib.parse.urlencode(
        {
            "version": "1.1",
            "operation": "searchRetrieve",
            "recordSchema": "marcxml",
            "maximumRecords": _MAX_RECORDS,
            "query": query,
        }
    )
    raw = _fetch(
        f"{LOC_SRU_ENDPOINT}?{params}", accept="application/xml", source_label="LoC（美国国会图书馆）"
    )
    return parse_marcxml(raw, metadata, isbn, source_key="loc", source_label="LoC（美国国会图书馆）")


# ── K10plus (SRU / MARCXML) ──────────────────────────────────────────────────


def lookup_k10plus(metadata: Mapping[str, object]) -> List[Dict[str, object]]:
    query, isbn = _build_cql(metadata)
    if not query:
        raise BookLookupError("invalid_query", "请先填写 ISBN 或书名。")
    params = urllib.parse.urlencode(
        {
            "version": "1.1",
            "operation": "searchRetrieve",
            "recordSchema": "marcxml",
            "maximumRecords": _MAX_RECORDS,
            "query": query,
        }
    )
    raw = _fetch(
        f"{K10PLUS_ENDPOINT}?{params}", accept="application/xml", source_label="K10plus"
    )
    return parse_marcxml(raw, metadata, isbn, source_key="k10plus", source_label="K10plus（GBV）")


def _build_cql(metadata: Mapping[str, object]) -> tuple[str, str]:
    isbn = _normalize_isbn(metadata.get("isbn"))
    if isbn:
        return f"pica.isb={isbn}", isbn
    title = _clean(metadata.get("title"))
    if not title:
        return "", ""
    return f'pica.tit="{title}"', ""


def parse_marcxml(
    raw: str,
    requested: Mapping[str, object],
    isbn: str,
    *,
    source_key: str,
    source_label: str,
) -> List[Dict[str, object]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise BookLookupError("site_changed", "图书目录返回内容无法解析。")
    candidates: List[Dict[str, object]] = []
    for record in root.iter(f"{_MARC}record"):
        candidate = _candidate_from_marc(record, requested, isbn, source_key, source_label)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda c: -float(c["match"]["score"]))
    return candidates


def parse_k10plus_marcxml(
    raw: str, requested: Mapping[str, object], isbn: str
) -> List[Dict[str, object]]:
    """Backwards-compatible wrapper: parse K10plus MARCXML with k10plus labels."""

    return parse_marcxml(raw, requested, isbn, source_key="k10plus", source_label="K10plus（GBV）")


def _datafields(record: ET.Element, tag: str) -> List[List[tuple]]:
    fields: List[List[tuple]] = []
    for df in record.findall(f"{_MARC}datafield"):
        if df.get("tag") == tag:
            fields.append([(s.get("code"), (s.text or "").strip()) for s in df.findall(f"{_MARC}subfield")])
    return fields


def _first_sub(field: List[tuple], code: str) -> str:
    for sub_code, value in field:
        if sub_code == code and value:
            return value
    return ""


def _person(name: str) -> str:
    name = _clean(name).rstrip(",").strip()
    if "," in name:
        family, given = name.split(",", 1)
        rebuilt = f"{given.strip()} {family.strip()}".strip()
        return rebuilt or name
    return name


def _candidate_from_marc(
    record: ET.Element,
    requested: Mapping[str, object],
    isbn: str,
    source_key: str = "k10plus",
    source_label: str = "K10plus（GBV）",
) -> Optional[Dict[str, object]]:
    title_fields = _datafields(record, "245")
    if not title_fields:
        return None
    title_main = _clean(_first_sub(title_fields[0], "a")).rstrip(" /:").strip()
    if not title_main:
        return None
    subtitle = _clean(_first_sub(title_fields[0], "b")).rstrip(" /:").strip()
    full_title = f"{title_main}: {subtitle}" if subtitle else title_main

    authors: List[str] = []
    for field in _datafields(record, "100") + _datafields(record, "700"):
        person = _person(_first_sub(field, "a"))
        if person and person not in authors:
            authors.append(person)
    author = ", ".join(authors)

    place = publisher = year = ""
    for tag in ("264", "260"):
        for field in _datafields(record, tag):
            place = place or _clean(_first_sub(field, "a")).rstrip(" :,").strip()
            publisher = publisher or _clean(_first_sub(field, "b")).rstrip(" ,").strip()
            if not year:
                match = re.search(r"(1[5-9]\d{2}|20\d{2})", _first_sub(field, "c"))
                year = match.group(1) if match else ""
        if publisher or year:
            break

    isbns: List[str] = []
    for field in _datafields(record, "020"):
        for code in ("a", "9"):
            value = _normalize_isbn(_first_sub(field, code))
            if value and value not in isbns:
                isbns.append(value)

    return _assemble_candidate(
        full_title, author, publisher, place, year, isbns, "",
        requested, isbn, source_key=source_key, source_label=source_label,
    )


# ── Shared candidate assembly ────────────────────────────────────────────────


def _assemble_candidate(
    full_title: str,
    author: str,
    publisher: str,
    place: str,
    year: str,
    isbns: List[str],
    record_url: str,
    requested: Mapping[str, object],
    isbn: str,
    *,
    source_key: str,
    source_label: str,
) -> Optional[Dict[str, object]]:
    if not full_title:
        return None
    metadata: Dict[str, str] = {"title": full_title}
    if author:
        metadata["author"] = author
    if publisher:
        metadata["publisher"] = publisher
    if place:
        metadata["publish_place"] = place
    if year:
        metadata["publish_year"] = year
    resolved_isbn = isbn if isbn in isbns else (isbns[0] if isbns else "")
    if resolved_isbn:
        metadata["isbn"] = resolved_isbn

    match = _match(requested, full_title, author, isbns, isbn)
    evidence = {
        field: {
            "source": source_key,
            "source_page": None,
            "evidence_text": f"{source_label}：{full_title}",
            "value": value,
            "record_url": record_url,
        }
        for field, value in metadata.items()
    }
    return {
        "metadata": metadata,
        "match": match,
        "record_url": record_url,
        "publish_date": year,
        "evidence": evidence,
    }


def _google_books_candidates(metadata: Mapping[str, object]) -> List[Dict[str, object]]:
    result = lookup_google_books(metadata)
    candidates = result.get("candidates")
    return candidates if isinstance(candidates, list) else []


# ── small value helpers ──────────────────────────────────────────────────────


def _as_list(value: object) -> List[object]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _first_of(value: object) -> str:
    items = _as_list(value)
    return _clean(items[0]) if items else ""


def _join_list(value: object) -> str:
    return ", ".join(v for v in (_clean(x) for x in _as_list(value)) if v)


def _join_names(value: object, *, key: str, first_only: bool = False) -> str:
    names: List[str] = []
    for entry in _as_list(value):
        name = _clean(entry.get(key)) if isinstance(entry, Mapping) else _clean(entry)
        if name and name not in names:
            names.append(name)
            if first_only:
                break
    return ", ".join(names)


def _year4(text: str) -> str:
    match = re.search(r"(1[5-9]\d{2}|20\d{2})", text or "")
    return match.group(1) if match else ""
