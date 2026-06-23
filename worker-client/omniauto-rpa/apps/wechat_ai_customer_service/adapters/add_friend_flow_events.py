"""Flow-to-diagnostic-event mapping for add_friend RPA."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.adapters.add_friend_diagnostics import (
    make_step_event,
    normalize_step_event,
)


def add_friend_entry_click_events_from_payload(
    payload: dict[str, Any],
    *,
    existing_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build complete, stable step events from the add-friend entry-click payload."""
    events: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(event: dict[str, Any]) -> None:
        normalized = normalize_step_event(event)
        step_id = str(normalized.get("step_id") or "")
        if not step_id or step_id in seen:
            return
        seen.add(step_id)
        events.append(normalized)

    for event in existing_events or []:
        if isinstance(event, dict):
            add(event)

    if _is_validation_failure(payload) and "payload_validation" not in seen:
        add(_payload_validation_event(payload))

    before = _dict(payload.get("before"))
    if before and "entry_before_capture" not in seen:
        popup_detection = _dict(before.get("popup_detection"))
        add(
            make_step_event(
                step_id="entry_before_capture",
                title="运行前窗口截图与入口定位",
                status="completed",
                state_before="payload_valid",
                state_after="plus_entry_popup_menu" if popup_detection.get("detected") else "main_window",
                ocr_items=_list(before.get("ocr_items")),
                targets=_list(before.get("planned_targets")),
                selected_target=_first_target(before.get("planned_targets")),
                artifacts=_artifacts(before),
                result={
                    "ok": True,
                    "capture_mode": before.get("capture_mode"),
                    "readiness": before.get("readiness"),
                    "popup_detection": popup_detection,
                    "hover": before.get("hover"),
                },
            )
        )

    for attempt in _list(payload.get("click_attempts")):
        if not isinstance(attempt, dict):
            continue
        attempt_no = attempt.get("attempt")
        popup_detection = _dict(attempt.get("popup_detection"))
        add(
            make_step_event(
                step_id=f"plus_entry_click_attempt_{attempt_no}",
                title=f"点击 + 入口 attempt {attempt_no}",
                status="completed" if popup_detection.get("detected") else "warning",
                state_before="main_window",
                state_after="plus_entry_popup_menu" if popup_detection.get("detected") else "main_window",
                targets=_list(attempt.get("planned_targets")),
                selected_target=_first_target(attempt.get("planned_targets")),
                artifacts=_artifacts(attempt),
                result={
                    "attempt": attempt_no,
                    "readiness": attempt.get("readiness"),
                    "popup_detection": popup_detection,
                },
            )
        )

    menu_click = _dict(payload.get("menu_click"))
    if menu_click and "add_friend_menu_click" not in seen:
        add(
            make_step_event(
                step_id="add_friend_menu_click",
                title="点击添加朋友菜单项",
                status="completed" if menu_click.get("clicked") else "failed",
                state_before="plus_entry_popup_menu",
                state_after="add_friend_search_page" if menu_click.get("clicked") else "add_friend_menu_click_failed",
                targets=[menu_click["target"]] if isinstance(menu_click.get("target"), dict) else [],
                selected_target=menu_click.get("target") if isinstance(menu_click.get("target"), dict) else {},
                artifacts=_artifacts(menu_click),
                result=menu_click,
            )
        )

    after = _dict(payload.get("after"))
    if after and not _dict(payload.get("query_search")):
        add(
            make_step_event(
                step_id="entry_popup_after_click_review",
                title="入口弹出菜单复核",
                status="completed" if _dict(after.get("popup_detection")).get("detected") else "warning",
                state_before="plus_entry_clicked",
                state_after="plus_entry_popup_menu" if _dict(after.get("popup_detection")).get("detected") else str(payload.get("state") or ""),
                ocr_items=_list(after.get("ocr_items")),
                targets=_list(after.get("planned_targets")),
                selected_target=_first_target(after.get("planned_targets")),
                artifacts=_artifacts(after),
                result=_dict(after.get("popup_detection")),
            )
        )

    query_search = _dict(payload.get("query_search"))
    if query_search:
        _add_query_search_events(add, query_search)

    return events


def add_friend_query_search_events_from_result(query_search: dict[str, Any]) -> list[dict[str, Any]]:
    """Build step events for the query/search/add-contact subflow at execution time."""
    events: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(event: dict[str, Any]) -> None:
        normalized = normalize_step_event(event)
        step_id = str(normalized.get("step_id") or "")
        if not step_id or step_id in seen:
            return
        seen.add(step_id)
        events.append(normalized)

    _add_query_search_events(add, _dict(query_search))
    return events


def _add_query_search_events(add: Any, query_search: dict[str, Any]) -> None:
    page = _dict(query_search.get("page"))
    if page:
        add(
            make_step_event(
                step_id="query_search_page",
                title="添加朋友页搜索框定位",
                status="completed",
                state_before="add_friend_search_page",
                state_after=str(query_search.get("state") or "search_page_ready"),
                ocr_items=_list(page.get("ocr_items")),
                targets=_list(page.get("targets")),
                selected_target=_first_target(page.get("targets")),
                artifacts=_artifacts(page),
                result={
                    "state": query_search.get("state"),
                    "query": query_search.get("query"),
                    "input_empty_before_clear": page.get("input_empty_before_clear"),
                    "clear_result": query_search.get("clear_result"),
                },
            )
        )

    clear_verify = _dict(query_search.get("clear_verify"))
    if clear_verify:
        verify = _dict(clear_verify.get("verify"))
        add(
            make_step_event(
                step_id="query_search_input_clear_verify",
                title="清空搜索框后复核",
                status="completed" if verify.get("ok") else "failed",
                state_before="add_friend_search_page",
                state_after="search_input_empty" if verify.get("ok") else "search_input_clear_failed",
                ocr_items=_list(clear_verify.get("ocr_items")),
                targets=_list(page.get("targets")),
                selected_target=_first_target(page.get("targets")),
                artifacts=_artifacts(clear_verify),
                result=verify,
            )
        )

    for attempt in _list(query_search.get("input_attempts")):
        if not isinstance(attempt, dict):
            continue
        verify = _dict(attempt.get("verify"))
        attempt_no = attempt.get("attempt")
        add(
            make_step_event(
                step_id=f"query_input_verify_attempt_{attempt_no}",
                title=f"输入核对 attempt {attempt_no}",
                status="completed" if verify.get("ok") else "failed",
                state_before="search_input_empty",
                state_after="search_input_verified" if verify.get("ok") else "search_input_mismatch",
                targets=_list(page.get("targets")),
                selected_target=_first_target(page.get("targets")),
                artifacts=_artifacts(attempt),
                result=verify,
            )
        )

    result = _dict(query_search.get("result"))
    if result:
        add(
            make_step_event(
                step_id="query_search_result",
                title="点击搜索后结果区识别",
                status="completed" if query_search.get("ok") else "failed",
                state_before="search_input_verified",
                state_after=str(query_search.get("state") or "search_result_checked"),
                ocr_items=_list(result.get("ocr_items")),
                artifacts=_artifacts(result),
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
        )

    add_contact_result = _dict(query_search.get("add_contact_result"))
    if add_contact_result:
        _add_add_contact_events(add, add_contact_result)


def _add_add_contact_events(add: Any, add_contact_result: dict[str, Any]) -> None:
    add_contact_before = _dict(add_contact_result.get("before"))
    if add_contact_before:
        add(
            make_step_event(
                step_id="add_contact_entry_before_click",
                title="点击添加到通讯录前定位",
                status="completed" if not add_contact_result.get("error_code") else "failed",
                state_before="search_result_checked",
                state_after=str(add_contact_result.get("state") or "add_contact_entry_detected"),
                ocr_items=_list(add_contact_before.get("ocr_items")),
                targets=_list(add_contact_before.get("targets")),
                selected_target=_first_target(add_contact_before.get("targets")),
                artifacts=_artifacts(add_contact_before),
                result=_result_fields(add_contact_result),
            )
        )
    elif add_contact_result.get("annotated_path") or add_contact_result.get("screenshot_path"):
        task_status = str(add_contact_result.get("task_status") or "")
        result_code = str(add_contact_result.get("result_code") or "")
        terminal_ok = task_status == "completed" or result_code == "already_friend"
        add(
            make_step_event(
                step_id="add_contact_search_terminal" if terminal_ok else "add_contact_search_failure",
                title="搜索结果终态判定" if terminal_ok else "搜索结果失败判定",
                status="completed" if terminal_ok else "failed",
                state_before="search_result_checked",
                state_after=str(add_contact_result.get("state") or "search_failed"),
                ocr_items=_list(add_contact_result.get("ocr_items")),
                targets=_list(add_contact_result.get("targets")),
                artifacts=_artifacts(add_contact_result),
                result={
                    **_result_fields(add_contact_result),
                    "not_found": add_contact_result.get("not_found"),
                },
            )
        )

    add_contact_after = _dict(add_contact_result.get("after"))
    if add_contact_after:
        add(
            make_step_event(
                step_id="add_contact_entry_after_click",
                title="点击添加到通讯录后复核",
                status="completed" if not add_contact_result.get("error_code") else "failed",
                state_before="add_contact_entry_detected",
                state_after=str(add_contact_result.get("state") or "invite_form_probe"),
                ocr_items=_list(add_contact_after.get("ocr_items")),
                targets=_list(add_contact_after.get("targets")),
                selected_target=_first_target(add_contact_after.get("targets")),
                artifacts=_artifacts(add_contact_after),
                result={
                    **_result_fields(add_contact_result),
                    "click": add_contact_result.get("click"),
                    "invite_form_probe": add_contact_result.get("invite_form_probe"),
                },
            )
        )

    invite_form = _dict(add_contact_result.get("invite_form"))
    if invite_form:
        _add_invite_form_events(add, invite_form)


def _add_invite_form_events(add: Any, invite_form: dict[str, Any]) -> None:
    invite_before = _dict(invite_form.get("before"))
    if invite_before:
        add(
            make_step_event(
                step_id="invite_form_before_fill",
                title="申请表单填写前定位",
                status="completed",
                state_before="invite_form_opened",
                state_after=str(invite_form.get("state") or "invite_form_ready"),
                ocr_items=_list(invite_before.get("ocr_items")),
                targets=_list(invite_before.get("targets")),
                selected_target=_first_target(invite_before.get("targets")),
                artifacts=_artifacts(invite_before),
                result=_invite_fields(invite_form),
            )
        )

    invite_filled = _dict(invite_form.get("filled"))
    if invite_filled:
        add(
            make_step_event(
                step_id="invite_form_after_fill_before_confirm",
                title="申请表单填写后确定前复核",
                status="completed",
                state_before="invite_form_ready",
                state_after="invite_form_filled",
                ocr_items=_list(invite_filled.get("ocr_items")),
                targets=_list(invite_filled.get("targets")),
                selected_target=_first_target(invite_filled.get("targets")),
                artifacts=_artifacts(invite_filled),
                result={
                    **_invite_fields(invite_form),
                    "greeting": invite_form.get("greeting"),
                    "remark_fill": invite_form.get("remark_fill"),
                },
            )
        )

    invite_after = _dict(invite_form.get("after"))
    if invite_after:
        add(
            make_step_event(
                step_id="invite_confirm_after_click",
                title="点击确定后结果复核",
                status="completed" if invite_form.get("task_status") == "completed" else "failed",
                state_before="invite_form_filled",
                state_after=str(invite_form.get("state") or "invite_confirm_checked"),
                ocr_items=_list(invite_after.get("ocr_items")),
                artifacts=_artifacts(invite_after),
                result={
                    **_invite_fields(invite_form),
                    **_result_fields(invite_form),
                    "confirm": invite_form.get("confirm"),
                    "final_status": invite_after.get("final_status"),
                },
            )
        )


def _payload_validation_event(payload: dict[str, Any]) -> dict[str, Any]:
    return make_step_event(
        step_id="payload_validation",
        title="字段契约校验",
        status="failed",
        state_before="task_received",
        state_after="task_payload_invalid",
        result={
            "ok": False,
            "task_status": payload.get("task_status"),
            "error_code": payload.get("error_code"),
            "verify_message": payload.get("verify_message"),
            "remark_name": payload.get("remark_name"),
            "remark_code": payload.get("remark_code"),
            "remark_code_valid": payload.get("remark_code_valid"),
            "validation_errors": payload.get("validation_errors") or [],
            "legacy_remark_fallback": payload.get("legacy_remark_fallback"),
            "wechat_ui_action_attempted": False,
        },
    )


def _invite_fields(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": source.get("state"),
        "verify_message": source.get("verify_message"),
        "remark_name": source.get("remark_name"),
        "remark_code": source.get("remark_code"),
        "remark_code_valid": source.get("remark_code_valid"),
        "validation_errors": source.get("validation_errors") or [],
        "legacy_remark_fallback": source.get("legacy_remark_fallback"),
    }


def _result_fields(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": source.get("state"),
        "task_status": source.get("task_status"),
        "result_code": source.get("result_code"),
        "error_code": source.get("error_code"),
        "current_step": source.get("current_step"),
        "server_report_payload": source.get("server_report_payload"),
    }


def _artifacts(source: dict[str, Any]) -> dict[str, str]:
    return {
        "raw": str(source.get("screenshot_path") or ""),
        "annotated": str(source.get("annotated_path") or ""),
    }


def _first_target(value: Any) -> dict[str, Any]:
    targets = _list(value)
    for target in targets:
        if isinstance(target, dict):
            return dict(target)
    return {}


def _is_validation_failure(payload: dict[str, Any]) -> bool:
    return bool(payload.get("validation_errors")) or payload.get("state") == "task_payload_invalid"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
