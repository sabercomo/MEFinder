"""Script-folding dual-track search for Traditional/Simplified Chinese (#16).

Wraps ``SearchService.execute`` so a query typed in one Chinese script also
matches paragraphs written in the other, without touching ``search.py``,
the FTS index, or page-anchor data.  ``script_conversion.query_variants``
expands the query (e.g. "剩余价值" -> ["剩余价值", "剩餘價值"]); each variant
runs through the regular search pipeline unchanged, so trigram FTS, the
legacy ``_search_sql`` fallback, and the SequenceMatcher precise stage all
keep working per variant, and every returned offset still refers to the
original ``text_raw``.  Page-number fields flow through untouched.

Merge policy is deliberately simple ("不用太精细" per the issue): results
in the script the user typed come first, cross-script hits follow in their
own relevance order, and a paragraph duplicated across variants is kept at
its first (best-ranked) occurrence.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List

from .. import script_conversion
from .search_service import SearchRequest, SearchService


def _merge_key(item: Dict[str, object]) -> object:
    paragraph_id = item.get("paragraph_id")
    if paragraph_id is not None:
        return (item.get("source_file_id"), paragraph_id)
    return id(item)


def _cap(results: List[Dict[str, object]], limit: object) -> List[Dict[str, object]]:
    # ``SearchLimit`` also allows the legacy "all" value; only real integers
    # cap the merged list (bool excluded defensively: it subclasses int).
    if isinstance(limit, bool) or not isinstance(limit, int):
        return results
    return results[:limit]


def execute_with_script_folding(
    engine,
    request: SearchRequest,
    *,
    enabled: bool = True,
) -> Dict[str, object]:
    """Run ``request`` across Traditional/Simplified query variants.

    With the toggle off, an empty query, or no OpenCC installed this is a
    plain pass-through to ``SearchService.execute``.  ``total`` is the
    number of merged results actually returned, matching the empty-result
    convention of ``SearchService`` itself.
    """
    try:
        variants = script_conversion.query_variants(request.query, enabled=enabled)
    except Exception:
        variants = [request.query]
    if len(variants) <= 1:
        return SearchService.execute(engine, request)

    merged: Dict[object, Dict[str, object]] = {}
    order: List[object] = []
    base = None
    for variant in variants:
        raw = SearchService.execute(engine, replace(request, query=variant))
        if base is None:
            base = raw
        for item in raw.get("results", []):
            key = _merge_key(item)
            if key not in merged:
                merged[key] = item
                order.append(key)
    if base is None:  # defensive: variants was non-empty, so unreachable
        return SearchService.execute(engine, request)
    results = _cap([merged[key] for key in order], getattr(request, "limit", None))
    folded = dict(base)
    folded["results"] = results
    folded["total"] = len(results)
    return folded
