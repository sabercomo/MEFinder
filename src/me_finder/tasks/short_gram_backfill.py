"""Drain the short-query index backlog while the desktop runtime is up.

Imports, deletions, rebuilds and the v10 migration only queue paragraphs;
searches stay correct meanwhile because the prefilter admits every queued
rowid. This loop tokenizes the queue in short write transactions, each run
while index publication cannot swap the database file underneath it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional, TypeVar

from ..persistence.short_gram_index import drain_short_gram_backlog_at

T = TypeVar("T")
RunWhenReady = Callable[[Callable[[Path], T]], Optional[T]]

IDLE_SECONDS = 3.0
# Yield between batches so queued searches get the runtime lock.
BATCH_PAUSE_SECONDS = 0.05


def run_short_gram_backfill(
    run_when_ready: RunWhenReady,
    stop: threading.Event,
    *,
    idle_seconds: float = IDLE_SECONDS,
    drain: Callable[[Path], int] = drain_short_gram_backlog_at,
) -> None:
    """Poll until ``stop`` is set; a failing batch never ends the loop."""

    while not stop.is_set():
        try:
            drained = run_when_ready(drain) or 0
        except Exception:  # noqa: BLE001 - the loop must survive
            logging.exception("short-gram backfill batch failed")
            drained = 0
        if stop.wait(BATCH_PAUSE_SECONDS if drained else idle_seconds):
            return
