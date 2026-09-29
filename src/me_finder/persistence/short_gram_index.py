"""Auxiliary unigram/bigram index for short (<3 character) search queries.

The trigram FTS index cannot serve queries shorter than three characters, so
those recall passes scan every eligible paragraph with ``instr``. This index
stores each paragraph's distinct characters and adjacent character pairs in a
contentless FTS5 table; a short query ANDs its pairs to select a *superset* of
the paragraphs that can contain it. Recall still verifies every candidate with
``instr`` against the canonical text, so match decisions, offsets, candidate
budgets and ordering are unchanged -- the index only skips paragraphs that
cannot match.

Correctness never depends on a writer knowing about this index: triggers
(see :mod:`short_gram_schema`) queue every inserted, deleted or edited rowid as
*pending*; the prefilter always admits pending rowids and a background drain
replaces their grams in short write transactions.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .connection import connect_index
from .short_gram_schema import (
    GRAMS_TABLE,
    PENDING_TABLE,
    TEXT_COLUMNS as _TEXT_COLUMNS,
    install_short_gram_triggers,
    objects_present as _objects_present,
    short_gram_supported,
    upgrade_short_gram_triggers,
)

SHORT_GRAM_INDEX_VERSION = 1
SHORT_GRAM_METADATA_KEY = "paragraph_short_gram_version"
# Above this backlog the prefilter would admit most of the library anyway and
# lose the rowid-ordered early stop; recall keeps the plain scan until drained.
PENDING_PREFILTER_LIMIT = 4096
# An AND over a subset of a string's pairs still selects a superset.
MAX_TERMS_PER_STRING = 48
DRAIN_MAX_ROWS = 400
DRAIN_MAX_CHARS = 200_000
DRAIN_BUSY_TIMEOUT_MS = 250

PREFILTER_SQL = (
    f" AND p.rowid IN (SELECT rowid FROM {GRAMS_TABLE} WHERE {GRAMS_TABLE} MATCH ?"
    f" UNION ALL SELECT paragraph_rowid FROM {PENDING_TABLE})"
)
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


@lru_cache(maxsize=1 << 16)
def _code(character: str) -> str:
    """Fixed-width ASCII token for one code point (36**4 > 0x10FFFF)."""

    value = ord(character)
    digits = []
    for _ in range(4):
        value, remainder = divmod(value, 36)
        digits.append(_BASE36[remainder])
    return "".join(reversed(digits))


def paragraph_gram_tokens(texts: Iterable[Optional[str]]) -> str:
    """Space-joined distinct unigram and bigram tokens of every text."""

    tokens: set[str] = set()
    for text in texts:
        codes = [_code(character) for character in text or ""]
        tokens.update(codes)
        tokens.update(left + right for left, right in zip(codes, codes[1:]))
    return " ".join(tokens)


def short_gram_match_expression(strings: Sequence[str]) -> Optional[str]:
    """OR over ``strings`` of the AND of each string's pairs (or its character).

    A paragraph containing any of the strings contains every pair of it, so
    the expression selects a superset of the paragraphs ``instr`` can match.
    """

    groups: List[str] = []
    for text in dict.fromkeys(item for item in strings if item):
        codes = [_code(character) for character in text]
        terms = codes if len(codes) == 1 else list(
            dict.fromkeys(left + right for left, right in zip(codes, codes[1:]))
        )
        quoted = ['"' + term + '"' for term in terms[:MAX_TERMS_PER_STRING]]
        groups.append("(" + " AND ".join(quoted) + ")")
    return " OR ".join(groups) or None


def install_short_gram_index(
    connection: sqlite3.Connection, *, rebuild: bool = False
) -> bool:
    """Create the index, triggers and marker; queue every paragraph when new.

    Returns whether anything changed. An unsupported SQLite build returns
    False without creating anything; searches then keep the plain scan.
    """

    if not rebuild and _objects_present(connection) and _marker_current(connection):
        return False
    if not short_gram_supported():
        # Keep another machine's index writable here; it stays unread.
        upgrade_short_gram_triggers(connection)
        return False
    connection.execute("SAVEPOINT install_short_grams")
    try:
        # Without a current marker the grams may be partial: start over.
        connection.execute(f"DROP TABLE IF EXISTS {GRAMS_TABLE}")
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {GRAMS_TABLE} USING fts5("
            "grams, content='', contentless_delete=1, detail='none', tokenize='ascii')"
        )
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {PENDING_TABLE} "
            "(paragraph_rowid INTEGER PRIMARY KEY)"
        )
        install_short_gram_triggers(connection)
        connection.execute(f"DELETE FROM {PENDING_TABLE}")
        connection.execute(f"INSERT INTO {PENDING_TABLE} SELECT rowid FROM paragraphs")
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            (SHORT_GRAM_METADATA_KEY, json.dumps(SHORT_GRAM_INDEX_VERSION)),
        )
        connection.execute("RELEASE SAVEPOINT install_short_grams")
        return True
    except sqlite3.OperationalError:
        connection.execute("ROLLBACK TO SAVEPOINT install_short_grams")
        connection.execute("RELEASE SAVEPOINT install_short_grams")
        return False


def short_gram_prefilter_ready(connection: sqlite3.Connection) -> bool:
    """Whether recall may add :data:`PREFILTER_SQL` right now."""

    if not short_gram_supported():
        return False
    try:
        if not _objects_present(connection) or not _marker_current(connection):
            return False
        backlog = connection.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {PENDING_TABLE} LIMIT ?)",
            (PENDING_PREFILTER_LIMIT + 1,),
        ).fetchone()[0]
    except sqlite3.Error:
        return False
    return int(backlog) <= PENDING_PREFILTER_LIMIT


def short_gram_prefilter(strings: Sequence[str]) -> Tuple[str, List[object]]:
    """SQL fragment and arguments restricting ``p`` to possible matches."""

    expression = short_gram_match_expression(strings)
    return ("", []) if expression is None else (PREFILTER_SQL, [expression])


def drain_short_gram_backlog(connection: sqlite3.Connection) -> int:
    """Index one bounded batch of pending paragraphs in one write transaction.

    A pending rowid whose paragraph is gone only loses its stale grams.
    """

    if not short_gram_supported() or not _objects_present(connection):
        return 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT q.paragraph_rowid, "
            + ", ".join(f"p.{column}" for column in _TEXT_COLUMNS)
            + f" FROM {PENDING_TABLE} q LEFT JOIN paragraphs p"
            " ON p.rowid = q.paragraph_rowid ORDER BY q.paragraph_rowid LIMIT ?",
            (DRAIN_MAX_ROWS,),
        ).fetchall()
        done: List[Tuple[int]] = []
        characters = 0
        for rowid, *texts in rows:
            if done and characters >= DRAIN_MAX_CHARS:
                break
            characters += sum(len(text or "") for text in texts)
            connection.execute(f"DELETE FROM {GRAMS_TABLE} WHERE rowid = ?", (rowid,))
            if texts[0] is not None:
                connection.execute(
                    f"INSERT INTO {GRAMS_TABLE}(rowid, grams) VALUES (?, ?)",
                    (rowid, paragraph_gram_tokens(texts)),
                )
            done.append((rowid,))
        connection.executemany(
            f"DELETE FROM {PENDING_TABLE} WHERE paragraph_rowid = ?", done
        )
        connection.commit()
        return len(done)
    except BaseException:
        connection.rollback()
        raise


def drain_short_gram_backlog_at(db_path: Path) -> int:
    """Open a short-timeout writer, drain one batch, and report its size.

    Lock contention returns 0 so the caller simply retries later.
    """

    try:
        connection = connect_index(
            db_path, write=True, row_factory=None, busy_timeout_ms=DRAIN_BUSY_TIMEOUT_MS
        )
    except sqlite3.Error:
        return 0
    try:
        return drain_short_gram_backlog(connection)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
            return 0
        raise
    finally:
        connection.close()


def _marker_current(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT value_json FROM metadata WHERE key = ?", (SHORT_GRAM_METADATA_KEY,)
        ).fetchone()
        return row is not None and int(json.loads(row[0])) == SHORT_GRAM_INDEX_VERSION
    except (sqlite3.Error, TypeError, ValueError):
        return False
