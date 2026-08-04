"""Crossref lookup for foreign-language journal articles.

DOI is the precise path (``/works/{doi}`` returns the authoritative record);
without a DOI it falls back to a bibliographic query.  Crossref is a clean,
keyless JSON API and is usually reachable from mainland China without a proxy.
Parsing is JSON-only and every failure degrades to a structured error so the
caller can show a message instead of blocking.
"""

from __future__ import annotations

import difflib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Mapping, Optional, Tuple

CROSSREF_ENDPOINT = "https://api.crossref.org/works"
_TIMEOUT_SECONDS = 12
_MAX_RESULTS = 5
# Crossref 礼貌池建议带联系方式；不写入任何个人邮箱，仅标识客户端。
_USER_AGENT = "MEFinder/0.3 (bibliographic lookup; https://github.com)"


class CrossrefLookupError(Exception):
    """A recoverable Crossref lookup failure with a stable machine code."""

    def __init__(self, code: str, message: str, open_url: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.open_url = open_url


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_doi(value: object) -> str:
    text = _clean(value)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    return text.strip().strip(".")


def _compact(value: object) -> str:
    return re.sub(r"[^0-9a-z]+", "", _clean(value).lower())


def _first_list_item(value: object) -> str:
    if isinstance(value, list):
        for item in value:
            if _clean(item):
                return _clean(item)
        return ""
    return _clean(value)


def _request_json(url: str, open_url: str) -> object:
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 429:
            raise CrossrefLookupError("rate_limited", "Crossref 暂时限流，请稍后重试。", open_url)
        raise CrossrefLookupError("http_error", f"Crossref 请求失败（HTTP {exc.code}）。", open_url)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CrossrefLookupError("timeout", "连接 Crossref 失败，请检查网络后重试。", open_url)
    try:
        return json.loads(raw)
    except ValueError:
        raise CrossrefLookupError("site_changed", "Crossref 返回内容无法解析。", open_url)


def lookup_crossref(metadata: Mapping[str, object]) -> Dict[str, object]:
    """Query Crossref by DOI (precise) or bibliographic query and return candidates."""

    doi = _normalize_doi(metadata.get("doi"))
    title = _clean(metadata.get("title"))
    if doi:
        open_url = f"https://doi.org/{doi}"
        payload = _request_json(f"{CROSSREF_ENDPOINT}/{urllib.parse.quote(doi)}", open_url)
        work = payload.get("message") if isinstance(payload, dict) else None
        candidate = _candidate_from_work(work, metadata, doi) if isinstance(work, Mapping) else None
        return {"candidates": [candidate] if candidate else [], "open_url": open_url}
    if not title:
        raise CrossrefLookupError("invalid_query", "请先填写 DOI 或篇名。")
    params = {"rows": _MAX_RESULTS, "select": "DOI,title,author,container-title,volume,issue,page,issued,published-print,published-online,ISSN,type"}
    params["query.bibliographic"] = title
    author = _clean(metadata.get("author"))
    if author:
        params["query.author"] = author
    open_url = "https://search.crossref.org/?q=" + urllib.parse.quote(title)
    payload = _request_json(f"{CROSSREF_ENDPOINT}?{urllib.parse.urlencode(params)}", open_url)
    message = payload.get("message") if isinstance(payload, dict) else None
    items = message.get("items") if isinstance(message, Mapping) else None
    if not isinstance(items, list):
        return {"candidates": [], "open_url": open_url}
    candidates = [c for c in (_candidate_from_work(w, metadata, "") for w in items) if c]
    candidates.sort(key=lambda c: -float(c["match"]["score"]))
    return {"candidates": candidates, "open_url": open_url}


def parse_crossref_work(payload_json: str, metadata: Mapping[str, object], doi: str) -> Optional[Dict[str, object]]:
    """Parse a single ``/works/{doi}`` JSON response (used by tests)."""

    try:
        payload = json.loads(payload_json)
    except ValueError:
        raise CrossrefLookupError("site_changed", "Crossref 返回内容无法解析。")
    work = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(work, Mapping):
        return None
    return _candidate_from_work(work, metadata, doi)


def parse_crossref_query(payload_json: str, metadata: Mapping[str, object]) -> List[Dict[str, object]]:
    """Parse a ``?query`` work-list JSON response (used by tests)."""

    try:
        payload = json.loads(payload_json)
    except ValueError:
        raise CrossrefLookupError("site_changed", "Crossref 返回内容无法解析。")
    message = payload.get("message") if isinstance(payload, dict) else None
    items = message.get("items") if isinstance(message, Mapping) else None
    if not isinstance(items, list):
        return []
    candidates = [c for c in (_candidate_from_work(w, metadata, "") for w in items) if c]
    candidates.sort(key=lambda c: -float(c["match"]["score"]))
    return candidates


def _author_names(work: Mapping[str, object]) -> str:
    authors = work.get("author")
    if not isinstance(authors, list):
        return ""
    names: List[str] = []
    for entry in authors:
        if not isinstance(entry, Mapping):
            continue
        given = _clean(entry.get("given"))
        family = _clean(entry.get("family"))
        full = (f"{given} {family}".strip()) or _clean(entry.get("name"))
        if full:
            names.append(full)
    return ", ".join(names)


def _work_year(work: Mapping[str, object]) -> str:
    for key in ("published-print", "published-online", "issued", "published"):
        block = work.get(key)
        if isinstance(block, Mapping):
            parts = block.get("date-parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                year = _clean(parts[0][0])
                if re.fullmatch(r"\d{4}", year):
                    return year
    return ""


def _candidate_from_work(
    work: object, requested: Mapping[str, object], doi: str
) -> Optional[Dict[str, object]]:
    if not isinstance(work, Mapping):
        return None
    title = _first_list_item(work.get("title"))
    if not title:
        return None
    resolved_doi = _normalize_doi(work.get("DOI")) or doi
    metadata: Dict[str, str] = {"document_type": "journal_article", "title": title}
    author = _author_names(work)
    if author:
        metadata["author"] = author
    journal = _first_list_item(work.get("container-title"))
    if journal:
        metadata["journal_name"] = journal
    for field, key in (("volume", "volume"), ("issue", "issue"), ("page_range", "page")):
        value = _clean(work.get(key))
        if value:
            metadata[field] = value
    year = _work_year(work)
    if year:
        metadata["publish_year"] = year
    if resolved_doi:
        metadata["doi"] = resolved_doi
    issn = _first_list_item(work.get("ISSN"))
    if issn:
        metadata["issn"] = issn
    record_url = f"https://doi.org/{resolved_doi}" if resolved_doi else ""
    match = _match(requested, title, author, resolved_doi, doi)
    evidence = {
        field: {
            "source": "crossref",
            "source_page": None,
            "evidence_text": f"Crossref：{title}",
            "value": value,
            "record_url": record_url,
        }
        for field, value in metadata.items()
        if field != "document_type"
    }
    return {
        "metadata": metadata,
        "match": match,
        "record_url": record_url,
        "publish_date": year,
        "evidence": evidence,
    }


def _match(
    requested: Mapping[str, object],
    candidate_title: str,
    candidate_author: str,
    candidate_doi: str,
    requested_doi: str,
) -> Dict[str, object]:
    if requested_doi and candidate_doi and _compact(requested_doi) == _compact(candidate_doi):
        return {"level": "high", "score": 0.98, "reasons": ["DOI 一致"], "conflicts": []}
    reasons: List[str] = []
    conflicts: List[str] = []
    score = 0.0
    requested_title = _compact(requested.get("title"))
    candidate_key = _compact(candidate_title)
    if requested_title and candidate_key:
        if requested_title == candidate_key or candidate_key.startswith(requested_title):
            score += 0.75
            reasons.append("篇名一致")
        else:
            ratio = difflib.SequenceMatcher(None, requested_title, candidate_key).ratio()
            if ratio >= 0.72:
                score += 0.5 * ratio
                reasons.append(f"篇名相似 {round(ratio * 100)}%")
            else:
                conflicts.append("篇名差异较大")
    requested_author = _compact(requested.get("author"))
    if requested_author and candidate_author:
        if requested_author in _compact(candidate_author) or _compact(candidate_author) in requested_author:
            score += 0.2
            reasons.append("作者有交集")
    level = "high" if score >= 0.9 else ("medium" if score >= 0.5 else "low")
    return {"level": level, "score": round(min(score, 0.97), 3), "reasons": reasons, "conflicts": conflicts}
