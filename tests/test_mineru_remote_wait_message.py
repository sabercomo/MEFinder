from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.application.import_job_lifecycle import mineru_progress_message
from src.me_finder.large_document import engine as engine_module
from src.me_finder.large_document.engine import LargeDocumentJobEngine
from src.me_finder.large_document.job_ledger import JobLedger
from src.me_finder.large_document.slicing import PhysicalPDFSlicer
from src.me_finder.parser_provider import (
    ParserProvider,
    ParserPollResult,
    ParserSubmission,
    ParserTaskStatus,
    ProviderCapabilities,
)


class QueuedUpstreamProvider(ParserProvider):
    """Accepts the slice and then reports the remote queue forever."""

    provider_id = "queued-upstream"

    def capabilities(self):
        return ProviderCapabilities(
            max_pages_per_file=200,
            max_bytes_per_file=None,
            max_concurrency=1,
            supports_async_jobs=True,
        )

    def submit(self, request, *, credential=None):
        self.prepare(request)
        return ParserSubmission(
            self.provider_id, "remote-1", ParserTaskStatus.SUBMITTED
        )

    def poll(self, remote_task_id, *, credential=None):
        return ParserPollResult(ParserTaskStatus.WAITING, remote_state="pending")

    def fetch_result(self, submission, request, *, credential=None):
        raise AssertionError("a queued task has no result")

    def normalize_result(self, raw_result, request):
        raise AssertionError("a queued task has no result")


def synthetic_slice_writer(source, start, end, output):
    with Path(output).open("wb") as stream:
        stream.write(b"%PDF-slice\n")
        for page in range(start, end + 1):
            stream.write((f"page-{page}\n".encode("ascii")) * 8)


class MinerURemoteWaitMessageTests(unittest.TestCase):
    """A provider queue that never starts must not be reported as parsing."""

    def test_queued_remote_state_is_surfaced_with_waited_minutes(self) -> None:
        self.assertEqual(
            mineru_progress_message(
                {
                    "completed": 0,
                    "total": 1,
                    "remote_state": "pending",
                    "remote_waiting_minutes": 17,
                }
            ),
            "MinerU 云端排队中：已等待 17 分钟，0/1 个分段",
        )

    def test_running_remote_state_keeps_the_parsing_text(self) -> None:
        self.assertEqual(
            mineru_progress_message(
                {
                    "completed": 1,
                    "total": 3,
                    "remote_state": "running",
                    "remote_waiting_minutes": 17,
                }
            ),
            "MinerU 解析中：1/3 个分段",
        )

    def test_first_minute_of_queueing_does_not_claim_a_jam(self) -> None:
        self.assertEqual(
            mineru_progress_message(
                {"completed": 0, "total": 1, "remote_state": "pending", "remote_waiting_minutes": 0}
            ),
            "MinerU 解析中：0/1 个分段",
        )

    def test_credential_shortage_still_wins_over_remote_state(self) -> None:
        self.assertEqual(
            mineru_progress_message(
                {
                    "completed": 0,
                    "total": 3,
                    "waiting_for_credential": True,
                    "remote_state": "pending",
                    "remote_waiting_minutes": 9,
                }
            ),
            "等待可用的 MinerU 账号：0/3 个分段已完成",
        )

    def test_engine_reports_how_long_a_slice_sat_in_one_remote_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            source.write_bytes(b"source")
            ledger = JobLedger(root / "jobs.sqlite3")
            engine = LargeDocumentJobEngine(
                ledger=ledger,
                provider=QueuedUpstreamProvider(),
                work_dir=root / "work",
                slicer=PhysicalPDFSlicer(synthetic_slice_writer),
                page_counter=lambda path: 3,
            )
            job = engine.prepare(
                source_path=source,
                source_file_id="pdf-1",
                document_id="doc",
            )
            now = [1000.0]
            clock = types.SimpleNamespace(time=lambda: now[0])
            with mock.patch.object(engine_module, "time", clock):
                self.assertEqual(engine.remote_wait(job.id), {})
                engine.run_once(job.id)  # submits the slice
                self.assertEqual(engine.remote_wait(job.id), {})
                engine.run_once(job.id)  # first poll notes "pending"
                now[0] += 7 * 60 + 30
                engine.run_once(job.id)  # same state: the clock keeps running
                self.assertEqual(
                    engine.remote_wait(job.id),
                    {"remote_state": "pending", "remote_waiting_minutes": 7},
                )


if __name__ == "__main__":
    unittest.main()
