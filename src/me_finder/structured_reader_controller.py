"""Transport-neutral JSON responses for structured document reading."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .structured_reader import (
    CitationPositionNotFound,
    InvalidCitationRange,
    InvalidPagination,
    InvalidSourceId,
    SourceNotFound,
    StructuredReaderError,
    UnsupportedSourceType,
)


StructuredReaderResponse = Tuple[int, Dict[str, object]]
ReadyOperation = Callable[[Path], Dict[str, object]]
ReadyRunner = Callable[[ReadyOperation], Optional[Dict[str, object]]]
ReaderOperation = Callable[..., Dict[str, object]]
ExceptionLogger = Callable[[str], None]


class StructuredReaderController:
    """Validate reader requests and map application failures to JSON."""

    def __init__(
        self,
        run_when_ready: ReadyRunner,
        *,
        get_window: ReaderOperation,
        get_citation: ReaderOperation,
        log_exception: ExceptionLogger,
    ) -> None:
        self._run_when_ready = run_when_ready
        self._get_window = get_window
        self._get_citation = get_citation
        self._log_exception = log_exception

    def pages(
        self,
        params: Mapping[str, Sequence[object]],
    ) -> StructuredReaderResponse:
        source_ids = params.get("source_id", [])
        starts = params.get("start", ["0"])
        counts = params.get("count", ["20"])
        if len(source_ids) != 1 or len(starts) != 1 or len(counts) != 1:
            return 400, {
                "error": (
                    "source_id 必须提供一次，start 和 count "
                    "最多各提供一次。"
                )
            }

        try:
            result = self._run_when_ready(
                lambda database_path: self._get_window(
                    database_path,
                    source_ids[0],
                    start=starts[0],
                    count=counts[0],
                )
            )
        except (
            InvalidPagination,
            InvalidSourceId,
            UnsupportedSourceType,
        ) as exc:
            return 400, {"error": str(exc)}
        except SourceNotFound as exc:
            return 404, {"error": str(exc)}
        except (OSError, sqlite3.Error, StructuredReaderError):
            self._log_exception("structured reader data request failed")
            return 500, {
                "error": "结构化阅读数据读取失败，请稍后重试。"
            }

        if result is None:
            return 503, {
                "error": "索引正在重建，请稍候再打开结构化阅读。"
            }
        return 200, result

    def citation(self, payload: object) -> StructuredReaderResponse:
        if not isinstance(payload, dict):
            return 400, {"error": "引文请求必须是 JSON 对象。"}

        allowed_fields = {
            "source_id",
            "start_anchor_id",
            "end_anchor_id",
        }
        if set(payload) - allowed_fields:
            return 400, {"error": "引文请求包含不支持的字段。"}
        if set(payload) != allowed_fields:
            return 400, {
                "error": (
                    "source_id、start_anchor_id 和 end_anchor_id "
                    "必须各提供一次。"
                )
            }

        try:
            result = self._run_when_ready(
                lambda database_path: self._get_citation(
                    database_path,
                    payload["source_id"],
                    start_anchor_id=payload["start_anchor_id"],
                    end_anchor_id=payload["end_anchor_id"],
                )
            )
        except (
            InvalidCitationRange,
            InvalidPagination,
            InvalidSourceId,
            UnsupportedSourceType,
        ) as exc:
            return 400, {"error": str(exc)}
        except (CitationPositionNotFound, SourceNotFound) as exc:
            return 404, {"error": str(exc)}
        except (OSError, sqlite3.Error, StructuredReaderError):
            self._log_exception("structured reader citation request failed")
            return 500, {
                "error": "结构化阅读引文生成失败，请稍后重试。"
            }

        if result is None:
            return 503, {
                "error": "索引正在重建，请稍候再生成引文。"
            }
        return 200, result
