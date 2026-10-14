"""Typed quote/revoke and original C4 precheck over actual native receipts."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import pytest,uuid
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.wechat import MessageEvent,WechatSessionBinding
from app.models.c3 import Conversation,MessageBatch,ReplyAction
from app.services import hook_history_service,wechat_service
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound,coverage,upload


def test_quote_only_new_title_is_customer_input_and_reference_is_retained(monkeypatch):
    w,t,p,b=bound();value=coverage(b);mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    for scan in value['scans']:
        row=scan['databases'][0]['pages'][0][1];row['Type']='49'
        row['StrContent']=f'<msg><appmsg><type>57</type><title>现在改成18万元，但不要报价🙂</title><refermsg><svrid>{mid}</svrid><content>旧正文不能当成新话</content></refermsg></appmsg></msg>'
    value['schema_version']='hook-history-coverage-v2'
    result,_=upload(w,p,b,value,monkeypatch);assert result.status_code==200,result.text
    assert result.json()['data']['history_ready']
    with SessionLocal() as db:
        quote=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='text',MessageEvent.content.like('现在改成%')))
        assert quote and quote.content=='现在改成18万元，但不要报价🙂'
        assert quote.raw_payload['native_quote']['referenced_native_msg_id']==mid
        assert all('旧正文不能当成新话' not in (m.content or '') for m in db.scalars(select(MessageEvent)))


def test_revoke_removes_original_from_context_without_destroying_evidence(monkeypatch):
    w,t,p,b=bound();value=coverage(b);initial,_=upload(w,p,b,value,monkeypatch)
    target=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    old_batch=initial.json()['data']['message_batch']['batch_id']
    value['schema_version']='hook-history-coverage-v2'
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0]
        rows.append({**rows[-1],'localId':'7803','MsgSvrID':'0','Type':'10002','Sequence':str(int(rows[-1]['Sequence'])+1),
            'MsgSequence':str(int(rows[-1]['MsgSequence'])+1),'StrContent':f'<sysmsg type="revokemsg"><revokemsg><newmsgid>{target}</newmsgid></revokemsg></sysmsg>'})
        d['upper_local_id']='7803';d['row_count']=d['count_after']='3'
    latest,_=upload(w,p,b,value,monkeypatch);assert latest.status_code==200,latest.text
    assert latest.json()['data']['history_ready']
    with SessionLocal() as db:
        original=db.scalar(select(MessageEvent).where(MessageEvent.content=='同秒先发🙂'))
        if original is None:original=list(db.scalars(select(MessageEvent)))[0]
        assert original.item_state=='withdrawn' and original.content
        assert original.id not in latest.json()['data']['message_event_ids']
        assert db.get(MessageBatch,old_batch).status=='cancelled'


def test_native_recall_uses_original_due_intent_and_fresh_history_precheck(monkeypatch):
    w,t,p,b=bound();value=coverage(b)
    for scan in value['scans']:
        scan['databases'][0]['pages'][0][1]['IsSender']='1'
    first,_=upload(w,p,b,value,monkeypatch);assert first.status_code==200
    future=datetime(2026,10,20,2,0,tzinfo=timezone.utc) # 10am Shanghai, outside original quiet time.
    monkeypatch.setattr(wechat_service,'utcnow',lambda:future)
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:future)
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id']);conv.status='waiting_user_reply';conv.next_recall_at=future-timedelta(seconds=1)
        conv.recall_count=0;conv.recall_daily_count=0;conv.recall_cycle_id=None;db.commit()
    # Upload helper changes its clock to invitation time; use the actual endpoint here.
    for scan in value['scans']:scan['observed_at']=future.isoformat()
    from app.contracts.shared_rules import shared_adapter
    body={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],'capture_id':str(uuid.uuid4()),
        'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value)}
    latest=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body)
    assert latest.status_code==200,latest.text
    result=latest.json()['data'];assert result['history_ready'] and result['message_batch']
    with SessionLocal() as db:
        batch=db.get(MessageBatch,result['message_batch']['batch_id']);assert batch.trigger_type=='recall'
        binding=db.get(WechatSessionBinding,b['binding_id']);assert binding.native_context['native_recall_precheck']['cycle_id']==batch.recall_cycle_id
        conv=db.get(Conversation,b['conversation_id']);assert conv.recall_count==0 and conv.recall_daily_count==0
