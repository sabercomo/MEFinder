"""v0.5.8 agent bibliographic fill: propose → confirm → desktop applies."""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from src.me_finder.app_context import AppPaths
from src.me_finder.application import LiteratureVerificationService
from src.me_finder.application.bibliographic_metadata_coordinator import (
    BibliographicMetadataCoordinator,
)
from src.me_finder.application.bibliographic_update_applier import (
    apply_confirmed_updates,
)
from src.me_finder.bibliographic_fill import (
    manual_save_payload,
    normalize_proposals,
    plan_fill,
)
from src.me_finder.bibliographic_updates import list_requests
from src.me_finder.database import build_database
from src.me_finder.mcp_server import TOOLS, _call_tool
from src.me_finder.persistence.bibliographic_update_store import (
    read_bibliographic_update_snapshot,
    restore_bibliographic_update_snapshot,
)
from src.me_finder.persistence.migrations import migrate_index_database
from tests.mcp_v1_fixture import PDF_SOURCE_ID, build_mcp_v1_fixture

PUBLISHER = {
    "field": "publisher",
    "value": "人民出版社",
    "evidence_text": "人民出版社出版发行",
    "source_page": "版权页",
}

ISBN = {
    "field": "isbn",
    "value": "9787010000000",
    "evidence_text": "ISBN 978-7-01-000000-0",
    "source_page": "iv",
}


class FillPlanTests(unittest.TestCase):
    def test_only_empty_fields_are_filled_and_existing_values_win(self) -> None:
        proposals = normalize_proposals(
            [
                PUBLISHER,
                {"field": "title", "value": "新书名", "evidence_text": "书名页"},
                {"field": "author", "value": "作者甲", "evidence_text": "作者甲 著"},
            ]
        )
        plan = plan_fill({"title": "旧书名", "author": "作者甲"}, proposals)
        self.assertEqual(
            [(item["field"], item["action"]) for item in plan],
            [("publisher", "fill"), ("title", "conflict"), ("author", "same")],
        )
        payload = manual_save_payload(
            {"title": "旧书名", "author": "作者甲", "document_type": "book"}, plan
        )
        self.assertEqual(payload["title"], "旧书名")
        self.assertEqual(payload["publisher"], "人民出版社")
        self.assertEqual(payload["document_type"], "book")
        self.assertEqual(list(payload["metadata_evidence"]), ["publisher"])
        self.assertEqual(payload["metadata_evidence"]["publisher"]["source"], "mcp_agent")

    def test_proposals_need_evidence_and_valid_distinct_fields(self) -> None:
        bad_inputs = [
            [{**PUBLISHER, "evidence_text": ""}],
            [PUBLISHER, PUBLISHER],
            [{"field": "language", "value": "zh", "evidence_text": "x"}],
            [{"field": "doi", "value": "not a doi", "evidence_text": "x"}],
            [{"field": "title", "value": "???", "evidence_text": "x"}],
        ]
        for proposals in bad_inputs:
            with self.subTest(proposals=proposals), self.assertRaises(ValueError):
                normalize_proposals(proposals)
        [doi] = normalize_proposals(
            [{"field": "doi", "value": "https://doi.org/10.1000/ABC", "evidence_text": "x"}]
        )
        self.assertEqual(doi["value"], "10.1000/abc")


class McpProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name)
        self.index_path = self.runtime_root / "data" / "index.sqlite3"
        build_mcp_v1_fixture(self.index_path)
        self.service = LiteratureVerificationService(lambda: self.runtime_root)
        self.validators = {
            tool.name: Draft202012Validator(tool.output_schema) for tool in TOOLS
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = _call_tool(self.service, name, arguments)
        self.assertFalse(result.is_error, result.structured_content)
        self.validators[name].validate(result.structured_content)
        return result.structured_content

    def test_propose_previews_and_queues_without_changing_metadata(self) -> None:
        before = self.call("read_bibliographic_metadata", {"source_file_id": PDF_SOURCE_ID})
        title = next(item for item in before["fields"] if item["field"] == "title")
        self.assertEqual(title["status"], "present")

        proposal = self.call(
            "propose_bibliographic_update",
            {
                "source_file_id": PDF_SOURCE_ID,
                "fields": [
                    ISBN,
                    {"field": "title", "value": "别的书名", "evidence_text": "书名页"},
                ],
            },
        )
        self.assertEqual(
            {item["field"]: item["action"] for item in proposal["fields"]},
            {"isbn": "fill", "title": "conflict"},
        )
        self.assertIsNotNone(proposal["confirmation_token"])

        after = self.call("read_bibliographic_metadata", {"source_file_id": PDF_SOURCE_ID})
        self.assertEqual(after["fields"], before["fields"])
        [request] = after["update_requests"]
        self.assertEqual(request["status"], "pending")
        # Only the fillable field is queued; the conflict is never sent to the writer.
        self.assertEqual(request["fields"], ["isbn"])

    def test_nothing_fillable_records_no_request(self) -> None:
        result = _call_tool(
            self.service,
            "propose_bibliographic_update",
            {
                "source_file_id": PDF_SOURCE_ID,
                "fields": [{"field": "title", "value": "别的书名", "evidence_text": "书名页"}],
            },
        )
        self.assertIsNone(result.structured_content["request_id"])
        self.assertIn("未记录请求", result.content[0].text)
        self.assertEqual(list_requests(self.index_path), [])

    def test_confirm_requires_the_issued_token(self) -> None:
        proposal = self.call(
            "propose_bibliographic_update",
            {"source_file_id": PDF_SOURCE_ID, "fields": [ISBN]},
        )
        wrong = _call_tool(
            self.service,
            "confirm_bibliographic_update",
            {"request_id": proposal["request_id"], "confirmation_token": "x" * 32},
        )
        self.assertTrue(wrong.is_error)
        self.assertEqual(list_requests(self.index_path)[0]["status"], "pending")

        confirmed = self.call(
            "confirm_bibliographic_update",
            {
                "request_id": proposal["request_id"],
                "confirmation_token": proposal["confirmation_token"],
            },
        )
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["fields"], ["isbn"])

    def test_applier_writes_fill_and_records_the_result(self) -> None:
        proposal = self.call(
            "propose_bibliographic_update",
            {"source_file_id": PDF_SOURCE_ID, "fields": [ISBN]},
        )
        self.call(
            "confirm_bibliographic_update",
            {
                "request_id": proposal["request_id"],
                "confirmation_token": proposal["confirmation_token"],
            },
        )
        written = []

        def fill_empty_fields(source_file_id, build):
            written.append((source_file_id, build({"title": "MCP 合成 PDF 样例"})))
            return written[-1][1]

        self.assertEqual(apply_confirmed_updates(self.index_path, fill_empty_fields), 1)
        [(source_id, payload)] = written
        self.assertEqual(source_id, PDF_SOURCE_ID)
        self.assertEqual(payload["isbn"], "9787010000000")
        self.assertEqual(payload["title"], "MCP 合成 PDF 样例")

        [request] = list_requests(self.index_path)
        self.assertEqual(request["status"], "applied")
        self.assertEqual(request["result"]["filled"], ["isbn"])
        # A closed request is never applied twice.
        self.assertEqual(apply_confirmed_updates(self.index_path, fill_empty_fields), 0)
        self.validators["read_bibliographic_metadata"].validate(
            self.service.read_bibliographic_metadata(PDF_SOURCE_ID)
        )

    def test_sparse_config_record_never_blanks_values_shown_from_the_index(self) -> None:
        proposal = self.call(
            "propose_bibliographic_update",
            {"source_file_id": PDF_SOURCE_ID, "fields": [ISBN]},
        )
        self.call(
            "confirm_bibliographic_update",
            {
                "request_id": proposal["request_id"],
                "confirmation_token": proposal["confirmation_token"],
            },
        )
        payloads = []
        # A legacy config entry can lack the metadata the panel shows from the index.
        apply_confirmed_updates(
            self.index_path,
            lambda _sid, build: payloads.append(build({"source_file_id": PDF_SOURCE_ID})),
        )
        [payload] = payloads
        self.assertEqual(payload["isbn"], "9787010000000")
        self.assertEqual(payload["title"], "MCP 合成 PDF 样例")
        self.assertEqual(payload["author"], "测试作者甲")
        self.assertEqual(payload["publisher"], "测试出版社")

    def test_value_filled_meanwhile_becomes_a_conflict_not_an_overwrite(self) -> None:
        proposal = self.call(
            "propose_bibliographic_update",
            {"source_file_id": PDF_SOURCE_ID, "fields": [ISBN]},
        )
        self.call(
            "confirm_bibliographic_update",
            {
                "request_id": proposal["request_id"],
                "confirmation_token": proposal["confirmation_token"],
            },
        )
        payloads = []
        apply_confirmed_updates(
            self.index_path,
            lambda _sid, build: payloads.append(build({"isbn": "9787000000000"})),
        )
        self.assertEqual(payloads, [None])
        [request] = list_requests(self.index_path)
        self.assertEqual(request["status"], "applied")
        self.assertEqual(request["result"]["filled"], [])
        self.assertEqual(request["result"]["conflicts"][0]["kept_value"], "9787000000000")

    def test_write_failure_is_recorded_as_failed(self) -> None:
        proposal = self.call(
            "propose_bibliographic_update",
            {"source_file_id": PDF_SOURCE_ID, "fields": [ISBN]},
        )
        self.call(
            "confirm_bibliographic_update",
            {
                "request_id": proposal["request_id"],
                "confirmation_token": proposal["confirmation_token"],
            },
        )

        def refuse(_sid, _build):
            raise ValueError("PDF 配置中找不到该文献。")

        apply_confirmed_updates(self.index_path, refuse)
        [request] = list_requests(self.index_path)
        self.assertEqual(request["status"], "failed")
        self.assertIn("找不到", request["result"]["message"])


class CoordinatorFillTests(unittest.TestCase):
    def test_payload_is_built_inside_the_config_lock_with_real_manual_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = AppPaths.create(root, index_path=root / "data" / "index.sqlite3")
            events: list[str] = []
            document = {"source_file_id": "pdf-one", "title": "旧书名", "author": "作者甲"}
            config = {"documents": [document]}
            saves = []

            @contextmanager
            def lock_config(_path):
                events.append("config_enter")
                yield config
                events.append("config_exit")

            @contextmanager
            def noop():
                yield

            class Runtime:
                def mutation(self):
                    return noop()

                def suspend(self):
                    events.append("suspend")

                def reopen(self, *, attempts=1):
                    return True

            class Durable:
                def operation(self):
                    return noop()

            coordinator = BibliographicMetadataCoordinator(
                paths,
                queries=None,
                index_runtime=Runtime(),
                durable_operations=Durable(),
                jobs=None,
                lock_config=lock_config,
                save_config=lambda _path, data: saves.append(copy.deepcopy(data)),
                update_database=lambda *_args: events.append("update_database"),
            )

            def build(current):
                events.append("build")
                plan = plan_fill(current, normalize_proposals([PUBLISHER]))
                return manual_save_payload(current, plan)

            metadata = coordinator.fill_empty_fields("pdf-one", build)

        self.assertEqual(events[:2], ["config_enter", "build"])
        self.assertLess(events.index("build"), events.index("update_database"))
        self.assertEqual(metadata["publisher"], "人民出版社")
        self.assertEqual(metadata["title"], "旧书名")
        self.assertEqual(metadata["metadata_source"], "manual")
        evidence = metadata["metadata_evidence"]["publisher"]
        self.assertEqual((evidence["source"], evidence["source_page"]), ("mcp_agent", "版权页"))
        self.assertEqual(saves[0]["documents"][0]["publisher"], "人民出版社")


class QueueSchemaTests(unittest.TestCase):
    def test_v8_index_migrates_and_requests_survive_a_rebuild_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            index_path = Path(temp) / "data" / "index.sqlite3"
            build_mcp_v1_fixture(index_path)
            connection = sqlite3.connect(index_path)
            connection.execute("PRAGMA user_version = 8")
            connection.close()

            migrate_index_database(index_path)
            connection = sqlite3.connect(index_path)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.close()
            self.assertEqual(version, 10)

            service = LiteratureVerificationService(lambda: Path(temp))
            service.propose_bibliographic_update(
                source_file_id=PDF_SOURCE_ID, fields=[ISBN]
            )
            snapshot = read_bibliographic_update_snapshot(index_path)
            self.assertEqual(len(snapshot), 1)

            fresh = Path(temp) / "fresh.sqlite3"
            build_database({"source_files": [], "paragraphs": []}, fresh)
            connection = sqlite3.connect(fresh)
            restore_bibliographic_update_snapshot(connection, snapshot)
            connection.commit()
            connection.close()
            self.assertEqual(
                [item["request_id"] for item in list_requests(fresh)],
                [snapshot[0]["request_id"]],
            )


class RuntimeApplierTests(unittest.TestCase):
    """The real desktop runtime applies a confirmed request on its own."""

    def test_running_runtime_writes_confirmed_fill_to_config_and_index(self) -> None:
        from scripts.performance_fixture import create_fixture
        from src.me_finder.app_context import AppContext
        from src.me_finder.web_runtime import build_application_runtime

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_fixture(root, documents=2, paragraphs=10, alignment_paragraphs=4)
            index_path = root / "data" / "index.sqlite3"
            service = LiteratureVerificationService(lambda: root)
            source_id = service.list_documents()["documents"][0]["source_file_id"]
            config_path = root / "config" / "pdf_imports.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps({"documents": [{"source_file_id": source_id}]}),
                encoding="utf-8",
            )
            proposal = service.propose_bibliographic_update(
                source_file_id=source_id, fields=[ISBN]
            )
            service.confirm_bibliographic_update(
                request_id=proposal["request_id"],
                confirmation_token=proposal["confirmation_token"],
            )

            with mock.patch(
                "src.me_finder.tasks.runtime_lifecycle.translation_works"
                ".start_body_bounds_warm_up"
            ):
                runtime = build_application_runtime(
                    AppContext.create(root, index_path=index_path),
                    open_pdf_with_platform=lambda *args: None,
                    open_path_with_default_app=lambda *args: None,
                    open_external_cnki_url=lambda *args: None,
                    open_mineru_token_page=lambda *args: None,
                )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    [request] = list_requests(index_path)
                    if request["status"] != "confirmed":
                        break
                    time.sleep(0.1)
            finally:
                self.assertTrue(runtime.close_runtime(timeout=5))

            self.assertEqual(request["status"], "applied", request["result"])
            self.assertEqual(request["result"]["filled"], ["isbn"])
            [document] = json.loads(config_path.read_text(encoding="utf-8"))["documents"]
            self.assertEqual(document["isbn"], "9787010000000")
            evidence = document["bibliographic_metadata"]["metadata_evidence"]["isbn"]
            self.assertEqual(evidence["source"], "mcp_agent")
            isbn = next(
                item
                for item in service.read_bibliographic_metadata(source_id)["fields"]
                if item["field"] == "isbn"
            )
            self.assertEqual((isbn["value"], isbn["status"]), ("9787010000000", "present"))


if __name__ == "__main__":
    unittest.main()
