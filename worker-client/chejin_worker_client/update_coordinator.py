from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

import psutil

from . import __version__
from .api import WorkerApiClient
from .client_update import (
    ClientUpdateError,
    UpdateStateStore,
    is_formal_update_runtime,
    prepare_release_package,
    update_status_text,
)
from .config import CONFIG
from .models import Binding, ClientRelease
from .post_update_health import authenticated_healthy_marker
from .storage import append_log, set_update_new_work_gate
from .task_runner import TaskRunner
from .update_data_snapshot import protected_update_snapshot


UPDATER_CREATE_TIME_TOLERANCE_SECONDS = 0.01
UPDATER_READY_TIMEOUT_SECONDS = 120.0


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _safe_update_log(
    level: str,
    event: str,
    message: str,
    **kwargs: Any,
) -> None:
    try:
        append_log(level, event, message, **kwargs)
    except Exception:
        # Diagnostics must never decide whether a verified installer starts,
        # whether the old client exits, or whether the intake gate is restored.
        return


def _persisted_release_identity(release: ClientRelease) -> dict[str, Any]:
    """Return the API release snapshot without persisting a presigned URL."""

    identity = asdict(release)
    identity.pop("artifact_url", None)
    return identity


def _download_release_identity(release: ClientRelease) -> dict[str, Any]:
    """Return exactly the client-visible immutable facts used for URL renewal.

    ``artifact_storage_key`` is immutable in the backend publication record,
    but is deliberately not exposed to the client.  Presentation fields such
    as release notes and response-state fields therefore cannot participate in
    deciding whether a refreshed short-lived URL still names the same package.
    """

    return {
        "latest_version": release.latest_version,
        "channel": release.channel,
        "platform": release.platform,
        "artifact_size_bytes": release.artifact_size_bytes,
        "artifact_sha256": release.artifact_sha256,
        "manifest_signature": release.manifest_signature,
        "signature_key_id": release.signature_key_id,
        "git_commit": release.git_commit,
        "package_manifest_sha256": release.package_manifest_sha256,
        "published_at": release.published_at,
        "minimum_updater_version": release.minimum_updater_version,
        "rollback_safe": release.rollback_safe,
    }


def _normalized_process_path(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return os.path.normcase(str(Path(raw).resolve(strict=False)))


class UpdateCoordinator:
    """Single-flight manual update coordinator.

    It owns only the update lifecycle.  Business completion remains owned by
    TaskRunner; this coordinator can close the new-work gate and wait for the
    runner's read-only safe-boundary projection, but cannot settle or delete a
    business record.
    """

    def __init__(
        self,
        api: WorkerApiClient,
        runner: TaskRunner,
        *,
        binding_provider: Callable[[], Binding | None],
        on_state: Callable[[dict[str, Any]], None],
        request_normal_exit: Callable[[], None],
        state_store: UpdateStateStore | None = None,
        formal_package: bool | None = None,
        current_program_dir: Path | None = None,
        updater_launcher: Callable[[Path, Path, str], Any] | None = None,
        recovery_updater_launcher: (
            Callable[[Path, Path, str, int], Any] | None
        ) = None,
        process_identity: Callable[[int], dict[str, Any] | None] | None = None,
        updater_terminator: Callable[[int], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        missing_result_grace_seconds: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api = api
        self.runner = runner
        self.binding_provider = binding_provider
        self.on_state = on_state
        self.request_normal_exit = request_normal_exit
        self.store = state_store or UpdateStateStore()
        self.formal_package = (
            is_formal_update_runtime()
            if formal_package is None
            else bool(formal_package)
        )
        self.current_program_dir = (
            current_program_dir.resolve(strict=False)
            if current_program_dir is not None
            else Path(sys.executable).resolve(strict=False).parent
        )
        self.updater_launcher = updater_launcher or self._launch_updater
        self.recovery_updater_launcher = (
            recovery_updater_launcher or self._launch_missing_result_recovery
        )
        self.process_identity = process_identity or self._read_process_identity
        self.updater_terminator = updater_terminator
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.missing_result_grace_seconds = max(
            0.0,
            float(missing_result_grace_seconds),
        )
        self.sleep = sleep
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._operator_pause_after_request = False
        self._fault_after_request = False
        self._install_started = False
        self._post_update_plan: dict[str, Any] | None = None
        self._post_update_token = ""
        try:
            persisted = self.store.load()
            self._operator_pause_after_request = bool(
                persisted.get("operator_pause_after_request")
            )
            self._fault_after_request = bool(persisted.get("fault_after_request"))
        except ClientUpdateError:
            pass

    def set_post_update_context(self, plan: dict[str, Any], token: str) -> None:
        """Keep the one-time token in memory for finite missing-result recovery."""

        self._post_update_plan = dict(plan) if isinstance(plan, dict) else None
        self._post_update_token = str(token or "")

    @staticmethod
    def _read_process_identity(pid: int) -> dict[str, Any] | None:
        """Read the OS identity tuple; a PID by itself is never sufficient."""

        if pid <= 0:
            return None
        try:
            process = psutil.Process(pid)
            return {
                "pid": int(process.pid),
                "create_time": float(process.create_time()),
                "executable_path": _normalized_process_path(process.exe()),
            }
        except psutil.NoSuchProcess:
            return None
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError, TypeError):
            return {"status": "unknown"}

    def _updater_identity_matches(self, state: dict[str, Any]) -> bool:
        return self._updater_identity_status(state) == "matching"

    def _updater_identity_status(self, state: dict[str, Any]) -> str:
        try:
            pid = int(state.get("updater_pid") or 0)
        except (TypeError, ValueError):
            return "unknown"
        if pid <= 0:
            return "unknown"
        actual = self.process_identity(pid)
        if actual is None:
            return "absent"
        if not isinstance(actual, dict) or actual.get("status") == "unknown":
            return "unknown"
        if self._process_identity_matches_state(actual, state):
            return "matching"
        return "replaced"

    @staticmethod
    def _process_identity_matches_state(
        actual: dict[str, Any] | None,
        state: dict[str, Any],
    ) -> bool:
        try:
            pid = int(state.get("updater_pid") or 0)
            expected_create_time = float(
                state.get("updater_create_time_epoch") or 0
            )
        except (TypeError, ValueError):
            return False
        expected_path = _normalized_process_path(
            state.get("updater_executable_path")
        )
        if pid <= 0 or expected_create_time <= 0 or not expected_path:
            return False
        if not isinstance(actual, dict):
            return False
        try:
            actual_pid = int(actual.get("pid") or 0)
            actual_create_time = float(actual.get("create_time") or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            actual_pid == pid
            and abs(actual_create_time - expected_create_time)
            <= UPDATER_CREATE_TIME_TOLERANCE_SECONDS
            and _normalized_process_path(actual.get("executable_path"))
            == expected_path
        )

    def _capture_started_updater_identity(
        self,
        process: Any,
        executable_path: Path,
    ) -> dict[str, Any]:
        try:
            pid = int(getattr(process, "pid", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ClientUpdateError(
                "UPDATE_INSTALL_FAILED",
                "无法确认独立更新器进程身份",
            ) from exc
        identity = self.process_identity(pid)
        # Narrow process-double hook used by unit tests.  ``subprocess.Popen``
        # does not expose this attribute, so production still requires the OS
        # identity tuple from psutil.
        if identity is None:
            identity = getattr(process, "update_process_identity", None)
        if not isinstance(identity, dict):
            raise ClientUpdateError(
                "UPDATE_INSTALL_FAILED",
                "无法读取独立更新器进程身份",
            )
        try:
            actual_pid = int(identity.get("pid") or 0)
            create_time = float(identity.get("create_time") or 0)
        except (TypeError, ValueError) as exc:
            raise ClientUpdateError(
                "UPDATE_INSTALL_FAILED",
                "独立更新器进程身份无效",
            ) from exc
        expected_path = _normalized_process_path(executable_path)
        actual_path = _normalized_process_path(identity.get("executable_path"))
        if (
            actual_pid != pid
            or create_time <= 0
            or not expected_path
            or actual_path != expected_path
        ):
            raise ClientUpdateError(
                "UPDATE_INSTALL_FAILED",
                "独立更新器进程身份与启动文件不一致",
            )
        return {
            "updater_pid": pid,
            "updater_create_time_epoch": create_time,
            "updater_executable_path": expected_path,
        }

    def _terminate_verified_updater(self, state: dict[str, Any]) -> bool:
        """Terminate only the exact updater whose identity was persisted."""

        if not self._updater_identity_matches(state):
            return False
        pid = int(state.get("updater_pid") or 0)
        if self.updater_terminator is not None:
            try:
                terminated = bool(self.updater_terminator(pid))
            except Exception:
                return False
            return terminated and not self._updater_identity_matches(state)
        try:
            process = psutil.Process(pid)
            # Close the PID-reuse race immediately before termination.
            current = {
                "pid": process.pid,
                "create_time": process.create_time(),
                "executable_path": process.exe(),
            }
            if not self._process_identity_matches_state(current, state):
                return False
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        except psutil.NoSuchProcess:
            return True
        except (psutil.Error, OSError, ValueError, TypeError):
            return False
        return not self._updater_identity_matches(state)

    def state(self) -> dict[str, Any]:
        try:
            state = self.store.load()
        except ClientUpdateError as exc:
            return {
                "state": "failed",
                "result_code": exc.code,
                "message": str(exc),
                "status_text": "检查更新失败，请重试",
                "in_progress": False,
                "available": self.formal_package,
            }
        return self._present(state)

    def _present(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "status_text": update_status_text(state),
            "in_progress": state.get("state")
            not in {"idle", "succeeded", "failed", "rolled_back"},
            "available": self.formal_package,
        }

    def _publish(self, state: dict[str, Any]) -> None:
        try:
            self.on_state(self._present(state))
        except Exception as exc:
            _safe_update_log(
                "WARN",
                "client_update_ui_projection_failed",
                "客户端更新状态展示失败；更新安全门禁保持有效。",
                error_code="UPDATE_UI_PROJECTION_FAILED",
                metadata={"error_type": type(exc).__name__},
            )

    def _set_new_work_gate(self, blocked: bool, request_id: str) -> None:
        setter = getattr(self.runner, "set_update_new_work_gate", None)
        if callable(setter):
            setter(blocked, update_request_id=request_id)
            return
        # Narrow test doubles do not own admission. The production TaskRunner
        # always uses the locked branch above.
        set_update_new_work_gate(
            blocked,
            update_request_id=request_id,
        )

    def _cleanup_request_payload(self, request_id: str) -> None:
        """Remove only this request's downloaded/staged program payload.

        Control documents, updater results and failed-program evidence remain
        available for audit.  A malformed persisted request id can never
        expand the cleanup outside ``update_root/requests/<request-id>``.
        """

        clean_id = str(request_id or "").strip()
        if not clean_id:
            return
        requests_root = (self.store.root / "requests").resolve(strict=False)
        request_root = (requests_root / clean_id).resolve(strict=False)
        if request_root.parent != requests_root:
            _safe_update_log(
                "WARN",
                "client_update_payload_cleanup_skipped",
                "更新请求目录不合法，已跳过自动清理。",
                error_code="UPDATE_STATE_INVALID",
                metadata={"update_request_id": clean_id},
            )
            return
        try:
            for name in ("download", "staging"):
                target = request_root / name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
        except OSError as exc:
            _safe_update_log(
                "WARN",
                "client_update_payload_cleanup_failed",
                "更新包清理失败；不影响业务状态和更新结果。",
                error_code="UPDATE_PAYLOAD_CLEANUP_FAILED",
                metadata={
                    "update_request_id": clean_id,
                    "error_type": type(exc).__name__,
                },
            )

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        saved = self.store.save(state)
        self._publish(saved)
        return saved

    def check_for_updates(self) -> bool:
        if not self.formal_package:
            self._save(
                {
                    "state": "failed",
                    "result_code": "UPDATE_FORMAL_PACKAGE_REQUIRED",
                    "message": "检查更新仅支持正式 EXE 交付包",
                    "install_started": False,
                }
            )
            return False
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                self._publish(self.store.load())
                return False
            binding = self.binding_provider()
            pre_status = binding.run_status if binding else "unbound"
            client_instance_id = binding.client_instance_id if binding else self._unbound_instance_id()
            try:
                state = self.store.begin(
                    pre_update_run_status=pre_status,
                    client_instance_id=client_instance_id,
                )
            except ClientUpdateError:
                self._publish(self.store.load())
                return False
            self._stop.clear()
            self._operator_pause_after_request = False
            self._fault_after_request = False
            self._install_started = False
            self._publish(state)
            self._worker = threading.Thread(
                target=self._run,
                args=(state,),
                name="CheJinWorkerClientUpdater",
                daemon=True,
            )
            self._worker.start()
            return True

    def note_operator_pause(self) -> None:
        with self._lock:
            try:
                state = self.store.load()
            except ClientUpdateError:
                return
            if state.get("state") not in {"idle", "failed", "rolled_back", "succeeded"} or state.get("status_restore_pending"):
                self._operator_pause_after_request = True
                self._save({**state, "operator_pause_after_request": True})

    def stop(self) -> None:
        self._stop.set()

    def start_result_reconciliation(self) -> None:
        """Finish state restoration in the newly started or rolled-back app."""

        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            try:
                state = self.store.load()
            except ClientUpdateError:
                return
            if state.get("state") in {
                "checking",
                "downloading",
                "waiting_for_safe_boundary",
            } and not bool(state.get("install_started")):
                self._reconcile_interrupted_preinstall(state)
                return
            if state.get("result_reconciled") is True and not state.get(
                "status_restore_pending"
            ):
                return
            if not state.get("plan_path") or state.get("state") not in {
                "installing",
                "restarting",
                "verifying",
                "succeeded",
                "rolled_back",
            }:
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._reconcile_result,
                args=(state,),
                name="CheJinWorkerUpdateResultReconciler",
                daemon=True,
            )
            self._worker.start()

    def _reconcile_interrupted_preinstall(self, state: dict[str, Any]) -> None:
        """Cancel an interrupted download/wait without touching business data.

        A process can be closed while the package is downloading or while an
        existing Flow drains.  Installation has not started at that point, so
        the only safe recovery is to compare-and-clear this request's intake
        barrier and restore the pre-update run state only when no later pause
        or fault exists.
        """

        request_id = str(state.get("update_request_id") or "")
        self._set_new_work_gate(False, request_id)
        self._cleanup_request_payload(request_id)
        self._operator_pause_after_request = bool(
            state.get("operator_pause_after_request")
        )
        self._fault_after_request = bool(state.get("fault_after_request"))
        self._restore_pre_update_state(state)
        self._save(
            {
                **state,
                "state": "failed",
                "result_code": "UPDATE_PREINSTALL_INTERRUPTED",
                "message": "上次更新在安装前被中断，请重新检查更新",
                "install_started": False,
                "status_restore_pending": False,
                "result_reconciled": False,
            }
        )

    def _reconcile_result(self, state: dict[str, Any]) -> None:
        plan_path = Path(str(state.get("plan_path") or ""))
        result_path = plan_path.parent / "update-result.json"
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                raise ValueError("update plan is not an object")
            if plan.get("update_request_id") != state.get("update_request_id"):
                raise ValueError("update plan request mismatch")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._settle_missing_result_failure(state, str(exc))
            return

        result_timeout_seconds = 0.0
        if not result_path.is_file():
            try:
                result_timeout_seconds = float(plan["result_timeout_seconds"])
                if result_timeout_seconds <= 0:
                    raise ValueError("result timeout must be positive")
            except (KeyError, TypeError, ValueError) as exc:
                self._settle_result_reconciliation_failure(
                    state,
                    result_code="UPDATE_STATE_INVALID",
                    message=str(exc),
                )
                return

        identity_lost_at: float | None = None
        while not self._stop.is_set() and not result_path.is_file():
            identity_status = self._updater_identity_status(state)
            if identity_status == "matching":
                identity_lost_at = None
                try:
                    started_at = float(
                        state.get("updater_create_time_epoch") or 0
                    )
                except (TypeError, ValueError):
                    started_at = 0
                elapsed = max(0.0, self.wall_time() - started_at)
                if elapsed >= result_timeout_seconds:
                    # The result may have appeared after the loop condition;
                    # never terminate an updater that has already committed it.
                    if result_path.is_file():
                        break
                    if not self._terminate_verified_updater(state):
                        self._settle_result_reconciliation_failure(
                            state,
                            result_code="UPDATE_UPDATER_TERMINATION_FAILED",
                            message=(
                                "更新器超过总等待期限，且无法安全终止已验证进程"
                            ),
                            clear_gate=False,
                        )
                        return
                    recovery = self._recover_missing_update_result(
                        state,
                        plan,
                        plan_path,
                        result_path,
                    )
                    if recovery in {"exit_requested", "terminal_failure"}:
                        return
                    if recovery == "result_ready":
                        break
            elif identity_status in {"absent", "replaced"}:
                if identity_lost_at is None:
                    identity_lost_at = self.monotonic()
            else:
                identity_lost_at = None
                try:
                    started_at = float(
                        state.get("updater_create_time_epoch") or 0
                    )
                except (TypeError, ValueError):
                    started_at = 0
                if started_at <= 0:
                    self._settle_result_reconciliation_failure(
                        state,
                        result_code="UPDATE_STATE_INVALID",
                        message="更新状态缺少可验证的更新器进程身份",
                        clear_gate=False,
                    )
                    return
                if max(0.0, self.wall_time() - started_at) >= result_timeout_seconds:
                    self._settle_result_reconciliation_failure(
                        state,
                        result_code="UPDATE_UPDATER_IDENTITY_UNVERIFIABLE",
                        message=(
                            "更新器超过总等待期限，但进程身份无法安全确认；"
                            "禁止启动并发恢复"
                        ),
                        clear_gate=False,
                    )
                    return
            if (
                identity_lost_at is not None
                and self.monotonic() - identity_lost_at
                >= self.missing_result_grace_seconds
            ):
                recovery = self._recover_missing_update_result(
                    state,
                    plan,
                    plan_path,
                    result_path,
                )
                if recovery == "exit_requested":
                    return
                if recovery == "terminal_failure":
                    return
                if recovery == "result_ready":
                    break
            self.sleep(0.2)
        if self._stop.is_set():
            return
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("update result is not an object")
            if result.get("update_request_id") != state.get("update_request_id"):
                raise ValueError("update result request mismatch")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._settle_result_reconciliation_failure(
                state,
                result_code="UPDATE_STATE_INVALID",
                message=str(exc),
            )
            return

        final_state = str(result.get("state") or "failed")
        merged = self._save(
            {
                **state,
                **result,
                "state": final_state,
                "previous_version": plan.get("current_version"),
                "operator_pause_after_request": bool(
                    plan.get("operator_pause_after_request")
                    or self._operator_pause_after_request
                ),
                "fault_after_request": bool(
                    plan.get("fault_after_request") or self._fault_after_request
                ),
                "status_restore_pending": False,
            }
        )
        self._set_new_work_gate(
            False,
            str(state.get("update_request_id") or ""),
        )
        if final_state in {"succeeded", "rolled_back"}:
            self._cleanup_request_payload(
                str(state.get("update_request_id") or "")
            )
        if final_state not in {"succeeded", "rolled_back"}:
            self._save({**merged, "result_reconciled": True})
            return
        if str(plan.get("pre_update_run_status") or "") != "running":
            self._save({**merged, "result_reconciled": True})
            return
        while not self._stop.is_set():
            binding = self.binding_provider()
            if (
                binding is None
                or binding.run_status == "faulted"
                or self._operator_pause_after_request
                or bool(merged.get("operator_pause_after_request"))
                or bool(merged.get("fault_after_request"))
            ):
                self._save({**merged, "result_reconciled": True})
                return
            if self.runner.set_run_status("running"):
                self._save(
                    {
                        **merged,
                        "status_restore_pending": False,
                        "result_reconciled": True,
                    }
                )
                return
            merged = self._save(
                {
                    **merged,
                    "status_restore_pending": True,
                    "result_reconciled": False,
                }
            )
            self.sleep(5.0)

    def _recover_missing_update_result(
        self,
        state: dict[str, Any],
        plan: dict[str, Any],
        plan_path: Path,
        result_path: Path,
    ) -> str:
        """Settle a dead updater once, never wait forever or guess business data."""

        request_id = str(state.get("update_request_id") or "")
        context = self._post_update_plan or {}
        token = self._post_update_token
        context_keys = (
            "update_request_id",
            "target_version",
            "current_program_dir",
            "previous_program_dir",
            "failed_program_dir",
            "healthy_marker_path",
            "one_time_token_sha256",
        )
        context_matches = bool(
            token
            and all(context.get(key) == plan.get(key) for key in context_keys)
            and str(plan.get("update_request_id") or "") == request_id
        )
        try:
            running_from_current = self.current_program_dir == Path(
                str(plan.get("current_program_dir") or "")
            ).resolve(strict=True)
        except (OSError, RuntimeError):
            running_from_current = False
        if (
            context_matches
            and running_from_current
            and authenticated_healthy_marker(plan, token) is not None
        ):
            _atomic_json_write(
                result_path,
                {
                    "schema_version": 1,
                    "state": "succeeded",
                    "result_code": "UPDATE_SUCCEEDED",
                    "update_request_id": request_id,
                    "target_version": plan.get("target_version"),
                    "artifact_sha256": (plan.get("release") or {}).get(
                        "artifact_sha256"
                    )
                    if isinstance(plan.get("release"), dict)
                    else None,
                    "recovered_from_missing_updater_result": True,
                },
            )
            return "result_ready"

        previous = Path(str(plan.get("previous_program_dir") or ""))
        updater = plan_path.parent / "CheJinUpdater.exe"
        if context_matches and previous.is_dir() and updater.is_file():
            if state.get("result_recovery_started") is True:
                self._settle_result_reconciliation_failure(
                    state,
                    result_code="UPDATE_RESULT_RECOVERY_INCOMPLETE",
                    message="更新结果恢复已经执行过，禁止启动第二个恢复更新器",
                )
                return "terminal_failure"
            ready_path = plan_path.parent / "missing-result-recovery-ready.json"
            ready_path.unlink(missing_ok=True)
            recovery_state = state
            try:
                recovery_state = self._save(
                    {
                        **state,
                        "result_recovery_started": True,
                        "result_recovery_started_at": self.wall_time(),
                    }
                )
                process = self.recovery_updater_launcher(
                    updater,
                    plan_path,
                    token,
                    os.getpid(),
                )
                deadline = self.monotonic() + 5.0
                while self.monotonic() < deadline:
                    if ready_path.is_file():
                        ready = json.loads(ready_path.read_text(encoding="utf-8"))
                        if (
                            isinstance(ready, dict)
                            and ready.get("ready") is True
                            and str(ready.get("update_request_id") or "")
                            == request_id
                        ):
                            self._save(
                                {
                                    **recovery_state,
                                    "state": "restarting",
                                    "result_code": "UPDATE_RESULT_RECOVERY_STARTED",
                                    "recovery_updater_pid": getattr(
                                        process, "pid", None
                                    ),
                                }
                            )
                            self.request_normal_exit()
                            return "exit_requested"
                        raise ValueError("missing-result recovery request mismatch")
                    poll = getattr(process, "poll", None)
                    if callable(poll) and poll() is not None:
                        raise RuntimeError("missing-result recovery updater exited")
                    self.sleep(0.1)
            except Exception as exc:
                self._settle_missing_result_failure(recovery_state, str(exc))
                return "terminal_failure"

        self._settle_missing_result_failure(
            state,
            "更新器结果缺失，且无法验证当前版本或自动回滚",
        )
        return "terminal_failure"

    def _settle_missing_result_failure(
        self,
        state: dict[str, Any],
        message: str,
    ) -> None:
        self._settle_result_reconciliation_failure(
            state,
            result_code="UPDATE_RESULT_MISSING",
            message=message,
        )

    def _settle_result_reconciliation_failure(
        self,
        state: dict[str, Any],
        *,
        result_code: str,
        message: str,
        clear_gate: bool = True,
    ) -> None:
        """Finish one failed reconciliation without restoring run status.

        Corrupt or mismatched result documents are terminal for this request.
        The client remains paused/faulted, but the request-scoped intake gate
        is cleared so an unreadable control file cannot permanently masquerade
        as an update still in progress.
        """

        request_id = str(state.get("update_request_id") or "")
        if clear_gate:
            self._set_new_work_gate(False, request_id)
        self._save(
            {
                **state,
                "state": "failed",
                "result_code": result_code,
                "message": message,
                "install_started": True,
                "status_restore_pending": False,
                "result_reconciled": True,
                "intake_gate_cleared": clear_gate,
            }
        )

    def _unbound_instance_id(self) -> str:
        path = self.store.root / "unbound-client-instance-id.txt"
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            return value
        value = f"unbound-{secrets.token_hex(16)}"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
        return value

    def _run(self, initial: dict[str, Any]) -> None:
        request_id = str(initial.get("update_request_id") or "")
        gate_set = False
        try:
            release = self.api.latest_client_release(
                current_version=__version__,
                client_instance_id=str(initial.get("client_instance_id") or ""),
            )
            if not release.update_available:
                self._save(
                    {
                        **initial,
                        "state": "idle",
                        "result_code": "UPDATE_ALREADY_LATEST",
                        "latest_version": release.latest_version,
                        "install_started": False,
                    }
                )
                return

            self._set_new_work_gate(True, request_id)
            gate_set = True
            binding = self.binding_provider()
            if binding and binding.run_status == "running":
                self.runner.set_run_status("paused")

            downloading = self._save(
                {
                    **initial,
                    "state": "downloading",
                    "target_version": release.latest_version,
                    "artifact_sha256": release.artifact_sha256,
                    "release": _persisted_release_identity(release),
                    "install_started": False,
                }
            )
            request_root = self.store.root / "requests" / request_id
            try:
                prepared = prepare_release_package(
                    release,
                    request_root=request_root,
                )
            except ClientUpdateError as exc:
                if exc.code != "UPDATE_DOWNLOAD_URL_EXPIRED":
                    raise
                refreshed = self.api.latest_client_release(
                    current_version=__version__,
                    client_instance_id=str(initial.get("client_instance_id") or ""),
                )
                if (
                    not refreshed.update_available
                    or _download_release_identity(refreshed)
                    != _download_release_identity(release)
                ):
                    raise ClientUpdateError(
                        "UPDATE_RELEASE_IDENTITY_CHANGED",
                        "临时下载地址续签时发布包身份发生变化，请重新检查更新",
                    )
                release = refreshed
                prepared = prepare_release_package(
                    release,
                    request_root=request_root,
                )
            waiting = self._save(
                {
                    **downloading,
                    **prepared,
                    "state": "waiting_for_safe_boundary",
                }
            )
            while not self._stop.is_set():
                binding = self.binding_provider()
                if binding and binding.run_status == "faulted":
                    self._fault_after_request = True
                boundary = self.runner.update_install_safety_snapshot()
                if boundary.get("safe") is True:
                    self._start_install(waiting, release, request_root, boundary)
                    return
                reason_code = str(boundary.get("waiting_reason_code") or "")
                reason_text = str(boundary.get("waiting_reason_text") or "")
                if (
                    waiting.get("waiting_reason_code") != reason_code
                    or waiting.get("waiting_reason_text") != reason_text
                ):
                    waiting = self._save(
                        {
                            **waiting,
                            "waiting_reason_code": reason_code,
                            "waiting_reason_text": reason_text,
                        }
                    )
                self.sleep(0.25)
            raise ClientUpdateError("UPDATE_CANCELLED", "客户端退出前未开始安装")
        except Exception as exc:
            if self._install_started:
                return
            code = exc.code if isinstance(exc, ClientUpdateError) else "UPDATE_CHECK_FAILED"
            _safe_update_log(
                "ERROR",
                "client_update_prepare_failed",
                str(exc),
                error_code=code,
                metadata={"update_request_id": request_id},
            )
            if gate_set:
                self._set_new_work_gate(False, request_id)
                self._restore_pre_update_state(initial)
            self._cleanup_request_payload(request_id)
            self._save(
                {
                    **initial,
                    "state": "failed",
                    "result_code": code,
                    "message": str(exc),
                    "install_started": False,
                    "target_version": (
                        self.store.load().get("target_version")
                        if self.store.state_path.exists()
                        else None
                    ),
                }
            )

    def _restore_pre_update_state(self, initial: dict[str, Any]) -> None:
        binding = self.binding_provider()
        if (
            str(initial.get("pre_update_run_status") or "") == "running"
            and binding is not None
            and binding.run_status != "faulted"
            and not self._operator_pause_after_request
            and not self._fault_after_request
        ):
            self.runner.set_run_status("running")

    def _start_install(
        self,
        state: dict[str, Any],
        release: ClientRelease,
        request_root: Path,
        boundary: dict[str, Any],
    ) -> None:
        updater_source = self.current_program_dir / "CheJinUpdater.exe"
        if not updater_source.is_file():
            raise ClientUpdateError("UPDATE_INSTALL_FAILED", "正式包缺少独立更新器")
        control_root = request_root / "control"
        control_root.mkdir(parents=True, exist_ok=True)
        updater_copy = control_root / "CheJinUpdater.exe"
        shutil.copy2(updater_source, updater_copy)
        token = secrets.token_urlsafe(32)
        request_id = str(state.get("update_request_id") or "")
        plan_path = control_root / "update-plan.json"
        current = self.current_program_dir
        release_identity = _persisted_release_identity(release)
        release_identity["release_notes"] = ""
        plan = {
            "schema_version": 1,
            "update_request_id": request_id,
            "current_version": __version__,
            "target_version": release.latest_version,
            "current_program_dir": str(current),
            "staged_program_dir": str(Path(str(state["package_root"]))),
            "previous_program_dir": str(current.with_name(current.name + ".previous")),
            "failed_program_dir": str(request_root / "failed-program"),
            "data_dir": str(CONFIG.app_dir.resolve(strict=False)),
            "archive_path": str(Path(str(state["archive_path"])).resolve(strict=True)),
            "healthy_marker_path": str(control_root / "healthy.json"),
            "updater_ready_path": str(control_root / "updater-ready.json"),
            "worker_executable_relative": "CheJinWorkerClient.exe",
            "old_pid": os.getpid(),
            "old_exit_timeout_seconds": 30,
            "health_timeout_seconds": 120,
            "result_timeout_seconds": 180,
            "one_time_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            # The updater needs immutable signed identity, not the temporary
            # object-storage URL or user-facing notes.  Keeping those out of
            # the plan makes the handoff document non-credential-bearing.
            "release": release_identity,
            "protected_data_snapshot": protected_update_snapshot(),
            "safe_boundary": boundary,
            "pre_update_run_status": state.get("pre_update_run_status"),
            "operator_pause_after_request": self._operator_pause_after_request,
            "fault_after_request": self._fault_after_request,
        }
        _atomic_json_write(plan_path, plan)
        installing = self._save(
            {
                **state,
                "state": "installing",
                "install_started": False,
                "plan_path": str(plan_path),
            }
        )
        updater_process = self.updater_launcher(updater_copy, plan_path, token)
        ready_path = control_root / "updater-ready.json"
        # The signed PyInstaller updater can legitimately spend time in
        # first-launch extraction and Windows malware scanning.  Keep the old
        # Worker alive and the program directory untouched until this bounded
        # ready gate succeeds; do not impose a hidden shorter startup window.
        deadline = time.monotonic() + UPDATER_READY_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                if ready_path.is_file():
                    try:
                        ready = json.loads(ready_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ClientUpdateError(
                            "UPDATE_INSTALL_FAILED",
                            "独立更新器启动确认无效",
                        ) from exc
                    if (
                        not isinstance(ready, dict)
                        or ready.get("ready") is not True
                        or str(ready.get("update_request_id") or "") != request_id
                    ):
                        raise ClientUpdateError(
                            "UPDATE_INSTALL_FAILED",
                            "独立更新器启动确认与本次请求不一致",
                        )
                    break
                poll = getattr(updater_process, "poll", None)
                if callable(poll) and poll() is not None:
                    raise ClientUpdateError(
                        "UPDATE_INSTALL_FAILED",
                        "独立更新器未通过启动校验",
                    )
                self.sleep(0.1)
            else:
                raise ClientUpdateError("UPDATE_INSTALL_FAILED", "独立更新器启动确认超时")

            updater_identity = self._capture_started_updater_identity(
                updater_process,
                updater_copy,
            )
            installing = self._save(
                {
                    **installing,
                    "install_started": True,
                    **updater_identity,
                }
            )
            _safe_update_log(
                "INFO",
                "client_update_installer_started",
                "独立更新器已启动，旧客户端将正常退出。",
                metadata={
                    "update_request_id": request_id,
                    "target_version": release.latest_version,
                    "state": installing.get("state"),
                },
            )
            self.request_normal_exit()
            self._install_started = True
        except Exception:
            if not self._install_started:
                self._terminate_preinstall_updater(updater_process)
            raise

    @staticmethod
    def _terminate_preinstall_updater(process: Any) -> None:
        """Stop only the not-yet-authorized updater, never the Worker itself."""

        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            try:
                terminate()
            except Exception:
                return

    @staticmethod
    def _launch_updater(updater: Path, plan_path: Path, token: str) -> subprocess.Popen:
        updater_env = os.environ.copy()
        updater_env["CHEJIN_UPDATER_DIAGNOSTIC_PATH"] = str(
            plan_path.parent / "updater-startup.jsonl"
        )
        return subprocess.Popen(
            [str(updater), "--plan", str(plan_path), "--token", token],
            cwd=str(updater.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=updater_env,
            creationflags=_creation_flags(),
        )

    @staticmethod
    def _launch_missing_result_recovery(
        updater: Path,
        plan_path: Path,
        token: str,
        current_pid: int,
    ) -> subprocess.Popen:
        updater_env = os.environ.copy()
        updater_env["CHEJIN_UPDATER_DIAGNOSTIC_PATH"] = str(
            plan_path.parent / "updater-recovery-startup.jsonl"
        )
        return subprocess.Popen(
            [
                str(updater),
                "--recover-missing-result",
                "--plan",
                str(plan_path),
                "--token",
                token,
                "--current-pid",
                str(current_pid),
            ],
            cwd=str(updater.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=updater_env,
            creationflags=_creation_flags(),
        )
