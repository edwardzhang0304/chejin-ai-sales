from __future__ import annotations

import ast
import copy
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
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("CHEJIN_WORKER_HOME", tempfile.mkdtemp(prefix="chejin-worker-test-"))
os.environ.setdefault("CHEJIN_RPA_MODE", "mock")

from chejin_worker_client.api import ApiError
from chejin_worker_client.action_journal import (
    action_journal_path,
    action_journal_phase,
    commit_action_journal_item_identity,
    initialize_action_journal,
    list_action_journals,
    read_action_journal,
    record_action_sequence_alignment,
    remove_action_journal,
    update_action_journal_item,
)
from chejin_worker_client.c2_contract import contract_revision, contract_sha256
from chejin_worker_client.config import CONFIG
from chejin_worker_client.models import Binding, RpaResult, RpaStep, Task, WechatReadTarget, WorkerProfile
from chejin_worker_client.incident_evidence import wait_for_incident
from chejin_worker_client.message_identity_commit import (
    MessageCommitBasis,
    committed_identity_record,
)
from chejin_worker_client.rpa_bridge import RpaBridge
from chejin_worker_client.sequence_alignment import (
    build_pre_action_identity_sequence,
    normalized_content_hash,
)
from chejin_worker_client.storage import (
    begin_runtime_flow,
    checkpoint_c2_action_outcomes,
    c2_outbox_id,
    db_connection,
    enqueue_c2_outbox,
    finish_runtime_flow,
    has_pending_c2_outbox,
    list_c2_action_journal,
    list_c2_ledger_entries,
    list_c2_outbox_waiting,
    load_c2_state,
    load_runtime_control,
    load_c2_ledger_entry,
    load_c2_outbox_entry,
    load_reply_send_ack_outbox,
    read_logs,
    request_runtime_pause,
    refresh_c2_outbox_payload,
    save_c2_state,
    save_c2_ledger_terminal as _save_c2_ledger_terminal,
    save_reply_send_intent,
    transition_c2_outbox,
)
from chejin_worker_client.task_runner import (
    C2_RECENT_VISIBLE_CACHE_TTL_SECONDS,
    TaskLeaseGuard,
    TaskRunner,
    _executable_untranscribed_voice_observations,
    align_post_action_observations,
    collapse_same_frame_voice_aliases,
    _freeze_phase_metadata,
)
from chejin_worker_client.transaction_outcomes import (
    FlowOutcomeAccumulator,
    merge_item_outcomes,
)
from chejin_worker_client.ui_lock import LOCK_FILE, UiLockError
from chejin_worker_client.wechat_c2 import (
    apply_image_terminal_result,
    build_message_ingest_payload,
    image_observation_source_key,
    project_final_slot_flow_gates,
    voice_observation_source_key,
)


def worker_source_message_key(target, *, identity_kind, identity):
    """Construct a historical fixture key without a production bypass."""

    raw = json.dumps(
        {
            "conversation_id": target.conversation_id,
            "identity_kind": str(identity_kind).strip().lower(),
            "identity": identity,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return "source:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:40]


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


def attach_sequence_identity_fixture(
    payload: dict,
    *,
    frame_id: str,
) -> dict:
    """Give direct plan tests the identities production alignment provides."""

    observations = [
        dict(item) if isinstance(item, dict) else item
        for item in (payload.get("observations") or [])
    ]
    new_ids: list[str] = []
    for index, item in enumerate(observations, start=1):
        if not isinstance(item, dict):
            continue
        observation_id = str(item.get("observation_id") or "").strip()
        message_type = str(item.get("message_type") or "").strip().lower()
        row_kind = str(item.get("row_kind") or "").strip().lower()
        source_message = (
            item.get("source_message")
            if isinstance(item.get("source_message"), dict)
            else {}
        )
        native_id = str(
            item.get("native_source_message_id")
            or source_message.get("native_source_message_id")
            or ""
        ).strip()
        if native_id:
            stable_id = str(
                item.get("_worker_stable_id")
                or f"worker-message-{index}"
            )
            item["_worker_stable_id"] = stable_id
            item["_worker_identity_scope"] = "committed"
            item["_worker_committed_message"] = committed_identity_record(
                worker_stable_id=stable_id,
                commit_basis=MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID,
                observation_id=observation_id,
                sender_role=str(item.get("sender_role") or ""),
                message_type=message_type,
                proof={
                    "native_source_message_id": native_id,
                    "sender_role": str(item.get("sender_role") or ""),
                    "message_type": message_type,
                },
            )
        elif (
            message_type == "text" and row_kind == "text_bubble"
        ) or (
            message_type == "system"
            and row_kind in {"system_row", "system_message"}
        ):
            stable_id = str(
                item.get("_worker_stable_id")
                or f"worker-message-{index}"
            )
            item["_worker_stable_id"] = stable_id
            item["_worker_identity_scope"] = "committed"
            item["_worker_committed_message"] = committed_identity_record(
                worker_stable_id=stable_id,
                commit_basis=MessageCommitBasis.NEW_SUFFIX,
                observation_id=observation_id,
                sender_role=str(item.get("sender_role") or ""),
                message_type=message_type,
                proof={
                    "alignment_status": "not_required",
                    "old_tail_fully_consumed": True,
                    "new_suffix_observation_id": observation_id,
                },
            )
        if observation_id:
            new_ids.append(observation_id)
    payload["observations"] = observations
    payload["sequence_alignment_evidence"] = {
        "pre_sequence_source": "empty_checkpoint",
        "pre_frame_id": f"checkpoint:none:{frame_id}",
        "post_frame_id": f"frame:{frame_id}",
        "alignment_status": "not_required",
        "candidate_alignment_count": 0,
        "matched_pairs": [],
        "old_tail_fully_consumed": True,
        "new_suffix_observation_ids": new_ids,
    }
    return payload


def attach_native_committed_identity(
    observation: dict,
    *,
    worker_stable_id: str,
    native_source_message_id: str,
) -> dict:
    item = dict(observation)
    observation_id = str(item.get("observation_id") or "")
    sender_role = str(item.get("sender_role") or "")
    message_type = str(item.get("message_type") or "")
    item["native_source_message_id"] = native_source_message_id
    item["_worker_stable_id"] = worker_stable_id
    item["_worker_identity_scope"] = "committed"
    item["_worker_committed_message"] = committed_identity_record(
        worker_stable_id=worker_stable_id,
        commit_basis=MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID,
        observation_id=observation_id,
        sender_role=sender_role,
        message_type=message_type,
        proof={
            "native_source_message_id": native_source_message_id,
            "sender_role": sender_role,
            "message_type": message_type,
        },
    )
    return item


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
        self.inflight_flow_id: str | None = None
        self.inflight_flow_state: dict = {}
        self.inflight_flow_events: list[str] = []

    def start_inflight_flow(self, binding: Binding, *, flow_id: str, flow_kind: str):
        self.inflight_flow_id = flow_id
        self.inflight_flow_events.append(f"start:{flow_kind}:{flow_id}")
        self.inflight_flow_state = {
            "status": "active",
            "flow_id": flow_id,
            "flow_kind": flow_kind,
            "registered_at": "2026-08-14T00:00:00+00:00",
            "pause_requested_at": None,
        }
        return dict(self.inflight_flow_state)

    def finish_inflight_flow(
        self,
        binding: Binding,
        *,
        flow_id: str,
        terminal_kind: str,
        conversation_id: str | None = None,
        error_code: str | None = None,
    ):
        self.inflight_flow_events.append(
            f"finish:{flow_id}:{terminal_kind}:{conversation_id or ''}:{error_code or ''}"
        )
        self.inflight_flow_id = None
        self.inflight_flow_state = {}
        return {"finished": True, "flow_id": flow_id}

    def heartbeat(self, binding: Binding, **kwargs):
        self.heartbeat_payloads.append(dict(kwargs))
        self.events.append(f"heartbeat:{kwargs['rpa_component_status']}:{kwargs['wechat_status']}")
        return WorkerProfile(
            id=binding.worker_id,
            worker_name="测试 Worker",
            run_status=self.heartbeat_run_status or binding.run_status,
            inflight_flow_state=dict(self.inflight_flow_state),
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
        if run_status == "paused" and self.inflight_flow_state:
            self.inflight_flow_state["status"] = "draining"
            self.inflight_flow_state["pause_requested_at"] = (
                "2026-08-14T00:00:01+00:00"
            )
        return WorkerProfile(
            id=binding.worker_id,
            worker_name="测试 Worker",
            run_status=run_status,
            inflight_flow_state=dict(self.inflight_flow_state),
        )

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
        self.last_message_payload: dict = {}
        self.get_messages_payloads: list[dict] = []
        self.locate_chats: list[dict] = []
        self.locate_payloads: list[dict] = []
        self.voice_transcribes: list[dict] = []
        self.voice_payloads: list[dict] = []
        self.c2_operation_order: list[str] = []
        self.add_friend_cancel_check = None
        self.probe_calls = 0
        self.preflight_calls = 0
        self.calibration_prepare_calls = 0
        self.calibration_verify_calls = 0
        self.preflight_payload = {"ok": True, "state": "mock_preflight"}
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

    def preflight_window_normalization(self):
        self.preflight_calls += 1
        return dict(self.preflight_payload)

    def verify_window_readiness(self):
        self.preflight_calls += 1
        return dict(self.preflight_payload)

    def prepare_startup_layout_for_new_transaction(self):
        self.calibration_prepare_calls += 1
        return dict(self.preflight_payload)

    def verify_startup_layout_for_inflight_transaction(self):
        self.calibration_verify_calls += 1
        return dict(self.preflight_payload)

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
            result = self._contractual_message_payload(payload)
            self.last_message_payload = dict(result)
            return result
        result = self._contractual_message_payload({
            "ok": True,
            "adapter": "mock",
            "state": "messages_mock",
            "sidecar_run_id": "message-run-1",
            "messages": [
                {"id": "wx-msg-1", "sender_role": self.message_sender_role, "type": "text", "content": "你好", "ocr_confidence": 0.98}
            ],
        })
        self.last_message_payload = dict(result)
        return result

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

    def prepare_voice_action(self, *, display_name: str, rpa_session_key: str, **kwargs):
        self.c2_operation_order.append("voice_prepare")
        observations = collapse_same_frame_voice_aliases(
            list(self.last_message_payload.get("observations") or [])
        )
        candidates = [
            item for item in observations
            if isinstance(item, dict)
            and item.get("row_kind") == "voice_bubble"
            and item.get("voice_state") == "untranscribed"
        ]
        excluded = {
            str(value).strip()
            for value in (kwargs.get("excluded_voice_anchor_keys") or [])
            if str(value).strip()
        }
        candidates = [
            item for item in candidates
            if str(item.get("voice_anchor_key") or item.get("observation_id") or "")
            not in excluded
        ]
        if not candidates:
            return self._contractual_message_payload({
                **self.last_message_payload,
                "observations": observations,
                "ok": True,
                "state": "voice_action_prepare_empty",
                "voice_action_stage": "prepare",
                "pre_frame_id": "fixture-empty-frame",
                "ui_action_performed": False,
            })
        selected = candidates[0]
        selected_id = str(selected.get("observation_id") or "")
        return self._contractual_message_payload({
            **self.last_message_payload,
            "observations": observations,
            "ok": True,
            "state": "voice_action_prepared",
            "voice_action_stage": "prepare",
            "pre_frame_id": f"fixture-pre:{selected_id}",
            "selected_pre_observation_id": selected_id,
            "selected_action_token": f"fixture-token:{selected_id}",
            "selected_target_fingerprint": f"fixture-fingerprint:{selected_id}",
            "selected_voice_observation": dict(selected),
            "selected_physical_anchor_keys": sorted({
                str(value).strip()
                for value in (
                    selected.get("_voice_action_anchor_keys")
                    or [selected.get("voice_anchor_key") or selected_id]
                )
                if str(value or "").strip()
            }),
            "candidate_group_count": len(candidates),
            "ui_action_performed": False,
        })

    def execute_voice_action(self, *, display_name: str, rpa_session_key: str, **kwargs):
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
        action_id = str(
            kwargs.get("canonical_voice_action_id") or ""
        ).strip()
        reserved_id = str(
            kwargs.get("reserved_worker_stable_id") or ""
        ).strip()
        selected_observation_id = str(
            kwargs.get("selected_pre_observation_id") or ""
        ).strip()
        selected_observation = next(
            (
                item
                for item in (
                    self.last_message_payload.get("observations") or []
                )
                if isinstance(item, dict)
                and str(item.get("observation_id") or "").strip()
                == selected_observation_id
            ),
            {},
        )
        selected_sender_role = str(
            selected_observation.get("sender_role") or ""
        ).strip().lower()
        if (
            action_id
            and reserved_id
            and str(payload.get("state") or "")
            == "voice_transcribe_partial"
        ):
            selected_id = str(
                kwargs.get("selected_pre_observation_id") or ""
            ).strip()
            selected = next(
                (
                    item
                    for item in (
                        self.last_message_payload.get("observations") or []
                    )
                    if isinstance(item, dict)
                    and str(item.get("observation_id") or "").strip()
                    == selected_id
                ),
                {},
            )
            selected_source = (
                selected.get("source_message")
                if isinstance(selected.get("source_message"), dict)
                else {}
            )
            selected_aliases = {
                str(value).strip()
                for value in (
                    selected_id,
                    selected.get("voice_anchor_key"),
                    selected.get("parent_voice_anchor_key"),
                    selected.get("voice_anchor_structural_key"),
                    selected_source.get("id"),
                    selected_source.get("voice_anchor_stable_key"),
                    selected_source.get("voice_anchor_structural_key"),
                )
                if str(value or "").strip()
            }
            processed_aliases = {
                str(value).strip()
                for value in (
                    payload.get("processed_voice_anchor_keys") or []
                )
                if str(value).strip()
            }
            failed_aliases = {
                str(value).strip()
                for value in (
                    payload.get("failed_voice_anchor_keys") or []
                )
                if str(value).strip()
            }
            if selected_aliases & processed_aliases:
                matching_transcripts = [
                    item
                    for item in (payload.get("transcribed_messages") or [])
                    if isinstance(item, dict)
                    and {
                        str(item.get("voice_anchor_stable_key") or "").strip(),
                        str(item.get("parent_voice_anchor_key") or "").strip(),
                    }
                    & selected_aliases
                ]
                payload["state"] = "voice_transcribe_completed"
                payload["processed_voice_anchor_keys"] = sorted(
                    selected_aliases & processed_aliases
                )
                payload["failed_voice_anchor_keys"] = []
                payload["transcribed_messages"] = (
                    matching_transcripts
                    or list(payload.get("transcribed_messages") or [])[:1]
                )
            elif selected_aliases & failed_aliases:
                payload.update(
                    {
                        "ok": False,
                        "state": "voice_transcribe_failed",
                        "error_code": next(
                            (
                                str(item.get("error_code") or "")
                                for item in (
                                    payload.get("item_action_outcomes") or []
                                )
                                if isinstance(item, dict)
                                and str(item.get("error_code") or "")
                            ),
                            "C2_VOICE_TRANSCRIBE_FAILED",
                        ),
                        "processed_voice_anchor_keys": [],
                        "failed_voice_anchor_keys": sorted(
                            selected_aliases & failed_aliases
                        ),
                        "transcribed_messages": [],
                    }
                )
        if (
            action_id
            and reserved_id
            and str(payload.get("state") or "")
            == "voice_transcribe_completed"
            and not payload.get(
            "confirmed_action_mapping"
            )
        ):
            future_payload = (
                self._contractual_message_payload(
                    dict(self.get_messages_payloads.pop(0))
                )
                if self.get_messages_payloads
                else self._contractual_message_payload(payload)
            )
            voice_observations = [
                item
                for item in (future_payload.get("observations") or [])
                if isinstance(item, dict)
                and item.get("message_type") == "voice"
            ]
            reported_anchors = {
                str(value).strip()
                for value in [
                    *(payload.get("processed_voice_anchor_keys") or []),
                    *(payload.get("failed_voice_anchor_keys") or []),
                ]
                if str(value).strip()
            }
            matched_voice_observations = []
            for item in voice_observations:
                source = (
                    item.get("source_message")
                    if isinstance(item.get("source_message"), dict)
                    else {}
                )
                aliases = {
                    str(value).strip()
                    for value in (
                        item.get("voice_anchor_key"),
                        item.get("parent_voice_anchor_key"),
                        source.get("voice_anchor_key"),
                        source.get("voice_anchor_stable_key"),
                        source.get("voice_anchor_structural_key"),
                        source.get("id"),
                    )
                    if str(value or "").strip()
                }
                if aliases & reported_anchors:
                    matched_voice_observations.append(item)
            if len(matched_voice_observations) == 1:
                voice_observations = matched_voice_observations
            post_observation_id = str(
                (voice_observations[0] if voice_observations else {}).get(
                    "observation_id"
                )
                or ""
            ).strip()
            payload.update(
                {
                    "canonical_voice_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                    "transcript_binding_status": (
                        "confirmed" if post_observation_id else "failed"
                    ),
                    "transcript_binding_method": (
                        "continuous_target_tracking"
                        if post_observation_id
                        else "none"
                    ),
                    "binding_candidate_count": (
                        1 if post_observation_id else 0
                    ),
                    "tracking_frame_ids": ([
                        str(
                            kwargs.get("pre_frame_id")
                            or "fixture-pre"
                        ),
                        "fixture-mid",
                        "fixture-post",
                    ] if post_observation_id else []),
                    "tracking_edges": ([
                        {
                            "from_frame_id": str(kwargs.get("pre_frame_id") or "fixture-pre"),
                            "from_observation_id": str(kwargs.get("selected_pre_observation_id") or ""),
                            "to_frame_id": "fixture-mid",
                            "to_observation_id": str(kwargs.get("selected_pre_observation_id") or ""),
                            "sender_role": selected_sender_role,
                            "message_type": "voice",
                            "structural_evidence": {"fixture": True},
                            "displacement_evidence": {"fixture": True},
                            "edge_candidate_count": 1,
                        },
                        {
                            "from_frame_id": "fixture-mid",
                            "from_observation_id": str(kwargs.get("selected_pre_observation_id") or ""),
                            "to_frame_id": "fixture-post",
                            "to_observation_id": post_observation_id,
                            "sender_role": selected_sender_role,
                            "message_type": "voice",
                            "structural_evidence": {"fixture": True},
                            "displacement_evidence": {"fixture": True},
                            "edge_candidate_count": 1,
                        },
                    ] if post_observation_id else []),
                    "matched_neighbor_pairs": [],
                    "native_source_message_id": None,
                    "confirmed_action_mapping": {
                        "canonical_action_id": action_id,
                        "reserved_worker_stable_id": reserved_id,
                        "selected_action_token": str(
                            kwargs.get("selected_action_token") or ""
                        ),
                        "pre_observation_id": str(
                            kwargs.get("selected_pre_observation_id") or ""
                        ),
                        "binding_confirmed": bool(post_observation_id),
                        "post_observation_id": post_observation_id,
                        "derived_observation_ids": [],
                    },
                }
            )
            payload["observations"] = list(
                future_payload.get("observations") or []
            )
            payload["messages"] = list(future_payload.get("messages") or [])
            payload["target_confirmation"] = {"ok": True}
            payload["final_frame_reusable"] = True
            payload.setdefault("action_phase", "confirmed")
            payload.setdefault("business_state", "completed")
            payload.setdefault("business_result_confirmed", True)
            payload.setdefault("ui_action_performed", True)
            self.last_message_payload = dict(payload)
        elif action_id and reserved_id and str(
            payload.get("state") or ""
        ) in {
            "voice_transcribe_click_failed",
            "voice_transcribe_failed",
            "voice_transcribe_ambiguous",
        }:
            ambiguous = str(payload.get("state") or "") == (
                "voice_transcribe_ambiguous"
            )
            future_payload = (
                self._contractual_message_payload(
                    dict(self.get_messages_payloads.pop(0))
                )
                if self.get_messages_payloads
                else self._contractual_message_payload(
                    dict(self.last_message_payload)
                )
            )
            selected_id = str(
                kwargs.get("selected_pre_observation_id") or ""
            ).strip()
            reported_anchors = {
                str(value).strip()
                for value in [
                    *(payload.get("processed_voice_anchor_keys") or []),
                    *(payload.get("failed_voice_anchor_keys") or []),
                ]
                if str(value).strip()
            }
            matched_voice_observations = []
            for item in future_payload.get("observations") or []:
                if not isinstance(item, dict) or item.get(
                    "message_type"
                ) != "voice":
                    continue
                source = (
                    item.get("source_message")
                    if isinstance(item.get("source_message"), dict)
                    else {}
                )
                aliases = {
                    str(value).strip()
                    for value in (
                        item.get("observation_id"),
                        item.get("voice_anchor_key"),
                        item.get("parent_voice_anchor_key"),
                        source.get("id"),
                        source.get("voice_anchor_key"),
                        source.get("voice_anchor_stable_key"),
                        source.get("voice_anchor_structural_key"),
                    )
                    if str(value or "").strip()
                }
                if selected_id in aliases or aliases & reported_anchors:
                    matched_voice_observations.append(item)
            post_observation_id = ""
            if not ambiguous and len(matched_voice_observations) == 1:
                post_observation_id = str(
                    matched_voice_observations[0].get("observation_id")
                    or ""
                ).strip()
            payload.update(
                {
                    "canonical_voice_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                    "transcript_binding_status": (
                        "ambiguous" if ambiguous else "failed"
                    ),
                    "transcript_binding_method": "none",
                    "binding_candidate_count": (
                        1 if post_observation_id else 0
                    ),
                    "tracking_frame_ids": ([
                        str(
                            kwargs.get("pre_frame_id")
                            or "fixture-pre"
                        ),
                        "fixture-failed-execute",
                        "fixture-failed-final",
                    ] if post_observation_id else []),
                    "tracking_edges": ([
                        {
                            "from_frame_id": str(
                                kwargs.get("pre_frame_id") or "fixture-pre"
                            ),
                            "from_observation_id": str(
                                kwargs.get("selected_pre_observation_id")
                                or ""
                            ),
                            "to_frame_id": "fixture-failed-execute",
                            "to_observation_id": str(
                                kwargs.get("selected_pre_observation_id")
                                or ""
                            ),
                            "sender_role": selected_sender_role,
                            "message_type": "voice",
                            "structural_evidence": {"fixture": True},
                            "displacement_evidence": {"fixture": True},
                            "edge_candidate_count": 1,
                        },
                        {
                            "from_frame_id": "fixture-failed-execute",
                            "from_observation_id": str(
                                kwargs.get("selected_pre_observation_id")
                                or ""
                            ),
                            "to_frame_id": "fixture-failed-final",
                            "to_observation_id": post_observation_id,
                            "sender_role": selected_sender_role,
                            "message_type": "voice",
                            "structural_evidence": {"fixture": True},
                            "displacement_evidence": {"fixture": True},
                            "edge_candidate_count": 1,
                        },
                    ] if post_observation_id else []),
                    "confirmed_action_mapping": {
                        "canonical_action_id": action_id,
                        "reserved_worker_stable_id": reserved_id,
                        "selected_action_token": str(
                            kwargs.get("selected_action_token") or ""
                        ),
                        "pre_observation_id": str(
                            kwargs.get("selected_pre_observation_id") or ""
                        ),
                        "binding_confirmed": bool(post_observation_id),
                        "post_observation_id": post_observation_id,
                        "derived_observation_ids": [],
                    },
                    "action_phase": (
                        "quarantined" if ambiguous else "failed"
                    ),
                    "ui_action_performed": True,
                }
            )
            if post_observation_id:
                payload["transcript_binding_method"] = (
                    "continuous_target_tracking"
                )
                payload["observations"] = list(
                    future_payload.get("observations") or []
                )
                payload["messages"] = list(
                    future_payload.get("messages") or []
                )
                self.last_message_payload = dict(payload)
            journal_path_value = kwargs.get("action_journal")
            if journal_path_value:
                update_action_journal_item(
                    Path(journal_path_value),
                    journal_item_id=action_id,
                    action_phase=(
                        "quarantined" if ambiguous else "failed"
                    ),
                    business_state="failed",
                    business_result_confirmed=False,
                    error_code=str(
                        payload.get("error_code")
                        or "C2_VOICE_TRANSCRIBE_FAILED"
                    ),
                    terminal_payload={
                        "state": (
                            "quarantined" if ambiguous else "failed"
                        )
                    },
                )
        elif action_id and reserved_id and str(
            payload.get("state") or ""
        ) == "voice_transcribe_cancelled":
            payload.update(
                {
                    "ok": True,
                    "state": "voice_action_cancelled_before_trigger",
                    "action_phase": "cancelled_before_trigger",
                    "ui_action_performed": False,
                }
            )
        elif (
            action_id
            and reserved_id
            and str(payload.get("error_code") or "")
            == "RPA_SIDECAR_TIMEOUT"
            and kwargs.get("action_journal")
        ):
            # A transport timeout cannot prove that the click did not occur.
            # Model the durable no-repeat barrier written by the real Sidecar.
            update_action_journal_item(
                Path(kwargs["action_journal"]),
                journal_item_id=action_id,
                action_phase="trigger_attempted",
                business_state="unknown",
                business_result_confirmed=False,
                error_code="RPA_SIDECAR_TIMEOUT",
            )
        if (
            str(payload.get("transcript_binding_method") or "")
            == "continuous_target_tracking"
            and not payload.get("tracking_frame_ids")
            and isinstance(payload.get("tracking_edges"), list)
            and payload["tracking_edges"]
        ):
            edges = payload["tracking_edges"]
            payload["tracking_frame_ids"] = [
                str(edges[0].get("from_frame_id") or ""),
                *[
                    str(edge.get("to_frame_id") or "")
                    for edge in edges
                    if isinstance(edge, dict)
                ],
            ]
        if action_id and reserved_id:
            tracking_frame_ids = payload.get("tracking_frame_ids")
            payload.setdefault("voice_action_stage", "execute")
            payload.setdefault("canonical_voice_action_id", action_id)
            payload.setdefault("reserved_worker_stable_id", reserved_id)
            payload.setdefault(
                "pre_frame_id", str(kwargs.get("pre_frame_id") or "")
            )
            payload.setdefault(
                "post_frame_id",
                str(
                    (
                        tracking_frame_ids[-1]
                        if isinstance(tracking_frame_ids, list)
                        and tracking_frame_ids
                        else "fixture-post-frame"
                    )
                    or "fixture-post-frame"
                ),
            )
            payload.setdefault(
                "selected_pre_observation_id",
                str(kwargs.get("selected_pre_observation_id") or ""),
            )
            payload.setdefault(
                "selected_action_token",
                str(kwargs.get("selected_action_token") or ""),
            )
            payload.setdefault(
                "selected_target_fingerprint",
                str(kwargs.get("selected_target_fingerprint") or ""),
            )
        return payload


class TaskRunnerTest(unittest.TestCase):
    @staticmethod
    def _ai_send_observation(
        observation_id: str,
        *,
        sender_role: str,
        content: str,
        stable_id: str = "",
        native_id: str = "",
        visual_id: str = "",
    ) -> dict:
        observation = {
            "schema_version": 3,
            "observation_id": observation_id,
            "row_kind": "text_bubble",
            "sender_role": sender_role,
            "sender_role_source": "same_row_avatar",
            "message_type": "text",
            "voice_state": "not_voice",
            "content_clean": content,
            "source_message": {
                "id": observation_id,
                "type": "text",
                "sender_role": sender_role,
            },
        }
        if stable_id:
            observation["_worker_stable_id"] = stable_id
        if native_id:
            observation["native_source_message_id"] = native_id
            observation["source_message"][
                "native_source_message_id"
            ] = native_id
        if visual_id:
            observation["frame_visual_id"] = visual_id
            observation["source_message"]["frame_visual_id"] = visual_id
        return observation

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

    def _prepare_ai_send_receipt_fixture(
        self,
        *,
        conversation_id: str,
        pre_observations: list[dict],
        next_sequence: int,
        state_version: int = 4,
        reply_text: str,
    ):
        runner, _states = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key="",
            display_name="CJAI0001",
            remark_code="CJAI0001",
            authorization_revision="revision-ai-send-receipt",
            raw={
                "identity_checkpoint": identity_checkpoint(
                    next_sequence_floor=next_sequence
                )
            },
        )
        state_key = f"message_identity:{conversation_id}"
        save_c2_state(
            state_key,
            {
                "version": state_version,
                "next_sequence": next_sequence,
                "sequence_reservations": {},
                "sentinel": "must-survive",
            },
        )
        action_id = f"reply-{conversation_id}"
        reserved_id = runner._reserve_worker_sequence(
            target,
            reservation_key=f"send-action:{action_id}",
        )
        committed_ids = {
            str(item["observation_id"]): str(item["_worker_stable_id"])
            for item in pre_observations
            if item.get("_worker_stable_id")
        }
        pre_sequence = build_pre_action_identity_sequence(
            pre_observations,
            committed_ids=committed_ids,
        )
        pre_frame_id = f"send-pre:{conversation_id}"
        bridge = runner.bridge
        journal_path = bridge.send_transaction_journal_path(action_id)
        initialize_action_journal(
            journal_path,
            action_kind="send",
            transaction_id=action_id,
            conversation_id=conversation_id,
            items=[
                {
                    "journal_item_id": action_id,
                    "physical_anchor_keys": [],
                }
            ],
            pre_action_identity_sequence=pre_sequence,
            pre_frame_id=pre_frame_id,
            canonical_action_id=action_id,
            reserved_worker_stable_id=reserved_id,
        )
        runner._record_possible_ai_send(
            target=target,
            reply_action_id=action_id,
            reply_text=reply_text,
            reply_text_hash=runner._reply_text_hash(reply_text),
            reserved_worker_stable_id=reserved_id,
            pre_frame_id=pre_frame_id,
            pre_action_identity_sequence=pre_sequence,
        )
        return runner, bridge, target, action_id, reserved_id, state_key

    @staticmethod
    def _confirmed_send_sidecar_result(
        *,
        observations: list[dict],
        confirmed_observation_id: str,
        run_id: str,
    ) -> dict:
        confirmed = next(
            dict(item)
            for item in observations
            if item.get("observation_id") == confirmed_observation_id
        )
        return {
            "sidecar_run_id": run_id,
            "send_result": {
                "ok": True,
                "confirmed": True,
                "result": "sent",
                "sent_confirmation": {
                    "ok": True,
                    "attempt": 1,
                    "confirmed_observation": confirmed,
                    "snapshot": {"observations": observations},
                },
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
        observations = [
            {
                "observation_id": "voice-alias-a",
                "row_kind": "voice_bubble",
                "message_type": "voice",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "voice_anchor_structural_key": "voice-structural:a",
                "parent_voice_anchor_key": "voice-parent:shared",
            },
            {
                "observation_id": "voice-alias-b",
                "row_kind": "voice_bubble",
                "message_type": "voice",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "voice_anchor_structural_key": "voice-structural:b",
                "parent_voice_anchor_key": "voice-parent:shared",
            },
        ]

        collapsed = collapse_same_frame_voice_aliases(
            observations,
        )

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(
            collapsed[0]["_voice_action_anchor_keys"],
            [
                "voice-parent:shared",
                "voice-structural:a",
                "voice-structural:b",
            ],
        )
        self.assertEqual(collapsed[0]["sender_role"], "customer")

    def test_distinct_physical_voices_remain_distinct(self):
        observations = [
            {
                "observation_id": "voice-distinct-a",
                "row_kind": "voice_bubble",
                "message_type": "voice",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "voice_anchor_structural_key": "voice-structural:a",
            },
            {
                "observation_id": "voice-distinct-b",
                "row_kind": "voice_bubble",
                "message_type": "voice",
                "voice_state": "untranscribed",
                "sender_role": "customer",
                "voice_anchor_structural_key": "voice-structural:b",
            },
        ]

        collapsed = collapse_same_frame_voice_aliases(
            observations,
        )

        self.assertEqual(len(collapsed), 2)

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
            conn.execute(
                "DELETE FROM client_settings WHERE key = 'runtime_control_v1'"
            )
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
                journal_item_id=task.id,
                action_phase="trigger_attempted",
                business_state="invite_confirm_click_starting",
            )
            observed_phases.append(action_journal_phase(journal_path))
            update_action_journal_item(
                journal_path,
                journal_item_id=task.id,
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

    def test_pre_send_refresh_exception_finishes_telemetry_as_failed(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-pre-send-telemetry-failure",
            rpa_session_key="wx:rpa:v1:pre-send-telemetry-failure",
            display_name="CJPRE999",
            remark_code="CJPRE999",
            authorization_revision="revision-pre-send-telemetry-failure",
            process_run_id="42f76212-1bad-4b5f-b88e-7cf48f4e0dd4",
        )
        timer = Mock()

        with patch(
            "chejin_worker_client.task_runner.StageTimer",
            return_value=timer,
        ), patch(
            "chejin_worker_client.task_runner.schedule_stage_event_upload"
        ) as schedule_upload, patch.object(
            runner,
            "_read_one_wechat_target_impl",
            side_effect=RuntimeError("pre-send read crashed"),
        ), self.assertRaisesRegex(RuntimeError, "pre-send read crashed"):
            runner._read_one_wechat_target(
                binding,
                target,
                operation_phase="pre_send_refresh",
            )

        timer.finish.assert_called_once_with(
            status="failed",
            error_code="RuntimeError",
        )
        schedule_upload.assert_called_once_with(api, binding)

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
            pre_frame_id="frame-voice-before-crash",
            prepare_evidence={
                "pre_frame_id": "frame-voice-before-crash",
                "selected_pre_observation_id": "voice-before-crash",
                "selected_action_token": "token-voice-before-crash",
                "selected_target_fingerprint": (
                    "fingerprint-voice-before-crash"
                ),
                "ui_action_performed": False,
            },
            items=[
                {
                    "journal_item_id": "voice-triggered-before-crash",
                    "physical_anchor_keys": ["voice-anchor-1"],
                }
            ],
        )
        update_action_journal_item(
            path,
            journal_item_id="voice-triggered-before-crash",
            action_phase="trigger_attempted",
        )

        unresolved = runner._recover_physical_action_journals(target)

        ledger = load_c2_ledger_entry(
            target.conversation_id,
            "voice-triggered-before-crash",
        )
        self.assertIsNone(ledger)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(
            unresolved[0]["reason"],
            "action_triggered_without_confirmed_post_alignment",
        )
        self.assertTrue(path.exists())
        self.assertEqual(action_journal_phase(path), "quarantined")
        repeated = runner._recover_physical_action_journals(target)
        self.assertEqual(len(repeated), 1)
        self.assertIsNone(
            load_c2_ledger_entry(
                target.conversation_id,
                "voice-triggered-before-crash",
            )
        )
        self.assertEqual(action_journal_phase(path), "quarantined")

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
                    "journal_item_id": "image-not-attempted",
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
        action_id = "voice-sidecar-crashed"
        reserved_id = "worker-message-sidecar-crashed"

        def crash_after_trigger(*_args, flow_outcomes, **_kwargs):
            path = runner._start_irreversible_action_journal(
                action_kind="voice",
                target=target,
                items=[
                    {
                        "journal_item_id": action_id,
                        "action_local_id": action_id,
                        "physical_anchor_keys": ["voice-anchor-crashed"],
                    }
                ],
                flow_outcomes=flow_outcomes,
                transaction_id=action_id,
                pre_action_identity_sequence=[
                    {
                        "identity_state": "selected_action",
                        "canonical_action_id": action_id,
                        "reserved_worker_stable_id": reserved_id,
                        "pre_observation_id": "voice-before-crash",
                        "pre_sequence_index": 0,
                        "sender_role": "customer",
                        "message_type": "voice",
                    }
                ],
                pre_frame_id="frame-before-sidecar-crash",
                reserved_worker_stable_id=reserved_id,
                prepare_evidence={
                    "pre_frame_id": "frame-before-sidecar-crash",
                    "selected_pre_observation_id": "voice-before-crash",
                    "selected_action_token": "token-before-crash",
                    "selected_target_fingerprint": (
                        "fingerprint-before-crash"
                    ),
                    "candidate_group_count": 1,
                },
            )
            created_paths.append(path)
            update_action_journal_item(
                path,
                journal_item_id=action_id,
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

        self.assertTrue(created_paths)
        self.assertTrue(created_paths[0].exists())
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, action_id)
        )
        unresolved = runner._recover_physical_action_journals(target)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(
            unresolved[0]["reason"],
            "action_triggered_without_confirmed_post_alignment",
        )
        self.assertEqual(
            action_journal_phase(created_paths[0]), "quarantined"
        )
        self.assertTrue(created_paths[0].exists())

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

    def test_telemetry_sqlite_failure_does_not_change_add_friend_actions(self):
        task = Task(
            id="task-telemetry-failure",
            task_type="add_friend",
            status="pending",
            phone="13800000000",
            process_run_id="11111111-1111-4111-8111-111111111111",
        )
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=True,
                result_code="invite_sent",
                message="已发送添加通讯录邀请",
            )
        )
        runner, seen = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        with patch(
            "chejin_worker_client.telemetry._connect",
            side_effect=OSError("telemetry sqlite unavailable"),
        ):
            runner.tick_once()

        self.assertEqual(
            [step.current_step for step in seen["steps"]],
            ["checking_rpa", "invite_sent"],
        )
        self.assertIn("complete_invite_sent:task-telemetry-failure", api.events)
        self.assertEqual([item.id for item in bridge.tasks], [task.id])
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
        if "pull" not in api.events:
            runner._pull_and_execute(binding)
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

    def test_add_friend_sidecar_cancel_check_tracks_inflight_stop_and_ui_lease(self):
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
        self.assertTrue(
            runner._start_inflight_flow(
                binding,
                flow_id=task.id,
                flow_kind="task",
            )
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
        self.assertEqual(
            bridge.calibration_prepare_calls,
            1,
            "C1 must pass one startup calibration transaction gate",
        )
        self.assertTrue(callable(bridge.add_friend_cancel_check))
        self.assertFalse(bridge.add_friend_cancel_check())
        binding.run_status = "paused"
        self.assertFalse(bridge.add_friend_cancel_check())
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

    def test_preclick_layout_failure_does_not_pause_worker_after_failed_report(self):
        task = Task(id="task-layout", task_type="add_friend", status="pending", phone="13800000000")
        api = FakeApi(task)
        bridge = FakeBridge(
            RpaResult(
                ok=False,
                error_code="WECHAT_UI_LAYOUT_UNRESOLVED",
                failure_step="window_layout_calibration",
                message="当前帧动态布局未解析",
            )
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")
        runner.binding = binding

        runner.tick_once()

        self.assertIn("fail:WECHAT_UI_LAYOUT_UNRESOLVED:window_layout_calibration", api.events)
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
        self.assertTrue(load_runtime_control()["pause_requested"])
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

    def test_pause_drains_registered_flow_but_emergency_stop_cancels_it(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-drain",
            worker_token="token",
            client_instance_id="client-drain",
            run_status="running",
        )
        runner.binding = binding
        self.assertTrue(
            runner._start_inflight_flow(
                binding,
                flow_id="task-drain-1",
                flow_kind="chat_reply",
            )
        )

        self.assertTrue(runner.set_run_status("paused"))
        self.assertFalse(runner._can_start_new_flow(binding))
        self.assertTrue(
            runner._can_continue_inflight_flow("task-drain-1")
        )
        control = load_runtime_control()
        self.assertTrue(control["pause_requested"])
        self.assertEqual(control["inflight_flow_id"], "task-drain-1")

        from chejin_worker_client.emergency_stop import trigger_emergency_stop

        trigger_emergency_stop(reason="test", origin="unit")
        self.assertFalse(
            runner._can_continue_inflight_flow("task-drain-1")
        )

    def test_pause_during_image_keeps_exact_read_flow_until_image_terminal(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-image-drain",
            worker_token="token",
            client_instance_id="client-image-drain",
            run_status="running",
        )
        runner.binding = binding
        read_run_id = "read-image-drain"
        self.assertTrue(
            runner._start_inflight_flow(
                binding,
                flow_id=read_run_id,
                flow_kind="c2_read",
            )
        )
        self.assertTrue(runner.set_run_status("paused"))
        self.assertTrue(runner._can_continue_inflight_flow(read_run_id))
        target = WechatReadTarget(
            conversation_id="conv-image-drain",
            rpa_session_key="wx:image-drain",
            display_name="CJIMGD01",
            remark_code="CJIMGD01",
            authorization_revision="revision-image-drain",
        )
        sidecar_payload = {
            "frame_id": "frame-image-drain",
            "authoritative_frame_source": "initial_read",
            "observations": [
                {
                    "schema_version": 3,
                    "observation_id": "image-observation-drain",
                    "frame_visual_id": "visual-image-drain",
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "image",
                    "voice_state": "not_voice",
                    "item_state": "discovered",
                    "_worker_stable_id": "worker-message-1",
                    "_worker_identity_scope": "current_read_provisional",
                    "image_physical_anchor": {
                        "sender_role": "customer",
                        "bubble_visual_fingerprint": (
                            "dhash64:0123456789abcdef"
                        ),
                    },
                    "bubble_rect": [420, 180, 650, 320],
                    "source_message": {"id": "image-drain", "type": "image"},
                }
            ],
        }

        def settle_image(*_args, **kwargs):
            self.assertFalse(kwargs["cancel_check"]())
            return {
                "state": "completed",
                "action_phase": "confirmed",
                "business_state": "completed",
                "business_result_confirmed": True,
                "customer_image_understanding": {
                    "schema_version": 1,
                    "vision_summary": "车辆外观图",
                },
                "visual_bridge_input": {"summary": "车辆外观图"},
                "transaction": {
                    "action_phase": "confirmed",
                    "slot_identity_confirmed": True,
                    "image_sha256": "a" * 64,
                },
                "diagnostics": {"events": [], "image_persisted": False},
            }

        with patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            side_effect=settle_image,
        ) as vision:
            _payload, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
                cancel_check=lambda: not runner._can_continue_inflight_flow(
                    read_run_id
                ),
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id=read_run_id
                ),
            )

        self.assertEqual(vision.call_count, 1)
        self.assertEqual(stats["completed"], 1)
        self.assertFalse(runner._can_start_new_flow(binding))

    def test_restart_draining_flow_only_finishes_persisted_settlement(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-restart-drain",
            worker_token="token",
            client_instance_id="client-restart-drain",
            run_status="paused",
        )
        runner.binding = binding
        begin_runtime_flow("read-restart-drain", "c2_read")
        request_runtime_pause()
        runner._restart_recovery_flow_id = "read-restart-drain"
        api.inflight_flow_id = "read-restart-drain"
        api.inflight_flow_state = {
            "status": "draining",
            "flow_id": "read-restart-drain",
            "flow_kind": "c2_read",
            "registered_at": "2026-08-14T00:00:00+00:00",
            "pause_requested_at": "2026-08-14T00:00:01+00:00",
        }
        runner._backend_inflight_flow_state = dict(
            api.inflight_flow_state
        )
        save_c2_state(
            "inflight_finish_receipt:read-restart-drain",
            {
                "terminal_kind": "read_confirmed",
                "conversation_id": "conv-restart-drain",
                "error_code": None,
            },
        )

        runner._finish_restart_recovery_flow_if_settled(binding)

        self.assertEqual(
            api.inflight_flow_events,
            [
                "finish:read-restart-drain:read_confirmed:conv-restart-drain:"
            ],
        )
        self.assertIsNone(load_runtime_control()["inflight_flow_id"])
        self.assertEqual(bridge.locate_payloads, [])
        self.assertEqual(bridge.sent_replies, [])

    def test_restart_triggered_voice_recovers_gate_finishes_flow_and_pulls(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-restart-voice-journal",
            rpa_session_key="wx:rpa:v1:restart-voice-journal",
            display_name="CJVOICE09",
            remark_code="CJVOICE09",
            read_reason="recoverable_hold",
            authorization_revision="revision-restart-voice-journal",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-restart-voice-journal",
            worker_token="token",
            client_instance_id="client-restart-voice-journal",
            run_status="running",
        )
        runner.binding = binding
        flow_id = "read-restart-voice-journal"
        action_id = "voice-restart-triggered"
        begin_runtime_flow(flow_id, "c2_read")
        runner._restart_recovery_flow_id = flow_id
        api.inflight_flow_id = flow_id
        api.inflight_flow_state = {
            "status": "active",
            "flow_id": flow_id,
            "flow_kind": "c2_read",
        }
        runner._backend_inflight_flow_state = dict(api.inflight_flow_state)
        save_c2_state(
            f"inflight_finish_receipt:{flow_id}",
            {
                "terminal_kind": "read_confirmed",
                "conversation_id": target.conversation_id,
                "error_code": "C2_VOICE_EXECUTE_INTERRUPTED",
            },
        )
        journal_path = action_journal_path("voice", action_id)
        initialize_action_journal(
            journal_path,
            action_kind="voice",
            transaction_id=action_id,
            conversation_id=target.conversation_id,
            origin_read_run_id=flow_id,
            canonical_action_id=action_id,
            reserved_worker_stable_id="worker-message-9",
            pre_frame_id="voice-frame-before-restart",
            pre_action_identity_sequence=[
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": "worker-message-9",
                    "pre_observation_id": "voice-before-restart",
                    "pre_sequence_index": 0,
                    "sender_role": "customer",
                    "message_type": "voice",
                }
            ],
            prepare_evidence={
                # Model the 0.9.28 journal that caused the UAT incident: the
                # old record has physical prepare evidence but no duplicated
                # target authorization envelope. Restart must resolve it from
                # the existing lightweight backend authorization endpoint.
                "pre_frame_id": "voice-frame-before-restart",
                "selected_pre_observation_id": "voice-before-restart",
                "selected_action_token": "voice-token-before-restart",
                "selected_target_fingerprint": (
                    "voice-fingerprint-before-restart"
                ),
                "candidate_group_count": 1,
            },
            items=[
                {
                    "journal_item_id": action_id,
                    "physical_anchor_keys": ["voice-anchor-before-restart"],
                }
            ],
        )
        update_action_journal_item(
            journal_path,
            journal_item_id=action_id,
            action_phase="trigger_attempted",
            business_state="failed",
            business_result_confirmed=False,
            error_code="C2_VOICE_EXECUTE_INTERRUPTED",
        )

        runner.tick_once()

        self.assertEqual(action_journal_phase(journal_path), "quarantined")
        self.assertTrue(journal_path.exists())
        self.assertIsNone(load_runtime_control()["inflight_flow_id"])
        self.assertIsNone(runner._restart_recovery_flow_id)
        self.assertIn(
            f"finish:{flow_id}:read_confirmed:{target.conversation_id}:"
            "C2_VOICE_EXECUTE_INTERRUPTED",
            api.inflight_flow_events,
        )
        self.assertIn("pull", api.events)
        self.assertLess(
            api.events.index(f"read_authorization:{target.conversation_id}"),
            api.events.index("ingest:0"),
        )
        self.assertLess(
            api.events.index("ingest:0"),
            api.events.index("pull"),
        )
        self.assertEqual(bridge.locate_payloads, [])
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertEqual(bridge.sent_replies, [])

    def test_restart_triggered_image_recovers_gate_finishes_flow_and_pulls(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-restart-image-journal",
            rpa_session_key="wx:rpa:v1:restart-image-journal",
            display_name="CJIMAGE09",
            remark_code="CJIMAGE09",
            read_reason="recoverable_hold",
            authorization_revision="revision-restart-image-journal",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-restart-image-journal",
            worker_token="token",
            client_instance_id="client-restart-image-journal",
            run_status="running",
        )
        runner.binding = binding
        flow_id = "read-restart-image-journal"
        action_id = "image-restart-triggered"
        begin_runtime_flow(flow_id, "c2_read")
        runner._restart_recovery_flow_id = flow_id
        api.inflight_flow_id = flow_id
        api.inflight_flow_state = {
            "status": "active",
            "flow_id": flow_id,
            "flow_kind": "c2_read",
        }
        runner._backend_inflight_flow_state = dict(api.inflight_flow_state)
        save_c2_state(
            f"inflight_finish_receipt:{flow_id}",
            {
                "terminal_kind": "read_confirmed",
                "conversation_id": target.conversation_id,
                "error_code": "C2_IMAGE_EXECUTE_INTERRUPTED",
            },
        )
        journal_path = action_journal_path("image", action_id)
        initialize_action_journal(
            journal_path,
            action_kind="image",
            transaction_id=action_id,
            conversation_id=target.conversation_id,
            origin_read_run_id=flow_id,
            canonical_action_id=action_id,
            reserved_worker_stable_id="worker-message-10",
            pre_frame_id="image-frame-before-restart",
            pre_action_identity_sequence=[
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": "worker-message-10",
                    "pre_observation_id": "image-before-restart",
                    "pre_sequence_index": 0,
                    "sender_role": "customer",
                    "message_type": "image",
                }
            ],
            prepare_evidence={
                "pre_frame_id": "image-frame-before-restart",
                "selected_pre_observation_id": "image-before-restart",
                "selected_action_token": "image-token-before-restart",
                "selected_target_fingerprint": (
                    "image-fingerprint-before-restart"
                ),
                "candidate_group_count": 1,
            },
            items=[
                {
                    "journal_item_id": action_id,
                    "physical_anchor_keys": ["image-anchor-before-restart"],
                }
            ],
        )
        update_action_journal_item(
            journal_path,
            journal_item_id=action_id,
            action_phase="trigger_attempted",
            business_state="failed",
            business_result_confirmed=False,
            error_code="C2_IMAGE_EXECUTE_INTERRUPTED",
        )

        with patch(
            "chejin_worker_client.omniauto_vision.process_image_slot"
        ) as vision:
            runner.tick_once()

        recovered_journal = read_action_journal(journal_path)
        recovered_item = recovered_journal["items"][action_id]
        self.assertEqual(recovered_item["action_phase"], "quarantined")
        self.assertTrue(
            recovered_item["terminal_payload"]["identity_gate_reported"]
        )
        self.assertIsNone(load_runtime_control()["inflight_flow_id"])
        self.assertIsNone(runner._restart_recovery_flow_id)
        self.assertIn("pull", api.events)
        self.assertLess(
            api.events.index(f"read_authorization:{target.conversation_id}"),
            api.events.index("ingest:0"),
        )
        self.assertLess(api.events.index("ingest:0"), api.events.index("pull"))
        self.assertEqual(bridge.locate_payloads, [])
        self.assertEqual(bridge.message_reads, [])
        vision.assert_not_called()
        self.assertEqual(bridge.sent_replies, [])

    def test_restart_without_finish_receipt_keeps_draining_flow(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-restart-no-receipt",
            worker_token="token",
            client_instance_id="client-restart-no-receipt",
            run_status="paused",
        )
        runner.binding = binding
        begin_runtime_flow("read-no-receipt", "c2_read")
        request_runtime_pause()
        runner._restart_recovery_flow_id = "read-no-receipt"
        api.inflight_flow_id = "read-no-receipt"
        api.inflight_flow_state = {
            "status": "draining",
            "flow_id": "read-no-receipt",
            "flow_kind": "c2_read",
        }
        runner._backend_inflight_flow_state = dict(api.inflight_flow_state)

        runner._finish_restart_recovery_flow_if_settled(binding)

        self.assertEqual(api.inflight_flow_events, [])
        self.assertEqual(
            load_runtime_control()["inflight_flow_id"], "read-no-receipt"
        )

    def test_pending_same_flow_outbox_blocks_inflight_finish(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-finish-pending-outbox",
            worker_token="token",
            client_instance_id="client-finish-pending-outbox",
            run_status="paused",
        )
        runner.binding = binding
        begin_runtime_flow("read-pending-outbox", "c2_read")
        api.inflight_flow_id = "read-pending-outbox"
        api.inflight_flow_state = {
            "status": "draining",
            "flow_id": "read-pending-outbox",
            "flow_kind": "c2_read",
        }
        runner._backend_inflight_flow_state = dict(api.inflight_flow_state)
        enqueue_c2_outbox(
            {
                "read_run_id": "read-pending-outbox",
                "conversation_id": "conv-pending-outbox",
                "authorization_revision": "revision-pending-outbox",
                "messages": [],
                "evidence": {},
            }
        )

        with self.assertRaisesRegex(
            RuntimeError, "RUNTIME_INFLIGHT_C2_OUTBOX_PENDING"
        ):
            runner._finish_inflight_flow(
                binding,
                "read-pending-outbox",
                terminal_kind="read_confirmed",
                conversation_id="conv-pending-outbox",
            )

        self.assertEqual(api.inflight_flow_events, [])
        self.assertEqual(
            load_runtime_control()["inflight_flow_id"],
            "read-pending-outbox",
        )

    def test_failed_before_message_action_rejects_confirmed_same_flow_outbox(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-finish-confirmed-outbox",
            worker_token="token",
            client_instance_id="client-finish-confirmed-outbox",
            run_status="paused",
        )
        runner.binding = binding
        begin_runtime_flow("read-confirmed-outbox", "c2_read")
        api.inflight_flow_id = "read-confirmed-outbox"
        api.inflight_flow_state = {
            "status": "draining",
            "flow_id": "read-confirmed-outbox",
            "flow_kind": "c2_read",
        }
        runner._backend_inflight_flow_state = dict(api.inflight_flow_state)
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": "read-confirmed-outbox",
                "conversation_id": "conv-confirmed-outbox",
                "authorization_revision": "revision-confirmed-outbox",
                "messages": [],
                "evidence": {},
            }
        )
        transition_c2_outbox(outbox_id, status="confirmed")

        with self.assertRaisesRegex(
            RuntimeError, "RUNTIME_INFLIGHT_PRE_ACTION_OUTBOX_CONFLICT"
        ):
            runner._finish_inflight_flow(
                binding,
                "read-confirmed-outbox",
                terminal_kind="failed_before_message_action",
                conversation_id="conv-confirmed-outbox",
                error_code="C2_LOCATE_FAILED",
            )

        self.assertEqual(api.inflight_flow_events, [])

    def test_sidecar_read_failure_finishes_as_read_failed_no_fact(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "ok": False,
                "state": "messages_ocr_failed",
                "error_code": "C2_MESSAGE_OCR_FAILED",
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-read-no-fact",
            worker_token="token",
            client_instance_id="client-read-no-fact",
            run_status="running",
        )
        runner.binding = binding
        target = WechatReadTarget(
            conversation_id="conv-read-no-fact",
            rpa_session_key="wx:read-no-fact",
            display_name="CJNOFACT",
            remark_code="CJNOFACT",
            authorization_revision="revision-read-no-fact",
            read_reason="recall_precheck",
            raw={"identity_checkpoint": identity_checkpoint()},
        )

        result = runner._read_one_wechat_target(binding, target)

        self.assertFalse(result["ok"])
        self.assertEqual(
            bridge.calibration_prepare_calls,
            1,
            "C4 recall read must reuse one startup calibration transaction gate",
        )
        self.assertEqual(result["error_code"], "C2_MESSAGE_OCR_FAILED")
        self.assertEqual(len(api.inflight_flow_events), 2)
        self.assertTrue(
            api.inflight_flow_events[0].startswith("start:c2_read:read-")
        )
        self.assertIn(
            ":read_failed_no_fact:conv-read-no-fact:C2_MESSAGE_OCR_FAILED",
            api.inflight_flow_events[1],
        )
        self.assertIsNone(load_runtime_control()["inflight_flow_id"])

    def test_task_terminal_waits_for_nested_c2_outbox(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        binding = Binding(
            worker_id="worker-task-nested-outbox",
            worker_token="token",
            client_instance_id="client-task-nested-outbox",
            run_status="paused",
        )
        runner.binding = binding
        flow_id = "task-nested-c2-outbox"
        begin_runtime_flow(flow_id, "chat_reply")
        api.inflight_flow_id = flow_id
        api.inflight_flow_state = {
            "status": "draining",
            "flow_id": flow_id,
            "flow_kind": "chat_reply",
        }
        runner._backend_inflight_flow_state = dict(api.inflight_flow_state)
        enqueue_c2_outbox(
            {
                "read_run_id": flow_id,
                "conversation_id": "conv-task-nested-outbox",
                "authorization_revision": "revision-task-nested-outbox",
                "messages": [],
                "evidence": {},
            }
        )

        with self.assertRaisesRegex(
            RuntimeError, "RUNTIME_INFLIGHT_C2_OUTBOX_PENDING"
        ):
            runner._finish_inflight_flow(
                binding,
                flow_id,
                terminal_kind="task_terminal",
            )

        self.assertEqual(api.inflight_flow_events, [])
        self.assertEqual(load_runtime_control()["inflight_flow_id"], flow_id)

    def test_task_terminal_waits_for_nested_c2_ledger_and_journal(self):
        for artifact_kind, expected_error in (
            ("ledger", "RUNTIME_INFLIGHT_C2_LEDGER_PENDING"),
            ("journal", "RUNTIME_INFLIGHT_C2_ACTION_JOURNAL_PENDING"),
            ("physical_journal", "RUNTIME_INFLIGHT_ACTION_JOURNAL_PENDING"),
        ):
            with self.subTest(artifact_kind=artifact_kind):
                api = FakeApi(None)
                runner, _ = self.make_runner(
                    api,
                    FakeBridge(
                        RpaResult(
                            ok=True,
                            result_code="unused",
                            message="unused",
                        )
                    ),
                )
                binding = Binding(
                    worker_id=f"worker-task-{artifact_kind}",
                    worker_token="token",
                    client_instance_id=f"client-task-{artifact_kind}",
                    run_status="paused",
                )
                runner.binding = binding
                flow_id = f"task-nested-c2-{artifact_kind}"
                begin_runtime_flow(flow_id, "chat_reply")
                api.inflight_flow_id = flow_id
                api.inflight_flow_state = {
                    "status": "draining",
                    "flow_id": flow_id,
                    "flow_kind": "chat_reply",
                }
                runner._backend_inflight_flow_state = dict(
                    api.inflight_flow_state
                )
                if artifact_kind == "ledger":
                    save_c2_ledger_terminal(
                        conversation_id=f"conv-{artifact_kind}",
                        source_message_key=f"source-{artifact_kind}",
                        origin_read_run_id=flow_id,
                        dedupe_key=f"dedupe-{artifact_kind}",
                        message_type="text",
                        terminal_state="completed",
                        ingest_state="waiting",
                        result={"state": "completed"},
                    )
                else:
                    if artifact_kind == "journal":
                        checkpoint_c2_action_outcomes(
                            flow_id=f"action-{artifact_kind}",
                            conversation_id=f"conv-{artifact_kind}",
                            origin_read_run_id=flow_id,
                            outcomes=[
                                {
                                    "source_message_key": f"source-{artifact_kind}",
                                    "result": "failed",
                                    "evidence": {"action_kind": "image"},
                                    "terminal_payload": {"state": "failed"},
                                }
                            ],
                        )
                    else:
                        journal_path = action_journal_path(
                            "image", f"action-{artifact_kind}"
                        )
                        initialize_action_journal(
                            journal_path,
                            action_kind="image",
                            transaction_id=f"action-{artifact_kind}",
                            conversation_id=f"conv-{artifact_kind}",
                            origin_read_run_id=flow_id,
                            items=[
                                {
                                    "journal_item_id": f"source-{artifact_kind}",
                                    "physical_anchor_keys": ["image-anchor"],
                                }
                            ],
                        )

                with self.assertRaisesRegex(RuntimeError, expected_error):
                    runner._finish_inflight_flow(
                        binding,
                        flow_id,
                        terminal_kind="task_terminal",
                    )

                self.assertEqual(api.inflight_flow_events, [])
                if artifact_kind == "physical_journal":
                    remove_action_journal(journal_path)
                finish_runtime_flow(flow_id)

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

    def test_start_accepting_does_not_run_a_second_window_verification(self):
        api = FakeApi(None)
        bridge = FakeBridge(RpaResult(ok=True, result_code="unused", message="unused"))
        bridge.preflight_payload = {
            "ok": False,
            "error_code": "WECHAT_UI_WINDOW_NORMALIZATION_FAILED",
            "reason": "screen_work_area_too_small",
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="paused",
        )
        runner.binding = binding

        self.assertTrue(runner.set_run_status("running"))
        self.assertEqual(bridge.preflight_calls, 0)
        self.assertEqual(api.run_status_updates, ["running"])
        self.assertEqual(binding.run_status, "running")

    def test_task_safe_wake_coalesces_repeated_events_into_one_followup_tick(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        observed: list[int] = []

        def tick_once() -> None:
            observed.append(len(observed) + 1)
            if len(observed) == 1:
                for _ in range(8):
                    runner._task_wake_event.set()
            else:
                runner.stop_event.set()

        runner.tick_once = tick_once  # type: ignore[method-assign]
        runner.poll_interval_seconds = 60

        runner._loop()

        self.assertEqual(observed, [1, 2])

    def test_task_safe_wake_never_bypasses_runtime_gates(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.binding = binding

        with (
            patch(
                "chejin_worker_client.task_runner.load_runtime_control",
                return_value={
                    "pause_requested": False,
                    "inflight_flow_id": None,
                },
            ),
            patch(
                "chejin_worker_client.task_runner.lock_summary",
                return_value={"locked": False},
            ),
        ):
            self.assertTrue(runner._request_task_wake_if_safe(reason="idle"))
            runner._task_wake_event.clear()

            runner.current_task = self.make_chat_reply_task(task_id="busy")
            self.assertFalse(
                runner._request_task_wake_if_safe(reason="current_task")
            )
            runner.current_task = None

            runner.current_ui_lock = object()  # type: ignore[assignment]
            self.assertFalse(
                runner._request_task_wake_if_safe(reason="ui_lock")
            )
            runner.current_ui_lock = None

            binding.run_status = "paused"
            self.assertFalse(
                runner._request_task_wake_if_safe(reason="paused")
            )
            binding.run_status = "running"

            with patch(
                "chejin_worker_client.task_runner.load_runtime_control",
                return_value={
                    "pause_requested": True,
                    "inflight_flow_id": None,
                },
            ):
                self.assertFalse(
                    runner._request_task_wake_if_safe(reason="pause_requested")
                )

            with patch(
                "chejin_worker_client.task_runner.load_runtime_control",
                return_value={
                    "pause_requested": False,
                    "inflight_flow_id": "flow-1",
                },
            ):
                self.assertFalse(
                    runner._request_task_wake_if_safe(reason="inflight")
                )

            with patch(
                "chejin_worker_client.task_runner.emergency_stop_requested",
                return_value=True,
            ):
                self.assertFalse(
                    runner._request_task_wake_if_safe(reason="emergency")
                )

            runner.can_pull_tasks = lambda: False
            self.assertFalse(
                runner._request_task_wake_if_safe(reason="cannot_pull")
            )
            runner.can_pull_tasks = lambda: True

            runner.task_lock.acquire()
            try:
                self.assertFalse(
                    runner._request_task_wake_if_safe(reason="task_lock")
                )
            finally:
                runner.task_lock.release()

            with patch(
                "chejin_worker_client.task_runner.CONFIG",
                replace(CONFIG, task_safe_wake_enabled=False),
            ):
                self.assertFalse(
                    runner._request_task_wake_if_safe(reason="feature_off")
                )

        with (
            patch(
                "chejin_worker_client.task_runner.load_runtime_control",
                return_value={
                    "pause_requested": False,
                    "inflight_flow_id": None,
                },
            ),
            patch(
                "chejin_worker_client.task_runner.lock_summary",
                return_value={"locked": True},
            ),
        ):
            self.assertFalse(
                runner._request_task_wake_if_safe(reason="persisted_ui_lock")
            )

        self.assertFalse(runner._task_wake_event.is_set())

    def test_transaction_barrier_wakes_only_on_unsettled_to_settled_edge(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        with patch.object(
            runner,
            "_request_task_wake_if_safe",
            return_value=True,
        ) as wake:
            runner._remember_transaction_barrier_state(False)
            runner._remember_transaction_barrier_state(True)
            runner._remember_transaction_barrier_state(True)

        wake.assert_called_once()
        self.assertEqual(
            wake.call_args.kwargs["reason"], "transaction_barrier_settled"
        )
        self.assertIsInstance(
            wake.call_args.kwargs["barrier_occupied_duration_ms"], int
        )

    def test_task_completion_requests_wake_only_after_task_lock_release(self):
        task = self.make_chat_reply_task(task_id="task-safe-wake")
        api = FakeApi(task)
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.binding = binding
        lock_states: list[bool] = []

        def observe_wake(*, reason: str) -> bool:
            self.assertEqual(reason, "task_execution_boundary_released")
            lock_states.append(runner.task_lock.locked())
            return True

        with (
            patch.object(
                runner, "_worker_transaction_barrier_ready", return_value=True
            ),
            patch.object(runner, "_execute_task"),
            patch.object(
                runner,
                "_request_task_wake_if_safe",
                side_effect=observe_wake,
            ) as wake,
        ):
            runner._pull_and_execute(binding)

        wake.assert_called_once()
        self.assertEqual(lock_states, [False])

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
        self.assertEqual(
            bridge.calibration_prepare_calls,
            1,
            "C3 pre-send refresh must reuse one startup calibration transaction gate",
        )
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
                    journal_item_id=str(reply_action_id or ""),
                    action_phase="trigger_attempted",
                    business_state="send_button_click_starting",
                )
                observed_phases.append(action_journal_phase(journal_path))
                update_action_journal_item(
                    journal_path,
                    journal_item_id=str(reply_action_id or ""),
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

    def test_pause_after_send_trigger_does_not_repeat_and_reaches_sent_ack(self):
        task = self.make_chat_reply_task(
            task_id="task-pause-after-send-trigger"
        )
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        runner_holder: dict[str, TaskRunner] = {}
        send_call_count = 0

        class PauseAfterTriggerBridge(FakeBridge):
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
                nonlocal send_call_count
                send_call_count += 1
                journal_path = self.send_transaction_journal_path(
                    str(reply_action_id or "")
                )
                update_action_journal_item(
                    journal_path,
                    journal_item_id=str(reply_action_id or ""),
                    action_phase="trigger_attempted",
                    business_state="send_button_click_starting",
                )
                self.pause_succeeded = runner_holder[
                    "runner"
                ].set_run_status("paused")
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

        bridge = PauseAfterTriggerBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        runner_holder["runner"] = runner
        runner.binding = Binding(
            worker_id="worker-pause-after-send-trigger",
            worker_token="token",
            client_instance_id="client-pause-after-send-trigger",
            run_status="running",
        )

        runner.tick_once()

        self.assertTrue(bridge.pause_succeeded)
        self.assertEqual(send_call_count, 1)
        self.assertEqual(len(bridge.sent_replies), 1)
        self.assertEqual(
            api.events.count("sent_ack:sent:None"),
            1,
        )
        self.assertIn(
            f"finish:{task.id}:task_terminal::",
            api.inflight_flow_events,
        )
        self.assertFalse(
            bridge.send_transaction_journal_path(
                "reply-action-1"
            ).exists()
        )

    def test_pause_after_brain_reply_keeps_same_flow_through_sent_ack(self):
        task = self.make_chat_reply_task(task_id="task-pause-after-brain")
        api = FakeApi(task)
        self.authorize_chat_reply_target(api)
        api.message_ingest_result = "duplicated"
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        runner.binding = Binding(
            worker_id="worker-pause-after-brain",
            worker_token="token",
            client_instance_id="client-pause-after-brain",
            run_status="running",
        )

        def pause_after_claim_send(_task):
            self.assertTrue(runner.set_run_status("paused"))
            self.assertTrue(
                runner._can_continue_inflight_flow(task.id)
            )

        api.claim_send_callback = pause_after_claim_send

        runner.tick_once()

        self.assertEqual(runner.binding.run_status, "paused")
        self.assertEqual(len(bridge.sent_replies), 1)
        self.assertTrue(api.message_payloads)
        self.assertEqual(api.message_payloads[0]["read_run_id"], task.id)
        self.assertIn("sent_ack:sent:None", api.events)
        self.assertIn(
            f"finish:{task.id}:task_terminal::",
            api.inflight_flow_events,
        )
        self.assertNotIn("pull", api.events[api.events.index("run_status:paused") + 1 :])

    def test_pause_during_target_locating_finishes_current_read_only(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-pause-locate-1",
                rpa_session_key="wx:rpa:v1:pause-locate-1",
                display_name="CJTEST01 客户一",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 客户一"},
                ocr_confidence=0.98,
                read_reason="recent_ai_sent",
                authorization_revision="revision-pause-locate-1",
            ),
            WechatReadTarget(
                conversation_id="conv-pause-locate-2",
                rpa_session_key="wx:rpa:v1:pause-locate-2",
                display_name="CJTEST02 客户二",
                remark_code="CJTEST02",
                row_fingerprint={"title_text": "CJTEST02 客户二"},
                ocr_confidence=0.98,
                read_reason="waiting_user_reply",
                authorization_revision="revision-pause-locate-2",
            ),
        ]
        runner_holder: dict[str, TaskRunner] = {}

        class PauseDuringLocateBridge(FakeBridge):
            def locate_chat(self, **kwargs):
                result = super().locate_chat(**kwargs)
                if len(self.locate_chats) == 1:
                    self.assert_pause_succeeded = runner_holder[
                        "runner"
                    ].set_run_status("paused")
                return result

        bridge = PauseDuringLocateBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        runner_holder["runner"] = runner
        binding = Binding(
            worker_id="worker-pause-locate",
            worker_token="token",
            client_instance_id="client-pause-locate",
            run_status="running",
        )
        runner.binding = binding

        runner._read_state_target_queue(binding)

        self.assertTrue(bridge.assert_pause_succeeded)
        self.assertEqual(binding.run_status, "paused")
        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            api.message_payloads[0]["conversation_id"],
            "conv-pause-locate-1",
        )
        self.assertFalse(
            any(
                item.get("conversation_id") == "conv-pause-locate-2"
                for item in api.message_payloads
            )
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in api.inflight_flow_events
                    if item.startswith("finish:")
                ]
            ),
            1,
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

        self.assertIn(
            "ingest:1",
            api.events,
            msg={"stats": runner.c2_stats, "events": api.events},
        )
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

    def test_c2_ingest_switches_to_batch_continuation_before_brain_wait(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-serial-brain",
            rpa_session_key="wx:rpa:v1:serial-brain",
            display_name="CJV6P3R8",
            remark_code="CJV6P3R8",
            read_reason="waiting_user_reply",
            authorization_revision="revision-serial-brain",
            raw={
                "authorization_read_reason": "waiting_user_reply",
                "identity_checkpoint": identity_checkpoint(),
            },
        )
        api.read_targets = [target]
        api.message_batch_result = {
            "batch_id": "batch-serial-brain",
            "batch_status": "generating",
            "continuation": {
                "batch_id": "batch-serial-brain",
                "token": "continuation-batch-serial-brain",
                "authorization_revision": "revision-serial-brain",
                "read_reason": "waiting_user_reply",
            },
        }
        api.message_batch_statuses = [
            {
                "batch_id": "batch-serial-brain",
                "batch_status": "generating",
                "processing": True,
                "updated_at": "generating",
            },
            {
                "batch_id": "batch-serial-brain",
                "batch_status": "handoff",
                "processing": False,
                "decision": "handoff",
                "updated_at": "done",
            },
        ]
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
        runner.binding = binding
        ingest_confirmed = False
        authorization_requests: list[tuple[bool, str, str]] = []
        lock_states_during_batch_poll: list[bool] = []
        original_ingest = api.post_wechat_messages_ingest
        original_authorization = api.get_wechat_read_authorization
        original_batch_status = api.get_wechat_message_batch

        def ingest_and_consume_active_read(*args, **kwargs):
            nonlocal ingest_confirmed
            result = original_ingest(*args, **kwargs)
            ingest_confirmed = True
            return result

        def authorization_after_ingest(
            binding_arg,
            conversation_id,
            *,
            continuation_batch_id=None,
            continuation_token=None,
            **kwargs,
        ):
            authorization_requests.append(
                (
                    ingest_confirmed,
                    str(continuation_batch_id or ""),
                    str(continuation_token or ""),
                )
            )
            if ingest_confirmed and not continuation_batch_id:
                return {
                    "allowed": False,
                    "conversation_id": conversation_id,
                    "authorization_revision": "",
                    "read_reason": "",
                }
            return original_authorization(
                binding_arg,
                conversation_id,
                continuation_batch_id=continuation_batch_id,
                continuation_token=continuation_token,
                **kwargs,
            )

        def batch_status_with_lock_check(*args, **kwargs):
            lock_states_during_batch_poll.append(
                runner.current_ui_lock is not None
            )
            return original_batch_status(*args, **kwargs)

        api.post_wechat_messages_ingest = ingest_and_consume_active_read
        api.get_wechat_read_authorization = authorization_after_ingest
        api.get_wechat_message_batch = batch_status_with_lock_check

        with patch(
            "chejin_worker_client.task_runner.time.sleep",
            return_value=None,
        ):
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["conversation_terminal_state"], "handoff")
        self.assertEqual(len(lock_states_during_batch_poll), 2)
        self.assertTrue(all(lock_states_during_batch_poll))
        after_ingest_requests = [
            request for request in authorization_requests if request[0]
        ]
        self.assertTrue(after_ingest_requests)
        self.assertTrue(
            all(
                request[1:] == (
                    "batch-serial-brain",
                    "continuation-batch-serial-brain",
                )
                for request in after_ingest_requests
            )
        )
        self.assertEqual(bridge.session_scans, [])
        self.assertNotIn("run_status:paused", api.events)

    def test_c2_active_batch_without_valid_continuation_pauses_before_wait(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-missing-continuation",
            rpa_session_key="wx:rpa:v1:missing-continuation",
            display_name="CJV6P3R8",
            remark_code="CJV6P3R8",
            read_reason="waiting_user_reply",
            authorization_revision="revision-missing-continuation",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        api.read_targets = [target]
        api.message_batch_result = {
            "batch_id": "batch-missing-continuation",
            "batch_status": "generating",
        }
        runner, _ = self.make_runner(
            api,
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        runner.binding = binding

        with patch.object(
            runner,
            "_wait_and_send_current_c3_batch",
        ) as wait_for_brain:
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "C3_BATCH_CONTINUATION_INVALID")
        self.assertEqual(binding.run_status, "paused")
        self.assertIn("run_status:paused", api.events)
        wait_for_brain.assert_not_called()

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

    def test_c2_scan_paused_after_sidecar_returns_closes_ui_timeline(self):
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
        runner.binding = binding
        runtime_events: list[dict] = []
        runner.on_runtime_process = runtime_events.append
        original_list_sessions = bridge.list_sessions

        def pause_after_scan(**kwargs):
            payload = original_list_sessions(**kwargs)
            binding.run_status = "paused"
            return payload

        bridge.list_sessions = pause_after_scan  # type: ignore[method-assign]

        runner._scan_wechat_sessions(binding, reason="unit")

        self.assertEqual(
            [event["event"] for event in runtime_events],
            ["scan_started", "scan_cancelled"],
        )
        self.assertIsNone(runner.current_step)
        self.assertEqual(api.scan_payloads, [])
        self.assertFalse(LOCK_FILE.exists())

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
        self.assertNotIn("voice_prepare", bridge.c2_operation_order)
        self.assertEqual(
            api.message_payloads[0]["evidence"][
                "authoritative_frame_source"
            ],
            "initial_read",
        )
        self.assertEqual(
            [
                item["content"]
                for item in api.message_payloads[0]["messages"]
            ],
            ["你好"],
        )
        self.assertEqual(
            api.message_payloads[0]["evidence"]["flow_gate_errors"],
            [],
        )
        self.assertIsNone(api.message_payloads[0]["evidence"]["voice_transcription"])

    def test_c2_transcribed_and_backend_confirmed_voices_never_prepare(self):
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        transcribed_api = FakeApi(None)
        transcribed_target = WechatReadTarget(
            conversation_id="conv-transcribed-only",
            rpa_session_key="wx:transcribed-only",
            display_name="CJTRN001",
            remark_code="CJTRN001",
            authorization_revision="revision-transcribed-only",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        transcribed_api.read_targets = [transcribed_target]
        transcribed_bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused")
        )
        transcribed_bridge.get_messages_payloads = [{
            "observations": [{
                "schema_version": 3,
                "observation_id": "voice-already-transcribed",
                "row_kind": "voice_transcript",
                "sender_role": "customer",
                "sender_role_source": "parent_voice",
                "message_type": "voice",
                "voice_state": "transcribed",
                "native_source_message_id": "native-transcribed-voice",
                "content_clean": "已经转写",
                "parent_voice_anchor_key": "voice-transcribed-parent",
                "source_message": {
                    "id": "voice-already-transcribed",
                    "type": "voice",
                    "sender_role": "customer",
                    "content": "已经转写",
                    "native_source_message_id": "native-transcribed-voice",
                },
            }],
        }]
        transcribed_runner, _ = self.make_runner(
            transcribed_api,
            transcribed_bridge,
        )
        transcribed_runner._read_state_target_queue(binding)

        self.assertNotIn(
            "voice_prepare",
            transcribed_bridge.c2_operation_order,
        )
        self.assertEqual(
            len(transcribed_api.message_payloads),
            1,
            msg={
                "stats": transcribed_runner.c2_stats,
                "events": transcribed_api.events,
            },
        )
        self.assertEqual(
            transcribed_api.message_payloads[0]["evidence"][
                "authoritative_frame_source"
            ],
            "initial_read",
        )

        historical_api = FakeApi(None)
        historical_target = WechatReadTarget(
            conversation_id="conv-historical-voice-only",
            rpa_session_key="wx:historical-voice-only",
            display_name="CJHIS001",
            remark_code="CJHIS001",
            authorization_revision="revision-historical-voice-only",
        )
        historical_source_key = worker_source_message_key(
            historical_target,
            identity_kind="worker_sequence",
            identity="worker-message-7",
        )
        historical_target.raw = {
            "identity_checkpoint": {
                "version": 2,
                "next_sequence_floor": 8,
                "recent_messages": [{
                    "stable_id": "worker-message-7",
                    "source_message_key": historical_source_key,
                    "origin_read_run_id": "read-historical-voice",
                    "sender_role": "customer",
                    "message_type": "voice",
                    "normalized_content_hash": "",
                    "native_source_message_id": "native-historical-voice",
                    "frame_visual_id": "",
                }],
            },
        }
        historical_api.read_targets = [historical_target]
        historical_bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused")
        )
        historical_bridge.get_messages_payloads = [{
            "observations": [{
                "schema_version": 3,
                "observation_id": "historical-voice-current-frame",
                "row_kind": "voice_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "voice",
                "voice_state": "untranscribed",
                "native_source_message_id": "native-historical-voice",
                "voice_anchor_key": "frame-local-history-anchor",
                "source_message": {
                    "id": "historical-voice-current-frame",
                    "type": "voice",
                    "sender_role": "customer",
                },
            }],
        }]
        historical_runner, _ = self.make_runner(
            historical_api,
            historical_bridge,
        )
        historical_runner._read_state_target_queue(binding)

        self.assertNotIn(
            "voice_prepare",
            historical_bridge.c2_operation_order,
        )
        self.assertEqual(historical_bridge.voice_transcribes, [])
        self.assertEqual(historical_api.message_payloads, [])

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
                        "voice_anchor_stable_key": "voice-transcription-1",
                        "frame_visual_id": (
                            "visual-untranscribed-voice-bubble"
                        ),
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

    def test_c2_text_and_voice_preserve_order_and_ingest(self):
        api = FakeApi(None)
        api.read_targets = [
            WechatReadTarget(
                conversation_id="conv-1",
                rpa_session_key="wx:rpa:v1:a",
                display_name="CJTEST01 许聪",
                remark_code="CJTEST01",
                row_fingerprint={"title_text": "CJTEST01 许聪"},
                ocr_confidence=0.98,
                authorization_revision="revision-text-and-voice",
                raw={"identity_checkpoint": identity_checkpoint()},
            )
        ]
        bridge = FakeBridge(RpaResult(ok=True, result_code="invite_sent", message="unused"), message_sender_role="customer")
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-text-before-voice",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "在？",
                    },
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": '[语音] 2"',
                        "voice_anchor_stable_key": "voice-transcription-1",
                        "frame_visual_id": (
                            "visual-untranscribed-voice-bubble"
                        ),
                    }
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-text-before-voice",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "在？",
                    },
                    {
                        "id": "wx-msg-voice-text",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "你好",
                        "voice_anchor_stable_key": "voice-transcription-1",
                        "frame_visual_id": (
                            "visual-expanded-voice-transcript"
                        ),
                    }
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_completed",
            "action_phase": "confirmed",
            "business_state": "completed",
            "business_result_confirmed": True,
            "ui_action_performed": True,
            "sidecar_run_id": "voice-run-1",
            "artifact_dir": "C:/voice-run-1",
            "attempt_count": 1,
            "quality_flags": [],
            "transcribed_messages": [{"content": "你好", "sender_role": "customer"}],
            "item_action_outcomes": [
                {
                    "action_phase": "confirmed",
                    "business_state": "completed",
                    "business_result_confirmed": True,
                    "physical_anchor_keys": ["voice-transcription-1"],
                }
            ],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running")

        result = runner._read_one_wechat_target(
            binding,
            api.read_targets[0],
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            bridge.c2_operation_order,
            [
                "locate_chat",
                "messages",
                "voice_prepare",
                "voice_transcribe",
            ],
        )
        self.assertEqual(bridge.voice_transcribes[0]["target_mode"], "current")
        self.assertEqual(bridge.voice_transcribes[0]["max_duration_seconds"], 240)
        self.assertEqual(bridge.message_reads[0]["target_mode"], "current")
        self.assertIn(
            "ingest:2",
            api.events,
            msg={"stats": runner.c2_stats, "events": api.events},
        )
        messages = api.message_payloads[0]["messages"]
        self.assertEqual(
            [(item["message_type"], item["content"]) for item in messages],
            [("text", "在？"), ("voice", "你好")],
        )
        self.assertEqual(messages[1]["sender_role_hint"], "customer")
        self.assertEqual(
            messages[1]["raw_payload"]["voice_transcription"],
            "你好",
        )
        self.assertEqual(
            messages[1]["raw_payload"]["voice_transcription_meta"][
                "state"
            ],
            "voice_transcribe_completed",
        )
        self.assertEqual(
            api.message_payloads[0]["evidence"][
                "authoritative_frame_source"
            ],
            "final_read",
        )
        self.assertIs(
            api.message_payloads[0]["evidence"][
                "ui_frame_invalidated"
            ],
            True,
        )
        self.assertEqual(
            api.message_payloads[0]["evidence"]["flow_gate_errors"],
            [],
        )
        timing = api.message_payloads[0]["evidence"]["timing"]
        self.assertEqual(timing["schema_version"], 1)
        self.assertEqual(
            [phase["name"] for phase in timing["phases"]],
            [
                "target_chat_locate",
                "initial_message_read",
                "voice_transcribe",
                "build_ingest_payload",
            ],
        )

    def test_voice_action_concurrent_new_text_commits_one_ordered_batch_and_one_brain(self):
        unique = str(time.time_ns())
        conversation_id = f"conv-voice-a-text-b-{unique}"
        batch_id = f"batch-voice-a-text-b-{unique}"
        authorization_revision = f"revision-voice-a-text-b-{unique}"
        api = FakeApi(None)
        api.message_batch_result = {
            "batch_id": batch_id,
            "conversation_id": conversation_id,
            "batch_status": "generating",
            "continuation": {
                "batch_id": batch_id,
                "token": f"continuation-{batch_id}",
                "authorization_revision": authorization_revision,
                "read_reason": "waiting_user_reply",
            },
        }
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key="wx:rpa:v1:voice-a-text-b",
            display_name="CJVTB001",
            remark_code="CJVTB001",
            read_reason="waiting_user_reply",
            authorization_revision=authorization_revision,
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        api.read_targets = [target]
        voice_a_raw = {
            "id": "voice-a",
            "type": "voice",
            "sender_role": "customer",
            "voice_duration": 5,
            "content": '[语音] 5"',
            "voice_anchor_stable_key": "voice-a-anchor",
            "bubble_rect": [420, 120, 610, 170],
        }
        voice_a_done = {
            **voice_a_raw,
            "id": "voice-a-transcript",
            "content": "语音A已转写",
        }
        text_b = {
            "id": "text-b-arrived-during-voice",
            "type": "text",
            "sender_role": "customer",
            "content": "文字B在转写期间到达",
            "bubble_rect": [420, 220, 720, 270],
        }
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {"ok": True, "messages": [voice_a_raw]},
            {"ok": True, "messages": [voice_a_done, text_b]},
        ]
        bridge.voice_payload = {
            "ok": True,
            "state": "voice_transcribe_completed",
            "action_phase": "confirmed",
            "business_state": "completed",
            "business_result_confirmed": True,
            "ui_action_performed": True,
            "sidecar_run_id": "voice-a-action",
            "processed_voice_anchor_keys": ["voice-a-anchor"],
            "failed_voice_anchor_keys": [],
            "transcribed_messages": [
                {
                    "content": "语音A已转写",
                    "sender_role": "customer",
                    "voice_anchor_stable_key": "voice-a-anchor",
                }
            ],
            "item_action_outcomes": [
                {
                    "action_phase": "confirmed",
                    "business_state": "completed",
                    "business_result_confirmed": True,
                    "physical_anchor_keys": ["voice-a-anchor"],
                }
            ],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-voice-a-text-b",
            worker_token="token",
            client_instance_id="client-voice-a-text-b",
            run_status="running",
        )

        with patch.object(
            runner,
            "_wait_and_send_current_c3_batch",
            return_value={"ok": True, "sent": False},
        ) as brain:
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(len(api.message_payloads), 1)
        messages = api.message_payloads[0]["messages"]
        self.assertEqual(
            [(item["message_type"], item["content"]) for item in messages],
            [
                ("voice", "语音A已转写"),
                ("text", "文字B在转写期间到达"),
            ],
        )
        slot_states = api.message_payloads[0]["evidence"][
            "slot_ledger_states"
        ]
        self.assertEqual(
            [item["screen_order"] for item in slot_states],
            sorted(item["screen_order"] for item in slot_states),
        )
        self.assertEqual(
            len({item["source_message_key"] for item in slot_states}),
            2,
        )
        brain.assert_called_once()

    def test_wrapped_ai_history_still_transcribes_and_ingests_new_voice(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-wrapped-history-full-voice",
            rpa_session_key="wx:rpa:v1:wrapped-history-full-voice",
            display_name="CJVOICE9",
            remark_code="CJVOICE9",
            read_reason="recent_ai_sent",
            authorization_revision="revision-wrapped-history-full-voice",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 3,
                    "recent_messages": [
                        {
                            "stable_id": "worker-message-1",
                            "source_message_key": "source-customer-1",
                            "origin_read_run_id": "read-history-1",
                            "sender_role": "customer",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "你好在吗"
                            ),
                        },
                        {
                            "stable_id": "worker-message-2",
                            "source_message_key": "source-ai-2",
                            "origin_read_run_id": "read-history-2",
                            "sender_role": "self",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "你好，欢迎加上好友，很高兴认识你！请问有什么可以帮您？"
                            ),
                        },
                    ],
                }
            },
        )
        api.read_targets = [target]
        wrapped_ai = "你好，欢迎加上好友，很高兴认识你！请问有\n什么可以帮您？"
        raw_voice = {
            "id": "new-five-second-voice",
            "type": "voice",
            "sender_role": "customer",
            "voice_duration": 5,
            "content": '[语音] 5"',
            "voice_anchor_stable_key": "voice-new-five-seconds",
            "frame_visual_id": "visual-new-five-seconds",
        }
        history = [
            {
                "id": "visible-customer-1",
                "type": "text",
                "sender_role": "customer",
                "content": "你好在吗",
            },
            {
                "id": "visible-ai-2",
                "type": "text",
                "sender_role": "self",
                "content": wrapped_ai,
            },
        ]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {"ok": True, "messages": [*history, raw_voice]},
            {
                "ok": True,
                "messages": [
                    *history,
                    {
                        **raw_voice,
                        "id": "new-five-second-voice-transcribed",
                        "content": "你好，我想咨询一下",
                    },
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "adapter": "mock",
            "state": "voice_transcribe_completed",
            "action_phase": "confirmed",
            "business_state": "completed",
            "business_result_confirmed": True,
            "ui_action_performed": True,
            "sidecar_run_id": "voice-run-wrapped-history",
            "artifact_dir": "C:/voice-run-wrapped-history",
            "attempt_count": 1,
            "quality_flags": [],
            "transcribed_messages": [
                {"content": "你好，我想咨询一下", "sender_role": "customer"}
            ],
            "item_action_outcomes": [
                {
                    "action_phase": "confirmed",
                    "business_state": "completed",
                    "business_result_confirmed": True,
                    "physical_anchor_keys": ["voice-new-five-seconds"],
                }
            ],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-wrapped-history",
            worker_token="token",
            client_instance_id="client-wrapped-history",
            run_status="running",
        )

        result = runner._read_one_wechat_target(
            binding,
            target,
            current_step="state_target_message_read",
            enforce_read_targets=True,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(len(api.message_payloads), 1)
        submitted_messages = api.message_payloads[0]["messages"]
        self.assertEqual(
            [
                message["content"]
                for message in submitted_messages
                if message["message_type"] == "voice"
            ],
            ["你好，我想咨询一下"],
        )
        self.assertEqual(
            api.message_payloads[0]["evidence"]["flow_gate_errors"],
            [],
        )

    def test_c2_two_same_duration_voices_are_both_transcribed_once(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-two-same-duration-voices",
            rpa_session_key="wx:rpa:v1:two-same-duration-voices",
            display_name="CJK7M4Q2",
            remark_code="CJK7M4Q2",
            read_reason="waiting_user_reply",
            authorization_revision="revision-two-same-duration-voices",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )

        def voice(
            message_id: str,
            anchor: str,
            *,
            content: str,
            visual_id: str,
            top: int,
        ) -> dict:
            return {
                "id": message_id,
                "type": "voice",
                "sender_role": "customer",
                "voice_duration": 3,
                "content": content,
                "voice_anchor_stable_key": anchor,
                "frame_visual_id": visual_id,
                "bubble_rect": [420, top, 620, top + 44],
            }

        upper_pre = voice(
            "same-duration-upper-pre",
            "same-duration-upper",
            content='[语音] 3"',
            visual_id="visual-upper-untranscribed",
            top=220,
        )
        lower_pre = voice(
            "same-duration-lower-pre",
            "same-duration-lower",
            content='[语音] 3"',
            visual_id="visual-lower-untranscribed",
            top=320,
        )
        upper_post = voice(
            "same-duration-upper-post",
            "same-duration-upper",
            content="第一条三秒语音",
            visual_id="visual-upper-expanded-transcript",
            top=220,
        )
        lower_post = voice(
            "same-duration-lower-post",
            "same-duration-lower",
            content="第二条三秒语音",
            visual_id="visual-lower-expanded-transcript",
            top=320,
        )
        bridge.get_messages_payloads = [
            {"messages": [upper_pre, lower_pre]},
            {"messages": [upper_post, lower_pre]},
            {"messages": [upper_post, lower_post]},
        ]
        bridge.voice_payloads = [
            {
                "ok": True,
                "state": "voice_transcribe_completed",
                "processed_voice_anchor_keys": ["same-duration-upper"],
                "failed_voice_anchor_keys": [],
                "transcribed_messages": [upper_post],
            },
            {
                "ok": True,
                "state": "voice_transcribe_completed",
                "processed_voice_anchor_keys": ["same-duration-lower"],
                "failed_voice_anchor_keys": [],
                "transcribed_messages": [lower_post],
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

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(bridge.voice_transcribes), 2)
        self.assertEqual(
            len(
                {
                    item["reserved_worker_stable_id"]
                    for item in bridge.voice_transcribes
                }
            ),
            2,
        )
        self.assertEqual(len(api.message_payloads), 1)
        messages = api.message_payloads[0]["messages"]
        self.assertEqual(
            [item["content"] for item in messages],
            ["第一条三秒语音", "第二条三秒语音"],
        )
        self.assertEqual(
            [item["item_state"] for item in messages],
            ["completed", "completed"],
        )
        self.assertEqual(
            api.message_payloads[0]["evidence"]["flow_gate_errors"],
            [],
        )
        self.assertFalse(result.get("identity_gate_reported", False))

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
            def execute_voice_action(
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
                    journal_item_id=source_key,
                    action_phase="trigger_attempted",
                    business_state="voice_menu_clicked",
                )
                observed_phases.append(action_journal_phase(journal_path))
                update_action_journal_item(
                    journal_path,
                    journal_item_id=source_key,
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
                    "action_phase": "confirmed",
                    "business_state": "completed",
                    "business_result_confirmed": True,
                    "ui_action_performed": True,
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
                return super().execute_voice_action(
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
        self.assertEqual(
            api.message_payloads[0]["evidence"][
                "authoritative_frame_source"
            ],
            "final_read",
        )
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
            def execute_voice_action(
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
                    journal_item_id=source_key,
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
                    "action_phase": "confirmed",
                    "business_state": "completed",
                    "business_result_confirmed": True,
                    "ui_action_performed": True,
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
                return super().execute_voice_action(
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

        self.assertTrue(
            result["ok"],
            {
                "result": result,
                "voice_transcribes": bridge.voice_transcribes,
                "last_payload": bridge.last_message_payload,
                "journal_after": (
                    read_action_journal(
                        Path(bridge.voice_transcribes[0]["action_journal"])
                    )
                    if bridge.voice_transcribes
                    else None
                ),
            },
        )
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
            "native_source_message_id": "native-voice-old",
            "source_message": {"voice_anchor_stable_key": "voice:customer:2:bottom:1"},
            "_worker_stable_id": "worker-message-1",
        }
        voice_observation = attach_native_committed_identity(
            voice_observation,
            worker_stable_id="worker-message-1",
            native_source_message_id="native-voice-old",
        )
        old_source_key = voice_observation_source_key(target, voice_observation)
        old_read_run_id = "read-confirmed-old-voice"
        target.raw["identity_checkpoint"] = {
            "version": 2,
            "next_sequence_floor": 2,
            "recent_messages": [{
                "stable_id": "worker-message-1",
                "source_message_key": old_source_key,
                "origin_read_run_id": old_read_run_id,
                "sender_role": "customer",
                "message_type": "voice",
                "normalized_content_hash": "",
                "native_source_message_id": "native-voice-old",
                "frame_visual_id": "",
            }],
        }
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=old_source_key,
            origin_read_run_id=old_read_run_id,
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
        self.assertEqual(
            api.message_payloads[0]["messages"][0]["content"],
            "新的文字",
        )

    def test_legacy_anchor_ledger_cannot_skip_new_voice_at_same_position(self):
        target = WechatReadTarget(
            conversation_id=f"conv-anchor-upgrade-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:anchor-upgrade",
            display_name="CJUPGRD1",
            remark_code="CJUPGRD1",
            authorization_revision="revision-anchor-upgrade",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        reused_anchor = "voice:customer:3:bottom:1"
        legacy_source_key = worker_source_message_key(
            target,
            identity_kind="voice_physical_anchor",
            identity=reused_anchor,
        )
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=legacy_source_key,
            origin_read_run_id="read-legacy-anchor",
            dedupe_key=None,
            message_type="voice",
            terminal_state="completed",
            ingest_state="confirmed",
            result={"content": "旧语音已完成"},
        )
        new_voice = {
            "schema_version": 3,
            "observation_id": "new-voice-same-position",
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "item_state": "discovered",
            "voice_anchor_key": reused_anchor,
            "source_message": {
                "id": "new-voice-same-position",
                "type": "voice",
                "sender_role": "customer",
                "voice_anchor_stable_key": reused_anchor,
            },
        }

        executable = _executable_untranscribed_voice_observations(
            target,
            {"observations": [new_voice]},
        )

        self.assertEqual(
            [item["observation_id"] for item in executable],
            ["new-voice-same-position"],
        )
        with self.assertRaisesRegex(
            ValueError,
            "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        ):
            voice_observation_source_key(target, new_voice)

    def test_new_suffix_does_not_commit_untrusted_image_identity(self):
        target = WechatReadTarget(
            conversation_id=f"conv-untrusted-image-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:untrusted-image",
            display_name="CJIMGU01",
            remark_code="CJIMGU01",
            authorization_revision="revision-untrusted-image",
        )
        observations = [
            {
                "observation_id": "image-role-unknown",
                "row_kind": "image_bubble",
                "sender_role": "unknown",
                "sender_role_source": "unknown",
                "message_type": "image",
                "voice_state": "not_voice",
                "source_message": {"id": "image-role-unknown"},
            },
            {
                "observation_id": "text-role-confirmed",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "text",
                "voice_state": "not_voice",
                "content_clean": "在吗",
                "source_message": {"id": "text-role-confirmed"},
            },
        ]
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(
                    ok=True,
                    result_code="unused",
                    message="unused",
                )
            ),
        )

        assigned = runner._assign_sequence_new_suffix_identities(
            target=target,
            observations=observations,
            evidence={
                "alignment_status": "not_required",
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [
                    "image-role-unknown",
                    "text-role-confirmed",
                ],
            },
            read_run_id="read-untrusted-image",
        )

        self.assertNotIn("_worker_stable_id", assigned[0])
        self.assertRegex(
            assigned[1]["_worker_stable_id"], r"^worker-message-\d+$"
        )

    def test_second_round_single_text_history_ingests_new_tail_and_enters_brain(self):
        unique = str(time.time_ns())
        api = FakeApi(None)
        batch_id = f"batch-second-round-{unique}"
        authorization_revision = f"revision-second-round-{unique}"
        api.message_batch_result = {
            "batch_id": batch_id,
            "conversation_id": f"conv-second-round-{unique}",
            "batch_status": "generating",
            "continuation": {
                "batch_id": batch_id,
                "token": f"continuation-{batch_id}",
                "authorization_revision": authorization_revision,
                "read_reason": "waiting_user_reply",
            },
        }
        target = WechatReadTarget(
            conversation_id=f"conv-second-round-{unique}",
            rpa_session_key="wx:rpa:v1:second-round",
            display_name="CJROUND1",
            remark_code="CJROUND1",
            read_reason="waiting_user_reply",
            authorization_revision=authorization_revision,
        )
        target.raw["identity_checkpoint"] = {
            "version": 2,
            "next_sequence_floor": 2,
            "recent_messages": [
                {
                    "stable_id": "worker-message-1",
                    "source_message_key": worker_source_message_key(
                        target,
                        identity_kind="worker_sequence",
                        identity="worker-message-1",
                    ),
                    "origin_read_run_id": "read-first-round",
                    "sender_role": "customer",
                    "message_type": "text",
                    "normalized_content_hash": (
                        normalized_content_hash("你好")
                    ),
                    "native_source_message_id": "",
                    "frame_visual_id": "",
                }
            ],
        }
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "messages": [
                    {
                        "id": "visible-old-hello",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "你好",
                        "bubble_rect": [420, 100, 650, 140],
                    },
                    {
                        "id": "new-question",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "在吗",
                        "bubble_rect": [420, 180, 650, 220],
                    },
                ]
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-second-round",
            worker_token="token",
            client_instance_id="client-second-round",
            run_status="running",
        )

        with patch.object(
            runner,
            "_wait_and_send_current_c3_batch",
            return_value={"ok": True, "sent": False},
        ) as brain:
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            [item["content"] for item in api.message_payloads[0]["messages"]],
            ["在吗"],
        )
        brain.assert_called_once()

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
                        "voice_anchor_stable_key": "voice-machine-hash",
                        "bubble_rect": [120, 200, 240, 240],
                    }
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": "你好",
                        "voice_anchor_stable_key": "voice-machine-hash",
                        "bubble_rect": [120, 200, 240, 240],
                    }
                ],
            },
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

        self.assertEqual(
            bridge.c2_operation_order,
            ["locate_chat", "messages", "voice_prepare", "voice_transcribe"],
        )
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
            [
                "locate_chat",
                "messages",
                "voice_prepare",
                "voice_transcribe",
                "voice_prepare",
                "voice_transcribe",
            ],
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
            "voice_transcribe_completed",
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
        failed_source_key = failed_voice["source_message_key"]
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
            },
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
        self.assertTrue(
            any(
                item["item_state"] == "failed"
                for item in payload["messages"]
            ),
            payload,
        )
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
                        "observation_id": "stable-text-before-media",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "稳定上下文",
                        "native_source_message_id": (
                            "native-stable-text-before-media"
                        ),
                        "bubble_rect": [400, 40, 600, 80],
                        "source_message": {
                            "id": "stable-text-before-media",
                            "type": "text",
                            "sender_role": "customer",
                            "content": "稳定上下文",
                        },
                    },
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
                            "bubble_visual_fingerprint": (
                                "dhash64:1111111111111111"
                            ),
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
            "ok": False,
            "state": "voice_transcribe_click_failed",
            "action_phase": "failed",
            "business_state": "failed",
            "business_result_confirmed": False,
            "ui_action_performed": True,
            "error_code": "VOICE_TRANSCRIBE_TRIGGER_FAILED",
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
            "transaction": {
                "action_phase": "confirmed",
                "slot_identity_confirmed": True,
                "clipboard_image_matches_target": True,
                "image_sha256": "b" * 64,
            },
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

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(bridge.voice_transcribes), 2)
        self.assertNotEqual(
            bridge.voice_transcribes[0][
                "selected_pre_observation_id"
            ],
            bridge.voice_transcribes[1][
                "selected_pre_observation_id"
            ],
        )
        self.assertNotEqual(
            bridge.voice_transcribes[0]["reserved_worker_stable_id"],
            bridge.voice_transcribes[1]["reserved_worker_stable_id"],
        )
        self.assertEqual(len(api.message_payloads), 1)
        messages = api.message_payloads[0]["messages"]
        self.assertEqual(
            len(messages),
            2,
            {
                "messages": messages,
                "evidence": api.message_payloads[0]["evidence"],
                "last_payload": bridge.last_message_payload,
            },
        )
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

    def test_failed_voice_move_does_not_hide_new_voice_in_old_seat(self):
        """A failed bubble is excluded by its post frame, never its old seat."""

        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-failed-voice-moved",
            rpa_session_key="wx:failed-voice-moved",
            display_name="CJMOVE01",
            remark_code="CJMOVE01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-failed-voice-moved",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="unused"))

        def voice(
            observation_id: str,
            anchor: str,
            *,
            content: str = '[语音] 3"',
            transcribed: bool = False,
        ) -> dict:
            return {
                "schema_version": 3,
                "observation_id": observation_id,
                "row_kind": (
                    "voice_transcript" if transcribed else "voice_bubble"
                ),
                "sender_role": "customer",
                "sender_role_source": (
                    "parent_voice" if transcribed else "same_row_avatar"
                ),
                "message_type": "voice",
                "voice_state": (
                    "transcribed" if transcribed else "untranscribed"
                ),
                "voice_anchor_key": anchor,
                "parent_voice_anchor_key": (
                    anchor if transcribed else ""
                ),
                "content_clean": content if transcribed else "",
                "source_message": {
                    "id": observation_id,
                    "type": "voice",
                    "sender_role": "customer",
                    "voice_anchor_stable_key": anchor,
                },
            }

        initial = bridge._contractual_message_payload(
            {"observations": [voice("old-pre", "seat-bottom")]}
        )
        bridge.last_message_payload = dict(initial)
        execute_calls = 0
        prepare_calls: list[dict] = []
        original_prepare_voice_action = bridge.prepare_voice_action

        def prepare_voice_action(**kwargs):
            prepare_calls.append(dict(kwargs))
            return original_prepare_voice_action(**kwargs)

        bridge.prepare_voice_action = prepare_voice_action  # type: ignore[method-assign]

        def tracking_edge(
            from_frame: str,
            from_id: str,
            to_frame: str,
            to_id: str,
        ) -> dict:
            return {
                "from_frame_id": from_frame,
                "from_observation_id": from_id,
                "to_frame_id": to_frame,
                "to_observation_id": to_id,
                "sender_role": "customer",
                "message_type": "voice",
                "structural_evidence": {"fixture": True},
                "displacement_evidence": {"fixture": True},
                "edge_candidate_count": 1,
            }

        def execute_voice_action(**kwargs):
            nonlocal execute_calls
            execute_calls += 1
            bridge.c2_operation_order.append("voice_transcribe")
            bridge.voice_transcribes.append(dict(kwargs))
            pre_id = str(kwargs["selected_pre_observation_id"])
            pre_frame = str(kwargs["pre_frame_id"])
            action_id = str(kwargs["canonical_voice_action_id"])
            reserved_id = str(kwargs["reserved_worker_stable_id"])
            if execute_calls == 1:
                post_id = "old-post-shifted"
                post_observations = [
                    voice(post_id, "seat-shifted-up"),
                    voice("new-in-old-seat", "seat-bottom"),
                ]
                state = "voice_transcribe_failed"
                action_phase = "failed"
                ok = False
                error_code = "C2_VOICE_TRANSCRIBE_FAILED"
                binding_status = "failed"
            else:
                post_id = "new-post-transcribed"
                post_observations = [
                    voice("old-post-shifted", "seat-shifted-up"),
                    voice(
                        post_id,
                        "seat-bottom",
                        content="新语音已转写",
                        transcribed=True,
                    ),
                ]
                state = "voice_transcribe_completed"
                action_phase = "confirmed"
                ok = True
                error_code = ""
                binding_status = "confirmed"
            mid_frame = f"mid-{execute_calls}"
            post_frame = f"post-{execute_calls}"
            payload = bridge._contractual_message_payload(
                {
                    "ok": ok,
                    "state": state,
                    "error_code": error_code,
                    "voice_action_stage": "execute",
                    "canonical_voice_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                    "pre_frame_id": pre_frame,
                    "post_frame_id": post_frame,
                    "selected_pre_observation_id": pre_id,
                    "selected_action_token": str(
                        kwargs["selected_action_token"]
                    ),
                    "selected_target_fingerprint": str(
                        kwargs["selected_target_fingerprint"]
                    ),
                    "transcript_binding_status": binding_status,
                    "transcript_binding_method": (
                        "continuous_target_tracking"
                    ),
                    "binding_candidate_count": 1,
                    "tracking_frame_ids": [
                        pre_frame,
                        mid_frame,
                        post_frame,
                    ],
                    "tracking_edges": [
                        tracking_edge(
                            pre_frame, pre_id, mid_frame, pre_id
                        ),
                        tracking_edge(
                            mid_frame, pre_id, post_frame, post_id
                        ),
                    ],
                    "confirmed_action_mapping": {
                        "canonical_action_id": action_id,
                        "reserved_worker_stable_id": reserved_id,
                        "selected_action_token": str(
                            kwargs["selected_action_token"]
                        ),
                        "pre_observation_id": pre_id,
                        "binding_confirmed": True,
                        "post_observation_id": post_id,
                        "derived_observation_ids": [],
                    },
                    "observations": post_observations,
                    "action_phase": action_phase,
                    "business_state": (
                        "completed" if ok else "failed"
                    ),
                    "business_result_confirmed": ok,
                    "ui_action_performed": True,
                }
            )
            bridge.last_message_payload = dict(payload)
            return payload

        bridge.execute_voice_action = execute_voice_action  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        result = runner._finish_new_visible_voices_in_current_chat(
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target=target,
            target_label="CJMOVE01",
            sidecar_payload=initial,
            lease=unittest.mock.Mock(),
            action_cancel_requested=lambda: False,
            enforce_read_targets=False,
            read_run_id="read-failed-voice-moved",
            excluded_voice_anchor_keys=set(),
            flow_outcomes=FlowOutcomeAccumulator(
                origin_read_run_id="read-failed-voice-moved"
            ),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(bridge.voice_transcribes), 2)
        self.assertEqual(
            bridge.voice_transcribes[0]["selected_pre_observation_id"],
            "old-pre",
        )
        self.assertEqual(
            bridge.voice_transcribes[1]["selected_pre_observation_id"],
            "new-in-old-seat",
        )
        self.assertEqual(len(prepare_calls), 2)
        self.assertIn(
            "seat-shifted-up",
            prepare_calls[1]["excluded_voice_anchor_keys"],
        )
        self.assertNotIn(
            "seat-bottom",
            prepare_calls[1]["excluded_voice_anchor_keys"],
        )
        self.assertEqual(
            sorted(
                item["result"] for item in result["item_outcomes"]
            ),
            ["completed", "failed"],
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
        ambiguous_evidence = {
            "pre_sequence_source": "action_frame",
            "pre_frame_id": "voice-before-identity-pre",
            "post_frame_id": "voice-before-identity-post",
            "alignment_status": "ambiguous",
            "candidate_alignment_count": 2,
            "matched_pairs": [],
            "old_tail_fully_consumed": False,
            "new_suffix_observation_ids": [],
        }
        journals_at_alignment = []

        def return_ambiguous_alignment(_pre, post, **_kwargs):
            journals_at_alignment.extend(
                list_action_journals(
                    conversation_id=target.conversation_id,
                    action_kinds=("voice",),
                )
            )
            return list(post), dict(ambiguous_evidence)

        with patch(
            "chejin_worker_client.task_runner.align_post_action_observations",
            side_effect=return_ambiguous_alignment,
        ):
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
        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(len(journals_at_alignment), 1)
        journal_items = journals_at_alignment[0][1]["items"]
        self.assertEqual(len(journal_items), 1)
        journal_item = next(iter(journal_items.values()))
        self.assertEqual(journal_item["business_state"], "failed")
        self.assertNotEqual(
            journal_item["action_phase"], "not_attempted"
        )
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(api.message_payloads[0]["messages"], [])
        self.assertEqual(
            api.message_payloads[0]["evidence"]["flow_gate_errors"],
            ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
        )
        self.assertEqual(
            api.message_payloads[0]["evidence"][
                "authoritative_frame_source"
            ],
            "final_read",
        )
        self.assertTrue(
            api.message_payloads[0]["evidence"]["ui_frame_invalidated"]
        )

    def test_voice_ambiguous_is_one_terminal_gate_and_other_target_continues(
        self,
    ):
        api = FakeApi(None)
        ambiguous_target = WechatReadTarget(
            conversation_id="conv-voice-ambiguous-terminal",
            rpa_session_key="wx:rpa:v1:voice-ambiguous-terminal",
            display_name="CJAMBV01 客户",
            remark_code="CJAMBV01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-voice-ambiguous-terminal",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        healthy_target = WechatReadTarget(
            conversation_id="conv-after-voice-ambiguous",
            rpa_session_key="wx:rpa:v1:after-voice-ambiguous",
            display_name="CJNEXT01 客户",
            remark_code="CJNEXT01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-after-voice-ambiguous",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        api.read_targets = [ambiguous_target, healthy_target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.get_messages_payloads = [
            {
                "ok": True,
                "messages": [
                    {
                        "id": "ambiguous-voice",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 5"',
                        "voice_anchor_stable_key": "ambiguous-anchor",
                        "bubble_rect": [400, 100, 600, 140],
                    }
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "ambiguous-voice",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 5"',
                        "voice_anchor_stable_key": "ambiguous-anchor",
                        "bubble_rect": [400, 100, 600, 140],
                    }
                ],
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "healthy-text",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "另一个客户继续处理",
                        "bubble_rect": [400, 100, 650, 140],
                    }
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "state": "voice_transcribe_ambiguous",
            "error_code": "C2_VOICE_RESULT_AMBIGUOUS",
            "business_state": "failed",
            "business_result_confirmed": False,
            "processed_voice_anchor_keys": [],
            "failed_voice_anchor_keys": ["ambiguous-anchor"],
        }
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )
        production_read = runner._read_one_wechat_target
        read_results: dict[str, list[dict]] = {}

        def tracked_read(
            current_binding: Binding,
            current_target: WechatReadTarget,
            **kwargs,
        ):
            read_result = production_read(
                current_binding,
                current_target,
                **kwargs,
            )
            read_results.setdefault(
                current_target.conversation_id,
                [],
            ).append(read_result)
            return read_result

        runner._read_one_wechat_target = tracked_read  # type: ignore[method-assign]

        emitted_timings: list[dict] = []

        def capture_timing_log(
            _level: str,
            event: str,
            _message: str,
            **kwargs,
        ) -> None:
            if event == "c2_message_read_timing":
                emitted_timings.append(kwargs["metadata"])

        with (
            patch.object(
                runner,
                "_wait_and_send_current_c3_batch",
            ) as brain,
            patch(
                "chejin_worker_client.task_runner.append_log",
                side_effect=capture_timing_log,
            ),
        ):
            runner._read_state_target_queue(binding)

            self.assertEqual(len(bridge.voice_transcribes), 1)
            first_ambiguous_result = read_results[
                ambiguous_target.conversation_id
            ][0]
            self.assertFalse(first_ambiguous_result["ok"])
            self.assertEqual(
                first_ambiguous_result["error_code"],
                "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
            )
            self.assertNotEqual(
                first_ambiguous_result["error_code"],
                "MESSAGE_READ_FAILED",
            )
            self.assertTrue(
                first_ambiguous_result["identity_gate_reported"]
            )
            self.assertEqual(len(api.message_payloads), 2)
            gate_payload = next(
                payload
                for payload in api.message_payloads
                if payload["conversation_id"]
                == ambiguous_target.conversation_id
            )
            self.assertEqual(gate_payload["messages"], [])
            self.assertEqual(
                gate_payload["evidence"]["flow_gate_errors"],
                ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
            )
            self.assertEqual(
                gate_payload["evidence"]["authoritative_frame_source"],
                "final_read",
            )
            self.assertTrue(
                gate_payload["evidence"]["ui_frame_invalidated"]
            )
            healthy_payload = next(
                payload
                for payload in api.message_payloads
                if payload["conversation_id"]
                == healthy_target.conversation_id
            )
            self.assertEqual(
                [item["content"] for item in healthy_payload["messages"]],
                ["另一个客户继续处理"],
            )

            second = runner._read_one_wechat_target(
                binding,
                ambiguous_target,
                current_step="state_target_message_read",
                enforce_read_targets=False,
            )

        self.assertFalse(second["ok"])
        self.assertEqual(
            second["error_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        )
        self.assertEqual(len(bridge.voice_transcribes), 1)
        self.assertEqual(
            len(
                [
                    payload
                    for payload in api.message_payloads
                    if payload["conversation_id"]
                    == ambiguous_target.conversation_id
                ]
            ),
            1,
        )
        brain.assert_not_called()
        voice_phases = [
            phase
            for timing in emitted_timings
            for phase in timing.get("phases", [])
            if phase.get("name") == "voice_transcribe"
        ]
        self.assertEqual(len(voice_phases), 1)
        self.assertFalse(voice_phases[0]["completed"])
        self.assertTrue(voice_phases[0]["failed"])
        self.assertEqual(
            voice_phases[0]["error_code"],
            "C2_VOICE_RESULT_AMBIGUOUS",
        )
        journals = list_action_journals(
            conversation_id=ambiguous_target.conversation_id,
            action_kinds=("voice",),
        )
        self.assertEqual(len(journals), 1)
        journal_item = next(iter(journals[0][1]["items"].values()))
        self.assertEqual(journal_item["action_phase"], "quarantined")
        self.assertFalse(
            str(journals[0][1].get("committed_worker_stable_id") or "")
        )
        identity_state = load_c2_state(
            f"message_identity:{ambiguous_target.conversation_id}"
        )
        self.assertEqual(identity_state["next_sequence"], 2)
        self.assertEqual(
            sorted(identity_state["sequence_reservations"].values()),
            ["worker-message-1"],
        )
        self.assertEqual(
            runner._reserve_worker_sequence(
                ambiguous_target,
                reservation_key="test:after-ambiguous",
            ),
            "worker-message-2",
        )
        self.assertEqual(
            list_c2_ledger_entries(
                ambiguous_target.conversation_id,
                message_type="voice",
            ),
            [],
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
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        bridge = FakeBridge(RpaResult(ok=True, result_code="unused", message="unused"))
        initial = bridge._contractual_message_payload({
            "messages": [
                {"id": "existing-context", "native_source_message_id": "native-existing-context", "type": "text", "sender_role": "customer", "content": "之前的上下文", "bubble_rect": [400, 40, 600, 80]},
                {"id": "voice-first-failed", "type": "voice", "sender_role": "customer", "content": "[语音] 4\"", "voice_anchor_stable_key": "voice-first-failed", "bubble_rect": [400, 100, 600, 140]},
                {"id": "voice-second-success", "type": "voice", "sender_role": "self", "content": "[语音] 3\"", "voice_anchor_stable_key": "voice-second-success", "bubble_rect": [700, 220, 900, 260]},
            ]
        })
        for item in initial["observations"]:
            item["sender_role_source"] = "same_row_avatar"
            if item.get("message_type") == "voice":
                item["row_kind"] = "voice_bubble"
                item["voice_state"] = "untranscribed"
            else:
                item["_worker_stable_id"] = "worker-message-1"
                item["native_source_message_id"] = (
                    "native-existing-context"
                )
        bridge.last_message_payload = dict(initial)
        bridge.voice_payloads = [
            {"ok": False, "state": "voice_transcribe_click_failed", "error_code": "VOICE_TRANSCRIBE_TRIGGER_FAILED", "action_phase": "failed", "business_state": "failed", "business_result_confirmed": False, "ui_action_performed": True},
            {"ok": True, "state": "voice_transcribe_completed", "action_phase": "confirmed", "business_state": "completed", "business_result_confirmed": True, "processed_voice_anchor_keys": ["voice-second-success"], "failed_voice_anchor_keys": [], "item_action_outcomes": [{"physical_anchor_keys": ["voice-second-success"], "action_phase": "confirmed", "business_state": "completed", "business_result_confirmed": True}]},
        ]
        bridge.get_messages_payloads = [
            {"messages": [
                {"id": "existing-context", "native_source_message_id": "native-existing-context", "type": "text", "sender_role": "customer", "content": "之前的上下文", "bubble_rect": [400, 40, 600, 80]},
                {"id": "voice-first-failed", "type": "voice", "sender_role": "customer", "content": "[语音] 4\"", "voice_anchor_stable_key": "voice-first-failed", "bubble_rect": [400, 100, 600, 140]},
                {"id": "voice-second-success", "type": "voice", "sender_role": "self", "content": "[语音] 3\"", "voice_anchor_stable_key": "voice-second-success", "bubble_rect": [700, 220, 900, 260]},
            ]},
            {"messages": [
                {"id": "existing-context", "native_source_message_id": "native-existing-context", "type": "text", "sender_role": "customer", "content": "之前的上下文", "bubble_rect": [400, 40, 600, 80]},
                {"id": "voice-first-failed", "type": "voice", "sender_role": "customer", "content": "[语音] 4\"", "voice_anchor_stable_key": "voice-first-failed", "bubble_rect": [400, 100, 600, 140]},
                {"id": "voice-second-success", "type": "voice", "sender_role": "self", "content": "transcribed second voice", "voice_anchor_stable_key": "voice-second-success", "bubble_rect": [700, 220, 900, 280]},
            ]},
        ]
        original_get_messages = bridge.get_messages

        def trusted_get_messages(**kwargs):
            payload = original_get_messages(**kwargs)
            for observation in payload.get("observations") or []:
                if not isinstance(observation, dict):
                    continue
                observation["sender_role_source"] = "same_row_avatar"
                if observation.get("message_type") == "text":
                    observation["_worker_stable_id"] = "worker-message-1"
                    observation["native_source_message_id"] = (
                        "native-existing-context"
                    )
            bridge.last_message_payload = dict(payload)
            return payload

        bridge.get_messages = trusted_get_messages  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)

        class Lease:
            def update_step(self, _step):
                return None

        result = runner._finish_new_visible_voices_in_current_chat(
            binding=Binding(worker_id="worker-1", worker_token="token", client_instance_id="client-1", run_status="running"),
            target=target,
            target_label="CJMIX01",
            sidecar_payload=initial,
            lease=Lease(),  # type: ignore[arg-type]
            action_cancel_requested=lambda: False,
            enforce_read_targets=False,
            read_run_id="read-mixed",
            excluded_voice_anchor_keys=set(),
            flow_outcomes=FlowOutcomeAccumulator(origin_read_run_id="read-mixed"),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["failure_code"], "VOICE_TRANSCRIBE_TRIGGER_FAILED")
        self.assertEqual(
            {item["result"] for item in result["item_outcomes"]},
            {"completed", "failed"},
            msg={
                "result": result,
                "operations": bridge.c2_operation_order,
                "voice_calls": bridge.voice_transcribes,
            },
        )
        self.assertEqual(len(result["item_outcomes"]), 2)
        self.assertEqual(len(bridge.voice_transcribes), 2)
        self.assertIs(
            result["payload"]["ui_frame_invalidated"],
            True,
        )
        self.assertEqual([call["selected_pre_observation_id"] for call in bridge.voice_transcribes], ["voice-first-failed", "voice-second-success"])

    def test_voice_prepare_empty_cannot_overwrite_same_authoritative_voice(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-prepare-empty-same-voice",
            rpa_session_key="wx:prepare-empty-same-voice",
            display_name="CJEMPTY1",
            remark_code="CJEMPTY1",
            authorization_revision="revision-prepare-empty-same-voice",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused")
        )
        initial = bridge._contractual_message_payload({
            "observations": [{
                "schema_version": 3,
                "observation_id": "voice-still-visible",
                "row_kind": "voice_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "voice",
                "voice_state": "untranscribed",
                "voice_anchor_key": "voice-still-visible",
                "source_message": {
                    "id": "voice-still-visible",
                    "type": "voice",
                    "sender_role": "customer",
                },
            }],
        })

        def invalid_empty_prepare(**_kwargs):
            bridge.c2_operation_order.append("voice_prepare")
            return bridge._contractual_message_payload({
                **initial,
                "ok": True,
                "state": "voice_action_prepare_empty",
                "voice_action_stage": "prepare",
                "pre_frame_id": "prepare-empty-same-frame",
                "ui_action_performed": False,
            })

        bridge.prepare_voice_action = invalid_empty_prepare  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        result = runner._finish_new_visible_voices_in_current_chat(
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target=target,
            target_label="CJEMPTY1",
            sidecar_payload=initial,
            lease=unittest.mock.Mock(),
            action_cancel_requested=lambda: False,
            enforce_read_targets=False,
            read_run_id="read-prepare-empty-same",
            excluded_voice_anchor_keys=set(),
            flow_outcomes=FlowOutcomeAccumulator(
                origin_read_run_id="read-prepare-empty-same"
            ),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "C2_AUTHORITATIVE_FRAME_SOURCE_INVALID",
        )
        self.assertEqual(bridge.voice_transcribes, [])

    def test_voice_prepare_cannot_select_backend_confirmed_historical_voice(
        self,
    ):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-prepare-selected-history",
            rpa_session_key="wx:prepare-selected-history",
            display_name="CJHISTV1",
            remark_code="CJHISTV1",
            authorization_revision="revision-prepare-selected-history",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 2,
                    "recent_messages": [
                        {
                            "stable_id": "worker-message-1",
                            "source_message_key": (
                                "wechat:conv-prepare-selected-history:"
                                "worker_sequence:worker-message-1"
                            ),
                            "origin_read_run_id": "read-history",
                        }
                    ],
                }
            },
        )
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused")
        )
        initial = bridge._contractual_message_payload(
            {
                "observations": [
                    {
                        "schema_version": 3,
                        "observation_id": "historical-voice",
                        "row_kind": "voice_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "voice",
                        "voice_state": "untranscribed",
                        "voice_anchor_key": "historical-anchor",
                        "_worker_stable_id": "worker-message-1",
                        "_worker_identity_scope": "committed",
                        "_worker_committed_message": (
                            committed_identity_record(
                                worker_stable_id="worker-message-1",
                                commit_basis=(
                                    MessageCommitBasis.HISTORICAL_CHECKPOINT_ALIGNMENT
                                ),
                                observation_id="historical-voice",
                                sender_role="customer",
                                message_type="voice",
                                proof={
                                    "alignment_status": "unique",
                                    "pre_observation_id": (
                                        "checkpoint-historical-voice"
                                    ),
                                    "post_observation_id": (
                                        "historical-voice"
                                    ),
                                    "worker_stable_id": (
                                        "worker-message-1"
                                    ),
                                    "match_basis": (
                                        "two_sided_historical_context"
                                    ),
                                },
                            )
                        ),
                        "source_message": {
                            "id": "historical-voice",
                            "type": "voice",
                            "sender_role": "customer",
                        },
                    },
                    {
                        "schema_version": 3,
                        "observation_id": "new-voice",
                        "row_kind": "voice_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "voice",
                        "voice_state": "untranscribed",
                        "voice_anchor_key": "new-anchor",
                        "source_message": {
                            "id": "new-voice",
                            "type": "voice",
                            "sender_role": "customer",
                        },
                    },
                ]
            }
        )
        bridge.last_message_payload = dict(initial)

        def prepare_wrong_historical_voice(**_kwargs):
            historical = dict(initial["observations"][0])
            return bridge._contractual_message_payload(
                {
                    **initial,
                    "ok": True,
                    "state": "voice_action_prepared",
                    "voice_action_stage": "prepare",
                    "pre_frame_id": "prepare-history-frame",
                    "selected_pre_observation_id": "historical-voice",
                    "selected_action_token": "prepare-history-token",
                    "selected_target_fingerprint": (
                        "prepare-history-fingerprint"
                    ),
                    "selected_voice_observation": historical,
                    "selected_physical_anchor_keys": [
                        "historical-anchor"
                    ],
                    "candidate_group_count": 2,
                    "ui_action_performed": False,
                }
            )

        bridge.prepare_voice_action = (  # type: ignore[method-assign]
            prepare_wrong_historical_voice
        )
        runner, _ = self.make_runner(api, bridge)
        result = runner._finish_new_visible_voices_in_current_chat(
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target=target,
            target_label="CJHISTV1",
            sidecar_payload=initial,
            lease=unittest.mock.Mock(),
            action_cancel_requested=lambda: False,
            enforce_read_targets=False,
            read_run_id="read-current",
            excluded_voice_anchor_keys=set(),
            flow_outcomes=FlowOutcomeAccumulator(
                origin_read_run_id="read-current"
            ),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error_code"],
            "C2_VOICE_PREPARE_CONTRACT_INVALID",
        )
        self.assertEqual(bridge.voice_transcribes, [])
        self.assertEqual(
            list_action_journals(
                conversation_id=target.conversation_id,
                action_kinds=("voice",),
            ),
            [],
        )

    def test_voice_prepare_empty_page_change_adopts_complete_final_read(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-prepare-empty-page-change",
            rpa_session_key="wx:prepare-empty-page-change",
            display_name="CJEMPTY2",
            remark_code="CJEMPTY2",
            authorization_revision="revision-prepare-empty-page-change",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused")
        )
        existing_text = {
            "schema_version": 3,
            "observation_id": "existing-context",
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "text",
            "voice_state": "not_voice",
            "content_clean": "稳定上下文",
            "native_source_message_id": "native-stable-context",
            "_worker_stable_id": "worker-message-1",
            "source_message": {
                "id": "existing-context",
                "type": "text",
                "sender_role": "customer",
                "content": "稳定上下文",
            },
        }
        initial = bridge._contractual_message_payload({
            "observations": [
                existing_text,
                {
                    "schema_version": 3,
                    "observation_id": "voice-disappeared",
                    "row_kind": "voice_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "voice",
                    "voice_state": "untranscribed",
                    "voice_anchor_key": "voice-disappeared",
                    "source_message": {
                        "id": "voice-disappeared",
                        "type": "voice",
                        "sender_role": "customer",
                    },
                },
            ],
        })

        def changed_empty_prepare(**_kwargs):
            bridge.c2_operation_order.append("voice_prepare")
            return bridge._contractual_message_payload({
                "ok": True,
                "state": "voice_action_prepare_empty",
                "voice_action_stage": "prepare",
                "pre_frame_id": "prepare-empty-new-frame",
                "observations": [
                    existing_text,
                    {
                        "schema_version": 3,
                        "observation_id": "voice-disappeared",
                        "row_kind": "voice_transcript",
                        "sender_role": "customer",
                        "sender_role_source": "parent_voice",
                        "message_type": "voice",
                        "voice_state": "transcribed",
                        "content_clean": "页面变化后已有转写证据",
                        "parent_voice_anchor_key": "voice-disappeared",
                        "source_message": {
                            "id": "voice-disappeared",
                            "type": "voice",
                            "sender_role": "customer",
                            "content": "页面变化后已有转写证据",
                        },
                    },
                ],
                "ui_action_performed": False,
            })

        bridge.prepare_voice_action = changed_empty_prepare  # type: ignore[method-assign]
        runner, _ = self.make_runner(api, bridge)
        result = runner._finish_new_visible_voices_in_current_chat(
            binding=Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            target=target,
            target_label="CJEMPTY2",
            sidecar_payload=initial,
            lease=unittest.mock.Mock(),
            action_cancel_requested=lambda: False,
            enforce_read_targets=False,
            read_run_id="read-prepare-empty-page-change",
            excluded_voice_anchor_keys=set(),
            flow_outcomes=FlowOutcomeAccumulator(
                origin_read_run_id="read-prepare-empty-page-change"
            ),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["payload"]["authoritative_frame_source"],
            "final_read",
        )
        self.assertIs(
            result["payload"]["ui_frame_invalidated"],
            False,
        )
        self.assertEqual(
            [
                item["observation_id"]
                for item in result["payload"]["observations"]
            ],
            ["existing-context", "voice-disappeared"],
        )
        self.assertEqual(bridge.voice_transcribes, [])

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
            "native_source_message_id": "native-failed-voice",
            "type": "voice",
            "sender_role": "customer",
            "content": '[语音] 5"',
            "bubble_rect": [400, 100, 600, 140],
        }
        failed_observation = bridge._contractual_message_payload(
            {"messages": [failed_message]}
        )["observations"][0]
        failed_observation["_worker_stable_id"] = "worker-message-1"
        failed_observation["_worker_identity_scope"] = "committed"
        failed_observation["native_source_message_id"] = (
            "native-failed-voice"
        )
        failed_observation["_worker_committed_message"] = (
            committed_identity_record(
                worker_stable_id="worker-message-1",
                commit_basis=MessageCommitBasis.NATIVE_SOURCE_MESSAGE_ID,
                observation_id=str(
                    failed_observation.get("observation_id") or ""
                ),
                sender_role="customer",
                message_type="voice",
                proof={
                    "native_source_message_id": "native-failed-voice",
                    "sender_role": "customer",
                    "message_type": "voice",
                },
            )
        )
        failed_source_key = voice_observation_source_key(
            target,
            failed_observation,
        )
        target.raw["identity_checkpoint"] = {
            "version": 2,
            "next_sequence_floor": 2,
            "recent_messages": [
                {
                    "stable_id": "worker-message-1",
                    "source_message_key": failed_source_key,
                    "origin_read_run_id": "read-failed-old-voice",
                    "sender_role": "customer",
                    "message_type": "voice",
                    "normalized_content_hash": "",
                    "native_source_message_id": "native-failed-voice",
                    "frame_visual_id": "",
                }
            ],
        }
        save_c2_ledger_terminal(
            conversation_id=target.conversation_id,
            source_message_key=failed_source_key,
            origin_read_run_id="read-failed-old-voice",
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
        # The backend checkpoint is authoritative proof that this identity
        # was already accepted. Recovery confirms the local waiting ledger
        # without retransmitting the historical failure.
        self.assertEqual(api.message_payloads[0]["messages"], [])
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
            "continuation": {
                "batch_id": "batch-flow-failed",
                "token": "continuation-batch-flow-failed",
                "authorization_revision": "revision-conv-flow-failed",
                "read_reason": "waiting_sales_reply",
            },
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
            },
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

        self.assertEqual(api.message_payloads, [])
        self.assertEqual(
            runner.c2_stats["last_error"],
            "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        )
        journals = list_action_journals(
            conversation_id=target.conversation_id,
            action_kinds=("voice",),
        )
        self.assertEqual(len(journals), 1)
        journal_items = journals[0][1]["items"]
        self.assertEqual(len(journal_items), 1)
        terminal_item = next(iter(journal_items.values()))
        self.assertEqual(
            terminal_item["action_phase"], "quarantined"
        )
        self.assertEqual(
            terminal_item["error_code"],
            "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        )
        self.assertEqual(
            bridge.c2_operation_order,
            [
                "locate_chat",
                "messages",
                "voice_prepare",
                "voice_transcribe",
            ],
        )
        self.assertEqual(
            len(runner.c2_voice_binding_blocked_authorizations), 0
        )

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

        with patch(
            "chejin_worker_client.task_runner."
            "align_committed_message_sequence",
            return_value={
                "pre_sequence_source": "empty_checkpoint",
                "pre_frame_id": "checkpoint:none:conv-cross-round-ambiguous",
                "post_frame_id": "frame:ambiguous-image",
                "alignment_status": "ambiguous",
                "candidate_alignment_count": 2,
                "matched_pairs": [],
                "old_tail_fully_consumed": False,
                "new_suffix_observation_ids": [],
            },
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

    def test_identity_alignment_failure_marks_message_read_timing_failed(self):
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
                        "observation_id": "timing-ambiguous-text",
                        "row_kind": "text_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "text",
                        "voice_state": "not_voice",
                        "content_clean": "当前文字",
                    }
                ],
            }
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-timing-gate",
            worker_token="token",
            client_instance_id="client-timing-gate",
            run_status="running",
        )
        target = WechatReadTarget(
            conversation_id="conv-timing-identity-gate",
            rpa_session_key="",
            display_name="CJTIME01",
            remark_code="CJTIME01",
            authorization_revision="revision-timing-identity-gate",
            raw={"identity_checkpoint": identity_checkpoint()},
        )

        with patch(
            "chejin_worker_client.task_runner.align_committed_message_sequence",
            return_value={
                "pre_sequence_source": "empty_checkpoint",
                "pre_frame_id": "checkpoint:none:timing",
                "post_frame_id": "frame:timing",
                "alignment_status": "ambiguous",
                "candidate_alignment_count": 2,
                "matched_pairs": [],
                "old_tail_fully_consumed": False,
                "new_suffix_observation_ids": [],
            },
        ):
            result = runner._read_one_wechat_target(
                binding,
                target,
                current_step="state_target_message_read",
                enforce_read_targets=False,
            )

        self.assertFalse(result["ok"])
        timing_log = next(
            row
            for row in read_logs(limit=50)
            if row.get("event") == "c2_message_read_timing"
            and (row.get("metadata") or {}).get("conversation_id")
            == target.conversation_id
        )
        read_phase = next(
            phase
            for phase in timing_log["metadata"]["phases"]
            if phase["name"] == "initial_message_read"
        )
        self.assertIs(read_phase["completed"], False)
        self.assertIs(read_phase["failed"], True)
        self.assertEqual(
            read_phase["error_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
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
                c2_outbox_id(payload)
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
        stored = load_c2_outbox_entry(c2_outbox_id(payload))
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
        stored = load_c2_outbox_entry(c2_outbox_id(payload))
        self.assertEqual(stored["status"], "capability_paused")
        self.assertEqual(stored["payload"]["messages"], messages)
        self.assertEqual(api.message_payloads, [])

    def test_same_read_run_submits_each_new_fact_set_through_a_new_outbox(self):
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
        base = {
            "read_run_id": "read-multi-outbox-production",
            "conversation_id": "conv-multi-outbox-production",
            "authorization_revision": "revision-multi-outbox-production",
            "evidence": {"observations": []},
        }
        message_a = {
            "source_message_key": "source-a",
            "dedupe_key": "dedupe-a",
            "sender_role_hint": "customer",
            "message_type": "text",
            "content": "A",
            "item_state": "completed",
            "flow_state": "completed",
        }
        message_b = {
            "source_message_key": "source-b",
            "dedupe_key": "dedupe-b",
            "sender_role_hint": "customer",
            "message_type": "voice",
            "content": "B",
            "item_state": "completed",
            "flow_state": "completed",
            "raw_payload": {
                "voice_transcription_meta": {
                    "state": "voice_transcribe_completed",
                    "canonical_voice_action_id": "voice-action-b",
                    "artifact_dir": "C:/diagnostic-only",
                }
            },
        }
        first_payload = {**base, "messages": [message_a]}
        second_payload = {**base, "messages": [message_a, message_b]}

        first = runner._submit_c2_outbox_payload(
            binding=binding,
            payload=first_payload,
            operation="message_ingest",
        )
        second = runner._submit_c2_outbox_payload(
            binding=binding,
            payload=second_payload,
            operation="message_ingest",
        )

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["outbox_id"], second["outbox_id"])
        self.assertEqual(len(api.message_payloads), 2)
        self.assertEqual(
            [len(payload["messages"]) for payload in api.message_payloads],
            [1, 2],
        )
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id, status
                FROM c2_ingest_outbox
                WHERE read_run_id = ?
                ORDER BY created_at
                """,
                (base["read_run_id"],),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["status"] for row in rows}, {"confirmed"})

    def test_outbox_fact_collision_blocks_backend_delivery(self):
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
        message = {
            "source_message_key": "source-collision",
            "dedupe_key": "dedupe-collision",
            "sender_role_hint": "customer",
            "message_type": "text",
            "content": "original",
            "item_state": "completed",
            "flow_state": "completed",
        }
        payload = {
            "read_run_id": "read-collision-production",
            "conversation_id": "conv-collision-production",
            "authorization_revision": "revision-collision-production",
            "messages": [message],
            "evidence": {"observations": []},
        }
        first = runner._submit_c2_outbox_payload(
            binding=binding,
            payload=payload,
            operation="message_ingest",
        )
        collision = runner._submit_c2_outbox_payload(
            binding=binding,
            payload={
                **payload,
                "messages": [{**message, "content": "changed"}],
            },
            operation="message_ingest",
        )

        self.assertTrue(first["ok"], first)
        self.assertFalse(collision["ok"], collision)
        self.assertEqual(
            collision["error_code"],
            "C2_OUTBOX_LOGICAL_FACT_COLLISION",
        )
        self.assertEqual(len(api.message_payloads), 1)
        stored = load_c2_outbox_entry(first["outbox_id"])
        self.assertEqual(
            stored["payload"]["messages"][0]["content"],
            "original",
        )

    def test_confirmed_outbox_retry_reuses_id_without_second_backend_call(self):
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
        message = {
            "source_message_key": "source-retry-confirmed",
            "dedupe_key": "dedupe-retry-confirmed",
            "sender_role_hint": "customer",
            "message_type": "text",
            "content": "same fact",
            "item_state": "completed",
            "flow_state": "completed",
        }
        payload = {
            "read_run_id": "read-retry-confirmed",
            "conversation_id": "conv-retry-confirmed",
            "authorization_revision": "revision-original",
            "messages": [message],
            "evidence": {
                "observations": [],
                "timing": {"elapsed": 1.0},
            },
        }
        first = runner._submit_c2_outbox_payload(
            binding=binding,
            payload=payload,
            operation="message_ingest",
        )
        retried = runner._submit_c2_outbox_payload(
            binding=binding,
            payload={
                **payload,
                "authorization_revision": "revision-retried",
                "evidence": {
                    **payload["evidence"],
                    "timing": {"elapsed": 99.0},
                },
            },
            operation="message_ingest",
        )

        self.assertTrue(first["ok"], first)
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(first["outbox_id"], retried["outbox_id"])
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            load_c2_outbox_entry(first["outbox_id"])["status"],
            "confirmed",
        )

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
            [
                "locate_chat",
                "messages",
                "voice_prepare",
                "voice_transcribe",
            ],
        )
        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(len(bridge.message_reads), 1)
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

    def test_c2_voice_sidecar_timeout_fails_closed_without_reclick_or_ingest(self):
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
            [
                "locate_chat",
                "messages",
                "voice_prepare",
                "voice_transcribe",
            ],
        )
        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(api.message_payloads, [])
        self.assertEqual(
            runner.c2_stats["last_error"],
            "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        )
        journals = list_action_journals(
            conversation_id="conv-1",
            action_kinds=("voice",),
        )
        self.assertEqual(len(journals), 1)
        self.assertEqual(
            action_journal_phase(journals[0][0]),
            "quarantined",
        )
        self.assertEqual(
            list_c2_ledger_entries("conv-1", message_type="voice"),
            [],
        )

    def test_voice_execute_exception_without_sidecar_journal_update_is_quarantined(self):
        target = WechatReadTarget(
            conversation_id="conv-voice-execute-exception",
            rpa_session_key="wx:rpa:v1:voice-execute-exception",
            display_name="CJERR001",
            remark_code="CJERR001",
            authorization_revision="revision-voice-execute-exception",
            raw={"identity_checkpoint": identity_checkpoint()},
        )

        class CrashingBridge(FakeBridge):
            def execute_voice_action(self, **kwargs):
                self.c2_operation_order.append("voice_transcribe")
                self.voice_transcribes.append(dict(kwargs))
                raise TimeoutError("sidecar disappeared after execute began")

        bridge = CrashingBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        initial = bridge._contractual_message_payload(
            {
                "ok": True,
                "state": "messages_ocr",
                "sidecar_run_id": "frame-before-execute-exception",
                "messages": [
                    {
                        "id": "voice-execute-exception",
                        "type": "voice",
                        "sender_role": "customer",
                        "content": '[语音] 3"',
                        "voice_anchor_stable_key": "voice-execute-exception",
                        "bubble_rect": [420, 180, 650, 240],
                    }
                ],
            }
        )
        for item in initial["observations"]:
            item["sender_role_source"] = "same_row_avatar"
        bridge.last_message_payload = dict(initial)
        runner, _ = self.make_runner(FakeApi(None), bridge)

        class Lease:
            def update_step(self, _step):
                return None

        with self.assertRaisesRegex(
            TimeoutError,
            "sidecar disappeared",
        ):
            runner._finish_new_visible_voices_in_current_chat(
                binding=Binding(
                    worker_id="worker-1",
                    worker_token="token",
                    client_instance_id="client-1",
                    run_status="running",
                ),
                target=target,
                target_label="CJERR001",
                sidecar_payload=initial,
                lease=Lease(),  # type: ignore[arg-type]
                action_cancel_requested=lambda: False,
                enforce_read_targets=False,
                read_run_id="read-voice-execute-exception",
                excluded_voice_anchor_keys=set(),
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-voice-execute-exception"
                ),
            )

        journals = list_action_journals(
            conversation_id=target.conversation_id,
            action_kinds=("voice",),
        )
        self.assertEqual(len(journals), 1)
        self.assertEqual(action_journal_phase(journals[0][0]), "quarantined")
        item = next(iter(journals[0][1]["items"].values()))
        self.assertEqual(item["error_code"], "C2_VOICE_EXECUTE_INTERRUPTED")

    def test_c2_voice_timeout_cannot_bind_transcript_from_unowned_later_frame(self):
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

        self.assertEqual(api.message_payloads, [])
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(len(bridge.get_messages_payloads), 1)
        self.assertEqual(
            runner.c2_stats["last_error"],
            "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        )
        journals = list_action_journals(
            conversation_id=target.conversation_id,
            action_kinds=("voice",),
        )
        self.assertEqual(len(journals), 1)
        self.assertEqual(
            action_journal_phase(journals[0][0]),
            "quarantined",
        )
        self.assertEqual(
            list_c2_ledger_entries(
                target.conversation_id,
                message_type="voice",
            ),
            [],
        )

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
        self.assertEqual(
            list_c2_ledger_entries(
                target.conversation_id,
                message_type="voice",
            ),
            [],
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

        self.assertFalse(result.get("ok"), result)
        self.assertEqual(
            result.get("error_code"),
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        )
        # The pre-trigger failure invalidates the provisional reservation and
        # requires one fresh authoritative read before the flow may settle.
        self.assertEqual(len(bridge.message_reads), 1)
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
            "frame_visual_id": f"canonical-image-{unique}",
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
            self.assertFalse(first_result.get("ok"), first_result)
            self.assertEqual(
                first_result.get("error_code"),
                "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
            )
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
        observation = {
            "schema_version": 3,
            "observation_id": f"image-adapter-exception-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "_worker_stable_id": f"worker-image-adapter-{unique}",
            "bubble_rect": [420, 180, 650, 320],
            "image_physical_anchor": {
                "sender_role": "customer",
                "bubble_visual_fingerprint": "dhash64:0123456789abcdef",
            },
        }
        # Before a confirmed image receipt this is only the ActionJournal
        # item id; no durable source_message_key exists yet.
        source_key = f"image-action-adapter-{unique}"

        def crash_after_trigger(**kwargs):
            update_action_journal_item(
                kwargs["action_journal_path"],
                journal_item_id=kwargs["action_local_id"],
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
                payload={
                    "frame_id": f"frame-image-adapter-{unique}",
                    "observations": [observation],
                },
                observation=observation,
                action_local_id=source_key,
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
        self.assertEqual(item["action_phase"], "quarantined")
        self.assertEqual(item["business_state"], "failed")
        self.assertEqual(
            item["error_code"],
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        )
        self.assertEqual(
            item["terminal_payload"]["state"],
            "failed",
        )
        self.assertEqual(
            item["terminal_payload"]["media_action_terminal"],
            "identity_unresolved",
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

        self.assertEqual(
            bridge.calibration_prepare_calls,
            2,
            "C2 scan and subsequent read are two UI transactions and must each reuse the startup calibration gate",
        )
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

    def test_worker_passes_omniauto_visible_frame_evidence_without_interpreting_it(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        evidence = {
            "frame_id": "frame-1",
            "scan_id": "scan-1",
            "sidecar_run_id": "sessions-1",
            "sidebar_sha256": "a" * 64,
            "opaque_future_field": {"must": "survive"},
        }
        candidate = runner._sidecar_visible_session_candidate(
            {
                "name": "CJP6M3R7许聪",
                "session_key": "wx:rpa:v1:one",
                "c2_conversation_type": "private",
                "c2_remark_code_candidates": ["CJP6M3R7"],
                "c2_conversation_admission": {
                    "conversation_type": "private",
                    "admission_allowed": True,
                    "remark_code": "CJP6M3R7",
                },
                "visible_frame_reuse_evidence": evidence,
                "center_y": 120,
            }
        )

        self.assertEqual(candidate["visible_frame_reuse_evidence"], evidence)
        self.assertEqual(
            candidate["visible_frame_reuse_evidence"]["opaque_future_field"],
            {"must": "survive"},
        )

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

    def test_c2_visible_stale_after_click_reauthorizes_and_relocates_once(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-visible-reordered",
            rpa_session_key="wx:rpa:v1:before-reorder",
            display_name="CJK7M4Q2",
            remark_code="CJK7M4Q2",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-visible-reordered",
        )
        api.read_targets = [target]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.locate_payloads = [
            {
                "ok": False,
                "state": "target_not_confirmed",
                "error_code": "C2_VISIBLE_TARGET_STALE_AFTER_CLICK",
                "target_mode": "visible",
                "targeting": {
                    "stale_after_click": {
                        "ui_click_performed": True,
                        "active_title_nonempty": True,
                        "target_remark_code_confirmed": False,
                        "message_read_attempted": False,
                        "media_action_attempted": False,
                        "input_or_send_attempted": False,
                        "safe_relocation_allowed": True,
                    }
                },
            },
            {
                "ok": True,
                "state": "chat_target_confirmed",
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
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertGreaterEqual(
            api.events.count("read_authorization:conv-visible-reordered"),
            3,
        )

    def test_c2_visible_stale_after_click_does_not_relocate_after_revocation(self):
        api = FakeApi(None)
        target = WechatReadTarget(
            conversation_id="conv-visible-reordered-revoked",
            rpa_session_key="wx:rpa:v1:before-reorder",
            display_name="CJK7M4Q2",
            remark_code="CJK7M4Q2",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-visible-reordered-revoked",
        )
        api.read_targets = [target]
        authorization_calls = {"count": 0}

        def revoke_before_relocation(
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

        api.get_wechat_read_authorization = revoke_before_relocation  # type: ignore[method-assign]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.locate_payloads = [
            {
                "ok": False,
                "state": "target_not_confirmed",
                "error_code": "C2_VISIBLE_TARGET_STALE_AFTER_CLICK",
                "target_mode": "visible",
                "targeting": {
                    "stale_after_click": {
                        "ui_click_performed": True,
                        "active_title_nonempty": True,
                        "target_remark_code_confirmed": False,
                        "message_read_attempted": False,
                        "media_action_attempted": False,
                        "input_or_send_attempted": False,
                        "safe_relocation_allowed": True,
                    }
                },
            }
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
            ["visible"],
        )
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(api.message_payloads, [])

    def test_c2_second_wrong_relocation_ends_current_target_and_continues_queue(self):
        api = FakeApi(None)
        first = WechatReadTarget(
            conversation_id="conv-reordered-first",
            rpa_session_key="wx:rpa:v1:before-reorder",
            display_name="CJK7M4Q2",
            remark_code="CJK7M4Q2",
            read_reason="waiting_sales_reply",
            authorization_revision="revision-reordered-first",
        )
        second = WechatReadTarget(
            conversation_id="conv-after-reordered",
            rpa_session_key="wx:rpa:v1:next-target",
            display_name="CJV6P3R8",
            remark_code="CJV6P3R8",
            read_reason="waiting_user_reply",
            authorization_revision="revision-after-reordered",
        )
        api.read_targets = [first, second]
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        bridge.locate_payloads = [
            {
                "ok": False,
                "state": "target_not_confirmed",
                "error_code": "C2_VISIBLE_TARGET_STALE_AFTER_CLICK",
                "target_mode": "visible",
                "targeting": {
                    "stale_after_click": {
                        "ui_click_performed": True,
                        "active_title_nonempty": True,
                        "target_remark_code_confirmed": False,
                        "message_read_attempted": False,
                        "media_action_attempted": False,
                        "input_or_send_attempted": False,
                        "safe_relocation_allowed": True,
                    }
                },
            },
            {
                "ok": False,
                "state": "target_not_confirmed",
                "error_code": "TARGET_NOT_CONFIRMED",
                "target_mode": "search_by_remark_code",
            },
            {
                "ok": True,
                "state": "chat_target_confirmed",
                "target_mode": "visible",
            },
        ]
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner._read_state_target_queue(binding, targets=[first, second])

        self.assertEqual(
            [item["target_mode"] for item in bridge.locate_chats],
            ["visible", "search_by_remark_code", "visible"],
        )
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            api.message_payloads[0]["conversation_id"],
            "conv-after-reordered",
        )

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

                emitted_timings: list[dict] = []
                with (
                    patch(
                        "chejin_worker_client.task_runner.load_process_run",
                        return_value="process-run-c2-locate-failure",
                    ),
                    patch(
                        "chejin_worker_client.task_runner.enqueue_c2_flow_timing_stages",
                        side_effect=lambda **kwargs: emitted_timings.append(
                            kwargs["flow_timing"]
                        )
                        or [],
                    ),
                    patch(
                        "chejin_worker_client.task_runner.schedule_stage_event_upload"
                    ),
                ):
                    runner._read_state_target_queue(binding, targets=[target])

                self.assertEqual(
                    [item["target_mode"] for item in bridge.locate_chats],
                    ["visible", "search_by_remark_code"],
                )
                self.assertEqual(bridge.message_reads, [])
                locate_phases = [
                    phase
                    for timing in emitted_timings
                    for phase in timing.get("phases", [])
                    if phase.get("name") == "target_chat_locate"
                ]
                self.assertEqual(len(locate_phases), 2)
                self.assertTrue(all(phase.get("failed") is True for phase in locate_phases))
                self.assertTrue(all(phase.get("completed") is False for phase in locate_phases))
                self.assertEqual(locate_phases[-1].get("error_code"), error_code)
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
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": "已完成转写",
                        "voice_anchor_stable_key": "wx-msg-voice-raw",
                    }
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "state": "voice_transcribe_completed",
            "processed_voice_anchor_keys": ["wx-msg-voice-raw"],
            "transcribed_messages": [
                {
                    "content": "已完成转写",
                    "sender_role": "customer",
                    "voice_anchor_stable_key": "wx-msg-voice-raw",
                }
            ],
        }
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
                "allowed": not bridge.voice_transcribes,
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
            },
            {
                "ok": True,
                "messages": [
                    {
                        "id": "wx-msg-voice-raw",
                        "type": "voice",
                        "sender_role": "customer",
                        "voice_duration": 2,
                        "content": "已完成转写",
                        "voice_anchor_stable_key": "wx-msg-voice-raw",
                    }
                ],
            },
        ]
        bridge.voice_payload = {
            "ok": True,
            "state": "voice_transcribe_completed",
            "processed_voice_anchor_keys": ["wx-msg-voice-raw"],
            "transcribed_messages": [
                {
                    "content": "已完成转写",
                    "sender_role": "customer",
                    "voice_anchor_stable_key": "wx-msg-voice-raw",
                }
            ],
        }
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
                "allowed": not bridge.voice_transcribes,
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

    def test_recent_ai_sent_read_carries_confirmed_text_through_locate_and_messages(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        conversation_id = "conv-recent-ai-reread-plumbing"
        reply_text = (
            "你好，10万左右可以先按你的用车需求筛选合适车型。"
            "你主要是日常通勤、家庭出行，还是更看重大空间？"
        )
        runner, _ = self.make_runner(api, bridge)
        reply_hash = runner._reply_text_hash(reply_text)
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key="wx:rpa:v1:recent-ai-reread",
            display_name="CJNCXB8R",
            remark_code="CJNCXB8R",
            read_reason="recent_ai_sent",
            authorization_revision="revision-recent-ai-reread",
            raw={
                "identity_checkpoint": identity_checkpoint(),
                "ai_reply_boundary": {
                    "reply_action_id": "reply-recent-ai-reread",
                    "reply_text_hash": reply_hash,
                },
            },
        )
        api.read_targets = [target]
        save_c2_state(
            f"message_identity:{conversation_id}",
            {
                "version": 4,
                "ai_reply_receipts": [
                    {
                        "reply_action_id": "reply-recent-ai-reread",
                        "reply_text": reply_text,
                        "reply_text_hash": reply_hash,
                        "worker_stable_id": "worker-message-8",
                    }
                ],
            },
        )
        binding = Binding(
            worker_id="worker-1",
            worker_token="token",
            client_instance_id="client-1",
            run_status="running",
        )

        runner._read_state_target_queue(binding, targets=[target])

        self.assertEqual(len(bridge.locate_chats), 1)
        self.assertEqual(len(bridge.message_reads), 1)
        self.assertEqual(
            bridge.locate_chats[0]["expected_confirmed_self_text"],
            reply_text,
        )
        self.assertEqual(
            bridge.message_reads[0]["expected_confirmed_self_text"],
            reply_text,
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

    def test_c2_outbox_quarantines_identity_collision_without_rekey_or_ui(self):
        api = FakeApi(None)
        attempts = 0
        old_source_key = "source-old-collision"
        old_dedupe_key = "dedupe-old-collision"

        def collide(_binding, _payload):
            nonlocal attempts
            attempts += 1
            raise ApiError(
                "MESSAGE_IDENTITY_COLLISION",
                "collision",
                409,
                {
                    "recovery_action": "identity_quarantined",
                    "source_message_key": old_source_key,
                    "dedupe_key": old_dedupe_key,
                    "next_sequence_floor": 8,
                },
            )

        api.post_wechat_messages_ingest = collide  # type: ignore[method-assign]
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
        quarantined = load_c2_outbox_entry(outbox_id)
        self.assertEqual(quarantined["status"], "identity_quarantined")
        original = quarantined["payload"]["messages"][0]
        self.assertEqual(original["source_message_key"], old_source_key)
        self.assertEqual(
            original["raw_payload"]["dedupe_basis"][
                "worker_stable_id"
            ],
            "worker-message-1",
        )
        self.assertIsNotNone(
            load_c2_ledger_entry("conv-collision", old_source_key)
        )
        self.assertEqual(bridge.message_reads, [])
        self.assertEqual(bridge.locate_chats, [])
        self.assertEqual(bridge.voice_transcribes, [])

        self.assertTrue(runner._replay_c2_outbox(binding))
        self.assertEqual(attempts, 1)

    def test_non_rekeyable_identity_collision_is_quarantined_once(self):
        api = FakeApi(None)
        attempts = 0

        def reject(_binding, _payload):
            nonlocal attempts
            attempts += 1
            raise ApiError(
                "MESSAGE_IDENTITY_COLLISION_NOT_REKEYABLE",
                "collision cannot be rekeyed",
                409,
                {"existing_identity": "old", "incoming_identity": "new"},
            )

        api.post_wechat_messages_ingest = reject  # type: ignore[method-assign]
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
        conversation_id = f"conv-nonrekeyable-{time.time_ns()}"
        outbox_id = enqueue_c2_outbox(
            {
                "read_run_id": f"read-{time.time_ns()}",
                "conversation_id": conversation_id,
                "authorization_revision": "revision-1",
                "messages": [
                    {
                        "source_message_key": "source-collision",
                        "dedupe_key": "dedupe-collision",
                        "sender_role_hint": "customer",
                        "message_type": "text",
                        "content": "新消息",
                    }
                ],
            }
        )

        self.assertFalse(runner._replay_c2_outbox(binding))
        self.assertEqual(attempts, 1)
        self.assertEqual(
            load_c2_outbox_entry(outbox_id)["status"],
            "identity_quarantined",
        )
        self.assertFalse(has_pending_c2_outbox())
        quarantine = load_c2_state(
            f"identity_quarantine:{conversation_id}"
        )
        self.assertTrue(quarantine["active"])
        self.assertEqual(quarantine["outbox_id"], outbox_id)

        self.assertTrue(runner._replay_c2_outbox(binding))
        self.assertEqual(attempts, 1)

    def test_same_frame_ocr_and_visual_voice_aliases_collapse_once(self):
        common = {
            "row_kind": "voice_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "bubble_rect": [400, 100, 600, 140],
        }
        collapsed = collapse_same_frame_voice_aliases(
            [
                {
                    **common,
                    "observation_id": "ocr-voice",
                    "voice_anchor_stable_key": "stable-anchor",
                },
                {
                    **common,
                    "observation_id": "visual-voice",
                    "voice_anchor_structural_key": "structural-anchor",
                    "quality_flags": ["visual_voice_hint"],
                },
            ]
        )

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(
            set(collapsed[0]["_voice_action_anchor_keys"]),
            {"stable-anchor", "structural-anchor"},
        )

    def test_identity_quarantine_skips_only_affected_conversation(self):
        api = FakeApi(None)
        quarantined = WechatReadTarget(
            conversation_id="conv-quarantined",
            rpa_session_key="wx:rpa:v1:quarantined",
            display_name="CJQUAR01",
            remark_code="CJQUAR01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-quarantined",
        )
        healthy = WechatReadTarget(
            conversation_id="conv-healthy",
            rpa_session_key="wx:rpa:v1:healthy",
            display_name="CJNEXT01",
            remark_code="CJNEXT01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-healthy",
        )
        api.read_targets = [quarantined, healthy]
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        save_c2_state(
            "identity_quarantine:conv-quarantined",
            {
                "active": True,
                "error_code": "MESSAGE_IDENTITY_COLLISION_NOT_REKEYABLE",
                "outbox_id": "outbox-quarantined",
            },
        )
        processed: list[str] = []

        def read_one(_binding, target, **_kwargs):
            processed.append(target.conversation_id)
            return {"ok": True}

        runner._read_one_wechat_target = read_one  # type: ignore[method-assign]
        runner._read_state_target_queue(
            Binding(
                worker_id="worker-1",
                worker_token="token",
                client_instance_id="client-1",
                run_status="running",
            ),
            targets=[quarantined, healthy],
        )

        self.assertEqual(processed, ["conv-healthy"])

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
            prefix="chejin-missing-identity-db-",
            # Windows runners can retain a transient SQLite/WAL handle after
            # the assertions have completed.  Cleanup is not the behavior
            # under test and must not turn a passing identity test red.
            ignore_cleanup_errors=True,
        ) as temp_dir:
            app_dir = Path(temp_dir)
            missing_db = app_dir / "worker_client.sqlite3"
            self.assertFalse(missing_db.exists())
            with patch.object(storage, "APP_DIR", app_dir), patch.object(
                storage,
                "DB_FILE",
                missing_db,
            ):
                reserved = runner._reserve_worker_sequence(
                    target,
                    reservation_key=(
                        "committed:read-after-db-loss:"
                        "new-after-db-loss"
                    ),
                )
                state = load_c2_state(
                    f"message_identity:{target.conversation_id}"
                )
                self.assertTrue(missing_db.exists())

        self.assertEqual(reserved, "worker-message-31")
        self.assertEqual(state["next_sequence"], 32)

    def test_stale_server_floor_cannot_reuse_recent_committed_sequence(self):
        api = FakeApi(None)
        runner, _ = self.make_runner(
            api,
            FakeBridge(RpaResult(ok=True, result_code="unused", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-stale-floor-{time.time_ns()}",
            rpa_session_key="wx:stale-floor",
            display_name="CJSAFE01 测试客户",
            remark_code="CJSAFE01",
            read_reason="waiting_user_reply",
            authorization_revision="revision-stale-floor",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 3,
                    "recent_messages": [
                        {"stable_id": "worker-message-8"},
                        {"stable_id": "worker-message-12"},
                    ],
                }
            },
        )

        reserved = runner._reserve_worker_sequence(
            target,
            reservation_key="selected-action:stale-floor",
        )

        self.assertEqual(reserved, "worker-message-13")

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
        result_lock = threading.Lock()

        def allocate(index: int) -> None:
            barrier.wait()
            reserved = runner._reserve_worker_sequence(
                target,
                reservation_key=(
                    f"committed:read-concurrent:concurrent-{index}"
                ),
            )
            with result_lock:
                assigned.append(reserved)

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

    def test_authorization_refresh_never_rekeys_committed_ledger_identity(self):
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
        old_ledger_before = load_c2_ledger_entry(
            conversation_id,
            old_source_key,
        )
        read_run_id = f"read-rollback-{time.time_ns()}"
        original_payload = {
            "read_run_id": read_run_id,
            "conversation_id": conversation_id,
            "authorization_revision": "revision-1",
            "messages": [
                {
                    "source_message_key": old_source_key,
                    "dedupe_key": "dedupe-old",
                    "sender_role_hint": "customer",
                    "message_type": "text",
                    "content": "original fact",
                    "item_state": "completed",
                    "flow_state": "completed",
                    "raw_payload": {},
                }
            ],
        }
        outbox_id = enqueue_c2_outbox(
            original_payload
        )
        outbox_before = load_c2_outbox_entry(outbox_id)

        with self.assertRaisesRegex(
            ValueError,
            "C2_OUTBOX_IDENTITY_MISMATCH",
        ):
            refresh_c2_outbox_payload(
                outbox_id,
                {
                    "read_run_id": "read-rollback-refreshed",
                    "conversation_id": conversation_id,
                    "authorization_revision": "revision-2",
                    "messages": original_payload["messages"],
                },
                next_status="waiting",
            )

        self.assertEqual(
            load_c2_outbox_entry(outbox_id),
            outbox_before,
        )
        self.assertEqual(
            load_c2_ledger_entry(conversation_id, old_source_key),
            old_ledger_before,
        )
        self.assertIsNone(
            load_c2_ledger_entry(conversation_id, new_source_key)
        )

    def test_c2_outbox_quarantines_invalid_voice_without_rewriting_fact(self):
        api = FakeApi(None)

        def reject_invalid_payload(_binding, _payload):
            raise ApiError(
                "VOICE_TRANSCRIBE_INVALID_CONTENT",
                "invalid voice payload",
                409,
                {
                    "retryable": False,
                    "recovery_action": "identity_quarantined",
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

        original_payload = copy.deepcopy(
            load_c2_outbox_entry(outbox_id)["payload"]
        )

        assert runner._replay_c2_outbox(binding) is False
        stored = load_c2_outbox_entry(outbox_id)
        self.assertEqual(stored["status"], "identity_quarantined")
        self.assertEqual(stored["payload"], original_payload)
        self.assertEqual(
            stored["payload"]["messages"][0]["source_message_key"],
            source_key,
        )
        self.assertEqual(
            stored["payload"]["messages"][0]["item_state"],
            "completed",
        )
        self.assertEqual(
            stored["payload"]["messages"][0]["content"],
            '5"',
        )
        original_slot = stored["payload"]["evidence"][
            "slot_ledger_states"
        ][0]
        self.assertEqual(original_slot["item_state"], "completed")
        self.assertEqual(
            original_slot["origin_read_run_id"],
            original_read_run_id,
        )
        self.assertEqual(
            load_c2_ledger_entry(
                "conv-outbox-invalid",
                source_key,
            )["ingest_state"],
            "waiting",
        )
        quarantine = load_c2_state("identity_quarantine:conv-outbox-invalid")
        self.assertTrue(quarantine["active"])
        self.assertEqual(quarantine["outbox_id"], outbox_id)

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
            "frame_id": f"frame-image-cache-{unique}",
            "authoritative_frame_source": "initial_read",
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
                    "_worker_stable_id": "worker-message-1",
                    "_worker_identity_scope": "current_read_provisional",
                    "frame_visual_id": f"visual-image-{unique}",
                    "image_physical_anchor": {
                        "sender_role": "customer",
                        "preceding_stable_message": f"before-{unique}",
                        "following_stable_message": f"after-{unique}",
                        "bubble_visual_fingerprint": (
                            "dhash64:0123456789abcdef"
                        ),
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
            "transaction": {
                "action_phase": "confirmed",
                "slot_identity_confirmed": True,
                "clipboard_image_matches_target": True,
                "image_sha256": "a" * 64,
                "image_bytes": "must-not-persist",
            },
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
                sidecar_payload=first,
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
        source_key = image_observation_source_key(
            target,
            first["observations"][0],
        )
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

    def test_completed_image_without_confirmed_receipt_never_reaches_ledger(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-image-receipt-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:image-receipt",
            display_name="CJIMG003",
            remark_code="CJIMG003",
            authorization_revision="revision-image-receipt",
        )
        observation = {
            "schema_version": 3,
            "observation_id": "image-missing-receipt",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "_worker_stable_id": "worker-message-3",
            "_worker_identity_scope": "current_read_provisional",
            "image_physical_anchor": {
                "sender_role": "customer",
                "bubble_visual_fingerprint": (
                    "dhash64:0123456789abcdef"
                ),
            },
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {
                "id": "image-missing-receipt",
                "type": "image",
            },
        }
        sidecar_payload = {
            "frame_id": "frame-image-missing-receipt",
            "authoritative_frame_source": "initial_read",
            "observations": [observation],
        }
        completed_without_identity = {
            "state": "completed",
            "action_phase": "confirmed",
            "business_state": "completed",
            "business_result_confirmed": True,
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "不应入库的图片正文",
            },
            "visual_bridge_input": {"summary": "不应入库"},
            "transaction": {
                "action_phase": "confirmed",
                # Deliberately missing slot_identity_confirmed.
                "image_sha256": "a" * 64,
            },
            "diagnostics": {"events": [], "image_persisted": False},
        }

        with patch(
            "chejin_worker_client.omniauto_vision.process_image_slot",
            return_value=completed_without_identity,
        ) as vision, patch(
            "chejin_worker_client.task_runner.load_c2_ledger_entry",
            side_effect=AssertionError(
                "provisional image queried the durable Ledger"
            ),
        ) as ledger_lookup:
            payload, stats = runner._process_final_image_slots(
                binding=Binding(
                    worker_id="worker-1",
                    worker_token="token",
                    client_instance_id="client-1",
                    run_status="running",
                ),
                target=target,
                sidecar_payload=sidecar_payload,
                enforce_read_targets=False,
                allowed_new_observation_ids={"image-missing-receipt"},
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-missing-receipt"
                ),
            )

        self.assertEqual(vision.call_count, 1)
        self.assertEqual(ledger_lookup.call_count, 0)
        self.assertEqual(payload["observations"], [])
        self.assertEqual(stats["completed"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(
            stats["terminal_gate"]["error_code"],
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        )
        self.assertEqual(
            list_c2_ledger_entries(
                target.conversation_id,
                message_type="image",
            ),
            [],
        )

    def test_invalid_image_scope_fails_before_actions_or_durable_consumers(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        for scope in (None, "", "unknown"):
            with self.subTest(scope=scope):
                unique = f"{scope!r}-{time.time_ns()}"
                target = WechatReadTarget(
                    conversation_id=f"conv-image-scope-{unique}",
                    rpa_session_key="wx:rpa:v1:image-scope",
                    display_name="CJIMG016",
                    remark_code="CJIMG016",
                    authorization_revision=f"revision-image-scope-{unique}",
                )
                observation = {
                    "schema_version": 3,
                    "observation_id": f"image-scope-{unique}",
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "image",
                    "voice_state": "not_voice",
                    "item_state": "completed",
                    "_worker_stable_id": "worker-message-16",
                    "image_physical_anchor": {
                        "sender_role": "customer",
                        "bubble_visual_fingerprint": (
                            "dhash64:0123456789abcdef"
                        ),
                    },
                    "source_message": {
                        "id": f"image-scope-{unique}",
                        "type": "image",
                    },
                }
                if scope is not None:
                    observation["_worker_identity_scope"] = scope
                with patch.object(
                    runner,
                    "_execute_one_image_slot_vision",
                ) as execute, patch(
                    "chejin_worker_client.task_runner.load_c2_ledger_entry",
                    side_effect=AssertionError(
                        "invalid image queried Ledger"
                    ),
                ) as ledger_lookup, patch(
                    "chejin_worker_client.task_runner.load_c2_outbox_origin_read_run_ids",
                    side_effect=AssertionError(
                        "invalid image queried Outbox"
                    ),
                ) as outbox_lookup:
                    payload, stats = runner._process_final_image_slots(
                        binding=Binding(
                            worker_id="worker-1",
                            worker_token="token",
                            client_instance_id="client-1",
                            run_status="running",
                        ),
                        target=target,
                        sidecar_payload={
                            "authoritative_frame_source": "initial_read",
                            "observations": [observation],
                        },
                        enforce_read_targets=False,
                        allowed_new_observation_ids={
                            str(observation["observation_id"])
                        },
                        flow_outcomes=FlowOutcomeAccumulator(
                            origin_read_run_id=f"read-image-scope-{unique}"
                        ),
                    )

                self.assertEqual(execute.call_count, 0)
                self.assertEqual(ledger_lookup.call_count, 0)
                self.assertEqual(outbox_lookup.call_count, 0)
                self.assertEqual(payload["observations"], [])
                self.assertEqual(
                    stats["terminal_gate"]["error_code"],
                    "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                )
                self.assertEqual(
                    list_c2_ledger_entries(
                        target.conversation_id,
                        message_type="image",
                    ),
                    [],
                )
                with patch(
                    "chejin_worker_client.task_runner.load_c2_ledger_entry",
                    side_effect=AssertionError(
                        "invalid image plan queried Ledger"
                    ),
                ) as plan_ledger_lookup, patch(
                    "chejin_worker_client.task_runner.load_c2_outbox_origin_read_run_ids",
                    side_effect=AssertionError(
                        "invalid image plan queried Outbox"
                    ),
                ) as plan_outbox_lookup:
                    plan = runner._build_final_slot_incremental_plan(
                        target=target,
                        sidecar_payload={
                            "observation_schema_version": 3,
                            "authoritative_frame_source": "initial_read",
                            "observations": [observation],
                            "sequence_alignment_evidence": {
                                "pre_sequence_source": "empty_checkpoint",
                                "pre_frame_id": "checkpoint:none",
                                "post_frame_id": "frame:image-invalid-scope",
                                "alignment_status": "not_required",
                                "candidate_alignment_count": 0,
                                "matched_pairs": [],
                                "old_tail_fully_consumed": True,
                                "new_suffix_observation_ids": [],
                            },
                        },
                        read_run_id=f"read-image-plan-{unique}",
                    )
                self.assertEqual(plan_ledger_lookup.call_count, 0)
                self.assertEqual(plan_outbox_lookup.call_count, 0)
                self.assertEqual(
                    plan["identity_errors"][0]["error_code"],
                    "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                )

    def test_noncommitted_runtime_object_matrix_reaches_no_durable_consumer(self):
        for object_type in (
            "frame_observation",
            "pending_media_action",
            "quarantine_record",
            "",
            "unknown",
        ):
            with self.subTest(object_type=object_type):
                unique = f"{object_type or 'blank'}-{time.time_ns()}"
                api = FakeApi(None)
                observation = attach_native_committed_identity(
                    {
                        "schema_version": 3,
                        "observation_id": f"image-object-{unique}",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                        "item_state": "completed",
                        "image_physical_anchor": {
                            "sender_role": "customer",
                            "bubble_visual_fingerprint": (
                                "dhash64:0123456789abcdef"
                            ),
                        },
                        "source_message": {
                            "id": f"native-image-{unique}",
                            "type": "image",
                        },
                    },
                    worker_stable_id="worker-message-1",
                    native_source_message_id=f"native-image-{unique}",
                )
                observation["_worker_committed_message"]["object_type"] = (
                    object_type
                )
                frame = {
                    "ok": True,
                    "frame_id": f"frame-object-{unique}",
                    "observation_schema_version": 3,
                    "authoritative_frame_source": "initial_read",
                    "observations": [observation],
                    "sequence_alignment_evidence": {
                        "pre_sequence_source": "empty_checkpoint",
                        "pre_frame_id": "checkpoint:none",
                        "post_frame_id": f"frame-object-{unique}",
                        "alignment_status": "not_required",
                        "candidate_alignment_count": 0,
                        "matched_pairs": [],
                        "old_tail_fully_consumed": True,
                        "new_suffix_observation_ids": [],
                    },
                }
                bridge = FakeBridge(
                    RpaResult(ok=True, result_code="ok", message="unused")
                )
                bridge.get_messages_payloads = [frame]
                runner, _ = self.make_runner(api, bridge)
                binding = Binding(
                    worker_id="worker-identity-matrix",
                    worker_token="token",
                    client_instance_id="client-identity-matrix",
                    run_status="running",
                )
                target = WechatReadTarget(
                    conversation_id=f"conv-object-{unique}",
                    rpa_session_key="wx:rpa:v1:identity-matrix",
                    display_name="CJIDM001",
                    remark_code="CJIDM001",
                    authorization_revision=f"revision-{unique}",
                    raw={"identity_checkpoint": identity_checkpoint()},
                )
                with patch.object(
                    runner,
                    "_align_initial_identity_frame",
                    return_value=(frame, []),
                ), patch(
                    "chejin_worker_client.task_runner.load_c2_ledger_entry",
                    side_effect=AssertionError(
                        "noncommitted object queried Ledger"
                    ),
                ) as ledger, patch(
                    "chejin_worker_client.task_runner.load_c2_outbox_origin_read_run_ids",
                    side_effect=AssertionError(
                        "noncommitted object queried Outbox"
                    ),
                ) as outbox, patch.object(
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
                    "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                )
                self.assertEqual(ledger.call_count, 0)
                self.assertEqual(outbox.call_count, 0)
                brain.assert_not_called()
                self.assertEqual(len(api.message_payloads), 1)
                self.assertEqual(api.message_payloads[0]["messages"], [])
                self.assertEqual(
                    list_c2_ledger_entries(target.conversation_id),
                    [],
                )
                self.assertEqual(
                    [
                        item
                        for item in list_c2_outbox_waiting(limit=500)
                        if item.get("conversation_id")
                        == target.conversation_id
                    ],
                    [],
                )

    def test_all_image_identity_consumers_use_the_shared_whitelist_gate(self):
        planner_source = inspect.getsource(
            TaskRunner._build_final_slot_incremental_plan
        )
        processor_source = inspect.getsource(
            TaskRunner._process_final_image_slots
        )
        persistence_source = inspect.getsource(
            TaskRunner._persist_one_image_slot_terminal
        )
        recovery_source = inspect.getsource(
            TaskRunner._merge_waiting_image_facts
        )
        for name, source in {
            "final_slot_plan": planner_source,
            "image_processor": processor_source,
            "terminal_persistence": persistence_source,
            "ledger_recovery": recovery_source,
        }.items():
            with self.subTest(consumer=name):
                self.assertIn(
                    "validate_committed_image_identity(",
                    source,
                )
        self.assertLess(
            planner_source.index("validate_committed_image_identity("),
            planner_source.index("load_c2_ledger_entry("),
        )
        self.assertLess(
            processor_source.index("validate_committed_image_identity("),
            processor_source.index("load_c2_ledger_entry("),
        )

    def test_uncommitted_image_identity_gate_never_blocks_other_conversations(self):
        unique = str(time.time_ns())
        conversation_id = f"conv-image-gate-local-{unique}"
        action_id = f"image-action-candidate:{unique}"
        path = action_journal_path("image", action_id)
        initialize_action_journal(
            path,
            action_kind="image",
            transaction_id=action_id,
            conversation_id=conversation_id,
            origin_read_run_id=f"read-image-gate-{unique}",
            canonical_action_id=action_id,
            reserved_worker_stable_id="worker-message-1",
            items=[
                {
                    "journal_item_id": action_id,
                    "physical_anchor_keys": ["image-gate-observation"],
                }
            ],
        )
        update_action_journal_item(
            path,
            journal_item_id=action_id,
            action_phase="not_attempted",
            business_state="failed",
            business_result_confirmed=False,
            error_code="C2_IMAGE_IDENTITY_CONTRACT_INVALID",
            terminal_payload={
                "state": "failed",
                "error_code": "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
                "reason_detail": (
                    "confirmed_image_identity_receipt_missing_or_invalid"
                ),
            },
        )

        self.assertNotIn(
            conversation_id,
            TaskRunner._pending_image_recovery_conversation_ids(),
        )
        self.assertEqual(
            list_c2_ledger_entries(
                conversation_id,
                message_type="image",
            ),
            [],
        )

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
            raw={
                "identity_checkpoint": identity_checkpoint(
                    next_sequence_floor=3
                )
            },
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
            "_worker_stable_id": "worker-message-1",
            "_worker_identity_scope": "current_read_provisional",
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
            "native_source_message_id": f"native-context-{unique}",
            "bubble_rect": [420, 340, 650, 390],
            "source_message": {
                "id": f"text-context-source-{unique}",
                "type": "text",
                "sender_role": "customer",
                "content": "图片后的稳定锚点文字",
            },
        }
        observation["_worker_stable_id"] = "worker-message-1"
        observation["_worker_identity_scope"] = (
            "current_read_provisional"
        )
        context_text["_worker_stable_id"] = "worker-message-2"
        reconciled = [observation, context_text]
        initial_payload = {
            "frame_id": f"frame-image-replay-{unique}",
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
                "slot_identity_confirmed": True,
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
            current_observations, sequence_evidence = (
                align_post_action_observations(
                    reconciled,
                    [shifted_context, new_text],
                )
            )
            self.assertEqual(
                sequence_evidence["alignment_status"],
                "unique",
            )
            current_observations = (
                runner._assign_sequence_new_suffix_identities(
                    target=target,
                    observations=current_observations,
                    evidence=sequence_evidence,
                    read_run_id=f"read-restored-{unique}",
                )
            )
            pushed_out_payload = {
                "observation_schema_version": 3,
                "authoritative_frame_source": "final_read",
                "observations": current_observations,
                "sequence_alignment_evidence": sequence_evidence,
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
        self.assertEqual(len(ingest["messages"]), 3, ingest)
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
            "_worker_stable_id": "worker-message-1",
            "_worker_identity_scope": "current_read_provisional",
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
        observation["_worker_stable_id"] = "worker-message-1"
        observation["_worker_identity_scope"] = (
            "current_read_provisional"
        )
        reconciled = [observation]
        initial_payload = {
            "frame_id": f"frame-image-restart-{unique}",
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

        def vision_completes_with_identity_receipt(**kwargs):
            journal_path = Path(kwargs["action_journal_path"])
            journal = read_action_journal(journal_path)
            selected = [
                item
                for item in journal["pre_action_identity_sequence"]
                if item.get("identity_state") == "selected_action"
            ]
            self.assertEqual(len(selected), 1)
            self.assertEqual(
                selected[0]["pre_observation_id"],
                observation["observation_id"],
            )
            action_id = str(journal["canonical_action_id"])
            reserved_id = str(journal["reserved_worker_stable_id"])
            update_action_journal_item(
                journal_path,
                journal_item_id=kwargs["action_local_id"],
                action_phase="confirmed",
                business_state="completed",
                business_result_confirmed=True,
                terminal_payload={
                    "state": "completed",
                    "error_code": None,
                    "reason_detail": None,
                    "customer_image_understanding": completed[
                        "customer_image_understanding"
                    ],
                    "visual_bridge_input": completed[
                        "visual_bridge_input"
                    ],
                    "confirmed_action_mapping": {
                        "canonical_action_id": action_id,
                        "reserved_worker_stable_id": reserved_id,
                        "pre_observation_id": observation[
                            "observation_id"
                        ],
                        "post_observation_id": observation[
                            "observation_id"
                        ],
                        "binding_confirmed": True,
                    },
                    "image_visual_fingerprint": (
                        "dhash64:fedcba9876543210"
                    ),
                },
            )
            return completed

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
            side_effect=vision_completes_with_identity_receipt,
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
        self.assertEqual(
            api.message_payloads[0]["evidence"][
                "authoritative_frame_source"
            ],
            "action_journal_recovery",
        )
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
                    "journal_item_id": "image-not-attempted-source",
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
                "journal_item_id": source_key,
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
            "_worker_stable_id": "worker-message-91",
            "_worker_identity_scope": "current_read_provisional",
            "image_physical_anchor": physical_anchor,
            "error_code": "C2_IMAGE_SOURCE_INVALID",
            "reason_detail": "text_context_menu_rejected",
            "source_message": {
                "sender_role": "customer",
                "type": "image",
                "image_physical_anchor": physical_anchor,
            },
        }
        receipt = {
            "canonical_action_id": transaction_id,
            "reserved_worker_stable_id": "worker-message-91",
            "pre_observation_id": observation["observation_id"],
            "post_observation_id": observation["observation_id"],
            "binding_confirmed": True,
            "image_visual_fingerprint": "failed-menu-fingerprint",
        }
        observation = apply_image_terminal_result(
            observation,
            {
                "state": "failed",
                "reason": "C2_IMAGE_SOURCE_INVALID",
                "action_phase": "not_attempted",
                "_confirmed_image_action_receipt": receipt,
            },
        )
        source_key = image_observation_source_key(target, observation)
        journal_item_id = transaction_id
        initialize_action_journal(
            path,
            action_kind="image",
            transaction_id=transaction_id,
            conversation_id=conversation_id,
            origin_read_run_id="read-image-physical-alias",
            items=[
                {
                    "journal_item_id": journal_item_id,
                    "physical_anchor_keys": ["image-anchor"],
                    "replayable_observation": observation,
                }
            ],
        )
        commit_action_journal_item_identity(
            path,
            journal_item_id=journal_item_id,
            source_message_key=source_key,
        )
        update_action_journal_item(
            path,
            journal_item_id=journal_item_id,
            action_phase="not_attempted",
            business_state="failed",
            business_result_confirmed=False,
            error_code="C2_IMAGE_SOURCE_INVALID",
            terminal_payload={
                "error_code": "C2_IMAGE_SOURCE_INVALID",
                "reason_detail": "text_context_menu_rejected",
                "media_action_terminal": "committed_failed",
                "confirmed_action_mapping": {
                    key: receipt[key]
                    for key in (
                        "canonical_action_id",
                        "reserved_worker_stable_id",
                        "pre_observation_id",
                        "post_observation_id",
                        "binding_confirmed",
                    )
                },
                "image_visual_fingerprint": receipt[
                    "image_visual_fingerprint"
                ],
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
        journal_item_ids = []
        for index, (formal_reason, reason_detail) in enumerate(cases):
            physical_anchor = {
                "sender_role": "self",
                "preceding_stable_message": f"before-{index}",
                "following_stable_message": f"after-{index}",
                "bubble_visual_fingerprint": f"fingerprint-{index}",
                "occurrence_index": index,
            }
            stable_id = f"worker-message-{100 + index}"
            observation = {
                "schema_version": 3,
                "observation_id": f"invalid-image-observation-{index}",
                "row_kind": "image_bubble",
                "sender_role": "self",
                "sender_role_source": "same_row_avatar",
                "message_type": "image",
                "voice_state": "not_voice",
                "item_state": "failed",
                "_worker_stable_id": stable_id,
                "_worker_identity_scope": "current_read_provisional",
                "image_physical_anchor": physical_anchor,
                "error_code": formal_reason,
                "reason_detail": reason_detail,
                "source_message": {
                    "sender_role": "self",
                    "type": "image",
                    "image_physical_anchor": physical_anchor,
                },
            }
            observation = apply_image_terminal_result(
                observation,
                {
                    "state": "failed",
                    "reason": formal_reason,
                    "action_phase": "confirmed",
                    "_confirmed_image_action_receipt": {
                        "canonical_action_id": (
                            f"image-invalid-action-{index}"
                        ),
                        "reserved_worker_stable_id": stable_id,
                        "pre_observation_id": observation[
                            "observation_id"
                        ],
                        "post_observation_id": observation[
                            "observation_id"
                        ],
                        "binding_confirmed": True,
                        "image_visual_fingerprint": physical_anchor[
                            "bubble_visual_fingerprint"
                        ],
                    },
                },
            )
            source_key = image_observation_source_key(target, observation)
            source_keys.append(source_key)
            journal_item_id = f"image-invalid-action-{index}"
            journal_item_ids.append(journal_item_id)
            journal_items.append(
                {
                    "journal_item_id": journal_item_id,
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
            journal_item_id = journal_item_ids[index]
            commit_action_journal_item_identity(
                journal_path,
                journal_item_id=journal_item_id,
                source_message_key=source_key,
            )
            update_action_journal_item(
                journal_path,
                journal_item_id=journal_item_id,
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
            "_worker_stable_id": "worker-message-90",
            "_worker_identity_scope": "committed",
            "_worker_image_action_summary": {
                "confirmed_action_mapping": {
                    "canonical_action_id": "image-action-invalid-unconfirmed",
                    "reserved_worker_stable_id": (
                        "worker-message-90"
                    ),
                    "pre_observation_id": (
                        "invalid-unconfirmed-observation"
                    ),
                    "post_observation_id": (
                        "invalid-unconfirmed-observation"
                    ),
                    "binding_confirmed": True,
                },
                "image_visual_fingerprint": "invalid-unconfirmed",
            },
            "_worker_committed_message": committed_identity_record(
                worker_stable_id="worker-message-90",
                commit_basis=MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
                observation_id="invalid-unconfirmed-observation",
                sender_role="customer",
                message_type="image",
                proof={
                    **{
                        "canonical_action_id": (
                            "image-action-invalid-unconfirmed"
                        ),
                        "reserved_worker_stable_id": "worker-message-90",
                        "pre_observation_id": (
                            "invalid-unconfirmed-observation"
                        ),
                        "post_observation_id": (
                            "invalid-unconfirmed-observation"
                        ),
                        "binding_confirmed": True,
                    },
                    "image_visual_fingerprint": "invalid-unconfirmed",
                },
            ),
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
                    "journal_item_id": source_key,
                    "physical_anchor_keys": ["image-anchor"],
                    "replayable_observation": replayable_observation,
                }
            ],
        )
        update_action_journal_item(
            journal_path,
            journal_item_id=source_key,
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
                "bubble_visual_fingerprint": (
                    f"image-window-fingerprint-{unique}"
                ),
                "occurrence_index": 0,
            },
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {
                "id": f"image-message-{unique}",
                "frame_visual_id": f"canonical-image-{unique}",
                "type": "image",
                "sender_role": "customer",
            },
        }
        text_arriving_during_image = {
            "schema_version": 3,
            "observation_id": f"text-during-image-{unique}",
            "row_kind": "text_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "text",
            "voice_state": "not_voice",
            "item_state": "completed",
            "content_clean": "图片处理期间新增的文字",
            "bubble_rect": [420, 340, 680, 390],
            "source_message": {
                "id": f"text-message-during-image-{unique}",
                "type": "text",
                "sender_role": "customer",
                "content": "图片处理期间新增的文字",
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
                "observations": [
                    observation,
                    text_arriving_during_image,
                ],
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
            journal_item_id = str(kwargs["action_local_id"])
            journal_source_keys.append(journal_item_id)
            journal_payload = read_action_journal(journal_path)
            action_id = str(journal_payload["canonical_action_id"])
            reserved_id = str(
                journal_payload["reserved_worker_stable_id"]
            )
            journal_observations.append(
                dict(
                    journal_payload["items"][journal_item_id][
                        "replayable_observation"
                    ]
                )
            )
            observed_phases.append(action_journal_phase(journal_path))
            update_action_journal_item(
                journal_path,
                journal_item_id=journal_item_id,
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
                journal_item_id=journal_item_id,
                action_phase="confirmed",
                business_state="completed",
                business_result_confirmed=True,
                terminal_payload={
                    "state": "completed",
                    "customer_image_understanding": understanding,
                    "visual_bridge_input": {
                        "summary": "车辆外观图"
                    },
                    "confirmed_action_mapping": {
                        "canonical_action_id": action_id,
                        "reserved_worker_stable_id": reserved_id,
                        "pre_observation_id": observation[
                            "observation_id"
                        ],
                        "post_observation_id": observation[
                            "observation_id"
                        ],
                        "binding_confirmed": True,
                    },
                    "image_visual_fingerprint": observation[
                        "image_physical_anchor"
                    ]["bubble_visual_fingerprint"],
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
                    "slot_identity_confirmed": True,
                    "clipboard_image_matches_target": True,
                    "image_sha256": "c" * 64,
                },
                "diagnostics": {
                    "schema_version": 1,
                    "trace_id": journal_item_id,
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

        self.assertTrue(
            result["ok"],
            {
                "result": result,
                "voice_transcribes": bridge.voice_transcribes,
                "last_payload": bridge.last_message_payload,
            },
        )
        self.assertNotIn("voice_prepare", bridge.c2_operation_order)
        self.assertEqual(
            observed_phases,
            ["not_attempted", "trigger_attempted", "confirmed"],
        )
        self.assertEqual(len(journal_observations), 1)
        self.assertEqual(
            journal_observations[0]["sender_role"],
            "customer",
        )
        self.assertNotIn(
            "source_message_key",
            journal_observations[0]["source_message"],
        )
        self.assertNotIn("image_bytes", journal_observations[0])
        self.assertNotIn("image_local_path", journal_observations[0])
        self.assertEqual(len(api.message_payloads), 1)
        self.assertEqual(
            api.message_payloads[0]["evidence"][
                "authoritative_frame_source"
            ],
            "final_read",
        )
        final_messages = api.message_payloads[0]["messages"]
        self.assertEqual(
            [message["message_type"] for message in final_messages],
            ["image", "text"],
        )
        image_message = final_messages[0]
        self.assertNotEqual(
            image_message["source_message_key"],
            journal_source_keys[0],
        )
        self.assertEqual(image_message["message_type"], "image")
        self.assertEqual(image_message["item_state"], "completed")
        self.assertEqual(
            image_message["content"],
            "客户发来一张车辆外观图。",
        )
        self.assertEqual(
            final_messages[1]["content"],
            "图片处理期间新增的文字",
        )
        ledger = load_c2_ledger_entry(
            target.conversation_id,
            image_message["source_message_key"],
        )
        self.assertEqual(ledger["terminal_state"], "completed")
        self.assertEqual(ledger["ingest_state"], "confirmed")
        text_ledger = load_c2_ledger_entry(
            target.conversation_id,
            final_messages[1]["source_message_key"],
        )
        self.assertEqual(text_ledger["terminal_state"], "completed")
        self.assertEqual(text_ledger["ingest_state"], "confirmed")
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
        observation["_worker_stable_id"] = "worker-message-1"
        observation["_worker_identity_scope"] = (
            "current_read_provisional"
        )
        reconciled = [observation]
        durable_source_key = worker_source_message_key(
            target,
            identity_kind="worker_sequence",
            identity="worker-message-1",
        )
        action_key = f"image-action-candidate:{unique}"
        flow_outcomes = FlowOutcomeAccumulator(
            origin_read_run_id="read-flow-outcomes"
        )

        def vision_finishes_then_process_exits(**kwargs):
            journal_path = Path(kwargs["action_journal_path"])
            journal = read_action_journal(journal_path)
            selected = [
                item
                for item in journal["pre_action_identity_sequence"]
                if item.get("identity_state") == "selected_action"
            ]
            self.assertEqual(len(selected), 1)
            self.assertEqual(
                selected[0]["pre_observation_id"],
                observation["observation_id"],
            )
            action_id = str(journal["canonical_action_id"])
            reserved_id = str(journal["reserved_worker_stable_id"])
            update_action_journal_item(
                journal_path,
                journal_item_id=action_key,
                action_phase="trigger_attempted",
                business_state="clipboard_copy_confirmed",
            )
            update_action_journal_item(
                journal_path,
                journal_item_id=action_key,
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
                    "confirmed_action_mapping": {
                        "canonical_action_id": action_id,
                        "reserved_worker_stable_id": reserved_id,
                        "pre_observation_id": observation[
                            "observation_id"
                        ],
                        "post_observation_id": observation[
                            "observation_id"
                        ],
                        "binding_confirmed": True,
                    },
                    "image_visual_fingerprint": (
                        "dhash64:1234567890abcdef"
                    ),
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
                        "frame_id": f"frame-image-journal-{unique}",
                        "observations": reconciled,
                        "window_context": {
                            "hwnd": 100,
                            "capture_source": "confirmed_c2_window",
                        }
                    },
                    observation=reconciled[0],
                    action_local_id=action_key,
                    cancel_check=lambda: False,
                    flow_outcomes=flow_outcomes,
                )
            self.assertIsNone(
                load_c2_ledger_entry(
                    target.conversation_id,
                    durable_source_key,
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
            restart_flow_id = "read-flow-outcomes"
            begin_runtime_flow(restart_flow_id, "c2_read")
            restarted_runner.binding = binding
            restarted_runner._restart_recovery_flow_id = restart_flow_id
            api.inflight_flow_id = restart_flow_id
            api.inflight_flow_state = {
                "status": "active",
                "flow_id": restart_flow_id,
                "flow_kind": "c2_read",
            }
            restarted_runner._backend_inflight_flow_state = dict(
                api.inflight_flow_state
            )
            save_c2_state(
                f"inflight_finish_receipt:{restart_flow_id}",
                {
                    "terminal_kind": "read_confirmed",
                    "conversation_id": target.conversation_id,
                },
            )

            restarted_runner.tick_once()

        self.assertEqual(vision.call_count, 1)
        self.assertIsNone(load_runtime_control()["inflight_flow_id"])
        self.assertIn("pull", api.events)
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
            durable_source_key,
        )
        self.assertEqual(ledger["ingest_state"], "confirmed")
        self.assertNotIn(
            target.conversation_id,
            restarted_runner._pending_image_recovery_conversation_ids(),
        )

    def test_image_recovery_rejects_mismatched_confirmed_fingerprint(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-bad-receipt-{unique}",
            rpa_session_key="",
            display_name="CJIMGBAD",
            remark_code="CJIMGBAD",
        )
        action_id = f"image:{target.conversation_id}:action"
        reserved_id = "worker-message-1"
        source_key = worker_source_message_key(
            target,
            identity_kind="worker_sequence",
            identity=reserved_id,
        )
        path = action_journal_path("image", action_id)
        initialize_action_journal(
            path,
            action_kind="image",
            transaction_id=action_id,
            conversation_id=target.conversation_id,
            origin_read_run_id=f"read-image-bad-receipt-{unique}",
            canonical_action_id=action_id,
            reserved_worker_stable_id=reserved_id,
            pre_frame_id=f"frame-image-bad-receipt-{unique}",
            pre_action_identity_sequence=[
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                    "pre_observation_id": "image-selected",
                    "pre_sequence_index": 0,
                    "sender_role": "customer",
                    "message_type": "image",
                    "image_visual_fingerprint": (
                        "dhash64:1111111111111111"
                    ),
                }
            ],
            items=[
                {
                    "journal_item_id": source_key,
                    "physical_anchor_keys": ["image-selected"],
                    "replayable_observation": {
                        "schema_version": 3,
                        "observation_id": "image-selected",
                        "row_kind": "image_bubble",
                        "sender_role": "customer",
                        "sender_role_source": "same_row_avatar",
                        "message_type": "image",
                        "voice_state": "not_voice",
                    },
                }
            ],
        )
        update_action_journal_item(
            path,
            journal_item_id=source_key,
            action_phase="confirmed",
            business_state="completed",
            business_result_confirmed=True,
            terminal_payload={
                "state": "completed",
                "confirmed_action_mapping": {
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                    "pre_observation_id": "image-selected",
                    "post_observation_id": "image-selected",
                    "binding_confirmed": True,
                },
                "image_visual_fingerprint": (
                    "dhash64:2222222222222222"
                ),
            },
        )

        unresolved = runner._recover_physical_action_journals(target)

        self.assertEqual(len(unresolved), 1)
        self.assertEqual(
            unresolved[0]["error_code"],
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        )
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, source_key)
        )

    def test_crash_before_image_receipt_reports_gate_without_ui_or_ledger(self):
        api = FakeApi(None)
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="unused", message="unused")
        )
        runner, _ = self.make_runner(api, bridge)
        binding = Binding(
            worker_id="worker-image-receipt-crash",
            worker_token="token",
            client_instance_id="client-image-receipt-crash",
            run_status="running",
        )
        unique = str(time.time_ns())
        target = WechatReadTarget(
            conversation_id=f"conv-image-receipt-crash-{unique}",
            rpa_session_key="wx:rpa:v1:image-receipt-crash",
            display_name="CJIMGCR1",
            remark_code="CJIMGCR1",
            read_reason="visible_unread",
            authorization_revision=f"revision-image-crash-{unique}",
        )
        action_id = f"image-action-candidate:{unique}"
        path = action_journal_path("image", action_id)
        initialize_action_journal(
            path,
            action_kind="image",
            transaction_id=action_id,
            conversation_id=target.conversation_id,
            origin_read_run_id=f"read-image-crash-{unique}",
            canonical_action_id=action_id,
            reserved_worker_stable_id="worker-message-1",
            pre_frame_id=f"frame-image-crash-{unique}",
            pre_action_identity_sequence=[
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": "worker-message-1",
                    "pre_observation_id": "image-crash-observation",
                    "image_visual_fingerprint": (
                        "dhash64:0123456789abcdef"
                    ),
                }
            ],
            prepare_evidence={
                "authorization_revision": (
                    target.authorization_revision
                ),
                "remark_code": target.remark_code,
                "rpa_session_key": target.rpa_session_key,
                "display_name": target.display_name,
                "read_reason": target.read_reason,
            },
            items=[
                {
                    "journal_item_id": action_id,
                    "physical_anchor_keys": [
                        "image-crash-observation"
                    ],
                }
            ],
        )
        update_action_journal_item(
            path,
            journal_item_id=action_id,
            action_phase="confirmed",
            business_state="completed",
            business_result_confirmed=True,
            terminal_payload={
                "state": "completed",
                "customer_image_understanding": {
                    "vision_summary": "不得进入正式消息的结果"
                },
            },
        )

        recovered = runner._recover_pending_image_transaction(binding)

        self.assertTrue(recovered)
        self.assertEqual(bridge.c2_operation_order, [])
        self.assertEqual(len(api.message_payloads), 1)
        gate_payload = api.message_payloads[0]
        self.assertEqual(gate_payload["messages"], [])
        self.assertIn(
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
            gate_payload["evidence"]["flow_gate_errors"],
        )
        self.assertEqual(
            list_c2_ledger_entries(
                target.conversation_id,
                message_type="image",
            ),
            [],
        )
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
            "frame_visual_id": f"visual-image-cancel-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "_worker_stable_id": "worker-message-1",
            "_worker_identity_scope": "current_read_provisional",
            "image_physical_anchor": {
                "sender_role": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "bubble_visual_fingerprint": (
                    f"image-frame-fingerprint-{unique}"
                ),
                "occurrence_index": 0,
            },
            "bubble_rect": [420, 180, 650, 320],
            "source_message": {"id": f"image-source-{unique}", "type": "image"},
        }
        sidecar_payload = {
            "frame_id": f"frame-image-cancel-{unique}",
            "observations": [observation],
        }

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
        self.assertEqual(
            list_c2_ledger_entries(
                target.conversation_id,
                message_type="image",
            ),
            [],
        )
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
            "_worker_stable_id": "worker-message-92",
            "_worker_identity_scope": "current_read_provisional",
            "image_physical_anchor": {
                "sender_role": "customer",
                "preceding_stable_message": f"before-{unique}",
                "following_stable_message": f"after-{unique}",
                "bubble_visual_fingerprint": (
                    f"image-frame-fingerprint-{unique}"
                ),
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
            "frame_id": f"frame-image-window-{unique}",
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

        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["removed_from_final_screen"], 1)
        self.assertEqual(result["observations"], [])
        self.assertEqual(
            list_c2_ledger_entries(target.conversation_id),
            [],
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
            "frame_visual_id": f"visual-image-moved-{unique}",
            "row_kind": "image_bubble",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "_worker_stable_id": "worker-message-1",
            "_worker_identity_scope": "current_read_provisional",
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
        sidecar_payload = {
            "frame_id": f"frame-image-moved-{unique}",
            "observations": [observation],
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
            return_value={
                # Match the production Vision transaction envelope exactly.
                "state": "image_not_visible",
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
        self.assertEqual(
            list_c2_ledger_entries(
                target.conversation_id,
                message_type="image",
            ),
            [],
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
                "_worker_stable_id": "worker-message-1",
                "_worker_identity_scope": "current_read_provisional",
                "frame_visual_id": f"visual-image-{name}-{unique}",
                "_worker_stable_id": (
                    "worker-message-1"
                    if name == "old-failed"
                    else "worker-message-2"
                ),
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "preceding_stable_message": (
                        f"before-{name}-{unique}"
                    ),
                    "following_stable_message": (
                        f"after-{name}-{unique}"
                    ),
                    "bubble_visual_fingerprint": (
                        "dhash64:0123456789abcdef"
                        if name == "old-failed"
                        else "dhash64:fedcba9876543210"
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
        old_failed = apply_image_terminal_result(
            old_failed,
            {
                "state": "failed",
                "reason": "image_copy_failed",
                "action_phase": "confirmed",
                "_confirmed_image_action_receipt": {
                    "canonical_action_id": f"image-old-action-{unique}",
                    "reserved_worker_stable_id": "worker-message-1",
                    "pre_observation_id": old_failed["observation_id"],
                    "post_observation_id": old_failed["observation_id"],
                    "binding_confirmed": True,
                    "image_visual_fingerprint": (
                        "dhash64:0123456789abcdef"
                    ),
                },
            },
        )
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
            "transaction": {
                "action_phase": "confirmed",
                "slot_identity_confirmed": True,
            },
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
                    "sidecar_run_id": f"image-refresh-{unique}",
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
                "sequence_alignment_evidence": {
                    "pre_sequence_source": "empty_checkpoint",
                    "pre_frame_id": (
                        f"checkpoint:none:{target.conversation_id}"
                    ),
                    "post_frame_id": f"frame:image-untrusted-{unique}",
                    "alignment_status": "not_required",
                    "candidate_alignment_count": 0,
                    "matched_pairs": [],
                    "old_tail_fully_consumed": True,
                    "new_suffix_observation_ids": [
                        observation["observation_id"]
                    ],
                },
            },
            read_run_id=f"read-image-untrusted-{unique}",
        )

        self.assertEqual(len(plan["identity_errors"]), 1)
        self.assertEqual(
            plan["identity_errors"][0]["error_code"],
            "MESSAGE_IDENTITY_UNCONFIRMED",
        )
        self.assertEqual(plan["new_image_observation_ids"], set())

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
            allowed_new_observation_ids=set(),
            flow_outcomes=FlowOutcomeAccumulator(
                origin_read_run_id="read-image-not-new"
            ),
        )

        self.assertNotIn("ignored", phase_result)
        self.assertEqual(
            result["observations"][0]["item_state"],
            "discovered",
        )
        self.assertEqual(
            list_c2_ledger_entries(
                target.conversation_id,
                message_type="image",
            ),
            [],
        )

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
        existing_text = {
            "schema_version": 3,
            "observation_id": "existing-text",
            "row_kind": "text_bubble",
            "message_type": "text",
            "voice_state": "not_voice",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "content_clean": "图片动作前已存在的文字",
            "_worker_stable_id": "worker-message-1",
            "bubble_rect": [420, 100, 650, 150],
            "source_message": {
                "id": "existing-text",
                "type": "text",
                "sender_role": "customer",
            },
        }
        new_image = {
            "schema_version": 3,
            "observation_id": "new-image",
            "row_kind": "image_bubble",
            "message_type": "image",
            "voice_state": "not_voice",
            "item_state": "discovered",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "frame_visual_id": "visual-new-image",
            "image_physical_anchor": {
                "sender_role": "customer",
                "bubble_visual_fingerprint": (
                    "dhash64:1234567890abcdef"
                ),
                "occurrence_index": 0,
                "occurrence_count": 1,
            },
            "bubble_rect": [420, 200, 650, 320],
            "source_message": {
                "id": "new-image",
                "type": "image",
                "sender_role": "customer",
                "frame_visual_id": "visual-new-image",
            },
        }
        refreshed_payload = {
            "ok": True,
            "frame_id": "frame-post-vision-new-image",
            "observations": [existing_text, new_image],
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
                "new_image_observation_ids": {"new-image"},
            },
            {
                "history_gap": False,
                "identity_errors": [],
                "new_image_observation_ids": set(),
            },
        ]
        processed_payload = {
            **refreshed_payload,
            "observations": [
                existing_text,
                {
                    **new_image,
                    "item_state": "completed",
                    "_worker_stable_id": "worker-message-2",
                    "_worker_identity_scope": "committed",
                    "_worker_image_action_summary": {
                        "confirmed_action_mapping": {
                            "canonical_action_id": "image-action-1",
                            "reserved_worker_stable_id": (
                                "worker-message-2"
                            ),
                            "pre_observation_id": "new-image",
                            "post_observation_id": "new-image",
                            "binding_confirmed": True,
                        },
                        "image_visual_fingerprint": (
                            "dhash64:1234567890abcdef"
                        ),
                    },
                }
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
        ) as process_images, patch.object(
            runner,
            "_confirmed_ai_reply_text_for_read",
            return_value="已确认的 AI 回复",
        ):
            result = runner._converge_current_screen_after_images(
                binding=binding,
                target=target,
                target_label="CJPOST01",
                sidecar_payload={
                    "ok": True,
                    "frame_id": "frame-before-new-image",
                    "observations": [existing_text],
                },
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
            first_call["allowed_new_observation_ids"],
            {"new-image"},
        )
        for read_call in runner.bridge.get_messages.call_args_list:
            self.assertEqual(
                read_call.kwargs["target_mode"],
                "current",
            )
            self.assertNotIn("max_scroll_steps", read_call.kwargs)
            self.assertEqual(
                read_call.kwargs["expected_confirmed_self_text"],
                "已确认的 AI 回复",
            )
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
        existing_text = {
            "observation_id": "existing-text",
            "row_kind": "text_bubble",
            "message_type": "text",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "content_clean": "在？",
            "_worker_stable_id": "worker-seq:1",
            "native_source_message_id": "native-existing-text",
        }
        refreshed_payload = {
            "ok": True,
            "observations": [existing_text, voice],
        }
        transcribed_payload = {
            "ok": True,
            "observations": [
                existing_text,
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
                "new_image_observation_ids": set(),
            },
        ):
            result = runner._converge_current_screen_after_images(
                binding=binding,
                target=target,
                target_label="CJPOST02",
                sidecar_payload={
                    "ok": True,
                    "observations": [existing_text],
                },
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
            result["payload"]["observations"][1]["voice_state"],
            "transcribed",
        )

    def test_post_vision_refresh_propagates_voice_terminal_gate(self):
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
            conversation_id="conv-post-vision-voice-gate",
            rpa_session_key="",
            display_name="CJPOST04",
            remark_code="CJPOST04",
            authorization_revision="revision-post-vision-voice-gate",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        existing_text = {
            "observation_id": "existing-text",
            "row_kind": "text_bubble",
            "message_type": "text",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
            "content_clean": "在？",
            "_worker_stable_id": "worker-message-1",
        }
        new_voice = {
            "observation_id": "new-ambiguous-voice",
            "row_kind": "voice_bubble",
            "message_type": "voice",
            "voice_state": "untranscribed",
            "sender_role": "customer",
            "sender_role_source": "same_row_avatar",
        }
        refreshed_payload = {
            "ok": True,
            "observations": [existing_text, new_voice],
        }
        terminal_gate = {
            "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
            "identity_errors": [
                {
                    "row_kind": "voice_bubble",
                    "signature": "voice:post-vision:action-1",
                    "reason": (
                        "action_triggered_without_confirmed_post_alignment"
                    ),
                }
            ],
            "authoritative_frame_source": "final_read",
            "ui_frame_invalidated": True,
        }
        runner.bridge.get_messages = unittest.mock.Mock(
            return_value=refreshed_payload
        )
        with patch(
            "chejin_worker_client.task_runner.sidecar_contract_error",
            return_value=None,
        ), patch.object(
            runner,
            "_finish_new_visible_voices_in_current_chat",
            return_value={
                "ok": True,
                "payload": refreshed_payload,
                "failed_source_keys": [],
                "failed_roles": {},
                "terminal_gate": terminal_gate,
            },
        ) as finish_voice, patch.object(
            runner,
            "_build_final_slot_incremental_plan",
        ) as build_plan:
            result = runner._converge_current_screen_after_images(
                binding=binding,
                target=target,
                target_label="CJPOST04",
                sidecar_payload={
                    "ok": True,
                    "observations": [existing_text],
                },
                lease=unittest.mock.Mock(),
                action_cancel_requested=lambda: False,
                enforce_read_targets=True,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-image-flow-4"
                ),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["terminal_gate"], terminal_gate)
        self.assertEqual(
            result["error_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        )
        finish_voice.assert_called_once()
        build_plan.assert_not_called()

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
            "chejin_worker_client.task_runner."
            "align_post_action_observations",
            return_value=(
                [],
                {
                    "pre_sequence_source": "action_frame",
                    "pre_frame_id": "frame-before-ambiguous",
                    "post_frame_id": "frame-after-ambiguous",
                    "alignment_status": "ambiguous",
                    "candidate_alignment_count": 2,
                    "matched_pairs": [],
                    "old_tail_fully_consumed": False,
                    "new_suffix_observation_ids": [],
                },
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

    def test_backend_confirmed_historical_fact_is_not_reenqueued(self):
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
                        "fact_scope": "historical",
                        "delivery_state": "backend_confirmed",
                    },
                    {
                        "source_message_key": "new-sales",
                        "screen_order": 2,
                        "ledger_state": "NEW_MESSAGE",
                        "fact_scope": "current_read_run",
                        "delivery_state": "not_enqueued",
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
        payload["frame_id"] = f"frame-history-gap-{unique}"
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
                "_worker_stable_id": "worker-message-1",
                "_worker_identity_scope": "current_read_provisional",
                "frame_visual_id": f"visual-new-image-{unique}",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "preceding_stable_message": f"new-before-{unique}",
                    "following_stable_message": f"new-after-{unique}",
                    "bubble_visual_fingerprint": (
                        "dhash64:0123456789abcdef"
                    ),
                    "occurrence_index": 0,
                },
                "bubble_rect": [420, 120, 650, 260],
                "source_message": {"id": f"new-image-source-{unique}", "type": "image"},
            },
        )
        payload["observations"][1]["bubble_rect"] = [420, 300, 650, 340]
        old_text = payload["observations"][1]
        old_text_id = str(old_text.get("observation_id") or "")
        old_text["_worker_stable_id"] = "worker-message-2"
        old_text["_worker_identity_scope"] = "committed"
        old_text["_worker_committed_message"] = committed_identity_record(
            worker_stable_id="worker-message-2",
            commit_basis=MessageCommitBasis.NEW_SUFFIX,
            observation_id=old_text_id,
            sender_role="customer",
            message_type="text",
            proof={
                "alignment_status": "not_required",
                "old_tail_fully_consumed": True,
                "new_suffix_observation_id": old_text_id,
            },
        )
        payload["sequence_alignment_evidence"] = {
            "pre_sequence_source": "empty_checkpoint",
            "pre_frame_id": f"checkpoint:none:{target.conversation_id}",
            "post_frame_id": f"frame:history-gap-{unique}",
            "alignment_status": "not_required",
            "candidate_alignment_count": 0,
            "matched_pairs": [],
            "old_tail_fully_consumed": True,
            "new_suffix_observation_ids": [
                str(item.get("observation_id") or "")
                for item in payload["observations"]
            ],
        }
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
                "action_phase": "confirmed",
                "transaction": {
                    "action_phase": "confirmed",
                    "slot_identity_confirmed": True,
                },
                "diagnostics": {"events": [], "image_persisted": False},
            },
        ) as vision:
            processed_payload, stats = runner._process_final_image_slots(
                binding=binding,
                target=target,
                sidecar_payload=payload,
                enforce_read_targets=False,
                allowed_new_observation_ids=set(
                    plan["new_image_observation_ids"]
                ),
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id=current_read_run_id
                ),
            )

        self.assertTrue(plan["history_gap"])
        self.assertEqual(
            [
                item["ledger_state"]
                for item in plan["slot_ledger_states"]
            ],
            ["OLD_COMPLETED"],
        )
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
        terminal_image = next(
            item
            for item in processed_payload["observations"]
            if item.get("row_kind") == "image_bubble"
        )
        image_source_key = image_observation_source_key(
            target,
            terminal_image,
        )
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
        for index, observation in enumerate(payload["observations"], start=1):
            stable_id = f"worker-message-{index}"
            observation_id = str(
                observation.get("observation_id") or ""
            )
            observation["_worker_stable_id"] = stable_id
            observation["_worker_identity_scope"] = "committed"
            observation["_worker_committed_message"] = (
                committed_identity_record(
                    worker_stable_id=stable_id,
                    commit_basis=MessageCommitBasis.NEW_SUFFIX,
                    observation_id=observation_id,
                    sender_role="customer",
                    message_type="text",
                    proof={
                        "alignment_status": "not_required",
                        "old_tail_fully_consumed": True,
                        "new_suffix_observation_id": observation_id,
                    },
                )
            )
        payload["sequence_alignment_evidence"] = {
            "pre_sequence_source": "empty_checkpoint",
            "pre_frame_id": f"checkpoint:none:{target.conversation_id}",
            "post_frame_id": f"frame:backend-history-{unique}",
            "alignment_status": "not_required",
            "candidate_alignment_count": 0,
            "matched_pairs": [],
            "old_tail_fully_consumed": True,
            "new_suffix_observation_ids": [
                str(item.get("observation_id") or "")
                for item in payload["observations"]
            ],
        }
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
        attach_sequence_identity_fixture(
            sidecar_payload,
            frame_id=f"backend-stage-{unique}",
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
                            "bubble_visual_fingerprint": (
                                "dhash64:0123456789abcdef"
                            ),
                        },
                    }
                )
                for index, observation in enumerate(
                    payload["observations"],
                    start=1,
                ):
                    stable_id = f"worker-message-{index}"
                    observation_id = str(
                        observation.get("observation_id") or ""
                    )
                    message_type = str(
                        observation.get("message_type") or ""
                    )
                    observation["_worker_stable_id"] = stable_id
                    observation["_worker_identity_scope"] = "committed"
                    if message_type == "text":
                        basis = MessageCommitBasis.NEW_SUFFIX
                        proof = {
                            "alignment_status": "not_required",
                            "old_tail_fully_consumed": True,
                            "new_suffix_observation_id": observation_id,
                        }
                    elif message_type == "voice":
                        basis = MessageCommitBasis.CONFIRMED_VOICE_ACTION
                        action_id = f"voice-action-{unique}"
                        observation["_worker_voice_action_summary"] = {
                            "confirmed_action_mapping": {
                                "canonical_action_id": action_id,
                                "reserved_worker_stable_id": stable_id,
                                "pre_observation_id": observation_id,
                                "post_observation_id": observation_id,
                                "selected_action_token": (
                                    f"voice-token-{unique}"
                                ),
                                "binding_confirmed": True,
                            }
                        }
                        proof = dict(
                            observation["_worker_voice_action_summary"][
                                "confirmed_action_mapping"
                            ]
                        )
                    else:
                        continue
                    observation["_worker_committed_message"] = (
                        committed_identity_record(
                            worker_stable_id=stable_id,
                            commit_basis=basis,
                            observation_id=observation_id,
                            sender_role="customer",
                            message_type=message_type,
                            proof=proof,
                        )
                    )
                image_observation = payload["observations"][1]
                image_observation["_worker_image_action_summary"] = {
                    "confirmed_action_mapping": {
                        "canonical_action_id": f"image-action-{unique}",
                        "reserved_worker_stable_id": "worker-message-2",
                        "pre_observation_id": str(
                            image_observation["observation_id"]
                        ),
                        "post_observation_id": str(
                            image_observation["observation_id"]
                        ),
                        "binding_confirmed": True,
                    },
                    "image_visual_fingerprint": str(
                        image_observation["image_physical_anchor"][
                            "bubble_visual_fingerprint"
                        ]
                    ),
                }
                image_observation["_worker_committed_message"] = (
                    committed_identity_record(
                        worker_stable_id="worker-message-2",
                        commit_basis=(
                            MessageCommitBasis.CONFIRMED_IMAGE_ACTION
                        ),
                        observation_id=str(
                            image_observation["observation_id"]
                        ),
                        sender_role="customer",
                        message_type="image",
                        proof={
                            **image_observation[
                                "_worker_image_action_summary"
                            ]["confirmed_action_mapping"],
                            "image_visual_fingerprint": str(
                                image_observation[
                                    "image_physical_anchor"
                                ]["bubble_visual_fingerprint"]
                            ),
                        },
                    )
                )
                payload["sequence_alignment_evidence"] = {
                    "pre_sequence_source": "empty_checkpoint",
                    "pre_frame_id": f"checkpoint:none:{unique}",
                    "post_frame_id": f"frame:{unique}",
                    "alignment_status": "not_required",
                    "candidate_alignment_count": 1,
                    "matched_pairs": [],
                    "old_tail_fully_consumed": True,
                    "new_suffix_observation_ids": [
                        str(item["observation_id"])
                        for item in payload["observations"]
                    ],
                }
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
                    "not_enqueued",
                )
                self.assertEqual(
                    post_media["slot_ledger_states"][1]["item_state"],
                    terminal_state,
                )
                self.assertFalse(post_media["history_gap"])
                self.assertNotIn(
                    image_slot["source_message_key"],
                    post_media["new_image_observation_ids"],
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
            attach_sequence_identity_fixture(
                payload,
                frame_id=f"conversation-{suffix}",
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
        payload["observations"][2]["native_source_message_id"] = (
            f"native-current-bottom-{unique}"
        )
        attach_sequence_identity_fixture(
            payload,
            frame_id=f"real-gap-{unique}",
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
                "item_state": "completed",
                "content_clean": "历史图片",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "preceding_stable_message": "top-boundary",
                    "following_stable_message": "bottom-boundary",
                    "occurrence_index": 0,
                    "bubble_visual_fingerprint": (
                        "dhash64:0123456789abcdef"
                    ),
                },
                "customer_image_understanding": {
                    "schema_version": 1,
                    "vision_summary": "历史图片",
                },
                "visual_bridge_input": {"summary": "历史图片"},
            }
        )
        attach_sequence_identity_fixture(
            payload,
            frame_id=f"unknown-origin-{unique}",
        )
        image_observation = payload["observations"][0]
        image_observation["_worker_stable_id"] = "worker-message-1"
        image_observation["_worker_identity_scope"] = "committed"
        image_observation["_worker_image_action_summary"] = {
            "confirmed_action_mapping": {
                "canonical_action_id": f"image-action-{unique}",
                "reserved_worker_stable_id": "worker-message-1",
                "pre_observation_id": str(
                    image_observation["observation_id"]
                ),
                "post_observation_id": str(
                    image_observation["observation_id"]
                ),
                "binding_confirmed": True,
            },
            "image_visual_fingerprint": "dhash64:0123456789abcdef",
        }
        image_observation["_worker_committed_message"] = (
            committed_identity_record(
                worker_stable_id="worker-message-1",
                commit_basis=MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
                observation_id=str(image_observation["observation_id"]),
                sender_role="customer",
                message_type="image",
                proof={
                    **image_observation[
                        "_worker_image_action_summary"
                    ]["confirmed_action_mapping"],
                    "image_visual_fingerprint": (
                        "dhash64:0123456789abcdef"
                    )
                },
            )
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
        self.assertEqual(plan["new_image_observation_ids"], set())
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
        attach_sequence_identity_fixture(
            payload,
            frame_id=f"historical-warning-{unique}",
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
        bridge = FakeBridge(
            RpaResult(ok=True, result_code="ok", message="unused")
        )
        runner, _ = self.make_runner(FakeApi(None), bridge)
        target = WechatReadTarget(
            conversation_id=f"conv-old-identity-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:old-identity",
            display_name="CJOLDID 客户",
            remark_code="CJOLDID",
            authorization_revision="revision-old-identity",
            raw={"identity_checkpoint": identity_checkpoint()},
        )
        payload = bridge._contractual_message_payload(
            {
                "ok": True,
                "messages": [
                    {
                        "id": "latest-self",
                        "type": "text",
                        "sender_role": "self",
                        "content": "销售已经回复",
                        "bubble_rect": [700, 220, 920, 260],
                    },
                    {
                        "id": "latest-customer",
                        "type": "text",
                        "sender_role": "customer",
                        "content": "最新问题",
                        "bubble_rect": [420, 340, 650, 380],
                    },
                ],
            }
        )
        attach_sequence_identity_fixture(
            payload,
            frame_id="cross-round-historical-warning",
        )
        payload["historical_warnings"] = [
            {
                "warning_code": (
                    "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS_HISTORICAL"
                ),
                "observation_id": "old-ambiguous",
            }
        ]

        plan = runner._build_final_slot_incremental_plan(
            target=target,
            sidecar_payload=payload,
            read_run_id="read-cross-round-historical-warning",
        )

        self.assertEqual(plan["identity_errors"], [])
        self.assertFalse(plan["history_gap"])
        self.assertEqual(
            plan["historical_warnings"][0]["warning_code"],
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
                "sidecar_run_id": "history-gap-clean-frame",
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

        self.assertFalse(
            retried_plan["history_gap"],
            {
                "retried_payload": retried_payload,
                "retried_plan": retried_plan,
            },
        )
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
                "_worker_stable_id": f"worker-message-{index + 1}",
                "_worker_identity_scope": "current_read_provisional",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "preceding_stable_message": f"before-{index}-{unique}",
                    "following_stable_message": f"after-{index}-{unique}",
                    "bubble_visual_fingerprint": (
                        f"dhash64:{index + 1:016x}"
                    ),
                    "occurrence_index": 0,
                },
                "bubble_rect": [420, 120 + index * 180, 650, 260 + index * 180],
                "source_message": {
                    "id": f"image-source-{index}-{unique}",
                    "frame_visual_id": f"visual-image-{index}-{unique}",
                    "type": "image",
                },
            }
            for index in range(len(failure_reasons))
        ]
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
            stats_by_frame = []
            for observation in payload["observations"]:
                _, frame_stats = runner._process_final_image_slots(
                    binding=binding,
                    target=target,
                    sidecar_payload={
                        **payload,
                        "sidecar_run_id": (
                            f"image-frame-{observation['observation_id']}"
                        ),
                        "observations": [observation],
                    },
                    enforce_read_targets=False,
                    allowed_new_observation_ids={
                        str(observation["observation_id"])
                    },
                    flow_outcomes=FlowOutcomeAccumulator(
                        origin_read_run_id=(
                            f"read-image-failures-{unique}"
                        )
                    ),
                )
                stats_by_frame.append(frame_stats)

        self.assertEqual(
            sum(int(stats["failed"]) for stats in stats_by_frame),
            0,
        )
        # A failure before the irreversible trigger is a burned reservation,
        # not an image failure fact.  The three attempted actions have no
        # confirmed receipt and therefore close as identity_unresolved.
        self.assertNotIn("terminal_gate", stats_by_frame[0])
        self.assertTrue(
            all(
                stats["terminal_gate"]["error_code"]
                == "C2_IMAGE_IDENTITY_CONTRACT_INVALID"
                for stats in stats_by_frame[1:]
            )
        )
        self.assertEqual(vision.call_count, 4)
        incident_logs = [
            row
            for row in read_logs(limit=100)
            if row.get("event")
            == "c2_image_identity_receipt_rejected"
            and (row.get("metadata") or {}).get("conversation_id")
            == target.conversation_id
        ]
        self.assertEqual(len(incident_logs), 3)
        self.assertEqual(
            sorted(row.get("error_code") for row in incident_logs),
            ["C2_IMAGE_IDENTITY_CONTRACT_INVALID"] * 3,
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
                if (
                    (row.get("metadata") or {}).get("reason")
                    == "C2_IMAGE_MENU_OPERATION_FAILED"
                ):
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
            self.assertEqual(
                manifest["error_code"],
                "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
            )

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
            "frame_id": f"frame-image-config-{unique}",
            "observations": [
                {
                    "schema_version": 3,
                    "observation_id": f"image-config-{unique}",
                    "frame_visual_id": f"visual-image-config-{unique}",
                    "row_kind": "image_bubble",
                    "sender_role": "customer",
                    "sender_role_source": "same_row_avatar",
                    "message_type": "image",
                    "voice_state": "not_voice",
                    "item_state": "discovered",
                    "_worker_stable_id": "worker-message-1",
                    "_worker_identity_scope": "current_read_provisional",
                    "image_physical_anchor": {
                        "sender_role": "customer",
                        "preceding_stable_message": f"config-before-{unique}",
                        "following_stable_message": f"config-after-{unique}",
                        "bubble_visual_fingerprint": (
                            "dhash64:0123456789abcdef"
                        ),
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
        assert first_stats["failed"] == 0
        assert second_stats["cached"] == 0
        assert second_stats["failed"] == 0
        assert first_stats["cached"] == 0
        assert first_stats["removed_from_final_screen"] == 1
        assert second_stats["removed_from_final_screen"] == 1
        assert first_stats.get("terminal_gate") is None
        assert second_stats.get("terminal_gate") is None
        assert list_c2_ledger_entries(
            target.conversation_id,
            message_type="image",
        ) == []

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

        self.assertFalse(result["ok"], result)
        self.assertEqual(
            result["error_code"],
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        )
        # The image action never reached the trigger boundary, so its reserved
        # identity is burned locally and no formal V3 payload is allowed.  A
        # later stable read must arbitrate the current frame from scratch.
        self.assertEqual(api.message_payloads, [])

    def test_ai_reply_receipt_attaches_only_by_committed_stable_identity(self):
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
            f"message_identity:{target.conversation_id}",
            {
                "version": 4,
                "ai_reply_receipts": [
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

    def test_possible_ai_send_never_rehangs_matching_text(self):
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
        self.assertNotIn("_worker_ai_reply_receipt", enriched[1])
        self.assertNotIn("_worker_ai_reply_receipt", enriched[2])

    def test_ai_send_receipt_accepts_concurrent_message_after_confirmed_send(self):
        pre = [
            self._ai_send_observation(
                "old-1",
                sender_role="customer",
                content="在吗",
                stable_id="worker-message-10",
            ),
            self._ai_send_observation(
                "old-2",
                sender_role="customer",
                content="想了解价格",
                stable_id="worker-message-11",
            ),
        ]
        runner, _bridge, target, action_id, reserved_id, state_key = (
            self._prepare_ai_send_receipt_fixture(
                conversation_id="conv-send-after-concurrent",
                pre_observations=pre,
                next_sequence=12,
                reply_text="好的",
            )
        )
        post = [
            dict(pre[0]),
            dict(pre[1]),
            self._ai_send_observation(
                "sent-ai",
                sender_role="self",
                content="好的",
                native_id="native-sent-ai",
            ),
            self._ai_send_observation(
                "new-after",
                sender_role="customer",
                content="还有一个问题",
            ),
        ]

        recorded = runner._record_confirmed_ai_reply_receipt(
            target=target,
            reply_action_id=action_id,
            reply_text_hash=runner._reply_text_hash("好的"),
            sidecar_result=self._confirmed_send_sidecar_result(
                observations=post,
                confirmed_observation_id="sent-ai",
                run_id="send-after-concurrent",
            ),
            confirmed_at="2026-08-11T00:00:00+00:00",
        )

        self.assertTrue(recorded)
        state = load_c2_state(state_key)
        self.assertEqual(
            state["sequence_commits"][action_id], reserved_id
        )
        self.assertEqual(
            state["ai_reply_receipts"][0]["worker_stable_id"],
            reserved_id,
        )

    def test_ai_send_receipt_rejects_concurrent_message_before_send(self):
        pre = [
            self._ai_send_observation(
                "old-a",
                sender_role="customer",
                content="并发的新问题",
                stable_id="worker-message-20",
            ),
        ]
        runner, _bridge, target, action_id, _reserved_id, state_key = (
            self._prepare_ai_send_receipt_fixture(
                conversation_id="conv-send-before-concurrent",
                pre_observations=pre,
                next_sequence=21,
                reply_text="好的",
            )
        )
        post = [
            dict(pre[0]),
            self._ai_send_observation(
                "new-before",
                sender_role="customer",
                content="并发的新问题",
            ),
            self._ai_send_observation(
                "sent-ai",
                sender_role="self",
                content="好的",
                native_id="native-sent-ai-before",
            ),
        ]

        recorded = runner._record_confirmed_ai_reply_receipt(
            target=target,
            reply_action_id=action_id,
            reply_text_hash=runner._reply_text_hash("好的"),
            sidecar_result=self._confirmed_send_sidecar_result(
                observations=post,
                confirmed_observation_id="sent-ai",
                run_id="send-before-concurrent",
            ),
            confirmed_at="2026-08-11T00:00:00+00:00",
        )

        self.assertFalse(recorded)
        self.assertNotIn("ai_reply_receipts", load_c2_state(state_key))
        possible = load_c2_state(
            f"possible_ai_sends:{target.conversation_id}"
        )["sends"][0]
        self.assertTrue(possible["physical_send_confirmed"])
        self.assertEqual(
            possible["reconciliation_state"],
            "identity_unconfirmed_possible_sent",
        )

    def test_ai_send_receipt_identifies_same_text_only_by_confirmed_action(self):
        pre = [
            self._ai_send_observation(
                "old-same-text",
                sender_role="self",
                content="好的",
                stable_id="worker-message-30",
            ),
            self._ai_send_observation(
                "customer-latest",
                sender_role="customer",
                content="麻烦确认",
                stable_id="worker-message-31",
            ),
        ]
        runner, _bridge, target, action_id, reserved_id, state_key = (
            self._prepare_ai_send_receipt_fixture(
                conversation_id="conv-send-identical-text",
                pre_observations=pre,
                next_sequence=32,
                reply_text="好的",
            )
        )
        post = [
            dict(pre[0]),
            dict(pre[1]),
            self._ai_send_observation(
                "new-same-text",
                sender_role="self",
                content="好的",
                native_id="native-new-same-text",
            ),
        ]

        recorded = runner._record_confirmed_ai_reply_receipt(
            target=target,
            reply_action_id=action_id,
            reply_text_hash=runner._reply_text_hash("好的"),
            sidecar_result=self._confirmed_send_sidecar_result(
                observations=post,
                confirmed_observation_id="new-same-text",
                run_id="send-identical-text",
            ),
            confirmed_at="2026-08-11T00:00:00+00:00",
        )

        self.assertTrue(recorded)
        self.assertEqual(
            load_c2_state(state_key)["ai_reply_receipts"][0][
                "worker_stable_id"
            ],
            reserved_id,
        )

    def test_ai_send_receipt_aligns_after_viewport_scroll(self):
        pre = [
            self._ai_send_observation(
                f"scroll-{index}",
                sender_role="customer",
                content=f"消息{index}",
                stable_id=f"worker-message-{40 + index}",
            )
            for index in range(4)
        ]
        runner, _bridge, target, action_id, reserved_id, state_key = (
            self._prepare_ai_send_receipt_fixture(
                conversation_id="conv-send-scroll",
                pre_observations=pre,
                next_sequence=44,
                reply_text="收到",
            )
        )
        post = [
            dict(pre[2]),
            dict(pre[3]),
            self._ai_send_observation(
                "sent-after-scroll",
                sender_role="self",
                content="收到",
                native_id="native-sent-after-scroll",
            ),
        ]

        recorded = runner._record_confirmed_ai_reply_receipt(
            target=target,
            reply_action_id=action_id,
            reply_text_hash=runner._reply_text_hash("收到"),
            sidecar_result=self._confirmed_send_sidecar_result(
                observations=post,
                confirmed_observation_id="sent-after-scroll",
                run_id="send-scroll",
            ),
            confirmed_at="2026-08-11T00:00:00+00:00",
        )

        self.assertTrue(recorded)
        receipt = load_c2_state(state_key)["ai_reply_receipts"][0]
        self.assertEqual(receipt["worker_stable_id"], reserved_id)
        self.assertEqual(
            [
                pair["worker_stable_id"]
                for pair in receipt["sequence_alignment_evidence"][
                    "matched_pairs"
                ]
            ],
            ["worker-message-42", "worker-message-43"],
        )

    def test_ai_send_receipt_aligns_live_scroll_when_all_ocr_ids_rebuild(self):
        pre_specs = [
            ("pre-top", "self", "好嘞，有需要随时跟我说。"),
            ("pre-long", "self", "这是一条保留在窗口里的较长历史消息"),
            ("pre-ok-1", "customer", "好"),
            ("pre-middle", "self", "小号回我一局"),
            ("pre-ok-2", "customer", "好"),
        ]
        pre = [
            self._ai_send_observation(
                observation_id,
                sender_role=sender_role,
                content=content,
                stable_id=f"worker-message-{index + 1}",
                visual_id=f"visual-{observation_id}",
            )
            for index, (observation_id, sender_role, content) in enumerate(
                pre_specs
            )
        ]
        runner, _bridge, target, action_id, reserved_id, state_key = (
            self._prepare_ai_send_receipt_fixture(
                conversation_id="conv-send-live-scroll-rebuilt-ids",
                pre_observations=pre,
                next_sequence=6,
                reply_text="好的，您有需要随时发我。",
            )
        )
        post = [
            self._ai_send_observation(
                f"post-{index}",
                sender_role=sender_role,
                content=content,
                visual_id=f"visual-post-{index}",
            )
            for index, (_old_id, sender_role, content) in enumerate(
                pre_specs[1:]
            )
        ]
        post.append(
            self._ai_send_observation(
                "post-ai-reply",
                sender_role="self",
                content="好的，您有需要随时发我。",
                native_id="native-post-ai-reply",
                visual_id="visual-post-ai-reply",
            )
        )

        reply_text = "好的，您有需要随时发我。"
        recorded = runner._record_confirmed_ai_reply_receipt(
            target=target,
            reply_action_id=action_id,
            reply_text_hash=runner._reply_text_hash(reply_text),
            sidecar_result=self._confirmed_send_sidecar_result(
                observations=post,
                confirmed_observation_id="post-ai-reply",
                run_id="send-live-scroll-rebuilt-ids",
            ),
            confirmed_at="2026-08-12T10:27:26+00:00",
        )

        self.assertTrue(recorded)
        state = load_c2_state(state_key)
        self.assertEqual(state["sequence_commits"][action_id], reserved_id)
        receipt = state["ai_reply_receipts"][0]
        self.assertEqual(receipt["worker_stable_id"], reserved_id)
        evidence = receipt["sequence_alignment_evidence"]
        self.assertEqual(evidence["alignment_status"], "unique")
        self.assertEqual(
            [pair["pre_index"] for pair in evidence["matched_pairs"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [pair["post_index"] for pair in evidence["matched_pairs"]],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            evidence["new_suffix_observation_ids"],
            ["post-ai-reply"],
        )

        target.raw["identity_checkpoint"] = {
            "version": 2,
            "next_sequence_floor": 7,
            "recent_messages": [
                {
                    "stable_id": f"worker-message-{index + 1}",
                    "source_message_key": f"source-history-{index + 1}",
                    "sender_role": sender_role,
                    "message_type": "text",
                    "normalized_content_hash": normalized_content_hash(
                        content
                    ),
                    "frame_visual_id": f"checkpoint-visual-{index + 1}",
                }
                for index, (_observation_id, sender_role, content) in enumerate(
                    pre_specs
                )
            ],
        }
        later_read = [
            self._ai_send_observation(
                f"later-{index}",
                sender_role=sender_role,
                content=content,
                visual_id=f"later-visual-{index}",
            )
            for index, (_old_id, sender_role, content) in enumerate(
                pre_specs[1:]
            )
        ]
        later_read.append(
            self._ai_send_observation(
                "later-ai-reply",
                sender_role="self",
                content=reply_text,
                visual_id="later-visual-ai-reply",
            )
        )

        aligned, errors = runner._align_initial_identity_frame(
            target=target,
            sidecar_payload={
                "ok": True,
                "frame_id": "later-no-change-frame",
                "observations": later_read,
            },
            read_run_id="read-after-confirmed-ai-reply",
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            [
                item.get("_worker_stable_id")
                for item in aligned["observations"]
            ],
            [
                "worker-message-2",
                "worker-message-3",
                "worker-message-4",
                "worker-message-5",
                reserved_id,
            ],
        )
        self.assertEqual(
            aligned["sequence_alignment_evidence"]["alignment_status"],
            "unique",
        )
        self.assertEqual(
            aligned["sequence_alignment_evidence"][
                "new_suffix_observation_ids"
            ],
            [],
        )

    def test_ai_send_receipt_commits_action_when_media_history_is_weak(self):
        pre = [
            {
                "observation_id": "old-image",
                "row_kind": "image_bubble",
                "message_type": "image",
                "sender_role": "customer",
                "_worker_stable_id": "worker-message-70",
            },
            {
                "observation_id": "old-voice-1",
                "row_kind": "voice_transcript",
                "message_type": "voice",
                "sender_role": "customer",
                "content_clean": "第一条语音",
                "_worker_stable_id": "worker-message-71",
            },
            {
                "observation_id": "old-voice-2",
                "row_kind": "voice_transcript",
                "message_type": "voice",
                "sender_role": "customer",
                "content_clean": "第二条语音",
                "_worker_stable_id": "worker-message-72",
            },
        ]
        runner, _bridge, target, action_id, reserved_id, state_key = (
            self._prepare_ai_send_receipt_fixture(
                conversation_id="conv-send-media-history",
                pre_observations=pre,
                next_sequence=73,
                reply_text="中午好，在的，您想咨询什么事？",
            )
        )
        sent = self._ai_send_observation(
            "sent-after-media",
            sender_role="self",
            content="中午好，在的，您想咨询什么事？",
        )

        recorded = runner._record_confirmed_ai_reply_receipt(
            target=target,
            reply_action_id=action_id,
            reply_text_hash=runner._reply_text_hash(
                sent["content_clean"]
            ),
            sidecar_result=self._confirmed_send_sidecar_result(
                observations=[*pre, sent],
                confirmed_observation_id="sent-after-media",
                run_id="send-media-history",
            ),
            confirmed_at="2026-08-12T07:24:35+00:00",
        )

        self.assertTrue(recorded)
        receipt = load_c2_state(state_key)["ai_reply_receipts"][0]
        self.assertEqual(receipt["worker_stable_id"], reserved_id)
        self.assertTrue(
            receipt["sequence_alignment_evidence"][
                "action_identity_only"
            ]
        )
        self.assertFalse(
            receipt["sequence_alignment_evidence"][
                "old_tail_fully_consumed"
            ]
        )

    def test_recent_ai_sent_boundary_does_not_restore_one_sided_media_ids(self):
        conversation_id = "conv-recent-ai-sent-media-history"
        reply_text = "中午好，在的，您想咨询什么事？"
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        save_c2_state(
            f"message_identity:{conversation_id}",
            {
                "version": 4,
                "next_sequence": 74,
                "ai_reply_receipts": [
                    {
                        "reply_action_id": "reply-media-history",
                        "reply_text_hash": runner._reply_text_hash(
                            reply_text
                        ),
                        "worker_stable_id": "worker-message-73",
                        "confirmed_at": "2026-08-12T07:24:35+00:00",
                    }
                ],
            },
        )
        recent_messages = [
            {
                "stable_id": "worker-message-70",
                "source_message_key": "source:image",
                "sender_role": "customer",
                "message_type": "image",
                "normalized_content_hash": normalized_content_hash(""),
            },
            {
                "stable_id": "worker-message-71",
                "source_message_key": "source:voice-1",
                "sender_role": "customer",
                "message_type": "voice",
                "normalized_content_hash": normalized_content_hash(
                    "第一条语音"
                ),
            },
            {
                "stable_id": "worker-message-72",
                "source_message_key": "source:voice-2",
                "sender_role": "customer",
                "message_type": "voice",
                "normalized_content_hash": normalized_content_hash(
                    "第二条语音"
                ),
            },
        ]
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key="",
            display_name="CJK7M4Q2",
            remark_code="CJK7M4Q2",
            read_reason="recent_ai_sent",
            authorization_revision="revision-recent-ai-sent",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 74,
                    "recent_messages": recent_messages,
                },
                "ai_reply_boundary": {
                    "reply_action_id": "reply-media-history",
                    "sent_at": "2026-08-12T07:24:35+00:00",
                    "reply_text_hash": runner._reply_text_hash(reply_text),
                    "worker_stable_id": "worker-message-73",
                },
            },
        )
        observations = [
            {
                "observation_id": "current-image",
                "row_kind": "image_bubble",
                "message_type": "image",
                "sender_role": "customer",
            },
            {
                "observation_id": "current-voice-1",
                "row_kind": "voice_transcript",
                "message_type": "voice",
                "sender_role": "customer",
                "content_clean": "第一条语音",
            },
            {
                "observation_id": "current-voice-2",
                "row_kind": "voice_transcript",
                "message_type": "voice",
                "sender_role": "customer",
                "content_clean": "第二条语音",
            },
            self._ai_send_observation(
                "current-ai-reply",
                sender_role="self",
                content=reply_text,
            ),
        ]

        aligned, errors = runner._align_initial_identity_frame(
            target=target,
            sidecar_payload={
                "ok": True,
                "frame_id": "recent-ai-sent-frame",
                "observations": observations,
            },
            read_run_id="read-recent-ai-sent",
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            [
                item.get("_worker_stable_id")
                for item in aligned["observations"]
            ],
            [
                None,
                None,
                None,
                "worker-message-73",
            ],
        )
        self.assertEqual(
            aligned["sequence_alignment_evidence"]["alignment_status"],
            "unique",
        )
        self.assertEqual(
            aligned["sequence_alignment_evidence"][
                "new_suffix_observation_ids"
            ],
            [],
        )

    def test_recent_ai_sent_exposes_only_the_matching_locally_confirmed_text_to_sidecar(self):
        conversation_id = "conv-recent-ai-text-recovery"
        reply_text = (
            "你好，10万左右可以先按你的用车需求筛选合适车型。"
            "你主要是日常通勤、家庭出行，还是更看重大空间？"
        )
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        reply_hash = runner._reply_text_hash(reply_text)
        save_c2_state(
            f"message_identity:{conversation_id}",
            {
                "version": 4,
                "ai_reply_receipts": [
                    {
                        "reply_action_id": "reply-current",
                        "reply_text": reply_text,
                        "reply_text_hash": reply_hash,
                        "worker_stable_id": "worker-message-9",
                    },
                    {
                        "reply_action_id": "reply-old",
                        "reply_text": "旧回复",
                        "reply_text_hash": runner._reply_text_hash("旧回复"),
                        "worker_stable_id": "worker-message-8",
                    },
                ],
            },
        )
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key="",
            display_name="CJNCXB8R",
            remark_code="CJNCXB8R",
            read_reason="recent_ai_sent",
            authorization_revision="revision-current",
            raw={
                "ai_reply_boundary": {
                    "reply_action_id": "reply-current",
                    "reply_text_hash": reply_hash,
                    "worker_stable_id": "worker-message-9",
                }
            },
        )

        self.assertEqual(
            runner._confirmed_ai_reply_text_for_read(target),
            reply_text,
        )
        target.read_reason = "waiting_sales_reply"
        self.assertEqual(runner._confirmed_ai_reply_text_for_read(target), "")

    def test_wrapped_confirmed_ai_reply_keeps_new_voice_executable(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id="conv-wrapped-ai-reply-new-voice",
            rpa_session_key="",
            display_name="CJVOICE9",
            remark_code="CJVOICE9",
            read_reason="recent_ai_sent",
            authorization_revision="revision-wrapped-ai-reply",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 3,
                    "recent_messages": [
                        {
                            "stable_id": "worker-message-1",
                            "source_message_key": "source-customer-1",
                            "sender_role": "customer",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "你好在吗"
                            ),
                        },
                        {
                            "stable_id": "worker-message-2",
                            "source_message_key": "source-ai-2",
                            "sender_role": "self",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "你好，欢迎加上好友，很高兴认识你！请问有什么可以帮您？"
                            ),
                        },
                    ],
                }
            },
        )
        observations = [
            self._ai_send_observation(
                "current-customer",
                sender_role="customer",
                content="你好在吗",
            ),
            self._ai_send_observation(
                "current-ai",
                sender_role="self",
                content="你好，欢迎加上好友，很高兴认识你！请问有\n什么可以帮您？",
            ),
            {
                "observation_id": "new-five-second-voice",
                "row_kind": "voice_bubble",
                "message_type": "voice",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "voice_state": "untranscribed",
                "frame_visual_id": "visual-new-five-second-voice",
                "bubble_rect": [420, 300, 620, 344],
                "source_message": {
                    "id": "new-five-second-voice",
                    "type": "voice",
                    "sender_role": "customer",
                },
            },
        ]

        aligned, errors = runner._align_initial_identity_frame(
            target=target,
            sidecar_payload={
                "ok": True,
                "frame_id": "frame-wrapped-ai-new-voice",
                "observations": observations,
            },
            read_run_id="read-wrapped-ai-new-voice",
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            aligned["sequence_alignment_evidence"]["alignment_status"],
            "unique",
        )
        executable = _executable_untranscribed_voice_observations(
            target,
            aligned,
        )
        self.assertEqual(
            [item["observation_id"] for item in executable],
            ["new-five-second-voice"],
        )

    def test_scrolled_transcribed_voice_does_not_block_new_voice_or_reuse_identity(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id="conv-scrolled-old-voice-new-voice",
            rpa_session_key="",
            display_name="CJNCXB8R",
            remark_code="CJNCXB8R",
            read_reason="waiting_user_reply",
            authorization_revision="revision-scrolled-old-voice",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 7,
                    "recent_messages": [
                        {
                            "stable_id": "worker-message-3",
                            "source_message_key": "source-self-3",
                            "sender_role": "self",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "欢迎你，很高兴认识你"
                            ),
                            "frame_visual_id": "frame-before-self-3",
                        },
                        {
                            "stable_id": "worker-message-5",
                            "source_message_key": "source-voice-5",
                            "sender_role": "customer",
                            "message_type": "voice",
                            "normalized_content_hash": normalized_content_hash(
                                "你好，10万块钱左右的二手车有什么推荐吗？"
                            ),
                            "frame_visual_id": "frame-before-voice-5",
                        },
                        {
                            "stable_id": "worker-message-6",
                            "source_message_key": "source-self-6",
                            "sender_role": "self",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "可以先按你的用车需求筛选合适车型"
                            ),
                            "frame_visual_id": "frame-before-self-6",
                        },
                    ],
                }
            },
        )
        observations = [
            self._ai_send_observation(
                "current-self-3",
                sender_role="self",
                content="欢迎你，很高兴认识你",
                visual_id="legacy-visual-after-self-3",
            ),
            {
                "observation_id": "current-voice-5",
                "row_kind": "voice_transcript",
                "message_type": "voice",
                "sender_role": "customer",
                "sender_role_source": "parent_voice",
                "voice_state": "transcribed",
                "content_clean": "你好，10万块钱左右的二手车有什么推荐吗？",
                "frame_visual_id": "frame-after-voice-5",
            },
            self._ai_send_observation(
                "current-self-6",
                sender_role="self",
                content="可以先按你的用车需求筛选合适车型",
                visual_id="legacy-visual-after-self-6",
            ),
            {
                "observation_id": "new-voice-3",
                "row_kind": "voice_bubble",
                "message_type": "voice",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "voice_state": "untranscribed",
                "content_clean": "",
                "frame_visual_id": "frame-new-voice-3",
                "bubble_rect": [470, 619, 586, 645],
            },
        ]

        aligned, errors = runner._align_initial_identity_frame(
            target=target,
            sidecar_payload={
                "ok": True,
                "frame_id": "frame-current-cjncx",
                "observations": observations,
            },
            read_run_id="read-current-cjncx",
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            aligned["sequence_alignment_evidence"]["alignment_status"],
            "unique",
        )
        self.assertEqual(
            [item.get("_worker_stable_id") for item in aligned["observations"]],
            [
                "worker-message-3",
                "worker-message-5",
                "worker-message-6",
                None,
            ],
        )
        executable = _executable_untranscribed_voice_observations(
            target,
            aligned,
        )
        self.assertEqual(
            [item["observation_id"] for item in executable],
            ["new-voice-3"],
        )

    def test_missing_old_voice_cannot_transfer_checkpoint_id_to_new_tail_voice(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id="conv-old-voice-missing-new-tail",
            rpa_session_key="",
            display_name="CJNCXB8R",
            remark_code="CJNCXB8R",
            read_reason="waiting_user_reply",
            authorization_revision="revision-old-voice-missing",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 12,
                    "recent_messages": [
                        {
                            "stable_id": "worker-message-10",
                            "source_message_key": "source-old-text",
                            "sender_role": "customer",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "前文"
                            ),
                        },
                        {
                            "stable_id": "worker-message-11",
                            "source_message_key": "source-old-voice",
                            "sender_role": "customer",
                            "message_type": "voice",
                            "normalized_content_hash": normalized_content_hash(
                                "旧 5 秒语音"
                            ),
                        },
                    ],
                }
            },
        )
        observations = [
            self._ai_send_observation(
                "current-text",
                sender_role="customer",
                content="前文",
            ),
            {
                "observation_id": "new-voice-3",
                "row_kind": "voice_bubble",
                "message_type": "voice",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "voice_state": "untranscribed",
                "content_clean": "",
                "frame_visual_id": "frame-new-voice-3",
                "bubble_rect": [470, 619, 586, 645],
            },
        ]

        aligned, errors = runner._align_initial_identity_frame(
            target=target,
            sidecar_payload={
                "ok": True,
                "frame_id": "frame-old-voice-missing-new-tail",
                "observations": observations,
            },
            read_run_id="read-old-voice-missing-new-tail",
        )

        self.assertEqual(
            [item["error_code"] for item in errors],
            ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
        )
        new_voice = next(
            item
            for item in aligned["observations"]
            if item["observation_id"] == "new-voice-3"
        )
        self.assertFalse(new_voice.get("_worker_stable_id"))
        self.assertEqual(
            [
                item["observation_id"]
                for item in _executable_untranscribed_voice_observations(
                    target,
                    aligned,
                )
            ],
            ["new-voice-3"],
        )

    def test_missing_old_image_cannot_transfer_checkpoint_id_or_source_key(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        target = WechatReadTarget(
            conversation_id="conv-old-image-missing-new-tail",
            rpa_session_key="",
            display_name="CJNCXB8R",
            remark_code="CJNCXB8R",
            read_reason="waiting_user_reply",
            authorization_revision="revision-old-image-missing",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 12,
                    "recent_messages": [
                        {
                            "stable_id": "worker-message-10",
                            "source_message_key": "source-old-text",
                            "sender_role": "customer",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "前文"
                            ),
                        },
                        {
                            "stable_id": "worker-message-11",
                            "source_message_key": "source-old-image",
                            "sender_role": "customer",
                            "message_type": "image",
                            "normalized_content_hash": normalized_content_hash(
                                ""
                            ),
                        },
                    ],
                }
            },
        )
        observations = [
            self._ai_send_observation(
                "current-text",
                sender_role="customer",
                content="前文",
            ),
            {
                "observation_id": "new-image",
                "row_kind": "image_bubble",
                "message_type": "image",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "voice_state": "not_voice",
                "item_state": "discovered",
                "content_clean": "",
                "frame_visual_id": "frame-new-image",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "bubble_visual_fingerprint": (
                        "dhash64:0123456789abcdef"
                    ),
                },
                "bubble_rect": [470, 619, 686, 745],
            },
        ]

        aligned, errors = runner._align_initial_identity_frame(
            target=target,
            sidecar_payload={
                "ok": True,
                "frame_id": "frame-old-image-missing-new-tail",
                "observations": observations,
            },
            read_run_id="read-old-image-missing-new-tail",
        )

        self.assertEqual(
            [item["error_code"] for item in errors],
            ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
        )
        new_image = next(
            item
            for item in aligned["observations"]
            if item["observation_id"] == "new-image"
        )
        self.assertFalse(new_image.get("_worker_stable_id"))
        with self.assertRaisesRegex(
            ValueError,
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        ):
            image_observation_source_key(target, new_image)

    def test_recent_ai_sent_uses_matching_possible_send_after_restart(self):
        conversation_id = "conv-recent-ai-possible-send"
        reply_text = "我已经回复，等待您的新消息。"
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(
                RpaResult(ok=True, result_code="unused", message="unused")
            ),
        )
        reply_hash = runner._reply_text_hash(reply_text)
        save_c2_state(
            f"possible_ai_sends:{conversation_id}",
            {
                "sends": [
                    {
                        "reply_action_id": "reply-possible-restart",
                        "reply_text_hash": reply_hash,
                        "reserved_worker_stable_id": "worker-message-42",
                        "reconciliation_state": "ai_unreconciled",
                    }
                ]
            },
        )
        target = WechatReadTarget(
            conversation_id=conversation_id,
            rpa_session_key="",
            display_name="CJWAIT01",
            remark_code="CJWAIT01",
            read_reason="recent_ai_sent",
            authorization_revision="revision-possible-restart",
            raw={
                "identity_checkpoint": {
                    "version": 2,
                    "next_sequence_floor": 42,
                    "recent_messages": [
                        {
                            "stable_id": "worker-message-41",
                            "source_message_key": "source-customer-41",
                            "sender_role": "customer",
                            "message_type": "text",
                            "normalized_content_hash": normalized_content_hash(
                                "请回复我"
                            ),
                        }
                    ],
                },
                "ai_reply_boundary": {
                    "reply_action_id": "reply-possible-restart",
                    "sent_at": "2026-08-14T08:00:00+00:00",
                    "reply_text_hash": reply_hash,
                    "worker_stable_id": "",
                },
            },
        )
        observations = [
            self._ai_send_observation(
                "customer-41",
                sender_role="customer",
                content="请回复我",
            ),
            self._ai_send_observation(
                "possible-ai-42",
                sender_role="self",
                content=reply_text,
            ),
        ]

        aligned, errors = runner._align_initial_identity_frame(
            target=target,
            sidecar_payload={
                "ok": True,
                "frame_id": "possible-restart-frame",
                "observations": observations,
            },
            read_run_id="read-possible-restart",
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            [
                item.get("_worker_stable_id")
                for item in aligned["observations"]
            ],
            ["worker-message-41", "worker-message-42"],
        )
        self.assertEqual(
            aligned["sequence_alignment_evidence"][
                "new_suffix_observation_ids"
            ],
            [],
        )

    def test_ai_send_crash_state_keeps_possible_sent_and_reuses_reservation(self):
        pre = [
            self._ai_send_observation(
                "crash-old",
                sender_role="customer",
                content="请回复",
                stable_id="worker-message-60",
            )
        ]
        runner, _bridge, target, action_id, reserved_id, _state_key = (
            self._prepare_ai_send_receipt_fixture(
                conversation_id="conv-send-crash",
                pre_observations=pre,
                next_sequence=61,
                reply_text="请回复",
            )
        )
        journal_path = runner.bridge.send_transaction_journal_path(action_id)
        update_action_journal_item(
            journal_path,
            journal_item_id=action_id,
            action_phase="trigger_attempted",
            business_state="send_triggered_before_worker_crash",
        )

        recovered_reservation = runner._reserve_worker_sequence(
            target,
            reservation_key=f"send-action:{action_id}",
        )
        unchanged = runner._attach_possible_ai_send_receipts(
            target=target,
            observations=[
                self._ai_send_observation(
                    "same-text-after-crash",
                    sender_role="self",
                    content="请回复",
                    stable_id="worker-message-999",
                )
            ],
        )

        self.assertEqual(recovered_reservation, reserved_id)
        self.assertEqual(action_journal_phase(journal_path), "trigger_attempted")
        self.assertNotIn("_worker_ai_reply_receipt", unchanged[0])
        self.assertEqual(
            load_c2_state(
                f"possible_ai_sends:{target.conversation_id}"
            )["sends"][0]["reconciliation_state"],
            "armed_before_trigger",
        )

    def test_ai_send_receipt_never_downgrades_newer_identity_state_version(self):
        pre = [
            self._ai_send_observation(
                "version-old",
                sender_role="customer",
                content="版本测试",
                stable_id="worker-message-99",
                native_id="native-version-old",
            )
        ]
        runner, _bridge, target, action_id, reserved_id, state_key = (
            self._prepare_ai_send_receipt_fixture(
                conversation_id="conv-send-version",
                pre_observations=pre,
                next_sequence=100,
                state_version=9,
                reply_text="已收到",
            )
        )
        post = [
            dict(pre[0]),
            self._ai_send_observation(
                "version-sent",
                sender_role="self",
                content="已收到",
                native_id="native-version-sent",
            ),
        ]

        self.assertTrue(
            runner._record_confirmed_ai_reply_receipt(
                target=target,
                reply_action_id=action_id,
                reply_text_hash=runner._reply_text_hash("已收到"),
                sidecar_result=self._confirmed_send_sidecar_result(
                    observations=post,
                    confirmed_observation_id="version-sent",
                    run_id="send-version",
                ),
                confirmed_at="2026-08-11T00:00:00+00:00",
            )
        )
        state = load_c2_state(state_key)
        self.assertEqual(state["version"], 9)
        self.assertEqual(state["next_sequence"], 101)
        self.assertEqual(state["sentinel"], "must-survive")
        self.assertEqual(state["sequence_commits"][action_id], reserved_id)

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

    def test_same_duration_voice_actions_reserve_distinct_nonreusable_ids(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-reservation-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:reservation",
            display_name="CJRSV001",
            remark_code="CJRSV001",
            authorization_revision="revision-reservation",
            raw={"identity_checkpoint": identity_checkpoint(next_sequence_floor=11)},
        )

        first = runner._reserve_worker_sequence(
            target,
            reservation_key="voice-action:first-3-seconds",
        )
        second = runner._reserve_worker_sequence(
            target,
            reservation_key="voice-action:second-3-seconds",
        )
        retried_first = runner._reserve_worker_sequence(
            target,
            reservation_key="voice-action:first-3-seconds",
        )

        self.assertEqual(first, "worker-message-11")
        self.assertEqual(second, "worker-message-12")
        self.assertEqual(retried_first, first)

    def test_new_suffix_gets_next_sequence_without_reordering_existing_outbox_ids(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-tail-order-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:tail-order",
            display_name="CJORDER1",
            remark_code="CJORDER1",
            authorization_revision="revision-tail-order",
            raw={"identity_checkpoint": identity_checkpoint(next_sequence_floor=14)},
        )
        observations = [
            {
                "observation_id": observation_id,
                "row_kind": "text_bubble",
                "message_type": "text",
                "voice_state": "not_voice",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "content_clean": content,
                **(
                    {"_worker_stable_id": f"worker-message-{sequence}"}
                    if sequence is not None
                    else {}
                ),
            }
            for observation_id, content, sequence in (
                ("text-1", "文字1", 10),
                ("voice-1", "语音1", 11),
                ("image-1", "图片1", 12),
                ("text-2", "好的", 13),
                ("text-3", "好的", None),
            )
        ]
        evidence = {
            "alignment_status": "unique",
            "old_tail_fully_consumed": True,
            "new_suffix_observation_ids": ["text-3"],
        }

        assigned = runner._assign_sequence_new_suffix_identities(
            target=target,
            observations=observations,
            evidence=evidence,
            read_run_id="read-tail-order",
        )

        self.assertEqual(
            [item.get("_worker_stable_id") for item in assigned],
            [
                "worker-message-10",
                "worker-message-11",
                "worker-message-12",
                "worker-message-13",
                "worker-message-14",
            ],
        )

    def test_image_processor_executes_only_one_candidate_per_authoritative_frame(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-one-image-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:one-image",
            display_name="CJIMG001",
            remark_code="CJIMG001",
            authorization_revision="revision-one-image",
        )

        def image(observation_id: str, sequence: int, top: int) -> dict:
            return {
                "schema_version": 3,
                "observation_id": observation_id,
                "row_kind": "image_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "image",
                "voice_state": "not_voice",
                "item_state": "discovered",
                "frame_visual_id": f"visual-{observation_id}",
                "bubble_rect": [420, top, 650, top + 80],
                "_worker_stable_id": f"worker-message-{sequence}",
                "_worker_identity_scope": "current_read_provisional",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "bubble_visual_fingerprint": (
                        f"dhash64:{sequence:016x}"
                    ),
                },
                "source_message": {
                    "id": observation_id,
                    "frame_visual_id": f"visual-{observation_id}",
                    "type": "image",
                    "sender_role": "customer",
                },
            }

        first = image("image-first", 41, 100)
        second = image("image-second", 42, 220)
        allowed = {
            str(first["observation_id"]),
            str(second["observation_id"]),
        }
        terminal = {
            "state": "failed",
            "action_phase": "trigger_attempted",
            "business_state": "failed",
            "business_result_confirmed": False,
            "reason": "menu_panel_unconfirmed",
            "transaction": {
                "action_phase": "trigger_attempted",
                "status": "menu_panel_unconfirmed",
            },
            "diagnostics": {"events": [], "image_persisted": False},
        }
        with patch.object(
            runner,
            "_execute_one_image_slot_vision",
            return_value=terminal,
        ) as execute:
            payload, stats = runner._process_final_image_slots(
                binding=Binding(
                    worker_id="worker-1",
                    worker_token="token",
                    client_instance_id="client-1",
                    run_status="running",
                ),
                target=target,
                sidecar_payload={
                    "authoritative_frame_source": "final_read",
                    "sidecar_run_id": "frame-two-images",
                    "observations": [first, second],
                },
                enforce_read_targets=False,
                allowed_new_observation_ids=allowed,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-two-images"
                ),
            )

        self.assertEqual(execute.call_count, 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["completed"], 0)
        self.assertEqual(
            [
                item.get("observation_id")
                for item in payload["observations"]
            ],
            ["image-second"],
        )
        self.assertEqual(
            stats["terminal_gate"]["error_code"],
            "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        )

    def test_zero_ui_image_cancellation_defers_next_image_to_fresh_frame(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-zero-ui-images-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:zero-ui-images",
            display_name="CJIMG002",
            remark_code="CJIMG002",
            authorization_revision="revision-zero-ui-images",
        )

        def image(observation_id: str, sequence: int, top: int) -> dict:
            return {
                "schema_version": 3,
                "observation_id": observation_id,
                "row_kind": "image_bubble",
                "sender_role": "customer",
                "sender_role_source": "same_row_avatar",
                "message_type": "image",
                "voice_state": "not_voice",
                "item_state": "discovered",
                "frame_visual_id": f"visual-{observation_id}",
                "bubble_rect": [420, top, 650, top + 80],
                "_worker_stable_id": f"worker-message-{sequence}",
                "_worker_identity_scope": "current_read_provisional",
                "image_physical_anchor": {
                    "sender_role": "customer",
                    "bubble_visual_fingerprint": (
                        f"dhash64:{sequence:016x}"
                    ),
                },
                "source_message": {
                    "id": observation_id,
                    "frame_visual_id": f"visual-{observation_id}",
                    "type": "image",
                    "sender_role": "customer",
                },
            }

        first = image("image-zero-ui", 51, 100)
        second = image("image-with-ui", 52, 220)
        allowed = {
            str(first["observation_id"]),
            str(second["observation_id"]),
        }
        zero_ui_failure = {
            "state": "failed",
            "action_phase": "not_attempted",
            "business_state": "failed",
            "business_result_confirmed": False,
            "reason": "C2_IMAGE_SLOT_RECONFIRM_FAILED",
            "transaction": {"action_phase": "not_attempted"},
            "diagnostics": {"events": [], "image_persisted": False},
        }
        triggered_failure = {
            "state": "failed",
            "action_phase": "trigger_attempted",
            "business_state": "failed",
            "business_result_confirmed": False,
            "reason": "menu_panel_unconfirmed",
            "transaction": {
                "action_phase": "trigger_attempted",
                "status": "menu_panel_unconfirmed",
            },
            "diagnostics": {"events": [], "image_persisted": False},
        }
        with patch.object(
            runner,
            "_execute_one_image_slot_vision",
            side_effect=[zero_ui_failure, triggered_failure],
        ) as execute:
            payload, stats = runner._process_final_image_slots(
                binding=Binding(
                    worker_id="worker-1",
                    worker_token="token",
                    client_instance_id="client-1",
                    run_status="running",
                ),
                target=target,
                sidecar_payload={
                    "authoritative_frame_source": "initial_read",
                    "sidecar_run_id": "frame-zero-ui-images",
                    "observations": [first, second],
                },
                enforce_read_targets=False,
                allowed_new_observation_ids=allowed,
                flow_outcomes=FlowOutcomeAccumulator(
                    origin_read_run_id="read-zero-ui-images"
                ),
            )

        self.assertEqual(execute.call_count, 1)
        self.assertEqual(stats["failed"], 0)
        self.assertFalse(stats["ui_frame_invalidated"])
        self.assertNotIn("terminal_gate", stats)
        self.assertEqual(
            [
                item.get("observation_id")
                for item in payload["observations"]
            ],
            ["image-with-ui"],
        )

    def test_triggered_voice_without_post_alignment_blocks_reclick_on_recovery(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-crash-before-post-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:crash-before-post",
            display_name="CJCRSH01",
            remark_code="CJCRSH01",
            authorization_revision="revision-crash",
        )
        action_id = f"voice:{target.conversation_id}:action"
        path = action_journal_path("voice", action_id)
        initialize_action_journal(
            path,
            action_kind="voice",
            transaction_id=action_id,
            conversation_id=target.conversation_id,
            origin_read_run_id="read-before-crash",
            canonical_action_id=action_id,
            reserved_worker_stable_id="worker-message-21",
            pre_frame_id="frame-before-crash",
            pre_action_identity_sequence=[
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": "worker-message-21",
                    "pre_observation_id": "voice-before-crash",
                    "pre_sequence_index": 0,
                    "sender_role": "customer",
                    "message_type": "voice",
                    "normalized_content_hash": "",
                    "native_source_message_id": "",
                    "frame_visual_id": "",
                }
            ],
            prepare_evidence={
                "pre_frame_id": "frame-before-crash",
                "selected_pre_observation_id": "voice-before-crash",
                "selected_action_token": "token-before-crash",
                "selected_target_fingerprint": "fingerprint-before-crash",
                "candidate_group_count": 1,
                "ui_action_performed": False,
            },
            items=[
                {
                    "journal_item_id": action_id,
                    "physical_anchor_keys": ["voice-anchor-before-crash"],
                }
            ],
        )
        update_action_journal_item(
            path,
            journal_item_id=action_id,
            action_phase="trigger_attempted",
            business_state="failed",
            business_result_confirmed=False,
            error_code="VOICE_INTERRUPTED_AFTER_TRIGGER",
        )

        unresolved = runner._recover_physical_action_journals(target)

        self.assertEqual(len(unresolved), 1)
        self.assertEqual(
            unresolved[0]["reason"],
            "action_triggered_without_confirmed_post_alignment",
        )
        self.assertEqual(action_journal_phase(path), "quarantined")
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, action_id)
        )
        self.assertTrue(path.exists())

    def test_pretrigger_crash_discards_action_but_never_reuses_reservation(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-crash-pretrigger-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:crash-pretrigger",
            display_name="CJCRSH03",
            remark_code="CJCRSH03",
            authorization_revision="revision-crash-pretrigger",
            raw={
                "identity_checkpoint": identity_checkpoint(
                    next_sequence_floor=51
                )
            },
        )
        action_id = f"voice:{target.conversation_id}:first"
        reserved_id = runner._reserve_worker_sequence(
            target,
            reservation_key=f"selected-action:{action_id}",
        )
        path = action_journal_path("voice", action_id)
        initialize_action_journal(
            path,
            action_kind="voice",
            transaction_id=action_id,
            conversation_id=target.conversation_id,
            origin_read_run_id="read-pretrigger-crash",
            canonical_action_id=action_id,
            reserved_worker_stable_id=reserved_id,
            pre_frame_id="frame-pretrigger",
            pre_action_identity_sequence=[
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                    "pre_observation_id": "voice-pretrigger",
                    "pre_sequence_index": 0,
                    "sender_role": "customer",
                    "message_type": "voice",
                    "normalized_content_hash": "",
                    "native_source_message_id": "",
                    "frame_visual_id": "",
                }
            ],
            prepare_evidence={
                "pre_frame_id": "frame-pretrigger",
                "selected_pre_observation_id": "voice-pretrigger",
                "selected_action_token": "token-pretrigger",
                "selected_target_fingerprint": "fingerprint-pretrigger",
                "candidate_group_count": 1,
                "ui_action_performed": False,
            },
            items=[
                {
                    "journal_item_id": action_id,
                    "physical_anchor_keys": ["voice-pretrigger-anchor"],
                }
            ],
        )

        self.assertEqual(runner._recover_physical_action_journals(target), [])
        self.assertFalse(path.exists())
        next_id = runner._reserve_worker_sequence(
            target,
            reservation_key=(
                f"selected-action:voice:{target.conversation_id}:second"
            ),
        )
        self.assertNotEqual(next_id, reserved_id)
        self.assertEqual(reserved_id, "worker-message-51")
        self.assertEqual(next_id, "worker-message-52")

    def test_unique_post_alignment_recovers_reserved_voice_identity_after_crash(self):
        runner, _ = self.make_runner(
            FakeApi(None),
            FakeBridge(RpaResult(ok=True, result_code="ok", message="unused")),
        )
        target = WechatReadTarget(
            conversation_id=f"conv-crash-after-post-{time.time_ns()}",
            rpa_session_key="wx:rpa:v1:crash-after-post",
            display_name="CJCRSH02",
            remark_code="CJCRSH02",
            authorization_revision="revision-crash-post",
        )
        action_id = f"voice:{target.conversation_id}:action"
        reserved_id = "worker-message-31"
        path = action_journal_path("voice", action_id)
        initialize_action_journal(
            path,
            action_kind="voice",
            transaction_id=action_id,
            conversation_id=target.conversation_id,
            origin_read_run_id="read-after-post-crash",
            canonical_action_id=action_id,
            reserved_worker_stable_id=reserved_id,
            pre_frame_id="frame-before-post",
            pre_action_identity_sequence=[
                {
                    "identity_state": "selected_action",
                    "canonical_action_id": action_id,
                    "reserved_worker_stable_id": reserved_id,
                    "pre_observation_id": "voice-before-post",
                    "pre_sequence_index": 0,
                    "sender_role": "customer",
                    "message_type": "voice",
                    "normalized_content_hash": "",
                    "native_source_message_id": "",
                    "frame_visual_id": "",
                }
            ],
            prepare_evidence={
                "pre_frame_id": "frame-before-post",
                "selected_pre_observation_id": "voice-before-post",
                "selected_action_token": "token-before-post",
                "selected_target_fingerprint": "fingerprint-before-post",
                "candidate_group_count": 1,
                "ui_action_performed": False,
            },
            items=[
                {
                    "journal_item_id": action_id,
                    "physical_anchor_keys": ["voice-anchor-after-post"],
                }
            ],
        )
        update_action_journal_item(
            path,
            journal_item_id=action_id,
            action_phase="confirmed",
            business_state="completed",
            business_result_confirmed=True,
            terminal_payload={"state": "completed"},
        )
        record_action_sequence_alignment(
            path,
            {
                "pre_sequence_source": "action_frame",
                "pre_frame_id": "frame-before-post",
                "post_frame_id": "frame-after-post",
                "alignment_status": "unique",
                "candidate_alignment_count": 1,
                "matched_pairs": [
                    {
                        "identity_state": "selected_action",
                        "worker_stable_id": reserved_id,
                        "pre_observation_id": "voice-before-post",
                        "post_observation_id": "voice-after-post",
                        "pre_index": 0,
                        "post_index": 0,
                        "match_basis": "confirmed_action",
                    }
                ],
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [],
            },
        )

        unresolved = runner._recover_physical_action_journals(target)
        durable_source_key = worker_source_message_key(
            target,
            identity_kind="worker_sequence",
            identity=reserved_id,
        )

        self.assertEqual(unresolved, [])
        self.assertIsNone(
            load_c2_ledger_entry(target.conversation_id, action_id)
        )
        self.assertEqual(
            load_c2_ledger_entry(
                target.conversation_id,
                durable_source_key,
            )["terminal_state"],
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
