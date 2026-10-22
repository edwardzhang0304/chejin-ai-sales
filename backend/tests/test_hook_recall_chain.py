"""Recall contracts through real APIs; external AI/WeChat are simulated."""
import importlib
import json
import uuid
from datetime import timedelta, timezone
from pathlib import Path
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.core.config import get_settings
from app.contracts.shared_rules import shared_adapter
from app.models.c3 import Conversation, MessageBatch, HandoffEvent, ReplyAction
from app.services import hook_history_service, wechat_service, c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_hook_business_http import clean, post
from test_hook_history_business_http import bound, coverage, upload


def recall_input(monkeypatch, case='due', origin='sales_replied_waiting_user'):
    w,t,p,b=bound(); value=coverage(b)
    for scan in value['scans']:
        scan['databases'][0]['pages'][0][1]['IsSender']='1'
    response,_=upload(w,p,b,value,monkeypatch)
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id'])
        assert conv.status=='sales_replied_waiting_user'
        due=conv.next_recall_at.replace(tzinfo=timezone.utc)
        self_at=int(value['scans'][0]['databases'][0]['pages'][0][1]['CreateTime'])
        assert int(due.timestamp())==self_at+get_settings().c3_recall_after_hours*3600
        # Next UTC 02:00 (Shanghai 10:00), safely after the real scheduled due time.
        future=(due+timedelta(days=1)).replace(hour=2,minute=0,second=0,microsecond=0)
        if case=='not_due':future=due-timedelta(seconds=1)
        if case=='quiet':future=future.replace(hour=14) # Shanghai 22:00
        if case=='total_limit':conv.reply_ownership={**conv.reply_ownership,'recall_streak':get_settings().c3_recall_max_cycles}
        if case=='daily_limit':
            conv.recall_daily_date=(future+timedelta(hours=8)).date()
            conv.recall_daily_count=get_settings().c3_recall_daily_limit
        if case=='disabled':conv.ai_enabled=False
        if case=='handoff':
            db.add(HandoffEvent(conversation_id=conv.conversation_id,handoff_reason_code='CUSTOMER_REQUEST_HUMAN'))
        # State matrix isolates existing eligible origins; the Worker runtime test
        # separately produces waiting_user_reply through an actual welcome receipt.
        conv.status=origin
        db.commit()
    monkeypatch.setattr(wechat_service,'utcnow',lambda:future)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:future)
    for scan in value['scans']:scan['observed_at']=future.isoformat()
    body={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
          'capture_id':str(uuid.uuid4()),'coverage':value,
          'coverage_digest':shared_adapter('hook_contract_rules').digest(value)}
    response=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body)
    assert response.status_code==200,response.text
    return w,p,b,value,body,response.json()['data']


@pytest.mark.parametrize('case',['due','not_due','quiet','total_limit','daily_limit','disabled','handoff'])
def test_real_history_recall_trigger_gates(monkeypatch,case):
    w,p,b,value,body,result=recall_input(monkeypatch,case)
    with SessionLocal() as db:
        batches=list(db.scalars(select(MessageBatch).where(MessageBatch.trigger_type=='recall')))
        assert len(batches)==(1 if case=='due' else 0)
        if case=='due':
            assert result['message_batch']['batch_id']==batches[0].id
            assert batches[0].message_event_ids==[]
            assert db.get(Conversation,b['conversation_id']).recall_count==0
    if case=='due':
        body['capture_id']=str(uuid.uuid4())
        repeat=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body)
        assert repeat.status_code==200,repeat.text
        with SessionLocal() as db:
            assert len(list(db.scalars(select(MessageBatch).where(MessageBatch.trigger_type=='recall'))))==1


def test_real_recall_batch_preserves_intent_through_brain_and_reviewer(monkeypatch):
    w,p,b,value,body,result=recall_input(monkeypatch)
    adapter=RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa'))
    monkeypatch.setattr(adapter,'_load_config',lambda:{})
    monkeypatch.setattr(adapter,'_require_api_key',lambda config:None)
    captured={}
    def external_provider_boundary(*,config,invocation,timeout_seconds):
        brain=importlib.import_module('apps.wechat_ai_customer_service.workflows.customer_service_brain')
        reviewer=importlib.import_module('customer_service_quality_reviewer')
        # Real production input projection. Empty evidence cannot alter trigger dispatch.
        projected=brain.build_brain_input(settings={},target_name=invocation['target_name'],
            target_state=invocation['target_state'],batch=invocation['batch'],combined=invocation['combined'],
            raw_capture=invocation['raw_capture'],evidence_pack={})
        author=brain.compact_current_message_for_prompt(projected['current_message'])
        review=reviewer.build_quality_review_request(settings={},brain_input=projected,evidence_pack={},
            plan={},deterministic_quality={})
        captured.update(backend_trigger=invocation['target_state']['trigger_type'],
            recall_cycle_present=bool(invocation['target_state']['recall_cycle_id']),
            customer_message_count=len(invocation['batch']),
            brain_trigger=projected['current_message']['trigger_type'],
            author_trigger=author['trigger_type'],reviewer_trigger=review['reply_trigger']['trigger_type'])
        return {'adoptable':False,'brain_plan':{}}
    monkeypatch.setattr(adapter,'_run_brain_isolated',external_provider_boundary)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',lambda:adapter)
    with SessionLocal() as db:
        c3_service.generate_for_batch(db,batch_id=result['message_batch']['batch_id'],native_client_admitted=True)
        db.commit()
    assert captured.get('backend_trigger')=='recall',captured
    assert captured['brain_trigger']==captured['author_trigger']==captured['reviewer_trigger']=='recall',captured


@pytest.mark.parametrize('legacy_batch',[False,True])
@pytest.mark.parametrize('origin',['waiting_user_reply','sales_replied_waiting_user','recalled_waiting_user'])
def test_native_recall_no_action_restores_real_origin(monkeypatch, origin, legacy_batch):
    w,p,b,value,body,result=recall_input(monkeypatch,origin=origin)
    class NoAction:
        def generate_reply_decision(self,**kwargs):
            return c3_service.AIEngineDecision(decision='no_action',guard_result='pass',suggested_action='no_action')
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',lambda:NoAction())
    with SessionLocal() as db:
        before=db.get(Conversation,b['conversation_id']).recall_origin_status
        batch=db.get(MessageBatch,result['message_batch']['batch_id'])
        saved_origin=batch.origin_conversation_status
        if legacy_batch:batch.origin_conversation_status=None
        outcome=c3_service.generate_for_batch(db,batch_id=batch.id,native_client_admitted=True)
        db.commit()
        conv=db.get(Conversation,b['conversation_id'])
        actual={'before':before,'batch_origin':saved_origin,'decision':outcome['decision'],
                'after':conv.status,'recall_count':conv.recall_count}
    assert actual['decision']=='no_action',actual
    assert actual['after']==actual['before']==origin,actual
    assert actual['batch_origin']==origin and actual['recall_count']==0
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id'])
        assert conv.recall_cycle_id is None and conv.recall_origin_status is None
        assert conv.next_recall_at is not None
        assert not list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id==result['message_batch']['batch_id'])))


@pytest.mark.parametrize('sender',['customer','self'])
def test_new_message_cancels_old_recall(monkeypatch,sender):
    w,p,b,value,body,result=recall_input(monkeypatch)
    batch_id=result['message_batch']['batch_id']
    with SessionLocal() as db:
        generated=c3_service.generate_for_batch(db,batch_id=batch_id,native_client_admitted=True)
        db.commit()
        assert generated['decision']=='send_reply',generated
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        rows.append({**rows[-1],'localId':'7803','Sequence':str(int(rows[-1]['Sequence'])+1),
            'MsgSequence':'780013','MsgSvrID':'8000000000000000003',
            'IsSender':'1' if sender=='self' else '0','StrContent':'我来继续确认购车需求'})
        d.update(upper_local_id='7803',row_count='3',count_after='3')
    body.update(capture_id=str(uuid.uuid4()),coverage_digest=shared_adapter('hook_contract_rules').digest(value))
    response=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body)
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        old=db.get(MessageBatch,batch_id)
        action=db.get(ReplyAction,generated['reply_action_id'])
        assert not old.active and action.status=='superseded'
        assert db.get(Conversation,b['conversation_id']).recall_count==0
