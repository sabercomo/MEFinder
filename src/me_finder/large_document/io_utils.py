"""Bounded-memory file primitives shared by large-document components."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import BinaryIO, Iterator


DEFAULT_CHUNK_SIZE = 1024 * 1024


def iter_file_chunks(
    path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> Iterator[bytes]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                return
            yield chunk


def sha256_file(path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    for chunk in iter_file_chunks(path, chunk_size=chunk_size):
        digest.update(chunk)
    return digest.hexdigest()


def stream_copy(source: BinaryIO, target: BinaryIO, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> int:
    copied = 0
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            return copied
        target.write(chunk)
        copied += len(chunk)


def fsync_path(path: Path) -> None:
    with Path(path).open("rb+") as stream:
        stream.flush()
        os.fsync(stream.fileno())

