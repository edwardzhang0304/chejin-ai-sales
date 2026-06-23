"""Payload builders for add_friend task-level outcomes."""

from __future__ import annotations

from typing import Any

from apps.wechat_ai_customer_service.adapters.add_friend_contract import normalize_add_friend_query
from apps.wechat_ai_customer_service.adapters.add_friend_result_mapping import (
    ERROR_ADD_CONTACT_ENTRY_NOT_FOUND,
    ERROR_INVITE_FORM_WINDOW_NOT_FOUND,
    ERROR_TASK_PAYLOAD_INVALID,
    add_friend_after_confirm_result,
    add_friend_failed_result,
    add_friend_search_not_found_result,
    add_friend_server_report_payload,
)


def add_friend_task_payload_from_result(
    *,
    result: dict[str, Any],
    phone: Any = "",
    wechat: Any = "",
    verify_message: Any = "",
    remark_name: Any = "",
    remark_code: Any = "",
    remark_code_valid: bool | None = None,
    validation_errors: list[dict[str, Any]] | None = None,
    plan_path: str = "",
    adapter: str = "win32_ocr",
    online: bool = True,
    legacy_remark_fallback: bool = False,
    timings: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a stable task-level payload from a mapped add_friend result."""
    task_status = str(result.get("task_status") or "")
    payload: dict[str, Any] = {
        "ok": task_status == "completed",
        "online": bool(online),
        "adapter": str(adapter or "win32_ocr"),
        "state": str(result.get("state") or ""),
        "task_type": "add_friend",
        "task_status": task_status,
        "result_code": str(result.get("result_code") or ""),
        "error_code": str(result.get("error_code") or ""),
        "current_step": str(result.get("current_step") or ""),
        "server_report_payload": result.get("server_report_payload") or {},
        "query": normalize_add_friend_query(phone=phone, wechat=wechat),
        "phone": str(phone or ""),
        "wechat": str(wechat or ""),
        "verify_message": str(verify_message or ""),
        "remark_name": str(remark_name or ""),
        "remark_code": str(remark_code or ""),
        "remark_code_valid": bool(remark_code_valid) if remark_code_valid is not None else None,
        "validation_errors": validation_errors or [],
        "legacy_remark_fallback": bool(legacy_remark_fallback),
        "plan_path": str(plan_path or ""),
        "timings": timings or [],
    }
    for key, value in result.items():
        if key not in payload:
            payload[key] = value
    payload.update(extra)
    return payload


def add_friend_after_confirm_payload(
    *,
    confirm_ok: bool,
    surface_text: str,
    invite_form_detected: bool = False,
    phone: Any = "",
    wechat: Any = "",
    verify_message: Any = "",
    remark_name: Any = "",
    remark_code: Any = "",
    remark_code_valid: bool | None = None,
    plan_path: str = "",
    timings: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the task payload after clicking invite confirm."""
    return add_friend_task_payload_from_result(
        result=add_friend_after_confirm_result(
            confirm_ok=confirm_ok,
            surface_text=surface_text,
            invite_form_detected=invite_form_detected,
        ),
        phone=phone,
        wechat=wechat,
        verify_message=verify_message,
        remark_name=remark_name,
        remark_code=remark_code,
        remark_code_valid=remark_code_valid,
        plan_path=plan_path,
        timings=timings,
        **extra,
    )


def add_friend_phone_not_found_payload(
    *,
    query: str,
    not_found: dict[str, Any],
    screenshot_path: str,
    annotated_path: str,
    ocr_items: list[dict[str, Any]],
    phone: Any = "",
    wechat: Any = "",
    verify_message: Any = "",
    remark_name: Any = "",
    remark_code: Any = "",
    remark_code_valid: bool | None = None,
    plan_path: str = "",
    timings: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the official failed + PHONE_NOT_FOUND task payload."""
    payload = add_friend_task_payload_from_result(
        result=add_friend_search_not_found_result(
            query=query,
            not_found=not_found,
            screenshot_path=screenshot_path,
            annotated_path=annotated_path,
            ocr_items=ocr_items,
        ),
        phone=phone or query,
        wechat=wechat,
        verify_message=verify_message,
        remark_name=remark_name,
        remark_code=remark_code,
        remark_code_valid=remark_code_valid,
        plan_path=plan_path,
        timings=timings,
        **extra,
    )
    payload["query"] = str(query or payload.get("query") or "")
    return payload


def add_friend_add_contact_entry_not_found_payload(
    *,
    phone: Any = "",
    wechat: Any = "",
    verify_message: Any = "",
    remark_name: Any = "",
    remark_code: Any = "",
    remark_code_valid: bool | None = None,
    plan_path: str = "",
    timings: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the official failed + ADD_CONTACT_ENTRY_NOT_FOUND task payload."""
    return add_friend_task_payload_from_result(
        result=add_friend_failed_result(
            state="add_contact_entry_not_found",
            error_code=ERROR_ADD_CONTACT_ENTRY_NOT_FOUND,
            current_step="searching_contact",
        ),
        phone=phone,
        wechat=wechat,
        verify_message=verify_message,
        remark_name=remark_name,
        remark_code=remark_code,
        remark_code_valid=remark_code_valid,
        plan_path=plan_path,
        timings=timings,
        **extra,
    )


def add_friend_invite_form_window_not_found_payload(
    *,
    phone: Any = "",
    wechat: Any = "",
    verify_message: Any = "",
    remark_name: Any = "",
    remark_code: Any = "",
    remark_code_valid: bool | None = None,
    plan_path: str = "",
    timings: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the official failed + INVITE_FORM_WINDOW_NOT_FOUND task payload."""
    return add_friend_task_payload_from_result(
        result=add_friend_failed_result(
            state="invite_form_window_not_found",
            error_code=ERROR_INVITE_FORM_WINDOW_NOT_FOUND,
            current_step="add_contact_entry_clicked",
        ),
        phone=phone,
        wechat=wechat,
        verify_message=verify_message,
        remark_name=remark_name,
        remark_code=remark_code,
        remark_code_valid=remark_code_valid,
        plan_path=plan_path,
        timings=timings,
        **extra,
    )


def add_friend_task_payload_invalid(
    *,
    phone: Any,
    wechat: Any,
    validation: dict[str, Any],
    plan_path: str,
    probe: dict[str, Any] | None = None,
    adapter: str = "win32_ocr",
) -> dict[str, Any]:
    """Build the stable task result returned when formal add_friend fields are invalid."""
    return {
        "ok": False,
        "online": False,
        "adapter": str(adapter or "win32_ocr"),
        "state": "task_payload_invalid",
        "task_type": "add_friend",
        "task_status": "failed",
        "result_code": "",
        "error_code": ERROR_TASK_PAYLOAD_INVALID,
        "current_step": "payload_validation",
        "server_report_payload": add_friend_server_report_payload(
            task_status="failed",
            error_code=ERROR_TASK_PAYLOAD_INVALID,
            current_step="payload_validation",
        ),
        "query": normalize_add_friend_query(phone=phone, wechat=wechat),
        "phone": str(phone or ""),
        "wechat": str(wechat or ""),
        "verify_message": validation.get("verify_message"),
        "remark_name": validation.get("remark_name"),
        "remark_code": validation.get("remark_code"),
        "remark_code_valid": validation.get("remark_code_valid"),
        "validation_errors": validation.get("validation_errors") or [],
        "legacy_remark_fallback": False,
        "wechat_ui_action_attempted": False,
        "window_probe": probe or {},
        "plan_path": str(plan_path or ""),
        "note": "formal add_friend fields are invalid; no WeChat UI action was attempted",
        "timings": [],
    }
