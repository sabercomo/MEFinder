from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.me_finder.update_service import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateError,
    UpdateService,
    is_newer_version,
    normalized_version,
    parse_sha256_text,
    release_from_payload,
    windows_installer_assets,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class _Opener:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requests = []

    def __call__(self, request, timeout: int):
        self.requests.append((request, timeout))
        try:
            payload = self.responses[request.full_url]
        except KeyError as exc:
            raise AssertionError(f"unexpected request: {request.full_url}") from exc
        return _Response(payload)


def _asset(name: str, url: str, size: int = 0) -> dict[str, object]:
    return {"name": name, "browser_download_url": url, "size": size}


def _release(
    version: str,
    assets: list[dict[str, object]],
    *,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    return {
        "tag_name": f"v{version}",
        "name": f"MEFinder v{version}",
        "html_url": f"https://github.com/sabercomo/MEFinder/releases/tag/v{version}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
    }


class UpdateParsingTests(unittest.TestCase):
    def test_versions_are_strict_semver_triplets(self) -> None:
        self.assertEqual(normalized_version(" v1.2.30 "), (1, 2, 30))
        self.assertIsNone(normalized_version("1.2"))
        self.assertIsNone(normalized_version("v1.2.3-beta"))
        self.assertTrue(is_newer_version("0.2.0", "0.1.99"))
        self.assertFalse(is_newer_version("broken", "0.1.0"))

    def test_release_parser_ignores_unsafe_asset_names(self) -> None:
        release = release_from_payload(
            _release(
                "0.1.7",
                [
                    _asset("MEFinder-v0.1.7-windows-setup.exe", "https://github.com/setup"),
                    _asset("../escape.exe", "https://github.com/escape"),
                    _asset("", "https://github.com/empty"),
                ],
            )
        )
        self.assertEqual(release.version, "0.1.7")
        self.assertEqual([asset.name for asset in release.assets], ["MEFinder-v0.1.7-windows-setup.exe"])
        with self.assertRaises(UpdateError):
            release_from_payload(_release("0.1.8", [], prerelease=True))

    def test_installer_requires_matching_sidecar_checksum(self) -> None:
        installer = ReleaseAsset(
            "MEFinder-v0.1.7-windows-setup.exe",
            "https://github.com/setup",
        )
        checksum = ReleaseAsset(
            "MEFinder-v0.1.7-windows-setup.exe.sha256.txt",
            "https://github.com/hash",
        )
        release = ReleaseInfo("0.1.7", "v0.1.7", "", "", (installer, checksum))
        self.assertEqual(windows_installer_assets(release), (installer, checksum))

    def test_installer_asset_version_must_match_release(self) -> None:
        stale = ReleaseAsset(
            "MEFinder-v0.1.6-windows-installer.exe",
            "https://github.com/stale",
        )
        release = ReleaseInfo("0.1.7", "v0.1.7", "", "", (stale,))
        self.assertEqual(windows_installer_assets(release), (None, None))

    def test_checksum_parser_accepts_common_sha256_sidecar_format(self) -> None:
        digest = "A" * 64
        self.assertEqual(parse_sha256_text(f"{digest} *setup.exe\n"), digest.lower())
        with self.assertRaises(UpdateError):
            parse_sha256_text("not-a-checksum")


class UpdateServiceTests(unittest.TestCase):
    release_api = "https://api.github.com/repos/sabercomo/MEFinder/releases?per_page=20"

    def test_check_skips_newer_macos_only_release_and_selects_windows_release(self) -> None:
        setup_name = "MEFinder-v0.1.7-windows-setup.exe"
        payload = [
            _release("0.1.8", [_asset("MEFinder-v0.1.8-macos.dmg", "https://github.com/mac")]),
            _release(
                "0.1.7",
                [
                    _asset(setup_name, "https://github.com/setup"),
                    _asset(setup_name + ".sha256.txt", "https://github.com/hash"),
                ],
            ),
        ]
        opener = _Opener({self.release_api: json.dumps(payload).encode("utf-8")})
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UpdateService(
                "0.1.6",
                Path(temp_dir),
                install_kind="installed",
                platform="win32",
                opener=opener,
            )
            state = service.check()

        self.assertEqual(state["status"], "available")
        self.assertEqual(state["latest_version"], "0.1.7")
        self.assertTrue(state["update_available"])
        self.assertTrue(state["can_self_update"])
        self.assertEqual(opener.requests[0][0].get_header("User-agent"), "MEFinder-Windows-Updater")

    def test_check_reports_current_when_releases_have_no_windows_installer(self) -> None:
        payload = [_release("0.1.8", [_asset("MEFinder-v0.1.8-macos.dmg", "https://github.com/mac")])]
        opener = _Opener({self.release_api: json.dumps(payload).encode("utf-8")})
        with tempfile.TemporaryDirectory() as temp_dir:
            state = UpdateService(
                "0.1.6",
                Path(temp_dir),
                install_kind="installed",
                platform="win32",
                opener=opener,
            ).check()

        self.assertEqual(state["status"], "up_to_date")
        self.assertFalse(state["update_available"])
        self.assertIn("暂无可用的 Windows 安装版", state["message"])

    def test_auto_download_verifies_sha256_then_install_launches_silent_setup(self) -> None:
        setup_name = "MEFinder-v0.1.7-windows-setup.exe"
        setup_url = "https://github.com/sabercomo/MEFinder/releases/download/v0.1.7/" + setup_name
        checksum_url = setup_url + ".sha256.txt"
        setup_bytes = b"MZ-test-windows-installer"
        digest = hashlib.sha256(setup_bytes).hexdigest()
        payload = [
            _release(
                "0.1.7",
                [
                    _asset(setup_name, setup_url, len(setup_bytes)),
                    _asset(setup_name + ".sha256.txt", checksum_url),
                ],
            )
        ]
        opener = _Opener(
            {
                self.release_api: json.dumps(payload).encode("utf-8"),
                checksum_url: f"{digest} *{setup_name}\n".encode("ascii"),
                setup_url: setup_bytes,
            }
        )
        launches = []
        install_started = []

        def launcher(args, **kwargs) -> None:
            launches.append((args, kwargs))

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir)
            service = UpdateService(
                "0.1.6",
                cache,
                install_kind="installed",
                platform="win32",
                opener=opener,
                process_launcher=launcher,
                on_install_started=lambda: install_started.append(True),
            )
            ready = service.check(auto_download=True)
            target = cache / "0.1.7" / setup_name
            self.assertEqual(target.read_bytes(), setup_bytes)
            denied = service.install("wrong-token")
            self.assertEqual(service.status()["status"], "ready")
            installing = service.install(ready["install_token"])
            duplicate = service.install(ready["install_token"])

        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["downloaded"])
        self.assertEqual(denied["status"], "error")
        self.assertIn("确认", denied["message"])
        self.assertEqual(installing["status"], "installing")
        self.assertEqual(duplicate["status"], "installing")
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][0][0], str(target))
        self.assertIn("/VERYSILENT", launches[0][0])
        self.assertNotIn("/RESTARTAPPLICATIONS", launches[0][0])
        self.assertEqual(launches[0][1]["cwd"], str(target.parent))
        self.assertTrue(launches[0][1]["close_fds"])
        self.assertEqual(install_started, [True])

    def test_failed_checksum_discards_partial_installer(self) -> None:
        setup_name = "MEFinder-v0.1.7-windows-setup.exe"
        setup_url = "https://github.com/setup.exe"
        checksum_url = "https://github.com/setup.exe.sha256.txt"
        payload = [
            _release(
                "0.1.7",
                [
                    _asset(setup_name, setup_url, 3),
                    _asset(setup_name + ".sha256.txt", checksum_url),
                ],
            )
        ]
        opener = _Opener(
            {
                self.release_api: json.dumps(payload).encode("utf-8"),
                checksum_url: ("0" * 64).encode("ascii"),
                setup_url: b"bad",
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir)
            service = UpdateService(
                "0.1.6",
                cache,
                install_kind="installed",
                platform="win32",
                opener=opener,
            )
            state = service.check(auto_download=True)
            version_dir = cache / "0.1.7"
            self.assertFalse((version_dir / setup_name).exists())
            self.assertFalse((version_dir / (setup_name + ".part")).exists())

        self.assertEqual(state["status"], "error")
        self.assertIn("SHA-256 校验失败", state["message"])

    def test_install_rechecks_downloaded_file_before_launch(self) -> None:
        setup_name = "MEFinder-v0.1.7-windows-setup.exe"
        setup_url = "https://github.com/setup.exe"
        checksum_url = "https://github.com/setup.exe.sha256.txt"
        setup_bytes = b"verified-installer"
        digest = hashlib.sha256(setup_bytes).hexdigest()
        payload = [
            _release(
                "0.1.7",
                [
                    _asset(setup_name, setup_url, len(setup_bytes)),
                    _asset(setup_name + ".sha256.txt", checksum_url),
                ],
            )
        ]
        opener = _Opener(
            {
                self.release_api: json.dumps(payload).encode("utf-8"),
                checksum_url: digest.encode("ascii"),
                setup_url: setup_bytes,
            }
        )
        launcher = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UpdateService(
                "0.1.6",
                Path(temp_dir),
                install_kind="installed",
                platform="win32",
                opener=opener,
                process_launcher=launcher,
            )
            ready = service.check(auto_download=True)
            target = Path(temp_dir) / "0.1.7" / setup_name
            target.write_bytes(b"changed-after-download")
            state = service.install(ready["install_token"])

            self.assertEqual(ready["status"], "ready")
            self.assertEqual(state["status"], "error")
            self.assertIn("发生变化", state["message"])
            self.assertFalse(target.exists())
            launcher.assert_not_called()

    def test_noninstalled_and_nonwindows_builds_never_self_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            portable = UpdateService(
                "0.1.6",
                Path(temp_dir),
                install_kind="portable",
                platform="win32",
            )
            self.assertFalse(portable.status()["can_self_update"])
            self.assertEqual(portable.download()["status"], "error")

            mac = UpdateService(
                "0.1.6",
                Path(temp_dir),
                install_kind="installed",
                platform="darwin",
            )
            state = mac.check()
            self.assertEqual(state["status"], "unsupported")
            self.assertFalse(state["can_self_update"])


if __name__ == "__main__":
    unittest.main()
