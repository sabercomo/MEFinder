"""Transport-neutral JSON responses for page-mapping workflows."""

from __future__ import annotations

import json
import sqlite3
from typing import Dict, Mapping, Tuple

from .application.page_mapping_coordinator import PageMappingCoordinator
from .mineru_api import MinerUError


PageMappingResponse = Tuple[int, Dict[str, object]]


class PageMappingController:
    """Validate page-mapping payloads and map application failures."""

    def __init__(self, coordinator: PageMappingCoordinator) -> None:
        self._coordinator = coordinator

    def calibrate(self, payload: object) -> PageMappingResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_id = str(payload.get("source_id") or "")
        segments = payload.get("segments", [])
        if not source_file_id or not isinstance(segments, list):
            return 400, {"error": "invalid request"}
        try:
            self._coordinator.apply_manual_page_mapping(
                source_file_id,
                segments,
            )
        except (ValueError, MinerUError) as exc:
            return 400, {"error": str(exc)}
        except (
            OSError,
            RuntimeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            return 500, {
                "error": f"校准已保存，但索引重建失败：{exc}"
            }
        return 200, {"ok": True, "rebuilt": True}

    def detect(self, payload: object) -> PageMappingResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_id = str(payload.get("source_id") or "")
        if not source_file_id:
            return 400, {"error": "invalid request"}
        try:
            result = self._coordinator.detect_auto_page_mapping(source_file_id)
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            return 500, {"error": f"页码自动检测失败：{exc}"}
        return 200, {"ok": True, "result": result}

    def apply(self, payload: object) -> PageMappingResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_id = str(payload.get("source_id") or "")
        segments = payload.get("segments") or []
        auto_mapping = payload.get("auto_mapping") or {}
        if (
            not source_file_id
            or not isinstance(segments, list)
            or not isinstance(auto_mapping, dict)
        ):
            return 400, {"error": "invalid request"}
        try:
            updated = self._coordinator.apply_live_auto_mapping(
                source_file_id,
                segments,
                auto_mapping,
                bool(payload.get("replace_manual")),
            )
        except MinerUError as exc:
            return 409, {"error": str(exc)}
        except (
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            return 500, {"error": f"应用自动映射失败：{exc}"}
        return 200, {"ok": True, "updated": updated}

    def accept(self, payload: object) -> PageMappingResponse:
        if not isinstance(payload, Mapping):
            return 400, {"error": "invalid request"}
        source_file_id = str(payload.get("source_id") or "")
        if not source_file_id:
            return 400, {"error": "invalid request"}
        try:
            segment_count = self._coordinator.accept_auto_page_mapping(
                source_file_id
            )
        except MinerUError as exc:
            return 400, {"error": str(exc)}
        except (
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
            return 500, {
                "error": f"自动映射已保存失败或索引重建失败：{exc}"
            }
        return 200, {
            "ok": True,
            "segment_count": segment_count,
            "rebuilt": True,
        }
