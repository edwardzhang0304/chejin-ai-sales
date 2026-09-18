"""Append an original-image correction; retain demand through the existing reader."""
from sqlalchemy import select

from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation, MessageBatch, ReplyAction, SentAck
from app.models.message_text_correction import MessageTextCorrection
from app.models.task import Task
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.worker import Worker
from app.services.message_effective_text import effective_context_digest, effective_versions, text_sha256
from app.services.message_text_correction_proof import validate_original_proof

CAUSE = "historical_text_correction"
INVALIDATED = "HISTORICAL_TEXT_CORRECTED"


def _busy():
    raise AppError("HISTORICAL_TEXT_CORRECTION_BUSY", "原流程仍有未结算动作，稍后重新核验", 409,
                   {"retryable": True})


def _rejected(reason, *, resolution=None):
    raise AppError("HISTORICAL_TEXT_CORRECTION_REJECTED", "历史文字纠错未获确认", 422,
                   {"reason": reason, **({"resolution": resolution} if resolution else {})})


def _result(row, *, duplicated):
    return {"outcome": "accepted", "duplicated": duplicated, "correction_id": row.id,
            "message_event_id": row.message_event_id, "effective_version": row.effective_version,
            "effective_text": row.effective_text, "effective_text_sha256": row.effective_text_sha256}


def append_correction(db, *, worker, payload, client_instance_id, current_flow_id=None):
    from app.services import c3_service as c3
    from app.services.followup_eligibility import followup_block_reason, lock_leads, token_for_revision
    from app.services.pre_send_read_recovery import PENDING, pending_sources, pack_pending_sources

    original = db.get(MessageEvent, payload.message_event_id)
    if not original or original.conversation_id != payload.conversation_id:
        _rejected("original_message_not_found")
    lock_leads(db, [original.lead_id])
    binding = db.scalar(select(WechatSessionBinding).where(
        WechatSessionBinding.conversation_id == payload.conversation_id,
        WechatSessionBinding.deleted_at.is_(None)).with_for_update().execution_options(populate_existing=True))
    if not binding:
        _rejected("binding_missing")
    workers = list(db.scalars(select(Worker).where(Worker.id.in_({worker.id, binding.worker_id}))
        .order_by(Worker.id).with_for_update().execution_options(populate_existing=True)))
    worker = next(item for item in workers if item.id == worker.id)
    conversation = db.scalar(select(Conversation).where(
        Conversation.conversation_id == payload.conversation_id).with_for_update().execution_options(populate_existing=True))
    if (not client_instance_id or worker.client_instance_id != client_instance_id
            or worker.client_binding_state != "bound" or not worker.enabled or worker.deleted_at
            or binding.id != payload.binding_id or binding.worker_id != worker.id
            or original.worker_id != worker.id or original.binding_id != binding.id):
        _rejected("current_authorization_changed")
    capabilities = (worker.local_lock_summary or {}).get("capabilities") or {}
    if type(capabilities.get("historical_text_correction_version")) is not int or capabilities["historical_text_correction_version"] != 1:
        _rejected("capability_unsupported")
    if not conversation or conversation.deleted_at:
        _rejected("business_blocked")
    business_ended = (not conversation.ai_enabled
        or conversation.status in {"waiting_sales_reply", "sales_replied_waiting_user", "closed", "rejected"}
        or not binding.allow_listening or binding.bind_status != "bound"
        or bool(followup_block_reason(db, binding.lead_id, lock=False))
        or bool(c3.open_handoff_events_for_conversation(db, binding.conversation_id, for_update=True)))
    # A revoked revision cannot authorize a correction. It can only identify
    # the old proposal in a fact-only closure receipt after all original work
    # is settled. No correction or resumption occurs on that path.
    if not business_ended:
        if payload.authorization_revision != token_for_revision(binding.id, int(binding.authorization_revision or 1)):
            _rejected("current_authorization_changed")
        if binding.listen_status not in {"listening", "degraded"}:
            _rejected("business_blocked")
    flow = worker.inflight_flow_state or {}
    if flow and not (flow.get("status") == "active" and flow.get("flow_kind") == "c2_read"
                     and flow.get("conversation_id") == binding.conversation_id
                     and flow.get("flow_id") == current_flow_id):
        _busy()
    # The outer locks also serialize generation, task claim and sending. Include
    # inactive batches: the first sent segment may have made its batch inactive.
    batches = list(db.scalars(select(MessageBatch).where(
        MessageBatch.conversation_id == binding.conversation_id,
        MessageBatch.deleted_at.is_(None)).order_by(MessageBatch.id).with_for_update().execution_options(populate_existing=True)))
    actions = list(db.scalars(select(ReplyAction).where(
        ReplyAction.conversation_id == binding.conversation_id,
        ReplyAction.deleted_at.is_(None)).order_by(ReplyAction.id).with_for_update().execution_options(populate_existing=True)))
    tasks = list(db.scalars(select(Task).where(Task.reply_action_id.in_([a.id for a in actions]),
        Task.deleted_at.is_(None)).order_by(Task.id).with_for_update().execution_options(populate_existing=True)))
    original = db.scalar(select(MessageEvent).where(MessageEvent.id == original.id)
                         .with_for_update().execution_options(populate_existing=True))
    prior = db.scalar(select(MessageTextCorrection).where(
        MessageTextCorrection.message_event_id == original.id,
        MessageTextCorrection.proof_sha256 == payload.proof_sha256))
    if prior:
        # Still verify the frozen envelope: an alleged digest alone is not a retry.
        from app.services.message_text_correction_proof import proof_digest
        if proof_digest(payload) != prior.proof_sha256 or payload.proof.image_sha256 != prior.image_sha256:
            _rejected("idempotency_conflict")
        import base64
        import binascii
        import hashlib
        try:
            image_bytes = base64.b64decode(payload.image_base64, validate=True)
        except (ValueError, binascii.Error):
            _rejected("idempotency_conflict")
        if hashlib.sha256(image_bytes).hexdigest() != prior.image_sha256:
            _rejected("idempotency_conflict")
        return _result(prior, duplicated=True)
    pending_actions = [a for a in actions if a.status in {"draft", "guarding", "queued"}]
    task_by_action = {task.reply_action_id: task for task in tasks}
    # Closing an obsolete proposal uses fault_recovery_readiness below to
    # distinguish settled unknown receipts from outstanding sends. A live
    # history correction retains the stricter unknown-send barrier.
    blocking_send_states = {"sending"} if business_ended else {"sending", "unknown_send_result"}
    if (any(a.status in blocking_send_states for a in actions)
            or any(a.send_token or a.sending_claimed_at for a in pending_actions)
            or any(t.status != "pending" or t.claimed_at or t.lease_owner_worker_id
                   for a in pending_actions if (t := task_by_action.get(a.id)))
            or db.scalar(select(SentAck.id).where(SentAck.reply_action_id.in_([a.id for a in pending_actions])).limit(1))):
        _busy()
    previous = effective_versions(db, [original])[original.id]
    if previous["version"] != payload.expected_effective_version:
        _rejected("effective_version_changed")
    from app.services.text_correspondence_context import build_context
    entities = tuple(e['value'] for e in build_context(db, binding)['known_entities'])
    image_bytes = validate_original_proof(original, payload, previous_text=previous["text"] or "", known_entities=entities)
    if business_ended:
        from app.services.worker_service import fault_recovery_readiness
        if not fault_recovery_readiness(db, worker)["ready"]:
            _busy()
        from app.contracts.shared_rules import shared_adapter
        resolution = shared_adapter("historical_text_correction").closed_business_resolution(
            payload.model_dump(mode="json"), worker_id=worker.id, client_instance_id=client_instance_id)
        _rejected("business_ended", resolution=resolution)
    row = MessageTextCorrection(message_event_id=original.id, conversation_id=original.conversation_id,
        worker_id=worker.id, previous_version=previous["version"], effective_version=previous["version"] + 1,
        original_text_sha256=payload.original_text_sha256, effective_text=payload.corrected_text,
        effective_text_sha256=text_sha256(payload.corrected_text), proof_sha256=payload.proof_sha256,
        image_sha256=payload.proof.image_sha256, image_bytes=image_bytes,
        proof={"request": payload.model_dump(mode="json", exclude={"image_base64"})})
    db.add(row)
    db.flush()
    digest = effective_context_digest(db, original.conversation_id)
    sources = []
    for batch in batches:
        remaining = sorted((a for a in pending_actions if a.batch_id == batch.id), key=lambda a: a.segment_index)
        if not remaining and not (batch.active and batch.status in {"collecting", "generating"}):
            continue
        original_ids = list(dict.fromkeys(batch.message_event_ids or []))
        customer_ids = set(db.scalars(select(MessageEvent.id).where(MessageEvent.id.in_(original_ids),
            MessageEvent.conversation_id == original.conversation_id, MessageEvent.sender_role == "customer")))
        ids = [identity for identity in original_ids if identity in customer_ids]
        if ids and batch.trigger_type != "recall":
            origin = remaining[0] if remaining else None
            sources.append({"status": "pending", "cause": CAUSE, "correction_id": row.id,
                "reply_action_id": origin.id if origin else None, "batch_id": batch.id,
                "generation_no": batch.generation_no, "segment_index": origin.segment_index if origin else 1,
                "worker_id": worker.id, "binding_id": binding.id, "message_event_ids": ids,
                "created_at": utcnow().isoformat(), "effective_context_digest": digest,
                "last_outbound_at": conversation.last_outbound_at.isoformat() if conversation.last_outbound_at else None})
        for action in remaining:
            action.status, action.current, action.error_code = "cancelled", False, INVALIDATED
            c3._cancel_task_for_action(db, action, reason=INVALIDATED)
        batch.status, batch.active, batch.retryable = "cancelled", False, False
        batch.error_code, batch.suggested_action = INVALIDATED, "wait_for_new_authorization"
    row.proof = {**row.proof, "pending_sources": sources}
    scan = dict(binding.last_scan_snapshot or {})
    existing = [dict(source) for source in pending_sources(scan.get(PENDING)) if source.get("status") == "pending"]
    linked = False
    from app.contracts.shared_rules import shared_adapter
    proposal_fields = shared_adapter("historical_correction_pending").PROPOSAL_FIELDS
    for source in existing:
        required = source.get("required_correction")
        if required and all(required.get(key) == getattr(payload, key) for key in proposal_fields):
            source["required_correction"] = {**required, "status": "accepted", "correction_id": row.id,
                "effective_context_digest": digest}
            linked = True
    if sources or linked:
        # Keep the original receipt-bound source intact when both causes coexist.
        scan[PENDING] = pack_pending_sources([*existing, *sources])
        binding.last_scan_snapshot = scan
        binding.next_read_due_at, binding.no_change_read_count = utcnow(), 0
        if conversation.status == "ai_active":
            conversation.status = "waiting_user_reply"
        conversation.next_recall_at = None
    db.flush()
    return _result(row, duplicated=False)


def validate_pending_source(db, pending, conversation_id, batch, action):
    row = db.get(MessageTextCorrection, pending.get("correction_id"))
    if (not row or row.conversation_id != conversation_id or not batch
            or batch.conversation_id != conversation_id or batch.active
            or batch.status != "cancelled" or batch.error_code != INVALIDATED
            or action and (action.batch_id != batch.id or action.status != "cancelled"
                           or action.error_code != INVALIDATED or action.send_token)):
        _rejected("pending_correction_source_invalid")
    # Compare the server-created source, never accept an invented recovery cause.
    saved = (row.proof or {}).get("pending_sources") or []
    if not any(all(pending.get(key) == value for key, value in source.items()) for source in saved):
        _rejected("pending_correction_source_mismatch")
