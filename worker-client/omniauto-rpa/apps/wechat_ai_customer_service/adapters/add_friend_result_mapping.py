"""Result mapping for add_friend RPA outcomes."""

from __future__ import annotations

from typing import Any


RESULT_INVITE_SENT = "invite_sent"
RESULT_ALREADY_FRIEND = "already_friend"

ERROR_TASK_PAYLOAD_INVALID = "TASK_PAYLOAD_INVALID"
ERROR_PHONE_NOT_FOUND = "PHONE_NOT_FOUND"
ERROR_WECHAT_ID_NOT_FOUND = "WECHAT_ID_NOT_FOUND"
ERROR_ACCOUNT_RESTRICTED = "ACCOUNT_RESTRICTED"
ERROR_PLUS_ENTRY_NOT_FOUND = "PLUS_ENTRY_NOT_FOUND"
ERROR_PLUS_ENTRY_POPUP_NOT_DETECTED = "PLUS_ENTRY_POPUP_NOT_DETECTED"
ERROR_ADD_FRIEND_MENU_CLICK_FAILED = "ADD_FRIEND_MENU_CLICK_FAILED"
ERROR_ADD_CONTACT_ENTRY_NOT_FOUND = "ADD_CONTACT_ENTRY_NOT_FOUND"
ERROR_INVITE_FORM_WINDOW_NOT_FOUND = "INVITE_FORM_WINDOW_NOT_FOUND"
ERROR_INVITE_CONFIRM_CLICK_FAILED = "INVITE_CONFIRM_CLICK_FAILED"
ERROR_INVITE_FIELD_VERIFICATION_FAILED = "INVITE_FIELD_VERIFICATION_FAILED"
ERROR_WECHAT_WINDOW_NOT_READY = "WECHAT_WINDOW_NOT_READY"
ERROR_OPERATOR_GUARD_NOT_READY = "OPERATOR_GUARD_NOT_READY"


def add_friend_server_report_payload(
    *,
    task_status: str | None = None,
    result_code: str | None = None,
    error_code: str | None = None,
    current_step: str | None = None,
) -> dict[str, str]:
    payload: dict[str, str] = {}
    if task_status:
        payload["task.status"] = str(task_status)
    if result_code:
        payload["task.result_code"] = str(result_code)
    if error_code:
        payload["task.error_code"] = str(error_code)
    if current_step:
        payload["task.current_step"] = str(current_step)
    return payload


def add_friend_completed_result(
    *,
    state: str,
    result_code: str,
    current_step: str = "task_completed",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "ok": True,
        "state": state,
        "task_status": "completed",
        "result_code": result_code,
        "error_code": "",
        "current_step": current_step,
        "server_report_payload": add_friend_server_report_payload(
            task_status="completed",
            result_code=result_code,
            current_step=current_step,
        ),
        **extra,
    }


def add_friend_failed_result(
    *,
    state: str,
    error_code: str,
    current_step: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "ok": False,
        "state": state,
        "task_status": "failed",
        "result_code": "",
        "error_code": error_code,
        "current_step": current_step,
        "server_report_payload": add_friend_server_report_payload(
            task_status="failed",
            error_code=error_code,
            current_step=current_step,
        ),
        **extra,
    }


def add_friend_after_confirm_result(
    *,
    confirm_ok: bool,
    surface_text: str,
    invite_form_detected: bool = False,
) -> dict[str, Any]:
    text = normalize_surface_text(surface_text)
    if not confirm_ok:
        return add_friend_failed_result(
            state="invite_confirm_click_failed",
            error_code=ERROR_INVITE_CONFIRM_CLICK_FAILED,
            current_step="invite_confirming",
        )
    if has_any(text, ("操作频繁", "账号异常", "账号安全", "被限制", "限制使用", "稍后再试")):
        return add_friend_failed_result(
            state="account_restricted",
            error_code=ERROR_ACCOUNT_RESTRICTED,
            current_step="invite_confirm_clicked",
        )
    if has_any(text, ("发送成功", "已发送", "等待验证", "朋友验证已发送", "验证申请已发送", "申请已发送")):
        return add_friend_completed_result(state=RESULT_INVITE_SENT, result_code=RESULT_INVITE_SENT)
    if invite_form_detected or has_any(text, ("申请添加朋友", "发送添加朋友申请", "确定", "取消")):
        return add_friend_completed_result(
            state=RESULT_INVITE_SENT,
            result_code=RESULT_INVITE_SENT,
            result_basis="confirm_click_ok_no_failure_signal",
            observation="invite_form_still_visible_after_confirm",
        )
    return add_friend_completed_result(
        state=RESULT_INVITE_SENT,
        result_code=RESULT_INVITE_SENT,
        result_basis="confirm_click_ok_no_failure_signal",
        observation="no_explicit_invite_sent_signal",
    )


def add_friend_search_not_found_result(
    *,
    query: str,
    not_found: dict[str, Any],
    screenshot_path: str,
    annotated_path: str,
    ocr_items: list[dict[str, Any]],
) -> dict[str, Any]:
    return add_friend_failed_result(
        state="phone_not_found",
        error_code=ERROR_PHONE_NOT_FOUND,
        current_step="searching_phone",
        query=query,
        not_found=not_found,
        screenshot_path=screenshot_path,
        annotated_path=annotated_path,
        targets=[],
        ocr_items=ocr_items,
    )


def normalize_surface_text(text: str) -> str:
    return str(text or "").replace(" ", "").replace("\n", "")


def has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(normalize_surface_text(token) in text for token in tokens)
