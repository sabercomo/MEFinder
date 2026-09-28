"""``GET /api/calibration``: read the PDF import/calibration config.

Moved out of the transport unchanged, including its legacy reply shapes: a
missing config yields ``{"documents": []}`` and an unknown ``source_id`` yields
``{"error": "not found"}`` -- both with status 200.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from .http_route_table import RoutePair


class CalibrationConfigController:
    def __init__(
        self,
        *,
        root: Path,
        load_import_config: Callable[[Path], Mapping[str, object]],
    ) -> None:
        self._root = root
        self._load_import_config = load_import_config

    def config(self, params: Mapping[str, list[str]]) -> tuple[int, object]:
        config_path = self._root / "config" / "pdf_imports.json"
        if not config_path.exists():
            return 200, {"documents": []}
        config = self._load_import_config(config_path)
        sid = (params.get("source_id") or [None])[0]
        if sid:
            doc = next(
                (d for d in config.get("documents", []) if d.get("source_file_id") == sid),
                None,
            )
            return 200, doc or {"error": "not found"}
        return 200, config


def assemble_calibration_config_routes(controller: CalibrationConfigController) -> RoutePair:
    return {"/api/calibration": controller.config}, {}
