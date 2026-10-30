"""Deterministic metadata rules for named collected/complete works."""

from __future__ import annotations

import re
from typing import Dict, Optional


_COLLECTION_MARKERS = {
    "文集": "article_collection",
    "全集": "complete_works",
    "选集": "selected_works",
}
_FILE_SUFFIX_RE = re.compile(r"\.(?:docx?|pdf)$", re.IGNORECASE)
_LEADING_COUNTRY_RE = re.compile(r"^[（(][^）)]{1,8}[）)]")
_NON_PERSON_CREATOR_TERMS = (
    "中国", "世界", "全国", "国际", "当代", "现代", "古代", "哲学", "文学",
    "社会", "历史", "文化", "教育", "学术", "论文", "艺术", "经济", "政治",
    "科学", "研究", "资料", "档案", "经典", "作品", "文献", "百科", "辞典",
    "字典", "年鉴", "选编", "汇编", "主义", "编委会", "编辑部", "出版社",
    "研究所", "学会", "协会", "委员会", "大学", "学院",
)


def infer_collection_metadata(*titles: object) -> Dict[str, str]:
    """Infer collection type, canonical title and author from a series title.

    The type rule is intentionally literal. Author inference is narrower: an
    existing responsibility statement always wins, while a plausible personal
    name before the marker can supply a missing author. Marx/Engels aliases are
    normalized to the joint author.
    """

    for value in titles:
        text = str(value or "").strip()
        if not text:
            continue
        text = re.split(r"[\\/]", text)[-1]
        text = _FILE_SUFFIX_RE.sub("", text).strip()
        compact = re.sub(r"\s+", "", text)
        matches = [
            (compact.find(marker), marker)
            for marker in _COLLECTION_MARKERS
            if marker in compact
        ]
        if not matches:
            continue
        marker_index, marker = min(matches, key=lambda item: item[0])
        creator = compact[:marker_index]
        if "《" in creator:
            creator = creator.rsplit("《", 1)[-1]
        creator = _LEADING_COUNTRY_RE.sub("", creator)
        creator = creator.strip("《》〈〉【】[]（）()·:：,，、-—_")
        if not creator or len(creator) > 40:
            continue
        if creator in {"马恩", "马克思恩格斯", "马克思、恩格斯", "马克思和恩格斯"}:
            author = "马克思、恩格斯"
            collection_creator = "马克思恩格斯"
        else:
            collection_creator = creator
            author = creator if _looks_like_person_name(creator) else ""
        result = {
            "collection_title": f"{collection_creator}{marker}",
            "primary_structure": _COLLECTION_MARKERS[marker],
        }
        if author:
            result["author"] = author
        return result
    return {}


def infer_collection_author(*titles: object) -> Optional[str]:
    return infer_collection_metadata(*titles).get("author")


def infer_collection_structure(*titles: object) -> Optional[str]:
    return infer_collection_metadata(*titles).get("primary_structure")


def _looks_like_person_name(value: str) -> bool:
    if not 2 <= len(value) <= 16:
        return False
    if re.search(r"[0-9×*?？]", value):
        return False
    return not any(term in value for term in _NON_PERSON_CREATOR_TERMS)
