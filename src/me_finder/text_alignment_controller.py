"""Transport-neutral endpoints for pair alignment and reader location."""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

from .application.text_alignment_coordinator import (
    TextAlignmentCancelled,
    TextAlignmentCoordinator,
    TextAlignmentFailed,
    TextAlignmentRejected,
)
from .embedding_runtime import request_embedding_cancel
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
        self._job_lock = threading.Lock()
        self._job_id: str | None = None
        self._job_payload: Dict[str, object] | None = None
        self._job_response: AlignmentResponse | None = None

    @staticmethod
    def _valid_generate_payload(payload: object) -> bool:
        required = {
            "document_group_id",
            "pivot_source_file_id",
            "target_source_file_id",
        }
        return (
            isinstance(payload, Mapping)
            and required.issubset(payload)
            and set(payload).issubset(required | {"force"})
            and isinstance(payload.get("force", False), bool)
        )

    def start(self, payload: object) -> AlignmentResponse:
        """Start one background run without holding a browser request open."""
        if not self._valid_generate_payload(payload):
            return 400, {"error": "自动对齐请求字段无效。"}
        with self._job_lock:
            if self._job_id is not None and self._job_response is None:
                if payload != self._job_payload:
                    return 409, {"error": "已有译本正在对齐，请等待完成或取消后再试"}
                return 202, {"job_id": self._job_id, "status": "running"}
            self._job_id = uuid.uuid4().hex
            self._job_payload = dict(payload)
            self._job_response = None
            self._job_thread = threading.Thread(
                target=self._generate_job, args=(dict(payload),), daemon=True,
            )
            self._job_thread.start()
            return 202, {"job_id": self._job_id, "status": "running"}

    def _generate_job(self, payload: Dict[str, object]) -> None:
        try:
            response = self.generate(payload)
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            # Surface worker failures to the polling client instead of leaving
            # a dead worker permanently displayed as running.
            self._log_exception("background text alignment failed")
            response = 500, {"error": "自动对齐发生错误，请查看运行日志"}
        with self._job_lock:
            self._job_response = response

    def status(self, params: Mapping[str, Sequence[object]]) -> AlignmentResponse:
        """Return the active or most recently finished background run."""
        job_ids = params.get("job_id", [])
        if set(params) != {"job_id"} or len(job_ids) != 1:
            return 400, {"error": "job_id 必须提供一次"}
        with self._job_lock:
            if self._job_id is None or job_ids[0] != self._job_id:
                return 404, {"error": "对齐任务不存在，请刷新作品组查看已保存的结果"}
            return self._job_response or (202, {"job_id": self._job_id, "status": "running"})

    def generate(self, payload: object) -> AlignmentResponse:
        """Generate synchronously for existing API clients and the worker."""
        if not self._valid_generate_payload(payload):
            return 400, {"error": "自动对齐请求字段无效。"}
        LOGGER.info(
            "text alignment requested: group=%s pivot=%s target=%s force=%s",
            payload["document_group_id"],
            payload["pivot_source_file_id"],
            payload["target_source_file_id"],
            payload.get("force", False),
        )
        try:
            result = self._coordinator.generate(
                payload["document_group_id"],
                payload["pivot_source_file_id"],
                payload["target_source_file_id"],
                force=payload.get("force", False),
            )
        except TextAlignmentCancelled:
            LOGGER.info("text alignment cancelled by user")
            return 200, {"ok": False, "cancelled": True}
        except TextAlignmentRejected as exc:
            # A rejection is the user-visible "失败" reason; record it so the
            # cause is diagnosable from the log, not only shown once in a toast.
            LOGGER.warning("text alignment rejected: %s", exc)
            return 400, {"error": str(exc)}
        except TextAlignmentFailed:
            self._log_exception("automatic text alignment failed")
            return 500, {"error": "自动对齐失败，请检查两本文献的解析文本。"}
        if isinstance(result, Mapping):
            LOGGER.info(
                "text alignment done: accepted=%s rejected=%s unmatched=%s",
                result.get("accepted_link_count"),
                result.get("rejected_link_count"),
                result.get("unmatched_link_count"),
            )
        return 200, {
            "ok": True,
            "result": result,
            "event": "library_changed",
        }

    def cancel(self, payload: object) -> AlignmentResponse:
        """Ask an in-flight alignment to stop at the next batch boundary."""

        request_embedding_cancel()
        return 200, {"ok": True, "cancelled": True}

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
