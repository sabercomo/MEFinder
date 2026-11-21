"""Local HTTP boundary for the material library."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from ..application.index_runtime import IndexRuntime
from ..errors import UserFacingError
from ..http_route_table import BinaryResponse
from .images import collect_image, edit_image, preview_pdf, read_image
from .service import list_materials, mutate_materials


class MaterialsController:
    """Run material operations under the same lock as index publication."""

    def __init__(self, index_runtime: IndexRuntime, root: Path, source_path: Callable[[str], Path]) -> None:
        self._index_runtime = index_runtime
        self._root = root
        self._source_path = source_path

    def list(self, params: dict) -> tuple[int, object]:
        """List saved material and tags."""
        try:
            digest = (params.get("image") or [""])[0]
            result = self._index_runtime.run_when_ready(
                lambda path: BinaryResponse(*read_image(path, self._root, digest)) if digest else list_materials(path)
            )
        except UserFacingError as exc:
            return 400, {"error": str(exc)}
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).exception("Could not read materials")
            return 500, {"error": "素材库读取失败，请重试"}
        return (200, result) if result is not None else (503, {"error": "索引正在重建，请稍后重试"})

    def mutate(self, payload: object) -> tuple[int, object]:
        """Validate and apply a material operation."""
        def apply(path: Path) -> dict:
            if isinstance(payload, dict):
                if payload.get("action") == "collect_image":
                    return collect_image(path, self._root, payload, self._source_path)
                if payload.get("action") == "edit_image":
                    return edit_image(path, payload)
                if payload.get("action") == "preview_pdf":
                    return preview_pdf(path, payload, self._source_path)
            return mutate_materials(path, payload)

        try:
            with self._index_runtime.mutation():
                result = self._index_runtime.run_when_ready(apply)
        except UserFacingError as exc:
            return 400, {"error": str(exc)}
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).exception("Could not update materials")
            return 500, {"error": "素材库写入失败，请重试"}
        return (200, result) if result is not None else (503, {"error": "索引正在重建，请稍后重试"})
