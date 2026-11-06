"""Update intent wins over periodic checks; synthetic data, no Windows claim."""
import json
import threading
from types import SimpleNamespace

import pytest

from chejin_worker_client import storage
from chejin_worker_client import update_coordinator as coordinator_module
from chejin_worker_client.client_update import UpdateStateStore
from chejin_worker_client.update_coordinator import UpdateCoordinator
from test_update_admission import environment
from test_update_coordinator import FakeApi, FakeRunner, _release, _wait
from chejin_worker_client.models import Binding
from test_emergency_status_settlement import stopped_runner
from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from test_hook_worker_services import runtime, until, projected_count
from test_hook_connection import callback


@pytest.mark.parametrize('status', ['running', 'paused', 'faulted'])
@pytest.mark.parametrize('boundary', ['request_before', 'request_after', 'gate_after', 'thread_start'])
def test_update_crash_boundaries_restart_stopped(stopped_runner, tmp_path, monkeypatch, status, boundary):
    from chejin_worker_client.task_runner import TaskRunner
    from chejin_worker_client import task_runner as runner_module
    from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests
    runner, _ = stopped_runner
    reset_emergency_stop_for_tests()
    runner._apply_local_run_status('running')
    runner._apply_local_run_status(status)
    store = UpdateStateStore(tmp_path/'crash-update')
    def coordinator(r):
        return UpdateCoordinator(FakeApi(_release()), r, binding_provider=lambda: r.binding,
            on_state=lambda _: None, request_normal_exit=lambda: pytest.fail('No install'),
            state_store=store, formal_package=True)
    c = coordinator(runner)
    with monkeypatch.context() as patch:
        if boundary.startswith('request_'):
            original = store.begin
            def crash_request(**kw):
                if boundary == 'request_after': original(**kw)
                raise SystemExit('Synthetic process loss')
            patch.setattr(store, 'begin', crash_request)
        elif boundary == 'gate_after':
            original = runner_module.set_update_new_work_gate
            def crash_gate(*a, **kw):
                original(*a, **kw)
                raise SystemExit('Synthetic process loss')
            patch.setattr(runner_module, 'set_update_new_work_gate', crash_gate)
        else:
            patch.setattr(threading.Thread, 'start', lambda _: (_ for _ in ()).throw(SystemExit('Synthetic process loss')))
        with pytest.raises(SystemExit): c.check_for_updates()
    restarted = TaskRunner(runner.api, runner.bridge, on_profile=lambda _: None,
        on_status=lambda _: None, on_step=lambda _: None, on_task=lambda _: None,
        on_result=lambda _: None, on_error=lambda _: None)
    restarted.binding = storage.load_binding()
    # Even before update-result reconciliation can run, the normal task gate
    # sees the persisted pause; no startup scheduling window is left open.
    assert restarted.binding.run_status == ('faulted' if status == 'faulted' else 'paused')
    assert storage.load_runtime_control()['pause_requested']
    assert not restarted._can_start_new_flow()
    coordinator(restarted).start_result_reconciliation()
    assert storage.load_binding().run_status == restarted.binding.run_status
    assert storage.load_runtime_control()['pause_requested']
    assert not restarted._can_start_new_flow()


def test_interrupted_update_cannot_clear_gate_when_stop_write_fails(stopped_runner, tmp_path, monkeypatch):
    from chejin_worker_client import task_runner as runner_module
    from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests
    runner, _ = stopped_runner
    reset_emergency_stop_for_tests()
    runner._apply_local_run_status('running')
    store = UpdateStateStore(tmp_path/'interrupted')
    state = store.begin(pre_update_run_status='running', client_instance_id='synthetic',
                        keep_stopped_after_update=True)
    storage.set_update_new_work_gate(True, update_request_id=state['update_request_id'])
    original = runner_module.save_binding
    monkeypatch.setattr(runner_module, 'save_binding', lambda _: (_ for _ in ()).throw(OSError('Synthetic disk failure')))
    c = UpdateCoordinator(FakeApi(_release()), runner, binding_provider=lambda: runner.binding,
        on_state=lambda _: None, request_normal_exit=lambda: pytest.fail('No install'),
        state_store=store, formal_package=True)
    c.start_result_reconciliation()
    assert store.load()['result_code'] == 'UPDATE_GATE_RELEASE_FAILED'
    assert storage.load_runtime_control()['update_no_new_work']
    assert not runner._can_start_new_flow()
    monkeypatch.setattr(runner_module, 'save_binding', original)
    c.start_result_reconciliation()
    assert store.load()['result_code'] == 'UPDATE_PREINSTALL_INTERRUPTED'
    assert storage.load_binding().run_status == 'paused'
    assert storage.load_runtime_control()['pause_requested']
    assert not storage.load_runtime_control()['update_no_new_work']


@pytest.mark.parametrize('status', ['running', 'paused', 'faulted'])
def test_click_during_real_cycle_stops_next_checks_and_waits_for_current(environment, monkeypatch, status):
    runner, control, _, create = environment
    runner.binding.run_status = status
    entered, finish, prepared, installed = [threading.Event() for _ in range(4)]
    calls = []
    runner.hook_services = SimpleNamespace(connection=SimpleNamespace(
        update_safety_snapshot=lambda: {'checking': False, 'process_generation': None}))
    def current_tick():
        runner.current_ui_lock = object()
        calls.append('check'); entered.set(); assert finish.wait(4)
        calls.append('saved-current-outcome')
        runner.current_ui_lock = None
    runner._tick_once = current_tick
    runner._publish_fault_recovery = lambda: None
    runner._process_fault_recovery = lambda: calls.append('recovery')
    runner._sync_pending_run_status = lambda: None
    def stopped(value):
        runner.binding.run_status = value
        runner._backend_confirmed_run_status = value
    runner.set_run_status = stopped
    c = create(FakeApi(_release()))
    monkeypatch.setattr(coordinator_module, 'prepare_release_package',
        lambda *a, **kw: (prepared.set() or {'package_manifest': {}}))
    def install(*args):
        assert finish.is_set() and runner._update_active_cycles == 0
        assert calls == ['check', 'saved-current-outcome']
        installed.set()
    c._start_install = install  # OS replacement is outside this local test.
    thread = threading.Thread(target=runner.tick_once)
    thread.start(); assert entered.wait(2)
    try:
        assert c.check_admission()['allowed']
        assert c.check_for_updates() and prepared.wait(2)
        assert control['update_no_new_work']
        assert not c.check_for_updates()
        assert runner.update_install_safety_snapshot()['waiting_reason_code'] == 'UPDATE_WAITING_BACKGROUND_DRAIN'
        for _ in range(20):
            runner.tick_once()
        assert calls == ['check'] and not installed.is_set()
    finally:
        finish.set(); thread.join(3)
    _wait(c)
    assert installed.is_set()
    assert runner.binding.run_status == ('faulted' if status == 'faulted' else 'paused')


def test_cycle_count_is_not_a_normal_fault_recovery_blocker(environment):
    runner, control, _, _ = environment
    with runner.update_background_cycle() as admitted:
        assert admitted
        assert runner.update_install_safety_snapshot()['settlement_complete']
        runner.begin_update_check(lambda: {'update_request_id': 'synthetic-drain'})
        assert not runner.update_install_safety_snapshot()['settlement_complete']
    assert runner.update_install_safety_snapshot()['background_cycles_active'] == 0


def test_cycle_exception_releases_count_without_erasing_unknown_result(environment):
    runner, control, durable, _ = environment
    control['update_no_new_work'] = False
    with pytest.raises(RuntimeError):
        with runner.update_background_cycle():
            durable['pending_sent_ack'] = 1
            runner.begin_update_check(lambda: {'update_request_id': 'synthetic-drain'})
            raise RuntimeError('Synthetic interrupted writer')
    snapshot = runner.update_install_safety_snapshot()
    assert snapshot['background_cycles_active'] == 0 and not snapshot['safe']
    assert durable['pending_sent_ack'] == 1


def test_ocr_listener_does_not_start_a_scan_after_update_intent(stopped_runner):
    runner, _ = stopped_runner
    runner.begin_update_check(lambda: {'update_request_id': 'synthetic-ocr-update'})
    runner._c2_tick_once = lambda: pytest.fail('OCR listener started work after update intent')
    class StopAfterWait:
        stopped = False
        def is_set(self): return self.stopped
        def wait(self, seconds): self.stopped = True
    runner.stop_event = StopAfterWait()
    runner._c2_loop()
    assert runner.stop_event.is_set() and runner._update_active_cycles == 0


@pytest.mark.parametrize('pending', ['running', 'paused', 'faulted', None])
def test_update_mode_only_retries_stopped_status_ack(environment, pending):
    runner, _, _, _ = environment
    runner.begin_update_check(lambda: {'update_request_id': 'synthetic-status'})
    runner._pending_run_status_sync = pending
    calls = []
    runner._sync_pending_run_status = lambda: calls.append(pending)
    runner._tick_once = lambda: pytest.fail('Background checking resumed')
    runner.tick_once()
    assert calls == ([pending] if pending in {'paused', 'faulted'} else [])


def test_unsettled_send_times_out_with_reason_and_keeps_evidence(environment, monkeypatch):
    runner, control, durable, create = environment
    durable['pending_sent_ack'] = 1
    c = create(FakeApi(_release()))
    monkeypatch.setattr(coordinator_module, 'prepare_release_package', lambda *a, **kw: {'package_manifest': {}})
    monkeypatch.setattr(coordinator_module, 'UPDATE_DRAIN_TIMEOUT_SECONDS', 0)
    c._start_install = lambda *a: pytest.fail('Unconfirmed send must not permit replacement')
    assert c.check_for_updates(); _wait(c)
    state = c.store.load()
    assert state['result_code'] == 'UPDATE_SAFE_STOP_TIMEOUT'
    assert state['prepare_diagnostic']['waiting_reason_code'] == 'UPDATE_WAITING_DURABLE_SETTLEMENT'
    assert '发送结果' in state['message']
    assert '发送结果' in c.state()['status_text']
    assert '请重试' not in c.state()['status_text']
    assert durable['pending_sent_ack'] == 1 and not control['update_no_new_work']
    assert runner.binding.run_status == 'paused'


@pytest.mark.parametrize('status', ['paused', 'faulted'])
def test_no_wechat_still_downloads_and_reaches_safe_install_with_real_pause(stopped_runner, tmp_path, monkeypatch, status):
    runner, posts = stopped_runner
    runner.binding.run_status = status
    runner.api.task_lease_fencing_tokens = {}
    runner.api.inflight_flow_id = None
    runner.bridge.sidecar_active = lambda: False
    runner._pending_flow_finish = {}
    installed = []
    c = UpdateCoordinator(FakeApi(_release()), runner, binding_provider=lambda: runner.binding,
        on_state=lambda _: None, request_normal_exit=lambda: pytest.fail('No native replacement'),
        state_store=UpdateStateStore(tmp_path/'update'), formal_package=True)
    monkeypatch.setattr(coordinator_module, 'prepare_release_package', lambda *a, **kw: {'package_manifest': {}})
    c._start_install = lambda state, release, root, boundary: installed.append(boundary)
    assert c.check_for_updates(); _wait(c)
    assert posts == [status] and installed[0]['safe']
    assert storage.load_binding().run_status == status
    assert storage.load_binding().client_instance_id == 'synthetic-client'
    assert storage.load_runtime_control()['pause_requested']


def test_hook_update_freezes_projection_but_preserves_raw_callbacks(runtime):
    runner, services, binding, *_ = runtime
    runner.binding = binding
    services.attach(binding, runner._hook_persistence_failed)
    runner.tick_once()
    runner.begin_update_check(lambda: {'update_request_id': 'synthetic-update'})
    callback(services.connection)
    with storage.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0] == 1
    entered = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(target=services.intake_loop, args=(stop, lambda _: entered.set()))
    thread.start(); assert entered.wait(2)
    try:
        # Scheduling ticks cannot invoke another WeChat check after intent.
        services.tick = lambda *_: pytest.fail('New WeChat check after update intent')
        for _ in range(10): runner.tick_once()
        assert projected_count() == 0
    finally:
        stop.set(); thread.join(3)
    assert projected_count() == 0
    # Preserved raw event can still be projected after cancellation/restart.
    runner.set_update_new_work_gate(False, update_request_id='synthetic-update')
    assert services.projection.project_next(services.connection.prepared.registration.scope) is not None
    assert projected_count() == 1


def test_new_process_checks_health_while_persisted_business_gate_stays_closed(runtime):
    runner, services, binding, *_ = runtime
    storage.set_update_new_work_gate(True, update_request_id='synthetic-old-process')
    runner.start(binding)
    until(lambda: services.connection.prepared is not None)
    until(lambda: runner.post_update_runtime_health_snapshot()['ready'])
    assert not runner.update_checks_suspended()
    assert not runner._can_start_new_flow()
    assert binding.run_status == 'paused'
    assert storage.load_runtime_control()['update_no_new_work']


@pytest.mark.parametrize('outcome', ['succeeded', 'rolled_back'])
@pytest.mark.parametrize('flag_in', ['plan', 'state'])
def test_new_update_result_cannot_resume_intake_after_restart(tmp_path, monkeypatch, outcome, flag_in):
    binding = Binding('synthetic', 'synthetic-token', 'synthetic-instance', run_status='paused')
    runner = FakeRunner(binding, [])
    store = UpdateStateStore(tmp_path/'update')
    control = tmp_path/'control'; control.mkdir()
    plan = {'update_request_id': 'request-stop', 'current_version': '0.9.59',
            'pre_update_run_status': 'running'}
    state = {'state': 'installing', 'update_request_id': 'request-stop',
             'plan_path': str(control/'update-plan.json'), 'pre_update_run_status': 'running',
             'install_started': True}
    (plan if flag_in == 'plan' else state)['keep_stopped_after_update'] = True
    (control/'update-plan.json').write_text(json.dumps(plan))
    (control/'update-result.json').write_text(json.dumps({'state': outcome,
        'result_code': 'UPDATE_SUCCEEDED' if outcome == 'succeeded' else 'UPDATE_ROLLED_BACK',
        'update_request_id': 'request-stop'}))
    store.save(state)
    monkeypatch.setattr(coordinator_module, 'set_update_new_work_gate', lambda *a, **kw: None)
    c = UpdateCoordinator(FakeApi(_release()), runner, binding_provider=lambda: binding,
        on_state=lambda _: None, request_normal_exit=lambda: None, state_store=store, formal_package=True)
    c.start_result_reconciliation(); _wait(c)
    assert store.load()['result_reconciled'] and store.load()['state'] == outcome
    assert not runner.statuses and binding.run_status == 'paused'
