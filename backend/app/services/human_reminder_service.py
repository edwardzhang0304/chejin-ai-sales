"""Durable, per-unanswered-round reminders through the existing Feishu adapter."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.core.database import SessionLocal
from app.errors import AppError
from app.models.base import utcnow
from app.models.c3 import Conversation, HumanReplyReminder
from app.models.wechat import WechatSessionBinding
from app.models.sales import Sales
from app.services.reply_ownership import aware, state
from app.services.feishu_adapter import get_feishu_adapter, FeishuAdapterError
from app.services.audit_service import write_system_log


def next_daytime(now):
    local=now.astimezone(ZoneInfo('Asia/Shanghai'))
    if local.hour>=21:
        return (local+timedelta(days=1)).replace(hour=9,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    if local.hour<9:
        return local.replace(hour=9,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    return now


def _current(conversation,row,binding):
    return bool(conversation and conversation.deleted_at is None and conversation.status not in {'closed','rejected'}
        and state(conversation)['owner']=='human' and state(conversation).get('round_id')==row.id
        and row.closed_at is None and binding and binding.deleted_at is None
        and binding.bind_status=='bound' and binding.allow_listening)


def _facts_ready(binding,row,now):
    from app.services.worker_service import computed_online_status, ONLINE_TIMEOUT_SECONDS
    from app.services.hook_history_service import ensure_history_context
    worker=binding.worker
    if not worker or computed_online_status(worker)!='online':
        return False
    if binding.unread_hint and int(binding.unread_generation or 0)>int(binding.consumed_unread_generation or 0):
        return False
    if binding.followup_restore_pending or (binding.recovery_hold or {}).get('status') in {'active','suspended'}:
        return False
    after=max(filter(None,[aware(row.next_attempt_at),aware(worker.reply_history_required_after)]))
    if binding.native_self_wxid:
        ctx=binding.native_context or {}
        try:
            ensure_history_context(binding)
        except AppError:
            return False
        checked=aware(ctx.get('history_observed_at'))
        return bool(ctx.get('actors_ready') and ctx.get('history_reason_code') in {None,'HOOK_VOICE_PENDING'}
            and checked and checked>=after
            and timedelta(0)<=now-checked<=timedelta(seconds=ONLINE_TIMEOUT_SECONDS))
    checked=aware(binding.last_read_completed_at)
    return bool(binding.last_read_result in {'new_facts','no_change'} and checked and checked>=after
        and timedelta(0)<=now-checked<=timedelta(seconds=ONLINE_TIMEOUT_SECONDS))


def _followup_allowed(db,conversation):
    from app.services.followup_eligibility import followup_block_reason
    try:
        return followup_block_reason(db,conversation.lead_id,lock=False) is None
    except AppError as exc:
        if exc.code=='LEAD_NOT_FOUND':
            return False
        raise


def dispatch_reminder(reminder_id,*,adapter=None):
    # Conversation lock serializes human replies/reinvites against the final
    # pre-send check. No Worker or model work is triggered by a reminder.
    with SessionLocal() as db:
        hint=db.get(HumanReplyReminder,reminder_id)
        if not hint:
            return 'missing'
        conversation_id=hint.conversation_id
        conversation=db.scalar(select(Conversation).where(Conversation.conversation_id==conversation_id).with_for_update().execution_options(populate_existing=True))
        row=db.scalar(select(HumanReplyReminder).where(HumanReplyReminder.id==reminder_id).with_for_update().execution_options(populate_existing=True))
        binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id==row.conversation_id,
            WechatSessionBinding.deleted_at.is_(None)))
        now=utcnow()
        if not _current(conversation,row,binding) or not _followup_allowed(db,conversation):
            if row.closed_at is None:
                row.closed_at=now
            if row.status in {'pending','failed'}:
                row.status='cancelled'
            db.commit()
            return 'closed'
        if row.status not in {'pending','failed'} or row.attempt_count>=3 or aware(row.next_attempt_at)>now:
            return 'not_due'
        # Rotate checked rounds independently of their business due time. A
        # disconnected customer's original timer must not starve other rounds.
        row.last_checked_at=now
        permitted=next_daytime(now)
        if permitted>now:
            row.next_attempt_at=permitted
            db.commit()
            return 'quiet'
        if not _facts_ready(binding,row,now):
            db.commit()
            return 'await_history'
        row.status='sending'
        row.attempt_count+=1
        row.attempted_at=now
        db.commit()  # crash after this point is unknown, never retried blindly
    with SessionLocal() as db:
        from app.services.followup_eligibility import lock_leads
        hint=db.get(Conversation,conversation_id)
        lock_leads(db,[hint.lead_id] if hint else [])
        conversation=db.scalar(select(Conversation).where(Conversation.conversation_id==conversation_id).with_for_update().execution_options(populate_existing=True))
        row=db.scalar(select(HumanReplyReminder).where(HumanReplyReminder.id==reminder_id).with_for_update().execution_options(populate_existing=True))
        binding=db.scalar(select(WechatSessionBinding).where(WechatSessionBinding.conversation_id==row.conversation_id,
            WechatSessionBinding.deleted_at.is_(None)))
        now=utcnow()
        if row.status!='sending':
            return 'not_claimed'
        if (not _current(conversation,row,binding) or not _followup_allowed(db,conversation)
                or not _facts_ready(binding,row,now) or next_daytime(now)>now):
            # No external call happened. Preserve consumed claim, do not assert
            # success. This round can only be retried under the normal budget.
            result='failed'
            code='HUMAN_REMINDER_PRE_SEND_CHANGED'
        else:
            try:
                sales=db.get(Sales,conversation.sales_id) if conversation.sales_id else None
                if not sales or sales.deleted_at or not sales.feishu_user_id:
                    raise FeishuAdapterError('FEISHU_OPEN_ID_MISSING','missing_sales_recipient')
                if not binding.remark_code:
                    raise FeishuAdapterError('FEISHU_MESSAGE_SEND_FAILED','missing_customer_code')
                message=f'客户仍在等待您的回复，请及时处理。\n客户短码：{binding.remark_code}\n本轮等待开始：{aware(row.started_at).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")}\n请前往微信处理。'
                (adapter or get_feishu_adapter()).send_text_message(sales.feishu_user_id,message)
                result='succeeded'
                code=None
            except FeishuAdapterError as exc:
                result='unknown' if exc.result_unknown else 'failed'
                code=exc.code
            except Exception:
                result='unknown'
                code='FEISHU_NOTIFY_RESULT_UNKNOWN'
        row.status=result
        row.error_code=code
        row.attempts=[*(row.attempts or []),{'number':row.attempt_count,'at':now.isoformat(),'result':result,'error_code':code}]
        if result=='failed' and row.attempt_count<3:
            row.next_attempt_at=next_daytime(now+timedelta(minutes=1 if row.attempt_count==1 else 5))
        write_system_log(db,event_type='human_reply_reminder_'+result,module='c3',target_type='human_reply_reminder',
            target_id=row.id,lead_id=conversation.lead_id,metadata={'conversation_id':row.conversation_id,'attempt':row.attempt_count,'error_code':code})
        db.commit()
        return result


def recover_reminders_once(*,adapter=None,limit=20):
    now=utcnow()
    from app.core.config import get_settings
    # A bounded provider request may still be running in another server. Do
    # not recover a fresh claim as unknown while its owner is inside the call.
    cutoff=now-timedelta(seconds=max(120,4*get_settings().feishu_http_timeout_seconds))
    with SessionLocal() as db:
        for row in db.scalars(select(HumanReplyReminder).where(HumanReplyReminder.status=='sending',
                HumanReplyReminder.attempted_at<cutoff).with_for_update(skip_locked=True)):
            row.status='unknown'
            row.error_code='FEISHU_NOTIFY_RESULT_UNKNOWN'
            row.attempts=[*(row.attempts or []),{'number':row.attempt_count,'at':now.isoformat(),'result':'unknown','error_code':row.error_code}]
        ids=list(db.scalars(select(HumanReplyReminder.id).where(HumanReplyReminder.closed_at.is_(None),
            HumanReplyReminder.status.in_({'pending','failed'}),HumanReplyReminder.attempt_count<3,
            HumanReplyReminder.next_attempt_at<=now).order_by(
                HumanReplyReminder.last_checked_at.asc().nullsfirst(),
                HumanReplyReminder.next_attempt_at,HumanReplyReminder.id).limit(limit)))
        db.commit()
    results=[dispatch_reminder(mid,adapter=adapter) for mid in ids]
    return {'examined':len(ids),'attempted':sum(r in {'succeeded','failed','unknown'} for r in results)}
