"""History → human handoff closure → Brain/reviewer → Worker send/ack.

Windows/Hook effects and external model HTTP responses are synthetic. Connection,
backend, native history validation, Brain subprocess, send journal/lock and
settlement use current production code. No production service is contacted.
"""
import json
import sqlite3
import time
from pathlib import Path
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.core.config import get_settings
from app.models.c3 import Conversation,HandoffEvent,ReplyAction,SentAck
from app.models.wechat import MessageEvent
from app.models.task import Task
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_hook_business_http import clean,post
from test_hook_history_business_http import bound
from test_hook_native_media import voice_payload
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_callback_recovery import connection_factory
from test_hook_formal_runtime import drive_reply
from test_partial_reply_recovery_provider import recovery_provider_factory
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime


@pytest.mark.parametrize('mode',['uncertain_voice','voice_during_image_handoff','late_uncertain_inventory'])
def test_human_reply_unblocks_later_customer_through_actual_pipeline(
        identity_client,lab,connection_factory,tmp_path,monkeypatch,recovery_provider_factory,mode):
    w,task,account,binding=bound()
    server,gateway,*_=lab
    manager,credentials,prepared=connection_factory(w)
    runtime=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),credentials,
        gateway,prepared,tmp_path/'runtime')
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'handoff-handle'}]
    server.query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
    # A native second at the invitation boundary is intentionally not admissible.
    while int(time.time())<=int(binding['history_since_create_time']):time.sleep(.02)
    def append(kind,sender,content):
        seq=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1
        created=int(time.time());mid=str(8000000000000000000+seq)
        table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
            (seq,mid,kind,sender,created,created*1000+seq,0,seq,binding['peer_wxid'],content))
        return mid
    def on_request(path):
        server.raw_override=None
        if path=='/SendTextMsg':
            append(1,1,server.requests[-1][1]['msg'])
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    voice='<msg><voicemsg length="43"/></msg>'
    parent=append(34 if mode=='uncertain_voice' else 3,0,voice if mode=='uncertain_voice' else '<msg><img md5="synthetic"/></msg>')
    first=runtime.sync(binding)
    if mode=='uncertain_voice':
        assert first['reason_code']=='HOOK_VOICE_PENDING'
        result=post(w,f"/workers/{w['id']}/wechat/native-voice/result",
            voice_payload(w,runtime.account,binding,parent,'uncertain'))
        assert result.status_code==200,result.text
    if mode=='late_uncertain_inventory':
        # Exercise an already-started local transcription as a producer fixture,
        # not admission of new work during handoff. The real transcriber persists
        # its unknown result; the real dispatcher later recovers the saved row.
        parent=append(34,0,voice)
        assert runtime.sync(binding)['reason_code']=='HOOK_VOICE_PENDING'
        from dataclasses import replace
        from test_hook_voice import AUDIO,persist
        from chejin_worker_client.hook_voice import capture_voice_audio,process_voice_call_unresolved
        from chejin_worker_client.hook_gateway import HookLocalError
        from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
        dispatcher=HookBusinessDispatcher(runtime.client,tmp_path/'runtime',
            lambda *args:pytest.fail('unexpected invite'),lambda *args:None,
            automatic_transcription_disabled=lambda:True)
        persist(lab,metadata=True,audio=True,mid=parent,data=AUDIO,scope=runtime.scope)
        audio=capture_voice_audio(runtime.scope,binding['peer_wxid'],parent,max_audio_bytes=4096)
        assert audio and audio.audio==AUDIO
        old_generation=prepared.process.generation
        server.voice_response={'status':1,'text':''}
        with pytest.raises(HookLocalError,match='VOICE_RESPONSE_INVALID'):
            dispatcher.voice.transcribe_once(gateway,prepared.process,audio,
                automatic_transcription_disabled=lambda:True)
        original_attempt=dispatcher.voice.saved_parent(runtime.scope,binding['peer_wxid'],parent)
        assert original_attempt['state']=='uncertain' and process_voice_call_unresolved(old_generation)
        with pytest.raises(HookLocalError,match='QUARANTINED'):gateway.login_status(prepared.process)
        manager.close()
        server.current_process=replace(prepared.process,started_at=prepared.process.started_at+1)
        manager,credentials,prepared=connection_factory(w)
        runtime=HookFormalRuntime(runtime.client,credentials,gateway,prepared,tmp_path/'runtime')
        assert runtime.sync(binding)['reason_code']=='HOOK_VOICE_PENDING'
        inventory=runtime.client.call(credentials,'POST',f"/workers/{w['id']}/wechat/native-work",
            {**runtime.account,'retained_flow_id':None})
        assert [j['native_msg_id'] for j in inventory['bindings'][0]['voice_jobs']]==[parent]
    with SessionLocal() as db:
        handoff=db.scalar(select(HandoffEvent));assert handoff and handoff.closed_at is None
        handoff_id=handoff.id
    if mode=='voice_during_image_handoff':
        parent=append(34,0,voice)
        assert runtime.sync(binding)['reason_code']=='HOOK_VOICE_PENDING'
    append(1,1,'我是销售，刚才的问题已经解释过了，您还想了解什么？')
    if mode=='late_uncertain_inventory':
        from types import SimpleNamespace
        import threading
        from chejin_worker_client.models import Binding
        # Tick obtains inventory containing the old voice, then history sees the
        # sales reply, then the SAME tick uploads the genuinely saved old result.
        runner=SimpleNamespace(binding=Binding(w['id'],w['worker_token'],w['instance'],'running'),
            stop_event=threading.Event(),api=SimpleNamespace(inflight_flow_id=None),current_task=None,
            on_task=lambda task:None,can_pull_tasks=lambda:False,_can_start_new_flow=lambda:False)
        services=SimpleNamespace(connection=manager,_credentials=credentials,
            startup_window_ready_for=lambda p:p is manager.prepared)
        dispatcher.tick(runner,services)
        runtime=dispatcher.runtime
        assert dispatcher.voice.saved_parent(runtime.scope,binding['peer_wxid'],parent)==original_attempt
        assert process_voice_call_unresolved(old_generation)
        assert not process_voice_call_unresolved(prepared.process.generation)
        assert list((tmp_path/'runtime/voice-receipts').rglob('*.json'))
        runtime=HookFormalRuntime(runtime.client,credentials,gateway,prepared,tmp_path/'runtime')
    human=runtime.sync(binding);assert human['history_ready'],human
    with SessionLocal() as db:
        handoff=db.get(HandoffEvent,handoff_id)
        assert handoff.status=='sales_replied' and handoff.closed_at
        assert db.get(Conversation,binding['conversation_id']).status=='sales_replied_waiting_user'
        original=db.scalar(select(MessageEvent).where(MessageEvent.message_type=='voice'))
        assert original.item_state==('metadata_observed' if mode=='voice_during_image_handoff' else 'uncertain') and original.content is None
    inventory=runtime.client.call(credentials,'POST',f"/workers/{w['id']}/wechat/native-work",{**runtime.account,'retained_flow_id':None})
    assert not inventory['bindings'][0]['voice_jobs'] and not inventory['bindings'][0]['open_handoffs']
    append(1,0,'现在想先了解十五万元左右的车型，暂时不预约看车。')
    incoming=runtime.sync(binding);assert incoming['history_ready'] and incoming['message_batch']
    bid=incoming['message_batch']['batch_id']
    requests=recovery_provider_factory('好的，先按十五万元左右的预算了解，您更喜欢轿车还是SUV？')
    root=str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',root);monkeypatch.setattr(get_settings(),'c3_omniauto_root',root)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    sent=drive_reply(runtime,binding,bid)
    assert len(sent['sends'])==1 and sent['sends'][0]['local_send_observed'],sent
    assert [r['model'] for r in requests]==['recovery-author','recovery-reviewer']
    with SessionLocal() as db:
        action=db.scalar(select(ReplyAction).where(ReplyAction.batch_id==bid))
        ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==action.id))
        settlement=action.ai_payload['native_send_settlement'];identity=action.ai_payload['native_send_identity']
        assert action.status=='sent' and ack and db.get(Task,action.claimed_task_id).status=='completed'
        intent=json.loads(next((tmp_path/'runtime/sends').glob(f'*/*/{action.id}/intent.json')).read_text())
        assert .5<=intent['delay_seconds']<=3
        evidence={'scope':__doc__,'invite_task_id':task['id'],'handoff_id':handoff_id,'batch_id':bid,'action_id':action.id,'ack_id':ack.id,
            'task_id':action.claimed_task_id,'native_msg_id':settlement['result']['native_msg_id'],
            'delay_seconds':intent['delay_seconds'],'parent_voice_state':original.item_state}
        (tmp_path/'handoff-chain-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    repeated=post(w,f"/workers/{w['id']}/wechat/native-replies/{action.id}/settle",settlement['request'],
        {'X-Inflight-Flow-Id':identity['flow_id'],'X-Task-Lease-Fencing-Token':str(identity['lease_fencing_token'])})
    assert repeated.status_code==200 and repeated.json()['data']==settlement['result']
    with SessionLocal() as db:
        assert len(list(db.scalars(select(SentAck))))==1
        assert len(list(db.scalars(select(HandoffEvent))))==1
    assert not drive_reply(runtime,binding,bid,max_wait_seconds=5)['sends']
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1
    assert len([r for r in server.requests if r[0]=='/Voice2Text'])==(1 if mode=='late_uncertain_inventory' else 0)
    assert all(server.lock_observations[i]['locked'] for i,r in enumerate(server.requests) if r[0]=='/SendTextMsg')
    assert list((tmp_path/'runtime/finished').glob('*.json'))
    table.close()
