"""Actual origin/binding/history/parent/notification APIs; no real model or WeChat."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,uuid
import pytest
from sqlalchemy import select,func
from app.core.database import SessionLocal
from app.models.hook_native import HookVoiceReceipt
from app.models.wechat import MessageEvent,WechatSessionBinding
from app.models.c3 import MessageBatch,HandoffEvent,Conversation
from app.models.sales import Sales
from app.contracts.shared_rules import shared_adapter
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound,coverage,upload


def media_coverage(binding,kind='34',self_second=False):
    value=coverage(binding);value['schema_version']='hook-history-coverage-v2'
    for scan in value['scans']:
        rows=scan['databases'][0]['pages'][0]
        rows[0]['Type']=kind
        rows[0]['StrContent']='<msg><voicemsg length="43"/></msg>' if kind=='34' else '<msg><img md5="test-only"/></msg>'
        if self_second:rows[1]['IsSender']='1'
    return value


def voice_payload(w,p,b,parent,state='transcribed',text='预算15万元，先不要报价🙂'):
    at=datetime.now(timezone.utc).isoformat()
    saved={**p,'native_msg_id':parent,'peer_wxid':b['peer_wxid'],'audio_sha256':hashlib.sha256(b'synthetic-silk-audio').hexdigest(),
        'audio_bytes':43,'state':state,'attempted_at':at,'completed_at':at,'response_sha256':'b'*64 if state=='transcribed' else None,
        'text':text if state=='transcribed' else None,'error_code':None if state=='transcribed' else 'HOOK_VOICE_TRANSCRIPTION_FAILED'}
    return {**saved,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
        'attempt_digest':shared_adapter('hook_contract_rules').digest(saved)}


def test_transcript_updates_original_parent_and_waiting_text_keeps_native_order(monkeypatch):
    w,t,p,b=bound();value=media_coverage(b)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    first=r.json()['data'];assert not first['history_ready'] and first['reason_code']=='HOOK_VOICE_PENDING'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MessageEvent))==2
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
        parent_id=parent.id;native_id=parent.raw_payload['native_msg_id']
    body=voice_payload(w,p,b,native_id)
    completed=post(w,f"/workers/{w['id']}/wechat/native-voice/result",body)
    assert completed.status_code==200,completed.text
    result=completed.json()['data'];assert result['message_event_id']==parent_id and not result['ai_dispatch']
    duplicate=post(w,f"/workers/{w['id']}/wechat/native-voice/result",body)
    assert duplicate.status_code==200 and duplicate.json()['data']==result
    with SessionLocal() as db:
        parent=db.get(MessageEvent,parent_id)
        assert parent.content==body['text'] and parent.item_state=='completed'
        assert db.scalar(select(func.count()).select_from(HookVoiceReceipt))==1
        assert db.scalar(select(func.count()).select_from(MessageEvent))==2
    latest,_=upload(w,p,b,value,monkeypatch);assert latest.status_code==200,latest.text
    ready=latest.json()['data'];assert ready['history_ready'] and ready['message_batch']
    from app.services.c3_service import _build_ai_context
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);conversation=db.get(Conversation,b['conversation_id'])
        batch=db.get(MessageBatch,ready['message_batch']['batch_id'])
        context=_build_ai_context(db,binding,conversation,batch)
        assert [m['content'] for m in context['messages']]==[body['text'],'预算15万元']
        assert ready['message_event_ids'][0]==parent_id
    changed=deepcopy(body);changed['text']='不能覆盖原转写'
    saved={k:v for k,v in changed.items() if k not in {'binding_id','binding_revision','attempt_digest'}}
    changed['attempt_digest']=shared_adapter('hook_contract_rules').digest(saved)
    rejected=post(w,f"/workers/{w['id']}/wechat/native-voice/result",changed)
    assert rejected.status_code==409 and rejected.json()['code']=='HOOK_VOICE_RESULT_CONFLICT'


@pytest.mark.parametrize('case',['other_peer','other_epoch','other_revision','outgoing','no_parent','digest'])
def test_unapproved_or_wrong_parent_results_cannot_enter_context(monkeypatch,case):
    w,t,p,b=bound();value=media_coverage(b)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200
    mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID'];body=voice_payload(w,p,b,mid)
    if case=='other_peer':body['peer_wxid']='wxid_wrong_peer'
    if case=='other_epoch':body['account_epoch']=str(uuid.uuid4())
    if case=='other_revision':body['binding_revision']+=1
    if case=='no_parent':body['native_msg_id']='9999999999999999999'
    if case=='digest':body['attempt_digest']='c'*64
    if case=='outgoing':
        with SessionLocal() as db:
            parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'));parent.sender_role='self';db.commit()
    rejected=post(w,f"/workers/{w['id']}/wechat/native-voice/result",body)
    assert rejected.status_code==409,rejected.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HookVoiceReceipt))==0
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0


def test_failed_customer_voice_uses_original_handoff_but_unknown_voice_waits(monkeypatch):
    w,t,p,b=bound();value=media_coverage(b);r,_=upload(w,p,b,value,monkeypatch)
    mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    failed=post(w,f"/workers/{w['id']}/wechat/native-voice/result",voice_payload(w,p,b,mid,'failed'))
    assert failed.status_code==200,failed.text
    ready,_=upload(w,p,b,value,monkeypatch);assert ready.status_code==200,ready.text
    with SessionLocal() as db:
        handoff=db.scalar(select(HandoffEvent))
        assert handoff and handoff.handoff_reason_code=='C2_VOICE_TRANSCRIBE_FAILED'
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'


def test_new_customer_image_handoffs_once_and_notification_is_not_duplicated(monkeypatch):
    from app.services import feishu_service
    w,t,p,b=bound();sent=[]
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);db.get(Sales,binding.sales_id).feishu_user_id='synthetic-open-id';db.commit()
    class Adapter:
        def send_text_message(self,open_id,message):sent.append((open_id,message))
    monkeypatch.setattr(feishu_service,'get_feishu_adapter',lambda:Adapter())
    value=media_coverage(b,'3');one,_=upload(w,p,b,value,monkeypatch);assert one.status_code==200,one.text
    again,_=upload(w,p,b,value,monkeypatch);assert again.status_code==200,again.text
    with SessionLocal() as db:
        events=list(db.scalars(select(HandoffEvent)));assert len(events)==1
        assert events[0].handoff_reason_code=='HOOK_CUSTOMER_IMAGE_HANDOFF'
        assert events[0].notify_status=='succeeded'
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
    assert len(sent)==1 and '客户发来图片' in sent[0][1]


def test_old_history_image_before_human_reply_does_not_notify_or_block_new_text(monkeypatch):
    w,t,p,b=bound();value=media_coverage(b,'3',self_second=True)
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==0
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        assert db.get(Conversation,b['conversation_id']).status=='sales_replied_waiting_user'


def test_restarted_same_account_delivers_saved_voice_without_retranscription(monkeypatch):
    from app.models.worker import Worker
    w,t,p,b=bound();value=media_coverage(b);first,_=upload(w,p,b,value,monkeypatch)
    mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    body=voice_payload(w,p,b,mid);done=post(w,f"/workers/{w['id']}/wechat/native-voice/result",body)
    assert done.status_code==200
    changed={**body,'process_generation':'another-process-generation','attempt_process_generation':p['process_generation']}
    with SessionLocal() as db:
        worker=db.get(Worker,w['id']);worker.hook_runtime_context={**worker.hook_runtime_context,'process_generation':changed['process_generation']};db.commit()
    repeated=post(w,f"/workers/{w['id']}/wechat/native-voice/result",changed)
    assert repeated.status_code==200 and repeated.json()['data']==done.json()['data']
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(HookVoiceReceipt))==1


def test_uncertain_voice_is_preserved_and_cannot_enter_ai_context(monkeypatch):
    w,t,p,b=bound();value=media_coverage(b);upload(w,p,b,value,monkeypatch)
    mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    done=post(w,f"/workers/{w['id']}/wechat/native-voice/result",voice_payload(w,p,b,mid,'uncertain'))
    assert done.status_code==200
    latest,_=upload(w,p,b,value,monkeypatch)
    assert latest.status_code==200 and latest.json()['data']['reason_code']=='HOOK_VOICE_PENDING'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        assert db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice')).content is None


@pytest.mark.parametrize('legacy_receipt',[False,True])
def test_unknown_voice_handoff_is_once_without_rewriting_original_result(monkeypatch,legacy_receipt):
    from app.models.worker import Worker
    w,t,p,b=bound();value=media_coverage(b);upload(w,p,b,value,monkeypatch)
    mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    body=voice_payload(w,p,b,mid,'uncertain')
    done=post(w,f"/workers/{w['id']}/wechat/native-voice/result",body)
    assert done.status_code==200
    with SessionLocal() as db:
        receipt=db.scalar(select(HookVoiceReceipt));original_receipt=(receipt.payload_digest,deepcopy(receipt.payload_json),deepcopy(receipt.result_json))
        event=db.scalar(select(HandoffEvent))
        assert event and event.handoff_reason_code=='HOOK_VOICE_RESULT_UNCONFIRMED'
        assert '未确认' in event.reason_detail
        if legacy_receipt:
            # Model an old admitted receipt with no business disposition.
            db.delete(event)
            binding=db.get(WechatSessionBinding,b['binding_id'])
            ctx=dict(binding.native_context);ctx.pop('media_handoff_applied_ids',None);binding.native_context=ctx
            conversation=db.get(Conversation,b['conversation_id']);conversation.status='ai_active';db.commit()
    repeated=post(w,f"/workers/{w['id']}/wechat/native-voice/result",body)
    assert repeated.status_code==200 and repeated.json()['data']==done.json()['data']
    inventory=post(w,f"/workers/{w['id']}/wechat/native-work",{**p,'retained_flow_id':None})
    assert inventory.status_code==200,inventory.text
    current=next(row for row in inventory.json()['data']['bindings'] if row['binding_id']==b['binding_id'])
    assert not current['voice_jobs'] and current['open_handoffs']
    with SessionLocal() as db:
        parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
        assert parent.item_state=='uncertain' and parent.content is None
        event=db.scalar(select(HandoffEvent));assert event and event.handoff_reason_code=='HOOK_VOICE_RESULT_UNCONFIRMED'
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1
        receipt=db.scalar(select(HookVoiceReceipt));assert (receipt.payload_digest,receipt.payload_json,receipt.result_json)==original_receipt
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'
        # An operator handles this handoff, then the original receipt is replayed
        # from a new process. It must not create another handoff or transcription.
        event.closed_at=datetime.now(timezone.utc)
        worker=db.get(Worker,w['id']);worker.hook_runtime_context={**worker.hook_runtime_context,'process_generation':'restarted-generation'}
        db.commit()
    replay={**body,'process_generation':'restarted-generation','attempt_process_generation':p['process_generation']}
    again=post(w,f"/workers/{w['id']}/wechat/native-voice/result",replay)
    assert again.status_code==200 and again.json()['data']==done.json()['data']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1
        assert db.scalar(select(HandoffEvent)).closed_at is not None
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
