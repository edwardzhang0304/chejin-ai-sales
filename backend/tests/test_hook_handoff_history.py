"""Independent audit: actual Hook APIs, synthetic message/provider inputs only."""
import json
from sqlalchemy import select
import pytest
from app.core.database import SessionLocal
from app.models.c3 import Conversation,HandoffEvent
from app.models.wechat import MessageEvent
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound,coverage,upload
from test_hook_native_media import media_coverage,voice_payload



def append_row(value,*,kind='1',sender='0',text='客户后续正常文字'):
    for scan in value['scans']:
        d=scan['databases'][0];rows=d['pages'][0];last=rows[-1]
        i=int(last['localId'])+1
        rows.append({**last,'localId':str(i),'MsgSvrID':str(int(last['MsgSvrID'])+1),
                     'Sequence':str(int(last['Sequence'])+1),'MsgSequence':str(int(last['MsgSequence'])+1),
                     'Type':kind,'IsSender':sender,'StrContent':text})
        d.update(upper_local_id=str(i),row_count=str(len(rows)),count_after=str(len(rows)))

@pytest.mark.parametrize('mode',['uncertain_voice','uncertain_after_restart','voice_during_image_handoff','failed_voice_control','image_control'])
def test_human_reply_can_close_media_handoff_without_automated_voice_retry(monkeypatch,mode,tmp_path):
    w,t,p,b=bound();value=media_coverage(b,'3' if mode in {'voice_during_image_handoff','image_control'} else '34')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    if mode in {'uncertain_voice','uncertain_after_restart','failed_voice_control'}:
        mid=value['scans'][0]['databases'][0]['pages'][0][0]['MsgSvrID']
        r=post(w,f"/workers/{w['id']}/wechat/native-voice/result",voice_payload(w,p,b,mid,'failed' if mode=='failed_voice_control' else 'uncertain'))
        assert r.status_code==200,r.text
        response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    with SessionLocal() as db:
        event=db.scalar(select(HandoffEvent));assert event and event.closed_at is None
        handoff_id=event.id
    if mode=='uncertain_after_restart':
        from test_hook_account_http import context,heartbeat
        hc=context();hc.update(account_epoch=p['account_epoch'],process_generation='process-2')
        restarted=heartbeat(w,hc);assert restarted.status_code==200,restarted.text
        assert not restarted.json()['data']['hook_context']['blocked']
        p={**p,'process_generation':'process-2'}
    if mode=='voice_during_image_handoff':
        append_row(value,kind='34',text='<msg><voicemsg length="43"/></msg>')
        response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    append_row(value,sender='1',text='我是销售，这条语音我已经听过，接下来由我协助您')
    response,_=upload(w,p,b,value,monkeypatch);assert response.status_code==200,response.text
    data=response.json()['data']
    inv=post(w,f"/workers/{w['id']}/wechat/native-work",{**p,'retained_flow_id':None})
    assert inv.status_code==200,inv.text
    with SessionLocal() as db:
        event=db.get(HandoffEvent,handoff_id);conv=db.get(Conversation,b['conversation_id'])
        evidence={'mode':mode,'history_ready':data['history_ready'],'history_reason':data['reason_code'],
            'handoff_closed':event.closed_at is not None,'handoff_status':event.status,'conversation_status':conv.status,
            'human_message_saved':any(m.sender_role=='self' for m in db.scalars(select(MessageEvent))),
            'voice_jobs':inv.json()['data']['bindings'][0]['voice_jobs'],
            'open_handoff_count':len(inv.json()['data']['bindings'][0]['open_handoffs'])}
    evidence['inventory_binding']=inv.json()['data']['bindings'][0]
    if mode in {'uncertain_voice','uncertain_after_restart','voice_during_image_handoff'}:
        append_row(value,text='人工已经解释过了，现在想再了解十五万元的车型')
        later,_=upload(w,p,b,value,monkeypatch);assert later.status_code==200,later.text
        evidence['later_customer_history_ready']=later.json()['data']['history_ready']
        evidence['later_customer_message_batch']=later.json()['data']['message_batch']
    (tmp_path/(mode+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert evidence['human_message_saved']
    assert evidence['handoff_closed'] and evidence['conversation_status']=='sales_replied_waiting_user',evidence

    assert not evidence['voice_jobs'] and evidence['open_handoff_count']==0
    if 'later_customer_history_ready' in evidence:
        assert evidence['later_customer_history_ready'] and evidence['later_customer_message_batch']
    if mode in {'uncertain_voice','uncertain_after_restart'}:
        with SessionLocal() as db:
            voice=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
            assert voice.item_state=='uncertain' and voice.content is None
        # Use the original admitted receipt, not a newly timed replacement.
        from app.models.hook_native import HookVoiceReceipt
        with SessionLocal() as db: replay=dict(db.scalar(select(HookVoiceReceipt)).payload_json)
        replay['attempt_process_generation']=replay['process_generation']
        replay['process_generation']=p['process_generation']
        again=post(w,f"/workers/{w['id']}/wechat/native-voice/result",replay)
        assert again.status_code==200,again.text
        with SessionLocal() as db:
            assert len(list(db.scalars(select(HandoffEvent))))==1
            assert db.get(HandoffEvent,handoff_id).closed_at is not None


@pytest.mark.parametrize('case',['earlier_human','later_voice','ai_echo','unknown_actor','ai_pending_ack','order_unproved','paused','rejected','ai_disabled','unmanaged'])
def test_media_handoff_history_boundaries(monkeypatch,case):
    """API transition matrix; stored actor/stop variants are explicit fixtures.

    The runtime test separately covers the continuous producer/consumer chain.
    """
    from app.models.wechat import WechatSessionBinding
    from app.models.c3 import ReplyAction
    w,t,p,b=bound();value=media_coverage(b,'3')
    if case=='earlier_human':
        for scan in value['scans']:
            rows=scan['databases'][0]['pages'][0]
            rows[0]['Type']='1';rows[0]['IsSender']='1';rows[0]['StrContent']='此前的人工回复'
            rows[1]['Type']='3';rows[1]['StrContent']='<msg><img md5="synthetic"/></msg>'
    first,_=upload(w,p,b,value,monkeypatch);assert first.status_code==200,first.text
    with SessionLocal() as db:
        event=db.scalar(select(HandoffEvent));assert event and not event.closed_at
        handoff_id=event.id
        conv=db.get(Conversation,b['conversation_id'])
        if case=='rejected':conv.status='rejected';conv.ai_enabled=False
        if case=='ai_disabled':conv.ai_enabled=False
        if case=='unmanaged':db.get(WechatSessionBinding,b['binding_id']).allow_listening=False
        db.commit()
    if case=='paused':
        assert post(w,f"/workers/{w['id']}/run-status",{'run_status':'paused','client_instance_id':w['instance']}).status_code==200
    if case!='earlier_human':
        append_row(value,kind='34',text='<msg><voicemsg length="43"/></msg>')
        if case in {'ai_echo','unknown_actor','ai_pending_ack'}:
            # Persist outgoing identity via history while order is deliberately
            # unproven, then provide a pre-existing sender classification.
            append_row(value,sender='1',text='程序发出过的相同文字')
            unordered=__import__('copy').deepcopy(value)
            for scan in unordered['scans']:
                rows=scan['databases'][0]['pages'][0];rows[-1]['MsgSequence']=rows[-2]['MsgSequence']
            held,_=upload(w,p,b,unordered,monkeypatch);assert held.status_code==200
            with SessionLocal() as db:
                message=db.scalar(select(MessageEvent).where(MessageEvent.sender_role=='self'))
                assert message
                message.raw_payload={**message.raw_payload,'sender_source':{'ai_echo':'ai','unknown_actor':'unknown','ai_pending_ack':'ai_pending_ack'}[case]}
                db.commit()
        else:append_row(value,sender='1',text='销售已人工回复')
        if case=='later_voice':append_row(value,kind='34',text='<msg><voicemsg length="45"/></msg>')
        if case=='order_unproved':
            for scan in value['scans']:
                rows=scan['databases'][0]['pages'][0];rows[-1]['MsgSequence']=rows[-2]['MsgSequence']
    response,body=upload(w,p,b,value,monkeypatch)
    if case=='unmanaged':
        assert response.status_code==409
    else:
        assert response.status_code==200,response.text
        assert post(w,f"/workers/{w['id']}/wechat/native-history/coverage",body).json()['data']==response.json()['data']
    closed=case in {'later_voice','paused','ai_disabled'}
    with SessionLocal() as db:
        event=db.get(HandoffEvent,handoff_id)
        assert bool(event.closed_at)==closed
        assert not list(db.scalars(select(ReplyAction)))
        conv=db.get(Conversation,b['conversation_id'])
        if case in {'rejected','ai_disabled'}:assert conv.ai_enabled is False
        if case=='rejected':assert conv.status=='rejected'
    if case=='later_voice':
        assert response.json()['data']['reason_code']=='HOOK_VOICE_PENDING'
        inventory=post(w,f"/workers/{w['id']}/wechat/native-work",{**p,'retained_flow_id':None}).json()['data']
        jobs=inventory['bindings'][0]['voice_jobs'];assert len(jobs)==1
        assert jobs[0]['native_msg_id']==value['scans'][0]['databases'][0]['pages'][0][-1]['MsgSvrID']
