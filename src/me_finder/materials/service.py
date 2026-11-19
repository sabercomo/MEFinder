"""Transactional operations on locally collected text."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..errors import UserFacingError
from ..export.materials import export_materials_markdown
from ..persistence import materials_store as store
from ..persistence.connection import PROJECT_BUSY_TIMEOUT_MS, open_write
from ..persistence.structured_reader_reads import read_source_row
from .excerpts import relocate_selection, resolve_selection, validate_selection


def tag_path(value: object) -> str:
    """Canonicalize a user-supplied tag path, up to three levels."""
    if not isinstance(value, str):
        raise UserFacingError("标签名必须是文字")
    parts = [part.strip() for part in value.strip().removeprefix("#").replace("／", "/").split("/")]
    if len(parts) > 3:
        raise UserFacingError("标签最多三级")
    if any(not part or len(part) > 30 or not part.isprintable() for part in parts):
        raise UserFacingError("每级标签名需为 1–30 个字符，层级之间用 / 分隔")
    return "/".join(parts)


def _tags(payload: object, *, required: bool = True) -> list[str]:
    if not isinstance(payload, list) or (required and not payload):
        raise UserFacingError("请选择或新建至少一个标签")
    return list(dict.fromkeys(tag_path(path) for path in payload))


def list_materials(db_path: Path) -> dict[str, list]:
    """Return the material library without opening original files."""
    return store.read_materials_snapshot(db_path)


def _find_collected_excerpt(connection: sqlite3.Connection, source: sqlite3.Row, fields: dict) -> int | None:
    excerpt_id = store.find_excerpt(connection, fields)
    if excerpt_id is not None:
        return excerpt_id
    for previous in store.read_matching_excerpts(connection, fields):
        old_selection = {key: previous[key] for key in store.IDENTITY}
        if resolve_selection(connection, source, old_selection) is not None:
            continue
        relocated = relocate_selection(connection, source, old_selection)
        if relocated and all(relocated[0][key] == fields[key] for key in store.IDENTITY):
            store.update_location(connection, previous["excerpt_id"], fields)
            return previous["excerpt_id"]
    return None


def mutate_materials(db_path: Path, payload: object) -> dict:
    """Create tags, save a batch, edit an excerpt, or resolve its live position."""
    if not isinstance(payload, dict):
        raise UserFacingError("素材请求必须是 JSON 对象")
    action = payload.get("action")
    if action == "export":
        return export_materials_markdown(db_path, payload)
    if action not in ("create_tag", "collect", "edit", "locate"):
        raise UserFacingError("不支持的素材操作")
    store.ensure_materials_schema(db_path)
    with open_write(db_path, immediate=True, busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS) as connection:
        if action == "create_tag":
            path = tag_path(payload.get("path"))
            store.insert_tag(connection, path)
            result = {"path": path}
        elif action == "collect":
            paths = _tags(payload.get("tags"))
            selections = payload.get("selections")
            if not isinstance(selections, list) or not selections:
                raise UserFacingError("请选择要收集的原文")
            for path in paths:
                store.insert_tag(connection, path)
            created = 0
            ids = []
            timestamp = datetime.now(timezone.utc).isoformat()
            for selection in selections:
                selection = validate_selection(selection)
                source = read_source_row(connection, selection["source_id"])
                resolved = resolve_selection(connection, source, selection) if source is not None else None
                if resolved is None:
                    raise UserFacingError("原文位置已变化，请重新检索或划选后收集")
                fields, _reader = resolved
                excerpt_id = _find_collected_excerpt(connection, source, fields)
                if excerpt_id is None:
                    excerpt_id = store.insert_excerpt(connection, fields, timestamp)
                    created += 1
                store.attach_tags(connection, excerpt_id, paths)
                ids.append(excerpt_id)
            result = {"excerpt_ids": ids, "created": created}
        else:
            excerpt_id = payload.get("excerpt_id")
            if type(excerpt_id) is not int or not 0 < excerpt_id <= 2**63 - 1:
                raise UserFacingError("摘录编号无效")
            excerpt = store.read_excerpt(connection, excerpt_id)
            if excerpt is None:
                raise UserFacingError("未找到这条摘录")
            if action == "edit":
                if "tags" in payload:
                    paths = _tags(payload["tags"], required=False)
                    for path in paths:
                        store.insert_tag(connection, path)
                    store.attach_tags(connection, excerpt_id, paths, replace=True)
                if "note" in payload:
                    note = payload["note"]
                    if not isinstance(note, str) or len(note) > 10000:
                        raise UserFacingError("备注最多 10000 字符")
                    store.write_note(connection, excerpt_id, note)
                result = {"excerpt_id": excerpt_id}
            else:
                source = read_source_row(connection, excerpt["source_id"])
                if source is None:
                    return {"status": "source_deleted", "excerpt": excerpt}
                selection = {key: excerpt[key] for key in store.IDENTITY}
                resolved = resolve_selection(connection, source, selection)
                if resolved is None:
                    resolved = relocate_selection(connection, source, selection)
                if resolved is None:
                    return {"status": "text_changed", "excerpt": excerpt}
                fields, reader = resolved
                if any(excerpt[key] != fields[key] for key in store.EXCERPT_FIELDS):
                    store.update_location(connection, excerpt_id, fields)
                result = {"status": "located", "excerpt": {**excerpt, **fields}, "reader": reader}
        connection.commit()
        return result
