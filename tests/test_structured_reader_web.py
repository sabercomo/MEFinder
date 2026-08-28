from __future__ import annotations

import json
import os
import sqlite3
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import src.me_finder.web as web
from src.me_finder.database import build_database
from src.me_finder.structured_reader_controller import StructuredReaderController


def _reader_index() -> dict[str, object]:
    return {
        "metadata": {"anchor_spec_version": 1},
        "source_files": [
            {
                "source_file_id": "pdf-http",
                "source_type": "pdf",
                "file_name": "reader.pdf",
                "display_title": "PDF 阅读样例",
                "bibliographic_metadata": {
                    "document_type": "book",
                    "title": "PDF 阅读样例",
                    "author": "测试作者",
                    "publish_place": "北京",
                    "publisher": "测试出版社",
                    "publish_year": "2026",
                },
            },
            {
                "source_file_id": "word-http",
                "source_type": "word",
                "file_name": "reader.docx",
                "display_title": "Word 阅读样例",
                "bibliographic_metadata": {
                    "document_type": "book",
                    "title": "Word 阅读样例",
                    "author": "测试作者",
                    "publish_place": "北京",
                    "publisher": "测试出版社",
                    "publish_year": "2026",
                },
            },
            {
                "source_file_id": "html-http",
                "source_type": "html",
                "file_name": "reader.html",
            },
        ],
        "volumes": [],
        "works": [],
        "paragraphs": [
            {
                "paragraph_id": "word-http-P000002",
                "source_file_id": "word-http",
                "source_type": "word",
                "paragraph_index": 2,
                "work_id": "word-http-W0001",
                "eligible_for_search": True,
                "text_raw": "Word 第二段",
                "page_source_type": "section_break_inferred",
                "page_display": "38",
                "original_page_start": "38",
            },
            {
                "paragraph_id": "word-http-P000005",
                "source_file_id": "word-http",
                "source_type": "word",
                "paragraph_index": 5,
                "work_id": "word-http-W0001",
                "eligible_for_search": True,
                "text_raw": "Word 第五段",
                "page_source_type": "section_break_inferred",
                "page_display": "39",
                "original_page_start": "39",
            },
        ],
        "pdf_pages": [
            {
                "pdf_page_id": "pdf-http-PAGE-000000",
                "source_file_id": "pdf-http",
                "pdf_page_index": 0,
                "text_raw": "PDF 第零页",
                "citation_page": "1",
                "page_mapping_method": "manual_segment",
                "segment_id": "MAPSEG-000000-000003",
            },
            {
                "pdf_page_id": "pdf-http-PAGE-000001",
                "source_file_id": "pdf-http",
                "pdf_page_index": 1,
                "text_raw": "PDF 第一页",
                "citation_page": "2",
                "page_mapping_method": "manual_segment",
                "segment_id": "MAPSEG-000000-000003",
            },
            {
                "pdf_page_id": "pdf-http-PAGE-000002",
                "source_file_id": "pdf-http",
                "pdf_page_index": 2,
                "text_raw": "PDF 第二页",
                "citation_page": "3",
                "page_mapping_method": "manual_segment",
                "segment_id": "MAPSEG-000000-000003",
            },
            {
                "pdf_page_id": "pdf-http-PAGE-000003",
                "source_file_id": "pdf-http",
                "pdf_page_index": 3,
                "text_raw": "PDF 第三页",
                "citation_page": "4",
                "page_mapping_method": "manual_segment",
                "segment_id": "MAPSEG-000000-000003",
            },
        ],
    }


class StructuredReaderWebTests(unittest.TestCase):
    @contextmanager
    def _server(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            database_path = root / "data" / "index.sqlite3"
            database_path.parent.mkdir(parents=True)
            (root / "config").mkdir()
            build_database(_reader_index(), database_path)

            previous_cwd = Path.cwd()
            handler = None
            server = None
            try:
                os.chdir(root)
                handler = web.make_handler(database_path)
                handler.log_message = lambda *_args: None
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                thread = threading.Thread(
                    target=server.serve_forever,
                    daemon=True,
                )
                thread.start()
                yield (
                    f"http://127.0.0.1:{server.server_port}",
                    handler,
                )
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
                os.chdir(previous_cwd)

    @staticmethod
    def _get_json(base_url: str, path: str) -> tuple[int, dict[str, object]]:
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(base_url + path, timeout=5) as response:
                return (
                    response.status,
                    json.loads(response.read().decode("utf-8")),
                )
        except HTTPError as exc:
            return (
                exc.code,
                json.loads(exc.read().decode("utf-8")),
            )

    @staticmethod
    def _post_json(
        base_url: str,
        path: str,
        payload: object,
    ) -> tuple[int, dict[str, object]]:
        opener = build_opener(ProxyHandler({}))
        request = Request(
            base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=5) as response:
                return (
                    response.status,
                    json.loads(response.read().decode("utf-8")),
                )
        except HTTPError as exc:
            return (
                exc.code,
                json.loads(exc.read().decode("utf-8")),
            )

    def test_pdf_and_word_windows_are_served_over_real_http(self) -> None:
        with self._server() as (base_url, _handler):
            pdf_status, pdf = self._get_json(
                base_url,
                "/api/document/pages"
                "?source_id=pdf-http&start=3&count=1",
            )
            word_status, word = self._get_json(
                base_url,
                "/api/document/pages"
                "?source_id=word-http&start=3&count=1",
            )

        self.assertEqual(pdf_status, 200)
        self.assertEqual(pdf["start"], 3)
        self.assertEqual(pdf["total"], 4)
        self.assertEqual(pdf["last_position"], 3)
        self.assertEqual(pdf["previous_start"], 2)
        self.assertEqual(pdf["next_start"], 4)
        self.assertEqual(
            pdf["items"][0]["pdf_page_id"],
            "pdf-http-PAGE-000003",
        )
        self.assertTrue(pdf["items"][0]["citation_formats"]["can_copy"])
        self.assertTrue(pdf["items"][0]["citation_formats"]["page_verified"])
        self.assertEqual(word_status, 200)
        self.assertEqual(word["start"], 3)
        self.assertEqual(word["last_position"], 5)
        self.assertEqual(word["previous_start"], 2)
        self.assertEqual(word["next_start"], 6)
        self.assertEqual(
            word["items"][0]["paragraph_id"],
            "word-http-P000005",
        )
        self.assertFalse(word["items"][0]["citation_formats"]["can_copy"])
        self.assertFalse(word["items"][0]["citation_formats"]["page_verified"])

    def test_cross_page_citation_api_and_unverified_word_rejection(self) -> None:
        with self._server() as (base_url, _handler):
            pdf_status, pdf = self._post_json(
                base_url,
                "/api/document/citation",
                {
                    "source_id": "pdf-http",
                    "start_anchor_id": "pdf-http-PAGE-000000",
                    "end_anchor_id": "pdf-http-PAGE-000003",
                },
            )
            word_status, word = self._post_json(
                base_url,
                "/api/document/citation",
                {
                    "source_id": "word-http",
                    "start_anchor_id": "word-http-P000002",
                    "end_anchor_id": "word-http-P000005",
                },
            )

        self.assertEqual(pdf_status, 200)
        self.assertTrue(pdf["page_range"]["verified"])
        self.assertEqual(pdf["page_range"]["citation_page_start"], "1")
        self.assertEqual(pdf["page_range"]["citation_page_end"], "4")
        self.assertTrue(pdf["citation_formats"]["can_copy"])
        self.assertIn("第1—4页", pdf["citation_formats"]["chinese"])
        self.assertNotIn("text_raw", pdf)
        self.assertNotIn("relative_path", pdf["source"])

        self.assertEqual(word_status, 200)
        self.assertFalse(word["page_range"]["verified"])
        self.assertFalse(word["citation_formats"]["can_copy"])
        self.assertEqual(
            word["citation_formats"]["gb"],
            "该文献页码尚未校准，不能生成 GB/T 引文。",
        )

    def test_citation_api_rejects_ambiguous_or_invalid_payloads(self) -> None:
        cases = [
            (
                {
                    "source_id": "pdf-http",
                    "start_anchor_id": "../bad-anchor",
                    "end_anchor_id": "pdf-http-PAGE-000000",
                },
                400,
                "持久化文献锚点",
            ),
            (
                {
                    "source_id": "pdf-http",
                    "start_anchor_id": "pdf-http-PAGE-000000",
                    "end_anchor_id": "pdf-http-PAGE-000099",
                },
                404,
                "不存在",
            ),
            (
                {
                    "source_id": "pdf-http",
                    "start_anchor_id": "pdf-http-PAGE-000000",
                    "end_anchor_id": "pdf-http-PAGE-000003",
                    "relative_path": "/private/document.pdf",
                },
                400,
                "不支持",
            ),
            (
                ["pdf-http", 0, 3],
                400,
                "JSON 对象",
            ),
        ]
        with self._server() as (base_url, _handler):
            for payload, expected_status, message in cases:
                with self.subTest(payload=payload):
                    status, result = self._post_json(
                        base_url,
                        "/api/document/citation",
                        payload,
                    )
                    self.assertEqual(status, expected_status)
                    self.assertIn(message, result["error"])

    def test_reader_deep_link_get_and_head_return_application_html(self) -> None:
        opener = build_opener(ProxyHandler({}))
        with self._server() as (base_url, _handler):
            with opener.open(
                base_url + "/reader?source=pdf-http&page=3",
                timeout=5,
            ) as response:
                get_status = response.status
                content_type = response.headers.get("Content-Type", "")
                html = response.read().decode("utf-8")
            head_request = Request(
                base_url + "/reader?source=pdf-http&page=3",
                method="HEAD",
            )
            with opener.open(head_request, timeout=5) as response:
                head_status = response.status
                head_length = int(response.headers["Content-Length"])
                head_body = response.read()
            with opener.open(
                base_url + "/reader/?source=source-01&page=MEWJ-01-P000001",
                timeout=5,
            ) as response:
                trailing_slash_status = response.status

        self.assertEqual(get_status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("<!doctype html>", html.lower())
        self.assertEqual(head_status, 200)
        self.assertGreater(head_length, 0)
        self.assertEqual(head_body, b"")
        self.assertEqual(trailing_slash_status, 200)

    def test_invalid_missing_and_unsupported_requests_have_clear_statuses(
        self,
    ) -> None:
        cases = [
            ("/api/document/pages", 400, "source_id"),
            (
                "/api/document/pages?source_id=pdf-http&count=0",
                400,
                "count",
            ),
            (
                "/api/document/pages"
                "?source_id=pdf-http&start=0&start=1",
                400,
                "一次",
            ),
            (
                "/api/document/pages?source_id=html-http",
                400,
                "暂不支持",
            ),
            (
                "/api/document/pages?source_id=missing-http",
                404,
                "未找到",
            ),
        ]
        with self._server() as (base_url, _handler):
            for path, expected_status, message in cases:
                with self.subTest(path=path):
                    status, payload = self._get_json(base_url, path)
                    self.assertEqual(status, expected_status)
                    self.assertIn(message, payload["error"])

    def test_rebuilding_and_database_failures_do_not_escape_as_tracebacks(
        self,
    ) -> None:
        with self._server() as (base_url, handler):
            handler.index_runtime.suspend()
            rebuilding_status, rebuilding = self._get_json(
                base_url,
                "/api/document/pages?source_id=pdf-http",
            )
            citation_rebuilding_status, citation_rebuilding = self._post_json(
                base_url,
                "/api/document/citation",
                {
                    "source_id": "pdf-http",
                    "start_anchor_id": "pdf-http-PAGE-000000",
                    "end_anchor_id": "pdf-http-PAGE-000003",
                },
            )
            handler.index_runtime.reopen()

            with patch(
                "src.me_finder.web_runtime.get_document_window",
                side_effect=sqlite3.DatabaseError("database is locked"),
            ), patch("src.me_finder.web_runtime.logging.exception") as logged:
                failed_status, failed = self._get_json(
                    base_url,
                    "/api/document/pages?source_id=pdf-http",
                )
            with patch(
                "src.me_finder.web_runtime.get_document_citation",
                side_effect=sqlite3.DatabaseError(
                    "/private/library/index.sqlite3 is locked"
                ),
            ), patch("src.me_finder.web_runtime.logging.exception") as citation_logged:
                citation_failed_status, citation_failed = self._post_json(
                    base_url,
                    "/api/document/citation",
                    {
                        "source_id": "pdf-http",
                        "start_anchor_id": "pdf-http-PAGE-000000",
                        "end_anchor_id": "pdf-http-PAGE-000003",
                    },
                )

        self.assertEqual(rebuilding_status, 503)
        self.assertIn("正在重建", rebuilding["error"])
        self.assertEqual(citation_rebuilding_status, 503)
        self.assertIn("正在重建", citation_rebuilding["error"])
        self.assertEqual(failed_status, 500)
        self.assertIn("读取失败", failed["error"])
        self.assertNotIn("database is locked", failed["error"])
        logged.assert_called_once()
        self.assertEqual(citation_failed_status, 500)
        self.assertIn("生成失败", citation_failed["error"])
        self.assertNotIn("/private/", citation_failed["error"])
        citation_logged.assert_called_once()

    def test_route_table_and_embedded_reader_assets_are_wired(self) -> None:
        with self._server() as (_base_url, handler):
            self.assertIsInstance(
                handler.structured_reader_controller,
                StructuredReaderController,
            )

        package_dir = Path(web.__file__).resolve().parent
        reader_css = (package_dir / "static" / "reader.css").read_text(
            encoding="utf-8"
        )
        reader_js = (package_dir / "static" / "reader.js").read_text(
            encoding="utf-8"
        )
        self.assertIn(reader_css, web.HTML)
        self.assertIn(reader_js, web.HTML)
        self.assertNotIn("/*__READER_CSS__*/", web.HTML)
        self.assertNotIn("//__READER_JS__", web.HTML)


if __name__ == "__main__":
    unittest.main()
