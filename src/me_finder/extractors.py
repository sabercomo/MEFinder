"""Corpus extractors for DOCX and legacy DOC files."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .normalization import (
    cn_volume_number,
    compact_text,
    normalize_text,
    parse_int_label,
    punctuationless_text,
    split_sentences,
    trim_for_display,
)


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"w": W_NS, "r": R_NS}


def w_tag(name: str) -> str:
    return f"{{{W_NS}}}{name}"


def w_attr(element: Optional[ET.Element], name: str) -> Optional[str]:
    if element is None:
        return None
    return element.get(w_tag(name))


def text_only(element: ET.Element) -> str:
    return "".join(t.text or "" for t in element.findall(".//w:t", NS)).strip()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def volume_number_from_name(name: str) -> int:
    match = re.search(r"第\s*(\d+)\s*卷", name)
    if match:
        return int(match.group(1))
    raise ValueError(f"Cannot determine volume number from {name!r}")


def is_marx_engels_volume_name(name: str) -> bool:
    """Return whether a Word file is one of the legacy MEWJ volume sources."""

    compact_name = re.sub(r"\s+", "", name)
    if not any(
        marker in compact_name
        for marker in ("马恩文集", "马克思恩格斯文集")
    ):
        return False
    try:
        volume_number_from_name(name)
    except ValueError:
        return False
    return True


def page_display(start: Optional[str], end: Optional[str] = None) -> Optional[str]:
    if not start:
        return None
    if end and end != start:
        return f"{start}-{end}"
    return str(start)


def is_index_title(title: str) -> bool:
    return any(token in title for token in ("索引", "注释", "插图", "目录"))


def source_file_record(path: Path, root: Path) -> Dict[str, object]:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    volume_number = volume_number_from_name(path.name)
    return {
        "source_file_id": f"source-{volume_number:02d}",
        "relative_path": str(resolved_path.relative_to(resolved_root)).replace("\\", "/"),
        "volume_number": volume_number,
        "file_format": path.suffix.lower().lstrip("."),
        "container_format": "openxml_zip" if path.suffix.lower() == ".docx" else "ole_cfb",
        "file_name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "last_modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def generic_docx_source_file_record(path: Path, root: Path) -> Dict[str, object]:
    """Build a stable catalog record for a standalone, non-volume DOCX."""

    resolved_path = path.resolve()
    resolved_root = root.resolve()
    digest = file_sha256(path)
    identity = hashlib.sha256(
        f"{path.name}\0{digest}".encode("utf-8", "ignore")
    ).hexdigest()[:32]
    try:
        relative_path = resolved_path.relative_to(resolved_root).as_posix()
    except ValueError:
        relative_path = resolved_path.as_posix()
    title = re.sub(
        r"\s+\(imported-[0-9a-f]{8}\)$", "", path.stem, flags=re.I
    ).strip() or path.stem
    return {
        "source_file_id": f"docx-{identity}",
        "source_type": "word",
        "relative_path": relative_path,
        "volume_number": None,
        "file_format": "docx",
        "container_format": "openxml_zip",
        "file_name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
        "last_modified": datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat(),
        "display_title": title,
        "document_title": title,
        "title": title,
        "document_type": "book",
        "bibliographic_metadata": {
            "title": title,
            "document_type": "book",
            "metadata_status": "partial",
            "metadata_source": "file_name",
            "metadata_confidence": 0.35,
            "metadata_missing_fields": ["author", "publisher", "publish_year"],
        },
    }


def volume_record(volume_number: int, source_file_id: str, file_name: str) -> Dict[str, object]:
    if volume_number <= 4:
        structure = "article_collection"
    elif volume_number in {5, 6, 7}:
        structure = "monograph"
    elif volume_number == 8:
        structure = "manuscript_selection"
    elif volume_number == 10:
        structure = "letters"
    else:
        structure = "mixed"
    return {
        "volume_id": f"MEWJ-{volume_number:02d}",
        "corpus_title": "马克思恩格斯文集",
        "display_title": f"《马克思恩格斯文集》第{volume_number}卷",
        "volume_number": volume_number,
        "version_info": infer_version_info(file_name),
        "primary_structure": structure,
        "source_file_id": source_file_id,
    }


def infer_version_info(file_name: str) -> str:
    bits = []
    if "二版全集" in file_name:
        match = re.search(r"二版全集\s*(\d+)\s*卷", file_name)
        bits.append(f"文件名标注：二版全集{match.group(1)}卷" if match else "文件名标注：二版全集")
    date_match = re.search(r"20\d{2}[.\[\]0-9]*", file_name)
    if date_match:
        bits.append(f"文件名日期：{date_match.group(0)}")
    return "；".join(bits) if bits else "文件名未标注明确版本"


def parse_toc_entry_text(raw_text: str, volume_number: int) -> Optional[Dict[str, object]]:
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    if not text or "目录" == text.replace(" ", ""):
        return None
    leader_match = re.search(r"([…\.·•。．]{3,}|[-—_]{4,})\s*(\d{1,4})(?:\s*[—\-–]\s*(\d{1,4}))?\s*$", text)
    if not leader_match:
        return None
    page_start, page_end = leader_match.group(2), leader_match.group(3) or leader_match.group(2)
    title = text[: leader_match.start()].strip(" .·•。．…-—_")
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None
    author = None
    author_patterns = [
        r"^(卡\s*[·.]\s*马克思和弗\s*[·.]\s*恩格斯)\s+",
        r"^(卡\s*[·.]\s*马克思)\s+",
        r"^(弗\s*[·.]\s*恩格斯)\s+",
        r"^(马克思和恩格斯)\s+",
        r"^(马克思)\s+",
        r"^(恩格斯)\s+",
    ]
    for pattern in author_patterns:
        match = re.match(pattern, title)
        if match:
            author = normalize_author(match.group(1))
            title = title[match.end() :].strip()
            break
    if not title:
        return None
    is_major = bool(author) or "卷说明" in title or is_index_title(title)
    if volume_number == 10 and re.match(r"^\d+\.", title):
        is_major = True
    return {
        "raw_text": raw_text,
        "title": title,
        "author": author,
        "page_start": page_start,
        "page_end": page_end,
        "page_start_int": parse_int_label(page_start),
        "page_end_int": parse_int_label(page_end),
        "is_major": is_major,
        "parse_confidence": 0.8 if is_major else 0.65,
    }


def normalize_author(author: str) -> str:
    return re.sub(r"\s+", "", author).replace(".", "·")


def work_id(volume_number: int, order: int) -> str:
    return f"MEWJ-{volume_number:02d}-W{order:04d}"


def make_work(entry: Dict[str, object], volume_number: int, order: int, source: str) -> Dict[str, object]:
    title = str(entry.get("title") or "未识别文献")
    author = canonical_author_for_title(title, entry.get("author"))
    return {
        "work_id": work_id(volume_number, order),
        "volume_id": f"MEWJ-{volume_number:02d}",
        "parent_work_id": None,
        "work_order": order,
        "title": title,
        "subtitle": None,
        "author_label": author,
        "date_label": None,
        "title_source": source,
        "boundary_source": "toc_page_range" if entry.get("page_start") else "heading_match",
        "toc_page_start": entry.get("page_start"),
        "toc_page_end": entry.get("page_end"),
        "confidence": entry.get("parse_confidence", 0.5),
        "notes": "",
    }


def canonical_author_for_title(title: str, author: object) -> object:
    """Patch obvious co-authored works when noisy legacy DOC text loses a name."""

    normalized_title = punctuationless_text(title)
    coauthored_markers = [
        "共产党宣言",
        "神圣家族",
        "德意志意识形态",
        "关于波兰的演说",
    ]
    if any(marker in normalized_title for marker in coauthored_markers):
        return "卡·马克思、弗·恩格斯"
    return author


def choose_work_for_page(works: Sequence[Dict[str, object]], page_number: Optional[int]) -> Optional[Dict[str, object]]:
    if page_number is None:
        return None
    selected = None
    for work in works:
        start = parse_int_label(work.get("toc_page_start"))
        end = parse_int_label(work.get("toc_page_end")) or start
        if start is None:
            continue
        if end is None:
            end = start
        if start <= page_number <= end:
            selected = work
    return selected


def enrich_paragraph_text(record: Dict[str, object]) -> None:
    text = str(record.get("text_raw") or "")
    record["normalized_text"] = normalize_text(text)
    record["compact_text"] = compact_text(text)
    record["plain_text"] = punctuationless_text(text)
    record["sentences"] = split_sentences(text)
    record["text_hash"] = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def extract_docx(path: Path, root: Path) -> Dict[str, object]:
    source = source_file_record(path, root)
    volume_number = int(source["volume_number"])
    volume = volume_record(volume_number, str(source["source_file_id"]), path.name)
    paragraphs: List[Dict[str, object]] = []
    toc_entries: List[Dict[str, object]] = []

    with zipfile.ZipFile(str(path)) as archive:
        styles = read_docx_styles(archive)
        doc = ET.fromstring(archive.read("word/document.xml"))
        xml_paragraphs = doc.findall(".//w:p", NS)
        section_spans = build_docx_section_spans(xml_paragraphs, doc)
        section_for_para = {}
        for span in section_spans:
            for i in range(span["start_para"], span["end_para"] + 1):
                section_for_para[i] = span

        toc_mode = False
        for index, paragraph in enumerate(xml_paragraphs):
            text = text_only(paragraph)
            p_pr = paragraph.find("w:pPr", NS)
            p_style = p_pr.find("w:pStyle", NS) if p_pr is not None else None
            jc = p_pr.find("w:jc", NS) if p_pr is not None else None
            style_id = w_attr(p_style, "val")
            style_name = styles.get(style_id, style_id)
            align = w_attr(jc, "val")
            span = section_for_para.get(index, {})
            compact_no_space = re.sub(r"\s+", "", text)
            if compact_no_space == "目录":
                toc_mode = True
            if toc_mode and compact_no_space in {"插图", "插图目录"}:
                toc_mode = False
            toc_entry = parse_toc_entry_text(text, volume_number) if toc_mode else None
            if toc_entry:
                toc_entry_id = f"MEWJ-{volume_number:02d}-TOC{len(toc_entries)+1:04d}"
                toc_entry["toc_entry_id"] = toc_entry_id
                toc_entry["volume_id"] = volume["volume_id"]
                toc_entry["paragraph_index"] = index
                toc_entries.append(toc_entry)
            paragraph_record = {
                "paragraph_id": f"MEWJ-{volume_number:02d}-P{index:06d}",
                "source_file_id": source["source_file_id"],
                "volume_id": volume["volume_id"],
                "volume_number": volume_number,
                "work_id": None,
                "work_title": None,
                "author_label": None,
                "paragraph_index": index,
                "section_index": span.get("section_index"),
                "text_raw": text,
                "style_name": style_name,
                "alignment": align,
                "font_summary": summarize_docx_fonts(paragraph),
                "is_title_candidate": is_docx_title_candidate(text, style_name, align),
                "is_toc_entry": bool(toc_entry) or toc_mode,
                "is_index_entry": False,
                "original_page_start": span.get("page_label"),
                "original_page_end": span.get("page_label"),
                "page_source_type": "section_break_inferred" if span.get("page_label") else "unknown",
                "page_confidence": 0.55 if span.get("page_label") else 0.0,
                "page_display": span.get("page_label"),
                "original_file_name": path.name,
                "eligible_for_search": bool(text) and not toc_mode,
            }
            enrich_paragraph_text(paragraph_record)
            paragraphs.append(paragraph_record)

    major_entries = [entry for entry in toc_entries if entry.get("is_major")]
    works = [make_work(entry, volume_number, i + 1, "toc") for i, entry in enumerate(major_entries)]
    fallback_work = {
        "work_id": work_id(volume_number, 0),
        "volume_id": volume["volume_id"],
        "parent_work_id": None,
        "work_order": 0,
        "title": f"第{cn_volume_number(volume_number)}卷未分组文本",
        "subtitle": None,
        "author_label": None,
        "date_label": None,
        "title_source": "fallback",
        "boundary_source": "fallback",
        "toc_page_start": None,
        "toc_page_end": None,
        "confidence": 0.2,
        "notes": "未能按目录页码归入具体文献。",
    }
    works.insert(0, fallback_work)
    for paragraph in paragraphs:
        page_int = parse_int_label(paragraph.get("original_page_start"))
        assigned = choose_work_for_page(works[1:], page_int) or fallback_work
        paragraph["work_id"] = assigned["work_id"]
        paragraph["work_title"] = assigned["title"]
        paragraph["author_label"] = assigned.get("author_label")
        paragraph["is_index_entry"] = is_index_title(str(assigned["title"]))
        if paragraph["is_index_entry"] or paragraph["is_toc_entry"]:
            paragraph["eligible_for_search"] = False
    return {
        "source_file": source,
        "volume": volume,
        "works": works,
        "toc_entries": toc_entries,
        "paragraphs": paragraphs,
        "page_anchors": make_page_anchors_from_paragraphs(paragraphs, volume["volume_id"], source["source_file_id"]),
        "audit_issues": [
            {
                "severity": "warning",
                "issue_type": "page_unverified",
                "message": "DOCX 页码来自分节推断，需按 PAGE_NUMBER_STRATEGY.md 抽样验证后才能视为可靠原书页码。",
                "source_file_id": source["source_file_id"],
            }
        ],
    }


def extract_generic_docx(path: Path, root: Path) -> Dict[str, object]:
    """Extract a standalone DOCX without inventing a collection volume number."""

    source = generic_docx_source_file_record(path, root)
    source_id = str(source["source_file_id"])
    title = str(source["document_title"])
    volume_id = f"{source_id}-document"
    work_id_value = f"{source_id}-work"
    volume = {
        "volume_id": volume_id,
        "source_file_id": source_id,
        "source_type": "word",
        "display_title": title,
        "document_title": title,
        "volume_number": None,
        "primary_structure": "standalone_document",
        "document_type": "book",
    }
    work = {
        "work_id": work_id_value,
        "volume_id": volume_id,
        "source_type": "word",
        "parent_work_id": None,
        "work_order": 1,
        "title": title,
        "subtitle": None,
        "author_label": None,
        "date_label": None,
        "title_source": "file_name",
        "boundary_source": "whole_document",
        "toc_page_start": None,
        "toc_page_end": None,
        "confidence": 0.35,
        "notes": "独立 DOCX，未从文件名推定文集卷号。",
    }
    paragraphs: List[Dict[str, object]] = []

    with zipfile.ZipFile(str(path)) as archive:
        styles = read_docx_styles(archive)
        doc = ET.fromstring(archive.read("word/document.xml"))
        xml_paragraphs = doc.findall(".//w:p", NS)
        section_for_para: Dict[int, Dict[str, object]] = {}
        for span in build_docx_section_spans(xml_paragraphs, doc):
            for paragraph_index in range(
                int(span["start_para"]), int(span["end_para"]) + 1
            ):
                section_for_para[paragraph_index] = span

        for index, paragraph in enumerate(xml_paragraphs):
            text = text_only(paragraph)
            p_pr = paragraph.find("w:pPr", NS)
            p_style = p_pr.find("w:pStyle", NS) if p_pr is not None else None
            jc = p_pr.find("w:jc", NS) if p_pr is not None else None
            style_id = w_attr(p_style, "val")
            style_name = styles.get(style_id, style_id)
            align = w_attr(jc, "val")
            span = section_for_para.get(index, {})
            paragraph_record: Dict[str, object] = {
                "paragraph_id": f"{source_id}-P{index:06d}",
                "source_file_id": source_id,
                "source_type": "word",
                "volume_id": volume_id,
                "volume_number": None,
                "volume_display": title,
                "work_id": work_id_value,
                "work_title": title,
                "document_title": title,
                "author_label": None,
                "paragraph_index": index,
                "section_index": span.get("section_index"),
                "text_raw": text,
                "style_name": style_name,
                "alignment": align,
                "font_summary": summarize_docx_fonts(paragraph),
                "is_title_candidate": is_docx_title_candidate(
                    text, style_name, align
                ),
                "is_toc_entry": False,
                "is_index_entry": False,
                "original_page_start": span.get("page_label"),
                "original_page_end": span.get("page_label"),
                "page_source_type": (
                    "section_break_inferred" if span.get("page_label") else "unknown"
                ),
                "page_confidence": 0.55 if span.get("page_label") else 0.0,
                "page_display": span.get("page_label"),
                "original_file_name": path.name,
                "eligible_for_search": bool(text),
            }
            enrich_paragraph_text(paragraph_record)
            paragraphs.append(paragraph_record)

    return {
        "source_file": source,
        "volume": volume,
        "works": [work],
        "toc_entries": [],
        "paragraphs": paragraphs,
        "page_anchors": make_page_anchors_from_paragraphs(
            paragraphs, volume_id, source_id
        ),
        "audit_issues": [
            {
                "severity": "warning",
                "issue_type": "page_unverified",
                "message": (
                    "独立 DOCX 不依赖文件名卷号；页码仅来自分节推断，"
                    "未出现分节页码时保持未校验。"
                ),
                "source_file_id": source_id,
            }
        ],
    }


def read_docx_styles(archive: zipfile.ZipFile) -> Dict[Optional[str], str]:
    try:
        styles_root = ET.fromstring(archive.read("word/styles.xml"))
    except KeyError:
        return {}
    styles: Dict[Optional[str], str] = {}
    for style in styles_root.findall("w:style", NS):
        style_id = style.get(w_tag("styleId"))
        name = style.find("w:name", NS)
        styles[style_id] = w_attr(name, "val") or style_id or ""
    return styles


def clean_xml_attrib(element: Optional[ET.Element]) -> Dict[str, str]:
    if element is None:
        return {}
    return {key.split("}")[-1]: value for key, value in element.attrib.items()}


def build_docx_section_spans(xml_paragraphs: Sequence[ET.Element], doc: ET.Element) -> List[Dict[str, object]]:
    spans: List[Dict[str, object]] = []
    start_para = 0
    current_page: Optional[int] = None
    section_index = 0
    for index, paragraph in enumerate(xml_paragraphs):
        sect = paragraph.find(".//w:sectPr", NS)
        if sect is None:
            continue
        pg = clean_xml_attrib(sect.find("w:pgNumType", NS))
        start_value = parse_int_label(pg.get("start"))
        if start_value is not None:
            current_page = start_value
        elif current_page is not None:
            current_page += 1
        page_label = str(current_page) if current_page is not None else None
        type_value = w_attr(sect.find("w:type", NS), "val") or "nextPage"
        spans.append(
            {
                "section_index": section_index,
                "start_para": start_para,
                "end_para": index,
                "page_label": page_label,
                "section_type": type_value,
            }
        )
        start_para = index + 1
        section_index += 1
    if start_para < len(xml_paragraphs):
        if current_page is not None:
            current_page += 1
        spans.append(
            {
                "section_index": section_index,
                "start_para": start_para,
                "end_para": len(xml_paragraphs) - 1,
                "page_label": str(current_page) if current_page is not None else None,
                "section_type": "final",
            }
        )
    return spans


def summarize_docx_fonts(paragraph: ET.Element) -> Dict[str, object]:
    fonts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    bold_runs = 0
    run_count = 0
    for run in paragraph.findall("w:r", NS):
        run_count += 1
        r_pr = run.find("w:rPr", NS)
        if r_pr is None:
            continue
        if r_pr.find("w:b", NS) is not None or r_pr.find("w:bCs", NS) is not None:
            bold_runs += 1
        size = r_pr.find("w:sz", NS)
        if size is not None and w_attr(size, "val"):
            sizes[w_attr(size, "val")] += 1
        r_fonts = r_pr.find("w:rFonts", NS)
        if r_fonts is not None:
            for key in ("ascii", "eastAsia", "hAnsi", "cs"):
                value = r_fonts.get(w_tag(key))
                if value:
                    fonts[value] += 1
    return {
        "fonts": fonts.most_common(3),
        "sizes": sizes.most_common(3),
        "bold_runs": bold_runs,
        "run_count": run_count,
    }


def is_docx_title_candidate(text: str, style_name: Optional[str], align: Optional[str]) -> bool:
    if not text or len(text) > 100:
        return False
    style = (style_name or "").lower()
    if "heading" in style or "标题" in (style_name or "") or "subtitle" in style:
        return True
    if align == "center" and len(text) <= 80:
        return True
    return False


END_OF_CHAIN = 0xFFFFFFFE
FREE_SECTOR = 0xFFFFFFFF


class CompoundFile:
    """Minimal OLE Compound File reader for WordDocument stream inspection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        if self.data[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
            raise ValueError(f"{path} is not an OLE compound file")
        self.sector_size = 1 << struct.unpack_from("<H", self.data, 0x1E)[0]
        self.num_fat = struct.unpack_from("<I", self.data, 0x2C)[0]
        self.first_dir = struct.unpack_from("<I", self.data, 0x30)[0]
        self.cutoff = struct.unpack_from("<I", self.data, 0x38)[0]
        self.difat = [x for x in struct.unpack_from("<109I", self.data, 0x4C) if x != FREE_SECTOR]
        self.fat: List[int] = []
        for sid in self.difat[: self.num_fat]:
            self.fat.extend(struct.unpack(f"<{self.sector_size // 4}I", self.sector(sid)))
        self.dir_entries: List[Dict[str, object]] = []
        self._read_directory()

    def sector(self, sid: int) -> bytes:
        start = (sid + 1) * self.sector_size
        return self.data[start : start + self.sector_size]

    def chain(self, start: int) -> List[int]:
        out: List[int] = []
        sid = start
        seen = set()
        while sid not in (END_OF_CHAIN, FREE_SECTOR) and sid < len(self.fat) and sid not in seen:
            seen.add(sid)
            out.append(sid)
            sid = self.fat[sid]
        return out

    def _read_directory(self) -> None:
        raw = b"".join(self.sector(sid) for sid in self.chain(self.first_dir))
        for offset in range(0, len(raw), 128):
            entry = raw[offset : offset + 128]
            if len(entry) < 128:
                continue
            name_len = struct.unpack_from("<H", entry, 64)[0]
            name = entry[: max(0, name_len - 2)].decode("utf-16le", "replace") if name_len >= 2 else ""
            entry_type = entry[66]
            start = struct.unpack_from("<I", entry, 116)[0]
            size = struct.unpack_from("<I", entry, 120)[0]
            if name or entry_type:
                self.dir_entries.append({"name": name, "type": entry_type, "start": start, "size": size})

    def stream(self, name: str) -> bytes:
        for entry in self.dir_entries:
            if entry["name"] == name and int(entry["size"]) >= self.cutoff:
                return b"".join(self.sector(sid) for sid in self.chain(int(entry["start"])))[: int(entry["size"])]
        return b""


def utf16_text_runs(data: bytes, min_chars: int = 8) -> List[str]:
    runs: List[str] = []
    cur: List[str] = []
    for i in range(0, len(data) - 1, 2):
        code = data[i] | (data[i + 1] << 8)
        ch = chr(code)
        ok = (
            ch in "\t\r\n"
            or 0x20 <= code <= 0x7E
            or 0x3400 <= code <= 0x9FFF
            or 0x2000 <= code <= 0x303F
            or 0xFF00 <= code <= 0xFFEF
        )
        if ok:
            cur.append(ch)
            continue
        value = "".join(cur).strip()
        if is_useful_doc_run(value, min_chars):
            runs.append(re.sub(r"\s+", " ", value))
        cur = []
    value = "".join(cur).strip()
    if is_useful_doc_run(value, min_chars):
        runs.append(re.sub(r"\s+", " ", value))
    return runs


def is_useful_doc_run(value: str, min_chars: int) -> bool:
    if len(value) < min_chars:
        return False
    chinese = sum(1 for ch in value if "\u3400" <= ch <= "\u9fff")
    if chinese == 0:
        return False
    return chinese / max(len(value), 1) > 0.08


def extract_doc(path: Path, root: Path) -> Dict[str, object]:
    source = source_file_record(path, root)
    volume_number = int(source["volume_number"])
    volume = volume_record(volume_number, str(source["source_file_id"]), path.name)
    cfb = CompoundFile(path)
    core = cfb.stream("WordDocument") + cfb.stream("0Table") + cfb.stream("1Table") + cfb.stream("Data")
    runs = utf16_text_runs(path.read_bytes())
    toc_entries = extract_doc_toc_entries(runs, volume_number)
    major_entries = [entry for entry in toc_entries if entry.get("is_major")]
    works = [make_work(entry, volume_number, i + 1, "toc") for i, entry in enumerate(major_entries)]
    fallback_work = {
        "work_id": work_id(volume_number, 0),
        "volume_id": volume["volume_id"],
        "parent_work_id": None,
        "work_order": 0,
        "title": f"第{cn_volume_number(volume_number)}卷未分组文本",
        "subtitle": None,
        "author_label": None,
        "date_label": None,
        "title_source": "fallback",
        "boundary_source": "fallback",
        "toc_page_start": None,
        "toc_page_end": None,
        "confidence": 0.2,
        "notes": "旧版 DOC 暂用 OLE UTF-16 文本流抽取，未获得段落级页码。",
    }
    works.insert(0, fallback_work)
    paragraphs: List[Dict[str, object]] = []
    body_runs = select_doc_body_runs(runs, volume_number)
    current_work = fallback_work
    normalized_major_titles = [(punctuationless_text(str(w["title"])), w) for w in works[1:]]
    paragraph_index = 0
    for run in body_runs:
        for piece in split_doc_run_to_paragraphs(run):
            plain_piece = punctuationless_text(piece)
            for title_plain, work in normalized_major_titles:
                if title_plain and title_plain in plain_piece[: max(120, len(title_plain) + 20)]:
                    current_work = work
                    break
            paragraph_record = {
                "paragraph_id": f"MEWJ-{volume_number:02d}-P{paragraph_index:06d}",
                "source_file_id": source["source_file_id"],
                "volume_id": volume["volume_id"],
                "volume_number": volume_number,
                "work_id": current_work["work_id"],
                "work_title": current_work["title"],
                "author_label": current_work.get("author_label"),
                "paragraph_index": paragraph_index,
                "section_index": None,
                "text_raw": piece,
                "style_name": None,
                "alignment": None,
                "font_summary": None,
                "is_title_candidate": len(piece) <= 80,
                "is_toc_entry": False,
                "is_index_entry": is_index_title(str(current_work["title"])),
                "original_page_start": None,
                "original_page_end": None,
                "page_source_type": "toc_range_bound" if current_work.get("toc_page_start") else "unknown",
                "page_confidence": 0.35 if current_work.get("toc_page_start") else 0.0,
                "page_display": page_display(current_work.get("toc_page_start"), current_work.get("toc_page_end")),
                "original_file_name": path.name,
                "eligible_for_search": bool(piece) and not is_index_title(str(current_work["title"])),
            }
            enrich_paragraph_text(paragraph_record)
            paragraphs.append(paragraph_record)
            paragraph_index += 1
    return {
        "source_file": source,
        "volume": volume,
        "works": works,
        "toc_entries": toc_entries,
        "paragraphs": paragraphs,
        "page_anchors": [],
        "audit_issues": [
            {
                "severity": "warning",
                "issue_type": "legacy_doc_page_unverified",
                "message": "旧版 DOC 段落级页码未解析；结果页码仅为目录范围约束，不能视为精确原书页码。",
                "source_file_id": source["source_file_id"],
            },
            {
                "severity": "info",
                "issue_type": "ole_field_probe",
                "message": f"核心流 PAGE 字段痕迹：{core.upper().count('PAGE'.encode('utf-16le'))}",
                "source_file_id": source["source_file_id"],
            },
        ],
    }


def extract_doc_toc_entries(runs: Sequence[str], volume_number: int) -> List[Dict[str, object]]:
    entries: List[Dict[str, object]] = []
    seen_toc = False
    for run in runs[:120]:
        if "目 录" in run or "目录" in run:
            seen_toc = True
        if not seen_toc:
            continue
        if run.startswith(f"第{cn_volume_number(volume_number)}卷说明") and "…" not in run and "." not in run[:80]:
            break
        for match in re.finditer(r"(.{2,100}?)(?:…|\.|．){4,}\s*(\d{1,4})(?:\s*[—\-–]\s*(\d{1,4}))?", run):
            raw_title = match.group(1).strip()
            raw_title = re.sub(r"^.*?((?:卡\s*[·.]\s*马克思|弗\s*[·.]\s*恩格斯|马克思和恩格斯|马克思|恩格斯|第[一二三四五六七八九十]+卷说明|注释|人名索引|文献索引|名目索引).*)$", r"\1", raw_title)
            parsed = parse_toc_entry_text(
                f"{raw_title} {'…' * 8} {match.group(2)}" + (f"-{match.group(3)}" if match.group(3) else ""),
                volume_number,
            )
            if parsed:
                parsed["toc_entry_id"] = f"MEWJ-{volume_number:02d}-TOC{len(entries)+1:04d}"
                parsed["volume_id"] = f"MEWJ-{volume_number:02d}"
                parsed["paragraph_index"] = None
                entries.append(parsed)
    deduped: List[Dict[str, object]] = []
    seen = set()
    for entry in entries:
        key = (entry["title"], entry.get("page_start"), entry.get("page_end"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def select_doc_body_runs(runs: Sequence[str], volume_number: int) -> List[str]:
    cn = cn_volume_number(volume_number)
    marker = f"第{cn}卷说明"
    start = None
    seen_toc = False
    for i, run in enumerate(runs):
        if "目 录" in run or "目录" in run:
            seen_toc = True
        if seen_toc and run.startswith(marker) and "…" not in run[:160] and "." not in run[:80]:
            start = i
            break
    if start is None:
        for i, run in enumerate(runs):
            if run.startswith(marker):
                start = i
                break
    if start is None:
        start = min(8, len(runs))
    return list(runs[start:])


def split_doc_run_to_paragraphs(run: str) -> List[str]:
    run = re.sub(r"\s+", " ", run or "").strip()
    if not run:
        return []
    pieces = split_sentences(run)
    paragraphs: List[str] = []
    buffer = ""
    for sentence in pieces:
        if not buffer:
            buffer = sentence
        elif len(buffer) + len(sentence) <= 360:
            buffer += sentence
        else:
            paragraphs.append(buffer.strip())
            buffer = sentence
    if buffer:
        paragraphs.append(buffer.strip())
    return paragraphs


def make_page_anchors_from_paragraphs(
    paragraphs: Sequence[Dict[str, object]], volume_id: object, source_file_id: object
) -> List[Dict[str, object]]:
    anchors: Dict[str, Dict[str, object]] = {}
    for paragraph in paragraphs:
        label = paragraph.get("original_page_start")
        if not label:
            continue
        existing = anchors.get(str(label))
        if existing is None:
            anchors[str(label)] = {
                "page_anchor_id": f"{volume_id}-PAGE-{label}",
                "volume_id": volume_id,
                "source_file_id": source_file_id,
                "original_page_label": str(label),
                "page_sequence_in_volume": len(anchors) + 1,
                "section_index": paragraph.get("section_index"),
                "start_paragraph_id": paragraph.get("paragraph_id"),
                "end_paragraph_id": paragraph.get("paragraph_id"),
                "anchor_source_type": paragraph.get("page_source_type"),
                "anchor_text": trim_for_display(str(paragraph.get("text_raw") or ""), 120),
                "confidence": paragraph.get("page_confidence", 0.0),
                "validated_by": "automatic",
                "validation_notes": "MVP 自动分节推断，尚未人工抽样验证。",
            }
        else:
            existing["end_paragraph_id"] = paragraph.get("paragraph_id")
    return list(anchors.values())


def extract_source(path: Path, root: Path) -> Dict[str, object]:
    if path.suffix.lower() == ".docx":
        if is_marx_engels_volume_name(path.name):
            return extract_docx(path, root)
        return extract_generic_docx(path, root)
    if path.suffix.lower() == ".doc":
        return extract_doc(path, root)
    raise ValueError(f"Unsupported corpus file: {path}")
