"""Zotero-style ISBN book lookup: K10plus (GBV union catalog) then Google Books.

Zotero resolves ISBNs against library catalogs (Library of Congress, GBV,
WorldCat), not Google Books — that is why it avoids Google's anonymous rate
limit and covers German titles well.  We mirror that: query the keyless K10plus
(GBV) SRU catalog first (excellent German/European coverage, MARCXML), and fall
back to Google Books for international titles it misses.  Parsing is limited to
the few bibliographic fields we need and every failure degrades to a structured
:class:`BookLookupError`.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Mapping, Optional

from .foreign_book_lookup import (
    BookLookupError,
    _clean,
    _match,
    _normalize_isbn,
    _open_url,
    lookup_google_books,
)

K10PLUS_ENDPOINT = "https://sru.k10plus.de/gvk"
_TIMEOUT_SECONDS = 12
_MAX_RECORDS = 5
_USER_AGENT = "MEFinder/0.3 (bibliographic lookup)"
_MARC = "{http://www.loc.gov/MARC21/slim}"


def lookup_book(metadata: Mapping[str, object]) -> Dict[str, object]:
    """Return book candidates from K10plus first, then Google Books as fallback."""

    isbn = _normalize_isbn(metadata.get("isbn"))
    open_url = _open_url(metadata, isbn)
    candidates: List[Dict[str, object]] = []
    catalog_error: Optional[BookLookupError] = None
    try:
        candidates = lookup_k10plus(metadata)
    except BookLookupError as exc:
        catalog_error = exc
    if candidates:
        return {"candidates": candidates, "open_url": open_url}
    # K10plus 未命中或不可达时退回 Google Books（国际覆盖更广）。
    try:
        fallback = lookup_google_books(metadata)
    except BookLookupError as exc:
        # 两个源都失败：优先报联网错误，让用户知道是网络/代理问题。
        raise catalog_error or exc
    return {"candidates": fallback.get("candidates", []), "open_url": fallback.get("open_url") or open_url}


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
    request = urllib.request.Request(
        f"{K10PLUS_ENDPOINT}?{params}",
        headers={"User-Agent": _USER_AGENT, "Accept": "application/xml"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise BookLookupError("rate_limited", "K10plus 暂时限流，请稍后重试。")
        raise BookLookupError("http_error", f"K10plus 请求失败（HTTP {exc.code}）。")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BookLookupError("timeout", "连接图书目录失败，请检查网络后重试。")
    return parse_k10plus_marcxml(raw, metadata, isbn)


def _build_cql(metadata: Mapping[str, object]) -> tuple[str, str]:
    isbn = _normalize_isbn(metadata.get("isbn"))
    if isbn:
        return f"pica.isb={isbn}", isbn
    title = _clean(metadata.get("title"))
    if not title:
        return "", ""
    return f'pica.tit="{title}"', ""


def parse_k10plus_marcxml(
    raw: str, requested: Mapping[str, object], isbn: str
) -> List[Dict[str, object]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise BookLookupError("site_changed", "图书目录返回内容无法解析。")
    candidates: List[Dict[str, object]] = []
    for record in root.iter(f"{_MARC}record"):
        candidate = _candidate_from_marc(record, requested, isbn)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda c: -float(c["match"]["score"]))
    return candidates


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
    record: ET.Element, requested: Mapping[str, object], isbn: str
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
            "source": "k10plus",
            "source_page": None,
            "evidence_text": f"K10plus（GBV）：{full_title}",
            "value": value,
        }
        for field, value in metadata.items()
    }
    return {
        "metadata": metadata,
        "match": match,
        "record_url": "",
        "publish_date": year,
        "evidence": evidence,
    }
