from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.me_finder.database import build_database
from src.me_finder.web import make_handler


class BackupFilePickerTests(unittest.TestCase):
    def test_picker_returns_a_selected_zip(self) -> None:
        selected: dict[str, Path] = {}
        with self._server(lambda: selected["path"]) as (base_url, root):
            selected["path"] = root / "MEFinder-backup.zip"
            selected["path"].write_bytes(b"PK")

            response = self._post_json(
                base_url + "/api/backup/import/choose",
                {},
            )

        self.assertEqual(
            response,
            {
                "ok": True,
                "cancelled": False,
                "path": str(selected["path"]),
                "name": selected["path"].name,
            },
        )

    def test_cancel_is_not_an_error(self) -> None:
        with self._server(lambda: None) as (base_url, _root):
            response = self._post_json(
                base_url + "/api/backup/import/choose",
                {},
            )

        self.assertEqual(response, {"ok": True, "cancelled": True})

    def test_missing_or_non_zip_selection_is_rejected(self) -> None:
        with self._server(lambda: "/missing/backup.zip") as (base_url, _root):
            with self.assertRaises(HTTPError) as missing:
                self._post_json(base_url + "/api/backup/import/choose", {})
            self.assertEqual(missing.exception.code, 400)
            self.assertEqual(
                json.loads(missing.exception.read().decode("utf-8")),
                {"error": "所选路径不是文件。"},
            )

        selected: dict[str, Path] = {}
        with self._server(lambda: selected["path"]) as (base_url, root):
            selected["path"] = root / "backup.txt"
            selected["path"].write_text("no", encoding="utf-8")
            with self.assertRaises(HTTPError) as wrong_type:
                self._post_json(base_url + "/api/backup/import/choose", {})
            self.assertEqual(wrong_type.exception.code, 400)
            self.assertEqual(
                json.loads(wrong_type.exception.read().decode("utf-8")),
                {"error": "请选择 .zip 备份文件。"},
            )

    def test_unavailable_and_native_failures_have_stable_json(self) -> None:
        with self._server(None) as (base_url, _root):
            with self.assertRaises(HTTPError) as unavailable:
                self._post_json(base_url + "/api/backup/import/choose", {})
            self.assertEqual(unavailable.exception.code, 400)

        class NativeDialogError(RuntimeError):
            pass

        def fail() -> str:
            raise NativeDialogError("窗口尚未就绪")

        with self._server(fail) as (base_url, _root):
            with self.assertRaises(HTTPError) as failed:
                self._post_json(base_url + "/api/backup/import/choose", {})
            self.assertEqual(failed.exception.code, 500)
            self.assertEqual(
                json.loads(failed.exception.read().decode("utf-8")),
                {"error": "打开备份文件选择器失败：窗口尚未就绪"},
            )

    class _Server:
        def __init__(self, chooser) -> None:
            self._chooser = chooser
            self._temp = TemporaryDirectory()
            self._server = None
            self._handler = None
            self._previous_cwd = Path.cwd()

        def __enter__(self):
            root = Path(self._temp.name) / "app"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            os.chdir(root)
            handler = make_handler(
                root / "data" / "index.sqlite3",
                native_backup_file_chooser=self._chooser,
            )
            handler.log_message = lambda *_args: None
            self._handler = handler
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
            ).start()
            return f"http://127.0.0.1:{self._server.server_port}", root

        def __exit__(self, *_exc: object) -> None:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
            if self._handler is not None:
                self._handler.close_runtime()
            os.chdir(self._previous_cwd)
            self._temp.cleanup()

    def _server(self, chooser):
        return self._Server(chooser)

    @staticmethod
    def _post_json(url: str, payload: object) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
