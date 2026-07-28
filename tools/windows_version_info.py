"""Generate PyInstaller Windows version metadata from the package version."""

from __future__ import annotations

import re
from pathlib import Path

from src.me_finder import __version__


def render_windows_version_info(version: str = __version__) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(version).strip())
    if match is None:
        raise ValueError("Windows file version must use numeric major.minor.patch form")
    major, minor, patch = (int(part) for part in match.groups())
    numeric = f"({major}, {minor}, {patch}, 0)"
    text = f"{major}.{minor}.{patch}"
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric},
    prodvers={numeric},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'080404B0',
        [
          StringStruct(u'CompanyName', u'sabercomo'),
          StringStruct(u'FileDescription', u'文献原句定位器'),
          StringStruct(u'FileVersion', u'{text}'),
          StringStruct(u'InternalName', u'MEFinder'),
          StringStruct(u'LegalCopyright', u'Copyright (C) 2026 sabercomo'),
          StringStruct(u'OriginalFilename', u'文献原句定位器.exe'),
          StringStruct(u'ProductName', u'MEFinder 文献原句定位器'),
          StringStruct(u'ProductVersion', u'{text}')
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [0x0804, 1200])])
  ]
)
"""


def write_windows_version_info(
    target: Path, version: str = __version__
) -> Path:
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_windows_version_info(version), encoding="utf-8")
    return destination
