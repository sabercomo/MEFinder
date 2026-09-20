"""Adapter for a locally deployed MinerU service.

MinerU, model weights, PyTorch, and CUDA remain outside the MEFinder process.
Two MinerU protocols are supported and detected at runtime, so a user who
upgrades their own MinerU install does not have to wait for a MEFinder release:

* ``tasks`` — MinerU 3.x: ``GET /health``, ``POST /tasks``,
  ``GET /tasks/{id}``, ``GET /tasks/{id}/result``.
* ``v1-jobs`` — MinerU 4.x: ``GET /v1/health`` plus the upload/parse-job API
  implemented in :mod:`me_finder.mineru_local_v1`.
"""

from __future__ import annotations

import json
import mimetypes
import socket
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence
from urllib.parse import urlparse

from .mineru_local_http import (
    MINERU_LOCAL_PROVIDER_ID,
    MinerULocalTransport,
)
from .mineru_local_v1 import (
    MINERU_V1_PROTOCOL,
    MinerUV1Client,
    block_bbox,
    block_text,
    block_text_level,
    decode_structured_content,
    iter_page_blocks,
    job_status,
    scaled_bbox,
    structured_content_file_id,
    tier_for_backend,
)
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


MINERU_TASKS_PROTOCOL = "tasks"
MINERU_PROTOCOL_AUTO = "auto"
MINERU_LOCAL_PROTOCOLS = (
    MINERU_PROTOCOL_AUTO,
    MINERU_TASKS_PROTOCOL,
    MINERU_V1_PROTOCOL,
)
DEFAULT_LOCAL_SLICE_MAX_PAGES = 200
DEFAULT_LOCAL_SLICE_MAX_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class MinerULocalConfig:
    endpoint: str = "http://127.0.0.1:8000"
    backend: str = "pipeline"
    parse_method: str = "auto"
    language: str = "ch"
    timeout_seconds: float = 300.0
    max_pages_per_file: Optional[int] = DEFAULT_LOCAL_SLICE_MAX_PAGES
    max_bytes_per_file: Optional[int] = DEFAULT_LOCAL_SLICE_MAX_BYTES
    max_concurrency: int = 1
    return_content_list: bool = True
    return_middle_json: bool = True
    formula_enable: bool = True
    table_enable: bool = True
    protocol: str = MINERU_PROTOCOL_AUTO
    api_key: str = ""
    tier: str = ""

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MinerU Local endpoint must be an http(s) URL")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.protocol not in MINERU_LOCAL_PROTOCOLS:
            raise ValueError("MinerU Local protocol must be auto, tasks, or v1-jobs")


class MinerULocalHTTPClient:
    """MinerU 3.x ``/tasks`` client."""

    def __init__(self, config: MinerULocalConfig) -> None:
        self.config = config
        self.transport = MinerULocalTransport(
            config.endpoint,
            timeout_seconds=config.timeout_seconds,
            api_key=config.api_key,
        )
        self.base = self.transport.base

    def health(self) -> Dict[str, object]:
        return self.transport.json_request("GET", "/health")

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
        connection = self.transport.connection()
        try:
            connection.putrequest("POST", self.transport.path("/tasks"))
            connection.putheader(
                "Content-Type", f"multipart/form-data; boundary={boundary}"
            )
            connection.putheader("Content-Length", str(content_length))
            for name, value in self.transport.headers().items():
                connection.putheader(name, value)
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
            return self.transport.decode_response(response)
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

    def task_status(self, task_id: str) -> Dict[str, object]:
        return self.transport.json_request("GET", f"/tasks/{task_id}")

    def task_result(self, task_id: str) -> Dict[str, object]:
        return self.transport.json_request("GET", f"/tasks/{task_id}/result")


class MinerULocalProvider(ParserProvider):
    provider_id = MINERU_LOCAL_PROVIDER_ID

    def __init__(
        self,
        config: MinerULocalConfig,
        *,
        client: Optional[MinerULocalHTTPClient] = None,
        v1_client: Optional[MinerUV1Client] = None,
    ) -> None:
        self.config = config
        self.client = client or MinerULocalHTTPClient(config)
        self._v1_client = v1_client
        self._detected_protocol: Optional[str] = (
            None if config.protocol == MINERU_PROTOCOL_AUTO else config.protocol
        )
        self._detected_version = ""
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
            optional_limits={"protocol": config.protocol},
        )

    # ── protocol detection ───────────────────────────────────────────

    @property
    def v1_client(self) -> MinerUV1Client:
        if self._v1_client is None:
            self._v1_client = MinerUV1Client(
                MinerULocalTransport(
                    self.config.endpoint,
                    timeout_seconds=self.config.timeout_seconds,
                    api_key=self.config.api_key,
                )
            )
        return self._v1_client

    def resolve_protocol(self) -> str:
        """Return the protocol this endpoint speaks, probing it once."""

        if self._detected_protocol is not None:
            return self._detected_protocol
        self.probe()
        assert self._detected_protocol is not None
        return self._detected_protocol

    def probe(self) -> Dict[str, object]:
        """Detect the endpoint's protocol and report its health payload."""

        configured = self.config.protocol
        attempts = (
            (MINERU_V1_PROTOCOL, MINERU_TASKS_PROTOCOL)
            if configured == MINERU_PROTOCOL_AUTO
            else (configured,)
        )
        last_error: Optional[ParserProviderError] = None
        for protocol in attempts:
            try:
                payload = (
                    self.v1_client.health()
                    if protocol == MINERU_V1_PROTOCOL
                    else self.client.health()
                )
            except ParserProviderError as exc:
                # A missing route means "not this protocol"; anything else
                # (refused connection, unhealthy service) is a real failure.
                if exc.status_code in {404, 405}:
                    last_error = exc
                    continue
                raise
            self._detected_protocol = protocol
            self._detected_version = str(payload.get("version") or "")
            return {
                "protocol": protocol,
                "version": self._detected_version,
                "health": payload,
            }
        raise ParserProviderError(
            "MinerU Local endpoint exposes neither the 3.x /tasks API nor the "
            "4.x /v1 API; check the service address and version",
            provider_id=self.provider_id,
            retryable=False,
        ) from last_error

    def capabilities(self) -> ProviderCapabilities:
        if self._detected_protocol is None:
            return self._capabilities
        return replace(
            self._capabilities,
            optional_limits={"protocol": self._detected_protocol},
        )

    def health(self) -> Dict[str, object]:
        probed = self.probe()
        payload = probed["health"]
        merged = dict(payload) if isinstance(payload, Mapping) else {}
        return {
            "ok": True,
            "protocol": probed["protocol"],
            "mineru_version": probed["version"],
            **merged,
        }

    # ── submission ───────────────────────────────────────────────────

    def submit(
        self,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserSubmission:
        request = self.prepare(request)
        if self.resolve_protocol() == MINERU_V1_PROTOCOL:
            return self._submit_v1(request)
        return self._submit_tasks(request)

    def _submit_tasks(self, request: ParserRequest) -> ParserSubmission:
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
        return ParserSubmission(
            provider_id=self.provider_id,
            remote_task_id=task_id,
            status=_task_status(response),
            metadata={
                "queued_ahead": response.get("queued_ahead"),
                "protocol": MINERU_TASKS_PROTOCOL,
            },
        )

    def _submit_v1(self, request: ParserRequest) -> ParserSubmission:
        client = self.v1_client
        file_id = client.upload_file(Path(request.source_path))
        response = client.create_job(
            file_id,
            tier=self._v1_tier(request),
            ocr_mode=self._v1_ocr_mode(request),
            output_formats=["structured_content"],
        )
        job_id = str(response.get("job_id") or "")
        if not job_id:
            raise ParserProviderError(
                "MinerU Local did not return a job_id",
                provider_id=self.provider_id,
            )
        return ParserSubmission(
            provider_id=self.provider_id,
            remote_task_id=job_id,
            status=job_status(response),
            metadata={"protocol": MINERU_V1_PROTOCOL, "file_id": file_id},
        )

    def _v1_tier(self, request: ParserRequest) -> str:
        requested = str(
            request.options.get("tier") or self.config.tier or ""
        ).strip().lower()
        if requested:
            return tier_for_backend(requested)
        backend = str(request.options.get("backend") or self.config.backend)
        return tier_for_backend(backend)

    def _v1_ocr_mode(self, request: ParserRequest) -> str:
        mode = str(
            request.options.get("parse_method") or self.config.parse_method or "auto"
        ).strip().lower()
        return mode if mode in {"auto", "txt", "ocr"} else "auto"

    # ── polling and results ──────────────────────────────────────────

    def poll(
        self,
        remote_task_id: str,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserPollResult:
        if self.resolve_protocol() == MINERU_V1_PROTOCOL:
            response = self.v1_client.job(remote_task_id)
            return ParserPollResult(
                status=job_status(response),
                raw_status=response,
                progress=_v1_progress(response),
                message=_v1_message(response),
            )
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
        if self.resolve_protocol() == MINERU_V1_PROTOCOL:
            job = self.v1_client.job(submission.remote_task_id)
            file_id = structured_content_file_id(job)
            payload = self.v1_client.file_content(file_id)
            return decode_structured_content(payload)
        return self.client.task_result(submission.remote_task_id)

    def normalize_result(
        self, raw_result: object, request: ParserRequest
    ) -> NormalizedParseResult:
        if _is_structured_content(raw_result):
            return self._normalize_v1(raw_result, request)
        return self._normalize_tasks(raw_result, request)

    def _normalize_tasks(
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
                    text_level=item.get("text_level"),
                    provenance={"mineru_local_item_index": item_index},
                )
            )
        return self._assemble(blocks_by_page, request, MINERU_TASKS_PROTOCOL)

    def _normalize_v1(
        self, raw_result: object, request: ParserRequest
    ) -> NormalizedParseResult:
        assert isinstance(raw_result, Mapping)
        grouped = iter_page_blocks(raw_result)
        blocks_by_page: Dict[int, list[NormalizedBlock]] = {
            index: [] for index in range(request.page_count)
        }
        for local_page, blocks in grouped.items():
            if local_page not in blocks_by_page:
                continue
            for item_index, block in enumerate(blocks):
                text = block_text(block).strip()
                if not text:
                    continue
                normalized_bbox = block_bbox(block)
                provenance: Dict[str, object] = {
                    "mineru_local_item_index": item_index,
                    "mineru_local_protocol": MINERU_V1_PROTOCOL,
                }
                if normalized_bbox is not None:
                    # MinerU 4.x reports 0..1 boxes; keep the citation canvas
                    # identical to 3.x documents and publish the exact
                    # normalized box alongside it.
                    provenance["bbox_normalized"] = list(normalized_bbox)
                blocks_by_page[local_page].append(
                    NormalizedBlock(
                        text=text,
                        block_type=str(block.get("type") or "") or None,
                        bbox=(
                            scaled_bbox(normalized_bbox)
                            if normalized_bbox is not None
                            else None
                        ),
                        reading_order=len(blocks_by_page[local_page]),
                        text_level=block_text_level(block),
                        provenance=provenance,
                    )
                )
        return self._assemble(blocks_by_page, request, MINERU_V1_PROTOCOL)

    def _assemble(
        self,
        blocks_by_page: Mapping[int, Sequence[NormalizedBlock]],
        request: ParserRequest,
        protocol: str,
    ) -> NormalizedParseResult:
        pages = tuple(
            NormalizedPage(
                physical_pdf_page=request.global_page_offset + local_page + 1,
                text="\n".join(block.text for block in blocks),
                blocks=tuple(blocks),
                parser_provenance={
                    "provider": self.provider_id,
                    "backend": self.config.backend,
                    "protocol": protocol,
                    "local_page_index": local_page,
                    "global_page_offset": request.global_page_offset,
                },
            )
            for local_page, blocks in sorted(blocks_by_page.items())
        )
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=self.config.backend,
            pages=pages,
            parser_version=self._detected_version or None,
            provenance={"endpoint": self.config.endpoint, "protocol": protocol},
        )


def _bool_text(value: object) -> str:
    return "true" if bool(value) else "false"


def _optional_float(value: object) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _v1_progress(response: Mapping[str, object]) -> Optional[float]:
    progress = response.get("progress")
    if not isinstance(progress, Mapping):
        return None
    try:
        total = float(progress.get("total") or 0)
        completed = float(progress.get("completed") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return max(0.0, min(1.0, completed / total))


def _v1_message(response: Mapping[str, object]) -> Optional[str]:
    files = response.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return None
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        error = entry.get("error")
        if isinstance(error, Mapping):
            message = str(error.get("message") or "").strip()
            if message:
                return message
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


def _is_structured_content(value: object) -> bool:
    """Recognize a MinerU 4.x ``structured_content`` payload."""

    if not isinstance(value, Mapping):
        return False
    pages = value.get("pages")
    if not isinstance(pages, list):
        return False
    return all(
        isinstance(page, Mapping) and "page_idx" in page and "blocks" in page
        for page in pages
    )


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
