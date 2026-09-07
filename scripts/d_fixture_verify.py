"""Re-verify issue #18's 11 golden fixtures against a rebuilt v13/v21 copy.

Locates each fixture's pivot segment by its *original v12 order* (mapped to v13
so recurring short phrases are not mislocated), reads the new v21 link, and
compares its target against the manually-established correct target — both
projected into v13 coordinates.  Emits a JSON evidence record.

Run scripts.d_fixture_reverify for all six pairs first, then:

    python -m scripts.d_fixture_verify \
        --db .codex-tmp/d-experiment/index-v21.sqlite3 \
        --sample reports/note-channel-random-sample-2026-09-06.json \
        --out reports/d-fixture-reverify-2026-09-07.json
"""
import argparse
import difflib
import json
from pathlib import Path
import sqlite3
import sys

# Correct target, in the coordinate system named per fixture.  "v12" = the
# order in the frozen v12 segment set (note-channel table); "v13" = already in
# the rebuilt set (n93's §203 corridor was reported in v13 order by issue #17).
CORRECT = {
    11: {"coords": "v12", "orders": list(range(3162, 3166))},
    24: {"coords": "v12", "orders": [3535, 3536]},
    28: {"coords": "v12", "orders": [1193]},
    41: {"coords": "v12", "orders": list(range(6503, 6509))},
    53: {"coords": "v12", "orders": [562, 563, 564]},
    60: {"coords": "v12", "orders": [3075]},
    80: {"coords": "v12", "orders": [1879, 1880]},
    82: {"coords": "v12", "orders": [4511]},
    88: {"coords": None, "orders": []},  # 双生命题, allow-unfixable
    93: {"coords": "v13", "orders": list(range(4605, 4641))},  # §202..§204 corridor
    98: {"coords": "v12", "orders": [3115, 3116]},
}
FIXTURES = [11, 24, 28, 41, 53, 60, 80, 82, 88, 93, 98]


def _texts(con, set_id):
    return [r[0] for r in con.execute(
        "SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index",
        (set_id,))]


def _v12_set(con, src):
    row = con.execute(
        "SELECT segment_set_id FROM segment_sets WHERE source_file_id=? AND segmenter_version='12'",
        (src,)).fetchone()
    return row[0] if row else None


def _latest_v21(con, pv, tg):
    return con.execute(
        "SELECT alignment_run_id, pivot_segment_set_id, target_segment_set_id "
        "FROM alignment_runs WHERE pivot_source_file_id=? AND target_source_file_id=? "
        "AND algorithm_version='21' AND status='completed' ORDER BY created_at DESC LIMIT 1",
        (pv, tg)).fetchone()


def _order_map(old, new):
    if old == new:
        return {i: i for i in range(len(old))}
    mapping = {}
    for tag, i1, i2, j1, _ in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                mapping[i1 + k] = j1 + k
    return mapping


def _link_for(con, run_id, set_id, order):
    seg = con.execute(
        "SELECT segment_id FROM text_segments WHERE segment_set_id=? AND order_index=?",
        (set_id, order)).fetchone()
    if seg is None:
        return None, []
    link = con.execute(
        "SELECT l.review_status, l.confidence, l.alignment_link_id FROM alignment_links l "
        "JOIN alignment_link_members m ON m.alignment_link_id=l.alignment_link_id "
        "WHERE l.alignment_run_id=? AND m.segment_id=? AND m.side='pivot'",
        (run_id, seg[0])).fetchone()
    if link is None:
        return {"status": "unmatched", "confidence": None}, []
    targets = con.execute(
        "SELECT t.order_index, t.text_raw FROM alignment_link_members m "
        "JOIN text_segments t ON t.segment_id=m.segment_id "
        "WHERE m.alignment_link_id=? AND m.side='target' ORDER BY t.order_index",
        (link[2],)).fetchall()
    return {"status": link[0], "confidence": link[1]}, [(t[0], t[1]) for t in targets]


def verify(args):
    con = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sample = {x["n"]: x for x in json.loads(Path(args.sample).read_text(encoding="utf-8"))["samples"]}
    records = []
    for n in FIXTURES:
        x = sample[n]
        pv, tg = x["pair"].split(" -> ")
        run = _latest_v21(con, pv, tg)
        pset, tset = run["pivot_segment_set_id"], run["target_segment_set_id"]
        v12_pset, v12_tset = _v12_set(con, pv), _v12_set(con, tg)
        pmap = _order_map(_texts(con, v12_pset), _texts(con, pset))
        tmap = _order_map(_texts(con, v12_tset), _texts(con, tset))
        v12_pord = x["pivot"][0][0]
        v13_pord = pmap.get(v12_pord, v12_pord)
        link, targets = _link_for(con, run["alignment_run_id"], pset, v13_pord)
        # Correct target range in v13 coordinates.
        c = CORRECT[n]
        if c["coords"] == "v12":
            correct_v13 = sorted({tmap.get(o) for o in c["orders"] if tmap.get(o) is not None})
        elif c["coords"] == "v13":
            correct_v13 = c["orders"]
        else:
            correct_v13 = []
        new_orders = [o for o, _ in targets]
        overlap = bool(set(new_orders) & set(correct_v13))
        records.append({
            "n": n, "pair": f"{pv} -> {tg}", "label_v20": x["label"],
            "pivot_v12_order": v12_pord, "pivot_v13_order": v13_pord,
            "pivot_text": x["pivot"][0][1],
            "old_wrong_target_v12": [t[0] for t in x["target"]],
            "new_v21_status": link["status"],
            "new_v21_confidence": link["confidence"],
            "new_v21_targets": [{"order": o, "text": t} for o, t in targets],
            "correct_target_v13": correct_v13,
            "new_overlaps_correct": overlap,
        })
    con.close()
    Path(args.out).write_text(json.dumps({"fixtures": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in records:
        tgt = r["new_v21_targets"]
        head = tgt[0]["text"][:48].replace("\n", " ") if tgt else ""
        print(f"n{r['n']:>2} {r['label_v20']:<4} new={r['new_v21_status']:<9} "
              f"conf={r['new_v21_confidence']!s:<8} T{[t['order'] for t in tgt]} "
              f"correct_v13={r['correct_target_v13'][:4]}{'..' if len(r['correct_target_v13'])>4 else ''} "
              f"OVERLAP={'YES' if r['new_overlaps_correct'] else 'no'}  {head!r}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--sample", required=True)
    p.add_argument("--out", required=True)
    verify(p.parse_args())
