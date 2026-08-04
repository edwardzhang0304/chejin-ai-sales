"""Explicit cleanup helpers for non-serializable in-memory image payloads."""

from __future__ import annotations

from typing import Any


def release_image_payload(value: Any) -> None:
    releaser = getattr(value, "release", None)
    if callable(releaser):
        releaser()
