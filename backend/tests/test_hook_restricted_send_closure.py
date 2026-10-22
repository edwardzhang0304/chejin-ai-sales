"""Real original-send HTTP gates; synthetic history, no external notifications."""
from copy import deepcopy
from datetime import datetime
import uuid
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.contracts.shared_rules import shared_adapter
from app.models.c3 import ReplyAction, SentAck, HandoffEvent
from app.models.wechat import WechatSessionBinding
from app.models.worker import Worker
from app.models.hook_native import HookHistoryReceipt
from app.services import hook_history_service
from test_hook_business_http import clean, post
from test_hook_native_send_http import beginning
from test_hook_friend_verification import append, notifications, REASON


@pytest.mark.parametrize('case', ['fresh','zero_notice','ambiguous','call_pending','old_notice',
    'plain_text','closed_handoff','other_customer','wrong_epoch','wrong_generation','incomplete',
    'runtime_blocked','old_permit','old_permit_same_second','baseline_notice','paused'])
def test_restriction_closure_uses_original_identity_complete_history_and_open_handoff(case,monkeypatch):
    w,p,b,h,permit,value,path,now,start=beginning(monkeypatch)
    notifications(w,monkeypatch)
    since=int(b['history_since_create_time'])
    if case=='ambiguous':
        for _ in range(2):append(value,kind='1',sender='1',text=permit['reply_text'],second=since+40)
    append(value,kind='1' if case=='plain_text' else '10000',second=since+(20 if case=='old_notice' else 40 if case=='old_permit_same_second' else 41))
    for scan in value['scans']:
        scan['observed_at']=now(50)
        if case=='zero_notice':scan['databases'][0]['pages'][0][-1]['MsgSvrID']='0'
    monkeypatch.setattr(hook_history_service,'utcnow',lambda:datetime.fromisoformat(now(60)))
    body={**p,'binding_id':b['binding_id'],'binding_revision':b['binding_revision'],
        'capture_id':str(uuid.uuid4()),'coverage':value,'coverage_digest':shared_adapter('hook_contract_rules').digest(value)}
    r=post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body);assert r.status_code==200,r.text
    with SessionLocal() as db:
        binding=db.get(WechatSessionBinding,b['binding_id']);action=db.get(ReplyAction,permit['reply_action_id'])
        event=db.scalar(select(HandoffEvent))
        if case=='closed_handoff':event.closed_at=datetime.fromisoformat(now(59))
        if case=='other_customer':event.conversation_id=str(uuid.uuid4())
        if case in {'wrong_epoch','wrong_generation','incomplete'}:
            receipt=db.get(HookHistoryReceipt,binding.native_context['active_history_receipt_id']);v=deepcopy(receipt.payload_json)
            if case=='incomplete':v['coverage']['local_pending_count']=1
            else:v['account_epoch' if case=='wrong_epoch' else 'process_generation']='different-scope'
            receipt.payload_json=v
        if case=='runtime_blocked':
            worker=db.get(Worker,w['id']);worker.hook_runtime_context={**worker.hook_runtime_context,'blocked':True}
        if case in {'old_permit','old_permit_same_second','baseline_notice'}:
            v=deepcopy(action.ai_payload)
            if case=='baseline_notice':v['native_send_identity']['baseline_friend_notice_keys']=binding.native_context['friend_verification_notice_keys']
            else:v['native_send_identity'].pop('baseline_friend_notice_keys')
            action.ai_payload=v
        if case=='paused':db.get(Worker,w['id']).run_status='paused'
        original=deepcopy(action.ai_payload);db.commit()
    payload={**p,'task_id':permit['task_id'],'send_token':permit['send_token'],
        'journal_sha256':start['journal_sha256'],'completed_at':now(41),'call_completed':case!='call_pending'}
    if case=='incomplete':
        with pytest.raises(ValueError,match='HOOK_HISTORY_LOCAL_PENDING'):post(w,path+'/observe',payload,h)
        with SessionLocal() as db:
            assert db.get(ReplyAction,permit['reply_action_id']).status=='sending'
            assert db.scalar(select(SentAck)) is None
        return
    r=post(w,path+'/observe',payload,h);assert r.status_code==200,r.text
    result=r.json()['data'];eligible=case in {'fresh','zero_notice','ambiguous','old_permit','paused'}
    assert result['early_settlement_reason']==(REASON if eligible else None)
    assert result['deadline_elapsed'] is False and result['unique_local_send_observed'] is False
    assert result['may_call_hook'] is False
    with SessionLocal() as db:
        assert db.get(ReplyAction,permit['reply_action_id']).status=='sending'
        assert db.scalar(select(SentAck)) is None
    if eligible:
        r=post(w,path+'/settle',payload,h);assert r.status_code==200,r.text
        settled=r.json()['data'];assert not settled['local_send_observed'] and not settled['automatic_resend_allowed']
        assert post(w,path+'/settle',payload,h).json()['data']==settled
        r=post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':permit['flow_id'],'terminal_kind':'task_terminal','conversation_id':b['conversation_id']},h)
        assert r.status_code==200,r.text
        with SessionLocal() as db:
            action=db.get(ReplyAction,permit['reply_action_id'])
            assert action.status=='unknown_send_result'
            assert action.ai_payload['native_send_identity']==original['native_send_identity']
            assert action.ai_payload['native_send_attempt']==original['native_send_attempt']
            assert db.scalar(select(SentAck)).send_result=='unknown'
            worker=db.get(Worker,w['id']);assert not worker.inflight_flow_state
            assert worker.run_status==('paused' if case=='paused' else 'running')
