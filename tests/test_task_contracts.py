from __future__ import annotations

import unittest

from src.me_finder.managed_component import ManagedComponent
from src.me_finder.tasks import TaskEvent


class _Component:
    component_id = "fixture"

    def summary(self):
        return {"supported": True}

    def perform(self, payload):
        return dict(payload)

    def diagnostics(self):
        return {"component_id": self.component_id}


class TaskContractTests(unittest.TestCase):
    def test_import_progress_and_component_progress_share_one_shape(self) -> None:
        imported = TaskEvent.from_mapping(
            "import-one",
            {
                "status": "processing",
                "phase": "parse",
                "progress": {
                    "completed_pages": [0, 1, 2],
                    "total_pages": 6,
                },
            },
            unit="pages",
        ).to_dict()
        component = TaskEvent.from_mapping(
            "component-one",
            {
                "state": "downloading",
                "operation": "install",
                "downloaded_bytes": 25,
                "total_bytes": 100,
                "download_speed_bps": 10,
                "eta_seconds": 7.5,
            },
            unit="bytes",
        ).to_dict()
        self.assertEqual(set(imported), set(component))
        self.assertEqual(imported["completed"], 3)
        self.assertEqual(imported["progress"], 0.5)
        self.assertEqual(component["progress"], 0.25)
        self.assertEqual(component["speed_per_second"], 10.0)

    def test_managed_component_protocol_is_runtime_checkable(self) -> None:
        self.assertIsInstance(_Component(), ManagedComponent)


if __name__ == "__main__":
    unittest.main()
