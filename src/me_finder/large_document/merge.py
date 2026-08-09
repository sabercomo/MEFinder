"""Pure coverage validation and streaming normalized-result merge."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Mapping, Sequence, Tuple

from ..parser_provider import NormalizedParseResult
from .io_utils import sha256_file


class CoverageValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MergedResult:
    path: Path
    sha256: str
    page_count: int


def _range_values(item: object) -> Tuple[int, int]:
    if isinstance(item, Mapping):
        return int(item["page_start"]), int(item["page_end"])
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return int(item[0]), int(item[1])
    return int(getattr(item, "page_start")), int(getattr(item, "page_end"))


def validate_slice_coverage(
    ranges: Iterable[object],
    total_pages: int,
    *,
    require_input_order: bool = False,
) -> List[Tuple[int, int]]:
    """Return ordered ranges only when they cover exactly ``1..total_pages``."""

    if total_pages < 1:
        raise CoverageValidationError("invalid_total", "total_pages must be positive")
    original = [_range_values(item) for item in ranges]
    if not original:
        raise CoverageValidationError("missing", "no slice ranges were provided")
    if len(original) != len(set(original)):
        raise CoverageValidationError("duplicate", "duplicate slice range detected")
    ordered = sorted(original)
    if require_input_order and original != ordered:
        raise CoverageValidationError("out_of_order", "slice ranges are out of order")
    expected_start = 1
    for start, end in ordered:
        if start < 1 or end < start or end > total_pages:
            raise CoverageValidationError(
                "invalid_range", f"invalid slice range {start}-{end}"
            )
        if start < expected_start:
            raise CoverageValidationError(
                "overlap", f"slice range {start}-{end} overlaps an earlier range"
            )
        if start > expected_start:
            raise CoverageValidationError(
                "missing",
                f"page range {expected_start}-{start - 1} is not covered",
            )
        expected_start = end + 1
    if expected_start != total_pages + 1:
        raise CoverageValidationError(
            "missing",
            f"page range {expected_start}-{total_pages} is not covered",
        )
    return ordered


def write_normalized_result(path: Path, result: NormalizedParseResult) -> str:
    """Persist one bounded slice result as page-wise NDJSON, atomically."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    with partial.open("w", encoding="utf-8", newline="\n") as stream:
        for page in result.pages:
            stream.write(
                json.dumps(
                    page.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    partial.replace(target)
    return sha256_file(target)


def iter_normalized_pages(path: Path) -> Iterator[dict[str, object]]:
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                page = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CoverageValidationError(
                    "malformed_result",
                    f"invalid normalized result at line {line_number}",
                ) from exc
            if not isinstance(page, dict):
                raise CoverageValidationError(
                    "malformed_result",
                    f"normalized result line {line_number} is not an object",
                )
            yield page


def merge_normalized_result_files(
    slices: Sequence[object],
    output_path: Path,
    *,
    total_pages: int,
) -> MergedResult:
    """Validate slice ranges and page offsets while writing one merged stream."""

    ordered_ranges = validate_slice_coverage(slices, total_pages)
    by_range = {_range_values(item): item for item in slices}
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    seen_pages: set[int] = set()
    page_count = 0
    with partial.open("w", encoding="utf-8", newline="\n") as output:
        for start, end in ordered_ranges:
            item = by_range[(start, end)]
            result_path = getattr(item, "result_path", None)
            if result_path is None and isinstance(item, Mapping):
                result_path = item.get("result_path")
            if not result_path:
                raise CoverageValidationError(
                    "missing_result", f"slice {start}-{end} has no normalized result"
                )
            expected_page = start
            for page in iter_normalized_pages(Path(str(result_path))):
                try:
                    physical = int(page.get("physical_pdf_page") or 0)
                except (TypeError, ValueError) as exc:
                    raise CoverageValidationError(
                        "invalid_page", "normalized page number is invalid"
                    ) from exc
                if physical in seen_pages:
                    raise CoverageValidationError(
                        "duplicate_page", f"physical page {physical} appears twice"
                    )
                if physical != expected_page:
                    code = "offset" if start <= physical <= end else "out_of_range"
                    raise CoverageValidationError(
                        code,
                        f"slice {start}-{end} returned page {physical}; expected {expected_page}",
                    )
                seen_pages.add(physical)
                expected_page += 1
                output.write(
                    json.dumps(
                        page,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                output.write("\n")
                page_count += 1
            if expected_page != end + 1:
                raise CoverageValidationError(
                    "missing_page",
                    f"slice {start}-{end} ended before page {expected_page}",
                )
        if seen_pages != set(range(1, total_pages + 1)):
            raise CoverageValidationError(
                "coverage", "merged page coverage is not exactly 1..N"
            )
        output.flush()
        os.fsync(output.fileno())
    partial.replace(target)
    return MergedResult(
        path=target,
        sha256=sha256_file(target),
        page_count=page_count,
    )

