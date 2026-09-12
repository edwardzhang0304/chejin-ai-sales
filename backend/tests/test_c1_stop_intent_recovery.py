"""Crash at a real persisted-result boundary, then use the same SQLite.

No failure/finish/resume request is supplied by this test. The only production
method replacement exits the first process immediately after a real commit.
"""
import json
import shutil

import pytest
from sqlalchemy import select
from test_lead_followup_eligibility import isolated_db, http_api, fixture_rows
import test_worker_failure_consistency as submitted
from app.core.database import SessionLocal
from app.models.task import Task
from app.models.worker import Worker
from app.enums import ContactType
from app.services.lead_service import _contact_model
from app.services import contact_utils


def backend_state(worker_id):
    with SessionLocal() as db:
        worker = db.get(Worker, worker_id)
        return {
            'status': worker.run_status, 'flow': worker.inflight_flow_state,
            'tasks': [{'id': t.id, 'status': t.status, 'error_code': t.error_code}
                      for t in db.scalars(select(Task).where(Task.worker_id == worker_id))],
        }


@pytest.mark.parametrize('boundary,code,expected,restart_injection', [
    pytest.param('receipt_saved','WECHAT_UI_LAYOUT_UNRESOLVED','faulted','',id='receipt_saved'),
    pytest.param('stop_saved','WECHAT_UI_LAYOUT_UNRESOLVED','faulted','',id='stop_saved'),
    pytest.param('pause_saved','WECHAT_UI_LAYOUT_UNRESOLVED','faulted','',id='pause_saved'),
    pytest.param('binding_saved','WECHAT_UI_LAYOUT_UNRESOLVED','faulted','',id='binding_saved'),
    pytest.param('receipt_saved','WECHAT_WINDOW_NOT_FOUND','paused','',id='environment_pause'),
    pytest.param('receipt_saved','PHONE_NOT_FOUND','running','',id='business_failure_continues'),
    pytest.param('receipt_saved','WECHAT_UI_LAYOUT_UNRESOLVED','faulted','save_before',id='restore_save_before'),
    pytest.param('receipt_saved','WECHAT_UI_LAYOUT_UNRESOLVED','faulted','save_after',id='restore_save_after'),
    pytest.param('receipt_saved','WECHAT_UI_LAYOUT_UNRESOLVED','faulted','run-status:before',id='restore_status_network'),
])
def test_saved_technical_failure_cannot_restart_intake_without_click(http_api, tmp_path, monkeypatch, boundary, code, expected, restart_injection):
    worker, rows = fixture_rows()
    with SessionLocal() as db:
        for index, row in enumerate(rows):
            db.add(_contact_model(row['lead_id'], ContactType.phone,
                                 contact_utils.normalize_phone(f'1380000444{index}'), True))
            db.add(Task(lead_id=row['lead_id'], worker_id=worker['id'],
                        task_type='add_friend', status='pending'))
        db.commit()
    request = {'base_url': http_api.get('/healthz').url.removesuffix('/healthz') + '/api',
               'worker_id': worker['id'], 'token': worker['worker_token'],
               'code': code, 'boundary': boundary, 'expected': expected}
    first = submitted.run_worker(tmp_path, r'''
import os
from chejin_worker_client.storage import load_c2_state
def crash():
    flow = load_runtime_control()['inflight_flow_id']
    print(json.dumps({'runtime':load_runtime_control(),'saved_status':load_binding().run_status,
        'memory_status':binding.run_status,'calls':bridge.calls,'http':events,
        'receipt':load_c2_state(runner._inflight_finish_receipt_key(flow))}),flush=True)
    os._exit(0)
if request['boundary']=='receipt_saved':
    original_save = runner._save_add_friend_failure
    def commit_then_exit(*args, **kwargs):
        original_save(*args, **kwargs)
        crash()
    runner._save_add_friend_failure = commit_then_exit
elif request['boundary'] in {'pause_saved','binding_saved'}:
    import chejin_worker_client.task_runner as module
    original_binding_save = module.save_binding
    def commit_binding_then_exit(value):
        if value.run_status == 'faulted':
            if request['boundary']=='binding_saved':
                original_binding_save(value)
            crash()
        return original_binding_save(value)
    module.save_binding = commit_binding_then_exit
else:
    runner._deliver_add_friend_failure = lambda *args, **kwargs: crash()
runner.tick_once()
raise AssertionError('Crash boundary was not reached')
''', request)
    first['backend'] = backend_state(worker['id'])
    (tmp_path/'first-evidence.json').write_text(json.dumps(first, indent=2))
    for name in ('worker.py','worker.stdout','worker.stderr','input.json'):
        shutil.copy2(tmp_path/name, tmp_path/('first-'+name))
    assert first['calls'] == 1
    assert first['receipt']['task_failure']['error_code'] == request['code']
    assert first['receipt']['task_failure_confirmed'] is False
    assert sorted(t['status'] for t in first['backend']['tasks']) == ['pending','running']
    # Load, do not replace or change the original binding. A stale running
    # binding is an observed product state, not an artificial restart fixture.
    needle = "binding=Binding(request['worker_id'],request['token'],'followup-test',run_status='running')\nsave_binding(binding)"
    assert submitted.COMMON.count(needle) == 1
    monkeypatch.setattr(submitted,'COMMON',submitted.COMMON.replace(needle,'binding=load_binding()'))
    request['restart_injection'] = restart_injection
    request['interruption'] = restart_injection if restart_injection.startswith('run-status:') else ''
    restarted = submitted.run_worker(tmp_path, r'''
import time
import chejin_worker_client.task_runner as module
restore_failures = []
original_binding_save = module.save_binding
def save_with_one_failure(value):
    fault = request['restart_injection']
    if fault in {'save_before','save_after'} and value.run_status=='faulted' and not restore_failures:
        if fault=='save_after': original_binding_save(value)
        restore_failures.append({'memory':binding.run_status,'saved':load_binding().run_status,
                                 'pause':load_runtime_control()['pause_requested']})
        raise OSError('controlled stop-state persistence failure')
    return original_binding_save(value)
module.save_binding = save_with_one_failure
# Observe real requests without preventing a broken implementation from
# reaching the backend; assert durable stop state in the parent afterward.
original_transport = api.session.send
submission_states = []
def observe_submission(prepared, **kwargs):
    if prepared.url.endswith('/fail'):
        state={'saved':load_binding().run_status, 'pause':load_runtime_control()['pause_requested']}
        submission_states.append(state)
    return original_transport(prepared, **kwargs)
api.session.send = observe_submission
runner.heartbeat_interval_seconds = 1
runner.start(binding)
deadline = time.monotonic()+12
while time.monotonic()<deadline:
    if bridge.calls or (request['expected'] != 'running' and binding.run_status==request['expected'] and not load_runtime_control()['inflight_flow_id'] and runner._backend_confirmed_run_status==request['expected']):
        break
    time.sleep(.1)
runner.stop_for_update(timeout_seconds=5)
print(json.dumps({'runtime':load_runtime_control(),'saved_status':load_binding().run_status,
    'memory_status':binding.run_status,'calls':bridge.calls,'http':events,
    'recovery':runner.fault_recovery_state(), 'restore_failures':restore_failures,
    'submission_states':submission_states,'injected':injected},default=str))
''', request)
    restarted['backend'] = backend_state(worker['id'])
    (tmp_path/'restart-evidence.json').write_text(json.dumps(restarted, indent=2))
    if expected == 'running':
        assert restarted['calls'] == 1, restarted
        claims=[e for e in restarted['http'] if e['url'].endswith('/claim')]
        assert len(claims)==1 and claims[0]['status']==200, restarted
        assert first['runtime']['inflight_flow_id'] not in claims[0]['url'], restarted
        assert sorted(t['status'] for t in restarted['backend']['tasks']) == ['failed','failed'], restarted
    else:
        assert restarted['calls'] == 0, restarted
        assert not any(e['url'].endswith('/claim') for e in restarted['http']), restarted
        assert sorted(t['status'] for t in restarted['backend']['tasks']) == ['failed','pending'], restarted
    assert restarted['saved_status'] == restarted['memory_status'] == restarted['backend']['status'] == expected, restarted
    if restart_injection.startswith('save_'):
        assert len(restarted['restore_failures'])==1, restarted
    if restart_injection.startswith('run-status:'):
        assert len(restarted['injected'])==1, restarted
    assert restarted['submission_states'], restarted
    if expected != 'running':
        assert all(state == {'saved':expected,'pause':True} for state in restarted['submission_states']), restarted
    assert not restarted['runtime']['inflight_flow_id']
    assert not restarted['backend']['flow'].get('flow_id')
    if boundary=='receipt_saved' and code=='WECHAT_UI_LAYOUT_UNRESOLVED' and not restart_injection:
        request['interruption']=''
        resumed=submitted.run_worker(tmp_path,r'''
import time
runner.heartbeat_interval_seconds=1
runner.start(binding)
deadline=time.monotonic()+15
while not runner.fault_recovery_state()['ready'] and time.monotonic()<deadline:
    time.sleep(.1)
assert runner.fault_recovery_state()['ready'],runner.fault_recovery_state()
assert bridge.calls==0
accepted=runner.set_run_status('running')
assert accepted
while (bridge.calls != 1 or load_runtime_control()['inflight_flow_id']) and time.monotonic()<deadline:
    time.sleep(.1)
runner.stop_for_update(timeout_seconds=5)
print(json.dumps({'calls':bridge.calls,'http':events,'accepted':accepted,
    'runtime':load_runtime_control()},default=str))
''',request)
        resumed['backend']=backend_state(worker['id'])
        (tmp_path/'explicit-resume-evidence.json').write_text(json.dumps(resumed,indent=2))
        assert resumed['calls']==1 and resumed['accepted'],resumed
        claims=[e for e in resumed['http'] if e['url'].endswith('/claim')]
        assert len(claims)==1 and claims[0]['status']==200,resumed
        assert first['runtime']['inflight_flow_id'] not in claims[0]['url'],resumed
        assert sorted(t['status'] for t in resumed['backend']['tasks'])==['failed','failed'],resumed
        assert not resumed['runtime']['inflight_flow_id'],resumed
