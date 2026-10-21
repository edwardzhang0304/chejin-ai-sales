"""Verified customer system notices reuse C3 handoff, never the Worker fault gate."""
from app.contracts.shared_rules import shared_adapter
from sqlalchemy import select
from app.models.c3 import Conversation
from app.models.wechat import MessageEvent
from app.models.base import utcnow
from app.services import c3_service
from app.services.followup_eligibility import followup_block_reason

REASON = 'HOOK_FRIEND_VERIFICATION_REQUIRED'


def apply_unresolved_outgoing_notice(db, binding, checked, receipt, ctx):
    """Keep this customer's incomplete local send outside automatic service."""
    conversation = db.get(Conversation, binding.conversation_id)
    if (conversation is None or conversation.status in {'closed', 'rejected'}
            or not conversation.ai_enabled or followup_block_reason(db, binding.lead_id)):
        return
    applied = set(ctx.get('unresolved_outgoing_notice_keys', []))
    current = {notice_key(row, ctx) for row in checked['unresolved_outgoing_rows']}
    if current <= applied:
        return
    ctx['unresolved_outgoing_notice_keys'] = sorted(applied | current)
    binding.native_context = {**binding.native_context, **ctx, 'active_history_receipt_id': receipt.id}
    reason = shared_adapter('hook_history_rules').OUTGOING_ID_PENDING
    event, _ = c3_service._create_or_reuse_open_handoff(
        db, conversation=conversation, batch_id=None, handoff_reason_code=reason,
        reason_detail='本机存在服务器编号未确认的发出记录，暂停该客户自动回复，请人工核实；不自动补发。',
        trigger_message_event_ids=[], risk_flags=['unresolved_local_send'],
        evidence_refs=['native_history_receipt:' + receipt.id],
        ai_payload={'source': 'hook_customer_notice', 'history_receipt_id': receipt.id,
                    'automatic_resend_allowed': False})
    conversation.status = 'waiting_sales_reply'
    conversation.handoff_reason_code = event.handoff_reason_code
    conversation.handoff_at = event.created_at or utcnow()
    conversation.recall_origin_status = conversation.recall_cycle_id = conversation.next_recall_at = None
    c3_service.cancel_active_batches_for_conversation_change(db, binding.conversation_id, reason=reason)


def notice_key(row, ctx):
    identity = (['native', row['MsgSvrID']] if int(row['MsgSvrID']) else
                ['local', row['database'], row['localId'], row['CreateTime'], row['StrContent']])
    return shared_adapter('hook_contract_rules').digest([ctx.get('account_epoch'), identity])


def notice_sequence(row, checked):
    """Pagination localId is not business order; require both native sequences."""
    sequence, message_sequence, created = map(int, (row['Sequence'], row['MsgSequence'], row['CreateTime']))
    if sequence <= 0 or message_sequence <= 0:
        return None
    for other in checked['semantic_rows']:
        if (other['database'], other['database_generation']) != (row['database'], row['database_generation']):
            return None
        delta = int(other['Sequence']) - sequence
        message_delta = int(other['MsgSequence']) - message_sequence
        time_delta = int(other['CreateTime']) - created
        if delta == 0 or delta * message_delta <= 0 or delta * time_delta < 0:
            return None
    return sequence


def handoff_history_orders(db, binding, checked, ctx):
    """Order evidence only from this validated complete scan, never wall time."""
    history = shared_adapter('hook_history_rules')
    positions = {notice_key(row, ctx): notice_sequence(row, checked) for row in checked['rows']
                 if history.is_friend_verification_required_notice(row)}
    boundaries = {}
    for event in c3_service.open_handoff_events_for_conversation(db, binding.conversation_id):
        payload = event.ai_payload or {}
        if 'native_friend_verification_notice_keys' not in payload:
            continue
        keys = payload.get('native_friend_verification_notice_keys') or []
        boundaries[event.id] = (max(positions[key] for key in keys)
                                if keys and all(positions.get(key) is not None for key in keys) else None)
    return boundaries


def apply_friend_verification_notice(db, binding, checked, receipt, ctx):
    notices = checked['friend_verification_notices']
    if not notices:
        return
    conversation = db.get(Conversation, binding.conversation_id)
    if (conversation is None or conversation.status in {'closed', 'rejected'}
            or not conversation.ai_enabled or followup_block_reason(db, binding.lead_id)):
        return
    # Native IDs can be zero on system rows. Use a stable local fallback then;
    # database handles/process generations change on restart, message identity does not.
    applied = set(ctx.get('friend_verification_notice_keys', []))
    # A later verified acceptance or already applied human reply can resolve an
    # old restriction. Re-reading its immutable history must not reopen it.
    history = shared_adapter('hook_history_rules')
    human_ids = {(m.raw_payload or {}).get('native_msg_id') for m in db.scalars(
        select(MessageEvent).where(MessageEvent.binding_id == binding.id,
            MessageEvent.id.in_(ctx.get('actor_applied_message_ids', []))))
        if (m.raw_payload or {}).get('sender_source') == 'human'}
    resolved_rows = [r for r in checked['rows'] if history.is_friend_acceptance_notice(r)
                     or (r['Type'] == '1' and r['IsSender'] == '1' and r['MsgSvrID'] in human_ids)]
    new = []
    for row in notices:
        key = notice_key(row, ctx)
        if key not in applied:
            applied.add(key)
            sequence = notice_sequence(row, checked)
            if not any(sequence is not None and r['database'] == row['database'] and
                       r['database_generation'] == row['database_generation'] and
                       int(r['Sequence']) > sequence and int(r['MsgSequence']) > int(row['MsgSequence'])
                       and int(r['CreateTime']) >= int(row['CreateTime']) for r in resolved_rows):
                new.append(key)
    ctx['friend_verification_notice_keys'] = sorted(applied)
    binding.native_context = {**binding.native_context, **ctx, 'active_history_receipt_id': receipt.id}
    if not new:
        return
    event, _ = c3_service._create_or_reuse_open_handoff(
        db, conversation=conversation, batch_id=None,
        handoff_reason_code=REASON,
        reason_detail='微信提示需要好友验证，已暂停该客户自动回复；请销售确认好友关系并人工处理。原发送不自动重发。',
        trigger_message_event_ids=[], risk_flags=['friend_verification_required'],
        evidence_refs=['native_history_receipt:' + receipt.id],
        ai_payload={'source': 'hook_customer_notice',
                    'history_receipt_id': receipt.id, 'automatic_resend_allowed': False},
    )
    # The unique open handoff may already represent image/timeout handling.
    # Keep its reason, evidence and notification; add this newer restriction to
    # that same event so an older human reply cannot clear it accidentally.
    event.ai_payload = {**(event.ai_payload or {}),
        'native_friend_verification_notice_keys': list(dict.fromkeys(
            list((event.ai_payload or {}).get('native_friend_verification_notice_keys', [])) + new))}
    event.evidence_refs = list(dict.fromkeys(list(event.evidence_refs or []) + ['native_history_receipt:' + receipt.id]))
    event.risk_flags = list(dict.fromkeys(list(event.risk_flags or []) + ['friend_verification_required']))
    conversation.status = 'waiting_sales_reply'
    conversation.handoff_reason_code = event.handoff_reason_code
    conversation.handoff_at = event.created_at or utcnow()
    conversation.recall_origin_status = conversation.recall_cycle_id = conversation.next_recall_at = None
    # Existing cancellation skips physical sends already in progress. Their
    # original permits/journals and receipt settlement stay fully responsible.
    c3_service.cancel_active_batches_for_conversation_change(db, binding.conversation_id, reason=REASON)
