"""Backend-independent route lookup for segment-keyed human corrections."""
import sqlite3
from typing import List, Tuple
from .bertalign_backend import BERTALIGN_ALGORITHM
from .text_alignment import ALIGNMENT_ALGORITHM, AlignmentNotFound, _resolve_alignment_route

def _resolve_alignment_route_any_backend(
    connection: sqlite3.Connection, source_id: str, target_id: str
) -> Tuple[List[sqlite3.Row], str | None]:
    """Resolve a route from whichever backend has one.

    Manual corrections and deferrals are keyed by segment *sets*, and both
    backends read the same segment sets for a source, so they only need a route
    that exists — not a specific backend. This lets a pair aligned only with the
    Bertalign backend still be corrected/deferred.
    """
    last_error: AlignmentNotFound | None = None
    for algorithm in (ALIGNMENT_ALGORITHM, BERTALIGN_ALGORITHM):
        try:
            return _resolve_alignment_route(connection, source_id, target_id, algorithm)
        except AlignmentNotFound as exc:
            last_error = exc
    raise last_error or AlignmentNotFound("这两个版本还没有可用的自动对齐。")


