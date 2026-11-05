"""Real admission/recovery and SQLite; controlled external wait boundaries."""
import threading
from types import SimpleNamespace

import pytest

from test_emergency_status_settlement import stopped_runner
from chejin_worker_client import storage
from chejin_worker_client.api import ApiError
from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests
from chejin_worker_client.hook_formal_runtime import HookFormalRuntime


@pytest.mark.parametrize('boundary,result', [('heartbeat', 'success'), ('heartbeat', 'failure'),
    ('start', 'success'), ('start', 'unknown'), ('start', 'rejected')])
def test_update_admitted_during_flow_network_and_late_response_fenced(stopped_runner, tmp_path, boundary, result):
    runner, _ = stopped_runner
    reset_emergency_stop_for_tests()
    runner._apply_local_run_status('running')
    runner.api.task_lease_fencing_tokens = {}
    runner.api.inflight_flow_id = None
    runner.bridge.sidecar_active = lambda: False
    rt = HookFormalRuntime.__new__(HookFormalRuntime)
    rt.lifecycle_runner = runner
    rt.private_root = tmp_path/'formal'
    rt.credentials = SimpleNamespace(worker_id='synthetic-worker')
    rt.active_flow_id = None
    entered, release, clicked = threading.Event(), threading.Event(), threading.Event()
    requests, errors, failures = [], [], []
    def wait_external():
        entered.set()
        assert release.wait(3)
    def heartbeat(**kwargs):
        if boundary == 'heartbeat':
            wait_external()
            if result == 'failure': raise ConnectionError('Synthetic heartbeat failure')
    def call(credentials, method, path, body):
        requests.append(path)
        if boundary == 'start': wait_external()
        if result == 'unknown': raise ConnectionError('Synthetic unknown start response')
        if result == 'rejected': raise ApiError('WORKER_NEW_FLOW_NOT_ALLOWED', 'Synthetic rejection', 409)
        return {**body, 'status': 'active'}
    rt.heartbeat = heartbeat
    rt.client = SimpleNamespace(call=call)
    def start():
        try: rt.start_flow({'id': 'original-flow'}, 'original-customer')
        except (ConnectionError, ValueError) as exc: errors.append(str(exc))
        except BaseException as exc: failures.append(exc)
    def click():
        try: runner.begin_update_check(lambda: {'update_request_id': 'concurrent-update'})
        except BaseException as exc: failures.append(exc)
        finally: clicked.set()
    flow = threading.Thread(target=start)
    update = threading.Thread(target=click)
    flow.start()
    try:
        assert entered.wait(1)
        update.start()
        assert clicked.wait(1), 'Update waited for original network request'
        assert storage.load_binding().run_status == 'paused'
        snapshot = runner.update_install_safety_snapshot()
        assert snapshot['background_cycles_active'] >= 1 and not snapshot['safe']
        assert not runner._can_start_new_flow()
    finally:
        release.set()
        flow.join(3)
        if update.ident: update.join(3)
    assert not flow.is_alive() and not update.is_alive() and not failures
    assert storage.load_binding().run_status == 'paused'
    assert storage.load_runtime_control()['update_no_new_work']
    saved = storage.load_runtime_control().get('inflight_flow_id')
    if boundary == 'heartbeat':
        assert not requests and not saved and errors
    elif result == 'rejected':
        assert not saved and errors
        assert list((rt.private_root/'flow-denials').glob('*.json'))
    else:
        assert saved == 'original-flow'
        assert not runner.update_install_safety_snapshot()['safe']
        assert list((rt.private_root/'flows').glob('*.json'))
        assert bool(errors) == (result == 'unknown')

from test_c2_identity_gate_receipts import harness
from test_task_runner import FakeApi, FakeBridge
from chejin_worker_client.models import Binding, RpaResult
import time


@pytest.mark.parametrize('overlap', [None, 'before_request', 'after_response'])
def test_real_fault_recovery_and_update_complete_without_lock_cycle(harness, monkeypatch, overlap):
    reset_emergency_stop_for_tests()
    class Api(FakeApi):
        def set_run_status(self, binding, status, **kwargs):
            return super().set_run_status(binding, status)
    runner, _ = harness.make_runner(Api(None), FakeBridge(RpaResult(ok=True, result_code='unused')))
    runner.api.task_lease_fencing_tokens = {}
    runner.bridge.sidecar_active = lambda: False
    runner.binding = Binding('worker-test', 'synthetic-token', 'instance-test', run_status='faulted')
    storage.save_binding(runner.binding)
    runner._backend_confirmed_run_status = 'faulted'
    runner._backend_fault_recovery = {'protocol_version': 1, 'ready': True}
    runner._recovery_heartbeat_at = time.monotonic()
    runner.last_rpa_component_status = 'ready'
    runner.last_wechat_status = 'logged_in'
    stop = threading.Event()
    health_threads = []
    for name, attr in [('task_runner', 'thread'), ('thread_monitor', 'thread_monitor'), ('c2_listener', 'c2_thread')]:
        ready = threading.Event()
        def live(kind=name, entered=ready):
            runner._mark_background_loop_entered(kind)
            entered.set()
            stop.wait(10)
        thread = threading.Thread(target=live)
        setattr(runner, attr, thread)
        health_threads.append(thread)
        thread.start()
        assert ready.wait(1)
    original_check = runner._check_fault_recovery
    entered, release, clicked = threading.Event(), threading.Event(), threading.Event()
    failures, observations = [], []
    count = 0
    def check():
        nonlocal count
        count += 1
        observations.append(runner._run_status_intent_lock._is_owned())
        target = 2 if overlap == 'before_request' else 3
        if overlap and count == target:
            entered.set()
            assert release.wait(3)
        return original_check()
    def recover():
        try: runner._process_fault_recovery()
        except BaseException as exc: failures.append(exc)
    def click():
        try: runner.begin_update_check(lambda: {'update_request_id': 'recovery-overlap'})
        except BaseException as exc: failures.append(exc)
        finally: clicked.set()
    recovery, update = threading.Thread(target=recover), threading.Thread(target=click)
    try:
        assert original_check()['ready']
        runner._publish_fault_recovery()
        assert runner._request_fault_recovery()
        monkeypatch.setattr(runner, '_check_fault_recovery', check)
        recovery.start()
        if overlap:
            assert entered.wait(1)
            update.start()
            assert clicked.wait(1), 'Update and recovery lock ordering deadlocked'
        else:
            recovery.join(3)
            assert not recovery.is_alive()
            assert storage.load_binding().run_status == 'running'
            update.start()
        release.set()
        recovery.join(3)
        update.join(3)
        assert not recovery.is_alive() and not update.is_alive() and not failures
        assert not any(observations), 'Full readiness check holds status intent lock'
        assert storage.load_binding().run_status == ('faulted' if overlap else 'paused')
        assert storage.load_runtime_control()['update_no_new_work']
        assert not runner._can_start_new_flow()
    finally:
        release.set()
        stop.set()
        for thread in [recovery, update, *health_threads]:
            if thread.ident: thread.join(3)


def test_update_does_not_wait_for_layout_pause_network_ack(stopped_runner):
    runner, _ = stopped_runner
    reset_emergency_stop_for_tests()
    runner._apply_local_run_status('running')
    payload = {'ok': False, 'error_code': 'WECHAT_UI_LAYOUT_STALE'}
    for _ in range(2): runner._observe_layout_result('read', payload)
    entered, release, clicked = threading.Event(), threading.Event(), threading.Event()
    failures, held = [], []
    original_status = runner.api.set_run_status
    def status(*args):
        held.append(runner._run_status_intent_lock._is_owned())
        entered.set()
        assert release.wait(3)
        return original_status(*args)
    runner.api.set_run_status = status
    def layout():
        try: runner._observe_layout_result('read', payload)
        except BaseException as exc: failures.append(exc)
    def click():
        try: runner.begin_update_check(lambda: {'update_request_id': 'layout-network'})
        except BaseException as exc: failures.append(exc)
        finally: clicked.set()
    old = threading.Thread(target=layout)
    update = threading.Thread(target=click)
    old.start()
    try:
        assert entered.wait(1)
        update.start()
        assert clicked.wait(1), 'Update blocked behind layout pause ACK'
        assert storage.load_binding().run_status == 'paused'
        assert storage.load_runtime_control()['update_no_new_work']
    finally:
        release.set()
        old.join(3)
        if update.ident: update.join(3)
    assert not old.is_alive() and not update.is_alive() and not failures
    assert held == [False]
    assert runner._pending_run_status_sync == 'paused'  # late ACK fenced
    assert storage.load_binding().run_status == 'paused'
