"""Thread-safe ownership of the live search index."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
)

from ..app_context import AppPaths
from ..search import SearchEngine
from .search_service import SearchRequest, SearchService


EngineFactory = Callable[[Path], SearchEngine]
RebuildIndex = Callable[..., Dict[str, object]]
ReplaceSource = Callable[..., Dict[str, object]]
ProgressCallback = Callable[[Dict[str, object]], None]
T = TypeVar("T")


class IndexRuntime:
    """Own one live ``SearchEngine`` and publish rebuilt indexes atomically."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        engine_factory: EngineFactory,
        rebuild_index: RebuildIndex,
        replace_source: ReplaceSource,
    ) -> None:
        self.paths = paths
        self._engine_factory = engine_factory
        self._rebuild_index = rebuild_index
        self._replace_source = replace_source
        self._state_lock = threading.RLock()
        # Re-entrant because callers may reserve or edit configuration while
        # invoking rebuild()/replace_source(), which acquire the same region.
        self._mutation_lock = threading.RLock()
        self._rebuilding = False
        self._closing = False

        engine = self._engine_factory(self.paths.index_path)
        self._engine: Optional[SearchEngine] = engine
        self._install_catalog(engine)

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """Serialize configuration changes with index publication."""

        with self._mutation_lock:
            yield

    @property
    def rebuilding(self) -> bool:
        with self._state_lock:
            return self._rebuilding

    @property
    def closing(self) -> bool:
        with self._state_lock:
            return self._closing

    def metadata(self) -> Dict[str, object]:
        with self._state_lock:
            return dict(self._index_metadata)

    def catalog(self) -> Dict[str, List[Dict[str, object]]]:
        with self._state_lock:
            return {
                "source_files": list(self._sources),
                "volumes": list(self._volumes),
                "works": list(self._works),
                "folders": list(self._folders),
                "document_groups": list(self._document_groups),
            }

    def source(self, source_file_id: str) -> Optional[Dict[str, object]]:
        with self._state_lock:
            source = self._source_files.get(str(source_file_id))
            return dict(source) if source is not None else None

    def search(self, request: SearchRequest) -> Optional[Dict[str, object]]:
        """Search while holding the engine alive against concurrent writes."""

        with self._state_lock:
            if self._rebuilding or self._engine is None:
                return None
            return SearchService.execute(self._engine, request)

    def run_when_ready(
        self,
        operation: Callable[[Path], T],
    ) -> Optional[T]:
        """Run a database read while publication cannot replace the index."""

        with self._state_lock:
            if self._rebuilding or self._engine is None:
                return None
            return operation(self.paths.index_path)

    def suspend(self) -> None:
        """Mark reads unavailable and close the current SQLite handle."""

        with self._state_lock:
            self._rebuilding = True
            engine = self._engine
            self._engine = None
            if engine is not None:
                engine.close()

    def reopen(self, *, attempts: int = 1) -> bool:
        """Open the current database and publish it unless shutdown is terminal."""

        published, _source_ids = self._open_and_publish(attempts=attempts)
        return published

    def rebuild(
        self,
        on_progress: Optional[ProgressCallback] = None,
        expected_source_ids: Sequence[str] = (),
    ) -> Set[str]:
        """Rebuild the full database and return expected sources still missing."""

        expected = {
            str(source_file_id)
            for source_file_id in expected_source_ids
            if str(source_file_id)
        }
        with self.mutation():
            self.suspend()
            try:
                self._rebuild_index(
                    self.paths.runtime_root,
                    on_progress,
                    database_path=self.paths.index_path,
                )
                _published, indexed_source_ids = self._open_and_publish(attempts=1)
            except Exception:
                if self.closing:
                    with self._state_lock:
                        self._rebuilding = False
                    raise
                self.reopen()
                raise
        return expected.difference(indexed_source_ids)

    def replace_source(
        self,
        extracted: Mapping[str, object],
        expected_source_id: str,
        *,
        backup_existing: bool = False,
    ) -> None:
        """Transactionally replace one source and republish the live engine."""

        with self.mutation():
            self.suspend()
            try:
                self._replace_source(
                    extracted,
                    self.paths.index_path,
                    backup_existing=backup_existing,
                )
            except Exception as write_error:
                try:
                    self.reopen(attempts=5)
                except Exception:
                    logging.exception(
                        "index write failed and the previous runtime index "
                        "could not be reopened"
                    )
                raise write_error.with_traceback(write_error.__traceback__)
            self.reopen(attempts=5)
            with self._state_lock:
                indexed = expected_source_id in self._source_files
            if not indexed:
                raise RuntimeError(
                    f"写入后未找到文献记录：{expected_source_id}"
                )

    def begin_shutdown(self) -> None:
        with self._state_lock:
            self._closing = True

    def close(self) -> None:
        with self._state_lock:
            self._closing = True
            engine = self._engine
            self._engine = None
        if engine is not None:
            engine.close()

    def _open_and_publish(self, *, attempts: int) -> Tuple[bool, Set[str]]:
        for attempt in range(attempts):
            try:
                engine = self._engine_factory(self.paths.index_path)
                break
            except Exception:
                if attempt + 1 == attempts:
                    with self._state_lock:
                        self._rebuilding = True
                    raise
                time.sleep(0.05 * (2**attempt))

        (
            sources,
            volumes,
            works,
            folders,
            document_groups,
            metadata,
            source_files,
        ) = self._catalog_from(engine)
        indexed_source_ids = set(source_files)
        with self._state_lock:
            self._sources = sources
            self._volumes = volumes
            self._works = works
            self._folders = folders
            self._document_groups = document_groups
            self._index_metadata = metadata
            self._source_files = source_files
            self._rebuilding = False
            if self._closing:
                engine.close()
                return False, indexed_source_ids
            previous_engine = self._engine
            self._engine = engine
            if previous_engine is not None:
                previous_engine.close()
        return True, indexed_source_ids

    def _install_catalog(self, engine: SearchEngine) -> None:
        (
            self._sources,
            self._volumes,
            self._works,
            self._folders,
            self._document_groups,
            self._index_metadata,
            self._source_files,
        ) = self._catalog_from(engine)

    @staticmethod
    def _catalog_from(
        engine: SearchEngine,
    ) -> Tuple[
        List[Dict[str, object]],
        List[Dict[str, object]],
        List[Dict[str, object]],
        List[Dict[str, object]],
        List[Dict[str, object]],
        Dict[str, object],
        Dict[str, Dict[str, object]],
    ]:
        sources = list(engine.index.get("source_files", []))
        volumes = list(engine.index.get("volumes", []))
        works = list(engine.index.get("works", []))
        folders = list(engine.index.get("folders", []))
        document_groups = list(engine.index.get("document_groups", []))
        metadata = dict(engine.index.get("metadata", {}))
        source_files = {
            str(item.get("source_file_id")): item
            for item in sources
            if item.get("source_file_id")
        }
        return (
            sources,
            volumes,
            works,
            folders,
            document_groups,
            metadata,
            source_files,
        )
