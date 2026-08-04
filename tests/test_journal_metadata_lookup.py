from __future__ import annotations

import json
import os
import ssl
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from src.me_finder.database import build_database
from src.me_finder.journal_metadata_lookup import (
    CNKIClient,
    CNKILookupError,
    parse_cnki_detail_page,
    parse_cnki_search_results,
)
from src.me_finder.web import make_handler, open_external_cnki_url


FIXTURES = Path(__file__).parent / "fixtures"


class CNKILookupParserTests(unittest.TestCase):
    def test_search_parser_removes_injected_text_and_filters_non_journals(self) -> None:
        candidates = parse_cnki_search_results(
            (FIXTURES / "cnki_search_results.html").read_text(encoding="utf-8"),
            {"title": "重思马克思的市民社会理论", "author": "张双利", "publish_year": "2020"},
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["metadata"]["title"], "重思马克思的市民社会理论")
        self.assertEqual(candidate["metadata"]["journal_name"], "学术月刊")
        self.assertEqual(candidate["match"]["level"], "high")
        self.assertEqual(candidate["match"]["score"], 1.0)
        self.assertTrue(candidate["record_url"].startswith("https://oversea.cnki.net/"))

    def test_search_parser_reports_verification_and_site_change(self) -> None:
        with self.assertRaisesRegex(CNKILookupError, "浏览器验证") as caught:
            parse_cnki_search_results('<div class="verifycode"></div>', {"title": "测试"})
        self.assertEqual(caught.exception.code, "verification_required")
        with self.assertRaises(CNKILookupError) as changed:
            parse_cnki_search_results("<html><body>new layout</body></html>", {"title": "测试"})
        self.assertEqual(changed.exception.code, "site_changed")
        self.assertEqual(parse_cnki_search_results('<p class="no-content">暂无数据</p>', {"title": "测试"}), [])

    def test_detail_parser_returns_complete_public_metadata_and_evidence(self) -> None:
        url = "https://oversea.cnki.net/kcms2/article/abstract?v=opaque"
        metadata, evidence = parse_cnki_detail_page(
            (FIXTURES / "cnki_detail_page.html").read_text(encoding="utf-8"),
            url,
        )

        self.assertEqual(
            metadata,
            {
                "document_type": "journal_article",
                "title": "重思马克思的市民社会理论",
                "author": "张双利",
                "journal_name": "学术月刊",
                "page_range": "15-27",
                "publish_year": "2020",
                "volume": "52",
                "issue": "09",
                "doi": "10.19862/j.cnki.xsyk.000034",
            },
        )
        self.assertEqual(evidence["journal_name"]["record_url"], url)
        self.assertEqual(evidence["doi"]["source"], "cnki_lookup")

    def test_detail_parser_joins_multiple_authors_and_drops_affiliation_marks(self) -> None:
        html = (
            '<div class="top-tip"><span><a>社会科学 . </a><a>2024 ,(05)</a></span></div>'
            '<h1><p class="title-one">论批判理论</p></h1>'
            '<h3 class="author" id="authorpart">'
            '<span><a>张能</a><sup>1</sup></span>'
            '<span><a>钟雯</a><sup>1,2</sup></span>'
            '<span class="author-tip">*</span>'
            '</h3>'
        )
        metadata, _ = parse_cnki_detail_page(
            html, "https://oversea.cnki.net/kcms2/article/abstract?v=x"
        )
        self.assertEqual(metadata["author"], "张能、钟雯")

    def test_candidate_url_is_strictly_allowlisted(self) -> None:
        client = CNKIClient(opener=object())
        for url in (
            "http://oversea.cnki.net/kcms2/article/abstract?v=x",
            "https://evil.example/kcms2/article/abstract?v=x",
            "https://oversea.cnki.net.evil.example/kcms2/article/abstract?v=x",
            "https://oversea.cnki.net/other/path?v=x",
        ):
            with self.subTest(url=url), self.assertRaises(CNKILookupError) as caught:
                client.fetch_candidate({"record_url": url})
            self.assertEqual(caught.exception.code, "invalid_candidate")

    def test_tls_failure_is_not_bypassed(self) -> None:
        class BrokenOpener:
            def open(self, *_args, **_kwargs):
                raise URLError(ssl.SSLCertVerificationError("certificate mismatch"))

        with self.assertRaises(CNKILookupError) as caught:
            CNKIClient(opener=BrokenOpener()).search({"title": "测试文章"})
        self.assertEqual(caught.exception.code, "tls_error")

    def test_search_uses_browser_transport_when_provided(self) -> None:
        fixture = (FIXTURES / "cnki_search_results.html").read_text(encoding="utf-8")
        calls: list[tuple[str, str, dict]] = []

        def transport(url, method, data, headers):
            calls.append((url, method, headers))
            return {"status": 200, "text": fixture}

        result = CNKIClient(transport=transport).search(
            {"title": "重思马克思的市民社会理论", "author": "张双利"}
        )
        self.assertTrue(result["candidates"])
        # The request goes through the browser session, targets the domestic
        # host, and drops the forbidden UA/Origin/Referer headers.
        self.assertEqual(calls[0][1], "POST")
        self.assertIn("kns.cnki.net/kns8s/brief/grid", calls[0][0])
        self.assertNotIn("Origin", calls[0][2])
        self.assertNotIn("User-Agent", calls[0][2])

    def test_transport_403_maps_to_verification_required(self) -> None:
        def transport(url, method, data, headers):
            return {"status": 403, "text": ""}

        with self.assertRaises(CNKILookupError) as caught:
            CNKIClient(transport=transport).search({"title": "测试文章"})
        self.assertEqual(caught.exception.code, "verification_required")

    def test_rate_limit_is_explicit_and_not_retried(self) -> None:
        class LimitedOpener:
            def __init__(self):
                self.calls = 0

            def open(self, request, **_kwargs):
                self.calls += 1
                raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

        opener = LimitedOpener()
        with self.assertRaises(CNKILookupError) as caught:
            CNKIClient(opener=opener).search({"title": "测试文章"})
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(opener.calls, 1)

    def test_unrelated_doi_default_list_falls_back_to_title(self) -> None:
        class Headers(dict):
            def get_content_charset(self):
                return "utf-8"

        class Response:
            def __init__(self, body: str):
                self.body = body.encode("utf-8")
                self.headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return self.body

        class SequenceOpener:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []

            def open(self, request, **_kwargs):
                self.calls.append(request.data.decode("utf-8"))
                return Response(self.responses.pop(0))

        fixture = (FIXTURES / "cnki_search_results.html").read_text(encoding="utf-8")
        unrelated = (
            fixture.replace("重思马克思的市民社会理论", "原美：关于美的内涵的理本论探索")
            .replace("张双利", "杨立华")
            .replace("2020-09-20", "2026-06-25")
        )
        opener = SequenceOpener([unrelated, fixture])
        result = CNKIClient(opener=opener).search(
            {
                "doi": "10.13644/j.cnki.cn31-1112.2024.05.009",
                "title": "重思马克思的市民社会理论",
                "author": "张双利",
                "publish_year": "2020",
            }
        )

        self.assertEqual(result["query_type"], "title_fallback")
        self.assertIn("已自动改用篇名", result["query_notice"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["metadata"]["title"], "重思马克思的市民社会理论")
        self.assertEqual(result["candidates"][0]["match"]["level"], "high")
        self.assertEqual(len(opener.calls), 2)
        self.assertIn("DOI", opener.calls[0])
        self.assertIn("%22Field%22%3A%22TI%22", opener.calls[1])

    def test_one_transient_failure_is_retried_once(self) -> None:
        class Headers(dict):
            def get_content_charset(self):
                return "utf-8"

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return '<p class="no-content">暂无数据</p>'.encode("utf-8")

        class TransientOpener:
            def __init__(self):
                self.calls = 0

            def open(self, request, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise HTTPError(request.full_url, 503, "Unavailable", {}, None)
                return Response()

        opener = TransientOpener()
        result = CNKIClient(opener=opener).search({"title": "测试文章"})
        self.assertEqual(result["candidates"], [])
        # kns (primary): one 503 retried once, then an empty "no rows" page;
        # since it yielded nothing, the oversea mirror is consulted once more.
        self.assertEqual(opener.calls, 3)

    def test_external_open_url_is_strictly_allowlisted(self) -> None:
        valid = "https://oversea.cnki.net/kns8s/search?kw=%E6%B5%8B%E8%AF%95"
        if os.name == "nt":
            with patch("src.me_finder.web.os.startfile") as startfile:
                open_external_cnki_url(valid)
            startfile.assert_called_once_with(valid)
        else:
            with patch("src.me_finder.web.subprocess.Popen") as popen:
                open_external_cnki_url(valid)
            popen.assert_called_once()
        for url in (
            "http://oversea.cnki.net/kns8s/search?kw=x",
            "https://user@oversea.cnki.net/kns8s/search?kw=x",
            "https://oversea.cnki.net:444/kns8s/search?kw=x",
            "https://evil.example/kns8s/search?kw=x",
            "https://oversea.cnki.net/other/path?kw=x",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                open_external_cnki_url(url)


class CNKILookupAPITests(unittest.TestCase):
    @contextmanager
    def _server(self, *, native_cnki_session=None):
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
                handler = make_handler(database, native_cnki_session=native_cnki_session)
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
    def _post(base_url: str, path: str, payload: object) -> tuple[int, dict[str, object]]:
        request = Request(
            base_url + path,
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

    def test_lookup_and_candidate_endpoints_return_structured_data(self) -> None:
        lookup_result = {
            "provider": "cnki",
            "query_type": "title",
            "open_url": "https://oversea.cnki.net/kns8s/search?kw=x",
            "candidates": [{"record_url": "https://oversea.cnki.net/kcms2/article/abstract?v=x"}],
        }
        detail_result = {
            "provider": "cnki",
            "record_url": "https://oversea.cnki.net/kcms2/article/abstract?v=x",
            "metadata": {"journal_name": "学术月刊"},
            "evidence": {},
        }
        with self._server() as base_url, patch(
            "src.me_finder.web.lookup_cnki_journal", return_value=lookup_result
        ), patch("src.me_finder.web.fetch_cnki_candidate", return_value=detail_result):
            lookup_status, lookup = self._post(
                base_url,
                "/api/bibliographic-metadata/lookup-cnki",
                {"metadata": {"title": "重思马克思的市民社会理论", "author": "张双利"}},
            )
            detail_status, detail = self._post(
                base_url,
                "/api/bibliographic-metadata/cnki-candidate",
                {"candidate": {"record_url": detail_result["record_url"]}},
            )

        self.assertEqual(lookup_status, 200)
        self.assertTrue(lookup["ok"])
        self.assertEqual(lookup["provider"], "cnki")
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["metadata"]["journal_name"], "学术月刊")

    def test_lookup_escalates_to_in_app_session_when_headless_empty(self) -> None:
        class FakeBridge:
            def __init__(self) -> None:
                self.ensure_calls: list[str] = []

            def is_ready(self) -> bool:
                return True

            def ensure_ready(self, open_url: str) -> bool:
                self.ensure_calls.append(open_url)
                return True

            def fetch(self, url, method, data, headers):  # pragma: no cover - patched
                return {"status": 200, "text": ""}

        def fake_lookup(metadata, *, opener=None, transport=None):
            base = {
                "provider": "cnki",
                "query_type": "title",
                "open_url": "https://kns.cnki.net/kns8s/search?kw=x",
            }
            if transport is None:
                # Headless attempt hits CNKI's captcha wall: no rows.
                return {**base, "candidates": []}
            # Issued from inside the authenticated window: the record is found.
            return {
                **base,
                "candidates": [
                    {
                        "record_url": "https://kns.cnki.net/kcms2/article/abstract?v=x",
                        "metadata": {"title": "命中"},
                        "match": {"level": "high", "score": 1.0},
                    }
                ],
            }

        bridge = FakeBridge()
        with self._server(native_cnki_session=bridge) as base_url, patch(
            "src.me_finder.web.lookup_cnki_journal", side_effect=fake_lookup
        ):
            status, payload = self._post(
                base_url,
                "/api/bibliographic-metadata/lookup-cnki",
                {"metadata": {"title": "命中"}},
            )

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertEqual(len(bridge.ensure_calls), 1)
        self.assertTrue(bridge.ensure_calls[0].startswith("https://kns.cnki.net/kns8s/search"))

    def test_lookup_api_preserves_explicit_error_code_and_fallback_url(self) -> None:
        failure = CNKILookupError(
            "verification_required",
            "知网要求浏览器验证。",
            open_url="https://oversea.cnki.net/kns8s/search?kw=x",
        )
        with self._server() as base_url, patch(
            "src.me_finder.web.lookup_cnki_journal", side_effect=failure
        ):
            status, payload = self._post(
                base_url,
                "/api/bibliographic-metadata/lookup-cnki",
                {"metadata": {"title": "测试文章"}},
            )

        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "verification_required")
        self.assertTrue(payload["open_url"].startswith("https://oversea.cnki.net/"))

    def test_lookup_api_rejects_unknown_fields_and_invalid_candidate_shape(self) -> None:
        with self._server() as base_url:
            lookup_status, _ = self._post(
                base_url,
                "/api/bibliographic-metadata/lookup-cnki",
                {"metadata": {"title": "测试", "abstract": "not allowed"}},
            )
            candidate_status, _ = self._post(
                base_url,
                "/api/bibliographic-metadata/cnki-candidate",
                {"candidate": {"record_url": "https://oversea.cnki.net/", "extra": True}},
            )

        self.assertEqual(lookup_status, 400)
        self.assertEqual(candidate_status, 400)

    def test_lookup_api_allows_only_one_inflight_request(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow_lookup(_metadata):
            entered.set()
            release.wait(timeout=3)
            return {
                "provider": "cnki",
                "query_type": "title",
                "open_url": "https://oversea.cnki.net/kns8s/search?kw=x",
                "candidates": [],
            }

        first_result: dict[str, object] = {}
        with self._server() as base_url, patch(
            "src.me_finder.web.lookup_cnki_journal", side_effect=slow_lookup
        ):
            def first_request() -> None:
                first_result["value"] = self._post(
                    base_url,
                    "/api/bibliographic-metadata/lookup-cnki",
                    {"metadata": {"title": "第一篇"}},
                )

            worker = threading.Thread(target=first_request)
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            second_status, second_payload = self._post(
                base_url,
                "/api/bibliographic-metadata/lookup-cnki",
                {"metadata": {"title": "第二篇"}},
            )
            release.set()
            worker.join(timeout=3)

        self.assertEqual(second_status, 409)
        self.assertEqual(second_payload["code"], "lookup_busy")
        self.assertEqual(first_result["value"][0], 200)


if __name__ == "__main__":
    unittest.main()
