"""Real binding/history/round producer -> reminder consumer; no real Feishu send."""
from datetime import datetime,timedelta,timezone
import uuid
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation,HumanReplyReminder,MessageBatch
from app.models.wechat import WechatSessionBinding,MessageEvent
from app.models.sales import Sales
from app.services import human_reminder_service as reminders, hook_history_service,reply_ownership
from app.services.feishu_adapter import FeishuAdapterError
from app.contracts.shared_rules import shared_adapter
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound,coverage,upload
from test_hook_handoff_history import append_row


def send_history(w,p,b,value,monkeypatch,now):
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:now)
    for scan in value['scans']:scan['observed_at']=now.isoformat()
    payload={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
        'capture_id':str(uuid.uuid4()),'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value)}
    response=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",payload)
    assert response.status_code==200,response.text
    return response.json()['data']


def unanswered(monkeypatch):
    w,t,p,b=bound();value=coverage(b)
    for scan in value['scans']:scan['databases'][0]['pages'][0][1]['IsSender']='1'
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    append_row(value,text='销售您好，请继续帮我看看')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    assert response.json()['data']['message_batch'] is None
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id']);row=db.scalar(select(HumanReplyReminder))
        assert conv.reply_ownership['owner']=='human' and conv.status=='waiting_sales_reply'
        rid=row.id;start=reminders.aware(row.started_at)
        sales=db.get(Sales,conv.sales_id);sales.feishu_user_id='ou_synthetic_owner';db.commit()
    return w,p,b,value,rid,start


class Adapter:
    def __init__(self,results=()):self.calls=[];self.results=list(results)
    def send_text_message(self,recipient,text):
        self.calls.append((recipient,text))
        if self.results:
            error=self.results.pop(0)
            if error:raise error


def daytime_after(start):
    return (start+timedelta(days=1)).replace(hour=2,minute=0,second=0,microsecond=0)


def test_human_cycle_voice_does_not_start_ai_or_transcription_and_replay_keeps_timer(monkeypatch):
    w,p,b,value,rid,start=unanswered(monkeypatch)
    value['schema_version']='hook-history-coverage-v2'
    append_row(value,kind='34',text='<msg><voicemsg length="43"/></msg>')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    assert response.json()['data']['history_ready'] and response.json()['data']['message_batch'] is None
    inv=post(w,f"/workers/{w['id']}/wechat/native-work",{**p,'retained_flow_id':None}).json()['data']
    assert inv['bindings'][0]['voice_jobs']==[]
    for _ in range(2):upload(w,p,b,value,monkeypatch)
    with SessionLocal() as db:
        rows=list(db.scalars(select(HumanReplyReminder)));assert len(rows)==1
        assert rows[0].id==rid and reminders.aware(rows[0].started_at)==start
        assert not list(db.scalars(select(MessageBatch).where(MessageBatch.active.is_(True))))


def test_actual_round_reminder_once_then_sales_closes_and_next_round_reopens(monkeypatch):
    w,p,b,value,rid,start=unanswered(monkeypatch);now=daytime_after(start)
    adapter=Adapter();monkeypatch.setattr(reminders,'utcnow',lambda:now)
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='await_history'
    send_history(w,p,b,value,monkeypatch,now)
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='succeeded'
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='not_due'
    assert len(adapter.calls)==1 and adapter.calls[0][0]=='ou_synthetic_owner'
    append_row(value,sender='1',text='我来接着为您处理')
    send_history(w,p,b,value,monkeypatch,now)
    with SessionLocal() as db:
        assert db.get(HumanReplyReminder,rid).closed_at
        assert db.get(Conversation,b['conversation_id']).reply_ownership['owner']=='human'
    append_row(value,text='好的，还有一件事')
    send_history(w,p,b,value,monkeypatch,now)
    with SessionLocal() as db:
        rows=list(db.scalars(select(HumanReplyReminder)));assert len(rows)==2
        assert len([r for r in rows if r.closed_at is None])==1


@pytest.mark.parametrize('unknown',[False,True])
def test_reminder_retry_schedule_and_unknown_are_durable(monkeypatch,unknown):
    w,p,b,value,rid,start=unanswered(monkeypatch);now=daytime_after(start)
    monkeypatch.setattr(reminders,'utcnow',lambda:now)
    adapter=Adapter([FeishuAdapterError('FAIL','synthetic',result_unknown=unknown)]*3)
    send_history(w,p,b,value,monkeypatch,now)
    assert reminders.dispatch_reminder(rid,adapter=adapter)==('unknown' if unknown else 'failed')
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='not_due'
    if not unknown:
        for minutes in (1,5):
            now+=timedelta(minutes=minutes)
            send_history(w,p,b,value,monkeypatch,now)
            assert reminders.dispatch_reminder(rid,adapter=adapter)=='failed'
    now+=timedelta(hours=1)
    send_history(w,p,b,value,monkeypatch,now)
    reminders.recover_reminders_once(adapter=adapter)
    assert len(adapter.calls)==(1 if unknown else 3)


@pytest.mark.parametrize('hour,next_day',[('13:00',1),('16:00',1),('00:59',0),('01:00',0)])
def test_quiet_hours(hour,next_day):
    h,m=map(int,hour.split(':'));now=datetime(2026,10,22,h,m,tzinfo=timezone.utc)
    result=reminders.next_daytime(now)
    if h>=13 or h<1:
        assert result.astimezone(timezone(timedelta(hours=8))).hour==9
        assert (result.date()-now.date()).days==next_day
    else:assert result==now


def test_sales_reply_before_due_cancels_round_without_notification(monkeypatch):
    w,p,b,value,rid,start=unanswered(monkeypatch)
    append_row(value,sender='1',text='已经给您答复')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200
    now=daytime_after(start);monkeypatch.setattr(reminders,'utcnow',lambda:now)
    adapter=Adapter();assert reminders.dispatch_reminder(rid,adapter=adapter)=='closed'
    assert adapter.calls==[]


def test_offline_reconnect_requires_new_history_and_human_reply_cancels(monkeypatch):
    from app.models.worker import Worker
    from app.services import worker_service
    from test_hook_account_http import heartbeat,context
    w,p,b,value,rid,start=unanswered(monkeypatch);now=daytime_after(start)
    monkeypatch.setattr(reminders,'utcnow',lambda:now)
    send_history(w,p,b,value,monkeypatch,now)
    with SessionLocal() as db:
        db.get(Worker,w['id']).online_status='offline';db.commit()
    adapter=Adapter();assert reminders.dispatch_reminder(rid,adapter=adapter)=='await_history'
    now+=timedelta(seconds=1)
    monkeypatch.setattr(worker_service,'utcnow',lambda:now)
    assert heartbeat(w,context(account_epoch=p['account_epoch'])).status_code==200
    # A fresh heartbeat cannot make the pre-reconnect conversation snapshot fresh.
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='await_history'
    append_row(value,sender='1',text='断线时已由销售回复')
    send_history(w,p,b,value,monkeypatch,now)
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='closed'
    assert adapter.calls==[]


def test_abandoned_notification_claim_becomes_unknown_without_resend(monkeypatch):
    w,p,b,value,rid,start=unanswered(monkeypatch);now=daytime_after(start)
    monkeypatch.setattr(reminders,'utcnow',lambda:now)
    with SessionLocal() as db:
        row=db.get(HumanReplyReminder,rid);row.status='sending';row.attempt_count=1
        row.attempted_at=now-timedelta(hours=1);db.commit()
    adapter=Adapter();reminders.recover_reminders_once(adapter=adapter)
    send_history(w,p,b,value,monkeypatch,now)
    reminders.recover_reminders_once(adapter=adapter)
    with SessionLocal() as db:
        row=db.get(HumanReplyReminder,rid)
        assert row.status=='unknown' and row.attempt_count==1 and len(row.attempts)==1
    assert adapter.calls==[]


def test_legacy_history_does_not_backfill_but_new_message_starts_new_cycle(monkeypatch):
    w,t,p,b=bound();value=coverage(b)
    since=datetime.fromtimestamp(int(value['scans'][0]['databases'][0]['pages'][0][-1]['CreateTime'])+1,timezone.utc)
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id']);conv.reply_ownership={
            'owner':'legacy','revision':0,'recall_streak':0,'since':since.isoformat()}
        db.commit()
    first,_=upload(w,p,b,value,monkeypatch)
    assert first.status_code==200 and first.json()['data']['message_batch'] is None
    with SessionLocal() as db:
        assert db.get(Conversation,b['conversation_id']).reply_ownership['owner']=='legacy'
        assert db.scalar(select(HumanReplyReminder)) is None
    append_row(value,text='更新后的新问题')
    for scan in value['scans']:
        row=scan['databases'][0]['pages'][0][-1]
        row['CreateTime']=str(int(since.timestamp())+1)
        row['Sequence']=str((int(since.timestamp())+1)*1000)
    result=send_history(w,p,b,value,monkeypatch,since+timedelta(seconds=2))
    assert result['message_batch']
    with SessionLocal() as db:
        assert db.get(Conversation,b['conversation_id']).reply_ownership['owner']=='ai'
        assert db.scalar(select(HumanReplyReminder)) is None


def test_initial_handoff_uses_first_server_receipt_and_merged_cause_keeps_round(monkeypatch):
    from test_hook_native_media import media_coverage
    from app.models.c3 import HandoffEvent
    w,t,p,b=bound();value=media_coverage(b,'3')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    with SessionLocal() as db:
        event=db.scalar(select(HandoffEvent));row=db.scalar(select(HumanReplyReminder))
        message=db.get(MessageEvent,row.first_message_id)
        assert reminders.aware(row.started_at)==reply_ownership.business_time(message)
        assert reminders.aware(row.next_attempt_at)-reminders.aware(row.started_at)==timedelta(hours=1)
        rid=row.id;start=row.started_at
    append_row(value,kind='3',text='<msg><img md5="second"/></msg>')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    with SessionLocal() as db:
        assert len(list(db.scalars(select(HandoffEvent))))==1
        assert len(list(db.scalars(select(HumanReplyReminder))))==1
        assert db.get(HumanReplyReminder,rid).started_at==start


def test_new_customer_resets_only_silence_streak_not_daily_or_total(monkeypatch):
    w,p,b,value,rid,start=unanswered(monkeypatch)
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id'])
        conv.reply_ownership={**conv.reply_ownership,'recall_streak':3}
        conv.recall_count=8;conv.recall_daily_count=1
        db.commit()
    upload(w,p,b,value,monkeypatch)
    with SessionLocal() as db:assert db.get(Conversation,b['conversation_id']).reply_ownership['recall_streak']==3
    append_row(value,text='这是新的客户回复')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id'])
        assert conv.reply_ownership['recall_streak']==0
        assert (conv.recall_count,conv.recall_daily_count)==(8,1)
        assert db.get(HumanReplyReminder,rid).started_at==start.replace(tzinfo=None)


def test_failed_retry_crossing_quiet_hours_keeps_original_round(monkeypatch):
    w,p,b,value,rid,start=unanswered(monkeypatch)
    now=daytime_after(start).replace(hour=12,minute=59,second=30)
    monkeypatch.setattr(reminders,'utcnow',lambda:now)
    send_history(w,p,b,value,monkeypatch,now)
    adapter=Adapter([FeishuAdapterError('FAIL','synthetic')])
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='failed'
    with SessionLocal() as db:
        row=db.get(HumanReplyReminder,rid)
        assert reminders.aware(row.started_at)==start
        retry=reminders.aware(row.next_attempt_at)
        assert retry==now.replace(hour=1,minute=0,second=0)+timedelta(days=1)
    now=retry-timedelta(seconds=1)
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='not_due'
    now=retry
    send_history(w,p,b,value,monkeypatch,now)
    assert reminders.dispatch_reminder(rid,adapter=adapter)=='succeeded'
    assert len(adapter.calls)==2


def test_unready_older_rounds_do_not_starve_ready_customer(monkeypatch):
    from copy import deepcopy
    # The eligible target and its round come from actual task/binding/history APIs.
    w,p,b,value,rid,start=unanswered(monkeypatch)
    now=daytime_after(start)
    monkeypatch.setattr(reminders,'utcnow',lambda:now)
    send_history(w,p,b,value,monkeypatch,now)
    with SessionLocal() as db:
        original=db.get(Conversation,b['conversation_id'])
        binding=db.get(WechatSessionBinding,b['binding_id'])
        # Queue-scale fixture: distinct synthetic customers awaiting fresh history.
        # No mocks in recovery scheduler, dispatch, eligibility or adapter handling.
        for n in range(20):
            cid=f'blocked-customer-{n:02d}';round_id=f'blocked-round-{n:02d}'
            conv=Conversation(conversation_id=cid,lead_id=original.lead_id,sales_id=original.sales_id,
                worker_id=original.worker_id,status='waiting_sales_reply',reply_ownership={
                    'owner':'human','revision':1,'recall_streak':0,'round_id':round_id})
            data={c.name:deepcopy(getattr(binding,c.name)) for c in binding.__table__.columns
                if c.name not in {'id','conversation_id'}}
            data['native_peer_wxid']=f'wxid_blocked_{n}'
            data['remark_code']=f'BLOCKED{n}'
            data['native_context']={**data['native_context'],'history_observed_at':(now-timedelta(days=2)).isoformat()}
            db.add_all([conv,WechatSessionBinding(id=f'blocked-binding-{n:02d}',conversation_id=cid,**data),
                HumanReplyReminder(id=round_id,conversation_id=cid,first_message_id=f'old-msg-{n}',
                    started_at=start-timedelta(hours=3),next_attempt_at=start-timedelta(hours=2))])
        db.commit()
    adapter=Adapter()
    results=[reminders.recover_reminders_once(adapter=adapter) for _ in range(3)]
    with SessionLocal() as db:status=db.get(HumanReplyReminder,rid).status
    assert len(adapter.calls)==1 and status=='succeeded'
    with SessionLocal() as db:
        blocked=list(db.scalars(select(HumanReplyReminder).where(HumanReplyReminder.id.like('blocked-round-%'))))
        assert len(blocked)==20
        assert all(r.status=='pending' and r.attempt_count==0 and r.closed_at is None for r in blocked)
        assert all(reminders.aware(r.started_at)==start-timedelta(hours=3) for r in blocked)
        assert all(reminders.aware(r.next_attempt_at)==start-timedelta(hours=2) for r in blocked)
        assert all(r.last_checked_at is not None for r in blocked)


def test_late_success_accounting_does_not_enter_new_customer_silence_epoch(monkeypatch):
    from types import SimpleNamespace
    # Actual verified customer ingestion produces the epoch; this focused
    # accounting test represents an older first segment settling afterwards.
    w,p,b,value,rid,start=unanswered(monkeypatch)
    with SessionLocal() as db:
        previous=int(db.get(Conversation,b['conversation_id']).reply_ownership['silence_revision'])
    append_row(value,text='客户真正提出新问题，开始新的阶段')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id'])
        assert conv.reply_ownership['silence_revision']==previous+1
        batch=SimpleNamespace(trigger_type='recall',silence_revision=previous)
        action=SimpleNamespace(segment_index=1,sent_at=start)
        reply_ownership.record_recall_success(conv,action,batch)
        assert conv.reply_ownership['recall_streak']==0
        assert conv.reply_ownership['owner']=='human'
        assert conv.recall_count==1 and conv.recall_daily_count==1
        # Later segment success is not a second successful recall cycle.
        action.segment_index=2
        reply_ownership.record_recall_success(conv,action,batch)
        assert conv.recall_count==1 and conv.recall_daily_count==1


def test_hook_business_timer_uses_first_ingestion_not_skewed_native_time():
    from types import SimpleNamespace
    captured=datetime(2026,10,28,12,0,tzinfo=timezone.utc)
    for offset in (-3600,3600):
        message=SimpleNamespace(raw_payload={'transport':'hook'},ingested_at=captured,
            occurred_at=captured+timedelta(seconds=offset))
        assert reply_ownership.business_time(message)==captured
        # Native chronology is preserved for ordering, not rewritten for timers.
        assert message.occurred_at==captured+timedelta(seconds=offset)
        message.raw_payload={'transport':'ocr'}
        assert reply_ownership.business_time(message)==message.occurred_at
