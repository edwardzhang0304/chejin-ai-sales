"""Update discovery admission: real runner lock and coordinator, no WeChat/network."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from chejin_worker_client import storage
from chejin_worker_client.client_update import ClientUpdateError, UpdateStateStore
from chejin_worker_client.update_coordinator import UpdateCoordinator
import chejin_worker_client.update_coordinator as coordinator_module
import chejin_worker_client.task_runner as runner_module
from test_update_safety_boundary import _runner, ZERO_DURABLE
from test_update_coordinator import FakeApi, _release, _wait


@pytest.fixture
def environment(tmp_path, monkeypatch):
    runner = _runner()
    control = {'update_no_new_work': False, 'inflight_flow_id': None}
    durable = dict(ZERO_DURABLE)
    monkeypatch.setattr(runner_module, 'load_runtime_control', lambda: dict(control))
    monkeypatch.setattr(runner_module, 'update_install_business_blockers', lambda: dict(durable))
    monkeypatch.setattr(runner_module, 'lock_summary', lambda: {'locked': False})
    monkeypatch.setattr(runner_module, 'emergency_stop_requested', lambda: False)
    def gate(blocked, *, update_request_id=None):
        if blocked or control.get('update_request_id') in (None, update_request_id):
            control.update(update_no_new_work=blocked, update_request_id=update_request_id if blocked else None)
        return dict(control)
    monkeypatch.setattr(runner_module, 'set_update_new_work_gate', gate)
    monkeypatch.setattr(coordinator_module, '_safe_update_log', lambda *a, **kw: None)
    def coordinator(api=None):
        return UpdateCoordinator(api or FakeApi(_release(available=False)), runner,
            binding_provider=lambda: runner.binding, on_state=lambda state: None,
            request_normal_exit=lambda: pytest.fail('must not install'),
            state_store=UpdateStateStore(tmp_path/'update'), formal_package=True)
    return runner, control, durable, coordinator


@pytest.mark.parametrize('status', ['paused', 'faulted', 'running'])
def test_idle_paused_faulted_and_offline_can_check_without_backend_confirmation(environment, status):
    runner, control, _, create = environment
    runner.binding.run_status = status
    runner._backend_confirmed_run_status = ''
    runner.connection_status = 'offline'
    c = create()
    assert c.check_admission()['allowed'] is True
    assert c.check_for_updates() is True
    _wait(c)
    assert c.api.calls
    assert runner.binding.run_status == status
    assert control['update_no_new_work'] is False


@pytest.mark.parametrize('field,reason', [
    ('pending_sqlite_action_journal', '结果尚未确认'),
    ('pending_file_action_journal', '结果尚未确认'),
    ('pending_sent_ack', '发送结果'),
    ('pending_c2_outbox', '尚未回传'),
    ('waiting_ledger', '尚未回传'),
    ('action_journal_state_unavailable', '本地操作记录'),
])
def test_fault_does_not_hide_durable_blocker_or_allow_direct_call(environment, field, reason):
    runner, control, durable, create = environment
    runner.binding.run_status = 'faulted'
    durable[field] = 1
    c = create()
    assert reason in c.check_admission()['reason_text']
    assert c.check_for_updates() is False
    assert not c.api.calls and not control['update_no_new_work']
    assert durable[field] == 1


def test_ui_snapshot_can_be_stale_but_atomic_entry_rechecks(environment):
    runner, control, _, create = environment
    c = create()
    assert c.check_admission()['allowed']
    original = runner.begin_update_check
    def race(begin):
        runner.current_task = SimpleNamespace(id='synthetic-inflight')
        return original(begin)
    runner.begin_update_check = race
    assert c.check_for_updates() is False
    assert c.api.calls == [] and not control['update_no_new_work']
    assert c.store.load()['state'] == 'idle'


def test_accepted_check_closes_admission_before_release_request_and_duplicate(environment):
    runner, control, _, create = environment
    runner.binding.run_status = 'running'
    entered, finish = threading.Event(), threading.Event()
    class Api(FakeApi):
        def latest_client_release(self, **kw):
            entered.set()
            assert control['update_no_new_work'] and not runner._can_start_new_flow()
            assert finish.wait(3)
            return super().latest_client_release(**kw)
    c = create(Api(_release(available=False)))
    assert c.check_for_updates()
    assert entered.wait(2)
    try:
        request = c.store.load()['update_request_id']
        assert not c.check_for_updates()
        assert c.store.load()['update_request_id'] == request
    finally:
        finish.set()
    _wait(c)
    assert not control['update_no_new_work'] and runner.binding.run_status == 'running'
    assert len(c.api.calls) == 1


def test_task_admission_owner_blocks_check_without_waiting_for_http(environment):
    runner, _, _, create = environment
    entered, finish = threading.Event(), threading.Event()
    def owner():
        with runner._new_work_admission_lock:
            entered.set(); assert finish.wait(3)
    t = threading.Thread(target=owner);t.start();assert entered.wait(2)
    try:
        c = create()
        assert c.check_admission()['reason_code'] == 'UPDATE_ADMISSION_BUSY'
        assert not c.check_for_updates() and not c.api.calls
    finally:
        finish.set();t.join()


@pytest.mark.parametrize('status', ['paused', 'faulted', 'running'])
@pytest.mark.parametrize('outcome', ['download_failure', 'cancel', 'network_failure'])
def test_failure_and_cancel_release_only_own_gate_preserving_status(environment, monkeypatch, status, outcome):
    runner, control, _, create = environment
    runner.binding.run_status = status
    runner.set_run_status = lambda value: setattr(runner.binding, 'run_status', value)
    c = create(FakeApi(_release()))
    def prepare(*a, **kw):
        if outcome == 'cancel':
            c.stop()
            return {'package_manifest': {}}
        raise ClientUpdateError('UPDATE_DOWNLOAD_FAILED', 'synthetic download error')
    monkeypatch.setattr(coordinator_module, 'prepare_release_package', prepare)
    if outcome == 'network_failure':
        def failed(**kw):
            from chejin_worker_client.api import ApiError
            raise ApiError('NETWORK_ERROR', 'synthetic unavailable', 0)
        c.api.latest_client_release = failed
    assert c.check_for_updates(); _wait(c)
    assert runner.binding.run_status == status
    assert not control['update_no_new_work']
    expected = {'download_failure':'UPDATE_DOWNLOAD_FAILED', 'cancel':'UPDATE_CANCELLED', 'network_failure':'NETWORK_ERROR'}[outcome]
    assert c.store.load()['result_code'] == expected


@pytest.mark.parametrize('operator_action', ['paused', 'faulted'])
def test_fault_or_manual_pause_during_download_is_never_resumed(environment, monkeypatch, operator_action):
    runner, control, _, create = environment
    runner.binding.run_status = 'running'
    runner.set_run_status = lambda value: setattr(runner.binding, 'run_status', value)
    c = create(FakeApi(_release()))
    def prepare(*a, **kw):
        runner.binding.run_status = operator_action
        if operator_action == 'paused':c.note_operator_pause()
        raise ClientUpdateError('UPDATE_DOWNLOAD_FAILED', 'synthetic error')
    monkeypatch.setattr(coordinator_module, 'prepare_release_package', prepare)
    assert c.check_for_updates();_wait(c)
    assert runner.binding.run_status == operator_action and not control['update_no_new_work']


def test_storage_check_failure_is_visible_and_cannot_start(environment, monkeypatch):
    _, _, _, create = environment
    monkeypatch.setattr(runner_module, 'update_install_business_blockers', lambda: (_ for _ in ()).throw(OSError()))
    c = create()
    assert c.check_admission()['reason_code'] == 'UPDATE_SAFETY_STATE_UNAVAILABLE'
    assert not c.check_for_updates() and not c.api.calls


def test_widget_uses_coordinator_decision_and_visible_reason():
    source = Path(coordinator_module.__file__).with_name('ui.py').read_text()
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'WorkerWindow')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'refresh_update_controls')
    ns = {};exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '<ui>', 'exec'), ns)
    values = {}
    widget = SimpleNamespace(isVisible=lambda:True, setEnabled=lambda v:values.update(enabled=v),
        setToolTip=lambda v:values.update(tooltip=v),setText=lambda v:values.update(button=v))
    ui = SimpleNamespace(update_button=widget, update_state={'status_text':'ready'},
        update_status_label=SimpleNamespace(setText=lambda v:values.update(label=v)))
    for allowed in (False, True):
        ui.update_coordinator = SimpleNamespace(check_admission=lambda state: {'allowed':allowed,'reason_text':'' if allowed else '发送结果未确认'})
        ns['refresh_update_controls'](ui)
        assert values['enabled'] is allowed
        assert values['label'] == ('ready' if allowed else '发送结果未确认')


def test_hook_unknown_current_process_and_pending_delivery_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'APP_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DB_FILE', tmp_path/'worker_client.sqlite3')
    from chejin_worker_client.hook_active_calls import reserve, finish
    reserve('old-process', 'action-a', 'Voice2Text');finish('old-process', 'action-a', False)
    assert storage.hook_update_business_blockers(None)['hook_unresolved_calls'] == 1
    assert storage.hook_update_business_blockers('old-process')['hook_unresolved_calls'] == 1
    assert storage.hook_update_business_blockers('verified-new-process')['hook_unresolved_calls'] == 0
    with storage.db_connection() as db:
        assert db.execute('SELECT state FROM hook_active_calls').fetchone()[0] == 'uncertain'
        db.execute('CREATE TABLE hook_delivery_outbox(status TEXT)')
        db.execute("INSERT INTO hook_delivery_outbox VALUES ('retry_wait')");db.commit()
    assert storage.hook_update_business_blockers('verified-new-process')['hook_pending_delivery'] == 1


def test_stopped_read_can_discover_but_target_must_still_prove_handoff(environment, tmp_path, monkeypatch):
    from test_pending_read_handoff import pending
    from chejin_worker_client.pending_read_recovery import package_recovery_capability
    runner, control, durable, create = environment
    data = tmp_path/'pending-data'
    handoff = pending(data, monkeypatch)
    runner.binding = storage.load_binding()
    runner._restart_recovery_lock = threading.RLock()
    control['inflight_flow_id'] = handoff['flow_id']
    durable.update(waiting_ledger=1, pending_c2_outbox=1)
    monkeypatch.setattr(runner_module, 'CONFIG', SimpleNamespace(app_dir=data))
    assert create().check_admission()['allowed']
    control['update_no_new_work'] = True
    calls = []
    runner.api.set_run_status = lambda *a: calls.append(a) or SimpleNamespace(
        run_status='faulted', pending_read_recovery={**handoff, 'ready':True})
    assert not runner.update_pending_read_handoff_snapshot({})['safe']
    assert not calls
    assert runner.update_pending_read_handoff_snapshot({'pending_read_recovery':package_recovery_capability()})['safe']
    assert storage.load_runtime_control()['inflight_flow_id'] == handoff['flow_id']
    durable['pending_sent_ack'] = 1
    control['update_no_new_work'] = False
    assert not create().check_admission()['allowed']


def test_model_query_after_update_reservation_cannot_start_generation(environment, monkeypatch):
    from chejin_worker_client.hook_formal_runtime import read_batch_view
    runner, control, _, create = environment
    runner.binding.run_status = 'running'
    monkeypatch.setattr(storage, 'load_runtime_control', lambda:dict(control))
    entered, finish = threading.Event(), threading.Event()
    class Api(FakeApi):
        def latest_client_release(self, **kw):
            entered.set();assert finish.wait(3)
            return self.release
    c = create(Api(_release(available=False)))
    paths=[]
    client=SimpleNamespace(call=lambda _,method,path: paths.append(path) or {'observation_only':True})
    assert c.check_for_updates();assert entered.wait(2)
    try:
        read_batch_view(client, SimpleNamespace(worker_id='synthetic-worker'), runner,
                        'synthetic-batch', observation_query_version=1)
        assert paths == ['/workers/synthetic-worker/wechat/message-batches/synthetic-batch?observe_only=true']
    finally:finish.set()
    _wait(c)


def test_failed_gate_write_does_not_crash_ui_or_start_http(environment, monkeypatch):
    _, _, _, create = environment
    monkeypatch.setattr(runner_module, 'set_update_new_work_gate', lambda *a, **kw: (_ for _ in ()).throw(OSError('synthetic storage unavailable')))
    c = create()
    assert not c.check_for_updates()
    assert not c.api.calls
    assert c.store.load()['result_code'] == 'UPDATE_GATE_RELEASE_FAILED'


def test_thread_start_failure_clears_own_reservation(environment, monkeypatch):
    _, control, _, create = environment
    monkeypatch.setattr(threading.Thread, 'start', lambda _: (_ for _ in ()).throw(RuntimeError('synthetic start error')))
    c=create()
    assert not c.check_for_updates() and not c.api.calls
    assert not control['update_no_new_work']
    assert c.store.load()['state']=='failed'
