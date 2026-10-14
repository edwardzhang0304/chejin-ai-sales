"""Native sends are simulated; formal task/batch/permit/history APIs are real."""
from datetime import datetime, timezone
import uuid
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import ReplyAction, SentAck
from app.models.task import Task
from app.models.wechat import MessageEvent
from app.services import c3_service, hook_send_service, hook_history_service
from app.contracts.shared_rules import shared_adapter
from test_hook_business_http import clean, post
from test_hook_history_business_http import bound, coverage, upload


def permitted(monkeypatch):
    w,t,p,b=bound();value=coverage(b);r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    result=r.json()['data'];b['history_state_digest']=result['history_state_digest']
    with SessionLocal() as db:
        generated=c3_service.generate_for_batch(db,batch_id=result['message_batch']['batch_id'],native_client_admitted=True);db.commit()
    assert generated['decision']=='send_reply',generated
    task_id=generated['task_id'];action_id=generated['reply_action_id']
    assert post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':task_id,'flow_kind':'chat_reply','conversation_id':b['conversation_id']}).status_code==200
    h={'X-Inflight-Flow-Id':task_id}
    r=post(w,f'/tasks/{task_id}/claim',{'worker_id':w['id'],'claim_source':'hook_conversation_flow','conversation_id':b['conversation_id']},h)
    assert r.status_code==200,r.text
    h['X-Task-Lease-Fencing-Token']=str(r.json()['data']['lease_fencing_token'])
    claim={**p,'task_id':task_id,'history_state_digest':b['history_state_digest']}
    r=post(w,f"/workers/{w['id']}/wechat/native-replies/{action_id}/claim",claim,h);assert r.status_code==200,r.text
    permit=r.json()['data'];assert permit['native_send_ready']
    return w,p,b,h,permit,value


def beginning(monkeypatch):
    w,p,b,h,permit,value=permitted(monkeypatch)
    since=int(b['history_since_create_time']);now=lambda sec:datetime.fromtimestamp(since+sec,timezone.utc).isoformat()
    monkeypatch.setattr(hook_send_service,'utcnow',lambda:datetime.fromtimestamp(since+60,timezone.utc))
    start={**p,'task_id':permit['task_id'],'history_state_digest':b['history_state_digest'],
        'send_token':permit['send_token'],'journal_sha256':'c'*64,'attempted_at':now(40)}
    path=f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}"
    r=post(w,path+'/begin',start,h);assert r.status_code==200,r.text
    assert r.json()['data']['may_call_hook']
    again=post(w,path+'/begin',start,h);assert again.status_code==200 and not again.json()['data']['may_call_hook']
    return w,p,b,h,permit,value,path,now,start


@pytest.mark.parametrize('kind',['unique','missing','ambiguous','timeout_but_observed'])
def test_one_native_history_match_settles_original_reply_without_resend(kind,monkeypatch):
    w,p,b,h,permit,value,path,now,start=beginning(monkeypatch)
    since=int(b['history_since_create_time'])
    count=0 if kind=='missing' else 2 if kind=='ambiguous' else 1
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        for i in range(count):
            rows.append({**rows[0],'localId':str(7803+i),'CreateTime':str(since+40),'Sequence':str((since+40)*1000+i),
                'MsgSequence':str(780013+i),'MsgSvrID':str(8000000000000000003+i),'IsSender':'1','StrContent':permit['reply_text']})
        d['row_count']=d['count_after']=str(len(rows));d['upper_local_id']=rows[-1]['localId'];scan['observed_at']=now(50)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromtimestamp(since+60,timezone.utc))
    body={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],'capture_id':str(uuid.uuid4()),
          'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value)}
    observed=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body);assert observed.status_code==200,observed.text
    payload={**p,'task_id':permit['task_id'],'send_token':permit['send_token'],'journal_sha256':start['journal_sha256'],
        'completed_at':now(41),'call_completed':kind!='timeout_but_observed'}
    result=post(w,path+'/settle',payload,h);assert result.status_code==200,result.text
    confirmed=count==1
    data=result.json()['data'];assert data['local_send_observed']==confirmed and not data['automatic_resend_allowed']
    assert not data['recipient_delivery_confirmed']
    assert post(w,path+'/settle',payload,h).json()['data']==data
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id']);task=db.get(Task,permit['task_id'])
        assert action.status==('sent' if confirmed else 'unknown_send_result')
        assert task.status==('completed' if confirmed else 'failed')
        ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==action.id));assert ack
        if confirmed:
            message=db.scalar(select(MessageEvent).where(MessageEvent.sender_role=='self'))
            assert message.raw_payload['sender_source']=='ai'
            assert message.raw_payload['ai_reply_action_id']==action.id
    assert post(w,path+'/claim',{**p,'task_id':permit['task_id'],'history_state_digest':b['history_state_digest']},h).status_code==409


@pytest.mark.parametrize('wrong',['token','generation','digest','clock'])
def test_native_begin_denies_changed_authority_before_hook_call(wrong,monkeypatch):
    w,p,b,h,permit,value=permitted(monkeypatch);since=int(b['history_since_create_time'])
    monkeypatch.setattr(hook_send_service,'utcnow',lambda:datetime.fromtimestamp(since+60,timezone.utc))
    request={**p,'task_id':permit['task_id'],'history_state_digest':b['history_state_digest'],
        'send_token':permit['send_token'],'journal_sha256':'d'*64,'attempted_at':datetime.fromtimestamp(since+40,timezone.utc).isoformat()}
    if wrong=='token':request['send_token']='wrong-token'
    if wrong=='generation':request['process_generation']='process-2'
    if wrong=='digest':request['history_state_digest']='e'*64
    if wrong=='clock':request['attempted_at']='2000-01-01T00:00:00+00:00'
    r=post(w,f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}/begin",request,h)
    assert r.status_code==409,r.text
    with SessionLocal() as db:assert not db.get(ReplyAction,permit['reply_action_id']).ai_payload.get('native_send_attempt')


def test_settlement_cannot_switch_the_registered_flow(monkeypatch):
    w,p,b,h,permit,value,path,now,start=beginning(monkeypatch)
    headers={**h,'X-Inflight-Flow-Id':str(uuid.uuid4())}
    payload={**p,'task_id':permit['task_id'],'send_token':permit['send_token'],
        'journal_sha256':start['journal_sha256'],'completed_at':now(41),'call_completed':True}
    r=post(w,path+'/settle',payload,headers)
    assert r.status_code==409,r.text
    with SessionLocal() as db:
        assert db.get(ReplyAction,permit['reply_action_id']).status=='sending'
        assert db.scalar(select(SentAck)) is None


def test_late_unique_history_classifies_actor_without_overwriting_unknown_ack(monkeypatch):
    from copy import deepcopy
    from app.models.c3 import SentAck
    w,p,b,h,permit,value,path,now,start=beginning(monkeypatch)
    since=int(b['history_since_create_time'])
    body={**p,'task_id':permit['task_id'],'send_token':permit['send_token'],'journal_sha256':start['journal_sha256'],
        'completed_at':now(41),'call_completed':True}
    old=post(w,path+'/settle',body,h);assert old.status_code==200 and not old.json()['data']['local_send_observed']
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id']);immutable=deepcopy(action.ai_payload['native_send_settlement'])
        ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==action.id));original_ack=ack.send_result
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        rows.append({**rows[0],'localId':'7803','MsgSvrID':'8000000000000000003','IsSender':'1',
            'CreateTime':str(since+40),'Sequence':str((since+40)*1000),'MsgSequence':'780013','StrContent':permit['reply_text']})
        d['row_count']=d['count_after']='3';d['upper_local_id']='7803';scan['observed_at']=now(50)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromtimestamp(since+60,timezone.utc))
    coverage_body={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
        'capture_id':str(uuid.uuid4()),'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value)}
    observed=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",coverage_body)
    assert observed.status_code==200,observed.text
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id']);assert action.status=='unknown_send_result'
        assert action.ai_payload['native_send_settlement']==immutable
        assert action.ai_payload['native_actor_resolution']['native_msg_id']=='8000000000000000003'
        assert db.scalar(select(SentAck).where(SentAck.reply_action_id==action.id)).send_result==original_ack
        message=db.scalar(select(MessageEvent).where(MessageEvent.sender_role=='self'))
        assert message.raw_payload['sender_source']=='ai'


def test_native_context_change_before_any_trigger_cancels_without_handoff(monkeypatch):
    from app.models.c3 import HandoffEvent
    w,p,b,h,permit,value=permitted(monkeypatch)
    proof={'reply_action_id':permit['reply_action_id'],'task_id':permit['task_id'],'phase':'not_triggered',
        'reason_code':'HOOK_SEND_LATEST_CONTEXT_STALE','intent_sha256':None,'observed_at':datetime.now(timezone.utc).isoformat()}
    body={**p,'task_id':permit['task_id'],'send_token':permit['send_token'],'proof':proof,'proof_digest':shared_adapter('hook_contract_rules').digest(proof)}
    path=f"/workers/{w['id']}/wechat/native-replies/{permit['reply_action_id']}/not-triggered"
    result=post(w,path,body,h);assert result.status_code==200,result.text
    assert not result.json()['data']['physical_send_attempted']
    with SessionLocal() as db:
        action=db.get(ReplyAction,permit['reply_action_id']);assert action.status=='superseded'
        assert db.scalar(select(HandoffEvent)) is None
        assert db.scalar(select(SentAck).where(SentAck.reply_action_id==action.id)).action_phase=='not_attempted'
    assert post(w,path,body,h).json()['data']==result.json()['data']
