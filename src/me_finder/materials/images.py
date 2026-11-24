"""Store local images and render PDF crops without parsing text."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..document_file_store import pdf_file_stamp
from ..errors import UserFacingError
from ..page_display import resolve_citation_page
from ..persistence import materials_store as store
from ..persistence.connection import PROJECT_BUSY_TIMEOUT_MS, open_readonly_snapshot, open_write
from .service import tag_path

MAX_IMAGE_BYTES = 25 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg"}
_IMAGE_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class PdfImageSource:
    """Source and page metadata captured before opening the PDF renderer."""

    source_id: str
    page_number: int
    path: Path
    stamp: tuple[int, int, int, int]
    title: str
    citation_page: str | None


def image_path(root: Path, digest: str) -> Path:
    """Resolve a content-addressed image under the local data root."""
    if not isinstance(digest, str) or not _IMAGE_DIGEST.fullmatch(digest):
        raise ValueError("图片编号无效")
    return root / "materials" / "images" / digest


def image_type(data: bytes) -> str:
    """Identify supported browser raster formats from their file headers."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    raise UserFacingError("请选择 PNG 或 JPEG 图片")


def write_image(root: Path, digest: str, data: bytes) -> None:
    """Publish complete image bytes before committing metadata."""
    path = image_path(root, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def read_image(db_path: Path, root: Path, digest: str) -> tuple[bytes, str]:
    """Return one registered image independently of the material list."""
    try:
        path = image_path(root, digest)
    except ValueError as exc:
        raise UserFacingError(str(exc)) from exc
    with open_readonly_snapshot(db_path) as connection:
        mime = store.image_mime(connection, digest)
    if not mime or not path.is_file():
        raise UserFacingError("图片文件不存在")
    return path.read_bytes(), mime


def prepare_pdf_image(db_path: Path, payload: dict, source_path: Callable[[str], Path]) -> PdfImageSource:
    """Capture indexed page metadata and the current file identity without rendering."""
    source_id = payload.get("source_id")
    page_number = payload.get("page")
    if not isinstance(source_id, str) or type(page_number) is not int or page_number < 1:
        raise UserFacingError("请选择 PDF 和有效页码")
    source, page_row = store.read_pdf_image_rows(db_path, source_id, page_number - 1)
    if source is None:
        raise UserFacingError("未找到库内 PDF")
    try:
        path = source_path(source_id)
        stamp = pdf_file_stamp(path.stat())
    except (OSError, RuntimeError, ValueError) as exc:
        raise UserFacingError("无法打开 PDF 原件：" + str(exc)) from exc
    citation = resolve_citation_page(json.loads(page_row["payload_json"])) if page_row else None
    record = json.loads(source["payload_json"])
    title = record.get("display_title") or record.get("title") or source["file_name"]
    return PdfImageSource(source_id, page_number, path, stamp, title,
                          citation.start if citation and citation.verified else None)


def _pdf_image(source: PdfImageSource, payload: dict, *, crop: bool = False) -> tuple[bytes, dict]:
    import fitz

    revision = json.dumps([source.source_id, source.page_number, *source.stamp], separators=(",", ":"))
    try:
        with fitz.open(source.path) as pdf:
            if pdf.needs_pass or source.page_number > pdf.page_count:
                raise UserFacingError("无法读取该 PDF 页，请检查页码或文件密码")
            page_count = pdf.page_count
            page = pdf[source.page_number - 1]
            clip = page.rect
            box = payload.get("box")
            scale = 1400 / max(clip.width, clip.height)
            if crop:
                if payload.get("revision") != revision:
                    raise UserFacingError("PDF 文件已变化，请重新框选")
                if (not isinstance(box, list) or len(box) != 4
                        or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in box)
                        or box[0] >= box[2] or box[1] >= box[3]):
                    raise UserFacingError("请框选有效的截图区域")
                clip = fitz.Rect(clip.width * box[0], clip.height * box[1],
                                 clip.width * box[2], clip.height * box[3])
                scale = min(3, 4000 / max(clip.width, clip.height))
            image = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip).tobytes("png")
        if pdf_file_stamp(source.path.stat()) != source.stamp:
            raise UserFacingError("PDF 文件已变化，请重新框选")
    except (OSError, RuntimeError, ValueError) as exc:
        raise UserFacingError("无法打开 PDF 原件：" + str(exc)) from exc
    return image, {
        "source_id": source.source_id, "page": source.page_number, "box": box, "revision": revision,
        "citation_page": source.citation_page, "title": source.title, "page_count": page_count,
    }


def render_pdf_preview(source: PdfImageSource, payload: dict) -> dict:
    """Render the prepared PDF page without accessing the index."""
    image, origin = _pdf_image(source, payload)
    return {**origin, "data": _data_url(image, "image/png")}


def collect_image(db_path: Path, root: Path, payload: dict, source_path: Callable[[str], Path]) -> dict:
    """Save a PDF crop or imported image using the shared material tags."""
    note = payload.get("note", "")
    paths = payload.get("tags", [])
    if not isinstance(note, str) or len(note) > 200:
        raise UserFacingError("图片说明最多 200 字符")
    if not isinstance(paths, list) or not paths:
        raise UserFacingError("请选择或新建至少一个标签")
    tags = list(dict.fromkeys(tag_path(path) for path in paths))
    origin = {}
    if "source_id" in payload:
        data, origin = _pdf_image(prepare_pdf_image(db_path, payload, source_path), payload, crop=True)
        title = origin.pop("title")
        origin.pop("revision")
        origin.pop("page_count")
    else:
        title = payload.get("title") or "剪贴板图片"
        encoded = payload.get("data")
        if not isinstance(title, str) or not isinstance(encoded, str):
            raise UserFacingError("请选择要导入的图片")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise UserFacingError("图片数据无效") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise UserFacingError("每张图片最多 25 MB")
    mime = image_type(data)
    if not origin:
        import fitz

        try:
            fitz.Pixmap(data)
        except (RuntimeError, ValueError) as exc:
            raise UserFacingError("图片损坏，无法读取") from exc
    digest = hashlib.sha256(data).hexdigest()
    image = {
        "digest": digest, "title": title[:300], "mime": mime, "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(), "origin": json.dumps(origin, ensure_ascii=False),
    }
    store.ensure_materials_schema(db_path)
    with open_write(db_path, immediate=True, busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS) as connection:
        created = store.save_image(connection, image, tags)
        if created or not image_path(root, digest).is_file():
            write_image(root, digest, data)
        connection.commit()
    return {"digest": digest, "created": created, "tags": tags if created else []}


def edit_image(db_path: Path, payload: dict) -> dict:
    """Update a saved image's description or shared tags."""
    digest = payload.get("digest")
    if not isinstance(digest, str) or not _IMAGE_DIGEST.fullmatch(digest):
        raise UserFacingError("图片编号无效")
    note = payload.get("note")
    if "note" in payload and (not isinstance(note, str) or len(note) > 200):
        raise UserFacingError("图片说明最多 200 字符")
    tags = None
    if "tags" in payload:
        paths = payload["tags"]
        if not isinstance(paths, list):
            raise UserFacingError("图片标签无效")
        tags = list(dict.fromkeys(tag_path(path) for path in paths))
    with open_write(db_path, immediate=True, busy_timeout_ms=PROJECT_BUSY_TIMEOUT_MS) as connection:
        if store.image_mime(connection, digest) is None:
            raise UserFacingError("未找到这张图片")
        store.update_image(connection, digest, note, tags)
        connection.commit()
    return {"digest": digest}
