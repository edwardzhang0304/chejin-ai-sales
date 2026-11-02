"""Between-task moves through native C1 and the actual diagnostic ZIP exporter."""

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import (
    hook_c1,
    hook_c1_flow as flow,
)
from test_c1_evidence_chain import scene, outcome, original, TASK, FormalScene
from test_failure_evidence_boundaries import home
from test_hook_search_startup_cache import calibration


def test_malformed_origin_is_recorded_as_rejection():
    from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr.c1_layout import (
        layout_comparison,
    )

    key = {
        "hwnd": 1,
        "pid": 1,
        "dpi": 96,
        "origin": None,
        "geometry": {"width": 1060, "height": 740, "screen_left": 0, "screen_top": 0},
    }
    decision = layout_comparison(key, key)
    assert decision["accepted"] is False
    assert decision["reason"] == "layout_identity_or_shape_changed"


@pytest.fixture(autouse=True)
def partitioned_synthetic_header(monkeypatch):
    # Old fixture predates startup sidebar partitioning. Supply native-shaped
    # synthetic navigation/separator pixels, keeping real calibration enabled.
    from apps.wechat_ai_customer_service.tests.test_search_control_icons import header

    original_grab, original_ocr = FormalScene.grab, FormalScene.ocr

    def grab(self, bbox):
        image = original_grab(self, bbox)
        if image.info["scene_label"] == "header":
            image = header().image.crop((0, 0, image.width, image.height))
            image.info["scene_label"] = "header"
        elif image.info["scene_label"] == "lookup":
            image.paste(
                header(query="13800000003").image.crop((0, 0, image.width, 90)), (0, 0)
            )
        return image

    def ocr(self, image):
        if image.info.get("scene_label") == "header":
            self.ocr_calls.append("header")
            return []
        rows = original_ocr(self, image)
        if image.info.get("scene_label") == "lookup":
            rows[0].update(left=91, right=191, top=29, bottom=42)
        return rows

    monkeypatch.setattr(FormalScene, "grab", grab)
    monkeypatch.setattr(FormalScene, "ocr", ocr)


def calibrate_complete_geometry(s):
    # The original fixtures omit Win32 client edges; use a full native-shaped
    # rectangle and establish its cache through the real startup entry.
    g = s.boundary.client
    g.update(
        left=0,
        top=0,
        right=g["width"],
        bottom=g["height"],
        screen_right=g["screen_left"] + g["width"],
        screen_bottom=g["screen_top"] + g["height"],
    )
    gateway = SimpleNamespace(
        _locked=lambda *a: nullcontext(SimpleNamespace(raise_if_lost=lambda: None)),
        _history_identity=lambda *a: None,
    )
    with patch.object(
        hook_c1, "prepare_window", return_value={"ok": True}
    ), patch.object(s.native, "_foreground_ready", return_value=True):
        s.native.startup(SimpleNamespace(pid=99), gateway, {})
    s.grabs.clear()
    s.ocr_calls.clear()


def move(s, dx, dy):
    for name, delta in [("left", dx), ("right", dx), ("top", dy), ("bottom", dy)]:
        if name in s.boundary.geom:
            s.boundary.geom[name] += delta
        s.boundary.client["screen_" + name] += delta


@pytest.mark.parametrize("dx,dy", [(1, 0), (0, -1), (3, 3), (-3, -3)])
def test_between_tasks_move_finishes_without_relocation_and_exports_basis(
    scene, monkeypatch, dx, dy
):
    s = scene()
    calibrate_complete_geometry(s)
    before = copy.deepcopy(s.native._search_control_cache)
    move(s, dx, dy)
    with patch.object(
        flow, "search_control_bounds", side_effect=AssertionError("relocated")
    ):
        result = s.execute(monkeypatch)
    assert result["status"] == "not_found", json.dumps(result)
    assert result["cleanup"] == "search_empty_verified"
    assert s.grabs == s.ocr_calls == ["header", "lookup", "card", "header"]
    assert s.native._search_control_cache["bounds"] == before["bounds"]
    assert s.native._search_control_cache["regions"] == before["regions"]
    saved = outcome(s.export())
    assert saved["layout_reuse"]["expected_key"] == json.loads(
        json.dumps(before["key"])
    )
    assert saved["layout_reuse"]["observed_key"]["origin"] == [12 + dx, 12 + dy]
    assert saved["layout_reuse"]["ocr_calls_added"] == 0
    assert not (original(s) / "submit-attempt.json").exists()


@pytest.mark.parametrize(
    "change,reason",
    [
        ("size", "key_mismatch"),
        ("dpi", "key_mismatch"),
        ("hwnd", "key_mismatch"),
        ("pid", "key_mismatch"),
        ("missing", "key_mismatch"),
        ("regions", "regions_missing_or_invalid"),
        ("bounds", "search_bounds_missing_or_invalid"),
    ],
)
def test_cache_failure_exports_expected_observed_and_keeps_attempt(
    scene, monkeypatch, change, reason
):
    s = scene()
    calibrate_complete_geometry(s)
    before = copy.deepcopy(s.native._search_control_cache)
    if change == "size":
        s.boundary.client["width"] += 4
        s.boundary.client["right"] += 4
        s.boundary.client["screen_right"] += 4
    elif change == "dpi":
        # Cached scale differs from actual startup observation; no scale
        # tolerance should accidentally be treated as a pure translation.
        s.native._search_control_cache["key"]["dpi"] = 1.001
    elif change in ("hwnd", "pid"):
        s.native._search_control_cache["key"][change] += 1
    elif change == "missing":
        s.native._search_control_cache.clear()
    elif change == "regions":
        s.native._search_control_cache["regions"].pop("lookup")
    else:
        s.native._search_control_cache["bounds"] = [0, 0, 0, 0]
    result = s.execute(monkeypatch)
    assert result["error_code"] == "SEARCH_STARTUP_LAYOUT_INVALIDATED"
    assert not result["submit_attempted"] and not s.native.startup_layout_ready()
    assert s.grabs == s.ocr_calls == []
    s.ops.win32api.mouse_event.assert_not_called()
    saved = outcome(s.export())["guard_failures"][-1]["observed"]
    assert reason in saved["reasons"]
    assert saved["observed_key"]["hwnd"] == 11
    assert (
        saved["expected_key"] is None
        if change == "missing"
        else saved["expected_key"] is not None
    )
    if change == "size":
        assert "geometry.width" in saved["changed_fields"]
        assert saved["expected_key"]["geometry"] == before["key"]["geometry"]
    receipt = (original(s) / "outcome.json").read_bytes()
    with pytest.raises(ValueError, match="PREVIOUS_ATTEMPT"):
        s.execute(monkeypatch)
    assert (original(s) / "outcome.json").read_bytes() == receipt


def test_move_during_capture_after_translation_still_stops(scene, monkeypatch):
    s = scene()
    calibrate_complete_geometry(s)
    move(s, 2, 2)
    grab = s.grab

    def interrupted(bbox):
        image = grab(bbox)
        move(s, 1, 0)
        return image

    s.ops.try_image_grab.side_effect = interrupted
    result = s.execute(monkeypatch)
    assert result["error_code"] == "CAPTURE_GEOMETRY_CHANGED"
    s.ops.win32api.mouse_event.assert_not_called()
    assert not s.native.startup_layout_ready()
    saved = outcome(s.export())
    assert saved["layout_reuse"]["observed_key"]["origin"] == [14, 14]
    failure = saved["guard_failures"][-1]
    assert failure["expected"]["startup_layout_key"]["origin"] == [12, 12]
    assert failure["observed"]["geometry"]["screen_left"] == 15


@pytest.mark.parametrize("axis", ["width", "height"])
@pytest.mark.parametrize("delta", [-3, -1, 1, 3])
def test_small_resize_reuses_immutable_map_without_extra_capture(
    scene, monkeypatch, axis, delta
):
    s = scene()
    calibrate_complete_geometry(s)
    before = copy.deepcopy(s.native._search_control_cache)
    g = s.boundary.client
    edge = "right" if axis == "width" else "bottom"
    g[axis] += delta
    g[edge] += delta
    g["screen_" + edge] += delta
    result = s.execute(monkeypatch)
    assert result["status"] == "not_found", json.dumps(result)
    assert s.native._search_control_cache == before
    assert s.grabs == s.ocr_calls == ["header", "lookup", "card", "header"]
    assert outcome(s.export())["layout_reuse"]["deltas"][axis] == delta


def test_tolerance_does_not_ratchet_and_real_moves_stop(calibration, monkeypatch):
    native, gateway, process, state, ops, root = calibration
    monkeypatch.setattr(hook_c1, "prepare_window", Mock(return_value={"ok": True}))
    native.startup(process, gateway, {})
    baseline = copy.deepcopy(native._search_control_cache)
    original_geometry = ops.get_window_client_geometry(11)
    for offset in (1, 2, 3, 4):
        changed = {
            **original_geometry,
            "screen_left": original_geometry["screen_left"] + offset,
        }
        monkeypatch.setattr(ops, "get_window_client_geometry", lambda _: changed)
        ui = native.ui(process, {"task_id": "drift"}, Mock())
        if offset <= 3:
            ui.verified_empty_search()
            assert native._search_control_cache == baseline
        else:
            with pytest.raises(flow.FlowStop, match="LAYOUT_INVALIDATED"):
                ui.verified_empty_search()
            assert ui.guard_failures[-1]["observed"]["deltas"]["screen_left"] == 4
            assert not native._search_control_cache
