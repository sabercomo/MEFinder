"""Filesystem storage for imported document copies."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import time
import unicodedata
import uuid
from pathlib import Path
from typing import List, Optional

from .mineru_api import MinerUError


# Keep internal corpus paths comfortably below the legacy Windows MAX_PATH
# budget. The user's full PDF name is stored separately in pdf_imports.json
# and remains the title/file name shown by the application.
INTERNAL_DOCUMENT_NAME_MAX_BYTES = 180
LEGACY_WINDOWS_PATH_BUDGET = 240
STALE_DOCUMENT_STORAGE_SECONDS = 24 * 60 * 60


def _truncate_utf8(text: str, byte_limit: int) -> str:
    encoded = str(text).encode("utf-8")
    if len(encoded) <= byte_limit:
        return str(text)
    return encoded[: max(0, byte_limit)].decode("utf-8", errors="ignore")


def internal_document_name(
    file_name: str,
    *,
    byte_limit: int = INTERNAL_DOCUMENT_NAME_MAX_BYTES,
) -> str:
    """Return a portable, collision-resistant basename for corpus storage."""

    original = Path(str(file_name)).name
    byte_limit = max(48, int(byte_limit))
    if len(original.encode("utf-8")) <= byte_limit:
        return original
    suffix = Path(original).suffix
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    trailer = f"-{digest}{suffix}"
    stem_limit = byte_limit - len(trailer.encode("utf-8"))
    shortened = _truncate_utf8(Path(original).stem, stem_limit).rstrip(" .")
    if not shortened:
        shortened = "document"
    return f"{shortened}{trailer}"


def _document_storage_reservation_path(target: Path) -> Path:
    # Default macOS and Windows volumes compare path components without case,
    # and macOS may also normalize Unicode. The reservation identity must use
    # the same conservative equivalence or Case.pdf/case.pdf can acquire two
    # locks and atomically replace the same underlying file.
    normalized_name = unicodedata.normalize(
        "NFC",
        Path(target).name,
    ).casefold()
    identity = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()
    return Path(target).parent / f".mefinder-reserve-{identity}.lock"


def cleanup_stale_document_storage_files(
    directory: Path,
    *,
    older_than_seconds: int = STALE_DOCUMENT_STORAGE_SECONDS,
) -> List[Path]:
    """Remove abandoned hidden upload/copy reservations, never final files."""

    directory = Path(directory)
    cutoff = time.time() - max(0, int(older_than_seconds))
    removed: List[Path] = []
    for pattern in (
        ".mefinder-copy-*.tmp",
        ".mefinder-upload-*.tmp",
        ".mefinder-reserve-*.lock",
    ):
        for path in directory.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed.append(path)
            except OSError:
                continue
    return removed


def document_storage_target(
    directory: Path,
    file_name: str,
    *,
    shorten_long_names: bool = True,
) -> Path:
    """Reserve an unused internal path for one incoming document."""

    directory = Path(directory)
    name_limit = INTERNAL_DOCUMENT_NAME_MAX_BYTES
    if os.name == "nt":
        name_limit = min(
            name_limit,
            max(48, LEGACY_WINDOWS_PATH_BUDGET - len(str(directory.resolve())) - 1),
        )
    original_name = Path(str(file_name)).name
    stored_name = (
        internal_document_name(original_name, byte_limit=name_limit)
        if shorten_long_names
        else original_name
    )
    for attempt in range(100):
        candidate = stored_name
        if attempt:
            candidate_with_suffix = (
                f"{Path(stored_name).stem} "
                f"(imported-{uuid.uuid4().hex[:8]})"
                f"{Path(stored_name).suffix}"
            )
            candidate = (
                internal_document_name(
                    candidate_with_suffix,
                    byte_limit=name_limit,
                )
                if shorten_long_names
                or len(candidate_with_suffix.encode("utf-8")) > 255
                else candidate_with_suffix
            )
        target = directory / candidate
        reservation = _document_storage_reservation_path(target)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            descriptor = os.open(reservation, flags, 0o600)
        except FileExistsError:
            continue
        try:
            os.close(descriptor)
            if target.exists():
                reservation.unlink(missing_ok=True)
                continue
            return target
        except Exception:
            reservation.unlink(missing_ok=True)
            raise
    raise FileExistsError(
        errno.EEXIST,
        "无法为导入文件分配唯一的内部文件名。",
    )


def release_document_storage_target(target: Path) -> None:
    """Release a path returned by :func:`document_storage_target`."""

    try:
        _document_storage_reservation_path(Path(target)).unlink(missing_ok=True)
    except OSError:
        # A synchronizer or antivirus can transiently hold the lock on Windows.
        # The completed document is already durable at this point, so lock
        # cleanup must never replace a successful import result with an error.
        # The 24-hour stale cleanup will retry later.
        logging.warning(
            "document storage reservation could not be released; "
            "stale cleanup will retry"
        )


def document_storage_error(file_name: str, exc: OSError) -> MinerUError:
    """Translate filesystem failures without exposing the private data path."""

    display_name = Path(str(file_name)).name or "该文件"
    if getattr(exc, "errno", None) == errno.ENAMETOOLONG:
        message = (
            f"无法保存“{display_name}”：文件名或应用数据目录路径过长。"
            "请把应用数据目录移到更短的位置后重试。"
        )
    elif getattr(exc, "errno", None) == errno.ENOSPC:
        message = f"无法保存“{display_name}”：磁盘空间不足。请释放空间后重试。"
    elif getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM}:
        message = f"无法保存“{display_name}”：应用数据目录没有写入权限。"
    else:
        message = f"无法保存“{display_name}”。请检查磁盘空间和目录权限后重试。"
    return MinerUError(message)


def copy_local_document(root: Path, source_path: Path) -> Path:
    """Copy one scanned file into the corpus, never touching the original."""

    root = Path(root)
    source_path = Path(source_path)
    suffix = source_path.suffix.lower()
    if suffix not in {".pdf", ".docx", ".epub"}:
        raise MinerUError("只支持 PDF、DOCX 或 EPUB 文件。")
    directory = root / "corpus" / ("raw_pdf" if suffix == ".pdf" else "raw_docx")
    target: Optional[Path] = None
    temp_path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        cleanup_stale_document_storage_files(directory)
        target = document_storage_target(
            directory,
            source_path.name,
            # PDF config stores the user's original name separately, allowing
            # a shorter portable internal path. Word extraction still uses
            # the on-disk basename, so preserve DOCX names until it has an
            # equivalent metadata sidecar.
            shorten_long_names=suffix == ".pdf",
        )
        temp_path = directory / f".mefinder-copy-{uuid.uuid4().hex}.tmp"
        shutil.copy2(source_path, temp_path)
        temp_path.replace(target)
    except OSError as exc:
        raise document_storage_error(source_path.name, exc) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if target is not None:
            release_document_storage_target(target)
    assert target is not None
    return target


def reuse_registered_pdf_copy(
    root: Path,
    incoming_path: Path,
    configured_path: Optional[Path],
) -> Path:
    """Reuse a configured identical PDF and discard only our new corpus copy."""

    root = Path(root).resolve()
    incoming_path = Path(incoming_path)
    if configured_path is None or not configured_path.is_file():
        return incoming_path
    if configured_path.resolve() == incoming_path.resolve():
        return configured_path

    # ``incoming_path`` was created by this import. Never unlink a caller's
    # original file even if this helper is accidentally used outside the web
    # import flow.
    raw_pdf_dir = (root / "corpus" / "raw_pdf").resolve()
    try:
        incoming_path.resolve().relative_to(raw_pdf_dir)
    except ValueError:
        return configured_path
    incoming_path.unlink(missing_ok=True)
    return configured_path
