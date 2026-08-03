"""Google Books lookup for foreign-language monographs.

Only fills book metadata for *foreign* titles; Chinese books stay on local CIP
recognition.  The Google Books ``volumes`` endpoint is a clean, keyless JSON API
with a stable schema, so this module parses JSON (never scrapes HTML) and
degrades safely on network, quota, or parse failure — it never blocks or guesses.

Google is unreachable from mainland China without a proxy; ``urllib`` follows the
system proxy, so the desktop app inherits the user's proxy automatically and
returns a structured ``timeout``/``blocked`` error when it cannot connect.
"""

from __future__ import annotations

import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Mapping, Optional, Tuple

GOOGLE_BOOKS_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
_TIMEOUT_SECONDS = 12
_MAX_RESULTS = 5
_USER_AGENT = "MEFinder/0.3 (+bibliographic lookup)"


class BookLookupError(Exception):
    """A recoverable Google Books lookup failure with a stable machine code."""

    def __init__(self, code: str, message: str, open_url: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.open_url = open_url


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_isbn(value: object) -> str:
    return re.sub(r"[^0-9Xx]", "", _clean(value)).upper()


def _compact(value: object) -> str:
    return re.sub(r"[^0-9a-z]+", "", _clean(value).lower())


def _year_of(published_date: object) -> str:
    match = re.search(r"(1[5-9]\d{2}|20\d{2})", _clean(published_date))
    return match.group(1) if match else ""


def _build_query(metadata: Mapping[str, object]) -> Tuple[str, str]:
    """Return ``(query, isbn)``; ISBN queries are the precise path."""

    isbn = _normalize_isbn(metadata.get("isbn"))
    if isbn:
        return f"isbn:{isbn}", isbn
    title = _clean(metadata.get("title"))
    if not title:
        return "", ""
    parts = [f'intitle:{title}']
    author = _clean(metadata.get("author"))
    if author:
        parts.append(f'inauthor:{author}')
    return " ".join(parts), ""


def _open_url(metadata: Mapping[str, object], isbn: str) -> str:
    if isbn:
        return f"https://books.google.com/books?vid=ISBN{isbn}"
    title = _clean(metadata.get("title"))
    if title:
        return "https://www.google.com/search?tbm=bks&q=" + urllib.parse.quote(title)
    return "https://books.google.com/"


def lookup_google_books(metadata: Mapping[str, object]) -> Dict[str, object]:
    """Query Google Books and return conservative candidates.

    ``metadata`` may carry ``isbn``, ``title``, ``author``, ``publish_year``.
    Raises :class:`BookLookupError` on any failure so the caller can degrade.
    """

    query, isbn = _build_query(metadata)
    open_url = _open_url(metadata, isbn)
    if not query:
        raise BookLookupError("invalid_query", "请先填写 ISBN 或书名。", open_url)
    params = urllib.parse.urlencode(
        {"q": query, "maxResults": _MAX_RESULTS, "printType": "books"}
    )
    request = urllib.request.Request(
        f"{GOOGLE_BOOKS_ENDPOINT}?{params}",
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise BookLookupError("rate_limited", "Google Books 暂时限流，请稍后重试。", open_url)
        raise BookLookupError("http_error", f"Google Books 请求失败（HTTP {exc.code}）。", open_url)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BookLookupError(
            "timeout",
            "连接 Google Books 失败，请检查网络或代理后重试。",
            open_url,
        )
    return {"candidates": parse_google_books_response(raw, metadata, isbn), "open_url": open_url}


def parse_google_books_response(
    raw: str, requested: Mapping[str, object], isbn: str
) -> List[Dict[str, object]]:
    try:
        data = json.loads(raw)
    except ValueError:
        raise BookLookupError("site_changed", "Google Books 返回内容无法解析。")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    candidates: List[Dict[str, object]] = []
    for item in items:
        candidate = _candidate_from_item(item, requested, isbn)
        if candidate:
            candidates.append(candidate)
    # 最可信的排在最前：ISBN 命中优先，其次匹配分。
    candidates.sort(key=lambda c: -float(c["match"]["score"]))
    return candidates


def _candidate_from_item(
    item: object, requested: Mapping[str, object], isbn: str
) -> Optional[Dict[str, object]]:
    if not isinstance(item, Mapping):
        return None
    info = item.get("volumeInfo")
    if not isinstance(info, Mapping):
        return None
    title = _clean(info.get("title"))
    if not title:
        return None
    subtitle = _clean(info.get("subtitle"))
    full_title = f"{title}: {subtitle}" if subtitle else title
    authors = info.get("authors")
    author = ", ".join(_clean(a) for a in authors if _clean(a)) if isinstance(authors, list) else ""
    candidate_isbns = _isbns_of(info)
    metadata: Dict[str, str] = {"title": full_title}
    if author:
        metadata["author"] = author
    publisher = _clean(info.get("publisher"))
    if publisher:
        metadata["publisher"] = publisher
    year = _year_of(info.get("publishedDate"))
    if year:
        metadata["publish_year"] = year
    resolved_isbn = isbn if isbn in candidate_isbns else (candidate_isbns[0] if candidate_isbns else "")
    if resolved_isbn:
        metadata["isbn"] = resolved_isbn
    record_url = _clean(info.get("canonicalVolumeLink")) or _clean(info.get("infoLink"))
    match = _match(requested, full_title, author, candidate_isbns, isbn)
    evidence = {
        field: {
            "source": "google_books",
            "source_page": None,
            "evidence_text": f"Google Books：{full_title}",
            "value": value,
            "record_url": record_url,
        }
        for field, value in metadata.items()
    }
    return {
        "metadata": metadata,
        "match": match,
        "record_url": record_url,
        "publish_date": _clean(info.get("publishedDate")),
        "evidence": evidence,
    }


def _isbns_of(info: Mapping[str, object]) -> List[str]:
    identifiers = info.get("industryIdentifiers")
    if not isinstance(identifiers, list):
        return []
    result: List[str] = []
    for entry in identifiers:
        if isinstance(entry, Mapping) and str(entry.get("type", "")).startswith("ISBN"):
            value = _normalize_isbn(entry.get("identifier"))
            if value:
                result.append(value)
    return result


def _match(
    requested: Mapping[str, object],
    candidate_title: str,
    candidate_author: str,
    candidate_isbns: List[str],
    isbn: str,
) -> Dict[str, object]:
    reasons: List[str] = []
    conflicts: List[str] = []
    score = 0.0
    if isbn and isbn in candidate_isbns:
        return {"level": "high", "score": 0.98, "reasons": ["ISBN 一致"], "conflicts": []}

    requested_title = _compact(requested.get("title"))
    candidate_key = _compact(candidate_title)
    if requested_title and candidate_key:
        if requested_title == candidate_key or candidate_key.startswith(requested_title):
            score += 0.75
            reasons.append("书名一致")
        else:
            ratio = difflib.SequenceMatcher(None, requested_title, candidate_key).ratio()
            if ratio >= 0.72:
                score += 0.5 * ratio
                reasons.append(f"书名相似 {round(ratio * 100)}%")
            else:
                conflicts.append("书名差异较大")

    requested_author = _compact(requested.get("author"))
    if requested_author and candidate_author:
        if requested_author in _compact(candidate_author) or _compact(candidate_author) in requested_author:
            score += 0.2
            reasons.append("作者有交集")
    if candidate_isbns and not isbn:
        score += 0.03

    level = "high" if score >= 0.9 else ("medium" if score >= 0.5 else "low")
    return {"level": level, "score": round(min(score, 0.97), 3), "reasons": reasons, "conflicts": conflicts}
