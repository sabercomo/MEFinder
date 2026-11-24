"""Local HTTP boundary for the material library."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from ..application.index_runtime import IndexRuntime
from ..errors import UserFacingError
from ..http_route_table import BinaryResponse, MATERIALS_BODY, RoutePair, route
from ..persistence.materials_store import read_paragraph_excerpts, read_tag_paths
from .images import collect_image, edit_image, prepare_pdf_image, read_image, render_pdf_preview
from .service import list_materials, mutate_materials


class MaterialsController:
    """Run material operations under the same lock as index publication."""

    def __init__(self, index_runtime: IndexRuntime, root: Path, source_path: Callable[[str], Path]) -> None:
        self._index_runtime = index_runtime
        self._root = root
        self._source_path = source_path

    def http_routes(self) -> RoutePair:
        """Declare the material HTTP routes and their read/write policies."""
        get_routes = {
            "/api/materials": self.list,
            "/api/materials/tags": self.tags,
        }
        post_routes = {
            "/api/materials": route(self.mutate, **MATERIALS_BODY),
            "/api/materials/paragraph-excerpts": self.paragraph_excerpts,
        }
        return get_routes, post_routes

    def list(self, params: dict) -> tuple[int, object]:
        """List saved material and tags."""
        digest = (params.get("image") or [""])[0]
        return self._read(
            lambda path: BinaryResponse(*read_image(path, self._root, digest)) if digest else list_materials(path)
        )

    def tags(self, _params: dict) -> tuple[int, object]:
        """List tag paths for the lightweight tag picker."""
        return self._read(lambda path: {"material_tags": read_tag_paths(path)})

    def paragraph_excerpts(self, payload: object) -> tuple[int, object]:
        """Read only excerpts anchored in the requested search results."""
        keys = payload.get("paragraphs") if isinstance(payload, dict) else None
        if not isinstance(keys, list) or any(
            not isinstance(key, list) or len(key) != 2 or not all(isinstance(value, str) and value for value in key)
            for key in keys
        ):
            return 400, {"error": "请提供有效的文献与段落编号。"}
        paragraphs = {(source, paragraph) for source, paragraph in keys}
        return self._read(lambda path: {"excerpts": read_paragraph_excerpts(path, paragraphs)})

    def _read(self, operation: Callable[[Path], object]) -> tuple[int, object]:
        try:
            result = self._index_runtime.run_when_ready(operation)
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
            return mutate_materials(path, payload)

        try:
            with self._index_runtime.mutation():
                if isinstance(payload, dict) and payload.get("action") == "preview_pdf":
                    # Keep publication and other material renders serialized, but
                    # don't hold the search state lock during PyMuPDF or encoding.
                    if self._index_runtime.closing:
                        return 503, {"error": "应用正在关闭。"}
                    source = self._index_runtime.run_when_ready(
                        lambda path: prepare_pdf_image(path, payload, self._source_path)
                    )
                    result = render_pdf_preview(source, payload) if source is not None else None
                    if self._index_runtime.closing:
                        return 503, {"error": "应用正在关闭。"}
                else:
                    result = self._index_runtime.run_when_ready(apply)
        except UserFacingError as exc:
            return 400, {"error": str(exc)}
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).exception("Could not update materials")
            return 500, {"error": "素材库写入失败，请重试"}
        return (200, result) if result is not None else (503, {"error": "索引正在重建，请稍后重试"})
