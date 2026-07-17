"""Re-run MinerU and atomically replace one runtime PDF source in SQLite."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.me_finder.database import replace_source_in_database
from src.me_finder.pdf_extractors import extract_pdf_source
from src.me_finder.pdf_import_service import load_import_config, parse_pdf_with_mineru


def restore(runtime_root: Path, source_id: str) -> dict[str, object]:
    runtime_root = runtime_root.resolve()
    os.chdir(runtime_root)
    config_path = runtime_root / "config" / "pdf_imports.json"
    config = load_import_config(config_path)
    document = next(
        (
            item
            for item in config.get("documents", [])
            if isinstance(item, dict) and item.get("source_file_id") == source_id
        ),
        None,
    )
    if document is None:
        raise ValueError(f"Source id not found: {source_id}")
    pdf_path = runtime_root / "corpus" / "raw_pdf" / str(document.get("file_name") or "")
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    progress_log: list[dict[str, object]] = []

    def progress(update: dict[str, object]) -> None:
        progress_log.append(dict(update))
        print(json.dumps(update, ensure_ascii=True), flush=True)

    mineru_config = document.get("mineru") if isinstance(document.get("mineru"), dict) else {}
    manifest_ref = str(mineru_config.get("manifest") or "")
    manifest_path = (runtime_root / manifest_ref).resolve() if manifest_ref else None
    if manifest_path is not None and manifest_path.exists():
        mineru = {"manifest_path": str(manifest_path), "status": "existing_manifest"}
    else:
        mineru = parse_pdf_with_mineru(runtime_root, pdf_path, source_id, on_progress=progress)
    config = load_import_config(config_path)
    document = next(item for item in config["documents"] if item.get("source_file_id") == source_id)
    extracted = extract_pdf_source(
        pdf_path,
        runtime_root,
        document,
        parsed_dir=runtime_root / "corpus" / "parsed" / "pdf",
    )
    source = extracted["source_files"][0]
    profile = source.get("pdf_profile") or {}
    if profile.get("detected_pdf_type") != "mineru_structured":
        raise RuntimeError("MinerU result was downloaded but not selected as the structured parser.")
    database = replace_source_in_database(
        extracted,
        runtime_root / "data" / "index.sqlite3",
        backup_existing=True,
    )
    return {
        "source_file_id": source_id,
        "mineru": mineru,
        "detected_pdf_type": profile.get("detected_pdf_type"),
        "parser": "mineru",
        "pdf_page_count": profile.get("pdf_page_count"),
        "database": database,
        "progress_updates": len(progress_log),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    args = parser.parse_args()
    print(json.dumps(restore(args.runtime_root, args.source_id), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
