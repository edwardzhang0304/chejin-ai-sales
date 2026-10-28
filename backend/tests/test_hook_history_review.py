"""HTTP review lifecycle and native boundaries; all data is synthetic."""
from copy import deepcopy
from datetime import datetime, timezone
from sqlalchemy import select, func
import pytest
from app.core.database import SessionLocal
from app.models.c3 import Conversation, HandoffEvent, MessageBatch
from app.models.history_review import HistoryReview
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.models.task import TaskEvidence
from app.contracts.shared_rules import shared_adapter
from test_hook_business_http import clean, client, post, submitted, body
from test_hook_history_business_http import bound, coverage, upload


def legacy_binding():
    w,t,p,b = bound()
    with SessionLocal() as db:
        conv = db.get(Conversation,b['conversation_id'])
        conv.reply_ownership = {'owner':'legacy','revision':0,'since':'2099-01-01T00:00:00+00:00'}
        db.commit()
    return w,t,p,b


def add_message(value, *, index=3, sequence=None, text='新的明确需求'):
    value = deepcopy(value)
    for scan in value['scans']:
        d=scan['databases'][0]; rows=d['pages'][0]
        row={**rows[0], 'localId':str(7800+index),'MsgSvrID':str(8000000000000000000+index),
            'Sequence':str(sequence or int(rows[-1]['Sequence'])+1),
            'MsgSequence':str(780010+index),'Type':'1','StrContent':text}
        rows.append(row)
        rows.sort(key=lambda r:int(r['localId']))
        d['row_count']=d['count_after']=str(len(rows)); d['upper_local_id']=max((r['localId'] for r in rows),key=int)
    return value


def review_url(b):
    return f"/api/leads/{b['lead_id']}/wechat/history-reviews"


def test_legacy_history_is_reviewed_new_message_replies_and_resolution_never_replays(monkeypatch):
    w,t,p,b=legacy_binding(); value=coverage(b)
    first,_=upload(w,p,b,value,monkeypatch);assert first.status_code==200,first.text
    assert first.json()['data']['message_batch'] is None
    reviews=client.get(review_url(b)).json()['data'];assert len(reviews)==1
    review=reviews[0];assert len(review['messages'])==2
    newer=add_message(value)
    second,_=upload(w,p,b,newer,monkeypatch);assert second.status_code==200,second.text
    batch=second.json()['data']['message_batch'];assert batch
    from app.services import c3_service
    with SessionLocal() as db:
        stored=db.get(MessageBatch,batch['batch_id'])
        assert len(stored.message_event_ids)==1
        bind=db.get(WechatSessionBinding,b['binding_id']); conv=db.get(Conversation,b['conversation_id'])
        context=c3_service._build_ai_context(db,bind,conv,stored)
        assert [m['content'] for m in context['messages']]==['新的明确需求']
        assert conv.reply_ownership['owner']=='ai'
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==0
        original={e.id:deepcopy(e.raw_payload) for e in db.scalars(select(MessageEvent))}
    url=review_url(b)+'/'+review['id']+'/resolve'
    assert client.post(url,json={'revision':review['revision'],'note':'  '}).status_code==422
    assert client.post(url,json={'revision':review['revision']-1,'note':'核对完成'}).status_code==409
    data={'revision':review['revision'],'note':'已核对原档案，无需再回复'}
    assert client.post(url,json=data).status_code==200
    assert client.post(url,json=data).status_code==200
    again,_=upload(w,p,b,newer,monkeypatch);assert again.status_code==200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HistoryReview))==1
        r=db.get(HistoryReview,review['id']);assert r.status=='processed' and len(r.resolutions)==1
        assert db.scalar(select(func.count()).select_from(MessageBatch))==1
        for mid,raw in original.items():
            event=db.get(MessageEvent,mid)
            assert event.raw_payload.get('history_review_id')==raw.get('history_review_id')


def test_late_old_row_reopens_review_without_entering_ai_and_preserves_resolution(monkeypatch):
    w,t,p,b=legacy_binding();value=coverage(b)
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    r=client.get(review_url(b)).json()['data'][0]
    assert client.post(review_url(b)+'/'+r['id']+'/resolve',json={'revision':r['revision'],'note':'已核对'}).status_code==200
    # Native order before the frozen fence, despite a larger pagination localId.
    older=add_message(value,index=0,sequence=int(value['scans'][0]['databases'][0]['pages'][0][0]['Sequence'])-1,text='迟到的旧消息')
    response,_=upload(w,p,b,older,monkeypatch);assert response.status_code==200,response.text
    assert response.json()['data']['message_batch'] is None
    with SessionLocal() as db:
        row=db.get(HistoryReview,r['id']);assert row.status=='pending' and len(row.message_ids)==3
        assert row.revision>r['revision'] and len(row.resolutions)==1
    assert client.post(review_url(b)+'/'+r['id']+'/resolve',json={'revision':r['revision'],'note':'旧页面提交'}).status_code==409


def test_real_handoff_remains_blocking_new_messages(monkeypatch):
    w,t,p,b=legacy_binding();value=coverage(b)
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    from app.services import c3_service
    with SessionLocal() as db:
        conv=db.get(Conversation,b['conversation_id'])
        c3_service._create_or_reuse_open_handoff(db,conversation=conv,batch_id=None,
            handoff_reason_code='HOOK_FRIEND_VERIFICATION_REQUIRED',reason_detail='Synthetic real restriction',
            trigger_message_event_ids=[],risk_flags=['contact_identity_unconfirmed'],evidence_refs=[],ai_payload={})
        db.commit()
    response,_=upload(w,p,b,add_message(value),monkeypatch)
    assert response.status_code==200,response.text
    assert response.json()['data']['message_batch'] is None
    with SessionLocal() as db:
        assert db.get(Conversation,b['conversation_id']).reply_ownership['owner']=='human'
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1


def test_catalog_change_does_not_reuse_native_fence(monkeypatch):
    w,t,p,b=legacy_binding();value=coverage(b)
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    next_value=add_message(value)
    for scan in next_value['scans']:
        scan['databases'][0]['generation']='b'*64
        scan['catalog_fingerprint']=shared_adapter('hook_contract_rules').digest([['MSG0.db','b'*64]])
    response,_=upload(w,p,b,next_value,monkeypatch);assert response.status_code==200,response.text
    assert response.json()['data']['reason_code']=='HOOK_HISTORY_BOUNDARY_GENERATION_CHANGED'
    assert response.json()['data']['message_batch'] is None


def test_old_invitation_without_local_clock_keeps_proof_and_requires_native_fence(monkeypatch):
    w,t,p,h,prep=submitted()
    with SessionLocal() as db:
        row=db.scalar(select(TaskEvidence).where(TaskEvidence.task_id==t['id'],TaskEvidence.evidence_type=='hook_invite_submission'))
        proof=dict(row.extra_metadata);proof.pop('local_attempted_at');row.extra_metadata=proof;db.commit()
    response=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(t,p,prep));assert response.status_code==200,response.text
    b=response.json()['data'];assert b['history_since_create_time']=='0'
    result,_=upload(w,p,b,coverage(b),monkeypatch);assert result.status_code==200,result.text
    assert result.json()['data']['message_batch'] is None
    assert client.get(review_url(b)).json()['data'][0]['reason']=='legacy_invitation_clock'
    with SessionLocal() as db:
        assert db.scalar(select(TaskEvidence).where(TaskEvidence.task_id==t['id'],TaskEvidence.evidence_type=='hook_invite_submission')).extra_metadata==proof


def test_empty_snapshot_is_not_a_native_boundary_for_late_history(monkeypatch):
    w,t,p,b=legacy_binding(); value=coverage(b); empty=deepcopy(value)
    for scan in empty['scans']:
        d=scan['databases'][0]
        d.update(pages=[], row_count='0', count_after='0', upper_local_id='0')
    response,_=upload(w,p,b,empty,monkeypatch)
    assert response.status_code==200,response.text
    assert response.json()['data']['message_batch'] is None
    with SessionLocal() as db:
        assert not db.get(WechatSessionBinding,b['binding_id']).native_context.get('native_admission_anchor')
    response,_=upload(w,p,b,value,monkeypatch)
    assert response.status_code==200,response.text
    assert response.json()['data']['message_batch'] is None
    assert len(client.get(review_url(b)).json()['data'][0]['messages'])==2
    response,_=upload(w,p,b,add_message(value),monkeypatch)
    assert response.json()['data']['message_batch']


@pytest.mark.parametrize('voice_state',['transcribed','failed','uncertain'])
def test_archived_voice_result_cannot_interrupt_new_reply_or_handoff(monkeypatch,voice_state):
    from test_hook_native_media import media_coverage, voice_payload
    w,t,p,b=legacy_binding(); value=media_coverage(b)
    response,_=upload(w,p,b,value,monkeypatch)
    assert response.status_code==200,response.text
    native_id=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    review=client.get(review_url(b)).json()['data'][0]
    assert client.post(review_url(b)+'/'+review['id']+'/resolve',json={'revision':review['revision'],'note':'先核对现有证据'}).status_code==200
    newer=add_message(value)
    response,_=upload(w,p,b,newer,monkeypatch)
    batch=response.json()['data']['message_batch'];assert batch
    payload=voice_payload(w,p,b,native_id,voice_state)
    result=post(w,f"/workers/{w['id']}/wechat/native-voice/result",payload)
    assert result.status_code==200,result.text
    assert not result.json()['data']['history_coverage_required']
    updated=client.get(review_url(b)).json()['data'][0]
    assert updated['status']=='pending' and updated['revision']==review['revision']+1
    assert client.post(review_url(b)+'/'+review['id']+'/resolve',json={'revision':review['revision'],'note':'旧页面'}).status_code==409
    repeat=post(w,f"/workers/{w['id']}/wechat/native-voice/result",payload)
    assert repeat.status_code==200
    assert client.get(review_url(b)).json()['data'][0]['revision']==updated['revision']
    again,_=upload(w,p,b,newer,monkeypatch)
    assert again.json()['data']['message_batch']['batch_id']==batch['batch_id']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==0
        assert db.get(MessageBatch,batch['batch_id']).active
        assert db.get(Conversation,b['conversation_id']).reply_ownership['owner']=='ai'


def test_review_resolution_cannot_target_another_customer_or_change_owner(monkeypatch):
    w,t,p,b=legacy_binding()
    assert upload(w,p,b,coverage(b),monkeypatch)[0].status_code==200
    review=client.get(review_url(b)).json()['data'][0]
    other=client.post('/api/leads',json={'customer_name':'Synthetic unrelated','phones':['13800000008']}).json()['data']
    assert client.post(f"/api/leads/{other['id']}/wechat/history-reviews/{review['id']}/resolve",json={'revision':review['revision'],'note':'wrong customer'}).status_code==404
    assert client.post(review_url(b)+'/'+review['id']+'/resolve',json={'revision':review['revision'],'note':'review','owner':'ai'}).status_code==400
    with SessionLocal() as db:
        assert db.get(HistoryReview,review['id']).status=='pending'
        assert db.get(Conversation,b['conversation_id']).reply_ownership['owner']=='legacy'
