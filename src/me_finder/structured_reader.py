"""Read-only, windowed access to structured document text.

The reader deliberately works from SQLite instead of loading the exported
index JSON.  PDF documents are rendered from ``pdf_pages`` (never from the
synthetic PDF paragraphs), while Word documents are rendered in paragraph
order.  Page wording is delegated to :mod:`me_finder.page_display` so search
results and reader views cannot drift into different page-number rules.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .database import open_database
from .page_display import PageDisplayResult, build_page_display
from .pdf_extractors import pdf_page_text_hash


DEFAULT_WINDOW_COUNT = 20
MAX_WINDOW_COUNT = 100
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_NONNEGATIVE_INTEGER_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")


class StructuredReaderError(RuntimeError):
    """Base error raised by the structured-reader data layer."""


class InvalidSourceId(StructuredReaderError, ValueError):
    """The supplied source ID is not one of the persisted ID shapes."""


class InvalidPagination(StructuredReaderError, ValueError):
    """The requested window is outside the supported pagination contract."""


class SourceNotFound(StructuredReaderError, LookupError):
    """No source metadata exists for the requested ID."""


class UnsupportedSourceType(StructuredReaderError, ValueError):
    """The source exists, but the reader has no safe rendering model for it."""


def get_document_window(
    db_path: Path,
    source_id: str,
    *,
    start: object = 0,
    count: object = DEFAULT_WINDOW_COUNT,
) -> Dict[str, object]:
    """Return one zero-based, read-only content window for a document.

    ``start`` and ``count`` may be integers or canonical decimal strings so a
    Web route can pass parsed query parameters without duplicating validation.
    ``count`` is intentionally capped to keep a single request from loading a
    long book into memory.  ``start`` is a natural position cursor
    (``pdf_page_index`` or ``paragraph_index``), not a SQL row offset.
    """

    validated_source_id = _validate_source_id(source_id)
    validated_start = _parse_pagination_value("start", start, minimum=0)
    validated_count = _parse_pagination_value(
        "count",
        count,
        minimum=1,
        maximum=MAX_WINDOW_COUNT,
    )
    database_path = Path(db_path)
    if not database_path.is_file():
        raise StructuredReaderError(f"索引数据库不存在：{database_path}")

    connection = open_database(database_path)
    try:
        source_row = connection.execute(
            """
            SELECT source_file_id, source_type, file_name, relative_path,
                   volume_number, payload_json
            FROM source_files
            WHERE source_file_id = ?
            """,
            (validated_source_id,),
        ).fetchone()
        if source_row is None:
            raise SourceNotFound(f"未找到文献：{validated_source_id}")

        source = _source_metadata(connection, source_row)
        source_type = str(source["source_type"]).lower()
        if source_type == "pdf":
            total, items, has_more, previous_start = _pdf_window(
                connection,
                validated_source_id,
                validated_start,
                validated_count,
            )
        elif source_type == "word":
            total, items, has_more, previous_start = _word_window(
                connection,
                validated_source_id,
                validated_start,
                validated_count,
            )
        else:
            raise UnsupportedSourceType(
                f"暂不支持结构化阅读的文献类型：{source_type or 'unknown'}"
            )
    finally:
        connection.close()

    return {
        "source": source,
        "start": validated_start,
        "count": validated_count,
        "total": total,
        "has_more": has_more,
        "previous_start": previous_start,
        "next_start": _next_start(items),
        "items": items,
    }


def _validate_source_id(source_id: object) -> str:
    if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.fullmatch(source_id):
        raise InvalidSourceId(
            "source_id 只能包含 ASCII 字母、数字、点、下划线和连字符，且长度不超过 128"
        )
    return source_id


def _parse_pagination_value(
    name: str,
    value: object,
    *,
    minimum: int,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool):
        raise InvalidPagination(f"{name} 必须是整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _NONNEGATIVE_INTEGER_PATTERN.fullmatch(value):
        parsed = int(value)
    else:
        raise InvalidPagination(f"{name} 必须是非负十进制整数")
    if parsed < minimum:
        raise InvalidPagination(f"{name} 不能小于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise InvalidPagination(f"{name} 不能大于 {maximum}")
    return parsed


def _source_metadata(
    connection: sqlite3.Connection,
    source_row: sqlite3.Row,
) -> Dict[str, object]:
    payload = _json_object(source_row["payload_json"])
    volume_row = connection.execute(
        """
        SELECT display_title, payload_json
        FROM volumes
        WHERE source_file_id = ?
        ORDER BY rowid
        LIMIT 1
        """,
        (source_row["source_file_id"],),
    ).fetchone()
    volume_payload = (
        _json_object(volume_row["payload_json"]) if volume_row is not None else {}
    )

    file_name = _first_nonempty(source_row["file_name"], payload.get("file_name"))
    document_title = _first_nonempty(
        payload.get("document_title"),
        payload.get("title"),
        volume_payload.get("document_title"),
        volume_payload.get("title"),
    )
    display_title = _first_nonempty(
        payload.get("display_title"),
        volume_row["display_title"] if volume_row is not None else None,
        volume_payload.get("display_title"),
        document_title,
        file_name,
    )
    return {
        "source_file_id": str(source_row["source_file_id"]),
        "source_type": str(source_row["source_type"] or "unknown"),
        "file_name": file_name,
        "display_title": display_title,
        "document_title": document_title,
        "volume_number": (
            source_row["volume_number"]
            if source_row["volume_number"] is not None
            else payload.get("volume_number")
        ),
        "file_format": _first_nonempty(
            payload.get("file_format"),
            Path(str(file_name)).suffix.lstrip(".").lower() if file_name else None,
        ),
    }


def _pdf_window(
    connection: sqlite3.Connection,
    source_id: str,
    start: int,
    count: int,
) -> Tuple[int, List[Dict[str, object]], bool, Optional[int]]:
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM pdf_pages WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()[0]
    )
    rows = connection.execute(
        """
        SELECT row_id, pdf_page_index, payload_json
        FROM pdf_pages
        WHERE source_file_id = ? AND pdf_page_index >= ?
        ORDER BY pdf_page_index, row_id
        LIMIT ?
        """,
        (source_id, start, count + 1),
    ).fetchall()
    has_more = len(rows) > count
    rows = rows[:count]
    previous_start = _previous_natural_start(
        connection,
        table="pdf_pages",
        position_column="pdf_page_index",
        source_id=source_id,
        before=int(rows[0]["pdf_page_index"]) if rows else start,
        count=count,
    )
    items: List[Dict[str, object]] = []
    for row in rows:
        payload = _json_object(row["payload_json"])
        page_index = _integer_or_default(
            row["pdf_page_index"],
            payload.get("pdf_page_index"),
            default=0,
        )
        text_raw = str(payload.get("text_raw") or "")
        display_fields = dict(payload)
        display_fields.update(
            {
                "source_type": "pdf",
                "pdf_page_index": page_index,
            }
        )
        display = build_page_display(display_fields)
        pdf_page_id = _optional_text(payload.get("pdf_page_id"))
        items.append(
            {
                "item_type": "pdf_page",
                "anchor_id": pdf_page_id,
                "pdf_page_id": pdf_page_id,
                "pdf_page_index": page_index,
                "pdf_page_number_1based": _integer_or_default(
                    payload.get("pdf_page_number_1based"),
                    page_index + 1,
                    default=page_index + 1,
                ),
                "text_raw": text_raw,
                "page_text_hash": _optional_text(payload.get("page_text_hash"))
                or pdf_page_text_hash(text_raw),
                "page_source_type": display.page_source_type,
                "page_display": display.display,
                "page_note": display.note,
                "page_verified": _display_is_verified(display),
                "is_empty": not bool(text_raw.strip()),
            }
        )
    return total, items, has_more, previous_start


def _word_window(
    connection: sqlite3.Connection,
    source_id: str,
    start: int,
    count: int,
) -> Tuple[int, List[Dict[str, object]], bool, Optional[int]]:
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM paragraphs WHERE source_file_id = ?",
            (source_id,),
        ).fetchone()[0]
    )
    rows = connection.execute(
        """
        SELECT rowid, paragraph_id, paragraph_index, text_raw, page_display,
               page_source_type, payload_json
        FROM paragraphs
        WHERE source_file_id = ? AND paragraph_index >= ?
        ORDER BY paragraph_index, rowid
        LIMIT ?
        """,
        (source_id, start, count + 1),
    ).fetchall()
    has_more = len(rows) > count
    rows = rows[:count]
    previous_start = _previous_natural_start(
        connection,
        table="paragraphs",
        position_column="paragraph_index",
        source_id=source_id,
        before=int(rows[0]["paragraph_index"]) if rows else start,
        count=count,
    )
    previous_page_key: Optional[Tuple[str, str]] = None
    if rows:
        first_position = int(rows[0]["paragraph_index"])
        previous_row = connection.execute(
            """
            SELECT paragraph_id, paragraph_index, text_raw, page_display,
                   page_source_type, payload_json
            FROM paragraphs
            WHERE source_file_id = ? AND paragraph_index < ?
            ORDER BY paragraph_index DESC, rowid DESC
            LIMIT 1
            """,
            (source_id, first_position),
        ).fetchone()
        if previous_row is not None:
            previous_payload = _merged_word_payload(previous_row)
            previous_page_key = _word_page_key(previous_payload)

    items: List[Dict[str, object]] = []
    for row in rows:
        payload = _merged_word_payload(row)
        display = build_page_display(payload)
        current_page_key = _word_page_key(payload)
        paragraph_id = str(row["paragraph_id"])
        is_docx_page_start = (
            display.page_source_type == "section_break_inferred"
            and current_page_key is not None
            and current_page_key != previous_page_key
        )
        document_page_range = (
            display.display
            if display.page_source_type == "toc_range_bound"
            else None
        )
        items.append(
            {
                "item_type": "word_paragraph",
                "anchor_id": paragraph_id if is_docx_page_start else None,
                "paragraph_id": paragraph_id,
                "paragraph_index": int(row["paragraph_index"]),
                "text_raw": str(row["text_raw"] or ""),
                "page_source_type": display.page_source_type,
                "page_display": display.display,
                "page_note": display.note,
                "page_verified": _display_is_verified(display),
                "document_page_range": document_page_range,
            }
        )
        previous_page_key = current_page_key
    return total, items, has_more, previous_start


def _previous_natural_start(
    connection: sqlite3.Connection,
    *,
    table: str,
    position_column: str,
    source_id: str,
    before: int,
    count: int,
) -> Optional[int]:
    """Return the first natural position in the preceding record window.

    ``table`` and ``position_column`` are internal constants supplied by the
    two reader implementations above; user input is never interpolated into
    this query.
    """

    rows = connection.execute(
        f"""
        SELECT {position_column}
        FROM {table}
        WHERE source_file_id = ? AND {position_column} < ?
        ORDER BY {position_column} DESC, rowid DESC
        LIMIT ?
        """,
        (source_id, before, count),
    ).fetchall()
    if not rows:
        return None
    return int(rows[-1][0])


def _next_start(items: List[Dict[str, object]]) -> Optional[int]:
    if not items:
        return None
    last = items[-1]
    position = (
        last.get("pdf_page_index")
        if last.get("item_type") == "pdf_page"
        else last.get("paragraph_index")
    )
    return int(position) + 1 if position is not None else None


def _merged_word_payload(row: sqlite3.Row) -> Dict[str, object]:
    payload = _json_object(row["payload_json"])
    payload.update(
        {
            "source_type": "word",
            "paragraph_id": row["paragraph_id"],
            "paragraph_index": row["paragraph_index"],
            "text_raw": row["text_raw"],
            "page_display": row["page_display"],
            "page_source_type": row["page_source_type"],
        }
    )
    return payload


def _word_page_key(fields: Mapping[str, object]) -> Optional[Tuple[str, str]]:
    source_type = str(fields.get("page_source_type") or "unknown")
    label = _first_nonempty(
        fields.get("original_page_start"),
        fields.get("page_display"),
    )
    if not label:
        return None
    return source_type, str(label)


def _display_is_verified(display: PageDisplayResult) -> bool:
    """Conservatively derive a boolean from the shared formatter's result.

    The page formatter remains the source of truth.  A display containing one
    of its explicit uncertainty warnings is never promoted to verified.
    """

    combined = f"{display.display}\n{display.note}"
    uncertainty_markers = ("未验证", "尚未", "需核验", "不能作为", "非段落")
    if any(marker in combined for marker in uncertainty_markers):
        return False
    return display.display.startswith("引用页码：") or (
        display.display.startswith("第 ") and display.display.endswith(" 页")
    )


def _json_object(value: object) -> Dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _integer_or_default(*values: object, default: int) -> int:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def _first_nonempty(*values: object) -> Optional[object]:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
            continue
        return value
    return None


def _optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
