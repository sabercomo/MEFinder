#!/usr/bin/env python3
"""Render the Homebrew Cask definition for a MEFinder macOS release.

The in-repo tap definition lives at ``homebrew-tap/Casks/mefinder.rb``. This
script regenerates it from the built DMG artifacts in ``release/`` so the cask
always matches a published GitHub Release (asset digests are cross-checked
against the ``.sha256.txt`` sidecars). After publishing a release, run:

    .venv-macos312-arm64/bin/python scripts/update_homebrew_tap.py --version X.Y.Z

Optionally mirror the tap into a standalone GitHub tap repository (named
``homebrew-mefinder``) and commit/push there:

    .venv-macos312-arm64/bin/python scripts/update_homebrew_tap.py \
        --tap-repo ../homebrew-mefinder --push
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CASK_RELATIVE_PATH = Path("homebrew-tap") / "Casks" / "mefinder.rb"
TAP_README_RELATIVE_PATH = Path("homebrew-tap") / "README.md"
RELEASE_DIR = REPO_ROOT / "release"

CASK_TOKEN = "mefinder"
CASK_APP_NAME = "MEFinder"
CASK_DESC = "Local-first literature passage locator for PDF, DOCX and EPUB"
SUPPORTED_ARCHES = ("arm64", "x86_64")
BREW_DEPENDS_ARCH = {"arm64": ":arm64", "x86_64": ":intel"}

DMG_NAME_RE = re.compile(r"^MEFinder-v(\d+\.\d+\.\d+)-macos-(arm64|x86_64)\.dmg$")


def repo_slug_from_remote_url(url: str) -> str:
    """Extract the GitHub ``owner/repo`` slug from a git remote URL."""
    url = url.strip().rstrip("/")
    if url.startswith("git@"):
        url = url.replace(":", "/", 1)
    if url.endswith(".git"):
        url = url[: -len(".git")]
    segments = [part for part in url.split("/") if part]
    if len(segments) < 2:
        raise ValueError(f"cannot derive owner/repo from remote URL: {url!r}")
    return "/".join(segments[-2:])


def derive_repo_slug(repo_root: Path = REPO_ROOT) -> str:
    """Derive the GitHub ``owner/repo`` slug from the origin remote."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    )
    return repo_slug_from_remote_url(result.stdout)


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_sidecar_digest(sidecar: Path) -> str | None:
    """Read the digest recorded in a ``.sha256.txt`` sidecar, if present."""
    text = sidecar.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return text.split()[0].lower()


def parse_dmg_name(name: str) -> tuple[str, str]:
    """Return ``(version, arch)`` parsed from a release DMG file name."""
    match = DMG_NAME_RE.match(name)
    if match is None:
        raise ValueError(f"unrecognized DMG file name: {name!r}")
    return match.group(1), match.group(2)


def version_sort_key(version: str) -> tuple[int, ...]:
    """Sort key for dotted numeric versions such as ``0.5.3``."""
    return tuple(int(part) for part in version.split("."))


def discover_release_versions(release_dir: Path) -> list[str]:
    """List release versions that have at least one built DMG."""
    versions = set()
    for dmg in release_dir.glob("MEFinder-v*-macos-*.dmg"):
        try:
            version, _arch = parse_dmg_name(dmg.name)
        except ValueError:
            continue
        versions.add(version)
    return sorted(versions, key=version_sort_key)


def collect_arch_digests(release_dir: Path, version: str) -> dict[str, str]:
    """Compute SHA-256 digests for every built DMG of one release version."""
    digests: dict[str, str] = {}
    for dmg in sorted(release_dir.glob(f"MEFinder-v{version}-macos-*.dmg")):
        file_version, arch = parse_dmg_name(dmg.name)
        if file_version != version or arch in digests:
            continue
        digest = sha256_file(dmg)
        sidecar = dmg.with_name(dmg.name + ".sha256.txt")
        if sidecar.exists():
            recorded = read_sidecar_digest(sidecar)
            if recorded is not None and recorded != digest:
                raise ValueError(
                    f"sidecar digest mismatch for {dmg.name}: "
                    f"file={digest} sidecar={recorded}"
                )
        digests[arch] = digest
    if not digests:
        raise ValueError(f"no built DMG artifacts found for version {version!r}")
    return digests


def render_cask(version: str, arch_sha256: dict[str, str], repo_slug: str) -> str:
    """Render the complete cask DSL for one release version.

    Dual-architecture releases use the ``arch`` stanza with ``#{version}`` /
    ``#{arch}`` interpolation; single-architecture releases hard-code the URL
    and declare ``depends_on arch`` so incompatible hosts fail fast.
    """
    unknown = sorted(set(arch_sha256) - set(SUPPORTED_ARCHES))
    if unknown:
        raise ValueError(f"unsupported architectures: {unknown}")
    ordered = {arch: arch_sha256[arch] for arch in SUPPORTED_ARCHES if arch in arch_sha256}
    if not ordered:
        raise ValueError("at least one architecture digest is required")

    homepage = f"https://github.com/{repo_slug}"
    download_base = f"{homepage}/releases/download/v#{{version}}"
    lines = [f'cask "{CASK_TOKEN}" do']

    if len(ordered) == 2:
        lines.append('  arch arm: "arm64", intel: "x86_64"')
        lines.append("")
        lines.append(f'  version "{version}"')
        lines.append(f'  sha256 arm:   "{ordered["arm64"]}",')
        lines.append(f'         intel: "{ordered["x86_64"]}"')
        lines.append("")
        lines.append(
            f'  url "{download_base}/MEFinder-v#{{version}}-macos-#{{arch}}.dmg"'
        )
    else:
        arch, digest = next(iter(ordered.items()))
        lines.append(f'  version "{version}"')
        lines.append(f'  sha256 "{digest}"')
        lines.append("")
        lines.append(f'  url "{download_base}/MEFinder-v#{{version}}-macos-{arch}.dmg"')

    lines.append(f'  name "{CASK_APP_NAME}"')
    lines.append(f'  desc "{CASK_DESC}"')
    lines.append(f'  homepage "{homepage}"')
    lines.append("")
    lines.append("  livecheck do")
    lines.append(f'    url "{homepage}/releases/latest"')
    lines.append("    strategy :github_latest")
    lines.append("  end")
    lines.append("")

    if len(ordered) == 1:
        arch = next(iter(ordered))
        lines.append(f"  depends_on arch: {BREW_DEPENDS_ARCH[arch]}")
        lines.append("")

    lines.append(f'  app "{CASK_APP_NAME}.app"')
    lines.append("end")
    return "\n".join(lines) + "\n"


def _git(tap_repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(tap_repo), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def _has_staged_changes(tap_repo: Path) -> bool:
    """Return True when the tap repo has staged (content-changed) files."""
    result = subprocess.run(
        ["git", "-C", str(tap_repo), "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    return result.returncode != 0


def sync_tap_repo(tap_repo: Path, version: str, push: bool) -> None:
    """Copy the rendered tap content into a standalone tap repo and commit."""
    if not (tap_repo / ".git").exists():
        raise ValueError(f"{tap_repo} is not a git repository (clone it first)")
    (tap_repo / "Casks").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / CASK_RELATIVE_PATH, tap_repo / "Casks" / "mefinder.rb")
    shutil.copy2(REPO_ROOT / TAP_README_RELATIVE_PATH, tap_repo / "README.md")

    _git(tap_repo, "add", "Casks/mefinder.rb", "README.md")
    if not _has_staged_changes(tap_repo):
        print(f"tap repo already up to date: {tap_repo}")
        return
    _git(tap_repo, "commit", "-m", f"cask: MEFinder v{version}")
    print(f"committed tap update in {tap_repo}")
    if push:
        _git(tap_repo, "push")
        print("pushed tap update")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the cask renderer CLI."""
    parser = argparse.ArgumentParser(
        description="Render homebrew-tap/Casks/mefinder.rb from built DMG artifacts."
    )
    parser.add_argument(
        "--version",
        help="release version to render (default: newest built DMG in release/)",
    )
    parser.add_argument("--release-dir", type=Path, default=RELEASE_DIR)
    parser.add_argument(
        "--repo-slug",
        help="GitHub owner/repo (default: derived from the origin remote)",
    )
    parser.add_argument(
        "--tap-repo",
        type=Path,
        help="standalone tap repository to mirror the cask into",
    )
    parser.add_argument(
        "--push", action="store_true", help="push the tap repository after committing"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the cask instead of writing files"
    )
    args = parser.parse_args(argv)

    versions = discover_release_versions(args.release_dir)
    version = args.version
    if version is None:
        if not versions:
            parser.error(f"no built DMG artifacts found in {args.release_dir}")
        version = versions[-1]
        print(
            f"warning: defaulting to newest built version {version}; "
            "confirm it is actually published on GitHub Releases "
            "(local builds of unreleased versions also live in release/)"
        )
    if version not in versions:
        raise SystemExit(
            f"version {version!r} has no built DMG in {args.release_dir}; found: {versions}"
        )

    repo_slug = args.repo_slug or derive_repo_slug()
    arch_sha256 = collect_arch_digests(args.release_dir, version)
    cask_text = render_cask(version, arch_sha256, repo_slug)

    if args.dry_run:
        sys.stdout.write(cask_text)
        return 0

    cask_path = REPO_ROOT / CASK_RELATIVE_PATH
    cask_path.parent.mkdir(parents=True, exist_ok=True)
    cask_path.write_text(cask_text, encoding="utf-8")
    print(f"wrote {cask_path} (v{version}, arches: {sorted(arch_sha256)})")

    if args.tap_repo is not None:
        sync_tap_repo(args.tap_repo, version, args.push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
