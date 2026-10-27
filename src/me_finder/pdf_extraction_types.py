"""Shared page records and errors for PDF extraction stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


class PDFExtractionError(RuntimeError):
    pass


@dataclass
class PDFTextPage:
    pdf_page_index: int
    pdf_page_label: Optional[str]
    raw_text: str
    blocks: List[Dict[str, object]]
    parser: str
    parser_version: str
    page_width: Optional[float] = None
    page_height: Optional[float] = None
