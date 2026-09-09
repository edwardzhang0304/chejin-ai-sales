"""Flow settlement regression, derived from the independent MECE probes.

Real isolated PostgreSQL, HTTP, Worker threads and SQLite. AI and physical
boundaries are controlled fixtures; this does not test real WeChat or async AI.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select
from conftest import authenticated_admin_dependency
from test_lead_followup_eligibility import http_api, isolated_db
import test_c3_api as c3t
from app.core.database import SessionLocal
from app.models.c3 import ReplyAction, SentAck
from app.models.task import Task
from app.models.worker import Worker


@pytest.mark.parametrize('send_result, proof_case, expected', [
    ('sent', 'intact', 200), ('unknown', 'intact', 200),
    ('unknown', 'hash_mismatch', 200),
    ('unknown', 'missing_ack', 409), ('unknown', 'wrong_token', 409),
    ('unknown', 'wrong_task', 409), ('unknown', 'sending', 409),
    ('unknown', 'task_running', 409), ('unknown', 'lease_present', 409),
])
def test_settled_reply_does_not_permanently_block_later_fault_recovery(http_api, monkeypatch, tmp_path, send_result, proof_case, expected):
    monkeypatch.setattr(c3t, 'client', http_api)
    worker, binding = c3t._setup_bound_conversation()
    message = c3t._ingest(worker, binding['conversation_id'], 'audit-terminal-message', '想了解15万SUV')
    generated = c3t._generate(c3t._collect(binding['conversation_id'], message)['batch_id'])
    task_id, action_id = generated['task_id'], generated['reply_action_id']
    claimed = http_api.post(f'/api/tasks/{task_id}/claim', headers=c3t._worker_headers(worker), json={
        'worker_id': worker['id'], 'claim_source': 'c2_conversation_flow', 'conversation_id': binding['conversation_id']})
    assert claimed.status_code == 200, claimed.text
    send = http_api.post(f'/api/reply-actions/{action_id}/claim-send',
        headers=c3t._task_lease_headers(worker, claimed), json={'task_id': task_id, 'worker_id': worker['id']})
    assert send.status_code == 200, send.text
    claim = send.json()['data']
    ack_payload = {'task_id': task_id, 'worker_id': worker['id'], 'client_instance_id': 'client-c3',
        'send_token': claim['send_token'], 'reply_text_hash': claim['reply_text_hash'],
        'send_result': send_result, 'action_phase': 'confirmed' if send_result == 'sent' else 'trigger_attempted',
        'error_code': None if send_result == 'sent' else 'SEND_RESULT_UNKNOWN'}
    if proof_case == 'hash_mismatch':
        ack_payload['reply_text_hash'] = '0' * 64
    ack = http_api.post(f'/api/reply-actions/{action_id}/sent-ack', headers=c3t._worker_headers(worker), json=ack_payload)
    assert ack.status_code == 200, ack.text
    replay = http_api.post(f'/api/reply-actions/{action_id}/sent-ack', headers=c3t._worker_headers(worker), json=ack_payload)
    assert replay.status_code == 200, replay.text
    with SessionLocal() as db:
        ack_row = db.scalar(select(SentAck).where(SentAck.reply_action_id == action_id))
        action_row, task_row = db.get(ReplyAction, action_id), db.get(Task, task_id)
        if proof_case == 'missing_ack': db.delete(ack_row)
        if proof_case == 'wrong_token': ack_row.send_token = 'different-token'
        if proof_case == 'wrong_task': action_row.claimed_task_id = 'different-task'
        if proof_case == 'sending': action_row.status = 'sending'
        if proof_case == 'task_running': task_row.status = 'running'
        if proof_case == 'lease_present': task_row.lease_owner_worker_id = worker['id']
        db.commit()
    status_url = f"/api/workers/{worker['id']}/run-status"
    stopped = http_api.post(status_url, headers=c3t._worker_headers(worker),
        json={'client_instance_id': 'client-c3', 'run_status': 'faulted'})
    assert stopped.status_code == 200, stopped.text
    responses = [http_api.post(status_url, headers=c3t._worker_headers(worker),
        json={'client_instance_id': 'client-c3', 'run_status': 'running', 'recover_from_fault': True}) for _ in range(3)]
    with SessionLocal() as db:
        task, action, owner = db.get(Task, task_id), db.get(ReplyAction, action_id), db.get(Worker, worker['id'])
        evidence = {'send_result': send_result, 'ack_http': ack.status_code, 'duplicate_ack_http': replay.status_code,
            'task_status': task.status, 'lease_owner': task.lease_owner_worker_id, 'lease_until': str(task.lease_expires_at),
            'action_status': action.status, 'ack_persisted': db.scalar(select(SentAck.id).where(SentAck.reply_action_id == action_id)) is not None,
            'worker_flow': owner.inflight_flow_state, 'worker_current_task': owner.current_task,
            'recovery_projection': stopped.json()['data']['fault_recovery'],
            'recoveries': [{'http': r.status_code, 'code': r.json()['code']} for r in responses]}
    (tmp_path / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    assert all(response.status_code == expected for response in responses), evidence
    if expected == 200:
        assert evidence['task_status'] in {'completed', 'failed'} and evidence['ack_persisted']
        assert evidence['worker_current_task'] is None and not evidence['worker_flow'] and evidence['lease_owner'] is None
        assert evidence['action_status'] == ('sent' if send_result == 'sent' else 'unknown_send_result')
        # Recovery cannot revive this action or erase its terminal no-resend history.
        old_claim = http_api.post(f'/api/reply-actions/{action_id}/claim-send',
            headers=c3t._worker_headers(worker), json={'task_id': task_id, 'worker_id': worker['id']})
        assert old_claim.status_code == 409, old_claim.text
    else:
        assert all(response.json()['code'] == 'WORKER_FAULT_RECOVERY_NOT_READY' for response in responses)
        with SessionLocal() as db:
            assert db.get(Worker, worker['id']).run_status == 'faulted'


WORKER_SCRIPT = r'''
import json,sys,time
from pathlib import Path
from test_task_runner import FakeBridge
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.models import Binding,RpaResult
from chejin_worker_client.storage import save_binding,load_binding,load_runtime_control
from chejin_worker_client.action_journal import action_journal_path
request=json.loads(Path(sys.argv[1]).read_text())
class Transport(WorkerApiClient):
    def __init__(self):
        super().__init__(request['url']+'/api'); self.finishes=0; self.finish_requests=[]; self.injected=0; self.heartbeats=0; self.pulls=0; self.pulls_after_finish=0
    def _request(self, method, path, **kwargs):
        if path.endswith('/inflight-flow/finish'):
            self.finishes+=1
            self.finish_requests.append(dict(kwargs.get('json_body') or kwargs.get('json') or {}))
            if request['mode']=='before' and not self.injected:
                self.injected+=1; raise TimeoutError('audit: finish request unavailable once')
            result=super()._request(method,path,**kwargs)
            if request['mode']=='after' and not self.injected:
                self.injected+=1; raise TimeoutError('audit: committed finish response lost once')
            return result
        if path.endswith('/heartbeat'): self.heartbeats+=1
        if path.endswith('/pull'):
            self.pulls+=1
            if self.finishes: self.pulls_after_finish+=1
        return super()._request(method,path,**kwargs)
class DesktopBoundary(FakeBridge):
    def __init__(self): super().__init__(RpaResult(ok=True,result_code='invite_sent',message='synthetic physical success')); self.actions=0
    def add_friend_transaction_journal_path(self, task_id): return action_journal_path('add_friend',task_id)
    def probe(self): return ('ready','logged_in')
    def sidecar_active(self): return False
    def prepare_startup_layout_for_new_transaction(self): return {'ok':True}
    def run_add_friend(self, task, emit_step, cancel_check=None):
        self.actions+=1
        return RpaResult(ok=True,result_code='invite_sent',message='synthetic physical success')
api=Transport(); bridge=DesktopBoundary(); errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda p:None,on_status=lambda x:None,on_step=lambda x:None,
    on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
binding=Binding(**request['binding'],run_status='running'); save_binding(binding)
runner.start(load_binding())
try:
    deadline=time.monotonic()+12
    while time.monotonic()<deadline and not api.finishes: time.sleep(.05)
    at_finish=api.heartbeats
    deadline=time.monotonic()+5
    while time.monotonic()<deadline and api.heartbeats<at_finish+10: time.sleep(.05)
    result={'mode':request['mode'],'finishes':api.finishes,'injected':api.injected,'heartbeats_after':api.heartbeats-at_finish,
        'runtime':load_runtime_control(),'backend_flow':runner._backend_inflight_flow_state,
        'current_task':runner.current_task.id if runner.current_task else None,'restart_flow':runner._restart_recovery_flow_id,
        'run_status':runner.binding.run_status,'can_start':runner._can_start_new_flow(),
        'errors':errors,'pulls':api.pulls,'pulls_after_finish':api.pulls_after_finish,'physical_actions':bridge.actions,'health':runner.post_update_runtime_health_snapshot()}
finally: runner.stop_for_update(timeout_seconds=10)
print(json.dumps(result))
'''


@pytest.mark.parametrize('mode', ['control', 'before', 'after'])
def test_live_owner_retries_finalization_after_one_transport_failure(http_api, monkeypatch, tmp_path, mode):
    monkeypatch.setattr(c3t, 'client', http_api)
    worker = c3t._create_worker()
    with SessionLocal() as db:
        task = Task(worker_id=worker['id'], task_type='add_friend', status='pending')
        db.add(task); db.commit(); task_id=task.id
    url = http_api.get('/healthz').url.removesuffix('/healthz')
    request = tmp_path/'request.json'
    request.write_text(json.dumps({'mode':mode,'url':url,'binding':{
        'worker_id':worker['id'],'worker_token':worker['worker_token'],'client_instance_id':'client-c3'}}))
    proc=subprocess.run([sys.executable,'-B','-c',WORKER_SCRIPT,str(request)],capture_output=True,text=True,timeout=35,
        env={**os.environ,'PYTHONPATH':os.pathsep.join(str(p) for p in (c3t.WORKER_CLIENT_ROOT, c3t.WORKER_CLIENT_ROOT/'tests', c3t.OMNIAUTO_ROOT)),
            'CHEJIN_WORKER_HOME':str(tmp_path/'worker'),'CHEJIN_C2_ENABLED':'true',
            'CHEJIN_TASK_POLL_INTERVAL':'0.1','CHEJIN_HEARTBEAT_INTERVAL':'0.1'})
    assert proc.returncode==0,proc.stderr
    evidence=json.loads(proc.stdout.splitlines()[-1])
    with SessionLocal() as db:
        owner=db.get(Worker,worker['id']); task=db.get(Task,task_id)
        evidence['persisted_backend']={'task_status':task.status,'flow':owner.inflight_flow_state,'worker_run_status':owner.run_status}
    (tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert evidence['persisted_backend']['task_status']=='completed',evidence
    assert evidence['physical_actions']==1 and evidence['health']['ready'] and evidence['heartbeats_after']>=10,evidence
    assert evidence['injected']==(0 if mode=='control' else 1)
    assert not evidence['runtime']['inflight_flow_id'],evidence
    assert not evidence['persisted_backend']['flow'] and evidence['can_start'],evidence
    assert evidence['pulls_after_finish'] >= 1,evidence
    assert evidence['finishes'] == (1 if mode == 'control' else 2),evidence


def _registered_terminal_task(http_api, monkeypatch):
    monkeypatch.setattr(c3t, 'client', http_api)
    worker = c3t._create_worker()
    with SessionLocal() as db:
        task = Task(worker_id=worker['id'], task_type='add_friend', status='completed')
        db.add(task); db.commit(); task_id = task.id
    path = f"/api/workers/{worker['id']}"
    headers = {**c3t._worker_headers(worker), 'X-Inflight-Flow-Id': task_id}
    start = http_api.post(path+'/inflight-flow/start', headers=headers,
        json={'flow_id': task_id, 'flow_kind': 'task'})
    assert start.status_code == 200, start.text
    payload = {'flow_id': task_id, 'terminal_kind': 'task_terminal', 'conversation_id': None, 'error_code': None}
    return worker, path, headers, payload


@pytest.mark.parametrize('status', ['running', 'paused', 'faulted'])
def test_finish_receipt_is_idempotent_under_concurrent_http_and_pause(http_api, monkeypatch, status):
    from concurrent.futures import ThreadPoolExecutor
    from app.models.audit import OperationLog
    worker, path, headers, payload = _registered_terminal_task(http_api, monkeypatch)
    if status != 'running':
        response = http_api.post(path+'/run-status', headers=headers,
            json={'client_instance_id':'client-c3', 'run_status':status})
        assert response.status_code == 200, response.text
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: http_api.post(path+'/inflight-flow/finish', headers=headers, json=payload), range(2)))
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    with SessionLocal() as db:
        assert not db.get(Worker, worker['id']).inflight_flow_state
        assert db.get(Worker, worker['id']).run_status == status
        logs = list(db.scalars(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished')))
        assert len(logs) == 1
        assert logs[0].after_data['flow_id'] == payload['flow_id']


@pytest.mark.parametrize('damage', ['missing_proof', 'flow', 'conversation', 'terminal', 'error', 'header', 'instance', 'token', 'binding_epoch'])
def test_finish_replay_rejects_missing_or_mismatched_proof(http_api, monkeypatch, damage):
    from datetime import timedelta
    from app.models.audit import OperationLog
    worker, path, headers, payload = _registered_terminal_task(http_api, monkeypatch)
    first = http_api.post(path+'/inflight-flow/finish', headers=headers, json=payload)
    assert first.status_code == 200, first.text
    with SessionLocal() as db:
        if damage == 'missing_proof':
            db.delete(db.scalar(select(OperationLog).where(OperationLog.event_type == 'worker_inflight_finished')))
        if damage == 'binding_epoch': db.get(Worker, worker['id']).bound_at += timedelta(seconds=1)
        db.commit()
    if damage == 'flow': payload['flow_id'] = headers['X-Inflight-Flow-Id'] = 'different-flow'
    if damage == 'conversation': payload['conversation_id'] = 'different-conversation'
    if damage == 'terminal': payload['terminal_kind'] = 'read_confirmed'
    if damage == 'error': payload['error_code'] = 'DIFFERENT_ERROR'
    if damage == 'header': headers['X-Inflight-Flow-Id'] = 'different-flow'
    if damage == 'instance': headers['X-Client-Instance-Id'] = 'different-instance'
    if damage == 'token': headers['X-Worker-Token'] = 'different-token'
    replay = http_api.post(path+'/inflight-flow/finish', headers=headers, json=payload)
    assert replay.status_code == (401 if damage in {'instance','token'} else 409), replay.text


def test_old_finish_does_not_clear_a_new_flow(http_api, monkeypatch):
    worker, path, headers, payload = _registered_terminal_task(http_api, monkeypatch)
    assert http_api.post(path+'/inflight-flow/finish', headers=headers, json=payload).status_code == 200
    start = http_api.post(path+'/inflight-flow/start', headers=headers,
        json={'flow_id': 'another-flow', 'flow_kind':'task'})
    assert start.status_code == 200, start.text
    replay = http_api.post(path+'/inflight-flow/finish', headers=headers, json=payload)
    assert replay.status_code == 200, replay.text
    with SessionLocal() as db:
        assert db.get(Worker, worker['id']).inflight_flow_state['flow_id'] == 'another-flow'


RESTART_SCRIPT = WORKER_SCRIPT.split('api=Transport();')[0] + r'''
from chejin_worker_client.storage import load_c2_state
api=Transport(); bridge=DesktopBoundary(); errors=[]
runner=TaskRunner(api,bridge,on_profile=lambda p:None,on_status=lambda x:None,on_step=lambda x:None,
    on_task=lambda x:None,on_result=lambda x:None,on_error=errors.append)
if request['stage']=='prepare':
    binding=Binding(**request['binding'],run_status='running'); save_binding(binding); runner.binding=binding
    assert runner._start_inflight_flow(binding,flow_id=request['flow_id'],flow_kind=request['kind'],conversation_id=request.get('conversation_id'),unread_generation=0 if request['kind']=='c2_read' else None)
    caught=False
    try:
        runner._finish_inflight_flow(binding,request['flow_id'],terminal_kind=request['terminal'],
            conversation_id=request.get('conversation_id'),error_code=request.get('error_code'))
    except TimeoutError: caught=True
    assert caught
    result={'runtime':load_runtime_control(),'receipt':bool(load_c2_state(runner._inflight_finish_receipt_key(request['flow_id'])).get('finish_request')),
        'physical_actions':bridge.actions,'finishes':api.finishes,'waiting':runner.flow_finish_wait_reason,'can_start':runner._can_start_new_flow()}
else:
    runner.start(load_binding())
    try:
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and (load_runtime_control()['inflight_flow_id'] or not api.pulls): time.sleep(.05)
        result={'runtime':load_runtime_control(),'physical_actions':bridge.actions,'finishes':api.finishes,'finish_requests':api.finish_requests,
            'health':runner.post_update_runtime_health_snapshot(),'pulls':api.pulls,'waiting':runner.flow_finish_wait_reason,'errors':errors}
    finally: runner.stop_for_update(timeout_seconds=10)
print(json.dumps(result))
'''


@pytest.mark.parametrize('kind', ['task', 'c2_read'])
@pytest.mark.parametrize('failure', ['before', 'after'])
def test_finish_intent_survives_process_exit_without_repeating_actions(http_api, monkeypatch, tmp_path, kind, failure):
    import uuid
    monkeypatch.setattr(c3t, 'client', http_api)
    if kind == 'c2_read':
        worker, binding = c3t._setup_bound_conversation()
        flow_id, conversation_id = str(uuid.uuid4()), binding['conversation_id']
        # _setup_bound_conversation seeds a friend-active conversation but
        # leaves its automatic add_friend task pending. Complete that setup-only
        # fixture before creating the C2 Flow under test; otherwise successful
        # intake recovery correctly executes a different task during assertions.
        with SessionLocal() as db:
            seeded = list(db.scalars(select(Task).where(
                Task.worker_id == worker['id'], Task.task_type == 'add_friend',
                Task.status == 'pending',
            )))
            assert len(seeded) == 1
            for task in seeded:
                task.status = 'completed'
            db.commit()
    else:
        worker = c3t._create_worker(); conversation_id = None
        with SessionLocal() as db:
            task = Task(worker_id=worker['id'], task_type='add_friend', status='completed')
            db.add(task); db.commit(); flow_id=task.id
    data={'kind':kind,'flow_id':flow_id,'conversation_id':conversation_id,
        'terminal':'task_terminal' if kind=='task' else 'failed_before_message_action',
        'error_code':None if kind=='task' else 'WECHAT_TARGET_NOT_FOUND',
        'url':http_api.get('/healthz').url.removesuffix('/healthz'),
        'binding':{'worker_id':worker['id'],'worker_token':worker['worker_token'],'client_instance_id':'client-c3'}}
    evidence={}
    for stage in ['prepare','resume']:
        request=tmp_path/'request.json'
        request.write_text(json.dumps({**data,'stage':stage,'mode':failure if stage=='prepare' else 'control'}))
        proc=subprocess.run([sys.executable,'-B','-c',RESTART_SCRIPT,str(request)],capture_output=True,text=True,timeout=25,
            env={**os.environ,'PYTHONPATH':os.pathsep.join(str(p) for p in (c3t.WORKER_CLIENT_ROOT,c3t.WORKER_CLIENT_ROOT/'tests',c3t.OMNIAUTO_ROOT)),
                'CHEJIN_WORKER_HOME':str(tmp_path/'same-worker'),'CHEJIN_C2_ENABLED':'true',
                'CHEJIN_TASK_POLL_INTERVAL':'0.1','CHEJIN_HEARTBEAT_INTERVAL':'0.1'})
        assert proc.returncode==0,proc.stderr
        evidence[stage]=json.loads(proc.stdout.splitlines()[-1])
    (tmp_path/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    assert evidence['prepare']['receipt'] and not evidence['prepare']['can_start']
    assert evidence['prepare']['waiting']
    assert sum(item['flow_id'] == flow_id for item in evidence['resume']['finish_requests']) == 1, evidence
    assert evidence['resume']['finishes'] == 1 and not evidence['resume']['runtime']['inflight_flow_id']
    assert evidence['resume']['pulls']>=1 and evidence['resume']['health']['ready']
    assert not evidence['resume']['waiting']
    assert evidence['prepare']['physical_actions']==evidence['resume']['physical_actions']==0
    with SessionLocal() as db:
        assert not db.get(Worker,worker['id']).inflight_flow_state


@pytest.mark.parametrize('damage', ['invalid_flow_status', 'unfinished_task'])
def test_first_finish_preserves_existing_flow_and_task_guards(http_api, monkeypatch, damage):
    from app.models.audit import OperationLog
    worker, path, headers, payload = _registered_terminal_task(http_api, monkeypatch)
    with SessionLocal() as db:
        if damage == 'invalid_flow_status':
            owner = db.get(Worker, worker['id'])
            owner.inflight_flow_state = {**owner.inflight_flow_state, 'status':'unrecognized'}
        else:
            db.get(Task, payload['flow_id']).status = 'running'
        db.commit()
    response = http_api.post(path+'/inflight-flow/finish', headers=headers, json=payload)
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.get(Worker, worker['id']).inflight_flow_state['flow_id'] == payload['flow_id']
        assert not db.scalar(select(OperationLog.id).where(OperationLog.event_type == 'worker_inflight_finished'))
