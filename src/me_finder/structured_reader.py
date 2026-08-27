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
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .auto_page_mapping import normalize_page_candidate
from .citations import build_citation_formats
from .database import open_database
from .page_display import (
    CitationPageResolution,
    PageDisplayResult,
    build_page_display,
    resolve_citation_page,
)
from .pdf_extractors import pdf_page_text_hash


DEFAULT_WINDOW_COUNT = 20
MAX_WINDOW_COUNT = 100
MAX_CITATION_RANGE_ITEMS = 1000
_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ANCHOR_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_NONNEGATIVE_INTEGER_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
_CITATION_METADATA_FIELDS = (
    "document_type",
    "citation_type",
    "publication_type",
    "type",
    "title",
    "document_title",
    "display_title",
    "book_title",
    "monograph_title",
    "article_title",
    "chapter_title",
    "work_title",
    "author",
    "authors",
    "author_label",
    "creator",
    "country",
    "nationality",
    "translator",
    "translators",
    "translated_by",
    "publisher",
    "press",
    "publish_place",
    "publication_place",
    "place",
    "city",
    "publisher_place",
    "publish_year",
    "publication_year",
    "year",
    "date_label",
    "published_year",
    "journal_name",
    "journal_title",
    "journal",
    "periodical",
    "volume",
    "journal_volume",
    "issue",
    "issue_number",
    "journal_issue",
    "page_range",
    "pages",
    "article_pages",
    "collection_title",
    "container_title",
    "editor",
    "editors",
    "chief_editor",
    "file_name",
    "original_file_name",
    "volume_number",
    "volume_id",
)
_CITATION_FORMAT_FIELDS = (
    "chinese",
    "gb",
    "chinese_status",
    "gb_status",
    "chinese_missing_fields",
    "gb_missing_fields",
)


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


class InvalidCitationRange(StructuredReaderError, ValueError):
    """A requested citation range is reversed, incomplete, or too large."""


class CitationPositionNotFound(StructuredReaderError, LookupError):
    """One or both natural positions do not exist in the requested source."""


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
        citation_catalog = _citation_catalog(connection, source_row)
        source_type = str(source["source_type"]).lower()
        if source_type == "pdf":
            total, items, has_more, previous_start, last_position = _pdf_window(
                connection,
                validated_source_id,
                validated_start,
                validated_count,
                citation_catalog,
            )
        elif source_type == "word":
            total, items, has_more, previous_start, last_position = _word_window(
                connection,
                validated_source_id,
                validated_start,
                validated_count,
                citation_catalog,
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
        "last_position": last_position,
        "has_more": has_more,
        "previous_start": previous_start,
        "next_start": _next_start(items),
        "items": items,
    }


def get_document_citation(
    db_path: Path,
    source_id: str,
    *,
    start_anchor_id: object,
    end_anchor_id: object,
) -> Dict[str, object]:
    """Build a safe citation for a natural PDF-page or Word-paragraph range."""

    validated_source_id = _validate_source_id(source_id)
    validated_start_anchor = _validate_anchor_id(
        "start_anchor_id", start_anchor_id
    )
    validated_end_anchor = _validate_anchor_id("end_anchor_id", end_anchor_id)

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
        source_type = str(source["source_type"]).strip().lower()
        citation_catalog = _citation_catalog(connection, source_row)
        if source_type == "pdf":
            requested_start = _pdf_anchor_position(
                connection, validated_source_id, validated_start_anchor
            )
            requested_end = _pdf_anchor_position(
                connection, validated_source_id, validated_end_anchor
            )
            if requested_end < requested_start:
                validated_start, validated_end = requested_end, requested_start
                resolved_start_anchor = validated_end_anchor
                resolved_end_anchor = validated_start_anchor
                selection_reversed = True
            else:
                validated_start, validated_end = requested_start, requested_end
                resolved_start_anchor = validated_start_anchor
                resolved_end_anchor = validated_end_anchor
                selection_reversed = False
            if (
                validated_end - validated_start + 1
                > MAX_CITATION_RANGE_ITEMS
            ):
                raise InvalidCitationRange(
                    f"一次最多引用 {MAX_CITATION_RANGE_ITEMS} 个阅读单元"
                )
            page_records = _pdf_citation_records(
                connection,
                validated_source_id,
                validated_start,
                validated_end,
            )
        elif source_type == "word":
            requested_start = _word_anchor_position(
                connection, validated_source_id, validated_start_anchor
            )
            requested_end = _word_anchor_position(
                connection, validated_source_id, validated_end_anchor
            )
            if requested_end < requested_start:
                validated_start, validated_end = requested_end, requested_start
                resolved_start_anchor = validated_end_anchor
                resolved_end_anchor = validated_start_anchor
                selection_reversed = True
            else:
                validated_start, validated_end = requested_start, requested_end
                resolved_start_anchor = validated_start_anchor
                resolved_end_anchor = validated_end_anchor
                selection_reversed = False
            page_records = _word_citation_records(
                connection,
                validated_source_id,
                validated_start,
                validated_end,
            )
        else:
            raise UnsupportedSourceType(
                f"暂不支持结构化阅读的文献类型：{source_type or 'unknown'}"
            )

        if len(page_records) > MAX_CITATION_RANGE_ITEMS:
            raise InvalidCitationRange(
                f"一次最多引用 {MAX_CITATION_RANGE_ITEMS} 个阅读单元"
            )
        if (
            not page_records
            or page_records[0][0] != validated_start
            or page_records[-1][0] != validated_end
        ):
            raise CitationPositionNotFound("所选引文的起止位置不存在。")

        payloads = [record[1] for record in page_records]
        _validate_citation_range_continuity(
            page_records,
            payloads,
            source_type=source_type,
            start_index=validated_start,
            end_index=validated_end,
        )
        if source_type == "word":
            # A multi-paragraph range must have an authoritative work ID on
            # every record.  Old/migrated indexes may have omitted work_id
            # from payload_json, so _merged_word_payload overlays the
            # paragraphs table columns before this check.
            resolved_work_ids = [
                _optional_text(payload.get("work_id"))
                for payload in payloads
            ]
            if len(payloads) > 1 and any(
                work_id is None for work_id in resolved_work_ids
            ):
                raise InvalidCitationRange(
                    "所选范围缺少可靠的文献条目归属，不能生成跨段引文。"
                )
            work_ids = {
                work_id for work_id in resolved_work_ids if work_id is not None
            }
            if len(work_ids) > 1:
                raise InvalidCitationRange("不能跨越不同文献条目生成同一条引文。")
        page_state = _citation_range_state(payloads, source_type)
        citation_metadata = _citation_metadata_for_item(
            citation_catalog,
            payloads[0],
            source_type=source_type,
        )
        citation_formats = _safe_citation_formats(
            citation_metadata,
            page_state["hit_page"],
        )
    finally:
        connection.close()

    return {
        "source": source,
        "source_id": validated_source_id,
        "source_type": source_type,
        "start_anchor_id": resolved_start_anchor,
        "end_anchor_id": resolved_end_anchor,
        "start_index": validated_start,
        "end_index": validated_end,
        "selection_reversed": selection_reversed,
        "selected_item_count": len(page_records),
        "page_range": {
            "verified": page_state["verified"],
            "citation_page_start": page_state["citation_page_start"],
            "citation_page_end": page_state["citation_page_end"],
            "status": (
                "verified" if page_state["verified"] else "page_unverified"
            ),
            "note": page_state["note"],
        },
        "citation_formats": citation_formats,
    }


def _citation_catalog(
    connection: sqlite3.Connection,
    source_row: sqlite3.Row,
) -> Dict[str, object]:
    source_payload = _json_object(source_row["payload_json"])
    source_payload.setdefault("source_file_id", source_row["source_file_id"])
    source_payload.setdefault("source_type", source_row["source_type"])
    source_payload.setdefault("file_name", source_row["file_name"])
    source_payload.setdefault("volume_number", source_row["volume_number"])

    volume_rows = connection.execute(
        """
        SELECT volume_id, payload_json
        FROM volumes
        WHERE source_file_id = ?
        ORDER BY rowid
        """,
        (source_row["source_file_id"],),
    ).fetchall()
    volumes = {
        str(row["volume_id"]): _json_object(row["payload_json"])
        for row in volume_rows
        if row["volume_id"] not in (None, "")
    }
    work_rows = connection.execute(
        """
        SELECT works.work_id, works.payload_json
        FROM works
        INNER JOIN volumes ON volumes.volume_id = works.volume_id
        WHERE volumes.source_file_id = ?
        ORDER BY works.rowid
        """,
        (source_row["source_file_id"],),
    ).fetchall()
    works = {
        str(row["work_id"]): _json_object(row["payload_json"])
        for row in work_rows
        if row["work_id"] not in (None, "")
    }
    return {
        "source": source_payload,
        "volumes": volumes,
        "works": works,
    }


def _citation_metadata_for_item(
    catalog: Mapping[str, object],
    item: Mapping[str, object],
    *,
    source_type: str,
) -> Dict[str, object]:
    source = catalog.get("source")
    source_payload = dict(source) if isinstance(source, Mapping) else {}
    volumes = catalog.get("volumes")
    volume_map = dict(volumes) if isinstance(volumes, Mapping) else {}
    works = catalog.get("works")
    work_map = dict(works) if isinstance(works, Mapping) else {}

    volume_id = _optional_text(item.get("volume_id"))
    volume_payload = (
        volume_map.get(volume_id)
        if volume_id is not None
        else next(iter(volume_map.values()), {})
    )
    work_id = _optional_text(item.get("work_id"))
    work_payload = work_map.get(work_id, {}) if work_id is not None else {}
    metadata: Dict[str, object] = {}
    for record in (source_payload, volume_payload, work_payload, item):
        if not isinstance(record, Mapping):
            continue
        for field in _CITATION_METADATA_FIELDS:
            clean = _citation_metadata_value(record.get(field))
            if clean not in (None, "", []):
                metadata[field] = clean

    bibliographic = source_payload.get("bibliographic_metadata")
    if isinstance(bibliographic, Mapping):
        for field in _CITATION_METADATA_FIELDS:
            clean = _citation_metadata_value(bibliographic.get(field))
            if clean not in (None, "", []):
                metadata[field] = clean

    metadata.setdefault("author", item.get("author_label"))
    metadata.setdefault(
        "title",
        item.get("work_title")
        or item.get("document_title")
        or source_payload.get("document_title")
        or source_payload.get("display_title"),
    )
    metadata.setdefault(
        "document_title",
        item.get("document_title")
        or source_payload.get("document_title")
        or source_payload.get("display_title"),
    )
    if source_type == "word" and _is_marx_engels_metadata(metadata):
        metadata.setdefault("document_type", "marx_engels_collection")
        metadata.setdefault(
            "collection_title",
            _marx_engels_collection_title(metadata),
        )
        if metadata.get("collection_title") == "马克思恩格斯文集":
            metadata.setdefault("publication_place", "北京")
            metadata.setdefault("publisher", "人民出版社")
            metadata.setdefault("publication_year", "2009")
    else:
        metadata.setdefault("document_type", "book")
    return {
        key: value
        for key, value in metadata.items()
        if key in _CITATION_METADATA_FIELDS
        and value not in (None, "", [])
    }


def _citation_metadata_value(value: object) -> object:
    if value is None or isinstance(value, (dict, set)):
        return None
    if isinstance(value, (list, tuple)):
        cleaned = [
            str(item).strip()[:500]
            for item in value[:50]
            if item is not None and str(item).strip()
        ]
        return cleaned
    if isinstance(value, (str, int, float)):
        return str(value).strip()[:500]
    return None


def _is_marx_engels_metadata(metadata: Mapping[str, object]) -> bool:
    volume_id = str(metadata.get("volume_id") or "").upper()
    if metadata.get("volume_number") is not None and volume_id.startswith("MEWJ-"):
        return True
    text = "".join(
        str(metadata.get(key) or "")
        for key in (
            "collection_title",
            "document_title",
            "display_title",
            "title",
            "file_name",
            "original_file_name",
        )
    )
    return any(
        marker in text
        for marker in ("马克思恩格斯文集", "马克思恩格斯全集", "马克思恩格斯选集")
    )


def _marx_engels_collection_title(metadata: Mapping[str, object]) -> str:
    text = "".join(
        str(metadata.get(key) or "")
        for key in (
            "collection_title",
            "document_title",
            "display_title",
            "title",
            "file_name",
            "original_file_name",
        )
    )
    if "全集" in text:
        return "马克思恩格斯全集"
    if "选集" in text:
        return "马克思恩格斯选集"
    return "马克思恩格斯文集"


def _citation_hit_page(
    fields: Mapping[str, object],
    display: PageDisplayResult,
) -> Dict[str, object]:
    resolved = resolve_citation_page(fields)
    if resolved.verified and resolved.start:
        return {
            "start": resolved.start,
            "end": resolved.end,
        }
    return {
        "display": display.display or "页码未验证",
        "uncalibrated": True,
    }


def _safe_citation_formats(
    metadata: Mapping[str, object],
    hit_page: Mapping[str, object],
) -> Dict[str, object]:
    formats = build_citation_formats(metadata, hit_page)
    result: Dict[str, object] = {}
    for field in _CITATION_FORMAT_FIELDS:
        value = formats.get(field)
        if field.endswith("_missing_fields"):
            missing_fields = value if isinstance(value, list) else []
            result[field] = [
                str(item)[:64]
                for item in missing_fields
                if isinstance(item, str)
            ]
        else:
            result[field] = str(value or "")[:4000]
    page_verified = not bool(hit_page.get("uncalibrated")) and bool(
        hit_page.get("start") or hit_page.get("page")
    )
    result["page_verified"] = page_verified
    result["can_copy"] = page_verified and any(
        result.get(field) == "complete"
        for field in ("chinese_status", "gb_status")
    )
    return result


def _citation_range_state(
    payloads: Sequence[Mapping[str, object]],
    source_type: str,
) -> Dict[str, object]:
    states: List[Tuple[CitationPageResolution, PageDisplayResult]] = []
    for payload in payloads:
        display_fields = dict(payload)
        display_fields["source_type"] = source_type
        display = build_page_display(display_fields)
        states.append((resolve_citation_page(display_fields), display))

    fully_verified = bool(states) and all(
        resolution.verified and resolution.start
        for resolution, _display in states
    )
    if fully_verified:
        citation_start = str(states[0][0].start)
        citation_end = str(states[-1][0].end or states[-1][0].start)
        hit_page = {
            "start": citation_start,
            "end": citation_end,
        }
        notes = {
            state[1].note
            for state in states
            if state[1].note
        }
        note = "；".join(sorted(notes))
    else:
        citation_start = None
        citation_end = None
        hit_page = {
            "display": "页码未验证",
            "uncalibrated": True,
        }
        note = "所选范围包含尚未验证的页码，不能生成带页码引文。"
    return {
        "verified": fully_verified,
        "citation_page_start": citation_start,
        "citation_page_end": citation_end,
        "hit_page": hit_page,
        "note": note,
    }


def _pdf_anchor_position(
    connection: sqlite3.Connection,
    source_id: str,
    anchor_id: str,
) -> int:
    # Persisted anchor IDs remain authoritative.  The numeric suffix is only
    # a query hint for long documents; the payload is still compared exactly
    # and future non-standard IDs fall back to the full safe scan.
    suffix_match = re.search(r"-PAGE-([0-9]{1,10})\Z", anchor_id)
    if suffix_match is not None:
        hinted_index = int(suffix_match.group(1))
        hinted_rows = connection.execute(
            """
            SELECT pdf_page_index, payload_json
            FROM pdf_pages
            WHERE source_file_id = ? AND pdf_page_index = ?
            ORDER BY rowid
            """,
            (source_id, hinted_index),
        ).fetchall()
        hinted_matches = [
            int(row["pdf_page_index"])
            for row in hinted_rows
            if _json_object(row["payload_json"]).get("pdf_page_id") == anchor_id
        ]
        if len(hinted_matches) == 1:
            return hinted_matches[0]
        if len(hinted_matches) > 1:
            raise CitationPositionNotFound("所选 PDF 页锚点不存在或不唯一。")

    matches: List[int] = []
    rows = connection.execute(
        """
        SELECT pdf_page_index, payload_json
        FROM pdf_pages
        WHERE source_file_id = ?
        ORDER BY pdf_page_index, rowid
        """,
        (source_id,),
    ).fetchall()
    for row in rows:
        payload = _json_object(row["payload_json"])
        if payload.get("pdf_page_id") == anchor_id:
            matches.append(int(row["pdf_page_index"]))
    if len(matches) != 1:
        raise CitationPositionNotFound("所选 PDF 页锚点不存在或不唯一。")
    return matches[0]


def _word_anchor_position(
    connection: sqlite3.Connection,
    source_id: str,
    anchor_id: str,
) -> int:
    rows = connection.execute(
        """
        SELECT paragraph_index
        FROM paragraphs
        WHERE source_file_id = ? AND paragraph_id = ?
        ORDER BY rowid
        """,
        (source_id, anchor_id),
    ).fetchall()
    if len(rows) != 1:
        raise CitationPositionNotFound("所选 Word 段落锚点不存在或不唯一。")
    return int(rows[0]["paragraph_index"])


def _validate_citation_range_continuity(
    page_records: Sequence[Tuple[int, Mapping[str, object]]],
    payloads: Sequence[Mapping[str, object]],
    *,
    source_type: str,
    start_index: int,
    end_index: int,
) -> None:
    if source_type == "pdf":
        actual_positions = [position for position, _payload in page_records]
        expected_count = end_index - start_index + 1
        if (
            len(actual_positions) != expected_count
            or any(
                position != start_index + offset
                for offset, position in enumerate(actual_positions)
            )
        ):
            raise InvalidCitationRange("所选 PDF 范围存在缺页，不能生成连续页码引文。")

    resolutions = [resolve_citation_page(payload) for payload in payloads]
    mapping_sources = {resolution.page_source_type for resolution in resolutions}
    if len(mapping_sources) > 1:
        raise InvalidCitationRange("所选范围的页码映射来源不一致。")

    if len(resolutions) > 1 and all(
        resolution.verified for resolution in resolutions
    ):
        if source_type == "pdf":
            segment_ids = [
                _optional_text(payload.get("segment_id"))
                for payload in payloads
            ]
            if any(segment_id is None for segment_id in segment_ids):
                raise InvalidCitationRange(
                    "旧索引缺少页码分段标识，重新导入后才能生成跨页引文。"
                )
            if len(set(segment_ids)) > 1:
                raise InvalidCitationRange("所选范围跨越不同页码映射分段。")
        normalized_pages: List[Tuple[str, int]] = []
        for resolution in resolutions:
            candidate = normalize_page_candidate(resolution.start)
            if candidate is None:
                raise InvalidCitationRange("所选范围的引用页码序列无法安全验证。")
            style, number, _canonical = candidate
            family = "roman" if style.startswith("roman") else "arabic"
            normalized_pages.append((family, number))
        if len({family for family, _number in normalized_pages}) > 1:
            raise InvalidCitationRange("所选范围跨越不同引用页码体系。")
        previous = normalized_pages[0][1]
        for _family, number in normalized_pages[1:]:
            if number not in {previous, previous + 1}:
                raise InvalidCitationRange("所选范围的引用页码映射不连续。")
            previous = number


def _pdf_citation_records(
    connection: sqlite3.Connection,
    source_id: str,
    start_index: int,
    end_index: int,
) -> List[Tuple[int, Dict[str, object]]]:
    rows = connection.execute(
        """
        SELECT pdf_page_index, payload_json
        FROM pdf_pages
        WHERE source_file_id = ?
          AND pdf_page_index >= ?
          AND pdf_page_index <= ?
        ORDER BY pdf_page_index, rowid
        LIMIT ?
        """,
        (
            source_id,
            start_index,
            end_index,
            MAX_CITATION_RANGE_ITEMS + 1,
        ),
    ).fetchall()
    records: List[Tuple[int, Dict[str, object]]] = []
    for row in rows:
        position = int(row["pdf_page_index"])
        payload = _json_object(row["payload_json"])
        payload.update(
            {
                "source_type": "pdf",
                "pdf_page_index": position,
            }
        )
        records.append((position, payload))
    return records


def _word_citation_records(
    connection: sqlite3.Connection,
    source_id: str,
    start_index: int,
    end_index: int,
) -> List[Tuple[int, Dict[str, object]]]:
    rows = connection.execute(
        """
        SELECT paragraph_id, volume_id, work_id, paragraph_index, text_raw,
               page_display, page_source_type, payload_json
        FROM paragraphs
        WHERE source_file_id = ?
          AND paragraph_index >= ?
          AND paragraph_index <= ?
        ORDER BY paragraph_index, rowid
        LIMIT ?
        """,
        (
            source_id,
            start_index,
            end_index,
            MAX_CITATION_RANGE_ITEMS + 1,
        ),
    ).fetchall()
    return [
        (int(row["paragraph_index"]), _merged_word_payload(row))
        for row in rows
    ]


def _validate_source_id(source_id: object) -> str:
    if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.fullmatch(source_id):
        raise InvalidSourceId(
            "source_id 只能包含 ASCII 字母、数字、点、下划线和连字符，且长度不超过 128"
        )
    return source_id


def _validate_anchor_id(name: str, value: object) -> str:
    if not isinstance(value, str) or not _ANCHOR_ID_PATTERN.fullmatch(value):
        raise InvalidCitationRange(
            f"{name} 必须是长度不超过 256 的持久化文献锚点"
        )
    return value


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
    profile = payload.get("pdf_profile") or {}
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
        "original_file_name": _first_nonempty(
            payload.get("original_file_name"),
            file_name,
        ),
        "display_title": display_title,
        "document_title": document_title,
        "parser_label": (
            "原生文本" if profile.get("detected_pdf_type") == "native_text"
            else _first_nonempty(profile.get("parser_label"), profile.get("provider_name"))
        ),
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
    citation_catalog: Mapping[str, object],
) -> Tuple[int, List[Dict[str, object]], bool, Optional[int], Optional[int]]:
    summary = connection.execute(
        """
        SELECT COUNT(*) AS total, MAX(pdf_page_index) AS last_position
        FROM pdf_pages
        WHERE source_file_id = ?
        """,
        (source_id,),
    ).fetchone()
    total = int(summary["total"])
    last_position = (
        int(summary["last_position"])
        if summary["last_position"] is not None
        else None
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
        page_resolution = resolve_citation_page(display_fields)
        page_verified = page_resolution.verified
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
                "pdf_page_label": _optional_text(payload.get("pdf_page_label")),
                "text_raw": text_raw,
                "page_text_hash": _optional_text(payload.get("page_text_hash"))
                or pdf_page_text_hash(text_raw),
                "page_source_type": display.page_source_type,
                "page_display": display.display,
                "page_note": display.note,
                "page_verified": page_verified,
                "citation_page_start": page_resolution.start,
                "citation_page_end": page_resolution.end,
                "page_mapping_method": _optional_text(
                    payload.get("page_mapping_method")
                )
                or display.page_source_type,
                "page_mapping_confidence": payload.get(
                    "page_mapping_confidence"
                ),
                "mapping_confidence_level": _optional_text(
                    payload.get("mapping_confidence_level")
                ),
                "is_empty": not bool(text_raw.strip()),
                "citation_formats": _safe_citation_formats(
                    _citation_metadata_for_item(
                        citation_catalog,
                        payload,
                        source_type="pdf",
                    ),
                    _citation_hit_page(
                        display_fields,
                        display,
                    ),
                ),
            }
        )
    return total, items, has_more, previous_start, last_position


def _word_window(
    connection: sqlite3.Connection,
    source_id: str,
    start: int,
    count: int,
    citation_catalog: Mapping[str, object],
) -> Tuple[int, List[Dict[str, object]], bool, Optional[int], Optional[int]]:
    summary = connection.execute(
        """
        SELECT COUNT(*) AS total, MAX(paragraph_index) AS last_position
        FROM paragraphs
        WHERE source_file_id = ?
        """,
        (source_id,),
    ).fetchone()
    total = int(summary["total"])
    last_position = (
        int(summary["last_position"])
        if summary["last_position"] is not None
        else None
    )
    rows = connection.execute(
        """
        SELECT rowid, paragraph_id, volume_id, work_id, paragraph_index,
               text_raw, page_display, page_source_type, payload_json
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
            SELECT paragraph_id, volume_id, work_id, paragraph_index, text_raw,
                   page_display, page_source_type, payload_json
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
        text_raw = str(row["text_raw"] or "")
        display = build_page_display(payload)
        page_resolution = resolve_citation_page(payload)
        page_verified = page_resolution.verified
        current_page_key = _word_page_key(payload)
        paragraph_id = str(row["paragraph_id"])
        is_text_page_start = (
            display.page_source_type
            in {
                "section_break_inferred",
                "epub_page_list",
                "epub_pagebreak",
            }
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
                "anchor_id": paragraph_id if is_text_page_start else None,
                "paragraph_id": paragraph_id,
                "paragraph_index": int(row["paragraph_index"]),
                "text_raw": text_raw,
                # Word deep links need the same hash → quote → page recovery
                # ladder as PDF links.  The hash is format-agnostic despite
                # the historical helper name.
                "page_text_hash": pdf_page_text_hash(text_raw),
                "page_source_type": display.page_source_type,
                "page_display": display.display,
                "page_note": display.note,
                "page_verified": page_verified,
                "citation_page_start": page_resolution.start,
                "citation_page_end": page_resolution.end,
                "page_mapping_method": display.page_source_type,
                "page_mapping_confidence": payload.get(
                    "page_mapping_confidence"
                ),
                "mapping_confidence_level": _optional_text(
                    payload.get("mapping_confidence_level")
                ),
                "document_page_range": document_page_range,
                "citation_formats": _safe_citation_formats(
                    _citation_metadata_for_item(
                        citation_catalog,
                        payload,
                        source_type="word",
                    ),
                    _citation_hit_page(
                        payload,
                        display,
                    ),
                ),
            }
        )
        previous_page_key = current_page_key
    return total, items, has_more, previous_start, last_position


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
            "volume_id": row["volume_id"],
            "work_id": row["work_id"],
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
