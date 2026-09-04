from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from chejin_worker_client.client_update import UpdateStateStore
from chejin_worker_client.models import Binding, ClientRelease
from chejin_worker_client.update_coordinator import UpdateCoordinator
import chejin_worker_client.update_coordinator as coordinator_module


def _release(
    *,
    available: bool = True,
    artifact_url: str = "https://download.example.test/client.zip",
) -> ClientRelease:
    return ClientRelease(
        update_available=available,
        latest_version="0.9.60" if available else "0.9.59",
        channel="gray",
        platform="windows-x64",
        artifact_url=artifact_url if available else None,
        artifact_size_bytes=10 if available else None,
        artifact_sha256="a" * 64 if available else None,
        manifest_signature="signed" if available else None,
        signature_key_id="gray-test" if available else None,
        git_commit="b" * 40 if available else None,
        package_manifest_sha256="c" * 64 if available else None,
        published_at="2026-09-01T00:00:00+00:00",
        release_notes="test",
        minimum_updater_version="0.9.59",
        rollback_safe=True,
    )


class FakeApi:
    def __init__(self, release: ClientRelease) -> None:
        self.release = release
        self.calls: list[dict[str, str]] = []

    def latest_client_release(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.release


class SequenceApi(FakeApi):
    def __init__(self, releases: list[ClientRelease]) -> None:
        super().__init__(releases[-1])
        self.releases = list(releases)

    def latest_client_release(self, **kwargs):
        self.calls.append(dict(kwargs))
        index = min(len(self.calls) - 1, len(self.releases) - 1)
        return self.releases[index]


class FakeRunner:
    def __init__(self, binding: Binding | None, events: list[str]) -> None:
        self.binding = binding
        self.events = events
        self.safe = False
        self.statuses: list[str] = []

    def set_run_status(self, status: str) -> bool:
        self.events.append(f"status:{status}")
        self.statuses.append(status)
        if self.binding:
            self.binding.run_status = status  # type: ignore[assignment]
        return True

    def update_install_safety_snapshot(self):
        self.events.append("safety")
        return {
            "safe": self.safe,
            "new_work_blocked": True,
            "backend_stopped_confirmed_or_unbound": True,
            "confirmed_run_status": (
                self.binding.run_status if self.binding else "unbound"
            ),
        }


class FakeProcess:
    def __init__(self, executable_path: Path | None = None) -> None:
        self.terminated = False
        self.pid = 9876
        self.update_process_identity = (
            {
                "pid": self.pid,
                "create_time": 1000.0,
                "executable_path": str(executable_path.resolve(strict=False)),
            }
            if executable_path is not None
            else None
        )

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


def _wait(coordinator: UpdateCoordinator, timeout: float = 2.0) -> None:
    worker = coordinator._worker
    assert worker is not None
    worker.join(timeout)
    assert not worker.is_alive()


def test_independent_updater_ready_timeout_matches_packaged_startup_budget() -> None:
    assert coordinator_module.UPDATER_READY_TIMEOUT_SECONDS == 120.0


def test_updater_launcher_injects_bounded_startup_diagnostic_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_popen(arguments, **kwargs):
        captured["arguments"] = arguments
        captured.update(kwargs)
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(coordinator_module.subprocess, "Popen", fake_popen)
    updater = tmp_path / "CheJinUpdater.exe"
    plan = tmp_path / "control" / "update-plan.json"
    UpdateCoordinator._launch_updater(updater, plan, "secret-token")

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["CHEJIN_UPDATER_DIAGNOSTIC_PATH"] == str(
        plan.parent / "updater-startup.jsonl"
    )
    assert "secret-token" not in environment["CHEJIN_UPDATER_DIAGNOSTIC_PATH"]


def test_no_update_does_not_pause_or_close_new_work_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding = Binding(
        "w", "worker-secret-token-must-not-enter-update-plan", "instance", run_status="running"
    )
    events: list[str] = []
    runner = FakeRunner(binding, events)
    gates: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append((blocked, update_request_id)),
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release(available=False)),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: events.append("exit"),
        state_store=UpdateStateStore(tmp_path / "update"),
        formal_package=True,
    )
    assert coordinator.check_for_updates() is True
    _wait(coordinator)
    assert gates == []
    assert runner.statuses == []
    assert "exit" not in events
    assert coordinator.state()["status_text"] == "当前已是最新版本"


def test_update_blocks_new_work_before_pause_and_waits_for_safe_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = Binding(
        "w",
        "worker-secret-token-must-not-enter-update-plan",
        "instance",
        run_status="running",
    )
    events: list[str] = []
    runner = FakeRunner(binding, events)
    store = UpdateStateStore(tmp_path / "update")
    current = tmp_path / "install" / "CheJinWorkerClient"
    current.mkdir(parents=True)
    (current / "CheJinUpdater.exe").write_bytes(b"updater")
    archive = tmp_path / "prepared" / "client.zip"
    staged = tmp_path / "prepared" / "staging" / "CheJinWorkerClient"
    staged.mkdir(parents=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archive")
    gates: list[tuple[bool, str | None]] = []

    def set_gate(blocked: bool, update_request_id=None):
        events.append(f"gate:{blocked}")
        gates.append((blocked, update_request_id))

    def prepare(_release_value, *, request_root, **_kwargs):
        events.append("prepare")
        return {
            "archive_path": str(archive),
            "package_root": str(staged),
            "package_manifest": {"version": "0.9.60"},
        }

    def launch(_updater: Path, plan_path: Path, _token: str):
        events.append("launch")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        Path(plan["updater_ready_path"]).write_text(
            json.dumps(
                {
                    "ready": True,
                    "update_request_id": plan["update_request_id"],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess(_updater)

    exit_called = threading.Event()
    waiting_snapshots = []
    monkeypatch.setattr(coordinator_module, "set_update_new_work_gate", set_gate)
    monkeypatch.setattr(coordinator_module, "prepare_release_package", prepare)
    monkeypatch.setattr(
        coordinator_module,
        "protected_update_snapshot",
        lambda: {"snapshot_sha256": "d" * 64},
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda state: waiting_snapshots.append(state["waiting_safety_snapshot"])
        if "waiting_safety_snapshot" in state else None,
        request_normal_exit=lambda: exit_called.set(),
        state_store=store,
        formal_package=True,
        current_program_dir=current,
        updater_launcher=launch,
        sleep=lambda _seconds: setattr(runner, "safe", True),
    )
    assert coordinator.check_for_updates() is True
    _wait(coordinator)
    assert events.index("gate:True") < events.index("status:paused") < events.index("prepare")
    assert "safety" in events
    assert any(snapshot["safe"] is False for snapshot in waiting_snapshots)
    assert exit_called.is_set()
    state = store.load()
    assert state["state"] == "installing"
    assert state["install_started"] is True
    plan = json.loads(Path(state["plan_path"]).read_text(encoding="utf-8"))
    assert plan["safe_boundary"]["safe"] is True
    assert plan["protected_data_snapshot"]["snapshot_sha256"] == "d" * 64
    assert plan["health_timeout_seconds"] == 120
    assert plan["result_timeout_seconds"] == 180
    assert state["updater_pid"] == 9876
    assert state["updater_create_time_epoch"] == 1000.0
    assert state["updater_executable_path"].endswith("CheJinUpdater.exe")
    persisted_state_text = store.state_path.read_text(encoding="utf-8")
    plan_text = Path(state["plan_path"]).read_text(encoding="utf-8")
    assert "worker-secret-token-must-not-enter-update-plan" not in plan_text
    assert "https://download.example.test/client.zip" not in persisted_state_text
    assert "https://download.example.test/client.zip" not in plan_text
    assert plan["release"]["artifact_sha256"] == "a" * 64


def test_expired_download_url_is_requeried_only_when_package_identity_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _release(artifact_url="https://download.example.test/lease-one")
    renewed = _release(artifact_url="https://download.example.test/lease-two")
    api = SequenceApi([first, renewed])
    binding = Binding("w", "token", "instance", run_status="paused")
    runner = FakeRunner(binding, [])
    runner.safe = True
    current = tmp_path / "install" / "CheJinWorkerClient"
    current.mkdir(parents=True)
    (current / "CheJinUpdater.exe").write_bytes(b"updater")
    archive = tmp_path / "prepared" / "client.zip"
    staged = tmp_path / "prepared" / "staging" / "CheJinWorkerClient"
    staged.mkdir(parents=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archive")
    prepared_urls: list[str | None] = []

    def prepare(release, *, request_root, **_kwargs):
        del request_root
        prepared_urls.append(release.artifact_url)
        if len(prepared_urls) == 1:
            raise coordinator_module.ClientUpdateError(
                "UPDATE_DOWNLOAD_URL_EXPIRED",
                "expired",
            )
        return {
            "archive_path": str(archive),
            "package_root": str(staged),
            "package_manifest": {"version": "0.9.60"},
        }

    def launch(_updater: Path, plan_path: Path, _token: str):
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        Path(plan["updater_ready_path"]).write_text(
            json.dumps(
                {
                    "ready": True,
                    "update_request_id": plan["update_request_id"],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess(_updater)

    monkeypatch.setattr(coordinator_module, "prepare_release_package", prepare)
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        coordinator_module,
        "protected_update_snapshot",
        lambda: {"snapshot_sha256": "d" * 64},
    )
    coordinator = UpdateCoordinator(
        api,  # type: ignore[arg-type]
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=UpdateStateStore(tmp_path / "update"),
        formal_package=True,
        current_program_dir=current,
        updater_launcher=launch,
    )

    assert coordinator.check_for_updates() is True
    _wait(coordinator)
    assert len(api.calls) == 2, coordinator.state()
    assert prepared_urls == [first.artifact_url, renewed.artifact_url]
    assert coordinator.state()["install_started"] is True


def test_expired_url_refresh_rejects_changed_package_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _release(artifact_url="https://download.example.test/lease-one")
    changed = ClientRelease(
        **{
            **first.__dict__,
            "artifact_url": "https://download.example.test/lease-two",
            "artifact_sha256": "f" * 64,
        }
    )
    api = SequenceApi([first, changed])
    binding = Binding("w", "token", "instance", run_status="paused")
    runner = FakeRunner(binding, [])
    monkeypatch.setattr(
        coordinator_module,
        "prepare_release_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            coordinator_module.ClientUpdateError(
                "UPDATE_DOWNLOAD_URL_EXPIRED",
                "expired",
            )
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda *_args, **_kwargs: None,
    )
    coordinator = UpdateCoordinator(
        api,  # type: ignore[arg-type]
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=UpdateStateStore(tmp_path / "update"),
        formal_package=True,
    )
    assert coordinator.check_for_updates() is True
    _wait(coordinator)
    assert coordinator.state()["result_code"] == "UPDATE_RELEASE_IDENTITY_CHANGED", coordinator.state()


def test_faulted_client_with_clear_boundary_can_install_without_becoming_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = Binding("w", "token", "instance", run_status="faulted")
    runner = FakeRunner(binding, [])
    runner.safe = True
    current = tmp_path / "install" / "CheJinWorkerClient"
    current.mkdir(parents=True)
    (current / "CheJinUpdater.exe").write_bytes(b"updater")
    archive = tmp_path / "prepared" / "client.zip"
    staged = tmp_path / "prepared" / "staging" / "CheJinWorkerClient"
    staged.mkdir(parents=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archive")

    monkeypatch.setattr(
        coordinator_module,
        "prepare_release_package",
        lambda *_args, **_kwargs: {
            "archive_path": str(archive),
            "package_root": str(staged),
            "package_manifest": {"version": "0.9.60"},
        },
    )
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        coordinator_module,
        "protected_update_snapshot",
        lambda: {"snapshot_sha256": "d" * 64},
    )

    def launch(_updater: Path, plan_path: Path, _token: str):
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        Path(plan["updater_ready_path"]).write_text(
            json.dumps(
                {
                    "ready": True,
                    "update_request_id": plan["update_request_id"],
                }
            ),
            encoding="utf-8",
        )
        return FakeProcess(_updater)

    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=UpdateStateStore(tmp_path / "update"),
        formal_package=True,
        current_program_dir=current,
        updater_launcher=launch,
    )
    assert coordinator.check_for_updates() is True
    _wait(coordinator)
    state = coordinator.state()
    assert "plan_path" in state, state
    plan = json.loads(Path(state["plan_path"]).read_text(encoding="utf-8"))
    assert state["install_started"] is True
    assert plan["pre_update_run_status"] == "faulted"
    assert plan["safe_boundary"]["confirmed_run_status"] == "faulted"
    assert binding.run_status == "faulted"
    assert runner.statuses == []


def test_prepare_failure_restores_running_only_without_new_operator_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = Binding("w", "token", "instance", run_status="running")
    events: list[str] = []
    runner = FakeRunner(binding, events)
    gates: list[bool] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append(blocked),
    )
    monkeypatch.setattr(
        coordinator_module,
        "prepare_release_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("download failed")),
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=UpdateStateStore(tmp_path / "update"),
        formal_package=True,
    )
    assert coordinator.check_for_updates() is True
    _wait(coordinator)
    assert runner.statuses == ["paused", "running"]
    assert gates == [True, False]


def test_fast_uat_runtime_cannot_start_formal_update(tmp_path: Path) -> None:
    events: list[str] = []
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(None, events),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: events.append("exit"),
        state_store=UpdateStateStore(tmp_path / "update"),
        formal_package=False,
    )
    assert coordinator.check_for_updates() is False
    assert coordinator.state()["result_code"] == "UPDATE_FORMAL_PACKAGE_REQUIRED"
    assert events == []


def test_invalid_updater_ready_marker_terminates_updater_before_restoring_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = Binding("w", "token", "instance", run_status="running")
    events: list[str] = []
    runner = FakeRunner(binding, events)
    runner.safe = True
    current = tmp_path / "install" / "CheJinWorkerClient"
    staged = tmp_path / "prepared" / "staging" / "CheJinWorkerClient"
    archive = tmp_path / "prepared" / "client.zip"
    current.mkdir(parents=True)
    staged.mkdir(parents=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"archive")
    (current / "CheJinUpdater.exe").write_bytes(b"updater")
    process = FakeProcess()
    gates: list[bool] = []

    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append(blocked),
    )
    monkeypatch.setattr(
        coordinator_module,
        "prepare_release_package",
        lambda *_args, **_kwargs: {
            "archive_path": str(archive),
            "package_root": str(staged),
            "package_manifest": {"version": "0.9.60"},
        },
    )
    monkeypatch.setattr(
        coordinator_module,
        "protected_update_snapshot",
        lambda: {"snapshot_sha256": "d" * 64},
    )

    def launch(_updater: Path, plan_path: Path, _token: str):
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        Path(plan["updater_ready_path"]).write_text(
            json.dumps({"ready": True, "update_request_id": "another-request"}),
            encoding="utf-8",
        )
        return process

    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: events.append("exit"),
        state_store=UpdateStateStore(tmp_path / "update"),
        formal_package=True,
        current_program_dir=current,
        updater_launcher=launch,
    )
    assert coordinator.check_for_updates() is True
    _wait(coordinator)

    assert process.terminated is True
    assert gates == [True, False]
    assert runner.statuses == ["paused", "running"]
    assert "exit" not in events
    assert coordinator.state()["result_code"] == "UPDATE_INSTALL_FAILED"


@pytest.mark.parametrize(
    ("binding_status", "operator_pause", "fault_after_request", "expected_statuses"),
    [
        ("paused", False, False, ["running"]),
        ("paused", True, False, []),
        ("faulted", False, True, []),
    ],
)
def test_restart_cancels_interrupted_preinstall_without_overriding_latest_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_status: str,
    operator_pause: bool,
    fault_after_request: bool,
    expected_statuses: list[str],
) -> None:
    binding = Binding("w", "token", "instance", run_status=binding_status)
    events: list[str] = []
    runner = FakeRunner(binding, events)
    store = UpdateStateStore(tmp_path / "update")
    store.save(
        {
            "state": "waiting_for_safe_boundary",
            "update_request_id": "request-old",
            "pre_update_run_status": "running",
            "operator_pause_after_request": operator_pause,
            "fault_after_request": fault_after_request,
            "install_started": False,
        }
    )
    gates: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append(
            (blocked, update_request_id)
        ),
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
    )

    coordinator.start_result_reconciliation()

    assert gates == [(False, "request-old")]
    assert runner.statuses == expected_statuses
    assert store.load()["result_code"] == "UPDATE_PREINSTALL_INTERRUPTED"


def test_update_payload_cleanup_is_request_scoped_and_keeps_audit_documents(
    tmp_path: Path,
) -> None:
    store = UpdateStateStore(tmp_path / "update")
    request_root = store.root / "requests" / "request-a"
    (request_root / "download").mkdir(parents=True)
    (request_root / "download" / "client-update.zip").write_bytes(b"archive")
    (request_root / "staging" / "CheJinWorkerClient").mkdir(parents=True)
    (request_root / "staging" / "CheJinWorkerClient" / "worker.exe").write_bytes(
        b"program"
    )
    (request_root / "control").mkdir()
    (request_root / "control" / "update-result.json").write_text(
        "{}",
        encoding="utf-8",
    )
    outside = store.root / "must-remain.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("keep", encoding="utf-8")
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(Binding("w", "token", "instance"), []),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
    )

    coordinator._cleanup_request_payload("request-a")
    coordinator._cleanup_request_payload("../must-not-escape")

    assert not (request_root / "download").exists()
    assert not (request_root / "staging").exists()
    assert (request_root / "control" / "update-result.json").is_file()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_success_result_retries_running_restore_after_backend_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = Binding("w", "token", "instance", run_status="paused")
    events: list[str] = []

    class RecoveringRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__(binding, events)
            self.attempts = 0

        def set_run_status(self, status: str) -> bool:
            self.events.append(f"status:{status}")
            self.statuses.append(status)
            self.attempts += 1
            if self.attempts == 1:
                return False
            binding.run_status = status  # type: ignore[assignment]
            return True

    runner = RecoveringRunner()
    store = UpdateStateStore(tmp_path / "update")
    control = tmp_path / "control"
    control.mkdir()
    plan_path = control / "update-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "update_request_id": "request-restore",
                "current_version": "0.9.59",
                "pre_update_run_status": "running",
                "operator_pause_after_request": False,
                "fault_after_request": False,
            }
        ),
        encoding="utf-8",
    )
    (control / "update-result.json").write_text(
        json.dumps(
            {
                "state": "succeeded",
                "result_code": "UPDATE_SUCCEEDED",
                "update_request_id": "request-restore",
            }
        ),
        encoding="utf-8",
    )
    store.save(
        {
            "state": "installing",
            "update_request_id": "request-restore",
            "plan_path": str(plan_path),
            "pre_update_run_status": "running",
            "install_started": True,
        }
    )
    gates: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append(
            (blocked, update_request_id)
        ),
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
        sleep=lambda _seconds: None,
    )

    coordinator.start_result_reconciliation()
    _wait(coordinator)

    assert gates == [(False, "request-restore")]
    assert runner.statuses == ["running", "running"]
    assert binding.run_status == "running"
    assert store.load()["status_restore_pending"] is False
    assert store.load()["result_reconciled"] is True

    coordinator.start_result_reconciliation()
    assert runner.statuses == ["running", "running"]


def test_success_result_never_overrides_operator_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = Binding("w", "token", "instance", run_status="paused")
    events: list[str] = []
    runner = FakeRunner(binding, events)
    store = UpdateStateStore(tmp_path / "update")
    control = tmp_path / "control"
    control.mkdir()
    plan_path = control / "update-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "update_request_id": "request-paused",
                "current_version": "0.9.59",
                "pre_update_run_status": "running",
                "operator_pause_after_request": True,
            }
        ),
        encoding="utf-8",
    )
    (control / "update-result.json").write_text(
        json.dumps(
            {
                "state": "rolled_back",
                "result_code": "UPDATE_ROLLED_BACK",
                "update_request_id": "request-paused",
            }
        ),
        encoding="utf-8",
    )
    store.save(
        {
            "state": "installing",
            "update_request_id": "request-paused",
            "plan_path": str(plan_path),
            "pre_update_run_status": "running",
            "operator_pause_after_request": True,
            "install_started": True,
        }
    )
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda *_args, **_kwargs: None,
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
    )

    coordinator.start_result_reconciliation()
    _wait(coordinator)

    assert runner.statuses == []
    assert binding.run_status == "paused"
    assert store.load()["result_reconciled"] is True


def _missing_result_plan(
    tmp_path: Path,
    *,
    request_id: str,
    token: str,
) -> tuple[UpdateStateStore, Path, dict]:
    store = UpdateStateStore(tmp_path / "update")
    control = store.root / "requests" / request_id / "control"
    control.mkdir(parents=True)
    current = tmp_path / "program" / "current"
    previous = tmp_path / "program" / "previous"
    data = tmp_path / "data"
    current.mkdir(parents=True)
    previous.mkdir(parents=True)
    data.mkdir()
    (control / "CheJinUpdater.exe").write_bytes(b"updater")
    plan = {
        "schema_version": 1,
        "update_request_id": request_id,
        "current_version": "0.9.58",
        "target_version": "0.9.59",
        "one_time_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "current_program_dir": str(current),
        "previous_program_dir": str(previous),
        "failed_program_dir": str(tmp_path / "program" / "failed"),
        "data_dir": str(data),
        "healthy_marker_path": str(control / "healthy.json"),
        "worker_executable_relative": "CheJinWorkerClient.exe",
        "health_timeout_seconds": 120,
        "result_timeout_seconds": 180,
        "pre_update_run_status": "paused",
        "release": {"artifact_sha256": "a" * 64},
    }
    plan_path = control / "update-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    store.save(
        {
            "state": "installing",
            "update_request_id": request_id,
            "plan_path": str(plan_path),
            "pre_update_run_status": "paused",
            "install_started": True,
            "updater_pid": 998877,
            "updater_create_time_epoch": 1000.0,
            "updater_executable_path": str(
                (control / "CheJinUpdater.exe").resolve(strict=False)
            ),
        }
    )
    return store, plan_path, plan


def _valid_health_marker(plan: dict, token: str) -> dict:
    return {
        "schema_version": 2,
        "healthy": True,
        "version": plan["target_version"],
        "update_request_id": plan["update_request_id"],
        "one_time_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "runtime_health": {
            "ready": True,
            "binding_state": "bound",
            "ui_event_loop_alive": True,
            "required_threads": [
                "task_runner",
                "c2_listener",
                "thread_monitor",
            ],
            "threads": {
                name: {"entered_loop": True, "alive": True}
                for name in ("task_runner", "c2_listener", "thread_monitor")
            },
            "startup_failures": [],
            "stable_sample_count": 5,
            "stable_for_ms": 1250,
        },
    }


def test_dead_updater_with_valid_runtime_health_settles_success_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "missing-result-token"
    store, plan_path, plan = _missing_result_plan(
        tmp_path,
        request_id="request-health-recovery",
        token=token,
    )
    Path(plan["healthy_marker_path"]).write_text(
        json.dumps(_valid_health_marker(plan, token)),
        encoding="utf-8",
    )
    gates: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append(
            (blocked, update_request_id)
        ),
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(None, []),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
        current_program_dir=Path(plan["current_program_dir"]),
        process_identity=lambda _pid: None,
        missing_result_grace_seconds=0,
    )
    coordinator.set_post_update_context(plan, token)
    coordinator.start_result_reconciliation()
    _wait(coordinator)

    result = json.loads(
        (plan_path.parent / "update-result.json").read_text(encoding="utf-8")
    )
    assert result["recovered_from_missing_updater_result"] is True
    assert store.load()["state"] == "succeeded"
    assert store.load()["result_reconciled"] is True
    assert gates == [(False, "request-health-recovery")]


def test_dead_updater_without_health_launches_single_rollback_recovery(
    tmp_path: Path,
) -> None:
    token = "rollback-token"
    store, plan_path, plan = _missing_result_plan(
        tmp_path,
        request_id="request-rollback-recovery",
        token=token,
    )
    launches: list[tuple[Path, Path, str, int]] = []
    exits: list[str] = []

    def launch(updater: Path, path: Path, raw_token: str, pid: int):
        launches.append((updater, path, raw_token, pid))
        (path.parent / "missing-result-recovery-ready.json").write_text(
            json.dumps(
                {"ready": True, "update_request_id": plan["update_request_id"]}
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(None, []),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: exits.append("exit"),
        state_store=store,
        formal_package=True,
        recovery_updater_launcher=launch,
        process_identity=lambda _pid: None,
        missing_result_grace_seconds=0,
    )
    coordinator.set_post_update_context(plan, token)
    coordinator.start_result_reconciliation()
    _wait(coordinator)

    assert len(launches) == 1
    assert exits == ["exit"]
    assert store.load()["state"] == "restarting"
    assert not (plan_path.parent / "update-result.json").exists()


def test_missing_result_without_authenticated_startup_context_is_finite_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "unavailable-token"
    store, _plan_path, _plan = _missing_result_plan(
        tmp_path,
        request_id="request-finite-failure",
        token=token,
    )
    gates: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append(
            (blocked, update_request_id)
        ),
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(None, []),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
        process_identity=lambda _pid: None,
        missing_result_grace_seconds=0,
    )
    coordinator.start_result_reconciliation()
    _wait(coordinator)

    state = store.load()
    assert state["state"] == "failed"
    assert state["result_code"] == "UPDATE_RESULT_MISSING"
    assert state["result_reconciled"] is True
    assert gates == [(False, "request-finite-failure")]


@pytest.mark.parametrize(
    ("result_payload", "expected_message"),
    [
        ("{broken-json", "Expecting property name"),
        (
            json.dumps(
                {
                    "state": "succeeded",
                    "result_code": "UPDATE_SUCCEEDED",
                    "update_request_id": "another-request",
                }
            ),
            "update result request mismatch",
        ),
    ],
)
def test_corrupt_or_mismatched_result_is_finitely_settled_and_clears_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_payload: str,
    expected_message: str,
) -> None:
    store, plan_path, _plan = _missing_result_plan(
        tmp_path,
        request_id="request-invalid-result",
        token="result-token",
    )
    (plan_path.parent / "update-result.json").write_text(
        result_payload,
        encoding="utf-8",
    )
    gates: list[tuple[bool, str | None]] = []
    runner = FakeRunner(
        Binding("worker", "token", "instance", run_status="paused"),
        [],
    )
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append(
            (blocked, update_request_id)
        ),
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        runner,  # type: ignore[arg-type]
        binding_provider=lambda: runner.binding,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
    )

    coordinator.start_result_reconciliation()
    _wait(coordinator)

    state = store.load()
    assert state["state"] == "failed"
    assert state["result_code"] == "UPDATE_STATE_INVALID"
    assert expected_message in state["message"]
    assert state["result_reconciled"] is True
    assert state["intake_gate_cleared"] is True
    assert gates == [(False, "request-invalid-result")]
    assert runner.statuses == []
    assert runner.binding.run_status == "paused"


def test_verified_hung_updater_is_terminated_then_recovered_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "hung-updater-token"
    store, plan_path, plan = _missing_result_plan(
        tmp_path,
        request_id="request-hung-updater",
        token=token,
    )
    updater_path = plan_path.parent / "CheJinUpdater.exe"
    plan["result_timeout_seconds"] = 5
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    expected_identity = {
        "pid": 998877,
        "create_time": 1000.0,
        "executable_path": str(updater_path.resolve(strict=False)),
    }
    state = store.load()
    store.save(
        {
            **state,
            "updater_create_time_epoch": 1000.0,
            "updater_executable_path": expected_identity["executable_path"],
        }
    )
    identities = {998877: dict(expected_identity)}
    terminations: list[int] = []
    launches: list[int] = []
    exits: list[str] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda *_args, **_kwargs: None,
    )

    def terminate(pid: int) -> bool:
        terminations.append(pid)
        identities.pop(pid, None)
        return True

    def launch(_updater: Path, path: Path, _token: str, _pid: int):
        launches.append(1)
        (path.parent / "missing-result-recovery-ready.json").write_text(
            json.dumps(
                {"ready": True, "update_request_id": plan["update_request_id"]}
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(None, []),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: exits.append("exit"),
        state_store=store,
        formal_package=True,
        current_program_dir=Path(plan["current_program_dir"]),
        recovery_updater_launcher=launch,
        process_identity=lambda pid: identities.get(pid),
        updater_terminator=terminate,
        wall_time=lambda: 1006.0,
        missing_result_grace_seconds=0,
    )
    coordinator.set_post_update_context(plan, token)
    coordinator.start_result_reconciliation()
    _wait(coordinator)

    assert terminations == [998877]
    assert launches == [1]
    assert exits == ["exit"]
    assert store.load()["result_recovery_started"] is True

    # A restarted/re-entered coordinator may observe the same incomplete
    # recovery state, but must never launch a second updater.
    coordinator.start_result_reconciliation()
    _wait(coordinator)
    assert launches == [1]
    assert store.load()["result_code"] == "UPDATE_RESULT_RECOVERY_INCOMPLETE"


def test_reused_updater_pid_is_not_treated_as_original_or_terminated(
    tmp_path: Path,
) -> None:
    token = "reused-pid-token"
    store, plan_path, plan = _missing_result_plan(
        tmp_path,
        request_id="request-reused-pid",
        token=token,
    )
    updater_path = plan_path.parent / "CheJinUpdater.exe"
    state = store.load()
    store.save(
        {
            **state,
            "updater_create_time_epoch": 1000.0,
            "updater_executable_path": str(updater_path.resolve(strict=False)),
        }
    )
    launches: list[int] = []
    terminations: list[int] = []

    def launch(_updater: Path, path: Path, _token: str, _pid: int):
        launches.append(1)
        (path.parent / "missing-result-recovery-ready.json").write_text(
            json.dumps(
                {"ready": True, "update_request_id": plan["update_request_id"]}
            ),
            encoding="utf-8",
        )
        return FakeProcess()

    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(None, []),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
        recovery_updater_launcher=launch,
        process_identity=lambda _pid: {
            "pid": 998877,
            "create_time": 2000.0,
            "executable_path": str(tmp_path / "unrelated.exe"),
        },
        updater_terminator=lambda pid: terminations.append(pid) or True,
        missing_result_grace_seconds=0,
    )
    coordinator.set_post_update_context(plan, token)
    coordinator.start_result_reconciliation()
    _wait(coordinator)

    assert terminations == []
    assert launches == [1]


def test_verified_updater_before_total_deadline_keeps_waiting_for_its_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, plan_path, plan = _missing_result_plan(
        tmp_path,
        request_id="request-live-updater",
        token="live-updater-token",
    )
    updater_path = plan_path.parent / "CheJinUpdater.exe"
    state = store.load()
    store.save(
        {
            **state,
            "updater_create_time_epoch": 1000.0,
            "updater_executable_path": str(updater_path.resolve(strict=False)),
        }
    )
    identity = {
        "pid": 998877,
        "create_time": 1000.0,
        "executable_path": str(updater_path.resolve(strict=False)),
    }
    sleeps: list[float] = []
    terminations: list[int] = []
    recoveries: list[int] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda *_args, **_kwargs: None,
    )

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        (plan_path.parent / "update-result.json").write_text(
            json.dumps(
                {
                    "state": "succeeded",
                    "result_code": "UPDATE_SUCCEEDED",
                    "update_request_id": plan["update_request_id"],
                }
            ),
            encoding="utf-8",
        )

    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(None, []),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
        process_identity=lambda _pid: dict(identity),
        updater_terminator=lambda pid: terminations.append(pid) or True,
        recovery_updater_launcher=lambda *_args: recoveries.append(1),
        wall_time=lambda: 1010.0,
        sleep=sleep,
    )
    coordinator.start_result_reconciliation()
    _wait(coordinator)

    assert sleeps == [0.2]
    assert terminations == []
    assert recoveries == []
    assert store.load()["state"] == "succeeded"
    assert store.load()["result_reconciled"] is True


def test_unverifiable_updater_identity_never_starts_parallel_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, plan_path, plan = _missing_result_plan(
        tmp_path,
        request_id="request-unverifiable-updater",
        token="unverifiable-token",
    )
    plan["result_timeout_seconds"] = 5
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    gates: list[tuple[bool, str | None]] = []
    recoveries: list[int] = []
    terminations: list[int] = []
    monkeypatch.setattr(
        coordinator_module,
        "set_update_new_work_gate",
        lambda blocked, update_request_id=None: gates.append(
            (blocked, update_request_id)
        ),
    )
    coordinator = UpdateCoordinator(
        FakeApi(_release()),
        FakeRunner(None, []),  # type: ignore[arg-type]
        binding_provider=lambda: None,
        on_state=lambda _state: None,
        request_normal_exit=lambda: None,
        state_store=store,
        formal_package=True,
        process_identity=lambda _pid: {"status": "unknown"},
        updater_terminator=lambda pid: terminations.append(pid) or True,
        recovery_updater_launcher=lambda *_args: recoveries.append(1),
        wall_time=lambda: 1006.0,
    )
    coordinator.start_result_reconciliation()
    _wait(coordinator)

    state = store.load()
    assert state["result_code"] == "UPDATE_UPDATER_IDENTITY_UNVERIFIABLE"
    assert state["result_reconciled"] is True
    assert state["intake_gate_cleared"] is False
    assert gates == []
    assert terminations == []
    assert recoveries == []
