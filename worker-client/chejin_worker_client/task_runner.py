from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .api import ApiError, WorkerApiClient
from .c2_contract import sidecar_contract_error
from .config import CONFIG
from .models import Binding, ReplySendClaim, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from .rpa_bridge import RpaBridge
from .storage import append_log, clear_c2_state, save_binding, save_c2_state
from .ui_lock import UiLockError, UiLockLease, acquire_ui_lock, force_recover_stale_lock, lock_summary
from .wechat_c2 import build_message_ingest_payload, build_scan_result_payload, extract_remark_codes


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

C2_RECENT_VISIBLE_CACHE_TTL_SECONDS = 90.0

C2_LOCATE_TERMINAL_ERROR_CODES = {
    "C2_VISIBLE_TARGET_AMBIGUOUS",
    "C2_GROUP_CHAT_NOT_ALLOWED",
    "C2_CONVERSATION_TYPE_UNKNOWN",
}


def _messages_need_voice_transcribe(sidecar_payload: dict[str, Any]) -> bool:
    observations = sidecar_payload.get("observations")
    return isinstance(observations, list) and any(
        isinstance(item, dict)
        and item.get("row_kind") == "voice_bubble"
        and item.get("message_type") == "voice"
        and item.get("voice_state") == "untranscribed"
        and item.get("sender_role") in {"customer", "self"}
        and item.get("sender_role_source") == "same_row_avatar"
        and not item.get("contract_errors")
        for item in observations
    )


def _voice_payload_has_unbound_transcript(sidecar_payload: dict[str, Any]) -> bool:
    transcribed = sidecar_payload.get("transcribed_messages")
    if isinstance(transcribed, list) and any(isinstance(item, dict) for item in transcribed):
        return False
    new_messages = sidecar_payload.get("new_messages")
    return isinstance(new_messages, list) and any(
        isinstance(item, dict)
        and str(item.get("type") or item.get("message_type") or "").lower() in {"voice", "audio"}
        and "voice_duration_prefix_removed" in (item.get("quality_flags") or [])
        and bool(str(item.get("content_clean") or item.get("content") or "").strip())
        for item in new_messages
    )


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
        self.last_rpa_probe_at = 0.0
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
        self.c2_read_success_cooldowns: dict[str, float] = {}
        self.c2_read_allowlist_keys: set[str] = set()
        self.c2_active_target_cache: dict[str, Any] = {}
        self.c2_last_visible_sessions: list[dict[str, Any]] = []
        self.c2_last_visible_sessions_monotonic = 0.0
        self.c2_recent_visible_hits_by_remark_code: dict[str, dict[str, Any]] = {}
        self.c2_voice_binding_blocked_authorizations: set[str] = set()
        self.c2_stop_guard_before_voice_seconds = max(0.0, float(CONFIG.c2_stop_guard_before_voice_seconds))

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
        now = time.monotonic()
        probe_due = (
            self.last_rpa_component_status is None
            or self.last_wechat_status is None
            or now - self.last_rpa_probe_at >= max(1.0, float(self.heartbeat_interval_seconds))
        )
        ui_action_active = self.current_ui_lock is not None or bool(lock_summary().get("locked"))
        if probe_due and not ui_action_active:
            rpa_status, wechat_status = self.bridge.probe()
            self.last_rpa_component_status = rpa_status
            self.last_wechat_status = wechat_status
            self.last_rpa_probe_at = time.monotonic()
        else:
            rpa_status = self.last_rpa_component_status or "unavailable"
            wechat_status = self.last_wechat_status or "unknown"
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
            authorization_revision=str(claim.raw.get("authorization_revision") or "").strip() or None,
            raw={
                "source": "reply_send_claim",
                "reply_action_id": claim.reply_action_id,
                "remark_code": remark_code,
                "authorization_revision": claim.raw.get("authorization_revision"),
            },
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
            authorization_revision=str(task.raw.get("authorization_revision") or "").strip() or None,
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
                (self.bridge, "voice_transcribe"),
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
                (self.bridge, "voice_transcribe"),
                (self.api, "post_wechat_session_scan_result"),
                (self.api, "get_wechat_read_targets"),
                (self.api, "post_wechat_messages_ingest"),
            )
        )

    def _wechat_ready_for_c2(self) -> bool:
        return self.last_rpa_component_status == "ready" and self.last_wechat_status == "logged_in"

    def _high_priority_active(self) -> bool:
        return self.current_task is not None or self.task_lock.locked()

    def _compact_visible_sessions_for_review(self, sessions: list[Any], *, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, session in enumerate(sessions[:limit]):
            if not isinstance(session, dict):
                continue
            rows.append(
                {
                    "index": index,
                    "display_name": session.get("display_name") or session.get("name") or session.get("title"),
                    "rpa_session_key": session.get("rpa_session_key") or session.get("session_key"),
                    "remark_code_candidates": session.get("remark_code_candidates") or [],
                    "last_message_preview": session.get("last_message_preview") or session.get("content") or session.get("preview"),
                    "ocr_confidence": session.get("ocr_confidence"),
                    "row_fingerprint": session.get("row_fingerprint"),
                    "center_y": session.get("center_y"),
                    "bounds": [session.get("left"), session.get("top"), session.get("right"), session.get("bottom")]
                    if any(session.get(key) is not None for key in ("left", "top", "right", "bottom"))
                    else None,
                }
            )
        return rows

    def _compact_ocr_items_for_review(self, items: list[Any], *, limit: int = 120) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            row: dict[str, Any] = {"text": str(item.get("text") or "")}
            for key in ("left", "top", "right", "bottom", "center_x", "center_y", "confidence"):
                if key in item:
                    try:
                        row[key] = round(float(item.get(key) or 0), 3)
                    except Exception:
                        row[key] = item.get(key)
            if item.get("ocr_source"):
                row["ocr_source"] = item.get("ocr_source")
            rows.append(row)
        return rows

    def _visible_sessions_with_click_geometry(self, mapped_sessions: list[Any], raw_sessions: list[Any]) -> list[dict[str, Any]]:
        raw_by_key = {str(item.get("session_key") or ""): item for item in raw_sessions if isinstance(item, dict)}
        enriched: list[dict[str, Any]] = []
        for item in mapped_sessions:
            if not isinstance(item, dict):
                continue
            session = dict(item)
            raw = raw_by_key.get(str(session.get("rpa_session_key") or ""))
            if isinstance(raw, dict):
                for source_key, target_key in (
                    ("center_y", "center_y"),
                    ("left", "left"),
                    ("right", "right"),
                    ("top", "top"),
                    ("bottom", "bottom"),
                    ("center_x", "center_x"),
                    ("confidence", "ocr_confidence"),
                ):
                    if session.get(target_key) is None and raw.get(source_key) is not None:
                        session[target_key] = raw.get(source_key)
                if not isinstance(session.get("row_fingerprint"), dict) and raw.get("row_fingerprint"):
                    session["row_fingerprint"] = raw.get("row_fingerprint")
                if session.get("center_y") is None:
                    row_fingerprint = session.get("row_fingerprint")
                    if isinstance(row_fingerprint, dict):
                        bbox = row_fingerprint.get("title_bbox")
                        if isinstance(bbox, list) and len(bbox) >= 4:
                            try:
                                left, top, right, bottom = [float(value) for value in bbox[:4]]
                                session.setdefault("left", left)
                                session.setdefault("right", right)
                                session.setdefault("top", top)
                                session.setdefault("bottom", bottom)
                                session["center_y"] = (top + bottom) / 2.0
                            except (TypeError, ValueError):
                                pass
                session["raw_session_available"] = True
            enriched.append(session)
        return enriched

    def _remember_recent_visible_hits(self, sessions: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        self._prune_recent_visible_hits(now=now)
        for session in sessions:
            if not isinstance(session, dict):
                continue
            codes = [str(code or "").strip().upper() for code in (session.get("remark_code_candidates") or []) if str(code or "").strip()]
            for code in codes:
                self.c2_recent_visible_hits_by_remark_code[code] = {"session": dict(session), "seen_at": now}

    def _prune_recent_visible_hits(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [
            code
            for code, entry in self.c2_recent_visible_hits_by_remark_code.items()
            if current - float(entry.get("seen_at") or 0) > C2_RECENT_VISIBLE_CACHE_TTL_SECONDS
        ]
        for code in expired:
            self.c2_recent_visible_hits_by_remark_code.pop(code, None)

    def _sidecar_visible_session_candidate(self, session: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(session, dict):
            return {}
        row_fingerprint = session.get("row_fingerprint")
        candidate: dict[str, Any] = {
            "name": session.get("name") or session.get("display_name") or session.get("title"),
            "session_key": session.get("session_key") or session.get("rpa_session_key"),
            "row_fingerprint": row_fingerprint,
            "confidence": session.get("confidence") if session.get("confidence") is not None else session.get("ocr_confidence"),
            "preview": session.get("preview") or session.get("content") or session.get("last_message_preview"),
        }
        for key in ("center_y", "left", "right", "top", "bottom"):
            value = session.get(key)
            if value is None:
                continue
            try:
                candidate[key] = float(value)
            except (TypeError, ValueError):
                pass
        if "center_y" not in candidate and isinstance(row_fingerprint, dict):
            bbox = row_fingerprint.get("title_bbox")
            if isinstance(bbox, list) and len(bbox) >= 4:
                try:
                    left, top, right, bottom = [float(value) for value in bbox[:4]]
                    candidate.setdefault("left", left)
                    candidate.setdefault("right", right)
                    candidate.setdefault("top", top)
                    candidate.setdefault("bottom", bottom)
                    candidate["center_y"] = (top + bottom) / 2.0
                    candidate["click_geometry_source"] = "row_fingerprint.title_bbox"
                except (TypeError, ValueError):
                    pass
        elif "center_y" in candidate:
            candidate["click_geometry_source"] = "session_fields"
        return {key: value for key, value in candidate.items() if value not in (None, "", {})}

    def _write_c2_sessions_review(
        self,
        *,
        reason: str,
        sidecar_payload: dict[str, Any],
        scan_payload: dict[str, Any],
        target: WechatReadTarget | None = None,
        match_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        artifact_dir_value = sidecar_payload.get("artifact_dir")
        if not artifact_dir_value:
            return None
        try:
            artifact_dir = Path(str(artifact_dir_value))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            raw_sessions = sidecar_payload.get("sessions") if isinstance(sidecar_payload.get("sessions"), list) else []
            mapped_sessions = scan_payload.get("sessions") if isinstance(scan_payload.get("sessions"), list) else []
            ocr_items = sidecar_payload.get("ocr_items") if isinstance(sidecar_payload.get("ocr_items"), list) else []
            review = {
                "schema": "chejin.c2.sessions_review.v1",
                "reason": reason,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sidecar": {
                    "ok": bool(sidecar_payload.get("ok")),
                    "state": sidecar_payload.get("state"),
                    "error_code": sidecar_payload.get("error_code"),
                    "sidecar_run_id": sidecar_payload.get("sidecar_run_id") or scan_payload.get("sidecar_run_id"),
                    "artifact_dir": str(artifact_dir),
                    "screenshot_path": sidecar_payload.get("screenshot_path") or (scan_payload.get("evidence") or {}).get("screenshot"),
                    "ocr_items_count": sidecar_payload.get("ocr_items_count"),
                    "ocr_items_enhanced_count": sidecar_payload.get("ocr_items_enhanced_count"),
                    "ocr_items": self._compact_ocr_items_for_review(ocr_items),
                },
                "target": {
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "display_name": target.display_name,
                    "rpa_session_key": target.rpa_session_key,
                    "read_reason": target.read_reason,
                }
                if target
                else None,
                "scan": {
                    "scan_id": scan_payload.get("scan_id"),
                    "session_count": len(mapped_sessions),
                    "mapped_sessions": self._compact_visible_sessions_for_review(mapped_sessions),
                    "raw_sessions": self._compact_visible_sessions_for_review(raw_sessions),
                },
                "match": match_metadata,
            }
            review_path = artifact_dir / "sessions_review.json"
            review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(review_path)
        except Exception as exc:
            append_log("WARN", "c2_sessions_review_write_failed", "C2 首屏扫描证据报告写入失败。", error_code="C2_SESSIONS_REVIEW_WRITE_FAILED", metadata={"reason": reason, "error": str(exc)})
            return None

    def _run_c2_scan_round(self, binding: Binding, *, reason: str) -> None:
        self.c2_round_processed_conversation_ids = set()
        self._scan_wechat_sessions(binding, reason=reason)
        targets = self._fetch_read_targets(binding)
        allowed_keys = {self._target_dedupe_key(target) for target in targets}
        self.c2_read_allowlist_keys = allowed_keys
        self._drain_visible_hit_queue(binding, authorized_targets=targets)
        self._read_state_target_queue(binding, targets=targets)

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
            review_path = self._write_c2_sessions_review(reason=reason, sidecar_payload=sidecar_payload, scan_payload=payload)
            raw_sessions = sidecar_payload.get("sessions") if isinstance(sidecar_payload.get("sessions"), list) else []
            self.c2_last_visible_sessions = [
                item
                for item in self._visible_sessions_with_click_geometry(payload.get("sessions") or [], raw_sessions)
                if isinstance(item, dict)
            ]
            self.c2_last_visible_sessions_monotonic = time.monotonic()
            self._remember_recent_visible_hits(self.c2_last_visible_sessions)
            result = self.api.post_wechat_session_scan_result(binding, payload)
            self._enqueue_visible_hits(payload, result, sidecar_payload=sidecar_payload)
            self.c2_stats.update(
                {
                    "last_scan_at": payload.get("finished_at"),
                    "last_scan_sessions": len(payload.get("sessions") or []),
                    "last_bound_count": result.get("bound_count") if isinstance(result, dict) else 0,
                    "last_visible_hit_count": len(self.visible_hit_queue),
                    "last_error": payload.get("error_code"),
                }
            )
            append_log(
                "INFO",
                "c2_session_scan_reported",
                "微信第一屏会话扫描结果已上报。",
                metadata={
                    "session_count": self.c2_stats["last_scan_sessions"],
                    "bound_count": self.c2_stats["last_bound_count"],
                    "visible_hit_count": self.c2_stats["last_visible_hit_count"],
                    "reason": reason,
                    "sidecar_run_id": payload.get("sidecar_run_id"),
                    "artifact_dir": sidecar_payload.get("artifact_dir"),
                    "screenshot_path": (payload.get("evidence") or {}).get("screenshot"),
                    "review_path": review_path,
                    "session_match_debug": self._visible_session_match_debug("", self.c2_last_visible_sessions),
                },
            )
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

    def _enqueue_visible_hits(self, payload: dict[str, Any], result: dict[str, Any] | None, *, sidecar_payload: dict[str, Any] | None = None) -> None:
        if not isinstance(result, dict):
            return
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        session_by_key = {str(item.get("rpa_session_key") or ""): item for item in sessions if isinstance(item, dict)}
        raw_sessions = (sidecar_payload or {}).get("sessions") if isinstance((sidecar_payload or {}).get("sessions"), list) else []
        raw_session_by_key = {str(item.get("session_key") or ""): item for item in raw_sessions if isinstance(item, dict)}
        queued_keys = {self._target_dedupe_key(item) for item in self.visible_hit_queue}
        for item in result.get("bindings") or []:
            if not isinstance(item, dict) or not item.get("can_ingest_messages"):
                continue
            conversation_id = str(item.get("conversation_id") or "")
            rpa_session_key = str(item.get("rpa_session_key") or "")
            session = session_by_key.get(rpa_session_key) or self._visible_session_for_binding(item, sessions) or {}
            visible_session_key = str(session.get("rpa_session_key") or "").strip()
            target = WechatReadTarget.from_api(
                {
                    "conversation_id": conversation_id,
                    "lead_id": item.get("lead_id"),
                    "sales_id": item.get("sales_id"),
                    "remark_code": item.get("remark_code"),
                    "rpa_session_key": visible_session_key or rpa_session_key,
                    "display_name": item.get("display_name") or session.get("display_name"),
                    "row_fingerprint": item.get("row_fingerprint") or session.get("row_fingerprint"),
                    "ocr_confidence": item.get("ocr_confidence") if item.get("ocr_confidence") is not None else session.get("ocr_confidence"),
                    "read_reason": "visible_hit",
                    "visible_session_candidate": self._sidecar_visible_session_candidate(raw_session_by_key.get(visible_session_key or rpa_session_key) or session),
                    "visible_session_source": "first_screen_session_scan",
                }
            )
            dedupe_key = self._target_dedupe_key(target)
            if not dedupe_key or dedupe_key in queued_keys or dedupe_key in self.c2_round_processed_conversation_ids:
                continue
            self.visible_hit_queue.append(target)
            queued_keys.add(dedupe_key)

    def _visible_session_for_binding(self, binding_item: dict[str, Any], sessions: list[Any]) -> dict[str, Any] | None:
        remark_code = str(binding_item.get("remark_code") or "").strip().upper()
        display_name = str(binding_item.get("display_name") or "").strip()
        compact_display = self._compact_identity_text(display_name)
        remark_matches: list[dict[str, Any]] = []
        display_matches: list[dict[str, Any]] = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            session_display = str(session.get("display_name") or session.get("name") or session.get("title") or "").strip()
            session_preview = str(session.get("last_message_preview") or session.get("content") or session.get("preview") or "").strip()
            candidates = {str(code or "").strip().upper() for code in (session.get("remark_code_candidates") or [])}
            session_identity = self._visible_session_identity_text(session)
            if remark_code and (remark_code in candidates or remark_code in session_identity):
                remark_matches.append(session)
            if compact_display and (
                compact_display == self._compact_identity_text(session_display)
                or compact_display in self._compact_identity_text(session_identity)
                or self._compact_identity_text(session_display) in compact_display
            ):
                display_matches.append(session)
        if len(remark_matches) == 1:
            return remark_matches[0]
        if len(display_matches) == 1:
            return display_matches[0]
        return None

    def _visible_sessions_for_remark_code(self, remark_code: str, sessions: list[Any]) -> list[dict[str, Any]]:
        code = str(remark_code or "").strip().upper()
        if not code:
            return []
        normalized_code = self._code_match_text(code)
        matches: list[dict[str, Any]] = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            candidates = {str(item or "").strip().upper() for item in (session.get("remark_code_candidates") or [])}
            normalized_candidates = {self._code_match_text(item) for item in candidates if item}
            identity = self._visible_session_identity_text(session)
            normalized_identity = self._code_match_text(identity)
            if code in candidates or normalized_code in normalized_candidates or code in identity or normalized_code in normalized_identity:
                matches.append(session)
        return matches

    @staticmethod
    def _compact_identity_text(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "")).upper()

    @staticmethod
    def _code_match_text(value: Any) -> str:
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

    def _visible_session_identity_text(self, session: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("display_name", "name", "title", "last_message_preview", "content", "preview"):
            value = session.get(key)
            if value:
                parts.append(str(value))
        candidates = session.get("remark_code_candidates")
        if isinstance(candidates, list):
            parts.extend(str(item) for item in candidates if item)
        row = session.get("row_fingerprint")
        if isinstance(row, dict):
            parts.extend(str(value) for value in row.values() if value)
        elif row:
            parts.append(str(row))
        return " ".join(parts).upper()

    def _visible_session_match_debug(self, remark_code: str, sessions: list[Any]) -> list[dict[str, Any]]:
        code = str(remark_code or "").strip().upper()
        normalized_code = self._code_match_text(code)
        debug_rows: list[dict[str, Any]] = []
        for index, session in enumerate(sessions[:12]):
            if not isinstance(session, dict):
                continue
            candidates = [str(item or "").strip().upper() for item in (session.get("remark_code_candidates") or []) if str(item or "").strip()]
            normalized_candidates = [self._code_match_text(item) for item in candidates if item]
            identity = self._visible_session_identity_text(session)
            normalized_identity = self._code_match_text(identity)
            debug_rows.append(
                {
                    "index": index,
                    "display_name": session.get("display_name") or session.get("name") or session.get("title"),
                    "rpa_session_key": session.get("rpa_session_key"),
                    "remark_code_candidates": candidates,
                    "last_message_preview": session.get("last_message_preview") or session.get("content") or session.get("preview"),
                    "ocr_confidence": session.get("ocr_confidence"),
                    "row_fingerprint": session.get("row_fingerprint"),
                    "identity_text": identity[:300],
                    "normalized_identity_text": normalized_identity[:300],
                    "match_checks": {
                        "candidate_exact": code in candidates,
                        "candidate_normalized": normalized_code in normalized_candidates,
                        "identity_exact_contains": bool(code and code in identity),
                        "identity_normalized_contains": bool(normalized_code and normalized_code in normalized_identity),
                    },
                }
            )
        return debug_rows

    def _authorized_visible_hit_target(
        self,
        visible_target: WechatReadTarget,
        authorized_target: WechatReadTarget,
    ) -> WechatReadTarget:
        return WechatReadTarget(
            conversation_id=authorized_target.conversation_id,
            lead_id=authorized_target.lead_id,
            sales_id=authorized_target.sales_id,
            rpa_session_key=visible_target.rpa_session_key or authorized_target.rpa_session_key,
            display_name=visible_target.display_name or authorized_target.display_name,
            remark_code=authorized_target.remark_code,
            row_fingerprint=visible_target.row_fingerprint or authorized_target.row_fingerprint,
            ocr_confidence=(
                visible_target.ocr_confidence
                if visible_target.ocr_confidence is not None
                else authorized_target.ocr_confidence
            ),
            read_reason="visible_hit",
            authorization_revision=authorized_target.authorization_revision,
            raw={
                **authorized_target.raw,
                **visible_target.raw,
                "authorization_revision": authorized_target.authorization_revision,
                "authorization_read_reason": authorized_target.read_reason,
            },
        )

    def _drain_visible_hit_queue(
        self,
        binding: Binding,
        *,
        authorized_targets: list[WechatReadTarget] | None = None,
    ) -> None:
        authorized_by_key = {
            self._target_dedupe_key(target): target
            for target in (authorized_targets or [])
            if self._target_dedupe_key(target)
        }
        if authorized_targets is not None and not authorized_by_key:
            dropped = len(self.visible_hit_queue)
            self.visible_hit_queue.clear()
            if dropped:
                append_log("INFO", "c2_visible_hit_queue_cleared", "后端 read-targets 为空，已清空本地第一屏命中读取队列。", metadata={"dropped_count": dropped})
            return
        while self.visible_hit_queue:
            visible_target = self.visible_hit_queue.pop(0)
            dedupe_key = self._target_dedupe_key(visible_target)
            authorized_target = authorized_by_key.get(dedupe_key)
            if authorized_targets is not None and authorized_target is None:
                append_log(
                    "INFO",
                    "c2_visible_hit_not_allowed",
                    "第一屏命中目标不在后端 read-targets 许可内，跳过本地读取。",
                    error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                    metadata={"conversation_id": visible_target.conversation_id, "remark_code": visible_target.remark_code, "read_reason": visible_target.read_reason},
                )
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                continue
            if authorized_target is None or not authorized_target.authorization_revision:
                self.c2_stats["last_error"] = "C2_TARGET_AUTHORIZATION_REVISION_MISSING"
                append_log(
                    "WARN",
                    "c2_visible_hit_authorization_missing",
                    "第一屏命中目标缺少当前 read-targets 授权版本，已禁止读取。",
                    error_code="C2_TARGET_AUTHORIZATION_REVISION_MISSING",
                    metadata={
                        "conversation_id": visible_target.conversation_id,
                        "remark_code": visible_target.remark_code,
                        "read_reason": visible_target.read_reason,
                    },
                )
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                continue
            target = self._authorized_visible_hit_target(visible_target, authorized_target)
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
            self.c2_round_processed_conversation_ids.add(dedupe_key)
            read_result = self._read_one_wechat_target(binding, target, current_step="visible_hit_message_read", enforce_read_targets=True)
            if read_result.get("ok"):
                self.c2_read_failure_cooldowns.pop(dedupe_key, None)
                self._mark_c2_read_success_cooldown(dedupe_key)
            else:
                self._mark_c2_read_failure_cooldown(dedupe_key, read_result.get("error_code"))

    def _read_bound_wechat_messages(self, binding: Binding) -> None:
        self._read_state_target_queue(binding)

    def _fetch_read_targets(self, binding: Binding) -> list[WechatReadTarget]:
        try:
            return list(self.api.get_wechat_read_targets(binding, limit=CONFIG.c2_read_targets_limit) or [])
        except Exception as exc:
            self.c2_stats["last_error"] = str(exc)
            append_log("ERROR", "c2_read_targets_failed", str(exc))
            return []

    def _read_state_target_queue(self, binding: Binding, *, targets: list[WechatReadTarget] | None = None) -> None:
        targets = self._fetch_read_targets(binding) if targets is None else list(targets)
        self.c2_read_allowlist_keys = {self._target_dedupe_key(target) for target in targets}
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
            success_cooldown_remaining = self._c2_read_success_cooldown_remaining(dedupe_key)
            if success_cooldown_remaining > 0:
                append_log("INFO", "c2_state_target_success_cooldown", "C2 读取目标刚完成，短冷却内跳过重复读取。", metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code, "cooldown_remaining_seconds": round(success_cooldown_remaining, 1)})
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
            read_result = self._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)
            if read_result.get("ok"):
                self.c2_round_processed_conversation_ids.add(dedupe_key)
                self.c2_read_failure_cooldowns.pop(dedupe_key, None)
                self._mark_c2_read_success_cooldown(dedupe_key)
            else:
                self._mark_c2_read_failure_cooldown(dedupe_key, read_result.get("error_code"))

    def _visible_target_from_recent_scan(
        self,
        target: WechatReadTarget,
        *,
        sessions: list[dict[str, Any]] | None = None,
        seen_at: float | None = None,
    ) -> WechatReadTarget | None:
        if target.read_reason == "visible_hit":
            return None
        scan_seen_at = float(self.c2_last_visible_sessions_monotonic if seen_at is None else seen_at)
        visible_sessions = (
            sessions
            if isinstance(sessions, list) and time.monotonic() - scan_seen_at <= C2_RECENT_VISIBLE_CACHE_TTL_SECONDS
            else self.c2_last_visible_sessions
            if isinstance(self.c2_last_visible_sessions, list) and time.monotonic() - scan_seen_at <= C2_RECENT_VISIBLE_CACHE_TTL_SECONDS
            else []
        )
        session = self._visible_session_for_binding(
            {
                "remark_code": target.remark_code,
                "display_name": target.display_name,
                "rpa_session_key": target.rpa_session_key,
            },
            visible_sessions,
        )
        if not session:
            self._prune_recent_visible_hits()
            recent = self.c2_recent_visible_hits_by_remark_code.get(str(target.remark_code or "").strip().upper())
            if isinstance(recent, dict):
                recent_session = recent.get("session")
                recent_seen_at = float(recent.get("seen_at") or 0)
                if isinstance(recent_session, dict) and time.monotonic() - recent_seen_at <= C2_RECENT_VISIBLE_CACHE_TTL_SECONDS:
                    session = recent_session
                    scan_seen_at = recent_seen_at
            if not session:
                return None
        visible_session_key = str(session.get("rpa_session_key") or "").strip()
        if not visible_session_key:
            return None
        append_log(
            "INFO",
            "c2_state_target_promoted_to_visible_hit",
            "状态机读取目标仍在最近第一屏扫描结果中，优先按首屏命中读取，避免重复定向搜索。",
            metadata={
                "conversation_id": target.conversation_id,
                "remark_code": target.remark_code,
                "rpa_session_key": visible_session_key,
                "display_name": session.get("display_name") or target.display_name,
            },
        )
        return WechatReadTarget(
            conversation_id=target.conversation_id,
            lead_id=target.lead_id,
            sales_id=target.sales_id,
            rpa_session_key=visible_session_key,
            display_name=str(session.get("display_name") or target.display_name or ""),
            remark_code=target.remark_code,
            row_fingerprint=session.get("row_fingerprint") or target.row_fingerprint,
            ocr_confidence=session.get("ocr_confidence") if session.get("ocr_confidence") is not None else target.ocr_confidence,
            read_reason="visible_hit",
            authorization_revision=target.authorization_revision,
            raw={
                **target.raw,
                "visible_session_candidate": self._sidecar_visible_session_candidate(session),
                "visible_session_source": "recent_visible_scan",
                "authorization_read_reason": target.read_reason,
            },
        )

    def _resolve_current_visible_target(self, target: WechatReadTarget) -> tuple[WechatReadTarget | None, dict[str, Any]]:
        sidecar_payload = self.bridge.list_sessions()
        payload = build_scan_result_payload(sidecar_payload)
        raw_sessions = sidecar_payload.get("sessions") if isinstance(sidecar_payload.get("sessions"), list) else []
        sessions = [
            item
            for item in self._visible_sessions_with_click_geometry(payload.get("sessions") or [], raw_sessions)
            if isinstance(item, dict)
        ]
        raw_session_by_key = {str(item.get("session_key") or ""): item for item in raw_sessions if isinstance(item, dict)}
        self.c2_last_visible_sessions = sessions
        self.c2_last_visible_sessions_monotonic = time.monotonic()
        self._remember_recent_visible_hits(sessions)
        metadata: dict[str, Any] = {
            "ok": bool(sidecar_payload.get("ok")),
            "state": sidecar_payload.get("state"),
            "error_code": sidecar_payload.get("error_code") or payload.get("error_code"),
            "sidecar_run_id": sidecar_payload.get("sidecar_run_id") or payload.get("sidecar_run_id"),
            "artifact_dir": sidecar_payload.get("artifact_dir"),
            "screenshot_path": (payload.get("evidence") or {}).get("screenshot") or sidecar_payload.get("screenshot_path"),
            "session_count": len(sessions),
            "remark_code": target.remark_code,
            "scan_id": payload.get("scan_id"),
        }
        matches = self._visible_sessions_for_remark_code(target.remark_code, sessions)
        metadata["match_count"] = len(matches)
        metadata["session_match_debug"] = self._visible_session_match_debug(target.remark_code, sessions)
        metadata["review_path"] = self._write_c2_sessions_review(
            reason="read_target_realtime_visible_check",
            sidecar_payload=sidecar_payload,
            scan_payload=payload,
            target=target,
            match_metadata={
                "remark_code": target.remark_code,
                "match_count": len(matches),
                "session_match_debug": metadata["session_match_debug"],
            },
        )
        if len(matches) > 1:
            metadata["matches"] = [
                {
                    "display_name": item.get("display_name"),
                    "rpa_session_key": item.get("rpa_session_key"),
                    "last_message_preview": item.get("last_message_preview"),
                    "ocr_confidence": item.get("ocr_confidence"),
                }
                for item in matches[:5]
            ]
            return None, metadata
        if len(matches) != 1:
            return None, metadata
        session = matches[0]
        visible_session_key = str(session.get("rpa_session_key") or "").strip()
        metadata["visible_session"] = {
            "display_name": session.get("display_name"),
            "rpa_session_key": visible_session_key,
            "last_message_preview": session.get("last_message_preview"),
            "ocr_confidence": session.get("ocr_confidence"),
            "candidate_passed_to_sidecar": bool(raw_session_by_key.get(visible_session_key)),
        }
        if not visible_session_key:
            metadata["error_code"] = "C2_VISIBLE_TARGET_SESSION_KEY_MISSING"
            return None, metadata
        return (
            WechatReadTarget(
                conversation_id=target.conversation_id,
                lead_id=target.lead_id,
                sales_id=target.sales_id,
                rpa_session_key=visible_session_key,
                display_name=str(session.get("display_name") or target.display_name or ""),
                remark_code=target.remark_code,
                row_fingerprint=session.get("row_fingerprint") or target.row_fingerprint,
                ocr_confidence=session.get("ocr_confidence") if session.get("ocr_confidence") is not None else target.ocr_confidence,
                read_reason="visible_hit",
                authorization_revision=target.authorization_revision,
            raw={
                **target.raw,
                "visible_session_candidate": self._sidecar_visible_session_candidate(raw_session_by_key.get(visible_session_key) or session),
                "visible_session_source": "read_target_realtime_visible_check",
                "authorization_read_reason": target.read_reason,
            },
            ),
            metadata,
        )

    def _backend_still_allows_read_target(self, binding: Binding, target: WechatReadTarget) -> bool:
        if not target.authorization_revision:
            self.c2_stats["last_error"] = "C2_TARGET_AUTHORIZATION_REVISION_MISSING"
            append_log(
                "WARN",
                "c2_message_read_cancelled_by_missing_authorization",
                "C2 V3 读取目标缺少授权版本，已在操作微信前取消。",
                error_code="C2_TARGET_AUTHORIZATION_REVISION_MISSING",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "read_reason": target.read_reason,
                },
            )
            return False
        current_targets = self._fetch_read_targets(binding)
        allowed_keys = {self._target_dedupe_key(item) for item in current_targets}
        allowed_authorization_keys = {
            self._target_authorization_key(item)
            for item in current_targets
            if item.authorization_revision
        }
        self.c2_read_allowlist_keys = allowed_keys
        target_key = self._target_dedupe_key(target)
        authorization_key = self._target_authorization_key(target)
        allowed = authorization_key in allowed_authorization_keys
        if not allowed:
            self.c2_stats["last_error"] = "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"
            append_log(
                "INFO",
                "c2_message_read_cancelled_by_read_targets",
                "后端 read-targets 已不再允许该目标，取消本地消息读取并释放 UI 锁。",
                error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "read_reason": target.read_reason,
                    "authorization_key": authorization_key,
                    "target_key": target_key,
                    "allowed_count": len(allowed_keys),
                },
            )
        return allowed

    def _backend_still_allows_read_target_for_voice(self, binding: Binding, target: WechatReadTarget) -> bool:
        if not self._backend_still_allows_read_target(binding, target):
            return False
        guard_seconds = max(0.0, float(self.c2_stop_guard_before_voice_seconds))
        if guard_seconds <= 0:
            return True
        self.stop_event.wait(guard_seconds)
        if self.stop_event.is_set():
            self.c2_stats["last_error"] = "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"
            append_log(
                "INFO",
                "c2_message_read_cancelled_by_local_stop",
                "客户端停止信号已触发，取消进入语音转写。",
                error_code="C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "read_reason": target.read_reason,
                },
            )
            return False
        return self._backend_still_allows_read_target(binding, target)

    def _target_dedupe_key(self, target: WechatReadTarget) -> str:
        if target.conversation_id and target.remark_code:
            return f"conversation:{target.conversation_id}:remark_code:{target.remark_code}"
        return f"invalid:{target.conversation_id}:{target.remark_code or ''}:{target.rpa_session_key}:{target.display_name}"

    def _target_authorization_key(self, target: WechatReadTarget) -> str:
        if target.authorization_revision:
            return f"authorization_revision:{target.conversation_id}:{target.authorization_revision}"
        reason = ""
        if isinstance(target.raw, dict):
            reason = str(target.raw.get("authorization_read_reason") or "").strip()
        reason = reason or str(target.read_reason or "").strip()
        if target.conversation_id and target.remark_code and reason and reason != "visible_hit":
            return f"authorization:{target.conversation_id}:remark_code:{target.remark_code}:read_reason:{reason}"
        return self._target_dedupe_key(target)

    def _validate_read_target(self, target: WechatReadTarget) -> str | None:
        if not target.conversation_id:
            return "C2_TARGET_CONVERSATION_ID_MISSING"
        if not target.remark_code:
            return "C2_TARGET_REMARK_CODE_MISSING"
        if str(target.remark_code).strip().upper() not in extract_remark_codes(target.remark_code):
            return "C2_TARGET_REMARK_CODE_INVALID"
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

    def _c2_read_success_cooldown_remaining(self, dedupe_key: str) -> float:
        until = float(self.c2_read_success_cooldowns.get(dedupe_key) or 0)
        remaining = until - time.monotonic()
        if remaining <= 0:
            self.c2_read_success_cooldowns.pop(dedupe_key, None)
            return 0.0
        return remaining

    def _mark_c2_read_failure_cooldown(self, dedupe_key: str, error_code: Any = None) -> None:
        cooldown = max(0.0, float(CONFIG.c2_message_failure_cooldown_seconds))
        if cooldown <= 0:
            return
        self.c2_read_failure_cooldowns[dedupe_key] = time.monotonic() + cooldown
        append_log("INFO", "c2_read_failure_cooldown_started", "C2 定向读取失败，已进入短冷却，避免反复重置微信搜索框。", metadata={"target_key": dedupe_key, "error_code": error_code, "cooldown_seconds": cooldown})

    def _mark_c2_read_success_cooldown(self, dedupe_key: str) -> None:
        cooldown = max(float(CONFIG.c2_message_read_interval_seconds), 20.0)
        self.c2_read_success_cooldowns[dedupe_key] = time.monotonic() + cooldown
        append_log("INFO", "c2_read_success_cooldown_started", "C2 读取完成，已进入短冷却，避免服务端状态更新前重复读取同一目标。", metadata={"target_key": dedupe_key, "cooldown_seconds": cooldown})

    def _read_one_wechat_target(
        self,
        binding: Binding,
        target: WechatReadTarget,
        *,
        current_step: str = "message_read",
        allow_during_current_task: bool = False,
        enforce_read_targets: bool = False,
    ) -> dict[str, Any]:
        owner = f"{binding.worker_id}:{binding.client_instance_id}:message_ingest:{target.conversation_id}"
        lease: UiLockLease | None = None
        flow_started_at = time.perf_counter()
        flow_timing: dict[str, Any] = {
            "schema_version": 1,
            "flow": "c2_message_read",
            "conversation_id": target.conversation_id,
            "remark_code": target.remark_code,
            "phases": [],
        }

        def record_phase(name: str, started_at: float, **metadata: Any) -> None:
            phase = {
                "name": name,
                "duration_seconds": round(max(0.0, time.perf_counter() - started_at), 4),
            }
            phase.update({key: value for key, value in metadata.items() if value is not None})
            flow_timing["phases"].append(phase)

        last_authorization_check = 0.0

        def action_cancel_requested() -> bool:
            nonlocal last_authorization_check
            if self.stop_event.is_set():
                return True
            if not enforce_read_targets:
                return False
            now = time.monotonic()
            if now - last_authorization_check < 1.0:
                return False
            last_authorization_check = now
            return not self._backend_still_allows_read_target(binding, target)

        try:
            if not str(target.authorization_revision or "").strip():
                self.c2_stats["last_error"] = "C2_TARGET_AUTHORIZATION_REVISION_MISSING"
                append_log(
                    "WARN",
                    "c2_message_read_blocked_by_missing_authorization",
                    "C2 目标缺少后端签发的当前授权票据，未执行任何微信操作。",
                    error_code="C2_TARGET_AUTHORIZATION_REVISION_MISSING",
                    metadata={"conversation_id": target.conversation_id, "remark_code": target.remark_code},
                )
                return {"ok": False, "error_code": "C2_TARGET_AUTHORIZATION_REVISION_MISSING"}
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
            if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"}
            effective_target = target
            target_label = effective_target.display_name or effective_target.remark_code or ""
            real_time_visible_metadata: dict[str, Any] = {}
            visible_source = ""
            fallback_target_mode = ""
            if target.read_reason == "visible_hit":
                base_target_mode = "visible"
                visible_source = "visible_hit_queue"
            else:
                previous_visible_sessions = list(self.c2_last_visible_sessions) if isinstance(self.c2_last_visible_sessions, list) else []
                previous_visible_sessions_seen_at = float(self.c2_last_visible_sessions_monotonic or 0)
                recent_visible_target = self._visible_target_from_recent_scan(
                    target,
                    sessions=previous_visible_sessions,
                    seen_at=previous_visible_sessions_seen_at,
                )
                if recent_visible_target:
                    effective_target = recent_visible_target
                    target_label = effective_target.display_name or effective_target.remark_code or ""
                    visible_source = "recent_visible_scan_hint"
                    if isinstance(effective_target.raw, dict):
                        effective_target.raw["authorization_read_reason"] = target.read_reason
                else:
                    visible_source = "atomic_visible_scan"
                base_target_mode = "visible"
                fallback_target_mode = "search_by_remark_code" if effective_target.remark_code else ""
                real_time_visible_metadata = {
                    "merged_into_locate": True,
                    "reason": "fresh_visible_scan_and_click_share_one_sidecar_frame",
                    "recent_hint_used": bool(recent_visible_target),
                }
                append_log(
                    "INFO",
                    "c2_state_target_visible_check_merged",
                    "C2 状态目标的实时首屏检查已合并到定位动作，使用同一张新截图匹配并点击。",
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "visible_source": visible_source,
                        "visible_scan": real_time_visible_metadata,
                    },
                )
            if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS"}
            lease.update_step("target_chat_locating")
            self.current_step = "target_chat_locating"
            target_cache_key = self._target_dedupe_key(target)
            cache = self.c2_active_target_cache if isinstance(self.c2_active_target_cache, dict) else {}
            can_try_current = (
                cache.get("target_key") == target_cache_key
                and float(cache.get("expires_at") or 0) > time.monotonic()
            )
            if base_target_mode == "visible" and effective_target.remark_code and not fallback_target_mode:
                fallback_target_mode = "search_by_remark_code"
            locate_modes = ["current", base_target_mode] if can_try_current else [base_target_mode]
            if fallback_target_mode and fallback_target_mode not in locate_modes:
                locate_modes.append(fallback_target_mode)
            locate_payload: dict[str, Any] = {}
            for locate_mode in locate_modes:
                visible_target = locate_mode == "visible"
                phase_started_at = time.perf_counter()
                locate_payload = self.bridge.locate_chat(
                    display_name=target_label,
                    rpa_session_key=effective_target.rpa_session_key if visible_target else "",
                    remark_code=effective_target.remark_code or "",
                    target_mode=locate_mode,
                    visible_session_candidate=effective_target.raw.get("visible_session_candidate") if visible_target and isinstance(effective_target.raw, dict) else None,
                    max_duration_seconds=20 if locate_mode == "current" else 30 if visible_target else 90,
                    cancel_check=action_cancel_requested,
                )
                record_phase(
                    "target_chat_locate",
                    phase_started_at,
                    target_mode=locate_mode,
                    state=locate_payload.get("state"),
                    sidecar_run_id=locate_payload.get("sidecar_run_id"),
                )
                attempt_ok = bool(locate_payload.get("ok"))
                append_log(
                    "INFO" if attempt_ok else "WARN",
                    "c2_target_chat_locate_attempt",
                    f"C2 目标会话定位尝试：{locate_mode}。",
                    error_code=None
                    if attempt_ok
                    else str(locate_payload.get("error_code") or locate_payload.get("state") or "TARGET_NOT_CONFIRMED"),
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "target_mode": locate_mode,
                        "visible_source": visible_source,
                        "state": locate_payload.get("state"),
                        "sidecar_run_id": locate_payload.get("sidecar_run_id"),
                        "artifact_dir": locate_payload.get("artifact_dir"),
                        "review_path": locate_payload.get("review_path"),
                        "targeting": locate_payload.get("targeting"),
                        "step_events": locate_payload.get("step_events"),
                        "open_chat_timing": locate_payload.get("open_chat_timing"),
                    },
                )
                if locate_payload.get("ok"):
                    break
                if str(locate_payload.get("error_code") or "") in C2_LOCATE_TERMINAL_ERROR_CODES:
                    break
            locate_payload["visible_scan"] = real_time_visible_metadata
            append_log(
                "INFO" if locate_payload.get("ok") else "WARN",
                "c2_target_chat_located",
                "C2 目标会话定位完成。" if locate_payload.get("ok") else "C2 目标会话定位失败。",
                error_code=None if locate_payload.get("ok") else str(locate_payload.get("error_code") or locate_payload.get("state") or "TARGET_NOT_CONFIRMED"),
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "state": locate_payload.get("state"),
                    "sidecar_run_id": locate_payload.get("sidecar_run_id"),
                    "artifact_dir": locate_payload.get("artifact_dir"),
                    "review_path": locate_payload.get("review_path"),
                    "target_mode": locate_payload.get("target_mode"),
                    "visible_source": visible_source,
                    "attempted_target_modes": locate_modes,
                    "targeting": locate_payload.get("targeting"),
                    "step_events": locate_payload.get("step_events"),
                    "open_chat_timing": locate_payload.get("open_chat_timing"),
                    "visible_scan": real_time_visible_metadata,
                },
            )
            if not locate_payload.get("ok"):
                code = str(locate_payload.get("error_code") or locate_payload.get("state") or "TARGET_NOT_CONFIRMED")
                self.c2_stats["last_error"] = code
                return {"ok": False, "error_code": code, "target_confirmation": locate_payload}
            self.c2_active_target_cache = {
                "target_key": target_cache_key,
                "conversation_id": target.conversation_id,
                "remark_code": target.remark_code,
                "expires_at": time.monotonic() + 120.0,
            }
            if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "target_confirmation": locate_payload}
            lease.update_step(current_step)
            self.current_step = current_step
            phase_started_at = time.perf_counter()
            sidecar_payload = self.bridge.get_messages(
                display_name=target_label,
                rpa_session_key="",
                remark_code=effective_target.remark_code or "",
                target_mode="current",
                max_duration_seconds=20,
                cancel_check=action_cancel_requested,
            )
            record_phase(
                "initial_message_read",
                phase_started_at,
                state=sidecar_payload.get("state"),
                sidecar_run_id=sidecar_payload.get("sidecar_run_id"),
            )
            sidecar_payload["target_confirmation"] = locate_payload
            sidecar_payload["authoritative_frame_source"] = "initial_read"
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
            initial_contract_error = sidecar_contract_error(sidecar_payload)
            if initial_contract_error:
                self.c2_stats["last_error"] = initial_contract_error
                append_log(
                    "WARN",
                    "c2_sidecar_contract_invalid",
                    "OmniAuto 消息观察不符合当前唯一 C2 合同，已在任何语音操作和入库前阻断。",
                    error_code=initial_contract_error,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "contract_revision": sidecar_payload.get("contract_revision"),
                        "contract_sha256": sidecar_payload.get("contract_sha256"),
                        "observation_validation_errors": sidecar_payload.get("observation_validation_errors"),
                    },
                )
                return {
                    "ok": False,
                    "error_code": initial_contract_error,
                    "target_confirmation": locate_payload,
                    "initial_messages": sidecar_payload,
                }
            if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "target_confirmation": locate_payload, "initial_messages": sidecar_payload}
            voice_binding_guard_key = (
                f"{target_cache_key}:{str(target.authorization_revision).strip()}"
            )
            if voice_binding_guard_key in self.c2_voice_binding_blocked_authorizations:
                code = "C2_VOICE_TRANSCRIPT_BINDING_PENDING"
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_messages_ingest_blocked_by_voice_binding",
                    "此前已出现可见语音正文与稳定锚点绑定矛盾，本授权轮次禁止部分上报。",
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "authorization_revision": target.authorization_revision,
                    },
                )
                return {
                    "ok": False,
                    "error_code": code,
                    "target_confirmation": locate_payload,
                    "initial_messages": sidecar_payload,
                }
            if _messages_need_voice_transcribe(sidecar_payload):
                if enforce_read_targets and not self._backend_still_allows_read_target_for_voice(binding, target):
                    return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "target_confirmation": locate_payload, "initial_messages": sidecar_payload}
                lease.update_step("voice_transcribe_current_chat")
                self.current_step = "voice_transcribe_current_chat"
                phase_started_at = time.perf_counter()
                voice_payload = self.bridge.voice_transcribe(
                    display_name=target_label,
                    rpa_session_key="",
                    remark_code=effective_target.remark_code or "",
                    target_mode="current",
                    max_duration_seconds=CONFIG.c2_voice_transcribe_max_duration_seconds,
                    cancel_check=action_cancel_requested,
                )
                record_phase(
                    "voice_transcribe",
                    phase_started_at,
                    state=voice_payload.get("state"),
                    sidecar_run_id=voice_payload.get("sidecar_run_id"),
                    timing=voice_payload.get("timing") if isinstance(voice_payload.get("timing"), dict) else None,
                )
                voice_contract_error = sidecar_contract_error(voice_payload, require_observations=False)
                if voice_contract_error:
                    self.c2_stats["last_error"] = voice_contract_error
                    append_log(
                        "WARN",
                        "c2_voice_sidecar_contract_invalid",
                        "OmniAuto 语音动作返回的合同指纹不一致，已阻断后续读取和入库。",
                        error_code=voice_contract_error,
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "contract_revision": voice_payload.get("contract_revision"),
                            "contract_sha256": voice_payload.get("contract_sha256"),
                        },
                    )
                    return {
                        "ok": False,
                        "error_code": voice_contract_error,
                        "target_confirmation": locate_payload,
                        "initial_messages": sidecar_payload,
                        "voice_transcription": voice_payload,
                    }
                voice_state = str(voice_payload.get("state") or voice_payload.get("error_code") or "").strip()
                voice_fatal_states = {
                    "target_not_confirmed_for_voice_transcribe",
                    "voice_transcribe_target_not_found",
                    "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE",
                }
                voice_blocking_states = voice_fatal_states | {
                    "voice_transcribe_click_failed",
                    "VOICE_TRANSCRIBE_CLICK_FAILED",
                    "RPA_SIDECAR_TIMEOUT",
                    "RPA_SIDECAR_PROTOCOL_INVALID",
                    "RPA_SIDECAR_CRASHED",
                    "voice_transcribe_cancelled",
                    "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                }
                voice_success_states = {
                    "voice_transcribe_completed",
                    "voice_transcribe_partial",
                    # The initial OCR can conservatively flag voice-like noise.
                    # A fresh structural scan finding no voice is safe to continue.
                    "voice_transcribe_no_visible_voice",
                }
                append_log(
                    "INFO" if voice_state in voice_success_states else "WARN",
                    "c2_voice_transcribe_finished",
                    "C2 语音转文字调用完成。",
                    error_code=None if voice_state in voice_success_states else str(voice_payload.get("error_code") or voice_state or "VOICE_TRANSCRIBE_FAILED"),
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "state": voice_state,
                        "sidecar_run_id": voice_payload.get("sidecar_run_id"),
                        "artifact_dir": voice_payload.get("artifact_dir"),
                        "review_path": voice_payload.get("review_path"),
                        "target_mode": voice_payload.get("target_mode"),
                        "transcribed_count": len(voice_payload.get("transcribed_messages") or []) if isinstance(voice_payload.get("transcribed_messages"), list) else 0,
                        "timing": voice_payload.get("timing") if isinstance(voice_payload.get("timing"), dict) else None,
                    },
                )
                if _voice_payload_has_unbound_transcript(voice_payload):
                    code = "VOICE_TRANSCRIPT_BINDING_INCONSISTENT"
                    self.c2_voice_binding_blocked_authorizations.add(voice_binding_guard_key)
                    if len(self.c2_voice_binding_blocked_authorizations) > 128:
                        self.c2_voice_binding_blocked_authorizations = {voice_binding_guard_key}
                    self.c2_stats["last_error"] = code
                    append_log(
                        "WARN",
                        "c2_voice_transcript_binding_inconsistent",
                        "OCR 已识别完整语音正文，但 sidecar 未绑定到稳定语音锚点。",
                        error_code=code,
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "authorization_revision": target.authorization_revision,
                            "state": voice_state,
                            "sidecar_run_id": voice_payload.get("sidecar_run_id"),
                            "new_message_count": len(voice_payload.get("new_messages") or []),
                            "transcribed_count": len(voice_payload.get("transcribed_messages") or []),
                        },
                    )
                    return {
                        "ok": False,
                        "error_code": code,
                        "target_confirmation": locate_payload,
                        "initial_messages": sidecar_payload,
                        "voice_transcription": voice_payload,
                    }
                if voice_state in voice_blocking_states or voice_state not in voice_success_states:
                    code = str(voice_payload.get("error_code") or voice_state or "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE")
                    self.c2_stats["last_error"] = code
                    return {"ok": False, "error_code": code, "target_confirmation": locate_payload, "initial_messages": sidecar_payload, "voice_transcription": voice_payload}
                if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                    return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "target_confirmation": locate_payload, "initial_messages": sidecar_payload, "voice_transcription": voice_payload}
                # The final messages frame now confirms the active target before
                # parsing messages. Keep the observable reconfirming step, but
                # do not start a separate sidecar process for an unchanged UI.
                lease.update_step("target_chat_reconfirming")
                self.current_step = "target_chat_reconfirming"
                phase_started_at = time.perf_counter()
                transcribed_payload = self.bridge.get_messages(
                    display_name=target_label,
                    rpa_session_key="",
                    remark_code=effective_target.remark_code or "",
                    target_mode="current",
                    max_duration_seconds=20,
                    cancel_check=action_cancel_requested,
                )
                record_phase(
                    "target_chat_reconfirm_and_final_read",
                    phase_started_at,
                    state=transcribed_payload.get("state"),
                    sidecar_run_id=transcribed_payload.get("sidecar_run_id"),
                )
                if not transcribed_payload.get("ok"):
                    code = str(transcribed_payload.get("error_code") or transcribed_payload.get("state") or "TARGET_NOT_CONFIRMED_FOR_MESSAGES")
                    self.c2_stats["last_error"] = code
                    append_log(
                        "WARN",
                        "c2_message_read_sidecar_failed",
                        "C2 语音转写后消息读取失败。",
                        error_code=code,
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "state": transcribed_payload.get("state"),
                            "sidecar_run_id": transcribed_payload.get("sidecar_run_id"),
                            "artifact_dir": transcribed_payload.get("artifact_dir"),
                            "review_path": transcribed_payload.get("review_path"),
                            "target_mode": transcribed_payload.get("target_mode"),
                        },
                    )
                    return {
                        "ok": False,
                        "error_code": code,
                        "target_confirmation": locate_payload,
                        "initial_messages": sidecar_payload,
                        "voice_transcription": voice_payload,
                        "target_reconfirmation": transcribed_payload.get("target_confirmation"),
                    }
                final_contract_error = sidecar_contract_error(transcribed_payload)
                if final_contract_error:
                    self.c2_stats["last_error"] = final_contract_error
                    append_log(
                        "WARN",
                        "c2_final_sidecar_contract_invalid",
                        "OmniAuto 最终权威画面不符合当前唯一 C2 合同，已阻断入库。",
                        error_code=final_contract_error,
                        metadata={
                            "conversation_id": target.conversation_id,
                            "remark_code": target.remark_code,
                            "contract_revision": transcribed_payload.get("contract_revision"),
                            "contract_sha256": transcribed_payload.get("contract_sha256"),
                            "observation_validation_errors": transcribed_payload.get("observation_validation_errors"),
                        },
                    )
                    return {
                        "ok": False,
                        "error_code": final_contract_error,
                        "target_confirmation": locate_payload,
                        "initial_messages": sidecar_payload,
                        "voice_transcription": voice_payload,
                        "final_messages": transcribed_payload,
                    }
                if enforce_read_targets and not self._backend_still_allows_read_target(binding, target):
                    return {"ok": False, "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS", "target_confirmation": locate_payload, "initial_messages": sidecar_payload, "voice_transcription": voice_payload, "target_reconfirmation": transcribed_payload.get("target_confirmation")}
                target_reconfirmation = transcribed_payload.get("target_confirmation") if isinstance(transcribed_payload.get("target_confirmation"), dict) else {}
                lease.update_step(current_step)
                self.current_step = current_step
                transcribed_payload["target_confirmation"] = locate_payload
                transcribed_payload["target_reconfirmation"] = target_reconfirmation
                transcribed_payload["voice_transcription"] = voice_payload
                transcribed_payload["initial_messages"] = sidecar_payload
                transcribed_payload["authoritative_frame_source"] = "final_read"
                sidecar_payload = transcribed_payload
            phase_started_at = time.perf_counter()
            try:
                payload = build_message_ingest_payload(target, sidecar_payload)
            except ValueError as exc:
                code = str(exc)
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_messages_ingest_blocked_by_missing_authorization",
                    "C2 V3 入库请求不符合唯一合同，已在发送前阻断。",
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "read_reason": target.read_reason,
                    },
                )
                return {
                    "ok": False,
                    "error_code": code,
                    "target_confirmation": locate_payload,
                    "initial_messages": sidecar_payload,
                }
            record_phase("build_ingest_payload", phase_started_at, message_count=len(payload.get("messages") or []))
            local_validation_errors = (
                (payload.get("evidence") or {}).get("observation_validation_errors")
                if isinstance(payload.get("evidence"), dict)
                else []
            )
            if not isinstance(local_validation_errors, list):
                local_validation_errors = []
            if local_validation_errors:
                code = str(local_validation_errors[0].get("error_code") or "C2_OBSERVATION_CONTRACT_INVALID")
                self.c2_stats["last_error"] = code
                append_log(
                    "WARN",
                    "c2_messages_ingest_blocked_by_contract",
                    "C2 observation 存在合同冲突，整批已在调用后端前阻断。",
                    error_code=code,
                    metadata={
                        "conversation_id": target.conversation_id,
                        "remark_code": target.remark_code,
                        "local_validation_errors": local_validation_errors,
                    },
                )
                return {
                    "ok": False,
                    "error_code": code,
                    "payload": payload,
                    "local_validation_errors": local_validation_errors,
                }
            if isinstance(payload.get("evidence"), dict):
                evidence_timing = {
                    **flow_timing,
                    "elapsed_before_ingest_seconds": round(max(0.0, time.perf_counter() - flow_started_at), 4),
                }
                payload["evidence"]["timing"] = json.loads(json.dumps(evidence_timing, ensure_ascii=False, default=str))
            phase_started_at = time.perf_counter()
            result = self.api.post_wechat_messages_ingest(binding, payload)
            record_phase(
                "messages_ingest_api",
                phase_started_at,
                ingested_count=result.get("ingested_count") if isinstance(result, dict) else None,
                ignored_count=result.get("ignored_count") if isinstance(result, dict) else None,
            )
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
            ignored_results = [
                item
                for item in (result.get("results") or [])
                if isinstance(item, dict) and item.get("ingest_result") == "ignored"
            ]
            ingest_error_code = ""
            if local_validation_errors:
                ingest_error_code = str(local_validation_errors[0].get("error_code") or "C2_OBSERVATION_CONTRACT_INVALID")
            elif ignored_results:
                ingest_error_code = str(ignored_results[0].get("error_code") or "C2_MESSAGE_INGEST_IGNORED")
            elif int(result.get("ignored_count") or 0) > 0:
                ingest_error_code = "C2_MESSAGE_INGEST_IGNORED"
            self.c2_stats.update(
                {
                    "last_message_read_at": (payload.get("evidence") or {}).get("finished_at") if isinstance(payload.get("evidence"), dict) else None,
                    "last_ingested_count": result.get("ingested_count") if isinstance(result, dict) else 0,
                    "last_error": ingest_error_code or None,
                }
            )
            append_log(
                "WARN" if ingest_error_code else "INFO",
                "c2_messages_ingest_partial_failure" if ingest_error_code else "c2_messages_ingested",
                "微信消息存在合同校验失败或被后端忽略。" if ingest_error_code else "微信消息读取结果已上报。",
                error_code=ingest_error_code or None,
                metadata={
                    "conversation_id": target.conversation_id,
                    "remark_code": target.remark_code,
                    "message_count": len(payload.get("messages") or []),
                    "ingested_count": self.c2_stats["last_ingested_count"],
                    "ignored_count": int(result.get("ignored_count") or 0),
                    "local_validation_errors": local_validation_errors,
                    "ignored_results": ignored_results,
                },
            )
            return {
                "ok": not bool(ingest_error_code),
                "error_code": ingest_error_code or None,
                "result": result,
                "payload": payload,
                "new_customer_message_count": new_customer_message_count,
            }
        except UiLockError as exc:
            code = "voice_transcribe_lock_timeout" if exc.code in {"UI_LOCK_BUSY", "UI_LOCK_ACQUIRE_TIMEOUT"} else exc.code
            self.c2_stats["last_error"] = code
            append_log("INFO", "c2_message_read_interrupted", "C2 消息读取未拿到锁，按低优先级中断处理。", error_code=code, metadata={"ui_lock": exc.data, "lock_error_code": exc.code})
            return {"ok": False, "error_code": code}
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
            flow_timing["total_duration_seconds"] = round(max(0.0, time.perf_counter() - flow_started_at), 4)
            try:
                append_log(
                    "INFO",
                    "c2_message_read_timing",
                    "C2 消息读取耗时账本。",
                    metadata=flow_timing,
                )
            except Exception:
                pass
            self.current_step = None
