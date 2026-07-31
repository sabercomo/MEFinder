"""Deterministic bibliographic metadata extraction from PDF front matter."""

from __future__ import annotations

import re
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .database import paragraph_payload_for_storage

METADATA_FIELDS = (
    "title", "author", "country", "translator", "publisher", "publish_place",
    "publish_year", "isbn", "journal_name", "volume", "issue", "page_range",
)
DOCUMENT_TYPES = ("book", "translated_book", "journal_article")
PUBLISHER_PLACES = {
    "上海人民出版社": "上海",
    "上海译文出版社": "上海",
    "商务印书馆": "北京",
    "人民出版社": "北京",
    "北京大学出版社": "北京",
    "清华大学出版社": "北京",
    "中信出版社": "北京",
    "中信出版集团": "北京",
    "中信出版集团股份有限公司": "北京",
    "生活·读书·新知三联书店": "北京",
    "三联书店": "北京",
    "江苏人民出版社": "南京",
    "南京大学出版社": "南京",
    "译林出版社": "南京",
    "华东师范大学出版社": "上海",
    "复旦大学出版社": "上海",
    "广西师范大学出版社": "桂林",
    "重庆出版社": "重庆",
    "社会科学文献出版社": "北京",
    "中国社会科学出版社": "北京",
}
KNOWN_PUBLISHERS = sorted(PUBLISHER_PLACES, key=len, reverse=True)
INVALID_PLACEHOLDERS = {"unknown", "unrecognized", "未识别", "未知", "暂无", "null", "none"}
INVALID_PUBLICATION_PLACES = {
    "出版",
    "出版地",
    "出版者",
    "出版社",
    "出版发行",
    "发行",
    "策划推广",
    "图书在版编目",
}
PUBLISHER_ALIASES = {
    "China CITIC Press": "中信出版社",
    "CHINA CITIC PRESS": "中信出版社",
    "SDX Joint Publishing Company": "生活·读书·新知三联书店",
}
MARX_ENGELS_FIRST_EDITION_YEARS = {
    1: "1956", 2: "1957", 3: "1960", 4: "1958", 5: "1958",
    6: "1961", 7: "1959", 8: "1961", 9: "1961", 10: "1962",
    11: "1962", 12: "1962", 13: "1962", 14: "1964", 15: "1963",
    16: "1964", 17: "1963", 18: "1964", 19: "1963", 20: "1971",
    21: "1965", 22: "1965", 23: "1972", 24: "1972", 25: "1974",
    27: "1972", 28: "1973", 29: "1972", 30: "1975", 31: "1972",
    32: "1975", 33: "1973", 34: "1972", 35: "1971", 36: "1974",
    37: "1971", 38: "1972", 39: "1974", 40: "1982", 41: "1982",
    42: "1979", 43: "1982", 44: "1982", 45: "1985", 47: "1979",
    48: "1985", 49: "1982", 50: "1985",
}
MARX_ENGELS_FIRST_EDITION_PART_YEARS = {
    (26, "一"): "1972",
    (26, "二"): "1973",
    (26, "三"): "1974",
    (46, "上"): "1979",
    (46, "下"): "1980",
}
# 《马克思恩格斯全集》中文第二版逐卷出版年份，由用户提供。
# 第二版仍在陆续出版，表中没有的卷次一律不给年份，不做任何推断。
MARX_ENGELS_SECOND_EDITION_YEARS = {
    1: "1995", 2: "2005", 3: "2002", 10: "1998", 11: "1995",
    12: "1998", 13: "1998", 14: "2013", 16: "2007", 19: "2006",
    21: "2003", 25: "2001", 26: "2014", 28: "2018", 29: "2021",
    30: "1995", 31: "1998", 32: "1998", 33: "2004", 34: "2008",
    35: "2013", 36: "2015", 37: "2019", 38: "2019", 42: "2016",
    43: "2016", 44: "2001", 45: "2003", 46: "2003", 47: "2004",
    48: "2007", 49: "2016", 50: "2022",
}
_CHINESE_DIGITS = "零一二三四五六七八九"
MIN_AUTO_CONFIDENCE = {
    "title": 0.9,
    "author": 0.88,
    "country": 0.88,
    "translator": 0.9,
    "publisher": 0.88,
    "publish_place": 0.88,
    "publish_year": 0.86,
    "isbn": 0.88,
}

_CHINESE_PUBLISHER_SUFFIX = r"(?:出版集团(?:股份有限公司|有限公司)?|出版社|印书馆|书局)"
_CHINESE_NAME_CHARS = r"\u3400-\u4dbf\u4e00-\u9fff·•.．・‧\-—A-Za-z"
_ENGLISH_NAME_CHARS = r"A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’.\-\s"


@dataclass(frozen=True)
class _MetadataCandidate:
    value: str
    page_idx: int
    evidence_text: str
    confidence: float
    rule: str


def canonical_metadata(value: Mapping[str, object]) -> Dict[str, object]:
    return _canonical_metadata(value)


def is_valid_bibliographic_value(value: object) -> bool:
    text = str(value or "").strip()
    if not text or text.lower() in INVALID_PLACEHOLDERS or "\ufffd" in text:
        return False
    question_count = text.count("?") + text.count("？")
    if question_count >= 2 and question_count / max(len(text), 1) >= 0.3:
        return False
    return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", text))


def _has_suspicious_person_punctuation(value: object) -> bool:
    """Question marks inside a person's name usually indicate encoding loss."""

    text = str(value or "").strip()
    return bool(re.search(r"[A-Za-z\u3400-\u9fff][?？][A-Za-z\u3400-\u9fff]", text))


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


def marx_engels_first_edition_metadata(file_name: object) -> Dict[str, str]:
    """Return trusted catalog defaults for Chinese first-edition全集 scans."""

    stem = Path(str(file_name or "")).stem.strip()
    normalized = unicodedata.normalize("NFKC", stem)
    match = re.fullmatch(
        r"\s*《?\s*(?:马克思恩格斯|马恩)全集\s*》?\s*"
        r"第\s*0*(?P<volume>\d{1,2})\s*卷"
        r"(?:\s*\(?\s*(?P<part>上|中|下|一|二|三|1|2|3|上册|中册|下册|第一册|第二册|第三册)\s*\)?)?\s*",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return {}
    volume = int(match.group("volume"))
    if not 1 <= volume <= 50:
        return {}
    part_aliases = {
        "1": "一", "2": "二", "3": "三",
        "第一册": "一", "第二册": "二", "第三册": "三",
        "上册": "上", "中": "二", "中册": "二", "下册": "下",
    }
    part = part_aliases.get(str(match.group("part") or ""), str(match.group("part") or ""))
    allowed_parts = {
        1: {"上", "下"},
        14: {"上", "下"},
        25: {"上", "下"},
        26: {"一", "二", "三"},
        28: {"上", "下"},
        30: {"上", "下"},
        31: {"上", "下"},
        39: {"上", "下"},
        46: {"上", "下"},
    }
    if part and part not in allowed_parts.get(volume, set()):
        return {}
    year = MARX_ENGELS_FIRST_EDITION_PART_YEARS.get(
        (volume, part), MARX_ENGELS_FIRST_EDITION_YEARS.get(volume)
    )
    if not year:
        return {}
    part_labels = {"一": "第一册", "二": "第二册", "三": "第三册", "上": "上册", "下": "下册"}
    volume_label = (
        f"{volume}卷{part_labels[part]}"
        if part and volume in {26, 46}
        else str(volume)
    )
    return {
        "title": stem,
        "author": "马克思、恩格斯",
        "publisher": "人民出版社",
        "publish_place": "北京",
        "publish_year": year,
        "volume": volume_label,
        "document_type": "book",
    }


def _chinese_volume_numeral(volume: int) -> str:
    """Render 1–50 the way the volume title pages do (三十一, not 31)."""

    if volume < 10:
        return _CHINESE_DIGITS[volume]
    tens, ones = divmod(volume, 10)
    prefix = "十" if tens == 1 else f"{_CHINESE_DIGITS[tens]}十"
    return prefix + (_CHINESE_DIGITS[ones] if ones else "")


def marx_engels_second_edition_metadata(file_name: object) -> Dict[str, str]:
    """Return catalog defaults for Chinese second-edition全集 scans.

    The user's second-edition scans are named ``me2-<volume>``; that naming is
    a deterministic signal, unlike the OCR title which lands on some volumes
    and not others. Volumes the second edition has not published yet carry no
    year here — the card must show the year as missing rather than borrow one.
    """

    stem = Path(str(file_name or "")).stem.strip()
    normalized = unicodedata.normalize("NFKC", stem)
    match = re.fullmatch(
        r"\s*me2\s*[-_ ]\s*0*(?P<volume>\d{1,2})\s*",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return {}
    volume = int(match.group("volume"))
    if not 1 <= volume <= 50:
        return {}
    defaults = {
        "title": f"马克思恩格斯全集第{_chinese_volume_numeral(volume)}卷",
        "author": "马克思、恩格斯",
        "publisher": "人民出版社",
        "publish_place": "北京",
        "volume": str(volume),
        "document_type": "book",
    }
    year = MARX_ENGELS_SECOND_EDITION_YEARS.get(volume)
    if year:
        defaults["publish_year"] = year
    return defaults


def marx_engels_collection_metadata(file_name: object) -> Tuple[Dict[str, str], str]:
    """Return ``(defaults, rule)`` for whichever全集 edition the name matches."""

    defaults = marx_engels_first_edition_metadata(file_name)
    if defaults:
        return defaults, "marx_engels_chinese_first_edition"
    defaults = marx_engels_second_edition_metadata(file_name)
    if defaults:
        return defaults, "marx_engels_chinese_second_edition"
    return {}, ""


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
        fallback_title = path.stem.strip()
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
    is_journal = any(
        page_idx < 2 and looks_like_journal_article(text) for page_idx, text in texts
    )
    if is_journal:
        detected, detected_evidence, detected_confidence = _extract_journal_article(
            texts, Path(path).stem
        )
        conflicts = []
        filename_values, filename_evidence, filename_confidence = {}, {}, {}
    else:
        detected, detected_evidence, detected_confidence, conflicts = _extract_from_front_matter(texts)
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

    if is_journal:
        result["document_type"] = "journal_article"
    elif result.get("translator"):
        result["document_type"] = "translated_book"
    else:
        result.setdefault("document_type", "book")
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
    if doc_type == "journal_article":
        # 期刊论文不需要出版社/出版地；卷次和起止页可选。
        required = ["author", "title", "journal_name", "publish_year", "issue"]
    else:
        required = ["author", "title", "publisher", "publish_place", "publish_year"]
        if doc_type == "translated_book":
            required.insert(2, "translator")
    return [field for field in required if not is_valid_bibliographic_value(metadata.get(field))]


def manual_metadata(payload: Mapping[str, object], previous: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    result = _canonical_metadata(previous or {})
    invalid = invalid_metadata_fields(payload)
    if invalid:
        raise ValueError("以下书目字段包含无效问号或不可用文本：" + "、".join(invalid))
    for field in METADATA_FIELDS:
        result[field] = str(payload.get(field) or "").strip() or None
    requested_type = str(payload.get("document_type") or "").strip()
    if requested_type in DOCUMENT_TYPES:
        result["document_type"] = requested_type
    elif requested_type:
        raise ValueError(f"未知文献类型：{requested_type}")
    else:
        result["document_type"] = "translated_book" if result.get("translator") else "book"
    missing = metadata_missing_fields(result)
    result["metadata_status"] = "complete" if not missing else "partial"
    result["metadata_source"] = "manual"
    result["metadata_confidence"] = 1.0
    result["metadata_missing_fields"] = missing
    result.setdefault("metadata_evidence", {})
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
        rf"(?:[.。．]\s*)?[—–―一\-]{{1,2}}\s*"
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
            windows = [line]
            if line_index + 1 < len(lines):
                windows.append(f"{line} {lines[line_index + 1]}")

            # 论文脚注/参考文献里的“黑格尔：《法哲学原理》，北京：人民出版社，
            # 1972年版”与版权页声明同形。含书名号或“年版”的行是引文，
            # 不参与本篇出版社/出版地/年份提取（真正的版权页不用书名号）。
            if "《" in line or "》" in line or "年版" in line:
                continue

            for publisher in KNOWN_PUBLISHERS:
                if publisher in line:
                    _add_candidate(candidates, "publisher", publisher, page_idx, line, 0.9, "known_publisher")
            for publisher_alias, publisher in PUBLISHER_ALIASES.items():
                if publisher_alias.casefold() in line.casefold():
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

            explicit_place = re.search(r"(?:出版地|出版地点)\s*[:：]\s*([\u4e00-\u9fff]{2,8})", line)
            if explicit_place:
                _add_candidate(
                    candidates,
                    "publish_place",
                    explicit_place.group(1),
                    page_idx,
                    line,
                    0.97,
                    "explicit_publication_place",
                )

            city_publisher = re.search(
                rf"(?P<place>[\u3400-\u9fff]{{2,10}})\s*[:：]\s*"
                rf"(?P<publisher>[\u3400-\u9fff·]{{2,40}}?{_CHINESE_PUBLISHER_SUFFIX})",
                line,
            )
            if city_publisher and city_publisher.group("place") not in INVALID_PUBLICATION_PLACES:
                _add_candidate(
                    candidates,
                    "publish_place",
                    city_publisher.group("place"),
                    page_idx,
                    line,
                    0.99,
                    "chinese_catalog_statement",
                )
                _add_candidate(
                    candidates,
                    "publisher",
                    _clean_publisher(city_publisher.group("publisher")),
                    page_idx,
                    line,
                    0.99,
                    "chinese_catalog_statement",
                )

            labelled_publisher = re.search(
                rf"(?:出版发行|出版者|出版社)\s*[:：]\s*"
                rf"(?P<publisher>[\u3400-\u9fff·]{{2,40}}?{_CHINESE_PUBLISHER_SUFFIX})",
                line,
            )
            if labelled_publisher:
                _add_candidate(
                    candidates,
                    "publisher",
                    _clean_publisher(labelled_publisher.group("publisher")),
                    page_idx,
                    line,
                    0.97,
                    "labelled_chinese_publisher",
                )

            for generic_publisher in re.finditer(
                rf"([\u3400-\u9fff·]{{2,40}}?{_CHINESE_PUBLISHER_SUFFIX})",
                line,
            ):
                _add_candidate(
                    candidates,
                    "publisher",
                    _clean_publisher(generic_publisher.group(1)),
                    page_idx,
                    line,
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


# GB/T 中文期刊首页的固定标记。专著的内容提要同样会出现「摘要」「关键词」，
# 版权页也可能印中图分类号，因此书刊判定必须先看专著独有的版权信息。
_JOURNAL_MARKERS = ("中图分类号", "文献标识码", "文献标志码")
_JOURNAL_WEAK_MARKERS = ("摘要", "关键词", "DOI:", "doi:")
_BOOK_ONLY_MARKERS = ("图书在版编目", "ISBN", "出版发行", "版次", "定价")

# 例：文章编号 0439-8041(2020)09-0015-13
#     ISSN 0439-8041、2020 年、第 09 期、起始页 15、共 13 页 → 15-27。
_ARTICLE_NUMBER_RE = re.compile(
    r"文章编\s*号\s*[:：]?\s*"
    r"(?P<issn>[0-9]{4}\s*-\s*[0-9]{3}[0-9Xx])"
    r"\s*\(\s*(?P<year>\d{4})\s*\)\s*"
    r"(?P<issue>\d{1,3})\s*-\s*"
    r"(?P<start>\d{1,5})\s*-\s*"
    r"(?P<length>\d{1,4})"
)
# 期刊版式常见的卷期行：「第 52 卷第 9 期」「2020 年第 9 期」。
_VOLUME_ISSUE_RE = re.compile(r"第\s*(?P<volume>\d{1,3})\s*卷\s*第\s*(?P<issue>\d{1,3})\s*期")
_YEAR_ISSUE_RE = re.compile(r"(?P<year>\d{4})\s*年\s*第\s*(?P<issue>\d{1,3})\s*期")
# 「作者张双利，复旦大学哲学学院教授（上海 200433）。」
_AUTHOR_STATEMENT_RE = re.compile(r"^作\s*者\s*[:：]?\s*(?P<author>[^，,。（(]{2,20})")


def looks_like_journal_article(text: str) -> bool:
    """Report whether a page carries the GB/T markers of a journal article."""

    normalized = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"\s+", "", normalized)
    # 文章编号是期刊独有的 GB/T 编码，可直接判定。
    if _ARTICLE_NUMBER_RE.search(compact):
        return True
    # 版权页信息只属于专著；出现即排除期刊，避免内容提要里的“摘要/关键词”误导。
    if any(marker.casefold() in compact.casefold() for marker in _BOOK_ONLY_MARKERS):
        return False
    if any(marker in compact for marker in _JOURNAL_MARKERS):
        return True
    return sum(1 for marker in _JOURNAL_WEAK_MARKERS if marker in compact) >= 2


def _extract_journal_article(
    texts: Sequence[Tuple[int, str]],
    file_stem: str,
) -> Tuple[Dict[str, str], Dict[str, object], Dict[str, float]]:
    """Read the fields a Chinese journal offprint actually encodes.

    ``文章编号`` is a registered GB/T code rather than a guess: it carries the
    year, issue and the article's own page span.  The journal name and volume
    are deliberately *not* inferred -- CNKI offprints usually print neither,
    and inventing them would be exactly the kind of fabricated citation this
    project refuses to produce.  They stay missing so the user is prompted.
    """

    values: Dict[str, str] = {}
    evidence: Dict[str, object] = {}
    confidence: Dict[str, float] = {}

    def record(field: str, value: str, page_idx: Optional[int], source: str, score: float, text: str) -> None:
        if field in values or not is_valid_bibliographic_value(value):
            return
        values[field] = value
        evidence[field] = {"source": source, "source_page": page_idx, "evidence_text": text}
        confidence[field] = score

    first_page = next((item for item in texts if item[0] == 0), None)

    for page_idx, raw_text in texts:
        normalized = unicodedata.normalize("NFKC", raw_text)
        compact = re.sub(r"[ \t]+", "", normalized)

        match = _ARTICLE_NUMBER_RE.search(compact)
        if match:
            year = match.group("year")
            issue = str(int(match.group("issue")))
            start = int(match.group("start"))
            length = int(match.group("length"))
            record("publish_year", year, page_idx, "article_number", 0.97, match.group(0))
            record("issue", issue, page_idx, "article_number", 0.97, match.group(0))
            if start > 0 and length > 0:
                record(
                    "page_range",
                    f"{start}-{start + length - 1}",
                    page_idx,
                    "article_number",
                    0.95,
                    match.group(0),
                )

        volume_match = _VOLUME_ISSUE_RE.search(compact)
        if volume_match:
            record("volume", str(int(volume_match.group("volume"))), page_idx, "masthead", 0.9, volume_match.group(0))
            record("issue", str(int(volume_match.group("issue"))), page_idx, "masthead", 0.9, volume_match.group(0))
        year_match = _YEAR_ISSUE_RE.search(compact)
        if year_match:
            record("publish_year", year_match.group("year"), page_idx, "masthead", 0.88, year_match.group(0))
            record("issue", str(int(year_match.group("issue"))), page_idx, "masthead", 0.88, year_match.group(0))

    # 篇名与作者：期刊首页顶部依次是篇名、作者，随后才是摘要。
    if first_page is not None:
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in unicodedata.normalize("NFKC", first_page[1]).splitlines()
            if line.strip()
        ]
        for line in lines[:12]:
            statement = _AUTHOR_STATEMENT_RE.match(line)
            if statement:
                record("author", _clean_person(statement.group("author")), 0, "author_statement", 0.93, line)
                break
        heading: List[str] = []
        for line in lines:
            if re.match(r"^(摘\s*要|关\s*键\s*词|中图分类号|文献标[识志]码|文章编\s*号|作\s*者)", line):
                break
            heading.append(line)
        if heading:
            record("title", heading[0], 0, "journal_title_line", 0.9, heading[0])
            if len(heading) > 1 and _is_plausible_person_name(heading[1]):
                record("author", _clean_person(heading[1]), 0, "journal_author_line", 0.9, heading[1])

    # CNKI 导出件的文件名是「篇名_作者」，可为版面抽取提供独立佐证。
    stem_match = re.match(r"^(?P<title>.+?)_(?P<author>[^_]{2,20})$", file_stem.strip())
    if stem_match:
        record("title", stem_match.group("title").strip(), None, "file_name", 0.75, file_stem)
        candidate = stem_match.group("author").strip()
        if _is_plausible_person_name(candidate):
            record("author", _clean_person(candidate), None, "file_name", 0.75, file_stem)

    return values, evidence, confidence


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


def _add_candidate(
    candidates: Dict[str, List[_MetadataCandidate]],
    field: str,
    value: object,
    page_idx: int,
    evidence_text: str,
    confidence: float,
    rule: str,
) -> None:
    cleaned = str(value or "").strip(" ,，:：;；.|｜")
    if field == "publisher":
        cleaned = PUBLISHER_ALIASES.get(cleaned, cleaned)
    if not cleaned or not is_valid_bibliographic_value(cleaned):
        return
    if field in {"author", "translator"} and not _is_plausible_person_name(cleaned):
        return
    if field == "publish_place" and cleaned in INVALID_PUBLICATION_PLACES:
        return
    candidate = _MetadataCandidate(cleaned, page_idx, evidence_text, confidence, rule)
    for index, item in enumerate(candidates[field]):
        if (
            item.value == candidate.value
            and item.page_idx == candidate.page_idx
            and item.evidence_text == candidate.evidence_text
        ):
            if candidate.confidence > item.confidence:
                candidates[field][index] = candidate
            return
    candidates[field].append(candidate)


def _compact_value_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9㐀-鿿]", "", str(value or "")).casefold()


def _repair_english_title_casing(title: str, texts: Sequence[Tuple[int, str]]) -> Optional[str]:
    """LoC CIP 的书名是全小写；若书名页有大小写规范的同一书名，用其写法。"""

    target = _compact_value_key(title)
    if not target:
        return None
    for _page_idx, text in texts:
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
        for index in range(len(lines)):
            for width in (1, 2, 3):
                parts = lines[index:index + width]
                if len(parts) < width:
                    break
                joined = " ".join(parts)
                if _compact_value_key(joined) != target:
                    continue
                if joined.upper() == joined:
                    continue  # 全大写的书名页写法不如 CIP 原文
                if width > 1 and ":" not in joined:
                    return f"{parts[0]}: {' '.join(parts[1:])}"
                return joined
    return None


def _candidate_group_score(items: Sequence[_MetadataCandidate]) -> float:
    return max(item.confidence for item in items) + min(0.04, 0.015 * (len(items) - 1))


def _candidate_values_are_compatible(first: str, second: str) -> bool:
    def compact(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9\u3400-\u9fff]", "", value).casefold()

    left = compact(first)
    right = compact(second)
    return bool(left and right and (left in right or right in left))


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


_ARTIFACT_AUTHOR_NAMES = {
    "cnki", "中国知网", "知网", "superstar", "超星", "adobe", "acrobat",
    "microsoft", "word", "wps", "office", "unknown", "admin", "administrator",
    "user", "pdf", "epub", "z-library", "zlibrary", "kdc",
}


def _is_plausible_person_name(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    # 产库/软件署名不是作者（知网 PDF 的内嵌 Author 常是"CNKI"）。
    if compact.casefold() in _ARTIFACT_AUTHOR_NAMES:
        return False
    if compact in {"作者", "著者", "译者", "主编", "编辑", "的翻", "的译", "本书", "此书"}:
        return False
    if re.match(r"^(?:本书|此书|该书|由此|其中|的翻|的译)", compact):
        return False
    return bool(re.search(r"[A-Za-z\u3400-\u9fff]", compact))


def _extract_chinese_people(
    line: str,
    page_idx: int,
    candidates: Dict[str, List[_MetadataCandidate]],
) -> None:
    country = r"(?:[\[［【(（〔](?P<country>[^\]］】)）〕]{1,8})[\]］】)）〕]\s*)?"
    name = rf"(?P<name>[{_CHINESE_NAME_CHARS}][{_CHINESE_NAME_CHARS}\s,，、]{{1,48}}?)"

    author_patterns = (
        (rf"(?:著者|作者)\s*[:：]\s*{country}{name}(?=$|[;；])", "labelled_chinese_author", 0.98),
        (rf"{country}{name}\s*(?:[/／]\s*)?著(?=$|[\s,，;；.。/／])", "chinese_author_role", 0.96),
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


def _canonical_metadata(value: Mapping[str, object]) -> Dict[str, object]:
    nested = value.get("bibliographic_metadata")
    source = dict(nested) if isinstance(nested, Mapping) else dict(value)
    result: Dict[str, object] = {}
    aliases = {
        "title": ("title", "document_title", "display_title"),
        "author": ("author", "author_label"),
        "country": ("country", "nationality"),
        "translator": ("translator",),
        "publisher": ("publisher", "press"),
        "publish_place": ("publish_place", "publication_place"),
        "publish_year": ("publish_year", "publication_year"),
        "isbn": ("isbn",),
        "journal_name": ("journal_name", "journal_title", "journal", "periodical"),
        "volume": ("volume", "journal_volume"),
        "issue": ("issue", "issue_number", "journal_issue"),
        "page_range": ("page_range", "pages", "article_pages"),
    }
    for field, keys in aliases.items():
        result[field] = next((source.get(key) for key in keys if source.get(key) not in (None, "")), None)
    for field in (
        "document_type",
        "metadata_status",
        "metadata_source",
        "metadata_confidence",
        "metadata_evidence",
        "metadata_conflicts",
        "metadata_missing_fields",
    ):
        if source.get(field) not in (None, ""):
            result[field] = source[field]
    return result


def _clean_person(value: str) -> str:
    return _clean_people(value)


def _clean_people(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("•", "·").replace("・", "·").replace("‧", "·")
    text = re.sub(r"^[\[［【(（〔][^\]］】)）〕]{1,8}[\]］】)）〕]\s*", "", text)
    text = re.sub(r"\s*(?:/|／)\s*$", "", text)
    text = text.strip(" ,，、:：;；.。[]［］【】〔〕()（）")
    text = re.sub(r"\s*(?:,|，|、|;|；|&|\band\b)\s*", "、", text, flags=re.IGNORECASE)
    text = re.sub(r"、+", "、", text).strip("、")

    if re.search(r"[\u3400-\u9fff]", text):
        groups: List[str] = []
        for group in text.split("、"):
            tokens = group.split()
            if len(tokens) > 1 and all(re.fullmatch(r"[\u3400-\u9fff·]{2,6}", token) for token in tokens):
                groups.extend(tokens)
            else:
                groups.append(re.sub(r"\s+", "", group))
        text = "、".join(item for item in groups if item)
    else:
        text = re.sub(r"\s+", " ", text)
    return text


def _clean_publisher(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("•", "·").replace("・", "·").replace("‧", "·")
    text = text.strip(" ,，:：;；.。[]［］【】〔〕()（）")
    # 引文里不带英文公司后缀（Rowman & Littlefield International, Ltd. →
    # Rowman & Littlefield International）。
    text = re.sub(r"[\s,，]*\b(?:Ltd|Limited|Inc|Incorporated|LLC|GmbH)\.?$", "", text, flags=re.IGNORECASE)
    return text.strip(" ,，:：;；.。")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
