"""Repeatable A/B comparison for search over a frozen snapshot.

Runs a fixed query set through the **real application search path** — the same
``execute_with_script_folding`` union that ``IndexRuntime.search`` (and the HTTP
``/api/search`` handler) uses when 繁简 folding is on — not a bare
``SearchEngine.search`` call. It captures, per query:

- the folded **union** response (what a user actually receives);
- each script **variant** run on its own (e.g. "社会" and "社會") so a
  high-frequency simplified term and its low-frequency traditional variant are
  reported separately and never conflated;
- a canonical digest of the **full** union response — every contract field
  (hits, order, dedup, total, total_is_exact, has_more, match_type, score,
  match_start/end, page, page_match_spans, context, citation) — with only the
  explicitly listed non-deterministic fields excluded.

Two modes:
- write a baseline JSON (``--output``);
- compare against a baseline (``--compare``) and exit non-zero on any digest
  mismatch, printing the first differing query. This is the equivalence gate
  for a search change: run it on the old tree, then the new tree, and diff.

Read-only: opens the snapshot via ``SearchEngine`` (query_only). No network.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.me_finder import script_conversion  # noqa: E402
from src.me_finder.application.script_search import (  # noqa: E402
    execute_with_script_folding,
)
from src.me_finder.application.search_service import SearchRequest  # noqa: E402
from src.me_finder.search import SearchEngine  # noqa: E402

# Fields that legitimately vary run-to-run and are excluded from the digest.
_NON_DETERMINISTIC_TOP = ("index_metadata",)

# Fixed query set: the eight baseline queries plus short/variant coverage.
_QUERIES = [
    {"id": "common_zh", "query": "社会", "mode": "auto", "source_type": "all"},
    {"id": "script_variant", "query": "社會", "mode": "exact", "source_type": "all"},
    {"id": "common_en", "query": "gender", "mode": "auto", "source_type": "all"},
    {"id": "one_char", "query": "社", "mode": "auto", "source_type": "all"},
    {"id": "three_char", "query": "社会学", "mode": "auto", "source_type": "all"},
    {"id": "short_en", "query": "the", "mode": "auto", "source_type": "all"},
    {"id": "trad_common", "query": "國家", "mode": "auto", "source_type": "all"},
    {"id": "scoped_pdf", "query": "社会", "mode": "auto", "source_type": "pdf"},
    {"id": "no_hit", "query": "MEFinderBaselineNoHit7f83b92c60a4", "mode": "exact",
     "source_type": "all"},
    {"id": "en_exact",
     "query": "the author and publisher have provided this e-book to you for "
              "your personal use ",
     "mode": "exact", "source_type": "all"},
]


def _canonical_hit(hit: dict) -> dict:
    # Every contract field a user relies on for citing a passage.
    return {
        "id": hit.get("paragraph_id") or hit.get("id"),
        "source_file_id": hit.get("source_file_id"),
        "match_type": hit.get("match_type"),
        "score": round(float(hit.get("match_score") or hit.get("score") or 0), 6),
        "match_start": hit.get("match_start"),
        "match_end": hit.get("match_end"),
        "page": hit.get("page") or hit.get("page_display"),
        "page_match_spans": hit.get("page_match_spans"),
        "citation": hit.get("citation"),
        "context_before": hit.get("context_before"),
        "context_after": hit.get("context_after"),
    }


def canonical_response(response: dict) -> dict:
    out = {
        key: value
        for key, value in response.items()
        if key not in _NON_DETERMINISTIC_TOP and key != "results"
    }
    out["results"] = [_canonical_hit(hit) for hit in response.get("results", [])]
    return out


def _median_ms(fn, repeats: int) -> tuple[float, object]:
    fn()  # warm
    samples = []
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return round(statistics.median(samples), 1), result


def measure_query(engine, spec: dict, repeats: int) -> dict:
    request = SearchRequest(
        query=spec["query"], mode=spec["mode"], limit=spec.get("limit", 10),
        source_type=spec.get("source_type", "all"),
    )
    variants = script_conversion.query_variants(spec["query"], enabled=True)

    union_ms, union = _median_ms(
        lambda: execute_with_script_folding(engine, request, enabled=True), repeats
    )
    # Per-variant single-path timing (folding off, one query each), so the
    # simplified and traditional costs are visible separately.
    variant_timing = {}
    for variant in variants:
        v_request = SearchRequest(
            query=variant, mode=spec["mode"], limit=spec.get("limit", 10),
            source_type=spec.get("source_type", "all"),
        )
        v_ms, v_res = _median_ms(
            lambda r=v_request: execute_with_script_folding(engine, r, enabled=False),
            repeats,
        )
        variant_timing[variant] = {"p50_ms": v_ms, "total": v_res.get("total")}
    return {
        "id": spec["id"],
        "variants": variants,
        "union": {"p50_ms": union_ms, "total": union.get("total"),
                  "total_is_exact": union.get("total_is_exact"),
                  "has_more": union.get("has_more")},
        "variant_single_path": variant_timing,
        "digest": canonical_response(union),
    }


def run(db: Path, repeats: int) -> dict:
    engine = SearchEngine(db)
    try:
        return {
            "db": str(db),
            "repeats": repeats,
            "queries": [measure_query(engine, spec, repeats) for spec in _QUERIES],
        }
    finally:
        engine.close()


def _digests(report: dict) -> dict:
    return {q["id"]: q["digest"] for q in report["queries"]}


def compare(current: dict, baseline_path: Path) -> int:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    cur, base = _digests(current), _digests(baseline)
    mismatches = []
    for qid in sorted(set(cur) | set(base)):
        if cur.get(qid) != base.get(qid):
            mismatches.append(qid)
    if mismatches:
        print(f"DIGEST MISMATCH in {len(mismatches)} queries: {mismatches}")
        first = mismatches[0]
        print("  baseline:", json.dumps(base.get(first), ensure_ascii=False)[:400])
        print("  current :", json.dumps(cur.get(first), ensure_ascii=False)[:400])
        return 1
    print(f"OK: {len(cur)} query digests identical to baseline")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="index.sqlite3 snapshot (read-only)")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, help="write the full report JSON")
    parser.add_argument("--compare", type=Path, help="baseline report to diff digests against")
    arguments = parser.parse_args()
    report = run(arguments.db, arguments.repeats)
    if arguments.output is not None:
        arguments.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"wrote {arguments.output}")
    for query in report["queries"]:
        union = query["union"]
        singles = " ".join(
            f"{v}={t['p50_ms']}ms/{t['total']}"
            for v, t in query["variant_single_path"].items()
        )
        print(f"  {query['id']:<16} union p50={union['p50_ms']:>8}ms total={union['total']:>4}"
              f"  | variants: {singles}")
    if arguments.compare is not None:
        return compare(report, arguments.compare)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
