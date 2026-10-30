"""Shared building blocks for the locally managed component installers.

``managed_mineru``, ``managed_alignment_runtime`` and ``local_ocr_installer``
each own their manifest, state model, retry policy and install steps. The
mechanics they share live here: reading uv's download log, the rolling
speed/ETA estimate, streaming a response to disk, verifying a downloaded file,
unpacking the pinned uv executable and stopping a child process.

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
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Protocol, Tuple

from .import_resume import atomic_write_json

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


class DownloadProgressState(Protocol):
    downloaded_bytes: int
    total_bytes: int
    progress: Optional[float]
    download_speed_bps: float
    eta_seconds: Optional[int]
    download_samples: list[tuple[float, int]]


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
