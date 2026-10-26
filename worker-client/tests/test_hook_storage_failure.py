"""Storage failure stops admission; teardown must signal writers before joining."""
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from chejin_worker_client import storage, task_runner
from chejin_worker_client.emergency_stop import emergency_stop_requested, reset_emergency_stop_for_tests
from chejin_worker_client.hook_worker_services import HookWorkerServices
from chejin_worker_client.task_runner import TaskRunner


@pytest.fixture(autouse=True)
def emergency_state():
    reset_emergency_stop_for_tests()
    yield
    reset_emergency_stop_for_tests()


def test_failed_connection_loop_stops_business_before_join(monkeypatch):
    stopped = threading.Event()
    closed = []
    error = sqlite3.OperationalError('attempt to write a readonly database')
    class BusinessThread:
        def join(self, timeout):
            assert stopped.is_set(), 'business writer was never told to stop'
            assert emergency_stop_requested(), 'new actions must already be blocked'
        def is_alive(self):
            return False
    def fail():
        raise error
    runner = SimpleNamespace(stop_event=stopped, _task_wake_event=threading.Event(),
        _mark_background_loop_entered=lambda _:None, tick_once=fail,
        hook_business_thread=BusinessThread(), hook_services=SimpleNamespace(close=lambda:closed.append(True)))
    with pytest.raises(sqlite3.OperationalError) as raised:
        TaskRunner._hook_loop(runner)
    assert raised.value is error and closed == [True]


def test_inflight_writer_still_blocks_receiver_close():
    closed = []
    stopped = threading.Event(); stopped.set()
    class BusinessThread:
        def join(self, timeout):
            assert timeout == 90
        def is_alive(self):
            return True
    runner = SimpleNamespace(stop_event=stopped, _mark_background_loop_entered=lambda _:None,
        hook_business_thread=BusinessThread(), hook_services=SimpleNamespace(close=lambda:closed.append(True)))
    with pytest.raises(RuntimeError, match='HOOK_BUSINESS_WRITER_NOT_STOPPED'):
        TaskRunner._hook_loop(runner)
    assert not closed


@pytest.mark.parametrize('log_writable', [True, False])
def test_sqlite_fault_uses_storage_message_and_keeps_stop_gate(monkeypatch, log_writable):
    errors, statuses, logs, fallbacks = [], [], [], []
    services = HookWorkerServices(None, None, intake_interval_seconds=1, projection_batch_size=1)
    runner = SimpleNamespace(stop_event=threading.Event(), on_error=errors.append,
        _preserve_stopped_run_status=lambda status, **kwargs: statuses.append(status))
    def log(*args, **kwargs):
        logs.append(kwargs)
        if not log_writable:
            raise sqlite3.OperationalError('attempt to write a readonly database')
    monkeypatch.setattr(storage, 'append_log', log)
    monkeypatch.setattr('chejin_worker_client.failure_evidence.record_capture_failure',
        lambda *args, **kwargs:fallbacks.append(kwargs))
    services._fault(runner, 'HOOK_CONNECTION_CHECK_FAILED',
        local_error=sqlite3.OperationalError('attempt to write a readonly database'))
    assert statuses == ['faulted']
    assert runner.stop_event.is_set() and emergency_stop_requested()
    assert services.startup_check_state()['canStart'] is False
    assert '本机记录' in services.startup_check_state()['title']
    assert logs[0]['error_code'] == 'HOOK_LOCAL_STORAGE_FAILED'
    assert len(errors) == 1 and '本机记录' in errors[0] and '检查微信' not in errors[0]
    assert len(fallbacks) == (0 if log_writable else 1)


def test_supervisor_notifies_even_if_fault_and_pause_writes_fail(monkeypatch):
    errors, fallbacks = [], []
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('attempt to write a readonly database')
    monkeypatch.setattr(task_runner, 'append_log', fail)
    monkeypatch.setattr('chejin_worker_client.failure_evidence.record_capture_failure',
        lambda *args, **kwargs:fallbacks.append(kwargs))
    runner = SimpleNamespace(binding=SimpleNamespace(run_status='running'),
        _thread_health_lock=threading.RLock(), _thread_failure_reported=set(),
        _apply_local_run_status=fail, c2_stats={}, on_error=errors.append)
    TaskRunner._handle_background_thread_failure(runner, 'task_runner',
        error_code='WORKER_BACKGROUND_THREAD_CRASHED', message='synthetic', traceback_text='')
    assert emergency_stop_requested() and runner.binding.run_status == 'paused'
    assert len(errors) == 1 and len(fallbacks) == 1


def test_sqlite_diagnostics_keep_fixed_category_without_error_text(tmp_path):
    from chejin_worker_client.failure_evidence import exception_details
    with sqlite3.connect(tmp_path/'synthetic.sqlite3') as conn:
        conn.execute('CREATE TABLE sample (value)')
        conn.execute('PRAGMA query_only=ON')
        try:
            conn.execute('INSERT INTO sample VALUES (1)')
        except sqlite3.OperationalError as exc:
            details = exception_details(exc)
    assert details['sqlite_errorname'] == 'SQLITE_READONLY'
    assert details['sqlite_errorcode'] == sqlite3.SQLITE_READONLY
    assert 'readonly database' not in str(details)


def test_error_notice_still_publishes_if_database_log_is_readonly(monkeypatch):
    from test_web_ui_binding_behavior import _headless_web_ui_module
    published, fallbacks = [], []
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('attempt to write a readonly database')
    monkeypatch.setattr(storage, 'append_log', fail)
    monkeypatch.setattr('chejin_worker_client.failure_evidence.record_capture_failure',
        lambda *args, **kwargs:fallbacks.append(kwargs))
    with _headless_web_ui_module() as module:
        module.WorkerWebWindow.on_error(SimpleNamespace(_publish=lambda:published.append(True)),
            '本机记录读写失败')
    assert published == [True] and len(fallbacks) == 1
