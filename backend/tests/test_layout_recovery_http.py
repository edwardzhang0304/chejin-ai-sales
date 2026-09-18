"""Real HTTP routes + backend/Worker SQLite; failures only at I/O boundaries."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker-client"))
sys.path.insert(0, str(ROOT / "worker-client/tests"))
from test_lead_followup_eligibility import http_api
import test_c3_api as backend
from test_task_runner import FakeBridge
from chejin_worker_client import task_runner as module, storage
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests, emergency_stop_requested
from chejin_worker_client.models import Binding, RpaResult


@pytest.fixture
def environment(http_api, monkeypatch, tmp_path):
    assert 'layout_recovery_test' in str(backend.engine.url.database)
    assert Path(storage.DB_FILE).resolve().is_relative_to(Path('/private/tmp'))
    backend.setup_function()
    reset_emergency_stop_for_tests()
    with storage.db_connection() as conn:
        for table in ('binding', 'client_settings', 'c2_runtime_state', 'c2_action_journal',
                      'c2_message_ledger', 'c2_ingest_outbox', 'reply_send_ack_outbox'):
            conn.execute(f'DELETE FROM {table}')
        conn.commit()
    worker = backend._create_worker()
    base = http_api.get('/healthz').url.removesuffix('/healthz') + '/api'
    api = WorkerApiClient(base)
    binding = Binding(worker['id'], worker['worker_token'], 'client-c3', run_status='paused')
    response = api.session.put(f"{base}/workers/{worker['id']}/vision-credential",
                              json={'vision_api_key': 'test-only-not-a-provider-secret'})
    assert response.status_code == 200, response.text
    class Desktop(FakeBridge):
        def sidecar_active(self):
            return False
        def list_sessions(self, **kwargs):
            return {'ok': False, 'error_code': 'WECHAT_UI_LAYOUT_UNRESOLVED'}
    desktop = Desktop(RpaResult(ok=True, result_code='unused'))
    errors = []
    runner = module.TaskRunner(api, desktop, on_profile=lambda _: None, on_status=lambda _: None,
        on_step=lambda _: None, on_task=lambda _: None, on_result=lambda _: None, on_error=errors.append)
    runner.binding = binding
    api.set_run_status(binding, 'paused')
    storage.save_binding(binding)
    runner._apply_local_run_status('paused')
    trace = []
    wire = api.session.send
    outage = {'active': False}
    def send(request, **kwargs):
        body = json.loads(request.body) if request.body else {}
        event = {'path': request.url.removeprefix(base), 'body_status': body.get('run_status')}
        trace.append(event)
        if outage['active'] and request.url.endswith('/run-status') and body.get('run_status') in {'paused', 'faulted'}:
            event['offline'] = True
            raise __import__('requests').ConnectionError('controlled stop-compensation outage')
        response = wire(request, **kwargs)
        event['status'] = response.status_code
        return response
    monkeypatch.setattr(api.session, 'send', send)
    try:
        yield runner, desktop, trace, outage, errors, base, tmp_path
    finally:
        reset_emergency_stop_for_tests()
        api.session.close()


def live_health_threads(runner):
    """Real liveness checked by production; no claim these are business loops."""
    stop = threading.Event()
    threads = []
    for kind, attr in [('task_runner', 'thread'), ('thread_monitor', 'thread_monitor')]:
        entered = threading.Event()
        def target(name=kind, ready=entered):
            runner._mark_background_loop_entered(name)
            ready.set()
            stop.wait(20)
        thread = threading.Thread(target=target, daemon=True)
        setattr(runner, attr, thread)
        threads.append(thread)
        thread.start()
        assert entered.wait(1)
    return stop, threads


def assert_no_new_work(runner, desktop, trace):
    assert not runner._can_start_new_flow()
    assert not any('/tasks/pull' in e['path'] or '/claim' in e['path'] or '/inflight-flow/start' in e['path'] for e in trace), trace
    assert not desktop.sent_replies and not desktop.message_reads
    assert not storage.load_runtime_control()['inflight_flow_id']


def restart_same_database(base, tmp_path, expected, offline=False):
    # A new interpreter reads the same DB; no shared emergency Event or Runner.
    script = tmp_path / 'restart.py'
    script.write_text('''
import json,sys,time
from chejin_worker_client import storage
from chejin_worker_client.api import WorkerApiClient
from chejin_worker_client.rpa_bridge import RpaBridge
from chejin_worker_client.task_runner import TaskRunner
class Desktop(RpaBridge):
    def probe(self):return 'ready','logged_in'
    def _call_omniauto(self,args,**kwargs):raise AssertionError('No physical work before explicit resume')
api=WorkerApiClient(sys.argv[1]); trace=[]; original=api.session.send
def wire(request,**kwargs):
    trace.append(request.url)
    if sys.argv[3]=='1' and request.url.endswith('/run-status'):
        raise __import__('requests').ConnectionError('controlled compensation outage after restart')
    return original(request,**kwargs)
api.session.send=wire
runner=TaskRunner(api,Desktop(),on_profile=lambda _:None,on_status=lambda _:None,on_step=lambda _:None,
                  on_task=lambda _:None,on_result=lambda _:None,on_error=lambda _:None)
runner.start(storage.load_binding())
deadline=time.monotonic()+5
while time.monotonic()<deadline:
    if any(u.endswith('/heartbeat') for u in trace):break
    time.sleep(.02)
runner.stop_for_update(timeout_seconds=5)
assert runner.binding.run_status==sys.argv[2], (runner.binding.run_status,trace)
assert any(u.endswith('/heartbeat') for u in trace),trace
assert not any('/tasks/pull' in u or '/claim' in u or '/inflight-flow/start' in u for u in trace),trace
assert not storage.load_runtime_control()['inflight_flow_id']
print(json.dumps({'local':runner.binding.run_status,'pause':storage.load_runtime_control()['pause_requested'],
                  'trace':trace,'blocked':runner.layout_recovery_state()['blocked']}))
''', encoding='utf-8')
    result = subprocess.run([sys.executable, str(script), base, expected, '1' if offline else '0'],
        env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(ROOT/'worker-client'), str(ROOT/'backend')])},
        capture_output=True, text=True, timeout=20)
    (tmp_path/'restart.stdout').write_text(result.stdout)
    (tmp_path/'restart.stderr').write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize('stored', ['current_blocked', 'other_worker', 'other_instance', 'other_binding', 'unowned', 'not_blocked', 'cleared'])
def test_restart_only_consumes_current_bound_blocked_budget(environment, stored):
    runner, desktop, trace, outage, errors, base, tmp_path = environment
    assert runner.set_run_status('running')
    identity = runner._layout_binding_identity()
    state = {'counts': {'scan': 3}, 'binding_identity': dict(identity)}
    if stored == 'other_worker':
        state['binding_identity']['worker_id'] = 'different-worker'
    elif stored == 'other_instance':
        state['binding_identity']['client_instance_id'] = 'different-instance'
    elif stored == 'other_binding':
        state['binding_identity']['bound_at'] = 'different-binding-time'
    elif stored == 'unowned':
        state.pop('binding_identity')
    elif stored == 'not_blocked':
        state['counts']['scan'] = 2
    elif stored == 'cleared':
        state = {}
    storage.save_c2_state('layout_recovery', state)
    if stored == 'current_blocked':
        restarted = restart_same_database(base, tmp_path, 'paused', offline=True)
        assert restarted['blocked']
    else:
        # Actual start on a new Runner with controlled thread scheduling only;
        # the startup code must not invent a stop for an unrelated budget.
        from unittest.mock import patch
        fresh = module.TaskRunner(runner.api, desktop, on_profile=lambda _: None, on_status=lambda _: None,
            on_step=lambda _: None, on_task=lambda _: None, on_result=lambda _: None, on_error=errors.append)
        with patch.object(module.threading, 'Thread') as thread_factory:
            thread_factory.return_value.is_alive.return_value = False
            fresh.start(storage.load_binding())
        assert fresh.binding.run_status == 'running'
        assert not storage.load_runtime_control()['pause_requested']
        assert not fresh.layout_recovery_state()['blocked']
        fresh.tick_once()
        assert any('/tasks/pull' in e['path'] for e in trace), trace


@pytest.mark.parametrize('original', ['paused', 'faulted'])
@pytest.mark.parametrize('failure,offline', [('none', False), ('reset', False), ('reset', True),
                                          ('binding', True), ('clear_pause', True)])
def test_resume_persistence_failure_http_and_restart(environment, monkeypatch, original, failure, offline):
    runner, desktop, trace, outage, errors, base, tmp_path = environment
    runner.api.set_run_status(runner.binding, original)
    runner._apply_local_run_status(original)
    old_budget = {'counts': {'scan': 3}, 'error_code': 'WECHAT_UI_LAYOUT_UNRESOLVED',
                  'binding_identity': runner._layout_binding_identity()}
    storage.save_c2_state('layout_recovery', old_budget)
    stop, threads = live_health_threads(runner)
    try:
        runner.tick_once()  # Actual heartbeat supplies the fault-recovery proof.
        assert runner.binding.run_status == original
        trace.clear()
        outage['active'] = offline
        failed = []
        method = {'reset': 'save_c2_state', 'binding': 'save_binding', 'clear_pause': 'clear_runtime_pause'}.get(failure)
        if method:
            real = getattr(module, method)
            def save(*args, **kwargs):
                should_fail = not failed and (failure != 'reset' or args == ('layout_recovery', {}))
                if failure == 'binding':
                    should_fail = should_fail and args[0].run_status == 'running'
                if should_fail:
                    failed.append(failure)
                    raise sqlite3.OperationalError('controlled one-write failure')
                return real(*args, **kwargs)
            monkeypatch.setattr(module, method, save)
        if original == 'faulted':
            assert runner.fault_recovery_state()['ready'], runner.fault_recovery_state()
            assert runner.set_run_status('running')
            runner._process_fault_recovery()
        else:
            assert runner.set_run_status('running') is (failure == 'none')
        if failure == 'none':
            assert runner.binding.run_status == 'running'
            assert not runner.layout_recovery_state()['blocked']
            assert not storage.load_runtime_control()['pause_requested']
            runner.tick_once()
            assert any('/tasks/pull' in e['path'] for e in trace), trace
        else:
            assert failed == [failure]
            assert runner.binding.run_status == original
            assert storage.load_binding().run_status == original
            assert storage.load_c2_state('layout_recovery') == old_budget
            assert storage.load_runtime_control()['pause_requested']
            assert any(e['body_status'] == 'running' and e.get('status') == 200 for e in trace), trace
            assert any(e['body_status'] == original for e in trace), trace
            runner.tick_once()
            assert runner.binding.run_status == original
            assert_no_new_work(runner, desktop, trace)
            restarted = restart_same_database(base, tmp_path, original, offline)
            assert restarted['blocked']
            # Only another explicit successful Start clears the retained budget.
            outage['active'] = False
            runner._sync_pending_run_status(force=True)
            runner.tick_once()
            if original == 'faulted':
                runner._publish_fault_recovery()
                assert runner.set_run_status('running')
                runner._process_fault_recovery()
            else:
                assert runner.set_run_status('running')
            assert runner.binding.run_status == 'running'
            assert not runner.layout_recovery_state()['blocked']
        (tmp_path/'evidence.json').write_text(json.dumps({'original': original, 'failure': failure,
            'offline': offline, 'http': trace, 'errors': errors}, ensure_ascii=False, indent=2))
    finally:
        stop.set()
        for thread in threads:
            thread.join(1)


@pytest.mark.parametrize('failed_write', ['counter', 'pause', 'binding', 'both_stop_records', 'all_records'])
def test_third_failure_stops_despite_sqlite_error(environment, monkeypatch, failed_write):
    runner, desktop, trace, outage, errors, base, tmp_path = environment
    assert runner.set_run_status('running')
    storage.save_c2_state('layout_recovery', {'counts': {'scan': 2}})
    trace.clear()
    outage['active'] = True
    originals = {name: getattr(module, name) for name in ('save_c2_state', 'request_runtime_pause', 'save_binding')}
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('controlled persistent stop-write failure')
    if failed_write in {'counter', 'all_records'}:
        def save(key, value):
            if key == 'layout_recovery':
                fail()
            return originals['save_c2_state'](key, value)
        monkeypatch.setattr(module, 'save_c2_state', save)
    if failed_write in {'pause', 'both_stop_records', 'all_records'}:
        monkeypatch.setattr(module, 'request_runtime_pause', fail)
    if failed_write in {'binding', 'both_stop_records', 'all_records'}:
        monkeypatch.setattr(module, 'save_binding', fail)
    runner._scan_wechat_sessions(runner.binding)
    assert runner.binding.run_status == 'paused'
    assert_no_new_work(runner, desktop, trace)
    runner.tick_once()
    assert_no_new_work(runner, desktop, trace)
    if failed_write != 'counter':
        assert emergency_stop_requested()
    if failed_write != 'all_records':
        # At least one durable stop record survives; next process must honor it.
        restart_same_database(base, tmp_path, 'paused', offline=True)
    else:
        # Both writes + network fail: only the current-process guarantee applies.
        # Durable abnormal-exit behavior requires separate product approval.
        assert storage.load_binding().run_status == 'running'
        assert not storage.load_runtime_control()['pause_requested']
    (tmp_path/'evidence.json').write_text(json.dumps({'failure': failed_write, 'http': trace,
        'errors': errors, 'emergency': emergency_stop_requested()}, ensure_ascii=False, indent=2))
