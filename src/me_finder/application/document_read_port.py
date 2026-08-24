"""Persistence port used by document-facing application workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Protocol


class DocumentReadPort(Protocol):
    def front_matter_pages(
        self,
        database_path: Path,
        source_file_id: str,
        *,
        limit: int,
        tail: int,
    ) -> List[Dict[str, object]]: ...

    def latest_pdf_import_runs(
        self,
        database_path: Path,
    ) -> Dict[str, Dict[str, object]]: ...

    def language_samples(
        self,
        database_path: Path,
        source_file_ids: Iterable[str],
    ) -> Dict[str, str]: ...
