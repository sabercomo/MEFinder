"""Unified, deterministic PDF page-mapping evidence orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .auto_page_mapping import (
    PageNumberCandidate,
    extract_mineru_page_number_candidates,
    extract_native_pdf_edge_candidates,
    extract_pdf_numeric_bookmark_candidates,
    extract_pdf_page_label_candidates,
    infer_auto_page_mapping,
)


class PageMappingService:
    """Collect all available evidence before fitting one shared page sequence model."""

    def infer(
        self,
        path: Path,
        pages: Sequence[Dict[str, object]],
        *,
        mineru_segments: Optional[Sequence[Dict[str, object]]] = None,
        page_count: Optional[int] = None,
        dry_run: bool = False,
        manual_mapping_present: bool = False,
    ) -> Dict[str, object]:
        label_candidates = extract_pdf_page_label_candidates(Path(path))
        bookmark_candidates = extract_pdf_numeric_bookmark_candidates(Path(path))
        mineru_candidates = extract_mineru_page_number_candidates(mineru_segments or [])
        edge_candidates = extract_native_pdf_edge_candidates(pages)
        candidates: List[PageNumberCandidate] = []
        candidates.extend(label_candidates)
        candidates.extend(bookmark_candidates)
        candidates.extend(mineru_candidates)
        candidates.extend(edge_candidates)
        effective_page_count = int(page_count or 0)
        if not effective_page_count:
            effective_page_count = max([int(page.get("pdf_page_index") or 0) for page in pages] or [-1]) + 1
        result = infer_auto_page_mapping(
            candidates,
            effective_page_count,
            page_texts={int(page.get("pdf_page_index") or 0): str(page.get("text_raw") or "") for page in pages},
        )
        evidence_counts = {
            "pdf_page_labels": len(label_candidates),
            "numeric_bookmarks": len(bookmark_candidates),
            "mineru_candidates": len(mineru_candidates),
            "native_edge_candidates": len(edge_candidates),
        }
        failure_reasons: List[str] = []
        if not label_candidates:
            failure_reasons.append("no_page_labels")
        if not bookmark_candidates:
            failure_reasons.append("no_bookmarks")
        if not mineru_candidates:
            failure_reasons.append("no_mineru_candidates")
        if not edge_candidates:
            failure_reasons.append("no_edge_candidates")
        if not result.get("selected_segments"):
            failure_reasons.append("sequence_not_found")
        selected = [item for item in result.get("selected_segments", []) if isinstance(item, dict)]
        applied = [item for item in result.get("applied_segments", []) if isinstance(item, dict)]
        if manual_mapping_present and not dry_run:
            status = "manual_mapped"
        elif applied:
            status = "auto_mapped_high"
        elif any(item.get("confidence_level") == "medium" for item in selected):
            status = "needs_review"
        elif selected:
            status = "needs_review"
        else:
            status = "auto_mapping_failed"
        result.update(
            {
                "mapping_status": status,
                "failure_reasons": failure_reasons,
                "evidence_counts": evidence_counts,
                "manual_mapping_present": bool(manual_mapping_present),
                "dry_run": bool(dry_run),
            }
        )
        return result
