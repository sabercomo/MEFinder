"""Local HTTP boundary for the material library."""

from __future__ import annotations

import logging
import sqlite3

from ..application.index_runtime import IndexRuntime
from ..errors import UserFacingError
from .service import list_materials, mutate_materials


class MaterialsController:
    """Run material operations under the same lock as index publication."""

    def __init__(self, index_runtime: IndexRuntime) -> None:
        self._index_runtime = index_runtime

    def list(self, params: object) -> tuple[int, object]:
        """List saved material and tags."""
        try:
            result = self._index_runtime.run_when_ready(list_materials)
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).exception("Could not read materials")
            return 500, {"error": "素材库读取失败，请重试"}
        return (200, result) if result is not None else (503, {"error": "索引正在重建，请稍后重试"})

    def mutate(self, payload: object) -> tuple[int, object]:
        """Validate and apply a material operation."""
        try:
            with self._index_runtime.mutation():
                result = self._index_runtime.run_when_ready(lambda path: mutate_materials(path, payload))
        except UserFacingError as exc:
            return 400, {"error": str(exc)}
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).exception("Could not update materials")
            return 500, {"error": "素材库写入失败，请重试"}
        return (200, result) if result is not None else (503, {"error": "索引正在重建，请稍后重试"})
