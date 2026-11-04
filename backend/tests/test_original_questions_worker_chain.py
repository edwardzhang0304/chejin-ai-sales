"""Original two customer utterances, synthetic cars/model/Hook; actual Worker chain."""
from dataclasses import replace
from types import SimpleNamespace
import json
import sqlite3
import time
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import MessageBatch, ReplyAction
from app.models.task import Task
from app.models.wechat import MessageEvent
from test_hook_business_http import clean
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_model_query_hook_pipeline import actual_brain
import test_catalog_brain_joined as joined
import test_hook_formal_runtime as formal
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from apps.wechat_ai_customer_service.tests.test_hook_c1_flow import ReplayUI


@pytest.mark.parametrize('case',['budget','year'])
def test_original_questions_run_through_worker_and_durable_recovery(identity_client,lab,tmp_path,monkeypatch,actual_brain,case):
    state=actual_brain
    state['scenario']='case_budget_timeout' if case=='budget' else 'case_energy'
    server,gateway,process,scope,*_=lab
    w,t,account=formal.claimed(process_generation=process.generation,pending=True)
    scope=replace(scope,account_epoch=account['account_epoch'])
    credentials=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    prepared=SimpleNamespace(process=process,registration=SimpleNamespace(scope=scope),
        clock=formal.calibrated_clock(identity_client,credentials,gateway,process,scope))
    runtime=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),credentials,gateway,prepared,tmp_path/'runtime')
    def factory(prep,guard):
        class UI(ReplayUI):
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
        return UI()
    invited=runtime.invite(t,factory);assert invited['settlement']['task']['result_code']=='invite_sent'
    code=invited['preparation']['remark_code'];peer='syntheticcustomer26'
    table=sqlite3.connect(':memory:',check_same_thread=False);table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'synthetic-original-cases'}]
    server.query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
    def append(text,sender):
        created=int(time.time());seq=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1
        table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(seq,str(8000000000000000000+seq),1,sender,created,created*1000+seq,0,seq,peer,text))
    def on_request(path):
        server.raw_override=None
        if path=='/GetFriendAndChatRoomList':server.raw_override=json.dumps({'code':0,'data':[{'wxid':peer,'markname':code}]}).encode()
        if path=='/SendTextMsg':
            append(server.requests[-1][1]['msg'],1);server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    binding=runtime.discover(t['id'])
    for i in range(6 if case=='budget' else 3):
        vid=joined.api._create_listed_vehicle()
        response=joined.api.client.put(f'/api/vehicles/{vid}',json={'display_name':f'合成车{i+1}','public_price':f'{5.2+i*.4:.2f}','energy_type':'electric'},headers=joined.api.HEADERS)
        assert response.status_code==200,response.text
    # Establish the actual initial history boundary before the new customer message.
    baseline=runtime.sync(binding);assert baseline['history_ready']
    with SessionLocal() as db:
        welcome=db.get(MessageBatch,baseline['message_batch']['batch_id'])
        assert welcome.trigger_type=='friend_welcome'
        assert welcome.ai_request_snapshot['model_query_contract']=='vehicle_need_v2'
    # Hook CreateTime has second precision: the new utterance occurs after binding.
    time.sleep(1.05)
    first_question='5—8万有什么车' if case=='budget' else '有新能源吗'
    append(first_question,0)
    history=runtime.sync(binding);assert history['message_batch'],history
    bid=history['message_batch']['batch_id']
    first=formal.drive_reply(runtime,binding,bid,max_wait_seconds=45)
    assert first['decision']=='send_reply' and len(first['sends'])==1
    if case=='year':
        state['scenario']='case_year'
        append('有年份近一点的吗',0)
        history=runtime.sync(binding);bid=history['message_batch']['batch_id']
        second=formal.drive_reply(runtime,binding,bid,max_wait_seconds=30)
        assert second['decision']=='send_reply' and len(second['sends'])==1
        query=state['receipts'][-1]['query']
        assert query['unsupported_requirements'] and query['sort_origin']=='program_default' and query['sort']=='price_asc'
        assert not state['drafts'][-1]['facts_claimed']
        assert '还没核实' in state['drafts'][-1]['reply_segments'][0]
    else:
        claims=[r['brain_plan_summary']['facts_claimed'] for r in state['reviews']]
        assert [len(c) for c in claims]==[6,9,9],claims
        assert [float(c['price']) for c in state['receipts'][-1]['recommendations']]==[6.4,6.8,6.0,7.2,5.6,5.2]
        assert claims[1]==state['drafts'][1]['facts_claimed']
    with SessionLocal() as db:
        batch=db.get(MessageBatch,bid);snap=batch.ai_response_snapshot
        assert batch.ai_request_snapshot['model_query_contract']=='vehicle_need_v2'
        assert batch.generation_attempt_count==(2 if case=='budget' else 1)
        assert snap['generation_accounting_totals']['call_counts_complete']
        actions=list(db.scalars(select(ReplyAction).where(ReplyAction.batch_id==bid)))
        assert len(actions)==1 and actions[0].status=='sent'
        assert db.scalar(select(Task).where(Task.reply_action_id==actions[0].id)).status=='completed'
        stored={'case':case,'batch':bid,'snapshot':snap,'reviews':state['reviews'],'drafts':state['drafts']}
        (tmp_path/'result.json').write_text(json.dumps(stored,ensure_ascii=False,default=str))
        if case=='budget':
            first_audit=snap['generation_attempt_history'][0]['response']['raw_payload']['omniauto_brain_result']['model_call_accounting']
            assert sum(c['stage']=='brain_quality_repair' for c in first_audit['logical_calls'])==1
            assert first_audit['http_attempts'][-1]['failure_category']=='service_unavailable'
        else:
            model=snap['raw_payload']['omniauto_brain_result']
            assert model['catalog_stages']['understanding']['json_structure_repaired']
        sent=list(db.scalars(select(MessageEvent).where(MessageEvent.sender_role=='self')))
        assert all(m.raw_payload['sender_source']=='ai' for m in sent)
    assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==(1 if case=='budget' else 2)
    assert list((tmp_path/'runtime'/'finished').glob('*.json'))
    table.close()
