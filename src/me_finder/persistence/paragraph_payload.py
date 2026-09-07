"""段落记录在 SQLite 行与规范字典之间的形状转换。

哪些字段进 typed column、哪些留在 payload_json、旧库的全量 payload 如何补水，
只与存储表示有关，不涉及任何领域概念。原先住在 ``database`` 里，使
``bibliographic_metadata`` 为了一个纯函数而顶层依赖整个 database 模块，
构成 0.4.x 遗留的领域纠缠环；下沉到 persistence 后该环消除。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Dict


# These four large strings already have authoritative typed columns.  Keeping
# them in payload_json as well made every paragraph carry two complete copies
# of all searchable text representations.  New/rebuilt databases store a
# sparse payload; paragraph_from_database_row transparently hydrates both old
# full payloads and new sparse ones.
PARAGRAPH_PAYLOAD_OMITTED_FIELDS = frozenset(
    {"text_raw", "normalized_text", "compact_text", "plain_text", "sentences"}
)

PARAGRAPH_TYPED_COLUMNS = (
    "paragraph_id",
    "volume_id",
    "work_id",
    "source_file_id",
    "source_type",
    "paragraph_index",
    "eligible_for_search",
    "text_raw",
    "normalized_text",
    "compact_text",
    "plain_text",
    "page_display",
    "page_source_type",
    "page_confidence",
    "citation_page_start",
    "citation_page_end",
    "pdf_page_start_index",
    "pdf_page_end_index",
    "pdf_page_start_label",
    "pdf_page_end_label",
)

PARAGRAPH_SELECT_COLUMNS = ", ".join(
    f"p.{column} AS {column}" for column in PARAGRAPH_TYPED_COLUMNS
) + ", p.payload_json AS payload_json"


def paragraph_payload_for_storage(paragraph: Dict[str, object]) -> Dict[str, object]:
    """Return the non-duplicated JSON portion of one paragraph record."""

    return {
        key: value
        for key, value in paragraph.items()
        if key not in PARAGRAPH_PAYLOAD_OMITTED_FIELDS
    }


def paragraph_from_database_row(row: sqlite3.Row) -> Dict[str, object]:
    """Hydrate one canonical paragraph from a sparse or legacy database row."""

    raw_payload = row["payload_json"]
    try:
        payload = json.loads(raw_payload) if raw_payload else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    available = set(row.keys())
    for column in PARAGRAPH_TYPED_COLUMNS:
        if column not in available:
            continue
        value = row[column]
        # A few pre-v1/import-recovery databases populated optional values in
        # JSON but left the newer typed columns NULL. Preserve that legacy
        # value; all current writers update both representations together.
        if value is None and payload.get(column) not in (None, ""):
            continue
        if column == "eligible_for_search":
            value = bool(value)
        payload[column] = value
    return payload
