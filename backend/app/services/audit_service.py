from datetime import datetime
import logging
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import Select, cast, func, select
from sqlalchemy import Text
from sqlalchemy.orm import Session

from app.core.request_context import ActorContext
from app.core.database import SessionLocal
from app.errors import AppError
from app.models.audit import OperationLog
from app.models.lead import Lead


EVENT_NAMES: dict[str, str] = {
    "lead_created": "新增客户",
    "lead_updated": "编辑客户",
    "lead_marked_invalid": "标记无效",
    "lead_restored": "恢复线索",
    "lead_auto_assigned": "轮询分配",
    "lead_assign_failed": "轮询分配失败",
    "lead_retry_assign": "重新分配线索",
    "duplicate_detected": "重复手机号录入",
    "duplicate_note_appended": "追加备注",
    "phone_revealed": "查看完整手机号",
    "sales_created": "新增销售",
    "sales_updated": "编辑销售",
    "sales_enabled_changed": "启用/停用销售",
    "sales_worker_bound": "绑定销售 Worker",
    "sales_worker_unbound": "清空销售 Worker",
    "worker_created": "新增 Worker",
    "worker_updated": "编辑 Worker",
    "worker_enabled_changed": "启用/停用 Worker",
    "worker_binding_reset": "重置 Worker 绑定",
    "worker_inflight_failed_before_message_action": "读取动作前失败结算",
    "worker_inflight_read_failed_no_fact": "读取无可信事实失败结算",
    "worker_inflight_read_retry_scheduled": "读取重读已调度",
    "worker_inflight_technical_failed": "客户端技术故障结算",
    "worker_legacy_media_recovery_settled": "旧媒体恢复已结算",
    "worker_legacy_media_owner_unknown": "旧媒体归属待人工检查",
    "leads_exported": "导出选中线索",
    "task_created": "创建任务",
    "task_unblocked": "解除任务阻塞",
    "task_cancelled": "取消任务",
    "task_comment_added": "补充任务备注",
    "vehicle_created": "新增车辆",
    "vehicle_updated": "编辑车辆",
    "vehicle_listed": "上架车辆",
    "vehicle_unlisted": "下架车辆",
    "vehicle_image_uploaded": "上传车辆图片",
    "vehicle_image_reordered": "调整车辆图片顺序",
    "vehicle_image_deleted": "删除车辆图片",
    "vehicle_excel_import_confirmed": "确认导入车辆 Excel",
    "vehicle_operation_failed": "车辆操作失败",
    "admin_account_created": "创建后台账号",
    "admin_account_enabled": "启用后台账号",
    "admin_account_disabled": "停用后台账号",
    "admin_password_reset": "重置后台账号密码",
    "admin_login_succeeded": "后台登录成功",
    "admin_login_failed": "后台登录失败",
    "admin_logout": "退出后台",
    "wechat_binding_restored": "恢复微信监听",
    "sales_feishu_open_id_matched": "销售飞书身份匹配成功",
    "sales_feishu_open_id_sync_failed": "销售飞书身份匹配失败",
    "sales_feishu_open_id_stale_discarded": "丢弃陈旧销售飞书身份结果",
    "handoff_feishu_notify_succeeded": "转人工飞书通知成功",
    "handoff_feishu_notify_failed": "转人工飞书通知失败",
}

FAILED_EVENTS = {
    "lead_assign_failed",
    "admin_login_failed",
    "vehicle_operation_failed",
    "sales_feishu_open_id_sync_failed",
    "handoff_feishu_notify_failed",
    "worker_legacy_media_owner_unknown",
}
logger = logging.getLogger(__name__)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _result_for(log: OperationLog) -> str:
    return "failed" if log.event_type in FAILED_EVENTS else "success"


def _summary_for(log: OperationLog, lead_name: str | None) -> str:
    metadata = log.extra_metadata or {}
    before = log.before_data or {}
    after = log.after_data or {}
    if log.event_type in {"lead_auto_assigned", "lead_retry_assign"} and metadata.get("sales_name"):
        return f"轮询分配给 {metadata['sales_name']}"
    if log.event_type == "lead_assign_failed":
        return str(metadata.get("reason") or "轮询分配失败")
    if log.event_type == "phone_revealed":
        suffix = metadata.get("phone_suffix")
        return f"查看手机号后四位 {suffix}" if suffix else "查看完整手机号"
    if log.event_type == "duplicate_detected":
        phone = metadata.get("submitted_phone_masked")
        return f"重复录入手机号 {phone}" if phone else "重复手机号录入"
    if log.event_type == "duplicate_note_appended":
        return "本次备注已追加到原线索"
    if log.event_type == "sales_enabled_changed":
        before_enabled = before.get("enabled")
        after_enabled = after.get("enabled")
        if before_enabled is not None and after_enabled is not None:
            return f"销售状态从{'启用' if before_enabled else '停用'}改为{'启用' if after_enabled else '停用'}"
    if log.event_type == "worker_enabled_changed":
        before_enabled = before.get("enabled")
        after_enabled = after.get("enabled")
        if before_enabled is not None and after_enabled is not None:
            return f"Worker 状态从{'启用' if before_enabled else '停用'}改为{'启用' if after_enabled else '停用'}"
    if log.event_type == "sales_worker_bound":
        return f"绑定 Worker：{after.get('worker_id')}"
    if log.event_type == "sales_worker_unbound":
        return "清空销售 Worker 绑定"
    if log.event_type == "worker_binding_reset":
        return "重置 Worker Token，客户端需重新绑定"
    if log.event_type == "leads_exported":
        count = metadata.get("selected_count")
        return f"导出 {count} 条线索" if count is not None else "导出选中线索"
    if log.event_type == "task_unblocked":
        return "销售已绑定 Worker，任务恢复为 pending"
    if log.event_type == "worker_legacy_media_owner_unknown":
        return "旧媒体记录无法确认客户归属，需要人工检查"
    if lead_name:
        return f"操作对象：{lead_name}"
    return EVENT_NAMES.get(log.event_type, log.event_type)


def write_log(
    db: Session,
    actor: ActorContext,
    *,
    event_type: str,
    module: str,
    target_type: str,
    target_id: str | None = None,
    lead_id: str | None = None,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> OperationLog:
    log = OperationLog(
        event_type=event_type,
        module=module,
        target_type=target_type,
        target_id=target_id,
        lead_id=lead_id,
        operator_id=str(actor.operator_id),
        operator_name_snapshot=actor.operator_name,
        ip_address=actor.ip_address,
        user_agent=actor.user_agent,
        request_id=actor.request_id,
        before_data=jsonable_encoder(before_data) if before_data is not None else None,
        after_data=jsonable_encoder(after_data) if after_data is not None else None,
        extra_metadata=jsonable_encoder(metadata or {}),
    )
    db.add(log)
    return log


def write_system_log(
    db: Session,
    *,
    event_type: str,
    module: str,
    target_type: str,
    target_id: str | None = None,
    lead_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> OperationLog:
    log = OperationLog(
        event_type=event_type,
        module=module,
        target_type=target_type,
        target_id=target_id,
        lead_id=lead_id,
        operator_id=None,
        operator_name_snapshot="system",
        ip_address=None,
        user_agent=None,
        request_id=None,
        before_data=None,
        after_data=None,
        extra_metadata=jsonable_encoder(metadata or {}),
    )
    db.add(log)
    return log


def record_vehicle_operation_failure(
    actor: ActorContext,
    *,
    operation: str,
    target_id: str | None,
    error: Exception,
) -> None:
    error_code = error.code if isinstance(error, AppError) else "INTERNAL_ERROR"
    status_code = error.status_code if isinstance(error, AppError) else 500
    try:
        with SessionLocal() as audit_db:
            write_log(
                audit_db,
                actor,
                event_type="vehicle_operation_failed",
                module="vehicles",
                target_type="vehicle",
                target_id=target_id,
                metadata={
                    "operation": operation,
                    "error_code": error_code,
                    "error_type": type(error).__name__,
                    "status_code": status_code,
                },
            )
            audit_db.commit()
    except Exception:
        logger.exception(
            "vehicle failure audit persistence failed operation=%s request_id=%s",
            operation,
            actor.request_id,
        )


def build_log_query(
    *,
    keyword: str | None = None,
    event_type: str | None = None,
    module: str | None = None,
    operator_id: str | None = None,
    operator_name: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    lead_id: str | None = None,
    result: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> Select:
    query = select(OperationLog, Lead.customer_name).outerjoin(Lead, OperationLog.lead_id == Lead.id)
    if keyword:
        like = f"%{keyword}%"
        query = query.where(
            (Lead.customer_name.ilike(like))
            | (OperationLog.operator_name_snapshot.ilike(like))
            | (OperationLog.target_id.ilike(like))
            | (cast(OperationLog.extra_metadata, Text).ilike(like))
            | (cast(OperationLog.before_data, Text).ilike(like))
            | (cast(OperationLog.after_data, Text).ilike(like))
        )
    if event_type:
        query = query.where(OperationLog.event_type == event_type)
    if module:
        query = query.where(OperationLog.module == module)
    if operator_id:
        query = query.where(OperationLog.operator_id == operator_id)
    if operator_name:
        query = query.where(OperationLog.operator_name_snapshot.ilike(f"%{operator_name}%"))
    if target_type:
        query = query.where(OperationLog.target_type == target_type)
    if target_id:
        query = query.where(OperationLog.target_id == target_id)
    if lead_id:
        query = query.where(OperationLog.lead_id == lead_id)
    if result == "failed":
        query = query.where(OperationLog.event_type.in_(FAILED_EVENTS))
    elif result == "success":
        query = query.where(OperationLog.event_type.not_in(FAILED_EVENTS))
    start_at = _parse_datetime(created_from)
    end_at = _parse_datetime(created_to)
    if start_at:
        query = query.where(OperationLog.created_at >= start_at)
    if end_at:
        query = query.where(OperationLog.created_at <= end_at)
    return query.order_by(OperationLog.created_at.desc())


def paginate_logs(db: Session, query: Select, page: int, page_size: int) -> dict[str, Any]:
    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = db.scalar(count_query) or 0
    rows = db.execute(query.offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "items": [
            {
                "id": log.id,
                "event_type": log.event_type,
                "event_name": EVENT_NAMES.get(log.event_type, log.event_type),
                "event_label": EVENT_NAMES.get(log.event_type, log.event_type),
                "module": log.module,
                "target_type": log.target_type,
                "target_id": log.target_id,
                "lead_id": log.lead_id,
                "lead_customer_name": lead_name,
                "operator_id": log.operator_id,
                "operator_name": log.operator_name_snapshot,
                "ip_address": log.ip_address,
                "user_agent": log.user_agent,
                "request_id": log.request_id,
                "before_data": log.before_data,
                "after_data": log.after_data,
                "result": _result_for(log),
                "summary": _summary_for(log, lead_name),
                "created_at": log.created_at,
                "metadata": log.extra_metadata,
            }
            for log, lead_name in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }
