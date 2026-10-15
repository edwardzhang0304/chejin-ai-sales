"""Official coordinator against HTTP backend/Hook simulator and durable SQLite.

UI/OCR replay and model mode are explicitly simulated. No live WeChat action.
"""
from dataclasses import replace
from types import SimpleNamespace
from copy import deepcopy
import json,sqlite3,time
from pathlib import Path
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.wechat import MessageEvent
from test_hook_business_http import clean,claimed,post
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from chejin_worker_client.hook_backend import HookWorkerCredentials, HookHeartbeatIdentity, HookHeartbeatState
from chejin_worker_client.hook_clock import HookBackendClock
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI


def calibrated_clock(identity_client, credentials, gateway, process, scope):
    """Give simulated prepared connections the authenticated clock production has."""
    receipt = identity_client.heartbeat(credentials,
        HookHeartbeatIdentity(scope.self_wxid, process.generation,
            gateway.deployment.revision, process.dll_sha256, account_epoch=scope.account_epoch),
        HookHeartbeatState('idle', 'ready', 'logged_in', None, None, '{}'))
    clock = HookBackendClock()
    clock.observe(receipt)
    return clock


def drive_reply(runtime, binding, batch_id, *, max_wait_seconds=30):
    """Bound only this test driver; all physical work uses the production entry."""
    deadline = time.monotonic() + max_wait_seconds
    sends = []
    last_task = None
    while time.monotonic() < deadline:
        data = runtime.batch_view(batch_id)
        if data.get('processing'):
            runtime.wait(.05)
            continue
        action = data.get('reply_action') or {}
        if action.get('status') != 'queued':
            if runtime.active_flow_id and (data.get('reply_sequence') or {}).get('terminal'):
                runtime.finish(last_task['id'], runtime.active_flow_id,
                               last_task['lease_fencing_token'], binding['conversation_id'])
            return {'decision': data.get('decision'), 'batch_id': batch_id, 'sends': sends}
        history = runtime.sync(binding, show_progress=False)
        reads = []
        original_sync = runtime.sync
        def checked_sync(*args, **kwargs):
            reads.append(kwargs)
            return original_sync(*args, **kwargs)
        runtime.sync = checked_sync
        try:
            result = runtime.reply_once(binding, {**data, 'batch_id': batch_id}, history=history)
        finally:
            runtime.sync = original_sync
        if result and result['status'] == 'sent':
            # Live dispatch supplies its admitted receipt. Physical send must
            # still perform the pre-send and post-send reads, exactly once each.
            assert len(reads) == 2
            assert 'reconciliation' not in reads[0]
            assert reads[1].get('reconciliation') is not None
        if result and result['status'] in {'sent', 'unknown_send_result'}:
            task = data['task']
            # The original persisted task provides the current fencing token.
            from app.core.database import SessionLocal
            from app.models.task import Task
            with SessionLocal() as db:
                row = db.get(Task, task['id'])
                last_task = {'id': row.id, 'lease_fencing_token': row.lease_fencing_token}
            sends.append({**result, 'local_send_observed': result['status'] == 'sent'})
            if result['status'] == 'unknown_send_result':
                return {'decision': 'unknown_send_result', 'batch_id': batch_id, 'sends': sends}
        runtime.wait(.05)
    raise AssertionError('Production reply entry did not settle within the test budget')


@pytest.mark.parametrize('segments',[1,2])
def test_coordinator_pulls_formal_invite_binds_history_generates_and_sends(identity_client,lab,tmp_path,monkeypatch,segments):
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
    result=drive_reply(runtime,binding,batch,max_wait_seconds=30)
    assert len(result['sends'])==segments and all(item['local_send_observed'] for item in result['sends'])
    assert uis[0].actions.count('confirm_invite_once')==1
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==len(result['sends'])
    with SessionLocal() as db:
        assert db.get(Task,t['id']).result_code=='invite_sent'
        messages=list(db.scalars(select(MessageEvent).where(MessageEvent.sender_role=='self')))
        assert all(m.raw_payload['sender_source']=='ai' for m in messages)
    assert list((tmp_path/'coordinator'/'finished').glob('*.json'))
    table.close()
