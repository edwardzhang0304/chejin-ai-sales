from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.request_context import ActorContext
from app.enums import TaskStatus
from app.errors import AppError
from app.models.audit import OperationLog
from app.models.base import utcnow
from app.models.sales import Sales
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker, WorkerHeartbeatLog
from app.schemas.worker import (
    WorkerClientBindRequest,
    WorkerHeartbeat,
    WorkerInflightFlowFinishRequest,
    WorkerInflightFlowStartRequest,
    WorkerLegacyMediaRecoverySettleRequest,
    WorkerRunStatusRequest,
    WorkerUpdate,
)
from app.schemas.worker import WorkerCreate
from app.services.audit_service import write_log
from app.services.worker_token_service import (
    decrypt_worker_token,
    encrypt_worker_token,
    generate_worker_token,
    hash_worker_token,
)

ONLINE_TIMEOUT_SECONDS = 120
OFFLINE_TASK_NOTICE_SECONDS = 600
RUN_STATUS_VALUES = {"running", "paused", "faulted"}
RPA_COMPONENT_STATUS_VALUES = {"ready", "unavailable"}
RUNNING_TASK_STATUSES = {"running"}
RUNNING_STATUS_VALUES = {"idle", "running"}
INFLIGHT_FLOW_KINDS = {"task", "c2_read", "chat_reply"}


def _lock_worker(db: Session, worker_id: str) -> Worker:
    # Authentication may have loaded the same ORM identity before a competing
    # transaction committed. Expire it before the locking SELECT so the state
    # inspected below is always the row version acquired under FOR UPDATE.
    db.expire_all()
    worker = db.scalar(
        select(Worker)
        .where(Worker.id == worker_id, Worker.deleted_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if worker is None:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)
    return worker


def computed_online_status(worker: Worker) -> str:
    if not worker.enabled:
        return "offline"
    if not worker.last_heartbeat_at:
        return "offline"
    last_heartbeat_at = worker.last_heartbeat_at
    if last_heartbeat_at.tzinfo is None:
        last_heartbeat_at = last_heartbeat_at.replace(tzinfo=timezone.utc)
    if utcnow() - last_heartbeat_at > timedelta(seconds=ONLINE_TIMEOUT_SECONDS):
        return "offline"
    return worker.online_status


def worker_offline_seconds(worker: Worker) -> int | None:
    if not worker.last_heartbeat_at:
        return None
    last_heartbeat_at = worker.last_heartbeat_at
    if last_heartbeat_at.tzinfo is None:
        last_heartbeat_at = last_heartbeat_at.replace(tzinfo=timezone.utc)
    return max(0, int((utcnow() - last_heartbeat_at).total_seconds()))


def _bound_sales(db: Session, worker_id: str) -> Sales | None:
    return db.scalar(select(Sales).where(Sales.worker_id == worker_id, Sales.deleted_at.is_(None)))


def worker_summary(db: Session, worker: Worker | None, *, include_token: bool = False) -> dict | None:
    if not worker:
        return None
    sales = _bound_sales(db, worker.id)
    data = {
        "id": worker.id,
        "worker_name": worker.worker_name,
        "device_name": worker.device_name,
        "platform": worker.platform,
        "enabled": worker.enabled,
        "online_status": computed_online_status(worker),
        "running_status": worker.running_status,
        "run_status": worker.run_status,
        "rpa_component_status": worker.rpa_component_status,
        "wechat_status": worker.wechat_status,
        "current_task": worker.current_task,
        "current_step": worker.current_step,
        "local_lock_summary": worker.local_lock_summary,
        "inflight_flow_state": worker.inflight_flow_state or {},
        "last_heartbeat_at": worker.last_heartbeat_at,
        "client_binding_state": worker.client_binding_state,
        "client_instance_id": worker.client_instance_id,
        "bound_at": worker.bound_at,
        "offline_seconds": worker_offline_seconds(worker),
        "remark": worker.remark,
        "bound_sales_id": sales.id if sales else None,
        "bound_sales_name": sales.sales_name if sales else None,
        "created_at": worker.created_at,
        "updated_at": worker.updated_at,
    }
    if include_token:
        data["worker_token"] = decrypt_worker_token(worker.worker_token_encrypted)
    return data


def list_workers(db: Session) -> list[dict]:
    workers = list(
        db.scalars(
            select(Worker)
            .where(Worker.deleted_at.is_(None))
            .order_by(Worker.enabled.desc(), Worker.created_at.desc())
        )
    )
    return [worker_summary(db, worker, include_token=False) for worker in workers]


def get_worker_detail(db: Session, worker_id: str) -> dict:
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)
    return worker_summary(db, worker, include_token=True)


def create_worker(db: Session, payload: WorkerCreate, actor: ActorContext) -> dict:
    token = generate_worker_token()
    worker = Worker(
        worker_name=payload.worker_name,
        device_name=payload.device_name,
        platform=payload.platform,
        enabled=payload.enabled,
        run_status="paused",
        rpa_component_status="unavailable",
        client_binding_state="unbound",
        worker_token_hash=hash_worker_token(token),
        worker_token_encrypted=encrypt_worker_token(token),
        remark=payload.remark,
    )
    db.add(worker)
    db.flush()
    write_log(
        db,
        actor,
        event_type="worker_created",
        module="worker",
        target_type="worker",
        target_id=worker.id,
        after_data={"worker_name": worker.worker_name, "enabled": worker.enabled},
    )
    data = worker_summary(db, worker, include_token=False)
    data["worker_token"] = token
    return data


def update_worker(db: Session, worker_id: str, payload: WorkerUpdate, actor: ActorContext) -> dict:
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)

    before = worker_summary(db, worker, include_token=False)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(worker, key, value)
    db.flush()
    after = worker_summary(db, worker, include_token=False)
    event_type = "worker_enabled_changed" if before["enabled"] != after["enabled"] else "worker_updated"
    write_log(
        db,
        actor,
        event_type=event_type,
        module="worker",
        target_type="worker",
        target_id=worker.id,
        before_data=before,
        after_data=after,
    )
    return after


def set_worker_enabled(db: Session, worker_id: str, enabled: bool, actor: ActorContext) -> dict:
    return update_worker(db, worker_id, WorkerUpdate(enabled=enabled), actor)


def authenticate_worker_client(
    db: Session,
    worker_id: str,
    worker_token: str | None,
    client_instance_id: str | None = None,
) -> Worker:
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)
    if not worker_token or hash_worker_token(worker_token) != worker.worker_token_hash:
        raise AppError("WORKER_TOKEN_INVALID", "Worker Token 无效", 401)
    if not worker.enabled:
        raise AppError("WORKER_DISABLED", "Worker 已停用，不能上报心跳", 400)
    if worker.client_instance_id:
        if not client_instance_id:
            raise AppError("WORKER_CLIENT_INSTANCE_REQUIRED", "缺少客户端实例 ID", 401)
        if worker.client_instance_id != client_instance_id:
            raise AppError("WORKER_CLIENT_BINDING_INVALID", "客户端绑定已失效，请重新绑定", 401)
    elif worker.client_binding_state == "reset_required":
        raise AppError("WORKER_CLIENT_BINDING_RESET", "Worker 绑定已被后台重置，请重新绑定", 401)
    return worker


def bind_worker_client(db: Session, worker_id: str, payload: WorkerClientBindRequest) -> dict:
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)
    if not payload.worker_token or hash_worker_token(payload.worker_token) != worker.worker_token_hash:
        raise AppError("WORKER_TOKEN_INVALID", "Worker Token 无效", 401)
    if not worker.enabled:
        raise AppError("WORKER_DISABLED", "Worker 已停用，不能绑定客户端", 400)
    if worker.client_instance_id and worker.client_instance_id != payload.client_instance_id:
        raise AppError("WORKER_CLIENT_ALREADY_BOUND", "该 Worker 已绑定其他客户端，请联系管理员在后台重置绑定", 409)

    worker.client_instance_id = payload.client_instance_id
    worker.client_binding_state = "bound"
    worker.bound_at = worker.bound_at or utcnow()
    worker.run_status = "paused"
    db.flush()
    return worker_summary(db, worker, include_token=False)


def heartbeat_worker(db: Session, worker_id: str, worker_token: str | None, payload: WorkerHeartbeat) -> dict:
    authenticated = authenticate_worker_client(
        db, worker_id, worker_token, payload.client_instance_id
    )
    worker = _lock_worker(db, authenticated.id)

    worker.online_status = "online"
    if payload.run_status is not None:
        if payload.run_status not in RUN_STATUS_VALUES:
            raise AppError("WORKER_RUN_STATUS_INVALID", "Worker 接单状态不合法", 400)
        worker.run_status = payload.run_status
    if payload.rpa_component_status is not None:
        if payload.rpa_component_status not in RPA_COMPONENT_STATUS_VALUES:
            raise AppError("WORKER_RPA_STATUS_INVALID", "RPA 组件状态不合法", 400)
        worker.rpa_component_status = payload.rpa_component_status
    if payload.running_status not in RUNNING_STATUS_VALUES:
        raise AppError("WORKER_RUNNING_STATUS_INVALID", "Worker 执行状态仅支持 idle / running", 400)
    worker.running_status = payload.running_status
    worker.current_task = payload.current_task
    worker.current_step = payload.current_step
    worker.local_lock_summary = payload.local_lock_summary or {}
    worker.wechat_status = payload.wechat_status
    if payload.client_binding_state:
        worker.client_binding_state = payload.client_binding_state
    worker.last_heartbeat_at = utcnow()
    db.add(
        WorkerHeartbeatLog(
            worker_id=worker.id,
            client_instance_id=payload.client_instance_id,
            online_status="online",
            run_status=worker.run_status,
            rpa_component_status=worker.rpa_component_status,
            wechat_status=worker.wechat_status,
            current_task=worker.current_task,
            current_step=worker.current_step,
            local_lock_summary=worker.local_lock_summary,
            created_at=worker.last_heartbeat_at,
        )
    )
    db.flush()
    return worker_summary(db, worker, include_token=False)


def set_worker_run_status(
    db: Session,
    worker_id: str,
    worker_token: str | None,
    payload: WorkerRunStatusRequest,
) -> dict:
    authenticated = authenticate_worker_client(
        db, worker_id, worker_token, payload.client_instance_id
    )
    worker = _lock_worker(db, authenticated.id)
    if payload.run_status not in RUN_STATUS_VALUES:
        raise AppError("WORKER_RUN_STATUS_INVALID", "Worker 接单状态不合法", 400)
    worker.run_status = payload.run_status
    if payload.run_status in {"paused", "faulted"}:
        current = dict(worker.inflight_flow_state or {})
        if current.get("status") == "active" and current.get("flow_id"):
            current["status"] = "draining"
            current["pause_requested_at"] = utcnow().isoformat()
            worker.inflight_flow_state = current
    db.flush()
    return worker_summary(db, worker, include_token=False)


def start_inflight_flow(
    db: Session,
    worker: Worker,
    payload: WorkerInflightFlowStartRequest,
) -> dict:
    worker = _lock_worker(db, worker.id)
    if worker.run_status != "running":
        raise AppError("WORKER_NOT_ACCEPTING_TASKS", "Worker 已暂停，不能开始新流程", 409)
    if payload.flow_kind not in INFLIGHT_FLOW_KINDS:
        raise AppError("WORKER_INFLIGHT_FLOW_KIND_INVALID", "在途流程类型不合法", 400)
    current = dict(worker.inflight_flow_state or {})
    if current.get("flow_id"):
        if (
            current.get("flow_id") == payload.flow_id
            and current.get("flow_kind") == payload.flow_kind
            and current.get("status") in {"active", "draining"}
        ):
            return current
        raise AppError("WORKER_INFLIGHT_FLOW_CONFLICT", "Worker 已有其他在途流程", 409)
    state = {
        "status": "active",
        "flow_id": payload.flow_id,
        "flow_kind": payload.flow_kind,
        "registered_at": utcnow().isoformat(),
        "pause_requested_at": None,
    }
    worker.inflight_flow_state = state
    db.flush()
    return state


def finish_inflight_flow(
    db: Session,
    worker: Worker,
    payload: WorkerInflightFlowFinishRequest,
    actor: ActorContext,
) -> dict:
    worker = _lock_worker(db, worker.id)
    current = dict(worker.inflight_flow_state or {})
    if current.get("flow_id") != payload.flow_id:
        raise AppError("WORKER_INFLIGHT_FLOW_MISMATCH", "只能结束当前同一在途流程", 409)
    flow_kind = str(current.get("flow_kind") or "")
    if payload.terminal_kind == "task_terminal":
        if flow_kind not in {"task", "chat_reply"}:
            raise AppError(
                "WORKER_INFLIGHT_FLOW_TERMINAL_KIND_INVALID",
                "C2 读取流程不能按任务终态结束",
                409,
            )
        task = db.get(Task, payload.flow_id)
        if task is None or task.status not in {
            TaskStatus.completed.value,
            TaskStatus.failed.value,
            TaskStatus.cancelled.value,
        }:
            raise AppError(
                "WORKER_INFLIGHT_FLOW_NOT_SETTLED",
                "任务终态持久化前不能结束在途流程",
                409,
            )
    elif payload.terminal_kind == "read_confirmed":
        if flow_kind != "c2_read" or not payload.conversation_id or payload.error_code:
            raise AppError(
                "WORKER_INFLIGHT_FLOW_TERMINAL_KIND_INVALID",
                "读取确认终态字段不完整",
                409,
            )
        binding = db.scalar(
            select(WechatSessionBinding)
            .where(
                WechatSessionBinding.worker_id == worker.id,
                WechatSessionBinding.conversation_id == payload.conversation_id,
                WechatSessionBinding.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if (
            binding is None
            or binding.last_read_run_id != payload.flow_id
            or binding.last_read_completed_at is None
            or binding.last_read_result not in {"new_facts", "no_change"}
        ):
            raise AppError(
                "WORKER_INFLIGHT_FLOW_NOT_SETTLED",
                "后端尚未确认本次 C2 读取结算",
                409,
            )
    elif payload.terminal_kind == "retry_required":
        if (
            flow_kind != "c2_read"
            or not payload.conversation_id
            or payload.error_code != "C2_UNREAD_RESULT_INCONCLUSIVE"
        ):
            raise AppError(
                "WORKER_INFLIGHT_FLOW_TERMINAL_KIND_INVALID",
                "后端重读调度终态必须携带会话和标准错误码",
                409,
            )
        binding = db.scalar(
            select(WechatSessionBinding)
            .where(
                WechatSessionBinding.worker_id == worker.id,
                WechatSessionBinding.conversation_id == payload.conversation_id,
                WechatSessionBinding.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if (
            binding is None
            or binding.last_read_run_id != payload.flow_id
            or binding.last_read_completed_at is None
            or binding.last_read_result != "retry_required"
            or binding.next_read_due_at is None
        ):
            raise AppError(
                "WORKER_INFLIGHT_FLOW_NOT_SETTLED",
                "后端尚未确认本次 C2 重读调度",
                409,
            )
        write_log(
            db,
            actor,
            event_type="worker_inflight_read_retry_scheduled",
            module="worker",
            target_type="worker",
            target_id=worker.id,
            metadata={
                "flow_id": payload.flow_id,
                "conversation_id": payload.conversation_id,
                "error_code": payload.error_code,
                "next_read_due_at": binding.next_read_due_at.isoformat(),
            },
        )
    elif payload.terminal_kind == "failed_before_message_action":
        if (
            flow_kind != "c2_read"
            or not payload.conversation_id
            or not payload.error_code
        ):
            raise AppError(
                "WORKER_INFLIGHT_FLOW_TERMINAL_KIND_INVALID",
                "动作前失败终态必须携带会话和错误码",
                409,
            )
        event_exists = db.scalar(
            select(MessageEvent.id).where(
                MessageEvent.worker_id == worker.id,
                MessageEvent.conversation_id == payload.conversation_id,
                MessageEvent.read_run_id == payload.flow_id,
            ).limit(1)
        )
        if event_exists:
            raise AppError(
                "WORKER_INFLIGHT_FLOW_NOT_SETTLED",
                "已形成消息事实的读取不得声明为动作前失败",
                409,
            )
        write_log(
            db,
            actor,
            event_type="worker_inflight_failed_before_message_action",
            module="worker",
            target_type="worker",
            target_id=worker.id,
            metadata={
                "flow_id": payload.flow_id,
                "conversation_id": payload.conversation_id,
                "error_code": payload.error_code,
            },
        )
    elif payload.terminal_kind == "read_failed_no_fact":
        if (
            flow_kind != "c2_read"
            or not payload.conversation_id
            or not payload.error_code
        ):
            raise AppError(
                "WORKER_INFLIGHT_FLOW_TERMINAL_KIND_INVALID",
                "无可信事实读取失败必须携带会话和错误码",
                409,
            )
        event_exists = db.scalar(
            select(MessageEvent.id).where(
                MessageEvent.worker_id == worker.id,
                MessageEvent.conversation_id == payload.conversation_id,
                MessageEvent.read_run_id == payload.flow_id,
            ).limit(1)
        )
        if event_exists:
            raise AppError(
                "WORKER_INFLIGHT_FLOW_NOT_SETTLED",
                "已经形成消息事实的读取不能声明为无可信事实失败",
                409,
            )
        write_log(
            db,
            actor,
            event_type="worker_inflight_read_failed_no_fact",
            module="worker",
            target_type="worker",
            target_id=worker.id,
            metadata={
                "flow_id": payload.flow_id,
                "conversation_id": payload.conversation_id,
                "error_code": payload.error_code,
            },
        )
    else:
        if (
            flow_kind != "c2_read"
            or not payload.conversation_id
            or not payload.error_code
        ):
            raise AppError(
                "WORKER_INFLIGHT_FLOW_TERMINAL_KIND_INVALID",
                "技术故障终态必须携带会话和错误码",
                409,
            )
        if worker.run_status != "faulted":
            raise AppError(
                "WORKER_INFLIGHT_FLOW_NOT_SETTLED",
                "客户端进入 faulted 前不能结束技术故障流程",
                409,
            )
        if payload.error_code == "C2_UNREAD_RESULT_REPEATEDLY_INCONCLUSIVE":
            binding = db.scalar(
                select(WechatSessionBinding)
                .where(
                    WechatSessionBinding.worker_id == worker.id,
                    WechatSessionBinding.conversation_id
                    == payload.conversation_id,
                    WechatSessionBinding.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if (
                binding is None
                or binding.last_read_run_id != payload.flow_id
                or binding.last_read_completed_at is None
                or binding.last_read_result != "technical_failed"
            ):
                raise AppError(
                    "WORKER_INFLIGHT_FLOW_NOT_SETTLED",
                    "后端尚未确认同一读取流程的连续不明确技术终态",
                    409,
                )
        write_log(
            db,
            actor,
            event_type="worker_inflight_technical_failed",
            module="worker",
            target_type="worker",
            target_id=worker.id,
            metadata={
                "flow_id": payload.flow_id,
                "conversation_id": payload.conversation_id,
                "error_code": payload.error_code,
                "handoff_created": False,
            },
        )
    worker.inflight_flow_state = {}
    db.flush()
    return {"finished": True, "flow_id": payload.flow_id}


def settle_legacy_media_recovery(
    db: Session,
    worker: Worker,
    payload: WorkerLegacyMediaRecoverySettleRequest,
    actor: ActorContext,
) -> dict:
    """Confirm one bounded legacy-media exit without creating a message."""

    worker = _lock_worker(db, worker.id)
    current_flow = dict(worker.inflight_flow_state or {})
    current_flow_id = str(current_flow.get("flow_id") or "").strip()
    if current_flow_id and current_flow_id != payload.flow_id:
        raise AppError(
            "WORKER_INFLIGHT_FLOW_MISMATCH",
            "旧媒体恢复只能结算本机当前在途流程",
            409,
        )
    if (
        current_flow_id == payload.flow_id
        and str(current_flow.get("flow_kind") or "").strip() != "c2_read"
    ):
        raise AppError(
            "LEGACY_MEDIA_RECOVERY_FLOW_KIND_INVALID",
            "旧媒体恢复只允许结算 C2 读取流程",
            409,
        )

    requested_conversation_id = str(payload.conversation_id or "").strip()
    requested_resolution = payload.resolution
    idempotency_owner = requested_conversation_id or worker.id
    idempotency_key = (
        f"legacy:{idempotency_owner}:{payload.legacy_record_digest}"
    )
    existing = db.scalar(
        select(OperationLog)
        .where(
            OperationLog.event_type.in_(
                {
                    "worker_legacy_media_recovery_settled",
                    "worker_legacy_media_owner_unknown",
                }
            ),
            OperationLog.target_type == "legacy_media_recovery",
            OperationLog.target_id == idempotency_key,
            OperationLog.operator_id == worker.id,
        )
        .order_by(OperationLog.created_at.asc(), OperationLog.id.asc())
        .with_for_update()
    )
    if existing is not None:
        result = (
            dict(existing.after_data)
            if isinstance(existing.after_data, dict)
            else {}
        )
        stored_resolution = str(result.get("resolution") or "")
        allowed_stored_resolutions = {requested_resolution}
        if requested_resolution == "legacy_identity_unresolved_handoff":
            # Ownership is checked only on the first settlement. If the
            # binding later moves, a retry after a lost HTTP response must
            # still receive the already-committed finite terminal result.
            allowed_stored_resolutions.add("legacy_owner_unknown_incident")
        if (
            str(result.get("legacy_record_digest") or "")
            != payload.legacy_record_digest
            or stored_resolution not in allowed_stored_resolutions
        ):
            raise AppError(
                "LEGACY_MEDIA_RECOVERY_IDEMPOTENCY_CONFLICT",
                "旧媒体恢复终态与已确认记录冲突",
                409,
            )
        return {**result, "duplicated": True}

    actual_resolution = requested_resolution
    reason_code = {
        "legacy_cancelled_before_trigger": (
            "LEGACY_MEDIA_CANCELLED_BEFORE_TRIGGER"
        ),
        "legacy_identity_unresolved_handoff": (
            "LEGACY_MEDIA_IDENTITY_UNRESOLVED"
        ),
        "legacy_owner_unknown_incident": "LEGACY_MEDIA_OWNER_UNKNOWN",
    }[actual_resolution]
    binding: WechatSessionBinding | None = None
    if actual_resolution == "legacy_identity_unresolved_handoff":
        binding = db.scalar(
            select(WechatSessionBinding)
            .where(
                WechatSessionBinding.worker_id == worker.id,
                WechatSessionBinding.conversation_id
                == requested_conversation_id,
                WechatSessionBinding.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if binding is None:
            # A stale/moved conversation must not be handed to an unrelated
            # customer. It degrades to the finite Worker-level review exit.
            actual_resolution = "legacy_owner_unknown_incident"
            reason_code = "LEGACY_MEDIA_OWNER_UNKNOWN"

    handoff_result: dict = {}
    if actual_resolution == "legacy_identity_unresolved_handoff":
        from app.services.c3_service import (
            create_deterministic_handoff_for_ingest,
        )

        handoff_result = create_deterministic_handoff_for_ingest(
            db,
            conversation_id=requested_conversation_id,
            message_event_ids=[],
            reason_codes=[reason_code],
            trigger_key=f"legacy:{payload.legacy_record_digest}",
        )

    summary = dict(payload.record_summary or {})

    def non_negative_count(key: str) -> int:
        try:
            return max(0, int(summary.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    safe_summary = {
        "journal_count": non_negative_count("journal_count"),
        "ledger_count": non_negative_count("ledger_count"),
        "action_journal_count": non_negative_count(
            "action_journal_count"
        ),
        "outbox_count": non_negative_count("outbox_count"),
        "action_kinds": sorted(
            {
                str(value).strip().lower()
                for value in (summary.get("action_kinds") or [])
                if str(value).strip().lower() in {"voice", "image"}
            }
        ),
    }
    result = {
        "confirmed": True,
        "flow_id": payload.flow_id,
        "legacy_record_digest": payload.legacy_record_digest,
        "resolution": actual_resolution,
        "reason_code": reason_code,
        "conversation_id": (
            requested_conversation_id
            if actual_resolution == "legacy_identity_unresolved_handoff"
            else None
        ),
        "manual_review_required": (
            actual_resolution == "legacy_owner_unknown_incident"
        ),
        "handoff": handoff_result,
        "duplicated": False,
        "flow_released": (
            not current_flow_id or current_flow_id == payload.flow_id
        ),
    }
    write_log(
        db,
        actor,
        event_type=(
            "worker_legacy_media_owner_unknown"
            if result["manual_review_required"]
            else "worker_legacy_media_recovery_settled"
        ),
        module="worker",
        target_type="legacy_media_recovery",
        target_id=idempotency_key,
        after_data=result,
        metadata={
            "error_code": reason_code,
            "flow_id": payload.flow_id,
            "conversation_id": result["conversation_id"],
            "legacy_record_digest": payload.legacy_record_digest,
            "resolution": actual_resolution,
            "review_status": (
                "pending_manual_review"
                if result["manual_review_required"]
                else "handoff_created"
            ),
            "record_summary": safe_summary,
        },
    )
    if current_flow_id == payload.flow_id:
        worker.inflight_flow_state = {}
    db.flush()
    return result


def validate_inflight_continuation(
    worker: Worker,
    presented_flow_id: str | None,
    *,
    new_work: bool = False,
) -> None:
    current = dict(worker.inflight_flow_state or {})
    if new_work:
        if worker.run_status != "running" or current.get("flow_id"):
            raise AppError("WORKER_NEW_FLOW_NOT_ALLOWED", "Worker 已暂停或正在处理当前客户", 409)
        return
    if worker.run_status == "running" and not current.get("flow_id"):
        return
    if (
        not presented_flow_id
        or presented_flow_id != current.get("flow_id")
        or current.get("status") not in {"active", "draining"}
    ):
        raise AppError("WORKER_INFLIGHT_FLOW_MISMATCH", "在途流程凭证不匹配", 409)


def worker_can_claim(
    worker: Worker,
    *,
    allow_registered_draining_flow: bool = False,
) -> tuple[bool, str | None]:
    if computed_online_status(worker) != "online":
        return False, "WORKER_OFFLINE"
    if (
        worker.run_status != "running"
        and not allow_registered_draining_flow
    ):
        return False, "WORKER_NOT_ACCEPTING_TASKS"
    if worker.rpa_component_status != "ready":
        return False, "RPA_COMPONENT_UNAVAILABLE"
    return True, None


def running_task_for_worker(db: Session, worker_id: str) -> Task | None:
    return db.scalar(
        select(Task)
        .where(
            Task.worker_id == worker_id,
            Task.status == TaskStatus.running.value,
            Task.deleted_at.is_(None),
        )
        .order_by(Task.claimed_at.desc(), Task.updated_at.desc())
    )


def reset_worker_binding(db: Session, worker_id: str, actor: ActorContext) -> dict:
    worker = db.get(Worker, worker_id)
    if not worker or worker.deleted_at:
        raise AppError("WORKER_NOT_FOUND", "Worker 不存在", 404)

    has_running_task = bool(running_task_for_worker(db, worker.id) or (worker.current_task and worker.running_status in RUNNING_TASK_STATUSES))
    before = worker_summary(db, worker, include_token=False)
    token = generate_worker_token()
    worker.worker_token_hash = hash_worker_token(token)
    worker.worker_token_encrypted = encrypt_worker_token(token)
    worker.client_instance_id = None
    worker.client_binding_state = "reset_required"
    worker.bound_at = None
    worker.run_status = "paused"
    db.flush()
    after = worker_summary(db, worker, include_token=False)
    write_log(
        db,
        actor,
        event_type="worker_binding_reset",
        module="worker",
        target_type="worker",
        target_id=worker.id,
        before_data=before,
        after_data=after,
        metadata={"has_running_task": has_running_task},
    )
    return {
        **after,
        "worker_token": token,
        "has_running_task": has_running_task,
        "warning": "Worker 当前存在进行中任务，重置后旧 Token 失效，客户端需重新绑定。"
        if has_running_task
        else None,
        "reset_allowed": True,
    }
