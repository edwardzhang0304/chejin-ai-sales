"""Independent API chain; synthetic native observations, no DB state manipulation."""
import json
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch, HandoffEvent
from test_hook_business_http import clean, post
from test_hook_history_business_http import bound, upload
from test_hook_native_media import media_coverage, voice_payload
from test_hook_handoff_history import append_row


@pytest.mark.parametrize('voice_state',['uncertain','failed','transcribed'])
def test_late_old_voice_keeps_new_customer_reply_reachable(monkeypatch,voice_state,tmp_path):
    w,t,p,b=bound(); value=media_coverage(b,'3')
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    append_row(value,kind='34',text='<msg><voicemsg length="43"/></msg>')
    mid=value['scans'][0]['databases'][0]['pages'][0][-1]['MsgSvrID']
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    append_row(value,sender='1',text='我是销售，刚才语音的问题已处理')
    assert upload(w,p,b,value,monkeypatch)[0].json()['data']['history_ready']
    append_row(value,text='现在想了解十五万元的车型')
    incoming=upload(w,p,b,value,monkeypatch)[0]
    assert incoming.status_code==200
    before=incoming.json()['data']['message_batch']
    assert before['next_step']=='generate'
    late=voice_payload(w,p,b,mid,voice_state)
    r=post(w,f"/workers/{w['id']}/wechat/native-voice/result",late)
    assert r.status_code==200,r.text
    after=upload(w,p,b,value,monkeypatch)[0]
    assert after.status_code==200 and after.json()['data']['history_ready'],after.text
    repeated=upload(w,p,b,value,monkeypatch)[0]
    assert repeated.status_code==200
    inventory=post(w,f"/workers/{w['id']}/wechat/native-work",{**p,'retained_flow_id':None})
    assert inventory.status_code==200
    with SessionLocal() as db:
        evidence={'voice_state':voice_state,'before':before,'after':after.json()['data']['message_batch'],
            'repeated':repeated.json()['data']['message_batch'],
            'inventory_batch_ids':inventory.json()['data']['bindings'][0]['batch_ids'],
            'batches':[{'id':x.id,'status':x.status,'active':x.active} for x in db.scalars(select(MessageBatch))],
            'open_handoffs':sum(x.closed_at is None for x in db.scalars(select(HandoffEvent)))}
    (tmp_path/f'active-batch-{voice_state}.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert evidence['open_handoffs']==0,evidence
    assert evidence['after']['next_step']=='generate' and evidence['inventory_batch_ids'],evidence


@pytest.mark.parametrize('voice_state',['uncertain','failed','transcribed'])
def test_new_voice_after_human_still_invalidates_the_older_customer_batch(monkeypatch,voice_state):
    """Opposite order: the voice is new business, not an archived old result."""
    w,t,p,b=bound();value=media_coverage(b,'3')
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    append_row(value,sender='1',text='销售已处理之前的问题')
    assert upload(w,p,b,value,monkeypatch)[0].json()['data']['history_ready']
    append_row(value,text='请先介绍十五万元的车型')
    original=upload(w,p,b,value,monkeypatch)[0].json()['data']['message_batch']['batch_id']
    append_row(value,kind='34',text='<msg><voicemsg length="43"/></msg>')
    mid=value['scans'][0]['databases'][0]['pages'][0][-1]['MsgSvrID']
    pending=upload(w,p,b,value,monkeypatch)[0]
    assert pending.status_code==200 and pending.json()['data']['reason_code']=='HOOK_VOICE_PENDING'
    late=post(w,f"/workers/{w['id']}/wechat/native-voice/result",voice_payload(w,p,b,mid,voice_state))
    assert late.status_code==200 and late.json()['data']['history_coverage_required']
    current=upload(w,p,b,value,monkeypatch)[0].json()['data']
    with SessionLocal() as db:
        old=db.get(MessageBatch,original)
        assert old.status=='cancelled' and not old.active
        open_handoffs=[h for h in db.scalars(select(HandoffEvent)) if h.closed_at is None]
        if voice_state=='transcribed':
            assert not open_handoffs and current['history_ready']
            assert current['message_batch']['batch_id']!=original
            assert current['message_batch']['next_step']=='generate'
        else:
            assert open_handoffs and current['message_batch'] is None
