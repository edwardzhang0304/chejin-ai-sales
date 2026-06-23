"""Contract checks for visible WeChat window selection planning."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_candidate(
    *,
    hwnd: int,
    title: str,
    geometry: dict[str, int],
    capture_ready: bool,
    content_health_score: int = 0,
) -> dict:
    item = {"hwnd": hwnd, "title": title, "class_name": "Qt51514QWindowIcon", "visible": True}
    return {
        "item": item,
        "geometry": geometry,
        "content_health_score": content_health_score,
        "score": window_action_planning.visible_window_candidate_score(
            geometry,
            capture_ready=capture_ready,
            content_health_score=content_health_score,
            min_send_width=sidecar.MIN_SEND_CLIENT_WIDTH,
            min_send_height=sidecar.MIN_SEND_CLIENT_HEIGHT,
            title_score=sidecar.wechat_window_title_score(item),
        ),
    }


def test_window_selection_planning_exports_expected_helpers() -> None:
    for name in ("visible_window_candidate_score", "select_best_visible_window_candidate"):
        assert_true(callable(getattr(window_action_planning, name, None)), f"window selection planner missing: {name}")


def test_candidate_score_prefers_capture_ready_safe_large_window() -> None:
    small_login = make_candidate(
        hwnd=1001,
        title="微信",
        geometry={"left": 775, "top": 331, "right": 1143, "bottom": 815, "width": 368, "height": 484},
        capture_ready=False,
    )
    large_actionable = make_candidate(
        hwnd=1002,
        title="Weixin",
        geometry={"left": 0, "top": 0, "right": 980, "bottom": 860, "width": 980, "height": 860},
        capture_ready=True,
    )
    selected = window_action_planning.select_best_visible_window_candidate([small_login, large_actionable])
    assert_true(int((selected or {}).get("hwnd") or 0) == 1002, f"large actionable window should win: {selected}")
    assert_true((selected or {}).get("geometry_hint") == large_actionable["geometry"], f"geometry hint mismatch: {selected}")


def test_candidate_score_prefers_readable_window_over_larger_blank_window() -> None:
    larger_blank = make_candidate(
        hwnd=1001,
        title="微信",
        geometry={"left": 0, "top": 0, "right": 981, "bottom": 860, "width": 981, "height": 860},
        capture_ready=True,
        content_health_score=-100,
    )
    readable_chat = make_candidate(
        hwnd=1002,
        title="微信",
        geometry={"left": 0, "top": 0, "right": 785, "bottom": 688, "width": 785, "height": 688},
        capture_ready=True,
        content_health_score=38,
    )
    selected = window_action_planning.select_best_visible_window_candidate([larger_blank, readable_chat])
    assert_true(int((selected or {}).get("hwnd") or 0) == 1002, f"readable window should win over larger blank: {selected}")
    assert_true(int((selected or {}).get("content_health_score") or 0) == 38, f"content score mismatch: {selected}")


def test_selection_returns_none_for_empty_or_invalid_candidates() -> None:
    assert_true(window_action_planning.select_best_visible_window_candidate([]) is None, "empty candidate list should return None")
    assert_true(window_action_planning.select_best_visible_window_candidate([{"score": (1, 0, 0, 0, 0)}]) is None, "missing item should return None")


def main() -> int:
    tests = [
        test_window_selection_planning_exports_expected_helpers,
        test_candidate_score_prefers_capture_ready_safe_large_window,
        test_candidate_score_prefers_readable_window_over_larger_blank_window,
        test_selection_returns_none_for_empty_or_invalid_candidates,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR window selection planning checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
