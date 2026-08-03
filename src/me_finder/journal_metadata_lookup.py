"""Best-effort CNKI journal metadata lookup with conservative matching.

Only public bibliographic pages are requested.  The client never downloads
attachments, bypasses verification, disables TLS checks, or persists cookies.
"""

from __future__ import annotations

import difflib
import http.cookiejar
import json
import re
import socket
import ssl
import unicodedata
from html import unescape
from html.parser import HTMLParser
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .bibliographic_metadata import normalize_doi, normalize_issn


CNKI_SEARCH_ENDPOINT = "https://oversea.cnki.net/kns8s/brief/grid?language=CHS"
CNKI_SEARCH_PAGE = "https://oversea.cnki.net/kns8s/search"
CNKI_ALLOWED_HOST = "oversea.cnki.net"
CNKI_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CNKI_TIMEOUT_SECONDS = 12.0

_JOURNAL_DATABASE_CODES = (
    "ON8XK5WL,B7ZYGRCM,BT8YKI4I,TBRPZP83,I8IOAWAD,HT3U9UVL,"
    "BHWTLLXZ,SZU0GLDC,IAF5Y951"
)
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_YEAR_RE = re.compile(r"(?:18|19|20)\d{2}")
_PUBLICATION_RE = re.compile(
    r"(?P<year>(?:18|19|20)\d{2})\s*[,，]?\s*"
    r"(?:(?P<volume>\d+)\s*)?(?:[（(]\s*(?P<issue>[^()（）]+?)\s*[)）])?"
)


class CNKILookupError(RuntimeError):
    """A user-actionable lookup failure with a stable machine code."""

    def __init__(self, code: str, message: str, *, open_url: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.open_url = open_url


def _clean_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", unescape(str(value or "")))
    text = _INVISIBLE_RE.sub("", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)


def _clean_author_name(value: object) -> str:
    """Normalize one ``#authorpart > span`` into a bare author name.

    CNKI attaches affiliation markers (superscript digits, ``*``, ``△``) to each
    name.  Strip those trailing/leading markers so authors join cleanly; drop
    entries that are only markers (e.g. a standalone affiliation footnote span).
    """

    text = _clean_text(value)
    text = re.sub(r"^[\s,，、;；]+", "", text)
    text = re.sub(r"[\s0-9,，;；．.\*△＃#†‡]+$", "", text).strip()
    if not text or not re.search(r"[㐀-鿿A-Za-z]", text):
        return ""
    return text


def _compact_key(value: object) -> str:
    return "".join(
        char.casefold()
        for char in _clean_text(value)
        if char.isalnum() or "\u3400" <= char <= "\u9fff"
    )


def _author_tokens(value: object) -> set[str]:
    return {
        _compact_key(part)
        for part in re.split(r"[、,，;；/\s]+", _clean_text(value))
        if _compact_key(part)
    }


def cnki_open_search_url(metadata: Mapping[str, object]) -> str:
    doi = normalize_doi(metadata.get("doi"))
    keyword = doi or _clean_text(metadata.get("title"))
    order = "DOI" if doi else "TI"
    return CNKI_SEARCH_PAGE + "?" + urlencode(
        {"classid": "R0DPFOXP", "kw": keyword, "korder": order, "language": "CHS"}
    )


def _candidate_match(
    requested: Mapping[str, object], candidate: Mapping[str, object], *, doi_query: bool
) -> Dict[str, object]:
    reasons: List[str] = []
    conflicts: List[str] = []
    score = 0.0
    requested_title = _compact_key(requested.get("title"))
    candidate_title = _compact_key(candidate.get("title"))
    title_ratio = (
        difflib.SequenceMatcher(None, requested_title, candidate_title).ratio()
        if requested_title and candidate_title
        else 0.0
    )
    # The CNKI DOI form sometimes falls back to a journal's newest issue while
    # still returning HTTP 200.  A result appearing after a DOI query is not
    # evidence that its DOI matches; only the visible title/author/year (or a
    # DOI read from the detail page) may raise its score.
    if requested_title and candidate_title:
        if requested_title == candidate_title:
            score += 0.75
            reasons.append("篇名一致")
        elif title_ratio >= 0.72:
            score += 0.48 * title_ratio
            reasons.append(f"篇名相似 {round(title_ratio * 100)}%")
        else:
            conflicts.append("篇名差异较大")
    requested_authors = _author_tokens(requested.get("author"))
    candidate_authors = _author_tokens(candidate.get("author"))
    if requested_authors and candidate_authors:
        if requested_authors & candidate_authors:
            score += 0.17
            reasons.append("作者有交集")
        else:
            conflicts.append("作者不一致")
    requested_year = _clean_text(requested.get("publish_year"))
    candidate_year = _clean_text(candidate.get("publish_year"))
    if requested_year and candidate_year:
        if requested_year == candidate_year:
            score += 0.08
            reasons.append("年份一致")
        else:
            conflicts.append("年份不一致")
            score -= 0.18
    score = max(0.0, min(1.0, score))
    if requested_title and requested_title == candidate_title and not conflicts:
        level = "high" if (not requested_authors or requested_authors & candidate_authors) else "medium"
    elif score >= 0.62 and not conflicts:
        level = "medium"
    else:
        level = "low"
    return {
        "level": level,
        "score": round(score, 4),
        "reasons": reasons,
        "conflicts": conflicts,
    }


class _SearchResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.in_results_table = False
        self.current_row: Optional[Dict[str, object]] = None
        self.current_cell = ""
        self.current_text: List[str] = []
        self.in_title_anchor = False
        self.title_text: List[str] = []
        self.skip_depth = 0
        self.rows: List[Dict[str, str]] = []
        self.table_found = False
        self.verification_found = False

    @staticmethod
    def _attrs(attrs: Sequence[Tuple[str, Optional[str]]]) -> Dict[str, str]:
        return {str(key): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        values = self._attrs(attrs)
        classes = set(values.get("class", "").split())
        if "verifycode" in classes or values.get("id") in {"vericode", "checkCodeBtn"}:
            self.verification_found = True
        if tag == "table" and "result-table-list" in classes:
            self.in_results_table = True
            self.table_depth = 1
            self.table_found = True
            return
        if self.in_results_table and tag == "table":
            self.table_depth += 1
        if not self.in_results_table:
            return
        if "hrc" in classes:
            self.skip_depth = 1
            return
        if self.skip_depth:
            self.skip_depth += 1
            return
        if tag == "tr":
            self.current_row = {}
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = next(iter(classes), "")
            self.current_text = []
        elif self.current_row is not None and tag == "a" and self.current_cell == "name":
            if "fz14" in classes and values.get("href"):
                self.current_row["record_url"] = values["href"]
                self.in_title_anchor = True
                self.title_text = []
        elif self.current_row is not None and tag == "input" and self.current_cell == "seq":
            if values.get("value"):
                self.current_row["record_id"] = values["value"]
        if self.current_row is not None and self.current_cell == "operat":
            if values.get("data-dbname"):
                self.current_row["dbname"] = values["data-dbname"]
            if values.get("data-filename"):
                self.current_row["filename"] = values["data-filename"]

    def handle_endtag(self, tag: str) -> None:
        if not self.in_results_table:
            return
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag in {"td", "th"} and self.current_row is not None and self.current_cell:
            text = _clean_text(" ".join(self.current_text))
            if text and self.current_cell != "name":
                self.current_row[self.current_cell] = text
            self.current_cell = ""
            self.current_text = []
        elif tag == "tr" and self.current_row is not None:
            title = _clean_text(self.current_row.get("name"))
            record_url = urljoin("https://oversea.cnki.net", str(self.current_row.get("record_url") or ""))
            if title and record_url:
                self.current_row["title"] = title
                self.current_row["author"] = _clean_text(self.current_row.get("author"))
                self.current_row["journal_name"] = _clean_text(self.current_row.get("source"))
                self.current_row["publish_date"] = _clean_text(self.current_row.get("date"))
                self.current_row["database_label"] = _clean_text(self.current_row.get("data"))
                self.current_row["record_url"] = record_url
                self.rows.append({key: str(value) for key, value in self.current_row.items()})
            self.current_row = None
        elif tag == "a" and self.in_title_anchor and self.current_row is not None:
            title = _clean_text(" ".join(self.title_text))
            if title:
                self.current_row["name"] = title
            self.in_title_anchor = False
            self.title_text = []
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_results_table = False

    def handle_data(self, data: str) -> None:
        if self.in_results_table and self.current_row is not None and self.current_cell and not self.skip_depth:
            self.current_text.append(data)
            if self.in_title_anchor:
                self.title_text.append(data)


class _DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_depth = 0
        self.author_depth = 0
        self.top_tip_depth = 0
        self.anchor_depth = 0
        self.title_parts: List[str] = []
        self.author_parts: List[str] = []
        self.author_names: List[str] = []
        self.author_span_depth = 0
        self.current_author: List[str] = []
        self.top_tip_anchors: List[str] = []
        self.current_anchor: List[str] = []
        self.hidden: Dict[str, str] = {}
        self.verification_found = False

    @staticmethod
    def _attrs(attrs: Sequence[Tuple[str, Optional[str]]]) -> Dict[str, str]:
        return {str(key): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        values = self._attrs(attrs)
        classes = set(values.get("class", "").split())
        if "verifycode" in classes or values.get("id") in {"vericode", "checkCodeBtn"}:
            self.verification_found = True
        if tag == "input" and values.get("id") and values.get("value"):
            self.hidden[values["id"]] = values["value"]
        if "title-one" in classes:
            self.title_depth = 1
        elif self.title_depth:
            self.title_depth += 1
        if values.get("id") == "authorpart":
            self.author_depth = 1
        elif self.author_depth:
            self.author_depth += 1
        # 逐个抓取 #authorpart > span 作为单个作者：作者名常带上标机构编号，
        # 分 span 收集才能干净地按顿号拼接，而不是把所有文本糊成一串。
        if self.author_depth:
            if self.author_span_depth:
                self.author_span_depth += 1
            elif tag == "span" and self.author_depth == 2:
                self.author_span_depth = 1
                self.current_author = []
        if "top-tip" in classes:
            self.top_tip_depth = 1
        elif self.top_tip_depth:
            self.top_tip_depth += 1
        if tag == "a" and self.top_tip_depth:
            self.anchor_depth = 1
            self.current_anchor = []
        elif self.anchor_depth:
            self.anchor_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.anchor_depth:
            self.anchor_depth -= 1
            if self.anchor_depth == 0:
                text = _clean_text(" ".join(self.current_anchor))
                if text:
                    self.top_tip_anchors.append(text)
                self.current_anchor = []
        if self.author_span_depth:
            self.author_span_depth -= 1
            if self.author_span_depth == 0:
                name = _clean_author_name(" ".join(self.current_author))
                if name:
                    self.author_names.append(name)
                self.current_author = []
        if self.title_depth:
            self.title_depth -= 1
        if self.author_depth:
            self.author_depth -= 1
        if self.top_tip_depth:
            self.top_tip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if self.author_span_depth:
            self.current_author.append(data)
        if self.author_depth:
            self.author_parts.append(data)
        if self.anchor_depth:
            self.current_anchor.append(data)


def _text_fragment(fragment: str) -> str:
    class Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: List[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    parser = Parser()
    parser.feed(fragment)
    return _clean_text(" ".join(parser.parts))


def parse_cnki_search_results(
    html_text: str, requested: Mapping[str, object], *, doi_query: bool = False
) -> List[Dict[str, object]]:
    parser = _SearchResultsParser()
    parser.feed(html_text)
    if parser.verification_found or "知网节超时验证" in html_text or ">captcha<" in html_text.lower():
        raise CNKILookupError("verification_required", "知网要求浏览器验证，请打开知网页面后重试或粘贴引用文字。")
    candidates: List[Dict[str, object]] = []
    for row in parser.rows:
        database_label = row.get("database_label", "")
        if database_label and "期刊" not in database_label and "辑刊" not in database_label:
            continue
        parsed_url = urlparse(row.get("record_url", ""))
        if parsed_url.scheme != "https" or parsed_url.hostname != CNKI_ALLOWED_HOST:
            continue
        year_match = _YEAR_RE.search(row.get("publish_date", ""))
        metadata = {
            "document_type": "journal_article",
            "title": row.get("title", ""),
            "author": row.get("author", ""),
            "journal_name": row.get("journal_name", ""),
            "publish_year": year_match.group(0) if year_match else "",
        }
        metadata = {key: value for key, value in metadata.items() if value not in (None, "")}
        evidence = {
            field: {
                "source": "cnki_search_result",
                "source_page": None,
                "evidence_text": str(value),
                "value": str(value),
                "record_url": row["record_url"],
            }
            for field, value in metadata.items()
            if field != "document_type"
        }
        candidates.append(
            {
                "provider": "cnki",
                "record_id": row.get("record_id", ""),
                "record_url": row["record_url"],
                "metadata": metadata,
                "evidence": evidence,
                "database_label": database_label or "期刊",
                "publish_date": row.get("publish_date", ""),
                "match": _candidate_match(requested, metadata, doi_query=doi_query),
            }
        )
    candidates.sort(key=lambda item: float((item.get("match") or {}).get("score") or 0), reverse=True)
    no_content = re.search(r"class=[\"'][^\"']*no-content[^\"']*[\"'][^>]*value=[\"']([^\"']*)[\"']", html_text)
    if no_content and _clean_text(no_content.group(1)):
        raise CNKILookupError("site_changed", "知网拒绝了当前检索参数，页面接口可能已变化。")
    if not parser.table_found and "no-content" not in html_text:
        raise CNKILookupError("site_changed", "知网检索页面结构已变化，当前无法自动解析。")
    return candidates[:20]


def parse_cnki_detail_page(html_text: str, record_url: str) -> Tuple[Dict[str, str], Dict[str, object]]:
    parser = _DetailParser()
    parser.feed(html_text)
    if parser.verification_found or "知网节超时验证" in html_text or ">captcha<" in html_text.lower():
        raise CNKILookupError("verification_required", "知网要求浏览器验证，请打开记录页后粘贴引用文字。")
    title = _clean_text(" ".join(parser.title_parts))
    # 优先用分 span 抓到的作者列表（可干净拼接多作者、去掉机构上标）；
    # 若结构异常没抓到 span，退回整块 #authorpart 文本。
    author = "、".join(parser.author_names) if parser.author_names else _clean_text(" ".join(parser.author_parts))
    journal_name = _clean_text(parser.top_tip_anchors[0]).rstrip(".。").strip() if parser.top_tip_anchors else ""
    publication = _clean_text(parser.top_tip_anchors[1]) if len(parser.top_tip_anchors) > 1 else ""
    publication_match = _PUBLICATION_RE.search(publication)
    page_range = _clean_text(parser.hidden.get("prite-page-num"))
    doi_match = re.search(
        r"<span[^>]*class=[\"'][^\"']*rowtit[^\"']*[\"'][^>]*>\s*DOI\s*[:：]?\s*</span>\s*<p[^>]*>(.*?)</p>",
        html_text,
        re.IGNORECASE | re.DOTALL,
    )
    issn_match = re.search(
        r"<span[^>]*class=[\"'][^\"']*rowtit[^\"']*[\"'][^>]*>\s*ISSN\s*[:：]?\s*</span>\s*<p[^>]*>(.*?)</p>",
        html_text,
        re.IGNORECASE | re.DOTALL,
    )
    doi = normalize_doi(_text_fragment(doi_match.group(1))) if doi_match else None
    issn = normalize_issn(_text_fragment(issn_match.group(1))) if issn_match else None
    metadata: Dict[str, str] = {"document_type": "journal_article"}
    for field, value in (("title", title), ("author", author), ("journal_name", journal_name), ("page_range", page_range)):
        if value:
            metadata[field] = value
    if publication_match:
        metadata["publish_year"] = publication_match.group("year")
        if publication_match.group("volume"):
            metadata["volume"] = publication_match.group("volume")
        if publication_match.group("issue"):
            metadata["issue"] = publication_match.group("issue").strip()
    if doi:
        metadata["doi"] = doi
    if issn:
        metadata["issn"] = issn
    if not title or not journal_name or not metadata.get("publish_year"):
        raise CNKILookupError("site_changed", "知网记录页缺少可核验的篇名、刊名或年份。")
    evidence = {
        field: {
            "source": "cnki_lookup",
            "source_page": None,
            "evidence_text": value,
            "value": value,
            "record_url": record_url,
        }
        for field, value in metadata.items()
        if field != "document_type"
    }
    return metadata, evidence


class CNKIClient:
    def __init__(self, *, timeout: float = CNKI_TIMEOUT_SECONDS, opener=None) -> None:
        self.timeout = float(timeout)
        self.opener = opener or build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    @staticmethod
    def _search_payload(metadata: Mapping[str, object]) -> Tuple[bytes, bool]:
        doi = normalize_doi(metadata.get("doi"))
        title = _clean_text(metadata.get("title"))
        if doi:
            field, operator, value, label, key = "DOI", 9, doi, "DOI", "doi"
        elif title:
            field, operator, value, label, key = "TI", 2, title, "篇名", "title"
        else:
            raise CNKILookupError("invalid_query", "自动查询至少需要篇名或 DOI。")
        if len(value) > 300 or any(ord(char) < 32 for char in value):
            raise CNKILookupError("invalid_query", "篇名或 DOI 过长，无法安全查询。")
        query = {
            "Platform": "",
            "Resource": "CROSSDB",
            "Classid": "R0DPFOXP",
            "Products": "",
            "QNode": {
                "QGroup": [
                    {
                        "Key": "Subject",
                        "Title": "",
                        "Logic": 0,
                        "Items": [
                            {
                                "Key": key,
                                "Title": label,
                                "Logic": 0,
                                "Field": field,
                                "Operator": operator,
                                "Value": value,
                                "Value2": "",
                            }
                        ],
                        "ChildItems": [],
                    },
                    {"Key": "ControlGroup", "Title": "", "Logic": 0, "Items": [], "ChildItems": []},
                ]
            },
            "ExScope": "0",
            "SearchType": "2",
            "Rlang": "CHINESE",
            "KuaKuCode": _JOURNAL_DATABASE_CODES,
        }
        form: Dict[str, object] = {
            "boolSearch": "true",
            "QueryJson": query,
            "pageNum": "1",
            "pageSize": "20",
            "sortField": "",
            "sortType": "",
            "dstyle": "listmode",
            "boolSortSearch": "false",
            "sentenceSearch": "false",
            "productStr": "",
            "aside": "",
            "searchFrom": "",
            "manageId": "",
            "subject": "",
            "turnpage": "",
            "CurPage": "1",
            "language": "CHS",
        }
        encoded = {
            name: json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if isinstance(item, (dict, list))
            else str(item)
            for name, item in form.items()
        }
        return urlencode(encoded).encode("utf-8"), bool(doi)

    def _request(self, request: Request, *, open_url: str) -> str:
        for attempt in range(2):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    length = response.headers.get("Content-Length")
                    if length and int(length) > CNKI_MAX_RESPONSE_BYTES:
                        raise CNKILookupError("site_changed", "知网页面响应异常过大，已停止解析。", open_url=open_url)
                    body = response.read(CNKI_MAX_RESPONSE_BYTES + 1)
                    if len(body) > CNKI_MAX_RESPONSE_BYTES:
                        raise CNKILookupError("site_changed", "知网页面响应异常过大，已停止解析。", open_url=open_url)
                    charset = response.headers.get_content_charset() or "utf-8"
                    return body.decode(charset, "replace")
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise CNKILookupError("verification_required", "知网要求浏览器验证。", open_url=open_url) from exc
                if exc.code == 429:
                    raise CNKILookupError("rate_limited", "知网请求过于频繁，请稍后再试。", open_url=open_url) from exc
                if exc.code in {502, 503, 504} and attempt == 0:
                    continue
                raise CNKILookupError("provider_unavailable", f"知网暂时不可用（HTTP {exc.code}）。", open_url=open_url) from exc
            except (TimeoutError, socket.timeout) as exc:
                if attempt == 0:
                    continue
                raise CNKILookupError("timeout", "连接知网超时。", open_url=open_url) from exc
            except URLError as exc:
                reason = exc.reason
                if isinstance(reason, ssl.SSLCertVerificationError):
                    raise CNKILookupError("tls_error", "知网 TLS 证书校验失败，未降低安全设置。", open_url=open_url) from exc
                if isinstance(reason, (TimeoutError, socket.timeout)) and attempt == 0:
                    continue
                raise CNKILookupError("offline", "无法连接知网，请检查网络。", open_url=open_url) from exc
            except ssl.SSLError as exc:
                raise CNKILookupError("tls_error", "知网 TLS 连接失败，未降低安全设置。", open_url=open_url) from exc
        raise CNKILookupError("provider_unavailable", "知网暂时不可用。", open_url=open_url)

    def search(self, metadata: Mapping[str, object]) -> Dict[str, object]:
        open_url = cnki_open_search_url(metadata)
        payload, doi_query = self._search_payload(metadata)
        candidates = self._search_once(metadata, payload, doi_query=doi_query, open_url=open_url)
        query_type = "doi" if doi_query else "title"
        query_notice = ""

        if doi_query:
            requested_title = _compact_key(metadata.get("title"))
            if requested_title:
                # Never expose CNKI's unrelated default list as DOI results.
                # Keep only candidates corroborated by the user's existing
                # bibliographic fields.  If none survive, retry once by title.
                corroborated = [
                    candidate
                    for candidate in candidates
                    if str((candidate.get("match") or {}).get("level") or "") in {"high", "medium"}
                ]
                if corroborated:
                    candidates = corroborated
                else:
                    title_metadata = dict(metadata)
                    title_metadata["doi"] = ""
                    fallback_url = cnki_open_search_url(title_metadata)
                    fallback_payload, _ = self._search_payload(title_metadata)
                    candidates = self._search_once(
                        title_metadata,
                        fallback_payload,
                        doi_query=False,
                        open_url=fallback_url,
                    )
                    open_url = fallback_url
                    query_type = "title_fallback"
                    query_notice = "DOI 检索未返回可信记录，已自动改用篇名"
            else:
                # With DOI as the only input, a single search hit still needs
                # verification against the DOI visible on its detail page.
                # Multiple hits are CNKI's default-list behaviour and are
                # rejected without issuing a burst of detail requests.
                verified: List[Dict[str, object]] = []
                if len(candidates) == 1:
                    detail = self.fetch_candidate(candidates[0])
                    requested_doi = normalize_doi(metadata.get("doi"))
                    returned_doi = normalize_doi((detail.get("metadata") or {}).get("doi"))
                    if requested_doi and returned_doi == requested_doi:
                        candidate = dict(candidates[0])
                        candidate["metadata"] = detail["metadata"]
                        candidate["evidence"] = detail["evidence"]
                        candidate["match"] = {
                            "level": "high",
                            "score": 1.0,
                            "reasons": ["DOI 一致"],
                            "conflicts": [],
                        }
                        verified.append(candidate)
                candidates = verified
                if not candidates:
                    query_notice = "知网 DOI 检索未返回可核验的精确记录"

        result = {
            "provider": "cnki",
            "query_type": query_type,
            "open_url": open_url,
            "candidates": candidates,
        }
        if query_notice:
            result["query_notice"] = query_notice
        return result

    def _search_once(
        self,
        metadata: Mapping[str, object],
        payload: bytes,
        *,
        doi_query: bool,
        open_url: str,
    ) -> List[Dict[str, object]]:
        request = Request(
            CNKI_SEARCH_ENDPOINT,
            data=payload,
            method="POST",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0",
                "Accept": "text/html, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://oversea.cnki.net",
                "Referer": open_url,
            },
        )
        html_text = self._request(request, open_url=open_url)
        return parse_cnki_search_results(html_text, metadata, doi_query=doi_query)

    def fetch_candidate(self, candidate: Mapping[str, object]) -> Dict[str, object]:
        record_url = str(candidate.get("record_url") or "").strip()
        parsed = urlparse(record_url)
        if (
            len(record_url) > 4096
            or parsed.scheme != "https"
            or parsed.hostname != CNKI_ALLOWED_HOST
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/kcms2/article/abstract"
        ):
            raise CNKILookupError("invalid_candidate", "知网候选地址无效。")
        request = Request(
            record_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": CNKI_SEARCH_PAGE,
            },
        )
        html_text = self._request(request, open_url=record_url)
        metadata, evidence = parse_cnki_detail_page(html_text, record_url)
        return {
            "provider": "cnki",
            "record_url": record_url,
            "metadata": metadata,
            "evidence": evidence,
        }


def lookup_cnki_journal(metadata: Mapping[str, object]) -> Dict[str, object]:
    return CNKIClient().search(metadata)


def fetch_cnki_candidate(candidate: Mapping[str, object]) -> Dict[str, object]:
    return CNKIClient().fetch_candidate(candidate)
