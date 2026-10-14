"""Hook identity extends the existing Worker binding and heartbeat authority."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.sales import Sales
from app.models.worker import Worker
from app.core.request_context import ActorContext
from app.schemas.worker import HookHeartbeatContext
from app.services.audit_service import write_log


def apply_hook_heartbeat(db: Session, worker: Worker, context: HookHeartbeatContext | None,
                         *, actor: ActorContext | None = None) -> None:
    if context is None and not worker.hook_self_wxid and not worker.hook_runtime_context:
        return  # Existing OCR heartbeat retains its original behaviour.
    observation = context.model_dump(exclude={"account_epoch", "self_wxid"}) if context else {}
    observation["observed_self_wxid"] = context.self_wxid if context else None
    previous = dict(worker.hook_runtime_context or {})

    def reject(code: str) -> None:
        worker.hook_runtime_context = {**observation, "blocked": True, "reason_code": code,
            "rebind_required": bool(previous.get("rebind_required")) or code in {
                "HOOK_ACCOUNT_CHANGED", "HOOK_SALES_BINDING_CHANGED"}}
        worker.run_status = "faulted"
        flow = dict(worker.inflight_flow_state or {})
        if flow.get("status") == "active" and flow.get("flow_id"):
            worker.inflight_flow_state = {**flow, "status": "draining", "pause_requested_at": utcnow().isoformat()}
        if actor and (previous.get("reason_code") != code or previous.get("observed_self_wxid") != observation["observed_self_wxid"]):
            write_log(db, actor, event_type="worker_hook_account_blocked", module="worker",
                target_type="worker", target_id=worker.id, before_data={"account_epoch": worker.hook_account_epoch},
                after_data={"run_status": "faulted", "reason_code": code},
                metadata={"client_instance_id": worker.client_instance_id})

    if previous.get("rebind_required"):
        # Switching back to the old login does not bypass the confirmed reset
        # workflow. Only the existing admin reset clears this durable latch.
        reject(str(previous.get("reason_code") or "HOOK_ACCOUNT_CHANGED"))
        return
    if context is None:
        reject("HOOK_CONTEXT_REQUIRED")
        return
    if worker.platform != "windows" or worker.client_binding_state != "bound" or not worker.client_instance_id:
        reject("HOOK_CLIENT_BINDING_REQUIRED")
        return
    sales = db.scalar(select(Sales).where(Sales.worker_id == worker.id, Sales.deleted_at.is_(None)))
    if sales is None or not sales.enabled:
        reject("HOOK_SALES_BINDING_REQUIRED")
        return
    if worker.wechat_status != "logged_in":
        reject("HOOK_LOGIN_REQUIRED")
        return
    if worker.hook_self_wxid:
        if context.self_wxid != worker.hook_self_wxid:
            reject("HOOK_ACCOUNT_CHANGED")
            return
        if worker.hook_sales_id != sales.id:
            reject("HOOK_SALES_BINDING_CHANGED")
            return
        if context.account_epoch is not None and context.account_epoch != worker.hook_account_epoch:
            reject("HOOK_ACCOUNT_EPOCH_MISMATCH")
            return
    else:
        if context.account_epoch is not None:
            reject("HOOK_ACCOUNT_EPOCH_MISMATCH")
            return
        if db.scalar(select(Worker.id).where(Worker.id != worker.id, Worker.deleted_at.is_(None),
                Worker.hook_self_wxid == context.self_wxid).limit(1)):
            reject("HOOK_ACCOUNT_ALREADY_BOUND")
            return
        # Unique partial index is the authority if two different Workers race.
        # Keep the rejection/fault durable instead of rolling back the heartbeat.
        try:
            with db.begin_nested():
                worker.hook_self_wxid = context.self_wxid
                worker.hook_account_epoch = str(uuid.uuid4())
                worker.hook_sales_id = sales.id
                db.flush()
        except IntegrityError:
            reject("HOOK_ACCOUNT_ALREADY_BOUND")
            return
        if worker.run_status != "faulted":
            worker.run_status = "paused"
        flow = dict(worker.inflight_flow_state or {})
        if flow.get("status") == "active" and flow.get("flow_id"):
            worker.inflight_flow_state = {**flow, "status": "draining", "pause_requested_at": utcnow().isoformat()}
        if actor:
            write_log(db, actor, event_type="worker_hook_account_bound", module="worker",
                target_type="worker", target_id=worker.id,
                after_data={"self_wxid": worker.hook_self_wxid, "account_epoch": worker.hook_account_epoch,
                    "sales_id": worker.hook_sales_id}, metadata={"client_instance_id": worker.client_instance_id})
    worker.hook_runtime_context = {**observation, "blocked": False, "reason_code": None,
        "rebind_required": False}
    # Clearing a health problem never changes faulted -> running. The existing
    # explicit-start and settlement readiness gate remains responsible.
