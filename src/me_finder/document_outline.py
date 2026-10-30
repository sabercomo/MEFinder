"""Chapter navigation from indexed headings, without reading source files."""

from __future__ import annotations

import json
from pathlib import Path
import re

from .database import open_database
from .export.normalize import build_page_artifact_profile, reader_heading_spans
from .persistence import structured_reader_reads as reader_reads
from .structured_reader import (
    SourceNotFound,
    StructuredReaderError,
    UnsupportedSourceType,
    validate_source_id,
)


def get_document_outline(db_path: Path, source_id: str) -> dict[str, object]:
    """Return level-1/2 titles and natural positions in Unicode code points."""
    source_id = validate_source_id(source_id)
    if not Path(db_path).is_file():
        raise StructuredReaderError(f"索引数据库不存在：{db_path}")
    connection = open_database(db_path)
    try:
        source = reader_reads.read_source_row(connection, source_id)
        if source is None:
            raise SourceNotFound(f"未找到文献：{source_id}")
        entries = []
        if source["source_type"] == "pdf":
            # Two streaming passes keep full-book text out of the response and memory.
            profile = build_page_artifact_profile(
                json.loads(row["payload_json"])
                for row in reader_reads.iter_outline_pdf_pages(connection, source_id)
            )
            for row in reader_reads.iter_outline_pdf_pages(connection, source_id):
                page = json.loads(row["payload_json"])
                for heading in reader_heading_spans(page, profile=profile):
                    entries.append({**heading, "item_index": row["pdf_page_index"],
                                    "anchor_id": page.get("pdf_page_id")})
        elif source["source_type"] == "word":
            for row in reader_reads.iter_outline_paragraphs(connection, source_id):
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
