"""Small provider contract for parser-neutral document job orchestration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple


class ParserTaskStatus(str, Enum):
    QUEUED = "queued"
    SUBMITTED = "submitted"
    WAITING = "waiting"
    COMPLETED = "completed"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Limits and features that the scheduler may act on.

    Provider limits live here (or in provider configuration), never in the
    large-document planner.  ``None`` means the provider did not declare a
    limit; it does not mean that the upstream service is unlimited.
    """

    max_pages_per_file: Optional[int]
    max_bytes_per_file: Optional[int]
    max_concurrency: int = 1
    supports_scanned_pdf: bool = True
    supports_bbox: bool = False
    supports_page_ranges: bool = False
    supports_async_jobs: bool = True
    supports_stream_upload: bool = False
    supported_models: Tuple[str, ...] = ()
    optional_limits: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("max_pages_per_file", "max_bytes_per_file"):
            value = getattr(self, name)
            if value is not None and int(value) < 1:
                raise ValueError(f"{name} must be positive or None")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")


@dataclass(frozen=True)
class ParserCredential:
    """One resolved secret; its repr intentionally never includes the secret."""

    credential_id: str
    secret: str = field(repr=False)
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ParserRequest:
    source_path: Path
    source_sha256: str
    document_id: str
    page_start: int
    page_end: int
    global_page_offset: int
    output_dir: Optional[Path] = None
    model: Optional[str] = None
    options: Mapping[str, object] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1

    def __post_init__(self) -> None:
        if self.page_start < 1 or self.page_end < self.page_start:
            raise ValueError("invalid parser page range")
        if self.global_page_offset < 0:
            raise ValueError("global_page_offset cannot be negative")


@dataclass(frozen=True)
class ParserSubmission:
    provider_id: str
    remote_task_id: Optional[str]
    status: ParserTaskStatus
    raw_result: object = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ParserPollResult:
    status: ParserTaskStatus
    raw_status: object = None
    progress: Optional[float] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class NormalizedBlock:
    text: str
    block_type: Optional[str] = None
    bbox: Optional[Sequence[float]] = None
    reading_order: Optional[int] = None
    text_level: Optional[int] = None
    provenance: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "text": self.text,
            "type": self.block_type,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "reading_order": self.reading_order,
            "text_level": self.text_level,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class NormalizedPage:
    physical_pdf_page: int
    text: str
    blocks: Tuple[NormalizedBlock, ...] = ()
    logical_page: Optional[object] = None
    parser_provenance: Mapping[str, object] = field(default_factory=dict)
    warnings: Tuple[object, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "physical_pdf_page": self.physical_pdf_page,
            "logical_page": self.logical_page,
            "text": self.text,
            "blocks": [block.to_dict() for block in self.blocks],
            "parser_provenance": dict(self.parser_provenance),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class NormalizedParseResult:
    provider_id: str
    model: Optional[str]
    pages: Tuple[NormalizedPage, ...]
    parser_version: Optional[str] = None
    provenance: Mapping[str, object] = field(default_factory=dict)
    warnings: Tuple[object, ...] = ()

    def __post_init__(self) -> None:
        page_numbers = [page.physical_pdf_page for page in self.pages]
        if page_numbers != sorted(page_numbers) or len(page_numbers) != len(set(page_numbers)):
            raise ValueError("normalized pages must be unique and ordered")


class ParserProviderError(RuntimeError):
    """Provider-neutral failure classification consumed by schedulers."""

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        retryable: bool = False,
        authentication_failed: bool = False,
        rate_limited: bool = False,
        remote_task_missing: bool = False,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.retryable = bool(retryable)
        self.authentication_failed = bool(authentication_failed)
        self.rate_limited = bool(rate_limited)
        self.remote_task_missing = bool(remote_task_missing)
        self.status_code = status_code


class ParserProvider(ABC):
    """The only parser-specific interface known by the job engine."""

    provider_id: str

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    def prepare(self, request: ParserRequest) -> ParserRequest:
        """Validate one already-physical input slice before submission."""

        path = Path(request.source_path)
        if not path.is_file():
            raise ParserProviderError(
                "parser input file does not exist",
                provider_id=self.provider_id,
            )
        capabilities = self.capabilities()
        if (
            capabilities.max_pages_per_file is not None
            and request.page_count > capabilities.max_pages_per_file
        ):
            raise ParserProviderError(
                "parser input exceeds provider page capability",
                provider_id=self.provider_id,
            )
        if (
            capabilities.max_bytes_per_file is not None
            and path.stat().st_size > capabilities.max_bytes_per_file
        ):
            raise ParserProviderError(
                "parser input exceeds provider byte capability",
                provider_id=self.provider_id,
            )
        return request

    @abstractmethod
    def submit(
        self,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserSubmission:
        raise NotImplementedError

    @abstractmethod
    def poll(
        self,
        remote_task_id: str,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserPollResult:
        raise NotImplementedError

    @abstractmethod
    def fetch_result(
        self,
        submission: ParserSubmission,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> object:
        raise NotImplementedError

    def cancel(
        self,
        remote_task_id: str,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> bool:
        return False

    @abstractmethod
    def normalize_result(
        self,
        raw_result: object,
        request: ParserRequest,
    ) -> NormalizedParseResult:
        raise NotImplementedError
