"""Provider-neutral local statistics for indexed PDF parsing results."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional


MINERU_PROVIDER_ID = "mineru-cloud"


def build_parser_statistics(
    database_path: Path,
    *,
    mineru_statistics: Optional[object] = None,
) -> Dict[str, object]:
    """Summarize the current PDF index, grouped by the parser that produced it.

    The index is authoritative for the global/provider totals.  MinerU's job
    ledger is used only to add per-account attribution to indexed MinerU books.
    No remote quota or billing endpoint is consulted.
    """

    books = _indexed_pdf_books(Path(database_path))
    providers: Dict[str, Dict[str, object]] = {}
    for book in books:
        provider_id = str(book.pop("provider_id"))
        provider_name = str(book.pop("provider_name"))
        provider_kind = str(book.pop("provider_kind"))
        provider = providers.setdefault(
            provider_id,
            {
                "provider_id": provider_id,
                "provider_name": provider_name,
                "provider_kind": provider_kind,
                "parsed_book_count": 0,
                "parsed_page_count": 0,
                "books": [],
            },
        )
        provider["parsed_book_count"] = int(provider["parsed_book_count"]) + 1
        provider["parsed_page_count"] = int(provider["parsed_page_count"]) + int(
            book["parsed_page_count"]
        )
        provider_books = provider["books"]
        if isinstance(provider_books, list):
            provider_books.append(book)

    mineru = providers.get(MINERU_PROVIDER_ID)
    if mineru is not None:
        indexed_source_ids = {
            str(item.get("source_file_id") or "")
            for item in mineru.get("books", [])
            if isinstance(item, Mapping)
        }
        mineru["credentials"] = _indexed_mineru_credentials(
            mineru_statistics,
            indexed_source_ids=indexed_source_ids,
        )

    ordered = sorted(
        providers.values(),
        key=lambda item: (
            0 if item["provider_id"] == MINERU_PROVIDER_ID else 1,
            -int(item["parsed_page_count"]),
            str(item["provider_name"]).casefold(),
        ),
    )
    return {
        "scope": "current_index",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": {
            "parsed_book_count": len(books),
            "parsed_page_count": sum(
                int(item["parsed_page_count"]) for item in books
            ),
            "provider_count": len(ordered),
        },
        "providers": ordered,
    }


def _indexed_pdf_books(database_path: Path) -> list[Dict[str, object]]:
    if not database_path.is_file():
        return []
    try:
        connection = sqlite3.connect(str(database_path))
    except sqlite3.Error:
        return []
    try:
        source_rows = connection.execute(
            "SELECT source_file_id, file_name, payload_json FROM source_files "
            "WHERE source_type = 'pdf' ORDER BY rowid"
        ).fetchall()
        page_counts = {
            str(source_id): int(count)
            for source_id, count in connection.execute(
                "SELECT source_file_id, COUNT(*) FROM pdf_pages "
                "GROUP BY source_file_id"
            )
            if source_id and int(count) > 0
        }
        latest_runs: Dict[str, Mapping[str, object]] = {}
        for source_id, payload_json in connection.execute(
            "SELECT source_file_id, payload_json FROM pdf_import_runs "
            "ORDER BY row_id"
        ):
            payload = _json_object(payload_json)
            if source_id and payload:
                latest_runs[str(source_id)] = payload
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    books: list[Dict[str, object]] = []
    for source_id, file_name, payload_json in source_rows:
        normalized_source_id = str(source_id or "")
        page_count = page_counts.get(normalized_source_id, 0)
        if not normalized_source_id or page_count < 1:
            continue
        source = _json_object(payload_json)
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
        parser = str(profile.get("parser") or "").strip()
        provider_id, provider_name, provider_kind = _provider_identity(
            profile,
            parser=parser,
        )
        run = latest_runs.get(normalized_source_id, {})
        title = str(
            bibliographic.get("title")
            or source.get("display_title")
            or Path(str(source.get("file_name") or file_name or normalized_source_id)).stem
        )
        books.append(
            {
                "provider_id": provider_id,
                "provider_name": provider_name,
                "provider_kind": provider_kind,
                "source_file_id": normalized_source_id,
                "document_id": str(
                    source.get("document_id") or normalized_source_id
                ),
                "title": title,
                "file_name": str(source.get("file_name") or file_name or ""),
                "parsed_page_count": page_count,
                "parser": parser or str(run.get("parser") or "unknown"),
                "model": str(profile.get("model") or "") or None,
                "completed_at": str(
                    run.get("finished_at") or run.get("started_at") or ""
                )
                or None,
            }
        )
    return books


def _provider_identity(
    profile: Mapping[str, object],
    *,
    parser: str,
) -> tuple[str, str, str]:
    explicit_id = str(profile.get("provider_id") or "").strip()
    explicit_name = str(
        profile.get("provider_name") or profile.get("parser_label") or ""
    ).strip()
    if explicit_id:
        return (
            explicit_id,
            explicit_name or explicit_id,
            "local" if explicit_id == "mineru-local" else "api",
        )
    if parser in {"mineru", "precision"}:
        return MINERU_PROVIDER_ID, explicit_name or "MinerU", "api"
    if parser == "openai_compatible":
        return "openai-compatible", explicit_name or "其他解析 API", "api"
    if parser == "pymupdf":
        return "pymupdf", "本地 PDF 文本提取", "local"
    if parser == "simple_pdf_text":
        return "simple-pdf-text", "本地 PDF 文本提取", "local"
    normalized = parser or "unknown-parser"
    return normalized, explicit_name or _human_parser_name(normalized), "local"


def _human_parser_name(parser: str) -> str:
    return {
        "qwen-ocr": "Qwen OCR",
        "unknown-parser": "未标记解析器",
    }.get(parser, parser.replace("_", " "))


def _indexed_mineru_credentials(
    statistics: Optional[object],
    *,
    indexed_source_ids: set[str],
) -> list[Dict[str, object]]:
    if statistics is None:
        return []
    payload = statistics.to_dict() if hasattr(statistics, "to_dict") else statistics
    if not isinstance(payload, Mapping):
        return []
    credentials = payload.get("credentials")
    if not isinstance(credentials, (list, tuple)):
        return []
    output: list[Dict[str, object]] = []
    for credential in credentials:
        if not isinstance(credential, Mapping):
            continue
        books = [
            dict(book)
            for book in credential.get("books", [])
            if isinstance(book, Mapping)
            and str(book.get("source_file_id") or "") in indexed_source_ids
        ]
        if not books:
            continue
        output.append(
            {
                "account_id": str(credential.get("account_id") or ""),
                "display_name": str(
                    credential.get("display_name")
                    or credential.get("account_id")
                    or "未命名账号"
                ),
                "parsed_book_count": len(
                    {
                        str(book.get("source_file_id") or "")
                        for book in books
                    }
                ),
                "parsed_page_count": sum(
                    int(book.get("parsed_page_count") or 0) for book in books
                ),
                "books": books,
            }
        )
    return sorted(
        output,
        key=lambda item: (
            -int(item["parsed_page_count"]),
            str(item["display_name"]).casefold(),
        ),
    )


def _json_object(value: object) -> Dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
