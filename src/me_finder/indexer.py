"""Build the local searchable index."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from . import __version__
from .database import DEFAULT_DATABASE_PATH, build_database, load_database_index
from .extractors import extract_source, volume_number_from_name
from .pdf_extractors import extract_configured_pdfs


DEFAULT_CORPUS_DIR = Path("corpus/raw_docx")
DEFAULT_PDF_CORPUS_DIR = Path("corpus/raw_pdf")
DEFAULT_PDF_CONFIG_PATH = Path("config/pdf_imports.json")
DEFAULT_PARSED_PDF_DIR = Path("corpus/parsed/pdf")
DEFAULT_INDEX_PATH = Path("data/index.json")


def build_index(
    corpus_dir: Path = DEFAULT_CORPUS_DIR,
    index_path: Path = DEFAULT_INDEX_PATH,
    *,
    include_pdf: bool = False,
    pdf_corpus_dir: Path = DEFAULT_PDF_CORPUS_DIR,
    pdf_config_path: Path = DEFAULT_PDF_CONFIG_PATH,
    parsed_pdf_dir: Path = DEFAULT_PARSED_PDF_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
    pdf_limit: int | None = None,
    backup_existing: bool = False,
    export_json: bool = False,
) -> Dict[str, object]:
    root = Path(".").resolve()
    corpus_dir = Path(corpus_dir)
    index_path = Path(index_path)
    files = sorted(
        [p for p in corpus_dir.iterdir() if p.is_file() and p.suffix.lower() in {".docx", ".doc"}],
        key=lambda p: volume_number_from_name(p.name),
    )
    source_files: List[Dict[str, object]] = []
    volumes: List[Dict[str, object]] = []
    works: List[Dict[str, object]] = []
    toc_entries: List[Dict[str, object]] = []
    paragraphs: List[Dict[str, object]] = []
    page_anchors: List[Dict[str, object]] = []
    audit_issues: List[Dict[str, object]] = []
    pdf_pages: List[Dict[str, object]] = []
    pdf_page_mappings: List[Dict[str, object]] = []
    pdf_import_runs: List[Dict[str, object]] = []
    for path in files:
        extracted = extract_source(path, root)
        mark_word_records(extracted)
        source_files.append(extracted["source_file"])
        volumes.append(extracted["volume"])
        works.extend(extracted["works"])
        toc_entries.extend(extracted["toc_entries"])
        paragraphs.extend(extracted["paragraphs"])
        page_anchors.extend(extracted["page_anchors"])
        audit_issues.extend(extracted["audit_issues"])
    if include_pdf:
        pdf_extracted = extract_configured_pdfs(
            root=root,
            pdf_corpus_dir=Path(pdf_corpus_dir),
            config_path=Path(pdf_config_path),
            parsed_dir=Path(parsed_pdf_dir),
            limit=pdf_limit,
        )
        source_files.extend(pdf_extracted["source_files"])
        volumes.extend(pdf_extracted["volumes"])
        works.extend(pdf_extracted["works"])
        paragraphs.extend(pdf_extracted["paragraphs"])
        pdf_pages.extend(pdf_extracted["pdf_pages"])
        pdf_page_mappings.extend(pdf_extracted["pdf_page_mappings"])
        pdf_import_runs.extend(pdf_extracted["pdf_import_runs"])
        audit_issues.extend(pdf_extracted["audit_issues"])
    index = {
        "metadata": {
            "app": "ME_Finder",
            "version": __version__,
            "schema_version": 2,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "corpus_dir": str(corpus_dir).replace("\\", "/"),
            "corpus_dirs": {
                "word": str(corpus_dir).replace("\\", "/"),
                "pdf": str(Path(pdf_corpus_dir)).replace("\\", "/"),
            },
            "supported_source_types": ["word", "pdf"],
            "include_pdf": include_pdf,
            "source_count": len(source_files),
            "paragraph_count": len(paragraphs),
            "eligible_paragraph_count": sum(1 for p in paragraphs if p.get("eligible_for_search")),
            "notes": [
                "第1卷 DOCX 页码为分节推断，尚未人工验证。",
                "第2-10卷 DOC 页码为目录范围约束，非段落级精确页码。",
            ],
        },
        "source_files": source_files,
        "volumes": volumes,
        "works": works,
        "toc_entries": toc_entries,
        "paragraphs": paragraphs,
        "page_anchors": page_anchors,
        "pdf_pages": pdf_pages,
        "pdf_page_mappings": pdf_page_mappings,
        "pdf_import_runs": pdf_import_runs,
        "audit_issues": audit_issues,
    }
    # SQLite 是唯一权威索引；JSON 仅作离线备份，默认不再随每次重建
    # 全量重写（300MB）。需要时用 export_json=True（CLI 的 --export-json）。
    if export_json:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_existing and index_path.exists():
            backup_index(index_path)
        with index_path.open("w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)
    build_database(index, Path(database_path), backup_existing=backup_existing)
    return index


def load_index(index_path: Path = DEFAULT_INDEX_PATH) -> Dict[str, object]:
    if Path(index_path).suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        return load_database_index(Path(index_path))
    return json.loads(Path(index_path).read_text(encoding="utf-8"))


def mark_word_records(extracted: Dict[str, object]) -> None:
    source = extracted.get("source_file", {})
    if isinstance(source, dict):
        source.setdefault("source_type", "word")
        source.setdefault("open_source_url", f"/source/{source.get('source_file_id')}")
    for volume in extracted.get("volumes", []):
        if isinstance(volume, dict):
            volume.setdefault("source_type", "word")
    volume = extracted.get("volume")
    if isinstance(volume, dict):
        volume.setdefault("source_type", "word")
    for work in extracted.get("works", []):
        if isinstance(work, dict):
            work.setdefault("source_type", "word")
    for paragraph in extracted.get("paragraphs", []):
        if isinstance(paragraph, dict):
            paragraph.setdefault("source_type", "word")


def backup_index(index_path: Path) -> Path:
    backup_dir = index_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = backup_dir / f"{index_path.stem}-{stamp}{index_path.suffix}"
    shutil.copy2(index_path, backup_path)
    return backup_path
