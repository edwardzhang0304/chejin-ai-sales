"""Regression checks for the WeChat Win32/OCR compatibility adapter."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SIDECAR_SCRIPT = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"

from apps.wechat_ai_customer_service.adapters.wechat_connector import (  # noqa: E402
    RPALockTimeoutError,
    WeChatConnector,
    add_friend_rpa_env,
    blind_send_without_ocr,
    compat_args,
    enqueue_simulated_inbound_message,
    guarded_send_confirmation_fallback,
    inject_simulated_inbound_messages,
    interactive_rpa_probe_env,
    parse_json_object,
    pop_simulated_inbound_message,
    rpa_payload_has_invalid_window_handle,
    rpa_payload_is_tray_hidden,
    rpa_payload_needs_interactive_confirmation,
    rpa_payload_needs_render_recovery,
    send_rpa_env,
    verify_send_from_messages,
    wechat_rpa_lock,
)
import apps.wechat_ai_customer_service.adapters.wechat_connector as wechat_connector_module  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import (  # noqa: E402
    active_chat_matches,
    add_friend_ocr_compact,
    add_friend_login_or_security_block,
    add_friend_surface_readiness,
    add_friend_optional_field_fill_enabled,
    add_friend_virtual_key_for_digit,
    active_chat_title_cutoff_y,
    adapt_humanized_input_settings,
    auxiliary_wechat_shell_like,
    blind_target_confirmation_guard,
    blocking_screen_reason,
    capabilities_payload,
    chat_header_cutoff_y,
    calculate_send_points,
    classify_message_side,
    clear_add_friend_sidebar_search_box,
    classify_add_friend_ocr_surface,
    clear_existing_input_draft,
    detect_session_subview_back_target,
    detect_blank_render,
    allow_blind_target_confirmation,
    find_add_friend_action_item,
    find_add_friend_search_result_item,
    add_friend_windows_1080p_reference_plus_button_point_for_geometry,
    add_friend_windows_plus_button_point_for_geometry,
    likely_foreign_overlay_capture,
    normalize_add_friend_query,
    normalize_chat_title_for_match,
    parse_messages_from_ocr,
    parse_sessions_from_ocr,
    quick_login_like,
    rect_in_input_area,
    rect_in_input_toolbar,
    set_uia_control_value_humanized,
    search_box_point_for_geometry,
    send_rate_decision,
    session_name_matches,
    session_row_click_candidate_points,
    session_row_click_x,
    session_click_x_for_geometry,
    select_uia_edit_control,
    select_uia_send_button,
    humanized_chunk_text,
    add_friend_human_pause,
    type_add_friend_phone_query_like_human,
    type_add_friend_search_query,
    humanized_input_settings,
    input_text_region_state,
    input_region_visual_delta_confirms,
    input_region_soft_blank_noise,
    input_click_candidate_points,
    is_message_noise,
    jitter_client_click_surface_point,
    jitter_input_click_point,
    jitter_send_click_point,
    sendinput_safe_text,
    sendinput_utf16_units,
    merge_message_history_snapshots,
    message_anchor_match_type,
    message_probe_tokens,
    sidecar_message_content_key,
    normalize_anchor_reply_key,
    normalize_send_trigger_mode,
    normalize_wechat_window,
    non_retryable_input_failure,
    type_text_with_sendinput_unicode,
    ensure_visible_wechat_window,
    select_primary_visible_main_window,
    use_passive_probe_mode,
    validate_capture_geometry,
    validate_active_send_target,
    validate_send_geometry,
    scroll_to_latest_before_read_enabled,
    active_ui_action_budget_decision,
    send_input_confirm_attempt_count,
    send_click_candidate_points,
    safe_send_trigger,
    send_with_guarded_clicks,
)
import apps.wechat_ai_customer_service.admin_backend.services.wechat_startup_check as startup_check  # noqa: E402
import apps.wechat_ai_customer_service.workflows.preflight as preflight  # noqa: E402
from apps.wechat_ai_customer_service.admin_backend.services.wechat_startup_check import evaluate_wechat_capability  # noqa: E402


class FakeControl:
    def __init__(self, *, name: str, control_type: str, rect: object, class_name: str = "") -> None:
        self.Name = name
        self.ControlTypeName = control_type
        self.BoundingRectangle = rect
        self.ClassName = class_name


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class FakeValuePattern:
    def __init__(self) -> None:
        self.values: list[str] = []

    def SetValue(self, value: str) -> None:
        self.values.append(str(value))


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_sidecar_script_without_pythonpath(
    args: list[str],
    *,
    cwd: Path | str,
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(SIDECAR_SCRIPT), *args],
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def test_sidecar_script_bootstraps_project_root_without_pythonpath() -> None:
    with tempfile.TemporaryDirectory() as temp:
        result = run_sidecar_script_without_pythonpath(["--help"], cwd=temp)
    assert_true(
        result.returncode == 0,
        f"sidecar script should bootstrap project root before absolute apps imports: rc={result.returncode}, stderr={result.stderr[-500:]}",
    )


def test_sidecar_help_exposes_stable_actions_and_flags() -> None:
    with tempfile.TemporaryDirectory() as temp:
        result = run_sidecar_script_without_pythonpath(["--help"], cwd=temp)
    assert_true(result.returncode == 0, f"sidecar --help failed: rc={result.returncode}, stderr={result.stderr[-500:]}")
    help_text = result.stdout
    for action in (
        "status",
        "capabilities",
        "sessions",
        "messages",
        "send",
        "recover-render",
        "add-friend-entry-click-plan-windows",
    ):
        assert_true(action in help_text, f"sidecar --help should expose stable action {action}: {help_text[:800]}")
    for flag in (
        "--target",
        "--session-key",
        "--text",
        "--phone",
        "--wechat",
        "--verify-message",
        "--remark-name",
        "--remark-code",
        "--calibration-only",
        "--history-load-times",
        "--history-mode",
        "--anchor-id",
        "--anchor-content-key",
        "--reply-content-key",
        "--restore-to-latest",
        "--no-restore-to-latest",
        "--artifact-dir",
        "--daemon",
    ):
        assert_true(flag in help_text, f"sidecar --help should expose stable flag {flag}: {help_text[:800]}")


def test_sidecar_contract_validation_failure_is_json_without_window_probe() -> None:
    with tempfile.TemporaryDirectory() as temp:
        result = run_sidecar_script_without_pythonpath(
            ["add-friend-entry-click-plan-windows", "--phone", "17368746889", "--artifact-dir", temp],
            cwd=temp,
        )
    assert_true(result.returncode == 1, f"invalid add_friend payload should fail with rc=1: rc={result.returncode}, stdout={result.stdout}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"sidecar failure stdout should remain JSON: {exc}; stdout={result.stdout!r}") from exc
    assert_true(payload.get("ok") is False, f"invalid payload should fail: {payload}")
    assert_true(payload.get("state") == "task_payload_invalid", f"invalid payload state mismatch: {payload}")
    assert_true(payload.get("task_status") == "failed", f"invalid payload task_status mismatch: {payload}")
    assert_true(payload.get("error_code") == "TASK_PAYLOAD_INVALID", f"invalid payload error_code mismatch: {payload}")
    assert_true(payload.get("current_step") == "payload_validation", f"invalid payload current_step mismatch: {payload}")
    assert_true(payload.get("wechat_ui_action_attempted") is False, f"validation failure must not touch WeChat UI: {payload}")
    assert_true(
        payload.get("window_probe", {}).get("reason") == "task_payload_invalid_before_window_probe",
        f"validation failure should occur before window probe: {payload}",
    )
    assert_true(
        payload.get("server_report_payload", {}).get("task.error_code") == "TASK_PAYLOAD_INVALID",
        f"server report should keep invalid-payload error code: {payload}",
    )


def test_sidecar_facade_exports_contract_surface() -> None:
    import apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar as sidecar_module

    for action in (
        "status",
        "capabilities",
        "sessions",
        "messages",
        "send",
        "recover-render",
        "add-friend-entry-click-plan-windows",
    ):
        assert_true(action in sidecar_module.SIDECAR_ACTION_CHOICES, f"sidecar action choice missing: {action}")
    required_exports = (
        "main",
        "run_action",
        "run_sidecar_cli",
        "args_for_daemon_request",
        "parse_sessions_from_ocr",
        "parse_messages_from_ocr",
        "calculate_send_points",
        "validate_capture_geometry",
        "validate_send_geometry",
        "validate_active_send_target",
        "normalize_wechat_window",
        "add_friend_surface_readiness",
        "add_friend_entry_click_plan_payload",
        "send_payload",
    )
    for name in required_exports:
        exported = getattr(sidecar_module, name, None)
        assert_true(callable(exported), f"sidecar facade should keep callable export {name}")


def test_parse_sessions_from_ocr() -> None:
    items = [
        {"text": "Q搜索", "confidence": 0.99, "left": 110, "right": 170, "top": 55, "bottom": 80, "center_x": 140, "center_y": 68},
        {"text": "文件传输助手", "confidence": 0.99, "left": 155, "right": 260, "top": 117, "bottom": 140, "center_x": 205, "center_y": 128},
        {"text": "星期三", "confidence": 0.99, "left": 280, "right": 322, "top": 119, "bottom": 138, "center_x": 301, "center_y": 128},
        {"text": "您想今天定，我完全理...", "confidence": 0.96, "left": 154, "right": 316, "top": 143, "bottom": 165, "center_x": 235, "center_y": 154},
        {"text": "许聪", "confidence": 0.99, "left": 154, "right": 195, "top": 198, "bottom": 221, "center_x": 174, "center_y": 209},
        {"text": "[图片]", "confidence": 0.96, "left": 154, "right": 200, "top": 225, "bottom": 247, "center_x": 177, "center_y": 236},
        {"text": "新数据测试", "confidence": 0.99, "left": 154, "right": 247, "top": 361, "bottom": 385, "center_x": 200, "center_y": 373},
        {"text": "[文件] recorder_expor...", "confidence": 0.96, "left": 157, "right": 321, "top": 388, "bottom": 408, "center_x": 239, "center_y": 398},
    ]
    sessions = parse_sessions_from_ocr(items, (641, 919))
    names = [item["name"] for item in sessions]
    assert_true(names == ["文件传输助手", "许聪", "新数据测试"], f"unexpected sessions: {names}")
    assert_true(
        sessions[0].get("preview") == "您想今天定，我完全理...",
        f"session preview should be parsed from the same row: {sessions[0]}",
    )


def test_parse_sessions_detects_visual_unread_red_dot() -> None:
    image = Image.new("RGB", (641, 919), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((124, 112, 144, 132), fill=(250, 81, 81))
    items = [
        {"text": "许聪", "confidence": 0.99, "left": 154, "right": 195, "top": 117, "bottom": 140, "center_x": 174, "center_y": 128},
        {"text": "服务号", "confidence": 0.99, "left": 154, "right": 215, "top": 198, "bottom": 221, "center_x": 184, "center_y": 209},
    ]
    sessions = parse_sessions_from_ocr(items, (641, 919), screenshot=image)
    assert_true(sessions[0].get("unread_badge") == "visual_red_dot", f"red dot should become unread signal: {sessions}")
    assert_true(not sessions[1].get("unread_badge"), f"nearby rows should not inherit unread dot: {sessions}")


def test_parse_sessions_normalizes_truncated_file_transfer() -> None:
    items = [
        {"text": "文件传输..昨天02:57", "confidence": 0.93, "left": 156, "right": 302, "top": 117, "bottom": 140, "center_x": 229, "center_y": 128},
        {"text": "许聪", "confidence": 0.99, "left": 154, "right": 195, "top": 198, "bottom": 221, "center_x": 174, "center_y": 209},
    ]
    sessions = parse_sessions_from_ocr(items, (981, 860))
    names = [item["name"] for item in sessions]
    assert_true(names[:2] == ["文件传输助手", "许聪"], f"truncated file-transfer alias should normalize: {names}")


def test_parse_sessions_strips_standalone_relative_day_suffix() -> None:
    items = [
        {"text": "新数据测试昨天", "confidence": 0.93, "left": 154, "right": 286, "top": 117, "bottom": 140, "center_x": 220, "center_y": 128},
        {"text": "许聪", "confidence": 0.99, "left": 154, "right": 195, "top": 198, "bottom": 221, "center_x": 174, "center_y": 209},
    ]
    sessions = parse_sessions_from_ocr(items, (981, 860))
    names = [item["name"] for item in sessions]
    assert_true(names[:2] == ["新数据测试", "许聪"], f"standalone relative day suffix should normalize: {names}")


def test_parse_sessions_preserves_duplicate_display_names_with_session_keys() -> None:
    items = [
        {"text": "许聪", "confidence": 0.99, "left": 154, "right": 195, "top": 117, "bottom": 140, "center_x": 174, "center_y": 128},
        {"text": "您好", "confidence": 0.96, "left": 154, "right": 195, "top": 143, "bottom": 165, "center_x": 174, "center_y": 154},
        {"text": "许聪", "confidence": 0.99, "left": 154, "right": 195, "top": 198, "bottom": 221, "center_x": 174, "center_y": 209},
        {"text": "晚上好", "confidence": 0.96, "left": 154, "right": 215, "top": 225, "bottom": 247, "center_x": 184, "center_y": 236},
    ]
    sessions = parse_sessions_from_ocr(items, (641, 919))
    xucong = [item for item in sessions if item.get("name") == "许聪"]
    assert_true(len(xucong) == 2, f"duplicate display names should be preserved for ambiguity handling: {sessions}")
    assert_true(bool(xucong[0].get("session_key")), f"first duplicate should carry session key: {xucong}")
    assert_true(bool(xucong[1].get("session_key")), f"second duplicate should carry session key: {xucong}")
    assert_true(
        xucong[0].get("session_key") != xucong[1].get("session_key"),
        f"duplicate display names should not collapse to the same session key: {xucong}",
    )


def test_add_friend_query_normalization_prefers_phone_digits() -> None:
    assert_true(normalize_add_friend_query(phone="173 6874-6889", wechat="wxid_demo") == "17368746889", "phone digits should be the primary add_friend query")
    assert_true(normalize_add_friend_query(phone="", wechat=" wxid_demo ") == "wxid_demo", "wechat id should be used when phone is empty")
    assert_true(add_friend_ocr_compact(" 网络查找手机 / QQ号 ") == "网络查找手机/qq号", "OCR text should compact consistently")


def test_add_friend_windows_plus_entry_uses_windows_sidebar_geometry() -> None:
    geometry = {"left": -22, "top": 0, "right": 959, "bottom": 860, "width": 981, "height": 860}
    windows_point = add_friend_windows_plus_button_point_for_geometry(geometry)
    windows_1080p_reference_point = add_friend_windows_1080p_reference_plus_button_point_for_geometry(geometry)
    assert_true(292 <= windows_point[0] <= 314, f"Windows add_friend + should land beside the Windows search box: {windows_point}")
    assert_true(58 <= windows_point[1] <= 78, f"Windows add_friend + y should stay on the search row: {windows_point}")
    assert_true(
        windows_1080p_reference_point[0] > windows_point[0] + 35,
        f"Windows 1920x1080 reference point should remain distinct from adaptive Windows point: ref={windows_1080p_reference_point}, adaptive={windows_point}",
    )
    import apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar as sidecar_mod

    original_get_window_geometry = sidecar_mod.get_window_geometry
    try:
        sidecar_mod.get_window_geometry = lambda _hwnd: dict(geometry)
        _x, _y, meta = sidecar_mod.jitter_window_image_click_surface_point(1001, windows_point[0], windows_point[1])
        assert_true(meta.get("role") == "plus_entry_button", f"Windows plus point should use plus-entry jitter bounds: {meta}")
    finally:
        sidecar_mod.get_window_geometry = original_get_window_geometry


def test_add_friend_search_result_detection_from_ocr() -> None:
    items = [
        {"text": "搜索", "confidence": 0.98, "left": 120, "right": 170, "top": 62, "bottom": 86, "center_x": 145, "center_y": 74},
        {"text": "网络查找手机/QQ号：17368746889", "confidence": 0.98, "left": 225, "right": 520, "top": 170, "bottom": 205, "center_x": 372, "center_y": 187},
        {"text": "17368746889", "confidence": 0.98, "left": 260, "right": 386, "top": 250, "bottom": 276, "center_x": 323, "center_y": 263},
    ]
    result = find_add_friend_search_result_item(items, "17368746889", (980, 860))
    assert_true(result is not None, f"phone search result should be detected: {items}")
    assert_true("17368746889" in str(result.get("text")), f"result should carry the searched phone: {result}")


def test_add_friend_surface_classification() -> None:
    add_entry_items = [
        {"text": "挂杌山兮", "confidence": 0.98, "left": 410, "right": 510, "top": 250, "bottom": 280, "center_x": 460, "center_y": 265},
        {"text": "添加到通讯录", "confidence": 0.99, "left": 430, "right": 560, "top": 520, "bottom": 555, "center_x": 495, "center_y": 538},
    ]
    add_entry = classify_add_friend_ocr_surface(add_entry_items, (980, 860))
    assert_true(add_entry.get("state") == "add_contact_entry", f"add contact entry should be classified: {add_entry}")
    assert_true(find_add_friend_action_item(add_entry_items, ("添加到通讯录",), (980, 860)) is not None, "add button should be located")

    invite_items = [
        {"text": "发送添加朋友申请", "confidence": 0.99, "left": 260, "right": 450, "top": 180, "bottom": 215, "center_x": 355, "center_y": 197},
        {"text": "备注名", "confidence": 0.98, "left": 260, "right": 340, "top": 318, "bottom": 345, "center_x": 300, "center_y": 332},
        {"text": "发送", "confidence": 0.99, "left": 808, "right": 880, "top": 760, "bottom": 795, "center_x": 844, "center_y": 778},
    ]
    invite = classify_add_friend_ocr_surface(invite_items, (980, 860))
    assert_true(invite.get("state") == "invite_form", f"invite form should be classified: {invite}")

    already_items = [
        {"text": "发消息", "confidence": 0.99, "left": 430, "right": 500, "top": 550, "bottom": 580, "center_x": 465, "center_y": 565},
        {"text": "音视频通话", "confidence": 0.99, "left": 515, "right": 630, "top": 550, "bottom": 580, "center_x": 572, "center_y": 565},
    ]
    already = classify_add_friend_ocr_surface(already_items, (980, 860))
    assert_true(already.get("result_code") == "already_friend", f"already-friend state should become result_code: {already}")

    windows_profile_items = [
        {"text": "朋友资料", "confidence": 0.99, "left": 68, "right": 142, "top": 290, "bottom": 312, "center_x": 105, "center_y": 301},
        {"text": "发消息", "confidence": 0.99, "left": 93, "right": 142, "top": 794, "bottom": 813, "center_x": 117, "center_y": 803},
        {"text": "语音聊天", "confidence": 0.99, "left": 182, "right": 246, "top": 795, "bottom": 813, "center_x": 214, "center_y": 804},
        {"text": "视频聊天", "confidence": 0.99, "left": 278, "right": 342, "top": 795, "bottom": 813, "center_x": 310, "center_y": 804},
    ]
    windows_profile = classify_add_friend_ocr_surface(windows_profile_items, (428, 577))
    assert_true(windows_profile.get("result_code") == "already_friend", f"Windows friend profile should become already_friend: {windows_profile}")

    not_found_items = [
        {"text": "该用户不存在", "confidence": 0.99, "left": 330, "right": 500, "top": 300, "bottom": 330, "center_x": 415, "center_y": 315},
    ]
    not_found = classify_add_friend_ocr_surface(not_found_items, (980, 860))
    assert_true(not_found.get("error_code") == "PHONE_NOT_FOUND", f"not found state should map to PHONE_NOT_FOUND: {not_found}")


def test_add_friend_surface_readiness_blocks_blank_or_empty_ocr() -> None:
    blank = Image.new("RGB", (980, 860), "white")
    readiness = add_friend_surface_readiness(blank, [], {"width": 980, "height": 860}, stage="after_search")
    assert_true(readiness.get("ok") is False, f"blank add_friend surface should be blocked: {readiness}")
    assert_true(readiness.get("error_code") == "WECHAT_RENDER_NOT_READY", f"blank surface should not become PHONE_NOT_FOUND: {readiness}")

    sparse = Image.new("RGB", (980, 860), "white")
    sparse_items = [{"text": "Weixin", "confidence": 0.98, "left": 10, "right": 80, "top": 10, "bottom": 34, "center_x": 45, "center_y": 22}]
    sparse_readiness = add_friend_surface_readiness(sparse, sparse_items, {"width": 980, "height": 860}, stage="before_search")
    assert_true(sparse_readiness.get("ok") is False, f"title-only WeChat shell should be blocked: {sparse_readiness}")
    assert_true(sparse_readiness.get("error_code") == "WECHAT_RENDER_NOT_READY", f"title-only shell should be render-not-ready: {sparse_readiness}")

    not_found_items = [{"text": "该用户不存在", "confidence": 0.98, "left": 320, "right": 500, "top": 290, "bottom": 322, "center_x": 410, "center_y": 306}]
    not_found_readiness = add_friend_surface_readiness(sparse, not_found_items, {"width": 980, "height": 860}, stage="after_search")
    assert_true(not_found_readiness.get("ok") is True, f"single business error text should remain classifiable: {not_found_readiness}")

    login_items = [{"text": "全，请重新登录。", "confidence": 0.99, "left": 180, "right": 303, "top": 213, "bottom": 232, "center_x": 241, "center_y": 222}]
    login_geometry = {"width": 368, "height": 484}
    login_block = add_friend_login_or_security_block(login_items, geometry=login_geometry)
    assert_true(login_block.get("detected") is True, f"login prompt should be detected: {login_block}")
    login_readiness = add_friend_surface_readiness(sparse, login_items, login_geometry, stage="entry_before_click")
    assert_true(login_readiness.get("ok") is False, f"login prompt must stop before clicking: {login_readiness}")
    assert_true(login_readiness.get("error_code") == "WECHAT_WINDOW_NOT_READY", f"login prompt should map to window-not-ready: {login_readiness}")

    security_items = [{"text": "账号安全，操作频繁，请稍后再试", "confidence": 0.99, "left": 430, "right": 720, "top": 260, "bottom": 288, "center_x": 575, "center_y": 274}]
    security_readiness = add_friend_surface_readiness(sparse, security_items, {"width": 980, "height": 860}, stage="entry_before_click")
    assert_true(security_readiness.get("ok") is False, f"security prompt must stop before clicking: {security_readiness}")
    assert_true(security_readiness.get("error_code") == "ACCOUNT_RESTRICTED", f"security prompt should map to restricted: {security_readiness}")

    sidebar_preview_items = [
        {"text": "文件传输助手", "confidence": 0.98, "left": 155, "right": 270, "top": 116, "bottom": 140, "center_x": 212, "center_y": 128},
        {"text": "【低压发送安全验证】", "confidence": 0.98, "left": 161, "right": 313, "top": 144, "bottom": 164, "center_x": 237, "center_y": 154},
    ]
    preview_block = add_friend_login_or_security_block(sidebar_preview_items, geometry={"width": 981, "height": 860})
    assert_true(preview_block.get("detected") is False, f"sidebar chat preview must not become a security block: {preview_block}")
    preview_readiness = add_friend_surface_readiness(sparse, sidebar_preview_items, {"width": 981, "height": 860}, stage="entry_before_click")
    assert_true(preview_readiness.get("ok") is True, f"sidebar security words should not block add_friend: {preview_readiness}")

    chat_explanation_items = [
        {"text": "许聪", "confidence": 0.99, "left": 352, "right": 398, "top": 56, "bottom": 82, "center_x": 375, "center_y": 69},
        {
            "text": "遇到登录、安全验证、操作频繁、账号异常等状态",
            "confidence": 0.99,
            "left": 466,
            "right": 856,
            "top": 472,
            "bottom": 495,
            "center_x": 661,
            "center_y": 483,
        },
    ]
    chat_explanation_block = add_friend_login_or_security_block(chat_explanation_items, geometry={"width": 981, "height": 860})
    assert_true(
        chat_explanation_block.get("detected") is False,
        f"normal chat explanation about security prompts must not block add_friend: {chat_explanation_block}",
    )
    chat_explanation_readiness = add_friend_surface_readiness(
        sparse,
        chat_explanation_items,
        {"width": 981, "height": 860},
        stage="entry_before_click",
    )
    assert_true(
        chat_explanation_readiness.get("ok") is True,
        f"main-chat security discussion should stay readable for add_friend: {chat_explanation_readiness}",
    )


def test_connector_add_friend_builds_win32_ocr_request() -> None:
    class FakeAddFriendConnector(WeChatConnector):
        def __init__(self) -> None:
            object.__setattr__(self, "calls", [])

        def call_compat_sidecar(self, args: list[str], *, allow_failure: bool = False, primary_payload: dict[str, Any] | None = None, env_overrides: dict[str, str] | None = None) -> dict[str, Any]:
            self.calls.append({"args": list(args), "allow_failure": allow_failure, "env": dict(env_overrides or {})})
            return {"ok": True, "state": "completed", "result_code": "invite_sent"}

    connector = FakeAddFriendConnector()
    result = connector.add_friend(
        phone="17368746889",
        verify_message="我是车金二手车张伟",
        remark_name="客户-CJ8K2P-6889",
        remark_code="CJ8K2P",
    )
    assert_true(result.get("ok") is True and result.get("result_code") == "invite_sent", f"unexpected add_friend result: {result}")
    args = connector.calls[0]["args"]
    assert_true(args[:3] == ["add-friend-entry-click-plan-windows", "--phone", "17368746889"], f"add_friend should call the stable add_friend CLI action: {args}")
    assert_true("--verify-message" in args and "我是车金二手车张伟" in args, f"verify_message should pass through: {args}")
    assert_true("--remark-name" in args and "客户-CJ8K2P-6889" in args, f"remark_name should pass through: {args}")
    assert_true("--remark-code" in args and "CJ8K2P" in args, f"remark_code should pass through: {args}")
    assert_true("--remark" not in args and "--greeting" not in args and "--sales-name" not in args, f"removed add_friend flags must not pass through: {args}")
    assert_true(result.get("wxauto4_reserve_status", {}).get("state") == "wxauto4_reserve_skipped_for_add_friend", f"wxauto reserve should be skipped for add_friend: {result}")


def test_add_friend_rpa_env_is_non_recovery_by_default() -> None:
    previous_recovery = os.environ.get("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO")
    previous_normalize = os.environ.get("WECHAT_WIN32_OCR_WINDOW_NORMALIZE")
    try:
        os.environ.pop("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", None)
        os.environ.pop("WECHAT_WIN32_OCR_WINDOW_NORMALIZE", None)
        env = add_friend_rpa_env()
        assert_true(env.get("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO") == "0", f"add_friend must not auto-recover blank render by default: {env}")
        assert_true(env.get("WECHAT_WIN32_OCR_WINDOW_NORMALIZE") == "0", f"add_friend must not normalize the window by default: {env}")
        assert_true(env.get("WECHAT_WIN32_OCR_PASSIVE_PROBE") == "0", f"add_friend still needs an explicit foreground action env: {env}")
    finally:
        if previous_recovery is None:
            os.environ.pop("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", None)
        else:
            os.environ["WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO"] = previous_recovery
        if previous_normalize is None:
            os.environ.pop("WECHAT_WIN32_OCR_WINDOW_NORMALIZE", None)
        else:
            os.environ["WECHAT_WIN32_OCR_WINDOW_NORMALIZE"] = previous_normalize


def test_add_friend_entry_click_script_keeps_render_recovery_opt_in() -> None:
    script_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "scripts" / "run_wechat_add_friend_entry_click_plan_windows.ps1"
    script = script_path.read_text(encoding="utf-8")
    assert_true("[switch]$AllowRenderRecovery" in script, "entry-click script should make render recovery an explicit operator choice")
    assert_true('WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO = $(if ($AllowRenderRecovery) { "1" } else { "0" })' in script, "entry-click script should default render recovery to off")
    assert_true("[switch]$NormalizeWindow" in script, "entry-click script should make window normalization explicit")
    for removed in ["run_wechat_add_friend_live.ps1", "run_wechat_add_friend_plan.ps1", "run_wechat_add_friend_entry_plan.ps1"]:
        assert_true(not (script_path.parent / removed).exists(), f"removed add_friend script should not exist: {removed}")


def test_add_friend_menu_click_handles_stale_dialog_hwnd() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    screenshot = Image.new("RGB", (980, 860), "white")
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    target = {
        "name": "add_friend_menu_entry",
        "source": "ocr_popup_menu_item",
        "x": 320,
        "y": 150,
        "click_bounds": [280, 120, 380, 180],
        "click_screen_bounds": [280, 120, 380, 180],
    }
    originals = {
        "add_friend_paced_pause": sidecar_mod.add_friend_paced_pause,
        "human_screen_hover": sidecar_mod.human_screen_hover,
        "human_screen_click_in_bounds": sidecar_mod.human_screen_click_in_bounds,
        "wait_for_add_friend_dialog_window": sidecar_mod.wait_for_add_friend_dialog_window,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "capture_wechat_window_visible_screen": sidecar_mod.capture_wechat_window_visible_screen,
        "draw_add_friend_screen_annotation": sidecar_mod.draw_add_friend_screen_annotation,
    }
    try:
        sidecar_mod.add_friend_paced_pause = lambda *_args, **_kwargs: 0.0
        sidecar_mod.human_screen_hover = lambda *_args, **_kwargs: {"ok": True}
        sidecar_mod.human_screen_click_in_bounds = lambda *_args, **_kwargs: {"ok": True}
        sidecar_mod.wait_for_add_friend_dialog_window = lambda **_kwargs: {"ok": True, "hwnd": 2002}
        sidecar_mod.get_window_geometry = lambda hwnd: (_ for _ in ()).throw(RuntimeError("invalid hwnd")) if int(hwnd) == 2002 else dict(geometry)
        sidecar_mod.capture_wechat_window_visible_screen = lambda hwnd, **_kwargs: (screenshot, f"capture_{hwnd}.png")
        sidecar_mod.draw_add_friend_screen_annotation = lambda *_args, **_kwargs: "annotated.png"
        result = sidecar_mod.click_add_friend_menu_entry_and_capture(1001, PROJECT_ROOT, menu_targets=[target])
        assert_true(result.get("clicked") is False, f"stale dialog hwnd should not be treated as clicked: {result}")
        assert_true(result.get("menu_clicked") is True, f"menu click itself should be preserved: {result}")
        assert_true(result.get("reason") == "add_friend_dialog_window_handle_invalid_after_menu_click", f"unexpected stale hwnd reason: {result}")
        assert_true(result.get("next_hwnd") == 0, f"stale next hwnd should be cleared: {result}")
        assert_true(result.get("readiness", {}).get("dialog_handle_invalid") is True, f"readiness should record stale hwnd: {result}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_add_friend_uses_serialized_human_pacing() -> None:
    sidecar = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
    add_friend_windows = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr" / "add_friend_windows.py"
    source = sidecar.read_text(encoding="utf-8")
    implementation_source = source + "\n" + add_friend_windows.read_text(encoding="utf-8")
    assert_true(callable(add_friend_human_pause), "add_friend human pacing helper should be importable")
    assert_true(callable(clear_add_friend_sidebar_search_box), "add_friend slow clear helper should be importable")
    assert_true("clear_add_friend_sidebar_search_box(" in implementation_source and "target_hint=query" in implementation_source, "add_friend should use slow serialized search clearing")
    assert_true("clear_sidebar_search_box_without_select_all(hwnd, search_x, search_y, target_hint=query)" not in implementation_source, "add_friend must not use fast shared search clearing")
    assert_true(
        'add_friend_wait_before_ocr("after_search_input_before_ocr")' in implementation_source
        or "add_friend_wait_before_ocr('after_search_input_before_ocr')" in implementation_source,
        "add_friend must pause between keyboard input and OCR",
    )
    assert_true(
        'add_friend_human_pause(650, 1450, reason="before_mouse_click")' in implementation_source
        or "add_friend_human_pause(650, 1450, reason='before_mouse_click')" in implementation_source,
        "add_friend must pause before mouse click",
    )
    assert_true(
        'add_friend_human_pause(900, 1900, reason="after_mouse_click")' in implementation_source
        or "add_friend_human_pause(900, 1900, reason='after_mouse_click')" in implementation_source,
        "add_friend must pause after mouse click",
    )
    flow_source = (PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "add_friend_flow.py").read_text(encoding="utf-8")
    assert_true("max_attempts = 1" in flow_source, "add_friend plus entry should use exactly one click attempt")
    assert_true("WECHAT_WIN32_OCR_PLUS_ENTRY_CLICK_MAX_ATTEMPTS" not in flow_source, "add_friend plus entry attempts must not be expanded by env overrides")
    assert_true('add_friend_surface_readiness(before_shot' in flow_source, "add_friend must preflight full-window readiness before clicking")


def test_add_friend_query_input_uses_digit_key_presses_by_default() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    original_pause = sidecar_mod.add_friend_human_pause
    pressed: list[int] = []
    try:
        sidecar_mod.add_friend_human_pause = lambda *_args, **_kwargs: 0.0
        result = type_add_friend_phone_query_like_human(
            1001,
            "173 6874 6889",
            key_press_func=lambda key: pressed.append(int(key)),
            window_guard_func=lambda: {"ok": True},
        )
        assert_true(result.get("ok") is True, f"phone query input should pass: {result}")
        assert_true(result.get("method") == "add_friend_digit_keys", f"phone query must use digit key method: {result}")
        assert_true(pressed == [ord(char) for char in "17368746889"], f"unexpected digit keys: {pressed}")
        assert_true(add_friend_virtual_key_for_digit("8") == ord("8"), "digit key mapping should use visible digit VK")
    finally:
        sidecar_mod.add_friend_human_pause = original_pause


def test_add_friend_query_blocks_non_numeric_sendinput_without_opt_in() -> None:
    previous = os.environ.get("WECHAT_WIN32_OCR_ADD_FRIEND_ALLOW_SENDINPUT_QUERY")
    try:
        os.environ.pop("WECHAT_WIN32_OCR_ADD_FRIEND_ALLOW_SENDINPUT_QUERY", None)
        result = type_add_friend_search_query(1001, "wxid_demo")
        assert_true(result.get("ok") is False, f"non-numeric query should not default to SendInput: {result}")
        assert_true(result.get("reason") == "non_numeric_query_requires_explicit_sendinput_opt_in", f"unexpected reason: {result}")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_ADD_FRIEND_ALLOW_SENDINPUT_QUERY", None)
        else:
            os.environ["WECHAT_WIN32_OCR_ADD_FRIEND_ALLOW_SENDINPUT_QUERY"] = previous


def test_add_friend_optional_field_fill_disabled_by_default() -> None:
    previous = os.environ.get("WECHAT_WIN32_OCR_ADD_FRIEND_FILL_OPTIONAL_FIELDS")
    try:
        os.environ.pop("WECHAT_WIN32_OCR_ADD_FRIEND_FILL_OPTIONAL_FIELDS", None)
        assert_true(add_friend_optional_field_fill_enabled() is False, "optional text fill should be disabled by default")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_ADD_FRIEND_FILL_OPTIONAL_FIELDS", None)
        else:
            os.environ["WECHAT_WIN32_OCR_ADD_FRIEND_FILL_OPTIONAL_FIELDS"] = previous


def test_message_probe_tokens_prefer_semantic_body_after_live_marker() -> None:
    text = "【常规验收20260531_0401_post_aipool_twosession-R1】你好，我预算12万左右，想买省心家用二手车，主要上下班和周末带娃，先看哪类？"
    tokens = message_probe_tokens(text)
    assert_true("预算12万左右" in tokens, f"semantic budget token should be preferred: {tokens}")
    assert_true(any("省心家用" in token or "心家用二手车" in token for token in tokens), f"semantic product token should be preferred: {tokens}")
    assert_true(
        not any("常规验收" in token for token in tokens[:3]),
        f"bracketed live marker should be fallback, not primary: {tokens}",
    )


def test_parse_messages_from_ocr() -> None:
    items = [
        {"text": "新数据测试(2)", "confidence": 0.99, "left": 350, "right": 450, "top": 55, "bottom": 80, "center_x": 400, "center_y": 68},
        {"text": "星期三03:02", "confidence": 0.99, "left": 516, "right": 609, "top": 594, "bottom": 614, "center_x": 562, "center_y": 604},
        {"text": "高频漏采实盘测试 订测试耗材1", "confidence": 0.98, "left": 441, "right": 640, "top": 652, "bottom": 672, "center_x": 540, "center_y": 662},
        {"text": "1个 1元", "confidence": 0.98, "left": 439, "right": 520, "top": 674, "bottom": 698, "center_x": 480, "center_y": 686},
    ]
    messages = parse_messages_from_ocr(items, (641, 919), target="新数据测试")
    assert_true(len(messages) == 1, f"unexpected message count: {messages}")
    assert_true("订测试耗材1" in messages[0]["content"], f"message content not merged: {messages}")
    assert_true(messages[0]["sender"] == "self", f"right-side message should be self: {messages}")


def test_parse_messages_keeps_low_visible_bubble_lines() -> None:
    items = [
        {"text": "长订单第一行", "confidence": 0.98, "left": 440, "right": 610, "top": 712, "bottom": 736, "center_x": 525, "center_y": 724},
        {"text": "末尾价格 324元", "confidence": 0.98, "left": 440, "right": 610, "top": 782, "bottom": 806, "center_x": 525, "center_y": 794},
        {"text": "发送", "confidence": 0.99, "left": 560, "right": 610, "top": 850, "bottom": 876, "center_x": 585, "center_y": 863},
    ]
    messages = parse_messages_from_ocr(items, (641, 919), target="新数据测试")
    content = "\n".join(item["content"] for item in messages)
    assert_true("末尾价格 324元" in content, f"low visible bubble line should be retained: {messages}")
    assert_true("发送" not in content, f"send button noise should still be filtered: {messages}")


def test_parse_messages_excludes_left_input_draft_residue() -> None:
    items = [
        {"text": "客户刚才问的车还能看吗", "confidence": 0.98, "left": 410, "right": 650, "top": 610, "bottom": 635, "center_x": 525, "center_y": 622},
        {"text": "[二会话快测]金融这块不能先口头定死，", "confidence": 0.98, "left": 394, "right": 770, "top": 690, "bottom": 718, "center_x": 582, "center_y": 704},
    ]
    messages = parse_messages_from_ocr(items, (980, 860), target="新数据测试")
    content = "\n".join(item["content"] for item in messages)
    assert_true("客户刚才问的车还能看吗" in content, f"normal chat bubble should be retained: {messages}")
    assert_true("二会话快测" not in content, f"input draft residue must not become chat content: {messages}")


def test_parse_messages_classifies_wide_right_bubbles_as_self() -> None:
    items = [
        {"text": "从9万以内、自动挡、倒车影像配置优先、日常家", "confidence": 0.99, "left": 469, "right": 857, "top": 173, "bottom": 196, "center_x": 663, "center_y": 184.5},
        {"text": "用代步这个条件看，可以先排在前面。", "confidence": 0.99, "left": 469, "right": 760, "top": 198, "bottom": 221, "center_x": 614.5, "center_y": 209.5},
        {"text": "顺序排清楚。", "confidence": 0.99, "left": 474, "right": 575, "top": 223, "bottom": 246, "center_x": 524.5, "center_y": 234.5},
    ]
    messages = parse_messages_from_ocr(items, (980, 860), target="文件传输助手")
    assert_true(len(messages) == 1, f"wide right bubble lines should stay merged: {messages}")
    assert_true(messages[0]["sender"] == "self", f"wide right bubble should be self: {messages}")
    assert_true(
        classify_message_side(items[0], width=980) == "self",
        "left-edge cue should classify long self bubbles even when center is near threshold",
    )


def test_parse_messages_outputs_message_envelope_fields() -> None:
    items = [
        {"text": "许聪", "confidence": 0.99, "left": 390, "right": 443, "top": 210, "bottom": 232, "center_x": 416, "center_y": 221},
        {"text": "[引用 张老师：旧订单 试剂盒 9盒 1元]", "confidence": 0.96, "left": 390, "right": 688, "top": 236, "bottom": 258, "center_x": 539, "center_y": 247},
        {"text": "枪头 2盒 30元", "confidence": 0.98, "left": 390, "right": 558, "top": 262, "bottom": 286, "center_x": 474, "center_y": 274},
    ]
    messages = parse_messages_from_ocr(items, (980, 860), target="实验订货群")
    assert_true(len(messages) == 1, f"speaker/quote/current bubble should remain one cleaned message: {messages}")
    message = messages[0]
    assert_true(message.get("content") == "枪头 2盒 30元", f"content should be body-only: {message}")
    assert_true(message.get("content_body") == "枪头 2盒 30元", f"content_body should be body-only: {message}")
    assert_true(message.get("speaker_name") == "许聪", f"speaker should be metadata: {message}")
    assert_true(message.get("quoted_fragments"), f"quote fragment should be retained: {message}")
    assert_true("quote_contamination" in set(message.get("quality_flags") or []), f"quote risk should be flagged: {message}")
    assert_true(str(message.get("captured_at") or "").count(":") >= 2, f"captured_at should include seconds: {message}")
    assert_true(isinstance(message.get("message_envelope"), dict), f"message envelope should be attached: {message}")
    assert_true(str(message.get("canonical_input_id") or "").startswith("canonical_"), f"canonical input id should be attached: {message}")
    assert_true(str(message.get("canonical_visual_id") or "").startswith("canonical_visual_"), f"canonical visual id should be attached: {message}")
    envelope = message.get("message_envelope") or {}
    assert_true(
        envelope.get("canonical_input_id") == message.get("canonical_input_id"),
        f"record and envelope canonical input ids should match: {message}",
    )


def test_history_snapshot_merge_orders_and_dedupes() -> None:
    latest = {
        "messages": [
            {"id": "b03-new", "sender": "self", "content": "[GAP-B03] 连续补充\n第3点"},
            {"id": "b04", "sender": "self", "content": "[GAP-B04] 连续补充\n第4点"},
        ]
    }
    older = {
        "messages": [
            {"id": "b01", "sender": "self", "content": "[GAP-B01] 连续补充\n第1点"},
            {"id": "b02", "sender": "self", "content": "[GAP-B02] 连续补充\n第2点"},
            {"id": "b03-old", "sender": "self", "content": "[GAP-B03]连续补充\n第3点"},
        ]
    }
    merged = merge_message_history_snapshots([latest, older])
    contents = [item["content"] for item in merged]
    assert_true(contents == ["[GAP-B01] 连续补充\n第1点", "[GAP-B02] 连续补充\n第2点", "[GAP-B03]连续补充\n第3点", "[GAP-B04] 连续补充\n第4点"], f"unexpected merged order: {merged}")


def test_history_snapshot_merge_preserves_repeated_short_messages() -> None:
    latest = {
        "messages": [
            {"id": "probe-old-visible", "sender": "unknown", "content": "在？"},
            {"id": "probe-new-visible", "sender": "unknown", "content": "在？"},
        ]
    }
    older = {
        "messages": [
            {"id": "probe-old-history", "sender": "unknown", "content": "在？"},
        ]
    }
    merged = merge_message_history_snapshots([latest, older])
    ids = [item["id"] for item in merged]
    contents = [item["content"] for item in merged]
    assert_true(ids == ["probe-old-history", "probe-new-visible"], f"short repeated probes should preserve the latest distinct occurrence: {merged}")
    assert_true(contents == ["在？", "在？"], f"short repeated probes should keep both visible occurrences: {merged}")


def test_anchor_match_supports_content_and_reply_keys() -> None:
    customer_message = {"id": "ocr-different", "sender": "customer", "type": "text", "content": "上一轮\n已经处理"}
    key = sidecar_message_content_key(customer_message)
    assert_true(key.endswith("上一轮已经处理"), f"content key should normalize OCR line breaks: {key!r}")
    assert_true(
        message_anchor_match_type(
            customer_message,
            anchor_ids=set(),
            anchor_content_keys={key},
            reply_content_keys=set(),
        )
        == "message_content_key",
        "customer content key should locate an anchor",
    )
    reply_message = {"id": "reply-1", "sender": "self", "type": "text", "content": "这台可以先重点看，车况和预算都更贴近。"}
    reply_key = normalize_anchor_reply_key("这台可以先重点看车况和预算都更贴近")
    assert_true(
        message_anchor_match_type(
            reply_message,
            anchor_ids=set(),
            anchor_content_keys=set(),
            reply_content_keys={reply_key},
        )
        == "reply_content_key",
        "self reply content key should locate a reply boundary",
    )


def test_message_noise_filters_relative_timestamps() -> None:
    assert_true(is_message_noise("昨天23:57"), "relative timestamp should be filtered")
    assert_true(is_message_noise("今天 08:01"), "relative timestamp with space should be filtered")


def test_connector_helpers() -> None:
    args = compat_args(["sessions", "--fresh"])
    assert_true(args == ["sessions"], f"--fresh should be omitted for compat sidecar: {args}")
    previous = os.environ.get("WECHAT_WIN32_OCR_ARTIFACT_DIR")
    try:
        os.environ["WECHAT_WIN32_OCR_ARTIFACT_DIR"] = "runtime/wechat_debug_artifacts"
        with_artifact = compat_args(["send", "--target", "文件传输助手", "--text", "你好"])
        assert_true(
            with_artifact[-2:] == ["--artifact-dir", "runtime/wechat_debug_artifacts"],
            f"compat args should append artifact dir when configured: {with_artifact}",
        )
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_ARTIFACT_DIR", None)
        else:
            os.environ["WECHAT_WIN32_OCR_ARTIFACT_DIR"] = previous
    payload = parse_json_object("library log\n{\"ok\": true, \"nested\": {\"a\": 1}}")
    assert_true(payload == {"ok": True, "nested": {"a": 1}}, f"failed to parse logged JSON: {payload}")
    skip_guard = compat_args(
        ["send", "--target", "文件传输助手", "--text", "你好", "--skip-send-rate-guard"]
    )
    assert_true(
        "--skip-send-rate-guard" in skip_guard,
        f"compat args should preserve loopback guard flag: {skip_guard}",
    )


def test_send_geometry_guard() -> None:
    unsafe = {"width": 650, "height": 1100}
    safe = {"width": 801, "height": 1149}
    assert_true(validate_send_geometry(unsafe)["ok"] is False, "small WeChat window should be blocked")
    points = calculate_send_points(safe)
    assert_true(points["ok"] is True, f"safe geometry should produce points: {points}")
    assert_true(points["input_point"][1] > 900, f"input point should stay in text area: {points}")
    assert_true(points["send_point"][1] > 1000, f"send point should stay near send button: {points}")


def test_send_points_apply_small_safe_jitter() -> None:
    geometry = {"width": 980, "height": 860}
    input_x, input_y = jitter_input_click_point(650, 715, geometry)
    send_x, send_y = jitter_send_click_point(918, 816, geometry)
    assert_true(540 <= input_x <= 892, f"input jitter should remain in input pane: {(input_x, input_y)}")
    assert_true(640 <= input_y <= 778, f"input jitter should remain near draft box: {(input_x, input_y)}")
    assert_true(848 <= send_x <= 960, f"send jitter should remain near send button: {(send_x, send_y)}")
    assert_true(768 <= send_y <= 844, f"send jitter should remain near send button row: {(send_x, send_y)}")


def test_send_and_input_points_use_candidate_pools() -> None:
    geometry = {"width": 980, "height": 860}
    points = calculate_send_points(geometry)
    input_candidates = points.get("input_candidate_points") or []
    send_candidates = points.get("send_candidate_points") or []
    assert_true(len(input_candidates) >= 10, f"input click should expose a candidate pool: {input_candidates}")
    assert_true(len(send_candidates) >= 10, f"send click should expose a candidate pool: {send_candidates}")
    assert_true(
        len(set(tuple(point) for point in input_click_candidate_points(geometry))) >= 10,
        "input candidate points should be distinct",
    )
    assert_true(
        len(set(tuple(point) for point in send_click_candidate_points(geometry))) >= 10,
        "send candidate points should be distinct",
    )


def test_input_click_jitter_has_enough_entropy() -> None:
    previous = {
        "WECHAT_WIN32_OCR_INPUT_POINT_JITTER_X": os.environ.get("WECHAT_WIN32_OCR_INPUT_POINT_JITTER_X"),
        "WECHAT_WIN32_OCR_INPUT_POINT_JITTER_Y": os.environ.get("WECHAT_WIN32_OCR_INPUT_POINT_JITTER_Y"),
        "WECHAT_WIN32_OCR_CLICK_SURFACE_INPUT_JITTER_X": os.environ.get("WECHAT_WIN32_OCR_CLICK_SURFACE_INPUT_JITTER_X"),
        "WECHAT_WIN32_OCR_CLICK_SURFACE_INPUT_JITTER_Y": os.environ.get("WECHAT_WIN32_OCR_CLICK_SURFACE_INPUT_JITTER_Y"),
    }
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    original_geometry = sidecar_mod.get_window_geometry
    try:
        for key in previous:
            os.environ.pop(key, None)
        geometry = {"width": 980, "height": 860}
        points = [jitter_input_click_point(637, 715, geometry) for _ in range(80)]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        assert_true(len(set(points)) >= 32, f"input point jitter should avoid repeated exact coordinates: {len(set(points))}")
        assert_true(max(xs) - min(xs) >= 80, f"input point jitter should spread x enough: {min(xs)}..{max(xs)}")
        assert_true(max(ys) - min(ys) >= 28, f"input point jitter should spread y enough: {min(ys)}..{max(ys)}")
        send_points = [jitter_send_click_point(918, 816, geometry) for _ in range(80)]
        send_xs = [point[0] for point in send_points]
        send_ys = [point[1] for point in send_points]
        assert_true(len(set(send_points)) >= 18, f"send point jitter should avoid fixed button coordinates: {len(set(send_points))}")
        assert_true(max(send_xs) - min(send_xs) >= 28, f"send point jitter should spread x enough: {min(send_xs)}..{max(send_xs)}")
        assert_true(max(send_ys) - min(send_ys) >= 18, f"send point jitter should spread y enough: {min(send_ys)}..{max(send_ys)}")
        sidecar_mod.get_window_geometry = lambda _hwnd: {"width": 980, "height": 860}
        surface_points = [jitter_client_click_surface_point(1001, 637, 715) for _ in range(80)]
        finals = [tuple(item[:2]) for item in surface_points]
        roles = {str(item[2].get("role") or "") for item in surface_points}
        assert_true("input_area" in roles, f"input-area click surface jitter should classify input clicks: {roles}")
        assert_true(len(set(finals)) >= 18, f"surface jitter should protect fixed caller points: {len(set(finals))}")
        assert_true(all(item[2].get("original") == [637, 715] for item in surface_points), "surface jitter should keep original point in metadata")
    finally:
        sidecar_mod.get_window_geometry = original_geometry
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_input_region_visual_delta_confirmation() -> None:
    before = {"has_visible_text": False, "ocr_hits": 0, "dark_ratio": 0.001}
    after = {"has_visible_text": True, "ocr_hits": 0, "dark_ratio": 0.025}
    result = input_region_visual_delta_confirms(before, after, {"ok": True, "method": "sendinput_unicode", "typed_chars": 32})
    assert_true(result["ok"] is True, f"visual delta should confirm fresh typed text: {result}")

    stale_before = {"has_visible_text": True, "ocr_hits": 1, "dark_ratio": 0.02}
    stale = input_region_visual_delta_confirms(stale_before, after, {"ok": True, "method": "sendinput_unicode", "typed_chars": 32})
    assert_true(stale["ok"] is False, f"pre-existing input text must not be blindly sent: {stale}")


def test_input_fast_visual_confirm_keeps_before_ocr_and_skips_after_ocr() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    previous_env = {
        "WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM": os.environ.get("WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM"),
        "WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS": os.environ.get("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"),
    }
    originals = {
        "activate_window": sidecar_mod.activate_window,
        "capture_wechat": sidecar_mod.capture_wechat,
        "run_ocr": sidecar_mod.run_ocr,
        "input_text_region_state": sidecar_mod.input_text_region_state,
        "clear_existing_input_draft": sidecar_mod.clear_existing_input_draft,
        "jitter_input_click_point": sidecar_mod.jitter_input_click_point,
        "human_client_click": sidecar_mod.human_client_click,
        "client_click": sidecar_mod.client_click,
        "recover_send_window_guard": sidecar_mod.recover_send_window_guard,
        "type_text_with_sendinput_unicode": sidecar_mod.type_text_with_sendinput_unicode,
        "input_region_visual_delta_confirms": sidecar_mod.input_region_visual_delta_confirms,
        "time_sleep": sidecar_mod.time.sleep,
    }
    calls = {"capture": [], "ocr": 0, "region": [], "click": 0}
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    try:
        os.environ["WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM"] = "1"
        os.environ["WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"] = "1"
        sidecar_mod.activate_window = lambda *_args, **_kwargs: True
        sidecar_mod.time.sleep = lambda _seconds: None
        sidecar_mod.recover_send_window_guard = lambda *_args, **_kwargs: {"ok": True, "reason": "window_valid"}

        def fake_capture(_hwnd, artifact_dir=None, label="capture"):
            calls["capture"].append(label)
            return Image.new("RGB", (980, 860), "white"), f"{label}.png"

        def fake_run_ocr(_screenshot):
            calls["ocr"] += 1
            return []

        def fake_region(_screenshot, ocr_items, *, geometry):
            calls["region"].append(len(ocr_items))
            if len(calls["region"]) == 1:
                return {"has_visible_text": False, "ocr_hits": len(ocr_items), "dark_ratio": 0.001}
            return {"has_visible_text": True, "ocr_hits": len(ocr_items), "dark_ratio": 0.025}

        def fake_clear(_hwnd, *, points, geometry, before_state, artifact_dir=None, attempt=1):
            assert_true(before_state.get("has_visible_text") is False, f"before OCR state should be checked: {before_state}")
            return {"ok": True, "after": before_state, "reason": "already_blank"}

        sidecar_mod.capture_wechat = fake_capture
        sidecar_mod.run_ocr = fake_run_ocr
        sidecar_mod.input_text_region_state = fake_region
        sidecar_mod.clear_existing_input_draft = fake_clear
        sidecar_mod.jitter_input_click_point = lambda x, y, _geometry: (int(x), int(y))
        sidecar_mod.human_client_click = lambda *_args, **_kwargs: calls.__setitem__("click", calls["click"] + 1)
        sidecar_mod.client_click = lambda *_args, **_kwargs: calls.__setitem__("click", calls["click"] + 1)
        sidecar_mod.type_text_with_sendinput_unicode = lambda *_args, **_kwargs: {
            "ok": True,
            "method": "sendinput_unicode",
            "typed_chars": 8,
            "chunks": 1,
        }
        sidecar_mod.input_region_visual_delta_confirms = lambda before, after, input_result: {
            "ok": True,
            "reason": "input_area_visual_delta",
            "before": before,
            "after": after,
            "typed_chars": input_result.get("typed_chars"),
        }
        result = sidecar_mod.paste_text_with_confirmation(
            1001,
            "你好呀",
            points={"input_point": [637, 715], "send_point": [919, 816]},
            geometry=geometry,
            settings={"enabled": True, "method": "sendinput_unicode"},
        )
        assert_true(result.get("ok") is True, f"fast visual input confirmation should pass: {result}")
        assert_true(result.get("confirmed_by") == "input_area_visual_delta_fast", f"should use fast visual confirm: {result}")
        assert_true(calls["ocr"] == 1, f"before-input OCR should remain, after-input OCR should be skipped: {calls}")
        assert_true(calls["click"] == 1, f"single input click expected: {calls}")
        timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
        assert_true(
            "paste_text_with_confirmation_duration_seconds" in timing,
            f"paste timing should expose total confirmation duration: {result}",
        )
        assert_true("before_ocr_duration_seconds" in timing, f"before-input OCR timing should be present: {timing}")
        assert_true("input_operation_duration_seconds" in timing, f"input operation timing should be present: {timing}")
        assert_true("fast_visual_confirm_duration_seconds" in timing, f"fast visual timing should be present: {timing}")
        assert_true("after_ocr_duration_seconds" not in timing, f"fast visual path should skip after OCR timing: {timing}")
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name, value in originals.items():
            if name == "time_sleep":
                sidecar_mod.time.sleep = value
            else:
                setattr(sidecar_mod, name, value)


def test_input_region_soft_blank_noise_allows_post_clear_progress() -> None:
    noise_after_clear = {
        "has_visible_text": True,
        "ocr_hits": 0,
        "dark_ratio": 0.027106,
        "mean": 244.856,
    }
    assert_true(
        input_region_soft_blank_noise(noise_after_clear) is True,
        "low-dark high-mean zero-OCR residue after draft clearing should be treated as soft blank noise",
    )
    ocr_residue = {
        "has_visible_text": True,
        "ocr_hits": 1,
        "dark_ratio": 0.027106,
        "mean": 244.856,
    }
    assert_true(
        input_region_soft_blank_noise(ocr_residue) is False,
        "any OCR-visible residue must not be treated as blank because it can be an unsent draft fragment",
    )
    likely_draft = {
        "has_visible_text": True,
        "ocr_hits": 0,
        "dark_ratio": 0.047955,
        "mean": 241.007,
    }
    assert_true(
        input_region_soft_blank_noise(likely_draft) is False,
        "heavier pre-clear draft-like pixels must still trigger guarded clearing",
    )


def test_input_area_token_confirmation_excludes_recent_chat_bubble() -> None:
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    recent_chat_bubble = {"left": 425, "top": 615, "right": 610, "bottom": 655}
    draft_text = {"left": 405, "top": 705, "right": 610, "bottom": 735}
    assert_true(
        rect_in_input_area(recent_chat_bubble, geometry) is False,
        "recent lower chat bubble must not confirm input text",
    )
    assert_true(rect_in_input_area(draft_text, geometry) is True, "draft text inside input area should confirm")


def test_clear_existing_input_draft_noops_when_blank() -> None:
    result = clear_existing_input_draft(
        0,
        points={"input_point": [600, 720], "send_point": [920, 810]},
        geometry={"width": 980, "height": 860},
        before_state={"has_visible_text": False, "reason": "input_region_blank"},
    )
    assert_true(result["ok"] is True and result["cleared"] is False, f"blank input should not trigger select-all: {result}")


def test_send_rate_guard() -> None:
    state = {"events": [{"target": "新数据测试", "at": 100.0}]}
    limited = send_rate_decision(
        state,
        target="新数据测试",
        now_ts=120.0,
        min_interval_seconds=30,
        burst_window_seconds=600,
        burst_limit=5,
    )
    assert_true(limited["ok"] is False and limited["reason"] == "min_interval_not_elapsed", f"expected interval block: {limited}")
    burst = send_rate_decision(
        {"events": [{"target": "新数据测试", "at": value} for value in (100, 150, 200, 250, 300)]},
        target="新数据测试",
        now_ts=350.0,
        min_interval_seconds=0,
        burst_window_seconds=600,
        burst_limit=5,
    )
    assert_true(burst["ok"] is False and burst["reason"] == "burst_limit_reached", f"expected burst block: {burst}")


def test_wechat_rpa_lock_recovers_stale_lock_and_times_out_on_live_lock() -> None:
    import json
    import tempfile
    import time

    original_lock_path = wechat_connector_module.WECHAT_RPA_LOCK_PATH
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "wechat_rpa.lock"
        wechat_connector_module.WECHAT_RPA_LOCK_PATH = lock_path
        try:
            lock_path.write_text(
                json.dumps({"pid": 99999999, "action": "dead", "created_at": time.time()}),
                encoding="utf-8",
            )
            with wechat_rpa_lock("fresh_after_stale", timeout_seconds=1.0, stale_seconds=60.0) as lock_meta:
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                assert_true(payload.get("action") == "fresh_after_stale", f"stale lock should be replaced: {payload}")
                assert_true(
                    isinstance(lock_meta, dict) and "waited_seconds" in lock_meta and "attempts" in lock_meta,
                    f"lock metadata should expose wait/attempt observability: {lock_meta}",
                )
            assert_true(not lock_path.exists(), "lock should be released after context exit")

            lock_path.write_text(
                json.dumps({"pid": os.getpid(), "action": "live", "created_at": time.time()}),
                encoding="utf-8",
            )
            timed_out = False
            timeout_meta: dict[str, object] = {}
            try:
                with wechat_rpa_lock("blocked_by_live", timeout_seconds=0.35, stale_seconds=60.0):
                    pass
            except RPALockTimeoutError as exc:
                timed_out = True
                timeout_meta = dict(exc.meta or {})
            assert_true(timed_out, "live lock should timeout instead of breaking a healthy owner")
            assert_true(
                timeout_meta.get("action") == "blocked_by_live" and float(timeout_meta.get("waited_seconds") or 0.0) > 0.0,
                f"timeout should include lock wait metadata: {timeout_meta}",
            )
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            assert_true(payload.get("action") == "live", f"live lock should be preserved: {payload}")
        finally:
            wechat_connector_module.WECHAT_RPA_LOCK_PATH = original_lock_path


def test_uia_control_selection_prefers_chatbox() -> None:
    geometry = {"left": 0, "top": 0, "width": 801, "height": 1149}
    edit = FakeControl(name="", control_type="EditControl", rect=FakeRect(345, 970, 770, 1085))
    search = FakeControl(name="搜索", control_type="EditControl", rect=FakeRect(95, 55, 275, 90))
    send = FakeControl(name="发送", control_type="ButtonControl", rect=FakeRect(690, 1082, 766, 1125))
    attach = FakeControl(name="文件", control_type="ButtonControl", rect=FakeRect(440, 1082, 475, 1125))
    assert_true(rect_in_input_area({"left": 345, "top": 970, "right": 770, "bottom": 1085}, geometry), "chat edit should be inside input area")
    assert_true(rect_in_input_toolbar({"left": 690, "top": 1082, "right": 766, "bottom": 1125}, geometry), "send button should be inside toolbar")
    assert_true(select_uia_edit_control([search, edit], geometry) is edit, "chat edit should beat search box")
    assert_true(select_uia_send_button([attach, send], geometry) is send, "send button should be selected by name")


def test_startup_capability_decision() -> None:
    offline = evaluate_wechat_capability(
        {"online": False, "scheme": "wechat_not_ready", "receive": {"ok": False}, "send": {"ok": False}},
        require_send=True,
        module_name="微信自动客服",
    )
    assert_true(offline["ok"] is False and offline["detail"] == "wechat_not_ready", f"offline should block startup: {offline}")
    minimized = evaluate_wechat_capability(
        {
            "online": False,
            "scheme": "win32_ocr_window_geometry_invalid",
            "state": "main_window_geometry_invalid",
            "reason": "window_offscreen_or_minimized",
            "receive": {"ok": False},
            "send": {"ok": False},
        },
        require_send=True,
        module_name="微信自动客服",
    )
    assert_true(
        minimized["ok"] is False
        and minimized["detail"] == "wechat_window_minimized"
        and "未检测到" not in str(minimized.get("message") or ""),
        f"minimized window should not be reported as missing WeChat: {minimized}",
    )
    tray_hidden = evaluate_wechat_capability(
        {
            "online": False,
            "scheme": "win32_ocr_window_in_tray",
            "state": "main_window_in_tray",
            "reason": "wechat_window_in_tray",
            "receive": {"ok": False},
            "send": {"ok": False},
        },
        require_send=True,
        module_name="微信自动客服",
    )
    assert_true(
        tray_hidden["ok"] is False
        and tray_hidden["detail"] == "wechat_window_in_tray"
        and "手动点开微信主窗口" in str(tray_hidden.get("message") or ""),
        f"tray-hidden WeChat should require manual open: {tray_hidden}",
    )

    receive_only = {
        "online": True,
        "scheme": "win32_ocr_receive_only",
        "adapter": "win32_ocr",
        "receive": {"ok": True},
        "send": {"ok": False},
    }
    recorder = evaluate_wechat_capability(receive_only, require_send=False, module_name="AI智能记录员")
    service = evaluate_wechat_capability(receive_only, require_send=True, module_name="微信自动客服")
    assert_true(recorder["ok"] is True, f"recorder can run with receive-only transport: {recorder}")
    assert_true(service["ok"] is False and service["detail"] == "wechat_send_unavailable", f"customer service requires send: {service}")

    wxauto = evaluate_wechat_capability({"online": True, "adapter": "wxauto4", "scheme": "wxauto4"}, require_send=True, module_name="微信自动客服")
    assert_true(wxauto["ok"] is True and wxauto["scheme"] == "wxauto4", f"wxauto4 should pass startup: {wxauto}")


def test_startup_self_check_uses_interactive_probe_env() -> None:
    original_connector = startup_check.WeChatConnector
    observed: dict[str, object] = {}

    class FakeConnector:
        def capabilities(self, *, interactive: bool = False) -> dict[str, object]:
            observed["interactive"] = interactive
            return {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "scheme": "win32_ocr_guarded_click",
                "receive": {"ok": True},
                "send": {"ok": True},
            }

    try:
        startup_check.WeChatConnector = FakeConnector
        result = startup_check.run_wechat_startup_self_check(require_send=True, module_name="微信自动客服")
        assert_true(result["ok"] is True, f"interactive startup self-check should pass: {result}")
        assert_true(observed["interactive"] is True, f"startup should explicitly request interactive probe: {observed}")
    finally:
        startup_check.WeChatConnector = original_connector


def test_connector_interactive_capabilities_passes_probe_env() -> None:
    observed: dict[str, object] = {}

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            observed["args"] = list(args)
            observed["env_overrides"] = dict(env_overrides or {})
            return {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "scheme": "win32_ocr_guarded_click",
                "receive": {"ok": True},
                "send": {"ok": True},
            }

    payload = StubConnector().capabilities(interactive=True)
    env_overrides = observed.get("env_overrides") if isinstance(observed.get("env_overrides"), dict) else {}
    expected_env = interactive_rpa_probe_env()
    assert_true(payload.get("ok") is True, f"interactive capabilities should pass: {payload}")
    assert_true(observed.get("args") == ["capabilities"], f"capabilities action should be used: {observed}")
    assert_true(env_overrides == expected_env, f"interactive probe env should be explicit: {observed}")


def test_connector_interactive_status_passes_probe_env() -> None:
    observed: dict[str, object] = {}

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            observed["args"] = list(args)
            observed["env_overrides"] = dict(env_overrides or {})
            return {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "state": "main_window_compat",
            }

    payload = StubConnector().status(interactive=True)
    env_overrides = observed.get("env_overrides") if isinstance(observed.get("env_overrides"), dict) else {}
    assert_true(payload.get("ok") is True, f"interactive status should pass: {payload}")
    assert_true(observed.get("args") == ["status"], f"status action should be used: {observed}")
    assert_true(env_overrides == interactive_rpa_probe_env(), f"interactive status env should be explicit: {observed}")


def test_connector_fresh_session_discovery_passes_probe_env() -> None:
    observed: dict[str, object] = {}

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            observed["args"] = list(args)
            observed["env_overrides"] = dict(env_overrides or {})
            return {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "state": "sessions_ocr",
                "sessions": [{"name": "文件传输助手"}],
            }

    payload = StubConnector().list_sessions(fresh=True)
    env_overrides = observed.get("env_overrides") if isinstance(observed.get("env_overrides"), dict) else {}
    assert_true(payload.get("ok") is True, f"fresh session discovery should pass: {payload}")
    assert_true(observed.get("args") == ["sessions", "--fresh"], f"fresh discovery should preserve sessions args: {observed}")
    assert_true(env_overrides == interactive_rpa_probe_env(), f"fresh discovery should request interactive probe env: {observed}")


def test_connector_passive_session_poll_keeps_probe_env_empty() -> None:
    observed: dict[str, object] = {}

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            observed["args"] = list(args)
            observed["env_overrides"] = dict(env_overrides or {})
            return {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "state": "sessions_ocr",
                "sessions": [{"name": "文件传输助手"}],
            }

    payload = StubConnector().list_sessions(fresh=False)
    env_overrides = observed.get("env_overrides") if isinstance(observed.get("env_overrides"), dict) else {}
    assert_true(payload.get("ok") is True, f"passive session poll should pass: {payload}")
    assert_true(observed.get("args") == ["sessions"], f"passive poll should keep basic sessions args: {observed}")
    assert_true(env_overrides == {}, f"passive session poll should remain non-invasive: {observed}")


def test_connector_interactive_capabilities_reports_tray_hidden_window_without_restore() -> None:
    observed: dict[str, object] = {"calls": [], "reserve_calls": 0}

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            calls = observed["calls"]
            assert isinstance(calls, list)
            calls.append({"args": list(args), "env": dict(env_overrides or {})})
            return {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "scheme": "win32_ocr_window_in_tray",
                "state": "main_window_in_tray",
                "reason": "wechat_window_in_tray",
                "window_probe": {"main_count": 1, "visible_main_count": 0},
                "receive": {"ok": False},
                "send": {"ok": False},
                "manual_action_required": "open_wechat_main_window",
            }

        def call_reserve_sidecar(self, args, *, allow_failure=False, primary_payload=None):  # type: ignore[override]
            observed["reserve_calls"] = int(observed.get("reserve_calls") or 0) + 1
            return {"ok": False, "online": False, "adapter": "wxauto4", "state": "wxauto4_reserve_disabled"}

    payload = StubConnector().capabilities(interactive=True)
    calls = observed["calls"]
    assert isinstance(calls, list)
    assert_true(payload.get("ok") is False and payload.get("state") == "main_window_in_tray", f"tray-hidden window should block startup: {payload}")
    assert_true(len(calls) == 1, f"tray-hidden window must not be auto-restored/retried: {observed}")
    assert_true(calls[0]["env"] == interactive_rpa_probe_env(), f"first interactive call should use restore env: {observed}")
    assert_true(payload.get("manual_action_required") == "open_wechat_main_window", f"manual action should be explicit: {payload}")
    assert_true(rpa_payload_is_tray_hidden(payload) is True, f"helper should detect tray-hidden payload: {payload}")
    assert_true(observed.get("reserve_calls") == 0, f"wxauto4 reserve should not mask tray-hidden RPA state: {observed}")


def test_connector_passive_status_does_not_recover_minimized_window() -> None:
    observed: dict[str, object] = {"calls": [], "reserve_calls": 0}

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            calls = observed["calls"]
            assert isinstance(calls, list)
            calls.append({"args": list(args), "env": dict(env_overrides or {})})
            return {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "state": "main_window_geometry_invalid",
                "reason": "window_offscreen_or_minimized",
                "geometry": {"left": -32000, "top": -32000, "width": 199, "height": 34},
            }

        def call_reserve_sidecar(self, args, *, allow_failure=False, primary_payload=None):  # type: ignore[override]
            observed["reserve_calls"] = int(observed.get("reserve_calls") or 0) + 1
            return {"ok": False, "online": False, "adapter": "wxauto4", "state": "wxauto4_reserve_disabled"}

    payload = StubConnector().status(interactive=False)
    calls = observed["calls"]
    assert isinstance(calls, list)
    assert_true(payload.get("ok") is False, f"passive status should stay passive on recoverable failure: {payload}")
    assert_true(len(calls) == 1 and calls[0]["env"] == {}, f"passive status should not do interactive retry: {observed}")
    assert_true(observed.get("reserve_calls") == 1, f"reserve status should still be recorded for diagnostics: {observed}")


def test_interactive_probe_does_not_restore_tray_hidden_window() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    original_probe = sidecar_mod.probe_wechat_windows
    original_restore = sidecar_mod.restore_wechat_window
    calls = {"restore": 0}
    try:
        sidecar_mod.probe_wechat_windows = lambda: {
            "windows": [{"hwnd": 1001, "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": False}],
            "visible_windows": [],
            "main_windows": [{"hwnd": 1001, "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": False}],
            "visible_main_windows": [],
            "visible_count": 0,
            "main_count": 1,
            "visible_main_count": 0,
        }

        def fake_restore(_probe):
            calls["restore"] += 1
            return {"hwnd": 1001}

        sidecar_mod.restore_wechat_window = fake_restore
        probe = sidecar_mod.ensure_visible_wechat_window(interactive=True)
        assert_true(probe.get("main_window_in_tray") is True, f"tray-hidden state should be explicit: {probe}")
        assert_true(probe.get("manual_action_required") == "open_wechat_main_window", f"manual action should be required: {probe}")
        assert_true(calls["restore"] == 0, f"tray-hidden WeChat must not be auto-restored: {calls}")
    finally:
        sidecar_mod.probe_wechat_windows = original_probe
        sidecar_mod.restore_wechat_window = original_restore


def test_connector_interactive_status_reports_blank_render_without_auto_recovery_by_default() -> None:
    observed: dict[str, object] = {"calls": [], "reserve_calls": 0}

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            calls = observed["calls"]
            assert isinstance(calls, list)
            calls.append({"args": list(args), "env": dict(env_overrides or {})})
            if len(calls) == 1:
                return {
                    "ok": False,
                    "online": False,
                    "adapter": "win32_ocr",
                    "scheme": "win32_ocr_blank_render",
                    "state": "blank_render_detected",
                    "reason": "blank_render",
                    "render_probe": {"detected": True, "reason": "blank_white_like"},
                }
            if list(args) == ["recover-render"]:
                return {
                    "ok": True,
                    "online": True,
                    "adapter": "win32_ocr",
                    "state": "main_window_compat",
                    "render_recovery": {"ok": True, "attempted": True},
                }
            return {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "state": "main_window_compat",
            }

        def call_reserve_sidecar(self, args, *, allow_failure=False, primary_payload=None):  # type: ignore[override]
            observed["reserve_calls"] = int(observed.get("reserve_calls") or 0) + 1
            return {"ok": False, "online": False, "adapter": "wxauto4", "state": "wxauto4_reserve_disabled"}

    payload = StubConnector().status(interactive=True)
    calls = observed["calls"]
    assert isinstance(calls, list)
    assert_true(payload.get("ok") is False, f"blank render should stop and report by default: {payload}")
    assert_true(
        [item["args"] for item in calls] == [["status"]],
        f"blank render must not auto close/reopen WeChat by default: {observed}",
    )
    recovery = payload.get("rpa_recovery") if isinstance(payload.get("rpa_recovery"), dict) else {}
    assert_true(
        recovery.get("mode") == "render_recovery_disabled_stop_and_report",
        f"blank render should report disabled recovery mode: {payload}",
    )
    assert_true(observed.get("reserve_calls") == 0, f"wxauto4 reserve should not run after blank render stop: {observed}")


def test_connector_capabilities_skips_reserve_after_blank_render_stop() -> None:
    observed: dict[str, object] = {"calls": [], "reserve_calls": 0}

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            calls = observed["calls"]
            assert isinstance(calls, list)
            calls.append({"args": list(args), "env": dict(env_overrides or {})})
            return {
                "ok": False,
                "online": False,
                "adapter": "win32_ocr",
                "scheme": "win32_ocr_blank_render",
                "state": "blank_render_detected",
                "reason": "blank_render",
            }

        def call_reserve_sidecar(self, args, *, allow_failure=False, primary_payload=None):  # type: ignore[override]
            observed["reserve_calls"] = int(observed.get("reserve_calls") or 0) + 1
            return {"ok": True, "online": True, "adapter": "wxauto4", "state": "wxauto4_reserve_ready"}

    payload = StubConnector().capabilities(interactive=True)
    assert_true(payload.get("ok") is False, f"blank render should not be masked by reserve adapter: {payload}")
    assert_true(observed.get("reserve_calls") == 0, f"blank render stop should skip reserve probing: {observed}")
    recovery = payload.get("rpa_recovery") if isinstance(payload.get("rpa_recovery"), dict) else {}
    assert_true(
        recovery.get("mode") == "render_recovery_disabled_stop_and_report",
        f"capabilities should expose render-stop recovery metadata: {payload}",
    )


def test_connector_interactive_status_recovers_blank_render_when_explicitly_enabled() -> None:
    observed: dict[str, object] = {"calls": [], "reserve_calls": 0}
    previous = os.environ.get("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO")

    class StubConnector(WeChatConnector):
        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            calls = observed["calls"]
            assert isinstance(calls, list)
            calls.append({"args": list(args), "env": dict(env_overrides or {})})
            if len(calls) == 1:
                return {
                    "ok": False,
                    "online": False,
                    "adapter": "win32_ocr",
                    "scheme": "win32_ocr_blank_render",
                    "state": "blank_render_detected",
                    "reason": "blank_render",
                    "render_probe": {"detected": True, "reason": "blank_white_like"},
                }
            if list(args) == ["recover-render"]:
                return {
                    "ok": True,
                    "online": True,
                    "adapter": "win32_ocr",
                    "state": "main_window_compat",
                    "render_recovery": {"ok": True, "attempted": True},
                }
            return {
                "ok": True,
                "online": True,
                "adapter": "win32_ocr",
                "state": "main_window_compat",
            }

        def call_reserve_sidecar(self, args, *, allow_failure=False, primary_payload=None):  # type: ignore[override]
            observed["reserve_calls"] = int(observed.get("reserve_calls") or 0) + 1
            return {"ok": False, "online": False, "adapter": "wxauto4", "state": "wxauto4_reserve_disabled"}

    try:
        os.environ["WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO"] = "1"
        payload = StubConnector().status(interactive=True)
        calls = observed["calls"]
        assert isinstance(calls, list)
        assert_true(payload.get("ok") is True, f"explicit blank render recovery should pass: {payload}")
        assert_true(
            [item["args"] for item in calls] == [["status"], ["recover-render"], ["status"]],
            f"explicit blank render recovery should run tray redraw before retry: {observed}",
        )
        recovery = payload.get("rpa_recovery") if isinstance(payload.get("rpa_recovery"), dict) else {}
        assert_true(
            recovery.get("mode") == "interactive_blank_render_tray_redraw",
            f"explicit recovery mode should be preserved: {payload}",
        )
        assert_true(observed.get("reserve_calls") == 0, f"wxauto4 reserve should not run after recovery: {observed}")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", None)
        else:
            os.environ["WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO"] = previous


def test_sidecar_recover_render_reports_disabled_by_default() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    initial = {
        "ok": False,
        "online": False,
        "adapter": "win32_ocr",
        "state": "blank_render_detected",
        "reason": "blank_render",
    }
    originals = {
        "status_payload": sidecar_mod.status_payload,
        "trigger_wechat_tray_redraw": sidecar_mod.trigger_wechat_tray_redraw,
    }
    previous = os.environ.get("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO")
    calls: list[str] = []
    try:
        os.environ.pop("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", None)
        sidecar_mod.status_payload = lambda hwnd, probe, artifact_dir=None: initial
        sidecar_mod.trigger_wechat_tray_redraw = lambda hwnd, probe: calls.append("redraw") or {"ok": True}
        payload = sidecar_mod.recover_blank_render_payload(1001, {"selected_main_window": {"hwnd": 1001}})
        json.dumps(payload, ensure_ascii=True)
        assert_true(payload.get("ok") is False, f"disabled recovery should preserve failure payload: {payload}")
        recovery = payload.get("render_recovery") if isinstance(payload.get("render_recovery"), dict) else {}
        assert_true(
            recovery.get("reason") == "auto_render_recovery_disabled",
            f"sidecar should not auto recover blank render by default: {payload}",
        )
        assert_true(calls == [], f"default recovery must not close/reopen WeChat: {calls}")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", None)
        else:
            os.environ["WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO"] = previous
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_sidecar_recover_render_enters_visible_quick_login_when_explicitly_enabled() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    initial = {
        "ok": False,
        "online": False,
        "adapter": "win32_ocr",
        "state": "blank_render_detected",
        "reason": "blank_render",
    }
    final = {
        "ok": True,
        "online": True,
        "adapter": "win32_ocr",
        "state": "main_window_compat",
    }
    probe_after_redraw = {
        "visible_main_windows": [
            {"hwnd": 1001, "title": "微信"},
            {"hwnd": 2002, "title": "微信"},
        ],
    }
    calls: list[str] = []
    previous = os.environ.get("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO")
    originals = {
        "status_payload": sidecar_mod.status_payload,
        "reserve_render_recovery": sidecar_mod.reserve_render_recovery,
        "trigger_wechat_tray_redraw": sidecar_mod.trigger_wechat_tray_redraw,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
        "probe_wechat_windows": sidecar_mod.probe_wechat_windows,
        "ensure_visible_wechat_window": sidecar_mod.ensure_visible_wechat_window,
        "select_primary_visible_main_window": sidecar_mod.select_primary_visible_main_window,
        "activate_window": sidecar_mod.activate_window,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "ensure_quick_login_if_available": sidecar_mod.ensure_quick_login_if_available,
    }
    status_calls: list[int] = []
    try:
        os.environ["WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO"] = "1"

        def fake_status_payload(_hwnd, _probe, artifact_dir=None):
            status_calls.append(1)
            return initial if len(status_calls) == 1 else final

        sidecar_mod.status_payload = fake_status_payload
        sidecar_mod.reserve_render_recovery = lambda: {"ok": True, "guard_enabled": True}
        sidecar_mod.trigger_wechat_tray_redraw = lambda hwnd, probe: calls.append("redraw") or {"ok": True, "mode": "stub"}
        sidecar_mod.humanized_action_sleep = lambda _min_ms, _max_ms: None
        sidecar_mod.probe_wechat_windows = lambda: probe_after_redraw
        sidecar_mod.ensure_visible_wechat_window = lambda interactive=True: {"visible_main_windows": [{"hwnd": 3003, "title": "微信"}]}
        sidecar_mod.select_primary_visible_main_window = lambda probe: {"hwnd": 3003, "title": "微信"}
        sidecar_mod.activate_window = lambda hwnd: calls.append(f"activate:{hwnd}")
        sidecar_mod.get_window_geometry = lambda hwnd: (
            {"left": 0, "top": 0, "right": 785, "bottom": 688, "width": 785, "height": 688}
            if int(hwnd) == 1001
            else {"left": 620, "top": 266, "right": 914, "bottom": 653, "width": 294, "height": 387}
        )

        def fake_quick_login(hwnd, artifact_dir=None, auto_enter=False):
            calls.append(f"quick:{hwnd}:{auto_enter}")
            return {
                "attempted": True,
                "detected": True,
                "auto_enter_enabled": bool(auto_enter),
                "reason": "quick_login_enter_clicked",
            }

        sidecar_mod.ensure_quick_login_if_available = fake_quick_login
        payload = sidecar_mod.recover_blank_render_payload(1001, {"selected_main_window": {"hwnd": 1001}})
        assert_true(payload.get("ok") is True, f"recovery should pass after quick-login enter: {payload}")
        recovery = payload.get("render_recovery") if isinstance(payload.get("render_recovery"), dict) else {}
        quick = recovery.get("quick_login") if isinstance(recovery.get("quick_login"), dict) else {}
        assert_true(quick.get("attempted") is True, f"recovery should record quick-login attempt: {payload}")
        assert_true("quick:2002:True" in calls, f"quick-login should target the small visible window: {calls}")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO", None)
        else:
            os.environ["WECHAT_WIN32_OCR_RENDER_RECOVERY_AUTO"] = previous
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_startup_evaluates_nested_rpa_geometry_failure() -> None:
    payload = {
        "ok": False,
        "online": False,
        "adapter": "none",
        "scheme": "wechat_not_ready",
        "state": "no_supported_wechat_transport",
        "primary_status": {
            "ok": False,
            "online": False,
            "adapter": "win32_ocr",
            "scheme": "win32_ocr_window_geometry_invalid",
            "state": "main_window_geometry_invalid",
            "reason": "window_offscreen_or_minimized",
            "geometry": {"left": -32000, "top": -32000, "width": 199, "height": 34},
        },
        "reserve_status": {"ok": False, "online": False, "adapter": "wxauto4", "state": "wxauto4_reserve_disabled"},
    }
    decision = evaluate_wechat_capability(payload, require_send=True, module_name="微信自动客服")
    assert_true(decision.get("detail") == "wechat_window_minimized", f"nested RPA geometry reason should be preserved: {decision}")
    assert_true("未检测到" not in str(decision.get("message") or ""), f"minimized window should not be called missing: {decision}")
    assert_true(rpa_payload_needs_interactive_confirmation(payload["primary_status"]) is True, "recoverable geometry helper should trigger")
    tray_payload = {
        "ok": False,
        "online": False,
        "adapter": "win32_ocr",
        "scheme": "win32_ocr_window_in_tray",
        "state": "main_window_in_tray",
        "reason": "wechat_window_in_tray",
    }
    assert_true(
        rpa_payload_is_tray_hidden(tray_payload) is True,
        "tray-hidden WeChat should be classified separately",
    )
    assert_true(
        rpa_payload_needs_interactive_confirmation(tray_payload) is False,
        "tray-hidden WeChat should not trigger automatic restore retry",
    )
    blank_payload = {
        "ok": False,
        "online": False,
        "adapter": "win32_ocr",
        "scheme": "win32_ocr_blank_render",
        "state": "blank_render_detected",
        "reason": "blank_render",
    }
    assert_true(
        rpa_payload_needs_interactive_confirmation(blank_payload) is True,
        "blank render should be classified as interactively recoverable",
    )
    sparse_shell_payload = {
        "ok": False,
        "online": False,
        "adapter": "win32_ocr",
        "scheme": "win32_ocr_auxiliary_shell",
        "state": "auxiliary_shell_window_detected",
        "reason": "auxiliary_shell_window",
        "shell_probe": {"detected": True, "reason": "sparse_auxiliary_shell", "ocr_count": 0},
    }
    assert_true(
        rpa_payload_needs_render_recovery(sparse_shell_payload) is True,
        "sparse auxiliary shell should be classified as render recovery, not normal chat failure",
    )
    assert_true(
        rpa_payload_needs_interactive_confirmation(sparse_shell_payload) is True,
        "sparse auxiliary shell should trigger interactive recovery when user starts the module",
    )


def test_preflight_uses_interactive_status_probe() -> None:
    observed: dict[str, object] = {}
    original_connector = preflight.WeChatConnector
    original_build_review_queue = preflight.build_review_queue
    original_load_config = preflight.load_config
    original_check_local_config = preflight.check_local_config
    original_parse_targets = preflight.parse_targets
    original_resolve_path = preflight.resolve_path

    class Target:
        name = "文件传输助手"

    class FakeConnector:
        sidecar_python = Path(__file__)
        sidecar_script = Path(__file__)
        compat_sidecar_script = Path(__file__)

        def wxauto4_reserve_enabled(self) -> bool:
            return False

        def status(self, *, interactive: bool = False) -> dict[str, object]:
            observed["status_interactive"] = interactive
            return {"ok": True, "online": True, "adapter": "win32_ocr", "compat_reason": "rpa_primary"}

        def list_sessions(self) -> dict[str, object]:
            observed["list_sessions_called"] = True
            return {"ok": True, "sessions": [{"name": "文件传输助手"}]}

    try:
        preflight.WeChatConnector = FakeConnector  # type: ignore[assignment]
        preflight.build_review_queue = lambda **_kwargs: {"counts": {}}  # type: ignore[assignment]
        preflight.load_config = lambda _path: {"rpa_humanized_send": {}}  # type: ignore[assignment]
        preflight.check_local_config = lambda _path, _config: {"checks": [], "warnings": [], "errors": []}  # type: ignore[assignment]
        preflight.parse_targets = lambda _config: [Target()]  # type: ignore[assignment]
        preflight.resolve_path = lambda _value: Path(__file__)  # type: ignore[assignment]
        result = preflight.run_preflight(Path("fake.json"), extra_targets=[], skip_wechat=False)
    finally:
        preflight.WeChatConnector = original_connector
        preflight.build_review_queue = original_build_review_queue
        preflight.load_config = original_load_config
        preflight.check_local_config = original_check_local_config
        preflight.parse_targets = original_parse_targets
        preflight.resolve_path = original_resolve_path

    assert_true(result.get("ok") is True, f"preflight should pass with interactive RPA status: {result}")
    assert_true(observed.get("status_interactive") is True, f"preflight should request interactive status: {observed}")
    assert_true(observed.get("list_sessions_called") is True, f"preflight should still validate sessions: {observed}")


def test_adaptive_window_points() -> None:
    compact = {"width": 760, "height": 780}
    wide = {"width": 1600, "height": 980}
    compact_search = search_box_point_for_geometry(compact)
    wide_search = search_box_point_for_geometry(wide)
    compact_session_x = session_click_x_for_geometry(compact)
    wide_session_x = session_click_x_for_geometry(wide)
    assert_true(90 <= compact_search[0] <= 170, f"compact search x should stay in sidebar area: {compact_search}")
    assert_true(90 <= wide_search[0] <= 170, f"wide search x should stay in sidebar area: {wide_search}")
    assert_true(compact_session_x <= 330, f"compact session click x should stay inside session list: {compact_session_x}")
    assert_true(wide_session_x <= 330, f"wide session click x should stay inside session list: {wide_session_x}")
    assert_true(chat_header_cutoff_y(780) >= 90, "header cutoff should respect baseline minimum")
    assert_true(chat_header_cutoff_y(1400) <= 150, "header cutoff should be capped for large windows")


def test_session_match_and_click_x() -> None:
    geometry = {"width": 980, "height": 860}
    default_x = session_click_x_for_geometry(geometry)
    session = {"name": "文件传输助手", "left": 152, "right": 276, "center_y": 292}
    click_x = session_row_click_x(session, geometry, default_x=default_x)
    assert_true(170 <= click_x <= 352, f"session click should stay inside sidebar: {click_x}")
    assert_true(session_name_matches("文件传输助手", "文件传输助手", exact=True), "exact match should pass")
    assert_true(session_name_matches("文件传输助手(2)", "文件传输助手", exact=False), "fuzzy match should pass")
    assert_true(session_name_matches("File Transfer Assistant", "文件传输助手", exact=True), "alias exact match should pass")
    assert_true(session_name_matches("新数据测试昨天10:39", "新数据测试", exact=True), "OCR-merged session time should be stripped")
    assert_true(session_name_matches("新数据测试昨天", "新数据测试", exact=True), "OCR-merged standalone day should be stripped")
    assert_true(session_name_matches("许聪星期四", "许聪", exact=True), "OCR-merged weekday suffix should be stripped")
    assert_true(session_name_matches("文件传输站", "文件传输助手", exact=True) is False, "unrelated exact mismatch should fail")


def test_session_click_candidate_points_spread_across_row() -> None:
    geometry = {"width": 785, "height": 688}
    session = {"name": "新数据测试", "left": 154, "right": 247, "center_y": 128}
    points = session_row_click_candidate_points(session, geometry, default_x=session_click_x_for_geometry(geometry))
    assert_true(len(points) >= 10, f"session row should expose at least 10 click candidates: {points}")
    assert_true(len(set(points)) >= 10, f"candidate points should be distinct: {points}")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    assert_true(max(xs) - min(xs) >= 90, f"candidate x spread should avoid fixed-pixel clicks: {points}")
    assert_true(max(ys) - min(ys) >= 18, f"candidate y spread should avoid fixed-line clicks: {points}")
    assert_true(all(74 <= x <= 318 and 88 <= y <= 670 for x, y in points), f"points must stay inside sidebar row: {points}")


def test_active_chat_matches_file_transfer_alias() -> None:
    items = [
        {"text": "File Transfer Assistant", "confidence": 0.99, "left": 412, "right": 668, "top": 62, "bottom": 92, "center_x": 540, "center_y": 76},
    ]
    ok = active_chat_matches(items, (980, 860), target="文件传输助手", exact=True)
    assert_true(ok, "header alias should match file transfer target")


def test_active_chat_matches_wrapped_title_text() -> None:
    items = [
        {"text": "与文件传输助手的聊天", "confidence": 0.99, "left": 420, "right": 705, "top": 108, "bottom": 144, "center_x": 562, "center_y": 126},
    ]
    ok = active_chat_matches(items, (980, 860), target="文件传输助手", exact=True)
    assert_true(ok, "wrapped title text should match exact target")
    assert_true(
        normalize_chat_title_for_match("与文件传输助手的聊天") == normalize_chat_title_for_match("文件传输助手"),
        "wrapped title normalization should collapse to target identity",
    )


def test_active_chat_matches_ignores_group_speaker_label_below_title_roi() -> None:
    items = [
        {"text": "新数据测试", "confidence": 0.99, "left": 420, "right": 540, "top": 62, "bottom": 92, "center_x": 480, "center_y": 76},
        {"text": "许聪", "confidence": 0.99, "left": 410, "right": 460, "top": 126, "bottom": 152, "center_x": 435, "center_y": 139},
        {"text": "晚上好", "confidence": 0.99, "left": 420, "right": 520, "top": 158, "bottom": 184, "center_x": 470, "center_y": 171},
    ]
    assert_true(active_chat_matches(items, (785, 688), target="新数据测试", exact=True), "real title should still match")
    assert_true(
        active_chat_matches(items, (785, 688), target="许聪", exact=True) is False,
        "group speaker labels in the chat body must not authorize a send to that speaker",
    )


def test_active_chat_matches_rejects_other_chat_body_sender_as_title() -> None:
    items = [
        {"text": "新数据测试", "confidence": 0.99, "left": 420, "right": 540, "top": 62, "bottom": 92, "center_x": 480, "center_y": 76},
        {"text": "许聪", "confidence": 0.99, "left": 410, "right": 460, "top": 116, "bottom": 140, "center_x": 435, "center_y": 128},
        {"text": "在吗", "confidence": 0.99, "left": 420, "right": 480, "top": 148, "bottom": 176, "center_x": 450, "center_y": 162},
    ]
    assert_true(active_chat_matches(items, (785, 688), target="新数据测试", exact=True), "actual chat title should match")
    assert_true(
        active_chat_matches(items, (785, 688), target="许聪", exact=True) is False,
        "a body sender name in another active chat must not authorize send-to-target fast path",
    )


def test_active_chat_matches_unread_badge_and_roi_edge() -> None:
    items = [
        {
            "text": "新数据测试(2)",
            "confidence": 0.99,
            "left": 353,
            "right": 471,
            "top": 56,
            "bottom": 81,
            "center_x": 412,
            "center_y": 68,
        },
        {
            "text": "许聪",
            "confidence": 0.99,
            "left": 421,
            "right": 459,
            "top": 293,
            "bottom": 314,
            "center_x": 440,
            "center_y": 303,
        },
    ]
    assert_true(
        active_chat_matches(items, (785, 688), target="新数据测试", exact=True),
        "active title should tolerate unread suffix and small OCR ROI drift",
    )
    assert_true(
        active_chat_matches(items, (785, 688), target="许聪", exact=True) is False,
        "body speaker labels below title ROI must not authorize the wrong session",
    )


def test_active_chat_matches_real_981_title_left_edge() -> None:
    items = [
        {
            "text": "许聪",
            "confidence": 0.99,
            "left": 356,
            "right": 391,
            "top": 58,
            "bottom": 78,
            "center_x": 373,
            "center_y": 68,
        },
        {
            "text": "许聪",
            "confidence": 0.99,
            "left": 420,
            "right": 462,
            "top": 385,
            "bottom": 410,
            "center_x": 441,
            "center_y": 398,
        },
        {
            "text": "在吗",
            "confidence": 0.99,
            "left": 420,
            "right": 470,
            "top": 620,
            "bottom": 645,
            "center_x": 445,
            "center_y": 632,
        },
    ]
    assert_true(
        active_chat_matches(items, (981, 860), target="许聪", exact=True),
        "real 981px WeChat header title near the body split should confirm active chat",
    )


def test_quick_login_detection() -> None:
    items = [
        {"text": "Meta_xc", "confidence": 0.99, "left": 120, "right": 200, "top": 100, "bottom": 128, "center_x": 160, "center_y": 114},
        {"text": "进入微信", "confidence": 0.99, "left": 90, "right": 220, "top": 280, "bottom": 322, "center_x": 155, "center_y": 301},
        {"text": "仅传输文件", "confidence": 0.99, "left": 170, "right": 255, "top": 342, "bottom": 368, "center_x": 212, "center_y": 355},
        {"text": "切换账号", "confidence": 0.99, "left": 70, "right": 150, "top": 342, "bottom": 368, "center_x": 110, "center_y": 355},
    ]
    geometry = {"width": 368, "height": 484}
    assert_true(quick_login_like(items, geometry=geometry), "quick-login card should be detected")
    assert_true(quick_login_like(items, geometry={"width": 980, "height": 860}) is False, "large window should not be treated as quick-login")


def test_quick_login_auto_enter_uses_human_client_click() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    geometry = {"left": 776, "top": 333, "right": 1144, "bottom": 817, "width": 368, "height": 484}
    screenshot = Image.new("RGB", (368, 484), color=(30, 30, 30))
    items = [
        {"text": "Meta_xc", "confidence": 0.99, "left": 120, "right": 200, "top": 100, "bottom": 128, "center_x": 160, "center_y": 114},
        {"text": "进入微信", "confidence": 0.99, "left": 72, "right": 296, "top": 337, "bottom": 381, "center_x": 184, "center_y": 359},
        {"text": "切换账号", "confidence": 0.99, "left": 91, "right": 159, "top": 413, "bottom": 435, "center_x": 125, "center_y": 424},
        {"text": "仅传输文件", "confidence": 0.99, "left": 188, "right": 278, "top": 413, "bottom": 435, "center_x": 233, "center_y": 424},
    ]
    calls: list[tuple[int, int, int]] = []
    originals = {
        "capture_wechat": sidecar_mod.capture_wechat,
        "run_ocr": sidecar_mod.run_ocr,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "human_client_click": sidecar_mod.human_client_click,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
    }
    try:
        sidecar_mod.capture_wechat = lambda hwnd, artifact_dir=None, label="quick_login_probe": (screenshot, "quick.png")
        sidecar_mod.run_ocr = lambda image: list(items)
        sidecar_mod.get_window_geometry = lambda hwnd: geometry
        sidecar_mod.human_client_click = lambda hwnd, x, y: calls.append((int(hwnd), int(x), int(y)))
        sidecar_mod.humanized_action_sleep = lambda _min_ms, _max_ms: None
        result = sidecar_mod.ensure_quick_login_if_available(2002, auto_enter=True)
        assert_true(result.get("attempted") is True, f"quick login should be clicked: {result}")
        assert_true(calls == [(2002, 184, 359)], f"quick login should use human client click at OCR button center: {calls}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_blocking_screen_ignores_normal_chat_login_words() -> None:
    chat_items = [
        {"text": "许聪", "confidence": 0.99, "left": 420, "right": 470, "top": 62, "bottom": 92, "center_x": 445, "center_y": 76},
        {"text": "文件传输助手", "confidence": 0.99, "left": 153, "right": 266, "top": 116, "bottom": 140, "center_x": 210, "center_y": 128},
        {"text": "发送", "confidence": 0.99, "left": 900, "right": 946, "top": 800, "bottom": 832, "center_x": 923, "center_y": 816},
        {"text": "我刚才登录VPS服务端，为什么发了两次验证码？", "confidence": 0.99, "left": 430, "right": 810, "top": 220, "bottom": 250, "center_x": 620, "center_y": 235},
        {"text": "后面扫码确认一下也可以", "confidence": 0.99, "left": 430, "right": 720, "top": 270, "bottom": 300, "center_x": 575, "center_y": 285},
        {"text": "这是一条发送阶段安全验证测试消息", "confidence": 0.99, "left": 430, "right": 790, "top": 320, "bottom": 350, "center_x": 610, "center_y": 335},
    ]
    assert_true(
        blocking_screen_reason(chat_items) == "",
        "normal chat text containing login/scan/security words should not block RPA read",
    )

    login_items = [
        {"text": "进入微信", "confidence": 0.99, "left": 90, "right": 220, "top": 280, "bottom": 322, "center_x": 155, "center_y": 301},
        {"text": "切换账号", "confidence": 0.99, "left": 70, "right": 150, "top": 342, "bottom": 368, "center_x": 110, "center_y": 355},
    ]
    assert_true(
        blocking_screen_reason(login_items) == "login_or_qr",
        "real login-card text should still block RPA read",
    )


def test_blocking_screen_detects_wechat_storage_full_dialog() -> None:
    items = [
        {"text": "存储空间已满"},
        {"text": "C盘存储空间已满，无法继续使用微信。"},
        {"text": "请清理出足够存储空间后再继续使用微信。"},
    ]
    reason = blocking_screen_reason(items)
    assert_true(
        reason.startswith("blocking_text:"),
        f"WeChat storage-full dialog should stop live RPA before clicking: {reason}",
    )


def test_service_subview_back_target_detection() -> None:
    items = [
        {"text": "<服务号", "confidence": 0.99, "left": 104, "right": 188, "top": 112, "bottom": 144, "center_x": 146, "center_y": 128},
        {"text": "丰巢", "confidence": 0.99, "left": 154, "right": 204, "top": 162, "bottom": 188, "center_x": 179, "center_y": 175},
    ]
    point = detect_session_subview_back_target(items, (981, 860))
    assert_true(isinstance(point, dict), f"service subview back point should be detected: {point}")
    assert_true(70 <= int(point.get("x") or 0) <= 170, f"back x should stay in sidebar header: {point}")


def test_connector_rpa_priority_and_wxauto_reserve_toggle() -> None:
    class StubConnector(WeChatConnector):
        def __init__(self, *, reserve_enabled: bool, rpa_ok: bool, reserve_ok: bool) -> None:
            super().__init__()
            self._reserve_enabled = reserve_enabled
            self._rpa_ok = rpa_ok
            self._reserve_ok = reserve_ok

        def wxauto4_reserve_enabled(self) -> bool:
            return self._reserve_enabled

        def call_compat_sidecar(self, args, *, allow_failure=False, primary_payload=None, env_overrides=None):  # type: ignore[override]
            if self._rpa_ok:
                return {"ok": True, "online": True, "adapter": "win32_ocr", "state": "stub_rpa_ok"}
            return {"ok": False, "online": False, "adapter": "win32_ocr", "state": "stub_rpa_fail"}

        def call_reserve_sidecar(self, args, *, allow_failure=False, primary_payload=None):  # type: ignore[override]
            if self._reserve_enabled is False:
                return {
                    "ok": False,
                    "online": False,
                    "adapter": "wxauto4",
                    "state": "wxauto4_reserve_disabled",
                }
            if self._reserve_ok:
                return {"ok": True, "online": True, "adapter": "wxauto4", "state": "stub_wxauto_ok"}
            return {"ok": False, "online": False, "adapter": "wxauto4", "state": "stub_wxauto_fail"}

    rpa_primary = StubConnector(reserve_enabled=False, rpa_ok=True, reserve_ok=False)
    rpa_status = rpa_primary.status()
    assert_true(rpa_status["adapter"] == "win32_ocr", f"RPA should be primary adapter: {rpa_status}")

    reserve_disabled = StubConnector(reserve_enabled=False, rpa_ok=False, reserve_ok=True)
    disabled_status = reserve_disabled.status()
    assert_true(
        disabled_status.get("wxauto4_reserve_status", {}).get("state") == "wxauto4_reserve_disabled",
        f"reserve should remain disabled by default: {disabled_status}",
    )

    reserve_enabled = StubConnector(reserve_enabled=True, rpa_ok=False, reserve_ok=True)
    reserve_status = reserve_enabled.status()
    assert_true(reserve_status.get("adapter") == "wxauto4", f"wxauto4 reserve should activate only when enabled: {reserve_status}")


def test_send_verify_handles_split_multiline_messages() -> None:
    expected = "你好，在吗？\n[live-regression:20260523180041:1:1]"
    payload = {
        "messages": [
            {"sender": "unknown", "content": "你好，在吗？"},
            {"sender": "self", "content": "[live-regression:20260523180041:1:1]"},
        ]
    }
    assert_true(
        verify_send_from_messages(payload, expected_text=expected),
        "split multiline messages should be considered verified",
    )


def test_send_verify_tolerates_common_ocr_noise_on_long_self_reply() -> None:
    expected = "哥，10万左右接娃通勤，先看这两台比较合适。大众Polo 4.58万，自动挡，小巧好停，日常市区通勤挺合适。丰田凯美瑞 8.98万，预算内空间更宽敞，接娃家用会更从容些。"
    payload = {
        "messages": [
            {
                "sender": "self",
                "content": "哥，10万左右接娃通勤，先看这两台比较合适。大众Polo4.58万，自动挡，小I巧好停，日常市区通勤挺合适。丰田凯美瑞8.98万，预算内空间更宽,接娃家用会更从容些。",
            }
        ]
    }
    assert_true(
        verify_send_from_messages(payload, expected_text=expected),
        "post-send verification should tolerate common OCR glyph/punctuation noise on long self replies",
    )
    unrelated = {"messages": [{"sender": "self", "content": "这是一条完全无关的短消息"}]}
    assert_true(
        verify_send_from_messages(unrelated, expected_text=expected) is False,
        "OCR-tolerant verification must not accept unrelated content",
    )


def test_blind_send_without_ocr_verification_fallback() -> None:
    send = {
        "ok": True,
        "send_result": {
            "post_send_guard": {"reason": "target_confirm_skipped_no_ocr"},
        },
    }
    messages = {"ok": True, "state": "messages_ocr", "messages": []}
    assert_true(
        blind_send_without_ocr(send, messages),
        "blind send fallback should accept empty-message verification payload",
    )


def test_guarded_send_confirmation_fallback() -> None:
    send = {
        "ok": True,
        "send_result": {
            "ok": True,
            "pre_send_guard": {"ok": True, "reason": "target_confirmed", "confirmation_confidence": "active_title_strict"},
            "post_send_guard": {"ok": True, "reason": "send_window_readable_after_send"},
            "click": {
                "paste": {
                    "ok": True,
                    "confirmed_by": "clipboard_copyback",
                }
            },
        },
    }
    messages = {"ok": True, "state": "messages_ocr", "messages": []}
    assert_true(
        guarded_send_confirmation_fallback(send, messages),
        "guard-confirmed send should allow fallback when OCR replay misses fresh bubble",
    )
    visual_send = {
        "ok": True,
        "send_result": {
            "ok": True,
            "pre_send_guard": {"ok": True, "reason": "target_confirmed", "confirmation_confidence": "active_title_strict"},
            "post_send_guard": {"ok": True, "reason": "send_window_readable_after_send"},
            "click": {
                "paste": {
                    "ok": True,
                    "confirmed_by": "input_area_visual_delta_fast",
                }
            },
        },
    }
    assert_true(
        guarded_send_confirmation_fallback(visual_send, messages),
        "visual-delta fast confirmation should be treated as guarded send confirmation",
    )
    missing_pre_guard = {
        "ok": True,
        "send_result": {
            "ok": True,
            "post_send_guard": {"ok": True, "reason": "target_confirmed"},
            "click": {"paste": {"ok": True, "confirmed_by": "ocr_input_area"}},
        },
    }
    assert_true(
        guarded_send_confirmation_fallback(missing_pre_guard, messages) is False,
        "fast confirmation must require pre-send target confirmation",
    )
    blocked = {"ok": True, "state": "login_window_detected", "messages": []}
    assert_true(
        guarded_send_confirmation_fallback(send, blocked) is False,
        "guard fallback should not pass when login window is detected",
    )


def test_fast_send_confirmation_skips_slow_message_read_when_guard_is_strong() -> None:
    class FastVerifyConnector(WeChatConnector):
        def __init__(self) -> None:
            self.messages_called = 0

        def send_text(self, target: str, text: str, exact: bool = True, *, skip_send_rate_guard: bool = False) -> dict:
            return {
                "ok": True,
                "send_result": {
                    "ok": True,
                    "pre_send_guard": {"ok": True, "reason": "target_confirmed", "confirmation_confidence": "active_title_strict"},
                    "post_send_guard": {"ok": True, "reason": "send_window_readable_after_send"},
                    "click": {"paste": {"ok": True, "confirmed_by": "ocr_input_area"}},
                },
            }

        def get_messages(self, target: str, exact: bool = True, history_load_times: int = 0) -> dict:
            self.messages_called += 1
            return {"ok": True, "state": "messages_ocr", "messages": []}

    previous = os.environ.get("WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION")
    os.environ["WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION"] = "1"
    try:
        connector = FastVerifyConnector()
        result = connector.send_text_and_verify("文件传输助手", "您好")
        assert_true(bool(result.get("verified")), "fast confirmation should verify strong guarded sends")
        assert_true(
            result.get("verification_mode") == "send_guard_confirmed_fast",
            "fast confirmation should be explicitly labeled",
        )
        assert_true(connector.messages_called == 0, "fast confirmation should skip full OCR message read")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION", None)
        else:
            os.environ["WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION"] = previous


def test_invalid_window_handle_is_hard_stop_not_recovery() -> None:
    payload = {
        "ok": False,
        "online": False,
        "state": "win32_ocr_failed",
        "error": "error(1400, 'GetWindowRect', '无效的窗口句柄。')",
    }
    assert_true(
        rpa_payload_has_invalid_window_handle(payload),
        "invalid Win32 hwnd should be classified explicitly",
    )
    assert_true(
        rpa_payload_needs_interactive_confirmation(payload) is False,
        "invalid hwnd should not be treated as a recoverable interactive probe",
    )


def test_daemon_invalid_window_handle_is_hard_stop() -> None:
    payload = {
        "ok": False,
        "online": False,
        "state": "daemon_dispatch_failed",
        "error": "error(1400, 'GetWindowRect', '无效的窗口句柄。')",
    }
    assert_true(
        rpa_payload_has_invalid_window_handle(payload),
        "invalid hwnd surfaced through daemon_dispatch_failed should be classified explicitly",
    )


def test_sidecar_exception_payload_marks_invalid_window_handle() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    payload = sidecar_mod.exception_payload_for_sidecar(
        RuntimeError("error(1400, 'GetWindowRect', '无效的窗口句柄。')"),
        state="daemon_dispatch_failed",
    )
    assert_true(payload.get("ok") is False, f"exception payload should fail: {payload}")
    assert_true(payload.get("reason") == "window_handle_invalid", f"invalid hwnd reason should be explicit: {payload}")
    assert_true(payload.get("risk_stop_recommended") is True, f"invalid hwnd should request hard stop: {payload}")
    assert_true(rpa_payload_has_invalid_window_handle(payload), f"connector should classify sidecar payload: {payload}")


def test_send_text_invalid_window_handle_skips_wxauto4_reserve() -> None:
    class InvalidHwndConnector(WeChatConnector):
        def __init__(self) -> None:
            super().__init__()
            self.reserve_calls = 0

        def call_compat_sidecar(self, args, allow_failure=False, env_overrides=None):  # type: ignore[override]
            return {
                "ok": False,
                "online": False,
                "state": "win32_ocr_failed",
                "error": "error(1400, 'GetWindowRect', '无效的窗口句柄。')",
            }

        def call_reserve_sidecar(self, args, *, allow_failure=False, primary_payload=None):  # type: ignore[override]
            self.reserve_calls += 1
            return {"ok": True, "online": True, "adapter": "wxauto4", "state": "unexpected_reserve_call"}

    connector = InvalidHwndConnector()
    result = connector.send_text("文件传输助手", "您好", exact=True)
    assert_true(result.get("ok") is False, f"invalid hwnd send should fail: {result}")
    assert_true(result.get("risk_stop_recommended") is True, f"invalid hwnd should request hard stop: {result}")
    assert_true(
        result.get("risk_stop_reason") == "win32_invalid_window_handle",
        f"invalid hwnd reason should be explicit: {result}",
    )
    assert_true(connector.reserve_calls == 0, f"wxauto4 reserve should be skipped after hard stop: {result}")
    assert_true(
        result.get("wxauto4_reserve_status", {}).get("state") == "wxauto4_reserve_skipped_due_to_rpa_hard_stop",
        f"reserve skip should be recorded: {result}",
    )


def test_foreign_overlay_capture_filter_and_blind_target_gate() -> None:
    foreign = [
        {"text": "apps/wechat_ai_customer_servic"},
        {"text": "15个文件已更改+1550-351"},
        {"text": "New project"},
    ]
    assert_true(likely_foreign_overlay_capture(foreign), "codex overlay OCR should be recognized as foreign capture")
    old = os.environ.get("WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND")
    os.environ.pop("WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND", None)
    try:
        assert_true(
            allow_blind_target_confirmation("文件传输助手") is False,
            "file transfer blind target confirmation should be disabled by default",
        )
        os.environ["WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND"] = "1"
        assert_true(
            allow_blind_target_confirmation("文件传输助手"),
            "file transfer blind target confirmation should require explicit opt-in",
        )
    finally:
        if old is None:
            os.environ.pop("WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND", None)
        else:
            os.environ["WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND"] = old
    assert_true(
        allow_blind_target_confirmation("许聪") is False,
        "non-file-transfer sessions should not allow blind target confirmation",
    )


def test_blind_target_confirmation_uses_sidebar_match_when_title_missing() -> None:
    items = [
        {"text": "文件传输助手", "confidence": 0.99, "left": 156, "right": 272, "top": 130, "bottom": 152, "center_x": 214, "center_y": 141},
        {"text": "周六下午三点到店", "confidence": 0.98, "left": 498, "right": 664, "top": 642, "bottom": 672, "center_x": 581, "center_y": 657},
    ]
    old = os.environ.get("WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND")
    os.environ.pop("WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND", None)
    try:
        default_blocked = blind_target_confirmation_guard(
            target="文件传输助手",
            exact=True,
            ocr_items=items,
            image_size=(980, 860),
            geometry={"width": 980, "height": 860},
            screenshot_path="",
        )
        assert_true(
            bool(default_blocked.get("ok")) is False,
            f"file-transfer blind fallback should be disabled by default: {default_blocked}",
        )
        os.environ["WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND"] = "1"
        guard = blind_target_confirmation_guard(
            target="文件传输助手",
            exact=True,
            ocr_items=items,
            image_size=(980, 860),
            geometry={"width": 980, "height": 860},
            screenshot_path="",
        )
        assert_true(bool(guard.get("ok")), f"file-transfer blind fallback should pass when explicitly enabled: {guard}")
        assert_true(
            guard.get("reason") == "target_confirm_skipped_title_ocr_drift",
            f"fallback reason should be explicit for diagnostics: {guard}",
        )
        assert_true(
            guard.get("confirmation_confidence") == "weak_sidebar_only" and not guard.get("confirmed_target"),
            f"blind fallback must not masquerade as confirmed target: {guard}",
        )
        blocked = blind_target_confirmation_guard(
            target="许聪",
            exact=True,
            ocr_items=items,
            image_size=(980, 860),
            geometry={"width": 980, "height": 860},
            screenshot_path="",
        )
        assert_true(bool(blocked.get("ok")) is False, f"non-file-transfer target should not use blind fallback: {blocked}")
    finally:
        if old is None:
            os.environ.pop("WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND", None)
        else:
            os.environ["WECHAT_WIN32_OCR_ALLOW_BLIND_FILE_TRANSFER_SEND"] = old


def test_active_chat_cutoff_extends_above_sidebar_cutoff() -> None:
    height = 860
    assert_true(
        active_chat_title_cutoff_y(height) > chat_header_cutoff_y(height),
        "active chat title cutoff should be looser than sidebar/message cutoff",
    )


def test_file_transfer_simulated_inbound_fallback() -> None:
    enqueue_simulated_inbound_message(target="文件传输助手", text="回环测试消息")
    payload = {"ok": True, "state": "messages_ocr", "messages": []}
    patched = inject_simulated_inbound_messages(payload, target="文件传输助手")
    messages = patched.get("messages") if isinstance(patched, dict) else []
    assert_true(isinstance(messages, list) and len(messages) == 1, f"loopback fallback should inject one message: {patched}")
    assert_true(messages[0].get("source_adapter") == "win32_loopback", f"injected message should be loopback typed: {messages}")
    enqueue_simulated_inbound_message(target="文件传输助手", text="回环测试消息2")
    with_existing = inject_simulated_inbound_messages(
        {"ok": True, "state": "messages_ocr", "messages": [{"id": "legacy", "content": "旧消息", "sender": "self"}]},
        target="文件传输助手",
    )
    with_existing_messages = with_existing.get("messages") if isinstance(with_existing, dict) else []
    assert_true(
        isinstance(with_existing_messages, list) and len(with_existing_messages) == 2,
        f"loopback fallback should append even when OCR has existing messages: {with_existing}",
    )
    assert_true(
        with_existing_messages[-1].get("source_adapter") == "win32_loopback",
        f"appended fallback should stay loopback typed: {with_existing_messages}",
    )
    untouched = inject_simulated_inbound_messages({"ok": True, "state": "messages_ocr", "messages": []}, target="许聪")
    assert_true(untouched.get("messages") == [], f"non-file-transfer target should not receive loopback fallback: {untouched}")


def test_fast_confirmation_still_enqueues_file_transfer_loopback() -> None:
    while pop_simulated_inbound_message("文件传输助手") is not None:
        pass

    class FastConfirmConnector(WeChatConnector):
        def __init__(self) -> None:
            super().__init__()
            self.messages_reads = 0

        def send_text(self, target: str, text: str, exact: bool = True, *, skip_send_rate_guard: bool = False) -> dict:
            assert_true(skip_send_rate_guard, "file-transfer loopback should skip the transport rate guard only for simulated inbound")
            return {
                "ok": True,
                "send_result": {
                    "ok": True,
                    "pre_send_guard": {"ok": True, "reason": "target_confirmed", "confirmation_confidence": "active_title_strict"},
                    "post_send_guard": {"ok": True, "reason": "send_window_readable_after_send"},
                    "click": {"paste": {"ok": True, "confirmed_by": "ocr_input_area"}},
                },
            }

        def get_messages(self, target: str, exact: bool = True, **_kwargs) -> dict:
            self.messages_reads += 1
            return {"ok": True, "state": "messages_ocr", "messages": []}

    previous = os.environ.get("WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION")
    try:
        os.environ["WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION"] = "1"
        connector = FastConfirmConnector()
        result = connector.send_text_and_verify(
            "文件传输助手",
            "快速确认回环测试",
            exact=True,
            simulate_inbound_file_transfer=True,
        )
        assert_true(result.get("verified") is True, f"fast confirmation should verify send: {result}")
        assert_true(connector.messages_reads == 0, "fast confirmation should still skip slow message reads")
        patched = inject_simulated_inbound_messages(
            {"ok": True, "state": "messages_ocr", "messages": []},
            target="文件传输助手",
        )
        messages = patched.get("messages") if isinstance(patched, dict) else []
        assert_true(isinstance(messages, list) and len(messages) == 1, f"loopback message should be enqueued: {patched}")
        assert_true(messages[0].get("content") == "快速确认回环测试", f"unexpected loopback content: {messages}")
    finally:
        while pop_simulated_inbound_message("文件传输助手") is not None:
            pass
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION", None)
        else:
            os.environ["WECHAT_WIN32_OCR_FAST_SEND_CONFIRMATION"] = previous


def test_passive_probe_mode_toggle() -> None:
    previous = os.environ.get("WECHAT_WIN32_OCR_PASSIVE_PROBE")
    try:
        os.environ["WECHAT_WIN32_OCR_PASSIVE_PROBE"] = "1"
        assert_true(use_passive_probe_mode("status"), "status should support passive probe mode")
        assert_true(use_passive_probe_mode("capabilities"), "capabilities should support passive probe mode")
        assert_true(use_passive_probe_mode("sessions"), "sessions should support passive probe mode")
        assert_true(use_passive_probe_mode("send") is False, "send action should never use passive probe mode")
        assert_true(use_passive_probe_mode("add-friend-entry-click-plan-windows") is False, "Windows add_friend route should focus WeChat before clicks")
        os.environ["WECHAT_WIN32_OCR_PASSIVE_PROBE"] = "0"
        assert_true(use_passive_probe_mode("status") is False, "env override should disable passive probe mode")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_PASSIVE_PROBE", None)
        else:
            os.environ["WECHAT_WIN32_OCR_PASSIVE_PROBE"] = previous


def test_low_disturbance_read_and_action_budget_defaults() -> None:
    previous_scroll = os.environ.get("WECHAT_WIN32_OCR_SCROLL_TO_LATEST_BEFORE_READ")
    previous_budget = os.environ.get("WECHAT_WIN32_OCR_UI_ACTION_BUDGET_ENABLED")
    try:
        os.environ.pop("WECHAT_WIN32_OCR_SCROLL_TO_LATEST_BEFORE_READ", None)
        assert_true(
            scroll_to_latest_before_read_enabled() is False,
            "message reads should not scroll to latest before every capture by default",
        )
        os.environ["WECHAT_WIN32_OCR_SCROLL_TO_LATEST_BEFORE_READ"] = "1"
        assert_true(scroll_to_latest_before_read_enabled() is True, "explicit env override should keep compatibility")
        os.environ["WECHAT_WIN32_OCR_UI_ACTION_BUDGET_ENABLED"] = "0"
        decision = active_ui_action_budget_decision(action="unit_test", reserve=False)
        assert_true(decision.get("ok") is True and decision.get("enabled") is False, "UI action budget should be disable-able for diagnostics")
    finally:
        if previous_scroll is None:
            os.environ.pop("WECHAT_WIN32_OCR_SCROLL_TO_LATEST_BEFORE_READ", None)
        else:
            os.environ["WECHAT_WIN32_OCR_SCROLL_TO_LATEST_BEFORE_READ"] = previous_scroll
        if previous_budget is None:
            os.environ.pop("WECHAT_WIN32_OCR_UI_ACTION_BUDGET_ENABLED", None)
        else:
            os.environ["WECHAT_WIN32_OCR_UI_ACTION_BUDGET_ENABLED"] = previous_budget


def test_rpa_action_pacing_covers_keyboard_mouse_and_window_image_clicks() -> None:
    source_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
    source = source_path.read_text(encoding="utf-8")
    assert_true(
        "def coordinate_rpa_action" in source,
        "RPA should have one shared action pacing coordinator",
    )
    assert_true(
        "WECHAT_WIN32_OCR_UI_ACTION_KIND_SWITCH_GAP_MS" in source
        and "WECHAT_WIN32_OCR_UI_ACTION_NEAR_POINT_GAP_MS" in source,
        "RPA action pacing should expose kind-switch and near-point repeat controls",
    )
    image_click_body = source[source.find("def human_window_image_click") : source.find("def client_to_screen")]
    assert_true(
        "jitter_window_image_click_surface_point" in image_click_body,
        "window-image clicks should jitter fixed screenshot coordinates before clicking",
    )
    key_body = source[source.find("def key_press") : source.find("def is_wechat_main_window")]
    hotkey_body = source[source.find("def hotkey") : source.find("def key_press")]
    sendinput_body = source[source.find("def sendinput_unicode_unit") : source.find("def type_text_with_sendinput_unicode")]
    assert_true("coordinate_rpa_action" in key_body, "single key presses should be paced")
    assert_true("coordinate_rpa_action" in hotkey_body, "hotkeys should be paced")
    assert_true("coordinate_rpa_action" in sendinput_body, "SendInput unicode units should be paced")
    budget_body = source[source.find("def active_ui_action_budget_decision") : source.find("def record_ui_action")]
    assert_true(
        "metadata" in budget_body and "coordinate_rpa_action" in budget_body,
        "UI action budget should coordinate pacing with point metadata",
    )


def test_passive_probe_window_discovery_is_non_interactive() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    original_probe = sidecar_mod.probe_wechat_windows
    original_focus = sidecar_mod.focus_wechat_window
    original_restore = sidecar_mod.restore_wechat_window
    calls = {"focus": 0, "restore": 0}
    try:
        def fake_probe():
            return {
                "visible_main_windows": [{"hwnd": 1}],
                "windows": [{"hwnd": 1}],
            }

        def fake_focus(_probe):
            calls["focus"] += 1
            return {"hwnd": 1}

        def fake_restore(_probe):
            calls["restore"] += 1
            return {"hwnd": 1}

        sidecar_mod.probe_wechat_windows = fake_probe
        sidecar_mod.focus_wechat_window = fake_focus
        sidecar_mod.restore_wechat_window = fake_restore

        passive_probe = ensure_visible_wechat_window(interactive=False)
        active_probe = ensure_visible_wechat_window(interactive=True)
        assert_true(bool(passive_probe.get("visible_main_windows")), f"passive probe should still discover windows: {passive_probe}")
        assert_true(bool(active_probe.get("visible_main_windows")), f"interactive probe should discover windows: {active_probe}")
        assert_true(calls["focus"] == 1, f"focus should be skipped for passive and called once for interactive: {calls}")
        assert_true(calls["restore"] == 0, f"restore should not run when visible window exists: {calls}")
    finally:
        sidecar_mod.probe_wechat_windows = original_probe
        sidecar_mod.focus_wechat_window = original_focus
        sidecar_mod.restore_wechat_window = original_restore


def test_interactive_probe_restores_offscreen_visible_window() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "probe_wechat_windows": sidecar_mod.probe_wechat_windows,
        "focus_wechat_window": sidecar_mod.focus_wechat_window,
        "restore_wechat_window": sidecar_mod.restore_wechat_window,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "time_sleep": sidecar_mod.time.sleep,
    }
    calls = {"probe": 0, "focus": 0, "restore": 0}

    def fake_probe():
        calls["probe"] += 1
        if calls["probe"] == 1:
            return {
                "windows": [{"hwnd": 1001, "title": "微信"}],
                "visible_main_windows": [{"hwnd": 1001, "title": "微信"}],
            }
        return {
            "windows": [{"hwnd": 1002, "title": "微信"}],
            "visible_main_windows": [{"hwnd": 1002, "title": "微信"}],
        }

    def fake_get_window_geometry(hwnd):
        if int(hwnd) == 1001:
            return {"left": -32000, "top": -32000, "right": -31801, "bottom": -31966, "width": 199, "height": 34}
        return {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}

    def fake_focus(probe):
        calls["focus"] += 1
        return dict((probe.get("visible_main_windows") or [{}])[0])

    def fake_restore(_probe):
        calls["restore"] += 1
        return {"hwnd": 1001, "title": "微信"}

    try:
        sidecar_mod.probe_wechat_windows = fake_probe
        sidecar_mod.get_window_geometry = fake_get_window_geometry
        sidecar_mod.focus_wechat_window = fake_focus
        sidecar_mod.restore_wechat_window = fake_restore
        sidecar_mod.time.sleep = lambda *_args, **_kwargs: None
        probe = ensure_visible_wechat_window(interactive=True)
        assert_true(calls["restore"] == 1, f"offscreen visible window should trigger restore: {calls}")
        assert_true(calls["focus"] == 1, f"restored window should be focused: {calls}")
        assert_true(
            int((probe.get("visible_main_windows") or [{}])[0].get("hwnd") or 0) == 1002,
            f"probe should refresh after restore: {probe}",
        )
    finally:
        sidecar_mod.probe_wechat_windows = originals["probe_wechat_windows"]
        sidecar_mod.focus_wechat_window = originals["focus_wechat_window"]
        sidecar_mod.restore_wechat_window = originals["restore_wechat_window"]
        sidecar_mod.get_window_geometry = originals["get_window_geometry"]
        sidecar_mod.time.sleep = originals["time_sleep"]


def test_capture_geometry_guard_and_window_selection() -> None:
    bad_geometry = {"left": -32000, "top": -32000, "width": 199, "height": 34}
    good_geometry = {"left": 0, "top": 0, "width": 980, "height": 860}
    bad_check = validate_capture_geometry(bad_geometry)
    good_check = validate_capture_geometry(good_geometry)
    assert_true(bad_check.get("ok") is False, f"offscreen/tiny geometry should be rejected: {bad_check}")
    assert_true(good_check.get("ok") is True, f"normal geometry should pass capture guard: {good_check}")

    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    original_get_window_geometry = sidecar_mod.get_window_geometry
    try:
        geometry_map = {
            1001: {"left": -32000, "top": -32000, "width": 199, "height": 34},
            1002: {"left": 0, "top": 0, "width": 980, "height": 860},
        }

        def fake_get_window_geometry(hwnd):
            return geometry_map.get(int(hwnd), {"left": 0, "top": 0, "width": 0, "height": 0})

        sidecar_mod.get_window_geometry = fake_get_window_geometry
        selected = select_primary_visible_main_window(
            {
                "visible_main_windows": [
                    {"hwnd": 1001, "title": "微信"},
                    {"hwnd": 1002, "title": "微信"},
                ]
            }
        )
        assert_true(int((selected or {}).get("hwnd") or 0) == 1002, f"selection should prefer healthy window geometry: {selected}")
    finally:
        sidecar_mod.get_window_geometry = original_get_window_geometry


def test_window_selection_prefers_real_wechat_title_over_weixin_shell() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    original_get_window_geometry = sidecar_mod.get_window_geometry
    original_activate = sidecar_mod.activate_window
    original_foreground_match = sidecar_mod.foreground_window_matches_target
    calls: list[int] = []
    try:
        sidecar_mod.get_window_geometry = lambda hwnd: {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
        sidecar_mod.activate_window = lambda hwnd: calls.append(int(hwnd))
        sidecar_mod.foreground_window_matches_target = lambda hwnd: {
            "ok": True,
            "reason": "foreground_matches_target",
            "hwnd": int(hwnd or 0),
            "foreground_hwnd": int(hwnd or 0),
        }
        probe = {
            "windows": [
                {"hwnd": 1001, "title": "Weixin", "class_name": "Qt51514QWindowIcon", "visible": True},
                {"hwnd": 1002, "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": True},
            ],
            "visible_main_windows": [
                {"hwnd": 1001, "title": "Weixin", "class_name": "Qt51514QWindowIcon", "visible": True},
                {"hwnd": 1002, "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": True},
            ],
        }
        selected = select_primary_visible_main_window(probe)
        assert_true(int((selected or {}).get("hwnd") or 0) == 1002, f"should prefer real 微信 window: {selected}")
        focused = sidecar_mod.focus_wechat_window(probe)
        assert_true(int((focused or {}).get("hwnd") or 0) == 1002, f"focus should use selected real window: {focused}")
        restored = sidecar_mod.restore_wechat_window(probe)
        assert_true(int((restored or {}).get("hwnd") or 0) == 1002, f"restore should prefer real window: {restored}")
        assert_true(calls == [1002, 1002], f"unexpected activation calls: {calls}")
    finally:
        sidecar_mod.get_window_geometry = original_get_window_geometry
        sidecar_mod.activate_window = original_activate
        sidecar_mod.foreground_window_matches_target = original_foreground_match


def test_window_selection_prefers_large_actionable_window_over_quick_login_title() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    original_get_window_geometry = sidecar_mod.get_window_geometry
    previous_probe = os.environ.get("WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE")
    try:
        os.environ["WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE"] = "0"
        geometry_map = {
            1001: {"left": 775, "top": 331, "right": 1143, "bottom": 815, "width": 368, "height": 484},
            1002: {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860},
        }
        sidecar_mod.get_window_geometry = lambda hwnd: geometry_map[int(hwnd)]
        selected = select_primary_visible_main_window(
            {
                "visible_main_windows": [
                    {"hwnd": 1001, "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": True},
                    {"hwnd": 1002, "title": "Weixin", "class_name": "Qt51514QWindowIcon", "visible": True},
                ]
            }
        )
        assert_true(
            int((selected or {}).get("hwnd") or 0) == 1002,
            f"selection should prefer large actionable main window over small quick-login/title window: {selected}",
        )
    finally:
        if previous_probe is None:
            os.environ.pop("WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE", None)
        else:
            os.environ["WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE"] = previous_probe
        sidecar_mod.get_window_geometry = original_get_window_geometry


def test_window_selection_prefers_readable_window_over_larger_blank_window() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    original_get_window_geometry = sidecar_mod.get_window_geometry
    original_capture = sidecar_mod.capture_wechat
    original_run_ocr = sidecar_mod.run_ocr
    previous_probe = os.environ.get("WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE")
    try:
        os.environ["WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE"] = "1"
        geometry_map = {
            1001: {"left": 0, "top": 0, "right": 981, "bottom": 860, "width": 981, "height": 860},
            1002: {"left": 0, "top": 0, "right": 785, "bottom": 688, "width": 785, "height": 688},
        }
        blank = Image.new("RGB", (981, 860), color=(255, 255, 255))
        ImageDraw.Draw(blank).rectangle((0, 0, 980, 859), outline=(118, 118, 118), width=8)
        chat = Image.new("RGB", (785, 688), color=(245, 245, 245))

        sidecar_mod.get_window_geometry = lambda hwnd: geometry_map[int(hwnd)]
        sidecar_mod.capture_wechat = lambda hwnd, artifact_dir=None, label="window_select_probe": (
            blank if int(hwnd) == 1001 else chat,
            f"{hwnd}.png",
        )
        sidecar_mod.run_ocr = lambda image: [] if image is blank else [
            {"text": "Q搜索", "left": 100, "right": 170, "top": 60, "bottom": 84},
            {"text": "文件传输助手", "left": 155, "right": 260, "top": 118, "bottom": 140},
            {"text": "发送", "left": 710, "right": 752, "top": 638, "bottom": 664},
        ]
        selected = select_primary_visible_main_window(
            {
                "visible_main_windows": [
                    {"hwnd": 1001, "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": True},
                    {"hwnd": 1002, "title": "微信", "class_name": "Qt51514QWindowIcon", "visible": True},
                ]
            }
        )
        assert_true(
            int((selected or {}).get("hwnd") or 0) == 1002,
            f"selection should prefer readable chat surface over larger blank render: {selected}",
        )
        assert_true(
            int((selected or {}).get("content_health_score") or 0) > 0,
            f"readable selected window should expose positive content health score: {selected}",
        )
    finally:
        if previous_probe is None:
            os.environ.pop("WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE", None)
        else:
            os.environ["WECHAT_WIN32_OCR_MULTI_WINDOW_CONTENT_PROBE"] = previous_probe
        sidecar_mod.get_window_geometry = original_get_window_geometry
        sidecar_mod.capture_wechat = original_capture
        sidecar_mod.run_ocr = original_run_ocr


def test_auxiliary_wechat_shell_is_blocked() -> None:
    shell = auxiliary_wechat_shell_like([{"text": "Weixin"}], geometry={"width": 784, "height": 688})
    assert_true(shell.get("detected") is True, f"title-only shell should be blocked: {shell}")
    chat = auxiliary_wechat_shell_like(
        [{"text": "文件传输助手"}, {"text": "Q搜索"}, {"text": "您好"}],
        geometry={"width": 784, "height": 688},
    )
    assert_true(chat.get("detected") is False, f"real chat surface should pass: {chat}")


def test_normalize_wechat_window_clamps_offscreen_when_size_is_already_safe() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    if not hasattr(sidecar_mod.ctypes, "windll"):
        return
    original_get_window_geometry = sidecar_mod.get_window_geometry
    original_win32gui = sidecar_mod.win32gui
    if sidecar_mod.win32gui is None:
        sidecar_mod.win32gui = types.SimpleNamespace(MoveWindow=lambda *_args, **_kwargs: None)
    original_move_window = sidecar_mod.win32gui.MoveWindow
    original_windll = sidecar_mod.ctypes.windll
    previous_fixed_origin = os.environ.get("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN")
    geometry_state = {"left": -180, "top": 80, "right": 800, "bottom": 940, "width": 980, "height": 860}
    calls: list[tuple[int, int, int, int]] = []

    class FakeUser32:
        @staticmethod
        def GetSystemMetrics(index: int) -> int:
            return 1920 if index == 0 else 1200

    class FakeWindll:
        user32 = FakeUser32()

    def fake_get_window_geometry(_hwnd: int) -> dict[str, int]:
        return dict(geometry_state)

    def fake_move_window(_hwnd: int, left: int, top: int, width: int, height: int, _repaint: bool) -> None:
        calls.append((left, top, width, height))
        geometry_state.update(
            {
                "left": left,
                "top": top,
                "right": left + width,
                "bottom": top + height,
                "width": width,
                "height": height,
            }
        )

    try:
        os.environ.pop("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN", None)
        sidecar_mod.get_window_geometry = fake_get_window_geometry
        sidecar_mod.win32gui.MoveWindow = fake_move_window
        sidecar_mod.ctypes.windll = FakeWindll()
        result = normalize_wechat_window(1001)
        assert_true(result.get("ok") is True, f"normalization should pass: {result}")
        assert_true(result.get("applied") is True, f"offscreen same-size window must be moved: {result}")
        assert_true(calls == [(0, 0, 980, 860)], f"unexpected move call: {calls}")
        assert_true((result.get("after") or {}).get("left") == 0, f"window should be clamped on-screen: {result}")
        assert_true((result.get("after") or {}).get("top") == 0, f"window should use fixed top origin: {result}")
    finally:
        if previous_fixed_origin is None:
            os.environ.pop("WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN", None)
        else:
            os.environ["WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN"] = previous_fixed_origin
        sidecar_mod.get_window_geometry = original_get_window_geometry
        sidecar_mod.win32gui.MoveWindow = original_move_window
        sidecar_mod.win32gui = original_win32gui
        sidecar_mod.ctypes.windll = original_windll


def test_capabilities_success_exposes_top_level_geometry() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    screenshot = Image.new("RGB", (980, 860), "white")
    originals = {
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "validate_capture_geometry": sidecar_mod.validate_capture_geometry,
        "capture_wechat": sidecar_mod.capture_wechat,
        "run_ocr": sidecar_mod.run_ocr,
        "quick_login_like": sidecar_mod.quick_login_like,
        "detect_blank_render": sidecar_mod.detect_blank_render,
        "blocking_screen_reason": sidecar_mod.blocking_screen_reason,
        "validate_send_geometry": sidecar_mod.validate_send_geometry,
        "calculate_send_points": sidecar_mod.calculate_send_points,
        "inspect_uia_send_capability": sidecar_mod.inspect_uia_send_capability,
    }
    try:
        sidecar_mod.get_window_geometry = lambda hwnd: geometry
        sidecar_mod.validate_capture_geometry = lambda current: {"ok": True, "reason": "capture_geometry_ok", "geometry": current}
        sidecar_mod.capture_wechat = lambda hwnd, artifact_dir=None, label="capabilities": (screenshot, "capabilities.png")
        sidecar_mod.run_ocr = lambda image: [{"text": "文件传输助手", "left": 100, "top": 100, "right": 240, "bottom": 130}]
        sidecar_mod.quick_login_like = lambda items, geometry=None: False
        sidecar_mod.detect_blank_render = lambda image, items, geometry=None: {"detected": False}
        sidecar_mod.blocking_screen_reason = lambda items: ""
        sidecar_mod.validate_send_geometry = lambda current: {"ok": True, "reason": "geometry_ok", "geometry": current}
        sidecar_mod.calculate_send_points = lambda current: {
            "ok": True,
            "input_point": [637, 715],
            "send_point": [918, 816],
            "geometry": current,
        }
        sidecar_mod.inspect_uia_send_capability = lambda hwnd, current: {"ok": False, "reason": "uia_unavailable"}
        payload = capabilities_payload(1001, {"passive_probe": True}, artifact_dir=None)
        assert_true(payload.get("ok") is True, f"capabilities should pass: {payload}")
        assert_true(payload.get("geometry") == geometry, f"success payload should expose geometry: {payload}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_blank_render_detection_for_empty_white_capture() -> None:
    screenshot = Image.new("RGB", (980, 860), color=(255, 255, 255))
    detected = detect_blank_render(screenshot, [], geometry={"width": 980, "height": 860})
    assert_true(detected.get("detected") is True, f"blank render should be detected for pure white capture: {detected}")
    assert_true(detected.get("reason") == "blank_white_like", f"unexpected blank reason: {detected}")
    not_detected_with_ocr = detect_blank_render(
        screenshot,
        [{"text": "微信", "left": 12, "top": 8, "right": 58, "bottom": 26}],
        geometry={"width": 980, "height": 860},
    )
    assert_true(
        not_detected_with_ocr.get("detected") is False,
        f"blank render should not trigger when OCR sees visible text: {not_detected_with_ocr}",
    )


def test_blank_render_detection_for_bordered_white_capture() -> None:
    screenshot = Image.new("RGB", (981, 860), color=(255, 255, 255))
    draw = ImageDraw.Draw(screenshot)
    draw.rectangle((0, 0, 980, 859), outline=(118, 118, 118), width=8)
    detected = detect_blank_render(screenshot, [], geometry={"width": 981, "height": 860})
    assert_true(
        detected.get("detected") is True,
        f"bordered white render stall should be detected even when border raises stddev: {detected}",
    )
    assert_true(
        detected.get("reason") == "blank_bordered_white_like",
        f"bordered blank render reason should be explicit: {detected}",
    )


def test_send_guard_blocks_blank_render_before_file_transfer_blind_send() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    screenshot = Image.new("RGB", (980, 860), color=(255, 255, 255))
    originals = {
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "validate_send_geometry": sidecar_mod.validate_send_geometry,
        "capture_wechat": sidecar_mod.capture_wechat,
        "run_ocr": sidecar_mod.run_ocr,
    }
    try:
        sidecar_mod.get_window_geometry = lambda hwnd: geometry
        sidecar_mod.validate_send_geometry = lambda current: {"ok": True, "reason": "geometry_ok", "geometry": current}
        sidecar_mod.capture_wechat = lambda hwnd, artifact_dir=None, label="send_guard": (screenshot, "blank.png")
        sidecar_mod.run_ocr = lambda image: []
        guard = validate_active_send_target(1001, "文件传输助手", exact=True)
        assert_true(guard.get("ok") is False, f"blank render must block blind file-transfer send: {guard}")
        assert_true(guard.get("reason") == "blank_render", f"blank render reason should be explicit: {guard}")
        assert_true("blind_send" not in guard, f"blank render should not be marked as blind-send-safe: {guard}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_humanized_chunk_text_and_settings() -> None:
    previous = {
        name: os.environ.get(name)
        for name in (
            "WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED",
            "WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS",
            "WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY",
        )
    }
    try:
        os.environ["WECHAT_WIN32_OCR_HUMANIZED_INPUT_ENABLED"] = "1"
        os.environ["WECHAT_WIN32_OCR_HUMANIZED_INPUT_METHOD"] = "clipboard_chunks"
        os.environ["WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MIN_CHARS"] = "3"
        os.environ["WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHUNK_MAX_CHARS"] = "3"
        os.environ["WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MIN_MS"] = "0"
        os.environ["WECHAT_WIN32_OCR_HUMANIZED_TYPING_CHAR_DELAY_MAX_MS"] = "0"
        os.environ["WECHAT_WIN32_OCR_HUMANIZED_TYPING_TYPO_PROBABILITY"] = "0"
        settings = humanized_input_settings()
        assert_true(settings.get("enabled") is True, f"humanized input should be enabled: {settings}")
        assert_true(settings.get("method") == "clipboard_chunks", f"method should be parsed: {settings}")
        chunks = humanized_chunk_text("abcdefghij", min_chars=3, max_chars=3)
        assert_true(chunks == ["abc", "def", "ghi", "j"], f"chunking should be deterministic under fixed size: {chunks}")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_adaptive_humanized_input_speed_profiles() -> None:
    base = {
        "enabled": True,
        "method": "sendinput_unicode",
        "chunk_min_chars": 2,
        "chunk_max_chars": 6,
        "char_delay_min_ms": 50,
        "char_delay_max_ms": 180,
        "micro_pause_every_chars": 18,
        "micro_pause_min_ms": 220,
        "micro_pause_max_ms": 650,
        "typo_probability": 0.22,
        "typo_max": 1,
        "send_pre_delay_min_ms": 280,
        "send_pre_delay_max_ms": 1300,
        "send_post_input_delay_min_ms": 120,
        "send_post_input_delay_max_ms": 460,
        "adaptive_speed_enabled": True,
    }
    short = adapt_humanized_input_settings(base, "这台车今天能看吗？")
    assert_true(short.get("speed_profile") == "short_natural", f"short reply should use natural profile: {short}")
    assert_true(3 <= int(short.get("chunk_min_chars") or 0) <= 7, f"short chunks should remain human-sized: {short}")
    assert_true(45 <= int(short.get("char_delay_min_ms") or 0) <= 125, f"short char delay should not be superhuman: {short}")
    assert_true(18 <= int(short.get("micro_pause_every_chars") or 0) <= 40, f"short pauses should remain natural: {short}")
    assert_true(
        int(short.get("send_post_input_delay_min_ms") or 0) >= 120,
        f"short profile should keep a non-zero post-input pause: {short}",
    )
    assert_true(
        int(short.get("send_post_input_delay_max_ms") or 0) == 360,
        f"short profile should not inherit overly wide default post-input pause: {short}",
    )

    long_text = "这边先帮您确认几个点。" * 40
    long_profile = adapt_humanized_input_settings(base, long_text)
    assert_true(long_profile.get("speed_profile") == "long_natural_capped", f"long reply should use natural capped profile: {long_profile}")
    assert_true(int(long_profile.get("chunk_max_chars") or 0) <= 12, f"long chunks should remain bounded: {long_profile}")
    assert_true(32 <= int(long_profile.get("char_delay_min_ms") or 0) <= 95, f"long char delay should stay human-like: {long_profile}")

    disabled = dict(base)
    disabled["adaptive_speed_enabled"] = False
    unchanged = adapt_humanized_input_settings(disabled, "这台车今天能看吗？")
    assert_true(unchanged.get("speed_profile") is None, f"disabled adaptive profile should not mutate speed: {unchanged}")


def test_adaptive_humanized_input_clamps_live_wide_waits_by_profile() -> None:
    live_wide = {
        "enabled": True,
        "method": "sendinput_unicode",
        "chunk_min_chars": 2,
        "chunk_max_chars": 7,
        "char_delay_min_ms": 45,
        "char_delay_max_ms": 145,
        "micro_pause_every_chars": 18,
        "micro_pause_min_ms": 180,
        "micro_pause_max_ms": 480,
        "typo_probability": 0.12,
        "typo_max": 1,
        "send_pre_delay_min_ms": 500,
        "send_pre_delay_max_ms": 1300,
        "send_post_input_delay_min_ms": 450,
        "send_post_input_delay_max_ms": 1200,
        "send_trigger_delay_min_ms": 720,
        "send_trigger_delay_max_ms": 2100,
        "send_after_trigger_delay_min_ms": 420,
        "send_after_trigger_delay_max_ms": 1250,
        "adaptive_speed_enabled": True,
    }
    short = adapt_humanized_input_settings(live_wide, "你好呀，有什么可以帮您的吗？")
    assert_true(short.get("speed_profile") == "short_natural", f"short reply should use short profile: {short}")
    assert_true(short.get("send_post_input_delay_min_ms") == 180, f"short post-input min should be profiled: {short}")
    assert_true(short.get("send_post_input_delay_max_ms") == 360, f"short post-input max should be profiled: {short}")
    assert_true(short.get("send_trigger_delay_min_ms") == 520, f"short trigger min should be profiled: {short}")
    assert_true(short.get("send_trigger_delay_max_ms") == 1500, f"short trigger max should be profiled: {short}")
    assert_true(short.get("send_after_trigger_delay_min_ms") == 260, f"short after-trigger min should be profiled: {short}")
    assert_true(short.get("send_after_trigger_delay_max_ms") == 820, f"short after-trigger max should be profiled: {short}")

    long_profile = adapt_humanized_input_settings(live_wide, "这边先帮您确认几个点。" * 40)
    assert_true(long_profile.get("speed_profile") == "long_natural_capped", f"long reply should use long profile: {long_profile}")
    assert_true(long_profile.get("send_trigger_delay_max_ms") == 2200, f"long trigger window should stay wider: {long_profile}")


def test_set_uia_control_value_humanized_progressive_updates() -> None:
    settings = {
        "chunk_min_chars": 2,
        "chunk_max_chars": 2,
        "char_delay_min_ms": 0,
        "char_delay_max_ms": 0,
        "micro_pause_every_chars": 0,
        "micro_pause_min_ms": 0,
        "micro_pause_max_ms": 0,
        "typo_probability": 0.0,
        "typo_max": 0,
    }
    pattern = FakeValuePattern()
    result = set_uia_control_value_humanized(pattern, "你好ABCDE", settings)
    assert_true(result.get("ok") is True, f"uia humanized set value should succeed: {result}")
    assert_true(len(pattern.values) >= 2, f"humanized set value should write progressively: {pattern.values}")
    assert_true(pattern.values[-1] == "你好ABCDE", f"final value should match full text: {pattern.values}")


def test_sendinput_unicode_text_entry_helpers() -> None:
    settings = {
        "chunk_min_chars": 2,
        "chunk_max_chars": 2,
        "char_delay_min_ms": 0,
        "char_delay_max_ms": 0,
        "micro_pause_every_chars": 0,
        "micro_pause_min_ms": 0,
        "micro_pause_max_ms": 0,
        "typo_probability": 0.0,
        "typo_max": 0,
    }
    sent_units: list[int] = []
    result = type_text_with_sendinput_unicode("你\n好AB", settings, send_unit_func=sent_units.append)
    expected_text = "你 好AB"
    assert_true(result.get("ok") is True, f"sendinput unicode helper should succeed: {result}")
    assert_true(result.get("method") == "sendinput_unicode", f"method should be explicit: {result}")
    assert_true(result.get("normalized_newlines") is True, f"newlines should be normalized: {result}")
    assert_true(sent_units == sendinput_utf16_units(expected_text), f"typed utf16 units should match: {sent_units}")
    assert_true(sendinput_safe_text("a\r\nb\t c") == "a b c", "sendinput text should avoid accidental line-send")


def test_sendinput_unicode_aborts_when_window_guard_fails() -> None:
    settings = {
        "chunk_min_chars": 2,
        "chunk_max_chars": 2,
        "char_delay_min_ms": 0,
        "char_delay_max_ms": 0,
        "micro_pause_every_chars": 0,
        "micro_pause_min_ms": 0,
        "micro_pause_max_ms": 0,
        "typo_probability": 0.0,
        "typo_max": 0,
    }
    sent_units: list[int] = []
    calls = {"count": 0}

    def guard() -> dict[str, object]:
        calls["count"] += 1
        if calls["count"] >= 2:
            return {"ok": False, "reason": "unit_window_lost"}
        return {"ok": True}

    result = type_text_with_sendinput_unicode(
        "ABCDEFGH",
        settings,
        send_unit_func=sent_units.append,
        window_guard_func=guard,
    )
    assert_true(result.get("ok") is False, f"typing should abort on window loss: {result}")
    assert_true(result.get("reason") == "window_lost_during_sendinput", f"abort reason should be explicit: {result}")
    assert_true(len(sent_units) == 0, f"no unit should be typed after an immediate focus guard failure: {sent_units}")


def test_send_trigger_mode_defaults_to_enter_only() -> None:
    previous = os.environ.get("WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER")
    try:
        os.environ.pop("WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER", None)
        assert_true(normalize_send_trigger_mode(None) == "enter_only", "default send trigger should avoid clicking the send button")
        assert_true(normalize_send_trigger_mode("click_only") == "enter_only", "click send trigger should be disabled by default")
        assert_true(normalize_send_trigger_mode("enter_then_click") == "enter_only", "dual trigger should never be used")
        assert_true(normalize_send_trigger_mode("bad") == "enter_only", "bad trigger mode should fail safe")
        os.environ["WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER"] = "1"
        assert_true(normalize_send_trigger_mode("click_only") == "click_only", "single-click trigger should require explicit debug opt-in")
        assert_true(normalize_send_trigger_mode("enter_then_click") == "enter_only", "dual trigger should remain disabled even with click opt-in")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER", None)
        else:
            os.environ["WECHAT_WIN32_OCR_ALLOW_CLICK_SEND_TRIGGER"] = previous


def test_safe_send_trigger_uses_single_enter_with_randomized_delays() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "humanized_sleep_ms": sidecar_mod.humanized_sleep_ms,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
        "coordinate_rpa_action": sidecar_mod.coordinate_rpa_action,
        "ensure_left_button_released": sidecar_mod.ensure_left_button_released,
        "keybd_event": sidecar_mod.win32api.keybd_event,
        "human_client_click": sidecar_mod.human_client_click,
    }
    calls = {"sleep": [], "action": [], "key": [], "release": 0, "click": 0}
    try:
        sidecar_mod.humanized_sleep_ms = lambda low, high: calls["sleep"].append((int(low), int(high))) or 0.0
        sidecar_mod.humanized_action_sleep = lambda low, high=None: calls["sleep"].append((int(low), int(high or low))) or 0.0
        sidecar_mod.coordinate_rpa_action = lambda action, metadata=None: calls["action"].append((action, metadata or {})) or {"ok": True}
        sidecar_mod.ensure_left_button_released = lambda: calls.__setitem__("release", calls["release"] + 1)
        sidecar_mod.win32api.keybd_event = lambda key, *_args: calls["key"].append(int(key))
        sidecar_mod.human_client_click = lambda *_args, **_kwargs: calls.__setitem__("click", calls["click"] + 1)
        result = safe_send_trigger(
            1001,
            trigger_mode="enter_only",
            settings={
                "enabled": True,
                "send_trigger_delay_min_ms": 520,
                "send_trigger_delay_max_ms": 1500,
                "send_after_trigger_delay_min_ms": 260,
                "send_after_trigger_delay_max_ms": 820,
            },
            focus_guard_func=lambda: {"ok": True, "reason": "window_valid"},
        )
        assert_true(result.get("ok") is True, f"safe trigger should pass: {result}")
        assert_true(calls["click"] == 0, f"default send trigger must not click send button: {calls}")
        assert_true(calls["key"].count(sidecar_mod.win32con.VK_RETURN) == 2, f"one key down/up pair expected: {calls}")
        assert_true(calls["release"] >= 1, f"mouse button should be released before keyboard send: {calls}")
        assert_true(any(item[0] == "send_trigger_enter" for item in calls["action"]), f"send trigger should be audited: {calls}")
        assert_true((520, 1500) in calls["sleep"], f"pre-trigger randomized wait should be used: {calls}")
        assert_true((260, 820) in calls["sleep"], f"post-trigger randomized wait should be used: {calls}")
    finally:
        for name, value in originals.items():
            if name == "keybd_event":
                sidecar_mod.win32api.keybd_event = value
            else:
                setattr(sidecar_mod, name, value)


def test_send_with_guarded_clicks_skips_input_refocus_after_confirmed_paste() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "jitter_send_click_point": sidecar_mod.jitter_send_click_point,
        "humanized_sleep_ms": sidecar_mod.humanized_sleep_ms,
        "paste_text_with_confirmation": sidecar_mod.paste_text_with_confirmation,
        "recover_send_window_guard": sidecar_mod.recover_send_window_guard,
        "safe_send_trigger": sidecar_mod.safe_send_trigger,
        "human_client_click": sidecar_mod.human_client_click,
    }
    calls = {"click": 0, "trigger": 0, "guards": 0, "sleep": []}
    try:
        sidecar_mod.jitter_send_click_point = lambda x, y, geometry: (int(x), int(y))
        sidecar_mod.humanized_sleep_ms = lambda low, high: calls["sleep"].append((int(low), int(high))) or 0.0
        sidecar_mod.paste_text_with_confirmation = lambda *_args, **_kwargs: {
            "ok": True,
            "point": [718, 736],
            "input_mode": "sendinput_unicode",
            "confirmed_by": "ocr_input_area",
        }

        def fake_guard(*_args, **_kwargs):
            calls["guards"] += 1
            return {"ok": True, "reason": "window_valid"}

        sidecar_mod.recover_send_window_guard = fake_guard
        sidecar_mod.safe_send_trigger = lambda *_args, **_kwargs: calls.__setitem__("trigger", calls["trigger"] + 1) or {
            "ok": True,
            "method": "keyboard_enter",
            "send_trigger_mode": "enter_only",
        }
        sidecar_mod.human_client_click = lambda *_args, **_kwargs: calls.__setitem__("click", calls["click"] + 1)
        result = send_with_guarded_clicks(
            1001,
            "你好，我想给老婆看一台时尚点、不太贵的二手车，别太大，你先直接帮我推荐方向。",
            points={"input_point": [637, 715], "send_point": [919, 816]},
            geometry={"left": 0, "top": 0, "right": 981, "bottom": 860, "width": 981, "height": 860},
            settings={
                "enabled": True,
                "send_post_input_delay_min_ms": 450,
                "send_post_input_delay_max_ms": 1200,
                "send_trigger_delay_min_ms": 720,
                "send_trigger_delay_max_ms": 2100,
            },
        )
        assert_true(result.get("ok") is True, f"guarded click send should pass: {result}")
        assert_true(calls["click"] == 0, f"confirmed input must not be clicked again before Enter: {calls}")
        assert_true(calls["trigger"] == 1, f"send trigger should run once: {calls}")
        assert_true(result.get("input_refocus", {}).get("skipped") is True, f"input refocus should be explicit: {result}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_input_text_region_state_distinguishes_blank_and_text() -> None:
    geometry = {"left": 0, "top": 0, "width": 980, "height": 860}
    blank = Image.new("RGB", (980, 860), "white")
    blank_state = input_text_region_state(blank, [], geometry=geometry)
    assert_true(blank_state.get("has_visible_text") is False, f"blank input should stay retry-safe: {blank_state}")
    blank_with_boundary_ocr = input_text_region_state(
        blank,
        [
            {"text": "按您说的9万", "left": 400, "top": 688, "right": 560, "bottom": 712},
            {"text": "顺序排清楚", "left": 600, "top": 735, "right": 780, "bottom": 760},
        ],
        geometry=geometry,
    )
    assert_true(
        blank_with_boundary_ocr.get("has_visible_text") is False,
        f"OCR boundary drift on a white input area should not block typing: {blank_with_boundary_ocr}",
    )
    dark_blank = Image.new("RGB", (980, 860), (30, 30, 30))
    dark_blank_state = input_text_region_state(dark_blank, [], geometry=geometry)
    assert_true(
        dark_blank_state.get("has_visible_text") is False,
        f"dark-mode blank input should stay retry-safe: {dark_blank_state}",
    )
    dark_blank_with_boundary_ocr = input_text_region_state(
        dark_blank,
        [
            {
                "text": "许聪",
                "left": 356,
                "top": 670,
                "right": 410,
                "bottom": 705,
            }
        ],
        geometry=geometry,
    )
    assert_true(
        dark_blank_with_boundary_ocr.get("has_visible_text") is False,
        f"dark-mode blank input with boundary OCR noise should stay retry-safe: {dark_blank_with_boundary_ocr}",
    )
    dark_text_image = dark_blank.copy()
    ImageDraw.Draw(dark_text_image).rectangle([370, 690, 560, 715], fill="white")
    dark_text_state = input_text_region_state(dark_text_image, [], geometry=geometry)
    assert_true(
        dark_text_state.get("has_visible_text") is True,
        f"bright strokes on dark input should still block duplicate typing: {dark_text_state}",
    )
    text_image = blank.copy()
    ImageDraw.Draw(text_image).rectangle([370, 690, 560, 715], fill="black")
    text_state = input_text_region_state(
        text_image,
        [
            {
                "text": "奇骏和哈弗H6哪个更",
                "left": 370,
                "top": 690,
                "right": 560,
                "bottom": 715,
            }
        ],
        geometry=geometry,
    )
    assert_true(text_state.get("has_visible_text") is True, f"OCR text should block duplicate retry: {text_state}")


def test_send_rpa_env_enables_strict_focus_single_confirm_and_blank_retry() -> None:
    previous = os.environ.get("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS")
    previous_retry = os.environ.get("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY")
    previous_strict = os.environ.get("WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD")
    previous_fast_visual = os.environ.get("WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM")
    try:
        os.environ.pop("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS", None)
        os.environ.pop("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY", None)
        os.environ.pop("WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD", None)
        os.environ["WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM"] = "1"
        env = send_rpa_env()
        assert_true(env.get("WECHAT_WIN32_OCR_AGGRESSIVE_FOCUS") == "1", f"send env should force focus: {env}")
        assert_true(env.get("WECHAT_WIN32_OCR_ATTACH_THREAD_INPUT") == "1", f"send env should attach input: {env}")
        assert_true(env.get("WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD") == "1", f"send env should guard foreground focus: {env}")
        assert_true(env.get("WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND") == "1", f"send env should allow unknown-foreground guarded degrade: {env}")
        assert_true(
            env.get("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY") == "1",
            f"send env should allow one blank-input focus recovery without row reclicks: {env}",
        )
        assert_true(env.get("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS") == "1", f"send env should use one confirmed input attempt: {env}")
        assert_true(env.get("WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM") == "1", f"send env should propagate fast visual input confirm: {env}")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS", None)
        else:
            os.environ["WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"] = previous
        if previous_retry is None:
            os.environ.pop("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY", None)
        else:
            os.environ["WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY"] = previous_retry
        if previous_strict is None:
            os.environ.pop("WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD", None)
        else:
            os.environ["WECHAT_WIN32_OCR_STRICT_SEND_FOCUS_GUARD"] = previous_strict
        if previous_fast_visual is None:
            os.environ.pop("WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM", None)
        else:
            os.environ["WECHAT_WIN32_OCR_INPUT_FAST_VISUAL_CONFIRM"] = previous_fast_visual


def test_activate_window_debounces_aggressive_refocus() -> None:
    source_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
    activation_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr" / "window_activation.py"
    source = source_path.read_text(encoding="utf-8")
    activation_source = activation_path.read_text(encoding="utf-8")
    activate_body = source[source.find("def activate_window") : source.find("def configure_dpi_awareness")]
    activation_body = activation_source[
        activation_source.find("def activate_window_with_dependencies") :
    ]
    assert_true(
        "WECHAT_WIN32_OCR_ACTIVATE_DEBOUNCE_SECONDS" in source,
        "activate_window should debounce repeated foreground activations",
    )
    assert_true(
        'and not aggressive_focus' not in activate_body + activation_body,
        "aggressive focus must still skip when WeChat is already foreground",
    )
    assert_true(
        "WECHAT_WIN32_OCR_FOCUS_CLICK_FALLBACK" in source,
        "activate_window should expose focus click fallback env switch for strict-focus lock scenarios",
    )
    assert_true(
        "focus_click_fallback_enabled()" in activation_body,
        "activate_window should optionally use click fallback when foreground lock blocks SetForegroundWindow",
    )


def test_non_retryable_input_failure_detects_focus_loss() -> None:
    result = {
        "ok": False,
        "reason": "window_lost_during_sendinput",
        "window_guard": {"ok": False, "reason": "foreground_not_wechat_target"},
    }
    assert_true(non_retryable_input_failure(result), f"focus loss must not be retried: {result}")


def test_foreground_guard_zero_hwnd_can_degrade_when_enabled() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    previous = os.environ.get("WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND")
    win32gui_mod = sidecar_mod.win32gui
    if win32gui_mod is None:
        return
    originals = {
        "GetForegroundWindow": getattr(win32gui_mod, "GetForegroundWindow"),
        "GetAncestor": getattr(win32gui_mod, "GetAncestor"),
    }
    try:
        os.environ["WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND"] = "1"
        setattr(win32gui_mod, "GetForegroundWindow", lambda: 0)
        setattr(win32gui_mod, "GetAncestor", lambda _hwnd, _flag: 0)
        result = sidecar_mod.foreground_window_matches_target(1001)
        assert_true(result.get("ok") is True, f"foreground=0 should degrade to guarded-pass when enabled: {result}")
        assert_true(str(result.get("reason") or "") == "foreground_unknown_guard_degraded", f"unexpected reason: {result}")
    finally:
        setattr(win32gui_mod, "GetForegroundWindow", originals["GetForegroundWindow"])
        setattr(win32gui_mod, "GetAncestor", originals["GetAncestor"])
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND", None)
        else:
            os.environ["WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND"] = previous


def test_foreground_guard_zero_hwnd_blocks_when_disabled() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    previous = os.environ.get("WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND")
    win32gui_mod = sidecar_mod.win32gui
    if win32gui_mod is None:
        return
    originals = {
        "GetForegroundWindow": getattr(win32gui_mod, "GetForegroundWindow"),
        "GetAncestor": getattr(win32gui_mod, "GetAncestor"),
    }
    try:
        os.environ["WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND"] = "0"
        setattr(win32gui_mod, "GetForegroundWindow", lambda: 0)
        setattr(win32gui_mod, "GetAncestor", lambda _hwnd, _flag: 0)
        result = sidecar_mod.foreground_window_matches_target(1001)
        assert_true(result.get("ok") is False, f"foreground=0 should be blocked when disabled: {result}")
        assert_true(str(result.get("reason") or "") == "foreground_not_wechat_target", f"unexpected reason: {result}")
    finally:
        setattr(win32gui_mod, "GetForegroundWindow", originals["GetForegroundWindow"])
        setattr(win32gui_mod, "GetAncestor", originals["GetAncestor"])
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND", None)
        else:
            os.environ["WECHAT_WIN32_OCR_ALLOW_UNKNOWN_FOREGROUND"] = previous


def test_recover_send_window_guard_recovers_foreground_mismatch() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "basic_send_window_guard": sidecar_mod.basic_send_window_guard,
        "activate_window": sidecar_mod.activate_window,
        "sleep": sidecar_mod.time.sleep,
        "uniform": sidecar_mod.random.uniform,
    }
    calls = {"guard": 0, "activate": 0}
    try:
        def fake_guard(_hwnd: int) -> dict[str, object]:
            calls["guard"] += 1
            if calls["guard"] == 1:
                return {"ok": False, "reason": "foreground_not_wechat_target"}
            return {"ok": True, "reason": "window_valid"}

        def fake_activate(_hwnd: int) -> None:
            calls["activate"] += 1

        sidecar_mod.basic_send_window_guard = fake_guard
        sidecar_mod.activate_window = fake_activate
        sidecar_mod.time.sleep = lambda _seconds: None
        sidecar_mod.random.uniform = lambda _a, _b: 0.0
        result = sidecar_mod.recover_send_window_guard(1001, max_attempts=1)
        assert_true(result.get("ok") is True, f"expected focus recovery success: {result}")
        assert_true(result.get("focus_recovered") is True, f"expected focus_recovered flag: {result}")
        assert_true(int(result.get("focus_recovery_attempts") or 0) == 1, f"expected single recovery attempt: {result}")
        assert_true(calls["activate"] == 1, f"expected one activate call: {calls}")
    finally:
        sidecar_mod.basic_send_window_guard = originals["basic_send_window_guard"]
        sidecar_mod.activate_window = originals["activate_window"]
        sidecar_mod.time.sleep = originals["sleep"]
        sidecar_mod.random.uniform = originals["uniform"]


def test_recover_send_window_guard_does_not_retry_non_focus_failures() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "basic_send_window_guard": sidecar_mod.basic_send_window_guard,
        "activate_window": sidecar_mod.activate_window,
    }
    calls = {"activate": 0}
    try:
        sidecar_mod.basic_send_window_guard = lambda _hwnd: {"ok": False, "reason": "window_not_visible"}
        sidecar_mod.activate_window = lambda _hwnd: calls.__setitem__("activate", calls["activate"] + 1)
        result = sidecar_mod.recover_send_window_guard(1001, max_attempts=2)
        assert_true(result.get("ok") is False, f"expected guard to fail: {result}")
        assert_true(str(result.get("reason") or "") == "window_not_visible", f"unexpected guard reason: {result}")
        assert_true(calls["activate"] == 0, f"non-focus failures should not trigger recovery activate: {calls}")
    finally:
        sidecar_mod.basic_send_window_guard = originals["basic_send_window_guard"]
        sidecar_mod.activate_window = originals["activate_window"]


def test_blank_input_focus_retry_keeps_single_confirm_semantics() -> None:
    previous_attempts = os.environ.get("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS")
    previous_retry = os.environ.get("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY")
    try:
        os.environ["WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"] = "1"
        os.environ.pop("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY", None)
        assert_true(send_input_confirm_attempt_count(3) == 2, "blank-only focus retry should add one safe focus attempt")
        os.environ["WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY"] = "0"
        assert_true(send_input_confirm_attempt_count(3) == 1, "blank focus retry must be configurable off")
        os.environ["WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"] = "3"
        os.environ.pop("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY", None)
        assert_true(send_input_confirm_attempt_count(3) == 3, "explicit retry budget should be preserved")
    finally:
        if previous_attempts is None:
            os.environ.pop("WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS", None)
        else:
            os.environ["WECHAT_WIN32_OCR_SEND_INPUT_CONFIRM_ATTEMPTS"] = previous_attempts
        if previous_retry is None:
            os.environ.pop("WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY", None)
        else:
            os.environ["WECHAT_WIN32_OCR_BLANK_INPUT_FOCUS_RETRY"] = previous_retry


def test_target_ready_defaults_to_single_attempt_and_hard_stops_blank_render() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    previous_attempts = os.environ.get("WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS")
    originals = {
        "open_chat": sidecar_mod.open_chat,
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
        "key_press": sidecar_mod.key_press,
        "sleep": sidecar_mod.time.sleep,
    }
    calls = {"open": 0, "validate": 0, "key": 0}
    try:
        os.environ.pop("WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS", None)

        def fake_open_chat(
            hwnd: int,
            target: str,
            *,
            exact: bool,
            artifact_dir: str | None = None,
            session_key: str = "",
        ) -> bool:
            calls["open"] += 1
            return False

        def fake_validate(hwnd: int, target: str, *, exact: bool, artifact_dir: str | None = None) -> dict[str, object]:
            calls["validate"] += 1
            return {"ok": False, "online": False, "state": "blank_render_detected", "reason": "blank_render"}

        sidecar_mod.open_chat = fake_open_chat
        sidecar_mod.validate_active_send_target = fake_validate
        sidecar_mod.key_press = lambda key: calls.__setitem__("key", calls["key"] + 1)
        sidecar_mod.time.sleep = lambda seconds: None
        result = sidecar_mod.ensure_target_ready_for_send(1001, "新数据测试", exact=True)
        assert_true(result.get("ok") is False, f"blank render must fail target readiness: {result}")
        assert_true(result.get("hard_stop") is True, f"blank render should hard-stop retries: {result}")
        assert_true(result.get("attempts") == 1, f"default target ready attempts should be one: {result}")
        assert_true(calls["open"] == 0 and calls["validate"] == 1, f"unexpected retry count: {calls}")
        assert_true(calls["key"] == 0, f"hard stop should not press ESC for another retry: {calls}")
    finally:
        if previous_attempts is None:
            os.environ.pop("WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS", None)
        else:
            os.environ["WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS"] = previous_attempts
        for name, value in originals.items():
            if name == "sleep":
                sidecar_mod.time.sleep = value
            else:
                setattr(sidecar_mod, name, value)


def test_target_ready_requires_guard_confirmation_even_when_open_chat_returns_true() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    previous_attempts = os.environ.get("WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS")
    originals = {
        "open_chat": sidecar_mod.open_chat,
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
        "key_press": sidecar_mod.key_press,
        "sleep": sidecar_mod.time.sleep,
    }
    calls = {"open": 0, "validate": 0, "key": 0}
    try:
        os.environ["WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS"] = "1"

        def fake_open_chat(
            hwnd: int,
            target: str,
            *,
            exact: bool,
            artifact_dir: str | None = None,
            session_key: str = "",
        ) -> bool:
            calls["open"] += 1
            return True

        def fake_validate(hwnd: int, target: str, *, exact: bool, artifact_dir: str | None = None) -> dict[str, object]:
            calls["validate"] += 1
            return {"ok": False, "online": True, "state": "target_mismatch", "reason": "target_not_confirmed"}

        sidecar_mod.open_chat = fake_open_chat
        sidecar_mod.validate_active_send_target = fake_validate
        sidecar_mod.key_press = lambda key: calls.__setitem__("key", calls["key"] + 1)
        sidecar_mod.time.sleep = lambda seconds: None
        result = sidecar_mod.ensure_target_ready_for_send(1001, "新数据测试", exact=True)
        assert_true(result.get("ok") is False, f"target readiness must fail when guard confirmation fails: {result}")
        assert_true(result.get("attempts") == 1, f"single-attempt mode should stop immediately: {result}")
        assert_true(calls["open"] == 1 and calls["validate"] == 2, f"unexpected call path: {calls}")
        assert_true(calls["key"] == 0, f"single-attempt mode should not trigger retry ESC: {calls}")
    finally:
        if previous_attempts is None:
            os.environ.pop("WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS", None)
        else:
            os.environ["WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS"] = previous_attempts
        for name, value in originals.items():
            if name == "sleep":
                sidecar_mod.time.sleep = value
            else:
                setattr(sidecar_mod, name, value)


def test_target_ready_short_circuits_when_active_target_already_confirmed() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "open_chat": sidecar_mod.open_chat,
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
    }
    calls = {"open": 0, "validate": 0}
    try:
        sidecar_mod.open_chat = lambda *_args, **_kwargs: calls.__setitem__("open", calls["open"] + 1) or False

        def fake_validate(hwnd: int, target: str, *, exact: bool, artifact_dir: str | None = None) -> dict[str, object]:
            calls["validate"] += 1
            return {
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "confirmation_confidence": "active_title_strict",
            }

        sidecar_mod.validate_active_send_target = fake_validate
        result = sidecar_mod.ensure_target_ready_for_send(1001, "新数据测试", exact=True)
        assert_true(result.get("ok") is True, f"pre-validated active target should pass immediately: {result}")
        assert_true(calls["open"] == 0 and calls["validate"] == 1, f"open_chat should be skipped on pre-validation pass: {calls}")
        timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
        assert_true("target_ready_pre_validation_duration_seconds" in timing, f"pre-validation timing should be present: {timing}")
        assert_true("target_ready_internal_duration_seconds" in timing, f"target_ready total timing should be present: {timing}")
        assert_true("target_ready_open_chat_duration_seconds" not in timing, f"fast path should not open chat: {timing}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_target_ready_short_circuits_with_session_key_when_active_target_confirmed() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "open_chat": sidecar_mod.open_chat,
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
    }
    calls = {"open": 0, "validate": 0}
    previous_state = dict(sidecar_mod._LAST_RPA_ACTION_STATE)
    try:
        sidecar_mod._LAST_RPA_ACTION_STATE.clear()
        sidecar_mod._LAST_RPA_ACTION_STATE["active_session_key"] = "wx:rpa:v1:test-session"
        sidecar_mod.open_chat = lambda *_args, **_kwargs: calls.__setitem__("open", calls["open"] + 1) or False

        def fake_validate(hwnd: int, target: str, *, exact: bool, artifact_dir: str | None = None) -> dict[str, object]:
            calls["validate"] += 1
            return {
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "requested_target": target,
                "confirmed_target": target,
                "confirmation_confidence": "active_title_strict",
            }

        sidecar_mod.validate_active_send_target = fake_validate
        result = sidecar_mod.ensure_target_ready_for_send(1001, "新数据测试", exact=True, session_key="wx:rpa:v1:test-session")
        assert_true(result.get("ok") is True, f"pre-validated keyed active target should pass immediately: {result}")
        assert_true(calls["open"] == 0 and calls["validate"] == 1, f"session-key fast path should not click the sidebar again: {calls}")
        assert_true(
            sidecar_mod._LAST_RPA_ACTION_STATE.get("active_session_key") == "wx:rpa:v1:test-session",
            "session-key fast path should refresh active session cache",
        )
    finally:
        sidecar_mod._LAST_RPA_ACTION_STATE.clear()
        sidecar_mod._LAST_RPA_ACTION_STATE.update(previous_state)
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_target_ready_with_session_key_confirms_before_send_when_cache_empty() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "open_chat": sidecar_mod.open_chat,
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
    }
    calls = {"open": 0, "validate": 0}
    previous_state = dict(sidecar_mod._LAST_RPA_ACTION_STATE)
    try:
        sidecar_mod._LAST_RPA_ACTION_STATE.clear()

        def fake_open_chat(
            hwnd: int,
            target: str,
            *,
            exact: bool,
            artifact_dir: str | None = None,
            session_key: str = "",
        ) -> bool:
            calls["open"] += 1
            assert_true(session_key == "wx:rpa:v1:test-session", "session_key must flow into open_chat confirmation")
            return True

        def fake_validate(hwnd: int, target: str, *, exact: bool, artifact_dir: str | None = None) -> dict[str, object]:
            calls["validate"] += 1
            return {
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "requested_target": target,
                "confirmed_target": target,
                "confirmation_confidence": "active_title_strict",
            }

        sidecar_mod.open_chat = fake_open_chat
        sidecar_mod.validate_active_send_target = fake_validate
        sidecar_mod.humanized_action_sleep = lambda *_args, **_kwargs: None
        result = sidecar_mod.ensure_target_ready_for_send(1001, "新数据测试", exact=True, session_key="wx:rpa:v1:test-session")
        assert_true(result.get("ok") is True, f"cache-miss keyed target should confirm through open_chat: {result}")
        assert_true(calls["open"] == 1 and calls["validate"] == 2, f"cache-miss path should do one confirmation, not repeated clicks: {calls}")
        timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
        assert_true(timing.get("target_ready_session_cache_match") is False, f"cache miss should be explicit: {timing}")
        assert_true("target_ready_session_open_chat_duration_seconds" in timing, f"session open timing should be present: {timing}")
        assert_true("target_ready_session_post_validation_duration_seconds" in timing, f"session post-validation timing should be present: {timing}")
    finally:
        sidecar_mod._LAST_RPA_ACTION_STATE.clear()
        sidecar_mod._LAST_RPA_ACTION_STATE.update(previous_state)
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_target_ready_reopens_when_prevalidation_is_weak() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    previous_attempts = os.environ.get("WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS")
    originals = {
        "open_chat": sidecar_mod.open_chat,
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
        "key_press": sidecar_mod.key_press,
        "sleep": sidecar_mod.time.sleep,
    }
    calls = {"open": 0, "validate": 0, "key": 0}
    try:
        os.environ["WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS"] = "1"

        def fake_open_chat(
            hwnd: int,
            target: str,
            *,
            exact: bool,
            artifact_dir: str | None = None,
            session_key: str = "",
        ) -> bool:
            calls["open"] += 1
            return True

        def fake_validate(hwnd: int, target: str, *, exact: bool, artifact_dir: str | None = None) -> dict[str, object]:
            calls["validate"] += 1
            if calls["validate"] == 1:
                return {
                    "ok": True,
                    "online": True,
                    "reason": "target_confirmed",
                    "confirmation_confidence": "active_title",
                }
            return {
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "confirmation_confidence": "active_title_strict",
            }

        sidecar_mod.open_chat = fake_open_chat
        sidecar_mod.validate_active_send_target = fake_validate
        sidecar_mod.key_press = lambda key: calls.__setitem__("key", calls["key"] + 1)
        sidecar_mod.time.sleep = lambda seconds: None
        result = sidecar_mod.ensure_target_ready_for_send(1001, "新数据测试", exact=True)
        assert_true(result.get("ok") is True, f"strong confirmation after open_chat should pass: {result}")
        assert_true(result.get("opened") is True, f"weak fast path should force open_chat before send: {result}")
        assert_true(calls["open"] == 1 and calls["validate"] == 2, f"unexpected weak-guard path: {calls}")
        assert_true(calls["key"] == 0, f"single attempt should not trigger retry ESC: {calls}")
    finally:
        if previous_attempts is None:
            os.environ.pop("WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS", None)
        else:
            os.environ["WECHAT_WIN32_OCR_TARGET_READY_MAX_ATTEMPTS"] = previous_attempts
        for name, value in originals.items():
            if name == "sleep":
                sidecar_mod.time.sleep = value
            else:
                setattr(sidecar_mod, name, value)


def test_send_payload_rechecks_prevalidated_guard_before_typing() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    originals = {
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
        "recover_send_window_guard": sidecar_mod.recover_send_window_guard,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "validate_send_geometry": sidecar_mod.validate_send_geometry,
        "calculate_send_points": sidecar_mod.calculate_send_points,
        "reserve_send_rate": sidecar_mod.reserve_send_rate,
        "send_with_guarded_clicks": sidecar_mod.send_with_guarded_clicks,
        "validate_post_send_target": sidecar_mod.validate_post_send_target,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
    }
    calls = {"validate_active": 0}
    try:
        def pass_validate(*_args, **_kwargs):
            calls["validate_active"] += 1
            return {
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "confirmation_confidence": "active_title_strict",
                "geometry": dict(geometry),
                "screenshot_path": "send_guard.png",
            }

        sidecar_mod.validate_active_send_target = pass_validate
        sidecar_mod.recover_send_window_guard = lambda _hwnd, max_attempts=1: {"ok": True, "reason": "window_valid"}
        sidecar_mod.get_window_geometry = lambda _hwnd: dict(geometry)
        sidecar_mod.validate_send_geometry = lambda _g: {"ok": True}
        sidecar_mod.calculate_send_points = lambda _g: {
            "ok": True,
            "input_point": [640, 715],
            "send_point": [915, 816],
        }
        sidecar_mod.reserve_send_rate = lambda **_kwargs: {"ok": True, "reason": "rate_ok"}
        sidecar_mod.send_with_guarded_clicks = lambda *_args, **_kwargs: {
            "ok": True,
            "method": "win32.human_click_input+sendinput_unicode+send_trigger:enter_only",
            "paste": {"ok": True, "input_mode": "sendinput_unicode"},
        }
        sidecar_mod.validate_post_send_target = lambda *_args, **_kwargs: {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "geometry": dict(geometry),
            "post_send_fast_guard": True,
            "screenshot_path": "",
        }
        sidecar_mod.humanized_action_sleep = lambda *_args, **_kwargs: 0.0
        payload = sidecar_mod.send_payload(
            1001,
            {"windows": [], "visible_main_windows": []},
            target="新数据测试",
            text="您好",
            exact=True,
            validated_guard={
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "geometry": dict(geometry),
            },
        )
        assert_true(payload.get("ok") is True, f"send payload should succeed with prevalidated guard: {payload}")
        send_result = payload.get("send_result") if isinstance(payload.get("send_result"), dict) else {}
        assert_true(
            str(send_result.get("validation_source") or "") == "prevalidated_guard_strict_recheck",
            f"send payload should mark validation_source as strict recheck: {send_result}",
        )
        assert_true(calls["validate_active"] == 1, f"active target validation must be re-run: {calls}")
        pre_guard = send_result.get("pre_send_guard") if isinstance(send_result.get("pre_send_guard"), dict) else {}
        assert_true(pre_guard.get("strict_recheck") is True, f"pre-send guard should record strict recheck: {pre_guard}")
        assert_true(isinstance(pre_guard.get("cached_prevalidated_guard"), dict), f"cached guard should remain auditable: {pre_guard}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_send_payload_exposes_optional_timing_without_contract_changes() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    originals = {
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
        "recover_send_window_guard": sidecar_mod.recover_send_window_guard,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "validate_send_geometry": sidecar_mod.validate_send_geometry,
        "calculate_send_points": sidecar_mod.calculate_send_points,
        "reserve_send_rate": sidecar_mod.reserve_send_rate,
        "send_with_guarded_clicks": sidecar_mod.send_with_guarded_clicks,
        "validate_post_send_target": sidecar_mod.validate_post_send_target,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
    }
    previous_send_mode = os.environ.get("WECHAT_WIN32_OCR_SEND_MODE")
    try:
        os.environ["WECHAT_WIN32_OCR_SEND_MODE"] = "click_only"
        sidecar_mod.validate_active_send_target = lambda *_args, **_kwargs: {
            "ok": True,
            "online": True,
            "reason": "target_confirmed",
            "confirmation_confidence": "active_title_strict",
            "geometry": dict(geometry),
            "screenshot_path": "send_guard.png",
        }
        sidecar_mod.recover_send_window_guard = lambda *_args, **_kwargs: {"ok": True, "reason": "window_valid"}
        sidecar_mod.get_window_geometry = lambda _hwnd: dict(geometry)
        sidecar_mod.validate_send_geometry = lambda _geometry: {"ok": True}
        sidecar_mod.calculate_send_points = lambda _geometry: {
            "ok": True,
            "input_point": [640, 715],
            "send_point": [915, 816],
        }
        sidecar_mod.reserve_send_rate = lambda **_kwargs: {"ok": True, "reason": "rate_ok"}
        sidecar_mod.send_with_guarded_clicks = lambda *_args, **_kwargs: {
            "ok": True,
            "method": "win32.human_click_input+sendinput_unicode+send_trigger:enter_only",
            "timing": {
                "input_focus_started_at": "2026-06-19T00:00:00",
                "input_focus_finished_at": "2026-06-19T00:00:00",
                "input_focus_duration_seconds": 0.01,
                "typing_started_at": "2026-06-19T00:00:00",
                "typing_finished_at": "2026-06-19T00:00:00",
                "typing_duration_seconds": 0.02,
                "send_trigger_started_at": "2026-06-19T00:00:00",
                "send_trigger_finished_at": "2026-06-19T00:00:00",
                "send_trigger_duration_seconds": 0.03,
            },
        }
        sidecar_mod.validate_post_send_target = lambda *_args, **_kwargs: {
            "ok": True,
            "online": True,
            "reason": "send_window_readable_after_send",
            "geometry": dict(geometry),
            "post_send_fast_guard": True,
        }
        sidecar_mod.humanized_action_sleep = lambda *_args, **_kwargs: 0.0

        payload = sidecar_mod.send_payload(
            1001,
            {"windows": [], "visible_main_windows": []},
            target="新数据测试",
            text="您好",
            exact=True,
        )
        assert_true(payload.get("ok") is True, f"send payload should still succeed: {payload}")
        timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
        send_result = payload.get("send_result") if isinstance(payload.get("send_result"), dict) else {}
        nested_timing = send_result.get("timing") if isinstance(send_result.get("timing"), dict) else {}
        for field in [
            "pre_send_guard_duration_seconds",
            "rate_guard_duration_seconds",
            "guarded_click_send_duration_seconds",
            "input_focus_duration_seconds",
            "typing_duration_seconds",
            "send_trigger_duration_seconds",
            "post_send_guard_duration_seconds",
            "send_payload_duration_seconds",
        ]:
            assert_true(field in timing, f"top-level timing should expose {field}: {timing}")
            assert_true(field in nested_timing, f"send_result timing should expose {field}: {nested_timing}")
        assert_true(str(payload.get("state") or "") == "send_win32_rpa", f"state contract changed unexpectedly: {payload}")
        assert_true(isinstance(send_result.get("pre_send_guard"), dict), f"pre_send_guard contract missing: {send_result}")
        assert_true(isinstance(send_result.get("post_send_guard"), dict), f"post_send_guard contract missing: {send_result}")
    finally:
        if previous_send_mode is None:
            os.environ.pop("WECHAT_WIN32_OCR_SEND_MODE", None)
        else:
            os.environ["WECHAT_WIN32_OCR_SEND_MODE"] = previous_send_mode
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_send_payload_blocks_stale_prevalidated_guard_when_active_target_changed() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    originals = {
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
        "recover_send_window_guard": sidecar_mod.recover_send_window_guard,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "validate_send_geometry": sidecar_mod.validate_send_geometry,
        "calculate_send_points": sidecar_mod.calculate_send_points,
        "reserve_send_rate": sidecar_mod.reserve_send_rate,
        "send_with_guarded_clicks": sidecar_mod.send_with_guarded_clicks,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
    }
    calls = {"validate_active": 0, "send": 0}
    try:
        def stale_validate(*_args, **_kwargs):
            calls["validate_active"] += 1
            return {
                "ok": False,
                "online": True,
                "reason": "target_title_not_confirmed",
                "geometry": dict(geometry),
                "error": "active chat drifted",
            }

        sidecar_mod.validate_active_send_target = stale_validate
        sidecar_mod.recover_send_window_guard = lambda _hwnd, max_attempts=1: {"ok": True, "reason": "window_valid"}
        sidecar_mod.get_window_geometry = lambda _hwnd: dict(geometry)
        sidecar_mod.validate_send_geometry = lambda _g: {"ok": True}
        sidecar_mod.calculate_send_points = lambda _g: {"ok": True, "input_point": [640, 715], "send_point": [915, 816]}
        sidecar_mod.reserve_send_rate = lambda **_kwargs: {"ok": True, "reason": "rate_ok"}
        sidecar_mod.send_with_guarded_clicks = lambda *_args, **_kwargs: calls.__setitem__("send", calls["send"] + 1) or {"ok": True}
        sidecar_mod.humanized_action_sleep = lambda *_args, **_kwargs: 0.0
        payload = sidecar_mod.send_payload(
            1001,
            {"windows": [], "visible_main_windows": []},
            target="新数据测试",
            text="您好",
            exact=True,
            validated_guard={
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "geometry": dict(geometry),
            },
        )
        assert_true(payload.get("ok") is False, f"stale prevalidated guard should block send: {payload}")
        assert_true(str(payload.get("state") or "") == "send_guard_blocked", f"unexpected state: {payload}")
        assert_true(calls["validate_active"] == 1 and calls["send"] == 0, f"must revalidate before typing: {calls}")
        guard = payload.get("guard") if isinstance(payload.get("guard"), dict) else {}
        assert_true(guard.get("strict_recheck") is True, f"block reason should record strict recheck: {guard}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_activate_session_candidate_single_click_on_unconfirmed_target() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    session = {"name": "新数据测试", "center_y": 188, "left": 95, "right": 190, "top": 166, "bottom": 210}
    originals = {
        "choose_session_row_click_point": sidecar_mod.choose_session_row_click_point,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
        "human_client_click": sidecar_mod.human_client_click,
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
    }
    calls = {"click": 0, "validate": 0}
    try:
        sidecar_mod.choose_session_row_click_point = lambda *args, **kwargs: (180, 188, {"candidate_count": 10})
        sidecar_mod.humanized_action_sleep = lambda *_args, **_kwargs: 0.0
        sidecar_mod.human_client_click = lambda *_args, **_kwargs: calls.__setitem__("click", calls["click"] + 1)

        def fake_validate(*_args, **_kwargs):
            calls["validate"] += 1
            return {
                "ok": False,
                "online": True,
                "reason": "target_title_not_confirmed",
                "confirmation_confidence": "none",
        }

        sidecar_mod.validate_active_send_target = fake_validate
        previous = os.environ.get("WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS")
        os.environ["WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS"] = "2"
        opened = sidecar_mod.activate_session_candidate(
            1001,
            session,
            target="新数据测试",
            exact=True,
            geometry=geometry,
            default_click_x=180,
        )
        assert_true(opened is False, f"unconfirmed click should not open target: {opened}")
        assert_true(calls["click"] == 1, f"target activation must not retry physical clicks: {calls}")
        assert_true(calls["validate"] == 2, f"target activation should only passively re-read after the click: {calls}")
    finally:
        if "previous" in locals():
            if previous is None:
                os.environ.pop("WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS", None)
            else:
                os.environ["WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS"] = previous
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_activate_session_candidate_passive_confirm_without_second_click() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    session = {"name": "新数据测试", "center_y": 188, "left": 95, "right": 190, "top": 166, "bottom": 210}
    originals = {
        "choose_session_row_click_point": sidecar_mod.choose_session_row_click_point,
        "humanized_action_sleep": sidecar_mod.humanized_action_sleep,
        "human_client_click": sidecar_mod.human_client_click,
        "validate_active_send_target": sidecar_mod.validate_active_send_target,
    }
    previous = os.environ.get("WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS")
    calls = {"click": 0, "validate": 0}
    try:
        os.environ["WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS"] = "2"
        sidecar_mod.choose_session_row_click_point = lambda *args, **kwargs: (180, 188, {"candidate_count": 10})
        sidecar_mod.humanized_action_sleep = lambda *_args, **_kwargs: 0.0
        sidecar_mod.human_client_click = lambda *_args, **_kwargs: calls.__setitem__("click", calls["click"] + 1)

        def fake_validate(*_args, **_kwargs):
            calls["validate"] += 1
            if calls["validate"] < 2:
                return {
                    "ok": False,
                    "online": True,
                    "reason": "target_title_not_confirmed",
                    "confirmation_confidence": "none",
                }
            return {
                "ok": True,
                "online": True,
                "reason": "target_confirmed",
                "confirmation_confidence": "active_title_strict",
            }

        sidecar_mod.validate_active_send_target = fake_validate
        opened = sidecar_mod.activate_session_candidate(
            1001,
            session,
            target="新数据测试",
            exact=True,
            geometry=geometry,
            default_click_x=180,
        )
        assert_true(opened is True, f"passive confirmation should allow a settled target: {opened}")
        assert_true(calls["click"] == 1, f"passive confirmation must not add a second physical click: {calls}")
        assert_true(calls["validate"] == 2, f"should re-read without extra click until target settles: {calls}")
    finally:
        if previous is None:
            os.environ.pop("WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS", None)
        else:
            os.environ["WECHAT_WIN32_OCR_TARGET_SWITCH_PASSIVE_CONFIRM_ATTEMPTS"] = previous
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_open_chat_does_not_search_after_visible_candidate_unconfirmed() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    screenshot = Image.new("RGB", (980, 860), "white")
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    originals = {
        "ensure_main_session_list": sidecar_mod.ensure_main_session_list,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "target_switch_surface_state": sidecar_mod.target_switch_surface_state,
        "active_chat_matches": sidecar_mod.active_chat_matches,
        "parse_sessions_from_ocr": sidecar_mod.parse_sessions_from_ocr,
        "activate_session_candidate": sidecar_mod.activate_session_candidate,
        "clear_sidebar_search_box_without_select_all": sidecar_mod.clear_sidebar_search_box_without_select_all,
    }
    calls = {"activate": 0, "clear": 0}
    try:
        sidecar_mod.ensure_main_session_list = lambda hwnd, artifact_dir=None: (
            screenshot,
            [{"text": "新数据测试", "left": 95, "top": 172, "right": 190, "bottom": 204, "center_y": 188}],
        )
        sidecar_mod.get_window_geometry = lambda hwnd: geometry
        sidecar_mod.target_switch_surface_state = lambda *args, **kwargs: {"ok": True, "reason": "surface_ready"}
        sidecar_mod.active_chat_matches = lambda *args, **kwargs: False
        sidecar_mod.parse_sessions_from_ocr = lambda *args, **kwargs: [
            {"name": "新数据测试", "center_y": 188, "left": 95, "right": 190}
        ]
        sidecar_mod.activate_session_candidate = (
            lambda *args, **kwargs: calls.__setitem__("activate", calls["activate"] + 1) or False
        )
        sidecar_mod.clear_sidebar_search_box_without_select_all = (
            lambda *args, **kwargs: calls.__setitem__("clear", calls["clear"] + 1)
        )
        opened = sidecar_mod.open_chat(1001, "新数据测试", exact=True)
        assert_true(opened is False, f"unconfirmed visible candidate should stop this attempt: {opened}")
        assert_true(calls["activate"] == 1, f"visible candidate should be clicked once: {calls}")
        assert_true(calls["clear"] == 0, f"failed visible candidate must not fall through to search: {calls}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_open_chat_blocks_search_when_initial_ocr_unavailable() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    screenshot = Image.new("RGB", (980, 860), "white")
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    originals = {
        "ensure_main_session_list": sidecar_mod.ensure_main_session_list,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "target_switch_surface_state": sidecar_mod.target_switch_surface_state,
        "clear_sidebar_search_box_without_select_all": sidecar_mod.clear_sidebar_search_box_without_select_all,
    }
    calls = {"clear": 0}
    try:
        sidecar_mod.ensure_main_session_list = lambda hwnd, artifact_dir=None: (screenshot, [])
        sidecar_mod.get_window_geometry = lambda hwnd: geometry
        sidecar_mod.target_switch_surface_state = lambda *args, **kwargs: {
            "ok": True,
            "online": True,
            "reason": "surface_no_ocr_not_blank",
            "ocr_count": 0,
        }
        sidecar_mod.clear_sidebar_search_box_without_select_all = (
            lambda *args, **kwargs: calls.__setitem__("clear", calls["clear"] + 1)
        )
        opened = sidecar_mod.open_chat(1001, "新数据测试", exact=True)
        assert_true(opened is False, f"unreadable OCR should not open/search target: {opened}")
        assert_true(calls["clear"] == 0, f"OCR unavailable must block search clicks: {calls}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_open_chat_search_fallback_disabled_by_default() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    screenshot = Image.new("RGB", (980, 860), "white")
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    previous_search_fallback = os.environ.get("WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK")
    originals = {
        "ensure_main_session_list": sidecar_mod.ensure_main_session_list,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "target_switch_surface_state": sidecar_mod.target_switch_surface_state,
        "active_chat_matches": sidecar_mod.active_chat_matches,
        "parse_sessions_from_ocr": sidecar_mod.parse_sessions_from_ocr,
        "clear_sidebar_search_box_without_select_all": sidecar_mod.clear_sidebar_search_box_without_select_all,
        "type_sidebar_search_query": sidecar_mod.type_sidebar_search_query,
    }
    calls = {"clear": 0, "type": 0}
    try:
        os.environ.pop("WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK", None)
        sidecar_mod.ensure_main_session_list = lambda hwnd, artifact_dir=None: (
            screenshot,
            [{"text": "文件传输助手", "left": 86, "top": 120, "right": 210, "bottom": 150, "center_y": 135}],
        )
        sidecar_mod.get_window_geometry = lambda hwnd: geometry
        sidecar_mod.target_switch_surface_state = lambda *args, **kwargs: {"ok": True, "reason": "surface_ready"}
        sidecar_mod.active_chat_matches = lambda *args, **kwargs: False
        sidecar_mod.parse_sessions_from_ocr = lambda *args, **kwargs: []
        sidecar_mod.clear_sidebar_search_box_without_select_all = (
            lambda *args, **kwargs: calls.__setitem__("clear", calls["clear"] + 1)
        )
        sidecar_mod.type_sidebar_search_query = (
            lambda *args, **kwargs: calls.__setitem__("type", calls["type"] + 1) or {"ok": True}
        )
        opened = sidecar_mod.open_chat(1001, "新数据测试", exact=True)
        assert_true(opened is False, f"default search fallback should not open hidden target: {opened}")
        assert_true(calls == {"clear": 0, "type": 0}, f"default path must not touch search/header: {calls}")
    finally:
        if previous_search_fallback is None:
            os.environ.pop("WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK", None)
        else:
            os.environ["WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK"] = previous_search_fallback
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_open_chat_search_fallback_clicks_visible_result_without_enter_when_enabled() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    screenshot = Image.new("RGB", (980, 860), "white")
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    previous_search_fallback = os.environ.get("WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK")
    previous_enter_fallback = os.environ.get("WECHAT_WIN32_OCR_TARGET_SEARCH_ENTER_FALLBACK")
    originals = {
        "ensure_main_session_list": sidecar_mod.ensure_main_session_list,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "target_switch_surface_state": sidecar_mod.target_switch_surface_state,
        "active_chat_matches": sidecar_mod.active_chat_matches,
        "parse_sessions_from_ocr": sidecar_mod.parse_sessions_from_ocr,
        "clear_sidebar_search_box_without_select_all": sidecar_mod.clear_sidebar_search_box_without_select_all,
        "type_sidebar_search_query": sidecar_mod.type_sidebar_search_query,
        "capture_wechat": sidecar_mod.capture_wechat,
        "run_ocr": sidecar_mod.run_ocr,
        "activate_session_candidate": sidecar_mod.activate_session_candidate,
        "key_press": sidecar_mod.key_press,
        "sleep": sidecar_mod.time.sleep,
    }
    calls = {"clear": 0, "type": 0, "activate": 0, "key_return": 0}
    parse_call_count = {"count": 0}
    try:
        os.environ["WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK"] = "1"
        os.environ["WECHAT_WIN32_OCR_TARGET_SEARCH_ENTER_FALLBACK"] = "0"
        sidecar_mod.ensure_main_session_list = lambda hwnd, artifact_dir=None: (
            screenshot,
            [{"text": "文件传输助手", "left": 86, "top": 120, "right": 210, "bottom": 150, "center_y": 135}],
        )
        sidecar_mod.get_window_geometry = lambda hwnd: geometry
        sidecar_mod.target_switch_surface_state = lambda *args, **kwargs: {"ok": True, "reason": "surface_ready"}
        sidecar_mod.active_chat_matches = lambda *args, **kwargs: False

        def fake_parse_sessions(items, image_size, screenshot=None):
            parse_call_count["count"] += 1
            if parse_call_count["count"] == 1:
                return []
            return [{"name": "新数据测试", "center_y": 188, "left": 95, "right": 190}]

        sidecar_mod.parse_sessions_from_ocr = fake_parse_sessions
        sidecar_mod.clear_sidebar_search_box_without_select_all = (
            lambda hwnd, search_x, search_y, target_hint="": calls.__setitem__("clear", calls["clear"] + 1)
        )
        sidecar_mod.type_sidebar_search_query = (
            lambda hwnd, target: calls.__setitem__("type", calls["type"] + 1) or {"ok": True, "method": "sendinput_unicode"}
        )
        sidecar_mod.capture_wechat = lambda hwnd, artifact_dir=None, label="open_chat": (screenshot, f"{label}.png")
        sidecar_mod.run_ocr = lambda image: [
            {"text": "新数据测试", "left": 95, "top": 172, "right": 190, "bottom": 204, "center_y": 188}
        ]
        sidecar_mod.activate_session_candidate = (
            lambda *args, **kwargs: calls.__setitem__("activate", calls["activate"] + 1) or True
        )

        def fake_key_press(key: int) -> None:
            if key == sidecar_mod.win32con.VK_RETURN:
                calls["key_return"] += 1

        sidecar_mod.key_press = fake_key_press
        sidecar_mod.time.sleep = lambda seconds: None
        opened = sidecar_mod.open_chat(1001, "新数据测试", exact=True)
        assert_true(opened is True, f"visible search result should open target chat: {calls}")
        assert_true(calls["clear"] == 1 and calls["type"] == 1, f"search should run once: {calls}")
        assert_true(calls["activate"] == 1, f"visible result should be clicked once: {calls}")
        assert_true(calls["key_return"] == 0, f"default search fallback must not press Enter: {calls}")
    finally:
        if previous_search_fallback is None:
            os.environ.pop("WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK", None)
        else:
            os.environ["WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK"] = previous_search_fallback
        if previous_enter_fallback is None:
            os.environ.pop("WECHAT_WIN32_OCR_TARGET_SEARCH_ENTER_FALLBACK", None)
        else:
            os.environ["WECHAT_WIN32_OCR_TARGET_SEARCH_ENTER_FALLBACK"] = previous_enter_fallback
        for name, value in originals.items():
            if name == "sleep":
                sidecar_mod.time.sleep = value
            else:
                setattr(sidecar_mod, name, value)


def test_open_chat_blocks_search_when_surface_is_blank() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    screenshot = Image.new("RGB", (980, 860), "white")
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    originals = {
        "ensure_main_session_list": sidecar_mod.ensure_main_session_list,
        "get_window_geometry": sidecar_mod.get_window_geometry,
        "target_switch_surface_state": sidecar_mod.target_switch_surface_state,
        "clear_sidebar_search_box_without_select_all": sidecar_mod.clear_sidebar_search_box_without_select_all,
    }
    calls = {"clear": 0}
    try:
        sidecar_mod.ensure_main_session_list = lambda hwnd, artifact_dir=None: (screenshot, [])
        sidecar_mod.get_window_geometry = lambda hwnd: geometry
        sidecar_mod.target_switch_surface_state = lambda *args, **kwargs: {
            "ok": False,
            "online": False,
            "state": "blank_render_detected",
            "reason": "blank_render",
        }
        sidecar_mod.clear_sidebar_search_box_without_select_all = (
            lambda *args, **kwargs: calls.__setitem__("clear", calls["clear"] + 1)
        )
        opened = sidecar_mod.open_chat(1001, "新数据测试", exact=True)
        assert_true(opened is False, f"blank render should not open chat: {opened}")
        assert_true(calls["clear"] == 0, f"blank render must block search clicks: {calls}")
    finally:
        for name, value in originals.items():
            setattr(sidecar_mod, name, value)


def test_sidebar_search_clear_uses_window_image_click() -> None:
    sidecar_mod = sys.modules["apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar"]
    originals = {
        "human_window_image_click": sidecar_mod.human_window_image_click,
        "human_client_click": sidecar_mod.human_client_click,
        "key_press": sidecar_mod.key_press,
        "sleep": sidecar_mod.time.sleep,
    }
    calls = {"window_click": 0, "client_click": 0, "keys": 0}
    try:
        sidecar_mod.human_window_image_click = (
            lambda hwnd, x, y: calls.__setitem__("window_click", calls["window_click"] + 1)
        )
        sidecar_mod.human_client_click = (
            lambda hwnd, x, y: calls.__setitem__("client_click", calls["client_click"] + 1)
        )
        sidecar_mod.key_press = lambda key: calls.__setitem__("keys", calls["keys"] + 1)
        sidecar_mod.time.sleep = lambda seconds: None
        sidecar_mod.clear_sidebar_search_box_without_select_all(1001, 122, 64, target_hint="新数据测试")
        assert_true(calls["window_click"] == 1, f"search box should use screenshot/window coordinates: {calls}")
        assert_true(calls["client_click"] == 0, f"search box should avoid client-coordinate click drift: {calls}")
        assert_true(calls["keys"] >= 1, f"search clear should still clear stale query text: {calls}")
    finally:
        for name, value in originals.items():
            if name == "sleep":
                sidecar_mod.time.sleep = value
            else:
                setattr(sidecar_mod, name, value)


def test_rpa_action_layer_avoids_fixed_sleep_cadence() -> None:
    source_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
    activation_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr" / "window_activation.py"
    source = source_path.read_text(encoding="utf-8")
    activation_source = activation_path.read_text(encoding="utf-8")
    sidecar_action_functions = {
        "activate_session_candidate",
        "clear_sidebar_search_box_without_select_all",
        "client_click",
        "click",
        "confirm_input_token_via_clipboard",
        "ensure_left_button_released",
        "ensure_target_ready_for_send",
        "ensure_visible_wechat_window",
        "hotkey",
        "key_press",
        "normalize_wechat_window",
        "scroll_chat_history",
        "scroll_chat_to_latest",
        "send_with_uia_controls",
    }
    activation_action_functions = {"activate_window_with_dependencies"}
    fixed_sleeps: list[str] = []
    helper_hits: set[str] = set()
    sources = (
        ("sidecar", ast.parse(source), sidecar_action_functions),
        ("window_activation", ast.parse(activation_source), activation_action_functions),
    )
    for label, tree, action_functions in sources:
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in action_functions:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Name) and call.func.id == "humanized_action_sleep":
                    helper_hits.add(node.name)
                if isinstance(call.func, ast.Attribute) and call.func.attr == "humanized_action_sleep":
                    helper_hits.add(node.name)
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "sleep"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "time"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, (int, float))
                ):
                    fixed_sleeps.append(f"{label}:{node.name}:{call.lineno}:{call.args[0].value}")
    assert_true(not fixed_sleeps, f"RPA action functions should not use fixed sleep cadence: {fixed_sleeps}")
    action_functions = sidecar_action_functions | activation_action_functions
    missing_helpers = sorted(action_functions - helper_hits - {"ensure_visible_wechat_window"})
    assert_true(not missing_helpers, f"RPA action functions should use humanized_action_sleep: {missing_helpers}")


def test_scroll_actions_randomize_wheel_and_cursor_cadence() -> None:
    source_path = PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py"
    source = source_path.read_text(encoding="utf-8")
    history_start = source.find("def scroll_chat_history")
    latest_start = source.find("def scroll_chat_to_latest")
    capture_start = source.find("def capture_wechat")
    history_source = source[history_start:latest_start]
    latest_source = source[latest_start:capture_start]
    assert_true("random.randint(-12, 12)" in history_source, "history scroll should jitter cursor X")
    assert_true("random.randint(-10, 10)" in history_source, "history scroll should jitter cursor Y")
    assert_true("random.choice([-1, 0, 1])" in history_source, "history scroll should vary wheel units")
    assert_true('"cursor"' in history_source, "history scroll audit should record final cursor point")
    assert_true("actual_attempts" in latest_source, "scroll-to-latest should vary actual attempt count")
    assert_true("random.randint(5, 7)" in latest_source, "scroll-to-latest should vary wheel units")
    assert_true("humanized_action_sleep(85, 180)" in latest_source, "scroll-to-latest should avoid fixed cadence")


def main() -> int:
    tests = [
        test_sidecar_script_bootstraps_project_root_without_pythonpath,
        test_sidecar_help_exposes_stable_actions_and_flags,
        test_sidecar_contract_validation_failure_is_json_without_window_probe,
        test_sidecar_facade_exports_contract_surface,
        test_parse_sessions_from_ocr,
        test_parse_sessions_detects_visual_unread_red_dot,
        test_parse_sessions_normalizes_truncated_file_transfer,
        test_parse_sessions_strips_standalone_relative_day_suffix,
        test_parse_sessions_preserves_duplicate_display_names_with_session_keys,
        test_add_friend_query_normalization_prefers_phone_digits,
        test_add_friend_windows_plus_entry_uses_windows_sidebar_geometry,
        test_add_friend_search_result_detection_from_ocr,
        test_add_friend_surface_classification,
        test_add_friend_surface_readiness_blocks_blank_or_empty_ocr,
        test_connector_add_friend_builds_win32_ocr_request,
        test_add_friend_rpa_env_is_non_recovery_by_default,
        test_add_friend_entry_click_script_keeps_render_recovery_opt_in,
        test_add_friend_menu_click_handles_stale_dialog_hwnd,
        test_add_friend_uses_serialized_human_pacing,
        test_add_friend_query_input_uses_digit_key_presses_by_default,
        test_add_friend_query_blocks_non_numeric_sendinput_without_opt_in,
        test_add_friend_optional_field_fill_disabled_by_default,
        test_message_probe_tokens_prefer_semantic_body_after_live_marker,
        test_parse_messages_from_ocr,
        test_parse_messages_keeps_low_visible_bubble_lines,
        test_parse_messages_excludes_left_input_draft_residue,
        test_parse_messages_classifies_wide_right_bubbles_as_self,
        test_parse_messages_outputs_message_envelope_fields,
        test_history_snapshot_merge_orders_and_dedupes,
        test_anchor_match_supports_content_and_reply_keys,
        test_message_noise_filters_relative_timestamps,
        test_connector_helpers,
        test_send_geometry_guard,
        test_send_points_apply_small_safe_jitter,
        test_send_and_input_points_use_candidate_pools,
        test_input_click_jitter_has_enough_entropy,
        test_send_rate_guard,
        test_wechat_rpa_lock_recovers_stale_lock_and_times_out_on_live_lock,
        test_uia_control_selection_prefers_chatbox,
        test_startup_capability_decision,
        test_startup_self_check_uses_interactive_probe_env,
        test_connector_interactive_capabilities_passes_probe_env,
        test_connector_interactive_status_passes_probe_env,
        test_connector_fresh_session_discovery_passes_probe_env,
        test_connector_passive_session_poll_keeps_probe_env_empty,
        test_connector_interactive_capabilities_reports_tray_hidden_window_without_restore,
        test_connector_passive_status_does_not_recover_minimized_window,
        test_interactive_probe_does_not_restore_tray_hidden_window,
        test_connector_interactive_status_reports_blank_render_without_auto_recovery_by_default,
        test_connector_capabilities_skips_reserve_after_blank_render_stop,
        test_connector_interactive_status_recovers_blank_render_when_explicitly_enabled,
        test_sidecar_recover_render_reports_disabled_by_default,
        test_sidecar_recover_render_enters_visible_quick_login_when_explicitly_enabled,
        test_startup_evaluates_nested_rpa_geometry_failure,
        test_preflight_uses_interactive_status_probe,
        test_adaptive_window_points,
        test_session_match_and_click_x,
        test_session_click_candidate_points_spread_across_row,
        test_active_chat_matches_file_transfer_alias,
        test_active_chat_matches_wrapped_title_text,
        test_active_chat_matches_ignores_group_speaker_label_below_title_roi,
        test_active_chat_matches_rejects_other_chat_body_sender_as_title,
        test_active_chat_matches_unread_badge_and_roi_edge,
        test_active_chat_matches_real_981_title_left_edge,
        test_active_chat_cutoff_extends_above_sidebar_cutoff,
        test_file_transfer_simulated_inbound_fallback,
        test_fast_confirmation_still_enqueues_file_transfer_loopback,
        test_passive_probe_mode_toggle,
        test_low_disturbance_read_and_action_budget_defaults,
        test_rpa_action_pacing_covers_keyboard_mouse_and_window_image_clicks,
        test_passive_probe_window_discovery_is_non_interactive,
        test_interactive_probe_restores_offscreen_visible_window,
        test_capture_geometry_guard_and_window_selection,
        test_window_selection_prefers_real_wechat_title_over_weixin_shell,
        test_window_selection_prefers_large_actionable_window_over_quick_login_title,
        test_window_selection_prefers_readable_window_over_larger_blank_window,
        test_auxiliary_wechat_shell_is_blocked,
        test_normalize_wechat_window_clamps_offscreen_when_size_is_already_safe,
        test_capabilities_success_exposes_top_level_geometry,
        test_blank_render_detection_for_empty_white_capture,
        test_blank_render_detection_for_bordered_white_capture,
        test_send_guard_blocks_blank_render_before_file_transfer_blind_send,
        test_quick_login_detection,
        test_quick_login_auto_enter_uses_human_client_click,
        test_blocking_screen_ignores_normal_chat_login_words,
        test_blocking_screen_detects_wechat_storage_full_dialog,
        test_service_subview_back_target_detection,
        test_connector_rpa_priority_and_wxauto_reserve_toggle,
        test_send_verify_handles_split_multiline_messages,
        test_send_verify_tolerates_common_ocr_noise_on_long_self_reply,
        test_blind_send_without_ocr_verification_fallback,
        test_guarded_send_confirmation_fallback,
        test_fast_send_confirmation_skips_slow_message_read_when_guard_is_strong,
        test_invalid_window_handle_is_hard_stop_not_recovery,
        test_daemon_invalid_window_handle_is_hard_stop,
        test_sidecar_exception_payload_marks_invalid_window_handle,
        test_send_text_invalid_window_handle_skips_wxauto4_reserve,
        test_foreign_overlay_capture_filter_and_blind_target_gate,
        test_blind_target_confirmation_uses_sidebar_match_when_title_missing,
        test_humanized_chunk_text_and_settings,
        test_adaptive_humanized_input_speed_profiles,
        test_adaptive_humanized_input_clamps_live_wide_waits_by_profile,
        test_set_uia_control_value_humanized_progressive_updates,
        test_sendinput_unicode_text_entry_helpers,
        test_sendinput_unicode_aborts_when_window_guard_fails,
        test_send_trigger_mode_defaults_to_enter_only,
        test_safe_send_trigger_uses_single_enter_with_randomized_delays,
        test_send_with_guarded_clicks_skips_input_refocus_after_confirmed_paste,
        test_input_text_region_state_distinguishes_blank_and_text,
        test_input_region_visual_delta_confirmation,
        test_input_fast_visual_confirm_keeps_before_ocr_and_skips_after_ocr,
        test_input_region_soft_blank_noise_allows_post_clear_progress,
        test_input_area_token_confirmation_excludes_recent_chat_bubble,
        test_clear_existing_input_draft_noops_when_blank,
        test_send_rpa_env_enables_strict_focus_single_confirm_and_blank_retry,
        test_activate_window_debounces_aggressive_refocus,
        test_non_retryable_input_failure_detects_focus_loss,
        test_foreground_guard_zero_hwnd_can_degrade_when_enabled,
        test_foreground_guard_zero_hwnd_blocks_when_disabled,
        test_recover_send_window_guard_recovers_foreground_mismatch,
        test_recover_send_window_guard_does_not_retry_non_focus_failures,
        test_blank_input_focus_retry_keeps_single_confirm_semantics,
        test_target_ready_defaults_to_single_attempt_and_hard_stops_blank_render,
        test_target_ready_requires_guard_confirmation_even_when_open_chat_returns_true,
        test_target_ready_short_circuits_when_active_target_already_confirmed,
        test_target_ready_short_circuits_with_session_key_when_active_target_confirmed,
        test_target_ready_with_session_key_confirms_before_send_when_cache_empty,
        test_target_ready_reopens_when_prevalidation_is_weak,
        test_send_payload_rechecks_prevalidated_guard_before_typing,
        test_send_payload_exposes_optional_timing_without_contract_changes,
        test_send_payload_blocks_stale_prevalidated_guard_when_active_target_changed,
        test_activate_session_candidate_single_click_on_unconfirmed_target,
        test_activate_session_candidate_passive_confirm_without_second_click,
        test_open_chat_does_not_search_after_visible_candidate_unconfirmed,
        test_open_chat_blocks_search_when_initial_ocr_unavailable,
        test_open_chat_search_fallback_disabled_by_default,
        test_open_chat_search_fallback_clicks_visible_result_without_enter_when_enabled,
        test_open_chat_blocks_search_when_surface_is_blank,
        test_sidebar_search_clear_uses_window_image_click,
        test_rpa_action_layer_avoids_fixed_sleep_cadence,
        test_scroll_actions_randomize_wheel_and_cursor_cadence,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR compatibility checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
