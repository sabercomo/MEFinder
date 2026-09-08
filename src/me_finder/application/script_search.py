"""Expand Chinese queries while preserving original text and source offsets."""
from __future__ import annotations

from dataclasses import replace
from typing import Dict

from .. import script_conversion
from .search_service import SearchRequest, SearchService

_STAGES = ("exact", "compact", "punctuation", "fuzzy")


def _merge_key(item: Dict[str, object]) -> object:
    if item.get("paragraph_id") is None:
        return id(item)
    return (item.get("source_file_id"), item["paragraph_id"],
            item.get("match_start"), item.get("match_end"))


def execute_with_script_folding(engine, request: SearchRequest, *,
                                enabled: bool = True) -> Dict[str, object]:
    """Search every variant at each accuracy stage before falling back.

    Returned hits remain the engine's original objects. A truncated variant
    prevents an exact union count; report a conservative lower bound instead.
    """
    try:
        variants = script_conversion.query_variants(request.query, enabled=enabled)
    except Exception:
        variants = [request.query]
    if len(variants) <= 1:
        return SearchService.execute(engine, request)
    mode = request.mode if request.mode in (*_STAGES, "auto") else "auto"
    return_all = str(request.limit or "").strip().lower() in {"all", "0"}
    limit = None if return_all else max(1, min(int(request.limit or 10), 200))
    batches = []
    for stage in _STAGES if mode == "auto" else (mode,):
        batches = [SearchService.execute(engine, replace(request, query=q, mode=stage))
                   for q in variants]
        if any(b.get("results") or b.get("has_more") or b.get("total", 0)
               for b in batches):
            break
    ranked = sorted(
        ((item, variant, rank) for variant, batch in enumerate(batches)
         for rank, item in enumerate(batch.get("results", []))),
        key=lambda entry: (-float(entry[0].get("match_score") or 0), entry[1], entry[2]),
    )
    unique = {}
    for item, _, _ in ranked:
        unique.setdefault(_merge_key(item), item)
    merged = list(unique.values())
    complete = all(b.get("total_is_exact", True) and not b.get("has_more", False)
                   and b.get("total", len(b.get("results", []))) == len(b.get("results", []))
                   for b in batches)
    total = len(merged) if complete else max(
        [len(merged)] + [int(b.get("total", 0)) for b in batches])
    results = merged if limit is None else merged[:limit]
    return {**batches[0], "query": request.query.strip(), "mode": mode,
            "results": results, "total": total, "total_is_exact": complete,
            "has_more": not complete or len(merged) > len(results),
            "return_all": return_all}
