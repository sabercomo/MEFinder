"""Versioned, parser-neutral exports for one MEFinder document.

The public format deliberately contains normalized MEFinder records instead of
the raw response returned by MinerU (or any other parser).  Small exports use a
single JSON object.  Large exports use a Zip64 container whose page stream is
written one NDJSON record at a time.
"""

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence


DOCUMENT_SCHEMA_VERSION = "mefinder.document.v1"
ZIP_MANIFEST_NAME = "manifest.json"
ZIP_PAGES_NAME = "pages.ndjson"


class DocumentExportError(ValueError):
    """Raised when an export cannot be represented by the v1 contract."""


@dataclass(frozen=True)
class ExportedDocument:
    """Materialized representation returned for ordinary-size imports."""

    manifest: Dict[str, object]
    pages: Sequence[Dict[str, object]]


def document_manifest(
    *,
    document: Mapping[str, object],
    source_sha256: str,
    source_file: Mapping[str, object],
    parser_provider: str,
    parser_model: Optional[str] = None,
    parser_options: Optional[Mapping[str, object]] = None,
    parser_provenance: Optional[Mapping[str, object]] = None,
    bibliographic_metadata: Optional[Mapping[str, object]] = None,
    external_ids: Optional[Mapping[str, object]] = None,
    parsed_at: Optional[str] = None,
    parser_version: Optional[str] = None,
    warnings: Sequence[object] = (),
    missing_ranges: Sequence[object] = (),
    page_count: Optional[int] = None,
) -> Dict[str, object]:
    """Build the stable non-page portion of ``mefinder.document.v1``.

    Unknown data remains absent/``None``; this function never fabricates
    bibliographic, logical-page, layout, or model values.
    """

    digest = str(source_sha256 or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise DocumentExportError("source_sha256 must be a 64-character hex digest")
    provider = str(parser_provider or "").strip()
    if not provider:
        raise DocumentExportError("parser_provider is required")
    result: Dict[str, object] = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "document": dict(document),
        "source_sha256": digest,
        "source_file": dict(source_file),
        "bibliographic_metadata": dict(bibliographic_metadata or {}),
        "external_ids": dict(external_ids or {}),
        "parser": {
            "provider": provider,
            "model": parser_model,
            "version": parser_version,
            "options": dict(parser_options or {}),
            "provenance": dict(parser_provenance or {}),
        },
        "parsed_at": parsed_at,
        "page_count": int(page_count) if page_count is not None else None,
        "warnings": list(warnings),
        "missing_ranges": list(missing_ranges),
    }
    return result


def normalize_export_page(page: Mapping[str, object]) -> Dict[str, object]:
    """Adapt current MEFinder page fields to the public page-level contract."""

    physical = page.get("physical_pdf_page")
    if physical in (None, ""):
        physical = page.get("pdf_page_number_1based")
    if physical in (None, "") and page.get("pdf_page_index") not in (None, ""):
        physical = int(page["pdf_page_index"]) + 1
    try:
        physical_page = int(physical)
    except (TypeError, ValueError) as exc:
        raise DocumentExportError("each page requires a physical PDF page number") from exc
    if physical_page < 1:
        raise DocumentExportError("physical PDF page numbers are 1-based")

    logical = page.get("logical_page")
    if logical in (None, ""):
        logical = page.get("book_page")
    if logical in (None, ""):
        logical = page.get("printed_page")
    text = page.get("text")
    if text is None:
        text = page.get("text_raw")
    blocks = page.get("blocks")
    warnings = page.get("warnings")
    provenance = page.get("parser_provenance")
    if not isinstance(provenance, Mapping):
        provenance = {
            key: value
            for key, value in (
                ("provider", page.get("parser")),
                ("provider_label", page.get("parser_label")),
                ("version", page.get("parser_version")),
            )
            if value not in (None, "")
        }
    return {
        "physical_pdf_page": physical_page,
        "logical_page": logical if logical != "" else None,
        "pdf_page_label": page.get("pdf_page_label"),
        "text": str(text or ""),
        "blocks": list(blocks) if isinstance(blocks, (list, tuple)) else None,
        "bbox": page.get("bbox"),
        "reading_order": page.get("reading_order"),
        "parser_provenance": dict(provenance),
        "warnings": list(warnings) if isinstance(warnings, (list, tuple)) else [],
    }


def _partial_path(path: Path) -> Path:
    return path.with_name(path.name + ".partial")


def _write_encoded(handle, value: object, *, encoder: json.JSONEncoder) -> None:
    for chunk in encoder.iterencode(value):
        handle.write(chunk)


def export_document_json(
    output_path: Path,
    manifest: Mapping[str, object],
    pages: Iterable[Mapping[str, object]],
) -> Path:
    """Atomically stream one ordinary JSON export without building a JSON blob."""

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_path(target)
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    page_count = 0
    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("{")
        first = True
        for key in sorted(k for k in manifest if k != "pages"):
            if not first:
                handle.write(",")
            _write_encoded(handle, str(key), encoder=encoder)
            handle.write(":")
            _write_encoded(handle, manifest[key], encoder=encoder)
            first = False
        if not first:
            handle.write(",")
        handle.write('"pages":[')
        previous_page = 0
        for raw_page in pages:
            page = normalize_export_page(raw_page)
            physical_page = int(page["physical_pdf_page"])
            if physical_page <= previous_page:
                raise DocumentExportError(
                    "export pages must be strictly ordered by physical_pdf_page"
                )
            if page_count:
                handle.write(",")
            _write_encoded(handle, page, encoder=encoder)
            previous_page = physical_page
            page_count += 1
        handle.write("]}\n")
        handle.flush()
        os.fsync(handle.fileno())
    expected_count = manifest.get("page_count")
    if expected_count is not None and int(expected_count) != page_count:
        raise DocumentExportError(
            f"manifest page_count {expected_count} does not match {page_count} pages"
        )
    partial.replace(target)
    return target


def export_document_zip(
    output_path: Path,
    manifest: Mapping[str, object],
    pages: Iterable[Mapping[str, object]],
) -> Path:
    """Atomically write a Zip64 ``manifest.json`` + incremental NDJSON export."""

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_path(target)
    page_count = 0
    previous_page = 0
    with zipfile.ZipFile(
        partial,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        with archive.open(ZIP_PAGES_NAME, "w", force_zip64=True) as page_stream:
            for raw_page in pages:
                page = normalize_export_page(raw_page)
                physical_page = int(page["physical_pdf_page"])
                if physical_page <= previous_page:
                    raise DocumentExportError(
                        "export pages must be strictly ordered by physical_pdf_page"
                    )
                encoded = json.dumps(
                    page,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                page_stream.write(encoded)
                page_stream.write(b"\n")
                previous_page = physical_page
                page_count += 1
        expected_count = manifest.get("page_count")
        if expected_count is not None and int(expected_count) != page_count:
            raise DocumentExportError(
                f"manifest page_count {expected_count} does not match {page_count} pages"
            )
        zip_manifest = dict(manifest)
        zip_manifest["page_count"] = page_count
        zip_manifest["page_stream"] = {
            "format": "ndjson",
            "path": ZIP_PAGES_NAME,
        }
        archive.writestr(
            ZIP_MANIFEST_NAME,
            json.dumps(
                zip_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    with partial.open("rb+") as handle:
        os.fsync(handle.fileno())
    partial.replace(target)
    return target


def read_document_export(path: Path) -> ExportedDocument:
    """Read a complete ordinary or packaged export for compatibility checks."""

    source = Path(path)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            manifest = json.loads(archive.read(ZIP_MANIFEST_NAME).decode("utf-8"))
            pages = list(_iter_zip_pages(archive))
    else:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise DocumentExportError("document export must contain a JSON object")
        pages = payload.pop("pages", [])
        manifest = payload
    _validate_read_export(manifest, pages)
    return ExportedDocument(manifest=manifest, pages=pages)


def iter_document_pages(path: Path) -> Iterator[Dict[str, object]]:
    """Iterate packaged pages without materializing the full book."""

    source = Path(path)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            yield from _iter_zip_pages(archive)
        return
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    for page in payload.get("pages", []):
        if isinstance(page, dict):
            yield page


def _iter_zip_pages(archive: zipfile.ZipFile) -> Iterator[Dict[str, object]]:
    with archive.open(ZIP_PAGES_NAME, "r") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if not raw_line.strip():
                continue
            try:
                page = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DocumentExportError(
                    f"invalid pages.ndjson record at line {line_number}"
                ) from exc
            if not isinstance(page, dict):
                raise DocumentExportError(
                    f"pages.ndjson line {line_number} is not an object"
                )
            yield page


def _validate_read_export(
    manifest: Mapping[str, object], pages: Sequence[Mapping[str, object]]
) -> None:
    if manifest.get("schema_version") != DOCUMENT_SCHEMA_VERSION:
        raise DocumentExportError("unsupported document export schema")
    previous = 0
    for page in pages:
        current = int(page.get("physical_pdf_page") or 0)
        if current <= previous:
            raise DocumentExportError("export page order is invalid")
        previous = current
    expected = manifest.get("page_count")
    if expected is not None and int(expected) != len(pages):
        raise DocumentExportError("export page_count does not match page data")


def stable_export_fields(exported: ExportedDocument) -> Dict[str, object]:
    """Return deterministic protocol fields suitable for equality comparison."""

    manifest = dict(exported.manifest)
    # This describes only the container representation.  JSON and Zip/NDJSON
    # exports of the same document intentionally compare equal without it.
    manifest.pop("page_stream", None)
    return {
        "manifest": manifest,
        "pages": [dict(page) for page in exported.pages],
    }
