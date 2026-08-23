from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import FrozenInstanceError
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from src.me_finder.app_context import AppContext, AppPaths
from src.me_finder.database import build_database
from src.me_finder.component_catalog import ComponentCatalog
from src.me_finder.managed_mineru import ManagedMinerU
from src.me_finder.pdf_import_service import rebuild_local_index
from src.me_finder.web import make_handler


class AppContextTests(unittest.TestCase):
    @staticmethod
    def _request_json(
        server: ThreadingHTTPServer,
        method: str,
        path: str,
        payload: object | None = None,
    ) -> tuple[int, dict[str, object]]:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            }
        connection = HTTPConnection(
            "127.0.0.1", server.server_port, timeout=5
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return (
                response.status,
                json.loads(response.read().decode("utf-8")),
            )
        finally:
            connection.close()

    def test_relative_index_is_resolved_against_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            paths = AppPaths.create(root, index_path=Path("indexes/current.sqlite3"))

        self.assertEqual(paths.runtime_root, root)
        self.assertEqual(paths.index_path, root / "indexes" / "current.sqlite3")
        self.assertEqual(paths.config_root, root / "config")
        self.assertEqual(paths.corpus_root, root / "corpus")

    def test_context_does_not_depend_on_later_working_directory_changes(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            original = Path.cwd()
            try:
                context = AppContext.create(Path(first), index_path=Path("data/index.sqlite3"))
                os.chdir(second)
                self.assertEqual(
                    context.paths.index_path,
                    Path(first).resolve() / "data" / "index.sqlite3",
                )
                self.assertEqual(context.paths.runtime_root, Path(first).resolve())
            finally:
                os.chdir(original)

    def test_paths_are_immutable_after_composition(self) -> None:
        context = AppContext.create(Path.cwd())
        with self.assertRaises(FrozenInstanceError):
            context.paths.runtime_root = Path("/tmp")  # type: ignore[misc]

    def test_rebuild_can_publish_to_composed_custom_index_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            config_path = root / "config" / "pdf_imports.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"documents": []}', encoding="utf-8")
            context = AppContext.create(
                root,
                index_path=Path("custom/live.sqlite3"),
            )
            build_database({"metadata": {}}, context.paths.index_path)

            rebuild_local_index(
                root,
                database_path=context.paths.index_path,
            )

            connection = sqlite3.connect(context.paths.index_path)
            try:
                source_count = connection.execute(
                    "SELECT COUNT(*) FROM source_files"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(source_count, 0)
            self.assertFalse((root / "data" / "index.sqlite3").exists())

    def test_web_background_rebuild_receives_custom_index_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "runtime"
            config_path = root / "config" / "pdf_imports.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"documents": []}', encoding="utf-8")
            context = AppContext.create(
                root,
                index_path=Path("custom/live.sqlite3"),
            )
            build_database({"metadata": {}}, context.paths.index_path)
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w") as archive:
                archive.writestr(
                    "backup.json",
                    json.dumps(
                        {
                            "marker": "me_finder_backup",
                            "version": 1,
                        }
                    ),
                )
            backup_path = base / "backup.zip"
            backup_path.write_bytes(archive_buffer.getvalue())

            handler = make_handler(
                context.paths.index_path,
                app_context=context,
            )
            handler.log_message = lambda *_args: None
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            received_paths: list[Path] = []

            def record_rebuild(
                _root: Path,
                _on_progress=None,
                *,
                database_path: Path | None = None,
            ) -> dict[str, object]:
                if database_path is not None:
                    received_paths.append(Path(database_path))
                return {}

            try:
                with patch(
                    "src.me_finder.web.rebuild_local_index",
                    side_effect=record_rebuild,
                ):
                    server_thread.start()
                    status, response = self._request_json(
                        server,
                        "POST",
                        "/api/backup/import",
                        {"path": str(backup_path)},
                    )
                    self.assertEqual(status, 200)
                    job_id = str(response["job_id"])
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        status, job = self._request_json(
                            server,
                            "GET",
                            f"/api/import-status?job_id={job_id}",
                        )
                        if job.get("status") in {"completed", "failed"}:
                            break
                        time.sleep(0.02)
                    else:
                        self.fail("backup rebuild job did not finish")
                    self.assertEqual(status, 200)
                    self.assertEqual(job.get("status"), "completed")
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                server_thread.join(timeout=2)

            self.assertEqual(received_paths, [context.paths.index_path])
            self.assertFalse((root / "data" / "index.sqlite3").exists())

    def test_desktop_startup_schedules_component_catalog_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            context = AppContext.create(root, index_path=Path("data/index.sqlite3"))
            build_database({"metadata": {}}, context.paths.index_path)
            with (
                patch.dict(os.environ, {"ME_FINDER_DESKTOP_SHELL": "macos"}),
                patch.object(
                    ComponentCatalog,
                    "start_background_check",
                    return_value=True,
                ) as start_check,
                patch.object(
                    ManagedMinerU,
                    "start_installed_if_managed",
                    return_value=False,
                ),
            ):
                handler = make_handler(context.paths.index_path, app_context=context)
            try:
                start_check.assert_called_once()
                self.assertTrue(callable(start_check.call_args.kwargs["on_updated"]))
            finally:
                handler.close_runtime()


if __name__ == "__main__":
    unittest.main()
