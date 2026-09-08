"""Restart race: real production loops, HTTP, PostgreSQL and Worker SQLite.

Only schedule the HTTP/lock boundary. No replacement finish decisions or
responses, no test-written terminal receipts or local Flow cleanup.
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select, func

from test_lead_followup_eligibility import (
    isolated_db, http_api, fixture_rows, SessionLocal, Worker, client, headers,
)
from app.models.wechat import MessageEvent
from app.models.c3 import ReplyAction

ROOT = Path(__file__).resolve().parents[2]


def _worker_script():
    tree = ast.parse((ROOT / 'backend/tests/test_lead_followup_p1_regressions.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'test_invalidation_after_final_authorization_before_ingest')
    return next(n.value.value for n in function.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'script' for t in n.targets))


@pytest.mark.parametrize('run_status', ['running', 'paused'])
@pytest.mark.parametrize('crash_boundary', ['lost_response_restart', 'confirmed_restart'])
@pytest.mark.parametrize('finish_owner', ['task', 'listener'])
def test_actual_restart_threads_finish_once(http_api, tmp_path, run_status, crash_boundary, finish_owner):
    w, rows = fixture_rows()
    base = http_api.get('/healthz').url.removesuffix('/healthz')
    script = _worker_script().replace(
        "run_status='running' if phase=='read' else 'paused'",
        "run_status='running' if phase=='read' else os.environ['RESTART_TEST_STATUS']",
    )
    script = script.replace('original=api.session.send', r'''
import threading
schedule_lock=threading.Lock()
second_arrived=threading.Event()
first_http_seen=threading.Event()
contended_threads=[]
finish_threads=[]
waiter_returned=threading.Event()

class ObservedRecoveryLock:
    # Delegate to the production RLock; only observe and schedule contention.
    def __init__(self, delegate): self.delegate=delegate
    def __enter__(self):
        if os.environ['FINISH_TEST_OWNER']=='listener' and threading.current_thread().name=='CheJinWorkerTaskRunner' and not first_http_seen.is_set():
            # Heartbeat already supplied the real backend Flow. Let the formal
            # C2 listener own recovery, then contend at the real RLock.
            assert first_http_seen.wait(10), 'Listener did not reach finish'
        if self.delegate.acquire(blocking=False): return self
        name=threading.current_thread().name
        with schedule_lock: contended_threads.append(name)
        second_arrived.set()
        self.delegate.acquire()
        return self
    def __exit__(self, *args):
        self.delegate.release()
        if first_http_seen.is_set() and threading.current_thread().name in contended_threads:
            waiter_returned.set()

if phase=='restart' and hasattr(runner,'_restart_recovery_lock'):
    runner._restart_recovery_lock=ObservedRecoveryLock(runner._restart_recovery_lock)
original=api.session.send''')
    script = script.replace('    response=original(request,**kwargs)', r'''
    if phase=='restart' and request.url.endswith('/inflight-flow/finish'):
        with schedule_lock:
            finish_threads.append(threading.current_thread().name)
            first=len(finish_threads)==1
        if first:
            first_http_seen.set()
            assert second_arrived.wait(10), 'Other production loop did not contend'
        else:
            # With the fix disabled both enter HTTP, matching the independent
            # review: let first cleanup commit, then submit the stale request.
            second_arrived.set()
            deadline=time.monotonic()+10
            while load_runtime_control()['inflight_flow_id'] and time.monotonic()<deadline:
                time.sleep(.01)
    response=original(request,**kwargs)''')
    script = script.replace("'phase':phase,'url':request.url", "'phase':phase,'thread':threading.current_thread().name,'url':request.url")
    script = script.replace('    runner.stop_for_update(timeout_seconds=5)', r'''
    deadline=time.monotonic()+3
    while not waiter_returned.is_set() and not errors and time.monotonic()<deadline:
        time.sleep(.02)
    # Capture before shutdown, after the losing contender has returned.
    before_stop={'binding_run_status':binding.run_status,'runtime':load_runtime_control(),'errors':list(errors),'waiter_returned':waiter_returned.is_set(),'contended_threads':contended_threads,'finish_threads':finish_threads,'ui_operations':{'searches':len(bridge.locate_chats),'reads':len(bridge.message_reads),'sends':len(bridge.sent_replies),'voice':len(bridge.voice_transcribes),'scans':len(bridge.session_scans)}}
    runner.stop_for_update(timeout_seconds=5)''')
    script = script.replace("'result_ok':result.get('ok')", "'before_stop':globals().get('before_stop'),'result_ok':result.get('ok')")
    path = tmp_path / 'worker.py'
    path.write_text(script)
    env = {**os.environ, 'RESTART_TEST_STATUS':run_status, 'FINISH_TEST_OWNER':finish_owner,
           'CHEJIN_WORKER_HOME':str(tmp_path/'worker'), 'CHEJIN_RPA_MODE':'mock',
           'CHEJIN_UI_LOCK_LEASE_SECONDS':'1', 'CHEJIN_TASK_POLL_INTERVAL':'0.1',
           'PYTHONPATH':os.pathsep.join([str(ROOT/p) for p in ('worker-client','worker-client/tests','worker-client/omniauto-rpa')]+[os.environ.get('PYTHONPATH','')])}
    args = [sys.executable, str(path), base, json.dumps(w), json.dumps(rows[0]), crash_boundary]
    first = subprocess.run([*args, 'read'], env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 17, first.stderr
    response = client.post(f"/api/workers/{w['id']}/run-status", headers=headers(w), json={'run_status':run_status,'client_instance_id':'followup-test'})
    assert response.status_code == 200, response.text
    second = subprocess.run([*args, 'restart'], env=env, capture_output=True, text=True, timeout=45)
    assert second.returncode == 0, second.stderr
    proof = json.loads(second.stdout.strip().splitlines()[-1])
    with SessionLocal() as db:
        worker = db.get(Worker, w['id'])
        proof['backend'] = {'run_status':worker.run_status,'flow':worker.inflight_flow_state,
                            'facts':db.scalar(select(func.count()).select_from(MessageEvent)),
                            'replies':db.scalar(select(func.count()).select_from(ReplyAction))}
    out = Path(os.environ.get('RESTART_FINISH_EVIDENCE_DIR', str(tmp_path)))
    out.mkdir(parents=True, exist_ok=True)
    (out/f'{finish_owner}-{run_status}-{crash_boundary}.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))
    before = proof['before_stop']
    assert before['binding_run_status'] == run_status and not before['errors'], proof
    assert before['waiter_returned'] and before['contended_threads'], proof
    finishes = [x for x in proof['exchanges'] if x.get('finish_request')]
    assert len(finishes) == 1 and finishes[0]['status'] == 200, proof
    if finish_owner=='listener': assert finishes[0]['thread']=='CheJinWorkerC2Listener',proof
    assert finishes[0]['finish_request']['terminal_kind'] == 'read_cancelled', proof
    assert not before['runtime']['inflight_flow_id'] and not proof['backend']['flow'], proof
    assert proof['backend']['run_status'] == run_status, proof
    if run_status == 'running': assert not before['runtime']['pause_requested'], proof
    assert not any(before['ui_operations'].values()), proof
    assert not proof['pending'] and not proof['locked'], proof
    assert proof['backend']['facts'] == 1 and proof['backend']['replies'] == 0, proof
    # Both *formal* production threads participated, not two test-created workers.
    assert set(before['contended_threads'] + before['finish_threads']) == {'CheJinWorkerC2Listener','CheJinWorkerTaskRunner'}, proof


@pytest.mark.parametrize('run_status', ['running', 'paused'])
def test_finish_success_response_lost_then_restart(http_api, tmp_path, run_status):
    """Server finish commits; crash before local cleanup, then real restart."""
    w, rows = fixture_rows()
    base = http_api.get('/healthz').url.removesuffix('/healthz')
    script = _worker_script().replace(
        "run_status='running' if phase=='read' else 'paused'",
        "run_status='running' if phase=='read' else os.environ['RESTART_TEST_STATUS']",
    )
    script = script.replace('    return response\napi.session.send=send', '''    if phase=='restart' and request.url.endswith('/inflight-flow/finish') and os.environ.get('CRASH_AFTER_FINISH')=='1':
        assert response.status_code==200
        os._exit(19)  # Server committed; local receipt/pointer remain untouched.
    return response
api.session.send=send''')
    script = script.replace('    runner.stop_for_update(timeout_seconds=5)', '''    time.sleep(.3)
    before_stop={'binding_run_status':binding.run_status,'runtime':load_runtime_control(),'errors':list(errors),'ui_operations':{'searches':len(bridge.locate_chats),'reads':len(bridge.message_reads),'sends':len(bridge.sent_replies),'scans':len(bridge.session_scans)}}
    runner.stop_for_update(timeout_seconds=5)''')
    script = script.replace("'result_ok':result.get('ok')", "'before_stop':globals().get('before_stop'),'result_ok':result.get('ok')")
    path = tmp_path/'worker.py'; path.write_text(script)
    env = {**os.environ, 'RESTART_TEST_STATUS':run_status,
           'CHEJIN_WORKER_HOME':str(tmp_path/'worker'), 'CHEJIN_RPA_MODE':'mock',
           'CHEJIN_UI_LOCK_LEASE_SECONDS':'1', 'CHEJIN_TASK_POLL_INTERVAL':'0.1',
           'PYTHONPATH':os.pathsep.join([str(ROOT/p) for p in ('worker-client','worker-client/tests','worker-client/omniauto-rpa')]+[os.environ.get('PYTHONPATH','')])}
    args = [sys.executable, str(path), base, json.dumps(w), json.dumps(rows[0]), 'lost_response_restart']
    first = subprocess.run([*args,'read'],env=env,capture_output=True,text=True,timeout=45)
    assert first.returncode==17, first.stderr
    response=client.post(f"/api/workers/{w['id']}/run-status",headers=headers(w),json={'run_status':run_status,'client_instance_id':'followup-test'})
    assert response.status_code==200,response.text
    second = subprocess.run([*args,'restart'],env={**env,'CRASH_AFTER_FINISH':'1'},capture_output=True,text=True,timeout=45)
    assert second.returncode==19,second.stderr
    with SessionLocal() as db:
        assert not db.get(Worker,w['id']).inflight_flow_state
    third = subprocess.run([*args,'restart'],env=env,capture_output=True,text=True,timeout=45)
    assert third.returncode==0,third.stderr
    proof=json.loads(third.stdout.strip().splitlines()[-1])
    with SessionLocal() as db:
        worker=db.get(Worker,w['id'])
        proof['backend']={'run_status':worker.run_status,'flow':worker.inflight_flow_state,'facts':db.scalar(select(func.count()).select_from(MessageEvent))}
    out=Path(os.environ.get('RESTART_FINISH_EVIDENCE_DIR',str(tmp_path)));out.mkdir(parents=True,exist_ok=True)
    (out/f'{run_status}-finish-response-lost.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))
    before=proof['before_stop']
    assert before['binding_run_status']==run_status and not before['errors'],proof
    assert proof['backend']['run_status']==run_status and not proof['backend']['flow'],proof
    assert not before['runtime']['inflight_flow_id'],proof
    if run_status=='running':assert not before['runtime']['pause_requested'],proof
    finishes=[x for x in proof['exchanges'] if x.get('finish_request')]
    assert len(finishes)==1 and finishes[0]['status']==200,proof
    assert not proof['pending'] and not proof['locked'],proof
    assert not any(before['ui_operations'].values()),proof
    assert proof['backend']['facts']==1,proof
