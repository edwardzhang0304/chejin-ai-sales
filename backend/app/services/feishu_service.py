from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.base import utcnow
from app.models.c3 import Conversation, HandoffEvent
from app.models.lead import Lead, LeadContact
from app.models.sales import Sales
from app.models.wechat import WechatSessionBinding
from app.services.audit_service import write_system_log
from app.services.feishu_adapter import FeishuAdapter, FeishuAdapterError, get_feishu_adapter


logger = logging.getLogger(__name__)
POST_COMMIT_SALES_SYNCS = "post_commit_sales_feishu_syncs"
POST_COMMIT_HANDOFF_NOTIFICATIONS = "post_commit_handoff_notifications"
NOTIFY_PENDING = "pending"
NOTIFY_SENDING = "sending"
NOTIFY_SUCCEEDED = "succeeded"
NOTIFY_FAILED = "failed"


HANDOFF_REASON_LABELS = {
    "CUSTOMER_HIGH_INTENT": "客户表达了明确购车意向",
    "HANDOFF_REQUIRED": "当前咨询需要销售人工处理",
    "AI_ENGINE_RETRY_EXHAUSTED": "自动回复连续失败，需要人工处理",
    "AI_ENGINE_PAUSED_FOR_MANUAL_REVIEW": "自动回复已暂停，需要人工复核",
    "C2_IMAGE_UNDERSTANDING_FAILED": "客户图片暂时无法安全理解",
    "C2_MESSAGE_HISTORY_GAP": "会话消息上下文不完整",
    "C2_REPLY_CONTEXT_RECOVERY_FAILED": "回复前会话复核失败",
    "SEND_ACK_TIMEOUT": "微信回复发送结果未确认",
    "SEND_RESULT_UNKNOWN": "微信回复发送结果未知",
}


def enqueue_sales_open_id_sync(db: Session, *, sales_id: str, normalized_phone: str) -> None:
    pending = db.info.setdefault(POST_COMMIT_SALES_SYNCS, {})
    pending[str(sales_id)] = str(normalized_phone)


def enqueue_handoff_notification(db: Session, *, handoff_event_id: str) -> None:
    pending = db.info.setdefault(POST_COMMIT_HANDOFF_NOTIFICATIONS, set())
    pending.add(str(handoff_event_id))


def clear_post_commit_effects(db: Session) -> None:
    db.info.pop(POST_COMMIT_SALES_SYNCS, None)
    db.info.pop(POST_COMMIT_HANDOFF_NOTIFICATIONS, None)


def run_post_commit_effects(db: Session) -> None:
    sales_syncs = dict(db.info.pop(POST_COMMIT_SALES_SYNCS, {}) or {})
    handoff_ids = sorted(db.info.pop(POST_COMMIT_HANDOFF_NOTIFICATIONS, set()) or set())
    for sales_id, normalized_phone in sales_syncs.items():
        try:
            sync_sales_open_id(sales_id, normalized_phone)
        except Exception:
            logger.exception("sales Feishu sync crashed sales_id=%s", sales_id)
    for handoff_event_id in handoff_ids:
        try:
            dispatch_handoff_notification(handoff_event_id)
        except Exception:
            logger.exception(
                "handoff Feishu dispatch crashed handoff_event_id=%s",
                handoff_event_id,
            )


def _safe_error_summary(value: str | None) -> str:
    summary = str(value or "provider_error").replace("\r", " ").replace("\n", " ")
    summary = re.sub(r"(?i)bearer\s+[^\s]+", "Bearer ***", summary)
    summary = re.sub(r"(?i)(app_secret|tenant_access_token|token)=?[^\s,;]*", r"\1=***", summary)
    summary = re.sub(r"\b1[3-9]\d{9}\b", "1**********", summary)
    summary = re.sub(r"\bou_[A-Za-z0-9_-]+\b", "ou_***", summary)
    return summary[:512]


def sync_sales_open_id(
    sales_id: str,
    expected_phone: str,
    *,
    adapter: FeishuAdapter | None = None,
) -> str:
    with SessionLocal() as db:
        sales = db.get(Sales, sales_id)
        if (
            not sales
            or sales.deleted_at is not None
            or str(sales.phone or "") != expected_phone
        ):
            return "stale"

    try:
        open_id = (adapter or get_feishu_adapter()).lookup_open_id(expected_phone)
    except FeishuAdapterError as exc:
        with SessionLocal() as db:
            current = db.get(Sales, sales_id)
            if (
                current
                and current.deleted_at is None
                and str(current.phone or "") == expected_phone
            ):
                current.feishu_user_id = None
                write_system_log(
                    db,
                    event_type="sales_feishu_open_id_sync_failed",
                    module="sales",
                    target_type="sales",
                    target_id=sales_id,
                    metadata={"error_code": exc.code},
                )
                db.commit()
        logger.warning("sales Feishu sync failed sales_id=%s error_code=%s", sales_id, exc.code)
        return exc.code

    with SessionLocal() as db:
        result = db.execute(
            update(Sales)
            .where(
                Sales.id == sales_id,
                Sales.phone == expected_phone,
                Sales.deleted_at.is_(None),
            )
            .values(feishu_user_id=open_id)
        )
        if result.rowcount != 1:
            write_system_log(
                db,
                event_type="sales_feishu_open_id_stale_discarded",
                module="sales",
                target_type="sales",
                target_id=sales_id,
                metadata={"result": "stale_phone_or_sales"},
            )
            db.commit()
            return "stale"
        write_system_log(
            db,
            event_type="sales_feishu_open_id_matched",
            module="sales",
            target_type="sales",
            target_id=sales_id,
            metadata={"result": "matched"},
        )
        db.commit()
    return "matched"


def backfill_sales_open_ids(*, adapter: FeishuAdapter | None = None) -> dict[str, int]:
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(Sales.id, Sales.phone).where(Sales.deleted_at.is_(None))
            ).all()
        )
        db.execute(
            update(Sales)
            .where(Sales.deleted_at.is_(None))
            .values(feishu_user_id=None)
        )
        db.commit()

    counts = {"matched": 0, "failed": 0, "stale": 0}
    for sales_id, phone in rows:
        result = sync_sales_open_id(str(sales_id), str(phone or ""), adapter=adapter)
        if result == "matched":
            counts["matched"] += 1
        elif result == "stale":
            counts["stale"] += 1
        else:
            counts["failed"] += 1
    return counts


def _settle_notification(
    handoff_event_id: str,
    *,
    status: str,
    error_code: str | None,
    error_summary: str | None,
    duration_ms: int,
) -> None:
    with SessionLocal() as db:
        event = db.scalar(
            select(HandoffEvent)
            .where(HandoffEvent.id == handoff_event_id)
            .with_for_update()
        )
        if not event or event.notify_status != NOTIFY_SENDING:
            return
        event.notify_status = status
        event.notify_completed_at = utcnow()
        event.notify_error_code = error_code
        event.notify_error_summary = _safe_error_summary(error_summary) if error_summary else None
        from app.services.observability_service import (
            process_run_id_for_handoff_event,
            record_server_stage_best_effort,
        )

        record_server_stage_best_effort(
            db,
            process_run_id=process_run_id_for_handoff_event(db, event),
            conversation_id=event.conversation_id,
            stage_name="handoff.feishu_notify",
            component="backend",
            duration_ms=duration_ms,
            status=(
                "succeeded"
                if status == NOTIFY_SUCCEEDED
                else "failed"
            ),
            error_code=error_code,
            trace_id=str(uuid.uuid4()),
            stable_key=event.id,
        )
        write_system_log(
            db,
            event_type=(
                "handoff_feishu_notify_succeeded"
                if status == NOTIFY_SUCCEEDED
                else "handoff_feishu_notify_failed"
            ),
            module="c3",
            target_type="handoff_event",
            target_id=event.id,
            lead_id=(
                db.scalar(
                    select(Conversation.lead_id).where(
                        Conversation.conversation_id == event.conversation_id
                    )
                )
            ),
            metadata={
                "notify_status": status,
                "error_code": error_code,
            },
        )
        db.commit()


def _claim_notification(handoff_event_id: str) -> bool:
    with SessionLocal() as db:
        result = db.execute(
            update(HandoffEvent)
            .where(
                HandoffEvent.id == handoff_event_id,
                HandoffEvent.notify_status == NOTIFY_PENDING,
                HandoffEvent.notify_attempted_at.is_(None),
                HandoffEvent.closed_at.is_(None),
                HandoffEvent.deleted_at.is_(None),
            )
            .values(
                notify_status=NOTIFY_SENDING,
                notify_attempted_at=utcnow(),
                notify_completed_at=None,
                notify_error_code=None,
                notify_error_summary=None,
            )
        )
        db.commit()
        return result.rowcount == 1


def _notification_context(handoff_event_id: str) -> tuple[str, str]:
    with SessionLocal() as db:
        event = db.get(HandoffEvent, handoff_event_id)
        if not event:
            raise FeishuAdapterError("FEISHU_MESSAGE_SEND_FAILED", "handoff_event_missing")
        conversation = db.get(Conversation, event.conversation_id)
        if not conversation or not conversation.sales_id:
            raise FeishuAdapterError("HANDOFF_SALES_ID_MISSING", "conversation_sales_id_missing")
        sales = db.get(Sales, conversation.sales_id)
        if not sales or sales.deleted_at is not None:
            raise FeishuAdapterError("HANDOFF_SALES_NOT_FOUND", "conversation_sales_not_found")
        open_id = str(sales.feishu_user_id or "").strip()
        if not open_id:
            raise FeishuAdapterError("FEISHU_OPEN_ID_MISSING", "sales_open_id_missing")

        binding = db.scalar(
            select(WechatSessionBinding).where(
                WechatSessionBinding.conversation_id == conversation.conversation_id,
                WechatSessionBinding.bind_status == "bound",
                WechatSessionBinding.deleted_at.is_(None),
                WechatSessionBinding.remark_code.is_not(None),
                WechatSessionBinding.remark_code != "",
            )
        )
        customer_code = str(binding.remark_code if binding else "").strip()
        if not customer_code:
            raise FeishuAdapterError(
                "FEISHU_MESSAGE_SEND_FAILED",
                "conversation_remark_code_missing",
            )

        lead = db.get(Lead, conversation.lead_id) if conversation.lead_id else None
        phone_masked = "未提供"
        if lead:
            phone_masked = str(
                db.scalar(
                    select(LeadContact.masked_value)
                    .where(
                        LeadContact.lead_id == lead.id,
                        LeadContact.contact_type == "phone",
                    )
                    .order_by(LeadContact.is_primary.desc(), LeadContact.created_at.asc())
                )
                or "未提供"
            )
        reason = HANDOFF_REASON_LABELS.get(
            str(event.handoff_reason_code or ""),
            "当前咨询需要销售人工处理",
        )
        occurred_at = event.created_at or utcnow()
        time_text = occurred_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        message = "\n".join(
            [
                "客户已转人工，请及时处理。",
                f"客户短码：{customer_code}",
                f"手机号：{phone_masked}",
                f"原因：{reason}",
                f"转人工时间：{time_text}",
                "请前往微信处理。",
            ]
        )
        return open_id, message


def dispatch_handoff_notification(
    handoff_event_id: str,
    *,
    adapter: FeishuAdapter | None = None,
) -> str:
    if not _claim_notification(handoff_event_id):
        return "not_claimed"
    started_at = time.perf_counter()
    try:
        open_id, message = _notification_context(handoff_event_id)
        (adapter or get_feishu_adapter()).send_text_message(open_id, message)
    except FeishuAdapterError as exc:
        error_code = "FEISHU_NOTIFY_RESULT_UNKNOWN" if exc.result_unknown else exc.code
        _settle_notification(
            handoff_event_id,
            status=NOTIFY_FAILED,
            error_code=error_code,
            error_summary=exc.summary,
            duration_ms=int(round((time.perf_counter() - started_at) * 1000)),
        )
        return error_code
    except Exception as exc:
        _settle_notification(
            handoff_event_id,
            status=NOTIFY_FAILED,
            error_code="FEISHU_NOTIFY_RESULT_UNKNOWN",
            error_summary=f"unexpected_exception={type(exc).__name__}",
            duration_ms=int(round((time.perf_counter() - started_at) * 1000)),
        )
        return "FEISHU_NOTIFY_RESULT_UNKNOWN"

    _settle_notification(
        handoff_event_id,
        status=NOTIFY_SUCCEEDED,
        error_code=None,
        error_summary=None,
        duration_ms=int(round((time.perf_counter() - started_at) * 1000)),
    )
    return NOTIFY_SUCCEEDED


def recover_handoff_notifications(*, adapter: FeishuAdapter | None = None) -> dict[str, int]:
    now = utcnow()
    with SessionLocal() as db:
        unknown = db.execute(
            update(HandoffEvent)
            .where(HandoffEvent.notify_status == NOTIFY_SENDING)
            .values(
                notify_status=NOTIFY_FAILED,
                notify_completed_at=now,
                notify_error_code="FEISHU_NOTIFY_RESULT_UNKNOWN",
                notify_error_summary="service_restarted_after_send_claim",
            )
        ).rowcount
        db.commit()
    pending = recover_pending_handoff_notifications_once(adapter=adapter)
    return {
        "unknown_settled": int(unknown or 0),
        "pending_attempted": int(pending["attempted"]),
    }


def recover_pending_handoff_notifications_once(
    *,
    adapter: FeishuAdapter | None = None,
    limit: int = 20,
) -> dict[str, int]:
    """Drain durable, never-attempted notifications without a service restart."""

    with SessionLocal() as db:
        pending_ids = list(
            db.scalars(
                select(HandoffEvent.id)
                .where(
                    HandoffEvent.notify_status == NOTIFY_PENDING,
                    HandoffEvent.notify_attempted_at.is_(None),
                    HandoffEvent.closed_at.is_(None),
                    HandoffEvent.deleted_at.is_(None),
                )
                .order_by(HandoffEvent.created_at.asc(), HandoffEvent.id.asc())
                .limit(max(1, int(limit)))
            ).all()
        )

    attempted = 0
    for handoff_event_id in pending_ids:
        if (
            dispatch_handoff_notification(
                handoff_event_id,
                adapter=adapter,
            )
            != "not_claimed"
        ):
            attempted += 1
    return {"examined": len(pending_ids), "attempted": attempted}
