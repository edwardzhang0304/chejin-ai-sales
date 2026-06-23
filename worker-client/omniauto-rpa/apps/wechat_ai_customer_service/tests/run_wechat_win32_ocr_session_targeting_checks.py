"""Contract checks for Win32/OCR session targeting helpers."""

from __future__ import annotations

from pathlib import Path
import random
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import session_targeting  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


GEOMETRY = {"left": 0, "top": 0, "width": 981, "height": 860}
SESSION_WITH_TEXT = {"name": "新数据测试", "left": 96, "right": 184, "center_y": 164}
SESSION_WITHOUT_TEXT = {"name": "客户A", "center_y": 244}


def test_session_targeting_module_exports_expected_helpers() -> None:
    for name in (
        "session_row_click_x",
        "session_row_click_candidate_points",
        "choose_session_row_click_point",
        "target_switch_validation_is_hard_stop",
    ):
        assert_true(callable(getattr(session_targeting, name, None)), f"session targeting helper missing: {name}")


def test_session_row_click_x_matches_sidecar() -> None:
    for session in (SESSION_WITH_TEXT, SESSION_WITHOUT_TEXT):
        extracted = session_targeting.session_row_click_x(session, GEOMETRY, default_x=260)
        facade = sidecar.session_row_click_x(session, GEOMETRY, default_x=260)
        assert_true(extracted == facade, f"session click x mismatch: {extracted} vs {facade}")


def test_session_row_candidate_points_match_sidecar_with_seed() -> None:
    for session in (SESSION_WITH_TEXT, SESSION_WITHOUT_TEXT, {"name": "missing center"}):
        random.seed(20260619)
        extracted = session_targeting.session_row_click_candidate_points(
            session,
            GEOMETRY,
            default_x=260,
            min_points=12,
            random_module=random,
        )
        random.seed(20260619)
        facade = sidecar.session_row_click_candidate_points(session, GEOMETRY, default_x=260, min_points=12)
        assert_true(extracted == facade, f"candidate points mismatch: {session}: {extracted} vs {facade}")


def test_choose_session_row_click_point_matches_sidecar_with_seed() -> None:
    for session in (SESSION_WITH_TEXT, SESSION_WITHOUT_TEXT, {"name": "missing center"}):
        random.seed(20260619)
        extracted = session_targeting.choose_session_row_click_point(
            session,
            GEOMETRY,
            default_x=260,
            random_module=random,
        )
        random.seed(20260619)
        facade = sidecar.choose_session_row_click_point(session, GEOMETRY, default_x=260)
        assert_true(extracted == facade, f"chosen point mismatch: {session}: {extracted} vs {facade}")


def test_target_switch_hard_stop_matches_sidecar_contract() -> None:
    cases = [
        {"state": "blank_render_detected"},
        {"state": "login_window_detected"},
        {"state": "auxiliary_shell_window_detected"},
        {"reason": "blank_render"},
        {"reason": "login_or_qr"},
        {"reason": "auxiliary_shell_window"},
        {"state": "target_not_confirmed"},
        None,
    ]
    for validation in cases:
        extracted = session_targeting.target_switch_validation_is_hard_stop(validation)
        facade = sidecar.target_switch_validation_is_hard_stop(validation)
        assert_true(extracted == facade, f"hard-stop mismatch: {validation}: {extracted} vs {facade}")


def main() -> int:
    tests = [
        test_session_targeting_module_exports_expected_helpers,
        test_session_row_click_x_matches_sidecar,
        test_session_row_candidate_points_match_sidecar_with_seed,
        test_choose_session_row_click_point_matches_sidecar_with_seed,
        test_target_switch_hard_stop_matches_sidecar_contract,
    ]
    passed = 0
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
        passed += 1
    print(f"All {passed} WeChat Win32/OCR session targeting checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
