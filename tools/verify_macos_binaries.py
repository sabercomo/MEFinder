#!/usr/bin/env python3
"""Recursively verify the architecture and deployment target of a macOS .app.

Every Mach-O file inside the bundle is inspected with the platform's own
tools so the report is trustworthy on any build machine:

* ``file``  -- confirms the file really is Mach-O and reports its slices;
* ``lipo -info`` -- enumerates the architectures present in the (fat) binary;
* ``vtool -show-build`` (with an ``otool -l`` fallback) -- reads the minimum
  supported macOS version recorded in each slice's ``LC_BUILD_VERSION`` /
  ``LC_VERSION_MIN_MACOSX`` load command.

The check fails when either of the guarantees the release depends on is
violated:

1. a binary is missing the required target architecture (for an ``x86_64``
   build this rejects any arm64-only slice, which would refuse to run under
   Rosetta on an Intel Mac);
2. any slice advertises a minimum macOS version higher than the requested
   ceiling (``--max-min-version``), which would make the app refuse to launch
   on the oldest supported system.

Usage::

    python -m tools.verify_macos_binaries \
        --require-arch x86_64 --max-min-version 12.0 path/to/MEFinder.app

Exit status is ``0`` when every Mach-O file satisfies both guarantees and
non-zero otherwise, so it doubles as a build gate.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _run(command: list[str]) -> str:
    """Return the combined stdout of ``command`` (stderr folded in)."""

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )
    return result.stdout


def _is_macho(path: Path) -> bool:
    """Return ``True`` when ``file`` reports ``path`` as a Mach-O object."""

    description = _run(["file", "-b", str(path)])
    return "Mach-O" in description


def _parse_version(text: str) -> tuple[int, ...]:
    """Parse a dotted macOS version such as ``12.0`` into a comparable tuple."""

    parts: list[int] = []
    for chunk in text.strip().split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts) or (0,)


def _format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


@dataclass
class SliceInfo:
    """The deployment target discovered for one architecture slice."""

    arch: str
    min_version: tuple[int, ...] | None


@dataclass
class BinaryReport:
    """Per-file findings used both for the human report and the pass/fail gate."""

    path: Path
    archs: list[str]
    slices: list[SliceInfo]
    problems: list[str] = field(default_factory=list)


def _lipo_archs(path: Path) -> list[str]:
    """Return the architectures ``lipo -info`` reports for ``path``."""

    output = _run(["lipo", "-info", str(path)])
    # Formats:
    #   "Non-fat file: <p> is architecture: x86_64"
    #   "Architectures in the fat file: <p> are: x86_64 arm64"
    marker = "is architecture:"
    if marker in output:
        return output.split(marker, 1)[1].split()
    marker = "are:"
    if marker in output:
        return output.split(marker, 1)[1].split()
    return []


def _min_version_from_output(output: str) -> tuple[int, ...] | None:
    """Extract the first minimum-macOS version from vtool/otool -l text.

    Matches the ``minos`` field of ``LC_BUILD_VERSION`` and the ``version``
    field of the older ``LC_VERSION_MIN_MACOSX`` load command, while ignoring
    the ``sdk``, ``current version`` and ``compatibility version`` fields.
    """

    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("minos ") or line.startswith("version "):
            token = line.split()
            if len(token) >= 2 and token[1][:1].isdigit():
                return _parse_version(token[1])
    return None


def _slice_min_version(path: Path, arch: str) -> tuple[int, ...] | None:
    """Return the minimum macOS version recorded in ``path``'s ``arch`` slice.

    Queries the slice directly with ``vtool -arch <arch>`` so the answer is
    unambiguous for both thin and fat binaries, falling back to ``otool`` when
    ``vtool`` is unavailable. ``None`` means the slice carries no deployment
    target load command and therefore imposes no floor.
    """

    output = _run(["vtool", "-arch", arch, "-show-build", str(path)])
    if "minos" not in output and "version" not in output:
        # Older toolchains lack vtool (or the slice uses LC_VERSION_MIN_MACOSX
        # which some vtool builds do not print); fall back to otool -l.
        output = _run(["otool", "-arch", arch, "-l", str(path)])
    return _min_version_from_output(output)


def inspect_binary(path: Path) -> BinaryReport:
    archs = _lipo_archs(path)
    slices: list[SliceInfo] = []
    if archs:
        for arch in archs:
            slices.append(SliceInfo(arch=arch, min_version=_slice_min_version(path, arch)))
    else:
        # lipo could not classify the slices; probe the whole file once.
        output = _run(["vtool", "-show-build", str(path)])
        slices.append(SliceInfo(arch="unknown", min_version=_min_version_from_output(output)))
    return BinaryReport(path=path, archs=archs or [s.arch for s in slices], slices=slices)


def verify(
    app_path: Path,
    require_arch: str,
    max_min_version: tuple[int, ...],
    forbid_extra_arch: bool,
) -> int:
    macho_files = sorted(
        p for p in app_path.rglob("*") if p.is_file() and not p.is_symlink() and _is_macho(p)
    )
    if not macho_files:
        print(f"verify: no Mach-O files found under {app_path}", file=sys.stderr)
        return 2

    reports: list[BinaryReport] = []
    failures = 0
    for path in macho_files:
        report = inspect_binary(path)

        if require_arch and require_arch not in report.archs:
            report.problems.append(
                f"missing required architecture '{require_arch}' (has: {', '.join(report.archs) or 'unknown'})"
            )
        if forbid_extra_arch:
            extra = [a for a in report.archs if a != require_arch]
            if extra:
                report.problems.append(
                    f"unexpected extra architecture(s): {', '.join(extra)}"
                )

        for slice_info in report.slices:
            if slice_info.min_version is None:
                continue
            if slice_info.min_version > max_min_version:
                report.problems.append(
                    f"{slice_info.arch} slice targets macOS "
                    f"{_format_version(slice_info.min_version)} > "
                    f"{_format_version(max_min_version)}"
                )

        reports.append(report)
        if report.problems:
            failures += 1

    rel_base = app_path.parent
    print(f"Inspected {len(reports)} Mach-O file(s) under {app_path.name}")
    print(f"  required architecture : {require_arch or '(any)'}")
    print(f"  max minimum macOS     : {_format_version(max_min_version)}")
    print("-" * 72)
    for report in reports:
        try:
            shown = report.path.relative_to(rel_base)
        except ValueError:
            shown = report.path
        slice_desc = ", ".join(
            f"{s.arch}@{_format_version(s.min_version) if s.min_version else '-'}"
            for s in report.slices
        )
        status = "FAIL" if report.problems else "ok"
        print(f"[{status}] {shown}  [{slice_desc}]")
        for problem in report.problems:
            print(f"        -> {problem}")

    print("-" * 72)
    if failures:
        print(f"verify: FAILED — {failures} binary(ies) violate the constraints", file=sys.stderr)
        return 1
    print("verify: PASSED — all binaries satisfy architecture and version constraints")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="Path to the .app bundle (or any directory) to scan")
    parser.add_argument(
        "--require-arch",
        default="",
        help="Architecture that must be present in every Mach-O (e.g. x86_64 or arm64)",
    )
    parser.add_argument(
        "--max-min-version",
        default="12.0",
        help="Highest allowed minimum macOS version for any slice (e.g. 12.0)",
    )
    parser.add_argument(
        "--forbid-extra-arch",
        action="store_true",
        help="Fail if any binary carries architectures other than --require-arch (single-arch build)",
    )
    args = parser.parse_args(argv)

    app_path = args.app
    if app_path.suffix != ".app" and (app_path / "Contents").is_dir():
        pass  # already a bundle root without the suffix
    if not app_path.exists():
        print(f"verify: path does not exist: {app_path}", file=sys.stderr)
        return 2

    return verify(
        app_path=app_path,
        require_arch=args.require_arch,
        max_min_version=_parse_version(args.max_min_version),
        forbid_extra_arch=args.forbid_extra_arch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
