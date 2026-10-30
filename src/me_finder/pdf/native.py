"""Native PDF probing and page extraction via PyMuPDF or the built-in parser.

Returns page records; import policy, printed-page mapping and search windows
belong to the caller and the paragraph builder.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .extraction_types import PDFExtractionError, PDFTextPage
from .paragraphs import attach_page_block_offsets


SIMPLE_PDF_MAX_BYTES = 128 * 1024 * 1024


def get_pdf_page_labels(path: Path, page_count: int) -> List[Optional[str]]:
    labels: List[Optional[str]] = [None] * max(page_count, 0)
    pymupdf = load_pymupdf()
    if not pymupdf:
        return labels
    doc = pymupdf.open(str(path))
    try:
        for index in range(min(len(doc), page_count)):
            page = doc.load_page(index)
            label = page.get_label() if getattr(page, "get_label", None) else None
            labels[index] = str(label) if label else None
    finally:
        doc.close()
    return labels


def get_pdf_page_dimensions(
    path: Path,
    page_count: int,
) -> List[Tuple[Optional[float], Optional[float]]]:
    """Return physical PDF page dimensions for native and MinerU layouts."""

    dimensions: List[Tuple[Optional[float], Optional[float]]] = [
        (None, None)
    ] * max(page_count, 0)
    pymupdf = load_pymupdf()
    if not pymupdf:
        return dimensions
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return dimensions
    try:
        for index in range(min(len(doc), page_count)):
            rect = doc.load_page(index).rect
            dimensions[index] = (float(rect.width), float(rect.height))
    finally:
        doc.close()
    return dimensions


def detect_pdf_type(path: Path, sample_pages: int = 12) -> Dict[str, object]:
    pymupdf = load_pymupdf()
    parser_name = "pymupdf" if pymupdf else "simple_pdf_text"
    parser_version = getattr(pymupdf, "VersionBind", None) if pymupdf else "builtin"
    if pymupdf:
        page_count, sample_texts, image_count, has_labels = pymupdf_probe(path, pymupdf, sample_pages)
        to_unicode_count = low_level_count(path, b"/ToUnicode")
    else:
        simple = SimplePDF(path)
        page_numbers = simple.page_object_numbers()
        page_count = len(page_numbers) or simple.declared_page_count()
        sample_texts = [page.raw_text for page in simple.extract_pages(limit=sample_pages)]
        image_count = simple.image_object_count()
        has_labels = bool(simple.page_labels(page_count))
        to_unicode_count = simple.to_unicode_count()
    char_counts = [len(t.strip()) for t in sample_texts]
    extracted_pages = sum(1 for count in char_counts if count >= 30)
    avg_chars = int(sum(char_counts) / max(len(char_counts), 1))
    sample_text = "\n".join(sample_texts)
    garbled = estimate_garbled_ratio(sample_text)
    image_density = image_count / max(page_count, 1)
    if page_count == 0:
        detected = "complex_layout"
    elif avg_chars >= 180 and garbled < 0.22:
        detected = "native_text"
    elif image_density >= 0.7 and (avg_chars < 120 or to_unicode_count == 0):
        detected = "scanned"
    elif avg_chars >= 40 and garbled >= 0.22:
        detected = "broken_text"
    elif avg_chars >= 120:
        detected = "complex_layout"
    else:
        detected = "scanned" if image_density >= 0.4 else "broken_text"
    return {
        "detected_pdf_type": detected,
        "parser": parser_name,
        "parser_version": parser_version,
        "pdf_page_count": page_count,
        "has_page_labels": has_labels,
        "image_object_count": image_count,
        "to_unicode_map_count": to_unicode_count,
        "text_extractable_page_ratio": round(extracted_pages / max(len(char_counts), 1), 4),
        "avg_text_chars_per_page": avg_chars,
        "garbled_text_ratio": round(garbled, 4),
        "layout_complexity_hint": "unknown_without_layout_engine" if not pymupdf else "pymupdf_blocks_available",
        "notes": [] if pymupdf else ["PyMuPDF 未安装；使用内置简易原生文本解析器。"],
    }


def extract_native_pdf_pages(path: Path) -> List[PDFTextPage]:
    pymupdf = load_pymupdf()
    if pymupdf:
        return extract_pages_with_pymupdf(path, pymupdf)
    return SimplePDF(path).extract_pages()


def load_pymupdf():
    try:
        import fitz  # type: ignore

        return fitz
    except Exception:
        return None


def pymupdf_probe(path: Path, fitz, sample_pages: int) -> Tuple[int, List[str], int, bool]:
    doc = fitz.open(str(path))
    texts = []
    image_count = 0
    has_labels = False
    try:
        for index in range(min(len(doc), sample_pages)):
            page = doc.load_page(index)
            texts.append(page.get_text("text") or "")
            image_count += len(page.get_images(full=True))
            if getattr(page, "get_label", None) and page.get_label():
                has_labels = True
        return len(doc), texts, image_count, has_labels
    finally:
        doc.close()


def extract_pages_with_pymupdf(path: Path, fitz) -> List[PDFTextPage]:
    doc = fitz.open(str(path))
    pages: List[PDFTextPage] = []
    try:
        for index in range(len(doc)):
            page = doc.load_page(index)
            page_rect = page.rect
            label = page.get_label() if getattr(page, "get_label", None) else None
            raw_text = page.get_text("text") or ""
            block_records = []
            for block_index, block in enumerate(page.get_text("blocks") or []):
                if len(block) < 5:
                    continue
                block_records.append(
                    {
                        "block_index": block_index,
                        "bbox": [float(block[0]), float(block[1]), float(block[2]), float(block[3])],
                        "text": block[4],
                        "bbox_normalized": [
                            float(block[0]) / max(float(page_rect.width), 1.0),
                            float(block[1]) / max(float(page_rect.height), 1.0),
                            float(block[2]) / max(float(page_rect.width), 1.0),
                            float(block[3]) / max(float(page_rect.height), 1.0),
                        ],
                    }
                )
            attach_page_block_offsets(raw_text, block_records)
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


def low_level_count(path: Path, needle: bytes) -> int:
    if not needle:
        return 0
    count = 0
    overlap = b""
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            data = overlap + chunk
            count += data.count(needle)
            overlap = data[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return count


def estimate_garbled_ratio(text: str) -> float:
    text = text or ""
    if not text.strip():
        return 1.0
    considered = 0
    bad = 0.0
    for ch in text:
        if ch.isspace():
            continue
        considered += 1
        code = ord(ch)
        category = unicodedata.category(ch)
        if ch == "\ufffd" or category.startswith("C"):
            bad += 1
        elif 0x4E00 <= code <= 0x9FFF:
            continue
        elif 0x20 <= code <= 0x7E:
            continue
        elif ch in "，。；：？！、“”‘’《》（）【】—…·–-":
            continue
        elif ch in "éèêáàäöüßñçÉÈÊÁÀÄÖÜÑÇ©®™":
            continue
        else:
            bad += 0.45
    return min(1.0, bad / max(considered, 1))


class SimplePDF:
    """A deliberately small parser for simple native-text PDFs.

    It is not a replacement for PyMuPDF. It exists so the local MVP can still
    classify PDFs and validate a small native-text sample when no PDF package is
    installed in the current Python environment.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if self.path.stat().st_size > SIMPLE_PDF_MAX_BYTES:
            raise PDFExtractionError(
                "Large PDF fallback parsing requires PyMuPDF; refusing to load "
                "the complete file into memory."
            )
        self.data = self.path.read_bytes()
        self.objects = self._parse_objects()

    def _parse_objects(self) -> Dict[int, bytes]:
        objects: Dict[int, bytes] = {}
        pattern = re.compile(rb"(\d+)\s+(\d+)\s+obj\s*(.*?)\s*endobj", re.S)
        for match in pattern.finditer(self.data):
            objects[int(match.group(1))] = match.group(3)
        return objects

    def declared_page_count(self) -> int:
        counts = []
        for body in self.objects.values():
            if re.search(rb"/Type\s*/Pages\b|/Type/Pages\b", body[:500]):
                match = re.search(rb"/Count\s+(\d+)", body[:500])
                if match:
                    counts.append(int(match.group(1)))
        return max(counts or [0])

    def image_object_count(self) -> int:
        return sum(
            1
            for body in self.objects.values()
            if b"/Subtype/Image" in body.replace(b" ", b"") or b"/Subtype /Image" in body
        )

    def to_unicode_count(self) -> int:
        return sum(1 for body in self.objects.values() if b"/ToUnicode" in body)

    def page_object_numbers(self) -> List[int]:
        root = self._catalog_pages_ref()
        pages: List[int] = []
        if root is not None:
            self._walk_pages(root, pages, set())
        if pages:
            return pages
        for obj_num, body in self.objects.items():
            if self._is_page(body):
                pages.append(obj_num)
        return sorted(pages)

    def page_labels(self, page_count: int) -> List[Optional[str]]:
        labels: List[Optional[str]] = [None] * max(page_count, 0)
        catalog = self._catalog_body()
        if not catalog:
            return labels
        match = re.search(rb"/PageLabels\s+(\d+)\s+\d+\s+R", catalog)
        if not match:
            return labels
        label_obj = self.objects.get(int(match.group(1)), b"")
        nums_match = re.search(rb"/Nums\s*\[(.*?)\]", label_obj, re.S)
        if not nums_match:
            return labels
        entries = self._parse_label_entries(nums_match.group(1))
        for idx, (start_page, spec) in enumerate(entries):
            end_page = entries[idx + 1][0] if idx + 1 < len(entries) else page_count
            style = self._name_value(spec, b"S")
            prefix = self._literal_value(spec, b"P") or ""
            start_num = self._int_value(spec, b"St") or 1
            for page_index in range(start_page, min(end_page, page_count)):
                number = start_num + (page_index - start_page)
                if style == "D":
                    labels[page_index] = f"{prefix}{number}"
                elif style == "r":
                    from .page_mapping import int_to_roman

                    labels[page_index] = f"{prefix}{int_to_roman(number, upper=False)}"
                elif style == "R":
                    from .page_mapping import int_to_roman

                    labels[page_index] = f"{prefix}{int_to_roman(number, upper=True)}"
                else:
                    labels[page_index] = prefix or None
        return labels

    def extract_pages(self, limit: Optional[int] = None) -> List[PDFTextPage]:
        page_objects = self.page_object_numbers()
        if limit is not None:
            page_objects = page_objects[:limit]
        all_labels = self.page_labels(len(self.page_object_numbers()))
        pages: List[PDFTextPage] = []
        for index, page_obj in enumerate(page_objects):
            content = self._page_content(page_obj)
            raw_text = extract_text_from_pdf_content(content)
            pages.append(
                PDFTextPage(
                    pdf_page_index=index,
                    pdf_page_label=all_labels[index] if index < len(all_labels) else None,
                    raw_text=raw_text,
                    blocks=[
                        {
                            "block_index": 0,
                            "bbox": None,
                            "text": raw_text,
                            "page_idx": index,
                            "page_char_start": 0,
                            "page_char_end": len(raw_text),
                            "offset_unit": "unicode_codepoint",
                        }
                    ]
                    if raw_text
                    else [],
                    parser="simple_pdf_text",
                    parser_version="builtin",
                )
            )
        return pages

    def _catalog_body(self) -> Optional[bytes]:
        for body in self.objects.values():
            compact = body[:1000].replace(b" ", b"")
            if b"/Type/Catalog" in compact:
                return body
        return None

    def _catalog_pages_ref(self) -> Optional[int]:
        catalog = self._catalog_body()
        if not catalog:
            return None
        match = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", catalog)
        return int(match.group(1)) if match else None

    def _walk_pages(self, obj_num: int, pages: List[int], seen: set[int]) -> None:
        if obj_num in seen:
            return
        seen.add(obj_num)
        body = self.objects.get(obj_num, b"")
        if self._is_page(body):
            pages.append(obj_num)
            return
        kids_match = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
        if not kids_match:
            return
        for ref in indirect_refs(kids_match.group(1)):
            self._walk_pages(ref, pages, seen)

    def _is_page(self, body: bytes) -> bool:
        compact = body[:1200].replace(b" ", b"")
        return b"/Type/Page" in compact and b"/Type/Pages" not in compact

    def _page_content(self, page_obj: int) -> bytes:
        body = self.objects.get(page_obj, b"")
        refs: List[int] = []
        direct = re.search(rb"/Contents\s+(\d+)\s+\d+\s+R", body)
        if direct:
            refs.append(int(direct.group(1)))
        array = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
        if array:
            refs.extend(indirect_refs(array.group(1)))
        chunks: List[bytes] = []
        for ref in refs:
            chunks.append(self._stream_data(ref))
        return b"\n".join(chunk for chunk in chunks if chunk)

    def _stream_data(self, obj_num: int) -> bytes:
        body = self.objects.get(obj_num, b"")
        match = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.S)
        if not match:
            return b""
        stream = match.group(1)
        dictionary = body[: match.start()]
        if b"FlateDecode" in dictionary:
            try:
                stream = zlib.decompress(stream)
            except Exception:
                return b""
        return stream

    def _parse_label_entries(self, data: bytes) -> List[Tuple[int, bytes]]:
        entries: List[Tuple[int, bytes]] = []
        pattern = re.compile(rb"(\d+)\s+((?:\d+\s+\d+\s+R)|(?:<<.*?>>))", re.S)
        for match in pattern.finditer(data):
            page_index = int(match.group(1))
            spec_token = match.group(2)
            ref = re.match(rb"(\d+)\s+\d+\s+R", spec_token)
            spec = self.objects.get(int(ref.group(1)), b"") if ref else spec_token
            entries.append((page_index, spec))
        return sorted(entries)

    def _name_value(self, spec: bytes, key: bytes) -> Optional[str]:
        match = re.search(rb"/" + re.escape(key) + rb"\s*/([A-Za-z]+)", spec)
        return match.group(1).decode("ascii", "ignore") if match else None

    def _literal_value(self, spec: bytes, key: bytes) -> Optional[str]:
        match = re.search(rb"/" + re.escape(key) + rb"\s*\((.*?)\)", spec, re.S)
        return pdf_literal_to_text(match.group(1)) if match else None

    def _int_value(self, spec: bytes, key: bytes) -> Optional[int]:
        match = re.search(rb"/" + re.escape(key) + rb"\s+(\d+)", spec)
        return int(match.group(1)) if match else None


def indirect_refs(data: bytes) -> List[int]:
    return [int(match.group(1)) for match in re.finditer(rb"(\d+)\s+\d+\s+R", data)]


def extract_text_from_pdf_content(content: bytes) -> str:
    if not content:
        return ""
    texts: List[str] = []
    token_pattern = re.compile(
        rb"(\[(?:[^\[\]]|\((?:\\.|[^\\)])*\)|<[^<>]*>)*\]\s*TJ|"
        rb"\((?:\\.|[^\\)])*\)\s*Tj|"
        rb"<[0-9A-Fa-f\s]+>\s*Tj|"
        rb"\((?:\\.|[^\\)])*\)\s*'|"
        rb"\((?:\\.|[^\\)])*\)\s*\")",
        re.S,
    )
    for match in token_pattern.finditer(content):
        token = match.group(0)
        stripped = token.strip()
        if stripped.endswith(b"TJ"):
            text = "".join(pdf_string_tokens_to_text(token))
        else:
            strings = pdf_string_tokens_to_text(token)
            text = strings[0] if strings else ""
        text = cleanup_pdf_text_piece(text)
        if text:
            texts.append(text)
    return "\n".join(texts)


def pdf_string_tokens_to_text(data: bytes) -> List[str]:
    out: List[str] = []
    pos = 0
    while pos < len(data):
        if data[pos : pos + 1] == b"(":
            end = find_literal_end(data, pos)
            if end is None:
                break
            out.append(pdf_literal_to_text(data[pos + 1 : end]))
            pos = end + 1
            continue
        if data[pos : pos + 1] == b"<" and data[pos : pos + 2] != b"<<":
            end = data.find(b">", pos + 1)
            if end < 0:
                break
            out.append(pdf_hex_to_text(data[pos + 1 : end]))
            pos = end + 1
            continue
        pos += 1
    return out


def find_literal_end(data: bytes, start: int) -> Optional[int]:
    depth = 1
    pos = start + 1
    while pos < len(data):
        ch = data[pos]
        if ch == 92:
            pos += 2
            continue
        if ch == 40:
            depth += 1
        elif ch == 41:
            depth -= 1
            if depth == 0:
                return pos
        pos += 1
    return None


def pdf_literal_to_text(data: bytes) -> str:
    out = bytearray()
    pos = 0
    while pos < len(data):
        ch = data[pos]
        if ch == 92 and pos + 1 < len(data):
            pos += 1
            esc = data[pos]
            escape_map = {
                ord("n"): 10,
                ord("r"): 13,
                ord("t"): 9,
                ord("b"): 8,
                ord("f"): 12,
                ord("("): 40,
                ord(")"): 41,
                ord("\\"): 92,
            }
            if esc in escape_map:
                out.append(escape_map[esc])
                pos += 1
                continue
            if 48 <= esc <= 55:
                octal = bytes([esc])
                pos += 1
                while pos < len(data) and len(octal) < 3 and 48 <= data[pos] <= 55:
                    octal += bytes([data[pos]])
                    pos += 1
                out.append(int(octal, 8) & 0xFF)
                continue
            if esc in (10, 13):
                if esc == 13 and pos + 1 < len(data) and data[pos + 1] == 10:
                    pos += 1
                pos += 1
                continue
            out.append(esc)
            pos += 1
            continue
        out.append(ch)
        pos += 1
    raw = bytes(out)
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    return raw.decode("cp1252", "replace")


def pdf_hex_to_text(data: bytes) -> str:
    clean = re.sub(rb"\s+", b"", data)
    if len(clean) % 2:
        clean += b"0"
    try:
        raw = bytes.fromhex(clean.decode("ascii"))
    except ValueError:
        return ""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    return raw.decode("cp1252", "replace")


def cleanup_pdf_text_piece(text: str) -> str:
    text = text.replace("\x00", "")
    text = "".join(ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C"))
    return text.strip()
