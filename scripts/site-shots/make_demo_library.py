"""Build an isolated landing-page demo library.

Copies three documents out of the live library into tmp/site-demo/incoming with
clean display names (the Z-Library / 1lib watermark in the original filenames
must not reach a public web page), then leaves the actual import to the running
headless server so the real parsing pipeline is what produces the screenshots.

usage: python make_demo_library.py [--corpus <data root>/runtime/corpus] [--incoming tmp/site-demo/incoming]
"""

import argparse
import shutil
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--corpus", default="dist/MEFinderData/runtime/corpus")
parser.add_argument("--incoming", default="tmp/site-demo/incoming")
args = parser.parse_args()
CORPUS = Path(args.corpus)
INCOMING = Path(args.incoming)
INCOMING.mkdir(parents=True, exist_ok=True)

PICKS = [
    ("raw_pdf/法哲学原理*.pdf", "法哲学原理：或自然法和国家学纲要.pdf"),
    ("raw_docx/谁在害怕性别*.epub", "谁在害怕性别.epub"),
    ("raw_docx/Whos Afraid of Gender*.epub", "Who's Afraid of Gender.epub"),
]

manifest = []
for pattern, clean_name in PICKS:
    matches = sorted(CORPUS.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one match for {pattern!r}, got {len(matches)}")
    src = matches[0]
    dst = INCOMING / clean_name
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        manifest.append((clean_name, src.stat().st_size, "already-copied"))
        continue
    shutil.copy2(src, dst)
    same = dst.stat().st_size == src.stat().st_size
    manifest.append((clean_name, dst.stat().st_size, "copied" if same else "SIZE-MISMATCH"))

for name, size, state in manifest:
    print(f"{state:14} {size:>12,}  {name}")
