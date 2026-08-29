"""Current-frame image target selection for a Worker-approved occurrence."""

from __future__ import annotations

from typing import Any

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


def match_image_slot(
    current_candidates: list[dict[str, Any]],
    *,
    expected_anchor: Any,
    expected_role: str,
    expected_bounds: Any = None,
    expected_business_screen_order: int | None = None,
) -> dict[str, Any]:
    """Locate the approved occurrence in the latest frame.

    This function never claims that the current row is the same physical
    image seen during prepare. Old bounds, neighbours and visual fingerprints
    are deliberately ignored. The returned row supplies only the geometry for
    this click; clipboard bytes and the action receipt establish the result.
    """

    _ = expected_anchor, expected_bounds
    expected_role = str(expected_role or "").strip().lower()
    try:
        expected_order = int(expected_business_screen_order)
    except (TypeError, ValueError):
        return {"state": "action_policy_invalid", "bubble": {}}
    if expected_role not in {"customer", "self"} or expected_order < 0:
        return {"state": "action_policy_invalid", "bubble": {}}
    role_matches: list[dict[str, Any]] = []
    for bubble in current_candidates:
        current_anchor = (
            bubble.get("image_physical_anchor")
            if isinstance(bubble.get("image_physical_anchor"), dict)
            else {}
        )
        current_role = str(
            bubble.get("sender_role")
            or bubble.get("sender")
            or current_anchor.get("sender_role")
            or ""
        ).strip().lower()
        try:
            current_order = int(
                bubble.get("_current_business_screen_order")
            )
        except (TypeError, ValueError):
            continue
        if (
            current_role == expected_role
            and current_order == expected_order
        ):
            role_matches.append(
                {
                    **bubble,
                    "_current_role": current_role,
                    "_current_order": current_order,
                }
            )
    if len(role_matches) == 1:
        selected = dict(role_matches[0])
        selected.pop("_current_role", None)
        selected.pop("_current_order", None)
        selected["current_frame_selection_evidence"] = {
            "selection_policy": (
                "worker_approved_current_business_occurrence"
            ),
            "worker_planned_business_screen_order": expected_order,
            "current_business_screen_order": expected_order,
            "sender_role": expected_role,
            "physical_identity_inherited_from_prepare": False,
            "old_geometry_compared": False,
            "old_visual_fingerprint_compared": False,
        }
        return {
            "state": "matched",
            "bubble": selected,
            "current_frame_candidate_count": 1,
        }
    if current_candidates and not any(
        str(
            bubble.get("sender_role")
            or bubble.get("sender")
            or (
                bubble.get("image_physical_anchor") or {}
            ).get("sender_role")
            or ""
        ).strip().lower()
        == expected_role
        for bubble in current_candidates
        if isinstance(bubble, dict)
    ):
        return {
            "state": "role_mismatch",
            "bubble": {},
            "current_frame_candidate_count": len(current_candidates),
        }
    if not current_candidates:
        return {
            "state": "not_visible",
            "bubble": {},
            "current_frame_candidate_count": 0,
        }
    return {
        "state": "ambiguous",
        "bubble": {},
        "current_frame_candidate_count": len(role_matches),
    }


__all__ = ["match_image_slot", "valid_bounds"]
