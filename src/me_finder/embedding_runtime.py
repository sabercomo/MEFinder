"""Runtime safety knobs for cross-language embedding.

Kept separate from :mod:`semantic_alignment` (the alignment algorithm) so the
CPU-headroom policy and cooperative-cancel plumbing have a single, testable
home and the algorithm module stays within its size budget.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading


class SemanticAlignmentCancelled(Exception):
    """A user asked to stop the in-flight embedding run.

    Deliberately not a ``RuntimeError`` so coordinator/controller layers that
    catch ``RuntimeError`` as a generic failure do not turn a cancellation into
    an error response.
    """


_EMBEDDING_CANCEL = threading.Event()


def request_embedding_cancel() -> None:
    """Signal any in-flight embedding run to stop at the next batch boundary."""

    _EMBEDDING_CANCEL.set()


def begin_embedding_run() -> None:
    """Clear a stale cancel flag before starting a new embedding run."""

    _EMBEDDING_CANCEL.clear()


def embedding_cancel_requested() -> bool:
    return _EMBEDDING_CANCEL.is_set()


def _macos_performance_core_count() -> int | None:
    """Best-effort Apple-Silicon performance-core count, else ``None``."""

    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        value = int(completed.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return value if value > 0 else None


def embedding_thread_count() -> int:
    """Threads for ONNX inference, always leaving headroom for the UI.

    On Apple Silicon the interactive UI and WindowServer compete for the few
    performance cores; oversubscribing them with the SME matmul kernels pegs
    every P-core and beach-balls the whole machine. Cap to one below the
    performance-core count there, and to two below the logical count elsewhere.
    """

    performance = _macos_performance_core_count()
    if performance is not None:
        return max(1, min(6, performance - 1))
    logical = os.cpu_count() or 2
    return max(1, min(8, logical - 2))
