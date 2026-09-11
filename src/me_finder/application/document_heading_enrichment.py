"""Coordinated document heading enrichment as a standalone application operation.

Historically this enrichment ran inside every export and blindly rewrote full
stale payloads, so a bibliographic save that committed while the enrichment
was computing was silently reverted. It is now an independent application
operation wired into the existing write coordination (durable operations +
index runtime mutation), and its single write transaction re-reads the
document fresh: if anything touched the document while the enrichment was
computing, the write is skipped and a later run retries. Export itself no
longer writes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import AbstractContextManager, ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional, Protocol

from ..database import _sanitize_surrogates_in_place
from ..document_heading import (
    DOCUMENT_HEADING_VERSION,
    HEADING_SOURCE_PDF_OUTLINE,
    enrich_pdf_headings,
    find_content_list_v2,
)


class EnrichmentDurableOperationsPort(Protocol):
    def operation(self) -> AbstractContextManager[None]:
        ...


class EnrichmentIndexPort(Protocol):
    def mutation(self) -> AbstractContextManager[None]:
        ...


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(Path(path)), timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _payload_digest(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


class DocumentHeadingEnrichment:
    """Run one document's heading enrichment under the write coordination."""

    def __init__(
        self,
        *,
        database_path: Path,
        runtime_root: Path,
        durable_operations: Optional[EnrichmentDurableOperationsPort] = None,
        index_runtime: Optional[EnrichmentIndexPort] = None,
    ) -> None:
        self._database_path = Path(database_path)
        self._runtime_root = Path(runtime_root)
        self._durable_operations = durable_operations
        self._index_runtime = index_runtime

    def enrich(self, source_file_id: str) -> Dict[str, object]:
        """Enrich one document, serialized with other library writers."""

        with ExitStack() as coordination:
            if self._durable_operations is not None:
                coordination.enter_context(self._durable_operations.operation())
            if self._index_runtime is not None:
                coordination.enter_context(self._index_runtime.mutation())
            return ensure_document_headings(
                database_path=self._database_path,
                runtime_root=self._runtime_root,
                source_file_id=str(source_file_id),
            )


def ensure_document_headings(
    *,
    database_path: Path,
    runtime_root: Path,
    source_file_id: str,
) -> Dict[str, object]:
    """Lazily enrich an indexed PDF with canonical document heading metadata.

    Idempotent: returns immediately when the source already carries a
    ``document_heading_profile`` at the current version with status ``complete``.
    Otherwise it recomputes headings from the existing DB plus the original PDF's
    native outline and any cached MinerU ``content_list_v2`` — never re-OCRing,
    calling MinerU, reparsing body text, rebuilding the index, or changing the
    schema/``text_raw``/``text_level``/page mapping.  All writes happen in one
    transaction that re-reads the document fresh; when the document changed or
    disappeared while the enrichment was computing, nothing is written and the
    returned profile reports ``deferred``/``unavailable`` so a later run retries.
    """

    database = Path(database_path)
    root = Path(runtime_root)
    if not database.is_file():
        return {"version": DOCUMENT_HEADING_VERSION, "status": "unavailable"}

    with closing(_connect(database)) as connection:
        row = connection.execute(
            "SELECT payload_json FROM source_files WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchone()
        if row is None:
            return {"version": DOCUMENT_HEADING_VERSION, "status": "unavailable"}
        source = json.loads(row[0])
        if str(source.get("source_type") or "") != "pdf":
            return {"version": DOCUMENT_HEADING_VERSION, "status": "unavailable"}
        profile = source.get("document_heading_profile")
        if (
            isinstance(profile, Mapping)
            and profile.get("version") == DOCUMENT_HEADING_VERSION
            and profile.get("status") == "complete"
        ):
            return dict(profile)  # already enriched at this version

        page_rows = connection.execute(
            "SELECT pdf_page_index, payload_json FROM pdf_pages "
            "WHERE source_file_id = ? ORDER BY pdf_page_index",
            (source_file_id,),
        ).fetchall()
        pages = [json.loads(r[1]) for r in page_rows]

    # The single write transaction must be able to prove the document is
    # untouched since this snapshot; remember exactly what was read.
    source_digest = _payload_digest(source)
    pages_digest = [_payload_digest(page) for page in pages]

    # Locate original PDF (optional) and cached MinerU artifacts (optional).
    relative = str(source.get("relative_path") or "").strip()
    pdf_candidate = Path(relative)
    if relative and not pdf_candidate.is_absolute():
        pdf_candidate = root / pdf_candidate
    pdf_path = pdf_candidate if relative and pdf_candidate.is_file() else None

    document_job_id = _manifest_document_job_id(root, source_file_id)
    segments = _reconstruct_segments(pages, root, document_job_id)

    v2_available = any(
        find_content_list_v2(seg["result_dir"]) is not None for seg in segments
    ) or (
        document_job_id is not None
        and find_content_list_v2(None, root=root, document_job_id=document_job_id)
        is not None
    )

    try:
        outline = enrich_pdf_headings(pages, pdf_path, segments, root=root)
    except Exception:  # pragma: no cover - never let enrichment block export
        logging.exception("lazy document-heading enrichment failed")
        return {"version": DOCUMENT_HEADING_VERSION, "status": "unavailable"}

    sources_used = sorted(
        {
            str(block.get("document_heading_source"))
            for page in pages
            for block in page.get("blocks") or []
            if isinstance(block, Mapping) and block.get("document_heading_source")
        }
    )
    classification = str(outline.get("classification") or "none")
    if classification == "semantic" and HEADING_SOURCE_PDF_OUTLINE in sources_used:
        status = "complete"
    elif pdf_path is None and not v2_available:
        status = "unavailable"
    elif document_job_id is not None and not v2_available:
        status = "partial"  # a referenced v2 artifact is missing; retry later
    else:
        status = "complete"

    new_profile = {
        "version": DOCUMENT_HEADING_VERSION,
        "status": status,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources_used,
        "outline_classification": classification,
    }
    source["pdf_outline"] = outline
    source["document_heading_profile"] = new_profile

    # PDF bookmark/outline strings are decoded with ``surrogateescape``, so they
    # can carry lone UTF-16 surrogate code points.  SQLite stores ``str`` as
    # UTF-8, which forbids them, and the write below would otherwise raise
    # "surrogates not allowed" and abort the whole operation.  Scrub in place so
    # the re-enriched payloads (and the Markdown later built from them) stay clean.
    _sanitize_surrogates_in_place(source)
    for page in pages:
        _sanitize_surrogates_in_place(page)

    with closing(_connect(database)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT payload_json FROM source_files WHERE source_file_id = ?",
                (source_file_id,),
            ).fetchone()
            if current_row is None:
                # The document was deleted while the enrichment computed.
                connection.rollback()
                return {"version": DOCUMENT_HEADING_VERSION, "status": "unavailable"}
            current_page_rows = connection.execute(
                "SELECT pdf_page_index, payload_json FROM pdf_pages "
                "WHERE source_file_id = ? ORDER BY pdf_page_index",
                (source_file_id,),
            ).fetchall()
            current_pages = [json.loads(r[1]) for r in current_page_rows]
            untouched = (
                _payload_digest(json.loads(current_row[0])) == source_digest
                and [_payload_digest(page) for page in current_pages] == pages_digest
                and len(current_pages) == len(pages)
            )
            if not untouched:
                # A bibliographic save, re-import or other writer changed the
                # document while this enrichment was computing. Never overwrite
                # that newer data; the next run re-enriches from the fresh state.
                connection.rollback()
                logging.info(
                    "document %s changed during heading enrichment; write deferred",
                    source_file_id,
                )
                return {"version": DOCUMENT_HEADING_VERSION, "status": "deferred"}
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
        except Exception:
            connection.rollback()
            raise
    return new_profile


def _reconstruct_segments(
    pages: list, runtime_root: Path, document_job_id: Optional[str]
) -> list:
    """Rebuild MinerU segment descriptors from persisted block metadata.

    Every indexed block records its ``result_dir`` and page geometry, so we can
    recover the per-segment result directory and page-index offset without the
    original import config.  ``document_job_id`` (from the on-disk manifest, when
    present) lets the engine path locate whole-document v2 under parser_jobs.
    """

    groups: Dict[str, int] = {}
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        for block in page.get("blocks") or []:
            if not isinstance(block, Mapping):
                continue
            raw_dir = block.get("result_dir")
            if not raw_dir:
                continue
            result_dir = Path(str(raw_dir))
            if not result_dir.is_absolute():
                result_dir = Path(runtime_root) / result_dir
            key = str(result_dir)
            if key in groups:
                continue
            offset = block.get("page_index_offset")
            if offset in (None, ""):
                try:
                    offset = int(block.get("pdf_page_index")) - int(
                        block.get("local_page_idx")
                    )
                except (TypeError, ValueError):
                    offset = 0
            try:
                groups[key] = int(offset)
            except (TypeError, ValueError):
                groups[key] = 0
    return [
        {
            "result_dir": result_dir,
            "page_index_offset": offset,
            "document_job_id": document_job_id,
        }
        for result_dir, offset in groups.items()
    ]


def _manifest_document_job_id(runtime_root: Path, source_file_id: str) -> Optional[str]:
    manifest = (
        Path(runtime_root)
        / "corpus"
        / "processed"
        / "mineru"
        / "manifests"
        / f"segments-{source_file_id}.json"
    )
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    job = data.get("document_job_id") if isinstance(data, Mapping) else None
    return str(job) if job else None
