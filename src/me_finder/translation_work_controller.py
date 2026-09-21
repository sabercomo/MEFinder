"""Transport-neutral endpoints for the translation-comparison workspace."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .text_alignment import (
    AlignmentNotFound,
    InvalidAlignmentRequest,
    TextAlignmentError,
)
from . import translation_works

WorkResponse = Tuple[int, Dict[str, object]]
ReadyRunner = Callable[[Callable[[Path], Dict[str, object]]], Optional[Dict[str, object]]]


def _single(params: Mapping[str, Sequence[object]], name: str) -> object:
    values = params.get(name, [])
    if len(values) != 1:
        raise InvalidAlignmentRequest(f"{name} 必须提供一次。")
    return values[0]


class TranslationWorkController:
    """Reads run while the index is published; small writes do the same.

    Writes here touch only the v7 workspace tables and manual overrides, never
    search-visible data, so they do not suspend the index. ``run_when_ready``
    keeps them out of an index rebuild or data-location switch.
    """

    def __init__(
        self,
        run_when_ready: ReadyRunner,
        *,
        active_model_id: Callable[[], str],
        log_exception: Callable[[str], None],
        active_backend: Callable[[], str] = lambda: "default",
    ) -> None:
        self._run_when_ready = run_when_ready
        self._active_model_id = active_model_id
        self._log_exception = log_exception
        self._active_backend = active_backend

    def overview(self, params: Mapping[str, Sequence[object]] | None = None) -> WorkResponse:
        model_id = self._active_model_id()
        params = params or {}

        def operation(path: Path) -> Dict[str, object]:
            if set(params) - {"include_statistics", "source_id", "target_id", "backend"}:
                raise InvalidAlignmentRequest("不支持的总览参数。")
            statistics = _single(params, "include_statistics") if "include_statistics" in params else "1"
            if statistics not in ("0", "1"):
                raise InvalidAlignmentRequest("include_statistics 必须为 0 或 1。")
            for name in ("source_id", "target_id"):
                if name in params and not _single(params, name):
                    raise InvalidAlignmentRequest(f"{name} 不能为空。")
            backend = _single(params, "backend") if "backend" in params else self._active_backend()
            if backend not in ("default", "bertalign"):
                raise InvalidAlignmentRequest("backend 无效。")
            return translation_works.alignment_overview(
                path, active_model_id=model_id, include_statistics=statistics == "1",
                source_id=_single(params, "source_id") if "source_id" in params else "",
                target_id=_single(params, "target_id") if "target_id" in params else "",
                backend=backend,
            )

        return self._call(operation, "translation work overview failed")

    def reading_position(self, params: Mapping[str, Sequence[object]]) -> WorkResponse:
        return self._call(
            lambda path: translation_works.read_reading_position(
                path, _single(params, "document_group_id")
            ),
            "reading position read failed",
        )

    def save_reading_position(self, payload: object) -> WorkResponse:
        return self._with_payload(
            payload,
            lambda value, path: translation_works.save_reading_position(
                path,
                value.get("document_group_id"),
                value.get("left_source_file_id"),
                value.get("right_source_file_id"),
                value.get("item_index"),
                value.get("char_offset", 0),
            ),
            "reading position save failed",
        )

    def suggestion_dismissals(self, _params: object = None) -> WorkResponse:
        return self._call(
            translation_works.list_suggestion_dismissals,
            "suggestion dismissals read failed",
        )

    def dismiss_suggestion(self, payload: object) -> WorkResponse:
        return self._with_payload(
            payload,
            lambda value, path: translation_works.dismiss_suggestion(
                path, value.get("source_file_ids")
            ),
            "suggestion dismissal failed",
        )

    def links(self, params: Mapping[str, Sequence[object]]) -> WorkResponse:
        def operation(path: Path) -> Dict[str, object]:
            backend = _single(params, "backend") if "backend" in params else self._active_backend()
            if backend not in ("default", "bertalign"):
                raise InvalidAlignmentRequest("backend 无效。")
            return translation_works.alignment_link_window(
                path,
                _single(params, "source_file_id"),
                _single(params, "target_source_file_id"),
                _single(params, "start_index"),
                _single(params, "end_index"),
                backend=backend,
            )

        return self._call(operation, "alignment link window failed")

    def review_candidates(self, payload: object) -> WorkResponse:
        return self._with_payload(
            payload,
            lambda value, path: translation_works.review_candidates(
                path,
                value.get("source_file_id"),
                value.get("target_source_file_id"),
                value.get("source_segment_ids"),
                value.get("near_target_segment_ids"),
                value.get("radius", 4),
                backend=value.get("backend", self._active_backend()),
            ),
            "alignment review candidates failed",
        )

    def save_correction(self, payload: object) -> WorkResponse:
        return self._with_payload(
            payload,
            lambda value, path: translation_works.save_correction(
                path,
                value.get("source_file_id"),
                value.get("target_source_file_id"),
                value.get("source_segment_ids"),
                value.get("target_segment_ids"),
            ),
            "alignment correction save failed",
        )

    def defer_review(self, payload: object) -> WorkResponse:
        return self._with_payload(
            payload,
            lambda value, path: translation_works.defer_review(
                path,
                value.get("source_file_id"),
                value.get("target_source_file_id"),
                value.get("source_segment_ids"),
            ),
            "alignment review deferral failed",
        )

    def _with_payload(self, payload: object, operation, log_message: str) -> WorkResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "请求字段无效"}
        return self._call(lambda path: operation(payload, path), log_message)

    def _call(self, operation, log_message: str) -> WorkResponse:
        try:
            result = self._run_when_ready(operation)
        except InvalidAlignmentRequest as exc:
            return 400, {"error": str(exc).rstrip("。")}
        except AlignmentNotFound as exc:
            return 404, {"error": str(exc).rstrip("。")}
        except (OSError, sqlite3.Error, TextAlignmentError, ValueError):
            self._log_exception(log_message)
            return 500, {"error": "读取或保存失败，请稍后重试"}
        if result is None:
            return 503, {"error": "索引正在重建，请稍候再试"}
        return 200, result
