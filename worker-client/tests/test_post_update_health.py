from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from chejin_worker_client import storage
from chejin_worker_client.models import Binding
import chejin_worker_client.post_update_health as health_module
import chejin_worker_client.update_data_snapshot as snapshot_module
from chejin_worker_client.task_runner import TaskRunner
from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests


def _runtime_health(*, alive: bool = True) -> dict:
    return {
        "ready": alive,
        "binding_state": "bound",
        "ui_event_loop_alive": True,
        "required_threads": ["task_runner", "c2_listener", "thread_monitor"],
        "threads": {
            name: {"entered_loop": True, "alive": alive}
            for name in ("task_runner", "c2_listener", "thread_monitor")
        },
        "startup_failures": [],
        "stable_sample_count": 3,
        "stable_for_ms": 1250,
    }


def _prepare_health_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, Path]:
    data = tmp_path / "data"
    current = tmp_path / "program" / "CheJinWorkerClient"
    current.mkdir(parents=True)
    worker = current / "CheJinWorkerClient.exe"
    worker.write_bytes(b"worker-v0.9.60")
    manifest = {
        "schema_version": 1,
        "version": "0.9.60",
        "platform": "windows-x64",
        "git_commit": "b" * 40,
        "rollback_safe": True,
        "files": {
            "CheJinWorkerClient.exe": hashlib.sha256(worker.read_bytes()).hexdigest(),
        },
    }
    (current / "update-package-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "APP_DIR", data)
    monkeypatch.setattr(storage, "DB_FILE", data / "worker_client.sqlite3")
    monkeypatch.setattr(snapshot_module, "CONFIG", SimpleNamespace(app_dir=data))
    monkeypatch.setattr(health_module, "CONFIG", SimpleNamespace(app_dir=data))
    monkeypatch.setattr(health_module, "__version__", "0.9.60")
    monkeypatch.setattr(health_module.sys, "executable", str(worker))
    storage.connect().close()
    token = "health-token"
    plan = {
        "schema_version": 1,
        "update_request_id": "update-health",
        "one_time_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "target_version": "0.9.60",
        "current_program_dir": str(current),
        "healthy_marker_path": str(tmp_path / "control" / "healthy.json"),
        "health_timeout_seconds": 120,
        "protected_data_snapshot": snapshot_module.protected_update_snapshot(),
    }
    plan_path = tmp_path / "control" / "update-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path, token, worker


def test_post_update_health_is_local_and_writes_authenticated_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, token, _worker = _prepare_health_plan(tmp_path, monkeypatch)

    plan = health_module.verify_post_update_startup(plan_path, token)
    marker_path = health_module.write_healthy_marker(
        plan,
        token,
        runtime_health=_runtime_health(),
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))

    assert marker["healthy"] is True
    assert marker["version"] == "0.9.60"
    assert marker["update_request_id"] == "update-health"
    assert marker["one_time_token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert marker["runtime_health"]["threads"]["task_runner"]["alive"] is True
    assert health_module.authenticated_healthy_marker(plan, token) == marker


def test_post_update_health_rejects_program_or_business_data_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path, token, worker = _prepare_health_plan(tmp_path, monkeypatch)
    worker.write_bytes(b"tampered-worker")
    with pytest.raises(RuntimeError, match="UPDATE_STARTUP_FILE_HASH_MISMATCH"):
        health_module.verify_post_update_startup(plan_path, token)

    plan_path, token, worker = _prepare_health_plan(tmp_path / "second", monkeypatch)
    storage.save_binding(
        Binding("worker", "secret", "instance", run_status="paused")
    )
    with pytest.raises(RuntimeError, match="UPDATE_PROTECTED_DATABASE_CHANGED"):
        health_module.verify_post_update_startup(plan_path, token)


def test_runtime_must_remain_alive_across_stable_window_before_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    writes: list[dict] = []
    monkeypatch.setattr(
        health_module,
        "write_healthy_marker",
        lambda _plan, _token, *, runtime_health: (
            writes.append(dict(runtime_health)) or Path("healthy.json")
        ),
    )
    gate = health_module.RuntimeHealthGate(
        {},
        "token",
        monotonic=lambda: now[0],
        minimum_stable_seconds=1.0,
        minimum_samples=2,
        timeout_seconds=5.0,
    )

    first = _runtime_health()
    first.pop("stable_sample_count")
    first.pop("stable_for_ms")
    assert gate.observe(first) is None
    now[0] = 0.5
    assert gate.observe({**first, "ready": False}) is None
    assert writes == []

    now[0] = 1.0
    assert gate.observe(first) is None
    now[0] = 2.1
    assert gate.observe(first) == Path("healthy.json")
    assert writes[0]["stable_sample_count"] == 2
    assert writes[0]["stable_for_ms"] == 1100


def test_runtime_health_marker_rejects_exited_background_thread() -> None:
    with pytest.raises(RuntimeError, match="UPDATE_RUNTIME_NOT_READY"):
        health_module.write_healthy_marker(
            {"healthy_marker_path": "/tmp/should-not-exist.json"},
            "token",
            runtime_health=_runtime_health(alive=False),
        )


def test_runtime_health_gate_uses_plan_120_second_timeout_without_10_second_cap(
    tmp_path: Path,
) -> None:
    now = [0.0]
    gate = health_module.RuntimeHealthGate(
        {
            "healthy_marker_path": str(tmp_path / "healthy.json"),
            "health_timeout_seconds": 120,
        },
        "token",
        monotonic=lambda: now[0],
    )

    now[0] = 10.1
    assert gate.observe({"ready": False}) is None
    now[0] = 120.1
    with pytest.raises(RuntimeError, match="UPDATE_RUNTIME_HEALTH_TIMEOUT"):
        gate.observe({"ready": False})


def test_runtime_health_gate_rejects_plan_without_health_timeout() -> None:
    with pytest.raises(
        RuntimeError,
        match="UPDATE_RUNTIME_HEALTH_TIMEOUT_MISSING",
    ):
        health_module.RuntimeHealthGate({}, "token")


def test_production_task_runner_reports_all_required_loops_entered_and_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "APP_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_FILE", tmp_path / "worker_client.sqlite3")
    storage.connect().close()
    runner = TaskRunner(
        SimpleNamespace(),
        SimpleNamespace(),
        on_profile=lambda _value: None,
        on_status=lambda _value: None,
        on_step=lambda _value: None,
        on_task=lambda _value: None,
        on_result=lambda _value: None,
        on_error=lambda _value: None,
    )
    runner._maybe_cleanup_artifacts = lambda **_kwargs: None  # type: ignore[method-assign]
    runner.tick_once = lambda: runner.stop_event.wait()  # type: ignore[method-assign]
    runner._c2_dependencies_ready = lambda: False  # type: ignore[method-assign]
    runner.start(
        Binding(
            "worker",
            "token",
            "instance",
            run_status="paused",
        )
    )
    try:
        deadline = time.monotonic() + 2.0
        snapshot = runner.post_update_runtime_health_snapshot()
        while not snapshot["ready"] and time.monotonic() < deadline:
            time.sleep(0.01)
            snapshot = runner.post_update_runtime_health_snapshot()
        assert snapshot["ready"] is True
        assert snapshot["required_threads"] == [
            "task_runner",
            "thread_monitor",
            "c2_listener",
        ]
        assert all(
            item == {"entered_loop": True, "alive": True}
            for item in snapshot["threads"].values()
        )
    finally:
        runner.stop()
        for thread in (runner.thread, runner.c2_thread, runner.thread_monitor):
            if thread is not None:
                thread.join(1.0)


def test_production_loop_exit_during_stability_window_writes_no_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "APP_DIR", tmp_path / "data")
    monkeypatch.setattr(
        storage,
        "DB_FILE",
        tmp_path / "data" / "worker_client.sqlite3",
    )
    storage.connect().close()
    release_task_loop = threading.Event()
    runner = TaskRunner(
        SimpleNamespace(),
        SimpleNamespace(),
        on_profile=lambda _value: None,
        on_status=lambda _value: None,
        on_step=lambda _value: None,
        on_task=lambda _value: None,
        on_result=lambda _value: None,
        on_error=lambda _value: None,
    )
    runner._maybe_cleanup_artifacts = lambda **_kwargs: None  # type: ignore[method-assign]

    def short_task_loop() -> None:
        runner._mark_background_loop_entered("task_runner")
        release_task_loop.wait()

    def held_c2_loop() -> None:
        runner._mark_background_loop_entered("c2_listener")
        runner.stop_event.wait()

    runner._loop = short_task_loop  # type: ignore[method-assign]
    runner._c2_loop = held_c2_loop  # type: ignore[method-assign]
    runner.start(Binding("worker", "token", "instance", run_status="paused"))
    now = [0.0]
    gate = health_module.RuntimeHealthGate(
        {"healthy_marker_path": str(tmp_path / "healthy.json")},
        "token",
        monotonic=lambda: now[0],
        minimum_stable_seconds=1.0,
        timeout_seconds=5.0,
    )
    try:
        deadline = time.monotonic() + 2.0
        first = runner.post_update_runtime_health_snapshot()
        while not first["ready"] and time.monotonic() < deadline:
            time.sleep(0.01)
            first = runner.post_update_runtime_health_snapshot()
        first["ui_event_loop_alive"] = True
        assert first["ready"] is True
        assert gate.observe(first) is None

        release_task_loop.set()
        deadline = time.monotonic() + 2.0
        failed = runner.post_update_runtime_health_snapshot()
        while failed["ready"] and time.monotonic() < deadline:
            time.sleep(0.01)
            failed = runner.post_update_runtime_health_snapshot()
        failed["ui_event_loop_alive"] = True
        now[0] = 1.5
        assert failed["ready"] is False
        assert gate.observe(failed) is None
        assert not (tmp_path / "healthy.json").exists()
    finally:
        runner.stop()
        release_task_loop.set()
        for thread in (runner.thread, runner.c2_thread, runner.thread_monitor):
            if thread is not None:
                thread.join(1.0)
        reset_emergency_stop_for_tests()
