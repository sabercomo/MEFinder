"""Fill site/release.js from the repository's latest GitHub release.

Run by .github/workflows/deploy-site.yml before the Pages upload, so the
published page always carries the latest version, date, asset sizes and links.
Only the `released`, `date` and `sizes` fields are rewritten; `dev` and
`devTopic` stay hand-maintained in site/release.js.

Usage: python scripts/update_site_release.py [--repo owner/name] [--file site/release.js] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ASSET_PREFIX = re.compile(r"^MEFinder-v(?P<version>[^-]+)-(?P<suffix>.+)$")


def fetch_latest_release(repo: str, token: str | None) -> dict:
    """Return the JSON of GET /repos/{repo}/releases/latest."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "mefinder-site"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def release_fields(release: dict) -> tuple[str, str, dict[str, str]]:
    """Extract (version, publish date, {asset suffix: size label}) from a release."""
    version = release["tag_name"].lstrip("v")
    date = (release.get("published_at") or release["created_at"])[:10]
    sizes: dict[str, str] = {}
    for asset in release.get("assets", []):
        match = ASSET_PREFIX.match(asset["name"])
        if not match or match["version"] != version or asset["name"].endswith(".sha256.txt"):
            continue
        sizes[match["suffix"]] = f"{asset['size'] / 1048576:.1f} MB"
    return version, date, sizes


def rewrite(source: str, version: str, date: str, sizes: dict[str, str]) -> str:
    """Replace the released/date/sizes entries of the MEF_RELEASE block."""
    out, n1 = re.subn(r'(released:\s*)"[^"]*"', rf'\g<1>"{version}"', source, count=1)
    out, n2 = re.subn(r'(date:\s*)"[^"]*"', rf'\g<1>"{date}"', out, count=1)
    body = "".join(f'    "{key}": "{value}",\n' for key, value in sorted(sizes.items()))
    out, n3 = re.subn(r"(sizes:\s*\{)[^}]*(\})", lambda m: f"{m.group(1)}\n{body}  {m.group(2)}", out, count=1)
    if (n1, n2, n3) != (1, 1, 1):
        raise ValueError("site/release.js no longer has released/date/sizes fields in the expected shape")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "sabercomo/MEFinder"))
    parser.add_argument("--file", default="site/release.js", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print the result instead of writing")
    args = parser.parse_args()

    release = fetch_latest_release(args.repo, os.environ.get("GITHUB_TOKEN"))
    version, date, sizes = release_fields(release)
    if not sizes:
        print(f"latest release v{version} has no MEFinder-v{version}-* assets; leaving release.js unchanged")
        return 0
    updated = rewrite(args.file.read_text(encoding="utf-8"), version, date, sizes)
    if args.dry_run:
        sys.stdout.buffer.write(updated.encode("utf-8"))
    else:
        args.file.write_text(updated, encoding="utf-8", newline="\n")
    print(f"release.js -> v{version} ({date}), {len(sizes)} assets", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
