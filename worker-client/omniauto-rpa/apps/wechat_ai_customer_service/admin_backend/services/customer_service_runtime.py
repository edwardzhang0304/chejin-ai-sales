"""Runtime process control for the local WeChat customer-service listener."""

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
from apps.wechat_ai_customer_service.cloud_gate import cloud_gate_status, cloud_required_enabled
from apps.wechat_ai_customer_service.customer_service_live_safety import (
    CustomerServiceLiveSafetyError,
    assert_customer_service_recent_bootstrap_guard,
    assert_customer_service_live_safety_guard,
    evaluate_customer_service_live_safety_guard,
)
from apps.wechat_ai_customer_service.admin_backend.services.customer_service_settings import CustomerServiceSettings
from apps.wechat_ai_customer_service.admin_backend.services.wechat_startup_check import run_wechat_startup_self_check
from apps.wechat_ai_customer_service.knowledge_paths import active_tenant_id, tenant_runtime_root
from apps.wechat_ai_customer_service.sync import VpsLocalSyncService


PROJECT_ROOT = Path(__file__).resolve().parents[4]
APP_ROOT = PROJECT_ROOT / "apps" / "wechat_ai_customer_service"
DEFAULT_CHEJIN_TENANT_ID = "chejin"
DEFAULT_CHEJIN_CONFIG = APP_ROOT / "configs" / "jiangsu_chejin_xucong_live.example.json"

RUNTIME_STATES = {"idle", "thinking", "paused", "stopped"}
VOLATILE_WECHAT_STARTUP_DETAILS = {
    "wechat_not_ready",
    "wechat_window_minimized",
    "wechat_receive_unavailable",
    "wechat_send_unavailable",
}
VOLATILE_WECHAT_STARTUP_MESSAGE_TTL_SECONDS = 45
CLOUD_GATE_LOCK_MESSAGE_MARKERS = (
    "云端授权未通过",
    "无法向服务端注册本地节点",
    "无法从服务端刷新共享行业知识库",
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


TRANSIENT_WRITE_WINERRORS = {5, 32, 33}


def transient_write_error(exc: OSError) -> bool:
    """Windows can briefly lock files that are being read by the UI/AV/indexer."""
    winerror = getattr(exc, "winerror", None)
    if winerror in TRANSIENT_WRITE_WINERRORS:
        return True
    errno_value = getattr(exc, "errno", None)
    return errno_value in {13, 16, 32}


def atomic_write_json(path: Path, payload: Any, *, attempts: int = 10) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    for attempt in range(max(1, attempts)):
        temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
            return
        except OSError as exc:
            last_error = exc
            try:
                temp.unlink()
            except OSError:
                pass
            # Cross-process cleanup can momentarily remove the runtime folder.
            # Recreate parent and retry instead of failing the whole scheduler tick.
            if isinstance(exc, FileNotFoundError):
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                if attempt < attempts - 1:
                    time.sleep(0.03 * (attempt + 1))
                    continue
            if not transient_write_error(exc) or attempt >= attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error


def runtime_dir(tenant_id: str | None = None) -> Path:
    return tenant_runtime_root(tenant_id) / "customer_service"


def runtime_status_path(tenant_id: str | None = None) -> Path:
    return runtime_dir(tenant_id) / "runtime_status.json"


def runtime_pid_path(tenant_id: str | None = None) -> Path:
    return runtime_dir(tenant_id) / "listener.pid.json"


def runtime_operator_control_path(tenant_id: str | None = None) -> Path:
    return runtime_dir(tenant_id) / "operator_control.json"


def runtime_operator_guard_pid_path(tenant_id: str | None = None) -> Path:
    return runtime_dir(tenant_id) / "operator_guard.pid.json"


def runtime_operator_guard_state_path(tenant_id: str | None = None) -> Path:
    return runtime_dir(tenant_id) / "operator_guard.state.json"


def worker_pid_path(tenant_id: str | None = None) -> Path:
    return runtime_dir(tenant_id) / "worker.pid.json"


def runtime_log_path(tenant_id: str | None = None) -> Path:
    return tenant_runtime_root(tenant_id) / "logs" / "customer_service_managed_listener.log"


def write_runtime_status(
    state: str,
    message: str = "",
    *,
    tenant_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Write the compact status consumed by the floating console widget."""
    normalized = state if state in RUNTIME_STATES else "idle"
    payload: dict[str, Any] = {
        "ok": True,
        "state": normalized,
        "message": message or status_default_message(normalized),
        "updated_at": now_iso(),
        "tenant_id": active_tenant_id(tenant_id),
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    path = runtime_status_path(tenant_id)
    try:
        atomic_write_json(path, payload)
    except OSError as exc:
        payload.update(
            {
                "ok": False,
                "write_failed": True,
                "write_error": repr(exc),
                "write_path": str(path),
            }
        )
    return payload


def read_runtime_status(tenant_id: str | None = None) -> dict[str, Any]:
    path = runtime_status_path(tenant_id)
    if not path.exists():
        return {
            "ok": True,
            "state": "stopped",
            "message": status_default_message("stopped"),
            "updated_at": "",
            "tenant_id": active_tenant_id(tenant_id),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "ok": False,
            "state": "stopped",
            "message": "状态文件读取失败，请重新启动客服监听。",
            "updated_at": "",
            "tenant_id": active_tenant_id(tenant_id),
        }
    if not isinstance(payload, dict):
        payload = {}
    state = str(payload.get("state") or "stopped")
    if state not in RUNTIME_STATES:
        state = "stopped"
    return {
        **payload,
        "ok": payload.get("ok", True) is not False,
        "state": state,
        "message": str(payload.get("message") or status_default_message(state)),
        "tenant_id": str(payload.get("tenant_id") or active_tenant_id(tenant_id)),
    }


def status_default_message(state: str) -> str:
    return {
        "idle": "自动客服正在运行，当前没有正在处理的消息。",
        "thinking": "自动客服正在读取微信消息或调用大模型。",
        "paused": "自动客服已暂停，等待恢复指令。",
        "stopped": "已停止。",
    }.get(state, "自动客服状态未知。")


def stale_volatile_wechat_startup_failure(status: dict[str, Any], *, now_ts: float | None = None) -> bool:
    """Do not keep showing old recoverable WeChat startup failures forever."""
    if str(status.get("state") or "") != "stopped":
        return False
    check = status.get("wechat_check") if isinstance(status.get("wechat_check"), dict) else {}
    detail = str((check or {}).get("detail") or "")
    if detail not in VOLATILE_WECHAT_STARTUP_DETAILS:
        return False
    updated_at = str(status.get("updated_at") or "")
    if not updated_at:
        return True
    try:
        parsed = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    age = float(now_ts if now_ts is not None else time.time()) - parsed.timestamp()
    return age >= VOLATILE_WECHAT_STARTUP_MESSAGE_TTL_SECONDS


def stale_cloud_gate_lock_message(status: dict[str, Any], gate: dict[str, Any]) -> bool:
    """Hide stale cloud-lock text once the cloud gate has recovered."""
    if str(status.get("state") or "") != "stopped":
        return False
    if not bool((gate or {}).get("ok")):
        return False
    message = str(status.get("message") or "").strip()
    if not message:
        return False
    return any(marker in message for marker in CLOUD_GATE_LOCK_MESSAGE_MARKERS)


def stale_live_guard_lock_message(*, status: dict[str, Any], guard_blocking_now: bool) -> bool:
    """Hide stale live-guard startup failure after settings/config no longer violate the guard."""
    if guard_blocking_now:
        return False
    if str(status.get("state") or "") != "stopped":
        return False
    message = str(status.get("message") or "").strip()
    if not message:
        return False
    return "live safety guard failed:" in message


def summarize_listener_result(result: dict[str, Any]) -> dict[str, Any]:
    events = [item for item in result.get("events", []) or [] if isinstance(item, dict)]
    last_event = events[-1] if events else {}
    synthesis = last_event.get("llm_reply_synthesis") if isinstance(last_event.get("llm_reply_synthesis"), dict) else {}
    rag = last_event.get("rag_reply") if isinstance(last_event.get("rag_reply"), dict) else {}
    evidence_summary = synthesis.get("evidence_summary") if isinstance(synthesis.get("evidence_summary"), dict) else {}
    return {
        "last_action": last_event.get("action"),
        "last_target": last_event.get("target"),
        "last_reason": last_event.get("reason") or synthesis.get("reason") or rag.get("reason"),
        "last_reply_preview": str(((last_event.get("decision") or {}).get("reply_text") if isinstance(last_event.get("decision"), dict) else "") or "")[:180],
        "model_tier": synthesis.get("model_tier"),
        "model": synthesis.get("model"),
        "rag_hit_count": evidence_summary.get("rag_hit_count"),
        "structured_evidence_count": evidence_summary.get("structured_evidence_count"),
    }


class CustomerServiceRuntime:
    """Start/stop/status wrapper for one tenant's managed WeChat listener."""

    def __init__(self, *, tenant_id: str | None = None) -> None:
        self.tenant_id = active_tenant_id(tenant_id)

    def status(self) -> dict[str, Any]:
        pid_record = self._read_pid_record()
        pid = int(pid_record.get("pid") or 0)
        running = self._pid_alive(pid)
        if not running and not pid_record:
            running, scanned_pid = self._scan_listener_running(self.tenant_id)
            if running:
                pid = scanned_pid
                pid_record = {"pid": pid, "tenant_id": self.tenant_id}
        status = read_runtime_status(self.tenant_id)
        gate = cloud_gate_status() if cloud_required_enabled() else {"ok": True, "required": False}
        worker_pid_record = self._read_worker_pid_record()
        worker_pid = int(worker_pid_record.get("pid") or 0)
        worker_running = self._worker_pid_matches_tenant_queue(worker_pid)
        if worker_pid and not worker_running:
            self._clear_worker_pid_record()
        guard_pid_record = self._read_operator_guard_pid_record()
        guard_pid = int(guard_pid_record.get("pid") or 0)
        guard_running = self._pid_alive(guard_pid)
        guard_state = self._read_operator_guard_state() if guard_running else {}
        if not guard_running and guard_pid_record:
            self._clear_operator_guard_runtime_files()
        queue_summary = {}
        try:
            from apps.wechat_ai_customer_service.admin_backend.services.work_queue import WorkQueueService
            queue_summary = WorkQueueService(tenant_id=self.tenant_id).summary()
        except Exception:
            pass
        status.update(
            {
                "running": running,
                "pid": pid if running else None,
                "started_at": pid_record.get("started_at") if running else "",
                "config_path": str(pid_record.get("config_path") or self._config_path_or_empty()),
                "log_path": str(runtime_log_path(self.tenant_id)),
                "cloud_gate": gate,
                "worker_pid": worker_pid if worker_running else None,
                "worker_running": worker_running,
                "operator_guard_pid": guard_pid if guard_running else None,
                "operator_guard_running": guard_running,
                "operator_guard_state": guard_state,
                "queue_summary": queue_summary,
            }
        )
        if not running and pid_record:
            self._clear_pid_record()
        if not running:
            previous_state = str(status.get("state") or "")
            previous_message = str(status.get("message") or "").strip()
            live_guard_stale = stale_live_guard_lock_message(
                status=status,
                guard_blocking_now=self._live_guard_blocking_now(),
            )
            status["state"] = "stopped"
            if (
                previous_state == "stopped"
                and previous_message
                and not stale_volatile_wechat_startup_failure(status)
                and not stale_cloud_gate_lock_message(status, gate)
                and not live_guard_stale
            ):
                status["message"] = previous_message
            else:
                if previous_message:
                    status["stale_message"] = previous_message
                status["message"] = status_default_message("stopped")
        status["other_listeners"] = self._scan_all_listener_tenants()
        return status

    @staticmethod
    def _scan_all_listener_tenants() -> list[dict[str, Any]]:
        """Scan for running listeners across all tenants (dedupe launcher parent/child pairs)."""
        listeners = CustomerServiceRuntime._list_listener_processes()
        return [{"pid": item["pid"], "tenant_id": item["tenant_id"] or "unknown"} for item in listeners]

    def start(self, *, token: str = "") -> dict[str, Any]:
        current = self.status()
        if current.get("running"):
            return {"ok": True, "message": "自动客服已经在运行。", "item": current}
        if cloud_required_enabled():
            sync_service = VpsLocalSyncService()
            register = sync_service.register_node(
                token=token,
                tenant_id=self.tenant_id,
                display_name="Local Customer Service Runtime",
            )
            if register.get("ok") is not True:
                message = "无法向服务端注册本地节点，自动客服已锁定。请恢复服务端连接后重试。"
                write_runtime_status("stopped", message, tenant_id=self.tenant_id)
                return {
                    "ok": False,
                    "message": message,
                    "detail": "cloud_node_register_failed",
                    "sync_result": register,
                    "item": self.status(),
                }
            refresh = sync_service.fetch_shared_knowledge_snapshot(
                token=token,
                tenant_id=self.tenant_id,
                force=True,
            )
            if refresh.get("ok") is not True:
                message = "无法从服务端刷新共享行业知识库，自动客服已锁定。请恢复服务端连接后重试。"
                write_runtime_status("stopped", message, tenant_id=self.tenant_id)
                return {
                    "ok": False,
                    "message": message,
                    "detail": "cloud_snapshot_refresh_failed",
                    "sync_result": refresh,
                    "item": self.status(),
                }
            gate = cloud_gate_status()
            if not gate.get("ok"):
                message = "云端授权未通过，自动客服已锁定。请先连接服务端并刷新共享行业知识库。"
                write_runtime_status("stopped", message, tenant_id=self.tenant_id)
                return {"ok": False, "message": message, "detail": "cloud_authoritative_access_required", "cloud_gate": gate, "item": self.status()}
        try:
            config_path = self._resolve_config_path()
        except FileNotFoundError as exc:
            write_runtime_status("stopped", str(exc), tenant_id=self.tenant_id)
            return {"ok": False, "message": str(exc), "item": self.status()}
        bootstrap_refresh: dict[str, Any] | None = None
        try:
            live_safety_guard = self._validate_live_safety_guard(config_path)
        except CustomerServiceLiveSafetyError as exc:
            if not self._live_safety_failure_allows_bootstrap_refresh(exc.summary):
                return self._live_safety_guard_failed_response(exc.summary, error=exc)
            if not self._startup_auto_bootstrap_refresh_enabled(config_path):
                bootstrap_refresh = self._startup_bootstrap_refresh_skipped_payload(exc.summary)
                return self._live_safety_guard_failed_response(exc.summary, error=exc, bootstrap_refresh=bootstrap_refresh)
            bootstrap_refresh = self._refresh_recent_bootstrap_guard(config_path, token=token)
            if not bootstrap_refresh.get("ok"):
                return self._live_safety_guard_failed_response(exc.summary, error=exc, bootstrap_refresh=bootstrap_refresh)
            try:
                live_safety_guard = self._validate_live_safety_guard(config_path)
            except CustomerServiceLiveSafetyError as second_exc:
                return self._live_safety_guard_failed_response(
                    second_exc.summary,
                    error=second_exc,
                    bootstrap_refresh=bootstrap_refresh,
                )
            live_safety_guard = dict(live_safety_guard)
            live_safety_guard["bootstrap_refresh"] = bootstrap_refresh
        listener_interval = self._managed_listener_interval_seconds(config_path)
        script_path = APP_ROOT / "scripts" / "run_customer_service_listener.py"
        if not script_path.exists():
            return {"ok": False, "message": f"缺少监听脚本：{script_path}", "item": current}
        log_path = runtime_log_path(self.tenant_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        wxauto_update = self._auto_update_wxauto4()
        wechat_check = self._wechat_startup_self_check(wxauto_update=wxauto_update)
        if not wechat_check.get("ok"):
            return {
                "ok": False,
                "message": str(wechat_check.get("message") or "微信启动前自检未通过。"),
                "detail": str(wechat_check.get("detail") or "wechat_startup_check_failed"),
                "wxauto_update": wxauto_update,
                "wechat_check": wechat_check,
                "item": self.status(),
            }
        env = dict(os.environ)
        env["WECHAT_KNOWLEDGE_TENANT"] = self.tenant_id
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if token:
            env["WECHAT_RUNTIME_SYNC_TOKEN"] = token
        write_runtime_status(
            "thinking",
            "正在启动微信自动客服监听。",
            tenant_id=self.tenant_id,
            wxauto_update=wxauto_update,
            wechat_check=wechat_check,
        )
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [
                str(self._python_executable()),
                str(script_path),
                "--tenant-id",
                self.tenant_id,
                "--config",
                str(config_path),
                "--interval-seconds",
                f"{listener_interval:g}",
                "--send",
                "--write-data",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._write_pid_record(
            {
                "pid": proc.pid,
                "tenant_id": self.tenant_id,
                "config_path": str(config_path),
                "started_at": now_iso(),
                "log_path": str(log_path),
                "wxauto_update": wxauto_update,
                "wechat_check": wechat_check,
                "live_safety_guard": live_safety_guard,
            }
        )
        # Start background worker
        worker_result = self._start_worker()
        time.sleep(0.8)
        result = {"ok": True, "message": "自动客服监听已启动。", "item": self.status()}
        result["wxauto_update"] = wxauto_update
        result["wechat_check"] = wechat_check
        if not worker_result.get("ok"):
            result["worker_warning"] = worker_result.get("message", "worker start warning")
        result["live_safety_guard"] = live_safety_guard
        pending_visible = live_safety_guard.get("pending_visible_targets") if isinstance(live_safety_guard, dict) else {}
        if pending_visible:
            result["pending_visible_warning"] = "当前不在可见会话列表里，待收到新消息时会自动识别"
        return result

    def _live_safety_guard_failed_response(
        self,
        summary: dict[str, Any],
        *,
        error: Exception,
        bootstrap_refresh: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(bootstrap_refresh, dict) and bootstrap_refresh.get("reason") == "startup_auto_bootstrap_refresh_disabled":
            message = "微信自动客服启动前安全护栏未通过：会话识别基线已过期。为避免启动时自动操作微信导致白屏或风控，请先在网页端重新识别并确认会话后再启动。"
        else:
            message = f"微信自动客服启动前安全护栏未通过：{error}"
        payload = dict(summary)
        if bootstrap_refresh is not None:
            payload["bootstrap_refresh"] = bootstrap_refresh
        write_runtime_status("stopped", message, tenant_id=self.tenant_id, live_safety_guard=payload)
        return {
            "ok": False,
            "message": message,
            "detail": "live_safety_guard_failed",
            "live_safety_guard": payload,
            "item": self.status(),
        }

    @staticmethod
    def _live_safety_failure_allows_bootstrap_refresh(summary: dict[str, Any]) -> bool:
        reasons = {str(item) for item in summary.get("fail_reasons", []) or [] if str(item)}
        return bool(reasons) and reasons <= {"recent_bootstrap_missing", "recent_bootstrap_stale"}

    def _startup_auto_bootstrap_refresh_enabled(self, config_path: Path) -> bool:
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        guard = payload.get("live_safety_guard") if isinstance(payload.get("live_safety_guard"), dict) else {}
        if not isinstance(guard, dict):
            return False
        value = guard.get("startup_auto_bootstrap_refresh_enabled")
        if value is None:
            value = guard.get("allow_startup_auto_bootstrap_refresh")
        return self._truthy_flag(value, default=True)

    @staticmethod
    def _startup_bootstrap_refresh_skipped_payload(summary: dict[str, Any]) -> dict[str, Any]:
        targets = summary.get("allowed_targets") or summary.get("targets") or []
        return {
            "ok": False,
            "reason": "startup_auto_bootstrap_refresh_disabled",
            "status": "skipped",
            "safe_manual_action_required": True,
            "targets": list(targets) if isinstance(targets, list) else [],
            "guidance": "recent bootstrap is stale or missing; re-identify/confirm sessions from the UI before starting live RPA",
        }

    def _refresh_recent_bootstrap_guard(self, config_path: Path, *, token: str = "") -> dict[str, Any]:
        workflow = APP_ROOT / "workflows" / "listen_and_reply.py"
        if not workflow.exists():
            return {"ok": False, "reason": "workflow_missing", "workflow": str(workflow)}
        bootstrap_targets = self._bootstrap_refresh_targets(config_path)
        timeout_seconds = max(20.0, min(180.0, float(os.getenv("WECHAT_BOOTSTRAP_REFRESH_TIMEOUT_SECONDS") or "90")))
        env = dict(os.environ)
        env["WECHAT_KNOWLEDGE_TENANT"] = self.tenant_id
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["WECHAT_WIN32_OCR_TARGET_SEARCH_FALLBACK"] = "0"
        env["WECHAT_WIN32_OCR_TARGET_SEARCH_ENTER_FALLBACK"] = "0"
        if token:
            env["WECHAT_RUNTIME_SYNC_TOKEN"] = token
        write_runtime_status(
            "thinking",
            "正在刷新微信自动客服启动安全基线。",
            tenant_id=self.tenant_id,
            live_safety_guard={"bootstrap_refresh": {"ok": None, "status": "running"}},
        )
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        command = [
            str(self._python_executable()),
            str(workflow),
            "--config",
            str(config_path),
            "--once",
            "--bootstrap",
        ]
        for target in bootstrap_targets:
            command.extend(["--target", target])
        started = time.time()
        try:
            completed = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "reason": "bootstrap_refresh_timeout",
                "timeout_seconds": timeout_seconds,
                "stdout_tail": self._tail_text(exc.stdout),
                "stderr_tail": self._tail_text(exc.stderr),
            }
        except Exception as exc:
            return {"ok": False, "reason": "bootstrap_refresh_exception", "error": repr(exc)}
        parsed = self._parse_last_json(completed.stdout)
        verified = self._verify_bootstrap_refresh_result(parsed, bootstrap_targets)
        ok = completed.returncode == 0 and bool(verified.get("ok", True))
        return {
            "ok": ok,
            "reason": "" if ok else str(verified.get("reason") or "bootstrap_refresh_failed"),
            "returncode": completed.returncode,
            "duration_seconds": round(time.time() - started, 3),
            "targets": bootstrap_targets,
            "verified": verified,
            "stdout_tail": self._tail_text(completed.stdout),
            "stderr_tail": self._tail_text(completed.stderr),
        }

    def _bootstrap_refresh_targets(self, config_path: Path) -> list[str]:
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        guard = payload.get("live_safety_guard") if isinstance(payload.get("live_safety_guard"), dict) else {}
        raw_targets = guard.get("allowed_targets") or guard.get("targets") or []
        targets = self._dedupe_names(raw_targets if isinstance(raw_targets, list) else [])
        if targets:
            return targets
        raw_config_targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []
        return self._dedupe_names(
            [
                (item or {}).get("name") or (item or {}).get("target_name")
                for item in raw_config_targets
                if isinstance(item, dict) and item.get("enabled", True) is not False
            ]
        )

    @staticmethod
    def _dedupe_names(raw_values: list[Any]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for raw in raw_values:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            names.append(name)
            seen.add(name)
        return names

    @staticmethod
    def _parse_last_json(text: str) -> dict[str, Any]:
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload
        for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return {}

    @staticmethod
    def _verify_bootstrap_refresh_result(payload: dict[str, Any], expected_targets: list[str]) -> dict[str, Any]:
        if not expected_targets:
            return {"ok": True, "reason": "no_explicit_bootstrap_targets"}
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return {"ok": False, "reason": "bootstrap_refresh_output_not_ok", "bootstrapped_targets": []}
        events = payload.get("events") if isinstance(payload.get("events"), list) else []
        bootstrapped = {
            str(event.get("target") or "").strip()
            for event in events
            if isinstance(event, dict) and str(event.get("action") or "") == "bootstrapped"
        }
        pending_visible = {
            str(event.get("target") or "").strip()
            for event in events
            if isinstance(event, dict) and str(event.get("action") or "") == "pending_visible"
        }
        accepted = bootstrapped | pending_visible
        missing = [target for target in expected_targets if target not in accepted]
        return {
            "ok": not missing,
            "reason": "" if not missing else "bootstrap_refresh_target_not_bootstrapped",
            "bootstrapped_targets": sorted(bootstrapped),
            "pending_visible_targets": sorted(pending_visible),
            "missing_targets": missing,
            "event_count": len(events),
        }

    @staticmethod
    def _tail_text(value: Any, *, limit: int = 1600) -> str:
        if value is None:
            return ""
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        return text[-limit:]

    @staticmethod
    def _truthy_flag(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", ""}

    def _synchronize_settings_with_live_guard(
        self,
        config: dict[str, Any],
        *,
        settings_service: CustomerServiceSettings,
        settings: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        guard = config.get("live_safety_guard")
        if not isinstance(guard, dict) or not self._truthy_flag(guard.get("enabled"), default=False):
            return settings, {"changed": False, "reason": "guard_disabled"}
        raw_allowed_targets = guard.get("allowed_targets") or guard.get("targets") or []
        allowed_targets = self._dedupe_names(raw_allowed_targets if isinstance(raw_allowed_targets, list) else [])
        if not allowed_targets:
            return settings, {"changed": False, "reason": "allowed_targets_missing"}
        allowed_set = set(allowed_targets)
        require_exact = self._truthy_flag(guard.get("require_exact_targets"), default=True)
        disable_respond_all = self._truthy_flag(guard.get("disable_respond_all_unread_sessions"), default=True)

        raw_targets = settings.get("session_targets") if isinstance(settings.get("session_targets"), list) else []
        by_name: dict[str, dict[str, Any]] = {}
        for raw in raw_targets:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or raw.get("target_name") or raw.get("display_name") or "").strip()
            if not name:
                continue
            item = dict(raw)
            item["name"] = name
            item["display_name"] = str(item.get("display_name") or name).strip() or name
            item["exact"] = bool(item.get("exact", True))
            item["enabled"] = bool(item.get("enabled", False))
            item["archived"] = bool(item.get("archived", False))
            item["source"] = str(item.get("source") or "manual").strip() or "manual"
            item["conversation_type"] = str(item.get("conversation_type") or "unknown").strip() or "unknown"
            item["updated_at"] = str(item.get("updated_at") or now_iso()).strip() or now_iso()
            by_name[name] = item

        changed = False
        inserted_targets: list[str] = []
        reenabled_targets: list[str] = []
        disabled_targets: list[str] = []
        tick = now_iso()

        for name in allowed_targets:
            item = by_name.get(name)
            if item is None:
                by_name[name] = {
                    "name": name,
                    "display_name": name,
                    "enabled": True,
                    "exact": True,
                    "archived": False,
                    "archived_at": "",
                    "archived_reason": "",
                    "conversation_type": "unknown",
                    "source": "guard_sync",
                    "updated_at": tick,
                }
                inserted_targets.append(name)
                changed = True
                continue
            item_changed = False
            if not bool(item.get("enabled", False)):
                item["enabled"] = True
                reenabled_targets.append(name)
                item_changed = True
            if bool(item.get("archived", False)):
                item["archived"] = False
                item["archived_at"] = ""
                item["archived_reason"] = ""
                item_changed = True
            if not bool(item.get("exact", True)):
                item["exact"] = True
                item_changed = True
            display_name = str(item.get("display_name") or "").strip()
            if not display_name:
                item["display_name"] = name
                item_changed = True
            if not str(item.get("source") or "").strip():
                item["source"] = "guard_sync"
                item_changed = True
            if item_changed:
                item["updated_at"] = tick
                changed = True
            by_name[name] = item

        if require_exact:
            for name, item in by_name.items():
                if name in allowed_set or not bool(item.get("enabled", False)):
                    continue
                item["enabled"] = False
                item["updated_at"] = tick
                disabled_targets.append(name)
                changed = True

        patch: dict[str, Any] = {}
        if not bool(settings.get("session_targets_managed", False)):
            patch["session_targets_managed"] = True
            changed = True
        if disable_respond_all and bool(settings.get("respond_all_unread_sessions", False)):
            patch["respond_all_unread_sessions"] = False
            changed = True
        if not changed:
            return settings, {"changed": False, "reason": "already_aligned", "allowed_targets": allowed_targets}

        patch["session_targets"] = list(by_name.values())
        updated = settings_service.save(patch)
        summary = {
            "changed": True,
            "allowed_targets": allowed_targets,
            "inserted_targets": inserted_targets,
            "reenabled_targets": reenabled_targets,
            "disabled_targets": disabled_targets,
            "respond_all_unread_sessions": bool(updated.get("respond_all_unread_sessions", False)),
            "session_targets_managed": bool(updated.get("session_targets_managed", False)),
        }
        return updated, summary

    @staticmethod
    def _enabled_managed_session_target_names(settings: dict[str, Any]) -> list[str]:
        if not bool((settings or {}).get("session_targets_managed", False)):
            return []
        names: list[str] = []
        for raw in (settings or {}).get("session_targets", []) or []:
            if (
                not isinstance(raw, dict)
                or not bool(raw.get("enabled", False))
                or bool(raw.get("archived", False))
            ):
                continue
            name = str(raw.get("name") or raw.get("target_name") or raw.get("display_name") or "").strip()
            if name:
                names.append(name)
        deduped: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            deduped.append(name)
        return deduped

    def _sync_live_guard_allowed_targets_from_settings(
        self,
        *,
        config_path: Path,
        payload: dict[str, Any],
        settings: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        guard = payload.get("live_safety_guard") if isinstance(payload.get("live_safety_guard"), dict) else {}
        if not guard or not self._truthy_flag(guard.get("enabled"), default=False):
            return payload, {"changed": False, "reason": "guard_disabled"}
        prefer_settings_targets = self._truthy_flag(
            guard.get("sync_allowed_targets_from_settings"),
            default=self._truthy_flag(guard.get("customer_experience_first"), default=True),
        )
        if not prefer_settings_targets:
            return payload, {"changed": False, "reason": "guard_settings_sync_disabled"}
        enabled_targets = self._enabled_managed_session_target_names(settings)
        if not enabled_targets:
            return payload, {"changed": False, "reason": "no_enabled_managed_targets"}
        current_allowed = self._dedupe_names(
            (guard.get("allowed_targets") or guard.get("targets") or [])
            if isinstance(guard.get("allowed_targets") or guard.get("targets") or [], list)
            else []
        )
        if current_allowed == enabled_targets:
            return payload, {"changed": False, "reason": "already_aligned", "allowed_targets": current_allowed}

        updated_payload = dict(payload)
        updated_guard = dict(guard)
        updated_guard["allowed_targets"] = list(enabled_targets)
        updated_guard["targets"] = list(enabled_targets)
        updated_payload["live_safety_guard"] = updated_guard

        raw_targets = updated_payload.get("targets") if isinstance(updated_payload.get("targets"), list) else []
        by_name: dict[str, dict[str, Any]] = {}
        for raw in raw_targets:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or raw.get("target_name") or "").strip()
            if not name:
                continue
            item = dict(raw)
            item["name"] = name
            by_name[name] = item
        for name in enabled_targets:
            item = by_name.get(name) or {"name": name}
            item["name"] = name
            item["enabled"] = True
            item["exact"] = True
            if name != "文件传输助手":
                item["allow_self_for_test"] = False
            by_name[name] = item
        for name, item in by_name.items():
            if name not in set(enabled_targets):
                item["enabled"] = False
        updated_payload["targets"] = sorted(
            list(by_name.values()),
            key=lambda item: (0 if bool(item.get("enabled", False)) else 1, str(item.get("name") or "")),
        )

        write_error = ""
        try:
            atomic_write_json(config_path, updated_payload)
        except OSError as exc:
            write_error = repr(exc)
        summary: dict[str, Any] = {
            "changed": True,
            "reason": "aligned_from_settings",
            "allowed_targets": enabled_targets,
            "previous_allowed_targets": current_allowed,
            "config_written": not bool(write_error),
        }
        if write_error:
            summary["write_error"] = write_error
        return updated_payload, summary

    def _auto_update_wxauto4(self) -> dict[str, Any]:
        if not wxauto4_module_update_enabled():
            return WxautoPackageManager().auto_update_on_wechat_module_start()
        write_runtime_status("thinking", "正在检查 wxauto4 技术储备更新（包含 beta/rc 预发布版）。", tenant_id=self.tenant_id)
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
        if result.get("updated"):
            message = "wxauto4 技术储备已自动更新（包含预发布更新策略），正在启动微信自动客服监听。"
        elif result.get("ok"):
            message = "wxauto4 技术储备已是当前可用版本（已检查 beta/rc），正在启动微信自动客服监听。"
        else:
            message = "wxauto4 技术储备更新检查失败，将继续使用 RPA 优先模式启动。"
        write_runtime_status("thinking", message, tenant_id=self.tenant_id, wxauto_update=result)
        return result

    def _wechat_startup_self_check(self, *, wxauto_update: dict[str, Any]) -> dict[str, Any]:
        write_runtime_status(
            "thinking",
            "正在自检微信登录状态和当前可用适配方案。",
            tenant_id=self.tenant_id,
            wxauto_update=wxauto_update,
        )
        check = run_wechat_startup_self_check(require_send=True, module_name="微信自动客服")
        target_state = "thinking" if check.get("ok") else "stopped"
        write_runtime_status(
            target_state,
            str(check.get("message") or "微信启动前自检完成。"),
            tenant_id=self.tenant_id,
            wxauto_update=wxauto_update,
            wechat_check=check,
        )
        return check

    def _validate_live_safety_guard(self, config_path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"enabled": False, "ok": True, "fail_reasons": ["config_unreadable"]}
        settings_service = CustomerServiceSettings(tenant_id=self.tenant_id)
        settings = settings_service.get()
        payload, guard_target_sync = self._sync_live_guard_allowed_targets_from_settings(
            config_path=config_path,
            payload=payload,
            settings=settings,
        )
        if guard_target_sync.get("changed"):
            settings = settings_service.get()
        settings, sync_summary = self._synchronize_settings_with_live_guard(
            payload,
            settings_service=settings_service,
            settings=settings,
        )
        summary = assert_customer_service_live_safety_guard(payload, settings=settings)
        if sync_summary.get("changed"):
            summary = dict(summary)
            summary["settings_sync"] = sync_summary
        if guard_target_sync.get("changed") or guard_target_sync.get("write_error"):
            summary = dict(summary)
            summary["guard_target_sync"] = guard_target_sync
        state = self._load_listener_state(payload)
        bootstrap_summary = assert_customer_service_recent_bootstrap_guard(payload, state=state)
        if bootstrap_summary.get("enabled"):
            summary = dict(summary)
            summary["bootstrap_guard"] = bootstrap_summary
        return summary

    def _live_guard_blocking_now(self) -> bool:
        try:
            config_path = self._resolve_config_path()
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        settings = CustomerServiceSettings(tenant_id=self.tenant_id).get()
        guard = payload.get("live_safety_guard") if isinstance(payload.get("live_safety_guard"), dict) else {}
        if guard and self._truthy_flag(guard.get("enabled"), default=False):
            prefer_settings_targets = self._truthy_flag(
                guard.get("sync_allowed_targets_from_settings"),
                default=self._truthy_flag(guard.get("customer_experience_first"), default=True),
            )
            if prefer_settings_targets:
                enabled_targets = self._enabled_managed_session_target_names(settings)
                if enabled_targets:
                    updated_payload = dict(payload)
                    updated_guard = dict(guard)
                    updated_guard["allowed_targets"] = list(enabled_targets)
                    updated_guard["targets"] = list(enabled_targets)
                    updated_payload["live_safety_guard"] = updated_guard
                    payload = updated_payload
        summary = evaluate_customer_service_live_safety_guard(payload, settings=settings)
        return bool(summary.get("enabled")) and not bool(summary.get("ok"))

    def _load_listener_state(self, config: dict[str, Any]) -> dict[str, Any]:
        raw_path = str((config or {}).get("state_path") or "").strip()
        if not raw_path:
            return {}
        path = Path(raw_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _managed_listener_interval_seconds(self, config_path: Path) -> float:
        default_interval = 3.0
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default_interval
        poll = payload.get("poll") if isinstance(payload, dict) else {}
        try:
            interval = float(
                (poll or {}).get("interval_min_seconds")
                or (poll or {}).get("min_interval_seconds")
                or (poll or {}).get("interval_seconds", default_interval)
            )
        except (TypeError, ValueError):
            interval = default_interval
        return max(0.5, min(10.0, interval))

    def stop(self) -> dict[str, Any]:
        pid_record = self._read_pid_record()
        pid = int(pid_record.get("pid") or 0)
        if pid and self._pid_alive(pid):
            self._terminate_tree(pid)
        self._stop_worker()
        self._stop_ocr_sidecars()
        self._shutdown_operator_guard()
        write_runtime_status("stopped", "已停止。", tenant_id=self.tenant_id)
        self._clear_pid_record()
        return {"ok": True, "message": "已停止。", "item": self.status()}

    def _config_path_or_empty(self) -> str:
        try:
            return str(self._resolve_config_path())
        except FileNotFoundError:
            return ""

    def _resolve_config_path(self) -> Path:
        candidates = [
            runtime_dir(self.tenant_id) / "listener_config.json",
            APP_ROOT / "configs" / f"{self.tenant_id}.json",
            APP_ROOT / "configs" / f"{self.tenant_id}.example.json",
        ]
        if self.tenant_id == DEFAULT_CHEJIN_TENANT_ID:
            candidates.append(DEFAULT_CHEJIN_CONFIG)
        for path in candidates:
            if path.exists():
                return path
        raise FileNotFoundError(
            "当前客户账号还没有配置微信监听目标。请先为该账号创建 listener_config.json，或在后台为该账号完成微信自动客服配置。"
        )

    def _python_executable(self) -> Path:
        override = str(os.getenv("WECHAT_RUNTIME_PYTHON") or "").strip()
        if override:
            override_path = Path(override)
            if override_path.exists():
                return override_path
        current = Path(sys.executable)
        if current.exists():
            return current
        venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        if venv_python.exists():
            return venv_python
        return current

    def _read_pid_record(self) -> dict[str, Any]:
        path = runtime_pid_path(self.tenant_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_operator_guard_pid_record(self) -> dict[str, Any]:
        path = runtime_operator_guard_pid_path(self.tenant_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_operator_guard_state(self) -> dict[str, Any]:
        path = runtime_operator_guard_state_path(self.tenant_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_pid_record(self, payload: dict[str, Any]) -> None:
        path = runtime_pid_path(self.tenant_id)
        atomic_write_json(path, payload)

    def _clear_pid_record(self) -> None:
        try:
            runtime_pid_path(self.tenant_id).unlink()
        except FileNotFoundError:
            pass

    def _clear_operator_guard_runtime_files(self) -> None:
        for path in (
            runtime_operator_guard_pid_path(self.tenant_id),
            runtime_operator_guard_state_path(self.tenant_id),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _shutdown_operator_guard(self) -> None:
        pid_record = self._read_operator_guard_pid_record()
        guard_pid = int(pid_record.get("pid") or 0)
        if guard_pid and self._pid_alive(guard_pid):
            self._terminate_tree(guard_pid)
        self._clear_operator_guard_runtime_files()

    _LISTENER_SCRIPT_NAME = "run_customer_service_listener.py"
    _WORKER_SCRIPT_NAME = "background_worker.py"
    _OCR_SIDECAR_SCRIPT_NAME = "wechat_win32_ocr_sidecar.py"

    @staticmethod
    def _cmdline_has_script(cmdline: list[str], script_name: str) -> bool:
        target = script_name
        for arg in cmdline:
            arg_norm = os.path.normpath(str(arg))
            if os.path.basename(arg_norm) == target:
                return True
        return False

    @staticmethod
    def _cmdline_has_listener_script(cmdline: list[str]) -> bool:
        """Return True if cmdline actually executes the listener script (not just mentions it)."""
        return CustomerServiceRuntime._cmdline_has_script(cmdline, CustomerServiceRuntime._LISTENER_SCRIPT_NAME)

    @staticmethod
    def _scan_listener_running(tenant_id: str) -> tuple[bool, int]:
        """Fallback: scan process list for a running listener matching tenant_id."""
        for listener in CustomerServiceRuntime._list_listener_processes():
            if listener.get("tenant_id") == tenant_id:
                return True, int(listener.get("pid") or 0)
        return False, 0

    @staticmethod
    def _extract_tenant_from_cmdline(cmdline: list[str]) -> str:
        for index, part in enumerate(cmdline):
            text = str(part)
            if text == "--tenant-id" and index + 1 < len(cmdline):
                return str(cmdline[index + 1]).strip()
            if text.startswith("--tenant-id="):
                return text.split("=", 1)[1].strip()
        return ""

    @staticmethod
    def _list_listener_processes() -> list[dict[str, Any]]:
        my_pid = os.getpid()
        candidates: dict[int, dict[str, Any]] = {}
        for proc in psutil.process_iter(["pid", "ppid", "cmdline", "name"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                cmdline = [str(item) for item in (proc.info.get("cmdline") or [])]
                name = str(proc.info.get("name") or "").lower()
                if pid <= 0 or pid == my_pid or "python" not in name:
                    continue
                if not CustomerServiceRuntime._cmdline_has_listener_script(cmdline):
                    continue
                if not CustomerServiceRuntime._pid_alive(pid):
                    continue
                candidates[pid] = {
                    "pid": pid,
                    "ppid": int(proc.info.get("ppid") or 0),
                    "tenant_id": CustomerServiceRuntime._extract_tenant_from_cmdline(cmdline),
                    "cmdline": cmdline,
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not candidates:
            return []
        child_map: dict[int, list[dict[str, Any]]] = {}
        for info in candidates.values():
            parent = int(info.get("ppid") or 0)
            if parent in candidates:
                child_map.setdefault(parent, []).append(info)
        effective: list[dict[str, Any]] = []
        for info in candidates.values():
            children = child_map.get(int(info["pid"]), [])
            if not children:
                effective.append(info)
                continue
            parent_tenant = str(info.get("tenant_id") or "")
            has_same_listener_child = any(
                (not parent_tenant or parent_tenant == str(child.get("tenant_id") or ""))
                and CustomerServiceRuntime._cmdline_has_listener_script(child.get("cmdline") or [])
                for child in children
            )
            if not has_same_listener_child:
                effective.append(info)
        effective.sort(key=lambda item: int(item.get("pid") or 0))
        return effective

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

    # --- Background worker lifecycle ---

    def _read_worker_pid_record(self) -> dict[str, Any]:
        path = worker_pid_path(self.tenant_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _clear_worker_pid_record(self) -> None:
        try:
            worker_pid_path(self.tenant_id).unlink()
        except FileNotFoundError:
            pass

    def _worker_pid_matches_tenant_queue(self, pid: int) -> bool:
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
        if not self._cmdline_has_script(cmdline, self._WORKER_SCRIPT_NAME):
            return False
        return (
            self._cmdline_option_equals(cmdline, "--tenant-id", self.tenant_id)
            and self._cmdline_option_equals(cmdline, "--queue", "customer_service")
        )

    def _start_worker(self) -> dict[str, Any]:
        worker_record = self._read_worker_pid_record()
        existing_pid = int(worker_record.get("pid") or 0)
        if existing_pid and self._worker_pid_matches_tenant_queue(existing_pid):
            return {"ok": True, "message": "后台 worker 已经在运行。"}
        if existing_pid:
            self._clear_worker_pid_record()
        scanned_worker_pids = self._scan_worker_running_pids(self.tenant_id)
        if scanned_worker_pids:
            worker_pid = max(scanned_worker_pids)
            path = worker_pid_path(self.tenant_id)
            atomic_write_json(
                path,
                {
                    "pid": worker_pid,
                    "tenant_id": self.tenant_id,
                    "queue": "customer_service",
                    "started_at": now_iso(),
                },
            )
            return {"ok": True, "message": "后台 worker 已经在运行。", "worker_pid": worker_pid}
        script_path = APP_ROOT / "scripts" / "background_worker.py"
        if not script_path.exists():
            return {"ok": False, "message": f"缺少后台 worker 脚本：{script_path}"}
        env = dict(os.environ)
        env["WECHAT_KNOWLEDGE_TENANT"] = self.tenant_id
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [
                str(self._python_executable()),
                str(script_path),
                "--tenant-id",
                self.tenant_id,
                "--queue",
                "customer_service",
                "--interval-seconds",
                "5",
                "--limit",
                "3",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        time.sleep(0.3)
        return {"ok": True, "message": "后台 worker 已启动。", "worker_pid": proc.pid}

    def _stop_worker(self) -> None:
        worker_record = self._read_worker_pid_record()
        pids = set(self._scan_worker_running_pids(self.tenant_id))
        pid = int(worker_record.get("pid") or 0)
        if pid and self._worker_pid_matches_tenant_queue(pid):
            pids.add(pid)
        for worker_pid in sorted(pids):
            if worker_pid and self._pid_alive(worker_pid):
                self._terminate_tree(worker_pid)
        self._clear_worker_pid_record()

    @staticmethod
    def _scan_worker_running_pids(tenant_id: str) -> list[int]:
        my_pid = os.getpid()
        pids: set[int] = set()
        for proc in psutil.process_iter(["pid", "cmdline", "name"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                cmdline = proc.info.get("cmdline") or []
                name = str(proc.info.get("name") or "").lower()
                if pid <= 0 or pid == my_pid or "python" not in name:
                    continue
                if not CustomerServiceRuntime._cmdline_has_script(cmdline, CustomerServiceRuntime._WORKER_SCRIPT_NAME):
                    continue
                if not CustomerServiceRuntime._cmdline_option_equals(cmdline, "--tenant-id", tenant_id):
                    continue
                if not CustomerServiceRuntime._cmdline_option_equals(cmdline, "--queue", "customer_service"):
                    continue
                if CustomerServiceRuntime._pid_alive(pid):
                    pids.add(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return sorted(pids)

    def _stop_ocr_sidecars(self) -> None:
        stop_wechat_ocr_sidecars()

    @staticmethod
    def _scan_ocr_sidecar_daemon_pids() -> list[int]:
        my_pid = os.getpid()
        pids: set[int] = set()
        for proc in psutil.process_iter(["pid", "cmdline", "name"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                cmdline = [str(item) for item in (proc.info.get("cmdline") or [])]
                name = str(proc.info.get("name") or "").lower()
                if pid <= 0 or pid == my_pid or "python" not in name:
                    continue
                if "--daemon" not in cmdline:
                    continue
                if not CustomerServiceRuntime._cmdline_has_script(cmdline, CustomerServiceRuntime._OCR_SIDECAR_SCRIPT_NAME):
                    continue
                if not CustomerServiceRuntime._sidecar_script_belongs_to_project(cmdline):
                    continue
                if CustomerServiceRuntime._pid_alive(pid):
                    pids.add(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return sorted(pids)

    @staticmethod
    def _sidecar_script_belongs_to_project(cmdline: list[str]) -> bool:
        script_name = CustomerServiceRuntime._OCR_SIDECAR_SCRIPT_NAME
        for arg in cmdline:
            arg_path = Path(str(arg))
            if arg_path.name != script_name:
                continue
            if not arg_path.is_absolute():
                return True
            try:
                resolved = arg_path.resolve()
            except OSError:
                return False
            if resolved == APP_ROOT / "adapters" / script_name:
                return True
            if APP_ROOT in resolved.parents:
                return True
        return False

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


def stop_customer_service_support_processes(
    tenant_id: str | None = None,
    *,
    include_operator_guard: bool = True,
) -> None:
    """Best-effort cleanup used by both web stop and F8 emergency stop."""

    runtime = CustomerServiceRuntime(tenant_id=tenant_id)
    runtime._stop_worker()
    runtime._stop_ocr_sidecars()
    if include_operator_guard:
        runtime._shutdown_operator_guard()


def stop_wechat_ocr_sidecars() -> None:
    """Stop project-owned OCR daemon processes left behind by RPA loops."""

    for sidecar_pid in CustomerServiceRuntime._scan_ocr_sidecar_daemon_pids():
        if sidecar_pid and CustomerServiceRuntime._pid_alive(sidecar_pid):
            CustomerServiceRuntime._terminate_tree(sidecar_pid)
