"""Build the local searchable index."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from . import __version__
from .database import (
    ANCHOR_SPEC_VERSION,
    DATABASE_SCHEMA_VERSION,
    DEFAULT_DATABASE_PATH,
    build_database,
    load_database_index,
)
from .extractors import (
    extract_source,
    is_marx_engels_volume_name,
    volume_number_from_name,
)
from .pdf_extractors import extract_configured_pdfs


DEFAULT_CORPUS_DIR = Path("corpus/raw_docx")
DEFAULT_PDF_CORPUS_DIR = Path("corpus/raw_pdf")
DEFAULT_PDF_CONFIG_PATH = Path("config/pdf_imports.json")
DEFAULT_PARSED_PDF_DIR = Path("corpus/parsed/pdf")
DEFAULT_INDEX_PATH = Path("data/index.json")

_FATAL_PDF_AUDIT_TYPES = frozenset({"pdf_missing", "pdf_import_failed"})
SourceInventory = Dict[str, Dict[str, Dict[str, int]]]


class IncompleteIndexBuildError(RuntimeError):
    """Raised before publication when active or configured sources are incomplete."""

    def __init__(self, audit_issues: List[Dict[str, object]]) -> None:
        self.audit_issues = [dict(issue) for issue in audit_issues]
        details = "; ".join(
            "{}{}: {}".format(
                str(issue.get("issue_type") or "pdf_error"),
                f"[{issue.get('source_file_id')}]" if issue.get("source_file_id") else "",
                str(issue.get("message") or "PDF 解析失败"),
            )
            for issue in self.audit_issues
        )
        source_label = (
            "索引"
            if any(
                str(issue.get("issue_type") or "").startswith("word_")
                for issue in self.audit_issues
            )
            else "PDF 索引"
        )
        super().__init__(
            f"{source_label}构建不完整；为避免发布残缺索引，"
            f"数据库未更新或创建。{details}"
        )


def _fatal_pdf_audit_issues(
    extracted: Dict[str, List[Dict[str, object]]],
) -> List[Dict[str, object]]:
    """Return source-level failures that make a full rebuild incomplete."""

    issues = extracted.get("audit_issues", [])
    return [
        issue
        for issue in issues
        if isinstance(issue, dict)
        and (
            str(issue.get("issue_type") or "") in _FATAL_PDF_AUDIT_TYPES
            or str(issue.get("severity") or "").lower() == "error"
        )
    ]


def _existing_source_inventory(
    database_path: Path,
) -> tuple[SourceInventory, Dict[str, object] | None]:
    """Read active source identities and content counts in one read-only query."""

    path = Path(database_path)
    if not path.is_file():
        return {"word": {}, "pdf": {}}, None
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=1.0,
        )
        try:
            rows = connection.execute(
                """
                WITH searchable_paragraphs AS (
                    SELECT source_file_id, COUNT(*) AS item_count
                    FROM paragraphs
                    WHERE eligible_for_search = 1
                    GROUP BY source_file_id
                ), pdf_page_counts AS (
                    SELECT source_file_id, COUNT(*) AS item_count
                    FROM pdf_pages
                    GROUP BY source_file_id
                )
                SELECT
                    source.source_file_id,
                    source.source_type,
                    COALESCE(searchable.item_count, 0),
                    COALESCE(pages.item_count, 0)
                FROM source_files AS source
                LEFT JOIN searchable_paragraphs AS searchable
                    ON searchable.source_file_id = source.source_file_id
                LEFT JOIN pdf_page_counts AS pages
                    ON pages.source_file_id = source.source_file_id
                WHERE source.source_type IN ('word', 'pdf')
                """
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {"word": {}, "pdf": {}}, {
            "severity": "error",
            "issue_type": "active_index_unreadable",
            "message": f"活动索引无法读取，拒绝覆盖：{exc}",
        }
    inventory: SourceInventory = {"word": {}, "pdf": {}}
    for source_file_id, source_type, searchable_count, pdf_page_count in rows:
        source_id = str(source_file_id or "")
        source_kind = str(source_type or "")
        if source_id and source_kind in inventory:
            inventory[source_kind][source_id] = {
                "searchable_paragraph_count": int(searchable_count or 0),
                "pdf_page_count": int(pdf_page_count or 0),
            }
    return inventory, None


def _word_publication_issues(
    extracted_sources: List[Dict[str, object]],
    existing_source_ids: set[str],
) -> List[Dict[str, object]]:
    """Reject a rebuild that omits any Word source still in the active index."""

    extracted_ids = {
        str(item.get("source_file_id") or "")
        for item in extracted_sources
        if isinstance(item, dict)
        and str(item.get("source_type") or "word") == "word"
        and str(item.get("source_file_id") or "")
    }
    missing_ids = existing_source_ids.difference(extracted_ids)
    if not missing_ids:
        return []
    return [
        {
            "severity": "error",
            "issue_type": "word_source_set_incomplete",
            "message": (
                "Word 提取结果缺少活动索引中的文献："
                + ", ".join(sorted(missing_ids))
            ),
        }
    ]


def _enabled_pdf_configs(
    config_path: Path,
) -> tuple[List[Dict[str, object]], Dict[str, object] | None]:
    """Load enabled documents while preserving missing/invalid-state evidence."""

    path = Path(config_path)
    if not path.is_file():
        return [], {
            "severity": "error",
            "issue_type": "pdf_config_missing",
            "message": f"PDF 导入配置不存在：{path}",
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], {
            "severity": "error",
            "issue_type": "pdf_config_invalid",
            "message": f"PDF 导入配置无法读取：{exc}",
        }
    documents = raw if isinstance(raw, list) else raw.get("documents", []) if isinstance(raw, dict) else None
    if not isinstance(documents, list):
        return [], {
            "severity": "error",
            "issue_type": "pdf_config_invalid",
            "message": "PDF 导入配置中的 documents 必须是列表。",
        }
    return [
        item
        for item in documents
        if isinstance(item, dict) and item.get("enabled", True)
    ], None


def _pdf_publication_issues(
    *,
    configured: List[Dict[str, object]],
    extracted: Dict[str, List[Dict[str, object]]],
    existing_source_ids: set[str],
    existing_inventory: Dict[str, Dict[str, int]],
    pdf_limit: int | None,
    config_issue: Dict[str, object] | None,
) -> List[Dict[str, object]]:
    """Validate that a full rebuild cannot silently shrink the active catalog."""

    issues = _fatal_pdf_audit_issues(extracted)
    if config_issue is not None:
        # A missing optional config is harmless on a genuinely new Word-only
        # build.  Invalid content is never safe, and a previous PDF catalog
        # proves that a missing file would be destructive.
        if existing_source_ids or config_issue.get("issue_type") != "pdf_config_missing":
            issues.append(config_issue)
        return issues

    if existing_source_ids and not configured:
        issues.append(
            {
                "severity": "error",
                "issue_type": "pdf_config_empty",
                "message": "PDF 配置已为空，但活动索引仍包含 PDF 文献。",
            }
        )
        return issues

    configured_id_values = [
        str(item.get("source_file_id") or "").strip() for item in configured
    ]
    configured_ids = {source_id for source_id in configured_id_values if source_id}
    duplicate_ids = {
        source_id
        for source_id in configured_ids
        if configured_id_values.count(source_id) > 1
    }
    if duplicate_ids or (existing_source_ids and "" in configured_id_values):
        issues.append(
            {
                "severity": "error",
                "issue_type": "pdf_config_identity_invalid",
                "message": (
                    "活动 PDF 配置中存在缺失或重复的 source_file_id"
                    + (
                        f"：{', '.join(sorted(duplicate_ids))}"
                        if duplicate_ids
                        else "。"
                    )
                ),
            }
        )
    elif len(configured_ids) == len(configured):
        missing_from_config = existing_source_ids.difference(configured_ids)
        if missing_from_config:
            issues.append(
                {
                    "severity": "error",
                    "issue_type": "pdf_config_source_set_mismatch",
                    "message": (
                        "PDF 配置缺少活动索引中的文献："
                        + ", ".join(sorted(missing_from_config))
                    ),
                }
            )

    selected = configured
    if pdf_limit is not None:
        selected = configured[: max(0, int(pdf_limit))]
        if existing_source_ids and len(selected) < len(configured):
            issues.append(
                {
                    "severity": "error",
                    "issue_type": "pdf_partial_build",
                    "message": "pdf_limit 只能用于新建测试索引，不能覆盖活动资料库。",
                }
            )

    extracted_sources = [
        item
        for item in extracted.get("source_files", [])
        if isinstance(item, dict)
    ]
    extracted_ids = {
        str(item.get("source_file_id") or "")
        for item in extracted_sources
        if str(item.get("source_file_id") or "")
    }
    expected_ids = {
        str(item.get("source_file_id") or "")
        for item in selected
        if str(item.get("source_file_id") or "")
    }
    incomplete = len(extracted_sources) != len(selected)
    if len(expected_ids) == len(selected):
        incomplete = incomplete or extracted_ids != expected_ids
    if incomplete:
        missing_ids = expected_ids.difference(extracted_ids)
        issues.append(
            {
                "severity": "error",
                "issue_type": "pdf_source_set_incomplete",
                "message": (
                    "PDF 提取结果与启用配置不一致"
                    + (f"，缺少：{', '.join(sorted(missing_ids))}" if missing_ids else "。")
                ),
            }
        )

    searchable_counts: Dict[str, int] = {}
    for paragraph in extracted.get("paragraphs", []):
        if not isinstance(paragraph, dict) or not paragraph.get(
            "eligible_for_search"
        ):
            continue
        source_id = str(paragraph.get("source_file_id") or "")
        if source_id:
            searchable_counts[source_id] = searchable_counts.get(source_id, 0) + 1
    pdf_page_counts: Dict[str, int] = {}
    for page in extracted.get("pdf_pages", []):
        if not isinstance(page, dict):
            continue
        source_id = str(page.get("source_file_id") or "")
        if source_id:
            pdf_page_counts[source_id] = pdf_page_counts.get(source_id, 0) + 1

    # Identity preservation alone is not enough: an OCR/parser regression can
    # still return the source row while silently replacing all searchable
    # content with an empty result.  Compare only identities present in both
    # inventories so the existing source-set error remains the primary signal.
    for source_id in sorted(existing_source_ids.intersection(extracted_ids)):
        previous = existing_inventory.get(source_id, {})
        previous_searchable = int(
            previous.get("searchable_paragraph_count", 0)
        )
        previous_pages = int(previous.get("pdf_page_count", 0))
        extracted_searchable = searchable_counts.get(source_id, 0)
        extracted_pages = pdf_page_counts.get(source_id, 0)
        emptied: List[str] = []
        if previous_searchable > 0 and extracted_searchable == 0:
            emptied.append("可搜索段落")
        if previous_pages > 0 and extracted_pages == 0:
            emptied.append("PDF 页")
        if not emptied:
            continue
        issues.append(
            {
                "severity": "error",
                "issue_type": "pdf_source_content_incomplete",
                "source_file_id": source_id,
                "message": (
                    "活动 PDF 文献的已索引内容在本轮提取中变为空："
                    + "、".join(emptied)
                ),
                "previous_searchable_paragraph_count": previous_searchable,
                "extracted_searchable_paragraph_count": extracted_searchable,
                "previous_pdf_page_count": previous_pages,
                "extracted_pdf_page_count": extracted_pages,
            }
        )
    return issues


def word_source_sort_key(path: Path) -> tuple[int, int, str]:
    """Sort canonical numbered volumes first and standalone documents by name."""

    try:
        if not is_marx_engels_volume_name(path.name):
            raise ValueError
        return (0, volume_number_from_name(path.name), path.name.casefold())
    except ValueError:
        return (1, 0, path.name.casefold())


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
    root: Path | None = None,
) -> Dict[str, object]:
    # Packaged builds keep their data outside the working directory, so callers
    # that know the data root pass it in; the CLI keeps using the current one.
    root = Path(root).resolve() if root is not None else Path(".").resolve()
    corpus_dir = Path(corpus_dir)
    index_path = Path(index_path)
    existing_inventory, existing_database_issue = _existing_source_inventory(
        Path(database_path)
    )
    if existing_database_issue is not None:
        # Do not parse the Word corpus or spend paid PDF parser/API quota when
        # publication is already forbidden by an unreadable live database.
        raise IncompleteIndexBuildError([existing_database_issue])
    existing_word_source_ids = set(existing_inventory["word"])
    existing_pdf_source_ids = set(existing_inventory["pdf"])
    if existing_pdf_source_ids and not include_pdf:
        raise IncompleteIndexBuildError(
            [
                {
                    "severity": "error",
                    "issue_type": "pdf_sources_excluded",
                    "message": (
                        "活动索引包含 PDF 文献，必须显式启用 PDF 完整重建："
                        + ", ".join(sorted(existing_pdf_source_ids))
                    ),
                }
            ]
        )
    files = sorted(
        [p for p in corpus_dir.iterdir() if p.is_file() and p.suffix.lower() in {".docx", ".doc"}],
        key=word_source_sort_key,
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
    word_publication_issues = _word_publication_issues(
        source_files, existing_word_source_ids
    )
    if word_publication_issues:
        # Missing Word originals are known before PDF extraction; fail without
        # consuming parser/API quota or touching either publication artifact.
        raise IncompleteIndexBuildError(word_publication_issues)
    if include_pdf:
        configured_pdfs, pdf_config_issue = _enabled_pdf_configs(
            Path(pdf_config_path)
        )
        pdf_extracted = extract_configured_pdfs(
            root=root,
            pdf_corpus_dir=Path(pdf_corpus_dir),
            config_path=Path(pdf_config_path),
            parsed_dir=Path(parsed_pdf_dir),
            limit=pdf_limit,
        )
        fatal_pdf_issues = _pdf_publication_issues(
            configured=configured_pdfs,
            extracted=pdf_extracted,
            existing_source_ids=existing_pdf_source_ids,
            existing_inventory=existing_inventory["pdf"],
            pdf_limit=pdf_limit,
            config_issue=pdf_config_issue,
        )
        if fatal_pdf_issues:
            # extract_configured_pdfs deliberately records per-source failures so
            # all configured PDFs can be inspected in one pass.  A full rebuild,
            # however, must not turn those partial results into the active DB.
            raise IncompleteIndexBuildError(fatal_pdf_issues)
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
            "schema_version": DATABASE_SCHEMA_VERSION,
            # Version of the paragraph-to-source anchor contract.  Consumers
            # still inspect each paragraph because an index may contain a mix
            # of records imported before and after this specification.
            "anchor_spec_version": ANCHOR_SPEC_VERSION,
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
