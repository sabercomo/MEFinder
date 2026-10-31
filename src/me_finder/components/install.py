"""Shared building blocks for the locally managed component installers.

``managed_mineru``, ``managed_alignment_runtime`` and ``local_ocr_installer``
each own their manifest, state model, retry policy and install steps. The
mechanics they share live here: reading uv's download log, the rolling
speed/ETA estimate, streaming a response to disk, verifying a downloaded file,
unpacking the pinned uv executable, stopping a child process, and the
cancel / shutdown plumbing around an in-flight operation.

Callers pass in the clock reading, the URL opener and their own error type, so
each installer keeps its existing patch points and user-facing messages.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import (
    Callable,
    Collection,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    Union,
)

from ..import_resume import atomic_write_json

UV_DOWNLOAD_PATTERN = re.compile(
    r"^Downloading (.+) \(([0-9]+(?:\.[0-9]+)?)(KiB|MiB|GiB)\)\r?$",
    re.MULTILINE,
)
UV_DOWNLOADED_PATTERN = re.compile(r"^ Downloaded (.+?)\r?$", re.MULTILINE)
BINARY_SIZE_MULTIPLIERS = {
    "KiB": 1024,
    "MiB": 1024 * 1024,
    "GiB": 1024 * 1024 * 1024,
}
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
#: 取消相关的两句用户可见文案。三个安装器此前各写一遍，字面相同。
CANCELLED_MESSAGE = "操作已取消。"
CANCELLING_MESSAGE = "正在取消并清理…"


class DownloadProgressState(Protocol):
    downloaded_bytes: int
    total_bytes: int
    progress: Optional[float]
    download_speed_bps: float
    eta_seconds: Optional[int]
    download_samples: list[tuple[float, int]]


class OperationState(Protocol):
    """The fields every installer's per-operation state carries.

    各安装器的状态类字段并不相同（对齐运行时另有 step / 维护期，MinerU 另有
    “总量是估算”标记），这里只约束取消与关停真正用得到的那几个。
    """

    state: str
    operation: Optional[str]
    message: str
    cancel_event: threading.Event
    process: Optional[subprocess.Popen]
    thread: Optional[threading.Thread]


class UvArchive(Protocol):
    version: str
    url: str
    size: int
    sha256: str
    archive_type: str
    member: str


def uv_download_totals(log_text: str) -> Optional[Tuple[int, int]]:
    """Return ``(completed, total)`` bytes announced in a uv log, if any."""

    announced = {
        name: round(float(size) * BINARY_SIZE_MULTIPLIERS[unit])
        for name, size, unit in UV_DOWNLOAD_PATTERN.findall(log_text)
    }
    if not announced:
        return None
    completed_names = set(UV_DOWNLOADED_PATTERN.findall(log_text))
    completed = sum(
        size for name, size in announced.items() if name in completed_names
    )
    return completed, sum(announced.values())


def read_log_tail(log_path: Path, start_offset: int) -> str:
    with log_path.open("rb") as log:
        log.seek(start_offset)
        return log.read().decode("utf-8", errors="replace")


def begin_download_progress(
    state: DownloadProgressState,
    downloaded: int,
    total: int,
    now: float,
    *,
    total_is_estimate: Optional[bool] = None,
) -> None:
    """Start a new speed measurement at ``downloaded`` bytes (caller holds the lock)."""

    state.downloaded_bytes = downloaded
    state.total_bytes = total
    if total_is_estimate is not None:
        state.total_is_estimate = total_is_estimate  # type: ignore[attr-defined]
    state.progress = min(1.0, downloaded / total) if total else None
    state.download_speed_bps = 0.0
    state.eta_seconds = None
    state.download_samples = [(now, downloaded)]


def record_download_progress(
    state: DownloadProgressState,
    downloaded: int,
    total: int,
    now: float,
    *,
    window_seconds: Optional[float],
    total_is_estimate: Optional[bool] = None,
) -> None:
    """Record one byte count and refresh speed/ETA (caller holds the lock).

    Speed is the byte growth since the oldest sample still inside
    ``window_seconds`` (``None`` keeps every sample since the last begin). A
    span shorter than half a second leaves the previous estimate untouched;
    no growth over a longer span reports a stall.
    """

    downloaded = max(0, downloaded)
    state.downloaded_bytes = downloaded
    state.total_bytes = total
    if total_is_estimate is not None:
        state.total_is_estimate = total_is_estimate  # type: ignore[attr-defined]
    state.progress = min(1.0, downloaded / total) if total else None
    state.download_samples.append((now, downloaded))
    if window_seconds is not None:
        cutoff = now - window_seconds
        while (
            len(state.download_samples) > 2
            and state.download_samples[1][0] < cutoff
        ):
            state.download_samples.pop(0)
    started_at, started_bytes = state.download_samples[0]
    elapsed = now - started_at
    transferred = downloaded - started_bytes
    if elapsed >= 0.5 and transferred > 0:
        state.download_speed_bps = transferred / elapsed
        state.eta_seconds = (
            math.ceil(max(0, total - downloaded) / state.download_speed_bps)
            if total
            else None
        )
    elif elapsed >= 0.5:
        state.download_speed_bps = 0.0
        state.eta_seconds = None


def copy_response_to_file(
    response,
    output,
    *,
    downloaded: int,
    raise_if_cancelled: Callable[[], None],
    on_progress: Callable[[int], None],
    chunk_bytes: int = DOWNLOAD_CHUNK_BYTES,
) -> int:
    """Stream ``response`` into ``output``; return the total byte count."""

    while True:
        raise_if_cancelled()
        chunk = response.read(chunk_bytes)
        if not chunk:
            return downloaded
        output.write(chunk)
        downloaded += len(chunk)
        on_progress(downloaded)


def verify_download(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    error: type[Exception],
    size_message: str,
    digest_message: str,
) -> None:
    if path.stat().st_size != expected_size:
        raise error(size_message)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest().lower() != expected_sha256.lower():
        raise error(digest_message)


def extract_archive_member(
    archive: Path,
    *,
    archive_type: str,
    member: str,
    target: Path,
    error: type[Exception],
) -> None:
    """Copy one regular file out of a ``zip`` or ``tar.gz`` archive."""

    if archive_type == "zip":
        with zipfile.ZipFile(archive) as bundle:
            try:
                info = bundle.getinfo(member)
            except KeyError as exc:
                raise error("uv 归档缺少清单指定的可执行文件。") from exc
            with bundle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
        return
    if archive_type != "tar.gz":
        raise error("uv 归档格式不受支持。")
    with tarfile.open(archive, "r:gz") as bundle:
        try:
            entry = bundle.getmember(member)
        except KeyError as exc:
            raise error("uv 归档缺少清单指定的可执行文件。") from exc
        if not entry.isfile():
            raise error("uv 归档入口无效。")
        source = bundle.extractfile(entry)
        if source is None:
            raise error("uv 归档入口无法读取。")
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


def install_uv_executable(
    *,
    uv: UvArchive,
    tool_dir: Path,
    staging: Path,
    archive_name: str,
    download: Callable[[Path], None],
    remove_tree: Callable[[Path], None],
    error: type[Exception],
    receipt: Optional[Mapping[str, object]] = None,
) -> Path:
    """Download, unpack and publish the pinned uv executable into ``tool_dir``.

    ``download`` must leave a verified archive at the path it is given. The
    executable is assembled in ``staging`` and swapped in as a whole, so an
    interrupted install never leaves a half-written ``tool_dir``.
    """

    executable_name = PurePosixPath(uv.member).name
    archive = staging / archive_name
    staging.mkdir(parents=True)
    try:
        download(archive)
        extracted = staging / executable_name
        extract_archive_member(
            archive,
            archive_type=uv.archive_type,
            member=uv.member,
            target=extracted,
            error=error,
        )
        extracted.chmod(0o755)
        if receipt is not None:
            atomic_write_json(staging / "receipt.json", dict(receipt))
        archive.unlink()
        tool_dir.parent.mkdir(parents=True, exist_ok=True)
        remove_tree(tool_dir)
        staging.replace(tool_dir)
        return tool_dir / executable_name
    finally:
        remove_tree(staging)


def stop_process(process: subprocess.Popen) -> None:
    """Terminate a child process, escalating to kill after five seconds."""

    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    except OSError:
        pass


def resolve_manifest_path(source: Union[Path, Callable[[], Path]]) -> Path:
    """Manifest path, whether the installer was given a path or a provider."""

    return Path(source() if callable(source) else source)


def check_cancelled(state: OperationState, error: type[Exception]) -> None:
    """Raise the installer's own cancel error once cancellation was signalled."""

    if state.cancel_event.is_set():
        raise error(CANCELLED_MESSAGE)


def wait_unless_cancelled(
    state: OperationState, delay: float, error: type[Exception]
) -> None:
    """Cancel-aware sleep: a retry backoff must never delay a cancellation."""

    if state.cancel_event.wait(delay):
        raise error(CANCELLED_MESSAGE)


def stop_if_cancelled(
    state: OperationState,
    process: subprocess.Popen,
    delay: float,
    error: type[Exception],
) -> None:
    """Poll-loop step: reap the child before unwinding, or the handle leaks."""

    if state.cancel_event.wait(delay):
        stop_process(process)
        raise error(CANCELLED_MESSAGE)


def request_cancel(
    state: OperationState,
    lock: threading.RLock,
    active_states: Collection[str],
) -> bool:
    """Signal cancellation for one in-flight operation.

    Returns ``False`` when the component was not in a cancellable state, so the
    caller can raise its own message; the child process is stopped outside the
    lock, as the worker thread needs it to make progress toward its ``finally``.
    """

    with lock:
        if state.state not in active_states:
            return False
        state.cancel_event.set()
        process = state.process
        state.message = CANCELLING_MESSAGE
    if process is not None:
        stop_process(process)
    return True


def cancel_and_join_operations(
    states: Iterable[OperationState],
    lock: threading.RLock,
    timeout: Optional[float],
    *,
    before_join: Optional[Callable[[], None]] = None,
) -> bool:
    """Cancel every in-flight operation and wait for its worker to finish.

    Shutdown must not report done while an install subprocess still holds file
    handles. Cancellation is signalled under ``lock``, the children are stopped
    outside it, ``before_join`` runs any service stop the caller needs, and each
    worker thread is joined against one shared deadline. Returns whether every
    worker actually stopped within ``timeout``.
    """

    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    with lock:
        active = [state for state in states if state.operation is not None]
        threads = [state.thread for state in active if state.thread is not None]
        processes = [state.process for state in active if state.process is not None]
        for state in active:
            state.cancel_event.set()
    for process in processes:
        stop_process(process)
    if before_join is not None:
        before_join()
    for thread in threads:
        thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
    return all(not thread.is_alive() for thread in threads)


def remove_tree_if_present(path: Path) -> None:
    """Delete a directory tree that may not exist.

    对齐计算运行时有自己的 strict 重试版（Windows 文件占用），不走这里。
    """

    if path.exists():
        shutil.rmtree(path)
