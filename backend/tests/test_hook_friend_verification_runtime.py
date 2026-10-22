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
    def hook(path):
        server.raw_override=None
        if path=='/SendTextMsg':
            add(1,1,server.requests[-1][1]['msg'])
            if zero_id:table.execute('UPDATE MSG SET MsgSvrID=0 WHERE IsSender=1')
            add(10000,0,NOTICE)
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=hook
    if zero_id:
        # Move only the server confirmation clock after the real post-send
        # history read. This exercises reply_once's terminal-unknown entry.
        from app.services import hook_send_service
        from datetime import timedelta
        observe=HookFormalClient.observe_send
        def after_deadline(*args,**kwargs):
            now=hook_send_service.utcnow()
            monkeypatch.setattr(hook_send_service,'utcnow',lambda:now+timedelta(seconds=301))
            return observe(*args,**kwargs)
        monkeypatch.setattr(HookFormalClient,'observe_send',after_deadline)
    first=runtime.sync(binding)
    result=drive_reply(runtime,binding,first['message_batch']['batch_id'])
    assert len(result['sends'])==1
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
    manager.close();table.close()
