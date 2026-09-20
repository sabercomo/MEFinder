"""Adapter for MinerU 4.x's ``/v1`` parse-jobs API.

MinerU 4.0 replaced the 3.x ``/tasks`` protocol with an OpenAI-shaped API:
an upload is registered (``POST /v1/uploads``), its bytes are streamed
(``PUT /v1/uploads/{id}/content``), the upload is completed into a file id
(``POST /v1/uploads/{id}/complete``), and a parse job referencing that file id
is created (``POST /v1/parse/jobs``).  Results are artifact files downloaded
through ``GET /v1/files/{id}/content``.

Two contract differences against 3.x matter for citation anchors:

* Block boxes are normalized to ``0..1`` instead of MinerU's 1000-unit canvas,
  so they are rescaled to the 1000 canvas already stored for 3.x documents and
  additionally published as ``bbox_normalized``.
* ``structured_content`` nests blocks per page instead of carrying ``page_idx``
  on every flat item.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from .mineru_local_http import MINERU_LOCAL_PROVIDER_ID, MinerULocalTransport
from .parser_provider import ParserProviderError, ParserTaskStatus


MINERU_V1_PROTOCOL = "v1-jobs"
MINERU_V1_BBOX_CANVAS = 1000.0
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024

#: MinerU 4.x parse tiers, ordered from cheapest to most accurate.
MINERU_V1_TIERS = ("flash", "basic", "standard", "advanced")

#: 3.x ``backend`` values mapped onto the 4.x tier model.  The 3.x pipeline
#: backend is the OCR-first path, while the VLM backends were the accurate
#: path, which 4.x expresses as ``standard`` (hybrid effort ``high``).
_BACKEND_TIERS = {
    "pipeline": "basic",
    "vlm": "standard",
    "vlm-auto-engine": "standard",
    "vlm-transformers": "standard",
    "vlm-mlx-engine": "standard",
    "vlm-vllm-engine": "standard",
    "vlm-lmdeploy-engine": "standard",
    "vlm-http-client": "standard",
    "hybrid": "standard",
    "hybrid-engine": "standard",
}

_JOB_STATUS = {
    "queued": ParserTaskStatus.SUBMITTED,
    "running": ParserTaskStatus.WAITING,
    "completed": ParserTaskStatus.COMPLETED,
    "partial": ParserTaskStatus.PERMANENT_FAILURE,
    "failed": ParserTaskStatus.PERMANENT_FAILURE,
    "canceled": ParserTaskStatus.CANCELLED,
}


def tier_for_backend(backend: str, *, default: str = "standard") -> str:
    """Map a configured 3.x backend name onto a 4.x parse tier."""

    normalized = (backend or "").strip().lower()
    if normalized in MINERU_V1_TIERS:
        return normalized
    return _BACKEND_TIERS.get(normalized, default)


def job_status(payload: Mapping[str, object]) -> ParserTaskStatus:
    value = str(payload.get("status") or "queued").strip().lower()
    return _JOB_STATUS.get(value, ParserTaskStatus.WAITING)


class MinerUV1Client:
    """Thin client over the ``/v1`` upload, job, and file endpoints."""

    def __init__(self, transport: MinerULocalTransport) -> None:
        self.transport = transport

    def health(self) -> Dict[str, object]:
        return self.transport.json_request("GET", "/v1/health")

    def upload_file(self, path: Path) -> str:
        """Register, stream, and complete one upload, returning its file id."""

        source = Path(path)
        size = source.stat().st_size
        digest = _sha256_file(source)
        filename = source.name
        mime = mimetypes.guess_type(filename)[0] or "application/pdf"
        created = self.transport.json_request(
            "POST",
            "/v1/uploads",
            body={
                "filename": filename,
                "bytes": size,
                "mime_type": mime,
                "purpose": "parse",
                "sha256sum": digest,
            },
        )
        upload_id = str(created.get("id") or "")
        if not upload_id:
            raise ParserProviderError(
                "MinerU Local did not return an upload id",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            )
        file_id = _upload_file_id(created)
        if file_id:
            # The server already held an identical blob (sha256 dedupe).
            return file_id
        self.transport.stream_file(
            "PUT",
            f"/v1/uploads/{upload_id}/content",
            path=source,
            content_type="application/octet-stream",
            chunk_bytes=UPLOAD_CHUNK_BYTES,
        )
        completed = self.transport.json_request(
            "POST",
            f"/v1/uploads/{upload_id}/complete",
            body={"sha256sum": digest},
        )
        file_id = _upload_file_id(completed)
        if not file_id:
            raise ParserProviderError(
                "MinerU Local upload completed without a file id",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            )
        return file_id

    def create_job(
        self,
        file_id: str,
        *,
        tier: str,
        ocr_mode: str,
        output_formats: Sequence[str],
    ) -> Dict[str, object]:
        return self.transport.json_request(
            "POST",
            "/v1/parse/jobs",
            body={
                "files": [{"source": {"type": "file_id", "file_id": file_id}}],
                "tier": tier,
                "ocr_mode": ocr_mode,
                "output_formats": list(output_formats),
            },
        )

    def job(self, job_id: str) -> Dict[str, object]:
        return self.transport.json_request("GET", f"/v1/parse/jobs/{job_id}")

    def file_content(self, file_id: str) -> bytes:
        return self.transport.download(f"/v1/files/{file_id}/content")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _upload_file_id(payload: Mapping[str, object]) -> str:
    candidate = payload.get("file")
    if isinstance(candidate, Mapping):
        return str(candidate.get("id") or "")
    return ""


def structured_content_file_id(job: Mapping[str, object]) -> str:
    """Return the ``structured_content`` artifact id of a finished job."""

    files = job.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise ParserProviderError(
            "MinerU Local job result does not list any files",
            provider_id=MINERU_LOCAL_PROVIDER_ID,
        )
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status and status != "completed":
            raise ParserProviderError(
                _file_error_message(entry),
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            )
        outputs = entry.get("output_files")
        if not isinstance(outputs, Mapping):
            continue
        reference = outputs.get("structured_content")
        if isinstance(reference, Mapping) and reference.get("file_id"):
            return str(reference["file_id"])
    raise ParserProviderError(
        "MinerU Local job produced no structured_content artifact",
        provider_id=MINERU_LOCAL_PROVIDER_ID,
    )


def _file_error_message(entry: Mapping[str, object]) -> str:
    error = entry.get("error")
    if isinstance(error, Mapping):
        message = str(error.get("message") or "").strip()
        if message:
            return f"MinerU Local parse failed: {message}"
    return "MinerU Local parse failed"


def decode_structured_content(payload: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParserProviderError(
            "MinerU Local structured_content is not valid JSON",
            provider_id=MINERU_LOCAL_PROVIDER_ID,
        ) from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("pages"), list):
        raise ParserProviderError(
            "MinerU Local structured_content does not contain pages",
            provider_id=MINERU_LOCAL_PROVIDER_ID,
        )
    return value


def iter_page_blocks(
    structured_content: Mapping[str, object],
) -> Dict[int, Sequence[Mapping[str, object]]]:
    """Group ``structured_content`` blocks by their zero-based page index."""

    grouped: Dict[int, Sequence[Mapping[str, object]]] = {}
    for page in structured_content.get("pages") or ():
        if not isinstance(page, Mapping):
            continue
        try:
            page_idx = int(page.get("page_idx"))
        except (TypeError, ValueError):
            continue
        blocks = page.get("blocks")
        grouped[page_idx] = [
            block for block in blocks if isinstance(block, Mapping)
        ] if isinstance(blocks, list) else []
    return grouped


#: Characters upstream escapes when they appear as literal text
#: (docvortex ``escape_conservative_markdown_text``).
_MARKDOWN_ESCAPABLE = "*_`~$#+-\\[]()!"
_ESCAPE_SENTINEL = "\x00{}\x00"
_ESCAPED_RE = re.compile(r"\\([" + re.escape(_MARKDOWN_ESCAPABLE) + r"])")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]*>|<!--.*?-->")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CODE_FENCE_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"\$([^$]+)\$|\\\((.+?)\\\)", re.DOTALL)
_EMPHASIS_RE = re.compile(r"(\*{1,3}|~~|_{1,2})(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_LIST_MARKER_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
_HEADING_MARKER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_TABLE_RULE_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$", re.MULTILINE)
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
}


def plain_text_from_markdown(value: str) -> str:
    """Recover the printed text from one rendered-Markdown ``content`` string.

    MinerU 4.x hands back Markdown, not source text: upstream applies emphasis
    wrappers (``**`` / ``*`` / ``***`` / ``~~``), HTML wrappers for the styles
    Markdown cannot express (``<strong>`` / ``<u>`` / ``<sup>`` / ``<s>`` …),
    inline-code fences, LaTeX delimiters, links and images, and it escapes
    literal ``*_`~$`` and leading block markers with a backslash.  Storing that
    verbatim would break exact quote location and leak markup into citations,
    so the markup is removed while every literal character is preserved.
    """

    if not value:
        return ""
    # Literal characters upstream escaped must survive markup removal, so park
    # them behind sentinels first and restore them at the very end.
    protected: list[str] = []

    def _park(match: "re.Match[str]") -> str:
        protected.append(match.group(1))
        return _ESCAPE_SENTINEL.format(len(protected) - 1)

    text = _ESCAPED_RE.sub(_park, value)
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = _HTML_TAG_RE.sub("", text)
    for entity, replacement in _HTML_ENTITIES.items():
        text = text.replace(entity, replacement)
    text = _IMAGE_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _CODE_FENCE_RE.sub(lambda match: match.group(2).strip(), text)
    text = _INLINE_MATH_RE.sub(
        lambda match: (match.group(1) or match.group(2) or "").strip(), text
    )
    previous = None
    while previous != text:
        previous = text
        text = _EMPHASIS_RE.sub(r"\2", text)
    text = _TABLE_RULE_RE.sub("", text)
    text = "\n".join(_table_row_text(line) for line in text.split("\n"))
    text = _LIST_MARKER_RE.sub("", text)
    text = _HEADING_MARKER_RE.sub("", text)
    for index, character in enumerate(protected):
        text = text.replace(_ESCAPE_SENTINEL.format(index), character)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _table_row_text(line: str) -> str:
    """Turn one Markdown table row into spaced cell text."""

    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return line
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return " ".join(cell for cell in cells if cell)


def block_text(block: Mapping[str, object]) -> str:
    """Flatten one structured-content block into plain source text.

    Every block carries its body as a rendered Markdown ``content`` string;
    image, table, chart, and code blocks additionally carry ``captions`` and
    ``footnotes`` lists whose entries hold their own ``content``.  Markup is
    stripped so stored text matches the printed page character for character.
    """

    parts: list[str] = []
    content = block.get("content")
    if isinstance(content, str):
        parts.append(plain_text_from_markdown(content))
    for key in ("captions", "footnotes"):
        for annotation in _annotations(block, key):
            value = annotation.get("content")
            if isinstance(value, str):
                parts.append(plain_text_from_markdown(value))
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _annotations(
    block: Mapping[str, object], key: str
) -> Sequence[Mapping[str, object]]:
    value = block.get(key)
    if not isinstance(value, list):
        return ()
    return [item for item in value if isinstance(item, Mapping)]


def block_bbox(block: Mapping[str, object]) -> Optional[tuple[float, ...]]:
    """Return the normalized ``0..1`` box of a block, if it carries one."""

    bbox = block.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        values = tuple(float(item) for item in bbox)
    except (TypeError, ValueError):
        return None
    return values


def scaled_bbox(normalized: Sequence[float]) -> tuple[float, ...]:
    """Rescale a normalized box onto MinerU's 1000-unit citation canvas."""

    return tuple(round(value * MINERU_V1_BBOX_CANVAS, 3) for value in normalized)


def block_text_level(block: Mapping[str, object]) -> Optional[int]:
    """Derive the 3.x ``text_level`` heading depth from a 4.x block type."""

    block_type = str(block.get("type") or "").strip().lower()
    if block_type == "doc_title":
        return 1
    if block_type in {"paragraph_title", "title"}:
        level = block.get("level")
        try:
            resolved = int(level)
        except (TypeError, ValueError):
            return 2
        return max(1, resolved)
    return None
