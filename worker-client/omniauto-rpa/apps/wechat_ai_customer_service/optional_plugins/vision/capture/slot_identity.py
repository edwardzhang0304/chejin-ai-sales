"""Pure identity matching for a refreshed image slot."""

from __future__ import annotations

from typing import Any

from .wechat import (
    image_visual_fingerprint_distance,
    image_visual_static_content_matches,
)


IMAGE_FINGERPRINT_MAX_DISTANCE = 6
IMAGE_STABLE_SLOT_MIN_IOU = 0.85


def valid_bounds(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict):
        raw = (
            value.get("left"),
            value.get("top"),
            value.get("right"),
            value.get("bottom"),
        )
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        raw = value[:4]
    else:
        return None
    try:
        left, top, right, bottom = (float(item) for item in raw)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _bounds_iou(first: Any, second: Any) -> float:
    first_bounds = valid_bounds(first)
    second_bounds = valid_bounds(second)
    if first_bounds is None or second_bounds is None:
        return 0.0
    first_left, first_top, first_right, first_bottom = first_bounds
    second_left, second_top, second_right, second_bottom = second_bounds
    intersection_width = max(
        0.0,
        min(first_right, second_right) - max(first_left, second_left),
    )
    intersection_height = max(
        0.0,
        min(first_bottom, second_bottom) - max(first_top, second_top),
    )
    intersection_area = intersection_width * intersection_height
    first_area = (first_right - first_left) * (first_bottom - first_top)
    second_area = (second_right - second_left) * (
        second_bottom - second_top
    )
    union_area = first_area + second_area - intersection_area
    return intersection_area / union_area if union_area > 0 else 0.0


def match_image_slot(
    current_candidates: list[dict[str, Any]],
    *,
    expected_anchor: Any,
    expected_role: str,
    expected_bounds: Any = None,
) -> dict[str, Any]:
    """Prove that one refreshed candidate is the original authorized slot."""

    anchor = expected_anchor if isinstance(expected_anchor, dict) else {}
    expected_fingerprint = str(
        anchor.get("bubble_visual_fingerprint") or ""
    ).strip().lower()
    expected_role = str(expected_role or "").strip().lower()
    if not expected_fingerprint or expected_role not in {"customer", "self"}:
        return {"state": "identity_invalid", "bubble": {}}
    try:
        expected_occurrence = int(anchor.get("occurrence_index") or 0)
        expected_occurrence_count = int(anchor.get("occurrence_count") or 0)
    except (TypeError, ValueError):
        return {"state": "identity_invalid", "bubble": {}}
    if expected_occurrence_count <= 0:
        return {"state": "identity_invalid", "bubble": {}}
    expected_preceding = str(
        anchor.get("preceding_stable_message") or ""
    ).strip()
    expected_following = str(
        anchor.get("following_stable_message") or ""
    ).strip()
    fingerprint_matches: list[dict[str, Any]] = []
    role_conflicts: list[dict[str, Any]] = []
    for bubble in current_candidates:
        current_anchor = (
            bubble.get("image_physical_anchor")
            if isinstance(bubble.get("image_physical_anchor"), dict)
            else {}
        )
        current_role = str(
            current_anchor.get("sender_role") or ""
        ).strip().lower()
        current_visual_side = str(
            current_anchor.get("visual_side") or bubble.get("side") or ""
        ).strip().lower()
        fingerprint_distance = image_visual_fingerprint_distance(
            expected_fingerprint,
            current_anchor.get("bubble_visual_fingerprint"),
        )
        static_content_matches = image_visual_static_content_matches(
            expected_fingerprint,
            current_anchor.get("bubble_visual_fingerprint"),
        )
        if (
            fingerprint_distance is None
            or fingerprint_distance > IMAGE_FINGERPRINT_MAX_DISTANCE
        ):
            continue
        if current_role != expected_role:
            role_conflicts.append(
                {
                    "expected_c2_sender_role": expected_role,
                    "refreshed_c2_sender_role": (
                        current_role
                        if current_role in {"customer", "self"}
                        else "unknown"
                    ),
                    "visual_side": current_visual_side,
                    "fingerprint_distance": fingerprint_distance,
                    "static_content_matches": static_content_matches,
                }
            )
            continue
        fingerprint_matches.append(
            {
                **bubble,
                "identity_match_evidence": {
                    "expected_c2_sender_role": expected_role,
                    "refreshed_c2_sender_role": current_role,
                    "visual_side": current_visual_side,
                    "visual_side_consistent": current_visual_side == current_role,
                    "fingerprint_distance": fingerprint_distance,
                    "static_content_matches": static_content_matches,
                    "preceding_stable_message": str(
                        current_anchor.get("preceding_stable_message") or ""
                    ),
                    "following_stable_message": str(
                        current_anchor.get("following_stable_message") or ""
                    ),
                    "occurrence_index": int(
                        current_anchor.get("occurrence_index") or 0
                    ),
                    "occurrence_count": int(
                        current_anchor.get("occurrence_count") or 0
                    ),
                },
            }
        )
    occurrence_matches = []
    contextual_matches = []
    for bubble in fingerprint_matches:
        evidence = (
            bubble.get("identity_match_evidence")
            if isinstance(bubble.get("identity_match_evidence"), dict)
            else {}
        )
        if int(evidence.get("occurrence_index") or 0) != expected_occurrence:
            continue
        if int(evidence.get("occurrence_count") or 0) != expected_occurrence_count:
            continue
        occurrence_matches.append(bubble)
        current_preceding = str(
            evidence.get("preceding_stable_message") or ""
        ).strip()
        current_following = str(
            evidence.get("following_stable_message") or ""
        ).strip()
        expected_neighbors = [
            value for value in (expected_preceding, expected_following) if value
        ]
        matching_neighbor_count = sum(
            (
                bool(expected_preceding)
                and current_preceding == expected_preceding,
                bool(expected_following)
                and current_following == expected_following,
            )
        )
        explicit_neighbor_conflict = bool(
            (
                expected_preceding
                and current_preceding
                and current_preceding != expected_preceding
            )
            or (
                expected_following
                and current_following
                and current_following != expected_following
            )
        )
        evidence["explicit_neighbor_conflict"] = (
            explicit_neighbor_conflict
        )
        # A neighbour proves sequence context, not the image pixels themselves.
        # Distinct pictures can share the same perceptual dHash and slot, so a
        # v0.9.35 image action also requires the strict local thumbnail proof.
        if not bool(evidence.get("static_content_matches")):
            continue
        if expected_neighbors and matching_neighbor_count == 0:
            continue
        contextual_matches.append(bubble)
    if len(contextual_matches) == 1:
        contextual_matches[0]["identity_match_evidence"][
            "match_mode"
        ] = "stable_neighbor_context"
        return {
            "state": "matched",
            "bubble": contextual_matches[0],
            "fingerprint_match_count": len(fingerprint_matches),
            "contextual_match_count": 1,
        }
    stable_slot_matches = [
        item
        for item in occurrence_matches
        if bool(
            (item.get("identity_match_evidence") or {}).get(
                "static_content_matches"
            )
        )
        and not bool(
            (item.get("identity_match_evidence") or {}).get(
                "explicit_neighbor_conflict"
            )
        )
    ]
    if len(stable_slot_matches) == 1 and not contextual_matches:
        geometry_iou = _bounds_iou(
            expected_bounds,
            stable_slot_matches[0].get("bounds")
            or stable_slot_matches[0].get("bubble_rect"),
        )
        if geometry_iou >= IMAGE_STABLE_SLOT_MIN_IOU:
            stable_slot_matches[0]["identity_match_evidence"].update(
                {
                    "match_mode": "stable_slot_with_neighbor_ocr_drift",
                    "stable_slot_iou": round(geometry_iou, 6),
                }
            )
            return {
                "state": "matched",
                "bubble": stable_slot_matches[0],
                "fingerprint_match_count": len(fingerprint_matches),
                "contextual_match_count": 0,
                "occurrence_match_count": 1,
                "stable_slot_iou": round(geometry_iou, 6),
            }
    if not fingerprint_matches:
        if role_conflicts:
            return {
                "state": "role_mismatch",
                "bubble": {},
                "fingerprint_match_count": len(role_conflicts),
                "contextual_match_count": 0,
                "role_conflicts": role_conflicts,
            }
        return {
            "state": "not_visible",
            "bubble": {},
            "fingerprint_match_count": 0,
            "contextual_match_count": 0,
        }
    return {
        "state": "ambiguous",
        "bubble": {},
        "fingerprint_match_count": len(fingerprint_matches),
        "contextual_match_count": len(contextual_matches),
        "occurrence_match_count": len(occurrence_matches),
        "stable_slot_iou": round(
            max(
                (
                    _bounds_iou(
                        expected_bounds,
                        item.get("bounds") or item.get("bubble_rect"),
                    )
                    for item in occurrence_matches
                ),
                default=0.0,
            ),
            6,
        ),
    }


__all__ = ["match_image_slot", "valid_bounds"]
