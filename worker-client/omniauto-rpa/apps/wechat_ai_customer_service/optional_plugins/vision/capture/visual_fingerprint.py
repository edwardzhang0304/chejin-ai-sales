"""In-memory visual fingerprints for current-screen image verification.

The algorithm is integrated from meta-xucong/omniauto@2318bd8
``capture/visual_collector.py``. It is intentionally isolated from the
upstream scheduler and occurrence identity: Chejin uses it only to prove that
the clipboard bitmap still matches the C2-authorized image bubble.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageOps

from ..clipboard_payload import EphemeralClipboardImage
from .wechat import clamp_bounds


MAX_DHASH_DISTANCE = 16
MAX_ASPECT_RATIO_RELATIVE_ERROR = 0.18
MAX_COLOR_GRID_AVG_DISTANCE = 48.0


def _inset_bounds(
    bounds: list[int] | tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    left, top, right, bottom = clamp_bounds(bounds, image_size)
    width = right - left
    height = bottom - top
    inset_x = max(2, int(width * 0.04))
    inset_y = max(2, int(height * 0.04))
    if width - inset_x * 2 >= 24 and height - inset_y * 2 >= 24:
        return (
            left + inset_x,
            top + inset_y,
            right - inset_x,
            bottom - inset_y,
        )
    return left, top, right, bottom


def _dhash64(image: Image.Image) -> int:
    resized = ImageOps.grayscale(image).resize(
        (9, 8),
        Image.Resampling.LANCZOS,
    )
    pixels = list(resized.getdata())
    value = 0
    for row in range(8):
        for col in range(8):
            value <<= 1
            if pixels[row * 9 + col] > pixels[row * 9 + col + 1]:
                value |= 1
    return value


def image_fingerprint(image: Image.Image) -> dict[str, Any]:
    normalized = ImageOps.exif_transpose(image).convert("RGB")
    normalized.load()
    width, height = normalized.size
    if width <= 0 or height <= 0:
        return {}
    color_grid = [
        channel
        for pixel in normalized.resize(
            (3, 3),
            Image.Resampling.LANCZOS,
        ).getdata()
        for channel in pixel[:3]
    ]
    return {
        "orientation": (
            "portrait"
            if height > width
            else "landscape"
            if width > height
            else "square"
        ),
        "aspect_ratio": float(width) / float(height),
        "dhash64": _dhash64(normalized),
        "color_grid": color_grid,
    }


def crop_fingerprint(
    screenshot: Image.Image,
    bounds: list[int] | tuple[int, int, int, int],
) -> dict[str, Any]:
    left, top, right, bottom = _inset_bounds(bounds, screenshot.size)
    crop = screenshot.crop((left, top, right, bottom))
    try:
        return image_fingerprint(crop)
    finally:
        crop.close()


def clipboard_payload_fingerprint(
    payload: EphemeralClipboardImage,
) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(bytes(payload.image_bytes))) as image:
            image.load()
            bounds = [0, 0, int(image.width), int(image.height)]
            left, top, right, bottom = _inset_bounds(bounds, image.size)
            crop = image.crop((left, top, right, bottom))
            try:
                return image_fingerprint(crop)
            finally:
                crop.close()
    except Exception:
        return {}


def _hamming64(left: int, right: int) -> int:
    return int(left ^ right).bit_count()


def _color_grid_distance(left: Any, right: Any) -> float:
    if (
        not isinstance(left, list)
        or not isinstance(right, list)
        or len(left) != len(right)
        or not left
    ):
        return float("inf")
    try:
        return sum(
            abs(int(a) - int(b))
            for a, b in zip(left, right)
        ) / float(len(left))
    except (TypeError, ValueError):
        return float("inf")


def fingerprints_match(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    if not expected or not actual:
        return False
    if str(expected.get("orientation") or "") != str(
        actual.get("orientation") or ""
    ):
        return False
    try:
        expected_ratio = float(expected.get("aspect_ratio") or 0.0)
        actual_ratio = float(actual.get("aspect_ratio") or 0.0)
    except (TypeError, ValueError):
        return False
    if expected_ratio <= 0.0 or actual_ratio <= 0.0:
        return False
    if (
        abs(expected_ratio - actual_ratio)
        / max(expected_ratio, actual_ratio)
        > MAX_ASPECT_RATIO_RELATIVE_ERROR
    ):
        return False
    if (
        _color_grid_distance(
            expected.get("color_grid"),
            actual.get("color_grid"),
        )
        > MAX_COLOR_GRID_AVG_DISTANCE
    ):
        return False
    try:
        return _hamming64(
            int(expected.get("dhash64") or 0),
            int(actual.get("dhash64") or 0),
        ) <= MAX_DHASH_DISTANCE
    except (TypeError, ValueError):
        return False
