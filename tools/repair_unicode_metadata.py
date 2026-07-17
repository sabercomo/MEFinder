"""Restore one source's metadata from a trusted UTF-8 import config.

This utility deliberately accepts an ASCII source id instead of bibliographic
values on the command line, avoiding Windows console code-page conversion.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.me_finder.bibliographic_metadata import (
    METADATA_FIELDS,
    canonical_metadata,
    invalid_metadata_fields,
    metadata_missing_fields,
    update_metadata_in_database,
)
from src.me_finder.pdf_import_service import load_import_config, save_import_config


def _document(config: dict[str, object], source_id: str) -> dict[str, object]:
    documents = config.get("documents") or []
    match = next(
        (item for item in documents if isinstance(item, dict) and item.get("source_file_id") == source_id),
        None,
    )
    if match is None:
        raise ValueError(f"Source id not found in config: {source_id}")
    return match


def repair(source_config: Path, runtime_root: Path, source_id: str) -> dict[str, object]:
    trusted_config = load_import_config(source_config)
    trusted_document = _document(trusted_config, source_id)
    metadata = canonical_metadata(trusted_document)
    invalid = invalid_metadata_fields(metadata)
    if invalid:
        raise ValueError("Trusted metadata is invalid: " + ", ".join(invalid))
    if metadata_missing_fields(metadata):
        raise ValueError("Trusted metadata is incomplete; refusing automatic recovery.")

    runtime_config_path = runtime_root / "config" / "pdf_imports.json"
    runtime_config = load_import_config(runtime_config_path)
    runtime_document = _document(runtime_config, source_id)
    metadata["metadata_status"] = "complete"
    metadata["metadata_source"] = "manual"
    metadata["metadata_confidence"] = 1.0
    metadata["metadata_missing_fields"] = []
    for field in METADATA_FIELDS:
        runtime_document[field] = metadata.get(field)
    for field in (
        "document_type",
        "metadata_status",
        "metadata_source",
        "metadata_confidence",
        "metadata_evidence",
        "metadata_missing_fields",
    ):
        runtime_document[field] = metadata.get(field)
    runtime_document["publication_year"] = metadata.get("publish_year")
    runtime_document["bibliographic_metadata"] = metadata
    save_import_config(runtime_config_path, runtime_config)

    counts = update_metadata_in_database(runtime_root / "data" / "index.sqlite3", source_id, metadata)
    verified = canonical_metadata(_document(load_import_config(runtime_config_path), source_id))
    if invalid_metadata_fields(verified) or verified.get("title") != metadata.get("title"):
        raise RuntimeError("Runtime metadata verification failed after save.")
    return {
        "source_file_id": source_id,
        "title_codepoints": [ord(ch) for ch in str(verified.get("title") or "")],
        "updated": counts,
        "database": str(runtime_root / "data" / "index.sqlite3"),
        "config": str(runtime_config_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    args = parser.parse_args()
    print(json.dumps(repair(args.source_config, args.runtime_root, args.source_id), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
