"""Extract the 4 disputed R9 links (JA->ZH) with full neighbor context, UTF-8 out.

Usage:
    python scripts/extract_disputed_alignment.py [experiment_index.sqlite3] [out.json]

Reads the read-only v22-trial experiment copy and dumps each disputed link's JA/ZH
members plus neighbor windows, so the bilingual evidence behind
reports/e5-disputed-four-adjudication-2026-09-09.md can be regenerated on demand.
The dumped JSON contains verbatim corpus text; keep it local, do not commit it.
"""
import json
import sqlite3
import sys
from pathlib import Path

DB = sys.argv[1] if len(sys.argv) > 1 else r"D:\ME_Finder\.codex-tmp\alignment-v22-trial\runtime\data\index.sqlite3"
RUN = "alignment-run-c615cd37a2d24099a3c8c6447488a878"  # v22 completed, pdf ja -> pdf zh
JA_SET = "segment-set-293d950d56ee0867db5e526b"
ZH_SET = "segment-set-70a6dcce7d3ab4b47e800d9f"
DISPUTED = [483, 487, 549, 551]
OUT = sys.argv[2] if len(sys.argv) > 2 else "disputed_context.json"

c = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
c.row_factory = sqlite3.Row


def seg_text(set_id, oi):
    r = c.execute("SELECT text_raw FROM text_segments WHERE segment_set_id=? AND order_index=?",
                  (set_id, oi)).fetchone()
    return r["text_raw"] if r else None


def seg_oi(segment_id):
    r = c.execute("SELECT segment_set_id, order_index FROM text_segments WHERE segment_id=?",
                  (segment_id,)).fetchone()
    return (r["segment_set_id"], r["order_index"]) if r else (None, None)


# Map: for the run, which link contains a given JA order_index?
links = {}
for lk in c.execute("SELECT alignment_link_id, order_index, confidence, review_status, cost, anchor_key "
                    "FROM alignment_links WHERE alignment_run_id=?", (RUN,)):
    links[lk["alignment_link_id"]] = dict(lk)

# members grouped by link
mem = {}
for m in c.execute(
        "SELECT lm.alignment_link_id, lm.side, lm.segment_id, lm.member_order "
        "FROM alignment_link_members lm JOIN alignment_links l "
        "ON l.alignment_link_id=lm.alignment_link_id WHERE l.alignment_run_id=?", (RUN,)):
    d = mem.setdefault(m["alignment_link_id"], {"pivot": [], "target": []})
    set_id, oi = seg_oi(m["segment_id"])
    side = "pivot" if set_id == JA_SET else "target"
    d[side].append((m["member_order"], oi, seg_text(set_id, oi)))

# index: JA order_index -> link_id
ja_to_link = {}
for lid, sides in mem.items():
    for _, oi, _ in sides["pivot"]:
        ja_to_link[oi] = lid

out = []
for ja_oi in DISPUTED:
    lid = ja_to_link.get(ja_oi)
    entry = {"ja_order_index": ja_oi, "link_id": lid}
    if lid:
        entry["link"] = links[lid]
        s = mem[lid]
        entry["ja_members"] = [{"oi": oi, "text": t} for _, oi, t in sorted(s["pivot"])]
        entry["zh_members"] = [{"oi": oi, "text": t} for _, oi, t in sorted(s["target"])]
        zh_ois = [oi for _, oi, _ in s["target"] if oi is not None]
        lo = (min(zh_ois) - 4) if zh_ois else 0
        hi = (max(zh_ois) + 5) if zh_ois else 0
        entry["zh_window"] = [{"oi": i, "text": seg_text(ZH_SET, i)} for i in range(lo, hi)
                              if seg_text(ZH_SET, i) is not None]
    # JA neighbors for context
    entry["ja_context"] = [{"oi": i, "text": seg_text(JA_SET, i)}
                           for i in range(ja_oi - 1, ja_oi + 3) if seg_text(JA_SET, i) is not None]
    out.append(entry)

Path(OUT).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("wrote", OUT)
print("JA total segs:", c.execute("SELECT count(*) FROM text_segments WHERE segment_set_id=?", (JA_SET,)).fetchone()[0])
print("ZH total segs:", c.execute("SELECT count(*) FROM text_segments WHERE segment_set_id=?", (ZH_SET,)).fetchone()[0])
c.close()
