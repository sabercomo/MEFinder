from __future__ import annotations

import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from src.me_finder.database import build_database
from src.me_finder.pdf_import_service import (
    attach_mineru_manifest,
    load_import_config,
    register_pdf,
    save_import_config as real_save_import_config,
)
from src.me_finder.web import make_handler


class _ObservedRLock:
    """Expose when the manifest worker tries to enter the shared config lock."""

    def __init__(self, remote_attempted: threading.Event) -> None:
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self._remote_attempted = remote_attempted

    def __enter__(self):
        if threading.current_thread().name.startswith("manifest-attach"):
            self._remote_attempted.set()
        self._lock.acquire()
        ident = threading.get_ident()
        with self._state_lock:
            if self._owner == ident:
                self._depth += 1
            else:
                self._owner = ident
                self._depth = 1
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        ident = threading.get_ident()
        with self._state_lock:
            if self._owner != ident or self._depth < 1:
                raise RuntimeError("config lock released by a non-owner")
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
        self._lock.release()

    def owned_by_current_thread(self) -> bool:
        with self._state_lock:
            return (
                self._owner == threading.get_ident()
                and self._depth > 0
            )


class ImportConfigConcurrencyTests(unittest.TestCase):
    def test_metadata_save_does_not_erase_concurrent_parser_manifest(
        self,
    ) -> None:
        """Two documents updating pdf_imports.json must not lose either edit."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "app"
            raw_pdf_dir = root / "corpus" / "raw_pdf"
            raw_pdf_dir.mkdir(parents=True)
            (root / "data").mkdir(parents=True)

            native_pdf = raw_pdf_dir / "native.pdf"
            remote_pdf = raw_pdf_dir / "remote.pdf"
            native_pdf.write_bytes(b"%PDF native metadata concurrency fixture")
            remote_pdf.write_bytes(b"%PDF remote manifest concurrency fixture")
            native_document = register_pdf(root, native_pdf)
            remote_document = register_pdf(root, remote_pdf)
            native_source_id = str(native_document["source_file_id"])
            remote_source_id = str(remote_document["source_file_id"])

            build_database(
                {
                    "metadata": {},
                    "source_files": [
                        {
                            "source_file_id": native_source_id,
                            "source_type": "pdf",
                            "file_name": native_pdf.name,
                            "relative_path": (
                                f"corpus/raw_pdf/{native_pdf.name}"
                            ),
                        },
                        {
                            "source_file_id": remote_source_id,
                            "source_type": "pdf",
                            "file_name": remote_pdf.name,
                            "relative_path": (
                                f"corpus/raw_pdf/{remote_pdf.name}"
                            ),
                        },
                    ],
                },
                root / "data" / "index.sqlite3",
            )
            manifest_path = root / "data" / "remote-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "segments": [],
                        "total_pages": 0,
                        "completed_pages": [],
                        "failed_pages": [],
                    }
                ),
                encoding="utf-8",
            )

            metadata_snapshot_ready = threading.Event()
            remote_lock_attempted = threading.Event()
            remote_manifest_saved = threading.Event()
            intercepted = threading.Event()
            observed_config_lock = _ObservedRLock(remote_lock_attempted)

            def delay_stale_metadata_save(
                config_path: Path,
                config: dict[str, object],
            ) -> None:
                documents = [
                    item
                    for item in config.get("documents", [])
                    if isinstance(item, dict)
                ]
                native_record = next(
                    (
                        item
                        for item in documents
                        if item.get("source_file_id") == native_source_id
                    ),
                    {},
                )
                if (
                    native_record.get("bibliographic_metadata")
                    and not intercepted.is_set()
                ):
                    intercepted.set()
                    metadata_snapshot_ready.set()
                    if not remote_lock_attempted.wait(5):
                        raise TimeoutError(
                            "remote manifest did not attempt its config write"
                        )
                    # Before the fix, the metadata path releases the shared
                    # lock between load and save. Let the manifest finish
                    # first to deterministically expose the stale overwrite.
                    # With an atomic RMW lock, waiting for completion here
                    # would deadlock; the manifest is correctly blocked until
                    # the metadata transaction commits.
                    if (
                        not observed_config_lock.owned_by_current_thread()
                        and not remote_manifest_saved.wait(5)
                    ):
                        raise TimeoutError(
                            "unlocked remote manifest did not finish"
                        )
                real_save_import_config(config_path, config)

            def attach_remote_manifest() -> None:
                try:
                    attach_mineru_manifest(
                        root,
                        remote_source_id,
                        manifest_path,
                    )
                finally:
                    remote_manifest_saved.set()

            previous_cwd = Path.cwd()
            server = None
            handler = None
            opener = build_opener(ProxyHandler({}))
            with (
                patch(
                    "src.me_finder.pdf_import_service._IMPORT_CONFIG_LOCK",
                    observed_config_lock,
                ),
                patch(
                    "src.me_finder.web_runtime.save_import_config",
                    side_effect=delay_stale_metadata_save,
                ),
            ):
                try:
                    os.chdir(root)
                    handler = make_handler(root / "data" / "index.sqlite3")
                    handler.log_message = lambda *_args: None
                    server = ThreadingHTTPServer(
                        ("127.0.0.1", 0),
                        handler,
                    )
                    threading.Thread(
                        target=server.serve_forever,
                        daemon=True,
                    ).start()
                    url = (
                        f"http://127.0.0.1:{server.server_port}"
                        "/api/bibliographic-metadata/save"
                    )

                    with (
                        ThreadPoolExecutor(
                            max_workers=1,
                            thread_name_prefix="metadata-save",
                        ) as metadata_executor,
                        ThreadPoolExecutor(
                            max_workers=1,
                            thread_name_prefix="manifest-attach",
                        ) as manifest_executor,
                    ):
                        metadata_future = metadata_executor.submit(
                            self._post_json,
                            opener,
                            url,
                            {
                                "source_id": native_source_id,
                                "metadata": {
                                    "author": "本地作者",
                                    "title": "本地元数据标题",
                                    "publisher": "本地出版社",
                                    "publish_place": "北京",
                                    "publish_year": "2026",
                                    "document_type": "book",
                                },
                            },
                        )
                        self.assertTrue(
                            metadata_snapshot_ready.wait(5),
                            "metadata update never reached its config save",
                        )
                        manifest_future = manifest_executor.submit(
                            attach_remote_manifest
                        )
                        response = metadata_future.result(timeout=5)
                        manifest_future.result(timeout=5)

                    self.assertTrue(response["ok"])
                    config = load_import_config(
                        root / "config" / "pdf_imports.json"
                    )
                    by_source_id = {
                        str(item["source_file_id"]): item
                        for item in config["documents"]
                    }
                    self.assertEqual(
                        by_source_id[native_source_id][
                            "bibliographic_metadata"
                        ]["title"],
                        "本地元数据标题",
                    )
                    self.assertIn(
                        "mineru",
                        by_source_id[remote_source_id],
                        "a stale metadata snapshot erased the remote manifest",
                    )
                finally:
                    remote_manifest_saved.set()
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                    if handler is not None:
                        handler.close_runtime()
                    os.chdir(previous_cwd)

    @staticmethod
    def _post_json(
        opener,
        url: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
