from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder import runtime_location


class RuntimeLocationTests(unittest.TestCase):
    def test_source_mode_uses_project_root_for_mutable_data(self) -> None:
        with mock.patch.object(runtime_location.sys, "frozen", False, create=True):
            source_root = Path(runtime_location.__file__).resolve().parents[2]
            self.assertEqual(runtime_location.app_root(), source_root)
            self.assertEqual(runtime_location.runtime_root(), source_root)
            self.assertEqual(runtime_location.installation_kind(source_root), "source")

    def test_portable_windows_keeps_runtime_beside_the_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle_root = Path(directory)
            (bundle_root / runtime_location.PORTABLE_MARKER).touch()
            with (
                mock.patch.object(runtime_location.sys, "platform", "win32"),
                mock.patch.object(runtime_location.sys, "frozen", True, create=True),
            ):
                self.assertEqual(runtime_location.runtime_root(bundle_root), bundle_root)
                self.assertEqual(
                    runtime_location.installation_kind(bundle_root),
                    "portable",
                )

    def test_installed_windows_uses_current_selected_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            bundle_root = base / "app"
            selected_root = base / "selected" / "MEFinder"
            bundle_root.mkdir()
            (bundle_root / "data_root.txt").write_text(
                str(selected_root),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(runtime_location.os.environ, {}, clear=True),
                mock.patch.object(runtime_location.sys, "platform", "win32"),
                mock.patch.object(runtime_location.sys, "frozen", True, create=True),
            ):
                self.assertEqual(
                    runtime_location.local_app_data_root(bundle_root=bundle_root),
                    selected_root.resolve(),
                )
                self.assertEqual(
                    runtime_location.runtime_root(bundle_root),
                    selected_root.resolve() / "runtime",
                )

    def test_frozen_macos_resolves_bundle_resources_and_application_support(self) -> None:
        executable = "/Applications/MEFinder.app/Contents/MacOS/MEFinder"
        home = Path("/Users/example")
        with (
            mock.patch.dict(runtime_location.os.environ, {}, clear=True),
            mock.patch.object(runtime_location.sys, "platform", "darwin"),
            mock.patch.object(runtime_location.sys, "frozen", True, create=True),
            mock.patch.object(runtime_location.sys, "executable", executable),
        ):
            bundle_root = Path(
                "/Applications/MEFinder.app/Contents/Resources"
            ).resolve()
            app_data_root = home / "Library" / "Application Support" / "MEFinder"
            self.assertEqual(runtime_location.app_root(), bundle_root)
            self.assertEqual(runtime_location.local_app_data_root(home), app_data_root)
            self.assertEqual(
                runtime_location.runtime_root(
                    bundle_root,
                    app_data_root=app_data_root,
                ),
                app_data_root / "runtime",
            )

    def test_explicit_app_data_override_is_resolved_on_every_call(self) -> None:
        first = Path("/private/tmp/mefinder-one")
        second = Path("/private/tmp/mefinder-two")
        with mock.patch.object(runtime_location.sys, "frozen", True, create=True):
            with mock.patch.dict(
                runtime_location.os.environ,
                {runtime_location.APP_DATA_ROOT_ENV: str(first)},
                clear=True,
            ):
                self.assertEqual(
                    runtime_location.runtime_root(Path("/bundle")),
                    first.resolve() / "runtime",
                )
            with mock.patch.dict(
                runtime_location.os.environ,
                {runtime_location.APP_DATA_ROOT_ENV: str(second)},
                clear=True,
            ):
                self.assertEqual(
                    runtime_location.runtime_root(Path("/bundle")),
                    second.resolve() / "runtime",
                )


if __name__ == "__main__":
    unittest.main()
