"""PyMuPDF page text and geometry extraction for native PDF imports."""

from __future__ import annotations

import re
from pathlib import Path

from ..normalization import NUMERIC_SUPERSCRIPTS
from .extraction_types import PDFTextPage
from .paragraphs import attach_page_block_offsets


def _text_with_numeric_powers(lines: list, superscript_flag: int) -> str:
    pieces = []
    for line in lines:
        spans = line["spans"]
        for index, span in enumerate(spans):
            text = span["text"]
            if (
                index
                and line["dir"] == (1.0, 0.0)
                and span["flags"] & superscript_flag
                and re.fullmatch(r"[+−-]?[0-9]+", text)
            ):
                base = spans[index - 1]
                last = base["text"][-1:]
                # MuPDF can also flag a raised fraction numerator or a
                # baseline glyph after a subscript. Require an attached base.
                if (last.isalnum() or last in (")", "]", "|")) and span["origin"][1] < base["origin"][1]:
                    text = text.translate(NUMERIC_SUPERSCRIPTS)
            pieces.append(text)
        pieces.append("\n")
    return "".join(pieces)


def extract_pages_with_pymupdf(path: Path, fitz) -> list[PDFTextPage]:
    """Extract text and source rectangles together, preserving numeric superscripts."""

    doc = fitz.open(str(path))
    pages: list[PDFTextPage] = []
    try:
        for index in range(len(doc)):
            page = doc.load_page(index)
            page_rect = page.rect
            label = page.get_label() if getattr(page, "get_label", None) else None
            text_page = page.get_textpage(flags=fitz.TEXTFLAGS_TEXT)
            raw_text = text_page.extractText()
            block_records = []
            for x0, y0, x1, y1, text, number, _ in text_page.extractBLOCKS():
                block_records.append(
                    {
                        "block_index": number,
                        "bbox": [x0, y0, x1, y1],
                        "text": text,
                        "bbox_normalized": [
                            x0 / max(float(page_rect.width), 1.0),
                            y0 / max(float(page_rect.height), 1.0),
                            x1 / max(float(page_rect.width), 1.0),
                            y1 / max(float(page_rect.height), 1.0),
                        ],
                    }
                )
            attach_page_block_offsets(raw_text, block_records)
            layout = {block["number"]: block["lines"] for block in text_page.extractDICT()["blocks"]}
            for block in block_records:
                if "page_char_start" not in block:
                    continue
                start, end = int(block["page_char_start"]), int(block["page_char_end"])
                source_chars = raw_text[start:end]
                lines = layout[block["block_index"]]
                original = "".join("".join(s["text"] for s in line["spans"]) + "\n" for line in lines)
                # Only use matching glyph evidence; extraction modes can
                # disagree on malformed text. Substitution is one codepoint
                # for one, so source offsets and boxes remain valid.
                if source_chars != original:
                    continue
                text = _text_with_numeric_powers(lines, fitz.TEXT_FONT_SUPERSCRIPT)
                if text != original:
                    raw_text = raw_text[:start] + text + raw_text[end:]
                    block["text"] = text
            pages.append(
                PDFTextPage(
                    pdf_page_index=index,
                    pdf_page_label=str(label) if label else None,
                    raw_text=raw_text,
                    blocks=block_records,
                    parser="pymupdf",
                    parser_version=str(getattr(fitz, "VersionBind", "")),
                    page_width=float(page_rect.width),
                    page_height=float(page_rect.height),
                )
            )
    finally:
        doc.close()
    return pages
