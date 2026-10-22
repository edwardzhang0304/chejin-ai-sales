"""Full Worker/HTTP/SQLite path; Hook and model simulated, no real WeChat/Feishu."""
import sqlite3,time
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import HandoffEvent,ReplyAction
from app.models.worker import Worker
from app.models.task import Task
from app.services import c3_service
from app.services.ai_adapter import AIEngineDecision
from test_hook_business_http import clean
from test_hook_history_business_http import bound
from test_hook_friend_verification import NOTICE,REASON,notifications
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_callback_recovery import connection_factory
from test_hook_formal_runtime import drive_reply
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime


@pytest.mark.parametrize('zero_id',[False,True])
def test_friend_verification_after_first_segment_keeps_original_journal_and_stops_only_customer(
        identity_client,lab,connection_factory,monkeypatch,tmp_path,zero_id):
    class Model:
        def generate_reply_decision(self,**kwargs):
            parts=['车辆需要结合实际检测结果核实，'*4+'我们会说明已知情况。',
                   '请结合日常通勤情况选择车型，'*4+'我们可以一起核实需求。']
            return AIEngineDecision(decision='send_reply',reply_text=' '.join(parts),guard_result='pass',
                raw_payload={'omniauto_brain_result':{'brain_plan':{'reply_segments':parts}}})
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',Model)
    w,_,_,binding=bound();sent=notifications(w,monkeypatch)
    server,gateway,*_=lab
    manager,credentials,prepared=connection_factory(w)
    runtime=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),credentials,gateway,prepared,tmp_path/'runtime')
    from chejin_worker_client.hook_progress import HookProgressTimeline
    from types import SimpleNamespace
    timeline=HookProgressTimeline();runtime.lifecycle_runner=SimpleNamespace(on_runtime_process=timeline.apply,api=SimpleNamespace(),
        _can_start_new_flow=lambda:True,binding=SimpleNamespace(run_status='running'))
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'friend-notice-test'}]
    server.query_response=lambda body:{'data':[dict(r) for r in table.execute(body['sql']).fetchall()]}
    while int(time.time())<=int(binding['history_since_create_time']):time.sleep(.02)
    def add(kind,sender,text):
        n=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+9;now=int(time.time())
        table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
            (n,str(8200000000000000000+n),kind,sender,now,now*1000+n,0,n,binding['peer_wxid'],text))
    add(1,0,'想了解二手车的选购方式，暂时不预约。')
    restricted_peer=binding['peer_wxid']
    def hook(path):
        server.raw_override=None
        if path=='/SendTextMsg':
            add(1,1,server.requests[-1][1]['msg'])
            if binding['peer_wxid']==restricted_peer:
                if zero_id:table.execute('UPDATE MSG SET MsgSvrID=0 WHERE IsSender=1 AND StrTalker=?',(restricted_peer,))
                add(10000,0,NOTICE)
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=hook
    observations=[];original_observe=HookFormalClient.observe_send
    def observe(*args):
        result=original_observe(*args);observations.append(result);return result
    monkeypatch.setattr(HookFormalClient,'observe_send',observe)
    first=runtime.sync(binding)
    result=drive_reply(runtime,binding,first['message_batch']['batch_id'])
    assert len(result['sends'])==1
    assert observations and not observations[0]['deadline_elapsed']
    if zero_id:assert observations[0]['early_settlement_reason']==REASON
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1
    assert not runtime.active_flow_id
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction).order_by(ReplyAction.segment_index)))
        assert len(actions)==2 and actions[0].status==('unknown_send_result' if zero_id else 'sent') and actions[1].status=='cancelled'
        cancelled=db.scalar(select(Task).where(Task.reply_action_id==actions[1].id))
        assert cancelled.cancel_reason==REASON
        original_action=actions[0].id
        assert db.get(Worker,w['id']).run_status=='running'
        assert not db.get(Worker,w['id']).inflight_flow_state
        assert db.scalar(select(HandoffEvent)).handoff_reason_code==REASON
    confirm=timeline.latest[(result['sends'][0]['task_id'],'send_confirm')]
    assert confirm['state']=='done'
    assert confirm['title']==('发送结果核对已结束' if zero_id else '确认发送结果')
    assert runtime._send_result_pending is False
    files=list((runtime.private_root/'sends').rglob('trigger.json'));assert len(files)==1
    folder=files[0].parent;before={p.name:p.read_bytes() for p in folder.iterdir()}
    for _ in range(2):assert runtime.sync(binding)['message_batch'] is None
    assert before=={p.name:p.read_bytes() for p in folder.iterdir()}
    assert len(sent)==1
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==1
    assert (runtime.private_root/'send-receipts'/f'{original_action}.json').exists()
    if zero_id:
        # A different customer's complete real task/flow/reply chain must work
        # after the restricted send closes, without resetting Worker or journals.
        from test_hook_account_http import client
        from test_hook_business_http import post,proof,body
        lead=client.post('/api/leads',json={'customer_name':'Synthetic healthy customer','phones':['13800000004']})
        assert lead.status_code==200,lead.text
        pulled=client.get(f"/api/workers/{w['id']}/tasks/pull",headers={
            'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
        task=pulled.json()['data']['task'];assert task['business_object']['lead']['id']==lead.json()['data']['id']
        assert post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':task['id'],'flow_kind':'task'}).status_code==200
        headers={'X-Inflight-Flow-Id':task['id']}
        claimed=post(w,f"/tasks/{task['id']}/claim",{'worker_id':w['id']},headers)
        assert claimed.status_code==200,claimed.text
        headers['X-Task-Lease-Fencing-Token']=str(claimed.json()['data']['lease_fencing_token'])
        account={k:getattr(runtime.scope,k) for k in ('self_wxid','account_epoch','process_generation')}
        prep=post(w,f"/tasks/{task['id']}/hook-invite/prepare",account,headers).json()['data']
        submitted=post(w,f"/tasks/{task['id']}/hook-invite/submitted",proof(account,prep),headers)
        assert submitted.status_code==200,submitted.text
        assert post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':task['id'],'terminal_kind':'task_terminal'},headers).status_code==200
        bound_result=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(task,account,prep,('wxid_customer_B',)))
        assert bound_result.status_code==200,bound_result.text
        binding=bound_result.json()['data']
        while int(time.time())<=int(binding['history_since_create_time']):time.sleep(.02)
        add(1,0,'请介绍一下选购方式。')
        synced=runtime.sync(binding);assert synced['message_batch']
        healthy=drive_reply(runtime,binding,synced['message_batch']['batch_id'])
        assert len(healthy['sends'])==2 and not runtime.active_flow_id
        assert all(item['status']=='sent' for item in healthy['sends'])
        assert before=={p.name:p.read_bytes() for p in folder.iterdir()}
        assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==3
        with SessionLocal() as db:
            assert db.get(ReplyAction,original_action).status=='unknown_send_result'
            assert db.get(Worker,w['id']).run_status=='running'
            assert not db.get(Worker,w['id']).inflight_flow_state
    manager.close();table.close()
