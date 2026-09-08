"""D single-variable experiment: context-aware gap penalty (issue #18).

Everything on the read-only v13/v21 copy; production algorithm and index untouched.
The *only* variable is the gap penalty applied to structurally-detected edition-only
apparatus / OCR-noise segments (``alignment_corridor_refine.detect_edition_apparatus``
— text structure only, no fixture id, position, embedding, or gold answer).  Embedding,
anchors, segmentation, acceptance threshold and match cost are all fixed.

The acceptance checker actually reads and compares the 60 human-"correct" gold links
plus n74, per-link, at their final accepted status — baseline vs each discount level —
and cross-checks corridors that carry no fixture.  The gold-forced constrained path is
NOT used here (that stays a diagnostic in d_corridor_experiment); a "fix" is only a real
DP output whose accepted target matches the full gold member set.

    python -m scripts.d_gap_penalty_experiment \
        --db .codex-tmp/d-experiment/index-v21.sqlite3 \
        --cache dist/MEFinderData/runtime/components/text-alignment/models \
        --sample reports/note-channel-random-sample-2026-09-06.json \
        --reverify reports/d-fixture-reverify-2026-09-07.json \
        --out reports/d-gap-penalty-experiment-2026-09-07.json
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.me_finder.alignment_corridor_refine import (  # noqa: E402
    align_corridor, corridor_ratio, detect_edition_apparatus, group_similarity,
    non_space_lengths, prefix_sums,
)
from src.me_finder.semantic_alignment import cached_text_sequence_vectors  # noqa: E402
from scripts.d_corridor_experiment import _frozen_links, _anchor_boundaries, _partitions, _find_partition  # noqa: E402
from scripts.d_fixture_reverify import _map_order  # noqa: E402

THRESHOLD = 0.83
LEVELS = [2.2, 1.5, 1.0, 0.5]  # 2.2 == production baseline; others are discounts


def _texts(con, sid):
    return [r[0] for r in con.execute(
        "SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index", (sid,))]


def _v12_set(con, src):
    r = con.execute("SELECT segment_set_id FROM segment_sets WHERE source_file_id=? AND segmenter_version='12'", (src,)).fetchone()
    return r[0] if r else None


class RunState:
    def __init__(self, con, cache, pv, tg):
        rr = con.execute(
            "SELECT alignment_run_id, pivot_segment_set_id, target_segment_set_id, parameters_json "
            "FROM alignment_runs WHERE pivot_source_file_id=? AND target_source_file_id=? "
            "AND algorithm_version='21' AND status='completed' ORDER BY created_at DESC LIMIT 1", (pv, tg)).fetchone()
        self.rid, pset, tset = rr["alignment_run_id"], rr["pivot_segment_set_id"], rr["target_segment_set_id"]
        self.ptexts, self.ttexts = _texts(con, pset), _texts(con, tset)
        prows = cached_text_sequence_vectors(self.ptexts, cache, model_id="multilingual-e5-large")
        trows = cached_text_sequence_vectors(self.ttexts, cache, model_id="multilingual-e5-large")
        self.sp, self.tp = prefix_sums(prows), prefix_sums(trows)
        self.sl, self.tl = non_space_lengths(self.ptexts), non_space_lengths(self.ttexts)
        body = json.loads(rr["parameters_json"])["body_ranges"]
        frozen = _frozen_links(con, self.rid)
        self.parts = _partitions(body, _anchor_boundaries(frozen))
        self.frozen = frozen
        # v12 -> v13 order maps (identity unless the book re-segmented)
        v12p, v12t = _v12_set(con, pv), _v12_set(con, tg)
        self.pmap = _map_order(_texts(con, v12p), self.ptexts) if v12p else {}
        self.tmap = _map_order(_texts(con, v12t), self.ttexts) if v12t else {}
        self._flagged = {}
        self._paths = {}

    def flagged(self, corridor):
        if corridor not in self._flagged:
            s0, s1, t0, t1 = corridor
            self._flagged[corridor] = frozenset(detect_edition_apparatus(self.ttexts, t0, t1))
        return self._flagged[corridor]

    def path(self, corridor, penalty):
        key = (corridor, penalty)
        if key not in self._paths:
            s0, s1, t0, t1 = corridor
            ratio = corridor_ratio(self.sl, self.tl, s0, s1, t0, t1)
            kw = {} if penalty == 2.2 else {"flagged_targets": self.flagged(corridor), "flagged_gap_penalty": penalty}
            self._paths[key] = align_corridor(self.sp, self.tp, self.sl, self.tl, s0, s1, t0, t1, ratio, **kw)["path"]
        return self._paths[key]

    def pivot_link(self, pivot, penalty):
        corridor = _find_partition(self.parts, pivot)
        if corridor is None:
            return None
        for a, b, c, d in self.path(corridor, penalty):
            if a <= pivot < b:
                if c == d:  # target gap -> unmatched
                    return {"target": None, "accepted": False, "confidence": 0.0}
                conf = group_similarity(self.sp, self.tp, a, b, c, d)
                return {"source": [a, b], "target": [c, d], "confidence": round(conf, 4), "accepted": conf >= THRESHOLD}
        return None


def run(args):
    con = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cache = Path(args.cache).resolve()
    samples = json.loads(Path(args.sample).read_text(encoding="utf-8"))["samples"]
    reverify = {r["n"]: r for r in json.loads(Path(args.reverify).read_text(encoding="utf-8"))["fixtures"]}
    golds = [s for s in samples if s["label"] == "正确"] + [s for s in samples if s["n"] == 74]
    fixtures = [s for s in samples if s["label"] == "错配"]

    states = {}

    def state(pair):
        if pair not in states:
            pv, tg = pair.split(" -> ")
            states[pair] = RunState(con, cache, pv, tg)
        return states[pair]

    # --- 60 golds + n74: per-link acceptance at each level ---
    gold_rows = []
    for g in golds:
        st = state(g["pair"])
        pv12 = g["pivot"][0][0]
        pv13 = st.pmap.get(pv12, pv12)
        gold_t = sorted({st.tmap.get(t[0], t[0]) for t in g["target"]})
        base = st.pivot_link(pv13, 2.2)
        row = {"n": g["n"], "label": g["label"], "pair": g["pair"], "pivot_v13": pv13,
               "gold_target_v13": gold_t, "baseline": base, "levels": {}}
        row["baseline_correct"] = bool(base and base["accepted"] and base["target"]
                                       and set(range(*base["target"])) & set(gold_t))
        for lv in LEVELS[1:]:
            row["levels"][lv] = st.pivot_link(pv13, lv)
        gold_rows.append(row)

    # --- fixtures: fixed by full member set at each level ---
    fix_rows = []
    for f in fixtures:
        n = f["n"]
        rv = reverify[n]
        st = state(f["pair"])
        pv13 = rv["pivot_v13_order"]
        gold_t = rv["correct_target_v13"]
        is_range = len(gold_t) > 3  # n41, n93: gold is a range, not a member set
        base = st.pivot_link(pv13, 2.2)
        row = {"n": n, "pair": f["pair"], "pivot_v13": pv13, "gold_target_v13": gold_t[:2] + ["..."] if is_range else gold_t,
               "gold_is_range": is_range, "baseline": base, "levels": {}}
        for lv in LEVELS[1:]:
            lk = st.pivot_link(pv13, lv)
            fixed = bool(lk and lk["accepted"] and lk["target"] and not is_range
                         and set(range(*lk["target"])) == set(gold_t))
            partial = bool(lk and lk["accepted"] and lk["target"] and set(range(*lk["target"])) & set(gold_t))
            row["levels"][lv] = {"link": lk, "fixed_full_member_set": fixed, "partial_overlap": partial}
        fix_rows.append(row)

    # --- aggregate per level: regressions, fixes, changed links ---
    summary = {"levels": {}}
    for lv in LEVELS[1:]:
        regressions = [r["n"] for r in gold_rows if r["baseline_correct"]
                       and not (r["levels"][lv] and r["levels"][lv]["accepted"] and r["levels"][lv]["target"]
                                and set(range(*r["levels"][lv]["target"])) & set(r["gold_target_v13"]))]
        gold_changed = [r["n"] for r in gold_rows
                        if (r["baseline"] or {}).get("target") != (r["levels"][lv] or {}).get("target")]
        fixes = [r["n"] for r in fix_rows if r["levels"][lv]["fixed_full_member_set"]]
        fix_changed = [r["n"] for r in fix_rows if (r["baseline"] or {}).get("target") != (r["levels"][lv]["link"] or {}).get("target")]
        n74row = next(r for r in gold_rows if r["n"] == 74)
        summary["levels"][lv] = {
            "fixtures_fixed_full_member_set": fixes,
            "gold_regressions": regressions, "gold_links_changed": gold_changed,
            "fixture_links_changed": fix_changed,
            "n74_baseline": n74row["baseline"], "n74_level": n74row["levels"][lv],
        }
    con.close()
    Path(args.out).write_text(json.dumps({"summary": summary, "golds": gold_rows, "fixtures": fix_rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    base_ok = sum(1 for r in gold_rows if r["n"] != 74 and r["baseline_correct"])
    print(f"baseline: {base_ok}/{len([g for g in golds if g['n']!=74])} '正确' golds accepted at correct target (my DP baseline).")
    for lv in LEVELS[1:]:
        s = summary["levels"][lv]
        print(f"\n== gap penalty {lv} (flagged apparatus/OCR only) ==")
        print(f"  fixtures fixed (full member set): {s['fixtures_fixed_full_member_set']}")
        print(f"  fixture links changed: {s['fixture_links_changed']}")
        print(f"  60-gold regressions: {s['gold_regressions']}  (gold links changed at all: {s['gold_links_changed']})")
        print(f"  n74: baseline={s['n74_baseline']} -> level={s['n74_level']}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--sample", required=True)
    p.add_argument("--reverify", required=True)
    p.add_argument("--out", required=True)
    run(p.parse_args())
