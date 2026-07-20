"""Create the blank SQLite index bundled with the public portable release."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from src.me_finder import __version__
from src.me_finder.database import build_database


def create_empty_index(target: Path) -> None:
    index = {
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": __version__,
            "schema_version": 2,
            "source_count": 0,
            "paragraph_count": 0,
            "eligible_paragraph_count": 0,
        },
        "source_files": [],
        "volumes": [],
        "works": [],
        "toc_entries": [],
        "paragraphs": [],
        "page_anchors": [],
        "pdf_pages": [],
        "pdf_page_mappings": [],
        "pdf_import_runs": [],
        "audit_issues": [],
    }
    build_database(index, Path(target), backup_existing=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    create_empty_index(args.target)
    print(args.target)


if __name__ == "__main__":
    main()
