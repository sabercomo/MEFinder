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
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from src.me_finder.app_context import AppContext
from src.me_finder.bibliographic_metadata import update_metadata_in_database
from src.me_finder.database import build_database
from src.me_finder.data_location import (
    DATA_ROOT_MARKER,
    DataLocationError,
    data_location_summary,
    default_macos_data_root,
    migrate_data_root,
    proposed_data_root,
    read_macos_data_root,
)
from src.me_finder.web import make_handler


def _create_current_data_root(root: Path) -> None:
    database = root / "runtime" / "data" / "index.sqlite3"
    database.parent.mkdir(parents=True)
    # sqlite3 的上下文管理器只提交事务，并不关闭连接；Windows 无法删除仍被
    # 打开的数据库文件，因此这里必须显式关闭。
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('原句')")
        connection.commit()
    finally:
        connection.close()
    config = root / "runtime" / "config"
    config.mkdir(parents=True)
    (config / "pdf_imports.json").write_text(
        '{"documents": []}\n',
        encoding="utf-8",
    )
    corpus = root / "runtime" / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "sample.pdf").write_bytes(b"%PDF-test")
    (root / "preferences.json").write_text(
        '{"theme": "midnight"}\n',
        encoding="utf-8",
    )


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
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def _make_web_runtime(base: Path):
    app_data = base / "current" / "MEFinder"
    runtime = app_data / "runtime"
    index_path = runtime / "data" / "index.sqlite3"
    config_path = runtime / "config" / "pdf_imports.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "source_file_id": "pdf-one",
                        "file_name": "one.pdf",
                        "title": "Old",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    build_database(
        {
            "metadata": {},
            "source_files": [
                {
                    "source_file_id": "pdf-one",
                    "source_type": "pdf",
                    "file_name": "one.pdf",
                    "title": "Old",
                }
            ],
        },
        index_path,
    )
    context = AppContext.create(
        runtime,
        index_path=index_path,
        app_data_root=app_data,
        default_app_data_root=app_data,
    )
    handler = make_handler(index_path, app_context=context)
    handler.log_message = lambda *_args: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    return app_data, runtime, handler, server, thread


class DataLocationTests(unittest.TestCase):
    def test_default_location_and_valid_marker_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            default = default_macos_data_root(home)
            self.assertEqual(
                default,
                home / "Library" / "Application Support" / "MEFinder",
            )
            self.assertEqual(read_macos_data_root(home), default)

            custom = Path(directory) / "OneDrive" / "MEFinder"
            default.mkdir(parents=True)
            (default / DATA_ROOT_MARKER).write_text(
                str(custom) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(read_macos_data_root(home), custom.resolve())

    def test_invalid_relative_marker_falls_back_to_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            default = default_macos_data_root(home)
            default.mkdir(parents=True)
            (default / DATA_ROOT_MARKER).write_text(
                "../relative-location\n",
                encoding="utf-8",
            )

            self.assertEqual(read_macos_data_root(home), default)

    def test_selected_parent_gets_a_mefinder_child(self) -> None:
        # 「绝对路径」的写法依平台而异：Windows 要有盘符，POSIX 用挂载点。
        # 写死 /Volumes/... 会让这条规则在 Windows 上被误判为相对路径。
        parent = Path("C:/PortableSSD") if os.name == "nt" else Path("/Volumes/PortableSSD")
        expected = parent / "MEFinder"
        self.assertEqual(proposed_data_root(parent), expected)
        self.assertEqual(proposed_data_root(expected), expected)
        with self.assertRaisesRegex(DataLocationError, "完整"):
            proposed_data_root("OneDrive")

    def test_summary_distinguishes_default_and_custom_locations(self) -> None:
        default = Path("/Users/example/Library/Application Support/MEFinder")
        self.assertFalse(data_location_summary(default, default)["is_custom"])
        self.assertTrue(
            data_location_summary(
                Path("/Volumes/SSD/MEFinder"),
                default,
            )["is_custom"]
        )

    def test_migration_copies_all_data_validates_sqlite_and_retains_old_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = base / "home" / "Library" / "Application Support" / "MEFinder"
            target = base / "OneDrive" / "MEFinder"
            _create_current_data_root(current)
            (current / "runtime/data/index.sqlite3-wal").write_bytes(b"")
            (current / "runtime/data/index.sqlite3-shm").write_bytes(b"")

            result = migrate_data_root(current, target, current)

            self.assertTrue(result["restart_required"])
            self.assertTrue(result["old_data_retained"])
            self.assertTrue((current / "runtime/data/index.sqlite3").is_file())
            self.assertEqual(
                (target / "runtime/corpus/sample.pdf").read_bytes(),
                b"%PDF-test",
            )
            self.assertEqual(
                (target / "preferences.json").read_text(encoding="utf-8"),
                '{"theme": "midnight"}\n',
            )
            connection = sqlite3.connect(target / "runtime/data/index.sqlite3")
            try:
                self.assertEqual(
                    connection.execute("SELECT value FROM sample").fetchone()[0],
                    "原句",
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                connection.close()
            self.assertFalse((target / "runtime/data/index.sqlite3-wal").exists())
            self.assertFalse((target / "runtime/data/index.sqlite3-shm").exists())
            self.assertEqual(
                (current / DATA_ROOT_MARKER).read_text(encoding="utf-8").strip(),
                str(target.resolve()),
            )
            self.assertFalse(any(target.parent.glob(".MEFinder.migration-*")))

    def test_migration_refuses_nonempty_or_nested_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = base / "current" / "MEFinder"
            _create_current_data_root(current)

            nonempty = base / "cloud" / "MEFinder"
            nonempty.mkdir(parents=True)
            (nonempty / "existing.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(DataLocationError, "不是空的"):
                migrate_data_root(current, nonempty, current)
            self.assertEqual(
                (nonempty / "existing.txt").read_text(encoding="utf-8"),
                "keep",
            )

            nested = current / "external" / "MEFinder"
            with self.assertRaisesRegex(DataLocationError, "内部"):
                migrate_data_root(current, nested, current)

    def test_web_migration_blocks_writes_and_seals_old_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _app_data, _runtime, handler, server, server_thread = (
                _make_web_runtime(base)
            )
            target = base / "target" / "MEFinder"
            migration_started = threading.Event()
            release_migration = threading.Event()
            metadata_write_started = threading.Event()
            migration_response: list[tuple[int, dict[str, object]]] = []
            metadata_response: list[tuple[int, dict[str, object]]] = []
            sealed_response: list[tuple[int, dict[str, object]]] = []

            def blocked_migration(*_args, **_kwargs):
                migration_started.set()
                release_migration.wait(timeout=3)
                return {
                    "ok": True,
                    "target_path": str(target),
                    "restart_required": True,
                }

            def observed_metadata_write(*args, **kwargs):
                metadata_write_started.set()
                return update_metadata_in_database(*args, **kwargs)

            def migrate_request() -> None:
                migration_response.append(
                    _request_json(
                        server,
                        "POST",
                        "/api/data-location/migrate",
                        {"target_path": str(target)},
                    )
                )

            def metadata_request() -> None:
                metadata_response.append(
                    _request_json(
                        server,
                        "POST",
                        "/api/bibliographic-metadata/save",
                        {
                            "source_id": "pdf-one",
                            "metadata": {"title": "New"},
                        },
                    )
                )

            migrate_thread = threading.Thread(target=migrate_request)
            metadata_thread = threading.Thread(target=metadata_request)
            try:
                with patch(
                    "src.me_finder.web.migrate_data_root",
                    side_effect=blocked_migration,
                ), patch(
                    "src.me_finder.web.update_metadata_in_database",
                    side_effect=observed_metadata_write,
                ):
                    server_thread.start()
                    migrate_thread.start()
                    self.assertTrue(migration_started.wait(timeout=2))
                    self.assertFalse(
                        handler.wait_for_durable_operations(timeout=0.01)
                    )
                    metadata_thread.start()
                    metadata_thread.join(timeout=1)
                    self.assertFalse(metadata_thread.is_alive())
                    self.assertFalse(metadata_write_started.is_set())
                    self.assertEqual(metadata_response[0][0], 409)
                    self.assertIn(
                        "正在迁移",
                        str(metadata_response[0][1].get("error")),
                    )
                    release_migration.set()
                    migrate_thread.join(timeout=2)
                    sealed_response.append(
                        _request_json(
                            server,
                            "POST",
                            "/api/bibliographic-metadata/save",
                            {
                                "source_id": "pdf-one",
                                "metadata": {"title": "Still old"},
                            },
                        )
                    )
                    self.assertFalse(metadata_write_started.is_set())
            finally:
                release_migration.set()
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                server_thread.join(timeout=2)
                migrate_thread.join(timeout=2)
                metadata_thread.join(timeout=2)

            self.assertEqual(migration_response[0][0], 200)
            self.assertEqual(sealed_response[0][0], 409)
            self.assertIn(
                "请重启应用",
                str(sealed_response[0][1].get("error")),
            )

    def test_web_migration_rejects_active_job_inside_consistency_region(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _app_data, runtime, handler, server, server_thread = (
                _make_web_runtime(base)
            )
            target = base / "target" / "MEFinder"
            worker_release = threading.Event()
            both_workers_started = threading.Event()
            worker_count = 0
            worker_count_lock = threading.Lock()

            def occupy_worker() -> None:
                nonlocal worker_count
                with worker_count_lock:
                    worker_count += 1
                    if worker_count == 2:
                        both_workers_started.set()
                worker_release.wait(timeout=3)

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
            backup_path = base / "queued-backup.zip"
            backup_path.write_bytes(archive_buffer.getvalue())

            try:
                server_thread.start()
                handler._submit_background_task(occupy_worker)
                handler._submit_background_task(occupy_worker)
                self.assertTrue(both_workers_started.wait(timeout=2))
                status, queued = _request_json(
                    server,
                    "POST",
                    "/api/backup/import",
                    {"path": str(backup_path)},
                )
                self.assertEqual(status, 200)
                with patch("src.me_finder.web.migrate_data_root") as migrate:
                    status, response = _request_json(
                        server,
                        "POST",
                        "/api/data-location/migrate",
                        {"target_path": str(target)},
                    )
                    migrate.assert_not_called()
                self.assertEqual(status, 409)
                self.assertIn("文献正在导入", str(response.get("error")))
                job_id = str(queued.get("job_id", ""))
                self.assertTrue(job_id.startswith("restore-"))
                worker_release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    _status, job = _request_json(
                        server,
                        "GET",
                        f"/api/import-status?job_id={job_id}",
                    )
                    if job.get("status") in {"completed", "failed"}:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("queued backup job did not finish")
            finally:
                worker_release.set()
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                server_thread.join(timeout=2)

            self.assertTrue((runtime / "data" / "index.sqlite3").exists())

    def test_web_migration_rejects_active_upload_and_reopens_admission(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            _app_data, _runtime, handler, server, server_thread = (
                _make_web_runtime(base)
            )
            target = base / "target" / "MEFinder"
            try:
                server_thread.start()
                status, started = _request_json(
                    server,
                    "POST",
                    "/api/import-upload/start",
                    {
                        "file_name": "pending.pdf",
                        "size": 10,
                        "parse_mode": "auto",
                        "provider_id": "",
                    },
                )
                self.assertEqual(status, 200)

                with patch("src.me_finder.web.migrate_data_root") as migrate:
                    status, response = _request_json(
                        server,
                        "POST",
                        "/api/data-location/migrate",
                        {"target_path": str(target)},
                    )
                    migrate.assert_not_called()
                self.assertEqual(status, 409)
                self.assertIn("文件正在上传", str(response.get("error")))

                status, cancelled = _request_json(
                    server,
                    "POST",
                    "/api/import-upload/cancel",
                    {"upload_id": str(started["upload_id"])},
                )
                self.assertEqual(status, 200)
                self.assertTrue(cancelled["cancelled"])

                migration_result = {
                    "ok": True,
                    "target_path": str(target),
                    "restart_required": True,
                }
                with patch(
                    "src.me_finder.web.migrate_data_root",
                    return_value=migration_result,
                ) as migrate:
                    status, response = _request_json(
                        server,
                        "POST",
                        "/api/data-location/migrate",
                        {"target_path": str(target)},
                    )
                    migrate.assert_called_once()
                self.assertEqual(status, 200)
                self.assertEqual(response, migration_result)
            finally:
                server.shutdown()
                server.server_close()
                handler.close_runtime()
                server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
