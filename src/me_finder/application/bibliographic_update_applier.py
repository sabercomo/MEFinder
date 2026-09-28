"""Apply user-confirmed agent fills from the desktop runtime.

Only this process owns the import-config lock, so the MCP sidecar merely
queues confirmed requests and this applier writes them through the normal
manual-save path. Each request is re-planned against the document read
inside that write lock: empty fields are filled, existing values are kept and
reported as conflicts.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Mapping, Optional

from ..bibliographic_fill import manual_save_payload, plan_fill
from ..bibliographic_updates import list_requests, record_request_result
from ..bibliographic_values import canonical_metadata

POLL_SECONDS = 5.0
BATCH_LIMIT = 20

FillEmptyFields = Callable[
    [str, Callable[[Mapping[str, object]], Optional[Mapping[str, object]]]],
    Optional[Mapping[str, object]],
]


def apply_confirmed_updates(index_path: Path, fill_empty_fields: FillEmptyFields) -> int:
    """Apply every confirmed request once, oldest first; return how many closed."""

    closed = 0
    requests = list_requests(index_path, status="confirmed", limit=BATCH_LIMIT)
    for request in reversed(requests):
        plan: list[dict[str, object]] = []

        def build(document: Mapping[str, object]) -> Optional[Mapping[str, object]]:
            current = canonical_metadata(document)
            plan[:] = plan_fill(current, request["fields"])
            if not any(item["action"] == "fill" for item in plan):
                return None
            return manual_save_payload(current, plan)

        try:
            fill_empty_fields(str(request["source_file_id"]), build)
        except (ValueError, LookupError, OSError, sqlite3.Error) as exc:
            status, result = "failed", {"message": str(exc)[:500]}
        else:
            status, result = "applied", {
                "filled": [item["field"] for item in plan if item["action"] == "fill"],
                "unchanged": [item["field"] for item in plan if item["action"] == "same"],
                "conflicts": [
                    {
                        "field": item["field"],
                        "kept_value": item["current_value"],
                        "proposed_value": item["value"],
                    }
                    for item in plan
                    if item["action"] == "conflict"
                ],
            }
        if record_request_result(index_path, str(request["request_id"]), status=status, result=result):
            closed += 1
    return closed


def run_applier(
    index_path: Path,
    fill_empty_fields: FillEmptyFields,
    stop: threading.Event,
    *,
    interval: float = POLL_SECONDS,
) -> None:
    """Poll until ``stop`` is set; a failing tick never ends the loop."""

    while True:
        try:
            apply_confirmed_updates(index_path, fill_empty_fields)
        except RuntimeError:
            # Durable operations reject new writes once shutdown has begun.
            if stop.is_set():
                return
            logging.exception("bibliographic update tick failed")
        except Exception:  # noqa: BLE001 - the loop must survive
            logging.exception("bibliographic update tick failed")
        if stop.wait(interval):
            return
