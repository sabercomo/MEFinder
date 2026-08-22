"""Search use case and transport-neutral request DTO."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence, Union


SearchLimit = Union[int, str]


class SearchEnginePort(Protocol):
    def search(
        self,
        query: str,
        mode: str = "auto",
        limit: SearchLimit | None = 10,
        source_type: str = "all",
        source_file_id: Optional[str] = None,
        source_file_ids: Optional[Sequence[str]] = None,
    ) -> dict[str, object]:
        ...


@dataclass(frozen=True)
class SearchRequest:
    query: str
    mode: str = "auto"
    limit: SearchLimit = 10
    source_type: str = "all"
    source_file_id: str | None = None
    source_file_ids: tuple[str, ...] | None = None

    @classmethod
    def from_payload(cls, payload: object) -> "SearchRequest":
        if not isinstance(payload, Mapping):
            raise ValueError("搜索请求必须是 JSON 对象。")
        requested_limit = payload.get("limit", 10)
        limit: SearchLimit
        if str(requested_limit).strip().lower() in {"all", "0"}:
            limit = "all"
        else:
            try:
                limit = int(requested_limit)
            except (TypeError, ValueError):
                limit = 10
        source_file_id = str(payload.get("source_file_id") or "").strip() or None
        raw_ids = payload.get("source_file_ids", None)
        if raw_ids is None:
            source_file_ids: tuple[str, ...] | None = None
        elif isinstance(raw_ids, (list, tuple)):
            # Preserve an empty list as an explicit empty scope (() != None).
            source_file_ids = tuple(
                str(item).strip() for item in raw_ids if str(item).strip()
            )
        else:
            raise ValueError("source_file_ids 必须是数组。")
        if source_file_id and source_file_ids is not None:
            # Single-source and set scope are mutually exclusive; refuse rather
            # than silently choosing one.
            raise ValueError("source_file_id 与 source_file_ids 不能同时指定。")
        return cls(
            query=str(payload.get("query") or ""),
            mode=str(payload.get("mode") or "auto"),
            limit=limit,
            source_type=str(payload.get("source_type") or "all"),
            source_file_id=source_file_id,
            source_file_ids=source_file_ids,
        )


class SearchService:
    """Execute a search without knowing how the request was transported."""

    @staticmethod
    def execute(
        engine: SearchEnginePort,
        request: SearchRequest,
    ) -> dict[str, object]:
        return engine.search(
            request.query,
            request.mode,
            request.limit,
            request.source_type,
            request.source_file_id,
            request.source_file_ids,
        )
