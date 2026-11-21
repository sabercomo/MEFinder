"""Validate portable materials snapshots before backup restore writes files."""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import UserFacingError
from ..persistence.materials_store import IDENTITY, TABLES
from .excerpts import validate_selection
from .service import tag_path
from .images import IMAGE_TYPES, image_path


def decode_materials_snapshot(data: bytes) -> dict[str, list]:
    """Decode a complete snapshot, including valid hierarchy and assignments."""
    try:
        snapshot = json.loads(data)
        if not isinstance(snapshot, dict):
            raise ValueError
        legacy = set(TABLES) - {"material_images", "material_image_tags"}
        if set(snapshot) not in (legacy, set(TABLES)):
            raise ValueError
        for table, rows in snapshot.items():
            columns = TABLES[table]
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or set(row) != set(columns) for row in rows
            ):
                raise ValueError
        json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        paths = {tag_path(row["path"]) for row in snapshot["material_tags"]}
        if len(paths) != len(snapshot["material_tags"]) or any(
            row["path"] not in paths for row in snapshot["material_tags"]
        ):
            raise ValueError
        if any("/" in path and path.rsplit("/", 1)[0] not in paths for path in paths):
            raise ValueError
        ids: set[int] = set()
        identities = set()
        for row in snapshot["material_excerpts"]:
            selection = validate_selection(row)
            key = tuple(selection[field] for field in IDENTITY)
            excerpt_id = row["excerpt_id"]
            if type(excerpt_id) is not int or not 0 < excerpt_id <= 2**63 - 1:
                raise ValueError
            if excerpt_id in ids or key in identities:
                raise ValueError
            ids.add(excerpt_id)
            identities.add(key)
            if any(not isinstance(row[field], str) for field in ("title", "author", "note", "created_at")):
                raise ValueError
            if any(row[field] is not None and not isinstance(row[field], str) for field in ("citation_start", "citation_end")):
                raise ValueError
        assignments = set()
        for row in snapshot["material_excerpt_tags"]:
            excerpt_id, path = row["excerpt_id"], row["path"]
            if (type(excerpt_id) is not int or excerpt_id not in ids
                    or not isinstance(path, str) or path not in paths or (excerpt_id, path) in assignments):
                raise ValueError
            assignments.add((excerpt_id, path))
        digests = set()
        for row in snapshot.get("material_images", []):
            digest = row["digest"]
            image_path(Path("."), digest)
            if digest in digests or any(not isinstance(value, str) for value in row.values()):
                raise ValueError
            if row["mime"] not in IMAGE_TYPES or not isinstance(json.loads(row["origin"]), dict):
                raise ValueError
            digests.add(digest)
        image_assignments = set()
        for row in snapshot.get("material_image_tags", []):
            digest, path = row["digest"], row["path"]
            if (not isinstance(digest, str) or digest not in digests or not isinstance(path, str)
                    or path not in paths or (digest, path) in image_assignments):
                raise ValueError
            image_assignments.add((digest, path))
    except (ValueError, UserFacingError) as exc:
        raise ValueError("素材库快照格式无效") from exc
    return snapshot
