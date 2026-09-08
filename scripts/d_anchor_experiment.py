"""D anchor experiment: iterative leave-one-out soft-anchor validation (issue #18).

Hypothesis (from n53): some context-gated soft anchors (name:/term:/number:) are
false friends — a shared surface token pairs two *different* sentences, forging a
mislocated landmark that squeezes the corridor and makes the true target
unreachable.  Detection rule, position-independent and corpus-general: tentatively
drop a soft anchor, re-run the real DP over the corridor bounded by its neighbours,
and measure how far its own source is re-placed (displacement).  Good anchors
re-place at ~0; false friends jump tens of segments.  Remove the worst offender,
re-validate, repeat (so confounded neighbours heal instead of being dropped).

The rule is wired in by monkeypatching ``semantic_alignment._validate_soft_anchors``
for the duration of an experimental ``generate_alignment`` on the read-only copy —
the production module on disk is NOT changed.  This runs the FULL pipeline (DP +
note-channel routing + segment-quality gates), so a recovered link is a real
accepted output, not a raw-DP artefact.  No fixture id, position, or gold answer
enters the rule.

    python -m scripts.d_anchor_experiment \
        --db .codex-tmp/d-experiment/index-v21.sqlite3 \
        --cache dist/MEFinderData/runtime/components/text-alignment/models \
        --ranges reports/alignment-reviewed-body-ranges-2026-09-05.json \
        --pivot pdf-import-a47c115e247c8194 --target pdf-import-1d4016f99fc5dbba \
        --out reports/d-anchor-experiment-2026-09-07.json
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import src.me_finder.semantic_alignment as SA  # noqa: E402
from src.me_finder.semantic_alignment import (  # noqa: E402
    _validate_soft_anchors as _orig_validate, _group_rows, _align_partition,
    _CONTEXT_GATED_ANCHOR_PREFIXES,
)
from src.me_finder.text_alignment import generate_alignment  # noqa: E402

DISPLACEMENT_LIMIT = 50  # clear false-friend outliers (n53 权威=77); spares borderline (<=36)

# Body-relative non-space lengths, stashed by run() so the monkeypatched validator
# scores placement with the real cost model (unit lengths inflate displacement).
_BODY_LENGTHS = {"source": None, "target": None}


def _leave_one_out_displacement(anchors, i, sp, tp, sg, tg, sl, tl, low):
    a = anchors[i]
    prev = anchors[i - 1] if i > 0 else None
    nxt = anchors[i + 1] if i + 1 < len(anchors) else None
    s0 = (prev.source_index + 1) if prev else 0
    t0 = (prev.target_index + 1) if prev else 0
    s1 = nxt.source_index if nxt else len(sl)
    t1 = nxt.target_index if nxt else len(tl)
    if s1 - s0 < 2 or t1 - t0 < 2 or not (s0 <= a.source_index < s1):
        return 0
    landed = None
    for link in _align_partition(sp, tp, sl, tl, s0, s1, t0, t1, sg, tg, low):
        if link.source_start <= a.source_index < link.source_end and link.target_start < link.target_end:
            landed = (link.target_start + link.target_end - 1) // 2
    return abs(landed - a.target_index) if landed is not None else 0


BETTER_ALT_MARGIN = 0.05  # a false friend's source has a clearly better target than the anchor


def _source_has_better_alternative(a, source_prefix, target_prefix, tvec_unit):
    """True iff the anchor's SOURCE segment has a clearly better target than the anchor.

    Source-side better-alternative gate (not a bidirectional mutual-nearest test):
    a false friend pairs a shared surface token in two different sentences, so its
    source's best target is elsewhere; a correct anchor is its source's own best
    target.  This spares correct anchors whose leave-one-out displacement is inflated
    by a hard/noisy neighbourhood (e.g. the 'died 1642' Galileo-footnote anchor),
    which displacement alone wrongly flags.
    """
    svec = source_prefix[a.source_index + 1] - source_prefix[a.source_index]
    n = float(np.linalg.norm(svec))
    if n == 0:
        return False
    svec = svec / n
    anchor_sim = float(svec @ tvec_unit[a.target_index])
    best_sim = float((tvec_unit @ svec).max())
    return best_sim - anchor_sim > BETTER_ALT_MARGIN


def enhanced_validate(anchors, source_prefix, target_prefix, low_threshold):
    """Original validation, then iterative worst-first leave-one-out removal.

    Removal requires BOTH a large leave-one-out displacement AND that the anchor is
    a false friend (its source has a clearly better target elsewhere).  The second
    gate protects correct anchors whose displacement is inflated by a hard region.
    """
    kept = list(_orig_validate(anchors, source_prefix, target_prefix, low_threshold))
    sg, tg = _group_rows(source_prefix), _group_rows(target_prefix)
    sl = _BODY_LENGTHS["source"] or [1] * (source_prefix.shape[0] - 1)
    tl = _BODY_LENGTHS["target"] or [1] * (target_prefix.shape[0] - 1)
    trows = target_prefix[1:] - target_prefix[:-1]
    tvec_unit = trows / np.clip(np.linalg.norm(trows, axis=1, keepdims=True), 1e-12, None)
    removed = []
    while True:
        worst_i, worst_d = None, DISPLACEMENT_LIMIT
        for i, a in enumerate(kept):
            if not a.key.startswith(_CONTEXT_GATED_ANCHOR_PREFIXES):
                continue
            d = _leave_one_out_displacement(kept, i, source_prefix, target_prefix, sg, tg, sl, tl, low_threshold)
            if d > worst_d and _source_has_better_alternative(a, source_prefix, target_prefix, tvec_unit):
                worst_i, worst_d = i, d
        if worst_i is None:
            break
        removed.append((kept[worst_i].source_index, kept[worst_i].target_index, kept[worst_i].key, worst_d))
        del kept[worst_i]
    enhanced_validate.removed = removed  # surfaced for the report
    return kept


def _v12_set_id(db, src):
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        r = c.execute("SELECT segment_set_id FROM segment_sets WHERE source_file_id=? AND segmenter_version='12'", (src,)).fetchone()
        return r[0] if r else None


def _seg_texts(db, set_id):
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        return [r[0] for r in c.execute("SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index", (set_id,))]


def _accepted_links(con, run_id, s_lo, s_hi):
    out = {}
    for r in con.execute(
        "SELECT alignment_link_id, review_status FROM alignment_links WHERE alignment_run_id=?", (run_id,)):
        p = [x[0] for x in con.execute(
            "SELECT t.order_index FROM alignment_link_members m JOIN text_segments t ON t.segment_id=m.segment_id "
            "WHERE m.alignment_link_id=? AND m.side='pivot' ORDER BY t.order_index", (r["alignment_link_id"],))]
        t = [x[0] for x in con.execute(
            "SELECT t.order_index FROM alignment_link_members m JOIN text_segments t ON t.segment_id=m.segment_id "
            "WHERE m.alignment_link_id=? AND m.side='target' ORDER BY t.order_index", (r["alignment_link_id"],))]
        if p and s_lo <= p[0] <= s_hi:
            out[(min(p), max(p) + 1)] = {"target": [min(t), max(t) + 1] if t else None, "status": r["review_status"]}
    return out


def run(args):
    db = Path(args.db).resolve()
    cache = Path(args.cache).resolve()
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        rr = con.execute(
            "SELECT alignment_run_id, document_group_id, pivot_segment_set_id, target_segment_set_id, parameters_json "
            "FROM alignment_runs WHERE pivot_source_file_id=? AND target_source_file_id=? "
            "AND algorithm_version='21' AND status='completed' ORDER BY created_at DESC LIMIT 1", (args.pivot, args.target)).fetchone()
        gid, pset, tset = rr["document_group_id"], rr["pivot_segment_set_id"], rr["target_segment_set_id"]
        bounds = json.loads(rr["parameters_json"])["body_ranges"]  # already in current (v13) coordinates
        # Body-relative non-space lengths for the displacement DP (real cost model).
        ptexts = [r[0] for r in con.execute("SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index", (pset,))]
        ttexts = [r[0] for r in con.execute("SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index", (tset,))]

    def _nsl(texts, lo, hi):
        return [max(1, sum(not c.isspace() for c in t)) for t in texts[lo:hi]]
    _BODY_LENGTHS["source"] = _nsl(ptexts, bounds["pivot"][0], bounds["pivot"][1])
    _BODY_LENGTHS["target"] = _nsl(ttexts, bounds["target"][0], bounds["target"][1])
    if args.limit is not None:
        global DISPLACEMENT_LIMIT
        DISPLACEMENT_LIMIT = args.limit
    enhanced_validate.removed = []

    import time

    def _run(validator):
        SA._validate_soft_anchors = validator
        t0 = time.monotonic()
        try:
            res = generate_alignment(db, gid, args.pivot, args.target, force=True,
                                     model_cache_dir=cache, embedding_model_id="multilingual-e5-large",
                                     reviewed_body_ranges=bounds)
        finally:
            SA._validate_soft_anchors = _orig_validate
        seconds = time.monotonic() - t0
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as c:
            c.row_factory = sqlite3.Row
            return res, _accepted_links(c, res["alignment_run_id"], 0, 10_000), seconds

    _, baseline, base_secs = _run(_orig_validate)          # production baseline (stateless)
    result, enhanced, enh_secs = _run(enhanced_validate)   # enhanced anchor validation

    # Gold regression on this pair's human-"correct" golds (+n74), by full accepted status.
    gold_rows = []
    if args.sample:
        from scripts.d_fixture_reverify import _map_order
        v12p = _v12_set_id(args.db, args.pivot)
        tmap_src = _map_order(_seg_texts(args.db, v12p), ptexts) if v12p else {}
        samples = json.loads(Path(args.sample).read_text(encoding="utf-8"))["samples"]
        pair = f"{args.pivot} -> {args.target}"

        def _link_for(links, pivot):
            for span, v in links.items():
                if span[0] <= pivot < span[1]:
                    return v
            return None
        for g in samples:
            if g["pair"] != pair or (g["label"] != "正确" and g["n"] != 74):
                continue
            pv13 = tmap_src.get(g["pivot"][0][0], g["pivot"][0][0])
            b, e = _link_for(baseline, pv13), _link_for(enhanced, pv13)
            gold_rows.append({"n": g["n"], "label": g["label"], "pivot_v13": pv13,
                              "baseline": b, "enhanced": e,
                              "changed": (b or {}).get("target") != (e or {}).get("target")
                              or (b or {}).get("status") != (e or {}).get("status")})

    changed = []
    for span in sorted(set(baseline) | set(enhanced)):
        b, e = baseline.get(span), enhanced.get(span)
        if (b or {}).get("target") != (e or {}).get("target") or (b or {}).get("status") != (e or {}).get("status"):
            changed.append({"source": list(span), "baseline": b, "enhanced": e})
    record = {"pair": f"{args.pivot} -> {args.target}",
              "removed_anchors": getattr(enhanced_validate, "removed", []),
              "baseline_accepted": sum(1 for v in baseline.values() if v["status"] == "automatic"),
              "enhanced_accepted": sum(1 for v in enhanced.values() if v["status"] == "automatic"),
              "changed_links": changed, "golds": gold_rows, "threshold": DISPLACEMENT_LIMIT,
              "seconds": {"baseline": round(base_secs, 2), "enhanced": round(enh_secs, 2),
                          "leave_one_out_overhead": round(enh_secs - base_secs, 2)},
              "result": {k: result[k] for k in ("accepted_link_count", "unmatched_link_count", "heading_anchor_count")}}
    Path(args.out).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"threshold={DISPLACEMENT_LIMIT} removed anchors: {record['removed_anchors']}")
    print(f"accepted links: {record['baseline_accepted']} -> {record['enhanced_accepted']}; changed: {len(changed)}")
    print(f"timing: baseline={base_secs:.2f}s enhanced={enh_secs:.2f}s (leave-one-out overhead {enh_secs-base_secs:+.2f}s)")
    if gold_rows:
        changed_golds = [g["n"] for g in gold_rows if g["changed"]]
        print(f"golds in pair: {len(gold_rows)}; changed: {changed_golds or 'none'}")
        for g in gold_rows:
            if g["changed"]:
                print(f"  n{g['n']} ({g['label']}) base={g['baseline']} -> enh={g['enhanced']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--ranges", required=True)
    p.add_argument("--pivot", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=None, help="displacement threshold (default 50)")
    p.add_argument("--sample", default=None, help="note-channel sample json for gold regression check")
    run(p.parse_args())
