"""SQLite state for user-owned excerpts, independent of source lifecycle."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

from .connection import PROJECT_BUSY_TIMEOUT_MS, open_readonly_snapshot, open_write, table_exists
from .migrations import migrate_index_database
from .paragraph_payload import paragraph_from_database_row

EXCERPT_FIELDS = (
    "source_id", "kind", "start_anchor", "end_anchor", "start_offset", "end_offset",
    "text", "title", "author", "citation_start", "citation_end",
)
TABLES = {
    "material_tags": ("path",),
    "material_excerpts": ("excerpt_id", *EXCERPT_FIELDS, "note", "created_at"),
    "material_excerpt_tags": ("excerpt_id", "path"),
    "material_images": ("digest", "title", "mime", "note", "created_at", "origin"),
    "material_image_tags": ("digest", "path"),
}
IDENTITY = EXCERPT_FIELDS[:7]


def read_materials(connection: sqlite3.Connection) -> dict[str, list]:
    """Read excerpts, images and shared tags in one transaction."""
    return {
        table: [dict(zip(columns, row)) for row in connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(columns[:2])}"
        )] if table_exists(connection, table) else [] for table, columns in TABLES.items()
    }


def read_materials_snapshot(db_path: Path) -> dict[str, list]:
    """Snapshot material data even when source files were removed."""
    if db_path.is_file():
        with open_readonly_snapshot(db_path) as connection:
            connection.execute("BEGIN")
            if table_exists(connection, "material_excerpts"):
                return read_materials(connection)
    return {table: [] for table in TABLES}


def ensure_materials_schema(db_path: Path) -> None:
    """Upgrade older indexes before their first material write."""
    with open_readonly_snapshot(db_path) as connection:
        if table_exists(connection, "material_images"):
            return
    migrate_index_database(db_path)


def insert_materials_snapshot(connection: sqlite3.Connection, snapshot: dict[str, list]) -> None:
    """Copy the material snapshot into an empty index."""
    for table, columns in TABLES.items():
        connection.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [tuple(row[column] for column in columns) for row in snapshot.get(table, [])],
        )


def replace_materials_snapshot(snapshot: dict[str, list], db_path: Path) -> None:
    """Replace all material data atomically on restore."""
    ensure_materials_schema(db_path)
    with open_write(db_path, immediate=True, busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS) as connection:
        if "material_images" not in snapshot:
            current = read_materials(connection)
            image_tags = set()
            for assignment in current["material_image_tags"]:
                parts = assignment["path"].split("/")
                image_tags.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
            image_tags.update(row["path"] for row in snapshot["material_tags"])
            snapshot = {
                **snapshot,
                "material_tags": [{"path": path} for path in sorted(image_tags)],
                "material_images": current["material_images"],
                "material_image_tags": current["material_image_tags"],
            }
        for table in reversed(TABLES):
            connection.execute(f"DELETE FROM {table}")
        insert_materials_snapshot(connection, snapshot)
        connection.commit()


def insert_tag(connection: sqlite3.Connection, path: str) -> None:
    """Make all parents of a canonical tag path available."""
    parts = path.split("/")
    connection.executemany("INSERT OR IGNORE INTO material_tags(path) VALUES (?)", [
        ("/".join(parts[:length]),) for length in range(1, len(parts) + 1)
    ])


def find_excerpt(connection: sqlite3.Connection, fields: dict[str, object]) -> int | None:
    """Find the same text at the same canonical position."""
    row = connection.execute(
        f"SELECT excerpt_id FROM material_excerpts WHERE {' AND '.join(f'{key}=?' for key in IDENTITY)}",
        tuple(fields[key] for key in IDENTITY),
    ).fetchone()
    return row["excerpt_id"] if row else None


def read_matching_excerpts(connection: sqlite3.Connection, fields: dict[str, object]) -> list[dict]:
    """Find older positions for the same text in the same source and anchor space."""
    return [dict(row) for row in connection.execute(
        "SELECT * FROM material_excerpts WHERE source_id=? AND kind=? AND text=? ORDER BY excerpt_id",
        (fields["source_id"], fields["kind"], fields["text"]),
    )]


def insert_excerpt(connection: sqlite3.Connection, fields: dict[str, object], created_at: str) -> int:
    """Save verified text and its citation snapshot."""
    row = connection.execute(
        f"INSERT INTO material_excerpts ({', '.join(EXCERPT_FIELDS)}, created_at) "
        f"VALUES ({','.join('?' for _ in EXCERPT_FIELDS)}, ?) RETURNING excerpt_id",
        (*[fields[field] for field in EXCERPT_FIELDS], created_at),
    ).fetchone()
    return row["excerpt_id"]


def read_excerpt(connection: sqlite3.Connection, excerpt_id: int) -> dict | None:
    """Read one excerpt and its explicit tags."""
    row = connection.execute("SELECT * FROM material_excerpts WHERE excerpt_id=?", (excerpt_id,)).fetchone()
    if row is None:
        return None
    excerpt = dict(row)
    excerpt["tags"] = [row[0] for row in connection.execute(
        "SELECT path FROM material_excerpt_tags WHERE excerpt_id=? ORDER BY path", (excerpt_id,)
    )]
    return excerpt


def attach_tags(
    connection: sqlite3.Connection, excerpt_id: int, paths: list[str], *, replace: bool = False
) -> None:
    """Edit explicit tags or append without overwriting old assignments."""
    if replace:
        connection.execute("DELETE FROM material_excerpt_tags WHERE excerpt_id=?", (excerpt_id,))
    connection.executemany(
        "INSERT OR IGNORE INTO material_excerpt_tags(excerpt_id, path) VALUES (?, ?)",
        [(excerpt_id, path) for path in paths],
    )


def write_note(connection: sqlite3.Connection, excerpt_id: int, note: str) -> None:
    """Save the excerpt's note."""
    connection.execute("UPDATE material_excerpts SET note=? WHERE excerpt_id=?", (note, excerpt_id))


def update_location(connection: sqlite3.Connection, excerpt_id: int, fields: dict[str, object]) -> None:
    """Update a resolved position without overwriting a separately saved excerpt."""
    existing = find_excerpt(connection, fields)
    if existing is not None and existing != excerpt_id:
        return
    connection.execute(
        f"UPDATE material_excerpts SET {', '.join(f'{key}=?' for key in EXCERPT_FIELDS)} "
        "WHERE excerpt_id=?",
        (*[fields[key] for key in EXCERPT_FIELDS], excerpt_id),
    )


def read_material_paragraph(
    connection: sqlite3.Connection, source_id: str, anchor_id: str
) -> tuple[int, str, dict] | None:
    """Read an exact paragraph even if a legacy source reused a position."""
    row = connection.execute(
        "SELECT * FROM paragraphs WHERE source_file_id=? AND paragraph_id=?", (source_id, anchor_id)
    ).fetchone()
    if row is None:
        return None
    return row["paragraph_index"], row["paragraph_id"], paragraph_from_database_row(row)


def paragraph_position(connection: sqlite3.Connection, source_id: str, anchor_id: str) -> int | None:
    """Resolve a paragraph ID within the saved source."""
    row = connection.execute(
        "SELECT paragraph_index FROM paragraphs WHERE source_file_id=? AND paragraph_id=?",
        (source_id, anchor_id),
    ).fetchone()
    return row[0] if row else None


def iter_units(
    connection: sqlite3.Connection, source_id: str, kind: str, start: int, end: int
) -> Iterator[tuple[int, str, dict]]:
    """Stream canonical paragraph or page payloads in reading order."""
    if kind == "pdf_page":
        rows = connection.execute(
            "SELECT pdf_page_index, payload_json FROM pdf_pages "
            "WHERE source_file_id=? AND pdf_page_index BETWEEN ? AND ? ORDER BY pdf_page_index, rowid",
            (source_id, start, end),
        )
        for index, payload in rows:
            fields = json.loads(payload)
            anchor = fields.get("pdf_page_id") or f"{source_id}-PAGE-{index:06d}"
            yield index, anchor, {**fields, "pdf_page_index": index, "source_type": "pdf"}
    else:
        rows = connection.execute(
            "SELECT * FROM paragraphs WHERE source_file_id=? AND paragraph_index BETWEEN ? AND ? "
            "ORDER BY paragraph_index, rowid",
            (source_id, start, end),
        )
        for row in rows:
            yield row["paragraph_index"], row["paragraph_id"], paragraph_from_database_row(row)


def save_image(connection: sqlite3.Connection, image: dict, tags: list[str]) -> bool:
    """Insert a unique image and its tag assignments."""
    if connection.execute("SELECT 1 FROM material_images WHERE digest=?", (image["digest"],)).fetchone():
        return False
    connection.execute(
        "INSERT INTO material_images(digest, title, mime, note, created_at, origin) VALUES (?, ?, ?, ?, ?, ?)",
        tuple(image[key] for key in TABLES["material_images"]),
    )
    for path in tags:
        insert_tag(connection, path)
    connection.executemany(
        "INSERT INTO material_image_tags(digest, path) VALUES (?, ?)",
        [(image["digest"], path) for path in tags],
    )
    return True


def image_mime(connection: sqlite3.Connection, digest: str) -> str | None:
    """Return the MIME type of a registered image."""
    if not table_exists(connection, "material_images"):
        return None
    row = connection.execute("SELECT mime FROM material_images WHERE digest=?", (digest,)).fetchone()
    return row[0] if row else None


def update_image(connection: sqlite3.Connection, digest: str, note: str | None, tags: list[str] | None) -> None:
    """Update image details without replacing the imported file."""
    if note is not None:
        connection.execute("UPDATE material_images SET note=? WHERE digest=?", (note, digest))
    if tags is not None:
        connection.execute("DELETE FROM material_image_tags WHERE digest=?", (digest,))
        for path in tags:
            insert_tag(connection, path)
        connection.executemany(
            "INSERT INTO material_image_tags(digest, path) VALUES (?, ?)",
            [(digest, path) for path in tags],
        )
