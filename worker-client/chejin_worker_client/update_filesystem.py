from __future__ import annotations

import ntpath
import os
from pathlib import Path


def _extended_windows_path(absolute_path: str) -> str:
    """Use Win32 extended paths without depending on LongPathsEnabled.

    Only filesystem callers use this representation. Persisted update plans,
    package member names and data-directory identities keep their normal paths.
    ZIP member validation must still run before joining any member to this root.
    """
    value = absolute_path.replace("/", "\\")
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\.\\") or not ntpath.isabs(value):
        raise ValueError("An absolute filesystem path is required")
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith("\\"):
        raise ValueError("A drive or UNC share is required")
    value = ntpath.normpath(value)
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def update_filesystem_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    return Path(_extended_windows_path(os.path.abspath(path)))
