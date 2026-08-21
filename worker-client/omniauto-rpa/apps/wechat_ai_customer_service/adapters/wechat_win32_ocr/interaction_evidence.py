"""Pure evidence checks for Win32/OCR interaction points."""

from __future__ import annotations

from typing import Any


_INPUT_REGION_REASONS = {
    "input_region_blank",
    "ocr_or_dark_pixels",
    "input_region_soft_blank_noise",
    "input_region_soft_blank_after_clear",
}


def _normalized_bounds(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        left, top, right, bottom = [int(value[index]) for index in range(4)]
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def input_surface_click_evidence(input_region: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a freshly observed input region before any input-area click."""

    state = input_region if isinstance(input_region, dict) else {}
    if not state:
        return {"ok": False, "reason": "input_click_evidence_missing"}
    if state.get("error"):
        return {"ok": False, "reason": "input_click_evidence_probe_failed"}
    reason = str(state.get("reason") or "")
    if reason not in _INPUT_REGION_REASONS:
        return {
            "ok": False,
            "reason": "input_click_evidence_unrecognized_region",
            "input_region_reason": reason,
        }
    # Draft detection intentionally uses a narrower text-only region.  Input
    # focus still belongs to the full calibrated click surface when supplied.
    bounds = _normalized_bounds(state.get("click_bounds") or state.get("bounds"))
    if bounds is None:
        return {
            "ok": False,
            "reason": "input_click_evidence_bounds_missing",
            "input_region_reason": reason,
        }
    left, top, right, bottom = bounds
    if right - left < 120 or bottom - top < 32:
        return {"ok": False, "reason": "input_click_evidence_bounds_too_small", "bounds": bounds}
    inset_x = max(12, min(36, (right - left) // 8))
    inset_y = max(7, min(18, (bottom - top) // 6))
    click_bounds = [left + inset_x, top + inset_y, right - inset_x, bottom - inset_y]
    if click_bounds[2] <= click_bounds[0] or click_bounds[3] <= click_bounds[1]:
        return {"ok": False, "reason": "input_click_evidence_no_safe_interior", "bounds": bounds}
    return {
        "ok": True,
        "reason": "fresh_input_region_observed",
        "source_reason": reason,
        "bounds": bounds,
        "text_bounds": _normalized_bounds(state.get("bounds")),
        "click_bounds": click_bounds,
    }


def choose_input_click_point(
    evidence: dict[str, Any] | None,
    *,
    random_module: Any,
) -> dict[str, Any]:
    """Choose one center-biased point inside the verified input surface."""

    proof = evidence if isinstance(evidence, dict) else {}
    if proof.get("ok") is not True:
        return {"ok": False, "reason": "input_click_evidence_missing"}
    bounds = _normalized_bounds(proof.get("click_bounds"))
    if bounds is None:
        return {"ok": False, "reason": "input_click_evidence_bounds_missing"}
    left, top, right, bottom = bounds
    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    x = int(round(random_module.triangular(left, right - 1, center_x)))
    y = int(round(random_module.triangular(top, bottom - 1, center_y)))
    return {
        "ok": True,
        "point": [max(left, min(right - 1, x)), max(top, min(bottom - 1, y))],
        "bounds": bounds,
        "selection": "center_biased_random_verified_input_interior",
    }
