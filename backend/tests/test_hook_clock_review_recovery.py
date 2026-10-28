"""Cross-module recovery regression; synthetic customers, real HTTP producers."""
from copy import deepcopy
from datetime import timedelta, datetime, timezone
import logging
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from app.core.database import SessionLocal, engine
from app.models.c3 import Conversation, HumanReplyReminder, MessageBatch, ReplyAction
from app.models.wechat import WechatSessionBinding, MessageEvent
from app.services import human_reminder_service as reminders, c3_service
from test_hook_business_http import clean, client, post, proof, body
from test_hook_history_business_http import coverage, upload
from test_hook_history_review import legacy_binding, add_message, review_url
from test_human_reply_ownership import unanswered, daytime_after, Adapter, send_history
from test_hook_handoff_history import append_row


def second_binding(w, p):
    r=client.post('/api/leads',json={'customer_name':'Synthetic healthy customer','phones':['13800000004']})
    assert r.status_code==200,r.text
    r=client.get(f"/api/workers/{w['id']}/tasks/pull",headers={'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
    t=r.json()['data']['task'];assert t
    assert post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':t['id'],'flow_kind':'task'}).status_code==200
    h={'X-Inflight-Flow-Id':t['id']}
    r=post(w,f"/tasks/{t['id']}/claim",{'worker_id':w['id']},h);assert r.status_code==200,r.text
    h['X-Task-Lease-Fencing-Token']=str(r.json()['data']['lease_fencing_token'])
    r=post(w,f"/tasks/{t['id']}/hook-invite/prepare",p,h);assert r.status_code==200,r.text
    prep=r.json()['data']
    assert post(w,f"/tasks/{t['id']}/hook-invite/submitted",proof(p,prep),h).status_code==200
    assert post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':t['id'],'terminal_kind':'task_terminal'},h).status_code==200
    r=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(t,p,prep,('wxid_healthy',)))
    assert r.status_code==200,r.text
    return r.json()['data']


@pytest.mark.parametrize('broken_logger',[False,True])
def test_clock_failure_rolls_back_only_customer_and_never_blocks_unknown_closure(monkeypatch,caplog,broken_logger):
    w,p,b,value,rid,start=unanswered(monkeypatch)
    healthy=second_binding(w,p)
    healthy_value=coverage(healthy)
    for scan in healthy_value['scans']:scan['databases'][0]['pages'][0][1]['IsSender']='1'
    assert upload(w,p,healthy,healthy_value,monkeypatch)[0].status_code==200
    append_row(healthy_value,text='合成客户继续咨询')
    assert upload(w,p,healthy,healthy_value,monkeypatch)[0].status_code==200
    now=daytime_after(start);monkeypatch.setattr(reminders,'utcnow',lambda:now)
    send_history(w,p,healthy,healthy_value,monkeypatch,now)
    with SessionLocal() as db:
        for bid in (b['binding_id'],healthy['binding_id']):
            bind=db.get(WechatSessionBinding,bid)
            bind.native_context={k:v for k,v in bind.native_context.items() if k!='business_clock_version'}
        healthy_reminder=db.scalar(select(HumanReplyReminder).where(HumanReplyReminder.conversation_id==healthy['conversation_id']))
        healthy_rid=healthy_reminder.id
        healthy_reminder.next_attempt_at=now+timedelta(days=365)
        row=db.get(HumanReplyReminder,rid);row.status='sending';row.attempt_count=1;row.attempted_at=now-timedelta(hours=1)
        msg=db.scalar(select(MessageEvent).where(MessageEvent.binding_id==b['binding_id']))
        msg.raw_payload={k:v for k,v in msg.raw_payload.items() if k not in {'server_received_at','business_clock_source'}}
        original_raw=deepcopy(msg.raw_payload);mid=msg.id
        db.commit()
    # Fail after repair has stamped messages and autoflushed, proving rollback.
    active_bad=False
    def fail(conn,cursor,statement,params,ctx,many):
        nonlocal active_bad
        if 'FROM message_events' in statement and 'message_events.binding_id =' in statement:
            active_bad=b['binding_id'] in params
        if active_bad and 'JOIN sent_acks' in statement:
            active_bad=False
            raise OperationalError(statement,params,Exception('PRIVATE_SYNTHETIC_PAYLOAD'))
    if broken_logger:
        monkeypatch.setattr(logging.getLogger('app.services.hook_business_time'),'log',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('handler failed')))
    adapter=Adapter();event.listen(engine,'before_cursor_execute',fail)
    try:
        for attempt in range(2):
            result=reminders.recover_reminders_once(adapter=adapter)
            assert result['failed']==1
            assert result['recovered']==(1 if attempt==0 else 0)
            with SessionLocal() as db:
                assert db.get(HumanReplyReminder,rid).status=='unknown'
                assert db.get(HumanReplyReminder,rid).attempt_count==1
                assert 'business_clock_version' not in db.get(WechatSessionBinding,b['binding_id']).native_context
                assert db.get(WechatSessionBinding,healthy['binding_id']).native_context['business_clock_version']==1
                assert db.get(MessageEvent,mid).raw_payload==original_raw
    finally:event.remove(engine,'before_cursor_execute',fail)
    assert len(adapter.calls)==1
    with SessionLocal() as db:assert db.get(HumanReplyReminder,healthy_rid).status=='succeeded'
    if not broken_logger:
        assert b['binding_id'] in caplog.text and b['conversation_id'] in caplog.text
        assert 'hook_business_clock_repair' in caplog.text and 'location=' in caplog.text
        assert 'PRIVATE_SYNTHETIC_PAYLOAD' not in caplog.text and 'SELECT' not in caplog.text
    assert reminders.recover_reminders_once(adapter=adapter)['failed']==0
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding,b['binding_id']).native_context['business_clock_version']==1
        assert db.get(HumanReplyReminder,rid).status=='unknown'
    assert len(adapter.calls)==1
    with SessionLocal() as db:assert db.get(HumanReplyReminder,healthy_rid).status=='succeeded'


def revoke(value, targets):
    result=deepcopy(value);result['schema_version']='hook-history-coverage-v2'
    for scan in result['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        for target in targets:
            last=rows[-1]
            rows.append({**last,'localId':str(int(last['localId'])+1),'MsgSvrID':'0','Type':'10002',
                'Sequence':str(int(last['Sequence'])+1),'MsgSequence':str(int(last['MsgSequence'])+1),
                'StrContent':f'<sysmsg type="revokemsg"><revokemsg><newmsgid>{target}</newmsgid></revokemsg></sysmsg>'})
        d['upper_local_id']=rows[-1]['localId'];d['row_count']=d['count_after']=str(len(rows))
    return result


@pytest.mark.parametrize('target_kind',['old','current','mixed'])
@pytest.mark.parametrize('stage',['collecting','generating','queued'])
def test_revoke_uses_same_archive_boundary_as_new_reply(monkeypatch,target_kind,stage):
    w,t,p,b=legacy_binding();value=coverage(b)
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    review=client.get(review_url(b)).json()['data'][0]
    assert client.post(review_url(b)+'/'+review['id']+'/resolve',json={'revision':review['revision'],'note':'原事实已核对'}).status_code==200
    newer=add_message(value)
    r,_=upload(w,p,b,newer,monkeypatch);batch_id=r.json()['data']['message_batch']['batch_id']
    old=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    current=newer['scans'][0]['databases'][0]['pages'][0][-1]['MsgSvrID']
    changed=revoke(newer,[old] if target_kind=='old' else [current] if target_kind=='current' else [old,current])
    def deliver():
        r,_=upload(w,p,b,changed,monkeypatch);assert r.status_code==200,r.text
        return r
    if stage=='generating':
        original=c3_service.get_ai_engine_adapter()
        class Delayed:
            def generate_reply_decision(self,**kwargs):
                deliver()
                return original.generate_reply_decision(**kwargs)
        monkeypatch.setattr(c3_service,'get_ai_engine_adapter',lambda:Delayed())
    if stage in {'generating','queued'}:
        with SessionLocal() as db:
            generated=c3_service.generate_for_batch(db,batch_id=batch_id,native_client_admitted=True);db.commit()
        if stage=='queued':assert generated['decision']=='send_reply',generated
    if stage!='generating':deliver()
    updated=client.get(review_url(b)).json()['data'][0]
    if target_kind in {'old','mixed'}:
        assert updated['status']=='pending' and updated['revision']>review['revision']
        assert updated['messages'][0]['withdrawn'] and updated['messages'][0]['content']==review['messages'][0]['content']
        assert client.post(review_url(b)+'/'+review['id']+'/resolve',json={'revision':review['revision'],'note':'旧页面'}).status_code==409
    else:assert updated['status']=='processed' and updated['revision']==review['revision']
    deliver()
    assert client.get(review_url(b)).json()['data'][0]['revision']==updated['revision']
    with SessionLocal() as db:
        batch=db.get(MessageBatch,batch_id)
        if target_kind=='old':
            assert batch.status!='cancelled'
            assert db.get(Conversation,b['conversation_id']).reply_ownership['owner']=='ai'
            if stage!='collecting':assert db.get(ReplyAction,generated['reply_action_id']).status=='queued'
        else:assert batch.status=='cancelled'


@pytest.mark.parametrize('target_kind',['old','current','mixed'])
def test_inflight_revoke_preserves_original_attempt_and_unknown_settlement(monkeypatch,target_kind):
    from app.services import hook_send_service
    from app.models.c3 import SentAck
    w,t,p,b=legacy_binding();value=coverage(b)
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    newer=add_message(value);r,_=upload(w,p,b,newer,monkeypatch)
    digest=r.json()['data']['history_state_digest'];batch_id=r.json()['data']['message_batch']['batch_id']
    with SessionLocal() as db:
        generated=c3_service.generate_for_batch(db,batch_id=batch_id,native_client_admitted=True);db.commit()
    assert generated['decision']=='send_reply',generated
    task=generated['task_id'];aid=generated['reply_action_id']
    assert post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':task,'flow_kind':'chat_reply','conversation_id':b['conversation_id']}).status_code==200
    h={'X-Inflight-Flow-Id':task}
    r=post(w,f'/tasks/{task}/claim',{'worker_id':w['id'],'claim_source':'hook_conversation_flow','conversation_id':b['conversation_id']},h);assert r.status_code==200,r.text
    h['X-Task-Lease-Fencing-Token']=str(r.json()['data']['lease_fencing_token'])
    path=f"/workers/{w['id']}/wechat/native-replies/{aid}"
    r=post(w,path+'/claim',{**p,'task_id':task,'history_state_digest':digest,'clock_protocol_version':1},h);assert r.status_code==200,r.text
    permit=r.json()['data'];since=int(b['history_since_create_time'])
    now=lambda sec:datetime.fromtimestamp(since+sec,timezone.utc).isoformat()
    monkeypatch.setattr(hook_send_service,'utcnow',lambda:datetime.fromtimestamp(since+60,timezone.utc))
    start={**p,'task_id':task,'history_state_digest':digest,'clock_protocol_version':1,'send_token':permit['send_token'],'journal_sha256':'c'*64,'attempted_at':now(40)}
    assert post(w,path+'/begin',start,h).status_code==200
    with SessionLocal() as db:attempt=deepcopy(db.get(ReplyAction,aid).ai_payload['native_send_attempt'])
    old=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    current=newer['scans'][0]['databases'][0]['pages'][0][-1]['MsgSvrID']
    changed=revoke(newer,[old] if target_kind=='old' else [current] if target_kind=='current' else [old,current])
    r,_=upload(w,p,b,changed,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        assert db.get(ReplyAction,aid).ai_payload['native_send_attempt']==attempt
        assert db.get(ReplyAction,aid).status=='sending'
        # An already invoked single-segment action is retained for settlement,
        # rather than cancelled as though no physical call had occurred.
        assert db.get(MessageBatch,batch_id).status=='reply_action_created'
        current_digest=db.get(WechatSessionBinding,b['binding_id']).native_context['history_state_digest']
        assert (current_digest==digest)==(target_kind=='old')
    settle={**p,'task_id':task,'send_token':permit['send_token'],'journal_sha256':start['journal_sha256'],
        'completed_at':now(41),'local_call_window':{'source':'physical_call_v1','started_at':now(40),'completed_at':now(41),'elapsed_seconds':1.0},'call_completed':True}
    r=post(w,path+'/settle',settle,h);assert r.status_code==200,r.text
    assert not r.json()['data']['automatic_resend_allowed']
    with SessionLocal() as db:
        assert db.get(ReplyAction,aid).status=='unknown_send_result'
        assert db.get(ReplyAction,aid).ai_payload['native_send_attempt']==attempt
        assert db.scalar(select(SentAck).where(SentAck.reply_action_id==aid)).send_result=='unknown'
    assert post(w,path+'/settle',settle,h).json()['data']==r.json()['data']


def test_old_saved_system_notice_updates_review_without_invalidating_new_reply(monkeypatch):
    from test_hook_welcome_notice import NOTICE
    w,t,p,b=legacy_binding();value=coverage(b)
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    review=client.get(review_url(b)).json()['data'][0]
    # Model a previous release's already archived acceptance notice. The new
    # parser classifies its original content as a system fact, not customer text.
    with SessionLocal() as db:
        db.get(MessageEvent,review['messages'][0]['id']).content=NOTICE;db.commit()
    for scan in value['scans']:scan['databases'][0]['pages'][0][0]['StrContent']=NOTICE
    assert client.post(review_url(b)+'/'+review['id']+'/resolve',json={'revision':review['revision'],'note':'旧版存量核对'}).status_code==200
    newer=add_message(value)
    r,_=upload(w,p,b,newer,monkeypatch);assert r.status_code==200,r.text
    batch_id=r.json()['data']['message_batch']['batch_id']
    updated=client.get(review_url(b)).json()['data'][0]
    assert updated['status']=='pending' and updated['messages'][0]['system_notice']
    assert not updated['messages'][0]['withdrawn']
    assert updated['messages'][0]['content']==NOTICE
    r,_=upload(w,p,b,newer,monkeypatch)
    assert r.json()['data']['message_batch']['batch_id']==batch_id
    assert client.get(review_url(b)).json()['data'][0]['revision']==updated['revision']
