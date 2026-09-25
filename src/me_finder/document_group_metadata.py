"""Compatibility imports for document-group metadata formatting."""

from .persistence.document_group_metadata import (
    VERSION_LABEL_MAX_LENGTH,
    canonical_version_label,
    member_display_name,
)

__all__ = ["VERSION_LABEL_MAX_LENGTH", "canonical_version_label", "member_display_name"]
