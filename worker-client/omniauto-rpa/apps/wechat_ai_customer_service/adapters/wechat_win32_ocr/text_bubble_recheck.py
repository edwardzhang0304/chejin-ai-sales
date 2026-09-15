"""One pixel-based observation of complete text bubbles; no historical text input."""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from PIL import Image, ImageDraw


# Measured against the incident's original PNG with the existing RapidOCR model.
# A runtime round has one transform, not a search for a matching OCR answer.
PADDING = 8
SCALE = 2
COLOR_TOLERANCE = 3


def _rect(value: Any) -> tuple[int, int, int, int]:
    if isinstance(value, dict):
        value = [value.get(key) for key in ("left", "top", "right", "bottom")]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("text_recheck_rect_missing")
    return tuple(round(float(v)) for v in value)


def locate_complete_bubbles(
    image: Image.Image,
    observations: list[dict[str, Any]],
    observation_ids: list[str],
    viewport: Any,
) -> list[dict[str, Any]]:
    """Use OCR only as a seed; find the enclosing physical background component.

    Reject background connected to the viewport edge, clipped bubbles, merged
    observations and uncertain colors. No coordinates or colors are account-specific.
    """
    import numpy as np

    bounds = _rect(viewport)
    vl, vt, vr, vb = bounds
    if not (0 <= vl < vr <= image.width and 0 <= vt < vb <= image.height):
        raise ValueError("text_recheck_viewport_invalid")
    if not observation_ids or len(set(observation_ids)) != len(observation_ids):
        raise ValueError("text_recheck_selection_invalid")
    pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    area = pixels[vt:vb, vl:vr]
    regions = []
    for observation_id in observation_ids:
        candidates = [o for o in observations if o.get("observation_id") == observation_id]
        if len(candidates) != 1:
            raise ValueError("text_recheck_observation_not_unique")
        observation = candidates[0]
        if (observation.get("message_type") != "text"
                or observation.get("row_kind") != "text_bubble"
                or observation.get("sender_role") not in {"self", "customer"}
                or observation.get("contract_errors")):
            raise ValueError("text_recheck_not_ordinary_text")
        left, top, right, bottom = _rect(observation.get("bubble_rect"))
        if not (vl < left < right < vr and vt < top < bottom < vb):
            raise ValueError("text_recheck_observation_clipped")
        colors = Counter(map(tuple, pixels[top:bottom, left:right].reshape(-1, 3)))
        color, count = colors.most_common(1)[0]
        if count < (right - left) * (bottom - top) * 0.4:
            raise ValueError("text_recheck_background_uncertain")
        mask = np.max(np.abs(area - np.asarray(color)), axis=2) <= COLOR_TOLERANCE
        seeds = np.argwhere(mask[top-vt:bottom-vt, left-vl:right-vl])
        sy, sx = seeds[0]
        component_image = Image.fromarray(mask.astype("uint8") * 255).copy()
        ImageDraw.floodfill(component_image, (int(sx + left-vl), int(sy + top-vt)), 128)
        component = np.asarray(component_image) == 128
        ys, xs = np.nonzero(component)
        x0, y0, x1, y1 = int(xs.min())+vl, int(ys.min())+vt, int(xs.max())+vl+1, int(ys.max())+vt+1
        if (x0 <= vl+2 or y0 <= vt+2 or x1 >= vr-2 or y1 >= vb-2
                or x0 > left+2 or y0 > top+2 or x1 < right-2 or y1 < bottom-2
                or int(component.sum()) < (x1-x0)*(y1-y0)*0.6):
            raise ValueError("text_recheck_complete_bubble_unproven")
        for other in observations:
            if other.get("observation_id") == observation_id:
                continue
            ol, ot, or_, ob = _rect(other.get("bubble_rect"))
            if min(x1, or_) > max(x0, ol) and min(y1, ob) > max(y0, ot):
                raise ValueError("text_recheck_bubble_contains_multiple_observations")
        crop = [max(vl, x0-PADDING), max(vt, y0-PADDING), min(vr, x1+PADDING), min(vb, y1+PADDING)]
        regions.append({"observation_id": observation_id, "bubble_rect": [x0,y0,x1,y1],
                        "crop_rect": crop, "padding": PADDING, "scale": SCALE,
                        "resample": "lanczos", "background_rgb": list(map(int, color))})
    return regions


def recognize_regions(
    image: Image.Image,
    regions: list[dict[str, Any]],
    ocr_runner: Callable[[Image.Image], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Exactly one OCR call per frozen region, mapped back to the same frame."""
    results = []
    for region in regions:
        left, top, right, bottom = region["crop_rect"]
        crop = image.crop((left, top, right, bottom))
        enlarged = crop.resize((crop.width*SCALE, crop.height*SCALE), Image.Resampling.LANCZOS)
        mapped = []
        for raw in ocr_runner(enlarged):
            item = dict(raw)
            for key in ("left", "right", "center_x"):
                item[key] = float(raw[key])/SCALE + left
            for key in ("top", "bottom", "center_y"):
                item[key] = float(raw[key])/SCALE + top
            item["box"] = [[float(x)/SCALE+left, float(y)/SCALE+top] for x,y in raw["box"]]
            x0,y0,x1,y1 = region["bubble_rect"]
            if not (x0 <= item["center_x"] <= x1 and y0 <= item["center_y"] <= y1):
                raise ValueError("text_recheck_ocr_outside_bubble")
            mapped.append(item)
        if not mapped:
            raise ValueError("text_recheck_ocr_empty")
        results.append({**region, "ocr_items": mapped})
    return results
