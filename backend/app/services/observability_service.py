from __future__ import annotations

import uuid
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.contracts.c2 import c2_contract_v3
from app.core.config import get_settings
from app.models.observability import ProcessStageRun
from app.models.c3 import HandoffEvent, MessageBatch, ReplyAction
from app.models.task import Task
from app.models.wechat import WechatScanRun
from app.models.worker import Worker
from app.models.wechat import WechatSessionBinding
from app.schemas.observability import ProcessStageEventIn


def _contract_standard_stage_names() -> frozenset[str]:
    contract = c2_contract_v3().get("observability_contract")
    values = contract.get("standard_stage_names") if isinstance(contract, dict) else None
    if not isinstance(values, list) or not values:
        raise RuntimeError("Invalid observability standard stage contract")
    names = frozenset(str(value).strip() for value in values if str(value).strip())
    if len(names) != len(values):
        raise RuntimeError("Duplicate or empty observability standard stage name")
    return names


STANDARD_STAGE_NAMES = _contract_standard_stage_names()
TERMINAL_STAGE_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "abandoned"}
)
PROCESS_RUN_NAMESPACE = uuid.UUID("2446d48e-7d89-48f8-a708-37644bb57cb3")
logger = logging.getLogger(__name__)
def process_run_id_for_key(kind: str, stable_key: str) -> str:
    """Server-owned deterministic identity for a persisted business start."""

    cleaned_kind = str(kind or "").strip().lower()
    cleaned_key = str(stable_key or "").strip()
    if not cleaned_kind or not cleaned_key:
        raise ValueError("process run key is incomplete")
    return str(uuid.uuid5(PROCESS_RUN_NAMESPACE, f"{cleaned_kind}:{cleaned_key}"))


def stage_run_id_for_key(process_run_id: str, stage_name: str, stable_key: str) -> str:
    return str(
        uuid.uuid5(
            PROCESS_RUN_NAMESPACE,
            f"stage:{process_run_id}:{stage_name}:{stable_key}",
        )
    )


def next_stage_attempt(
    db: Session,
    *,
    process_run_id: str,
    stage_name: str,
) -> int:
    """Return the next observable attempt without ever becoming a business gate."""

    if not get_settings().observability_enabled:
        return 1
    try:
        latest = db.scalar(
            select(func.max(ProcessStageRun.attempt)).where(
                ProcessStageRun.process_run_id == process_run_id,
                ProcessStageRun.stage_name == stage_name,
            )
        )
        return max(1, int(latest or 0) + 1)
    except Exception:
        return 1


def record_server_stage_best_effort(
    db: Session,
    *,
    process_run_id: str,
    stage_name: str,
    component: str,
    conversation_id: str | None = None,
    worker_id: str | None = None,
    attempt: int = 1,
    duration_ms: int | None = None,
    status: str = "succeeded",
    error_code: str | None = None,
    trace_id: str | None = None,
    stable_key: str | None = None,
    queued_at: datetime | None = None,
    queue_duration_ms: int | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> str | None:
    """Write side-channel telemetry under a savepoint and never fail business work."""

    if not get_settings().observability_enabled:
        return None
    if stage_name not in STANDARD_STAGE_NAMES or status not in {
        "running",
        *TERMINAL_STAGE_STATUSES,
    }:
        return None
    try:
        stage_run_id = (
            stage_run_id_for_key(
                process_run_id,
                stage_name,
                stable_key,
            )
            if stable_key
            else str(uuid.uuid4())
        )
        finished_at = ended_at or datetime.now(timezone.utc)
        elapsed = max(0, int(duration_ms)) if duration_ms is not None else None
        began_at = started_at or (
            finished_at - timedelta(milliseconds=elapsed)
            if elapsed is not None
            else finished_at
        )
        event = ProcessStageEventIn(
            process_run_id=process_run_id,
            stage_run_id=stage_run_id,
            conversation_id=conversation_id,
            stage_name=stage_name,
            component=component,
            attempt=attempt,
            queued_at=queued_at,
            started_at=began_at,
            ended_at=None if status == "running" else finished_at,
            # Never derive a duration by subtracting wall clocks. Callers may
            # supply it only when the same process measured it monotonically.
            queue_duration_ms=(
                max(0, int(queue_duration_ms))
                if queue_duration_ms is not None
                else None
            ),
            execution_duration_ms=elapsed,
            status=status,
            error_code=error_code,
            trace_id=trace_id,
        )
        with db.begin_nested():
            row = db.get(ProcessStageRun, stage_run_id)
            if row is None:
                row = ProcessStageRun(
                    **event.model_dump(),
                    worker_id=worker_id,
                )
                db.add(row)
                db.flush()
            elif row.status == "running" and status in TERMINAL_STAGE_STATUSES:
                row.status = status
                row.ended_at = event.ended_at
                row.execution_duration_ms = event.execution_duration_ms
                row.error_code = error_code
                db.flush()
        return stage_run_id
    except Exception:
        logger.warning(
            "observability stage write ignored",
            extra={"stage_name": stage_name, "process_run_id": process_run_id},
            exc_info=True,
        )
        return None


def abandon_open_server_stages_after_restart(db: Session) -> int:
    """Close only pre-startup backend stages; never infer elapsed duration."""

    if not get_settings().observability_enabled:
        return 0
    try:
        rows = list(
            db.scalars(
                select(ProcessStageRun).where(
                    ProcessStageRun.component == "backend",
                    ProcessStageRun.status == "running",
                )
            )
        )
        if not rows:
            return 0
        ended_at = datetime.now(timezone.utc)
        with db.begin_nested():
            for row in rows:
                row.status = "abandoned"
                row.ended_at = ended_at
                row.execution_duration_ms = None
                row.error_code = "BACKEND_RESTARTED_DURING_STAGE"
            db.flush()
        return len(rows)
    except Exception:
        logger.warning(
            "open observability stages could not be abandoned",
            exc_info=True,
        )
        return 0


def process_run_id_for_read_target(
    *,
    conversation_id: str,
    authorization_revision: str,
    unread_generation: int,
    read_reason: str,
    recall_cycle_id: str | None = None,
) -> str:
    if recall_cycle_id:
        return process_run_id_for_key("c4", recall_cycle_id)
    return process_run_id_for_key(
        "c2",
        "|".join(
            [
                conversation_id,
                authorization_revision,
                str(max(0, unread_generation)),
                read_reason,
            ]
        ),
    )


def process_run_id_for_batch(db: Session, batch: MessageBatch) -> str:
    if batch.trace_id:
        linked = db.scalar(
            select(ProcessStageRun)
            .where(
                ProcessStageRun.conversation_id == batch.conversation_id,
                ProcessStageRun.trace_id == batch.trace_id,
            )
            .order_by(ProcessStageRun.created_at.desc())
        )
        if linked is not None:
            return linked.process_run_id
    if batch.trigger_type == "recall" and batch.recall_cycle_id:
        return process_run_id_for_key("c4", batch.recall_cycle_id)
    return process_run_id_for_key("c3", batch.id)


def process_run_id_for_handoff_event(db: Session, event: HandoffEvent) -> str:
    batch = db.get(MessageBatch, event.batch_id) if event.batch_id else None
    if batch is not None:
        return process_run_id_for_batch(db, batch)
    return process_run_id_for_key("handoff", event.id)


def _stage_to_dict(row: ProcessStageRun) -> dict[str, Any]:
    return {
        "process_run_id": row.process_run_id,
        "stage_run_id": row.stage_run_id,
        "parent_stage_run_id": row.parent_stage_run_id,
        "conversation_id": row.conversation_id,
        "stage_name": row.stage_name,
        "component": row.component,
        "attempt": row.attempt,
        "queued_at": row.queued_at,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "queue_duration_ms": row.queue_duration_ms,
        "execution_duration_ms": row.execution_duration_ms,
        "status": row.status,
        "error_code": row.error_code,
        "trace_id": row.trace_id,
    }


def _assert_immutable_identity(
    row: ProcessStageRun,
    event: ProcessStageEventIn,
    worker_id: str,
) -> None:
    expected = (
        row.process_run_id,
        row.parent_stage_run_id,
        row.conversation_id,
        row.stage_name,
        row.component,
        row.attempt,
        row.worker_id,
    )
    incoming = (
        event.process_run_id,
        event.parent_stage_run_id,
        event.conversation_id,
        event.stage_name,
        event.component,
        event.attempt,
        worker_id,
    )
    if expected != incoming:
        raise AppError(
            "OBSERVABILITY_STAGE_IDENTITY_CONFLICT",
            "同一 stage_run_id 的不可变身份不一致",
            409,
        )


def _assert_worker_process_run_issued(
    db: Session,
    *,
    worker: Worker,
    event: ProcessStageEventIn,
) -> None:
    """Reject made-up process ids while keeping rejection side-channel only."""

    issued = db.scalar(
        select(ProcessStageRun.stage_run_id).where(
            ProcessStageRun.process_run_id == event.process_run_id,
            ProcessStageRun.worker_id == worker.id,
        )
    )
    if issued:
        return
    if event.stage_name == "c2.scan" and event.trace_id:
        scan = db.scalar(
            select(WechatScanRun.id).where(
                WechatScanRun.worker_id == worker.id,
                WechatScanRun.scan_id == event.trace_id,
            )
        )
        if scan and event.process_run_id == process_run_id_for_key(
            "c2_scan", event.trace_id
        ):
            return
    if event.stage_name == "c1.add_friend_execute":
        lead_ids = list(
            db.scalars(
                select(Task.lead_id).where(
                    Task.worker_id == worker.id,
                    Task.task_type == "add_friend",
                    Task.lead_id.is_not(None),
                    Task.deleted_at.is_(None),
                )
            )
        )
        if any(
            event.process_run_id
            == process_run_id_for_key("c0_lead", str(lead_id))
            for lead_id in lead_ids
        ):
            return
    if event.stage_name in {
        "c3.pre_send_refresh",
        "c3.reply_send_confirm",
        "c4.reply_send_confirm",
    }:
        reply_action_ids = list(
            db.scalars(
                select(Task.reply_action_id).where(
                    Task.worker_id == worker.id,
                    Task.task_type == "chat_reply",
                    Task.reply_action_id.is_not(None),
                    Task.deleted_at.is_(None),
                )
            )
        )
        for reply_action_id in reply_action_ids:
            action = db.get(ReplyAction, reply_action_id)
            batch = db.get(MessageBatch, action.batch_id) if action else None
            if batch and event.process_run_id == process_run_id_for_batch(db, batch):
                return
    raise AppError(
        "OBSERVABILITY_PROCESS_RUN_NOT_ISSUED",
        "process_run_id 不是后端签发给当前 Worker 的业务处理编号",
        403,
    )


def ingest_worker_stage_events(
    db: Session,
    *,
    worker: Worker,
    events: list[ProcessStageEventIn],
) -> dict[str, Any]:
    if not get_settings().observability_enabled:
        return {
            "accepted_count": 0,
            "inserted_count": 0,
            "updated_count": 0,
            "ignored_terminal_regressions": 0,
            "observability_enabled": False,
            "authority_snapshots": [],
        }
    accepted = 0
    inserted = 0
    updated = 0
    ignored_terminal_regressions = 0
    for event in events:
        if event.stage_name not in STANDARD_STAGE_NAMES:
            raise AppError(
                "OBSERVABILITY_STAGE_NAME_INVALID",
                "观测阶段名称不在正式清单中",
                400,
                {"stage_name": event.stage_name},
            )
        if event.component not in {"worker", "sidecar"}:
            raise AppError(
                "OBSERVABILITY_COMPONENT_INVALID",
                "Worker 观测上报组件不合法",
                400,
            )
        _assert_worker_process_run_issued(
            db,
            worker=worker,
            event=event,
        )
        if event.conversation_id:
            owned = db.scalar(
                select(WechatSessionBinding.id).where(
                    WechatSessionBinding.worker_id == worker.id,
                    WechatSessionBinding.conversation_id
                    == event.conversation_id,
                    WechatSessionBinding.deleted_at.is_(None),
                )
            )
            if not owned:
                raise AppError(
                    "OBSERVABILITY_CONVERSATION_NOT_BOUND",
                    "观测事件不属于当前 Worker 会话",
                    403,
                )
        row = db.get(ProcessStageRun, event.stage_run_id)
        if row is None:
            row = ProcessStageRun(
                stage_run_id=event.stage_run_id,
                process_run_id=event.process_run_id,
                parent_stage_run_id=event.parent_stage_run_id,
                conversation_id=event.conversation_id,
                worker_id=worker.id,
                stage_name=event.stage_name,
                component=event.component,
                attempt=event.attempt,
                queued_at=event.queued_at,
                started_at=event.started_at,
                ended_at=event.ended_at,
                queue_duration_ms=event.queue_duration_ms,
                execution_duration_ms=event.execution_duration_ms,
                status=event.status,
                error_code=event.error_code,
                trace_id=event.trace_id,
            )
            db.add(row)
            inserted += 1
            accepted += 1
            continue
        _assert_immutable_identity(row, event, worker.id)
        if row.status in TERMINAL_STAGE_STATUSES:
            if row.status == "succeeded" and event.status == "failed":
                row.status = "failed"
                row.ended_at = event.ended_at
                row.execution_duration_ms = event.execution_duration_ms
                row.error_code = event.error_code
                if not row.trace_id:
                    row.trace_id = event.trace_id
                updated += 1
                accepted += 1
                continue
            ignored_terminal_regressions += 1
            accepted += 1
            continue
        if row.queued_at is None:
            row.queued_at = event.queued_at
        if row.started_at is None:
            row.started_at = event.started_at
        if row.queue_duration_ms is None:
            row.queue_duration_ms = event.queue_duration_ms
        if not row.trace_id:
            row.trace_id = event.trace_id
        if event.status in TERMINAL_STAGE_STATUSES:
            row.status = event.status
            row.ended_at = event.ended_at
            row.execution_duration_ms = event.execution_duration_ms
            row.error_code = event.error_code
        updated += 1
        accepted += 1
    db.flush()
    authority_snapshots = [
        get_process_run(db, process_run_id)
        for process_run_id in sorted(
            {str(event.process_run_id) for event in events}
        )
    ]
    return {
        "accepted_count": accepted,
        "inserted_count": inserted,
        "updated_count": updated,
        "ignored_terminal_regressions": ignored_terminal_regressions,
        "authority_snapshots": authority_snapshots,
    }


def get_process_run(db: Session, process_run_id: str) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(ProcessStageRun)
            .where(ProcessStageRun.process_run_id == process_run_id)
            .order_by(
                ProcessStageRun.started_at.asc().nulls_last(),
                ProcessStageRun.created_at.asc(),
            )
        )
    )
    if not rows:
        raise AppError("PROCESS_RUN_NOT_FOUND", "未找到该业务处理耗时记录", 404)
    summary: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "attempt_count": 0,
            "queue_duration_ms": None,
            "execution_duration_ms": None,
            "terminal_status": None,
        }
    )
    for row in rows:
        item = summary[row.stage_name]
        item["attempt_count"] += 1
        if row.queue_duration_ms is not None:
            item["queue_duration_ms"] = int(
                item["queue_duration_ms"] or 0
            ) + row.queue_duration_ms
        if row.execution_duration_ms is not None:
            item["execution_duration_ms"] = int(
                item["execution_duration_ms"] or 0
            ) + row.execution_duration_ms
        if row.status in TERMINAL_STAGE_STATUSES:
            item["terminal_status"] = row.status
    return {
        "process_run_id": process_run_id,
        "conversation_id": next(
            (row.conversation_id for row in rows if row.conversation_id),
            None,
        ),
        "stage_count": len(rows),
        "stages": [_stage_to_dict(row) for row in rows],
        "summary": dict(summary),
    }
