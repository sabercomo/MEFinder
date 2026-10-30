"""Process-local mutual exclusion shared by OCR runners and installers."""

from __future__ import annotations

import threading


_ENGINE_LOCKS = {
    "ndlocr-lite": threading.Lock(),
    "ndlkotenocr-lite": threading.Lock(),
}


def local_ocr_engine_lock(provider_id: str) -> threading.Lock:
    return _ENGINE_LOCKS[provider_id]
