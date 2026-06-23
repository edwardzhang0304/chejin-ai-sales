from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .api import ApiError, WorkerApiClient
from .config import CONFIG
from .models import Binding, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from .rpa_bridge import RpaBridge
from .storage import append_log, save_binding
from .ui_lock import UiLockError, UiLockLease, acquire_ui_lock, force_recover_stale_lock, lock_summary
from .wechat_c2 import build_message_ingest_payload, build_scan_result_payload


ENV_STOP_ERRORS = {
    "RPA_COMPONENT_NOT_READY",
    "WECHAT_WINDOW_NOT_FOUND",
    "ACCOUNT_RESTRICTED",
    "OPERATION_TOO_FREQUENT",
    "NETWORK_ERROR",
    "RPA_SIDECAR_TIMEOUT",
    "RPA_SIDECAR_PROTOCOL_INVALID",
    "RPA_SIDECAR_CRASHED",
    "WORKER_INTERRUPTED",
    "OTHER",
}


class TaskRunner:
    def __init__(
        self,
        api: WorkerApiClient,
        bridge: RpaBridge,
        *,
        on_profile: Callable[[WorkerProfile], None],
        on_status: Callable[[str], None],
        on_step: Callable[[RpaStep], None],
        on_task: Callable[[Task | None], None],
        on_result: Callable[[RpaResult | None], None],
        on_error: Callable[[str], None],
        can_pull_tasks: Callable[[], bool] | None = None,
    ) -> None:
        self.api = api
        self.bridge = bridge
        self.on_profile = on_profile
        self.on_status = on_status
        self.on_step = on_step
        self.on_task = on_task
        self.on_result = on_result
        self.on_error = on_error
        self.can_pull_tasks = can_pull_tasks or (lambda: True)
        self.binding: Binding | None = None
        self.current_task: Task | None = None
        self.current_step: str | None = None
        self.current_ui_lock: UiLockLease | None = None
        self.c2_stats: dict[str, Any] = {
            "last_scan_at": None,
            "last_scan_sessions": 0,
            "last_bound_count": 0,
            "last_message_read_at": None,
            "last_ingested_count": 0,
            "last_error": None,
        }
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.task_lock = threading.Lock()
        self.heartbeat_interval_seconds = CONFIG.heartbeat_interval_seconds
        self.poll_interval_seconds = CONFIG.poll_interval_seconds
        self.last_c2_scan_at = 0.0
        self.last_c2_read_at = 0.0

    def start(self, binding: Binding) -> None:
        self.binding = binding
        self.stop_event.clear()
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="CheJinWorkerTaskRunner", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def set_run_status(self, run_status: str) -> None:
        if not self.binding:
            return
        self.binding.run_status = run_status  # type: ignore[assignment]
        save_binding(self.binding)
        try:
            profile = self.api.set_run_status(self.binding, run_status)
            self.on_profile(profile)
            append_log("INFO", "run_status_changed", "开始接单。" if run_status == "running" else "暂停接单。")
        except Exception as exc:
            self.on_error(str(exc))

    def _loop(self) -> None:
        append_log("INFO", "client_started", "Worker 客户端任务循环启动。")
        while not self.stop_event.is_set():
            self.tick_once()
            self.stop_event.wait(self.poll_interval_seconds)

    def tick_once(self) -> None:
        binding = self.binding
        if not binding:
            return
        rpa_status, wechat_status = self.bridge.probe()
        local_lock = lock_summary()
        try:
            profile = self.api.heartbeat(
                binding,
                running_status="running" if self.current_task else "idle",
                current_task=self.current_task.id if self.current_task else None,
                rpa_component_status=rpa_status,
                wechat_status=wechat_status,
                current_step=self.current_step,
                local_lock_summary=local_lock,
            )
            self.on_profile(profile)
            self.on_status("online")
        except ApiError as exc:
            if exc.status_code == 401:
                self.on_status("invalid")
                self.on_error("绑定已失效，请重新绑定。")
                append_log("ERROR", "binding_invalid", str(exc), error_code=exc.code)
                return
            self.on_status("offline")
            self.on_error(str(exc))
            append_log("ERROR", "heartbeat_failed", str(exc), error_code=exc.code)
            return
        except Exception as exc:
            self.on_status("offline")
            self.on_error(str(exc))
            append_log("ERROR", "heartbeat_failed", str(exc))
            return

        if (
            binding.run_status == "running"
            and rpa_status == "ready"
            and wechat_status == "logged_in"
            and not self.current_task
            and self.can_pull_tasks()
        ):
            self._run_c2_once(binding)
            self._pull_and_execute(binding)

    def _pull_and_execute(self, binding: Binding) -> None:
        try:
            mode, task, reason = self.api.pull_task(binding)
        except Exception as exc:
            append_log("ERROR", "task_pull_failed", str(exc))
            self.on_error(str(exc))
            return
        if not task:
            self.on_task(None)
            if reason and reason != "NO_PENDING_TASK":
                self.on_error(reason)
            return
        with self.task_lock:
            self._execute_task(binding, task, mode)

    def _execute_task(self, binding: Binding, task: Task, mode: str) -> None:
        self.current_task = task
        self.on_task(task)
        self.on_result(None)
        append_log("INFO", "task_recovered" if mode == "running" else "task_pulled", f"准备执行任务 {task.id}", task_id=task.id)
        try:
            running_task = task if mode == "running" else self.api.claim_task(binding, task)
            if not running_task.search_phone and task.search_phone:
                running_task.phone = task.phone
            if not running_task.wechat and task.wechat:
                running_task.wechat = task.wechat
            if not running_task.verify_message and task.verify_message:
                running_task.verify_message = task.verify_message
            if not running_task.remark_name and task.remark_name:
                running_task.remark_name = task.remark_name
            if not running_task.remark_code and task.remark_code:
                running_task.remark_code = task.remark_code
            if running_task.remark_code_valid is None and task.remark_code_valid is not None:
                running_task.remark_code_valid = task.remark_code_valid
            self.current_task = running_task
            self.on_task(running_task)
            result = self._run_add_friend_with_ui_lock(binding, running_task)
            if result.ok:
                completed = (
                    self.api.complete_already_friend(binding, running_task.id)
                    if result.result_code == "already_friend"
                    else self.api.complete_invite_sent(binding, running_task.id)
                )
                if result.evidence_path or result.evidence_metadata:
                    self._upload_evidence_best_effort(
                        binding,
                        running_task.id,
                        result.message,
                        evidence_path=result.evidence_path,
                        metadata=result.evidence_metadata,
                    )
                self.on_task(None)
                self.on_result(result)
                append_log("INFO", "task_completed", result.message, task_id=completed.id)
                return
            failed = self.api.fail_task(binding, running_task.id, result.error_code or "OTHER", result.failure_step, result.message)
            self._upload_evidence_best_effort(
                binding,
                running_task.id,
                result.message,
                error_code=result.error_code,
                evidence_path=result.evidence_path,
                metadata=result.evidence_metadata,
            )
            self.on_task(None)
            self.on_result(result)
            append_log("ERROR", "task_failed", result.message, task_id=failed.id, error_code=result.error_code)
            if result.error_code in ENV_STOP_ERRORS:
                binding.run_status = "paused"
                save_binding(binding)
                self.api.set_run_status(binding, "paused")
                self.on_error("运行环境异常，已暂停接单。")
        except Exception as exc:
            self.on_error(str(exc))
            append_log("ERROR", "task_execute_failed", str(exc), task_id=task.id)
        finally:
            self.current_step = None
            self.current_task = None

    def _run_add_friend_with_ui_lock(self, binding: Binding, task: Task) -> RpaResult:
        owner = f"{binding.worker_id}:{binding.client_instance_id}:add_friend:{task.id}"
        try:
            force_recover_stale_lock(reason="before_add_friend")
            lease = acquire_ui_lock(operation_type="add_friend", owner=owner, current_step="add_friend_starting")
            lease.start_auto_renew()
            self.current_ui_lock = lease
            self.current_step = "add_friend_starting"
            append_log("INFO", "ui_lock_acquired", "已获取微信 UI 锁，开始执行加好友。", task_id=task.id, metadata={"lock_id": lease.lock_id, "fencing_token": lease.fencing_token})
        except UiLockError as exc:
            append_log("ERROR", "ui_lock_acquire_failed", str(exc), task_id=task.id, error_code=exc.code, metadata=exc.data)
            return RpaResult(ok=False, error_code=exc.code, failure_step="ui_lock_acquire", message=str(exc), evidence_metadata={"ui_lock": exc.data})
        try:
            return self.bridge.run_add_friend(task, lambda step: self._report_step(binding, task, step))
        except UiLockError as exc:
            append_log("ERROR", "ui_lock_runtime_failed", str(exc), task_id=task.id, error_code=exc.code, metadata=exc.data)
            return RpaResult(ok=False, error_code=exc.code, failure_step=self.current_step or "ui_lock_runtime", message=str(exc), evidence_metadata={"ui_lock": exc.data})
        finally:
            self._release_current_ui_lock(task_id=task.id, reason="add_friend_finished")

    def _release_current_ui_lock(self, *, task_id: str | None = None, reason: str) -> None:
        lease = self.current_ui_lock
        self.current_ui_lock = None
        if not lease:
            return
        try:
            lease.release()
            append_log("INFO", "ui_lock_released", f"已释放微信 UI 锁：{reason}", task_id=task_id, metadata={"lock_id": lease.lock_id})
        except UiLockError as exc:
            append_log("ERROR", "ui_lock_release_failed", str(exc), task_id=task_id, error_code=exc.code, metadata=exc.data)

    def _upload_evidence_best_effort(
        self,
        binding: Binding,
        task_id: str,
        content: str,
        *,
        error_code: str | None = None,
        evidence_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.api.upload_evidence(
                binding,
                task_id,
                content,
                error_code=error_code,
                evidence_path=evidence_path,
                metadata=metadata,
            )
        except Exception as exc:
            append_log("WARN", "evidence_upload_failed", str(exc), task_id=task_id, error_code=error_code)
            self.on_error("执行证据上传失败，主任务结果已保留。")

    def _report_step(self, binding: Binding, task: Task, step: RpaStep) -> None:
        self.current_step = step.current_step
        if self.current_ui_lock:
            self.current_ui_lock.update_step(step.current_step)
            self.current_ui_lock.check_step_timeout()
        self.on_step(step)
        self.api.report_step(binding, task.id, step.current_step, step.remark)
        append_log("INFO", "step_reported", step.remark, task_id=task.id)

    def _run_c2_once(self, binding: Binding) -> None:
        if not CONFIG.c2_enabled:
            return
        if not all(
            hasattr(obj, name)
            for obj, name in (
                (self.bridge, "list_sessions"),
                (self.bridge, "get_messages"),
                (self.api, "post_wechat_session_scan_result"),
                (self.api, "get_wechat_read_targets"),
                (self.api, "post_wechat_messages_ingest"),
            )
        ):
            return
        now = time.monotonic()
        if now - self.last_c2_scan_at >= CONFIG.c2_session_scan_interval_seconds:
            self._scan_wechat_sessions(binding)
            self.last_c2_scan_at = now
        if now - self.last_c2_read_at >= CONFIG.c2_message_read_interval_seconds:
            self._read_bound_wechat_messages(binding)
            self.last_c2_read_at = now

    def _scan_wechat_sessions(self, binding: Binding) -> None:
        owner = f"{binding.worker_id}:{binding.client_instance_id}:session_scan"
        lease: UiLockLease | None = None
        try:
            force_recover_stale_lock(reason="before_session_scan")
            lease = acquire_ui_lock(operation_type="session_scan", owner=owner, current_step="session_scan")
            lease.start_auto_renew()
            self.current_ui_lock = lease
            self.current_step = "session_scan"
            payload = build_scan_result_payload(self.bridge.list_sessions())
            result = self.api.post_wechat_session_scan_result(binding, payload)
            self.c2_stats.update(
                {
                    "last_scan_at": payload.get("finished_at"),
                    "last_scan_sessions": len(payload.get("sessions") or []),
                    "last_bound_count": result.get("bound_count") if isinstance(result, dict) else 0,
                    "last_error": payload.get("error_code"),
                }
            )
            append_log("INFO", "c2_session_scan_reported", "微信会话扫描结果已上报。", metadata={"session_count": self.c2_stats["last_scan_sessions"], "bound_count": self.c2_stats["last_bound_count"]})
        except UiLockError as exc:
            self.c2_stats["last_error"] = exc.code
            append_log("WARN", "c2_session_scan_lock_skipped", str(exc), error_code=exc.code, metadata=exc.data)
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            append_log("ERROR", "c2_session_scan_failed", str(exc))
            self.on_error(f"C2 会话扫描失败：{exc}")
        finally:
            if lease:
                self._release_current_ui_lock(reason="session_scan_finished")
            self.current_step = None

    def _read_bound_wechat_messages(self, binding: Binding) -> None:
        try:
            targets = self.api.get_wechat_read_targets(binding, limit=CONFIG.c2_read_targets_limit)
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            append_log("ERROR", "c2_read_targets_failed", str(exc))
            return
        for target in targets:
            if not target.conversation_id or not target.rpa_session_key or not target.display_name:
                continue
            self._read_one_wechat_target(binding, target)

    def _read_one_wechat_target(self, binding: Binding, target: WechatReadTarget) -> None:
        owner = f"{binding.worker_id}:{binding.client_instance_id}:message_ingest:{target.conversation_id}"
        lease: UiLockLease | None = None
        try:
            force_recover_stale_lock(reason="before_message_ingest")
            lease = acquire_ui_lock(operation_type="message_ingest", owner=owner, current_step="message_read")
            lease.start_auto_renew()
            self.current_ui_lock = lease
            self.current_step = "message_read"
            sidecar_payload = self.bridge.get_messages(display_name=target.display_name, rpa_session_key=target.rpa_session_key)
            if not sidecar_payload.get("ok"):
                code = str(sidecar_payload.get("error_code") or sidecar_payload.get("state") or "MESSAGE_READ_FAILED")
                self.c2_stats["last_error"] = code
                append_log("WARN", "c2_message_read_sidecar_failed", str(sidecar_payload.get("error") or code), error_code=code, metadata={"conversation_id": target.conversation_id})
                return
            payload = build_message_ingest_payload(target, sidecar_payload)
            result = self.api.post_wechat_messages_ingest(binding, payload)
            self.c2_stats.update(
                {
                    "last_message_read_at": (payload.get("evidence") or {}).get("finished_at") if isinstance(payload.get("evidence"), dict) else None,
                    "last_ingested_count": result.get("ingested_count") if isinstance(result, dict) else 0,
                    "last_error": None,
                }
            )
            append_log("INFO", "c2_messages_ingested", "微信消息读取结果已上报。", metadata={"conversation_id": target.conversation_id, "message_count": len(payload.get("messages") or []), "ingested_count": self.c2_stats["last_ingested_count"]})
        except UiLockError as exc:
            self.c2_stats["last_error"] = exc.code
            append_log("WARN", "c2_message_read_lock_skipped", str(exc), error_code=exc.code, metadata=exc.data)
        except ApiError as exc:
            self.c2_stats["last_error"] = exc.code
            append_log("WARN", "c2_messages_ingest_rejected", str(exc), error_code=exc.code, metadata={"conversation_id": target.conversation_id})
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            append_log("ERROR", "c2_message_read_failed", str(exc), metadata={"conversation_id": target.conversation_id})
        finally:
            if lease:
                self._release_current_ui_lock(reason="message_ingest_finished")
            self.current_step = None
