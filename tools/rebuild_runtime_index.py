"""Rebuild the packaged desktop app's mutable runtime index."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.me_finder.pdf_import_service import rebuild_local_index


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", "")) / "MEFinder" / "runtime",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    os.chdir(root)
    index = rebuild_local_index(root)
    metadata = index.get("metadata", {})
    print(f"sources={metadata.get('source_count')} paragraphs={metadata.get('paragraph_count')}")


if __name__ == "__main__":
    main()
