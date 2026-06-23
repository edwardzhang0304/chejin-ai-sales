"""Screenshot artifact helpers for add_friend RPA."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any


def screenshot_artifact_filename(label: str, *, timestamp_ms: int | None = None) -> str:
    clean_label = sanitize_artifact_label(label or "screenshot")
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    return f"{clean_label}_{ts}.png"


def save_screenshot_artifact(
    image: Any,
    *,
    artifact_dir: str | Path | None,
    label: str,
    timestamp_ms: int | None = None,
) -> str:
    if not artifact_dir:
        return ""
    root = Path(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    saved_path = root / screenshot_artifact_filename(label, timestamp_ms=timestamp_ms)
    image.save(saved_path)
    return str(saved_path)


def screenshot_artifact_metadata(
    *,
    path: str,
    label: str,
    capture_mode: str,
    image_size: tuple[int, int] | list[int] | None = None,
    region: list[int] | tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    width = height = 0
    if image_size and len(image_size) >= 2:
        width = int(image_size[0])
        height = int(image_size[1])
    return {
        "path": str(path or ""),
        "label": sanitize_artifact_label(label or "screenshot"),
        "capture_mode": str(capture_mode or ""),
        "image_size": [width, height],
        "region": normalize_region(region),
    }


def sanitize_artifact_label(label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(label or "").strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean or "screenshot"


def normalize_region(region: list[int] | tuple[int, int, int, int] | None) -> list[int]:
    if not region or len(region) < 4:
        return []
    values = [int(value) for value in list(region)[:4]]
    left, top, right, bottom = values
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]
