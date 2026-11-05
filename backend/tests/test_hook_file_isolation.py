"""Real history API and handoff persistence, with synthetic file observations."""
from sqlalchemy import select, func
import pytest
from app.core.database import SessionLocal
from app.models.c3 import HandoffEvent, MessageBatch
from app.models.wechat import MessageEvent
from test_hook_business_http import clean
from test_hook_history_business_http import bound, upload
from test_hook_native_media import media_coverage


FILE = '<msg><appmsg><title>synthetic-diagnostics.zip</title><type>6</type><appattach><fileext>zip</fileext></appattach></appmsg></msg>'
QUOTE = '<msg><appmsg><type>57</type><title>合成引用回复</title><refermsg><svrid>12345</svrid></refermsg></appmsg></msg>'
UNSUPPORTED = '<msg><appmsg><type>33</type><title>合成卡片</title></appmsg></msg>'


@pytest.mark.parametrize('body', [FILE, '', '<msg><appmsg>', '<msg><appmsg><type>57</type></appmsg></msg>'])
def test_app_message_is_customer_scoped_and_handoff_is_idempotent(monkeypatch,body):
    w,_,p,b = bound()
    value=media_coverage(b,'49')
    for scan in value['scans']:
        scan['databases'][0]['pages'][0][0]['StrContent']=body
    for _ in range(3):
        r,_=upload(w,p,b,value,monkeypatch)
        assert r.status_code==200,r.text
        result=r.json()['data']
        assert not result['message_batch']
    with SessionLocal() as db:
        handoffs=list(db.scalars(select(HandoffEvent)))
        assert len(handoffs)==1
        assert handoffs[0].conversation_id==b['conversation_id']
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type.in_(['file','app_message'])))
        assert parent and parent.sender_role=='customer'
        assert parent.raw_payload['native_media_xml']==body


def test_outgoing_empty_app_message_keeps_direction(monkeypatch):
    w,_,p,b = bound()
    value=media_coverage(b,'49')
    for scan in value['scans']:
        first=scan['databases'][0]['pages'][0][0]
        first.update(StrContent='',IsSender='1')
    r,_=upload(w,p,b,value,monkeypatch)
    assert r.status_code==200,r.text
    assert not r.json()['data']['history_ready']
    with SessionLocal() as db:
        parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='app_message'))
        assert parent.sender_role=='self'
        assert parent.raw_payload['sender_source']=='unresolved'
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0


def test_later_file_body_completes_same_message_without_reopening_automation(monkeypatch):
    from app.models.hook_native import HookHistoryReceipt
    from app.models.c3 import Conversation
    w,_,p,b=bound()
    value=media_coverage(b,'49')
    for body in ['',FILE,FILE]:
        for scan in value['scans']:
            scan['databases'][0]['pages'][0][0]['StrContent']=body
        response,_=upload(w,p,b,value,monkeypatch)
        assert response.status_code==200,response.text
        assert not response.json()['data']['message_batch']
    with SessionLocal() as db:
        events=list(db.scalars(select(MessageEvent).where(MessageEvent.message_type.in_(['file','app_message']))))
        assert len(events)==1 and events[0].message_type=='file'
        assert events[0].raw_payload['native_media_xml']==FILE
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'
        receipts=list(db.scalars(select(HookHistoryReceipt)))
        assert len(receipts)==3
        assert any(r.payload_json['coverage']['scans'][0]['databases'][0]['pages'][0][0]['StrContent']=='' for r in receipts)


@pytest.mark.parametrize('known,kind',[(FILE,'file'),(QUOTE,'quote'),(UNSUPPORTED,'unsupported')])
@pytest.mark.parametrize('sender',['0','1'])
def test_known_app_temporarily_incomplete_preserves_facts_and_direction(monkeypatch,known,kind,sender):
    from app.models.hook_native import HookHistoryReceipt
    w,_,p,b=bound();value=media_coverage(b,'49')
    native_id=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    fact_id=None
    for body in [known,'','<broken',known,known]:
        for scan in value['scans']:
            scan['databases'][0]['pages'][0][0].update(StrContent=body,IsSender=sender)
        response,_=upload(w,p,b,value,monkeypatch)
        assert response.status_code==200,response.text
        if body!=known:
            assert response.json()['data']['history_ready'] is False
            assert response.json()['data']['content_isolated'] is True
        with SessionLocal() as db:
            event=next(m for m in db.scalars(select(MessageEvent)) if m.raw_payload.get('native_msg_id')==native_id)
            if fact_id is None:fact_id=event.id
            assert event.id==fact_id
            assert event.sender_role==('self' if sender=='1' else 'customer')
            assert event.raw_payload['native_app_message']['kind']==kind
            assert db.get(HookHistoryReceipt,event.raw_payload['native_app_fact_receipt_id'])
            if kind=='quote':assert event.content=='合成引用回复'
            else:assert event.raw_payload['native_media_xml']==known
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1


def test_missing_content_does_not_relax_direction_conflict(monkeypatch):
    w,_,p,b=bound();value=media_coverage(b,'49')
    for scan in value['scans']:scan['databases'][0]['pages'][0][0]['StrContent']=FILE
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    for scan in value['scans']:scan['databases'][0]['pages'][0][0].update(StrContent='',IsSender='1')
    response,_=upload(w,p,b,value,monkeypatch)
    assert response.status_code==409 and response.json()['code']=='HOOK_MESSAGE_ID_CONFLICT'


def test_legacy_quote_fact_keeps_original_receipt_when_content_disappears(monkeypatch):
    w,_,p,b=bound();value=media_coverage(b,'49')
    for scan in value['scans']:scan['databases'][0]['pages'][0][0]['StrContent']=QUOTE
    first,_=upload(w,p,b,value,monkeypatch);assert first.status_code==200
    receipt=first.json()['data']['history_receipt_id']
    with SessionLocal() as db:
        event=next(m for m in db.scalars(select(MessageEvent)) if m.raw_payload.get('native_quote'))
        event.raw_payload={k:v for k,v in event.raw_payload.items()
                           if k not in {'native_app_message','native_app_observation','native_app_fact_receipt_id'}}
        mid=event.id;db.commit()
    for scan in value['scans']:scan['databases'][0]['pages'][0][0]['StrContent']=''
    response,_=upload(w,p,b,value,monkeypatch)
    assert response.status_code==200 and response.json()['data']['content_isolated']
    with SessionLocal() as db:
        event=db.get(MessageEvent,mid)
        assert event.content=='合成引用回复'
        assert event.raw_payload['native_app_message']['kind']=='quote'
        assert event.raw_payload['native_app_fact_receipt_id']==receipt
