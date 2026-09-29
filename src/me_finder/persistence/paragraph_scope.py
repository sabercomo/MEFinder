"""The paragraph scope predicate shared by every SQLite recall query.

Scope (bibliographic type, single source, DocumentGroup membership) is stored
across a typed column and a JSON payload field, and EPUB rows sit under
``source_type = 'word'``.  Keeping that mapping in one place is what stops the
recall passes and the passage reads from drifting apart.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

Scope = Optional[frozenset]


def source_filter_clause(
    source_type: str,
    source_file_id: Optional[str],
    scope: Scope,
    alias: str = "",
) -> Tuple[str, List[object]]:
    """WHERE fragment narrowing paragraphs by format, one source or one group."""

    prefix = f"{alias}." if alias else ""
    clauses: List[str] = []
    args: List[object] = []
    if source_type == "epub":
        clauses.append(f"{prefix}source_type = 'word'")
        clauses.append(
            f"json_extract({prefix}payload_json, '$.source_format') = 'epub'"
        )
    elif source_type == "word":
        clauses.append(f"{prefix}source_type = 'word'")
        clauses.append(
            f"COALESCE(json_extract({prefix}payload_json, '$.source_format'), 'word') <> 'epub'"
        )
    elif source_type != "all":
        clauses.append(f"{prefix}source_type = ?")
        args.append(source_type)
    if scope is not None:
        # Explicit set scope (DocumentGroup members). An empty set matches
        # nothing; it must never fall through to an unscoped whole-library search.
        if not scope:
            clauses.append("1 = 0")
        else:
            ordered = list(scope)
            placeholders = ", ".join("?" for _ in ordered)
            clauses.append(f"{prefix}source_file_id IN ({placeholders})")
            args.extend(ordered)
    elif source_file_id:
        clauses.append(f"{prefix}source_file_id = ?")
        args.append(source_file_id)
    return (" AND " + " AND ".join(clauses), args) if clauses else ("", args)
