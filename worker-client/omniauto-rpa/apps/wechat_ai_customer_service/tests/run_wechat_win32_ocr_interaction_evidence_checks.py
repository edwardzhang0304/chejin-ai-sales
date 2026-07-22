"""Focused checks for no-blind-click evidence helpers."""

from __future__ import annotations

import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import interaction_evidence


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def test_missing_or_failed_probe_never_authorizes_click() -> None:
    missing = interaction_evidence.input_surface_click_evidence(
        {"has_visible_text": False, "reason": "input_region_blank"}
    )
    failed = interaction_evidence.input_surface_click_evidence(
        {
            "has_visible_text": False,
            "reason": "input_region_blank",
            "bounds": [400, 680, 880, 800],
            "error": "capture_failed",
        }
    )
    assert_true(missing.get("ok") is False, f"missing bounds must block: {missing}")
    assert_true(failed.get("ok") is False, f"failed probe must block: {failed}")


def test_verified_clicks_stay_inside_observed_interior_with_variation() -> None:
    evidence = interaction_evidence.input_surface_click_evidence(
        {"has_visible_text": False, "reason": "input_region_blank", "bounds": [394, 677, 886, 799]}
    )
    assert_true(evidence.get("ok") is True, f"valid observed input surface should pass: {evidence}")
    points: set[tuple[int, int]] = set()
    for seed in range(60):
        random.seed(seed)
        selected = interaction_evidence.choose_input_click_point(evidence, random_module=random)
        x, y = selected["point"]
        left, top, right, bottom = evidence["click_bounds"]
        assert_true(left <= x < right and top <= y < bottom, f"point escaped evidence bounds: {selected}")
        points.add((x, y))
    assert_true(len(points) >= 24, f"verified point selection lacks variation: {len(points)}")


def test_missing_input_bounds_causes_zero_rpa_clicks() -> None:
    originals = {
        "activate_window": sidecar.activate_window,
        "recover_send_window_guard": sidecar.recover_send_window_guard,
        "capture_wechat": sidecar.capture_wechat,
        "run_ocr_for_input_region_probe": sidecar.run_ocr_for_input_region_probe,
        "input_text_region_state": sidecar.input_text_region_state,
        "clear_existing_input_draft": sidecar.clear_existing_input_draft,
        "human_client_click": sidecar.human_client_click,
        "time_sleep": sidecar.time.sleep,
    }
    calls = {"click": 0}
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    try:
        sidecar.activate_window = lambda *_args, **_kwargs: True
        sidecar.recover_send_window_guard = lambda *_args, **_kwargs: {"ok": True, "reason": "window_valid"}
        sidecar.capture_wechat = lambda *_args, **_kwargs: (object(), "input.png")
        sidecar.run_ocr_for_input_region_probe = lambda *_args, **_kwargs: ([], "roi")
        sidecar.input_text_region_state = lambda *_args, **_kwargs: {"has_visible_text": False, "reason": "input_region_blank"}
        sidecar.clear_existing_input_draft = lambda *_args, before_state=None, **_kwargs: {"ok": True, "after": before_state}
        sidecar.human_client_click = lambda *_args, **_kwargs: calls.__setitem__("click", calls["click"] + 1)
        sidecar.time.sleep = lambda _seconds: None
        result = sidecar.paste_text_with_confirmation(
            1001,
            "测试输入",
            points={"input_point": [637, 715], "send_point": [919, 816]},
            geometry=geometry,
            settings={"enabled": True, "method": "sendinput_unicode"},
        )
        assert_true(result.get("ok") is False, f"missing evidence must stop input: {result}")
        assert_true(result.get("reason") == "input_click_evidence_missing_before_type", f"wrong failure: {result}")
        assert_true(calls["click"] == 0, f"missing input bounds must cause zero clicks: {calls}")
    finally:
        for name, value in originals.items():
            if name == "time_sleep":
                sidecar.time.sleep = value
            else:
                setattr(sidecar, name, value)


def test_missing_search_label_causes_zero_rpa_actions() -> None:
    originals = {
        "basic_send_window_guard": sidecar.basic_send_window_guard,
        "get_window_geometry": sidecar.get_window_geometry,
        "capture_wechat": sidecar.capture_wechat,
        "run_ocr_traced": sidecar.run_ocr_traced,
        "target_switch_surface_state": sidecar.target_switch_surface_state,
        "human_window_image_click_in_bounds": sidecar.human_window_image_click_in_bounds,
        "key_press": sidecar.key_press,
    }
    calls = {"click": 0, "key": 0}
    try:
        sidecar.basic_send_window_guard = lambda *_args, **_kwargs: {"ok": True, "reason": "window_valid"}
        sidecar.get_window_geometry = lambda *_args, **_kwargs: {
            "left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860
        }
        sidecar.capture_wechat = lambda *_args, **_kwargs: (object(), "search.png")
        sidecar.run_ocr_traced = lambda *_args, **_kwargs: []
        sidecar.target_switch_surface_state = lambda *_args, **_kwargs: {"ok": True, "reason": "surface_ready"}
        sidecar.human_window_image_click_in_bounds = lambda *_args, **_kwargs: calls.__setitem__("click", calls["click"] + 1)
        sidecar.key_press = lambda *_args, **_kwargs: calls.__setitem__("key", calls["key"] + 1)
        result = sidecar.clear_sidebar_search_box_without_select_all(1001, 122, 64, target_hint="新数据测试")
        assert_true(result.get("ok") is False, f"missing search label must stop: {result}")
        assert_true(result.get("reason") == "search_box_evidence_missing_before_click", f"wrong failure: {result}")
        assert_true(calls == {"click": 0, "key": 0}, f"missing search evidence must cause zero actions: {calls}")
    finally:
        for name, value in originals.items():
            setattr(sidecar, name, value)


def test_observed_search_placeholder_variants_are_accepted_in_sidebar_only() -> None:
    geometry = {"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860}
    placeholder = {"text": "Q搜索", "left": 101, "top": 61, "right": 143, "bottom": 83}
    evidence = sidecar.sidebar_search_box_evidence([placeholder], geometry=geometry)
    assert_true(evidence.get("ok") is True, f"known placeholder OCR variant must be accepted: {evidence}")
    outside = sidecar.sidebar_search_box_evidence([dict(placeholder, left=522, right=563)], geometry=geometry)
    assert_true(outside.get("ok") is False, f"search-like text outside sidebar must remain blocked: {outside}")


def main() -> int:
    tests = [
        test_missing_or_failed_probe_never_authorizes_click,
        test_verified_clicks_stay_inside_observed_interior_with_variation,
        test_missing_input_bounds_causes_zero_rpa_clicks,
        test_missing_search_label_causes_zero_rpa_actions,
        test_observed_search_placeholder_variants_are_accepted_in_sidebar_only,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"All {len(tests)} Win32/OCR interaction evidence checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
