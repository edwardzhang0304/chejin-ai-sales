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
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root


PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = PROJECT_ROOT / "apps" / "wechat_ai_customer_service"
GUARD_SCRIPT = APP_ROOT / "scripts" / "run_rpa_operator_guard.py"
CONTROL_MODES = {"running", "paused", "stopped"}
CONTROL_HOTKEYS = {"f8", "esc"}

_ACTIVE_GUARD: dict[str, Any] | None = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
            default=550,
            minimum=120,
            maximum=3000,
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
        "pid_path": root / "operator_guard.pid.json",
        "stdout_log_path": root / "operator_guard.stdout.log",
        "stderr_log_path": root / "operator_guard.stderr.log",
    }


def empty_operator_control_state(tenant_id: str, *, mode: str = "running") -> dict[str, Any]:
    normalized_mode = mode if mode in CONTROL_MODES else "running"
    return {
        "version": 1,
        "tenant_id": tenant_id,
        "mode": normalized_mode,
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


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    output = dict(payload)
    output["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(output, ensure_ascii=False, indent=2)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


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
    payload["mode"] = mode if mode in CONTROL_MODES else "running"
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
            if state_parent != int(expected_parent_pid) and state_pid != int(pid):
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
    if not bool(last_state.get("lock_enabled")):
        return {
            "ok": False,
            "reason": "guard_lock_not_enabled",
            "pid": pid,
            "state_pid": state_pid,
            "state": last_state,
            "timeout_seconds": float(timeout_seconds),
        }
    return {"ok": True, "reason": "guard_ready", "pid": pid, "state_pid": state_pid, "state": last_state}


def start_rpa_operator_guard(*, operation: str = "", route: str = "", artifact_dir: str | None = None) -> dict[str, Any]:
    global _ACTIVE_GUARD
    settings = rpa_operator_guard_settings()
    tenant_id = active_tenant_id()
    paths = rpa_operator_guard_paths(tenant_id)
    operation_name = str(operation or route or "").strip()
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

    existing_pid = int(read_json(paths["pid_path"]).get("pid") or 0)
    if existing_pid and pid_alive(existing_pid):
        try:
            sync_operator_mode(paths["control_path"], tenant_id=tenant_id, mode="stopped", message="replace_stale_rpa_guard")
            started = time.monotonic()
            while pid_alive(existing_pid) and time.monotonic() - started < 2.0:
                time.sleep(0.08)
        except Exception:
            pass
        if pid_alive(existing_pid):
            terminate_pid_tree(existing_pid)

    clear_file(paths["state_path"])
    clear_file(paths["pid_path"])
    write_json(paths["control_path"], empty_operator_control_state(tenant_id, mode="running"))
    write_json(
        paths["status_path"],
        {
            "ok": True,
            "state": "thinking",
            "message": "微信 RPA 正在运行，悬浮球键鼠守护已接管。",
            "tenant_id": tenant_id,
        },
    )
    command = [
        str(sys.executable),
        str(GUARD_SCRIPT),
        "--tenant-id",
        tenant_id,
        "--control-path",
        str(paths["control_path"]),
        "--status-path",
        str(paths["status_path"]),
        "--guard-state-path",
        str(paths["state_path"]),
        "--parent-pid",
        str(os.getpid()),
        "--control-key",
        str(settings.get("control_hotkey") or "f8"),
        "--esc-double-window-ms",
        str(int(settings.get("esc_double_press_window_ms") or 420)),
        "--pause-poll-interval-ms",
        str(int(settings.get("pause_poll_interval_ms") or 550)),
    ]
    command.append("--block-manual-input" if settings.get("block_manual_input", True) else "--allow-manual-input")
    command.append("--floating-indicator" if settings.get("floating_indicator_enabled", True) else "--no-floating-indicator")
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
            "tenant_id": tenant_id,
            "started_at": now_iso(),
            "control_path": str(paths["control_path"]),
            "status_path": str(paths["status_path"]),
            "state_path": str(paths["state_path"]),
            "parent_pid": os.getpid(),
        },
    )
    verify = verify_rpa_operator_guard(
        pid=int(proc.pid),
        state_path=paths["state_path"],
        expected_parent_pid=os.getpid(),
        timeout_seconds=float(settings.get("bootstrap_timeout_seconds") or 15.0),
    )
    result = {
        **base,
        "ok": verify.get("ok") is True,
        "started": True,
        "reused_existing": False,
        "pid": int(proc.pid),
        "verify": verify,
        "control_path": str(paths["control_path"]),
    }
    if result["ok"]:
        _ACTIVE_GUARD = result
        return result
    stop_rpa_operator_guard(result, reason="operator_guard_verify_failed")
    _ACTIVE_GUARD = None
    return result


def stop_rpa_operator_guard(guard: dict[str, Any] | None, *, reason: str = "rpa_operation_finished") -> dict[str, Any]:
    global _ACTIVE_GUARD
    if not isinstance(guard, dict) or not guard.get("enabled"):
        _ACTIVE_GUARD = None
        return {"ok": True, "skipped": True, "reason": "operator_guard_not_enabled"}
    if guard.get("reused_existing"):
        _ACTIVE_GUARD = None
        return {"ok": True, "skipped": True, "reason": "reused_existing_operator_guard_not_stopped"}
    paths = guard.get("paths") if isinstance(guard.get("paths"), dict) else {}
    tenant_id = str(guard.get("tenant_id") or active_tenant_id())
    control_path = Path(str(paths.get("control_path") or ""))
    status_path = Path(str(paths.get("status_path") or ""))
    pid_path = Path(str(paths.get("pid_path") or ""))
    state_snapshot = read_json(Path(str(paths.get("state_path") or "")))
    verify = guard.get("verify") if isinstance(guard.get("verify"), dict) else {}
    verify_state = verify.get("state") if isinstance(verify.get("state"), dict) else {}
    pid_candidates: set[int] = set()
    for raw_pid in (
        guard.get("pid"),
        verify.get("state_pid"),
        verify_state.get("pid"),
        state_snapshot.get("pid"),
        read_json(pid_path).get("pid"),
    ):
        try:
            parsed_pid = int(raw_pid or 0)
        except (TypeError, ValueError):
            parsed_pid = 0
        if parsed_pid > 0:
            pid_candidates.add(parsed_pid)
    pid = int(guard.get("pid") or 0)
    try:
        if str(control_path):
            sync_operator_mode(control_path, tenant_id=tenant_id, mode="stopped", message=reason)
        if str(status_path):
            write_json(
                status_path,
                {
                    "ok": True,
                    "state": "stopped",
                    "message": "微信 RPA 已结束，键鼠已释放。",
                    "tenant_id": tenant_id,
                },
            )
    except Exception as exc:
        release = {"ok": False, "reason": "operator_guard_stop_signal_failed", "error": repr(exc), "pid": pid}
    else:
        started = time.monotonic()
        while any(pid_alive(candidate) for candidate in pid_candidates) and time.monotonic() - started < 3.0:
            time.sleep(0.08)
        for candidate in sorted(pid_candidates):
            if pid_alive(candidate):
                terminate_pid_tree(candidate)
        alive_after = {str(candidate): pid_alive(candidate) for candidate in sorted(pid_candidates)}
        release = {
            "ok": True,
            "reason": reason,
            "pid": pid,
            "pid_candidates": sorted(pid_candidates),
            "process_alive_after_stop": any(alive_after.values()),
            "alive_after": alive_after,
        }
    _ACTIVE_GUARD = None
    return release


def rpa_operator_guard_checkpoint(*, reason: str = "") -> dict[str, Any]:
    guard = _ACTIVE_GUARD
    if not isinstance(guard, dict) or not guard.get("enabled"):
        return {"ok": True, "skipped": True, "reason": "operator_guard_not_enabled"}
    paths = guard.get("paths") if isinstance(guard.get("paths"), dict) else {}
    control_path = Path(str(paths.get("control_path") or ""))
    status_path = Path(str(paths.get("status_path") or ""))
    settings = guard.get("settings") if isinstance(guard.get("settings"), dict) else {}
    pause_max_seconds = float(settings.get("pause_max_seconds") or 600.0)
    waited_seconds = 0.0
    started = time.monotonic()
    while True:
        control = read_json(control_path)
        mode = str(control.get("mode") or "running").strip().lower()
        if mode == "stopped":
            raise RuntimeError(f"rpa_operator_guard_stopped:{reason}")
        if mode != "paused":
            return {
                "ok": True,
                "mode": mode or "running",
                "waited_seconds": round(waited_seconds, 3),
                "reason": reason,
            }
        if waited_seconds == 0.0:
            try:
                write_json(
                    status_path,
                    {
                        "ok": True,
                        "state": "paused",
                        "message": "微信 RPA 已暂停，等待悬浮球恢复。",
                        "tenant_id": str(guard.get("tenant_id") or active_tenant_id()),
                    },
                )
            except Exception:
                pass
        if time.monotonic() - started >= pause_max_seconds:
            raise RuntimeError(f"rpa_operator_guard_pause_timeout:{reason}")
        time.sleep(0.2)
        waited_seconds = time.monotonic() - started
