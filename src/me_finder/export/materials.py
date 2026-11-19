"""Export saved excerpt text and its recorded citation to Markdown."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ..errors import UserFacingError
from ..persistence.materials_store import read_materials_snapshot


def export_materials_markdown(database_path: Path, payload: dict) -> dict[str, str]:
    """Render the requested excerpt order, or save it to the selected folder."""
    ids = payload.get("excerpt_ids")
    heading = payload.get("heading")
    if not isinstance(ids, list) or not ids or any(type(value) is not int for value in ids):
        raise UserFacingError("请选择要导出的语料")
    if not isinstance(heading, str) or not heading or len(heading) > 100 or not heading.isprintable():
        raise UserFacingError("素材导出标题无效")
    snapshot = read_materials_snapshot(database_path)
    excerpts = {row["excerpt_id"]: row for row in snapshot["material_excerpts"]}
    tags: dict[int, list[str]] = {}
    for row in snapshot["material_excerpt_tags"]:
        tags.setdefault(row["excerpt_id"], []).append(row["path"])
    if any(excerpt_id not in excerpts for excerpt_id in ids):
        raise UserFacingError("部分语料已变化，请重新打开素材库")
    sections = []
    for excerpt_id in ids:
        excerpt = excerpts[excerpt_id]
        start, end = excerpt["citation_start"], excerpt["citation_end"]
        page = "页码未核定"
        if start:
            page_range = f"{start}–{end}" if end and end != start else start
            page = f"第 {page_range} 页"
        origin = " · ".join(filter(None, (excerpt["author"], f"《{excerpt['title']}》", page)))
        quote = "\n".join(f"> {line}" for line in excerpt["text"].split("\n"))
        parts = [quote, origin]
        if excerpt_id in tags:
            parts.append("标签　" + "、".join(tags[excerpt_id]))
        if excerpt["note"]:
            parts.append("备注　" + excerpt["note"])
        sections.append("\n\n".join(parts))
    markdown = f"# {heading}\n\n" + "\n\n---\n\n".join(sections) + "\n"
    if "output_dir" not in payload:
        return {"markdown": markdown}

    output_dir = payload["output_dir"]
    if not isinstance(output_dir, str) or not output_dir:
        raise UserFacingError("请先选择素材导出文件夹")
    directory = Path(output_dir).expanduser()
    if not directory.is_absolute() or not directory.is_dir():
        raise UserFacingError("素材导出路径必须是现有文件夹")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = directory / f"MEFinder-素材库-{timestamp}-{uuid4().hex[:8]}.md"
    stream = NamedTemporaryFile(mode="w", dir=directory, encoding="utf-8", delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            stream.write(markdown)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(target)}
