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


class ScanDirectoryPickerTests(unittest.TestCase):
    """The import page relies on the shell's folder picker instead of a typed path."""

    def test_chosen_folder_is_returned_to_the_page(self) -> None:
        picked = {}

        with self._server(lambda: str(picked["folder"])) as (base_url, root):
            picked["folder"] = root / "文献库"
            picked["folder"].mkdir()
            response = self._post_json(
                base_url + "/api/scan-directories/choose", {}
            )

        self.assertTrue(response["ok"])
        self.assertFalse(response["cancelled"])
        self.assertEqual(response["folder"], str(picked["folder"]))

    def test_cancelled_picker_is_not_an_error(self) -> None:
        with self._server(lambda: None) as (base_url, _root):
            response = self._post_json(
                base_url + "/api/scan-directories/choose", {}
            )

        self.assertTrue(response["ok"])
        self.assertTrue(response["cancelled"])
        self.assertNotIn("folder", response)

    def test_missing_folder_is_rejected(self) -> None:
        with self._server(lambda: "/definitely/not/a/real/folder") as (
            base_url,
            _root,
        ):
            with self.assertRaises(HTTPError) as caught:
                self._post_json(base_url + "/api/scan-directories/choose", {})
            payload = json.loads(caught.exception.read().decode("utf-8"))

        self.assertEqual(caught.exception.code, 400)
        self.assertIn("文件夹", payload["error"])

    def test_picker_failure_is_reported_instead_of_crashing(self) -> None:
        def explode() -> str:
            raise RuntimeError("窗口尚未就绪")

        with self._server(explode) as (base_url, _root):
            with self.assertRaises(HTTPError) as caught:
                self._post_json(base_url + "/api/scan-directories/choose", {})
            payload = json.loads(caught.exception.read().decode("utf-8"))

        self.assertEqual(caught.exception.code, 400)
        self.assertIn("窗口尚未就绪", payload["error"])

    def test_browser_session_without_a_shell_gets_a_clear_error(self) -> None:
        with self._server(None) as (base_url, _root):
            with self.assertRaises(HTTPError) as caught:
                self._post_json(base_url + "/api/scan-directories/choose", {})
            payload = json.loads(caught.exception.read().decode("utf-8"))

        self.assertEqual(caught.exception.code, 400)
        self.assertIn("不支持", payload["error"])

    class _Server:
        def __init__(self, chooser) -> None:
            self._chooser = chooser
            self._temp = TemporaryDirectory()
            self._server = None
            self._previous_cwd = Path.cwd()

        def __enter__(self):
            root = Path(self._temp.name) / "app"
            (root / "data").mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            build_database({"metadata": {}}, root / "data" / "index.sqlite3")
            os.chdir(root)
            handler = make_handler(
                root / "data" / "index.sqlite3",
                native_scan_directory_chooser=self._chooser,
            )
            handler.log_message = lambda *_args: None
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
            return f"http://127.0.0.1:{self._server.server_port}", root

        def __exit__(self, *_exc) -> None:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
            os.chdir(self._previous_cwd)
            self._temp.cleanup()

    def _server(self, chooser):
        return self._Server(chooser)

    @staticmethod
    def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
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
