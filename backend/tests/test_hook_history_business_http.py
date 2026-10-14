"""Formal HTTP origin -> native coverage -> original C3, simulated clock/Hook data."""
from copy import deepcopy
from datetime import datetime, timezone
import uuid
import pytest
from sqlalchemy import select, func
from app.core.database import SessionLocal
from app.models.hook_native import HookHistoryReceipt
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.c3 import MessageBatch, ReplyAction
from app.contracts.shared_rules import shared_adapter
from test_hook_business_http import clean, submitted, body, post


def bound():
    w,t,p,h,prep=submitted()
    r=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(t,p,prep));assert r.status_code==200,r.text
    return w,t,p,r.json()['data']


def coverage(binding,changes=None):
    since=int(binding['history_since_create_time'])
    def row(i,text):
        return {'localId':str(7800+i),'CreateTime':str(since+10),'Sequence':str((since+10)*1000+i),
            'MsgServerSeq':'1','MsgSequence':str(780010+i),'MsgSvrID':str(8000000000000000000+i),
            'Type':'1','IsSender':'0','StrTalker':binding['peer_wxid'],'StrContent':text}
    rows=[row(1,'先看宝马'),row(2,'预算15万元')]
    databases=[{'name':'MSG0.db','generation':'a'*64,'upper_local_id':'7802','row_count':'2','count_after':'2','pages':[rows]}]
    digest=shared_adapter('hook_contract_rules').digest([[d['name'],d['generation']] for d in databases])
    observed=datetime.fromtimestamp(since+20,timezone.utc).isoformat()
    scan={'catalog_fingerprint':digest,'databases':databases,'observed_at':observed}
    value={'schema_version':'hook-history-coverage-v1','since_create_time':str(since),'scans':[scan,deepcopy(scan)],'local_pending_count':0}
    if changes:changes(value)
    return value


def upload(w,p,b,value,monkeypatch,capture_id=None):
    from app.services import hook_history_service
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromtimestamp(int(b['history_since_create_time'])+30,timezone.utc))
    data={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],'capture_id':capture_id or str(uuid.uuid4()),
          'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value)}
    return post(w,f"/workers/{w['id']}/wechat/native-history/coverage",data),data


def test_native_text_coverage_builds_original_brain_context_without_ocr(monkeypatch):
    from app.services import c3_service
    w,t,p,b=bound();value=coverage(b);r,data=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    result=r.json()['data'];assert result['history_ready'] and result['message_batch'] and not result['native_send_ready']
    assert post(w,f"/workers/{w['id']}/wechat/native-history/coverage",data).json()['data']==result
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);batch=db.get(MessageBatch,result['message_batch']['batch_id'])
        context=c3_service._build_ai_context(db,binding,c3_service._conversation_for_binding(db,binding),batch)
        assert [m['content'] for m in context['messages']]==['先看宝马','预算15万元']
        assert context['native_sync_checkpoint']['history_state_digest']==result['history_state_digest']
        assert 'pre_send_fact_checkpoint' not in context
        assert context['brain_context_snapshot']['history_event_count_before_batch']==0
        assert db.scalar(select(func.count()).select_from(HookHistoryReceipt))==1
        generated=c3_service.generate_for_batch(db,batch_id=batch.id,native_client_admitted=True)
        assert generated['decision']=='send_reply',generated
        action=db.get(ReplyAction,generated['reply_action_id']);assert action.reply_text
        with pytest.raises(Exception) as failure:
            c3_service._ensure_reply_action_send_eligible(db,binding=binding,conversation=c3_service._conversation_for_binding(db,binding),action=action)
        assert getattr(failure.value,'code',None)=='HOOK_NATIVE_SEND_PERMIT_REQUIRED'
        db.commit()


@pytest.mark.parametrize('case',['count','gap','changed','since','peer','native_id','missing_sequence','float_id','pending','catalog'])
def test_incomplete_or_wrong_scopes_never_start_ai(case,monkeypatch):
    w,t,p,b=bound();value=coverage(b)
    for scan in value['scans']:
        d=scan['databases'][0];row=d['pages'][0][0]
        if case=='count':d['count_after']='1'
        if case=='gap':d['pages'][0].pop()
        if case=='peer':row['StrTalker']='wxid_other_peer'
        if case=='native_id':row['MsgSvrID']='0'
        if case=='missing_sequence':row.pop('MsgSequence')
        if case=='float_id':row['MsgSvrID']=8.0e18
        if case=='catalog':scan['catalog_fingerprint']='b'*64
    if case=='changed':value['scans'][1]['databases'][0]['pages'][0][0]['StrContent']='changed'
    if case=='since':value['since_create_time']='0'
    if case=='pending':value['local_pending_count']=1
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==409,r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        assert db.scalar(select(func.count()).select_from(HookHistoryReceipt))==0
        assert db.scalar(select(func.count()).select_from(MessageEvent))==0


@pytest.mark.parametrize('case',['media','tie','cross_database','boundary'])
def test_saved_but_unverified_native_history_is_a_hold(case,monkeypatch):
    w,t,p,b=bound();value=coverage(b)
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        if case=='media':rows[0]['Type']='34';rows[0]['StrContent']='<msg>voice metadata</msg>'
        if case=='tie':rows[1]['MsgSequence']=rows[0]['MsgSequence']
        if case=='boundary':rows[0]['CreateTime']=b['history_since_create_time']
        if case=='cross_database':
            d['pages']=[[rows[0]]];d['row_count']=d['count_after']='1';d['upper_local_id']=rows[0]['localId']
            other=deepcopy(d);other.update(name='MSG1.db',generation='b'*64,upper_local_id=rows[1]['localId'],pages=[[rows[1]]])
            scan['databases'].append(other)
            scan['catalog_fingerprint']=shared_adapter('hook_contract_rules').digest([[x['name'],x['generation']] for x in scan['databases']])
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    assert not r.json()['data']['history_ready'] and r.json()['data']['message_batch'] is None
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(MessageBatch))==0


def test_repeat_history_new_capture_dedupes_facts_and_batch(monkeypatch):
    w,t,p,b=bound();value=coverage(b)
    a,_=upload(w,p,b,value,monkeypatch);z,_=upload(w,p,b,value,monkeypatch)
    assert a.status_code==z.status_code==200
    assert a.json()['data']['message_event_ids']==z.json()['data']['message_event_ids']
    assert a.json()['data']['message_batch']['batch_id']==z.json()['data']['message_batch']['batch_id']
    assert z.json()['data']['new_message_event_ids']==[]


def test_native_human_reply_uses_history_order_to_close_original_handoff(monkeypatch):
    from app.services import c3_service
    from app.models.c3 import Conversation,HandoffEvent
    w,t,p,b=bound();value=coverage(b)
    for scan in value['scans']:scan['databases'][0]['pages'][0][1]['StrContent']='请人工销售回复'
    r,_=upload(w,p,b,value,monkeypatch)
    with SessionLocal() as db:
        generated=c3_service.generate_for_batch(db,batch_id=r.json()['data']['message_batch']['batch_id'],native_client_admitted=True);db.commit()
        assert generated['decision']=='handoff'
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        rows.append({**rows[-1],'localId':'7803','Sequence':str(int(rows[-1]['Sequence'])+1),'MsgSequence':'780013',
            'MsgSvrID':'8000000000000000003','IsSender':'1','StrContent':'我是销售，现在由我来协助您'})
        d['upper_local_id']='7803';d['row_count']=d['count_after']='3'
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id']);assert conv.status=='sales_replied_waiting_user'
        handoff=db.scalar(select(HandoffEvent));assert handoff.status=='sales_replied'
        assert 'sales_reply_order_proof:native_history_sequence' in handoff.evidence_refs
        outgoing=db.scalar(select(MessageEvent).where(MessageEvent.sender_role=='self'));assert outgoing.raw_payload['sender_source']=='human'
        assert outgoing.lead_id and outgoing.sales_id


def test_raw_intake_invalidates_ready_history_before_dispatch(monkeypatch):
    from test_hook_business_http import native_payload
    w,t,p,b=bound();r,_=upload(w,p,b,coverage(b),monkeypatch)
    assert r.json()['data']['history_ready']
    incoming=native_payload(w,b,mid='8000000000000000009',text='新收到的客户问题')
    r=post(w,f"/workers/{w['id']}/wechat/messages/native-observations",incoming)
    assert r.status_code==200,r.text
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id'])
        assert not binding.native_context['history_ready']
        assert binding.native_context['history_reason_code']=='HOOK_HISTORY_NEW_FACTS_PENDING'
        assert db.scalar(select(MessageEvent).where(MessageEvent.content=='新收到的客户问题')) is None
