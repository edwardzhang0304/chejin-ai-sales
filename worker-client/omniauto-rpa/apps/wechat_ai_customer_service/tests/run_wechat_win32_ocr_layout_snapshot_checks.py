"""Focused checks for per-frame layout snapshots and coordinate mapping."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr import window_layout  # noqa: E402


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class SyntheticImage:
    def __init__(self, width: int, height: int, verticals: tuple[int, ...], horizontals: tuple[int, ...]) -> None:
        self.size = (width, height)
        self.verticals = verticals
        self.horizontals = horizontals

    def getpixel(self, point: tuple[int, int]) -> tuple[int, int, int]:
        x, y = point
        if any(x == boundary - 1 for boundary in self.verticals):
            return (20, 20, 20)
        if any(x == boundary + 1 for boundary in self.verticals):
            return (230, 230, 230)
        if any(y == boundary - 1 for boundary in self.horizontals):
            return (20, 20, 20)
        if any(y == boundary + 1 for boundary in self.horizontals):
            return (230, 230, 230)
        return (120, 120, 120)


def structural_layout(width: int, height: int) -> dict:
    image = SyntheticImage(
        width,
        height,
        (int(width * 0.12), int(width * 0.375)),
        (int(height * 0.12), int(height * 0.80), int(height * 0.84)),
    )
    result = window_layout.build_structural_layout_regions(image)
    assert_true(result.get("ok"), f"production layout builder rejected test frame: {result}")
    return result


def snapshot(
    *,
    hwnd: int,
    image_size: tuple[int, int],
    capture_origin: list[int] | None,
    frame_id: str,
    executable: bool = True,
    capture_mode: str = window_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
    dpi_scale: float = 1.25,
) -> dict:
    width, height = image_size
    layout = structural_layout(width, height)
    return window_layout.build_layout_snapshot(
        hwnd=hwnd,
        frame_id=frame_id,
        capture_mode=capture_mode,
        image_size=image_size,
        capture_screen_origin=capture_origin,
        window_rect=[capture_origin[0], capture_origin[1], capture_origin[0] + width, capture_origin[1] + height]
        if capture_origin
        else [0, 0, width, height],
        client_rect=[0, 0, width - 18, height - 40],
        client_screen_origin=[capture_origin[0] + 9, capture_origin[1] + 32] if capture_origin else None,
        dpi_scale=dpi_scale,
        regions=layout["regions"],
        anchors=layout["anchors"],
        confidence=layout["confidence"],
        conflicts=layout["conflicts"],
        executable=executable,
    )


def test_different_capture_origins_produce_different_screen_points() -> None:
    first = snapshot(hwnd=101, image_size=(1920, 1080), capture_origin=[100, 200], frame_id="frame-1920")
    second = snapshot(hwnd=101, image_size=(2560, 1440), capture_origin=[800, 40], frame_id="frame-2560")
    first_point = window_layout.image_point_to_screen(first, [400, 300])
    second_point = window_layout.image_point_to_screen(second, [400, 300])
    assert_true(first_point == [500, 500], f"unexpected first screen point: {first_point}")
    assert_true(second_point == [1200, 340], f"unexpected second screen point: {second_point}")
    assert_true(first_point != second_point, "different screenshot origins must not collapse to one coordinate")


def test_client_conversion_uses_client_screen_origin() -> None:
    current = snapshot(hwnd=102, image_size=(1920, 1080), capture_origin=[300, 120], frame_id="frame-client")
    screen_point = window_layout.image_point_to_screen(current, [600, 400])
    client_point = window_layout.screen_point_to_client(current, screen_point)
    assert_true(screen_point == [900, 520], f"screen mapping mismatch: {screen_point}")
    assert_true(client_point == [591, 368], f"client mapping mismatch: {client_point}")


def test_new_frame_invalidates_previous_snapshot() -> None:
    store = window_layout.LayoutSnapshotStore()
    first = snapshot(hwnd=103, image_size=(1920, 1080), capture_origin=[0, 0], frame_id="frame-old")
    second = snapshot(hwnd=103, image_size=(1920, 1080), capture_origin=[80, 30], frame_id="frame-new")
    store.put(first)
    store.put(second)
    store.invalidate(first["layout_snapshot_id"], reason="new_frame_captured")
    old = store.get(first["layout_snapshot_id"])
    new = store.get(second["layout_snapshot_id"])
    assert_true(bool(old and old.get("invalidated")), "old frame must be invalidated")
    assert_true(bool(new and not new.get("invalidated")), "new frame must remain executable")


def test_geometry_or_image_size_change_makes_snapshot_stale() -> None:
    current = snapshot(hwnd=104, image_size=(1920, 1080), capture_origin=[0, 0], frame_id="frame-stale")
    same = window_layout.snapshot_matches_current(
        current,
        hwnd=104,
        window_rect=[0, 0, 1920, 1080],
        client_rect=[0, 0, 1902, 1040],
        dpi_scale=1.25,
        image_size=(1920, 1080),
    )
    moved = window_layout.snapshot_matches_current(
        current,
        hwnd=104,
        window_rect=[40, 0, 1960, 1080],
        client_rect=[0, 0, 1902, 1040],
        dpi_scale=1.25,
        image_size=(1920, 1080),
    )
    resized = window_layout.snapshot_matches_current(
        current,
        hwnd=104,
        window_rect=[0, 0, 2560, 1440],
        client_rect=[0, 0, 2542, 1400],
        dpi_scale=1.25,
        image_size=(2560, 1440),
    )
    assert_true(same, "unchanged window geometry should keep the snapshot current")
    assert_true(not moved, "window movement must stale the snapshot")
    assert_true(not resized, "window or screenshot resize must stale the snapshot")


def test_unknown_capture_origin_cannot_become_physical_click() -> None:
    current = snapshot(
        hwnd=105,
        image_size=(1920, 1080),
        capture_origin=None,
        frame_id="frame-print-window",
        capture_mode=window_layout.CAPTURE_MODE_PRINT_WINDOW,
    )
    assert_true(not current["clickable"], "PrintWindow or unknown-origin frame must not be clickable")
    try:
        window_layout.image_point_to_screen(current, [500, 300])
    except window_layout.LayoutSnapshotError as exc:
        assert_true(exc.code == "WECHAT_UI_COORDINATE_MAPPING_INVALID", f"wrong mapping error: {exc.code}")
    else:
        raise AssertionError("unknown capture origin unexpectedly produced a screen point")


def test_structural_regions_follow_each_image_size() -> None:
    first = window_layout.build_structural_layout_regions(
        SyntheticImage(1920, 1080, (230, 720), (130, 860, 900))
    )
    second = window_layout.build_structural_layout_regions(
        SyntheticImage(2560, 1440, (300, 960), (170, 1160, 1210))
    )
    assert_true(first.get("ok"), f"first structural layout should resolve: {first}")
    assert_true(second.get("ok"), f"second structural layout should resolve: {second}")
    assert_true(
        first["regions"]["sidebar_bounds"] != second["regions"]["sidebar_bounds"],
        "layout regions must be derived from each current image",
    )
    for result in (first, second):
        width, height = result["regions"]["input_bounds"][2], result["regions"]["input_bounds"][3]
        for name, bounds in result["regions"].items():
            assert_true(bounds[0] >= 0 and bounds[1] >= 0, f"{name} has negative origin: {bounds}")
            assert_true(bounds[2] <= width and bounds[3] <= height, f"{name} exceeds image: {bounds}")


def test_chat_panel_vertical_does_not_replace_first_sidebar_separator() -> None:
    result = window_layout.build_structural_layout_regions(
        SyntheticImage(
            1920,
            1080,
            # 230/720 are the real nav/sidebar separators.  A similarly strong
            # full-height edge at 900 must not silently replace the sidebar.
            (230, 720, 900),
            (130, 860, 900),
        )
    )
    assert_true(result.get("ok") is True, f"chat-panel edge blocked the real layout: {result}")
    assert_true(
        list((result.get("regions") or {}).get("sidebar_bounds") or [])[2] == 720,
        f"chat-panel edge replaced the first sidebar separator: {result}",
    )


def test_distinct_navigation_layouts_fail_closed() -> None:
    result = window_layout.build_structural_layout_regions(
        SyntheticImage(1920, 1080, (120, 230, 550, 720), (130, 860, 900))
    )
    assert_true(result.get("ok") is False, f"ambiguous nav layout became executable: {result}")
    assert_true(
        "sidebar_boundary_pair_ambiguous" in (result.get("conflicts") or []),
        f"wrong ambiguity evidence: {result}",
    )


def test_required_resolution_and_dpi_matrix_uses_current_frame_geometry() -> None:
    cases = (
        ((1920, 1080), 1.00, [0, 0]),
        ((2560, 1440), 1.25, [240, 80]),
        ((3840, 2160), 1.50, [640, 180]),
    )
    snapshots = []
    for index, (image_size, dpi_scale, origin) in enumerate(cases, start=1):
        current = snapshot(
            hwnd=200 + index,
            image_size=image_size,
            capture_origin=origin,
            frame_id=f"matrix-{index}",
            dpi_scale=dpi_scale,
        )
        assert_true(current.get("executable"), f"matrix layout must be executable: {current}")
        message_bounds = window_layout.required_region(current, "message_viewport_bounds")
        point = [(message_bounds[0] + message_bounds[2]) // 2, (message_bounds[1] + message_bounds[3]) // 2]
        mapped = window_layout.transform_target_to_screen(current, point=point, bounds=message_bounds)
        assert_true(mapped["screen_point"] == [origin[0] + point[0], origin[1] + point[1]], f"matrix mapping mismatch: {mapped}")
        snapshots.append(current)
    assert_true(len({item["geometry_signature"] for item in snapshots}) == 3, "each DPI/display frame needs its own geometry signature")


def test_popup_snapshot_has_its_own_surface_and_cannot_reuse_main_regions() -> None:
    width, height = 460, 620
    popup = window_layout.build_layout_snapshot(
        hwnd=301,
        frame_id="popup-frame",
        capture_mode=window_layout.CAPTURE_MODE_WINDOW_VISIBLE_SCREEN,
        image_size=(width, height),
        capture_screen_origin=[900, 120],
        window_rect=[900, 120, 900 + width, 120 + height],
        client_rect=[0, 0, width, height],
        client_screen_origin=[900, 120],
        dpi_scale=1.25,
        regions={"surface_bounds": [0, 0, width, height]},
        anchors=[{"name": "popup_window_bounds", "confidence": 1.0}],
        confidence=1.0,
        executable=True,
        surface_kind="popup",
        required_region_names=window_layout.POPUP_LAYOUT_REGION_NAMES,
    )
    assert_true(popup.get("executable"), f"popup surface should be independently executable: {popup}")
    assert_true(window_layout.required_region(popup, "surface_bounds") == [0, 0, width, height], f"popup surface missing: {popup}")
    try:
        window_layout.required_region(popup, "message_viewport_bounds")
    except window_layout.LayoutSnapshotError:
        pass
    else:
        raise AssertionError("popup unexpectedly inherited the main WeChat message viewport")


def test_production_has_no_fixed_geometry_or_unbounded_click_bypass() -> None:
    """Prevent diagnostic 1920/fixed helpers from returning to live actions."""

    production_root = PROJECT_ROOT / "apps" / "wechat_ai_customer_service"
    sidecar_file = production_root / "adapters" / "wechat_win32_ocr_sidecar.py"
    forbidden_fixed_symbols = {
        "SEARCH_BOX_REL",
        "SESSION_CLICK_X",
        "CHAT_HEADER_MAX_Y",
        "session_split_x",
        "default_session_split_x",
        "default_search_box_point",
        "chat_header_cutoff_y",
        "active_chat_title_cutoff_y",
        "active_chat_title_top_cutoff_y",
        "active_chat_title_left_x",
        "active_chat_title_right_x",
        "active_chat_title_top_y",
        "active_chat_title_bottom_y",
        "search_box_point_for_geometry",
        "session_click_x_for_geometry",
        "input_text_region_bounds",
        "rect_in_input_area",
        "rect_in_input_toolbar",
        "_spread_points_in_rect",
        "input_click_candidate_points",
        "send_click_candidate_points",
        "calculate_send_points",
        "plus_entry_safe_bounds",
        "plus_entry_layout_regions",
        "find_sidebar_search_anchor_item",
        "windows_1080p_reference_plus_point",
        "windows_plus_point",
        "invite_form_geometry_targets",
        "add_friend_plus_entry_safe_bounds",
        "add_friend_plus_button_point_for_geometry",
        "add_friend_windows_plus_button_point_for_geometry",
        "add_friend_windows_1080p_reference_plus_button_point_for_geometry",
        "diagnostic_windows_1080p_reference_geometry",
        "diagnostic_windows_current_geometry",
        "diagnostic_sidebar_search_ocr_anchor_offset",
    }
    fixed_symbol_residue: list[str] = []
    unbounded_clicks: list[str] = []
    raw_action_bypasses: list[str] = []
    snapshot_unbound_actions: list[str] = []
    snapshot_bound_action_names = {
        "human_client_click",
        "human_window_image_click",
        "human_window_image_click_in_bounds",
        "human_window_image_right_click_in_bounds",
        "human_window_image_hover",
    }
    allowed_raw_actions = {
        "human_screen_hover": {"human_window_image_hover"},
        "human_screen_click_in_bounds": {
            "human_window_image_click",
            "human_window_image_click_in_bounds",
        },
    }

    for path in sorted(production_root.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function_stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                function_stack.append(node.name)
                if node.name in forbidden_fixed_symbols:
                    fixed_symbol_residue.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:definition:{node.name}"
                    )
                self.generic_visit(node)
                function_stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    imported_name = alias.asname or alias.name.rsplit(".", 1)[-1]
                    if imported_name in forbidden_fixed_symbols:
                        fixed_symbol_residue.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:import:{imported_name}"
                        )
                self.generic_visit(node)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                for alias in node.names:
                    imported_name = alias.asname or alias.name
                    if alias.name in forbidden_fixed_symbols or imported_name in forbidden_fixed_symbols:
                        fixed_symbol_residue.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:import:{alias.name}"
                        )
                self.generic_visit(node)

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Store) and node.id in forbidden_fixed_symbols:
                    fixed_symbol_residue.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:assignment:{node.id}"
                    )
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                call_name = ""
                if isinstance(node.func, ast.Name):
                    call_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr
                current_function = function_stack[-1] if function_stack else "<module>"
                if call_name in forbidden_fixed_symbols:
                    fixed_symbol_residue.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:call:{call_name}"
                    )
                if path == sidecar_file and call_name == "human_client_click":
                    keyword_names = {item.arg for item in node.keywords if item.arg}
                    if not {"bounds", "expected_snapshot_id"}.issubset(keyword_names):
                        unbounded_clicks.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{current_function}"
                        )
                if call_name in snapshot_bound_action_names:
                    keyword_names = {item.arg for item in node.keywords if item.arg}
                    if "expected_snapshot_id" not in keyword_names:
                        snapshot_unbound_actions.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{current_function}->{call_name}"
                        )
                if path == sidecar_file and call_name in allowed_raw_actions:
                    if current_function not in allowed_raw_actions[call_name]:
                        raw_action_bypasses.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{current_function}->{call_name}"
                        )
                if path == sidecar_file and call_name == "human_screen_click":
                    raw_action_bypasses.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{current_function}->{call_name}"
                    )
                self.generic_visit(node)

        Visitor().visit(tree)

    assert_true(not fixed_symbol_residue, f"fixed coordinate symbol remains in production: {fixed_symbol_residue}")
    assert_true(not unbounded_clicks, f"client click lacks snapshot/bounds proof: {unbounded_clicks}")
    assert_true(not snapshot_unbound_actions, f"UI action lacks exact frame snapshot proof: {snapshot_unbound_actions}")
    assert_true(not raw_action_bypasses, f"screen-space action bypassed the layout converter: {raw_action_bypasses}")

    worker_vision_file = PROJECT_ROOT.parent / "chejin_worker_client" / "omniauto_vision.py"
    worker_vision_source = worker_vision_file.read_text(encoding="utf-8")
    assert_true(
        "human_screen_click" not in worker_vision_source,
        "Worker Vision bypassed the layout converter with a raw screen click",
    )


def test_window_policy_is_owned_only_by_sidecar() -> None:
    worker_root = PROJECT_ROOT.parent
    policy_token = "WECHAT_WIN32_OCR_WINDOW_FIXED_ORIGIN"
    allowed = {
        PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr_sidecar.py",
        PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "admin_backend" / "services" / "rpa_acceptance_report.py",
    }
    owners = []
    for path in sorted(worker_root.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        if policy_token in path.read_text(encoding="utf-8", errors="ignore"):
            owners.append(path)
    unexpected = [str(path.relative_to(worker_root)) for path in owners if path not in allowed]
    assert_true(not unexpected, f"multiple production modules own the WeChat window policy: {unexpected}")

    retired_compatibility_tokens = {
        "WECHAT_WIN32_OCR_DYNAMIC_LAYOUT_ENABLED",
        "WECHAT_WIN32_OCR_LEGACY_DEVICE_PROFILE",
    }
    retired_compatibility_owners: list[str] = []
    retired_switch_owners: list[Path] = []
    for path in sorted(worker_root.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        production_source = path.read_text(encoding="utf-8", errors="ignore")
        for token in retired_compatibility_tokens:
            if token in production_source:
                retired_compatibility_owners.append(f"{path.relative_to(worker_root)}:{token}")
        if "WECHAT_WIN32_OCR_WINDOW_NORMALIZE" in production_source:
            retired_switch_owners.append(path)
    assert_true(
        not retired_compatibility_owners,
        f"retired dynamic-layout compatibility returned: {retired_compatibility_owners}",
    )
    assert_true(not retired_switch_owners, f"retired normalization bypass switch returned: {retired_switch_owners}")


def test_layout_tests_invoke_real_production_builder_and_converter() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    compat_source = (Path(__file__).parent / "run_wechat_win32_ocr_compat_checks.py").read_text(encoding="utf-8")
    add_friend_source = (Path(__file__).parent / "run_add_friend_package_smoke.py").read_text(encoding="utf-8")
    assert_true("window_layout.build_structural_layout_regions(" in source, "layout checks bypass production builder")
    assert_true("window_layout.transform_target_to_screen(" in source, "layout checks bypass production converter")
    assert_true("win32_ocr_layout.build_structural_layout_regions(" in compat_source, "compat checks bypass production builder")
    assert_true("build_structural_layout_regions(" in add_friend_source, "add-friend checks bypass production builder")
    mocked_core: list[str] = []
    for test_name, test_source in (("layout", source), ("compat", compat_source), ("add_friend", add_friend_source)):
        for node in ast.walk(ast.parse(test_source)):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                target_name = target.attr if isinstance(target, ast.Attribute) else (target.id if isinstance(target, ast.Name) else "")
                if target_name in {"build_structural_layout_regions", "transform_target_to_screen"}:
                    mocked_core.append(f"{test_name}:{node.lineno}:{target_name}")
    assert_true(not mocked_core, f"production layout core is mocked: {mocked_core}")


def test_layout_module_exposes_only_the_four_public_ui_error_codes() -> None:
    allowed = {
        "WECHAT_UI_WINDOW_NORMALIZATION_FAILED",
        "WECHAT_UI_LAYOUT_UNRESOLVED",
        "WECHAT_UI_LAYOUT_STALE",
        "WECHAT_UI_COORDINATE_MAPPING_INVALID",
    }
    source = (PROJECT_ROOT / "apps" / "wechat_ai_customer_service" / "adapters" / "wechat_win32_ocr" / "window_layout.py").read_text(encoding="utf-8")
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value.startswith("WECHAT_UI_") or node.value.startswith("LAYOUT_"))
    }
    assert_true(literals <= allowed, f"layout module introduced a second error-code vocabulary: {sorted(literals - allowed)}")


def main() -> None:
    tests = [
        value
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"layout snapshot checks passed: {len(tests)}")


if __name__ == "__main__":
    main()
