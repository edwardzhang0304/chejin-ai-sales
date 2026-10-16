"""Independent segment-boundary review. Real services/Brain/Worker; synthetic Hook/model/audio result."""
import json, sqlite3, time
from pathlib import Path
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.core.config import get_settings
from app.models.c3 import ReplyAction, SentAck, HandoffEvent
from app.models.task import Task
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_hook_business_http import clean, post
from test_hook_history_business_http import bound
from test_hook_native_media import voice_payload
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_hook_callback_recovery import connection_factory
from test_hook_formal_runtime import drive_reply
from test_partial_reply_recovery_provider import controlled_recovery_provider
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime

@pytest.mark.parametrize('state',['uncertain','failed','transcribed'])
@pytest.mark.parametrize('new_question',[False,True])
def test_late_archival_result_between_segments_preserves_only_valid_reply(identity_client,lab,connection_factory,monkeypatch,tmp_path,state,new_question):
    w,_,_,binding=bound()
    server,gateway,*_=lab
    manager,credentials,prepared=connection_factory(w)
    client=HookFormalClient(identity_client.base_url,5)
    runtime=HookFormalRuntime(client,credentials,gateway,prepared,tmp_path/'runtime')
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'independent-segments'}]
    server.query_response=lambda body:{'data':[dict(r) for r in table.execute(body['sql']).fetchall()]}
    while int(time.time())<=int(binding['history_since_create_time']):time.sleep(.02)
    def append(kind,sender,text):
        n=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1;now=int(time.time());mid=str(8200000000000000000+n)
        table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(n,mid,kind,sender,now,now*1000+n,0,n,binding['peer_wxid'],text))
        return mid
    append(3,0,'<msg><img md5="synthetic"/></msg>')
    assert runtime.sync(binding)['history_ready']
    voice=append(34,0,'<msg><voicemsg length="43"/></msg>')
    assert runtime.sync(binding)['reason_code']=='HOOK_VOICE_PENDING'
    append(1,1,'我是销售，之前的问题已处理')
    append(1,0,'现在想了解十五万元车型的挑选方式，先不预约。')
    incoming=runtime.sync(binding);bid=incoming['message_batch']['batch_id']
    late=voice_payload(w,runtime.account,binding,voice,state)
    deliveries=[];settled=[]
    original=HookFormalClient.call
    def observed(self,*args,**kwargs):
        value=original(self,*args,**kwargs)
        if args[1]=='POST' and args[2].endswith('/settle'):
            settled.append(value)
            if len(settled)==1:
                r=post(w,f"/workers/{w['id']}/wechat/native-voice/result",late)
                assert r.status_code==200 and not r.json()['data']['history_coverage_required'],r.text
                deliveries.append(r.json()['data'])
                if new_question:append(1,0,'先别介绍车型了，我想问置换流程。')
        return value
    monkeypatch.setattr(HookFormalClient,'call',observed)
    def hook(path):
        server.raw_override=None
        if path=='/SendTextMsg':
            append(1,1,server.requests[-1][1]['msg']);server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=hook
    engine=str(Path(__file__).resolve().parents[2]/'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT',engine);monkeypatch.setattr(get_settings(),'c3_omniauto_root',engine)
    monkeypatch.setattr(c3_service,'get_ai_engine_adapter',RealOmniAutoAIEngineAdapter)
    answers=['可以先按预算筛选车型，再结合日常通勤、乘坐人数和停车条件缩小范围。具体车况还需要查看实际检测记录，我们会说明已知情况。',
             '试车时可以留意驾驶感受和空间是否合适，维修保养记录也值得核实。您目前更喜欢轿车还是SUV，我们可以先从您的使用需求聊起。']
    requests=[]
    with controlled_recovery_provider(monkeypatch,answers,requests):
        result=drive_reply(runtime,binding,bid)
    expected=1 if new_question else 2
    with SessionLocal() as db:
        actions=list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id==bid).order_by(ReplyAction.segment_index)))
        evidence={'state':state,'new_question':new_question,'batch_id':bid,'sends':result['sends'],
            'actions':[{'id':a.id,'status':a.status,'segment':a.segment_index,'task_id':a.claimed_task_id,
                'native_id':(a.ai_payload.get('native_send_settlement') or {}).get('result',{}).get('native_msg_id')} for a in actions],
            'acks':len(list(db.scalars(select(SentAck)))),'provider_models':[r['model'] for r in requests],
            'open_handoffs':sum(h.closed_at is None for h in db.scalars(select(HandoffEvent)))}
        (tmp_path/'segment-chain.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
        assert len(actions)==2,evidence
        assert evidence['acks']==expected and len(result['sends'])==expected,evidence
        assert evidence['open_handoffs']==0 and len(deliveries)==1,evidence
        if not new_question:assert all(a.status=='sent' and db.get(Task,a.claimed_task_id).status=='completed' for a in actions),evidence
        else:assert actions[0].status=='sent' and actions[1].status!='sent',evidence
    assert [r['model'] for r in requests]==['recovery-author','recovery-reviewer']
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==expected
    replay=post(w,f"/workers/{w['id']}/wechat/native-voice/result",late)
    assert replay.status_code==200 and replay.json()['data']==deliveries[0]
    assert not drive_reply(runtime,binding,bid,max_wait_seconds=5)['sends']
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==expected
    for p in (tmp_path/'runtime/sends').rglob('intent.json'):
        assert .5<=json.loads(p.read_text())['delay_seconds']<=3
    assert all(server.lock_observations[i]['locked'] for i,r in enumerate(server.requests) if r[0]=='/SendTextMsg')
    table.close()
