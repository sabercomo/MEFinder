"""Recoverable file switch paired with an atomic index callback."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Callable, Optional

from .io_utils import stream_copy


class AtomicPublishError(RuntimeError):
    pass


class AtomicPublisher:
    """Publish a fully validated candidate, rolling the file back on index failure."""

    def publish(
        self,
        candidate: Path,
        destination: Path,
        *,
        publish_index: Optional[Callable[[], None]] = None,
    ) -> Path:
        source = Path(candidate)
        target = Path(destination)
        if not source.is_file():
            raise AtomicPublishError("validated export candidate does not exist")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.previous")
        with source.open("rb") as input_stream, partial.open("wb") as output_stream:
            stream_copy(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        had_previous = target.is_file()
        if had_previous:
            target.replace(backup)
        try:
            partial.replace(target)
            if publish_index is not None:
                publish_index()
        except Exception:
            target.unlink(missing_ok=True)
            if had_previous and backup.exists():
                backup.replace(target)
            raise
        else:
            backup.unlink(missing_ok=True)
        finally:
            partial.unlink(missing_ok=True)
        return target
