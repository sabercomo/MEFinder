"""Reconciling one index identity against newly extracted content.

Splitting a source out of :mod:`database` keeps that module focused on building,
publishing and replacing the SQLite index.  Everything here is pure in-memory
reconciliation: it decides whether a re-import describes the *same* document,
merges fields the new extraction left blank, and collapses duplicate rows.
No SQL, no file publishing.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple


def _int_or_none(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class IndexIdentityConflictError(ValueError):
    """Raised when one persisted index identity points at different content."""

def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}

def _merge_missing_fields(
    canonical: Dict[str, object], duplicate: Dict[str, object]
) -> Dict[str, object]:
    """Keep the first record stable while filling fields it did not have."""

    merged = dict(canonical)
    for key, value in duplicate.items():
        if _is_empty(merged.get(key)) and not _is_empty(value):
            merged[key] = value
    return merged

def _identity_conflict(
    table_name: str,
    identity: object,
    field: str,
    first: object,
    second: object,
) -> IndexIdentityConflictError:
    return IndexIdentityConflictError(
        "索引身份冲突："
        f"{table_name} 的 {identity!r} 在字段 {field!r} 上对应不同内容"
        f"（{first!r} != {second!r}）。请移除重复或损坏的导入记录后重试。"
    )

def _verify_source_identity(
    canonical: Dict[str, object], duplicate: Dict[str, object], source_file_id: str
) -> None:
    """Verify that duplicate source IDs really describe the same file.

    Content hashes are authoritative. Older records without a hash may only be
    merged when they still point at the same file and do not disagree on size.
    This lets retry-created copies of one PDF coalesce without hiding a genuine
    source ID collision.
    """

    first_type = str(canonical.get("source_type") or "").strip()
    second_type = str(duplicate.get("source_type") or "").strip()
    if first_type and second_type and first_type != second_type:
        raise _identity_conflict(
            "source_files", source_file_id, "source_type", first_type, second_type
        )

    first_hash = str(canonical.get("sha256") or "").strip().lower()
    second_hash = str(duplicate.get("sha256") or "").strip().lower()
    if first_hash and second_hash:
        if first_hash != second_hash:
            raise _identity_conflict(
                "source_files", source_file_id, "sha256", first_hash, second_hash
            )
        return

    if canonical == duplicate:
        return

    first_size = _int_or_none(canonical.get("size_bytes"))
    second_size = _int_or_none(duplicate.get("size_bytes"))
    if first_size is not None and second_size is not None and first_size != second_size:
        raise _identity_conflict(
            "source_files", source_file_id, "size_bytes", first_size, second_size
        )

    same_relative_path = bool(
        canonical.get("relative_path")
        and canonical.get("relative_path") == duplicate.get("relative_path")
    )
    same_file_name = bool(
        canonical.get("file_name")
        and canonical.get("file_name") == duplicate.get("file_name")
    )
    if not (same_relative_path or same_file_name):
        raise IndexIdentityConflictError(
            "索引身份冲突："
            f"source_files 的 {source_file_id!r} 存在缺少 SHA-256 且文件位置不同的记录，"
            "无法确认它们是否为同一内容。"
        )

def _deduplicate_source_files(
    rows: Sequence[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], int]:
    """Coalesce retry-created copies by stable source ID.

    The first record is canonical because import configuration order preserves
    the original file before retry-created ``(imported-...)`` copies. Later
    records only fill missing metadata.
    """

    canonical_by_id: Dict[str, Dict[str, object]] = {}
    ordered_ids: List[str] = []
    merged_count = 0
    for row in rows:
        source_file_id = str(row.get("source_file_id") or "").strip()
        if not source_file_id:
            continue
        canonical = canonical_by_id.get(source_file_id)
        if canonical is None:
            canonical_by_id[source_file_id] = dict(row)
            ordered_ids.append(source_file_id)
            continue
        _verify_source_identity(canonical, row, source_file_id)
        canonical_by_id[source_file_id] = _merge_missing_fields(canonical, row)
        merged_count += 1
    return [canonical_by_id[source_id] for source_id in ordered_ids], merged_count

def _deduplicate_keyed_rows(
    rows: Sequence[Dict[str, object]],
    *,
    table_name: str,
    key_fields: Sequence[str],
    content_identity_fields: Sequence[str] = (),
) -> Tuple[List[Dict[str, object]], int]:
    """Deduplicate rows that would otherwise collide on a database identity."""

    canonical_by_key: Dict[Tuple[object, ...], Dict[str, object]] = {}
    ordered_keys: List[Tuple[object, ...]] = []
    merged_count = 0
    for row in rows:
        key = tuple(row.get(field) for field in key_fields)
        if any(value is None or value == "" for value in key):
            # Preserve legacy incomplete rows; their existing insert filters or
            # nullable table columns decide whether they are stored.
            ordered_keys.append((object(),))
            canonical_by_key[ordered_keys[-1]] = dict(row)
            continue
        canonical = canonical_by_key.get(key)
        if canonical is None:
            canonical_by_key[key] = dict(row)
            ordered_keys.append(key)
            continue
        for field in content_identity_fields:
            first = canonical.get(field)
            second = row.get(field)
            if not _is_empty(first) and not _is_empty(second) and first != second:
                raise _identity_conflict(table_name, key, field, first, second)
        canonical_by_key[key] = _merge_missing_fields(canonical, row)
        merged_count += 1
    return [canonical_by_key[key] for key in ordered_keys], merged_count
