"""Real backend parent/history/handoff lifecycle, isolated synthetic data."""
from datetime import datetime,timezone,timedelta
import pytest
from sqlalchemy import select,func
from app.core.database import SessionLocal
from app.models.c3 import HandoffEvent,MessageBatch,Conversation
from app.models.wechat import MessageEvent
from app.models.hook_native import HookVoiceReceipt
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound,upload
from test_hook_native_media import media_coverage,voice_payload


def prepare(monkeypatch):
    w,t,p,b=bound();value=media_coverage(b)
    response,_=upload(w,p,b,value,monkeypatch)
    assert response.status_code==200,response.text
    mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
    body={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
          'peer_wxid':b['peer_wxid'],'native_msg_id':mid}
    return w,b,body,value


def test_missing_audio_wait_survives_repeated_requests_then_hands_off_once(monkeypatch):
    from app.services import hook_media_service
    w,b,body,_=prepare(monkeypatch)
    now=datetime.now(timezone.utc)
    monkeypatch.setattr(hook_media_service,'utcnow',lambda:now)
    url=f"/workers/{w['id']}/wechat/native-voice/audio-wait"
    first=post(w,url,body);assert first.status_code==200,first.text
    original=first.json()['data'];assert original['state']=='waiting'
    now+=timedelta(seconds=59)
    assert post(w,url,body).json()['data']==original
    now+=timedelta(seconds=1)
    terminal=post(w,url,body).json()['data'];assert terminal['state']=='handed_off'
    assert terminal['native_call_started'] is False
    with SessionLocal() as db:
        handoff=db.scalar(select(HandoffEvent));assert handoff.handoff_reason_code=='HOOK_VOICE_AUDIO_MISSING'
        handoff.closed_at=now;db.commit()
    now+=timedelta(minutes=5)
    assert post(w,url,body).json()['data']==terminal
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==1
        assert db.scalar(select(func.count()).select_from(HookVoiceReceipt))==0
        assert db.scalar(select(func.count()).select_from(MessageBatch))==0
        parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
        assert parent.content is None and parent.item_state=='metadata_observed'
        assert db.get(Conversation,b['conversation_id']).status=='waiting_sales_reply'


def test_audio_arriving_within_budget_completes_parent_and_releases_waiting_text(monkeypatch):
    w,b,body,value=prepare(monkeypatch)
    url=f"/workers/{w['id']}/wechat/native-voice/audio-wait"
    assert post(w,url,body).json()['data']['state']=='waiting'
    p={k:body[k] for k in ('self_wxid','account_epoch','process_generation')}
    done=post(w,f"/workers/{w['id']}/wechat/native-voice/result",voice_payload(w,p,b,body['native_msg_id']))
    assert done.status_code==200,done.text
    assert post(w,url,body).json()['data']['state']=='settled'
    ready,_=upload(w,p,b,value,monkeypatch)
    assert ready.json()['data']['history_ready'] and ready.json()['data']['message_batch']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==0


@pytest.mark.parametrize('field,value',[('peer_wxid','wxid_wrong'),('native_msg_id','999999999'),('binding_revision',999)])
def test_waiting_report_requires_original_authorized_parent(monkeypatch,field,value):
    w,b,body,_=prepare(monkeypatch)
    response=post(w,f"/workers/{w['id']}/wechat/native-voice/audio-wait",{**body,field:value})
    assert response.status_code==409,response.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(HandoffEvent))==0
