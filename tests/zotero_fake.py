"""A local fake of the Zotero 7/10 Local API for sync tests.

Runs a real ``http.server`` on 127.0.0.1 so the client is exercised end to end
(paging, Total-Results, versions, file URLs, error codes). Shapes follow
https://www.zotero.org/support/dev/web_api/v3/local_api and the Web API v3
basics; only the endpoints MEFinder reads are implemented.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional


class FakeZotero:
    def __init__(self, *, server_id: Optional[str] = None) -> None:
        self.server_id = server_id
        self.enabled = True
        self.collections: Dict[str, Dict] = {}
        self.items: Dict[str, Dict] = {}
        self.files: Dict[str, Path] = {}
        self.fail_paths: List[str] = []
        self.lie_total_by: int = 0
        self.hide_items_in: set[str] = set()
        self.requests: List[str] = []
        self._version = 1
        self._server: Optional[ThreadingHTTPServer] = None

    # ── fixture builders ──

    def _bump(self) -> int:
        self._version += 1
        return self._version

    def add_collection(self, key: str, name: str, parent: Optional[str] = None) -> None:
        self.collections[key] = {"key": key, "name": name, "parentCollection": parent or False}

    def add_item(self, key: str, title: str, collections: List[str], **fields) -> None:
        data = {"key": key, "itemType": fields.pop("itemType", "book"), "title": title, "collections": list(collections)}
        data.update(fields)
        self.items[key] = {"key": key, "version": self._bump(), "data": data}

    def add_attachment(self, key: str, parent: str, path: Path, *, content_type: str = "application/pdf", link_mode: str = "imported_file", md5: str = "m1") -> None:
        data = {
            "key": key, "itemType": "attachment", "parentItem": parent, "linkMode": link_mode,
            "contentType": content_type, "filename": path.name, "md5": md5, "mtime": 1,
        }
        self.items[key] = {"key": key, "version": self._bump(), "data": data}
        self.files[key] = path

    def update(self, key: str, **fields) -> None:
        self.items[key]["data"].update(fields)
        self.items[key]["version"] = self._bump()

    def delete(self, key: str) -> None:
        self.items.pop(key, None)
        self.files.pop(key, None)

    # ── server ──

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_GET(self):  # noqa: N802 - http.server hook
                fake.requests.append(self.path)
                status, headers, body = fake.handle(self.path)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/api"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ── routing ──

    def _collection_entry(self, key: str) -> Dict:
        data = dict(self.collections[key])
        count = sum(1 for item in self.items.values() if key in item["data"].get("collections", []))
        return {"key": key, "version": 1, "meta": {"numItems": count}, "data": data}

    def handle(self, raw_path: str):
        parsed = urllib.parse.urlsplit(raw_path)
        path = parsed.path
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        headers = {"Zotero-API-Version": "3", "X-Zotero-Version": "9.0.6"}
        if self.server_id:
            headers["Zotero-Server-ID"] = self.server_id
        if not self.enabled:
            return 403, headers, b"Local API is not enabled"
        if any(fragment in path for fragment in self.fail_paths):
            return 500, headers, b"boom"
        prefix = "/api/users/0/"
        if not path.startswith(prefix):
            return 404, headers, b"not found"
        rest = path[len(prefix):]
        parts = rest.split("/")
        if rest == "collections":
            return self._list([self._collection_entry(key) for key in self.collections], query, headers)
        if len(parts) == 4 and parts[0] == "collections" and parts[2:] == ["items", "top"]:
            key = parts[1]
            if key not in self.collections:
                return 404, headers, b"no collection"
            entries = [item for item in self.items.values() if key in item["data"].get("collections", [])]
            if key in self.hide_items_in:
                entries = []
            return self._list(entries, query, headers)
        if rest == "items":
            entries = list(self.items.values())
            if query.get("itemType") == "attachment":
                entries = [item for item in entries if item["data"]["itemType"] == "attachment"]
            if query.get("itemKey"):
                wanted = set(query["itemKey"].split(","))
                entries = [item for item in entries if item["key"] in wanted]
            return self._list(entries, query, headers)
        if len(parts) == 5 and parts[0] == "items" and parts[2:] == ["file", "view", "url"]:
            file_path = self.files.get(parts[1])
            if file_path is None:
                return 404, headers, b"Not found"
            return 200, {**headers, "Content-Type": "text/plain"}, file_path.resolve().as_uri().encode()
        return 404, headers, b"not found"

    def _list(self, entries: List[Dict], query: Dict[str, str], headers: Dict[str, str]):
        if query.get("format") == "versions":
            body = {entry["key"]: entry["version"] for entry in entries}
            return 200, {**headers, "Total-Results": str(len(body))}, json.dumps(body).encode()
        start = int(query.get("start", 0))
        limit = int(query.get("limit", 0)) or len(entries)
        page = entries[start:start + limit]
        headers = {**headers, "Total-Results": str(len(entries) + self.lie_total_by), "Content-Type": "application/json"}
        return 200, headers, json.dumps(page).encode()
