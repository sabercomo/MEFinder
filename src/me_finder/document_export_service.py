"""Application service for exporting one indexed PDF as mefinder.document.v1."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional

from .document_export import (
    DOCUMENT_SCHEMA_VERSION,
    DocumentExportError,
    document_manifest,
    export_document_zip,
)
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
) -> Dict[str, object]:
    """Stream one indexed PDF into an atomic Zip64 document export."""

    source_id = str(source_file_id or "").strip()
    if not source_id or len(source_id) > 256:
        raise IndexedDocumentNotFound("缺少要导出的文献标识。")
    database = Path(database_path)
    if not database.is_file():
        raise IndexedDocumentNotFound("当前文献索引不存在。")

    with _connect(database) as connection:
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
    source_digest = _source_digest(source, Path(runtime_root))
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
