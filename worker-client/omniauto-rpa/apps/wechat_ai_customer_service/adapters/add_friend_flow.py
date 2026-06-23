"""Flow orchestration for the add_friend entry-click RPA route."""

from __future__ import annotations

import time
from typing import Any, Protocol

from apps.wechat_ai_customer_service.adapters.add_friend_contract import (
    normalize_add_friend_query,
    validate_add_friend_entry_click_contract,
)
from apps.wechat_ai_customer_service.adapters.add_friend_flow_context import AddFriendFlowContext
from apps.wechat_ai_customer_service.adapters.add_friend_flow_events import add_friend_query_search_events_from_result
from apps.wechat_ai_customer_service.adapters.add_friend_result_mapping import (
    ERROR_ADD_FRIEND_MENU_CLICK_FAILED,
    ERROR_PLUS_ENTRY_NOT_FOUND,
    ERROR_PLUS_ENTRY_POPUP_NOT_DETECTED,
    add_friend_server_report_payload,
)
from apps.wechat_ai_customer_service.adapters.add_friend_routes import (
    ADD_FRIEND_MAIN_ROUTE,
)


class AddFriendOpsProtocol(Protocol):
    """Sidecar operations required by the add_friend entry-click flow."""

    PROJECT_ROOT: Any

    def add_friend_entry_click_validation_failure_payload(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_window_geometry(self, hwnd: int) -> dict[str, Any]: ...
    def capture_wechat_window_visible_screen(self, hwnd: int, *, artifact_dir: str, label: str) -> tuple[Any, str]: ...
    def add_friend_plus_button_point_for_geometry(self, geometry: dict[str, Any]) -> tuple[int, int]: ...
    def add_friend_windows_1080p_reference_plus_button_point_for_geometry(self, geometry: dict[str, Any]) -> tuple[int, int]: ...
    def add_friend_windows_plus_button_point_for_geometry(self, geometry: dict[str, Any]) -> tuple[int, int]: ...
    def add_friend_plus_entry_target(self, geometry: dict[str, Any], image_size: Any, ocr_items: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]: ...
    def add_friend_popup_menu_bounds(self, image_size: Any, *, plus_screen_x: int, plus_screen_y: int) -> list[int]: ...
    def run_ocr_on_screen_region(self, image: Any, bounds: list[int]) -> list[dict[str, Any]]: ...
    def add_friend_ocr_snapshots(self, items: list[dict[str, Any]], image_size: Any) -> list[dict[str, Any]]: ...
    def add_friend_surface_readiness(self, image: Any, items: list[dict[str, Any]], geometry: dict[str, Any], **kwargs: Any) -> dict[str, Any]: ...
    def add_friend_menu_candidate_targets(self, items: list[dict[str, Any]], image_size: Any, **kwargs: Any) -> list[dict[str, Any]]: ...
    def plus_entry_popup_menu_detected(self, items: list[dict[str, Any]], targets: list[dict[str, Any]]) -> dict[str, Any]: ...
    def draw_add_friend_screen_annotation(self, image: Any, **kwargs: Any) -> str: ...
    def draw_add_friend_layout_calibration_annotation(self, image: Any, **kwargs: Any) -> str: ...
    def add_click_screen_origin_to_targets(self, targets: list[dict[str, Any]], *, origin_x: int, origin_y: int) -> list[dict[str, Any]]: ...
    def click_add_friend_menu_entry_and_capture(self, hwnd: int, output_dir: Any, *, menu_targets: list[dict[str, Any]]) -> dict[str, Any]: ...
    def input_add_friend_query_and_search(self, hwnd: int, output_dir: Any, **kwargs: Any) -> dict[str, Any]: ...
    def write_add_friend_entry_click_review(self, output_dir: Any, payload: dict[str, Any]) -> str: ...
    def add_friend_paced_pause(self, tier: str, **kwargs: Any) -> float: ...
    def add_friend_operator_guard_checkpoint(self, **kwargs: Any) -> dict[str, Any]: ...
    def human_window_image_hover(self, hwnd: int, x: int, y: int) -> dict[str, Any]: ...
    def human_window_image_click_in_bounds(self, hwnd: int, x: int, y: int, *, bounds: list[int], action_name: str = "human_window_image_click_in_bounds") -> dict[str, Any]: ...
    def bounded_int(self, value: Any, *, default: int, minimum: int, maximum: int) -> int: ...


def add_friend_entry_click_task_outcome(query_search: dict[str, Any]) -> dict[str, Any]:
    explicit_status = str(query_search.get("task_status") or "")
    ok = bool(query_search.get("ok")) or explicit_status == "completed"
    task_status = explicit_status or ("completed" if ok else "failed")
    result_code = str(query_search.get("result_code") or "")
    error_code = str(query_search.get("error_code") or "")
    current_step = str(
        query_search.get("current_step")
        or ("task_completed" if task_status == "completed" else query_search.get("state") or "query_search_flow")
    )
    server_report_payload = query_search.get("server_report_payload")
    if not isinstance(server_report_payload, dict) or not server_report_payload:
        server_report_payload = add_friend_server_report_payload(
            task_status=task_status,
            result_code=result_code or None,
            error_code=error_code or None,
            current_step=current_step,
        )
    return {
        "ok": ok,
        "task_status": task_status,
        "result_code": result_code,
        "error_code": error_code,
        "current_step": current_step,
        "server_report_payload": server_report_payload,
    }


def run_add_friend_entry_click_plan_flow(
    ops: AddFriendOpsProtocol,
    hwnd: int,
    probe: dict[str, Any],
    *,
    phone: str = "",
    wechat: str = "",
    verify_message: str = "",
    remark_name: str = "",
    remark_code: str = "",
    artifact_dir: str | None = None,
    route: str = ADD_FRIEND_MAIN_ROUTE,
) -> dict[str, Any]:
    """Run an add_friend entry-click flow using sidecar Windows Win32/OCR ops."""
    selected_route = str(route or ADD_FRIEND_MAIN_ROUTE).strip().lower()
    query = normalize_add_friend_query(phone=phone, wechat=wechat)
    flow = AddFriendFlowContext(
        project_root=ops.PROJECT_ROOT,
        route=selected_route,
        artifact_dir=artifact_dir,
    )
    output_dir = flow.output_dir
    validation = validate_add_friend_entry_click_contract(
        phone=phone,
        wechat=wechat,
        verify_message=verify_message,
        remark_name=remark_name,
        remark_code=remark_code,
    )
    if not validation.get("ok"):
        return ops.add_friend_entry_click_validation_failure_payload(
            phone=phone,
            wechat=wechat,
            verify_message=verify_message,
            remark_name=remark_name,
            remark_code=remark_code,
            artifact_dir=str(output_dir),
            probe=probe,
        )

    query = str(validation.get("query") or query)
    clean_verify_message = str(validation.get("verify_message") or "")
    clean_remark_name = str(validation.get("remark_name") or "")
    clean_remark_code = str(validation.get("remark_code") or "")
    remark_code_valid = bool(validation.get("remark_code_valid"))
    flow.add_event(
        step_id="payload_validation",
        title="字段契约校验",
        status="completed",
        state_before="task_received",
        state_after="payload_valid",
        result={
            "ok": True,
            "verify_message": clean_verify_message,
            "remark_name": clean_remark_name,
            "remark_code": clean_remark_code,
            "remark_code_valid": remark_code_valid,
            "validation_errors": [],
            "legacy_remark_fallback": False,
        },
    )
    geometry = ops.get_window_geometry(hwnd)
    window_rect = [
        int(geometry.get("left") or 0),
        int(geometry.get("top") or 0),
        int(geometry.get("right") or 0),
        int(geometry.get("bottom") or 0),
    ]

    timings = flow.timings
    guard_checkpoint = ops.add_friend_operator_guard_checkpoint(reason="before_entry_capture")
    timings.append({"name": "operator_guard_before_entry_capture", "seconds": 0.0, "result": guard_checkpoint})
    before_shot, before_screenshot_path = ops.capture_wechat_window_visible_screen(
        hwnd,
        artifact_dir=str(output_dir),
        label="add_friend_entry_before_click_window",
    )
    window_origin_x = int(geometry.get("left") or 0)
    window_origin_y = int(geometry.get("top") or 0)
    platform_adapter = "windows"
    before_full_ocr_started_at = time.perf_counter()
    before_full_items = ops.run_ocr_on_screen_region(before_shot, [0, 0, before_shot.size[0], before_shot.size[1]])
    timings.append(
        {
            "name": "before_full_surface_ocr",
            "seconds": round(time.perf_counter() - before_full_ocr_started_at, 3),
            "ocr_scope": "full_window_preflight",
            "bounds": [0, 0, before_shot.size[0], before_shot.size[1]],
            "ocr_count": len(before_full_items),
        }
    )
    plus_target = ops.add_friend_plus_entry_target(
        geometry,
        before_shot.size,
        before_full_items,
        screenshot=before_shot,
        route_kind=platform_adapter,
    )
    plus_x = int(plus_target.get("x") or plus_target.get("point", [0, 0])[0])
    plus_y = int(plus_target.get("y") or plus_target.get("point", [0, 0])[1])
    popup_bounds = ops.add_friend_popup_menu_bounds(before_shot.size, plus_screen_x=plus_x, plus_screen_y=plus_y)
    before_ocr_started_at = time.perf_counter()
    before_items = ops.run_ocr_on_screen_region(before_shot, popup_bounds)
    timings.append(
        {
            "name": "before_popup_region_ocr",
            "seconds": round(time.perf_counter() - before_ocr_started_at, 3),
            "ocr_scope": "plus_entry_popup_region",
            "bounds": popup_bounds,
            "ocr_count": len(before_items),
        }
    )
    before_readiness = ops.add_friend_surface_readiness(before_shot, before_full_items or before_items, geometry, stage="entry_before_click")
    before_readiness = {
        **before_readiness,
        "capture_mode": "screen_visible",
        "ocr_scope": "full_window_preflight",
        "popup_ocr_count": len(before_items),
        "ocr_count": int(before_readiness.get("ocr_count") or len(before_full_items or before_items)),
    }
    before_annotated_path = output_dir / "add_friend_entry_before_click_screen_annotated.png"
    before_actual_menu_targets = ops.add_friend_menu_candidate_targets(
        before_items,
        before_shot.size,
        plus_screen_x=plus_x,
        plus_screen_y=plus_y,
        include_expected=False,
    )
    before_popup_detection = ops.plus_entry_popup_menu_detected(before_items, before_actual_menu_targets)
    before_menu_targets = before_actual_menu_targets
    if before_popup_detection.get("detected"):
        before_menu_targets = ops.add_friend_menu_candidate_targets(
            before_items,
            before_shot.size,
            plus_screen_x=plus_x,
            plus_screen_y=plus_y,
            include_expected=True,
        )
    before_annotated = ops.draw_add_friend_screen_annotation(
        before_shot,
        ocr_items=before_items,
        targets=[plus_target, *before_menu_targets],
        output_path=before_annotated_path,
        window_rect=None,
    )
    layout_meta = plus_target.get("metadata") if isinstance(plus_target.get("metadata"), dict) else {}
    layout_calibration = layout_meta.get("layout_calibration")
    layout_annotated_path = output_dir / "add_friend_window_layout_calibration_annotated.png"
    layout_annotated = ops.draw_add_friend_layout_calibration_annotation(
        before_shot,
        layout_calibration=layout_calibration,
        output_path=layout_annotated_path,
    )
    flow.add_event(
        step_id="window_layout_calibration",
        title="微信窗口布局校准",
        status="completed" if plus_target.get("executable") else "failed",
        state_before="payload_valid",
        state_after="plus_entry_located" if plus_target.get("executable") else "plus_entry_not_found",
        ocr_items=ops.add_friend_ocr_snapshots(before_full_items or before_items, before_shot.size),
        targets=[plus_target],
        selected_target=plus_target,
        artifacts={"raw": before_screenshot_path, "annotated": layout_annotated},
        result={
            "ok": bool(plus_target.get("executable")),
            "geometry": geometry,
            "window_rect": window_rect,
            "image_size": list(before_shot.size),
            "layout_calibration": layout_calibration,
            "diagnostic_references": plus_target.get("diagnostic_references") or layout_meta.get("diagnostic_references") or [],
            "source": plus_target.get("source"),
            "confidence": plus_target.get("confidence"),
            "executable": plus_target.get("executable"),
            "selected_reason": plus_target.get("selected_reason"),
        },
    )
    flow.add_event(
        step_id="entry_before_capture",
        title="运行前窗口截图与入口定位",
        status="completed",
        state_before="payload_valid",
        state_after="plus_entry_popup_menu" if before_popup_detection.get("detected") else "main_window",
        ocr_items=ops.add_friend_ocr_snapshots(before_full_items or before_items, before_shot.size),
        targets=[plus_target, *before_menu_targets],
        selected_target=plus_target,
        artifacts={"raw": before_screenshot_path, "annotated": before_annotated},
        result={
            "ok": True,
            "capture_mode": "screen_visible",
            "readiness": before_readiness,
            "popup_detection": before_popup_detection,
        },
    )
    if not before_readiness.get("ok"):
        query_search = {
            "ok": False,
            "state": before_readiness.get("state") or "wechat_window_not_ready",
            "task_status": "failed",
            "result_code": "",
            "error_code": before_readiness.get("error_code") or "WECHAT_WINDOW_NOT_READY",
            "current_step": "preflight_window_ready",
            "server_report_payload": add_friend_server_report_payload(
                task_status="failed",
                error_code=str(before_readiness.get("error_code") or "WECHAT_WINDOW_NOT_READY"),
                current_step="preflight_window_ready",
            ),
            "reason": before_readiness.get("reason") or "add_friend_surface_not_ready_before_click",
            "readiness": before_readiness,
        }
        task_outcome = add_friend_entry_click_task_outcome(query_search)
        payload = _build_entry_click_payload(
            task_outcome=task_outcome,
            query=query,
            phone=phone,
            wechat=wechat,
            verify_message=clean_verify_message,
            remark_name=clean_remark_name,
            remark_code=clean_remark_code,
            remark_code_valid=remark_code_valid,
            probe=probe,
            geometry_before=geometry,
            geometry_after=geometry,
            before={
                "screenshot_path": before_screenshot_path,
                "annotated_path": before_annotated,
                "capture_mode": "screen_visible",
                "readiness": before_readiness,
                "ocr_items": ops.add_friend_ocr_snapshots(before_full_items or before_items, before_shot.size),
                "planned_targets": [plus_target, *before_menu_targets],
                "popup_detection": before_popup_detection,
                "hover": {"skipped": True, "reason": "surface_not_ready_before_click"},
            },
            after={
                "screenshot_path": before_screenshot_path,
                "annotated_path": before_annotated,
                "capture_mode": "screen_visible",
                "readiness": before_readiness,
                "ocr_items": ops.add_friend_ocr_snapshots(before_full_items or before_items, before_shot.size),
                "planned_targets": before_menu_targets,
                "popup_detection": before_popup_detection,
            },
            click_attempts=[],
            menu_click={"clicked": False, "reason": before_readiness.get("reason") or "surface_not_ready_before_click", "target": None},
            query_search=query_search,
            plan_path=str(flow.plan_path),
            note="add_friend_preflight_stopped_before_click_due_to_window_or_account_state",
        )
        _append_flow_timings(payload, timings, payload["menu_click"], query_search, flow.started_at)
        return flow.finalize_payload(payload, report_writer=ops.write_add_friend_entry_click_review)

    if not plus_target.get("executable"):
        query_search = {
            "ok": False,
            "state": "plus_entry_not_found",
            "task_status": "failed",
            "result_code": "",
            "error_code": ERROR_PLUS_ENTRY_NOT_FOUND,
            "current_step": "window_layout_calibration",
            "server_report_payload": add_friend_server_report_payload(
                task_status="failed",
                error_code=ERROR_PLUS_ENTRY_NOT_FOUND,
                current_step="window_layout_calibration",
            ),
            "reason": "plus_icon_not_found_inside_calibrated_sidebar_header",
            "selected_target": plus_target,
        }
        task_outcome = add_friend_entry_click_task_outcome(query_search)
        payload = _build_entry_click_payload(
            task_outcome=task_outcome,
            query=query,
            phone=phone,
            wechat=wechat,
            verify_message=clean_verify_message,
            remark_name=clean_remark_name,
            remark_code=clean_remark_code,
            remark_code_valid=remark_code_valid,
            probe=probe,
            geometry_before=geometry,
            geometry_after=geometry,
            before={
                "screenshot_path": before_screenshot_path,
                "annotated_path": before_annotated,
                "capture_mode": "screen_visible",
                "readiness": before_readiness,
                "ocr_items": ops.add_friend_ocr_snapshots(before_full_items or before_items, before_shot.size),
                "planned_targets": [plus_target],
                "popup_detection": before_popup_detection,
                "hover": {"skipped": True, "reason": "plus_entry_not_executable"},
            },
            after={
                "screenshot_path": before_screenshot_path,
                "annotated_path": before_annotated,
                "capture_mode": "screen_visible",
                "readiness": before_readiness,
                "ocr_items": ops.add_friend_ocr_snapshots(before_full_items or before_items, before_shot.size),
                "planned_targets": before_menu_targets,
                "popup_detection": before_popup_detection,
            },
            click_attempts=[],
            menu_click={"clicked": False, "reason": "plus_entry_not_executable", "target": None},
            query_search=query_search,
            plan_path=str(flow.plan_path),
            note="add_friend_stopped_before_click_because_plus_icon_was_not_visually_located",
        )
        _append_flow_timings(payload, timings, payload["menu_click"], query_search, flow.started_at)
        return flow.finalize_payload(payload, report_writer=ops.write_add_friend_entry_click_review)

    if before_popup_detection.get("detected"):
        plan_path = flow.plan_path
        menu_click = ops.click_add_friend_menu_entry_and_capture(
            hwnd,
            output_dir,
            menu_targets=ops.add_click_screen_origin_to_targets(
                before_menu_targets,
                origin_x=window_origin_x,
                origin_y=window_origin_y,
            ),
        )
        query_hwnd = int(menu_click.get("next_hwnd") or 0) if isinstance(menu_click, dict) else 0
        query_search = (
            ops.input_add_friend_query_and_search(
                query_hwnd,
                output_dir,
                query=query,
                verify_message=clean_verify_message,
                remark_name=clean_remark_name,
                remark_code=clean_remark_code,
            )
            if menu_click.get("clicked") and query and query_hwnd
            else {
                "ok": False,
                "state": "query_not_run",
                "reason": "empty_query_or_menu_click_failed_or_dialog_hwnd_missing",
                "query": query,
                "dialog_hwnd": query_hwnd,
            }
        )
        _add_menu_and_query_events(flow, ops, before_menu_targets, menu_click, query_search, state_before="plus_entry_popup_menu")
        task_outcome = add_friend_entry_click_task_outcome(query_search)
        payload = _build_entry_click_payload(
            task_outcome=task_outcome,
            query=query,
            phone=phone,
            wechat=wechat,
            verify_message=clean_verify_message,
            remark_name=clean_remark_name,
            remark_code=clean_remark_code,
            remark_code_valid=remark_code_valid,
            probe=probe,
            geometry_before=geometry,
            geometry_after=geometry,
            before={
                "screenshot_path": before_screenshot_path,
                "annotated_path": before_annotated,
                "capture_mode": "screen_visible",
                "readiness": before_readiness,
                "ocr_items": ops.add_friend_ocr_snapshots(before_items, before_shot.size),
                "planned_targets": [plus_target, *before_menu_targets],
                "popup_detection": before_popup_detection,
                "hover": {"skipped": True, "reason": "plus_entry_popup_menu_already_visible"},
            },
            after={
                "screenshot_path": before_screenshot_path,
                "annotated_path": before_annotated,
                "capture_mode": "screen_visible",
                "readiness": before_readiness,
                "ocr_items": ops.add_friend_ocr_snapshots(before_items, before_shot.size),
                "planned_targets": before_menu_targets,
                "popup_detection": before_popup_detection,
            },
            click_attempts=[],
            menu_click=menu_click,
            query_search=query_search,
            plan_path=str(plan_path),
            note="plus_entry_popup_menu_already_visible_then_click_add_friend_menu_entry_type_query_and_click_search",
        )
        _append_flow_timings(payload, timings, menu_click, query_search, flow.started_at)
        return flow.finalize_payload(payload, report_writer=ops.write_add_friend_entry_click_review)

    pause_seconds = ops.add_friend_paced_pause("critical_click", reason="before_plus_entry_hover")
    timings.append({"name": "before_plus_entry_hover_pause", "seconds": round(pause_seconds, 3)})
    hover_started_at = time.perf_counter()
    hover_result = ops.human_window_image_hover(hwnd, plus_x, plus_y)
    timings.append({"name": "plus_entry_hover", "seconds": round(time.perf_counter() - hover_started_at, 3), "result": hover_result})
    pause_seconds = ops.add_friend_paced_pause("critical_click", reason="after_plus_entry_hover_before_click")
    timings.append({"name": "after_plus_entry_hover_before_click_pause", "seconds": round(pause_seconds, 3)})

    max_attempts = 1
    click_attempts: list[dict[str, Any]] = []
    after_geometry = geometry
    after_shot = before_shot
    after_screenshot_path = ""
    after_items: list[dict[str, Any]] = []
    after_readiness: dict[str, Any] = {}
    menu_targets: list[dict[str, Any]] = []
    popup_detection: dict[str, Any] = {"detected": False, "reason": "not_attempted"}
    after_annotated = ""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            pause_seconds = ops.add_friend_paced_pause("critical_click", reason=f"before_plus_entry_retry_{attempt}")
            timings.append({"name": f"before_plus_entry_retry_{attempt}_pause", "seconds": round(pause_seconds, 3)})
        guard_checkpoint = ops.add_friend_operator_guard_checkpoint(reason=f"before_plus_entry_click_{attempt}")
        timings.append({"name": f"operator_guard_before_plus_entry_click_{attempt}", "seconds": 0.0, "result": guard_checkpoint})
        click_bounds = list(plus_target.get("click_bounds") or plus_target.get("bounds") or [])
        click_started_at = time.perf_counter()
        click_result = ops.human_window_image_click_in_bounds(
            hwnd,
            plus_x,
            plus_y,
            bounds=click_bounds,
            action_name=f"plus_entry_click_{attempt}",
        )
        timings.append(
            {
                "name": f"plus_entry_click_{attempt}",
                "seconds": round(time.perf_counter() - click_started_at, 3),
                "result": click_result,
            }
        )
        if not click_result.get("ok"):
            popup_detection = {"detected": False, "reason": "plus_entry_click_failed", "click": click_result}
            click_attempts.append(
                {
                    "attempt": attempt,
                    "screenshot_path": "",
                    "annotated_path": "",
                    "readiness": {},
                    "popup_detection": popup_detection,
                    "planned_targets": [],
                    "click": click_result,
                }
            )
            flow.add_event(
                step_id=f"plus_entry_click_attempt_{attempt}",
                title=f"点击 + 入口 attempt {attempt}",
                status="failed",
                state_before="main_window",
                state_after="plus_entry_click_failed",
                targets=[plus_target],
                selected_target=plus_target,
                result={"attempt": attempt, "click": click_result, "popup_detection": popup_detection},
            )
            break
        pause_seconds = ops.add_friend_paced_pause("verify", reason=f"after_plus_entry_click_{attempt}_before_screen_capture")
        timings.append({"name": f"after_plus_entry_click_{attempt}_before_screen_capture_pause", "seconds": round(pause_seconds, 3)})

        after_geometry = ops.get_window_geometry(hwnd)
        after_shot, after_screenshot_path = ops.capture_wechat_window_visible_screen(
            hwnd,
            artifact_dir=str(output_dir),
            label=f"add_friend_entry_after_click_window_attempt_{attempt}",
        )
        popup_bounds = ops.add_friend_popup_menu_bounds(after_shot.size, plus_screen_x=plus_x, plus_screen_y=plus_y)
        after_ocr_started_at = time.perf_counter()
        after_items = ops.run_ocr_on_screen_region(after_shot, popup_bounds)
        timings.append(
            {
                "name": f"after_plus_entry_click_{attempt}_popup_region_ocr",
                "seconds": round(time.perf_counter() - after_ocr_started_at, 3),
                "ocr_scope": "plus_entry_popup_region",
                "bounds": popup_bounds,
                "ocr_count": len(after_items),
            }
        )
        after_readiness = ops.add_friend_surface_readiness(after_shot, after_items, after_geometry, stage="entry_after_click")
        after_readiness = {
            **after_readiness,
            "capture_mode": "screen_visible",
            "attempt": attempt,
            "ocr_scope": "plus_entry_popup_region",
            "ocr_count": int(after_readiness.get("ocr_count") or len(after_items)),
        }
        actual_menu_targets = ops.add_friend_menu_candidate_targets(
            after_items,
            after_shot.size,
            plus_screen_x=plus_x,
            plus_screen_y=plus_y,
            include_expected=False,
        )
        popup_detection = ops.plus_entry_popup_menu_detected(after_items, actual_menu_targets)
        menu_targets = actual_menu_targets
        if popup_detection.get("detected"):
            menu_targets = ops.add_friend_menu_candidate_targets(
                after_items,
                after_shot.size,
                plus_screen_x=plus_x,
                plus_screen_y=plus_y,
                include_expected=True,
            )
        attempt_annotated_path = output_dir / f"add_friend_entry_after_click_screen_attempt_{attempt}_annotated.png"
        attempt_annotated = ops.draw_add_friend_screen_annotation(
            after_shot,
            ocr_items=after_items,
            targets=[plus_target, *menu_targets],
            output_path=attempt_annotated_path,
            window_rect=None,
        )
        click_attempts.append(
            {
                "attempt": attempt,
                "screenshot_path": after_screenshot_path,
                "annotated_path": attempt_annotated,
                "readiness": after_readiness,
                "popup_detection": popup_detection,
                "planned_targets": menu_targets,
                "click": click_result,
            }
        )
        flow.add_event(
            step_id=f"plus_entry_click_attempt_{attempt}",
            title=f"点击 + 入口 attempt {attempt}",
            status="completed" if popup_detection.get("detected") else "warning",
            state_before="main_window",
            state_after="plus_entry_popup_menu" if popup_detection.get("detected") else "main_window",
            ocr_items=ops.add_friend_ocr_snapshots(after_items, after_shot.size),
            targets=[plus_target, *menu_targets],
            selected_target=plus_target,
            artifacts={"raw": after_screenshot_path, "annotated": attempt_annotated},
            result={
                "attempt": attempt,
                "click": click_result,
                "readiness": after_readiness,
                "popup_detection": popup_detection,
            },
        )
        after_annotated = attempt_annotated
        if popup_detection.get("detected"):
            break
        if not after_readiness.get("ok"):
            popup_detection = {
                "detected": False,
                "reason": after_readiness.get("reason") or "surface_not_ready_after_plus_click",
                "readiness": after_readiness,
            }
            break

    menu_click = (
        ops.click_add_friend_menu_entry_and_capture(
            hwnd,
            output_dir,
            menu_targets=ops.add_click_screen_origin_to_targets(
                menu_targets,
                origin_x=window_origin_x,
                origin_y=window_origin_y,
            ),
        )
        if popup_detection.get("detected")
        else {"clicked": False, "reason": popup_detection.get("reason") or "plus_entry_popup_menu_not_detected", "target": None}
    )
    query_hwnd = int(menu_click.get("next_hwnd") or 0) if isinstance(menu_click, dict) else 0
    menu_failed_after_popup = bool(popup_detection.get("detected")) and not bool(menu_click.get("clicked"))
    query_search = (
        ops.input_add_friend_query_and_search(
            query_hwnd,
            output_dir,
            query=query,
            verify_message=clean_verify_message,
            remark_name=clean_remark_name,
            remark_code=clean_remark_code,
        )
        if menu_click.get("clicked") and query and query_hwnd
        else {
            "ok": False,
            "state": (
                after_readiness.get("state")
                if after_readiness and not after_readiness.get("ok")
                else ("add_friend_menu_click_failed" if menu_failed_after_popup else "plus_entry_popup_menu_not_detected")
            ),
            "task_status": "failed",
            "error_code": (
                after_readiness.get("error_code")
                if after_readiness and not after_readiness.get("ok")
                else (ERROR_ADD_FRIEND_MENU_CLICK_FAILED if menu_failed_after_popup else ERROR_PLUS_ENTRY_POPUP_NOT_DETECTED)
            ),
            "current_step": (
                "preflight_window_ready"
                if after_readiness and not after_readiness.get("ok")
                else ("add_friend_menu_click" if menu_failed_after_popup else "plus_entry_click")
            ),
            "server_report_payload": (
                add_friend_server_report_payload(
                    task_status="failed",
                    error_code=str(after_readiness.get("error_code")),
                    current_step="preflight_window_ready",
                )
                if after_readiness and not after_readiness.get("ok") and after_readiness.get("error_code")
                else add_friend_server_report_payload(
                    task_status="failed",
                    error_code=ERROR_ADD_FRIEND_MENU_CLICK_FAILED if menu_failed_after_popup else ERROR_PLUS_ENTRY_POPUP_NOT_DETECTED,
                    current_step="add_friend_menu_click" if menu_failed_after_popup else "plus_entry_click",
                )
            ),
            "reason": (
                after_readiness.get("reason")
                if after_readiness and not after_readiness.get("ok")
                else (
                    menu_click.get("reason")
                    if menu_failed_after_popup
                    else (popup_detection.get("reason") or "plus_entry_popup_menu_not_detected")
                )
            ),
            "query": query,
            "dialog_hwnd": query_hwnd,
            "readiness": after_readiness if after_readiness and not after_readiness.get("ok") else {},
        }
    )
    _add_menu_and_query_events(
        flow,
        ops,
        menu_targets,
        menu_click,
        query_search,
        state_before="plus_entry_popup_menu" if popup_detection.get("detected") else "main_window",
    )
    plan_path = flow.plan_path
    task_outcome = add_friend_entry_click_task_outcome(query_search)
    payload = _build_entry_click_payload(
        task_outcome=task_outcome,
        query=query,
        phone=phone,
        wechat=wechat,
        verify_message=clean_verify_message,
        remark_name=clean_remark_name,
        remark_code=clean_remark_code,
        remark_code_valid=remark_code_valid,
        probe=probe,
        geometry_before=geometry,
        geometry_after=after_geometry,
        before={
            "screenshot_path": before_screenshot_path,
            "annotated_path": before_annotated,
            "capture_mode": "screen_visible",
            "readiness": before_readiness,
            "ocr_items": ops.add_friend_ocr_snapshots(before_items, before_shot.size),
            "planned_targets": [plus_target],
            "hover": hover_result,
        },
        after={
            "screenshot_path": after_screenshot_path,
            "annotated_path": after_annotated,
            "capture_mode": "screen_visible",
            "readiness": after_readiness,
            "ocr_items": ops.add_friend_ocr_snapshots(after_items, after_shot.size),
            "planned_targets": menu_targets,
            "popup_detection": popup_detection,
        },
        click_attempts=click_attempts,
        menu_click=menu_click,
        query_search=query_search,
        plan_path=str(plan_path),
        note="wechat_window_capture_clicks_plus_until_popup_then_clicks_add_friend_menu_entry_type_query_and_click_search",
    )
    _append_flow_timings(payload, timings, menu_click, query_search, flow.started_at)
    return flow.finalize_payload(payload, report_writer=ops.write_add_friend_entry_click_review)


def _build_entry_click_payload(
    *,
    task_outcome: dict[str, Any],
    query: str,
    phone: str,
    wechat: str,
    verify_message: str,
    remark_name: str,
    remark_code: str,
    remark_code_valid: bool,
    probe: dict[str, Any],
    geometry_before: dict[str, Any],
    geometry_after: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    click_attempts: list[dict[str, Any]],
    menu_click: dict[str, Any],
    query_search: dict[str, Any],
    plan_path: str,
    note: str,
) -> dict[str, Any]:
    return {
        "ok": task_outcome["ok"],
        "online": True,
        "adapter": "win32_ocr",
        "state": "add_friend_entry_click_plan",
        "task_type": "add_friend",
        "task_status": task_outcome["task_status"],
        "result_code": task_outcome["result_code"],
        "error_code": task_outcome["error_code"],
        "current_step": task_outcome["current_step"],
        "server_report_payload": task_outcome["server_report_payload"],
        "query": query,
        "phone": phone,
        "wechat": wechat,
        "verify_message": verify_message,
        "remark_name": remark_name,
        "remark_code": remark_code,
        "remark_code_valid": remark_code_valid,
        "validation_errors": [],
        "legacy_remark_fallback": False,
        "window_probe": probe,
        "geometry_before": geometry_before,
        "geometry_after": geometry_after,
        "before": before,
        "after": after,
        "click_attempts": click_attempts,
        "menu_click": menu_click,
        "query_search": query_search,
        "plan_path": plan_path,
        "note": note,
    }


def _add_menu_and_query_events(
    flow: AddFriendFlowContext,
    ops: AddFriendOpsProtocol,
    menu_targets: list[dict[str, Any]],
    menu_click: dict[str, Any],
    query_search: dict[str, Any],
    *,
    state_before: str,
) -> None:
    flow.add_event(
        step_id="add_friend_menu_click",
        title="点击添加朋友菜单项",
        status="completed" if menu_click.get("clicked") else "failed",
        state_before=state_before,
        state_after="add_friend_search_page" if menu_click.get("clicked") else "add_friend_menu_click_failed",
        targets=menu_targets,
        selected_target=menu_click.get("target") if isinstance(menu_click.get("target"), dict) else {},
        artifacts={
            "raw": menu_click.get("screenshot_path") if isinstance(menu_click, dict) else "",
            "annotated": menu_click.get("annotated_path") if isinstance(menu_click, dict) else "",
        },
        result=menu_click if isinstance(menu_click, dict) else {"clicked": False},
    )
    flow.add_event(
        step_id="query_search_flow",
        title="输入手机号/微信号并执行后续加好友链路",
        status="completed" if query_search.get("ok") else "failed",
        state_before="add_friend_search_page",
        state_after=str(query_search.get("state") or "query_search_done"),
        ocr_items=(
            query_search.get("result", {}).get("ocr_items")
            if isinstance(query_search.get("result"), dict)
            else []
        ),
        targets=(
            query_search.get("page", {}).get("targets")
            if isinstance(query_search.get("page"), dict)
            else []
        ),
        artifacts={
            "raw": (
                query_search.get("result", {}).get("screenshot_path")
                if isinstance(query_search.get("result"), dict)
                else ""
            ),
            "annotated": (
                query_search.get("result", {}).get("annotated_path")
                if isinstance(query_search.get("result"), dict)
                else ""
            ),
        },
        result={
            "ok": query_search.get("ok"),
            "state": query_search.get("state"),
            "task_status": query_search.get("task_status"),
            "result_code": query_search.get("result_code"),
            "error_code": query_search.get("error_code"),
            "current_step": query_search.get("current_step"),
            "server_report_payload": query_search.get("server_report_payload"),
        },
    )
    flow.add_events(add_friend_query_search_events_from_result(query_search))


def _append_flow_timings(
    payload: dict[str, Any],
    timings: list[dict[str, Any]],
    menu_click: dict[str, Any],
    query_search: dict[str, Any],
    started_at: float,
) -> None:
    menu_click_timings = list(menu_click.get("timings") or []) if isinstance(menu_click, dict) else []
    query_timings = list(query_search.get("timings") or []) if isinstance(query_search, dict) else []
    payload["timings"] = [
        *timings,
        *menu_click_timings,
        *query_timings,
        {"name": "flow_total", "seconds": round(time.perf_counter() - started_at, 3)},
    ]
