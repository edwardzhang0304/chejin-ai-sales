"""Capture selected WeChat group chats into the shared raw message store."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
for path in (PROJECT_ROOT, APP_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apps.wechat_ai_customer_service.admin_backend.services.recorder_service import RecorderService  # noqa: E402
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root  # noqa: E402
from apps.wechat_ai_customer_service.scripts.run_customer_service_listener import (  # noqa: E402
    apply_operator_command,
    read_operator_control_state,
    sync_operator_mode,
    write_operator_control_state,
)


class RecorderLoopControl:
    """Apply the same RPA guard control commands used by the auto-reply loop."""

    def __init__(
        self,
        path: Path | None,
        *,
        tenant_id: str,
        pause_poll_seconds: float = 0.55,
        status_path: Path | None = None,
    ) -> None:
        self.path = path
        self.tenant_id = tenant_id
        self.status_path = status_path
        self.last_command_id = 0
        self.paused = False
        self.pause_poll_seconds = max(0.12, min(3.0, float(pause_poll_seconds or 0.55)))

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def poll(self) -> str:
        if self.path is None:
            return ""
        control = read_operator_control_state(self.path, tenant_id=self.tenant_id)
        command = control.get("command") if isinstance(control.get("command"), dict) else {}
        try:
            command_id = int(command.get("id") or 0)
        except (TypeError, ValueError):
            command_id = 0
        command_status = str(command.get("status") or "").strip().lower()
        command_action = str(command.get("action") or "").strip().lower()

        if command_id > self.last_command_id and command_status == "pending":
            if command_action == "pause":
                self.paused = True
                control = apply_operator_command(control, action="pause", message="AI智能记录员已暂停。")
                write_operator_control_state(self.path, control)
                write_recorder_runtime_status(
                    self.status_path,
                    tenant_id=self.tenant_id,
                    state="paused",
                    message="AI智能记录员已暂停，等待恢复。",
                )
                self.last_command_id = command_id
                return "pause"
            if command_action == "resume":
                self.paused = False
                control = apply_operator_command(control, action="resume", message="AI智能记录员已恢复。")
                write_operator_control_state(self.path, control)
                write_recorder_runtime_status(
                    self.status_path,
                    tenant_id=self.tenant_id,
                    state="idle",
                    message="AI智能记录员正在运行。",
                )
                self.last_command_id = command_id
                return "resume"
            if command_action == "stop":
                self.paused = False
                control = apply_operator_command(control, action="stop", message="AI智能记录员已停止。")
                write_operator_control_state(self.path, control)
                write_recorder_runtime_status(
                    self.status_path,
                    tenant_id=self.tenant_id,
                    state="stopped",
                    message="AI智能记录员已停止。",
                )
                self.last_command_id = command_id
                return "stop"

        if command_id > self.last_command_id:
            self.last_command_id = command_id

        desired_mode = "paused" if self.paused else "running"
        if str(control.get("mode") or "").strip().lower() != desired_mode and command_status != "pending":
            sync_operator_mode(self.path, tenant_id=self.tenant_id, mode=desired_mode, message="recorder_mode_sync")
        return ""

    def wait_if_paused(self) -> bool:
        while self.paused:
            if self.poll() == "stop":
                return False
            time.sleep(self.pause_poll_seconds)
        return True

    def sleep(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
        while time.monotonic() < deadline:
            if self.poll() == "stop":
                return False
            if not self.wait_if_paused():
                return False
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(self.pause_poll_seconds, remaining))
        return True


def recorder_runtime_status_path(tenant_id: str) -> Path:
    return tenant_runtime_root(tenant_id) / "recorder" / "runtime_status.json"


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def write_recorder_runtime_status(path: Path | None, *, tenant_id: str, state: str, message: str) -> None:
    if path is None:
        return
    payload = read_json(path)
    payload.update(
        {
            "ok": True,
            "state": state if state in {"idle", "thinking", "paused", "stopped"} else "idle",
            "message": message,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "tenant_id": tenant_id,
        }
    )
    atomic_write_json(path, payload)


def write_recorder_runtime_heartbeat(
    path: Path | None,
    *,
    tenant_id: str,
    state: str = "idle",
    message: str = "AI智能记录员正在运行。",
    iteration: int = 0,
    capture_interval_seconds: int = 30,
    runtime_max_runtime_seconds: int = 0,
    result: dict[str, Any] | None = None,
) -> None:
    if path is None:
        return
    payload = read_json(path)
    now_text = datetime.now().isoformat(timespec="seconds")
    payload.update(
        {
            "ok": True,
            "state": state if state in {"idle", "thinking", "paused", "stopped"} else "idle",
            "message": message,
            "updated_at": now_text,
            "tenant_id": tenant_id,
            "heartbeat_at": now_text,
            "last_capture_heartbeat_at": now_text,
            "loop_iteration": max(0, int(iteration or 0)),
            "capture_interval_seconds": max(1, int(capture_interval_seconds or 30)),
            "runtime_max_runtime_seconds": max(0, int(runtime_max_runtime_seconds or 0)),
        }
    )
    if isinstance(result, dict):
        payload["last_capture_summary"] = {
            "ok": bool(result.get("ok")),
            "conversation_count": int(result.get("conversation_count", 0) or 0),
            "inserted_count": int(result.get("inserted_count", 0) or 0),
        }
    atomic_write_json(path, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one capture iteration.")
    parser.add_argument("--forever", action="store_true", help="Run capture loop until process exits.")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--discover", action="store_true", help="Refresh the WeChat session list before capture.")
    parser.add_argument("--notify", action="store_true", help="Send collection notices when enabled for a group.")
    parser.add_argument("--tenant-id", default="", help="Tenant scope for recorder state and storage.")
    parser.add_argument("--operator-control", type=Path, help="RPA operator control file for pause/resume/stop.")
    parser.add_argument("--operator-pause-poll-seconds", type=float, default=0.55)
    args = parser.parse_args()

    tenant_id = active_tenant_id(args.tenant_id or None)
    os.environ["WECHAT_KNOWLEDGE_TENANT"] = tenant_id
    service = RecorderService(tenant_id=tenant_id)
    settings = service.settings()
    interval = max(1, int(args.interval_seconds or 30))
    runtime_max_runtime_seconds = max(0, int(settings.get("runtime_max_runtime_seconds") or 0))
    operator_control = RecorderLoopControl(
        args.operator_control.resolve() if isinstance(args.operator_control, Path) else None,
        tenant_id=tenant_id,
        pause_poll_seconds=float(args.operator_pause_poll_seconds or 0.55),
        status_path=recorder_runtime_status_path(tenant_id),
    )

    if args.forever:
        if args.discover:
            discover_payload = service.discover_sessions()
            print(json.dumps({"kind": "discover", "tenant_id": tenant_id, "result": discover_payload}, ensure_ascii=True), flush=True)
        index = 0
        runtime_started = time.monotonic()
        while True:
            if operator_control.poll() == "stop":
                return 0
            if not operator_control.wait_if_paused():
                return 0
            if runtime_max_runtime_seconds > 0 and time.monotonic() - runtime_started >= runtime_max_runtime_seconds:
                write_recorder_runtime_status(
                    operator_control.status_path,
                    tenant_id=tenant_id,
                    state="stopped",
                    message="AI智能记录员达到运行时长上限，已自动停止。",
                )
                return 0
            index += 1
            loop_started = time.monotonic()
            result = service.capture_selected_once(send_notifications=bool(args.notify))
            write_recorder_runtime_heartbeat(
                operator_control.status_path,
                tenant_id=tenant_id,
                iteration=index,
                capture_interval_seconds=interval,
                runtime_max_runtime_seconds=runtime_max_runtime_seconds,
                result=result,
            )
            print(
                json.dumps(
                    {
                        "kind": "capture",
                        "tenant_id": tenant_id,
                        "iteration": index,
                        "captured_at": datetime.now().isoformat(timespec="seconds"),
                        "result": result,
                    },
                    ensure_ascii=True,
                ),
                flush=True,
            )
            elapsed_seconds = max(0.0, time.monotonic() - loop_started)
            sleep_seconds = max(0.0, float(adjusted_sleep_interval(result, interval)) - elapsed_seconds)
            if not operator_control.sleep(sleep_seconds):
                return 0

    events: list[dict[str, Any]] = []
    if args.discover:
        events.append({"kind": "discover", "tenant_id": tenant_id, "result": service.discover_sessions()})
    iterations = 1 if args.once else max(1, int(args.iterations or 1))
    for index in range(iterations):
        if operator_control.poll() == "stop":
            break
        if not operator_control.wait_if_paused():
            break
        result = service.capture_selected_once(send_notifications=bool(args.notify))
        events.append({"kind": "capture", "tenant_id": tenant_id, "iteration": index + 1, "result": result})
        if index + 1 < iterations:
            if not operator_control.sleep(interval):
                break
    print(json.dumps({"ok": True, "tenant_id": tenant_id, "events": events}, ensure_ascii=True, indent=2))
    return 0


def adjusted_sleep_interval(result: dict[str, Any], base_interval: int) -> int:
    interval = max(1, int(base_interval or 30))
    for item in result.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        recovery = item.get("capture_recovery") if isinstance(item.get("capture_recovery"), dict) else {}
        if recovery.get("gap_risk") or recovery.get("history_load_applied"):
            return min(interval, 5)
    return interval


if __name__ == "__main__":
    raise SystemExit(main())
