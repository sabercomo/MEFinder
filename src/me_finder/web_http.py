"""HTTP transport for the local MEFinder web application.

The application composition root lives in :mod:`me_finder.web`.  This module
owns request parsing, trust checks, response serialization and source streaming,
and receives the already-built application services through one explicit
context object.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .application import SearchRequest
from .http_range import InvalidByteRange, parse_byte_range
from .http_route_table import RawRequest


MAX_JSON_REQUEST_BYTES = 1024 * 1024
SOURCE_STREAM_CHUNK_BYTES = 1024 * 1024
@dataclass(frozen=True)
class WebHTTPContext:
    """Application services and adapters used by the HTTP boundary."""

    index_path: Path
    root: Path
    index_runtime: Any
    data_root_admission: Any
    # http_route_table.RouteTable: the single (method, path) -> Route registry.
    routes: Any
    render_html: Callable[..., str]
    package_dir: Path
    read_preferences: Callable[[Path], Mapping[str, object]]
    resolve_preferences_path: Callable[[Path], Path]
    load_import_config: Callable[[Path], Mapping[str, object]]
    resolve_document_group_source_ids: Callable[[object, Path], list[str]]
    data_root_admission_error: type[Exception]
    document_group_not_found_error: type[Exception]


def make_http_handler(context: WebHTTPContext):
    """Build a request handler over an already-composed application runtime."""

    index_path = context.index_path
    root = context.root
    index_runtime = context.index_runtime
    data_root_admission = context.data_root_admission
    routes = context.routes
    render_html = context.render_html
    _PACKAGE_DIR = context.package_dir
    read_preferences = context.read_preferences
    resolve_preferences_path = context.resolve_preferences_path
    load_import_config = context.load_import_config
    resolve_document_group_source_ids = (
        context.resolve_document_group_source_ids
    )
    data_root_admission_error = context.data_root_admission_error
    document_group_not_found_error = context.document_group_not_found_error

    class Handler(BaseHTTPRequestHandler):
        route_table = routes
        _POST_ROUTE_TABLE = {
            "/api/search": "_post_search",
        }

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            content_length: int | None = None,
            send_body: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body) if content_length is None else content_length))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _send_json(self, data: object, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _discard_small_request_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return
            if 0 < length <= MAX_JSON_REQUEST_BYTES:
                self.rfile.read(length)

        def _drain_request_body(self) -> None:
            # Consume the pending request body in full before replying. Uploads
            # legitimately exceed MAX_JSON_REQUEST_BYTES, so this reads the whole
            # declared Content-Length rather than the JSON-sized cap. Replying
            # while inbound bytes are still unread makes Windows reset the socket,
            # so the client would see a connection abort instead of our response.
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return
            while length > 0:
                chunk = self.rfile.read(min(length, 1 << 20))
                if not chunk:
                    break
                length -= len(chunk)

        def _validated_request_host(
            self,
        ) -> tuple[Optional[tuple[str, int]], Optional[int]]:
            values = self.headers.get_all("Host") or []
            if len(values) != 1:
                return None, 400
            value = str(values[0]).strip()
            try:
                parsed = urlparse(f"//{value}")
                port = parsed.port or 80
            except ValueError:
                return None, 400
            hostname = str(parsed.hostname or "").casefold()
            if (
                not hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.params
                or parsed.query
                or parsed.fragment
            ):
                return None, 400
            if (
                hostname not in {"127.0.0.1", "localhost", "::1"}
                or port != self.server.server_port
            ):
                return None, 421
            return (hostname, port), None

        def _request_origin_is_trusted(
            self,
            authority: tuple[str, int],
        ) -> bool:
            values = self.headers.get_all("Origin") or []
            if not values:
                return True
            if len(values) != 1:
                return False
            try:
                parsed = urlparse(str(values[0]).strip())
                port = parsed.port or 80
            except ValueError:
                return False
            return bool(
                parsed.scheme == "http"
                and str(parsed.hostname or "").casefold() == authority[0]
                and port == authority[1]
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and not parsed.params
                and not parsed.query
                and not parsed.fragment
            )

        def _request_target_matches(
            self,
            authority: tuple[str, int],
        ) -> bool:
            parsed = urlparse(self.path)
            if not parsed.scheme and not parsed.netloc:
                return True
            try:
                port = parsed.port or 80
            except ValueError:
                return False
            return bool(
                parsed.scheme == "http"
                and str(parsed.hostname or "").casefold() == authority[0]
                and port == authority[1]
                and parsed.username is None
                and parsed.password is None
            )

        def _reject_untrusted_request(self, *, send_body: bool = True) -> bool:
            authority, host_error = self._validated_request_host()
            if host_error is None and not self._request_target_matches(authority):
                host_error = 421
            if host_error is None and self._request_origin_is_trusted(authority):
                return False
            status = host_error or 403
            message = (
                "Host 请求头无效。"
                if status == 400
                else "请求目标不是当前本地服务。"
                if status == 421
                else "请求来源不受信任。"
            )
            body = json.dumps(
                {"error": message},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(
                status,
                body if send_body else b"",
                "application/json; charset=utf-8",
                content_length=len(body),
                send_body=send_body,
            )
            return True

        def _post_search(self, payload: object) -> None:
            try:
                # Resolve a document_group_id scope to member source_file_ids at the
                # transport boundary; SearchService / search.py never see DocumentGroups.
                if isinstance(payload, dict) and str(
                    payload.get("document_group_id") or ""
                ).strip():
                    if str(payload.get("source_file_id") or "").strip():
                        raise ValueError(
                            "source_file_id 与 document_group_id 不能同时指定。"
                        )
                    member_ids = resolve_document_group_source_ids(
                        payload["document_group_id"], index_path
                    )
                    payload = dict(payload)
                    payload.pop("document_group_id", None)
                    payload["source_file_ids"] = member_ids
                request = SearchRequest.from_payload(payload)
            except document_group_not_found_error as exc:
                self._send_json({"error": str(exc)}, status=404)
                return
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            try:
                result = index_runtime.search(request)
            except sqlite3.OperationalError as exc:
                # A read that sat out the full busy_timeout on a concurrent
                # writer's lock surfaces here as "database is locked" (or
                # "busy"). Map that to a distinct, retriable 503 — never swallow
                # it into an empty result that would falsely read as "no hits".
                # Any other operational error is a real fault and must not be
                # masked as a transient, so it propagates to the 500 handler.
                message = str(exc).lower()
                if "locked" in message or "busy" in message:
                    self._send_json(
                        {
                            "error": "索引正忙（写入未在超时内完成），请稍候重试。",
                            "retriable": True,
                        },
                        status=503,
                    )
                    return
                # A non-lock operational error is a real fault: surface it as a
                # logged 500, not a dropped connection and not an empty result.
                logging.exception("search query failed")
                self._send_json(
                    {"error": "搜索失败，请查看 desktop.log。"}, status=500
                )
                return
            if result is None:
                self._send_json(
                    {"error": "索引正在重建，请稍候再搜索。"},
                    status=503,
                )
                return
            self._send_json(result)

        def do_GET(self) -> None:
            if self._reject_untrusted_request():
                return
            parsed = urlparse(self.path)
            api_route = routes.get("GET", parsed.path)
            if api_route is not None:
                params = parse_qs(
                    parsed.query,
                    keep_blank_values=api_route.policy.keep_blank_query,
                )
                status, payload = api_route.handler(params)
                self._send_json(payload, status=status)
                return
            if parsed.path in {"/", "/index.html", "/reader", "/reader/", "/reader-window"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                body = (render_html(theme, reader_window=True) if parsed.path == "/reader-window" else render_html(theme)).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/static/brands/"):
                name = parsed.path.rsplit("/", 1)[-1]
                icon_path = _PACKAGE_DIR / "static" / "brands" / name
                if re.fullmatch(r"[a-z0-9][a-z0-9-]*\.svg", name) and icon_path.is_file():
                    self._send(200, icon_path.read_bytes(), "image/svg+xml")
                else:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            if parsed.path == "/api/calibration":
                config_path = root / "config" / "pdf_imports.json"
                if not config_path.exists():
                    self._send_json({"documents": []})
                    return
                config = load_import_config(config_path)
                params = parse_qs(parsed.query)
                sid = (params.get("source_id") or [None])[0]
                if sid:
                    doc = next((d for d in config.get("documents", []) if d.get("source_file_id") == sid), None)
                    self._send_json(doc or {"error": "not found"})
                else:
                    self._send_json(config)
                return
            if parsed.path.startswith("/source/"):
                self._send_source(parsed.path)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_HEAD(self) -> None:
            if self._reject_untrusted_request(send_body=False):
                return
            parsed = urlparse(self.path)
            if parsed.path.startswith("/source/"):
                self._send_source(parsed.path, send_body=False)
                return
            if parsed.path in {"/", "/index.html", "/reader", "/reader/", "/reader-window"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                content_length = len((render_html(theme, reader_window=True) if parsed.path == "/reader-window" else render_html(theme)).encode("utf-8"))
                self._send(200, b"", "text/html; charset=utf-8", content_length=content_length, send_body=False)
                return
            self._send(404, b"", "text/plain; charset=utf-8", send_body=False)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if self._reject_untrusted_request():
                self._discard_small_request_body()
                return
            api_route = routes.get("POST", parsed.path)
            content_type = str(self.headers.get("Content-Type") or "")
            media_type = content_type.partition(";")[0].strip().casefold()
            if api_route is not None and api_route.body == "raw":
                invalid_content_type = media_type in {
                    "",
                    "text/plain",
                    "application/x-www-form-urlencoded",
                    "multipart/form-data",
                }
                content_type_error = "不支持此上传 Content-Type。"
            else:
                invalid_content_type = media_type != "application/json"
                content_type_error = "JSON 请求必须使用 application/json。"
            if invalid_content_type:
                self._discard_small_request_body()
                self._send_json(
                    {"error": content_type_error},
                    status=415,
                )
                return
            if api_route is None or not api_route.mutates_data_root:
                self._do_POST(api_route)
                return
            try:
                with data_root_admission.operation():
                    self._do_POST(api_route)
            except data_root_admission_error as exc:
                self._discard_small_request_body()
                self._send_json({"error": str(exc)}, status=409)

        def _do_POST(self, api_route) -> None:
            parsed = urlparse(self.path)
            if index_runtime.closing:
                self._discard_small_request_body()
                self._send_json({"error": "应用正在关闭。"}, status=503)
                return
            if api_route is not None and api_route.body == "raw":
                status, response = api_route.handler(
                    RawRequest(self.headers, self.rfile, self._drain_request_body)
                )
                self._send_json(response, status=status)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                self._send_json({"error": "Content-Length 无效。"}, status=400)
                return
            if length < 0:
                self._send_json({"error": "Content-Length 无效。"}, status=400)
                return
            if length > MAX_JSON_REQUEST_BYTES:
                self._send_json({"error": "JSON 请求内容过大。"}, status=413)
                return
            policy = api_route.policy if api_route is not None else None
            if policy is not None and policy.max_body_bytes is not None and length > policy.max_body_bytes:
                if policy.drain_oversize:
                    self.rfile.read(length)
                self._send_json({"error": policy.oversize_error}, status=413)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json({"error": "请求格式无效。"}, status=400)
                return
            route_method = self._POST_ROUTE_TABLE.get(parsed.path)
            if route_method is not None:
                getattr(self, route_method)(payload)
                return
            if api_route is not None:
                status, response = api_route.handler(payload)
                self._send_json(response, status=status)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def _send_source(self, request_path: str, send_body: bool = True) -> None:
            source_id = unquote(request_path[len("/source/") :])
            record = index_runtime.source(source_id)
            if not record:
                self._send(404, b"Unknown source", "text/plain; charset=utf-8")
                return
            relative_path = str(record.get("relative_path") or "")
            target = (root / relative_path).resolve()
            if target != root and root not in target.parents:
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return
            if target.suffix.lower() not in {".pdf", ".doc", ".docx", ".epub"} or not target.exists():
                self._send(404, b"Source not found", "text/plain; charset=utf-8")
                return
            content_type = {
                ".pdf": "application/pdf",
                ".doc": "application/msword",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".epub": "application/epub+zip",
            }.get(target.suffix.lower(), "application/octet-stream")
            file_size = target.stat().st_size
            try:
                requested_range = parse_byte_range(
                    self.headers.get("Range"),
                    file_size,
                )
            except InvalidByteRange:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if requested_range is None:
                status = 200
                start = 0
                content_length = file_size
            else:
                status = 206
                start = requested_range.start
                content_length = requested_range.length

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(content_length))
            if requested_range is not None:
                self.send_header(
                    "Content-Range",
                    f"bytes {requested_range.start}-{requested_range.end}/{file_size}",
                )
            self.end_headers()
            if not send_body or content_length == 0:
                return

            try:
                with target.open("rb") as stream:
                    stream.seek(start)
                    remaining = content_length
                    while remaining:
                        chunk = stream.read(min(SOURCE_STREAM_CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Closing a PDF tab while a range is streaming is normal and
                # should not produce a server traceback.
                return

        def log_message(self, format: str, *args) -> None:
            return


    return Handler
