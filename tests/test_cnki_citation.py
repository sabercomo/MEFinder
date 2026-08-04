from __future__ import annotations

import json
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from src.me_finder.cnki_citation import parse_cnki_journal_citation
from src.me_finder.database import build_database
from src.me_finder.web import make_handler


class CNKICitationParserTests(unittest.TestCase):
    def test_parses_cnki_gbt_journal_citation(self) -> None:
        metadata = parse_cnki_journal_citation(
            "[1]孙向晨.现代社会中的“家庭”：马克思与黑格尔的社会理论[J]."
            "学术月刊,2017,49(04):15-27."
        )

        self.assertEqual(
            metadata,
            {
                "document_type": "journal_article",
                "author": "孙向晨",
                "title": "现代社会中的“家庭”：马克思与黑格尔的社会理论",
                "journal_name": "学术月刊",
                "publish_year": "2017",
                "volume": "49",
                "issue": "04",
                "page_range": "15-27",
            },
        )

    def test_parses_header_issue_only_doi_and_compound_pages(self) -> None:
        metadata = parse_cnki_journal_citation(
            "GB/T 7714-2015\n"
            "张双利.重思马克思的市民社会理论[J].哲学研究,2020(09):"
            "66-81+125.DOI:10.1234/example"
        )

        self.assertEqual(metadata["journal_name"], "哲学研究")
        self.assertEqual(metadata["publish_year"], "2020")
        self.assertNotIn("volume", metadata)
        self.assertEqual(metadata["issue"], "09")
        self.assertEqual(metadata["page_range"], "66-81+125")
        self.assertEqual(metadata["doi"], "10.1234/example")

    def test_parses_cnki_issue_followed_directly_by_pages(self) -> None:
        metadata = parse_cnki_journal_citation(
            "[1]周爱民.论法兰克福学派批判理论面临的“根本挑战”——"
            "从社会批判的方法论视角看[J].社会科学,2024,(5)53-61."
            "DOI:10.13644/j.cnki.cn31-1112.2024.05.009."
        )

        self.assertEqual(metadata["author"], "周爱民")
        self.assertEqual(metadata["journal_name"], "社会科学")
        self.assertEqual(metadata["publish_year"], "2024")
        self.assertEqual(metadata["issue"], "5")
        self.assertEqual(metadata["page_range"], "53-61")
        self.assertEqual(metadata["doi"], "10.13644/j.cnki.cn31-1112.2024.05.009")

    def test_parses_full_width_marker_parentheses_and_journal_name(self) -> None:
        metadata = parse_cnki_journal_citation(
            "［1］王某．文章题目［Ｊ］．复旦学报(社会科学版)，2024，"
            "49（增刊）：1－6＋259－263。"
        )

        self.assertEqual(metadata["author"], "王某")
        self.assertEqual(metadata["journal_name"], "复旦学报(社会科学版)")
        self.assertEqual(metadata["volume"], "49")
        self.assertEqual(metadata["issue"], "增刊")
        self.assertEqual(metadata["page_range"], "1－6＋259－263")

    def test_rejects_non_journal_or_incomplete_text(self) -> None:
        for text in (
            "张三.一本书[M].北京:某出版社,2020.",
            "张三.一篇文章[J].",
            "",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_cnki_journal_citation(text)

        with self.assertRaises(ValueError):
            parse_cnki_journal_citation(None)

    def test_rejects_multiple_pages_of_pasted_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "只粘贴一条"):
            parse_cnki_journal_citation("期刊引文" * 3000)


class CNKICitationAPITests(unittest.TestCase):
    @contextmanager
    def _server(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            database = root / "data" / "index.sqlite3"
            database.parent.mkdir(parents=True)
            (root / "config").mkdir()
            build_database({"metadata": {}}, database)

            previous_cwd = Path.cwd()
            handler = None
            server = None
            try:
                os.chdir(root)
                handler = make_handler(database)
                handler.log_message = lambda *_args: None
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                yield f"http://127.0.0.1:{server.server_port}"
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if handler is not None:
                    handler.close_runtime()
                os.chdir(previous_cwd)

    @staticmethod
    def _post(base_url: str, payload: object) -> tuple[int, dict[str, object]]:
        request = Request(
            base_url + "/api/bibliographic-metadata/parse-cnki-citation",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_api_returns_parsed_metadata(self) -> None:
        with self._server() as base_url:
            status, payload = self._post(
                base_url,
                {
                    "citation_text": (
                        "张双利.重思马克思的市民社会理论[J]."
                        "哲学研究,2020(09):15-27."
                    )
                },
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["metadata"]["journal_name"], "哲学研究")

    def test_api_rejects_unknown_fields_invalid_text_and_large_body(self) -> None:
        with self._server() as base_url:
            unknown_status, _ = self._post(
                base_url,
                {"citation_text": "文本", "source_id": "pdf-test"},
            )
            invalid_status, invalid = self._post(
                base_url,
                {"citation_text": "这不是一条期刊引文"},
            )
            large_status, large = self._post(
                base_url,
                {"citation_text": "x" * (33 * 1024)},
            )

        self.assertEqual(unknown_status, 400)
        self.assertEqual(invalid_status, 400)
        self.assertIn("[J]", invalid["error"])
        self.assertEqual(large_status, 413)
        self.assertIn("只粘贴一条", large["error"])


if __name__ == "__main__":
    unittest.main()
