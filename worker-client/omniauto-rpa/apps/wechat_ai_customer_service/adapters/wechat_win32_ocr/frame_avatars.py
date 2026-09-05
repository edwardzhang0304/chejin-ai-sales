"""One raw-frame avatar table shared by text, voice and image observations.

Coordinates and component references are frame-local visual evidence only.
No OCR, captures, UI actions, or durable message identities are created here.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

from .window_layout import required_region


class AvatarEvidenceError(RuntimeError):
    def __init__(self, evidence: dict[str, Any]):
        self.evidence = evidence
        super().__init__("C2_AVATAR_EVIDENCE_INVALID:" + str(evidence.get("reason", "unknown")))


# Logical-pixel visual tolerances, scaled by the valid frame layout, never by
# an OCR row. These concern candidate detection, not text continuation rules.
MIN_SIZE = 24.0
MAX_SIZE = 64.0
COLUMN_WIDTH = 96.0
# External surfaces can be white on the light-grey chat background. The old
# 18-level texture threshold erased that boundary and left only colour islands.
# Low-contrast surface extraction is still gated by full contour, size, column
# and exterior isolation below; it is not evidence of an avatar by itself.
BACKGROUND_DISTANCE = 3.0
ISOLATION_FRACTION = 0.90


def _invalid(reason: str) -> dict[str, Any]:
    return {"state": "invalid", "reason": reason, "components": [], "unresolved": []}


def avatar_table(image: Any, layout: dict[str, Any] | None) -> dict[str, Any]:
    """Cache on the original image object, not a recyclable id or pixel hash."""
    try:
        if (layout or {}).get("invalidated"):
            return _invalid("layout_invalidated")
        viewport = required_region(layout, "message_viewport_bounds")
        width, height = image.size
        scale = float((layout or {}).get("dpi_scale", 1.0))
        if not math.isfinite(scale) or scale <= 0:
            return _invalid("layout_scale_invalid")
        left, top, right, bottom = viewport
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            return _invalid("viewport_outside_raw_frame")
        key = (str((layout or {}).get("layout_snapshot_id", "")),
               str((layout or {}).get("frame_id", "")), tuple(viewport), scale)
        cached = getattr(image, "_chejin_frame_avatars", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            table = _detect(image, viewport, scale)
        except Exception as exc:
            table = _invalid("detector_exception:" + type(exc).__name__)
        table["layout_snapshot_id"] = key[0]
        setattr(image, "_chejin_frame_avatars", (key, table))
        return table
    except Exception as exc:
        return _invalid("raw_frame_or_layout_invalid:" + type(exc).__name__)


def _detect(image: Any, viewport: list[int], scale: float) -> dict[str, Any]:
    import cv2
    import numpy as np

    left, top, right, bottom = viewport
    pixels = np.asarray(image.convert("RGB"), dtype=np.int16)[top:bottom, left:right]
    # The modal coarse RGB colour is the chat background, not the border of an
    # OCR-driven crop (which can consist mostly of the green bubble itself).
    sample = pixels[::3, ::3].reshape(-1, 3)
    colours, counts = np.unique(sample // 8, axis=0, return_counts=True)
    modal = colours[int(counts.argmax())]
    background = np.median(sample[np.all(sample // 8 == modal, axis=1)], axis=0)
    mask = (np.abs(pixels - background).mean(axis=2) >= BACKGROUND_DISTANCE).astype("uint8")
    # External contours keep holes/colour islands inside an enclosing avatar.
    # No dilation/closing is permitted to join separate external objects.
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    column_width = min(COLUMN_WIDTH * scale, (right - left) * 0.18)
    columns = {"customer": [left, top, left + column_width, bottom],
               "self": [right - column_width, top, right, bottom]}
    table: dict[str, Any] = {"state": "complete", "reason": "detection_complete",
        "frame_reference": uuid.uuid4().hex, "columns": columns,
        "components": [], "unresolved": [], "excluded": [], "scale": scale}
    pad = max(1, round(2 * scale))
    for contour in contours:
        x, y, w, h = (int(v) for v in cv2.boundingRect(contour))
        bounds = [left + x, top + y, left + x + w, top + y + h]
        # Classify the *whole* object before restricting it to avatar columns.
        # A bubble crossing a column boundary is not a cropped avatar candidate.
        role = next((role for role, lane in columns.items()
                     if lane[0] <= bounds[0] and bounds[2] <= lane[2]), None)
        if role is None:
            # A concave object may contain an avatar joined to media. Its
            # rectangular hull cannot confirm either an avatar or absence.
            # Keep only the intersecting search column as unresolved evidence;
            # never promote this clipped search region to a component.
            silhouette = abs(float(cv2.contourArea(contour))) / (w*h)
            if silhouette < 0.90:
                for candidate_role, lane in columns.items():
                    intersection = [max(bounds[0], lane[0]), bounds[1],
                                    min(bounds[2], lane[2]), bounds[3]]
                    if (intersection[2]-intersection[0] >= MIN_SIZE*scale
                            and h >= MIN_SIZE*scale):
                        table["unresolved"].append({"bounds": intersection,
                            "object_bounds": bounds, "role": candidate_role,
                            "reason": "candidate_attached_to_other_object"})
            continue
        diagnostic = {"bounds": bounds, "role": role}
        if (
            w < MIN_SIZE * scale and h > MAX_SIZE * scale
            and (x == 0 or x + w == mask.shape[1])
            and y == 0 and y + h == mask.shape[0]
        ):
            # A thin line on the viewport edge spanning its full height has
            # explicit window-boundary evidence, unlike a merely large object.
            table["excluded"].append({**diagnostic, "reason": "viewport_vertical_boundary"})
            continue
        clipped = x == 0 or y == 0 or x + w == mask.shape[1] or y + h == mask.shape[0]
        if clipped and max(w, h) >= MIN_SIZE * scale and min(w, h) >= 4 * scale:
            table["unresolved"].append({**diagnostic, "reason": "candidate_clipped"})
            continue
        if w > MAX_SIZE * scale or h > MAX_SIZE * scale:
            # Size alone cannot prove noise: this may be an avatar attached
            # to another object entirely inside its column. Only a component
            # too narrow to contain an avatar can be excluded on scale here.
            if min(w, h) >= MIN_SIZE * scale:
                table["unresolved"].append({**diagnostic, "reason": "oversized_avatar_candidate"})
            else:
                table["excluded"].append({**diagnostic, "reason": "not_avatar_scale_or_shape"})
            continue
        if min(w, h) < MIN_SIZE * scale:
            table["excluded"].append({**diagnostic, "reason": "not_avatar_scale_or_shape"})
            continue
        if min(w, h) / max(w, h) < 0.75:
            table["unresolved"].append({**diagnostic, "reason": "avatar_shape_unresolved"})
            continue
        # Check an exterior ring, never the occupancy of the avatar's interior.
        # This admits white space/holes but rejects an object attached to a bubble.
        if x < pad or y < pad or x + w + pad > mask.shape[1] or y + h + pad > mask.shape[0]:
            table["unresolved"].append({**diagnostic, "reason": "exterior_boundary_clipped"})
            continue
        ring = mask[y-pad:y+h+pad, x-pad:x+w+pad].copy()
        ring[pad:pad+h, pad:pad+w] = 0
        ring_area = ring.size - w*h
        isolation = 1.0 - float(ring.sum()) / ring_area
        # A closed outer contour must span all four sides, not just the bounding
        # box of scattered inner texture. Interior fill is deliberately unused.
        silhouette = abs(float(cv2.contourArea(contour))) / (w*h)
        if isolation < ISOLATION_FRACTION or silhouette < 0.65:
            table["unresolved"].append({**diagnostic, "reason": "outer_boundary_not_confirmed"})
            continue
        table["components"].append({**diagnostic, "isolation": isolation,
            "outer_silhouette_ratio": silhouette, "state": "confirmed"})
    table["components"].sort(key=lambda c: (c["bounds"][1], c["bounds"][0]))
    for index, component in enumerate(table["components"]):
        component["component_id"] = f"avatar:{table['frame_reference']}:{index}"
    _exclude_independent_inward_objects(table, viewport)
    return table


def _exclude_independent_inward_objects(table: dict[str, Any], viewport: list[int]) -> None:
    """Finalize coarse candidates using confirmed objects from this frame only.

    The column intersection discovers candidates; only full contour bounds and
    a unique, already isolated avatar can prove an inward object independent.
    This excludes avatar evidence, never OCR content or a message type.
    """
    left, top, right, bottom = viewport
    unresolved = []
    for candidate in table["unresolved"]:
        if candidate["reason"] != "candidate_attached_to_other_object":
            unresolved.append(candidate)
            continue
        bounds = candidate["object_bounds"]
        x0, y0, x1, y1 = bounds
        if not (left < x0 < x1 < right and top < y0 < y1 < bottom):
            unresolved.append(candidate)
            continue
        supporting = []
        for avatar in table["components"]:
            ax0, ay0, ax1, ay1 = avatar["bounds"]
            if avatar["role"] != candidate["role"] or not ay0 <= y0 < ay1:
                continue
            if (candidate["role"] == "customer" and ax1 < x0
                    or candidate["role"] == "self" and x1 < ax0):
                supporting.append(avatar)
        if len(supporting) != 1:
            unresolved.append(candidate)
            continue
        table["excluded"].append({**candidate, "bounds": bounds,
            "reason": "independent_inward_object_beside_confirmed_avatar",
            "supporting_avatar_bounds": supporting[0]["bounds"]})
    table["unresolved"] = unresolved


def associate(table: dict[str, Any], bounds: list[float], role: str) -> dict[str, Any]:
    base = {"present": False, "state": "absent", "reason": "no_same_row_avatar"}
    if table.get("state") != "complete":
        return {**base, "state": "invalid", "reason": table.get("reason", "table_invalid")}
    try:
        coordinates = [float(v) for v in bounds]
    except (TypeError, ValueError):
        coordinates = []
    if len(coordinates) != 4 or not all(math.isfinite(v) for v in coordinates):
        return {**base, "state": "invalid", "reason": "row_bounds_invalid"}
    left, top, right, bottom = coordinates
    if left >= right or top >= bottom:
        return {**base, "state": "invalid", "reason": "row_bounds_invalid"}
    scale = table["scale"]
    def same_row(candidate: dict[str, Any]) -> bool:
        x0, y0, x1, y1 = candidate["bounds"]
        gap = left - x1 if role == "customer" else x0 - right
        # Preserve the existing leading/regular-row association distances.
        centres = [top, min((top + bottom)/2, top + 24*scale)]
        return (candidate["role"] == role and -20*scale <= gap <= (150 if role == "customer" else 320)*scale
                and min(abs((y0+y1)/2 - centre) for centre in centres) <= 30*scale)
    # Detection already classified these candidates as unresolved. Their
    # diagnostic reason must not downgrade that state to absence: a clipped
    # attachment can move its centre far away while still covering this row.
    # Proven noise lives in excluded, not unresolved, and is unaffected.
    pending = [c for c in table["unresolved"] if same_row(c) or (
        c["role"] == role
        and c["bounds"][1] < bottom and top < c["bounds"][3])]
    matches = [c for c in table["components"] if same_row(c)]
    if pending or len(matches) > 1:
        return {**base, "state": "ambiguous", "reason": "avatar_association_unresolved",
                "candidates": matches, "unresolved": pending}
    if not matches:
        return base
    component = matches[0]
    return {"present": True, "state": "confirmed", "reason": "independent_frame_avatar",
            "component_id": component["component_id"], "foreground_bounds": component["bounds"],
            "component_bounds": component["bounds"], "bounds": component["bounds"],
            "avatar_sized_component": True, "position_source": "frame_avatar_column",
            "isolation": component["isolation"]}


def role_details(image: Any, layout: dict[str, Any] | None, bounds: list[float]) -> dict[str, Any]:
    table = avatar_table(image, layout)
    customer = associate(table, bounds, "customer")
    own = associate(table, bounds, "self")
    invalid = any(c["state"] in {"invalid", "ambiguous"} for c in (customer, own))
    conflict = customer["present"] and own["present"]
    role = "" if invalid or conflict else ("customer" if customer["present"] else "self" if own["present"] else "")
    return {"role": role, "source": "wechat_avatar_row_structure_v2" if role else "",
            "customer": customer, "self": own, "ambiguous": bool(invalid or conflict),
            "state": "invalid" if table.get("state") != "complete" else "ambiguous" if invalid or conflict else "confirmed" if role else "absent",
            "reason": table.get("reason") if table.get("state") != "complete" else "avatar_association_unresolved" if invalid or conflict else "row_associated",
            "avatar_component_id": (customer if role == "customer" else own).get("component_id", ""),
            "frame_reference": table.get("frame_reference")}
