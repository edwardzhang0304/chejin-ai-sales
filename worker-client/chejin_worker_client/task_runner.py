from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .api import ApiError, WorkerApiClient
from .config import CONFIG
from .models import Binding, ReplySendClaim, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from .rpa_bridge import RpaBridge
from .storage import append_log, clear_c2_state, save_binding, save_c2_state
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
        self.last_rpa_component_status: str | None = None
        self.last_wechat_status: str | None = None
        self.c2_stats: dict[str, Any] = {
            "last_scan_at": None,
            "last_scan_sessions": 0,
            "last_bound_count": 0,
            "last_message_read_at": None,
            "last_ingested_count": 0,
            "last_visible_hit_count": 0,
            "last_state_target_count": 0,
            "last_error": None,
        }
        self.visible_hit_queue: list[WechatReadTarget] = []
        self.c2_round_processed_conversation_ids: set[str] = set()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.c2_thread: threading.Thread | None = None
        self.c2_manual_scan_requested = threading.Event()
        self.task_lock = threading.Lock()
        self.heartbeat_interval_seconds = CONFIG.heartbeat_interval_seconds
        self.poll_interval_seconds = CONFIG.poll_interval_seconds
        self.last_c2_scan_at = 0.0
        self.last_c2_read_at = 0.0
        self.c2_read_failure_cooldowns: dict[str, float] = {}

    def start(self, binding: Binding) -> None:
        self.binding = binding
        self.stop_event.clear()
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self._loop, name="CheJinWorkerTaskRunner", daemon=True)
            self.thread.start()
        if CONFIG.c2_enabled and not (self.c2_thread and self.c2_thread.is_alive()):
            self.c2_thread = threading.Thread(target=self._c2_loop, name="CheJinWorkerC2Listener", daemon=True)
            self.c2_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.c2_manual_scan_requested.set()

    def request_immediate_scan(self) -> None:
        self.c2_manual_scan_requested.set()

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
        self.last_rpa_component_status = rpa_status
        self.last_wechat_status = wechat_status
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
            if running_task.task_type == "add_friend":
                self._execute_add_friend_task(binding, running_task)
                return
            if running_task.task_type in {"chat_reply", "follow_up"}:
                self._execute_send_task(binding, running_task)
                return
            result = RpaResult(ok=False, error_code="TASK_TYPE_NOT_SUPPORTED", failure_step="task_dispatch", message=f"不支持的任务类型：{running_task.task_type}")
            self._handle_failed_result(binding, running_task, result)
        except Exception as exc:
            self.on_error(str(exc))
            append_log("ERROR", "task_execute_failed", str(exc), task_id=task.id)
        finally:
            self.current_step = None
            self.current_task = None

    def _execute_add_friend_task(self, binding: Binding, task: Task) -> None:
        result = self._run_add_friend_with_ui_lock(binding, task)
        if result.ok:
            completed = (
                self.api.complete_already_friend(binding, task.id)
                if result.result_code == "already_friend"
                else self.api.complete_invite_sent(binding, task.id)
            )
            if result.evidence_path or result.evidence_metadata:
                self._upload_evidence_best_effort(
                    binding,
                    task.id,
                    result.message,
                    evidence_path=result.evidence_path,
                    metadata=result.evidence_metadata,
                )
            self.on_task(None)
            self.on_result(result)
            append_log("INFO", "task_completed", result.message, task_id=completed.id)
            return
        self._handle_failed_result(binding, task, result)

    def _handle_failed_result(self, binding: Binding, task: Task, result: RpaResult) -> None:
        failed = self.api.fail_task(binding, task.id, result.error_code or "OTHER", result.failure_step, result.message)
        self._upload_evidence_best_effort(
            binding,
            task.id,
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

    def _execute_send_task(self, binding: Binding, task: Task) -> None:
        if task.task_type == "follow_up":
            precheck = self._recall_precheck_read(binding, task)
            if not precheck.get("ok"):
                result = RpaResult(
                    ok=False,
                    error_code=str(precheck.get("error_code") or "RECALL_PRECHECK_READ_FAILED"),
                    failure_step="recall_precheck_read",
                    message=str(precheck.get("message") or "召回发送前未完成微信事实读取，禁止发送 follow_up。"),
                    evidence_metadata={"recall_precheck": precheck},
                )
                self._handle_failed_result(binding, task, result)
                return
        try:
            claim = self.api.claim_send(binding, task)
        except ApiError as exc:
            result = RpaResult(ok=False, error_code=exc.code, failure_step="claim_send", message=str(exc), evidence_metadata={"claim_send": exc.data})
            self._handle_failed_result(binding, task, result)
            return
        except Exception as exc:
            result = RpaResult(ok=False, error_code="CLAIM_SEND_FAILED", failure_step="claim_send", message=str(exc))
            self._handle_failed_result(binding, task, result)
            return

        pending_key = f"pending_send:{claim.reply_action_id}"
        save_c2_state(
            pending_key,
            {
                "reply_action_id": claim.reply_action_id,
                "task_id": task.id,
                "conversation_id": claim.conversation_id,
                "rpa_session_key": claim.rpa_session_key,
                "send_token": claim.send_token,
                "reply_text_hash": claim.reply_text_hash,
                "claimed_at": self._utc_now_iso(),
            },
        )

        actual_hash = self._reply_text_hash(claim.reply_text)
        if claim.reply_text_hash and actual_hash != claim.reply_text_hash:
            self._send_ack_best_effort(binding, claim, send_result="failed", reply_text_hash=actual_hash, error_code="SEND_TEXT_HASH_MISMATCH", remark="实际发送文本 hash 与服务端不一致，未操作微信。")
            clear_c2_state(pending_key)
            self.on_task(None)
            self.on_result(RpaResult(ok=False, error_code="SEND_TEXT_HASH_MISMATCH", failure_step="reply_text_hash_check", message="实际发送文本 hash 与服务端不一致，未操作微信。"))
            return
        if self._is_expired(claim.expire_at):
            self._send_ack_best_effort(binding, claim, send_result="failed", reply_text_hash=actual_hash, error_code="REPLY_ACTION_EXPIRED", remark="reply_action 已过期，未操作微信。")
            clear_c2_state(pending_key)
            self.on_task(None)
            self.on_result(RpaResult(ok=False, error_code="REPLY_ACTION_EXPIRED", failure_step="reply_action_expired", message="reply_action 已过期，未操作微信。"))
            return
        if task.task_type == "chat_reply":
            refresh = self._pre_send_refresh(binding, task, claim, actual_hash)
            if not refresh.get("ok"):
                clear_c2_state(pending_key)
                result = RpaResult(
                    ok=False,
                    error_code=str(refresh.get("error_code") or "PRE_SEND_REFRESH_FAILED"),
                    failure_step="pre_send_refresh",
                    message=str(refresh.get("message") or "发送前微信事实刷新失败，未操作微信。"),
                    evidence_metadata={"pre_send_refresh": refresh},
                )
                self.on_task(None)
                self.on_result(result)
                append_log("WARN", "pre_send_refresh_blocked_send", result.message, task_id=task.id, error_code=result.error_code)
                return

        result = self._send_reply_with_ui_lock(binding, task, claim, actual_hash)
        clear_c2_state(pending_key)
        self.on_task(None)
        self.on_result(result)
        append_log("INFO" if result.ok else "ERROR", "chat_reply_send_finished", result.message, task_id=task.id, error_code=result.error_code)

    def _send_reply_with_ui_lock(self, binding: Binding, task: Task, claim: ReplySendClaim, actual_hash: str) -> RpaResult:
        owner = f"{binding.worker_id}:{binding.client_instance_id}:{task.task_type}:{task.id}"
        try:
            force_recover_stale_lock(reason=f"before_{task.task_type}")
            lease = acquire_ui_lock(operation_type=task.task_type, owner=owner, current_step="reply_send_starting")
            lease.start_auto_renew()
            self.current_ui_lock = lease
            self.current_step = "reply_send_starting"
            append_log("INFO", "ui_lock_acquired", "已获取微信 UI 锁，开始发送服务端批准文本。", task_id=task.id, metadata={"lock_id": lease.lock_id, "fencing_token": lease.fencing_token})
            if self._is_expired(claim.expire_at):
                self._send_ack_best_effort(binding, claim, send_result="failed", reply_text_hash=actual_hash, error_code="REPLY_ACTION_EXPIRED", remark="reply_action 已过期，未发送。")
                return RpaResult(ok=False, error_code="REPLY_ACTION_EXPIRED", failure_step="reply_action_expired", message="reply_action 已过期，未发送。")
            self._report_step(binding, task, RpaStep(current_step="reply_send_starting", title="准备发送回复", remark="已通过 claim-send，准备定位微信会话。"))
            target = self._send_target(task, claim)
            sidecar_payload = self.bridge.send_reply(target=target, rpa_session_key=claim.rpa_session_key, text=claim.reply_text, task_id=task.id)
            evidence = self._send_evidence(sidecar_payload, target=target)
            sidecar_run_id = str(sidecar_payload.get("sidecar_run_id") or sidecar_payload.get("run_id") or "") or None
            if sidecar_payload.get("ok"):
                self._send_ack_best_effort(binding, claim, send_result="sent", reply_text_hash=actual_hash, sidecar_run_id=sidecar_run_id, evidence=evidence, remark="服务端批准文本已发送。", sent_at=self._utc_now_iso())
                self._report_step(binding, task, RpaStep(current_step="sent_ack_reported", title="发送回执已上报", remark="sent_ack=sent 已上报服务端。"))
                return RpaResult(ok=True, result_code="chat_reply_sent" if task.task_type == "chat_reply" else "follow_up_sent", message="服务端批准文本已发送。", evidence_metadata=evidence)
            error_code = str(sidecar_payload.get("error_code") or ("SEND_RESULT_UNKNOWN" if sidecar_payload.get("current_step") == "rpa_sidecar_timeout" else "RPA_SEND_REPLY_FAILED"))
            send_result = "unknown" if error_code in {"RPA_SIDECAR_TIMEOUT", "SEND_RESULT_UNKNOWN"} else "failed"
            remark = "发送结果未知，需人工确认。" if send_result == "unknown" else str(sidecar_payload.get("message") or sidecar_payload.get("error") or "发送服务端批准文本失败。")
            self._send_ack_best_effort(binding, claim, send_result=send_result, reply_text_hash=actual_hash, sidecar_run_id=sidecar_run_id, evidence=evidence, error_code=error_code, remark=remark)
            return RpaResult(ok=False, error_code=error_code, failure_step="send_reply_unknown" if send_result == "unknown" else "send_reply", message=remark, evidence_metadata=evidence)
        except UiLockError as exc:
            self._send_ack_best_effort(binding, claim, send_result="failed", reply_text_hash=actual_hash, error_code=exc.code, remark=str(exc), evidence={"ui_lock": exc.data})
            return RpaResult(ok=False, error_code=exc.code, failure_step="ui_lock_acquire", message=str(exc), evidence_metadata={"ui_lock": exc.data})
        except Exception as exc:
            self._send_ack_best_effort(binding, claim, send_result="unknown", reply_text_hash=actual_hash, error_code="SEND_RESULT_UNKNOWN", remark="发送结果未知，需人工确认。", evidence={"error": str(exc)})
            return RpaResult(ok=False, error_code="SEND_RESULT_UNKNOWN", failure_step="send_reply_unknown", message="发送结果未知，需人工确认。", evidence_metadata={"error": str(exc)})
        finally:
            self._release_current_ui_lock(task_id=task.id, reason=f"{task.task_type}_finished")

    def _send_ack_best_effort(
        self,
        binding: Binding,
        claim: ReplySendClaim,
        *,
        send_result: str,
        reply_text_hash: str | None,
        sidecar_run_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        error_code: str | None = None,
        remark: str | None = None,
        sent_at: str | None = None,
    ) -> None:
        try:
            self.api.sent_ack(
                binding,
                claim,
                send_result=send_result,
                reply_text_hash=reply_text_hash,
                sidecar_run_id=sidecar_run_id,
                evidence=evidence,
                error_code=error_code,
                remark=remark,
                sent_at=sent_at,
            )
        except Exception as exc:
            append_log("ERROR", "sent_ack_failed", str(exc), task_id=claim.task_id, error_code=error_code, metadata={"reply_action_id": claim.reply_action_id, "send_result": send_result})
            self.on_error(f"sent_ack 上报失败：{exc}")

    def _send_target(self, task: Task, claim: ReplySendClaim) -> str:
        for value in (
            task.raw.get("display_name"),
            task.raw.get("wechat_display_name"),
            task.raw.get("rpa_target"),
            task.customer_name,
            claim.raw.get("display_name"),
            claim.rpa_session_key,
        ):
            text = str(value or "").strip()
            if text:
                return text
        return claim.conversation_id

    def _claim_read_target(self, task: Task, claim: ReplySendClaim, *, read_reason: str) -> WechatReadTarget:
        target = self._send_target(task, claim)
        remark_code = str(claim.raw.get("remark_code") or task.raw.get("remark_code") or task.remark_code or "").strip() or None
        return WechatReadTarget(
            conversation_id=claim.conversation_id,
            rpa_session_key=claim.rpa_session_key,
            display_name=target,
            remark_code=remark_code,
            row_fingerprint={"value": f"{read_reason}:{claim.conversation_id}:{claim.rpa_session_key}"},
            read_reason=read_reason,
            raw={"source": "reply_send_claim", "reply_action_id": claim.reply_action_id, "remark_code": remark_code},
        )

    def _pre_send_refresh(self, binding: Binding, task: Task, claim: ReplySendClaim, actual_hash: str) -> dict[str, Any]:
        target = self._claim_read_target(task, claim, read_reason="pre_send_refresh")
        read_result = self._read_one_wechat_target(
            binding,
            target,
            current_step="pre_send_refresh",
            allow_during_current_task=True,
        )
        if not read_result.get("ok"):
            self._send_ack_best_effort(
                binding,
                claim,
                send_result="failed",
                reply_text_hash=actual_hash,
                error_code=str(read_result.get("error_code") or "PRE_SEND_REFRESH_FAILED"),
                remark="发送前微信事实刷新失败，禁止发送。",
                evidence={"pre_send_refresh": read_result},
            )
            return {"ok": False, "error_code": read_result.get("error_code") or "PRE_SEND_REFRESH_FAILED", "message": "发送前微信事实刷新失败，未操作微信。", "read_result": read_result}
        if int(read_result.get("new_customer_message_count") or 0) > 0:
            return {
                "ok": False,
                "error_code": "REPLY_ACTION_SUPERSEDED_BY_PRE_SEND_REFRESH",
                "message": "发送前刷新发现客户新消息，旧回复动作已作废，未操作微信。",
                "read_result": read_result,
            }
        return {"ok": True, "read_result": read_result}

    def _recall_precheck_read(self, binding: Binding, task: Task) -> dict[str, Any]:
        conversation_id = str(task.raw.get("conversation_id") or "").strip()
        rpa_session_key = str(task.raw.get("rpa_session_key") or "").strip()
        display_name = str(task.raw.get("display_name") or task.raw.get("wechat_display_name") or task.customer_name or "").strip()
        if not conversation_id or not rpa_session_key or not display_name:
            return {"ok": False, "error_code": "RECALL_PRECHECK_TARGET_MISSING", "message": "follow_up 任务缺少 recall_precheck_read 所需会话定位字段。"}
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key=rpa_session_key,
            display_name=display_name,
            remark_code=str(task.raw.get("remark_code") or task.remark_code or "").strip() or None,
            row_fingerprint={"value": f"recall_precheck:{conversation_id}:{rpa_session_key}"},
            read_reason="recall_precheck",
            raw={"source": "follow_up_task"},
        )
        read_result = self._read_one_wechat_target(
            binding,
            target,
            current_step="recall_precheck_read",
            allow_during_current_task=True,
        )
        if not read_result.get("ok"):
            return {"ok": False, "error_code": read_result.get("error_code") or "RECALL_PRECHECK_READ_FAILED", "message": "召回发送前微信事实读取失败。", "read_result": read_result}
        return {"ok": True, "read_result": read_result}

    def _send_evidence(self, payload: dict[str, Any], *, target: str) -> dict[str, Any]:
        return {
            "adapter": payload.get("adapter"),
            "state": payload.get("state"),
            "target": target,
            "window_probe": payload.get("window_probe"),
            "send_result": payload.get("send_result"),
            "guard": payload.get("guard"),
            "timing": payload.get("timing"),
            "stdout_tail": payload.get("stdout_tail") or payload.get("stdout"),
            "stderr_tail": payload.get("stderr_tail") or payload.get("stderr"),
            "returncode": payload.get("returncode"),
        }

    def _reply_text_hash(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _is_expired(self, value: str | None) -> bool:
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)

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

    def _c2_loop(self) -> None:
        append_log("INFO", "c2_listener_started", "C2 微信监听循环启动。")
        while not self.stop_event.is_set():
            binding = self.binding
            if not binding or not CONFIG.c2_enabled or not self._c2_dependencies_ready():
                self.stop_event.wait(1.0)
                continue
            if not self._wechat_ready_for_c2():
                self.stop_event.wait(1.0)
                continue
            now = time.monotonic()
            if self.c2_manual_scan_requested.is_set():
                self.c2_manual_scan_requested.clear()
                self._run_c2_scan_round(binding, reason="manual_immediate_scan")
            if now - self.last_c2_scan_at >= CONFIG.c2_session_scan_interval_seconds:
                self._run_c2_scan_round(binding, reason="visible_sessions")
                self.last_c2_scan_at = now
            if now - self.last_c2_read_at >= CONFIG.c2_message_read_interval_seconds:
                self._read_state_target_queue(binding)
                self.last_c2_read_at = now
            self.stop_event.wait(1.0)

    def _c2_dependencies_ready(self) -> bool:
        return all(
            hasattr(obj, name)
            for obj, name in (
                (self.bridge, "list_sessions"),
                (self.bridge, "get_messages"),
                (self.api, "post_wechat_session_scan_result"),
                (self.api, "get_wechat_read_targets"),
                (self.api, "post_wechat_messages_ingest"),
            )
        )

    def _wechat_ready_for_c2(self) -> bool:
        return self.last_rpa_component_status == "ready" and self.last_wechat_status == "logged_in"

    def _high_priority_active(self) -> bool:
        return self.current_task is not None or self.task_lock.locked()

    def _run_c2_scan_round(self, binding: Binding, *, reason: str) -> None:
        self.c2_round_processed_conversation_ids = set()
        self._scan_wechat_sessions(binding, reason=reason)
        self._drain_visible_hit_queue(binding)
        self._read_state_target_queue(binding)

    def _scan_wechat_sessions(self, binding: Binding, *, reason: str = "scheduled") -> None:
        if self._high_priority_active():
            self.c2_stats["last_error"] = "C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION"
            append_log("INFO", "c2_session_scan_skipped", "C2 第一屏扫描被高优先级微信动作跳过。", error_code="C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION", metadata={"reason": reason})
            return
        owner = f"{binding.worker_id}:{binding.client_instance_id}:session_scan:first_screen"
        lease: UiLockLease | None = None
        try:
            force_recover_stale_lock(reason="before_session_scan")
            lease = acquire_ui_lock(
                operation_type="session_scan",
                owner=owner,
                current_step="first_screen_session_scan",
                timeout_seconds=CONFIG.c2_low_priority_lock_timeout_seconds,
            )
            lease.start_auto_renew()
            self.current_ui_lock = lease
            self.current_step = "first_screen_session_scan"
            if self._high_priority_active():
                self.c2_stats["last_error"] = "C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION"
                append_log("INFO", "c2_session_scan_skipped", "C2 第一屏扫描拿锁后发现高优先级动作，已跳过。", error_code="C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION", metadata={"reason": reason})
                return
            sidecar_payload = self.bridge.list_sessions()
            payload = build_scan_result_payload(sidecar_payload)
            result = self.api.post_wechat_session_scan_result(binding, payload)
            self._enqueue_visible_hits(payload, result)
            self.c2_stats.update(
                {
                    "last_scan_at": payload.get("finished_at"),
                    "last_scan_sessions": len(payload.get("sessions") or []),
                    "last_bound_count": result.get("bound_count") if isinstance(result, dict) else 0,
                    "last_visible_hit_count": len(self.visible_hit_queue),
                    "last_error": payload.get("error_code"),
                }
            )
            append_log("INFO", "c2_session_scan_reported", "微信第一屏会话扫描结果已上报。", metadata={"session_count": self.c2_stats["last_scan_sessions"], "bound_count": self.c2_stats["last_bound_count"], "visible_hit_count": self.c2_stats["last_visible_hit_count"], "reason": reason})
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

    def _enqueue_visible_hits(self, payload: dict[str, Any], result: dict[str, Any] | None) -> None:
        if not isinstance(result, dict):
            return
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        session_by_key = {str(item.get("rpa_session_key") or ""): item for item in sessions if isinstance(item, dict)}
        queued_keys = {self._target_dedupe_key(item) for item in self.visible_hit_queue}
        for item in result.get("bindings") or []:
            if not isinstance(item, dict) or not item.get("can_ingest_messages"):
                continue
            conversation_id = str(item.get("conversation_id") or "")
            rpa_session_key = str(item.get("rpa_session_key") or "")
            session = session_by_key.get(rpa_session_key) or {}
            target = WechatReadTarget.from_api(
                {
                    "conversation_id": conversation_id,
                    "lead_id": item.get("lead_id"),
                    "sales_id": item.get("sales_id"),
                    "remark_code": item.get("remark_code"),
                    "rpa_session_key": rpa_session_key,
                    "display_name": item.get("display_name") or session.get("display_name"),
                    "row_fingerprint": item.get("row_fingerprint") or session.get("row_fingerprint"),
                    "ocr_confidence": item.get("ocr_confidence") if item.get("ocr_confidence") is not None else session.get("ocr_confidence"),
                    "read_reason": "visible_hit",
                }
            )
            dedupe_key = self._target_dedupe_key(target)
            if not dedupe_key or dedupe_key in queued_keys or dedupe_key in self.c2_round_processed_conversation_ids:
                continue
            self.visible_hit_queue.append(target)
            queued_keys.add(dedupe_key)

    def _drain_visible_hit_queue(self, binding: Binding) -> None:
        while self.visible_hit_queue:
            target = self.visible_hit_queue.pop(0)
            dedupe_key = self._target_dedupe_key(target)
            if dedupe_key in self.c2_round_processed_conversation_ids:
                continue
            cooldown_remaining = self._c2_read_cooldown_remaining(dedupe_key)
            if cooldown_remaining > 0:
                append_log("INFO", "c2_visible_hit_cooldown", "C2 第一屏命中目标刚失败过，冷却期内跳过本轮重试。", metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "cooldown_remaining_seconds": round(cooldown_remaining, 1)})
                continue
            validation_error = self._validate_read_target(target)
            if validation_error:
                self.c2_stats["last_error"] = validation_error
                append_log(
                    "WARN",
                    "c2_visible_hit_skipped",
                    "C2 第一屏命中目标校验未通过，已跳过。",
                    error_code=validation_error,
                    metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "read_reason": target.read_reason},
                )
                continue
            read_result = self._read_one_wechat_target(binding, target, current_step="visible_hit_message_read")
            if read_result.get("ok"):
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                self.c2_read_failure_cooldowns.pop(dedupe_key, None)
            else:
                self._mark_c2_read_failure_cooldown(dedupe_key, read_result.get("error_code"))

    def _read_bound_wechat_messages(self, binding: Binding) -> None:
        self._read_state_target_queue(binding)

    def _read_state_target_queue(self, binding: Binding) -> None:
        try:
            targets = self.api.get_wechat_read_targets(binding, limit=CONFIG.c2_read_targets_limit)
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            append_log("ERROR", "c2_read_targets_failed", str(exc))
            return
        self.c2_stats["last_state_target_count"] = len(targets)
        for target in targets:
            dedupe_key = self._target_dedupe_key(target)
            if dedupe_key in self.c2_round_processed_conversation_ids:
                append_log("INFO", "c2_state_target_deduped", "状态机读取目标已在本轮第一屏命中读取中处理，跳过重复读取。", metadata={"conversation_id": target.conversation_id, "rpa_session_key": target.rpa_session_key, "remark_code": target.remark_code, "read_reason": target.read_reason})
                continue
            cooldown_remaining = self._c2_read_cooldown_remaining(dedupe_key)
            if cooldown_remaining > 0:
                append_log("INFO", "c2_state_target_cooldown", "C2 定向读取刚失败过，冷却期内跳过本轮重试。", metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "cooldown_remaining_seconds": round(cooldown_remaining, 1)})
                continue
            validation_error = self._validate_read_target(target)
            if validation_error:
                self.c2_stats["last_error"] = validation_error
                append_log("WARN", "c2_read_target_skipped", "C2 读取目标校验未通过，已跳过。", error_code=validation_error, metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "read_reason": target.read_reason})
                continue
            if self._high_priority_active():
                append_log("INFO", "c2_message_read_interrupted", "C2 消息读取被高优先级微信动作中断。", error_code="SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION", metadata={"conversation_id": target.conversation_id})
                self.c2_stats["last_error"] = "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION"
                break
            read_result = self._read_one_wechat_target(binding, target, current_step="state_target_message_read")
            if read_result.get("ok"):
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                self.c2_read_failure_cooldowns.pop(dedupe_key, None)
            else:
                self._mark_c2_read_failure_cooldown(dedupe_key, read_result.get("error_code"))

    def _target_dedupe_key(self, target: WechatReadTarget) -> str:
        if target.conversation_id and target.remark_code:
            return f"conversation:{target.conversation_id}:remark_code:{target.remark_code}"
        return f"invalid:{target.conversation_id}:{target.remark_code or ''}:{target.rpa_session_key}:{target.display_name}"

    def _validate_read_target(self, target: WechatReadTarget) -> str | None:
        if not target.conversation_id:
            return "C2_TARGET_CONVERSATION_ID_MISSING"
        if not target.remark_code:
            return "C2_TARGET_REMARK_CODE_MISSING"
        if not target.rpa_session_key and not target.display_name:
            return "C2_TARGET_LOCATOR_MISSING"
        if target.ocr_confidence is not None and target.ocr_confidence < CONFIG.c2_message_min_ocr_confidence:
            return "C2_TARGET_OCR_LOW_CONFIDENCE"
        return None

    def _c2_read_cooldown_remaining(self, dedupe_key: str) -> float:
        until = float(self.c2_read_failure_cooldowns.get(dedupe_key) or 0)
        remaining = until - time.monotonic()
        if remaining <= 0:
            self.c2_read_failure_cooldowns.pop(dedupe_key, None)
            return 0.0
        return remaining

    def _mark_c2_read_failure_cooldown(self, dedupe_key: str, error_code: Any = None) -> None:
        cooldown = max(0.0, float(CONFIG.c2_message_failure_cooldown_seconds))
        if cooldown <= 0:
            return
        self.c2_read_failure_cooldowns[dedupe_key] = time.monotonic() + cooldown
        append_log("INFO", "c2_read_failure_cooldown_started", "C2 定向读取失败，已进入短冷却，避免反复重置微信搜索框。", metadata={"target_key": dedupe_key, "error_code": error_code, "cooldown_seconds": cooldown})

    def _read_one_wechat_target(
        self,
        binding: Binding,
        target: WechatReadTarget,
        *,
        current_step: str = "message_read",
        allow_during_current_task: bool = False,
    ) -> dict[str, Any]:
        owner = f"{binding.worker_id}:{binding.client_instance_id}:message_ingest:{target.conversation_id}"
        lease: UiLockLease | None = None
        try:
            if not allow_during_current_task and self._high_priority_active():
                self.c2_stats["last_error"] = "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION"
                append_log("INFO", "c2_message_read_interrupted", "C2 消息读取被高优先级微信动作中断。", error_code="SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION", metadata={"conversation_id": target.conversation_id})
                return {"ok": False, "error_code": "SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION"}
            force_recover_stale_lock(reason="before_message_ingest")
            lease = acquire_ui_lock(
                operation_type="message_ingest",
                owner=owner,
                current_step=current_step,
                timeout_seconds=CONFIG.c2_low_priority_lock_timeout_seconds,
            )
            lease.start_auto_renew()
            self.current_ui_lock = lease
            self.current_step = current_step
            target_mode = "visible" if target.read_reason == "visible_hit" else "search_by_remark_code"
            sidecar_payload = self.bridge.get_messages(
                display_name=target.display_name or target.remark_code or "",
                rpa_session_key=target.rpa_session_key if target_mode == "visible" else "",
                remark_code=target.remark_code or "",
                target_mode=target_mode,
            )
            if not sidecar_payload.get("ok"):
                code = str(sidecar_payload.get("error_code") or sidecar_payload.get("state") or "MESSAGE_READ_FAILED")
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_message_read_sidecar_failed",
                    str(sidecar_payload.get("error") or sidecar_payload.get("reason") or code),
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "sidecar_run_id": sidecar_payload.get("sidecar_run_id"),
                        "artifact_dir": sidecar_payload.get("artifact_dir"),
                        "review_path": sidecar_payload.get("review_path"),
                        "evidence_path": sidecar_payload.get("evidence_path"),
                        "target_mode": sidecar_payload.get("target_mode"),
                        "targeting": sidecar_payload.get("targeting"),
                        "step_events": sidecar_payload.get("step_events"),
                        "open_chat_timing": sidecar_payload.get("open_chat_timing"),
                    },
                )
                return {"ok": False, "error_code": code}
            payload = build_message_ingest_payload(target, sidecar_payload)
            result = self.api.post_wechat_messages_ingest(binding, payload)
            customer_keys = {
                item.get("dedupe_key")
                for item in payload.get("messages") or []
                if isinstance(item, dict) and item.get("sender_role_hint") == "customer" and item.get("dedupe_key")
            }
            new_customer_message_count = sum(
                1
                for item in (result.get("results") or [])
                if isinstance(item, dict) and item.get("dedupe_key") in customer_keys and item.get("ingest_result") == "ingested"
            )
            if not result.get("results") and customer_keys and int(result.get("ingested_count") or 0) > 0:
                new_customer_message_count = int(result.get("ingested_count") or 0)
            self.c2_stats.update(
                {
                    "last_message_read_at": (payload.get("evidence") or {}).get("finished_at") if isinstance(payload.get("evidence"), dict) else None,
                    "last_ingested_count": result.get("ingested_count") if isinstance(result, dict) else 0,
                    "last_error": None,
                }
            )
            append_log("INFO", "c2_messages_ingested", "微信消息读取结果已上报。", metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "message_count": len(payload.get("messages") or []), "ingested_count": self.c2_stats["last_ingested_count"]})
            return {"ok": True, "result": result, "payload": payload, "new_customer_message_count": new_customer_message_count}
        except UiLockError as exc:
            self.c2_stats["last_error"] = exc.code
            append_log("INFO", "c2_message_read_interrupted", "C2 消息读取未拿到锁，按低优先级中断处理。", error_code="SCAN_INTERRUPTED_BY_HIGH_PRIORITY_ACTION", metadata={"ui_lock": exc.data, "lock_error_code": exc.code})
            return {"ok": False, "error_code": exc.code}
        except ApiError as exc:
            self.c2_stats["last_error"] = exc.code
            append_log("WARN", "c2_messages_ingest_rejected", str(exc), error_code=exc.code, metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code})
            return {"ok": False, "error_code": exc.code, "api_error": exc.data}
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            append_log("ERROR", "c2_message_read_failed", str(exc), metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code})
            return {"ok": False, "error_code": "MESSAGE_READ_FAILED", "message": str(exc)}
        finally:
            if lease:
                self._release_current_ui_lock(reason="message_ingest_finished")
            self.current_step = None
