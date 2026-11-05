"""D2: actual Worker in its own process; untraced backend/Hook/model fixtures.

No Windows, DLL, production account, actual model key or customer messages.
Keep sampler policy and all business timeouts/budgets unchanged in both modes.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.request import Request, urlopen
import zipfile

import pytest
import psutil
from sqlalchemy import select, text
from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine as database_engine
from app.models.c3 import ReplyAction, SentAck, MessageBatch, HandoffEvent
from app.models.worker import Worker
from app.models.wechat import MessageEvent
from app.models.task import Task
from app.services import c3_service
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from test_hook_history_business_http import bound
from test_hook_account_client import identity_client
from test_hook_gateway import lab
from test_partial_reply_recovery_provider import controlled_recovery_provider
from chejin_worker_client import memory_capture as mc
from chejin_worker_client.diagnostic_bundle import export_bundle


@pytest.fixture(autouse=True)
def clean():
    # These tests reset all tables. Only an explicitly named disposable local
    # database may be used; never accept a production/default connection.
    url = database_engine.url
    if database_engine.dialect.name == 'postgresql':
        assert os.environ.get('PE02_PG_TEST') == '1'
        assert url.host == '127.0.0.1' and url.port == 55464
        assert url.database == 'pe02_memory_verification' and url.username == 'pe02_test'
    else:
        assert database_engine.dialect.name == 'sqlite'
        assert '/private/tmp/chejin-hook-formal-20261011/test-' in str(url)
    Base.metadata.drop_all(database_engine)
    if database_engine.dialect.name == 'postgresql':
        with database_engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS wechat_ai_customer_service"))
    Base.metadata.create_all(database_engine)


def second_bound(w, account):
    """Second customer through original lead/task/invite/bind APIs."""
    from test_hook_business_http import client, post, proof, body
    lead=client.post('/api/leads',json={'customer_name':'Synthetic second customer','phones':['13800000004']})
    assert lead.status_code==200,lead.text
    response=client.get(f"/api/workers/{w['id']}/tasks/pull",headers={
        'X-Worker-Token':w['worker_token'],'X-Client-Instance-Id':w['instance']})
    assert response.status_code==200,response.text
    task=response.json()['data']['task'];assert task
    flow={'X-Inflight-Flow-Id':task['id']}
    response=post(w,f"/workers/{w['id']}/inflight-flow/start",{'flow_id':task['id'],'flow_kind':'task'})
    assert response.status_code==200,response.text
    response=post(w,f"/tasks/{task['id']}/claim",{'worker_id':w['id']},flow)
    assert response.status_code==200,response.text
    headers={**flow,'X-Task-Lease-Fencing-Token':str(response.json()['data']['lease_fencing_token'])}
    response=post(w,f"/tasks/{task['id']}/hook-invite/prepare",account,headers)
    assert response.status_code==200,response.text
    prep=response.json()['data']
    response=post(w,f"/tasks/{task['id']}/hook-invite/submitted",proof(account,prep),headers)
    assert response.status_code==200,response.text
    response=post(w,f"/workers/{w['id']}/inflight-flow/finish",{'flow_id':task['id'],'terminal_kind':'task_terminal'},headers)
    assert response.status_code==200,response.text
    response=post(w,f"/workers/{w['id']}/wechat/native-bindings",body(task,account,prep,('wxid_customer_B',)))
    assert response.status_code==200,response.text
    return response.json()['data']


class Actor:
    def __init__(self, folder):
        repo = Path(__file__).resolve().parents[2]
        self.stderr = (folder / 'actor.log').open('w')
        env = {**os.environ, 'QT_QPA_PLATFORM':'offscreen',
               'PYTHONPATH': os.pathsep.join([str(repo/'worker-client'),str(repo/'worker-client/omniauto-rpa')])}
        self.p = subprocess.Popen([sys.executable, str(repo/'worker-client/tests/helpers/memory_business_actor.py'),str(folder)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True, env=env)
        self.selector = selectors.DefaultSelector(); self.selector.register(self.p.stdout, selectors.EVENT_READ)
    def call(self, op, **value):
        self.p.stdin.write(json.dumps({'op':op, **value})+'\n'); self.p.stdin.flush()
        assert self.selector.select(40), f'Worker did not answer {op}'
        line = self.p.stdout.readline()
        assert line, f'Worker exited at {op}'
        result = json.loads(line)
        assert 'error' not in result, result
        return result
    def close(self):
        try:
            if self.p.poll() is None:
                try:
                    self.p.stdin.write('{"op":"close"}\n'); self.p.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    self.p.wait(10)
                except subprocess.TimeoutExpired:
                    self.p.terminate()
                    try:
                        self.p.wait(5)
                    except subprocess.TimeoutExpired:
                        self.p.kill(); self.p.wait(5)
        finally:
            self.selector.close(); self.stderr.close()
            self.p.stdin.close(); self.p.stdout.close()


def business_snapshot(worker_id):
    """Copy actual producer results before assertions, including failed rounds."""
    def columns(obj):
        return {c.name:getattr(obj,c.name) for c in obj.__table__.columns}
    with SessionLocal() as db:
        return {
            name: [columns(obj) for obj in db.scalars(select(model))]
            for name, model in (
                ('actions', ReplyAction), ('acks', SentAck), ('batches', MessageBatch),
                ('messages', MessageEvent), ('tasks', Task), ('handoffs', HandoffEvent))
        } | {'worker': columns(db.get(Worker, worker_id))}


def finalize_evidence(actor, table, folder, destination, report, evidence,
                      request_thread, results, armed, worker_id, model_exchanges, hook_requests):
    """Independent best-effort evidence stages; never replace a business failure."""
    primary = sys.exc_info()[1]  # Caller may be unwinding a business assertion.
    failures = []
    issues = []
    def describe(exc):
        return {'type':type(exc).__name__, 'message':str(exc)}
    if primary is not None:
        report['primary_business_failure'] = describe(primary)
        evidence['primary_business_failure'] = describe(primary)
    report['evidence_issues'] = issues
    evidence['evidence_issues'] = issues
    def attempt(stage, action):
        try:
            return action()
        except BaseException as exc:
            failures.append(exc)
            issues.append({'stage':stage, **describe(exc)})
            return None
    def wait_for_capture():
        if request_thread:
            request_thread.join(42)
            assert not request_thread.is_alive(), 'capture requester did not terminate'
        elif armed:
            remaining=armed['started']+30-time.monotonic()
            if remaining>0: time.sleep(remaining)
    def read_observer():
        observer=folder/'observer.jsonl'
        events=[json.loads(line) for line in observer.read_text().splitlines()] if observer.exists() else []
        report.update(spans=[e for e in events if e['kind']=='span'],
            resources=[e for e in events if e['kind']=='resource'],
            waits=[e for e in events if e['kind']=='wait'], observer_events=events)
    try:
        attempt('destination', lambda:destination.mkdir(parents=True,exist_ok=True))
        evidence.update(model_exchanges=model_exchanges, hook_requests=hook_requests)
        attempt('business_snapshot', lambda:evidence.update(business_snapshot(worker_id)))
        attempt('capture_wait', wait_for_capture)
        attempt('actor_report', lambda:report.update(actor.call('report')) if actor.p.poll() is None else None)
        attempt('observer', read_observer)
        report['capture_request']=results[0] if results else None
        report['capture_requests']=results
    finally:
        attempt('actor_close', actor.close)
        attempt('database_close', table.close)
    # These remain independent even if the database, actor or another export
    # failed. The original business exception stays primary in the caller.
    attempt('business_chain_write', lambda:(destination/'business-chain.json').write_text(
        json.dumps(evidence,ensure_ascii=False,indent=2,default=str)))
    attempt('worker_export', lambda:export_bundle(folder,destination/'evidence.zip',
        capture_request=results[0] if results else None))
    attempt('result_write', lambda:(destination/'result.json').write_text(
        json.dumps(report,ensure_ascii=False,indent=2)))
    if failures and primary is None:
        raise failures[0]


@pytest.mark.parametrize('failure', ['snapshot', 'report', 'export', 'requester'])
def test_finalization_failure_preserves_evidence_and_closes_resources(monkeypatch, tmp_path, failure):
    closed=[]; exported=[]
    actual={'actions':[{'id':'actual-action','status':'unknown_send_result'}]}
    def snapshot(worker_id):
        if failure=='snapshot': raise RuntimeError('snapshot failed')
        return actual
    def report(op):
        if failure=='report': raise RuntimeError('report failed')
        return {'tracing':False}
    def export(*args, **kwargs):
        exported.append(True)
        if failure=='export': raise RuntimeError('export failed')
    monkeypatch.setattr(sys.modules[__name__], 'business_snapshot', snapshot)
    monkeypatch.setattr(sys.modules[__name__], 'export_bundle', export)
    actor=SimpleNamespace(p=SimpleNamespace(poll=lambda:None), call=report,
                          close=lambda:closed.append('actor'))
    table=SimpleNamespace(close=lambda:closed.append('db'))
    requester=SimpleNamespace(join=lambda _:None, is_alive=lambda:True) if failure=='requester' else None
    evidence={}; result={}; destination=tmp_path/'evidence'
    with pytest.raises((RuntimeError, AssertionError)):
        finalize_evidence(actor,table,tmp_path,destination,result,evidence,requester,[],None,
            'worker', [{'response':'actual-model-result'}], [['actual-hook-request']])
    assert closed==['actor','db']
    saved=json.loads((destination/'business-chain.json').read_text())
    assert saved['model_exchanges']==[{'response':'actual-model-result'}]
    assert saved['hook_requests']==[['actual-hook-request']]
    if failure!='snapshot':
        assert saved['actions']==actual['actions']
    assert exported==[True]
    assert (destination/'result.json').is_file()
    assert result['evidence_issues']


@pytest.mark.parametrize('business_failure', [False, True])
@pytest.mark.parametrize('faults', [('report','export','actor_close','db_close'), ('snapshot','observer')])
def test_combined_finalization_faults_keep_primary_and_try_all_sources(monkeypatch,tmp_path,business_failure,faults):
    calls=[]; report={}; evidence={}
    def operation(name, value=None):
        calls.append(name)
        if name in faults: raise RuntimeError(name+' failure')
        return value
    monkeypatch.setattr(sys.modules[__name__],'business_snapshot',lambda _:operation('snapshot',{'actions':[]}))
    monkeypatch.setattr(sys.modules[__name__],'export_bundle',lambda *a,**kw:operation('export'))
    actor=SimpleNamespace(p=SimpleNamespace(poll=lambda:None),
        call=lambda _:operation('report',{'tracing':False}),close=lambda:operation('actor_close'))
    table=SimpleNamespace(close=lambda:operation('db_close'))
    (tmp_path/'observer.jsonl').write_text('invalid json' if 'observer' in faults else '{"kind":"resource","rss":123}\n')
    primary=AssertionError('original business failure')
    with pytest.raises(AssertionError if business_failure else RuntimeError) as raised:
        try:
            if business_failure: raise primary
        finally:
            finalize_evidence(actor,table,tmp_path,tmp_path/'out',report,evidence,None,[],None,'worker',[],[])
    if business_failure:
        assert raised.value is primary
        assert report['primary_business_failure']=={'type':'AssertionError','message':str(primary)}
    else:
        assert str(raised.value)==('report failure' if 'report' in faults else 'snapshot failure')
    assert calls==['snapshot','report','actor_close','db_close','export']
    stages={i['stage'] for i in report['evidence_issues']}
    expected={'actor_report','worker_export','actor_close','database_close'} if 'report' in faults else {'business_snapshot','observer'}
    assert stages==expected
    saved=json.loads((tmp_path/'out/result.json').read_text())
    assert saved['evidence_issues']==report['evidence_issues']
    if business_failure: assert saved['primary_business_failure']==report['primary_business_failure']


@pytest.mark.parametrize('repeat', [0, 1, 2])
@pytest.mark.parametrize('scenario', ['segmented', 'retained_echo', 'retained_unknown', 'running_dispatch'])
@pytest.mark.parametrize('mode', ['off', 'on'])
def test_capture_during_full_business_and_original_recovery(identity_client, lab, monkeypatch, tmp_path, mode, scenario, repeat):
    repo = Path(__file__).resolve().parents[2]
    engine = str(repo/'worker-client/omniauto-rpa')
    monkeypatch.setenv('C3_OMNIAUTO_ROOT', engine)
    monkeypatch.setattr(get_settings(), 'c3_omniauto_root', engine)
    monkeypatch.setattr(c3_service, 'get_ai_engine_adapter', RealOmniAutoAIEngineAdapter)
    w, task, account, binding = bound()
    other=None
    server, gateway, process, scope, receiver, receiver_thread = lab
    # The test Hook HTTP server's actual process substitutes only the physical
    # WeChat OS boundary. Its RSS is test-process evidence, never DLL allocation.
    process=replace(process,pid=os.getpid(),started_at=psutil.Process().create_time(),listener_owners=(os.getpid(),))
    server.current_process=process
    receiver.shutdown(); receiver_thread.join(2); receiver.server_close()
    table = sqlite3.connect(':memory:', check_same_thread=False); table.row_factory=sqlite3.Row
    table.execute('CREATE TABLE MSG(localId INTEGER PRIMARY KEY,MsgSvrID TEXT,Type INTEGER,IsSender INTEGER,CreateTime INTEGER,Sequence INTEGER,MsgServerSeq INTEGER,MsgSequence INTEGER,StrTalker TEXT,StrContent TEXT)')
    server.catalog_entries=[{'dbpath':r'C:\WeChat Files\wxid_sales_A\Msg\Multi\MSG0.db','dbhandle':'memory-test-handle'}]
    server.query_response=lambda body:{'data':[dict(row) for row in table.execute(body['sql']).fetchall()]}
    delayed=[]
    def row(text, sender, peer=None):
        n=table.execute('SELECT COUNT(*) FROM MSG').fetchone()[0]+1
        now=int(time.time())
        return (n,str(8100000000000000000+n),1,sender,now,now*1000+n,0,n,peer or binding['peer_wxid'],text)
    def on_request(path):
        server.raw_override=None
        if path == '/SendTextMsg':
            sent=row(server.requests[-1][1]['msg'],1,server.requests[-1][1]['toid'])
            if scenario.startswith('retained'): delayed.append(sent)
            else: table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',sent)
            server.raw_override=b'{"SendTextMsg":"1"}'
    server.on_request=on_request
    folder=tmp_path/'worker'; folder.mkdir()
    actor=Actor(folder)
    request_thread=None; results=[]; model_requests=[]; model_exchanges=[]; report={}; evidence={}; armed=None
    answer = ['好的，先按您的日常代步需求了解，您平时主要在市区通勤还是也会跑长途？了解使用场景以后再讨论车型。也可以说说日常乘坐人数、停车空间和后备厢的使用需要，这些都会影响选车的方向。',
              '另外想了解您更偏好轿车还是SUV，以及自动挡是否方便驾驶。具体车况需要结合实际检测资料核实，不能只凭描述作保证。后续看车时可以逐项检查保养记录、实际使用情况和检测项目，再根据核实的资料选择。']
    if scenario.startswith('retained'): answer=answer[:1]
    current_answer=answer
    try:
        init=actor.call('init',deployment=asdict(gateway.deployment),process=asdict(process),
            credentials={'worker_id':w['id'],'worker_token':w['worker_token'],'client_instance_id':w['instance']},base_url=identity_client.base_url,
            run_status='running' if scenario=='running_dispatch' else 'paused')
        assert init['pid'] != os.getpid() and init['policy']['sample_seconds']==15
        def deliver(text, target=None):
            target=target or binding
            incoming=row(text,0,target['peer_wxid'])
            table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',incoming)
            callback={'ServerPort':str(gateway.deployment.hook_api_port),'selfwxid':scope.self_wxid,
                'sendorrecv':'2','msgnumber':'1','msglist':[{'msgtype':'1','msgsvrid':incoming[1],
                'fromtype':'1','fromid':target['peer_wxid'],'toid':scope.self_wxid,'msg':incoming[-1]}]}
            with urlopen(Request(init['callback_url'],data=json.dumps(callback).encode(),headers={'Content-Type':'application/json'}),timeout=5) as response:
                assert response.status==200
            evidence.setdefault('callbacks',[]).append(callback)
        with controlled_recovery_provider(monkeypatch, answer, model_requests, catalog_claim_kind='non_inventory', exchange_observer=model_exchanges.append,
                answer_factory=lambda _:current_answer):
            if scenario=='running_dispatch':
                deliver('我想了解日常代步怎么选择，主要市区通勤，还没有决定轿车还是SUV。')
                before=actor.call('dispatch',expected_sends=len(answer))
                evidence['dispatch_before_capture']=before
                other=second_bound(w,init['account'])
                # Keep the first message strictly after the binding second.
                # Same-second facts correctly require boundary review.
                time.sleep(max(0,int(other['history_since_create_time'])+1.01-time.time()))
            armed=actor.call('arm')
            if mode=='on':
                request_thread=threading.Thread(target=lambda:results.append(mc.request_capture(folder)))
                request_thread.start()
                assert actor.call('await_capture')['tracing']
            deliver('主要两个人坐，希望停车方便，周末也会出去玩。' if scenario=='running_dispatch'
                    else '我想了解日常代步怎么选择，主要市区通勤，还没有决定轿车还是SUV。', target=other)
            if scenario=='running_dispatch':
                reply=actor.call('dispatch',expected_sends=len(answer)*2)
                evidence['dispatch_during_capture']=reply
                if request_thread:
                    request_thread.join(42)
                    assert not request_thread.is_alive()
                else:
                    remaining=armed['started']+30-time.monotonic()
                    if remaining>0: time.sleep(remaining)
                current_answer=['婴儿车的收纳确实需要考虑，建议看车时带上常用的婴儿车试放，检查后备厢开口宽度和取放是否方便，也看看后排放倒后的空间是否符合您的使用习惯。',
                    '日常停车除了车身长度，也可以实际体验倒车视野、方向盘操作和车内储物空间。您提到主要市区代步，后续可以围绕这些使用场景逐项体验，再决定适合自己的车型。']
                deliver('我知道了，另外后备厢平时要放婴儿车，这个也帮我考虑。')
                after=actor.call('dispatch',expected_sends=len(answer)*3)
                evidence['dispatch_after_capture']=after
                if repeat==2:
                    if mode=='on': evidence['first_capture_resources']=actor.call('capture_resources')
                    # Real cooldown, never monkeypatch the coordinator clock.
                    remaining=armed['started']+mc.protocol.COOLDOWN_SECONDS+1-time.monotonic()
                    if remaining>0: time.sleep(remaining)
                    second_started=time.monotonic()
                    if mode=='on':
                        request_thread=threading.Thread(target=lambda:results.append(mc.request_capture(folder)))
                        request_thread.start(); assert actor.call('await_capture')['tracing']
                    current_answer=['接送家人可以重点体验后排上下车是否方便、乘坐姿势和腿部空间，老人和小孩的实际感受也很重要。看车时方便的话，可以让经常乘坐的家人一起体验座椅和车门开口。',
                        '另外可以看看儿童安全座椅安装位置、后排安全带及空调出风口。具体配置要以实车为准，不同年份和配置会有差别，把常用需求列下来逐项确认会更清楚。']
                    deliver('选车还需要考虑接送家人，后排座椅和儿童安全座椅安装位置应该怎么看？')
                    evidence['dispatch_second_capture']=actor.call('dispatch',expected_sends=len(answer)*4)
                    if mode=='on':
                        request_thread.join(42); assert not request_thread.is_alive()
                        evidence['second_capture_resources']=actor.call('capture_resources')
                        a=evidence['first_capture_resources']; b=evidence['second_capture_resources']
                        assert a['capture_id']!=b['capture_id'] and b['prior_profile_released']
                        assert a['baseline_released'] and a['previous_released']
                        assert b['baseline_released'] and b['previous_released']
                        assert a['threads']==b['threads']
                        assert b['fd_count']<=a['fd_count']
                        assert all(r['status']=='finished' for r in results)
                    else:
                        time.sleep(max(0,second_started+30-time.monotonic()))
            else:
                reply=actor.call('reply',binding=binding)
        evidence.update(reply=reply, binding=binding, other_binding=other)
        if scenario.startswith('retained'):
            assert reply['decision']=='confirmation_pending', reply
            assert len(delayed)==1
            inventory=actor.call('inventory')
            assert len(inventory['retained_sends'])==1
            metadata=inventory['retained_sends'][0]
            evidence['original_inventory']=inventory
            if scenario=='retained_echo':
                # Release the original echo; never call physical send again.
                table.execute('INSERT INTO MSG VALUES(?,?,?,?,?,?,?,?,?,?)',delayed[0])
            else:
                # Simulate elapsed server observation deadline, not a changed
                # timeout/permit or fabricated receipt. Worker clocks stay real.
                from app.services import hook_send_service
                timeout_now=datetime.now(timezone.utc)+timedelta(seconds=get_settings().c3_send_ack_stale_after_seconds+1)
                monkeypatch.setattr(hook_send_service,'utcnow',lambda:timeout_now)
                evidence['controlled_server_observation_time']=timeout_now.isoformat()
            recovery=actor.call('recover')
            evidence['recovery']=recovery
            receipt=recovery['receipts'][metadata['reply_action_id']]
            assert receipt['local_send_observed'] is (scenario=='retained_echo')
            assert receipt['automatic_resend_allowed'] is False
            assert not recovery['local_control']['inflight_flow_id']
        elif scenario!='running_dispatch':
            assert len(reply['sends'])==2 and all(x['status']=='sent' for x in reply['sends']), reply
        rounds=(4 if repeat==2 else 3) if scenario=='running_dispatch' else 1
        expected_sends=len(answer)*rounds
        assert len([r for r in server.requests if r[0]=='/SendTextMsg'])==expected_sends
        assert [r['model'] for r in model_requests]==['recovery-author','recovery-reviewer']*rounds
        evidence.update(model_exchanges=model_exchanges, hook_requests=server.requests)
        report.update(mode=mode,scenario=scenario,repeat=repeat,business_seconds=reply['business_seconds'],
            model_calls=[r['model'] for r in model_requests],driver_backend_pid=os.getpid(),
            actual_callback_persistence=True,send_count=expected_sends,original_recovery=scenario.startswith('retained'),
            sampling_policy=init['policy'])
    finally:
        destination=Path(os.environ.get('PE02_D2_OUTPUT',tmp_path/'evidence'))/f'{mode}-{scenario}-{repeat}'
        finalize_evidence(actor, table, folder, destination, report, evidence,
            request_thread, results, armed, w['id'], model_exchanges, server.requests)
    # Production sent_ack contract: unknown is a failed task with handoff,
    # not a successful completion (see test_c3_recovery_chain).
    actions=evidence['actions']; acks=evidence['acks']
    tasks={t['id']:t for t in evidence['tasks']}
    unknown=scenario=='retained_unknown'
    assert len(actions)==expected_sends
    assert all(a['status']==('unknown_send_result' if unknown else 'sent') for a in actions)
    assert len(acks)==expected_sends
    for action in actions:
        task=tasks[action['claimed_task_id']]
        assert task['status']==('failed' if unknown else 'completed')
        related=[ack for ack in acks if ack['reply_action_id']==action['id']]
        assert len(related)==1
        ack=related[0]
        assert ack['task_id']==task['id'] and task['reply_action_id']==action['id']
        assert ack['send_result']==('unknown' if unknown else 'sent')
        if unknown:
            assert task['failure_step']=='send_reply_unknown'
            assert task['failed_at'] and not task['lease_owner_worker_id']
            handoffs=[h for h in evidence['handoffs'] if h['conversation_id']==action['conversation_id']
                and h['batch_id']==action['batch_id']]
            assert len(handoffs)==1
            assert handoffs[0]['handoff_reason_code']=='HOOK_NATIVE_SEND_UNCONFIRMED'
            assert 'send_unknown' in handoffs[0]['risk_flags']
            assert 'reply_action:'+action['id'] in handoffs[0]['evidence_refs']
            receipt=evidence['recovery']['receipts'][action['id']]
            assert receipt['ack']['id']==ack['id']
            assert receipt['ack']['task_id']==task['id']
            assert receipt['automatic_resend_allowed'] is False
            assert receipt['local_send_observed'] is False
    assert not evidence['worker']['inflight_flow_state']
    if scenario=='running_dispatch':
        for phase in ('before','during','after'):
            state=evidence['dispatch_'+phase+'_capture']
            assert state['before']['run_status']==state['run_status']=='running'
            assert state['before']['can_start'] and state['can_start']
            assert not state['local_control']['inflight_flow_id']
        assert {a['conversation_id'] for a in actions}=={binding['conversation_id'],other['conversation_id']}
        assert not before['tracing'] and not after['tracing']
        assert reply['tracing'] is (mode=='on')
    assert not report['backend_imported']
    if mode=='on':
        required={'NativeInbox.persist','NativeProjectionStore.project_next','HookDeliveryOutbox.mark_stored',
            'HookFormalRuntime.drain','HookFormalRuntime.sync','HookFormalRuntime.reply_once',
            'HookNativeSender.execute','HookFormalClient.native_send_settle','HookFormalRuntime.finish'}
        if scenario=='running_dispatch': required.add('HookBusinessDispatcher.tick')
        if scenario.startswith('retained'): required.update({'HookBusinessDispatcher.tick','HookBusinessDispatcher._recover_send'})
        seen={s['operation'] for s in report['spans'] if s['tracing_at_start'] and s['tracing_at_end']}
        assert required <= seen, {'missing_active_capture':sorted(required-seen),'request':results}
        assert results and results[0]['status']=='finished', results
        assert results[0]['target']['pid']==report['pid']
    assert report['qt_max_gap']<2, report['qt_max_gap']
    from memory_bundle_checks import check_bundle
    checked=check_bundle(destination/'evidence.zip', report, evidence, w['worker_token'])
    (destination/'bundle-check.json').write_text(json.dumps(checked,indent=2))
