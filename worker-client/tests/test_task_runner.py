from __future__ import annotations

import ast
import inspect
import json
import hashlib
import os
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.api import ApiError
from chejin_worker_client.action_journal import (
    action_journal_path,
    action_journal_phase,
    initialize_action_journal,
    list_action_journals,
    read_action_journal,
    remove_action_journal,
    update_action_journal_item,
)
from chejin_worker_client.c2_contract import contract_revision, contract_sha256
from chejin_worker_client.models import Binding, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from chejin_worker_client.incident_evidence import wait_for_incident
from chejin_worker_client.rpa_bridge import RpaBridge
from chejin_worker_client.storage import (
    checkpoint_c2_action_outcomes,
    db_connection,
    enqueue_c2_outbox,
    list_c2_action_journal,
    list_c2_ledger_entries,
    list_c2_outbox_waiting,
    load_c2_state,
    load_c2_ledger_entry,
    load_c2_outbox_entry,
    load_reply_send_ack_outbox,
    read_logs,
    refresh_c2_outbox_payload,
    save_c2_state,
    save_c2_ledger_terminal as _save_c2_ledger_terminal,
    save_reply_send_intent,
)
from chejin_worker_client.task_runner import (
    C2_RECENT_VISIBLE_CACHE_TTL_SECONDS,
    TaskLeaseGuard,
    TaskRunner,
    coalesce_physical_voice_observations,
    _freeze_phase_metadata,
)
from chejin_worker_client.transaction_outcomes import (
    FlowOutcomeAccumulator,
    merge_item_outcomes,
)
from chejin_worker_client.ui_lock import LOCK_FILE, UiLockError
from chejin_worker_client.wechat_c2 import (
    build_message_ingest_payload,
    image_observation_source_key,
    project_final_slot_flow_gates,
    reconcile_v16104_identity_transition,
    voice_observation_source_key,
)


def save_c2_ledger_terminal(**kwargs):
    """Seed legacy-independent ledger fixtures with explicit fact ownership."""

    kwargs.setdefault(
        "origin_read_run_id",
        f"read-fixture:{kwargs.get('conversation_id') or 'unknown'}",
    )
    return _save_c2_ledger_terminal(**kwargs)


def sidecar_identity_contract(
    code: str = "",
    *,
    conversation_type: str = "unknown",
    allowed: bool = False,
    reason: str = "test_sidecar_identity",
) -> dict:
    normalized_code = str(code or "").strip().upper()
    return {
        "c2_remark_code_candidates": [normalized_code] if allowed else [],
        "c2_conversation_admission": {
            "conversation_type": conversation_type,
            "admission_allowed": allowed,
            "remark_code": normalized_code,
            "reason": reason,
        },
    }


def identity_checkpoint(
    *, next_sequence_floor: int = 1,
) -> dict:
    return {
        "version": 2,
        "next_sequence_floor": next_sequence_floor,
        "recent_messages": [],
    }


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
        self.settlement_tokens: list[str | None] = []
        self.read_targets: list[WechatReadTarget] = []
        self.message_ingest_result = "ingested"
        self.friend_activation_payloads: list[dict] = []
        self.message_batch_statuses: list[dict] = []
        self.message_batch_result: dict | None = None
        self.heartbeat_payloads: list[dict] = []
        self.heartbeat_run_status: str | None = None
        self.message_ingest_error: Exception | None = None
        self.read_authorization_overrides: dict[str, dict] = {}
        self.claim_reply_text = "您好，可以继续沟通这台车。"
        self.claim_reply_hash = hashlib.sha256(self.claim_reply_text.encode("utf-8")).hexdigest()

    def heartbeat(self, binding: Binding, **kwargs):
        self.heartbeat_payloads.append(dict(kwargs))
        self.events.append(f"heartbeat:{kwargs['rpa_component_status']}:{kwargs['wechat_status']}")
        return WorkerProfile(
            id=binding.worker_id,
            worker_name="测试 Worker",
            run_status=self.heartbeat_run_status or binding.run_status,
        )

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
            if isinstance(target.raw, dict):
                target.raw.setdefault(
                    "identity_checkpoint",
                    {
                        "version": 2,
                        "next_sequence_floor": 1,
                        "recent_messages": [],
                    },
                )
        return targets

    def get_wechat_read_authorization(
        self,
        binding: Binding,
        conversation_id: str,
        *,
        continuation_batch_id: str | None = None,
        continuation_token: str | None = None,
        recovery_transaction_id: str | None = None,
        action_kind: str | None = None,
        source_message_key_digest: str | None = None,
        original_authorization_revision: str | None = None,
    ):
        self.events.append(f"read_authorization:{conversation_id}")
        if conversation_id in self.read_authorization_overrides:
            return dict(
                self.read_authorization_overrides[conversation_id]
            )
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
                "recovery_decision": "target_terminated",
                "conversation_id": conversation_id,
                "authorization_revision": "",
                "read_reason": "",
            }
        if not target.authorization_revision:
            target.authorization_revision = f"revision-{target.conversation_id}"
        if recovery_transaction_id:
            return {
                "allowed": False,
                "recovery_decision": "settle_without_ui",
                "settlement_mode": "fact_only",
                "settlement_token": "test-settlement-token",
                "conversation_id": target.conversation_id,
                "authorization_revision": (
                    original_authorization_revision
                    or target.authorization_revision
                ),
                "read_reason": "fact_settlement",
                "target": {
                    "conversation_id": target.conversation_id,
                    "rpa_session_key": target.rpa_session_key,
                    "display_name": target.display_name,
                    "remark_code": target.remark_code,
                    "lead_id": target.lead_id,
                    "sales_id": target.sales_id,
                    "read_reason": "fact_settlement",
                    "authorization_revision": (
                        original_authorization_revision
                        or target.authorization_revision
                    ),
                },
            }
        return {
            "allowed": True,
            "recovery_decision": "allowed",
            "conversation_id": target.conversation_id,
            "authorization_revision": target.authorization_revision,
            "read_reason": target.read_reason,
            "identity_checkpoint": (
                target.raw.get("identity_checkpoint")
                if isinstance(target.raw, dict)
                and isinstance(
                    target.raw.get("identity_checkpoint"), dict
                )
                else {
                    "version": 2,
                    "next_sequence_floor": 1,
                    "recent_messages": [],
                }
            ),
            "next_read_due_at": (
                target.raw.get("next_read_due_at")
                if isinstance(target.raw, dict)
                else None
            ),
            "target": {
                "conversation_id": target.conversation_id,
                "rpa_session_key": target.rpa_session_key,
                "display_name": target.display_name,
                "remark_code": target.remark_code,
                "row_fingerprint": target.row_fingerprint,
                "ocr_confidence": target.ocr_confidence,
                "lead_id": target.lead_id,
                "sales_id": target.sales_id,
                "read_reason": target.read_reason,
                "authorization_revision": target.authorization_revision,
            },
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

    def post_wechat_messages_ingest(
        self,
        binding: Binding,
        payload: dict,
        *,
        settlement_token: str | None = None,
    ):
        if self.message_ingest_error is not None:
            raise self.message_ingest_error
        self.settlement_tokens.append(settlement_token)
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
                    **sidecar_identity_contract(
                        "CJTEST01",
                        conversation_type="private",
                        allowed=True,
                    ),
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
    @staticmethod
    def _identity_text_observation(
        observation_id: str,
        content: str,
        top: int,
    ) -> dict:
        return {
            "schema_version": 3,
            "observation_id": observation_id,
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "text",
            "voice_state": "not_voice",
            "content_clean": content,
            "bubble_rect": [420, top, 650, top + 56],
            "source_message": {
                "id": observation_id,
                "type": "text",
                "sender_role": "customer",
            },
        }

    def test_phase_metadata_is_frozen_before_later_image_merges(self):
        source_key = "source:image-1"
        mutable = {
            "completed": 1,
            "completed_source_keys": [source_key],
            "terminal_source_keys": [source_key],
            "cached_source_keys": [],
        }

        frozen = _freeze_phase_metadata(mutable)
        mutable["completed_source_keys"].append(source_key)
        mutable["terminal_source_keys"].append(source_key)
        mutable["cached_source_keys"].append(source_key)

        self.assertEqual(frozen["completed_source_keys"], [source_key])
        self.assertEqual(frozen["terminal_source_keys"], [source_key])
        self.assertEqual(frozen["cached_source_keys"], [])

    def test_physical_voice_aliases_merge_before_journal_creation(self):
        target = WechatReadTarget(
            conversation_id="conv-one-physical-voice",
            rpa_session_key="wx:rpa:v1:one-physical-voice",
            display_name="CJT9V5X1",
            remark_code="CJT9V5X1",
            authorization_revision="revision-one-physical-voice",
        )
        observations = [
            {
                "sender_role": "customer",
                "voice_anchor_structural_key": "voice-structural:a",
                "parent_voice_anchor_key": "voice-parent:shared",
            },
            {
                "sender_role": "customer",
                "voice_anchor_structural_key": "voice-structural:b",
                "parent_voice_anchor_key": "voice-parent:shared",
            },
        ]

        groups = coalesce_physical_voice_observations(
            target,
            observations,
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["source_message_keys"]), 2)
        self.assertEqual(
            groups[0]["physical_anchor_keys"],
            [
                "voice-parent:shared",
                "voice-structural:a",
                "voice-structural:b",
            ],
        )
        self.assertEqual(groups[0]["role"], "customer")

    def test_distinct_physical_voices_remain_distinct(self):
        target = WechatReadTarget(
            conversation_id="conv-two-physical-voices",
            rpa_session_key="wx:rpa:v1:two-physical-voices",
            display_name="CJT9V5X1",
            remark_code="CJT9V5X1",
            authorization_revision="revision-two-physical-voices",
        )
        observations = [
            {
                "sender_role": "customer",
                "voice_anchor_structural_key": "voice-structural:a",
            },
            {
                "sender_role": "customer",
                "voice_anchor_structural_key": "voice-structural:b",
            },
        ]

        groups = coalesce_physical_voice_observations(
            target,
            observations,
        )

        self.assertEqual(len(groups), 2)

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

    def setUp(self):
        from chejin_worker_client.emergency_stop import reset_emergency_stop_for_tests

        reset_emergency_stop_for_tests()
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        for path, _payload in list_action_journals():
            remove_action_journal(path)
        with db_connection() as conn:
            conn.execute("DELETE FROM c2_action_journal")
            conn.execute("DELETE FROM c2_message_ledger")
            conn.execute("DELETE FROM c2_ingest_outbox")
            conn.execute("DELETE FROM reply_send_ack_outbox")
            conn.execute("DELETE FROM c2_runtime_state")
            conn.commit()

    def tearDown(self):
        for path, _payload in list_action_journals():
            remove_action_journal(path)

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
            origin_read_run_id="read-crashed-flow",
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
            origin_read_run_id="read-voice-crashed-after-click",
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
            ledger["origin_read_run_id"],
            "read-voice-crashed-after-click",
        )
        self.assertEqual(
            ledger["result"]["action_outcome"]["action_phase"],
            "trigger_attempted",
        )
        self.assertEqual(ledger["ingest_state"], "waiting")
        self.assertTrue(path.exists())
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key="voice-triggered-before-crash",
            origin_read_run_id="read-voice-crashed-after-click",
            dedupe_key=None,
            message_type="voice",
            terminal_state="failed",
            ingest_state="confirmed",
            result=ledger["result"],
        )
        runner._recover_physical_action_journals(target)
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
            origin_read_run_id="read-image-crashed-before-copy",
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
        self.assertEqual(ledger["ingest_state"], "waiting")
        self.assertTrue(created_paths[0].exists())
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key="voice-sidecar-crashed",
            origin_read_run_id=ledger["origin_read_run_id"],
            dedupe_key=None,
            message_type="voice",
            terminal_state="failed",
            ingest_state="confirmed",
            result=ledger["result"],
        )
        runner._recover_physical_action_journals(target)
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
            origin_read_run_id="read-test-accumulator",
            dedupe_key=None,
            message_type="voice",
            terminal_state="failed",
            ingest_state="waiting",
            result={"state": "failed", "error_code": "VOICE_FAILED"},
        )
        accumulator = FlowOutcomeAccumulator(
            origin_read_run_id="read-test-accumulator"
        )
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
        # read-targets in production always carry the backend identity
        # checkpoint. Tests that invoke the single-target flow directly must
        # model that same contract instead of relying on local state leaked
        # from another test.
        for target in api.read_targets:
            if isinstance(target.raw, dict):
                target.raw.setdefault(
                    "identity_checkpoint",
                    {
                        "version": 2,
                        "next_sequence_floor": 1,
                        "recent_messages": [],
                    },
                )
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
        runner.last_c2_vision_preflight_at = time.monotonic()
        runner.c2_vision_preflight_ready = True
        production_reconcile = runner._reconcile_message_identities

        def reconcile_with_contract_fixture(
            target,
            observations,
            **kwargs,
        ):
            # Direct unit tests bypass FakeApi.read-targets. Supply the field
            # that the current backend C2 contract always returns in production.
            if isinstance(target.raw, dict):
                target.raw.setdefault(
                    "identity_checkpoint",
                    {
                        "version": 1,
                        "next_sequence_floor": 1,
                        "recent_messages": [],
                    },
                )
            return production_reconcile(
                target,
                observations,
                **kwargs,
            )

        runner._reconcile_message_identities = (  # type: ignore[method-assign]
            reconcile_with_contract_fixture
        )
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

    def test_tick_once_does_not_pull_task_while_image_fact_waits_for_outbox(
        self,
    ):
        task = Task(
            id="task-add-friend-image-ledger-barrier",
            task_type="add_friend",
            status="pending",
            phone="17368746889",
        )
        api = FakeApi(task)
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
            worker_id="worker-image-ledger-barrier",
            worker_token="token",
            client_instance_id="client-image-ledger-barrier",
            run_status="running",
        )
        runner.binding = binding
        unique = str(time.time_ns())
        conversation_id = f"conv-image-ledger-barrier-{unique}"
        source_key = f"source:image-ledger-barrier-{unique}"
        save_c2_ledger_terminal(
            conversation_id=conversation_id,
            source_message_key=source_key,
            dedupe_key=None,
            message_type="image",
            terminal_state="completed",
            ingest_state="waiting",
            result={
                "state": "completed",
                "replayable_observation": {
                    "schema_version": 3,
                    "observation_id": f"image-ledger-{unique}",
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "image",
                    "voice_state": "not_voice",
                    "item_state": "completed",
                    "content_clean": "等待上报的图片事实",
                    "source_message": {
                        "type": "image",
                        "sender_role": "customer",
                        "source_message_key": source_key,
                    },
                },
            },
        )
        try:
            runner.tick_once()

            self.assertNotIn("pull", api.events)
            self.assertFalse(
                any(event.startswith("claim:") for event in api.events)
            )
            ledger = load_c2_ledger_entry(
                conversation_id,
                source_key,
            )
            self.assertEqual(ledger["ingest_state"], "waiting")
        finally:
            with db_connection() as conn:
                conn.execute(
                    """
                    DELETE FROM c2_message_ledger
                    WHERE conversation_id = ?
                    """,
                    (conversation_id,),
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

    def test_backend_pause_returned_by_heartbeat_stops_task_pull_immediately(self):
        task = Task(id="task-server-paused", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        api.heartbeat_run_status = "paused"
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.binding = binding

        runner.tick_once()

        self.assertEqual(binding.run_status, "paused")
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

    def test_heartbeat_reports_persisted_global_vision_preflight(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(api, FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")))
        runner.binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="paused")
        save_c2_state(
            "vision_preflight",
            {
                "state": "vision_not_ready",
                "reason": "vision_configuration_incomplete",
                "missing_configuration": ["CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"],
            },
        )

        runner.tick_once()

        vision = api.heartbeat_payloads[-1]["local_lock_summary"]["capabilities"]["vision"]
        self.assertEqual(vision["state"], "vision_not_ready")
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
        incident_logs = [
            row
            for row in read_logs(limit=50)
            if row.get("event") == "send_result_unknown"
            and (row.get("metadata") or {}).get("sidecar_run_id")
            == "send-unconfirmed"
        ]
        self.assertEqual(len(incident_logs), 1)
        incident_path = wait_for_incident(
            incident_logs[0]["metadata"]["incident_id"],
            timeout=10.0,
        )
        self.assertIsNotNone(incident_path)
        assert incident_path is not None
        self.assertTrue(
            incident_path.is_file()
        )
        self.assertTrue(
            incident_logs[0]["metadata"]["incident_id"].startswith("INC-")
        )
        with zipfile.ZipFile(incident_path) as archive:
            outbox = json.loads(archive.read("state/outbox.json"))
        self.assertEqual(
            outbox["related_sent_ack"]["ack_payload"]["send_result"],
            "unknown",
        )
        self.assertEqual(
            outbox["related_sent_ack"]["action_phase"],
            "trigger_attempted",
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

    def test_chat_reply_recovery_obeys_global_vision_preflight_before_ui(self):
        task = self.make_chat_reply_task(
            task_id="task-chat-vision-not-ready",
            status="running",
        )
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        bridge = FakeBridge(
            RpaResult(
                ok=True,
                result_code="invite_sent",
                message="unused",
            )
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
            "_c2_vision_ready_before_scan",
            return_value=False,
        ):
            runner.tick_once()

        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(bridge.sent_replies, [])
        self.assertFalse(
            any(event.startswith("sent_ack:") for event in api.events)
        )
        self.assertFalse(
            any(event.startswith("fail:") for event in api.events)
        )

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
        ) as refresh:
            runner.tick_once()

        self.assertIn(
            "fail:C2_REPLY_CONTEXT_RECOVERY_FAILED:pre_send_refresh",
            api.events,
        )
        self.assertEqual(
            refresh.call_args.kwargs["operation_phase"],
            "pre_send_refresh",
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
        assert kwargs["operation_phase"] == "pre_send_refresh"

    def test_c3_pre_send_refresh_failure_settles_pending_reply_before_return(self):
        task = self.make_chat_reply_task(task_id="task-pre-send-failed")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
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

        with patch.object(
            runner,
            "_read_one_wechat_target",
            return_value={
                "ok": False,
                "error_code": "C2_FRIEND_ACTIVATION_STATE_INVALID",
            },
        ):
            result = runner._wait_and_send_current_c3_batch(
                binding=binding,
                target=api.read_targets[0],
                batch_id="batch-1",
                cancel_check=lambda: False,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["reply_task_settled"])
        self.assertEqual(binding.run_status, "running")
        self.assertNotIn("paused", api.run_status_updates)
        self.assertEqual(
            api.events.count(
                "fail:C2_REPLY_CONTEXT_RECOVERY_FAILED:pre_send_refresh"
            ),
            1,
        )
        self.assertFalse(any(event.startswith("claim:") for event in api.events))

    def test_unconfirmed_pre_send_failure_settlement_pauses_worker(self):
        task = self.make_chat_reply_task(task_id="task-settlement-unconfirmed")
        api = FakeApi(task)
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
        runner.binding = binding
        api.fail_task = Mock(side_effect=RuntimeError("backend unavailable"))

        settled = runner._settle_chat_reply_context_failure_before_unlock(
            binding,
            task_id=task.id,
            source_error_code="C2_TARGET_CHAT_NOT_FOUND",
        )

        self.assertFalse(settled)
        self.assertEqual(binding.run_status, "paused")
        self.assertIn("paused", api.run_status_updates)

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
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 1,
                    "recent_messages": [],
                }
            },
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

        self.assertTrue(result["ok"], result)
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

    def test_same_physical_voice_has_one_journal_item_before_sidecar_action(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-voice-alias-journal",
            rpa_session_key="wx:rpa:v1:voice-alias-journal",
            display_name="CJT9V5X1",
            remark_code="CJT9V5X1",
            read_reason="waiting_user_reply",
            authorization_revision="revision-voice-alias-journal",
        )
        api.read_targets = [target]
        observed_journals: list[dict] = []

        class AliasVoiceBridge(FakeBridge):
            def voice_transcribe(
                self,
                *,
                display_name: str,
                rpa_session_key: str,
                **kwargs,
            ):
                journal_path = Path(kwargs["action_journal"])
                journal = read_action_journal(journal_path)
                observed_journals.append(journal)
                source_key = next(iter(journal["items"]))
                update_action_journal_item(
                    journal_path,
                    source_message_key=source_key,
                    action_phase="confirmed",
                    business_state="completed",
                    business_result_confirmed=True,
                    terminal_payload={
                        "state": "completed",
                        "transcribed_message": {
                            "content": "同一条语音只结算一次。",
                            "sender_role": "customer",
                            "voice_anchor_stable_key": "voice-stable:a",
                        },
                    },
                )
                self.voice_payload = {
                    "ok": True,
                    "state": "voice_transcribe_completed",
                    "sidecar_run_id": "voice-alias-run",
                    "processed_voice_anchor_keys": [
                        "voice-parent:shared"
                    ],
                    "failed_voice_anchor_keys": [],
                    "item_action_outcomes": [
                        {
                            "action_phase": "confirmed",
                            "business_state": "completed",
                            "business_result_confirmed": True,
                            "physical_anchor_keys": [
                                "voice-stable:a",
                                "voice-stable:b",
                                "voice-structural:a",
                                "voice-structural:b",
                                "voice-parent:shared",
                            ],
                        }
                    ],
                    "transcribed_messages": [
                        {
                            "content": "同一条语音只结算一次。",
                            "sender_role": "customer",
                            "parent_voice_anchor_key": (
                                "voice-parent:shared"
                            ),
                            "voice_anchor_stable_key": "voice-stable:a",
                        }
                    ],
                }
                return super().voice_transcribe(
                    display_name=display_name,
                    rpa_session_key=rpa_session_key,
                    **kwargs,
                )

        bridge = AliasVoiceBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        initial = bridge._contractual_message_payload(
            {
                "messages": [
                    {
                        "id": "voice-alias-a",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 5"',
                        "voice_anchor_stable_key": "voice-stable:a",
                        "voice_anchor_structural_key": (
                            "voice-structural:a"
                        ),
                    }
                ]
            }
        )
        first = initial["observations"][0]
        first["parent_voice_anchor_key"] = "voice-parent:shared"
        first["voice_anchor_structural_key"] = "voice-structural:a"
        second = json.loads(json.dumps(first))
        second["observation_id"] = "voice-alias-b"
        second["voice_anchor_key"] = "voice-stable:b"
        second["voice_anchor_structural_key"] = "voice-structural:b"
        second["source_message"]["id"] = "voice-alias-b"
        second["source_message"]["voice_anchor_stable_key"] = (
            "voice-stable:b"
        )
        second["source_message"]["voice_anchor_structural_key"] = (
            "voice-structural:b"
        )
        initial["observations"].append(second)
        bridge.get_messages_payloads = [
            initial,
            {
                "messages": [
                    {
                        "id": "voice-alias-a",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "同一条语音只结算一次。",
                        "voice_anchor_stable_key": "voice-stable:a",
                        "voice_anchor_structural_key": (
                            "voice-structural:a"
                        ),
                    }
                ]
            },
        ]
        runner, _ = self.make_runner(api, bridge)

        result = runner._read_one_wechat_target(
            Binding(
                worker_id="worker-voice-alias",
                worker_token="token",
                client_instance_id="client-voice-alias",
                run_status="running",
            ),
            target,
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(observed_journals), 1)
        journal_items = observed_journals[0]["items"]
        self.assertEqual(len(journal_items), 1)
        only_item = next(iter(journal_items.values()))
        self.assertEqual(
            set(only_item["physical_anchor_keys"]),
            {
                "voice-stable:a",
                "voice-stable:b",
                "voice-structural:a",
                "voice-structural:b",
                "voice-parent:shared",
            },
        )
        voice_ledger = list_c2_ledger_entries(
            target.conversation_id,
            message_type="voice",
        )
        self.assertEqual(len(voice_ledger), 1)
        self.assertEqual(voice_ledger[0]["terminal_state"], "completed")
        self.assertNotEqual(
            voice_ledger[0]["result"]["action_outcome"]["action_phase"],
            "not_attempted",
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

        self.assertTrue(result["ok"], result)
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
            "business_state": "completed",
            "business_result_confirmed": True,
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
                        "voice_anchor_structural_key": (
                            "voice-existing-structural"
                        ),
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
                        "voice_anchor_structural_key": (
                            "voice-existing-structural"
                        ),
                        "bubble_rect": [400, 100, 600, 140],
                    },
                    {
                        "id": "voice-arrived-later",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 4"',
                        "voice_anchor_structural_key": (
                            "voice-arrived-later-structural"
                        ),
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
                        "voice_anchor_structural_key": (
                            "voice-existing-structural"
                        ),
                        "bubble_rect": [400, 100, 600, 140],
                    },
                    {
                        "id": "voice-arrived-later",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "后来到达的语音也已转写",
                        "voice_anchor_stable_key": "voice-arrived-later",
                        "voice_anchor_structural_key": (
                            "voice-arrived-later-structural"
                        ),
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
        voice_ledger = list_c2_ledger_entries(
            target.conversation_id,
            message_type="voice",
        )
        self.assertEqual(len(voice_ledger), 2)
        self.assertEqual(
            sorted(item["terminal_state"] for item in voice_ledger),
            ["completed", "failed"],
        )
        self.assertNotIn(
            "not_attempted",
            {
                str(
                    (item.get("result") or {})
                    .get("action_outcome", {})
                    .get("action_phase")
                    or ""
                )
                for item in voice_ledger
            },
        )

    def test_identity_ambiguity_runs_only_after_voice_terminal_is_saved(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-voice-before-identity-gate",
            rpa_session_key="wx:rpa:v1:voice-before-identity",
            display_name="CJIDGATE",
            remark_code="CJIDGATE",
            read_reason="waiting_user_reply",
            authorization_revision="revision-voice-before-identity",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        frame = {
            "messages": [
                {
                    "id": "voice-before-identity",
                    "type": "voice",
                    "sender_role": "customer",
                    "content": '[语音] 5"',
                    "bubble_rect": [400, 100, 600, 140],
                }
            ]
        }
        bridge.get_messages_payloads = [frame, frame, frame]
        bridge.voice_payload = {
            "ok": True,
            "state": "voice_transcribe_partial",
            "sidecar_run_id": "voice-before-identity-run",
            "processed_voice_anchor_keys": [],
            "failed_voice_anchor_keys": ["voice-before-identity"],
            "item_action_outcomes": [
                {
                    "physical_anchor_keys": ["voice-before-identity"],
                    "action_phase": "trigger_attempted",
                    "business_state": "failed",
                    "business_result_confirmed": False,
                    "error_code": "VOICE_TRANSCRIBE_RESULT_UNKNOWN",
                }
            ],
        }
        runner, _ = self.make_runner(api, bridge)
        identity_error = {
            "observation_id": "voice-before-identity",
            "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        }
        runner._reconcile_message_identities = Mock(
            side_effect=lambda _target, observations, **_kwargs: (
                list(observations),
                {},
                [dict(identity_error)],
            )
        )

        result = runner._read_one_wechat_target(
            Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target,
            current_step="state_target_message_read",
            enforce_read_targets=False,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        )
        voice_ledger = list_c2_ledger_entries(
            target.conversation_id,
            message_type="voice",
        )
        self.assertEqual(len(voice_ledger), 1)
        self.assertEqual(voice_ledger[0]["terminal_state"], "failed")
        self.assertNotEqual(
            voice_ledger[0]["result"]["action_outcome"][
                "action_phase"
            ],
            "not_attempted",
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
        original_origin_read_run_id = load_c2_ledger_entry(
            target.conversation_id,
            failed_source_key,
        )["origin_read_run_id"]
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

        self.assertTrue(result["ok"], result)
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
        self.assertEqual(
            ledger["origin_read_run_id"],
            original_origin_read_run_id,
        )
        failed_slot = next(
            item
            for item in api.message_payloads[0]["evidence"][
                "slot_ledger_states"
            ]
            if item["source_message_key"] == failed_source_key
        )
        self.assertEqual(
            failed_slot["origin_read_run_id"],
            original_origin_read_run_id,
        )

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
            failed_voice["raw_payload"]["error_code"],
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
                read_run_id="read-identity-failure-gate",
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
                read_run_id="read-identity-failure-gate",
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
            raw={"identity_checkpoint": identity_checkpoint()},
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
            {item["payload"]["read_run_id"] for item in stored},
            {
                "read-common-outbox-1",
                "read-common-outbox-2",
                "read-common-outbox-3",
            },
        )
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
            failed_voice["raw_payload"]["error_code"],
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
            failed_voice["raw_payload"]["error_code"],
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
                display_name="CJVOIC01 虾丸子大人",
                remark_code="CJVOIC01",
                row_fingerprint={"title_text": "CJVOIC01 虾丸子大人"},
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
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 1,
                    "recent_messages": [],
                }
            },
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        first = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=False)
        second = runner._read_one_wechat_target(binding, target, current_step="state_target_message_read", enforce_read_targets=False)

        self.assertTrue(first.get("ok"))
        self.assertTrue(second.get("ok"), second)
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
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 1,
                    "recent_messages": [],
                }
            },
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
        artifact_dir = Path(
            tempfile.mkdtemp(prefix="chejin-reused-frame-evidence-")
        )
        review_path = artifact_dir / "wechat_messages_targeting_review.json"
        review_path.write_text("{}", encoding="utf-8")
        bridge.locate_payloads = [
            {
                "ok": True,
                "state": "chat_target_confirmed",
                "initial_messages_frame_reused": True,
                "initial_messages_snapshot": snapshot,
                "artifact_dir": str(artifact_dir),
                "review_path": str(review_path),
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(binding, target)

        self.assertTrue(result.get("ok"))
        self.assertEqual(bridge.message_reads, [])
        self.assertTrue(bridge.locate_chats[0]["capture_initial_messages"])
        self.assertEqual(api.message_payloads[0]["messages"][0]["content"], "复用打开会话时的画面")

    def test_reused_initial_snapshot_passes_real_evidence_dir_to_image_vision(self):
        api = FakeApi(None)
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-frame-image-{unique}",
            rpa_session_key="wx:rpa:v1:frame-image",
            display_name="CJFRAME02 客户",
            remark_code="CJFRAME02",
            read_reason="waiting_user_reply",
            authorization_revision=f"revision-frame-image-{unique}",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        snapshot = bridge._contractual_message_payload(
            {
                "ok": True,
                "state": "messages_ocr",
                "sidecar_run_id": "locate-frame-image-1",
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": f"image-frame-{unique}",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                        "item_state": "discovered",
                        "bubble_rect": [420, 180, 650, 320],
                        "image_physical_anchor": {
                            "sender_role": "customer",
                            "bubble_visual_fingerprint": (
                                "dhash64:0123456789abcdef"
                            ),
                            "preceding_stable_message": "",
                            "following_stable_message": "",
                            "occurrence_index": 0,
                            "occurrence_count": 1,
                        },
                        "source_message": {
                            "id": f"image-frame-source-{unique}",
                            "type": "image",
                            "sender_role": "customer",
                        },
                    }
                ],
            }
        )
        artifact_dir = Path(
            tempfile.mkdtemp(prefix="chejin-reused-image-evidence-")
        )
        review_path = artifact_dir / "wechat_messages_targeting_review.json"
        review_path.write_text("{}", encoding="utf-8")
        bridge.locate_payloads = [
            {
                "ok": True,
                "state": "chat_target_confirmed",
                "initial_messages_frame_reused": True,
                "initial_messages_snapshot": snapshot,
                "artifact_dir": str(artifact_dir),
                "review_path": str(review_path),
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        failed_before_copy = {
            "state": "failed",
            "action_phase": "not_attempted",
            "reason": "C2_IMAGE_MENU_OPERATION_FAILED",
            "transaction": {"status": "menu_evidence_incomplete"},
            "diagnostics": {"events": [], "image_persisted": False},
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
            return_value=failed_before_copy,
        ) as vision:
            result = runner._read_one_wechat_target(binding, target)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(vision.call_count, 1)
        self.assertEqual(
            vision.call_args.kwargs["artifact_dir"],
            str(artifact_dir),
        )
        self.assertTrue(artifact_dir.is_dir())

    def test_real_menu_failure_pipeline_releases_barrier_for_next_target(self):
        api = FakeApi(None)
        unique = str(time.time_ns())
        first = WechatReadTarget(
            conversation_id=f"conv-menu-failed-{unique}",
            rpa_session_key="wx:rpa:v1:menu-failed",
            display_name="CJP0A001 客户",
            remark_code="CJP0A001",
            read_reason="waiting_user_reply",
            authorization_revision=f"revision-menu-failed-{unique}",
        )
        second = WechatReadTarget(
            conversation_id=f"conv-after-menu-failed-{unique}",
            rpa_session_key="wx:rpa:v1:after-menu-failed",
            display_name="CJP0B001 客户",
            remark_code="CJP0B001",
            read_reason="waiting_user_reply",
            authorization_revision=f"revision-after-menu-failed-{unique}",
        )
        api.read_targets = [first, second]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        image_observation = {
            "schema_version": 3,
            "observation_id": f"image-menu-failed-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "bubble_rect": [420, 180, 650, 320],
            "image_physical_anchor": {
                "sender_role": "customer",
                "bubble_visual_fingerprint": "dhash64:0123456789abcdef",
                "preceding_stable_message": "",
                "following_stable_message": "",
                "occurrence_index": 0,
                "occurrence_count": 1,
            },
            "source_message": {
                "id": f"image-menu-failed-source-{unique}",
                "type": "image",
                "sender_role": "customer",
            },
        }
        first_snapshot = bridge._contractual_message_payload({
            "ok": True,
            "state": "messages_ocr",
            "sidecar_run_id": f"menu-failed-run-{unique}",
            "observations": [image_observation],
        })
        second_snapshot = bridge._contractual_message_payload({
            "ok": True,
            "state": "messages_ocr",
            "sidecar_run_id": f"after-menu-failed-run-{unique}",
            "messages": [{
                "id": f"after-menu-text-{unique}",
                "sender_role": "customer",
                "type": "text",
                "content": "前一个目标失败后我仍被读取",
            }],
        })
        artifact_dirs = [
            Path(tempfile.mkdtemp(prefix="chejin-menu-failed-a-")),
            Path(tempfile.mkdtemp(prefix="chejin-menu-failed-b-")),
        ]
        bridge.locate_payloads = []
        for index, snapshot in enumerate((first_snapshot, second_snapshot)):
            review_path = (
                artifact_dirs[index]
                / "wechat_messages_targeting_review.json"
            )
            review_path.write_text("{}", encoding="utf-8")
            bridge.locate_payloads.append({
                "ok": True,
                "state": "chat_target_confirmed",
                "initial_messages_frame_reused": True,
                "initial_messages_snapshot": snapshot,
                "artifact_dir": str(artifact_dirs[index]),
                "review_path": str(review_path),
                "window_context": {
                    "hwnd": 31415,
                    "source": "sidecar_selected_main_window",
                },
            })
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-p0-next-target",
            worker_token="token",
            client_instance_id="client-p0-next-target",
            run_status="running",
        )

        class MenuFailurePlugin:
            def __init__(self, *, ports, config):
                pass

            def run(self, context):
                return {
                    "applied": False,
                    "reason": "C2_IMAGE_MENU_OPERATION_FAILED",
                    "clipboard_transaction": {
                        "action_phase": "not_attempted",
                        "status": "menu_evidence_conflict",
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
            "apps.wechat_ai_customer_service.optional_plugins."
            "vision.plugin.BuiltinVisionPlugin",
            MenuFailurePlugin,
        ):
            first_result = runner._read_one_wechat_target(binding, first)
            self.assertTrue(first_result.get("ok"), first_result)
            self.assertEqual(
                list_action_journals(conversation_id=first.conversation_id),
                [],
            )
            self.assertTrue(
                runner._worker_transaction_barrier_ready(
                    binding,
                    reason="after_real_menu_failure",
                )
            )
            second_result = runner._read_one_wechat_target(binding, second)

        self.assertTrue(second_result.get("ok"), second_result)
        self.assertEqual(len(bridge.locate_chats), 2)
        self.assertTrue(
            any(
                item.get("content") == "前一个目标失败后我仍被读取"
                for payload in api.message_payloads
                for item in (payload.get("messages") or [])
            )
        )

    def test_adapter_exception_after_trigger_writes_terminal_before_recovery(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-adapter-exception-{unique}",
            rpa_session_key="wx:rpa:v1:adapter-exception",
            display_name="CJP0EX01 客户",
            remark_code="CJP0EX01",
            authorization_revision=f"revision-adapter-exception-{unique}",
        )
        source_key = f"image-adapter-exception-{unique}"
        observation = {
            "schema_version": 3,
            "observation_id": source_key,
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "bubble_rect": [420, 180, 650, 320],
            "image_physical_anchor": {
                "sender_role": "customer",
                "bubble_visual_fingerprint": "dhash64:0123456789abcdef",
            },
        }

        def crash_after_trigger(**kwargs):
            update_action_journal_item(
                kwargs["action_journal_path"],
                source_message_key=kwargs["source_message_key"],
                action_phase="trigger_attempted",
                business_state=None,
                business_result_confirmed=False,
            )
            raise RuntimeError("adapter_crashed_after_trigger")

        with patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            side_effect=crash_after_trigger,
        ):
            result = runner._execute_one_image_slot_vision(
                target=target,
                payload={},
                observation=observation,
                source_key=source_key,
                cancel_check=None,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-adapter-exception"
                ),
            )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "vision_adapter_failed")
        journals = list_action_journals(
            conversation_id=target.conversation_id,
        )
        self.assertEqual(len(journals), 1)
        item = journals[0][1]["items"][source_key]
        self.assertEqual(item["action_phase"], "trigger_attempted")
        self.assertEqual(item["business_state"], "failed")
        self.assertEqual(item["error_code"], "vision_adapter_failed")
        self.assertEqual(
            item["terminal_payload"]["state"],
            "failed",
        )

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
                    {
                        "name": "腾讯新闻",
                        "session_key": "wx:rpa:v1:news",
                        **sidecar_identity_contract(),
                        "content": "新闻",
                        "ocr_confidence": 0.98,
                    }
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

    def test_c2_scan_warns_and_rejects_invalid_sidecar_identity_contract(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_contradictory_session(**_kwargs):
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-invalid-contract",
                "sessions": [
                    {
                        "name": "CJP6M3R7许聪",
                        "raw_title": "CJP6M3R7许聪",
                        "session_key": "wx:rpa:v1:invalid-contract",
                        "c2_remark_code_candidates": ["CJP6M3R7"],
                        "c2_conversation_admission": {
                            "conversation_type": "unknown",
                            "admission_allowed": True,
                            "remark_code": "CJP6M3R7",
                            "reason": "contradictory_test_fixture",
                        },
                    }
                ],
            }

        bridge.list_sessions = list_contradictory_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        with patch("chejin_worker_client.task_runner.append_log") as logger:
            runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(
            api.scan_payloads[0]["sessions"][0]["remark_code_candidates"],
            [],
        )
        warning_calls = [
            call
            for call in logger.call_args_list
            if len(call.args) >= 2
            and call.args[1] == "c2_sidecar_identity_contract_rejected"
        ]
        self.assertEqual(len(warning_calls), 1)
        self.assertEqual(
            warning_calls[0].kwargs.get("error_code"),
            "C2_SIDECAR_IDENTITY_CONTRACT_INVALID",
        )

    def test_c2_visible_hit_uses_current_scan_session_key_when_backend_binding_key_is_stale(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-voice",
                rpa_session_key="wx:rpa:v1:stale-binding",
                display_name="CJVOIC01 许聪",
                remark_code="CJVOIC01",
                row_fingerprint={"title_text": "CJVOIC01 许聪"},
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
                        "name": "CJVOIC01 许聪",
                        "session_key": "wx:rpa:v1:current-visible",
                        **sidecar_identity_contract(
                            "CJVOIC01",
                            conversation_type="private",
                            allowed=True,
                        ),
                        "row_fingerprint": {"title_text": "CJVOIC01 许聪", "title_bbox": [154, 115, 306, 143]},
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
                        "remark_code": "CJVOIC01",
                        "rpa_session_key": "wx:rpa:v1:stale-binding",
                        "display_name": "CJVOIC01 许聪",
                        "row_fingerprint": {"title_text": "CJVOIC01 许聪"},
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
                display_name="CJVOIC01 虾丸子大人",
                remark_code="CJVOIC01",
                row_fingerprint={"title_text": "CJVOIC01 虾丸子大人"},
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
                        "name": "CJVOIC01 虾丸子大人",
                        "session_key": "wx:rpa:v1:visible-now",
                        "row_fingerprint": {"title_text": "CJVOIC01 虾丸子大人"},
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
        self.assertEqual(bridge.locate_chats[0]["remark_code"], "CJVOIC01")

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
                display_name="CJVOIC01 虾丸子大人",
                remark_code="CJVOIC01",
                row_fingerprint={"title_text": "CJVOIC01 虾丸子大人"},
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
                display_name="CJVOIC01 虾丸子大人",
                remark_code="CJVOIC01",
                row_fingerprint={"title_text": "CJVOIC01 虾丸子大人"},
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
                "display_name": "CJVOIC01 虾丸子大人",
                "rpa_session_key": "wx:rpa:v1:recent-visible",
                "remark_code_candidates": ["CJVOIC01"],
                "last_message_preview": '[语音] 2"',
                "row_fingerprint": {"title_text": "CJVOIC01 虾丸子大人", "title_bbox": [154, 198, 306, 222]},
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
                display_name="CJVOIC01 虾丸子大人",
                remark_code="CJVOIC01",
                row_fingerprint={"title_text": "CJVOIC01 虾丸子大人"},
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
                "display_name": "CJVOIC01 虾丸子大人",
                "rpa_session_key": "wx:rpa:v1:recent-visible",
                "remark_code_candidates": ["CJVOIC01"],
                "last_message_preview": '[语音] 2"',
                "row_fingerprint": {"title_text": "CJVOIC01 虾丸子大人"},
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
                display_name="CJVOIC01 虾丸子大人",
                remark_code="CJVOIC01",
                row_fingerprint={"title_text": "CJVOIC01 虾丸子大人"},
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
                    {"name": "CJVOIC01 虾丸子大人", "session_key": "wx:rpa:v1:a", "content": '[语音] 2"', "ocr_confidence": 0.98},
                    {"name": "群聊", "session_key": "wx:rpa:v1:b", "content": "包含:CJVOIC01 虾丸子大人", "ocr_confidence": 0.98},
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

    def test_first_screen_queue_uses_success_failure_and_backend_cooldowns(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        session = {
            "rpa_session_key": "wx:rpa:v1:cooldown",
            "display_name": "CJCOOL01 客户",
            "remark_code_candidates": ["CJCOOL01"],
            "unread_hint": False,
        }
        payload = {"sessions": [session]}
        result = {
            "bindings": [
                {
                    "conversation_id": "conv-visible-cooldown",
                    "rpa_session_key": "wx:rpa:v1:cooldown",
                    "display_name": "CJCOOL01 客户",
                    "remark_code": "CJCOOL01",
                    "can_ingest_messages": True,
                }
            ]
        }
        target = WechatReadTarget(
            conversation_id="conv-visible-cooldown",
            rpa_session_key="wx:rpa:v1:cooldown",
            display_name="CJCOOL01 客户",
            remark_code="CJCOOL01",
        )
        dedupe_key = runner._target_dedupe_key(target)

        runner.c2_read_success_cooldowns[dedupe_key] = (
            time.monotonic() + 120
        )
        runner._enqueue_visible_hits(payload, result)
        self.assertEqual(runner.visible_hit_queue, [])

        session["unread_hint"] = True
        runner._enqueue_visible_hits(payload, result)
        self.assertEqual(len(runner.visible_hit_queue), 1)

        runner.visible_hit_queue.clear()
        runner.c2_read_failure_cooldowns[dedupe_key] = (
            time.monotonic() + 120
        )
        runner._enqueue_visible_hits(payload, result)
        self.assertEqual(runner.visible_hit_queue, [])

        runner.c2_read_failure_cooldowns.clear()
        runner.c2_read_success_cooldowns.clear()
        session["unread_hint"] = False
        result["bindings"][0]["next_read_due_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=2)
        ).isoformat()
        runner._enqueue_visible_hits(payload, result)
        self.assertEqual(runner.visible_hit_queue, [])

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
            raw={
                "visible_session_source": "first_screen_session_scan",
                "local_unread_hint": True,
            },
        )
        authorized_target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:backend",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="visible_unread",
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
        self.assertEqual(captured[0].read_reason, "visible_unread")
        self.assertEqual(captured[0].raw["authorization_read_reason"], "visible_unread")
        self.assertTrue(captured[0].raw["visible_hit"])

    def test_c2_visible_hit_v3_ingest_carries_current_authorization_revision(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:backend",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="visible_unread",
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
        self.assertEqual(api.message_payloads[0]["evidence"]["read_reason"], "visible_unread")
        self.assertEqual(api.message_payloads[0]["evidence"]["authorization_read_reason"], "visible_unread")

    def test_c2_visible_unread_empty_result_is_reported_to_consume_backend_unread_fact(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-visible-unread-empty",
            rpa_session_key="wx:rpa:v1:visible-unread-empty",
            display_name="CJEMPTY01 客户",
            remark_code="CJEMPTY01",
            row_fingerprint={"title_text": "CJEMPTY01 客户"},
            ocr_confidence=0.98,
            read_reason="visible_unread",
            authorization_revision="revision-visible-unread-empty",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [],
                "state": "messages_ocr",
                "sidecar_run_id": "visible-unread-empty-run",
            }
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
            enforce_read_targets=False,
        )

        self.assertTrue(result.get("ok"))
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["messages"], [])
        self.assertEqual(
            api.message_payloads[0]["evidence"]["authorization_read_reason"],
            "visible_unread",
        )
        self.assertIn("ingest:0", api.events)

    def test_c2_every_authorized_empty_read_reports_completion(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-waiting-empty",
            rpa_session_key="wx:rpa:v1:waiting-empty",
            display_name="CJEMPTY02 客户",
            remark_code="CJEMPTY02",
            read_reason="waiting_user_reply",
            authorization_revision="revision-waiting-empty",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [],
                "state": "messages_ocr",
                "sidecar_run_id": "waiting-empty-run",
            }
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
            enforce_read_targets=False,
        )

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["messages"], [])
        self.assertEqual(
            api.message_payloads[0]["evidence"][
                "authorization_read_reason"
            ],
            "waiting_user_reply",
        )

    def test_c2_visible_unread_all_duplicate_result_still_reports_completion(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-visible-unread-duplicates",
            rpa_session_key="wx:rpa:v1:visible-unread-duplicates",
            display_name="CJDUP001 客户",
            remark_code="CJDUP001",
            row_fingerprint={"title_text": "CJDUP001 客户"},
            ocr_confidence=0.98,
            read_reason="visible_unread",
            authorization_revision="revision-visible-unread-duplicates",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused"),
            message_sender_role="customer",
        )
        runner, _ = self.make_runner(api, bridge)
        original_filter = runner._filter_confirmed_messages

        def filter_all_as_confirmed(payload):
            filtered = original_filter(payload)
            filtered["messages"] = []
            return filtered

        runner._filter_confirmed_messages = filter_all_as_confirmed  # type: ignore[method-assign]
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
            enforce_read_targets=False,
        )

        self.assertTrue(result.get("ok"))
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["messages"], [])
        self.assertIn("ingest:0", api.events)

    def test_c2_visible_unread_without_current_local_unread_fact_uses_visible_fast_path(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="visible_unread",
                authorization_revision="revision-visible-unread",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_visible_but_read_session(**_kwargs):
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-read",
                "sessions": [
                    {
                        "name": "CJTEST01 许聪",
                        "session_key": "wx:rpa:v1:a",
                        **sidecar_identity_contract(
                            "CJTEST01",
                            conversation_type="private",
                            allowed=True,
                        ),
                        "row_fingerprint": {"title_text": "CJTEST01 许聪"},
                        "content": "没有未读标记",
                        "unread_signal": False,
                        "ocr_confidence": 0.98,
                    }
                ],
            }

        bridge.list_sessions = list_visible_but_read_session  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(
            [item["target_mode"] for item in bridge.locate_chats],
            ["visible"],
        )
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            api.message_payloads[0]["evidence"]["authorization_read_reason"],
            "visible_unread",
        )
        self.assertEqual(runner.visible_hit_queue, [])

    def test_c2_offscreen_visible_unread_runs_full_search_state_machine(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="acbd7657-fe82-413b-ac69-39a6535841e1",
            rpa_session_key="wx:rpa:v1:offscreen",
            display_name="CJT9V5X1",
            remark_code="CJT9V5X1",
            read_reason="visible_unread",
            authorization_revision="revision-visible-unread-offscreen",
        )
        api.read_targets = [target]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_unrelated_first_screen(**_kwargs):
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-without-target",
                "sessions": [],
            }

        def post_scan_without_binding(_binding: Binding, payload: dict):
            api.scan_payloads.append(payload)
            api.events.append(
                f"scan:{len(payload.get('sessions') or [])}:{payload.get('error_code')}"
            )
            return {"bound_count": 0, "bindings": []}

        def locate_offscreen_target(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.c2_operation_order.append("locate_chat")
            bridge.locate_chats.append(
                {
                    "display_name": display_name,
                    "rpa_session_key": rpa_session_key,
                    **kwargs,
                }
            )
            mode = kwargs.get("target_mode")
            if mode == "visible":
                return {
                    "ok": False,
                    "state": "target_not_confirmed",
                    "error_code": "TARGET_NOT_CONFIRMED",
                    "target_mode": mode,
                }
            return {
                "ok": True,
                "state": "chat_target_confirmed",
                "target_mode": mode,
                "remark_code": "CJT9V5X1",
                "conversation_type": "private",
                "guard": {
                    "allowed": True,
                    "conversation_type": "private",
                    "remark_code": "CJT9V5X1",
                },
            }

        bridge.list_sessions = list_unrelated_first_screen  # type: ignore[method-assign]
        bridge.locate_chat = locate_offscreen_target  # type: ignore[method-assign]
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observation_schema_version": 3,
                "observations": [],
                "state": "messages_ocr",
                "sidecar_run_id": "offscreen-visible-unread-empty-read",
            }
        ]
        api.post_wechat_session_scan_result = post_scan_without_binding  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        processed_before_state_queue: list[bool] = []
        original_read_state_target_queue = runner._read_state_target_queue

        def read_state_target_queue_with_probe(
            current_binding: Binding,
            *,
            targets: list[WechatReadTarget] | None = None,
        ):
            processed_before_state_queue.append(
                runner._target_dedupe_key(target)
                in runner.c2_round_processed_conversation_ids
            )
            return original_read_state_target_queue(
                current_binding,
                targets=targets,
            )

        runner._read_state_target_queue = read_state_target_queue_with_probe  # type: ignore[method-assign]

        runner._run_c2_scan_round(binding, reason="unit")

        self.assertEqual(processed_before_state_queue, [False])
        self.assertEqual(
            [item["target_mode"] for item in bridge.locate_chats],
            ["visible", "search_by_remark_code"],
        )
        self.assertEqual(bridge.locate_chats[1]["remark_code"], "CJT9V5X1")
        self.assertEqual(bridge.locate_chats[1]["rpa_session_key"], "")
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["messages"], [])
        self.assertEqual(
            api.message_payloads[0]["authorization_revision"],
            "revision-visible-unread-offscreen",
        )
        self.assertEqual(
            api.message_payloads[0]["evidence"]["authorization_read_reason"],
            "visible_unread",
        )
        self.assertEqual(
            bridge.c2_operation_order[:4],
            ["sessions", "locate_chat", "locate_chat", "messages"],
        )
        self.assertEqual(runner.visible_hit_queue, [])

    def test_c2_visible_unread_contract_validation_does_not_require_visible_hit(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        target = WechatReadTarget(
            conversation_id="conv-1",
            rpa_session_key="wx:rpa:v1:a",
            display_name="CJTEST01 许聪",
            remark_code="CJTEST01",
            read_reason="visible_unread",
            authorization_revision="revision-current",
        )

        self.assertIsNone(runner._validate_read_target(target))
        target.raw["visible_hit"] = True
        self.assertIsNone(runner._validate_read_target(target))

    def test_c2_visible_unread_failure_retries_after_cooldown_without_new_visible_hit(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                read_reason="visible_unread",
                authorization_revision="revision-visible-unread",
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"))

        def list_empty_first_screen(**_kwargs):
            bridge.c2_operation_order.append("sessions")
            bridge.session_scans.append({})
            return {
                "ok": True,
                "adapter": "mock",
                "state": "sessions_mock",
                "sidecar_run_id": "session-run-empty",
                "sessions": [],
            }

        def post_scan_without_binding(_binding: Binding, payload: dict):
            api.scan_payloads.append(payload)
            api.events.append(
                f"scan:{len(payload.get('sessions') or [])}:{payload.get('error_code')}"
            )
            return {"bound_count": 0, "bindings": []}

        def locate_by_search(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.c2_operation_order.append("locate_chat")
            bridge.locate_chats.append(
                {
                    "display_name": display_name,
                    "rpa_session_key": rpa_session_key,
                    **kwargs,
                }
            )
            mode = kwargs.get("target_mode")
            return {
                "ok": mode == "search_by_remark_code",
                "state": (
                    "chat_target_confirmed"
                    if mode == "search_by_remark_code"
                    else "target_not_confirmed"
                ),
                "error_code": None if mode == "search_by_remark_code" else "TARGET_NOT_CONFIRMED",
                "target_mode": mode,
            }

        def failing_get_messages(*, display_name: str, rpa_session_key: str, **kwargs):
            bridge.message_reads.append({"display_name": display_name, "rpa_session_key": rpa_session_key, **kwargs})
            return {
                "ok": False,
                "error_code": "TARGET_NOT_CONFIRMED_FOR_MESSAGES",
                "sidecar_run_id": f"message-failed-{len(bridge.message_reads)}",
            }

        bridge.get_messages = failing_get_messages  # type: ignore[method-assign]
        bridge.list_sessions = list_empty_first_screen  # type: ignore[method-assign]
        bridge.locate_chat = locate_by_search  # type: ignore[method-assign]
        api.post_wechat_session_scan_result = post_scan_without_binding  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        runner._run_c2_scan_round(binding, reason="first")
        runner._run_c2_scan_round(binding, reason="cooldown")
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertTrue(runner.c2_read_failure_cooldowns)
        self.assertTrue(all(payload["sessions"] == [] for payload in api.scan_payloads))

        runner.c2_read_failure_cooldowns.clear()
        runner._run_c2_scan_round(binding, reason="retry")

        self.assertEqual(len(bridge.message_reads), 2)
        self.assertEqual(
            [
                item["target_mode"]
                for item in bridge.locate_chats
                if item["target_mode"] == "search_by_remark_code"
            ],
            ["search_by_remark_code", "search_by_remark_code"],
        )
        self.assertEqual(api.message_payloads, [])

    def test_c2_offscreen_visible_unread_revoked_before_search_never_opens_wechat(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-visible-unread-revoked-before-search",
            rpa_session_key="wx:rpa:v1:offscreen",
            display_name="CJT9V5X1",
            remark_code="CJT9V5X1",
            read_reason="visible_unread",
            authorization_revision="revision-revoked-before-search",
        )
        api.read_targets = [target]

        def reject_authorization(
            _binding: Binding,
            conversation_id: str,
            **_kwargs,
        ):
            api.events.append(f"read_authorization:{conversation_id}")
            return {
                "allowed": False,
                "conversation_id": conversation_id,
                "authorization_revision": "",
                "read_reason": "",
            }

        api.get_wechat_read_authorization = reject_authorization  # type: ignore[method-assign]
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

        runner._read_state_target_queue(binding, targets=[target])

        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(
            runner.c2_stats["last_error"],
            "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
        )

    def test_c2_offscreen_visible_unread_revoked_during_search_cancels_immediately(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-visible-unread-revoked-during-search",
            rpa_session_key="wx:rpa:v1:offscreen",
            display_name="CJT9V5X1",
            remark_code="CJT9V5X1",
            read_reason="visible_unread",
            authorization_revision="revision-revoked-during-search",
        )
        api.read_targets = [target]
        authorization_calls = {"count": 0}

        def expire_authorization_during_search(
            _binding: Binding,
            conversation_id: str,
            **_kwargs,
        ):
            api.events.append(f"read_authorization:{conversation_id}")
            authorization_calls["count"] += 1
            allowed = authorization_calls["count"] <= 2
            return {
                "allowed": allowed,
                "conversation_id": conversation_id,
                "authorization_revision": (
                    target.authorization_revision if allowed else ""
                ),
                "read_reason": target.read_reason if allowed else "",
            }

        api.get_wechat_read_authorization = expire_authorization_during_search  # type: ignore[method-assign]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="invite_sent", message="unused")
        )

        def locate_with_authorization_expiry(
            *,
            display_name: str,
            rpa_session_key: str,
            **kwargs,
        ):
            bridge.c2_operation_order.append("locate_chat")
            bridge.locate_chats.append(
                {
                    "display_name": display_name,
                    "rpa_session_key": rpa_session_key,
                    **kwargs,
                }
            )
            mode = kwargs.get("target_mode")
            if mode == "visible":
                return {
                    "ok": False,
                    "state": "target_not_confirmed",
                    "error_code": "TARGET_NOT_CONFIRMED",
                    "target_mode": mode,
                }
            self.assertTrue(kwargs["cancel_check"]())
            return {
                "ok": False,
                "state": "cancelled",
                "error_code": "C2_TARGET_NOT_ALLOWED_BY_READ_TARGETS",
                "target_mode": mode,
            }

        bridge.locate_chat = locate_with_authorization_expiry  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner._read_state_target_queue(binding, targets=[target])

        self.assertEqual(
            [item["target_mode"] for item in bridge.locate_chats],
            ["visible", "search_by_remark_code"],
        )
        self.assertGreaterEqual(authorization_calls["count"], 3)
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(api.message_payloads, [])

    def test_c2_offscreen_visible_unread_unsafe_search_result_never_reads_messages(self):
        for error_code in (
            "TARGET_NOT_CONFIRMED",
            "C2_VISIBLE_TARGET_AMBIGUOUS",
            "C2_GROUP_CHAT_NOT_ALLOWED",
            "C2_CONVERSATION_TYPE_UNKNOWN",
        ):
            with self.subTest(error_code=error_code):
                api = FakeApi(None)
                target = WechatReadTarget(
                    conversation_id=f"conv-{error_code.lower()}",
                    rpa_session_key="wx:rpa:v1:offscreen",
                    display_name="CJT9V5X1",
                    remark_code="CJT9V5X1",
                    read_reason="visible_unread",
                    authorization_revision=f"revision-{error_code.lower()}",
                )
                api.read_targets = [target]
                bridge = FakeBridge(
                    RpaResult(
                        ok=True,
                        result_code="invite_sent",
                        message="unused",
                    )
                )
                bridge.locate_payloads = [
                    {
                        "ok": False,
                        "state": "target_not_confirmed",
                        "error_code": "TARGET_NOT_CONFIRMED",
                        "target_mode": "visible",
                    },
                    {
                        "ok": False,
                        "state": "target_not_confirmed",
                        "error_code": error_code,
                        "target_mode": "search_by_remark_code",
                    },
                ]
                runner, _ = self.make_runner(api, bridge)
                binding = Binding(
                    worker_id="worker-1",
                    worker_token="token",
                    client_instance_id="client-1",
                    run_status="running",
                )

                runner._read_state_target_queue(binding, targets=[target])

                self.assertEqual(
                    [item["target_mode"] for item in bridge.locate_chats],
                    ["visible", "search_by_remark_code"],
                )
                self.assertEqual(bridge.message_reads, [])
                self.assertEqual(api.message_payloads, [])

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

    def test_all_backend_authorized_read_reasons_search_when_offscreen(self):
        backend_read_reasons = (
            "recall_precheck",
            "friend_acceptance_visible_hit",
            "visible_unread",
            "recent_ai_sent",
            "waiting_user_reply",
            "waiting_sales_reply",
        )
        for read_reason in backend_read_reasons:
            with self.subTest(read_reason=read_reason):
                api = FakeApi(None)
                bridge = FakeBridge(
                    RpaResult(ok=True, result_code="unused", message="unused")
                )
                bridge.locate_payloads = [
                    {
                        "ok": False,
                        "state": "target_not_confirmed",
                        "error_code": "TARGET_NOT_CONFIRMED",
                        "target_mode": "visible",
                    },
                    {
                        "ok": True,
                        "state": "chat_target_confirmed",
                        "target_mode": "search_by_remark_code",
                        "conversation_type": "private",
                        "conversation_type_evidence": {
                            "matched": True,
                            "short_code_confirmed": True,
                            "admission_allowed": True,
                            "conversation_type": "private",
                            "raw_title": "CJK7M4Q2 新好友",
                        },
                    },
                ]
                conversation_id = f"conv-offscreen-{read_reason}"
                target = WechatReadTarget(
                    conversation_id=conversation_id,
                    rpa_session_key=f"wx:rpa:v1:{read_reason}",
                    display_name="CJK7M4Q2 新好友",
                    remark_code="CJK7M4Q2",
                    read_reason=read_reason,
                    authorization_revision=f"revision-{read_reason}",
                    raw={"identity_checkpoint": identity_checkpoint()},
                )
                api.read_targets = [target]
                runner, _ = self.make_runner(api, bridge)
                binding = Binding(
                    worker_id="worker-1",
                    worker_token="token",
                    client_instance_id="client-1",
                    run_status="running",
                )

                runner._read_state_target_queue(binding, targets=[target])

                self.assertEqual(
                    [item["target_mode"] for item in bridge.locate_chats],
                    ["visible", "search_by_remark_code"],
                )
                self.assertEqual(bridge.locate_chats[1]["rpa_session_key"], "")
                self.assertEqual(
                    bridge.locate_chats[1]["remark_code"], "CJK7M4Q2"
                )
                activation_count = (
                    1 if read_reason == "friend_acceptance_visible_hit" else 0
                )
                self.assertEqual(
                    len(api.friend_activation_payloads), activation_count
                )
                self.assertEqual(len(bridge.message_reads), 1)
                self.assertEqual(len(api.message_payloads), 1)
                if activation_count:
                    self.assertLess(
                        api.events.index(f"friend_activation:{conversation_id}"),
                        api.events.index("ingest:1"),
                    )
                dedupe_key = runner._target_dedupe_key(target)
                self.assertIn(
                    dedupe_key, runner.c2_round_processed_conversation_ids
                )
                self.assertNotIn(
                    dedupe_key, runner.c2_read_failure_cooldowns
                )

    def test_no_read_reason_specific_return_exists_before_target_chat_locating(self):
        source = textwrap.dedent(
            inspect.getsource(TaskRunner._read_one_wechat_target_impl)
        )
        tree = ast.parse(source)
        locating_line = next(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and node.value == "target_chat_locating"
        )
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If) or node.lineno >= locating_line:
                continue
            condition = ast.unparse(node.test)
            if "read_reason" not in condition:
                continue
            if any(isinstance(child, ast.Return) for child in ast.walk(node)):
                violations.append(condition)
        self.assertEqual(violations, [])

    def test_offscreen_friend_acceptance_revoked_before_locate_never_searches(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-friend-revoked",
            rpa_session_key="wx:rpa:v1:friend-revoked",
            display_name="CJK7M4Q2 新好友",
            remark_code="CJK7M4Q2",
            read_reason="friend_acceptance_visible_hit",
            authorization_revision="revision-friend-revoked",
        )
        api.read_targets = [target]
        api.read_authorization_overrides[target.conversation_id] = {
            "allowed": False,
            "conversation_id": target.conversation_id,
            "authorization_revision": target.authorization_revision,
            "read_reason": target.read_reason,
        }
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

        runner._read_state_target_queue(binding, targets=[target])

        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(api.friend_activation_payloads, [])
        self.assertEqual(bridge.message_reads, [])

    def test_offscreen_friend_acceptance_group_result_never_activates_or_reads(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-friend-group",
            rpa_session_key="wx:rpa:v1:friend-group",
            display_name="CJK7M4Q2 新好友",
            remark_code="CJK7M4Q2",
            read_reason="friend_acceptance_visible_hit",
            authorization_revision="revision-friend-group",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.locate_payloads = [
            {
                "ok": False,
                "state": "target_not_confirmed",
                "error_code": "TARGET_NOT_CONFIRMED",
                "target_mode": "visible",
            },
            {
                "ok": True,
                "state": "chat_target_confirmed",
                "target_mode": "search_by_remark_code",
                "conversation_type": "group",
                "conversation_type_evidence": {
                    "matched": True,
                    "short_code_confirmed": True,
                    "admission_allowed": False,
                    "conversation_type": "group",
                    "raw_title": "CJK7M4Q2 测试群",
                },
            },
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner._read_state_target_queue(binding, targets=[target])

        self.assertEqual(
            [item["target_mode"] for item in bridge.locate_chats],
            ["visible", "search_by_remark_code"],
        )
        self.assertEqual(runner.c2_stats["last_error"], "C2_FRIEND_ACTIVATION_EVIDENCE_INVALID")
        self.assertEqual(api.friend_activation_payloads, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(api.message_payloads, [])

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
            raw={"identity_checkpoint": identity_checkpoint()},
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

    def test_pre_send_refresh_preserves_friend_reason_without_reactivating(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
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
                    "raw_title": "CJK7M4Q2",
                },
            }
        ]
        target = WechatReadTarget(
            conversation_id="conv-friend-pre-send",
            rpa_session_key="wx:rpa:v1:friend-pre-send",
            display_name="CJK7M4Q2",
            remark_code="CJK7M4Q2",
            read_reason="friend_acceptance_visible_hit",
            authorization_revision="revision-friend-pre-send",
            raw={
                "identity_checkpoint": identity_checkpoint(),
                "authorization_read_reason": "friend_acceptance_visible_hit",
                "batch_continuation": {
                    "batch_id": "batch-friend-pre-send",
                    "token": "continuation-batch-friend-pre-send",
                },
            },
        )
        api.read_targets = [target]
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
            operation_phase="pre_send_refresh",
            current_step="pre_send_refresh",
            current_only=True,
            wait_for_brain=False,
            enforce_read_targets=True,
        )

        self.assertTrue(result.get("ok"))
        self.assertEqual(api.friend_activation_payloads, [])
        self.assertTrue(bridge.locate_chats[0]["capture_initial_messages"])
        self.assertEqual(len(bridge.message_reads), 1)

    def test_pre_send_step_without_explicit_operation_phase_fails_closed(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        target = WechatReadTarget(
            conversation_id="conv-pre-send-phase-conflict",
            rpa_session_key="",
            display_name="CJK7M4Q2",
            remark_code="CJK7M4Q2",
            read_reason="friend_acceptance_visible_hit",
            authorization_revision="revision-pre-send-phase-conflict",
        )
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
            current_step="pre_send_refresh",
            wait_for_brain=False,
        )

        self.assertEqual(
            result.get("error_code"),
            "C2_READ_OPERATION_PHASE_CONFLICT",
        )
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(api.friend_activation_payloads, [])

    def test_image_observer_exception_blocks_same_screen_text_ingest(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-image-observer-error",
            rpa_session_key="wx:rpa:v1:image-observer-error",
            display_name="CJIMGERR1",
            remark_code="CJIMGERR1",
            row_fingerprint={"title_text": "CJIMGERR1"},
            read_reason="waiting_user_reply",
            authorization_revision="revision-image-observer-error",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
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
                    "raw_title": "CJIMGERR1",
                },
            }
        ]
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "same-screen-text",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "图片检测器异常时不能单独回复这句话",
                        "source_message": {"id": "same-screen-text"},
                    }
                ],
                "observation_validation_errors": [
                    {
                        "observation_id": "structural-image-observer",
                        "row_kind": "image_bubble",
                        "error_codes": ["C2_IMAGE_OBSERVATION_FAILED"],
                        "stage": "detect_visual_image_bubbles",
                        "error_type": "RuntimeError",
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

        result = runner._read_one_wechat_target(
            binding,
            target,
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "OMNIAUTO_OBSERVATION_CONTRACT_INVALID",
        )
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(bridge.voice_transcribes, [])

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
        original_read_run_id = (
            f"read-outbox-expired-{time.time_ns()}"
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": original_read_run_id,
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
            waiting["payload"]["read_run_id"],
            original_read_run_id,
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

    def test_c2_outbox_rekeys_identity_collision_without_repeating_ui_work(self):
        api = FakeApi(None)
        attempts = 0
        old_source_key = "source-old-collision"
        old_dedupe_key = "dedupe-old-collision"

        def authorize(_binding, conversation_id, **kwargs):
            return {
                "allowed": True,
                "conversation_id": conversation_id,
                "authorization_revision": "fresh-revision",
                "read_reason": "waiting_user_reply",
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 8,
                    "recent_messages": [],
                },
            }

        def collide_once(_binding, payload):
            nonlocal attempts
            attempts += 1
            item = payload["messages"][0]
            if attempts == 1:
                raise ApiError(
                    "MESSAGE_IDENTITY_COLLISION",
                    "collision",
                    409,
                    {
                        "recovery_action": "refresh_identity_and_retry",
                        "source_message_key": old_source_key,
                        "dedupe_key": old_dedupe_key,
                        "next_sequence_floor": 8,
                    },
                )
            return {
                "results": [
                    {
                        "source_message_key": item["source_message_key"],
                        "ingest_result": "ingested",
                    }
                ]
            }

        api.get_wechat_read_authorization = authorize  # type: ignore[method-assign]
        api.post_wechat_messages_ingest = collide_once  # type: ignore[method-assign]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        save_c2_ledger_terminal(
            conversation_id="conv-collision",
            source_message_key=old_source_key,
            dedupe_key=old_dedupe_key,
            message_type="text",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-collision-{time.time_ns()}",
                "conversation_id": "conv-collision",
                "authorization_revision": "old-revision",
                "messages": [
                    {
                        "source_message_key": old_source_key,
                        "dedupe_key": old_dedupe_key,
                        "sender_role_hint": "customer",
                        "message_type": "text",
                        "content": "不会重新读取微信",
                        "raw_payload": {
                            "source_message_key": old_source_key,
                            "dedupe_basis": {
                                "source": "worker_cross_round_sequence",
                                "worker_stable_id": "worker-message-1",
                            },
                        },
                    }
                ],
            }
        )

        self.assertFalse(runner._replay_c2_outbox(binding))
        waiting = next(
            item
            for item in list_c2_outbox_waiting(limit=100)
            if item["outbox_id"] == outbox_id
        )
        replacement = waiting["payload"]["messages"][0]
        self.assertNotEqual(replacement["source_message_key"], old_source_key)
        self.assertEqual(
            replacement["raw_payload"]["dedupe_basis"][
                "worker_stable_id"
            ],
            "worker-message-8",
        )
        self.assertIsNone(
            load_c2_ledger_entry("conv-collision", old_source_key)
        )
        self.assertGreaterEqual(
            int(
                load_c2_state(
                    "message_identity:conv-collision"
                ).get("next_sequence")
                or 0
            ),
            9,
        )
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.voice_transcribes, [])

        self.assertTrue(runner._replay_c2_outbox(binding))
        self.assertEqual(attempts, 2)
        self.assertEqual(
            load_c2_ledger_entry(
                "conv-collision",
                replacement["source_message_key"],
            )["ingest_state"],
            "confirmed",
        )

    def test_missing_local_identity_database_uses_server_sequence_floor(self):
        from chejin_worker_client import storage

        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused")),
        )
        target = WechatReadTarget(
            conversation_id="conv-missing-local-db",
            rpa_session_key="wx:missing-local-db",
            display_name="CJMISS01 测试客户",
            remark_code="CJMISS01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-missing-db",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 31,
                    "recent_messages": [],
                }
            },
        )
        with tempfile.TemporaryDirectory(
            prefix="chejin-missing-identity-db-"
        ) as temp_dir:
            app_dir = Path(temp_dir)
            missing_db = app_dir / "worker_client.sqlite3"
            self.assertFalse(missing_db.exists())
            with patch.object(storage, "APP_DIR", app_dir), patch.object(
                storage,
                "DB_FILE",
                missing_db,
            ):
                reconciled, state, errors = (
                    runner._reconcile_message_identities(
                        target,
                        [
                            self._identity_text_observation(
                                "new-after-db-loss",
                                "本地数据库删除后的新消息",
                                220,
                            )
                        ],
                    )
                )
                self.assertTrue(missing_db.exists())

        self.assertEqual(errors, [])
        self.assertEqual(
            reconciled[0]["_worker_stable_id"],
            "worker-message-31",
        )
        self.assertEqual(state["next_sequence"], 32)

    def test_concurrent_identity_allocation_is_unique_and_transactional(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused")),
        )
        target = WechatReadTarget(
            conversation_id="conv-concurrent-identity",
            rpa_session_key="wx:concurrent-identity",
            display_name="CJCONC01 测试客户",
            remark_code="CJCONC01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-concurrent",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 41,
                    "recent_messages": [],
                }
            },
        )
        barrier = threading.Barrier(3)
        assigned: list[str] = []
        errors: list[list[dict]] = []
        result_lock = threading.Lock()

        def allocate(index: int) -> None:
            barrier.wait()
            reconciled, _state, identity_errors = (
                runner._reconcile_message_identities(
                    target,
                    [
                        self._identity_text_observation(
                            f"concurrent-{index}",
                            f"并发新消息-{index}",
                            180 + index * 180,
                        )
                    ],
                )
            )
            with result_lock:
                assigned.append(reconciled[0]["_worker_stable_id"])
                errors.append(identity_errors)

        threads = [
            threading.Thread(target=allocate, args=(index,))
            for index in (1, 2)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [[], []])
        self.assertEqual(len(set(assigned)), 2)
        self.assertEqual(set(assigned), {"worker-message-41", "worker-message-42"})

    def test_future_server_read_due_blocks_repeated_poll_ui_actions(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-read-not-due",
            rpa_session_key="wx:read-not-due",
            display_name="CJDUE001 测试客户",
            remark_code="CJDUE001",
            read_reason="waiting_user_reply",
            authorization_revision="revision-not-due",
            raw={
                "next_read_due_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=2)
                ).isoformat(),
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 1,
                    "recent_messages": [],
                },
            },
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner._read_state_target_queue(binding, targets=[target])
        runner._read_state_target_queue(binding, targets=[target])

        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(
            runner.c2_stats["last_error"],
            "C2_READ_TARGET_NOT_DUE",
        )

    def test_latest_authorization_checkpoint_is_merged_before_ui_read(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-refresh-checkpoint",
            rpa_session_key="wx:refresh-checkpoint",
            display_name="CJREF001 测试客户",
            remark_code="CJREF001",
            read_reason="waiting_user_reply",
            authorization_revision="revision-refresh-checkpoint",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 2,
                    "recent_messages": [],
                }
            },
        )
        api.read_targets = [target]
        api.read_authorization_overrides[target.conversation_id] = {
            "allowed": True,
            "recovery_decision": "allowed",
            "conversation_id": target.conversation_id,
            "authorization_revision": target.authorization_revision,
            "read_reason": target.read_reason,
            "identity_checkpoint": {
                "version": 2,
                "next_sequence_floor": 50,
                "recent_messages": [],
            },
            "next_read_due_at": None,
        }
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused")),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        self.assertTrue(
            runner._backend_still_allows_read_target(binding, target)
        )
        self.assertEqual(
            target.raw["identity_checkpoint"]["next_sequence_floor"],
            50,
        )

    def test_collision_identity_and_outbox_refresh_are_one_sqlite_transaction(self):
        conversation_id = "conv-collision-rollback"
        old_source_key = "source-collision-rollback-old"
        new_source_key = "source-collision-rollback-new"
        save_c2_ledger_terminal(
            conversation_id=conversation_id,
            source_message_key=old_source_key,
            dedupe_key="dedupe-old",
            message_type="text",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-rollback-{time.time_ns()}",
                "conversation_id": conversation_id,
                "authorization_revision": "revision-1",
                "messages": [],
            }
        )

        with self.assertRaisesRegex(ValueError, "C2_OUTBOX_NOT_WAITING"):
            refresh_c2_outbox_payload(
                outbox_id,
                {
                    "read_run_id": "read-rollback-refreshed",
                    "conversation_id": conversation_id,
                    "authorization_revision": "revision-2",
                    "messages": [],
                },
                next_status="waiting",
                identity_replacement={
                    "old_source_message_key": old_source_key,
                    "new_source_message_key": new_source_key,
                    "new_dedupe_key": "dedupe-new",
                    "new_stable_id": "worker-message-12",
                },
                identity_state_key=(
                    "message_identity:conv-collision-rollback"
                ),
            )

        self.assertIsNotNone(
            load_c2_ledger_entry(conversation_id, old_source_key)
        )
        self.assertIsNone(
            load_c2_ledger_entry(conversation_id, new_source_key)
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
        original_read_run_id = (
            f"read-outbox-invalid-{time.time_ns()}"
        )
        save_c2_ledger_terminal(
            conversation_id="conv-outbox-invalid",
            source_message_key=source_key,
            origin_read_run_id=original_read_run_id,
            dedupe_key="dedupe-voice-invalid-structural",
            message_type="voice",
            terminal_state="completed",
            ingest_state="waiting",
            result={"state": "completed"},
        )
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": original_read_run_id,
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
                    "slot_ledger_states": [
                        {
                            "observation_id": "observation-invalid-voice",
                            "screen_order": 1,
                            "order_source": "visual_top",
                            "row_kind": "voice_transcript",
                            "source_message_key": source_key,
                            "origin_read_run_id": original_read_run_id,
                            "fact_scope": "current_read_run",
                            "delivery_state": "outbox_waiting",
                            "item_state": "completed",
                        }
                    ],
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
        rebuilt_slot = stored["payload"]["evidence"][
            "slot_ledger_states"
        ][0]
        self.assertEqual(rebuilt_slot["item_state"], "failed")
        self.assertEqual(
            rebuilt_slot["origin_read_run_id"],
            original_read_run_id,
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

    def test_backend_target_terminal_keeps_fact_until_settlement(self):
        api = FakeApi(None)

        def reject_unbound(_binding, _payload):
            raise ApiError(
                "MESSAGE_CONVERSATION_NOT_BOUND",
                "target removed",
                409,
                {
                    "recovery_action": "target_terminated",
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
                "trace-validation-paused",
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
        incident_log = next(
            row
            for row in read_logs(limit=50)
            if row.get("event") == "c2_outbox_capability_paused"
            and (row.get("metadata") or {}).get("outbox_id") == outbox_id
            and (row.get("metadata") or {}).get("backend_error_response")
        )
        metadata = incident_log["metadata"]
        self.assertEqual(metadata["trace_id"], "trace-validation-paused")
        self.assertEqual(
            metadata["backend_error_response"]["code"],
            "VALIDATION_ERROR",
        )
        self.assertEqual(
            metadata["backend_error_response"]["data"]["recovery_action"],
            "capability_paused",
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
            "business_state": "completed",
            "business_result_confirmed": True,
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
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-cache"
                ),
            )
            second, second_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-cache"
                ),
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

    def test_completed_image_pushed_out_is_restored_and_ingested_once(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-image-replay",
            worker_token="token",
            client_instance_id="client-image-replay",
            run_status="running",
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-replay-{unique}",
            rpa_session_key="wx:rpa:v1:image-replay",
            display_name="CJREPLAY1",
            remark_code="CJREPLAY1",
            authorization_revision=f"revision-image-replay-{unique}",
        )
        observation = {
            "schema_version": 3,
            "observation_id": f"image-replay-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": {
                "sender_role": "customer",
                "visual_side": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "bubble_visual_fingerprint": (
                    "dhash64:0123456789abcdef"
                ),
                "occurrence_index": 0,
                "occurrence_count": 1,
            },
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {
                "id": f"image-source-{unique}",
                "type": "image",
                "sender_role": "customer",
            },
        }
        context_text = {
            "schema_version": 3,
            "observation_id": f"text-context-{unique}",
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "text",
            "voice_state": "not_voice",
            "content_clean": "图片后的稳定锚点文字",
            "bubble_rect": [420, 340, 650, 390],
            "source_message": {
                "id": f"text-context-source-{unique}",
                "type": "text",
                "sender_role": "customer",
                "content": "图片后的稳定锚点文字",
            },
        }
        reconciled, identity_state, identity_errors = (
            reconcile_v16104_identity_transition(
                target,
                [observation, context_text],
                {},
            )
        )
        self.assertEqual(identity_errors, [])
        initial_payload = {
            "observation_schema_version": 3,
            "authoritative_frame_source": "final_read",
            "observations": reconciled,
        }
        completed = {
            "state": "completed",
            "action_phase": "confirmed",
            "business_state": "completed",
            "business_result_confirmed": True,
            "reason": "vision_ready",
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "已完成且后来被顶出屏幕的车辆图片",
            },
            "visual_bridge_input": {
                "summary": "车辆图片",
            },
            "transaction": {
                "action_phase": "confirmed",
                "image_sha256": "d" * 64,
            },
            "diagnostics": {
                "events": [],
                "image_persisted": False,
            },
        }
        with patch(
            "chejin_worker_client.omniauto_vision."
            "vision_configuration_status",
            return_value={
                "ready": True,
                "config": {
                    "customer_image_understanding": {"enabled": True}
                },
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value=completed,
        ) as vision:
            processed, first_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=initial_payload,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-pushed-out"
                ),
            )
            shifted_context = {
                **context_text,
                "bubble_rect": [420, 120, 650, 170],
                "source_message": {
                    **context_text["source_message"],
                    "bubble_rect": [420, 120, 650, 170],
                },
            }
            new_text = {
                "schema_version": 3,
                "observation_id": f"text-new-{unique}",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "text",
                "voice_state": "not_voice",
                "content_clean": "图片被顶出后出现的新文字",
                "bubble_rect": [420, 190, 650, 240],
                "source_message": {
                    "id": f"text-new-source-{unique}",
                    "type": "text",
                    "sender_role": "customer",
                    "content": "图片被顶出后出现的新文字",
                },
            }
            current_observations, _, current_identity_errors = (
                reconcile_v16104_identity_transition(
                    target,
                    [shifted_context, new_text],
                    identity_state,
                )
            )
            self.assertEqual(current_identity_errors, [])
            pushed_out_payload = {
                "observation_schema_version": 3,
                "authoritative_frame_source": "final_read",
                "observations": current_observations,
            }
            restored = runner._merge_waiting_image_facts(
                target=target,
                sidecar_payload=pushed_out_payload,
            )
            restored, second_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=restored,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-pushed-out"
                ),
            )

        self.assertEqual(vision.call_count, 1)
        self.assertEqual(first_stats["completed"], 1)
        self.assertEqual(second_stats["cached"], 1)
        self.assertEqual(len(restored["observations"]), 3)
        restored_images = [
            item
            for item in restored["observations"]
            if item.get("row_kind") == "image_bubble"
        ]
        self.assertEqual(len(restored_images), 1)
        self.assertEqual(
            restored_images[0]["content_clean"],
            "已完成且后来被顶出屏幕的车辆图片",
        )
        self.assertNotIn("bubble_rect", restored_images[0])
        ingest = build_message_ingest_payload(
            target,
            restored,
            read_run_id=f"read-restored-{unique}",
        )
        self.assertEqual(len(ingest["messages"]), 3)
        self.assertEqual(
            ingest["messages"][0]["message_type"],
            "image",
        )
        self.assertEqual(
            ingest["messages"][0]["item_state"],
            "completed",
        )
        source_key = ingest["messages"][0]["source_message_key"]
        ledger = load_c2_ledger_entry(
            target.conversation_id,
            source_key,
        )
        self.assertEqual(ledger["ingest_state"], "waiting")
        serialized = json.dumps(ledger["result"], ensure_ascii=False)
        self.assertNotIn("image_bytes", serialized)
        self.assertNotIn("image_local_path", serialized)
        self.assertEqual(
            processed["observations"][0]["_worker_stable_id"],
            restored_images[0]["_worker_stable_id"],
        )

    def test_completed_image_survives_final_read_failure_and_restart(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-image-restart",
            worker_token="token",
            client_instance_id="client-image-restart",
            run_status="running",
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-restart-{unique}",
            rpa_session_key="wx:rpa:v1:image-restart",
            display_name="CJRESTART1",
            remark_code="CJRESTART1",
            authorization_revision=f"revision-image-restart-{unique}",
        )
        observation = {
            "schema_version": 3,
            "observation_id": f"image-restart-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": {
                "sender_role": "customer",
                "visual_side": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "bubble_visual_fingerprint": (
                    "dhash64:fedcba9876543210"
                ),
                "occurrence_index": 0,
                "occurrence_count": 1,
            },
            "bubble_rect": [420, 220, 650, 360],
            "source_message": {
                "id": f"image-source-{unique}",
                "type": "image",
                "sender_role": "customer",
            },
        }
        reconciled, identity_state, identity_errors = (
            reconcile_v16104_identity_transition(
                target,
                [observation],
                {},
            )
        )
        self.assertEqual(identity_errors, [])
        save_c2_state(
            f"message_identity:{target.conversation_id}",
            identity_state,
        )
        initial_payload = {
            "observation_schema_version": 3,
            "authoritative_frame_source": "final_read",
            "observations": reconciled,
        }
        completed = {
            "state": "completed",
            "action_phase": "confirmed",
            "business_state": "completed",
            "business_result_confirmed": True,
            "reason": "vision_ready",
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "重启后仍需上报的车辆图片",
            },
            "visual_bridge_input": {"summary": "车辆图片"},
            "transaction": {
                "action_phase": "confirmed",
                "image_sha256": "e" * 64,
            },
            "diagnostics": {
                "events": [],
                "image_persisted": False,
            },
        }
        bridge.get_messages_payloads = [
            {
                "ok": False,
                "state": "final_frame_capture_failed",
            }
        ]
        with patch(
            "chejin_worker_client.omniauto_vision."
            "vision_configuration_status",
            return_value={
                "ready": True,
                "config": {
                    "customer_image_understanding": {"enabled": True}
                },
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value=completed,
        ) as vision:
            processed, _ = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=initial_payload,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-preflight"
                ),
            )
            convergence = runner._converge_current_screen_after_images(
                binding=binding,
                target=target,
                target_label=target.display_name,
                sidecar_payload=processed,
                lease=type(
                    "Lease",
                    (),
                    {"update_step": lambda _self, _step: None},
                )(),
                action_cancel_requested=lambda: False,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-preflight"
                ),
            )
            self.assertFalse(
                runner._worker_transaction_barrier_ready(
                    binding,
                    reason="restart_test_before_recovery",
                )
            )
            second_target = WechatReadTarget(
                conversation_id=f"conv-image-second-{unique}",
                rpa_session_key="wx:rpa:v1:image-second",
                display_name="CJSECOND1",
                remark_code="CJSECOND1",
                authorization_revision=f"revision-image-second-{unique}",
            )
            api.read_targets = [second_target]
            api.read_authorization_overrides[
                target.conversation_id
            ] = {
                "allowed": False,
                "recovery_decision": "settle_without_ui",
                "settlement_mode": "fact_only",
                "settlement_token": "restart-settlement-token",
                "conversation_id": target.conversation_id,
                "authorization_revision": (
                    target.authorization_revision
                ),
                "read_reason": target.read_reason or "",
                "target": {
                    "conversation_id": target.conversation_id,
                    "rpa_session_key": target.rpa_session_key,
                    "display_name": target.display_name,
                    "remark_code": target.remark_code,
                    "row_fingerprint": target.row_fingerprint,
                    "ocr_confidence": target.ocr_confidence,
                    "lead_id": target.lead_id,
                    "sales_id": target.sales_id,
                    "read_reason": target.read_reason or "",
                    "authorization_revision": (
                        target.authorization_revision
                    ),
                },
            }
            recovery_bridge = FakeBridge(
                RpaResult(
                    ok=True,
                    result_code="ok",
                    message="unused",
                )
            )
            recovery_bridge.get_messages_payloads = [
                {
                    "authoritative_frame_source": "initial_read",
                    "observations": [],
                }
            ]
            restarted_runner, _ = self.make_runner(
                api,
                recovery_bridge,
            )
            restarted_runner.binding = binding
            restarted_runner.last_rpa_component_status = "ready"
            restarted_runner.last_wechat_status = "logged_in"
            post_messages_ingest = api.post_wechat_messages_ingest

            def confirm_ingest_then_stop(
                current_binding,
                payload,
                *,
                settlement_token=None,
            ):
                result = post_messages_ingest(
                    current_binding,
                    payload,
                    settlement_token=settlement_token,
                )
                restarted_runner.stop_event.set()
                return result

            api.post_wechat_messages_ingest = confirm_ingest_then_stop
            restarted_runner._c2_loop()
            recovered = (
                target.conversation_id
                not in restarted_runner
                ._pending_image_recovery_conversation_ids()
            )

        self.assertFalse(convergence["ok"])
        self.assertEqual(
            convergence["error_code"],
            "final_frame_capture_failed",
        )
        self.assertTrue(recovered)
        self.assertEqual(vision.call_count, 1)
        self.assertNotIn("sessions", recovery_bridge.c2_operation_order)
        self.assertEqual(
            [item["display_name"] for item in recovery_bridge.locate_chats],
            [],
        )
        self.assertNotIn(
            second_target.remark_code,
            [
                item["display_name"]
                for item in recovery_bridge.locate_chats
            ],
        )
        self.assertEqual(len(api.message_payloads), 1)
        image_messages = [
            item
            for item in api.message_payloads[0]["messages"]
            if item.get("message_type") == "image"
        ]
        self.assertEqual(len(image_messages), 1)
        self.assertEqual(
            image_messages[0]["content"],
            "重启后仍需上报的车辆图片",
        )
        ledger = load_c2_ledger_entry(
            target.conversation_id,
            image_messages[0]["source_message_key"],
        )
        self.assertEqual(ledger["ingest_state"], "confirmed")
        self.assertTrue(
            restarted_runner._worker_transaction_barrier_ready(
                binding,
                reason="restart_test_after_recovery",
            )
        )

    def test_not_attempted_image_journal_is_removed_before_global_barrier(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-image-not-attempted",
            worker_token="token",
            client_instance_id="client-image-not-attempted",
            run_status="running",
        )
        transaction_id = f"image-not-attempted-{time.time_ns()}"
        path = action_journal_path("image", transaction_id)
        initialize_action_journal(
            path,
            action_kind="image",
            transaction_id=transaction_id,
            conversation_id="conv-image-not-attempted",
            origin_read_run_id="read-image-not-attempted",
            items=[
                {
                    "source_message_key": "image-not-attempted-source",
                    "physical_anchor_keys": ["image-anchor"],
                    "replayable_observation": {
                        "schema_version": 3,
                        "observation_id": "image-not-attempted-observation",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                    },
                }
            ],
        )

        self.assertTrue(path.exists())
        self.assertTrue(
            runner._worker_transaction_barrier_ready(
                binding,
                reason="not_attempted_image_journal",
            )
        )
        self.assertFalse(path.exists())
        self.assertNotIn(
            "read_authorization:conv-image-not-attempted",
            api.events,
        )

    def test_legacy_empty_journal_with_confirmed_fact_releases_global_barrier(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-legacy-confirmed-image",
            worker_token="token",
            client_instance_id="client-legacy-confirmed-image",
            run_status="running",
        )
        conversation_id = f"conv-legacy-confirmed-{time.time_ns()}"
        source_key = "image-legacy-confirmed-source"
        transaction_id = f"image-legacy-confirmed-{time.time_ns()}"
        path = action_journal_path("image", transaction_id)
        initialize_action_journal(
            path,
            action_kind="image",
            transaction_id=transaction_id,
            conversation_id=conversation_id,
            origin_read_run_id="read-image-legacy-confirmed",
            items=[{
                "source_message_key": source_key,
                "physical_anchor_keys": ["image-anchor-legacy"],
                "replayable_observation": {
                    "schema_version": 3,
                    "observation_id": "image-legacy-confirmed-observation",
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "image",
                    "voice_state": "not_voice",
                },
            }],
        )
        save_c2_ledger_terminal(
            conversation_id=conversation_id,
            source_message_key=source_key,
            dedupe_key=None,
            message_type="image",
            terminal_state="failed",
            ingest_state="confirmed",
            result={
                "state": "failed",
                "reason": "C2_IMAGE_MENU_OPERATION_FAILED",
                "reason_detail": "menu_evidence_conflict",
            },
        )

        self.assertTrue(path.exists())
        self.assertTrue(
            runner._worker_transaction_barrier_ready(
                binding,
                reason="legacy_confirmed_image_journal",
            )
        )
        self.assertFalse(path.exists())
        self.assertEqual(
            load_c2_ledger_entry(conversation_id, source_key)[
                "ingest_state"
            ],
            "confirmed",
        )

    def test_not_attempted_journal_with_failed_fact_is_never_discarded(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        conversation_id = f"conv-not-attempted-fact-{time.time_ns()}"
        binding = Binding(
            worker_id="worker-not-attempted-fact",
            worker_token="token",
            client_instance_id="client-not-attempted-fact",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key="wx:rpa:v1:not-attempted-fact",
            display_name="CJFACT001 客户",
            remark_code="CJFACT001",
            read_reason="waiting_user_reply",
            authorization_revision="revision-not-attempted-fact",
        )
        api.read_targets = [target]
        transaction_id = f"image-not-attempted-fact-{time.time_ns()}"
        path = action_journal_path("image", transaction_id)
        physical_anchor = {
            "sender_role": "customer",
            "preceding_stable_message": "before-failed-menu",
            "following_stable_message": "after-failed-menu",
            "bubble_visual_fingerprint": "failed-menu-fingerprint",
            "occurrence_index": 0,
        }
        observation = {
            "schema_version": 3,
            "observation_id": "not-attempted-failed-observation",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "failed",
            "image_physical_anchor": physical_anchor,
            "error_code": "C2_IMAGE_SOURCE_INVALID",
            "reason_detail": "text_context_menu_rejected",
            "source_message": {
                "sender_role": "customer",
                "type": "image",
                "image_physical_anchor": physical_anchor,
            },
        }
        source_key = image_observation_source_key(target, observation)
        initialize_action_journal(
            path,
            action_kind="image",
            transaction_id=transaction_id,
            conversation_id=conversation_id,
            origin_read_run_id="read-image-physical-alias",
            items=[
                {
                    "source_message_key": source_key,
                    "physical_anchor_keys": ["image-anchor"],
                    "replayable_observation": observation,
                }
            ],
        )
        update_action_journal_item(
            path,
            source_message_key=source_key,
            action_phase="not_attempted",
            business_state="failed",
            business_result_confirmed=False,
            error_code="C2_IMAGE_SOURCE_INVALID",
            terminal_payload={
                "error_code": "C2_IMAGE_SOURCE_INVALID",
                "reason_detail": "text_context_menu_rejected",
            },
        )

        self.assertEqual(
            runner._discard_not_attempted_image_action_journals(),
            0,
        )
        self.assertTrue(path.exists())
        self.assertIsNone(
            load_c2_ledger_entry(conversation_id, source_key)
        )
        recovered = runner._recover_pending_image_transaction(binding)
        self.assertTrue(
            recovered,
            json.dumps(
                {
                    "ledger": load_c2_ledger_entry(
                        conversation_id,
                        source_key,
                    ),
                    "payloads": api.message_payloads,
                    "logs": read_logs(limit=20),
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        self.assertEqual(
            load_c2_ledger_entry(conversation_id, source_key)[
                "ingest_state"
            ],
            "confirmed",
        )
        self.assertFalse(path.exists())

    def test_image_recovery_retry_later_keeps_exact_transaction_blocking(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-image-retry",
            worker_token="token",
            client_instance_id="client-image-retry",
            run_status="running",
        )
        conversation_id = f"conv-image-retry-{time.time_ns()}"
        source_key = "image-retry-source"
        save_c2_ledger_terminal(
            conversation_id=conversation_id,
            source_message_key=source_key,
            dedupe_key="image-retry-dedupe",
            message_type="image",
            terminal_state="completed",
            ingest_state="waiting",
            result={
                "replayable_observation": {
                    "schema_version": 3,
                    "observation_id": "image-retry-observation",
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "image",
                    "voice_state": "not_voice",
                }
            },
        )
        api.read_authorization_overrides[conversation_id] = {
            "allowed": False,
            "recovery_decision": "retry_later",
            "conversation_id": conversation_id,
            "authorization_revision": "revision-retry",
            "read_reason": "",
        }

        self.assertFalse(
            runner._recover_pending_image_transaction(binding)
        )
        self.assertEqual(
            load_c2_ledger_entry(conversation_id, source_key)[
                "ingest_state"
            ],
            "waiting",
        )
        self.assertFalse(
            runner._worker_transaction_barrier_ready(
                binding,
                reason="image_recovery_retry_later",
            )
        )

    def test_ledger_before_outbox_crash_recovers_full_facts_without_ui(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-invalid-image-recovery",
            worker_token="token",
            client_instance_id="client-invalid-image-recovery",
            run_status="running",
        )
        conversation_id = f"conv-invalid-image-{time.time_ns()}"
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key="wx:rpa:v1:invalid-image",
            display_name="CJONE001 客户",
            remark_code="CJONE001",
            read_reason="waiting_user_reply",
            authorization_revision="revision-invalid-image",
        )
        api.read_targets = [target]
        cases = (
            ("C2_IMAGE_SOURCE_INVALID", "text_context_menu_rejected"),
            ("C2_IMAGE_SOURCE_INVALID", "voice_context_menu_rejected"),
            ("C2_IMAGE_MENU_OPERATION_FAILED", "menu_panel_unconfirmed"),
            ("C2_IMAGE_MENU_OPERATION_FAILED", "menu_evidence_incomplete"),
            ("C2_IMAGE_MENU_OPERATION_FAILED", "menu_evidence_conflict"),
            ("C2_IMAGE_MENU_OPERATION_FAILED", "menu_copy_item_unsafe"),
            (
                "C2_IMAGE_SOURCE_INVALID",
                "clipboard_current_content_not_bitmap",
            ),
        )
        source_keys = []
        journal_items = []
        for index, (formal_reason, reason_detail) in enumerate(cases):
            physical_anchor = {
                "sender_role": "self",
                "preceding_stable_message": f"before-{index}",
                "following_stable_message": f"after-{index}",
                "bubble_visual_fingerprint": f"fingerprint-{index}",
                "occurrence_index": index,
            }
            observation = {
                "schema_version": 3,
                "observation_id": f"invalid-image-observation-{index}",
                "row_kind": "image_bubble",
                "sender_role": "self",
                "sender_role_source": "same_row_avatar",
                "message_type": "image",
                "voice_state": "not_voice",
                "item_state": "failed",
                "image_physical_anchor": physical_anchor,
                "error_code": formal_reason,
                "reason_detail": reason_detail,
                "source_message": {
                    "sender_role": "self",
                    "type": "image",
                    "image_physical_anchor": physical_anchor,
                },
            }
            source_key = image_observation_source_key(target, observation)
            source_keys.append(source_key)
            journal_items.append(
                {
                    "source_message_key": source_key,
                    "physical_anchor_keys": [
                        physical_anchor["bubble_visual_fingerprint"]
                    ],
                    "replayable_observation": observation,
                }
            )
            save_c2_ledger_terminal(
                conversation_id=conversation_id,
                source_message_key=source_key,
                dedupe_key=f"invalid-image-dedupe-{index}",
                message_type="image",
                terminal_state="failed",
                ingest_state="waiting",
                result={
                    "state": "failed",
                    "reason": formal_reason,
                    "reason_detail": reason_detail,
                    "transaction": {
                        "status": reason_detail,
                    },
                    "replayable_observation": observation,
                },
            )

        transaction_id = f"image-ledger-before-outbox-{time.time_ns()}"
        journal_path = action_journal_path("image", transaction_id)
        initialize_action_journal(
            journal_path,
            action_kind="image",
            transaction_id=transaction_id,
            conversation_id=conversation_id,
            origin_read_run_id="read-image-ledger-before-outbox",
            items=journal_items,
        )
        for index, source_key in enumerate(source_keys):
            update_action_journal_item(
                journal_path,
                source_message_key=source_key,
                action_phase=(
                    "trigger_attempted" if index == 3 else "not_attempted"
                ),
                business_state="failed",
                business_result_confirmed=False,
                error_code=cases[index][0],
                terminal_payload={
                    "error_code": cases[index][0],
                    "reason_detail": cases[index][1],
                },
            )
        self.assertEqual(
            [
                item
                for item in list_c2_outbox_waiting()
                if item.get("conversation_id") == conversation_id
            ],
            [],
        )

        with patch(
            "chejin_worker_client.task_runner.TaskRunner._execute_one_image_slot_vision",
            side_effect=AssertionError("recovery must not call Vision"),
        ):
            self.assertTrue(
                runner._recover_pending_image_transaction(binding)
            )
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(api.settlement_tokens, ["test-settlement-token"])
        self.assertEqual(len(api.message_payloads), 1)
        payload = api.message_payloads[0]
        self.assertEqual(payload["authorization_scope"], "fact_settlement")
        self.assertEqual(
            payload["evidence"]["recovery_transaction_id"],
            transaction_id,
        )
        self.assertFalse(payload["evidence"]["wechat_reopened"])
        self.assertFalse(payload["evidence"]["clipboard_repeated"])
        self.assertFalse(payload["evidence"]["vision_repeated"])
        self.assertEqual(
            sorted(
                message["source_message_key"]
                for message in payload["messages"]
            ),
            sorted(source_keys),
        )
        self.assertEqual(
            {
                message["raw_payload"]["reason_detail"]
                for message in payload["messages"]
            },
            {reason_detail for _formal, reason_detail in cases},
        )
        self.assertEqual(
            payload["evidence"]["flow_gate_errors"],
            [],
        )
        self.assertNotIn(
            "failed_image_source_keys",
            payload["evidence"],
        )
        for source_key in source_keys:
            self.assertEqual(
                load_c2_ledger_entry(conversation_id, source_key)[
                    "ingest_state"
                ],
                "confirmed",
            )
        self.assertFalse(journal_path.exists())
        self.assertTrue(
            runner._worker_transaction_barrier_ready(
                binding,
                reason="invalid_image_failure_settled",
            )
        )

    def test_invalid_image_recovery_requires_per_message_backend_confirmation(self):
        api = FakeApi(None)
        api.message_ingest_result = "ignored"
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-invalid-image-unconfirmed",
            worker_token="token",
            client_instance_id="client-invalid-image-unconfirmed",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id=f"conv-invalid-unconfirmed-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:invalid-unconfirmed",
            display_name="CJONE001 客户",
            remark_code="CJONE001",
            read_reason="waiting_user_reply",
            authorization_revision="revision-invalid-unconfirmed",
        )
        api.read_targets = [target]
        physical_anchor = {
            "sender_role": "customer",
            "preceding_stable_message": "before",
            "following_stable_message": "after",
            "bubble_visual_fingerprint": "invalid-unconfirmed",
            "occurrence_index": 0,
        }
        observation = {
            "schema_version": 3,
            "observation_id": "invalid-unconfirmed-observation",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "failed",
            "image_physical_anchor": physical_anchor,
            "error_code": "C2_IMAGE_SOURCE_INVALID",
            "reason_detail": "text_context_menu_rejected",
            "source_message": {
                "sender_role": "customer",
                "type": "image",
                "image_physical_anchor": physical_anchor,
            },
        }
        source_key = image_observation_source_key(target, observation)
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=source_key,
            dedupe_key="invalid-unconfirmed-dedupe",
            message_type="image",
            terminal_state="failed",
            ingest_state="waiting",
            result={
                "state": "failed",
                "reason": "C2_IMAGE_SOURCE_INVALID",
                "reason_detail": "text_context_menu_rejected",
                "transaction": {
                    "status": "text_context_menu_rejected",
                },
                "replayable_observation": observation,
            },
        )

        self.assertFalse(runner._recover_pending_image_transaction(binding))
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(
            load_c2_ledger_entry(target.conversation_id, source_key)[
                "ingest_state"
            ],
            "waiting",
        )

    def test_legacy_target_terminated_cannot_delete_image_fact(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-image-terminated",
            worker_token="token",
            client_instance_id="client-image-terminated",
            run_status="running",
        )
        conversation_id = f"conv-image-terminated-{time.time_ns()}"
        source_key = "image-terminated-source"
        replayable_observation = {
            "schema_version": 3,
            "observation_id": "image-terminated-observation",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
        }
        save_c2_ledger_terminal(
            conversation_id=conversation_id,
            source_message_key=source_key,
            dedupe_key="image-terminated-dedupe",
            message_type="image",
            terminal_state="completed",
            ingest_state="waiting",
            result={
                "replayable_observation": replayable_observation,
            },
        )
        journal_path = action_journal_path(
            "image",
            f"image-terminated-{time.time_ns()}",
        )
        initialize_action_journal(
            journal_path,
            action_kind="image",
            transaction_id="image-terminated-transaction",
            conversation_id=conversation_id,
            origin_read_run_id="read-image-terminated",
            items=[
                {
                    "source_message_key": source_key,
                    "physical_anchor_keys": ["image-anchor"],
                    "replayable_observation": replayable_observation,
                }
            ],
        )
        update_action_journal_item(
            journal_path,
            source_message_key=source_key,
            action_phase="trigger_attempted",
        )
        api.read_authorization_overrides[conversation_id] = {
            "allowed": False,
            "recovery_decision": "target_terminated",
            "conversation_id": conversation_id,
            "authorization_revision": "",
            "read_reason": "",
        }

        self.assertFalse(
            runner._recover_pending_image_transaction(binding)
        )
        ledger = load_c2_ledger_entry(conversation_id, source_key)
        self.assertEqual(ledger["terminal_state"], "completed")
        self.assertEqual(ledger["ingest_state"], "waiting")
        self.assertTrue(journal_path.exists())
        self.assertFalse(
            runner._worker_transaction_barrier_ready(
                binding,
                reason="image_recovery_target_terminated",
            )
        )
        self.assertEqual(bridge.locate_chats, [])

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
        journal_observations: list[dict] = []
        journal_source_keys: list[str] = []

        def vision_boundary(**kwargs):
            journal_path = Path(kwargs["action_journal_path"])
            source_key = str(kwargs["source_message_key"])
            journal_source_keys.append(source_key)
            journal_payload = read_action_journal(journal_path)
            journal_observations.append(
                dict(
                    journal_payload["items"][source_key][
                        "replayable_observation"
                    ]
                )
            )
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
        self.assertEqual(len(journal_observations), 1)
        self.assertEqual(
            journal_observations[0]["sender_role"],
            "customer",
        )
        self.assertEqual(
            journal_observations[0]["source_message"][
                "source_message_key"
            ],
            journal_source_keys[0],
        )
        self.assertNotIn("image_bytes", journal_observations[0])
        self.assertNotIn("image_local_path", journal_observations[0])
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

    def test_scheduler_recovers_confirmed_image_journal_after_process_exit(
        self,
    ):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-image-journal-restart",
            worker_token="token",
            client_instance_id="client-image-journal-restart",
            run_status="running",
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-journal-restart-{unique}",
            rpa_session_key="wx:rpa:v1:image-journal-restart",
            display_name="CJJOURNAL1",
            remark_code="CJJOURNAL1",
            authorization_revision=(
                f"revision-image-journal-restart-{unique}"
            ),
        )
        observation = {
            "schema_version": 3,
            "observation_id": f"image-journal-restart-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": {
                "sender_role": "customer",
                "visual_side": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "bubble_visual_fingerprint": (
                    "dhash64:1234567890abcdef"
                ),
                "occurrence_index": 0,
                "occurrence_count": 1,
            },
            "bubble_rect": [420, 220, 650, 360],
            "source_message": {
                "id": f"image-journal-source-{unique}",
                "type": "image",
                "sender_role": "customer",
            },
        }
        reconciled, identity_state, identity_errors = (
            reconcile_v16104_identity_transition(
                target,
                [observation],
                {},
            )
        )
        self.assertEqual(identity_errors, [])
        save_c2_state(
            f"message_identity:{target.conversation_id}",
            identity_state,
        )
        source_key = image_observation_source_key(
            target,
            reconciled[0],
        )
        flow_outcomes = FlowOutcomeAccumulator(
            origin_read_run_id="read-flow-outcomes"
        )

        def vision_finishes_then_process_exits(**kwargs):
            journal_path = Path(kwargs["action_journal_path"])
            update_action_journal_item(
                journal_path,
                source_message_key=source_key,
                action_phase="trigger_attempted",
                business_state="clipboard_copy_confirmed",
            )
            update_action_journal_item(
                journal_path,
                source_message_key=source_key,
                action_phase="confirmed",
                business_state="completed",
                business_result_confirmed=True,
                terminal_payload={
                    "state": "completed",
                    "error_code": None,
                    "reason_detail": None,
                    "customer_image_understanding": {
                        "schema_version": 1,
                        "vision_summary": "进程退出前已识别的车辆图片",
                    },
                    "visual_bridge_input": {
                        "summary": "车辆图片",
                    },
                },
            )
            raise SystemExit("simulated hard process exit")

        with patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            side_effect=vision_finishes_then_process_exits,
        ) as vision:
            with self.assertRaises(SystemExit):
                runner._execute_one_image_slot_vision(
                    target=target,
                    payload={
                        "window_context": {
                            "hwnd": 100,
                            "capture_source": "confirmed_c2_window",
                        }
                    },
                    observation=reconciled[0],
                    source_key=source_key,
                    cancel_check=lambda: False,
                    flow_outcomes=flow_outcomes,
                )
            self.assertIsNone(
                load_c2_ledger_entry(
                    target.conversation_id,
                    source_key,
                )
            )
            self.assertFalse(
                runner._worker_transaction_barrier_ready(
                    binding,
                    reason="journal_restart_before_recovery",
                )
            )
            second_target = WechatReadTarget(
                conversation_id=f"conv-journal-second-{unique}",
                rpa_session_key="wx:rpa:v1:journal-second",
                display_name="CJSECOND2",
                remark_code="CJSECOND2",
                authorization_revision=f"revision-journal-second-{unique}",
            )
            api.read_targets = [second_target, target]
            recovery_bridge = FakeBridge(
                RpaResult(
                    ok=True,
                    result_code="unused",
                    message="unused",
                )
            )
            recovery_bridge.get_messages_payloads = [
                {
                    "authoritative_frame_source": "initial_read",
                    "observations": [],
                }
            ]
            restarted_runner, _ = self.make_runner(
                api,
                recovery_bridge,
            )
            recovered = (
                restarted_runner._recover_pending_image_transaction(
                    binding
                )
            )

        self.assertTrue(recovered)
        self.assertEqual(vision.call_count, 1)
        self.assertNotIn("sessions", recovery_bridge.c2_operation_order)
        self.assertEqual(
            [item["display_name"] for item in recovery_bridge.locate_chats],
            [],
        )
        self.assertEqual(
            api.settlement_tokens,
            ["test-settlement-token"],
        )
        self.assertEqual(len(api.message_payloads), 1)
        image_messages = [
            item
            for item in api.message_payloads[0]["messages"]
            if item.get("message_type") == "image"
        ]
        self.assertEqual(len(image_messages), 1)
        self.assertEqual(
            image_messages[0]["content"],
            "进程退出前已识别的车辆图片",
        )
        self.assertEqual(image_messages[0]["item_state"], "completed")
        ledger = load_c2_ledger_entry(
            target.conversation_id,
            source_key,
        )
        self.assertEqual(ledger["ingest_state"], "confirmed")
        self.assertNotIn(
            target.conversation_id,
            restarted_runner._pending_image_recovery_conversation_ids(),
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
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-cancelled"
                ),
            )

        self.assertEqual(stats["authorization_revoked"], 1)
        source_key = image_observation_source_key(target, observation)
        self.assertIsNone(load_c2_ledger_entry(target.conversation_id, source_key))
        self.assertIs(vision.call_args.kwargs["cancel_check"](), True)

    def test_c2_pre_action_window_failure_is_terminal_failed_with_same_window_context(self):
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
        failed = {
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
            return_value=failed,
        ) as vision:
            result, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-window-failure"
                ),
            )

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(
            result["observations"][0]["item_state"], "failed"
        )
        source_key = image_observation_source_key(target, observation)
        self.assertEqual(
            load_c2_ledger_entry(
                target.conversation_id, source_key
            )["terminal_state"],
            "failed",
        )
        self.assertEqual(
            vision.call_args.kwargs["window_context"],
            window_context,
        )

    def test_c2_image_moved_out_of_view_is_removed_without_ledger(self):
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
                "state": "not_visible",
                "reason": "image_bubble_not_visible_after_refresh",
                "action_phase": "not_attempted",
                "diagnostics": {"events": [], "image_persisted": False},
            },
        ) as vision:
            result, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-removed"
                ),
            )

        self.assertEqual(stats["removed_from_final_screen"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["completed"], 0)
        self.assertTrue(stats["requires_final_refresh"])
        self.assertEqual(result["observations"], [])
        source_key = image_observation_source_key(target, observation)
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, source_key)
        )
        vision.assert_called_once()

    def test_cached_failed_and_new_completed_image_requires_final_refresh(self):
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
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-refresh-{unique}",
            rpa_session_key="wx:rpa:v1:image-refresh",
            display_name="CJREFRESH01",
            remark_code="CJREFRESH01",
            authorization_revision=f"revision-image-refresh-{unique}",
        )

        def image_observation(name: str, top: int) -> dict:
            return {
                "schema_version": 3,
                "observation_id": f"{name}-{unique}",
                "row_kind": "image_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "image",
                "voice_state": "not_voice",
                "item_state": "discovered",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "preceding_stable_message": (
                        f"before-{name}-{unique}"
                    ),
                    "following_stable_message": (
                        f"after-{name}-{unique}"
                    ),
                    "occurrence_index": 0,
                },
                "bubble_rect": [420, top, 650, top + 120],
                "source_message": {
                    "id": f"source-{name}-{unique}",
                    "type": "image",
                },
            }

        old_failed = image_observation("old-failed", 120)
        new_image = image_observation("new-completed", 300)
        old_source_key = image_observation_source_key(
            target,
            old_failed,
        )
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=old_source_key,
            dedupe_key=None,
            message_type="image",
            terminal_state="failed",
            ingest_state="confirmed",
            result={
                "state": "failed",
                "reason": "C2_IMAGE_SLOT_RECONFIRM_FAILED",
            },
        )
        completed = {
            "state": "completed",
            "action_phase": "confirmed",
            "business_state": "completed",
            "business_result_confirmed": True,
            "reason": "vision_ready",
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "新图片已识别",
            },
            "visual_bridge_input": {"summary": "新图片"},
            "transaction": {"action_phase": "confirmed"},
            "diagnostics": {"events": [], "image_persisted": False},
        }
        with patch(
            "chejin_worker_client.omniauto_vision."
            "vision_configuration_status",
            return_value={
                "ready": True,
                "config": {
                    "customer_image_understanding": {"enabled": True}
                },
            },
        ), patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value=completed,
        ) as vision:
            _, phase_result = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload={
                    "observations": [old_failed, new_image]
                },
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-mixed-cache"
                ),
            )

        self.assertEqual(vision.call_count, 1)
        self.assertEqual(phase_result["cached"], 1)
        self.assertEqual(phase_result["failed"], 1)
        self.assertEqual(phase_result["completed"], 1)
        self.assertEqual(phase_result["new_action_count"], 1)
        self.assertTrue(phase_result["requires_final_refresh"])

    def test_incremental_plan_routes_untrusted_image_to_identity_gate(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-untrusted-{unique}",
            rpa_session_key="wx:rpa:v1:image-untrusted",
            display_name="CJIGNORE01",
            remark_code="CJIGNORE01",
            authorization_revision=f"revision-image-untrusted-{unique}",
        )
        observation = {
            "schema_version": 3,
            "observation_id": f"image-untrusted-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "unknown",
            "sender_role_source": "same_row_avatar_unconfirmed",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "image_physical_anchor": {
                "sender_role": "unknown",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "bubble_visual_fingerprint": f"fingerprint-{unique}",
                "occurrence_index": 0,
            },
            "bubble_rect": [420, 220, 650, 340],
            "source_message": {
                "id": f"source-image-untrusted-{unique}",
                "type": "image",
            },
        }

        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload={
                "observation_schema_version": 3,
                "authoritative_frame_source": "final_read",
                "observations": [observation],
            },
            read_run_id=f"read-image-untrusted-{unique}",
        )

        source_key = image_observation_source_key(target, observation)
        self.assertEqual(len(plan["identity_errors"]), 1)
        self.assertEqual(
            plan["identity_errors"][0]["error_code"],
            "MESSAGE_IDENTITY_UNCONFIRMED",
        )
        self.assertEqual(plan["new_image_source_keys"], set())

        result, phase_result = runner._process_final_image_slots(
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target=target,
            sidecar_payload={"observations": [observation]},
            enforce_read_targets=False,
            allowed_new_source_keys=set(),
            flow_outcomes=FlowOutcomeAccumulator(
                origin_read_run_id="read-image-not-new"
            ),
        )

        self.assertNotIn("ignored", phase_result)
        self.assertEqual(
            result["observations"][0]["item_state"],
            "discovered",
        )
        ledger = load_c2_ledger_entry(target.conversation_id, source_key)
        self.assertIsNone(ledger)

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
            raw={"identity_checkpoint": identity_checkpoint()},
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
                        "removed_from_final_screen": 0,
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
                        "removed_from_final_screen": 0,
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
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-flow-1"
                ),
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
            raw={"identity_checkpoint": identity_checkpoint()},
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
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-flow-2"
                ),
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
            raw={"identity_checkpoint": identity_checkpoint()},
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
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-flow-3"
                ),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        )
        process_images.assert_not_called()

    def test_confirmed_local_ledger_does_not_hide_visible_fact_from_backend(self):
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
            ["old-trigger", "new-sales"],
        )
        self.assertEqual(
            [
                item["observation_id"]
                for item in filtered["evidence"]["observations"]
            ],
            ["observation-old-trigger", "observation-new-sales"],
        )
        self.assertEqual(
            [
                item["source_message_key"]
                for item in filtered["evidence"]["slot_ledger_states"]
            ],
            ["old-trigger", "new-sales"],
        )

    def test_backend_terminal_local_ledger_still_filters_visible_fact(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        payload = {
            "conversation_id": "conv-filter-terminal",
            "messages": [
                {
                    "source_message_key": "terminal-fact",
                    "raw_payload": {
                        "observation": {
                            "observation_id": "terminal-observation",
                        }
                    },
                }
            ],
            "evidence": {
                "observations": [
                    {"observation_id": "terminal-observation"}
                ]
            },
        }

        with patch(
            "chejin_worker_client.task_runner.load_c2_ledger_entry",
            return_value={"ingest_state": "not_required"},
        ):
            filtered = runner._filter_confirmed_messages(payload)

        self.assertEqual(filtered["messages"], [])
        self.assertEqual(filtered["evidence"]["observations"], [])

    def test_c2_history_gap_blocks_brain_but_still_terminalizes_image(self):
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
        current_read_run_id = f"read-history-gap-{unique}"
        first_plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=current_read_run_id,
        )
        old_text_slot = next(item for item in first_plan["slot_ledger_states"] if item["row_kind"] == "text_bubble")
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=old_text_slot["source_message_key"],
            origin_read_run_id=f"read-old-history-{unique}",
            dedupe_key=f"dedupe:{old_text_slot['source_message_key']}",
            message_type="text",
            terminal_state="completed",
            ingest_state="confirmed",
            result={},
        )
        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=current_read_run_id,
        )
        with patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value={
                "state": "failed",
                "reason": "image_copy_failed",
                "action_phase": "not_attempted",
                "diagnostics": {"events": [], "image_persisted": False},
            },
        ) as vision:
            _, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=payload,
                enforce_read_targets=False,
                allowed_new_source_keys=set(
                    plan["new_image_source_keys"]
                ),
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id=current_read_run_id
                ),
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
        self.assertEqual(stats["failed"], 1)
        image_source_key = next(item["source_message_key"] for item in plan["slot_ledger_states"] if item["row_kind"] == "image_bubble")
        self.assertEqual(
            load_c2_ledger_entry(
                target.conversation_id, image_source_key
            )["terminal_state"],
            "failed",
        )
        incident_log = next(
            row
            for row in read_logs(limit=50)
            if row.get("event") == "c2_image_slot_terminalized"
            and (row.get("metadata") or {}).get("conversation_id")
            == target.conversation_id
        )
        incident_path = wait_for_incident(
            incident_log["metadata"]["incident_id"],
            timeout=10.0,
        )
        self.assertIsNotNone(incident_path)
        vision.assert_called_once()

    def test_backend_checkpoint_prevents_false_history_gap_after_local_cleanup(self):
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(
            FakeApi(None),
            bridge,
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-backend-history-{unique}",
            rpa_session_key="wx:rpa:v1:backend-history",
            display_name="CJBACKEND 客户",
            remark_code="CJBACKEND",
            authorization_revision=f"revision-{unique}",
        )
        payload = bridge._contractual_message_payload(
            {
                "ok": True,
                "messages": [
                    {
                        "id": f"new-customer-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "最新问题",
                        "bubble_rect": [420, 120, 650, 160],
                    },
                    {
                        "id": f"backend-old-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "后端已有的旧问题",
                        "bubble_rect": [420, 220, 650, 260],
                    },
                ],
            }
        )
        first = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=f"read-current-backend-{unique}",
        )
        backend_old_source_key = first["slot_ledger_states"][0][
            "source_message_key"
        ]
        local_old_source_key = first["slot_ledger_states"][1][
            "source_message_key"
        ]
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=local_old_source_key,
            origin_read_run_id=f"read-old-local-{unique}",
            dedupe_key=f"dedupe:{local_old_source_key}",
            message_type="text",
            terminal_state="completed",
            ingest_state="confirmed",
            result={},
        )
        target.raw["identity_checkpoint"] = {
            "version": 2,
            "next_sequence_floor": 3,
            "recent_messages": [
                {
                    "stable_id": "worker-message-2",
                    "source_message_key": backend_old_source_key,
                    "origin_read_run_id": f"read-old-backend-{unique}",
                }
            ],
        }

        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=f"read-current-backend-{unique}",
        )

        self.assertEqual(
            [item["ledger_state"] for item in plan["slot_ledger_states"]],
            ["OLD_COMPLETED", "OLD_COMPLETED"],
        )
        self.assertEqual(
            plan["slot_ledger_states"][0]["history_source"],
            "backend_identity_checkpoint",
        )
        self.assertFalse(plan["history_gap"])

    def test_staging_backend_historical_fact_preserves_its_origin(self):
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(FakeApi(None), bridge)
        unique = str(time.time_ns())
        current_read_run_id = f"read-current-stage-{unique}"
        historical_read_run_id = f"read-backend-stage-{unique}"
        target = WechatReadTarget(
            conversation_id=f"conv-backend-stage-{unique}",
            rpa_session_key="wx:rpa:v1:backend-stage",
            display_name="CJSTAGE1 客户",
            remark_code="CJSTAGE1",
            read_reason="waiting_user_reply",
            authorization_revision=f"revision-{unique}",
        )
        sidecar_payload = bridge._contractual_message_payload(
            {
                "ok": True,
                "messages": [
                    {
                        "id": f"backend-history-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "后端已有历史事实",
                        "bubble_rect": [420, 100, 650, 140],
                    }
                ],
            }
        )
        first_plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=sidecar_payload,
            read_run_id=current_read_run_id,
        )
        source_key = first_plan["slot_ledger_states"][0][
            "source_message_key"
        ]
        target.raw["identity_checkpoint"] = {
            "version": 2,
            "next_sequence_floor": 2,
            "recent_messages": [
                {
                    "stable_id": "worker-message-1",
                    "source_message_key": source_key,
                    "origin_read_run_id": historical_read_run_id,
                }
            ],
        }
        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=sidecar_payload,
            read_run_id=current_read_run_id,
        )
        sidecar_payload.update(project_final_slot_flow_gates(plan))
        ingest = build_message_ingest_payload(
            target,
            sidecar_payload,
            read_run_id=current_read_run_id,
        )

        runner._stage_payload_ledger(ingest)

        self.assertEqual(
            load_c2_ledger_entry(
                target.conversation_id,
                source_key,
            )["origin_read_run_id"],
            historical_read_run_id,
        )

    def test_current_read_media_waiting_between_new_text_and_voice_is_not_history_gap(self):
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(FakeApi(None), bridge)
        for terminal_state in ("completed", "failed"):
            with self.subTest(terminal_state=terminal_state):
                unique = f"{terminal_state}-{time.time_ns()}"
                read_run_id = f"read-current-media-{unique}"
                target = WechatReadTarget(
                    conversation_id=f"conv-current-media-{unique}",
                    rpa_session_key="wx:rpa:v1:current-media",
                    display_name="CJCURR01 客户",
                    remark_code="CJCURR01",
                    authorization_revision=f"revision-{unique}",
                )
                payload = bridge._contractual_message_payload(
                    {
                        "ok": True,
                        "messages": [
                            {
                                "id": f"text-{unique}",
                                "sender_role": "customer",
                                "type": "text",
                                "content": "在？",
                                "bubble_rect": [420, 100, 650, 140],
                            },
                            {
                                "id": f"image-{unique}",
                                "sender_role": "customer",
                                "type": "image",
                                "content": "",
                                "bubble_rect": [420, 180, 650, 280],
                            },
                            {
                                "id": f"voice-{unique}",
                                "sender_role": "customer",
                                "type": "voice",
                                "content": "你好，在吗？",
                                "bubble_rect": [420, 320, 650, 370],
                            },
                        ],
                    }
                )
                payload["observations"][1].update(
                    {
                        "row_kind": "image_bubble",
                        "message_type": "image",
                        "content_clean": "",
                        "image_physical_anchor": {
                            "sender_role": "customer",
                            "preceding_stable_message": f"text-{unique}",
                            "following_stable_message": f"voice-{unique}",
                            "occurrence_index": 0,
                        },
                    }
                )
                first = runner._build_final_slot_incremental_plan(
                    target=target,
                    sidecar_payload=payload,
                    read_run_id=read_run_id,
                )
                image_slot = next(
                    item
                    for item in first["slot_ledger_states"]
                    if item["row_kind"] == "image_bubble"
                )
                save_c2_ledger_terminal(
                    conversation_id=target.conversation_id,
                    source_message_key=image_slot["source_message_key"],
                    origin_read_run_id=read_run_id,
                    dedupe_key=None,
                    message_type="image",
                    terminal_state=terminal_state,
                    ingest_state="waiting",
                    result={"state": terminal_state},
                )

                post_media = runner._build_final_slot_incremental_plan(
                    target=target,
                    sidecar_payload=payload,
                    read_run_id=read_run_id,
                )

                self.assertEqual(
                    [
                        item["fact_scope"]
                        for item in post_media["slot_ledger_states"]
                    ],
                    ["current_read_run"] * 3,
                )
                self.assertEqual(
                    post_media["slot_ledger_states"][1]["delivery_state"],
                    "outbox_waiting",
                )
                self.assertEqual(
                    post_media["slot_ledger_states"][1]["item_state"],
                    terminal_state,
                )
                self.assertFalse(post_media["history_gap"])
                self.assertNotIn(
                    image_slot["source_message_key"],
                    post_media["new_image_source_keys"],
                )

    def test_two_conversations_keep_distinct_read_run_ownership(self):
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(FakeApi(None), bridge)
        observed_origins: dict[str, str] = {}
        for suffix in ("a", "b"):
            read_run_id = f"read-conversation-{suffix}"
            target = WechatReadTarget(
                conversation_id=f"conv-read-run-{suffix}",
                rpa_session_key=f"wx:rpa:v1:read-run-{suffix}",
                display_name=f"CJREAD0{suffix.upper()}",
                remark_code=f"CJREAD0{suffix.upper()}",
                authorization_revision=f"revision-{suffix}",
            )
            payload = bridge._contractual_message_payload(
                {
                    "ok": True,
                    "messages": [
                        {
                            "id": f"message-{suffix}",
                            "sender_role": "customer",
                            "type": "text",
                            "content": f"会话 {suffix}",
                            "bubble_rect": [420, 100, 650, 140],
                        }
                    ],
                }
            )
            plan = runner._build_final_slot_incremental_plan(
                target=target,
                sidecar_payload=payload,
                read_run_id=read_run_id,
            )
            observed_origins[target.conversation_id] = plan[
                "slot_ledger_states"
            ][0]["origin_read_run_id"]

        self.assertEqual(
            observed_origins,
            {
                "conv-read-run-a": "read-conversation-a",
                "conv-read-run-b": "read-conversation-b",
            },
        )

    def test_historical_fact_below_current_read_still_creates_history_gap(self):
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(FakeApi(None), bridge)
        unique = str(time.time_ns())
        read_run_id = f"read-current-gap-{unique}"
        target = WechatReadTarget(
            conversation_id=f"conv-real-gap-{unique}",
            rpa_session_key="wx:rpa:v1:real-gap",
            display_name="CJREAL01 客户",
            remark_code="CJREAL01",
            authorization_revision=f"revision-{unique}",
        )
        payload = bridge._contractual_message_payload(
            {
                "ok": True,
                "messages": [
                    {
                        "id": f"current-top-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "本轮顶部消息",
                        "bubble_rect": [420, 100, 650, 140],
                    },
                    {
                        "id": f"historical-middle-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "旧轮次消息",
                        "bubble_rect": [420, 180, 650, 220],
                    },
                    {
                        "id": f"current-bottom-{unique}",
                        "sender_role": "customer",
                        "type": "voice",
                        "content": "本轮语音",
                        "bubble_rect": [420, 260, 650, 310],
                    },
                ],
            }
        )
        first = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=read_run_id,
        )
        middle = first["slot_ledger_states"][1]
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=middle["source_message_key"],
            origin_read_run_id=f"read-historical-{unique}",
            dedupe_key=None,
            message_type="text",
            terminal_state="completed",
            ingest_state="confirmed",
            result={},
        )

        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=read_run_id,
        )

        self.assertTrue(plan["history_gap"])
        self.assertEqual(
            [item["fact_scope"] for item in plan["slot_ledger_states"]],
            ["current_read_run", "historical", "current_read_run"],
        )

    def test_unproved_backend_origin_is_identity_gate_not_history_gap(self):
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(FakeApi(None), bridge)
        unique = str(time.time_ns())
        read_run_id = f"read-unknown-origin-{unique}"
        target = WechatReadTarget(
            conversation_id=f"conv-unknown-origin-{unique}",
            rpa_session_key="wx:rpa:v1:unknown-origin",
            display_name="CJUNKN01 客户",
            remark_code="CJUNKN01",
            authorization_revision=f"revision-{unique}",
        )
        payload = bridge._contractual_message_payload(
            {
                "ok": True,
                "messages": [
                    {
                        "id": f"unknown-{unique}",
                        "sender_role": "customer",
                        "type": "image",
                        "content": "",
                        "bubble_rect": [420, 100, 650, 220],
                    }
                ],
            }
        )
        payload["observations"][0].update(
            {
                "row_kind": "image_bubble",
                "message_type": "image",
                "content_clean": "",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "preceding_stable_message": "top-boundary",
                    "following_stable_message": "bottom-boundary",
                    "occurrence_index": 0,
                },
            }
        )
        first = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=read_run_id,
        )
        source_key = first["slot_ledger_states"][0]["source_message_key"]
        target.raw["identity_checkpoint"] = {
            "version": 2,
            "next_sequence_floor": 2,
            "recent_messages": [
                {
                    "stable_id": "worker-message-1",
                    "source_message_key": source_key,
                }
            ],
        }

        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=read_run_id,
        )

        self.assertEqual(
            plan["slot_ledger_states"][0]["fact_scope"],
            "unknown",
        )
        self.assertTrue(plan["identity_errors"])
        self.assertFalse(plan["history_gap"])
        self.assertEqual(plan["new_image_source_keys"], set())
        payload.update(project_final_slot_flow_gates(plan))
        ingest = build_message_ingest_payload(
            target,
            payload,
            read_run_id=read_run_id,
        )
        runner._stage_payload_ledger(ingest)
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, source_key)
        )

    def test_history_gap_before_latest_self_is_warning_not_reply_gate(self):
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(
            FakeApi(None),
            bridge,
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-historical-gap-{unique}",
            rpa_session_key="wx:rpa:v1:historical-gap",
            display_name="CJHIST 客户",
            remark_code="CJHIST",
            authorization_revision=f"revision-{unique}",
            raw={
                "recoverable_handoff_reason_codes": [
                    "C2_MESSAGE_HISTORY_GAP"
                ]
            },
        )
        payload = bridge._contractual_message_payload(
            {
                "ok": True,
                "messages": [
                    {
                        "id": f"historical-new-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "旧区间新发现",
                        "bubble_rect": [420, 100, 650, 140],
                    },
                    {
                        "id": f"historical-old-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "旧区间已确认",
                        "bubble_rect": [420, 180, 650, 220],
                    },
                    {
                        "id": f"latest-self-{unique}",
                        "sender_role": "self",
                        "type": "text",
                        "content": "销售此前已经回复",
                        "bubble_rect": [700, 260, 920, 300],
                    },
                    {
                        "id": f"latest-customer-{unique}",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "这是最新待回复问题",
                        "bubble_rect": [420, 340, 650, 380],
                    },
                ],
            }
        )
        first = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=f"read-current-historical-{unique}",
        )
        historical_old_key = first["slot_ledger_states"][1][
            "source_message_key"
        ]
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=historical_old_key,
            origin_read_run_id=f"read-old-historical-{unique}",
            dedupe_key=f"dedupe:{historical_old_key}",
            message_type="text",
            terminal_state="completed",
            ingest_state="confirmed",
            result={},
        )

        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=f"read-current-historical-{unique}",
        )

        self.assertFalse(plan["history_gap"])
        self.assertEqual(
            plan["historical_warnings"][0]["warning_code"],
            "C2_MESSAGE_HISTORY_GAP_HISTORICAL",
        )
        self.assertEqual(
            plan["recoverable_handoff_resolution"]["status"],
            "latest_unreplied_turn_complete",
        )
        payload.update(project_final_slot_flow_gates(plan))
        ingest = build_message_ingest_payload(
            target,
            payload,
            read_run_id=f"read-current-historical-{unique}",
        )
        self.assertEqual(
            ingest["evidence"]["recoverable_handoff_resolution"][
                "reason_codes"
            ],
            ["C2_MESSAGE_HISTORY_GAP"],
        )

    def test_cross_round_error_before_latest_self_is_historical_warning(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="ok", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-old-identity-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:old-identity",
            display_name="CJOLDID 客户",
            remark_code="CJOLDID",
            authorization_revision="revision-old-identity",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        observations = [
            {
                "observation_id": "old-ambiguous",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "content_clean": "很早以前的重复消息",
                "bubble_rect": [420, 100, 650, 140],
            },
            {
                "observation_id": "latest-self",
                "row_kind": "text_bubble",
                "sender_role": "self",
                "sender_role_source": "same_row_avatar",
                "content_clean": "销售已经回复",
                "bubble_rect": [700, 220, 920, 260],
            },
            {
                "observation_id": "latest-customer",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "content_clean": "最新问题",
                "bubble_rect": [420, 340, 650, 380],
            },
        ]

        reconciled, blocking, warnings = (
            runner._downgrade_historical_identity_errors(
                target=target,
                observations=observations,
                errors=[
                    {
                        "observation_id": "old-ambiguous",
                        "error_code": (
                            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                        ),
                    }
                ],
            )
        )

        self.assertEqual(blocking, [])
        self.assertEqual(
            [item["observation_id"] for item in reconciled],
            ["latest-self", "latest-customer"],
        )
        self.assertEqual(
            warnings[0]["warning_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS_HISTORICAL",
        )

    def test_history_gap_gets_one_backend_first_current_chat_reread(self):
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(FakeApi(None), bridge)
        target = WechatReadTarget(
            conversation_id=f"conv-gap-retry-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:gap-retry",
            display_name="CJGAPRETRY 客户",
            remark_code="CJGAPRETRY",
            authorization_revision="revision-gap-retry",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        clean_payload = bridge._contractual_message_payload(
            {
                "ok": True,
                "messages": [
                    {
                        "id": "clean-latest-customer",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "重新读取后顺序完整",
                        "bubble_rect": [420, 300, 650, 340],
                    }
                ],
            }
        )
        bridge.get_messages = unittest.mock.Mock(
            return_value=clean_payload
        )

        retried_payload, retried_plan = (
            runner._retry_history_gap_from_backend_once(
                target=target,
                read_run_id="read-gap-retry",
                target_label="CJGAPRETRY",
                sidecar_payload=clean_payload,
                incremental_plan={"history_gap": True},
                cancel_check=lambda: False,
            )
        )

        self.assertFalse(retried_plan["history_gap"])
        self.assertTrue(
            retried_payload[
                "history_gap_automatic_reread_performed"
            ]
        )
        bridge.get_messages.assert_called_once()

    def test_history_gap_reread_new_voice_stays_gated_for_next_authorized_read(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-gap-new-voice-{unique}",
            rpa_session_key="wx:rpa:v1:gap-new-voice",
            display_name="CJGAPV01 客户",
            remark_code="CJGAPV01",
            read_reason="waiting_user_reply",
            authorization_revision=f"revision-gap-new-voice-{unique}",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        api.read_targets = [target]
        initial_messages = {
            "ok": True,
            "messages": [
                {
                    "id": "replyable-current-text",
                    "sender_role": "customer",
                    "type": "text",
                    "content": "这条文字可以回复",
                    "bubble_rect": [420, 100, 650, 140],
                },
            ],
        }
        production_plan = runner._build_final_slot_incremental_plan
        plan_calls = {"count": 0}

        def first_frame_has_history_gap(*args, **kwargs):
            plan = production_plan(*args, **kwargs)
            plan_calls["count"] += 1
            if plan_calls["count"] == 1:
                plan = dict(plan)
                plan["history_gap"] = True
            return plan

        runner._build_final_slot_incremental_plan = (  # type: ignore[method-assign]
            first_frame_has_history_gap
        )
        bridge.get_messages_payloads = [
            initial_messages,
            {
                "ok": True,
                "messages": [
                    {
                        "id": "replyable-current-text",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "这条文字可以回复",
                        "bubble_rect": [420, 100, 650, 140],
                    },
                    {
                        "id": "new-customer-voice",
                        "sender_role": "customer",
                        "type": "voice",
                        "content": '[语音] 6"',
                        "voice_anchor_stable_key": "voice-new-on-gap-reread",
                        "bubble_rect": [420, 220, 650, 260],
                    },
                ],
            },
        ]
        original_ingest = api.post_wechat_messages_ingest

        def ingest_with_batch_only_when_ungated(
            binding: Binding,
            payload: dict,
            *,
            settlement_token: str | None = None,
        ):
            response = original_ingest(
                binding,
                payload,
                settlement_token=settlement_token,
            )
            flow_gate_errors = (
                payload.get("evidence", {}).get("flow_gate_errors", [])
            )
            if "C2_MESSAGE_HISTORY_GAP" not in flow_gate_errors:
                response["message_batch"] = {
                    "batch_id": "batch-must-not-exist",
                    "conversation_id": target.conversation_id,
                }
            return response

        api.post_wechat_messages_ingest = (  # type: ignore[method-assign]
            ingest_with_batch_only_when_ungated
        )
        api.message_batch_statuses = [
            {
                "batch_id": "batch-must-not-exist",
                "batch_status": "no_action",
                "processing": False,
                "decision": "no_action",
                "task": None,
            }
        ]

        result = runner._read_one_wechat_target(
            Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target,
            current_step="state_target_message_read",
            enforce_read_targets=False,
        )

        self.assertEqual(len(api.message_payloads), 1)
        evidence = api.message_payloads[0]["evidence"]
        self.assertIn(
            "C2_MESSAGE_HISTORY_GAP",
            evidence["flow_gate_errors"],
            msg={
                "operations": bridge.c2_operation_order,
                "observations": [
                    item.get("observation_id")
                    for item in evidence["observations"]
                    if isinstance(item, dict)
                ],
                "slot_states": evidence["slot_ledger_states"],
                "plan_call_count": plan_calls["count"],
            },
        )
        self.assertNotIn("message_batch", result["result"])
        self.assertFalse(
            any(event.startswith("message_batch:") for event in api.events)
        )
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertNotIn(
            "new-customer-voice",
            {
                item.get("observation_id")
                for item in evidence["observations"]
                if isinstance(item, dict)
            },
        )

        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "replyable-current-text",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "这条文字可以回复",
                        "bubble_rect": [420, 100, 650, 140],
                    },
                    {
                        "id": "new-customer-voice",
                        "sender_role": "customer",
                        "type": "voice",
                        "content": '[语音] 6"',
                        "voice_anchor_stable_key": (
                            "voice-new-on-gap-reread"
                        ),
                        "bubble_rect": [420, 220, 650, 260],
                    },
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "replyable-current-text",
                        "sender_role": "customer",
                        "type": "text",
                        "content": "这条文字可以回复",
                        "bubble_rect": [420, 100, 650, 140],
                    },
                    {
                        "id": "new-customer-voice-transcript",
                        "sender_role": "customer",
                        "type": "voice",
                        "content": "下一轮已经完成语音转写",
                        "voice_anchor_stable_key": (
                            "voice-new-on-gap-reread"
                        ),
                        "bubble_rect": [420, 220, 650, 260],
                    },
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_completed",
            "sidecar_run_id": "voice-gap-next-authorized-read",
            "attempt_count": 1,
            "quality_flags": [],
            "transcribed_messages": [
                {
                    "content": "下一轮已经完成语音转写",
                    "sender_role": "customer",
                    "voice_anchor_stable_key": (
                        "voice-new-on-gap-reread"
                    ),
                }
            ],
        }

        second_result = runner._read_one_wechat_target(
            Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target,
            current_step="state_target_message_read",
            enforce_read_targets=False,
            wait_for_brain=False,
        )

        self.assertTrue(second_result["fact_ingest_ok"])
        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(len(api.message_payloads), 2)
        self.assertTrue(
            any(
                item.get("message_type") == "voice"
                and item.get("item_state") == "completed"
                for item in api.message_payloads[1]["messages"]
                if isinstance(item, dict)
            )
        )

    def test_real_image_terminal_path_captures_each_unattended_failure(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="ok", message="unused"))
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-incidents-{unique}",
            rpa_session_key="wx:rpa:v1:image-incidents",
            display_name="CJIMAGEINC 客户",
            remark_code="CJIMAGEINC",
            authorization_revision=f"revision-image-incidents-{unique}",
        )
        payload = bridge._contractual_message_payload({"ok": True, "messages": []})
        import chejin_worker_client.incident_evidence as incident_evidence

        artifact_dir = (
            incident_evidence.incident_directory().parent
            / "artifacts"
            / "wechat_c2"
            / "messages"
            / f"image-menu-incident-{unique}"
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        menu_screenshot = artifact_dir / "vision_image_context_menu.png"
        menu_roi_screenshot = artifact_dir / "vision_image_context_menu_ocr_roi.png"
        menu_screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfull-menu")
        menu_roi_screenshot.write_bytes(b"\x89PNG\r\n\x1a\nmenu-roi")
        payload["artifact_dir"] = str(artifact_dir)
        failure_reasons = [
            "image_bubble_not_visible_after_refresh",
            "C2_IMAGE_MENU_OPERATION_FAILED",
            "clipboard_image_fingerprint_mismatch",
            "customer_image_understanding_provider_failed",
        ]
        payload["observations"] = [
            {
                "schema_version": 3,
                "observation_id": f"image-incident-{index}-{unique}",
                "row_kind": "image_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "image",
                "voice_state": "not_voice",
                "item_state": "discovered",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "preceding_stable_message": f"before-{index}-{unique}",
                    "following_stable_message": f"after-{index}-{unique}",
                    "occurrence_index": 0,
                },
                "bubble_rect": [420, 120 + index * 180, 650, 260 + index * 180],
                "source_message": {
                    "id": f"image-source-{index}-{unique}",
                    "type": "image",
                },
            }
            for index in range(len(failure_reasons))
        ]
        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id=f"read-image-failures-{unique}",
        )
        results = []
        for index, reason in enumerate(failure_reasons):
            diagnostics_events = []
            if reason == "C2_IMAGE_MENU_OPERATION_FAILED":
                diagnostics_events = [
                    {
                        "sequence": 8,
                        "stage": "frame_capture",
                        "status": "completed",
                        "phase": "image_context_menu",
                        "screenshot_path": str(menu_screenshot),
                        "roi_screenshot_path": str(menu_roi_screenshot),
                        "ocr_item_count": 40,
                        "local_ocr_item_count": 39,
                        "local_ocr_evidence": [
                            {
                                "text": "复",
                                "confidence": 0.91,
                                "bounds": [540, 272, 570, 320],
                            },
                            {
                                "text": "制",
                                "confidence": 0.92,
                                "bounds": [571, 272, 610, 320],
                            },
                        ],
                    }
                ]
            results.append({
                "state": "failed",
                "reason": reason,
                "action_phase": "not_attempted" if index == 0 else "trigger_attempted",
                "diagnostics": {
                    "events": diagnostics_events,
                    "image_persisted": False,
                    "provider_error_type": "TimeoutExpired" if index == 3 else "",
                },
            })

        with patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            side_effect=results,
        ) as vision:
            _, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=payload,
                enforce_read_targets=False,
                allowed_new_source_keys=set(plan["new_image_source_keys"]),
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id=f"read-image-failures-{unique}"
                ),
            )

        self.assertEqual(stats["failed"], 4)
        self.assertEqual(vision.call_count, 4)
        incident_logs = [
            row
            for row in read_logs(limit=100)
            if row.get("event") == "c2_image_slot_terminalized"
            and (row.get("metadata") or {}).get("conversation_id")
            == target.conversation_id
        ]
        self.assertEqual(len(incident_logs), 4)
        self.assertEqual(
            sorted(row.get("error_code") for row in incident_logs),
            sorted(
                [
                "C2_IMAGE_SLOT_RECONFIRM_FAILED",
                "C2_IMAGE_MENU_OPERATION_FAILED",
                "C2_IMAGE_CLIPBOARD_TRANSACTION_FAILED",
                "C2_IMAGE_UNDERSTANDING_FAILED",
                ]
            ),
        )
        for row in incident_logs:
            incident_path = wait_for_incident(
                row["metadata"]["incident_id"],
                timeout=10.0,
            )
            self.assertIsNotNone(incident_path)
            assert incident_path is not None
            with zipfile.ZipFile(incident_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                if row.get("error_code") == "C2_IMAGE_MENU_OPERATION_FAILED":
                    names = archive.namelist()
                    occurrence = json.loads(
                        archive.read("occurrences/initial.json")
                    )
                    diagnostic_event = occurrence["metadata"]["diagnostics"][
                        "events"
                    ][0]
                    self.assertEqual(
                        [item["text"] for item in diagnostic_event["local_ocr_evidence"]],
                        ["复", "制"],
                    )
                    self.assertEqual(
                        diagnostic_event["roi_screenshot_path"],
                        str(menu_roi_screenshot),
                    )
                    self.assertTrue(
                        any(name.endswith(menu_screenshot.name) for name in names)
                    )
                    self.assertTrue(
                        any(name.endswith(menu_roi_screenshot.name) for name in names)
                    )
            self.assertEqual(manifest["error_code"], row["error_code"])

    def test_c2_image_method_fails_closed_if_global_preflight_is_bypassed(self):
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
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value={
                "state": "failed",
                "reason": "vision_configuration_incomplete",
                "action_phase": "not_attempted",
                "diagnostics": {"events": [], "image_persisted": False},
            },
        ) as vision:
            _, first_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-preflight-bypass"
                ),
            )
            _, second_stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-preflight-bypass"
                ),
            )

        assert vision.call_count == 1
        assert first_stats["failed"] == 1
        assert second_stats["cached"] == 1
        assert second_stats["failed"] == 1
        assert first_stats["cached"] == 0
        source_key = image_observation_source_key(target, sidecar_payload["observations"][0])
        ledger = load_c2_ledger_entry(target.conversation_id, source_key)
        assert ledger["terminal_state"] == "failed"

    def test_legacy_per_target_vision_pause_state_does_not_control_c2(self):
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
            raw={"identity_checkpoint": identity_checkpoint()},
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

    def test_c2_global_vision_preflight_blocks_before_any_wechat_action(self):
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
        runner.last_c2_vision_preflight_at = 0.0
        runner.c2_vision_preflight_ready = False

        with patch(
            "chejin_worker_client.omniauto_vision.vision_configuration_status",
            return_value={
                "ready": False,
                "missing_configuration": [
                    "CUSTOMER_IMAGE_UNDERSTANDING_API_KEY"
                ],
            },
        ):
            self.assertFalse(runner._c2_vision_ready_before_scan())

        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(api.message_payloads, [])
        state = load_c2_state("vision_preflight")
        self.assertEqual(state["state"], "vision_not_ready")
        self.assertEqual(state["error_code"], "C2_VISION_NOT_READY")

    def test_c2_visible_unreconfirmed_image_becomes_failed_fact(self):
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
                        "observation_id": "text-with-unreconfirmed-image",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "item_state": "completed",
                        "content_clean": "请结合图片看看",
                        "bubble_rect": [420, 120, 680, 160],
                        "source_message": {
                            "id": "text-with-unreconfirmed-image",
                            "type": "text",
                            "content": "请结合图片看看",
                        },
                    },
                    {
                        "schema_version": 3,
                        "observation_id": "unreconfirmed-image",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                        "item_state": "discovered",
                        "bubble_rect": [420, 180, 680, 360],
                        "image_physical_anchor": {
                            "sender_role": "customer",
                            "preceding_stable_message": (
                                "text-with-unreconfirmed-image"
                            ),
                            "following_stable_message": "",
                            "bubble_visual_fingerprint": (
                                "dhash64:0123456789abcdef"
                            ),
                            "occurrence_index": 0,
                            "occurrence_count": 1,
                        },
                        "source_message": {
                            "id": "unreconfirmed-image",
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
            conversation_id="conv-image-unreconfirmed-mixed",
            rpa_session_key="",
            display_name="CJIMAGE05",
            remark_code="CJIMAGE05",
            authorization_revision="revision-image-unreconfirmed-mixed",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 1,
                    "recent_messages": [],
                }
            },
        )

        with patch(
            "chejin_worker_client.omniauto_vision."
            "vision_configuration_status",
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
                "reason": "C2_IMAGE_SLOT_RECONFIRM_FAILED",
                "action_phase": "not_attempted",
                "diagnostics": {
                    "events": [],
                    "image_persisted": False,
                },
            },
        ):
            result = runner._read_one_wechat_target(binding, target)

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(api.message_payloads), 1)
        payload = api.message_payloads[0]
        self.assertEqual(
            [item["message_type"] for item in payload["messages"]],
            ["text", "image"],
        )
        self.assertNotIn(
            "C2_IMAGE_UNDERSTANDING_FAILED",
            payload["evidence"]["flow_gate_errors"],
        )
        self.assertNotIn(
            "C2_IMAGE_PROCESSING_DEFERRED",
            payload["evidence"]["flow_gate_errors"],
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
