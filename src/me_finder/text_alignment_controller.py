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
    TextAlignmentComponentUnavailable,
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
        read_body_ranges: ReadOperation,
        read_body_range_segments: ReadOperation,
        log_exception: Callable[[str], None],
    ) -> None:
        self._coordinator = coordinator
        self._run_when_ready = run_when_ready
        self._list_targets = list_targets
        self._locate = locate
        self._read_body_ranges = read_body_ranges
        self._read_body_range_segments = read_body_range_segments
        self._log_exception = log_exception
        self._job_lock = threading.Lock()
        self._job_id: str | None = None
        self._job_payload: Dict[str, object] | None = None
        self._job_response: AlignmentResponse | None = None

    @staticmethod
    def _valid_body_ranges(value: object) -> bool:
        """Accept only a half-open segment interval per side.

        The exact bounds are re-validated against the live segmentation in
        ``generate_alignment``; this only keeps malformed payloads out.
        """

        if not isinstance(value, Mapping) or set(value) != {"pivot", "target"}:
            return False
        return all(
            isinstance(bounds, list)
            and len(bounds) == 2
            and all(type(number) is int for number in bounds)
            and 0 <= bounds[0] < bounds[1]
            for bounds in value.values()
        )

    @classmethod
    def _valid_generate_payload(cls, payload: object) -> bool:
        required = {
            "document_group_id",
            "pivot_source_file_id",
            "target_source_file_id",
        }
        optional = {"force", "reviewed_body_ranges", "expected_segment_set_ids"}
        return (
            isinstance(payload, Mapping)
            and required.issubset(payload)
            and set(payload).issubset(required | optional)
            and isinstance(payload.get("force", False), bool)
            and (("reviewed_body_ranges" in payload) == ("expected_segment_set_ids" in payload))
            and (
                "reviewed_body_ranges" not in payload
                or (
                    cls._valid_body_ranges(payload["reviewed_body_ranges"])
                    and isinstance(payload["expected_segment_set_ids"], Mapping)
                    and set(payload["expected_segment_set_ids"]) == {"pivot", "target"}
                    and all(isinstance(value, str) and value.strip()
                            for value in payload["expected_segment_set_ids"].values())
                )
            )
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

    def current(self, _params: object = None) -> AlignmentResponse:
        """Report the in-flight run, so a reloaded page can show it again.

        Only the run's identity is known here; the compute worker does not
        report batch progress, so no percentage is invented.
        """
        with self._job_lock:
            if self._job_id is None or self._job_response is not None:
                return 200, {"running": False}
            payload = dict(self._job_payload or {})
            return 200, {
                "running": True,
                "job_id": self._job_id,
                "document_group_id": payload.get("document_group_id"),
                "pivot_source_file_id": payload.get("pivot_source_file_id"),
                "target_source_file_id": payload.get("target_source_file_id"),
                "force": bool(payload.get("force", False)),
            }

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
                reviewed_body_ranges=payload.get("reviewed_body_ranges"),
                expected_segment_set_ids=payload.get("expected_segment_set_ids"),
            )
        except TextAlignmentCancelled:
            LOGGER.info("text alignment cancelled by user")
            return 200, {"ok": False, "cancelled": True}
        except TextAlignmentRejected as exc:
            # A rejection is the user-visible "失败" reason; record it so the
            # cause is diagnosable from the log, not only shown once in a toast.
            LOGGER.warning("text alignment rejected: %s", exc)
            return 400, {"error": str(exc)}
        except TextAlignmentComponentUnavailable as exc:
            # A missing / incompatible / unstartable compute runtime is a
            # distinct, user-actionable condition — surface its specific reason
            # instead of the misleading "检查解析文本". Must precede the generic
            # TextAlignmentFailed handler (this is a subclass).
            LOGGER.warning("alignment compute component unavailable: %s", exc)
            return 503, {"error": str(exc), "component_unavailable": True}
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

    def body_ranges(self, payload: object) -> AlignmentResponse:
        """Describe both books' current body range for the review screen.

        A POST because a pair that has never been aligned is segmented here;
        nothing is aligned and no source file is re-parsed.
        """

        required = {
            "document_group_id",
            "pivot_source_file_id",
            "target_source_file_id",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            return 400, {"error": "正文范围请求字段无效。"}
        return self._read(
            lambda path: self._read_body_ranges(
                path,
                payload["document_group_id"],
                payload["pivot_source_file_id"],
                payload["target_source_file_id"],
            ),
            unavailable="索引正在重建，请稍候再读取正文范围。",
            failure_message="正文范围读取失败，请稍后重试。",
            log_message="body range request failed",
        )

    def body_range_segments(
        self, params: Mapping[str, Sequence[object]]
    ) -> AlignmentResponse:
        allowed = {"source_id", "segment_set_id", "start", "count", "pdf_page"}
        if not set(params).issubset(allowed) or not {
            "source_id",
            "segment_set_id",
        }.issubset(params):
            return 400, {"error": "正文范围文本段请求字段无效。"}
        if any(len(values) != 1 for values in params.values()):
            return 400, {"error": "正文范围文本段请求字段无效。"}
        return self._read(
            lambda path: self._read_body_range_segments(
                path,
                params["source_id"][0],
                params["segment_set_id"][0],
                start=params.get("start", ["0"])[0],
                count=params.get("count", ["9"])[0],
                pdf_page=(
                    params["pdf_page"][0] if "pdf_page" in params else None
                ),
            ),
            unavailable="索引正在重建，请稍候再读取文本段。",
            failure_message="文本段读取失败，请稍后重试。",
            log_message="body range segment window request failed",
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
