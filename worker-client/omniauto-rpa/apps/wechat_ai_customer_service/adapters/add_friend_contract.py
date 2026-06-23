"""Formal add_friend task payload contract."""

from __future__ import annotations

import re
from typing import Any


ADD_FRIEND_ENTRY_CLICK_WINDOWS_ROUTE = "add-friend-entry-click-plan-windows"
ADD_FRIEND_ENTRY_CLICK_REQUIRED_FIELDS = ("phone_or_wechat", "verify_message", "remark_name", "remark_code")

VALIDATION_ERROR_REQUIRED = "REQUIRED"
VALIDATION_ERROR_REMARK_CODE_MISSING = "REMARK_CODE_MISSING"


def normalize_add_friend_query(*, phone: Any = "", wechat: Any = "") -> str:
    """Return the canonical add_friend search query, preferring phone digits."""
    phone_digits = re.sub(r"\D+", "", str(phone or ""))
    if phone_digits:
        return phone_digits
    return str(wechat or "").strip()


def validate_add_friend_entry_click_contract(
    *,
    phone: Any = "",
    wechat: Any = "",
    verify_message: Any,
    remark_name: Any,
    remark_code: Any,
) -> dict[str, Any]:
    """Validate the formal add_friend entry-click payload before any WeChat UI action."""
    query = normalize_add_friend_query(phone=phone, wechat=wechat)
    clean_verify_message = str(verify_message or "").strip()
    clean_remark_name = str(remark_name or "").strip()
    clean_remark_code = str(remark_code or "").strip()
    errors: list[dict[str, str]] = []
    if not query:
        errors.append(
            {
                "field": "phone_or_wechat",
                "code": VALIDATION_ERROR_REQUIRED,
                "message": "phone or wechat is required",
            }
        )
    if not clean_verify_message:
        errors.append(
            {
                "field": "verify_message",
                "code": VALIDATION_ERROR_REQUIRED,
                "message": "verify_message is required",
            }
        )
    if not clean_remark_name:
        errors.append(
            {
                "field": "remark_name",
                "code": VALIDATION_ERROR_REQUIRED,
                "message": "remark_name is required",
            }
        )
    if not clean_remark_code:
        errors.append(
            {
                "field": "remark_code",
                "code": VALIDATION_ERROR_REQUIRED,
                "message": "remark_code is required",
            }
        )
    if clean_remark_name and clean_remark_code and clean_remark_code not in clean_remark_name:
        errors.append(
            {
                "field": "remark_name",
                "code": VALIDATION_ERROR_REMARK_CODE_MISSING,
                "message": "remark_name must include remark_code",
            }
        )
    return {
        "ok": not errors,
        "query": query,
        "phone": re.sub(r"\D+", "", str(phone or "")),
        "wechat": str(wechat or "").strip(),
        "verify_message": clean_verify_message,
        "remark_name": clean_remark_name,
        "remark_code": clean_remark_code,
        "remark_code_valid": bool(clean_remark_name and clean_remark_code and clean_remark_code in clean_remark_name),
        "validation_errors": errors,
    }


def add_friend_entry_click_contract_summary(validation: dict[str, Any]) -> dict[str, Any]:
    """Return the stable fields every report should expose for this contract."""
    return {
        "required_fields": list(ADD_FRIEND_ENTRY_CLICK_REQUIRED_FIELDS),
        "query": validation.get("query"),
        "phone": validation.get("phone"),
        "wechat": validation.get("wechat"),
        "verify_message": validation.get("verify_message"),
        "remark_name": validation.get("remark_name"),
        "remark_code": validation.get("remark_code"),
        "remark_code_valid": validation.get("remark_code_valid"),
        "validation_errors": validation.get("validation_errors") or [],
    }
