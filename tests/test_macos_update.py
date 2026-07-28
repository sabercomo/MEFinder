from __future__ import annotations

import io
import json
import unittest

from src.me_finder.macos_update import (
    DEFAULT_RELEASE_API,
    check_macos_update,
    is_newer_version,
    normalized_version,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class _Opener:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.requests = []

    def __call__(self, request, timeout: int):
        self.requests.append((request, timeout))
        return _Response(self.payload)


def _asset(name: str) -> dict[str, object]:
    return {
        "name": name,
        "browser_download_url": (
            "https://github.com/sabercomo/MEFinder/releases/download/test/" + name
        ),
    }


def _release(
    version: str,
    assets: list[dict[str, object]],
    *,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    return {
        "tag_name": f"v{version}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
    }


class MacOSUpdateTests(unittest.TestCase):
    def test_versions_are_strict_triplets(self) -> None:
        self.assertEqual(normalized_version(" v1.2.30 "), (1, 2, 30))
        self.assertIsNone(normalized_version("1.2"))
        self.assertIsNone(normalized_version("v1.2.3-beta"))
        self.assertTrue(is_newer_version("0.2.0", "0.1.99"))
        self.assertFalse(is_newer_version("broken", "0.1.6"))

    def test_check_ignores_newer_windows_release_and_selects_mac_dmg(self) -> None:
        opener = _Opener(
            [
                _release(
                    "0.2.0",
                    [_asset("MEFinder-v0.2.0-windows-setup.exe")],
                ),
                _release(
                    "0.1.7",
                    [_asset("MEFinder-v0.1.7-macos-arm64.dmg")],
                ),
                _release(
                    "0.1.6",
                    [_asset("MEFinder-v0.1.6-macos-arm64.dmg")],
                ),
            ]
        )

        state = check_macos_update(
            "0.1.6",
            architecture="arm64",
            opener=opener,
        )

        self.assertEqual(state["status"], "available")
        self.assertEqual(state["latest_version"], "0.1.7")
        self.assertTrue(state["update_available"])
        self.assertEqual(
            state["release_url"],
            "https://github.com/sabercomo/MEFinder/releases/tag/v0.1.7",
        )
        self.assertEqual(
            state["dmg_name"],
            "MEFinder-v0.1.7-macos-arm64.dmg",
        )
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, DEFAULT_RELEASE_API)
        self.assertEqual(
            request.get_header("User-agent"),
            "MEFinder-macOS-Update-Checker",
        )
        self.assertEqual(timeout, 20)

    def test_wrong_architecture_draft_and_mismatched_names_are_ignored(self) -> None:
        opener = _Opener(
            [
                _release(
                    "0.1.9",
                    [_asset("MEFinder-v0.1.9-macos-x86_64.dmg")],
                ),
                _release(
                    "0.1.8",
                    [_asset("MEFinder-v0.1.8-macos-arm64.dmg")],
                    draft=True,
                ),
                _release(
                    "0.1.7",
                    [_asset("MEFinder-v9.9.9-macos-arm64.dmg")],
                ),
            ]
        )

        state = check_macos_update(
            "0.1.6",
            architecture="arm64",
            opener=opener,
        )

        self.assertEqual(state["status"], "unavailable")
        self.assertFalse(state["update_available"])
        self.assertIsNone(state["release_url"])

    def test_current_or_older_mac_release_reports_up_to_date(self) -> None:
        opener = _Opener(
            [
                _release(
                    "0.1.6",
                    [_asset("MEFinder-v0.1.6-macos-arm64.dmg")],
                ),
                _release(
                    "0.1.5",
                    [_asset("MEFinder-v0.1.5-macos-arm64.dmg")],
                ),
            ]
        )

        state = check_macos_update(
            "0.1.6",
            architecture="arm64",
            opener=opener,
        )

        self.assertEqual(state["status"], "up_to_date")
        self.assertEqual(state["latest_version"], "0.1.6")
        self.assertFalse(state["update_available"])

    def test_network_or_payload_failure_is_reported_without_raising(self) -> None:
        def failing_opener(_request, timeout: int):
            raise OSError(f"offline after {timeout}s")

        state = check_macos_update(
            "0.1.6",
            architecture="arm64",
            opener=failing_opener,
        )

        self.assertEqual(state["status"], "error")
        self.assertFalse(state["update_available"])
        self.assertIn("offline", state["message"])


if __name__ == "__main__":
    unittest.main()
