"""Coordinate request-runtime shutdown and its background owners."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .. import translation_works
from ..embedding_runtime import request_embedding_cancel
from ..import_assembly import ImportAssembly
from ..managed_component_assembly import ManagedComponents
from ..zotero_sync import ZoteroSyncService
from .background_tasks import BackgroundTasks


class RuntimeLifecycle:
    """Keep SQLite open until durable and background work has stopped."""

    def __init__(
        self,
        imports: ImportAssembly,
        managed: ManagedComponents,
        zotero_sync: ZoteroSyncService,
    ) -> None:
        self.imports = imports
        self.managed = managed
        self.zotero_sync = zotero_sync
        self.background_tasks = BackgroundTasks()

    def start(self, index_path: Path) -> None:
        """Start the runtime-owned scheduler and read-only warm-up."""

        self.zotero_sync.start_scheduler()
        translation_works.start_body_bounds_warm_up(index_path, self.background_tasks)

    def begin_shutdown(self) -> None:
        """Reject new writes and request every background owner to stop."""

        request_embedding_cancel()
        self.background_tasks.begin_shutdown()
        self.zotero_sync.stop()
        self.managed.catalog.begin_shutdown()
        self.managed.embedding_models.begin_shutdown()
        self.managed.alignment_runtime.begin_shutdown()
        self.imports.durable_operations.begin_shutdown()
        self.imports.index_runtime.begin_shutdown()
        self.imports.import_task_queue.shutdown(wait=False)

    def close_runtime(self, timeout: float = 2.0) -> bool:
        """Release SQLite only after its users have exited; allow retry on timeout."""

        self.begin_shutdown()
        self.imports.document_imports.close()
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        if not self.imports.durable_operations.wait(timeout=timeout):
            logging.warning("durable mutations are still committing; runtime engine kept open")
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.imports.import_task_queue.shutdown(wait=True, timeout=remaining):
            logging.warning("background imports are still stopping; runtime engine kept open")
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.background_tasks.close(timeout=remaining):
            logging.warning("background warm-up is still stopping; runtime engine kept open")
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.zotero_sync.close(timeout=remaining):
            logging.warning("zotero sync is still stopping; runtime engine kept open")
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.managed.catalog.close(timeout=remaining):
            logging.warning("component catalog is still checking; runtime engine kept open")
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.managed.mineru.close(timeout=remaining):
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.managed.local_ocr.close(timeout=remaining):
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.managed.embedding_models.close(timeout=remaining):
            return False
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.managed.alignment_runtime.close(timeout=remaining):
            return False
        self.imports.index_runtime.close()
        return True
