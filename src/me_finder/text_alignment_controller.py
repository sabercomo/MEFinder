"""Transport-neutral endpoints for pair alignment and reader location."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .application.text_alignment_coordinator import (
    TextAlignmentCoordinator,
    TextAlignmentFailed,
    TextAlignmentRejected,
)
from .text_alignment import (
    AlignmentNotFound,
    InvalidAlignmentRequest,
    TextAlignmentError,
)


AlignmentResponse = Tuple[int, Dict[str, object]]
ReadyOperation = Callable[[Path], Dict[str, object]]
ReadyRunner = Callable[[ReadyOperation], Optional[Dict[str, object]]]
ReadOperation = Callable[..., Dict[str, object]]


class TextAlignmentController:
    def __init__(
        self,
        coordinator: TextAlignmentCoordinator,
        run_when_ready: ReadyRunner,
        *,
        list_targets: ReadOperation,
        locate: ReadOperation,
        log_exception: Callable[[str], None],
    ) -> None:
        self._coordinator = coordinator
        self._run_when_ready = run_when_ready
        self._list_targets = list_targets
        self._locate = locate
        self._log_exception = log_exception

    def generate(self, payload: object) -> AlignmentResponse:
        required = {
            "document_group_id",
            "pivot_source_file_id",
            "target_source_file_id",
        }
        if (
            not isinstance(payload, Mapping)
            or not required.issubset(payload)
            or not set(payload).issubset(required | {"force"})
            or not isinstance(payload.get("force", False), bool)
        ):
            return 400, {"error": "自动对齐请求字段无效。"}
        try:
            result = self._coordinator.generate(
                payload["document_group_id"],
                payload["pivot_source_file_id"],
                payload["target_source_file_id"],
                force=payload.get("force", False),
            )
        except TextAlignmentRejected as exc:
            return 400, {"error": str(exc)}
        except TextAlignmentFailed:
            self._log_exception("automatic text alignment failed")
            return 500, {"error": "自动对齐失败，请检查两本文献的解析文本。"}
        return 200, {
            "ok": True,
            "result": result,
            "event": "library_changed",
        }

    def targets(
        self, params: Mapping[str, Sequence[object]]
    ) -> AlignmentResponse:
        source_ids = params.get("source_id", [])
        if len(source_ids) != 1 or set(params) != {"source_id"}:
            return 400, {"error": "source_id 必须提供一次。"}
        return self._read(
            lambda path: self._list_targets(path, source_ids[0]),
            unavailable="索引正在重建，请稍候再读取对齐版本。",
            failure_message="对齐版本读取失败，请稍后重试。",
            log_message="alignment targets request failed",
        )

    def locate(self, payload: object) -> AlignmentResponse:
        required = {
            "source_file_id",
            "target_source_file_id",
            "start_page_index",
            "end_page_index",
            "start_offset",
            "end_offset",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            return 400, {"error": "跨版本定位请求字段无效。"}
        try:
            result = self._run_when_ready(
                lambda path: self._locate(
                    path,
                    payload["source_file_id"],
                    payload["target_source_file_id"],
                    start_page_index=payload["start_page_index"],
                    end_page_index=payload["end_page_index"],
                    start_offset=payload["start_offset"],
                    end_offset=payload["end_offset"],
                )
            )
        except InvalidAlignmentRequest as exc:
            return 400, {"error": str(exc)}
        except AlignmentNotFound as exc:
            return 404, {"error": str(exc)}
        except (OSError, sqlite3.Error, TextAlignmentError):
            self._log_exception("cross-version reader location failed")
            return 500, {"error": "跨版本定位失败，请稍后重试。"}
        if result is None:
            return 503, {"error": "索引正在重建，请稍候再定位。"}
        return 200, result

    def _read(
        self,
        operation: ReadyOperation,
        *,
        unavailable: str,
        failure_message: str,
        log_message: str,
    ) -> AlignmentResponse:
        try:
            result = self._run_when_ready(operation)
        except InvalidAlignmentRequest as exc:
            return 400, {"error": str(exc)}
        except (OSError, sqlite3.Error, TextAlignmentError):
            self._log_exception(log_message)
            return 500, {"error": failure_message}
        if result is None:
            return 503, {"error": unavailable}
        return 200, result
