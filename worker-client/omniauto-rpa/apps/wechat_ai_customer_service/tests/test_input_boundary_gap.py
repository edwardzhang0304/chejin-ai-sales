"""Bounded edge tolerance and navigation isolation, through real registration."""
import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from test_dynamic_composer import install_desktop, register, sidecar, window_layout


def calibration():
    return {
        "executable": True, "confidence": 0.868, "hwnd": 525856,
        "process_id": 11712, "dpi_scale": 1.0, "image_width": 784,
        "image_height": 844, "window_rect": [12, 12, 812, 864],
        "client_screen_origin": [20, 12], "calibration_id": "test-calibration",
        "left_nav_bounds": [0, 0, 60, 844],
        "sidebar_bounds": [60, 0, 300, 844],
        "sidebar_header_bounds": [60, 0, 300, 94],
        "session_list_bounds": [60, 94, 300, 844],
        "chat_header_bounds": [300, 0, 784, 81],
        "message_viewport_bounds": [300, 81, 784, 700],
        "input_bounds": [304, 705, 707, 800],
        "toolbar_bounds": [300, 800, 784, 844],
    }


def frame(*, gap=0, boundary=True):
    image = Image.new("RGB", (784, 844), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    if boundary:
        draw.line((311, 701, 768, 701), fill=(234, 234, 234))
        if gap:
            draw.line((531, 699, 530 + gap, 699), fill=(236, 236, 237))
    return image


@pytest.mark.parametrize("gap", [0, 1, 2, 3, 4])
def test_small_gap_keeps_real_boundary(gap):
    result = window_layout.measure_business_input_regions(frame(gap=gap), calibration())
    assert result["ok"], result
    assert result["regions"]["message_viewport_bounds"][3] == 700


@pytest.mark.parametrize("scale,gap,accepted", [(1, 4, True), (1, 5, False), (1.5, 6, True), (1.5, 7, False), (2, 8, True), (2, 9, False)])
def test_gap_limit_scales_with_dpi(scale, gap, accepted):
    assert window_layout._continuous_horizontal_edge(
        frame(gap=gap), left=300, right=784, y=700, dpi_scale=scale) is accepted


def test_many_tiny_holes_cannot_exceed_total_budget():
    image = frame()
    for x in (430, 535, 640):
        ImageDraw.Draw(image).line((x, 699, x+3, 699), fill=(234, 234, 234))
    assert not window_layout._continuous_horizontal_edge(image, left=300, right=784, y=700)


def test_sessions_to_physical_navigation_and_strict_target_guard(monkeypatch, tmp_path):
    """Public scan -> real planner/mapping/click/guard, external OS/OCR doubles."""
    c = calibration()
    geometry = install_desktop(monkeypatch, tmp_path, c)
    desktop = {"opened": False, "clicks": []}
    def capture(hwnd, **kwargs):
        image = frame(boundary=desktop["opened"])
        register(image, c)
        return image, "synthetic-desktop.png"
    def ocr(image):
        def item(text, left, top, right, bottom):
            return dict(text=text, left=left, top=top, right=right, bottom=bottom,
                        center_x=(left+right)/2, center_y=(top+bottom)/2, confidence=.99)
        items = [item("搜索", 80, 42, 115, 60), item("CJTEST01", 113, 104, 185, 121)]
        if desktop["opened"] or desktop.get("force_title"):
            items += [item("CJTEST01", 325, 43, 415, 62), item("发送", 727, 807, 752, 823)]
        return items
    def click(x, y, **kwargs):
        desktop["clicks"].append([x, y])
        assert 80 <= x <= 320 and y >= 106
        desktop["opened"] = True
        return {"ok": True}
    monkeypatch.setattr(sidecar, "capture_wechat", capture)
    monkeypatch.setattr(sidecar, "run_ocr", ocr)
    monkeypatch.setattr(sidecar, "human_screen_click_in_bounds", click)
    monkeypatch.setattr(sidecar, "ensure_left_button_released", lambda: None)
    monkeypatch.setattr(sidecar, "humanized_action_sleep", lambda *args: None)
    monkeypatch.setenv("CHEJIN_C3_PRE_SEND_ROI_REUSE_ENABLED", "0")
    scan = sidecar.sessions_payload(c["hwnd"], {"ok": True})
    assert scan["ok"] and scan["sessions"], scan
    session = next(s for s in scan["sessions"] if s["name"] == "CJTEST01")
    assert sidecar.open_chat(c["hwnd"], "CJTEST01", exact=True,
        session_key=session["session_key"]), json.dumps(sidecar._LAST_OPEN_CHAT_TIMING, ensure_ascii=False)
    assert len(desktop["clicks"]) == 1
    guard = sidecar.validate_active_send_target(c["hwnd"], "CJTEST01", exact=True)
    assert sidecar.c2_target_activation_confirmed(guard), guard
    assert sidecar.current_layout_snapshot(c["hwnd"])["message_viewport_bounds"][3] == 700
    desktop["opened"] = False
    desktop["force_title"] = True  # correct title alone cannot authorize a missing input boundary
    guard = sidecar.validate_active_send_target(c["hwnd"], "CJTEST01", exact=True)
    assert not sidecar.c2_target_activation_confirmed(guard)
    assert not sidecar.current_layout_snapshot(c["hwnd"])["valid"]
    assert len(desktop["clicks"]) == 1


@pytest.mark.parametrize("kind", ["long_gap", "many_gaps", "missing", "ambiguous"])
def test_invalid_edges_stay_blocked(kind):
    image = frame(gap=12 if kind == "long_gap" else 0, boundary=kind != "missing")
    draw = ImageDraw.Draw(image)
    if kind == "many_gaps":
        for x in range(315, 767, 4):
            draw.point((x, 699), fill=(234, 234, 234))
    if kind == "ambiguous":
        draw.line((311, 601, 768, 601), fill=(234, 234, 234))
    if kind in {"long_gap", "many_gaps"}:
        assert not window_layout._continuous_horizontal_edge(image, left=300, right=784, y=700)
    else:
        assert not window_layout.measure_business_input_regions(image, calibration())["ok"]


def test_navigation_can_move_but_cannot_type_with_missing_input(monkeypatch, tmp_path):
    c = calibration()
    install_desktop(monkeypatch, tmp_path, c)
    image = frame(boundary=False)
    full = register(image, c)
    assert not full["valid"]
    nav = sidecar.navigation_layout_snapshot_for_image(image)
    assert nav["valid"] and nav["frame_id"] == full["frame_id"]
    mapped, failure = sidecar._map_window_image_target(
        c["hwnd"], 150, 120, bounds=[70, 100, 280, 150],
        expected_snapshot_id=nav["layout_snapshot_id"])
    assert not failure and mapped["screen_point"] == [170, 132]
    _, failure = sidecar._map_window_image_target(
        c["hwnd"], 500, 740, bounds=c["input_bounds"],
        expected_snapshot_id=nav["layout_snapshot_id"])
    assert failure["error_code"] == "WECHAT_UI_COORDINATE_MAPPING_INVALID"
    with pytest.raises(window_layout.LayoutSnapshotError):
        window_layout.required_region(full, "input_bounds")
    sidecar.invalidate_layout_snapshot(c["hwnd"], reason="physical_action")
    _, failure = sidecar._map_window_image_target(
        c["hwnd"], 150, 120, bounds=[70, 100, 280, 150],
        expected_snapshot_id=nav["layout_snapshot_id"])
    assert failure["error_code"] == "WECHAT_UI_LAYOUT_STALE"


def test_navigation_rejects_old_frame_and_stale_calibration(monkeypatch, tmp_path):
    c = calibration()
    install_desktop(monkeypatch, tmp_path, c)
    old_image = frame(boundary=False)
    register(old_image, c)
    old = sidecar.navigation_layout_snapshot_for_image(old_image)
    register(frame(), c)
    _, failure = sidecar._map_window_image_target(
        c["hwnd"], 150, 120, bounds=[70, 100, 280, 150],
        expected_snapshot_id=old["layout_snapshot_id"])
    assert failure["error_code"] == "WECHAT_UI_LAYOUT_STALE"
    monkeypatch.setattr(sidecar, "window_dpi_scale", lambda _: 1.25)
    image = frame()
    register(image, c)
    assert not sidecar.navigation_layout_snapshot_for_image(image)["valid"]


def test_add_friend_entry_uses_same_bounded_navigation_capability(monkeypatch, tmp_path):
    c = calibration()
    c["anchors"] = [{"name": "sidebar_search_anchor", "bounds": [75, 46, 128, 68]}]
    geometry = install_desktop(monkeypatch, tmp_path, c)
    normal_image = frame()
    normal = register(normal_image, c)
    baseline = sidecar.add_friend_plus_entry_target(geometry, normal_image.size, [],
        screenshot=normal_image, layout_snapshot=normal)
    assert baseline["executable"]
    image = frame(boundary=False)
    full = register(image, c)
    navigation = sidecar.finalize_add_friend_entry_layout_snapshot(image, [])
    target = sidecar.add_friend_plus_entry_target(geometry, image.size, [], screenshot=image,
        layout_snapshot=navigation)
    assert not full["valid"] and target["executable"]
    assert target["point"] == baseline["point"]  # existing per-host fallback; no new plus algorithm
    mapped, failure = sidecar._map_window_image_target(c["hwnd"], *target["point"],
        bounds=target["click_bounds"], expected_snapshot_id=target["layout_snapshot_id"])
    assert mapped and not failure
    assert target["metadata"]["startup_calibration_evidence"] == full["startup_calibration_evidence"]


def test_all_original_delay_incident_frames(monkeypatch, tmp_path):
    root = Path(os.environ.get("CHEJIN_DELAY_INCIDENT", ""))
    paths = sorted(p for p in root.glob("artifacts/wechat_c2/sessions/20260917_*/sessions_*.png")
                   if p.parent.name >= "20260917_172000")
    if not paths:
        pytest.skip("private incident originals required")
    cal_path = root / "artifacts/startup_layout_calibration/frame-4e1dc58c62befa850313-c0303452/calibration.json"
    c = json.loads(cal_path.read_text())
    install_desktop(monkeypatch, tmp_path, c)
    for path in paths:
        image = Image.open(path).convert("RGB")
        result = register(image, c, path)
        assert result["valid"], (path.name, result["layout_builder"])
        assert result["message_viewport_bounds"][3] == 700
    assert len(paths) == 144
