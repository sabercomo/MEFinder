"""HTTP-facing full-text search (``POST /api/search``).

Moved out of the transport unchanged: a ``document_group_id`` scope is resolved
to member ``source_file_ids`` here, so ``SearchService`` / ``search.py`` never
see DocumentGroups; a lock timeout maps to a retriable 503 instead of an empty
result that would falsely read as "no hits".
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .application import SearchRequest
from .http_route_table import RoutePair


class SearchController:
    def __init__(
        self,
        *,
        index_runtime: Any,
        index_path: Path,
        resolve_document_group_source_ids: Callable[[object, Path], list[str]],
        document_group_not_found_error: type[Exception],
    ) -> None:
        self._index_runtime = index_runtime
        self._index_path = index_path
        self._resolve_group = resolve_document_group_source_ids
        self._group_not_found = document_group_not_found_error

    def search(self, payload: object) -> tuple[int, object]:
        try:
            if isinstance(payload, dict) and str(
                payload.get("document_group_id") or ""
            ).strip():
                if str(payload.get("source_file_id") or "").strip():
                    raise ValueError(
                        "source_file_id 与 document_group_id 不能同时指定。"
                    )
                member_ids = self._resolve_group(
                    payload["document_group_id"], self._index_path
                )
                payload = dict(payload)
                payload.pop("document_group_id", None)
                payload["source_file_ids"] = member_ids
            request = SearchRequest.from_payload(payload)
        except self._group_not_found as exc:
            return 404, {"error": str(exc)}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        try:
            result = self._index_runtime.search(request)
        except sqlite3.OperationalError as exc:
            # A read that sat out the full busy_timeout on a concurrent writer's
            # lock surfaces as "database is locked"/"busy": retriable 503.  Any
            # other operational error is a real fault: a logged 500.
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                return 503, {
                    "error": "索引正忙（写入未在超时内完成），请稍候重试。",
                    "retriable": True,
                }
            logging.exception("search query failed")
            return 500, {"error": "搜索失败，请查看 desktop.log。"}
        if result is None:
            return 503, {"error": "索引正在重建，请稍候再搜索。"}
        return 200, result


def assemble_search_routes(controller: SearchController) -> RoutePair:
    return {}, {"/api/search": controller.search}
