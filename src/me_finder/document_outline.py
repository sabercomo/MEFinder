"""Chapter navigation from indexed headings, without reading source files."""

from __future__ import annotations

import json
from pathlib import Path
import re

from .database import open_database
from .markdown_export_normalize import build_page_artifact_profile, reader_heading_spans
from .structured_reader import (
    SourceNotFound, StructuredReaderError, UnsupportedSourceType, _validate_source_id,
)


def get_document_outline(db_path: Path, source_id: str) -> dict[str, object]:
    """Return level-1/2 titles and natural positions in Unicode code points."""
    source_id = _validate_source_id(source_id)
    if not Path(db_path).is_file():
        raise StructuredReaderError(f"索引数据库不存在：{db_path}")
    connection = open_database(db_path)
    try:
        source = connection.execute(
            "SELECT source_type FROM source_files WHERE source_file_id = ?", (source_id,)
        ).fetchone()
        if source is None:
            raise SourceNotFound(f"未找到文献：{source_id}")
        entries = []
        if source["source_type"] == "pdf":
            query = "SELECT pdf_page_index, payload_json FROM pdf_pages WHERE source_file_id = ? ORDER BY pdf_page_index"
            # Two streaming passes keep full-book text out of the response and memory.
            profile = build_page_artifact_profile(
                (json.loads(row["payload_json"]) for row in connection.execute(query, (source_id,)))
            )
            for row in connection.execute(query, (source_id,)):
                page = json.loads(row["payload_json"])
                for heading in reader_heading_spans(page, profile=profile):
                    entries.append({**heading, "item_index": row["pdf_page_index"],
                                    "anchor_id": page.get("pdf_page_id")})
        elif source["source_type"] == "word":
            for row in connection.execute(
                "SELECT paragraph_id, paragraph_index, text_raw, payload_json FROM paragraphs "
                "WHERE source_file_id = ? ORDER BY paragraph_index", (source_id,)
            ):
                payload = json.loads(row["payload_json"])
                style = str(payload.get("style_name") or "").strip().lower()
                level = re.fullmatch(r"(?:h|heading\s*|标题\s*)([12])", style)
                text = str(row["text_raw"] or "")
                if level and text.strip():
                    entries.append({"level": int(level[1]), "title": text.strip(),
                                    "item_index": row["paragraph_index"],
                                    "anchor_id": row["paragraph_id"],
                                    "char_start": len(text) - len(text.lstrip()),
                                    "char_end": len(text.rstrip())})
        else:
            raise UnsupportedSourceType(f"暂不支持目录的文献类型：{source['source_type']}")
        return {"source_file_id": source_id, "offset_unit": "unicode_codepoint", "entries": entries}
    finally:
        connection.close()
