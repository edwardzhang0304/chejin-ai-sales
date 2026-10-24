"""Shared bounded icon matching; callers still own action/identity guards."""
from __future__ import annotations
from typing import Any
import numpy as np

from .circle_plus_icon import _match_scores


def icon_candidates(image: Any, *, search_bounds: list[int], templates, source: str, method: str, threshold: float = .92) -> list[dict]:
    if image is None or not hasattr(image, "crop") or len(search_bounds) != 4:
        return []
    left, top, right, bottom = map(int, search_bounds)
    left, top = max(0, left), max(0, top)
    right, bottom = min(image.width, right), min(image.height, bottom)
    if right <= left or bottom <= top:
        return []
    pixels = np.asarray(image.crop((left, top, right, bottom)).convert("L"), dtype=np.float64)
    candidates = []
    for scale, template in templates:
        h, w = template.shape
        if pixels.shape[0] < h or pixels.shape[1] < w:
            continue
        scores = _match_scores(pixels, template)
        ys, xs = np.nonzero(scores >= threshold)
        for y, x in zip(ys.tolist(), xs.tolist()):
            candidates.append(dict(source=source, method=method,
                bounds=[left+x, top+y, left+x+w, top+y+h],
                point=[int(round(left+x+(w-1)/2)), int(round(top+y+(h-1)/2))],
                confidence=round(float(scores[y,x]),6), template_scale=scale))
    return deduplicate_icons(candidates)


def deduplicate_icons(candidates: list[dict]) -> list[dict]:
    result = []
    for candidate in sorted(candidates, key=lambda c:c['confidence'], reverse=True):
        x, y = candidate['point']
        if any(abs(x-other['point'][0]) <= 16 and abs(y-other['point'][1]) <= 16 for other in result):
            continue
        result.append(candidate)
        if len(result) >= 5:
            break
    return result
