"""Audit declared foreign keys of MEFinder SQLite databases without writing them.

Each source database is opened read-only (``mode=ro`` URI) and copied with the
SQLite online backup API into ``--workdir``; every check then runs on the copy.
The JSON report carries only schema names and counts -- no document text,
titles or paths -- so it can be committed under ``reports/``.

Usage (repository root)::

    python -m scripts.foreign_key_audit --workdir <scratch> --label real <db> [...]
    python -m scripts.foreign_key_audit --workdir <scratch> --fixture
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def snapshot(source: Path, target: Path) -> None:
    """Copy ``source`` into ``target`` through a read-only connection."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    uri = f"{source.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as src, closing(
        sqlite3.connect(str(target))
    ) as dst:
        src.backup(dst)


def audit(db_path: Path) -> dict:
    """Return declared foreign keys and ``foreign_key_check`` violations."""

    with closing(sqlite3.connect(str(db_path))) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        declared = []
        for table in tables:
            quoted = table.replace('"', '""')
            for row in connection.execute(f'PRAGMA foreign_key_list("{quoted}")'):
                declared.append(
                    {
                        "table": table,
                        "id": row[0],
                        "parent": row[2],
                        "from": row[3],
                        "to": row[4],
                        "on_delete": row[6],
                    }
                )
        violations: dict[tuple[str, str, int], int] = {}
        for table, _rowid, parent, fkid in connection.execute(
            "PRAGMA foreign_key_check"
        ):
            key = (table, parent, fkid)
            violations[key] = violations.get(key, 0) + 1
        child_rows = {}
        for table in sorted({item["table"] for item in declared}):
            quoted = table.replace('"', '""')
            child_rows[table] = connection.execute(
                f'SELECT COUNT(*) FROM "{quoted}"'
            ).fetchone()[0]
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "table_count": len(tables),
            "declared_foreign_keys": declared,
            "child_table_rows": child_rows,
            "violations": [
                {"table": t, "parent": p, "fk_id": f, "rows": n}
                for (t, p, f), n in sorted(violations.items())
            ],
            "violation_rows": sum(violations.values()),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--label", default="db")
    parser.add_argument("--fixture", action="store_true",
                        help="build the synthetic performance fixture and audit it")
    parser.add_argument("databases", nargs="*", type=Path)
    args = parser.parse_args(argv)

    results = {}
    if args.fixture:
        from scripts.performance_fixture import create_fixture

        fixture_root = args.workdir / "fixture"
        create_fixture(fixture_root)
        for path in sorted(fixture_root.rglob("*.sqlite3")):
            results[f"fixture/{path.name}"] = audit(path)
    for source in args.databases:
        copy = args.workdir / "snapshots" / f"{args.label}-{source.name}"
        snapshot(source, copy)
        results[f"{args.label}/{source.name}"] = audit(copy)
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
