"""Best-effort cleanup of leaked PyInstaller onefile extraction directories.

The MCP sidecar (``MEFinderMCP.exe``) is a PyInstaller onefile build: every
launch extracts its bundle into a fresh ``_MEI*`` directory under the system
temp location. The bootloader normally deletes that directory on exit;
when it is killed before cleanup (for example along with a timed-out server's
process group), the extracted directory is left behind.
Repeated kills therefore accumulate ~0.16 GB folders on the system drive.

This module lets any MEFinder process sweep the system temp location for
*our own* leaked extraction directories and delete the ones whose owning
process is gone. Directories belonging to other applications are never
touched: a sidecar bundle carries a marker file that ships with the build
(``mefinder-onefile.marker``), and ownership of a live directory is decided
by an OS-level lock file (``mefinder-sidecar.lock``) that the owning process
keeps open for its whole lifetime — the lock is released by the operating
system when the process dies, so no PID bookkeeping is needed.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: Marker file shipped inside the sidecar bundle (see packaging/mcp_sidecar.spec).
ONEFILE_MARKER_NAME = "mefinder-onefile.marker"
#: Lock file written into the current extraction directory at sidecar startup.
LOCK_FILE_NAME = "mefinder-sidecar.lock"
#: PyInstaller onefile extraction directories are named ``_MEI<random>``.
ONEFILE_DIR_PREFIX = "_MEI"
_claimed_lock_path: Path | None = None
_claimed_lock_handle: int | None = None


def is_frozen_onefile() -> bool:
    """Return True when running from a PyInstaller onefile extraction."""
    return current_extraction_dir() is not None


def current_extraction_dir() -> Path | None:
    """Return the running process's onefile extraction directory, if any."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not meipass:
        return None
    directory = Path(meipass)
    # onedir sets _MEIPASS too (macOS: Contents/Frameworks). Only our
    # onefile spec ships this marker, extracted into a bootloader _MEI dir.
    if directory.name.startswith(ONEFILE_DIR_PREFIX) and (
        directory / ONEFILE_MARKER_NAME
    ).is_file():
        return directory
    return None


def claim_current_extraction() -> Path | None:
    """Hold an OS lock inside the running onefile extraction directory.

    The handle stays open for the process lifetime; the operating system
    releases it when the process dies for any reason, which is what lets a
    later sweep distinguish a live extraction from a leaked one. Returns the
    extraction directory, or None outside our onefile bundle. Lock failures
    propagate: a sidecar must not keep running without protecting its payload.
    """
    global _claimed_lock_path, _claimed_lock_handle

    if _claimed_lock_path is not None:
        return _claimed_lock_path.parent
    extraction_dir = current_extraction_dir()
    if extraction_dir is None:
        return None
    lock_path = extraction_dir / LOCK_FILE_NAME
    handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Publish a nonempty claim only AFTER locking. Sweepers must skip the
        # empty file in the open-to-flock window, regardless of directory age.
        os.write(handle, f"pid={os.getpid()} claimed={time.time()}\n".encode("ascii"))
        os.fsync(handle)
    except OSError:
        os.close(handle)
        raise
    _claimed_lock_path = lock_path
    _claimed_lock_handle = handle
    return extraction_dir


def _lock_is_held(lock_path: Path) -> bool:
    """Return True when some process still holds the extraction lock."""
    if sys.platform == "win32":
        # A live owner's handle has no FILE_SHARE_DELETE, so deletion of the
        # lock file is denied; once the owner is dead deletion succeeds.
        try:
            os.remove(lock_path)
        except OSError as error:
            logger.debug("extraction lock unavailable %s: %s", lock_path, error)
            return True
        return False
    import fcntl

    try:
        handle = os.open(lock_path, os.O_RDWR)
    except OSError as error:
        logger.warning("could not inspect extraction lock %s: %s", lock_path, error)
        return True
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as error:
            logger.warning("could not probe extraction lock %s: %s", lock_path, error)
            return True
        return False
    finally:
        os.close(handle)


def _is_stale(directory: Path, own_dir: Path | None) -> bool:
    """Decide whether a marker-carrying extraction directory may be deleted."""
    if own_dir is not None:
        if directory.samefile(own_dir):
            return False
    lock_path = directory / LOCK_FILE_NAME
    try:
        if lock_path.stat().st_size == 0:
            return False
    except FileNotFoundError:
        # Neither a missing claim nor age proves the bootloader/owner is dead.
        # Leave interrupted extraction and pre-claim startup to manual cleanup.
        return False
    return not _lock_is_held(lock_path)


def cleanup_leaked_extractions(
    root: str | os.PathLike[str] | None = None,
    *,
    own_dir: Path | None = None,
) -> int:
    """Delete leaked MEFinder onefile extraction directories under ``root``.

    Only directories that contain the MEFinder sidecar marker file are
    considered; anything else in the system temp location is left untouched.
    Returns the number of directories removed; filesystem failures are logged.
    """
    removed = 0
    scan_root = Path(root) if root is not None else Path(tempfile.gettempdir())
    try:
        candidates = [
            entry
            for entry in os.scandir(scan_root)
            if entry.is_dir(follow_symlinks=False)
            and entry.name.startswith(ONEFILE_DIR_PREFIX)
        ]
    except OSError as error:
        logger.warning("could not scan %s for leaked extractions: %s", scan_root, error)
        return 0
    for entry in candidates:
        directory = Path(entry.path)
        try:
            if not (directory / ONEFILE_MARKER_NAME).is_file():
                continue
            if not _is_stale(directory, own_dir):
                continue
            shutil.rmtree(directory)
            removed += 1
            logger.info("removed leaked onefile extraction dir %s", directory)
        except FileNotFoundError:
            # Another sweep or the bootloader already removed this candidate.
            continue
        except OSError as error:
            # Abort on the first failure instead of ignoring errors: partially
            # deleting a directory whose owner is still alive would be worse
            # than leaving it for the next sweep.
            logger.warning("could not remove leaked extraction dir %s: %s", directory, error)
    return removed


def start_background_cleanup() -> threading.Thread | None:
    """Start a daemon thread that sweeps leaked extractions once.

    Returns the started thread, or None in unfrozen (source) runs where no
    MEFinder onefile extraction exists to clean up or protect.
    """
    if not getattr(sys, "frozen", False):
        return None
    own_dir = claim_current_extraction()
    thread = threading.Thread(
        target=cleanup_leaked_extractions,
        kwargs={"own_dir": own_dir},
        name="mefinder-onefile-cleanup",
        daemon=True,
    )
    thread.start()
    return thread
