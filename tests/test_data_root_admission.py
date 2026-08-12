from __future__ import annotations

import threading
import time
import unittest

from src.me_finder.application.data_root_admission import (
    DataRootAdmissionError,
    DataRootAdmissionGate,
)


class DataRootAdmissionGateTests(unittest.TestCase):
    def test_migration_closes_new_admission_before_draining_entered_work(
        self,
    ) -> None:
        gate = DataRootAdmissionGate()
        release_operation = threading.Event()
        operation_entered = threading.Event()
        migration_entered = threading.Event()
        migration_finished = threading.Event()
        thread_errors: list[BaseException] = []

        def hold_operation() -> None:
            try:
                with gate.operation():
                    operation_entered.set()
                    release_operation.wait()
            except BaseException as exc:
                thread_errors.append(exc)

        def migrate() -> None:
            try:
                with gate.migration():
                    migration_entered.set()
            except BaseException as exc:
                thread_errors.append(exc)
            finally:
                migration_finished.set()

        operation_thread = threading.Thread(target=hold_operation)
        migration_thread = threading.Thread(target=migrate)
        operation_thread.start()
        self.assertTrue(operation_entered.wait(timeout=1))
        migration_thread.start()

        deadline = time.monotonic() + 1
        while True:
            try:
                with gate.operation():
                    pass
            except DataRootAdmissionError as exc:
                self.assertIn("正在迁移", str(exc))
                break
            if time.monotonic() >= deadline:
                self.fail("migration did not close new admission")

        self.assertFalse(migration_entered.is_set())
        release_operation.set()
        self.assertTrue(migration_finished.wait(timeout=1))
        operation_thread.join(timeout=1)
        migration_thread.join(timeout=1)

        self.assertTrue(migration_entered.is_set())
        self.assertEqual(thread_errors, [])

    def test_failed_migration_reopens_admission(self) -> None:
        gate = DataRootAdmissionGate()

        with self.assertRaisesRegex(RuntimeError, "copy failed"):
            with gate.migration():
                raise RuntimeError("copy failed")

        with gate.operation():
            pass

    def test_successful_migration_seals_old_root_until_restart(self) -> None:
        gate = DataRootAdmissionGate()

        with gate.migration():
            pass

        with self.assertRaisesRegex(DataRootAdmissionError, "请重启应用"):
            with gate.operation():
                pass
        with self.assertRaisesRegex(DataRootAdmissionError, "请重启应用"):
            with gate.migration():
                pass

    def test_running_migration_rejects_another_migration(self) -> None:
        gate = DataRootAdmissionGate()

        with gate.migration():
            with self.assertRaisesRegex(DataRootAdmissionError, "正在迁移"):
                with gate.migration():
                    pass


if __name__ == "__main__":
    unittest.main()
