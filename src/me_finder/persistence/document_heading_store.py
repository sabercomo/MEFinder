"""SQLite reads and the guarded write for document heading enrichment.

The application operation (``application.document_heading_enrichment``)
computes headings outside any lock; this store owns the two SQL touch points:
the initial snapshot read and the single ``BEGIN IMMEDIATE`` transaction that
re-reads the document and writes only when it is unchanged since the snapshot.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Callable, List, Literal, Optional, Tuple

from .connection import PROJECT_BUSY_TIMEOUT_MS, connect_index

WriteOutcome = Literal["written", "missing", "changed"]


class SQLiteDocumentHeadingStore:
    """Read a PDF's source/page payloads and publish enriched ones."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)

    def is_available(self) -> bool:
        return self._database_path.is_file()

    def _connect(self, *, write: bool = False):
        return connect_index(
            self._database_path,
            write=write,
            busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS,
        )

    def read_snapshot(
        self,
        source_file_id: str,
        *,
        needs_pages: Callable[[dict], bool],
    ) -> Optional[Tuple[dict, Optional[List[dict]]]]:
        """Return ``(source_payload, page_payloads)`` or ``None`` when missing.

        Page payloads are read on the same connection only when
        ``needs_pages(source_payload)`` is true; otherwise the second item is
        ``None`` so an already-enriched document costs one row read.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM source_files WHERE source_file_id = ?",
                (source_file_id,),
            ).fetchone()
            if row is None:
                return None
            source = json.loads(row[0])
            if not needs_pages(source):
                return source, None
            page_rows = connection.execute(
                "SELECT pdf_page_index, payload_json FROM pdf_pages "
                "WHERE source_file_id = ? ORDER BY pdf_page_index",
                (source_file_id,),
            ).fetchall()
            return source, [json.loads(r[1]) for r in page_rows]

    def write_if_unchanged(
        self,
        source_file_id: str,
        *,
        source: dict,
        pages: List[dict],
        source_digest: str,
        page_digests: List[str],
        digest: Callable[[object], str],
    ) -> WriteOutcome:
        """Write ``source``/``pages`` only if the stored rows still match.

        Returns ``"missing"`` when the document was deleted and ``"changed"``
        when any payload moved since the snapshot; neither case writes.
        """

        with closing(self._connect(write=True)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current_row = connection.execute(
                    "SELECT payload_json FROM source_files WHERE source_file_id = ?",
                    (source_file_id,),
                ).fetchone()
                if current_row is None:
                    connection.rollback()
                    return "missing"
                current_page_rows = connection.execute(
                    "SELECT pdf_page_index, payload_json FROM pdf_pages "
                    "WHERE source_file_id = ? ORDER BY pdf_page_index",
                    (source_file_id,),
                ).fetchall()
                current_pages = [json.loads(r[1]) for r in current_page_rows]
                untouched = (
                    digest(json.loads(current_row[0])) == source_digest
                    and [digest(page) for page in current_pages] == page_digests
                    and len(current_pages) == len(pages)
                )
                if not untouched:
                    connection.rollback()
                    return "changed"
                connection.execute(
                    "UPDATE source_files SET payload_json = ? WHERE source_file_id = ?",
                    (json.dumps(source, ensure_ascii=False), source_file_id),
                )
                for page in pages:
                    connection.execute(
                        "UPDATE pdf_pages SET payload_json = ? "
                        "WHERE source_file_id = ? AND pdf_page_index = ?",
                        (
                            json.dumps(page, ensure_ascii=False),
                            source_file_id,
                            int(page.get("pdf_page_index")),
                        ),
                    )
                connection.commit()
                return "written"
            except Exception:
                connection.rollback()
                raise
