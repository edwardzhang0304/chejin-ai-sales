"""Contract checks for Win32/OCR session targeting helpers."""

from __future__ import annotations

from pathlib import Path
import random
import sys

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import session_targeting  # noqa: E402
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


GEOMETRY = {"left": 0, "top": 0, "width": 981, "height": 860}
SESSION_WITH_TEXT = {"name": "新数据测试", "left": 96, "right": 184, "center_y": 164, "click_bounds": [72, 126, 370, 202]}
SESSION_WITHOUT_TEXT = {"name": "客户A", "center_y": 244, "click_bounds": [72, 206, 370, 282]}


def real_layout_snapshot(width: int = 980, height: int = 860) -> dict:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    nav_x = int(width * 0.073)
    sidebar_x = int(width * 0.378)
    sidebar_header_y = int(height * 0.112)
    chat_header_y = int(height * 0.102)
    input_y = int(height * 0.814)
    draw.rectangle((0, 0, nav_x - 1, height - 1), fill=(245, 245, 245))
    draw.rectangle((nav_x, 0, sidebar_x - 1, height - 1), fill=(235, 235, 235))
    draw.line((nav_x, 0, nav_x, height - 1), fill=(150, 150, 150), width=2)
    draw.line((sidebar_x, 0, sidebar_x, height - 1), fill=(130, 130, 130), width=2)
    draw.line((nav_x, sidebar_header_y, sidebar_x, sidebar_header_y), fill=(120, 120, 120), width=2)
    draw.line((sidebar_x, chat_header_y, width - 1, chat_header_y), fill=(120, 120, 120), width=2)
    draw.line((sidebar_x, input_y, width - 1, input_y), fill=(120, 120, 120), width=2)
    regions = window_layout.build_structural_layout_regions(image)
    assert_true(regions.get("ok"), f"real layout builder rejected synthetic WeChat frame: {regions}")
    return window_layout.build_layout_snapshot(
        hwnd=1,
        frame_id=window_layout.new_frame_id(1),
        capture_mode=window_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
        image_size=image.size,
        capture_screen_origin=[0, 0],
        window_rect=[0, 0, width, height],
        client_rect=[0, 0, width, height],
        client_screen_origin=[0, 0],
        dpi_scale=1.0,
        regions=regions["regions"],
        anchors=regions["anchors"],
        confidence=regions["confidence"],
        conflicts=regions["conflicts"],
        executable=True,
    )


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


def test_short_title_session_points_stay_out_of_preview_zone() -> None:
    geometry = {"left": 0, "top": 0, "width": 980, "height": 860}
    session = {"name": "许聪", "left": 153, "right": 197, "center_y": 128, "click_bounds": [142, 108, 208, 148]}
    points = session_targeting.session_row_click_candidate_points(
        session,
        geometry,
        default_x=175,
        min_points=10,
    )
    assert_true(len(points) >= 10, f"expected enough candidate points: {points}")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    assert_true(min(xs) >= 142 and max(xs) <= 208, f"short-title clicks must stay inside the proven row bounds: {points}")
    assert_true(len(set(points)) >= 10, f"candidate points should stay distinct without entering preview zone: {points}")
    assert_true(max(xs) - min(xs) >= 34, f"candidate x spread should still avoid fixed-pixel clicks: {points}")
    assert_true(max(ys) - min(ys) >= 18, f"candidate y spread should still avoid fixed-line clicks: {points}")


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
        {"state": "wrong_target_service_container_detected"},
        {"reason": "blank_render"},
        {"reason": "login_or_qr"},
        {"reason": "auxiliary_shell_window"},
        {"reason": "service_container_wrong_target"},
        {"state": "target_not_confirmed"},
        None,
    ]
    for validation in cases:
        extracted = session_targeting.target_switch_validation_is_hard_stop(validation)
        facade = sidecar.target_switch_validation_is_hard_stop(validation)
        assert_true(extracted == facade, f"hard-stop mismatch: {validation}: {extracted} vs {facade}")


def title_item(text: str, left: float, right: float) -> dict[str, float | str]:
    return {
        "text": text,
        "left": left,
        "right": right,
        "top": 76.0,
        "bottom": 102.0,
        "center_x": (left + right) / 2.0,
        "center_y": 89.0,
    }


def test_active_title_reuses_raw_ocr_for_private_group_admission() -> None:
    private = sidecar.active_chat_title_evidence(
        [title_item("张三-CJ123", 402, 556)],
        (981, 860),
        target="CJ123",
        exact=False,
        layout_snapshot=real_layout_snapshot(981, 860),
    )
    group = sidecar.active_chat_title_evidence(
        [title_item("销售讨论-CJ123", 402, 592), title_item("（5）", 598, 642)],
        (981, 860),
        target="CJ123",
        exact=False,
        layout_snapshot=real_layout_snapshot(981, 860),
    )

    assert_true(private["matched"] and private["conversation_type"] == "private", f"private title rejected: {private}")
    assert_true(private["admission_allowed"], f"private title not admitted: {private}")
    assert_true(group["matched"] and group["conversation_type"] == "group", f"split group suffix missed: {group}")
    assert_true(not group["admission_allowed"], f"group title admitted: {group}")


def test_private_and_group_same_code_only_private_is_unique_candidate() -> None:
    private = {"name": "张三-CJ123", "raw_title": "张三-CJ123", "session_key": "private"}
    group = {"name": "销售讨论-CJ123(5)", "raw_title": "销售讨论-CJ123(5)", "session_key": "group"}

    selected, evidence = sidecar.find_unique_session_candidate_by_semantics(
        [group, private],
        target="张三-CJ123",
        semantic_target="CJ123",
    )

    assert_true(selected is private, f"private candidate was not selected: {selected}, {evidence}")
    assert_true(not evidence.get("ambiguous"), f"group should not create a short-code conflict: {evidence}")
    excluded = evidence["attempts"][0]["excluded_by_c2_admission"]
    assert_true(excluded and excluded[0]["conversation_type"] == "group", f"group exclusion evidence missing: {evidence}")


def test_two_private_sessions_with_same_code_are_ambiguous() -> None:
    sessions = [
        {"name": "张三-CJ123", "raw_title": "张三-CJ123", "session_key": "private-a"},
        {"name": "李四-CJ123", "raw_title": "李四-CJ123", "session_key": "private-b"},
    ]
    selected, evidence = sidecar.find_unique_session_candidate_by_semantics(
        sessions,
        target="CJ123",
        semantic_target="CJ123",
    )
    assert_true(selected is None and evidence.get("ambiguous"), f"two private candidates must conflict: {evidence}")


def test_c2_activation_requires_strict_private_title_evidence() -> None:
    base = {
        "ok": True,
        "confirmation_confidence": "active_title_strict",
        "conversation_type_evidence": {"short_code_confirmed": True},
    }
    assert_true(sidecar.c2_target_activation_confirmed({**base, "conversation_type": "private"}), "private title should pass")
    assert_true(not sidecar.c2_target_activation_confirmed({**base, "conversation_type": "group"}), "group title must fail")
    assert_true(not sidecar.c2_target_activation_confirmed({**base, "conversation_type": "unknown"}), "unknown title must fail")


def test_c2_locate_rejects_missing_short_code_before_ui() -> None:
    result = sidecar.locate_chat_target_for_c2(
        1,
        target="张三",
        session_key="wx:rpa:v1:any",
        remark_code="",
        target_mode="visible",
        exact=False,
        artifact_dir=None,
        sidecar_run_id="missing-code",
        failure_state="target_not_confirmed",
        failure_error_code="TARGET_NOT_CONFIRMED",
    )
    assert_true(not result["ok"], f"missing short code should fail: {result}")
    assert_true(result["error_code"] == "C2_TARGET_REMARK_CODE_MISSING", f"wrong missing-code error: {result}")
    assert_true(not result["opened"], f"missing short code must not click a conversation: {result}")


def main() -> int:
    tests = [
        test_session_targeting_module_exports_expected_helpers,
        test_session_row_click_x_matches_sidecar,
        test_session_row_candidate_points_match_sidecar_with_seed,
        test_short_title_session_points_stay_out_of_preview_zone,
        test_choose_session_row_click_point_matches_sidecar_with_seed,
        test_target_switch_hard_stop_matches_sidecar_contract,
        test_active_title_reuses_raw_ocr_for_private_group_admission,
        test_private_and_group_same_code_only_private_is_unique_candidate,
        test_two_private_sessions_with_same_code_are_ambiguous,
        test_c2_activation_requires_strict_private_title_evidence,
        test_c2_locate_rejects_missing_short_code_before_ui,
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
