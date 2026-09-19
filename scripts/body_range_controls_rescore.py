"""Regenerate the Baudrillard pairs on a CLONED snapshot and rescore D-round2 controls.

Usage: python scripts/body_range_controls_rescore.py <dir with index.sqlite3 + cache/>
Writes into the clone only; never point it at the production library.
"""
import json, sqlite3, sys, time
from collections import Counter
from pathlib import Path
sys.path.insert(0, '.')
from src.me_finder.text_alignment import generate_alignment
E2E = Path(sys.argv[1]); DB = E2E / 'index.sqlite3'; CACHE = E2E / 'cache'
MODEL = 'multilingual-e5-large'
base = json.load(open('.codex-tmp/d-round2/baseline-2026-09-14.json'))
controls = base['controls']['rows']
pairs = sorted({r['pair'] for r in controls if 'pdf-import-3eedba8ff70fc8e0' in r['pair']})
out = {'pairs': {}, 'controls': []}
def links(con, run_id):
    rows = con.execute("SELECT l.alignment_link_id, l.review_status, m.side, s.order_index FROM alignment_links l "
        "JOIN alignment_link_members m ON m.alignment_link_id=l.alignment_link_id "
        "JOIN text_segments s ON s.segment_id=m.segment_id WHERE l.alignment_run_id=?", (run_id,)).fetchall()
    by = {}
    for lid, st, side, o in rows:
        e = by.setdefault(lid, {'status': st, 'pivot': [], 'target': []}); e[side].append(o)
    return list(by.values())
for pair in pairs:
    pivot, target = pair.split(' -> ')
    con = sqlite3.connect(DB)
    group, old_run, old_params = con.execute("SELECT document_group_id, alignment_run_id, parameters_json FROM alignment_runs WHERE pivot_source_file_id=? AND target_source_file_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (pivot, target)).fetchone()
    old = links(con, old_run); con.close()
    t = time.monotonic()
    res = generate_alignment(DB, group, pivot, target, force=True, model_cache_dir=CACHE, embedding_model_id=MODEL)
    con = sqlite3.connect(DB)
    new_params = json.loads(con.execute("SELECT parameters_json FROM alignment_runs WHERE alignment_run_id=?", (res['alignment_run_id'],)).fetchone()[0])
    new = links(con, res['alignment_run_id'])
    out['pairs'][pair] = {'seconds': round(time.monotonic() - t, 1),
        'body_ranges_before': json.loads(old_params)['body_ranges'], 'body_ranges_after': new_params['body_ranges'],
        'status_before': Counter(l['status'] for l in old), 'status_after': Counter(l['status'] for l in new)}
    for r in controls:
        if r['pair'] != pair: continue
        hit = next((l for l in new if r['pivot_v13'] in l['pivot']), None)
        tgt = sorted(hit['target']) if hit else None
        out['controls'].append({'n': r['n'], 'pair': pair, 'gold': r['gold_target_v13'], 'before': [r['baseline_target'], r['baseline_status'], r['baseline_correct_accepted']],
            'after': [tgt, hit and hit['status'], bool(hit and hit['status'] == 'automatic' and tgt == sorted(r['gold_target_v13']))]})
    con.close()
    print(pair, out['pairs'][pair], flush=True)
json.dump(out, open(E2E / 'result.json', 'w'), ensure_ascii=False, indent=1)
for c in out['controls']: print(c)
