"""Contract checks for Win32/OCR send and UI action risk helpers."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import send_action_risk  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_send_action_risk_module_exports_expected_helpers() -> None:
    for name in (
        "send_rate_decision",
        "ui_action_kind",
        "ui_action_point",
        "ui_action_min_gap_ms",
        "count_recent_near_point_actions",
        "plan_rpa_action_pacing",
    ):
        assert_true(callable(getattr(send_action_risk, name, None)), f"send/action risk helper missing: {name}")


def test_send_rate_decision_matches_sidecar_contract() -> None:
    scenarios = [
        (
            {"events": [{"target": "新数据测试", "at": 100.0}]},
            {
                "target": "新数据测试",
                "now_ts": 120.0,
                "min_interval_seconds": 30,
                "burst_window_seconds": 600,
                "burst_limit": 5,
            },
        ),
        (
            {"events": [{"target": "新数据测试", "at": value} for value in (100, 150, 200, 250, 300)]},
            {
                "target": "新数据测试",
                "now_ts": 350.0,
                "min_interval_seconds": 0,
                "burst_window_seconds": 600,
                "burst_limit": 5,
            },
        ),
        (
            {"events": [{"target": "其他客户", "at": 348.0}]},
            {
                "target": "新数据测试",
                "now_ts": 350.0,
                "min_interval_seconds": 30,
                "burst_window_seconds": 600,
                "burst_limit": 5,
            },
        ),
    ]
    for state, kwargs in scenarios:
        extracted = send_action_risk.send_rate_decision(state, **kwargs)
        facade = sidecar.send_rate_decision(state, **kwargs)
        assert_true(extracted == facade, f"send rate decision drift: {extracted} vs {facade}")


def test_ui_action_kind_and_point_contract() -> None:
    cases = {
        "key_press": "keyboard",
        "keyboard_enter": "keyboard",
        "sendinput_unicode_unit": "keyboard",
        "mouse_click": "mouse",
        "human_window_image_click": "mouse",
        "scroll_latest": "scroll",
        "focus_wechat": "focus",
        "other": "other",
    }
    for action, expected in cases.items():
        assert_true(send_action_risk.ui_action_kind(action) == expected, f"kind mismatch: {action}")
        assert_true(sidecar.ui_action_kind(action) == expected, f"sidecar kind mismatch: {action}")
    metadata_cases = [
        ({"point": [11, 22]}, (11, 22)),
        ({"jitter": {"final": [33, 44]}}, (33, 44)),
        ({"x": "55", "y": "66"}, (55, 66)),
        ({"point": ["bad", 22], "x": 77, "y": 88}, None),
        (None, None),
    ]
    for metadata, expected in metadata_cases:
        assert_true(send_action_risk.ui_action_point(metadata) == expected, f"point mismatch: {metadata}")
        assert_true(sidecar.ui_action_point(metadata) == expected, f"sidecar point mismatch: {metadata}")


def test_near_point_count_and_pacing_plan() -> None:
    events = [
        {"ts": 10.0, "metadata": {"point": [100, 100]}},
        {"ts": 11.0, "metadata": {"jitter": {"final": [103, 97]}}},
        {"ts": 11.5, "metadata": {"x": 250, "y": 250}},
        {"ts": 1.0, "metadata": {"point": [100, 100]}},
    ]
    near_count = send_action_risk.count_recent_near_point_actions(
        events,
        point=(101, 101),
        now_ts=12.0,
        radius=5,
        window_seconds=3.0,
    )
    assert_true(near_count == 2, f"near-point count mismatch: {near_count}")
    assert_true(
        sidecar.count_recent_near_point_actions(events, point=(101, 101), now_ts=12.0, radius=5, window_seconds=3.0)
        == near_count,
        "sidecar near-point wrapper should match extracted helper",
    )
    plan = send_action_risk.plan_rpa_action_pacing(
        "mouse_click",
        metadata={"point": [102, 102]},
        recent_events=events,
        last_state={"ts": 11.95, "kind": "keyboard", "point": [101, 100]},
        now_ts=12.0,
        min_gap_ms=110,
        kind_switch_gap_ms=170,
        near_point_radius_px=5,
        near_point_gap_ms=720,
        near_point_soft_limit=2,
        extra_delay_ms=lambda reason: {"min_gap": 20, "near_point_repeat": 100, "near_point_soft_limit": 240}[reason],
    )
    assert_true(plan.get("kind") == "mouse", f"pacing kind mismatch: {plan}")
    assert_true(plan.get("delay_ms") == 960, f"near-point soft limit should dominate delay: {plan}")
    assert_true("kind_switch:keyboard->mouse" in plan.get("reasons", []), f"missing kind-switch reason: {plan}")
    assert_true("near_point_repeat" in plan.get("reasons", []), f"missing repeat reason: {plan}")
    assert_true("near_point_soft_limit:2" in plan.get("reasons", []), f"missing soft-limit reason: {plan}")
    assert_true(plan.get("point") == [102, 102], f"point metadata mismatch: {plan}")


def main() -> int:
    tests = [
        test_send_action_risk_module_exports_expected_helpers,
        test_send_rate_decision_matches_sidecar_contract,
        test_ui_action_kind_and_point_contract,
        test_near_point_count_and_pacing_plan,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR send/action risk checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
