"""Runtime process control for the AI smart recorder capture loop."""

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

from apps.wechat_ai_customer_service.adapters.wechat_connector import reset_wxauto_sidecar_daemon
from apps.wechat_ai_customer_service.adapters.wxauto_package_manager import WxautoPackageManager, wxauto4_module_update_enabled
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_runtime import atomic_write_json, stop_wechat_ocr_sidecars
from apps.wechat_ai_customer_service.admin_backend.services.recorder_service import RecorderService
from apps.wechat_ai_customer_service.admin_backend.services.wechat_startup_check import run_wechat_startup_self_check
from apps.wechat_ai_customer_service.admin_backend.services.work_queue import WorkQueueService
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_logs_root, tenant_runtime_root
from apps.wechat_ai_customer_service.scripts.run_customer_service_listener import (
    clear_file,
    empty_operator_control_state,
    launch_operator_guard,
    normalize_operator_guard_settings,
    read_operator_guard_pid,
    sync_operator_mode,
    verify_operator_guard_bootstrap,
    write_operator_control_state,
)


PROJECT_ROOT = Path(__file__).resolve().parents[4]
APP_ROOT = PROJECT_ROOT / "apps" / "wechat_ai_customer_service"
RECORDER_SCRIPT_NAME = "recorder_loop.py"
WORKER_SCRIPT_NAME = "background_worker.py"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_iso_timestamp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def recorder_runtime_dir(tenant_id: str | None = None) -> Path:
    return tenant_runtime_root(tenant_id) / "recorder"


def recorder_runtime_status_path(tenant_id: str | None = None) -> Path:
    return recorder_runtime_dir(tenant_id) / "runtime_status.json"


def recorder_runtime_pid_path(tenant_id: str | None = None) -> Path:
    return recorder_runtime_dir(tenant_id) / "loop.pid.json"


def recorder_worker_pid_path(tenant_id: str | None = None) -> Path:
    return recorder_runtime_dir(tenant_id) / "worker.pid.json"


def recorder_runtime_log_path(tenant_id: str | None = None) -> Path:
    return tenant_runtime_logs_root(tenant_id) / "recorder_managed_loop.log"


def recorder_operator_control_path(tenant_id: str | None = None) -> Path:
    return recorder_runtime_dir(tenant_id) / "operator_control.json"


def recorder_operator_guard_pid_path(tenant_id: str | None = None) -> Path:
    return recorder_runtime_dir(tenant_id) / "operator_guard.pid.json"


def recorder_operator_guard_state_path(tenant_id: str | None = None) -> Path:
    return recorder_runtime_dir(tenant_id) / "operator_guard.state.json"


class RecorderRuntime:
    """Start/stop/status wrapper for one tenant recorder loop + export worker."""

    def __init__(self, *, tenant_id: str | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)

    def status(self) -> dict[str, Any]:
        settings = RecorderService(tenant_id=self.tenant_id).settings()
        pid_record = self._read_pid_record()
        pid = int(pid_record.get("pid") or 0)
        running = self._pid_alive(pid)
        scanned = set(self._scan_loop_running_pids(self.tenant_id))
        if running:
            scanned.add(pid)
        elif scanned:
            pid = max(scanned)
            running = True
        duplicate_loop_pids = self._independent_loop_root_pids(scanned, keep_pid=pid)

        worker_record = self._read_worker_pid_record()
        worker_pid = int(worker_record.get("pid") or 0)
        worker_running = self._worker_pid_matches_tenant_queue(worker_pid, queue_name="recorder_exports")
        if worker_pid and not worker_running:
            self._clear_worker_pid_record()
        if not worker_running and not worker_record:
            scanned_workers = self._scan_worker_running_pids(self.tenant_id, queue_name="recorder_exports")
            if scanned_workers:
                worker_pid = max(scanned_workers)
                worker_running = True

        guard_pid_record = self._read_operator_guard_pid_record()
        guard_pid = int(guard_pid_record.get("pid") or 0)
        guard_running = self._pid_alive(guard_pid)
        guard_state = self._read_operator_guard_state() if guard_running else {}
        if not guard_running and guard_pid_record:
            self._clear_operator_guard_runtime_files()
        status_payload = self._read_status_payload()
        queue_summary = WorkQueueService(tenant_id=self.tenant_id).summary()
        if not running:
            previous_state = str(status_payload.get("state") or "")
            previous_message = str(status_payload.get("message") or "").strip()
            status_payload["state"] = "stopped"
            if previous_state == "stopped" and previous_message:
                status_payload["message"] = previous_message
            else:
                status_payload["message"] = "AI智能记录员监听已停止。"
        liveness = self._evaluate_runtime_liveness(status_payload=status_payload, settings=settings, running=running)
        if running and not liveness["ok"]:
            status_payload["message"] = "AI智能记录员正在运行，但活性心跳异常，请检查微信窗口或重启监听。"
        status_payload.update(
            {
                "running": running,
                "pid": pid if running else None,
                "duplicate_loop_pids": duplicate_loop_pids,
                "duplicate_loop_count": len(duplicate_loop_pids),
                "worker_running": worker_running,
                "worker_pid": worker_pid if worker_running else None,
                "operator_guard_running": guard_running,
                "operator_guard_pid": guard_pid if guard_running else None,
                "operator_guard_state": guard_state,
                "queue_summary": queue_summary,
                "log_path": str(recorder_runtime_log_path(self.tenant_id)),
                "tenant_id": self.tenant_id,
                "liveness_ok": liveness["ok"],
                "liveness_reason": liveness["reason"],
                "liveness_age_seconds": liveness["age_seconds"],
                "liveness_interval_seconds": liveness["interval_seconds"],
                "liveness_stale_seconds": liveness["stale_seconds"],
                "heartbeat_at": liveness["heartbeat_at"],
                "runtime_max_runtime_seconds": liveness["runtime_max_runtime_seconds"],
            }
        )
        return status_payload

    def start(self) -> dict[str, Any]:
        current = self.status()
        if current.get("running"):
            deduped = self._dedupe_loop_processes(keep_pid=int(current.get("pid") or 0))
            message = "AI智能记录员已经在运行。"
            if deduped:
                message = f"AI智能记录员已经在运行，已清理重复监听进程：{deduped}。"
            return {"ok": True, "message": message, "deduped_loop_pids": deduped, "item": self.status()}

        settings = RecorderService(tenant_id=self.tenant_id).settings()
        if settings.get("enabled", True) is False:
            self._write_status_payload(
                {
                    "ok": True,
                    "state": "stopped",
                    "message": "AI智能记录员总开关已关闭，请先开启后再启动监听。",
                    "updated_at": now_iso(),
                    "tenant_id": self.tenant_id,
                }
            )
            return {
                "ok": False,
                "detail": "recorder_disabled",
                "message": "AI智能记录员总开关已关闭，请先在记录员设置中开启。",
                "item": self.status(),
            }

        recorder_script = APP_ROOT / "workflows" / "recorder_loop.py"
        if not recorder_script.exists():
            return {"ok": False, "message": f"缺少记录员脚本：{recorder_script}", "item": current}
        interval = max(5, int(settings.get("capture_interval_seconds", 30) or 30))
        wxauto_update = self._auto_update_wxauto4()
        wechat_check = self._wechat_startup_self_check(wxauto_update=wxauto_update)
        if not wechat_check.get("ok"):
            return {
                "ok": False,
                "detail": str(wechat_check.get("detail") or "wechat_startup_check_failed"),
                "message": str(wechat_check.get("message") or "微信启动前自检未通过。"),
                "wxauto_update": wxauto_update,
                "wechat_check": wechat_check,
                "item": self.status(),
            }

        env = dict(os.environ)
        env["WECHAT_KNOWLEDGE_TENANT"] = self.tenant_id
        env["PYTHONUTF8"] = "1"
        # Recorder must be able to re-raise WeChat even after the user sends it
        # behind other windows. Keep this scoped to the recorder subprocess.
        env.setdefault("WECHAT_WIN32_OCR_AGGRESSIVE_FOCUS", str(os.getenv("WECHAT_RECORDER_AGGRESSIVE_FOCUS") or "1"))
        env.setdefault("WECHAT_WIN32_OCR_ATTACH_THREAD_INPUT", str(os.getenv("WECHAT_RECORDER_ATTACH_THREAD_INPUT") or "1"))
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log_path = recorder_runtime_log_path(self.tenant_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        operator_guard_settings = self._operator_guard_settings(settings)
        operator_pause_poll_seconds = max(
            0.12,
            float(operator_guard_settings.get("pause_poll_interval_ms") or 550) / 1000.0,
        )
        log_handle = log_path.open("ab")
        try:
            proc = subprocess.Popen(
                [
                    str(self._python_executable()),
                    str(recorder_script),
                    "--tenant-id",
                    self.tenant_id,
                    "--forever",
                    "--interval-seconds",
                    str(interval),
                    "--discover",
                    "--operator-control",
                    str(recorder_operator_control_path(self.tenant_id)),
                    "--operator-pause-poll-seconds",
                    str(operator_pause_poll_seconds),
                ],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        finally:
            log_handle.close()
        self._write_pid_record(
            {
                "pid": proc.pid,
                "tenant_id": self.tenant_id,
                "script": str(recorder_script),
                "started_at": now_iso(),
                "interval_seconds": interval,
                "log_path": str(log_path),
                "wxauto_update": wxauto_update,
                "wechat_check": wechat_check,
            }
        )
        operator_guard = self._launch_operator_guard_for_loop(parent_pid=proc.pid, settings=settings)
        if operator_guard.get("ok") is not True:
            self._terminate_tree(proc.pid)
            self._clear_pid_record()
            message = "AI智能记录员 RPA 防误触守护未就绪，已停止启动。请检查悬浮球/键鼠拦截状态。"
            self._write_status_payload(
                {
                    "ok": True,
                    "state": "stopped",
                    "message": message,
                    "updated_at": now_iso(),
                    "tenant_id": self.tenant_id,
                    "operator_guard": operator_guard,
                    "wxauto_update": wxauto_update,
                    "wechat_check": wechat_check,
                }
            )
            return {
                "ok": False,
                "detail": "operator_guard_not_ready",
                "message": message,
                "operator_guard": operator_guard,
                "item": self.status(),
            }
        deduped: list[int] = []
        self._write_status_payload(
            {
                "ok": True,
                "state": "idle",
                "message": "AI智能记录员正在运行。",
                "updated_at": now_iso(),
                "tenant_id": self.tenant_id,
                "last_start_at": now_iso(),
                "wxauto_update": wxauto_update,
                "wechat_check": wechat_check,
                "operator_guard": operator_guard,
                "deduped_loop_pids": deduped,
            }
        )
        return {
            "ok": True,
            "message": "AI智能记录员已启动。",
            "wxauto_update": wxauto_update,
            "wechat_check": wechat_check,
            "operator_guard": operator_guard,
            "deduped_loop_pids": deduped,
            "item": self.status(),
        }

    def _auto_update_wxauto4(self) -> dict[str, Any]:
        if not wxauto4_module_update_enabled():
            return WxautoPackageManager().auto_update_on_wechat_module_start()
        self._write_status_payload(
            {
                "ok": True,
                "state": "thinking",
                "message": "正在检查 wxauto4 技术储备更新（包含 beta/rc 预发布版）。",
                "updated_at": now_iso(),
                "tenant_id": self.tenant_id,
            }
        )
        try:
            result = WxautoPackageManager().auto_update_on_wechat_module_start()
        except Exception as exc:
            result = {
                "ok": False,
                "enabled": True,
                "updated": False,
                "package": "wxauto4",
                "reason": "update_check_exception",
                "error": repr(exc),
            }
        if result.get("updated"):
            reset_wxauto_sidecar_daemon()
            message = "wxauto4 技术储备已自动更新（包含预发布更新策略），正在启动 AI智能记录员。"
        elif result.get("ok"):
            message = "wxauto4 技术储备已是当前可用版本（已检查 beta/rc），正在启动 AI智能记录员。"
        else:
            message = "wxauto4 技术储备更新检查失败，将继续使用 RPA 优先模式启动。"
        self._write_status_payload(
            {
                "ok": True,
                "state": "thinking",
                "message": message,
                "updated_at": now_iso(),
                "tenant_id": self.tenant_id,
                "wxauto_update": result,
            }
        )
        return result

    def _wechat_startup_self_check(self, *, wxauto_update: dict[str, Any]) -> dict[str, Any]:
        self._write_status_payload(
            {
                "ok": True,
                "state": "thinking",
                "message": "正在自检微信登录状态和当前可用适配方案。",
                "updated_at": now_iso(),
                "tenant_id": self.tenant_id,
                "wxauto_update": wxauto_update,
            }
        )
        check = run_wechat_startup_self_check(require_send=False, module_name="AI智能记录员")
        self._write_status_payload(
            {
                "ok": True,
                "state": "thinking" if check.get("ok") else "stopped",
                "message": str(check.get("message") or "微信启动前自检完成。"),
                "updated_at": now_iso(),
                "tenant_id": self.tenant_id,
                "wxauto_update": wxauto_update,
                "wechat_check": check,
            }
        )
        return check

    def _operator_guard_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        source = settings.get("rpa_operator_guard") if isinstance(settings.get("rpa_operator_guard"), dict) else {}
        merged = {"enabled": True, **dict(source)}
        if os.getenv("WECHAT_RECORDER_OPERATOR_GUARD_ENABLED") is not None:
            merged["enabled"] = str(os.getenv("WECHAT_RECORDER_OPERATOR_GUARD_ENABLED") or "").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
        return normalize_operator_guard_settings(merged)

    def _launch_operator_guard_for_loop(self, *, parent_pid: int, settings: dict[str, Any]) -> dict[str, Any]:
        guard_settings = self._operator_guard_settings(settings)
        if os.name != "nt" or not guard_settings.get("enabled"):
            return {"ok": True, "enabled": False, "settings": guard_settings}
        control_path = recorder_operator_control_path(self.tenant_id)
        pid_path = recorder_operator_guard_pid_path(self.tenant_id)
        state_path = recorder_operator_guard_state_path(self.tenant_id)
        clear_file(state_path)
        clear_file(pid_path)
        write_operator_control_state(
            control_path,
            empty_operator_control_state(self.tenant_id, mode="running"),
        )
        launch = launch_operator_guard(
            tenant_id=self.tenant_id,
            settings=guard_settings,
            control_path=control_path,
            status_path=recorder_runtime_status_path(self.tenant_id),
            state_path=state_path,
            pid_path=pid_path,
            parent_pid=parent_pid,
            local_safety_stop_path="/api/recorder/runtime/stop",
        )
        if launch.get("ok") is not True:
            return {"ok": False, "enabled": True, "settings": guard_settings, "launch": launch}
        verify = verify_operator_guard_bootstrap(
            int(launch.get("pid") or 0),
            state_path,
            timeout_seconds=float(guard_settings.get("bootstrap_timeout_seconds") or 15.0),
            expected_parent_pid=parent_pid,
        )
        result = {"ok": verify.get("ok") is True, "enabled": True, "settings": guard_settings, "launch": launch, "verify": verify}
        if result["ok"]:
            try:
                state_pid = int(verify.get("state_pid") or 0)
            except (TypeError, ValueError):
                state_pid = 0
            if state_pid > 0:
                atomic_write_json(
                    pid_path,
                    {
                        "pid": state_pid,
                        "launcher_pid": int(launch.get("pid") or 0),
                        "tenant_id": self.tenant_id,
                        "started_at": now_iso(),
                        "control_path": str(control_path),
                        "status_path": str(recorder_runtime_status_path(self.tenant_id)),
                        "state_path": str(state_path),
                        "parent_pid": parent_pid,
                    },
                )
        else:
            self._shutdown_operator_guard("recorder_operator_guard_verify_failed")
        return result

    def _shutdown_operator_guard(self, reason: str) -> None:
        control_path = recorder_operator_control_path(self.tenant_id)
        pid_path = recorder_operator_guard_pid_path(self.tenant_id)
        try:
            sync_operator_mode(control_path, tenant_id=self.tenant_id, mode="stopped", message=reason)
        except Exception:
            pass
        guard_pid = read_operator_guard_pid(pid_path)
        if guard_pid > 0 and self._pid_alive(guard_pid):
            self._terminate_tree(guard_pid)
        self._clear_operator_guard_runtime_files()

    def _clear_operator_guard_runtime_files(self) -> None:
        clear_file(recorder_operator_guard_pid_path(self.tenant_id))
        clear_file(recorder_operator_guard_state_path(self.tenant_id))

    def _dedupe_loop_processes(self, *, keep_pid: int) -> list[int]:
        running_pids = set(self._scan_loop_running_pids(self.tenant_id))
        if keep_pid and self._pid_alive(keep_pid) and not running_pids:
            running_pids.add(keep_pid)
        if not running_pids:
            return []
        keeper = self._preferred_loop_keeper(running_pids, keep_pid=keep_pid)
        extras = self._independent_loop_root_pids(running_pids, keep_pid=keeper)
        for item in extras:
            if self._pid_alive(item):
                self._terminate_tree(item)
        existing_record = self._read_pid_record()
        if int(existing_record.get("pid") or 0) != keeper:
            existing_record = {}
        self._write_pid_record(
            {
                **existing_record,
                "pid": keeper,
                "tenant_id": self.tenant_id,
                "script": str(APP_ROOT / "workflows" / "recorder_loop.py"),
                "started_at": str(existing_record.get("started_at") or now_iso()),
                "log_path": str(recorder_runtime_log_path(self.tenant_id)),
            }
        )
        return extras

    @staticmethod
    def _preferred_loop_keeper(running_pids: set[int], *, keep_pid: int) -> int:
        if keep_pid in running_pids:
            return keep_pid
        root_pids = [pid for pid in running_pids if RecorderRuntime._loop_parent_pid(pid) not in running_pids]
        return max(root_pids or running_pids)

    @staticmethod
    def _independent_loop_root_pids(running_pids: set[int], *, keep_pid: int) -> list[int]:
        extras: list[int] = []
        for pid in sorted(running_pids):
            if pid == keep_pid:
                continue
            if RecorderRuntime._loop_related(pid, keep_pid):
                continue
            parent_pid = RecorderRuntime._loop_parent_pid(pid)
            if parent_pid in running_pids and parent_pid != keep_pid:
                continue
            extras.append(pid)
        return extras

    @staticmethod
    def _loop_parent_pid(pid: int) -> int:
        try:
            return int(psutil.Process(pid).ppid())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0

    @staticmethod
    def _loop_related(left_pid: int, right_pid: int) -> bool:
        if not left_pid or not right_pid:
            return False
        return RecorderRuntime._loop_parent_pid(left_pid) == right_pid or RecorderRuntime._loop_parent_pid(right_pid) == left_pid

    def stop(self) -> dict[str, Any]:
        self._shutdown_operator_guard("recorder_manual_stop")
        loop_pids = set()
        pid_record = self._read_pid_record()
        pid = int(pid_record.get("pid") or 0)
        if pid:
            loop_pids.add(pid)
        loop_pids.update(self._scan_loop_running_pids(self.tenant_id))
        for item in sorted(loop_pids):
            if item and self._pid_alive(item):
                self._terminate_tree(item)
        stop_wechat_ocr_sidecars()

        worker_pids = set()
        worker_record = self._read_worker_pid_record()
        worker_pid = int(worker_record.get("pid") or 0)
        if worker_pid and self._worker_pid_matches_tenant_queue(worker_pid, queue_name="recorder_exports"):
            worker_pids.add(worker_pid)
        worker_pids.update(self._scan_worker_running_pids(self.tenant_id, queue_name="recorder_exports"))
        for item in sorted(worker_pids):
            if item and self._pid_alive(item):
                self._terminate_tree(item)

        self._clear_operator_guard_runtime_files()
        self._clear_pid_record()
        self._clear_worker_pid_record()
        self._write_status_payload(
            {
                "ok": True,
                "state": "stopped",
                "message": "AI智能记录员监听已手动停止。",
                "updated_at": now_iso(),
                "tenant_id": self.tenant_id,
            }
        )
        return {"ok": True, "message": "AI智能记录员监听已停止。", "item": self.status()}

    def ensure_export_worker(self) -> dict[str, Any]:
        """Ensure recorder export worker process is alive for async export runs."""
        result = self._ensure_worker_running()
        return {
            **result,
            "item": self.status(),
        }

    def _ensure_worker_running(self) -> dict[str, Any]:
        current = self._read_worker_pid_record()
        pid = int(current.get("pid") or 0)
        if pid and self._worker_pid_matches_tenant_queue(pid, queue_name="recorder_exports"):
            return {"ok": True, "message": "导出 worker 已在运行。", "worker_pid": pid}
        if pid:
            self._clear_worker_pid_record()
        scanned = self._scan_worker_running_pids(self.tenant_id, queue_name="recorder_exports")
        if scanned:
            worker_pid = max(scanned)
            self._write_worker_pid_record(
                {
                    "pid": worker_pid,
                    "tenant_id": self.tenant_id,
                    "queue": "recorder_exports",
                    "started_at": now_iso(),
                }
            )
            return {"ok": True, "message": "导出 worker 已在运行。", "worker_pid": worker_pid}

        worker_script = APP_ROOT / "scripts" / "background_worker.py"
        if not worker_script.exists():
            return {"ok": False, "message": f"缺少后台 worker 脚本：{worker_script}"}

        env = dict(os.environ)
        env["WECHAT_KNOWLEDGE_TENANT"] = self.tenant_id
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [
                str(self._python_executable()),
                str(worker_script),
                "--tenant-id",
                self.tenant_id,
                "--queue",
                "recorder_exports",
                "--interval-seconds",
                "5",
                "--limit",
                "2",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        time.sleep(0.3)
        self._write_worker_pid_record(
            {
                "pid": proc.pid,
                "tenant_id": self.tenant_id,
                "queue": "recorder_exports",
                "started_at": now_iso(),
            }
        )
        return {"ok": True, "message": "导出 worker 已启动。", "worker_pid": proc.pid}

    def _read_pid_record(self) -> dict[str, Any]:
        path = recorder_runtime_pid_path(self.tenant_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_pid_record(self, payload: dict[str, Any]) -> None:
        path = recorder_runtime_pid_path(self.tenant_id)
        atomic_write_json(path, payload)

    def _clear_pid_record(self) -> None:
        try:
            recorder_runtime_pid_path(self.tenant_id).unlink()
        except FileNotFoundError:
            pass

    def _read_worker_pid_record(self) -> dict[str, Any]:
        path = recorder_worker_pid_path(self.tenant_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_operator_guard_pid_record(self) -> dict[str, Any]:
        path = recorder_operator_guard_pid_path(self.tenant_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_operator_guard_state(self) -> dict[str, Any]:
        path = recorder_operator_guard_state_path(self.tenant_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_worker_pid_record(self, payload: dict[str, Any]) -> None:
        path = recorder_worker_pid_path(self.tenant_id)
        atomic_write_json(path, payload)

    def _clear_worker_pid_record(self) -> None:
        try:
            recorder_worker_pid_path(self.tenant_id).unlink()
        except FileNotFoundError:
            pass

    def _worker_pid_matches_tenant_queue(self, pid: int, *, queue_name: str) -> bool:
        if pid <= 0 or not self._pid_alive(pid):
            return False
        try:
            proc = psutil.Process(pid)
            name = str(proc.name() or "").lower()
            cmdline = [str(item) for item in (proc.cmdline() or [])]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        if "python" not in name:
            return False
        if not self._cmdline_has_script(cmdline, WORKER_SCRIPT_NAME):
            return False
        return (
            self._cmdline_option_equals(cmdline, "--tenant-id", self.tenant_id)
            and self._cmdline_option_equals(cmdline, "--queue", queue_name)
        )

    def _read_status_payload(self) -> dict[str, Any]:
        path = recorder_runtime_status_path(self.tenant_id)
        if not path.exists():
            return {
                "ok": True,
                "state": "stopped",
                "message": "AI智能记录员监听已停止。",
                "updated_at": "",
                "tenant_id": self.tenant_id,
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        state = str(payload.get("state") or "stopped")
        if state not in {"idle", "thinking", "paused", "stopped"}:
            state = "stopped"
        return {
            **payload,
            "ok": payload.get("ok", True) is not False,
            "state": state,
            "message": str(payload.get("message") or "AI智能记录员状态未知。"),
            "tenant_id": str(payload.get("tenant_id") or self.tenant_id),
        }

    def _write_status_payload(self, payload: dict[str, Any]) -> None:
        path = recorder_runtime_status_path(self.tenant_id)
        atomic_write_json(path, payload)

    def _evaluate_runtime_liveness(
        self,
        *,
        status_payload: dict[str, Any],
        settings: dict[str, Any],
        running: bool,
    ) -> dict[str, Any]:
        interval_seconds = max(15, min(3600, int(settings.get("runtime_liveness_interval_seconds") or 60)))
        stale_seconds = max(
            interval_seconds * 3,
            min(86400, int(settings.get("runtime_liveness_stale_seconds") or 240)),
        )
        runtime_max_runtime_seconds = max(0, int(settings.get("runtime_max_runtime_seconds") or 0))
        heartbeat_at = str(
            status_payload.get("heartbeat_at")
            or status_payload.get("last_capture_heartbeat_at")
            or status_payload.get("updated_at")
            or ""
        ).strip()
        if str(status_payload.get("state") or "").strip().lower() == "paused":
            return {
                "ok": True,
                "reason": "paused",
                "age_seconds": 0,
                "interval_seconds": interval_seconds,
                "stale_seconds": stale_seconds,
                "heartbeat_at": heartbeat_at,
                "runtime_max_runtime_seconds": runtime_max_runtime_seconds,
            }
        if not running:
            return {
                "ok": False,
                "reason": "not_running",
                "age_seconds": None,
                "interval_seconds": interval_seconds,
                "stale_seconds": stale_seconds,
                "heartbeat_at": heartbeat_at,
                "runtime_max_runtime_seconds": runtime_max_runtime_seconds,
            }
        heartbeat_ts = parse_iso_timestamp(heartbeat_at)
        if heartbeat_ts is None:
            return {
                "ok": False,
                "reason": "heartbeat_missing",
                "age_seconds": None,
                "interval_seconds": interval_seconds,
                "stale_seconds": stale_seconds,
                "heartbeat_at": heartbeat_at,
                "runtime_max_runtime_seconds": runtime_max_runtime_seconds,
            }
        age_seconds = max(0, int(time.time() - heartbeat_ts))
        return {
            "ok": age_seconds <= stale_seconds,
            "reason": "ok" if age_seconds <= stale_seconds else "heartbeat_stale",
            "age_seconds": age_seconds,
            "interval_seconds": interval_seconds,
            "stale_seconds": stale_seconds,
            "heartbeat_at": heartbeat_at,
            "runtime_max_runtime_seconds": runtime_max_runtime_seconds,
        }

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            return psutil.Process(pid).is_running()
        except psutil.NoSuchProcess:
            return False

    @staticmethod
    def _terminate_tree(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        try:
            os.kill(pid, 15)
        except OSError:
            pass

    @staticmethod
    def _cmdline_has_script(cmdline: list[str], script_name: str) -> bool:
        for arg in cmdline:
            normalized = os.path.normpath(str(arg))
            if os.path.basename(normalized) == script_name:
                return True
        return False

    @staticmethod
    def _scan_loop_running_pids(tenant_id: str) -> list[int]:
        my_pid = os.getpid()
        pids: set[int] = set()
        for proc in psutil.process_iter(["pid", "cmdline", "name"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                cmdline = [str(item) for item in (proc.info.get("cmdline") or [])]
                name = str(proc.info.get("name") or "").lower()
                if pid <= 0 or pid == my_pid or "python" not in name:
                    continue
                if not RecorderRuntime._cmdline_has_script(cmdline, RECORDER_SCRIPT_NAME):
                    continue
                if not RecorderRuntime._cmdline_option_equals(cmdline, "--tenant-id", tenant_id):
                    continue
                if RecorderRuntime._pid_alive(pid):
                    pids.add(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return sorted(pids)

    @staticmethod
    def _scan_worker_running_pids(tenant_id: str, *, queue_name: str) -> list[int]:
        my_pid = os.getpid()
        pids: set[int] = set()
        for proc in psutil.process_iter(["pid", "cmdline", "name"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                cmdline = [str(item) for item in (proc.info.get("cmdline") or [])]
                name = str(proc.info.get("name") or "").lower()
                if pid <= 0 or pid == my_pid or "python" not in name:
                    continue
                if not RecorderRuntime._cmdline_has_script(cmdline, WORKER_SCRIPT_NAME):
                    continue
                if not RecorderRuntime._cmdline_option_equals(cmdline, "--tenant-id", tenant_id):
                    continue
                if not RecorderRuntime._cmdline_option_equals(cmdline, "--queue", queue_name):
                    continue
                if RecorderRuntime._pid_alive(pid):
                    pids.add(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return sorted(pids)

    @staticmethod
    def _cmdline_option_equals(cmdline: list[str], option: str, expected: str) -> bool:
        expected = str(expected)
        prefix = f"{option}="
        for index, part in enumerate(cmdline):
            text = str(part)
            if text == option and index + 1 < len(cmdline):
                if str(cmdline[index + 1]) == expected:
                    return True
                continue
            if text.startswith(prefix) and text[len(prefix):] == expected:
                return True
        return False

    @staticmethod
    def _python_executable() -> Path:
        override = str(os.getenv("WECHAT_RUNTIME_PYTHON") or "").strip()
        if override:
            override_path = Path(override)
            if override_path.exists():
                return override_path
        current = Path(sys.executable)
        if current.exists():
            return current
        venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        return venv_python if venv_python.exists() else current
