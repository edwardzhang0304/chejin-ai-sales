"""Small in-memory perceptual fingerprint helper for the optional plugin."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageOps


def perceptual_dhash(image_bytes: bytes) -> str:
    """Return a 64-bit difference hash; image bytes never leave this function."""

    if not isinstance(image_bytes, (bytes, bytearray, memoryview)) or not image_bytes:
        raise ValueError("vehicle_image_bytes_missing")
    with Image.open(io.BytesIO(bytes(image_bytes))) as image:
        normalized = ImageOps.exif_transpose(image).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(normalized.getdata())
    bits = 0
    for row in range(8):
        start = row * 9
        for column in range(8):
            bits = (bits << 1) | int(pixels[start + column] > pixels[start + column + 1])
    return f"{bits:016x}"
