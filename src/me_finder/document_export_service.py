"""Application service for exporting one indexed PDF as mefinder.document.v1."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional

from .document_export import (
    DOCUMENT_SCHEMA_VERSION,
    DocumentExportError,
    document_manifest,
    export_document_zip,
)
from .document_heading import (
    DOCUMENT_HEADING_VERSION,
    HEADING_SOURCE_PDF_OUTLINE,
    enrich_pdf_headings,
    find_content_list_v2,
)
from .database import _sanitize_surrogates_in_place
from .markdown_export import document_to_markdown, safe_markdown_filename
from .pdf_extractors import file_sha256


class IndexedDocumentNotFound(DocumentExportError):
    """The requested source is not present in the current search index."""


class UnsupportedDocumentExport(DocumentExportError):
    """The indexed source cannot be represented by the page export schema."""


def export_indexed_pdf(
    *,
    database_path: Path,
    runtime_root: Path,
    source_file_id: str,
    output_dir: Path,
    include_source_pdf: bool = False,
) -> Dict[str, object]:
    """Stream one indexed PDF into an atomic Zip64 document export."""

    source_id = str(source_file_id or "").strip()
    if not source_id or len(source_id) > 256:
        raise IndexedDocumentNotFound("缺少要导出的文献标识。")
    database = Path(database_path)
    if not database.is_file():
        raise IndexedDocumentNotFound("当前文献索引不存在。")

    # Ensure canonical heading metadata is persisted so it travels inside the
    # exported package (older libraries were indexed before enrichment existed).
    ensure_document_headings(
        database_path=database,
        runtime_root=runtime_root,
        source_file_id=source_id,
    )

    with closing(_connect(database)) as connection:
        source = _payload_row(
            connection,
            "SELECT payload_json FROM source_files WHERE source_file_id = ?",
            (source_id,),
        )
        if source is None:
            raise IndexedDocumentNotFound("文献不存在或已从文献库移除。")
        if str(source.get("source_type") or "") != "pdf":
            raise UnsupportedDocumentExport(
                "当前 mefinder.document.v1 单书导出仅支持 PDF 文献。"
            )
        volume = _payload_row(
            connection,
            "SELECT payload_json FROM volumes WHERE source_file_id = ? "
            "ORDER BY volume_number, volume_id LIMIT 1",
            (source_id,),
        ) or {}
        latest_run = _payload_row(
            connection,
            "SELECT payload_json FROM pdf_import_runs WHERE source_file_id = ? "
            "ORDER BY row_id DESC LIMIT 1",
            (source_id,),
        ) or {}
        first_page = _payload_row(
            connection,
            "SELECT payload_json FROM pdf_pages WHERE source_file_id = ? "
            "ORDER BY pdf_page_index LIMIT 1",
            (source_id,),
        ) or {}
        page_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM pdf_pages WHERE source_file_id = ?",
                (source_id,),
            ).fetchone()[0]
        )
        if page_count < 1:
            raise UnsupportedDocumentExport(
                "这份 PDF 还没有可导出的页级解析结果。"
            )
        warnings = [
            item
            for item in _payload_rows(
                connection,
                "SELECT payload_json FROM audit_issues "
                "WHERE source_file_id = ? ORDER BY row_id",
                (source_id,),
            )
        ]
        missing_ranges = _missing_page_ranges(connection, source_id, source)

    profile = (
        source.get("pdf_profile")
        if isinstance(source.get("pdf_profile"), Mapping)
        else {}
    )
    bibliographic = (
        source.get("bibliographic_metadata")
        if isinstance(source.get("bibliographic_metadata"), Mapping)
        else {}
    )
    title = str(
        bibliographic.get("title")
        or source.get("display_title")
        or volume.get("display_title")
        or Path(str(source.get("file_name") or source_id)).stem
    )
    source_pdf = (
        _source_pdf_path(source, Path(runtime_root))
        if include_source_pdf
        else None
    )
    source_digest = (
        file_sha256(source_pdf)
        if source_pdf is not None
        else _source_digest(source, Path(runtime_root))
    )
    parser_provider = str(
        profile.get("provider_id")
        or first_page.get("parser")
        or profile.get("parser")
        or "mefinder-pdf"
    )
    parser_provenance = {
        key: value
        for key, value in (
            ("provider_name", profile.get("provider_name")),
            ("detected_pdf_type", profile.get("detected_pdf_type")),
            ("import_run_id", latest_run.get("run_id")),
            ("document_job_id", profile.get("document_job_id")),
        )
        if value not in (None, "")
    }
    manifest = document_manifest(
        document={
            "source_file_id": source_id,
            "document_id": source.get("document_id"),
            "title": title,
        },
        source_sha256=source_digest,
        source_file={
            "file_name": source.get("file_name"),
            "file_format": source.get("file_format") or "pdf",
            "size_bytes": source.get("size_bytes"),
            "last_modified": source.get("last_modified"),
        },
        bibliographic_metadata=bibliographic,
        external_ids=_external_ids(bibliographic, source),
        parser_provider=parser_provider,
        parser_model=(
            str(profile.get("model")) if profile.get("model") else None
        ),
        parser_version=(
            str(first_page.get("parser_version"))
            if first_page.get("parser_version")
            else None
        ),
        parser_provenance=parser_provenance,
        parsed_at=str(
            latest_run.get("finished_at") or latest_run.get("started_at") or ""
        ) or None,
        warnings=warnings,
        missing_ranges=missing_ranges,
        page_count=page_count,
    )
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = destination_dir / (
        f"{_safe_file_stem(title)}-{timestamp}-{uuid.uuid4().hex[:6]}.mefinder.zip"
    )
    export_document_zip(
        destination,
        manifest,
        iter_indexed_pdf_pages(database, source_id),
        source_pdf_path=source_pdf,
    )
    return {
        "ok": True,
        "source_file_id": source_id,
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "path": str(destination.resolve()),
        "size_bytes": destination.stat().st_size,
        "page_count": page_count,
        "warning_count": len(warnings),
        "missing_ranges": missing_ranges,
        "includes_source_pdf": source_pdf is not None,
    }


def iter_indexed_pdf_pages(
    database_path: Path, source_file_id: str
) -> Iterator[Dict[str, object]]:
    """Read page payloads incrementally so a large book is never materialized."""

    connection = _connect(Path(database_path))
    try:
        cursor = connection.execute(
            "SELECT payload_json FROM pdf_pages WHERE source_file_id = ? "
            "ORDER BY pdf_page_index, row_id",
            (str(source_file_id),),
        )
        for row in cursor:
            payload = _decode_payload(row[0])
            if payload is None:
                raise DocumentExportError("索引中的 PDF 页数据已损坏。")
            yield payload
    finally:
        connection.close()


def export_indexed_pdf_markdown(
    *,
    database_path: Path,
    source_file_id: str,
    output_dir: Path,
    runtime_root: Optional[Path] = None,
) -> Dict[str, object]:
    """Export one indexed PDF's persisted structured data as UTF-8 Markdown."""

    source_id = str(source_file_id or "").strip()
    if not source_id or len(source_id) > 256:
        raise IndexedDocumentNotFound("缺少要导出的文献标识。")
    database = Path(database_path)
    if not database.is_file():
        raise IndexedDocumentNotFound("当前文献索引不存在。")

    # Older libraries were indexed before canonical heading enrichment existed;
    # bring them up to the current version from cached artifacts before reading.
    if runtime_root is not None:
        ensure_document_headings(
            database_path=database,
            runtime_root=runtime_root,
            source_file_id=source_id,
        )

    with closing(_connect(database)) as connection:
        source = _payload_row(
            connection,
            "SELECT payload_json FROM source_files WHERE source_file_id = ?",
            (source_id,),
        )
        if source is None:
            raise IndexedDocumentNotFound("文献不存在或已从文献库移除。")
        if str(source.get("source_type") or "") != "pdf":
            raise UnsupportedDocumentExport(
                "Markdown 导出仅支持已解析的 PDF 文献。"
            )
        volume = _payload_row(
            connection,
            "SELECT payload_json FROM volumes WHERE source_file_id = ? "
            "ORDER BY volume_number, volume_id LIMIT 1",
            (source_id,),
        ) or {}
        page_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM pdf_pages WHERE source_file_id = ?",
                (source_id,),
            ).fetchone()[0]
        )
        if page_count < 1:
            raise UnsupportedDocumentExport(
                "这份 PDF 还没有可导出的页级解析结果。"
            )

    bibliographic = (
        source.get("bibliographic_metadata")
        if isinstance(source.get("bibliographic_metadata"), Mapping)
        else {}
    )
    title = str(
        bibliographic.get("title")
        or source.get("display_title")
        or volume.get("display_title")
        or Path(str(source.get("file_name") or source_id)).stem
    )
    author = bibliographic.get("author")
    markdown = document_to_markdown(
        iter_indexed_pdf_pages(database, source_id),
        title=title,
        author=author,
    )
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / safe_markdown_filename(title)
    partial = destination.with_name(destination.name + ".partial")
    partial.write_text(markdown, encoding="utf-8", newline="\n")
    partial.replace(destination)
    return {
        "ok": True,
        "source_file_id": source_id,
        "path": str(destination.resolve()),
        "size_bytes": destination.stat().st_size,
        "page_count": page_count,
    }


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
    transaction; enrichment failures never block export.
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

    profile = {
        "version": DOCUMENT_HEADING_VERSION,
        "status": status,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources_used,
        "outline_classification": classification,
    }
    source["pdf_outline"] = outline
    source["document_heading_profile"] = profile

    # PDF bookmark/outline strings are decoded with ``surrogateescape``, so they
    # can carry lone UTF-16 surrogate code points.  SQLite stores ``str`` as
    # UTF-8, which forbids them, and the write below would otherwise raise
    # "surrogates not allowed" and abort the whole export.  Scrub in place so the
    # re-enriched payloads (and the Markdown later built from them) stay clean.
    _sanitize_surrogates_in_place(source)
    for page in pages:
        _sanitize_surrogates_in_place(page)

    with closing(_connect(database)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
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
    return profile


def _source_digest(source: Mapping[str, object], runtime_root: Path) -> str:
    digest = str(source.get("sha256") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest):
        return digest
    relative = str(source.get("relative_path") or "").strip()
    candidate = Path(relative)
    if relative and not candidate.is_absolute():
        candidate = Path(runtime_root) / candidate
    if not relative or not candidate.is_file():
        raise UnsupportedDocumentExport(
            "文献索引缺少 source_sha256，且原 PDF 不可读。"
        )
    return file_sha256(candidate)


def _source_pdf_path(source: Mapping[str, object], runtime_root: Path) -> Path:
    relative = str(source.get("relative_path") or "").strip()
    candidate = Path(relative)
    if relative and not candidate.is_absolute():
        candidate = Path(runtime_root) / candidate
    if not relative or not candidate.is_file():
        raise UnsupportedDocumentExport(
            "找不到这份文献的原 PDF，无法导出包含原 PDF 的文档包。"
        )
    return candidate


def _missing_page_ranges(
    connection: sqlite3.Connection,
    source_file_id: str,
    source: Mapping[str, object],
) -> list[Dict[str, int]]:
    profile = source.get("pdf_profile")
    expected_total = 0
    if isinstance(profile, Mapping):
        try:
            expected_total = int(profile.get("pdf_page_count") or 0)
        except (TypeError, ValueError):
            expected_total = 0
    expected_index = 0
    missing: list[Dict[str, int]] = []
    for row in connection.execute(
        "SELECT pdf_page_index FROM pdf_pages WHERE source_file_id = ? "
        "ORDER BY pdf_page_index, row_id",
        (source_file_id,),
    ):
        page_index = int(row[0])
        if page_index > expected_index:
            missing.append(
                {"page_start": expected_index + 1, "page_end": page_index}
            )
        expected_index = max(expected_index, page_index + 1)
    if expected_total > expected_index:
        missing.append(
            {"page_start": expected_index + 1, "page_end": expected_total}
        )
    return missing


def _external_ids(
    bibliographic: Mapping[str, object], source: Mapping[str, object]
) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key in ("isbn", "issn", "doi", "cnki_id"):
        value = bibliographic.get(key) or source.get(key)
        if value not in (None, "", []):
            result[key] = value
    return result


def _safe_file_stem(value: object) -> str:
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", str(value or "")).strip(" .-")
    if not stem:
        stem = "MEFinder-document"
    encoded = stem.encode("utf-8")
    if len(encoded) > 120:
        stem = encoded[:120].decode("utf-8", errors="ignore").rstrip(" .-")
    return stem or "MEFinder-document"


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(Path(path)), timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _payload_row(
    connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> Optional[Dict[str, object]]:
    row = connection.execute(sql, parameters).fetchone()
    return _decode_payload(row[0]) if row is not None else None


def _payload_rows(
    connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> Iterator[Dict[str, object]]:
    for row in connection.execute(sql, parameters):
        payload = _decode_payload(row[0])
        if payload is not None:
            yield payload


def _decode_payload(value: object) -> Optional[Dict[str, object]]:
    try:
        payload = json.loads(str(value or ""))
    except json.JSONDecodeError:
        return None
    return dict(payload) if isinstance(payload, dict) else None
