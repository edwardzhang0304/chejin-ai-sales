"""Real Worker/backend with simulated Hook HTTP, mock AI, virtual due check."""
from test_hook_formal_runtime import (
    replace, SimpleNamespace, json, sqlite3, time, Path, pytest, select,
    SessionLocal, Task, MessageEvent, clean, claimed, post, identity_client, lab,
    HookWorkerCredentials, HookFormalClient, HookFormalRuntime, ReplayUI,
    calibrated_clock, drive_reply,
)
from test_partial_reply_recovery_provider import recovery_provider_factory
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services import c3_service
from app.core.config import get_settings
from app.models.c3 import ReplyAction, SentAck

@pytest.mark.parametrize('result_mode',['sent','unknown'])
def test_recall_through_actual_worker_sender_and_ack(identity_client,lab,tmp_path,monkeypatch,recovery_provider_factory,result_mode):
    segments=1
    server,gateway,process,scope,*_=lab
    w,t,account=claimed(process_generation=process.generation,pending=True)
    scope=replace(scope,account_epoch=account['account_epoch'])
    credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    prepared=SimpleNamespace(process=process,registration=SimpleNamespace(scope=scope),
        clock=calibrated_clock(identity_client,credentials,gateway,process,scope))
    runtime=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),
        credentials,gateway,prepared,tmp_path/'coordinator')
    uis=[]
    def factory(prep,guard):
        class ClipboardReplay(ReplayUI):
            def __init__(self):super().__init__();self.clipboard_field_events=[]
            def replace(self,frame,text):
                guard();super().replace(frame,text)
                if frame.name.startswith('fields'):
                    self.clipboard_field_events.append({'copy_calls':1,'paste_calls':1,'clipboard_match':True,'completion_guard_passed':True})
            def capture(self,name):
                guard();frame=super().capture(name)
                if name=='lookup':
                    for row in frame.rows:row['text']=row['text'].replace('13800000003',prep['primary_phone'])
                return frame
        ui=ClipboardReplay();uis.append(ui);return ui
    outcome=runtime.invite(t,factory)
    assert 'task' in outcome.get('settlement',{}),outcome
    assert outcome['settlement']['task']['result_code']=='invite_sent'
    code=outcome['preparation']['remark_code']
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'runtime-test-handle'}]
    server.query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
    def on_request(path):
        server.raw_override=None
        if path=='/GetFriendAndChatRoomList':server.raw_override=json.dumps({'code':0,'data':[{'wxid':'wxid_customer_A','markname':code}]}).encode()
        if path=='/SendTextMsg':
            text=server.requests[-1][1]['msg'];created=int(time.time());seq=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1
            if not (result_mode=='unknown' and seq==2):
                table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(seq,str(8000000000000000000+seq),1,1,created,created*1000+seq,0,seq,'wxid_customer_A',text))
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    binding=runtime.discover(t['id'])
    before=runtime.sync(binding);assert before['history_ready']
    batch=before['message_batch']['batch_id']
    result=drive_reply(runtime,binding,batch,max_wait_seconds=30)
    assert len(result['sends'])==segments and all(item['local_send_observed'] for item in result['sends'])
    assert uis[0].actions.count('confirm_invite_once')==1
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==len(result['sends'])
    with SessionLocal() as db:
        assert db.get(Task,t['id']).result_code=='invite_sent'
        messages=list(db.scalars(select(MessageEvent).where(MessageEvent.sender_role=='self')))
        assert all(m.raw_payload['sender_source']=='ai' for m in messages)
    assert list((tmp_path/'coordinator'/'finished').glob('*.json'))
    # Advance only the due-check clock to exercise the real recall trigger.
    # All receiver/send/lease clocks remain real; no wait of 72 hours is claimed.
    from app.services import wechat_service
    from app.models.c3 import Conversation,MessageBatch
    from datetime import datetime,timedelta,timezone
    due_clock=(datetime.now(timezone.utc)+timedelta(days=4)).replace(hour=2,minute=0)
    monkeypatch.setattr(wechat_service,'utcnow',lambda:due_clock)
    next_sync=runtime.sync(binding)
    assert next_sync['history_ready'] and next_sync['message_batch'],next_sync
    recall_batch=next_sync['message_batch']['batch_id']
    with SessionLocal() as db:
        row=db.get(MessageBatch,recall_batch)
        assert row.trigger_type=='recall' and row.message_event_ids==[]
        assert db.get(Conversation,binding['conversation_id']).recall_count==0
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa'))
    requests=recovery_provider_factory('之前聊过购车的事情，您最近还有想了解的吗？可以按您的节奏慢慢聊。')
    monkeypatch.setattr(get_settings(),'c3_omniauto_root',str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa'))
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    recalled=drive_reply(runtime,binding,recall_batch,max_wait_seconds=30)
    confirmed=result_mode=='sent'
    if confirmed:
        assert len(recalled['sends'])==1 and recalled['sends'][0]['local_send_observed'],recalled
    else:
        # A missing immediate echo is pending, not yet an unknown settlement.
        # Obtain the original recovery identity from the actual work inventory.
        assert recalled['sends']==[]
        inventory=runtime.client.call(credentials,'POST',f"/workers/{w['id']}/wechat/native-work",
            {**runtime.account,'retained_flow_id':runtime.active_flow_id})
        assert len(inventory['retained_sends'])==1
        metadata=inventory['retained_sends'][0]
        from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
        dispatcher=HookBusinessDispatcher(runtime.client,tmp_path/'coordinator',factory,None,
            automatic_transcription_disabled=True)
        dispatcher.runtime=runtime
        assert dispatcher._recover_send(binding,metadata) is False
        from app.services import hook_send_service
        timeout_now=datetime.now(timezone.utc)+timedelta(seconds=get_settings().c3_send_ack_stale_after_seconds+1)
        monkeypatch.setattr(hook_send_service,'utcnow',lambda:timeout_now)
        assert dispatcher._recover_send(binding,metadata) is True
        runtime.finish(metadata['task_id'],metadata['flow_id'],metadata['lease_fencing_token'],binding['conversation_id'])
    with SessionLocal() as db:
        conv=db.get(Conversation,binding['conversation_id'])
        if confirmed:
            assert conv.status=='recalled_waiting_user'
            assert conv.next_recall_at is not None and conv.recall_cycle_id is None
        assert conv.recall_count==int(confirmed) and conv.recall_daily_count==int(confirmed)
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==2
    assert [p['model'] for p in requests]==['recovery-author','recovery-reviewer'],requests
    for request in requests:
        prompt,_=json.JSONDecoder().raw_decode(request['messages'][-1]['content'].lstrip())
        if request['model']=='recovery-author':
            intent=prompt['brain_input']['current_message']
            assert not intent['message_ids']
            assert prompt['brain_input']['conversation']['current_batch_text']==''
        else:intent=prompt['review_request']['reply_trigger']
        assert intent['trigger_type']=='recall' and intent.get('instruction')
    with SessionLocal() as db:
        action=db.scalar(select(ReplyAction).where(ReplyAction.batch_id==recall_batch))
        ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==action.id))
        saved=db.get(MessageBatch,recall_batch)
        events=list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_id==binding['conversation_id'])))
        task=db.get(Task,action.claimed_task_id)
        settlement=action.ai_payload['native_send_settlement']
        repeat_headers={'X-Inflight-Flow-Id':action.ai_payload['native_send_identity']['flow_id'],
            'X-Task-Lease-Fencing-Token':str(task.lease_fencing_token)}
        assert action.status==('sent' if confirmed else 'unknown_send_result')
        evidence={'external_model':'synthetic HTTP provider; actual Brain subprocess and reviewer',
            'wechat':'simulated Hook HTTP','conversation_id':binding['conversation_id'],
            'invite_task_id':t['id'],'welcome_batch_id':batch,'recall_batch_id':recall_batch,
            'recall_cycle_id':saved.recall_cycle_id,'reply_action_id':action.id,'task_id':action.claimed_task_id,
            'ack_id':ack.id,'action_status':action.status,'recall_count':conv.recall_count,
            'messages':[{'id':m.id,'native_id':m.raw_payload.get('native_msg_id'),'occurred_at':str(m.occurred_at)} for m in events]}
        (tmp_path/'recall-chain-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    response=post(w,f"/workers/{w['id']}/wechat/native-replies/{action.id}/settle",settlement['request'],repeat_headers)
    assert response.status_code==200 and response.json()['data']==settlement['result'],response.text
    with SessionLocal() as db:
        assert db.get(Conversation,binding['conversation_id']).recall_count==int(confirmed)
        assert len(list(db.scalars(select(SentAck).where(SentAck.reply_action_id==action.id))))==1
    again=drive_reply(runtime,binding,recall_batch,max_wait_seconds=5)
    assert not again['sends']
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==2
    assert all(server.lock_observations[i]['locked'] for i,r in enumerate(server.requests) if r[0]=='/SendTextMsg')
    table.close()
