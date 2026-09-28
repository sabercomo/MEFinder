"""Place verified quotes inside the reader outline (chapter → section path).

Uses the same indexed outline and ordering as ``list_sections`` so the
``section_index`` here can be passed straight to ``read_document_window``.
Outlines are loaded once per source per call; a document without a usable
outline simply yields no section rather than a guessed one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .document_sections import clean_title


class QuoteSectionLocator:
    def __init__(self, index_path: Path) -> None:
        self._index_path = index_path
        self._outlines: dict[str, list[Mapping[str, object]]] = {}

    def section_for(self, fields: Mapping[str, object]) -> dict[str, object] | None:
        """Return the innermost enclosing heading and its title path, if any."""

        source_id = str(fields["source_file_id"])
        entries = self._entries(source_id)
        if not entries:
            return None
        if str(fields["source_type"]) == "pdf":
            spans = fields.get("page_match_spans")
            first_span = spans[0] if isinstance(spans, list) and spans else {}
            char = first_span.get("page_char_start") if isinstance(first_span, Mapping) else None
            # Without a page offset, headings on the hit page count as preceding.
            position = (
                int(fields.get("pdf_page_start_index") or 0),
                int(char) if char is not None else float("inf"),
            )
        else:
            position = (int(fields.get("paragraph_index") or 0), float("inf"))
        path: dict[int, str] = {}
        innermost: int | None = None
        for index, entry in enumerate(entries):
            if (int(entry["item_index"]), int(entry.get("char_start") or 0)) > position:
                break
            level = int(entry["level"])
            path = {key: value for key, value in path.items() if key < level}
            path[level] = clean_title(entry.get("title"))
            innermost = index
        if innermost is None:
            return None
        return {
            "section_index": innermost,
            "path": [path[level] for level in sorted(path)],
        }

    def _entries(self, source_id: str) -> list[Mapping[str, object]]:
        from ..document_outline import get_document_outline
        from ..structured_reader import StructuredReaderError

        if source_id not in self._outlines:
            try:
                outline = get_document_outline(self._index_path, source_id)
            except StructuredReaderError:
                outline = {"entries": []}
            self._outlines[source_id] = sorted(
                (entry for entry in outline["entries"] if isinstance(entry, Mapping)),
                key=lambda entry: (
                    int(entry["item_index"]),
                    int(entry.get("char_start") or 0),
                ),
            )
        return self._outlines[source_id]
