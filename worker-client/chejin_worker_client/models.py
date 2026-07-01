from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Literal


RunStatus = Literal["running", "paused"]
RpaStatus = Literal["ready", "unavailable"]
WechatStatus = Literal["logged_in", "not_found", "logged_out", "unknown"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Binding:
    worker_id: str
    worker_token: str
    client_instance_id: str
    run_status: RunStatus = "paused"
    bound_at: str = field(default_factory=utc_now_iso)


@dataclass
class WorkerProfile:
    id: str
    worker_name: str
    run_status: RunStatus
    online_status: str | None = None
    running_status: str | None = None
    rpa_component_status: str | None = None
    wechat_status: str | None = None
    client_binding_state: str | None = None
    current_task: str | None = None
    current_step: str | None = None
    local_lock_summary: dict[str, Any] = field(default_factory=dict)
    last_heartbeat_at: str | None = None
    bound_sales_name: str | None = None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "WorkerProfile":
        return cls(
            id=str(payload.get("id") or ""),
            worker_name=str(payload.get("worker_name") or "车金 Worker"),
            run_status=str(payload.get("run_status") or "paused"),  # type: ignore[arg-type]
            online_status=payload.get("online_status"),
            running_status=payload.get("running_status"),
            rpa_component_status=payload.get("rpa_component_status"),
            wechat_status=payload.get("wechat_status"),
            client_binding_state=payload.get("client_binding_state"),
            current_task=payload.get("current_task"),
            current_step=payload.get("current_step"),
            local_lock_summary=payload.get("local_lock_summary") if isinstance(payload.get("local_lock_summary"), dict) else {},
            last_heartbeat_at=payload.get("last_heartbeat_at"),
            bound_sales_name=payload.get("bound_sales_name"),
        )


@dataclass
class Task:
    id: str
    task_type: str
    status: str
    current_step: str | None = None
    customer_name: str | None = None
    phone: str | None = None
    wechat: str | None = None
    sales_name: str | None = None
    remark: str | None = None
    verify_message: str | None = None
    remark_name: str | None = None
    remark_code: str | None = None
    remark_code_valid: bool | None = None
    result_code: str | None = None
    error_code: str | None = None
    reply_action_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_searchable_contact(self) -> bool:
        return bool(self.search_phone or self.wechat)

    @property
    def search_phone(self) -> str | None:
        if not self.phone:
            return None
        digits = re.sub(r"\D", "", self.phone)
        if "*" in self.phone or len(digits) < 7:
            return None
        return digits

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "Task":
        lead = ((payload.get("business_object") or {}).get("lead") or {}) if isinstance(payload.get("business_object"), dict) else {}
        execution = payload.get("execution") or {}
        sales = execution.get("sales") or {}
        return cls(
            id=str(payload.get("id") or ""),
            task_type=str(payload.get("task_type") or "add_friend"),
            status=str(payload.get("status") or ""),
            current_step=payload.get("current_step") or execution.get("current_step"),
            customer_name=payload.get("customer_name") or lead.get("customer_name"),
            phone=payload.get("primary_phone")
            or lead.get("primary_phone")
            or payload.get("phone_plain")
            or lead.get("phone_plain")
            or payload.get("primary_phone_masked")
            or lead.get("primary_phone_masked"),
            wechat=payload.get("wechat") or lead.get("wechat") or payload.get("lead_wechat"),
            sales_name=payload.get("sales_name") or sales.get("sales_name"),
            remark=payload.get("remark") or lead.get("remark"),
            verify_message=payload.get("verify_message"),
            remark_name=payload.get("remark_name"),
            remark_code=payload.get("remark_code"),
            remark_code_valid=payload.get("remark_code_valid"),
            result_code=payload.get("result_code"),
            error_code=payload.get("error_code"),
            reply_action_id=payload.get("reply_action_id"),
            raw=payload,
        )


@dataclass
class ReplySendClaim:
    reply_action_id: str
    task_id: str
    send_token: str
    reply_text: str
    reply_text_hash: str | None
    conversation_id: str
    rpa_session_key: str
    expire_at: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "ReplySendClaim":
        return cls(
            reply_action_id=str(payload.get("reply_action_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            send_token=str(payload.get("send_token") or ""),
            reply_text=str(payload.get("reply_text") or ""),
            reply_text_hash=payload.get("reply_text_hash"),
            conversation_id=str(payload.get("conversation_id") or ""),
            rpa_session_key=str(payload.get("rpa_session_key") or ""),
            expire_at=payload.get("expire_at"),
            raw=payload,
        )


@dataclass
class RpaStep:
    current_step: str
    title: str
    remark: str
    evidence_path: str | None = None


@dataclass
class RpaResult:
    ok: bool
    result_code: str | None = None
    error_code: str | None = None
    failure_step: str | None = None
    message: str = ""
    evidence_path: str | None = None
    evidence_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WechatReadTarget:
    conversation_id: str
    rpa_session_key: str
    display_name: str
    remark_code: str | None = None
    row_fingerprint: dict[str, Any] = field(default_factory=dict)
    ocr_confidence: float | None = None
    lead_id: str | None = None
    sales_id: str | None = None
    read_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "WechatReadTarget":
        raw_confidence = payload.get("ocr_confidence")
        try:
            ocr_confidence = float(raw_confidence) if raw_confidence is not None else None
        except (TypeError, ValueError):
            ocr_confidence = None
        raw_fingerprint = payload.get("row_fingerprint")
        row_fingerprint = raw_fingerprint if isinstance(raw_fingerprint, dict) else {"value": str(raw_fingerprint or "")} if raw_fingerprint else {}
        return cls(
            conversation_id=str(payload.get("conversation_id") or ""),
            rpa_session_key=str(payload.get("rpa_session_key") or ""),
            display_name=str(payload.get("display_name") or ""),
            remark_code=str(payload.get("remark_code") or "").strip() or None,
            row_fingerprint=row_fingerprint,
            ocr_confidence=ocr_confidence,
            lead_id=payload.get("lead_id"),
            sales_id=payload.get("sales_id"),
            read_reason=payload.get("read_reason"),
            raw=payload,
        )
