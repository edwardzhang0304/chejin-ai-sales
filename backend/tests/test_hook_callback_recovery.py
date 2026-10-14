from test_hook_formal_runtime import drive_reply
"""Official coordinator against HTTP backend/Hook simulator and durable SQLite.

UI/OCR replay and model mode are explicitly simulated. No live WeChat action.
"""
from dataclasses import replace
from types import SimpleNamespace
from copy import deepcopy
import json,sqlite3,time,uuid
from pathlib import Path
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.wechat import MessageEvent
from test_hook_business_http import clean,claimed,post
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI


@pytest.mark.parametrize('mode',['single_callback','retained_send','capacity_reconcile'])
def test_callback_during_post_send_scan_and_retained_settlement(identity_client,lab,tmp_path,monkeypatch,mode):
    segments=1
    if segments==2:
        from app.services import c3_service
        from app.services.ai_adapter import AIEngineDecision
        class Model:
            def generate_reply_decision(self,**kwargs):
                parts=['这台车的资料需要按实际检测结果核实，'*3+'我们会说明已知的情况。', '另一台车也需要核实使用情况，'*4+'不能保证没有未发现的问题。']
                return AIEngineDecision(decision='send_reply',reply_text=' '.join(parts),guard_result='pass',raw_payload={'omniauto_brain_result':{'brain_plan':{'reply_segments':parts}}})
        monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Model)
    server,gateway,process,scope,*_=lab
    w,t,account=claimed(process_generation=process.generation,pending=True)
    scope=replace(scope,account_epoch=account['account_epoch'])
    prepared=SimpleNamespace(process=process,registration=SimpleNamespace(scope=scope))
    runtime=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),
        HookWorkerCredentials(w['id'],w['worker_token'],w['instance']),gateway,prepared,tmp_path/'coordinator')
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
            table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(seq,str(8000000000000000000+seq),1,1,created,created*1000+seq,0,seq,'wxid_customer_A',text))
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    binding=runtime.discover(t['id'])
    before=runtime.sync(binding);assert before['history_ready']
    batch=before['message_batch']['batch_id']
    from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
    original_scan=HookHistorySynchronizer.scan
    original_barrier=runtime.barrier
    changes=[0]
    def barrier(binding=None):
        value=original_barrier(binding);return (value[0]+changes[0],value[1])
    runtime.barrier=barrier
    def scan(*args,**kwargs):
        result=original_scan(*args,**kwargs)
        # The Hook call has happened and its row has landed. Emulate a callback
        # crossing the scan barrier once, or on all bounded attempts.
        if any(r[0]=='/SendTextMsg' for r in server.requests) and (mode in {'retained_send','capacity_reconcile'} or changes[0]==0):
            changes[0]+=1
        return result
    monkeypatch.setattr(HookHistorySynchronizer,'scan',scan)
    if mode=='single_callback':
        result=drive_reply(runtime,binding,batch,max_wait_seconds=30)
        assert len(runtime.history_retry_events)==1
    else:
        with pytest.raises(ValueError,match='HOOK_HISTORY_CALLBACK_BARRIER_CHANGED'):
            drive_reply(runtime,binding,batch,max_wait_seconds=30)
        assert len(runtime.history_retry_events)==3
        assert not list((tmp_path/'coordinator'/'send-receipts').glob('*.json'))
        assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1
        from app.models.c3 import ReplyAction,SentAck
        with SessionLocal() as db:
            action=db.scalar(select(ReplyAction));claimed_task=db.get(Task,action.claimed_task_id)
            identity=action.ai_payload['native_send_identity'];attempt=action.ai_payload['native_send_attempt']
            assert action.status=='sending'
            metadata={k:identity[k] for k in ('task_id','reply_action_id','flow_id','lease_fencing_token')}
            metadata['journal_sha256']=attempt['journal_sha256']
            claimed_task.lease_expires_at=__import__('datetime').datetime(2020,1,1,tzinfo=__import__('datetime').timezone.utc);db.commit()
        post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'paused'})
        retained=list((tmp_path/'coordinator'/'sends').rglob('*.json'))
        import hashlib
        hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in retained}
        monkeypatch.setattr(HookHistorySynchronizer,'scan',original_scan)
        recovery=HookFormalRuntime(runtime.client,runtime.credentials,gateway,prepared,tmp_path/'recovery')
        if mode=='capacity_reconcile':
            recovery.history_budget.maximum_bytes=16
            recovery.history_budget.save(tmp_path/'recovery/history-captures'/('a'*64+'.json'),{'synthetic':'occupied regular pool'},settlement=True)
            recovery.history_budget.begin_capture(allow_retire=False)
            assert not recovery.history_budget.ready
        post(w,f"/workers/{w['id']}/run-status",{'client_instance_id':w['instance'],'run_status':'running'})
        receipt=recovery.settle_retained_send(binding,tmp_path/'coordinator',metadata)
        assert receipt['local_send_observed'] and receipt['native_msg_id']
        assert recovery.settle_retained_send(binding,tmp_path/'coordinator',metadata)==receipt
        if mode=='capacity_reconcile':
            assert list((tmp_path/'recovery/history-reconcile/history-captures').glob('*.json'))
            assert not recovery.history_budget.ready
            with pytest.raises(ValueError,match='HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED'):
                recovery.start_flow({'id':str(uuid.uuid4())})
        recovery.finish(metadata['task_id'],metadata['flow_id'],metadata['lease_fencing_token'],binding['conversation_id'])
        assert all(hashlib.sha256(p.read_bytes()).hexdigest()==v for p,v in hashes.items())
        assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1
        with SessionLocal() as db:
            assert db.get(Task,metadata['task_id']).status=='completed'
            assert len(list(db.scalars(select(SentAck).where(SentAck.send_result=='sent'))))==1
        result={'sends':[{'local_send_observed':True}]}

    assert len(result['sends'])==segments and all(item['local_send_observed'] for item in result['sends'])
    assert uis[0].actions.count('confirm_invite_once')==1
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==len(result['sends'])
    with SessionLocal() as db:
        assert db.get(Task,t['id']).result_code=='invite_sent'
        messages=list(db.scalars(select(MessageEvent).where(MessageEvent.sender_role=='self')))
        assert all(m.raw_payload['sender_source']=='ai' for m in messages)
    assert list((tmp_path/'coordinator'/'finished').glob('*.json'))
    table.close()
