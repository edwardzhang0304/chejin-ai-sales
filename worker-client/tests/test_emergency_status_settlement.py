"""Emergency stop may settle only stopped status; real SQLite, controlled API/time."""
from types import SimpleNamespace

import pytest

from chejin_worker_client import storage
from chejin_worker_client import task_runner as runner_module
from chejin_worker_client.emergency_stop import (
    emergency_stop_requested, reset_emergency_stop_for_tests, trigger_emergency_stop,
)
from chejin_worker_client.models import Binding, WorkerProfile
from chejin_worker_client.task_runner import TaskRunner


@pytest.fixture
def stopped_runner(tmp_path, monkeypatch):
    reset_emergency_stop_for_tests()
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")
    posts = []
    def set_status(binding, status):
        posts.append(status)
        return WorkerProfile(id=binding.worker_id, worker_name="Synthetic", run_status=status)
    def no_ui():
        pytest.fail("Emergency settlement reached WeChat probe")
    api = SimpleNamespace(set_run_status=set_status)
    runner = TaskRunner(api, SimpleNamespace(probe=no_ui),
        on_profile=lambda _: None, on_status=lambda _: None, on_step=lambda _: None,
        on_task=lambda _: None, on_result=lambda _: None, on_error=lambda _: None)
    runner.binding = Binding(worker_id="synthetic-worker", worker_token="synthetic-token",
                             client_instance_id="synthetic-client", run_status="faulted")
    storage.save_binding(runner.binding)
    storage.request_runtime_pause()
    trigger_emergency_stop(reason="RUN_STATUS_PERSISTENCE_FAILED", origin="unit")
    try:
        yield runner, posts
    finally:
        reset_emergency_stop_for_tests()


@pytest.mark.parametrize("pending", [None, "running"])
def test_emergency_never_synchronizes_a_start(stopped_runner, pending):
    runner, posts = stopped_runner
    runner._pending_run_status_sync = pending
    runner._tick_once()
    assert posts == []
    assert storage.load_binding().run_status == "faulted"
    assert storage.load_runtime_control()["pause_requested"]
    assert emergency_stop_requested() and not runner._can_start_new_flow()


@pytest.mark.parametrize("stopped", ["paused", "faulted"])
def test_stopped_sync_retries_with_original_backoff_without_wechat(stopped_runner, monkeypatch, stopped):
    runner, posts = stopped_runner
    runner.binding.run_status = stopped
    storage.save_binding(runner.binding)
    runner._pending_run_status_sync = stopped
    runner._run_status_persistence_pending = True
    now = [100.0]
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: now[0])
    original = runner.api.set_run_status
    attempts = []
    def fail_once(binding, status):
        attempts.append(now[0])
        if len(attempts) == 1:
            raise TimeoutError("Synthetic unavailable backend")
        return original(binding, status)
    runner.api.set_run_status = fail_once
    runner._tick_once()
    assert runner._pending_run_status_sync == stopped
    now[0] = 104.99
    runner._tick_once()
    assert attempts == [100.0]
    now[0] = 105.0
    runner._tick_once()
    assert attempts == [100.0, 105.0] and posts == [stopped]
    assert runner._pending_run_status_sync is None
    assert not runner._run_status_persistence_pending
    assert storage.load_binding().run_status == stopped
    assert storage.load_runtime_control()["pause_requested"]
    assert emergency_stop_requested() and not runner._can_start_new_flow()


def test_repeated_disk_failure_preserves_stop_and_retries_only_status(stopped_runner, monkeypatch):
    runner, posts = stopped_runner
    runner._pending_run_status_sync = "faulted"
    runner._run_status_persistence_pending = True
    now = [100.0]
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: now[0])
    def unavailable_disk(_):
        raise OSError("Synthetic disk unavailable")
    monkeypatch.setattr(runner_module, "save_binding", unavailable_disk)
    for when in [100.0, 101.0, 105.0, 110.0]:
        now[0] = when
        runner._tick_once()
        assert runner._pending_run_status_sync == "faulted"
        assert runner._run_status_persistence_pending
        assert storage.load_binding().run_status == "faulted"
        assert storage.load_runtime_control()["pause_requested"]
        assert emergency_stop_requested() and not runner._can_start_new_flow()
    assert posts == ["faulted", "faulted", "faulted"]
