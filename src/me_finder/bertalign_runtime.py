"""Bertalign uses the existing component installer with its own venv and model."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .alignment_compute import AlignmentComputeError, COMPONENT_MISSING
from .bertalign_backend import BERTALIGN_MODEL_REVISION
from .bertalign_compute import BERTALIGN_REQUIRED, BertalignSubprocessComputeRunner
from .local_ocr_installer import LocalOCRInstallerError, load_local_ocr_installer_manifest
from .managed_alignment_runtime import (
    ALIGNMENT_RUNTIME_RECEIPT_SCHEMA, AlignmentRuntimeManifest,
    ManagedAlignmentRuntime, ManagedAlignmentRuntimeError, RuntimeLaunch,
    default_worker_context, _Cancelled,
)
from .runtime_location import component_runtime_root

COMPONENT_DIRECTORY = "text-alignment-bertalign"
PACKAGES = ("torch==2.14.0", "sentence-transformers==6.1.0", "faiss-cpu==1.15.1",
            "numba==0.67.0", "numpy==2.5.3")


def bertalign_runtime_dir(runtime_root: Path) -> Path:
    """Return the separately managed runtime directory."""
    return component_runtime_root(Path(runtime_root)) / "components" / COMPONENT_DIRECTORY / "runtime"


def bertalign_model_cache_dir(runtime_root: Path) -> Path:
    """Models publish atomically alongside their validated interpreter."""
    return bertalign_runtime_dir(runtime_root) / "models"


def bertalign_model_installed(runtime_root: Path) -> bool:
    """Require the model receipt written only after an offline load check."""
    from .bertalign_backend import bertalign_model_dir
    path = bertalign_model_dir(bertalign_model_cache_dir(runtime_root))
    try:
        return (path / "mefinder-revision.txt").read_text().strip() == BERTALIGN_MODEL_REVISION
    except FileNotFoundError:
        return False


class ManagedBertalignRuntime(ManagedAlignmentRuntime):
    """Reuse uv provisioning, atomic swap, maintenance leases and cancellation."""

    component_id = "text-alignment-bertalign"
    component_directory = COMPONENT_DIRECTORY
    worker_flags = ("--bertalign",)
    required_modules = BERTALIGN_REQUIRED

    def _load_manifest_safely(self) -> AlignmentRuntimeManifest:
        try:
            _engines, platform = load_local_ocr_installer_manifest(
                self._current_manifest_path(), platform_key=self.platform_key,
            )
        except LocalOCRInstallerError:
            return AlignmentRuntimeManifest("", "3.12", PACKAGES, None, configured=False)
        packages = PACKAGES
        # Windows/Linux PyPI torch wheels may bring CUDA; select the CPU build.
        if not self.platform_key.startswith("darwin"):
            packages = ("torch==2.14.0+cpu", *PACKAGES[1:])
            self.package_index_args = ("--extra-index-url", "https://download.pytorch.org/whl/cpu")
        return AlignmentRuntimeManifest(
            runtime_version="bertalign-1-" + BERTALIGN_MODEL_REVISION[:8],
            python="3.12", packages=packages, platform=platform,
        )

    def compute_status(self):
        available = resolve_bertalign_launch(self.runtime_root) is not None
        return {"available": available, "provider": "independent" if available else "none", "detail": ""}

    def _has_models(self):
        return bertalign_model_installed(self.runtime_root)

    def _validate(self, root: Path) -> None:
        super()._validate(root)
        module, source_root = self._worker_context()
        model_dir = root / "models" / "bertalign" / "labse"
        control = root / "model-validation.ndjson"
        control.write_text("", encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(source_root)
        # Download only during an explicitly requested install/update. Validation
        # of an already installed runtime is strictly offline.
        action = "--verify-model" if root == self.runtime_dir else "--download-bertalign-model"
        self._set_state("validating", message=("正在验证 LaBSE" if action == "--verify-model"
                                              else "正在下载并验证 LaBSE（约 1.8 GB）"))
        try:
            self._run_command(
                [str(self._venv_python(root)), "-m", module, "--bertalign", action, str(model_dir), str(control)],
                cwd=source_root, environment=environment, log_path=root / "model-install.log", timeout=7200,
            )
        except _Cancelled:
            raise
        except ManagedAlignmentRuntimeError as exc:
            raise ManagedAlignmentRuntimeError(self._read_control_error(control) or str(exc)) from exc
        messages = [json.loads(line) for line in control.read_text(encoding="utf-8").splitlines()]
        if not any(message.get("type") == "result" for message in messages):
            raise ManagedAlignmentRuntimeError(self._read_control_error(control) or "LaBSE 验证未完成")


def resolve_bertalign_launch(runtime_root: Path, *, worker_context=default_worker_context):
    """Resolve a receipt-validated interpreter and the frozen/development source."""
    runtime = bertalign_runtime_dir(runtime_root)
    try:
        receipt = json.loads((runtime / "installed.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != ALIGNMENT_RUNTIME_RECEIPT_SCHEMA
            or receipt.get("protocol") != 1 or not receipt.get("identity")):
        return None
    python = runtime / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
    if not python.is_file():
        return None
    module, source_root = worker_context()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root)
    return RuntimeLaunch((str(python), "-m", module, "--bertalign"), env, str(source_root))


def build_bertalign_compute_runner(*, task_id, cancel_check, runtime_root):
    """Never fall back to the application's interpreter when uninstalled."""
    launch = resolve_bertalign_launch(runtime_root)
    if launch is None:
        raise AlignmentComputeError(COMPONENT_MISSING, "请在设置 → 译本对齐中安装 Bertalign 组件。")
    return BertalignSubprocessComputeRunner(task_id=task_id, cancel_check=cancel_check,
        launch_command=launch.command, env=launch.env, cwd=Path(launch.cwd))
