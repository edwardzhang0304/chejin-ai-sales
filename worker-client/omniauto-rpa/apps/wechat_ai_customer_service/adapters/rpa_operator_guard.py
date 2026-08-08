"""Generic RPA operator guard launcher.

This module owns the shared keyboard/mouse hook and floating indicator used by
WeChat UI operations such as add_friend, C2 message reads, and send actions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root


PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = PROJECT_ROOT / "apps" / "wechat_ai_customer_service"
GUARD_SCRIPT = APP_ROOT / "scripts" / "run_rpa_operator_guard.py"
CONTROL_MODES = {"idle", "ready", "active", "paused", "stopped", "fault"}
CONTROL_HOTKEYS = {"f8", "esc"}

_ACTIVE_GUARD: dict[str, Any] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def first_env_bool(names: tuple[str, ...], *, default: bool) -> bool:
    for name in names:
        if os.getenv(name) is not None:
            return env_bool(name, default=default)
    return default


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def rpa_operator_guard_settings() -> dict[str, Any]:
    hotkey = str(
        os.getenv("WECHAT_RPA_OPERATOR_GUARD_CONTROL_HOTKEY")
        or os.getenv("WECHAT_ADD_FRIEND_OPERATOR_GUARD_CONTROL_HOTKEY")
        or "f8"
    ).strip().lower()
    if hotkey not in CONTROL_HOTKEYS:
        hotkey = "f8"
    return {
        "enabled": first_env_bool(
            ("WECHAT_RPA_OPERATOR_GUARD_ENABLED", "WECHAT_ADD_FRIEND_OPERATOR_GUARD_ENABLED"),
            default=os.name == "nt",
        ),
        "block_manual_input": first_env_bool(
            (
                "WECHAT_RPA_OPERATOR_GUARD_BLOCK_MANUAL_INPUT",
                "WECHAT_ADD_FRIEND_OPERATOR_GUARD_BLOCK_MANUAL_INPUT",
            ),
            default=True,
        ),
        "floating_indicator_enabled": first_env_bool(
            (
                "WECHAT_RPA_OPERATOR_GUARD_FLOATING_INDICATOR_ENABLED",
                "WECHAT_ADD_FRIEND_OPERATOR_GUARD_FLOATING_INDICATOR_ENABLED",
            ),
            default=True,
        ),
        "control_hotkey": hotkey,
        "esc_double_press_window_ms": bounded_int(
            os.getenv("WECHAT_RPA_OPERATOR_GUARD_ESC_DOUBLE_WINDOW_MS")
            or os.getenv("WECHAT_ADD_FRIEND_OPERATOR_GUARD_ESC_DOUBLE_WINDOW_MS"),
            default=420,
            minimum=180,
            maximum=1200,
        ),
        "pause_poll_interval_ms": bounded_int(
            os.getenv("WECHAT_RPA_OPERATOR_GUARD_PAUSE_POLL_INTERVAL_MS")
            or os.getenv("WECHAT_ADD_FRIEND_OPERATOR_GUARD_PAUSE_POLL_INTERVAL_MS"),
            default=100,
            minimum=50,
            maximum=500,
        ),
        "bootstrap_timeout_seconds": bounded_float(
            os.getenv("WECHAT_RPA_OPERATOR_GUARD_BOOTSTRAP_TIMEOUT_SECONDS")
            or os.getenv("WECHAT_ADD_FRIEND_OPERATOR_GUARD_BOOTSTRAP_TIMEOUT_SECONDS"),
            default=15.0,
            minimum=3.0,
            maximum=60.0,
        ),
        "pause_max_seconds": bounded_float(
            os.getenv("WECHAT_RPA_OPERATOR_GUARD_PAUSE_MAX_SECONDS")
            or os.getenv("WECHAT_ADD_FRIEND_OPERATOR_GUARD_PAUSE_MAX_SECONDS"),
            default=600.0,
            minimum=5.0,
            maximum=3600.0,
        ),
    }


def rpa_operator_guard_dir(tenant_id: str | None = None) -> Path:
    return tenant_runtime_root(tenant_id) / "rpa_operator_guard"


def rpa_operator_guard_paths(tenant_id: str | None = None) -> dict[str, Path]:
    root = rpa_operator_guard_dir(tenant_id)
    return {
        "root": root,
        "control_path": root / "operator_control.json",
        "status_path": root / "runtime_status.json",
        "state_path": root / "operator_guard.state.json",
        "heartbeat_a_path": root / "operator_guard.heartbeat.0.json",
        "heartbeat_b_path": root / "operator_guard.heartbeat.1.json",
        "pid_path": root / "operator_guard.pid.json",
        "stdout_log_path": root / "operator_guard.stdout.log",
        "stderr_log_path": root / "operator_guard.stderr.log",
    }


def read_latest_guard_heartbeat(paths: dict[str, Any]) -> dict[str, Any]:
    """Read the newest valid heartbeat from the alternating heartbeat files."""

    newest: dict[str, Any] = {}
    newest_at = datetime.min.replace(tzinfo=timezone.utc)
    for key in ("heartbeat_a_path", "heartbeat_b_path"):
        raw_path = str(paths.get(key) or "").strip()
        if not raw_path:
            continue
        candidate = read_json(Path(raw_path), attempts=2, retry_delay_seconds=0.01)
        heartbeat_text = str(candidate.get("heartbeat_at") or "")
        try:
            heartbeat_at = datetime.fromisoformat(heartbeat_text.replace("Z", "+00:00"))
            if heartbeat_at.tzinfo is None:
                heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
            heartbeat_at = heartbeat_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if heartbeat_at > newest_at:
            newest = candidate
            newest_at = heartbeat_at
    return newest


def empty_operator_control_state(
    tenant_id: str,
    *,
    mode: str = "idle",
    guard_instance_id: str = "",
    client_instance_id: str = "",
    owner_worker_pid: int = 0,
    owner_process_create_time: float = 0.0,
) -> dict[str, Any]:
    normalized_mode = mode if mode in CONTROL_MODES else "idle"
    return {
        "version": 2,
        "tenant_id": tenant_id,
        "guard_instance_id": guard_instance_id,
        "client_instance_id": client_instance_id,
        "owner_worker_pid": int(owner_worker_pid or 0),
        "owner_process_create_time": float(owner_process_create_time or 0.0),
        "mode": normalized_mode,
        "active_ui_lock_id": "",
        "active_fencing_token": 0,
        "operation_type": "",
        "current_step": "",
        "control_epoch": 0,
        "command": {
            "id": 0,
            "action": "none",
            "status": "idle",
            "source": "",
            "requested_at": "",
            "applied_at": "",
            "message": "",
        },
        "updated_at": now_iso(),
    }


def read_json(
    path: Path,
    *,
    attempts: int = 5,
    retry_delay_seconds: float = 0.02,
) -> dict[str, Any]:
    """Read an atomically replaced runtime file without treating a read race as a fault."""

    for attempt in range(max(1, int(attempts))):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if attempt + 1 < max(1, int(attempts)):
                time.sleep(max(0.0, float(retry_delay_seconds)))
                continue
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    output = dict(payload)
    output["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(output, ensure_ascii=False, indent=2)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    for attempt in range(4):
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
            return
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass
            if attempt >= 3:
                raise
            time.sleep(0.01 * (attempt + 1))
            temp = path.with_name(
                f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )


def clear_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    if not proc.is_running():
        return False
    try:
        return proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return True


def terminate_pid_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def sync_operator_mode(path: Path, *, tenant_id: str, mode: str, message: str = "") -> dict[str, Any]:
    payload = read_json(path) or empty_operator_control_state(tenant_id)
    payload["tenant_id"] = tenant_id
    payload["mode"] = mode if mode in CONTROL_MODES else "idle"
    command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
    if message:
        command["message"] = message
    payload["command"] = command
    write_json(path, payload)
    return payload


def verify_rpa_operator_guard(
    *,
    pid: int,
    state_path: Path,
    expected_parent_pid: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    last_state: dict[str, Any] = {}
    while time.monotonic() - started <= max(0.2, float(timeout_seconds)):
        snapshot = read_json(state_path)
        if snapshot:
            last_state = snapshot
            state_parent = int(snapshot.get("parent_pid") or 0)
            state_pid = int(snapshot.get("pid") or 0)
            if state_parent != int(expected_parent_pid) or state_pid != int(pid):
                time.sleep(0.08)
                continue
            if str(snapshot.get("phase") or "").strip().lower() == "failed":
                break
            if bool(snapshot.get("hooks_installed")):
                break
        if not pid_alive(pid):
            return {"ok": False, "reason": "guard_process_exited_early", "pid": pid, "state": last_state}
        time.sleep(0.08)
    if not last_state:
        return {
            "ok": False,
            "reason": "guard_state_missing",
            "pid": pid,
            "process_alive": pid_alive(pid),
            "timeout_seconds": float(timeout_seconds),
        }
    if str(last_state.get("phase") or "").strip().lower() == "failed" or not bool(last_state.get("hooks_installed")):
        return {
            "ok": False,
            "reason": "guard_hook_not_ready",
            "pid": pid,
            "state": last_state,
            "timeout_seconds": float(timeout_seconds),
        }
    try:
        state_pid = int(last_state.get("pid") or 0)
    except (TypeError, ValueError):
        state_pid = 0
    mode = str(last_state.get("mode") or "").strip().lower()
    if mode not in CONTROL_MODES:
        return {
            "ok": False,
            "reason": "guard_mode_invalid",
            "pid": pid,
            "state_pid": state_pid,
            "state": last_state,
            "timeout_seconds": float(timeout_seconds),
        }
    if bool(last_state.get("lock_enabled")) != (mode == "active"):
        return {
            "ok": False,
            "reason": "guard_lock_mode_mismatch",
            "pid": pid,
            "state_pid": state_pid,
            "state": last_state,
        }
    return {"ok": True, "reason": "guard_ready", "pid": pid, "state_pid": state_pid, "state": last_state}


def operator_guard_command(args: list[str]) -> list[str]:
    if bool(getattr(sys, "frozen", False)):
        return [str(sys.executable), "--rpa-operator-guard", *args]
    return [str(sys.executable), str(GUARD_SCRIPT), *args]


def _process_create_time(pid: int) -> float:
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, OSError, ValueError):
        return 0.0


def wait_for_pid_exit(pid: int, *, timeout_seconds: float) -> bool:
    """Wait for a Windows child to disappear, including post-signal teardown."""

    if pid <= 0:
        return True
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.08)
    return not pid_alive(pid)


def _guard_process_identity_matches(
    record: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    try:
        pid = int(record.get("pid") or 0)
        recorded_create_time = float(record.get("guard_process_create_time") or 0.0)
        owner_pid = int(record.get("owner_worker_pid") or 0)
        owner_create_time = float(record.get("owner_process_create_time") or 0.0)
        state_pid = int(state.get("pid") or 0)
        state_owner_pid = int(state.get("owner_worker_pid") or 0)
        state_owner_create_time = float(state.get("owner_process_create_time") or 0.0)
    except (TypeError, ValueError):
        return False
    instance_id = str(record.get("guard_instance_id") or "").strip()
    if pid <= 0 or not instance_id or recorded_create_time <= 0:
        return False
    if str(state.get("guard_instance_id") or "") != instance_id:
        return False
    if str(state.get("client_instance_id") or "") != str(record.get("client_instance_id") or ""):
        return False
    if state_pid != pid:
        return False
    if owner_pid <= 0 or owner_create_time <= 0:
        return False
    if state_owner_pid != owner_pid:
        return False
    if abs(state_owner_create_time - owner_create_time) > 0.01:
        return False
    if abs(_process_create_time(pid) - recorded_create_time) > 0.01:
        return False
    try:
        process = psutil.Process(pid)
        command = " ".join(process.cmdline()).lower()
        parent_pid = int(process.ppid() or 0)
    except psutil.Error:
        return False
    return (
        parent_pid == owner_pid
        and ("--rpa-operator-guard" in command or "run_rpa_operator_guard.py" in command)
    )


def start_rpa_operator_guard(
    *,
    operation: str = "worker_lifecycle",
    route: str = "",
    artifact_dir: str | None = None,
    initial_mode: str = "idle",
    client_instance_id: str = "",
) -> dict[str, Any]:
    global _ACTIVE_GUARD
    settings = rpa_operator_guard_settings()
    tenant_id = active_tenant_id()
    paths = rpa_operator_guard_paths(tenant_id)
    operation_name = str(operation or route or "worker_lifecycle").strip()
    normalized_initial_mode = initial_mode if initial_mode in CONTROL_MODES else "idle"
    guard_instance_id = f"guard_{uuid.uuid4()}"
    owner_worker_pid = os.getpid()
    owner_process_create_time = _process_create_time(owner_worker_pid)
    base = {
        "ok": True,
        "enabled": bool(settings.get("enabled")),
        "tenant_id": tenant_id,
        "settings": settings,
        "operation": operation_name,
        "route": operation_name,
        "artifact_dir": str(artifact_dir or ""),
        "paths": {key: str(value) for key, value in paths.items()},
        "script_path": str(GUARD_SCRIPT),
        "guard_instance_id": guard_instance_id,
        "client_instance_id": str(client_instance_id or ""),
        "owner_worker_pid": owner_worker_pid,
        "owner_process_create_time": owner_process_create_time,
    }
    if os.name != "nt":
        result = {**base, "enabled": False, "started": False, "reason": "windows_only"}
        _ACTIVE_GUARD = None
        return result
    if not settings.get("enabled"):
        result = {**base, "started": False, "reason": "operator_guard_disabled"}
        _ACTIVE_GUARD = None
        return result
    if not GUARD_SCRIPT.exists():
        result = {**base, "ok": False, "started": False, "reason": "operator_guard_script_missing"}
        _ACTIVE_GUARD = None
        return result

    existing_record = read_json(paths["pid_path"])
    existing_pid = int(existing_record.get("pid") or 0)
    if existing_pid and pid_alive(existing_pid):
        existing_state = read_json(paths["state_path"])
        existing_heartbeat = read_latest_guard_heartbeat(paths)
        if existing_heartbeat:
            existing_state = {**existing_state, **existing_heartbeat}
        if not _guard_process_identity_matches(existing_record, existing_state):
            result = {
                **base,
                "ok": False,
                "started": False,
                "reason": "operator_guard_identity_mismatch",
                "existing_pid": existing_pid,
            }
            _ACTIVE_GUARD = result
            return result
        existing_control = read_json(paths["control_path"])
        existing_control["mode"] = "stopped"
        existing_control["shutdown_requested"] = True
        existing_control["control_epoch"] = int(existing_control.get("control_epoch") or 0) + 1
        write_json(paths["control_path"], existing_control)
        # Real Windows teardown can exceed three seconds while hooks and the
        # layered window are being released. Do not declare a rebuild failure
        # just before the old guard finishes its clean exit.
        if not wait_for_pid_exit(existing_pid, timeout_seconds=8.0):
            terminate_pid_tree(existing_pid)
        if not wait_for_pid_exit(existing_pid, timeout_seconds=5.0):
            result = {**base, "ok": False, "started": False, "reason": "operator_guard_stop_failed"}
            _ACTIVE_GUARD = result
            return result

    clear_file(paths["state_path"])
    clear_file(paths["pid_path"])
    clear_file(paths["heartbeat_a_path"])
    clear_file(paths["heartbeat_b_path"])
    write_json(
        paths["control_path"],
        empty_operator_control_state(
            tenant_id,
            mode=normalized_initial_mode,
            guard_instance_id=guard_instance_id,
            client_instance_id=str(client_instance_id or ""),
            owner_worker_pid=owner_worker_pid,
            owner_process_create_time=owner_process_create_time,
        ),
    )
    write_json(
        paths["status_path"],
        {
            "ok": True,
            "state": normalized_initial_mode,
            "message": "Worker 安全守护正在启动。",
            "tenant_id": tenant_id,
        },
    )
    guard_args = [
        "--tenant-id",
        tenant_id,
        "--control-path",
        str(paths["control_path"]),
        "--status-path",
        str(paths["status_path"]),
        "--guard-state-path",
        str(paths["state_path"]),
        "--heartbeat-path-a",
        str(paths["heartbeat_a_path"]),
        "--heartbeat-path-b",
        str(paths["heartbeat_b_path"]),
        "--parent-pid",
        str(owner_worker_pid),
        "--guard-instance-id",
        guard_instance_id,
        "--client-instance-id",
        str(client_instance_id or ""),
        "--owner-process-create-time",
        str(owner_process_create_time),
        "--control-key",
        str(settings.get("control_hotkey") or "f8"),
        "--esc-double-window-ms",
        str(int(settings.get("esc_double_press_window_ms") or 420)),
        "--pause-poll-interval-ms",
        str(int(settings.get("pause_poll_interval_ms") or 100)),
    ]
    guard_args.append("--block-manual-input" if settings.get("block_manual_input", True) else "--allow-manual-input")
    guard_args.append("--floating-indicator" if settings.get("floating_indicator_enabled", True) else "--no-floating-indicator")
    command = operator_guard_command(guard_args)
    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        paths["stdout_log_path"].parent.mkdir(parents=True, exist_ok=True)
        stdout_handle = paths["stdout_log_path"].open("ab")
        stderr_handle = paths["stderr_log_path"].open("ab")
    except OSError as exc:
        result = {**base, "ok": False, "started": False, "reason": "operator_guard_log_open_failed", "error": repr(exc)}
        _ACTIVE_GUARD = None
        return result
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creationflags,
        )
    except Exception as exc:
        result = {**base, "ok": False, "started": False, "reason": "operator_guard_process_launch_failed", "error": repr(exc)}
        _ACTIVE_GUARD = None
        return result
    finally:
        try:
            stdout_handle.close()
        except Exception:
            pass
        try:
            stderr_handle.close()
        except Exception:
            pass

    write_json(
        paths["pid_path"],
        {
            "pid": int(proc.pid),
            "guard_instance_id": guard_instance_id,
            "client_instance_id": str(client_instance_id or ""),
            "tenant_id": tenant_id,
            "started_at": now_iso(),
            "guard_process_create_time": _process_create_time(int(proc.pid)),
            "owner_worker_pid": owner_worker_pid,
            "owner_process_create_time": owner_process_create_time,
            "control_path": str(paths["control_path"]),
            "status_path": str(paths["status_path"]),
            "state_path": str(paths["state_path"]),
            "heartbeat_a_path": str(paths["heartbeat_a_path"]),
            "heartbeat_b_path": str(paths["heartbeat_b_path"]),
            "parent_pid": owner_worker_pid,
        },
    )
    verify = verify_rpa_operator_guard(
        pid=int(proc.pid),
        state_path=paths["state_path"],
        expected_parent_pid=owner_worker_pid,
        timeout_seconds=float(settings.get("bootstrap_timeout_seconds") or 15.0),
    )
    verify_state = verify.get("state") if isinstance(verify.get("state"), dict) else {}
    identity_verified = (
        str(verify_state.get("guard_instance_id") or "") == guard_instance_id
        and int(verify_state.get("owner_worker_pid") or 0) == owner_worker_pid
        and abs(float(verify_state.get("owner_process_create_time") or 0.0) - owner_process_create_time) <= 0.01
    )
    result = {
        **base,
        "ok": verify.get("ok") is True and identity_verified,
        "started": True,
        "reused_existing": False,
        "pid": int(proc.pid),
        "verify": verify,
        "control_path": str(paths["control_path"]),
        "guard_instance_id": guard_instance_id,
        "client_instance_id": str(client_instance_id or ""),
        "owner_worker_pid": owner_worker_pid,
        "owner_process_create_time": owner_process_create_time,
        "guard_process_create_time": _process_create_time(int(proc.pid)),
        "identity_verified": identity_verified,
    }
    if result["ok"]:
        _ACTIVE_GUARD = result
        return result
    stop_rpa_operator_guard(result, reason="operator_guard_verify_failed")
    _ACTIVE_GUARD = None
    return result


def stop_rpa_operator_guard(guard: dict[str, Any] | None, *, reason: str = "rpa_operation_finished") -> dict[str, Any]:
    """Shut down the Worker-owned guard only during Worker exit or verified rebuild."""

    global _ACTIVE_GUARD
    if not isinstance(guard, dict) or not guard.get("enabled"):
        _ACTIVE_GUARD = None
        return {"ok": True, "skipped": True, "reason": "operator_guard_not_enabled"}
    paths = guard.get("paths") if isinstance(guard.get("paths"), dict) else {}
    control_path = Path(str(paths.get("control_path") or ""))
    pid_path = Path(str(paths.get("pid_path") or ""))
    state_path = Path(str(paths.get("state_path") or ""))
    pid_record = read_json(pid_path)
    state = read_json(state_path)
    heartbeat = read_latest_guard_heartbeat(paths)
    if heartbeat:
        state = {**state, **heartbeat}
    pid = int(pid_record.get("pid") or guard.get("pid") or 0)
    process_was_alive = pid_alive(pid)
    if pid > 0 and pid_alive(pid) and not _guard_process_identity_matches(pid_record, state):
        _ACTIVE_GUARD = guard
        return {
            "ok": False,
            "reason": "operator_guard_identity_mismatch",
            "pid": pid,
        }
    try:
        control = read_json(control_path)
        control.update(
            {
                "mode": "stopped",
                "active_ui_lock_id": "",
                "active_fencing_token": 0,
                "shutdown_requested": True,
                "control_epoch": int(control.get("control_epoch") or 0) + 1,
            }
        )
        command = control.get("command") if isinstance(control.get("command"), dict) else {}
        command.update({"action": "shutdown", "status": "pending", "message": reason})
        control["command"] = command
        write_json(control_path, control)
    except Exception as exc:
        _ACTIVE_GUARD = guard
        return {"ok": False, "reason": "operator_guard_stop_signal_failed", "error": repr(exc), "pid": pid}
    if not wait_for_pid_exit(pid, timeout_seconds=8.0):
        terminate_pid_tree(pid)
    process_alive_after_stop = not wait_for_pid_exit(pid, timeout_seconds=5.0)
    final_state = read_json(state_path)
    final_heartbeat = read_latest_guard_heartbeat(paths)
    if final_heartbeat:
        final_state = {**final_state, **final_heartbeat}
    try:
        final_pid = int(final_state.get("pid") or 0)
    except (TypeError, ValueError):
        final_pid = 0
    clean_guard_exit = bool(
        str(final_state.get("phase") or "") == "stopped"
        and str(final_state.get("reason") or "") == "guard_exit"
        and bool(final_state.get("lock_enabled")) is False
        and final_pid == pid
        and str(final_state.get("guard_instance_id") or "")
        == str(guard.get("guard_instance_id") or "")
    )
    release = {
        "ok": process_was_alive and not process_alive_after_stop and clean_guard_exit,
        "reason": (
            reason
            if process_was_alive and not process_alive_after_stop and clean_guard_exit
            else "operator_guard_stop_not_verified"
            if not process_alive_after_stop
            else "operator_guard_process_still_alive"
        ),
        "pid": pid,
        "process_alive_after_stop": process_alive_after_stop,
        "clean_guard_exit": clean_guard_exit,
        "final_state": final_state,
    }
    if release["ok"]:
        _ACTIVE_GUARD = None
    return release


def rpa_operator_guard_health(guard: dict[str, Any] | None) -> dict[str, Any]:
    """Return a non-blocking health snapshot for the UI-lock owner."""

    if not isinstance(guard, dict) or not guard.get("enabled"):
        return {"ok": True, "mode": "not_enabled", "reason": "operator_guard_not_enabled"}
    paths = guard.get("paths") if isinstance(guard.get("paths"), dict) else {}
    pid_record = read_json(Path(str(paths.get("pid_path") or "")))
    pid = int(pid_record.get("pid") or guard.get("pid") or 0)
    state = read_json(Path(str(paths.get("state_path") or "")))
    heartbeat = read_latest_guard_heartbeat(paths)
    if (
        heartbeat
        and int(heartbeat.get("pid") or 0) == pid
        and str(heartbeat.get("guard_instance_id") or "")
        == str(guard.get("guard_instance_id") or "")
    ):
        # The state JSON is deliberately replaceable and can be held by
        # antivirus/evidence readers. Health must use the independent heartbeat
        # channel so a live guard is not killed because that one file is busy.
        state = {**state, **heartbeat}
    mode = str(state.get("mode") or "fault").strip().lower()
    phase = str(state.get("phase") or "").strip().lower()
    hooks_installed = bool(state.get("hooks_installed"))
    if not pid_alive(pid):
        return {
            "ok": False,
            "mode": mode,
            "reason": "guard_process_exited_early",
            "pid": pid,
            "state": state,
        }
    if not _guard_process_identity_matches(pid_record, state):
        return {"ok": False, "mode": "fault", "reason": "operator_guard_identity_mismatch", "pid": pid, "state": state}
    if str(state.get("guard_instance_id") or "") != str(guard.get("guard_instance_id") or ""):
        return {"ok": False, "mode": "fault", "reason": "operator_guard_instance_mismatch", "pid": pid, "state": state}
    heartbeat_text = str(state.get("heartbeat_at") or "")
    try:
        heartbeat_at = datetime.fromisoformat(heartbeat_text.replace("Z", "+00:00"))
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        heartbeat_age = (datetime.now(timezone.utc) - heartbeat_at.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        heartbeat_age = 9999.0
    if heartbeat_age > 2.0:
        return {"ok": False, "mode": "fault", "reason": "operator_guard_heartbeat_expired", "pid": pid, "heartbeat_age_seconds": heartbeat_age, "state": state}
    if phase == "failed" or not hooks_installed:
        return {
            "ok": False,
            "mode": mode,
            "reason": "guard_hook_not_ready",
            "pid": pid,
            "state": state,
        }
    if mode not in CONTROL_MODES or bool(state.get("lock_enabled")) != (mode == "active"):
        return {
            "ok": False,
            "mode": "fault",
            "reason": "operator_guard_state_invalid",
            "pid": pid,
            "state": state,
        }
    if mode == "fault":
        return {
            "ok": False,
            "mode": "fault",
            "reason": str(state.get("reason") or "operator_guard_fault"),
            "pid": pid,
            "state": state,
        }
    return {
        "ok": True,
        "mode": mode,
        "reason": "guard_ready",
        "pid": pid,
        "heartbeat_age_seconds": heartbeat_age,
        "state": state,
    }


def transition_rpa_operator_guard(
    guard: dict[str, Any] | None,
    *,
    mode: str,
    ui_lock_id: str = "",
    fencing_token: int = 0,
    operation_type: str = "",
    current_step: str = "",
    reason: str = "",
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    if not isinstance(guard, dict) or not guard.get("enabled"):
        return {"ok": True, "mode": "not_enabled", "skipped": True, "reason": "operator_guard_not_enabled"}
    target_mode = mode if mode in CONTROL_MODES else "fault"
    paths = guard.get("paths") if isinstance(guard.get("paths"), dict) else {}
    control_path = Path(str(paths.get("control_path") or ""))
    health = rpa_operator_guard_health(guard)
    if health.get("ok") is not True:
        return health
    state = health.get("state") if isinstance(health.get("state"), dict) else {}
    if target_mode == "active" and (not ui_lock_id or int(fencing_token or 0) <= 0):
        return {"ok": False, "mode": "fault", "reason": "operator_guard_activation_identity_missing"}
    if target_mode != "active":
        ui_lock_id = ""
        fencing_token = 0
        operation_type = ""
        current_step = ""
    control = read_json(control_path)
    if str(control.get("guard_instance_id") or "") != str(guard.get("guard_instance_id") or ""):
        return {"ok": False, "mode": "fault", "reason": "operator_guard_instance_mismatch"}
    next_epoch = max(int(control.get("control_epoch") or 0), int(state.get("control_epoch") or 0)) + 1
    command = control.get("command") if isinstance(control.get("command"), dict) else {}
    command_action = {
        "active": "activate",
        "ready": "deactivate",
        "paused": "pause",
        "stopped": "stop",
    }.get(target_mode, target_mode)
    command.update(
        {
            "id": int(command.get("id") or 0) + 1,
            "action": command_action,
            "status": "pending",
            "source": "worker",
            "requested_at": now_iso(),
            "applied_at": "",
            "message": reason,
        }
    )
    control.update(
        {
            "mode": target_mode,
            "active_ui_lock_id": ui_lock_id,
            "active_fencing_token": int(fencing_token or 0),
            "operation_type": operation_type,
            "current_step": current_step,
            "control_epoch": next_epoch,
            "command": command,
            # A state transition never owns process shutdown. Only
            # stop_rpa_operator_guard() may set this flag.
            "shutdown_requested": False,
        }
    )
    write_json(control_path, control)
    started = time.monotonic()
    last_health: dict[str, Any] = {}
    while time.monotonic() - started <= max(0.2, timeout_seconds):
        last_health = rpa_operator_guard_health(guard)
        snapshot = last_health.get("state") if isinstance(last_health.get("state"), dict) else {}
        if last_health.get("ok") is not True:
            return last_health
        if (
            str(snapshot.get("mode") or "") == target_mode
            and int(snapshot.get("control_epoch") or -1) == next_epoch
            and str(snapshot.get("guard_instance_id") or "") == str(guard.get("guard_instance_id") or "")
            and str(snapshot.get("active_ui_lock_id") or "") == ui_lock_id
            and int(snapshot.get("active_fencing_token") or 0) == int(fencing_token or 0)
            and bool(snapshot.get("lock_enabled")) == (target_mode == "active")
        ):
            return {
                "ok": True,
                "mode": target_mode,
                "guard_instance_id": guard.get("guard_instance_id"),
                "active_ui_lock_id": ui_lock_id,
                "active_fencing_token": int(fencing_token or 0),
                "control_epoch": next_epoch,
                "hooks_installed": bool(snapshot.get("hooks_installed")),
                "lock_enabled": bool(snapshot.get("lock_enabled")),
                "pid": guard.get("pid"),
                "state": snapshot,
                "reason": reason,
            }
        time.sleep(0.04)
    return {"ok": False, "mode": "fault", "reason": "operator_guard_transition_timeout", "expected_control_epoch": next_epoch, "last_health": last_health}


def attach_rpa_operator_guard() -> dict[str, Any]:
    """Attach a Sidecar to the Worker-owned guard without creating or stopping it."""

    global _ACTIVE_GUARD
    root = str(os.environ.get("CHEJIN_OPERATOR_GUARD_ROOT") or "").strip()
    paths = rpa_operator_guard_paths()
    if root:
        root_path = Path(root)
        paths = {
            **paths,
            "root": root_path,
            "control_path": root_path / "operator_control.json",
            "status_path": root_path / "runtime_status.json",
            "state_path": root_path / "operator_guard.state.json",
            "pid_path": root_path / "operator_guard.pid.json",
        }
    pid_record = read_json(paths["pid_path"])
    guard = {
        "ok": True,
        "enabled": os.name == "nt",
        "started": True,
        "pid": int(pid_record.get("pid") or 0),
        "guard_instance_id": str(os.environ.get("CHEJIN_OPERATOR_GUARD_INSTANCE_ID") or pid_record.get("guard_instance_id") or ""),
        "tenant_id": active_tenant_id(),
        "paths": {key: str(value) for key, value in paths.items()},
    }
    if os.name != "nt":
        guard.update({"enabled": False, "started": False, "reason": "windows_only"})
    _ACTIVE_GUARD = guard
    health = rpa_operator_guard_health(guard)
    return {**guard, **({"ok": False, "reason": health.get("reason")} if health.get("ok") is not True else {"health": health})}


def rpa_operator_guard_checkpoint(*, reason: str = "") -> dict[str, Any]:
    guard = _ACTIVE_GUARD or attach_rpa_operator_guard()
    if not isinstance(guard, dict) or not guard.get("enabled"):
        return {"ok": True, "skipped": True, "reason": "operator_guard_not_enabled"}
    health = rpa_operator_guard_health(guard)
    if health.get("ok") is not True:
        raise RuntimeError(f"rpa_operator_guard_fault:{health.get('reason')}:{reason}")
    mode = str(health.get("mode") or "fault")
    if mode != "active":
        raise RuntimeError(f"rpa_operator_guard_{mode}:{reason}")
    return {"ok": True, "mode": mode, "reason": reason, "state": health.get("state")}
