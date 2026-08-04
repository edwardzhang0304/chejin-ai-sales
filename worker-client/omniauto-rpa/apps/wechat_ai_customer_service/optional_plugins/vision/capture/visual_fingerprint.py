"""In-memory visual fingerprints for current-screen image verification.

The fingerprint is transaction evidence only. It never determines sender
identity, conversation ownership, or scheduler occurrence identity.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageChops, ImageOps

from ..clipboard_payload import EphemeralClipboardImage
from .wechat import clamp_bounds


MAX_DHASH_DISTANCE = 16
MAX_ASPECT_RATIO_RELATIVE_ERROR = 0.18
MAX_COLOR_GRID_AVG_DISTANCE = 48.0
MIN_CONTENT_TRIM_AREA_RATIO = 0.50
CENTER_CROP_RATIOS = (0.90, 0.80, 0.70)


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


def _fingerprint_signature(normalized: Image.Image) -> dict[str, Any]:
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


def _center_crop(image: Image.Image, x_ratio: float, y_ratio: float) -> Image.Image:
    width, height = image.size
    crop_width = max(24, min(width, int(round(width * x_ratio))))
    crop_height = max(24, min(height, int(round(height * y_ratio))))
    left = max(0, (width - crop_width) // 2)
    top = max(0, (height - crop_height) // 2)
    return image.crop((left, top, left + crop_width, top + crop_height))


def _trim_uniform_padding(image: Image.Image) -> Image.Image | None:
    width, height = image.size
    if width < 24 or height < 24:
        return None
    corners = [
        image.getpixel((0, 0)),
        image.getpixel((width - 1, 0)),
        image.getpixel((0, height - 1)),
        image.getpixel((width - 1, height - 1)),
    ]
    background = tuple(
        int(round(sum(int(pixel[channel]) for pixel in corners) / 4.0))
        for channel in range(3)
    )
    difference = ImageChops.difference(
        image,
        Image.new("RGB", image.size, background),
    ).convert("L")
    mask = difference.point(lambda value: 255 if value > 12 else 0)
    bounds = mask.getbbox()
    mask.close()
    difference.close()
    if not bounds:
        return None
    left, top, right, bottom = bounds
    content_area = max(0, right - left) * max(0, bottom - top)
    if (
        right - left < 24
        or bottom - top < 24
        or content_area / float(width * height) < MIN_CONTENT_TRIM_AREA_RATIO
        or bounds == (0, 0, width, height)
    ):
        return None
    return image.crop(bounds)


def image_fingerprint(image: Image.Image) -> dict[str, Any]:
    normalized = ImageOps.exif_transpose(image).convert("RGB")
    normalized.load()
    base = _fingerprint_signature(normalized)
    if not base:
        normalized.close()
        return {}
    variants: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add_variant(candidate: Image.Image, kind: str) -> None:
        signature = _fingerprint_signature(candidate)
        if not signature:
            return
        key = (
            signature.get("orientation"),
            round(float(signature.get("aspect_ratio") or 0.0), 4),
            int(signature.get("dhash64") or 0),
            tuple(signature.get("color_grid") or []),
        )
        if key in seen:
            return
        seen.add(key)
        variants.append({"kind": kind, **signature})

    try:
        add_variant(normalized, "full")
        for ratio in CENTER_CROP_RATIOS:
            for x_ratio, y_ratio, kind in (
                (ratio, ratio, f"center_{int(ratio * 100)}"),
                (ratio, 1.0, f"center_x_{int(ratio * 100)}"),
                (1.0, ratio, f"center_y_{int(ratio * 100)}"),
            ):
                candidate = _center_crop(normalized, x_ratio, y_ratio)
                try:
                    add_variant(candidate, kind)
                finally:
                    candidate.close()
        trimmed = _trim_uniform_padding(normalized)
        if trimmed is not None:
            try:
                add_variant(trimmed, "uniform_padding_trimmed")
            finally:
                trimmed.close()
    finally:
        normalized.close()
    return {**base, "variants": variants}


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

    def variants_by_kind(
        fingerprint: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        variants = [
            item
            for item in (fingerprint.get("variants") or [])
            if isinstance(item, dict)
        ]
        if not variants:
            variants = [{"kind": "full", **fingerprint}]
        return {
            str(item.get("kind") or "full"): item
            for item in variants
        }

    expected_by_kind = variants_by_kind(expected)
    actual_by_kind = variants_by_kind(actual)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    expected_full = expected_by_kind.get("full")
    actual_full = actual_by_kind.get("full")
    if expected_full is not None:
        for kind, actual_variant in actual_by_kind.items():
            if kind == "full" or kind.startswith("center_"):
                pairs.append((expected_full, actual_variant))
    expected_trimmed = expected_by_kind.get("uniform_padding_trimmed")
    if expected_trimmed is not None and actual_full is not None:
        pairs.append((expected_trimmed, actual_full))

    # Matching is directional: the expected screenshot thumbnail may be a
    # centered crop of the clipboard original, or may contain removable
    # padding.  Cropping both images would discard their distinguishing edges.
    for expected_variant, actual_variant in pairs:
        if str(expected_variant.get("orientation") or "") != str(
            actual_variant.get("orientation") or ""
        ):
            continue
        try:
            expected_ratio = float(
                expected_variant.get("aspect_ratio") or 0.0
            )
            actual_ratio = float(
                actual_variant.get("aspect_ratio") or 0.0
            )
        except (TypeError, ValueError):
            continue
        if expected_ratio <= 0.0 or actual_ratio <= 0.0:
            continue
        if (
            abs(expected_ratio - actual_ratio)
            / max(expected_ratio, actual_ratio)
            > MAX_ASPECT_RATIO_RELATIVE_ERROR
        ):
            continue
        if (
            _color_grid_distance(
                expected_variant.get("color_grid"),
                actual_variant.get("color_grid"),
            )
            > MAX_COLOR_GRID_AVG_DISTANCE
        ):
            continue
        try:
            if _hamming64(
                int(expected_variant.get("dhash64") or 0),
                int(actual_variant.get("dhash64") or 0),
            ) <= MAX_DHASH_DISTANCE:
                return True
        except (TypeError, ValueError):
            continue
    return False
