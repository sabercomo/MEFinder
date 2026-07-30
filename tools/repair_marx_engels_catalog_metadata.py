"""Repair catalog metadata damaged by scanner-internal PDF properties.

The affected Marx/Engels PDFs have useful UTF-8 file names such as
``马恩全集第50卷.pdf`` but scanner metadata such as ``K93.pdf``/``kdc``.
This tool treats the local file name as the trusted title, keeps unrelated
bibliographic fields, synchronizes the SQLite search payloads, and creates a
recoverable import-config backup before applying changes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.me_finder.bibliographic_metadata import (  # noqa: E402
    METADATA_FIELDS,
    canonical_metadata,
    marx_engels_first_edition_metadata,
    metadata_missing_fields,
    update_metadata_in_database,
)
from src.me_finder.pdf_import_service import load_import_config, save_import_config  # noqa: E402


TRUSTED_AUTHOR = "马克思、恩格斯"
REPAIR_FIELDS = ("title", "author", "publisher", "publish_place", "publish_year", "volume")


def _is_target(document: dict[str, object]) -> bool:
    return bool(marx_engels_first_edition_metadata(document.get("file_name")))


def _repair_metadata(document: dict[str, object]) -> dict[str, object]:
    metadata = canonical_metadata(document.get("bibliographic_metadata") or document)
    file_name = str(document.get("file_name") or "").strip()
    defaults = marx_engels_first_edition_metadata(file_name)
    if not defaults:
        raise ValueError(f"Not a supported Marx/Engels first-edition file: {file_name}")
    metadata.update(defaults)
    evidence = dict(metadata.get("metadata_evidence") or {})
    for field in REPAIR_FIELDS:
        evidence[field] = {
            "source": "collection_rule",
            "source_page": None,
            "evidence_text": file_name,
            "rule": "marx_engels_chinese_first_edition",
            "confidence": 1.0,
        }
    metadata["metadata_evidence"] = evidence
    metadata["metadata_source"] = "automatic_recognition"
    metadata["metadata_confidence"] = max(float(metadata.get("metadata_confidence") or 0.0), 1.0)
    metadata["metadata_conflicts"] = [
        conflict
        for conflict in list(metadata.get("metadata_conflicts") or [])
        if not isinstance(conflict, dict) or conflict.get("field") not in set(REPAIR_FIELDS)
    ]
    missing = metadata_missing_fields(metadata)
    metadata["metadata_missing_fields"] = missing
    metadata["metadata_status"] = "partial" if missing else "complete"
    return metadata


def _write_document_metadata(document: dict[str, object], metadata: dict[str, object]) -> None:
    for field in METADATA_FIELDS:
        document[field] = metadata.get(field)
    for field in (
        "document_type",
        "metadata_status",
        "metadata_source",
        "metadata_confidence",
        "metadata_evidence",
        "metadata_conflicts",
        "metadata_missing_fields",
    ):
        document[field] = metadata.get(field)
    document["publication_year"] = metadata.get("publish_year")
    document["bibliographic_metadata"] = metadata


def repair(runtime_root: Path, *, apply: bool = False) -> dict[str, object]:
    runtime_root = runtime_root.resolve()
    config_path = runtime_root / "config" / "pdf_imports.json"
    database_path = runtime_root / "data" / "index.sqlite3"
    config = load_import_config(config_path)
    targets = [
        document
        for document in config.get("documents", [])
        if isinstance(document, dict) and _is_target(document)
    ]
    changes: list[tuple[dict[str, object], dict[str, object]]] = []
    for document in targets:
        metadata = _repair_metadata(document)
        current = canonical_metadata(document.get("bibliographic_metadata") or document)
        if any(current.get(field) != metadata.get(field) for field in REPAIR_FIELDS):
            changes.append((document, metadata))

    report: dict[str, object] = {
        "runtime_root": str(runtime_root),
        "target_documents": len(targets),
        "changed_documents": len(changes),
        "title_changes": sum(
            canonical_metadata(document.get("bibliographic_metadata") or document).get("title")
            != metadata["title"]
            for document, metadata in changes
        ),
        "author_changes": sum(
            canonical_metadata(document.get("bibliographic_metadata") or document).get("author")
            != metadata["author"]
            for document, metadata in changes
        ),
        "publisher_changes": sum(
            canonical_metadata(document.get("bibliographic_metadata") or document).get("publisher")
            != metadata["publisher"]
            for document, metadata in changes
        ),
        "publish_place_changes": sum(
            canonical_metadata(document.get("bibliographic_metadata") or document).get("publish_place")
            != metadata["publish_place"]
            for document, metadata in changes
        ),
        "publish_year_changes": sum(
            canonical_metadata(document.get("bibliographic_metadata") or document).get("publish_year")
            != metadata["publish_year"]
            for document, metadata in changes
        ),
        "applied": False,
    }
    if not apply or not changes:
        return report

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = config_path.with_name(f"pdf_imports.pre-marx-engels-repair-{stamp}.json")
    shutil.copy2(config_path, backup_path)
    totals = {"sources": 0, "volumes": 0, "works": 0, "paragraphs": 0}
    for document, metadata in changes:
        source_id = str(document.get("source_file_id") or "")
        if not source_id:
            raise ValueError(f"Missing source_file_id for {document.get('file_name')}")
        counts = update_metadata_in_database(database_path, source_id, metadata)
        for key, value in counts.items():
            totals[key] += int(value)
        _write_document_metadata(document, metadata)
    save_import_config(config_path, config)

    verified = load_import_config(config_path)
    verified_targets = [
        document
        for document in verified.get("documents", [])
        if isinstance(document, dict) and _is_target(document)
    ]
    if any(
        any(
            canonical_metadata(document.get("bibliographic_metadata") or document).get(field)
            != marx_engels_first_edition_metadata(document.get("file_name")).get(field)
            for field in REPAIR_FIELDS
        )
        for document in verified_targets
    ):
        raise RuntimeError("Catalog metadata verification failed after repair.")
    report.update(
        {
            "applied": True,
            "backup": str(backup_path),
            "database_updates": totals,
            "verified_documents": len(verified_targets),
        }
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair(args.runtime_root, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
