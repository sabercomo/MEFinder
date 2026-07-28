"""Read-only GitHub Releases update check for the macOS app.

The lightweight Mac updater never downloads or replaces the application.  It
only reports a newer release that contains a DMG for the current architecture;
the user then opens the trusted GitHub release page and installs it manually.
"""

from __future__ import annotations

import json
import platform
import re
from typing import Callable, Mapping, Optional, Tuple
from urllib.request import Request, urlopen


DEFAULT_RELEASE_API = (
    "https://api.github.com/repos/sabercomo/MEFinder/releases?per_page=20"
)
RELEASE_TAG_ROOT = "https://github.com/sabercomo/MEFinder/releases/tag"
MAX_RELEASE_BYTES = 2 * 1024 * 1024


def normalized_version(value: object) -> Optional[Tuple[int, int, int]]:
    match = re.fullmatch(r"\s*v?(\d+)\.(\d+)\.(\d+)\s*", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def is_newer_version(candidate: object, current: object) -> bool:
    candidate_version = normalized_version(candidate)
    current_version = normalized_version(current)
    if candidate_version is None or current_version is None:
        return False
    return candidate_version > current_version


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MEFinder-macOS-Update-Checker",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _read_limited(
    opener: Callable,
    url: str,
    limit: int = MAX_RELEASE_BYTES,
    timeout: int = 20,
) -> bytes:
    with opener(_request(url), timeout=timeout) as response:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("更新服务器返回的数据过大。")
    return payload


def _macos_release(
    payload: Mapping[str, object],
    architecture: str,
) -> Optional[dict[str, object]]:
    if payload.get("draft") or payload.get("prerelease"):
        return None
    version_tuple = normalized_version(payload.get("tag_name"))
    if version_tuple is None:
        return None
    version = ".".join(str(part) for part in version_tuple)
    expected_name = f"MEFinder-v{version}-macos-{architecture}.dmg"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return None
    asset_name = next(
        (
            str(item.get("name") or "")
            for item in assets
            if isinstance(item, Mapping)
            and str(item.get("name") or "").casefold() == expected_name.casefold()
        ),
        "",
    )
    if not asset_name:
        return None
    return {
        "version": version,
        "version_tuple": version_tuple,
        "release_url": f"{RELEASE_TAG_ROOT}/v{version}",
        "dmg_name": asset_name,
    }


def check_macos_update(
    current_version: str,
    *,
    architecture: Optional[str] = None,
    release_api: str = DEFAULT_RELEASE_API,
    opener: Callable = urlopen,
) -> dict[str, object]:
    """Return the newest compatible Mac DMG without downloading anything."""

    architecture = str(architecture or platform.machine() or "arm64").strip()
    base_state: dict[str, object] = {
        "status": "idle",
        "current_version": current_version,
        "latest_version": None,
        "update_available": False,
        "release_url": None,
        "dmg_name": None,
        "message": "尚未检查更新。",
    }
    try:
        raw = _read_limited(opener, release_api)
        decoded = json.loads(raw.decode("utf-8"))
        payloads = (
            [item for item in decoded if isinstance(item, Mapping)]
            if isinstance(decoded, list)
            else [decoded] if isinstance(decoded, Mapping) else []
        )
        releases = [
            release
            for release in (
                _macos_release(item, architecture) for item in payloads
            )
            if release is not None
        ]
        if not releases:
            return {
                **base_state,
                "status": "unavailable",
                "message": f"GitHub 暂无适用于 {architecture} Mac 的 DMG 更新。",
            }
        releases.sort(key=lambda item: item["version_tuple"], reverse=True)
        latest = releases[0]
        state = {
            **base_state,
            "latest_version": latest["version"],
            "release_url": latest["release_url"],
            "dmg_name": latest["dmg_name"],
        }
        if is_newer_version(latest["version"], current_version):
            return {
                **state,
                "status": "available",
                "update_available": True,
                "message": (
                    f"发现 Mac 新版本 v{latest['version']}，"
                    "请打开 Releases 下载 DMG。"
                ),
            }
        return {
            **state,
            "status": "up_to_date",
            "message": f"当前已是最新 Mac 版本 v{current_version}。",
        }
    except Exception as exc:
        return {
            **base_state,
            "status": "error",
            "message": f"检查更新失败：{exc}",
        }
