"""Server receipt clocks for Hook business timers, separate from native order.

Raw receipts, native CreateTime and send acknowledgements remain immutable.
Only derived conversation summaries are rebuilt during compatibility recovery.
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.models.hook_native import HookNativeReceipt, HookHistoryReceipt
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.c3 import Conversation, ReplyAction, SentAck, HumanReplyReminder


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def received_at(message):
    raw = message.raw_payload or {}
    return aware(raw.get('server_received_at') or message.ingested_at)


def stamp_receipt(message, receipt):
    at = min(received_at(message), aware(receipt.created_at))
    message.raw_payload = {**(message.raw_payload or {}), 'server_received_at': at.isoformat(),
                           'business_clock_source': 'server_receipt_v1'}
    return at


def restore_business_clock(db, binding, conversation, ctx=None):
    """One-time projection repair from original server facts, never replay actions."""
    ctx = dict(binding.native_context or {}) if ctx is None else ctx
    if not binding.native_self_wxid or (ctx.get('business_clock_version') == 1
            and not ctx.get('business_clock_missing_sources')):
        return
    messages = list(db.scalars(select(MessageEvent).where(MessageEvent.binding_id == binding.id)))
    by_id = {m.id:m for m in messages}
    # Old history receipts contain the original accepted event IDs. Their first
    # durable arrival can precede dispatch by hours; never use dispatch/replay now.
    for receipt in db.scalars(select(HookHistoryReceipt).where(HookHistoryReceipt.binding_id == binding.id)):
        for mid in (receipt.result_json or {}).get('message_event_ids', []):
            if mid in by_id:
                stamp_receipt(by_id[mid], receipt)
    for message in messages:
        receipt_id = (message.evidence or {}).get('native_receipt_id')
        receipt = db.get(HookNativeReceipt, receipt_id) if receipt_id else None
        if receipt and receipt.binding_id == binding.id:
            stamp_receipt(message, receipt)
    from app.services.hook_history_review import needs_review
    applied = set(ctx.get('actor_applied_message_ids', [])) - {m.id for m in messages if needs_review(m)}
    old = {key: aware(getattr(conversation, key)).isoformat() if getattr(conversation, key) else None
           for key in ('last_inbound_at','last_outbound_at','last_sales_reply_at','last_ai_reply_at','sales_first_reply_at','next_recall_at')}
    customer = [received_at(m) for m in messages if m.id in applied and m.sender_role == 'customer']
    human = [received_at(m) for m in messages if m.id in applied and (m.raw_payload or {}).get('sender_source') in {'human','human_confirmed','sales'}]
    outgoing = [received_at(m) for m in messages if m.id in applied and m.sender_role in {'self','sales','sales_candidate'}]
    ai = []
    for action, ack in db.execute(select(ReplyAction, SentAck).join(SentAck, SentAck.reply_action_id == ReplyAction.id)
            .where(ReplyAction.conversation_id == binding.conversation_id, SentAck.send_result == 'sent')):
        # Receipt creation is always server-owned, including old acknowledgements
        # whose sent_at was previously copied from a native message timestamp.
        ai.append(aware(ack.created_at))
    projections = {'last_inbound_at': customer, 'last_sales_reply_at': human,
        'sales_first_reply_at': human, 'last_ai_reply_at': ai, 'last_outbound_at': outgoing+ai}
    # Retain unsupported old summaries as evidence, but never use them to run
    # a timer. Later verified history may supply the missing source facts.
    prior = ctx.get('previous_business_clock_summary', old)
    missing = [key for key, facts in projections.items() if prior.get(key) and not facts]
    conversation.last_inbound_at = max(customer, default=None)
    conversation.last_sales_reply_at = max(human, default=None)
    conversation.sales_first_reply_at = min(human, default=None)
    conversation.last_ai_reply_at = max(ai, default=None)
    conversation.last_outbound_at = max(outgoing+ai, default=None)
    # Preserve the round identity, sent notifications, retry attempts and owner.
    reminder = db.scalar(select(HumanReplyReminder).where(HumanReplyReminder.conversation_id == binding.conversation_id,
        HumanReplyReminder.closed_at.is_(None)))
    if reminder and reminder.first_message_id not in by_id:
        missing.append('reminder_first_message')
    if reminder and reminder.first_message_id in by_id and reminder.status in {'pending','failed'}:
        start = received_at(by_id[reminder.first_message_id])
        reminder.started_at = start
        due = start + timedelta(hours=1)
        reminder.next_attempt_at = max(due, aware(reminder.next_attempt_at)) if reminder.attempt_count else due
    if conversation.next_recall_at is not None:
        from app.core.config import get_settings
        last = max(human+ai, default=None)
        conversation.next_recall_at = last + timedelta(hours=get_settings().c3_recall_after_hours) if last else None
    ctx['business_clock_version'] = 1
    ctx.setdefault('previous_business_clock_summary', old)
    ctx['business_clock_missing_sources'] = missing
    binding.native_context = dict(ctx)


def timers_ready(binding):
    ctx = binding.native_context or {}
    return not binding.native_self_wxid or (ctx.get('business_clock_version') == 1
        and not ctx.get('business_clock_missing_sources'))


def _restore_binding_clock(binding_id, evidence):
    from app.core.database import SessionLocal
    from app.services.followup_eligibility import lock_leads
    with SessionLocal() as db:
        hint = db.get(WechatSessionBinding, binding_id)
        if hint is None:
            return False
        evidence.update(conversation_id=hint.conversation_id)
        lock_leads(db, [hint.lead_id] if hint.lead_id else [])
        binding = db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.id == binding_id)
            .with_for_update().execution_options(populate_existing=True))
        conv = db.scalar(select(Conversation).where(
            Conversation.conversation_id == binding.conversation_id).with_for_update())
        if conv is None:
            return False
        restore_business_clock(db, binding, conv)
        db.commit()
        return True


def restore_pending_clocks(*, limit=20):
    """Bounded discovery; each binding owns an isolated recovery transaction."""
    import logging
    from app.core.database import SessionLocal
    from app.services.recovery_logging import recover_items
    with SessionLocal() as db:
        ids = list(db.scalars(select(WechatSessionBinding.id).where(
            WechatSessionBinding.native_self_wxid.is_not(None),
            WechatSessionBinding.native_context['business_clock_version'].as_integer().is_distinct_from(1))
            .order_by(WechatSessionBinding.id).limit(limit)))
    results, errors = recover_items(ids, _restore_binding_clock,
        logger=logging.getLogger(__name__), stage='hook_business_clock_repair',
        identity=lambda bid: {'binding_id': bid})
    return {'examined': len(ids), 'recovered': sum(results), 'failed': errors}
