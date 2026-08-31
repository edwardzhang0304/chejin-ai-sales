from fastapi.testclient import TestClient
import hashlib
import importlib
import json
import pytest
import shutil
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import sys
from sqlalchemy import select

from app.contracts.c2 import c2_contract_v3, contract_revision, contract_sha256
from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models.c3 import (
    Conversation,
    HandoffEvent,
    MessageBatch,
    ReplyAction,
    ReplyActionVehicleFact,
    SentAck,
)
from app.models.task import Task
from app.models.vehicle import KnowledgeItem
from app.models.wechat import MessageEvent, WechatSessionBinding
from app.services.wechat_service import _authorization_revision, _read_reason
from app.errors import AppError
from app.services import c3_service
from app.services.c3_recovery import (
    recover_due_message_batches_once,
    recover_stale_reply_sends_once,
)
from app.services.ai_adapter import RealOmniAutoAIEngineAdapter
from app.services.message_contract import canonical_reply_text, reply_text_hash
from app.models.base import utcnow


WORKER_CLIENT_ROOT = Path(__file__).resolve().parents[2] / "worker-client"
if str(WORKER_CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_CLIENT_ROOT))

from chejin_worker_client.pre_send_checkpoint import (
    checkpoint_binding_error as worker_checkpoint_binding_error,
    compare_checkpoint_to_observations as worker_compare_checkpoint,
)
from chejin_worker_client.message_identity_commit import (
    MessageCommitBasis,
    committed_identity_record,
)
from chejin_worker_client.message_viewport_projection import (
    boundary_tokens_for_observations,
    normalized_business_message_sequence,
)


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}
INTERNAL_HEADERS = {"X-Internal-Service-Token": "dev-only-internal-service-token-change-before-production"}
FORBIDDEN_RESPONSE_FIELDS = {
    "runtime_status",
    "current_task_id",
    "client_bind_status",
    "status_flow",
    "executor_status",
    "lead_status",
    "reason_code",
    "ingest_status",
    "notify_status",
}
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
)


def _generate_reply_decision_with_isolated_failure_evidence(
    adapter: RealOmniAutoAIEngineAdapter,
    *,
    conversation_context: dict,
    message_batch: dict,
):
    """Preserve child-process evidence in CI assertion output.

    AppError deliberately keeps its structured data out of ``str(exc)``.
    These production-boundary tests need that already-sanitized data when a
    platform-specific isolated worker failure occurs; otherwise Windows CI
    reports only the generic message and hides the actual failed stage.
    """

    try:
        return adapter.generate_reply_decision(
            conversation_context=conversation_context,
            message_batch=message_batch,
        )
    except AppError as exc:
        pytest.fail(
            f"isolated Brain failed: code={exc.code!r}, data={exc.data!r}"
        )


def _adapter_request_with_frozen_context(
    *,
    conversation_context: dict,
    message_batch: dict,
) -> dict:
    """Build the same empty-history snapshot required by the real adapter.

    Adapter unit tests may replace the external Provider boundary, but they
    must not bypass the production CheJin context bridge.
    """

    context = dict(conversation_context)
    batch = dict(message_batch)
    normalized_messages: list[dict] = []
    for index, value in enumerate(message_batch.get("messages") or []):
        item = dict(value)
        item["id"] = str(
            item.get("id")
            or item.get("message_event_id")
            or f"{batch.get('id') or 'batch'}-message-{index + 1}"
        )
        item.setdefault("sender_role", "customer")
        item.setdefault("message_type", "text")
        normalized_messages.append(item)
    batch["messages"] = normalized_messages
    conversation_id = str(context.get("conversation_id") or "")
    context["brain_context_snapshot"] = {
        "schema_version": 1,
        "history_authority": "chejin_message_events_v1",
        "conversation_id": conversation_id,
        "prior_messages": [],
        "current_batch_message_ids": [
            item["id"] for item in normalized_messages
        ],
        "history_event_count_before_batch": 0,
        "semantic_history_count_before_batch": 0,
        "prior_messages_sha256": hashlib.sha256(b"[]").hexdigest(),
        "history_window_complete": True,
    }
    return {"conversation_context": context, "message_batch": batch}


def _generate_adapter_decision(
    adapter: RealOmniAutoAIEngineAdapter,
    *,
    conversation_context: dict,
    message_batch: dict,
):
    return adapter.generate_reply_decision(
        **_adapter_request_with_frozen_context(
            conversation_context=conversation_context,
            message_batch=message_batch,
        )
    )


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _assert_no_forbidden_fields(value):
    if isinstance(value, dict):
        forbidden = FORBIDDEN_RESPONSE_FIELDS.intersection(value.keys())
        assert not forbidden, f"deprecated response fields leaked: {forbidden}"
        for item in value.values():
            _assert_no_forbidden_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_fields(item)


def _create_worker() -> dict:
    response = client.post(
        "/api/workers",
        json={"worker_name": "C3 Worker", "device_name": "Windows PC", "platform": "windows", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    worker = response.json()["data"]
    bind = client.post(
        f"/api/workers/{worker['id']}/client-bind",
        json={"worker_token": worker["worker_token"], "client_instance_id": "client-c3"},
    )
    assert bind.status_code == 200
    heartbeat = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={
            "client_instance_id": "client-c3",
            "run_status": "running",
            "rpa_component_status": "ready",
            "wechat_status": "logged_in",
            "running_status": "idle",
        },
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert heartbeat.status_code == 200
    return worker


def _worker_headers(worker: dict) -> dict:
    return {"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-c3"}


def _task_lease_headers(worker: dict, claim_response) -> dict:
    token = int(claim_response.json()["data"]["lease_fencing_token"])
    assert token > 0
    return {
        **_worker_headers(worker),
        "X-Task-Lease-Fencing-Token": str(token),
    }


def _create_sales(worker_id: str) -> str:
    response = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True, "sort_order": 10, "worker_id": worker_id},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]["id"]


def _create_lead(name: str = "王先生", phone: str = "13896676678", remark_code: str = "C3TEST01") -> dict:
    response = client.post(
        "/api/leads",
        json={"customer_name": name, "phones": [phone], "remark": "预算 10 万", "custom_fields": {"remark_code": remark_code}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _scan(worker: dict, remark_code: str = "C3TEST01") -> dict:
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json={
            "scan_id": "scan-c3-001",
            "sidecar_run_id": "sidecar-c3-001",
            "started_at": "2026-06-23T10:00:00+08:00",
            "finished_at": "2026-06-23T10:00:02+08:00",
            "sessions": [
                {
                    "rpa_session_key": "wx-c3-row-001",
                    "display_name": f"{remark_code} 王先生",
                    "remark_code_candidates": [remark_code],
                    "row_fingerprint": "wx-c3-row-fp-001",
                    "unread_hint": True,
                    "last_message_preview": "你好",
                    "ocr_confidence": 0.98,
                }
            ],
        },
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200
    return response.json()["data"]["bindings"][0]


def _setup_bound_conversation() -> tuple[dict, dict]:
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead()
    binding = _scan(worker)
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.friend_state = "friend_active"
        conversation.status = "waiting_sales_reply"
        db.commit()
    return worker, binding


def _ingest(
    worker: dict,
    conversation_id: str,
    dedupe_key: str,
    content: str,
    *,
    authoritative_frame_source: str = "final_read",
) -> str:
    return _ingest_with_role(
        worker,
        conversation_id,
        dedupe_key,
        content,
        "customer",
        authoritative_frame_source=authoritative_frame_source,
    )


def _ingest_with_role(
    worker: dict,
    conversation_id: str,
    dedupe_key: str,
    content: str,
    role: str,
    *,
    authoritative_frame_source: str = "final_read",
) -> str:
    continuation: dict[str, str] = {}
    with SessionLocal() as db:
        binding = db.query(WechatSessionBinding).filter(WechatSessionBinding.conversation_id == conversation_id).one()
        remark_code = binding.remark_code
        authorization_revision = _authorization_revision(binding)
        unread_generation = int(binding.unread_generation or 0)
        conversation = db.get(Conversation, conversation_id)
        authorization_read_reason = _read_reason(binding, conversation) or "waiting_sales_reply"
        continuation_batch = (
            db.query(MessageBatch)
            .filter(
                MessageBatch.conversation_id == conversation_id,
                MessageBatch.continuation_authorization_revision.is_not(None),
            )
            .order_by(MessageBatch.created_at.desc(), MessageBatch.id.desc())
            .first()
        )
        continuation_batch_id = (
            continuation_batch.id
            if conversation.status == "ai_active" and continuation_batch is not None
            else None
        )
    if continuation_batch_id:
        batch_status = client.get(
            f"/api/workers/{worker['id']}/wechat/message-batches/{continuation_batch_id}",
            headers=_worker_headers(worker),
        )
        assert batch_status.status_code == 200
        authorization = batch_status.json()["data"]["authorization"]
        assert authorization["allowed"] is True
        authorization_revision = authorization["authorization_revision"]
        authorization_read_reason = authorization["read_reason"]
        continuation = {
            "continuation_batch_id": continuation_batch_id,
            "continuation_token": authorization["continuation_token"],
        }
    read_run_id = f"read-{dedupe_key}"
    observation_id = f"observation:{dedupe_key}"
    worker_stable_id = (
        "worker-message-"
        + str(
            int(
                hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[
                    :12
                ],
                16,
            )
        )
    )
    observation = {
        "schema_version": 3,
        "observation_id": observation_id,
        "row_kind": "text_bubble",
        "sender_role": role,
        "sender_role_source": "same_row_avatar",
        "message_type": "text",
        "voice_state": "not_voice",
        "content_clean": content,
        "_worker_stable_id": worker_stable_id,
        "_worker_identity_scope": "committed",
        "source_message": {
            "id": dedupe_key,
            "type": "text",
            "sender_role": role,
            "content": content,
        },
    }
    commit_record = committed_identity_record(
        worker_stable_id=worker_stable_id,
        commit_basis=MessageCommitBasis.NEW_SUFFIX,
        observation_id=observation_id,
        sender_role=role,
        message_type="text",
        proof={
            "alignment_status": "not_required",
            "old_tail_fully_consumed": True,
            "new_suffix_observation_id": observation_id,
        },
    )
    observation["_worker_committed_message"] = commit_record
    business_projection = normalized_business_message_sequence(
        [observation],
        message_viewport_bounds=None,
    )[0]
    strong_boundary_tokens = sorted(
        boundary_tokens_for_observations(
            [observation],
            committed_only=True,
        ).get(0, set())
    )
    raw_payload = {
        "contract_version": 3,
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
        "source_message_key": dedupe_key,
        "dedupe_basis": {
            "source": "worker_cross_round_sequence",
            "worker_stable_id": worker_stable_id,
        },
        "observation": observation,
        "business_projection": business_projection,
        "strong_boundary_tokens": strong_boundary_tokens,
        "message_identity_commit_record": commit_record,
        "message_identity_runtime_evidence": {},
    }
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            "contract_version": 3,
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
            "read_run_id": read_run_id,
            "conversation_id": conversation_id,
            "remark_code": remark_code,
            "rpa_session_key": "wx-c3-row-001",
                "authorization_revision": authorization_revision,
                "unread_generation": unread_generation,
            "messages": [
                {
                    "dedupe_key": dedupe_key,
                    "source_message_key": dedupe_key,
                    "sender_role_hint": role,
                    "message_type": "text",
                    "content": content,
                    "item_state": "completed",
                    "flow_state": "completed",
                    "message_position": {
                        "screen_order": 1,
                        "frame_source": authoritative_frame_source,
                        "order_source": "observation_index_fallback",
                    },
                    "raw_payload": raw_payload,
                }
            ],
            "evidence": {
                "contract_revision": contract_revision(),
                "contract_sha256": contract_sha256(),
                "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
                "authoritative_frame_source": authoritative_frame_source,
                "observations": [raw_payload["observation"]],
                "read_reason": authorization_read_reason,
                "authorization_read_reason": authorization_read_reason,
                **continuation,
                "finished_at": utcnow().isoformat(),
                "flow_gate_errors": [],
                "flow_gate_details": [],
                    "slot_ledger_states": [
                        {
                        "observation_id": observation_id,
                        "screen_order": 1,
                        "order_source": "observation_index_fallback",
                        "row_kind": "text_bubble",
                        "source_message_key": dedupe_key,
                        "origin_read_run_id": read_run_id,
                        "fact_scope": "current_read_run",
                        "delivery_state": "not_enqueued",
                            "item_state": "completed",
                        }
                    ],
                    "sequence_alignment_evidence": {
                        "pre_sequence_source": "empty_checkpoint",
                        "pre_frame_id": (
                            f"checkpoint:none:{conversation_id}"
                        ),
                        "post_frame_id": f"frame:{read_run_id}",
                        "alignment_status": "not_required",
                        "candidate_alignment_count": 0,
                        "matched_pairs": [],
                        "old_tail_fully_consumed": True,
                        "new_suffix_observation_ids": [observation_id],
                    },
                },
            },
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]["results"][0]
    assert result["ingest_result"] == "ingested"
    assert "ingest_status" not in result
    return result["message_event_id"]


def _collect(conversation_id: str, message_event_id: str) -> dict:
    response = client.post(
        f"/api/internal/conversations/{conversation_id}/message-batches/collect",
        json={"trigger_message_event_id": message_event_id, "trace_id": "trace-c3-test"},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _generate(batch_id: str) -> dict:
    response = client.post(f"/api/internal/message-batches/{batch_id}/generate", json={}, headers=INTERNAL_HEADERS)
    assert response.status_code == 200
    return response.json()["data"]


def _create_listed_vehicle() -> str:
    created = client.post(
        "/api/vehicles",
        json={
            "display_name": "2024款发送门禁测试车",
            "brand": "车金测试",
            "series": "发送门禁系列",
            "public_price": 12.88,
            "customer_description": "适合城市通勤。",
        },
        headers=HEADERS,
    )
    assert created.status_code == 200, created.text
    vehicle_id = created.json()["data"]["vehicle_code"]
    uploaded = client.post(
        f"/api/vehicles/{vehicle_id}/images",
        files={"files": ("vehicle.png", PNG_1X1, "image/png")},
        headers=HEADERS,
    )
    assert uploaded.status_code == 200, uploaded.text
    listed = client.post(f"/api/vehicles/{vehicle_id}/list", headers=HEADERS)
    assert listed.status_code == 200, listed.text
    return vehicle_id


class _VehicleFactReplyAdapter:
    def __init__(self, vehicle_id: str):
        self.vehicle_id = vehicle_id

    def generate_reply_decision(self, **_kwargs):
        return c3_service.AIEngineDecision(
            decision="send_reply",
            reply_text="这款车目前在售，公开售价是12.88万元。",
            confidence=0.95,
            guard_result="pass",
            evidence_refs=[f"product_master:{self.vehicle_id}"],
            raw_payload={
                "omniauto_brain_result": {
                    "brain_plan": {
                        "recommended_action": "send_reply",
                        "evidence_used": {"product_ids": [self.vehicle_id]},
                        "facts_claimed": [
                            {
                                "fact_type": "price",
                                "value": "12.88万元",
                                "source_level": "product_master",
                                "source_id": self.vehicle_id,
                            }
                        ],
                        "reply_segments": ["这款车目前在售，公开售价是12.88万元。"],
                    }
                }
            },
        )


def _reset_batch_to_generation_state(
    batch_id: str,
    *,
    status: str,
    generation_attempt_count: int,
    generation_started_at=None,
) -> None:
    with SessionLocal() as db:
        action_ids = list(
            db.scalars(select(ReplyAction.id).where(ReplyAction.batch_id == batch_id))
        )
        if action_ids:
            db.query(Task).filter(Task.reply_action_id.in_(action_ids)).delete(
                synchronize_session=False
            )
            db.query(ReplyAction).filter(ReplyAction.id.in_(action_ids)).delete(
                synchronize_session=False
            )
        row = db.get(MessageBatch, batch_id)
        row.status = status
        row.active = True
        row.retryable = status == "retry_wait"
        row.decision = None
        row.error_code = None
        row.generation_attempt_count = generation_attempt_count
        row.generation_started_at = generation_started_at
        db.commit()


def test_real_adapter_uses_guard_approved_brain_text_without_rewriting(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    brain_result = {
        "rule_name": "customer_service_brain_reply",
        "adoptable": True,
        "visible_reply_source": "brain_plan.reply_segments",
        "reply_text": "这是 Guard 批准的 Brain 原文。",
        "guard_verdict": "pass",
        "brain_plan": {
            "recommended_action": "send_reply",
            "confidence": 0.91,
            "risk_flags": [],
            "evidence_refs": ["rag:vehicle:001"],
            "reply_segments": ["这是 Guard 批准的 Brain 原文。"],
        },
    }
    monkeypatch.setattr(adapter, "_load_config", lambda: {"customer_service_brain": {"provider": "test", "model": "test", "api_key": "test-only"}})
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(adapter, "_run_brain_isolated", lambda **_kwargs: brain_result)

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-real-adapter", "remark_code": "CJREAL01"},
        message_batch={"id": "batch-real-adapter", "messages": [{"content": "想看 SUV"}]},
    )

    assert decision.decision == "send_reply"
    assert decision.reply_text == "这是 Guard 批准的 Brain 原文。"
    assert decision.raw_payload["omniauto_brain_result"]["brain_plan"] == brain_result["brain_plan"]


def test_real_adapter_second_no_visible_attempt_receives_focused_recovery_instruction(
    monkeypatch,
):
    adapter = RealOmniAutoAIEngineAdapter()
    captured: dict = {}
    brain_result = {
        "rule_name": "customer_service_brain_reply",
        "adoptable": True,
        "visible_reply_source": "brain_plan.reply_segments",
        "reply_text": "可以的，您主要是家用还是日常代步？",
        "guard_verdict": "pass",
        "brain_plan": {
            "recommended_action": "send_reply",
            "confidence": 0.9,
            "risk_flags": [],
            "evidence_refs": ["conversation:last_customer_need_text"],
            "reply_segments": ["可以的，您主要是家用还是日常代步？"],
        },
    }

    def fake_run_brain_isolated(**kwargs):
        captured.update(kwargs["invocation"])
        return brain_result

    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "test",
                "model": "test",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(adapter, "_run_brain_isolated", fake_run_brain_isolated)

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-no-visible-retry"},
        message_batch={
            "id": "batch-no-visible-retry",
            "generation_attempt": 2,
            "previous_ai_response_snapshot": {
                "decision": "retry_later",
                "error_code": "AI_ENGINE_NO_VISIBLE_REPLY",
            },
            "messages": [
                {"content": "你好我想买10万的车有什么推荐吗"},
            ],
        },
    )

    instruction = captured["target_state"]["brain_retry_instruction"]
    assert "上一次 Brain 尝试没有形成可发送" in instruction
    assert "没有可依据的 product_master" in instruction
    assert "ask_clarifying_question" in instruction
    assert decision.decision == "send_reply"
    assert decision.reply_text == "可以的，您主要是家用还是日常代步？"


def test_real_adapter_first_attempt_does_not_claim_prior_failure(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    captured: dict = {}
    brain_result = {
        "rule_name": "customer_service_brain_no_visible_reply",
        "adoptable": False,
        "no_visible_reply": {"class": "quality_failed"},
        "brain_plan": {},
    }

    def fake_run_brain_isolated(**kwargs):
        captured.update(kwargs["invocation"])
        return brain_result

    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "test",
                "model": "test",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(adapter, "_run_brain_isolated", fake_run_brain_isolated)

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-first-attempt"},
        message_batch={
            "id": "batch-first-attempt",
            "generation_attempt": 1,
            "previous_ai_response_snapshot": {},
            "messages": [{"content": "10万预算有什么推荐"}],
        },
    )

    assert "brain_retry_instruction" not in captured["target_state"]
    assert decision.decision == "retry_later"
    assert decision.error_code == "AI_ENGINE_NO_VISIBLE_REPLY"


def test_real_adapter_emits_structured_hard_opt_out_without_customer_reply(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    brain_result = {
        "rule_name": "customer_service_brain_hard_opt_out",
        "adoptable": True,
        "guard_verdict": "pass",
        "hard_opt_out": {
            "detected": True,
            "message_event_id": "event-opt-out-1",
            "source_message_key": "source-opt-out-1",
            "customer_text": "请不要再联系我",
            "reason": "explicit_stop_contact",
        },
        "brain_plan": {
            "recommended_action": "hard_opt_out",
            "confidence": 0.99,
            "risk_flags": [],
            "evidence_refs": ["message:event-opt-out-1"],
            "reply_segments": [],
        },
    }
    monkeypatch.setattr(adapter, "_load_config", lambda: {"customer_service_brain": {"provider": "test", "model": "test", "api_key": "test-only"}})
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(adapter, "_run_brain_isolated", lambda **_kwargs: brain_result)

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-opt-out"},
        message_batch={"id": "batch-opt-out", "messages": [{"content": "请不要再联系我"}]},
    )

    assert decision.decision == "hard_opt_out"
    assert decision.reply_text is None
    assert decision.hard_opt_out_evidence == brain_result["hard_opt_out"]


def test_real_adapter_maps_structured_high_intent_to_direct_handoff(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    brain_result = {
        "rule_name": "customer_service_brain_handoff",
        "adoptable": True,
        "reason": "used_car_high_intent_or_risk",
        "brain_plan": {
            "recommended_action": "handoff_for_approval",
            "confidence": 0.96,
            "risk": {
                "risk_level": "high",
                "risk_tags": ["customer_high_intent"],
                "needs_handoff": True,
                "handoff_reason": "used_car_high_intent_or_risk",
            },
            "risk_flags": ["customer_high_intent"],
            "evidence_refs": ["policy:chejin_handoff_high_intent"],
            "reply_segments": ["我帮您确认到店安排。"],
        },
    }
    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "test",
                "model": "test",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(
        adapter,
        "_run_brain_isolated",
        lambda **_kwargs: brain_result,
    )

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-high-intent"},
        message_batch={
            "id": "batch-high-intent",
            "messages": [{"content": "我今天就想去看车"}],
        },
    )

    assert decision.decision == "handoff"
    assert decision.reply_text is None
    assert decision.handoff_reason_code == "CUSTOMER_HIGH_INTENT"
    assert decision.error_code == "CUSTOMER_HIGH_INTENT"
    assert "customer_high_intent" in decision.risk_flags
    assert decision.suggested_action == "handoff"


def test_real_adapter_high_intent_overrides_send_reply(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    brain_result = {
        "rule_name": "customer_service_brain_reply",
        "adoptable": True,
        "reply_text": "可以，我帮您安排。",
        "visible_reply_source": "brain_plan.reply_segments",
        "brain_plan": {
            "recommended_action": "send_reply",
            "confidence": 0.97,
            "risk_flags": ["customer_high_intent"],
            "evidence_refs": ["message:event-high-intent-conflict"],
            "reply_segments": ["可以，我帮您安排。"],
        },
    }
    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "test",
                "model": "test",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(
        adapter,
        "_run_brain_isolated",
        lambda **_kwargs: brain_result,
    )

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-high-intent-conflict"},
        message_batch={
            "id": "batch-high-intent-conflict",
            "messages": [{"content": "我今天就去店里付定金"}],
        },
    )

    assert decision.decision == "handoff"
    assert decision.reply_text is None
    assert decision.handoff_reason_code == "CUSTOMER_HIGH_INTENT"
    assert decision.error_code == "CUSTOMER_HIGH_INTENT"
    assert decision.suggested_action == "handoff"


def test_real_adapter_does_not_label_combined_risk_reason_as_high_intent_without_explicit_marker(
    monkeypatch,
):
    adapter = RealOmniAutoAIEngineAdapter()
    brain_result = {
        "rule_name": "customer_service_brain_handoff",
        "adoptable": True,
        "reason": "used_car_high_intent_or_risk",
        "brain_plan": {
            "recommended_action": "handoff",
            "confidence": 0.9,
            "risk": {
                "risk_tags": ["contract_dispute"],
                "needs_handoff": True,
                "handoff_reason": "used_car_high_intent_or_risk",
            },
            "risk_flags": ["contract_dispute"],
            "evidence_refs": ["policy:contract-risk"],
            "reply_segments": [],
        },
    }
    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "test",
                "model": "test",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(
        adapter,
        "_run_brain_isolated",
        lambda **_kwargs: brain_result,
    )

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-contract-risk"},
        message_batch={
            "id": "batch-contract-risk",
            "messages": [{"content": "合同条款存在争议"}],
        },
    )

    assert decision.decision == "handoff"
    assert decision.reply_text is None
    assert decision.handoff_reason_code == "used_car_high_intent_or_risk"
    assert decision.handoff_reason_code != "CUSTOMER_HIGH_INTENT"
    assert decision.error_code is None


def test_real_adapter_preserves_brain_owned_boundary_as_reply_then_handoff(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    brain_result = {
        "rule_name": "customer_service_brain_handoff",
        "adoptable": True,
        "reason": "formal_policy_requires_manual_confirmation",
        "reply_text": "这个需要结合可核实资料确认，我先为您保留需求。",
        "visible_reply_source": "brain_plan.reply_segments",
        "brain_plan": {
            "recommended_action": "handoff",
            "confidence": 0.92,
            "risk": {
                "risk_level": "medium",
                "risk_tags": ["manual_confirmation"],
                "needs_handoff": True,
                "handoff_reason": "formal_policy_requires_manual_confirmation",
            },
            "reply_segments": ["这个需要结合可核实资料确认，我先为您保留需求。"],
        },
    }
    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {"customer_service_brain": {"provider": "test", "model": "test", "api_key": "test-only"}},
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(adapter, "_run_brain_isolated", lambda **_kwargs: brain_result)

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-boundary-handoff"},
        message_batch={
            "id": "batch-boundary-handoff",
            "messages": [{"content": "这个情况能保证吗"}],
        },
    )

    assert decision.decision == "reply_then_handoff"
    assert decision.reply_text == brain_result["reply_text"]
    assert decision.guard_result == "handoff"
    assert decision.suggested_action == "reply_then_handoff"


def test_real_adapter_provider_exception_is_explicit_retry_later(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(adapter, "_load_config", lambda: {"customer_service_brain": {"provider": "test", "model": "test", "api_key": "test-only"}})

    def fail_brain(**_kwargs):
        raise AppError(
            "AI_ENGINE_PROVIDER_FAILED",
            "OmniAuto Brain 调用失败",
            503,
            {"suggested_action": "retry_later"},
        )

    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(adapter, "_run_brain_isolated", fail_brain)

    with pytest.raises(AppError) as exc:
        _generate_adapter_decision(
            adapter,
            conversation_context={"conversation_id": "conv-provider-fail"},
            message_batch={"id": "batch-provider-fail", "messages": [{"content": "你好"}]},
        )

    assert exc.value.code == "AI_ENGINE_PROVIDER_FAILED"
    assert exc.value.data["suggested_action"] == "retry_later"


def test_real_adapter_provider_hard_timeout_kills_isolated_process(monkeypatch, tmp_path):
    adapter = RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(adapter, "_load_config", lambda: {"customer_service_brain": {"provider": "test", "model": "test", "api_key": "test-only"}})
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    sleeper = tmp_path / "brain_sleeper.py"
    sleeper.write_text(
        "import json, os, time\n"
        "path = os.environ['CHEJIN_AI_PROGRESS_PATH']\n"
        "event = {"
        "'schema_version': 1, "
        "'progress_id': os.environ['CHEJIN_AI_PROGRESS_ID'], "
        "'stage': 'semantic_reviewer', "
        "'route': 'primary', "
        "'event': 'started', "
        "'provider': 'openai', "
        "'model': 'gpt-5.5', "
        "'timeout_seconds': 45, "
        "'call_id': 'timeout-call', "
        "'occurred_at_unix_ms': 1}\n"
        "with open(path, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(event) + '\\n')\n"
        "    handle.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(adapter, "_provider_worker_script", sleeper)
    monkeypatch.setattr(
        "app.services.ai_adapter.get_settings",
        lambda: SimpleNamespace(c3_brain_provider_timeout_seconds=1.0),
    )

    started_at = time.monotonic()
    with pytest.raises(AppError) as exc:
        _generate_adapter_decision(
            adapter,
            conversation_context={"conversation_id": "conv-provider-timeout"},
            message_batch={"id": "batch-provider-timeout", "messages": [{"content": "你好"}]},
        )

    assert time.monotonic() - started_at < 1.5
    assert exc.value.code == "AI_ENGINE_PROVIDER_TIMEOUT"
    assert exc.value.data["suggested_action"] == "retry_later"
    assert exc.value.data["last_provider_progress"]["stage"] == "semantic_reviewer"
    assert exc.value.data["last_provider_progress"]["event"] == "started"
    assert exc.value.data["provider_progress"][0]["call_id"] == "timeout-call"


def test_llm_total_budget_caps_fallback_to_remaining_time(monkeypatch):
    omniauto_root = Path(__file__).resolve().parents[2] / "worker-client" / "omniauto-rpa"
    if str(omniauto_root) not in sys.path:
        sys.path.insert(0, str(omniauto_root))
    llm_config = importlib.import_module(
        "apps.wechat_ai_customer_service.llm_config"
    )
    calls: list[dict] = []

    def fake_request_once_with_wall_timeout(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            time.sleep(0.12)
            return {
                "ok": False,
                "status": 0,
                "error": "llm_wall_timeout_after_0.1s",
                "wall_timeout": True,
            }
        return {
            "ok": True,
            "status": 200,
            "provider": "deepseek",
            "model": "deepseek-chat",
            "response_text": "{}",
        }

    monkeypatch.setattr(
        llm_config,
        "call_llm_request_once_with_wall_timeout",
        fake_request_once_with_wall_timeout,
    )
    # Leave a real but bounded fallback slice.  The previous 60 ms margin was
    # smaller than normal CI scheduler jitter and could expire before the
    # second call even though the production budget logic was correct.
    with llm_config.llm_total_time_budget(0.22):
        result = llm_config.call_llm_request_with_failover(
            provider="openai",
            api_key="test-primary",
            base_url="https://primary.invalid/v1",
            model="gpt-test",
            messages=[{"role": "user", "content": "test"}],
            timeout=0.12,
            wall_timeout=0.12,
            fallback_timeout=0.12,
            fallback_wall_timeout=0.12,
            max_tokens=8,
            config={
                "LLM_FALLBACK_ENABLED": "1",
                "LLM_FALLBACK_PROVIDER": "deepseek",
                "LLM_FALLBACK_BASE_URL": "https://fallback.invalid/v1",
                "LLM_FALLBACK_FLASH_MODEL": "deepseek-chat",
                "LLM_FALLBACK_API_KEY": "test-fallback",
            },
        )

    assert result["ok"] is True
    assert len(calls) == 2
    assert 0.05 <= float(calls[0]["wall_timeout"]) <= 0.12
    assert 0.05 <= float(calls[1]["wall_timeout"]) < 0.11
    assert float(calls[1]["timeout"]) <= float(calls[1]["wall_timeout"])
    assert llm_config.llm_total_time_budget_remaining_seconds() is None


def test_real_adapter_success_preserves_isolated_provider_progress(monkeypatch, tmp_path):
    adapter = RealOmniAutoAIEngineAdapter()
    worker = tmp_path / "brain_success.py"
    worker.write_text(
        "import json, os, sys\n"
        "json.loads(sys.stdin.read())\n"
        "path = os.environ['CHEJIN_AI_PROGRESS_PATH']\n"
        "progress_id = os.environ['CHEJIN_AI_PROGRESS_ID']\n"
        "events = [\n"
        "  {'schema_version': 1, 'progress_id': progress_id, 'stage': 'brain_llm', 'route': 'primary', 'event': 'started', 'provider': 'openai', 'model': 'gpt-5.5', 'timeout_seconds': 150, 'call_id': 'success-call', 'occurred_at_unix_ms': 1, 'api_key': 'must-not-survive', 'prompt': 'must-not-survive'},\n"
        "  {'schema_version': 1, 'progress_id': progress_id, 'stage': 'brain_llm', 'route': 'primary', 'event': 'finished', 'provider': 'openai', 'model': 'gpt-5.5', 'timeout_seconds': 150, 'call_id': 'success-call', 'occurred_at_unix_ms': 2, 'elapsed_ms': 25, 'result_class': 'succeeded', 'status': 200}\n"
        "]\n"
        "with open(path, 'a', encoding='utf-8') as handle:\n"
        "    for event in events:\n"
        "        handle.write(json.dumps(event) + '\\n')\n"
        "    handle.flush()\n"
        "sys.stdout.write(json.dumps({'ok': True, 'result': {'rule_name': 'test'}}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(adapter, "_provider_worker_script", worker)

    result = adapter._run_brain_isolated(
        config={"customer_service_brain": {}},
        invocation={},
        timeout_seconds=2.0,
    )

    assert result["rule_name"] == "test"
    assert [item["event"] for item in result["provider_progress"]] == [
        "started",
        "finished",
    ]
    assert result["provider_progress"][-1]["result_class"] == "succeeded"
    assert "api_key" not in str(result["provider_progress"]).lower()
    assert "must-not-survive" not in str(result["provider_progress"])


def test_real_provider_worker_records_runtime_and_workflow_boundaries(monkeypatch):
    omniauto_root = (
        Path(__file__).resolve().parents[2]
        / "worker-client"
        / "omniauto-rpa"
    )
    monkeypatch.setenv("C3_OMNIAUTO_ROOT", str(omniauto_root))
    # GitHub's Windows runner uses a non-UTF console code page.  The isolated
    # JSON pipe is a UTF-8 protocol and must not inherit that locale for either
    # its Chinese request or Chinese Brain result.
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    adapter = RealOmniAutoAIEngineAdapter()
    config = {
        "customer_service_brain": {
            "enabled": True,
            "mode": "brain_first",
            "provider": "manual_json",
            "brain_plan": {
                "schema_version": 1,
                "recommended_action": "send_reply",
                "reply_segments": ["您好，请问您主要关注什么车型？"],
                "confidence": 0.9,
                "risk_flags": [],
                "evidence_refs": [],
                "facts_claimed": [],
            },
            "quality_verifier_enabled": False,
            "semantic_reviewer_enabled": False,
        }
    }
    invocation = {
        "target_name": "CJPROGRESS",
        "target_state": {},
        "batch": [{"id": "message-1", "content": "你好"}],
        "combined": "你好",
        "decision": {},
        "reply_text": "",
        "intent_assist": {},
        "rag_reply": {},
        "llm_reply": {},
        "product_knowledge": {},
        "data_capture": {},
        "raw_capture": {
            "messages": [{"id": "message-1", "content": "你好"}],
            "conversation": {
                "conversation_id": "conv-progress",
                "chat_type": "private",
            },
        },
        "customer_profile": {},
    }

    result = adapter._run_brain_isolated(
        config=config,
        invocation=invocation,
        timeout_seconds=5.0,
    )

    boundaries = [
        (item["stage"], item["event"])
        for item in result["provider_progress"]
        if item.get("route") == "local"
    ]
    assert boundaries == [
        ("runtime_import", "started"),
        ("runtime_import", "finished"),
        ("brain_workflow", "started"),
        ("brain_workflow", "finished"),
    ]


def test_real_adapter_maps_brain_no_visible_provider_result_to_retry_later(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(adapter, "_load_config", lambda: {"customer_service_brain": {"provider": "test", "model": "test", "api_key": "test-only"}})
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(
        adapter,
        "_run_brain_isolated",
        lambda **_kwargs: {
            "rule_name": "customer_service_brain_no_visible_reply",
            "adoptable": False,
            "visible_reply_source": "none",
            "reply_text": "",
            "reason": "customer_service_brain_llm_unavailable",
            "no_visible_reply": {"class": "llm_unavailable", "retryable": True},
            "brain_plan": {},
            "duration_seconds": 0.21,
        },
    )

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-no-visible"},
        message_batch={"id": "batch-no-visible", "messages": [{"content": "你好"}]},
    )

    assert decision.decision == "retry_later"
    assert decision.error_code == "AI_ENGINE_PROVIDER_FAILED"
    assert decision.suggested_action == "retry_later"
    assert decision.raw_payload["omniauto_brain_result"]["no_visible_reply"]["retryable"] is True


def test_real_adapter_preserves_brain_timeout_as_provider_timeout(monkeypatch):
    adapter = RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "test",
                "model": "test",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(
        adapter,
        "_run_brain_isolated",
        lambda **_kwargs: {
            "rule_name": "customer_service_brain_no_visible_reply",
            "adoptable": False,
            "visible_reply_source": "none",
            "reply_text": "",
            "reason": "customer_service_brain_llm_unavailable",
            "no_visible_reply": {
                "class": "llm_timeout",
                "stage": "brain_llm",
                "reason": "customer_service_brain_llm_unavailable",
                "retryable": True,
            },
            "llm_status": {
                "ok": False,
                "status": 0,
                "error": "llm_wall_timeout_after_12.0s",
            },
            "brain_plan": {},
        },
    )

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-timeout"},
        message_batch={
            "id": "batch-timeout",
            "messages": [{"content": "你好"}],
        },
    )

    assert decision.decision == "retry_later"
    assert decision.error_code == "AI_ENGINE_PROVIDER_TIMEOUT"
    assert decision.suggested_action == "retry_later"


@pytest.mark.parametrize(
    ("recommended_action", "expected_decision", "expected_suggested_action"),
    [
        ("no_action", "no_action", "no_action"),
        ("pause", "pause", "sales_handoff"),
        ("retry_later", "retry_later", "retry_later"),
    ],
)
def test_real_adapter_maps_explicit_non_send_brain_decisions(
    monkeypatch,
    recommended_action,
    expected_decision,
    expected_suggested_action,
):
    adapter = RealOmniAutoAIEngineAdapter()
    brain_result = {
        "rule_name": f"customer_service_brain_{recommended_action}",
        "brain_plan": {
            "recommended_action": recommended_action,
            "confidence": 0.73,
            "risk_flags": ["explicit_non_send"],
            "evidence_refs": ["brain:decision"],
        },
        "reason": "模型明确要求本轮不自动发送",
        "error_code": (
            "AI_ENGINE_PAUSED_FOR_MANUAL_REVIEW"
            if recommended_action == "pause"
            else "AI_ENGINE_RETRY_LATER"
            if recommended_action == "retry_later"
            else None
        ),
    }
    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "test",
                "model": "test",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(
        adapter,
        "_run_brain_isolated",
        lambda **_kwargs: brain_result,
    )

    decision = _generate_adapter_decision(
        adapter,
        conversation_context={"conversation_id": "conv-explicit-decision"},
        message_batch={
            "id": "batch-explicit-decision",
            "messages": [{"content": "请按策略处理"}],
        },
    )

    assert decision.decision == expected_decision
    assert decision.suggested_action == expected_suggested_action
    assert decision.raw_payload["omniauto_brain_result"] == brain_result


@pytest.mark.parametrize(
    ("history_customer_turns", "current_text"),
    [
        (["家用", "SUV"], "10万以内"),
        (["预算8万"], "10万以内"),
        (["我想看SUV"], "不要SUV了，改看轿车"),
        ([], "不要只看SUV，轿车也可以"),
        ([], "不要太费油的SUV"),
    ],
)
def test_message_event_history_reaches_real_provider_input_once(
    monkeypatch,
    history_customer_turns,
    current_text,
):
    """DB -> snapshot -> adapter -> evidence -> Brain -> provider boundary."""

    worker, binding_payload = _setup_bound_conversation()
    base = utcnow() - timedelta(minutes=3)
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, binding_payload["id"])
        for index, content in enumerate(history_customer_turns):
            db.add(
                MessageEvent(
                    id=f"history-event-{index + 1}",
                    conversation_id=binding.conversation_id,
                    binding_id=binding.id,
                    lead_id=binding.lead_id,
                    sales_id=binding.sales_id,
                    worker_id=binding.worker_id,
                    rpa_session_key=binding.rpa_session_key,
                    read_run_id=f"history-read-{index + 1}",
                    contract_version=3,
                    source_message_key=f"history-source-{index + 1}",
                    dedupe_key=f"history-dedupe-{index + 1}",
                    sender_role="customer",
                    message_type="text",
                    content=content,
                    item_state="confirmed",
                    raw_payload={"item_state": "confirmed"},
                    evidence={},
                    occurred_at=base + timedelta(minutes=index),
                    observed_at=base + timedelta(minutes=index),
                    observation_order=index + 1,
                )
            )
        db.commit()

    current_id = _ingest(
        worker,
        binding_payload["conversation_id"],
        "brain-context-current",
        current_text,
    )
    batch_id = _collect(binding_payload["conversation_id"], current_id)[
        "batch_id"
    ]
    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        binding = db.get(WechatSessionBinding, binding_payload["id"])
        conversation = db.get(Conversation, binding.conversation_id)
        context = c3_service._build_ai_context(
            db,
            binding,
            conversation,
            batch,
        )

    assert "history" not in context["conversation"]
    snapshot = context["brain_context_snapshot"]
    assert [item["content"] for item in snapshot["prior_messages"]] == (
        history_customer_turns
    )
    assert snapshot["current_batch_message_ids"] == [current_id]

    omniauto_root = (
        Path(__file__).resolve().parents[2] / "worker-client" / "omniauto-rpa"
    )
    monkeypatch.setenv("C3_OMNIAUTO_ROOT", str(omniauto_root))
    monkeypatch.setattr(
        "app.services.ai_adapter.get_settings",
        lambda: SimpleNamespace(
            c3_omniauto_root=str(omniauto_root),
            c3_brain_provider_timeout_seconds=10.0,
        ),
    )
    adapter = RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "enabled": True,
                "mode": "brain_first",
                "provider": "manual_json",
                "api_key": "test-only-manual-provider",
                "brain_plan": {
                    "schema_version": 1,
                    "recommended_action": "send_reply",
                    "reply_segments": ["您更偏向省油还是空间？"],
                    "confidence": 0.9,
                    "risk_flags": [],
                    "evidence_refs": [],
                    "facts_claimed": [],
                },
                "min_confidence": 0.2,
                "require_evidence": False,
                "include_brain_input_in_audit": True,
                "quality_verifier_enabled": False,
                "semantic_reviewer_enabled": False,
                "fallback_to_legacy_on_error": False,
            },
            "llm_reply_synthesis": {
                "enabled": True,
                "provider": "manual_json",
            },
            "raw_message_store": {"enabled": False},
            "final_visible_llm_polish": {"enabled": False},
        },
    )

    decision = _generate_reply_decision_with_isolated_failure_evidence(
        adapter,
        conversation_context={
            **context["conversation"],
            "brain_context_snapshot": context["brain_context_snapshot"],
        },
        message_batch={
            "id": batch_id,
            "messages": context["messages"],
            "trigger_type": "customer_message",
        },
    )
    brain_input = decision.raw_payload["omniauto_brain_result"]["brain_input"]
    provider_history = brain_input["conversation"]["history_text"]
    provider_current = brain_input["conversation"]["current_batch_text"]
    provider_context = brain_input["conversation"]["context"]
    expected_history = "\n".join(
        f"客户：{text}" for text in history_customer_turns
    )
    semantic_instruction = (
        "结合完整历史判断客户当前需求；以后续明确修改为准；"
        "结合否定词的真实作用范围，不得仅凭关键词删除旧条件。"
    )
    assert provider_history == expected_history
    assert provider_current == f"客户：{current_text}"
    assert current_text not in provider_history
    assert provider_current.count(current_text) == 1
    assert not {
        key for key in provider_context if key.startswith("last_customer_need_")
    }
    assert brain_input["conversation"]["history_authority"] == (
        "chejin_message_events_v1"
    )
    assert brain_input["conversation"]["semantic_instruction"] == (
        semantic_instruction
    )
    brain_module = importlib.import_module(
        "apps.wechat_ai_customer_service.workflows.customer_service_brain"
    )
    normal_prompt = brain_module.build_brain_prompt_pack(
        settings={},
        brain_input=brain_input,
    )
    bridged = adapter._load_context_bridge()(
        brain_context_snapshot=context["brain_context_snapshot"],
        current_batch=context["messages"],
        expected_conversation_id=binding_payload["conversation_id"],
    )
    fast_target_state = {
        "conversation_id": binding_payload["conversation_id"],
        "conversation_context": bridged["conversation_context"],
        "conversation_strategy_state": bridged["conversation_strategy_state"],
        "conversation_interaction_state": bridged[
            "conversation_interaction_state"
        ],
        "chejin_brain_context": bridged,
    }
    fast_evidence = brain_module.build_low_authority_fast_evidence_pack(
        target_name=binding_payload["conversation_id"],
        target_state=fast_target_state,
        batch=context["messages"],
        combined=current_text,
        raw_capture={
            "conversation": {
                "conversation_id": binding_payload["conversation_id"]
            }
        },
        profile={"enabled": True},
    )
    fast_input = brain_module.build_brain_input(
        settings={"prompt_profile": "low_authority_fast"},
        target_name=binding_payload["conversation_id"],
        target_state=fast_target_state,
        batch=context["messages"],
        combined=current_text,
        raw_capture={
            "conversation": {
                "conversation_id": binding_payload["conversation_id"]
            }
        },
        evidence_pack=fast_evidence,
    )
    fast_prompt = brain_module.build_brain_prompt_pack(
        settings={
            "prompt_profile": "low_authority_fast",
            "history_char_budget": 80,
            "current_batch_char_budget": 120,
        },
        brain_input=fast_input,
    )
    for provider_prompt in (normal_prompt, fast_prompt):
        provider_prompt_context = provider_prompt["user"]["brain_input"][
            "conversation"
        ]
        assert provider_prompt_context["history_text"] == expected_history
        assert provider_prompt_context["current_batch_text"] == (
            f"客户：{current_text}"
        )
        assert provider_prompt_context["semantic_instruction"] == (
            semantic_instruction
        )
        assert semantic_instruction in provider_prompt["system"]
        assert brain_module.build_brain_user_content(provider_prompt).count(
            current_text
        ) == 1
    assert brain_input["runtime"]["conversation_interaction_state"][
        "unanswered_exists"
    ] is True


def test_message_event_history_reaches_auto_routine_product_fast_provider(
    monkeypatch,
    request,
    tmp_path,
):
    """DB -> Adapter -> automatic routine profile -> real Provider HTTP body."""

    worker, binding_payload = _setup_bound_conversation()
    base = utcnow() - timedelta(minutes=3)
    history_turns = ["家用通勤为主", "预算10万以内"]
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, binding_payload["id"])
        for index, content in enumerate(history_turns):
            db.add(
                MessageEvent(
                    id=f"routine-fast-history-{index + 1}",
                    conversation_id=binding.conversation_id,
                    binding_id=binding.id,
                    lead_id=binding.lead_id,
                    sales_id=binding.sales_id,
                    worker_id=binding.worker_id,
                    rpa_session_key=binding.rpa_session_key,
                    read_run_id=f"routine-fast-history-read-{index + 1}",
                    contract_version=3,
                    source_message_key=f"routine-fast-history-source-{index + 1}",
                    dedupe_key=f"routine-fast-history-dedupe-{index + 1}",
                    sender_role="customer",
                    message_type="text",
                    content=content,
                    item_state="confirmed",
                    raw_payload={"item_state": "confirmed"},
                    evidence={},
                    occurred_at=base + timedelta(minutes=index),
                    observed_at=base + timedelta(minutes=index),
                    observation_order=index + 1,
                )
            )
        db.commit()

    current_text = "秦PLUS多少钱？"
    current_id = _ingest(
        worker,
        binding_payload["conversation_id"],
        "routine-fast-current",
        current_text,
    )
    batch_id = _collect(binding_payload["conversation_id"], current_id)[
        "batch_id"
    ]
    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        binding = db.get(WechatSessionBinding, binding_payload["id"])
        conversation = db.get(Conversation, binding.conversation_id)
        context = c3_service._build_ai_context(
            db,
            binding,
            conversation,
            batch,
        )

    # Seed the ordinary product source through its production store.  This is
    # what lets the Brain select routine_product_fast itself; the test never
    # injects an evidence pack or prompt_profile.
    omniauto_root = (
        Path(__file__).resolve().parents[2] / "worker-client" / "omniauto-rpa"
    )
    monkeypatch.setenv("C3_OMNIAUTO_ROOT", str(omniauto_root))
    monkeypatch.setenv("WECHAT_STORAGE_BACKEND", "json")
    tenant_id = "routine_fast_" + hashlib.sha256(
        str(tmp_path).encode("utf-8")
    ).hexdigest()[:12]
    monkeypatch.setenv("WECHAT_KNOWLEDGE_TENANT", tenant_id)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "provider-test-key")
    monkeypatch.setattr(
        "app.services.ai_adapter.get_settings",
        lambda: SimpleNamespace(
            c3_omniauto_root=str(omniauto_root),
            c3_brain_provider_timeout_seconds=15.0,
        ),
    )
    adapter = RealOmniAutoAIEngineAdapter()
    adapter._load_brain()
    product_master_module = importlib.import_module(
        "apps.wechat_ai_customer_service.product_master"
    )
    knowledge_paths_module = importlib.import_module(
        "apps.wechat_ai_customer_service.knowledge_paths"
    )
    tenant_root = knowledge_paths_module.tenant_root(tenant_id)
    tenant_runtime_root = knowledge_paths_module.tenant_runtime_root(
        tenant_id
    )
    request.addfinalizer(
        lambda: shutil.rmtree(tenant_root, ignore_errors=True)
    )
    request.addfinalizer(
        lambda: shutil.rmtree(tenant_runtime_root, ignore_errors=True)
    )
    raw_store_module = importlib.import_module(
        "apps.wechat_ai_customer_service.admin_backend.services.raw_message_store"
    )
    product_id = "routine-fast-qinplus"
    product_result = product_master_module.ProductMasterStore(
        tenant_id=tenant_id
    ).save_item(
        {
            "id": product_id,
            "data": {
                "name": "秦PLUS",
                "aliases": ["秦PLUS", "秦plus"],
                "category": "二手车",
                "price": "8.68万",
                "unit": "辆",
                "inventory": 1,
            },
        }
    )
    assert product_result["ok"] is True

    # Put contradictory text in the legacy store.  If the CheJin path touches
    # the old source, that poison will replace/contaminate the Provider input.
    raw_store_module.RawMessageStore(tenant_id=tenant_id).upsert_messages(
        {
            "conversation_id": binding_payload["conversation_id"],
            "conversation_type": "private",
            "target_name": "legacy-poison",
        },
        [
            {
                "id": "legacy-poison-message",
                "sender": "customer",
                "sender_role": "customer",
                "content": "旧RawMessageStore错误历史",
                "type": "text",
            }
        ],
        source_module="test_legacy_poison",
        learning_enabled=False,
        create_batch=False,
    )

    plan = {
        "can_answer": True,
        "understanding": {
            "user_intent": "询问具体车型报价",
            "normalized_entities": [
                {
                    "raw": "秦PLUS",
                    "normalized": "秦PLUS",
                    "entity_type": "product",
                }
            ],
        },
        "answer_mode": "quote_product_fact",
        "reply_strategy": {"style": "concise_human"},
        "evidence_used": {"product_ids": [product_id]},
        "facts_claimed": [
            {
                "fact_type": "price",
                "value": "8.68万",
                "source_level": "product_master",
                "source_id": product_id,
            }
        ],
        "reply_segments": ["秦PLUS这台目前报价8.68万。"],
        "risk": {
            "risk_level": "low",
            "risk_tags": [],
            "needs_handoff": False,
        },
        "recommended_action": "send_reply",
        "confidence": 0.9,
        "reason": "商品主数据命中。",
    }
    provider_requests: list[dict] = []

    class ProviderHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler contract
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            provider_requests.append(json.loads(body.decode("utf-8")))
            response = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    plan,
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ],
                    "usage": {},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    config = {
        "customer_service_brain": {
            "enabled": True,
            "mode": "brain_first",
            "provider": "openai_compatible",
            "model": "provider-test-model",
            "base_url": base_url,
            "api_key": "provider-test-key",
            "min_confidence": 0.2,
            "require_evidence": True,
            "include_brain_input_in_audit": True,
            "include_evidence_pack_in_audit": True,
            "quality_verifier_enabled": False,
            "semantic_reviewer_enabled": False,
            "require_final_visible_polish": False,
            "fallback_to_legacy_on_error": False,
        },
        "llm_reply_synthesis": {
            "enabled": True,
            "provider": "openai_compatible",
        },
        "raw_message_store": {"enabled": False},
        "final_visible_llm_polish": {"enabled": False},
    }
    monkeypatch.setattr(adapter, "_load_config", lambda: config)

    try:
        decision = _generate_reply_decision_with_isolated_failure_evidence(
            adapter,
            conversation_context={
                **context["conversation"],
                "brain_context_snapshot": context[
                    "brain_context_snapshot"
                ],
            },
            message_batch={
                "id": batch_id,
                "messages": context["messages"],
                "trigger_type": "customer_message",
            },
        )

        # The first run above crosses the real Adapter JSON/subprocess and HTTP
        # boundaries.  Repeat the same production Brain runner in-process only
        # to make the forbidden legacy-store call count directly observable;
        # no evidence, profile, prompt, or Brain decision is replaced.
        legacy_store_calls: list[str] = []
        reply_evidence_module = importlib.import_module(
            "apps.wechat_ai_customer_service.workflows.reply_evidence_builder"
        )

        class ForbiddenRawMessageStore:
            def __init__(self, *_args, **_kwargs):
                legacy_store_calls.append("RawMessageStore")
                raise AssertionError(
                    "CheJin routine product Brain must not read RawMessageStore"
                )

        def forbidden_legacy_history(**_kwargs):
            legacy_store_calls.append("assemble_conversation_history")
            raise AssertionError(
                "CheJin routine product Brain must not assemble legacy history"
            )

        monkeypatch.setattr(
            reply_evidence_module,
            "RawMessageStore",
            ForbiddenRawMessageStore,
        )
        monkeypatch.setattr(
            reply_evidence_module,
            "assemble_conversation_history",
            forbidden_legacy_history,
        )
        brain_runner = adapter._load_brain()
        monkeypatch.setattr(
            adapter,
            "_run_brain_isolated",
            lambda *, config, invocation, timeout_seconds: brain_runner(
                config=config,
                **invocation,
            ),
        )
        observable_decision = adapter.generate_reply_decision(
            conversation_context={
                **context["conversation"],
                "brain_context_snapshot": context[
                    "brain_context_snapshot"
                ],
            },
            message_batch={
                "id": batch_id,
                "messages": context["messages"],
                "trigger_type": "customer_message",
            },
        )
        assert legacy_store_calls == []
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)
        shutil.rmtree(tenant_root, ignore_errors=True)
        shutil.rmtree(tenant_runtime_root, ignore_errors=True)

    expected_history = "\n".join(f"客户：{text}" for text in history_turns)
    semantic_instruction = (
        "结合完整历史判断客户当前需求；以后续明确修改为准；"
        "结合否定词的真实作用范围，不得仅凭关键词删除旧条件。"
    )
    assert len(provider_requests) == 2
    for provider_request in provider_requests:
        system_message = provider_request["messages"][0]["content"]
        user_message = provider_request["messages"][1]["content"]
        provider_user = json.loads(user_message.split("\n\n", 1)[0])
        provider_conversation = provider_user["brain_input"]["conversation"]
        assert "常规商品问价/推荐" in system_message
        assert semantic_instruction in system_message
        assert provider_conversation["history_text"] == expected_history
        assert provider_conversation["current_batch_text"] == (
            f"客户：{current_text}"
        )
        assert user_message.count(current_text) == 1
        assert "旧RawMessageStore错误历史" not in user_message
    for result in (decision, observable_decision):
        assert result.raw_payload["omniauto_brain_result"][
            "routine_product_fast_profile"
        ]["enabled"] is True
        assert result.raw_payload["omniauto_brain_result"]["brain_input"][
            "conversation"
        ]["history_authority"] == "chejin_message_events_v1"


def test_invalid_frozen_history_creates_no_provider_reply_or_handoff(monkeypatch):
    worker, binding = _setup_bound_conversation()
    current_id = _ingest(
        worker,
        binding["conversation_id"],
        "brain-context-invalid",
        "想看家用轿车",
    )
    batch_id = _collect(binding["conversation_id"], current_id)["batch_id"]
    _reset_batch_to_generation_state(
        batch_id,
        status="collecting",
        generation_attempt_count=0,
    )
    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        batch.ai_request_snapshot = {
            "brain_context_snapshot": {
                "schema_version": 1,
                "history_authority": "chejin_message_events_v1",
                "conversation_id": binding["conversation_id"],
                "prior_messages": [],
                "current_batch_message_ids": [current_id],
                "history_event_count_before_batch": 0,
                "semantic_history_count_before_batch": 0,
                "prior_messages_sha256": "0" * 64,
                "history_window_complete": True,
            }
        }
        db.commit()

    omniauto_root = (
        Path(__file__).resolve().parents[2] / "worker-client" / "omniauto-rpa"
    )
    app_root = omniauto_root / "apps" / "wechat_ai_customer_service"
    for import_root in reversed(
        [omniauto_root, app_root, app_root / "workflows", app_root / "adapters"]
    ):
        if str(import_root) not in sys.path:
            sys.path.insert(0, str(import_root))
    bridge = importlib.import_module(
        "apps.wechat_ai_customer_service.workflows.chejin_brain_context_bridge"
    ).build_chejin_brain_context
    adapter = RealOmniAutoAIEngineAdapter()
    provider_calls: list[dict] = []
    monkeypatch.setattr(adapter, "_load_context_bridge", lambda: bridge)
    monkeypatch.setattr(
        adapter,
        "_load_brain",
        lambda: pytest.fail("invalid context must fail before Brain runtime"),
    )
    monkeypatch.setattr(
        adapter,
        "_run_brain_isolated",
        lambda **kwargs: provider_calls.append(kwargs),
    )
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: adapter)

    generated = _generate(batch_id)
    assert generated["decision"] == "retry_later"
    assert generated["error_code"] == "AI_CONTEXT_BUILD_FAILED"
    assert generated["batch"]["status"] == "failed"
    assert provider_calls == []
    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        assert batch.message_event_ids == [current_id]
        assert db.get(MessageEvent, current_id) is not None
        assert db.query(ReplyAction).count() == 0
        assert db.query(HandoffEvent).count() == 0


def test_snapshot_construction_failure_is_settled_without_provider_or_handoff(
    monkeypatch,
):
    worker, binding = _setup_bound_conversation()
    current_id = _ingest(
        worker,
        binding["conversation_id"],
        "brain-context-snapshot-build-failure",
        "家用SUV，10万以内",
    )
    batch_id = _collect(binding["conversation_id"], current_id)["batch_id"]
    _reset_batch_to_generation_state(
        batch_id,
        status="collecting",
        generation_attempt_count=0,
    )
    provider_calls: list[dict] = []

    class ForbiddenAdapter:
        def generate_reply_decision(self, **kwargs):
            provider_calls.append(kwargs)
            raise AssertionError("snapshot failure must precede Adapter")

    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: ForbiddenAdapter())
    monkeypatch.setattr(
        c3_service,
        "_build_brain_context_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AppError(
                "AI_CONTEXT_BUILD_FAILED",
                "snapshot normalization failed",
                409,
            )
        ),
    )

    generated = _generate(batch_id)

    assert generated["decision"] == "retry_later"
    assert generated["error_code"] == "AI_CONTEXT_BUILD_FAILED"
    assert generated["batch"]["status"] == "failed"
    assert generated["batch"]["active"] is False
    assert provider_calls == []
    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        assert batch.status == "failed"
        assert batch.active is False
        assert batch.error_code == "AI_CONTEXT_BUILD_FAILED"
        assert batch.generation_started_at is None
        assert batch.ai_response_snapshot["error_code"] == (
            "AI_CONTEXT_BUILD_FAILED"
        )
        assert batch.ai_response_snapshot["raw_payload"]["context_error"][
            "exception_type"
        ] == "AppError"
        assert db.query(ReplyAction).filter_by(batch_id=batch_id).count() == 0
        assert db.query(HandoffEvent).filter_by(batch_id=batch_id).count() == 0


def test_provider_failure_enters_durable_retry_wait(monkeypatch):
    class FailingAdapter:
        def generate_reply_decision(self, **_kwargs):
            raise AppError("AI_ENGINE_PROVIDER_FAILED", "provider failed", 503)

    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: FailingAdapter())
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(worker, binding["conversation_id"], "msg-provider-fail", "请介绍一下")
    generated = _generate(_collect(binding["conversation_id"], message_event_id)["batch_id"])

    assert generated["decision"] == "retry_later"
    assert generated["error_code"] == "AI_ENGINE_PROVIDER_FAILED"
    assert generated["batch"]["status"] == "retry_wait"
    assert generated["batch"]["active"] is True
    assert generated["batch"]["retryable"] is True
    with SessionLocal() as db:
        assert db.query(ReplyAction).count() == 0


def test_brain_no_action_restores_conversation_to_listenable_state(monkeypatch):
    class NoActionAdapter:
        def generate_reply_decision(self, **_kwargs):
            return c3_service.AIEngineDecision(
                decision="no_action",
                guard_result="pass",
                suggested_action="wait_more",
            )

    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: NoActionAdapter())
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-provider-no-action",
        "暂时不用回复",
    )
    generated = _generate(_collect(binding["conversation_id"], message_event_id)["batch_id"])

    assert generated["decision"] == "no_action"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert conversation.status == "waiting_sales_reply"
        assert batch.status == "no_action"
        assert batch.active is False


def test_brain_pause_disables_auto_reply_and_creates_manual_handoff(monkeypatch):
    class PauseAdapter:
        def generate_reply_decision(self, **_kwargs):
            return c3_service.AIEngineDecision(
                decision="pause",
                confidence=0.8,
                guard_result="pause",
                error_code="AI_ENGINE_PAUSED_FOR_MANUAL_REVIEW",
                suggested_action="sales_handoff",
            )

    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-provider-pause",
        "这条需要进一步判断",
    )
    batch_id = _collect(binding["conversation_id"], message_event_id)["batch_id"]
    _reset_batch_to_generation_state(
        batch_id,
        status="collecting",
        generation_attempt_count=0,
    )
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "ai_active"
        conversation.ai_enabled = True
        db.commit()
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: PauseAdapter())
    generated = _generate(batch_id)

    assert generated["decision"] == "pause"
    assert generated["suggested_action"] == "sales_handoff"
    assert generated["handoff_event"]["handoff_reason_code"] == (
        "AI_ENGINE_PAUSED_FOR_MANUAL_REVIEW"
    )
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert conversation.status == "waiting_sales_reply"
        assert conversation.ai_enabled is False
        assert batch.status == "paused"
        assert batch.active is False
        assert batch.decision == "pause"
        assert db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch.id
        ).count() == 1


def test_reply_text_normalization_has_one_hash_for_whitespace_variants():
    canonical = "第一行 第二行 第三行"
    variants = [
        "第一行\n第二行\t第三行",
        "  第一行\u00a0第二行  第三行  ",
        canonical,
    ]

    assert {canonical_reply_text(value) for value in variants} == {canonical}
    assert len({reply_text_hash(value) for value in variants}) == 1


def test_due_brain_retry_is_reclaimed_and_exhaustion_creates_handoff(monkeypatch):
    class FailingAdapter:
        def generate_reply_decision(self, **_kwargs):
            raise AppError("AI_ENGINE_PROVIDER_FAILED", "provider failed", 503)

    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: FailingAdapter())
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(worker, binding["conversation_id"], "msg-provider-retry", "请介绍一下")
    batch = _collect(binding["conversation_id"], message_event_id)
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch["batch_id"])
        row.status = "retry_wait"
        row.active = True
        row.retryable = True
        row.generation_attempt_count = 1
        row.generated_at = utcnow() - timedelta(seconds=60)
        db.commit()

    with SessionLocal() as db:
        claim = c3_service.claim_message_batch_generation(
            db,
            batch_id=batch["batch_id"],
            stale_only=True,
        )
        assert claim["run"] is True
        assert claim["attempt"] == 2
        generated = c3_service.generate_for_batch(
            db,
            batch_id=batch["batch_id"],
            expected_generation_attempt=2,
        )
        db.commit()

    assert generated["decision"] == "handoff"
    assert generated["error_code"] == "AI_ENGINE_RETRY_EXHAUSTED"
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch["batch_id"])
        assert row.status == "handoff_created"
        assert row.retryable is False
        assert db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch["batch_id"]
        ).count() == 1


def test_provider_timeout_progress_is_persisted_in_generation_history(
    monkeypatch,
    tmp_path,
):
    sleeper = tmp_path / "brain_persistence_timeout.py"
    sleeper.write_text(
        "import json, os, sys, time\n"
        "json.loads(sys.stdin.read())\n"
        "path = os.environ['CHEJIN_AI_PROGRESS_PATH']\n"
        "event = {"
        "'schema_version': 1, "
        "'progress_id': os.environ['CHEJIN_AI_PROGRESS_ID'], "
        "'stage': 'brain_llm', "
        "'route': 'primary', "
        "'event': 'started', "
        "'provider': 'openai', "
        "'model': 'gpt-5.5', "
        "'timeout_seconds': 150, "
        "'call_id': 'db-timeout-call', "
        "'occurred_at_unix_ms': 1, "
        "'api_key': 'must-not-survive', "
        "'prompt': 'must-not-survive'}\n"
        "with open(path, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(event) + '\\n')\n"
        "    handle.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    adapter = RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(adapter, "_provider_worker_script", sleeper)
    monkeypatch.setattr(adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(
        adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "openai",
                "model": "gpt-5.5",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(
        "app.services.ai_adapter.get_settings",
        lambda: SimpleNamespace(c3_brain_provider_timeout_seconds=0.1),
    )
    monkeypatch.setattr(
        c3_service,
        "get_ai_engine_adapter",
        lambda: adapter,
    )
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-provider-progress-timeout",
        "请推荐十万元左右的二手车",
    )
    batch_id = _collect(
        binding["conversation_id"],
        message_event_id,
    )["batch_id"]

    generated = _generate(batch_id)

    assert generated["decision"] == "retry_later"
    assert generated["error_code"] == "AI_ENGINE_PROVIDER_TIMEOUT"
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch_id)
        history = row.ai_response_snapshot["generation_attempt_history"]
        provider_error = history[0]["response"]["raw_payload"]["provider_error"]
        assert provider_error["provider_progress"][0]["call_id"] == "db-timeout-call"
        assert provider_error["last_provider_progress"]["stage"] == "brain_llm"
        assert provider_error["last_provider_progress"]["event"] == "started"
        assert "api_key" not in str(provider_error).lower()
        assert "must-not-survive" not in str(provider_error)


def test_brain_retry_and_terminal_handoff_preserve_each_attempt_evidence(
    monkeypatch,
):
    class NoVisibleAdapter:
        def __init__(self):
            self.requests: list[dict] = []

        def generate_reply_decision(self, **kwargs):
            request = dict(kwargs["message_batch"])
            self.requests.append(request)
            attempt = int(request.get("generation_attempt") or 0)
            return c3_service.AIEngineDecision(
                decision="retry_later",
                guard_result="failed",
                error_code="AI_ENGINE_NO_VISIBLE_REPLY",
                suggested_action="retry_later",
                raw_payload={
                    "omniauto_brain_result": {
                        "attempt_marker": attempt,
                        "reason": "brain_quality_verification_failed",
                    }
                },
            )

    adapter = NoVisibleAdapter()
    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: adapter)
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-no-visible-attempt-history",
        "你好我想买10万的车有什么推荐吗",
    )
    batch_id = _collect(binding["conversation_id"], message_event_id)["batch_id"]

    first = _generate(batch_id)
    assert first["decision"] == "retry_later"
    with SessionLocal() as db:
        claim = c3_service.claim_message_batch_generation(
            db,
            batch_id=batch_id,
            force=True,
        )
        assert claim["run"] is True
        assert claim["attempt"] == 2
        second = c3_service.generate_for_batch(
            db,
            batch_id=batch_id,
            expected_generation_attempt=2,
        )
        db.commit()
    assert second["decision"] == "handoff"
    assert second["error_code"] == "AI_ENGINE_RETRY_EXHAUSTED"
    second_request = next(
        request
        for request in reversed(adapter.requests)
        if int(request.get("generation_attempt") or 0) == 2
    )
    previous = second_request["previous_ai_response_snapshot"]
    assert previous["error_code"] == "AI_ENGINE_NO_VISIBLE_REPLY"
    assert previous["generation_attempt_history"][0]["attempt"] == 1

    with SessionLocal() as db:
        row = db.get(MessageBatch, batch_id)
        event = db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch_id,
        ).one()
        history = row.ai_response_snapshot["generation_attempt_history"]
        assert [item["attempt"] for item in history] == [1, 2]
        assert [
            item["response"]["raw_payload"]["omniauto_brain_result"][
                "attempt_marker"
            ]
            for item in history
        ] == [1, 2]
        assert row.ai_response_snapshot["decision"] == "handoff"
        assert event.ai_payload["generation_attempt_history"] == history


def test_separate_ingest_calls_create_new_batch_after_prior_terminal_batch():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-001", "你好")
    m2 = _ingest(worker, binding["conversation_id"], "msg-c3-002", "预算 15 万")

    first = _collect(binding["conversation_id"], m1)
    second = _collect(binding["conversation_id"], m2)

    assert second["batch_id"] != first["batch_id"]
    assert first["batch"]["message_event_ids"] == [m1]
    assert second["batch"]["message_event_ids"] == [m2]


def test_generate_creates_one_reply_action_and_one_chat_reply_task_idempotently():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-003", "我想看看 SUV")
    batch = _collect(binding["conversation_id"], m1)

    first = _generate(batch["batch_id"])
    second = _generate(batch["batch_id"])

    assert first["decision"] == "send_reply"
    assert first["reply_action_id"] == second["reply_action_id"]
    assert first["task_id"] == second["task_id"]
    tasks = client.get("/api/tasks?task_type=chat_reply", headers=HEADERS).json()["data"]["items"]
    assert len(tasks) == 1
    assert tasks[0]["reply_action_id"] == first["reply_action_id"]
    detail = client.get(f"/api/tasks/{first['task_id']}", headers=HEADERS).json()["data"]
    _assert_no_forbidden_fields(detail)
    assert detail["c3"]["message_batch"]["id"] == batch["batch_id"]
    assert detail["c3"]["message_batch"]["status"] == "reply_action_created"
    assert detail["c3"]["reply_action"]["id"] == first["reply_action_id"]
    assert detail["c3"]["reply_action"]["status"] == "queued"
    assert detail["c3"]["sent_ack"] is None
    assert detail["c3"]["handoff_event"] is None


@pytest.mark.parametrize("mutation", ["unlist", "update"])
def test_vehicle_change_supersedes_old_reply_before_worker_claim(monkeypatch, mutation):
    vehicle_id = _create_listed_vehicle()
    monkeypatch.setattr(
        c3_service,
        "get_ai_engine_adapter",
        lambda: _VehicleFactReplyAdapter(vehicle_id),
    )
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        f"msg-vehicle-{mutation}",
        "这款车多少钱？",
    )
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])

    with SessionLocal() as db:
        snapshot = db.get(
            ReplyActionVehicleFact,
            {
                "reply_action_id": generated["reply_action_id"],
                "vehicle_id": vehicle_id,
            },
        )
        assert snapshot is not None
        assert len(snapshot.fact_fingerprint) == 64

    if mutation == "unlist":
        changed = client.post(f"/api/vehicles/{vehicle_id}/unlist", headers=HEADERS)
    else:
        changed = client.put(
            f"/api/vehicles/{vehicle_id}",
            json={"public_price": 11.66, "customer_description": "车辆资料已经更新。"},
            headers=HEADERS,
        )
    assert changed.status_code == 200, changed.text

    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        task = db.get(Task, generated["task_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert action.status == "superseded"
        assert action.current is False
        assert action.error_code == "REPLY_ACTION_VEHICLE_FACT_STALE"
        assert action.send_token is None
        assert task.status == "cancelled"
        assert vehicle_id in str(task.cancel_reason)
        assert batch.status == "superseded"
        assert batch.error_code == "MESSAGE_BATCH_VEHICLE_FACT_STALE"

    rejected_claim = client.post(
        f"/api/tasks/{generated['task_id']}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert rejected_claim.status_code == 409
    assert rejected_claim.json()["code"] == "TASK_CLAIM_NOT_ALLOWED"


@pytest.mark.parametrize("legacy_without_snapshot", [False, True])
def test_claim_send_final_gate_persists_cancellation_for_out_of_band_vehicle_change(
    monkeypatch,
    legacy_without_snapshot,
):
    vehicle_id = _create_listed_vehicle()
    monkeypatch.setattr(
        c3_service,
        "get_ai_engine_adapter",
        lambda: _VehicleFactReplyAdapter(vehicle_id),
    )
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-vehicle-final-send-gate",
        "确认一下这辆车的价格",
    )
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])
    claimed_task = client.post(
        f"/api/tasks/{generated['task_id']}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed_task.status_code == 200, claimed_task.text

    # Simulate a maintenance/import writer that bypassed vehicle_service. The
    # final claim-send comparison must still catch the changed Product Master.
    with SessionLocal() as db:
        vehicle = db.scalar(select(KnowledgeItem).where(KnowledgeItem.item_id == vehicle_id))
        if legacy_without_snapshot:
            db.query(ReplyActionVehicleFact).filter(
                ReplyActionVehicleFact.reply_action_id == generated["reply_action_id"]
            ).delete(synchronize_session=False)
        changed_payload = dict(vehicle.payload)
        changed_data = dict(changed_payload["data"])
        changed_data["price"] = 9.99
        changed_payload["data"] = changed_data
        vehicle.payload = changed_payload
        vehicle.updated_at = utcnow()
        db.commit()

    rejected_send = client.post(
        f"/api/reply-actions/{generated['reply_action_id']}/claim-send",
        json={"task_id": generated["task_id"], "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert rejected_send.status_code == 409
    assert rejected_send.json()["code"] == "REPLY_ACTION_VEHICLE_FACT_STALE"
    assert rejected_send.json()["data"]["vehicle_ids"] == [vehicle_id]
    assert rejected_send.json()["data"]["suggested_action"] == "do_not_send"

    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        task = db.get(Task, generated["task_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert action.status == "superseded"
        assert action.current is False
        assert action.send_token is None
        assert task.status == "cancelled"
        assert batch.status == "superseded"


def test_c2_ingest_to_c3_sent_ack_complete_closure():
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c2-c3-complete-closure",
        "请帮我介绍一款十五万左右的 SUV",
    )
    with SessionLocal() as db:
        batch = db.query(MessageBatch).filter(
            MessageBatch.conversation_id == binding["conversation_id"]
        ).one()
        assert batch.message_event_ids == [message_event_id]
        assert batch.status == "reply_action_created"
        action = db.query(ReplyAction).filter(
            ReplyAction.batch_id == batch.id,
            ReplyAction.current.is_(True),
        ).one()
        task = db.query(Task).filter(Task.reply_action_id == action.id).one()
        batch_id = batch.id
        action_id = action.id
        task_id = task.id

    batch_status = client.get(
        f"/api/workers/{worker['id']}/wechat/message-batches/{batch_id}",
        headers=_worker_headers(worker),
    )
    assert batch_status.status_code == 200
    assert batch_status.json()["data"]["decision"] == "send_reply"
    assert batch_status.json()["data"]["task"]["id"] == task_id

    claimed_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed_task.status_code == 200, claimed_task.text
    send_claim = client.post(
        f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert send_claim.status_code == 200
    send_data = send_claim.json()["data"]
    assert send_data["reply_text"]

    sent_ack = client.post(
        f"/api/reply-actions/{action_id}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": task_id,
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "sent",
            "action_phase": "confirmed",
            "reply_text_hash": send_data["reply_text_hash"],
            "sidecar_run_id": "mac-closure-sidecar",
        },
        headers=_worker_headers(worker),
    )
    assert sent_ack.status_code == 200

    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        action = db.get(ReplyAction, action_id)
        task = db.get(Task, task_id)
        ack = db.query(SentAck).filter(SentAck.reply_action_id == action_id).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert batch.status == "reply_action_created"
        assert action.status == "sent"
        assert task.status == "completed"
        assert ack.send_result == "sent"
        assert conversation.status == "waiting_user_reply"
        assert conversation.reply_count == 1


@pytest.mark.parametrize(
    "error_code",
    [
        "C3_SEND_CONTEXT_GUARD_REQUIRED",
        "C3_SEND_CONTEXT_GUARD_INVALID",
    ],
)
def test_send_context_technical_failure_never_creates_handoff(error_code):
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        f"msg-technical-send-guard-{error_code.lower()}",
        "请推荐一辆十万元左右的二手车",
    )
    generated = _generate(
        _collect(binding["conversation_id"], message_event_id)["batch_id"]
    )
    action_id = generated["reply_action_id"]
    task_id = generated["task_id"]
    claimed_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed_task.status_code == 200, claimed_task.text
    send_claim = client.post(
        f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert send_claim.status_code == 200, send_claim.text
    send_data = send_claim.json()["data"]

    failed_ack = client.post(
        f"/api/reply-actions/{action_id}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": task_id,
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "failed",
            "action_phase": "not_attempted",
            "reply_text_hash": send_data["reply_text_hash"],
            "sidecar_run_id": "sidecar-technical-guard",
            "error_code": error_code,
            "evidence": {
                "physical_send_triggered": False,
                "context_validation": {
                    "ok": False,
                    "error_code": error_code,
                },
            },
        },
        headers=_worker_headers(worker),
    )

    assert failed_ack.status_code == 200, failed_ack.text
    with SessionLocal() as db:
        action = db.get(ReplyAction, action_id)
        task = db.get(Task, task_id)
        conversation = db.get(Conversation, binding["conversation_id"])
        handoffs = db.scalars(
            select(HandoffEvent).where(
                HandoffEvent.conversation_id == binding["conversation_id"]
            )
        ).all()
        assert action.status == "failed"
        assert action.error_code == error_code
        assert task.status == "failed"
        assert task.error_code == error_code
        assert conversation.status != "waiting_sales_reply"
        assert handoffs == []


def test_pause_after_brain_allows_exact_c2_flow_to_claim_send_and_ack():
    worker, binding = _setup_bound_conversation()
    _ingest(
        worker,
        binding["conversation_id"],
        "msg-pause-after-brain",
        "请继续回复我",
    )
    with SessionLocal() as db:
        action = db.query(ReplyAction).filter(
            ReplyAction.conversation_id == binding["conversation_id"],
            ReplyAction.current.is_(True),
        ).one()
        task = db.query(Task).filter(
            Task.reply_action_id == action.id
        ).one()
        action_id = action.id
        task_id = task.id

    flow_id = "read-pause-after-brain"
    worker_headers = _worker_headers(worker)
    with SessionLocal() as db:
        unread_generation = int(
            db.query(WechatSessionBinding)
            .filter(
                WechatSessionBinding.conversation_id
                == binding["conversation_id"]
            )
            .one()
            .unread_generation
            or 0
        )
    started = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/start",
        json={
            "flow_id": flow_id,
            "flow_kind": "c2_read",
            "conversation_id": binding["conversation_id"],
            "unread_generation": unread_generation,
        },
        headers=worker_headers,
    )
    assert started.status_code == 200, started.text
    paused = client.post(
        f"/api/workers/{worker['id']}/run-status",
        json={"run_status": "paused", "client_instance_id": "client-c3"},
        headers=worker_headers,
    )
    assert paused.status_code == 200, paused.text
    continuation_headers = {
        **worker_headers,
        "X-Inflight-Flow-Id": flow_id,
    }
    claimed_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=continuation_headers,
    )
    assert claimed_task.status_code == 200, claimed_task.text
    lease_headers = {
        **continuation_headers,
        "X-Task-Lease-Fencing-Token": str(
            claimed_task.json()["data"]["lease_fencing_token"]
        ),
    }
    send_claim = client.post(
        f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=lease_headers,
    )
    assert send_claim.status_code == 200, send_claim.text
    send_data = send_claim.json()["data"]
    ack = client.post(
        f"/api/reply-actions/{action_id}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": task_id,
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "sent",
            "action_phase": "confirmed",
            "reply_text_hash": send_data["reply_text_hash"],
            "sidecar_run_id": "pause-after-brain-sidecar",
        },
        headers=lease_headers,
    )
    assert ack.status_code == 200, ack.text
    with SessionLocal() as db:
        persisted_binding = db.get(
            WechatSessionBinding, binding["id"]
        )
        persisted_binding.last_read_run_id = flow_id
        persisted_binding.last_read_completed_at = utcnow()
        persisted_binding.last_read_result = "new_facts"
        db.commit()
    finished = client.post(
        f"/api/workers/{worker['id']}/inflight-flow/finish",
        json={
            "flow_id": flow_id,
            "terminal_kind": "read_confirmed",
            "conversation_id": binding["conversation_id"],
            "error_code": None,
        },
        headers=continuation_headers,
    )
    assert finished.status_code == 200, finished.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "waiting_user_reply"
        assert db.get(Task, task_id).status == "completed"


def test_reply_then_handoff_sends_one_brain_boundary_and_keeps_handoff_open(monkeypatch):
    class BoundaryHandoffAdapter:
        def generate_reply_decision(self, **_kwargs):
            return c3_service.AIEngineDecision(
                decision="reply_then_handoff",
                reply_text="这个需要结合可核实资料确认，我先为您保留需求。",
                confidence=0.92,
                handoff_reason_code="FORMAL_POLICY_REQUIRES_MANUAL_CONFIRMATION",
                risk_flags=["manual_confirmation"],
                guard_result="handoff",
                suggested_action="reply_then_handoff",
            )

    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: BoundaryHandoffAdapter())
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-reply-then-handoff",
        "这个情况能保证吗",
    )
    generated = _generate(_collect(binding["conversation_id"], message_event_id)["batch_id"])
    assert generated["decision"] == "reply_then_handoff"
    assert generated["handoff_event"]["handoff_reason_code"] == (
        "FORMAL_POLICY_REQUIRES_MANUAL_CONFIRMATION"
    )

    action_id = generated["reply_action_id"]
    task_id = generated["task_id"]
    batch_status = client.get(
        f"/api/workers/{worker['id']}/wechat/message-batches/{generated['batch']['id']}",
        headers=_worker_headers(worker),
    )
    assert batch_status.status_code == 200, batch_status.text
    assert batch_status.json()["data"]["authorization"]["allowed"] is True

    claimed_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed_task.status_code == 200, claimed_task.text
    send_claim = client.post(
        f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert send_claim.status_code == 200, send_claim.text
    send_data = send_claim.json()["data"]
    assert send_data["reply_text"] == "这个需要结合可核实资料确认，我先为您保留需求。"

    sent_ack = client.post(
        f"/api/reply-actions/{action_id}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": task_id,
            "worker_id": worker["id"],
            "client_instance_id": "client-c3-boundary",
            "send_result": "sent",
            "action_phase": "confirmed",
            "reply_text_hash": send_data["reply_text_hash"],
            "sidecar_run_id": "sidecar-boundary-handoff",
        },
        headers=_worker_headers(worker),
    )
    assert sent_ack.status_code == 200, sent_ack.text

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        action = db.get(ReplyAction, action_id)
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert conversation.status == "waiting_sales_reply"
        assert action.status == "sent"
        assert action.decision == "reply_then_handoff"
        assert batch.decision == "reply_then_handoff"
        assert db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch.id,
            HandoffEvent.closed_at.is_(None),
        ).count() == 1
        assert db.query(ReplyAction).filter(ReplyAction.batch_id == batch.id).count() == 1


def test_recall_counts_only_after_confirmed_send_and_ack_is_idempotent():
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-recall-confirmed-send",
        "召回前确认没有新消息",
    )
    collected = _collect(binding["conversation_id"], message_event_id)
    batch_id = collected["batch_id"]
    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        conversation = db.get(Conversation, binding["conversation_id"])
        batch.trigger_type = "recall"
        batch.recall_cycle_id = "recall-confirmed-cycle"
        batch.origin_conversation_status = "waiting_user_reply"
        conversation.status = "recall_precheck"
        conversation.recall_cycle_id = "recall-confirmed-cycle"
        conversation.recall_origin_status = "waiting_user_reply"
        conversation.recall_count = 1
        conversation.recall_daily_count = 0
        db.commit()
    generated = _generate(batch_id)
    action_id = generated["reply_action_id"]
    task_id = generated["task_id"]

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.recall_count == 1
        assert conversation.recall_daily_count == 0

    claimed_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed_task.status_code == 200, claimed_task.text
    send_claim = client.post(
        f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert send_claim.status_code == 200
    send_data = send_claim.json()["data"]
    ack_payload = {
        "send_token": send_data["send_token"],
        "task_id": task_id,
        "worker_id": worker["id"],
        "client_instance_id": "client-c3",
        "send_result": "sent",
        "action_phase": "confirmed",
        "reply_text_hash": send_data["reply_text_hash"],
        "sidecar_run_id": "recall-confirmed-sidecar",
    }
    sent_ack = client.post(
        f"/api/reply-actions/{action_id}/sent-ack",
        json=ack_payload,
        headers=_worker_headers(worker),
    )
    assert sent_ack.status_code == 200
    repeated_ack = client.post(
        f"/api/reply-actions/{action_id}/sent-ack",
        json=ack_payload,
        headers=_worker_headers(worker),
    )
    assert repeated_ack.status_code == 200

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "recalled_waiting_user"
        assert conversation.recall_count == 2
        assert conversation.recall_daily_count == 1
        assert conversation.next_recall_at is not None
        comparison_now = (
            utcnow()
            if conversation.next_recall_at.tzinfo is not None
            else utcnow().replace(tzinfo=None)
        )
        assert conversation.next_recall_at > comparison_now


def test_sent_ack_hash_conflict_becomes_triggered_unknown_not_false_failure():
    worker, binding = _setup_bound_conversation()
    message_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-hash-conflict",
        "请回复这条消息",
    )
    generated = _generate(
        _collect(binding["conversation_id"], message_event_id)["batch_id"]
    )
    task_id = generated["task_id"]
    action_id = generated["reply_action_id"]
    claimed_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed_task.status_code == 200
    send_claim = client.post(
        f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert send_claim.status_code == 200
    send_data = send_claim.json()["data"]

    response = client.post(
        f"/api/reply-actions/{action_id}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": task_id,
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "sent",
            "action_phase": "confirmed",
            "reply_text_hash": "0" * 64,
            "sidecar_run_id": "sidecar-hash-conflict",
        },
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ack"]["send_result"] == "unknown"
    assert data["ack"]["action_phase"] == "trigger_attempted"
    assert data["ack"]["error_code"] == "SEND_TEXT_HASH_MISMATCH"
    assert data["reply_action"]["status"] == "unknown_send_result"
    assert data["task"]["status"] == "failed"


def test_pending_chat_reply_remains_available_for_c2_context_recovery():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(worker, binding["conversation_id"], "msg-c3-pull-guard", "请回复我")
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])
    with SessionLocal() as db:
        db.query(Task).filter(
            Task.worker_id == worker["id"],
            Task.task_type != "chat_reply",
            Task.status == "pending",
        ).update({"status": "cancelled"})
        db.commit()

    pulled = client.get(
        f"/api/workers/{worker['id']}/tasks/pull",
        headers=_worker_headers(worker),
    )

    assert pulled.status_code == 200
    pulled_task = pulled.json()["data"]["task"]
    assert pulled.json()["data"]["mode"] == "pending"
    assert pulled_task["task_type"] == "chat_reply"
    assert pulled_task["c3"]["message_batch"]["id"] == generated["batch"]["id"]
    task = client.get(f"/api/tasks/{generated['task_id']}", headers=HEADERS).json()["data"]
    assert task["status"] == "pending"


def test_pending_chat_reply_is_pulled_before_older_add_friend_task():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-reply-priority",
        "请优先回复当前会话",
    )
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])
    with SessionLocal() as db:
        older_add_friend = (
            db.query(Task)
            .filter(
                Task.worker_id == worker["id"],
                Task.task_type == "add_friend",
                Task.status == "pending",
            )
            .order_by(Task.created_at.asc())
            .first()
        )
        assert older_add_friend is not None
        assert older_add_friend.created_at <= db.get(Task, generated["task_id"]).created_at

    pulled = client.get(
        f"/api/workers/{worker['id']}/tasks/pull",
        headers=_worker_headers(worker),
    )

    assert pulled.status_code == 200
    assert pulled.json()["data"]["task"]["id"] == generated["task_id"]
    assert pulled.json()["data"]["task"]["task_type"] == "chat_reply"


def test_worker_can_terminally_close_unclaimable_pending_chat_reply():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(worker, binding["conversation_id"], "msg-c3-pending-close", "请回复我")
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])

    failed = client.post(
        f"/api/tasks/{generated['task_id']}/fail",
        json={
            "error_code": "C2_REPLY_TARGET_NOT_AUTHORIZED",
            "failure_step": "c2_reply_recovery",
            "failure_remark": "目标授权已撤销",
        },
        headers=_worker_headers(worker),
    )

    assert failed.status_code == 200
    assert failed.json()["data"]["status"] == "failed"
    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert action.status == "cancelled"
        assert action.current is False
        assert batch.status == "handoff_created"
        assert batch.active is False
        assert db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch.id,
            HandoffEvent.handoff_reason_code == "C2_REPLY_TARGET_NOT_AUTHORIZED",
        ).count() == 1


def test_pending_chat_reply_cannot_be_closed_with_arbitrary_worker_error():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(worker, binding["conversation_id"], "msg-c3-pending-denied", "请回复我")
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])

    failed = client.post(
        f"/api/tasks/{generated['task_id']}/fail",
        json={
            "error_code": "OTHER",
            "failure_step": "c2_reply_recovery",
            "failure_remark": "任意失败不允许跳过领取",
        },
        headers=_worker_headers(worker),
    )

    assert failed.status_code == 409
    assert failed.json()["code"] == "TASK_FAIL_NOT_ALLOWED"


def test_pending_reply_context_recovery_failure_closes_task_and_hands_off():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-recovery-handoff",
        "客户端重启后无法安全恢复会话",
    )
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])

    failed = client.post(
        f"/api/tasks/{generated['task_id']}/fail",
        json={
            "error_code": "C2_REPLY_CONTEXT_RECOVERY_FAILED",
            "failure_step": "pre_send_refresh",
            "failure_remark": "无法确认原微信会话",
        },
        headers=_worker_headers(worker),
    )

    assert failed.status_code == 200
    assert failed.json()["data"]["status"] == "failed"
    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch.id,
            HandoffEvent.handoff_reason_code == "C2_REPLY_CONTEXT_RECOVERY_FAILED",
        ).one()
        assert action.status == "cancelled"
        assert batch.status == "handoff_created"
        assert conversation.status == "waiting_sales_reply"
        assert handoff.status == "created"


@pytest.mark.parametrize(
    "error_code",
    [
        "C2_PRE_SEND_TEXT_CONTENT_UNREADABLE",
        "C2_PRE_SEND_MESSAGE_SEQUENCE_ALIGNMENT_FAILED",
        "C2_PRE_SEND_MESSAGE_ROLE_UNCONFIRMED",
        "C2_PRE_SEND_VOICE_TARGET_NOT_FOUND",
        "C2_PRE_SEND_VOICE_TARGET_AMBIGUOUS",
        "C2_PRE_SEND_IMAGE_TARGET_NOT_FOUND",
        "C2_PRE_SEND_IMAGE_TARGET_AMBIGUOUS",
        "C2_PRE_SEND_MESSAGE_VIEWPORT_CHANGED_AGAIN",
        "C2_PRE_SEND_SYSTEM_CONTENT_UNREADABLE",
        "C2_PRE_SEND_SYSTEM_CLASSIFICATION_UNRESOLVED",
    ],
)
def test_exact_pre_send_reidentification_error_hands_off_once(error_code):
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        f"msg-{error_code.lower()}",
        "发送前消息需要重新识别",
    )
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])

    failed = client.post(
        f"/api/tasks/{generated['task_id']}/fail",
        json={
            "error_code": error_code,
            "failure_step": "pre_send_refresh",
            "failure_remark": "完整重识别一次后仍无法确认具体消息",
        },
        headers=_worker_headers(worker),
    )

    assert failed.status_code == 200
    assert failed.json()["data"]["error_code"] == error_code
    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        handoffs = db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch.id,
            HandoffEvent.handoff_reason_code == error_code,
        ).all()
        assert action.status == "cancelled"
        assert batch.status == "handoff_created"
        assert len(handoffs) == 1
        task = db.get(Task, generated["task_id"])
        assert task is not None
        assert task.lease_owner_worker_id is None
        assert task.lease_owner_client_instance_id is None
        assert task.worker is not None
        assert task.worker.running_status == "idle"
        assert task.worker.current_task is None


def test_pre_send_layout_invalid_cancels_reply_without_customer_handoff():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-pre-send-layout-invalid",
        "布局不可用时禁止猜测客户",
    )
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])

    failed = client.post(
        f"/api/tasks/{generated['task_id']}/fail",
        json={
            "error_code": "C2_PRE_SEND_LAYOUT_INVALID",
            "failure_step": "pre_send_refresh",
            "failure_remark": "无法建立合法消息视口",
        },
        headers=_worker_headers(worker),
    )

    assert failed.status_code == 200
    assert failed.json()["data"]["error_code"] == "C2_PRE_SEND_LAYOUT_INVALID"
    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert action.status == "cancelled"
        assert batch.status == "cancelled"
        assert db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch.id
        ).count() == 0


def test_pre_send_checkpoint_invalid_cancels_reply_without_customer_handoff():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-pre-send-checkpoint-invalid",
        "checkpoint 无效时必须技术失败",
    )
    generated = _generate(
        _collect(binding["conversation_id"], message_id)["batch_id"]
    )

    failed = client.post(
        f"/api/tasks/{generated['task_id']}/fail",
        json={
            "error_code": "C2_PRE_SEND_FACT_CHECKPOINT_INVALID",
            "failure_step": "pre_send_checkpoint",
            "failure_remark": "checkpoint 缺失或绑定摘要矛盾",
        },
        headers=_worker_headers(worker),
    )

    assert failed.status_code == 200, failed.text
    assert (
        failed.json()["data"]["error_code"]
        == "C2_PRE_SEND_FACT_CHECKPOINT_INVALID"
    )
    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert action.status == "cancelled"
        assert batch.status == "cancelled"
        assert (
            db.query(HandoffEvent)
            .filter(HandoffEvent.batch_id == batch.id)
            .count()
            == 0
        )


def test_running_chat_reply_failure_cancels_unsent_action_and_batch():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-running-close",
        "停止后不要再发送",
    )
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])
    claimed = client.post(
        f"/api/tasks/{generated['task_id']}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed.status_code == 200

    failed = client.post(
        f"/api/tasks/{generated['task_id']}/fail",
        json={
            "error_code": "CONVERSATION_NOT_ELIGIBLE",
            "failure_step": "claim_send",
            "failure_remark": "监听授权已撤销",
        },
        headers=_task_lease_headers(worker, claimed),
    )

    assert failed.status_code == 200
    assert failed.json()["data"]["status"] == "failed"
    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert action.status == "cancelled"
        assert action.current is False
        assert batch.status == "cancelled"
        assert batch.active is False


def test_customer_messages_keep_worker_authoritative_batch_order():
    worker, binding = _setup_bound_conversation()
    first_id = _ingest(worker, binding["conversation_id"], "msg-order-first", "第一条")
    second_id = _ingest(worker, binding["conversation_id"], "msg-order-second", "第二条")
    with SessionLocal() as db:
        batch = MessageBatch(
            conversation_id=binding["conversation_id"],
            status="collecting",
            active=False,
            message_event_ids=[second_id, first_id],
            message_count=2,
            generation_no=1,
        )
        db.add(batch)
        db.flush()

        messages = c3_service._customer_messages(db, batch)

        assert [item.id for item in messages] == [second_id, first_id]
        assert [item.content for item in messages] == ["第二条", "第一条"]


def test_brain_history_uses_observed_order_when_old_fact_arrives_late():
    worker, binding = _setup_bound_conversation()
    newer_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-history-newer",
        "画面里较新的消息",
    )
    delayed_older_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-history-delayed-older",
        "网络延迟送达的旧消息",
    )
    with SessionLocal() as db:
        newer = db.get(MessageEvent, newer_id)
        delayed_older = db.get(MessageEvent, delayed_older_id)
        delayed_older.observed_at = utcnow() - timedelta(minutes=2)
        newer.observed_at = utcnow() - timedelta(minutes=1)
        delayed_older.observation_order = 1
        newer.observation_order = 1
        batch = MessageBatch(
            conversation_id=binding["conversation_id"],
            status="collecting",
            active=False,
            message_event_ids=[],
            message_count=0,
            generation_no=99,
        )
        db.add(batch)
        db.flush()
        binding_row = db.query(WechatSessionBinding).filter(
            WechatSessionBinding.conversation_id == binding["conversation_id"]
        ).one()
        conversation = db.get(Conversation, binding["conversation_id"])

        context = c3_service._build_ai_context(
            db,
            binding_row,
            conversation,
            batch,
        )

        assert [item["content"] for item in context[
            "brain_context_snapshot"
        ]["prior_messages"]] == [
            "网络延迟送达的旧消息",
            "画面里较新的消息",
        ]


def test_brain_history_preserves_structured_image_context_across_rounds():
    worker, binding = _setup_bound_conversation()
    image_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-history-image",
        "[图片]",
    )
    with SessionLocal() as db:
        image = db.get(MessageEvent, image_id)
        image.message_type = "image"
        image.content = None
        image.item_state = "completed"
        image.raw_payload = {
            **dict(image.raw_payload or {}),
            "item_state": "completed",
            "customer_image_understanding": {
                "schema_version": 1,
                "vision_summary": "白色 SUV 外观，车头朝左",
                "image_ocr_text": ["测试车牌"],
                "classification": {
                    "is_vehicle": True,
                    "vehicle_confidence": 0.91,
                    "unknown": False,
                },
                "entities": {
                    "brand_candidates": ["测试品牌"],
                    "series_candidates": [],
                },
                "bridge": {
                    "normalized_vehicle_query": "白色 SUV",
                },
            },
            "visual_bridge_input": {
                "schema_version": 1,
                "present": True,
                "vision_summary": "白色 SUV 外观，车头朝左",
                "catalog_assist": {
                    "normalized_vehicle_query": "白色 SUV",
                },
            },
            "server_validated_product_id": "server-product-001",
        }
        batch = MessageBatch(
            conversation_id=binding["conversation_id"],
            status="collecting",
            active=False,
            message_event_ids=[],
            message_count=0,
            generation_no=100,
        )
        db.add(batch)
        db.flush()
        binding_row = db.query(WechatSessionBinding).filter(
            WechatSessionBinding.conversation_id
            == binding["conversation_id"]
        ).one()
        conversation = db.get(
            Conversation,
            binding["conversation_id"],
        )

        context = c3_service._build_ai_context(
            db,
            binding_row,
            conversation,
            batch,
        )

    history_image = next(
        item
        for item in context["brain_context_snapshot"][
            "prior_messages"
        ]
        if item["message_event_id"] == image_id
    )
    assert context["messages"] == []
    assert history_image["message_type"] == "image"
    assert history_image["vision_summary"] == "白色 SUV 外观，车头朝左"
    assert history_image["image_ocr_text"] == ["测试车牌"]
    assert history_image["normalized_vehicle_query"] == "白色 SUV"
    assert history_image["server_validated_product_id"] == "server-product-001"


def test_same_batch_retry_reuses_frozen_brain_context_snapshot():
    worker, binding = _setup_bound_conversation()
    _ingest(
        worker,
        binding["conversation_id"],
        "msg-frozen-history-before",
        "家用轿车",
    )
    current_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-frozen-current",
        "10万以内",
    )
    batch_id = _collect(binding["conversation_id"], current_id)["batch_id"]

    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        first_context = c3_service._build_ai_context(
            db,
            binding_row,
            conversation,
            batch,
        )
        batch.ai_request_snapshot = first_context
        frozen = first_context["brain_context_snapshot"]
        db.commit()

    # A later fact may supersede this batch in normal production, but it must
    # never rewrite the immutable history used by a forced recovery of this
    # exact batch.
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        tick = utcnow() + timedelta(seconds=1)
        db.add(
            MessageEvent(
                id="history-arrived-after-freeze",
                conversation_id=binding_row.conversation_id,
                binding_id=binding_row.id,
                lead_id=binding_row.lead_id,
                sales_id=binding_row.sales_id,
                worker_id=binding_row.worker_id,
                rpa_session_key=binding_row.rpa_session_key,
                read_run_id="history-after-freeze-read",
                contract_version=3,
                source_message_key="history-after-freeze-source",
                dedupe_key="history-after-freeze-dedupe",
                sender_role="customer",
                message_type="text",
                content="后来才到的新消息",
                item_state="confirmed",
                raw_payload={"item_state": "confirmed"},
                evidence={},
                occurred_at=tick,
                observed_at=tick,
                observation_order=99,
            )
        )
        db.commit()

    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        retry_context = c3_service._build_ai_context(
            db,
            binding_row,
            conversation,
            batch,
        )

    assert retry_context["brain_context_snapshot"] == frozen
    assert "后来才到的新消息" not in str(frozen)


@pytest.mark.parametrize(
    "trigger_type",
    ["customer_message", "recall", "c2_handoff_recovery"],
)
def test_all_message_triggers_freeze_the_same_authoritative_history(
    trigger_type,
):
    worker, binding = _setup_bound_conversation()
    history_id = _ingest(
        worker,
        binding["conversation_id"],
        f"history-{trigger_type}",
        "前一轮家用轿车",
    )
    current_id = _ingest(
        worker,
        binding["conversation_id"],
        f"current-{trigger_type}",
        "这一轮10万以内",
    )
    with SessionLocal() as db:
        batch = MessageBatch(
            conversation_id=binding["conversation_id"],
            status="collecting",
            active=False,
            trigger_type=trigger_type,
            trigger_key=f"trigger-{trigger_type}",
            trigger_message_event_id=current_id,
            message_event_ids=[current_id],
            message_count=1,
            generation_no=99,
        )
        db.add(batch)
        db.flush()
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        context = c3_service._build_ai_context(
            db,
            binding_row,
            conversation,
            batch,
        )

    snapshot = context["brain_context_snapshot"]
    assert snapshot["history_authority"] == "chejin_message_events_v1"
    assert snapshot["current_batch_message_ids"] == [current_id]
    assert [item["message_event_id"] for item in snapshot["prior_messages"]] == [
        history_id
    ]
    assert [item["id"] for item in context["messages"]] == [current_id]


def test_stale_message_batch_generation_is_reclaimed_once():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(worker, binding["conversation_id"], "msg-c3-stale-recovery", "恢复测试")
    batch = _collect(binding["conversation_id"], message_id)
    _reset_batch_to_generation_state(
        batch["batch_id"],
        status="generating",
        generation_attempt_count=1,
        generation_started_at=utcnow() - timedelta(seconds=600),
    )

    with SessionLocal() as db:
        claim = c3_service.claim_message_batch_generation(
            db,
            batch_id=batch["batch_id"],
            stale_only=True,
        )
        db.commit()

    assert claim["run"] is True
    assert claim["recovery"] is True
    assert claim["attempt"] == 2
    with SessionLocal() as db:
        duplicate_claim = c3_service.claim_message_batch_generation(
            db,
            batch_id=batch["batch_id"],
            stale_only=True,
        )
    assert duplicate_claim["run"] is False


def test_durable_recovery_loop_finishes_stale_batch_without_worker_poll():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-autonomous-recovery",
        "后端重启后继续生成回复",
    )
    batch = _collect(binding["conversation_id"], message_id)
    _reset_batch_to_generation_state(
        batch["batch_id"],
        status="generating",
        generation_attempt_count=1,
        generation_started_at=utcnow() - timedelta(seconds=600),
    )

    result = recover_due_message_batches_once()

    assert result == {"examined": 1, "claimed": 1, "generated": 1, "failed": 0}
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch["batch_id"])
        action = db.query(ReplyAction).filter(
            ReplyAction.batch_id == batch["batch_id"],
            ReplyAction.current.is_(True),
        ).one()
        assert row.status == "reply_action_created"
        assert row.active is False
        assert action.status == "queued"


def test_durable_recovery_loop_starts_committed_collecting_batch_after_crash():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-collecting-crash",
        "入库成功后后端立刻重启",
    )
    batch = _collect(binding["conversation_id"], message_id)
    _reset_batch_to_generation_state(
        batch["batch_id"],
        status="collecting",
        generation_attempt_count=0,
    )

    result = recover_due_message_batches_once()

    assert result == {"examined": 1, "claimed": 1, "generated": 1, "failed": 0}
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch["batch_id"])
        assert row.status == "reply_action_created"
        assert row.generation_attempt_count == 1
        assert db.query(ReplyAction).filter(
            ReplyAction.batch_id == batch["batch_id"]
        ).count() == 1


def test_stale_message_batch_recovery_exhaustion_becomes_terminal():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(worker, binding["conversation_id"], "msg-c3-stale-terminal", "恢复失败测试")
    batch = _collect(binding["conversation_id"], message_id)
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch["batch_id"])
        row.status = "generating"
        row.active = True
        row.generation_attempt_count = 2
        row.generation_started_at = utcnow() - timedelta(seconds=600)
        db.commit()

    with SessionLocal() as db:
        claim = c3_service.claim_message_batch_generation(
            db,
            batch_id=batch["batch_id"],
            stale_only=True,
        )
        db.commit()

    assert claim["run"] is False
    assert claim["terminal"] is True
    assert claim["error_code"] == "AI_ENGINE_RETRY_EXHAUSTED"
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch["batch_id"])
        assert row.status == "handoff_created"
        assert row.active is False
        assert db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == batch["batch_id"],
            HandoffEvent.handoff_reason_code == "AI_ENGINE_RETRY_EXHAUSTED",
        ).count() == 1


def test_c3_does_not_expose_duplicate_worker_task_claim_endpoint():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-003-legacy-claim", "我想看看 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    legacy = client.post(
        f"/api/worker/tasks/{generated['task_id']}/claim",
        json={"worker_id": worker["id"], "current_step": "chat_reply_claimed"},
        headers=_worker_headers(worker),
    )

    assert legacy.status_code == 404


def test_new_customer_message_supersedes_old_reply_action_before_send():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-004", "我想看轿车")
    old = _generate(_collect(binding["conversation_id"], m1)["batch_id"])
    m2 = _ingest(worker, binding["conversation_id"], "msg-c3-005", "再补充一下，要 SUV")

    new_batch = _collect(binding["conversation_id"], m2)
    new_action = _generate(new_batch["batch_id"])

    old_task = client.get(f"/api/tasks/{old['task_id']}", headers=HEADERS).json()["data"]
    assert old_task["status"] == "cancelled"
    assert old_task["reply_action_id"] == old["reply_action_id"]
    assert "events" in old_task
    assert "status_flow" not in old_task
    assert new_action["reply_action_id"] != old["reply_action_id"]


def test_customer_message_ingest_supersedes_unsent_reply_action_even_before_collect():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-004-a", "我想看轿车")
    old = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    _ingest(worker, binding["conversation_id"], "msg-c3-004-b", "再补一句，要白色")

    old_task = client.get(f"/api/tasks/{old['task_id']}", headers=HEADERS).json()["data"]
    assert old_task["status"] == "cancelled"
    with SessionLocal() as db:
        old_action = db.get(ReplyAction, old["reply_action_id"])
        assert old_action.status == "superseded"


def test_generating_batch_is_superseded_when_new_customer_message_arrives():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-generating-001", "我想看轿车")
    old_batch = _collect(binding["conversation_id"], m1)
    with SessionLocal() as db:
        batch = db.get(MessageBatch, old_batch["batch_id"])
        batch.status = "generating"
        batch.active = True
        db.commit()

    m2 = _ingest(worker, binding["conversation_id"], "msg-c3-generating-002", "补充一下，要 SUV")
    new_batch = _collect(binding["conversation_id"], m2)

    assert new_batch["batch_id"] != old_batch["batch_id"]
    with SessionLocal() as db:
        old = db.get(MessageBatch, old_batch["batch_id"])
        new = db.get(MessageBatch, new_batch["batch_id"])
        assert old.status == "superseded"
        assert old.active is False
        assert old.error_code == "MESSAGE_BATCH_SUPERSEDED"
        assert new.active is False
        assert new.status == "reply_action_created"


def test_brain_provider_does_not_hold_batch_lock_and_stale_result_is_discarded(
    monkeypatch,
):
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-provider-unlocked",
        "模型思考期间又有新消息",
    )
    batch = _collect(binding["conversation_id"], message_id)
    _reset_batch_to_generation_state(
        batch["batch_id"],
        status="collecting",
        generation_attempt_count=0,
    )

    class SupersedingAdapter:
        def generate_reply_decision(self, **_kwargs):
            with SessionLocal() as other_db:
                row = other_db.get(MessageBatch, batch["batch_id"])
                row.status = "superseded"
                row.active = False
                row.error_code = "MESSAGE_BATCH_SUPERSEDED"
                other_db.commit()
            return c3_service.AIEngineDecision(
                decision="send_reply",
                reply_text="这条旧回复不能发送",
                guard_result="pass",
                raw_payload={
                    "omniauto_brain_result": {
                        "provider_progress": [
                            {
                                "schema_version": 1,
                                "progress_id": "stale-success-progress",
                                "stage": "semantic_reviewer",
                                "route": "primary",
                                "event": "started",
                            }
                        ],
                        "last_provider_progress": {
                            "stage": "semantic_reviewer",
                            "event": "started",
                        },
                    }
                },
            )

    monkeypatch.setattr(
        c3_service,
        "get_ai_engine_adapter",
        lambda: SupersedingAdapter(),
    )
    with SessionLocal() as db:
        result = c3_service.generate_for_batch(db, batch_id=batch["batch_id"])
        db.commit()

    assert result["error_code"] == "MESSAGE_BATCH_GENERATION_CLAIM_STALE"
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch["batch_id"])
        assert row.status == "superseded"
        history = row.ai_response_snapshot["generation_attempt_history"]
        stale_attempts = [item for item in history if item["attempt"] == 0]
        assert len(stale_attempts) == 1
        assert stale_attempts[0]["response"]["decision"] == "discarded_stale"
        assert stale_attempts[0]["response"]["reply_text"] is None
        assert "这条旧回复不能发送" not in str(stale_attempts[0])
        assert stale_attempts[0]["response"]["raw_payload"][
            "omniauto_brain_result"
        ]["last_provider_progress"]["stage"] == "semantic_reviewer"
        assert db.query(ReplyAction).filter(
            ReplyAction.batch_id == batch["batch_id"]
        ).count() == 0


def test_hard_timeout_and_concurrent_supersede_persist_progress_without_reply(
    monkeypatch,
    tmp_path,
):
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-timeout-concurrent-supersede",
        "模型审核期间又有新消息",
    )
    batch = _collect(binding["conversation_id"], message_id)
    _reset_batch_to_generation_state(
        batch["batch_id"],
        status="collecting",
        generation_attempt_count=2,
    )

    sleeper = tmp_path / "semantic_reviewer_timeout.py"
    sleeper.write_text(
        "import json, os, sys, time\n"
        "json.loads(sys.stdin.read())\n"
        "path = os.environ['CHEJIN_AI_PROGRESS_PATH']\n"
        "event = {"
        "'schema_version': 1, "
        "'progress_id': os.environ['CHEJIN_AI_PROGRESS_ID'], "
        "'stage': 'semantic_reviewer', "
        "'route': 'primary', "
        "'event': 'started', "
        "'provider': 'openai', "
        "'model': 'gpt-5.5', "
        "'timeout_seconds': 45, "
        "'call_id': 'stale-timeout-call', "
        "'occurred_at_unix_ms': 1}\n"
        "with open(path, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(event) + '\\n')\n"
        "    handle.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    real_adapter = RealOmniAutoAIEngineAdapter()
    monkeypatch.setattr(real_adapter, "_provider_worker_script", sleeper)
    monkeypatch.setattr(real_adapter, "_load_brain", lambda: object())
    monkeypatch.setattr(
        real_adapter,
        "_load_config",
        lambda: {
            "customer_service_brain": {
                "provider": "openai",
                "model": "gpt-5.5",
                "api_key": "test-only",
            }
        },
    )
    monkeypatch.setattr(
        "app.services.ai_adapter.get_settings",
        lambda: SimpleNamespace(c3_brain_provider_timeout_seconds=0.1),
    )

    class ConcurrentSupersedingTimeoutAdapter:
        def generate_reply_decision(self, **kwargs):
            def supersede_batch() -> None:
                time.sleep(0.15)
                with SessionLocal() as other_db:
                    row = other_db.get(MessageBatch, batch["batch_id"])
                    row.status = "superseded"
                    row.active = False
                    row.error_code = "MESSAGE_BATCH_SUPERSEDED"
                    row.ai_response_snapshot = {"superseded_marker": "keep"}
                    other_db.commit()

            thread = threading.Thread(target=supersede_batch)
            thread.start()
            try:
                return real_adapter.generate_reply_decision(**kwargs)
            finally:
                thread.join(timeout=2.0)

    monkeypatch.setattr(
        c3_service,
        "get_ai_engine_adapter",
        lambda: ConcurrentSupersedingTimeoutAdapter(),
    )
    with SessionLocal() as db:
        result = c3_service.generate_for_batch(db, batch_id=batch["batch_id"])
        db.commit()

    assert result["error_code"] == "MESSAGE_BATCH_GENERATION_CLAIM_STALE"
    with SessionLocal() as db:
        row = db.get(MessageBatch, batch["batch_id"])
        assert row.status == "superseded"
        assert row.active is False
        assert row.error_code == "MESSAGE_BATCH_SUPERSEDED"
        assert row.ai_response_snapshot["superseded_marker"] == "keep"
        history = row.ai_response_snapshot["generation_attempt_history"]
        timeout_attempts = [item for item in history if item["attempt"] == 2]
        assert len(timeout_attempts) == 1
        provider_error = timeout_attempts[0]["response"]["raw_payload"][
            "provider_error"
        ]
        assert provider_error["last_provider_progress"]["stage"] == "semantic_reviewer"
        assert provider_error["last_provider_progress"]["event"] == "started"
        assert db.query(ReplyAction).filter(
            ReplyAction.batch_id == batch["batch_id"]
        ).count() == 0


def test_sales_manual_reply_cancels_unsent_reply_action_without_disabling_ai():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-004-c", "我想看轿车")
    old = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    _ingest_with_role(worker, binding["conversation_id"], "msg-c3-sales-001", "我是销售，稍后联系您", "self")

    old_task = client.get(f"/api/tasks/{old['task_id']}", headers=HEADERS).json()["data"]
    assert old_task["status"] == "cancelled"
    with SessionLocal() as db:
        old_action = db.get(ReplyAction, old["reply_action_id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert old_action.status == "cancelled"
        assert conversation.status == "sales_replied_waiting_user"
        assert conversation.ai_enabled is True


def test_verified_hard_opt_out_atomically_rejects_and_cancels_unsent_actions(monkeypatch):
    worker, binding = _setup_bound_conversation()
    first_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-before-hard-opt-out",
        "我想看看轿车",
    )
    old = _generate(_collect(binding["conversation_id"], first_event_id)["batch_id"])
    opt_out_event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-hard-opt-out",
        "请不要再联系我",
    )
    opt_out_batch = _collect(binding["conversation_id"], opt_out_event_id)
    _reset_batch_to_generation_state(
        opt_out_batch["batch_id"],
        status="collecting",
        generation_attempt_count=0,
    )
    with SessionLocal() as db:
        event = db.get(MessageEvent, opt_out_event_id)
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.next_recall_at = utcnow() + timedelta(days=1)
        conversation.recall_cycle_id = "recall-cycle-before-opt-out"
        conversation.recall_origin_status = "waiting_user_reply"
        source_key = event.source_message_key
        db.commit()

    class HardOptOutAdapter:
        def generate_reply_decision(self, **_kwargs):
            return c3_service.AIEngineDecision(
                decision="hard_opt_out",
                confidence=0.99,
                guard_result="pass",
                evidence_refs=[f"message:{opt_out_event_id}"],
                hard_opt_out_evidence={
                    "detected": True,
                    "message_event_id": opt_out_event_id,
                    "source_message_key": source_key,
                    "customer_text": "请不要再联系我",
                    "reason": "explicit_stop_contact",
                },
            )

    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: HardOptOutAdapter())
    with SessionLocal() as db:
        generated = c3_service.generate_for_batch(db, batch_id=opt_out_batch["batch_id"])
        db.commit()

    assert generated["decision"] == "hard_opt_out"
    assert generated["suggested_action"] == "do_not_contact"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        old_action = db.get(ReplyAction, old["reply_action_id"])
        old_task = db.get(Task, old["task_id"])
        batch = db.get(MessageBatch, opt_out_batch["batch_id"])
        assert conversation.status == "rejected"
        assert conversation.ai_enabled is False
        assert conversation.next_recall_at is None
        assert conversation.recall_cycle_id is None
        assert conversation.recall_origin_status is None
        assert conversation.close_reason == f"customer_hard_opt_out:{opt_out_event_id}"
        assert old_action.status in {"superseded", "cancelled"}
        assert old_action.current is False
        assert old_task.status == "cancelled"
        assert batch.status == "rejected"
        assert batch.active is False
        assert db.query(ReplyAction).filter(ReplyAction.batch_id == batch.id).count() == 0


def test_hard_opt_out_with_unmatched_evidence_never_rejects_or_sends(monkeypatch):
    worker, binding = _setup_bound_conversation()
    event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-hard-opt-out-invalid",
        "请不要再联系我",
    )
    batch = _collect(binding["conversation_id"], event_id)
    _reset_batch_to_generation_state(
        batch["batch_id"],
        status="collecting",
        generation_attempt_count=0,
    )

    class InvalidHardOptOutAdapter:
        def generate_reply_decision(self, **_kwargs):
            return c3_service.AIEngineDecision(
                decision="hard_opt_out",
                guard_result="pass",
                hard_opt_out_evidence={
                    "detected": True,
                    "message_event_id": "not-the-current-event",
                    "source_message_key": "not-the-current-source",
                    "customer_text": "请不要再联系我",
                },
            )

    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: InvalidHardOptOutAdapter())
    with SessionLocal() as db:
        generated = c3_service.generate_for_batch(db, batch_id=batch["batch_id"])
        db.commit()

    assert generated["decision"] in {"retry_later", "handoff"}
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status != "rejected"
        assert conversation.ai_enabled is True
        assert db.query(ReplyAction).filter(ReplyAction.batch_id == batch["batch_id"]).count() == 0


def test_claim_send_is_single_owner_and_sent_ack_is_idempotent():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-006", "想了解 15 万 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])
    task_id = generated["task_id"]
    reply_action_id = generated["reply_action_id"]

    ordinary_thread_claim = client.post(
        f"/api/tasks/{task_id}/claim",
        json={"worker_id": worker["id"], "current_step": "chat_reply_claimed"},
        headers=_worker_headers(worker),
    )
    assert ordinary_thread_claim.status_code == 409
    assert ordinary_thread_claim.json()["code"] == "C2_REPLY_TASK_FLOW_OWNERSHIP_REQUIRED"

    claim_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claim_task.status_code == 200
    lease_headers = _task_lease_headers(worker, claim_task)

    first_claim_send = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=lease_headers,
    )
    assert first_claim_send.status_code == 200
    send_data = first_claim_send.json()["data"]

    duplicated_claim = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=lease_headers,
    )
    assert duplicated_claim.status_code == 200
    duplicated_data = duplicated_claim.json()["data"]
    assert duplicated_data["duplicated"] is True
    assert duplicated_data["send_token"] == send_data["send_token"]
    assert (
        duplicated_data["suggested_action"]
        == "reconcile_sent_ack_without_resend"
    )

    ack_payload = {
        "send_token": send_data["send_token"],
        "task_id": task_id,
        "worker_id": worker["id"],
        "client_instance_id": "client-c3",
        "send_result": "sent",
        "action_phase": "confirmed",
        "reply_text_hash": send_data["reply_text_hash"],
        "sidecar_run_id": "sidecar-send-001",
    }
    ack = client.post(f"/api/reply-actions/{reply_action_id}/sent-ack", json=ack_payload, headers=_worker_headers(worker))
    assert ack.status_code == 200
    assert ack.json()["data"]["task"]["status"] == "completed"
    assert ack.json()["data"]["task"]["result_code"] == "chat_reply_sent"
    completed_detail = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["data"]
    _assert_no_forbidden_fields(completed_detail)
    assert completed_detail["c3"]["reply_action"]["status"] == "sent"
    assert completed_detail["c3"]["sent_ack"]["send_result"] == "sent"
    assert completed_detail["c3"]["sent_ack"]["sidecar_run_id"] == "sidecar-send-001"
    worker_detail = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert worker_detail["running_status"] == "idle"
    assert worker_detail["current_task"] is None
    assert worker_detail["current_step"] is None

    duplicated = client.post(f"/api/reply-actions/{reply_action_id}/sent-ack", json=ack_payload, headers=_worker_headers(worker))
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["duplicated"] is True
    assert duplicated.json()["data"]["error_code"] == "SEND_ACK_DUPLICATED"


def test_pre_send_fact_checkpoint_is_frozen_in_batch_and_repeated_on_claim_send():
    worker, binding = _setup_bound_conversation()
    event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-checkpoint-frozen-001",
        "10万块钱的二手车有什么推荐的？",
    )
    generated = _generate(
        _collect(binding["conversation_id"], event_id)["batch_id"]
    )
    batch_id = generated["batch"]["id"]
    action_id = generated["reply_action_id"]
    task_id = generated["task_id"]

    status_response = client.get(
        f"/api/workers/{worker['id']}/wechat/message-batches/{batch_id}",
        headers=_worker_headers(worker),
    )
    assert status_response.status_code == 200, status_response.text
    status = status_response.json()["data"]
    checkpoint = status["pre_send_fact_checkpoint"]
    checkpoint_binding = status["pre_send_fact_checkpoint_binding"]
    assert checkpoint["checkpoint_revision"] == 5
    assert checkpoint["conversation_id"] == binding["conversation_id"]
    assert checkpoint["batch_id"] == batch_id
    assert checkpoint["tail_complete"] is True
    assert checkpoint["baseline_kind"] == "message_tail"
    assert checkpoint["authoritative_frame_source"] == "final_read"
    assert checkpoint["committed_tail"][-1]["message_type"] == "text"
    assert checkpoint["committed_tail"][-1]["worker_stable_id"]
    assert checkpoint_binding == {
        "conversation_id": binding["conversation_id"],
        "batch_id": batch_id,
        "reply_action_id": action_id,
        "checkpoint_digest": c3_service._canonical_sha256(checkpoint),
    }

    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        assert batch.ai_request_snapshot["pre_send_fact_checkpoint"] == checkpoint

    claimed_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed_task.status_code == 200, claimed_task.text
    claim_send = client.post(
        f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert claim_send.status_code == 200, claim_send.text
    claim_data = claim_send.json()["data"]
    assert claim_data["pre_send_fact_checkpoint"] == checkpoint
    assert claim_data["pre_send_fact_checkpoint_binding"] == checkpoint_binding


def test_pure_text_initial_read_freezes_the_authoritative_visible_tail():
    worker, binding = _setup_bound_conversation()
    event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-checkpoint-initial-read-001",
        "想看10万左右的SUV",
        authoritative_frame_source="initial_read",
    )

    generated = _generate(
        _collect(binding["conversation_id"], event_id)["batch_id"]
    )
    status = client.get(
        (
            f"/api/workers/{worker['id']}/wechat/message-batches/"
            f"{generated['batch']['id']}"
        ),
        headers=_worker_headers(worker),
    ).json()["data"]
    checkpoint = status["pre_send_fact_checkpoint"]

    assert checkpoint["tail_complete"] is True
    assert checkpoint["baseline_kind"] == "message_tail"
    assert checkpoint["authoritative_frame_source"] == "initial_read"
    assert len(checkpoint["committed_tail"]) == 1
    assert checkpoint["committed_tail"][0]["message_type"] == "text"
    assert worker_checkpoint_binding_error(
        checkpoint,
        status["pre_send_fact_checkpoint_binding"],
        conversation_id=binding["conversation_id"],
        batch_id=generated["batch"]["id"],
        reply_action_id=generated["reply_action_id"],
    ) == ""
    comparison = worker_compare_checkpoint(
        checkpoint,
        [
            {
                "observation_id": "fresh-initial-read-text",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "content_clean": "想看10万左右的SUV",
            }
        ],
        before_frame_id="checkpoint:backend-initial-read",
        after_frame_id="frame:worker-pre-send",
        current_tail_complete=True,
    )
    assert comparison["comparison_result"] == "checkpoint_equal"


def test_friend_welcome_freezes_an_explicit_complete_empty_baseline():
    worker, binding = _setup_bound_conversation()
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "ai_active"
        created = c3_service.create_control_message_batch(
            db,
            conversation_id=binding["conversation_id"],
            trigger_type="friend_welcome",
            trigger_key="friend_welcome",
            trace_id="trace-welcome-checkpoint",
        )
        db.commit()
        generated = c3_service.generate_for_batch(
            db,
            batch_id=created["batch_id"],
        )
        db.commit()

    status = client.get(
        (
            f"/api/workers/{worker['id']}/wechat/message-batches/"
            f"{generated['batch']['id']}"
        ),
        headers=_worker_headers(worker),
    ).json()["data"]
    checkpoint = status["pre_send_fact_checkpoint"]

    assert checkpoint == {
        "checkpoint_revision": 5,
        "conversation_id": binding["conversation_id"],
        "batch_id": generated["batch"]["id"],
        "baseline_kind": "friend_welcome_empty",
        "authoritative_frame_source": "control_empty",
        "committed_tail": [],
        "tail_complete": True,
    }
    binding_payload = status["pre_send_fact_checkpoint_binding"]
    assert worker_checkpoint_binding_error(
        checkpoint,
        binding_payload,
        conversation_id=binding["conversation_id"],
        batch_id=generated["batch"]["id"],
        reply_action_id=generated["reply_action_id"],
    ) == ""
    unchanged = worker_compare_checkpoint(
        checkpoint,
        [],
        before_frame_id="checkpoint:backend-welcome",
        after_frame_id="frame:worker-empty",
        current_tail_complete=True,
        current_empty_viewport_confirmed=True,
    )
    superseded = worker_compare_checkpoint(
        checkpoint,
        [
            {
                "observation_id": "first-customer-message",
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "content_clean": "想看10万左右的SUV",
            }
        ],
        before_frame_id="checkpoint:backend-welcome",
        after_frame_id="frame:worker-first-message",
        current_tail_complete=True,
    )
    assert unchanged["comparison_result"] == "checkpoint_equal"
    assert superseded["comparison_result"] == (
        "checkpoint_unique_prefix_with_suffix"
    )


@pytest.mark.parametrize(
    "authoritative_frame_source",
    ["initial_read", "final_read"],
)
def test_checkpoint_tail_uses_latest_complete_frame_not_entire_long_history(
    authoritative_frame_source,
):
    def message(index: int, content: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"event-{index}",
            sender_role="customer",
            message_type="text",
            content=content,
            raw_payload={
                "dedupe_basis": {
                    "worker_stable_id": f"worker-message-{index}"
                }
            },
            evidence={},
        )

    first = message(1, "已移出当前完整尾部")
    second = message(2, "当前可见的第一条")
    third = message(3, "当前可见的第二条")
    third.evidence = {
        "authoritative_frame_source": authoritative_frame_source,
        "observation_validation_errors": [],
        "observations": [
            {
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "_worker_stable_id": "worker-message-2",
            },
            {
                "row_kind": "text_bubble",
                "sender_role": "customer",
                "message_type": "text",
                "_worker_stable_id": "worker-message-3",
            },
        ],
    }

    projected = c3_service._checkpoint_tail_from_latest_complete_frame(
        [first, second, third]
    )

    assert [item.id for item in projected] == ["event-2", "event-3"]


def test_checkpoint_invalid_failed_ack_is_technical_and_creates_no_handoff():
    worker, binding = _setup_bound_conversation()
    event_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-checkpoint-invalid-001",
        "想了解 15 万 SUV",
    )
    generated = _generate(
        _collect(binding["conversation_id"], event_id)["batch_id"]
    )
    task_id = generated["task_id"]
    action_id = generated["reply_action_id"]

    claimed_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claimed_task.status_code == 200, claimed_task.text
    claim_send = client.post(
        f"/api/reply-actions/{action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert claim_send.status_code == 200, claim_send.text
    send_data = claim_send.json()["data"]

    ack = client.post(
        f"/api/reply-actions/{action_id}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": task_id,
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "failed",
            "action_phase": "not_attempted",
            "reply_text_hash": send_data["reply_text_hash"],
            "error_code": "C2_PRE_SEND_FACT_CHECKPOINT_INVALID",
            "remark": "checkpoint binding mismatch; zero wechat operation",
        },
        headers=_worker_headers(worker),
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["data"]["task"]["status"] == "failed"
    with SessionLocal() as db:
        assert (
            db.query(HandoffEvent)
            .filter(HandoffEvent.batch_id == generated["batch"]["id"])
            .count()
            == 0
        )


def test_new_customer_message_during_sending_keeps_original_ack_valid():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-pre-send-001", "想了解 15 万 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])
    task_id = generated["task_id"]
    reply_action_id = generated["reply_action_id"]

    claim_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claim_task.status_code == 200
    lease_headers = _task_lease_headers(worker, claim_task)
    claim_send = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=lease_headers,
    )
    assert claim_send.status_code == 200
    send_data = claim_send.json()["data"]

    m2 = _ingest(worker, binding["conversation_id"], "msg-c3-pre-send-002", "我又改主意了，想看新能源")
    new_batch = _collect(binding["conversation_id"], m2)
    new_action = _generate(new_batch["batch_id"])

    with SessionLocal() as db:
        old_action = db.get(ReplyAction, reply_action_id)
        assert old_action.status == "sending"
    old_task = client.get(f"/api/tasks/{task_id}", headers=HEADERS).json()["data"]
    assert old_task["status"] == "running"
    assert new_action["reply_action_id"] != reply_action_id

    ack_payload = {
        "send_token": send_data["send_token"],
        "task_id": task_id,
        "worker_id": worker["id"],
        "client_instance_id": "client-c3",
        "send_result": "sent",
        "action_phase": "confirmed",
        "reply_text_hash": send_data["reply_text_hash"],
        "sidecar_run_id": "sidecar-send-stale",
    }
    acknowledged = client.post(
        f"/api/reply-actions/{reply_action_id}/sent-ack",
        json=ack_payload,
        headers=_worker_headers(worker),
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["data"]["reply_action"]["status"] == "sent"
    assert acknowledged.json()["data"]["task"]["status"] == "completed"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "ai_active"

    binding_after = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS).json()["data"]
    assert "conversation_status" not in binding_after
    assert "ai_enabled" not in binding_after


def test_sent_ack_unknown_marks_unknown_result_and_prevents_auto_resend():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-unknown-001", "想了解 15 万 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])
    task_id = generated["task_id"]
    reply_action_id = generated["reply_action_id"]

    claim_task = client.post(
        f"/api/tasks/{task_id}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    assert claim_task.status_code == 200
    lease_headers = _task_lease_headers(worker, claim_task)
    claim_send = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=lease_headers,
    )
    assert claim_send.status_code == 200
    send_data = claim_send.json()["data"]

    ack = client.post(
        f"/api/reply-actions/{reply_action_id}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": task_id,
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "unknown",
            "action_phase": "trigger_attempted",
            "reply_text_hash": send_data["reply_text_hash"],
            "error_code": "SEND_RESULT_UNKNOWN",
        },
        headers=_worker_headers(worker),
    )
    assert ack.status_code == 200
    data = ack.json()["data"]
    assert data["reply_action"]["status"] == "unknown_send_result"
    assert data["task"]["status"] == "failed"
    assert data["task"]["error_code"] == "SEND_RESULT_UNKNOWN"
    assert data["task"]["failure_remark"] == (
        "发送结果未知，原动作已终结且禁止补发；"
        "会话已转销售正常接管。"
    )
    assert data["task"]["available_actions"] == []
    worker_detail = client.get(f"/api/workers/{worker['id']}", headers=HEADERS).json()["data"]
    assert worker_detail["running_status"] == "idle"
    assert worker_detail["current_task"] is None
    assert worker_detail["current_step"] is None

    resend = client.post(
        f"/api/reply-actions/{reply_action_id}/claim-send",
        json={"task_id": task_id, "worker_id": worker["id"]},
        headers=_worker_headers(worker),
    )
    assert resend.status_code == 409
    assert resend.json()["code"] == "REPLY_ACTION_CLAIM_CONFLICT"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == generated["batch"]["id"],
            HandoffEvent.handoff_reason_code == "SEND_RESULT_UNKNOWN",
        ).one()
        batch = db.get(MessageBatch, generated["batch"]["id"])
        assert conversation.status == "waiting_sales_reply"
        assert conversation.handoff_reason_code == "SEND_RESULT_UNKNOWN"
        assert handoff.status == "created"
        assert handoff.reason_detail == (
            "发送结果未知，原动作已终结且禁止补发；"
            "会话已转销售正常接管。"
        )
        assert batch.status == "handoff_created"
        assert conversation.reply_count == 0


def test_stale_sending_reply_is_released_and_handed_off_without_resend():
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-stale-send",
        "请回复后模拟客户端崩溃",
    )
    generated = _generate(_collect(binding["conversation_id"], message_id)["batch_id"])
    claimed_task = client.post(
        f"/api/tasks/{generated['task_id']}/claim",
        json={
            "worker_id": worker["id"],
            "current_step": "chat_reply_claimed",
            "claim_source": "c2_conversation_flow",
            "conversation_id": binding["conversation_id"],
        },
        headers=_worker_headers(worker),
    )
    claim_send = client.post(
        f"/api/reply-actions/{generated['reply_action_id']}/claim-send",
        json={"task_id": generated["task_id"], "worker_id": worker["id"]},
        headers=_task_lease_headers(worker, claimed_task),
    )
    assert claim_send.status_code == 200
    send_data = claim_send.json()["data"]
    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        action.sending_claimed_at = utcnow() - timedelta(seconds=600)
        db.commit()

    result = recover_stale_reply_sends_once()

    assert result == {"examined": 1, "recovered": 1, "failed": 0}
    with SessionLocal() as db:
        action = db.get(ReplyAction, generated["reply_action_id"])
        task = db.get(Task, generated["task_id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.query(HandoffEvent).filter(
            HandoffEvent.batch_id == generated["batch"]["id"],
            HandoffEvent.handoff_reason_code == "SEND_ACK_TIMEOUT",
        ).one()
        assert action.status == "unknown_send_result"
        assert task.status == "failed"
        assert task.error_code == "SEND_ACK_TIMEOUT"
        assert conversation.status == "waiting_sales_reply"
        assert handoff.status == "created"
        assert db.query(SentAck).filter(
            SentAck.reply_action_id == generated["reply_action_id"]
        ).count() == 0

    late_ack = client.post(
        f"/api/reply-actions/{generated['reply_action_id']}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": generated["task_id"],
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "sent",
            "action_phase": "confirmed",
            "reply_text_hash": send_data["reply_text_hash"],
            "sidecar_run_id": "late-sidecar-run",
        },
        headers=_worker_headers(worker),
    )
    assert late_ack.status_code == 200
    late_data = late_ack.json()["data"]
    assert late_data["error_code"] == (
        "SEND_ACK_RECONCILED_TO_UNKNOWN_TERMINAL"
    )
    assert late_data["reply_action"]["status"] == "unknown_send_result"
    assert late_data["task"]["status"] == "failed"
    assert late_data["ack"]["send_result"] == "unknown"
    assert late_data["ack"]["evidence"]["reported_send_result"] == "sent"

    duplicate_late_ack = client.post(
        f"/api/reply-actions/{generated['reply_action_id']}/sent-ack",
        json={
            "send_token": send_data["send_token"],
            "task_id": generated["task_id"],
            "worker_id": worker["id"],
            "client_instance_id": "client-c3",
            "send_result": "sent",
            "action_phase": "confirmed",
            "reply_text_hash": send_data["reply_text_hash"],
        },
        headers=_worker_headers(worker),
    )
    assert duplicate_late_ack.status_code == 200
    assert duplicate_late_ack.json()["data"]["duplicated"] is True


def test_handoff_decision_uses_state_gate_without_disabling_ai():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-007", "你们最低价是多少")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    assert generated["decision"] == "handoff"
    assert generated["handoff_event_id"]
    tasks = client.get("/api/tasks?task_type=chat_reply", headers=HEADERS).json()["data"]["items"]
    assert tasks == []
    binding_after = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS).json()["data"]
    assert "ai_enabled" not in binding_after
    assert "conversation_status" not in binding_after
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.ai_enabled is True
        assert conversation.status == "waiting_sales_reply"
        assert conversation.handoff_reason_code == "HANDOFF_REQUIRED"


def test_high_intent_notifies_sales_by_handoff_without_reply_action(monkeypatch):
    class HighIntentAdapter:
        def generate_reply_decision(self, **_kwargs):
            return c3_service.AIEngineDecision(
                decision="handoff",
                confidence=0.97,
                handoff_reason_code="CUSTOMER_HIGH_INTENT",
                risk_flags=["customer_high_intent"],
                evidence_refs=["policy:chejin_handoff_high_intent"],
                guard_result="handoff",
                error_code="CUSTOMER_HIGH_INTENT",
                suggested_action="handoff",
            )

    monkeypatch.setattr(
        c3_service,
        "get_ai_engine_adapter",
        lambda: HighIntentAdapter(),
    )
    worker, binding = _setup_bound_conversation()
    message_id = _ingest(
        worker,
        binding["conversation_id"],
        "msg-c3-high-intent",
        "我今天想直接到店看车",
    )
    batch = _collect(binding["conversation_id"], message_id)
    generated = _generate(batch["batch_id"])

    assert generated["decision"] == "handoff"
    assert generated["error_code"] == "CUSTOMER_HIGH_INTENT"
    assert generated["handoff_event_id"]
    assert generated.get("reply_action_id") is None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.query(HandoffEvent).one()
        assert conversation.status == "waiting_sales_reply"
        assert conversation.handoff_reason_code == "CUSTOMER_HIGH_INTENT"
        assert handoff.status == "created"
        assert handoff.handoff_reason_code == "CUSTOMER_HIGH_INTENT"
        assert handoff.risk_flags == ["customer_high_intent"]
        assert db.query(ReplyAction).count() == 0


def test_formal_api_responses_do_not_leak_deprecated_fields():
    worker, binding = _setup_bound_conversation()
    m1 = _ingest(worker, binding["conversation_id"], "msg-c3-field-scan-001", "我想了解 SUV")
    generated = _generate(_collect(binding["conversation_id"], m1)["batch_id"])

    responses = [
        client.get(f"/api/workers/{worker['id']}", headers=HEADERS),
        client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS),
        client.get(f"/api/conversations/{binding['conversation_id']}/messages", headers=HEADERS),
        client.get(f"/api/tasks/{generated['task_id']}", headers=HEADERS),
        client.post(f"/api/internal/message-batches/{generated['batch']['id']}/generate", json={}, headers=INTERNAL_HEADERS),
    ]
    for response in responses:
        assert response.status_code == 200
        _assert_no_forbidden_fields(response.json())
