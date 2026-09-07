"""《马克思恩格斯全集》中文一版、二版的卷次—年份对照表。

这是特定语料的事实数据而非通用推断规则：按卷号（及分册）给出出版年与版本
题名，供题录探测在识别出该丛书后直接查表补全。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Dict, Tuple

from .bibliographic_values import _CHINESE_DIGITS


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
