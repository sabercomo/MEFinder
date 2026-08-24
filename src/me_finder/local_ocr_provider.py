"""Page-image-only adapters for NDLOCR-Lite and NDL古典籍OCR-Lite."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .local_ocr_settings import LocalOCREngineConfig
from .local_ocr_runtime import local_ocr_engine_lock
from .parser_provider import (
    NormalizedBlock,
    NormalizedPage,
    NormalizedParseResult,
    ParserCredential,
    ParserPollResult,
    ParserProvider,
    ParserProviderError,
    ParserRequest,
    ParserSubmission,
    ParserTaskStatus,
    ProviderCapabilities,
)


@dataclass(frozen=True)
class RenderedOCRPage:
    local_page_index: int
    image_path: Path
    image_width: int
    image_height: int
    pdf_width: float
    pdf_height: float
    ink_ratio: float
    horizontal_text_bands: int = 0
    vertical_text_bands: int = 0


@dataclass(frozen=True)
class LocalOCRProbeEvidence:
    provider_id: str
    valid_lines: int
    character_count: int
    vertical_lines: int
    page_count: int
    kana_characters: int = 0
    horizontal_text_bands: int = 0
    vertical_text_bands: int = 0

    @property
    def vertical_ratio(self) -> float:
        return self.vertical_lines / max(self.valid_lines, 1)

    @property
    def kana_ratio(self) -> float:
        return self.kana_characters / max(self.character_count, 1)

    @property
    def vertical_layout(self) -> bool:
        return (
            self.vertical_text_bands >= 3
            and self.vertical_text_bands
            >= self.horizontal_text_bands * 1.5
        )


PageRenderer = Callable[
    [Path, Path, int, Optional[Sequence[int]]],
    Sequence[RenderedOCRPage],
]
CancelCheck = Callable[[], bool]
PageProgress = Callable[[int], None]


class LocalOCRProvider(ParserProvider):
    def __init__(
        self,
        engine: LocalOCREngineConfig,
        *,
        render_dpi: int = 200,
        pages_per_slice: int = 10,
        timeout_seconds_per_page: int = 300,
        blank_ink_ratio: float = 0.001,
        page_renderer: PageRenderer | None = None,
        cancel_requested: CancelCheck | None = None,
        page_progress: PageProgress | None = None,
    ) -> None:
        if not engine.configured:
            raise ValueError("local OCR engine is not configured")
        self.engine = engine
        self.provider_id = engine.provider_id
        self.render_dpi = int(render_dpi)
        self.timeout_seconds_per_page = int(timeout_seconds_per_page)
        self.blank_ink_ratio = float(blank_ink_ratio)
        self._page_renderer = page_renderer or render_pdf_pages
        self._cancel_requested = cancel_requested or (lambda: False)
        self._page_progress = page_progress
        self._capabilities = ProviderCapabilities(
            max_pages_per_file=max(1, int(pages_per_slice)),
            max_bytes_per_file=None,
            max_concurrency=1,
            supports_scanned_pdf=True,
            supports_bbox=True,
            supports_page_ranges=False,
            supports_async_jobs=False,
            supports_stream_upload=False,
            supported_models=(engine.version,),
            optional_limits={"input_mode": "page_image"},
        )

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def submit(
        self,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserSubmission:
        request = self.prepare(request)
        output_root = Path(request.output_dir or Path.cwd()) / (
            f"run-{uuid.uuid4().hex}"
        )
        input_dir = output_root / "pages"
        raw_dir = output_root / "raw"
        input_dir.mkdir(parents=True, exist_ok=False)
        raw_dir.mkdir(parents=True, exist_ok=False)
        rendered = self._page_renderer(
            request.source_path,
            input_dir,
            self.render_dpi,
            None,
        )
        if len(rendered) != request.page_count:
            raise ParserProviderError(
                "page renderer did not cover the complete PDF slice",
                provider_id=self.provider_id,
            )
        pages = []
        for page in rendered:
            if self._cancel_requested():
                return ParserSubmission(
                    provider_id=self.provider_id,
                    remote_task_id=None,
                    status=ParserTaskStatus.CANCELLED,
                )
            if page.ink_ratio <= self.blank_ink_ratio:
                payload = _blank_payload(page)
                warning = "blank_page"
            else:
                page_output = raw_dir / f"page-{page.local_page_index + 1:06d}"
                page_output.mkdir()
                try:
                    self._run_page(page.image_path, page_output)
                except _LocalOCRRunCancelled:
                    return ParserSubmission(
                        provider_id=self.provider_id,
                        remote_task_id=None,
                        status=ParserTaskStatus.CANCELLED,
                    )
                result_path = page_output / f"{page.image_path.stem}.json"
                try:
                    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ParserProviderError(
                        f"{self.engine.display_name} did not produce valid page JSON",
                        provider_id=self.provider_id,
                    ) from exc
                warning = None
            pages.append(
                {
                    "local_page_index": page.local_page_index,
                    "image_width": page.image_width,
                    "image_height": page.image_height,
                    "pdf_width": page.pdf_width,
                    "pdf_height": page.pdf_height,
                    "ink_ratio": page.ink_ratio,
                    "warning": warning,
                    "payload": payload,
                }
            )
            if self._page_progress is not None:
                self._page_progress(
                    request.global_page_offset + page.local_page_index + 1
                )
        return ParserSubmission(
            provider_id=self.provider_id,
            remote_task_id=None,
            status=ParserTaskStatus.COMPLETED,
            raw_result={"pages": pages},
        )

    def poll(
        self,
        remote_task_id: str,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> ParserPollResult:
        return ParserPollResult(ParserTaskStatus.COMPLETED)

    def fetch_result(
        self,
        submission: ParserSubmission,
        request: ParserRequest,
        *,
        credential: Optional[ParserCredential] = None,
    ) -> object:
        if submission.raw_result is None:
            raise ParserProviderError(
                "synchronous local OCR result is missing",
                provider_id=self.provider_id,
            )
        return submission.raw_result

    def normalize_result(
        self,
        raw_result: object,
        request: ParserRequest,
    ) -> NormalizedParseResult:
        if not isinstance(raw_result, Mapping) or not isinstance(
            raw_result.get("pages"), list
        ):
            raise ParserProviderError(
                "local OCR result is malformed",
                provider_id=self.provider_id,
            )
        pages = tuple(
            normalize_ndl_page(
                item,
                request=request,
                provider_id=self.provider_id,
                version=self.engine.version,
            )
            for item in raw_result["pages"]
            if isinstance(item, Mapping)
        )
        if len(pages) != request.page_count:
            raise ParserProviderError(
                "local OCR result does not cover every rendered page",
                provider_id=self.provider_id,
            )
        return NormalizedParseResult(
            provider_id=self.provider_id,
            model=self.engine.version,
            parser_version=self.engine.version,
            pages=tuple(sorted(pages, key=lambda item: item.physical_pdf_page)),
            provenance={
                "input_mode": "page_image",
                "render_dpi": self.render_dpi,
                "weights_sha256": self.engine.weights_sha256,
            },
        )

    def probe(
        self,
        pdf_path: Path,
        output_dir: Path,
        page_indices: Sequence[int],
    ) -> LocalOCRProbeEvidence:
        rendered = self._page_renderer(
            Path(pdf_path),
            Path(output_dir) / "pages",
            self.render_dpi,
            page_indices,
        )
        valid_lines = 0
        character_count = 0
        vertical_lines = 0
        kana_characters = 0
        horizontal_text_bands = sum(
            page.horizontal_text_bands for page in rendered
        )
        vertical_text_bands = sum(
            page.vertical_text_bands for page in rendered
        )
        vertical_layout = (
            vertical_text_bands >= 3
            and vertical_text_bands >= horizontal_text_bands * 1.5
        )
        for page in rendered:
            if page.ink_ratio <= self.blank_ink_ratio:
                continue
            if self.provider_id == "ndlkotenocr-lite" and not vertical_layout:
                continue
            page_output = Path(output_dir) / f"raw-{page.local_page_index:06d}"
            page_output.mkdir(parents=True, exist_ok=True)
            self._run_page(page.image_path, page_output)
            try:
                payload = json.loads(
                    (page_output / f"{page.image_path.stem}.json").read_text(
                        encoding="utf-8-sig"
                    )
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ParserProviderError(
                    f"{self.engine.display_name} did not produce valid probe JSON",
                    provider_id=self.provider_id,
                ) from exc
            for line in _ndl_lines(payload):
                text = str(line.get("text") or "").strip()
                polygon = _polygon(line.get("boundingBox"))
                if not text or polygon is None:
                    continue
                valid_lines += 1
                character_count += len(text)
                kana_characters += sum(
                    1
                    for character in text
                    if "\u3040" <= character <= "\u30ff"
                    or "\u31f0" <= character <= "\u31ff"
                    or "\uff66" <= character <= "\uff9d"
                )
                min_x, min_y, max_x, max_y = polygon
                if max_y - min_y > max_x - min_x:
                    vertical_lines += 1
        return LocalOCRProbeEvidence(
            provider_id=self.provider_id,
            valid_lines=valid_lines,
            character_count=character_count,
            vertical_lines=vertical_lines,
            page_count=len(rendered),
            kana_characters=kana_characters,
            horizontal_text_bands=horizontal_text_bands,
            vertical_text_bands=vertical_text_bands,
        )

    def _run_page(self, image_path: Path, output_dir: Path) -> None:
        command = [
            str(self.engine.python_path),
            str(self.engine.script_path),
            "--sourceimg",
            str(Path(image_path).resolve()),
            "--output",
            str(Path(output_dir).resolve()),
        ]
        if self.provider_id == "ndlocr-lite":
            command.append("--json-only")
        stdout_path = Path(output_dir) / "runner.log"
        run_lock = local_ocr_engine_lock(self.provider_id)
        while not run_lock.acquire(timeout=0.1):
            if self._cancel_requested():
                raise _LocalOCRRunCancelled()
        try:
            with stdout_path.open("wb") as output:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.engine.script_path.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if sys.platform == "win32"
                        else 0
                    ),
                )
                deadline = time.monotonic() + self.timeout_seconds_per_page
                while process.poll() is None:
                    if self._cancel_requested():
                        _stop_process(process)
                        raise _LocalOCRRunCancelled()
                    if time.monotonic() >= deadline:
                        _stop_process(process)
                        raise ParserProviderError(
                            f"{self.engine.display_name} page timed out",
                            provider_id=self.provider_id,
                            retryable=True,
                        )
                    time.sleep(0.1)
        finally:
            run_lock.release()
        if process.returncode:
            detail = stdout_path.read_text(
                encoding="utf-8", errors="replace"
            )[-2000:].strip()
            raise ParserProviderError(
                f"{self.engine.display_name} exited with {process.returncode}: "
                f"{detail}",
                provider_id=self.provider_id,
            )


def normalize_ndl_page(
    raw_page: Mapping[str, object],
    *,
    request: ParserRequest,
    provider_id: str,
    version: str,
) -> NormalizedPage:
    try:
        local_page_index = int(raw_page["local_page_index"])
        image_width = int(raw_page["image_width"])
        image_height = int(raw_page["image_height"])
        pdf_width = float(raw_page["pdf_width"])
        pdf_height = float(raw_page["pdf_height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParserProviderError(
            "local OCR page geometry is invalid",
            provider_id=provider_id,
        ) from exc
    if (
        local_page_index < 0
        or local_page_index >= request.page_count
        or image_width < 1
        or image_height < 1
        or pdf_width <= 0
        or pdf_height <= 0
    ):
        raise ParserProviderError(
            "local OCR page geometry is out of range",
            provider_id=provider_id,
        )
    payload = raw_page.get("payload")
    if not isinstance(payload, Mapping):
        raise ParserProviderError(
            "local OCR page JSON must be an object",
            provider_id=provider_id,
        )
    scale_x = pdf_width / image_width
    scale_y = pdf_height / image_height
    blocks = []
    for raw_order, line in enumerate(_ndl_lines(payload)):
        text = str(line.get("text") or "").strip()
        polygon = _polygon(line.get("boundingBox"))
        if not text or polygon is None:
            continue
        min_x, min_y, max_x, max_y = polygon
        bbox = (
            min_x * scale_x,
            min_y * scale_y,
            max_x * scale_x,
            max_y * scale_y,
        )
        blocks.append(
            NormalizedBlock(
                text=text,
                block_type="text",
                bbox=bbox,
                reading_order=len(blocks),
                provenance={
                    "provider": provider_id,
                    "raw_order": raw_order,
                    "raw_id": line.get("id"),
                    "raw_polygon": line.get("boundingBox"),
                    "raw_is_vertical": line.get("isVertical"),
                    "is_vertical": (bbox[3] - bbox[1]) > (bbox[2] - bbox[0]),
                    "detection_confidence": line.get("confidence"),
                    "class_index": line.get("class_index"),
                },
            )
        )
    warnings = []
    if raw_page.get("warning"):
        warnings.append(str(raw_page["warning"]))
    elif not blocks:
        warnings.append("no_text_detected")
    return NormalizedPage(
        physical_pdf_page=(
            request.global_page_offset + local_page_index + 1
        ),
        text="\n".join(block.text for block in blocks),
        blocks=tuple(blocks),
        parser_provenance={
            "provider": provider_id,
            "version": version,
            "local_page_index": local_page_index,
            "global_page_offset": request.global_page_offset,
            "image_width": image_width,
            "image_height": image_height,
            "pdf_width": pdf_width,
            "pdf_height": pdf_height,
            "ink_ratio": raw_page.get("ink_ratio"),
        },
        warnings=tuple(warnings),
    )


def render_pdf_pages(
    path: Path,
    output_dir: Path,
    dpi: int,
    page_indices: Optional[Sequence[int]],
) -> Sequence[RenderedOCRPage]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise ParserProviderError(
            "PyMuPDF is required to render local OCR page images",
            provider_id="local-ocr",
        ) from exc
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    document = fitz.open(str(path))
    try:
        indices = (
            list(range(len(document)))
            if page_indices is None
            else [int(index) for index in page_indices]
        )
        pages = []
        scale = float(dpi) / 72.0
        for page_index in indices:
            if page_index < 0 or page_index >= len(document):
                raise ValueError("OCR sample page is out of range")
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                alpha=False,
                colorspace=fitz.csRGB,
            )
            horizontal_text_bands, vertical_text_bands = _text_band_counts(
                pixmap.samples,
                int(pixmap.width),
                int(pixmap.height),
                int(pixmap.n),
            )
            image_path = output / f"page-{page_index + 1:06d}.png"
            image_path.write_bytes(pixmap.tobytes("png"))
            pages.append(
                RenderedOCRPage(
                    local_page_index=page_index,
                    image_path=image_path,
                    image_width=int(pixmap.width),
                    image_height=int(pixmap.height),
                    pdf_width=float(page.rect.width),
                    pdf_height=float(page.rect.height),
                    ink_ratio=_ink_ratio(
                        pixmap.samples,
                        int(pixmap.width),
                        int(pixmap.height),
                        int(pixmap.n),
                    ),
                    horizontal_text_bands=horizontal_text_bands,
                    vertical_text_bands=vertical_text_bands,
                )
            )
        return pages
    finally:
        document.close()


def sample_page_indices(page_count: int, sample_count: int) -> Tuple[int, ...]:
    if page_count < 1:
        return ()
    count = min(page_count, max(1, int(sample_count)))
    if count == 1:
        return (page_count // 2,)
    return tuple(
        sorted(
            {
                min(
                    page_count - 1,
                    int(round(index * (page_count - 1) / (count - 1))),
                )
                for index in range(count)
            }
        )
    )


def choose_local_ocr_engine(
    engines: Sequence[LocalOCREngineConfig],
    *,
    pdf_path: Path,
    work_dir: Path,
    render_dpi: int,
    probe_pages: int,
    timeout_seconds_per_page: int,
    blank_ink_ratio: float,
    cancel_requested: CancelCheck | None = None,
) -> Tuple[LocalOCREngineConfig, Dict[str, object]]:
    candidates = tuple(engines)
    if not candidates:
        raise ParserProviderError(
            "no local OCR engine is available",
            provider_id="local-ocr",
        )
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise ParserProviderError(
            "PyMuPDF is required to sample local OCR pages",
            provider_id="local-ocr",
        ) from exc
    document = fitz.open(str(pdf_path))
    try:
        indices = sample_page_indices(len(document), probe_pages)
    finally:
        document.close()
    evidence: Dict[str, LocalOCRProbeEvidence] = {}
    failures: Dict[str, str] = {}
    for engine in candidates:
        provider = LocalOCRProvider(
            engine,
            render_dpi=render_dpi,
            pages_per_slice=1,
            timeout_seconds_per_page=timeout_seconds_per_page,
            blank_ink_ratio=blank_ink_ratio,
            cancel_requested=cancel_requested,
        )
        try:
            evidence[engine.provider_id] = provider.probe(
                pdf_path,
                Path(work_dir) / engine.provider_id,
                indices,
            )
        except ParserProviderError as exc:
            failures[engine.provider_id] = str(exc)
    modern = next(
        (item for item in candidates if item.provider_id == "ndlocr-lite"),
        candidates[0],
    )
    ancient = next(
        (
            item
            for item in candidates
            if item.provider_id == "ndlkotenocr-lite"
        ),
        None,
    )
    modern_evidence = evidence.get(modern.provider_id)
    ancient_evidence = (
        evidence.get(ancient.provider_id) if ancient is not None else None
    )
    if modern_evidence is None and ancient_evidence is None:
        raise ParserProviderError(
            "every local OCR probe failed: "
            + "; ".join(f"{key}: {value}" for key, value in failures.items()),
            provider_id="local-ocr",
        )
    layout_evidence = modern_evidence or ancient_evidence
    ancient_matches = bool(
        ancient is not None
        and ancient_evidence is not None
        and layout_evidence is not None
        and layout_evidence.vertical_layout
        and (
            modern_evidence is None
            or (
                ancient_evidence.valid_lines
                >= max(1, math.floor(modern_evidence.valid_lines * 0.6))
                and ancient_evidence.character_count
                >= math.floor(modern_evidence.character_count * 0.5)
            )
        )
    )
    modern_matches = bool(
        modern_evidence is not None
        and modern_evidence.kana_characters >= 3
        and modern_evidence.kana_ratio >= 0.05
    )
    if ancient_matches:
        selected = ancient
        strategy = "vertical_geometry"
    elif modern_matches:
        selected = modern
        strategy = "japanese_script"
    else:
        raise ParserProviderError(
            "抽样页不是日文文本，也不是竖排古籍版式",
            provider_id="local-ocr",
        )
    assert selected is not None
    return selected, {
        "strategy": strategy,
        "sample_pages": [index + 1 for index in indices],
        "evidence": {
            provider_id: {
                "valid_lines": item.valid_lines,
                "character_count": item.character_count,
                "vertical_lines": item.vertical_lines,
                "vertical_ratio": round(item.vertical_ratio, 4),
                "kana_characters": item.kana_characters,
                "kana_ratio": round(item.kana_ratio, 4),
                "horizontal_text_bands": item.horizontal_text_bands,
                "vertical_text_bands": item.vertical_text_bands,
            }
            for provider_id, item in evidence.items()
        },
        "failures": failures,
    }


def _ndl_lines(payload: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    contents = payload.get("contents")
    if not isinstance(contents, list):
        return ()
    lines = contents[0] if contents else []
    if not isinstance(lines, list):
        return ()
    return tuple(item for item in lines if isinstance(item, Mapping))


def _polygon(value: object) -> Optional[Tuple[float, float, float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    points = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        try:
            points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    if not all(math.isfinite(item) for item in (*xs, *ys)):
        return None
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    if min_x < 0 or min_y < 0 or max_x <= min_x or max_y <= min_y:
        return None
    return min_x, min_y, max_x, max_y


def _ink_ratio(samples: bytes, width: int, height: int, channels: int) -> float:
    pixel_count = max(1, width * height)
    stride = max(1, pixel_count // 100_000)
    dark = 0
    sampled = 0
    for pixel in range(0, pixel_count, stride):
        offset = pixel * channels
        if min(samples[offset : offset + 3]) < 245:
            dark += 1
        sampled += 1
    return dark / max(sampled, 1)


def _text_band_counts(
    samples: bytes,
    width: int,
    height: int,
    channels: int,
) -> Tuple[int, int]:
    step = max(1, max(width, height) // 1000)
    sampled_width = len(range(0, width, step))
    sampled_height = len(range(0, height, step))
    row_counts = [0] * sampled_height
    column_counts = [0] * sampled_width
    color_channels = min(channels, 3)
    for sampled_y, y in enumerate(range(0, height, step)):
        row_offset = y * width * channels
        for sampled_x, x in enumerate(range(0, width, step)):
            pixel_offset = row_offset + x * channels
            if (
                sum(
                    samples[
                        pixel_offset : pixel_offset + color_channels
                    ]
                )
                < 190 * color_channels
            ):
                row_counts[sampled_y] += 1
                column_counts[sampled_x] += 1
    return (
        _count_density_bands(
            row_counts,
            max(2, math.ceil(sampled_width * 0.01)),
        ),
        _count_density_bands(
            column_counts,
            max(2, math.ceil(sampled_height * 0.01)),
        ),
    )


def _count_density_bands(values: Sequence[int], threshold: int) -> int:
    bands = 0
    inactive = 2
    for value in values:
        if value >= threshold:
            if inactive >= 2:
                bands += 1
            inactive = 0
        else:
            inactive += 1
    return bands


def _blank_payload(page: RenderedOCRPage) -> Dict[str, object]:
    return {
        "contents": [[]],
        "imginfo": {
            "img_width": page.image_width,
            "img_height": page.image_height,
            "img_name": page.image_path.name,
        },
    }


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class _LocalOCRRunCancelled(RuntimeError):
    pass
