from __future__ import annotations

import unittest

from src.me_finder.mineru_api import MinerUError
from src.me_finder.page_mapping_controller import PageMappingController


class FakePageMappingCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.errors: dict[str, Exception] = {}

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))
        error = self.errors.get(name)
        if error is not None:
            raise error

    def apply_manual_page_mapping(self, source_file_id, segments) -> None:
        self._record("calibrate", source_file_id, segments)

    def detect_auto_page_mapping(self, source_file_id):
        self._record("detect", source_file_id)
        return {"segments": [{"pdf_page_start": 0}]}

    def apply_live_auto_mapping(
        self,
        source_file_id,
        segments,
        auto_mapping,
        replace_manual,
    ):
        self._record(
            "apply",
            source_file_id,
            segments,
            auto_mapping,
            replace_manual,
        )
        return {"updated_pages": 3}

    def accept_auto_page_mapping(self, source_file_id):
        self._record("accept", source_file_id)
        return 2


class PageMappingControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = FakePageMappingCoordinator()
        self.controller = PageMappingController(self.coordinator)

    def test_success_responses_preserve_arguments_and_public_shape(self) -> None:
        segments = [{"pdf_page_start": 0, "citation_page_start": "1"}]
        self.assertEqual(
            self.controller.calibrate(
                {"source_id": "pdf-one", "segments": segments}
            ),
            (200, {"ok": True, "rebuilt": True}),
        )
        self.assertEqual(
            self.controller.detect({"source_id": "pdf-one"}),
            (
                200,
                {
                    "ok": True,
                    "result": {"segments": [{"pdf_page_start": 0}]},
                },
            ),
        )
        self.assertEqual(
            self.controller.apply(
                {
                    "source_id": "pdf-one",
                    "segments": segments,
                    "auto_mapping": {"confidence": 0.9},
                    "replace_manual": 1,
                }
            ),
            (200, {"ok": True, "updated": {"updated_pages": 3}}),
        )
        self.assertEqual(
            self.controller.accept({"source_id": "pdf-one"}),
            (
                200,
                {"ok": True, "segment_count": 2, "rebuilt": True},
            ),
        )
        self.assertEqual(
            self.coordinator.calls,
            [
                ("calibrate", "pdf-one", segments),
                ("detect", "pdf-one"),
                (
                    "apply",
                    "pdf-one",
                    segments,
                    {"confidence": 0.9},
                    True,
                ),
                ("accept", "pdf-one"),
            ],
        )

    def test_invalid_payloads_fail_before_coordinator_calls(self) -> None:
        invalid_calls = [
            (self.controller.calibrate, []),
            (self.controller.calibrate, {"source_id": "pdf-one", "segments": {}}),
            (self.controller.detect, {}),
            (
                self.controller.apply,
                {"source_id": "pdf-one", "auto_mapping": [1]},
            ),
            (self.controller.accept, None),
        ]
        for operation, payload in invalid_calls:
            with self.subTest(operation=operation.__name__, payload=payload):
                self.assertEqual(
                    operation(payload),
                    (400, {"error": "invalid request"}),
                )
        self.assertEqual(self.coordinator.calls, [])

    def test_calibration_error_contract_distinguishes_input_and_rebuild(self) -> None:
        self.coordinator.errors["calibrate"] = ValueError("页码段无效")
        self.assertEqual(
            self.controller.calibrate(
                {"source_id": "pdf-one", "segments": []}
            ),
            (400, {"error": "页码段无效"}),
        )
        self.coordinator.errors["calibrate"] = RuntimeError("重建失败")
        self.assertEqual(
            self.controller.calibrate(
                {"source_id": "pdf-one", "segments": []}
            ),
            (500, {"error": "校准已保存，但索引重建失败：重建失败"}),
        )

    def test_auto_mapping_errors_keep_route_specific_statuses(self) -> None:
        self.coordinator.errors["detect"] = MinerUError("配置不存在")
        self.assertEqual(
            self.controller.detect({"source_id": "pdf-one"}),
            (400, {"error": "配置不存在"}),
        )
        self.coordinator.errors["apply"] = MinerUError("手动映射冲突")
        self.assertEqual(
            self.controller.apply({"source_id": "pdf-one"}),
            (409, {"error": "手动映射冲突"}),
        )
        self.coordinator.errors["accept"] = OSError("写入失败")
        self.assertEqual(
            self.controller.accept({"source_id": "pdf-one"}),
            (
                500,
                {
                    "error": (
                        "自动映射已保存失败或索引重建失败："
                        "写入失败"
                    )
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
