"""Original TaskRunner threads, real HTTP/SQLite; Windows/DLL facts are fixtures."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import threading
import time
from types import SimpleNamespace

import pytest

from test_hook_gateway import lab
from test_hook_backend import peer as backend_peer
from test_hook_connection import setup, callback
from chejin_worker_client import storage
from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests, emergency_stop_requested
from chejin_worker_client.hook_projection import NativeProjectionStore
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.models import Binding, WorkerProfile
from chejin_worker_client.task_runner import TaskRunner


def until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def runtime(lab, backend_peer):
    reset_emergency_stop_for_tests()
    manager, credentials, state, response = setup(lab, backend_peer)
    # Business-enabled startup now also validates the authenticated UTC field.
    # Supply the real heartbeat shape, rather than omitting its clock metadata.
    heartbeat = json.loads(backend_peer.body)
    heartbeat['data']['last_heartbeat_at'] = datetime.now(timezone.utc).isoformat()
    backend_peer.body = json.dumps(heartbeat).encode()
    backend_peer.live_heartbeat_time = True
    services = HookWorkerServices(manager, NativeProjectionStore(max_projection_bytes=65536, max_entries=20),
        intake_interval_seconds=.02, projection_batch_size=3)
    profiles, errors, statuses, posted = [], [], [], []
    def set_status(binding, status, **kwargs):
        posted.append(status)
        return WorkerProfile(binding.worker_id, "Synthetic", status)
    def prohibited(*args, **kwargs):
        pytest.fail("Hook lifecycle entered an OCR/vision/physical business path")
    runner = TaskRunner(SimpleNamespace(set_run_status=set_status, get_vision_credential=prohibited),
        SimpleNamespace(probe=prohibited), on_profile=profiles.append, on_status=statuses.append,
        on_step=prohibited, on_task=prohibited, on_result=prohibited, on_error=errors.append,
        hook_services=services)
    runner.poll_interval_seconds=.04
    runner._maybe_cleanup_artifacts=lambda **kwargs:None
    binding=Binding(credentials.worker_id, credentials.worker_token, credentials.client_instance_id, 'paused')
    storage.save_binding(binding)
    try:
        yield runner, services, binding, profiles, errors, statuses, posted, response
    finally:
        runner.stop_for_update(5)
        reset_emergency_stop_for_tests()


def projected_count():
    with storage.db_connection() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hook_observation_projection'").fetchone() is None:
            return 0
        return conn.execute('SELECT COUNT(*) FROM hook_observation_projection').fetchone()[0]


def test_original_supervisor_receives_and_projects_while_paused_without_ocr_or_business(runtime, lab):
    runner, services, binding, profiles, errors, _, posted, _ = runtime
    runner.start(binding)
    until(lambda: services.connection.prepared is not None)
    callback(services.connection)
    until(lambda: projected_count()==1)
    until(lambda: runner.post_update_runtime_health_snapshot()['ready'])
    health=runner.post_update_runtime_health_snapshot()
    assert health['required_threads']==['task_runner','thread_monitor','hook_intake']
    assert runner.c2_thread is None and binding.run_status=='paused'
    assert profiles and not errors and set(posted)<={'paused'}
    assert all(path in {'/IsLoginStatus','/ConfigureMsgReciveFullURL'} for path, *_ in lab[0].requests)
    runner.stop_for_update(5)
    assert services.connection.prepared is None
    assert not runner.thread.is_alive() and not runner.hook_intake_thread.is_alive()
    assert projected_count()==1


def test_manual_login_wait_is_installation_healthy_and_requires_no_c2(runtime, lab):
    runner, services, binding, _, errors, statuses, *_ = runtime
    lab[0].login_status='0'
    runner.start(binding)
    until(lambda:'waiting_wechat' in statuses)
    until(lambda:runner.post_update_runtime_health_snapshot()['ready'])
    assert not errors and services.connection.prepared is None
    lab[0].login_status='3'
    until(lambda:services.connection.prepared is not None)
    callback(services.connection)
    until(lambda:projected_count()==1)
    assert not errors


def test_backend_outage_keeps_passive_approved_receiver_and_paused_status(runtime, backend_peer):
    runner, services, binding, _, errors, statuses, *_=runtime
    runner.start(binding)
    until(lambda:services.connection.prepared is not None)
    backend_peer.server_close()
    until(lambda:'connecting' in statuses)
    callback(services.connection)
    until(lambda:projected_count()==1)
    assert binding.run_status=='paused' and not errors
    assert runner.post_update_runtime_health_snapshot()['ready']


def test_normal_stop_drains_a_callback_commit_without_deleting_raw_evidence(runtime, monkeypatch):
    runner, services, binding, *_=runtime
    runner.start(binding)
    until(lambda:services.connection.prepared is not None)
    entered, allow_commit = threading.Event(), threading.Event()
    persist=services.connection.inbox.persist
    def delayed(raw, scope):
        entered.set()
        assert allow_commit.wait(3)
        return persist(raw, scope)
    monkeypatch.setattr(services.connection.inbox,'persist',delayed)
    failures=[]
    def call():
        try:callback(services.connection)
        except BaseException as exc:failures.append(exc)
    sending=threading.Thread(target=call);sending.start()
    assert entered.wait(2)
    runner.stop()
    assert runner.thread.is_alive()  # Still draining the actual server writer.
    allow_commit.set();sending.join(3)
    until(lambda:not runner.thread.is_alive())
    assert not failures and services.connection.prepared is None
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1


def test_slow_heartbeat_never_undoes_a_newer_manual_pause(runtime, monkeypatch, backend_peer):
    runner, services, binding, profiles, errors, _, posted, _=runtime
    runner.binding=binding
    services.attach(binding, runner._hook_persistence_failed)
    payload=json.loads(backend_peer.body);payload['data']['run_status']='running'
    backend_peer.body=json.dumps(payload).encode()
    binding.run_status='running';storage.save_binding(binding)
    original=services.connection.prepare
    def pause_during_response(*args):
        prepared=original(*args)
        assert runner.set_run_status('paused')
        return prepared
    monkeypatch.setattr(services.connection,'prepare',pause_during_response)
    runner.tick_once()
    assert binding.run_status=='paused' and storage.load_binding().run_status=='paused'
    assert profiles[-1].run_status=='paused' and posted==['paused'] and not errors


def test_dead_registered_receiver_fails_original_health_and_closes_new_work(runtime):
    runner, services, binding, *_=runtime
    runner.binding=binding
    services.attach(binding, runner._hook_persistence_failed)
    runner.tick_once()
    manager=services.connection
    manager._receiver.shutdown();manager._receiver_thread.join(2)
    assert not services.health_snapshot()['ready']
    runner.tick_once()
    assert emergency_stop_requested() and binding.run_status=='paused'
    assert not runner._can_start_new_flow()
    assert runner.post_update_runtime_health_snapshot()['startup_failures']==['hook_intake']


def test_changed_wechat_faults_original_worker_and_keeps_old_raw_account(runtime, lab):
    runner, services, binding, _, errors, _, _, response=runtime
    runner.binding=binding
    services.attach(binding,runner._hook_persistence_failed)
    runner.tick_once()
    original=services.connection.prepared
    callback(services.connection)
    lab[0].wxid='wxid_sales_B';response(blocked=True,rebind=True)
    runner.tick_once()
    assert binding.run_status=='faulted' and services.connection.prepared is None
    assert not runner._can_start_new_flow() and len(errors)==1
    with storage.db_connection() as conn:
        assert conn.execute('SELECT self_wxid FROM hook_raw_inbox').fetchone()[0]==original.account.self_wxid


def test_local_health_read_does_not_wait_for_a_backend_operation_lock(runtime):
    _, services, *_=runtime
    entered, release=threading.Event(),threading.Event()
    def hold():
        with services.connection._operation_lock:
            entered.set();release.wait(2)
    thread=threading.Thread(target=hold);thread.start();assert entered.wait(1)
    started=time.monotonic()
    try:
        assert services.health_snapshot()['ready']
        assert time.monotonic()-started < .2
    finally:release.set();thread.join(2)


def test_projection_writer_failure_closes_original_new_work_gate_but_raw_receiver_survives(runtime,monkeypatch):
    runner,services,binding,*_=runtime
    def failed_projection(*args):raise OSError('synthetic projection storage failure')
    monkeypatch.setattr(services.projection,'project_next',failed_projection)
    runner.start(binding)
    until(lambda:'hook_intake' in runner.post_update_runtime_health_snapshot()['startup_failures'])
    assert emergency_stop_requested() and not runner._can_start_new_flow()
    assert not runner.post_update_runtime_health_snapshot()['ready']
    callback(services.connection)
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1


def test_update_timeout_cannot_take_ownership_before_callback_commit(runtime,monkeypatch):
    runner,services,binding,*_=runtime
    runner.start(binding)
    until(lambda:services.connection.prepared is not None)
    entered,release=threading.Event(),threading.Event()
    original=services.connection.inbox.persist
    def delayed(raw,scope):
        entered.set();assert release.wait(3)
        return original(raw,scope)
    monkeypatch.setattr(services.connection.inbox,'persist',delayed)
    sender=threading.Thread(target=lambda:callback(services.connection));sender.start()
    assert entered.wait(2)
    try:
        with pytest.raises(RuntimeError,match='UPDATE_WRITERS_NOT_STOPPED'):runner.stop_for_update(.01)
        assert runner.thread.is_alive()
    finally:release.set();sender.join(3)
    runner.stop_for_update(5)
    with storage.db_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM hook_raw_inbox').fetchone()[0]==1


def test_credential_change_requires_full_stop_then_restart_reauthorizes(runtime,backend_peer):
    runner,services,binding,*_=runtime
    runner.start(binding)
    until(lambda:services.connection.prepared is not None)
    candidate=Binding(binding.worker_id,'synthetic-rotated-token',binding.client_instance_id,'paused')
    with pytest.raises(ValueError,match='REBIND_REQUIRES_STOP'):
        services.attach(candidate,runner._hook_persistence_failed)
    runner.stop_for_update(5)
    runner.start(candidate)
    until(lambda:services.connection.prepared is not None)
    until(lambda:runner.post_update_runtime_health_snapshot()['ready'])
    assert backend_peer.requests[-1][1]['X-Worker-Token']=='synthetic-rotated-token'
    assert candidate.run_status=='paused'
    callback(services.connection)
    until(lambda:projected_count()==1)


def test_business_writer_does_not_block_heartbeat_and_update_cannot_take_live_storage(runtime):
    runner,services,binding,*_=runtime
    entered=threading.Event();release=threading.Event()
    class Business:
        interval_seconds=.02
        def tick(self,runner,services):entered.set();release.wait(5)
    services.dispatcher=Business()
    runner.start(binding)
    until(entered.is_set)
    try:
        until(lambda:services.connection.prepared is not None)
        t=runner._recovery_heartbeat_at
        until(lambda:runner._recovery_heartbeat_at>t)
        assert runner.post_update_runtime_health_snapshot()['threads']['hook_business']['alive']
        with pytest.raises(RuntimeError,match='UPDATE_WRITERS_NOT_STOPPED'):runner.stop_for_update(.1)
        assert runner.hook_business_thread.is_alive()
    finally:release.set();runner.stop_for_update(5)
    assert not runner.hook_business_thread.is_alive()


def test_quarantine_without_call_identity_shows_generic_cause_and_keeps_both_audit_codes(runtime):
    from chejin_worker_client.hook_gateway import HookLocalError
    from chejin_worker_client.hook_progress import HookProgressTimeline
    runner,services,binding,*_=runtime
    runner.binding=binding;timeline=HookProgressTimeline();events=[]
    runner._emit_runtime_process=lambda event:(events.append(event),timeline.apply(event))
    error=HookLocalError('HOOK_LOCAL_TRANSPORT_FAILED',outcome_unknown=True)
    error.operation='Voice2Text';error.transport_kind='timeout'
    services._fault(runner,error.code,local_error=error)
    services._fault(runner,'HOOK_PROCESS_ACTIVE_CALL_QUARANTINED')
    rows=[item for item in timeline.model() if item['title']=='已停止接单']
    assert any('语音转写等待超时' in row['description'] for row in rows)
    from chejin_worker_client import startup_check_state
    assert services._display_fault==startup_check_state.failed('HOOK_PROCESS_ACTIVE_CALL_QUARANTINED')
    assert '语音转写等待超时' not in services._display_fault['message']
    assert binding.run_status=='faulted' and not services.startup_check_state()['canStart']
    with storage.db_connection() as db:
        faults=db.execute("SELECT error_code,metadata FROM local_logs WHERE event='hook_connection_fault' ORDER BY created_at").fetchall()
    assert [row['error_code'] for row in faults]==['HOOK_LOCAL_TRANSPORT_FAILED','HOOK_PROCESS_ACTIVE_CALL_QUARANTINED']
    assert json.loads(faults[0]['metadata'])['transport_kind']=='timeout'


def test_fault_diagnostics_never_echo_arbitrary_exception_attributes(runtime):
    runner,services,binding,*_=runtime;runner.binding=binding
    error=SimpleNamespace(operation='private phone and url',transport_kind='private text')
    services._fault(runner,'HOOK_LOCAL_TRANSPORT_FAILED',local_error=error)
    with storage.db_connection() as db:
        meta=json.loads(db.execute("SELECT metadata FROM local_logs WHERE event='hook_connection_fault' ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    assert meta['local_operation'] is None and meta['transport_kind'] is None
    assert 'private phone and url' not in json.dumps(meta)
    assert 'private text' not in json.dumps(meta)


def test_minimized_window_only_blocks_readiness_and_preserves_pause(runtime, monkeypatch):
    runner, services, binding, _, errors, _, posted, _ = runtime
    runner.binding = binding
    services.attach(binding, runner._hook_persistence_failed)
    prepare = services.connection.prepare
    minimized = [True]
    def current(*args):
        result = prepare(*args)
        return replace(result, process=replace(result.process, minimized=minimized[0]))
    monkeypatch.setattr(services.connection, 'prepare', current)
    runner.tick_once()
    assert services.startup_check_state()['title'] == '微信窗口不可操作'
    assert not services.startup_check_state()['canStart']
    assert binding.run_status == 'paused' and not errors and not posted
    minimized[0] = False
    runner.tick_once()
    assert services.startup_check_state()['canStart']
    assert binding.run_status == 'paused' and not errors and not posted


@pytest.mark.parametrize('entry', ['connection', 'business'])
def test_sqlite_failure_in_both_loops_uses_same_fault_gate(runtime, monkeypatch, entry):
    import sqlite3
    runner, services, binding, _, errors, _, _, _ = runtime
    runner.binding = binding
    services.attach(binding, runner._hook_persistence_failed)
    def fail(*args):
        raise sqlite3.OperationalError('attempt to write a readonly database')
    if entry == 'connection':
        monkeypatch.setattr(services.connection, 'prepare', fail)
        runner.tick_once()
    else:
        services.dispatcher = SimpleNamespace(tick=fail, interval_seconds=.01)
        services.business_loop(runner, lambda _:None)
    assert binding.run_status == 'faulted' and runner.stop_event.is_set()
    assert emergency_stop_requested()
    assert len(errors) == 1 and '本机记录' in errors[0]
    assert services.startup_check_state()['canStart'] is False
    row = next(r for r in storage.read_logs() if r['event'] == 'hook_connection_fault')
    assert row['error_code'] == 'HOOK_LOCAL_STORAGE_FAILED'
