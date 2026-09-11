"""Application services for exporting indexed MEFinder documents."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional

from .document_export import (
    DOCUMENT_SCHEMA_VERSION,
    DocumentExportError,
    document_manifest,
    export_document_zip,
)
from .database import paragraph_from_database_row
from .epub_export import safe_epub_filename, write_epub
from .markdown_export import (
    document_to_markdown,
    epub_paragraphs_to_markdown,
    safe_markdown_filename,
)
from .export_footnotes import normalize_document_export
from .markdown_page_selection import (
    PageSelection, resolve_pdf_pages, select_normalized_pages, select_epub_paragraphs,
)
from .markdown_export_normalize import ExportOptions
from .export_page_reconstruction import attach_export_layout
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

    # One explicit read transaction: every row below, including the streamed
    # pages, comes from a single database snapshot. Concurrent bibliographic
    # saves, deletions or re-imports therefore never mix into this export.
    with _snapshot_connection(database) as connection:
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
            iter_indexed_pdf_pages(database, source_id, connection=connection),
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


@contextmanager
def _snapshot_connection(database: Path) -> Iterator[sqlite3.Connection]:
    """Hold one explicit read transaction for a consistent export snapshot.

    The library runs in rollback-journal mode, so a deferred transaction pins
    one committed version for every read below; concurrent writers wait until
    the reads finish instead of leaking into a half-updated export.
    """

    connection = _connect(database)
    try:
        connection.execute("BEGIN DEFERRED")
        yield connection
    finally:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        connection.close()


def iter_indexed_pdf_pages(
    database_path: Optional[Path],
    source_file_id: str,
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> Iterator[Dict[str, object]]:
    """Read page payloads incrementally so a large book is never materialized.

    ``connection`` keeps the pages on the caller's read transaction so a
    streaming export never mixes two snapshots; without it a short-lived
    connection is opened for compatibility with direct callers.
    """

    if connection is None:
        if database_path is None:
            raise ValueError("iter_indexed_pdf_pages needs a database path or connection")
        owning_connection = True
        connection = _connect(Path(database_path))
    else:
        owning_connection = False
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
        if owning_connection:
            connection.close()


def export_indexed_pdf_markdown(
    *,
    database_path: Path,
    source_file_id: str,
    output_dir: Path,
    runtime_root: Optional[Path] = None,
    options: Optional[ExportOptions] = None,
    page_selection: Optional[PageSelection] = None,
) -> Dict[str, object]:
    """Export one indexed PDF or EPUB as UTF-8 Markdown.

    ``options`` carries the format-neutral page-anchor policy and page-cleanup
    flags (see :class:`ExportOptions`); the default is the ``printed`` marker
    mode with visible page numbers and running headers/footers removed.
    """

    options = options or ExportOptions()
    selection_result = None
    warnings = []

    source_id = str(source_file_id or "").strip()
    if not source_id or len(source_id) > 256:
        raise IndexedDocumentNotFound("缺少要导出的文献标识。")
    database = Path(database_path)
    if not database.is_file():
        raise IndexedDocumentNotFound("当前文献索引不存在。")

    # One explicit read transaction: the source row and every page or
    # paragraph payload below come from one database snapshot.
    with _snapshot_connection(database) as connection:
        source = _payload_row(
            connection,
            "SELECT payload_json FROM source_files WHERE source_file_id = ?",
            (source_id,),
        )
        if source is None:
            raise IndexedDocumentNotFound("文献不存在或已从文献库移除。")
        source_type = str(source.get("source_type") or "")
        source_format = str(source.get("file_format") or "").lower()
        is_pdf = source_type == "pdf"
        is_epub = source_format == "epub" or str(
            source.get("file_name") or ""
        ).lower().endswith(".epub")
        if not is_pdf and not is_epub:
            raise UnsupportedDocumentExport(
                "Markdown 导出仅支持已解析的 PDF 或 EPUB 文献。"
            )
        volume = _payload_row(
            connection,
            "SELECT payload_json FROM volumes WHERE source_file_id = ? "
            "ORDER BY volume_number, volume_id LIMIT 1",
            (source_id,),
        ) or {}
        if is_pdf:
            item_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM pdf_pages WHERE source_file_id = ?",
                    (source_id,),
                ).fetchone()[0]
            )
            if item_count < 1:
                raise UnsupportedDocumentExport(
                    "这份 PDF 还没有可导出的页级解析结果。"
                )
        else:
            item_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM paragraphs WHERE source_file_id = ?",
                    (source_id,),
                ).fetchone()[0]
            )
            if item_count < 1:
                raise UnsupportedDocumentExport("这份 EPUB 没有可导出的正文。")
        if is_pdf:
            pages = _text_export_pages(connection, source_id, runtime_root)
        else:
            paragraphs = list(
                map(
                    paragraph_from_database_row,
                    connection.execute(
                        "SELECT * FROM paragraphs WHERE source_file_id = ? "
                        "ORDER BY paragraph_index, rowid",
                        (source_id,),
                    ),
                )
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
    if is_pdf:
        selected = resolve_pdf_pages(pages, page_selection) if page_selection is not None else None
        normalized = normalize_document_export(pages, options=options)
        if selected is not None:
            normalized = select_normalized_pages(normalized, pages, selected, options)
            item_count = len(selected)
            selection_result = {'mode': page_selection.mode, 'pages': page_selection.expression,
                                'physical_pages': selected,
                                'note_count': normalized.footnote_report['selection']['note_count']}
            if normalized.footnote_report.get('unresolved_ref_count', 0):
                warnings.append('原书存在未能可靠配对的脚注；原标记保留，未猜测页外脚注。')
        markdown = document_to_markdown(
            pages,
            title=title,
            author=author,
            options=options,
            normalized=normalized,
        )
    else:
        if page_selection is not None:
            paragraphs = select_epub_paragraphs(paragraphs, page_selection)
            item_count = len(paragraphs)
            selection_result = {'mode': 'printed', 'pages': page_selection.expression,
                                'labels': list(page_selection.labels)}
            warnings.append('EPUB 当前入库文本未保留超链接脚注关系；按页导出不保证带出页外脚注。')
        markdown = epub_paragraphs_to_markdown(
            paragraphs,
            title=title,
            author=author,
            options=options,
        )
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    export_title = title
    if page_selection is not None:
        label = '原书页' if page_selection.mode == 'printed' else 'PDF页'
        # Keep the selection suffix even for titles longer than the filename cap.
        stem = safe_markdown_filename(title)[:-3].encode('utf-8')[:48].decode('utf-8', errors='ignore')
        digest = hashlib.sha256(repr(page_selection).encode()).hexdigest()[:8]
        expression = page_selection.expression.encode('utf-8')[:32].decode('utf-8', errors='ignore')
        export_title = f'{stem}-{label}-{digest}-{expression}'
        metadata = json.dumps({'mode': page_selection.mode, 'pages': page_selection.expression}, ensure_ascii=False)
        boundary = markdown.index('\n---\n') + len('\n---\n')
        markdown = markdown[:boundary] + '\n<!-- MEFinder page_selection: ' + metadata + ' -->\n' + markdown[boundary:]
        if warnings:
            markdown += '\n> 导出说明：' + '；'.join(warnings) + '\n'
    destination = destination_dir / safe_markdown_filename(export_title)
    partial = destination.with_name(destination.name + ".partial")
    partial.write_text(markdown, encoding="utf-8", newline="\n")
    partial.replace(destination)
    result: Dict[str, object] = {
        "ok": True,
        "source_file_id": source_id,
        "path": str(destination.resolve()),
        "size_bytes": destination.stat().st_size,
        "page_count": item_count if is_pdf else int(source.get("epub_page_count") or 0),
    }
    if selection_result is not None:
        result['page_selection'] = selection_result
        result['warnings'] = warnings
        if is_epub:
            result['page_count'] = len(page_selection.labels)
    if is_pdf:
        result["footnote_report"] = normalized.footnote_report
        result["reconstruction_report"] = normalized.reconstruction_report
    else:
        result["paragraph_count"] = item_count
    return result


def export_indexed_pdf_epub(
    *,
    database_path: Path,
    source_file_id: str,
    output_dir: Path,
    runtime_root: Optional[Path] = None,
    options: Optional[ExportOptions] = None,
) -> Dict[str, object]:
    """Export one indexed PDF's persisted structured data as EPUB 3."""

    options = options or ExportOptions()

    source_id = str(source_file_id or "").strip()
    if not source_id or len(source_id) > 256:
        raise IndexedDocumentNotFound("缺少要导出的文献标识。")
    database = Path(database_path)
    if not database.is_file():
        raise IndexedDocumentNotFound("当前文献索引不存在。")

    with _snapshot_connection(database) as connection:
        source = _payload_row(
            connection,
            "SELECT payload_json FROM source_files WHERE source_file_id = ?",
            (source_id,),
        )
        if source is None:
            raise IndexedDocumentNotFound("文献不存在或已从文献库移除。")
        if str(source.get("source_type") or "") != "pdf":
            raise UnsupportedDocumentExport("EPUB 导出仅支持已解析的 PDF 文献。")
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
        pages = _text_export_pages(connection, source_id, runtime_root)

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
    language = (
        source.get("language_code")
        or bibliographic.get("language_code")
        or bibliographic.get("language")
    )
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / safe_epub_filename(title)
    normalized = normalize_document_export(pages, options=options)
    write_epub(
        destination,
        pages,
        title=title,
        author=bibliographic.get("author"),
        language=language,
        options=options,
        normalized=normalized,
    )
    return {
        "ok": True,
        "source_file_id": source_id,
        "path": str(destination.resolve()),
        "size_bytes": destination.stat().st_size,
        "page_count": page_count,
        "epub_version": "3.0",
        "footnote_report": normalized.footnote_report,
        "reconstruction_report": normalized.reconstruction_report,
    }


def _text_export_pages(
    connection: sqlite3.Connection, source_id: str, runtime_root: Optional[Path]
) -> list:
    """Read private export copies and attach cached layout/span provenance."""
    pages = list(iter_indexed_pdf_pages(None, source_id, connection=connection))
    if runtime_root is None:
        return pages
    attach_export_layout(pages, Path(runtime_root))
    return pages


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
