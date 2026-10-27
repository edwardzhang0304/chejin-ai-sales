"""Real formal HTTP/SQLite/Worker sender closure; Hook/window/model simulated.

No live WeChat, external AI or Feishu. Callback facts enter NativeInbox; human
ownership is applied by its real rule to a synthetic verified sales message.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import json
import sqlite3
import time
import uuid
import pytest
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models.c3 import Conversation, ReplyAction, SentAck
from app.models.task import Task
from app.models.worker import Worker
from app.models.wechat import MessageEvent
from app.services import c3_service, reply_ownership
from test_hook_business_http import clean, post
from test_hook_history_business_http import bound
from test_hook_account_client import identity_client
from test_hook_formal_runtime import calibrated_clock
from test_hook_gateway import lab
from chejin_worker_client.hook_backend import HookWorkerCredentials
from chejin_worker_client.hook_formal_client import HookFormalClient
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime
from chejin_worker_client.hook_inbox import NativeInbox
from chejin_worker_client.hook_sender import NativeSendPermit
from chejin_worker_client.api import ApiError


@pytest.fixture
def case(identity_client,lab,tmp_path,request,monkeypatch):
    mode=getattr(request,'param',None)
    if mode=='catalog':
        from test_catalog_fact_references import CatalogAdapter
        monkeypatch.setattr(c3_service,'get_ai_engine_adapter',CatalogAdapter)
    elif mode=='boundary':
        from app.services.ai_adapter import AIEngineDecision
        class BoundaryModel:
            def generate_reply_decision(self,**kwargs):
                return AIEngineDecision(decision='reply_then_handoff',reply_text='好的，我们会结合您的实际需求核实车辆资料。',
                    guard_result='pass',handoff_reason_code='CUSTOMER_HIGH_INTENT')
        monkeypatch.setattr(c3_service,'get_ai_engine_adapter',BoundaryModel)
    w,_,account,binding=bound()
    server,gateway,process,scope,*_=lab
    scope=replace(scope,account_epoch=account['account_epoch'])
    creds=HookWorkerCredentials(w['id'],w['worker_token'],w['instance'])
    prepared=NS(process=process,registration=NS(scope=scope),
        clock=calibrated_clock(identity_client,creds,gateway,process,scope))
    rt=HookFormalRuntime(HookFormalClient(identity_client.base_url,5),creds,gateway,prepared,tmp_path/'transactions'/'hook',wait=lambda _:None)
    db=sqlite3.connect(':memory:',check_same_thread=False);db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'refusal-fixture'}]
    server.query_response=lambda body:{'data':[dict(r) for r in db.execute(body['sql']).fetchall()]}
    history=rt.sync(binding)
    bid=history['message_batch']['batch_id']
    with SessionLocal() as session:
        c3_service.generate_for_batch(session,batch_id=bid,native_client_admitted=True);session.commit()
    data={**rt.batch_view(bid),'batch_id':bid}
    assert data['reply_action']['status']=='queued'
    fixture=NS(w=w,rt=rt,binding=binding,history=history,data=data,server=server,db=db)
    yield fixture
    if hasattr(fixture,'extra_db'):fixture.extra_db.close()
    db.close()


def human_takeover(c):
    with SessionLocal() as db:
        conv=db.get(Conversation,c.binding['conversation_id'])
        event=MessageEvent(binding_id=c.binding['binding_id'],conversation_id=conv.conversation_id,
            worker_id=c.w['id'],rpa_session_key='hook:'+c.binding['binding_id'],read_run_id='synthetic-human-fact',sender_role='self',message_type='text',content='synthetic sales reply',
            item_state='observed',dedupe_key='synthetic-human-'+str(uuid.uuid4()),
            occurred_at=datetime.now(timezone.utc),raw_payload={'sender_source':'human','native_msg_id':'9000000000000000001'})
        db.add(event);db.flush()
        reply_ownership.apply_message(db,conv,event)
        assert conv.reply_ownership['owner']=='human'
        db.commit()


def callback(c):
    scope=c.rt.scope
    raw=json.dumps({'ServerPort':str(scope.hook_api_port),'selfwxid':scope.self_wxid,
        'sendorrecv':'2','msgnumber':'1','msglist':[{'msgsvrid':'9000000000000000010',
            'msgtype':'1','fromtype':'1','fromid':c.binding['peer_wxid'],'toid':scope.self_wxid,
            'msg':'synthetic new question','time':datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]}).encode()
    NativeInbox(max_body_bytes=65536,max_inbox_bytes=1048576).persist(raw,scope)


@pytest.mark.parametrize('change',['local_expiry','server_expiry','human_claim','human_task_claim','human_begin','callback_before','callback_after','pause_before_refresh'])
def test_customer_change_closes_original_without_physical_send(case,monkeypatch,change):
    c=case;original=HookFormalClient.call;fired=False
    def call(client,credentials,method,path,body=None,**kwargs):
        nonlocal fired
        if path.endswith('/tasks/'+c.data['task']['id']+'/claim') and change=='human_task_claim' and not fired:
            fired=True;human_takeover(c)
        if path.endswith('/native-replies/'+c.data['reply_action']['id']+'/claim') and change=='human_claim' and not fired:
            fired=True;human_takeover(c)
        if '/native-replies/' in path and path.endswith('/begin') and change in {'server_expiry','human_begin'} and not fired:
            fired=True
            if change=='human_begin':human_takeover(c)
            else:
                with SessionLocal() as db:
                    db.get(ReplyAction,c.data['reply_action']['id']).expire_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
        result=original(client,credentials,method,path,body,**kwargs)
        if '/native-replies/' in path and path.endswith('/claim') and change=='pause_before_refresh':
            from app.models.wechat import WechatSessionBinding
            with SessionLocal() as db:
                db.get(WechatSessionBinding,c.binding['binding_id']).allow_listening=False;db.commit()
        if '/native-replies/' in path and path.endswith('/claim') and change=='local_expiry':
            # Server returned a real permit; clock crosses its deadline in queue.
            deadline=datetime.fromisoformat(result['expire_at'])+timedelta(seconds=1)
            monkeypatch.setattr(NativeSendPermit,'validate_scope',lambda permit,process,scope:
                original_validate(replace(permit,clock=NS(upper_now=lambda:deadline)),process,scope))
        if '/native-replies/' in path and path.endswith('/begin') and change=='callback_after' and not fired:
            fired=True;callback(c)
        return result
    original_validate=NativeSendPermit.validate_scope
    monkeypatch.setattr(HookFormalClient,'call',call)
    original_send=c.rt.gateway.send_text_once
    def send(*args):
        nonlocal fired
        if change=='callback_before' and not fired:fired=True;callback(c)
        return original_send(*args)
    monkeypatch.setattr(c.rt.gateway,'send_text_once',send)
    result=c.rt.reply_once(c.binding,c.data,history=c.history)
    assert result['status']=='not_triggered' and not result['automatic_resend_allowed']
    assert c.rt.active_flow_id is None
    assert not any(row[0]=='/SendTextMsg' for row in c.server.requests)
    assert not list(c.rt.private_root.rglob('trigger.json'))
    with SessionLocal() as db:
        task=db.get(Task,c.data['task']['id']);action=db.get(ReplyAction,c.data['reply_action']['id'])
        assert task.status in {'cancelled','failed'}
        assert db.get(Worker,c.w['id']).run_status=='running'
        if change.startswith('human'):
            assert db.get(Conversation,c.binding['conversation_id']).reply_ownership['owner']=='human'
        if change in {'human_claim','human_task_claim'}:assert action.send_token is None
        else:
            ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==action.id))
            assert ack.action_phase=='not_attempted' and ack.send_result=='failed'
            assert bool(action.ai_payload.get('native_send_attempt'))==(change=='callback_after')


@pytest.mark.parametrize('wrong',['account','epoch','process'])
def test_changed_permit_identity_stays_error_and_preserves_permit(case,monkeypatch,wrong):
    c=case;original=HookFormalClient.native_send_claim
    def claim(*args,**kwargs):
        result=original(*args,**kwargs)
        result[{'account':'self_wxid','epoch':'account_epoch','process':'process_generation'}[wrong]]=str(uuid.uuid4()) if wrong=='epoch' else 'wxid_wrong'
        return result
    monkeypatch.setattr(HookFormalClient,'native_send_claim',claim)
    with pytest.raises(Exception,match='HOOK_SEND_PERMIT_EXPIRED_OR_CHANGED'):
        c.rt.reply_once(c.binding,c.data,history=c.history)
    assert list(c.rt.private_root.rglob('permit.json'))
    assert not list(c.rt.private_root.rglob('not-triggered.json'))
    assert not any(row[0]=='/SendTextMsg' for row in c.server.requests)


def restart_dispatch(c):
    import threading
    from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
    from chejin_worker_client.models import Binding
    old=c.rt
    c.rt=HookFormalRuntime(old.client,old.credentials,old.gateway,old.prepared,old.private_root,wait=lambda _:None)
    runner=NS(binding=Binding(c.w['id'],c.w['worker_token'],c.w['instance'],'paused'),
        stop_event=threading.Event(),api=NS(),_can_start_new_flow=lambda:False,can_pull_tasks=lambda:False,
        on_task=lambda _:None,_worker_transaction_barrier_ready=lambda *a,**k:True)
    c.rt.prepared.account=c.rt.heartbeat(passive=True)
    d=HookBusinessDispatcher(c.rt.client,c.rt.private_root,None,None,automatic_transcription_disabled=lambda:True)
    d.runtime=c.rt
    services=NS(connection=NS(prepared=c.rt.prepared,gateway=c.rt.gateway),_credentials=c.rt.credentials,
        startup_window_ready_for=lambda _:True)
    for _ in range(2):d.tick(runner,services)
    assert c.rt.active_flow_id is None
    assert runner.binding.run_status=='paused'
    with SessionLocal() as db:assert not db.get(Worker,c.w['id']).inflight_flow_state.get('flow_id')


@pytest.mark.parametrize('stage',['unpermitted','untriggered'])
@pytest.mark.parametrize('loss',['request','response','finish'])
def test_original_refusal_replays_after_network_loss_and_restart(case,monkeypatch,stage,loss):
    c=case;original=HookFormalClient.call;lost=False;changed=False
    target='/native-recovery' if stage=='unpermitted' else '/not-triggered'
    def call(client,credentials,method,path,body=None,**kw):
        nonlocal lost,changed
        if '/native-replies/' in path and path.endswith('/claim') and stage=='unpermitted' and not changed:
            changed=True;human_takeover(c)
        lose=path.endswith('/inflight-flow/finish' if loss=='finish' else target) and not lost
        if lose and loss=='request':
            lost=True;raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic request loss',0)
        result=original(client,credentials,method,path,body,**kw)
        if '/native-replies/' in path and path.endswith('/begin') and stage=='untriggered' and not changed:
            changed=True;callback(c)
        if lose:
            lost=True;raise ApiError('HOOK_BACKEND_NETWORK_FAILED','synthetic reply loss',0)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(HookFormalClient,'call',call)
        with pytest.raises(ApiError,match='synthetic'):
            c.rt.reply_once(c.binding,c.data,history=c.history)
    assert lost
    evidence={p.relative_to(c.rt.private_root):p.read_bytes() for p in c.rt.private_root.rglob('*.json')
        if p.name in {'permit.json','intent.json','not-triggered.json','request.json'}}
    assert post(c.w,f"/workers/{c.w['id']}/run-status",{'client_instance_id':c.w['instance'],'run_status':'paused'}).status_code==200
    restart_dispatch(c)
    assert all((c.rt.private_root/name).read_bytes()==raw for name,raw in evidence.items())
    assert not any(row[0]=='/SendTextMsg' for row in c.server.requests)
    with SessionLocal() as db:
        assert db.get(Worker,c.w['id']).run_status=='paused'
        assert db.get(Task,c.data['task']['id']).status in {'cancelled','failed'}


@pytest.mark.parametrize('danger',['still_eligible','wrong_fence','wrong_action','issued_permit','wrong_coverage_generation'])
def test_refusal_recovery_requires_authoritative_unsent_customer_state(case,danger):
    c=case;task=c.data['task'];action=c.data['reply_action']
    flow=c.rt.start_flow(task,c.binding['conversation_id'])
    claimed=c.rt.claim(task,flow,c.binding['conversation_id']);token=claimed['lease_fencing_token']
    if danger=='issued_permit':
        c.rt.client.native_send_claim(c.rt.credentials,c.rt.account,task['id'],action['id'],token,
            c.history['history_state_digest'],flow_id=flow)
    elif danger=='wrong_coverage_generation':
        from app.models.wechat import WechatSessionBinding
        with SessionLocal() as db:
            binding=db.get(WechatSessionBinding,c.binding['binding_id'])
            binding.native_context={**binding.native_context,'coverage_process_generation':'wrong-generation'};db.commit()
    elif danger!='still_eligible':human_takeover(c)
    if danger=='wrong_fence':token+=1
    if danger=='wrong_action':action={'id':str(uuid.uuid4())}
    with pytest.raises(ApiError):c.rt.close_unpermitted_reply(task,action,flow,c.binding['conversation_id'],token)
    assert c.rt.active_flow_id==flow
    assert not list((c.rt.private_root/'recovery').rglob('receipt.json'))
    assert not any(row[0]=='/SendTextMsg' for row in c.server.requests)


def test_customer_paused_callback_keeps_original_outbox_and_reason(case):
    from chejin_worker_client import storage
    from app.models.wechat import WechatSessionBinding
    c=case;callback(c)
    with SessionLocal() as db:
        b=db.get(WechatSessionBinding,c.binding['binding_id']);b.allow_listening=False;b.authorization_revision+=1;db.commit()
    with pytest.raises(ApiError) as error:c.rt.drain(c.binding)
    assert error.value.code=='HOOK_BINDING_PAUSED'
    with storage.db_connection() as db:
        original=db.execute('SELECT raw_body FROM hook_raw_inbox').fetchone()[0]
        rows=db.execute('SELECT status,payload_json FROM hook_delivery_outbox').fetchall()
        assert rows and all(row['status']!='stored' for row in rows)
    inventory=c.rt.client.call(c.rt.credentials,'POST',f"/workers/{c.w['id']}/wechat/native-work",c.rt.account)
    assert not any(b['binding_id']==c.binding['binding_id'] for b in inventory['bindings'])
    with storage.db_connection() as db:assert db.execute('SELECT raw_body FROM hook_raw_inbox').fetchone()[0]==original


def test_original_no_trigger_evidence_survives_desktop_export(case,monkeypatch,tmp_path):
    import zipfile
    from chejin_worker_client.diagnostic_bundle import export_bundle
    c=case;original=c.rt.gateway.send_text_once
    def interrupted(*args):callback(c);return original(*args)
    monkeypatch.setattr(c.rt.gateway,'send_text_once',interrupted)
    assert c.rt.reply_once(c.binding,c.data,history=c.history)['status']=='not_triggered'
    before={p:p.read_bytes() for p in c.rt.private_root.rglob('*.json')}
    target=tmp_path/'diagnostic.zip';export_bundle(tmp_path,target)
    with zipfile.ZipFile(target) as z:
        names=z.namelist()
        assert any(n.endswith('/not-triggered.json') for n in names)
        assert any(n.endswith('/permit.json') for n in names)
        assert any('/send-receipts/' in n for n in names)
        for n in names:
            if n.endswith('/not-triggered.json'):
                assert json.loads(z.read(n))['phase']=='not_triggered'
    assert all(p.read_bytes()==raw for p,raw in before.items())


@pytest.mark.parametrize('kind',['empty_text','unsupported_media','order_pending','cross_database','revoke_pending','boundary_second'])
def test_incomplete_valid_history_uses_original_send_deadline_not_global_fault(case,monkeypatch,kind):
    from app.services import hook_send_service
    from chejin_worker_client.hook_dispatcher import HookBusinessDispatcher
    c=case;time.sleep(1.05)
    if kind=='cross_database':
        c.extra_db=sqlite3.connect(':memory:',check_same_thread=False);c.extra_db.row_factory=sqlite3.Row
        c.extra_db.execute(c.db.execute("SELECT sql FROM sqlite_master WHERE name='MSG'").fetchone()[0])
        c.server.query_response=lambda body:{'data':[dict(r) for r in
            (c.extra_db if body['dbname']=='MSG1.db' else c.db).execute(body['sql']).fetchall()]}
    def provider(path):
        c.server.raw_override=None
        if path=='/SendTextMsg':
            created=int(c.binding['history_since_create_time']) if kind=='boundary_second' else int(time.time())
            c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(1,'9000000000000000002',
                43 if kind=='unsupported_media' else 10002 if kind=='revoke_pending' else 1,0,created,0 if kind=='order_pending' else created*1000,
                1,1,c.binding['peer_wxid'],'' if kind=='empty_text' else 'synthetic pending'))
            if kind=='cross_database':
                c.extra_db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (1,'9000000000000000003',1,0,created,created*1000+1,1,2,c.binding['peer_wxid'],'synthetic second shard'))
                c.server.catalog_entries.append({'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG1.db','dbhandle':'second-shard'})
            c.server.raw_override=b'{"SendTextMsg":"1"}'
    c.server.on_request=provider
    result=c.rt.reply_once(c.binding,c.data,history=c.history)
    assert result['status']=='confirmation_pending'
    inventory=c.rt.client.call(c.rt.credentials,'POST',f"/workers/{c.w['id']}/wechat/native-work",c.rt.account)
    retained=inventory['retained_sends'][0]
    before={p:p.read_bytes() for p in c.rt.private_root.rglob('*.json')
        if p.name in {'permit.json','intent.json','trigger.json','outcome.json'}}
    d=HookBusinessDispatcher(c.rt.client,c.rt.private_root,None,None,automatic_transcription_disabled=lambda:True);d.runtime=c.rt
    assert d._recover_send(retained['reconciliation_binding'],retained) is False
    with SessionLocal() as db:assert db.get(Worker,c.w['id']).run_status=='running'
    # Same original server deadline, not a new timeout or a fabricated success.
    monkeypatch.setattr(hook_send_service,'utcnow',lambda:datetime.now(timezone.utc)+timedelta(hours=1))
    assert d._recover_send(retained['reconciliation_binding'],retained) is True
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert sum(row[0]=='/SendTextMsg' for row in c.server.requests)==1
    with SessionLocal() as db:
        ack=db.scalar(select(SentAck).where(SentAck.reply_action_id==c.data['reply_action']['id']))
        assert ack.send_result=='unknown' and ack.action_phase=='trigger_attempted'
        assert db.get(Task,c.data['task']['id']).status=='failed'
        assert db.get(Worker,c.w['id']).run_status=='running'


def provider_success(c):
    def provider(path):
        c.server.raw_override=None
        if path=='/SendTextMsg':
            body=c.server.requests[-1][1];created=int(time.time())
            i=c.db.execute('SELECT COALESCE(MAX(localId),0)+1 FROM MSG').fetchone()[0]
            c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(i,str(9000000000000000100+i),
                1,1,created,created*1000+i,1,i,body['toid'],body['msg']))
            c.server.raw_override=b'{"SendTextMsg":"1"}'
    c.server.on_request=provider


def generate_and_reply(c,binding):
    history=c.rt.sync(binding);assert history['history_ready'],history
    batch=history['message_batch']['batch_id']
    with SessionLocal() as db:
        c3_service.generate_for_batch(db,batch_id=batch,native_client_admitted=True);db.commit()
    data={**c.rt.batch_view(batch),'batch_id':batch}
    return c.rt.reply_once(binding,data,history=history)


def test_callback_interruption_then_fresh_customer_turn_can_reply(case,monkeypatch):
    c=case;time.sleep(1.05);original=c.rt.gateway.send_text_once
    with monkeypatch.context() as patch:
        def interrupted(*args):callback(c);return original(*args)
        patch.setattr(c.rt.gateway,'send_text_once',interrupted)
        assert c.rt.reply_once(c.binding,c.data,history=c.history)['status']=='not_triggered'
    old={p:p.read_bytes() for p in c.rt.private_root.rglob('*.json')
         if p.name in {'permit.json','not-triggered.json'}}
    created=int(time.time())
    c.db.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',(1,'9000000000000000010',1,0,
        created,created*1000,1,1,c.binding['peer_wxid'],'synthetic new question'))
    provider_success(c)
    result=generate_and_reply(c,c.binding)
    assert result['status']=='sent'
    assert result['reply_action_id']!=c.data['reply_action']['id']
    assert sum(row[0]=='/SendTextMsg' for row in c.server.requests)==1
    assert all(p.read_bytes()==raw for p,raw in old.items())


def test_human_owned_customer_does_not_block_second_customer(case,monkeypatch):
    from test_hook_account_http import client
    from test_hook_business_http import proof,body
    c=case;original=HookFormalClient.native_send_claim
    with monkeypatch.context() as patch:
        def refused(*args,**kw):human_takeover(c);return original(*args,**kw)
        patch.setattr(HookFormalClient,'native_send_claim',refused)
        assert c.rt.reply_once(c.binding,c.data,history=c.history)['status']=='not_triggered'
    lead=client.post('/api/leads',json={'customer_name':'synthetic second customer','phones':['13800000004']})
    assert lead.status_code==200
    task=c.rt.client.call(c.rt.credentials,'GET',f"/workers/{c.w['id']}/tasks/pull")['task']
    flow=c.rt.start_flow(task);claimed=c.rt.claim(task,flow)
    h={'X-Inflight-Flow-Id':flow,'X-Task-Lease-Fencing-Token':str(claimed['lease_fencing_token'])}
    prep=post(c.w,f"/tasks/{task['id']}/hook-invite/prepare",c.rt.account,h).json()['data']
    submission=post(c.w,f"/tasks/{task['id']}/hook-invite/submitted",proof(c.rt.account,prep),h)
    assert submission.status_code==200,submission.text
    c.rt.finish(task['id'],flow,claimed['lease_fencing_token'])
    bound_response=post(c.w,f"/workers/{c.w['id']}/wechat/native-bindings",body(task,c.rt.account,prep,('wxid_other_customer',)))
    assert bound_response.status_code==200,bound_response.text
    binding=bound_response.json()['data'];time.sleep(1.05);provider_success(c)
    result=generate_and_reply(c,binding)
    assert result['status']=='sent'
    calls=[row for row in c.server.requests if row[0]=='/SendTextMsg']
    assert len(calls)==1 and calls[0][1]['toid']=='wxid_other_customer'
    with SessionLocal() as db:
        assert db.get(Worker,c.w['id']).run_status=='running'
        assert db.get(Conversation,c.binding['conversation_id']).reply_ownership['owner']=='human'


@pytest.mark.parametrize('stage',['claim','begin'])
@pytest.mark.parametrize('reason',['lead_invalid','conversation_closed'])
def test_related_customer_refusal_closes_original(case,monkeypatch,stage,reason):
    from test_hook_account_http import client
    c=case;original=HookFormalClient.call;changed=False;codes=[]
    def call(self,credentials,method,path,body=None,**kw):
        nonlocal changed
        if '/native-replies/' in path and path.endswith('/'+stage) and not changed:
            changed=True
            if reason=='lead_invalid':
                r=client.post(f"/api/leads/{c.binding['lead_id']}/mark-invalid",json={'invalid_reason':'test_data'})
                assert r.status_code==200,r.text
            else:
                with SessionLocal() as db:
                    conv=db.get(Conversation,c.binding['conversation_id']);conv.status='closed';conv.ai_enabled=False;db.commit()
        try:return original(self,credentials,method,path,body,**kw)
        except Exception as e:codes.append(getattr(e,'code',str(e)));raise
    monkeypatch.setattr(HookFormalClient,'call',call)
    result=c.rt.reply_once(c.binding,c.data,history=c.history)
    assert changed and result['status']=='not_triggered' and not result['automatic_resend_allowed']
    assert c.rt.active_flow_id is None
    assert not any(row[0]=='/SendTextMsg' for row in c.server.requests)
    with SessionLocal() as db:
        assert db.get(Task,c.data['task']['id']).status in {'failed','cancelled'}
        assert db.get(Worker,c.w['id']).run_status=='running'
    print(reason,stage,codes)


@pytest.mark.parametrize('case',['catalog','boundary'],indirect=True)
@pytest.mark.parametrize('stage',['claim','begin'])
def test_changed_generated_business_context_closes_without_hook(case,stage,monkeypatch,request):
    from app.models.c3 import HandoffEvent
    from test_c3_api import _create_listed_vehicle
    c=case;original=HookFormalClient.call;changed=False;codes=[]
    mode=request.node.callspec.params['case']
    def call(self,credentials,method,path,body=None,**kw):
        nonlocal changed
        if '/native-replies/' in path and path.endswith('/'+stage) and not changed:
            changed=True
            if mode=='catalog':_create_listed_vehicle()
            else:
                with SessionLocal() as db:
                    event=db.scalar(select(HandoffEvent).where(HandoffEvent.conversation_id==c.binding['conversation_id']))
                    assert event and not event.closed_at
                    event.closed_at=datetime.now(timezone.utc);db.commit()
        try:return original(self,credentials,method,path,body,**kw)
        except Exception as e:codes.append(getattr(e,'code',str(e)));raise
    monkeypatch.setattr(HookFormalClient,'call',call)
    result=c.rt.reply_once(c.binding,c.data,history=c.history)
    assert changed and result['status']=='not_triggered'
    assert not result['automatic_resend_allowed'] and c.rt.active_flow_id is None
    assert not any(row[0]=='/SendTextMsg' for row in c.server.requests)
    assert not list(c.rt.private_root.rglob('trigger.json'))
    with SessionLocal() as db:assert db.get(Worker,c.w['id']).run_status=='running'
    assert codes
