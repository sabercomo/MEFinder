"""Deterministic bibliographic metadata extraction from PDF front matter."""

from __future__ import annotations

import re
import json
import sqlite3
import unicodedata
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .collection_metadata import infer_collection_metadata
from .bibliographic_journal import (
    _JOURNAL_MAX_FALLBACK_PAGES,
    _extract_journal_article,
    _extract_journal_name,
    _has_book_only_markers,
    looks_like_journal_article as looks_like_journal_article,
    normalize_doi as normalize_doi,
    normalize_issn as normalize_issn,
)
from .bibliographic_marx_engels import (
    marx_engels_collection_metadata as marx_engels_collection_metadata,
    marx_engels_first_edition_metadata as marx_engels_first_edition_metadata,
    marx_engels_second_edition_metadata as marx_engels_second_edition_metadata,
)
from .bibliographic_thesis import (
    _extract_thesis_metadata,
    _looks_like_thesis,
    _strip_thesis_author_prefix,
)
from .bibliographic_values import (
    DOCUMENT_TYPES as DOCUMENT_TYPES,
    INVALID_PLACEHOLDERS as INVALID_PLACEHOLDERS,
    INVALID_PUBLICATION_PLACES,
    KNOWN_PUBLISHERS,
    MANUAL_LANGUAGE_CODES as MANUAL_LANGUAGE_CODES,
    METADATA_FIELDS as METADATA_FIELDS,
    MIN_AUTO_CONFIDENCE as MIN_AUTO_CONFIDENCE,
    PUBLISHER_ALIASES,
    PUBLISHER_PLACES,
    RESPONSIBILITY_STATUSES as RESPONSIBILITY_STATUSES,
    _CHINESE_NAME_CHARS,
    _CHINESE_PUBLISHER_SUFFIX,
    _ENGLISH_NAME_CHARS,
    _MetadataCandidate,
    _add_candidate,
    _candidate_group_score,
    _candidate_values_are_compatible,
    _canonical_metadata,
    _clean_people,
    _clean_publisher,
    _compact_value_key,
    _has_suspicious_person_punctuation,
    _is_plausible_person_name,
    _json,
    _repair_english_title_casing,
    canonical_metadata as canonical_metadata,
    is_valid_bibliographic_value as is_valid_bibliographic_value,
)
from .persistence.paragraph_payload import paragraph_payload_for_storage












def invalid_metadata_fields(metadata: Mapping[str, object]) -> List[str]:
    return [
        field
        for field in METADATA_FIELDS
        if metadata.get(field) not in (None, "")
        and (
            not is_valid_bibliographic_value(metadata.get(field))
            or (field in {"author", "translator"} and _has_suspicious_person_punctuation(metadata.get(field)))
        )
    ]










def _looks_like_pdf_file_label(value: object) -> bool:
    """Return true for an embedded PDF title that is only a file name.

    Scanner-produced books often carry an internal title such as ``K93.pdf``
    even after the actual file has been renamed to a useful catalog title.
    Such a value is provenance, not bibliographic metadata.
    """

    text = str(value or "").strip()
    return bool(text and Path(text).name == text and Path(text).suffix.casefold() == ".pdf")


def detect_pdf_bibliographic_metadata(
    path: Path,
    pages: Sequence[Mapping[str, object]],
    existing: Optional[Mapping[str, object]] = None,
    *,
    force: bool = False,
    scan_pages: int = 20,
    tail_pages: int = 8,
) -> Dict[str, object]:
    """Detect front-matter metadata while preserving user-maintained values."""

    path = Path(path)
    existing = dict(existing or {})
    if existing.get("metadata_source") == "manual" and not force:
        return _canonical_metadata(existing)
    result = _canonical_metadata(existing)
    evidence: Dict[str, object] = dict(existing.get("metadata_evidence") or {})
    confidence: Dict[str, object] = {}
    rejected_evidence: Dict[str, object] = {}

    # Windows 为同名下载件追加的 (1)(1) 不是书名的一部分。只在旧标题
    # 与实际文件名完全相同时修复，不触碰人工编辑过的书名。
    file_stem = path.stem.strip()
    repaired_file_title = re.sub(r"(?:\s*\(\d+\))+\s*$", "", file_stem).strip()
    if (
        result.get("title")
        and str(result["title"]).strip() == file_stem
        and repaired_file_title != file_stem
        and is_valid_bibliographic_value(repaired_file_title)
    ):
        result["title"] = repaired_file_title
        evidence["title"] = {
            "source": "file_name_repair",
            "source_page": None,
            "evidence_text": path.name,
            "rule": "windows_duplicate_suffix",
            "confidence": 0.99,
        }
        confidence["title"] = 0.99

    collection_defaults, collection_rule = marx_engels_collection_metadata(path.name)
    for field, value in collection_defaults.items():
        if field == "document_type":
            result[field] = value
            continue
        result[field] = value
        evidence[field] = {
            "source": "collection_rule",
            "source_page": None,
            "evidence_text": path.name,
            "rule": collection_rule,
            "confidence": 1.0,
        }
        confidence[field] = 1.0

    if _looks_like_pdf_file_label(result.get("title")):
        rejected_evidence["title"] = {
            "source": "existing_metadata",
            "evidence_text": result.get("title"),
            "reason": "pdf_file_name_is_not_a_title",
        }
        fallback_title = repaired_file_title
        result["title"] = fallback_title if is_valid_bibliographic_value(fallback_title) else None
        if result.get("title"):
            evidence["title"] = {
                "source": "file_name",
                "source_page": None,
                "evidence_text": path.name,
                "rule": "reject_embedded_pdf_file_name",
                "confidence": 0.99,
            }
            confidence["title"] = 0.99

    # Import configuration and old automatic results may contain a lossy PDF
    # metadata name such as "乔纳森?克拉里".  Do not let that block a clean
    # title-page or copyright-page candidate.
    for field in ("author", "translator"):
        person_value = result.get(field)
        if person_value and (
            _has_suspicious_person_punctuation(person_value)
            or not _is_plausible_person_name(str(person_value))
        ):
            rejected_evidence[field] = {
                "source": "existing_metadata",
                "evidence_text": person_value,
                "reason": (
                    "suspicious_person_punctuation"
                    if _has_suspicious_person_punctuation(person_value)
                    else "artifact_person_name"
                ),
            }
            result[field] = None

    embedded = _embedded_pdf_metadata(path)
    for field in ("title", "author"):
        embedded_value = embedded.get(field)
        if field == "title" and _looks_like_pdf_file_label(embedded_value):
            rejected_evidence[field] = {
                "source": "pdf_metadata",
                "evidence_text": embedded_value,
                "reason": "pdf_file_name_is_not_a_title",
            }
            continue
        if field == "author" and _has_suspicious_person_punctuation(embedded_value):
            rejected_evidence[field] = {
                "source": "pdf_metadata",
                "evidence_text": embedded_value,
                "reason": "suspicious_person_punctuation",
            }
            continue
        if field == "author" and embedded_value and not _is_plausible_person_name(str(embedded_value)):
            rejected_evidence[field] = {
                "source": "pdf_metadata",
                "evidence_text": embedded_value,
                "reason": "artifact_author_name",
            }
            continue
        replace_filename_title = (
            field == "title"
            and embedded_value
            and result.get(field)
            and path.exists()
            and str(result.get(field)).strip() == path.stem.strip()
        )
        if (not result.get(field) or replace_filename_title) and embedded_value:
            result[field] = embedded_value
            evidence[field] = {"source": "pdf_metadata", "source_page": None, "evidence_text": embedded_value}
            confidence[field] = 0.72

    page_indexes: List[int] = []
    for page in pages:
        try:
            page_indexes.append(int(page.get("pdf_page_index") or 0))
        except (TypeError, ValueError):
            continue
    total_pages = max(page_indexes) + 1 if page_indexes else 0
    # 中文书的版权页常在书末（图书在版编目 + 版次/定价），因此除前置页外
    # 也扫描末尾几页。
    tail_start = max(scan_pages, total_pages - tail_pages)
    texts: List[Tuple[int, str]] = []
    for page in pages:
        try:
            page_idx = int(page.get("pdf_page_index") or 0)
        except (TypeError, ValueError):
            continue
        if page_idx >= scan_pages and page_idx < tail_start:
            continue
        text = str(page.get("text_raw") or "").strip()
        if text:
            texts.append((page_idx, text))

    # 期刊单篇与专著的版式完全不同：专著的版权页/CIP 规则用在期刊首页上只会
    # 把院系、基金项目当成出版社。两条抽取链路互斥。
    is_thesis = _looks_like_thesis(texts)
    has_book_markers = _has_book_only_markers(texts)
    is_journal_by_marker = any(
        page_idx < 2 and looks_like_journal_article(text) for page_idx, text in texts
    )
    # 只有带明确 GB/T 标记的期刊才走期刊提取器；页数兜底和学位论文仍走温和的
    # 前置页提取，避免从糊掉的老扫描页里抽出垃圾作者。兜底只影响类型标签，
    # 且在提取完成、掌握出版社等证据后再判定（见下方 document_type 决策）。
    if is_thesis:
        detected, detected_evidence, detected_confidence = _extract_thesis_metadata(texts)
        conflicts = []
    elif is_journal_by_marker:
        detected, detected_evidence, detected_confidence = _extract_journal_article(
            texts, Path(path).stem
        )
        conflicts = []
    else:
        detected, detected_evidence, detected_confidence, conflicts = _extract_from_front_matter(texts)
    # 文件名里的作者/标题/年份对期刊和专著同样有效：两条路径都用它补空字段，
    # 绝不能因为判成期刊就丢掉文件名里的作者。
    filename_values, filename_evidence, filename_confidence = _extract_explicit_filename_metadata(Path(path).stem)
    for field, value in filename_values.items():
        if not detected.get(field) or filename_confidence[field] > detected_confidence.get(field, 0.0):
            detected[field] = value
            detected_evidence[field] = filename_evidence[field]
            detected_confidence[field] = filename_confidence[field]
    filename_translators = _translator_parts_from_filename(Path(path).stem)
    if detected.get("translator") and filename_translators:
        reconciled_translator = _reconcile_fused_people(
            str(detected["translator"]),
            filename_translators,
        )
        if reconciled_translator:
            detected["translator"] = reconciled_translator
            translator_evidence = dict(detected_evidence.get("translator") or {})
            translator_evidence.update(
                {
                    "rule": "front_matter_with_filename_name_boundaries",
                    "filename_boundary_evidence": "，".join(filename_translators),
                }
            )
            detected_evidence["translator"] = translator_evidence
            detected_confidence["translator"] = min(
                0.99,
                max(0.96, float(detected_confidence.get("translator") or 0.0)),
            )
    for field, value in detected.items():
        current_evidence = evidence.get(field) if isinstance(evidence.get(field), Mapping) else {}
        current_source = str(current_evidence.get("source") or "")
        # 检测值与既有值只是大小写/标点差异时视为同一值：保留既有写法
        # （通常大小写更规范），只补充证据。
        same_value_modulo_case = (
            is_valid_bibliographic_value(result.get(field))
            and _compact_value_key(str(result.get(field))) == _compact_value_key(str(value))
        )
        should_replace = (
            (force and current_source != "collection_rule")
            or not is_valid_bibliographic_value(result.get(field))
            or current_source == "pdf_metadata"
            or (field in {"author", "translator"} and _has_suspicious_person_punctuation(result.get(field)))
            # 既有值没有经过人工确认（manual 已在入口提前返回）：来自导入配置
            # 或旧的自动识别。版权页/CIP 级别的高置信度检测应当覆盖它们，
            # 否则错误的初始配置永远无法被自动识别纠正。
            or (
                not same_value_modulo_case
                and current_source not in {"file_name", "file_name_repair", "collection_rule"}
                and detected_confidence.get(field, 0.0) >= 0.95
            )
        )
        if (value == result.get(field) or same_value_modulo_case) and not current_evidence:
            evidence[field] = detected_evidence[field]
            confidence[field] = detected_confidence[field]
            continue
        if same_value_modulo_case:
            continue
        if is_valid_bibliographic_value(value) and should_replace:
            result[field] = value
            field_evidence = dict(detected_evidence[field])
            if field in rejected_evidence:
                field_evidence["rejected_evidence"] = rejected_evidence[field]
            evidence[field] = field_evidence
            confidence[field] = detected_confidence[field]

    if not result.get("publish_place") and result.get("publisher") in PUBLISHER_PLACES:
        publisher = str(result["publisher"])
        result["publish_place"] = PUBLISHER_PLACES[publisher]
        evidence["publish_place"] = {
            "source": "inferred_from_publisher",
            "source_page": (evidence.get("publisher") or {}).get("source_page") if isinstance(evidence.get("publisher"), dict) else None,
            "evidence_text": publisher,
            "confidence": "inferred_from_publisher",
        }
        confidence["publish_place"] = 0.62

    # 兜底：没有专著版权页/CIP 标记、且总页数不超过阈值的 PDF 视为单篇论文。
    # 用于救回老期刊、访谈等首页缺 GB/T 标记的文章。不依赖出版社字段——期刊
    # 引文里常出现被引专著的出版社，据此判书会把大量论文误判成专著。兜底只改
    # 类型标签，不改提取路径。
    is_journal_by_size = (
        not has_book_markers and 0 < total_pages <= _JOURNAL_MAX_FALLBACK_PAGES
    )
    if is_thesis:
        result["document_type"] = "thesis"
        for field in (
            "country", "translator", "publish_place", "isbn",
            "journal_name", "volume", "issue", "page_range", "doi", "issn",
        ):
            result[field] = None
            evidence.pop(field, None)
        # 学位论文文件名常写成「作者 - 篇名」（如「金芳冰 - 拉埃尔·耶吉…研究」）。
        # 当篇名以作者名加分隔符开头时，作者已单列，篇名不应再重复带上作者前缀。
        _strip_thesis_author_prefix(result, evidence)
    elif is_journal_by_marker or is_journal_by_size:
        result["document_type"] = "journal_article"
    elif result.get("translator"):
        result["document_type"] = "translated_book"
    else:
        result.setdefault("document_type", "book")
    # 期刊论文（学位论文除外）若还没有刊名，尝试从首页报头版式认出刊名；认不出
    # 就保持缺失，交给文件名/人工，绝不猜。
    if (
        result.get("document_type") == "journal_article"
        and not is_thesis
        and not is_valid_bibliographic_value(result.get("journal_name"))
    ):
        journal_name, journal_evidence, journal_rule = _extract_journal_name(texts)
        if journal_name:
            result["journal_name"] = journal_name
            evidence["journal_name"] = {
                "source": "masthead",
                "source_page": 1,
                "evidence_text": journal_evidence,
                "rule": journal_rule,
            }
            confidence["journal_name"] = 0.82 if journal_rule == "masthead_suffix_line" else 0.9
    if result.get("document_type") in {"book", "translated_book"} and not is_valid_bibliographic_value(result.get("author")):
        collection = infer_collection_metadata(result.get("title"), path.stem)
        if collection.get("author"):
            result["author"] = collection["author"]
            result["responsibility_status"] = "present"
            evidence["author"] = {
                "source": "collection_title_rule",
                "source_page": None,
                "evidence_text": result.get("title") or path.name,
                "rule": "personal_collection_creator",
                "confidence": 0.93,
            }
            confidence["author"] = 0.93
    missing = metadata_missing_fields(result)
    invalid = invalid_metadata_fields(result)
    if invalid:
        status = "recognition_failed"
        conflicts.extend({"field": field, "reason": "invalid_value"} for field in invalid)
    elif conflicts:
        status = "needs_review"
    elif missing:
        status = "partial" if any(result.get(field) for field in METADATA_FIELDS) else "missing"
    else:
        status = "complete"
    result.update(
        {
            "metadata_status": status,
            "metadata_source": "automatic_recognition",
            "metadata_confidence": round(sum(confidence.values()) / len(confidence), 4) if confidence else 0.0,
            "metadata_evidence": evidence,
            "metadata_conflicts": conflicts,
            "metadata_missing_fields": missing,
        }
    )
    return result


def metadata_missing_fields(metadata: Mapping[str, object]) -> List[str]:
    doc_type = str(metadata.get("document_type") or "")
    if doc_type == "thesis":
        # 学位论文用 publisher 承载学位授予学校；出版地、刊名和期号均不适用。
        required = ["author", "title", "publisher", "publish_year"]
    elif doc_type == "journal_article":
        # 期刊论文不需要出版社/出版地；卷次和起止页可选。
        required = ["author", "title", "journal_name", "publish_year", "issue"]
    else:
        required = ["author", "title", "publisher", "publish_place", "publish_year"]
        if doc_type == "translated_book":
            required.insert(2, "translator")
    return [
        field
        for field in required
        if not (
            field == "author"
            and responsibility_is_absent_or_unknown(metadata)
        )
        and not is_valid_bibliographic_value(metadata.get(field))
    ]


def responsibility_is_absent_or_unknown(metadata: Mapping[str, object]) -> bool:
    status = str(metadata.get("responsibility_status") or "").strip().lower()
    return status in {"none", "unknown"}


def manual_metadata(payload: Mapping[str, object], previous: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    previous_metadata = _canonical_metadata(previous or {})
    result = dict(previous_metadata)
    invalid = invalid_metadata_fields(payload)
    if invalid:
        raise ValueError("以下书目字段包含无效问号或不可用文本：" + "、".join(invalid))
    for field in METADATA_FIELDS:
        value = str(payload.get(field) or "").strip()
        if field == "doi" and value:
            normalized = normalize_doi(value)
            if not normalized:
                raise ValueError("DOI 格式无效。")
            value = normalized
        elif field == "issn" and value:
            normalized = normalize_issn(value)
            if not normalized:
                raise ValueError("ISSN 格式或校验位无效。")
            value = normalized
        result[field] = value or None
    requested_type = str(payload.get("document_type") or "").strip()
    if requested_type in DOCUMENT_TYPES:
        result["document_type"] = requested_type
    elif requested_type:
        raise ValueError(f"未知文献类型：{requested_type}")
    else:
        result["document_type"] = "translated_book" if result.get("translator") else "book"
    # 人工语言覆盖：正文键存在且非空→校验并记录；键存在且为空→清除（回到自动识别）；
    # 键缺席→保留 previous（已在 result 里，见下方 _canonical_metadata 承载）。
    if "language" in payload:
        requested_language = str(payload.get("language") or "").strip()
        if requested_language:
            if requested_language not in MANUAL_LANGUAGE_CODES:
                raise ValueError(f"未知语言代码：{requested_language}")
            result["language_code_manual"] = requested_language
        else:
            result.pop("language_code_manual", None)
    requested_responsibility = str(payload.get("responsibility_status") or "").strip().lower()
    if requested_responsibility:
        if requested_responsibility not in RESPONSIBILITY_STATUSES:
            raise ValueError("未知责任者状态")
        result["responsibility_status"] = requested_responsibility
    elif result.get("author"):
        result["responsibility_status"] = "present"
    if result["document_type"] == "thesis":
        for field in (
            "country", "translator", "publish_place", "isbn",
            "journal_name", "volume", "issue", "page_range", "doi", "issn",
        ):
            result[field] = None
    missing = metadata_missing_fields(result)
    result["metadata_status"] = "complete" if not missing else "partial"
    result["metadata_source"] = "manual"
    result["metadata_confidence"] = 1.0
    result["metadata_missing_fields"] = missing
    # Evidence follows the exact value it justified.  Manual edits invalidate
    # stale automatic evidence, while a user-confirmed CNKI candidate may pass
    # narrowly validated evidence carrying the matching value.
    evidence = dict(previous_metadata.get("metadata_evidence") or {})
    for field in METADATA_FIELDS:
        if result.get(field) != previous_metadata.get(field):
            evidence.pop(field, None)
    supplied_evidence = payload.get("metadata_evidence")
    if isinstance(supplied_evidence, Mapping):
        for field, raw_item in supplied_evidence.items():
            if field not in METADATA_FIELDS or not isinstance(raw_item, Mapping):
                continue
            source = str(raw_item.get("source") or "")
            value = str(raw_item.get("value") or "").strip()
            if source not in {"cnki_lookup", "cnki_search_result", "cnki_citation", "google_books", "crossref", "k10plus"}:
                continue
            if not value or value != str(result.get(field) or "").strip():
                continue
            item = {
                "source": source,
                "source_page": None,
                "evidence_text": str(raw_item.get("evidence_text") or value)[:500],
                "value": value,
            }
            record_url = str(raw_item.get("record_url") or "").strip()
            if (
                record_url.startswith("https://oversea.cnki.net/")
                or record_url.startswith("https://books.google.com/")
                or record_url.startswith("https://play.google.com/")
                or record_url.startswith("https://doi.org/")
            ):
                item["record_url"] = record_url[:4096]
            evidence[field] = item
    result["metadata_evidence"] = evidence
    return result


def update_metadata_in_database(database_path: Path, source_file_id: str, metadata: Mapping[str, object]) -> Dict[str, int]:
    """Update one document's catalog/search metadata without rebuilding text indexes."""

    canonical = _canonical_metadata(metadata)
    connection = sqlite3.connect(str(database_path))
    counts = {"sources": 0, "volumes": 0, "works": 0, "paragraphs": 0}
    try:
        connection.execute("BEGIN IMMEDIATE")
        source_row = connection.execute(
            "SELECT payload_json FROM source_files WHERE source_file_id = ?", (source_file_id,)
        ).fetchone()
        if not source_row:
            raise ValueError("文献不存在。")
        source = json.loads(source_row[0])
        source["bibliographic_metadata"] = canonical
        for key, value in canonical.items():
            if value not in (None, ""):
                source[key] = value
            elif key in METADATA_FIELDS:
                source.pop(key, None)
        connection.execute(
            "UPDATE source_files SET payload_json = ? WHERE source_file_id = ?",
            (_json(source), source_file_id),
        )
        counts["sources"] = 1

        title = str(canonical.get("title") or source.get("display_title") or "")
        author = canonical.get("author")
        year = canonical.get("publish_year")
        for row_id, payload_json in connection.execute(
            "SELECT rowid, payload_json FROM volumes WHERE source_file_id = ?", (source_file_id,)
        ).fetchall():
            volume = json.loads(payload_json)
            if title:
                volume["display_title"] = title
            connection.execute(
                "UPDATE volumes SET display_title = ?, payload_json = ? WHERE rowid = ?",
                (title or volume.get("display_title"), _json(volume), row_id),
            )
            counts["volumes"] += 1
        for row_id, payload_json in connection.execute(
            "SELECT rowid, payload_json FROM works WHERE payload_json LIKE ?", (f'%"source_file_id":"{source_file_id}"%',)
        ).fetchall():
            work = json.loads(payload_json)
            if title:
                work["title"] = title
                work["document_title"] = title
            work["author_label"] = author
            work["date_label"] = year
            connection.execute(
                "UPDATE works SET title = ?, payload_json = ? WHERE rowid = ?",
                (title or work.get("title"), _json(work), row_id),
            )
            counts["works"] += 1
        for paragraph_id, payload_json in connection.execute(
            "SELECT paragraph_id, payload_json FROM paragraphs WHERE source_file_id = ?", (source_file_id,)
        ).fetchall():
            paragraph = json.loads(payload_json)
            if title:
                paragraph["document_title"] = title
                paragraph["work_title"] = title
                paragraph["volume_display"] = title
            paragraph["author_label"] = author
            connection.execute(
                "UPDATE paragraphs SET payload_json = ? WHERE paragraph_id = ?",
                (_json(paragraph_payload_for_storage(paragraph)), paragraph_id),
            )
            counts["paragraphs"] += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return counts


def _extract_explicit_filename_metadata(
    file_stem: str,
) -> Tuple[Dict[str, str], Dict[str, object], Dict[str, float]]:
    """Read explicit Chinese author/translator roles from a descriptive filename."""

    normalized = unicodedata.normalize("NFKC", str(file_stem or ""))

    # Zotero/CNKI 导出命名："作者 - 年份 - 标题"。这类文件的 PDF 内嵌
    # 属性常是"CNKI"之类的产库署名，文件名反而是最可靠的来源。
    zotero = re.fullmatch(
        r"\s*(?P<author>[^-]{1,40}?)\s*-\s*(?P<year>(?:19|20)\d{2})\s*-\s*(?P<title>.{4,})\s*",
        normalized,
    )
    if zotero and _is_plausible_person_name(zotero.group("author")):
        values = {
            "author": _clean_people(zotero.group("author")),
            "publish_year": zotero.group("year"),
            "title": zotero.group("title").strip().replace(":", "："),
        }
        values = {field: value for field, value in values.items() if is_valid_bibliographic_value(value)}
        scores = {"author": 0.97, "publish_year": 0.96, "title": 0.97}
        evidence = {
            field: {
                "source": "file_name",
                "source_page": None,
                "evidence_text": normalized,
                "rule": "zotero_filename_pattern",
                "confidence": scores[field],
            }
            for field in values
        }
        return values, evidence, {field: scores[field] for field in values}

    match = re.search(
        rf"\(\s*(?:\((?P<country>[^()]{{1,8}})\)\s*)?"
        rf"(?P<author>[{_CHINESE_NAME_CHARS}\s]{{2,30}}?)\s*著\s*"
        rf"(?P<translator>[{_CHINESE_NAME_CHARS}\s,，、;；]{{2,60}}?)\s*译\s*\)",
        normalized,
    )
    if not match:
        # 知网导出常用「篇名_作者」命名：末段是作者，其余是篇名。仅当末段像人名
        # 时才采用，避免把含下划线的普通文件名错拆。作者置信度略高，以压过期刊
        # 首页里偶尔抽出的报头/日期噪声，保证文件名里的作者不被丢。
        cnki = re.fullmatch(r"\s*(?P<title>.+?)\s*_\s*(?P<author>[^_]{2,20})\s*", normalized)
        if cnki and _is_plausible_person_name(cnki.group("author")):
            values = {
                # 拆掉末段作者后，篇名里残留的下划线是知网导出遗留的分隔符，去掉。
                "title": cnki.group("title").strip().replace("_", "").replace(":", "："),
                "author": _clean_people(cnki.group("author")),
            }
            values = {field: value for field, value in values.items() if is_valid_bibliographic_value(value)}
            # 知网导出文件名带完整篇名与作者，置信度设得高于期刊首页抽取，确保
            # 篇名/作者这两项最重要的信息不被报头、页码、日期等版面噪声覆盖。
            scores = {"title": 0.95, "author": 0.95}
            evidence = {
                field: {
                    "source": "file_name",
                    "source_page": None,
                    "evidence_text": normalized,
                    "rule": "cnki_underscore_filename",
                    "confidence": scores[field],
                }
                for field in values
            }
            return values, evidence, {field: scores[field] for field in values}
        return {}, {}, {}

    title = normalized[: match.start()].strip()
    title = re.sub(r"^\s*\[[^\]]{1,30}\]\s*", "", title)
    title = re.sub(
        r"\s*第\s*\d+\s*版(?:\s*第\s*\d+\s*次印刷)?\s*$",
        "",
        title,
    ).strip()
    title = title.replace(":", "：")
    title = re.sub(r"\(([\u3400-\u9fff]{2,})\)", r"（\1）", title)
    values = {
        "title": title,
        "author": _clean_people(match.group("author")),
        "translator": _clean_people(match.group("translator")),
    }
    if match.group("country"):
        values["country"] = match.group("country").strip()
    values = {field: value for field, value in values.items() if is_valid_bibliographic_value(value)}
    scores = {"title": 0.94, "author": 0.98, "country": 0.99, "translator": 0.98}
    evidence = {
        field: {
            "source": "file_name",
            "source_page": None,
            "evidence_text": match.group(0),
            "rule": "explicit_filename_responsibility",
            "confidence": scores[field],
        }
        for field in values
    }
    return values, evidence, {field: scores[field] for field in values}


def _extract_chinese_cip_statement(
    text: str,
    page_idx: int,
    candidates: Dict[str, List[_MetadataCandidate]],
) -> None:
    """Extract one Chinese CIP responsibility and publication statement as a unit."""

    flat = re.sub(r"\s+", " ", text).strip()
    marker = re.search(
        r"图书在版编目\s*[（(]?\s*CIP\s*[)）]?\s*数据",
        flat,
        flags=re.IGNORECASE,
    )
    if not marker:
        return
    statement_text = flat[marker.end() : marker.end() + 520]
    statement = re.search(
        # 题名/责任者分隔斜杠后面跟的是责任者（（美）… / 人名），不会是
        # 数字；数字间的斜杠（《24/7》）属于题名本身。
        rf"(?P<title>.{{2,130}}?)\s*[/／](?!\d)\s*"
        rf"(?P<responsibility>.{{2,110}}?)\s*"
        rf"(?:[.。．]\s*)?(?:[—–―一\-]{{1,2}}\s*)?"
        rf"(?P<place>[\u3400-\u9fff]{{2,8}})\s*[:：]\s*"
        rf"(?P<publisher>[\u3400-\u9fff·\s]{{2,45}}?{_CHINESE_PUBLISHER_SUFFIX})"
        rf"\s*[,，]\s*(?P<year>(?:19|20)\d{{2}})",
        statement_text,
    )
    if not statement:
        return

    evidence_text = statement.group(0).strip()
    _add_candidate(
        candidates,
        "title",
        _clean_cip_title(statement.group("title")),
        page_idx,
        evidence_text,
        0.995,
        "chinese_cip_statement",
    )
    responsibility = statement.group("responsibility").strip()
    if not any(responsibility in line for line in text.splitlines()):
        _extract_chinese_people(responsibility, page_idx, candidates)
    _add_candidate(
        candidates,
        "publish_place",
        statement.group("place"),
        page_idx,
        evidence_text,
        0.995,
        "chinese_cip_statement",
    )
    _add_candidate(
        candidates,
        "publisher",
        re.sub(r"\s+", "", _clean_publisher(statement.group("publisher"))),
        page_idx,
        evidence_text,
        0.995,
        "chinese_cip_statement",
    )
    _add_candidate(
        candidates,
        "publish_year",
        statement.group("year"),
        page_idx,
        evidence_text,
        0.995,
        "chinese_cip_statement",
    )


def _extract_latest_chinese_edition(
    text: str,
    page_idx: int,
    candidates: Dict[str, List[_MetadataCandidate]],
) -> None:
    editions: List[Tuple[int, int, str]] = []
    for match in re.finditer(
        r"(?P<year>(?:19|20)\d{2})\s*年[^。\n]{0,36}?"
        r"第\s*(?P<edition>\d+|[一二三四五六七八九十]+)\s*版",
        text,
    ):
        edition = _small_chinese_number(match.group("edition"))
        if edition is not None:
            editions.append((edition, int(match.group("year")), match.group(0)))
    if not editions:
        return
    edition, year, evidence_text = max(editions, key=lambda item: (item[0], item[1]))
    _add_candidate(
        candidates,
        "publish_year",
        str(year),
        page_idx,
        evidence_text,
        0.995,
        "latest_chinese_edition_statement",
    )


def _small_chinese_number(value: str) -> Optional[int]:
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value)


def _clean_cip_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip(" ,，.。．:：")
    text = re.sub(r"[.．]\s*(?=第\s*\d+\s*卷)", " ", text)
    text = re.sub(r"(第\s*\d+\s*卷)\s*[,，]\s*", r"\1：", text, count=1)
    text = re.sub(r"第\s*(\d+)\s*卷", r"第\1卷", text)
    # NFKC 会把全角冒号压成半角；中文书名恢复全角。
    if re.search(r"[㐀-鿿]", text):
        text = text.replace(":", "：")
    return re.sub(r"\s+", " ", text).strip()


def _extract_from_front_matter(
    texts: Sequence[Tuple[int, str]],
) -> Tuple[Dict[str, str], Dict[str, object], Dict[str, float], List[Dict[str, object]]]:
    candidates: Dict[str, List[_MetadataCandidate]] = {field: [] for field in METADATA_FIELDS}
    for page_idx, text in texts:
        normalized = unicodedata.normalize("NFKC", text)
        normalized = normalized.replace("•", "·").replace("・", "·").replace("‧", "·")
        if not _is_bibliographic_page(page_idx, normalized):
            continue
        _extract_chinese_cip_statement(normalized, page_idx, candidates)
        _extract_latest_chinese_edition(normalized, page_idx, candidates)
        lines = [re.sub(r"\s+", " ", line).strip() for line in normalized.splitlines() if line.strip()]
        page_has_cjk = bool(re.search(r"[\u3400-\u9fff]", normalized))
        for line_index, line in enumerate(lines):
            cjk_line = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", line)
            windows = [line]
            if line_index + 1 < len(lines):
                windows.append(f"{line} {lines[line_index + 1]}")

            # 论文脚注/参考文献里的“黑格尔：《法哲学原理》，北京：人民出版社，
            # 1972年版”与版权页声明同形。含书名号或“年版”的行是引文，
            # 不参与本篇出版社/出版地/年份提取（真正的版权页不用书名号）。
            if "《" in line or "》" in line or "年版" in line:
                # 老籍出版说明常用“《书名》……，宋某某撰”复核著者。
                # 只读带“撰”的古籍责任句，仍不从该行提取出版信息。
                if "撰" in line:
                    _extract_chinese_people(line, page_idx, candidates)
                continue

            for publisher in KNOWN_PUBLISHERS:
                if publisher in cjk_line:
                    _add_candidate(candidates, "publisher", publisher, page_idx, cjk_line, 0.9, "known_publisher")
            for publisher_alias, publisher in PUBLISHER_ALIASES.items():
                if publisher_alias.casefold() in cjk_line.casefold():
                    _add_candidate(
                        candidates,
                        "publisher",
                        publisher,
                        page_idx,
                        line,
                        0.99,
                        "publisher_alias",
                    )

            # ISBN 扫描用双行窗口：中文 CIP 常把"ISBN"标签与号码分在两行；
            # 版权页的号码还常被 OCR 打散成"978 - 7 - 2 0 8 - …"，978 分支
            # 因此放宽长度上限。
            for isbn_text in windows:
                isbn_matches = list(
                    re.finditer(
                        r"(?:ISBN\s*[:：]?\s*(?:HB|PB|HC|SC|EBOOK|ELECTRONIC)?\s*)?"
                        r"(?<!\d)(97[89][0-9XxIlOo\- ]{9,34}|[0-9][0-9XxIlOo\- ]{8,18}[0-9XxIlOo])(?!\d)",
                        isbn_text,
                        flags=re.IGNORECASE,
                    )
                )
                isbn_label_position = isbn_text.casefold().find("isbn")
                for isbn in isbn_matches:
                    if isbn_label_position < 0 or isbn.start(1) < isbn_label_position:
                        continue
                    raw_isbn = isbn.group(1).translate(str.maketrans({"I": "1", "l": "1", "O": "0", "o": "0"}))
                    digits = re.sub(r"[^0-9Xx]", "", raw_isbn)
                    if len(digits) not in {10, 13}:
                        continue
                    leading_context = isbn_text[max(0, isbn.start() - 14) : isbn.start()]
                    trailing_context = isbn_text[isbn.end() : min(len(isbn_text), isbn.end() + 30)]
                    if re.search(r"ebook|electronic|电子", trailing_context, flags=re.IGNORECASE):
                        isbn_confidence, isbn_rule = 0.78, "isbn_electronic"
                    elif re.search(
                        r"\b(?:HB|HC)\b",
                        leading_context,
                        flags=re.IGNORECASE,
                    ) or re.search(r"\b(?:cloth|print)\b|精装", trailing_context, flags=re.IGNORECASE):
                        isbn_confidence, isbn_rule = 0.99, "isbn_print_edition"
                    else:
                        isbn_confidence, isbn_rule = 0.96, "isbn_label"
                    _add_candidate(
                        candidates,
                        "isbn",
                        re.sub(r"\s+", "", raw_isbn).rstrip("-"),
                        page_idx,
                        isbn_text,
                        isbn_confidence,
                        isbn_rule,
                    )

            _extract_chinese_people(line, page_idx, candidates)
            for window in windows:
                _extract_english_people(
                    window,
                    page_idx,
                    candidates,
                    role_confidence=0.84 if page_has_cjk else 0.98,
                )

            explicit_place = re.search(r"(?:出版地|出版地点)\s*[:：]\s*([\u4e00-\u9fff]{2,8})", cjk_line)
            if explicit_place:
                _add_candidate(
                    candidates,
                    "publish_place",
                    explicit_place.group(1),
                    page_idx,
                    cjk_line,
                    0.97,
                    "explicit_publication_place",
                )

            city_publisher = re.search(
                rf"(?P<place>[\u3400-\u9fff]{{2,10}})\s*[:：]\s*"
                rf"(?P<publisher>[\u3400-\u9fff·]{{2,40}}?{_CHINESE_PUBLISHER_SUFFIX})",
                cjk_line,
            )
            if city_publisher and city_publisher.group("place") not in INVALID_PUBLICATION_PLACES:
                _add_candidate(
                    candidates,
                    "publish_place",
                    city_publisher.group("place"),
                    page_idx,
                    cjk_line,
                    0.99,
                    "chinese_catalog_statement",
                )
                _add_candidate(
                    candidates,
                    "publisher",
                    _clean_publisher(city_publisher.group("publisher")),
                    page_idx,
                    cjk_line,
                    0.99,
                    "chinese_catalog_statement",
                )

            labelled_publisher = re.search(
                rf"(?:出版发行|出版者|出版社)(?:\s*[:：]\s*|\s+)"
                rf"(?P<publisher>[\u3400-\u9fff·]{{2,40}}?{_CHINESE_PUBLISHER_SUFFIX})",
                cjk_line,
            )
            if labelled_publisher:
                _add_candidate(
                    candidates,
                    "publisher",
                    _clean_publisher(labelled_publisher.group("publisher")),
                    page_idx,
                    cjk_line,
                    0.97,
                    "labelled_chinese_publisher",
                )

            standalone_publisher = re.fullmatch(
                rf"(?P<publisher>[\u3400-\u9fff·]{{2,40}}?{_CHINESE_PUBLISHER_SUFFIX})"
                rf"(?:出版|發行|发行)?",
                cjk_line,
            )
            if standalone_publisher:
                _add_candidate(
                    candidates,
                    "publisher",
                    _clean_publisher(standalone_publisher.group("publisher")),
                    page_idx,
                    cjk_line,
                    0.98,
                    "standalone_chinese_publisher",
                )

            for generic_publisher in re.finditer(
                rf"([\u3400-\u9fff·]{{2,40}}?{_CHINESE_PUBLISHER_SUFFIX})",
                cjk_line,
            ):
                _add_candidate(
                    candidates,
                    "publisher",
                    _clean_publisher(generic_publisher.group(1)),
                    page_idx,
                    cjk_line,
                    0.86,
                    "generic_chinese_publisher",
                )

            year = re.search(r"((?:19|20)\d{2})\s*年(?:\s*\d{1,2}\s*月)?(?:\s*第\s*[一二三四五六七八九十\d]+\s*版)?", line)
            if year and ("出版" in line or "版" in line or "发行" in line):
                if (
                    "英译本" in line
                    or "原版" in line
                    or "原著" in line
                    or ("据" in line and "译" in line)
                    or ("版" in line and "译" in line)
                ):
                    rule, year_confidence = "source_edition_year", 0.62
                elif re.search(r"(?:^|[：:])\s*(?:版次|出版时间|出版日期|出版发行)", line):
                    rule, year_confidence = "chinese_edition_statement", 0.99
                elif re.search(r"第\s*[一二三四五六七八九十\d]+\s*版", line):
                    rule, year_confidence = "chinese_edition_statement", 0.98
                elif "图书在版编目" in line or city_publisher:
                    rule, year_confidence = "chinese_catalog_year", 0.98
                elif "译者注" in line or re.search(r"第\s*\d+\s*页", line):
                    rule, year_confidence = "referenced_publication_year", 0.68
                else:
                    rule, year_confidence = "chinese_publication_year", 0.91
                _add_candidate(
                    candidates,
                    "publish_year",
                    year.group(1),
                    page_idx,
                    line,
                    year_confidence,
                    rule,
                )

            for window in windows:
                _extract_english_publication_statement(window, page_idx, candidates)

        _extract_english_title_page(lines, page_idx, candidates)

    detected: Dict[str, str] = {}
    evidence: Dict[str, object] = {}
    confidence: Dict[str, float] = {}
    conflicts: List[Dict[str, object]] = []
    for field, items in candidates.items():
        if not items:
            continue
        by_value: Dict[str, List[_MetadataCandidate]] = {}
        for item in items:
            by_value.setdefault(item.value, []).append(item)
        ranked = sorted(
            by_value.items(),
            key=lambda pair: (
                -_candidate_group_score(pair[1]),
                -len(pair[1]),
                min(item.page_idx for item in pair[1]),
            ),
        )
        value, support = ranked[0]
        best = max(support, key=lambda item: item.confidence)
        if _candidate_group_score(support) < MIN_AUTO_CONFIDENCE.get(field, 0.88):
            continue
        detected[field] = value
        evidence[field] = {
            "source": "front_matter_text",
            "source_page": best.page_idx + 1,
            "evidence_text": best.evidence_text,
            "rule": best.rule,
            "confidence": round(min(0.99, _candidate_group_score(support)), 4),
            "support_count": len(support),
        }
        confidence[field] = min(0.99, _candidate_group_score(support))
        if len(ranked) > 1:
            second_value, second_support = ranked[1]
            if (
                _candidate_group_score(support) - _candidate_group_score(second_support) < 0.015
                and not _candidate_values_are_compatible(value, second_value)
            ):
                conflicts.append({"field": field, "values": [value, second_value]})
    if detected.get("title") and isinstance(evidence.get("title"), dict) and evidence["title"].get("rule") == "english_catalog_title":
        repaired = _repair_english_title_casing(str(detected["title"]), texts)
        if repaired and repaired != detected["title"]:
            detected["title"] = repaired
            evidence["title"]["rule"] = "english_catalog_title_with_title_page_casing"
    return detected, evidence, confidence, conflicts
































def _is_bibliographic_page(page_idx: int, text: str) -> bool:
    strong_markers = (
        "ISBN",
        "图书在版编目",
        "CIP 数据",
        "出版发行",
        "出版者:",
        "出版者：",
        "版次:",
        "版次：",
        "著者:",
        "著者：",
        "译者:",
        "译者：",
        "Copyright",
        "All rights reserved",
        "Published by",
        "Library of Congress",
        "Cataloging-in-Publication",
        "Cataloguing in Publication",
        "Identifiers:",
        "Description:",
    )
    if any(marker.casefold() in text.casefold() for marker in strong_markers):
        return True

    non_bibliographic_markers = (
        "Titles in the Series",
        "Contents",
        "Acknowledgements",
        "Acknowledgments",
        "Bibliography",
        "目录",
        "总序",
        "代译序",
        "中译本序",
        "前言",
        "导言",
    )
    if any(marker.casefold() in text.casefold() for marker in non_bibliographic_markers):
        return False
    return page_idx < 8












def _translator_parts_from_filename(file_stem: str) -> List[str]:
    match = re.search(
        rf"著\s*(?P<names>[{_CHINESE_NAME_CHARS}\s,，、;；]{{3,60}}?)\s*译",
        unicodedata.normalize("NFKC", file_stem),
    )
    if not match:
        return []
    parts = [
        re.sub(r"\s+", "", part).strip(" ,，、;；()（）[]［］")
        for part in re.split(r"[,，、;；]+", match.group("names"))
    ]
    parts = [part for part in parts if re.fullmatch(r"[\u3400-\u9fff·]{2,8}", part)]
    return parts if len(parts) >= 2 else []


def _reconcile_fused_people(ocr_value: str, filename_parts: Sequence[str]) -> Optional[str]:
    ocr_compact = re.sub(r"[^A-Za-z\u3400-\u9fff·]", "", ocr_value)
    if not ocr_compact or "，" in ocr_value or "、" in ocr_value:
        return None
    lengths = [len(re.sub(r"[^A-Za-z\u3400-\u9fff·]", "", part)) for part in filename_parts]
    if not lengths or sum(lengths) != len(ocr_compact):
        return None

    slices: List[str] = []
    offset = 0
    differences = 0
    for part, length in zip(filename_parts, lengths):
        ocr_part = ocr_compact[offset : offset + length]
        file_part = re.sub(r"[^A-Za-z\u3400-\u9fff·]", "", part)
        differences += sum(left != right for left, right in zip(ocr_part, file_part))
        slices.append(ocr_part)
        offset += length
    if differences > max(1, len(ocr_compact) // 6):
        return None
    return "，".join(slices)






def _extract_chinese_people(
    line: str,
    page_idx: int,
    candidates: Dict[str, List[_MetadataCandidate]],
) -> None:
    country = r"(?:[\[［【(（〔](?P<country>[^\]］】)）〕]{1,8})[\]］】)）〕]\s*)?"
    required_country = r"[\[［【(（〔](?P<country>[^\]］】)）〕]{1,8})[\]］】)）〕]\s*"
    name = rf"(?P<name>[{_CHINESE_NAME_CHARS}][{_CHINESE_NAME_CHARS}\s,，、]{{1,48}}?)"
    short_name = rf"(?P<name>[{_CHINESE_NAME_CHARS}][{_CHINESE_NAME_CHARS}\s,，、]{{1,14}}?)"

    author_patterns = (
        (rf"(?:著者|作者)\s*[:：]\s*{country}{name}(?=$|[;；])", "labelled_chinese_author", 0.98),
        (rf"{country}{name}\s*(?:[/／]\s*)?著(?=$|[\s,，;；.。/／])", "chinese_author_role", 0.96),
        (rf"^{required_country}{short_name}\s*撰(?=$|[\s,，;；.。/／])", "classical_chinese_author_role", 0.98),
    )
    for pattern, rule, score in author_patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            _add_candidate(candidates, "author", _clean_people(match.group("name")), page_idx, line, score, rule)
            if match.groupdict().get("country"):
                _add_candidate(
                    candidates,
                    "country",
                    match.group("country").strip(),
                    page_idx,
                    line,
                    score,
                    rule,
                )

    classical_prose = re.search(
        rf"(?:^|[,，;；。：《》])"
        rf"(?P<country>先秦|秦|西漢|东汉|東漢|漢|汉|魏|晉|晋|隨|隋|唐|五代|宋|遼|辽|金|元|明|清|民國|民国)"
        rf"(?P<name>[{_CHINESE_NAME_CHARS}]{{2,10}}?)"
        rf"(?:[（(][^)）]{{1,32}}[)）])?\s*撰(?=$|[\s,，;；.。])",
        line,
    )
    if classical_prose:
        _add_candidate(
            candidates,
            "author",
            _clean_people(classical_prose.group("name")),
            page_idx,
            line,
            0.95,
            "classical_chinese_authorship_prose",
        )
        _add_candidate(
            candidates,
            "country",
            classical_prose.group("country"),
            page_idx,
            line,
            0.95,
            "classical_chinese_authorship_prose",
        )

    translator_patterns = (
        (
            rf"(?:译者|翻译|译校)\s*[:：]\s*(?P<name>[{_CHINESE_NAME_CHARS}\s,，、]{{2,50}}?)(?=$|[;；])",
            "labelled_chinese_translator",
            0.99,
        ),
        (
            rf"(?:著|author)\s*[,，;；、/／\s]*"
            rf"(?P<name>[{_CHINESE_NAME_CHARS}\s,，、]{{2,50}}?)\s*(?:[/／]\s*)?(?:译校|译)(?=$|[\s,，;；.。])",
            "chinese_translator_after_author",
            0.98,
        ),
        (
            rf"^(?P<name>[{_CHINESE_NAME_CHARS}\s,，、]{{2,50}}?)\s*(?:[/／]\s*)?(?:译校|译)(?=$|[\s,，;；.。])",
            "chinese_translator_role",
            0.96,
        ),
    )
    for pattern, rule, score in translator_patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            _add_candidate(
                candidates,
                "translator",
                _clean_people(match.group("name")),
                page_idx,
                line,
                score,
                rule,
            )


def _extract_english_people(
    text: str,
    page_idx: int,
    candidates: Dict[str, List[_MetadataCandidate]],
    *,
    role_confidence: float,
) -> None:
    translator = re.search(
        rf"\btranslated\s+by\s+(?P<name>[{_ENGLISH_NAME_CHARS}]{{2,80}}?)(?=$|[;|])",
        text,
        flags=re.IGNORECASE,
    )
    if translator:
        _add_candidate(
            candidates,
            "translator",
            _clean_people(translator.group("name")),
            page_idx,
            text,
            role_confidence,
            "english_translated_by",
        )

    catalog_author = re.search(
        r"\bNames?\s*:\s*(?P<last>[A-Z][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’.\- ]+),\s*"
        r"(?P<first>[A-Z][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’.\- ]+?)"
        r"(?:,\s*\d{4}[^,|]*?)?\s*,?\s*author\b",
        text,
        flags=re.IGNORECASE,
    )
    if catalog_author:
        author = f"{catalog_author.group('first').strip()} {catalog_author.group('last').strip()}"
        _add_candidate(candidates, "author", author, page_idx, text, 0.99, "english_catalog_author")

    # LoC CIP 的题名/责任者分隔符是两侧带空格的" / "；不带空格的斜杠
    # （24/7）属于题名本身。
    catalog_title = re.search(r"\bTitle\s*:\s*(?P<title>.+?)\s+/\s+", text, flags=re.IGNORECASE)
    if catalog_title:
        title = re.sub(r"\s+([:;,.])", r"\1", catalog_title.group("title")).strip(" .")
        _add_candidate(candidates, "title", title, page_idx, text, 0.98, "english_catalog_title")


def _extract_english_publication_statement(
    text: str,
    page_idx: int,
    candidates: Dict[str, List[_MetadataCandidate]],
) -> None:
    published_by = re.search(
        r"\bPublished\s+by\s+(?P<publisher>[A-Z][A-Za-z0-9&'’.,\- ]{2,120}?)"
        r"(?=$|\s+\d{1,6}\s+[A-Z])",
        text,
        flags=re.IGNORECASE,
    )
    if published_by:
        publisher = _clean_publisher(re.sub(r"\s+", " ", published_by.group("publisher")).rstrip(" .,\n"))
        _add_candidate(
            candidates,
            "publisher",
            publisher,
            page_idx,
            text,
            0.98,
            "english_published_by",
        )

    copyright_year = re.search(
        r"(?:Copyright\s*(?:©|\(c\))?|©)\s*(?P<year>(?:19|20)\d{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if copyright_year:
        _add_candidate(
            candidates,
            "publish_year",
            copyright_year.group("year"),
            page_idx,
            text,
            0.99,
            "english_copyright_year",
        )

    statement = re.search(
        r"(?P<place>[A-Z][A-Za-z .\-]{1,48}(?:\s*[;,]\s*[A-Z][A-Za-z .\-]{1,48}){0,2})\s*:\s*"
        r"(?P<publisher>(?:The\s+)?[A-Z][A-Za-z0-9&'’.\- ]{1,110}"
        r"(?:University Press|Publishers?|Publishing(?: Group)?|Press|Verlag|International(?:\s*,?\s*Ltd\.?)?))"
        r"\s*,\s*(?P<bracket>\[)?(?P<year>(?:19|20)\d{2})\]?",
        text,
    )
    if not statement:
        return
    # 多个出版地（London ; New York）只取第一个，与引文习惯一致。
    first_place = re.split(r"\s*[;]\s*", re.sub(r"\s+", " ", statement.group("place")))[0].strip()
    _add_candidate(
        candidates,
        "publish_place",
        first_place,
        page_idx,
        text,
        0.92,
        "english_catalog_statement",
    )
    _add_candidate(
        candidates,
        "publisher",
        _clean_publisher(re.sub(r"\s+", " ", statement.group("publisher"))),
        page_idx,
        text,
        0.99,
        "english_catalog_statement",
    )
    # LoC CIP 的方括号年份（[2018]）是登记年而非出版年，置信度低于版权行 ©。
    _add_candidate(
        candidates,
        "publish_year",
        statement.group("year"),
        page_idx,
        text,
        0.9 if statement.group("bracket") else 0.98,
        "english_catalog_statement",
    )


def _extract_english_title_page(
    lines: Sequence[str],
    page_idx: int,
    candidates: Dict[str, List[_MetadataCandidate]],
) -> None:
    publisher_pattern = re.compile(
        r"^(?P<publisher>(?:The\s+)?[A-Z][A-Za-z0-9&'’.\- ]{1,110}"
        r"(?:University Press|Publishers?|Publishing(?: Group)?|Press|Verlag|International(?:\s*,?\s*Ltd\.?)?))$"
    )
    place_pattern = re.compile(r"^[A-Z][A-Za-z .\-]{1,40}(?:,\s*[A-Z][A-Za-z .\-]{1,40})$")
    year_pattern = re.compile(r"^(?:19|20)\d{2}$")

    for index, line in enumerate(lines):
        publisher_text = line
        publisher_match = publisher_pattern.match(publisher_text)
        if not publisher_match and index + 1 < len(lines):
            publisher_text = f"{line} {lines[index + 1]}"
            publisher_match = publisher_pattern.match(publisher_text)
        if not publisher_match:
            continue

        publisher = _clean_publisher(re.sub(r"\s+", " ", publisher_match.group("publisher")))
        _add_candidate(
            candidates,
            "publisher",
            publisher,
            page_idx,
            publisher_text,
            0.94,
            "english_title_page_publisher",
        )
        nearby = lines[index + 1 : index + 7]
        for nearby_line in nearby:
            if place_pattern.match(nearby_line):
                _add_candidate(
                    candidates,
                    "publish_place",
                    nearby_line,
                    page_idx,
                    nearby_line,
                    0.95,
                    "english_title_page_place",
                )
                break
        for nearby_line in nearby:
            if year_pattern.match(nearby_line):
                _add_candidate(
                    candidates,
                    "publish_year",
                    nearby_line,
                    page_idx,
                    nearby_line,
                    0.95,
                    "english_title_page_year",
                )
                break


def _embedded_pdf_metadata(path: Path) -> Dict[str, str]:
    try:
        import fitz  # type: ignore
        document = fitz.open(str(path))
    except Exception:
        return {}
    try:
        raw = document.metadata or {}
    finally:
        document.close()
    result: Dict[str, str] = {}
    title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()
    author = re.sub(r"\s+", " ", str(raw.get("author") or "")).strip()
    invalid = {"ssreader print.", "hp", "untitled"}
    if title and title.lower() not in invalid:
        result["title"] = title
    if author and author.lower() not in invalid:
        result["author"] = author
    return result










