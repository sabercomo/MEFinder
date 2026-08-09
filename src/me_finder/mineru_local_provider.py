"""Adapter for the external ``mineru-api`` / ``mineru-router`` service.

MinerU, model weights, PyTorch, and CUDA remain outside the MEFinder process.
The adapter follows MinerU's current official async interface: ``GET /health``,
``POST /tasks``, ``GET /tasks/{id}``, and ``GET /tasks/{id}/result``.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence
from urllib.parse import urlparse

from .parser_provider import (
    NormalizedBlock,
    NormalizedPage,
    NormalizedParseResult,
    ParserCredential,
    ParserPollResult,
    ParserProvider,
    ParserProviderError,
    ParserRequest,
    ParserSubmission,
    ParserTaskStatus,
    ProviderCapabilities,
)


MINERU_LOCAL_PROVIDER_ID = "mineru-local"
MAX_LOCAL_JSON_RESPONSE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class MinerULocalConfig:
    endpoint: str = "http://127.0.0.1:8000"
    backend: str = "pipeline"
    parse_method: str = "auto"
    language: str = "ch"
    timeout_seconds: float = 300.0
    max_pages_per_file: Optional[int] = None
    max_bytes_per_file: Optional[int] = None
    max_concurrency: int = 1
    return_content_list: bool = True
    return_middle_json: bool = True
    formula_enable: bool = True
    table_enable: bool = True

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MinerU Local endpoint must be an http(s) URL")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


class MinerULocalHTTPClient:
    def __init__(self, config: MinerULocalConfig) -> None:
        self.config = config
        self.base = urlparse(config.endpoint.rstrip("/"))

    def health(self) -> Dict[str, object]:
        return self._json_request("GET", "/health")

    def submit(self, path: Path, fields: Mapping[str, str]) -> Dict[str, object]:
        boundary = f"----MEFinderMinerU{uuid.uuid4().hex}"
        prefix_parts = []
        for name, value in fields.items():
            prefix_parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )
        filename = Path(path).name.replace('"', "_")
        mime = mimetypes.guess_type(filename)[0] or "application/pdf"
        prefix_parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
        )
        prefix = b"".join(prefix_parts)
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        content_length = len(prefix) + Path(path).stat().st_size + len(suffix)
        connection = self._connection()
        try:
            connection.putrequest("POST", self._path("/tasks"))
            connection.putheader(
                "Content-Type", f"multipart/form-data; boundary={boundary}"
            )
            connection.putheader("Content-Length", str(content_length))
            connection.putheader("Accept", "application/json")
            connection.endheaders()
            connection.send(prefix)
            with Path(path).open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
            connection.send(suffix)
            response = connection.getresponse()
            return self._decode_response(response)
        except (OSError, socket.timeout) as exc:
            raise ParserProviderError(
                f"MinerU Local connection failed: {exc}",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
                retryable=True,
            ) from exc
        finally:
            connection.close()

    def task_status(self, task_id: str) -> Dict[str, object]:
        return self._json_request("GET", f"/tasks/{task_id}")

    def task_result(self, task_id: str) -> Dict[str, object]:
        return self._json_request("GET", f"/tasks/{task_id}/result")

    def _json_request(self, method: str, endpoint: str) -> Dict[str, object]:
        connection = self._connection()
        try:
            connection.request(
                method,
                self._path(endpoint),
                headers={"Accept": "application/json"},
            )
            return self._decode_response(connection.getresponse())
        except ParserProviderError:
            raise
        except (OSError, socket.timeout) as exc:
            raise ParserProviderError(
                f"MinerU Local connection failed: {exc}",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
                retryable=True,
            ) from exc
        finally:
            connection.close()

    def _decode_response(self, response) -> Dict[str, object]:
        raw = response.read(MAX_LOCAL_JSON_RESPONSE_BYTES + 1)
        if len(raw) > MAX_LOCAL_JSON_RESPONSE_BYTES:
            raise ParserProviderError(
                "MinerU Local JSON response exceeds the configured safety limit",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            )
        status = int(response.status)
        if status < 200 or status >= 300:
            remote_missing = status in {404, 410}
            raise ParserProviderError(
                f"MinerU Local HTTP {status}: {raw[:500].decode('utf-8', 'replace')}",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
                retryable=status >= 500 or status in {408, 429} or remote_missing,
                rate_limited=status == 429,
                remote_task_missing=remote_missing,
                status_code=status,
            )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParserProviderError(
                "MinerU Local returned malformed JSON",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            ) from exc
        if not isinstance(value, dict):
            raise ParserProviderError(
                "MinerU Local response must be a JSON object",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            )
        return value

    def _connection(self):
        connection_type = (
            http.client.HTTPSConnection
            if self.base.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_type(
            self.base.hostname,
            self.base.port,
            timeout=self.config.timeout_seconds,
        )

    def _path(self, endpoint: str) -> str:
        prefix = self.base.path.rstrip("/")
        return f"{prefix}{endpoint}" or "/"


class MinerULocalProvider(ParserProvider):
    provider_id = MINERU_LOCAL_PROVIDER_ID

    def __init__(
        self,
        config: MinerULocalConfig,
        *,
        client: Optional[MinerULocalHTTPClient] = None,
    ) -> None:
        self.config = config
        self.client = client or MinerULocalHTTPClient(config)
        self._capabilities = ProviderCapabilities(
            max_pages_per_file=config.max_pages_per_file,
            max_bytes_per_file=config.max_bytes_per_file,
            max_concurrency=config.max_concurrency,
            supports_scanned_pdf=True,
            supports_bbox=True,
            supports_page_ranges=False,
            supports_async_jobs=True,
            supports_stream_upload=True,
            supported_models=(config.backend,),
            optional_limits={"protocol": "mineru-api-tasks"},
        )

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def health(self) -> Dict[str, object]:
        result = self.client.health()
        return {"ok": True, **result}

    def submit(
        self,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserSubmission:
        request = self.prepare(request)
        fields = {
            "backend": str(request.options.get("backend") or self.config.backend),
            "parse_method": str(
                request.options.get("parse_method") or self.config.parse_method
            ),
            "lang_list": str(
                request.options.get("language") or self.config.language
            ),
            "formula_enable": _bool_text(
                request.options.get("formula_enable", self.config.formula_enable)
            ),
            "table_enable": _bool_text(
                request.options.get("table_enable", self.config.table_enable)
            ),
            "return_content_list": _bool_text(self.config.return_content_list),
            "return_middle_json": _bool_text(self.config.return_middle_json),
            "return_md": "true",
            "return_images": "false",
            "response_format_zip": "false",
        }
        response = self.client.submit(request.source_path, fields)
        task_id = str(response.get("task_id") or response.get("id") or "")
        if not task_id:
            raise ParserProviderError(
                "MinerU Local did not return a task_id",
                provider_id=self.provider_id,
            )
        status = _task_status(response)
        return ParserSubmission(
            provider_id=self.provider_id,
            remote_task_id=task_id,
            status=status,
            metadata={"queued_ahead": response.get("queued_ahead")},
        )

    def poll(
        self,
        remote_task_id: str,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserPollResult:
        response = self.client.task_status(remote_task_id)
        return ParserPollResult(
            status=_task_status(response),
            raw_status=response,
            progress=_optional_float(response.get("progress")),
            message=str(response.get("error") or response.get("message") or "")
            or None,
        )

    def fetch_result(
        self,
        submission: ParserSubmission,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> object:
        if submission.raw_result is not None:
            return submission.raw_result
        if not submission.remote_task_id:
            raise ParserProviderError(
                "MinerU Local result requires a task id",
                provider_id=self.provider_id,
            )
        return self.client.task_result(submission.remote_task_id)

    def normalize_result(
        self, raw_result: object, request: ParserRequest
    ) -> NormalizedParseResult:
        content = _find_content_list(raw_result)
        if content is None:
            raise ParserProviderError(
                "MinerU Local result does not contain content_list data",
                provider_id=self.provider_id,
            )
        blocks_by_page: Dict[int, list[NormalizedBlock]] = {
            index: [] for index in range(request.page_count)
        }
        for item_index, item in enumerate(content):
            if not isinstance(item, Mapping):
                continue
            try:
                local_page = int(item.get("page_idx"))
            except (TypeError, ValueError):
                continue
            if local_page not in blocks_by_page:
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if not text:
                continue
            bbox = item.get("bbox")
            blocks_by_page[local_page].append(
                NormalizedBlock(
                    text=text,
                    block_type=str(item.get("type") or "") or None,
                    bbox=tuple(bbox) if isinstance(bbox, (list, tuple)) else None,
                    reading_order=len(blocks_by_page[local_page]),
                    provenance={"mineru_local_item_index": item_index},
                )
            )
        pages = tuple(
            NormalizedPage(
                physical_pdf_page=request.global_page_offset + local_page + 1,
                text="\n".join(block.text for block in blocks),
                blocks=tuple(blocks),
                parser_provenance={
                    "provider": self.provider_id,
                    "backend": self.config.backend,
                    "local_page_index": local_page,
                    "global_page_offset": request.global_page_offset,
                },
            )
            for local_page, blocks in blocks_by_page.items()
        )
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=self.config.backend,
            pages=pages,
            provenance={"endpoint": self.config.endpoint},
        )


def _bool_text(value: object) -> str:
    return "true" if bool(value) else "false"


def _optional_float(value: object) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _task_status(response: Mapping[str, object]) -> ParserTaskStatus:
    value = str(response.get("status") or response.get("state") or "queued").lower()
    if value in {"completed", "complete", "done", "success", "succeeded"}:
        return ParserTaskStatus.COMPLETED
    if value in {"failed", "error"}:
        return ParserTaskStatus.PERMANENT_FAILURE
    if value in {"cancelled", "canceled"}:
        return ParserTaskStatus.CANCELLED
    if value in {"queued", "pending"}:
        return ParserTaskStatus.SUBMITTED
    return ParserTaskStatus.WAITING


def _find_content_list(value: object) -> Optional[Sequence[object]]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key == "content_list":
                if isinstance(nested, str):
                    try:
                        nested = json.loads(nested)
                    except json.JSONDecodeError:
                        return None
                return nested if isinstance(nested, list) else None
        for nested in value.values():
            found = _find_content_list(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        # A top-level content list is also accepted.
        if not value or all(isinstance(item, Mapping) for item in value):
            return value
        for nested in value:
            found = _find_content_list(nested)
            if found is not None:
                return found
    return None
