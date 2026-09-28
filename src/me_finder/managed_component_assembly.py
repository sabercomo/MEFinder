"""Construct and wire the locally managed runtime components.

Kept out of :mod:`me_finder.web_runtime` so the catalog + managed-component
assembly is one cohesive boundary that can grow (a new component, new manifest
wiring) without inflating the request-runtime module. The caller receives the
component registry (keyed by ``component_id`` for the settings HTTP layer) plus
the two components it still references directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from .component_catalog import ComponentCatalog
from .embedding_runtime import embedding_run_active
from .local_ocr_installer import LOCAL_OCR_MANIFEST_FILE, LocalOCRInstaller
from .local_ocr_settings import resolve_local_ocr_config_path
from .managed_alignment_runtime import (
    ManagedAlignmentRuntime,
    make_model_downloader,
)
from .managed_embedding_models import ManagedEmbeddingModels
from .managed_mineru import ManagedMinerU
from .mineru_api import resolve_mineru_config_path

_DESKTOP_SHELLS = {"macos", "win32", "linux"}


@dataclass
class ManagedComponents:
    catalog: ComponentCatalog
    mineru: ManagedMinerU
    local_ocr: LocalOCRInstaller
    alignment_runtime: ManagedAlignmentRuntime
    embedding_models: ManagedEmbeddingModels
    registry: Dict[str, object]


def assemble_managed_components(root: Path) -> ManagedComponents:
    """Build every managed component and wire the shared catalog refresh."""

    catalog = ComponentCatalog(root, LOCAL_OCR_MANIFEST_FILE)
    # Model download + load-probe run in the independent runtime when installed,
    # so a main process without the numeric stack can still complete them.
    embedding_models = ManagedEmbeddingModels(
        root,
        downloader=make_model_downloader(
            root, manifest_path=catalog.alignment_manifest_path,
            cancel_check=lambda: embedding_models.download_cancel_requested(),
        ),
    )
    alignment_runtime = ManagedAlignmentRuntime(
        root,
        manifest_path=catalog.alignment_manifest_path,
        catalog_summary=catalog.summary,
        models_component=embedding_models,
        is_compute_active=embedding_run_active,
    )
    mineru = ManagedMinerU(
        root,
        resolve_mineru_config_path(root),
        manifest_path=catalog.manifest_path,
        catalog_summary=catalog.summary,
    )
    local_ocr = LocalOCRInstaller(
        root,
        resolve_local_ocr_config_path(root),
        manifest_path=catalog.manifest_path,
        catalog_summary=catalog.summary,
    )
    if os.environ.get("ME_FINDER_DESKTOP_SHELL", "").strip().lower() in _DESKTOP_SHELLS:
        catalog.start_background_check(
            on_updated=lambda: (
                local_ocr.refresh_manifest(),
                mineru.refresh_manifest(),
                alignment_runtime.refresh_manifest(),
            )
        )
    mineru.start_installed_if_managed()
    return ManagedComponents(
        catalog=catalog,
        mineru=mineru,
        local_ocr=local_ocr,
        alignment_runtime=alignment_runtime,
        embedding_models=embedding_models,
        registry={
            local_ocr.component_id: local_ocr,
            mineru.component_id: mineru,
            embedding_models.component_id: embedding_models,
            alignment_runtime.component_id: alignment_runtime,
        },
    )
