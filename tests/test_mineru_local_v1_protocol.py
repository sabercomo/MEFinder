"""MinerU 4.x ``/v1`` protocol detection and parse-job normalization."""

import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.me_finder.mineru_local_provider import (
    MINERU_TASKS_PROTOCOL,
    MinerULocalConfig,
    MinerULocalProvider,
)
from src.me_finder.mineru_local_v1 import (
    MINERU_V1_PROTOCOL,
    plain_text_from_markdown,
    tier_for_backend,
)
from src.me_finder.parser_provider import (
    ParserProviderError,
    ParserRequest,
    ParserTaskStatus,
)


STRUCTURED_CONTENT = {
    "schema": "docvortex.middle",
    "pages": [
        {
            "page_idx": 0,
            "blocks": [
                {
                    "type": "doc_title",
                    "level": 1,
                    "content": "戦闘美少女の精神分析",
                    "bbox": [0.1, 0.2, 0.9, 0.3],
                },
                {
                    "type": "text",
                    # 上游交付的是渲染后的 Markdown，粗体带 ** 标记
                    "content": "労働は**価値**の実体である。",
                    "bbox": [0.1, 0.4, 0.9, 0.5],
                },
            ],
        },
        {
            "page_idx": 1,
            "blocks": [
                {
                    "type": "paragraph_title",
                    "level": 3,
                    "content": "第一節",
                    "bbox": [0.1, 0.1, 0.5, 0.15],
                },
                {
                    "type": "image",
                    "content": "![](figure-1.png)",
                    "captions": [{"content": "図 1 分析図"}],
                    "footnotes": [{"content": "出典：著者作成"}],
                    "bbox": [0.2, 0.3, 0.8, 0.7],
                },
                {"type": "text", "content": "   "},
            ],
        },
    ],
}


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class FakeMinerUV1Handler(BaseHTTPRequestHandler):
    job_status = "completed"
    uploaded = b""
    dedupe = False
    created_job = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/v1/health":
            self._json(200, {"version": "4.0.4", "features": {"sources": ["file_id"]}})
            return
        if self.path == "/v1/parse/jobs/job-1":
            self._json(200, self._job_payload())
            return
        if self.path == "/v1/files/out-1/content":
            self._raw(200, json.dumps(STRUCTURED_CONTENT).encode("utf-8"))
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        if self.path == "/v1/uploads":
            payload = {
                "id": "upload-1",
                "object": "upload",
                "bytes": json.loads(body).get("bytes", 0),
                "status": "pending",
            }
            if self.dedupe:
                payload["status"] = "completed"
                payload["file"] = {"id": "file-dedupe", "object": "file"}
            self._json(200, payload)
            return
        if self.path == "/v1/uploads/upload-1/complete":
            self._json(
                200,
                {
                    "id": "upload-1",
                    "status": "completed",
                    "file": {"id": "file-1", "object": "file"},
                },
            )
            return
        if self.path == "/v1/parse/jobs":
            type(self).created_job = json.loads(body)
            self._json(200, self._job_payload(status="queued"))
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_PUT(self):
        if self.path == "/v1/uploads/upload-1/content":
            length = int(self.headers.get("Content-Length") or 0)
            type(self).uploaded = self.rfile.read(length)
            self._raw(200, b"")
            return
        self._json(404, {"error": {"message": "not found"}})

    def _job_payload(self, status=None):
        resolved = status or self.job_status
        entry = {
            "file_id": "file-1",
            "name": "slice.pdf",
            "page_range": "1-2",
            "status": "completed" if resolved == "completed" else "queued",
        }
        if resolved == "completed":
            entry["output_files"] = {
                "structured_content": {"file_id": "out-1", "bytes": 1234}
            }
        if resolved == "failed":
            entry["status"] = "failed"
            entry["error"] = {"message": "模型加载失败"}
        return {
            "job_id": "job-1",
            "status": resolved,
            "created_at": "2026-09-20T00:00:00Z",
            "tier": "standard",
            "output_formats": ["structured_content"],
            "access_level": "anonymous",
            "progress": {"completed": 1 if resolved == "completed" else 0, "total": 2},
            "files": [entry],
            "links": {"self": "/v1/parse/jobs/job-1", "cancel": "/v1/parse/jobs/job-1"},
        }

    def _json(self, status, payload):
        self._raw(status, json.dumps(payload).encode("utf-8"))

    def _raw(self, status, raw):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeMinerUV1Service:
    def __enter__(self):
        FakeMinerUV1Handler.job_status = "completed"
        FakeMinerUV1Handler.uploaded = b""
        FakeMinerUV1Handler.dedupe = False
        FakeMinerUV1Handler.created_job = None
        self.server = QuietServer(("127.0.0.1", 0), FakeMinerUV1Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.endpoint = f"http://{host}:{port}"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class MinerUV1ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "slice.pdf"
        self.path.write_bytes(b"%PDF-v1-protocol-test")

    def request(self):
        return ParserRequest(
            source_path=self.path,
            source_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
            document_id="doc-slice",
            page_start=1,
            page_end=2,
            global_page_offset=40,
        )

    def test_auto_detection_reports_v1_protocol_and_version(self) -> None:
        with FakeMinerUV1Service() as service:
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            health = provider.health()
            self.assertEqual(health["protocol"], MINERU_V1_PROTOCOL)
            self.assertEqual(health["mineru_version"], "4.0.4")
            self.assertEqual(
                provider.capabilities().optional_limits["protocol"],
                MINERU_V1_PROTOCOL,
            )

    def test_submit_uploads_then_creates_parse_job(self) -> None:
        with FakeMinerUV1Service() as service:
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            submission = provider.submit(self.request())
            self.assertEqual(submission.remote_task_id, "job-1")
            self.assertEqual(submission.status, ParserTaskStatus.SUBMITTED)
            self.assertEqual(submission.metadata["protocol"], MINERU_V1_PROTOCOL)
            self.assertEqual(FakeMinerUV1Handler.uploaded, self.path.read_bytes())
            created = FakeMinerUV1Handler.created_job
            self.assertEqual(
                created["files"],
                [{"source": {"type": "file_id", "file_id": "file-1"}}],
            )
            self.assertEqual(created["output_formats"], ["structured_content"])
            self.assertEqual(created["ocr_mode"], "auto")
            self.assertEqual(created["tier"], "basic")

    def test_upload_reuses_server_side_deduplicated_blob(self) -> None:
        with FakeMinerUV1Service() as service:
            FakeMinerUV1Handler.dedupe = True
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            provider.submit(self.request())
            self.assertEqual(FakeMinerUV1Handler.uploaded, b"")
            self.assertEqual(
                FakeMinerUV1Handler.created_job["files"][0]["source"]["file_id"],
                "file-dedupe",
            )

    def test_structured_content_keeps_page_anchors_and_citation_canvas(self) -> None:
        with FakeMinerUV1Service() as service:
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            submission = provider.submit(self.request())
            self.assertEqual(provider.poll("job-1").status, ParserTaskStatus.COMPLETED)
            request = self.request()
            normalized = provider.normalize_result(
                provider.fetch_result(submission, request), request
            )
            self.assertEqual(
                [page.physical_pdf_page for page in normalized.pages], [41, 42]
            )
            first = normalized.pages[0].blocks[0]
            # 0..1 boxes are rescaled onto MinerU's 1000-unit citation canvas
            # and the exact normalized box stays available for anchor math.
            self.assertEqual(first.bbox, (100.0, 200.0, 900.0, 300.0))
            self.assertEqual(
                first.provenance["bbox_normalized"], [0.1, 0.2, 0.9, 0.3]
            )
            self.assertEqual(first.text_level, 1)
            self.assertEqual(normalized.pages[1].blocks[0].text_level, 3)
            # Blank blocks are dropped; captions and footnotes stay with the figure.
            figure = normalized.pages[1].blocks[1]
            self.assertEqual(len(normalized.pages[1].blocks), 2)
            self.assertIn("図 1 分析図", figure.text)
            self.assertIn("出典：著者作成", figure.text)
            self.assertEqual(normalized.parser_version, "4.0.4")
            self.assertEqual(normalized.provenance["protocol"], MINERU_V1_PROTOCOL)

    def test_failed_job_surfaces_upstream_message(self) -> None:
        with FakeMinerUV1Service() as service:
            FakeMinerUV1Handler.job_status = "failed"
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            submission = provider.submit(self.request())
            poll = provider.poll("job-1")
            self.assertEqual(poll.status, ParserTaskStatus.PERMANENT_FAILURE)
            self.assertEqual(poll.message, "模型加载失败")
            with self.assertRaises(ParserProviderError) as caught:
                provider.fetch_result(submission, self.request())
            self.assertIn("模型加载失败", str(caught.exception))

    def test_pinned_protocol_skips_detection(self) -> None:
        with FakeMinerUV1Service() as service:
            provider = MinerULocalProvider(
                MinerULocalConfig(
                    endpoint=service.endpoint, protocol=MINERU_TASKS_PROTOCOL
                )
            )
            # A 4.x service pinned to the 3.x protocol must fail loudly rather
            # than silently falling back.
            with self.assertRaises(ParserProviderError):
                provider.health()

    def test_api_key_is_sent_as_bearer_token(self) -> None:
        with FakeMinerUV1Service() as service:
            provider = MinerULocalProvider(
                MinerULocalConfig(endpoint=service.endpoint, api_key="secret-key")
            )
            headers = provider.v1_client.transport.headers()
            self.assertEqual(headers["Authorization"], "Bearer secret-key")
            self.assertTrue(provider.health()["ok"])

    def test_stored_text_is_plain_source_text_not_markdown(self) -> None:
        with FakeMinerUV1Service() as service:
            provider = MinerULocalProvider(MinerULocalConfig(endpoint=service.endpoint))
            submission = provider.submit(self.request())
            request = self.request()
            normalized = provider.normalize_result(
                provider.fetch_result(submission, request), request
            )
            body = normalized.pages[0].blocks[1]
            # 上游给的是渲染后的 Markdown；入库必须是印刷原文，否则逐字定位失配、
            # 引文里会带出 ** 之类的标记。
            self.assertEqual(body.text, "労働は価値の実体である。")
            self.assertNotIn("*", body.text)
            figure = normalized.pages[1].blocks[1]
            self.assertNotIn("![", figure.text)
            self.assertNotIn("](", figure.text)

    def test_markdown_recovery_keeps_literal_characters(self) -> None:
        cases = (
            ("劳动是**价值**的实体。", "劳动是价值的实体。"),
            ("这是 *斜体* 与 ***两者*** 与 ~~删除~~", "这是 斜体 与 两者 与 删除"),
            # 上游对字面量做了反斜杠转义，还原后必须保留原字符
            (r"脚注 5\* 与 a\_b", "脚注 5* 与 a_b"),
            (r"\# 不是标题", "# 不是标题"),
            ("见 [全集](https://example.com/a) 第 3 卷", "见 全集 第 3 卷"),
            ("![](fig.png)图注", "图注"),
            ("代码 `x = 1` 行内", "代码 x = 1 行内"),
            ("公式 $E = mc^2$ 完", "公式 E = mc^2 完"),
            ("<strong>强调</strong>与<sup>2</sup>", "强调与2"),
            ("a&nbsp;b<br>c", "a b\nc"),
            ("- 甲\n- 乙", "甲\n乙"),
            ("| 年份 | 页码 |\n|---|---|\n| 1867 | 12 |", "年份 页码\n1867 12"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(plain_text_from_markdown(source), expected)

    def test_backend_names_map_onto_tiers(self) -> None:
        self.assertEqual(tier_for_backend("pipeline"), "basic")
        self.assertEqual(tier_for_backend("vlm-auto-engine"), "standard")
        self.assertEqual(tier_for_backend("advanced"), "advanced")
        self.assertEqual(tier_for_backend("unknown-backend"), "standard")


if __name__ == "__main__":
    unittest.main()
