"""Clock projection compatibility uses receipts; no original message/ACK rewrite."""
from datetime import timedelta
from copy import deepcopy
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HumanReplyReminder
from app.models.hook_native import HookHistoryReceipt
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.services.hook_business_time import aware, received_at, restore_business_clock
from app.services.reply_ownership import business_time
from test_hook_business_http import clean
from test_human_reply_ownership import unanswered


def test_old_summary_rebuilt_without_resetting_owner_or_round(monkeypatch):
    w,p,b,value,rid,start=unanswered(monkeypatch)
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);conv=db.get(Conversation,b['conversation_id'])
        reminder=db.get(HumanReplyReminder,rid)
        owner=deepcopy(conv.reply_ownership);round_id=reminder.id
        messages=list(db.scalars(select(MessageEvent).where(MessageEvent.binding_id==binding.id)))
        original={m.id:(m.occurred_at, m.content) for m in messages}
        receipt_bodies={r.id:deepcopy(r.payload_json) for r in db.scalars(select(HookHistoryReceipt))}
        # The old release had native times in summaries. Persist before reentry.
        conv.last_inbound_at=aware(conv.last_inbound_at)+timedelta(hours=1)
        conv.last_sales_reply_at=aware(conv.last_sales_reply_at)+timedelta(hours=1)
        reminder.started_at=aware(reminder.started_at)+timedelta(hours=1)
        reminder.next_attempt_at=aware(reminder.next_attempt_at)+timedelta(hours=1)
        binding.native_context={k:v for k,v in binding.native_context.items() if k!='business_clock_version'}
        db.commit()
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);conv=db.get(Conversation,b['conversation_id'])
        restore_business_clock(db,binding,conv);db.commit()
        reminder=db.get(HumanReplyReminder,rid)
        assert conv.reply_ownership==owner and reminder.id==round_id and reminder.attempt_count==0
        assert aware(reminder.started_at)==start
        assert aware(reminder.next_attempt_at)==start+timedelta(hours=1)
        before=deepcopy(binding.native_context)
        restore_business_clock(db,binding,conv)
        assert binding.native_context==before
        for m in db.scalars(select(MessageEvent)):
            assert (m.occurred_at,m.content)==original[m.id]
        assert {r.id:r.payload_json for r in db.scalars(select(HookHistoryReceipt))}==receipt_bodies


def test_receipt_time_not_delayed_dispatch_or_native_clock(monkeypatch):
    w,p,b,value,rid,start=unanswered(monkeypatch)
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);conv=db.get(Conversation,b['conversation_id'])
        reminder=db.get(HumanReplyReminder,rid);message=db.get(MessageEvent,reminder.first_message_id)
        first=aware(message.ingested_at)-timedelta(hours=3)
        for receipt in db.scalars(select(HookHistoryReceipt)):
            if message.id in receipt.result_json.get('message_event_ids',[]):
                receipt.created_at=first
        message.raw_payload={k:v for k,v in message.raw_payload.items() if k not in {'server_received_at','business_clock_source'}}
        message.occurred_at=first+timedelta(hours=12)
        binding.native_context={k:v for k,v in binding.native_context.items() if k!='business_clock_version'}
        restore_business_clock(db,binding,conv)
        assert received_at(message)==first and business_time(message)==first
        assert aware(reminder.started_at)==first
        assert aware(message.occurred_at)==first+timedelta(hours=12)


def test_upgrade_pass_finds_future_timer_before_due_selection(monkeypatch):
    from app.services.hook_business_time import restore_pending_clocks, timers_ready
    w,p,b,value,rid,start=unanswered(monkeypatch)
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id'])
        reminder=db.get(HumanReplyReminder,rid)
        reminder.next_attempt_at=start+timedelta(days=365)
        binding.native_context={k:v for k,v in binding.native_context.items() if k!='business_clock_version'}
        db.commit()
    assert restore_pending_clocks()=={'examined':1,'recovered':1,'failed':0}
    with SessionLocal() as db:
        assert aware(db.get(HumanReplyReminder,rid).next_attempt_at)==start+timedelta(hours=1)
        assert timers_ready(db.get(WechatSessionBinding,b['binding_id']))
        assert restore_pending_clocks()=={'examined':0,'recovered':0,'failed':0}


def test_unprovable_old_summary_retained_and_cannot_start_recall(monkeypatch):
    from app.services.hook_business_time import timers_ready
    from app.services.wechat_service import _prepare_due_recall
    w,p,b,value,rid,start=unanswered(monkeypatch)
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);conv=db.get(Conversation,b['conversation_id'])
        ctx=dict(binding.native_context);ctx.pop('business_clock_version',None)
        ctx.pop('previous_business_clock_summary',None);ctx['actor_applied_message_ids']=[]
        binding.native_context=ctx
        old=aware(conv.last_inbound_at).isoformat()
        restore_business_clock(db,binding,conv)
        assert not timers_ready(binding)
        assert binding.native_context['previous_business_clock_summary']['last_inbound_at']==old
        assert 'last_inbound_at' in binding.native_context['business_clock_missing_sources']
        _prepare_due_recall(conv,binding=binding,db=db)
        assert conv.status=='waiting_sales_reply' and conv.recall_cycle_id is None
        assert db.get(HumanReplyReminder,rid).closed_at is None


def test_delayed_callback_dispatch_keeps_earliest_durable_arrival(monkeypatch):
    from app.models.hook_native import HookNativeReceipt
    from test_hook_business_http import native_payload, post
    w,p,b,value,rid,start=unanswered(monkeypatch)
    row=value['scans'][0]['databases'][0]['pages'][0][-1]
    payload=native_payload(w,b,mid=row['MsgSvrID'],text=row['StrContent'])
    stored=post(w,f"/workers/{w['id']}/wechat/messages/native-observations",payload)
    assert stored.status_code==200,stored.text
    with SessionLocal() as db:
        receipt=db.scalar(select(HookNativeReceipt).where(HookNativeReceipt.event_uuid==payload['event_uuid']))
        # Model durable receipt arriving before a delayed dispatcher. Original
        # native occurrence and later history scan are deliberately different.
        first=start-timedelta(minutes=30)
        receipt.created_at=first
        db.commit()
    result=post(w,f"/workers/{w['id']}/wechat/native-events/{payload['event_uuid']}/dispatch",None)
    assert result.status_code==200,result.text
    assert result.json()['data']['new_message_event_ids']==[]
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);conv=db.get(Conversation,b['conversation_id'])
        assert 'business_clock_version' not in binding.native_context
        restore_business_clock(db,binding,conv)
        reminder=db.get(HumanReplyReminder,rid)
        assert aware(reminder.started_at)==first
        assert aware(reminder.next_attempt_at)==first+timedelta(hours=1)
        db.commit()
    replay=post(w,f"/workers/{w['id']}/wechat/native-events/{payload['event_uuid']}/dispatch",None)
    assert replay.status_code==200
    with SessionLocal() as db:
        assert aware(db.get(HumanReplyReminder,rid).started_at)==first
