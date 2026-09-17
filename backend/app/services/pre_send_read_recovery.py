"""Settle a proven unsent read failure and retain work for the normal C2 reader.

No scheduler, model call or desktop action lives here. The two existing HTTP
receipts share this transaction; a later authoritative ingest owns generation.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from fastapi.encoders import jsonable_encoder

from app.contracts.shared_rules import shared_adapter
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation, MessageBatch, ReplyAction, SentAck
from app.models.task import Task
from app.models.wechat import WechatSessionBinding, MessageEvent
from app.models.worker import Worker


PENDING = "pre_send_read_pending"
RECEIPT = "pre_send_read_failure_receipt"


def _reject(message="发送前读取失败证明与原动作不一致"):
    raise AppError("PRE_SEND_READ_FAILURE_PROOF_INVALID", message, 409)


def settle(db, *, worker, payload, task_id, flow_id, client_instance_id,
           reply_action_id=None, lease_fencing_token=None):
    from app.services import c3_service as c3
    from app.services.followup_eligibility import (
        lock_leads, followup_block_reason, revoked_flow_proof, token_for_revision,
    )
    from app.services.reply_sequence_service import cancel_remaining
    from app.services.task_service import finish_task_and_release_worker, task_to_detail, _write_event
    from app.enums import TaskEventType

    try:
        proof = shared_adapter("pre_send_read_failure").validate_proof(
            payload.evidence["pre_send_read_failure"]
        )
    except (KeyError, TypeError, ValueError):
        _reject()
    if proof["task_id"] != task_id or (reply_action_id and proof["reply_action_id"] != reply_action_id):
        _reject()
    task = db.get(Task, task_id)
    if task is None or task.task_type != "chat_reply" or task.reply_action_id != proof["reply_action_id"]:
        _reject()
    lock_leads(db, [task.lead_id])
    # Lead -> binding -> Workers -> conversation -> action/task. A revoked
    # binding may have changed owner, but settlement still belongs to the old
    # Worker; lock both before the conversation, never afterwards.
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.conversation_id == proof["conversation_id"],
        WechatSessionBinding.deleted_at.is_(None)).with_for_update().execution_options(populate_existing=True))
    if binding is None:
        _reject()
    workers = list(db.scalars(select(Worker).where(Worker.id.in_({worker.id, binding.worker_id}))
                             .order_by(Worker.id).with_for_update().execution_options(populate_existing=True)))
    worker = next(row for row in workers if row.id == worker.id)
    conversation = db.scalar(select(Conversation).where(
        Conversation.conversation_id == proof["conversation_id"]).with_for_update().execution_options(populate_existing=True))
    action = db.scalar(select(ReplyAction).where(ReplyAction.id == task.reply_action_id).with_for_update().execution_options(populate_existing=True))
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))
    if (action is None or action.deleted_at or action.conversation_id != proof["conversation_id"]
            or task.worker_id != worker.id or worker.client_instance_id != client_instance_id
            or action.reply_text_hash != proof["reply_text_hash"]):
        _reject()
    raw = dict(action.ai_payload or {})
    saved = raw.get(RECEIPT)
    digest = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if saved:
        if (saved.get("proof_sha256") != digest or saved.get("worker_id") != worker.id
                or saved.get("client_instance_id") != client_instance_id
                or saved.get("receipt_kind") != ("sent_ack" if reply_action_id else "task_fail")):
            _reject("重复回执与原结算不一致")
        return {**saved["response"], "duplicated": True}
    batch = db.get(MessageBatch, action.batch_id)
    if binding is None or batch is None or conversation is None:
        _reject()
    capability = ((worker.local_lock_summary or {}).get("capabilities") or {}).get("pre_send_read_recovery_version")
    if type(capability) is not int or capability != 1:
        _reject("当前客户端没有声明发送前读取恢复能力")
    if worker.run_status != "faulted":
        _reject("技术故障必须先保存停止接单状态")
    inflight = dict(worker.inflight_flow_state or {})
    if not (flow_id and proof["flow_id"] == flow_id == inflight.get("flow_id")
            and inflight.get("status") in {"active", "draining"}
            and (inflight.get("conversation_id") == action.conversation_id
                 or (inflight.get("flow_kind") in {"task", "chat_reply"} and flow_id == task.id))):
        _reject("缺少原 Flow 身份，不允许申请新工作")
    origins = {batch.continuation_authorization_revision, inflight.get("authorization_revision"),
               token_for_revision(binding.id, int(binding.authorization_revision or 1))}
    if (proof["authorization_revision"] not in origins
            and not revoked_flow_proof(db, binding, flow_id, proof["authorization_revision"])):
        _reject("原授权来源无法核实")
    existing_ack = db.scalar(select(SentAck).where(SentAck.reply_action_id == action.id))
    if bool(action.send_token or existing_ack) != bool(reply_action_id):
        raise AppError("REPLY_ACTION_SENT_ACK_REQUIRED", "已签发许可必须通过原 sent_ack 结算", 409)
    if reply_action_id:
        if (payload.send_token != action.send_token or payload.send_result != "failed"
                or payload.action_phase != "not_attempted"
                or payload.reply_text_hash != action.reply_text_hash):
            _reject()
        if existing_ack or action.status in {"sent", "unknown_send_result"}:
            _reject("已有发送终态，不得以迟到失败回执改写")
        if action.status != "sending":
            _reject()
        if (action.claimed_by_worker_id != worker.id or action.claimed_task_id != task.id
                or proof["terminal_phase_proof"]["source"] != "action_journal"):
            _reject()
    else:
        if task.status not in {"pending", "running", "cancelled"} or action.status not in {"draft", "guarding", "queued", "cancelled"}:
            _reject()
        if int(task.lease_fencing_token or 0) > 0 and (
            task.lease_owner_worker_id != worker.id
            or task.lease_owner_client_instance_id != client_instance_id
            or lease_fencing_token != task.lease_fencing_token
        ):
            _reject("原任务租约归属已改变")
        if proof["terminal_phase_proof"]["source"] != "read_only_before_claim":
            _reject()

    blocked = (bool(followup_block_reason(db, task.lead_id)) or binding.worker_id != worker.id
               or proof["authorization_revision"] != token_for_revision(binding.id, int(binding.authorization_revision or 1)))
    before = task.status
    error = str(payload.error_code or proof["first_failure"]["error_code"])
    task.status = "cancelled" if blocked or task.status == "cancelled" else "failed"
    task.error_code = error
    task.failure_step = proof["first_failure"]["stage"]
    task.failure_remark = getattr(payload, "failure_remark", None) or getattr(payload, "remark", None)
    if task.status == "cancelled":
        task.cancelled_at = task.cancelled_at or utcnow()
    else:
        task.failed_at = utcnow()
    action.status = "cancelled" if blocked else "failed"
    action.current = False
    action.error_code = error
    action.suggested_action = "wait_for_new_authorization"
    cancel_remaining(db, action, error)
    batch.status, batch.active, batch.retryable = "cancelled", False, False
    batch.error_code, batch.suggested_action = error, "wait_for_new_authorization"
    finish_task_and_release_worker(task)
    _write_event(db, task, TaskEventType.cancelled if task.status == "cancelled" else TaskEventType.failed, from_status=before, to_status=task.status,
                 worker_id=worker.id, remark=task.failure_remark)
    pending = None
    if (not blocked and conversation.ai_enabled and binding.allow_listening
            and binding.listen_status in {"listening", "degraded"}
            and not c3.open_handoff_events_for_conversation(db, action.conversation_id, for_update=True)):
        # References originate in the server-owned batch, never in the receipt.
        existing_ids = set(db.scalars(select(MessageEvent.id).where(
            MessageEvent.id.in_(batch.message_event_ids or []),
            MessageEvent.conversation_id == action.conversation_id,
            MessageEvent.sender_role == "customer")))
        ids = [identity for identity in dict.fromkeys(batch.message_event_ids or []) if identity in existing_ids]
        if ids and batch.trigger_type != "recall":
            pending = {"status": "pending", "reply_action_id": action.id, "batch_id": batch.id,
                       "worker_id": worker.id, "binding_id": binding.id,
                       "message_event_ids": ids, "created_at": utcnow().isoformat(),
                       "last_outbound_at": conversation.last_outbound_at.isoformat() if conversation.last_outbound_at else None}
            binding.last_scan_snapshot = {**(binding.last_scan_snapshot or {}), PENDING: pending}
            binding.next_read_due_at = utcnow()
            binding.no_change_read_count = 0
        if conversation.status == "ai_active":
            conversation.status = "waiting_user_reply"
        # Recall preserves its existing cycle and count; no fictitious inbound.
        conversation.next_recall_at = None if pending else conversation.next_recall_at
    db.flush()
    response = {"duplicated": False, "task": task_to_detail(task),
                "reply_action": c3._reply_action_to_dict(action), "pending_read": bool(pending)}
    if reply_action_id:
        ack = SentAck(reply_action_id=action.id, task_id=task.id, worker_id=worker.id,
                      client_instance_id=client_instance_id, send_token=action.send_token,
                      send_result="failed", action_phase="not_attempted", reply_text_hash=action.reply_text_hash,
                      sidecar_run_id=payload.sidecar_run_id, evidence=payload.evidence,
                      error_code=error, remark=payload.remark, sent_at=payload.sent_at)
        db.add(ack); db.flush()
        response["ack"] = c3._sent_ack_to_dict(ack)
    response = jsonable_encoder(response)
    raw[RECEIPT] = {"proof_sha256": digest, "worker_id": worker.id,
                    "client_instance_id": client_instance_id,
                    "receipt_kind": "sent_ack" if reply_action_id else "task_fail",
                    "proof": proof, "response": response}
    action.ai_payload = raw
    db.flush()
    return response


def collect_after_read(db, *, worker, binding, conversation, read_run_id,
                       customer_tail_ids, visible_message_ids, trace_id=None):
    """Consume pending demand only inside a fresh, complete C2 ingest.

    The caller already holds the ordinary C2 scope locks and has rejected
    identity/media/capability gates. This creates batch metadata only; the
    existing HTTP BackgroundTasks path remains the sole generation caller.
    """
    from app.services import c3_service as c3
    from app.services.followup_eligibility import followup_block_reason
    from app.services.knowledge_management_service import current_release_for_batch

    scan = dict(binding.last_scan_snapshot or {})
    pending = scan.get(PENDING)
    if not isinstance(pending, dict) or pending.get("status") != "pending":
        return None
    flow = worker.inflight_flow_state or {}
    if (worker.run_status != "running" or flow.get("flow_kind") != "c2_read"
            or flow.get("status") != "active" or flow.get("flow_id") != read_run_id
            or flow.get("conversation_id") != binding.conversation_id):
        return None

    def finish(reason, batch_id=None):
        scan[PENDING] = {**pending, "status": "consumed" if batch_id else "closed",
                         "resolution": reason, "read_run_id": read_run_id,
                         "resolved_at": utcnow().isoformat(), "resolved_batch_id": batch_id}
        binding.last_scan_snapshot = scan

    if (pending.get("worker_id") != worker.id or pending.get("binding_id") != binding.id
            or binding.worker_id != worker.id or not binding.allow_listening
            or binding.listen_status not in {"listening", "degraded"}
            or not conversation.ai_enabled or conversation.status in {
                "waiting_sales_reply", "sales_replied_waiting_user", "closed", "rejected"}
            or followup_block_reason(db, binding.lead_id, lock=False)
            or c3.open_handoff_events_for_conversation(db, binding.conversation_id, for_update=True)):
        finish("business_blocked")
        return None
    origin = db.get(ReplyAction, pending.get("reply_action_id"))
    old_batch = db.get(MessageBatch, pending.get("batch_id"))
    if (not origin or not old_batch or origin.batch_id != old_batch.id
            or origin.conversation_id != binding.conversation_id
            or old_batch.conversation_id != binding.conversation_id
            or old_batch.active or origin.status not in {"failed", "cancelled"}
            or not (origin.ai_payload or {}).get(RECEIPT)):
        _reject("待回复事项的原结算不完整")
    last_outbound = conversation.last_outbound_at.isoformat() if conversation.last_outbound_at else None
    ids = list(dict.fromkeys(pending.get("message_event_ids") or []))
    if not ids or not set(ids).issubset(set(old_batch.message_event_ids or [])):
        _reject("待回复事项不是原批次的客户消息")
    # An all-OLD frame has no newly ingested rows. Resolve the original IDs
    # through its verified visual slots rather than fabricating NEW messages.
    visible_events = list(db.scalars(select(MessageEvent).where(
        MessageEvent.conversation_id == binding.conversation_id,
        MessageEvent.id.in_(list(visible_message_ids)))))
    visible_events.sort(key=lambda event: visible_message_ids[event.id])
    # A confirmed prefix of THIS interrupted generation is unfinished AI work,
    # not a sales takeover or an answer to a later question. The association is
    # server-validated at ingest; text similarity alone never grants this.
    prefix = list(db.execute(select(ReplyAction, SentAck).join(
        SentAck, SentAck.reply_action_id == ReplyAction.id).where(
        ReplyAction.batch_id == old_batch.id,
        ReplyAction.generation_no == origin.generation_no,
        ReplyAction.segment_index < origin.segment_index,
        ReplyAction.deleted_at.is_(None), ReplyAction.status == "sent",
        SentAck.send_result == "sent", SentAck.action_phase == "confirmed",
        SentAck.reply_text_hash == ReplyAction.reply_text_hash,
        SentAck.task_id == ReplyAction.claimed_task_id,
    ).order_by(ReplyAction.segment_index)))
    prefix_actions = {a.id: a for a, ack in prefix}
    if [a.segment_index for a, ack in prefix] != list(range(1, origin.segment_index)):
        # Unknown or unconfirmed earlier sends must not be retried by recovery.
        return None

    def is_prefix(event):
        raw = event.raw_payload or {}
        action = prefix_actions.get(raw.get("ai_reply_action_id"))
        return bool(event.sender_role == "self" and raw.get("sender_source") == "ai"
                    and action and raw.get("ai_reply_text_hash") == action.reply_text_hash)

    prefix_events = [event for event in visible_events if is_prefix(event)]
    if (len(prefix_events) != len(prefix_actions)
            or set(prefix_actions) != {(e.raw_payload or {}).get("ai_reply_action_id") for e in prefix_events}):
        # Reconstructing unfinished work requires actually seeing the confirmed
        # prefix in the fresh frame, not just trusting an old planned reply.
        return None
    if last_outbound != pending.get("last_outbound_at"):
        # The first fresh read can ingest the already-sent prefix for the first
        # time and advance last_outbound_at. Permit only that precise fact.
        reconciled_prefix = any((e.occurred_at or e.ingested_at).isoformat() == last_outbound
                                for e in prefix_events)
        later_send = db.scalar(select(SentAck.id).join(
            ReplyAction, ReplyAction.id == SentAck.reply_action_id).where(
            ReplyAction.conversation_id == binding.conversation_id,
            ReplyAction.id.not_in(prefix_actions), SentAck.send_result.in_(["sent", "unknown"]),
            SentAck.created_at > origin.updated_at).limit(1))
        if not reconciled_prefix or later_send:
            finish("reply_already_observed")
            return None
    last_self = max((visible_message_ids[event.id] for event in visible_events
                     if event.sender_role in {"self", "sales"} and not is_prefix(event)), default=0)
    customer_tail_ids = [event.id for event in visible_events if event.sender_role == "customer"
                         and visible_message_ids[event.id] > last_self]
    # The authoritative suffix must still contain the original last question.
    # Merely being able to read another customer or an unrelated old page is
    # insufficient. No OLD event is relabelled NEW.
    if ids[-1] not in customer_tail_ids or ids[-1] not in visible_message_ids:
        return None
    collected_ids = list(dict.fromkeys([*ids, *customer_tail_ids]))
    events = list(db.scalars(select(MessageEvent).where(MessageEvent.id.in_(collected_ids))))
    if (len(events) != len(collected_ids) or any(
            event.conversation_id != binding.conversation_id or event.sender_role != "customer"
            for event in events)):
        _reject("待回复事项与当前客户事实不一致")
    trigger = "pre-send-recovery:" + origin.id
    current = c3._active_batch(db, binding.conversation_id)
    if current is None:
        current = db.scalar(select(MessageBatch).where(
            MessageBatch.conversation_id == binding.conversation_id,
            MessageBatch.trigger_type == "customer_message", MessageBatch.trigger_key == trigger,
            MessageBatch.deleted_at.is_(None)))
    if current is None:
        current = MessageBatch(
            conversation_id=binding.conversation_id, status="collecting", active=True,
            trigger_type="customer_message", trigger_key=trigger,
            trigger_message_event_id=collected_ids[-1], message_event_ids=collected_ids,
            message_count=len(collected_ids), generation_no=1,
            knowledge_release_id=current_release_for_batch(db).id, trace_id=trace_id,
        )
        db.add(current)
        db.flush()
    elif current.status == "collecting":
        # A newer customer batch may already own the continuation. Keep it as
        # the only active batch, and merge before generation has begun.
        current.message_event_ids = list(dict.fromkeys([*collected_ids, *(current.message_event_ids or [])]))
        current.message_count = len(current.message_event_ids)
    if prefix_events and current.status == "collecting":
        current.ai_request_snapshot = {**(current.ai_request_snapshot or {}), "partial_reply_recovery": {
            "origin_batch_id": old_batch.id, "origin_reply_action_id": origin.id,
            "confirmed_prefix": [{"reply_action_id": (e.raw_payload or {})["ai_reply_action_id"],
                "message_event_id": e.id, "text": e.content} for e in prefix_events],
        }}
    # Generating/queued newer work already uses the conversation context. Do
    # not mutate its frozen request or create a competing duplicate answer.
    if current.active:
        # This read proved the original customer demand is still unanswered.
        # Enter the existing C2 -> Brain continuation state just as NEW input
        # does; changing read-authorization to bypass this state is unsafe.
        conversation.status = "ai_active"
    finish("fresh_read", current.id)
    db.flush()
    return {"batch_id": current.id, "batch_status": current.status,
            "next_step": "generate" if current.active else "use_existing",
            "batch": c3._batch_to_dict(current)}
