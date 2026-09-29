"""Where the short-query gram index can live, and its writer-side triggers.

The grams table needs FTS5 ``contentless_delete`` (SQLite 3.43+). The
triggers deliberately touch only the ordinary pending table, so a SQLite build
without that option can still insert, edit and delete paragraphs in a library
whose index another machine built; such a build never reads or drains the
grams and search keeps the plain scan. Packaged runtimes must provide 3.43+
(the build scripts refuse a blank index without :data:`GRAMS_TABLE`).
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

GRAMS_TABLE = "paragraph_short_grams"
PENDING_TABLE = "paragraph_short_gram_pending"
TEXT_COLUMNS = ("text_raw", "normalized_text", "compact_text", "plain_text")
# FTS5 gained ``contentless_delete`` in 3.43.0; the grams table needs it.
MIN_SQLITE_VERSION = (3, 43, 0)
TRIGGER_NAMES = (
    "paragraph_short_grams_ai",
    "paragraph_short_grams_ad",
    "paragraph_short_grams_au",
)


@lru_cache(maxsize=1)
def short_gram_supported() -> bool:
    """Whether this SQLite build can open the grams table at all."""

    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        return False
    try:
        probe = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        probe.execute(
            "CREATE VIRTUAL TABLE probe USING fts5("
            "grams, content='', contentless_delete=1, detail='none', tokenize='ascii')"
        )
        return True
    except sqlite3.Error:
        return False
    finally:
        probe.close()


def install_short_gram_triggers(connection: sqlite3.Connection) -> None:
    """(Re)create the triggers; they only queue rowids, never touch the grams.

    Pure DDL on ordinary tables, so any SQLite build can run it -- including
    one that cannot open the grams table itself.
    """

    text_columns = ", ".join(TEXT_COLUMNS)
    for name in TRIGGER_NAMES:
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for statement in (
        f"""CREATE TRIGGER paragraph_short_grams_ai
            AFTER INSERT ON paragraphs BEGIN
                INSERT OR IGNORE INTO {PENDING_TABLE} VALUES (new.rowid);
            END""",
        f"""CREATE TRIGGER paragraph_short_grams_ad
            AFTER DELETE ON paragraphs BEGIN
                INSERT OR IGNORE INTO {PENDING_TABLE} VALUES (old.rowid);
            END""",
        f"""CREATE TRIGGER paragraph_short_grams_au
            AFTER UPDATE OF {text_columns} ON paragraphs BEGIN
                INSERT OR IGNORE INTO {PENDING_TABLE} VALUES (old.rowid);
                INSERT OR IGNORE INTO {PENDING_TABLE} VALUES (new.rowid);
            END""",
    ):
        connection.execute(statement)


def upgrade_short_gram_triggers(connection: sqlite3.Connection) -> bool:
    """Migration step: replace v10 triggers that deleted from the grams table.

    Those triggers made every paragraph update/delete fail on a SQLite build
    without ``contentless_delete`` once another machine had built the index.
    """

    if not objects_present(connection):
        return False
    install_short_gram_triggers(connection)
    return True


def objects_present(connection: sqlite3.Connection) -> bool:
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN (?, ?)",
            (GRAMS_TABLE, PENDING_TABLE),
        )
    }
    return names == {GRAMS_TABLE, PENDING_TABLE}
