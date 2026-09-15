"""Install and manage the independent alignment *compute* runtime.

Phase 2A moved the alignment compute phase into a separate process, but that
process was still launched with the main application's own interpreter — it
reused whatever NumPy / ONNX Runtime / fastembed happened to be bundled with the
app. Phase 2B makes the compute runtime a *managed component*: an isolated uv
virtual environment, installed under the runtime root, that owns the pinned
numeric stack. The main program never mutates its own environment (or another
component's) to install or upgrade it, and once installed the compute phase can
run without any numeric stack in the main process.

This module owns only the install / upgrade / uninstall / validate lifecycle of
that runtime and its cross-process safety. It never opens the official
database, never re-parses source files and never triggers OCR. The compute seam
itself lives in :mod:`me_finder.alignment_compute`; the launch resolution that
prefers an installed runtime lives there too (``installed_runtime_command``).

The mechanics (uv download + isolated venv, staging → publish → previous swap,
receipts, cancellation, download verification) deliberately mirror
:mod:`me_finder.managed_mineru`, the proven reference for a managed local
runtime, so the two share operational behaviour and reviewer intuition.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Mapping, Optional, Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen

from .alignment_compute import ALIGNMENT_COMPUTE_PROTOCOL
from .local_ocr_installer import (
    LOCAL_OCR_MANIFEST_FILE,
    PlatformManifest,
    current_platform_key,
    load_local_ocr_installer_manifest,
)
from .import_resume import atomic_write_json
from .runtime_location import component_runtime_root

# Component identifier used in the managed-component registry and HTTP layer.
COMPONENT_ID = "text-alignment-runtime"

# Bump when the installed layout or receipt schema changes incompatibly.
ALIGNMENT_RUNTIME_RECEIPT_SCHEMA = 1

# Directory names under components/text-alignment/.
_RUNTIME_DIR = "runtime"
_MODELS_DIR = "models"
_COMPUTE_LOCK = ".compute.lock"
_MAINTENANCE_MARKER = ".maintenance"
# How long an install/upgrade/uninstall waits for an in-flight compute task
# before giving up (so a stuck task cannot wedge the operation forever).
_MAINTENANCE_WAIT_TIMEOUT = 30 * 60


class ComputeUnavailable(RuntimeError):
    """A compute task cannot be admitted: the runtime is under maintenance.

    Raised by :func:`compute_admission` so the coordinator refuses a new task
    (mapping it to a user-actionable 503) instead of racing an in-progress
    install / upgrade / uninstall.
    """


def _text_alignment_component_root(runtime_root: Path) -> Path:
    return (
        component_runtime_root(Path(runtime_root))
        / "components"
        / "text-alignment"
    )


@contextmanager
def compute_admission(runtime_root: Path):
    """Hold a shared 'compute in progress' lease for one compute task.

    Refuses admission (``ComputeUnavailable``) while the runtime is under
    maintenance — an install / upgrade / uninstall raised the maintenance marker
    or holds the exclusive compute lock (any application instance). While a task
    holds this shared lease, such an operation waits for it to finish before
    touching the runtime, so the runtime is never swapped or deleted out from
    under a running computation, and once maintenance begins no new task starts.

    On POSIX this is an ``fcntl`` shared/exclusive file lock, so it coordinates
    across processes. On other platforms it degrades to the marker check (the
    same-instance wait is covered by the in-process activity signal); real
    cross-instance coverage there is pending platform verification.
    """

    root = _text_alignment_component_root(runtime_root)
    if not root.exists():
        # No component directory ⇒ no install/op could be under way, so there is
        # nothing to coordinate with. Never create directories from the admission
        # path (it must be a cheap, side-effect-free gate).
        yield
        return
    marker = root / _MAINTENANCE_MARKER
    if marker.exists():
        raise ComputeUnavailable("对齐计算组件正在维护，请稍后重试。")
    if os.name != "posix":
        yield
        return
    import fcntl

    handle = os.open(root / _COMPUTE_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            raise ComputeUnavailable("对齐计算组件正在维护，请稍后重试。") from exc
        # Close the race where maintenance began between the marker check and
        # the lease: if the marker now exists, back out.
        if marker.exists():
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
            raise ComputeUnavailable("对齐计算组件正在维护，请稍后重试。")
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(handle)


def _builtin_stack_present() -> bool:
    """Whether the app's *own* interpreter already has the compute stack.

    Used only for the settings availability indicator, so an app that bundles
    the numeric stack (today's build) reports alignment compute as available
    without an independent runtime. A module-level function so tests can
    simulate a stack-less (slim) main process.
    """

    from importlib.util import find_spec

    try:
        return all(
            find_spec(name) is not None
            for name in ("numpy", "fastembed", "onnxruntime")
        )
    except (ImportError, ValueError):
        return False


class ManagedAlignmentRuntimeError(RuntimeError):
    pass


class _Cancelled(ManagedAlignmentRuntimeError):
    pass


@dataclass(frozen=True)
class AlignmentRuntimeManifest:
    runtime_version: str
    python: str
    packages: tuple[str, ...]
    platform: Optional[PlatformManifest]
    configured: bool = True

    def identity(self) -> str:
        """Stable identity of what an install of this manifest should contain.

        The receipt records it; ``_update_available`` compares against it, so a
        changed pin set (not just a bumped format version) is offered as an
        update, and an install from a different pin set is never mistaken for
        the current one.
        """

        payload = json.dumps(
            {
                "runtime_version": self.runtime_version,
                "python": self.python,
                "packages": sorted(self.packages),
                "protocol": ALIGNMENT_COMPUTE_PROTOCOL,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _RuntimeState:
    state: str = "not_installed"
    operation: Optional[str] = None
    progress: Optional[float] = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    message: str = ""
    error: str = ""
    uninstall_deferred: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process: Optional[subprocess.Popen] = None
    thread: Optional[threading.Thread] = None


def load_alignment_runtime_manifest(
    path: Path,
    *,
    platform_key: Optional[str] = None,
) -> AlignmentRuntimeManifest:
    """Read the ``alignment`` runtime block and the shared platform matrix.

    The numeric packages (numpy / fastembed) are shared across platforms; the
    per-platform ONNX Runtime pin and uv download come from the shared
    ``platforms`` matrix, so there is one source of truth for both.
    """

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedAlignmentRuntimeError("对齐计算组件清单无法读取。") from exc
    raw = payload.get("alignment") if isinstance(payload, Mapping) else None
    if not isinstance(raw, Mapping):
        # An older or remote-cached catalog may legitimately predate the
        # ``alignment`` block. That is NOT an error: the component reports itself
        # unconfigured (install disabled), and the app still starts and computes
        # via the bundled stack. Only a *present but malformed* block is a real
        # manifest error worth surfacing.
        selected_key = platform_key or current_platform_key()
        try:
            _engines, selected_platform = load_local_ocr_installer_manifest(
                Path(path), platform_key=selected_key
            )
        except Exception:  # noqa: BLE001 - platform info is optional here
            selected_platform = None
        return AlignmentRuntimeManifest(
            runtime_version="",
            python="",
            packages=(),
            platform=selected_platform,
            configured=False,
        )
    packages = raw.get("packages")
    if not isinstance(packages, list) or not all(
        isinstance(item, str) and "==" in item for item in packages
    ):
        raise ManagedAlignmentRuntimeError("对齐计算运行时依赖必须固定版本。")
    required = {name.split("==", 1)[0].lower() for name in packages}
    if not {"numpy", "onnxruntime", "fastembed"}.issubset(required):
        raise ManagedAlignmentRuntimeError(
            "对齐计算运行时依赖必须包含 numpy、onnxruntime、fastembed。"
        )
    selected_key = platform_key or current_platform_key()
    # The shared platform matrix supplies only the uv download and the venv
    # interpreter path; the numeric stack (including ONNX Runtime) is pinned in
    # the alignment block, so the runtime is self-describing and independent of
    # the OCR components' ONNX Runtime choice.
    _engines, selected_platform = load_local_ocr_installer_manifest(
        Path(path), platform_key=selected_key
    )
    return AlignmentRuntimeManifest(
        runtime_version=str(raw.get("version") or ""),
        python=str(raw.get("python") or ""),
        packages=tuple(packages),
        platform=selected_platform,
    )


class _CrossProcessOperationLock:
    """A best-effort exclusive lock held for the length of one operation.

    Unlike an in-process ``threading.Lock``, this is an OS advisory lock on a
    file, so a second application instance or an independent process cannot
    install / upgrade / uninstall the same component concurrently. The lock is
    released by the operating system if the holder dies, so a crash mid-install
    never wedges the component permanently.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Optional[int] = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(handle)
            raise ManagedAlignmentRuntimeError(
                "另一个进程正在安装或卸载对齐计算组件，请稍后重试。"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    os.lseek(handle, 0, os.SEEK_SET)
                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(handle)

    def __enter__(self) -> "_CrossProcessOperationLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class ManagedAlignmentRuntime:
    """Manage the isolated alignment compute runtime as a local component."""

    component_id = COMPONENT_ID

    def __init__(
        self,
        runtime_root: Path,
        *,
        manifest_path: Path | Callable[[], Path] = LOCAL_OCR_MANIFEST_FILE,
        platform_key: Optional[str] = None,
        opener: Callable = urlopen,
        process_launcher: Callable = subprocess.Popen,
        catalog_summary: Optional[Callable[[], Dict[str, object]]] = None,
        models_component: Optional[object] = None,
        is_compute_active: Optional[Callable[[], bool]] = None,
        worker_context: Optional[Callable[[], tuple[str, Path]]] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        # Colocate the runtime with the model cache under the stable machine
        # component directory (the models component uses the same wrapper), so
        # switching libraries never relocates or orphans the installed runtime.
        self.component_root = (
            component_runtime_root(self.runtime_root)
            / "components"
            / "text-alignment"
        )
        self.runtime_dir = self.component_root / _RUNTIME_DIR
        self.models_dir = self.component_root / _MODELS_DIR
        self._manifest_path = manifest_path
        self.platform_key = platform_key or current_platform_key()
        self.opener = opener
        self.process_launcher = process_launcher
        self._catalog_summary = catalog_summary
        self._models_component = models_component
        self._is_compute_active = is_compute_active or (lambda: False)
        self._worker_context = worker_context or default_worker_context
        self.manifest = self._load_manifest_safely()
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        # Recover from a crash that interrupted an atomic swap before setting the
        # initial state, so a valid previous runtime is not left stranded.
        self._recover_interrupted_state()
        self._state = _RuntimeState(
            state="installed" if self._installed() else "not_installed"
        )

    def _load_manifest_safely(self) -> AlignmentRuntimeManifest:
        """Never let a corrupt/missing manifest abort application startup."""

        try:
            return load_alignment_runtime_manifest(
                self._current_manifest_path(), platform_key=self.platform_key
            )
        except (ManagedAlignmentRuntimeError, OSError, ValueError):
            return AlignmentRuntimeManifest(
                runtime_version="", python="", packages=(), platform=None,
                configured=False,
            )

    # --- manifest -------------------------------------------------------- #
    def _current_manifest_path(self) -> Path:
        manifest_path = self._manifest_path
        return manifest_path() if callable(manifest_path) else manifest_path

    def refresh_manifest(self) -> None:
        manifest = self._load_manifest_safely()
        with self._lock:
            if self._state.operation:
                return
            self.manifest = manifest

    # --- receipts / installed state -------------------------------------- #
    def _receipt_path(self) -> Path:
        return self.runtime_dir / "installed.json"

    def _receipt(self) -> Mapping[str, object]:
        try:
            value = json.loads(self._receipt_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, Mapping) else {}

    def _venv_python(self, root: Path) -> Optional[Path]:
        platform_manifest = self.manifest.platform
        if platform_manifest is None:
            return None
        return root / platform_manifest.venv_python

    def _installed(self) -> bool:
        """A runtime counts as installed only if the receipt AND the interpreter
        it names are both present. A half-written staging directory (crash mid
        install) is never reported as installed."""

        receipt = self._receipt()
        if receipt.get("schema_version") != ALIGNMENT_RUNTIME_RECEIPT_SCHEMA:
            return False
        python = self._venv_python(self.runtime_dir)
        if python is None or not python.exists():
            return False
        return bool(receipt.get("identity"))

    def _update_available(self) -> bool:
        if not self._installed():
            return False
        return self._receipt().get("identity") != self.manifest.identity()

    def _is_valid_runtime(self, root: Path) -> bool:
        """A directory holds a complete install (receipt + interpreter)."""

        try:
            receipt = json.loads((root / "installed.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("schema_version") != ALIGNMENT_RUNTIME_RECEIPT_SCHEMA
            or not receipt.get("identity")
        ):
            return False
        python = self._venv_python(root)
        if python is not None and python.exists():
            return True
        # Recovery must still recognize a complete previous install when the
        # catalog is unavailable at startup. The two supported venv layouts are
        # stable and are also recorded by the platform matrix when it is usable.
        return any(
            candidate.exists()
            for candidate in (
                root / "venv" / "bin" / "python",
                root / "venv" / "Scripts" / "python.exe",
            )
        )

    def _recover_interrupted_state(self) -> None:
        """Restore a runtime left mid-swap by a crash (hard exit / power loss).

        The atomic install does ``final→.previous`` then ``staging→final``; a
        crash between them leaves no ``runtime/`` but an intact ``.previous-*``
        holding the working old version. Clearing ``.staging-*`` is not enough —
        the previous runtime must be moved back. Runs under the cross-process
        operation lock so it never races another instance mid-install; if the
        lock is held, another instance owns the swap and this instance skips.
        """

        root = self.component_root
        if not root.is_dir():
            return
        lock = _CrossProcessOperationLock(root / ".operation.lock")
        try:
            lock.acquire()
        except ManagedAlignmentRuntimeError:
            return
        try:
            for stale in list(root.glob(".staging-*")) + list(root.glob(".uv-*")):
                self._remove_tree(stale)
            previous = sorted(root.glob(".previous-*"))
            if self._is_valid_runtime(self.runtime_dir):
                for candidate in previous:
                    self._remove_tree(candidate)
                return
            for candidate in previous:
                if self._is_valid_runtime(candidate):
                    self._remove_tree(self.runtime_dir)
                    candidate.replace(self.runtime_dir)
                    for other in root.glob(".previous-*"):
                        self._remove_tree(other)
                    return
            for candidate in previous:
                self._remove_tree(candidate)
        finally:
            lock.release()

    def installed_venv_python(self) -> Optional[Path]:
        """The interpreter of a validly installed runtime, else None.

        The launch resolver in :mod:`me_finder.alignment_compute` uses this to
        prefer the independent runtime over the main interpreter.
        """

        if not self._installed():
            return None
        return self._venv_python(self.runtime_dir)

    def compute_status(self) -> Dict[str, object]:
        """Whether alignment compute is available now, and via which runtime.

        ``provider`` is exactly what a generation would actually use, so the
        settings line never disagrees with the launch path: the independent
        runtime only when it would truly launch (``resolve_installed_runtime_launch``
        — same receipt/protocol/interpreter checks), otherwise the app's bundled
        stack when importable. An installed-but-unlaunchable runtime (e.g. an
        incompatible protocol after a main-app upgrade) is surfaced in ``detail``
        rather than shown as "可用 · 独立运行时". An existing user whose app
        bundles the stack sees "可用", never "未安装" (the independent runtime is
        optional until the main package is slimmed, 2C).
        """

        try:
            launch = resolve_installed_runtime_launch(
                self.runtime_root,
                platform_key=self.platform_key,
                manifest_path=self._manifest_path,
                worker_context=self._worker_context,
            )
        except Exception:  # noqa: BLE001 - status must never raise
            launch = None
        if launch is not None:
            return {"available": True, "provider": "independent", "detail": ""}
        # Installed (receipt+interpreter) but not launchable ⇒ incompatible.
        incompatible = self._installed()
        if _builtin_stack_present():
            detail = (
                "已安装的独立运行时不兼容，当前使用随应用提供的运行时"
                if incompatible else ""
            )
            return {"available": True, "provider": "builtin", "detail": detail}
        if incompatible:
            return {
                "available": False,
                "provider": "none",
                "detail": "已安装的独立运行时不兼容，请重新安装",
            }
        return {"available": False, "provider": "none", "detail": ""}

    def _has_models(self) -> bool:
        models = self.models_dir
        if not models.is_dir():
            return False
        return any(models.iterdir())

    # --- summary --------------------------------------------------------- #
    def summary(self) -> Dict[str, object]:
        with self._lock:
            state = self._state
            receipt = self._receipt()
            result: Dict[str, object] = {
                "component_id": self.component_id,
                "supported": self.manifest.configured and self.manifest.platform is not None,
                "platform": self.platform_key,
                "version": self.manifest.runtime_version,
                "installed": self._installed(),
                "installed_version": str(receipt.get("runtime_version") or ""),
                "update_available": self._update_available(),
                "has_models": self._has_models(),
                "compute": self.compute_status(),
                "runtime_dir": str(self.runtime_dir),
                "state": state.state,
                "operation": state.operation,
                "progress": state.progress,
                "downloaded_bytes": state.downloaded_bytes,
                "total_bytes": state.total_bytes,
                "uninstall_deferred": state.uninstall_deferred,
                "message": state.message,
                "error": state.error,
            }
        if self._catalog_summary is not None:
            result["catalog"] = self._catalog_summary()
        return result

    def diagnostics(self) -> Dict[str, object]:
        with self._lock:
            return {
                "component_id": self.component_id,
                "supported": self.manifest.configured and self.manifest.platform is not None,
                "platform": self.platform_key,
                "installed": self._installed(),
                "update_available": self._update_available(),
                "operation": self._state.operation,
            }

    # --- operations ------------------------------------------------------ #
    def perform(self, payload: Mapping[str, object]) -> Dict[str, object]:
        action = str(payload.get("action") or "").strip().lower()
        if action == "cancel":
            self._cancel()
            return self.summary()
        if action not in {"install", "update", "uninstall", "validate"}:
            raise ManagedAlignmentRuntimeError("不支持的对齐计算组件操作。")
        self._start_operation(action)
        return self.summary()

    def _start_operation(self, action: str) -> None:
        installed = self._installed()
        if action == "install" and installed:
            raise ManagedAlignmentRuntimeError("对齐计算组件已安装。")
        if action == "update" and not self._update_available():
            raise ManagedAlignmentRuntimeError("对齐计算组件没有可安装的更新。")
        if action in {"uninstall", "validate"} and not installed:
            raise ManagedAlignmentRuntimeError("对齐计算组件尚未安装。")
        if action in {"install", "update"} and not self.manifest.configured:
            raise ManagedAlignmentRuntimeError(
                "当前组件清单未定义对齐计算运行时，请更新应用后再安装。"
            )
        if action in {"install", "update"} and self.manifest.platform is None:
            raise ManagedAlignmentRuntimeError("当前平台不在对齐计算安装矩阵中。")
        if not self._operation_lock.acquire(blocking=False):
            raise ManagedAlignmentRuntimeError("对齐计算组件正在操作中。")
        initial = {
            "install": "provisioning",
            "update": "provisioning",
            "uninstall": "cleaning",
            "validate": "validating",
        }[action]
        with self._lock:
            state = self._state
            state.state = initial
            state.operation = action
            state.progress = None
            state.downloaded_bytes = 0
            state.total_bytes = 0
            state.uninstall_deferred = False
            state.message = ""
            state.error = ""
            state.cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._operation_worker,
                args=(action,),
                name=f"managed-alignment-runtime-{action}",
                daemon=True,
            )
            state.thread = thread
        thread.start()

    def _operation_worker(self, action: str) -> None:
        cross_lock = _CrossProcessOperationLock(self.component_root / ".operation.lock")
        try:
            cross_lock.acquire()
        except ManagedAlignmentRuntimeError as exc:
            self._set_state(
                "installed" if self._installed() else "not_installed",
                operation=None,
                message="操作失败",
                error=str(exc),
            )
            self._operation_lock.release()
            return
        try:
            if action == "install":
                self._install()
                message = "对齐计算组件安装完成"
                final_state = "installed"
            elif action == "update":
                # Upgrade replaces the in-use runtime, so first stop new tasks
                # and wait for any running one — never swap files mid-compute.
                maintenance = self._enter_maintenance(
                    "upgrade_pending", "任务结束后升级", deferred=False
                )
                try:
                    self._install()
                finally:
                    self._exit_maintenance(maintenance)
                message = "对齐计算组件升级完成"
                final_state = "installed"
            elif action == "validate":
                self._validate(self.runtime_dir)
                message = "对齐计算组件验证通过"
                final_state = "installed"
            else:
                maintenance = self._enter_maintenance(
                    "uninstall_pending", "任务结束后卸载", deferred=True
                )
                try:
                    self._perform_uninstall()
                finally:
                    self._exit_maintenance(maintenance)
                message = "对齐计算组件已卸载"
                final_state = "not_installed"
            self._set_state(final_state, operation=None, message=message)
        except _Cancelled:
            self._set_state(
                "installed" if self._installed() else "not_installed",
                operation=None,
                message="操作已取消",
            )
        except (
            ManagedAlignmentRuntimeError,
            OSError,
            ValueError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
            tarfile.TarError,
            zipfile.BadZipFile,
            URLError,
        ) as exc:
            self._set_state(
                "installed" if self._installed() else "not_installed",
                operation=None,
                message="操作失败",
                error=str(exc),
            )
        finally:
            with self._lock:
                self._state.process = None
                self._state.thread = None
            cross_lock.release()
            self._operation_lock.release()

    def _install(self) -> None:
        platform_manifest = self.manifest.platform
        if platform_manifest is None:
            raise ManagedAlignmentRuntimeError("当前平台不在对齐计算安装矩阵中。")
        self.component_root.mkdir(parents=True, exist_ok=True)
        for stale in self.component_root.glob(".staging-*"):
            self._remove_tree(stale)
        staging = self.component_root / f".staging-{uuid.uuid4().hex}"
        final = self.runtime_dir
        previous = self.component_root / f".previous-{uuid.uuid4().hex}"
        published = False
        staging.mkdir(parents=True)
        try:
            uv_path = self._ensure_uv(platform_manifest)
            environment = self._install_environment(staging)
            install_log = staging / "install.log"
            self._set_state("provisioning", message="正在创建独立 Python 环境")
            self._run_command(
                [
                    str(uv_path),
                    "venv",
                    "--python",
                    self.manifest.python,
                    "--managed-python",
                    "--relocatable",
                    str(staging / "venv"),
                ],
                cwd=staging,
                environment=environment,
                log_path=install_log,
                timeout=1800,
            )
            python_path = staging / platform_manifest.venv_python
            self._set_state(
                "provisioning",
                message=f"正在安装对齐计算依赖（{len(self.manifest.packages)} 个）",
            )
            self._run_command(
                [
                    str(uv_path),
                    "pip",
                    "install",
                    "--python",
                    str(python_path),
                    *self.manifest.packages,
                ],
                cwd=staging,
                environment=environment,
                log_path=install_log,
                timeout=3600,
            )
            # Validate the *staged* runtime before publishing: the numeric stack
            # must actually import in the isolated interpreter (runtime load),
            # not merely exist on disk.
            self._set_state("validating", message="正在验证独立运行时")
            self._validate(staging)
            atomic_write_json(
                staging / "installed.json",
                {
                    "schema_version": ALIGNMENT_RUNTIME_RECEIPT_SCHEMA,
                    "runtime_version": self.manifest.runtime_version,
                    "python": self.manifest.python,
                    "platform": self.platform_key,
                    "protocol": ALIGNMENT_COMPUTE_PROTOCOL,
                    "packages": list(self.manifest.packages),
                    "identity": self.manifest.identity(),
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            if final.exists():
                final.replace(previous)
            staging.replace(final)
            published = True
            self._remove_tree(previous)
        except BaseException:
            # A failed or interrupted install must leave the previously working
            # runtime (if any) in place — never a half-published directory.
            if published:
                self._remove_tree(final)
            if previous.exists():
                previous.replace(final)
            raise
        finally:
            self._remove_tree(staging)

    def _validate(self, root: Path) -> None:
        """Run the compute worker's ``--verify`` with the runtime's interpreter.

        This is a real runtime-load check: ``--verify`` actually *imports* numpy
        / fastembed / onnxruntime in the isolated interpreter and runs a trivial
        op, then completes the versioned protocol handshake. File existence or
        ``find_spec`` (in this or the main process) would pass for a broken wheel
        that fails on real import, so it is not sufficient.
        """

        python = self._venv_python(root)
        if python is None or not python.exists():
            raise ManagedAlignmentRuntimeError("独立运行时缺少 Python 解释器。")
        module, source_root = self._worker_context()
        control = root / "probe.ndjson"
        control.write_text("", encoding="utf-8")
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(source_root), existing) if p
        )
        try:
            self._run_command(
                [str(python), "-m", module, "--verify", str(control)],
                cwd=source_root,
                environment=environment,
                log_path=root / "validation.log",
                timeout=300,
            )
        except ManagedAlignmentRuntimeError as exc:
            # --verify exits non-zero when the stack fails to load; prefer the
            # worker's specific reason over the generic "process exited N".
            detail = self._read_control_error(control)
            raise ManagedAlignmentRuntimeError(detail or str(exc)) from exc
        messages = [
            json.loads(line)
            for line in control.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        hello = next((m for m in messages if m.get("type") == "hello"), None)
        if hello is None:
            error = next((m for m in messages if m.get("type") == "error"), None)
            detail = str(error.get("message")) if error else "未返回能力应答"
            raise ManagedAlignmentRuntimeError(f"独立运行时验证失败：{detail}")
        if hello.get("protocol") != ALIGNMENT_COMPUTE_PROTOCOL:
            raise ManagedAlignmentRuntimeError("独立运行时协议版本不兼容。")
        capabilities = dict(hello.get("capabilities") or {})
        missing = [n for n in ("numpy", "fastembed", "onnxruntime") if not capabilities.get(n)]
        if missing:
            raise ManagedAlignmentRuntimeError(
                "独立运行时缺少计算依赖：" + "、".join(missing)
            )

    @staticmethod
    def _read_control_error(control: Path) -> str:
        try:
            lines = control.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "error":
                return str(message.get("message") or "")
        return ""

    def _enter_maintenance(self, state_name: str, message: str, *, deferred: bool):
        """Stop new tasks and wait for the running one, then hold the runtime.

        Raises the maintenance marker (new tasks are refused across instances),
        shows the pending state, then acquires the exclusive compute lock — which
        blocks until every in-flight compute task (this and other instances,
        POSIX) releases its shared lease. Cancellable and time-bounded so a stuck
        task cannot wedge the operation. Returns a handle for ``_exit_maintenance``.
        """

        self.component_root.mkdir(parents=True, exist_ok=True)
        marker = self.component_root / _MAINTENANCE_MARKER
        marker.write_text(f"pid={os.getpid()}", encoding="utf-8")
        with self._lock:
            self._state.state = state_name
            self._state.message = message
            if deferred:
                self._state.uninstall_deferred = True
        handle: Optional[int] = None
        deadline = time.monotonic() + _MAINTENANCE_WAIT_TIMEOUT
        try:
            if os.name == "posix":
                import fcntl

                handle = os.open(
                    self.component_root / _COMPUTE_LOCK, os.O_CREAT | os.O_RDWR, 0o600
                )
                while True:
                    self._raise_if_cancelled()
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        pass
                    if time.monotonic() >= deadline:
                        raise ManagedAlignmentRuntimeError("等待当前对齐任务结束超时。")
                    if self._state.cancel_event.wait(0.2):
                        raise _Cancelled("操作已取消。")
            else:
                while self._is_compute_active():
                    if self._state.cancel_event.wait(0.2):
                        raise _Cancelled("操作已取消。")
                    if time.monotonic() >= deadline:
                        raise ManagedAlignmentRuntimeError("等待当前对齐任务结束超时。")
        except BaseException:
            if handle is not None:
                os.close(handle)
            marker.unlink(missing_ok=True)
            with self._lock:
                self._state.uninstall_deferred = False
            raise
        self._set_state("cleaning" if deferred else "provisioning")
        return (handle, marker)

    def _exit_maintenance(self, handle_marker) -> None:
        handle, marker = handle_marker
        if handle is not None:
            try:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(handle)
        marker.unlink(missing_ok=True)
        with self._lock:
            self._state.uninstall_deferred = False

    def _perform_uninstall(self) -> None:
        # Remove the runtime, the shared uv tool cache and the managed Python,
        # then — per the confirmed product rule — the component's model files.
        # Failures propagate so the UI never shows "已卸载" over a runtime that
        # is still on disk.
        self._remove_tree(self.runtime_dir, strict=True)
        self._remove_tree(self.component_root / "_tools", strict=True)
        self._remove_tree(self.component_root / "_python", strict=True)
        self._delete_models()

    def _delete_models(self) -> None:
        # Reset the models component's receipts / in-memory state first (it also
        # refuses to delete a model mid-download), then remove the whole models
        # directory — it is exclusive to the alignment component, so a full
        # component uninstall clears every model file it owns, not only the
        # currently-registered model ids. Documents, notes and existing
        # alignment results live in the library database and are untouched.
        component = self._models_component
        if component is not None and hasattr(component, "delete_all_models"):
            component.delete_all_models()
        self._remove_tree(self.models_dir, strict=True)

    # --- uv + subprocess ------------------------------------------------- #
    def _ensure_uv(self, platform_manifest: PlatformManifest) -> Path:
        tool_dir = (
            self.component_root
            / "_tools"
            / f"uv-{platform_manifest.uv.version}-{platform_manifest.key}"
        )
        executable = tool_dir / PurePosixPath(platform_manifest.uv.member).name
        if executable.is_file():
            return executable
        staging = self.component_root / f".uv-{uuid.uuid4().hex}"
        archive = staging / "archive"
        staging.mkdir(parents=True)
        try:
            self._download_file(
                platform_manifest.uv.url,
                archive,
                platform_manifest.uv.size,
                platform_manifest.uv.sha256,
            )
            extracted = staging / executable.name
            if platform_manifest.uv.archive_type == "zip":
                with zipfile.ZipFile(archive) as bundle:
                    try:
                        member = bundle.getinfo(platform_manifest.uv.member)
                    except KeyError as exc:
                        raise ManagedAlignmentRuntimeError(
                            "uv 归档缺少清单指定的可执行文件。"
                        ) from exc
                    with bundle.open(member) as source, extracted.open("wb") as output:
                        shutil.copyfileobj(source, output)
            else:
                with tarfile.open(archive, "r:gz") as bundle:
                    try:
                        member = bundle.getmember(platform_manifest.uv.member)
                    except KeyError as exc:
                        raise ManagedAlignmentRuntimeError(
                            "uv 归档缺少清单指定的可执行文件。"
                        ) from exc
                    source = bundle.extractfile(member)
                    if source is None:
                        raise ManagedAlignmentRuntimeError("uv 归档入口无法读取。")
                    with source, extracted.open("wb") as output:
                        shutil.copyfileobj(source, output)
            extracted.chmod(0o755)
            archive.unlink()
            tool_dir.parent.mkdir(parents=True, exist_ok=True)
            self._remove_tree(tool_dir)
            staging.replace(tool_dir)
            return tool_dir / executable.name
        finally:
            self._remove_tree(staging)

    def _download_file(
        self,
        url: str,
        target: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        request = Request(url, headers={"User-Agent": "MEFinder-alignment-runtime-installer"})
        downloaded = 0
        with self._lock:
            self._state.total_bytes = expected_size
            self._state.downloaded_bytes = 0
        with self.opener(request, timeout=30) as response, target.open("wb") as output:
            while True:
                self._raise_if_cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                with self._lock:
                    self._state.downloaded_bytes = downloaded
                    if expected_size:
                        self._state.progress = min(downloaded / expected_size, 0.99)
        if target.stat().st_size != expected_size:
            raise ManagedAlignmentRuntimeError("uv 下载文件大小与清单不一致。")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise ManagedAlignmentRuntimeError("uv 下载文件 SHA-256 校验失败。")

    def _install_environment(self, staging: Path) -> Dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "UV_CACHE_DIR": str(staging / ".uv-cache"),
                "UV_PYTHON_INSTALL_DIR": str(self.component_root / "_python"),
                "UV_NO_PROGRESS": "1",
            }
        )
        return environment

    def _run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        log_path: Path,
        timeout: int,
    ) -> None:
        self._raise_if_cancelled()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as output:
            process = self.process_launcher(
                list(command),
                cwd=str(cwd),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
            with self._lock:
                self._state.process = process
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if self._state.cancel_event.wait(0.1):
                        self._stop_process(process)
                        raise _Cancelled("操作已取消。")
                    if time.monotonic() >= deadline:
                        self._stop_process(process)
                        raise ManagedAlignmentRuntimeError("对齐计算安装子进程超时。")
            finally:
                with self._lock:
                    if self._state.process is process:
                        self._state.process = None
        if process.returncode:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise ManagedAlignmentRuntimeError(
                f"对齐计算安装子进程退出 {process.returncode}：{detail.strip()}"
            )

    # --- cancellation / state -------------------------------------------- #
    def _cancel(self) -> None:
        with self._lock:
            state = self._state
            if state.operation is None:
                return
            state.cancel_event.set()
            process = state.process
        if process is not None:
            self._stop_process(process)

    def _raise_if_cancelled(self) -> None:
        if self._state.cancel_event.is_set():
            raise _Cancelled("操作已取消。")

    def _set_state(
        self,
        new_state: str,
        *,
        operation: object = "keep",
        message: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            state = self._state
            state.state = new_state
            if operation != "keep":
                state.operation = operation  # type: ignore[assignment]
            if message:
                state.message = message
            state.error = error

    def close(self) -> None:
        """Cancel any in-flight operation and wait for its subprocess to be reaped.

        Called on application shutdown: it must not report done while an install
        / verify subprocess is still alive. It signals cancellation, stops the
        process, then joins the operation thread (whose ``finally`` reaps the
        child and releases the locks), so shutdown genuinely leaves nothing behind.
        """

        with self._lock:
            state = self._state
            thread = state.thread if state.operation is not None else None
            if state.operation is not None:
                state.cancel_event.set()
                process = state.process
            else:
                process = None
        if process is not None:
            self._stop_process(process)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=30)

    # --- helpers --------------------------------------------------------- #
    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        except OSError:
            pass

    @staticmethod
    def _remove_tree(path: Path, *, strict: bool = False) -> None:
        if strict:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            return
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            try:
                path.unlink()
            except OSError:
                pass


@dataclass(frozen=True)
class RuntimeLaunch:
    command: tuple[str, ...]
    env: Dict[str, str]
    cwd: str


def resolve_installed_runtime_launch(
    runtime_root: Path,
    *,
    platform_key: Optional[str] = None,
    manifest_path: Path | Callable[[], Path] = LOCAL_OCR_MANIFEST_FILE,
    worker_context: Optional[Callable[[], tuple[str, Path]]] = None,
) -> Optional[RuntimeLaunch]:
    """Launch parameters for the installed independent runtime, else ``None``.

    Read-only: it inspects the on-disk receipt and interpreter without touching
    the install lifecycle, so the compute seam can prefer the independent
    runtime whenever one is validly installed and otherwise fall back to the
    main interpreter (2A behaviour). A half-installed or receipt-less directory
    resolves to ``None`` — it is never launched as if installed.
    """

    selected_key = platform_key or current_platform_key()
    path = manifest_path() if callable(manifest_path) else manifest_path
    try:
        _engines, platform_manifest = load_local_ocr_installer_manifest(
            Path(path), platform_key=selected_key
        )
    except Exception:  # noqa: BLE001 - a bad manifest must not break launching
        return None
    if platform_manifest is None:
        return None
    runtime_dir = (
        component_runtime_root(Path(runtime_root))
        / "components"
        / "text-alignment"
        / _RUNTIME_DIR
    )
    receipt_path = runtime_dir / "installed.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != ALIGNMENT_RUNTIME_RECEIPT_SCHEMA
        or receipt.get("protocol") != ALIGNMENT_COMPUTE_PROTOCOL
        or not receipt.get("identity")
    ):
        return None
    python = runtime_dir / platform_manifest.venv_python
    if not python.exists():
        return None
    module, source_root = (worker_context or default_worker_context)()
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(source_root), existing) if p)
    return RuntimeLaunch(
        command=(str(python), "-m", module),
        env=env,
        cwd=str(source_root),
    )


def make_model_downloader(
    runtime_root: Path,
    *,
    platform_key: Optional[str] = None,
    manifest_path: Path | Callable[[], Path] = LOCAL_OCR_MANIFEST_FILE,
    worker_context: Optional[Callable[[], tuple[str, Path]]] = None,
    process_launcher: Callable = subprocess.Popen,
) -> Callable[[str, Path], None]:
    """A model downloader that runs in the independent runtime when installed.

    Phase 2B: model download and its load-probe must not require the main
    process's numeric stack. When an independent runtime is installed, this runs
    ``download_embedding_model`` in *that* interpreter (a subprocess), so the
    fastembed download + real model load happen off the main process. Otherwise
    it falls back to the in-process download (the app's bundled stack, 2A).
    """

    def _download(model_id: str, cache_dir: Path) -> None:
        launch = resolve_installed_runtime_launch(
            runtime_root,
            platform_key=platform_key,
            manifest_path=manifest_path,
            worker_context=worker_context,
        )
        if launch is None:
            from .managed_embedding_models import download_embedding_model

            download_embedding_model(model_id, cache_dir)
            return
        work = Path(tempfile.mkdtemp(prefix="mefinder-model-download-"))
        control = work / "control.ndjson"
        control.write_text("", encoding="utf-8")
        try:
            kwargs: dict = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = process_launcher(
                [*launch.command, "--download-model", str(model_id), str(cache_dir), str(control)],
                cwd=launch.cwd,
                env=dict(launch.env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
            process.wait()
            messages = [
                json.loads(line)
                for line in control.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            error = next((m for m in messages if m.get("type") == "error"), None)
            if error is not None:
                raise ManagedAlignmentRuntimeError(
                    f"独立运行时下载模型失败：{error.get('message')}"
                )
            if not any(m.get("type") == "result" for m in messages):
                raise ManagedAlignmentRuntimeError(
                    f"独立运行时未完成模型下载(exit={process.returncode})。"
                )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return _download


def default_worker_context() -> tuple[str, Path]:
    """How to launch the compute worker from an external interpreter.

    Returns ``(module, source_root)``: the module to run with ``-m`` and the
    directory to place on ``PYTHONPATH`` / use as the working directory, so an
    interpreter that has only the numeric stack can still import the pure-Python
    compute code.

    * Development: the repository root exposes ``src.me_finder`` as a package.
    * Frozen: the bundled application source root ships the ``me_finder``
      package next to the runtime (packaging wiring is completed in phase 2C;
      the resolution is defined here so the launch path is testable now).
    """

    if getattr(sys, "frozen", False):
        from .runtime_location import app_root

        return "me_finder.alignment_compute_worker", app_root()
    return "src.me_finder.alignment_compute_worker", Path(__file__).resolve().parents[2]
