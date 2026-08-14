"""Generate PyInstaller Windows version metadata from the package version."""

from __future__ import annotations

import re
from pathlib import Path

from src.me_finder import __version__


def render_windows_version_info(
    version: str = __version__,
    *,
    file_description: str = "文献原句定位器",
    internal_name: str = "MEFinder",
    original_filename: str = "文献原句定位器.exe",
) -> str:
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
          StringStruct(u'FileDescription', u'{file_description}'),
          StringStruct(u'FileVersion', u'{text}'),
          StringStruct(u'InternalName', u'{internal_name}'),
          StringStruct(u'LegalCopyright', u'Copyright (C) 2026 sabercomo'),
          StringStruct(u'OriginalFilename', u'{original_filename}'),
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
    target: Path,
    version: str = __version__,
    **metadata: str,
) -> Path:
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_windows_version_info(version, **metadata),
        encoding="utf-8",
    )
    return destination
