"""Static safety checks for the Windows Sandbox WeChat live-test helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = PROJECT_ROOT / "runtime" / "sandbox" / "wechat_test" / "send_sandbox_customer_msg_once.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("sandbox_wechat_sender", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sandbox_wechat_sender"] = module
    spec.loader.exec_module(module)
    return module


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_target_whitelist() -> None:
    mod = load_module()
    assert_true(mod.canonical_target_name("Meta xc") == "Meta_xc", "Meta xc alias should normalize")
    assert_true(mod.canonical_target_name("Meta_xc") == "Meta_xc", "Meta_xc should stay canonical")
    assert_true(mod.canonical_target_name("文件传输助手") == "文件传输助手", "FTA should stay canonical")
    assert_true(mod.target_allowed("Meta xc") is True, "Meta alias should be allowed")
    assert_true(mod.target_allowed("文件传输助手") is True, "FTA should be allowed")
    assert_true(mod.target_allowed("新数据测试") is False, "non-whitelisted targets must be blocked")
    blocked = mod.preflight(target="新数据测试", artifact_dir=PROJECT_ROOT / "runtime" / "sandbox" / "unit", require_host_layout=False)
    assert_true(blocked.get("reason") == "target_not_whitelisted", f"preflight should block before Win32: {blocked}")


def test_layout_requires_non_overlap_and_host_left() -> None:
    mod = load_module()
    host = {"rect": [0, 0, 900, 860], "width": 900, "height": 860}
    sandbox = {"rect": [960, 0, 1920, 860], "width": 960, "height": 860}
    ok = mod.evaluate_layout(host, sandbox)
    assert_true(ok.get("ok") is True and ok.get("reason") == "layout_ok", f"layout should pass: {ok}")
    overlap = mod.evaluate_layout(host, {"rect": [860, 0, 1800, 860], "width": 940, "height": 860})
    assert_true(overlap.get("ok") is False and overlap.get("overlap") is True, f"overlap should fail: {overlap}")
    reversed_layout = mod.evaluate_layout({"rect": [960, 0, 1920, 860]}, {"rect": [0, 0, 900, 860]})
    assert_true(reversed_layout.get("ok") is False, f"host must stay left of sandbox: {reversed_layout}")


def test_active_target_confirmation_accepts_safe_aliases() -> None:
    mod = load_module()
    items = [
        {"text": "许聪", "left": 110, "right": 180, "center_y": 86, "center_x": 145},
        {"text": "Meta xc", "left": 430, "right": 530, "center_y": 72, "center_x": 480},
        {"text": "发送", "left": 890, "right": 940, "center_y": 814, "center_x": 915},
    ]
    assert_true(mod.sandbox_active_target_matches(items, (960, 860), target="Meta_xc") is True, "header alias should confirm")
    assert_true(mod.target_visible_anywhere(items, target="Meta_xc") is True, "visible alias should be detected")
    assert_true(mod.sandbox_active_target_matches(items, (960, 860), target="文件传输助手") is False, "wrong active target must fail")


def test_target_row_point_is_whitelist_exact_and_sidebar_scoped() -> None:
    mod = load_module()
    items = [
        {"text": "Meta xc", "left": 150, "right": 245, "center_y": 142, "center_x": 196},
        {"text": "Meta xc", "left": 500, "right": 610, "center_y": 70, "center_x": 550},
        {"text": "新数据测试", "left": 150, "right": 245, "center_y": 180, "center_x": 196},
    ]
    point = mod.find_target_row_point(items, (902, 1033), target="Meta_xc")
    assert_true(point == [196, 142], f"should click exact sidebar result: {point}")
    assert_true(mod.find_target_row_point(items, (902, 1033), target="文件传输助手") is None, "wrong target should not match")


def test_send_points_prefer_visible_send_button() -> None:
    mod = load_module()
    items = [
        {"text": "Meta_xc", "left": 430, "right": 530, "center_y": 72, "center_x": 480},
        {"text": "发送", "left": 890, "right": 940, "center_y": 814, "center_x": 915},
    ]
    points = mod.calculate_sandbox_send_points(items, (960, 860))
    assert_true(points.get("ok") is True, f"send points should pass: {points}")
    assert_true(points.get("reason") == "send_button_ocr", f"send button OCR should be preferred: {points}")
    assert_true(points.get("send_point") == [915, 814], f"send point should use OCR center: {points}")
    assert_true(points.get("input_point")[1] <= 814 - 50, f"input point must avoid bottom toolbar: {points}")


def test_content_offset_for_window_accounts_for_sandbox_chrome() -> None:
    mod = load_module()
    window = {"rect": [990, 0, 1910, 1080], "hwnd": 0}
    offset = mod.content_offset_for_window(window, (902, 1033))
    assert_true(offset.get("source") == "heuristic", f"unit test should use deterministic fallback: {offset}")
    assert_true(offset.get("x") == 9, f"sandbox left border offset should be 9: {offset}")
    assert_true(offset.get("y") == 38, f"sandbox title/top offset should be 38: {offset}")


def test_sandbox_input_methods_are_guarded_and_ordered() -> None:
    mod = load_module()
    assert_true(mod.input_methods_for_request("auto") == ["sendinput_unicode", "clipboard_chunks"], "auto should try typing before fallback")
    assert_true(mod.input_methods_for_request("sendinput_unicode") == ["sendinput_unicode"], "explicit unicode should not fallback")
    assert_true(mod.input_methods_for_request("clipboard_chunks") == ["clipboard_chunks"], "explicit clipboard chunks should be narrow")
    try:
        mod.input_methods_for_request("clipboard_once")
    except ValueError as exc:
        assert_true("unsupported_sandbox_input_method" in str(exc), f"unsupported method should fail closed: {exc}")
    else:
        raise AssertionError("unsupported method should raise")


def test_sandbox_clipboard_chunks_never_use_one_full_paste() -> None:
    mod = load_module()
    original_copy = mod.clipboard_copy
    original_read = mod.clipboard_read
    original_hotkey = mod.hotkey
    original_sleep = mod.time.sleep
    copied: list[str] = []
    hotkeys: list[tuple[int, ...]] = []
    text = "abcdefghijklmnopqrstuvwxyz012345"
    try:
        mod.clipboard_read = lambda: "OLD"
        mod.clipboard_copy = copied.append
        mod.hotkey = lambda *keys: hotkeys.append(tuple(keys))
        mod.time.sleep = lambda _: None
        result = mod.type_text_clipboard_chunks(text)
    finally:
        mod.clipboard_copy = original_copy
        mod.clipboard_read = original_read
        mod.hotkey = original_hotkey
        mod.time.sleep = original_sleep
    chunks = copied[:-1]
    assert_true(result.get("method") == "clipboard_chunks_sandbox_only", f"wrong method: {result}")
    assert_true(copied[-1] == "OLD", f"previous clipboard should be restored: {copied}")
    assert_true("".join(chunks) == text, f"chunks should reconstruct text: {chunks}")
    assert_true(all(chunk != text for chunk in chunks), f"must not paste full text in one chunk: {chunks}")
    assert_true(len(hotkeys) == len(chunks), f"each chunk should be pasted once: {hotkeys} vs {chunks}")


def test_sandbox_input_confirmation_uses_sandbox_editor_region() -> None:
    mod = load_module()
    items = [
        {
            "text": "你好，我想看15万以内家用二手车，通勤省油，周末带孩",
            "left": 405,
            "right": 849,
            "top": 635,
            "bottom": 658,
            "center_x": 627,
            "center_y": 646.5,
        },
        {
            "text": "你好，我想看15万以内家用二手车",
            "left": 405,
            "right": 849,
            "top": 490,
            "bottom": 520,
            "center_x": 627,
            "center_y": 505,
        },
    ]
    tokens = ["你好，我想看15万以"]
    assert_true(mod.sandbox_input_area_contains_any_token(items, (902, 1033), tokens=tokens) is True, "sandbox editor text should confirm")
    old_only = [items[1]]
    assert_true(mod.sandbox_input_area_contains_any_token(old_only, (902, 1033), tokens=tokens) is False, "older chat bubbles should not confirm")


def test_sandbox_input_quality_rejects_semantic_corruption() -> None:
    mod = load_module()
    corrupted_items = [
        {
            "text": "？果我周六过去看车，，不能顺便试驾？？要提前带身份证",
            "left": 405,
            "right": 849,
            "top": 635,
            "bottom": 658,
            "center_x": 627,
            "center_y": 646.5,
        },
        {
            "text": "和驾驶证吗？",
            "left": 405,
            "right": 849,
            "top": 663,
            "bottom": 686,
            "center_x": 627,
            "center_y": 674.5,
        },
    ]
    text = "如果我周六过去看车，能不能顺便试驾？需要提前带身份证和驾驶证吗？"
    tokens = mod.message_probe_tokens(text)
    report = mod.sandbox_input_quality_report(corrupted_items, (902, 1033), text=text, tokens=tokens)
    assert_true(report.get("ok") is False, f"semantic corruption should be rejected: {report}")
    assert_true("如果" in report.get("missing_required_tokens", []), f"missing leading phrase expected: {report}")
    assert_true("能不能" in report.get("missing_required_tokens", []), f"missing polar question expected: {report}")

    healthy_items = [
        {
            "text": "如果我周六过去看车，能不能顺便试驾？需要提前带身份证",
            "left": 405,
            "right": 849,
            "top": 635,
            "bottom": 658,
            "center_x": 627,
            "center_y": 646.5,
        },
        {
            "text": "和驾驶证吗？",
            "left": 405,
            "right": 849,
            "top": 663,
            "bottom": 686,
            "center_x": 627,
            "center_y": 674.5,
        },
    ]
    ok_report = mod.sandbox_input_quality_report(healthy_items, (902, 1033), text=text, tokens=tokens)
    assert_true(ok_report.get("ok") is True, f"healthy editor text should pass: {ok_report}")


def test_sandbox_rate_guard_blocks_global_and_target_burst() -> None:
    mod = load_module()
    original_read = mod.read_send_guard_state
    original_write = mod.write_send_guard_state
    writes: list[dict[str, Any]] = []
    now = time.time()
    try:
        mod.read_send_guard_state = lambda: {"events": [{"target": f"any:{idx}", "at": now - idx} for idx in range(5)]}
        mod.write_send_guard_state = writes.append
        global_block = mod.sandbox_send_rate_decision(target="Meta_xc", text="hello", reserve=False)
        assert_true(global_block.get("reason") == "global_burst_limit_reached", f"global block expected: {global_block}")

        mod.read_send_guard_state = lambda: {"events": [{"target": "sandbox:Meta_xc", "at": now - 20}]}
        target_block = mod.sandbox_send_rate_decision(target="Meta_xc", text="hello", reserve=False, min_interval_seconds=150)
        assert_true(target_block.get("reason") == "min_interval_not_elapsed", f"target interval block expected: {target_block}")

        mod.read_send_guard_state = lambda: {"events": []}
        allowed = mod.sandbox_send_rate_decision(target="Meta_xc", text="hello", reserve=True, min_interval_seconds=1)
        assert_true(allowed.get("ok") is True, f"reservation should pass: {allowed}")
        assert_true(bool(writes), "reservation should write send guard state")
        assert_true(writes[-1]["events"][-1]["target"] == "sandbox:Meta_xc", f"wrong reserved target: {writes[-1]}")
    finally:
        mod.read_send_guard_state = original_read
        mod.write_send_guard_state = original_write


def test_sandbox_risk_handoff_reason_is_accepted() -> None:
    mod = load_module()
    reason = mod.handoff_reason_for_preflight({"reason": "risk_token:登录", "ocr_text_sample": ["为了你的账号安全，请重新登录。"]})
    assert_true(reason == "sandbox_wechat_security_relogin_detected", f"security relogin should map to sandbox reason: {reason}")
    from apps.wechat_ai_customer_service.scripts.run_customer_service_listener import create_runtime_transport_handoff_case

    with tempfile.TemporaryDirectory() as tmp:
        result = create_runtime_transport_handoff_case(
            tenant_id="sandbox_handoff_reason_unit",
            reason=reason,
            message="sandbox relogin required",
            source="sandbox_wechat_test",
            verdict={"reason": reason},
            handoff_path=Path(tmp) / "handoff_cases.json",
            dispatch_handoff_fn=lambda case: {
                "enabled": True,
                "status": "sent",
                "adapter": "unit",
                "case_id": case.get("case_id"),
            },
        )
    assert_true(result.get("ok") is True and result.get("enabled") is True, f"handoff reason should be accepted: {result}")


def main() -> int:
    tests = [
        test_target_whitelist,
        test_layout_requires_non_overlap_and_host_left,
        test_active_target_confirmation_accepts_safe_aliases,
        test_target_row_point_is_whitelist_exact_and_sidebar_scoped,
        test_send_points_prefer_visible_send_button,
        test_content_offset_for_window_accounts_for_sandbox_chrome,
        test_sandbox_input_methods_are_guarded_and_ordered,
        test_sandbox_clipboard_chunks_never_use_one_full_paste,
        test_sandbox_input_confirmation_uses_sandbox_editor_region,
        test_sandbox_input_quality_rejects_semantic_corruption,
        test_sandbox_rate_guard_blocks_global_and_target_burst,
        test_sandbox_risk_handoff_reason_is_accepted,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} sandbox WeChat safety checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
