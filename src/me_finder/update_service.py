"""Secure GitHub Releases updater for the installed Windows build."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import ssl
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen


DEFAULT_RELEASE_API = "https://api.github.com/repos/sabercomo/MEFinder/releases?per_page=20"
MAX_RELEASE_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 16 * 1024
MAX_INSTALLER_BYTES = 512 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int = 0


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag_name: str
    page_url: str
    name: str
    assets: Tuple[ReleaseAsset, ...]


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


def release_from_payload(payload: Mapping[str, object]) -> ReleaseInfo:
    if payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("最新发布仍是草稿或预发布版本。")
    tag_name = str(payload.get("tag_name") or "").strip()
    parsed = normalized_version(tag_name)
    if parsed is None:
        raise UpdateError("GitHub 发布版本号格式无效。")
    version = ".".join(str(part) for part in parsed)
    assets = []
    raw_assets = payload.get("assets")
    if isinstance(raw_assets, list):
        for item in raw_assets:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            url = str(item.get("browser_download_url") or "").strip()
            if not name or not url or Path(name).name != name:
                continue
            try:
                size = max(int(item.get("size") or 0), 0)
            except (TypeError, ValueError):
                size = 0
            assets.append(ReleaseAsset(name=name, url=url, size=size))
    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        page_url=str(payload.get("html_url") or "").strip(),
        name=str(payload.get("name") or tag_name).strip(),
        assets=tuple(assets),
    )


def windows_installer_assets(
    release: ReleaseInfo,
) -> Tuple[Optional[ReleaseAsset], Optional[ReleaseAsset]]:
    exact = f"MEFinder-v{release.version}-windows-setup.exe".lower()
    installer = next((asset for asset in release.assets if asset.name.lower() == exact), None)
    if installer is None:
        pattern = re.compile(
            rf"^MEFinder-v{re.escape(release.version)}-windows-(?:setup|installer)\.exe$",
            re.I,
        )
        installer = next((asset for asset in release.assets if pattern.match(asset.name)), None)
    if installer is None:
        return None, None
    checksum_names = {
        (installer.name + ".sha256.txt").lower(),
        (installer.name + ".sha256").lower(),
    }
    checksum = next(
        (asset for asset in release.assets if asset.name.lower() in checksum_names),
        None,
    )
    return installer, checksum


def parse_sha256_text(value: str) -> str:
    match = re.search(r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", value)
    if not match:
        raise UpdateError("发布包的 SHA-256 校验文件格式无效。")
    return match.group(0).lower()


def _trusted_download_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in {
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MEFinder-Windows-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _direct_urlopen(request: Request, timeout: int):
    """Open trusted HTTPS without environment/system proxy settings."""

    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
    )
    return opener.open(request, timeout=timeout)


def _open_request(
    opener: Callable,
    url: str,
    timeout: int,
    direct_opener: Optional[Callable] = None,
):
    try:
        return opener(_request(url), timeout=timeout)
    except URLError as exc:
        # Some local Windows proxy clients accept Schannel/Go TLS but reject
        # Python OpenSSL during CONNECT. Retry the same allow-listed GitHub URL
        # directly with normal certificate verification; never bypass HTTPS.
        if (
            direct_opener is None
            or not _trusted_download_url(url)
            or not isinstance(getattr(exc, "reason", None), ssl.SSLError)
        ):
            raise
        return direct_opener(_request(url), timeout=timeout)


def _read_limited(
    opener: Callable,
    url: str,
    limit: int,
    timeout: int = 20,
    direct_opener: Optional[Callable] = None,
) -> bytes:
    with _open_request(opener, url, timeout, direct_opener) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("更新服务器返回的数据过大。")
    return data


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class UpdateService:
    """Check, download, verify and launch a Windows installer update."""

    def __init__(
        self,
        current_version: str,
        cache_dir: Path,
        *,
        install_kind: str,
        platform: str = sys.platform,
        release_api: str = DEFAULT_RELEASE_API,
        opener: Callable = urlopen,
        direct_opener: Optional[Callable] = _direct_urlopen,
        process_launcher: Callable = subprocess.Popen,
        on_install_started: Optional[Callable[[], None]] = None,
    ) -> None:
        self.current_version = current_version
        self.cache_dir = Path(cache_dir)
        self.install_kind = install_kind
        self.platform = platform
        self.release_api = release_api
        self.opener = opener
        self.direct_opener = direct_opener
        self.process_launcher = process_launcher
        self.on_install_started = on_install_started
        self._lock = threading.RLock()
        self._release: Optional[ReleaseInfo] = None
        self._installer: Optional[ReleaseAsset] = None
        self._checksum: Optional[ReleaseAsset] = None
        self._downloaded_path: Optional[Path] = None
        self._downloaded_sha256: Optional[str] = None
        self._state: Dict[str, object] = {
            "status": "idle",
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "can_self_update": platform == "win32" and install_kind == "installed",
            "install_kind": install_kind,
            "message": "尚未检查更新。",
            "release_url": None,
            "downloaded": False,
            "install_token": None,
        }

    def status(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._state)

    def _update_state(self, **values: object) -> Dict[str, object]:
        with self._lock:
            self._state.update(values)
            return dict(self._state)

    def check(self, *, auto_download: bool = False) -> Dict[str, object]:
        if self.platform != "win32":
            return self._update_state(
                status="unsupported",
                message="此更新入口仅用于 Windows 安装版。",
            )
        self._update_state(
            status="checking",
            message="正在检查 GitHub Releases…",
            install_token=None,
        )
        try:
            raw = _read_limited(
                self.opener,
                self.release_api,
                MAX_RELEASE_BYTES,
                direct_opener=self.direct_opener,
            )
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, Mapping):
                payloads = [payload]
            elif isinstance(payload, list):
                payloads = [item for item in payload if isinstance(item, Mapping)]
            else:
                raise UpdateError("更新服务器返回了无效数据。")
            releases = []
            for item in payloads:
                try:
                    releases.append(release_from_payload(item))
                except UpdateError:
                    continue
            if not releases:
                raise UpdateError("未找到可用的正式发布版本。")
            releases.sort(key=lambda item: normalized_version(item.version) or (0, 0, 0), reverse=True)
            windows_releases = []
            for item in releases:
                installer, checksum = windows_installer_assets(item)
                if installer is not None:
                    windows_releases.append((item, installer, checksum))
            if not windows_releases:
                newest = releases[0]
                return self._update_state(
                    status="up_to_date",
                    latest_version=self.current_version,
                    update_available=False,
                    release_url=newest.page_url,
                    downloaded=False,
                    message="GitHub 暂无可用的 Windows 安装版更新。",
                )
            release, installer, checksum = windows_releases[0]
            with self._lock:
                self._release = release
                self._installer = installer
                self._checksum = checksum
                self._downloaded_path = None
                self._downloaded_sha256 = None
            if not is_newer_version(release.version, self.current_version):
                return self._update_state(
                    status="up_to_date",
                    latest_version=release.version,
                    update_available=False,
                    release_url=release.page_url,
                    downloaded=False,
                    message=f"当前已是最新版本 v{self.current_version}。",
                )
            message = f"发现新版本 v{release.version}。"
            if installer is None:
                message += "该发布未提供 Windows 安装版，请打开发布页手动下载。"
            elif checksum is None:
                message += "缺少 SHA-256 校验文件，已禁止应用内安装。"
            elif self.install_kind != "installed":
                message += "当前不是安装版，请从发布页下载安装版。"
            state = self._update_state(
                status="available",
                latest_version=release.version,
                update_available=True,
                release_url=release.page_url,
                downloaded=False,
                message=message,
            )
            if auto_download and state.get("can_self_update") and installer and checksum:
                return self.download()
            return state
        except Exception as exc:
            message = str(exc) if isinstance(exc, UpdateError) else f"检查更新失败：{exc}"
            return self._update_state(status="error", message=message)

    def _download_file(self, asset: ReleaseAsset, destination: Path) -> str:
        if not _trusted_download_url(asset.url):
            raise UpdateError("更新文件地址不是受信任的 GitHub HTTPS 地址。")
        maximum = min(
            max(asset.size + 1024 * 1024, 8 * 1024 * 1024),
            MAX_INSTALLER_BYTES,
        ) if asset.size else MAX_INSTALLER_BYTES
        digest = hashlib.sha256()
        written = 0
        with _open_request(
            self.opener,
            asset.url,
            60,
            self.direct_opener,
        ) as response, destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > maximum:
                    raise UpdateError("Windows 安装包超过允许的大小。")
                digest.update(chunk)
                handle.write(chunk)
        if asset.size and written != asset.size:
            raise UpdateError("Windows 安装包下载不完整。")
        return digest.hexdigest()

    def download(self) -> Dict[str, object]:
        with self._lock:
            release = self._release
            installer = self._installer
            checksum = self._checksum
            can_self_update = bool(self._state.get("can_self_update"))
        if not can_self_update:
            return self._update_state(
                status="error",
                message="只有 Windows 安装版支持应用内下载与更新。",
            )
        if release is None or installer is None or checksum is None:
            return self._update_state(
                status="error",
                message="请先检查更新；安装包与 SHA-256 校验文件必须同时存在。",
            )
        if not _trusted_download_url(checksum.url):
            return self._update_state(status="error", message="校验文件地址不受信任。")

        self._update_state(status="downloading", message=f"正在下载 v{release.version}…")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        version_dir = self.cache_dir / release.version
        version_dir.mkdir(parents=True, exist_ok=True)
        target = version_dir / installer.name
        partial = target.with_suffix(target.suffix + ".part")
        try:
            checksum_text = _read_limited(
                self.opener,
                checksum.url,
                MAX_CHECKSUM_BYTES,
                direct_opener=self.direct_opener,
            ).decode("ascii", errors="replace")
            expected = parse_sha256_text(checksum_text)
            if target.is_file() and _sha256_file(target) == expected:
                actual = expected
            else:
                partial.unlink(missing_ok=True)
                actual = self._download_file(installer, partial)
                if actual != expected:
                    raise UpdateError("安装包 SHA-256 校验失败，文件已丢弃。")
                partial.replace(target)
            with self._lock:
                self._downloaded_path = target
                self._downloaded_sha256 = expected
            return self._update_state(
                status="ready",
                downloaded=True,
                downloaded_file=target.name,
                install_token=secrets.token_urlsafe(24),
                message=f"v{release.version} 已下载并通过 SHA-256 校验，可立即安装。",
            )
        except Exception as exc:
            partial.unlink(missing_ok=True)
            message = str(exc) if isinstance(exc, UpdateError) else f"下载更新失败：{exc}"
            return self._update_state(
                status="error",
                downloaded=False,
                install_token=None,
                message=message,
            )

    def install(self, confirm_token: object = None) -> Dict[str, object]:
        with self._lock:
            target = self._downloaded_path
            expected_sha256 = self._downloaded_sha256
            status = self._state.get("status")
            if status == "installing":
                return dict(self._state)
            if (
                status != "ready"
                or target is None
                or expected_sha256 is None
                or not target.is_file()
            ):
                state = dict(self._state)
                state.update(status="error", message="尚无已验证的更新可安装。")
                return state
            expected_token = self._state.get("install_token")
            supplied_token = str(confirm_token or "")
            if not isinstance(expected_token, str) or not secrets.compare_digest(
                supplied_token, expected_token
            ):
                state = dict(self._state)
                state.update(status="error", message="安装确认已失效，请重新点击安装。")
                return state
            # Consume the one-time token and leave the ready state atomically so
            # concurrent requests cannot launch multiple installer processes.
            self._state.update(
                status="installing",
                install_token=None,
                message="正在重新校验安装包并启动安装程序…",
            )
        try:
            if _sha256_file(target) != expected_sha256:
                target.unlink(missing_ok=True)
                with self._lock:
                    self._downloaded_path = None
                    self._downloaded_sha256 = None
                return self._update_state(
                    status="error",
                    downloaded=False,
                    install_token=None,
                    message="安装包在下载后发生变化，已删除；请重新下载更新。",
                )
        except OSError as exc:
            return self._update_state(
                status="error",
                downloaded=False,
                install_token=None,
                message=f"重新校验安装包失败：{exc}",
            )
        args: Iterable[str] = (
            str(target),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
        )
        try:
            self.process_launcher(list(args), cwd=str(target.parent), close_fds=True)
        except Exception as exc:
            return self._update_state(
                status="error",
                install_token=None,
                message=f"无法启动更新安装程序：{exc}",
            )
        state = self._update_state(
            status="installing",
            message="安装程序已启动，MEFinder 即将关闭并完成更新。",
        )
        if self.on_install_started is not None:
            self.on_install_started()
        return state
