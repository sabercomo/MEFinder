"""Create a translation work group in the demo library and run real alignment.

Reads the two Butler EPUB ids straight out of the isolated demo database, forms
the group, starts the background alignment run, and polls until it settles.

usage: python run_alignment.py [--db tmp/site-demo/root/data/index.sqlite3] [--api http://127.0.0.1:8766]
"""

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--db", default="tmp/site-demo/root/data/index.sqlite3")
parser.add_argument("--api", default="http://127.0.0.1:8766")
args = parser.parse_args()
DB = Path(args.db)
API = args.api.rstrip("/")


def post(path, body, timeout=90):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def get(path, timeout=60):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.load(r)


conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select source_file_id, file_name from source_files where file_name like '%害怕性别%' "
    "or file_name like '%Afraid%'"
).fetchall()
conn.close()

picked = {r["file_name"]: r["source_file_id"] for r in rows}
print("library:", json.dumps(picked, ensure_ascii=False))

cn = next(v for k, v in picked.items() if "害怕性别" in k)
en = next(v for k, v in picked.items() if "Afraid" in k)

code, group = post("/api/document-groups/combine", {
    "title": "谁在害怕性别 / Who's Afraid of Gender?",
    "source_file_ids": [en, cn],
    "base_source_file_id": en,
})
print("combine:", code, json.dumps(group, ensure_ascii=False)[:400])
gid = (group.get("result") or {}).get("document_group_id") or group.get("document_group_id")
if not gid:
    raise SystemExit("no group id")

code, started = post("/api/text-alignments/start", {
    "document_group_id": gid,
    "pivot_source_file_id": en,
    "target_source_file_id": cn,
})
print("start:", code, json.dumps(started, ensure_ascii=False)[:200])

for i in range(120):
    time.sleep(5)
    try:
        st = get("/api/text-alignments/status?document_group_id=" + gid)
    except Exception as exc:
        print("poll err", exc)
        continue
    text = json.dumps(st, ensure_ascii=False)
    print(f"[{i * 5:4d}s]", text[:260])
    low = text.lower()
    if '"running"' not in low and '"pending"' not in low and '"queued"' not in low:
        break
(DB.parent.parent.parent / "align.json").write_text(
    json.dumps({"group": gid, "cn": cn, "en": en}, ensure_ascii=False), encoding="utf-8"
)
