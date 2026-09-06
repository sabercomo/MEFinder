"""题录字段的取值校验、清洗与候选打分。

“这个值算不算有效题录”“人名怎么切、出版者怎么归一、候选怎么合并计分”——
所有文献类型的抽取器都要用同一套判断，这里就是那套共用底座。本模块不感知
PDF、不感知具体文献类型，是题录域的最底层。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


METADATA_FIELDS = (
    "title", "author", "country", "translator", "publisher", "publish_place",
    "publish_year", "isbn", "journal_name", "volume", "issue", "page_range",
    "doi", "issn",
)


DOCUMENT_TYPES = ("book", "translated_book", "journal_article", "thesis")


# 人工可指定的语言：自动识别失败（如英译本+德文原名前页判为平局→und）时的兜底。
MANUAL_LANGUAGE_CODES = frozenset(
    {"zh-Hans", "zh-Hant", "en", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "und"}
)


RESPONSIBILITY_STATUSES = ("present", "none", "unknown")


PUBLISHER_PLACES = {
    "上海古籍出版社": "上海",
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
    "中华书局": "北京",
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
    "中華書局": "中华书局",
    "商務印書館": "商务印书馆",
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
    "doi": 0.9,
    "issn": 0.9,
}


_CHINESE_PUBLISHER_SUFFIX = (
    r"(?:出版(?:集团|集團)(?:股份有限公司|有限公司)?|"
    r"出版中心|出版社|印书馆|印書館|书局|書局)"
)


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
        "doi": ("doi", "DOI"),
        "issn": ("issn", "ISSN"),
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
        "responsibility_status",
        "language_code_manual",
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
