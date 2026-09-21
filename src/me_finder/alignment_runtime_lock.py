"""OS-owned shared/exclusive leases for alignment runtime files."""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from contextlib import contextmanager

from .runtime_location import component_runtime_root

_COMPUTE_LOCK = ".compute.lock"
_MAINTENANCE_MARKER = ".maintenance"


class RuntimeFileLock:
    """Nonblocking file lock; closing its handle also releases it after a crash."""

    def __init__(self, path: Path, *, shared: bool = False) -> None:
        self.path = path
        self.shared = shared
        self.handle: int | None = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if sys.platform == 'win32':
                import ctypes
                import msvcrt
                from ctypes import wintypes

                class OVERLAPPED(ctypes.Structure):
                    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                                ('hEvent', wintypes.HANDLE)]

                lock = ctypes.WinDLL('kernel32', use_last_error=True).LockFileEx
                lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
                lock.restype = wintypes.BOOL
                overlapped = OVERLAPPED()
                flags = 1 | (0 if self.shared else 2)  # FAIL_IMMEDIATELY | EXCLUSIVE
                if not lock(msvcrt.get_osfhandle(handle), flags, 0, 1, 0, ctypes.byref(overlapped)):
                    error = ctypes.get_last_error()
                    if error == 33:  # ERROR_LOCK_VIOLATION: another lease holds the byte
                        os.close(handle)
                        return False
                    raise ctypes.WinError(error)
            else:
                import fcntl
                try:
                    fcntl.flock(handle, (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    os.close(handle)
                    return False
        except BaseException:
            os.close(handle)
            raise
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None:
            handle, self.handle = self.handle, None
            os.close(handle)


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
def compute_admission(runtime_root: Path, *, component_directory: str = "text-alignment"):
    """Hold a shared runtime lease through computation or model publication."""
    root = component_runtime_root(Path(runtime_root)) / "components" / component_directory
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _MAINTENANCE_MARKER
    if marker.exists():
        raise ComputeUnavailable("对齐计算组件正在维护，请稍后重试。")
    lease = RuntimeFileLock(root / _COMPUTE_LOCK, shared=True)
    if not lease.try_acquire():
        raise ComputeUnavailable("对齐计算组件正在维护，请稍后重试。")
    try:
        if marker.exists():
            raise ComputeUnavailable("对齐计算组件正在维护，请稍后重试。")
        yield
    finally:
        lease.release()

