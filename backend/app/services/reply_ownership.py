"""Authoritative reply owner, unanswered rounds and recall ownership fence.

Called under the existing conversation/business lock with verified sender/order
facts. Closing a HandoffEvent never implicitly transfers responsibility to AI.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation, HumanReplyReminder, HandoffEvent, MessageBatch
from app.models.wechat import MessageEvent


def aware(value):
    if isinstance(value,str):
        value=datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def business_time(message):
    """Native order stays in Sequence/CreateTime; timers stay on the server clock.

    The first durable server receipt is used, never re-read/replay time. It may be
    later than physical occurrence after an outage, so reminders never start
    early because a customer's machine clock is ahead or behind.
    """
    if (message.raw_payload or {}).get('transport') == 'hook':
        from app.services.hook_business_time import received_at
        return received_at(message)
    return aware(message.occurred_at)


def state(conversation):
    return dict(conversation.reply_ownership or {'owner':'legacy','revision':0,'recall_streak':0})


def human_owned(conversation):
    return state(conversation)['owner']=='human'


def revision(conversation):
    return int(state(conversation).get('revision',0))


def allows_batch(conversation,batch=None):
    value=state(conversation)
    if value['owner']=='legacy':
        return False
    if batch and batch.ownership_revision is not None and batch.ownership_revision!=revision(conversation):
        return False
    return value['owner']=='ai' or bool(batch and batch.trigger_type=='recall' and not value.get('round_id'))


def require_batch(conversation,batch=None):
    if not allows_batch(conversation,batch):
        raise AppError('HUMAN_REPLY_OWNERSHIP_REQUIRED','客户由销售负责回复，自动回复尚未获准',409)


def close_round(db,conversation,*,now=None):
    value=state(conversation)
    if value.get('round_id'):
        row=db.get(HumanReplyReminder,value['round_id'])
        if row and row.closed_at is None:
            row.closed_at=now or utcnow()
            if row.status in {'pending','failed'}:
                row.status='cancelled'
    value['round_id']=None
    conversation.reply_ownership=value


def _start_round(db,conversation,message,*,not_before=None,effective_at=None):
    value=state(conversation)
    if value.get('round_id'):
        return
    start=aware(effective_at) if effective_at else business_time(message)
    if start is None:
        return
    if not_before:
        start=max(start,aware(not_before))
    row=HumanReplyReminder(conversation_id=conversation.conversation_id,first_message_id=message.id,
        started_at=start,next_attempt_at=start+timedelta(hours=1))
    db.add(row)
    db.flush()
    value['round_id']=row.id
    conversation.reply_ownership=value


def _newer(db,message,value,orders):
    previous=value.get('last_message_id')
    if not previous:
        return True
    if message.id==previous:
        return False
    if previous in orders and message.id in orders:
        return orders[message.id]>orders[previous]
    old=db.get(MessageEvent,previous)
    return bool(old and old.occurred_at and message.occurred_at and aware(message.occurred_at)>aware(old.occurred_at))


def apply_message(db,conversation,message,*,orders=None,effective_at=None):
    """Verified original chronological fact; replay/transcript updates are inert."""
    db.scalar(select(Conversation.conversation_id).where(Conversation.conversation_id==conversation.conversation_id).with_for_update())
    value=state(conversation)
    orders=orders or {}
    if value['owner']=='legacy':
        # User explicitly excluded retroactive migration/reminders. New post-
        # upgrade facts begin new cycles; old evidence remains untouched.
        if (message.raw_payload or {}).get('transport') == 'hook':
            from app.models.wechat import WechatSessionBinding
            from app.services.hook_history_review import needs_review
            binding = db.get(WechatSessionBinding, message.binding_id)
            if not binding or not (binding.native_context or {}).get('native_admission_anchor') or needs_review(message):
                return False
        else:
            since=aware(value.get('since'))
            if not since or not message.occurred_at or aware(message.occurred_at)<since:
                return False
        opened=db.scalar(select(HandoffEvent.id).where(HandoffEvent.conversation_id==conversation.conversation_id,
            HandoffEvent.closed_at.is_(None),HandoffEvent.deleted_at.is_(None)))
        value.update(owner='human' if opened else 'ai',revision=0,recall_streak=0)
    if not _newer(db,message,value,orders):
        return False
    source=(message.raw_payload or {}).get('sender_source')
    customer=message.sender_role=='customer'
    human=message.sender_role in {'self','sales','sales_candidate'} and source in {'human','human_confirmed','sales'}
    if not customer and not human:
        return False
    value.update(last_message_id=message.id,revision=int(value.get('revision',0))+1)
    if customer:
        value['recall_streak']=0
        value['silence_revision']=int(value.get('silence_revision',0))+1
    else:
        value['owner']='human'
    conversation.reply_ownership=value
    conversation.recall_cycle_id=conversation.recall_origin_status=conversation.next_recall_at=None
    at=aware(effective_at) if effective_at else business_time(message)
    if human:
        close_round(db,conversation)
        conversation.last_sales_reply_at=at
        conversation.sales_first_reply_at=conversation.sales_first_reply_at or at
        from app.core.config import get_settings
        conversation.next_recall_at=at+timedelta(hours=get_settings().c3_recall_after_hours) if at else None
        conversation.status='sales_replied_waiting_user'
    elif human_owned(conversation):
        _start_round(db,conversation,message,effective_at=at)
        conversation.status='waiting_sales_reply'
    from app.services.c3_service import cancel_active_batches_for_conversation_change
    db.flush()  # honor pending supersession before SQL filters with autoflush disabled
    cancel_active_batches_for_conversation_change(db,conversation.conversation_id,reason='回复责任或最新消息已变化')
    return True


def take_handoff(db,conversation,event,*,created):
    value=state(conversation)
    if value['owner']=='legacy':
        return  # no retroactive ownership/reminder backfill
    if created or value['owner']!='human':
        value.update(owner='human',revision=revision(conversation)+1,handoff_id=event.id)
        conversation.reply_ownership=value
    # Preserve the existing round's first unanswered timestamp on merged causes.
    if not state(conversation).get('round_id'):
        messages=list(db.scalars(select(MessageEvent).where(MessageEvent.id.in_(event.trigger_message_event_ids or []),
            MessageEvent.conversation_id==conversation.conversation_id,MessageEvent.sender_role=='customer')))
        messages=[m for m in messages if business_time(m)]
        if messages:
            first=min(messages,key=business_time)
            _start_round(db,conversation,first,not_before=(None if (first.raw_payload or {}).get('transport')=='hook' else event.created_at))
    conversation.next_recall_at=None
    # A reply-then-handoff boundary remains bound to its own newly created gate.
    batch=db.get(MessageBatch,event.batch_id) if event.batch_id else None
    if created and batch:
        batch.ownership_revision=revision(conversation)


def recall_succeeded(db,conversation,action,binding,batch):
    """First confirmed segment only; ACK cannot override newer human facts."""
    if not batch or batch.trigger_type!='recall' or action.segment_index!=1:
        return False
    if not allows_batch(conversation,batch) or not conversation.ai_enabled or conversation.status in {'closed','rejected'}:
        return False
    if db.scalar(select(HandoffEvent.id).where(HandoffEvent.conversation_id==conversation.conversation_id,
            HandoffEvent.closed_at.is_(None),HandoffEvent.deleted_at.is_(None))):return False
    value=state(conversation)
    if binding.native_self_wxid:
        # Recovery coverage updates the receipt pointer, deliberately not the
        # normal business-history projection. Use the same validated receipt
        # as original-send reconciliation, not the older projection.
        from app.services.hook_send_service import original_send_history
        checked=original_send_history(db,action,binding)
        if checked is None:
            return False
        rows_by_native={str((m.raw_payload or {}).get('native_msg_id')):m for m in db.scalars(
            select(MessageEvent).where(MessageEvent.conversation_id==conversation.conversation_id))}
        native_ids=[r['MsgSvrID'] for r in checked['semantic_rows']]
        if any(mid not in rows_by_native for mid in native_ids):
            return False
        ordered=[rows_by_native[mid] for mid in native_ids]
        ids=[m.id for m in ordered]
        rows={m.id:m for m in ordered}
        sent=[i for i,m in enumerate(ordered) if (m.raw_payload or {}).get('ai_reply_action_id')==action.id]
        if len(sent)!=1:
            return False
        position=sent[0]
        anchor=value.get('last_message_id')
        if anchor and anchor not in ids:
            return False
        start=ids.index(anchor)+1 if anchor else 0
        def may_be_human(message):
            return message.sender_role in {'self','sales','sales_candidate'} and (message.raw_payload or {}).get('sender_source') not in {'ai','ai_pending_ack'}
        if any(rows[mid].sender_role=='customer' or may_be_human(rows[mid]) for mid in ids[start:position]):
            return False
        if any(may_be_human(rows[mid]) for mid in ids[position+1:]):
            return False
        value['last_message_id']=ids[position]
    value['owner']='ai'
    # The same revision admits the remaining segments of this current recall.
    conversation.reply_ownership=value
    close_round(db,conversation)
    return True


def record_recall_success(conversation,action,batch):
    """Account for a first-segment success independently of reply ownership.

    Called once by original ACK settlement. Human replies never end customer
    silence; a newer customer turn fences late ACKs out of the new silence epoch.
    """
    if not batch or batch.trigger_type!='recall' or action.segment_index!=1:
        return
    from zoneinfo import ZoneInfo
    local_today=aware(action.sent_at).astimezone(ZoneInfo('Asia/Shanghai')).date()
    if conversation.recall_daily_date is None or conversation.recall_daily_date<=local_today:
        if conversation.recall_daily_date!=local_today:
            conversation.recall_daily_date=local_today
            conversation.recall_daily_count=0
        conversation.recall_daily_count=int(conversation.recall_daily_count or 0)+1
    conversation.recall_count=int(conversation.recall_count or 0)+1
    value=state(conversation)
    if value['owner']!='legacy' and batch.silence_revision==int(value.get('silence_revision',0)):
        value['recall_streak']=int(value.get('recall_streak',0))+1
        conversation.reply_ownership=value
