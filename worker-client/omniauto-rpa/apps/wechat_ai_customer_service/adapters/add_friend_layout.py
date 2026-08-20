"""Adaptive layout model for Windows add_friend RPA."""

from __future__ import annotations

from typing import Any, Callable

from apps.wechat_ai_customer_service.adapters.add_friend_locator import make_locator_result, normalize_bounds, normalize_point
from apps.wechat_ai_customer_service.adapters.add_friend_ocr import compact_ocr_text


def point_in_bounds(x: int, y: int, bounds: list[int]) -> bool:
    left, top, right, bottom = [int(value) for value in normalize_bounds(bounds)]
    return left <= int(x) <= right and top <= int(y) <= bottom


def item_center(item: dict[str, Any]) -> tuple[int, int]:
    if item.get("center_x") is not None and item.get("center_y") is not None:
        return int(float(item.get("center_x") or 0)), int(float(item.get("center_y") or 0))
    left = int(float(item.get("left") or 0))
    top = int(float(item.get("top") or 0))
    right = int(float(item.get("right") or left))
    bottom = int(float(item.get("bottom") or top))
    return int((left + right) / 2), int((top + bottom) / 2)


def center_of_bounds(bounds: list[int]) -> tuple[int, int]:
    left, top, right, bottom = normalize_bounds(bounds)
    return int((left + right) / 2), int((top + bottom) / 2)


def item_bounds(item: dict[str, Any]) -> list[int]:
    center_x, center_y = item_center(item)
    return normalize_bounds(
        [
            int(float(item.get("left") or center_x)),
            int(float(item.get("top") or center_y)),
            int(float(item.get("right") or center_x)),
            int(float(item.get("bottom") or center_y)),
        ]
    )


def item_snapshot(item: dict[str, Any] | None, image_size: tuple[int, int]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    center_x, center_y = item_center(item)
    return {
        "text": str(item.get("text") or ""),
        "confidence": float(item.get("confidence") or 0.0),
        "bounds": item_bounds(item),
        "center": [center_x, center_y],
        "image_size": [int(image_size[0]), int(image_size[1])],
    }


def _pixel_is_plus_dark(pixel: Any) -> bool:
    if isinstance(pixel, int):
        return pixel < 120
    try:
        red, green, blue = int(pixel[0]), int(pixel[1]), int(pixel[2])
    except Exception:
        return False
    return red < 120 and green < 120 and blue < 120 and max(red, green, blue) - min(red, green, blue) < 70


def vision_plus_icon_candidates(
    image: Any,
    image_size: tuple[int, int],
    *,
    search_bounds: list[int],
) -> list[dict[str, Any]]:
    if image is None or not hasattr(image, "crop"):
        return []
    if not isinstance(search_bounds, list) or len(search_bounds) < 4:
        return []
    search_bounds = normalize_bounds(search_bounds)
    left, top, right, bottom = search_bounds
    try:
        crop = image.crop((left, top, right, bottom)).convert("RGB")
    except Exception:
        return []
    crop_width, crop_height = crop.size
    if crop_width < 18 or crop_height < 18:
        return []

    pixels = crop.load()
    candidates: list[dict[str, Any]] = []
    half = 7
    for cy in range(half + 2, crop_height - half - 2):
        for cx in range(half + 2, crop_width - half - 2):
            horizontal = 0
            for dx in range(-half, half + 1):
                if any(_pixel_is_plus_dark(pixels[cx + dx, max(0, min(crop_height - 1, cy + dy))]) for dy in (-1, 0, 1)):
                    horizontal += 1
            vertical = 0
            for dy in range(-half, half + 1):
                if any(_pixel_is_plus_dark(pixels[max(0, min(crop_width - 1, cx + dx)), cy + dy]) for dx in (-1, 0, 1)):
                    vertical += 1
            if horizontal < 9 or vertical < 9:
                continue
            center_dark = sum(
                1
                for dy in range(-2, 3)
                for dx in range(-2, 3)
                if _pixel_is_plus_dark(pixels[cx + dx, cy + dy])
            )
            # A text glyph or circle edge often scores on one axis only.  Require a
            # compact crossing in the center and similar horizontal/vertical arms.
            balance = 1.0 - min(1.0, abs(horizontal - vertical) / 12.0)
            confidence = min(0.96, 0.48 + (horizontal + vertical) / 48.0 + center_dark / 80.0 + balance * 0.10)
            if confidence < 0.78:
                continue
            point = [left + cx, top + cy]
            bounds = normalize_bounds([point[0] - 14, point[1] - 14, point[0] + 14, point[1] + 14])
            candidates.append(
                {
                    "source": "vision_plus_icon",
                    "bounds": bounds,
                    "point": point,
                    "confidence": round(confidence, 3),
                    "horizontal_score": horizontal,
                    "vertical_score": vertical,
                    "center_dark_pixels": center_dark,
                }
            )
    deduped: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: float(item.get("confidence") or 0.0), reverse=True):
        point = normalize_point(candidate.get("point"))
        if any(abs(point[0] - normalize_point(existing.get("point"))[0]) <= 5 and abs(point[1] - normalize_point(existing.get("point"))[1]) <= 5 for existing in deduped):
            continue
        deduped.append(candidate)
        if len(deduped) >= 5:
            break
    return deduped


def plus_entry_target(
    geometry: dict[str, Any],
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
    *,
    screenshot: Any | None = None,
    route_kind: str = "windows",
    dynamic_sidebar_header_bounds: list[int] | None = None,
) -> dict[str, Any]:
    width, height = int(image_size[0]), int(image_size[1])
    has_dynamic_header = bool(
        isinstance(dynamic_sidebar_header_bounds, list)
        and len(dynamic_sidebar_header_bounds) >= 4
    )
    safe_bounds = normalize_bounds(dynamic_sidebar_header_bounds) if has_dynamic_header else [0, 0, 0, 0]
    candidates = (
        vision_plus_icon_candidates(screenshot, image_size, search_bounds=safe_bounds)
        if has_dynamic_header
        else []
    )
    selected = candidates[0] if len(candidates) == 1 else None
    executable = bool(selected is not None and str(selected.get("source") or "") == "vision_plus_icon")
    if selected is None:
        selected = {
            "source": "plus_icon_not_found" if has_dynamic_header else "dynamic_sidebar_header_bounds_missing",
            "point": list(center_of_bounds(safe_bounds)) if has_dynamic_header else [0, 0],
            "bounds": list(safe_bounds),
            "confidence": 0.0,
            "executable": False,
        }
    selected_point = normalize_point(selected.get("point"))
    selected_source = str(selected.get("source") or "")
    selected_reason = (
        "plus icon shape matched inside calibrated sidebar header"
        if executable
        else "no executable plus icon candidate found inside calibrated sidebar header"
    )

    target = make_locator_result(
        name="plus_entry",
        label=f"Step1 click target: visually detected plus entry ({route_kind or 'windows'})",
        strategy="sidebar_header_plus_icon_vision_locator",
        region="sidebar_header",
        bounds=list(selected.get("bounds") or safe_bounds),
        point=selected_point,
        candidates=candidates,
        selected_reason=selected_reason,
        confidence=float(selected.get("confidence") or 0.0),
        fallback_used=False,
        fallback_reason="",
        source=selected_source,
        risk="single_click_plus_only_after_surface_preflight",
        metadata={
            "image_size": [width, height],
            "geometry": dict(geometry or {}),
            "route_kind": str(route_kind or "windows"),
            "verify_after_action": "plus_entry_popup_menu_detected",
            "layout_model": "dynamic_layout_snapshot_sidebar_plus_v1",
            "dynamic_sidebar_header_bounds": list(dynamic_sidebar_header_bounds or []),
            "actual_resolution": [width, height],
            "actual_geometry": dict(geometry or {}),
            "final_click_point": list(selected_point) if executable else [],
            "conflicts": [] if len(candidates) <= 1 else ["multiple_plus_icon_candidates"],
            "executable": executable,
        },
    )
    target["platform_adapter"] = str(route_kind or "windows")
    target["item"] = None
    target["executable"] = executable
    return target


def semantic_invite_form_targets(
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None,
    *,
    region_for_point_fn: Callable[[int, int, tuple[int, int]], str] | None = None,
) -> dict[str, dict[str, Any]]:
    width, height = int(image_size[0]), int(image_size[1])
    # Production targets are admitted only from current-frame semantic anchors.
    targets: dict[str, dict[str, Any]] = {}
    items = [item for item in (ocr_items or []) if isinstance(item, dict)]

    def region(x: int, y: int, fallback: str) -> str:
        if region_for_point_fn:
            return region_for_point_fn(x, y, image_size)
        return fallback

    greeting_anchor = find_best_text_item(items, ("发送添加朋友申请", "朋友申请", "申请"), image_size=image_size, max_y_ratio=0.34)
    if greeting_anchor is not None:
        bounds = item_bounds(greeting_anchor)
        field_bounds = normalize_bounds(
            [
                max(18, bounds[0] - 18),
                min(height - 80, bounds[3] + 8),
                min(width - 18, max(bounds[2] + 160, int(width * 0.92))),
                min(height - 190, max(bounds[3] + 84, int(height * 0.22))),
            ]
        )
        point = center_of_bounds(field_bounds)
        targets["invite_greeting_textarea"] = make_semantic_target(
            name="invite_greeting_textarea",
            label="发送添加朋友申请 textarea",
            region=region(point[0], point[1], "invite_form.verify_message"),
            bounds=field_bounds,
            point=point,
            anchor=greeting_anchor,
            selected_reason="semantic anchor: 发送添加朋友申请",
            source="ocr_invite_greeting_label_anchor",
            risk="clear_default_then_paste_verify_message",
            image_size=image_size,
        )

    remark_anchor = find_best_text_item(items, ("备注名", "备注"), image_size=image_size, min_y_ratio=0.18, max_y_ratio=0.72)
    if remark_anchor is not None:
        bounds = item_bounds(remark_anchor)
        field_bounds = normalize_bounds(
            [
                max(18, bounds[0] - 18),
                min(height - 130, bounds[3] + 8),
                min(width - 18, max(bounds[2] + 170, int(width * 0.92))),
                min(height - 80, max(bounds[3] + 64, int(height * 0.39))),
            ]
        )
        point = [int(min(max(field_bounds[0] + 96, field_bounds[0] + 16), field_bounds[2] - 28)), center_of_bounds(field_bounds)[1]]
        targets["invite_remark_input"] = make_semantic_target(
            name="invite_remark_input",
            label="备注 input",
            region=region(point[0], point[1], "invite_form.remark_name"),
            bounds=field_bounds,
            point=point,
            anchor=remark_anchor,
            selected_reason="semantic anchor: 备注",
            source="ocr_invite_remark_label_anchor",
            risk="clear_default_then_paste_remark_name",
            image_size=image_size,
        )

    confirm_anchor = find_best_text_item(items, ("确定", "完成", "发送"), image_size=image_size, min_y_ratio=0.70)
    if confirm_anchor is not None:
        bounds = item_bounds(confirm_anchor)
        point = item_center(confirm_anchor)
        click_bounds = normalize_bounds(
            [
                max(8, bounds[0] - 42),
                max(8, bounds[1] - 20),
                min(width - 8, bounds[2] + 42),
                min(height - 8, bounds[3] + 20),
            ]
        )
        targets["invite_confirm_button"] = make_semantic_target(
            name="invite_confirm_button",
            label="确定 button",
            region=region(point[0], point[1], "invite_form.confirm_button"),
            bounds=click_bounds,
            point=point,
            anchor=confirm_anchor,
            selected_reason="semantic anchor: 确定",
            source="ocr_invite_confirm_button_anchor",
            risk="click_confirm_after_text_review",
            image_size=image_size,
        )

    return targets


def make_semantic_target(
    *,
    name: str,
    label: str,
    region: str,
    bounds: list[int],
    point: list[int] | tuple[int, int],
    anchor: dict[str, Any],
    selected_reason: str,
    source: str,
    risk: str,
    image_size: tuple[int, int],
) -> dict[str, Any]:
    anchor_confidence = float(anchor.get("confidence") or 0.0)
    confidence = min(0.94, max(0.76, anchor_confidence * 0.92 if anchor_confidence else 0.78))
    target = make_locator_result(
        name=name,
        label=label,
        strategy="semantic_ocr_anchor_locator",
        region=region,
        bounds=bounds,
        point=point,
        candidates=[
            {
                "source": source,
                "anchor_text": str(anchor.get("text") or ""),
                "anchor_bounds": item_bounds(anchor),
                "point": normalize_point(point),
                "bounds": normalize_bounds(bounds),
                "confidence": confidence,
            }
        ],
        selected_reason=selected_reason,
        confidence=confidence,
        fallback_used=False,
        fallback_reason="",
        source=source,
        risk=risk,
        metadata={"image_size": [int(image_size[0]), int(image_size[1])], "layout_model": "add_friend_invite_form_v1"},
    )
    target["item"] = item_snapshot(anchor, image_size)
    return target


def find_best_text_item(
    items: list[dict[str, Any]],
    tokens: tuple[str, ...],
    *,
    image_size: tuple[int, int] | None = None,
    min_y_ratio: float = 0.0,
    max_y_ratio: float = 1.0,
) -> dict[str, Any] | None:
    normalized_tokens = tuple(
        token
        for token in (compact_ocr_text(value) for value in tokens)
        if token
    )
    candidates: list[tuple[dict[str, Any], tuple[int, int]]] = []
    for item in items:
        text = compact_ocr_text(item.get("text"))
        if not text:
            continue
        matched_tokens = [token for token in normalized_tokens if token in text]
        if not matched_tokens:
            continue
        center_x, center_y = item_center(item)
        image_height = int(image_size[1]) if image_size else int(item.get("image_height") or item.get("source_image_height") or 0)
        if image_height <= 1:
            image_height = 1
        ratio = center_y / image_height
        if ratio < min_y_ratio or ratio > max_y_ratio:
            continue
        longest_match = max(matched_tokens, key=len)
        candidates.append(
            (
                item,
                (
                    1 if text == longest_match else 0,
                    len(longest_match),
                ),
            )
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda candidate: (
            candidate[1][0],
            candidate[1][1],
            float(candidate[0].get("confidence") or 0.0),
            len(str(candidate[0].get("text") or "")),
        ),
    )[0]


def field_text_visible(
    expected: str,
    ocr_items: list[dict[str, Any]] | None,
    *,
    bounds: list[int] | None = None,
) -> dict[str, Any]:
    clean_expected = compact_ocr_text(expected)
    items = [item for item in (ocr_items or []) if isinstance(item, dict)]
    if bounds:
        items = [
            item
            for item in items
            if point_in_bounds(*item_center(item), bounds)
        ]
    ordered_items = sorted(
        items,
        key=lambda item: (item_center(item)[1], item_center(item)[0]),
    )
    surface = compact_ocr_text(
        "".join(str(item.get("text") or "") for item in ordered_items)
    )
    digits_expected = "".join(ch for ch in str(expected or "") if ch.isdigit())
    digits_surface = "".join(ch for ch in surface if ch.isdigit())
    ok = bool(clean_expected and clean_expected in surface) or bool(digits_expected and digits_expected in digits_surface)
    return {
        "ok": ok,
        "expected_length": len(str(expected or "")),
        "matched_by": "ocr_text" if clean_expected and clean_expected in surface else "digits" if digits_expected and digits_expected in digits_surface else "",
        "scoped_to_field": bool(bounds),
        "ocr_fragment_count": len(ordered_items),
    }


def invite_form_field_verification(
    *,
    verify_message: str,
    remark_name: str,
    remark_code: str,
    ocr_items: list[dict[str, Any]] | None,
    field_bounds: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    scoped_bounds = field_bounds or {}
    verify_result = field_text_visible(
        verify_message,
        ocr_items,
        bounds=scoped_bounds.get("verify_message"),
    )
    remark_result = field_text_visible(
        remark_name,
        ocr_items,
        bounds=scoped_bounds.get("remark_name"),
    )
    code_result = field_text_visible(
        remark_code,
        ocr_items,
        bounds=scoped_bounds.get("remark_code"),
    )
    return {
        "ok": bool(verify_result.get("ok")) and bool(remark_result.get("ok")) and bool(code_result.get("ok")),
        "verify_message": verify_result,
        "remark_name": remark_result,
        "remark_code": code_result,
        "method": "ocr_surface_text_visibility",
    }
