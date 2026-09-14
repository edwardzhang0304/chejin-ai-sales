"""Real loopback HTTP + isolated PostgreSQL + Worker SQLite; desktop controlled.

Scheduling instrumentation only observes entry and holds the existing lock;
it does not overwrite the fault/pause result or any business method.
"""
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

import pytest
from fastapi import Request
import httpx
import socket
import time
import uvicorn
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker-client"))
from sqlalchemy import select

from app.core.auth import require_admin_auth
from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.worker import Worker
from app.models.task import Task
from app.models.sales import Sales
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.models import Binding
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.storage import save_binding,load_binding,load_runtime_control

OUT = Path(os.environ.get("PROBE_EVIDENCE_ROOT", "/private/tmp/chejin-intersections-regression"))
OUT.mkdir(parents=True, exist_ok=True)
pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Requires isolated PostgreSQL")

def record(name,value):
    (OUT/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))

@pytest.fixture
def http(tmp_path, monkeypatch):
    # These tests intentionally recreate tables; never accept a business DB.
    assert str(engine.url.database).startswith("chejin_test_intersections_")
    monkeypatch.setenv("CHEJIN_WORKER_HOME", str(tmp_path / "worker"))
    from chejin_worker_client import storage
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path / 'worker')
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path / 'worker' / 'worker_client.sqlite3')
    Base.metadata.drop_all(engine)
    with engine.begin() as db:
        db.execute(text("CREATE SCHEMA IF NOT EXISTS wechat_ai_customer_service"))
    Base.metadata.create_all(engine)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    client = httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=15)
    try:
        yield client
    finally:
        client.close()
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "test HTTP server did not stop"

def data(response):
    assert response.status_code==200,response.text
    return response.json()['data']

class RecordingHttpTransport:
    """Record actual loopback requests; preserve WorkerApi request/error handling."""
    def __init__(self,http): self.http=http;self.events=[]
    def request(self,method,url,**kwargs):
        parsed=urlsplit(url)
        path=parsed.path+('?' +parsed.query if parsed.query else '')
        response=self.http.request(method,path,headers=kwargs.get('headers'),json=kwargs.get('json'))
        value=response.json()
        body=kwargs.get('json') or {}
        self.events.append({'method':method,'path':path,'requested_status':body.get('run_status'),'recover_from_fault':body.get('recover_from_fault',False),'http_status':response.status_code,'code':value.get('code'),'returned_run_status':(value.get('data') or {}).get('run_status')})
        return response
    def get(self,url,**kwargs): return self.request('GET',url,**kwargs)

def worker(http,name):
    w=data(http.post('/api/workers',json={'worker_name':name,'enabled':True,'platform':'windows'}))
    instance='instance-'+name
    data(http.post(f"/api/workers/{w['id']}/client-bind",json={'worker_token':w['worker_token'],'client_instance_id':instance}))
    api=WorkerApiClient('http://127.0.0.1/api');api.session=RecordingHttpTransport(http)
    binding=Binding(w['id'],w['worker_token'],instance,run_status='paused')
    api.heartbeat(binding,running_status='idle',current_task=None,rpa_component_status='ready',wechat_status='logged_in')
    api.set_run_status(binding,'running');binding.run_status='running'
    return w,api,binding

class DesktopBoundary:
    def probe(self): return 'ready','logged_in'
    def sidecar_active(self): return False

def runner_for(api,binding):
    errors=[]
    runner=TaskRunner(api,DesktopBoundary(),on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,on_task=lambda _:None,on_result=lambda _:None,on_error=errors.append,can_pull_tasks=lambda:False)
    runner.binding=binding
    save_binding(binding)
    return runner,errors

def state(runner):
    with SessionLocal() as db:
        backend=db.get(Worker,runner.binding.worker_id).run_status
    return {'memory':runner.binding.run_status,'sqlite':load_binding().run_status,'backend':backend,'pending':runner._pending_run_status_sync,'pause_requested':load_runtime_control()['pause_requested'],'recovery_requested':runner._fault_recovery_requested}

@pytest.mark.parametrize('ordering',['fault_then_pause','pause_then_fault','overlap'])
def test_fault_priority_over_manual_pause(http,ordering):
    _,api,binding=worker(http,'fault-'+ordering)
    runner,errors=runner_for(api,binding)
    timeline=[]
    if ordering=='fault_then_pause':
        runner.set_run_status('faulted');runner.set_run_status('paused')
    elif ordering=='pause_then_fault':
        runner.set_run_status('paused');runner.set_run_status('faulted')
    else:
        entered=threading.Event();failures=[]
        def tracer(frame,event,arg):
            if event=='call' and frame.f_code is TaskRunner._apply_local_run_status.__code__:
                timeline.append({'event':'pause_passed_outer_guard','status_argument':frame.f_locals['run_status'],'memory_before_lock':runner.binding.run_status})
                entered.set()
            return tracer
        def pause():
            sys.settrace(tracer)
            try: runner.set_run_status('paused')
            except BaseException as exc: failures.append(repr(exc))
            finally: sys.settrace(None)
        # Pause can wait for this very lock while C2 persists a new fault.
        with runner._run_status_intent_lock:
            thread=threading.Thread(target=pause)
            thread.start()
            assert entered.wait(3),'pause did not reach the lock boundary'
            assert runner.set_run_status('faulted')
            timeline.append({'event':'fault_confirmed_before_pause_gets_lock',**state(runner)})
        thread.join(5)
        assert not thread.is_alive() and not failures,failures
    before_heartbeat=state(runner)
    runner.tick_once()
    after_heartbeat=state(runner)
    start_result=None
    after_start=None
    if ordering=='overlap':
        start_result=runner.set_run_status('running')
        after_start=state(runner)
    record('fault-'+ordering,{'ordering':ordering,'timeline':timeline,'before_heartbeat':before_heartbeat,'after_heartbeat':after_heartbeat,'start_result':start_result,'after_start':after_start,'http':api.session.events,'errors':errors,'limits':'real Worker/API/loopback HTTP/PostgreSQL/SQLite; no Windows actions'})
    assert before_heartbeat['memory']==before_heartbeat['sqlite']==before_heartbeat['backend']=='faulted',before_heartbeat
    assert after_heartbeat['memory']==after_heartbeat['sqlite']==after_heartbeat['backend']=='faulted',after_heartbeat

@pytest.mark.parametrize('change',['unchanged','replace','clear','reuse_for_other_sales'])
def test_old_worker_cannot_claim_after_sales_binding_change(http,change):
    wa,api_a,binding_a=worker(http,'original')
    wb,api_b,binding_b=worker(http,'replacement')
    sales=data(http.post('/api/sales',json={'sales_name':'销售甲','phone':'13900000001','enabled':True,'worker_id':wa['id']}))
    lead=data(http.post('/api/leads',json={'customer_name':'规则交叉测试客户','phones':['13800001234']}))
    tasks=data(http.get('/api/tasks'))['items'];assert len(tasks)==1
    task=tasks[0]
    assert task['status']=='pending' and task['worker_id']==wa['id']
    if change=='replace':
        data(http.post(f"/api/sales/{sales['id']}/worker-binding",json={'worker_id':wb['id']}))
    elif change in {'clear','reuse_for_other_sales'}:
        data(http.delete(f"/api/sales/{sales['id']}/worker-binding"))
    if change=='reuse_for_other_sales':
        data(http.post('/api/sales',json={'sales_name':'销售乙','phone':'13900000002','enabled':True,'worker_id':wa['id']}))
    before=data(http.get(f"/api/tasks/{task['id']}"))
    pull=api_a.pull_task(binding_a)
    headers={'X-Worker-Token':binding_a.worker_token,'X-Client-Instance-Id':binding_a.client_instance_id}
    claimed=http.post(f"/api/tasks/{task['id']}/claim",headers=headers,json={'worker_id':wa['id'],'client_instance_id':binding_a.client_instance_id})
    with SessionLocal() as db:
        row=db.get(Task,task['id']);current_sales=db.get(Sales,sales['id'])
        persisted={'task_status':row.status,'task_worker_id':row.worker_id,'task_sales_id':row.sales_id,'current_sales_worker_id':current_sales.worker_id}
    record('binding-'+change,{'scenario':change,'lead_id':lead['id'],'original_worker':wa['id'],'replacement_worker':wb['id'],'task_before_claim':before,'pull_mode':pull[0],'pulled_task_id':pull[1].id if pull[1] else None,'claim_http_status':claimed.status_code,'claim_code':claimed.json().get('code'),'persisted':persisted,'limits':'synthetic records created through actual HTTP + PostgreSQL; no physical add friend'})
    if change=='unchanged': assert claimed.status_code==200,claimed.text
    else: assert claimed.status_code>=400,'old Worker claimed work after its current sales ownership changed'


def ownership_case(http, count=1):
    wa, api, ba = worker(http, 'owner-a')
    wb, _, bb = worker(http, 'owner-b')
    sales = data(http.post('/api/sales', json={'sales_name': '甲', 'phone': '13900000001', 'enabled': True, 'worker_id': wa['id']}))
    for i in range(count):
        data(http.post('/api/leads', json={'customer_name': f'人工测试客户{i}', 'phones': [f'1380000{i:04d}']}))
    tasks = data(http.get('/api/tasks'))['items']
    assert len(tasks) == count
    return wa, wb, api, ba, bb, sales, tasks


def headers(binding, flow=None):
    value = {'X-Worker-Token': binding.worker_token, 'X-Client-Instance-Id': binding.client_instance_id}
    if flow:
        value['X-Inflight-Flow-Id'] = flow
    return value


def claim(http, task, binding, flow=None):
    return http.post(f"/api/tasks/{task['id']}/claim", json={'worker_id': binding.worker_id}, headers=headers(binding, flow))


def test_ten_waiting_tasks_follow_sales_without_deletion_or_second_owner(http):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http, 10)
    original_ids = {t['id'] for t in tasks}
    data(http.delete(f"/api/sales/{sales['id']}/worker-binding"))
    waiting = data(http.get('/api/tasks'))['items']
    assert {t['id'] for t in waiting} == original_ids
    assert all(t['status'] == 'blocked' and t['worker_id'] is None and t['sales_id'] == sales['id'] for t in waiting)
    data(http.post('/api/sales', json={'sales_name': '乙', 'phone': '13900000002', 'worker_id': wa['id'], 'enabled': True}))
    assert api.pull_task(ba)[1] is None
    assert all(claim(http, task, ba).status_code == 409 for task in tasks)
    data(http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': wb['id']}))
    moved = data(http.get('/api/tasks'))['items']
    assert {t['id'] for t in moved} == original_ids
    assert all(t['status'] == 'pending' and t['worker_id'] == wb['id'] and t['sales_id'] == sales['id'] for t in moved)
    fetched = data(http.get(f"/api/workers/{wb['id']}/tasks/pull", headers=headers(bb)))
    assert fetched['task']['id'] in original_ids
    assert claim(http, fetched['task'], bb).status_code == 200
    record('ten-tasks', {'original_ids': sorted(original_ids), 'waiting': waiting, 'moved': moved, 'new_worker_pull': fetched})


@pytest.mark.parametrize('stage', ['flow_registered', 'claimed', 'completed_before_finish', 'finished'])
def test_started_task_stays_until_original_receipt_and_flow_finish(http, stage):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    task = tasks[0]
    api.start_inflight_flow(ba, flow_id=task['id'], flow_kind='task')
    completed_before = None
    if stage != 'flow_registered':
        result = data(claim(http, task, ba, task['id']))
        if stage in {'completed_before_finish', 'finished'}:
            h = headers(ba, task['id'])
            h['X-Task-Lease-Fencing-Token'] = str(result['lease_fencing_token'])
            completed_before = data(http.post(f"/api/tasks/{task['id']}/invite-sent", json={}, headers=h))
            if stage == 'finished':
                api.finish_inflight_flow(ba, flow_id=task['id'], terminal_kind='task_terminal')
    rebound = http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': wb['id']})
    assert rebound.status_code == (200 if stage == 'finished' else 409), rebound.text
    if stage != 'finished':
        assert rebound.json()['code'] == 'SALES_WORKER_REBIND_UNSETTLED'
        assert data(http.get(f"/api/sales/{sales['id']}"))['worker_id'] == wa['id']
        assert claim(http, task, bb).status_code == 409
    else:
        saved = data(http.get(f"/api/tasks/{task['id']}"))
        for key in ['worker_id','sales_id','status','result_code','completed_at']:
            assert saved[key] == completed_before[key]
        assert data(http.get(f"/api/workers/{wb['id']}/tasks/pull", headers=headers(bb)))['task'] is None
    record('settlement-'+stage, {'rebind_status': rebound.status_code, 'response': rebound.json(), 'original_task': data(http.get(f"/api/tasks/{task['id']}"))})


def test_stale_pulled_task_cannot_start_a_new_flow_or_operate_wechat(http):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    runner, errors = runner_for(api, ba)
    data(http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': wb['id']}))
    assert not runner._start_inflight_flow(ba, flow_id=tasks[0]['id'], flow_kind='task')
    assert runner._last_new_flow_block_reason == 'TASK_WORKER_MISMATCH'
    assert runner.binding.run_status == 'running'
    with SessionLocal() as db:
        assert not db.get(Worker, wa['id']).inflight_flow_state
        assert db.get(Task, tasks[0]['id']).status == 'pending'


def test_old_pending_assignment_reconciles_on_current_owner_pull(http):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    # A historical record from before this fix. No test writes its outcome.
    with SessionLocal() as db:
        db.get(Sales, sales['id']).worker_id = wb['id']
        db.commit()
    fetched = data(http.get(f"/api/workers/{wb['id']}/tasks/pull", headers=headers(bb)))
    assert fetched['task']['id'] == tasks[0]['id']
    assert fetched['task']['worker_id'] == wb['id']
    assert fetched['task']['execution']['worker']['id'] == wb['id']
    assert api.pull_task(ba)[1] is None
    assert claim(http, tasks[0], ba).status_code == 409
    assert claim(http, tasks[0], bb).status_code == 200


@pytest.mark.parametrize('first', ['rebind', 'claim'])
def test_postgres_two_connections_serialize_claim_and_rebind(http, first):
    from app.services import sales_service, task_service
    from app.schemas.sales import SalesWorkerBindRequest
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    task = tasks[0]
    responses, failures = [], []
    with SessionLocal() as first_db:
        actor = task_service.SYSTEM_TASK_LEASE_ACTOR
        if first == 'rebind':
            sales_service.bind_worker(first_db, sales['id'], SalesWorkerBindRequest(worker_id=wb['id']), actor)
        else:
            task_service.claim_task(first_db, task['id'], wa['id'], None, None, actor, require_worker_ready=True, client_instance_id=ba.client_instance_id)
        def second():
            try:
                response = (claim(http, task, ba) if first == 'rebind' else http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id': wb['id']}))
                responses.append(response)
            except BaseException as exc:
                failures.append(repr(exc))
        thread = threading.Thread(target=second)
        thread.start()
        observed_lock = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with engine.connect() as check:
                    observed_lock = bool(check.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")))
                if observed_lock:
                    break
                time.sleep(.01)
            assert observed_lock, 'second real transaction did not wait for the first transaction'
            first_db.commit()
        finally:
            first_db.rollback()
            thread.join(10)
        assert not thread.is_alive() and not failures, failures
        assert len(responses) == 1 and responses[0].status_code == 409, [r.text for r in responses]
    with SessionLocal() as db:
        row, owner = db.get(Task, task['id']), db.get(Sales, sales['id'])
        assert (row.worker_id, row.status, owner.worker_id) == ((wb['id'], 'pending', wb['id']) if first == 'rebind' else (wa['id'], 'running', wa['id']))
    record('concurrency-'+first, {'observed_database_lock': observed_lock, 'second_response': responses[0].json(), 'deadlock': False})


def test_legacy_pause_is_repaired_then_explicit_start_recovers_through_worker_threads(http, tmp_path):
    import subprocess
    from test_worker_fault_recovery import WORKER_PROCESS
    wa, api, binding = worker(http, 'old-pause')
    api.set_run_status(binding, 'faulted')
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'url': str(http.base_url).rstrip('/'), 'mode': 'success', 'persistence_probe': 'success', 'binding': {'worker_id': binding.worker_id, 'worker_token': binding.worker_token, 'client_instance_id': binding.client_instance_id}}))
    # Historical SQLite starts paused, while the backend already has a fault.
    program = WORKER_PROCESS.replace('Binding(**request["binding"],run_status="faulted")', 'Binding(**request["binding"],run_status="paused")')
    result = subprocess.run([sys.executable, '-c', program, str(request)], env={**os.environ, 'CHEJIN_WORKER_HOME': str(tmp_path/'old-client'), 'CHEJIN_TASK_POLL_INTERVAL':'0.1', 'CHEJIN_HEARTBEAT_INTERVAL':'0.1'}, capture_output=True, text=True, timeout=35)
    assert result.returncode == 0, result.stderr
    saved = json.loads(result.stdout.splitlines()[-1])
    record('old-paused-recovery', saved)
    assert saved['before'] == 'faulted' and saved['after'] == 'running', saved
    assert saved['recoveries'] == 1 and saved['pulls'] > 0
    assert any(p['recover_from_fault'] for p in saved['status_posts'])


def test_pending_with_prior_lease_is_never_reallocated_or_claimed_again(http):
    from app.models.base import utcnow
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    # Historical interrupted state: pending label cannot erase prior execution.
    with SessionLocal() as db:
        task = db.get(Task, tasks[0]['id'])
        task.claimed_at = utcnow()
        task.lease_fencing_token = 1
        db.commit()
    assert api.pull_task(ba)[1] is None
    assert claim(http, tasks[0], ba).status_code == 409
    rejected = http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id':wb['id']})
    assert rejected.status_code == 409 and rejected.json()['code'] == 'SALES_WORKER_REBIND_UNSETTLED'
    with SessionLocal() as db:
        assert db.get(Task, tasks[0]['id']).worker_id == wa['id']
        assert db.get(Task, tasks[0]['id']).lease_fencing_token == 1


def test_new_owner_must_be_ready_and_explicitly_running(http):
    wa, wb, api, ba, bb, sales, tasks = ownership_case(http)
    data(http.post(f"/api/workers/{wb['id']}/run-status", headers=headers(bb), json={'client_instance_id':bb.client_instance_id, 'run_status':'paused'}))
    data(http.post(f"/api/sales/{sales['id']}/worker-binding", json={'worker_id':wb['id']}))
    blocked = http.get(f"/api/workers/{wb['id']}/tasks/pull", headers=headers(bb))
    assert blocked.status_code == 409 and blocked.json()['code'] == 'WORKER_NEW_FLOW_NOT_ALLOWED'
    assert claim(http, tasks[0], bb).status_code == 409
    data(http.post(f"/api/workers/{wb['id']}/run-status", headers=headers(bb), json={'client_instance_id':bb.client_instance_id, 'run_status':'running'}))
    assert data(http.get(f"/api/workers/{wb['id']}/tasks/pull", headers=headers(bb)))['task']['id'] == tasks[0]['id']
    assert claim(http, tasks[0], bb).status_code == 200
