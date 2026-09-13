"""Citation information: bibliographic metadata, copy text and page labels."""

from __future__ import annotations

from typing import Dict, Tuple

from .page_display import resolve_citation_page


def citation_metadata(
    paragraph: Dict[str, object],
    source_type: str,
    sources_by_id: Dict[str, Dict[str, object]],
    volumes_by_id: Dict[str, Dict[str, object]],
    works_by_id: Dict[str, Dict[str, object]],
) -> Dict[str, object]:
    metadata: Dict[str, object] = {}
    source_record = sources_by_id.get(str(paragraph.get("source_file_id")))
    for record in (
        source_record,
        volumes_by_id.get(str(paragraph.get("volume_id"))),
        works_by_id.get(str(paragraph.get("work_id"))),
        paragraph,
    ):
        if isinstance(record, dict):
            for key, value in record.items():
                if value not in (None, ""):
                    metadata[key] = value
    if isinstance(source_record, dict):
        bibliographic = source_record.get("bibliographic_metadata")
        if not isinstance(bibliographic, dict):
            bibliographic = source_record
        for key in (
            "title",
            "author",
            "country",
            "translator",
            "publisher",
            "publish_place",
            "publish_year",
            "isbn",
            "journal_name",
            "volume",
            "issue",
            "page_range",
            "document_type",
            "metadata_status",
            "metadata_source",
            "metadata_confidence",
            "metadata_evidence",
        ):
            if bibliographic.get(key) not in (None, ""):
                metadata[key] = bibliographic[key]
        if bibliographic.get("title"):
            metadata["document_title"] = bibliographic["title"]
    if source_type == "word" and is_marx_engels_volume(metadata):
        metadata.setdefault("document_type", "marx_engels_collection")
        metadata.setdefault("collection_title", infer_marx_engels_collection_title(metadata))
        if metadata.get("collection_title") == "马克思恩格斯文集":
            metadata.setdefault("publication_place", "北京")
            metadata.setdefault("publisher", "人民出版社")
            metadata.setdefault("publication_year", "2009")
    else:
        metadata.setdefault("document_type", metadata.get("citation_type") or "book")
    metadata.setdefault("author", paragraph.get("author_label"))
    metadata.setdefault("title", paragraph.get("work_title") or paragraph.get("document_title"))
    metadata.setdefault("document_title", paragraph.get("document_title") or paragraph.get("work_title"))
    return metadata


def infer_marx_engels_collection_title(metadata: Dict[str, object]) -> str:
    text = "".join(
        str(metadata.get(key) or "")
        for key in ("collection_title", "document_title", "display_title", "title", "file_name", "original_file_name")
    )
    if "全集" in text:
        return "马克思恩格斯全集"
    if "选集" in text:
        return "马克思恩格斯选集"
    return "马克思恩格斯文集"


def is_marx_engels_volume(record: Dict[str, object]) -> bool:
    return (
        record.get("volume_number") is not None
        and str(record.get("volume_id") or "").upper().startswith("MEWJ-")
    )


def hit_page(paragraph: Dict[str, object], source_type: str, page_display: object) -> Dict[str, object]:
    resolved = resolve_citation_page(paragraph)
    if resolved.verified and resolved.start:
        return {
            "start": resolved.start,
            "end": resolved.end,
            "display": page_display,
        }
    return {
        "display": page_display
        or (
            "引用页码尚未校准"
            if source_type == "pdf"
            else "页码未验证"
        ),
        "uncalibrated": True,
    }


def build_copy_text(
    paragraph: Dict[str, object],
    source_type: str,
    page: str,
    raw: str,
) -> Tuple[str, str]:
    """Return ``(copy_text, volume_display)`` for one assembled result."""

    if source_type == "pdf":
        copy_text = f"{paragraph.get('document_title') or paragraph.get('work_title') or 'PDF 文献'}，{page}：{raw}"
        volume_display = str(paragraph.get("volume_display") or paragraph.get("document_title") or "PDF 文献")
    elif is_marx_engels_volume(paragraph):
        copy_text = f"《马克思恩格斯文集》第{paragraph.get('volume_number')}卷，{paragraph.get('work_title') or '未识别文献'}，{page}：{raw}"
        volume_display = f"《马克思恩格斯文集》第{paragraph.get('volume_number')}卷"
    else:
        volume_display = str(
            paragraph.get("volume_display")
            or paragraph.get("document_title")
            or paragraph.get("work_title")
            or "Word 文献"
        )
        copy_text = f"{volume_display}，{page}：{raw}"
    return copy_text, volume_display
