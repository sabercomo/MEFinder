"""Qwen-OCR parser provider built on the existing OpenAI-compatible client.

The current Qwen3.5-OCR Responses API accepts PDFs up to 50 pages / 100 MB,
but direct PDF input requires a service-accessible file URL.  MEFinder keeps
local books private by rendering one page at a time and sending only that page
through the documented image input.  The same provider-specific 50/100 limits
remain configurable capabilities for physical slice planning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Callable,
    Iterable,
    Mapping,
    Optional,
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
from .vision_api import (
    OpenAICompatibleVisionClient,
    VisionProviderConfig,
)


QWEN_OCR_PROVIDER_ID = "qwen-ocr"
QWEN_OCR_SUPPORTED_MODELS = (
    "qwen3.5-ocr",
    "qwen-vl-ocr-latest",
    "qwen-vl-ocr-2025-11-20",
    "qwen-vl-ocr",
)


@dataclass(frozen=True)
class QwenOCRConfig:
    api_base: str
    api_key: str = field(repr=False)
    model: str = "qwen3.5-ocr"
    display_name: str = "Qwen OCR"
    use_env_proxy: bool = False
    max_pages_per_file: int = 50
    max_bytes_per_file: int = 100 * 1024 * 1024
    max_concurrency: int = 1
    render_longest_edge: int = 2200

    def __post_init__(self) -> None:
        if not self.api_base.startswith(("http://", "https://")):
            raise ValueError("Qwen OCR api_base must be an http(s) URL")
        if not self.model:
            raise ValueError("Qwen OCR model is required")
        if self.render_longest_edge < 256:
            raise ValueError("render_longest_edge is too small")


PageRenderer = Callable[[Path, int], Iterable[bytes]]
ClientFactory = Callable[[VisionProviderConfig], OpenAICompatibleVisionClient]


class QwenOCRProvider(ParserProvider):
    provider_id = QWEN_OCR_PROVIDER_ID

    def __init__(
        self,
        config: QwenOCRConfig,
        *,
        client: Optional[OpenAICompatibleVisionClient] = None,
        client_factory: ClientFactory = OpenAICompatibleVisionClient,
        page_renderer: Optional[PageRenderer] = None,
    ) -> None:
        self.config = config
        self._fixed_client = client
        self._client_factory = client_factory
        self._page_renderer = page_renderer or _render_pdf_pages
        self._capabilities = ProviderCapabilities(
            max_pages_per_file=config.max_pages_per_file,
            max_bytes_per_file=config.max_bytes_per_file,
            max_concurrency=config.max_concurrency,
            supports_scanned_pdf=True,
            supports_bbox=False,
            supports_page_ranges=False,
            supports_async_jobs=False,
            supports_stream_upload=False,
            supported_models=QWEN_OCR_SUPPORTED_MODELS,
            optional_limits={
                "input_mode": "page_image",
                "official_pdf_page_limit": 50,
                "official_pdf_byte_limit": 100 * 1024 * 1024,
            },
        )

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def _client(
        self, credential: Optional[ParserCredential]
    ) -> OpenAICompatibleVisionClient:
        if credential is None and self._fixed_client is not None:
            return self._fixed_client
        api_key = credential.secret if credential is not None else self.config.api_key
        return self._client_factory(
            VisionProviderConfig(
                provider_id=self.provider_id,
                name=self.config.display_name,
                api_base=self.config.api_base,
                api_key=api_key,
                model=self.config.model,
                use_env_proxy=self.config.use_env_proxy,
            )
        )

    def submit(
        self,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserSubmission:
        request = self.prepare(request)
        client = self._client(credential)
        pages = []
        try:
            for page_idx, image in enumerate(
                self._page_renderer(
                    request.source_path, self.config.render_longest_edge
                )
            ):
                if page_idx >= request.page_count:
                    raise ParserProviderError(
                        "Qwen renderer returned more pages than the physical slice",
                        provider_id=self.provider_id,
                    )
                text = client.extract_page(image, mime_type="image/png")
                if not isinstance(text, str):
                    raise ParserProviderError(
                        "Qwen OCR returned a non-text page response",
                        provider_id=self.provider_id,
                    )
                pages.append({"page_idx": page_idx, "text": text})
        except ParserProviderError:
            raise
        except Exception as exc:
            raise _qwen_error(exc) from exc
        if len(pages) != request.page_count:
            raise ParserProviderError(
                f"Qwen renderer returned {len(pages)} pages for a "
                f"{request.page_count}-page slice",
                provider_id=self.provider_id,
            )
        return ParserSubmission(
            provider_id=self.provider_id,
            remote_task_id=None,
            status=ParserTaskStatus.COMPLETED,
            raw_result={"pages": pages, "model": self.config.model},
        )

    def poll(
        self,
        remote_task_id: str,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserPollResult:
        return ParserPollResult(ParserTaskStatus.COMPLETED)

    def fetch_result(
        self,
        submission: ParserSubmission,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> object:
        if submission.raw_result is None:
            raise ParserProviderError(
                "synchronous Qwen submission contains no result",
                provider_id=self.provider_id,
            )
        return submission.raw_result

    def normalize_result(
        self, raw_result: object, request: ParserRequest
    ) -> NormalizedParseResult:
        if not isinstance(raw_result, Mapping) or not isinstance(
            raw_result.get("pages"), list
        ):
            raise ParserProviderError(
                "Qwen OCR response is malformed",
                provider_id=self.provider_id,
            )
        normalized_pages = []
        seen: set[int] = set()
        for item in raw_result["pages"]:
            if not isinstance(item, Mapping):
                raise ParserProviderError(
                    "Qwen OCR page response is malformed",
                    provider_id=self.provider_id,
                )
            try:
                local_page = int(item.get("page_idx"))
            except (TypeError, ValueError) as exc:
                raise ParserProviderError(
                    "Qwen OCR page index is invalid",
                    provider_id=self.provider_id,
                ) from exc
            if local_page in seen or local_page < 0 or local_page >= request.page_count:
                raise ParserProviderError(
                    "Qwen OCR page index is duplicated or out of range",
                    provider_id=self.provider_id,
                )
            seen.add(local_page)
            text = item.get("text")
            if not isinstance(text, str):
                raise ParserProviderError(
                    "Qwen OCR page text is invalid",
                    provider_id=self.provider_id,
                )
            raw_blocks = item.get("blocks")
            blocks = []
            if isinstance(raw_blocks, list):
                for block_index, raw_block in enumerate(raw_blocks):
                    if not isinstance(raw_block, Mapping):
                        continue
                    bbox = raw_block.get("bbox")
                    blocks.append(
                        NormalizedBlock(
                            text=str(raw_block.get("text") or ""),
                            block_type=str(raw_block.get("type") or "") or None,
                            bbox=(
                                tuple(bbox)
                                if isinstance(bbox, (list, tuple))
                                else None
                            ),
                            reading_order=block_index,
                            provenance={"qwen_block_index": block_index},
                        )
                    )
            normalized_pages.append(
                NormalizedPage(
                    physical_pdf_page=request.global_page_offset + local_page + 1,
                    text=text,
                    blocks=tuple(blocks),
                    parser_provenance={
                        "provider": self.provider_id,
                        "model": self.config.model,
                        "local_page_index": local_page,
                        "global_page_offset": request.global_page_offset,
                    },
                )
            )
        if seen != set(range(request.page_count)):
            raise ParserProviderError(
                "Qwen OCR response does not cover every slice page",
                provider_id=self.provider_id,
            )
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=str(raw_result.get("model") or self.config.model),
            pages=tuple(sorted(normalized_pages, key=lambda page: page.physical_pdf_page)),
            provenance={
                "api_base": self.config.api_base,
                "input_mode": "page_image",
            },
        )


def _render_pdf_pages(path: Path, longest_edge: int) -> Iterable[bytes]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise ParserProviderError(
            "PyMuPDF is required to render Qwen OCR input pages",
            provider_id=QWEN_OCR_PROVIDER_ID,
        ) from exc
    document = fitz.open(str(path))
    try:
        for page_index in range(len(document)):
            page = document.load_page(page_index)
            longest = max(float(page.rect.width), float(page.rect.height), 1.0)
            scale = min(3.0, max(1.0, float(longest_edge) / longest))
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            yield pixmap.tobytes("png")
    finally:
        document.close()


def _qwen_error(exc: Exception) -> ParserProviderError:
    if isinstance(exc, ParserProviderError):
        return exc
    text = str(exc) or exc.__class__.__name__
    match = re.search(r"HTTP\s+(\d{3})", text, re.I)
    status = int(match.group(1)) if match else None
    lower = text.casefold()
    timeout = "timeout" in lower or "timed out" in lower or "超时" in text
    network = "network" in lower or "网络" in text
    return ParserProviderError(
        text,
        provider_id=QWEN_OCR_PROVIDER_ID,
        retryable=bool(
            timeout
            or network
            or status in {408, 409, 425, 429}
            or (status is not None and status >= 500)
        ),
        authentication_failed=status in {401, 403},
        rate_limited=status == 429,
        status_code=status,
    )
