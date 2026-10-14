"""Formal backend and Worker over HTTP + SQLite Hook simulator; Windows UI replay.

This proves code wiring, not real DLL delivery, native clipboard or live model.
"""
from datetime import datetime, timezone
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
import uuid
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import SentAck
from app.models.task import Task
from app.services import c3_service
from test_hook_business_http import clean, claimed
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from chejin_worker_client import ui_lock
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_c1 import HookFormalC1, _save
from chejin_worker_client.hook_history_sync import HookHistorySynchronizer
from chejin_worker_client.hook_outbox import HookDeliveryScope
from chejin_worker_client.hook_sender import HookNativeSender, NativeSendPermit
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI


def test_formal_c1_binding_original_ai_native_send_history_receipt(identity_client,lab,tmp_path):
    server,gateway,process,scope,*_=lab
    w,t,p,h,initial=claimed(process_generation=process.generation)
    api=HookFormalClient(identity_client.base_url,5)
    credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    c1=HookFormalC1(api,credentials,tmp_path/'formal')
    token=int(h['X-Task-Lease-Fencing-Token']);prep=c1.prepare(t['id'],token,p)
    class ClipboardReplay(ReplayUI):
        def __init__(self):super().__init__();self.clipboard_field_events=[]
        def replace(self,frame,text):
            super().replace(frame,text)
            if frame.name.startswith('fields'):
                self.clipboard_field_events.append({'copy_calls':1,'paste_calls':1,'clipboard_match':True,'completion_guard_passed':True})
        def capture(self,name):
            frame=super().capture(name)
            if name=='lookup':
                for row in frame.rows:row['text']=row['text'].replace('13800000003',prep['primary_phone'])
            return frame
    ui=ClipboardReplay()
    lease=ui_lock.acquire_ui_lock(operation_type='add_friend',owner='formal-test-c1',current_step='formal-invite')
    try:
        assert c1.execute_prepared(t['id'],token,prep,lambda:ui)['formal_submission_proof_saved']
    finally:lease.release()
    assert c1.settle(t['id'],token)['task']['result_code']=='invite_sent'
    api.call(credentials,'POST',f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':t['id'],'terminal_kind':'task_terminal'},task_id=t['id'],token=token)
    binding=api.bind_friend(credentials,t['id'],p,{'code':0,'data':[{'wxid':'wxid_customer_A','markname':prep['remark_code']}]})
    assert binding['source_task_id']==t['id']
    scope=replace(scope,account_epoch=p['account_epoch'])
    delivery=HookDeliveryScope(w['id'],w['instance'],scope.self_wxid,scope.account_epoch,binding['binding_id'],1,binding['peer_wxid'])
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,'
        'CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'test-native-handle'}]
    server.query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
    def save_capture(capture):
        _save(tmp_path/'captures'/f'{uuid.uuid4()}.json',{'payload':capture.payload,'kind':capture.query.kind})
    sync=HookHistorySynchronizer(gateway,save_capture,lambda:(0,0))
    capture,value=sync.capture(process,scope,delivery,binding['history_since_create_time'])
    observed=api.history_coverage(credentials,p,binding,capture,value)
    assert observed['history_ready']
    with SessionLocal() as db:
        generated=c3_service.generate_for_batch(db,batch_id=observed['message_batch']['batch_id'],native_client_admitted=True);db.commit()
    assert generated['decision']=='send_reply'
    task_id=generated['task_id'];action_id=generated['reply_action_id']
    api.call(credentials,'POST',f"/workers/{w['id']}/inflight-flow/start",{'flow_id':task_id,'flow_kind':'chat_reply','conversation_id':binding['conversation_id']})
    task=api.call(credentials,'POST',f'/tasks/{task_id}/claim',{'worker_id':w['id'],'claim_source':'hook_conversation_flow','conversation_id':binding['conversation_id']},task_id=task_id)
    permit=NativeSendPermit.capture(api.native_send_claim(credentials,p,task_id,action_id,task['lease_fencing_token'],observed['history_state_digest']))
    def send_response(path):
        if path=='/SendTextMsg':
            text=server.requests[-1][1]['msg'];created=int(time.time())
            table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(1,'8000000000000000001',1,1,created,created*1000,0,1,binding['peer_wxid'],text))
            server.raw_override=b'{"SendTextMsg":"1"}'
        else:server.raw_override=None
    server.on_request=send_response
    class Refresh:
        def barrier(self):return (0,0)
        def __call__(self,permit):
            capture,value=sync.capture(process,scope,delivery,binding['history_since_create_time'])
            current=api.history_coverage(credentials,p,binding,capture,value)
            assert current['history_ready'] and current['history_state_digest']==permit.history_state_digest
            return self.barrier()
    sender=HookNativeSender(gateway,api,credentials,tmp_path/'sending',delay=lambda:1.1)
    outcome=sender.execute(permit,process,scope,Refresh());assert outcome['call_completed']
    capture,value=sync.capture(process,scope,delivery,binding['history_since_create_time'])
    after=api.history_coverage(credentials,p,binding,capture,value)
    receipt=api.native_send_settle(credentials,permit,outcome)
    assert receipt['local_send_observed'] and receipt['native_msg_id']=='8000000000000000001'
    assert not receipt['automatic_resend_allowed']
    assert api.native_send_settle(credentials,permit,outcome)==receipt
    assert len([request for request in server.requests if request[0]=='/SendTextMsg'])==1
    assert ui.actions.count('confirm_invite_once')==1 and len(ui.clipboard_field_events)==2
    with SessionLocal() as db:
        assert db.get(Task,t['id']).result_code=='invite_sent'
        assert db.get(Task,task_id).result_code=='chat_reply_sent'
        assert db.scalar(select(SentAck).where(SentAck.reply_action_id==action_id))
    api.call(credentials,'POST',f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':task_id,'terminal_kind':'task_terminal','conversation_id':binding['conversation_id']},task_id=task_id,token=permit.lease_fencing_token)
    with pytest.raises(ValueError,match='PREVIOUS_ATTEMPT'):sender.execute(permit,process,scope,Refresh())
    table.close()
