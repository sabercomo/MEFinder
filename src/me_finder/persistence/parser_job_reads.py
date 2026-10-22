"""Read-only lookups in the parser job ledger (``data/parser_jobs.sqlite3``).

The ledger itself is owned by ``large_document.job_ledger``; this module only
answers questions later stages ask about finished jobs, without migrating or
writing the ledger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .connection import open_readonly_snapshot


def read_slice_job_id(
    ledger_path: Path, document_job_id: str, page_start: int, page_end: int
) -> Optional[str]:
    """Return the slice job that parsed ``page_start..page_end`` of a job."""

    if not Path(ledger_path).is_file():
        return None
    try:
        with open_readonly_snapshot(ledger_path) as connection:
            row = connection.execute(
                "SELECT id FROM slice_jobs WHERE document_job_id = ? "
                "AND page_start = ? AND page_end = ?",
                (document_job_id, page_start, page_end),
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row["id"]) if row else None
