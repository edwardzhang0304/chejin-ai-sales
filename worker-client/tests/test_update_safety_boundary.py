from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from chejin_worker_client.models import Binding, Task
from chejin_worker_client.task_runner import TaskRunner
import chejin_worker_client.task_runner as runner_module


ZERO_DURABLE = {
    "waiting_ledger": 0,
    "pending_c2_outbox": 0,
    "pending_sqlite_action_journal": 0,
    "pending_file_action_journal": 0,
    "pending_sent_ack": 0,
    "action_journal_state_unavailable": 0,
}


def _runner() -> TaskRunner:
    runner = object.__new__(TaskRunner)
    runner.binding = Binding("worker", "token", "instance", run_status="paused")
    runner._pending_run_status_sync = None
    runner._backend_confirmed_run_status = "paused"
    runner.bridge = SimpleNamespace(sidecar_active=lambda: False)
    runner.current_task = None
    runner.current_task_lease = None
    runner.api = SimpleNamespace(task_lease_fencing_tokens={}, inflight_flow_id=None)
    runner.current_ui_lock = None
    runner.task_lock = threading.Lock()
    runner._new_work_admission_lock = threading.RLock()
    runner.stop_event = threading.Event()
    runner._restart_backend_probe_pending = False
    return runner


def test_install_boundary_requires_every_business_and_physical_action_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    control = {
        "update_no_new_work": True,
        "update_request_id": "update-a",
        "inflight_flow_id": None,
    }
    durable = dict(ZERO_DURABLE)
    monkeypatch.setattr(runner_module, "load_runtime_control", lambda: dict(control))
    monkeypatch.setattr(runner_module, "update_install_business_blockers", lambda: dict(durable))
    monkeypatch.setattr(runner_module, "lock_summary", lambda: {"locked": False})

    assert runner.update_install_safety_snapshot()["safe"] is True

    for key in ZERO_DURABLE:
        durable[key] = 1
        snapshot = runner.update_install_safety_snapshot()
        assert snapshot["safe"] is False, key
        durable[key] = 0

    runner.current_task = SimpleNamespace(id="task-a")
    assert runner.update_install_safety_snapshot()["safe"] is False
    runner.current_task = None
    runner.current_task_lease = object()
    snapshot = runner.update_install_safety_snapshot()
    assert snapshot["safe"] is False
    assert snapshot["task_lease_guard_active"] is True
    runner.current_task_lease = None
    runner.api.task_lease_fencing_tokens["task-a"] = 7
    snapshot = runner.update_install_safety_snapshot()
    assert snapshot["safe"] is False
    assert snapshot["waiting_reason_code"] == "UPDATE_WAITING_TASK_LEASE"
    assert snapshot["cached_task_lease_count"] == 1
    assert snapshot["task_lease_guard_active"] is False
    assert "7" not in str(snapshot)  # evidence does not expose fencing tokens
    runner.api.task_lease_fencing_tokens.clear()
    runner.current_ui_lock = object()
    assert runner.update_install_safety_snapshot()["safe"] is False
    runner.current_ui_lock = None
    runner.bridge = SimpleNamespace(sidecar_active=lambda: True)
    assert runner.update_install_safety_snapshot()["safe"] is False


def test_faulted_backend_confirmation_allows_install_only_when_every_ledger_is_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    runner.binding.run_status = "faulted"
    runner._backend_confirmed_run_status = "faulted"
    control = {
        "update_no_new_work": True,
        "update_request_id": "update-faulted",
        "inflight_flow_id": None,
    }
    durable = dict(ZERO_DURABLE)
    monkeypatch.setattr(runner_module, "load_runtime_control", lambda: dict(control))
    monkeypatch.setattr(runner_module, "update_install_business_blockers", lambda: dict(durable))
    monkeypatch.setattr(runner_module, "lock_summary", lambda: {"locked": False})

    snapshot = runner.update_install_safety_snapshot()
    assert snapshot["safe"] is True
    assert snapshot["confirmed_run_status"] == "faulted"

    for blocker in ("pending_sqlite_action_journal", "pending_c2_outbox", "pending_sent_ack"):
        durable[blocker] = 1
        blocked = runner.update_install_safety_snapshot()
        assert blocked["safe"] is False
        assert blocked["waiting_reason_code"] == "UPDATE_WAITING_DURABLE_SETTLEMENT"
        durable[blocker] = 0


def test_unconfirmed_backend_status_has_explicit_waiting_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    runner.binding.run_status = "faulted"
    runner._backend_confirmed_run_status = "paused"
    monkeypatch.setattr(
        runner_module,
        "load_runtime_control",
        lambda: {"update_no_new_work": True, "inflight_flow_id": None},
    )
    monkeypatch.setattr(runner_module, "update_install_business_blockers", lambda: dict(ZERO_DURABLE))
    monkeypatch.setattr(runner_module, "lock_summary", lambda: {"locked": False})

    snapshot = runner.update_install_safety_snapshot()
    assert snapshot["safe"] is False
    assert snapshot["waiting_reason_code"] == "UPDATE_WAITING_BACKEND_RUN_STATUS_CONFIRMATION"
    assert "后端确认" in snapshot["waiting_reason_text"]


@pytest.mark.parametrize(
    "flow_kind",
    ["c1_task", "c2_read", "c3_reply", "c4_recall", "recovery"],
)
def test_update_gate_blocks_new_work_but_never_cancels_bound_inflight_flow(
    monkeypatch: pytest.MonkeyPatch,
    flow_kind: str,
) -> None:
    del flow_kind  # All C1-C4/recovery flows share the same production barrier.
    runner = _runner()
    runner.binding.run_status = "running"
    runner.api.inflight_flow_id = "flow-a"
    runner._backend_inflight_flow_state = {"flow_id": "flow-a", "status": "active"}
    monkeypatch.setattr(
        runner_module,
        "load_runtime_control",
        lambda: {
            "update_no_new_work": True,
            "update_request_id": "update-a",
            "pause_requested": False,
            "inflight_flow_id": "flow-a",
        },
    )
    monkeypatch.setattr(runner_module, "emergency_stop_requested", lambda: False)

    assert runner._can_start_new_flow(runner.binding) is False
    assert runner._can_continue_inflight_flow("flow-a") is True


def test_update_gate_rejects_operator_start_before_backend_status_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    errors: list[str] = []
    backend_calls: list[str] = []
    runner.on_error = errors.append
    runner.api = SimpleNamespace(
        task_lease_fencing_tokens={},
        inflight_flow_id=None,
        set_run_status=lambda _binding, status: backend_calls.append(status),
    )
    monkeypatch.setattr(
        runner_module,
        "load_runtime_control",
        lambda: {"update_no_new_work": True, "inflight_flow_id": None},
    )

    assert runner.set_run_status("running") is False
    assert runner.binding.run_status == "paused"
    assert backend_calls == []
    assert errors == ["客户端更新进行中，安装完成或取消后才能开始接单。"]


def test_task_pull_and_update_gate_have_no_orphan_lease_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    runner.binding.run_status = "running"
    control = {
        "update_no_new_work": False,
        "update_request_id": None,
        "pause_requested": False,
        "inflight_flow_id": None,
    }
    pull_entered = threading.Event()
    allow_pull_return = threading.Event()
    gate_finished = threading.Event()
    execution_finished = threading.Event()
    continuation: list[bool] = []

    class PullApi:
        def __init__(self) -> None:
            self.inflight_flow_id = None
            self.inflight_flow_state = {}
            self.task_lease_fencing_tokens = {}
            self.pull_count = 0

        def pull_task(self, _binding):
            self.pull_count += 1
            pull_entered.set()
            assert allow_pull_return.wait(2)
            return (
                "new",
                Task(
                    id="task-admission-race",
                    task_type="add_friend",
                    status="pending",
                ),
                None,
            )

        def start_inflight_flow(self, _binding, **kwargs):
            self.inflight_flow_id = kwargs["flow_id"]
            self.inflight_flow_state = {
                "flow_id": kwargs["flow_id"],
                "status": "active",
            }
            return dict(self.inflight_flow_state)

    api = PullApi()
    runner.api = api
    runner._backend_inflight_flow_state = {}
    runner._worker_transaction_barrier_ready = lambda *_args, **_kwargs: True

    def begin_flow(flow_id: str, flow_kind: str):
        control["inflight_flow_id"] = flow_id
        control["inflight_flow_kind"] = flow_kind
        return dict(control)

    def set_gate(blocked: bool, *, update_request_id=None):
        control["update_no_new_work"] = blocked
        control["update_request_id"] = update_request_id if blocked else None
        return dict(control)

    def execute_started_flow(*_args, **kwargs):
        assert kwargs["flow_already_started"] is True
        assert gate_finished.wait(2)
        continuation.append(
            runner._can_continue_inflight_flow("task-admission-race")
        )
        execution_finished.set()

    monkeypatch.setattr(runner_module, "load_runtime_control", lambda: dict(control))
    monkeypatch.setattr(runner_module, "begin_runtime_flow", begin_flow)
    monkeypatch.setattr(runner_module, "set_update_new_work_gate", set_gate)
    monkeypatch.setattr(runner_module, "lock_summary", lambda: {"locked": False})
    runner._execute_task = execute_started_flow

    pull_thread = threading.Thread(
        target=runner._pull_and_execute,
        args=(runner.binding,),
    )
    pull_thread.start()
    assert pull_entered.wait(2)

    gate_thread = threading.Thread(
        target=lambda: (
            runner.set_update_new_work_gate(
                True,
                update_request_id="update-race",
            ),
            gate_finished.set(),
        )
    )
    gate_thread.start()
    assert not gate_finished.wait(0.05), "gate must wait for pull admission"
    allow_pull_return.set()

    assert execution_finished.wait(2)
    pull_thread.join(2)
    gate_thread.join(2)
    assert not pull_thread.is_alive()
    assert not gate_thread.is_alive()
    assert api.pull_count == 1
    assert control["update_no_new_work"] is True
    assert control["inflight_flow_id"] == "task-admission-race"
    assert api.inflight_flow_id == "task-admission-race"
    assert continuation == [True]
