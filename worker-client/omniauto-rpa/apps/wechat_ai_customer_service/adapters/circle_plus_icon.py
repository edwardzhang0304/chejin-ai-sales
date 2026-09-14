"""Match the native circle-plus appearance inside an existing layout region."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import numpy as np
from PIL import Image

from apps.wechat_ai_customer_service.adapters.add_friend_locator import normalize_bounds


# 21x21 grayscale appearance of the Windows circle-plus, including its quiet
# border. This describes the icon only; it contains no window coordinates.
# Reference: the 2026-09-09 original supplied during the September incident.
_REFERENCE_PIXELS = bytes.fromhex(
    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    "eeeeeeeeeeeeeeeee9ceceeaeeeeeeeeeeeeeeeeee"
    "eeeeeeeeeeecaf724a48484b73b3edeeeeeeeeeeee"
    "eeeeeeeed76659a1c2e1e1c2a05669dbeeeeeeeeee"
    "eeeeeed1518de6eeeeeeeeeeeee48255d8eeeeeeee"
    "eeeee65d9beeeeeeeee5e5eeeeeeee8c65eaeeeeee"
    "eeee9069eeeeeeeeee7777eeeeeeeeea5ba0eeeeee"
    "eeed53b6eeeeeeeeee8687eeeeeeeeeea362eeeeee"
    "eecc49e4eeeeeeeeee8687eeeeeeeeeed148deeeee"
    "eebc53eeeee19e9b9b58599b9b9ee1eeec4bc3eeee"
    "eebd4ceeeed57471714040717174d5eeee4bbfeeee"
    "eece48e0eeeeeeeeee8687eeeeeeeeeedd48d1eeee"
    "eeec53afeeeeeeeeee8687eeeeeeeeeeaa59edeeee"
    "eeee9163eceeeeeeee8789eeeeeeeeec5d97eeeeee"
    "eeeee04f97eeeeeeeed0d1eeeeeeee8d53e4eeeeee"
    "eeeeeeb94888e6eeeeeeeeeeeee27d49c1eeeeeeee"
    "eeeeeeeeb64d58ade6eeeee3a45451bfeeeeeeeeee"
    "eeeeeeeeeed9844d48484848518addeeeeeeeeeeee"
    "eeeeeeeeeeeeeee7c6c4c4c8eaeeeeeeeeeeeeeeee"
    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
)
_MATCH_THRESHOLD = 0.92


@lru_cache(maxsize=1)
def _templates() -> tuple[tuple[float, np.ndarray], ...]:
    reference = Image.frombytes("L", (21, 21), _REFERENCE_PIXELS)
    return tuple(
        (scale, np.asarray(reference.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64))
        for scale, size in ((0.75, 16), (1.0, 21), (1.25, 26), (1.5, 32), (2.0, 42))
    )


def _window_sums(pixels: np.ndarray, height: int, width: int) -> np.ndarray:
    integral = np.pad(pixels.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    return integral[height:, width:] - integral[:-height, width:] - integral[height:, :-width] + integral[:-height, :-width]


def _match_scores(pixels: np.ndarray, template: np.ndarray) -> np.ndarray:
    height, width = template.shape
    centered = template - template.mean()
    shape = (pixels.shape[0] + height - 1, pixels.shape[1] + width - 1)
    correlation = np.fft.irfft2(
        np.fft.rfft2(pixels, s=shape) * np.fft.rfft2(centered[::-1, ::-1], s=shape),
        s=shape,
    )[height - 1:pixels.shape[0], width - 1:pixels.shape[1]]
    sums = _window_sums(pixels, height, width)
    energy = np.maximum(0.0, _window_sums(pixels * pixels, height, width) - sums * sums / template.size)
    denominator = np.sqrt(energy * np.sum(centered * centered))
    scores = np.divide(correlation, denominator, out=np.zeros_like(correlation), where=denominator > 1e-5)
    return np.clip(scores, -1.0, 1.0)


def circle_plus_icon_candidates(
    image: Any,
    *,
    search_bounds: list[int],
) -> list[dict[str, Any]]:
    """Return observed icon centers; never expand beyond the supplied region."""

    if image is None or not hasattr(image, "crop"):
        return []
    if not isinstance(search_bounds, list) or len(search_bounds) < 4:
        return []
    left, top, right, bottom = normalize_bounds(search_bounds)
    width, height = image.size
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right <= left or bottom <= top:
        return []
    try:
        pixels = np.asarray(image.crop((left, top, right, bottom)).convert("L"), dtype=np.float64)
    except (AttributeError, TypeError, ValueError, OSError):
        return []
    candidates: list[dict[str, Any]] = []
    for scale, template in _templates():
        template_height, template_width = template.shape
        if pixels.shape[0] < template_height or pixels.shape[1] < template_width:
            continue
        scores = _match_scores(pixels, template)
        ys, xs = np.nonzero(scores >= _MATCH_THRESHOLD)
        for y, x in zip(ys.tolist(), xs.tolist()):
            candidates.append({
                "source": "vision_plus_icon",
                "method": "circle_plus_template",
                "bounds": [left + x, top + y, left + x + template_width, top + y + template_height],
                "point": [int(round(left + x + (template_width - 1) / 2)), int(round(top + y + (template_height - 1) / 2))],
                "confidence": round(float(scores[y, x]), 6),
                "template_scale": scale,
            })
    deduped: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: item["confidence"], reverse=True):
        x, y = candidate["point"]
        if any(abs(x - other["point"][0]) <= 16 and abs(y - other["point"][1]) <= 16 for other in deduped):
            continue
        deduped.append(candidate)
        if len(deduped) >= 5:
            break
    return deduped
