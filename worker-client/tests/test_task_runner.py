from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.api import ApiError
from chejin_worker_client.action_journal import (
    action_journal_path,
    action_journal_phase,
    initialize_action_journal,
    read_action_journal,
    update_action_journal_item,
)
from chejin_worker_client.c2_contract import contract_revision, contract_sha256
from chejin_worker_client.models import Binding, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from chejin_worker_client.rpa_bridge import RpaBridge
from chejin_worker_client.storage import (
    checkpoint_c2_action_outcomes,
    db_connection,
    enqueue_c2_outbox,
    list_c2_action_journal,
    list_c2_outbox_waiting,
    load_c2_state,
    load_c2_ledger_entry,
    load_c2_outbox_entry,
    load_reply_send_ack_outbox,
    save_c2_state,
    save_c2_ledger_terminal,
    save_reply_send_intent,
)
from chejin_worker_client.task_runner import (
    C2_RECENT_VISIBLE_CACHE_TTL_SECONDS,
    TaskLeaseGuard,
    TaskRunner,
    image_operation_gate_errors,
)
from chejin_worker_client.transaction_outcomes import (
    FlowOutcomeAccumulator,
    merge_item_outcomes,
)
from chejin_worker_client.ui_lock import LOCK_FILE, UiLockError
from chejin_worker_client.wechat_c2 import image_observation_source_key, voice_observation_source_key


class FakeApi:
    def __init__(self, task: Task | None, result_mode: str = "success", claim_response: Task | None = None) -> None:
        self.task = task
        self.claim_response = claim_response
        self.result_mode = result_mode
        self.events: list[str] = []
        self.evidence_payloads: list[dict] = []
        self.run_status_updates: list[str] = []
        self.run_status_error: Exception | None = None
        self.claim_send_error: Exception | None = None
        self.claim_send_duplicated = False
        self.claim_send_callback = None
        self.sent_ack_error: Exception | None = None
        self.scan_payloads: list[dict] = []
        self.message_payloads: list[dict] = []
        self.read_targets: list[WechatReadTarget] = []
        self.message_ingest_result = "ingested"
        self.friend_activation_payloads: list[dict] = []
        self.message_batch_statuses: list[dict] = []
        self.message_batch_result: dict | None = None
        self.heartbeat_payloads: list[dict] = []
        self.message_ingest_error: Exception | None = None
        self.claim_reply_text = "您好，可以继续沟通这台车。"
        self.claim_reply_hash = hashlib.sha256(self.claim_reply_text.encode("utf-8")).hexdigest()

    def heartbeat(self, binding: Binding, **kwargs):
        self.heartbeat_payloads.append(dict(kwargs))
        self.events.append(f"heartbeat:{kwargs['rpa_component_status']}:{kwargs['wechat_status']}")
        return WorkerProfile(id=binding.worker_id, worker_name="测试 Worker", run_status=binding.run_status)

    def pull_task(self, binding: Binding):
        self.events.append("pull")
        if self.task:
            if self.task.status == "running" and self.task.lease_fencing_token <= 0:
                self.task.lease_fencing_token = 1
            if self.task.status == "running" and not self.task.lease_expires_at:
                self.task.lease_expires_at = "2099-01-01T00:00:00+00:00"
            return ("running" if self.task.status == "running" else "pending", self.task, None)
        return ("idle", None, "NO_PENDING_TASK")

    def claim_task(self, binding: Binding, task: Task, **kwargs):
        self.events.append(f"claim:{task.id}")
        if kwargs:
            self.events.append(f"claim_source:{kwargs.get('claim_source')}:{kwargs.get('conversation_id')}")
        claimed = self.claim_response or task
        if claimed.lease_fencing_token <= 0:
            claimed.lease_fencing_token = 1
        if not claimed.lease_expires_at:
            claimed.lease_expires_at = "2099-01-01T00:00:00+00:00"
        return claimed

    def renew_task_lease(self, binding: Binding, task_id: str, *, current_step: str | None):
        self.events.append(f"renew_task_lease:{task_id}:{current_step}")
        task = self.task or self.claim_response or Task(
            id=task_id,
            task_type="add_friend",
            status="running",
        )
        if task.lease_fencing_token <= 0:
            task.lease_fencing_token = 1
        task.lease_expires_at = "2099-01-01T00:00:00+00:00"
        return task

    def report_step(self, binding: Binding, task_id: str, current_step: str, remark: str):
        self.events.append(f"step:{current_step}")
        return self.task

    def claim_send(self, binding: Binding, task: Task):
        self.events.append(f"claim_send:{task.reply_action_id}")
        if self.claim_send_callback is not None:
            self.claim_send_callback(task)
        if self.claim_send_error:
            raise self.claim_send_error
        from chejin_worker_client.models import ReplySendClaim

        return ReplySendClaim(
            reply_action_id=task.reply_action_id or "reply-action-1",
            task_id=task.id,
            send_token="send-token-1",
            reply_text=self.claim_reply_text,
            reply_text_hash=self.claim_reply_hash,
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            expire_at=None,
            raw={
                "remark_code": "CJTEST01",
                "display_name": "CJTEST01 许聪",
                "authorization_revision": "revision-conv-1",
                "duplicated": self.claim_send_duplicated,
            },
        )

    def sent_ack(self, binding: Binding, claim, **kwargs):
        self.events.append(f"sent_ack:{kwargs['send_result']}:{kwargs.get('error_code')}")
        if self.sent_ack_error:
            raise self.sent_ack_error
        return {"task": self.task, "ack": kwargs}

    def complete_invite_sent(self, binding: Binding, task_id: str):
        self.events.append(f"complete_invite_sent:{task_id}")
        return self.task

    def complete_already_friend(self, binding: Binding, task_id: str):
        self.events.append(f"complete_already_friend:{task_id}")
        return self.task

    def fail_task(self, binding: Binding, task_id: str, error_code: str, failure_step: str | None, message: str):
        self.events.append(f"fail:{error_code}:{failure_step}")
        return self.task

    def upload_evidence(self, binding: Binding, task_id: str, content: str, **kwargs):
        self.evidence_payloads.append({"task_id": task_id, "content": content, **kwargs})
        self.events.append(f"evidence:{kwargs.get('error_code')}")

    def set_run_status(self, binding: Binding, run_status: str):
        if self.run_status_error:
            raise self.run_status_error
        self.run_status_updates.append(run_status)
        self.events.append(f"run_status:{run_status}")
        return WorkerProfile(id=binding.worker_id, worker_name="测试 Worker", run_status=run_status)

    def post_wechat_session_scan_result(self, binding: Binding, payload: dict):
        self.scan_payloads.append(payload)
        self.events.append(f"scan:{len(payload.get('sessions') or [])}:{payload.get('error_code')}")
        session = (payload.get("sessions") or [{}])[0] if payload.get("sessions") else {}
        return {
            "bound_count": 1,
            "bindings": [
                {
                    "conversation_id": "conv-1",
                    "lead_id": "lead-1",
                    "sales_id": "sales-1",
                    "remark_code": "CJTEST01",
                    "rpa_session_key": session.get("rpa_session_key") or "wx:rpa:v1:a",
                    "display_name": session.get("display_name") or "CJTEST01 许聪",
                    "row_fingerprint": session.get("row_fingerprint") or {"title_text": "CJTEST01 许聪"},
                    "ocr_confidence": session.get("ocr_confidence") or 0.98,
                    "can_ingest_messages": True,
                }
            ],
        }

    def get_wechat_read_targets(self, binding: Binding, *, limit: int = 20):
        self.events.append(f"read_targets:{limit}")
        targets = self.read_targets[:limit]
        for target in targets:
            if target.conversation_id and target.remark_code and not target.authorization_revision:
                target.authorization_revision = f"revision-{target.conversation_id}"
        return targets

    def get_wechat_read_authorization(
        self,
        binding: Binding,
        conversation_id: str,
        *,
        continuation_batch_id: str | None = None,
        continuation_token: str | None = None,
    ):
        self.events.append(f"read_authorization:{conversation_id}")
        target = next(
            (
                item
                for item in self.read_targets
                if item.conversation_id == conversation_id
            ),
            None,
        )
        if target is None:
            return {
                "allowed": False,
                "conversation_id": conversation_id,
                "authorization_revision": "",
                "read_reason": "",
            }
        if not target.authorization_revision:
            target.authorization_revision = f"revision-{target.conversation_id}"
        return {
            "allowed": True,
            "conversation_id": target.conversation_id,
            "authorization_revision": target.authorization_revision,
            "read_reason": target.read_reason,
            **(
                {
                    "authorization_scope": "batch_continuation",
                    "batch_id": continuation_batch_id,
                    "continuation_token": continuation_token,
                }
                if continuation_batch_id and continuation_token
                else {}
            ),
        }

    def confirm_wechat_friend_activation(self, binding: Binding, target: WechatReadTarget, **kwargs):
        self.friend_activation_payloads.append(
            {"conversation_id": target.conversation_id, "authorization_revision": target.authorization_revision, **kwargs}
        )
        self.events.append(f"friend_activation:{target.conversation_id}")
        return {
            "conversation_id": target.conversation_id,
            "friend_state": "friend_active",
            "conversation_status": "friend_activation_reading",
            "activation_confirmed": True,
            "authorization_revision": target.authorization_revision,
            "next_action": "read_current_chat",
        }

    def post_wechat_messages_ingest(self, binding: Binding, payload: dict):
        if self.message_ingest_error is not None:
            raise self.message_ingest_error
        self.message_payloads.append(payload)
        self.events.append(f"ingest:{len(payload.get('messages') or [])}")
        messages = payload.get("messages") or []
        response = {
            "ingested_count": len(messages) if self.message_ingest_result == "ingested" else 0,
            "ignored_count": len(messages) if self.message_ingest_result == "ignored" else 0,
            "results": [
                {
                    "source_message_key": item.get("source_message_key"),
                    "dedupe_key": item.get("dedupe_key"),
                    "ingest_result": self.message_ingest_result,
                    **(
                        {"error_code": "MESSAGE_ROW_ROLE_SOURCE_UNTRUSTED"}
                        if self.message_ingest_result == "ignored"
                        else {}
                    ),
                }
                for item in messages
                if isinstance(item, dict)
            ],
        }
        if self.message_batch_result is not None:
            response["message_batch"] = dict(self.message_batch_result)
        return response

    def get_wechat_message_batch(self, binding: Binding, batch_id: str):
        self.events.append(f"message_batch:{batch_id}")
        if self.message_batch_statuses:
            result = dict(self.message_batch_statuses.pop(0))
        else:
            task = self.task
            result = {
                "batch_id": batch_id,
                "batch_status": "reply_action_created",
                "processing": False,
                "decision": "send_reply",
                "updated_at": "ready",
                "task": dict(task.raw) if task else None,
            }
        target = self.read_targets[0] if self.read_targets else None
        if target is not None:
            result.setdefault(
                "authorization",
                {
                    "allowed": True,
                    "authorization_scope": "batch_continuation",
                    "batch_id": batch_id,
                    "continuation_token": f"continuation-{batch_id}",
                    "conversation_id": target.conversation_id,
                    "authorization_revision": target.authorization_revision,
                    "read_reason": (
                        str(target.raw.get("authorization_read_reason") or "")
                        if isinstance(target.raw, dict)
                        else ""
                    )
                    or target.read_reason,
                    "remark_code": target.remark_code,
                    "rpa_session_key": target.rpa_session_key,
                    "display_name": target.display_name,
                    "lead_id": target.lead_id,
                    "sales_id": target.sales_id,
                },
            )
        return result


class FakeBridge:
    def __init__(self, result: RpaResult, send_payload: dict | None = None, message_sender_role: str = "customer") -> None:
        self.result = result
        self.message_sender_role = message_sender_role
        self.tasks: list[Task] = []
        self.sent_replies: list[dict] = []
        self.session_scans: list[dict] = []
        self.message_reads: list[dict] = []
        self.get_messages_payloads: list[dict] = []
        self.locate_chats: list[dict] = []
        self.locate_payloads: list[dict] = []
        self.voice_transcribes: list[dict] = []
        self.voice_payloads: list[dict] = []
        self.c2_operation_order: list[str] = []
        self.add_friend_cancel_check = None
        self.probe_calls = 0
        self.send_journal_dir = Path(
            tempfile.mkdtemp(prefix="chejin-send-journal-test-")
        )
        self.send_payload = send_payload or {
            "ok": True,
            "adapter": "mock",
            "state": "send_mock",
            "sidecar_run_id": "send-run-1",
            "action_phase": "confirmed",
            "physical_send_triggered": True,
            "send_result": {
                "ok": True,
                "confirmed": True,
                "result": "sent",
                "action_phase": "confirmed",
                "physical_send_triggered": True,
            },
        }
        self.voice_payload: dict = {
            "ok": False,
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": 3,
            "adapter": "mock",
            "state": "voice_transcribe_no_visible_voice",
            "sidecar_run_id": "voice-run-1",
            "transcribed_messages": [],
            "attempt_count": 0,
            "quality_flags": ["mock_no_visible_voice"],
        }

    def probe(self):
        self.probe_calls += 1
        return "ready", "logged_in"

    def run_add_friend(self, task: Task, emit_step, cancel_check=None):
        self.tasks.append(task)
        self.add_friend_cancel_check = cancel_check
        emit_step(RpaStep(current_step="checking_rpa", title="检查自动化组件", remark="自动化组件可用"))
        emit_step(RpaStep(current_step="invite_sent", title="发送添加通讯录邀请", remark="已点击发送"))
        return self.result

    def send_reply(
        self,
        *,
        target: str,
        rpa_session_key: str,
        text: str,
        task_id: str,
        reply_action_id: str | None = None,
        current_only: bool = True,
        expected_context_guard: dict | None = None,
        cancel_check=None,
    ):
        self.sent_replies.append(
            {
                "target": target,
                "rpa_session_key": rpa_session_key,
                "text": text,
                "task_id": task_id,
                "reply_action_id": reply_action_id,
                "current_only": current_only,
                "expected_context_guard": expected_context_guard,
                "cancel_check": cancel_check,
            }
        )
        return self.send_payload

    def send_transaction_journal_path(self, reply_action_id: str) -> Path:
        return self.send_journal_dir / f"{reply_action_id}.json"

    def list_sessions(self, **kwargs):
        self.c2_operation_order.append("sessions")
        self.session_scans.append({})
        return {
            "ok": True,
            "adapter": "mock",
            "state": "sessions_mock",
            "sidecar_run_id": "session-run-1",
            "sessions": [
                {
                    "name": "CJTEST01 许聪",
                    "session_key": "wx:rpa:v1:a",
                    "row_fingerprint": {"title_text": "CJTEST01 许聪"},
                    "content": "你好",
                    "unread_signal": True,
                    "ocr_confidence": 0.98,
                }
            ],
        }

    def get_messages(self, *, display_name: str, rpa_session_key: str, **kwargs):
        self.c2_operation_order.append("messages")
        self.message_reads.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
        if self.get_messages_payloads:
            payload = dict(self.get_messages_payloads.pop(0))
            payload.setdefault("ok", True)
            payload.setdefault("adapter", "mock")
            payload.setdefault("state", "messages_mock")
            payload.setdefault("sidecar_run_id", f"message-run-{len(self.message_reads)}")
            return self._contractual_message_payload(payload)
        return self._contractual_message_payload({
            "ok": True,
            "adapter": "mock",
            "state": "messages_mock",
            "sidecar_run_id": "message-run-1",
            "messages": [
                {"id": "wx-msg-1", "sender_role": self.message_sender_role, "type": "text", "content": "你好", "ocr_confidence": 0.98}
            ],
        })

    def _contractual_message_payload(self, payload: dict) -> dict:
        payload.setdefault("contract_version", 3)
        payload.setdefault("contract_revision", contract_revision())
        payload.setdefault("contract_sha256", contract_sha256())
        payload.setdefault("observation_schema_version", 3)
        payload.setdefault(
            "authoritative_frame_source",
            "final_read" if len(self.message_reads) > 1 else "initial_read",
        )
        if "observations" in payload:
            payload.setdefault(
                "send_context_guard",
                self._send_context_guard(payload.get("observations") or []),
            )
            return payload

        observations: list[dict] = []
        for index, message in enumerate(payload.get("messages") or []):
            if not isinstance(message, dict):
                continue
            message_type = str(message.get("type") or "text").lower()
            role = str(message.get("sender_role") or "unknown").lower()
            content = str(message.get("content") or "").strip()
            is_voice_placeholder = message_type in {"voice", "audio"} and (
                "[语音]" in content or not content or '"' in content
            )
            if message_type in {"voice", "audio"}:
                row_kind = "voice_bubble" if is_voice_placeholder else "voice_transcript"
                role_source = "same_row_avatar" if is_voice_placeholder else "parent_voice"
                voice_state = "untranscribed" if is_voice_placeholder else "transcribed"
                canonical_type = "voice"
            elif message_type == "system":
                row_kind = "system_message"
                role = "system"
                role_source = "system"
                voice_state = "not_voice"
                canonical_type = "system"
            else:
                row_kind = "text_bubble"
                role_source = "same_row_avatar" if role in {"customer", "self"} else "unknown"
                voice_state = "not_voice"
                canonical_type = "text"
            anchor = str(message.get("voice_anchor_stable_key") or message.get("id") or f"voice-{index}")
            observation = {
                "schema_version": 3,
                "observation_id": str(message.get("id") or f"message-{index}"),
                "row_kind": row_kind,
                "sender_role": role,
                "sender_role_source": role_source,
                "message_type": canonical_type,
                "voice_state": voice_state,
                "source_message": dict(message),
            }
            if content and not is_voice_placeholder:
                observation["content_clean"] = content
            if row_kind in {"voice_bubble", "voice_transcript"}:
                observation["voice_anchor_key"] = anchor
                observation["source_message"]["voice_anchor_stable_key"] = anchor
            if row_kind == "voice_transcript":
                observation["parent_voice_anchor_key"] = anchor
            if isinstance(message.get("bubble_rect"), list):
                observation["bubble_rect"] = list(message["bubble_rect"])
            observations.append(observation)
        payload["observations"] = observations
        payload["send_context_guard"] = self._send_context_guard(observations)
        return payload

    @staticmethod
    def _send_context_guard(observations: list[dict]) -> dict:
        context_sequence = [
            {
                "row_kind": str(item.get("row_kind") or ""),
                "sender_role": str(item.get("sender_role") or ""),
                "content_normalized": "".join(
                    str(item.get("content_clean") or "").split()
                ),
                "voice_anchor": str(
                    item.get("parent_voice_anchor_key")
                    or item.get("voice_anchor_key")
                    or ""
                ),
                "image_anchor": "",
            }
            for item in observations
            if str(item.get("row_kind") or "")
            in {"text_bubble", "voice_transcript", "image_bubble", "system_message"}
        ]
        return {
            "schema_version": 1,
            "sequence": context_sequence,
            "message_count": len(context_sequence),
            "bottom": dict(context_sequence[-1]) if context_sequence else None,
        }

    def locate_chat(self, *, display_name: str, rpa_session_key: str, **kwargs):
        self.c2_operation_order.append("locate_chat")
        self.locate_chats.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
        if self.locate_payloads:
            payload = dict(self.locate_payloads.pop(0))
            payload.setdefault("adapter", "mock")
            payload.setdefault("sidecar_run_id", f"locate-run-{len(self.locate_chats)}")
            payload.setdefault("target_mode", kwargs.get("target_mode") or "visible")
            payload.setdefault("remark_code", kwargs.get("remark_code") or "")
            return payload
        return {
            "ok": True,
            "adapter": "mock",
            "state": "chat_target_confirmed",
            "sidecar_run_id": "locate-run-1",
            "target_mode": kwargs.get("target_mode") or "visible",
            "remark_code": kwargs.get("remark_code") or "",
            "targeting": {"ok": True, "mode": kwargs.get("target_mode") or "visible"},
        }

    def voice_transcribe(self, *, display_name: str, rpa_session_key: str, **kwargs):
        self.c2_operation_order.append("voice_transcribe")
        self.voice_transcribes.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
        payload = dict(
            self.voice_payloads.pop(0)
            if self.voice_payloads
            else self.voice_payload
        )
        payload.setdefault("contract_version", 3)
        payload.setdefault("contract_revision", contract_revision())
        payload.setdefault("contract_sha256", contract_sha256())
        payload.setdefault("observation_schema_version", 3)
        return payload


class TaskRunnerTest(unittest.TestCase):
    def test_original_and_later_voice_failures_are_merged_not_overwritten(self):
        outcomes = merge_item_outcomes(
            [
                {
                    "source_message_key": "voice-original-failed",
                    "result": "failed",
                    "evidence": {"sender_role": "customer"},
                }
            ],
            [
                {
                    "source_message_key": "voice-later-failed",
                    "result": "failed",
                    "evidence": {"sender_role": "self"},
                }
            ],
        )

        self.assertEqual(
            [item["source_message_key"] for item in outcomes],
            ["voice-later-failed", "voice-original-failed"],
        )
        self.assertEqual(
            {
                item["source_message_key"]: item["evidence"]["sender_role"]
                for item in outcomes
            },
            {
                "voice-original-failed": "customer",
                "voice-later-failed": "self",
            },
        )

    def test_voice_failure_blocks_brain_but_not_reliable_image_operation(self):
        self.assertEqual(
            image_operation_gate_errors(["C2_VOICE_TRANSCRIBE_FAILED"]),
            [],
        )
        self.assertEqual(
            image_operation_gate_errors(
                [
                    "C2_VOICE_TRANSCRIBE_FAILED",
                    "C2_MESSAGE_HISTORY_GAP",
                ]
            ),
            ["C2_MESSAGE_HISTORY_GAP"],
        )

    def setUp(self):
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        with db_connection() as conn:
            conn.execute("DELETE FROM c2_action_journal")
            conn.execute("DELETE FROM c2_message_ledger")
            conn.execute("DELETE FROM c2_ingest_outbox")
            conn.execute("DELETE FROM reply_send_ack_outbox")
            conn.execute("DELETE FROM c2_runtime_state")
            conn.commit()

    def test_action_journal_vertical_c1_add_friend_reaches_task_completion(self):
        task = Task(
            id="task-journal-vertical-c1",
            task_type="add_friend",
            status="pending",
            phone="13800000000",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-0000",
            remark_code="CJ8K2P",
        )
        api = FakeApi(task)
        bridge = RpaBridge(sidecar_script=Path(__file__))
        bridge.mode = "real"
        observed_phases: list[str] = []

        def sidecar_boundary(args, **_kwargs):
            journal_path = Path(args[args.index("--action-journal") + 1])
            observed_phases.append(action_journal_phase(journal_path))
            update_action_journal_item(
                journal_path,
                source_message_key=task.id,
                action_phase="trigger_attempted",
                business_state="invite_confirm_click_starting",
            )
            observed_phases.append(action_journal_phase(journal_path))
            update_action_journal_item(
                journal_path,
                source_message_key=task.id,
                action_phase="confirmed",
                business_state="invite_sent",
                business_result_confirmed=True,
                terminal_payload={
                    "ok": True,
                    "task_status": "completed",
                    "result_code": "invite_sent",
                    "current_step": "invite_confirm_clicked",
                },
            )
            observed_phases.append(action_journal_phase(journal_path))
            return {
                "ok": True,
                "task_status": "completed",
                "result_code": "invite_sent",
                "current_step": "invite_confirm_clicked",
                "message": "已点击发送好友申请。",
            }

        runner, seen = self.make_runner(api, bridge)  # type: ignore[arg-type]
        runner.binding = Binding(
            worker_id="worker-journal-c1",
            worker_token="token",
            client_instance_id="client-journal-c1",
            run_status="running",
        )

        with patch.object(
            bridge,
            "probe",
            return_value=("ready", "logged_in"),
        ), patch.object(
            bridge,
            "_call_omniauto",
            side_effect=sidecar_boundary,
        ):
            runner.tick_once()

        self.assertEqual(
            observed_phases,
            ["not_attempted", "trigger_attempted", "confirmed"],
        )
        self.assertIn(
            f"complete_invite_sent:{task.id}",
            api.events,
        )
        self.assertFalse(
            action_journal_path("add_friend", task.id).exists()
        )
        self.assertTrue(seen["results"][-1].ok)
        self.assertEqual(seen["results"][-1].result_code, "invite_sent")

    def test_c2_flow_finalizer_runs_when_main_flow_raises(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-finalize-on-error",
            rpa_session_key="wx:rpa:v1:finalize",
            display_name="CJFINAL01",
            remark_code="CJFINAL01",
            authorization_revision="revision-finalize",
        )

        def fail_after_action(
            _binding,
            _target,
            *,
            flow_outcomes: FlowOutcomeAccumulator,
            **_kwargs,
        ):
            flow_outcomes.record(
                {
                    "source_message_key": "voice-finalized-before-error",
                    "result": "completed",
                    "evidence": {"action_kind": "voice"},
                    "terminal_payload": {
                        "state": "completed",
                        "transcribed_message": {
                            "content": "已经完成的语音",
                        },
                    },
                }
            )
            raise RuntimeError("later flow failure")

        with patch.object(
            runner,
            "_read_one_wechat_target_impl",
            side_effect=fail_after_action,
        ), self.assertRaisesRegex(RuntimeError, "later flow failure"):
            runner._read_one_wechat_target(binding, target)

        ledger = load_c2_ledger_entry(
            target.conversation_id,
            "voice-finalized-before-error",
        )
        self.assertEqual(ledger["terminal_state"], "completed")
        self.assertEqual(
            ledger["result"]["transcribed_message"]["content"],
            "已经完成的语音",
        )
        self.assertEqual(
            list_c2_action_journal(target.conversation_id),
            [],
        )

    def test_c2_flow_recovers_durable_action_checkpoint_before_next_action(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id="conv-recover-action-journal",
            rpa_session_key="wx:rpa:v1:recover-action",
            display_name="CJRECOVER01",
            remark_code="CJRECOVER01",
            authorization_revision="revision-recover-action",
        )
        checkpoint_c2_action_outcomes(
            flow_id="crashed-flow",
            conversation_id=target.conversation_id,
            outcomes=[
                {
                    "source_message_key": "voice-completed-before-crash",
                    "result": "completed",
                    "evidence": {"action_kind": "voice"},
                    "terminal_payload": {
                        "state": "completed",
                        "transcribed_message": {
                            "content": "崩溃前已经完成",
                        },
                    },
                }
            ],
        )

        runner._recover_c2_action_journal(target)

        ledger = load_c2_ledger_entry(
            target.conversation_id,
            "voice-completed-before-crash",
        )
        self.assertEqual(ledger["terminal_state"], "completed")
        self.assertEqual(
            ledger["result"]["transcribed_message"]["content"],
            "崩溃前已经完成",
        )
        self.assertEqual(
            list_c2_action_journal(target.conversation_id),
            [],
        )

    def test_c2_flow_recovers_triggered_voice_before_next_physical_action(
        self,
    ):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id="conv-recover-physical-voice",
            rpa_session_key="wx:rpa:v1:recover-physical-voice",
            display_name="CJPHYSICAL01",
            remark_code="CJPHYSICAL01",
            authorization_revision="revision-physical-voice",
        )
        path = action_journal_path(
            "voice",
            "voice-crashed-after-click",
        )
        initialize_action_journal(
            path,
            action_kind="voice",
            transaction_id="voice-crashed-after-click",
            conversation_id=target.conversation_id,
            items=[
                {
                    "source_message_key": "voice-triggered-before-crash",
                    "physical_anchor_keys": ["voice-anchor-1"],
                }
            ],
        )
        update_action_journal_item(
            path,
            source_message_key="voice-triggered-before-crash",
            action_phase="trigger_attempted",
        )

        runner._recover_physical_action_journals(target)

        ledger = load_c2_ledger_entry(
            target.conversation_id,
            "voice-triggered-before-crash",
        )
        self.assertEqual(ledger["terminal_state"], "failed")
        self.assertEqual(
            ledger["result"]["action_outcome"]["action_phase"],
            "trigger_attempted",
        )
        self.assertFalse(path.exists())

    def test_c2_flow_drops_not_attempted_journal_without_terminalizing(
        self,
    ):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id="conv-recover-not-attempted",
            rpa_session_key="wx:rpa:v1:recover-not-attempted",
            display_name="CJNOTATTEMPT01",
            remark_code="CJNOTATTEMPT01",
            authorization_revision="revision-not-attempted",
        )
        path = action_journal_path(
            "image",
            "image-crashed-before-copy",
        )
        initialize_action_journal(
            path,
            action_kind="image",
            transaction_id="image-crashed-before-copy",
            conversation_id=target.conversation_id,
            items=[
                {
                    "source_message_key": "image-not-attempted",
                    "physical_anchor_keys": ["image-anchor-1"],
                }
            ],
        )

        runner._recover_physical_action_journals(target)

        self.assertIsNone(
            load_c2_ledger_entry(
                target.conversation_id,
                "image-not-attempted",
            )
        )
        self.assertFalse(path.exists())

    def test_c2_flow_recovers_sidecar_crash_before_removing_current_journal(
        self,
    ):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-sidecar-crash",
            worker_token="token",
            client_instance_id="client-sidecar-crash",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-sidecar-crash-after-click",
            rpa_session_key="wx:rpa:v1:sidecar-crash-after-click",
            display_name="CJSIDECAR01",
            remark_code="CJSIDECAR01",
            authorization_revision="revision-sidecar-crash",
        )
        created_paths: list[Path] = []

        def crash_after_trigger(*_args, flow_outcomes, **_kwargs):
            path = runner._start_irreversible_action_journal(
                action_kind="voice",
                target=target,
                items=[
                    {
                        "source_message_key": "voice-sidecar-crashed",
                        "physical_anchor_keys": ["voice-anchor-crashed"],
                    }
                ],
                flow_outcomes=flow_outcomes,
            )
            created_paths.append(path)
            update_action_journal_item(
                path,
                source_message_key="voice-sidecar-crashed",
                action_phase="trigger_attempted",
            )
            raise RuntimeError("SIDECAR_CRASHED_AFTER_CLICK")

        with patch.object(
            runner,
            "_read_one_wechat_target_impl",
            side_effect=crash_after_trigger,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "SIDECAR_CRASHED_AFTER_CLICK",
            ):
                runner._read_one_wechat_target(binding, target)

        ledger = load_c2_ledger_entry(
            target.conversation_id,
            "voice-sidecar-crashed",
        )
        self.assertEqual(ledger["terminal_state"], "failed")
        self.assertEqual(
            ledger["result"]["action_outcome"]["action_phase"],
            "trigger_attempted",
        )
        self.assertTrue(created_paths)
        self.assertFalse(created_paths[0].exists())

    def test_c2_flow_finalizer_enriches_existing_terminal_ledger(self):
        target = WechatReadTarget(
            conversation_id="conv-enrich-action-ledger",
            rpa_session_key="wx:rpa:v1:enrich-action",
            display_name="CJENRICH01",
            remark_code="CJENRICH01",
            authorization_revision="revision-enrich-action",
        )
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key="voice-failed-with-evidence",
            dedupe_key=None,
            message_type="voice",
            terminal_state="failed",
            ingest_state="waiting",
            result={"state": "failed", "error_code": "VOICE_FAILED"},
        )
        accumulator = FlowOutcomeAccumulator()
        accumulator.record(
            {
                "source_message_key": "voice-failed-with-evidence",
                "result": "failed",
                "evidence": {
                    "action_kind": "voice",
                    "action_phase": "trigger_attempted",
                },
                "terminal_payload": {
                    "state": "failed",
                    "error_code": "VOICE_FAILED",
                },
            }
        )

        TaskRunner._finalize_c2_flow_outcomes(target, accumulator)

        ledger = load_c2_ledger_entry(
            target.conversation_id,
            "voice-failed-with-evidence",
        )
        self.assertEqual(
            ledger["result"]["action_outcome"]["evidence"][
                "action_phase"
            ],
            "trigger_attempted",
        )

    def make_runner(self, api: FakeApi, bridge: FakeBridge):
        seen = {"profiles": [], "statuses": [], "steps": [], "tasks": [], "results": [], "errors": []}
        runner = TaskRunner(
            api,  # type: ignore[arg-type]
            bridge,  # type: ignore[arg-type]
            on_profile=lambda item: seen["profiles"].append(item),
            on_status=lambda item: seen["statuses"].append(item),
            on_step=lambda item: seen["steps"].append(item),
            on_task=lambda item: seen["tasks"].append(item),
            on_result=lambda item: seen["results"].append(item),
            on_error=lambda item: seen["errors"].append(item),
        )
        runner.c2_stop_guard_before_voice_seconds = 0
        return runner, seen

    def make_chat_reply_task(self, *, task_id: str, status: str = "pending") -> Task:
        raw = {
            "id": task_id,
            "task_type": "chat_reply",
            "status": status,
            "reply_action_id": "reply-action-1",
            "c3": {
                "message_batch": {"id": "batch-1", "conversation_id": "conv-1"},
                "reply_action": {"id": "reply-action-1", "conversation_id": "conv-1"},
            },
        }
        return Task(
            id=task_id,
            task_type="chat_reply",
            status=status,
            reply_action_id="reply-action-1",
            raw=raw,
        )

    def authorize_chat_reply_target(self, api: FakeApi) -> None:
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="waiting_sales_reply",
                authorization_revision="revision-conv-1",
            )
        ]

    def test_tick_once_claims_reports_steps_and_completes(self):
        task = Task(id="task-1", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, seen = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("online", seen["statuses"])
        self.assertEqual([step.current_step for step in seen["steps"]], ["checking_rpa", "invite_sent"])
        self.assertIn("claim:task-1", api.events)
        self.assertIn("step:checking_rpa", api.events)
        self.assertIn("complete_invite_sent:task-1", api.events)
        self.assertIsNone(runner.current_task)

    def test_task_pull_rechecks_ui_lock_before_claiming(self):
        task = Task(
            id="task-race",
            task_type="add_friend",
            status="pending",
            phone="13800000000",
        )
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=True,
                result_code="invite_sent",
                message="不应执行",
            )
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        with patch(
            "chejin_worker_client.task_runner.lock_summary",
            side_effect=[
                {"locked": False},
                {"locked": False},
                {"locked": True},
            ],
        ):
            runner.tick_once()

        self.assertNotIn("pull", api.events)
        self.assertNotIn("claim:task-race", api.events)
        self.assertEqual(bridge.tasks, [])

    def test_c2_ui_lock_acquire_is_atomic_with_task_pull(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-ui-race",
            rpa_session_key="wx:rpa:v1:ui-race",
            display_name="CJUIRACE01",
            remark_code="CJUIRACE01",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-ui-race",
        )
        api.read_targets = [target]
        observed = {"pulled_during_acquire": None}
        pull_thread: list[threading.Thread] = []

        def competing_acquire(**_kwargs):
            thread = threading.Thread(
                target=runner._pull_and_execute,
                args=(binding,),
                daemon=True,
            )
            pull_thread.append(thread)
            thread.start()
            time.sleep(0.05)
            observed["pulled_during_acquire"] = "pull" in api.events
            raise UiLockError("UI_LOCK_BUSY", "test stop")

        with patch(
            "chejin_worker_client.task_runner.acquire_ui_lock",
            side_effect=competing_acquire,
        ):
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=True,
            )
        pull_thread[0].join(timeout=1)

        self.assertFalse(result["ok"])
        self.assertFalse(observed["pulled_during_acquire"])
        self.assertIn("pull", api.events)

    def test_task_without_server_fencing_token_is_cancelled_before_rpa(self):
        task = Task(
            id="task-no-lease",
            task_type="add_friend",
            status="running",
            phone="13800000000",
        )
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(
                ok=True,
                result_code="invite_sent",
                message="不应执行",
            )
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        guard = runner._start_task_lease_guard(binding, task)

        self.assertTrue(guard.cancel_requested())
        self.assertEqual(guard.error_code, "TASK_LEASE_FENCING_MISSING")
        runner._stop_task_lease_guard()

    def test_task_lease_transient_network_failure_retries_before_expiry(self):
        renewed_task = Task(
            id="task-lease-retry",
            task_type="add_friend",
            status="running",
            lease_fencing_token=8,
            lease_expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=90)
            ).isoformat(),
        )

        class TransientApi:
            def __init__(self):
                self.calls = 0

            def renew_task_lease(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("temporary network timeout")
                return renewed_task

        api = TransientApi()
        guard = TaskLeaseGuard(
            api=api,  # type: ignore[arg-type]
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            task=renewed_task,
            current_step=lambda: "add_friend_running",
        )

        self.assertTrue(guard._renew_once())
        self.assertFalse(guard.cancel_requested())
        self.assertTrue(guard._renew_once())
        self.assertFalse(guard.cancel_requested())
        self.assertEqual(api.calls, 2)

    def test_task_lease_server_5xx_retries_before_expiry(self):
        task = Task(
            id="task-lease-5xx",
            task_type="chat_reply",
            status="running",
            lease_fencing_token=9,
            lease_expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=90)
            ).isoformat(),
        )

        class ServerErrorApi:
            def renew_task_lease(self, *_args, **_kwargs):
                raise ApiError(
                    "INTERNAL_SERVER_ERROR",
                    "temporary server error",
                    503,
                )

        guard = TaskLeaseGuard(
            api=ServerErrorApi(),  # type: ignore[arg-type]
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            task=task,
            current_step=lambda: "chat_reply_running",
        )

        self.assertTrue(guard._renew_once())
        self.assertFalse(guard.cancel_requested())

    def test_task_lease_definitive_expiry_or_fencing_stops_immediately(self):
        for error_code in (
            "TASK_LEASE_EXPIRED",
            "TASK_LEASE_FENCING_STALE",
        ):
            with self.subTest(error_code=error_code):
                task = Task(
                    id=f"task-{error_code.lower()}",
                    task_type="add_friend",
                    status="running",
                    lease_fencing_token=3,
                    lease_expires_at=(
                        datetime.now(timezone.utc) + timedelta(seconds=90)
                    ).isoformat(),
                )

                class DefinitiveApi:
                    def renew_task_lease(self, *_args, **_kwargs):
                        raise ApiError(error_code, "lease rejected", 409)

                guard = TaskLeaseGuard(
                    api=DefinitiveApi(),  # type: ignore[arg-type]
                    binding=Binding(
                        worker_id="worker-1",
                        worker_token="token",
                        client_instance_id="client-1",
                        run_status="running",
                    ),
                    task=task,
                    current_step=lambda: "add_friend_running",
                )

                self.assertFalse(guard._renew_once())
                self.assertTrue(guard.cancel_requested())
                self.assertEqual(guard.error_code, error_code)

    def test_task_lease_transient_failure_stops_once_local_expiry_passes(self):
        task = Task(
            id="task-local-expiry",
            task_type="add_friend",
            status="running",
            lease_fencing_token=4,
            lease_expires_at=(
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        )

        class OfflineApi:
            def renew_task_lease(self, *_args, **_kwargs):
                raise ConnectionError("offline")

        guard = TaskLeaseGuard(
            api=OfflineApi(),  # type: ignore[arg-type]
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            task=task,
            current_step=lambda: "add_friend_running",
        )

        self.assertFalse(guard._renew_once())
        self.assertTrue(guard.cancel_requested())
        self.assertEqual(guard.error_code, "TASK_LEASE_EXPIRED")

    def test_add_friend_sidecar_cancel_check_tracks_worker_stop_and_ui_lease(self):
        task = Task(id="task-cancel", task_type="add_friend", status="running", phone="13800000000")
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        class FakeLease:
            lock_id = "lock-add-friend"
            fencing_token = 1
            lost = False

            def start_auto_renew(self):
                return None

            def cancel_requested(self):
                return self.lost

            def update_step(self, _step):
                return None

            def check_step_timeout(self):
                return None

            def release(self):
                return None

        lease = FakeLease()
        with patch(
            "chejin_worker_client.task_runner.force_recover_stale_lock"
        ), patch(
            "chejin_worker_client.task_runner.acquire_ui_lock",
            return_value=lease,
        ):
            result = runner._run_add_friend_with_ui_lock(binding, task)

        self.assertTrue(result.ok)
        self.assertTrue(callable(bridge.add_friend_cancel_check))
        self.assertFalse(bridge.add_friend_cancel_check())
        binding.run_status = "paused"
        self.assertEqual(bridge.add_friend_cancel_check(), "WORKER_INTERRUPTED")
        binding.run_status = "running"
        lease.lost = True
        self.assertTrue(bridge.add_friend_cancel_check())
        lease.lost = False
        runner.stop_event.set()
        self.assertTrue(bridge.add_friend_cancel_check())

    def test_tick_once_does_not_pull_tasks_while_c2_ui_flow_is_active(self):
        task = Task(id="task-c3-reply", task_type="chat_reply", status="pending", reply_action_id="reply-1")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="chat_reply_sent", message="sent"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.current_ui_lock = object()  # type: ignore[assignment]

        runner.tick_once()

        self.assertNotIn("pull", api.events)
        self.assertFalse(any(event.startswith("claim:") for event in api.events))

    def test_tick_once_does_not_pull_add_friend_while_c2_outbox_is_pending(self):
        task = Task(
            id="task-add-friend-outbox-barrier",
            task_type="add_friend",
            status="pending",
            phone="17368746889",
        )
        api = FakeApi(task)
        api.message_ingest_error = ConnectionError("backend offline")
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(
                    ok=True,
                    result_code="invite_sent",
                    message="unused",
                )
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.binding = binding
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-global-barrier-{time.time_ns()}",
                "conversation_id": "conv-global-barrier",
                "authorization_revision": "revision-global-barrier",
                "messages": [],
            }
        )
        try:
            runner.tick_once()

            self.assertNotIn("pull", api.events)
            self.assertFalse(
                any(event.startswith("claim:") for event in api.events)
            )
            self.assertEqual(
                load_c2_outbox_entry(outbox_id)["status"],
                "retry_waiting",
            )
        finally:
            with db_connection() as conn:
                conn.execute(
                    "DELETE FROM c2_ingest_outbox WHERE outbox_id = ?",
                    (outbox_id,),
                )
                conn.commit()

    def test_claim_response_does_not_drop_plain_contact_from_pull_payload(self):
        pulled_task = Task(id="task-plain", task_type="add_friend", status="pending", phone="17368746889")
        masked_claim_task = Task(id="task-plain", task_type="add_friend", status="running", phone="173****6889")
        api = FakeApi(pulled_task, claim_response=masked_claim_task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertEqual(bridge.tasks[0].search_phone, "17368746889")

    def test_claim_response_does_not_drop_formal_rpa_fields_from_pull_payload(self):
        pulled_task = Task(
            id="task-formal",
            task_type="add_friend",
            status="pending",
            phone="17368746889",
            verify_message="您好，我是车金张伟",
            remark_name="CJ-张伟-CJ8K2P-6889",
            remark_code="CJ8K2P",
            remark_code_valid=True,
        )
        masked_claim_task = Task(id="task-formal", task_type="add_friend", status="running", phone="173****6889")
        api = FakeApi(pulled_task, claim_response=masked_claim_task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertEqual(bridge.tasks[0].verify_message, "您好，我是车金张伟")
        self.assertEqual(bridge.tasks[0].remark_name, "CJ-张伟-CJ8K2P-6889")
        self.assertEqual(bridge.tasks[0].remark_code, "CJ8K2P")
        self.assertTrue(bridge.tasks[0].remark_code_valid)

    def test_success_uploads_omniauto_evidence_metadata_when_present(self):
        task = Task(id="task-evidence", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=True,
                result_code="invite_sent",
                message="已发送添加通讯录邀请",
                evidence_path="C:/runtime/latest/review.html",
                evidence_metadata={"review_path": "C:/runtime/latest/review.html", "current_step": "invite_sent"},
            )
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("evidence:None", api.events)
        self.assertEqual(api.evidence_payloads[0]["evidence_path"], "C:/runtime/latest/review.html")
        self.assertEqual(api.evidence_payloads[0]["metadata"]["current_step"], "invite_sent")

    def test_environment_failure_pauses_worker_after_failed_report(self):
        task = Task(id="task-2", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=False,
                error_code="WECHAT_WINDOW_NOT_FOUND",
                failure_step="wechat_window_found",
                message="微信窗口未找到",
            )
        )
        runner, seen = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.binding = binding

        runner.tick_once()

        self.assertIn("fail:WECHAT_WINDOW_NOT_FOUND:wechat_window_found", api.events)
        self.assertIn("evidence:WECHAT_WINDOW_NOT_FOUND", api.events)
        self.assertIn("paused", api.run_status_updates)
        self.assertEqual(binding.run_status, "paused")
        self.assertTrue(any("运行环境异常" in item for item in seen["errors"]))

    def test_phone_not_found_does_not_pause_worker_after_failed_report(self):
        task = Task(id="task-404", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=False,
                error_code="PHONE_NOT_FOUND",
                failure_step="phone_search_finished",
                message="搜索不到客户",
            )
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.binding = binding

        runner.tick_once()

        self.assertIn("fail:PHONE_NOT_FOUND:phone_search_finished", api.events)
        self.assertNotIn("paused", api.run_status_updates)
        self.assertEqual(binding.run_status, "running")

    def test_paused_worker_only_sends_heartbeat(self):
        task = Task(id="task-3", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner.tick_once()

        self.assertIn("heartbeat:ready:logged_in", api.events)
        self.assertNotIn("pull", api.events)

    def test_paused_worker_does_not_run_c2_scan_or_read(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="paused",
        )
        runner.binding = binding

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(bridge.c2_operation_order, [])
        self.assertFalse(any(event.startswith("read_targets:") for event in api.events))

    def test_pause_is_local_immediately_and_retries_backend_sync(self):
        api = FakeApi(None)
        api.run_status_error = RuntimeError("offline")
        runner, seen = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.binding = binding

        self.assertFalse(runner.set_run_status("paused"))
        self.assertEqual(binding.run_status, "paused")
        self.assertEqual(runner._pending_run_status_sync, "paused")
        self.assertTrue(runner.run_status_sync_error)
        self.assertTrue(any("自动重试" in item for item in seen["errors"]))

        api.run_status_error = None
        runner._sync_pending_run_status(force=True)

        self.assertIsNone(runner._pending_run_status_sync)
        self.assertIsNone(runner.run_status_sync_error)
        self.assertEqual(api.run_status_updates[-1], "paused")

    def test_start_requires_backend_confirmation_before_local_running(self):
        api = FakeApi(None)
        api.run_status_error = RuntimeError("offline")
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="paused",
        )
        runner.binding = binding

        self.assertFalse(runner.set_run_status("running"))
        self.assertEqual(binding.run_status, "paused")

        api.run_status_error = None
        self.assertTrue(runner.set_run_status("running"))
        self.assertEqual(binding.run_status, "running")

    def test_run_status_rejects_mismatched_backend_confirmation(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="paused",
        )
        runner.binding = binding
        api.set_run_status = lambda _binding, _status: WorkerProfile(
            id=binding.worker_id,
            worker_name="测试 Worker",
            run_status="paused",
        )

        self.assertFalse(runner.set_run_status("running"))
        self.assertEqual(binding.run_status, "paused")

    def test_heartbeat_reuses_fresh_rpa_probe(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")

        runner.tick_once()
        runner.tick_once()

        self.assertEqual(bridge.probe_calls, 1)
        self.assertEqual(sum(1 for item in api.events if item == "heartbeat:ready:logged_in"), 2)

    def test_heartbeat_does_not_start_status_ocr_during_ui_action(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")
        runner.last_rpa_component_status = "ready"
        runner.last_wechat_status = "logged_in"
        runner.current_ui_lock = object()  # type: ignore[assignment]

        runner.tick_once()

        self.assertEqual(bridge.probe_calls, 0)
        self.assertIn("heartbeat:ready:logged_in", api.events)

    def test_heartbeat_reports_persisted_vision_capability_pause(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")))
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")
        save_c2_state(
            "vision_capability",
            {
                "state": "capability_paused",
                "reason": "vision_configuration_incomplete",
                "missing_configuration": ["CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"],
            },
        )

        runner.tick_once()

        vision = api.heartbeat_payloads[-1]["local_lock_summary"]["capabilities"]["vision"]
        self.assertEqual(vision["state"], "capability_paused")
        self.assertEqual(vision["missing_configuration"], ["CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"])

    def test_schedule_paused_worker_only_sends_heartbeat(self):
        task = Task(id="task-schedule", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="已发送添加通讯录邀请"))
        runner, _ = self.make_runner(api, bridge)
        runner.can_pull_tasks = lambda: False
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("heartbeat:ready:logged_in", api.events)
        self.assertNotIn("pull", api.events)

    def test_chat_reply_claim_send_then_sends_and_acks(self):
        task = self.make_chat_reply_task(task_id="task-chat")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("claim:task-chat", api.events)
        self.assertIn("claim_source:c2_conversation_flow:conv-1", api.events)
        self.assertIn("claim_send:reply-action-1", api.events)
        self.assertLess(api.events.index("ingest:1"), api.events.index("claim:task-chat"))
        self.assertEqual(bridge.sent_replies[0]["target"], "CJTEST01")
        self.assertEqual(bridge.sent_replies[0]["text"], "您好，可以继续沟通这台车。")
        self.assertEqual(bridge.sent_replies[0]["rpa_session_key"], "")
        self.assertIn("sent_ack:sent:None", api.events)
        self.assertTrue(bridge.sent_replies[0]["current_only"])
        self.assertTrue(callable(bridge.sent_replies[0]["cancel_check"]))
        self.assertEqual(
            load_reply_send_ack_outbox("reply-action-1")["status"],
            "confirmed",
        )

    def test_action_journal_vertical_c3_send_reaches_sent_ack(self):
        task = self.make_chat_reply_task(
            task_id="task-journal-vertical-send"
        )
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        observed_phases: list[str] = []

        class JournalSendBridge(FakeBridge):
            def send_reply(
                self,
                *,
                target: str,
                rpa_session_key: str,
                text: str,
                task_id: str,
                reply_action_id: str | None = None,
                current_only: bool = True,
                expected_context_guard: dict | None = None,
                cancel_check=None,
            ):
                journal_path = self.send_transaction_journal_path(
                    str(reply_action_id or "")
                )
                observed_phases.append(action_journal_phase(journal_path))
                update_action_journal_item(
                    journal_path,
                    source_message_key=str(reply_action_id or ""),
                    action_phase="trigger_attempted",
                    business_state="send_button_click_starting",
                )
                observed_phases.append(action_journal_phase(journal_path))
                update_action_journal_item(
                    journal_path,
                    source_message_key=str(reply_action_id or ""),
                    action_phase="confirmed",
                    business_state="sent",
                    business_result_confirmed=True,
                    terminal_payload={
                        "state": "sent",
                        "reply_text": text,
                    },
                )
                observed_phases.append(action_journal_phase(journal_path))
                return super().send_reply(
                    target=target,
                    rpa_session_key=rpa_session_key,
                    text=text,
                    task_id=task_id,
                    reply_action_id=reply_action_id,
                    current_only=current_only,
                    expected_context_guard=expected_context_guard,
                    cancel_check=cancel_check,
                )

        bridge = JournalSendBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-journal-send",
            worker_token="token",
            client_instance_id="client-journal-send",
            run_status="running",
        )

        runner.tick_once()

        self.assertEqual(
            observed_phases,
            ["not_attempted", "trigger_attempted", "confirmed"],
        )
        self.assertIn("sent_ack:sent:None", api.events)
        self.assertEqual(
            load_reply_send_ack_outbox("reply-action-1")["status"],
            "confirmed",
        )
        self.assertFalse(
            bridge.send_transaction_journal_path(
                "reply-action-1"
            ).exists()
        )

    def test_c2_claimed_chat_reply_is_visible_in_heartbeat_until_send_finishes(self):
        task = self.make_chat_reply_task(task_id="task-chat-heartbeat")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        runner, seen = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="invite_sent", message="unused")
            ),
        )
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        api.claim_send_callback = lambda _task: runner.tick_once()

        runner.tick_once()

        active_heartbeats = [
            payload
            for payload in api.heartbeat_payloads
            if payload.get("current_task") == task.id
        ]
        self.assertTrue(active_heartbeats)
        self.assertEqual(active_heartbeats[-1]["running_status"], "running")
        self.assertIsNone(runner.current_task)
        self.assertIsNone(runner.current_task_lease)
        self.assertIn(None, seen["tasks"])

    def test_sidecar_ok_without_new_bubble_confirmation_never_acks_sent(self):
        task = self.make_chat_reply_task(task_id="task-chat-unconfirmed")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused"),
            send_payload={
                "ok": True,
                "adapter": "win32_ocr",
                "state": "send_win32_rpa",
                "sidecar_run_id": "send-unconfirmed",
                "action_phase": "trigger_attempted",
                "physical_send_triggered": True,
                "send_result": {
                    "ok": True,
                    "confirmed": False,
                    "result": "unknown",
                    "action_phase": "trigger_attempted",
                    "physical_send_triggered": True,
                },
            },
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner.tick_once()

        self.assertEqual(len(bridge.sent_replies), 1)
        self.assertIn("sent_ack:unknown:SEND_RESULT_UNKNOWN", api.events)
        self.assertNotIn("sent_ack:sent:None", api.events)
        possible = load_c2_state("possible_ai_sends:conv-1")
        self.assertEqual(
            possible["sends"][0]["reconciliation_state"],
            "ai_unreconciled",
        )

    def test_pre_click_send_failure_clears_possible_ai_send(self):
        task = self.make_chat_reply_task(task_id="task-chat-pre-click-failed")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused"),
            send_payload={
                "ok": False,
                "adapter": "win32_ocr",
                "state": "send_input_not_ready",
                "error_code": "SEND_INPUT_NOT_READY",
                "action_phase": "not_attempted",
                "physical_send_triggered": False,
                "send_result": {
                    "ok": False,
                    "confirmed": False,
                    "result": "failed",
                    "action_phase": "not_attempted",
                    "physical_send_triggered": False,
                },
            },
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner.tick_once()

        self.assertIn("sent_ack:failed:SEND_INPUT_NOT_READY", api.events)
        possible = load_c2_state("possible_ai_sends:conv-1")
        self.assertEqual(possible.get("sends"), [])

    def test_noncanonical_reply_text_is_rejected_before_touching_wechat(self):
        task = self.make_chat_reply_task(task_id="task-chat-noncanonical")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        api.claim_reply_text = "第一行\n第二行"
        api.claim_reply_hash = hashlib.sha256(api.claim_reply_text.encode("utf-8")).hexdigest()
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner.tick_once()

        self.assertEqual(bridge.sent_replies, [])
        self.assertIn("sent_ack:failed:SEND_TEXT_NOT_CANONICAL", api.events)

    def test_sent_reply_ack_network_failure_replays_ack_without_resending_wechat(self):
        task = self.make_chat_reply_task(task_id="task-chat-ack-replay")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        api.sent_ack_error = ConnectionError("offline after send")
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.binding = binding

        runner.tick_once()

        self.assertEqual(len(bridge.sent_replies), 1)
        waiting = load_reply_send_ack_outbox("reply-action-1")
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["ack_payload"]["send_result"], "sent")

        bridge.c2_operation_order.clear()
        runner._run_c2_scan_round(binding, reason="ack_barrier_regression")
        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(
            load_reply_send_ack_outbox("reply-action-1")["status"],
            "waiting",
        )

        api.sent_ack_error = None
        api.task = None
        with db_connection() as conn:
            conn.execute(
                """
                UPDATE reply_send_ack_outbox
                SET next_attempt_at = NULL
                WHERE reply_action_id = 'reply-action-1'
                """
            )
            conn.commit()
        self.assertTrue(runner._replay_reply_send_ack_outbox(binding))

        self.assertEqual(len(bridge.sent_replies), 1)
        self.assertEqual(
            load_reply_send_ack_outbox("reply-action-1")["status"],
            "confirmed",
        )

    def test_pending_c2_outbox_blocks_new_scan_round(self):
        api = FakeApi(None)
        api.message_ingest_error = ConnectionError("backend offline")
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        enqueue_c2_outbox(
            {
                "read_run_id": f"read-outbox-barrier-{time.time_ns()}",
                "conversation_id": "conv-outbox-barrier",
                "authorization_revision": "revision-outbox-barrier",
                "messages": [],
            }
        )

        runner._run_c2_scan_round(binding, reason="outbox_barrier")

        self.assertEqual(bridge.c2_operation_order, [])
        self.assertFalse(
            any(event.startswith("read_targets:") for event in api.events)
        )

    def test_c2_outbox_replay_is_single_flight_across_worker_threads(self):
        api = FakeApi(None)
        entered = threading.Event()
        release = threading.Event()
        attempts = 0
        source_key = f"source-outbox-single-flight-{time.time_ns()}"

        def blocking_ingest(_binding, payload):
            nonlocal attempts
            attempts += 1
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return {
                "results": [
                    {
                        "source_message_key": source_key,
                        "dedupe_key": payload["messages"][0]["dedupe_key"],
                        "ingest_result": "ingested",
                    }
                ],
                "ignored_count": 0,
            }

        api.post_wechat_messages_ingest = blocking_ingest  # type: ignore[method-assign]
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        save_c2_ledger_terminal(
            conversation_id="conv-outbox-single-flight",
            source_message_key=source_key,
            dedupe_key=f"dedupe:{source_key}",
            message_type="text",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-outbox-single-flight-{time.time_ns()}",
                "conversation_id": "conv-outbox-single-flight",
                "authorization_revision": "revision-single-flight",
                "messages": [
                    {
                        "source_message_key": source_key,
                        "dedupe_key": f"dedupe:{source_key}",
                    }
                ],
            }
        )
        results: list[bool] = []
        first = threading.Thread(
            target=lambda: results.append(runner._replay_c2_outbox(binding)),
        )
        second = threading.Thread(
            target=lambda: results.append(runner._replay_c2_outbox(binding)),
        )

        first.start()
        self.assertTrue(entered.wait(timeout=2))
        second.start()
        time.sleep(0.05)
        self.assertEqual(attempts, 1)
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(attempts, 1)
        self.assertEqual(results, [True, True])
        self.assertEqual(load_c2_outbox_entry(outbox_id)["status"], "confirmed")

    def test_duplicated_send_claim_without_physical_attempt_resumes_send_once(self):
        task = self.make_chat_reply_task(task_id="task-chat-duplicate-claim")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        api.claim_send_duplicated = True
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner.tick_once()

        self.assertEqual(len(bridge.sent_replies), 1)
        self.assertIn("sent_ack:sent:None", api.events)
        self.assertEqual(
            load_reply_send_ack_outbox("reply-action-1")["status"],
            "confirmed",
        )

    def test_unattempted_send_intent_is_released_for_task_recovery(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        save_reply_send_intent(
            reply_action_id="reply-action-interrupted",
            task_id="task-interrupted",
            send_token="send-token-interrupted",
        )

        self.assertTrue(runner._replay_reply_send_ack_outbox(binding))

        self.assertEqual(bridge.sent_replies, [])
        self.assertFalse(any(item.startswith("sent_ack:") for item in api.events))
        self.assertIsNone(
            load_reply_send_ack_outbox("reply-action-interrupted")
        )

    def test_triggered_send_intent_replays_unknown_without_resending(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        reply_action_id = "reply-action-triggered"
        save_reply_send_intent(
            reply_action_id=reply_action_id,
            task_id="task-triggered",
            send_token="send-token-triggered",
            reply_text_hash="hash-triggered",
        )
        bridge.send_transaction_journal_path(reply_action_id).write_text(
            json.dumps({"action_phase": "trigger_attempted"}),
            encoding="utf-8",
        )

        self.assertTrue(runner._replay_reply_send_ack_outbox(binding))

        self.assertEqual(bridge.sent_replies, [])
        self.assertIn(
            "sent_ack:unknown:SEND_INTERRUPTED_BEFORE_RESULT_PERSISTED",
            api.events,
        )
        self.assertEqual(
            load_reply_send_ack_outbox(reply_action_id)["status"],
            "confirmed",
        )

    def test_heartbeat_does_not_recover_send_intent_while_ui_flow_is_active(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.current_ui_lock = object()  # type: ignore[assignment]
        save_reply_send_intent(
            reply_action_id="reply-action-in-flight",
            task_id="task-in-flight",
            send_token="send-token-in-flight",
        )

        runner.tick_once()

        self.assertFalse(any(item.startswith("sent_ack:") for item in api.events))
        self.assertEqual(
            load_reply_send_ack_outbox("reply-action-in-flight")["status"],
            "intent",
        )

        runner.current_ui_lock = None
        runner.last_rpa_component_status = "ready"
        runner.last_wechat_status = "logged_in"
        runner.last_rpa_probe_at = time.monotonic()
        runner.tick_once()

        self.assertFalse(any(item.startswith("sent_ack:") for item in api.events))
        self.assertIsNone(
            load_reply_send_ack_outbox("reply-action-in-flight")
        )

    def test_running_chat_reply_recovery_does_not_claim_task_twice(self):
        task = self.make_chat_reply_task(task_id="task-chat-running", status="running")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertNotIn("claim:task-chat-running", api.events)
        self.assertIn("claim_send:reply-action-1", api.events)
        self.assertFalse(any(event.startswith("read_targets:") for event in api.events))
        self.assertIn("sent_ack:sent:None", api.events)

    def test_pending_chat_reply_without_exact_batch_ticket_is_not_sent(self):
        task = self.make_chat_reply_task(task_id="task-chat-no-ticket")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner.tick_once()

        self.assertEqual(bridge.sent_replies, [])
        self.assertIn(
            "fail:C2_REPLY_TARGET_NOT_AUTHORIZED:c2_reply_recovery",
            api.events,
        )
        self.assertFalse(any(event.startswith("read_targets:") for event in api.events))

    def test_pending_chat_reply_context_recovery_failure_is_reported_once(self):
        task = self.make_chat_reply_task(task_id="task-chat-context-failed")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        with patch.object(
            runner,
            "_read_one_wechat_target",
            return_value={"ok": False, "error_code": "C2_TARGET_CHAT_NOT_FOUND"},
        ):
            runner.tick_once()

        self.assertIn(
            "fail:C2_REPLY_CONTEXT_RECOVERY_FAILED:pre_send_refresh",
            api.events,
        )
        self.assertEqual(bridge.sent_replies, [])

    def test_chat_reply_pre_send_refresh_supersedes_when_new_customer_message_arrives(self):
        task = self.make_chat_reply_task(task_id="task-chat-new-message")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, seen = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("ingest:1", api.events)
        self.assertEqual(bridge.sent_replies, [])
        self.assertNotIn("claim_send:reply-action-1", api.events)
        self.assertTrue(any(result and result.error_code == "C3_REPLACEMENT_BATCH_MISSING" for result in seen["results"]))

    def test_chat_reply_claim_send_failure_never_sends(self):
        task = self.make_chat_reply_task(task_id="task-chat-fail")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        api.claim_send_error = ApiError("REPLY_ACTION_EXPIRED", "回复动作已过期", 409)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("claim_send:reply-action-1", api.events)
        self.assertEqual(bridge.sent_replies, [])
        self.assertIn("fail:REPLY_ACTION_EXPIRED:claim_send", api.events)

    def test_chat_reply_timeout_reports_unknown_ack(self):
        task = self.make_chat_reply_task(task_id="task-chat-unknown")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused"),
            send_payload={
                "ok": False,
                "error_code": "RPA_SIDECAR_TIMEOUT",
                "current_step": "rpa_sidecar_timeout",
                "state": "send_maybe_sent",
                "action_phase": "trigger_attempted",
                "physical_send_triggered": True,
            },
        )
        api.message_ingest_result = "duplicated"
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("sent_ack:unknown:RPA_SIDECAR_TIMEOUT", api.events)
        self.assertTrue(any(result and result.error_code == "RPA_SIDECAR_TIMEOUT" for result in _["results"]))

    def test_chat_reply_pre_send_refresh_blocks_stale_reply_when_customer_message_ingested(self):
        task = self.make_chat_reply_task(task_id="task-chat-stale")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, seen = self.make_runner(api, bridge)
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner.tick_once()

        self.assertIn("ingest:1", api.events)
        self.assertEqual(bridge.sent_replies, [])
        self.assertNotIn("claim_send:reply-action-1", api.events)
        self.assertTrue(any(result and result.error_code == "C3_REPLACEMENT_BATCH_MISSING" for result in seen["results"]))

    def test_c3_brain_wait_has_no_fixed_total_timeout_while_backend_is_alive(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-long-brain",
                rpa_session_key="",
                display_name="CJLONG01",
                remark_code="CJLONG01",
                read_reason="waiting_user_reply",
                authorization_revision="revision-long-brain",
            )
        ]
        authorization = {
            "allowed": True,
            "conversation_id": "conv-long-brain",
            "authorization_revision": "revision-long-brain",
            "read_reason": "waiting_user_reply",
        }
        statuses = [
            {"batch_id": "batch-long", "batch_status": "generating", "processing": True, "updated_at": "progress-1", "authorization": authorization},
            {"batch_id": "batch-long", "batch_status": "generating", "processing": True, "updated_at": "progress-2", "authorization": authorization},
            {"batch_id": "batch-long", "batch_status": "completed", "processing": False, "decision": "handoff", "updated_at": "done", "authorization": authorization},
        ]
        api.get_wechat_message_batch = lambda _binding, _batch_id: statuses.pop(0)  # type: ignore[attr-defined]
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        target = WechatReadTarget(
            conversation_id="conv-long-brain",
            rpa_session_key="",
            display_name="CJLONG01",
            remark_code="CJLONG01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-long-brain",
        )

        with patch("chejin_worker_client.task_runner.time.sleep", return_value=None), patch(
            "chejin_worker_client.task_runner.time.monotonic",
            side_effect=[0.0, 0.0, 300.0, 300.0, 600.0, 600.0],
        ):
            result = runner._wait_and_send_current_c3_batch(
                binding=binding,
                target=target,
                batch_id="batch-long",
                cancel_check=lambda: False,
            )

        assert result["ok"] is True
        assert result["sent"] is False
        assert result["batch"]["decision"] == "handoff"

    def test_c3_brain_wait_stops_when_backend_state_has_no_progress(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-stuck-brain",
                rpa_session_key="",
                display_name="CJSTUCK01",
                remark_code="CJSTUCK01",
                read_reason="waiting_user_reply",
                authorization_revision="revision-stuck-brain",
            )
        ]
        api.get_wechat_message_batch = lambda _binding, _batch_id: {  # type: ignore[attr-defined]
            "batch_id": "batch-stuck",
            "batch_status": "generating",
            "processing": True,
            "updated_at": "unchanged",
            "authorization": {
                "allowed": True,
                "conversation_id": "conv-stuck-brain",
                "authorization_revision": "revision-stuck-brain",
                "read_reason": "waiting_user_reply",
            },
        }
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        target = WechatReadTarget(
            conversation_id="conv-stuck-brain",
            rpa_session_key="",
            display_name="CJSTUCK01",
            remark_code="CJSTUCK01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-stuck-brain",
        )

        with patch("chejin_worker_client.task_runner.time.sleep", return_value=None), patch(
            "chejin_worker_client.task_runner.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 361.0],
        ):
            result = runner._wait_and_send_current_c3_batch(
                binding=binding,
                target=target,
                batch_id="batch-stuck",
                cancel_check=lambda: False,
            )

        assert result["ok"] is False
        assert result["error_code"] == "C3_BRAIN_STATE_NO_PROGRESS_WATCHDOG"

    def test_c3_pre_send_refresh_reuses_full_c2_flow_in_current_chat_only(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-pre-send",
                rpa_session_key="wx:rpa:v1:pre-send",
                display_name="CJPRE01",
                remark_code="CJPRE01",
                read_reason="waiting_user_reply",
                authorization_revision="revision-pre-send",
            )
        ]
        api.get_wechat_message_batch = lambda _binding, _batch_id: {  # type: ignore[attr-defined]
            "batch_id": "batch-send",
            "batch_status": "reply_action_created",
            "processing": False,
            "decision": "send_reply",
            "updated_at": "ready",
            "authorization": {
                "allowed": True,
                "conversation_id": "conv-pre-send",
                "authorization_revision": "revision-pre-send",
                "read_reason": "waiting_user_reply",
            },
        }
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        target = WechatReadTarget(
            conversation_id="conv-pre-send",
            rpa_session_key="wx:rpa:v1:pre-send",
            display_name="CJPRE01",
            remark_code="CJPRE01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-pre-send",
        )

        with patch.object(
            runner,
            "_read_one_wechat_target",
            return_value={"ok": True, "new_self_message_count": 1, "new_customer_message_count": 0, "result": {}},
        ) as refresh:
            result = runner._wait_and_send_current_c3_batch(
                binding=binding,
                target=target,
                batch_id="batch-send",
                cancel_check=lambda: False,
            )

        assert result["sent"] is False
        assert result["reason"] == "sales_replied_during_brain_wait"
        kwargs = refresh.call_args.kwargs
        assert kwargs["current_only"] is True
        assert kwargs["wait_for_brain"] is False
        assert kwargs["enforce_read_targets"] is True
        assert kwargs["allow_during_current_task"] is True

    def test_c2_visible_scan_reports_first_screen_sessions(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.binding = binding

        runner._scan_wechat_sessions(binding, reason="unit")

        self.assertIn("scan:1:None", api.events)
        self.assertEqual(api.scan_payloads[0]["sessions"][0]["remark_code_candidates"], ["CJTEST01"])
        self.assertEqual(bridge.session_scans[0], {})
        self.assertNotIn("scan_mode", api.scan_payloads[0]["evidence"])

    def test_c2_message_read_allows_target_without_row_fingerprint(self):
        api = FakeApi(None)
        api.read_targets = [WechatReadTarget(conversation_id="conv-1", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["display_name"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertIn("ingest:1", api.events)
        self.assertEqual(api.message_payloads[0]["evidence"]["target_row_fingerprint"], {})

    def test_c2_message_read_skips_target_without_remark_code(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.message_reads, [])
        self.assertNotIn("ingest:1", api.events)
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_REMARK_CODE_MISSING")

    def test_c2_message_read_skips_target_with_invalid_remark_code(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="张三",
                remark_code="NOT-A-C2-CODE",
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_REMARK_CODE_INVALID")

    def test_c2_message_read_skips_target_without_conversation_id(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.message_reads, [])
        self.assertNotIn("ingest:1", api.events)
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_CONVERSATION_ID_MISSING")

    def test_c2_message_read_uses_remark_code_without_legacy_locator(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="",
                display_name="",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.locate_chats[0]["display_name"], "CJTEST01")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "")
        self.assertEqual(bridge.message_reads[0]["display_name"], "CJTEST01")
        self.assertIn("ingest:1", api.events)

    def test_c2_target_dedupe_key_uses_identity_pair(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)

        self.assertEqual(
            runner._target_dedupe_key(
                WechatReadTarget(
                    conversation_id="conv-1",
                    rpa_session_key="wx:rpa:v1:a",
                    display_name="CJTEST01 许聪",
                    remark_code="CJTEST01",
                )
            ),
            "conversation:conv-1:remark_code:CJTEST01",
        )
        self.assertTrue(
            runner._target_dedupe_key(
                WechatReadTarget(conversation_id="", rpa_session_key="wx:rpa:v1:a", display_name="CJTEST01 许聪", remark_code="CJTEST01")
            ).startswith("invalid:")
        )

    def test_visible_target_matching_uses_structured_remark_candidates_only(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        sessions = [
            {
                "name": "普通会话",
                "last_message_preview": "客户提到了 CJTEST01",
                "remark_code_candidates": [],
            },
            {
                "name": "OCR 显示名可能变化",
                "last_message_preview": "无关预览",
                "remark_code_candidates": ["CJTEST01"],
            },
        ]

        matches = runner._visible_sessions_for_remark_code("CJTEST01", sessions)

        self.assertEqual(matches, [sessions[1]])

    def test_c2_message_read_uses_read_targets_only_and_ingests(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.locate_chats[0]["display_name"], "CJTEST01")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.message_reads[0]["display_name"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertEqual(bridge.message_reads[0]["remark_code"], "CJTEST01")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertIn("ingest:1", api.events)

    def test_c2_message_read_skips_voice_transcribe_when_messages_have_no_voice(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertIsNone(api.message_payloads[0]["evidence"]["voice_transcription"])

    def test_c2_message_read_does_not_bypass_v3_observations_with_legacy_visual_hint(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [{"id": "text-1", "type": "text", "sender_role": "customer", "content": "下午退吧"}],
                "visible_untranscribed_voice": {
                    "detected": True,
                    "source": "visual_self_voice_bubble_context_menu_anchor",
                    "sender_role": "self",
                    "anchor_stable_key": "voice-stable:self-2s",
                },
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.voice_transcribes, [])
        self.assertNotIn("voice_transcribe", bridge.c2_operation_order)

    def test_c2_message_read_rejects_visual_voice_hint_without_avatar_role(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [{"id": "text-1", "type": "text", "sender_role": "self", "content": "普通绿色文字"}],
                "visible_untranscribed_voice": {
                    "detected": True,
                    "source": "visual_self_voice_bubble_context_menu_anchor",
                    "sender_role": "unknown",
                },
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.voice_transcribes, [])

    def test_c2_read_cancelled_after_message_read_before_ingest_when_target_stopped(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-text-after-stop",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "好的",
                    }
                ],
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        api.read_targets = [target]
        calls = {"count": 0}

        def get_authorization(
            binding: Binding,
            conversation_id: str,
            **kwargs,
        ):
            api.events.append(f"read_authorization:{conversation_id}")
            calls["count"] += 1
            return {
                "allowed": calls["count"] <= 3,
                "conversation_id": conversation_id,
                "authorization_revision": target.authorization_revision,
                "read_reason": target.read_reason,
            }

        api.get_wechat_read_authorization = get_authorization  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_stop_guard_before_voice_seconds = 0
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])
        self.assertEqual(api.message_payloads, [])

    def test_c2_read_cancelled_by_stable_guard_before_voice_when_target_stops(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        api.read_targets = [target]
        calls = {"count": 0}

        def get_authorization(
            binding: Binding,
            conversation_id: str,
            **kwargs,
        ):
            api.events.append(f"read_authorization:{conversation_id}")
            calls["count"] += 1
            return {
                "allowed": calls["count"] <= 5,
                "conversation_id": conversation_id,
                "authorization_revision": target.authorization_revision,
                "read_reason": target.read_reason,
            }

        api.get_wechat_read_authorization = get_authorization  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_stop_guard_before_voice_seconds = 0.001
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertEqual(api.message_payloads, [])

    def test_c2_voice_transcription_is_ingested_as_voice_message(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-text",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "你好",
                    }
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_completed",
            "sidecar_run_id": "voice-run-1",
            "artifact_dir": "C:/voice-run-1",
            "attempt_count": 1,
            "quality_flags": [],
            "transcribed_messages": [{"content": "你好", "sender_role": "customer"}],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(
            bridge.c2_operation_order,
            ["locate_chat", "messages", "voice_transcribe", "messages"],
        )
        self.assertEqual(bridge.voice_transcribes[0]["target_mode"], "current")
        self.assertEqual(bridge.voice_transcribes[0]["max_duration_seconds"], 240)
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[1]["target_mode"], "current")
        self.assertIn("ingest:1", api.events)
        self.assertEqual(api.message_payloads[0]["messages"][0]["message_type"], "voice")
        self.assertEqual(api.message_payloads[0]["messages"][0]["sender_role_hint"], "customer")
        self.assertEqual(api.message_payloads[0]["messages"][0]["raw_payload"]["voice_transcription"], "你好")
        self.assertEqual(api.message_payloads[0]["messages"][0]["raw_payload"]["voice_transcription_meta"]["state"], "voice_transcribe_completed")
        timing = api.message_payloads[0]["evidence"]["timing"]
        self.assertEqual(timing["schema_version"], 1)
        self.assertEqual(
            [phase["name"] for phase in timing["phases"]],
            [
                "target_chat_locate",
                "initial_message_read",
                "voice_transcribe",
                "target_chat_reconfirm_and_final_read",
                "build_ingest_payload",
            ],
        )

    def test_action_journal_vertical_c2_voice_reaches_ingest_and_ledger(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-journal-vertical-voice",
            rpa_session_key="wx:rpa:v1:journal-voice",
            display_name="CJVOICE01 客户",
            remark_code="CJVOICE01",
            row_fingerprint={"title_text": "CJVOICE01 客户"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-journal-voice",
        )
        api.read_targets = [target]
        observed_phases: list[str] = []

        class JournalVoiceBridge(FakeBridge):
            def voice_transcribe(
                self,
                *,
                display_name: str,
                rpa_session_key: str,
                **kwargs,
            ):
                journal_path = Path(kwargs["action_journal"])
                journal = read_action_journal(journal_path)
                source_key = next(iter(journal["items"]))
                observed_phases.append(action_journal_phase(journal_path))
                update_action_journal_item(
                    journal_path,
                    source_message_key=source_key,
                    action_phase="trigger_attempted",
                    business_state="voice_menu_clicked",
                )
                observed_phases.append(action_journal_phase(journal_path))
                update_action_journal_item(
                    journal_path,
                    source_message_key=source_key,
                    action_phase="confirmed",
                    business_state="completed",
                    business_result_confirmed=True,
                    terminal_payload={
                        "state": "completed",
                        "transcribed_message": {
                            "content": "纵向语音已经转写。",
                            "sender_role": "customer",
                            "voice_anchor_stable_key": "voice-anchor-vertical",
                        },
                    },
                )
                observed_phases.append(action_journal_phase(journal_path))
                self.voice_payload = {
                    "ok": True,
                    "state": "voice_transcribe_completed",
                    "sidecar_run_id": "voice-journal-vertical",
                    "processed_voice_anchor_keys": [
                        "voice-anchor-vertical"
                    ],
                    "failed_voice_anchor_keys": [],
                    "item_action_outcomes": [
                        {
                            "action_phase": "confirmed",
                            "business_state": "completed",
                            "business_result_confirmed": True,
                            "physical_anchor_keys": [
                                "voice-anchor-vertical"
                            ],
                        }
                    ],
                    "transcribed_messages": [
                        {
                            "content": "纵向语音已经转写。",
                            "sender_role": "customer",
                            "voice_anchor_stable_key": "voice-anchor-vertical",
                        }
                    ],
                }
                return super().voice_transcribe(
                    display_name=display_name,
                    rpa_session_key=rpa_session_key,
                    **kwargs,
                )

        bridge = JournalVoiceBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "messages": [
                    {
                        "id": "voice-message-vertical",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 2"',
                        "voice_duration": 2,
                        "voice_anchor_stable_key": "voice-anchor-vertical",
                        "bubble_rect": [400, 120, 610, 165],
                    }
                ]
            },
            {
                "messages": [
                    {
                        "id": "voice-message-vertical",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "纵向语音已经转写。",
                        "voice_anchor_stable_key": "voice-anchor-vertical",
                        "bubble_rect": [400, 120, 700, 220],
                    }
                ]
            },
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-journal-voice",
            worker_token="token",
            client_instance_id="client-journal-voice",
            run_status="running",
        )

        result = runner._read_one_wechat_target(
            binding,
            target,
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            observed_phases,
            ["not_attempted", "trigger_attempted", "confirmed"],
        )
        self.assertEqual(len(api.message_payloads), 1)
        voice_message = api.message_payloads[0]["messages"][0]
        self.assertEqual(voice_message["message_type"], "voice")
        self.assertEqual(voice_message["content"], "纵向语音已经转写。")
        ledger = load_c2_ledger_entry(
            target.conversation_id,
            voice_message["source_message_key"],
        )
        self.assertEqual(ledger["terminal_state"], "completed")
        self.assertEqual(ledger["ingest_state"], "confirmed")
        self.assertEqual(
            list_c2_action_journal(target.conversation_id),
            [],
        )

    def test_c2_voice_ledger_is_checked_before_any_right_click(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        api.read_targets = [target]
        voice_observation = {
            "schema_version": 3,
            "observation_id": "voice-old",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "voice_anchor_key": "voice:customer:2:bottom:1",
            "source_message": {"voice_anchor_stable_key": "voice:customer:2:bottom:1"},
        }
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=voice_observation_source_key(target, voice_observation),
            dedupe_key=None,
            message_type="voice",
            terminal_state="completed",
            ingest_state="confirmed",
            result={"content": "已经处理过"},
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "authoritative_frame_source": "initial_read",
                "observations": [
                    voice_observation,
                    {
                        "schema_version": 3,
                        "observation_id": "text-new",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "新的文字",
                        "source_message": {"id": "text-new"},
                    },
                ],
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertTrue(result["ok"])
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertNotIn("voice_transcribe", bridge.c2_operation_order)
        self.assertEqual(api.message_payloads[0]["messages"][0]["content"], "新的文字")

    def test_c2_does_not_treat_omniauto_machine_fingerprint_as_authoritative(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        bridge.voice_payload = {
            "ok": True,
            "contract_sha256": "0" * 64,
            "state": "voice_transcribe_completed",
            "transcribed_messages": [{"content": "你好", "sender_role": "customer"}],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages", "voice_transcribe", "messages"])
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["contract_sha256"], contract_sha256())

    def test_c2_partial_voice_ingests_success_gates_customer_failure_and_skips_retry(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "voice-customer-ok",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 3"',
                        "bubble_rect": [400, 100, 600, 140],
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    },
                    {
                        "id": "voice-customer-failed",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 6"',
                        "bubble_rect": [400, 200, 600, 240],
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    },
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-text",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "果然掉在更衣柜里了。",
                        "voice_anchor_stable_key": "voice-customer-ok",
                        "bubble_rect": [400, 100, 700, 160],
                    },
                    {
                        "id": "voice-customer-failed",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 6"',
                        "bubble_rect": [400, 200, 600, 240],
                        "quality_flags": ["untranscribed_voice_placeholder"],
                    },
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_partial",
            "sidecar_run_id": "voice-run-partial",
            "attempt_count": 1,
            "quality_flags": ["untranscribed_voice_remaining"],
            "processed_voice_anchor_keys": ["voice-customer-ok"],
            "failed_voice_anchor_keys": ["voice-customer-failed"],
            "transcribed_messages": [
                {
                    "content": "果然掉在更衣柜里了。",
                    "sender_role": "customer",
                    "voice_anchor_stable_key": "voice-customer-ok",
                }
            ],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(
            bridge.c2_operation_order,
            ["locate_chat", "messages", "voice_transcribe", "messages"],
        )
        self.assertIn("ingest:2", api.events)
        self.assertEqual(len(api.message_payloads[0]["messages"]), 2)
        completed_voice = next(
            item
            for item in api.message_payloads[0]["messages"]
            if item["item_state"] == "completed"
        )
        failed_voice = next(
            item
            for item in api.message_payloads[0]["messages"]
            if item["item_state"] == "failed"
        )
        self.assertEqual(completed_voice["content"], "果然掉在更衣柜里了。")
        self.assertEqual(
            completed_voice["raw_payload"]["voice_transcription_meta"]["state"],
            "voice_transcribe_partial",
        )
        self.assertEqual(failed_voice["message_type"], "voice")
        self.assertEqual(failed_voice["sender_role_hint"], "customer")
        self.assertIsNone(failed_voice["content"])
        self.assertEqual(failed_voice["flow_state"], "failed")
        self.assertIn(
            "C2_VOICE_TRANSCRIBE_FAILED",
            api.message_payloads[0]["evidence"]["flow_gate_errors"],
        )
        self.assertEqual(
            api.message_payloads[0]["evidence"]["flow_gate_details"],
            [
                {
                    "error_code": "C2_VOICE_TRANSCRIBE_FAILED",
                    "position_source": "failed_voice_visual_top",
                    "subject_sender_role": "customer",
                    "min_screen_order": 2,
                    "max_screen_order": 2,
                }
            ],
        )
        failed_observation = bridge._contractual_message_payload(
            {
                "messages": [
                    {
                        "id": "voice-customer-failed",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 6"',
                    }
                ]
            }
        )["observations"][0]
        failed_source_key = voice_observation_source_key(
            api.read_targets[0],
            failed_observation,
        )
        failed_ledger = load_c2_ledger_entry("conv-1", failed_source_key)
        self.assertEqual(failed_ledger["terminal_state"], "failed")
        self.assertEqual(failed_ledger["ingest_state"], "confirmed")

        runner.c2_read_failure_cooldowns.clear()
        runner.c2_read_success_cooldowns.clear()
        bridge.c2_operation_order.clear()
        bridge.get_messages_payloads = [
            {
                "messages": [
                    {
                        "id": "voice-customer-ok",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "果然掉在更衣柜里了。",
                        "voice_anchor_stable_key": "voice-customer-ok",
                    },
                    {
                        "id": "voice-customer-failed",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 6"',
                    },
                ]
            }
        ]
        runner._read_state_target_queue(binding)
        self.assertNotIn("voice_transcribe", bridge.c2_operation_order)

    def test_c2_partial_sales_voice_is_persisted_as_human_intervention_fact(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-partial-sales",
            rpa_session_key="wx:rpa:v1:partial-sales",
            display_name="CJSALE01",
            remark_code="CJSALE01",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-conv-partial-sales",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "messages": [
                    {
                        "id": "voice-customer-completed",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 3"',
                        "bubble_rect": [400, 100, 600, 140],
                    },
                    {
                        "id": "voice-sales-failed",
                        "type": "voice",
                        "sender_role": "self",
                        "content": '[语音] 5"',
                        "bubble_rect": [700, 200, 900, 240],
                    },
                ]
            },
            {
                "messages": [
                    {
                        "id": "voice-customer-completed",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "客户语音已经成功。",
                        "voice_anchor_stable_key": "voice-customer-completed",
                        "bubble_rect": [400, 100, 700, 160],
                    },
                    {
                        "id": "voice-sales-failed",
                        "type": "voice",
                        "sender_role": "self",
                        "content": '[语音] 5"',
                        "bubble_rect": [700, 200, 900, 240],
                    },
                ]
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "state": "voice_transcribe_partial",
            "sidecar_run_id": "voice-run-partial-sales",
            "processed_voice_anchor_keys": ["voice-customer-completed"],
            "failed_voice_anchor_keys": ["voice-sales-failed"],
            "transcribed_messages": [
                {
                    "content": "客户语音已经成功。",
                    "sender_role": "customer",
                    "voice_anchor_stable_key": "voice-customer-completed",
                }
            ],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        result = runner._read_one_wechat_target(
            binding,
            target,
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertTrue(result["ok"])
        payload = api.message_payloads[0]
        failed_sales = next(
            item
            for item in payload["messages"]
            if item["item_state"] == "failed"
        )
        self.assertEqual(failed_sales["sender_role_hint"], "self")
        self.assertEqual(failed_sales["message_type"], "voice")
        self.assertEqual(
            payload["evidence"]["flow_gate_details"],
            [
                {
                    "error_code": "C2_VOICE_TRANSCRIBE_FAILED",
                    "position_source": "failed_voice_visual_top",
                    "subject_sender_role": "self",
                    "min_screen_order": 2,
                    "max_screen_order": 2,
                }
            ],
        )
        failed_source_key = failed_sales["source_message_key"]
        failed_ledger = load_c2_ledger_entry(
            target.conversation_id,
            failed_source_key,
        )
        self.assertEqual(failed_ledger["terminal_state"], "failed")
        self.assertEqual(failed_ledger["ingest_state"], "confirmed")

    def test_c2_failed_voice_does_not_block_reliable_image_vision(self):
        api = FakeApi(None)
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-voice-image-{unique}",
            rpa_session_key="wx:rpa:v1:voice-image",
            display_name="CJMIX01",
            remark_code="CJMIX01",
            read_reason="waiting_user_reply",
            authorization_revision=f"revision-voice-image-{unique}",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )

        def frame_payload() -> dict:
            return {
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "voice-customer-failed",
                        "row_kind": "voice_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "voice",
                        "voice_state": "untranscribed",
                        "item_state": "discovered",
                        "voice_anchor_key": "voice-customer-failed",
                        "bubble_rect": [400, 100, 600, 140],
                        "source_message": {
                            "id": "voice-customer-failed",
                            "type": "voice",
                            "sender_role": "customer",
                            "content": '[语音] 6"',
                            "voice_anchor_stable_key": "voice-customer-failed",
                        },
                    },
                    {
                        "schema_version": 3,
                        "observation_id": "image-customer-ready",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                        "item_state": "discovered",
                        "image_physical_anchor": {
                            "sender_role": "customer",
                            "preceding_stable_message": "voice-customer-failed",
                            "following_stable_message": "",
                            "occurrence_index": 0,
                        },
                        "bubble_rect": [420, 180, 650, 320],
                        "source_message": {
                            "id": "image-customer-ready",
                            "type": "image",
                            "sender_role": "customer",
                        },
                    },
                ]
            }

        bridge.get_messages_payloads = [
            frame_payload(),
            frame_payload(),
            frame_payload(),
        ]
        bridge.voice_payload = {
            "ok": True,
            "state": "voice_transcribe_partial",
            "sidecar_run_id": "voice-run-failed-with-image",
            "processed_voice_anchor_keys": [],
            "failed_voice_anchor_keys": ["voice-customer-failed"],
            "transcribed_messages": [],
        }
        completed_image = {
            "state": "completed",
            "action_phase": "confirmed",
            "reason": "vision_ready",
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "客户发来一张车辆外观图",
            },
            "visual_bridge_input": {"summary": "车辆外观图"},
            "transaction": {"image_sha256": "b" * 64},
            "diagnostics": {
                "schema_version": 1,
                "trace_id": "image-customer-ready",
                "total_duration_ms": 800,
                "image_persisted": False,
                "events": [],
            },
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={
                "ready": True,
                "config": {
                    "customer_image_understanding": {"enabled": True}
                },
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value=completed_image,
        ) as vision:
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=False,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(vision.call_count, 1)
        self.assertEqual(len(api.message_payloads), 1)
        payload = api.message_payloads[0]
        self.assertIn(
            "C2_VOICE_TRANSCRIBE_FAILED",
            payload["evidence"]["flow_gate_errors"],
        )
        failed_voice = next(
            item
            for item in payload["messages"]
            if item["message_type"] == "voice"
        )
        completed_image_message = next(
            item
            for item in payload["messages"]
            if item["message_type"] == "image"
        )
        self.assertEqual(failed_voice["item_state"], "failed")
        self.assertEqual(completed_image_message["item_state"], "completed")
        self.assertEqual(
            completed_image_message["content"],
            "客户发来一张车辆外观图",
        )

    def test_c2_failed_voice_does_not_block_new_voice_in_same_ui_lock(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-new-voice-during-transcribe",
            rpa_session_key="wx:rpa:v1:new-voice",
            display_name="CJNEW01",
            remark_code="CJNEW01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-new-voice",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "messages": [
                    {
                        "id": "voice-existing",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 3"',
                        "bubble_rect": [400, 100, 600, 140],
                    }
                ]
            },
            {
                "messages": [
                    {
                        "id": "voice-existing",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 3"',
                        "bubble_rect": [400, 100, 600, 140],
                    },
                    {
                        "id": "voice-arrived-later",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 4"',
                        "bubble_rect": [400, 220, 600, 260],
                    },
                ]
            },
            {
                "messages": [
                    {
                        "id": "voice-existing",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 3"',
                        "bubble_rect": [400, 100, 600, 140],
                    },
                    {
                        "id": "voice-arrived-later",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "后来到达的语音也已转写",
                        "voice_anchor_stable_key": "voice-arrived-later",
                        "bubble_rect": [400, 220, 700, 280],
                    },
                ]
            },
        ]
        bridge.voice_payloads = [
            {
                "ok": True,
                "state": "voice_transcribe_partial",
                "sidecar_run_id": "voice-run-existing-failed",
                "processed_voice_anchor_keys": [],
                "failed_voice_anchor_keys": ["voice-existing"],
                "transcribed_messages": [],
                "item_action_outcomes": [
                    {
                        "physical_anchor_keys": ["voice-existing"],
                        "action_phase": "trigger_attempted",
                        "business_state": "failed",
                        "business_result_confirmed": False,
                        "error_code": "VOICE_TRANSCRIBE_RESULT_UNKNOWN",
                    }
                ],
            },
            {
                "ok": True,
                "state": "voice_transcribe_completed",
                "sidecar_run_id": "voice-run-new-arrival",
                "processed_voice_anchor_keys": ["voice-arrived-later"],
                "failed_voice_anchor_keys": [],
                "transcribed_messages": [
                    {
                        "content": "后来到达的语音也已转写",
                        "sender_role": "customer",
                        "voice_anchor_stable_key": "voice-arrived-later",
                    }
                ],
                "item_action_outcomes": [
                    {
                        "physical_anchor_keys": [
                            "voice-arrived-later"
                        ],
                        "action_phase": "confirmed",
                        "business_state": "completed",
                        "business_result_confirmed": True,
                    }
                ],
            },
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        result = runner._read_one_wechat_target(
            binding,
            target,
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(bridge.voice_transcribes), 2)
        self.assertIn(
            "voice-existing",
            bridge.voice_transcribes[1]["excluded_voice_anchor_keys"],
        )
        self.assertEqual(len(api.message_payloads), 1)
        messages = api.message_payloads[0]["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(
            next(
                item
                for item in messages
                if item["content"] == "后来到达的语音也已转写"
            )["item_state"],
            "completed",
        )
        self.assertEqual(
            next(
                item
                for item in messages
                if item["item_state"] == "failed"
            )["message_type"],
            "voice",
        )

    def test_new_voice_helper_keeps_one_success_and_one_failure(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-new-voice-mixed",
            rpa_session_key="wx:rpa:v1:new-voice-mixed",
            display_name="CJMIX01",
            remark_code="CJMIX01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-new-voice-mixed",
        )
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        initial = bridge._contractual_message_payload(
            {
                "messages": [
                    {
                        "id": "voice-later-success",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 3"',
                        "bubble_rect": [400, 100, 600, 140],
                    },
                    {
                        "id": "voice-later-failed",
                        "type": "voice",
                        "sender_role": "self",
                        "content": '[语音] 4"',
                        "bubble_rect": [700, 220, 900, 260],
                    },
                ]
            }
        )
        bridge.voice_payloads = [
            {
                "ok": True,
                "state": "voice_transcribe_partial",
                "sidecar_run_id": "voice-run-later-mixed",
                "processed_voice_anchor_keys": ["voice-later-success"],
                "failed_voice_anchor_keys": ["voice-later-failed"],
                "transcribed_messages": [],
                "item_action_outcomes": [
                    {
                        "physical_anchor_keys": [
                            "voice-later-success"
                        ],
                        "action_phase": "confirmed",
                        "business_state": "completed",
                        "business_result_confirmed": True,
                    },
                    {
                        "physical_anchor_keys": [
                            "voice-later-failed"
                        ],
                        "action_phase": "trigger_attempted",
                        "business_state": "failed",
                        "business_result_confirmed": False,
                        "error_code": "VOICE_TRANSCRIBE_RESULT_UNKNOWN",
                    },
                ],
            }
        ]
        bridge.get_messages_payloads = [
            {
                "messages": [
                    {
                        "id": "voice-later-success",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "后来成功的语音",
                        "voice_anchor_stable_key": "voice-later-success",
                        "bubble_rect": [400, 100, 700, 160],
                    },
                    {
                        "id": "voice-later-failed",
                        "type": "voice",
                        "sender_role": "self",
                        "content": '[语音] 4"',
                        "bubble_rect": [700, 220, 900, 260],
                    },
                ]
            }
        ]
        runner, _ = self.make_runner(api, bridge)

        class Lease:
            def update_step(self, _step):
                return None

        result = runner._finish_new_visible_voices_in_current_chat(
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target=target,
            target_label="CJMIX01",
            sidecar_payload=initial,
            lease=Lease(),  # type: ignore[arg-type]
            action_cancel_requested=lambda: False,
            enforce_read_targets=False,
            excluded_voice_anchor_keys=set(),
        )

        failed_observation = next(
            item
            for item in initial["observations"]
            if item["observation_id"] == "voice-later-failed"
        )
        failed_source_key = voice_observation_source_key(
            target,
            failed_observation,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed_source_keys"], [failed_source_key])
        self.assertEqual(result["failed_roles"], {failed_source_key: "self"})
        self.assertEqual(
            result["failure_code"],
            "VOICE_TRANSCRIBE_PARTIAL",
        )
        self.assertEqual(
            {
                item["result"]
                for item in result["item_outcomes"]
            },
            {"completed", "failed"},
        )
        self.assertEqual(
            len(result["item_outcomes"]),
            2,
        )

    def test_c2_failed_voice_waiting_is_reassembled_without_repeating_rpa(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-failed-voice-recovery",
            rpa_session_key="wx:rpa:v1:failed-voice-recovery",
            display_name="CJRECOVER01",
            remark_code="CJRECOVER01",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-conv-failed-voice-recovery",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        failed_message = {
            "id": "voice-failed-before-authorization-refresh",
            "type": "voice",
            "sender_role": "customer",
            "content": '[语音] 5"',
            "bubble_rect": [400, 100, 600, 140],
        }
        failed_observation = bridge._contractual_message_payload(
            {"messages": [failed_message]}
        )["observations"][0]
        failed_source_key = voice_observation_source_key(
            target,
            failed_observation,
        )
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=failed_source_key,
            dedupe_key=None,
            message_type="voice",
            terminal_state="failed",
            ingest_state="waiting",
            result={
                "state": "failed",
                "error_code": "VOICE_TRANSCRIBE_PARTIAL",
            },
        )
        bridge.get_messages_payloads = [{"messages": [failed_message]}]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        result = runner._read_one_wechat_target(
            binding,
            target,
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertTrue(result["ok"])
        self.assertNotIn("voice_transcribe", bridge.c2_operation_order)
        self.assertEqual(len(api.message_payloads), 1)
        recovered = api.message_payloads[0]["messages"][0]
        self.assertEqual(recovered["source_message_key"], failed_source_key)
        self.assertEqual(recovered["item_state"], "failed")
        self.assertEqual(recovered["sender_role_hint"], "customer")
        self.assertIn(
            "C2_VOICE_TRANSCRIBE_FAILED",
            api.message_payloads[0]["evidence"]["flow_gate_errors"],
        )
        ledger = load_c2_ledger_entry(
            target.conversation_id,
            failed_source_key,
        )
        self.assertEqual(ledger["terminal_state"], "failed")
        self.assertEqual(ledger["ingest_state"], "confirmed")

    def test_c2_brain_technical_failure_is_not_reported_as_read_success(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-flow-failed",
            rpa_session_key="wx:rpa:v1:flow-failed",
            display_name="CJFLOW01",
            remark_code="CJFLOW01",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-conv-flow-failed",
        )
        api.read_targets = [target]
        api.message_batch_result = {
            "batch_id": "batch-flow-failed",
            "batch_status": "generating",
        }
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "messages": [
                    {
                        "id": "customer-text-flow-failed",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "请问还在吗？",
                    }
                ]
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        with patch.object(
            runner,
            "_wait_and_send_current_c3_batch",
            return_value={
                "ok": False,
                "error_code": "C3_BRAIN_STATE_NO_PROGRESS_WATCHDOG",
            },
        ):
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=True,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["fact_ingest_ok"])
        self.assertFalse(result["conversation_flow_ok"])
        self.assertEqual(result["conversation_terminal_state"], "technical_failure")
        self.assertEqual(
            result["error_code"],
            "C3_BRAIN_STATE_NO_PROGRESS_WATCHDOG",
        )
        self.assertEqual(
            TaskRunner._conversation_flow_outcome(
                {"ok": True, "batch": {"decision": "handoff"}, "sent": False},
                had_message_batch=True,
            ),
            (True, "handoff", None),
        )

    def test_c2_text_noise_does_not_trigger_voice_transcribe(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-sales-voice-noise-1",
                        "type": "text",
                        "sender_role": "sales_candidate",
                        "content": '2" (c',
                        "content_raw_ocr": '2" (c',
                        "quality_flags": ["ocr_low_confidence"],
                    }
                ],
            },
            {"ok": True, "messages": []},
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_no_new_text",
            "sidecar_run_id": "voice-run-no-text",
            "attempt_count": 1,
            "quality_flags": ["no_new_transcribed_text"],
            "transcribed_messages": [],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.c2_operation_order, ["locate_chat", "messages"])
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(runner.c2_stats["last_error"], "MESSAGE_ROW_SENDER_ROLE_INVALID")

    def test_c2_unbound_visible_transcript_closes_current_screen_and_blocks_reclick(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
        )
        api.read_targets = [target]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        initial_voice = {
            "ok": True,
            "messages": [
                {
                    "id": "voice-23-before",
                    "type": "voice",
                    "sender_role": "customer",
                    "voice_duration": 23,
                    "content": '[语音] 23"',
                    "quality_flags": ["untranscribed_voice_placeholder"],
                }
            ],
        }
        bridge.get_messages_payloads = [
            initial_voice,
            {
                "ok": True,
                "messages": [
                    {
                        "id": "voice-23-before",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 23,
                        "content": '[语音] 23"',
                        "quality_flags": ["untranscribed_voice_placeholder"],
                        "bubble_rect": [120, 200, 260, 240],
                    },
                    {
                        "id": "text-after-unbound",
                        "type": "text",
                        "sender_role": "self",
                        "content": "我稍后确认",
                        "bubble_rect": [500, 260, 760, 300],
                    },
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_no_new_text",
            "sidecar_run_id": "voice-run-unbound",
            "attempt_count": 1,
            "quality_flags": ["no_new_transcribed_text", "voice_transcribe_anchor_failed"],
            "transcribed_messages": [],
            "new_messages": [
                {
                    "id": "voice-23-expanded",
                    "type": "voice",
                    "sender_role": "customer",
                    "content": "然后，你看那个数字人直播这块儿。",
                    "quality_flags": ["voice_duration_prefix_removed"],
                }
            ],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            {
                (item["message_type"], item["sender_role_hint"])
                for item in api.message_payloads[0]["messages"]
            },
            {("voice", "customer"), ("text", "self")},
        )
        self.assertEqual(api.message_payloads[0]["evidence"]["flow_gate_errors"], ["C2_VOICE_TRANSCRIBE_FAILED"])
        failed_voice = next(
            item
            for item in api.message_payloads[0]["messages"]
            if item["message_type"] == "voice"
        )
        self.assertEqual(
            failed_voice["raw_payload"]["voice_processing_reason"],
            "VOICE_TRANSCRIPT_BINDING_INCONSISTENT",
        )
        self.assertEqual(
            bridge.c2_operation_order,
            ["locate_chat", "messages", "voice_transcribe", "messages"],
        )
        self.assertEqual(len(runner.c2_voice_binding_blocked_authorizations), 1)

        runner.c2_read_failure_cooldowns.clear()
        runner.c2_read_success_cooldowns.clear()
        bridge.c2_operation_order.clear()
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {"id": "text-after-failure", "type": "text", "sender_role": "self", "content": "ok"}
                ],
            }
        ]
        runner._read_state_target_queue(binding)

        self.assertEqual(len(api.message_payloads), 1)
        self.assertNotIn("voice_transcribe", bridge.c2_operation_order)

    def test_identity_failure_gate_uses_stable_key_and_empty_message_payload(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            authorization_revision="revision-conv-1",
        )
        errors = [
            {
                "observation_id": "frame",
                "row_kind": "message_sequence",
                "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                "signature": "text:customer:好的",
                "reason": "all_visible_messages_are_identical_across_rounds",
            }
        ]

        self.assertTrue(
            runner._report_identity_failure_gate(
                binding=binding,
                target=target,
                error_code="MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                identity_errors=errors,
            )
        )
        first_payload = api.message_payloads[0]
        self.assertEqual(first_payload["messages"], [])
        self.assertEqual(
            first_payload["evidence"]["flow_gate_errors"],
            ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
        )
        first_key = first_payload["evidence"]["flow_gate_identity_key"]
        self.assertEqual(len(first_key), 64)

        self.assertTrue(
            runner._report_identity_failure_gate(
                binding=binding,
                target=target,
                error_code="MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                identity_errors=errors,
            )
        )
        self.assertEqual(
            len(api.message_payloads),
            1,
            "confirmed Outbox must not be submitted to the backend twice",
        )

    def test_cross_round_identity_ambiguity_blocks_vision_ingest_and_brain(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "ambiguous-image",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                        "item_state": "discovered",
                        "bubble_rect": [420, 180, 650, 320],
                        "image_physical_anchor": {
                            "sender_role": "customer",
                            "preceding_stable_message": "",
                            "following_stable_message": "",
                            "bubble_visual_fingerprint": (
                                "dhash64:1234567890abcdef"
                            ),
                            "occurrence_index": 0,
                            "occurrence_count": 1,
                        },
                        "source_message": {
                            "id": "ambiguous-image",
                            "type": "image",
                        },
                    }
                ],
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-cross-round-ambiguous",
            rpa_session_key="",
            display_name="CJAMB01",
            remark_code="CJAMB01",
            authorization_revision="revision-cross-round-ambiguous",
        )
        identity_errors = [
            {
                "observation_id": "ambiguous-image",
                "row_kind": "image_bubble",
                "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                "signature": "ambiguous-signature",
            }
        ]

        def reject_identity(_target, observations, previous_state):
            return list(observations), dict(previous_state or {}), identity_errors

        with patch(
            "chejin_worker_client.task_runner."
            "reconcile_v16104_identity_transition",
            side_effect=reject_identity,
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot"
        ) as vision, patch.object(
            runner,
            "_wait_and_send_current_c3_batch",
        ) as brain:
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        )
        vision.assert_not_called()
        brain.assert_not_called()
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["messages"], [])
        self.assertEqual(
            api.message_payloads[0]["evidence"]["flow_gate_errors"],
            ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
        )

    def test_all_c2_outbox_submit_types_preserve_original_json_after_network_failure(self):
        api = FakeApi(None)
        api.message_ingest_error = RuntimeError("network down")
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        for index, operation in enumerate(
            ("message_ingest", "voice_failure_gate", "identity_failure_gate"),
            start=1,
        ):
            payload = {
                "read_run_id": f"read-common-outbox-{index}",
                "conversation_id": "conv-1",
                "authorization_revision": "revision-conv-1",
                "messages": [],
                "evidence": {"operation": operation},
            }
            delivery = runner._submit_c2_outbox_payload(
                binding=binding,
                payload=payload,
                operation=operation,
            )
            self.assertFalse(delivery["ok"])

        stored = []
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id
                FROM c2_ingest_outbox
                WHERE read_run_id LIKE 'read-common-outbox-%'
                ORDER BY read_run_id
                """
            ).fetchall()
        stored = [
            load_c2_outbox_entry(row["outbox_id"])
            for row in rows
        ]
        self.assertEqual(len(stored), 3)
        self.assertEqual(
            {item["payload"]["evidence"]["operation"] for item in stored},
            {"message_ingest", "voice_failure_gate", "identity_failure_gate"},
        )

    def test_c2_outbox_persists_300kb_raw_payload_before_transport_compaction(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(
                    ok=True,
                    result_code="invite_sent",
                    message="unused",
                )
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        read_run_id = f"read-raw-300kb-{time.time_ns()}"
        raw_payload = {
            "source_message_key": "source-raw-300kb",
            "observation": {
                "observation_id": "observation-raw-300kb",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "text",
                "voice_state": "not_voice",
                "content_clean": "保留正文",
                "source_message": {
                    "id": "source-raw-300kb",
                    "content": "保留正文",
                },
            },
            "diagnostic_blob": "x" * 307_200,
        }
        payload = {
            "read_run_id": read_run_id,
            "conversation_id": "conv-raw-300kb",
            "authorization_revision": "revision-raw-300kb",
            "messages": [
                {
                    "source_message_key": "source-raw-300kb",
                    "dedupe_key": "dedupe-raw-300kb",
                    "message_type": "text",
                    "raw_payload": raw_payload,
                }
            ],
            "evidence": {
                "observations": [raw_payload["observation"]],
            },
        }

        from chejin_worker_client.c2_outbox_recovery import (
            split_ingest_payload as real_split_ingest_payload,
        )

        def assert_raw_checkpoint_exists(candidate):
            stored = load_c2_outbox_entry(
                f"c2-outbox:{read_run_id}"
            )
            self.assertIsNotNone(stored)
            self.assertEqual(
                stored["payload"]["messages"][0]["raw_payload"][
                    "diagnostic_blob"
                ],
                raw_payload["diagnostic_blob"],
            )
            return real_split_ingest_payload(candidate)

        with patch(
            "chejin_worker_client.task_runner.split_ingest_payload",
            side_effect=assert_raw_checkpoint_exists,
        ):
            delivery = runner._submit_c2_outbox_payload(
                binding=binding,
                payload=payload,
                operation="message_ingest",
            )

        self.assertTrue(delivery["ok"])
        sent_raw = api.message_payloads[0]["messages"][0]["raw_payload"]
        self.assertNotIn("diagnostic_blob", sent_raw)
        self.assertLessEqual(
            len(
                json.dumps(
                    sent_raw,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
            256 * 1024,
        )

    def test_c2_outbox_keeps_raw_checkpoint_when_single_item_cannot_split(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(
                    ok=True,
                    result_code="invite_sent",
                    message="unused",
                )
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        read_run_id = f"read-single-too-large-{time.time_ns()}"
        payload = {
            "read_run_id": read_run_id,
            "conversation_id": "conv-single-too-large",
            "authorization_revision": "revision-single-too-large",
            "messages": [
                {
                    "source_message_key": "source-single-too-large",
                    "dedupe_key": "dedupe-single-too-large",
                    "message_type": "text",
                    "content": "x" * 1_600_000,
                }
            ],
            "evidence": {"observations": []},
        }

        delivery = runner._submit_c2_outbox_payload(
            binding=binding,
            payload=payload,
            operation="message_ingest",
        )

        self.assertFalse(delivery["ok"])
        stored = load_c2_outbox_entry(f"c2-outbox:{read_run_id}")
        self.assertEqual(stored["status"], "capability_paused")
        self.assertEqual(
            stored["payload"]["messages"][0]["content"],
            payload["messages"][0]["content"],
        )
        self.assertEqual(api.message_payloads, [])

    def test_c2_outbox_keeps_parent_when_partition_persistence_is_interrupted(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(
                    ok=True,
                    result_code="invite_sent",
                    message="unused",
                )
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        read_run_id = f"read-split-interrupted-{time.time_ns()}"
        messages = [
            {
                "source_message_key": f"source-split-{index}",
                "dedupe_key": f"dedupe-split-{index}",
                "message_type": "text",
                "content": f"{index}:" + ("x" * 18_000),
            }
            for index in range(100)
        ]
        payload = {
            "read_run_id": read_run_id,
            "conversation_id": "conv-split-interrupted",
            "authorization_revision": "revision-split-interrupted",
            "messages": messages,
            "evidence": {"observations": []},
        }

        with patch(
            "chejin_worker_client.task_runner.replace_c2_outbox_with_partitions",
            side_effect=RuntimeError("simulated split interruption"),
        ):
            delivery = runner._submit_c2_outbox_payload(
                binding=binding,
                payload=payload,
                operation="message_ingest",
            )

        self.assertFalse(delivery["ok"])
        stored = load_c2_outbox_entry(f"c2-outbox:{read_run_id}")
        self.assertEqual(stored["status"], "capability_paused")
        self.assertEqual(stored["payload"]["messages"], messages)
        self.assertEqual(api.message_payloads, [])

    def test_c2_voice_click_failed_still_closes_current_screen_in_one_ingest(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                        "bubble_rect": [120, 200, 240, 240],
                    },
                    {
                        "id": "wx-msg-text-same-screen",
                        "type": "text",
                        "sender_role": "self",
                        "content": "我稍后看一下",
                        "bubble_rect": [500, 260, 760, 300],
                    },
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                        "bubble_rect": [120, 200, 240, 240],
                    },
                    {
                        "id": "wx-msg-text-same-screen",
                        "type": "text",
                        "sender_role": "self",
                        "content": "我稍后看一下",
                        "bubble_rect": [500, 260, 760, 300],
                    },
                ],
            }
        ]
        bridge.voice_payload = {
            "ok": False,
            "adapter": "mock",
            "state": "voice_transcribe_click_failed",
            "error_code": "VOICE_TRANSCRIBE_CLICK_FAILED",
            "sidecar_run_id": "voice-run-click-failed",
            "attempt_count": 1,
            "quality_flags": ["voice_transcribe_click_failed"],
            "transcribed_messages": [],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(
            bridge.c2_operation_order,
            ["locate_chat", "messages", "voice_transcribe", "messages"],
        )
        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(len(bridge.message_reads), 2)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            {
                (item["message_type"], item["sender_role_hint"])
                for item in api.message_payloads[0]["messages"]
            },
            {("voice", "customer"), ("text", "self")},
        )
        self.assertEqual(api.message_payloads[0]["evidence"]["flow_gate_errors"], ["C2_VOICE_TRANSCRIBE_FAILED"])
        failed_voice = next(
            item
            for item in api.message_payloads[0]["messages"]
            if item["message_type"] == "voice"
        )
        self.assertEqual(failed_voice["item_state"], "failed")
        self.assertEqual(
            failed_voice["raw_payload"]["voice_processing_reason"],
            "VOICE_TRANSCRIBE_CLICK_FAILED",
        )

    def test_c2_voice_sidecar_timeout_still_closes_current_screen_in_one_ingest(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                        "bubble_rect": [120, 200, 240, 240],
                    },
                    {
                        "id": "wx-msg-text-same-screen",
                        "type": "text",
                        "sender_role": "self",
                        "content": "我稍后看一下",
                        "bubble_rect": [500, 260, 760, 300],
                    },
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                        "bubble_rect": [120, 200, 240, 240],
                    },
                    {
                        "id": "wx-msg-text-same-screen",
                        "type": "text",
                        "sender_role": "self",
                        "content": "我稍后看一下",
                        "bubble_rect": [500, 260, 760, 300],
                    },
                ],
            }
        ]
        bridge.voice_payload = {
            "ok": False,
            "adapter": "mock",
            "error_code": "RPA_SIDECAR_TIMEOUT",
            "current_step": "rpa_sidecar_timeout",
            "sidecar_run_id": "voice-run-timeout",
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(
            bridge.c2_operation_order,
            ["locate_chat", "messages", "voice_transcribe", "messages"],
        )
        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(len(bridge.message_reads), 2)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            {
                (item["message_type"], item["sender_role_hint"])
                for item in api.message_payloads[0]["messages"]
            },
            {("voice", "customer"), ("text", "self")},
        )
        self.assertEqual(api.message_payloads[0]["evidence"]["flow_gate_errors"], ["C2_VOICE_TRANSCRIBE_FAILED"])
        failed_voice = next(
            item
            for item in api.message_payloads[0]["messages"]
            if item["message_type"] == "voice"
        )
        self.assertEqual(failed_voice["item_state"], "failed")
        self.assertEqual(
            failed_voice["raw_payload"]["voice_processing_reason"],
            "RPA_SIDECAR_TIMEOUT",
        )

    def test_c2_voice_timeout_does_not_mark_failed_when_final_frame_proves_transcript(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-timeout-final-success",
            rpa_session_key="wx:rpa:v1:timeout-final-success",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )
        initial_voice = {
            "id": "wx-msg-timeout-final-success",
            "type": "voice",
            "sender_role": "customer",
            "voice_duration": 2,
            "content": '[语音] 2"',
            "voice_anchor_stable_key": "voice-anchor-timeout-final-success",
            "bubble_rect": [120, 200, 240, 240],
        }
        final_voice = {
            **initial_voice,
            "content": "我下午三点有空",
        }
        bridge.get_messages_payloads = [
            {"ok": True, "messages": [initial_voice]},
            {"ok": True, "messages": [final_voice]},
        ]
        bridge.voice_payload = {
            "ok": False,
            "adapter": "mock",
            "error_code": "RPA_SIDECAR_TIMEOUT",
            "current_step": "rpa_sidecar_timeout",
            "sidecar_run_id": "voice-run-timeout-final-success",
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner._read_state_target_queue(binding)

        self.assertEqual(len(api.message_payloads), 1)
        payload = api.message_payloads[0]
        self.assertEqual(payload["evidence"]["flow_gate_errors"], [])
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["message_type"], "voice")
        self.assertEqual(payload["messages"][0]["item_state"], "completed")
        self.assertEqual(payload["messages"][0]["content"], "我下午三点有空")

    def test_c2_voice_cancelled_by_stop_does_not_terminalize_or_report_failure(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-voice-cancelled",
            rpa_session_key="wx:rpa:v1:voice-cancelled",
            display_name="CJCANCEL01",
            remark_code="CJCANCEL01",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-voice-cancelled",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        raw_voice = {
            "id": "voice-cancelled-before-finish",
            "type": "voice",
            "sender_role": "customer",
            "content": '[语音] 2"',
        }
        bridge.get_messages_payloads = [{"messages": [raw_voice]}]
        bridge.voice_payload = {
            "ok": False,
            "state": "voice_transcribe_cancelled",
            "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
            "sidecar_run_id": "voice-run-cancelled",
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        result = runner._read_one_wechat_target(
            binding,
            target,
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
        )
        self.assertEqual(api.message_payloads, [])
        observation = bridge._contractual_message_payload(
            {"messages": [raw_voice]}
        )["observations"][0]
        source_key = voice_observation_source_key(target, observation)
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, source_key)
        )

    def test_c2_message_read_failure_enters_cooldown_before_retry(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def failing_get_messages(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.message_reads.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
            return {
                "ok": False,
                "error_code": "TARGET_NOT_CONFIRMED_FOR_MESSAGES",
                "sidecar_run_id": "message-failed-1",
                "artifact_dir": "C:/artifact/message-failed-1",
            }

        bridge.get_messages = failing_get_messages  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)
        runner._read_state_target_queue(binding)

        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(runner.c2_stats["last_error"], "TARGET_NOT_CONFIRMED_FOR_MESSAGES")

    def test_c2_message_read_success_enters_short_cooldown_before_retry(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)
        runner._read_state_target_queue(binding)

        self.assertEqual(len(bridge.message_reads), 1)
        self.assertTrue(runner.c2_read_success_cooldowns)

    def test_c2_repeated_read_reuses_current_chat_before_searching_again(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        first = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=False)
        second = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=False)

        self.assertTrue(first.get("ok"))
        self.assertTrue(second.get("ok"))
        self.assertEqual([item["target_mode"] for item in bridge.locate_chats[:2]], ["visible", "current"])

    def test_c2_recent_visible_scan_survives_read_target_permission_delay(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="",
                display_name="CJR8S5K3 虾丸子大人",
                remark_code="CJR8S5K3",
                row_fingerprint={"title_text": "CJR8S5K3 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
                authorization_revision="revision-conv-1",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, _ = self.make_runner(api, bridge)
        runner.c2_last_visible_sessions = [
            {
                "display_name": "CJR8S5K3 虾丸子大.",
                "rpa_session_key": "wx:rpa:v1:recent",
                "remark_code_candidates": ["CJR8S5K3"],
                "last_message_preview": '[语音] 2"',
                "ocr_confidence": 0.94,
            }
        ]
        runner.c2_last_visible_sessions_monotonic = time.monotonic() - 60.0
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, api.read_targets[0], current_step="state_target_message_read", enforce_read_targets=False)

        self.assertTrue(result.get("ok"))
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:recent")
        self.assertNotIn("search_by_remark_code", [item.get("target_mode") for item in bridge.locate_chats])

    def test_c2_reuses_open_chat_confirmation_frame_for_initial_read(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-frame-reuse",
            rpa_session_key="wx:rpa:v1:frame-reuse",
            display_name="CJFRAME01 客户",
            remark_code="CJFRAME01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-frame-reuse",
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="unused", message="unused"))
        snapshot = bridge._contractual_message_payload(
            {
                "ok": True,
                "state": "messages_ocr",
                "sidecar_run_id": "locate-frame-1",
                "messages": [
                    {
                        "id": "frame-text-1",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "复用打开会话时的画面",
                    }
                ],
            }
        )
        bridge.locate_payloads = [
            {
                "ok": True,
                "state": "chat_target_confirmed",
                "initial_messages_frame_reused": True,
                "initial_messages_snapshot": snapshot,
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target)

        self.assertTrue(result.get("ok"))
        self.assertEqual(bridge.message_reads, [])
        self.assertTrue(bridge.locate_chats[0]["capture_initial_messages"])
        self.assertEqual(api.message_payloads[0]["messages"][0]["content"], "复用打开会话时的画面")

    def test_c2_recent_visible_hit_survives_one_ocr_miss(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJR8S5K3 虾丸子大人",
                remark_code="CJR8S5K3",
                row_fingerprint={"title_text": "CJR8S5K3 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
                authorization_revision="revision-conv-1",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")

        def list_missed_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-miss",
                "sessions": [
                    {"name": "腾讯新闻", "session_key": "wx:rpa:v1:news", "content": "新闻", "ocr_confidence": 0.98}
                ],
            }

        bridge.list_sessions = list_missed_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_recent_visible_hits_by_remark_code["CJR8S5K3"] = {
            "seen_at": time.monotonic() - 20.0,
            "session": {
                "display_name": "CJR8S5K3 虾丸子大.",
                "rpa_session_key": "wx:rpa:v1:recent-hit",
                "remark_code_candidates": ["CJR8S5K3"],
                "row_fingerprint": {"title_text": "CJR8S5K3 虾丸子大.", "title_bbox": [155, 118, 373, 141], "row_y_bucket": 16},
                "ocr_confidence": 0.95,
            },
        }
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, api.read_targets[0], current_step="state_target_message_read", enforce_read_targets=False)

        self.assertTrue(result.get("ok"))
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:recent-hit")
        self.assertEqual(bridge.locate_chats[0]["visible_session_candidate"]["center_y"], 129.5)
        self.assertNotIn("search_by_remark_code", [item.get("target_mode") for item in bridge.locate_chats])

    def test_c2_visible_hit_reads_before_state_target_and_dedupes_same_round(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertEqual(bridge.message_reads[0]["remark_code"], "CJTEST01")
        self.assertEqual(api.events.count("ingest:1"), 1)
        self.assertIn("read_targets:20", api.events)

    def test_c2_visible_hit_uses_current_scan_session_key_when_backend_binding_key_is_stale(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:stale-binding",
                display_name="CJVOICE01 许聪",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 许聪"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]

        def list_voice_session(**_kwargs):
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-voice",
                "sessions": [
                    {
                        "name": "CJVOICE01 许聪",
                        "session_key": "wx:rpa:v1:current-visible",
                        "row_fingerprint": {"title_text": "CJVOICE01 许聪", "title_bbox": [154, 115, 306, 143]},
                        "content": '[语音] 2"',
                        "unread_signal": True,
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        def post_scan_with_stale_key(binding: Binding, payload: dict):
            api.scan_payloads.append(payload)
            api.events.append(f"scan:{len(payload.get('sessions') or [])}:{payload.get('error_code')}")
            return {
                "bound_count": 1,
                "bindings": [
                    {
                        "conversation_id": "conv-voice",
                        "lead_id": "lead-voice",
                        "sales_id": "sales-1",
                        "remark_code": "CJVOICE01",
                        "rpa_session_key": "wx:rpa:v1:stale-binding",
                        "display_name": "CJVOICE01 许聪",
                        "row_fingerprint": {"title_text": "CJVOICE01 许聪"},
                        "ocr_confidence": 0.98,
                        "can_ingest_messages": True,
                    }
                ],
            }

        bridge.list_sessions = list_voice_session  # type: ignore[method-assign]
        api.post_wechat_session_scan_result = post_scan_with_stale_key  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:current-visible")
        self.assertEqual(bridge.locate_chats[0]["visible_session_candidate"]["center_y"], 129.0)
        self.assertEqual(bridge.locate_chats[0]["visible_session_candidate"]["click_geometry_source"], "row_fingerprint.title_bbox")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")

    def test_c2_visible_candidate_derives_click_geometry_from_title_bbox(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)

        candidate = runner._sidecar_visible_session_candidate(
            {
                "name": "CJR8S5K3虾丸子大..",
                "session_key": "wx:rpa:v1:visible",
                "row_fingerprint": {"title_text": "CJR8S5K3虾丸子大..", "title_bbox": [154, 115, 372, 143], "row_y_bucket": 16},
                "content": "好多人",
                "ocr_confidence": 0.955,
            }
        )

        self.assertEqual(candidate["center_y"], 129.0)
        self.assertEqual(candidate["top"], 115.0)
        self.assertEqual(candidate["bottom"], 143.0)
        self.assertEqual(candidate["click_geometry_source"], "row_fingerprint.title_bbox")

    def test_c2_visible_session_geometry_replaces_mapped_hash_with_raw_fingerprint(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)

        sessions = runner._visible_sessions_with_click_geometry(
            [
                {
                    "display_name": "CJR8S5K3虾丸子大",
                    "rpa_session_key": "wx:rpa:v1:visible",
                    "remark_code_candidates": ["CJR8S5K3"],
                    "row_fingerprint": "mapped-hash-only",
                }
            ],
            [
                {
                    "name": "CJR8S5K3虾丸子大",
                    "session_key": "wx:rpa:v1:visible",
                    "row_fingerprint": {"title_text": "CJR8S5K3虾丸子大", "title_bbox": [155, 118, 373, 141], "row_y_bucket": 16},
                }
            ],
        )

        self.assertEqual(sessions[0]["center_y"], 129.5)
        self.assertIsInstance(sessions[0]["row_fingerprint"], dict)

    def test_c2_state_target_merges_realtime_visible_scan_into_locate(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_voice_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-visible-now",
                "sessions": [
                    {
                        "name": "CJVOICE01 虾丸子大人",
                        "session_key": "wx:rpa:v1:visible-now",
                        "row_fingerprint": {"title_text": "CJVOICE01 虾丸子大人"},
                        "content": '[语音] 2"',
                        "unread_signal": True,
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_voice_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertNotIn("sessions", bridge.c2_operation_order)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:backend")
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJVOICE01")

    def test_c2_state_target_passes_short_code_to_atomic_visible_locate(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJR8S5K3 虾丸子大人",
                remark_code="CJR8S5K3",
                row_fingerprint={"title_text": "CJR8S5K3 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_visible_session_with_spaced_code():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-visible-spaced",
                "sessions": [
                    {
                        "name": "CJR8 S5K3 虾丸子大人",
                        "session_key": "wx:rpa:v1:visible-spaced",
                        "row_fingerprint": {"title_text": "CJR8 S5K3 虾丸子大人"},
                        "content": '[语音] 2"',
                        "unread_signal": True,
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_visible_session_with_spaced_code  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertNotIn("sessions", bridge.c2_operation_order)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:backend")
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJR8S5K3")

    def test_c2_state_target_falls_back_to_search_when_realtime_visible_misses(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_other_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-other",
                "sessions": [
                    {
                        "name": "CJOTHER01 许聪",
                        "session_key": "wx:rpa:v1:other",
                        "content": "你好",
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_other_session  # type: ignore[method-assign]
        bridge.locate_payloads = [
            {"ok": False, "state": "target_not_confirmed", "error_code": "TARGET_NOT_CONFIRMED"},
            {"ok": True, "state": "chat_target_confirmed"},
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual([item["target_mode"] for item in bridge.locate_chats], ["visible", "search_by_remark_code"])
        self.assertEqual(bridge.locate_chats[1]["rpa_session_key"], "")

    def test_c2_state_target_uses_recent_visible_scan_before_search(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_missed_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-miss",
                "sessions": [
                    {
                        "name": "腾讯新闻",
                        "session_key": "wx:rpa:v1:news",
                        "content": "新闻",
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_missed_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_last_visible_sessions = [
            {
                "display_name": "CJVOICE01 虾丸子大人",
                "rpa_session_key": "wx:rpa:v1:recent-visible",
                "remark_code_candidates": ["CJVOICE01"],
                "last_message_preview": '[语音] 2"',
                "row_fingerprint": {"title_text": "CJVOICE01 虾丸子大人", "title_bbox": [154, 198, 306, 222]},
                "ocr_confidence": 0.98,
            }
        ]
        runner.c2_last_visible_sessions_monotonic = time.monotonic()
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:recent-visible")
        self.assertEqual(bridge.locate_chats[0]["visible_session_candidate"]["center_y"], 210.0)
        self.assertNotEqual(bridge.locate_chats[0]["target_mode"], "search_by_remark_code")

    def test_c2_state_target_does_not_use_expired_recent_visible_scan(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_missed_session():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-miss",
                "sessions": [
                    {"name": "腾讯新闻", "session_key": "wx:rpa:v1:news", "content": "新闻", "ocr_confidence": 0.98}
                ],
            }

        bridge.list_sessions = list_missed_session  # type: ignore[method-assign]
        bridge.locate_payloads = [
            {"ok": False, "state": "target_not_confirmed", "error_code": "TARGET_NOT_CONFIRMED"},
            {"ok": True, "state": "chat_target_confirmed"},
        ]
        runner, _ = self.make_runner(api, bridge)
        runner.c2_last_visible_sessions = [
            {
                "display_name": "CJVOICE01 虾丸子大人",
                "rpa_session_key": "wx:rpa:v1:recent-visible",
                "remark_code_candidates": ["CJVOICE01"],
                "last_message_preview": '[语音] 2"',
                "row_fingerprint": {"title_text": "CJVOICE01 虾丸子大人"},
                "ocr_confidence": 0.98,
            }
        ]
        runner.c2_last_visible_sessions_monotonic = time.monotonic() - C2_RECENT_VISIBLE_CACHE_TTL_SECONDS - 1.0
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual([item["target_mode"] for item in bridge.locate_chats], ["visible", "search_by_remark_code"])
        self.assertEqual(bridge.locate_chats[1]["rpa_session_key"], "")

    def test_c2_state_target_rejects_ambiguous_realtime_visible_matches(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJVOICE01 虾丸子大人",
                remark_code="CJVOICE01",
                row_fingerprint={"title_text": "CJVOICE01 虾丸子大人"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_duplicate_sessions():
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-ambiguous",
                "sessions": [
                    {"name": "CJVOICE01 虾丸子大人", "session_key": "wx:rpa:v1:a", "content": '[语音] 2"', "ocr_confidence": 0.98},
                    {"name": "群聊", "session_key": "wx:rpa:v1:b", "content": "包含:CJVOICE01 虾丸子大人", "ocr_confidence": 0.98},
                ],
            }

        bridge.list_sessions = list_duplicate_sessions  # type: ignore[method-assign]
        bridge.locate_payloads = [
            {"ok": False, "state": "target_not_confirmed", "error_code": "C2_VISIBLE_TARGET_AMBIGUOUS"},
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._read_state_target_queue(binding)

        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_VISIBLE_TARGET_AMBIGUOUS")

    def test_c2_state_target_does_not_search_after_conversation_admission_rejection(self):
        for error_code in ("C2_GROUP_CHAT_NOT_ALLOWED", "C2_CONVERSATION_TYPE_UNKNOWN"):
            with self.subTest(error_code=error_code):
                api = FakeApi(None)
                api.read_targets = [
                    WechatReadTarget(
                        conversation_id="conv-group-guard",
                        rpa_session_key="wx:rpa:v1:group-guard",
                        display_name="CJTEST01",
                        remark_code="CJTEST01",
                        row_fingerprint={"title_text": "CJTEST01"},
                        ocr_confidence=0.98,
                        read_reason="waiting_user_reply",
                    )
                ]
                bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
                bridge.locate_payloads = [
                    {"ok": False, "state": "target_not_confirmed", "error_code": error_code},
                ]
                runner, _ = self.make_runner(api, bridge)
                binding = Binding(
                    worker_id="worker-1",
                    worker_token="token",
                    client_instance_id="client-1",
                    run_status="running",
                )

                runner._read_state_target_queue(binding)

                self.assertEqual([item["target_mode"] for item in bridge.locate_chats], ["visible"])
                self.assertEqual(bridge.message_reads, [])
                self.assertEqual(runner.c2_stats["last_error"], error_code)

    def test_c2_visible_hit_attempt_skips_state_target_search_in_same_round_even_when_visible_read_fails(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def failing_visible_get_messages(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.c2_operation_order.append("messages")
            bridge.message_reads.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
            return {
                "ok": False,
                "error_code": "TARGET_NOT_CONFIRMED_FOR_MESSAGES",
                "sidecar_run_id": "message-visible-failed",
            }

        bridge.get_messages = failing_visible_get_messages  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(bridge.locate_chats[0]["target_mode"], "visible")
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertEqual(bridge.message_reads[0]["rpa_session_key"], "")
        self.assertIn("read_targets:20", api.events)
        self.assertNotIn("search_by_remark_code", [item.get("target_mode") for item in bridge.locate_chats])

    def test_c2_visible_hit_locate_falls_back_to_remark_search_when_not_confirmed(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def locate_chat(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.c2_operation_order.append("locate_chat")
            bridge.locate_chats.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
            mode = kwargs.get("target_mode") or "visible"
            if mode == "visible":
                return {
                    "ok": False,
                    "adapter": "mock",
                    "state": "target_not_confirmed",
                    "sidecar_run_id": "locate-visible-failed",
                    "target_mode": "visible",
                }
            return {
                "ok": True,
                "adapter": "mock",
                "state": "chat_target_confirmed",
                "sidecar_run_id": "locate-search-ok",
                "target_mode": mode,
            }

        bridge.locate_chat = locate_chat  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual([item["target_mode"] for item in bridge.locate_chats[:2]], ["visible", "search_by_remark_code"])
        self.assertEqual(bridge.locate_chats[0]["rpa_session_key"], "wx:rpa:v1:a")
        self.assertEqual(bridge.locate_chats[1]["rpa_session_key"], "")
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertIn("ingest:1", api.events)

    def test_c2_visible_hit_queue_is_cleared_when_backend_read_targets_empty(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(bridge.voice_transcribes, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(runner.visible_hit_queue, [])
        self.assertIn("read_targets:20", api.events)

    def test_c2_visible_hit_inherits_current_read_target_authorization(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        visible_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:visible",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="visible_hit",
            raw={"visible_session_source": "first_screen_session_scan"},
        )
        authorized_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:backend",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-current",
            raw={"authorization_revision": "revision-current"},
        )
        captured: list[WechatReadTarget] = []

        def capture_read(binding: Binding, target: WechatReadTarget, **kwargs):
            captured.append(target)
            return {"ok": True}

        runner._read_one_wechat_target = capture_read  # type: ignore[method-assign]
        runner.visible_hit_queue = [visible_target]

        runner._drain_visible_hit_queue(
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            authorized_targets=[authorized_target],
        )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].authorization_revision, "revision-current")
        self.assertEqual(captured[0].rpa_session_key, "wx:rpa:v1:visible")
        self.assertEqual(captured[0].raw["authorization_read_reason"], "waiting_user_reply")

    def test_c2_visible_hit_v3_ingest_carries_current_authorization_revision(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="waiting_user_reply",
                authorization_revision="revision-current",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "text-1",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "明天下午三点联系。",
                        "source_message": {"id": "text-1", "type": "text", "content": "明天下午三点联系。"},
                    }
                ],
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["contract_version"], 3)
        self.assertEqual(api.message_payloads[0]["authorization_revision"], "revision-current")

    def test_c2_backend_ignored_message_is_not_reported_as_success(self):
        api = FakeApi(None)
        api.message_ingest_result = "ignored"
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="waiting_user_reply",
                authorization_revision="revision-current",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "text-ignored",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "后端忽略不能算成功",
                        "source_message": {"id": "text-ignored", "type": "text"},
                    }
                ],
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(runner.c2_stats["last_error"], "MESSAGE_ROW_ROLE_SOURCE_UNTRUSTED")
        self.assertEqual(len(api.message_payloads), 1)
        source_key = api.message_payloads[0]["messages"][0]["source_message_key"]
        self.assertEqual(load_c2_ledger_entry("conv-1", source_key)["ingest_state"], "not_required")

    def test_c2_visible_hit_without_current_authorization_is_dropped_before_ui_action(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        visible_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:visible",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="visible_hit",
        )
        authorized_without_revision = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:backend",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
        )
        runner.visible_hit_queue = [visible_target]

        runner._drain_visible_hit_queue(
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            authorized_targets=[authorized_without_revision],
        )

        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(runner.visible_hit_queue, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_AUTHORIZATION_REVISION_MISSING")

    def test_c2_read_authorization_requires_exact_revision(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="waiting_user_reply",
                authorization_revision="revision-new",
            )
        ]
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1")
        stale_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-old",
        )
        current_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-new",
        )

        self.assertFalse(runner._backend_still_allows_read_target(binding, stale_target))
        self.assertTrue(runner._backend_still_allows_read_target(binding, current_target))

    def test_c2_visible_read_rechecks_read_targets_after_voice_before_messages(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        api.read_targets = [target]
        calls = {"count": 0}

        def get_authorization(
            binding: Binding,
            conversation_id: str,
            **kwargs,
        ):
            api.events.append(f"read_authorization:{conversation_id}")
            calls["count"] += 1
            return {
                "allowed": calls["count"] <= 5,
                "conversation_id": conversation_id,
                "authorization_revision": target.authorization_revision,
                "read_reason": target.read_reason,
            }

        api.get_wechat_read_authorization = get_authorization  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertFalse(LOCK_FILE.exists())

    def test_c2_visible_read_cancelled_after_voice_before_reconfirm_when_target_stopped(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                    }
                ],
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        api.read_targets = [target]
        calls = {"count": 0}

        def get_authorization(
            binding: Binding,
            conversation_id: str,
            **kwargs,
        ):
            api.events.append(f"read_authorization:{conversation_id}")
            calls["count"] += 1
            return {
                "allowed": calls["count"] <= 5,
                "conversation_id": conversation_id,
                "authorization_revision": target.authorization_revision,
                "read_reason": target.read_reason,
            }

        api.get_wechat_read_authorization = get_authorization  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual([item["target_mode"] for item in bridge.locate_chats], ["visible"])
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(runner.c2_stats["last_error"], "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertFalse(LOCK_FILE.exists())

    def test_c2_read_authorization_requires_same_read_reason_for_state_target(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
                read_reason="recall_precheck",
                authorization_revision="revision-conv-1",
            )
        ]
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        self.assertFalse(runner._backend_still_allows_read_target(binding, target))

    def test_batch_authorization_requires_same_read_reason_with_same_revision(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="",
            display_name="CJTEST01",
            remark_code="CJTEST01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-same",
        )

        self.assertFalse(
            runner._batch_authorization_allows_target(
                {
                    "authorization": {
                        "allowed": True,
                        "conversation_id": "conv-1",
                        "authorization_revision": "revision-same",
                        "read_reason": "recall_precheck",
                    }
                },
                target,
            )
        )

    def test_friend_acceptance_target_never_falls_back_to_short_code_search(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="unused", message="unused"))
        target = WechatReadTarget(
            conversation_id="conv-friend-visible-only",
            rpa_session_key="wx:rpa:v1:friend",
            display_name="CJFRIEND01 新好友",
            remark_code="CJFRIEND01",
            read_reason="friend_acceptance_visible_hit",
            authorization_revision="revision-friend-visible-only",
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target)

        assert result["error_code"] == "C2_FRIEND_ACCEPTANCE_NOT_VISIBLE"
        assert bridge.locate_chats == []
        assert bridge.message_reads == []

    def test_friend_activation_is_confirmed_before_first_message_read(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="unused", message="unused"))
        bridge.locate_payloads = [
            {
                "ok": True,
                "state": "chat_target_confirmed",
                "conversation_type": "private",
                "conversation_type_evidence": {
                    "matched": True,
                    "short_code_confirmed": True,
                    "admission_allowed": True,
                    "conversation_type": "private",
                    "raw_title": "CJFRIEND01 新好友",
                },
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-friend-activation",
            rpa_session_key="wx:rpa:v1:friend",
            display_name="CJFRIEND01 新好友",
            remark_code="CJFRIEND01",
            read_reason="friend_acceptance_visible_hit",
            authorization_revision="revision-friend-activation",
        )
        runner, _ = self.make_runner(api, bridge)
        runner.c2_last_visible_sessions = [
            {
                "display_name": "CJFRIEND01 新好友",
                "rpa_session_key": "wx:rpa:v1:friend",
                "remark_code_candidates": ["CJFRIEND01"],
                "ocr_confidence": 0.98,
            }
        ]
        runner.c2_last_visible_sessions_monotonic = time.monotonic()
        original_get_messages = bridge.get_messages

        def guarded_get_messages(**kwargs):
            self.assertEqual(len(api.friend_activation_payloads), 1)
            return original_get_messages(**kwargs)

        bridge.get_messages = guarded_get_messages  # type: ignore[method-assign]
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target)

        self.assertTrue(result.get("ok"))
        self.assertEqual(len(api.friend_activation_payloads), 1)
        self.assertEqual(api.friend_activation_payloads[0]["conversation_type"], "private")
        self.assertFalse(bridge.locate_chats[0]["capture_initial_messages"])
        self.assertEqual(len(bridge.message_reads), 1)

    def test_c2_read_cancelled_before_locating_when_read_targets_empty_after_lock(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertFalse(LOCK_FILE.exists())

    def test_c2_read_cancelled_after_visible_check_before_locating_when_target_stopped(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            row_fingerprint={"title_text": "CJTEST01 许聪"},
            ocr_confidence=0.98,
            read_reason="waiting_user_reply",
            authorization_revision="revision-conv-1",
        )
        def reject_authorization(
            binding: Binding,
            conversation_id: str,
            **kwargs,
        ):
            api.events.append(f"read_authorization:{conversation_id}")
            return {
                "allowed": False,
                "conversation_id": conversation_id,
                "authorization_revision": "",
                "read_reason": "",
            }

        api.get_wechat_read_authorization = reject_authorization  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=True)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS")
        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(
            api.events.count("read_authorization:conv-1"),
            1,
        )
        self.assertFalse(LOCK_FILE.exists())

    def test_c2_scan_interrupted_when_high_priority_task_active(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.current_task = Task(id="task-chat", task_type="chat_reply", status="running")

        runner._scan_wechat_sessions(binding, reason="unit")

        self.assertEqual(runner.c2_stats["last_error"], "C2_SCAN_SKIPPED_BY_HIGH_PRIORITY_ACTION")
        self.assertEqual(api.scan_payloads, [])
        self.assertEqual(bridge.session_scans, [])

    def test_c2_listener_scans_after_start_when_wechat_ready(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        runner.last_rpa_component_status = "ready"
        runner.last_wechat_status = "logged_in"
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertTrue(api.scan_payloads)
        self.assertNotIn("scan_type", api.scan_payloads[0])

    def test_c2_outbox_refreshes_expired_authorization_without_rebuilding_facts(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-outbox-expired",
                rpa_session_key="wx:rpa:v1:outbox-expired",
                display_name="CJEXPIRE01 客户",
                remark_code="CJEXPIRE01",
                authorization_revision="fresh-revision",
                read_reason="waiting_sales_reply",
            )
        ]
        attempts = 0

        def reject_old_authorization(_binding, _payload):
            nonlocal attempts
            attempts += 1
            if _payload.get("authorization_revision") == "expired-revision":
                raise ApiError(
                    "MESSAGE_AUTHORIZATION_REVISION_EXPIRED",
                    "expired",
                    409,
                    {"recovery_action": "refresh_and_rebuild"},
                )
            return {
                "results": [
                    {
                        "source_message_key": source_key,
                        "ingest_result": "ingested",
                    }
                ]
            }

        api.post_wechat_messages_ingest = reject_old_authorization  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        source_key = "voice-expired-authorization"
        save_c2_ledger_terminal(
            conversation_id="conv-outbox-expired",
            source_message_key=source_key,
            dedupe_key="dedupe-voice-expired-authorization",
            message_type="voice",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-outbox-expired-{time.time_ns()}",
                "conversation_id": "conv-outbox-expired",
                "authorization_revision": "expired-revision",
                "messages": [
                    {
                        "source_message_key": source_key,
                        "dedupe_key": "dedupe-voice-expired-authorization",
                    }
                ],
            }
        )

        assert runner._replay_c2_outbox(binding) is False
        waiting = next(
            item
            for item in list_c2_outbox_waiting(limit=100)
            if item["outbox_id"] == outbox_id
        )
        self.assertEqual(
            waiting["payload"]["authorization_revision"],
            "fresh-revision",
        )
        self.assertEqual(
            waiting["payload"]["messages"][0]["source_message_key"],
            source_key,
        )
        assert runner._replay_c2_outbox(binding) is True
        assert all(
            item["outbox_id"] != outbox_id
            for item in list_c2_outbox_waiting(limit=100)
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(
            load_c2_ledger_entry(
                "conv-outbox-expired",
                source_key,
            )["ingest_state"],
            "confirmed",
        )

    def test_c2_outbox_rebuilds_invalid_voice_as_failed_fact(self):
        api = FakeApi(None)

        def reject_invalid_payload(_binding, _payload):
            raise ApiError(
                "VOICE_TRANSCRIBE_INVALID_CONTENT",
                "invalid voice payload",
                409,
                {
                    "retryable": False,
                    "recovery_action": "rebuild_failed_facts",
                    "source_message_key": "voice-invalid-structural",
                },
            )

        api.post_wechat_messages_ingest = reject_invalid_payload  # type: ignore[method-assign]
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        source_key = "voice-invalid-structural"
        save_c2_ledger_terminal(
            conversation_id="conv-outbox-invalid",
            source_message_key=source_key,
            dedupe_key="dedupe-voice-invalid-structural",
            message_type="voice",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-outbox-invalid-{time.time_ns()}",
                "conversation_id": "conv-outbox-invalid",
                "authorization_revision": "revision-invalid",
                "messages": [
                    {
                        "source_message_key": source_key,
                        "dedupe_key": "dedupe-voice-invalid-structural",
                        "message_type": "voice",
                        "sender_role_hint": "customer",
                        "content": '5"',
                        "item_state": "completed",
                        "flow_state": "completed",
                        "message_position": {
                            "screen_order": 1,
                            "visual_top": 100,
                            "visual_bottom": 140,
                            "frame_source": "final_read",
                            "order_source": "visual_top",
                        },
                        "raw_payload": {
                            "observation": {
                                "observation_id": "observation-invalid-voice",
                                "row_kind": "voice_transcript",
                                "sender_role": "customer",
                                "sender_role_source": "parent_voice",
                                "message_type": "voice",
                                "voice_state": "transcribed",
                                "content_clean": '5"',
                                "parent_voice_anchor_key": "anchor-invalid-voice",
                                "source_message": {"id": "invalid-voice"},
                            }
                        },
                    }
                ],
                "evidence": {
                    "observations": [
                        {
                            "observation_id": "observation-invalid-voice",
                            "row_kind": "voice_transcript",
                            "sender_role": "customer",
                            "sender_role_source": "parent_voice",
                            "message_type": "voice",
                            "voice_state": "transcribed",
                            "content_clean": '5"',
                            "parent_voice_anchor_key": "anchor-invalid-voice",
                            "source_message": {"id": "invalid-voice"},
                        }
                    ],
                    "flow_gate_errors": [],
                    "flow_gate_details": [],
                },
            }
        )

        assert runner._replay_c2_outbox(binding) is False
        stored = load_c2_outbox_entry(outbox_id)
        self.assertEqual(stored["status"], "waiting")
        self.assertEqual(
            stored["payload"]["messages"][0]["source_message_key"],
            source_key,
        )
        self.assertEqual(
            stored["payload"]["messages"][0]["item_state"],
            "failed",
        )
        self.assertIsNone(
            stored["payload"]["messages"][0]["content"],
        )
        self.assertEqual(
            load_c2_ledger_entry(
                "conv-outbox-invalid",
                source_key,
            )["ingest_state"],
            "waiting",
        )

        api.post_wechat_messages_ingest = (  # type: ignore[method-assign]
            lambda _binding, payload: {
                "results": [
                    {
                        "source_message_key": payload["messages"][0][
                            "source_message_key"
                        ],
                        "dedupe_key": payload["messages"][0]["dedupe_key"],
                        "ingest_result": "ingested",
                    }
                ],
                "ignored_count": 0,
            }
        )
        with db_connection() as conn:
            conn.execute(
                """
                UPDATE c2_ingest_outbox
                SET next_attempt_at = NULL
                WHERE outbox_id = ?
                """,
                (outbox_id,),
            )
            conn.commit()

        self.assertTrue(runner._replay_c2_outbox(binding))
        self.assertEqual(
            load_c2_outbox_entry(outbox_id)["status"],
            "confirmed",
        )
        self.assertEqual(
            load_c2_ledger_entry(
                "conv-outbox-invalid",
                source_key,
            )["ingest_state"],
            "confirmed",
        )

    def test_backend_confirmed_target_terminal_stops_outbox_retry(self):
        api = FakeApi(None)

        def reject_unbound(_binding, _payload):
            raise ApiError(
                "MESSAGE_CONVERSATION_NOT_BOUND",
                "target removed",
                409,
                {
                    "recovery_action": "target_terminated",
                    "terminal_confirmed": True,
                },
            )

        api.post_wechat_messages_ingest = reject_unbound  # type: ignore[method-assign]
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        source_key = f"source-target-terminal-{time.time_ns()}"
        conversation_id = "conv-target-terminal"
        save_c2_ledger_terminal(
            conversation_id=conversation_id,
            source_message_key=source_key,
            dedupe_key=f"dedupe:{source_key}",
            message_type="text",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-target-terminal-{time.time_ns()}",
                "conversation_id": conversation_id,
                "authorization_revision": "revision-target-terminal",
                "messages": [
                    {
                        "source_message_key": source_key,
                        "dedupe_key": f"dedupe:{source_key}",
                    }
                ],
            }
        )

        self.assertTrue(runner._replay_c2_outbox(binding))
        self.assertEqual(
            load_c2_outbox_entry(outbox_id)["status"],
            "target_terminated",
        )
        self.assertEqual(
            load_c2_ledger_entry(
                conversation_id,
                source_key,
            )["ingest_state"],
            "not_required",
        )

    def test_validation_error_pauses_outbox_without_rejecting_ledger(self):
        api = FakeApi(None)

        def reject_invalid_schema(_binding, _payload):
            raise ApiError(
                "VALIDATION_ERROR",
                "request was not parsed",
                400,
                {
                    "recovery_action": "capability_paused",
                    "retryable": False,
                },
            )

        api.post_wechat_messages_ingest = reject_invalid_schema  # type: ignore[method-assign]
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        source_key = f"source-validation-paused-{time.time_ns()}"
        conversation_id = "conv-validation-paused"
        save_c2_ledger_terminal(
            conversation_id=conversation_id,
            source_message_key=source_key,
            dedupe_key=f"dedupe:{source_key}",
            message_type="text",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-validation-paused-{time.time_ns()}",
                "conversation_id": conversation_id,
                "authorization_revision": "revision-validation-paused",
                "messages": [
                    {
                        "source_message_key": source_key,
                        "dedupe_key": f"dedupe:{source_key}",
                    }
                ],
            }
        )

        self.assertFalse(runner._replay_c2_outbox(binding))
        self.assertEqual(
            load_c2_outbox_entry(outbox_id)["status"],
            "capability_paused",
        )
        self.assertEqual(
            load_c2_ledger_entry(
                conversation_id,
                source_key,
            )["ingest_state"],
            "waiting",
        )

    def test_c2_outbox_replay_confirms_exact_source_key_without_rerunning_rpa(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="ok", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        source_key = f"source-outbox-{time.time_ns()}"
        conversation_id = f"conv-outbox-{time.time_ns()}"
        save_c2_ledger_terminal(
            conversation_id=conversation_id,
            source_message_key=source_key,
            dedupe_key=f"dedupe:{source_key}",
            message_type="image",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed", "content_clean": "车辆外观照片"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-outbox-{time.time_ns()}",
                "conversation_id": conversation_id,
                "authorization_revision": "revision-current",
                "messages": [
                    {
                        "source_message_key": source_key,
                        "dedupe_key": f"dedupe:{source_key}",
                        "message_type": "image",
                    }
                ],
            }
        )

        assert runner._replay_c2_outbox(binding) is True

        assert load_c2_ledger_entry(conversation_id, source_key)["ingest_state"] == "confirmed"
        assert all(item["outbox_id"] != outbox_id for item in list_c2_outbox_waiting(limit=100))
        assert bridge.c2_operation_order == []

    def test_c2_image_terminal_result_is_cached_without_second_vision_call(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-{unique}",
            rpa_session_key="wx:rpa:v1:image",
            display_name="CJIMAGE01 客户",
            remark_code="CJIMAGE01",
            authorization_revision=f"revision-image-{unique}",
        )
        sidecar_payload = {
            "observations": [
                {
                    "schema_version": 3,
                    "observation_id": f"image-observation-{unique}",
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "image",
                    "voice_state": "not_voice",
                    "item_state": "discovered",
                    "image_physical_anchor": {
                        "sender_role": "customer",
                        "preceding_stable_message": f"before-{unique}",
                        "following_stable_message": f"after-{unique}",
                        "occurrence_index": 0,
                    },
                    "bubble_rect": [420, 180, 650, 320],
                    "source_message": {"id": f"image-message-{unique}", "type": "image"},
                }
            ]
        }
        completed = {
            "state": "completed",
            "action_phase": "confirmed",
            "reason": "vision_ready",
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "客户发来一张车辆外观图",
                "image_bytes": "must-not-persist",
                "provider_response_text": "must-not-persist",
            },
            "visual_bridge_input": {"summary": "车辆外观图"},
            "transaction": {"image_sha256": "a" * 64, "image_bytes": "must-not-persist"},
            "diagnostics": {
                "schema_version": 1,
                "trace_id": f"image-observation-{unique}",
                "total_duration_ms": 1250,
                "image_persisted": False,
                "events": [
                    {
                        "sequence": 1,
                        "stage": "context_right_click",
                        "status": "completed",
                        "offset_ms": 120,
                        "duration_ms": 35,
                        "point": [520, 250],
                        "image_persisted": False,
                    }
                ],
            },
        }

        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={"ready": True, "config": {"customer_image_understanding": {"enabled": True}}},
        ), patch("chejin_worker_client.omniauto_vision.process_image_slot", return_value=completed) as vision, patch(
            "chejin_worker_client.task_runner.append_log"
        ) as logger:
            first, first_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
            )
            second, second_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
            )

        assert vision.call_count == 1
        assert first_stats["completed"] == 1
        assert second_stats["cached"] == 1
        assert first["observations"][0]["content_clean"] == "客户发来一张车辆外观图"
        assert second["observations"][0]["content_clean"] == "客户发来一张车辆外观图"
        source_key = image_observation_source_key(target, sidecar_payload["observations"][0])
        persisted = load_c2_ledger_entry(target.conversation_id, source_key)
        assert persisted is not None
        persisted_json = json.dumps(persisted["result"], ensure_ascii=False)
        assert "image_bytes" not in persisted_json
        assert "provider_response_text" not in persisted_json
        assert "must-not-persist" not in persisted_json
        events = [call.args[1] for call in logger.call_args_list]
        assert "c2_image_slot_discovered" in events
        assert "c2_image_role_confirmed" in events
        assert "c2_image_authorization_checked" in events
        assert "c2_image_slot_started" in events
        assert "c2_image_stage" in events
        assert "c2_image_slot_finished" in events
        assert "c2_image_slot_terminalized" in events
        assert "c2_image_slot_cached" in events

    def test_action_journal_vertical_c2_image_reaches_ingest_and_ledger(self):
        api = FakeApi(None)
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-journal-image-{unique}",
            rpa_session_key="wx:rpa:v1:journal-image",
            display_name="CJIMAGE01 客户",
            remark_code="CJIMAGE01",
            row_fingerprint={"title_text": "CJIMAGE01 客户"},
            read_reason="waiting_user_reply",
            authorization_revision=f"revision-journal-image-{unique}",
        )
        api.read_targets = [target]
        observation = {
            "schema_version": 3,
            "observation_id": f"image-journal-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": {
                "sender_role": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "occurrence_index": 0,
            },
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {
                "id": f"image-message-{unique}",
                "type": "image",
                "sender_role": "customer",
            },
        }
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "authoritative_frame_source": "initial_read",
                "observations": [observation],
            },
            {
                "authoritative_frame_source": "final_read",
                "observations": [observation],
            },
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-journal-image",
            worker_token="token",
            client_instance_id="client-journal-image",
            run_status="running",
        )
        observed_phases: list[str] = []

        def vision_boundary(**kwargs):
            journal_path = Path(kwargs["action_journal_path"])
            source_key = str(kwargs["source_message_key"])
            observed_phases.append(action_journal_phase(journal_path))
            update_action_journal_item(
                journal_path,
                source_message_key=source_key,
                action_phase="trigger_attempted",
                business_state="clipboard_copy_confirmed",
            )
            observed_phases.append(action_journal_phase(journal_path))
            understanding = {
                "schema_version": 1,
                "vision_summary": "客户发来一张车辆外观图。",
            }
            update_action_journal_item(
                journal_path,
                source_message_key=source_key,
                action_phase="confirmed",
                business_state="completed",
                business_result_confirmed=True,
                terminal_payload={
                    "state": "completed",
                    "customer_image_understanding": understanding,
                    "visual_bridge_input": {
                        "summary": "车辆外观图"
                    },
                },
            )
            observed_phases.append(action_journal_phase(journal_path))
            return {
                "state": "completed",
                "action_phase": "confirmed",
                "business_state": "completed",
                "business_result_confirmed": True,
                "reason": "vision_ready",
                "customer_image_understanding": understanding,
                "visual_bridge_input": {"summary": "车辆外观图"},
                "transaction": {
                    "action_phase": "confirmed",
                    "image_sha256": "c" * 64,
                },
                "diagnostics": {
                    "schema_version": 1,
                    "trace_id": source_key,
                    "events": [],
                    "image_persisted": False,
                },
            }

        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={
                "ready": True,
                "config": {
                    "customer_image_understanding": {"enabled": True}
                },
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            side_effect=vision_boundary,
        ):
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            observed_phases,
            ["not_attempted", "trigger_attempted", "confirmed"],
        )
        self.assertEqual(len(api.message_payloads), 1)
        image_message = api.message_payloads[0]["messages"][0]
        self.assertEqual(image_message["message_type"], "image")
        self.assertEqual(image_message["item_state"], "completed")
        self.assertEqual(
            image_message["content"],
            "客户发来一张车辆外观图。",
        )
        ledger = load_c2_ledger_entry(
            target.conversation_id,
            image_message["source_message_key"],
        )
        self.assertEqual(ledger["terminal_state"], "completed")
        self.assertEqual(ledger["ingest_state"], "confirmed")
        self.assertEqual(
            list_c2_action_journal(target.conversation_id),
            [],
        )

    def test_c2_cancelled_vision_is_not_terminalized_in_local_ledger(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-cancel-{unique}",
            rpa_session_key="wx:rpa:v1:image-cancel",
            display_name="CJCANCEL01",
            remark_code="CJCANCEL01",
            authorization_revision=f"revision-image-cancel-{unique}",
        )
        observation = {
            "schema_version": 3,
            "observation_id": f"image-cancel-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": {
                "sender_role": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "occurrence_index": 0,
            },
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {"id": f"image-source-{unique}", "type": "image"},
        }
        sidecar_payload = {"observations": [observation]}

        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={
                "ready": True,
                "config": {"customer_image_understanding": {"enabled": True}},
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value={
                "state": "cancelled",
                "reason": "vision_cancelled",
                "diagnostics": {"events": [], "image_persisted": False},
            },
        ) as vision:
            _, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
                cancel_check=lambda: True,
            )

        self.assertEqual(stats["authorization_revoked"], 1)
        source_key = image_observation_source_key(target, observation)
        self.assertIsNone(load_c2_ledger_entry(target.conversation_id, source_key))
        self.assertIs(vision.call_args.kwargs["cancel_check"](), True)

    def test_c2_pre_action_window_failure_is_deferred_with_same_window_context(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-frame-{unique}",
            rpa_session_key="wx:rpa:v1:image-frame",
            display_name="CJFRAME01",
            remark_code="CJFRAME01",
            authorization_revision=f"revision-image-frame-{unique}",
        )
        observation = {
            "schema_version": 3,
            "observation_id": f"image-frame-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": {
                "sender_role": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "occurrence_index": 0,
            },
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {
                "id": f"image-source-{unique}",
                "type": "image",
            },
        }
        window_context = {
            "schema_version": 1,
            "hwnd": 31415,
            "pid": 2718,
            "class_name": "WeChatMainWndForPC",
            "source": "sidecar_selected_main_window",
        }
        sidecar_payload = {
            "window_context": window_context,
            "observations": [observation],
        }
        deferred = {
            "state": "failed",
            "reason": "capture_wechat_failed",
            "action_phase": "not_attempted",
            "diagnostics": {
                "events": [
                    {
                        "sequence": 1,
                        "stage": "frame_capture",
                        "status": "failed",
                        "reason": "capture_wechat_failed",
                        "capture_step": "window_capture",
                        "capture_mode": "wechat_window",
                    }
                ],
                "image_persisted": False,
            },
        }

        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={
                "ready": True,
                "config": {
                    "customer_image_understanding": {"enabled": True}
                },
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value=deferred,
        ) as vision:
            result, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
            )

        self.assertEqual(stats["capability_paused"], 1)
        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(
            result["vision_capability"]["reason"],
            "capture_wechat_failed",
        )
        self.assertEqual(
            result["observations"][0]["item_state"],
            "discovered",
        )
        source_key = image_observation_source_key(target, observation)
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, source_key)
        )
        self.assertEqual(
            vision.call_args.kwargs["window_context"],
            window_context,
        )

    def test_c2_image_moved_out_of_view_stays_deferred_without_ledger(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-moved-{unique}",
            rpa_session_key="wx:rpa:v1:image-moved",
            display_name="CJMOVED01",
            remark_code="CJMOVED01",
            authorization_revision=f"revision-image-moved-{unique}",
        )
        observation = {
            "schema_version": 3,
            "observation_id": f"image-moved-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": {
                "sender_role": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "bubble_visual_fingerprint": "dhash64:0123456789abcdef",
                "occurrence_index": 0,
            },
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {
                "id": f"image-source-{unique}",
                "type": "image",
            },
        }
        sidecar_payload = {"observations": [observation]}
        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={
                "ready": True,
                "config": {
                    "customer_image_understanding": {"enabled": True}
                },
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value={
                "state": "failed",
                "reason": "image_bubble_slot_not_reconfirmed",
                "action_phase": "not_attempted",
                "diagnostics": {"events": [], "image_persisted": False},
            },
        ) as vision:
            result, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
            )

        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["completed"], 0)
        self.assertEqual(
            result["observations"][0]["item_state"],
            "discovered",
        )
        source_key = image_observation_source_key(target, observation)
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, source_key)
        )
        vision.assert_called_once()

    def test_post_vision_refresh_processes_new_image_in_same_ui_lease(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-post-vision-new-image",
            rpa_session_key="",
            display_name="CJPOST01",
            remark_code="CJPOST01",
            authorization_revision="revision-post-vision",
        )
        new_image = {
            "observation_id": "new-image",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
        }
        refreshed_payload = {
            "ok": True,
            "observations": [new_image],
        }
        runner.bridge.get_messages = unittest.mock.Mock(
            side_effect=[
                dict(refreshed_payload),
                dict(refreshed_payload),
            ]
        )
        lease = unittest.mock.Mock()
        plans = [
            {
                "history_gap": False,
                "identity_errors": [],
                "new_image_source_keys": {"new-image-key"},
            },
            {
                "history_gap": False,
                "identity_errors": [],
                "new_image_source_keys": set(),
            },
        ]
        processed_payload = {
            **refreshed_payload,
            "observations": [
                {**new_image, "item_state": "completed"}
            ],
        }
        with patch(
            "chejin_worker_client.task_runner.sidecar_contract_error",
            return_value=None,
        ), patch(
            "chejin_worker_client.task_runner.load_c2_state",
            return_value=None,
        ), patch(
            "chejin_worker_client.task_runner.save_c2_state",
        ), patch(
            "chejin_worker_client.task_runner."
            "reconcile_v16104_identity_transition",
            side_effect=lambda _target, observations, _state: (
                observations,
                {"schema_version": 1},
                [],
            ),
        ), patch.object(
            runner,
            "_build_final_slot_incremental_plan",
            side_effect=plans,
        ), patch.object(
            runner,
            "_process_final_image_slots",
            side_effect=[
                (
                    processed_payload,
                    {
                        "discovered": 1,
                        "completed": 1,
                        "failed": 0,
                        "ignored": 0,
                        "cached": 0,
                        "authorization_revoked": 0,
                        "configuration_incomplete": 0,
                        "capability_paused": 0,
                        "deferred": 0,
                    },
                ),
                (
                    processed_payload,
                    {
                        "discovered": 1,
                        "completed": 1,
                        "failed": 0,
                        "ignored": 0,
                        "cached": 1,
                        "authorization_revoked": 0,
                        "configuration_incomplete": 0,
                        "capability_paused": 0,
                        "deferred": 0,
                    },
                ),
            ],
        ) as process_images:
            result = runner._converge_current_screen_after_images(
                binding=binding,
                target=target,
                target_label="CJPOST01",
                sidecar_payload={"ok": True, "observations": []},
                lease=lease,
                action_cancel_requested=lambda: False,
                enforce_read_targets=True,
                flow_outcomes=FlowOutcomeAccumulator(),
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(runner.bridge.get_messages.call_count, 2)
        self.assertEqual(process_images.call_count, 2)
        first_call = process_images.call_args_list[0].kwargs
        self.assertEqual(
            first_call["allowed_new_source_keys"],
            {"new-image-key"},
        )
        for read_call in runner.bridge.get_messages.call_args_list:
            self.assertEqual(
                read_call.kwargs["target_mode"],
                "current",
            )
            self.assertNotIn("max_scroll_steps", read_call.kwargs)
        self.assertGreaterEqual(lease.update_step.call_count, 3)

    def test_post_vision_refresh_finishes_new_voice_before_return(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-post-vision-new-voice",
            rpa_session_key="",
            display_name="CJPOST02",
            remark_code="CJPOST02",
            authorization_revision="revision-post-vision-voice",
        )
        voice = {
            "observation_id": "new-voice",
            "row_kind": "voice_bubble",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
        }
        refreshed_payload = {
            "ok": True,
            "observations": [voice],
        }
        transcribed_payload = {
            "ok": True,
            "observations": [
                {
                    **voice,
                    "voice_state": "transcribed",
                    "row_kind": "voice_transcript",
                }
            ],
        }
        runner.bridge.get_messages = unittest.mock.Mock(
            return_value=refreshed_payload
        )
        with patch(
            "chejin_worker_client.task_runner.sidecar_contract_error",
            return_value=None,
        ), patch(
            "chejin_worker_client.task_runner.load_c2_state",
            return_value=None,
        ), patch(
            "chejin_worker_client.task_runner.save_c2_state",
        ), patch(
            "chejin_worker_client.task_runner."
            "reconcile_v16104_identity_transition",
            side_effect=lambda _target, observations, _state: (
                observations,
                {"schema_version": 1},
                [],
            ),
        ), patch.object(
            runner,
            "_finish_new_visible_voices_in_current_chat",
            return_value={
                "ok": True,
                "payload": transcribed_payload,
                "failed_source_keys": [],
                "failed_roles": {},
            },
        ) as finish_voice, patch.object(
            runner,
            "_build_final_slot_incremental_plan",
            return_value={
                "history_gap": False,
                "identity_errors": [],
                "new_image_source_keys": set(),
            },
        ):
            result = runner._converge_current_screen_after_images(
                binding=binding,
                target=target,
                target_label="CJPOST02",
                sidecar_payload={"ok": True, "observations": []},
                lease=unittest.mock.Mock(),
                action_cancel_requested=lambda: False,
                enforce_read_targets=True,
                flow_outcomes=FlowOutcomeAccumulator(),
            )

        self.assertTrue(result["ok"], result)
        finish_voice.assert_called_once()
        self.assertEqual(
            result["payload"]["observations"][0]["voice_state"],
            "transcribed",
        )

    def test_post_vision_identity_ambiguity_blocks_media_and_ingest(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-post-vision-ambiguous",
            rpa_session_key="",
            display_name="CJPOST03",
            remark_code="CJPOST03",
            authorization_revision="revision-post-vision-ambiguous",
        )
        runner.bridge.get_messages = unittest.mock.Mock(
            return_value={
                "ok": True,
                "observations": [
                    {
                        "observation_id": "ambiguous-image",
                        "row_kind": "image_bubble",
                    }
                ],
            }
        )
        with patch(
            "chejin_worker_client.task_runner.sidecar_contract_error",
            return_value=None,
        ), patch(
            "chejin_worker_client.task_runner.load_c2_state",
            return_value=None,
        ), patch(
            "chejin_worker_client.task_runner."
            "reconcile_v16104_identity_transition",
            return_value=(
                [],
                {"schema_version": 1},
                [
                    {
                        "error_code": (
                            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                        )
                    }
                ],
            ),
        ), patch.object(
            runner,
            "_process_final_image_slots",
        ) as process_images:
            result = runner._converge_current_screen_after_images(
                binding=binding,
                target=target,
                target_label="CJPOST03",
                sidecar_payload={"ok": True, "observations": []},
                lease=unittest.mock.Mock(),
                action_cancel_requested=lambda: False,
                enforce_read_targets=True,
                flow_outcomes=FlowOutcomeAccumulator(),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        )
        process_images.assert_not_called()

    def test_confirmed_message_filter_keeps_full_slot_order_but_removes_old_observation(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        old_observation = {
            "observation_id": "observation-old-trigger",
            "row_kind": "text_bubble",
        }
        new_observation = {
            "observation_id": "observation-new-sales",
            "row_kind": "text_bubble",
        }
        payload = {
            "conversation_id": "conv-filter-observation",
            "messages": [
                {
                    "source_message_key": "old-trigger",
                    "raw_payload": {"observation": old_observation},
                },
                {
                    "source_message_key": "new-sales",
                    "raw_payload": {"observation": new_observation},
                },
            ],
            "evidence": {
                "observations": [old_observation, new_observation],
                "slot_ledger_states": [
                    {
                        "source_message_key": "old-trigger",
                        "screen_order": 1,
                        "ledger_state": "OLD_COMPLETED",
                    },
                    {
                        "source_message_key": "new-sales",
                        "screen_order": 2,
                        "ledger_state": "NEW_MESSAGE",
                    },
                ],
            },
        }

        with patch(
            "chejin_worker_client.task_runner.load_c2_ledger_entry",
            side_effect=lambda _conversation_id, source_key: (
                {"ingest_state": "confirmed"}
                if source_key == "old-trigger"
                else None
            ),
        ):
            filtered = runner._filter_confirmed_messages(payload)

        self.assertEqual(
            [item["source_message_key"] for item in filtered["messages"]],
            ["new-sales"],
        )
        self.assertEqual(
            [
                item["observation_id"]
                for item in filtered["evidence"]["observations"]
            ],
            ["observation-new-sales"],
        )
        self.assertEqual(
            [
                item["source_message_key"]
                for item in filtered["evidence"]["slot_ledger_states"]
            ],
            ["old-trigger", "new-sales"],
        )

    def test_c2_history_gap_blocks_image_ui_action_before_vision(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="ok", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-history-gap-{unique}",
            rpa_session_key="wx:rpa:v1:history-gap",
            display_name="CJGAP01 客户",
            remark_code="CJGAP01",
            authorization_revision=f"revision-history-gap-{unique}",
        )
        payload = bridge._contractual_message_payload(
            {
                "ok": True,
                "messages": [
                    {
                        "id": f"old-text-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "已处理的旧消息",
                        "bubble_rect": [420, 300, 650, 340],
                    }
                ],
            }
        )
        payload["observations"].insert(
            0,
            {
                "schema_version": 3,
                "observation_id": f"new-image-{unique}",
                "row_kind": "image_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "image",
                "voice_state": "not_voice",
                "item_state": "discovered",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "preceding_stable_message": f"new-before-{unique}",
                    "following_stable_message": f"new-after-{unique}",
                    "occurrence_index": 0,
                },
                "bubble_rect": [420, 120, 650, 260],
                "source_message": {"id": f"new-image-source-{unique}", "type": "image"},
            },
        )
        payload["observations"][1]["bubble_rect"] = [420, 300, 650, 340]
        first_plan = runner._build_final_slot_incremental_plan(target=target, sidecar_payload=payload)
        old_text_slot = next(item for item in first_plan["slot_ledger_states"] if item["row_kind"] == "text_bubble")
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=old_text_slot["source_message_key"],
            dedupe_key=f"dedupe:{old_text_slot['source_message_key']}",
            message_type="text",
            terminal_state="completed",
            ingest_state="confirmed",
            result={},
        )

        plan = runner._build_final_slot_incremental_plan(target=target, sidecar_payload=payload)
        with patch("chejin_worker_client.omniauto_vision.process_image_slot") as vision:
            _, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=payload,
                enforce_read_targets=False,
                allowed_new_source_keys=set() if plan["history_gap"] else plan["new_image_source_keys"],
                incremental_gate_reason="C2_MESSAGE_HISTORY_GAP" if plan["history_gap"] else "",
            )

        self.assertTrue(plan["history_gap"])
        self.assertEqual([item["ledger_state"] for item in plan["slot_ledger_states"]], ["NEW_MESSAGE", "OLD_COMPLETED"])
        self.assertTrue(
            all(
                item["order_source"] == "visual_top"
                for item in plan["slot_ledger_states"]
            )
        )
        self.assertEqual(
            plan["flow_gate_details"][0]["position_source"],
            "slot_ledger_visual_top",
        )
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["deferred"], 1)
        image_source_key = next(item["source_message_key"] for item in plan["slot_ledger_states"] if item["row_kind"] == "image_bubble")
        self.assertIsNone(load_c2_ledger_entry(target.conversation_id, image_source_key))
        deferred_image = next(
            item
            for item in payload["observations"]
            if item.get("observation_id") == f"new-image-{unique}"
        )
        self.assertEqual(deferred_image["item_state"], "discovered")
        vision.assert_not_called()

    def test_c2_missing_vision_configuration_pauses_without_terminalizing(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")))
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-config-{unique}",
            rpa_session_key="",
            display_name="CJIMAGE02",
            remark_code="CJIMAGE02",
            authorization_revision=f"revision-image-config-{unique}",
        )
        sidecar_payload = {
            "observations": [
                {
                    "schema_version": 3,
                    "observation_id": f"image-config-{unique}",
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "image",
                    "voice_state": "not_voice",
                    "item_state": "discovered",
                    "image_physical_anchor": {
                        "sender_role": "customer",
                        "preceding_stable_message": f"config-before-{unique}",
                        "following_stable_message": f"config-after-{unique}",
                        "occurrence_index": 0,
                    },
                    "bubble_rect": [420, 180, 650, 320],
                    "source_message": {"id": f"image-config-source-{unique}", "type": "image"},
                }
            ]
        }
        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={
                "ready": False,
                "missing_configuration": ["CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"],
                "provider": "anthropic_compatible",
                "base_url": "https://example.invalid/v1",
                "model": "unit-model",
                "request_style": "anthropic_messages_vision",
            },
        ), patch("chejin_worker_client.omniauto_vision.process_image_slot") as vision:
            _, first_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
            )
            _, second_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
            )

        assert vision.call_count == 0
        assert first_stats["configuration_incomplete"] == 1
        assert first_stats["capability_paused"] == 1
        assert first_stats["failed"] == 0
        assert second_stats["configuration_incomplete"] == 1
        assert second_stats["capability_paused"] == 1
        assert second_stats["cached"] == 0
        assert second_stats["failed"] == 0
        assert first_stats["cached"] == 0
        source_key = image_observation_source_key(target, sidecar_payload["observations"][0])
        ledger = load_c2_ledger_entry(target.conversation_id, source_key)
        assert ledger is None

    def test_c2_vision_pause_does_not_freeze_text_and_voice_conversation_read(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused"),
            message_sender_role="self",
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.binding = binding
        target = WechatReadTarget(
            conversation_id="conv-image-paused",
            rpa_session_key="wx:rpa:v1:image-paused",
            display_name="CJIMAGE03",
            remark_code="CJIMAGE03",
            authorization_revision="revision-image-paused",
        )
        save_c2_state(
            f"vision_capability:{target.conversation_id}",
            {
                "state": "capability_paused",
                "reason": "vision_configuration_incomplete",
            },
        )

        result = runner._read_one_wechat_target(binding, target)

        self.assertTrue(result["ok"])
        self.assertIn("locate_chat", bridge.c2_operation_order)
        self.assertIn("messages", bridge.c2_operation_order)
        self.assertEqual(len(api.message_payloads), 1)

    def test_c2_missing_vision_configuration_keeps_text_and_defers_image(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused"),
        )
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "text-with-paused-image",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "item_state": "completed",
                        "content_clean": "图片旁边的文字仍要入库",
                        "bubble_rect": [420, 120, 680, 160],
                        "source_message": {
                            "id": "text-with-paused-image",
                            "type": "text",
                            "content": "图片旁边的文字仍要入库",
                        },
                    },
                    {
                        "schema_version": 3,
                        "observation_id": "image-without-config",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                        "item_state": "discovered",
                        "bubble_rect": [420, 180, 680, 360],
                        "image_physical_anchor": {
                            "sender_role": "customer",
                            "preceding_stable_message": "text-with-paused-image",
                            "following_stable_message": "",
                            "occurrence_index": 0,
                        },
                        "source_message": {
                            "id": "image-without-config",
                            "type": "image",
                        },
                    },
                ],
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.binding = binding
        target = WechatReadTarget(
            conversation_id="conv-image-config-mixed",
            rpa_session_key="",
            display_name="CJIMAGE04",
            remark_code="CJIMAGE04",
            authorization_revision="revision-image-config-mixed",
        )

        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={
                "ready": False,
                "missing_configuration": [
                    "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"
                ],
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot"
        ) as vision:
            result = runner._read_one_wechat_target(binding, target)

        self.assertTrue(result["ok"])
        vision.assert_not_called()
        self.assertEqual(len(api.message_payloads), 1)
        payload = api.message_payloads[0]
        self.assertEqual(
            [item["content"] for item in payload["messages"]],
            ["图片旁边的文字仍要入库"],
        )
        self.assertIn(
            "C2_VISION_CAPABILITY_PAUSED",
            payload["evidence"]["flow_gate_errors"],
        )
        image = next(
            item
            for item in payload["evidence"]["observations"]
            if item.get("row_kind") == "image_bubble"
        )
        image_source_key = image_observation_source_key(target, image)
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, image_source_key)
        )

    def test_ai_reply_receipt_attaches_only_to_matching_stable_self_bubble(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id="conv-ai-receipt",
            rpa_session_key="",
            display_name="CJAI01",
            remark_code="CJAI01",
            authorization_revision="revision-ai-receipt",
        )
        text = "好的，我帮您确认一下"
        save_c2_state(
            f"ai_reply_receipts:{target.conversation_id}",
            {
                "version": 1,
                "receipts": [
                    {
                        "reply_action_id": "reply-action-ai",
                        "reply_text_hash": runner._reply_text_hash(text),
                        "worker_stable_id": "worker-message-ai",
                        "confirmed_at": "2020-01-01T00:00:00+00:00",
                    }
                ],
            },
        )
        observations = [
            {
                "observation_id": "self-ai",
                "_worker_stable_id": "worker-message-ai",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "content_clean": text,
            },
            {
                "observation_id": "self-human-same-text",
                "_worker_stable_id": "worker-message-human",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "content_clean": text,
            },
        ]

        enriched = runner._attach_confirmed_ai_reply_receipts(
            target=target,
            observations=observations,
        )

        self.assertEqual(
            enriched[0]["_worker_ai_reply_receipt"]["reply_action_id"],
            "reply-action-ai",
        )
        self.assertNotIn("_worker_ai_reply_receipt", enriched[1])

    def test_possible_ai_send_marks_matching_new_bubble_as_unreconciled(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id="conv-ai-unreconciled",
            rpa_session_key="",
            display_name="CJAI02",
            remark_code="CJAI02",
            authorization_revision="revision-ai-unreconciled",
        )
        text = "微信可能已经发出这条回复"
        save_c2_state(
            f"possible_ai_sends:{target.conversation_id}",
            {
                "version": 1,
                "sends": [
                    {
                        "reply_action_id": "reply-action-unknown",
                        "reply_text_hash": runner._reply_text_hash(text),
                        "identity_sequence_floor": 7,
                        "armed_at": "2026-07-24T12:00:00+00:00",
                        "reconciliation_state": "ai_unreconciled",
                    }
                ],
            },
        )

        enriched = runner._attach_possible_ai_send_receipts(
            target=target,
            observations=[
                {
                    "observation_id": "old-same-text",
                    "_worker_stable_id": "worker-message-6",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_clean": text,
                },
                {
                    "observation_id": "new-same-text",
                    "_worker_stable_id": "worker-message-7",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_clean": text,
                },
                {
                    "observation_id": "later-human-same-text",
                    "_worker_stable_id": "worker-message-8",
                    "row_kind": "text_bubble",
                    "sender_role": "self",
                    "content_clean": text,
                },
            ],
        )

        self.assertNotIn("_worker_ai_reply_receipt", enriched[0])
        self.assertEqual(
            enriched[1]["_worker_ai_reply_receipt"][
                "reconciliation_state"
            ],
            "ai_unreconciled",
        )
        self.assertEqual(
            enriched[1]["_worker_ai_reply_receipt"]["reply_action_id"],
            "reply-action-unknown",
        )
        self.assertNotIn("_worker_ai_reply_receipt", enriched[2])

    def test_ai_reply_receipt_is_consumed_only_after_backend_confirms_bubble(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        conversation_id = "conv-ai-receipt-consume"
        state_key = f"ai_reply_receipts:{conversation_id}"
        receipt = {
            "reply_action_id": "reply-action-consume",
            "reply_text_hash": "hash-consume",
            "worker_stable_id": "worker-message-consume",
            "confirmed_at": "2020-01-01T00:00:00+00:00",
        }
        save_c2_state(state_key, {"version": 1, "receipts": [receipt]})
        possible_state_key = f"possible_ai_sends:{conversation_id}"
        save_c2_state(
            possible_state_key,
            {
                "version": 1,
                "sends": [
                    {
                        "reply_action_id": "reply-action-consume",
                        "reply_text_hash": "hash-consume",
                    }
                ],
            },
        )
        payload = {
            "conversation_id": conversation_id,
            "messages": [
                {
                    "source_message_key": "source-ai-consume",
                    "raw_payload": {"ai_reply_receipt": receipt},
                }
            ],
        }

        runner._consume_confirmed_ai_reply_receipts(
            payload=payload,
            result={"results": []},
        )
        self.assertEqual(load_c2_state(state_key)["receipts"], [receipt])

        runner._consume_confirmed_ai_reply_receipts(
            payload=payload,
            result={
                "results": [
                    {
                        "source_message_key": "source-ai-consume",
                        "ingest_result": "ingested",
                    }
                ]
            },
        )
        self.assertEqual(load_c2_state(state_key)["receipts"], [])
        self.assertEqual(
            load_c2_state(possible_state_key)["sends"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
