from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import sys

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_action_planning
from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout
from apps.wechat_ai_customer_service.adapters import wechat_win32_ocr_sidecar as sidecar


class PhysicalClickReached(RuntimeError):
    pass


def assert_true(value: object, message: str) -> None:
    if not value:
        raise AssertionError(message)


def shell_image(width: int = 800, height: int = 812) -> Image.Image:
    image = Image.new("RGB", (width, height), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    nav_x = int(width * 0.09)
    sidebar_x = int(width * 0.38)
    header_y = int(height * 0.085)
    input_y = int(height * 0.77)
    draw.rectangle((0, 0, nav_x, height), fill=(226, 226, 226))
    draw.rectangle((nav_x + 1, 0, sidebar_x, height), fill=(242, 242, 242))
    draw.rectangle((sidebar_x + 1, 0, width, height), fill=(250, 250, 250))
    draw.line((nav_x, 0, nav_x, height), fill=(190, 190, 190), width=2)
    draw.line((sidebar_x, 0, sidebar_x, height), fill=(185, 185, 185), width=2)
    draw.line((nav_x, header_y, width, header_y), fill=(188, 188, 188), width=2)
    draw.rectangle((sidebar_x + 1, input_y, width, height), fill=(244, 244, 244))
    draw.line((sidebar_x, input_y, width, input_y), fill=(180, 180, 180), width=2)
    return image


def search_ocr() -> list[dict[str, object]]:
    return [{
        "text": "Q搜索", "left": 92, "top": 24, "right": 155, "bottom": 44,
        "center_x": 123.5, "center_y": 34.0, "confidence": 0.96,
    }]


def check_window_profiles() -> int:
    checks = 0
    work = {"left": 1920, "top": -20, "width": 1920, "height": 1080}
    expected = {
        1.0: (1932, -8, 800, 852),
        1.25: (1935, -5, 985, 1050),
        1.5: (1938, -2, 980, 1044),
    }
    for dpi, target in expected.items():
        result = window_action_planning.plan_normalize_wechat_window(
            {}, dpi_scale=dpi, work_area=work
        )
        actual = (result["left"], result["top"], result["width"], result["height"])
        assert_true(actual == target, f"DPI profile mismatch {dpi}: {result}")
        margin = round(12 * dpi)
        assert_true(
            result["right"] <= work["left"] + work["width"] - margin,
            f"right margin was not preserved at {dpi}: {result}",
        )
        assert_true(
            result["bottom"] <= work["top"] + work["height"] - margin,
            f"bottom/taskbar margin was not preserved at {dpi}: {result}",
        )
        checks += 1
    constrained = window_action_planning.plan_normalize_wechat_window(
        {},
        dpi_scale=1.0,
        work_area={"left": -1280, "top": 40, "width": 820, "height": 900},
    )
    assert_true(constrained["ok"], f"negative-coordinate secondary monitor failed: {constrained}")
    assert_true(constrained["left"] == -1268 and constrained["top"] == 52, str(constrained))
    assert_true(constrained["right"] <= -472, f"right safety margin missing: {constrained}")
    assert_true(constrained["bottom"] <= 928, f"bottom safety margin missing: {constrained}")
    unavailable = window_action_planning.plan_normalize_wechat_window(
        {}, dpi_scale=1.0, work_area={"left": 0, "top": 0, "width": 0, "height": 0}
    )
    assert_true(not unavailable["ok"], "missing current-monitor work area must fail")
    return checks + 5


def check_calibration_and_mapping() -> int:
    image = shell_image()
    calibration = window_layout.build_startup_layout_calibration(
        hwnd=101,
        process_id=202,
        image=image,
        ocr_items=search_ocr(),
        window_rect={"left": 12, "top": 12, "right": 812, "bottom": 864},
        client_rect={"width": image.width, "height": image.height},
        client_screen_origin=[20, 50],
        dpi_scale=1.0,
        capture_mode=window_layout.CAPTURE_MODE_CLIENT_AREA,
    )
    assert_true(calibration["executable"], json.dumps(calibration, ensure_ascii=False))
    required = {
        "calibration_id", "schema_version", "hwnd", "process_id", "window_rect",
        "client_rect", "client_screen_origin", "dpi_scale", "image_width", "image_height",
        "capture_mode", "left_nav_bounds", "sidebar_bounds", "sidebar_header_bounds",
        "session_list_bounds", "chat_header_bounds", "message_viewport_bounds",
        "toolbar_bounds", "input_bounds", "anchors", "confidence", "conflicts",
        "calibrated_at", "executable",
    }
    assert_true(required.issubset(calibration), "calibration fields incomplete")
    plus = window_layout.map_reference_region_point(calibration, "plus_entry")
    assert_true(window_layout.point_in_bounds(plus["image_point"], calibration["sidebar_header_bounds"]), "plus outside sidebar header")
    header_left, header_top, header_right, header_bottom = calibration["sidebar_header_bounds"]
    expected_plus = [
        header_left + round((265 / 298) * (header_right - header_left)),
        header_top + round((29 / 60) * (header_bottom - header_top)),
    ]
    assert_true(
        plus["image_point"] == expected_plus,
        f"plus mapping did not match the independently calculated v0.9.20 regional reference: {plus} vs {expected_plus}",
    )
    calibration_with_irrelevant_anchor = {
        **calibration,
        "anchors": [
            *list(calibration.get("anchors") or []),
            {"name": "startup_plus_pixel_anchor", "point": [1, 1]},
        ],
    }
    assert_true(
        window_layout.map_reference_region_point(
            calibration_with_irrelevant_anchor,
            "plus_entry",
        )["image_point"] == expected_plus,
        "startup pixels changed the frozen region-reference mapping",
    )
    session = window_layout.map_reference_region_point(calibration, "session_row_x", dynamic_axis_value=240)
    assert_true(session["image_point"][1] == 240, "dynamic session row y was not retained")
    session_left, _session_top, session_right, _session_bottom = calibration["session_list_bounds"]
    expected_session_x = session_left + round((182 / 298) * (session_right - session_left))
    assert_true(session["image_point"][0] == expected_session_x, "session X did not use the frozen regional reference")
    bad_capture = window_layout.build_startup_layout_calibration(
        hwnd=101, process_id=202, image=image, ocr_items=search_ocr(),
        window_rect={}, client_rect={"width": image.width, "height": image.height},
        client_screen_origin=[0, 0], dpi_scale=1.0,
        capture_mode=window_layout.CAPTURE_MODE_PRINT_WINDOW,
    )
    assert_true(not bad_capture["executable"], "PrintWindow must never create executable calibration")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "calibration.json"
        window_layout.write_startup_layout_calibration(path, calibration)
        restored = window_layout.read_startup_layout_calibration(path)
        assert_true(restored == calibration, "persisted calibration changed")
    return 10


def check_public_normalize_entry() -> int:
    image = shell_image()
    originals = {
        name: getattr(sidecar, name)
        for name in (
            "_WIN32_IMPORT_ERROR", "ensure_visible_wechat_window", "select_primary_visible_main_window",
            "activate_window", "normalize_wechat_window", "get_window_client_geometry",
            "try_image_grab", "save_screenshot_artifact", "get_window_geometry", "window_dpi_scale",
            "win32gui", "win32process", "STARTUP_CALIBRATION_PATH", "foreground_window_matches_target",
            "human_window_image_hover", "human_window_image_click_in_bounds", "add_friend_paced_pause",
            "human_window_image_click", "human_client_click", "ensure_dpi_awareness_status",
        )
    }
    original_ocr = sidecar.win32_ocr_engine.run_ocr_with_cache
    windll_present = hasattr(sidecar.ctypes, "windll")
    original_windll = getattr(sidecar.ctypes, "windll", None)
    try:
        sidecar._WIN32_IMPORT_ERROR = ""
        sidecar.ensure_dpi_awareness_status = lambda: {
            "ok": True,
            "per_monitor_aware": True,
            "awareness": 2,
            "awareness_name": "per_monitor_aware",
            "query_method": "test_windows_boundary",
        }
        foreground = {"hwnd": 909}
        sidecar.ensure_visible_wechat_window = lambda **_kwargs: {
            "main_windows": [{"hwnd": 101, "pid": 202}],
            "visible_main_windows": [{"hwnd": 101, "pid": 202}],
            "visible_windows": [{"hwnd": 101, "pid": 202}],
        }
        sidecar.select_primary_visible_main_window = lambda _probe: {"hwnd": 101, "pid": 202}
        sidecar.activate_window = lambda hwnd, **_kwargs: foreground.__setitem__("hwnd", int(hwnd))
        sidecar.win32gui = SimpleNamespace(
            IsWindow=lambda hwnd: int(hwnd) == 101,
            GetForegroundWindow=lambda: foreground["hwnd"],
            GetAncestor=lambda hwnd, _flag: int(hwnd),
        )
        sidecar.ctypes.windll = SimpleNamespace(
            user32=SimpleNamespace(
                IsIconic=lambda _hwnd: 0,
                IsWindowVisible=lambda _hwnd: True,
            )
        )
        normalize_calls = {"count": 0}
        capture_calls = {"count": 0}
        ocr_calls = {"count": 0}

        def normalize_boundary(_hwnd, **_kwargs):
            normalize_calls["count"] += 1
            return {"ok": True, "enabled": True, "applied": True, "reason": "normalized"}

        def capture_boundary(_rect):
            capture_calls["count"] += 1
            return image.copy()

        def ocr_boundary(*_args, **kwargs):
            ocr_calls["count"] += 1
            source = _args[0] if _args else None
            items = search_ocr()
            if getattr(source, "size", None) == image.size:
                items = [
                    *items,
                    {
                        "text": "CJTEST01", "left": 120, "top": 112,
                        "right": 210, "bottom": 136, "center_x": 165.0,
                        "center_y": 124.0, "confidence": 0.98,
                    },
                ]
            return items, kwargs.get("engine")

        sidecar.normalize_wechat_window = normalize_boundary
        sidecar.get_window_client_geometry = lambda _hwnd: {"width": image.width, "height": image.height, "screen_left": 20, "screen_top": 50}
        sidecar.try_image_grab = capture_boundary
        sidecar.save_screenshot_artifact = lambda *_args, **_kwargs: "startup.png"
        sidecar.get_window_geometry = lambda _hwnd: {"left": 12, "top": 12, "right": 812, "bottom": 864, "width": 800, "height": 852}
        sidecar.window_dpi_scale = lambda _hwnd: 1.0
        sidecar.win32process = SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (1, 202))
        sidecar.human_window_image_hover = lambda *_args, **_kwargs: {"ok": True}
        sidecar.add_friend_paced_pause = lambda *_args, **_kwargs: 0.0
        sidecar.win32_ocr_engine.run_ocr_with_cache = ocr_boundary
        with tempfile.TemporaryDirectory() as directory:
            sidecar.STARTUP_CALIBRATION_PATH = Path(directory) / "startup.json"
            payload = sidecar.run_action(SimpleNamespace(
                action="normalize-window", artifact_dir=directory, phone="", wechat="",
                verify_message="", remark_name="", remark_code="", window_policy="normalize",
            ))
            assert_true(payload["ok"], json.dumps(payload, ensure_ascii=False, default=str))
            assert_true(payload["screenshot_call_count"] == 1, "startup public entry must capture once")
            assert_true(payload["ocr_call_count"] == 1, "startup public entry must OCR once")
            assert_true(payload["no_clicks_performed"] is True, "startup calibration must not click")
            assert_true(Path(sidecar.STARTUP_CALIBRATION_PATH).is_file(), "public entry did not persist calibration")
            assert_true(normalize_calls["count"] == 1, "startup normalization must run exactly once")
            startup_capture_count = capture_calls["count"]
            startup_ocr_count = ocr_calls["count"]
            sessions = sidecar.run_action(SimpleNamespace(
                action="sessions", artifact_dir=directory, phone="", wechat="",
                verify_message="", remark_name="", remark_code="", sidecar_run_id="s-1",
                scan_id="scan-1",
            ))
            assert_true(sessions.get("ok") is True, json.dumps(sessions, ensure_ascii=False, default=str))
            visible_sessions = list(sessions.get("sessions") or [])
            assert_true(len(visible_sessions) == 1, f"real sessions parser did not expose one target: {visible_sessions}")
            visible_target = visible_sessions[0]
            assert_true(visible_target.get("reference_click_point"), f"session row omitted calibrated click X: {visible_target}")
            assert_true(normalize_calls["count"] == 1, "C2 sessions production entry re-normalized the window")
            assert_true(capture_calls["count"] == startup_capture_count + 1, "C2 sessions must add one business screenshot")
            assert_true(
                ocr_calls["count"] == startup_ocr_count + 2,
                f"C2 sessions OCR boundary count mismatch: startup={startup_ocr_count}, after={ocr_calls['count']}",
            )
            c2_clicks: list[dict[str, object]] = []

            def c2_physical_click(_hwnd, x, y, *, bounds, expected_snapshot_id="", **_kwargs):
                c2_clicks.append({
                    "point": [int(x), int(y)],
                    "bounds": list(bounds),
                    "layout_snapshot_id": str(expected_snapshot_id),
                })
                raise PhysicalClickReached("c2_session_row")

            sidecar.human_window_image_click = c2_physical_click
            try:
                sidecar.run_action(SimpleNamespace(
                    action="open-chat", artifact_dir=directory, phone="", wechat="",
                    verify_message="", remark_name="", remark_code="CJTEST01",
                    target="CJTEST01", session_key=str(visible_target.get("session_key") or ""),
                    target_mode="visible", visible_session_candidate=json.dumps(visible_target),
                    exact=False, sidecar_run_id="open-1", capture_initial_messages=False,
                ))
            except PhysicalClickReached:
                pass
            assert_true(len(c2_clicks) == 1, f"C2 public open-chat did not reach one physical row click: {c2_clicks}")
            assert_true(
                c2_clicks[0]["point"][0] == visible_target["reference_click_point"][0],
                f"C2 click X did not come from startup map: {c2_clicks[0]} vs {visible_target}",
            )
            send_shot, _ = sidecar.capture_wechat(101, artifact_dir=directory, label="c3_send_seed")
            send_snapshot = sidecar.layout_snapshot_for_image(send_shot) or {}
            input_state = sidecar.input_text_region_state(
                send_shot,
                [],
                geometry=sidecar.get_window_geometry(101),
            )
            expected_input = sidecar.win32_ocr_layout.map_reference_region_point(
                send_snapshot, "input_focus"
            )["image_point"]
            c3_clicks: list[list[int]] = []

            def c3_physical_click(_hwnd, x, y, **_kwargs):
                c3_clicks.append([int(x), int(y)])
                raise PhysicalClickReached("c3_input")

            sidecar.human_client_click = c3_physical_click
            c3_result: dict[str, object] = {}
            try:
                c3_result = sidecar.send_with_visual_input(
                    101,
                    "测试回复",
                    geometry=sidecar.get_window_geometry(101),
                    artifact_dir=directory,
                    before_input_region_seed={"input_region": input_state},
                )
            except PhysicalClickReached:
                pass
            assert_true(c3_clicks == [list(expected_input)], f"C3 input click did not use calibrated input map: {c3_clicks} vs {expected_input}; result={c3_result}; state={input_state}")
            c1_calibration = sidecar.run_action(SimpleNamespace(
                action="add-friend-entry-click-plan-windows",
                artifact_dir=directory,
                phone="17368746889",
                wechat="",
                verify_message="您好，我是车金张伟",
                remark_name="客户-CJ8K2P",
                remark_code="CJ8K2P",
                calibration_only=True,
                action_journal="",
            ))
            assert_true(c1_calibration.get("ok") is True, str(c1_calibration))
            c1_target = c1_calibration["before"]["planned_targets"][0]
            assert_true(c1_target.get("source") == "startup_calibration_region_map", str(c1_target))
    finally:
        for name, value in originals.items():
            setattr(sidecar, name, value)
        if windll_present:
            sidecar.ctypes.windll = original_windll
        else:
            delattr(sidecar.ctypes, "windll")
        sidecar.win32_ocr_engine.run_ocr_with_cache = original_ocr
    return 18


def check_no_v0922_runtime_bypass() -> int:
    sidecar_source = Path(sidecar.__file__).read_text(encoding="utf-8")
    planner_source = Path(window_action_planning.__file__).read_text(encoding="utf-8")
    assert_true("DEFAULT_SAFE_WINDOW_WIDTH = 980" not in sidecar_source, "980x860 policy remains")
    register_body = sidecar_source.split("def _register_layout_snapshot", 1)[1].split("def current_layout_snapshot", 1)[0]
    assert_true("build_structural_layout_regions(image)" not in register_body, "business frame still rebuilds full shell")
    assert_true("WECHAT_WIN32_OCR_WINDOW_WIDTH" not in planner_source, "window override path remains")
    assert_true("startup_calibration_region_map" in Path(sidecar.win32_ocr_add_friend_windows.__file__).read_text(encoding="utf-8"), "plus does not use startup map")
    assert_true("--window-policy" not in sidecar_source, "v0.9.22 window-policy parser remains")
    run_action_body = sidecar_source.split("def run_action", 1)[1].split("def use_passive_probe_mode", 1)[0]
    assert_true(run_action_body.count("normalize_wechat_window(") == 1, "business actions retain a normalization path")
    return 6


def check_dpi_awareness_is_verified_before_startup_calibration() -> int:
    class PerMonitorUser32:
        @staticmethod
        def SetProcessDpiAwarenessContext(_context):
            return True

        @staticmethod
        def GetThreadDpiAwarenessContext():
            return -4

        @staticmethod
        def GetAwarenessFromDpiAwarenessContext(_context):
            return 2

    class SystemAwareUser32(PerMonitorUser32):
        @staticmethod
        def GetAwarenessFromDpiAwarenessContext(_context):
            return 1

    verified = sidecar.configure_dpi_awareness(
        user32=PerMonitorUser32(),
        shcore=SimpleNamespace(),
    )
    assert_true(verified.get("per_monitor_aware") is True, str(verified))
    assert_true(verified.get("awareness") == 2, str(verified))
    unverified = sidecar.configure_dpi_awareness(
        user32=SystemAwareUser32(),
        shcore=SimpleNamespace(),
    )
    assert_true(unverified.get("per_monitor_aware") is False, str(unverified))
    assert_true(unverified.get("awareness_name") == "system_aware", str(unverified))

    originals = {
        "_WIN32_IMPORT_ERROR": sidecar._WIN32_IMPORT_ERROR,
        "ensure_dpi_awareness_status": sidecar.ensure_dpi_awareness_status,
        "ensure_visible_wechat_window": sidecar.ensure_visible_wechat_window,
    }
    window_probe_calls = {"count": 0}
    try:
        sidecar._WIN32_IMPORT_ERROR = ""
        sidecar.ensure_dpi_awareness_status = lambda: dict(unverified)
        sidecar.ensure_visible_wechat_window = lambda **_kwargs: (
            window_probe_calls.__setitem__("count", window_probe_calls["count"] + 1)
            or {}
        )
        result = sidecar.run_action(SimpleNamespace(
            action="normalize-window",
            artifact_dir="",
            phone="",
            wechat="",
            verify_message="",
            remark_name="",
            remark_code="",
        ))
    finally:
        for name, value in originals.items():
            setattr(sidecar, name, value)
    assert_true(result.get("ok") is False, str(result))
    assert_true(
        result.get("error_code") == window_layout.ERROR_STARTUP_CALIBRATION_FAILED,
        str(result),
    )
    assert_true(result.get("no_clicks_performed") is True, str(result))
    assert_true(window_probe_calls["count"] == 0, str(result))
    return 8


def main() -> int:
    checks = 0
    checks += check_window_profiles()
    checks += check_calibration_and_mapping()
    checks += check_public_normalize_entry()
    checks += check_no_v0922_runtime_bypass()
    checks += check_dpi_awareness_is_verified_before_startup_calibration()
    print(f"v0.9.29 startup calibration checks passed: {checks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
