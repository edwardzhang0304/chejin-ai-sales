from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any


PLAN_SCHEMA_VERSION = 1


def _startup_diagnostic(phase: str, **details: Any) -> None:
    """Append bounded startup evidence without recording arguments or secrets."""

    raw_path = str(os.environ.get("CHEJIN_UPDATER_DIAGNOSTIC_PATH") or "").strip()
    if not raw_path:
        return
    payload: dict[str, Any] = {
        "timestamp_epoch": time.time(),
        "phase": str(phase),
        "pid": os.getpid(),
    }
    for key in ("error_type", "error_code", "reason", "exit_code_hex", "marker_error"):
        value = str(details.get(key) or "").strip()
        if value:
            payload[key] = value[:120]
    for key in ("child_pid", "exit_code", "elapsed_ms", "health_timeout_seconds"):
        value = details.get(key)
        if isinstance(value, (int, float)):
            payload[key] = value
    try:
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
    except Exception:
        # Observability cannot affect directory replacement or rollback.
        return


_startup_diagnostic("stdlib_imports_succeeded")

import psutil

_startup_diagnostic("psutil_import_succeeded")

from .models import ClientRelease
from .release_package_contract import (
    ClientUpdateError,
    PACKAGE_MANIFEST_NAME,
    hash_file,
    load_trusted_release_keys,
    validate_release_contract,
    verify_staged_package,
    verify_release_signature,
)

_startup_diagnostic("release_contract_import_succeeded")

from .update_runtime_health_contract import validate_authenticated_runtime_marker
from .update_diagnostics import update_error_code

_startup_diagnostic("health_contract_import_succeeded")


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划不可读") from exc
    if not isinstance(payload, dict):
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划格式不合法")
    return payload


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _release_from_plan(plan: dict[str, Any]) -> ClientRelease:
    payload = plan.get("release")
    if not isinstance(payload, dict):
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划缺少发布清单")
    return ClientRelease.from_api({**payload, "update_available": True})


def _safe_absolute_path(value: Any, *, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", f"更新计划缺少{label}")
    path = Path(raw)
    if not path.is_absolute():
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", f"{label}必须是绝对路径")
    return path.resolve(strict=False)


def validate_update_plan(plan_path: Path, token: str) -> dict[str, Any]:
    plan = _load_json(plan_path)
    if int(plan.get("schema_version") or 0) != PLAN_SCHEMA_VERSION:
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划版本不兼容")
    if not hmac.compare_digest(str(plan.get("one_time_token_sha256") or ""), _token_digest(token)):
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划一次性令牌不匹配")
    try:
        health_timeout_seconds = float(plan["health_timeout_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ClientUpdateError(
            "UPDATE_INSTALL_FAILED",
            "更新计划缺少有效健康检查期限",
        ) from exc
    if health_timeout_seconds <= 0:
        raise ClientUpdateError(
            "UPDATE_INSTALL_FAILED",
            "更新计划健康检查期限无效",
        )
    try:
        old_exit_timeout_seconds = float(plan["old_exit_timeout_seconds"])
        result_timeout_seconds = float(plan["result_timeout_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ClientUpdateError(
            "UPDATE_INSTALL_FAILED",
            "更新计划缺少有效结果对账期限",
        ) from exc
    if (
        old_exit_timeout_seconds <= 0
        or result_timeout_seconds
        < old_exit_timeout_seconds + health_timeout_seconds
    ):
        raise ClientUpdateError(
            "UPDATE_INSTALL_FAILED",
            "更新计划结果对账期限不足",
        )
    current = _safe_absolute_path(plan.get("current_program_dir"), label="当前程序目录")
    staged = _safe_absolute_path(plan.get("staged_program_dir"), label="暂存程序目录")
    previous = _safe_absolute_path(plan.get("previous_program_dir"), label="上一版本目录")
    failed = _safe_absolute_path(plan.get("failed_program_dir"), label="失败证据目录")
    data_dir = _safe_absolute_path(plan.get("data_dir"), label="数据目录")
    archive = _safe_absolute_path(plan.get("archive_path"), label="更新包")
    control_root = plan_path.parent.resolve(strict=False)
    isolated_paths = (current, staged, previous, failed, data_dir, control_root)
    if len(set(isolated_paths)) != len(isolated_paths):
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "程序、数据和更新目录必须相互分离")
    for index, left in enumerate(isolated_paths):
        for right in isolated_paths[index + 1 :]:
            if left in right.parents or right in left.parents:
                raise ClientUpdateError(
                    "UPDATE_INSTALL_FAILED",
                    "程序、数据和更新目录不得相互包含",
                )
    for replaceable in (current, staged, previous, failed):
        if (
            archive == replaceable
            or archive in replaceable.parents
            or replaceable in archive.parents
        ):
            raise ClientUpdateError(
                "UPDATE_INSTALL_FAILED",
                "更新包不得位于可替换程序目录内",
            )
    if not staged.is_dir() or not current.is_dir() or not archive.is_file():
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划指向的程序或更新包不存在")
    safe_boundary = plan.get("safe_boundary")
    if not isinstance(safe_boundary, dict) or safe_boundary.get("safe") is not True:
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划缺少安装安全边界证明")
    if safe_boundary.get("new_work_blocked") is not True:
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划未证明已禁止领取新任务")
    if safe_boundary.get("backend_stopped_confirmed_or_unbound") is not True:
        raise ClientUpdateError(
            "UPDATE_INSTALL_FAILED",
            "更新计划未证明后端已确认客户端停止接单或客户端未绑定",
        )
    if str(safe_boundary.get("confirmed_run_status") or "") not in {
        "paused",
        "faulted",
        "unbound",
    }:
        raise ClientUpdateError(
            "UPDATE_INSTALL_FAILED",
            "更新计划的后端接单状态证明无效",
        )
    forbidden_truthy = (
        "current_task",
        "inflight_flow_id",
        "task_lease_active",
        "ui_lock_active",
        "sidecar_active",
    )
    if any(bool(safe_boundary.get(key)) for key in forbidden_truthy):
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划仍包含运行中的业务动作")
    blocker_counts = (
        "waiting_ledger",
        "pending_c2_outbox",
        "pending_sqlite_action_journal",
        "pending_file_action_journal",
        "pending_sent_ack",
        "action_journal_state_unavailable",
    )
    try:
        has_durable_blocker = any(
            int(safe_boundary.get(key) or 0) != 0 for key in blocker_counts
        )
    except (TypeError, ValueError) as exc:
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划业务阻断计数无效") from exc
    if has_durable_blocker:
        raise ClientUpdateError("UPDATE_INSTALL_FAILED", "更新计划仍包含未结算业务记录")
    release = _release_from_plan(plan)
    # The archive is already local.  A presigned download URL is deliberately
    # excluded from update-plan.json so the independent updater validates only
    # immutable package identity and never receives reusable download access.
    validate_release_contract(
        release,
        current_version=str(plan.get("current_version") or ""),
        require_download_url=False,
    )
    verify_release_signature(release, trusted_keys=load_trusted_release_keys())
    if hash_file(archive) != str(release.artifact_sha256 or "").lower():
        raise ClientUpdateError("UPDATE_PACKAGE_HASH_MISMATCH", "Updater重新校验更新包失败")
    verify_staged_package(release, staged)
    return {
        **plan,
        "health_timeout_seconds": health_timeout_seconds,
        "old_exit_timeout_seconds": old_exit_timeout_seconds,
        "result_timeout_seconds": result_timeout_seconds,
        "_paths": {
            "current": current,
            "staged": staged,
            "previous": previous,
            "failed": failed,
            "data": data_dir,
            "archive": archive,
            "control": control_root,
        },
    }


def validate_missing_result_recovery_plan(
    plan_path: Path,
    token: str,
) -> dict[str, Any]:
    """Validate only the immutable paths needed to restore ``previous``.

    The staged directory and archive may already have been consumed by the
    original updater, so missing-result recovery must not require them.
    """

    plan = _load_json(plan_path)
    if int(plan.get("schema_version") or 0) != PLAN_SCHEMA_VERSION:
        raise ClientUpdateError("UPDATE_ROLLBACK_FAILED", "更新计划版本不兼容")
    if not hmac.compare_digest(
        str(plan.get("one_time_token_sha256") or ""),
        _token_digest(token),
    ):
        raise ClientUpdateError("UPDATE_ROLLBACK_FAILED", "恢复令牌不匹配")
    current = _safe_absolute_path(plan.get("current_program_dir"), label="当前程序目录")
    previous = _safe_absolute_path(plan.get("previous_program_dir"), label="上一版本目录")
    failed = _safe_absolute_path(plan.get("failed_program_dir"), label="失败证据目录")
    data = _safe_absolute_path(plan.get("data_dir"), label="数据目录")
    control = plan_path.parent.resolve(strict=False)
    isolated = (current, previous, failed, data, control)
    if len(set(isolated)) != len(isolated):
        raise ClientUpdateError("UPDATE_ROLLBACK_FAILED", "恢复目录必须相互分离")
    for index, left in enumerate(isolated):
        for right in isolated[index + 1 :]:
            if left in right.parents or right in left.parents:
                raise ClientUpdateError("UPDATE_ROLLBACK_FAILED", "恢复目录不得相互包含")
    if not current.is_dir() or not previous.is_dir():
        raise ClientUpdateError("UPDATE_ROLLBACK_FAILED", "当前版本或上一版本目录缺失")
    return {
        **plan,
        "_paths": {
            "current": current,
            "previous": previous,
            "failed": failed,
            "control": control,
        },
    }


def wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    if pid <= 0:
        return True
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            process = psutil.Process(pid)
            if process.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.Error:
            return True
        time.sleep(0.1)
    return not psutil.pid_exists(pid)


def _process_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _start_worker(executable: Path, *, plan_path: Path, token: str, rollback: bool = False) -> subprocess.Popen:
    arguments = [
        str(executable),
        "--post-rollback-plan" if rollback else "--post-update-plan",
        str(plan_path),
        "--post-update-token",
        token,
    ]
    return subprocess.Popen(
        arguments,
        cwd=str(executable.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_process_creation_flags(),
    )


def _wait_for_health(
    marker_path: Path,
    process: subprocess.Popen,
    request_id: str,
    target_version: str,
    token: str,
    timeout_seconds: float,
    *,
    diagnostic: dict[str, Any] | None = None,
) -> bool:
    started = time.monotonic()
    deadline = started + max(1.0, float(timeout_seconds))
    evidence = diagnostic if diagnostic is not None else {}
    evidence.update(pid=process.pid, health_timeout_seconds=timeout_seconds)

    def finish(reason: str, *, exit_code: int | None = None) -> bool:
        evidence.update(
            reason=reason,
            exit_code=exit_code,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        if exit_code is not None:
            evidence["exit_code_hex"] = f"0x{exit_code & 0xffffffff:08X}"
        return reason == "healthy"

    expected_token = _token_digest(token)
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            return finish("process_exited", exit_code=exit_code)
        try:
            marker = _load_json(marker_path)
        except ClientUpdateError:
            evidence["marker_error"] = "UPDATE_HEALTH_MARKER_UNREADABLE"
            time.sleep(0.2)
            continue
        try:
            validate_authenticated_runtime_marker(
                marker,
                request_id=request_id,
                target_version=target_version,
                token_sha256=expected_token,
            )
            evidence.pop("marker_error", None)
            return finish("healthy")
        except RuntimeError as exc:
            evidence["marker_error"] = update_error_code(exc)
        time.sleep(0.2)
    exit_code = process.poll()
    if exit_code is not None:
        return finish("process_exited", exit_code=exit_code)
    return finish("health_timeout")


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def run_update(plan_path: Path, token: str) -> int:
    result_path = plan_path.parent / "update-result.json"
    retired_previous: Path | None = None
    new_process: subprocess.Popen | None = None
    plan: dict[str, Any] = {}
    old_exit_confirmed = False
    startup_diagnostic: dict[str, Any] = {}
    try:
        _startup_diagnostic("plan_validation_started")
        plan = validate_update_plan(plan_path, token)
        _startup_diagnostic("plan_validation_succeeded")
        paths = plan["_paths"]
        current: Path = paths["current"]
        staged: Path = paths["staged"]
        previous: Path = paths["previous"]
        failed: Path = paths["failed"]
        old_pid = int(plan.get("old_pid") or 0)
        ready_path = _safe_absolute_path(
            plan.get("updater_ready_path"), label="Updater就绪标记"
        )
        _atomic_json_write(
            ready_path,
            {
                "schema_version": 1,
                "ready": True,
                "update_request_id": plan.get("update_request_id"),
                "pid": os.getpid(),
            },
        )
        _startup_diagnostic("ready_marker_written")
        if not wait_for_pid_exit(old_pid, float(plan.get("old_exit_timeout_seconds") or 30)):
            raise ClientUpdateError("UPDATE_INSTALL_FAILED", "旧客户端未能正常退出")
        old_exit_confirmed = True

        if previous.exists():
            retired_previous = previous.with_name(previous.name + ".retired-" + str(plan.get("update_request_id") or "unknown"))
            if retired_previous.exists():
                shutil.rmtree(retired_previous)
            os.replace(previous, retired_previous)
        os.replace(current, previous)
        try:
            os.replace(staged, current)
        except Exception:
            os.replace(previous, current)
            raise

        marker_path = _safe_absolute_path(plan.get("healthy_marker_path"), label="健康标记")
        marker_path.unlink(missing_ok=True)
        worker_executable = current / str(plan.get("worker_executable_relative") or "CheJinWorkerClient.exe")
        if not worker_executable.is_file():
            raise ClientUpdateError("UPDATE_RESTART_FAILED", "新客户端可执行文件不存在")
        startup_diagnostic["phase"] = "start_new_worker"
        new_process = _start_worker(worker_executable, plan_path=plan_path, token=token)
        startup_diagnostic["phase"] = "wait_for_health"
        healthy = _wait_for_health(
            marker_path,
            new_process,
            str(plan.get("update_request_id") or ""),
            str(plan.get("target_version") or ""),
            token,
            float(plan["health_timeout_seconds"]),
            diagnostic=startup_diagnostic,
        )
        # Persist before any rollback work: if rollback itself is interrupted,
        # the original child failure must still be diagnosable.
        _startup_diagnostic(
            "new_worker_health_checked",
            child_pid=startup_diagnostic.get("pid"),
            **{key: value for key, value in startup_diagnostic.items() if key not in {"phase", "pid"}},
        )
        if not healthy:
            raise ClientUpdateError("UPDATE_RESTART_FAILED", "新客户端未在健康检查窗口内启动")
        if retired_previous and retired_previous.exists():
            shutil.rmtree(retired_previous)
        _atomic_json_write(
            result_path,
            {
                "schema_version": 1,
                "state": "succeeded",
                "result_code": "UPDATE_SUCCEEDED",
                "update_request_id": plan.get("update_request_id"),
                "target_version": plan.get("target_version"),
                "artifact_sha256": plan.get("release", {}).get("artifact_sha256"),
            },
        )
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, ClientUpdateError) else "UPDATE_INSTALL_FAILED"
        if startup_diagnostic:
            startup_diagnostic.update(
                exception_type=type(exc).__name__,
                errno=getattr(exc, "errno", None),
                winerror=getattr(exc, "winerror", None),
            )
        _startup_diagnostic(
            "update_failed",
            error_type=type(exc).__name__,
            error_code=code,
        )
        message = str(exc)
        try:
            paths = plan.get("_paths") if isinstance(plan.get("_paths"), dict) else {}
            current = paths.get("current")
            previous = paths.get("previous")
            failed = paths.get("failed")
            if new_process is not None:
                _terminate_process(new_process)
            if isinstance(current, Path) and isinstance(previous, Path) and previous.exists():
                if current.exists():
                    if isinstance(failed, Path):
                        if failed.exists():
                            shutil.rmtree(failed)
                        os.replace(current, failed)
                    else:
                        raise ClientUpdateError("UPDATE_ROLLBACK_FAILED", "缺少失败证据目录")
                os.replace(previous, current)
                old_executable = current / str(plan.get("worker_executable_relative") or "CheJinWorkerClient.exe")
                rollback_process = _start_worker(old_executable, plan_path=plan_path, token=token, rollback=True)
                time.sleep(0.5)
                if rollback_process.poll() is not None:
                    raise ClientUpdateError("UPDATE_ROLLBACK_FAILED", "旧客户端回滚后无法启动")
                state = "rolled_back"
                result_code = "UPDATE_ROLLED_BACK"
                if retired_previous and retired_previous.exists():
                    shutil.rmtree(retired_previous)
            elif old_exit_confirmed and isinstance(current, Path) and current.exists():
                old_executable = current / str(
                    plan.get("worker_executable_relative")
                    or "CheJinWorkerClient.exe"
                )
                rollback_process = _start_worker(
                    old_executable,
                    plan_path=plan_path,
                    token=token,
                    rollback=True,
                )
                time.sleep(0.5)
                if rollback_process.poll() is not None:
                    raise ClientUpdateError(
                        "UPDATE_ROLLBACK_FAILED",
                        "目录切换失败后旧客户端无法重新启动",
                    )
                state = "rolled_back"
                result_code = "UPDATE_ROLLED_BACK"
                if retired_previous and retired_previous.exists():
                    shutil.rmtree(retired_previous)
            else:
                state = "failed"
                result_code = code
        except Exception as rollback_exc:
            state = "rollback_failed"
            result_code = "UPDATE_ROLLBACK_FAILED"
            message = f"{message}; rollback={type(rollback_exc).__name__}: {rollback_exc}"
        _atomic_json_write(
            result_path,
            {
                "schema_version": 1,
                "state": state,
                "result_code": result_code,
                "failure_code": code,
                "startup_diagnostic": startup_diagnostic,
                "message": message,
                "update_request_id": plan.get("update_request_id"),
                "target_version": plan.get("target_version"),
                "artifact_sha256": (plan.get("release") or {}).get("artifact_sha256") if isinstance(plan.get("release"), dict) else None,
            },
        )
        return 1


def run_missing_result_recovery(
    plan_path: Path,
    token: str,
    current_pid: int,
) -> int:
    """Rollback once when the original updater vanished without a result."""

    result_path = plan_path.parent / "update-result.json"
    if result_path.is_file():
        return 0
    plan: dict[str, Any] = {}
    try:
        plan = validate_missing_result_recovery_plan(plan_path, token)
        request_id = str(plan.get("update_request_id") or "")
        ready_path = plan_path.parent / "missing-result-recovery-ready.json"
        _atomic_json_write(
            ready_path,
            {
                "schema_version": 1,
                "ready": True,
                "update_request_id": request_id,
                "pid": os.getpid(),
            },
        )
        if not wait_for_pid_exit(
            current_pid,
            float(plan.get("old_exit_timeout_seconds") or 30),
        ):
            raise ClientUpdateError(
                "UPDATE_ROLLBACK_FAILED",
                "新客户端未能正常退出，无法恢复上一版本",
            )
        paths = plan["_paths"]
        current: Path = paths["current"]
        previous: Path = paths["previous"]
        failed: Path = paths["failed"]
        evidence_target = failed
        if evidence_target.exists():
            evidence_target = failed.with_name(
                failed.name + "-missing-result-" + (request_id or "unknown")
            )
        if evidence_target.exists():
            raise ClientUpdateError(
                "UPDATE_ROLLBACK_FAILED",
                "失败版本证据目录已存在",
            )
        os.replace(current, evidence_target)
        os.replace(previous, current)
        old_executable = current / str(
            plan.get("worker_executable_relative") or "CheJinWorkerClient.exe"
        )
        rollback_process = _start_worker(
            old_executable,
            plan_path=plan_path,
            token=token,
            rollback=True,
        )
        time.sleep(0.5)
        if rollback_process.poll() is not None:
            raise ClientUpdateError(
                "UPDATE_ROLLBACK_FAILED",
                "上一版本恢复后无法启动",
            )
        _atomic_json_write(
            result_path,
            {
                "schema_version": 1,
                "state": "rolled_back",
                "result_code": "UPDATE_ROLLED_BACK",
                "failure_code": "UPDATE_RESULT_MISSING",
                "message": "原更新器未写入结果，已自动恢复上一版本",
                "update_request_id": request_id,
                "target_version": plan.get("target_version"),
                "artifact_sha256": (plan.get("release") or {}).get(
                    "artifact_sha256"
                )
                if isinstance(plan.get("release"), dict)
                else None,
            },
        )
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, ClientUpdateError) else "UPDATE_ROLLBACK_FAILED"
        _atomic_json_write(
            result_path,
            {
                "schema_version": 1,
                "state": "rollback_failed",
                "result_code": "UPDATE_ROLLBACK_FAILED",
                "failure_code": code,
                "message": str(exc),
                "update_request_id": plan.get("update_request_id"),
                "target_version": plan.get("target_version"),
            },
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    _startup_diagnostic("main_entered")
    parser = argparse.ArgumentParser(prog="CheJinUpdater")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--recover-missing-result", action="store_true")
    parser.add_argument("--current-pid", type=int, default=0)
    args = parser.parse_args(argv)
    if args.recover_missing_result:
        return run_missing_result_recovery(
            args.plan.resolve(),
            args.token,
            args.current_pid,
        )
    return run_update(args.plan.resolve(), args.token)


if __name__ == "__main__":
    raise SystemExit(main())
