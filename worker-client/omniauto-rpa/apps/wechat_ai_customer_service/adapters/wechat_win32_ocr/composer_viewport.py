"""Frame evidence for the restricted S0 -> S1 composer crop.

Geometry is never message identity. Callers must also validate the original
Worker credential with the shared comparator; this module cannot authorize a
send and does not capture, OCR, scroll, or generate message identities.
"""
from __future__ import annotations

import hashlib
from statistics import median
from typing import Any

from PIL import Image

from ..message_viewport_projection import ordered_message_viewport_observations


def _reject(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason}


def _measured(snapshot: dict[str, Any]) -> bool:
    viewport = snapshot.get("message_viewport_bounds") or []
    return bool(
        snapshot.get("valid") and snapshot.get("executable")
        and not snapshot.get("invalidated") and len(viewport) == 4
        and any(
            anchor.get("name") == "input_separator"
            and anchor.get("source") == "current_frame_full_width_separator"
            and anchor.get("y") == viewport[3]
            for anchor in snapshot.get("anchors", [])
        )
    )


def composer_frame_scope(
    before_layout: dict[str, Any], after_layout: dict[str, Any],
    before_frame: dict[str, Any], after_frame: dict[str, Any],
) -> dict[str, Any]:
    """Check current measurements and capture identity before opting into suffixes."""
    if not all(_measured(s) for s in (before_layout, after_layout)):
        return _reject("composer_current_boundary_unconfirmed")
    for key in ("hwnd", "calibration_id", "window_rect", "client_rect",
                "client_screen_origin", "dpi_scale", "image_width", "image_height"):
        if not before_layout.get(key) or before_layout.get(key) != after_layout.get(key):
            return _reject("composer_window_identity_changed")
    for key in ("hwnd", "geometry", "dpi_scale"):
        if not before_frame.get(key) or before_frame.get(key) != after_frame.get(key):
            return _reject("composer_capture_geometry_changed")
    try:
        if (not before_frame.get("frame_id") or not after_frame.get("frame_id")
                or before_frame["frame_id"] == after_frame["frame_id"]
                or before_frame["screenshot_sha256"] == after_frame["screenshot_sha256"]
                or float(after_frame["captured_monotonic"]) <= float(before_frame["captured_monotonic"])):
            return _reject("composer_capture_timepoint_invalid")
        old, new = before_layout["message_viewport_bounds"], after_layout["message_viewport_bounds"]
        delta = old[3] - new[3]
        if old[:3] != new[:3] or delta <= 0:
            return _reject("composer_viewport_not_shortened")
        old_input, new_input = before_layout["input_bounds"], after_layout["input_bounds"]
        if (old_input[1] - new_input[1] != delta
                or [old_input[i] for i in (0, 2, 3)] != [new_input[i] for i in (0, 2, 3)]):
            return _reject("composer_input_boundary_inconsistent")
        return {"ok": True, "reason": "composer_same_transaction_frame_scope",
                "viewport_reduction_pixels": delta}
    except (KeyError, TypeError, ValueError):
        return _reject("composer_frame_evidence_unavailable")


def _rect(value: Any) -> list[float]:
    values = [value[key] for key in ("left", "top", "right", "bottom")] if isinstance(value, dict) else list(value)
    left, top, right, bottom = [float(v) for v in values[:4]]
    if right <= left or bottom <= top:
        raise ValueError("invalid_rect")
    return [left, top, right, bottom]


def _row_rect(observation: dict[str, Any]) -> list[float]:
    bounds = _rect(observation["bubble_rect"])
    avatar = (observation.get("source_message") or {}).get("avatar_alignment") or {}
    component = (avatar.get(observation.get("sender_role")) or {}).get("component_bounds")
    if component:
        other = _rect(component)
        bounds = [min(bounds[0], other[0]), min(bounds[1], other[1]),
                  max(bounds[2], other[2]), max(bounds[3], other[3])]
    return bounds


def _position_rect(observation: dict[str, Any]) -> list[float]:
    avatar = (observation.get("source_message") or {}).get("avatar_alignment") or {}
    evidence = avatar.get(observation.get("sender_role")) or {}
    if evidence.get("state") == "confirmed" and evidence.get("component_bounds"):
        return _rect(evidence["component_bounds"])
    return _rect(observation["bubble_rect"])


def _verified_image(frame: dict[str, Any]) -> Image.Image:
    with Image.open(frame["screenshot_path"]) as source:
        image = source.convert("RGB")
    if hashlib.sha256(image.tobytes()).hexdigest() != frame["screenshot_sha256"]:
        raise ValueError("frame_pixels_changed")
    return image


def composer_suffix_geometry(
    before: dict[str, Any], after: dict[str, Any],
    before_layout: dict[str, Any], after_layout: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Verify observed movement and the provenance of any cropped top objects.

    Displacement comes from the matched observations, never the reply length or
    an assumption that content moves by the input-height change. A partial old
    bubble is checked against the original pixels without restoring its text.
    """
    try:
        old = ordered_message_viewport_observations(before["observations"])
        new = ordered_message_viewport_observations(after["observations"])
        pairs = decision["matched_pairs"]
        scale = float(before_layout["dpi_scale"])
        tolerance = max(1, round(3 * scale))
        # Frame contour coordinates avoid treating OCR bounding-box jitter as
        # physical motion. These rectangles are displacement evidence only.
        matched = [(_position_rect(old[p["old_index"]]), _position_rect(new[p["new_index"]])) for p in pairs]
        if not matched:
            return _reject("composer_overlap_missing")
        displacement = round(median(a[1] - b[1] for a, b in matched))
        viewport = after_layout["message_viewport_bounds"]
        reduction = before_layout["message_viewport_bounds"][3] - viewport[3]
        if displacement <= 0 or displacement > reduction + tolerance:
            return _reject("composer_movement_not_explained_by_shrink")
        for a, b in matched:
            if any(abs(a[i] - b[i] - (displacement if i in (1, 3) else 0)) > tolerance for i in range(4)):
                return _reject("composer_rows_moved_inconsistently")
        missing = [_row_rect(item) for item in old[:pairs[0]["old_index"]]]
        fragments = after.get("top_message_fragment") or []
        readable_top = max([viewport[1], *[f["bounds"][3] for f in fragments]])
        if any(rect[3] - displacement > readable_top + tolerance for rect in missing):
            return _reject("composer_missing_row_still_visible")
        if fragments:
            images = [_verified_image(s["frame_observation"]) for s in (before, after)]
            for fragment in fragments:
                rect = [round(v) for v in _rect(fragment["bounds"])]
                source = [rect[0], rect[1] + displacement, rect[2], rect[3] + displacement]
                if not any(r[0] < source[2] and source[0] < r[2]
                           and r[1] < source[3] and source[1] < r[3] for r in missing):
                    return _reject("composer_top_fragment_source_unknown")
                if images[0].crop(tuple(source)).tobytes() != images[1].crop(tuple(rect)).tobytes():
                    return _reject("composer_top_fragment_pixels_changed")
        return {"ok": True, "reason": "composer_old_prefix_cropped",
                "observed_upward_pixels": displacement,
                "viewport_reduction_pixels": reduction,
                "omitted_row_count": len(missing), "top_fragment_count": len(fragments)}
    except (KeyError, TypeError, ValueError, OSError, IndexError):
        return _reject("composer_history_geometry_unavailable")
