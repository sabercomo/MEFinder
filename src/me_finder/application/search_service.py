"""Search use case and transport-neutral request DTO."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Union


SearchLimit = Union[int, str]


class SearchEnginePort(Protocol):
    def search(
        self,
        query: str,
        mode: str = "auto",
        limit: SearchLimit | None = 10,
        source_type: str = "all",
        source_file_id: Optional[str] = None,
        scope_type: str = "all",
        scope_id: Optional[str] = None,
    ) -> dict[str, object]:
        ...


@dataclass(frozen=True)
class SearchRequest:
    query: str
    mode: str = "auto"
    limit: SearchLimit = 10
    source_type: str = "all"
    source_file_id: str | None = None
    scope_type: str = "all"
    scope_id: str | None = None

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
        scope_type = str(payload.get("scope_type") or "all").strip()
        if scope_type not in {"all", "root", "folder", "document_group"}:
            raise ValueError("搜索范围无效。")
        scope_id = str(payload.get("scope_id") or "").strip() or None
        if scope_type in {"folder", "document_group"} and not scope_id:
            raise ValueError("搜索范围缺少 scope_id。")
        if scope_type in {"all", "root"}:
            scope_id = None
        return cls(
            query=str(payload.get("query") or ""),
            mode=str(payload.get("mode") or "auto"),
            limit=limit,
            source_type=str(payload.get("source_type") or "all"),
            source_file_id=source_file_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )


class SearchService:
    """Execute a search without knowing how the request was transported."""

    @staticmethod
    def execute(
        engine: SearchEnginePort,
        request: SearchRequest,
    ) -> dict[str, object]:
        if request.scope_type == "all" and request.scope_id is None:
            return engine.search(
                request.query,
                request.mode,
                request.limit,
                request.source_type,
                request.source_file_id,
            )
        return engine.search(
            request.query,
            request.mode,
            request.limit,
            request.source_type,
            request.source_file_id,
            request.scope_type,
            request.scope_id,
        )
