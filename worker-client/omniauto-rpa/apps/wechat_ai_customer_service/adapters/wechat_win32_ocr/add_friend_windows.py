"""Windows add_friend OCR/layout helpers for the WeChat Win32/OCR adapter.

This module owns the Windows-specific add-friend recognition, locator,
reporting, and pure payload helpers.  The sidecar keeps stable facade names
for worker and test compatibility.
"""

from __future__ import annotations

import ctypes
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from apps.wechat_ai_customer_service.adapters.add_friend_actions import (
    ACTION_COMPOSITE_INPUT,
    make_action_result,
)
from apps.wechat_ai_customer_service.adapters.add_friend_artifacts import (
    ADD_FRIEND_ENTRY_CLICK_PLAN_JSON,
    add_friend_route_artifact_root,
)
from apps.wechat_ai_customer_service.adapters.add_friend_contract import normalize_add_friend_query
from apps.wechat_ai_customer_service.adapters.add_friend_diagnostics import (
    step_events_from_review_rows,
    write_step_event_report,
)
from apps.wechat_ai_customer_service.adapters.add_friend_flow_events import add_friend_entry_click_events_from_payload
from apps.wechat_ai_customer_service.adapters.add_friend_layout import (
    find_sidebar_search_anchor_item as layout_find_anchor,
    plus_entry_safe_bounds,
    plus_entry_target as layout_plus_entry_target,
    semantic_invite_form_targets,
    windows_1080p_reference_plus_point,
    windows_plus_point,
)
from apps.wechat_ai_customer_service.adapters.add_friend_locator import (
    LOCATOR_RESULT_FIELDS,
    geometry_fallback_locator,
    ocr_item_locator,
)
from apps.wechat_ai_customer_service.adapters.add_friend_ocr import (
    compact_ocr_text as mapped_compact_ocr_text,
    ocr_item_text as mapped_ocr_item_text,
    ocr_surface_text as mapped_ocr_surface_text,
    ocr_text_has_any as mapped_ocr_text_has_any,
)
from apps.wechat_ai_customer_service.adapters.add_friend_payloads import (
    add_friend_add_contact_entry_not_found_payload,
    add_friend_after_confirm_payload,
    add_friend_invite_form_window_not_found_payload,
    add_friend_phone_not_found_payload,
)
from apps.wechat_ai_customer_service.adapters.add_friend_pacing import pacing_metadata, pacing_range
from apps.wechat_ai_customer_service.adapters.add_friend_result_mapping import (
    ERROR_ACCOUNT_RESTRICTED,
    ERROR_INVITE_FIELD_VERIFICATION_FAILED,
    ERROR_OPERATOR_GUARD_NOT_READY,
    ERROR_PHONE_NOT_FOUND,
    ERROR_WECHAT_WINDOW_NOT_READY,
    RESULT_ALREADY_FRIEND,
    add_friend_completed_result as mapped_add_friend_completed_result,
    add_friend_failed_result as mapped_add_friend_failed_result,
    add_friend_server_report_payload as mapped_add_friend_server_report_payload,
)
from apps.wechat_ai_customer_service.adapters.add_friend_routes import (
    ADD_FRIEND_MAIN_ROUTE,
)
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.env_config import env_flag
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.geometry import (
    bounded_int,
    chat_header_cutoff_y,
    clamp_point_to_bounds,
    input_text_region_bounds,
    point_in_bounds,
    search_box_point_for_geometry,
    session_split_x,
)
from apps.wechat_ai_customer_service.adapters.add_friend_layout import invite_form_field_verification
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import device_profile as win32_ocr_device_profile


PROJECT_ROOT = Path(__file__).resolve().parents[4]


_SIDECAR_OPS: Any | None = None


def bind_sidecar_ops(ops: Any) -> None:
    """Bind the sidecar facade module used for live Win32/OCR operations."""
    global _SIDECAR_OPS
    _SIDECAR_OPS = ops


def _ops() -> Any:
    if _SIDECAR_OPS is None:
        sidecar = sys.modules.get("apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar")
        if sidecar is None:
            raise RuntimeError("wechat_win32_ocr_sidecar facade is not bound")
        return sidecar
    return _SIDECAR_OPS


class _OpsNamespace:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(getattr(_ops(), self._name), attr)


win32con = _OpsNamespace("win32con")
win32api = _OpsNamespace("win32api")

LOGIN_WINDOW_MAX_WIDTH = 560
LOGIN_WINDOW_MAX_HEIGHT = 680
ADD_FRIEND_FOREGROUND_READY_REASONS = {
    "foreground_matches_target",
    "foreground_root_matches_target",
}

WECHAT_LOGIN_OR_SECURITY_BLOCK_TOKENS = (
    "请重新登录",
    "重新登录",
    "登录已过期",
    "登录失效",
    "退出登录",
    "无法继续使用微信",
    "账号安全",
    "安全验证",
    "登录环境异常",
    "操作频繁",
    "账号异常",
    "被限制",
    "限制使用",
)


def add_friend_ocr_compact(text: Any) -> str:
    return mapped_compact_ocr_text(text)

def add_friend_item_text(item: dict[str, Any]) -> str:
    return mapped_ocr_item_text(item)

def add_friend_surface_text(ocr_items: list[dict[str, Any]]) -> str:
    return mapped_ocr_surface_text(ocr_items)

def add_friend_blocking_prompt_region(
    item: dict[str, Any],
    *,
    geometry: dict[str, Any] | None = None,
    image_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    width = int((image_size or (0, 0))[0] or (geometry or {}).get("width") or 0)
    height = int((image_size or (0, 0))[1] or (geometry or {}).get("height") or 0)
    if width <= 0 or height <= 0:
        return {"region": "unknown", "sidebar_noise": False, "width": width, "height": height}
    center_x, center_y = add_friend_item_center(item)
    split_x = session_split_x(width)
    nav_right = max(64, min(92, int(width * 0.075)))
    search_bottom = max(112, min(148, int(height * 0.16)))
    sidebar_noise = nav_right < center_x < split_x and center_y > search_bottom
    return {
        "region": "sidebar_session_list" if sidebar_noise else add_friend_region_for_point(center_x, center_y, (width, height)),
        "sidebar_noise": sidebar_noise,
        "width": width,
        "height": height,
    }

def add_friend_login_or_security_block(
    ocr_items: list[dict[str, Any]],
    *,
    geometry: dict[str, Any] | None = None,
    image_size: tuple[int, int] | None = None,
) -> dict[str, Any]:
    text = add_friend_surface_text(ocr_items)
    matched_items: list[dict[str, Any]] = []
    for item in ocr_items:
        item_text = add_friend_item_text(item)
        if not item_text:
            continue
        item_tokens = [token for token in WECHAT_LOGIN_OR_SECURITY_BLOCK_TOKENS if add_friend_text_has_any(item_text, (token,))]
        if not item_tokens:
            continue
        region = add_friend_blocking_prompt_region(item, geometry=geometry, image_size=image_size)
        matched_items.append({"text": item_text, "tokens": item_tokens, "region": region})
    matched = sorted({token for item in matched_items for token in item.get("tokens", [])})
    if not matched:
        return {"detected": False, "matched_tokens": [], "surface_text": text}
    strong_login = {"请重新登录", "登录已过期", "登录失效", "退出登录", "无法继续使用微信"}
    strong_security = {"账号安全", "登录环境异常", "操作频繁", "账号异常", "被限制", "限制使用"}
    small_window = int((geometry or {}).get("width") or 0) <= LOGIN_WINDOW_MAX_WIDTH and int((geometry or {}).get("height") or 0) <= LOGIN_WINDOW_MAX_HEIGHT
    accepted_items: list[dict[str, Any]] = []
    for item in matched_items:
        tokens = set(item.get("tokens") or [])
        item_text = str(item.get("text") or "")
        item_compact = add_friend_ocr_compact(item_text)
        item_compact_len = len(item_compact)
        region = item.get("region") if isinstance(item.get("region"), dict) else {}
        sidebar_noise = bool(region.get("sidebar_noise"))
        explanatory_chat_text = (
            "遇到" in item_compact
            and "状态" in item_compact
            and bool(tokens & {"安全验证", "操作频繁", "账号异常"})
        )
        if explanatory_chat_text:
            continue
        if tokens & strong_login and (small_window or not sidebar_noise):
            accepted_items.append(item)
            continue
        security_prompt_like = item_compact_len <= (44 if small_window else 20)
        if tokens & strong_security and (not sidebar_noise) and security_prompt_like:
            accepted_items.append(item)
            continue
        if "安全验证" in tokens and not sidebar_noise and item_compact_len <= 24:
            accepted_items.append(item)
    if not accepted_items:
        return {
            "detected": False,
            "matched_tokens": matched,
            "ignored_as_sidebar_preview": True,
            "matched_items": matched_items,
            "surface_text": text,
        }
    accepted_tokens = sorted({token for item in accepted_items for token in item.get("tokens", [])})
    account_restricted = any(token in accepted_tokens for token in ("账号安全", "安全验证", "登录环境异常", "操作频繁", "账号异常", "被限制", "限制使用"))
    return {
        "detected": True,
        "matched_tokens": accepted_tokens,
        "matched_items": accepted_items,
        "surface_text": text,
        "state": "account_restricted" if account_restricted else "wechat_window_not_ready",
        "error_code": ERROR_ACCOUNT_RESTRICTED if account_restricted else ERROR_WECHAT_WINDOW_NOT_READY,
        "reason": "wechat_account_or_security_prompt" if account_restricted else "wechat_login_required",
    }

def add_friend_item_center(item: dict[str, Any]) -> tuple[int, int]:
    return int(float(item.get("center_x") or 0)), int(float(item.get("center_y") or 0))

def add_friend_zone_bounds(image_size: tuple[int, int]) -> list[dict[str, Any]]:
    width, height = image_size
    split_x = session_split_x(width)
    nav_right = max(64, min(92, int(width * 0.075)))
    search_bottom = max(112, min(148, int(height * 0.16)))
    header_bottom = chat_header_cutoff_y(height) + max(32, int(height * 0.045))
    input_left, input_top, input_right, input_bottom = input_text_region_bounds({"width": width, "height": height})
    main_bottom = max(header_bottom + 40, min(height, input_top))
    return [
        {"name": "left_nav", "label": "left_nav", "bounds": [0, 0, nav_right, height], "color": "#2563eb"},
        {"name": "sidebar_search", "label": "sidebar_search", "bounds": [nav_right, 0, split_x, search_bottom], "color": "#059669"},
        {"name": "session_list", "label": "session_list", "bounds": [nav_right, search_bottom, split_x, height], "color": "#ca8a04"},
        {"name": "main_header", "label": "main_header", "bounds": [split_x, 0, width, header_bottom], "color": "#7c3aed"},
        {"name": "main_content", "label": "main_content", "bounds": [split_x, header_bottom, width, main_bottom], "color": "#dc2626"},
        {"name": "input_area", "label": "input_area", "bounds": [input_left, input_top, input_right, input_bottom], "color": "#0891b2"},
    ]

def add_friend_region_for_point(x: int, y: int, image_size: tuple[int, int]) -> str:
    width, height = image_size
    split_x = session_split_x(width)
    nav_right = max(64, min(92, int(width * 0.075)))
    search_bottom = max(112, min(148, int(height * 0.16)))
    header_bottom = chat_header_cutoff_y(height) + max(32, int(height * 0.045))
    input_left, input_top, input_right, input_bottom = input_text_region_bounds({"width": width, "height": height})
    if point_in_bounds(x, y, [input_left, input_top, input_right, input_bottom]):
        return "input_area"
    if x <= nav_right:
        return "left_nav"
    if x < split_x:
        if y <= search_bottom:
            return "sidebar_search"
        return "session_list"
    if y <= header_bottom:
        return "main_header"
    if y >= input_top:
        return "right_bottom"
    return "main_content"

def add_friend_region_for_item(item: dict[str, Any], image_size: tuple[int, int]) -> str:
    center_x, center_y = add_friend_item_center(item)
    return add_friend_region_for_point(center_x, center_y, image_size)

def add_friend_windows_1080p_reference_plus_button_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    """Windows 1920x1080-oriented plus-entry reference kept from the incoming PR.

    On Windows WeChat this can land in the right conversation pane because the
    sidebar split and search-row layout differ. Keep it for comparison only.
    """
    return windows_1080p_reference_plus_point(
        geometry,
        split_x_fn=session_split_x,
        search_box_point_fn=search_box_point_for_geometry,
    )

def add_friend_windows_plus_button_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    """Windows WeChat plus-entry point beside the sidebar search box."""
    return windows_plus_point(
        geometry,
        split_x_fn=session_split_x,
        search_box_point_fn=search_box_point_for_geometry,
    )

def add_friend_plus_button_point_for_geometry(geometry: dict[str, Any]) -> tuple[int, int]:
    return add_friend_windows_plus_button_point_for_geometry(geometry)

def add_friend_plus_entry_safe_bounds(image_size: tuple[int, int]) -> list[int]:
    from apps.wechat_ai_customer_service.adapters.add_friend_layout import plus_entry_safe_bounds

    return plus_entry_safe_bounds(image_size, split_x_fn=session_split_x)

def find_sidebar_search_anchor_item(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any] | None:
    from apps.wechat_ai_customer_service.adapters.add_friend_layout import find_sidebar_search_anchor_item as layout_find_anchor

    return layout_find_anchor(ocr_items, image_size, split_x_fn=session_split_x)

def add_friend_plus_entry_target(
    geometry: dict[str, Any],
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
    *,
    screenshot: Any | None = None,
    route_kind: str = "windows",
) -> dict[str, Any]:
    return layout_plus_entry_target(
        geometry,
        image_size,
        ocr_items or [],
        screenshot=screenshot,
        route_kind=route_kind,
        split_x_fn=session_split_x,
        search_box_point_fn=search_box_point_for_geometry,
        region_for_point_fn=add_friend_region_for_point,
    )

def normalize_point_for_add_friend_target(point: Any) -> list[int]:
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        return [int(point[0] or 0), int(point[1] or 0)]
    return [0, 0]

def add_friend_text_has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return mapped_ocr_text_has_any(text, tokens)

def add_friend_server_report_payload(
    *,
    task_status: str | None = None,
    result_code: str | None = None,
    error_code: str | None = None,
    current_step: str | None = None,
) -> dict[str, str]:
    return mapped_add_friend_server_report_payload(
        task_status=task_status,
        result_code=result_code,
        error_code=error_code,
        current_step=current_step,
    )

def add_friend_completed_result(
    *,
    state: str,
    result_code: str,
    current_step: str = "task_completed",
    **extra: Any,
) -> dict[str, Any]:
    return mapped_add_friend_completed_result(
        state=state,
        result_code=result_code,
        current_step=current_step,
        **extra,
    )

def add_friend_failed_result(
    *,
    state: str,
    error_code: str,
    current_step: str,
    **extra: Any,
) -> dict[str, Any]:
    return mapped_add_friend_failed_result(
        state=state,
        error_code=error_code,
        current_step=current_step,
        **extra,
    )

def find_add_friend_action_item(
    ocr_items: list[dict[str, Any]],
    tokens: tuple[str, ...],
    image_size: tuple[int, int],
    *,
    min_y_ratio: float = 0.0,
    max_y_ratio: float = 1.0,
) -> dict[str, Any] | None:
    width, height = image_size
    min_y = max(0, int(height * min_y_ratio))
    max_y = min(height, int(height * max_y_ratio))
    candidates: list[dict[str, Any]] = []
    for item in ocr_items:
        text = add_friend_item_text(item)
        if not text:
            continue
        matched = False
        for token in tokens:
            compact_token = add_friend_ocr_compact(token)
            if not compact_token:
                continue
            if compact_token == "添加朋友":
                matched = text == compact_token
            else:
                matched = compact_token in text
            if matched:
                break
        if not matched:
            continue
        center_x, center_y = add_friend_item_center(item)
        if center_y < min_y or center_y > max_y:
            continue
        if center_x < 0 or center_x > width:
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (float(item.get("confidence") or 0.0), float(item.get("right") or 0.0) - float(item.get("left") or 0.0)))

def find_add_friend_search_result_item(
    ocr_items: list[dict[str, Any]],
    query: str,
    image_size: tuple[int, int],
) -> dict[str, Any] | None:
    clean_query = re.sub(r"\D+", "", str(query or "")) or add_friend_ocr_compact(query)
    if not clean_query:
        return None
    width, height = image_size
    top_limit = int(height * 0.10)
    right_limit = max(260, int(width * 0.72))
    candidates: list[dict[str, Any]] = []
    for item in ocr_items:
        text = add_friend_item_text(item)
        if not text:
            continue
        center_x, center_y = add_friend_item_center(item)
        if center_y < top_limit or center_x > right_limit:
            continue
        digits = re.sub(r"\D+", "", text)
        if clean_query and (clean_query in digits or clean_query in text):
            candidates.append(item)
            continue
        if "网络查找" in text and any(token in text for token in ("手机", "qq", "微信")):
            candidates.append(item)
    if not candidates:
        return None
    return min(candidates, key=lambda item: (abs(float(item.get("center_y") or 0.0) - height * 0.30), float(item.get("left") or 0.0)))

def classify_add_friend_ocr_surface(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any]:
    text = add_friend_surface_text(ocr_items)
    phone_not_found_tokens = (
        "用户不存在",
        "该用户不存在",
        "账号不存在",
        "手机号不存在",
        "查无此人",
        "没有找到",
        "未找到相关结果",
    )
    if add_friend_text_has_any(text, phone_not_found_tokens):
        return {"state": "phone_not_found", "result_code": "", "error_code": ERROR_PHONE_NOT_FOUND}
    restricted_tokens = ("操作频繁", "账号异常", "账号安全", "被限制", "限制使用")
    if add_friend_text_has_any(text, restricted_tokens):
        return {"state": "account_restricted", "result_code": "", "error_code": ERROR_ACCOUNT_RESTRICTED}
    if find_add_friend_action_item(ocr_items, ("添加到通讯录", "添加至通讯录", "添加朋友"), image_size):
        return {"state": "add_contact_entry", "result_code": "", "error_code": ""}
    if find_add_friend_action_item(ocr_items, ("发送",), image_size, min_y_ratio=0.35):
        if add_friend_text_has_any(text, ("朋友验证", "发送添加朋友申请", "申请添加朋友", "备注名", "标签")):
            return {"state": "invite_form", "result_code": "", "error_code": ""}
    if add_friend_text_has_any(text, ("发消息", "音视频通话", "视频号")) and not add_friend_text_has_any(text, ("添加到通讯录", "添加朋友")):
        return {"state": "already_friend", "result_code": RESULT_ALREADY_FRIEND, "error_code": ""}
    if find_add_friend_search_result_item(ocr_items, "", image_size):
        return {"state": "search_results", "result_code": "", "error_code": ""}
    return {"state": "unknown", "result_code": "", "error_code": ""}

def classify_add_friend_after_confirm_surface(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    confirm_ok: bool,
) -> dict[str, Any]:
    text = add_friend_surface_text(ocr_items)
    invite_surface = add_friend_invite_form_surface_detected(ocr_items)
    return add_friend_after_confirm_payload(
        confirm_ok=confirm_ok,
        surface_text=text,
        invite_form_detected=bool(invite_surface.get("detected")),
    )

def add_friend_item_snapshot(item: dict[str, Any] | None, image_size: tuple[int, int]) -> dict[str, Any] | None:
    if item is None:
        return None
    left = int(float(item.get("left") or 0))
    top = int(float(item.get("top") or 0))
    right = int(float(item.get("right") or 0))
    bottom = int(float(item.get("bottom") or 0))
    center_x, center_y = add_friend_item_center(item)
    return {
        "text": str(item.get("text") or ""),
        "confidence": float(item.get("confidence") or 0.0),
        "bbox": [left, top, right, bottom],
        "center": [center_x, center_y],
        "region": add_friend_region_for_item(item, image_size),
    }

def add_friend_ocr_snapshots(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for index, item in enumerate(ocr_items, start=1):
        snapshot = add_friend_item_snapshot(item, image_size)
        if snapshot is None:
            continue
        snapshot["index"] = index
        snapshots.append(snapshot)
    return snapshots

def draw_add_friend_screen_annotation(
    screenshot: Image.Image,
    *,
    ocr_items: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    output_path: Path,
    window_rect: list[int] | None = None,
) -> str:
    image = screenshot.convert("RGB").copy()
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    width, height = image.size
    if window_rect and len(window_rect) >= 4:
        left, top, right, bottom = [int(value) for value in window_rect[:4]]
        draw.rectangle([left, top, right, bottom], outline="#2563eb", width=4)
        draw.rectangle([left + 2, top + 2, min(right - 2, left + 170), min(bottom - 2, top + 22)], fill="#2563eb")
        draw.text((left + 6, top + 6), "wechat_window", fill="white", font=font)
    for index, item in enumerate(ocr_items, start=1):
        left = int(float(item.get("left") or 0))
        top = int(float(item.get("top") or 0))
        right = int(float(item.get("right") or 0))
        bottom = int(float(item.get("bottom") or 0))
        if right < 0 or bottom < 0 or left > width or top > height:
            continue
        draw.rectangle([left, top, right, bottom], outline="#f97316", width=2)
        label = f"{index}:ocr"
        label_y = max(0, top - 16)
        draw.rectangle([left, label_y, min(width - 1, left + max(42, len(label) * 7)), label_y + 14], fill="#f97316")
        draw.text((left + 2, label_y + 2), label, fill="white", font=font)
    for index, target in enumerate(targets, start=1):
        bounds = target.get("click_bounds")
        if isinstance(bounds, list) and len(bounds) >= 4:
            left, top, right, bottom = [int(value) for value in bounds[:4]]
            draw.rectangle([left, top, right, bottom], outline="#22c55e", width=2)
        x = int(target.get("annotation_x", target.get("x", target.get("screen_x") or 0)) or 0)
        y = int(target.get("annotation_y", target.get("y", target.get("screen_y") or 0)) or 0)
        label = f"T{index}:{target.get('name')}"
        draw.line([x - 16, y, x + 16, y], fill="#ef4444", width=4)
        draw.line([x, y - 16, x, y + 16], fill="#ef4444", width=4)
        draw.ellipse([x - 8, y - 8, x + 8, y + 8], outline="#ef4444", width=3)
        text_x = min(max(0, x + 12), max(0, width - 220))
        text_y = min(max(0, y + 12), max(0, height - 18))
        draw.rectangle([text_x, text_y, min(width - 1, text_x + max(110, len(label) * 7)), text_y + 16], fill="#ef4444")
        draw.text((text_x + 3, text_y + 3), label, fill="white", font=font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return str(output_path)

def draw_add_friend_layout_calibration_annotation(
    screenshot: Image.Image,
    *,
    layout_calibration: dict[str, Any] | None,
    output_path: Path,
) -> str:
    image = screenshot.convert("RGB").copy()
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    width, height = image.size
    regions = {}
    if isinstance(layout_calibration, dict) and isinstance(layout_calibration.get("regions"), dict):
        regions = layout_calibration.get("regions") or {}
    color_by_name = {
        "left_nav": "#2563eb",
        "sidebar_search": "#16a34a",
        "session_list": "#f97316",
        "main_header": "#7c3aed",
        "main_content": "#0891b2",
        "plus_search_region": "#dc2626",
    }
    if not regions:
        draw.rectangle([0, 0, width - 1, height - 1], outline="#dc2626", width=3)
        draw.rectangle([8, 8, min(width - 1, 248), 30], fill="#dc2626")
        draw.text((14, 14), "layout_calibration_missing", fill="white", font=font)
    for index, (name, bounds) in enumerate(regions.items(), start=1):
        if not isinstance(bounds, list) or len(bounds) < 4:
            continue
        left, top, right, bottom = [int(float(value or 0)) for value in bounds[:4]]
        left = max(0, min(width - 1, left))
        top = max(0, min(height - 1, top))
        right = max(left, min(width - 1, right))
        bottom = max(top, min(height - 1, bottom))
        color = color_by_name.get(str(name), "#64748b")
        line_width = 4 if str(name) == "plus_search_region" else 2
        draw.rectangle([left, top, right, bottom], outline=color, width=line_width)
        label = f"{index}:{name}"
        label_width = min(width - 1, left + max(86, len(label) * 7 + 8))
        label_y = top if top + 18 < height else max(0, top - 18)
        draw.rectangle([left, label_y, label_width, min(height - 1, label_y + 16)], fill=color)
        draw.text((left + 3, label_y + 3), label, fill="white", font=font)
    split_x = None
    if isinstance(layout_calibration, dict):
        try:
            split_x = int(layout_calibration.get("split_x"))
        except Exception:
            split_x = None
    if split_x is not None and 0 <= split_x < width:
        draw.line([split_x, 0, split_x, height - 1], fill="#0f172a", width=2)
        draw.rectangle([max(0, split_x - 42), 4, min(width - 1, split_x + 42), 20], fill="#0f172a")
        draw.text((max(0, split_x - 36), 8), f"split_x={split_x}", fill="white", font=font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return str(output_path)

def add_friend_popup_menu_bounds(
    image_size: tuple[int, int],
    *,
    plus_screen_x: int,
    plus_screen_y: int,
) -> list[int]:
    width, height = image_size
    left = max(0, int(plus_screen_x) - 86)
    top = max(0, int(plus_screen_y) + 24)
    right = min(width, int(plus_screen_x) + 132)
    bottom = min(height, int(plus_screen_y) + 206)
    return [left, top, right, bottom]

def add_friend_menu_text_matches(text: str, tokens: tuple[str, ...]) -> bool:
    compact = add_friend_ocr_compact(text)
    if not compact:
        return False
    for token in tokens:
        compact_token = add_friend_ocr_compact(token)
        if compact_token and compact_token in compact:
            return True
    if ("添加朋友" in tokens or "添加好友" in tokens) and "添加" in compact and ("朋友" in compact or "好友" in compact):
        return True
    if "发起群聊" in tokens and ("群聊" in compact or ("发起" in compact and "群" in compact)):
        return True
    if "新建笔记" in tokens and ("笔记" in compact or ("新建" in compact and "笔" in compact)):
        return True
    if "扫一扫" in tokens and "扫" in compact:
        return True
    return False

def find_add_friend_menu_item(
    ocr_items: list[dict[str, Any]],
    tokens: tuple[str, ...],
    image_size: tuple[int, int],
    *,
    popup_bounds: list[int],
) -> dict[str, Any] | None:
    left, top, right, bottom = [int(value) for value in popup_bounds[:4]]
    candidates: list[dict[str, Any]] = []
    for item in ocr_items:
        center_x, center_y = add_friend_item_center(item)
        if not point_in_bounds(center_x, center_y, [left, top, right, bottom]):
            continue
        if not add_friend_menu_text_matches(str(item.get("text") or ""), tokens):
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (float(item.get("confidence") or 0.0), float(item.get("right") or 0.0) - float(item.get("left") or 0.0)))

def add_friend_expected_menu_target(
    *,
    name: str,
    label: str,
    plus_screen_x: int,
    plus_screen_y: int,
    y_offset: int,
    image_size: tuple[int, int],
) -> dict[str, Any]:
    width, height = image_size
    target_x = bounded_int(plus_screen_x + 36, default=plus_screen_x + 36, minimum=0, maximum=max(0, width - 1))
    target_y = bounded_int(plus_screen_y + y_offset, default=plus_screen_y + y_offset, minimum=0, maximum=max(0, height - 1))
    bounds = add_friend_expected_menu_click_bounds(
        image_size=image_size,
        plus_screen_x=plus_screen_x,
        plus_screen_y=plus_screen_y,
        y_offset=y_offset,
    )
    target = geometry_fallback_locator(
        name=name,
        label=label,
        region=add_friend_region_for_point(target_x, target_y, image_size),
        bounds=bounds,
        point=[target_x, target_y],
        selected_reason="expected popup menu row from plus entry geometry",
        fallback_reason="ocr_menu_item_not_detected",
        risk="diagnostic_expected_popup_menu_item_center",
        source="expected_popup_geometry",
        metadata={"image_size": [width, height], "plus_point": [plus_screen_x, plus_screen_y], "y_offset": y_offset},
    )
    target["screen_x"] = target_x
    target["screen_y"] = target_y
    target["item"] = None
    return target

def add_friend_popup_menu_item_click_bounds(item: dict[str, Any], popup_bounds: list[int]) -> list[int]:
    left, top, right, bottom = [int(value) for value in popup_bounds[:4]]
    center_x, center_y = add_friend_item_center(item)
    item_left = int(float(item.get("left") or center_x))
    item_right = int(float(item.get("right") or center_x))
    row_top = max(top + 4, center_y - 22)
    row_bottom = min(bottom - 4, center_y + 22)
    click_left = max(left + 10, min(item_left - 30, right - 32))
    click_right = min(right - 10, max(item_right + 30, click_left + 44))
    if click_right <= click_left:
        click_left = left + 10
        click_right = right - 10
    if row_bottom <= row_top:
        row_top = max(top + 4, center_y - 18)
        row_bottom = min(bottom - 4, center_y + 18)
    return [click_left, row_top, click_right, row_bottom]

def add_friend_expected_menu_click_bounds(
    *,
    image_size: tuple[int, int],
    plus_screen_x: int,
    plus_screen_y: int,
    y_offset: int,
) -> list[int]:
    popup_bounds = add_friend_popup_menu_bounds(image_size, plus_screen_x=plus_screen_x, plus_screen_y=plus_screen_y)
    left, top, right, bottom = [int(value) for value in popup_bounds[:4]]
    center_y = bounded_int(plus_screen_y + y_offset, default=plus_screen_y + y_offset, minimum=top + 8, maximum=bottom - 8)
    return [left + 10, max(top + 4, center_y - 22), right - 10, min(bottom - 4, center_y + 22)]

def add_friend_menu_candidate_targets(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    plus_screen_x: int | None = None,
    plus_screen_y: int | None = None,
    include_expected: bool = True,
) -> list[dict[str, Any]]:
    candidates = [
        ("add_friend_menu_entry", "Menu candidate: 添加朋友", ("添加朋友", "添加好友")),
        ("start_group_chat_menu_entry", "Menu candidate: 发起群聊", ("发起群聊",)),
        ("scan_menu_entry", "Menu candidate: 扫一扫", ("扫一扫",)),
        ("new_note_menu_entry", "Menu candidate: 新建笔记", ("新建笔记",)),
    ]
    popup_bounds = (
        add_friend_popup_menu_bounds(image_size, plus_screen_x=int(plus_screen_x), plus_screen_y=int(plus_screen_y))
        if plus_screen_x is not None and plus_screen_y is not None
        else [0, 0, image_size[0], image_size[1]]
    )
    targets: list[dict[str, Any]] = []
    for name, label, tokens in candidates:
        item = find_add_friend_menu_item(ocr_items, tokens, image_size, popup_bounds=popup_bounds)
        if item is None:
            continue
        center_x, center_y = add_friend_item_center(item)
        click_bounds = add_friend_popup_menu_item_click_bounds(item, popup_bounds)
        click_x, click_y = clamp_point_to_bounds(center_x, center_y, click_bounds)
        target = ocr_item_locator(
            name=name,
            label=label,
            region=add_friend_region_for_point(click_x, click_y, image_size),
            bounds=click_bounds,
            point=[click_x, click_y],
            item=item,
            selected_reason="matched popup menu OCR text",
            risk="diagnostic_only_no_click_menu_item",
            source="ocr_popup_menu_item",
            metadata={"image_size": [image_size[0], image_size[1]], "tokens": list(tokens)},
        )
        target["raw_x"] = center_x
        target["raw_y"] = center_y
        target["item"] = add_friend_item_snapshot(item, image_size)
        targets.append(target)
    if include_expected and plus_screen_x is not None and plus_screen_y is not None:
        existing = {str(target.get("name") or "") for target in targets}
        expected_offsets = [
            ("start_group_chat_menu_entry", "Expected popup center: 发起群聊", 60),
            ("add_friend_menu_entry", "Expected popup center: 添加朋友", 104),
            ("new_note_menu_entry", "Expected popup center: 新建笔记", 148),
        ]
        for name, label, y_offset in expected_offsets:
            if name in existing:
                continue
            expected = add_friend_expected_menu_target(
                name=name,
                label=label,
                plus_screen_x=int(plus_screen_x),
                plus_screen_y=int(plus_screen_y),
                y_offset=y_offset,
                image_size=image_size,
            )
            targets.append(expected)
    return targets

def plus_entry_popup_menu_detected(ocr_items: list[dict[str, Any]], targets: list[dict[str, Any]]) -> dict[str, Any]:
    target_names = {
        str(item.get("name") or "")
        for item in targets
        if isinstance(item, dict) and str(item.get("source") or "") != "expected_popup_geometry"
    }
    menu_target_names = {
        "add_friend_menu_entry",
        "start_group_chat_menu_entry",
        "scan_menu_entry",
        "new_note_menu_entry",
    }
    matched_target_names = sorted(name for name in target_names if name in menu_target_names)
    if matched_target_names:
        return {
            "detected": True,
            "reason": "plus_entry_popup_menu_item_detected",
            "matched_target_names": matched_target_names,
            "target_names": sorted(target_names),
        }
    surface = add_friend_surface_text(ocr_items)
    menu_tokens = ("发起群聊", "添加朋友", "添加好友", "新建笔记", "扫一扫")
    matched = [token for token in menu_tokens if add_friend_ocr_compact(token) in surface]
    return {
        "detected": len(matched) >= 1,
        "reason": "plus_entry_popup_menu_text_detected" if len(matched) >= 1 else "menu_not_detected",
        "matched_tokens": matched,
        "target_names": sorted(target_names),
    }

def add_friend_target_review_text(targets: list[dict[str, Any]]) -> str:
    if not targets:
        return "无目标标注"
    parts: list[str] = []
    for target in targets:
        name = str(target.get("name") or "")
        label = str(target.get("label") or name)
        source = str(target.get("source") or "manual")
        x = target.get("screen_x", target.get("x"))
        y = target.get("screen_y", target.get("y"))
        parts.append(f"{name} ({label}) @ {x},{y}, source={source}")
    return "\n".join(parts)

def add_friend_target_by_name(targets: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for target in targets:
        if isinstance(target, dict) and str(target.get("name") or "") == name:
            return target
    return None

def add_friend_target_screen_point(target: dict[str, Any]) -> tuple[int, int]:
    return int(target.get("click_screen_x", target.get("screen_x", target.get("x") or 0)) or 0), int(target.get("click_screen_y", target.get("screen_y", target.get("y") or 0)) or 0)

def add_click_screen_origin_to_targets(targets: list[dict[str, Any]], *, origin_x: int, origin_y: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for target in targets:
        copied = dict(target)
        copied["click_screen_x"] = int(origin_x) + int(copied.get("x") or 0)
        copied["click_screen_y"] = int(origin_y) + int(copied.get("y") or 0)
        bounds = copied.get("click_bounds")
        if isinstance(bounds, list) and len(bounds) >= 4:
            copied["click_screen_bounds"] = [
                int(origin_x) + int(bounds[0]),
                int(origin_y) + int(bounds[1]),
                int(origin_x) + int(bounds[2]),
                int(origin_y) + int(bounds[3]),
            ]
        result.append(copied)
    return result

def add_friend_page_search_region(image_size: tuple[int, int]) -> list[int]:
    width, height = image_size
    if width <= 560:
        return [20, 54, max(21, width - 16), min(height - 16, 162)]
    split_x = session_split_x(width)
    left = max(split_x + 24, int(width * 0.38))
    top = max(88, int(height * 0.10))
    right = min(width - 24, max(left + 320, int(width * 0.86)))
    bottom = min(height - 40, max(top + 190, int(height * 0.38)))
    return [left, top, right, bottom]

def add_friend_search_result_region(image_size: tuple[int, int]) -> list[int]:
    width, height = image_size
    if width <= 560:
        return [20, 118, max(21, width - 16), max(160, height - 24)]
    split_x = session_split_x(width)
    left = max(split_x + 24, int(width * 0.36))
    top = max(150, int(height * 0.18))
    right = min(width - 24, max(left + 360, int(width * 0.90)))
    bottom = min(height - 36, max(top + 320, int(height * 0.72)))
    return [left, top, right, bottom]

def add_friend_phone_not_found_detected(ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    text = add_friend_surface_text(ocr_items)
    tokens = (
        "无法找到该用户",
        "请检查你填写的账号是否正确",
        "用户不存在",
        "该用户不存在",
        "账号不存在",
        "手机号不存在",
        "查无此人",
        "没有找到",
        "未找到相关结果",
    )
    matched = [token for token in tokens if add_friend_ocr_compact(token) in text]
    return {
        "detected": bool(matched),
        "matched_tokens": matched,
        "ocr_text": text,
    }

def add_friend_search_result_add_contact_target(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
) -> dict[str, Any] | None:
    item = find_add_friend_action_item(
        ocr_items,
        ("添加到通讯录", "添加至通讯录", "添加通讯录", "添加朋友"),
        image_size,
        min_y_ratio=0.15,
        max_y_ratio=0.95,
    )
    if item is None:
        return None
    center_x, center_y = add_friend_item_center(item)
    left = int(float(item.get("left") or center_x))
    top = int(float(item.get("top") or center_y))
    right = int(float(item.get("right") or center_x))
    bottom = int(float(item.get("bottom") or center_y))
    width, height = image_size
    bounds = [
        max(10, left - 42),
        max(10, top - 18),
        min(width - 10, right + 42),
        min(height - 10, bottom + 18),
    ]
    click_x, click_y = clamp_point_to_bounds(center_x, center_y, bounds)
    target = ocr_item_locator(
        name="add_contact_entry_button",
        label="Search result: 添加到通讯录",
        region=add_friend_region_for_point(click_x, click_y, image_size),
        bounds=bounds,
        point=[click_x, click_y],
        item=item,
        selected_reason="matched add-contact OCR text in search result",
        risk="click_add_contact_entry_then_stop",
        source="ocr_search_result_add_contact",
        metadata={"image_size": [image_size[0], image_size[1]]},
    )
    target["raw_x"] = center_x
    target["raw_y"] = center_y
    target["item"] = add_friend_item_snapshot(item, image_size)
    return target

def add_friend_invite_form_targets(
    image_size: tuple[int, int],
    ocr_items: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    return semantic_invite_form_targets(
        image_size,
        ocr_items or [],
        region_for_point_fn=add_friend_region_for_point,
    )

def find_add_friend_page_search_targets(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    screenshot: Image.Image | None = None,
) -> dict[str, Any]:
    search_region = add_friend_page_search_region(image_size)
    input_item = find_add_friend_search_placeholder_item(ocr_items, image_size, search_region=search_region)
    search_button = find_add_friend_search_button_item(ocr_items, image_size, search_region=search_region)
    visual_button = find_add_friend_search_button_by_visual(screenshot, image_size, search_region=search_region)
    split_x = session_split_x(image_size[0])
    small_add_friend_window = image_size[0] <= 560
    fallback_input_x = int(image_size[0] * 0.38) if small_add_friend_window else max(split_x + 150, int(image_size[0] * 0.53))
    fallback_input_y = 96 if small_add_friend_window else max(118, int(image_size[1] * 0.16))
    visual_button_bounds = visual_button.get("bounds") if isinstance(visual_button, dict) else None
    visual_button_left = int(visual_button_bounds[0]) if isinstance(visual_button_bounds, list) and len(visual_button_bounds) >= 4 else None
    if input_item is not None:
        input_x, input_y = add_friend_item_center(input_item)
        if not small_add_friend_window:
            input_x = max(split_x + 80, input_x)
        input_left = int(float(input_item.get("left") or input_x))
        input_top = int(float(input_item.get("top") or input_y))
        input_right = int(float(input_item.get("right") or input_x))
        input_bottom = int(float(input_item.get("bottom") or input_y))
        button_left = None
        if search_button is not None:
            button_left = int(float(search_button.get("left") or 0)) or None
        if button_left is None:
            button_left = visual_button_left
        right_limit = (button_left - 8) if button_left else max(input_right + 160, input_x + 80)
        input_bounds = [
            max(search_region[0] + 8, input_left - 28),
            max(search_region[1], input_top - 18),
            min(search_region[2] - 8, max(input_right + 28, right_limit)),
            min(search_region[3], input_bottom + 18),
        ]
        input_target = ocr_item_locator(
            name="add_friend_search_input",
            label="Add friend page search input",
            region=add_friend_region_for_point(input_x, input_y, image_size),
            bounds=input_bounds,
            point=[input_x, input_y],
            item=input_item,
            selected_reason="matched search input placeholder OCR text",
            risk="type_query_here",
            source="ocr_search_input_or_placeholder",
            metadata={"image_size": [image_size[0], image_size[1]], "search_region": search_region},
        )
        input_target["item"] = add_friend_item_snapshot(input_item, image_size)
    elif visual_button_left is not None:
        button_top = int(visual_button_bounds[1])
        button_bottom = int(visual_button_bounds[3])
        input_bounds = [
            search_region[0] + 12,
            max(search_region[1], button_top),
            max(search_region[0] + 90, visual_button_left - 8),
            min(search_region[3], button_bottom),
        ]
        input_x, input_y = clamp_point_to_bounds(
            int((input_bounds[0] + input_bounds[2]) / 2),
            int((input_bounds[1] + input_bounds[3]) / 2),
            input_bounds,
        )
        input_target = geometry_fallback_locator(
            name="add_friend_search_input",
            label="Add friend page search input from visual search button anchor",
            region=add_friend_region_for_point(input_x, input_y, image_size),
            bounds=input_bounds,
            point=[input_x, input_y],
            selected_reason="visual search button detected; input is the adjacent left field",
            fallback_reason="ocr_placeholder_not_detected_visual_button_anchor_used",
            risk="type_query_here_visual_button_anchor",
            source="visual_search_button_anchor",
            confidence=0.78,
            metadata={"image_size": [image_size[0], image_size[1]], "search_region": search_region},
        )
        input_target["strategy"] = "visual_button_anchor_locator"
        input_target["fallback_used"] = False
        input_target["fallback_reason"] = ""
        input_target["source"] = "visual_search_button_anchor"
        input_target["locator"] = {field: input_target.get(field) for field in LOCATOR_RESULT_FIELDS}
        input_target["item"] = None
    else:
        input_x, input_y = fallback_input_x, fallback_input_y
        if small_add_friend_window:
            input_bounds = [32, 72, max(120, min(image_size[0] - 126, 292)), 122]
            input_x, input_y = clamp_point_to_bounds(
                bounded_int(input_x, default=158, minimum=input_bounds[0] + 20, maximum=input_bounds[2] - 20),
                bounded_int(input_y, default=96, minimum=input_bounds[1] + 8, maximum=input_bounds[3] - 8),
                input_bounds,
            )
        else:
            input_bounds = [
                max(split_x + 48, input_x - 140),
                max(search_region[1], input_y - 24),
                min(image_size[0] - 80, input_x + 170),
                min(search_region[3], input_y + 24),
            ]
        input_target = geometry_fallback_locator(
            name="add_friend_search_input",
            label="Add friend page search input",
            region=add_friend_region_for_point(input_x, input_y, image_size),
            bounds=input_bounds,
            point=[input_x, input_y],
            selected_reason="last-resort fixed search input point from window geometry",
            fallback_reason="search_input_ocr_not_detected",
            risk="HIGH_RISK_FIXED_FALLBACK: type query only after exact OCR verification",
            source="fallback_search_input_geometry",
            metadata={"image_size": [image_size[0], image_size[1]], "search_region": search_region},
        )
        input_target["item"] = None
    if search_button is not None:
        button_x, button_y = add_friend_item_center(search_button)
        if abs(button_x - input_x) < 80:
            button_x = min(image_size[0] - 32, input_x + 210)
        button_left = int(float(search_button.get("left") or button_x))
        button_top = int(float(search_button.get("top") or button_y))
        button_right = int(float(search_button.get("right") or button_x))
        button_bottom = int(float(search_button.get("bottom") or button_y))
        button_bounds = [
            max(search_region[0], button_left - 28),
            max(search_region[1], button_top - 16),
            min(image_size[0] - 12, button_right + 28),
            min(search_region[3], button_bottom + 16),
        ]
        button_target = ocr_item_locator(
            name="add_friend_search_button",
            label="Add friend page search button",
            region=add_friend_region_for_point(button_x, button_y, image_size),
            bounds=button_bounds,
            point=[button_x, button_y],
            item=search_button,
            selected_reason="matched search button OCR text",
            risk="click_search_after_query_verified",
            source="ocr_search_button",
            metadata={"image_size": [image_size[0], image_size[1]], "search_region": search_region},
        )
        button_target["item"] = add_friend_item_snapshot(search_button, image_size)
    elif visual_button is not None:
        button_bounds = [int(value) for value in visual_button["bounds"]]
        button_x, button_y = clamp_point_to_bounds(
            int((button_bounds[0] + button_bounds[2]) / 2),
            int((button_bounds[1] + button_bounds[3]) / 2),
            button_bounds,
        )
        button_target = geometry_fallback_locator(
            name="add_friend_search_button",
            label="Add friend page search button visual target",
            region=add_friend_region_for_point(button_x, button_y, image_size),
            bounds=button_bounds,
            point=[button_x, button_y],
            selected_reason="visual green search button detected",
            fallback_reason="",
            risk="click_search_after_exact_query_verified_visual_button",
            source="visual_search_button",
            confidence=float(visual_button.get("confidence") or 0.78),
            metadata={"image_size": [image_size[0], image_size[1]], "search_region": search_region},
        )
        button_target["strategy"] = "visual_button_locator"
        button_target["fallback_used"] = False
        button_target["fallback_reason"] = ""
        button_target["source"] = "visual_search_button"
        button_target["locator"] = {field: button_target.get(field) for field in LOCATOR_RESULT_FIELDS}
        button_target["item"] = None
    else:
        if small_add_friend_window:
            button_bounds = [max(input_target["click_bounds"][2] + 4, image_size[0] - 118), 72, max(input_target["click_bounds"][2] + 44, image_size[0] - 30), 122]
            button_x, button_y = clamp_point_to_bounds(
                int((button_bounds[0] + button_bounds[2]) / 2),
                input_y,
                button_bounds,
            )
        else:
            button_x, button_y = min(image_size[0] - 38, input_x + 230), input_y
            button_bounds = [
                max(search_region[0], button_x - 42),
                max(search_region[1], button_y - 24),
                min(image_size[0] - 12, button_x + 42),
                min(search_region[3], button_y + 24),
            ]
        button_target = geometry_fallback_locator(
            name="add_friend_search_button",
            label="Add friend page search button",
            region=add_friend_region_for_point(button_x, button_y, image_size),
            bounds=button_bounds,
            point=[button_x, button_y],
            selected_reason="last-resort fixed search button point to the right of search input",
            fallback_reason="search_button_ocr_not_detected",
            risk="HIGH_RISK_FIXED_FALLBACK: click only after exact query verification",
            source="fallback_search_button_geometry",
            metadata={"image_size": [image_size[0], image_size[1]], "search_region": search_region},
        )
        button_target["item"] = None
    return {
        "search_region": search_region,
        "input": input_target,
        "button": button_target,
    }

def find_add_friend_search_placeholder_item(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    search_region: list[int],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in ocr_items:
        center_x, center_y = add_friend_item_center(item)
        if not point_in_bounds(center_x, center_y, search_region):
            continue
        compact = add_friend_ocr_compact(add_friend_item_text(item))
        has_contact_hint = any(token in compact for token in ("微信号", "手机号", "QQ号", "微信"))
        if not has_contact_hint:
            continue
        if compact == "搜索":
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (float(item.get("confidence") or 0.0), float(item.get("right") or 0.0) - float(item.get("left") or 0.0)))

def find_add_friend_search_button_item(
    ocr_items: list[dict[str, Any]],
    image_size: tuple[int, int],
    *,
    search_region: list[int],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in ocr_items:
        center_x, center_y = add_friend_item_center(item)
        if not point_in_bounds(center_x, center_y, search_region):
            continue
        compact = add_friend_ocr_compact(add_friend_item_text(item))
        if compact != "搜索":
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (float(item.get("confidence") or 0.0), float(item.get("left") or 0.0)))

def find_add_friend_search_button_by_visual(
    screenshot: Image.Image | None,
    image_size: tuple[int, int],
    *,
    search_region: list[int],
) -> dict[str, Any] | None:
    if screenshot is None:
        return None
    left, top, right, bottom = [int(value) for value in search_region[:4]]
    try:
        image = screenshot.convert("RGB")
    except Exception:
        return None
    points: list[tuple[int, int]] = []
    sample_step = 2
    for y in range(max(0, top), min(image_size[1], bottom), sample_step):
        for x in range(max(0, left), min(image_size[0], right), sample_step):
            r, g, b = image.getpixel((x, y))
            if g >= 145 and r <= 90 and b <= 140 and (g - max(r, b)) >= 50:
                points.append((x, y))
    if len(points) < 48:
        return None
    min_x = min(x for x, _ in points)
    max_x = max(x for x, _ in points)
    min_y = min(y for _, y in points)
    max_y = max(y for _, y in points)
    if max_x - min_x < 38 or max_y - min_y < 28:
        return None
    bounds = [
        max(left, min_x - 4),
        max(top, min_y - 4),
        min(right, max_x + 4),
        min(bottom, max_y + 4),
    ]
    return {
        "source": "visual_search_button",
        "bounds": bounds,
        "point": [int((bounds[0] + bounds[2]) / 2), int((bounds[1] + bounds[3]) / 2)],
        "confidence": 0.78,
        "green_pixels": len(points),
    }

def add_friend_query_visible_in_items(query: str, ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    clean_query = add_friend_ocr_compact(query)
    text = add_friend_surface_text(ocr_items)
    digits_query = re.sub(r"\D+", "", str(query or ""))
    digits_text = re.sub(r"\D+", "", text)
    if digits_query:
        visible = digits_text == digits_query
        reason = "digits_exact_match" if visible else "digits_not_exact_match"
    else:
        compact_items = [add_friend_ocr_compact(add_friend_item_text(item)) for item in ocr_items]
        compact_items = [item for item in compact_items if item and item != "搜索"]
        visible = bool(clean_query and compact_items.count(clean_query) == 1)
        reason = "text_exact_item_match" if visible else "text_not_exact_match"
    return {
        "ok": visible,
        "query": str(query or ""),
        "ocr_text": text,
        "digits_text": digits_text,
        "expected_digits": digits_query,
        "reason": reason,
    }

def add_friend_search_input_empty_in_items(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any]:
    search_region = add_friend_page_search_region(image_size)
    placeholder = find_add_friend_search_placeholder_item(ocr_items, image_size, search_region=search_region)
    surface = add_friend_surface_text(ocr_items)
    digit_sequences = re.findall(r"\d{5,20}", surface)
    return {
        "ok": placeholder is not None and not digit_sequences,
        "placeholder_visible": placeholder is not None,
        "digits": digit_sequences,
        "ocr_text": surface,
    }

def add_friend_dialog_surface_detected(ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    surface = add_friend_surface_text(ocr_items)
    has_title = "添加朋友" in surface or "添加好友" in surface
    has_search_placeholder = (
        ("搜索" in surface and ("微信号" in surface or "手机号" in surface))
        or "搜索微信号或者手机号" in surface
        or "搜索微信号或手机号" in surface
    )
    return {
        "detected": bool(has_title or has_search_placeholder),
        "has_title": has_title,
        "has_search_placeholder": has_search_placeholder,
        "surface": surface,
    }

def add_friend_invite_form_surface_detected(ocr_items: list[dict[str, Any]]) -> dict[str, Any]:
    surface = add_friend_surface_text(ocr_items)
    has_title = "申请添加朋友" in surface or "朋友验证" in surface
    has_greeting = "发送添加朋友申请" in surface
    has_remark = "备注" in surface
    has_confirm = "确定" in surface
    return {
        "detected": bool(has_title or (has_greeting and has_remark) or (has_remark and has_confirm)),
        "has_title": has_title,
        "has_greeting": has_greeting,
        "has_remark": has_remark,
        "has_confirm": has_confirm,
        "surface": surface,
    }

def add_friend_failure_payload(
    *,
    error_code: str,
    message: str,
    steps: list[str],
    query: str,
    phone: str,
    wechat: str,
    probe: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    state: str = "add_friend_failed",
) -> dict[str, Any]:
    return {
        "ok": False,
        "online": True,
        "adapter": "win32_ocr",
        "state": state,
        "task_type": "add_friend",
        "error_code": error_code,
        "message": message,
        "current_step": steps[-1] if steps else "",
        "steps": list(steps),
        "query": query,
        "phone": phone,
        "wechat": wechat,
        "window_probe": probe,
        "evidence": evidence or {},
    }

def add_friend_main_entry_surface_evidence(ocr_items: list[dict[str, Any]], image_size: tuple[int, int]) -> dict[str, Any]:
    search_anchor = find_sidebar_search_anchor_item(ocr_items, image_size)
    if search_anchor is not None:
        return {
            "ok": True,
            "reason": "sidebar_search_anchor_detected",
            "anchor": add_friend_item_snapshot(search_anchor, image_size),
        }
    text = add_friend_surface_text(ocr_items)
    compact = add_friend_ocr_compact(text)
    has_wechat_sidebar = any(token in compact for token in ("通讯录", "聊天", "文件传输助手", "微信团队"))
    has_browser_like_text = any(token.lower() in compact.lower() for token in ("127.0.0.1", "localhost", "github", "twitter", "provider", "http"))
    if has_wechat_sidebar and not has_browser_like_text:
        return {
            "ok": True,
            "reason": "wechat_sidebar_text_detected",
            "surface_text_sample": [item.get("text") for item in ocr_items[:12]],
        }
    return {
        "ok": False,
        "reason": "sidebar_search_anchor_missing_or_non_wechat_content",
        "browser_like_text": has_browser_like_text,
        "surface_text_sample": [item.get("text") for item in ocr_items[:12]],
    }

def add_friend_optional_field_fill_enabled() -> bool:
    return env_flag("WECHAT_WIN32_OCR_ADD_FRIEND_FILL_OPTIONAL_FIELDS", default=False)

def add_friend_virtual_key_for_digit(char: str) -> int:
    if not re.fullmatch(r"\d", str(char or "")):
        raise ValueError(f"not_a_digit:{char!r}")
    return ord(str(char))


def click_add_contact_entry_from_search_result(hwnd: int, output_dir: Path, *, result_shot: Image.Image, result_path: str, result_items: list[dict[str, Any]], query: str, verify_message: str='', remark_name: str='', remark_code: str='') -> dict[str, Any]:
    not_found = add_friend_phone_not_found_detected(result_items)
    if not_found.get('detected'):
        annotated_path = output_dir / 'add_friend_search_result_phone_not_found_annotated.png'
        annotated = draw_add_friend_screen_annotation(result_shot, ocr_items=result_items, targets=[], output_path=annotated_path, window_rect=None)
        payload = add_friend_phone_not_found_payload(query=query, not_found=not_found, screenshot_path=result_path, annotated_path=annotated, ocr_items=add_friend_ocr_snapshots(result_items, result_shot.size))
        return payload
    target = add_friend_search_result_add_contact_target(result_items, result_shot.size)
    annotated_before_path = output_dir / 'add_friend_search_result_add_contact_before_click_annotated.png'
    annotated_before = draw_add_friend_screen_annotation(result_shot, ocr_items=result_items, targets=[target] if target else [], output_path=annotated_before_path, window_rect=None)
    if target is None:
        surface = classify_add_friend_ocr_surface(result_items, result_shot.size)
        if surface.get('result_code') == RESULT_ALREADY_FRIEND:
            return add_friend_completed_result(state='already_friend', result_code=RESULT_ALREADY_FRIEND, current_step='searching_contact', screenshot_path=result_path, annotated_path=annotated_before, targets=[], ocr_items=add_friend_ocr_snapshots(result_items, result_shot.size), result_basis='search_result_profile_has_message_actions')
        return add_friend_add_contact_entry_not_found_payload(phone=query, screenshot_path=result_path, annotated_path=annotated_before, targets=[], ocr_items=add_friend_ocr_snapshots(result_items, result_shot.size))
    timings: list[dict[str, Any]] = []
    pause_seconds = _ops().add_friend_paced_pause('critical_click', reason='before_add_contact_entry_click')
    timings.append({'name': 'before_add_contact_entry_click_pause', 'seconds': round(pause_seconds, 3)})
    click_started_at = time.perf_counter()
    click_result = _ops().human_window_image_click_in_bounds(hwnd, int(target.get('x') or 0), int(target.get('y') or 0), bounds=list(target.get('click_bounds') or []), action_name='add_contact_entry_click')
    timings.append({'name': 'add_contact_entry_click', 'seconds': round(time.perf_counter() - click_started_at, 3), 'result': click_result})
    pause_seconds = _ops().add_friend_paced_pause('verify', reason='after_add_contact_entry_click_before_capture')
    timings.append({'name': 'after_add_contact_entry_click_before_capture_pause', 'seconds': round(pause_seconds, 3)})
    invite_probe = _ops().wait_for_add_friend_invite_form_window(exclude_hwnds={int(hwnd or 0)}, output_dir=output_dir)
    invite_hwnd = int(invite_probe.get('hwnd') or 0) if invite_probe.get('ok') else 0
    evidence_hwnd = invite_hwnd or hwnd
    after_shot, after_path = _ops().capture_wechat_window_visible_screen(evidence_hwnd, artifact_dir=str(output_dir), label='add_contact_entry_after_click_window')
    after_items = _ops().run_ocr_on_screen_region(after_shot, [0, 0, after_shot.size[0], after_shot.size[1]])
    after_annotated_path = output_dir / 'add_contact_entry_after_click_window_annotated.png'
    after_targets = list(add_friend_invite_form_targets(after_shot.size, after_items).values()) if invite_hwnd else []
    after_annotated = draw_add_friend_screen_annotation(after_shot, ocr_items=after_items, targets=after_targets, output_path=after_annotated_path, window_rect=None)
    if not invite_hwnd:
        return add_friend_invite_form_window_not_found_payload(phone=query, before={'screenshot_path': result_path, 'annotated_path': annotated_before, 'targets': [target], 'ocr_items': add_friend_ocr_snapshots(result_items, result_shot.size)}, click=click_result, after={'screenshot_path': after_path, 'annotated_path': after_annotated, 'ocr_items': add_friend_ocr_snapshots(after_items, after_shot.size)}, invite_form_probe=invite_probe, timings=timings)
    invite_result = _ops().fill_add_friend_invite_form_and_confirm(invite_hwnd, output_dir, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code)
    invite_timings = list(invite_result.get('timings') or []) if isinstance(invite_result, dict) else []
    timings.extend(invite_timings)
    return {'ok': bool(click_result.get('ok')) and bool(invite_result.get('ok')), 'state': str(invite_result.get('state') or 'add_contact_entry_clicked'), 'query': query, 'task_status': str(invite_result.get('task_status') or 'running'), 'result_code': str(invite_result.get('result_code') or ''), 'error_code': str(invite_result.get('error_code') or ''), 'current_step': str(invite_result.get('current_step') or 'invite_confirm_clicked'), 'server_report_payload': invite_result.get('server_report_payload'), 'before': {'screenshot_path': result_path, 'annotated_path': annotated_before, 'targets': [target], 'ocr_items': add_friend_ocr_snapshots(result_items, result_shot.size)}, 'click': click_result, 'after': {'screenshot_path': after_path, 'annotated_path': after_annotated, 'ocr_items': add_friend_ocr_snapshots(after_items, after_shot.size), 'targets': after_targets}, 'invite_form_probe': invite_probe, 'invite_form': invite_result, 'timings': timings}


def paste_invite_form_text(hwnd: int, target: dict[str, Any], text: str, *, action_name: str) -> dict[str, Any]:
    clean = str(text or '')
    if not clean:
        return {'ok': True, 'skipped': True, 'reason': 'empty_text', 'action': make_action_result(action_id=action_name, action_type=ACTION_COMPOSITE_INPUT, status='skipped', method='click_ctrl_a_backspace_clipboard_paste', target=target, text=clean, metadata={'reason': 'empty_text'})}
    bounds = list(target.get('click_bounds') or [])
    if len(bounds) < 4:
        return {'ok': False, 'reason': 'target_missing_click_bounds', 'target': target, 'action': make_action_result(action_id=action_name, action_type=ACTION_COMPOSITE_INPUT, status='failed', method='click_ctrl_a_backspace_clipboard_paste', target=target, text=clean, error='target_missing_click_bounds')}
    click_result = _ops().human_window_image_click_in_bounds(hwnd, int(target.get('x') or 0), int(target.get('y') or 0), bounds=bounds, action_name=f'{action_name}_click')
    if not click_result.get('ok'):
        return {'ok': False, 'reason': 'field_click_failed', 'method': 'click_ctrl_a_backspace_clipboard_paste', 'text_length': len(clean), 'click': click_result, 'target': target, 'action': make_action_result(action_id=action_name, action_type=ACTION_COMPOSITE_INPUT, status='failed', method='click_ctrl_a_backspace_clipboard_paste', target=target, text=clean, error=str(click_result.get('reason') or click_result.get('error') or 'field_click_failed'), result={'click': click_result, 'aborted_before_keyboard_input': True})}
    _ops().add_friend_paced_pause('input', reason=f'after_{action_name}_click_before_select_all')
    _ops().hotkey(win32con.VK_CONTROL, ord('A'))
    _ops().add_friend_paced_pause('input', reason=f'after_{action_name}_select_all_before_backspace')
    _ops().key_press(win32con.VK_BACK)
    _ops().add_friend_paced_pause('input', reason=f'after_{action_name}_clear_before_clipboard')
    _ops().clipboard_copy(clean)
    _ops().add_friend_paced_pause('input', reason=f'after_{action_name}_clipboard_copy_before_paste')
    _ops().hotkey(win32con.VK_CONTROL, ord('V'))
    _ops().add_friend_paced_pause('input', reason=f'after_{action_name}_paste')
    return {'ok': bool(click_result.get('ok')), 'method': 'click_ctrl_a_backspace_clipboard_paste', 'text_length': len(clean), 'click': click_result, 'action': make_action_result(action_id=action_name, action_type=ACTION_COMPOSITE_INPUT, status='completed' if bool(click_result.get('ok')) else 'failed', method='click_ctrl_a_backspace_clipboard_paste', target=target, text=clean, result={'click': click_result})}


def fill_add_friend_invite_form_and_confirm(hwnd: int, output_dir: Path, *, verify_message: str, remark_name: str, remark_code: str) -> dict[str, Any]:
    clean_verify_message = str(verify_message or '').strip()
    clean_remark_name = str(remark_name or '').strip()
    clean_remark_code = str(remark_code or '').strip()
    remark_code_valid = bool(clean_remark_code and clean_remark_code in clean_remark_name)
    timings: list[dict[str, Any]] = []
    pause_seconds = _ops().add_friend_paced_pause('verify', reason='before_invite_form_capture')
    timings.append({'name': 'before_invite_form_capture_pause', 'seconds': round(pause_seconds, 3)})
    before_shot, before_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label='add_friend_invite_form_before_fill_window')
    before_ocr_started_at = time.perf_counter()
    before_items = _ops().run_ocr_on_screen_region(before_shot, [0, 0, before_shot.size[0], before_shot.size[1]])
    timings.append({'name': 'invite_form_before_fill_ocr', 'seconds': round(time.perf_counter() - before_ocr_started_at, 3), 'ocr_count': len(before_items)})
    before_targets_map = add_friend_invite_form_targets(before_shot.size, before_items)
    before_targets = list(before_targets_map.values())
    before_annotated_path = output_dir / 'add_friend_invite_form_before_fill_window_annotated.png'
    before_annotated = draw_add_friend_screen_annotation(before_shot, ocr_items=before_items, targets=before_targets, output_path=before_annotated_path, window_rect=None)
    greeting_started_at = time.perf_counter()
    greeting_result = _ops().paste_invite_form_text(hwnd, before_targets_map['invite_greeting_textarea'], clean_verify_message, action_name='invite_greeting')
    timings.append({'name': 'fill_invite_greeting_text', 'seconds': round(time.perf_counter() - greeting_started_at, 3), 'result': greeting_result})
    remark_started_at = time.perf_counter()
    remark_result = _ops().paste_invite_form_text(hwnd, before_targets_map['invite_remark_input'], clean_remark_name, action_name='invite_remark')
    timings.append({'name': 'fill_invite_remark_text', 'seconds': round(time.perf_counter() - remark_started_at, 3), 'result': remark_result})
    pause_seconds = _ops().add_friend_paced_pause('verify', reason='after_invite_form_fill_before_review_capture')
    timings.append({'name': 'after_invite_form_fill_before_review_capture_pause', 'seconds': round(pause_seconds, 3)})
    filled_shot, filled_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label='add_friend_invite_form_filled_before_confirm_window')
    filled_ocr_started_at = time.perf_counter()
    filled_items = _ops().run_ocr_on_screen_region(filled_shot, [0, 0, filled_shot.size[0], filled_shot.size[1]])
    timings.append({'name': 'invite_form_filled_ocr', 'seconds': round(time.perf_counter() - filled_ocr_started_at, 3), 'ocr_count': len(filled_items)})
    filled_targets_map = add_friend_invite_form_targets(filled_shot.size, filled_items)
    filled_targets = list(filled_targets_map.values())
    field_verification = invite_form_field_verification(verify_message=clean_verify_message, remark_name=clean_remark_name, remark_code=clean_remark_code, ocr_items=filled_items)
    filled_annotated_path = output_dir / 'add_friend_invite_form_filled_before_confirm_window_annotated.png'
    filled_annotated = draw_add_friend_screen_annotation(filled_shot, ocr_items=filled_items, targets=filled_targets, output_path=filled_annotated_path, window_rect=None)
    if not field_verification.get('ok'):
        final_status = mapped_add_friend_failed_result(state='invite_field_verification_failed', error_code=ERROR_INVITE_FIELD_VERIFICATION_FAILED, current_step='invite_fields_review', field_verification=field_verification)
        timings.append({'name': 'invite_field_verification_gate', 'seconds': 0.0, 'result': field_verification})
        return {'ok': False, 'state': str(final_status.get('state') or 'invite_field_verification_failed'), 'task_status': str(final_status.get('task_status') or 'failed'), 'result_code': str(final_status.get('result_code') or ''), 'error_code': str(final_status.get('error_code') or ERROR_INVITE_FIELD_VERIFICATION_FAILED), 'current_step': str(final_status.get('current_step') or 'invite_fields_review'), 'verify_message': clean_verify_message, 'remark_name': clean_remark_name, 'remark_code': clean_remark_code, 'remark_code_valid': remark_code_valid, 'legacy_remark_fallback': False, 'validation_errors': [], 'before': {'screenshot_path': before_path, 'annotated_path': before_annotated, 'targets': before_targets, 'ocr_items': add_friend_ocr_snapshots(before_items, before_shot.size)}, 'filled': {'screenshot_path': filled_path, 'annotated_path': filled_annotated, 'targets': filled_targets, 'ocr_items': add_friend_ocr_snapshots(filled_items, filled_shot.size), 'field_verification': field_verification}, 'after': {'screenshot_path': '', 'annotated_path': '', 'ocr_items': [], 'final_status': final_status, 'skipped': True, 'reason': 'field_verification_failed_before_confirm'}, 'greeting': greeting_result, 'remark_fill': remark_result, 'field_verification': field_verification, 'confirm': {'ok': False, 'skipped': True, 'reason': 'field_verification_failed_before_confirm'}, 'server_report_payload': final_status.get('server_report_payload') or {'task.status': 'failed', 'task.error_code': ERROR_INVITE_FIELD_VERIFICATION_FAILED, 'task.current_step': 'invite_fields_review'}, 'timings': timings}
    pause_seconds = _ops().add_friend_paced_pause('critical_click', reason='before_invite_confirm_click')
    timings.append({'name': 'before_invite_confirm_click_pause', 'seconds': round(pause_seconds, 3)})
    confirm_started_at = time.perf_counter()
    confirm_target = filled_targets_map['invite_confirm_button']
    confirm_result = _ops().human_window_image_click_in_bounds(hwnd, int(confirm_target.get('x') or 0), int(confirm_target.get('y') or 0), bounds=list(confirm_target.get('click_bounds') or []), action_name='invite_confirm_button_click')
    timings.append({'name': 'invite_confirm_button_click', 'seconds': round(time.perf_counter() - confirm_started_at, 3), 'result': confirm_result})
    pause_seconds = _ops().add_friend_paced_pause('verify', reason='after_invite_confirm_click_before_capture')
    timings.append({'name': 'after_invite_confirm_click_before_capture_pause', 'seconds': round(pause_seconds, 3)})
    after_shot, after_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label='add_friend_invite_form_after_confirm_window')
    after_items = _ops().run_ocr_on_screen_region(after_shot, [0, 0, after_shot.size[0], after_shot.size[1]])
    final_status = classify_add_friend_after_confirm_surface(after_items, after_shot.size, confirm_ok=bool(confirm_result.get('ok')))
    after_annotated_path = output_dir / 'add_friend_invite_form_after_confirm_window_annotated.png'
    after_annotated = draw_add_friend_screen_annotation(after_shot, ocr_items=after_items, targets=[], output_path=after_annotated_path, window_rect=None)
    return {'ok': bool(greeting_result.get('ok')) and bool(remark_result.get('ok')) and bool(confirm_result.get('ok')), 'state': str(final_status.get('state') or 'invite_confirm_clicked'), 'task_status': str(final_status.get('task_status') or 'running'), 'result_code': str(final_status.get('result_code') or ''), 'error_code': str(final_status.get('error_code') or ''), 'current_step': str(final_status.get('current_step') or 'invite_confirm_clicked'), 'verify_message': clean_verify_message, 'remark_name': clean_remark_name, 'remark_code': clean_remark_code, 'remark_code_valid': remark_code_valid, 'legacy_remark_fallback': False, 'validation_errors': [], 'before': {'screenshot_path': before_path, 'annotated_path': before_annotated, 'targets': before_targets, 'ocr_items': add_friend_ocr_snapshots(before_items, before_shot.size)}, 'filled': {'screenshot_path': filled_path, 'annotated_path': filled_annotated, 'targets': filled_targets, 'ocr_items': add_friend_ocr_snapshots(filled_items, filled_shot.size), 'field_verification': field_verification}, 'after': {'screenshot_path': after_path, 'annotated_path': after_annotated, 'ocr_items': add_friend_ocr_snapshots(after_items, after_shot.size), 'final_status': final_status}, 'greeting': greeting_result, 'remark_fill': remark_result, 'field_verification': field_verification, 'confirm': confirm_result, 'server_report_payload': final_status.get('server_report_payload') or {'task.current_step': 'invite_confirm_clicked'}, 'timings': timings}


def type_add_friend_query_like_human_for_entry(query: str) -> dict[str, Any]:
    clean = str(query or '')
    typed = 0
    if not clean:
        return {'ok': False, 'reason': 'empty_query', 'typed_chars': 0}
    if not re.fullmatch('\\d{5,20}', clean):
        try:
            _ops().clipboard_copy(clean)
            _ops().humanized_action_sleep(260, 620)
            _ops().hotkey(win32con.VK_CONTROL, ord('V'))
            _ops().humanized_action_sleep(260, 680)
            return {'ok': True, 'method': 'clipboard_paste_full_query', 'typed_chars': len(clean)}
        except Exception as exc:
            return {'ok': False, 'method': 'clipboard_paste_full_query', 'error': repr(exc), 'typed_chars': 0}
    try:
        for index, char in enumerate(clean, start=1):
            _ops().key_press(add_friend_virtual_key_for_digit(char))
            typed += 1
            _ops().humanized_action_sleep(bounded_int(os.getenv('WECHAT_WIN32_OCR_ADD_FRIEND_CHAR_DELAY_MIN_MS'), default=90, minimum=40, maximum=500), bounded_int(os.getenv('WECHAT_WIN32_OCR_ADD_FRIEND_CHAR_DELAY_MAX_MS'), default=210, minimum=80, maximum=800))
            if index % random.randint(4, 6) == 0 and index < len(clean):
                _ops().humanized_action_sleep(240, 520)
    except Exception as exc:
        return {'ok': False, 'method': 'digit_key_by_key', 'error': repr(exc), 'typed_chars': typed}
    return {'ok': True, 'method': 'digit_key_by_key', 'typed_chars': typed}


def backspace_add_friend_query_chars(count: int) -> dict[str, Any]:
    deleted = 0
    try:
        for _ in range(max(0, int(count or 0))):
            _ops().key_press(win32con.VK_BACK)
            deleted += 1
            _ops().humanized_action_sleep(85, 220)
    except Exception as exc:
        return {'ok': False, 'error': repr(exc), 'deleted_chars': deleted}
    return {'ok': True, 'deleted_chars': deleted}


def is_add_friend_dialog_window_item(item: dict[str, Any], *, exclude_hwnd: int) -> bool:
    hwnd = int(item.get('hwnd') or 0)
    if not hwnd or hwnd == int(exclude_hwnd or 0):
        return False
    if not item.get('visible'):
        return False
    title = _ops().normalize_wechat_title(str(item.get('title') or ''))
    if '添加朋友' in title or '添加好友' in title:
        return True
    try:
        geometry = _ops().get_window_geometry(hwnd)
    except Exception:
        return False
    width = int(geometry.get('width') or 0)
    height = int(geometry.get('height') or 0)
    return 300 <= width <= 620 and 360 <= height <= 760


def wait_for_add_friend_dialog_window(*, exclude_hwnd: int, output_dir: Path, timeout_ms: int=5000) -> dict[str, Any]:
    started = time.perf_counter()
    attempts: list[dict[str, Any]] = []
    deadline = started + max(800, int(timeout_ms)) / 1000.0
    while time.perf_counter() < deadline:
        probe = _ops().probe_wechat_windows()
        candidates = [item for item in probe.get('visible_windows') or [] if _ops().is_add_friend_dialog_window_item(item, exclude_hwnd=exclude_hwnd)]
        candidates.sort(key=lambda item: (1 if '添加朋友' in _ops().normalize_wechat_title(str(item.get('title') or '')) else 0, int(_ops().get_window_geometry(int(item.get('hwnd') or 0)).get('width') or 0) if int(item.get('hwnd') or 0) else 0), reverse=True)
        for item in candidates:
            candidate_hwnd = int(item.get('hwnd') or 0)
            if not candidate_hwnd:
                continue
            try:
                screenshot, screenshot_path = _ops().capture_wechat_window_visible_screen(candidate_hwnd, artifact_dir=str(output_dir), label='add_friend_dialog_window_candidate')
                region = add_friend_page_search_region(screenshot.size)
                ocr_started_at = time.perf_counter()
                ocr_items = _ops().run_ocr_on_screen_region(screenshot, region)
                detection = add_friend_dialog_surface_detected(ocr_items)
                annotated_path = output_dir / f'add_friend_dialog_window_candidate_{candidate_hwnd}_annotated.png'
                annotated = draw_add_friend_screen_annotation(screenshot, ocr_items=ocr_items, targets=[], output_path=annotated_path, window_rect=None)
                attempt = {'hwnd': candidate_hwnd, 'window': item, 'screenshot_path': screenshot_path, 'annotated_path': annotated, 'ocr_region': region, 'ocr_seconds': round(time.perf_counter() - ocr_started_at, 3), 'ocr_count': len(ocr_items), 'detection': detection, 'geometry': _ops().get_window_geometry(candidate_hwnd)}
                attempts.append(attempt)
                if detection.get('detected'):
                    return {'ok': True, 'hwnd': candidate_hwnd, 'window': item, 'geometry': attempt['geometry'], 'screenshot_path': screenshot_path, 'annotated_path': annotated, 'ocr_items': add_friend_ocr_snapshots(ocr_items, screenshot.size), 'detection': detection, 'attempts': attempts, 'seconds': round(time.perf_counter() - started, 3)}
            except Exception as exc:
                attempts.append({'hwnd': candidate_hwnd, 'window': item, 'error': repr(exc)})
        _ops().humanized_action_sleep(240, 520)
    return {'ok': False, 'reason': 'add_friend_dialog_window_not_found', 'attempts': attempts, 'seconds': round(time.perf_counter() - started, 3)}


def is_add_friend_invite_form_window_item(item: dict[str, Any], *, exclude_hwnds: set[int]) -> bool:
    hwnd = int(item.get('hwnd') or 0)
    if not hwnd or hwnd in exclude_hwnds:
        return False
    if not item.get('visible'):
        return False
    title = _ops().normalize_wechat_title(str(item.get('title') or ''))
    if '申请添加朋友' in title or '朋友验证' in title:
        return True
    try:
        geometry = _ops().get_window_geometry(hwnd)
    except Exception:
        return False
    width = int(geometry.get('width') or 0)
    height = int(geometry.get('height') or 0)
    return 360 <= width <= 660 and 580 <= height <= 980


def wait_for_add_friend_invite_form_window(*, exclude_hwnds: set[int], output_dir: Path, timeout_ms: int=6000) -> dict[str, Any]:
    started = time.perf_counter()
    attempts: list[dict[str, Any]] = []
    deadline = started + max(1000, int(timeout_ms)) / 1000.0
    while time.perf_counter() < deadline:
        probe = _ops().probe_wechat_windows()
        candidates = [item for item in probe.get('visible_windows') or [] if _ops().is_add_friend_invite_form_window_item(item, exclude_hwnds=exclude_hwnds)]
        candidates.sort(key=lambda item: (1 if '申请添加朋友' in _ops().normalize_wechat_title(str(item.get('title') or '')) else 0, int(_ops().get_window_geometry(int(item.get('hwnd') or 0)).get('height') or 0) if int(item.get('hwnd') or 0) else 0), reverse=True)
        for item in candidates:
            candidate_hwnd = int(item.get('hwnd') or 0)
            if not candidate_hwnd:
                continue
            try:
                screenshot, screenshot_path = _ops().capture_wechat_window_visible_screen(candidate_hwnd, artifact_dir=str(output_dir), label='add_friend_invite_form_window_candidate')
                ocr_started_at = time.perf_counter()
                ocr_items = _ops().run_ocr_on_screen_region(screenshot, [0, 0, screenshot.size[0], screenshot.size[1]])
                detection = add_friend_invite_form_surface_detected(ocr_items)
                annotated_path = output_dir / f'add_friend_invite_form_window_candidate_{candidate_hwnd}_annotated.png'
                annotated = draw_add_friend_screen_annotation(screenshot, ocr_items=ocr_items, targets=list(add_friend_invite_form_targets(screenshot.size, ocr_items).values()), output_path=annotated_path, window_rect=None)
                attempt = {'hwnd': candidate_hwnd, 'window': item, 'screenshot_path': screenshot_path, 'annotated_path': annotated, 'ocr_seconds': round(time.perf_counter() - ocr_started_at, 3), 'ocr_count': len(ocr_items), 'detection': detection, 'geometry': _ops().get_window_geometry(candidate_hwnd)}
                attempts.append(attempt)
                if detection.get('detected'):
                    return {'ok': True, 'hwnd': candidate_hwnd, 'window': item, 'geometry': attempt['geometry'], 'screenshot_path': screenshot_path, 'annotated_path': annotated, 'ocr_items': add_friend_ocr_snapshots(ocr_items, screenshot.size), 'detection': detection, 'attempts': attempts, 'seconds': round(time.perf_counter() - started, 3)}
            except Exception as exc:
                attempts.append({'hwnd': candidate_hwnd, 'window': item, 'error': repr(exc)})
        _ops().humanized_action_sleep(240, 520)
    return {'ok': False, 'reason': 'add_friend_invite_form_window_not_found', 'attempts': attempts, 'seconds': round(time.perf_counter() - started, 3)}


def click_add_friend_menu_entry_and_capture(hwnd: int, output_dir: Path, *, menu_targets: list[dict[str, Any]]) -> dict[str, Any]:
    target = add_friend_target_by_name(menu_targets, 'add_friend_menu_entry')
    if target is None:
        return {'clicked': False, 'reason': 'add_friend_menu_entry_not_found', 'target': None}
    if str(target.get('source') or '') != 'ocr_popup_menu_item':
        return {'clicked': False, 'reason': 'add_friend_menu_entry_requires_ocr_confirmation', 'target': target}
    click_bounds = target.get('click_bounds')
    screen_bounds = target.get('click_screen_bounds')
    if not (isinstance(click_bounds, list) and len(click_bounds) >= 4 and isinstance(screen_bounds, list) and (len(screen_bounds) >= 4)):
        return {'clicked': False, 'reason': 'add_friend_menu_entry_missing_click_bounds', 'target': target}
    target_x = int(target.get('x') or 0)
    target_y = int(target.get('y') or 0)
    if not point_in_bounds(target_x, target_y, click_bounds):
        return {'clicked': False, 'reason': 'add_friend_menu_entry_target_outside_click_bounds', 'target': target, 'click_bounds': click_bounds}
    screen_x, screen_y = add_friend_target_screen_point(target)
    if not point_in_bounds(screen_x, screen_y, screen_bounds):
        screen_x, screen_y = clamp_point_to_bounds(screen_x, screen_y, screen_bounds)
    timings: list[dict[str, Any]] = []
    pause_seconds = _ops().add_friend_paced_pause('critical_click', reason='before_add_friend_menu_hover')
    timings.append({'name': 'before_add_friend_menu_hover_pause', 'seconds': round(pause_seconds, 3)})
    hover_started_at = time.perf_counter()
    hover_result = _ops().human_screen_hover(screen_x, screen_y, action_name='add_friend_menu_entry_hover')
    timings.append({'name': 'add_friend_menu_entry_hover', 'seconds': round(time.perf_counter() - hover_started_at, 3), 'result': hover_result})
    pause_seconds = _ops().add_friend_paced_pause('critical_click', reason='after_add_friend_menu_hover_before_click')
    timings.append({'name': 'after_add_friend_menu_hover_before_click_pause', 'seconds': round(pause_seconds, 3)})
    click_started_at = time.perf_counter()
    click_result = _ops().human_screen_click_in_bounds(screen_x, screen_y, bounds=screen_bounds, action_name='add_friend_menu_entry_click')
    timings.append({'name': 'add_friend_menu_entry_click', 'seconds': round(time.perf_counter() - click_started_at, 3), 'result': click_result})
    pause_seconds = _ops().add_friend_paced_pause('verify', reason='after_add_friend_menu_click_before_screen_capture')
    timings.append({'name': 'after_add_friend_menu_click_before_screen_capture_pause', 'seconds': round(pause_seconds, 3)})
    dialog_probe = _ops().wait_for_add_friend_dialog_window(exclude_hwnd=hwnd, output_dir=output_dir)
    next_hwnd = int(dialog_probe.get('hwnd') or 0) if dialog_probe.get('ok') else 0
    evidence_hwnd = next_hwnd or hwnd
    capture_error = ''
    dialog_handle_invalid = False
    try:
        geometry = _ops().get_window_geometry(evidence_hwnd)
        screenshot, screenshot_path = _ops().capture_wechat_window_visible_screen(evidence_hwnd, artifact_dir=str(output_dir), label='add_friend_menu_entry_after_click_window')
    except Exception as exc:
        capture_error = repr(exc)
        if evidence_hwnd == hwnd:
            return {'clicked': False, 'menu_clicked': bool(click_result.get('ok')), 'next_hwnd': 0, 'dialog_window': dialog_probe, 'reason': 'add_friend_menu_entry_after_click_capture_failed', 'target': target, 'hover': hover_result, 'click': click_result, 'timings': timings, 'error': capture_error}
        dialog_handle_invalid = True
        stale_hwnd = evidence_hwnd
        evidence_hwnd = hwnd
        next_hwnd = 0
        geometry = _ops().get_window_geometry(evidence_hwnd)
        screenshot, screenshot_path = _ops().capture_wechat_window_visible_screen(evidence_hwnd, artifact_dir=str(output_dir), label='add_friend_menu_entry_after_click_fallback_main_window')
        dialog_probe = {**dialog_probe, 'stale_hwnd': stale_hwnd, 'fallback_hwnd': hwnd, 'fallback_reason': 'dialog_window_handle_invalid', 'fallback_error': capture_error}
    ocr_items: list[dict[str, Any]] = []
    readiness = {'ok': bool(dialog_probe.get('ok')) and (not dialog_handle_invalid), 'stage': 'after_add_friend_menu_entry_click', 'capture_mode': 'wechat_window_visible', 'dialog_window_found': bool(dialog_probe.get('ok')), 'dialog_handle_invalid': dialog_handle_invalid, 'ocr_count': 0, 'ocr_skipped': True}
    annotated_path = output_dir / 'add_friend_menu_entry_after_click_window_annotated.png'
    local_target = dict(target)
    if evidence_hwnd != hwnd:
        local_target['annotation_x'] = 0
        local_target['annotation_y'] = 0
    annotated = draw_add_friend_screen_annotation(screenshot, ocr_items=ocr_items, targets=[] if evidence_hwnd != hwnd else [target], output_path=annotated_path, window_rect=None)
    return {'clicked': bool(click_result.get('ok')) and bool(dialog_probe.get('ok')) and (not dialog_handle_invalid), 'menu_clicked': bool(click_result.get('ok')), 'next_hwnd': next_hwnd, 'dialog_window': dialog_probe, 'reason': 'add_friend_dialog_window_handle_invalid_after_menu_click' if dialog_handle_invalid else 'add_friend_dialog_window_ready' if dialog_probe.get('ok') else 'add_friend_dialog_window_not_found_after_menu_click', 'target': target, 'hover': hover_result, 'click': click_result, 'timings': timings, 'error': capture_error, 'geometry': geometry, 'screenshot_path': screenshot_path, 'annotated_path': annotated, 'readiness': readiness, 'ocr_items': add_friend_ocr_snapshots(ocr_items, screenshot.size)}


def input_add_friend_query_and_search(hwnd: int, output_dir: Path, *, query: str, verify_message: str='', remark_name: str='', remark_code: str='') -> dict[str, Any]:
    if not query:
        return {'ok': False, 'reason': 'empty_query'}
    timings: list[dict[str, Any]] = []
    geometry = _ops().get_window_geometry(hwnd)
    page_shot, page_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label='add_friend_page_before_input_window')
    search_region = add_friend_page_search_region(page_shot.size)
    ocr_started_at = time.perf_counter()
    page_items = _ops().run_ocr_on_screen_region(page_shot, search_region)
    timings.append({'name': 'add_friend_page_search_region_ocr', 'seconds': round(time.perf_counter() - ocr_started_at, 3), 'bounds': search_region, 'ocr_count': len(page_items)})
    search_targets = find_add_friend_page_search_targets(page_items, page_shot.size, screenshot=page_shot)
    targets = [search_targets['input'], search_targets['button']]
    page_annotated_path = output_dir / 'add_friend_page_before_input_window_annotated.png'
    page_annotated = draw_add_friend_screen_annotation(page_shot, ocr_items=page_items, targets=targets, output_path=page_annotated_path, window_rect=None)
    input_target = search_targets['input']
    input_click_started_at = time.perf_counter()
    input_bounds = input_target.get('click_bounds')
    if isinstance(input_bounds, list) and len(input_bounds) >= 4:
        input_click_result = _ops().human_window_image_click_in_bounds(hwnd, int(input_target.get('x') or 0), int(input_target.get('y') or 0), bounds=input_bounds, action_name='add_friend_search_input_click')
    else:
        _ops().human_window_image_click(hwnd, int(input_target.get('x') or 0), int(input_target.get('y') or 0))
        input_click_result = {'ok': True, 'x': int(input_target.get('x') or 0), 'y': int(input_target.get('y') or 0), 'bounds': None}
    timings.append({'name': 'add_friend_search_input_click', 'seconds': round(time.perf_counter() - input_click_started_at, 3), 'result': input_click_result})
    pause_seconds = _ops().add_friend_human_pause(140, 360, reason='input:after_add_friend_search_input_click_before_typing')
    timings.append({'name': 'after_add_friend_search_input_click_before_typing_pause', 'seconds': round(pause_seconds, 3)})
    clear_started_at = time.perf_counter()
    search_x = int(input_target.get('x') or 0)
    search_y = int(input_target.get('y') or 0)
    initial_empty = add_friend_search_input_empty_in_items(page_items, page_shot.size)
    clear_verify_payload: dict[str, Any] | None = None
    if initial_empty.get('ok'):
        clear_result = {'ok': True, 'method': 'skip_clear_placeholder_visible', 'empty_check': initial_empty}
    else:
        clear_result = _ops().clear_add_friend_sidebar_search_box(hwnd, search_x, search_y, target_hint=query)
    timings.append({'name': 'clear_add_friend_search_box', 'seconds': round(time.perf_counter() - clear_started_at, 3), 'result': clear_result})
    if not clear_result.get('ok'):
        return {'ok': False, 'state': 'search_input_clear_failed', 'query': query, 'error_code': 'ADD_FRIEND_SEARCH_INPUT_CLEAR_FAILED', 'page': {'screenshot_path': page_path, 'annotated_path': page_annotated, 'ocr_items': add_friend_ocr_snapshots(page_items, page_shot.size), 'targets': targets, 'input_empty_before_clear': initial_empty}, 'clear_result': clear_result, 'timings': timings}
    if not initial_empty.get('ok'):
        clear_verify_started_at = time.perf_counter()
        clear_verify_shot, clear_verify_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label='add_friend_search_input_after_clear_window')
        clear_verify_region = add_friend_page_search_region(clear_verify_shot.size)
        clear_verify_items = _ops().run_ocr_on_screen_region(clear_verify_shot, clear_verify_region)
        clear_verify = add_friend_search_input_empty_in_items(clear_verify_items, clear_verify_shot.size)
        clear_verify_annotated_path = output_dir / 'add_friend_search_input_after_clear_window_annotated.png'
        clear_verify_annotated = draw_add_friend_screen_annotation(clear_verify_shot, ocr_items=clear_verify_items, targets=targets, output_path=clear_verify_annotated_path, window_rect=None)
        clear_verify_payload = {'screenshot_path': clear_verify_path, 'annotated_path': clear_verify_annotated, 'ocr_items': add_friend_ocr_snapshots(clear_verify_items, clear_verify_shot.size), 'verify': clear_verify}
        timings.append({'name': 'clear_add_friend_search_box_verify', 'seconds': round(time.perf_counter() - clear_verify_started_at, 3), 'bounds': clear_verify_region, 'ocr_count': len(clear_verify_items), 'result': clear_verify})
        if not clear_verify.get('ok'):
            return {'ok': False, 'state': 'search_input_clear_unconfirmed', 'query': query, 'error_code': 'ADD_FRIEND_SEARCH_INPUT_CLEAR_FAILED', 'page': {'screenshot_path': page_path, 'annotated_path': page_annotated, 'ocr_items': add_friend_ocr_snapshots(page_items, page_shot.size), 'targets': targets, 'input_empty_before_clear': initial_empty}, 'latest_verify': {'screenshot_path': clear_verify_path, 'annotated_path': clear_verify_annotated, 'ocr_items': add_friend_ocr_snapshots(clear_verify_items, clear_verify_shot.size), 'verify': clear_verify}, 'clear_verify': clear_verify_payload, 'clear_result': clear_result, 'timings': timings}
    input_attempts: list[dict[str, Any]] = []
    verified = False
    latest_verify_shot = page_shot
    latest_verify_path = page_path
    latest_verify_annotated = page_annotated
    latest_verify_items = page_items
    latest_verify_result: dict[str, Any] = {'ok': False, 'reason': 'not_attempted'}
    for attempt in range(1, 3):
        type_started_at = time.perf_counter()
        type_result = _ops().type_add_friend_query_like_human_for_entry(query)
        timings.append({'name': f'type_query_attempt_{attempt}', 'seconds': round(time.perf_counter() - type_started_at, 3), 'result': type_result})
        wait_started_at = time.perf_counter()
        _ops().add_friend_wait_before_ocr('after_search_input_before_ocr')
        timings.append({'name': f'after_query_type_attempt_{attempt}_before_verify_pause', 'seconds': round(time.perf_counter() - wait_started_at, 3)})
        try:
            latest_verify_shot, latest_verify_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label=f'add_friend_query_verify_attempt_{attempt}_window')
        except Exception as exc:
            latest_verify_result = {'ok': False, 'reason': 'dialog_handle_invalid_during_query_verify', 'error': repr(exc), 'attempt': attempt, 'hwnd': int(hwnd or 0)}
            input_attempts.append({'attempt': attempt, 'type_result': type_result, 'verify': latest_verify_result, 'screenshot_path': latest_verify_path, 'annotated_path': latest_verify_annotated})
            return {'ok': False, 'state': 'dialog_handle_invalid', 'task_status': 'failed', 'error_code': ERROR_WECHAT_WINDOW_NOT_READY, 'current_step': 'query_input_verify', 'server_report_payload': add_friend_server_report_payload(task_status='failed', error_code=ERROR_WECHAT_WINDOW_NOT_READY, current_step='query_input_verify'), 'query': query, 'geometry': geometry, 'page': {'screenshot_path': page_path, 'annotated_path': page_annotated, 'ocr_items': add_friend_ocr_snapshots(page_items, page_shot.size), 'targets': targets}, 'input_attempts': input_attempts, 'latest_verify': {'screenshot_path': latest_verify_path, 'annotated_path': latest_verify_annotated, 'ocr_items': add_friend_ocr_snapshots(latest_verify_items, latest_verify_shot.size), 'verify': latest_verify_result}, 'timings': timings}
        verify_region = add_friend_page_search_region(latest_verify_shot.size)
        verify_ocr_started_at = time.perf_counter()
        latest_verify_items = _ops().run_ocr_on_screen_region(latest_verify_shot, verify_region)
        timings.append({'name': f'query_verify_region_ocr_attempt_{attempt}', 'seconds': round(time.perf_counter() - verify_ocr_started_at, 3), 'bounds': verify_region, 'ocr_count': len(latest_verify_items)})
        latest_verify_result = add_friend_query_visible_in_items(query, latest_verify_items)
        latest_verify_annotated_path = output_dir / f'add_friend_query_verify_attempt_{attempt}_window_annotated.png'
        latest_verify_annotated = draw_add_friend_screen_annotation(latest_verify_shot, ocr_items=latest_verify_items, targets=targets, output_path=latest_verify_annotated_path, window_rect=None)
        input_attempts.append({'attempt': attempt, 'type_result': type_result, 'verify': latest_verify_result, 'screenshot_path': latest_verify_path, 'annotated_path': latest_verify_annotated})
        if latest_verify_result.get('ok'):
            verified = True
            break
        if attempt < 2:
            retry_clear_started_at = time.perf_counter()
            retry_clear_result = _ops().clear_add_friend_sidebar_search_box(hwnd, search_x, search_y, target_hint=query)
            timings.append({'name': f'clear_query_attempt_{attempt}', 'seconds': round(time.perf_counter() - retry_clear_started_at, 3), 'result': retry_clear_result})
            if not retry_clear_result.get('ok'):
                break
            pause_seconds = _ops().add_friend_human_pause(140, 360, reason=f'input:after_query_clear_attempt_{attempt}')
            timings.append({'name': f'after_query_clear_attempt_{attempt}_pause', 'seconds': round(pause_seconds, 3)})
    if not verified:
        return {'ok': False, 'state': 'input_unconfirmed', 'query': query, 'page': {'screenshot_path': page_path, 'annotated_path': page_annotated, 'ocr_items': add_friend_ocr_snapshots(page_items, page_shot.size), 'targets': targets}, 'input_attempts': input_attempts, 'input_empty_before_clear': initial_empty, 'clear_result': clear_result, 'clear_verify': clear_verify_payload, 'latest_verify': {'screenshot_path': latest_verify_path, 'annotated_path': latest_verify_annotated, 'ocr_items': add_friend_ocr_snapshots(latest_verify_items, latest_verify_shot.size), 'verify': latest_verify_result}, 'timings': timings}
    button_target = search_targets['button']
    pause_seconds = _ops().add_friend_paced_pause('critical_click', reason='before_add_friend_search_button_click')
    timings.append({'name': 'before_add_friend_search_button_click_pause', 'seconds': round(pause_seconds, 3)})
    button_click_started_at = time.perf_counter()
    button_bounds = button_target.get('click_bounds')
    if isinstance(button_bounds, list) and len(button_bounds) >= 4:
        button_click_result = _ops().human_window_image_click_in_bounds(hwnd, int(button_target.get('x') or 0), int(button_target.get('y') or 0), bounds=button_bounds, action_name='add_friend_search_button_click')
    else:
        _ops().human_window_image_click(hwnd, int(button_target.get('x') or 0), int(button_target.get('y') or 0))
        button_click_result = {'ok': True, 'x': int(button_target.get('x') or 0), 'y': int(button_target.get('y') or 0), 'bounds': None}
    timings.append({'name': 'add_friend_search_button_click', 'seconds': round(time.perf_counter() - button_click_started_at, 3), 'result': button_click_result})
    pause_seconds = _ops().add_friend_paced_pause('verify', reason='after_add_friend_search_button_click_before_result_capture')
    timings.append({'name': 'after_add_friend_search_button_click_before_result_capture_pause', 'seconds': round(pause_seconds, 3)})
    settle_seconds = add_friend_wait_for_search_result_settle()
    timings.append({'name': 'after_add_friend_search_button_click_result_settle_wait', 'seconds': round(settle_seconds, 3)})
    result_shot, result_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label='add_friend_search_result_window')
    result_region = add_friend_search_result_region(result_shot.size)
    result_ocr_started_at = time.perf_counter()
    result_items = _ops().run_ocr_on_screen_region(result_shot, result_region)
    timings.append({'name': 'search_result_region_ocr', 'seconds': round(time.perf_counter() - result_ocr_started_at, 3), 'bounds': result_region, 'ocr_count': len(result_items)})
    result_annotated_path = output_dir / 'add_friend_search_result_window_annotated.png'
    result_annotated = draw_add_friend_screen_annotation(result_shot, ocr_items=result_items, targets=[button_target], output_path=result_annotated_path, window_rect=None)
    add_contact_result = _ops().click_add_contact_entry_from_search_result(hwnd, output_dir, result_shot=result_shot, result_path=result_path, result_items=result_items, query=query, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code)
    add_contact_timings = list(add_contact_result.get('timings') or []) if isinstance(add_contact_result, dict) else []
    timings.extend(add_contact_timings)
    return {'ok': bool(add_contact_result.get('ok')) if isinstance(add_contact_result, dict) else False, 'state': str(add_contact_result.get('state') or 'search_clicked') if isinstance(add_contact_result, dict) else 'search_clicked', 'query': query, 'task_status': add_contact_result.get('task_status') if isinstance(add_contact_result, dict) else None, 'result_code': add_contact_result.get('result_code') if isinstance(add_contact_result, dict) else '', 'error_code': add_contact_result.get('error_code') if isinstance(add_contact_result, dict) else '', 'current_step': add_contact_result.get('current_step') if isinstance(add_contact_result, dict) else 'searching_contact', 'server_report_payload': add_contact_result.get('server_report_payload') if isinstance(add_contact_result, dict) else None, 'geometry': geometry, 'page': {'screenshot_path': page_path, 'annotated_path': page_annotated, 'ocr_items': add_friend_ocr_snapshots(page_items, page_shot.size), 'targets': targets}, 'input_attempts': input_attempts, 'input_empty_before_clear': initial_empty, 'clear_result': clear_result, 'clear_verify': clear_verify_payload, 'result': {'screenshot_path': result_path, 'annotated_path': result_annotated, 'ocr_items': add_friend_ocr_snapshots(result_items, result_shot.size)}, 'add_contact_result': add_contact_result, 'timings': timings}


def write_add_friend_entry_click_review(output_dir: Path, payload: dict[str, Any]) -> str:
    rows: list[dict[str, Any]] = []
    if payload.get('validation_errors') or payload.get('state') == 'task_payload_invalid':
        rows.append({'title': '00 字段契约校验', 'purpose': '检查 add-friend-entry-click-plan 是否收到正式必填字段；校验失败时不会触达微信 UI。', 'expected': 'verify_message、remark_name、remark_code 均非空，且 remark_name 必须包含 remark_code。', 'raw': '', 'annotated': '', 'targets': [], 'detection': {'state': payload.get('state'), 'task_status': payload.get('task_status'), 'error_code': payload.get('error_code'), 'verify_message': payload.get('verify_message'), 'remark_name': payload.get('remark_name'), 'remark_code': payload.get('remark_code'), 'remark_code_valid': payload.get('remark_code_valid'), 'validation_errors': payload.get('validation_errors') or [], 'legacy_remark_fallback': payload.get('legacy_remark_fallback'), 'server_report_payload': payload.get('server_report_payload')}})
    before = payload.get('before') if isinstance(payload.get('before'), dict) else {}
    if before:
        plus_target = {}
        planned_targets = before.get('planned_targets') or []
        if isinstance(planned_targets, list):
            for target in planned_targets:
                if isinstance(target, dict) and str(target.get('name') or '') == 'plus_entry':
                    plus_target = target
                    break
        target_meta = plus_target.get('metadata') if isinstance(plus_target.get('metadata'), dict) else {}
        if plus_target:
            rows.append({'title': '01 微信窗口布局校准', 'purpose': '先划分微信窗口区域，再在左侧栏顶部右侧区域用图标形状识别 + 入口；旧坐标推算只保留为诊断参考。', 'expected': 'source=vision_plus_icon 且 executable=true；diagnostic_references 只能用于排查，不能作为点击点。', 'raw': before.get('screenshot_path'), 'annotated': before.get('annotated_path'), 'targets': [plus_target], 'detection': {'source': plus_target.get('source'), 'strategy': plus_target.get('strategy'), 'confidence': plus_target.get('confidence'), 'executable': plus_target.get('executable'), 'selected_reason': plus_target.get('selected_reason'), 'layout_calibration': target_meta.get('layout_calibration'), 'diagnostic_references': plus_target.get('diagnostic_references') or target_meta.get('diagnostic_references') or []}})
        rows.append({'title': '02 运行前屏幕标注', 'purpose': '检查 + 入口目标是否落在微信左侧栏顶部右侧区域；如果菜单本来已经打开，也会标注菜单项。', 'expected': '红色 T1 应落在视觉识别出的 + 上；不能点到搜索框、聊天区或 PowerShell。', 'raw': before.get('screenshot_path'), 'annotated': before.get('annotated_path'), 'targets': before.get('planned_targets') or [], 'detection': before.get('popup_detection')})
    for attempt in payload.get('click_attempts') or []:
        if not isinstance(attempt, dict):
            continue
        attempt_no = attempt.get('attempt')
        rows.append({'title': f'03 点击 + 后屏幕标注 attempt {attempt_no}', 'purpose': '检查点击 + 后是否出现快捷操作弹出菜单 plus_entry_popup_menu，并检查菜单里的下一步目标。', 'expected': '应能看到 发起群聊 / 添加朋友 / 新建笔记；红色 add_friend_menu_entry 应落在“添加朋友”这一行。', 'raw': attempt.get('screenshot_path'), 'annotated': attempt.get('annotated_path'), 'targets': attempt.get('planned_targets') or [], 'detection': attempt.get('popup_detection')})
    menu_click = payload.get('menu_click') if isinstance(payload.get('menu_click'), dict) else {}
    if menu_click:
        rows.append({'title': '04 点击添加朋友后屏幕标注', 'purpose': '检查鼠标是否已经通过轨迹移动到“添加朋友”，停顿后点击，并进入下一层添加朋友界面。', 'expected': '应不再停留在快捷操作弹出菜单；如果微信进入添加朋友/搜索页，说明这一格通过。', 'raw': menu_click.get('screenshot_path'), 'annotated': menu_click.get('annotated_path'), 'targets': [menu_click.get('target')] if isinstance(menu_click.get('target'), dict) else [], 'detection': {'clicked': menu_click.get('clicked'), 'hover': menu_click.get('hover'), 'click': menu_click.get('click'), 'readiness': menu_click.get('readiness')}})
    query_search = payload.get('query_search') if isinstance(payload.get('query_search'), dict) else {}
    page = query_search.get('page') if isinstance(query_search.get('page'), dict) else {}
    if page:
        rows.append({'title': '05 添加朋友页搜索框标注', 'purpose': '检查进入添加朋友页后，搜索输入框和搜索按钮定位是否合理。', 'expected': '红色 add_friend_search_input 应落在输入框，add_friend_search_button 应落在搜索按钮。', 'raw': page.get('screenshot_path'), 'annotated': page.get('annotated_path'), 'targets': page.get('targets') or [], 'detection': {'state': query_search.get('state'), 'query': query_search.get('query'), 'input_empty_before_clear': page.get('input_empty_before_clear'), 'clear_result': query_search.get('clear_result')}})
    clear_verify = query_search.get('clear_verify') if isinstance(query_search.get('clear_verify'), dict) else {}
    if clear_verify:
        rows.append({'title': '06 清空搜索框后复核', 'purpose': '检查旧手机号/微信号是否已经清空；如果输入框为空，OCR 应看到占位文案且不应再看到手机号数字。', 'expected': 'verify.ok=true；否则直接失败，不继续输入新手机号，避免旧手机号残留 + 新手机号拼接。', 'raw': clear_verify.get('screenshot_path'), 'annotated': clear_verify.get('annotated_path'), 'targets': page.get('targets') or [], 'detection': clear_verify.get('verify')})
    for attempt in query_search.get('input_attempts') or []:
        if not isinstance(attempt, dict):
            continue
        rows.append({'title': f"07 输入核对 attempt {attempt.get('attempt')}", 'purpose': '检查手机号/微信号是否完整输入，OCR 是否确认输入内容正确。', 'expected': 'verify.ok=true；如果 false，脚本会重新 Ctrl+A 清空后只重输一次；仍不通过则失败。', 'raw': attempt.get('screenshot_path'), 'annotated': attempt.get('annotated_path'), 'targets': page.get('targets') or [], 'detection': attempt.get('verify')})
    result = query_search.get('result') if isinstance(query_search.get('result'), dict) else {}
    if result:
        rows.append({'title': '08 点击搜索后结果区标注', 'purpose': '检查点击搜索后，结果区域是否出现内容。', 'expected': '截图中应能看到搜索后的页面内容；橙色框只标和搜索结果区域有关的 OCR。', 'raw': result.get('screenshot_path'), 'annotated': result.get('annotated_path'), 'targets': [], 'detection': {'state': query_search.get('state'), 'ok': query_search.get('ok')}})
    add_contact_result = query_search.get('add_contact_result') if isinstance(query_search.get('add_contact_result'), dict) else {}
    if add_contact_result:
        add_contact_before = add_contact_result.get('before') if isinstance(add_contact_result.get('before'), dict) else {}
        add_contact_after = add_contact_result.get('after') if isinstance(add_contact_result.get('after'), dict) else {}
        if add_contact_before:
            rows.append({'title': '09 点击添加到通讯录前标注', 'purpose': '检查搜索结果里是否识别到“添加到通讯录”按钮；搜不到用户时这里会展示失败状态。', 'expected': '搜到用户时红色 add_contact_entry_button 应落在“添加到通讯录”；搜不到时 detection.error_code=PHONE_NOT_FOUND。', 'raw': add_contact_before.get('screenshot_path'), 'annotated': add_contact_before.get('annotated_path'), 'targets': add_contact_before.get('targets') or [], 'detection': {'state': add_contact_result.get('state'), 'task_status': add_contact_result.get('task_status'), 'error_code': add_contact_result.get('error_code'), 'current_step': add_contact_result.get('current_step'), 'server_report_payload': add_contact_result.get('server_report_payload')}})
        elif add_contact_result.get('annotated_path') or add_contact_result.get('screenshot_path'):
            rows.append({'title': '09 搜索结果失败判定', 'purpose': '检查搜索结果是否为找不到用户，并输出任务失败上报字段。', 'expected': '找不到用户时 task_status=failed、error_code=PHONE_NOT_FOUND、current_step=searching_phone。', 'raw': add_contact_result.get('screenshot_path'), 'annotated': add_contact_result.get('annotated_path'), 'targets': add_contact_result.get('targets') or [], 'detection': {'state': add_contact_result.get('state'), 'task_status': add_contact_result.get('task_status'), 'error_code': add_contact_result.get('error_code'), 'current_step': add_contact_result.get('current_step'), 'server_report_payload': add_contact_result.get('server_report_payload'), 'not_found': add_contact_result.get('not_found')}})
        if add_contact_after:
            rows.append({'title': '10 点击添加到通讯录后截图', 'purpose': '检查脚本是否只点击了一次“添加到通讯录”，然后进入申请添加朋友表单。', 'expected': '应出现“申请添加朋友”表单；下一步会清空默认申请文案并填写固定话术。', 'raw': add_contact_after.get('screenshot_path'), 'annotated': add_contact_after.get('annotated_path'), 'targets': add_contact_after.get('targets') or [], 'detection': {'state': add_contact_result.get('state'), 'task_status': add_contact_result.get('task_status'), 'current_step': add_contact_result.get('current_step'), 'click': add_contact_result.get('click'), 'error_code': add_contact_result.get('error_code'), 'invite_form_probe': add_contact_result.get('invite_form_probe')}})
        invite_form = add_contact_result.get('invite_form') if isinstance(add_contact_result.get('invite_form'), dict) else {}
        if invite_form:
            invite_before = invite_form.get('before') if isinstance(invite_form.get('before'), dict) else {}
            invite_filled = invite_form.get('filled') if isinstance(invite_form.get('filled'), dict) else {}
            invite_after = invite_form.get('after') if isinstance(invite_form.get('after'), dict) else {}
            if invite_before:
                rows.append({'title': '11 申请表单填写前标注', 'purpose': '检查申请文案框、备注框、确定按钮三个操作区域是否落在正确位置。', 'expected': 'invite_greeting_textarea 应落在“发送添加朋友申请”文本框；invite_remark_input 应落在备注框；invite_confirm_button 应落在绿色确定按钮。', 'raw': invite_before.get('screenshot_path'), 'annotated': invite_before.get('annotated_path'), 'targets': invite_before.get('targets') or [], 'detection': {'state': invite_form.get('state'), 'verify_message': invite_form.get('verify_message'), 'remark_name': invite_form.get('remark_name'), 'remark_code': invite_form.get('remark_code'), 'remark_code_valid': invite_form.get('remark_code_valid'), 'validation_errors': invite_form.get('validation_errors') or [], 'legacy_remark_fallback': invite_form.get('legacy_remark_fallback')}})
            if invite_filled:
                rows.append({'title': '12 申请表单填写后/确定前截图', 'purpose': '检查申请语是否写入 verify_message，微信备注框是否写入 remark_name。', 'expected': '申请语应等于传入的 verify_message；备注名应等于传入的 remark_name，且 remark_name 包含 remark_code。', 'raw': invite_filled.get('screenshot_path'), 'annotated': invite_filled.get('annotated_path'), 'targets': invite_filled.get('targets') or [], 'detection': {'state': invite_form.get('state'), 'verify_message': invite_form.get('verify_message'), 'remark_name': invite_form.get('remark_name'), 'remark_code': invite_form.get('remark_code'), 'remark_code_valid': invite_form.get('remark_code_valid'), 'validation_errors': invite_form.get('validation_errors') or [], 'legacy_remark_fallback': invite_form.get('legacy_remark_fallback'), 'greeting': invite_form.get('greeting'), 'remark_fill': invite_form.get('remark_fill')}})
            if invite_after:
                rows.append({'title': '13 点击确定后截图', 'purpose': '检查脚本是否点击了“确定”，并用点击后的 OCR 结果复核最终任务状态。', 'expected': 'confirm.ok=true；只要没有明确失败/风控提示，就按 completed + invite_sent 上报；already_friend 只允许在发送邀请前的搜索结果/资料页阶段判定。', 'raw': invite_after.get('screenshot_path'), 'annotated': invite_after.get('annotated_path'), 'targets': [], 'detection': {'state': invite_form.get('state'), 'task_status': invite_form.get('task_status'), 'result_code': invite_form.get('result_code'), 'error_code': invite_form.get('error_code'), 'current_step': invite_form.get('current_step'), 'verify_message': invite_form.get('verify_message'), 'remark_name': invite_form.get('remark_name'), 'remark_code': invite_form.get('remark_code'), 'remark_code_valid': invite_form.get('remark_code_valid'), 'validation_errors': invite_form.get('validation_errors') or [], 'confirm': invite_form.get('confirm'), 'final_status': invite_after.get('final_status'), 'server_report_payload': invite_form.get('server_report_payload')}})
    after = payload.get('after') if isinstance(payload.get('after'), dict) else {}
    if after:
        rows.append({'title': '99 最终判定', 'purpose': '确认本次脚本有没有识别到快捷操作弹出菜单，以及后续是否具备点击“添加朋友”的目标。', 'expected': 'popup_detection.detected=true，planned_targets 里应包含 add_friend_menu_entry；menu_click.clicked=true。', 'raw': after.get('screenshot_path'), 'annotated': after.get('annotated_path'), 'targets': after.get('planned_targets') or [], 'detection': after.get('popup_detection')})
    summary = {'state': payload.get('state'), 'note': payload.get('note'), 'calibration_only': bool(payload.get('calibration_only')), 'no_clicks_performed': bool(payload.get('no_clicks_performed')), 'verify_message': payload.get('verify_message'), 'remark_name': payload.get('remark_name'), 'remark_code': payload.get('remark_code'), 'remark_code_valid': payload.get('remark_code_valid'), 'validation_errors': payload.get('validation_errors') or [], 'legacy_remark_fallback': payload.get('legacy_remark_fallback'), 'device_profile': payload.get('device_profile') or (payload.get('window_probe') or {}).get('device_profile'), 'operator_guard': payload.get('operator_guard') or (payload.get('window_probe') or {}).get('operator_guard'), 'operator_guard_release': payload.get('operator_guard_release') or {}, 'timings': payload.get('timings') or []}
    diagnostic_events = payload.get('diagnostic_events')
    existing_events = [event for event in diagnostic_events if isinstance(event, dict)] if isinstance(diagnostic_events, list) else []
    events = add_friend_entry_click_events_from_payload(payload, existing_events=existing_events)
    if not events:
        events = step_events_from_review_rows(rows)
    summary['event_source'] = 'flow_payload_events' if events else 'legacy_review_rows'
    if existing_events:
        summary['event_source'] = 'diagnostic_events+flow_payload_events'
    return write_step_event_report(output_dir=output_dir, json_name='add_friend_entry_click_review.json', html_name='add_friend_entry_click_review.html', title='add_friend 入口点击复核报告', description='本报告验证点击 +、点击“添加朋友”、输入手机号/微信号、点击搜索、点击“添加到通讯录”、填写申请表单并点击“确定”。', summary=summary, events=events)


def add_friend_surface_readiness(screenshot: Image.Image, ocr_items: list[dict[str, Any]], geometry: dict[str, Any], *, stage: str, require_main_surface: bool | None=None) -> dict[str, Any]:
    blank_render = _ops().detect_blank_render(screenshot, ocr_items, geometry=geometry)
    shell_probe = _ops().auxiliary_wechat_shell_like(ocr_items, geometry=geometry)
    screenshot_size = getattr(screenshot, 'size', (int(geometry.get('width') or 0), int(geometry.get('height') or 0)))
    blocking_prompt = add_friend_login_or_security_block(ocr_items, geometry=geometry, image_size=screenshot_size)
    if blocking_prompt.get('detected'):
        return {'ok': False, 'error_code': blocking_prompt.get('error_code') or ERROR_WECHAT_WINDOW_NOT_READY, 'state': blocking_prompt.get('state') or 'wechat_window_not_ready', 'stage': stage, 'reason': blocking_prompt.get('reason') or 'wechat_login_or_security_prompt', 'blocking_prompt': blocking_prompt, 'render_probe': blank_render, 'shell_probe': shell_probe, 'ocr_count': len(ocr_items), 'ocr_texts': [item.get('text') for item in ocr_items[:20]]}
    if blank_render.get('detected'):
        return {'ok': False, 'error_code': 'WECHAT_RENDER_NOT_READY', 'state': 'wechat_render_not_ready', 'stage': stage, 'reason': 'blank_render', 'render_probe': blank_render, 'shell_probe': shell_probe, 'ocr_count': len(ocr_items), 'ocr_texts': [item.get('text') for item in ocr_items[:20]]}
    if shell_probe.get('detected') and str(shell_probe.get('reason') or '') == 'title_only_shell':
        return {'ok': False, 'error_code': 'WECHAT_RENDER_NOT_READY', 'state': 'wechat_render_not_ready', 'stage': stage, 'reason': str(shell_probe.get('reason') or 'auxiliary_shell_window'), 'render_probe': blank_render, 'shell_probe': shell_probe, 'ocr_count': len(ocr_items), 'ocr_texts': [item.get('text') for item in ocr_items[:20]]}
    if len(ocr_items) <= 0:
        return {'ok': False, 'error_code': 'WECHAT_RENDER_NOT_READY', 'state': 'wechat_render_not_ready', 'stage': stage, 'reason': 'empty_ocr_surface', 'render_probe': blank_render, 'shell_probe': shell_probe, 'ocr_count': len(ocr_items), 'ocr_texts': []}
    main_surface = add_friend_main_entry_surface_evidence(ocr_items, screenshot_size)
    main_surface_required = stage == 'calibration' if require_main_surface is None else bool(require_main_surface)
    if main_surface_required and (not main_surface.get('ok')):
        return {'ok': False, 'error_code': ERROR_WECHAT_WINDOW_NOT_READY, 'state': 'wechat_main_surface_not_ready', 'stage': stage, 'reason': str(main_surface.get('reason') or 'add_friend_entry_surface_not_confirmed'), 'main_surface': main_surface, 'render_probe': blank_render, 'shell_probe': shell_probe, 'ocr_count': len(ocr_items), 'ocr_texts': [item.get('text') for item in ocr_items[:20]]}
    return {'ok': True, 'stage': stage, 'main_surface': main_surface, 'render_probe': blank_render, 'shell_probe': shell_probe, 'ocr_count': len(ocr_items)}


def add_friend_focus_guard_ready(focus_guard: dict[str, Any]) -> dict[str, Any]:
    reason = str((focus_guard or {}).get('reason') or '')
    ok = bool((focus_guard or {}).get('ok')) and reason in ADD_FRIEND_FOREGROUND_READY_REASONS
    return {'ok': ok, 'reason': reason or 'foreground_guard_missing', 'allowed_reasons': sorted(ADD_FRIEND_FOREGROUND_READY_REASONS), 'focus_guard': focus_guard or {}}


def add_friend_pre_click_readiness_decision(*, focus_guard: dict[str, Any], surface_readiness: dict[str, Any]) -> dict[str, Any]:
    focus_ready = _ops().add_friend_focus_guard_ready(focus_guard)
    if not focus_ready.get('ok'):
        return {'ok': False, 'state': 'wechat_window_not_foreground', 'error_code': ERROR_WECHAT_WINDOW_NOT_READY, 'reason': str(focus_ready.get('reason') or 'foreground_not_wechat_target'), 'focus_ready': focus_ready, 'surface_readiness': surface_readiness, 'no_clicks_performed': True}
    if not bool((surface_readiness or {}).get('ok')):
        return {'ok': False, 'state': str((surface_readiness or {}).get('state') or 'wechat_main_surface_not_ready'), 'error_code': str((surface_readiness or {}).get('error_code') or ERROR_WECHAT_WINDOW_NOT_READY), 'reason': str((surface_readiness or {}).get('reason') or 'add_friend_entry_surface_not_confirmed'), 'focus_ready': focus_ready, 'surface_readiness': surface_readiness or {}, 'no_clicks_performed': True}
    return {'ok': True, 'state': 'wechat_main_surface_ready', 'error_code': '', 'reason': 'foreground_and_main_surface_ready', 'focus_ready': focus_ready, 'surface_readiness': surface_readiness or {}}


def add_friend_pre_click_main_window_readiness(hwnd: int, geometry: dict[str, Any], *, route: str, output_dir: Path) -> dict[str, Any]:
    focus_guard = _ops().foreground_window_matches_target(hwnd)
    try:
        screenshot, screenshot_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label='add_friend_pre_click_main_window')
    except Exception as exc:
        surface_readiness = {'ok': False, 'state': 'wechat_main_surface_not_ready', 'error_code': ERROR_WECHAT_WINDOW_NOT_READY, 'stage': 'formal_pre_click', 'reason': 'pre_click_capture_failed', 'error': repr(exc), 'ocr_count': 0}
        decision = _ops().add_friend_pre_click_readiness_decision(focus_guard=focus_guard, surface_readiness=surface_readiness)
        return {**decision, 'stage': 'formal_pre_click', 'focus_guard': focus_guard, 'screenshot_path': '', 'annotated_path': '', 'ocr_count': 0}
    ocr_started_at = time.perf_counter()
    ocr_items = _ops().run_ocr_on_screen_region(screenshot, [0, 0, screenshot.size[0], screenshot.size[1]])
    ocr_seconds = round(time.perf_counter() - ocr_started_at, 3)
    plus_target = add_friend_plus_entry_target(geometry, screenshot.size, ocr_items, screenshot=screenshot, route_kind='windows')
    surface_readiness = _ops().add_friend_surface_readiness(screenshot, ocr_items, geometry, stage='formal_pre_click', require_main_surface=True)
    annotated_path = output_dir / 'add_friend_pre_click_main_window_annotated.png'
    annotated = draw_add_friend_screen_annotation(screenshot, ocr_items=ocr_items, targets=[plus_target], output_path=annotated_path, window_rect=None)
    decision = _ops().add_friend_pre_click_readiness_decision(focus_guard=focus_guard, surface_readiness=surface_readiness)
    return {**decision, 'stage': 'formal_pre_click', 'focus_guard': focus_guard, 'screenshot_path': screenshot_path, 'annotated_path': annotated, 'ocr_count': len(ocr_items), 'ocr_seconds': ocr_seconds, 'planned_targets': [plus_target], 'ocr_items': add_friend_ocr_snapshots(ocr_items, screenshot.size), 'surface_readiness': surface_readiness}


def persist_add_friend_operator_guard_release(payload: dict[str, Any], release: dict[str, Any]) -> None:
    plan_path = Path(str(payload.get('plan_path') or ''))
    if not str(plan_path):
        return
    try:
        if plan_path.exists():
            saved = json.loads(plan_path.read_text(encoding='utf-8'))
            if isinstance(saved, dict):
                if payload.get('operator_guard') and 'operator_guard' not in saved:
                    saved['operator_guard'] = payload.get('operator_guard')
                if payload.get('device_profile') and 'device_profile' not in saved:
                    saved['device_profile'] = payload.get('device_profile')
                saved['operator_guard_release'] = release
                payload.update({'diagnostic_events': saved.get('diagnostic_events') or payload.get('diagnostic_events')})
                plan_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
                review_path = _ops().write_add_friend_entry_click_review(plan_path.parent, saved)
                payload['review_path'] = review_path
                return
        plan_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        payload['review_path'] = _ops().write_add_friend_entry_click_review(plan_path.parent, payload)
    except Exception as exc:
        payload['operator_guard_release_persist_error'] = repr(exc)


def add_friend_calibration_payload(hwnd: int, probe: dict[str, Any], *, geometry: dict[str, Any], route: str, phone: str, wechat: str, verify_message: str, remark_name: str, remark_code: str, output_dir: Path) -> dict[str, Any]:
    screenshot, screenshot_path = _ops().capture_wechat_window_visible_screen(hwnd, artifact_dir=str(output_dir), label='add_friend_calibration_main_window')
    ocr_started_at = time.perf_counter()
    ocr_items = _ops().run_ocr_on_screen_region(screenshot, [0, 0, screenshot.size[0], screenshot.size[1]])
    plus_target = add_friend_plus_entry_target(geometry, screenshot.size, ocr_items, screenshot=screenshot, route_kind='windows')
    readiness = _ops().add_friend_surface_readiness(screenshot, ocr_items, geometry, stage='calibration')
    annotated_path = output_dir / 'add_friend_calibration_main_window_annotated.png'
    annotated = draw_add_friend_screen_annotation(screenshot, ocr_items=ocr_items, targets=[plus_target], output_path=annotated_path, window_rect=None)
    device_profile = _ops().add_friend_device_profile(hwnd, geometry=geometry, screenshot_size=screenshot.size, route=route)
    calibration_ready = bool(readiness.get('ok'))
    payload = {'ok': calibration_ready, 'state': 'calibration_ready' if readiness.get('ok') else 'calibration_surface_not_ready', 'task_status': 'calibration_only', 'result_code': '', 'error_code': '' if readiness.get('ok') else str(readiness.get('error_code') or ERROR_WECHAT_WINDOW_NOT_READY), 'current_step': 'calibration', 'route': route, 'query': normalize_add_friend_query(phone=phone, wechat=wechat), 'phone': phone, 'wechat': wechat, 'verify_message': verify_message, 'remark_name': remark_name, 'remark_code': remark_code, 'remark_code_valid': bool(str(remark_code or '') and str(remark_code or '') in str(remark_name or '')), 'calibration_only': True, 'no_clicks_performed': True, 'window_probe': probe, 'geometry': geometry, 'device_profile': device_profile, 'before': {'screenshot_path': screenshot_path, 'annotated_path': annotated, 'capture_mode': 'screen_visible', 'readiness': readiness, 'ocr_items': add_friend_ocr_snapshots(ocr_items, screenshot.size), 'planned_targets': [plus_target], 'ocr_seconds': round(time.perf_counter() - ocr_started_at, 3)}, 'timings': [{'name': 'calibration_full_window_ocr', 'seconds': round(time.perf_counter() - ocr_started_at, 3), 'ocr_count': len(ocr_items)}], 'diagnostic_events': [{'step_id': 'add_friend_calibration', 'title': 'add_friend 自适应校准', 'status': 'completed' if readiness.get('ok') else 'failed', 'state_before': 'main_window', 'state_after': 'calibration_ready' if readiness.get('ok') else 'calibration_surface_not_ready', 'artifacts': {'raw': screenshot_path, 'annotated': annotated}, 'targets': [plus_target], 'selected_target': plus_target, 'result': {'ok': bool(readiness.get('ok')), 'readiness': readiness, 'device_profile': device_profile, 'no_clicks_performed': True}}]}
    payload['plan_path'] = str(output_dir / ADD_FRIEND_ENTRY_CLICK_PLAN_JSON)
    payload['review_path'] = _ops().write_add_friend_entry_click_review(output_dir, payload)
    Path(str(payload['plan_path'])).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def click_add_friend_ocr_item(hwnd: int, item: dict[str, Any]) -> None:
    x, y = add_friend_item_center(item)
    _ops().add_friend_human_pause(650, 1450, reason='before_mouse_click')
    _ops().human_window_image_click(hwnd, x, y)
    _ops().add_friend_human_pause(900, 1900, reason='after_mouse_click')


def add_friend_wait_before_ocr(reason: str) -> None:
    _ops().add_friend_human_pause(1200, 2600, reason=reason)


def add_friend_wait_for_search_result_settle() -> float:
    delay_ms = bounded_int(
        os.getenv('WECHAT_WIN32_OCR_ADD_FRIEND_SEARCH_RESULT_SETTLE_MS'),
        default=1800,
        minimum=0,
        maximum=8000,
    )
    if delay_ms <= 0:
        return 0.0
    time.sleep(delay_ms / 1000.0)
    return delay_ms / 1000.0


def clear_add_friend_sidebar_search_box(hwnd: int, search_x: int, search_y: int, *, target_hint: str='') -> None:
    """Clear the WeChat sidebar search box with slow serialized key actions."""
    _ops().add_friend_human_pause(700, 1600, reason='before_search_clear_escape')
    _ops().key_press(win32con.VK_ESCAPE)
    _ops().add_friend_human_pause(900, 1800, reason='after_escape_before_search_click')
    _ops().human_window_image_click(hwnd, search_x, search_y)
    _ops().add_friend_human_pause(900, 1900, reason='after_search_click_before_clear_keys')
    default_backspaces = random.randint(1, 3)
    backspaces = bounded_int(os.getenv('WECHAT_WIN32_OCR_ADD_FRIEND_CLEAR_BACKSPACES'), default=default_backspaces, minimum=0, maximum=12)
    deletes = bounded_int(os.getenv('WECHAT_WIN32_OCR_ADD_FRIEND_CLEAR_DELETES'), default=0, minimum=0, maximum=4)
    for idx in range(backspaces):
        _ops().key_press(win32con.VK_BACK)
        _ops().add_friend_human_pause(120, 420, reason=f'clear_backspace_{idx + 1}')
    for idx in range(deletes):
        _ops().key_press(win32con.VK_DELETE)
        _ops().add_friend_human_pause(150, 460, reason=f'clear_delete_{idx + 1}')
    _ops().add_friend_human_pause(700, 1500, reason='after_search_clear_keys')


def type_add_friend_phone_query_like_human(hwnd: int, query: str, *, key_press_func: Any | None=None, window_guard_func: Any | None=None) -> dict[str, Any]:
    """Type a phone query as visible one-by-one key presses.

    This intentionally avoids SendInput Unicode batches and clipboard paste.
    The contact search surface is sensitive; each digit is separated by a
    random pause and periodic longer hesitation.
    """
    clean = re.sub('\\D+', '', str(query or ''))
    if not clean:
        return {'ok': False, 'method': 'add_friend_digit_keys', 'reason': 'empty_phone_query'}
    press = key_press_func or _ops().key_press
    typed = 0
    pause_after = random.randint(3, 5)
    try:
        for index, char in enumerate(clean, start=1):
            if window_guard_func is not None:
                guard = window_guard_func()
                if not guard.get('ok'):
                    return {'ok': False, 'method': 'add_friend_digit_keys', 'reason': 'window_lost_during_digit_input', 'window_guard': guard, 'typed_chars': typed}
            _ops().add_friend_human_pause(420, 1300, reason=f'before_digit_{index}')
            press(add_friend_virtual_key_for_digit(char))
            typed += 1
            _ops().add_friend_human_pause(520, 1500, reason=f'after_digit_{index}')
            if index >= pause_after and index < len(clean):
                _ops().add_friend_human_pause(1200, 2800, reason=f'digit_group_pause_{index}')
                pause_after += random.randint(3, 5)
    except Exception as exc:
        return {'ok': False, 'method': 'add_friend_digit_keys', 'error': repr(exc), 'typed_chars': typed}
    return {'ok': True, 'method': 'add_friend_digit_keys', 'typed_chars': typed}


def type_add_friend_search_query(hwnd: int, query: str) -> dict[str, Any]:
    if re.fullmatch('\\d{5,20}', str(query or '')):
        return _ops().type_add_friend_phone_query_like_human(hwnd, query, window_guard_func=lambda: _ops().basic_send_window_guard(hwnd))
    if not env_flag('WECHAT_WIN32_OCR_ADD_FRIEND_ALLOW_SENDINPUT_QUERY', default=False):
        return {'ok': False, 'method': 'add_friend_query_blocked', 'reason': 'non_numeric_query_requires_explicit_sendinput_opt_in'}
    _ops().add_friend_human_pause(900, 1800, reason='before_non_numeric_sendinput_query')
    result = _ops().type_sidebar_search_query(hwnd, query)
    _ops().add_friend_human_pause(1000, 2200, reason='after_non_numeric_sendinput_query')
    return result


def paste_add_friend_text_at_item(hwnd: int, item: dict[str, Any], text: str, image_size: tuple[int, int], *, x_offset: int=150) -> dict[str, Any]:
    if not add_friend_optional_field_fill_enabled():
        return {'ok': True, 'skipped': True, 'reason': 'optional_field_fill_disabled_by_default'}
    clean = str(text or '')
    if not clean:
        return {'ok': True, 'skipped': True, 'reason': 'empty_text'}
    width, _height = image_size
    base_x, base_y = add_friend_item_center(item)
    click_x = bounded_int(base_x + x_offset, default=base_x + x_offset, minimum=base_x + 20, maximum=max(base_x + 20, width - 42))
    click_y = base_y
    _ops().add_friend_human_pause(700, 1600, reason='before_field_click')
    _ops().human_window_image_click(hwnd, click_x, click_y)
    _ops().add_friend_human_pause(850, 1800, reason='after_field_click_before_keyboard')
    _ops().hotkey(win32con.VK_CONTROL, ord('A'))
    _ops().add_friend_human_pause(420, 1050, reason='after_select_all')
    _ops().clipboard_copy(clean)
    _ops().add_friend_human_pause(380, 980, reason='after_clipboard_copy')
    _ops().hotkey(win32con.VK_CONTROL, ord('V'))
    _ops().add_friend_human_pause(900, 1900, reason='after_clipboard_paste')
    return {'ok': True, 'method': 'clipboard_paste', 'x': click_x, 'y': click_y, 'length': len(clean)}


def fill_add_friend_optional_fields(hwnd: int, ocr_items: list[dict[str, Any]], image_size: tuple[int, int], *, remark: str, greeting: str) -> dict[str, Any]:
    result: dict[str, Any] = {'ok': True, 'greeting': {'skipped': True}, 'remark': {'skipped': True}}
    greeting_item = find_add_friend_action_item(ocr_items, ('发送添加朋友申请', '朋友验证', '申请添加朋友'), image_size, min_y_ratio=0.08, max_y_ratio=0.65)
    if greeting and greeting_item is not None:
        result['greeting'] = _ops().paste_add_friend_text_at_item(hwnd, greeting_item, greeting, image_size, x_offset=190)
    remark_item = find_add_friend_action_item(ocr_items, ('备注名', '备注'), image_size, min_y_ratio=0.15, max_y_ratio=0.8)
    if remark and remark_item is not None:
        result['remark'] = _ops().paste_add_friend_text_at_item(hwnd, remark_item, remark, image_size, x_offset=160)
    return result


def add_friend_device_profile(hwnd: int, *, geometry: dict[str, Any] | None=None, screenshot_size: tuple[int, int] | None=None, route: str='') -> dict[str, Any]:
    client_rect: dict[str, Any] = {}
    dpi_scale = 1.0
    screen: dict[str, Any] = {}
    virtual_screen: dict[str, Any] = {}
    monitors: list[dict[str, Any]] = []
    errors: dict[str, Any] = {}
    try:
        client_rect = _ops().get_window_client_geometry(hwnd)
    except Exception as exc:
        client_rect = {'error': repr(exc)}
    try:
        dpi_scale = _ops().window_dpi_scale(hwnd)
    except Exception as exc:
        errors['dpi_error'] = repr(exc)
    try:
        user32 = ctypes.windll.user32
        screen = {'width': int(user32.GetSystemMetrics(0)), 'height': int(user32.GetSystemMetrics(1))}
        virtual_screen = {'left': int(user32.GetSystemMetrics(76)), 'top': int(user32.GetSystemMetrics(77)), 'width': int(user32.GetSystemMetrics(78)), 'height': int(user32.GetSystemMetrics(79))}
    except Exception as exc:
        errors['screen_error'] = repr(exc)
    try:
        for monitor in win32api.EnumDisplayMonitors():
            _handle, _hdc, rect = monitor
            left, top, right, bottom = rect
            monitors.append({'left': int(left), 'top': int(top), 'right': int(right), 'bottom': int(bottom), 'width': int(right - left), 'height': int(bottom - top)})
    except Exception as exc:
        errors['monitor_error'] = repr(exc)
        monitors = []
    return win32_ocr_device_profile.build_device_profile(route=route, geometry=geometry, screenshot_size=screenshot_size, client_rect=client_rect, dpi_scale=dpi_scale, screen=screen, virtual_screen=virtual_screen, monitors=monitors, errors=errors)


def add_friend_entry_click_plan_payload(hwnd: int, probe: dict[str, Any], *, route: str=ADD_FRIEND_MAIN_ROUTE, phone: str='', wechat: str='', verify_message: str='', remark_name: str='', remark_code: str='', artifact_dir: str | None=None, calibration_only: bool=False) -> dict[str, Any]:
    try:
        geometry = _ops().get_window_geometry(hwnd)
    except Exception as exc:
        geometry = {}
        geometry_check = {'ok': False, 'reason': 'wechat_window_geometry_unavailable', 'error': repr(exc)}
    else:
        geometry_check = _ops().validate_capture_geometry(geometry)
    quick_login = probe.get('quick_login') if isinstance(probe.get('quick_login'), dict) else {}
    if not geometry_check.get('ok') or quick_login.get('detected'):
        output_dir = Path(artifact_dir) if artifact_dir else add_friend_route_artifact_root(PROJECT_ROOT, route) / time.strftime('%Y%m%d_%H%M%S')
        output_dir.mkdir(parents=True, exist_ok=True)
        reason = str(quick_login.get('reason') or geometry_check.get('reason') or 'wechat_window_not_ready')
        payload = add_friend_failure_payload(error_code=ERROR_WECHAT_WINDOW_NOT_READY, message='WeChat main window is not ready for add_friend automation.', steps=['preflight_window_ready'], query=normalize_add_friend_query(phone=phone, wechat=wechat), phone=phone, wechat=wechat, probe=probe, evidence={'geometry': geometry, 'geometry_check': geometry_check, 'quick_login': quick_login, 'reason': reason, 'manual_action_required': 'open_or_login_wechat_main_window'}, state='wechat_window_not_ready')
        payload['task_status'] = 'failed'
        payload['current_step'] = 'preflight_window_ready'
        payload['server_report_payload'] = mapped_add_friend_server_report_payload(task_status='failed', error_code=ERROR_WECHAT_WINDOW_NOT_READY, current_step='preflight_window_ready')
        payload['plan_path'] = str(output_dir / ADD_FRIEND_ENTRY_CLICK_PLAN_JSON)
        payload['review_path'] = _ops().write_add_friend_entry_click_review(output_dir, payload)
        Path(str(payload['plan_path'])).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return payload
    output_dir = Path(artifact_dir) if artifact_dir else add_friend_route_artifact_root(PROJECT_ROOT, route) / time.strftime('%Y%m%d_%H%M%S')
    output_dir.mkdir(parents=True, exist_ok=True)
    device_profile = _ops().add_friend_device_profile(hwnd, geometry=geometry, route=route)
    probe = dict(probe or {})
    probe['device_profile'] = device_profile
    if calibration_only:
        return _ops().add_friend_calibration_payload(hwnd, probe, geometry=geometry, route=route, phone=phone, wechat=wechat, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, output_dir=output_dir)
    pre_click_readiness = _ops().add_friend_pre_click_main_window_readiness(hwnd, geometry, route=route, output_dir=output_dir)
    probe['add_friend_pre_click_main_window_readiness'] = pre_click_readiness
    if not pre_click_readiness.get('ok'):
        state = str(pre_click_readiness.get('state') or 'wechat_main_surface_not_ready')
        error_code = str(pre_click_readiness.get('error_code') or ERROR_WECHAT_WINDOW_NOT_READY)
        payload = add_friend_failure_payload(error_code=error_code, message='WeChat main window is not foreground or not on the add_friend entry surface.', steps=['preflight_main_surface_ready'], query=normalize_add_friend_query(phone=phone, wechat=wechat), phone=phone, wechat=wechat, probe=probe, evidence={'geometry': geometry, 'geometry_check': geometry_check, 'pre_click_readiness': pre_click_readiness, 'manual_action_required': 'run_wechat_startup_self_check_or_bring_wechat_main_window_foreground'}, state=state)
        payload['task_status'] = 'failed'
        payload['current_step'] = 'preflight_main_surface_ready'
        payload['no_clicks_performed'] = True
        payload['wechat_ui_action_attempted'] = False
        payload['calibration_only'] = False
        payload['route'] = route
        payload['verify_message'] = verify_message
        payload['remark_name'] = remark_name
        payload['remark_code'] = remark_code
        payload['server_report_payload'] = mapped_add_friend_server_report_payload(task_status='failed', error_code=error_code, current_step='preflight_main_surface_ready')
        payload['plan_path'] = str(output_dir / ADD_FRIEND_ENTRY_CLICK_PLAN_JSON)
        payload['review_path'] = _ops().write_add_friend_entry_click_review(output_dir, payload)
        Path(str(payload['plan_path'])).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return payload
    operator_guard = _ops().start_add_friend_operator_guard(route=route, artifact_dir=str(output_dir))
    if operator_guard.get('ok') is not True:
        payload = add_friend_failure_payload(error_code=ERROR_OPERATOR_GUARD_NOT_READY, message='Add-friend RPA operator guard is not ready; the click flow was not started.', steps=['operator_guard_ready'], query=normalize_add_friend_query(phone=phone, wechat=wechat), phone=phone, wechat=wechat, probe=probe, evidence={'geometry': geometry, 'geometry_check': geometry_check, 'operator_guard': operator_guard, 'manual_action_required': 'check_rpa_floating_ball_operator_guard'}, state='operator_guard_not_ready')
        payload['task_status'] = 'failed'
        payload['current_step'] = 'operator_guard_ready'
        payload['server_report_payload'] = mapped_add_friend_server_report_payload(task_status='failed', error_code=ERROR_OPERATOR_GUARD_NOT_READY, current_step='operator_guard_ready')
        payload['plan_path'] = str(output_dir / ADD_FRIEND_ENTRY_CLICK_PLAN_JSON)
        payload['review_path'] = _ops().write_add_friend_entry_click_review(output_dir, payload)
        Path(str(payload['plan_path'])).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return payload
    flow_probe = dict(probe)
    flow_probe['operator_guard'] = operator_guard
    payload: dict[str, Any] = {}
    try:
        payload = _ops().run_add_friend_entry_click_plan_flow(_ops(), hwnd, flow_probe, phone=phone, wechat=wechat, verify_message=verify_message, remark_name=remark_name, remark_code=remark_code, artifact_dir=str(output_dir), route=route)
        payload['operator_guard'] = operator_guard
    finally:
        release = _ops().stop_add_friend_operator_guard(operator_guard, reason='add_friend_entry_click_plan_finished')
        if isinstance(payload, dict):
            payload['operator_guard_release'] = release
            _ops().persist_add_friend_operator_guard_release(payload, release)
    return payload
