"""Create a standards-compliant ZIP with one top-level portable-app folder."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def create_portable_zip(source: Path, target: Path) -> None:
    source = Path(source).resolve()
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            arcname = (Path(source.name) / relative).as_posix()
            archive.write(path, arcname)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    create_portable_zip(args.source, args.target)
    print(args.target)


if __name__ == "__main__":
    main()
