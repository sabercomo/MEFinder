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
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .application import SearchRequest
from .http_range import InvalidByteRange, parse_byte_range


MAX_JSON_REQUEST_BYTES = 1024 * 1024
SOURCE_STREAM_CHUNK_BYTES = 1024 * 1024
RAW_BODY_POST_PATHS = frozenset(
    {
        "/api/import",
        "/api/import-upload/chunk",
    }
)
DATA_ROOT_MUTATING_POST_PATHS = frozenset(
    {
        "/api/preferences",
        "/api/mineru-accounts",
        "/api/mineru-accounts/service",
        "/api/mineru-config",
        "/api/mineru-local",
        "/api/mineru-local/component",
        "/api/local-ocr",
        "/api/local-ocr/component",
        "/api/vision-providers",
        "/api/import",
        "/api/import-upload/start",
        "/api/import-upload/chunk",
        "/api/import-upload/cancel",
        "/api/import-upload/finish",
        "/api/mineru-reparse",
        "/api/import-retry-mineru",
        "/api/import-retry-mineru-local",
        "/api/import-retry",
        "/api/import-resume",
        "/api/import-resume-dismiss",
        "/api/bibliographic-metadata/batch-detect",
        "/api/export-directory/choose",
        "/api/backup/export",
        "/api/document/export",
        "/api/backup/import",
        "/api/import-local",
        "/api/calibration",
        "/api/bibliographic-metadata/save",
        "/api/auto-page-mapping/apply",
        "/api/auto-page-mapping/accept",
        "/api/documents/remove",
        "/api/documents/remove-batch",
        "/api/document-groups/create",
        "/api/document-groups/rename",
        "/api/document-groups/delete",
        "/api/document-groups/add-member",
        "/api/document-groups/remove-member",
        "/api/document-groups/set-base",
        "/api/document-groups/version-label",
    }
)


@dataclass(frozen=True)
class WebHTTPContext:
    """Application services and adapters used by the HTTP boundary."""

    index_path: Path
    root: Path
    index_runtime: Any
    data_root_admission: Any
    document_imports: Any
    controller_get_routes: Mapping[str, Callable[..., tuple[int, object]]]
    controller_post_routes: Mapping[str, Callable[..., tuple[int, object]]]
    shell_get_routes: Mapping[str, Callable[..., tuple[int, object]]]
    shell_post_routes: Mapping[str, Callable[..., tuple[int, object]]]
    render_html: Callable[[str], str]
    package_dir: Path
    read_preferences: Callable[[Path], Mapping[str, object]]
    resolve_preferences_path: Callable[[Path], Path]
    load_import_config: Callable[[Path], Mapping[str, object]]
    resolve_document_group_source_ids: Callable[[object, Path], list[str]]
    validate_parse_options: Callable[[object, object], tuple[str, str | None]]
    data_root_admission_error: type[Exception]
    document_group_not_found_error: type[Exception]
    chunked_upload_error: type[Exception]
    mineru_error: type[Exception]
    vision_api_error: type[Exception]


def make_http_handler(context: WebHTTPContext):
    """Build a request handler over an already-composed application runtime."""

    index_path = context.index_path
    root = context.root
    index_runtime = context.index_runtime
    data_root_admission = context.data_root_admission
    document_imports = context.document_imports
    controller_get_routes = context.controller_get_routes
    controller_post_routes = context.controller_post_routes
    shell_get_routes = context.shell_get_routes
    shell_post_routes = context.shell_post_routes
    render_html = context.render_html
    _PACKAGE_DIR = context.package_dir
    read_preferences = context.read_preferences
    resolve_preferences_path = context.resolve_preferences_path
    load_import_config = context.load_import_config
    resolve_document_group_source_ids = (
        context.resolve_document_group_source_ids
    )
    validate_parse_options = context.validate_parse_options
    data_root_admission_error = context.data_root_admission_error
    document_group_not_found_error = context.document_group_not_found_error
    chunked_upload_error = context.chunked_upload_error
    mineru_error = context.mineru_error
    vision_api_error = context.vision_api_error

    class Handler(BaseHTTPRequestHandler):
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
            result = index_runtime.search(request)
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
            controller_route = controller_get_routes.get(parsed.path)
            if controller_route is not None:
                params = parse_qs(
                    parsed.query,
                    keep_blank_values=(parsed.path == "/api/document/pages"),
                )
                status, payload = controller_route(params)
                self._send_json(payload, status=status)
                return
            shell_route = shell_get_routes.get(parsed.path)
            if shell_route is not None:
                status, payload = shell_route()
                self._send_json(payload, status=status)
                return
            if parsed.path in {"/", "/index.html", "/reader", "/reader/"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                body = render_html(theme).encode("utf-8")
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
            if parsed.path in {"/", "/index.html", "/reader", "/reader/"}:
                preferences_path = resolve_preferences_path(root)
                theme = read_preferences(preferences_path)["theme"]
                content_length = len(render_html(theme).encode("utf-8"))
                self._send(200, b"", "text/html; charset=utf-8", content_length=content_length, send_body=False)
                return
            self._send(404, b"", "text/plain; charset=utf-8", send_body=False)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if self._reject_untrusted_request():
                self._discard_small_request_body()
                return
            content_type = str(self.headers.get("Content-Type") or "")
            media_type = content_type.partition(";")[0].strip().casefold()
            if parsed.path in RAW_BODY_POST_PATHS:
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
            if parsed.path not in DATA_ROOT_MUTATING_POST_PATHS:
                self._do_POST()
                return
            try:
                with data_root_admission.operation():
                    self._do_POST()
            except data_root_admission_error as exc:
                self._discard_small_request_body()
                self._send_json({"error": str(exc)}, status=409)

        def _do_POST(self) -> None:
            parsed = urlparse(self.path)
            if index_runtime.closing:
                self._discard_small_request_body()
                self._send_json({"error": "应用正在关闭。"}, status=503)
                return
            if parsed.path == "/api/import":
                filename = unquote(self.headers.get("X-File-Name", ""))
                suffix = Path(filename).suffix.lower()
                if suffix not in {".pdf", ".docx", ".epub"}:
                    self._send_json(
                        {"error": "只支持 PDF、DOCX 或 EPUB 文件。"},
                        status=400,
                    )
                    return
                try:
                    pdf_parse_mode, vision_provider_id = (
                        validate_parse_options(
                            self.headers.get("X-PDF-Parse-Mode", "auto"),
                            self.headers.get("X-Vision-Provider-ID", ""),
                        )
                    )
                    length = int(self.headers.get("Content-Length", "0"))
                    result = document_imports.import_stream(
                        filename,
                        length,
                        self.rfile,
                        pdf_parse_mode=pdf_parse_mode,
                        vision_provider_id=vision_provider_id,
                    )
                    self._send_json(result)
                except (mineru_error, vision_api_error, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except OSError:
                    logging.exception("legacy import request failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                except Exception:
                    logging.exception("legacy import request failed")
                    self._send_json({"error": "导入失败，请查看 desktop.log。"}, status=500)
                return
            if parsed.path == "/api/import-upload/chunk":
                try:
                    upload_id = str(self.headers.get("X-Upload-ID", ""))
                    offset = int(self.headers.get("X-Upload-Offset", "-1"))
                    length = int(self.headers.get("Content-Length", "0"))
                    progress = document_imports.append_chunk(
                        upload_id,
                        offset,
                        length,
                        self.rfile,
                    )
                    self._send_json(progress)
                except chunked_upload_error as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                except (TypeError, ValueError):
                    self._send_json({"error": "上传分块请求无效。"}, status=400)
                except Exception:
                    logging.exception("chunked import request failed")
                    self._send_json(
                        {"error": "上传分块失败，请查看 desktop.log。"},
                        status=500,
                    )
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
            if parsed.path == "/api/document/citation" and length > 16 * 1024:
                self._send_json({"error": "引文请求内容过大。"}, status=413)
                return
            if parsed.path == "/api/bibliographic-metadata/parse-cnki-citation" and length > 32 * 1024:
                self.rfile.read(length)
                self._send_json({"error": "知网引用文字过大，请只粘贴一条引文。"}, status=413)
                return
            if (
                parsed.path
                in {
                    "/api/import-upload/start",
                    "/api/import-upload/finish",
                    "/api/import-upload/cancel",
                }
                and length > 64 * 1024
            ):
                self._send_json({"error": "上传控制请求过大。"}, status=413)
                return
            if (
                parsed.path
                in {
                    "/api/bibliographic-metadata/lookup-cnki",
                    "/api/bibliographic-metadata/cnki-candidate",
                    "/api/bibliographic-metadata/open-cnki",
                }
                and length > 32 * 1024
            ):
                self._send_json({"error": "知网题录请求内容过大。"}, status=413)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json({"error": "请求格式无效。"}, status=400)
                return
            if parsed.path == "/api/import-upload/start":
                if not isinstance(payload, dict):
                    self._send_json({"error": "上传开始请求必须是 JSON 对象。"}, status=400)
                    return
                filename = str(payload.get("file_name") or payload.get("filename") or "")
                try:
                    total_size = int(payload.get("size") or 0)
                    result = document_imports.start_chunked(
                        filename,
                        total_size,
                        pdf_parse_mode=payload.get("parse_mode", "auto"),
                        vision_provider_id=payload.get("provider_id", ""),
                        import_kind=payload.get("import_kind", "document"),
                    )
                    self._send_json(result)
                except chunked_upload_error as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                except (mineru_error, ValueError, OSError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except Exception:
                    logging.exception("chunked import session start failed")
                    self._send_json(
                        {"error": "无法开始上传，请查看 desktop.log。"},
                        status=500,
                    )
                return
            if parsed.path == "/api/import-upload/cancel":
                if not isinstance(payload, dict):
                    self._send_json({"error": "上传取消请求必须是 JSON 对象。"}, status=400)
                    return
                try:
                    result = document_imports.cancel_chunked(
                        str(payload.get("upload_id") or "")
                    )
                    self._send_json(result)
                except chunked_upload_error as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                return
            if parsed.path == "/api/import-upload/finish":
                if not isinstance(payload, dict):
                    self._send_json({"error": "上传完成请求必须是 JSON 对象。"}, status=400)
                    return
                upload_id = str(payload.get("upload_id") or "")
                try:
                    result = document_imports.finish_chunked(upload_id)
                    self._send_json(result)
                except chunked_upload_error as exc:
                    self._send_json({"error": str(exc)}, status=exc.status)
                except (mineru_error, vision_api_error, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except OSError:
                    logging.exception("chunked import finalization failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                except Exception:
                    logging.exception("chunked import finalization failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                return
            route_method = self._POST_ROUTE_TABLE.get(parsed.path)
            if route_method is not None:
                getattr(self, route_method)(payload)
                return
            controller_route = controller_post_routes.get(parsed.path)
            if controller_route is not None:
                status, response = controller_route(payload)
                self._send_json(response, status=status)
                return
            shell_route = shell_post_routes.get(parsed.path)
            if shell_route is not None:
                status, response = shell_route(payload)
                self._send_json(response, status=status)
                return
            if parsed.path == "/api/import-local":
                if not isinstance(payload, dict):
                    self._send_json(
                        {"error": "本地导入请求必须是 JSON 对象。"},
                        status=400,
                    )
                    return
                raw_paths = payload.get("paths")
                if not isinstance(raw_paths, list) or not raw_paths:
                    self._send_json({"error": "没有选择要导入的文件。"}, status=400)
                    return
                if len(raw_paths) > 50:
                    self._send_json(
                        {"error": "一次最多批量导入 50 个文件，请分批选择。"},
                        status=400,
                    )
                    return
                try:
                    pdf_parse_mode, vision_provider_id = (
                        validate_parse_options(
                            payload.get("pdf_parse_mode", "auto"),
                            payload.get("vision_provider_id", ""),
                        )
                    )
                except mineru_error as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                try:
                    preferences = read_preferences(
                        resolve_preferences_path(root)
                    )
                    allowed_bases = [
                        Path(item).resolve()
                        for item in preferences.get("scan_directories") or []
                    ]
                    result = document_imports.import_local(
                        raw_paths,
                        allowed_bases,
                        pdf_parse_mode=pdf_parse_mode,
                        vision_provider_id=vision_provider_id,
                    )
                except OSError:
                    logging.exception("local import request failed")
                    self._send_json(
                        {"error": "导入失败，请查看 desktop.log。"},
                        status=500,
                    )
                    return
                self._send_json(result)
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
