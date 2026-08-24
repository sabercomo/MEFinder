"""Read-side document workflows independent of HTTP and desktop shells."""

from __future__ import annotations

from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TypeVar,
)

from ..app_context import AppPaths
from ..bibliographic_metadata import (
    canonical_metadata,
    detect_pdf_bibliographic_metadata,
)
from ..calibration_library import (
    build_calibration_library,
    build_library,
    build_library_detail,
    summarize_library,
)
from ..pdf_import_service import load_import_config
from .document_read_port import DocumentReadPort


T = TypeVar("T")


class DocumentIndexPort(Protocol):
    """The read surface supplied by the live index runtime."""

    def catalog(self) -> Dict[str, List[Dict[str, object]]]:
        ...

    def source(self, source_file_id: str) -> Optional[Dict[str, object]]:
        ...

    def run_when_ready(
        self, operation: Callable[[Path], T]
    ) -> Optional[T]:
        ...


ActiveSourceIds = Callable[[], Iterable[str]]
ConfigLoader = Callable[[Path], Dict[str, object]]


class MetadataDetector(Protocol):
    def __call__(
        self,
        path: Path,
        pages: Sequence[Mapping[str, object]],
        document: Mapping[str, object],
        *,
        force: bool = False,
    ) -> Dict[str, object]:
        ...


class DocumentQueryError(ValueError):
    """A document cannot be read through the configured application state."""


class DocumentQueryUnavailable(RuntimeError):
    """The live index cannot safely serve a document query right now."""


class DocumentQueryService:
    """Compose catalog, import config and SQLite data for document reads."""

    def __init__(
        self,
        paths: AppPaths,
        index: DocumentIndexPort,
        *,
        repository: DocumentReadPort,
        active_source_ids: ActiveSourceIds,
        config_loader: ConfigLoader = load_import_config,
        metadata_detector: MetadataDetector = detect_pdf_bibliographic_metadata,
    ) -> None:
        self.paths = paths
        self._index = index
        self._active_source_ids = active_source_ids
        self._config_loader = config_loader
        self._metadata_detector = metadata_detector
        self._repository = repository

    @property
    def config_path(self) -> Path:
        return self.paths.config_root / "pdf_imports.json"

    def library_data(
        self,
        *,
        additional_active_source_ids: Iterable[str] = (),
    ) -> Dict[str, object]:
        sources, volumes, works, documents, active = self._library_context(
            additional_active_source_ids
        )
        return build_library(
            self.paths.runtime_root,
            sources,
            volumes,
            works,
            documents,
            latest_runs=self.latest_pdf_import_runs(),
            active_source_ids=active,
            language_samples=self.language_samples(
                str(source.get("source_file_id") or "")
                for source in sources
                if source.get("source_file_id")
            ),
        )

    def calibration_library_data(
        self,
        *,
        additional_active_source_ids: Iterable[str] = (),
    ) -> Dict[str, object]:
        sources, volumes, _works, documents, active = self._library_context(
            additional_active_source_ids
        )
        return build_calibration_library(
            self.paths.runtime_root,
            sources,
            volumes,
            documents,
            latest_runs=self.latest_pdf_import_runs(),
            active_source_ids=active,
        )

    def library_summary(
        self,
        *,
        additional_active_source_ids: Iterable[str] = (),
    ) -> Dict[str, object]:
        return summarize_library(
            self.library_data(
                additional_active_source_ids=additional_active_source_ids
            )
        )

    def library_detail(
        self,
        source_file_id: str,
        *,
        additional_active_source_ids: Iterable[str] = (),
    ) -> Optional[Dict[str, object]]:
        return build_library_detail(
            self.library_data(
                additional_active_source_ids=additional_active_source_ids
            ),
            source_file_id,
        )

    def latest_pdf_import_runs(self) -> Dict[str, Dict[str, object]]:
        result = self._index.run_when_ready(
            self._repository.latest_pdf_import_runs
        )
        if result is None:
            raise DocumentQueryUnavailable("索引正在重建，请稍后再加载文献。")
        return result

    def language_samples(
        self, source_file_ids: Iterable[str]
    ) -> Dict[str, str]:
        source_ids = tuple(dict.fromkeys(str(value) for value in source_file_ids))
        result = self._index.run_when_ready(
            lambda database_path: self._repository.language_samples(
                database_path, source_ids
            )
        )
        if result is None:
            raise DocumentQueryUnavailable("索引正在重建，请稍后再加载文献。")
        return result

    def source_path(self, source_file_id: str) -> Path:
        record = self._index.source(source_file_id)
        if not record:
            raise DocumentQueryError("文献未找到。")
        target = (
            self.paths.runtime_root / str(record.get("relative_path") or "")
        ).resolve()
        root = self.paths.runtime_root
        if target != root and root not in target.parents:
            raise DocumentQueryError("拒绝打开应用目录外的文件。")
        if (
            target.suffix.lower() not in {".pdf", ".doc", ".docx", ".epub"}
            or not target.exists()
        ):
            raise DocumentQueryError("原始文件不存在。")
        return target

    def configured_document(
        self, source_file_id: str
    ) -> Tuple[Path, Dict[str, object], Dict[str, object]]:
        if not self.config_path.exists():
            raise DocumentQueryError("PDF 导入配置不存在。")
        config = self._config_loader(self.config_path)
        document = next(
            (
                item
                for item in config.get("documents", [])
                if item.get("source_file_id") == source_file_id
            ),
            None,
        )
        if not document:
            raise DocumentQueryError("PDF 配置中找不到该文献。")
        return self.config_path, config, document

    def front_matter_pages(
        self,
        source_file_id: str,
        *,
        limit: int = 20,
        tail: int = 8,
    ) -> List[Dict[str, object]]:
        """Return opening pages plus trailing colophon pages."""

        result = self._index.run_when_ready(
            lambda database_path: self._repository.front_matter_pages(
                database_path,
                source_file_id,
                limit=limit,
                tail=tail,
            )
        )
        if result is None:
            raise DocumentQueryUnavailable("索引正在重建，请稍后再读取文献。")
        return result

    def detect_bibliographic_metadata(
        self,
        source_file_id: str,
        *,
        force: bool = False,
    ) -> Dict[str, object]:
        _config_path, _config, document = self.configured_document(
            source_file_id
        )
        return self._metadata_detector(
            self.source_path(source_file_id),
            self.front_matter_pages(source_file_id),
            document,
            force=force,
        )

    def bibliographic_metadata(
        self, source_file_id: str
    ) -> Dict[str, object]:
        _config_path, _config, document = self.configured_document(
            source_file_id
        )
        return canonical_metadata(document)

    def batch_metadata_candidates(
        self,
        *,
        additional_active_source_ids: Iterable[str] = (),
    ) -> List[Dict[str, object]]:
        """Return non-manual PDFs that still merit metadata recognition."""

        candidates: List[Dict[str, object]] = []
        library = self.library_data(
            additional_active_source_ids=additional_active_source_ids
        )
        for item in library.get("items", []):
            if str(item.get("source_type") or "") != "pdf":
                continue
            nested = item.get("bibliographic_metadata")
            metadata = nested if isinstance(nested, Mapping) else {}
            source = str(
                metadata.get("metadata_source")
                or item.get("metadata_source")
                or ""
            )
            if source == "manual":
                continue
            document_type = str(
                item.get("document_type")
                or metadata.get("document_type")
                or ""
            )
            if item.get("metadata_missing_fields") or document_type in {
                "book",
                "translated_book",
            }:
                candidates.append(item)
        return candidates

    def _library_context(
        self,
        additional_active_source_ids: Iterable[str],
    ) -> Tuple[
        List[Dict[str, object]],
        List[Dict[str, object]],
        List[Dict[str, object]],
        List[Dict[str, object]],
        set[str],
    ]:
        config = self._config_loader(self.config_path)
        catalog = self._index.catalog()
        active = {str(value) for value in self._active_source_ids()}
        active.update(str(value) for value in additional_active_source_ids)
        return (
            catalog["source_files"],
            catalog["volumes"],
            catalog["works"],
            config.get("documents", []),
            active,
        )
