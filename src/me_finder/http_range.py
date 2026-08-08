"""Pure HTTP byte-range parsing used by the local source-file adapter."""

from __future__ import annotations

from dataclasses import dataclass


class InvalidByteRange(ValueError):
    """Raised for malformed, multiple, or unsatisfiable byte ranges."""


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_byte_range(value: str | None, file_size: int) -> ByteRange | None:
    """Parse one RFC 9110 ``bytes`` range and clamp it to ``file_size``."""

    if not value:
        return None
    if file_size < 0:
        raise ValueError("file_size must not be negative")
    text = value.strip()
    if not text.startswith("bytes=") or "," in text:
        raise InvalidByteRange("只支持单个 bytes 范围。")
    bounds = text[len("bytes=") :].strip()
    if bounds.count("-") != 1:
        raise InvalidByteRange("文件范围格式无效。")
    raw_start, raw_end = (part.strip() for part in bounds.split("-", 1))
    if not raw_start and not raw_end:
        raise InvalidByteRange("文件范围格式无效。")
    if file_size == 0:
        raise InvalidByteRange("空文件没有可读取的字节范围。")
    try:
        if not raw_start:
            suffix_length = int(raw_end)
            if suffix_length <= 0:
                raise InvalidByteRange("文件范围格式无效。")
            start = max(0, file_size - suffix_length)
            return ByteRange(start=start, end=file_size - 1)
        start = int(raw_start)
        end = file_size - 1 if not raw_end else int(raw_end)
    except ValueError as exc:
        if isinstance(exc, InvalidByteRange):
            raise
        raise InvalidByteRange("文件范围格式无效。") from exc
    if start < 0 or end < start or start >= file_size:
        raise InvalidByteRange("请求的文件范围不存在。")
    return ByteRange(start=start, end=min(end, file_size - 1))
