"""Repeated Hook faults must not erase a settled stop or weaken stop intents."""
from types import SimpleNamespace

import pytest

from chejin_worker_client import storage, task_runner as runner_module
from chejin_worker_client.hook_worker_services import HookWorkerServices
from test_emergency_status_settlement import stopped_runner


def confirmed(runner):
    runner.binding.run_status = "faulted"
    runner._backend_confirmed_run_status = "faulted"
    runner._pending_run_status_sync = None
    runner._run_status_persistence_pending = False
    runner._fault_recovery_requested = None
    runner._fault_recovery_processing = False


def test_repeated_hook_fault_keeps_confirmed_stop_and_durable_pause(stopped_runner):
    runner, posts = stopped_runner
    confirmed(runner)
    services = HookWorkerServices(None, None, intake_interval_seconds=1, projection_batch_size=1)
    revision = runner._run_status_revision
    for _ in range(3):
        services._fault(runner, "HOOK_CONNECTION_CHECK_FAILED")
        runner._sync_pending_run_status(force=True)
        assert runner._pending_run_status_sync is None
        assert runner._backend_confirmed_run_status == "faulted"
        assert runner._run_status_revision == revision
        assert storage.load_binding().run_status == "faulted"
        assert storage.load_runtime_control()["pause_requested"] is True
    assert posts == []


@pytest.mark.parametrize("change", ["new_fault", "pending", "persist", "recovery_requested", "recovery_processing", "explicit_pause"])
def test_new_stop_or_unsettled_work_still_invalidates_confirmation(stopped_runner, change):
    runner, _ = stopped_runner
    confirmed(runner)
    if change == "new_fault":
        runner.binding.run_status = runner._backend_confirmed_run_status = "paused"
    elif change == "pending":
        runner._pending_run_status_sync = "faulted"
    elif change == "persist":
        runner._run_status_persistence_pending = True
    elif change == "recovery_requested":
        runner._fault_recovery_requested = object()
    elif change == "recovery_processing":
        runner._fault_recovery_processing = True
    revision = runner._run_status_revision
    assert runner._preserve_stopped_run_status("paused" if change == "explicit_pause" else "faulted",
        repeated_observation=change != "explicit_pause")
    assert runner.binding.run_status == "faulted"
    assert runner._pending_run_status_sync == "faulted"
    assert runner._run_status_revision > revision
    assert runner._fault_recovery_requested is None


def test_reasserted_stop_persistence_failure_keeps_update_blocked(stopped_runner, monkeypatch):
    runner, _ = stopped_runner
    confirmed(runner)
    def fail(_):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(runner_module, "save_binding", fail)
    assert not runner._preserve_stopped_run_status("faulted", repeated_observation=True)
    assert runner._pending_run_status_sync == "faulted"
    assert runner._run_status_persistence_pending is True
    assert runner.binding.run_status == "faulted"


def test_late_start_cannot_override_explicit_pause(stopped_runner, monkeypatch):
    runner, _ = stopped_runner
    runner.binding.run_status = runner._backend_confirmed_run_status = "paused"
    runner._pending_run_status_sync = None
    monkeypatch.setattr(runner_module, "emergency_stop_requested", lambda: False)
    monkeypatch.setattr(runner, "_refresh_vision_credential", lambda _: True)
    def delayed_start(binding, status):
        assert status == "running"
        runner._preserve_stopped_run_status("paused")
        return SimpleNamespace(run_status="running")
    runner.api.set_run_status = delayed_start
    assert runner.set_run_status("running") is False
    assert runner.binding.run_status == "paused"
    assert runner._pending_run_status_sync == "paused"
    assert storage.load_binding().run_status == "paused"
