"""Continuous Task/Flow/history/send HTTP chain, simulated Windows/model only."""
from dataclasses import replace
from types import SimpleNamespace
import json,sqlite3,time
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.wechat import MessageEvent
from test_hook_business_http import clean,claimed,post
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from chejin_worker_client import storage
from chejin_worker_client.hook_clock import HookBackendClock
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
from chejin_worker_client.models import Binding
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI


@pytest.mark.parametrize('segments',[1,3])
def test_repeated_ticks_invite_bind_reply_recover_without_duplicate(identity_client,lab,tmp_path,monkeypatch,segments):
    if segments==3:
        from test_reply_sequence_http import SequenceModel
        from app.services import c3_service
        monkeypatch.setattr(c3_service,'get_ai_engine_adapter',SequenceModel)
    server,gateway,process,scope,*_=lab
    w,t,a=claimed(process_generation=process.generation,pending=True)
    scope=replace(scope,account_epoch=a['account_epoch'])
    credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    prepared=SimpleNamespace(process=process,registration=SimpleNamespace(scope=scope),account=None)
    client=HookFormalClient(identity_client.base_url,5)
    from chejin_worker_client.hook_backend import HookHeartbeatIdentity,HookHeartbeatState
    prepared.account=identity_client.heartbeat(credentials,HookHeartbeatIdentity(scope.self_wxid,process.generation,
        gateway.deployment.revision,process.dll_sha256,account_epoch=scope.account_epoch),HookHeartbeatState('idle','ready','logged_in',None,None,'{}'))
    prepared.clock=HookBackendClock()
    prepared.clock.observe(prepared.account)
    # Startup normalization is exercised by the original services/startup tests.
    # This business-chain fixture starts after that authoritative gate passed.
    services=SimpleNamespace(connection=SimpleNamespace(prepared=prepared,gateway=gateway),_credentials=credentials,
        startup_window_ready_for=lambda value:value is prepared)
    startup=[];uis=[]
    def factory(proc,prep,guard):
        class UI(ReplayUI):
            def __init__(self):super().__init__();self.clipboard_field_events=[]
            def capture(self,name):
                guard();frame=super().capture(name)
                if name=='lookup':
                    for row in frame.rows:row['text']=row['text'].replace('13800000003',prep['primary_phone'])
                return frame
            def replace(self,frame,text):
                guard();super().replace(frame,text)
                if frame.name.startswith('fields'):self.clipboard_field_events.append({'copy_calls':1,'paste_calls':1,'clipboard_match':True,'completion_guard_passed':True})
        ui=UI();uis.append(ui);return ui
    import threading
    runner=SimpleNamespace(binding=Binding(w['id'],w['worker_token'],w['instance'],'running'),stop_event=threading.Event(),api=SimpleNamespace(inflight_flow_id=None),current_task=None,
        on_task=lambda task:None,can_pull_tasks=lambda:True,_can_start_new_flow=lambda:not storage.load_runtime_control().get('inflight_flow_id'),
        _worker_transaction_barrier_ready=lambda *a,**kw:True)
    dispatcher=HookBusinessDispatcher(client,tmp_path/'continuous',factory,lambda *args:startup.append(1),automatic_transcription_disabled=lambda:True)
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'continuous-handle'}]
    server.query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
    def on_request(path):
        server.raw_override=None
        if path=='/GetFriendAndChatRoomList':
            prep=json.loads(next((tmp_path/'continuous'/'invites').glob('*/*/preparation.json')).read_text())
            server.raw_override=json.dumps({'code':0,'data':[{'wxid':'wxid_customer_A','markname':prep['remark_code']}]}).encode()
        if path=='/SendTextMsg':
            text=server.requests[-1][1]['msg'];now=int(time.time());seq=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1
            table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(seq,str(8000000000000000000+seq),1,1,now,now*1000+seq,0,seq,'wxid_customer_A',text))
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    dispatcher.tick(runner,services) # Pull and original C1 once.
    dispatcher.tick(runner,services) # Proved invitation, unique shortcode discovery.
    for _ in range(6):dispatcher.tick(runner,services)
    assert len(uis)==1 and uis[0].actions.count('confirm_invite_once')==1
    assert len([x for x in server.requests if x[0]=='/SendTextMsg'])==segments
    with SessionLocal() as db:
        assert db.get(Task,t['id']).result_code=='invite_sent'
        assert len(list(db.scalars(select(MessageEvent).where(MessageEvent.sender_role=='self'))))==segments
    assert not storage.load_runtime_control().get('inflight_flow_id')
    # Original GUI pause is honored: no pending C1 and no new physical action.
    post(w,f"/workers/{w['id']}/run-status",{'run_status':'paused'})
    runner.binding.run_status='paused'
    for _ in range(3):dispatcher.tick(runner,services)
    assert len([x for x in server.requests if x[0]=='/SendTextMsg'])==segments
    table.close()
