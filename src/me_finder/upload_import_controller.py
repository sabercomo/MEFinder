"""HTTP-facing document import: raw upload, chunked upload and local paths.

These handlers used to live inside the transport (``web_http``).  They return
``(status, payload)`` like every other controller; the transport only reads
the request and serializes the reply.  Raw-body handlers receive a
:class:`RawRequest` so they can read headers and the body stream, and drain an
unread body before replying (Windows resets the socket otherwise).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .http_route_table import RawRequest, RoutePair, mutating, route

UPLOAD_CONTROL_MAX_BYTES = 64 * 1024


class UploadImportController:
    def __init__(
        self,
        *,
        document_imports: Any,
        validate_parse_options: Callable[[object, object], tuple[str, str | None]],
        read_preferences: Callable[[Path], Mapping[str, object]],
        resolve_preferences_path: Callable[[Path], Path],
        root: Path,
        chunked_upload_error: type[Exception],
        mineru_error: type[Exception],
        vision_api_error: type[Exception],
    ) -> None:
        self._imports = document_imports
        self._validate_parse_options = validate_parse_options
        self._read_preferences = read_preferences
        self._resolve_preferences_path = resolve_preferences_path
        self._root = root
        self._chunked_upload_error = chunked_upload_error
        self._mineru_error = mineru_error
        self._vision_api_error = vision_api_error

    def import_stream(self, request: RawRequest) -> tuple[int, object]:
        filename = unquote(request.headers.get("X-File-Name", ""))
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".docx", ".epub"}:
            return 400, {"error": "只支持 PDF、DOCX 或 EPUB 文件。"}
        try:
            pdf_parse_mode, vision_provider_id = self._validate_parse_options(
                request.headers.get("X-PDF-Parse-Mode", "auto"),
                request.headers.get("X-Vision-Provider-ID", ""),
            )
        except (self._mineru_error, self._vision_api_error, ValueError) as exc:
            # Rejected before the upload body is consumed; drain it first so the
            # 400 reaches the client instead of a reset socket on Windows.
            request.drain()
            return 400, {"error": str(exc)}
        try:
            length = int(request.headers.get("Content-Length", "0"))
            result = self._imports.import_stream(
                filename,
                length,
                request.stream,
                pdf_parse_mode=pdf_parse_mode,
                vision_provider_id=vision_provider_id,
            )
            return 200, result
        except (self._mineru_error, self._vision_api_error, ValueError) as exc:
            return 400, {"error": str(exc)}
        except OSError:
            logging.exception("legacy import request failed")
            return 500, {"error": "导入失败，请查看 desktop.log。"}
        except Exception:
            logging.exception("legacy import request failed")
            return 500, {"error": "导入失败，请查看 desktop.log。"}

    def append_chunk(self, request: RawRequest) -> tuple[int, object]:
        try:
            upload_id = str(request.headers.get("X-Upload-ID", ""))
            offset = int(request.headers.get("X-Upload-Offset", "-1"))
            length = int(request.headers.get("Content-Length", "0"))
            progress = self._imports.append_chunk(
                upload_id,
                offset,
                length,
                request.stream,
            )
            return 200, progress
        except self._chunked_upload_error as exc:
            return exc.status, {"error": str(exc)}
        except (TypeError, ValueError):
            return 400, {"error": "上传分块请求无效。"}
        except Exception:
            logging.exception("chunked import request failed")
            return 500, {"error": "上传分块失败，请查看 desktop.log。"}

    def start(self, payload: object) -> tuple[int, object]:
        if not isinstance(payload, dict):
            return 400, {"error": "上传开始请求必须是 JSON 对象。"}
        filename = str(payload.get("file_name") or payload.get("filename") or "")
        try:
            total_size = int(payload.get("size") or 0)
            result = self._imports.start_chunked(
                filename,
                total_size,
                pdf_parse_mode=payload.get("parse_mode", "auto"),
                vision_provider_id=payload.get("provider_id", ""),
                import_kind=payload.get("import_kind", "document"),
            )
            return 200, result
        except self._chunked_upload_error as exc:
            return exc.status, {"error": str(exc)}
        except (self._mineru_error, ValueError, OSError) as exc:
            return 400, {"error": str(exc)}
        except Exception:
            logging.exception("chunked import session start failed")
            return 500, {"error": "无法开始上传，请查看 desktop.log。"}

    def cancel(self, payload: object) -> tuple[int, object]:
        if not isinstance(payload, dict):
            return 400, {"error": "上传取消请求必须是 JSON 对象。"}
        try:
            return 200, self._imports.cancel_chunked(str(payload.get("upload_id") or ""))
        except self._chunked_upload_error as exc:
            return exc.status, {"error": str(exc)}

    def finish(self, payload: object) -> tuple[int, object]:
        if not isinstance(payload, dict):
            return 400, {"error": "上传完成请求必须是 JSON 对象。"}
        upload_id = str(payload.get("upload_id") or "")
        try:
            return 200, self._imports.finish_chunked(upload_id)
        except self._chunked_upload_error as exc:
            return exc.status, {"error": str(exc)}
        except (self._mineru_error, self._vision_api_error, ValueError) as exc:
            return 400, {"error": str(exc)}
        except OSError:
            logging.exception("chunked import finalization failed")
            return 500, {"error": "导入失败，请查看 desktop.log。"}
        except Exception:
            logging.exception("chunked import finalization failed")
            return 500, {"error": "导入失败，请查看 desktop.log。"}

    def import_local(self, payload: object) -> tuple[int, object]:
        if not isinstance(payload, dict):
            return 400, {"error": "本地导入请求必须是 JSON 对象。"}
        raw_paths = payload.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            return 400, {"error": "没有选择要导入的文件。"}
        if len(raw_paths) > 50:
            return 400, {"error": "一次最多批量导入 50 个文件，请分批选择。"}
        try:
            pdf_parse_mode, vision_provider_id = self._validate_parse_options(
                payload.get("pdf_parse_mode", "auto"),
                payload.get("vision_provider_id", ""),
            )
        except self._mineru_error as exc:
            return 400, {"error": str(exc)}
        try:
            # Only files under the user's configured scan directories may be
            # imported by path.
            preferences = self._read_preferences(
                self._resolve_preferences_path(self._root)
            )
            allowed_bases = [
                Path(item).resolve()
                for item in preferences.get("scan_directories") or []
            ]
            result = self._imports.import_local(
                raw_paths,
                allowed_bases,
                pdf_parse_mode=pdf_parse_mode,
                vision_provider_id=vision_provider_id,
            )
        except OSError:
            logging.exception("local import request failed")
            return 500, {"error": "导入失败，请查看 desktop.log。"}
        return 200, result


def assemble_upload_import_routes(controller: UploadImportController) -> RoutePair:
    """导入:原始流上传、分片上传与按本机路径导入;全部改动数据目录。"""

    upload_control = dict(
        mutates_data_root=True,
        max_body_bytes=UPLOAD_CONTROL_MAX_BYTES,
        oversize_error="上传控制请求过大。",
    )
    post_routes = {
        "/api/import": route(controller.import_stream, body="raw", mutates_data_root=True),
        "/api/import-upload/chunk": route(
            controller.append_chunk, body="raw", mutates_data_root=True
        ),
        "/api/import-upload/start": route(controller.start, **upload_control),
        "/api/import-upload/cancel": route(controller.cancel, **upload_control),
        "/api/import-upload/finish": route(controller.finish, **upload_control),
        "/api/import-local": mutating(controller.import_local),
    }
    return {}, post_routes
