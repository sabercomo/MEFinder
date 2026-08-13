"""Capability-driven planning and physical, lossless PDF slicing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from ..parser_provider import ProviderCapabilities
from .io_utils import sha256_file


class PDFSlicingError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class SliceRange:
    page_start: int
    page_end: int
    estimated_bytes: int

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1

    @property
    def global_page_offset(self) -> int:
        return self.page_start - 1


@dataclass(frozen=True)
class SliceDescriptor:
    page_start: int
    page_end: int
    global_page_offset: int
    path: Path
    sha256: str
    size_bytes: int
    physical_slice: bool = True

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1


class SlicePlanner:
    """Plan coarse ranges; the slicer enforces actual post-write byte limits."""

    BYTE_ESTIMATE_SAFETY = 0.90

    def plan(
        self,
        *,
        total_pages: int,
        total_bytes: int,
        capabilities: ProviderCapabilities,
        page_byte_estimates: Optional[Sequence[int]] = None,
    ) -> List[SliceRange]:
        if total_pages < 1:
            raise ValueError("total_pages must be positive")
        if total_bytes < 0:
            raise ValueError("total_bytes cannot be negative")
        if page_byte_estimates is not None and len(page_byte_estimates) != total_pages:
            raise ValueError("page_byte_estimates must contain one value per page")
        max_pages = capabilities.max_pages_per_file or total_pages
        max_bytes = capabilities.max_bytes_per_file
        if page_byte_estimates is not None and max_bytes is not None:
            return self._plan_with_page_estimates(
                page_byte_estimates,
                max_pages=max_pages,
                max_bytes=max_bytes,
            )

        target_pages = min(total_pages, max_pages)
        if max_bytes is not None and total_bytes > 0:
            proportional_pages = int(
                (max_bytes * total_pages / total_bytes) * self.BYTE_ESTIMATE_SAFETY
            )
            target_pages = min(target_pages, max(1, proportional_pages))
        ranges: List[SliceRange] = []
        start = 1
        while start <= total_pages:
            end = min(total_pages, start + target_pages - 1)
            estimated = int(total_bytes * ((end - start + 1) / total_pages))
            ranges.append(SliceRange(start, end, estimated))
            start = end + 1
        return ranges

    @staticmethod
    def _plan_with_page_estimates(
        estimates: Sequence[int], *, max_pages: int, max_bytes: int
    ) -> List[SliceRange]:
        ranges: List[SliceRange] = []
        start_index = 0
        current_bytes = 0
        for page_index, raw_size in enumerate(estimates):
            page_bytes = max(0, int(raw_size))
            current_pages = page_index - start_index
            exceeds_pages = current_pages >= max_pages
            exceeds_bytes = current_pages > 0 and current_bytes + page_bytes > max_bytes
            if exceeds_pages or exceeds_bytes:
                ranges.append(
                    SliceRange(start_index + 1, page_index, current_bytes)
                )
                start_index = page_index
                current_bytes = 0
            current_bytes += page_bytes
        ranges.append(SliceRange(start_index + 1, len(estimates), current_bytes))
        return ranges


SliceWriter = Callable[[Path, int, int, Path], None]


class PhysicalPDFSlicer:
    """Write independent sub-PDFs and recursively enforce actual byte limits."""

    def __init__(self, writer: Optional[SliceWriter] = None) -> None:
        self._writer = writer or self._write_with_pymupdf

    def create_slices(
        self,
        source_path: Path,
        ranges: Iterable[SliceRange],
        output_dir: Path,
        *,
        max_bytes_per_file: Optional[int],
    ) -> List[SliceDescriptor]:
        source = Path(source_path)
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        descriptors: List[SliceDescriptor] = []
        for page_range in ranges:
            descriptors.extend(
                self._write_range(
                    source,
                    page_range.page_start,
                    page_range.page_end,
                    output,
                    max_bytes_per_file=max_bytes_per_file,
                )
            )
        return sorted(descriptors, key=lambda item: item.page_start)

    def _write_range(
        self,
        source: Path,
        start: int,
        end: int,
        output: Path,
        *,
        max_bytes_per_file: Optional[int],
    ) -> List[SliceDescriptor]:
        final_path = output / f"slice-{start:06d}-{end:06d}.pdf"
        partial = final_path.with_name(final_path.name + ".partial")
        if final_path.is_file():
            size = final_path.stat().st_size
        else:
            partial.unlink(missing_ok=True)
            try:
                self._writer(source, start, end, partial)
                if not partial.is_file():
                    raise PDFSlicingError("PDF slice writer produced no file")
                with partial.open("rb+") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                partial.replace(final_path)
            except Exception:
                partial.unlink(missing_ok=True)
                raise
            size = final_path.stat().st_size
        if max_bytes_per_file is not None and size > max_bytes_per_file:
            if start == end:
                raise PDFSlicingError(
                    f"physical PDF page {start} alone exceeds provider byte capability"
                )
            final_path.unlink(missing_ok=True)
            midpoint = start + ((end - start) // 2)
            left = self._write_range(
                source,
                start,
                midpoint,
                output,
                max_bytes_per_file=max_bytes_per_file,
            )
            right = self._write_range(
                source,
                midpoint + 1,
                end,
                output,
                max_bytes_per_file=max_bytes_per_file,
            )
            return left + right
        return [
            SliceDescriptor(
                page_start=start,
                page_end=end,
                global_page_offset=start - 1,
                path=final_path,
                sha256=sha256_file(final_path),
                size_bytes=size,
                physical_slice=True,
            )
        ]

    @staticmethod
    def _write_with_pymupdf(source: Path, start: int, end: int, output: Path) -> None:
        try:
            import fitz  # type: ignore
        except Exception as exc:
            raise PDFSlicingError(
                "PyMuPDF is required for physical large-PDF slicing"
            ) from exc
        source_document = fitz.open(str(source))
        target_document = fitz.open()
        try:
            if start < 1 or end > len(source_document) or end < start:
                raise PDFSlicingError(
                    f"invalid physical slice {start}-{end} for {len(source_document)} pages"
                )
            # insert_pdf copies page objects without rendering or OCR.
            target_document.insert_pdf(
                source_document,
                from_page=start - 1,
                to_page=end - 1,
            )
            target_document.save(str(output), garbage=3, deflate=True)
        finally:
            target_document.close()
            source_document.close()


def original_file_descriptor(
    source_path: Path, *, total_pages: int
) -> SliceDescriptor:
    """Represent a provider-compatible small PDF without making a copy."""

    source = Path(source_path)
    return SliceDescriptor(
        page_start=1,
        page_end=total_pages,
        global_page_offset=0,
        path=source,
        sha256=sha256_file(source),
        size_bytes=source.stat().st_size,
        physical_slice=False,
    )
