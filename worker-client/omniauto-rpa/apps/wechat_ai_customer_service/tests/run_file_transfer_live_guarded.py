"""Run File Transfer Assistant live regression with lock-screen monitoring.

This wrapper is for unattended live tests. It starts the resumable live
supervisor, keeps the session awake as much as Windows allows, and watches
Winlogon operational events. If Windows locks the session, it stops the child
test immediately, records a diagnosis snapshot, and sends the normal
long-running-task ServerChan notification.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
DEFAULT_SUPERVISOR = APP_ROOT / "tests" / "run_file_transfer_live_supervisor.py"
DEFAULT_RESULT_PATH = Path(
    "runtime/apps/wechat_ai_customer_service/test_artifacts/file_transfer_live_full_guarded.json"
)
DEFAULT_SUMMARY_PATH = Path(
    "runtime/apps/wechat_ai_customer_service/test_artifacts/file_transfer_live_full_guarded_summary.json"
)
DEFAULT_GUARD_PATH = Path(
    "runtime/apps/wechat_ai_customer_service/test_artifacts/file_transfer_live_full_guarded_status.json"
)
DEFAULT_SETTINGS_PATH = Path(
    "runtime/apps/wechat_ai_customer_service/tenants/default/customer_service/settings.json"
)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--supervisor", type=Path, default=DEFAULT_SUPERVISOR)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--guard-path", type=Path, default=DEFAULT_GUARD_PATH)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--temporary-full-auto", action="store_true")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=4)
    parser.add_argument("--start-index", type=int)
    parser.add_argument("--end-index", type=int)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--per-run-timeout-seconds", type=int, default=1200)
    parser.add_argument("--delay-seconds", type=float, default=0.6)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--prevent-idle", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--idle-heartbeat-seconds", type=float, default=55)
    args = parser.parse_args()

    result = run_guarded(args)
    print_json(result)
    return 0 if result.get("ok") else 1


def run_guarded(args: argparse.Namespace) -> dict[str, Any]:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    started_epoch = time.time()
    settings_snapshot = snapshot_settings() if args.temporary_full_auto else None
    stop_event = threading.Event()
    heartbeat_thread = None
    if args.prevent_idle:
        heartbeat_thread = threading.Thread(
            target=keep_awake_loop,
            args=(stop_event, max(10.0, float(args.idle_heartbeat_seconds or 55))),
            daemon=True,
        )
        heartbeat_thread.start()

    command = build_supervisor_command(args)
    supervisor_stdout_path = resolve_path(args.guard_path).with_suffix(".supervisor.stdout.log")
    supervisor_stderr_path = resolve_path(args.guard_path).with_suffix(".supervisor.stderr.log")
    guard_payload = {
        "ok": None,
        "status": "running",
        "started_at": started_at,
        "command": command,
        "result_path": str(args.result_path),
        "summary_path": str(args.summary_path),
        "supervisor_stdout_path": str(supervisor_stdout_path),
        "supervisor_stderr_path": str(supervisor_stderr_path),
        "prevent_idle": bool(args.prevent_idle),
    }
    write_json(resolve_path(args.guard_path), guard_payload)

    supervisor_stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = supervisor_stdout_path.open("w", encoding="utf-8")
    stderr_handle = supervisor_stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lock_event: dict[str, Any] | None = None
    try:
        while process.poll() is None:
            lock_event = latest_lock_event_after(started_epoch)
            if lock_event:
                diagnosis = diagnose_lock(lock_event)
                terminate_process_tree(process)
                wait_after_terminate(process)
                close_handles(stdout_handle, stderr_handle)
                payload = {
                    **guard_payload,
                    "ok": False,
                    "status": "locked",
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "returncode": process.returncode,
                    "lock_event": lock_event,
                    "diagnosis": diagnosis,
                    "stdout_tail": read_tail(supervisor_stdout_path),
                    "stderr_tail": read_tail(supervisor_stderr_path),
                }
                write_json(resolve_path(args.guard_path), payload)
                notify("blocked", "微信实盘长测被锁屏中断", lock_notification_message(payload))
                return payload
            time.sleep(max(2.0, float(args.poll_seconds or 15)))

        wait_after_terminate(process)
        close_handles(stdout_handle, stderr_handle)
        summary = load_json(args.summary_path)
        payload = {
            **guard_payload,
            "ok": process.returncode == 0 and bool(summary.get("ok", process.returncode == 0)),
            "status": "completed" if process.returncode == 0 else "failed",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "returncode": process.returncode,
            "supervisor_summary": summary,
            "stdout_tail": read_tail(supervisor_stdout_path),
            "stderr_tail": read_tail(supervisor_stderr_path),
        }
        write_json(resolve_path(args.guard_path), payload)
        notify("done" if payload["ok"] else "blocked", "微信实盘长测结束", completion_notification_message(payload))
        return payload
    finally:
        close_handles(stdout_handle, stderr_handle)
        stop_event.set()
        if args.prevent_idle:
            set_thread_execution_state(ES_CONTINUOUS)
        if heartbeat_thread:
            heartbeat_thread.join(timeout=2)
        if settings_snapshot is not None:
            restore_settings(settings_snapshot)


def build_supervisor_command(args: argparse.Namespace) -> list[str]:
    command = [
        str(args.python),
        str(args.supervisor),
        "--chunk-size",
        str(args.chunk_size),
        "--per-run-timeout-seconds",
        str(args.per_run_timeout_seconds),
        "--delay-seconds",
        str(args.delay_seconds),
        "--result-path",
        str(args.result_path),
        "--summary-path",
        str(args.summary_path),
    ]
    if args.send:
        command.append("--send")
    if args.temporary_full_auto:
        command.append("--temporary-full-auto")
    if args.reset_state:
        command.append("--reset-state")
    if args.start_index:
        command.extend(["--start-index", str(args.start_index)])
    if args.end_index:
        command.extend(["--end-index", str(args.end_index)])
    for name in args.only or []:
        command.extend(["--only", str(name)])
    return command


def keep_awake_loop(stop_event: threading.Event, interval_seconds: float) -> None:
    while not stop_event.is_set():
        set_thread_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        send_harmless_keypress()
        stop_event.wait(interval_seconds)


def set_thread_execution_state(flags: int) -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def send_harmless_keypress() -> None:
    # VK_F15 is outside normal text entry and is commonly used only to reset the
    # Windows idle timer. It should not type into WeChat or browser fields.
    try:
        user32 = ctypes.windll.user32
        vk_f15 = 0x7E
        keyeventf_keyup = 0x0002
        user32.keybd_event(vk_f15, 0, 0, 0)
        user32.keybd_event(vk_f15, 0, keyeventf_keyup, 0)
    except Exception:
        pass


def latest_lock_event_after(start_epoch: float) -> dict[str, Any] | None:
    start_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start_epoch))
    script = (
        "$start=[datetime]'"
        + start_iso
        + "'; "
        "Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Winlogon/Operational'; StartTime=$start} "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.Message -match '通知事件\\(4\\)' } | "
        "Select-Object -First 1 TimeCreated,Id,ProviderName,Message | ConvertTo-Json -Depth 4"
    )
    output = run_powershell(script)
    if not output.strip():
        return None
    try:
        payload = json.loads(output)
    except Exception:
        return None
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    return payload if isinstance(payload, dict) else None


def diagnose_lock(lock_event: dict[str, Any]) -> dict[str, Any]:
    event_time = str(lock_event.get("TimeCreated") or "")
    script = f"""
$center = [datetime]'{event_time}';
$start = $center.AddMinutes(-5);
$end = $center.AddMinutes(5);
$winlogon = Get-WinEvent -FilterHashtable @{{LogName='Microsoft-Windows-Winlogon/Operational'; StartTime=$start; EndTime=$end}} -ErrorAction SilentlyContinue |
  Select-Object TimeCreated,Id,ProviderName,Message;
$system = Get-WinEvent -FilterHashtable @{{LogName='System'; StartTime=$start; EndTime=$end}} -ErrorAction SilentlyContinue |
  Where-Object {{ $_.Id -in 1,42,107,506,507 -or $_.ProviderName -match 'Power|Kernel|Winlogon' }} |
  Select-Object TimeCreated,Id,ProviderName,Message;
$application = Get-WinEvent -FilterHashtable @{{LogName='Application'; StartTime=$start; EndTime=$end}} -ErrorAction SilentlyContinue |
  Where-Object {{ $_.ProviderName -match 'Goodix|Security-SPP|Application Error' }} |
  Select-Object TimeCreated,Id,ProviderName,Message;
$ps = Get-WinEvent -FilterHashtable @{{LogName='Windows PowerShell'; StartTime=$start; EndTime=$end}} -ErrorAction SilentlyContinue |
  Select-Object -First 10 TimeCreated,Id,Message;
[pscustomobject]@{{
  lock_event = $center;
  winlogon = $winlogon;
  system = $system;
  application = $application;
  powershell = $ps;
}} | ConvertTo-Json -Depth 6
"""
    output = run_powershell(script)
    try:
        payload = json.loads(output) if output.strip() else {}
    except Exception:
        payload = {"raw": output}
    return payload if isinstance(payload, dict) else {"events": payload}


def run_powershell(script: str) -> str:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.stdout


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()


def wait_after_terminate(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def close_handles(*handles: Any) -> None:
    for handle in handles:
        try:
            handle.close()
        except Exception:
            pass


def notify(status: str, title: str, message: str) -> None:
    helper = Path.home() / ".codex" / "skills" / "long-running-task" / "scripts" / "notify_serverchan.py"
    if not helper.exists():
        return
    subprocess.run(
        [
            sys.executable,
            str(helper),
            "--project",
            str(PROJECT_ROOT),
            "--status",
            status,
            "--title",
            title,
            "--message",
            message,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def lock_notification_message(payload: dict[str, Any]) -> str:
    event_time = (payload.get("lock_event") or {}).get("TimeCreated")
    return (
        "微信实盘长测检测到 Windows 锁屏并已停止测试。"
        f"锁屏时间: {event_time}; "
        f"诊断文件: {payload.get('result_path')} / {payload.get('summary_path')} / "
        "runtime/apps/wechat_ai_customer_service/test_artifacts/file_transfer_live_full_guarded_status.json"
    )


def completion_notification_message(payload: dict[str, Any]) -> str:
    summary = payload.get("supervisor_summary") or {}
    return (
        f"微信实盘长测状态: {payload.get('status')}; "
        f"通过 {summary.get('passed_count')} / 选择 {summary.get('selected_count')}; "
        f"失败 {summary.get('failed_count')}; 待跑 {summary.get('pending_count')}; "
        f"汇总: {payload.get('summary_path')}"
    )


def resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def snapshot_settings() -> dict[str, Any]:
    path = resolve_path(DEFAULT_SETTINGS_PATH)
    return {
        "path": str(path),
        "exists": path.exists(),
        "text": path.read_text(encoding="utf-8") if path.exists() else "",
    }


def restore_settings(snapshot: dict[str, Any]) -> None:
    path = Path(str(snapshot.get("path") or ""))
    if not path:
        return
    try:
        if snapshot.get("exists"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(snapshot.get("text") or ""), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except Exception:
        pass


def load_json(path: Path) -> dict[str, Any]:
    resolved = resolve_path(path)
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def tail(value: str, limit: int = 6000) -> str:
    return value if len(value) <= limit else value[-limit:]


def read_tail(path: Path, limit: int = 6000) -> str:
    if not path.exists():
        return ""
    try:
        return tail(path.read_text(encoding="utf-8", errors="replace"), limit=limit)
    except Exception:
        return ""


def print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
