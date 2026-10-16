"""Independent review: late producer receipts after real human closure, synthetic native inputs."""
import json
from sqlalchemy import select
import pytest
from app.core.database import SessionLocal
from app.models.c3 import Conversation,HandoffEvent
from app.models.wechat import MessageEvent
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound,upload
from test_hook_native_media import media_coverage,voice_payload
from test_hook_handoff_history import append_row


@pytest.mark.parametrize('voice_state',['uncertain','failed','transcribed'])
def test_late_original_voice_result_does_not_reopen_human_covered_handoff(monkeypatch,voice_state,tmp_path):
    w,t,p,b=bound();value=media_coverage(b,'3')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    append_row(value,kind='34',text='<msg><voicemsg length="43"/></msg>')
    mid=value['scans'][0]['databases'][0]['pages'][0][-1]['MsgSvrID']
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    assert r.json()['data']['reason_code']=='HOOK_VOICE_PENDING'
    with SessionLocal() as db:
        original=db.scalar(select(HandoffEvent));assert original.closed_at is None
        original_id=original.id
    append_row(value,sender='1',text='我是销售，刚才这条语音的问题已处理。')
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    assert r.json()['data']['history_ready']
    with SessionLocal() as db:assert db.get(HandoffEvent,original_id).closed_at is not None
    late=voice_payload(w,p,b,mid,voice_state)
    r=post(w,f"/workers/{w['id']}/wechat/native-voice/result",late);assert r.status_code==200,r.text
    # Same immutable receipt may be retried; no direct DB changes or synthetic closure.
    repeated=post(w,f"/workers/{w['id']}/wechat/native-voice/result",late);assert repeated.status_code==200,repeated.text
    r,_=upload(w,p,b,value,monkeypatch);assert r.status_code==200,r.text
    append_row(value,text='谢谢，现在想了解十五万元左右的车型。')
    next_round,_=upload(w,p,b,value,monkeypatch);assert next_round.status_code==200,next_round.text
    with SessionLocal() as db:
        events=list(db.scalars(select(HandoffEvent)));conv=db.get(Conversation,b['conversation_id'])
        parent=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
        evidence={'case':voice_state,'original_handoff_closed':db.get(HandoffEvent,original_id).closed_at is not None,
            'handoff_count':len(events),'open_handoff_count':sum(e.closed_at is None for e in events),
            'conversation_status':conv.status,'voice_state':parent.item_state,'history_ready':r.json()['data']['history_ready'],
            'new_customer_batch':next_round.json()['data']['message_batch'],
            'events':[{'id':e.id,'reason':e.handoff_reason_code,'closed':e.closed_at is not None} for e in events]}
    (tmp_path/f'late-voice-{voice_state}.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert evidence['open_handoff_count']==0,evidence
    assert evidence['handoff_count']==1 and evidence['new_customer_batch'],evidence


@pytest.mark.parametrize('case',['first_after_unproven_scan','legacy_replay','new_process_replay','after_human_voice','ai_echo','unknown_source','order_unproved'])
def test_late_voice_uses_original_trusted_human_scope(monkeypatch,case):
    """Real API closures; legacy/source mutations explicitly model stored rows."""
    from copy import deepcopy
    from app.models.wechat import WechatSessionBinding
    from app.models.hook_native import HookVoiceReceipt
    w,t,p,b=bound();value=media_coverage(b,'3')
    assert upload(w,p,b,value,monkeypatch)[0].status_code==200
    if case!='after_human_voice':
        append_row(value,kind='34',text='<msg><voicemsg length="43"/></msg>')
        mid=value['scans'][0]['databases'][0]['pages'][0][-1]['MsgSvrID']
        assert upload(w,p,b,value,monkeypatch)[0].status_code==200
        late=voice_payload(w,p,b,mid,'uncertain')
        if case in {'legacy_replay','new_process_replay'}:
            assert post(w,f"/workers/{w['id']}/wechat/native-voice/result",late).status_code==200
    append_row(value,sender='1',text='销售已处理刚才的问题')
    expected_covered=case in {'first_after_unproven_scan','legacy_replay','new_process_replay'}
    if case in {'ai_echo','unknown_source','order_unproved'}:
        unordered=deepcopy(value)
        for scan in unordered['scans']:
            rows=scan['databases'][0]['pages'][0];rows[-1]['MsgSequence']=rows[-2]['MsgSequence']
        held,_=upload(w,p,b,unordered,monkeypatch);assert held.status_code==200
        if case=='order_unproved':value=unordered
        else:
            with SessionLocal() as db:
                row=db.scalar(select(MessageEvent).where(MessageEvent.sender_role=='self'))
                row.raw_payload={**row.raw_payload,'sender_source':'ai' if case=='ai_echo' else 'unknown'}
                db.commit()
    before,_=upload(w,p,b,value,monkeypatch);assert before.status_code==200,before.text
    if expected_covered or case=='after_human_voice':
        with SessionLocal() as db:assert all(e.closed_at for e in db.scalars(select(HandoffEvent)))
    if case=='after_human_voice':
        append_row(value,kind='34',text='<msg><voicemsg length="45"/></msg>')
        mid=value['scans'][0]['databases'][0]['pages'][0][-1]['MsgSvrID']
        assert upload(w,p,b,value,monkeypatch)[0].status_code==200
        late=voice_payload(w,p,b,mid,'uncertain')
    if case=='first_after_unproven_scan':
        # A later incomplete order must not discard the already proved boundary.
        unordered=deepcopy(value)
        for scan in unordered['scans']:
            rows=scan['databases'][0]['pages'][0];rows[-1]['MsgSequence']=rows[-2]['MsgSequence']
        held,_=upload(w,p,b,unordered,monkeypatch)
        assert held.status_code==200 and not held.json()['data']['history_ready']
    if case in {'legacy_replay','new_process_replay'}:
        with SessionLocal() as db:
            receipt=db.scalar(select(HookVoiceReceipt));saved=(receipt.payload_digest,deepcopy(receipt.result_json))
            binding=db.get(WechatSessionBinding,b['binding_id'])
            ctx=dict(binding.native_context)
            ctx.pop('media_handoff_applied_ids',None)
            if case=='legacy_replay':ctx.pop('actor_history_receipt_id',None)
            binding.native_context=ctx;db.commit()
        if case=='new_process_replay':
            from test_hook_account_http import context,heartbeat
            current=context();current.update(account_epoch=p['account_epoch'],process_generation='process-2')
            assert heartbeat(w,current).status_code==200
            p={**p,'process_generation':'process-2'}
            late={**late,'process_generation':'process-2','attempt_process_generation':late['process_generation']}
    first=post(w,f"/workers/{w['id']}/wechat/native-voice/result",late)
    assert first.status_code==200,first.text
    again=post(w,f"/workers/{w['id']}/wechat/native-voice/result",late)
    assert again.status_code==200 and again.json()['data']==first.json()['data']
    with SessionLocal() as db:
        events=list(db.scalars(select(HandoffEvent)))
        assert any(e.closed_at is None for e in events) is not expected_covered
        voice=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
        assert voice.item_state=='uncertain' and voice.content is None
        receipt=db.scalar(select(HookVoiceReceipt))
        assert receipt.payload_json['audio_bytes']==late['audio_bytes']
        assert receipt.payload_json['audio_sha256']==late['audio_sha256']
        if case in {'legacy_replay','new_process_replay'}:assert (receipt.payload_digest,receipt.result_json)==saved
    if expected_covered:
        append_row(value,text='现在想了解十五万元的车型')
        next_round,_=upload(w,p,b,value,monkeypatch)
        assert next_round.status_code==200 and next_round.json()['data']['message_batch'],next_round.text
