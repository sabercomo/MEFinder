"""ParserProvider adapter for the existing MinerU Cloud precision client."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from .mineru_api import (
    MinerUClient,
    MinerUConfig,
    MinerUError,
    extract_upload_url,
    extract_zip,
    load_mineru_config,
)
from .pdf_extractors import find_mineru_content_list
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


MINERU_CLOUD_PROVIDER_ID = "mineru-cloud"
MINERU_CLOUD_MAX_BYTES_PER_FILE = 200 * 1024 * 1024


class MinerUCloudProvider(ParserProvider):
    """Expose current MinerU v4 submit/status/download behavior via the contract."""

    provider_id = MINERU_CLOUD_PROVIDER_ID

    def __init__(
        self,
        *,
        config: Optional[MinerUConfig] = None,
        config_path: Optional[Path] = None,
        client: Optional[MinerUClient] = None,
        client_factory: Callable[[MinerUConfig], MinerUClient] = MinerUClient,
        max_pages_per_file: int = 200,
        max_bytes_per_file: Optional[int] = MINERU_CLOUD_MAX_BYTES_PER_FILE,
        max_concurrency: int = 1,
        supported_models: tuple[str, ...] = ("vlm",),
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._fixed_client = client
        self._client_factory = client_factory
        self._capabilities = ProviderCapabilities(
            max_pages_per_file=max_pages_per_file,
            max_bytes_per_file=max_bytes_per_file,
            max_concurrency=max_concurrency,
            supports_scanned_pdf=True,
            supports_bbox=True,
            supports_page_ranges=True,
            supports_async_jobs=True,
            # urllib uploads are made from a bounded file object after Commit 3.
            supports_stream_upload=True,
            supported_models=supported_models,
            optional_limits={"api": "precision-v4"},
        )

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def _client(self, credential: Optional[ParserCredential]) -> MinerUClient:
        if credential is not None:
            base = self._base_config()
            return self._client_factory(
                MinerUConfig(
                    token=credential.secret,
                    api_base=str(credential.metadata.get("api_base") or base.api_base),
                    use_env_proxy=bool(
                        credential.metadata.get("use_env_proxy", base.use_env_proxy)
                    ),
                )
            )
        if self._fixed_client is not None:
            return self._fixed_client
        return self._client_factory(self._base_config())

    def _base_config(self) -> MinerUConfig:
        if self._config is not None:
            return self._config
        if self._config_path is not None:
            return load_mineru_config(self._config_path)
        raise ParserProviderError(
            "MinerU Cloud provider has no configured credential",
            provider_id=self.provider_id,
            authentication_failed=True,
        )

    def submit(
        self,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserSubmission:
        request = self.prepare(request)
        options = dict(request.options)
        file_spec: Dict[str, object] = {
            "name": request.source_path.name,
            "data_id": str(options.get("data_id") or request.document_id),
            "is_ocr": bool(options.get("is_ocr", True)),
        }
        # Retained only for backwards-compatible callers.  Physical slices do
        # not need a page range and the large-document engine never supplies it.
        if options.get("page_ranges"):
            file_spec["page_ranges"] = str(options["page_ranges"])
        client = self._client(credential)
        try:
            response = client.apply_upload_urls(
                [file_spec],
                model_version=str(request.model or options.get("model_version") or "vlm"),
                language=str(options.get("language") or "ch"),
                enable_table=bool(options.get("enable_table", True)),
                enable_formula=bool(options.get("enable_formula", True)),
            )
            data = response.get("data") or {}
            remote_task_id = str(data.get("batch_id") or "")
            urls = data.get("file_urls") or []
            if not remote_task_id or not urls:
                raise MinerUError("MinerU did not return a batch id and upload URL.")
            upload_status = client.upload_file(
                extract_upload_url(urls[0]), request.source_path
            )
        except Exception as exc:
            raise self._provider_error(exc) from exc
        return ParserSubmission(
            provider_id=self.provider_id,
            remote_task_id=remote_task_id,
            status=ParserTaskStatus.SUBMITTED,
            metadata={"upload_status": upload_status, "data_id": file_spec["data_id"]},
        )

    def poll(
        self,
        remote_task_id: str,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserPollResult:
        try:
            raw = self._client(credential).batch_status(remote_task_id)
        except Exception as exc:
            raise self._provider_error(exc) from exc
        item = _first_extract_result(raw)
        state = str(item.get("state") or item.get("status") or "unknown").lower()
        if state in {"done", "completed", "success"}:
            status = ParserTaskStatus.COMPLETED
        elif state in {"failed", "error"}:
            status = ParserTaskStatus.PERMANENT_FAILURE
        elif state in {"cancelled", "canceled"}:
            status = ParserTaskStatus.CANCELLED
        else:
            status = ParserTaskStatus.WAITING
        progress = item.get("extract_progress") or item.get("progress")
        try:
            parsed_progress = float(progress) if progress is not None else None
        except (TypeError, ValueError):
            parsed_progress = None
        return ParserPollResult(
            status=status,
            raw_status=raw,
            progress=parsed_progress,
            message=str(item.get("err_msg") or "") or None,
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
                "MinerU result fetch requires a remote task id",
                provider_id=self.provider_id,
            )
        try:
            raw_status = self._client(credential).batch_status(
                submission.remote_task_id
            )
            item = _first_extract_result(raw_status)
            inline = item.get("content_list")
            if isinstance(inline, list):
                return {"content_list": inline, "status": raw_status}
            url = item.get("full_zip_url")
            if not url:
                raise MinerUError("No completed MinerU result is available yet.")
            if request.output_dir is None:
                raise MinerUError("A result output directory is required.")
            output_dir = Path(request.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            zip_path = output_dir / "mineru_result.zip"
            self._client(credential).download_url(str(url), zip_path)
            extract_zip(zip_path, output_dir)
            content_path = find_mineru_content_list(output_dir)
            content = json.loads(content_path.read_text(encoding="utf-8-sig"))
            return {
                "content_list": content,
                "status": raw_status,
                "result_dir": str(output_dir),
            }
        except Exception as exc:
            raise self._provider_error(exc) from exc

    def normalize_result(
        self,
        raw_result: object,
        request: ParserRequest,
    ) -> NormalizedParseResult:
        if isinstance(raw_result, list):
            content = raw_result
            metadata: Mapping[str, object] = {}
        elif isinstance(raw_result, Mapping):
            content = raw_result.get("content_list") or []
            metadata = raw_result
        else:
            raise ParserProviderError(
                "MinerU returned an unsupported result type",
                provider_id=self.provider_id,
            )
        if not isinstance(content, list):
            raise ParserProviderError(
                "MinerU content_list is malformed",
                provider_id=self.provider_id,
            )
        blocks_by_page: Dict[int, List[NormalizedBlock]] = {
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
            text = str(item.get("text") or "").strip()
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
                    provenance={"mineru_item_index": item_index},
                )
            )
        pages = []
        model = request.model or str(request.options.get("model_version") or "vlm")
        for local_page, blocks in blocks_by_page.items():
            physical_page = request.global_page_offset + local_page + 1
            pages.append(
                NormalizedPage(
                    physical_pdf_page=physical_page,
                    text="\n".join(block.text for block in blocks),
                    blocks=tuple(blocks),
                    parser_provenance={
                        "provider": self.provider_id,
                        "model": model,
                        "local_page_index": local_page,
                        "global_page_offset": request.global_page_offset,
                    },
                )
            )
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=model,
            pages=tuple(pages),
            provenance={
                key: value
                for key, value in (
                    ("result_dir", metadata.get("result_dir")),
                    ("source_sha256", request.source_sha256),
                )
                if value not in (None, "")
            },
        )

    def _provider_error(self, exc: Exception) -> ParserProviderError:
        if isinstance(exc, ParserProviderError):
            return exc
        text = str(exc)
        match = re.search(r"HTTP\s+(\d{3})", text, re.I)
        status_code = int(match.group(1)) if match else None
        authentication_failed = status_code in {401, 403}
        rate_limited = status_code == 429
        remote_missing = bool(
            isinstance(exc, MinerUError) and exc.retry_with_new_task
        )
        retryable = rate_limited or remote_missing or status_code is None
        return ParserProviderError(
            text or exc.__class__.__name__,
            provider_id=self.provider_id,
            retryable=retryable,
            authentication_failed=authentication_failed,
            rate_limited=rate_limited,
            remote_task_missing=remote_missing,
            status_code=status_code,
        )


def _first_extract_result(raw: Mapping[str, object]) -> Dict[str, object]:
    data = raw.get("data") or {}
    if not isinstance(data, Mapping):
        return {}
    results = data.get("extract_result") or []
    if isinstance(results, Mapping):
        return dict(results)
    if isinstance(results, list) and results and isinstance(results[0], Mapping):
        return dict(results[0])
    return {}
