"""Transport-neutral JSON responses for read-only library operations."""

from __future__ import annotations

import json
import sqlite3
from typing import Callable, Dict, Iterable, Tuple

from .application.document_query_service import (
    DocumentQueryService,
    DocumentQueryUnavailable,
)
from .application.index_runtime import IndexRuntime


LibraryResponse = Tuple[int, Dict[str, object]]
ActiveSourceIds = Callable[[], Iterable[str]]


class LibraryQueryController:
    """Expose library projections without depending on an HTTP handler."""

    def __init__(
        self,
        document_queries: DocumentQueryService,
        index_runtime: IndexRuntime,
        *,
        additional_active_source_ids: ActiveSourceIds,
    ) -> None:
        self._document_queries = document_queries
        self._index_runtime = index_runtime
        self._additional_active_source_ids = additional_active_source_ids

    def index_metadata(self) -> LibraryResponse:
        return 200, self._index_runtime.metadata()

    def sources(self) -> LibraryResponse:
        return 200, self._index_runtime.catalog()

    def library(self, requested_view: str = "") -> LibraryResponse:
        try:
            active = self._additional_active_source_ids()
            if requested_view == "summary":
                payload = self._document_queries.library_summary(
                    additional_active_source_ids=active
                )
            else:
                payload = self._document_queries.library_data(
                    additional_active_source_ids=active
                )
        except DocumentQueryUnavailable as exc:
            return 503, {"error": str(exc)}
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            return 500, {"error": f"文献库加载失败：{exc}"}
        return 200, payload

    def calibration_library(self) -> LibraryResponse:
        try:
            payload = self._document_queries.calibration_library_data(
                additional_active_source_ids=(
                    self._additional_active_source_ids()
                )
            )
        except DocumentQueryUnavailable as exc:
            return 503, {"error": str(exc)}
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            return 500, {
                "error": f"页码校准文献加载失败：{exc}"
            }
        return 200, payload

    def document(self, source_file_id: str) -> LibraryResponse:
        try:
            detail = self._document_queries.library_detail(
                source_file_id,
                additional_active_source_ids=(
                    self._additional_active_source_ids()
                ),
            )
        except DocumentQueryUnavailable as exc:
            return 503, {"error": str(exc)}
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            return 500, {"error": f"文献详情加载失败：{exc}"}
        if detail is None:
            return 404, {"error": "文献不存在或已被移除。"}
        return 200, detail
