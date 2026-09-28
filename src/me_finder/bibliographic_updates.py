"""Stable API for the agent bibliographic-fill queue; SQLite lives in persistence."""

from .persistence.bibliographic_update_store import (
    BibliographicUpdateNotFound,
    confirm_pending_request,
    insert_pending_request,
    list_requests,
    record_request_result,
)

__all__ = [
    "BibliographicUpdateNotFound",
    "confirm_pending_request",
    "insert_pending_request",
    "list_requests",
    "record_request_result",
]
