"""Backup manifest helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FileManifestEntry:
    path: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}


def file_entry(path: Path, *, relative_path: str) -> FileManifestEntry:
    data = path.read_bytes()
    return FileManifestEntry(path=relative_path.replace("\\", "/"), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def stable_digest(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]
