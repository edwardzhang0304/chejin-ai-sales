"""Frame-local OCR input only. All visual decisions keep the original image."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import frame_avatars
from .window_layout import LayoutSnapshotError, required_region


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def provenance(raw: Any, layout: dict[str, Any] | None) -> dict[str, Any]:
    """Revalidate the raw frame; never trust a copied mask or another frame's table."""
    table = frame_avatars.avatar_table(raw, layout)
    if table.get("state") != "complete":
        raise frame_avatars.AvatarEvidenceError(table)
    viewport = list(required_region(layout, "message_viewport_bounds"))
    if list(raw.size) != [layout.get("image_width"), layout.get("image_height")]:
        raise frame_avatars.AvatarEvidenceError({"reason": "avatar_mask_frame_size_mismatch"})
    rectangles = []
    for component in table.get("components", []):
        if component.get("state") != "confirmed":
            continue
        bounds = component.get("component_bounds", component.get("bounds"))
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
                or any(not isinstance(x, int) or isinstance(x, bool) for x in bounds)):
            raise frame_avatars.AvatarEvidenceError({"reason": "avatar_mask_bounds_invalid"})
        l, t, r, b = bounds
        if not (viewport[0] <= l < r <= viewport[2] and viewport[1] <= t < b <= viewport[3]):
            raise frame_avatars.AvatarEvidenceError({"reason": "avatar_mask_outside_viewport"})
        rectangles.append(list(bounds))
    rectangles.sort()
    return {"method": "avatar_mask_v1", "raw_rgb_sha256": hashlib.sha256(raw.convert("RGB").tobytes()).hexdigest(),
            "image_size": list(raw.size),
            "frame_id": layout.get("frame_id"), "layout_snapshot_id": layout.get("layout_snapshot_id"),
            "viewport": viewport, "dpi_scale": layout.get("dpi_scale"),
            "rectangles": rectangles, "mask_sha256": _digest(rectangles)}


def prepare(raw: Any, layout: dict[str, Any] | None) -> tuple[Any, dict[str, Any]]:
    info = provenance(raw, layout)
    derived = raw.convert("RGB").copy()
    if info["rectangles"]:
        import numpy as np
        l, t, r, b = info["viewport"]
        pixels = np.asarray(raw.convert("RGB"), dtype=np.int16)[t:b, l:r]
        color = tuple(int(x) for x in frame_avatars._background_color(pixels))
        for bounds in info["rectangles"]:
            # PIL paste boxes are half-open, unlike ImageDraw.rectangle.
            derived.paste(color, tuple(bounds))
    return derived, info


class Rows(list):
    """Process-local provenance; JSON round trips deliberately cannot certify it."""
    def __init__(self, rows: list[dict[str, Any]], info: dict[str, Any], regions: Any):
        super().__init__(rows)
        self.provenance = {**info, "regions": regions}
        self._rows_sha256 = _digest(rows)
        self._source_sha256 = _digest(self.provenance)


def matches(raw: Any, layout: dict[str, Any] | None, rows: Any) -> bool:
    if not isinstance(rows, Rows):
        return False
    try:
        return (rows._rows_sha256 == _digest(list(rows))
                and rows._source_sha256 == _digest(rows.provenance)
                and _regions_match(layout, rows.provenance.get("regions"))
                and all(rows.provenance.get(k) == v for k, v in provenance(raw, layout).items()))
    except (ValueError, TypeError, LayoutSnapshotError, frame_avatars.AvatarEvidenceError):
        return False


def record(raw: Any, rows: list[dict[str, Any]], info: dict[str, Any], regions: Any) -> Rows:
    result = Rows(rows, info, regions)
    # Local frame review only; never register a layout or avatar table on derived.
    raw._chejin_avatar_text_ocr = dict(result.provenance)
    return result


def cache_suffix(rows: Any) -> str:
    return ":avatar_mask_v1:" + _digest(rows.provenance) if isinstance(rows, Rows) else ":unprocessed"


def saved_source(rows: Any) -> dict[str, Any]:
    """Written with the existing whole-file SHA, only by the real producer."""
    if (not isinstance(rows, Rows) or rows._rows_sha256 != _digest(list(rows))
            or rows._source_sha256 != _digest(rows.provenance)):
        raise ValueError("text_recheck_preprocessing_source_missing")
    return {"provenance": rows.provenance, "rows_sha256": rows._rows_sha256}


def _regions_match(layout: dict[str, Any], regions: Any) -> bool:
    if regions == ["full_frame"]:
        return True
    if isinstance(regions, dict) and set(regions) == {"base_source", "local_regions"}:
        return _regions_match(layout, regions["base_source"].get("regions"))
    names = ("chat_header_bounds", "message_viewport_bounds", "input_bounds")
    return (isinstance(regions, dict) and set(regions) == set(names)
            and all(regions[name] == list(required_region(layout, name)) for name in names))


def verify_saved_source(raw: Any, layout: dict[str, Any], rows: Any, saved: Any) -> dict[str, Any]:
    """After PNG/file/frame binding checks, verify stable cross-process facts.

    New layout instances have new IDs. The actual pixels, geometry, mask and
    original full/three-ROI plan must still match; local crops are separate.
    """
    if not isinstance(saved, dict) or not isinstance(saved.get("provenance"), dict):
        raise ValueError("text_recheck_preprocessing_source_missing")
    source = saved["provenance"]
    expected = provenance(raw, layout)
    stable = ("method", "raw_rgb_sha256", "image_size", "viewport", "dpi_scale", "rectangles", "mask_sha256")
    if any(source.get(key) != expected[key] for key in stable):
        raise ValueError("text_recheck_preprocessing_source_mismatch")
    if saved.get("rows_sha256") != _digest(rows):
        raise ValueError("text_recheck_preprocessing_rows_mismatch")
    regions = source.get("regions")
    if not _regions_match(layout, regions):
        raise ValueError("text_recheck_preprocessing_regions_mismatch")
    return source
