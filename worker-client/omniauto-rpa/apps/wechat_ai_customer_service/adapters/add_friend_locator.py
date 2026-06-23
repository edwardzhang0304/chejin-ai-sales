"""Locator result model for add_friend RPA targets."""

from __future__ import annotations

from typing import Any


LOCATOR_RESULT_FIELDS = (
    "name",
    "label",
    "strategy",
    "region",
    "region_bounds",
    "candidates",
    "selected_reason",
    "bounds",
    "point",
    "confidence",
    "fallback_used",
    "fallback_reason",
)


def make_locator_result(
    *,
    name: str,
    label: str,
    strategy: str,
    region: str,
    bounds: list[int],
    point: list[int] | tuple[int, int],
    candidates: list[dict[str, Any]] | None = None,
    selected_reason: str = "",
    confidence: float = 0.0,
    fallback_used: bool = False,
    fallback_reason: str = "",
    source: str = "",
    risk: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_bounds = normalize_bounds(bounds)
    clean_point = normalize_point(point)
    clean_candidates = [dict(item) for item in (candidates or []) if isinstance(item, dict)]
    result = {
        "name": str(name or ""),
        "label": str(label or name or ""),
        "strategy": str(strategy or ""),
        "region": str(region or ""),
        "region_bounds": clean_bounds,
        "candidates": clean_candidates,
        "selected_reason": str(selected_reason or ""),
        "bounds": clean_bounds,
        "point": clean_point,
        "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
        "fallback_used": bool(fallback_used),
        "fallback_reason": str(fallback_reason or ""),
        "source": str(source or strategy or ""),
        "risk": str(risk or ""),
        "metadata": dict(metadata or {}),
        # Compatibility fields used by the current drawing/clicking code.
        "x": clean_point[0],
        "y": clean_point[1],
        "click_bounds": clean_bounds,
    }
    result["locator"] = {field: result.get(field) for field in LOCATOR_RESULT_FIELDS}
    return result


def fixed_geometry_locator(
    *,
    name: str,
    label: str,
    region: str,
    bounds: list[int],
    point: list[int] | tuple[int, int],
    selected_reason: str,
    risk: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = {
        "source": "fixed_geometry",
        "bounds": normalize_bounds(bounds),
        "point": normalize_point(point),
        "confidence": 0.62,
    }
    return make_locator_result(
        name=name,
        label=label,
        strategy="window_region_geometry_fallback",
        region=region,
        bounds=normalize_bounds(bounds),
        point=normalize_point(point),
        candidates=[candidate],
        selected_reason=selected_reason,
        confidence=0.62,
        fallback_used=True,
        fallback_reason="ocr_locator_not_enabled_for_this_target_yet",
        source="fixed_invite_form_geometry",
        risk=risk,
        metadata=metadata,
    )


def ocr_item_locator(
    *,
    name: str,
    label: str,
    region: str,
    bounds: list[int],
    point: list[int] | tuple[int, int],
    item: dict[str, Any],
    selected_reason: str,
    risk: str,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = {
        "source": "ocr_item",
        "text": str(item.get("text") or ""),
        "bounds": normalize_bounds(
            [
                int(float(item.get("left") or 0)),
                int(float(item.get("top") or 0)),
                int(float(item.get("right") or 0)),
                int(float(item.get("bottom") or 0)),
            ]
        ),
        "point": normalize_point(point),
        "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
    }
    return make_locator_result(
        name=name,
        label=label,
        strategy="window_region_ocr_target",
        region=region,
        bounds=bounds,
        point=point,
        candidates=[candidate],
        selected_reason=selected_reason,
        confidence=float(candidate["confidence"] or 0.0),
        fallback_used=False,
        source=source,
        risk=risk,
        metadata=metadata,
    )


def geometry_fallback_locator(
    *,
    name: str,
    label: str,
    region: str,
    bounds: list[int],
    point: list[int] | tuple[int, int],
    selected_reason: str,
    fallback_reason: str,
    risk: str,
    source: str,
    confidence: float = 0.62,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = {
        "source": "geometry_fallback",
        "bounds": normalize_bounds(bounds),
        "point": normalize_point(point),
        "confidence": confidence,
    }
    return make_locator_result(
        name=name,
        label=label,
        strategy="window_region_geometry_fallback",
        region=region,
        bounds=bounds,
        point=point,
        candidates=[candidate],
        selected_reason=selected_reason,
        confidence=confidence,
        fallback_used=True,
        fallback_reason=fallback_reason,
        source=source,
        risk=risk,
        metadata=metadata,
    )


def normalize_bounds(bounds: list[int] | tuple[int, ...]) -> list[int]:
    values = [int(value) for value in list(bounds or [])[:4]]
    if len(values) < 4:
        values = [0, 0, 0, 0]
    left, top, right, bottom = values
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]


def normalize_point(point: list[int] | tuple[int, int]) -> list[int]:
    values = list(point or [])[:2]
    if len(values) < 2:
        return [0, 0]
    return [int(values[0]), int(values[1])]
