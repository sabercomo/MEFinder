"""Compute alignment_body_bounds for the latest segment set of every source (read-only).

Usage: python scripts/body_range_bounds_diff.py <index.sqlite3> <out.json>
Run before and after a region-detection change and diff the two outputs.
"""
import json, sqlite3, sys
sys.path.insert(0, 'src')
from me_finder.alignment_regions import alignment_body_bounds
db = sys.argv[1]
c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
out = {}
for sid, name in c.execute("select source_file_id, file_name from source_files order by source_file_id"):
    row = c.execute("select segment_set_id from segment_sets where source_file_id=? order by created_at desc, rowid desc limit 1", (sid,)).fetchone()
    if not row:
        continue
    texts = [r[0] for r in c.execute("select text_raw from text_segments where segment_set_id=? order by order_index", (row[0],))]
    out[sid] = {"n": len(texts), "bounds": list(alignment_body_bounds(texts))}
json.dump(out, open(sys.argv[2], 'w'), ensure_ascii=False, indent=1)
print(len(out))
