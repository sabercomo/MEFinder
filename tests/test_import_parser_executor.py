from __future__ import annotations

import unittest
from pathlib import Path
from typing import Dict, Optional
from unittest import mock

from src.me_finder.application.import_job_store import ImportJobCancelled, Job
from src.me_finder.application.import_parser_executor import ImportParserExecutor
from src.me_finder.mineru_api import MinerUError
from src.me_finder.vision_api import VisionAPIError


class _FakeJobs:
    def __init__(self, job: Optional[Job] = None) -> None:
        self.job: Job = dict(job or {"job_id": "import-one"})
        self.updates: list[Dict[str, object]] = []
        self.progress: list[Dict[str, object]] = []
        self.switches: list[Dict[str, object]] = []
        self.events: list[str] = []

    def job_status(self, _job_id: str) -> Job:
        return dict(self.job)

    def update_import_job(self, _job_id: str, **updates: object) -> None:
        self.events.append("update:" + str(updates.get("phase") or ""))
        self.updates.append(dict(updates))
        self.job.update(updates)

    def progress_import_job(
        self,
        _job_id: str,
        update: Dict[str, object],
    ) -> None:
        self.events.append("progress:" + str(update.get("phase") or ""))
        self.progress.append(dict(update))

    def switch_import_job_route(
        self,
        _job_id: str,
        *,
        parse_route: str,
        force_mineru: bool,
        vision_provider_id: Optional[str],
        provider_name: Optional[str],
    ) -> None:
        route = {
            "parse_route": parse_route,
            "force_mineru": force_mineru,
            "vision_provider_id": vision_provider_id,
            "provider_name": provider_name,
        }
        self.events.append("switch:" + parse_route)
        self.switches.append(route)
        self.job.update(route)


class ImportParserExecutorTests(unittest.TestCase):
    root = Path("/runtime")
    target = Path("/runtime/corpus/raw_pdf/paper.pdf")

    @staticmethod
    def _provider_summary(*, auto: bool = False) -> Dict[str, object]:
        return {
            "providers": [
                {
                    "id": "provider-one",
                    "name": "Provider One",
                    "enabled": True,
                    "configured": True,
                },
                {
                    "id": "provider-two",
                    "name": "Provider Two",
                    "enabled": True,
                    "configured": True,
                },
            ],
            "default_provider_id": "provider-one",
            "auto_fallback_from_mineru": auto,
        }

    def _executor(
        self,
        *,
        mineru=None,
        provider=None,
    ) -> ImportParserExecutor:
        return ImportParserExecutor(
            self.root,
            parse_with_mineru=mineru or mock.Mock(),
            parse_with_provider=provider or mock.Mock(),
        )

    def test_native_route_records_state_without_calling_a_parser(self) -> None:
        mineru = mock.Mock()
        provider = mock.Mock()
        jobs = _FakeJobs()
        executor = self._executor(mineru=mineru, provider=provider)

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "native_text"},
            True,
            jobs=jobs,
        )

        self.assertTrue(succeeded)
        mineru.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(jobs.job["phase"], "text_parsing")
        self.assertEqual(jobs.job["parse_route"], "native")

    def test_explicit_vision_uses_late_bound_parser_and_forwards_progress(
        self,
    ) -> None:
        first = mock.Mock()
        second = mock.Mock()
        selected = {"parser": first}

        def late_bound(*args, **kwargs):
            return selected["parser"](*args, **kwargs)

        executor = self._executor(provider=late_bound)
        selected["parser"] = second
        jobs = _FakeJobs({"provider_name": "Chosen Provider"})

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "scanned"},
            True,
            vision_provider_id="chosen-provider",
            jobs=jobs,
        )

        self.assertTrue(succeeded)
        first.assert_not_called()
        second.assert_called_once()
        self.assertEqual(
            second.call_args.args,
            (self.root, self.target, "pdf-one", "chosen-provider"),
        )
        second.call_args.kwargs["on_progress"](
            {"phase": "vision_processing", "completed": 1}
        )
        self.assertEqual(jobs.progress[-1]["completed"], 1)
        self.assertIn("Chosen Provider", str(jobs.updates[0]["message"]))

    def test_mineru_success_uses_force_message_and_forwards_progress(self) -> None:
        mineru = mock.Mock()
        jobs = _FakeJobs()
        executor = self._executor(mineru=mineru)

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "native_text"},
            True,
            force_mineru=True,
            jobs=jobs,
        )

        self.assertTrue(succeeded)
        mineru.assert_called_once()
        mineru.call_args.kwargs["on_progress"](
            {"phase": "mineru_processing", "completed": 2}
        )
        self.assertEqual(jobs.progress[-1]["completed"], 2)
        self.assertIn("已选择 MinerU", str(jobs.updates[0]["message"]))

    def test_local_mineru_request_is_explicit_and_passes_local_flag(self) -> None:
        mineru = mock.Mock()
        jobs = _FakeJobs()
        executor = self._executor(mineru=mineru)

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {
                "detected_pdf_type": "scanned",
                "mineru_local": True,
            },
            True,
            force_mineru=True,
            jobs=jobs,
        )

        self.assertTrue(succeeded)
        self.assertTrue(mineru.call_args.kwargs["use_local"])
        self.assertEqual(jobs.job["provider_id"], "mineru-local")
        self.assertEqual(jobs.job["provider_name"], "本地 MinerU")
        self.assertIn("本地 MinerU", str(jobs.updates[0]["message"]))

    @mock.patch(
        "src.me_finder.application.import_parser_executor.vision_config_summary",
        return_value={
            "providers": [],
            "default_provider_id": None,
            "auto_fallback_from_mineru": False,
        },
    )
    def test_transient_mineru_interruption_keeps_resume_without_paid_fallback(
        self,
        _summary: mock.Mock,
    ) -> None:
        provider = mock.Mock()
        jobs = _FakeJobs()
        executor = self._executor(
            mineru=mock.Mock(side_effect=RuntimeError("temporary")),
            provider=provider,
        )

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "scanned"},
            True,
            jobs=jobs,
        )

        self.assertFalse(succeeded)
        provider.assert_not_called()
        self.assertEqual(jobs.job["status"], "failed")
        self.assertTrue(jobs.job["can_resume"])
        self.assertTrue(jobs.job["mineru_interrupted"])
        self.assertFalse(jobs.job["mineru_failed"])
        self.assertIn("不会自动改用其他付费接口", str(jobs.job["message"]))

    @mock.patch(
        "src.me_finder.application.import_parser_executor.vision_config_summary"
    )
    def test_permanent_mineru_failure_only_offers_manual_provider(
        self,
        summary: mock.Mock,
    ) -> None:
        summary.return_value = self._provider_summary(auto=False)
        provider = mock.Mock()
        jobs = _FakeJobs()
        executor = self._executor(
            mineru=mock.Mock(
                side_effect=MinerUError(
                    "account rejected",
                    allow_parser_fallback=True,
                )
            ),
            provider=provider,
        )

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "scanned"},
            True,
            jobs=jobs,
        )

        self.assertFalse(succeeded)
        provider.assert_not_called()
        self.assertTrue(jobs.job["mineru_failed"])
        self.assertFalse(jobs.job["mineru_interrupted"])
        self.assertTrue(jobs.job["can_retry_with_provider"])
        self.assertEqual(jobs.job["retry_provider_id"], "provider-one")
        self.assertFalse(jobs.job["needs_provider_config"])

    @mock.patch(
        "src.me_finder.application.import_parser_executor.vision_config_summary"
    )
    def test_auto_fallback_switches_route_before_calling_provider(
        self,
        summary: mock.Mock,
    ) -> None:
        summary.return_value = self._provider_summary(auto=True)
        jobs = _FakeJobs()

        def provider(*_args, **_kwargs) -> None:
            jobs.events.append("provider")

        executor = self._executor(
            mineru=mock.Mock(
                side_effect=MinerUError(
                    "account rejected",
                    allow_parser_fallback=True,
                )
            ),
            provider=provider,
        )

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "scanned"},
            True,
            jobs=jobs,
        )

        self.assertTrue(succeeded)
        self.assertEqual(jobs.switches[0]["vision_provider_id"], "provider-one")
        self.assertLess(jobs.events.index("switch:vision"), jobs.events.index("provider"))
        self.assertLess(
            jobs.events.index("update:vision_processing"),
            jobs.events.index("provider"),
        )
        self.assertTrue(jobs.job["fallback_used"])

    @mock.patch(
        "src.me_finder.application.import_parser_executor.vision_config_summary"
    )
    def test_auto_fallback_failure_is_recorded_as_vision_failure(
        self,
        summary: mock.Mock,
    ) -> None:
        summary.return_value = self._provider_summary(auto=True)
        jobs = _FakeJobs()
        executor = self._executor(
            mineru=mock.Mock(side_effect=RuntimeError("temporary")),
            provider=mock.Mock(side_effect=RuntimeError("provider failed")),
        )

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "scanned"},
            True,
            jobs=jobs,
        )

        self.assertFalse(succeeded)
        self.assertEqual(jobs.job["status"], "failed")
        self.assertTrue(jobs.job["vision_failed"])
        self.assertEqual(jobs.job["fallback_error"], "provider failed")
        self.assertEqual(jobs.job["retry_provider_id"], "provider-one")

    @mock.patch(
        "src.me_finder.application.import_parser_executor.vision_config_summary"
    )
    def test_explicit_vision_failure_selects_an_alternate_provider(
        self,
        summary: mock.Mock,
    ) -> None:
        summary.return_value = self._provider_summary(auto=False)
        jobs = _FakeJobs({"provider_name": "Provider One"})
        executor = self._executor(
            provider=mock.Mock(side_effect=RuntimeError("vision failed"))
        )

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "scanned"},
            True,
            vision_provider_id="provider-one",
            jobs=jobs,
        )

        self.assertFalse(succeeded)
        self.assertTrue(jobs.job["vision_failed"])
        self.assertFalse(jobs.job["mineru_failed"])
        self.assertEqual(jobs.job["retry_provider_id"], "provider-two")
        self.assertEqual(jobs.job["original_error"], "vision failed")

    @mock.patch(
        "src.me_finder.application.import_parser_executor.vision_config_summary"
    )
    def test_cancellation_is_never_converted_to_a_failed_job(
        self,
        summary: mock.Mock,
    ) -> None:
        summary.return_value = self._provider_summary(auto=True)
        cases = (
            (
                mock.Mock(side_effect=ImportJobCancelled()),
                mock.Mock(),
                None,
            ),
            (
                mock.Mock(),
                mock.Mock(side_effect=ImportJobCancelled()),
                "provider-one",
            ),
            (
                mock.Mock(side_effect=RuntimeError("temporary")),
                mock.Mock(side_effect=ImportJobCancelled()),
                None,
            ),
        )
        for mineru, provider, provider_id in cases:
            with self.subTest(provider_id=provider_id, mineru_failure=mineru.side_effect):
                jobs = _FakeJobs({"provider_name": "Provider One"})
                executor = self._executor(mineru=mineru, provider=provider)
                with self.assertRaises(ImportJobCancelled):
                    executor.execute(
                        "import-one",
                        self.target,
                        "pdf-one",
                        {"detected_pdf_type": "scanned"},
                        True,
                        vision_provider_id=provider_id,
                        jobs=jobs,
                    )
                self.assertNotEqual(jobs.job.get("status"), "failed")

    @mock.patch(
        "src.me_finder.application.import_parser_executor.vision_config_summary",
        side_effect=VisionAPIError("invalid config"),
    )
    def test_invalid_vision_config_keeps_the_original_mineru_failure(
        self,
        _summary: mock.Mock,
    ) -> None:
        jobs = _FakeJobs()
        executor = self._executor(
            mineru=mock.Mock(
                side_effect=MinerUError(
                    "account rejected",
                    allow_parser_fallback=True,
                )
            )
        )

        succeeded = executor.execute(
            "import-one",
            self.target,
            "pdf-one",
            {"detected_pdf_type": "scanned"},
            True,
            jobs=jobs,
        )

        self.assertFalse(succeeded)
        self.assertTrue(jobs.job["mineru_failed"])
        self.assertTrue(jobs.job["needs_provider_config"])
        self.assertIn("account rejected", str(jobs.job["message"]))


if __name__ == "__main__":
    unittest.main()
