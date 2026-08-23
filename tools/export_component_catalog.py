"""Validate and export the independently published component catalog."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.me_finder.component_catalog import validate_component_catalog
from src.me_finder.local_ocr_installer import (
    LOCAL_OCR_MANIFEST_FILE,
    load_local_ocr_installer_manifest,
)
from src.me_finder.managed_mineru import load_managed_mineru_manifest


OUTPUT_NAME = "mefinder-components-v1.json"
PLATFORMS = (
    "darwin-arm64",
    "darwin-x86_64",
    "win32-x86_64",
    "linux-x86_64",
)


def export_catalog(output_dir: Path) -> tuple[Path, Path]:
    payload = json.loads(LOCAL_OCR_MANIFEST_FILE.read_text(encoding="utf-8"))
    validate_component_catalog(payload)
    for platform_key in PLATFORMS:
        load_local_ocr_installer_manifest(
            LOCAL_OCR_MANIFEST_FILE,
            platform_key=platform_key,
        )
        load_managed_mineru_manifest(
            LOCAL_OCR_MANIFEST_FILE,
            platform_key=platform_key,
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / OUTPUT_NAME
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.write_bytes(raw)
    checksum = output.with_suffix(output.suffix + ".sha256.txt")
    checksum.write_text(
        f"{hashlib.sha256(raw).hexdigest()}  {output.name}\n",
        encoding="utf-8",
    )
    return output, checksum


if __name__ == "__main__":
    catalog, digest = export_catalog(Path("release/component-catalog"))
    print(catalog)
    print(digest)
