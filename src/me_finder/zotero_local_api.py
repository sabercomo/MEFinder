"""Read-only client for the Zotero desktop Local API (``/api/`` on 127.0.0.1).

Only the local HTTP API is used: no Zotero Web API, no ``zotero.sqlite``.
Behaviour follows https://www.zotero.org/support/dev/web_api/v3/local_api :

* Base URL ``http://localhost:23119/api/``; the user enables it under
  Settings → Advanced → "Allow other applications on this computer to
  communicate with Zotero". Disabled → ``403 Forbidden``.
* Reads need no authentication; only API version 3 exists.
* Results are not paginated by default, but ``limit``/``start`` still work.
  This client always pages explicitly and checks ``Total-Results`` so an
  incomplete listing is detected instead of silently treated as complete.
* ``items/<key>/file/view/url`` returns the attachment's ``file://`` URL as
  plain text.
* Zotero 10+ sends ``Zotero-Server-ID`` and reports *local* object versions;
  earlier releases (Zotero 7) report synced versions that are ``0`` for
  never-synced objects and do not change on local edits. Callers must only
  trust versions when :attr:`ZoteroProbe.local_versions` is true.

Every method raises :class:`ZoteroUnavailable` or :class:`ZoteroIncomplete`
rather than returning a partial result, so the sync can refuse to remove
anything after a failed read.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import __version__


DEFAULT_ZOTERO_API_BASE = "http://127.0.0.1:23119/api"
MY_LIBRARY_PREFIX = "users/0"
PAGE_SIZE = 100
ITEM_KEY_BATCH = 50
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ZoteroUnavailable(RuntimeError):
    """Zotero is not reachable, the Local API is off, or a request failed."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


class ZoteroIncomplete(RuntimeError):
    """A listing could not be read completely (paging or count mismatch)."""


@dataclass(frozen=True)
class ZoteroProbe:
    """Outcome of a connection check; ``state`` is one of
    ``connected`` / ``api_disabled`` / ``not_running`` / ``unsupported`` / ``error``."""

    state: str
    message: str
    server_id: Optional[str] = None
    api_version: Optional[str] = None
    zotero_version: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    @property
    def local_versions(self) -> bool:
        # Zotero 10+ identifies itself with Zotero-Server-ID and keeps local
        # versions; without it versions are synced ones (see module docstring).
        return bool(self.server_id)


Response = Tuple[int, Mapping[str, str], bytes]
Transport = Callable[[str, Mapping[str, str], float], Response]


def _urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> Response:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    # Never route loopback traffic through a system proxy.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return None


class ZoteroLocalClient:
    """Tiny GET-only client for ``users/0`` ("My Library")."""

    def __init__(
        self,
        base_url: str = DEFAULT_ZOTERO_API_BASE,
        *,
        timeout: float = 10.0,
        transport: Transport = _urllib_transport,
        server_id: Optional[str] = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "http" or (parsed.hostname or "") not in _LOOPBACK_HOSTS:
            raise ValueError("Zotero 本机接口只能连接本机回环地址")
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._server_id = server_id

    # ── transport ───────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[Mapping[str, object]] = None) -> Response:
        query = urllib.parse.urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        )
        url = f"{self._base}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        headers = {
            # Not a Mozilla/ UA; the header below is sent anyway because Zotero
            # rejects browser-originated requests that lack it.
            "User-Agent": f"MEFinder/{__version__} (Zotero local sync)",
            "zotero-allowed-request": "1",
            "Zotero-API-Version": "3",
            "Accept": "application/json",
        }
        if self._server_id:
            headers["Zotero-Server-ID"] = self._server_id
        try:
            return self._transport(url, headers, self._timeout)
        except ConnectionRefusedError as exc:
            raise ZoteroUnavailable("not_running", "未检测到 Zotero") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ZoteroUnavailable("error", "Zotero 响应超时") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ConnectionRefusedError):
                raise ZoteroUnavailable("not_running", "未检测到 Zotero") from exc
            raise ZoteroUnavailable("error", f"无法连接 Zotero：{reason or exc}") from exc
        except OSError as exc:
            raise ZoteroUnavailable("error", f"无法连接 Zotero：{exc}") from exc

    def _checked(self, path: str, params: Optional[Mapping[str, object]] = None) -> Response:
        status, headers, body = self._get(path, params)
        if status == 403:
            raise ZoteroUnavailable("api_disabled", "Zotero 未开启本机接口")
        if status == 412:
            raise ZoteroUnavailable("error", "Zotero 数据库已更换，需重新读取")
        if status != 200:
            raise ZoteroUnavailable("error", f"Zotero 返回 HTTP {status}")
        return status, headers, body

    def _json(self, path: str, params: Optional[Mapping[str, object]] = None):
        _status, headers, body = self._checked(path, params)
        try:
            return headers, json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ZoteroIncomplete(f"Zotero 返回的数据无法解析：{path}") from exc

    def _listing(self, path: str, params: Optional[Mapping[str, object]] = None) -> List[Dict]:
        """Read a JSON array page by page and prove the listing is complete."""

        results: List[Dict] = []
        seen: set[str] = set()
        start = 0
        expected: Optional[int] = None
        while True:
            page_params = dict(params or {})
            page_params.update({"format": "json", "limit": PAGE_SIZE, "start": start})
            headers, page = self._json(path, page_params)
            if not isinstance(page, list):
                raise ZoteroIncomplete(f"Zotero 列表格式异常：{path}")
            total = _header(headers, "Total-Results")
            if total is not None:
                try:
                    total_value = int(total)
                except ValueError as exc:
                    raise ZoteroIncomplete("Total-Results 无法解析") from exc
                if expected is None:
                    expected = total_value
                elif expected != total_value:
                    raise ZoteroIncomplete("读取期间 Zotero 数据发生变化")
            for entry in page:
                key = str((entry or {}).get("key") or "") if isinstance(entry, dict) else ""
                if not key:
                    raise ZoteroIncomplete(f"Zotero 条目缺少 key：{path}")
                if key not in seen:
                    seen.add(key)
                    results.append(entry)
            start += len(page)
            if len(page) < PAGE_SIZE or (expected is not None and start >= expected):
                break
        if expected is not None and len(results) != expected:
            raise ZoteroIncomplete(
                f"Zotero 列表不完整：应有 {expected} 条，实际读到 {len(results)} 条"
            )
        return results

    # ── public reads ────────────────────────────────────────────────────────

    def probe(self) -> ZoteroProbe:
        try:
            status, headers, _body = self._get(
                f"{MY_LIBRARY_PREFIX}/collections", {"limit": 1, "format": "json"}
            )
        except ZoteroUnavailable as exc:
            return ZoteroProbe(exc.state, str(exc))
        server_id = _header(headers, "Zotero-Server-ID")
        api_version = _header(headers, "Zotero-API-Version")
        zotero_version = _header(headers, "X-Zotero-Version")
        if status == 200:
            self._server_id = self._server_id or server_id
            return ZoteroProbe("connected", "已连接", server_id, api_version, zotero_version)
        if status == 403:
            return ZoteroProbe("api_disabled", "Zotero 未开启本机接口")
        if status == 404:
            return ZoteroProbe("unsupported", "这个版本的 Zotero 没有本机接口，需要 Zotero 7 或更新")
        return ZoteroProbe("error", f"Zotero 返回 HTTP {status}")

    def collections(self) -> List[Dict]:
        return self._listing(f"{MY_LIBRARY_PREFIX}/collections")

    def collection_top_items(self, collection_key: str) -> List[Dict]:
        return self._listing(
            f"{MY_LIBRARY_PREFIX}/collections/{_key(collection_key)}/items/top"
        )

    def collection_top_versions(self, collection_key: str) -> Dict[str, int]:
        return self._versions(
            f"{MY_LIBRARY_PREFIX}/collections/{_key(collection_key)}/items/top"
        )

    def attachments(self) -> List[Dict]:
        return self._listing(f"{MY_LIBRARY_PREFIX}/items", {"itemType": "attachment"})

    def attachment_versions(self) -> Dict[str, int]:
        return self._versions(f"{MY_LIBRARY_PREFIX}/items", {"itemType": "attachment"})

    def items_by_key(self, keys: Sequence[str]) -> List[Dict]:
        found: List[Dict] = []
        unique = list(dict.fromkeys(_key(key) for key in keys))
        for index in range(0, len(unique), ITEM_KEY_BATCH):
            batch = unique[index:index + ITEM_KEY_BATCH]
            entries = self._listing(
                f"{MY_LIBRARY_PREFIX}/items", {"itemKey": ",".join(batch)}
            )
            found.extend(entries)
        return found

    def attachment_file_path(self, attachment_key: str) -> Optional[Path]:
        """Local path of an attachment file, or ``None`` when Zotero has none."""

        status, _headers, body = self._get(
            f"{MY_LIBRARY_PREFIX}/items/{_key(attachment_key)}/file/view/url"
        )
        if status == 403:
            raise ZoteroUnavailable("api_disabled", "Zotero 未开启本机接口")
        if status == 404:
            return None
        if status != 200:
            raise ZoteroUnavailable("error", f"Zotero 返回 HTTP {status}")
        return file_url_to_path(body.decode("utf-8", "replace").strip())

    def _versions(self, path: str, params: Optional[Mapping[str, object]] = None) -> Dict[str, int]:
        query = dict(params or {})
        query["format"] = "versions"
        headers, payload = self._json(path, query)
        if not isinstance(payload, dict):
            raise ZoteroIncomplete(f"Zotero 版本列表格式异常：{path}")
        total = _header(headers, "Total-Results")
        if total is not None and total.isdigit() and int(total) != len(payload):
            raise ZoteroIncomplete("Zotero 版本列表不完整")
        return {str(key): int(value or 0) for key, value in payload.items()}


def _key(value: str) -> str:
    text = str(value or "").strip()
    if not text or not text.isalnum() or len(text) > 16:
        raise ValueError(f"无效的 Zotero key：{value!r}")
    return text


def file_url_to_path(url: str) -> Optional[Path]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "file":
        return None
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        # UNC share on Windows: keep the host as part of the path.
        return Path(urllib.request.url2pathname(f"//{parsed.netloc}{parsed.path}"))
    return Path(urllib.request.url2pathname(parsed.path))
