import copy
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

WORKER_CLIENT_ROOT = Path(__file__).resolve().parents[2] / "worker-client"
if str(WORKER_CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_CLIENT_ROOT))
OMNIAUTO_ROOT = WORKER_CLIENT_ROOT / "omniauto-rpa"
if str(OMNIAUTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OMNIAUTO_ROOT))

from apps.wechat_ai_customer_service.adapters.wechat_win32_ocr_sidecar import build_message_observations_v3
from chejin_worker_client.models import WechatReadTarget as WorkerWechatReadTarget
from chejin_worker_client.message_identity_commit import (
    MessageCommitBasis,
    committed_identity_record,
)
from chejin_worker_client.message_viewport_projection import (
    compare_business_viewport_continuity,
    normalized_business_message_sequence,
    stable_business_content_signature,
)
from chejin_worker_client.message_contract import (
    canonical_message_identity_text as worker_canonical_message_identity_text,
    canonical_reply_text as worker_canonical_reply_text,
    reply_text_hash as worker_reply_text_hash,
)
from chejin_worker_client.c2_outbox_recovery import (
    split_ingest_payload,
)
from chejin_worker_client.wechat_c2 import (
    build_message_ingest_payload as build_worker_message_ingest_payload,
    image_observation_source_key,
    voice_observation_source_key,
)
from chejin_worker_client.task_runner import (
    TaskRunner as WorkerTaskRunner,
    _continuity_alignment_evidence_for_suffix,
    should_submit_c2_ingest_payload,
)
import chejin_worker_client.storage as worker_storage
from chejin_worker_client.sequence_alignment import (
    normalized_content_hash as worker_normalized_content_hash,
)
from chejin_worker_client.pre_send_checkpoint import (
    compare_checkpoint_to_observations as worker_compare_checkpoint,
)

from app.contracts.c2 import c2_contract_v3, contract_revision, contract_sha256
from app.contracts.message_limits import (
    C2_MESSAGE_BATCH_MAX_ITEMS,
    C2_MESSAGE_CONTENT_MAX_CHARS,
    C2_MESSAGE_INGEST_MAX_BYTES,
    C2_MESSAGE_RAW_PAYLOAD_MAX_BYTES,
)
from app.core.database import Base, engine
from app.errors import AppError
from app.main import app
from app.models.base import utcnow
from app.models.audit import OperationLog
from app.models.c3 import Conversation, HandoffEvent, MessageBatch, ReplyAction
from app.models.sales import Sales
from app.models.task import Task
from app.models.wechat import (
    MessageEvent,
    WechatRecoverySettlement,
    WechatSessionBinding,
)
from app.models.worker import Worker
from app.core.database import SessionLocal
from app.schemas.wechat import (
    WechatMessageEvidence,
    WechatMessageIngestRequest,
)
from app.services import c3_service, wechat_service
from app.services.message_contract import (
    canonical_message_identity_text as backend_canonical_message_identity_text,
    canonical_reply_text as backend_canonical_reply_text,
    reply_text_hash as backend_reply_text_hash,
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


client = TestClient(app)
HEADERS = {
    "X-Operator-Id": "00000000-0000-0000-0000-000000000001",
    "X-Operator-Name": "Ops Tester",
    "X-Operator-Role": "admin",
}


def test_reply_text_contract_is_identical_in_worker_and_backend():
    values = [
        "第一行\n第二行\t第三行",
        "  第一行\u00a0第二行  第三行  ",
        "第一行 第二行 第三行",
    ]
    contract = c2_contract_v3()["reply_text_contract"]

    assert contract["normalization"] == "unicode_whitespace_collapse"
    assert contract["hash"] == "sha256_utf8_canonical_text"
    assert {
        worker_canonical_reply_text(value) for value in values
    } == {
        backend_canonical_reply_text(value) for value in values
    } == {"第一行 第二行 第三行"}
    assert {
        worker_reply_text_hash(value) for value in values
    } == {
        backend_reply_text_hash(value) for value in values
    }


def test_frame_visual_identity_never_changes_backend_media_identity():
    contract = c2_contract_v3()
    alignment = contract["sequence_alignment_contract"]
    identity = contract["message_identity_contract"]
    assert alignment["strong_anchor_fields"] == [
        "native_source_message_id",
        "confirmed_action_mapping",
    ]
    assert "frame_visual_id" in alignment[
        "forbidden_cross_action_identity_inputs"
    ]
    assert identity["ocr_cross_round_identity_field"] == "worker_stable_id"

    def raw(stable_id: str, frame_visual_id: str) -> dict:
        return {
            "dedupe_basis": {
                "source": "worker_cross_round_sequence",
                "worker_stable_id": stable_id,
            },
            "observation": {
                "row_kind": "image_bubble",
                "frame_visual_id": frame_visual_id,
                "source_message": {
                    "frame_visual_id": frame_visual_id,
                },
            },
        }

    before = wechat_service._media_identity_hash(
        "image", raw("worker-message-7", "frame-before")
    )
    shifted = wechat_service._media_identity_hash(
        "image", raw("worker-message-7", "frame-after")
    )
    another = wechat_service._media_identity_hash(
        "image", raw("worker-message-8", "frame-after")
    )
    assert before
    assert shifted == before
    assert another != before
    assert (
        wechat_service._validate_cross_round_message_identity(
            raw("worker-message-7", "frame-before")
        )
        == "worker-message-7"
    )

    missing_stable = raw("worker-message-7", "frame-before")
    missing_stable["dedupe_basis"] = {
        "source": "frame_visual_id",
        "frame_visual_id": "frame-before",
    }
    with pytest.raises(AppError) as missing_error:
        wechat_service._validate_cross_round_message_identity(
            missing_stable
        )
    assert getattr(missing_error.value, "code", "") == (
        "MESSAGE_SEQUENCE_IDENTITY_MISSING"
    )

    forbidden_legacy = raw("worker-message-7", "frame-before")
    forbidden_legacy["observation"]["canonical_visual_id"] = (
        "canonical_visual_old-position"
    )
    with pytest.raises(AppError) as legacy_error:
        wechat_service._validate_cross_round_message_identity(
            forbidden_legacy
        )
    assert getattr(legacy_error.value, "code", "") == (
        "MESSAGE_FRAME_IDENTITY_FORBIDDEN"
    )


def test_message_identity_contract_ignores_only_visual_cjk_line_wraps():
    contract = c2_contract_v3()["message_identity_text_contract"]
    wrapped = "请问有\n什么可以帮您？"
    unwrapped = "请问有什么可以帮您？"

    assert (
        contract["normalization"]
        == "legacy_preserving_visual_cjk_line_wrap"
    )
    assert worker_canonical_message_identity_text(wrapped) == unwrapped
    assert backend_canonical_message_identity_text(wrapped) == unwrapped
    assert worker_canonical_message_identity_text("hello\nworld") == "hello world"
    assert backend_canonical_message_identity_text("hello\nworld") == "hello world"
    assert worker_canonical_message_identity_text("hello world") != "helloworld"
    legacy_values = ["全角？标点！", "保留  水平\t空白"]
    for value in legacy_values:
        legacy_hash = hashlib.sha256(
            backend_canonical_reply_text(value).encode("utf-8")
        ).hexdigest()
        assert (
            worker_canonical_message_identity_text(value)
            == backend_canonical_reply_text(value)
        )
        assert (
            backend_canonical_message_identity_text(value)
            == backend_canonical_reply_text(value)
        )
        assert worker_normalized_content_hash(value) == legacy_hash
        assert wechat_service._normalized_content_hash(value) == legacy_hash


def _v3_contract_fields() -> dict:
    return {
        "contract_version": 3,
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
    }


def _v3_raw_fields(source_message_key: str) -> dict:
    return {
        **_v3_contract_fields(),
        "source_message_key": source_message_key,
        "dedupe_basis": {
            "source": "worker_cross_round_sequence",
            "worker_stable_id": (
                "worker-message-test-"
                + hashlib.sha256(
                    source_message_key.encode("utf-8")
                ).hexdigest()[:16]
            ),
        },
    }


def _voice_action_evidence(
    *,
    action_id: str = "voice-action-test",
    stable_id: str = "voice-stable-test",
    action_token: str = "voice-token-test",
    pre_observation_id: str = "voice:pre",
    trigger_observation_id: str = "voice:execute",
    post_observation_id: str = "voice:final",
    content_signature: str = "a" * 64,
    result_screen_order: int = 0,
) -> dict:
    frames = ["frame:voice:pre", "frame:voice:execute", "frame:voice:final"]
    observations = [
        pre_observation_id,
        trigger_observation_id,
        post_observation_id,
    ]
    action_result_receipt = {
        "schema_version": 1,
        "canonical_action_id": action_id,
        "reserved_worker_stable_id": stable_id,
        "selected_action_token": action_token,
        "pre_observation_id": observations[0],
        "trigger_observation_id": observations[1],
        "physical_identity_inherited_from_prepare": False,
        "physical_action_count": 1,
        "result_candidate_count": 1,
        "stable_business_content_signature": content_signature,
        "result_screen_order": result_screen_order,
        "binding_confirmed": True,
        "post_observation_id": observations[-1],
    }
    return {
        "state": "voice_transcribe_completed",
        "voice_action_stage": "execute",
        "action_phase": "confirmed",
        "ui_action_performed": True,
        "canonical_voice_action_id": action_id,
        "reserved_worker_stable_id": stable_id,
        "pre_frame_id": frames[0],
        "post_frame_id": frames[-1],
        "selected_pre_observation_id": observations[0],
        "selected_action_token": action_token,
        "selected_target_fingerprint": "voice-fingerprint-test",
        "message_viewport_change_digest": "d" * 64,
        "transcript_binding_status": "confirmed",
        "transcript_binding_method": "actual_action_result",
        "binding_candidate_count": 1,
        "tracking_frame_ids": frames,
        "tracking_edges": [
            {
                "from_frame_id": frames[index],
                "from_observation_id": observations[index],
                "to_frame_id": frames[index + 1],
                "to_observation_id": observations[index + 1],
                "sender_role": "customer",
                "message_type": "voice",
                "structural_evidence": {"same_target": True},
                "displacement_evidence": {"delta_y": 0},
                "edge_candidate_count": 1,
            }
            for index in range(2)
        ],
        "matched_neighbor_pairs": [],
        "native_source_message_id": None,
        "action_result_receipt": dict(action_result_receipt),
        "confirmed_action_mapping": {
            **{
                key: value
                for key, value in action_result_receipt.items()
                if key != "schema_version"
            },
            "derived_observation_ids": [],
        },
    }


def _binding_authorization_revision(binding_id: str) -> str:
    with SessionLocal() as db:
        binding = db.get(WechatSessionBinding, binding_id)
        assert binding is not None
        return wechat_service._authorization_revision(binding)


def _v3_message(
    source_key: str,
    *,
    role: str,
    message_type: str,
    content: str | None,
    screen_order: int,
    raw_extra: dict | None = None,
    occurred_at: str | None = None,
    order_source: str = "observation_index_fallback",
) -> dict:
    row_kind = {
        "text": "text_bubble",
        "voice": "voice_transcript",
        "system": "system_message",
        "image": "image_bubble",
    }[message_type]
    role_source = "parent_voice" if message_type == "voice" else "system" if role == "system" else "same_row_avatar"
    observation = {
        "schema_version": 3,
        "observation_id": f"observation:{source_key}",
        "row_kind": row_kind,
        "sender_role": role,
        "sender_role_source": role_source,
        "message_type": message_type,
        "voice_state": "transcribed" if message_type == "voice" else "not_voice",
        "source_message": {
            "id": source_key,
            "type": message_type,
            "sender_role": role,
            "content": content,
        },
    }
    if content:
        observation["content_clean"] = content
    if message_type == "voice":
        observation["parent_voice_anchor_key"] = f"anchor:{source_key}"
        observation["source_message"]["voice_anchor_stable_key"] = f"anchor:{source_key}"
    if message_type == "image":
        image_anchor = {
            "sender_role": role,
            "preceding_stable_message": source_key,
            "following_stable_message": "",
            "occurrence_index": 0,
        }
        observation["image_physical_anchor"] = image_anchor
        observation["source_message"]["image_physical_anchor"] = image_anchor
        understanding = {
            "schema_version": 1,
            "enabled": True,
            "applied": True,
            "adoptable": True,
            "reason": "vision_ready",
            "provider": "https://aiself.vip/v1",
            "request_style": "anthropic_messages_vision",
            "model": "doubao-seed-2-0-lite-260428",
            "vision_summary": content,
            "image_ocr_text": [],
            "classification": {
                "is_vehicle": True,
                "vehicle_confidence": 0.9,
                "unknown": False,
                "non_vehicle_reason": "",
            },
            "entities": {
                "brand_candidates": [],
                "series_candidates": [],
                "model_clues": [],
                "body_type": "",
                "color": "",
                "year_clues": [],
            },
            "intent_hints": {
                "wants_catalog_match": False,
                "wants_similar_recommendation": False,
                "wants_general_chat": False,
                "needs_clarification": False,
            },
            "bridge": {
                "normalized_vehicle_query": "",
                "brain_mode": "",
                "catalog_lookup_mode": "",
            },
            "catalog_alignment": {
                "selected_product_id": "",
                "selected_product_name": "",
                "alignment_confidence": 0.0,
                "alignment_reason": "",
                "uncertain_reason": "",
            },
            "audit": {
                "latency_ms": 10,
                "used_fallback": False,
                "provider_error": "",
                "retry_error": "",
                "retry_after_non_json": False,
                "catalog_identity_candidate_count": 0,
            },
        }
        bridge = {
            "schema_version": 1,
            "present": True,
            "vision_summary": content,
            "classification": {
                "is_vehicle": True,
                "vehicle_confidence": 0.9,
                "unknown": False,
            },
            "catalog_assist": {
                "normalized_vehicle_query": "",
                "candidate_names": [],
                "exact_candidate_name": "",
            },
            "intent_hints": {
                "wants_catalog_match": False,
                "wants_similar_recommendation": False,
                "needs_clarification": False,
            },
            "vehicle_image_retrieval": {
                "matched": False,
                "candidate_names": [],
            },
            "source_message_ids": [source_key],
        }
        observation["customer_image_understanding"] = understanding
        observation["visual_bridge_input"] = bridge
    message_position = {
        "screen_order": screen_order,
        "frame_source": "final_read",
        "order_source": order_source,
    }
    if order_source == "visual_top":
        message_position.update(
            {
                "visual_top": screen_order * 100,
                "visual_bottom": screen_order * 100 + 40,
            }
        )
    payload = {
        "dedupe_key": source_key,
        "source_message_key": source_key,
        "sender_role_hint": role,
        "message_type": message_type,
        "content": content,
        "item_state": "completed",
        "flow_state": "completed",
        "message_position": message_position,
        "raw_payload": {
            **_v3_raw_fields(source_key),
            "observation": observation,
            **({"customer_image_understanding": understanding, "visual_bridge_input": bridge} if message_type == "image" else {}),
            **(raw_extra or {}),
        },
    }
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at
    return payload


def _v3_failed_image_message(
    source_key: str,
    *,
    role: str,
    screen_order: int,
    reason: str,
    order_source: str = "observation_index_fallback",
) -> dict:
    image_anchor = {
        "sender_role": role,
        "preceding_stable_message": source_key,
        "following_stable_message": "",
        "occurrence_index": 0,
    }
    observation = {
        "schema_version": 3,
        "observation_id": f"observation:{source_key}",
        "row_kind": "image_bubble",
        "sender_role": role,
        "sender_role_source": "same_row_avatar",
        "message_type": "image",
        "voice_state": "not_voice",
        "item_state": "failed",
        "image_physical_anchor": image_anchor,
        "error_code": reason,
        "reason_detail": reason,
        "source_message": {
            "id": source_key,
            "type": "image",
            "sender_role": role,
            "image_physical_anchor": image_anchor,
        },
    }
    message_position = {
        "screen_order": screen_order,
        "frame_source": "final_read",
        "order_source": order_source,
    }
    if order_source == "visual_top":
        message_position.update(
            {
                "visual_top": screen_order * 100,
                "visual_bottom": screen_order * 100 + 40,
            }
        )
    return {
        "dedupe_key": source_key,
        "source_message_key": source_key,
        "sender_role_hint": role,
        "message_type": "image",
        "content": None,
        "item_state": "failed",
        "flow_state": "completed",
        "message_position": message_position,
        "raw_payload": {
            **_v3_raw_fields(source_key),
            "observation": observation,
            "error_code": reason,
            "reason_detail": reason,
        },
    }


def _v3_failed_voice_message(
    source_key: str,
    *,
    role: str,
    screen_order: int,
    reason: str,
    order_source: str = "observation_index_fallback",
) -> dict:
    voice_anchor_key = f"voice-anchor:{source_key}"
    observation = {
        "schema_version": 3,
        "observation_id": f"observation:{source_key}",
        "row_kind": "voice_bubble",
        "sender_role": role,
        "sender_role_source": "same_row_avatar",
        "message_type": "voice",
        "voice_state": "untranscribed",
        "item_state": "failed",
        "voice_anchor_key": voice_anchor_key,
        "error_code": reason,
        "reason_detail": reason,
        "source_message": {
            "id": source_key,
            "type": "voice",
            "sender_role": role,
            "voice_anchor_stable_key": voice_anchor_key,
        },
    }
    message_position = {
        "screen_order": screen_order,
        "frame_source": "final_read",
        "order_source": order_source,
    }
    if order_source == "visual_top":
        message_position.update(
            {
                "visual_top": screen_order * 100,
                "visual_bottom": screen_order * 100 + 40,
            }
        )
    return {
        "dedupe_key": source_key,
        "source_message_key": source_key,
        "sender_role_hint": role,
        "message_type": "voice",
        "content": None,
        "item_state": "failed",
        "flow_state": "failed",
        "message_position": message_position,
        "raw_payload": {
            **_v3_raw_fields(source_key),
            "observation": observation,
            "error_code": reason,
            "reason_detail": reason,
        },
    }


def _v3_ingest_payload(
    binding: dict,
    remark_code: str,
    *,
    read_run_id: str,
    messages: list[dict],
    rpa_session_key: str | None = None,
    read_reason: str = "waiting_sales_reply",
    unread_generation: int | None = None,
) -> dict:
    ai_reply_boundary: dict = {}
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        if conversation is not None and conversation.status == "ai_active":
            if read_reason == "friend_acceptance_visible_hit":
                conversation.friend_state = "friend_active"
                conversation.status = "friend_activation_reading"
            elif read_reason == "recall_precheck":
                conversation.status = "recall_precheck"
            elif read_reason == "visible_unread":
                conversation.status = "ai_active"
            elif read_reason in {"waiting_user_reply", "recent_ai_sent"}:
                conversation.status = "waiting_user_reply"
            else:
                conversation.status = "waiting_sales_reply"
            db.commit()
        if read_reason == "recent_ai_sent":
            sent_action = db.scalar(
                select(ReplyAction)
                .where(
                    ReplyAction.conversation_id == binding["conversation_id"],
                    ReplyAction.status == "sent",
                    ReplyAction.sent_at.is_not(None),
                )
                .order_by(ReplyAction.sent_at.desc())
                .limit(1)
            )
            if sent_action is not None:
                ai_reply_boundary = {
                    "reply_action_id": sent_action.id,
                    "sent_at": sent_action.sent_at.isoformat(),
                    "reply_text_hash": sent_action.reply_text_hash,
                }
    observations = [
        message["raw_payload"]["observation"]
        for message in messages
        if isinstance(message.get("raw_payload"), dict)
        and isinstance(message["raw_payload"].get("observation"), dict)
    ]
    return {
        **_v3_contract_fields(),
        "read_run_id": read_run_id,
        "conversation_id": binding["conversation_id"],
        "remark_code": remark_code,
        "rpa_session_key": binding.get("rpa_session_key") if rpa_session_key is None else rpa_session_key,
        "authorization_revision": _binding_authorization_revision(binding["id"]),
        "unread_generation": (
            int(binding.get("unread_generation") or 0)
            if unread_generation is None
            else max(0, int(unread_generation))
        ),
        "messages": messages,
        "evidence": {
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
            "authoritative_frame_source": "final_read",
            "observations": observations,
            "read_reason": read_reason,
            "authorization_read_reason": read_reason,
            "ai_reply_boundary": ai_reply_boundary or None,
            "finished_at": utcnow().isoformat(),
            "flow_gate_errors": [],
            "flow_gate_details": [],
            "slot_ledger_states": [
                {
                    "observation_id": str(
                        message["raw_payload"]["observation"][
                            "observation_id"
                        ]
                    ),
                    "screen_order": int(
                        message["message_position"]["screen_order"]
                    ),
                    "order_source": str(
                        message["message_position"]["order_source"]
                    ),
                    "row_kind": str(
                        message["raw_payload"]["observation"]["row_kind"]
                    ),
                    "source_message_key": str(
                        message["source_message_key"]
                    ),
                    "origin_read_run_id": read_run_id,
                    "fact_scope": "current_read_run",
                    "delivery_state": "not_enqueued",
                    "item_state": str(
                        message.get("item_state") or "completed"
                    ),
                }
                for message in messages
            ],
            "sequence_alignment_evidence": {
                "pre_sequence_source": "empty_checkpoint",
                "pre_frame_id": f"checkpoint:none:{binding['conversation_id']}",
                "post_frame_id": f"frame:{read_run_id}",
                "alignment_status": "not_required",
                "candidate_alignment_count": 0,
                "matched_pairs": [],
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [
                    str(observation.get("observation_id") or "")
                    for observation in observations
                    if str(observation.get("observation_id") or "")
                ],
            },
        },
    }


def _committed_test_observation(
    observation: dict,
    *,
    worker_sequence: int,
    commit_basis: MessageCommitBasis,
    proof: dict,
    runtime_evidence: dict | None = None,
) -> dict:
    """Build raw Sidecar output that must still cross the production Worker gate.

    This helper deliberately does not manufacture an ingest message or a
    backend checkpoint.  It only supplies the Worker-owned annotations that a
    completed C2 action would have produced before
    ``build_worker_message_ingest_payload`` validates and serializes them.
    """

    committed = copy.deepcopy(observation)
    stable_id = f"worker-message-{worker_sequence}"
    committed.update(
        {
            "_worker_stable_id": stable_id,
            "_worker_identity_scope": "committed",
        }
    )
    for key, value in (runtime_evidence or {}).items():
        committed[key] = copy.deepcopy(value)
    committed["_worker_committed_message"] = committed_identity_record(
        worker_stable_id=stable_id,
        commit_basis=commit_basis,
        observation_id=str(committed.get("observation_id") or ""),
        sender_role=str(committed.get("sender_role") or ""),
        message_type=str(committed.get("message_type") or ""),
        proof=proof,
    )
    return committed


def _production_worker_payload_for_test(
    *,
    binding: dict,
    remark_code: str,
    read_run_id: str,
    observations: list[dict],
    read_reason: str = "waiting_sales_reply",
    historical_count: int = 0,
) -> dict:
    """Serialize one frame through the formal Worker production builder."""

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        if conversation is not None:
            if read_reason in {"waiting_user_reply", "recent_ai_sent"}:
                conversation.status = "waiting_user_reply"
            elif read_reason == "visible_unread":
                conversation.status = "ai_active"
            else:
                conversation.status = "waiting_sales_reply"
            db.commit()
    target = WorkerWechatReadTarget(
        conversation_id=binding["conversation_id"],
        remark_code=remark_code,
        rpa_session_key=binding["rpa_session_key"],
        display_name=f"合同测试-{remark_code}",
        read_reason=read_reason,
        authorization_revision=_binding_authorization_revision(binding["id"]),
        raw={"authorization_read_reason": read_reason},
    )
    slot_states = []
    for index, observation in enumerate(observations):
        stable_id = str(observation.get("_worker_stable_id") or "")
        slot_states.append(
            {
                "observation_id": str(
                    observation.get("observation_id") or ""
                ),
                "screen_order": index + 1,
                "order_source": "observation_index_fallback",
                "row_kind": str(observation.get("row_kind") or ""),
                "source_message_key": worker_source_message_key(
                    target,
                    identity_kind="worker_sequence",
                    identity=stable_id,
                ),
                "origin_read_run_id": (
                    "historical-read" if index < historical_count else read_run_id
                ),
                "fact_scope": (
                    "historical" if index < historical_count else "current_read_run"
                ),
                "delivery_state": (
                    "backend_confirmed"
                    if index < historical_count
                    else "not_enqueued"
                ),
                "item_state": "completed",
            }
        )
    return build_worker_message_ingest_payload(
        target,
        {
            "ok": True,
            **_v3_contract_fields(),
            "authoritative_frame_source": "final_read",
            "observations": copy.deepcopy(observations),
            "slot_ledger_states": slot_states,
            "sequence_alignment_evidence": {
                "pre_sequence_source": (
                    "checkpoint"
                    if historical_count
                    else "empty_checkpoint"
                ),
                "pre_frame_id": (
                    f"checkpoint:historical:{binding['conversation_id']}"
                    if historical_count
                    else f"checkpoint:none:{binding['conversation_id']}"
                ),
                "post_frame_id": f"frame:{read_run_id}",
                "alignment_status": "unique" if historical_count else "not_required",
                "candidate_alignment_count": 1 if historical_count else 0,
                "matched_pairs": [
                    {
                        "identity_state": "committed",
                        "worker_stable_id": str(
                            observations[index].get(
                                "_worker_stable_id"
                            )
                            or ""
                        ),
                        "pre_observation_id": str(
                            observations[index].get("observation_id") or ""
                        ),
                        "post_observation_id": str(
                            observations[index].get("observation_id") or ""
                        ),
                        "pre_index": index,
                        "post_index": index,
                        "match_basis": "worker_business_viewport_continuity",
                    }
                    for index in range(historical_count)
                ],
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [
                    str(observation.get("observation_id") or "")
                    for observation in observations[historical_count:]
                ],
            },
        },
        read_run_id=read_run_id,
    )


def _authorize_fact_settlement(
    worker: dict,
    binding: dict,
    *,
    transaction_id: str,
    source_keys: list[str],
    action_kind: str = "image",
) -> dict:
    digest = hashlib.sha256(
        "\n".join(sorted(source_keys)).encode("utf-8")
    ).hexdigest()
    response = client.get(
        (
            f"/api/workers/{worker['id']}/wechat/conversations/"
            f"{binding['conversation_id']}/read-authorization"
        ),
        params={
            "recovery_transaction_id": transaction_id,
            "action_kind": action_kind,
            "source_message_key_digest": digest,
            "original_authorization_revision": (
                _binding_authorization_revision(binding["id"])
            ),
        },
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200, response.text
    authorization = response.json()["data"]
    assert authorization["recovery_decision"] == "settle_without_ui"
    return authorization


def test_authoritative_frame_source_accepts_only_contract_values():
    base = {
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(
            c2_contract_v3()["observation_schema_version"]
        ),
        "observations": [],
        "authorization_read_reason": "waiting_sales_reply",
        "finished_at": utcnow().isoformat(),
        "flow_gate_errors": [],
        "flow_gate_details": [],
        "slot_ledger_states": [],
    }
    for source in (
        "initial_read",
        "final_read",
        "action_journal_recovery",
    ):
        evidence = WechatMessageEvidence.model_validate(
            {**base, "authoritative_frame_source": source}
        )
        assert evidence.authoritative_frame_source == source

    with pytest.raises(ValueError):
        WechatMessageEvidence.model_validate(
            {**base, "authoritative_frame_source": "voice_execute_final"}
        )
    with pytest.raises(ValueError, match="ActionJournal"):
        WechatMessageEvidence.model_validate(
            {
                **base,
                "authoritative_frame_source": (
                    "action_journal_recovery"
                ),
                "ui_frame_invalidated": True,
            }
        )


def test_voice_action_evidence_uses_actual_receipt_not_tracking_geometry():
    base = {
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(
            c2_contract_v3()["observation_schema_version"]
        ),
        "authoritative_frame_source": "final_read",
        "ui_frame_invalidated": True,
        "observations": [],
        "authorization_read_reason": "waiting_user_reply",
        "finished_at": utcnow().isoformat(),
        "flow_gate_errors": [],
        "flow_gate_details": [],
        "slot_ledger_states": [],
        "voice_transcription": _voice_action_evidence(),
    }

    validated = WechatMessageEvidence.model_validate(base)
    assert validated.voice_transcription is not None
    assert validated.voice_transcription.transcript_binding_status == (
        "confirmed"
    )

    two_frame = copy.deepcopy(base)
    two_frame["voice_transcription"]["tracking_frame_ids"] = [
        "frame:voice:pre",
        "frame:voice:final",
    ]
    two_frame["voice_transcription"]["tracking_edges"] = [
        {
            **two_frame["voice_transcription"]["tracking_edges"][0],
            "to_frame_id": "frame:voice:final",
            "to_observation_id": "voice:final",
        }
    ]
    # Tracking frames are diagnostic evidence only.  They must not become a
    # second cross-frame identity gate once the actual Sidecar action receipt
    # uniquely binds the result.
    validated_two_frame = WechatMessageEvidence.model_validate(two_frame)
    assert validated_two_frame.voice_transcription is not None
    assert validated_two_frame.voice_transcription.action_result_receipt

    broken_chain = copy.deepcopy(base)
    broken_chain["voice_transcription"]["tracking_edges"][1][
        "from_observation_id"
    ] = "voice:another"
    validated_broken_chain = WechatMessageEvidence.model_validate(
        broken_chain
    )
    assert validated_broken_chain.voice_transcription is not None
    assert validated_broken_chain.voice_transcription.action_result_receipt

    ambiguous_bound = copy.deepcopy(base)
    ambiguous_bound["voice_transcription"].update(
        {
            "action_phase": "quarantined",
            "transcript_binding_status": "ambiguous",
            "transcript_binding_method": "none",
            "binding_candidate_count": 0,
            "tracking_frame_ids": [],
            "tracking_edges": [],
            "action_result_receipt": None,
        }
    )
    ambiguous_bound["voice_transcription"]["confirmed_action_mapping"].update(
        {
            "binding_confirmed": False,
            "post_observation_id": "voice:still-bound",
        }
    )
    with pytest.raises(ValueError, match="歧义语音不得绑定"):
        WechatMessageEvidence.model_validate(ambiguous_bound)



def test_authoritative_frame_source_rejects_current_media_action_on_initial_read():
    source_key = "source-current-image"
    read_run_id = "read-current-image-action"
    base_request = {
        "contract_version": 3,
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(
            c2_contract_v3()["observation_schema_version"]
        ),
        "read_run_id": read_run_id,
        "conversation_id": "11111111-1111-1111-1111-111111111111",
        "remark_code": "CJFRAME1",
        "authorization_revision": "revision-frame-source",
        "unread_generation": 0,
        "messages": [
            {
                "dedupe_key": "dedupe-current-image",
                "source_message_key": source_key,
                "sender_role_hint": "customer",
                "message_type": "image",
                "content": None,
                "item_state": "failed",
                "flow_state": "failed",
                "message_position": {
                    "screen_order": 1,
                    "frame_source": "initial_read",
                    "order_source": "observation_index_fallback",
                },
                "raw_payload": {},
            }
        ],
        "evidence": {
            "contract_revision": contract_revision(),
            "contract_sha256": contract_sha256(),
            "observation_schema_version": int(
                c2_contract_v3()["observation_schema_version"]
            ),
            "authoritative_frame_source": "initial_read",
            "observations": [
                {
                    "observation_id": "image-current-action",
                    "row_kind": "image_bubble",
                    "action_phase": "trigger_attempted",
                    "source_message": {
                        "source_message_key": source_key,
                    },
                }
            ],
            "authorization_read_reason": "waiting_user_reply",
            "finished_at": utcnow().isoformat(),
            "flow_gate_errors": [],
            "flow_gate_details": [],
            "slot_ledger_states": [
                {
                    "observation_id": "image-current-action",
                    "screen_order": 1,
                    "order_source": "observation_index_fallback",
                    "row_kind": "image_bubble",
                    "source_message_key": source_key,
                    "origin_read_run_id": read_run_id,
                    "fact_scope": "current_read_run",
                    "delivery_state": "not_enqueued",
                    "item_state": "failed",
                }
            ],
            "sequence_alignment_evidence": {
                "pre_sequence_source": "empty_checkpoint",
                "pre_frame_id": "checkpoint:none:frame-source",
                "post_frame_id": "frame:current-image-action",
                "alignment_status": "not_required",
                "candidate_alignment_count": 0,
                "matched_pairs": [],
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [
                    "image-current-action"
                ],
            },
        },
    }

    with pytest.raises(ValueError, match="initial_read"):
        WechatMessageIngestRequest.model_validate(base_request)

    marker_request = copy.deepcopy(base_request)
    marker_request["evidence"]["observations"][0][
        "action_phase"
    ] = "not_attempted"
    marker_request["evidence"]["ui_frame_invalidated"] = True
    with pytest.raises(ValueError, match="initial_read"):
        WechatMessageIngestRequest.model_validate(marker_request)

    voice_request = copy.deepcopy(base_request)
    voice_request["messages"][0].update(
        {
            "message_type": "voice",
            "content": "已转写语音",
            "item_state": "completed",
            "flow_state": "completed",
        }
    )
    voice_request["evidence"]["observations"][0].update(
        {
            "row_kind": "voice_transcript",
            "action_phase": "confirmed",
        }
    )
    voice_request["evidence"]["slot_ledger_states"][0].update(
        {
            "row_kind": "voice_transcript",
            "item_state": "completed",
        }
    )
    voice_request["evidence"]["voice_transcription"] = (
        _voice_action_evidence()
    )
    with pytest.raises(ValueError, match="initial_read"):
        WechatMessageIngestRequest.model_validate(voice_request)

    voice_scrolled_out_request = copy.deepcopy(base_request)
    voice_scrolled_out_request["messages"][0].update(
        {
            "message_type": "text",
            "content": "语音操作后仍可见的文字",
            "item_state": "completed",
            "flow_state": "completed",
        }
    )
    voice_scrolled_out_request["evidence"]["observations"][0].update(
        {
            "row_kind": "text_bubble",
            "action_phase": "not_attempted",
        }
    )
    voice_scrolled_out_request["evidence"]["slot_ledger_states"][0].update(
        {
            "row_kind": "text_bubble",
            "item_state": "completed",
        }
    )
    voice_scrolled_out_request["evidence"]["voice_transcription"] = (
        _voice_action_evidence()
    )
    with pytest.raises(ValueError, match="initial_read"):
        WechatMessageIngestRequest.model_validate(
            voice_scrolled_out_request
        )

    voice_scrolled_out_final = copy.deepcopy(
        voice_scrolled_out_request
    )
    voice_scrolled_out_final["evidence"][
        "authoritative_frame_source"
    ] = "final_read"
    voice_scrolled_out_final["messages"][0]["message_position"][
        "frame_source"
    ] = "final_read"
    parsed = WechatMessageIngestRequest.model_validate(
        voice_scrolled_out_final
    )
    assert parsed.evidence.authoritative_frame_source == "final_read"

    final_request = copy.deepcopy(base_request)
    final_request["evidence"]["authoritative_frame_source"] = "final_read"
    final_request["evidence"]["ui_frame_invalidated"] = True
    final_request["messages"][0]["message_position"][
        "frame_source"
    ] = "final_read"
    parsed = WechatMessageIngestRequest.model_validate(final_request)
    assert parsed.evidence.authoritative_frame_source == "final_read"

    historical_request = copy.deepcopy(base_request)
    historical_request["messages"] = []
    historical_request["evidence"]["slot_ledger_states"][0].update(
        {
            "origin_read_run_id": "read-older-image-action",
            "fact_scope": "historical",
            "delivery_state": "backend_confirmed",
        }
    )
    parsed = WechatMessageIngestRequest.model_validate(
        historical_request
    )
    assert parsed.evidence.authoritative_frame_source == "initial_read"


def _fact_settlement_payload(
    binding: dict,
    remark_code: str,
    *,
    transaction_id: str,
    source_keys: list[str],
    settlement_mode: str,
    messages: list[dict],
    action_kind: str = "image",
) -> dict:
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"recovery:{transaction_id}",
        messages=messages,
    )
    for message in payload["messages"]:
        message["message_position"]["frame_source"] = (
            "action_journal_recovery"
        )
    evidence = payload["evidence"]
    evidence.update(
        {
            "authoritative_frame_source": "action_journal_recovery",
            "read_reason": "fact_settlement",
            "authorization_read_reason": "fact_settlement",
            "recovery_transaction_id": transaction_id,
            "action_kind": action_kind,
            "source_message_key_digest": hashlib.sha256(
                "\n".join(sorted(source_keys)).encode("utf-8")
            ).hexdigest(),
            "settlement_mode": settlement_mode,
            "settlement_source_message_keys": sorted(source_keys),
            "recovery_requires_per_message_confirmation": True,
            "wechat_reopened": False,
            "clipboard_repeated": False,
            "vision_repeated": False,
        }
    )
    payload["authorization_scope"] = "fact_settlement"
    return payload


def _simulate_worker_incremental_filter(
    payload: dict,
    *,
    keep_source_keys: set[str],
) -> dict:
    all_messages = [
        item
        for item in payload.get("messages") or []
        if isinstance(item, dict)
    ]
    payload["evidence"]["slot_ledger_states"] = [
        {
            "observation_id": str(
                ((item.get("raw_payload") or {}).get("observation") or {}).get(
                    "observation_id"
                )
                or ""
            ),
            "screen_order": int(
                (item.get("message_position") or {}).get("screen_order") or 0
            ),
            "order_source": str(
                (item.get("message_position") or {}).get("order_source") or ""
            ),
            "row_kind": str(
                ((item.get("raw_payload") or {}).get("observation") or {}).get(
                    "row_kind"
                )
                or ""
            ),
            "source_message_key": str(item.get("source_message_key") or ""),
            "origin_read_run_id": (
                str(payload.get("read_run_id") or "")
                if str(item.get("source_message_key") or "") in keep_source_keys
                else "read-historical-filter-fixture"
            ),
            "fact_scope": (
                "current_read_run"
                if str(item.get("source_message_key") or "") in keep_source_keys
                else "historical"
            ),
            "delivery_state": (
                "not_enqueued"
                if str(item.get("source_message_key") or "") in keep_source_keys
                else "backend_confirmed"
            ),
            "item_state": str(item.get("item_state") or "completed"),
            "ledger_state": (
                "NEW_MESSAGE"
                if str(item.get("source_message_key") or "") in keep_source_keys
                else "OLD_COMPLETED"
            ),
        }
        for item in all_messages
    ]
    payload["messages"] = [
        item
        for item in all_messages
        if str(item.get("source_message_key") or "") in keep_source_keys
    ]
    # Production filters only the incremental delivery set.  The complete
    # authoritative frame and its slot ledger remain intact so handoff order,
    # checkpoint construction and later pre-send alignment all see the same
    # text/voice/image sequence.
    return payload


def _seed_open_handoff(
    binding: dict,
    *,
    paused: bool,
    trigger_source_key: str | None = None,
    trigger_content: str = "触发人工接管的客户消息",
    reason_code: str | None = None,
) -> tuple[str, str]:
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation.status = "waiting_sales_reply"
        conversation.ai_enabled = not paused
        trigger_message = None
        if trigger_source_key:
            trigger_message = MessageEvent(
                conversation_id=binding["conversation_id"],
                binding_id=binding["id"],
                lead_id=binding_row.lead_id,
                sales_id=binding_row.sales_id,
                worker_id=binding_row.worker_id,
                rpa_session_key=binding_row.rpa_session_key or "wx:rpa:v1:test-handoff",
                read_run_id=f"seed-handoff-{trigger_source_key}",
                contract_version=3,
                source_message_key=trigger_source_key,
                dedupe_key=trigger_source_key,
                sender_role="customer",
                message_type="text",
                content=trigger_content,
                raw_payload={},
                evidence={},
                item_state="completed",
                flow_state="completed",
                observation_order=1,
                observed_at=utcnow(),
            )
            db.add(trigger_message)
            db.flush()
        resolved_reason_code = str(
            reason_code
            or (
                "AI_ENGINE_PAUSED_FOR_MANUAL_REVIEW"
                if paused
                else "HANDOFF_REQUIRED"
            )
        )
        batch = MessageBatch(
            conversation_id=binding["conversation_id"],
            status="paused" if paused else "handoff_created",
            active=False,
            trigger_type="customer_message",
            trigger_key=f"handoff-seed-{'pause' if paused else 'manual'}",
            trigger_message_event_id=trigger_message.id if trigger_message else None,
            message_event_ids=[trigger_message.id] if trigger_message else [],
            message_count=1 if trigger_message else 0,
            decision="pause" if paused else "handoff",
            error_code=resolved_reason_code,
            suggested_action="sales_handoff" if paused else "handoff",
        )
        db.add(batch)
        db.flush()
        handoff = HandoffEvent(
            conversation_id=binding["conversation_id"],
            batch_id=batch.id,
            status="created",
            handoff_reason_code=resolved_reason_code,
            reason_detail=resolved_reason_code,
            trigger_message_event_ids=[trigger_message.id] if trigger_message else [],
        )
        db.add(handoff)
        db.commit()
        return batch.id, handoff.id


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _create_worker() -> dict:
    response = client.post(
        "/api/workers",
        json={"worker_name": "Windows Worker", "device_name": "Windows PC", "platform": "windows", "enabled": True},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    worker = response.json()["data"]
    bind = client.post(
        f"/api/workers/{worker['id']}/client-bind",
        json={"worker_token": worker["worker_token"], "client_instance_id": "client-a"},
    )
    assert bind.status_code == 200
    heartbeat = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={
            "client_instance_id": "client-a",
            "run_status": "running",
            "rpa_component_status": "ready",
            "wechat_status": "logged_in",
            "running_status": "idle",
            "current_step": "wechat_scan_idle",
            "local_lock_summary": {"locked": False, "owner": None},
        },
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["data"]["current_step"] == "wechat_scan_idle"
    return worker


def _worker_headers(worker: dict) -> dict:
    return {"X-Worker-Token": worker["worker_token"], "X-Client-Instance-Id": "client-a"}


def _create_sales(worker_id: str) -> str:
    response = client.post(
        "/api/sales",
        json={"sales_name": "张伟", "phone": "13900000001", "enabled": True, "sort_order": 10, "worker_id": worker_id},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _create_lead(name: str, phone: str, custom_fields: dict | None = None) -> dict:
    response = client.post(
        "/api/leads",
        json={"customer_name": name, "phones": [phone], "remark": "预算 10 万", "custom_fields": custom_fields or {}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    return response.json()["data"]


def _first_task() -> dict:
    response = client.get("/api/tasks", headers=HEADERS)
    assert response.status_code == 200
    return response.json()["data"]["items"][0]


def _pull_remark_code(worker: dict) -> str:
    pull = client.get(f"/api/workers/{worker['id']}/tasks/pull", headers=_worker_headers(worker))
    assert pull.status_code == 200
    return pull.json()["data"]["task"]["remark_code"]


def _complete_add_friend_task_result(worker: dict, result_code: str) -> Task:
    with SessionLocal() as db:
        task = (
            db.query(Task)
            .filter(
                Task.worker_id == worker["id"],
                Task.task_type == "add_friend",
            )
            .order_by(Task.created_at.desc())
            .first()
        )
        assert task is not None
        task.status = "completed"
        task.result_code = result_code
        task.completed_at = utcnow()
        db.commit()
        db.refresh(task)
        db.expunge(task)
        return task


def _scan_payload(remark_code: str | None, *, rpa_session_key: str = "wx-row-1") -> dict:
    candidates = [remark_code] if remark_code else []
    return {
        "scan_id": "scan-001",
        "sidecar_run_id": "sidecar-001",
        "wechat_account_hint": "wx-main",
        "started_at": "2026-06-22T10:00:00+08:00",
        "finished_at": "2026-06-22T10:00:02+08:00",
        "sessions": [
            {
                "rpa_session_key": rpa_session_key,
                "display_name": remark_code or "未知客户",
                "remark_code_candidates": candidates,
                "row_fingerprint": f"fingerprint-{rpa_session_key}",
                "unread_hint": True,
                "last_message_preview": "你好",
                "ocr_confidence": 0.98,
            }
        ],
        "evidence": {"screenshot": "local://scan.png"},
    }


def test_scan_result_binds_unique_remark_code_and_authorizes_visible_unread():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200
    data = scan.json()["data"]
    assert data["next_action"] == "none"
    assert data["bound_count"] == 1
    binding = data["bindings"][0]
    assert binding["bind_status"] == "bound"
    assert binding["listen_status"] == "listening"
    assert binding["can_ingest_messages"] is True

    targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert targets.status_code == 200
    assert targets.json()["data"]["next_action"] == "none"
    visible_target = targets.json()["data"]["targets"][0]
    assert visible_target["conversation_id"] == binding["conversation_id"]
    assert visible_target["remark_code"] == remark_code
    assert visible_target["read_reason"] == "visible_unread"
    assert visible_target["authorization_revision"]
    worker_target = WorkerWechatReadTarget.from_api(visible_target)
    assert worker_target.read_reason == "visible_unread"
    assert worker_target.authorization_revision == visible_target["authorization_revision"]

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        sent_action = ReplyAction(
            batch_id="batch-recent-ai-boundary",
            conversation_id=binding["conversation_id"],
            status="sent",
            decision="send_reply",
            reply_text="您好，我在的。",
            reply_text_hash=hashlib.sha256(
                "您好，我在的。".encode("utf-8")
            ).hexdigest(),
            sent_at=conversation.last_ai_reply_at,
        )
        db.add(sent_action)
        db.flush()
        sent_action_id = sent_action.id
        sent_action_hash = sent_action.reply_text_hash
        sent_action_time = sent_action.sent_at.isoformat()
        db.add(
            MessageEvent(
                conversation_id=binding["conversation_id"],
                binding_id=binding["id"],
                lead_id=binding["lead_id"],
                sales_id=binding["sales_id"],
                worker_id=worker["id"],
                rpa_session_key="wx-row-1",
                read_run_id="legacy-read-run",
                contract_version=3,
                source_message_key="legacy-source-key",
                dedupe_key="legacy-dedupe-key",
                sender_role="customer",
                message_type="text",
                content="历史消息",
                raw_payload={
                    "dedupe_basis": {
                        "source": "ocr_structural_identity",
                    }
                },
                evidence={},
                item_state="completed",
                flow_state="completed",
            )
        )
        db.commit()

    state_targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert state_targets.status_code == 200
    read_target = state_targets.json()["data"]["targets"][0]
    assert read_target["conversation_id"] == binding["conversation_id"]
    assert read_target["lead_id"] == binding["lead_id"]
    assert read_target["remark_code"] == remark_code
    assert read_target["rpa_session_key"] == "wx-row-1"
    assert read_target["display_name"] == remark_code
    assert "last_ingested_at" in read_target
    assert read_target["read_reason"] == "recent_ai_sent"
    boundary = read_target["ai_reply_boundary"]
    assert {
        key: value
        for key, value in boundary.items()
        if key != "sent_at"
    } == {
        "reply_action_id": sent_action_id,
        "reply_text_hash": sent_action_hash,
        "worker_stable_id": "",
    }
    actual_sent_at = datetime.fromisoformat(boundary["sent_at"])
    expected_sent_at = datetime.fromisoformat(sent_action_time)
    if actual_sent_at.tzinfo is None:
        actual_sent_at = actual_sent_at.replace(tzinfo=timezone.utc)
    if expected_sent_at.tzinfo is None:
        expected_sent_at = expected_sent_at.replace(tzinfo=timezone.utc)
    assert actual_sent_at == expected_sent_at
    assert read_target["row_fingerprint"] == "fingerprint-wx-row-1"
    assert read_target["ocr_confidence"] == 0.98
    assert len(read_target["authorization_revision"]) == 32
    assert "identity_transition" not in read_target
    recent_messages = read_target["identity_checkpoint"]["recent_messages"]
    assert len(recent_messages) == 1
    checkpoint_message = recent_messages[0]
    assert checkpoint_message["stable_id"] == ""
    assert checkpoint_message["source_message_key"] == "legacy-source-key"
    assert checkpoint_message["origin_read_run_id"] == "legacy-read-run"
    assert checkpoint_message["dedupe_key"] == "legacy-dedupe-key"
    assert checkpoint_message["sender_role"] == "customer"
    assert checkpoint_message["message_type"] == "text"
    assert checkpoint_message["normalized_content_hash"]
    assert checkpoint_message["media_identity_hash"] == ""
    assert checkpoint_message["alignment_signature"]
    assert checkpoint_message["native_source_message_id"] == ""
    assert checkpoint_message["frame_visual_id"] == ""

    admin_binding = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS)
    assert admin_binding.status_code == 200
    assert admin_binding.json()["data"]["remark_code"] == remark_code


def test_visible_unread_successful_ingest_consumes_current_scan_fact():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("首次未读客户", "13896676681")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    empty_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-visible-unread-complete",
        messages=[],
        read_reason="visible_unread",
    )

    # This assertion crosses the Worker/backend boundary: the Worker must not
    # skip an authorized empty read before the backend can consume the unread
    # fact.
    assert should_submit_c2_ingest_payload(
        read_reason="visible_unread",
        messages=empty_payload["messages"],
        has_flow_gate=False,
    ) is True

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=empty_payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["state_transition_applied"] is True
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        # unread_hint remains the latest physical screen observation. Logical
        # consumption is tracked independently by the generation counters.
        assert binding_row.unread_hint is True
        assert binding_row.unread_generation == 1
        assert binding_row.consumed_unread_generation == 1
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation is not None
        assert conversation.status == "ai_active"

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert targets.status_code == 200
    assert all(
        item["read_reason"] != "visible_unread"
        for item in targets.json()["data"]["targets"]
    )


def test_visible_unread_failed_ingest_preserves_fact_for_retry():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("未读失败重试客户", "13896676682")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    failed_message = _v3_message(
        "visible-unread-read-failed",
        role="customer",
        message_type="text",
        content="这条不应入库",
        screen_order=1,
        raw_extra={"read_result": "target_not_confirmed"},
    )

    failed = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-visible-unread-failed",
            messages=[failed_message],
            read_reason="visible_unread",
        ),
        headers=_worker_headers(worker),
    )

    assert failed.status_code == 409
    assert failed.json()["code"] == "TARGET_NOT_CONFIRMED"
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        assert binding_row.unread_hint is True
        assert db.query(MessageEvent).count() == 0

    retry_targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert retry_targets.status_code == 200
    assert retry_targets.json()["data"]["targets"][0]["read_reason"] == "visible_unread"


def test_visible_unread_false_scan_revokes_temporary_read_reason():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("未读撤销客户", "13896676683")
    remark_code = _pull_remark_code(worker)
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200

    cleared_payload = _scan_payload(remark_code)
    cleared_payload["scan_id"] = "scan-visible-unread-cleared"
    cleared_payload["sessions"][0]["unread_hint"] = False
    cleared = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=cleared_payload,
        headers=_worker_headers(worker),
    )
    assert cleared.status_code == 200

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert targets.status_code == 200
    assert targets.json()["data"]["targets"] == []


def test_scan_id_changes_cannot_restore_consumed_visible_unread_without_new_semantic_evidence():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("未读去重客户", "13896676684")
    remark_code = _pull_remark_code(worker)
    first_payload = _scan_payload(remark_code)
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=first_payload,
        headers=_worker_headers(worker),
    )
    binding = first.json()["data"]["bindings"][0]
    original_revision = _binding_authorization_revision(binding["id"])
    consumed = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-visible-unread-before-duplicate-scan",
            messages=[],
            read_reason="visible_unread",
        ),
        headers=_worker_headers(worker),
    )
    assert consumed.status_code == 200

    duplicate = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=first_payload,
        headers=_worker_headers(worker),
    )
    assert duplicate.status_code == 200
    assert _binding_authorization_revision(binding["id"]) == original_revision
    after_duplicate = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert after_duplicate.json()["data"]["targets"] == []

    new_scan_payload = _scan_payload(remark_code)
    new_scan_payload["scan_id"] = "scan-visible-unread-new-fact"
    new_scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=new_scan_payload,
        headers=_worker_headers(worker),
    )
    assert new_scan.status_code == 200
    assert _binding_authorization_revision(binding["id"]) == original_revision
    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert targets.json()["data"]["targets"] == []


def test_read_targets_uses_empty_identity_checkpoint_without_legacy_transition():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("无历史身份客户", "13896676679")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        db.commit()

    response = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    target = response.json()["data"]["targets"][0]
    assert "identity_transition" not in target
    assert target["identity_checkpoint"]["recent_messages"] == []


def test_read_targets_fairly_rotates_more_than_twenty_eligible_conversations():
    worker = _create_worker()
    with SessionLocal() as db:
        for index in range(25):
            conversation_id = f"fair-conversation-{index:02d}"
            db.add(
                WechatSessionBinding(
                    conversation_id=conversation_id,
                    worker_id=worker["id"],
                    remark_code=f"CJFAIR{index:02d}",
                    display_name=f"CJFAIR{index:02d}",
                    rpa_session_key=f"fair-session-{index:02d}",
                    row_fingerprint=f"fair-row-{index:02d}",
                    bind_status="bound",
                    listen_status="listening",
                    allow_listening=True,
                )
            )
            db.add(
                Conversation(
                    conversation_id=conversation_id,
                    worker_id=worker["id"],
                    status="waiting_user_reply",
                )
            )
        db.commit()

    first = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets?limit=20",
        headers=_worker_headers(worker),
    )
    second = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets?limit=20",
        headers=_worker_headers(worker),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_ids = {
        item["conversation_id"]
        for item in first.json()["data"]["targets"]
    }
    second_ids = {
        item["conversation_id"]
        for item in second.json()["data"]["targets"]
    }
    assert len(first_ids) == 20
    assert len(second_ids) == 20
    assert len(first_ids | second_ids) == 25
    assert second_ids - first_ids


def test_authorization_revision_ignores_scan_refresh_and_changes_with_binding_permission():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding_id = first.json()["data"]["bindings"][0]["id"]
    with SessionLocal() as db:
        initial_revision = db.get(WechatSessionBinding, binding_id).authorization_revision

    refresh_payload = _scan_payload(remark_code)
    refresh_payload["scan_id"] = "scan-authorization-refresh"
    refreshed = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=refresh_payload,
        headers=_worker_headers(worker),
    )
    assert refreshed.status_code == 200
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding, binding_id).authorization_revision == initial_revision

    revoked_payload = _scan_payload(None)
    revoked_payload["scan_id"] = "scan-authorization-revoked"
    revoked = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=revoked_payload,
        headers=_worker_headers(worker),
    )
    assert revoked.status_code == 200
    with SessionLocal() as db:
        assert db.get(WechatSessionBinding, binding_id).authorization_revision > initial_revision


def test_scan_result_blocks_same_remark_code_claimed_by_multiple_sessions():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("虾丸子大", "13896676678", {"remark_code": "CJR8S5K3"})
    payload = _scan_payload("CJR8S5K3", rpa_session_key="wx-target")
    payload["sessions"][0]["display_name"] = "CJR8S5K3 虾丸子大"
    payload["sessions"].append(
        {
            "rpa_session_key": "wx-other-chat",
            "display_name": "聿安的家",
            "remark_code_candidates": ["CJR8S5K3"],
            "row_fingerprint": "fingerprint-other-chat",
            "unread_hint": False,
            "last_message_preview": "CJR8S5K3虾丸子大人：蛹者",
            "ocr_confidence": 0.99,
        }
    )

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert scan.status_code == 200
    data = scan.json()["data"]
    assert data["bound_count"] == 0
    assert data["needs_review_count"] == 2
    assert len({item["id"] for item in data["bindings"]}) == 2
    assert {item["display_name"] for item in data["bindings"]} == {"CJR8S5K3 虾丸子大", "聿安的家"}
    assert all(item["bind_status"] == "needs_review" for item in data["bindings"])
    assert all(item["error_code"] == "SESSION_REMARK_CODE_MULTIPLE_SESSIONS" for item in data["bindings"])
    assert all(item["can_ingest_messages"] is False for item in data["bindings"])

    targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert targets.status_code == 200
    assert targets.json()["data"]["targets"] == []


def test_scan_result_ignores_group_excluded_remark_candidate_when_private_chat_matches():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("虾丸子大", "13896676678", {"remark_code": "CJR8S5K3"})
    payload = _scan_payload("CJR8S5K3", rpa_session_key="wx-private-chat")
    payload["sessions"][0]["display_name"] = "虾丸子大-CJR8S5K3"
    payload["sessions"].append(
        {
            "rpa_session_key": "wx-group-chat",
            "display_name": "销售讨论-CJR8S5K3(5)",
            # OmniAuto/Worker already classified this title as group, so it is
            # intentionally excluded from the backend's short-code candidates.
            "remark_code_candidates": [],
            "row_fingerprint": "fingerprint-group-chat",
            "unread_hint": True,
            "last_message_preview": "群聊消息",
            "ocr_confidence": 0.99,
        }
    )

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert scan.status_code == 200
    data = scan.json()["data"]
    assert data["bound_count"] == 1
    private_binding = next(item for item in data["bindings"] if item["rpa_session_key"] == "wx-private-chat")
    group_binding = next(item for item in data["bindings"] if item["rpa_session_key"] == "wx-group-chat")
    assert private_binding["bind_status"] == "bound"
    assert private_binding["remark_code"] == "CJR8S5K3"
    assert group_binding["bind_status"] != "bound"
    assert group_binding["can_ingest_messages"] is False


def test_scan_result_does_not_reuse_soft_deleted_remark_binding():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("虾丸子大", "13896676678", {"remark_code": "CJR8S5K3"})
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload("CJR8S5K3", rpa_session_key="wx-old"),
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200
    deleted_binding_id = first.json()["data"]["bindings"][0]["id"]

    with SessionLocal() as db:
        deleted_binding = db.get(WechatSessionBinding, deleted_binding_id)
        assert deleted_binding is not None
        deleted_binding.deleted_at = utcnow()
        deleted_binding.bind_status = "disabled"
        deleted_binding.listen_status = "disabled"
        deleted_binding.allow_listening = False
        deleted_binding.rpa_session_key = f"wx-old#retired#{deleted_binding.id}"
        db.commit()

    payload = _scan_payload("CJR8S5K3", rpa_session_key="wx-new")
    payload["scan_id"] = "scan-after-soft-delete"
    rescan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert rescan.status_code == 200
    binding = rescan.json()["data"]["bindings"][0]
    assert binding["id"] != deleted_binding_id
    assert binding["rpa_session_key"] == "wx-new"
    assert binding["bind_status"] == "bound"
    assert binding["can_ingest_messages"] is True


def test_scan_result_with_conflicting_remark_code_goes_needs_review_and_has_no_read_target():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678", {"remark_code": "CJ-CONFLICT"})
    _create_lead("李女士", "13896676679", {"remark_code": "CJ-CONFLICT"})

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload("CJ-CONFLICT"),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200
    binding = scan.json()["data"]["bindings"][0]
    assert binding["bind_status"] == "needs_review"
    assert binding["error_code"] == "SESSION_REMARK_CODE_DUPLICATED"
    assert "reason_code" not in binding
    assert binding["can_ingest_messages"] is False

    targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert targets.json()["data"]["targets"] == []


def test_scan_result_without_remark_code_stays_unbound_and_message_ingest_is_rejected():
    worker = _create_worker()
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(None),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200
    binding = scan.json()["data"]["bindings"][0]
    assert binding["bind_status"] == "unbound"
    assert binding["error_code"] == "SESSION_REMARK_CODE_NOT_FOUND"
    assert "reason_code" not in binding

    ingest = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            "CJ000000",
            read_run_id="read-001",
            messages=[_v3_message("msg-001", role="customer", message_type="text", content="你好", screen_order=1)],
        ),
        headers=_worker_headers(worker),
    )
    assert ingest.status_code == 409
    assert ingest.json()["code"] == "MESSAGE_CONVERSATION_NOT_BOUND"
    assert ingest.json()["trace_id"]


def test_message_ingest_is_idempotent_by_dedupe_key_and_returns_next_action_none():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]
    conversation_id = binding["conversation_id"]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-001",
        messages=[_v3_message("msg-001", role="customer", message_type="text", content="你好", screen_order=1)],
    )
    payload["evidence"]["screenshot"] = "local://message.png"
    first = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    assert first.json()["data"]["ingested_count"] == 1
    assert first.json()["data"]["results"][0]["source_message_key"] == "msg-001"
    assert first.json()["data"]["next_action"] == "none"
    assert first.json()["data"]["message_batch"]["batch_id"]

    duplicated = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload, headers=_worker_headers(worker))
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["ingested_count"] == 0
    assert duplicated.json()["data"]["duplicated_count"] == 1
    assert duplicated.json()["data"]["results"][0]["error_code"] == "MESSAGE_INGEST_DUPLICATED"
    assert duplicated.json()["data"]["results"][0]["ingest_result"] == "duplicated"
    assert duplicated.json()["data"]["results"][0]["source_message_key"] == "msg-001"
    assert "ingest_status" not in duplicated.json()["data"]["results"][0]
    assert "conversation_status" not in duplicated.json()["data"]
    assert duplicated.json()["data"]["next_action"] == "none"
    assert "message_batch" not in duplicated.json()["data"]
    assert duplicated.json()["trace_id"]

    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=HEADERS)
    assert messages.status_code == 200
    assert len(messages.json()["data"]["items"]) == 1
    with SessionLocal() as db:
        assert db.query(MessageBatch).count() == 1


def test_completed_vision_image_and_text_are_ingested_in_one_customer_batch():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("图片客户", "13896676681")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    conversation_id = binding["conversation_id"]
    image_message = _v3_message(
        "image-observation",
        role="customer",
        message_type="image",
        content="客户发送了一张白色 SUV 图片",
        screen_order=1,
    )
    text_message = _v3_message(
        "text-after-image",
        role="customer",
        message_type="text",
        content="这辆车还有吗？",
        screen_order=2,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-image-observation",
        messages=[image_message, text_message],
    )
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 2
    assert response.json()["data"]["message_batch"]["batch_id"]
    with SessionLocal() as db:
        messages = db.query(MessageEvent).filter(MessageEvent.conversation_id == conversation_id).all()
        assert {item.message_type for item in messages} == {"image", "text"}
        image = next(item for item in messages if item.message_type == "image")
        assert image.raw_payload["customer_image_understanding"]["vision_summary"] == "客户发送了一张白色 SUV 图片"
        assert db.query(MessageBatch).count() == 1


def test_shared_mixed_roundtrip_fixture_crosses_worker_and_backend_without_second_contract():
    fixture_path = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "c2_v3_mixed_roundtrip.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("合同客户", "13896676685")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    _seed_open_handoff(binding, paused=False)
    target = WorkerWechatReadTarget(
        conversation_id=binding["conversation_id"],
        remark_code=remark_code,
        rpa_session_key=binding["rpa_session_key"],
        display_name=f"合同客户-{remark_code}",
        read_reason="waiting_sales_reply",
        authorization_revision=_binding_authorization_revision(binding["id"]),
    )
    read_run_id = "read-shared-mixed-roundtrip"
    stable_ids_by_observation = {
        str(item["observation_id"]): str(item["_worker_stable_id"])
        for item in fixture["omniauto_output"]["observations"]
    }
    for state in fixture["omniauto_output"]["slot_ledger_states"]:
        state["source_message_key"] = worker_source_message_key(
            target,
            identity_kind="worker_sequence",
            identity=stable_ids_by_observation[state["observation_id"]],
        )
    payload = build_worker_message_ingest_payload(
        target,
        fixture["omniauto_output"],
        read_run_id=read_run_id,
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ingested_count"] == 3
    assert [item["source_message_key"] for item in data["results"]] == [
        item["source_message_key"] for item in payload["messages"]
    ]
    assert [item["message_type"] for item in payload["messages"]] == ["text", "voice", "image"]
    assert [item["sender_role_hint"] for item in payload["messages"]] == ["customer", "customer", "self"]
    assert "message_batch" not in data
    with SessionLocal() as db:
        rows = (
            db.query(MessageEvent)
            .filter(MessageEvent.conversation_id == binding["conversation_id"])
            .order_by(MessageEvent.ingested_at.asc())
            .all()
        )
        assert [(item.message_type, item.sender_role) for item in rows] == [
            ("text", "customer"),
            ("voice", "customer"),
            ("image", "self"),
        ]
        assert db.query(MessageBatch).count() == 1


def test_worker_generated_text_voice_image_alignment_crosses_official_route():
    """Exercise the exact nested envelope that failed Windows UAT."""

    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "examples"
        / "c2_v3_mixed_roundtrip.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("混合消息合同客户", "13896676696")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    _seed_open_handoff(binding, paused=False)
    target = WorkerWechatReadTarget(
        conversation_id=binding["conversation_id"],
        remark_code=remark_code,
        rpa_session_key=binding["rpa_session_key"],
        display_name=f"混合消息合同客户-{remark_code}",
        read_reason="waiting_sales_reply",
        authorization_revision=_binding_authorization_revision(binding["id"]),
    )
    read_run_id = "read-worker-generated-mixed-alignment"
    post_observations = copy.deepcopy(
        fixture["omniauto_output"]["observations"]
    )
    pre_observations = copy.deepcopy(post_observations)
    for item in pre_observations:
        item["observation_id"] = "pre-" + str(item["observation_id"])
    continuity = compare_business_viewport_continuity(
        normalized_business_message_sequence(
            pre_observations,
            message_viewport_bounds=None,
        ),
        normalized_business_message_sequence(
            post_observations,
            message_viewport_bounds=None,
        ),
        old_top_boundary_complete=True,
        new_top_boundary_complete=True,
    )
    assert continuity["relation"] == "business_sequence_equal"
    generated_alignment = _continuity_alignment_evidence_for_suffix(
        pre_observations,
        post_observations,
        continuity,
        pre_sequence_source="action_frame",
        pre_frame_id="frame:mixed-before",
        post_frame_id="frame:mixed-after",
    )
    assert len(generated_alignment["matched_pairs"]) == 3
    assert all(
        {
            "identity_state",
            "pre_observation_id",
            "post_observation_id",
            "pre_index",
            "post_index",
            "match_basis",
        }.issubset(pair)
        for pair in generated_alignment["matched_pairs"]
    )

    sidecar = copy.deepcopy(fixture["omniauto_output"])
    sidecar["observations"] = post_observations
    sidecar["sequence_alignment_evidence"] = generated_alignment
    stable_ids = {
        str(item["observation_id"]): str(item["_worker_stable_id"])
        for item in post_observations
    }
    for state in sidecar["slot_ledger_states"]:
        state["origin_read_run_id"] = read_run_id
        state["source_message_key"] = worker_source_message_key(
            target,
            identity_kind="worker_sequence",
            identity=stable_ids[state["observation_id"]],
        )
    payload = build_worker_message_ingest_payload(
        target,
        sidecar,
        read_run_id=read_run_id,
    )
    # Validate with the backend request model before using the real route;
    # both consume the exact Worker-produced payload, not separate fixtures.
    WechatMessageIngestRequest.model_validate(payload)

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert [item["message_type"] for item in payload["messages"]] == [
        "text",
        "voice",
        "image",
    ]
    with SessionLocal() as db:
        rows = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.conversation_id == binding["conversation_id"]
            )
            .order_by(MessageEvent.ingested_at.asc())
            .all()
        )
        assert [item.message_type for item in rows] == [
            "text",
            "voice",
            "image",
        ]


def test_uat_shallow_alignment_is_rejected_atomically_by_official_route():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("旧版浅层对齐合同客户", "13896676697")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-uat-shallow-alignment-route",
        messages=[
            _v3_message(
                "uat-shallow-voice",
                role="customer",
                message_type="voice",
                content="有10万左右的二手车推荐吗？",
                screen_order=1,
            )
        ],
    )
    payload["evidence"]["sequence_alignment_evidence"] = {
        "pre_sequence_source": "worker_business_viewport_continuity",
        "pre_frame_id": "",
        "post_frame_id": "",
        "alignment_status": "unique",
        "candidate_alignment_count": 1,
        "matched_pairs": [{"old_index": 0, "new_index": 0}],
        "old_tail_fully_consumed": True,
        "new_suffix_observation_ids": [],
    }

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["data"]["retryable"] is False
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_same_worker_read_run_uses_distinct_local_outboxes_for_new_voice_and_supersedes_old_batch(
    tmp_path,
):
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "examples"
        / "c2_v3_mixed_roundtrip.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("发送前语音客户", "13896676695")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_sales_reply"
        db.commit()
    read_run_id = "read-local-outbox-multi-fact"

    def target_for(
        authorization_revision: str,
        *,
        continuation: dict | None = None,
    ) -> WorkerWechatReadTarget:
        read_reason = str(
            (continuation or {}).get("read_reason")
            or "waiting_sales_reply"
        )
        return WorkerWechatReadTarget(
            conversation_id=binding["conversation_id"],
            remark_code=remark_code,
            rpa_session_key=binding["rpa_session_key"],
            display_name=f"发送前语音客户-{remark_code}",
            read_reason=read_reason,
            authorization_revision=authorization_revision,
            raw={
                "authorization_read_reason": read_reason,
                "batch_continuation": dict(continuation or {}),
            },
        )

    def worker_payload(
        target: WorkerWechatReadTarget,
        observation_count: int,
    ) -> dict:
        sidecar = copy.deepcopy(fixture["omniauto_output"])
        sidecar["observations"] = sidecar["observations"][
            :observation_count
        ]
        selected_ids = {
            item["observation_id"] for item in sidecar["observations"]
        }
        sidecar["slot_ledger_states"] = [
            item
            for item in sidecar["slot_ledger_states"]
            if item["observation_id"] in selected_ids
        ]
        sidecar["sequence_alignment_evidence"][
            "new_suffix_observation_ids"
        ] = [item["observation_id"] for item in sidecar["observations"]]
        stable_ids = {
            item["observation_id"]: item["_worker_stable_id"]
            for item in sidecar["observations"]
        }
        for state in sidecar["slot_ledger_states"]:
            state["origin_read_run_id"] = read_run_id
            state["source_message_key"] = worker_source_message_key(
                target,
                identity_kind="worker_sequence",
                identity=stable_ids[state["observation_id"]],
            )
        return build_worker_message_ingest_payload(
            target,
            sidecar,
            read_run_id=read_run_id,
        )

    previous_app_dir = worker_storage.APP_DIR
    previous_db_file = worker_storage.DB_FILE
    worker_storage.APP_DIR = tmp_path
    worker_storage.DB_FILE = tmp_path / "worker_client.sqlite3"
    try:
        first_payload = worker_payload(
            target_for(_binding_authorization_revision(binding["id"])),
            1,
        )
        first_outbox_id = worker_storage.enqueue_c2_outbox(first_payload)
        first = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=first_payload,
            headers=_worker_headers(worker),
        )
        assert first.status_code == 200, first.text
        worker_storage.transition_c2_outbox(
            first_outbox_id,
            status="confirmed",
        )
        first_data = first.json()["data"]
        assert "message_batch" in first_data, json.dumps(
            first_data,
            ensure_ascii=False,
            sort_keys=True,
        )
        first_batch = first_data["message_batch"]
        continuation = first_batch["continuation"]

        second_payload = worker_payload(
            target_for(
                continuation["authorization_revision"],
                continuation=continuation,
            ),
            2,
        )
        second_outbox_id = worker_storage.enqueue_c2_outbox(second_payload)
        second = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=second_payload,
            headers=_worker_headers(worker),
        )
        assert second.status_code == 200, second.text
        worker_storage.transition_c2_outbox(
            second_outbox_id,
            status="confirmed",
        )

        assert first_outbox_id != second_outbox_id
        assert first_outbox_id.startswith(
            f"c2-outbox:{read_run_id}:batch-"
        )
        assert second_outbox_id.startswith(
            f"c2-outbox:{read_run_id}:batch-"
        )
        with worker_storage.db_connection() as local_db:
            local_rows = local_db.execute(
                """
                SELECT outbox_id, status FROM c2_ingest_outbox
                WHERE read_run_id = ? ORDER BY created_at
                """,
                (read_run_id,),
            ).fetchall()
        assert len(local_rows) == 2
        assert {row["status"] for row in local_rows} == {"confirmed"}

        second_data = second.json()["data"]
        assert second_data["ingested_count"] == 1
        assert second_data["duplicated_count"] == 1
        assert second_data["message_batch"]["batch_id"] != first_batch[
            "batch_id"
        ]
        with SessionLocal() as db:
            messages = (
                db.query(MessageEvent)
                .filter(
                    MessageEvent.conversation_id
                    == binding["conversation_id"]
                )
                .order_by(MessageEvent.ingested_at.asc())
                .all()
            )
            assert [message.message_type for message in messages] == [
                "text",
                "voice",
            ]
            old_batch = db.get(MessageBatch, first_batch["batch_id"])
            assert old_batch.status == "superseded"
            assert old_batch.active is False
    finally:
        worker_storage.APP_DIR = previous_app_dir
        worker_storage.DB_FILE = previous_db_file


def test_sent_first_reply_then_second_voice_keeps_five_row_checkpoint(
    tmp_path,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("第二轮语音客户", "13896676696")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200, scan.text
    binding = scan.json()["data"]["bindings"][0]

    first_read_run_id = "read-before-first-ai-reply"
    first_templates = [
        _v3_message(
            "welcome-self",
            role="self",
            message_type="text",
            content="您好，我是车金二手车顾问。",
            screen_order=1,
        ),
        _v3_message(
            "friend-accepted",
            role="customer",
            message_type="text",
            content="我通过了你的朋友验证请求，现在我们可以开始聊天了",
            screen_order=2,
        ),
        _v3_message(
            "first-customer-question",
            role="customer",
            message_type="text",
            content="有10万左右的二手车推荐吗？",
            screen_order=3,
        ),
    ]
    first_observations = [
        _committed_test_observation(
            template["raw_payload"]["observation"],
            worker_sequence=index,
            commit_basis=MessageCommitBasis.NEW_SUFFIX,
            proof={
                "alignment_status": "not_required",
                "old_tail_fully_consumed": True,
                "new_suffix_observation_id": template["raw_payload"][
                    "observation"
                ]["observation_id"],
            },
        )
        for index, template in enumerate(first_templates, start=1)
    ]
    first_payload = _production_worker_payload_for_test(
        binding=binding,
        remark_code=remark_code,
        read_run_id=first_read_run_id,
        observations=first_observations,
    )
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=first_payload,
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["ingested_count"] == 3

    reply_text = (
        "可以，预算先按10万元左右筛选。你更偏向轿车还是SUV，"
        "主要用于通勤还是家用？"
    )
    sent_at = utcnow()
    with SessionLocal() as db:
        sent_action = ReplyAction(
            batch_id="batch-first-reply-sent",
            conversation_id=binding["conversation_id"],
            status="sent",
            current=False,
            generation_no=1,
            decision="send_reply",
            reply_text=reply_text,
            reply_text_hash=hashlib.sha256(
                reply_text.encode("utf-8")
            ).hexdigest(),
            sent_at=sent_at,
        )
        db.add(sent_action)
        db.flush()
        first_reply_action_id = sent_action.id
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = sent_at
        db.commit()

    first_reply_template = _v3_message(
        "first-ai-reply-visible",
        role="self",
        message_type="text",
        content=reply_text,
        screen_order=4,
    )
    reply_receipt = {
        "reply_action_id": first_reply_action_id,
        "reply_text_hash": hashlib.sha256(
            reply_text.encode("utf-8")
        ).hexdigest(),
        "worker_stable_id": "worker-message-4",
        "source_message_key": "first-ai-reply-visible",
        "confirmed_at": sent_at.isoformat(),
    }
    first_reply_observation = _committed_test_observation(
        first_reply_template["raw_payload"]["observation"],
        worker_sequence=4,
        commit_basis=MessageCommitBasis.CONFIRMED_SENT_ACK,
        proof={"reply_action_id": first_reply_action_id},
        runtime_evidence={"_worker_ai_reply_receipt": reply_receipt},
    )
    second_voice_template = _v3_message(
        "second-customer-voice",
        role="customer",
        message_type="voice",
        content="家用吧。",
        screen_order=5,
    )
    voice_observation = second_voice_template["raw_payload"]["observation"]
    voice_observation["source_adapter"] = "win32_ocr"
    voice_observation["source_message"]["source_adapter"] = "win32_ocr"
    voice_observation["voice_duration"] = 2
    voice_observation["source_message"]["voice_duration"] = 2
    action_mapping = {
        "canonical_action_id": "voice-action-second-round",
        "reserved_worker_stable_id": "worker-message-5",
        "selected_action_token": "second-round-voice-token",
        "pre_observation_id": "second-customer-voice-before",
        "trigger_observation_id": "second-customer-voice-trigger",
        "post_observation_id": voice_observation["observation_id"],
        "physical_identity_inherited_from_prepare": False,
        "physical_action_count": 1,
        "result_candidate_count": 1,
        "stable_business_content_signature": (
            stable_business_content_signature(voice_observation)
        ),
        "result_screen_order": 4,
        "binding_confirmed": True,
    }
    second_voice_observation = _committed_test_observation(
        voice_observation,
        worker_sequence=5,
        commit_basis=MessageCommitBasis.CONFIRMED_VOICE_ACTION,
        proof=action_mapping,
        runtime_evidence={
            "_worker_voice_action_summary": {
                "confirmed_action_mapping": action_mapping,
            }
        },
    )

    second_read_run_id = "read-after-first-ai-reply"
    complete_frame_observations = [
        *copy.deepcopy(first_observations),
        first_reply_observation,
        second_voice_observation,
    ]
    complete_frame_payload = _production_worker_payload_for_test(
        binding=binding,
        remark_code=remark_code,
        read_run_id=second_read_run_id,
        observations=complete_frame_observations,
        read_reason="recent_ai_sent",
        historical_count=3,
    )
    for slot in complete_frame_payload["evidence"]["slot_ledger_states"][:3]:
        slot["origin_read_run_id"] = first_read_run_id

    previous_app_dir = worker_storage.APP_DIR
    previous_db_file = worker_storage.DB_FILE
    worker_storage.APP_DIR = tmp_path
    worker_storage.DB_FILE = tmp_path / "worker_client.sqlite3"
    try:
        for message in first_payload["messages"]:
            worker_storage.save_c2_ledger_terminal(
                conversation_id=binding["conversation_id"],
                source_message_key=message["source_message_key"],
                origin_read_run_id=first_read_run_id,
                dedupe_key=message["dedupe_key"],
                message_type=message["message_type"],
                terminal_state="completed",
                ingest_state="confirmed",
                result={},
            )
        incremental_payload = (
            WorkerTaskRunner._filter_confirmed_messages(
                object.__new__(WorkerTaskRunner),
                complete_frame_payload,
            )
        )
        assert len(incremental_payload["messages"]) == 2
        assert [
            message["message_type"]
            for message in incremental_payload["messages"]
        ] == ["text", "voice"]
        assert len(incremental_payload["evidence"]["observations"]) == 5
        assert len(
            incremental_payload["evidence"]["slot_ledger_states"]
        ) == 5

        second = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=incremental_payload,
            headers=_worker_headers(worker),
        )
        assert second.status_code == 200, second.text
        second_data = second.json()["data"]
        assert second_data["ingested_count"] == 2
        assert second_data["duplicated_count"] == 0
        batch_id = second_data["message_batch"]["batch_id"]

        generated = client.post(
            f"/api/internal/message-batches/{batch_id}/generate",
            json={},
            headers={
                "X-Internal-Service-Token": (
                    "dev-only-internal-service-token-change-before-production"
                )
            },
        )
        assert generated.status_code == 200, generated.text
        status = client.get(
            (
                f"/api/workers/{worker['id']}/wechat/message-batches/"
                f"{batch_id}"
            ),
            headers=_worker_headers(worker),
        )
        assert status.status_code == 200, status.text
        checkpoint = status.json()["data"]["pre_send_fact_checkpoint"]
        assert len(checkpoint["committed_tail"]) == 5
        comparison = worker_compare_checkpoint(
            checkpoint,
            incremental_payload["evidence"]["observations"],
            before_frame_id="checkpoint:second-voice",
            after_frame_id="frame:second-reply-pre-send",
            current_tail_complete=True,
        )
        assert comparison["comparison_result"] == "checkpoint_equal"
        assert comparison["old_tail_fully_consumed"] is True

        with SessionLocal() as db:
            stored = (
                db.query(MessageEvent)
                .filter(
                    MessageEvent.conversation_id
                    == binding["conversation_id"]
                )
                .order_by(MessageEvent.ingested_at.asc())
                .all()
            )
            assert len(stored) == 5
            assert len(stored[-1].evidence["observations"]) == 5
    finally:
        worker_storage.APP_DIR = previous_app_dir
        worker_storage.DB_FILE = previous_db_file


def test_unknown_observation_requires_gate_then_reaches_identity_settlement(
    tmp_path,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("身份未知客户", "13896676698")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200, scan.text
    binding = scan.json()["data"]["bindings"][0]
    unknown_message = _v3_message(
        "identity-unknown-message",
        role="customer",
        message_type="text",
        content="身份暂时无法确认",
        screen_order=1,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-identity-unknown-with-evidence",
        messages=[unknown_message],
        read_reason="waiting_user_reply",
    )
    payload["evidence"]["slot_ledger_states"][0].update(
        {
            "origin_read_run_id": "unknown",
            "fact_scope": "unknown",
            "delivery_state": "not_enqueued",
        }
    )
    previous_app_dir = worker_storage.APP_DIR
    previous_db_file = worker_storage.DB_FILE
    worker_storage.APP_DIR = tmp_path
    worker_storage.DB_FILE = tmp_path / "worker_client.sqlite3"
    try:
        filtered = WorkerTaskRunner._filter_confirmed_messages(
            object.__new__(WorkerTaskRunner),
            payload,
        )
    finally:
        worker_storage.APP_DIR = previous_app_dir
        worker_storage.DB_FILE = previous_db_file
    assert filtered["messages"] == []
    assert len(filtered["evidence"]["observations"]) == 1
    assert len(filtered["evidence"]["slot_ledger_states"]) == 1

    missing_gate = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=filtered,
        headers=_worker_headers(worker),
    )
    assert missing_gate.status_code == 409
    assert missing_gate.json()["code"] == (
        "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE"
    )

    filtered["evidence"]["flow_gate_errors"] = [
        "C2_MESSAGE_HISTORY_GAP"
    ]
    filtered["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_MESSAGE_HISTORY_GAP",
            "position_source": "position_unavailable",
            "gate_scope": "reply_suffix",
            "min_screen_order": 0,
            "max_screen_order": 0,
            "boundary_relation": "unknown",
        }
    ]
    wrong_gate = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=filtered,
        headers=_worker_headers(worker),
    )
    assert wrong_gate.status_code == 409
    assert wrong_gate.json()["code"] == (
        "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE"
    )

    filtered["evidence"]["flow_gate_errors"] = [
        "MESSAGE_IDENTITY_UNCONFIRMED"
    ]
    filtered["evidence"]["flow_gate_details"] = [
        {
            "error_code": "MESSAGE_IDENTITY_UNCONFIRMED",
            "position_source": "position_unavailable",
            "gate_scope": "reply_suffix",
            "min_screen_order": 0,
            "max_screen_order": 0,
            "boundary_relation": "unknown",
        }
    ]
    filtered["evidence"]["flow_gate_identity_key"] = (
        "identity-unknown-message-gate"
    )
    accepted = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=filtered,
        headers=_worker_headers(worker),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["message_batch"]["batch_status"] == (
        "recoverable_hold"
    )
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(HandoffEvent).count() == 0
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert persisted_binding.recovery_hold["status"] == "active"


def test_failed_vision_fact_and_text_are_persisted_without_failing_batch_or_creating_reply():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("图片失败客户", "13896676684")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    failed_image_message = _v3_failed_image_message(
        "failed-image",
        role="customer",
        screen_order=1,
        reason="IMAGE_UNDERSTANDING_PROVIDER_FAILED",
    )
    text_message = _v3_message(
        "text-with-failed-image",
        role="customer",
        message_type="text",
        content="图片看不清也没关系，请看这句话",
        screen_order=2,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-failed-image-and-text",
        messages=[failed_image_message, text_message],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ingested_count"] == 2
    assert data["message_batch"]["batch_id"]
    assert data["message_batch"]["batch_status"] == "handoff_created"
    with SessionLocal() as db:
        rows = (
            db.query(MessageEvent)
            .filter(MessageEvent.conversation_id == binding["conversation_id"])
            .order_by(MessageEvent.ingested_at.asc())
            .all()
        )
        assert [(item.message_type, item.item_state, item.content) for item in rows] == [
            ("image", "failed", None),
            ("text", "completed", "图片看不清也没关系，请看这句话"),
        ]
        assert rows[0].error_code == "IMAGE_UNDERSTANDING_PROVIDER_FAILED"
        handoff = db.query(HandoffEvent).one()
        assert handoff.handoff_reason_code == "C2_IMAGE_UNDERSTANDING_FAILED"
        assert db.query(ReplyAction).count() == 0


def test_failed_self_image_is_sales_intervention_not_waiting_for_sales():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("销售图片识别失败客户", "13896676685")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-failed-self-image",
        messages=[
            _v3_failed_image_message(
                "failed-self-image",
                role="self",
                screen_order=1,
                reason="IMAGE_UNDERSTANDING_PROVIDER_FAILED",
                order_source="visual_top",
            )
        ],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert message.sender_role == "self"
        assert message.item_state == "failed"
        assert conversation.status == "sales_replied_waiting_user"
        assert db.query(HandoffEvent).count() == 0
        assert db.query(ReplyAction).count() == 0


def test_identity_gate_without_recent_ai_boundary_uses_unified_recovery_hold():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("历史断层客户", "13896676683")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-empty-history-gap",
        messages=[],
    )
    payload["evidence"]["flow_gate_errors"] = ["C2_MESSAGE_HISTORY_GAP"]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_MESSAGE_HISTORY_GAP",
            "position_source": "position_unavailable",
            "gate_scope": "reply_suffix",
            "min_screen_order": 0,
            "max_screen_order": 0,
            "boundary_relation": "unknown",
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ingested_count"] == 0
    assert data["message_batch"]["batch_status"] == "recoverable_hold"
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(HandoffEvent).count() == 0
        persisted_binding = db.get(
            WechatSessionBinding, binding["id"]
        )
        assert persisted_binding.recovery_hold["status"] == "active"
        assert persisted_binding.recovery_hold["gate_scope"] == (
            "conversation_identity"
        )
        assert persisted_binding.recovery_hold["recovery_attempt_count"] == 0
        assert db.query(ReplyAction).count() == 0
        assert db.query(Task).filter(Task.task_type == "chat_reply").count() == 0


def test_identity_gate_never_relaxes_existing_hard_handoff():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("硬转人工保护客户", "13896676675")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_sales_reply"
        conversation.handoff_reason_code = "CUSTOMER_HIGH_INTENT"
        db.add(
            HandoffEvent(
                conversation_id=binding["conversation_id"],
                status="created",
                handoff_reason_code="CUSTOMER_HIGH_INTENT",
            )
        )
        db.commit()

    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-hard-handoff-identity-gate",
        messages=[],
        read_reason="waiting_sales_reply",
    )
    payload["evidence"]["flow_gate_errors"] = [
        "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
    ]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
            "position_source": "position_unavailable",
            "gate_scope": "reply_suffix",
            "min_screen_order": 0,
            "max_screen_order": 0,
            "boundary_relation": "unknown",
        }
    ]
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert conversation.status == "waiting_sales_reply"
        assert conversation.handoff_reason_code == "CUSTOMER_HIGH_INTENT"
        assert persisted_binding.recovery_hold in ({}, None)
        assert db.query(HandoffEvent).count() == 1
        assert db.query(MessageBatch).count() == 0
        assert db.query(ReplyAction).count() == 0


def test_retired_vision_capability_gate_is_rejected_without_persisting_facts():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("Vision 配置恢复客户", "13896676689")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    paused_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-vision-capability-paused",
        messages=[
            _v3_message(
                "text-next-to-paused-image",
                role="customer",
                message_type="text",
                content="图片稍后再识别，文字先保存",
                screen_order=1,
                )
        ],
        read_reason="waiting_user_reply",
    )
    paused_payload["evidence"]["flow_gate_errors"] = [
        "C2_VISION_CAPABILITY_PAUSED"
    ]
    paused_payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_VISION_CAPABILITY_PAUSED",
            "position_source": "slot_ledger_visual_top",
            "min_screen_order": 2,
            "max_screen_order": 2,
        }
    ]

    paused = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=paused_payload,
        headers=_worker_headers(worker),
    )

    assert paused.status_code == 409, paused.text
    assert paused.json()["code"] == "MESSAGE_FLOW_GATE_CODE_RETIRED"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "waiting_user_reply"
        assert db.query(HandoffEvent).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(MessageEvent).count() == 0


def test_retired_deferred_image_gate_is_rejected_without_persisting_text():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("图片暂缓客户", "13896676690")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-image-processing-deferred",
        messages=[
            _v3_message(
                "text-next-to-deferred-image",
                role="customer",
                message_type="text",
                content="请结合旁边的图片看看",
                screen_order=1,
            )
        ],
        read_reason="waiting_user_reply",
    )
    payload["evidence"]["flow_gate_errors"] = [
        "C2_IMAGE_PROCESSING_DEFERRED"
    ]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_IMAGE_PROCESSING_DEFERRED",
            "position_source": "slot_ledger_visual_top",
            "min_screen_order": 2,
            "max_screen_order": 2,
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "MESSAGE_FLOW_GATE_CODE_RETIRED"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "waiting_user_reply"
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(HandoffEvent).count() == 0
        assert db.query(ReplyAction).count() == 0
        assert (
            db.query(Task)
            .filter(Task.task_type == "chat_reply")
            .count()
            == 0
        )


def test_identity_gate_without_ai_boundary_is_one_idempotent_handoff():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("身份歧义客户", "13896676684")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]

    statuses = []
    last_payload = None
    for index, read_run_id in enumerate(
        (
            "read-identity-gate-discovery",
            "read-identity-gate-reread-1",
            "read-identity-gate-reread-2",
        )
    ):
        payload = _v3_ingest_payload(
            binding,
            remark_code,
            read_run_id=read_run_id,
            messages=[],
            read_reason="waiting_user_reply",
        )
        payload["evidence"]["flow_gate_errors"] = [
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
        ]
        payload["evidence"]["flow_gate_details"] = [
            {
                "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                "position_source": "position_unavailable",
                "gate_scope": "reply_suffix",
                "min_screen_order": 0,
                "max_screen_order": 0,
                "boundary_relation": "unknown",
            }
        ]
        payload["evidence"]["flow_gate_identity_key"] = "stable-identity-gate"
        if index > 0:
            payload["evidence"]["recovery_attempt_kind"] = "stable_reread"
            authorization = client.get(
                f"/api/workers/{worker['id']}/wechat/conversations/"
                f"{binding['conversation_id']}/read-authorization",
                headers=_worker_headers(worker),
            )
            assert authorization.status_code == 200, authorization.text
            payload["authorization_revision"] = authorization.json()["data"][
                "authorization_revision"
            ]
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=payload,
            headers=_worker_headers(worker),
        )
        assert response.status_code == 200, response.text
        message_batch = response.json()["data"].get("message_batch")
        if message_batch is not None:
            statuses.append(message_batch["batch_status"])
        last_payload = payload

    assert statuses[:2] == ["recoverable_hold", "recoverable_hold"]
    assert statuses[2] in {"handoff_created", "handoff_pending", "handoff"}

    retry = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=last_payload,
        headers=_worker_headers(worker),
    )
    assert retry.status_code == 200, retry.text

    with SessionLocal() as db:
        assert db.query(MessageBatch).count() == 1
        assert db.query(HandoffEvent).count() == 1
        persisted_binding = db.get(
            WechatSessionBinding, binding["id"]
        )
        assert persisted_binding.recovery_hold["status"] == "escalated"
        assert persisted_binding.recovery_hold["recovery_attempt_count"] == 2


def test_identity_gate_before_ai_boundary_is_warning_without_hold_or_handoff():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("AI边界前历史告警客户", "13896676674")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        db.add(
            ReplyAction(
                batch_id="batch-before-boundary-warning",
                conversation_id=binding["conversation_id"],
                status="sent",
                decision="send_reply",
                reply_text="已发送边界",
                reply_text_hash=hashlib.sha256(
                    "已发送边界".encode("utf-8")
                ).hexdigest(),
                sent_at=conversation.last_ai_reply_at,
            )
        )
        db.commit()

    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-ai-boundary-history-warning",
        messages=[],
        read_reason="recent_ai_sent",
    )
    payload["evidence"]["flow_gate_errors"] = [
        "MESSAGE_IDENTITY_UNCONFIRMED"
    ]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "MESSAGE_IDENTITY_UNCONFIRMED",
            "position_source": "slot_ledger_visual_top",
            "gate_scope": "reply_suffix",
            "min_screen_order": 1,
            "max_screen_order": 2,
            "boundary_relation": "before_or_equal",
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["message_batch"]["batch_status"] == (
        "historical_warning"
    )
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert conversation.status == "waiting_user_reply"
        assert persisted_binding.recovery_hold in ({}, None)
        assert db.query(HandoffEvent).count() == 0
        assert db.query(ReplyAction).count() == 1
        assert (
            db.query(ReplyAction)
            .filter(ReplyAction.status == "queued")
            .count()
            == 0
        )


def test_recent_ai_gate_with_incomplete_boundary_enters_generic_identity_hold():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("AI边界不完整客户", "13896676672")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        db.add(
            ReplyAction(
                batch_id="batch-incomplete-boundary",
                conversation_id=binding["conversation_id"],
                status="sent",
                decision="send_reply",
                reply_text="边界不完整",
                reply_text_hash=hashlib.sha256(
                    "边界不完整".encode("utf-8")
                ).hexdigest(),
                sent_at=conversation.last_ai_reply_at,
            )
        )
        db.commit()
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-incomplete-ai-boundary",
        messages=[],
        read_reason="recent_ai_sent",
    )
    payload["evidence"]["ai_reply_boundary"].pop("sent_at")
    payload["evidence"]["flow_gate_errors"] = [
        "MESSAGE_IDENTITY_UNCONFIRMED"
    ]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "MESSAGE_IDENTITY_UNCONFIRMED",
            "position_source": "position_unavailable",
            "gate_scope": "reply_suffix",
            "min_screen_order": 0,
            "max_screen_order": 0,
            "boundary_relation": "unknown",
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["message_batch"]["batch_status"] == (
        "recoverable_hold"
    )
    with SessionLocal() as db:
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert persisted_binding.recovery_hold["status"] == "active"
        assert persisted_binding.recovery_hold["gate_scope"] == "reply_suffix"
        assert db.query(HandoffEvent).count() == 0


@pytest.mark.parametrize(
    "gate_code",
    [
        "C2_IMAGE_IDENTITY_CONTRACT_INVALID",
        "C2_VOICE_IDENTITY_CONTRACT_INVALID",
        "C2_VOICE_RESULT_AMBIGUOUS",
    ],
)
def test_media_action_technical_failure_is_rejected_without_recovery_or_handoff(
    gate_code: str,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"媒体身份隔离{gate_code}", "13896676674")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        db.commit()

    def gate_payload(read_run_id: str) -> dict:
        payload = _v3_ingest_payload(
            binding,
            remark_code,
            read_run_id=read_run_id,
            messages=[
                _v3_message(
                    f"must-rollback-{gate_code}",
                    role="customer",
                    message_type="text",
                    content="这条事实必须随技术故障请求整体回滚",
                    screen_order=1,
                )
            ],
            read_reason="waiting_user_reply",
        )
        payload["evidence"]["flow_gate_errors"] = [gate_code]
        payload["evidence"]["flow_gate_identity_key"] = (
            f"stable-{gate_code}"
        )
        payload["evidence"]["flow_gate_details"] = [
            {
                "error_code": gate_code,
                "position_source": "position_unavailable",
                "gate_scope": "conversation_identity",
                "min_screen_order": 0,
                "max_screen_order": 0,
                "boundary_relation": "unknown",
            }
        ]
        return payload

    endpoint = (
        f"/api/workers/{worker['id']}/wechat/messages/ingest"
    )
    response = client.post(
        endpoint,
        json=gate_payload(f"read-{gate_code}-technical-failure"),
        headers=_worker_headers(worker),
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == gate_code

    with SessionLocal() as db:
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert not persisted_binding.recovery_hold
        assert db.query(HandoffEvent).count() == 0
        assert db.query(MessageEvent).count() == 0


def test_identity_unresolved_hold_escalates_after_120_seconds_without_more_ui_actions():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("身份隔离超时客户", "13896676675")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        persisted = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        persisted.recovery_hold = {
            "status": "active",
            "gate_key": "expired-identity-gate",
            "reason_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
            "reason_codes": ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
            "first_seen_at": (
                utcnow() - timedelta(seconds=121)
            ).isoformat(),
            "last_seen_at": utcnow().isoformat(),
            "recovery_attempt_count": 0,
        }
        db.commit()

    response = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        persisted = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert persisted.recovery_hold["status"] == "escalated"
        assert conversation.status == "waiting_sales_reply"
        assert db.query(HandoffEvent).count() == 1


def test_recent_ai_sent_without_new_messages_is_no_change_and_stays_waiting():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("AI回复后无新消息客户", "13896676671")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        db.add(
            ReplyAction(
                batch_id="batch-no-change-boundary",
                conversation_id=binding["conversation_id"],
                status="sent",
                decision="send_reply",
                reply_text="我等您的新消息。",
                reply_text_hash=hashlib.sha256(
                    "我等您的新消息。".encode("utf-8")
                ).hexdigest(),
                sent_at=conversation.last_ai_reply_at,
            )
        )
        db.commit()
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-recent-ai-no-change",
        messages=[],
        read_reason="recent_ai_sent",
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["read_completion"]["result"] == "no_change"
    assert "message_batch" not in data
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "waiting_user_reply"
        assert db.query(MessageBatch).count() == 0
        assert db.query(HandoffEvent).count() == 0


@pytest.mark.parametrize("tail_relation", ["unknown", "after"])
def test_reply_suffix_hold_aggregates_all_gates_and_escalates_after_real_reread(
    tail_relation: str,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("AI边界后稳定重读客户", "13896676673")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        db.add(
            ReplyAction(
                batch_id="batch-hold-boundary",
                conversation_id=binding["conversation_id"],
                status="sent",
                decision="send_reply",
                reply_text="边界回复",
                reply_text_hash=hashlib.sha256(
                    "边界回复".encode("utf-8")
                ).hexdigest(),
                sent_at=conversation.last_ai_reply_at,
            )
        )
        db.commit()

    def gate_payload(read_run_id: str, recovery_kind: str) -> dict:
        payload = _v3_ingest_payload(
            binding,
            remark_code,
            read_run_id=read_run_id,
            messages=[],
            read_reason=(
                "recent_ai_sent"
                if recovery_kind == "checkpoint_merge"
                else "waiting_user_reply"
            ),
        )
        payload["evidence"]["flow_gate_errors"] = [
            "C2_MESSAGE_HISTORY_GAP",
            "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        ]
        payload["evidence"]["flow_gate_details"] = [
            {
                "error_code": "C2_MESSAGE_HISTORY_GAP",
                "position_source": "slot_ledger_visual_top",
                "gate_scope": "reply_suffix",
                "min_screen_order": 1,
                "max_screen_order": 2,
                "boundary_relation": "before_or_equal",
            },
            {
                "error_code": "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
                "position_source": "identity_error_visual_top",
                "gate_scope": "reply_suffix",
                "min_screen_order": 8,
                "max_screen_order": 8,
                "boundary_relation": tail_relation,
            }
        ]
        payload["evidence"]["recovery_attempt_kind"] = recovery_kind
        return payload

    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=gate_payload("read-hold-merge", "checkpoint_merge"),
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["message_batch"]["batch_status"] == (
        "recoverable_hold"
    )
    with SessionLocal() as db:
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert persisted_binding.recovery_hold["recovery_attempt_count"] == 0
        assert persisted_binding.recovery_hold["boundary_relation"] == tail_relation
        sent_action = db.query(ReplyAction).filter(
            ReplyAction.conversation_id == binding["conversation_id"],
            ReplyAction.status == "sent",
        ).one()
        expected_gate_key = hashlib.sha256(
            (
                binding["conversation_id"]
                + "|"
                + sent_action.id
                + "|C2_MESSAGE_HISTORY_GAP|MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"
                + "|reply_suffix"
            ).encode("utf-8")
        ).hexdigest()
        assert persisted_binding.recovery_hold["gate_key"] == expected_gate_key
        assert db.query(HandoffEvent).count() == 0

    duplicate_first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=gate_payload("read-hold-merge", "checkpoint_merge"),
        headers=_worker_headers(worker),
    )
    assert duplicate_first.status_code == 200, duplicate_first.text
    with SessionLocal() as db:
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert persisted_binding.recovery_hold["recovery_attempt_count"] == 0

    second_payload = gate_payload("read-hold-reread", "stable_reread")
    refreshed_authorization = client.get(
        f"/api/workers/{worker['id']}/wechat/conversations/"
        f"{binding['conversation_id']}/read-authorization",
        headers=_worker_headers(worker),
    )
    assert refreshed_authorization.status_code == 200
    second_payload["authorization_revision"] = (
        refreshed_authorization.json()["data"]["authorization_revision"]
    )
    second = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=second_payload,
        headers=_worker_headers(worker),
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["message_batch"]["batch_status"] == (
        "recoverable_hold"
    )
    duplicate = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=second_payload,
        headers=_worker_headers(worker),
    )
    assert duplicate.status_code == 200, duplicate.text
    with SessionLocal() as db:
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert persisted_binding.recovery_hold["status"] == "active"
        assert persisted_binding.recovery_hold["recovery_attempt_count"] == 1
        assert db.query(HandoffEvent).count() == 0

    third_payload = gate_payload("read-hold-reread-2", "stable_reread")
    refreshed_authorization = client.get(
        f"/api/workers/{worker['id']}/wechat/conversations/"
        f"{binding['conversation_id']}/read-authorization",
        headers=_worker_headers(worker),
    )
    assert refreshed_authorization.status_code == 200
    third_payload["authorization_revision"] = (
        refreshed_authorization.json()["data"]["authorization_revision"]
    )
    third = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=third_payload,
        headers=_worker_headers(worker),
    )
    assert third.status_code == 200, third.text
    assert third.json()["data"]["message_batch"]["batch_status"] in {
        "handoff_created",
        "handoff_pending",
        "handoff",
    }
    with SessionLocal() as db:
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert persisted_binding.recovery_hold["status"] == "escalated"
        assert persisted_binding.recovery_hold["recovery_attempt_count"] == 2
        assert db.query(HandoffEvent).count() == 1
        assert (
            db.query(ReplyAction)
            .filter(ReplyAction.status == "queued")
            .count()
            == 0
        )


def test_v3_rejects_ingestible_observation_omitted_by_worker():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("完整性客户", "13896676682")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    included = _v3_message(
        "included-text",
        role="customer",
        message_type="text",
        content="第一条",
        screen_order=1,
    )
    omitted = _v3_message(
        "omitted-text",
        role="customer",
        message_type="text",
        content="第二条不能被漏掉",
        screen_order=2,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-observation-omitted",
        messages=[included],
    )
    payload["evidence"]["observations"].append(omitted["raw_payload"]["observation"])
    payload["evidence"]["sequence_alignment_evidence"][
        "new_suffix_observation_ids"
    ].append(omitted["raw_payload"]["observation"]["observation_id"])

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0


def test_v3_rejects_forged_historical_observation_not_found_in_backend():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("伪造历史客户", "13896676697")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    included = _v3_message(
        "included-current-text",
        role="customer",
        message_type="text",
        content="本轮真实新增消息",
        screen_order=2,
    )
    forged = _v3_message(
        "forged-historical-text",
        role="customer",
        message_type="text",
        content="后端从未保存过的消息",
        screen_order=1,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-forged-historical-observation",
        messages=[included],
    )
    payload["evidence"]["observations"].insert(
        0, forged["raw_payload"]["observation"]
    )
    payload["evidence"]["slot_ledger_states"].insert(
        0,
        {
            "observation_id": forged["raw_payload"]["observation"][
                "observation_id"
            ],
            "screen_order": 1,
            "order_source": "observation_index_fallback",
            "row_kind": "text_bubble",
            "source_message_key": forged["source_message_key"],
            "origin_read_run_id": "read-that-never-existed",
            "fact_scope": "historical",
            "delivery_state": "backend_confirmed",
            "item_state": "completed",
        },
    )
    payload["evidence"]["sequence_alignment_evidence"] = {
        "pre_sequence_source": "checkpoint",
        "pre_frame_id": "checkpoint:forged-history",
        "post_frame_id": "frame:read-forged-historical-observation",
        "alignment_status": "unique",
        "candidate_alignment_count": 1,
        "matched_pairs": [
            {
                "identity_state": "committed",
                "worker_stable_id": "worker-message-999",
                "pre_observation_id": forged["raw_payload"][
                    "observation"
                ]["observation_id"],
                "post_observation_id": forged["raw_payload"][
                    "observation"
                ]["observation_id"],
                "pre_index": 0,
                "post_index": 0,
                "match_basis": "worker_stable_identity",
            }
        ],
        "old_tail_fully_consumed": True,
        "new_suffix_observation_ids": [
            included["raw_payload"]["observation"]["observation_id"]
        ],
    }

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == (
        "MESSAGE_OBSERVATION_MAPPING_INCOMPLETE"
    )
    with SessionLocal() as db:
        assert (
            db.query(MessageEvent)
            .filter(
                MessageEvent.conversation_id == binding["conversation_id"]
            )
            .count()
            == 0
        )


@pytest.mark.parametrize(
    "forbidden",
    [
        "image_local_path",
        "image_recognition",
        "provider_response",
        "provider_response_text",
        "retry_response_diagnostics",
        "thumbnail",
        "asset_id",
        "picture_ref",
        "base64",
    ],
)
def test_image_text_whitelist_rejects_runtime_or_recoverable_fields(forbidden):
    payload = {
        "customer_image_understanding": {"schema_version": 1, "vision_summary": "白色 SUV", forbidden: "secret"},
        "visual_bridge_input": {"present": True, "vision_summary": "白色 SUV"},
    }
    with pytest.raises(Exception) as exc:
        wechat_service._validate_image_understanding(payload)
    assert getattr(exc.value, "code", None) in {"IMAGE_PERSISTENCE_FIELD_FORBIDDEN", "IMAGE_UNDERSTANDING_FIELD_INVALID"}


def test_image_text_whitelist_rejects_forbidden_field_hidden_in_observation_source():
    payload = {
        "customer_image_understanding": {"schema_version": 1, "vision_summary": "白色 SUV"},
        "visual_bridge_input": {"present": True, "vision_summary": "白色 SUV"},
        "observation": {
            "source_message": {
                "provider_response": {"output": "完整 Provider 响应不得持久化"},
            }
        },
    }

    with pytest.raises(Exception) as exc:
        wechat_service._validate_image_understanding(payload)

    assert getattr(exc.value, "code", None) == "IMAGE_PERSISTENCE_FIELD_FORBIDDEN"


@pytest.mark.parametrize(
    ("result_code", "expected_transition_status"),
    [
        ("invite_sent", "friend_request_sent"),
        ("already_friend", "friend_active"),
    ],
)
def test_completed_add_friend_results_share_guarded_first_activation_flow(
    result_code,
    expected_transition_status,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"好友激活-{result_code}", "13896676685")
    remark_code = _pull_remark_code(worker)
    completed_task = _complete_add_friend_task_result(worker, result_code)

    with SessionLocal() as db:
        assert db.query(Conversation).count() == 0
        assert db.query(MessageBatch).count() == 0
        task = db.get(Task, completed_task.id)
        assert task is not None
        assert task.status == "completed"
        assert task.result_code == result_code
    before_binding_targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert before_binding_targets.status_code == 200
    assert before_binding_targets.json()["data"]["targets"] == []

    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation is not None
        assert conversation.friend_state == expected_transition_status
        assert conversation.status == expected_transition_status

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    ).json()["data"]["targets"]
    assert len(targets) == 1
    assert targets[0]["read_reason"] == "friend_acceptance_visible_hit"

    invalid = client.post(
        f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/activation-confirm",
        json={
            "authorization_revision": targets[0]["authorization_revision"],
            "remark_code": remark_code,
            "conversation_type": "group",
            "chat_surface_ready": True,
            "title_evidence": {
                "short_code_confirmed": True,
                "admission_allowed": False,
                "conversation_type": "group",
            },
        },
        headers=_worker_headers(worker),
    )
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "C2_FRIEND_ACTIVATION_EVIDENCE_INVALID"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation is not None
        assert conversation.status == expected_transition_status

    activation_payload = {
        "authorization_revision": targets[0]["authorization_revision"],
        "remark_code": remark_code,
        "conversation_type": "private",
        "chat_surface_ready": True,
        "title_evidence": {
            "short_code_confirmed": True,
            "admission_allowed": True,
            "conversation_type": "private",
        },
    }
    activation = client.post(
        f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/activation-confirm",
        json=activation_payload,
        headers=_worker_headers(worker),
    )
    repeated = client.post(
        f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/activation-confirm",
        json=activation_payload,
        headers=_worker_headers(worker),
    )
    assert activation.status_code == 200
    assert repeated.status_code == 200
    assert activation.json()["data"]["friend_state"] == "friend_active"
    assert activation.json()["data"]["conversation_status"] == "friend_activation_reading"
    assert repeated.json()["data"]["conversation_status"] == "friend_activation_reading"
    continued_targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    ).json()["data"]["targets"]
    assert continued_targets[0]["read_reason"] == "friend_acceptance_visible_hit"


@pytest.mark.parametrize(
    ("invalid_field", "expected_error"),
    [
        ("authorization_revision", "MESSAGE_AUTHORIZATION_REVISION_EXPIRED"),
        ("remark_code", "MESSAGE_TARGET_IDENTITY_MISMATCH"),
        ("conversation_type", "C2_FRIEND_ACTIVATION_EVIDENCE_INVALID"),
        ("chat_surface_ready", "C2_FRIEND_ACTIVATION_EVIDENCE_INVALID"),
        ("short_code_confirmed", "C2_FRIEND_ACTIVATION_EVIDENCE_INVALID"),
        ("admission_allowed", "C2_FRIEND_ACTIVATION_EVIDENCE_INVALID"),
    ],
)
def test_friend_activation_requires_current_authorization_and_private_ready_chat(
    invalid_field,
    expected_error,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"激活安全门禁-{invalid_field}", "13896676687")
    remark_code = _pull_remark_code(worker)
    _complete_add_friend_task_result(worker, "already_friend")
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    target = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    ).json()["data"]["targets"][0]
    payload = {
        "authorization_revision": target["authorization_revision"],
        "remark_code": remark_code,
        "conversation_type": "private",
        "chat_surface_ready": True,
        "title_evidence": {
            "short_code_confirmed": True,
            "admission_allowed": True,
            "conversation_type": "private",
        },
    }
    if invalid_field == "authorization_revision":
        payload[invalid_field] = "stale-authorization-revision"
    elif invalid_field == "remark_code":
        payload[invalid_field] = "CJWRONG01"
    elif invalid_field == "conversation_type":
        payload[invalid_field] = "group"
        payload["title_evidence"]["conversation_type"] = "group"
    elif invalid_field in {"short_code_confirmed", "admission_allowed"}:
        payload["title_evidence"][invalid_field] = False
    else:
        payload[invalid_field] = False

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/activation-confirm",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == expected_error
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation is not None
        assert conversation.friend_state == "friend_active"
        assert conversation.status == "friend_active"


@pytest.mark.parametrize(
    ("friend_state", "conversation_status"),
    [
        ("friend_active", "friend_request_sent"),
        ("friend_request_sent", "friend_active"),
    ],
)
def test_inconsistent_friend_state_and_status_do_not_authorize_first_read(
    friend_state,
    conversation_status,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("好友状态不一致", "13896676688")
    remark_code = _pull_remark_code(worker)
    _complete_add_friend_task_result(worker, "already_friend")
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert conversation is not None
        assert binding_row is not None
        conversation.friend_state = friend_state
        conversation.status = conversation_status
        binding_row.unread_hint = False
        db.commit()

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )

    assert targets.status_code == 200
    assert targets.json()["data"]["targets"] == []


def test_friend_acceptance_empty_read_creates_one_welcome_batch_without_fake_message():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("开场客户", "13896676683")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.friend_state = "friend_request_sent"
        conversation.status = "friend_request_sent"
        db.commit()

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    ).json()["data"]["targets"]
    assert targets[0]["read_reason"] == "friend_acceptance_visible_hit"
    activation = client.post(
        f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/activation-confirm",
        json={
            "authorization_revision": targets[0]["authorization_revision"],
            "remark_code": remark_code,
            "conversation_type": "private",
            "chat_surface_ready": True,
            "title_evidence": {
                "matched": True,
                "short_code_confirmed": True,
                "admission_allowed": True,
                "conversation_type": "private",
                "raw_title": f"{remark_code} 开场客户",
            },
        },
        headers=_worker_headers(worker),
    )
    assert activation.status_code == 200
    assert activation.json()["data"]["activation_confirmed"] is True
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.friend_state == "friend_active"
        assert conversation.status == "friend_activation_reading"
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="welcome-read",
        messages=[],
        read_reason="friend_acceptance_visible_hit",
    )
    payload["authorization_revision"] = activation.json()["data"]["authorization_revision"]
    payload["evidence"]["read_reason"] = "friend_acceptance_visible_hit"
    payload["evidence"]["authorization_read_reason"] = "friend_acceptance_visible_hit"
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    repeated = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200
    assert repeated.status_code == 200
    assert response.json()["data"]["ingested_count"] == 0
    assert response.json()["data"]["message_batch"]["batch_id"]
    with SessionLocal() as db:
        batch = db.query(MessageBatch).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert batch.trigger_type == "friend_welcome"
        assert batch.message_event_ids == []
        assert db.query(MessageEvent).count() == 0
        assert conversation.friend_state == "friend_active"
        assert conversation.status not in {
            "friend_request_sent",
            "friend_active",
            "friend_activation_reading",
        }


@pytest.mark.parametrize(
    ("sender_role", "expected_batch_type", "expected_status"),
    [
        ("customer", "customer_message", "ai_active"),
        ("self", None, "sales_replied_waiting_user"),
    ],
)
def test_already_friend_first_read_routes_customer_and_sales_without_welcome(
    sender_role,
    expected_batch_type,
    expected_status,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"首读分流-{sender_role}", "13896676686")
    remark_code = _pull_remark_code(worker)
    _complete_add_friend_task_result(worker, "already_friend")
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    target = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    ).json()["data"]["targets"][0]
    activation = client.post(
        f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/activation-confirm",
        json={
            "authorization_revision": target["authorization_revision"],
            "remark_code": remark_code,
            "conversation_type": "private",
            "chat_surface_ready": True,
            "title_evidence": {
                "short_code_confirmed": True,
                "admission_allowed": True,
                "conversation_type": "private",
            },
        },
        headers=_worker_headers(worker),
    )
    assert activation.status_code == 200
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"already-friend-first-read-{sender_role}",
        messages=[
            _v3_message(
                f"already-friend-{sender_role}-message",
                role=sender_role,
                message_type="text",
                content=(
                    "客户首次发来的消息"
                    if sender_role == "customer"
                    else "销售已经人工回复"
                ),
                screen_order=1,
            )
        ],
        read_reason="friend_acceptance_visible_hit",
    )
    payload["authorization_revision"] = activation.json()["data"][
        "authorization_revision"
    ]
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["ingested_count"] == 1
    with SessionLocal() as db:
        batches = db.query(MessageBatch).all()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation is not None
        assert all(batch.trigger_type != "friend_welcome" for batch in batches)
        if expected_batch_type:
            assert [batch.trigger_type for batch in batches] == [
                expected_batch_type
            ]
        else:
            assert batches == []
        if expected_status:
            assert conversation.status == expected_status
        assert conversation.status not in {
            "friend_request_sent",
            "friend_active",
            "friend_activation_reading",
        }


def test_stale_friend_request_does_not_occupy_read_targets_until_seen_again():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("等待通过客户", "13896676693")
    remark_code = _pull_remark_code(worker)
    scan_payload = _scan_payload(remark_code)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=scan_payload,
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        session_binding = db.get(WechatSessionBinding, binding["id"])
        conversation.friend_state = "friend_request_sent"
        conversation.status = "friend_request_sent"
        session_binding.last_seen_at = utcnow() - timedelta(minutes=10)
        db.commit()

    stale_targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    ).json()["data"]["targets"]
    assert stale_targets == []

    refreshed_payload = _scan_payload(remark_code)
    refreshed_payload["scan_id"] = "scan-friend-visible-again"
    refreshed = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=refreshed_payload,
        headers=_worker_headers(worker),
    )
    assert refreshed.status_code == 200
    visible_targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    ).json()["data"]["targets"]
    assert len(visible_targets) == 1
    assert visible_targets[0]["read_reason"] == "friend_acceptance_visible_hit"


def test_recall_precheck_empty_read_creates_unique_recall_batch_and_customer_cancels_cycle():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("召回客户", "13896676684")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "recall_precheck"
        conversation.recall_cycle_id = "recall-cycle-1"
        db.commit()
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="recall-read",
        messages=[],
        read_reason="recall_precheck",
    )
    payload["evidence"]["read_reason"] = "recall_precheck"
    payload["evidence"]["authorization_read_reason"] = "recall_precheck"
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        recall = db.query(MessageBatch).one()
        assert recall.trigger_type == "recall"
        assert recall.recall_cycle_id == "recall-cycle-1"
        assert db.get(Conversation, binding["conversation_id"]).recall_count == 0

    customer = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="recall-customer-read",
        messages=[_v3_message("customer-after-recall", role="customer", message_type="text", content="我又回来看看", screen_order=1)],
        read_reason="waiting_user_reply",
    )
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=customer,
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "ai_active"
        assert conversation.recall_cycle_id is None


@pytest.mark.parametrize(
    "origin_status",
    [
        "waiting_user_reply",
        "recalled_waiting_user",
        "sales_replied_waiting_user",
    ],
)
def test_recall_no_action_restores_exact_origin_status(
    monkeypatch,
    origin_status,
):
    settings = wechat_service.get_settings()
    monkeypatch.setattr(settings, "c3_recall_quiet_start_hour", 0)
    monkeypatch.setattr(settings, "c3_recall_quiet_end_hour", 0)

    class NoActionAdapter:
        def generate_reply_decision(self, **_kwargs):
            return c3_service.AIEngineDecision(
                decision="no_action",
                guard_result="pass",
                suggested_action="no_action",
            )

    monkeypatch.setattr(c3_service, "get_ai_engine_adapter", lambda: NoActionAdapter())
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(
        f"召回来源-{origin_status}",
        f"1388{abs(hash(origin_status)) % 10_000_000:07d}",
    )
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = origin_status
        conversation.ai_enabled = True
        conversation.next_recall_at = utcnow() - timedelta(minutes=1)
        conversation.recall_count = 1
        conversation.recall_daily_count = 0
        db.commit()

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert targets.status_code == 200
    assert targets.json()["data"]["targets"][0]["read_reason"] == "recall_precheck"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "recall_precheck"
        assert conversation.recall_origin_status == origin_status

    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"recall-no-action-{origin_status}",
        messages=[],
        read_reason="recall_precheck",
    )
    payload["evidence"]["read_reason"] = "recall_precheck"
    payload["evidence"]["authorization_read_reason"] = "recall_precheck"
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200
    batch_id = response.json()["data"]["message_batch"]["batch_id"]
    with SessionLocal() as db:
        batch = db.get(MessageBatch, batch_id)
        conversation = db.get(Conversation, binding["conversation_id"])
        assert batch.origin_conversation_status == origin_status
        assert batch.status == "no_action"
        assert conversation.status == origin_status
        assert conversation.recall_origin_status is None
        assert conversation.recall_cycle_id is None
        assert conversation.recall_count == 1
        assert conversation.recall_daily_count == 0
        assert conversation.next_recall_at is not None
        comparison_now = (
            utcnow()
            if conversation.next_recall_at.tzinfo is not None
            else utcnow().replace(tzinfo=None)
        )
        assert conversation.next_recall_at > comparison_now


def test_message_batch_status_rejects_other_worker_and_returns_terminal_state():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("批次客户", "13896676685")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="batch-status-read",
        messages=[_v3_message("batch-status-message", role="customer", message_type="text", content="看 SUV", screen_order=1)],
    )
    ingested = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    ).json()["data"]
    batch_id = ingested["message_batch"]["batch_id"]
    status = client.get(
        f"/api/workers/{worker['id']}/wechat/message-batches/{batch_id}",
        headers=_worker_headers(worker),
    )
    assert status.status_code == 200
    status_data = status.json()["data"]
    assert status_data["terminal"] is True
    assert status_data["authorization"]["conversation_id"] == binding["conversation_id"]
    assert status_data["authorization"]["authorization_revision"] == _binding_authorization_revision(binding["id"])
    assert isinstance(status_data["authorization"]["allowed"], bool)
    assert "read_reason" in status_data["authorization"]
    other = _create_worker()
    forbidden = client.get(
        f"/api/workers/{other['id']}/wechat/message-batches/{batch_id}",
        headers=_worker_headers(other),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "MESSAGE_BATCH_WORKER_MISMATCH"


def test_v3_ingest_uses_canonical_content_and_rejects_expired_authorization_revision():
    assert contract_revision() == "0.9.48"
    location_recovery = c2_contract_v3()[
        "target_location_recovery_contract"
    ]
    assert (
        location_recovery["error_code"]
        == "C2_VISIBLE_TARGET_STALE_AFTER_CLICK"
    )
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        db.commit()

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    revision = targets.json()["data"]["targets"][0]["authorization_revision"]
    payload = {
        **_v3_contract_fields(),
        "read_run_id": "read-v3",
        "conversation_id": binding["conversation_id"],
        "remark_code": remark_code,
        "rpa_session_key": "wx-row-1",
        "authorization_revision": revision,
        "unread_generation": int(
            targets.json()["data"]["targets"][0].get(
                "unread_generation"
            )
            or 0
        ),
        "messages": [
            {
                "dedupe_key": "v3-voice-key",
                "source_message_key": "v3-voice-source",
                "sender_role_hint": "self",
                "message_type": "voice",
                "content": "我马上回去。",
                "item_state": "completed",
                "flow_state": "completed",
                    "message_position": {
                        "screen_order": 2,
                        "visual_top": 240,
                        "visual_bottom": 308,
                        "frame_source": "final_read",
                        "order_source": "visual_top",
                    },
                "raw_payload": {
                    **_v3_raw_fields("v3-voice-source"),
                    "voice_transcription": "后端不应改用这里的旧值",
                    "observation": {
                        "schema_version": 3,
                        "observation_id": "v3-voice-observation",
                        "row_kind": "voice_transcript",
                        "sender_role": "self",
                        "sender_role_source": "parent_voice",
                        "message_type": "voice",
                        "voice_state": "transcribed",
                        "content_clean": "我马上回去。",
                        "parent_voice_anchor_key": "voice:self:4:v3",
                        "source_message": {
                            "id": "v3-voice-observation",
                            "type": "voice",
                            "content": "我马上回去。",
                            "voice_anchor_stable_key": "voice:self:4:v3",
                        },
                    },
                },
            },
        ],
    }
    payload["evidence"] = {
        "contract_revision": contract_revision(),
        "contract_sha256": contract_sha256(),
        "observation_schema_version": int(c2_contract_v3()["observation_schema_version"]),
        "authoritative_frame_source": "final_read",
        "observations": [payload["messages"][0]["raw_payload"]["observation"]],
            "read_reason": "waiting_user_reply",
            "authorization_read_reason": "waiting_user_reply",
            "finished_at": utcnow().isoformat(),
        "flow_gate_errors": [],
        "flow_gate_details": [],
        "slot_ledger_states": [
            {
                "observation_id": "v3-voice-observation",
                "screen_order": 2,
                "order_source": "visual_top",
                "row_kind": "voice_transcript",
                "source_message_key": "v3-voice-source",
                "origin_read_run_id": "read-v3",
                "fact_scope": "current_read_run",
                "delivery_state": "not_enqueued",
                "item_state": "completed",
            }
        ],
        "sequence_alignment_evidence": {
            "pre_sequence_source": "empty_checkpoint",
            "pre_frame_id": f"checkpoint:none:{binding['conversation_id']}",
            "post_frame_id": "frame:read-v3",
            "alignment_status": "not_required",
            "candidate_alignment_count": 0,
            "matched_pairs": [],
            "old_tail_fully_consumed": True,
            "new_suffix_observation_ids": ["v3-voice-observation"],
        },
        }
    accepted = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert accepted.status_code == 200
    assert accepted.json()["data"]["ingested_count"] == 1
    assert accepted.json()["data"]["ignored_count"] == 0
    wrong_contract = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={**payload, "contract_sha256": "0" * 64},
        headers=_worker_headers(worker),
    )
    assert wrong_contract.status_code == 409
    assert wrong_contract.json()["code"] == "MESSAGE_CONTRACT_SHA256_MISMATCH"
    stale_contract_payload = copy.deepcopy(payload)
    stale_contract_payload["contract_revision"] = "0.9.19"
    stale_contract_payload["evidence"]["contract_revision"] = "0.9.19"
    stale_contract = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=stale_contract_payload,
        headers=_worker_headers(worker),
    )
    assert stale_contract.status_code == 409
    assert (
        stale_contract.json()["code"]
        == "MESSAGE_CONTRACT_REVISION_MISMATCH"
    )
    with SessionLocal() as db:
        event = db.query(MessageEvent).filter(MessageEvent.dedupe_key == "v3-voice-key").one()
        assert event.content == "我马上回去。"
        assert event.raw_payload["message_position"] == {
            "screen_order": 2,
            "visual_top": 240,
            "visual_bottom": 308,
            "frame_source": "final_read",
            "order_source": "visual_top",
        }
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.allow_listening = False
        binding_row.listen_status = "disabled"
        binding_row.authorization_revision += 1
        db.commit()
        binding_row.allow_listening = True
        binding_row.listen_status = "listening"
        binding_row.authorization_revision += 1
        db.commit()

    stale_payload = copy.deepcopy(payload)
    stale_payload["read_run_id"] = "read-v3-stale"
    stale_payload["messages"][0]["dedupe_key"] = "v3-stale-key"
    stale_payload["messages"][0]["source_message_key"] = "v3-stale-source"
    stale_payload["messages"][0]["raw_payload"][
        "source_message_key"
    ] = "v3-stale-source"
    stale_payload["evidence"]["slot_ledger_states"][0].update(
        {
            "source_message_key": "v3-stale-source",
            "origin_read_run_id": "read-v3-stale",
        }
    )
    stale_payload["evidence"]["sequence_alignment_evidence"][
        "post_frame_id"
    ] = "frame:read-v3-stale"
    stale = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=stale_payload,
        headers=_worker_headers(worker),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "MESSAGE_AUTHORIZATION_REVISION_EXPIRED"


def test_worker_v3_five_second_voice_transcript_is_accepted_by_backend():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("五秒语音客户", "13896676682")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        db.commit()
    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    target_payload = targets.json()["data"]["targets"][0]
    worker_target = WorkerWechatReadTarget.from_api(target_payload)
    voice_anchor_key = "voice:customer:5:v16-101"
    transcript = "我想看看这辆车"
    sidecar_messages = [
        {
            "id": "voice-transcript-five-seconds",
            "source_adapter": "win32_ocr",
            "type": "voice",
            "sender_role": "customer",
            "content": transcript,
            "voice_duration": 5,
            "voice_anchor_stable_key": voice_anchor_key,
            "bubble_rect": [420, 220, 700, 264],
            # Real bound transcripts retain the parent bubble's avatar
            # evidence. The sidecar must still emit parent_voice.
            "avatar_alignment": {"role": "customer"},
            "sender_role_evidence": ["avatar_row_structure_confirmed"],
        }
    ]
    observations = build_message_observations_v3(sidecar_messages)
    assert observations[0]["sender_role_source"] == "parent_voice"
    read_run_id = "read-worker-v3-five-second-voice"
    stable_id = "worker-message-1"
    observation_id = str(observations[0]["observation_id"])
    action_mapping = {
        "canonical_action_id": "voice-action-five-second",
        "reserved_worker_stable_id": stable_id,
        "selected_action_token": "voice-token-five-second",
        "pre_observation_id": "voice-pre-five-second",
        "trigger_observation_id": "voice-trigger-five-second",
        "post_observation_id": observation_id,
        "physical_identity_inherited_from_prepare": False,
        "physical_action_count": 1,
        "result_candidate_count": 1,
        "stable_business_content_signature": (
            stable_business_content_signature(observations[0])
        ),
        "result_screen_order": 0,
        "binding_confirmed": True,
    }
    observations[0].update(
        {
            "_worker_stable_id": stable_id,
            "_worker_identity_scope": "committed",
            "_worker_voice_action_summary": {
                "confirmed_action_mapping": action_mapping,
            },
            "_worker_committed_message": committed_identity_record(
                worker_stable_id=stable_id,
                commit_basis=MessageCommitBasis.CONFIRMED_VOICE_ACTION,
                observation_id=observation_id,
                sender_role="customer",
                message_type="voice",
                proof=action_mapping,
            ),
        }
    )
    worker_payload = build_worker_message_ingest_payload(
        worker_target,
        {
            "ok": True,
            **_v3_contract_fields(),
            "authoritative_frame_source": "final_read",
            "observations": observations,
            "slot_ledger_states": [
                {
                    "observation_id": observations[0]["observation_id"],
                    "screen_order": 1,
                    "order_source": "observation_index_fallback",
                    "row_kind": observations[0]["row_kind"],
                    "source_message_key": voice_observation_source_key(
                        worker_target, observations[0]
                    ),
                    "origin_read_run_id": read_run_id,
                    "fact_scope": "current_read_run",
                    "delivery_state": "not_enqueued",
                    "item_state": "completed",
                }
            ],
            "sequence_alignment_evidence": {
                "pre_sequence_source": "empty_checkpoint",
                "pre_frame_id": (
                    f"checkpoint:none:{binding['conversation_id']}"
                ),
                "post_frame_id": f"frame:{read_run_id}",
                "alignment_status": "not_required",
                "candidate_alignment_count": 0,
                "matched_pairs": [],
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [
                    observations[0]["observation_id"]
                ],
            },
            "voice_transcription": {
                **_voice_action_evidence(
                    action_id="voice-action-five-second",
                    stable_id=observations[0]["_worker_stable_id"],
                    action_token="voice-token-five-second",
                    pre_observation_id="voice-pre-five-second",
                    trigger_observation_id=(
                        "voice-trigger-five-second"
                    ),
                    post_observation_id=observation_id,
                    content_signature=action_mapping[
                        "stable_business_content_signature"
                    ],
                    result_screen_order=0,
                ),
                "attempt_count": 1,
                "quality_flags": [],
                "transcribed_messages": sidecar_messages,
            },
        },
        read_run_id=read_run_id,
    )

    assert len(worker_payload["messages"]) == 1
    worker_message = worker_payload["messages"][0]
    assert worker_message["message_type"] == "voice"
    assert worker_message["content"] == transcript
    assert worker_message["raw_payload"]["observation"]["row_kind"] == "voice_transcript"
    assert worker_message["raw_payload"]["observation"]["sender_role_source"] == "parent_voice"
    commit_evidence = worker_message["raw_payload"][
        "message_commit_evidence"
    ]
    assert commit_evidence["commit_basis"] == "confirmed_voice_action"
    assert commit_evidence["action_receipt"]["binding_confirmed"] is True
    assert commit_evidence["reply_fact_evidence"]["voice_duration"] == "5"

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=worker_payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    response_data = response.json()["data"]
    assert response_data["ingested_count"] == 1
    assert response_data["ignored_count"] == 0
    batch_id = response_data["message_batch"]["batch_id"]
    status = client.get(
        f"/api/workers/{worker['id']}/wechat/message-batches/{batch_id}",
        headers=_worker_headers(worker),
    )
    assert status.status_code == 200, status.text
    checkpoint = status.json()["data"]["pre_send_fact_checkpoint"]
    frozen_voice = checkpoint["committed_tail"][0]
    assert checkpoint["checkpoint_revision"] == 5
    assert "continuity_basis" not in frozen_voice
    assert "physical_identity_confirmed" not in frozen_voice
    assert frozen_voice["commit_basis"] == "confirmed_voice_action"
    assert len(frozen_voice["action_receipt_digest"]) == 64
    assert set(frozen_voice["business_projection"]) == {
        "screen_order",
        "sender_role",
        "message_type",
        "normalized_content_signature",
        "media_state",
    }
    assert frozen_voice["message_identity_commit_record"][
        "commit_basis"
    ] == "confirmed_voice_action"
    fresh_same_transcript = copy.deepcopy(observations[0])
    fresh_same_transcript.pop("_worker_stable_id", None)
    fresh_same_transcript.pop("_worker_identity_scope", None)
    fresh_same_transcript.pop("_worker_voice_action_summary", None)
    fresh_same_transcript.pop("_worker_committed_message", None)
    fresh_same_transcript["observation_id"] = (
        "new-physical-voice-same-transcript"
    )
    fresh_same_transcript["parent_voice_anchor_key"] = (
        "voice:customer:5:new-physical"
    )
    comparison = worker_compare_checkpoint(
        checkpoint,
        [fresh_same_transcript],
        before_frame_id="checkpoint:backend-voice",
        after_frame_id="frame:worker-new-physical-voice",
        current_tail_complete=True,
    )
    assert comparison["comparison_result"] == "checkpoint_equal"
    assert comparison["physical_identity_confirmed"] is False
    assert comparison["terminal_fact_equivalence_count"] == 0
    assert comparison["matched_pairs"][0]["worker_stable_id"] == ""
    assert comparison["matched_pairs"][0]["match_basis"] == (
        "worker_business_viewport_continuity"
    )
    assert "_worker_stable_id" not in fresh_same_transcript
    assert "_worker_committed_message" not in fresh_same_transcript

    changed_fact = copy.deepcopy(fresh_same_transcript)
    changed_fact["observation_id"] = "changed-terminal-voice"
    changed_fact["content_clean"] = "我想看看另一辆车"
    changed = worker_compare_checkpoint(
        checkpoint,
        [changed_fact],
        before_frame_id="checkpoint:backend-voice",
        after_frame_id="frame:worker-changed-voice",
        current_tail_complete=True,
    )
    assert changed["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )
    changed = worker_compare_checkpoint(
        checkpoint,
        [changed_fact],
        before_frame_id="checkpoint:backend-voice",
        after_frame_id="frame:worker-changed-voice-expanded",
        current_tail_complete=True,
        context_expansion_used=True,
        expanded_context_observations=[changed_fact],
    )
    assert changed["comparison_result"] == "checkpoint_not_continuous"
    assert changed["reason"] == "expanded_context_not_continuous"

    untrusted_message = {
        **worker_message,
        "dedupe_key": "voice-transcript-untrusted-role-source",
        "source_message_key": "voice-transcript-untrusted-role-source",
        "raw_payload": {
            **worker_message["raw_payload"],
            **_v3_raw_fields("voice-transcript-untrusted-role-source"),
            "observation": {
                **worker_message["raw_payload"]["observation"],
                "sender_role_source": "same_row_avatar",
            },
        },
    }
    untrusted_read_run_id = "read-v3-untrusted-voice-role"
    untrusted_payload = copy.deepcopy(worker_payload)
    untrusted_payload.update(
        read_run_id=untrusted_read_run_id,
        messages=[untrusted_message],
    )
    untrusted_payload["evidence"]["observations"] = [
        copy.deepcopy(untrusted_message["raw_payload"]["observation"])
    ]
    untrusted_payload["evidence"]["slot_ledger_states"][0].update(
        source_message_key=untrusted_message["source_message_key"],
        origin_read_run_id=untrusted_read_run_id,
    )
    untrusted_payload["evidence"]["sequence_alignment_evidence"].update(
        post_frame_id=f"frame:{untrusted_read_run_id}",
    )
    untrusted_response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=untrusted_payload,
        headers=_worker_headers(worker),
    )
    assert untrusted_response.status_code == 409
    assert untrusted_response.json()["code"] == "MESSAGE_ROW_ROLE_SOURCE_UNTRUSTED"

    with SessionLocal() as db:
        event = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
        assert event.message_type == "voice"
        assert event.sender_role == "customer"
        assert event.content == transcript
        assert event.raw_payload["observation"]["row_kind"] == "voice_transcript"
        assert event.raw_payload["voice_transcription_meta"]["message"]["voice_duration"] == 5


def test_worker_committed_terminal_image_without_strong_identity_fails_closed_instead_of_reusing_pixels():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("末尾图片客户", "13896676683")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        db.commit()
    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    worker_target = WorkerWechatReadTarget.from_api(
        targets.json()["data"]["targets"][0]
    )
    read_run_id = "read-worker-v3-terminal-image"
    stable_id = "worker-message-1"
    exact_digest = "a" * 64
    fingerprint = f"imagev2:0123456789abcdef:{exact_digest}"
    template = _v3_message(
        "terminal-image-template",
        role="customer",
        message_type="image",
        content="客户发送了一张车辆图片",
        screen_order=1,
    )
    observation = copy.deepcopy(template["raw_payload"]["observation"])
    observation.update(
        {
            "observation_id": "terminal-image-observation",
            "source_adapter": "win32_ocr",
            "item_state": "completed",
            "image_physical_anchor": {
                "sender_role": "customer",
                "bubble_visual_fingerprint": fingerprint,
                "preceding_stable_message": "",
                "following_stable_message": "",
                "occurrence_index": 0,
                "occurrence_count": 1,
            },
            "source_message": {
                **observation["source_message"],
                "id": "ocr-frame-local-terminal-image",
                "source_adapter": "win32_ocr",
                "image_physical_anchor": {
                    "bubble_visual_fingerprint": fingerprint,
                },
            },
            "_worker_stable_id": stable_id,
            "_worker_identity_scope": "committed",
        }
    )
    action_mapping = {
        "canonical_action_id": "image-action-terminal",
        "reserved_worker_stable_id": stable_id,
        "pre_observation_id": "image-pre-terminal",
        "post_observation_id": observation["observation_id"],
        "trigger_observation_id": observation["observation_id"],
        "physical_identity_inherited_from_prepare": False,
        "result_screen_order": 0,
        "binding_confirmed": True,
    }
    observation["_worker_image_action_summary"] = {
        "confirmed_action_mapping": dict(action_mapping),
        "image_sha256": exact_digest,
        "result_screen_order": 0,
    }
    observation["_worker_committed_message"] = committed_identity_record(
        worker_stable_id=stable_id,
        commit_basis=MessageCommitBasis.CONFIRMED_IMAGE_ACTION,
        observation_id=observation["observation_id"],
        sender_role="customer",
        message_type="image",
        proof={**action_mapping, "image_sha256": exact_digest},
    )
    source_key = image_observation_source_key(worker_target, observation)
    worker_payload = build_worker_message_ingest_payload(
        worker_target,
        {
            "ok": True,
            **_v3_contract_fields(),
            "authoritative_frame_source": "final_read",
            "observations": [observation],
            "slot_ledger_states": [
                {
                    "observation_id": observation["observation_id"],
                    "screen_order": 1,
                    "order_source": "observation_index_fallback",
                    "row_kind": observation["row_kind"],
                    "source_message_key": source_key,
                    "origin_read_run_id": read_run_id,
                    "fact_scope": "current_read_run",
                    "delivery_state": "not_enqueued",
                    "item_state": "completed",
                }
            ],
            "sequence_alignment_evidence": {
                "pre_sequence_source": "empty_checkpoint",
                "pre_frame_id": f"checkpoint:none:{binding['conversation_id']}",
                "post_frame_id": f"frame:{read_run_id}",
                "alignment_status": "not_required",
                "candidate_alignment_count": 0,
                "matched_pairs": [],
                "old_tail_fully_consumed": True,
                "new_suffix_observation_ids": [
                    observation["observation_id"]
                ],
            },
        },
        read_run_id=read_run_id,
    )
    assert len(worker_payload["messages"]) == 1
    evidence = worker_payload["messages"][0]["raw_payload"][
        "message_commit_evidence"
    ]
    assert evidence["commit_basis"] == "confirmed_image_action"
    assert evidence["reply_fact_evidence"][
        "exact_image_content_sha256"
    ] == exact_digest

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=worker_payload,
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200, response.text
    batch_id = response.json()["data"]["message_batch"]["batch_id"]
    status = client.get(
        f"/api/workers/{worker['id']}/wechat/message-batches/{batch_id}",
        headers=_worker_headers(worker),
    )
    assert status.status_code == 200, status.text
    checkpoint = status.json()["data"]["pre_send_fact_checkpoint"]
    frozen_image = checkpoint["committed_tail"][0]
    assert "continuity_basis" not in frozen_image
    assert "physical_identity_confirmed" not in frozen_image
    assert frozen_image["commit_basis"] == "confirmed_image_action"
    assert len(frozen_image["action_receipt_digest"]) == 64
    assert set(frozen_image["business_projection"]) == {
        "screen_order",
        "sender_role",
        "message_type",
        "normalized_content_signature",
        "media_state",
    }
    assert frozen_image["message_identity_commit_record"][
        "commit_basis"
    ] == "confirmed_image_action"

    fresh = copy.deepcopy(observation)
    for key in (
        "_worker_stable_id",
        "_worker_identity_scope",
        "_worker_image_action_summary",
        "_worker_committed_message",
    ):
        fresh.pop(key, None)
    fresh["observation_id"] = "fresh-terminal-image-no-native-id"
    comparison = worker_compare_checkpoint(
        checkpoint,
        [fresh],
        before_frame_id="checkpoint:backend-image",
        after_frame_id="frame:worker-same-image",
        current_tail_complete=True,
    )
    assert comparison["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )
    comparison = worker_compare_checkpoint(
        checkpoint,
        [fresh],
        before_frame_id="checkpoint:backend-image",
        after_frame_id="frame:worker-same-image-expanded",
        current_tail_complete=True,
        context_expansion_used=True,
        expanded_context_observations=[fresh],
    )
    assert comparison["comparison_result"] == "checkpoint_not_continuous"
    assert comparison["reason"] == "expanded_context_not_continuous"
    assert comparison["matched_pairs"] == []
    assert "_worker_stable_id" not in fresh

    similar = copy.deepcopy(fresh)
    similar["observation_id"] = "fresh-similar-but-not-exact-image"
    similar["image_physical_anchor"]["bubble_visual_fingerprint"] = (
        f"imagev2:0123456789abcdef:{'b' * 64}"
    )
    changed = worker_compare_checkpoint(
        checkpoint,
        [similar],
        before_frame_id="checkpoint:backend-image",
        after_frame_id="frame:worker-similar-image",
        current_tail_complete=True,
    )
    assert changed["comparison_result"] == (
        "checkpoint_continuity_context_expansion_required"
    )
    changed = worker_compare_checkpoint(
        checkpoint,
        [similar],
        before_frame_id="checkpoint:backend-image",
        after_frame_id="frame:worker-similar-image-expanded",
        current_tail_complete=True,
        context_expansion_used=True,
        expanded_context_observations=[similar],
    )
    assert changed["comparison_result"] == "checkpoint_not_continuous"
    assert changed["reason"] == "expanded_context_not_continuous"


def test_message_ingest_rejects_v2_before_any_source_processing():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]
    conversation_id = binding["conversation_id"]

    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-source-conflict",
        messages=[
            _v3_message("text-key", role="self", message_type="text", content="同一条消息", screen_order=1),
            _v3_message("voice-key", role="customer", message_type="voice", content="同一条消息", screen_order=2),
        ],
    )
    payload["contract_version"] = 2
    for message in payload["messages"]:
        message["source_message_key"] = "source-same-message"
        message["raw_payload"]["source_message_key"] = "source-same-message"

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "contract_version" in json.dumps(
        response.json()["data"]["errors"],
        ensure_ascii=False,
    )
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == conversation_id).count() == 0


def test_message_ingest_rejects_v2_before_identity_processing():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676679")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    v2_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-v2-contract",
        messages=[],
    )
    v2_payload["contract_version"] = 2
    rejected_v2 = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=v2_payload,
        headers=_worker_headers(worker),
    )
    assert rejected_v2.status_code == 409
    assert rejected_v2.json()["code"] == "MESSAGE_CONTRACT_V3_REQUIRED"

    missing_source_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-v3-no-source",
        messages=[_v3_message("v3-no-source", role="customer", message_type="text", content="你好", screen_order=1)],
    )
    del missing_source_payload["messages"][0]["source_message_key"]
    missing_source = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=missing_source_payload,
        headers=_worker_headers(worker),
    )
    assert missing_source.status_code == 400
    assert missing_source.json()["code"] == "VALIDATION_ERROR"
    assert (
        missing_source.json()["data"]["recovery_action"]
        == "capability_paused"
    )
    assert "terminal_confirmed" not in missing_source.json()["data"]
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0


def test_customer_v3_voice_is_deduped_and_collectable_for_c3():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-voice-001",
            messages=[
                _v3_message(
                    "voice-worker-key-001",
                    role="customer",
                    message_type="voice",
                    content="我想看看 SUV",
                    screen_order=1,
                    raw_extra={"voice_transcription": "我想看看 SUV", "voice_duration_seconds": 5},
                )
            ],
        ),
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200
    first_data = first.json()["data"]
    assert first_data["ingested_count"] == 1
    message_event_id = first_data["results"][0]["message_event_id"]
    assert first_data["results"][0]["dedupe_key"] == "voice-worker-key-001"

    duplicated = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-voice-002",
            messages=[
                _v3_message(
                    "voice-worker-key-001",
                    role="customer",
                    message_type="voice",
                    content="我想看看 SUV",
                    screen_order=1,
                    raw_extra={"voice_transcription": "我想看看 SUV", "voice_duration_seconds": 5},
                )
            ],
        ),
        headers=_worker_headers(worker),
    )
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["duplicated_count"] == 1
    assert duplicated.json()["data"]["results"][0]["error_code"] == "MESSAGE_INGEST_DUPLICATED"

    collected = client.post(
        f"/api/internal/conversations/{binding['conversation_id']}/message-batches/collect",
        json={"trigger_message_event_id": message_event_id, "trace_id": "trace-voice-customer"},
        headers={"X-Internal-Service-Token": "dev-only-internal-service-token-change-before-production"},
    )
    assert collected.status_code == 200
    assert collected.json()["data"]["batch_status"] == "reply_action_created"

    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
        batch = db.get(MessageBatch, collected.json()["data"]["batch_id"])
        assert message.message_type == "voice"
        assert message.content == "我想看看 SUV"
        assert message.raw_payload["voice_transcription"] == "我想看看 SUV"
        assert batch.message_event_ids == [message.id]


def test_equal_voice_transcripts_with_distinct_anchor_keys_are_both_ingested():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-equal-voice-anchors",
            messages=[
                _v3_message(
                    f"{binding['conversation_id']}:voice-anchor-a",
                    role="customer",
                    message_type="voice",
                    content="好的",
                    screen_order=1,
                ),
                _v3_message(
                    f"{binding['conversation_id']}:voice-anchor-b",
                    role="customer",
                    message_type="voice",
                    content="好的",
                    screen_order=2,
                ),
            ],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 2
    with SessionLocal() as db:
        messages = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).all()
        assert len(messages) == 2
        assert {message.dedupe_key for message in messages} == {
            f"{binding['conversation_id']}:voice-anchor-a",
            f"{binding['conversation_id']}:voice-anchor-b",
        }


def test_sales_voice_transcription_pauses_takeover_without_disabling_ai_or_creating_batch():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-sales-voice",
            messages=[
                _v3_message(
                    "sales-voice-worker-key",
                    role="self",
                    message_type="voice",
                    content="我来跟进",
                    screen_order=1,
                )
            ],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 1
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert message.sender_role == "self"
        assert message.message_type == "voice"
        assert conversation.status == "sales_replied_waiting_user"
        assert conversation.ai_enabled is True
        assert db.query(MessageBatch).count() == 0
        assert db.query(ReplyAction).count() == 0


def test_unconfirmed_ai_receipt_survives_more_than_24_hours_until_bubble_ingest():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    reply_text = "您好，可以继续沟通这台车。"
    sent_at = utcnow() - timedelta(days=2)
    with SessionLocal() as db:
        action = ReplyAction(
            batch_id="batch-ai-observation",
            conversation_id=binding["conversation_id"],
            status="sent",
            current=False,
            generation_no=1,
            decision="send_reply",
            reply_text=reply_text,
            reply_text_hash=hashlib.sha256(reply_text.encode("utf-8")).hexdigest(),
            sent_at=sent_at,
        )
        db.add(action)
        db.flush()
        action_id = action.id
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = sent_at
        db.commit()

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-ai-self-observation",
            messages=[
                _v3_message(
                    "ai-self-observation",
                    role="self",
                    message_type="text",
                    content=reply_text,
                    screen_order=1,
                    raw_extra={
                        "ai_reply_receipt": {
                            "reply_action_id": action_id,
                            "reply_text_hash": hashlib.sha256(reply_text.encode("utf-8")).hexdigest(),
                            "worker_stable_id": "worker-message-ai-1",
                            "source_message_key": "ai-self-observation",
                            "confirmed_at": sent_at.isoformat(),
                        }
                    },
                )
            ],
            read_reason="recent_ai_sent",
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert message.raw_payload["sender_source"] == "ai"
        assert message.raw_payload["ai_reply_action_id"]
        assert conversation.status == "waiting_user_reply"
        assert conversation.last_sales_reply_at is None
        assert db.query(MessageBatch).count() == 0


def test_valid_ai_receipt_while_sent_ack_is_pending_is_not_human_sales():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("发送回执恢复客户", "13896676690")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    reply_text = "这条消息已发到微信，回执正在重试。"
    confirmed_at = utcnow()
    with SessionLocal() as db:
        action = ReplyAction(
            batch_id="batch-ai-pending-ack",
            conversation_id=binding["conversation_id"],
            status="sending",
            current=True,
            generation_no=1,
            decision="send_reply",
            reply_text=reply_text,
            reply_text_hash=hashlib.sha256(reply_text.encode("utf-8")).hexdigest(),
            sending_claimed_at=confirmed_at,
        )
        db.add(action)
        db.flush()
        action_id = action.id
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        db.commit()

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-ai-pending-sent-ack",
            messages=[
                _v3_message(
                    "ai-self-pending-ack",
                    role="self",
                    message_type="text",
                    content=reply_text,
                    screen_order=1,
                    raw_extra={
                        "ai_reply_receipt": {
                            "reply_action_id": action_id,
                            "reply_text_hash": hashlib.sha256(
                                reply_text.encode("utf-8")
                            ).hexdigest(),
                            "worker_stable_id": "worker-ai-pending-ack",
                            "source_message_key": "ai-self-pending-ack",
                            "confirmed_at": confirmed_at.isoformat(),
                        }
                    },
                )
            ],
            read_reason="waiting_user_reply",
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert message.raw_payload["sender_source"] == "ai_pending_ack"
        assert message.raw_payload["ai_reply_action_id"] == action_id
        assert conversation.status == "waiting_user_reply"
        assert conversation.last_sales_reply_at is None
        assert db.query(HandoffEvent).count() == 0


def test_unknown_send_reconciled_bubble_never_closes_handoff_as_human_sales():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("发送结果待核对客户", "13896676691")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    reply_text = "这条回复可能已经发送，需要和微信气泡核对。"
    claimed_at = utcnow()
    with SessionLocal() as db:
        action = ReplyAction(
            batch_id="batch-ai-unknown-send",
            conversation_id=binding["conversation_id"],
            status="unknown_send_result",
            current=False,
            generation_no=1,
            decision="send_reply",
            reply_text=reply_text,
            reply_text_hash=hashlib.sha256(
                reply_text.encode("utf-8")
            ).hexdigest(),
            sending_claimed_at=claimed_at,
        )
        db.add(action)
        db.flush()
        action_id = action.id
        handoff = HandoffEvent(
            conversation_id=binding["conversation_id"],
            status="created",
            handoff_reason_code="SEND_RESULT_UNKNOWN",
        )
        db.add(handoff)
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_sales_reply"
        db.commit()
        handoff_id = handoff.id

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-ai-unknown-reconcile",
            messages=[
                _v3_message(
                    "ai-self-unknown-reconcile",
                    role="self",
                    message_type="text",
                    content=reply_text,
                    screen_order=1,
                    raw_extra={
                        "ai_reply_receipt": {
                            "reply_action_id": action_id,
                            "reply_text_hash": hashlib.sha256(
                                reply_text.encode("utf-8")
                            ).hexdigest(),
                            "worker_stable_id": "worker-message-unknown-1",
                            "source_message_key": "ai-self-unknown-reconcile",
                            "confirmed_at": "",
                            "reconciliation_state": "ai_unreconciled",
                        }
                    },
                )
            ],
            read_reason="waiting_sales_reply",
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert message.raw_payload["sender_source"] == "ai_unreconciled"
        assert message.raw_payload["ai_reply_action_id"] == action_id
        assert conversation.status == "waiting_sales_reply"
        assert conversation.last_sales_reply_at is None
        assert handoff.status == "created"
        assert handoff.closed_at is None


def test_unknown_send_without_local_receipt_never_rebinds_by_message_body():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("本地凭证丢失客户", "13896676692")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    reply_text = "这条回复发送结果未知。"
    with SessionLocal() as db:
        action = ReplyAction(
            batch_id="batch-ai-unknown-local-receipt-lost",
            conversation_id=binding["conversation_id"],
            status="unknown_send_result",
            current=False,
            generation_no=1,
            decision="send_reply",
            reply_text=reply_text,
            reply_text_hash=hashlib.sha256(
                reply_text.encode("utf-8")
            ).hexdigest(),
            sending_claimed_at=utcnow(),
        )
        db.add(action)
        db.flush()
        action_id = action.id
        handoff = HandoffEvent(
            conversation_id=binding["conversation_id"],
            status="created",
            handoff_reason_code="SEND_RESULT_UNKNOWN",
        )
        db.add(handoff)
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_sales_reply"
        db.commit()
        handoff_id = handoff.id

    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-ai-unknown-local-receipt-lost",
            messages=[
                _v3_message(
                    "ai-self-unknown-local-receipt-lost",
                    role="self",
                    message_type="text",
                    content=reply_text,
                    screen_order=1,
                )
            ],
            read_reason="waiting_sales_reply",
        ),
        headers=_worker_headers(worker),
    )

    assert first.status_code == 200, first.text
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert (
            message.raw_payload["sender_source"]
            == "ai_identity_unconfirmed_guard"
        )
        assert "ai_reply_action_id" not in message.raw_payload
        assert conversation.status == "waiting_sales_reply"
        assert conversation.last_sales_reply_at is None
        assert handoff.closed_at is None

    second = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-human-same-text-after-server-guard",
            messages=[
                _v3_message(
                    "human-same-text-after-server-guard",
                    role="self",
                    message_type="text",
                    content="这是一条正文完全不同的右侧消息。",
                    screen_order=1,
                )
            ],
            read_reason="waiting_sales_reply",
        ),
        headers=_worker_headers(worker),
    )

    assert second.status_code == 200, second.text
    with SessionLocal() as db:
        messages = db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).order_by(MessageEvent.ingested_at.asc()).all()
        assert (
            messages[1].raw_payload["sender_source"]
            == "ai_identity_unconfirmed_guard"
        )
        assert all(
            "ai_reply_action_id" not in message.raw_payload
            for message in messages
        )


def test_sent_action_without_stable_receipt_keeps_conversation_identity_guarded():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    reply_text = "好的，我帮您确认一下"
    with SessionLocal() as db:
        action = ReplyAction(
                batch_id="batch-old-ai-text",
                conversation_id=binding["conversation_id"],
                status="sent",
                current=False,
                generation_no=1,
                decision="send_reply",
                reply_text=reply_text,
                reply_text_hash=hashlib.sha256(reply_text.encode("utf-8")).hexdigest(),
                sent_at=utcnow(),
            )
        db.add(action)
        db.commit()

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-human-same-as-ai",
            messages=[
                _v3_message(
                    "human-same-as-ai",
                    role="self",
                    message_type="text",
                    content=reply_text,
                    screen_order=1,
                )
            ],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).one()
        conversation = db.get(Conversation, binding["conversation_id"])
        assert (
            message.raw_payload["sender_source"]
            == "ai_identity_unconfirmed_guard"
        )
        assert "ai_reply_action_id" not in message.raw_payload
        assert conversation.status != "sales_replied_waiting_user"
        assert conversation.last_sales_reply_at is None


def test_consumed_sent_action_does_not_guard_later_human_sales_message():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("已消费发送回执客户", "13896676679")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    reply_text = "这是已经稳定入库的 AI 回复"
    sent_at = utcnow()
    with SessionLocal() as db:
        action = ReplyAction(
            batch_id="batch-consumed-ai-send",
            conversation_id=binding["conversation_id"],
            status="sent",
            current=False,
            generation_no=1,
            decision="send_reply",
            reply_text=reply_text,
            reply_text_hash=hashlib.sha256(
                reply_text.encode("utf-8")
            ).hexdigest(),
            sent_at=sent_at,
        )
        db.add(action)
        db.flush()
        action_id = action.id
        db.commit()

    receipt_ingest = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-consumed-ai-send",
            messages=[
                _v3_message(
                    "consumed-ai-send",
                    role="self",
                    message_type="text",
                    content=reply_text,
                    screen_order=1,
                    raw_extra={
                        "ai_reply_receipt": {
                            "reply_action_id": action_id,
                            "reply_text_hash": hashlib.sha256(
                                reply_text.encode("utf-8")
                            ).hexdigest(),
                            "worker_stable_id": "worker-message-ai-consumed",
                            "source_message_key": "consumed-ai-send",
                            "confirmed_at": sent_at.isoformat(),
                        }
                    },
                )
            ],
        ),
        headers=_worker_headers(worker),
    )
    assert receipt_ingest.status_code == 200, receipt_ingest.text

    human_ingest = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-human-after-consumed-ai-send",
            messages=[
                _v3_message(
                    "human-after-consumed-ai-send",
                    role="self",
                    message_type="text",
                    content="这是后续销售人工回复",
                    screen_order=1,
                )
            ],
        ),
        headers=_worker_headers(worker),
    )
    assert human_ingest.status_code == 200, human_ingest.text
    with SessionLocal() as db:
        messages = db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).order_by(MessageEvent.ingested_at.asc()).all()
        assert messages[0].raw_payload["sender_source"] == "ai"
        assert messages[1].raw_payload["sender_source"] == "human"
        assert (
            messages[1].raw_payload.get("ai_reply_action_id") is None
        )


def test_failed_customer_image_before_later_human_sales_reply_does_not_reopen_handoff():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-image-failure-before-sales",
        messages=[
            _v3_failed_image_message(
                "failed-image-before-sales",
                role="customer",
                screen_order=1,
                reason="VISION_MODEL_TIMEOUT",
                order_source="visual_top",
            ),
            _v3_message(
                "human-sales-after-image",
                role="self",
                message_type="text",
                content="我已经人工处理了",
                screen_order=2,
                order_source="visual_top",
            ),
        ],
    )
    payload["evidence"]["flow_gate_errors"] = ["C2_IMAGE_UNDERSTANDING_FAILED"]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_IMAGE_UNDERSTANDING_FAILED",
            "position_source": "failed_image_visual_top",
            "min_screen_order": 1,
            "max_screen_order": 1,
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    replay_response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert replay_response.status_code == 200, replay_response.text
    assert replay_response.json()["data"]["duplicated_count"] == 2
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "sales_replied_waiting_user"
        assert db.query(HandoffEvent).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_failed_customer_voice_before_later_human_sales_reply_does_not_reopen_handoff():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("语音失败后已回复客户", "13896676688")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-voice-failure-before-sales",
        messages=[
            _v3_failed_voice_message(
                "failed-customer-voice-before-sales",
                role="customer",
                screen_order=1,
                reason="VOICE_TRANSCRIBE_PARTIAL",
                order_source="visual_top",
            ),
            _v3_message(
                "human-sales-after-failed-voice",
                role="self",
                message_type="text",
                content="这条语音我已经人工处理了",
                screen_order=2,
                order_source="visual_top",
            ),
        ],
    )
    payload["evidence"]["flow_gate_errors"] = ["C2_VOICE_TRANSCRIBE_FAILED"]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_VOICE_TRANSCRIBE_FAILED",
            "position_source": "failed_voice_visual_top",
            "subject_sender_role": "customer",
            "min_screen_order": 1,
            "max_screen_order": 1,
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    replay_response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert replay_response.status_code == 200, replay_response.text
    assert replay_response.json()["data"]["duplicated_count"] == 2
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        failed_voice = db.query(MessageEvent).filter(
            MessageEvent.message_type == "voice",
            MessageEvent.item_state == "failed",
        ).one()
        assert failed_voice.sender_role == "customer"
        assert failed_voice.error_code == "VOICE_TRANSCRIBE_PARTIAL"
        assert conversation.status == "sales_replied_waiting_user"
        assert db.query(HandoffEvent).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_failed_sales_voice_is_human_intervention_and_cannot_trigger_brain():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("销售语音失败客户", "13896676689")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-customer-success-sales-voice-failed",
        messages=[
            _v3_message(
                "customer-voice-completed-before-sales",
                role="customer",
                message_type="voice",
                content="客户语音已经成功",
                screen_order=1,
                order_source="visual_top",
            ),
            _v3_failed_voice_message(
                "failed-sales-voice",
                role="self",
                screen_order=2,
                reason="VOICE_TRANSCRIBE_PARTIAL",
                order_source="visual_top",
            ),
        ],
    )
    payload["evidence"]["flow_gate_errors"] = ["C2_VOICE_TRANSCRIBE_FAILED"]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_VOICE_TRANSCRIBE_FAILED",
            "position_source": "failed_voice_visual_top",
            "subject_sender_role": "self",
            "min_screen_order": 2,
            "max_screen_order": 2,
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    replay_response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert replay_response.status_code == 200, replay_response.text
    assert replay_response.json()["data"]["duplicated_count"] == 2
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        failed_sales_voice = db.query(MessageEvent).filter(
            MessageEvent.message_type == "voice",
            MessageEvent.item_state == "failed",
        ).one()
        assert failed_sales_voice.sender_role == "self"
        assert failed_sales_voice.error_code == "VOICE_TRANSCRIBE_PARTIAL"
        assert conversation.status == "sales_replied_waiting_user"
        assert db.query(HandoffEvent).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_customer_after_failed_sales_voice_continues_to_brain():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("销售失败语音后客户追问", "13896676690")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-sales-voice-failed-before-customer",
        messages=[
            _v3_failed_voice_message(
                "failed-sales-voice-before-customer",
                role="self",
                screen_order=1,
                reason="VOICE_TRANSCRIBE_PARTIAL",
                order_source="visual_top",
            ),
            _v3_message(
                "customer-after-failed-sales-voice",
                role="customer",
                message_type="text",
                content="您刚才说的是什么？",
                screen_order=2,
                order_source="visual_top",
            ),
        ],
    )
    payload["evidence"]["flow_gate_errors"] = ["C2_VOICE_TRANSCRIBE_FAILED"]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_VOICE_TRANSCRIBE_FAILED",
            "position_source": "failed_voice_visual_top",
            "subject_sender_role": "self",
            "min_screen_order": 1,
            "max_screen_order": 1,
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        failed_sales_voice = db.query(MessageEvent).filter(
            MessageEvent.message_type == "voice",
            MessageEvent.item_state == "failed",
        ).one()
        customer_message = db.query(MessageEvent).filter(
            MessageEvent.source_message_key
            == "customer-after-failed-sales-voice"
        ).one()
        batch = db.query(MessageBatch).one()
        assert failed_sales_voice.sender_role == "self"
        assert conversation.status == "ai_active"
        assert db.query(HandoffEvent).count() == 0
        assert batch.status == "reply_action_created"
        assert batch.message_event_ids == [customer_message.id]
        assert db.query(ReplyAction).filter(
            ReplyAction.batch_id == batch.id,
            ReplyAction.status == "queued",
        ).count() == 1


def test_customer_after_failed_sales_image_continues_to_brain():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("销售失败图片后客户追问", "13896676691")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-sales-image-failed-before-customer",
        messages=[
            _v3_failed_image_message(
                "failed-sales-image-before-customer",
                role="self",
                screen_order=1,
                reason="VISION_MODEL_TIMEOUT",
                order_source="visual_top",
            ),
            _v3_message(
                "customer-after-failed-sales-image",
                role="customer",
                message_type="text",
                content="这辆车还能看吗？",
                screen_order=2,
                order_source="visual_top",
            ),
        ],
    )
    payload["evidence"]["flow_gate_errors"] = [
        "C2_IMAGE_UNDERSTANDING_FAILED"
    ]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_IMAGE_UNDERSTANDING_FAILED",
            "position_source": "failed_image_visual_top",
            "subject_sender_role": "self",
            "min_screen_order": 1,
            "max_screen_order": 1,
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        failed_sales_image = db.query(MessageEvent).filter(
            MessageEvent.message_type == "image",
            MessageEvent.item_state == "failed",
        ).one()
        customer_message = db.query(MessageEvent).filter(
            MessageEvent.source_message_key
            == "customer-after-failed-sales-image"
        ).one()
        batch = db.query(MessageBatch).one()
        assert failed_sales_image.sender_role == "self"
        assert conversation.status == "ai_active"
        assert db.query(HandoffEvent).count() == 0
        assert batch.status == "reply_action_created"
        assert batch.message_event_ids == [customer_message.id]
        assert db.query(ReplyAction).filter(
            ReplyAction.batch_id == batch.id,
            ReplyAction.status == "queued",
        ).count() == 1


@pytest.mark.parametrize(
    "gate_code",
    ["C2_MESSAGE_HISTORY_GAP", "MESSAGE_IDENTITY_UNCONFIRMED"],
)
def test_older_safety_gate_cannot_override_later_human_sales_reply(gate_code: str):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"read-gate-before-sales-{gate_code}",
        messages=[
            _v3_message(
                f"customer-before-sales-{gate_code}",
                role="customer",
                message_type="text",
                content="这条消息已经由销售处理",
                screen_order=1,
                order_source="visual_top",
            ),
            _v3_message(
                f"human-sales-after-gate-{gate_code}",
                role="self",
                message_type="text",
                content="我已经人工回复了",
                screen_order=2,
                order_source="visual_top",
            ),
        ],
    )
    payload["evidence"]["flow_gate_errors"] = [gate_code]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": gate_code,
            "position_source": (
                "slot_ledger_visual_top"
                if gate_code == "C2_MESSAGE_HISTORY_GAP"
                else "identity_error_visual_top"
            ),
            "gate_scope": "reply_suffix",
            "min_screen_order": 1,
            "max_screen_order": 1,
            "boundary_relation": "before_or_equal",
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "sales_replied_waiting_user"
        assert db.query(HandoffEvent).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_safety_gate_rejects_screen_order_without_visual_position_source():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("弱门禁位置客户", "13896676684")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-weak-gate-position",
        messages=[
            _v3_message(
                "weak-gate-customer",
                role="customer",
                message_type="text",
                content="只有 OCR 顺序",
                screen_order=1,
            ),
        ],
    )
    payload["evidence"]["flow_gate_errors"] = ["C2_MESSAGE_HISTORY_GAP"]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_MESSAGE_HISTORY_GAP",
            "position_source": "position_unavailable",
            "min_screen_order": 1,
            "max_screen_order": 1,
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409, response.text
    assert (
        response.json()["code"]
        == "MESSAGE_FLOW_GATE_POSITION_SOURCE_UNTRUSTED"
    )
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0


def test_ingest_uses_screen_order_instead_of_json_array_order():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("画面顺序客户", "13896676679")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-json-order-reversed",
        messages=[
            _v3_message(
                "sales-screen-second",
                role="self",
                message_type="text",
                content="我已经回复客户",
                screen_order=2,
            ),
            _v3_message(
                "customer-screen-first",
                role="customer",
                message_type="text",
                content="请问还有货吗",
                screen_order=1,
            ),
        ],
    )
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data.get("message_batch") is None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "sales_replied_waiting_user"
        rows = (
            db.query(MessageEvent)
            .filter(MessageEvent.conversation_id == binding["conversation_id"])
            .order_by(MessageEvent.ingested_at.asc(), MessageEvent.id.asc())
            .all()
        )
        assert [row.sender_role for row in rows] == ["customer", "self"]
        assert db.query(MessageBatch).count() == 0


def test_ingest_rejects_duplicate_screen_order():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("重复顺序客户", "13896676680")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-duplicate-screen-order",
        messages=[
            _v3_message(
                "duplicate-order-a",
                role="customer",
                message_type="text",
                content="第一条",
                screen_order=1,
            ),
            _v3_message(
                "duplicate-order-b",
                role="self",
                message_type="text",
                content="第二条",
                screen_order=1,
            ),
        ],
    )
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert any(
        "screen_order 不能重复" in str(error.get("msg") or "")
        for error in response.json()["data"]["errors"]
    )
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0


def test_ingest_persists_each_slot_origin_read_run_id():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("轮次归属客户", "13896676689")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    current_read_run_id = "read-current-slot-origin"
    historical_read_run_id = "read-historical-slot-origin"
    messages = [
        _v3_message(
            "historical-slot-origin",
            role="customer",
            message_type="text",
            content="上一轮尚待确认的事实",
            screen_order=1,
        ),
        _v3_message(
            "current-slot-origin",
            role="customer",
            message_type="text",
            content="本轮新事实",
            screen_order=2,
        ),
    ]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=current_read_run_id,
        messages=messages,
    )
    payload["evidence"]["slot_ledger_states"] = [
        {
            "observation_id": "observation:historical-slot-origin",
            "screen_order": 1,
            "order_source": "observation_index_fallback",
            "row_kind": "text_bubble",
            "source_message_key": "historical-slot-origin",
            "origin_read_run_id": historical_read_run_id,
            "fact_scope": "historical",
            "delivery_state": "outbox_waiting",
            "item_state": "completed",
        },
        {
            "observation_id": "observation:current-slot-origin",
            "screen_order": 2,
            "order_source": "observation_index_fallback",
            "row_kind": "text_bubble",
            "source_message_key": "current-slot-origin",
            "origin_read_run_id": current_read_run_id,
            "fact_scope": "current_read_run",
            "delivery_state": "not_enqueued",
            "item_state": "completed",
        },
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        rows = {
            row.source_message_key: row.read_run_id
            for row in db.query(MessageEvent)
            .filter(
                MessageEvent.conversation_id == binding["conversation_id"]
            )
            .all()
        }
    assert rows == {
        "historical-slot-origin": historical_read_run_id,
        "current-slot-origin": current_read_run_id,
    }


def test_ingest_rejects_message_without_slot_read_run_ownership():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("缺少轮次归属客户", "13896676690")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-missing-slot-origin",
        messages=[
            _v3_message(
                "missing-slot-origin",
                role="customer",
                message_type="text",
                content="缺少槽位归属",
                screen_order=1,
            )
        ],
    )
    del payload["evidence"]["slot_ledger_states"]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0


def test_ingest_rejects_message_fact_without_sequence_alignment_evidence():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("缺少序列证据客户", "13896676691")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-missing-sequence-evidence",
        messages=[
            _v3_message(
                "missing-sequence-evidence",
                role="customer",
                message_type="text",
                content="缺少统一序列证据",
                screen_order=1,
            )
        ],
    )
    del payload["evidence"]["sequence_alignment_evidence"]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_ingest_rejects_alignment_post_index_that_points_to_another_observation():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("序列位置错配客户", "13896676694")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    messages = [
        _v3_message(
            "post-index-a",
            role="customer",
            message_type="text",
            content="第一条",
            screen_order=1,
        ),
        _v3_message(
            "post-index-b",
            role="customer",
            message_type="text",
            content="第二条",
            screen_order=2,
        ),
    ]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-post-index-mismatch",
        messages=messages,
    )
    observation_ids = [
        item["observation_id"]
        for item in payload["evidence"]["observations"]
    ]
    payload["evidence"]["sequence_alignment_evidence"] = {
        "pre_sequence_source": "checkpoint",
        "pre_frame_id": "checkpoint:post-index-mismatch",
        "post_frame_id": "frame:post-index-mismatch",
        "alignment_status": "unique",
        "candidate_alignment_count": 1,
        "matched_pairs": [
            {
                "identity_state": "frame_local_unselected",
                "worker_stable_id": None,
                "pre_observation_id": "pre-observation-b",
                "post_observation_id": observation_ids[1],
                "pre_index": 0,
                "post_index": 0,
                "match_basis": "business_sequence",
            }
        ],
        "old_tail_fully_consumed": True,
        "new_suffix_observation_ids": [],
    }

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_ingest_rejects_new_suffix_that_is_not_the_complete_contiguous_tail():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("新增后缀断裂客户", "13896676695")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    messages = [
        _v3_message(
            "suffix-a",
            role="customer",
            message_type="text",
            content="新增一",
            screen_order=1,
        ),
        _v3_message(
            "suffix-b",
            role="customer",
            message_type="text",
            content="新增二",
            screen_order=2,
        ),
    ]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-broken-new-suffix",
        messages=messages,
    )
    observation_ids = [
        item["observation_id"]
        for item in payload["evidence"]["observations"]
    ]
    payload["evidence"]["sequence_alignment_evidence"][
        "new_suffix_observation_ids"
    ] = [observation_ids[1]]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_ingest_rejects_ambiguous_alignment_that_declares_new_suffix():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("歧义序列客户", "13896676692")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-ambiguous-suffix",
        messages=[
            _v3_message(
                "ambiguous-suffix",
                role="customer",
                message_type="text",
                content="不能冒充新增尾部",
                screen_order=1,
            )
        ],
    )
    payload["evidence"]["sequence_alignment_evidence"].update(
        {
            "alignment_status": "ambiguous",
            "candidate_alignment_count": 2,
            "old_tail_fully_consumed": False,
        }
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_ingest_rejects_message_position_without_order_evidence_source():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("缺少顺序证据客户", "13896676683")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-missing-order-source",
        messages=[
            _v3_message(
                "missing-order-source",
                role="customer",
                message_type="text",
                content="顺序来源不能靠猜",
                screen_order=1,
            ),
        ],
    )
    del payload["messages"][0]["message_position"]["order_source"]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0


@pytest.mark.parametrize(
    ("duplicate_field", "duplicate_value"),
    [
        ("source_message_key", "slot-ledger-a"),
        ("screen_order", 1),
    ],
)
def test_ingest_rejects_ambiguous_final_frame_slot_ledger(
    duplicate_field: str,
    duplicate_value: str | int,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("槽位合同客户", "13896676681")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"read-ambiguous-slot-{duplicate_field}",
        messages=[
            _v3_message(
                "slot-ledger-new-message",
                role="customer",
                message_type="text",
                content="新消息",
                screen_order=2,
            ),
        ],
    )
    slots = [
        {
            "observation_id": "slot-observation-a",
            "screen_order": 1,
            "order_source": "visual_top",
            "row_kind": "text_bubble",
            "source_message_key": "slot-ledger-a",
            "origin_read_run_id": "read-slot-ledger-historical",
            "fact_scope": "historical",
            "delivery_state": "backend_confirmed",
            "item_state": "completed",
            "ledger_state": "OLD_COMPLETED",
        },
        {
            "observation_id": "slot-observation-b",
            "screen_order": 2,
            "order_source": "visual_top",
            "row_kind": "text_bubble",
            "source_message_key": "slot-ledger-b",
            "origin_read_run_id": "read-slot-ledger-duplicate",
            "fact_scope": "historical",
            "delivery_state": "backend_confirmed",
            "item_state": "completed",
            "ledger_state": "NEW_MESSAGE",
        },
    ]
    slots[1][duplicate_field] = duplicate_value
    payload["evidence"]["slot_ledger_states"] = slots

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0


def test_lightweight_read_authorization_returns_current_recovery_target():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("轻量授权客户", "13896676687")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert binding_row is not None
        assert conversation is not None
        conversation.status = "waiting_sales_reply"
        db.commit()

    cooling_down = client.get(
        (
            f"/api/workers/{worker['id']}/wechat/conversations/"
            f"{binding['conversation_id']}/read-authorization"
        ),
        headers=_worker_headers(worker),
    )

    assert cooling_down.status_code == 200, cooling_down.text
    cooling_data = cooling_down.json()["data"]
    assert cooling_data["allowed"] is False
    assert cooling_data["recovery_decision"] == "retry_later"
    assert cooling_data["read_reason"] == "waiting_sales_reply"
    assert cooling_data["identity_checkpoint"]["version"] == 3
    assert cooling_data["next_read_due_at"] is not None
    assert "target" not in cooling_data

    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        binding_row.last_read_conversation_status = "waiting_sales_reply"
        binding_row.next_read_due_at = utcnow() - timedelta(seconds=1)
        db.commit()

    response = client.get(
        (
            f"/api/workers/{worker['id']}/wechat/conversations/"
            f"{binding['conversation_id']}/read-authorization"
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["allowed"] is True
    assert data["recovery_decision"] == "allowed"
    assert data["conversation_id"] == binding["conversation_id"]
    assert data["authorization_revision"] == _binding_authorization_revision(
        binding["id"]
    )
    assert data["read_reason"] == "waiting_sales_reply"
    assert data["target"]["conversation_id"] == binding["conversation_id"]
    assert data["target"]["remark_code"] == remark_code
    assert data["target"]["read_reason"] == "waiting_sales_reply"
    assert (
        data["target"]["authorization_revision"]
        == data["authorization_revision"]
    )
    worker_target = WorkerWechatReadTarget.from_api(data["target"])
    assert worker_target.conversation_id == binding["conversation_id"]
    assert worker_target.remark_code == remark_code
    assert worker_target.authorization_revision == data[
        "authorization_revision"
    ]
    assert "identity_transition" not in data
    assert "targets" not in data


def test_image_recovery_authorization_distinguishes_retry_and_termination():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("图片恢复授权客户", "13896676688")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    endpoint = (
        f"/api/workers/{worker['id']}/wechat/conversations/"
        f"{binding['conversation_id']}/read-authorization"
    )

    with SessionLocal() as db:
        row = db.get(WechatSessionBinding, binding["id"])
        row.listen_status = "error"
        row.allow_listening = False
        db.commit()
    retry = client.get(endpoint, headers=_worker_headers(worker))
    assert retry.status_code == 200, retry.text
    assert retry.json()["data"]["allowed"] is False
    assert retry.json()["data"]["recovery_decision"] == "retry_later"

    with SessionLocal() as db:
        row = db.get(WechatSessionBinding, binding["id"])
        row.bind_status = "needs_review"
        row.listen_status = "not_started"
        db.commit()
    review = client.get(endpoint, headers=_worker_headers(worker))
    assert review.status_code == 200, review.text
    assert review.json()["data"]["allowed"] is False
    assert review.json()["data"]["recovery_decision"] == "retry_later"

    with SessionLocal() as db:
        row = db.get(WechatSessionBinding, binding["id"])
        row.bind_status = "unbound"
        db.commit()
    terminated = client.get(endpoint, headers=_worker_headers(worker))
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["data"] == {
        "allowed": False,
        "recovery_decision": "target_terminated",
        "conversation_id": binding["conversation_id"],
        "authorization_revision": "",
        "read_reason": "",
    }


def test_delayed_outbox_ingests_fact_without_rolling_back_current_state():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("延迟上报客户", "13896676681")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-delayed-old-outbox",
        messages=[
            _v3_message(
                "delayed-customer-fact",
                role="customer",
                message_type="text",
                content="这是一条延迟送达的旧消息",
                screen_order=1,
            )
        ],
        read_reason="waiting_sales_reply",
    )
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "sales_replied_waiting_user"
        db.commit()

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ingested_count"] == 1
    assert data["state_transition_applied"] is False
    assert data["state_transition_reason"] == "authorization_read_reason_changed"
    assert data.get("message_batch") is None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "sales_replied_waiting_user"
        assert db.query(MessageEvent).count() == 1
        assert db.query(MessageBatch).count() == 0
        assert db.query(HandoffEvent).count() == 0


def test_open_handoff_keeps_customer_fact_without_reactivating_brain():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("转人工继续追问客户", "13896676688")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    batch_id, handoff_id = _seed_open_handoff(binding, paused=False)
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-open-handoff-customer",
        messages=[
            _v3_message(
                "open-handoff-customer",
                role="customer",
                message_type="text",
                content="转人工后我再补充一个问题",
                screen_order=1,
            )
        ],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"].get("message_batch") is None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert conversation.status == "waiting_sales_reply"
        assert conversation.ai_enabled is True
        assert handoff.closed_at is None
        assert handoff.status == "created"
        assert db.query(MessageEvent).count() == 1
        assert db.query(MessageBatch).count() == 1
        assert db.get(MessageBatch, batch_id).status == "handoff_created"


@pytest.mark.parametrize(
    "reason_code",
    [
        "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        "C2_MESSAGE_HISTORY_GAP",
    ],
)
def test_clean_authoritative_read_auto_recovers_temporary_c2_handoff(
    reason_code: str,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"自动恢复{reason_code}", "13896676701")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    _, handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        reason_code=reason_code,
    )
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        binding_row.last_read_conversation_status = "waiting_sales_reply"
        binding_row.next_read_due_at = utcnow() - timedelta(seconds=1)
        db.commit()

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert targets.status_code == 200, targets.text
    target = next(
        item
        for item in targets.json()["data"]["targets"]
        if item["conversation_id"] == binding["conversation_id"]
    )
    assert target["recoverable_handoff_reason_codes"] == [reason_code]

    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"read-auto-recover-{reason_code}",
        messages=[
            _v3_message(
                f"customer-after-recovery-{reason_code}",
                role="customer",
                message_type="text",
                content="这是恢复后需要回复的新消息",
                screen_order=1,
            )
        ],
    )
    payload["evidence"]["recoverable_handoff_resolution"] = {
        "version": 1,
        "status": "latest_unreplied_turn_complete",
        "reason_codes": [reason_code],
        "identity_confirmed": True,
        "history_confirmed": True,
        "automatic_reread_performed": True,
    }

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"].get("message_batch") is not None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert conversation.status == "ai_active"
        assert conversation.handoff_reason_code is None
        assert handoff.status == "auto_recovered_clean_read"
        assert handoff.closed_at is not None
        assert any(
            value.startswith("c2_recovery_read:")
            for value in handoff.evidence_refs
        )
        recovered_batch = db.query(MessageBatch).filter(
            MessageBatch.conversation_id == binding["conversation_id"],
            MessageBatch.id != handoff.batch_id,
        ).one()
        assert recovered_batch.status == "reply_action_created"
        assert db.query(ReplyAction).filter(
            ReplyAction.batch_id == recovered_batch.id,
            ReplyAction.status == "queued",
        ).count() == 1


@pytest.mark.parametrize(
    "reason_code",
    [
        "MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS",
        "C2_MESSAGE_HISTORY_GAP",
    ],
)
def test_clean_authoritative_reread_replies_to_existing_unreplied_customer_tail_once(
    reason_code: str,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"重复消息恢复{reason_code}", "13896676711")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    source_key = f"existing-unreplied-{reason_code}"
    handoff_batch_id, handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        trigger_source_key=source_key,
        trigger_content="我想了解这辆车的具体情况",
        reason_code=reason_code,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"read-existing-recovery-{reason_code}",
        messages=[
            _v3_message(
                source_key,
                role="customer",
                message_type="text",
                content="我想了解这辆车的具体情况",
                screen_order=1,
            )
        ],
    )
    payload["evidence"]["recoverable_handoff_resolution"] = {
        "version": 1,
        "status": "latest_unreplied_turn_complete",
        "reason_codes": [reason_code],
        "identity_confirmed": True,
        "history_confirmed": True,
        "automatic_reread_performed": True,
    }

    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    assert first_data["ingested_count"] == 0
    assert first_data["duplicated_count"] == 1
    assert first_data.get("message_batch") is not None

    # A transport retry of the same authoritative read must reuse the
    # deterministic recovery work and must never enqueue a second reply.
    second = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )
    assert second.status_code == 200, second.text

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        recovery_batches = db.query(MessageBatch).filter(
            MessageBatch.conversation_id == binding["conversation_id"],
            MessageBatch.id != handoff_batch_id,
            MessageBatch.trigger_type == "c2_handoff_recovery",
        ).all()
        assert conversation.status == "ai_active"
        assert handoff.status == "auto_recovered_clean_read"
        assert handoff.closed_at is not None
        assert len(recovery_batches) == 1
        assert recovery_batches[0].message_event_ids == [
            handoff.trigger_message_event_ids[0]
        ]
        assert db.query(ReplyAction).filter(
            ReplyAction.batch_id == recovery_batches[0].id,
            ReplyAction.status == "queued",
        ).count() == 1
        assert db.query(ReplyAction).filter(
            ReplyAction.conversation_id == binding["conversation_id"]
        ).count() == 1


def test_postgres_concurrent_recovery_requests_create_one_batch_claim_and_reply():
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL recovery concurrency test")

    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("并发恢复客户", "13896676713")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    source_key = "postgres-concurrent-recovery-customer"
    _, handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        trigger_source_key=source_key,
        trigger_content="我想了解这辆车的具体情况",
        reason_code="C2_MESSAGE_HISTORY_GAP",
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-postgres-concurrent-recovery",
        messages=[
            _v3_message(
                source_key,
                role="customer",
                message_type="text",
                content="我想了解这辆车的具体情况",
                screen_order=1,
            )
        ],
    )
    payload["evidence"]["recoverable_handoff_resolution"] = {
        "version": 1,
        "status": "latest_unreplied_turn_complete",
        "reason_codes": ["C2_MESSAGE_HISTORY_GAP"],
        "identity_confirmed": True,
        "history_confirmed": True,
        "automatic_reread_performed": True,
    }

    start_barrier = threading.Barrier(2)
    outcome_lock = threading.Lock()
    outcomes: list[dict] = []
    claim_results: list[dict] = []

    def submit_recovery_request(request_no: int) -> None:
        try:
            request_payload = WechatMessageIngestRequest.model_validate(payload)
            start_barrier.wait(timeout=5)
            with SessionLocal() as request_db:
                worker_row = request_db.get(Worker, worker["id"])
                result = wechat_service.ingest_messages(
                    request_db,
                    worker_row,
                    request_payload,
                )
                request_db.commit()
                message_batch = result.get("message_batch") or {}
                batch_id = str(message_batch.get("batch_id") or "")
                claim = None
                if batch_id and str(message_batch.get("batch_status") or "") in {
                    "collecting",
                    "generating",
                }:
                    claim = c3_service.claim_message_batch_generation(
                        request_db,
                        batch_id=batch_id,
                    )
                    request_db.commit()
                    if claim.get("run"):
                        with SessionLocal() as generation_db:
                            c3_service.generate_for_batch(
                                generation_db,
                                batch_id=batch_id,
                                expected_generation_attempt=int(
                                    claim["attempt"]
                                ),
                            )
                            generation_db.commit()
                with outcome_lock:
                    outcomes.append(
                        {
                            "request_no": request_no,
                            "result": result,
                        }
                    )
                    if claim is not None:
                        claim_results.append(claim)
        except Exception as exc:  # pragma: no cover - asserted below
            with outcome_lock:
                outcomes.append(
                    {
                        "request_no": request_no,
                        "exception": exc,
                    }
                )

    threads = [
        threading.Thread(
            target=submit_recovery_request,
            args=(request_no,),
            daemon=True,
        )
        for request_no in (1, 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2
    assert all("exception" not in outcome for outcome in outcomes), outcomes
    assert sum(bool(claim.get("run")) for claim in claim_results) == 1

    with SessionLocal() as db:
        handoff = db.get(HandoffEvent, handoff_id)
        recovery_batches = db.query(MessageBatch).filter(
            MessageBatch.conversation_id == binding["conversation_id"],
            MessageBatch.trigger_type == "c2_handoff_recovery",
        ).all()
        assert handoff.status == "auto_recovered_clean_read"
        assert handoff.closed_at is not None
        assert len(recovery_batches) == 1
        assert recovery_batches[0].generation_attempt_count == 1
        assert db.query(ReplyAction).filter(
            ReplyAction.batch_id == recovery_batches[0].id,
            ReplyAction.status == "queued",
        ).count() == 1
        assert db.query(ReplyAction).filter(
            ReplyAction.conversation_id == binding["conversation_id"]
        ).count() == 1


def test_hard_handoff_keeps_gate_when_recoverable_handoff_is_closed():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("硬门禁并存恢复客户", "13896676712")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    source_key = "recoverable-with-hard-gate"
    _, recoverable_handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        trigger_source_key=source_key,
        reason_code="C2_MESSAGE_HISTORY_GAP",
    )
    with SessionLocal() as db:
        hard_batch = MessageBatch(
            conversation_id=binding["conversation_id"],
            status="handoff_created",
            active=False,
            trigger_type="c2_safety_handoff",
            trigger_key="coexisting-hard-gate",
            message_event_ids=[],
            message_count=0,
            decision="handoff",
            error_code="HARD_BUSINESS_RISK",
            suggested_action="handoff",
        )
        db.add(hard_batch)
        db.flush()
        conversation = db.get(Conversation, binding["conversation_id"])
        hard_handoff, created = c3_service._create_or_reuse_open_handoff(
            db,
            conversation=conversation,
            batch_id=hard_batch.id,
            handoff_reason_code="HARD_BUSINESS_RISK",
            reason_detail="HARD_BUSINESS_RISK",
            trigger_message_event_ids=[],
            risk_flags=["hard_business_risk"],
            evidence_refs=["test:hard_business_risk"],
            ai_payload={},
        )
        db.commit()
        hard_handoff_id = hard_handoff.id
        assert created is False
        assert hard_handoff_id == recoverable_handoff_id
        assert hard_handoff.handoff_reason_code == "HARD_BUSINESS_RISK"

    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-recoverable-with-hard-gate",
        messages=[
            _v3_message(
                source_key,
                role="customer",
                message_type="text",
                content="触发人工接管的客户消息",
                screen_order=1,
            )
        ],
    )
    payload["evidence"]["recoverable_handoff_resolution"] = {
        "version": 1,
        "status": "latest_unreplied_turn_complete",
        "reason_codes": ["C2_MESSAGE_HISTORY_GAP"],
        "identity_confirmed": True,
        "history_confirmed": True,
        "automatic_reread_performed": True,
    }

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"].get("message_batch") is None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        hard = db.get(HandoffEvent, hard_handoff_id)
        assert hard.status == "created"
        assert hard.closed_at is None
        assert hard.handoff_reason_code == "HARD_BUSINESS_RISK"
        assert db.query(HandoffEvent).count() == 1
        assert conversation.status == "waiting_sales_reply"
        assert db.query(MessageBatch).filter(
            MessageBatch.trigger_type == "c2_handoff_recovery"
        ).count() == 0
        assert db.query(ReplyAction).count() == 0


def test_clean_read_does_not_auto_close_nonrecoverable_handoff():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("硬风险不得自动恢复", "13896676702")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    _, handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        reason_code="HARD_BUSINESS_RISK",
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-must-not-auto-recover-hard-risk",
        messages=[
            _v3_message(
                "hard-risk-follow-up",
                role="customer",
                message_type="text",
                content="新的客户消息",
                screen_order=1,
            )
        ],
    )
    payload["evidence"]["recoverable_handoff_resolution"] = {
        "version": 1,
        "status": "latest_unreplied_turn_complete",
        "reason_codes": ["MESSAGE_CROSS_ROUND_IDENTITY_AMBIGUOUS"],
        "identity_confirmed": True,
        "history_confirmed": True,
    }

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"].get("message_batch") is None
    with SessionLocal() as db:
        handoff = db.get(HandoffEvent, handoff_id)
        assert handoff.closed_at is None
        assert handoff.status == "created"


def test_open_handoff_repairs_stale_conversation_projection_before_read_target():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("转人工状态修复客户", "13896676693")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    _batch_id, handoff_id = _seed_open_handoff(binding, paused=False)
    handoff_at = utcnow()
    notified_at = handoff_at - timedelta(seconds=1)
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert handoff is not None
        conversation.status = "ai_active"
        conversation.handoff_at = handoff_at
        conversation.recall_origin_status = "waiting_user_reply"
        conversation.recall_cycle_id = "stale-recall"
        conversation.next_recall_at = utcnow() - timedelta(minutes=1)
        handoff.notify_status = "succeeded"
        handoff.notify_attempted_at = notified_at
        handoff.notify_completed_at = notified_at
        db.commit()

    response = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["targets"] == []
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        binding_row = db.get(WechatSessionBinding, binding["id"])
        handoffs = db.query(HandoffEvent).filter(
            HandoffEvent.conversation_id == binding["conversation_id"]
        ).all()
        assert conversation.status == "waiting_sales_reply"
        assert binding_row is not None
        assert binding_row.next_read_due_at is not None
        due_at = binding_row.next_read_due_at
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        assert abs(
            (
                due_at.astimezone(timezone.utc)
                - handoff_at.astimezone(timezone.utc)
            ).total_seconds()
            - 120
        ) < 1
        assert [event.id for event in handoffs] == [handoff_id]
        assert handoffs[0].notify_status == "succeeded"
        assert handoffs[0].notify_attempted_at is not None
        assert handoffs[0].notify_completed_at is not None
        assert handoffs[0].notify_attempted_at.replace(
            tzinfo=timezone.utc
        ) == notified_at
        assert handoffs[0].notify_completed_at.replace(
            tzinfo=timezone.utc
        ) == notified_at
        assert conversation.recall_origin_status == "waiting_user_reply"
        assert conversation.recall_cycle_id == "stale-recall"
        assert conversation.next_recall_at is not None
        binding_row.next_read_due_at = utcnow() - timedelta(seconds=1)
        db.commit()

    due_response = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )

    assert due_response.status_code == 200, due_response.text
    targets = due_response.json()["data"]["targets"]
    assert len(targets) == 1
    assert targets[0]["conversation_id"] == binding["conversation_id"]
    assert targets[0]["read_reason"] == "waiting_sales_reply"
    with SessionLocal() as db:
        handoffs = db.query(HandoffEvent).filter(
            HandoffEvent.conversation_id == binding["conversation_id"]
        ).all()
        assert [event.id for event in handoffs] == [handoff_id]
        assert handoffs[0].notify_status == "succeeded"
        assert handoffs[0].notify_attempted_at is not None
        assert handoffs[0].notify_completed_at is not None
        assert handoffs[0].notify_attempted_at.replace(
            tzinfo=timezone.utc
        ) == notified_at
        assert handoffs[0].notify_completed_at.replace(
            tzinfo=timezone.utc
        ) == notified_at


def test_pause_keeps_customer_fact_without_409_or_brain_batch():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("暂停后继续追问客户", "13896676689")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    batch_id, handoff_id = _seed_open_handoff(binding, paused=True)
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-paused-customer",
        messages=[
            _v3_message(
                "paused-customer",
                role="customer",
                message_type="text",
                content="暂停后这条消息仍应保存",
                screen_order=1,
            )
        ],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"].get("message_batch") is None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert conversation.status == "waiting_sales_reply"
        assert conversation.ai_enabled is False
        assert handoff.closed_at is None
        assert db.query(MessageEvent).count() == 1
        assert db.query(MessageBatch).count() == 1
        assert db.get(MessageBatch, batch_id).status == "paused"


def test_human_sales_reply_closes_pause_handoff_and_restores_ai():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("销售接管恢复客户", "13896676690")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    trigger_source_key = "paused-human-sales-trigger"
    _, handoff_id = _seed_open_handoff(
        binding,
        paused=True,
        trigger_source_key=trigger_source_key,
        trigger_content="触发暂停的客户消息",
    )
    sales_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-paused-human-sales",
        messages=[
            _v3_message(
                trigger_source_key,
                role="customer",
                message_type="text",
                content="触发暂停的客户消息",
                screen_order=1,
                order_source="visual_top",
            ),
            _v3_message(
                "paused-human-sales",
                role="self",
                message_type="text",
                content="这条由销售本人处理",
                screen_order=2,
                order_source="visual_top",
            )
        ],
    )
    sales_payload = _simulate_worker_incremental_filter(
        sales_payload,
        keep_source_keys={"paused-human-sales"},
    )

    sales_response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=sales_payload,
        headers=_worker_headers(worker),
    )

    assert sales_response.status_code == 200, sales_response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert conversation.status == "sales_replied_waiting_user"
        assert conversation.ai_enabled is True
        assert conversation.handoff_reason_code is None
        assert handoff.status == "sales_replied"
        assert handoff.closed_at is not None
        assert any(
            value.startswith("sales_message_event:")
            for value in handoff.evidence_refs
        )

    customer_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-after-human-sales",
        read_reason="waiting_user_reply",
        messages=[
            _v3_message(
                "customer-after-human-sales",
                role="customer",
                message_type="text",
                content="销售回复后新的客户消息可以恢复自动流程",
                screen_order=1,
            )
        ],
    )
    customer_response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=customer_payload,
        headers=_worker_headers(worker),
    )

    assert customer_response.status_code == 200, customer_response.text
    assert customer_response.json()["data"].get("message_batch") is not None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "ai_active"
        assert conversation.ai_enabled is True
        assert db.query(MessageBatch).count() == 2


@pytest.mark.parametrize(
    "sales_occurred_at,source_suffix",
    [
        (None, "unknown-time"),
        ((utcnow() - timedelta(hours=3)).isoformat(), "historical"),
    ],
)
def test_sales_message_without_proven_post_handoff_time_keeps_handoff_open(
    sales_occurred_at,
    source_suffix,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"历史销售消息门禁客户-{source_suffix}", "13896676693")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    _, handoff_id = _seed_open_handoff(binding, paused=False)
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"read-handoff-{source_suffix}",
        messages=[
            _v3_message(
                f"handoff-sales-{source_suffix}",
                role="self",
                message_type="text",
                content="这是本轮才扫描到的历史销售消息",
                screen_order=1,
                occurred_at=sales_occurred_at,
            )
        ],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"].get("message_batch") is None
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert conversation.status == "waiting_sales_reply"
        assert handoff.status == "created"
        assert handoff.closed_at is None


def test_sales_message_without_occurred_at_closes_handoff_when_below_visible_trigger():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("同屏销售回复解除接管客户", "13896676694")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    trigger_source_key = "handoff-visible-trigger"
    _, handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        trigger_source_key=trigger_source_key,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-handoff-visible-sales-below",
        messages=[
            _v3_message(
                trigger_source_key,
                role="customer",
                message_type="text",
                content="触发人工接管的客户消息",
                screen_order=1,
                order_source="visual_top",
            ),
            _v3_message(
                "handoff-visible-sales-below",
                role="self",
                message_type="text",
                content="销售已经在同一画面下方回复",
                screen_order=2,
                order_source="visual_top",
            ),
        ],
    )
    payload = _simulate_worker_incremental_filter(
        payload,
        keep_source_keys={"handoff-visible-sales-below"},
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert conversation.status == "sales_replied_waiting_user"
        assert handoff.status == "sales_replied"
        assert handoff.closed_at is not None
        assert "sales_reply_order_proof:same_final_frame_order" in handoff.evidence_refs


def test_visual_order_wins_when_sales_time_conflicts_above_visible_trigger():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("同屏历史销售消息保持接管客户", "13896676695")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    trigger_source_key = "handoff-visible-trigger-after-sales"
    _, handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        trigger_source_key=trigger_source_key,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-handoff-visible-sales-above",
        messages=[
            _v3_message(
                "handoff-visible-sales-above",
                role="self",
                message_type="text",
                content="这是触发消息上方的历史销售消息",
                screen_order=1,
                order_source="visual_top",
                occurred_at=utcnow().isoformat(),
            ),
            _v3_message(
                trigger_source_key,
                role="customer",
                message_type="text",
                content="触发人工接管的客户消息",
                screen_order=2,
                order_source="visual_top",
            ),
        ],
    )
    payload = _simulate_worker_incremental_filter(
        payload,
        keep_source_keys={"handoff-visible-sales-above"},
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert conversation.status == "waiting_sales_reply"
        assert handoff.status == "created"
        assert handoff.closed_at is None


def test_sales_message_without_occurred_at_keeps_handoff_when_order_is_only_ocr_fallback():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("弱顺序不能解除接管客户", "13896676696")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    trigger_source_key = "handoff-fallback-trigger"
    _, handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        trigger_source_key=trigger_source_key,
        trigger_content="OCR 顺序里的客户消息",
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-handoff-fallback-order",
        messages=[
            _v3_message(
                trigger_source_key,
                role="customer",
                message_type="text",
                content="OCR 顺序里的客户消息",
                screen_order=1,
            ),
            _v3_message(
                "handoff-fallback-sales",
                role="self",
                message_type="text",
                content="OCR 顺序里的销售消息",
                screen_order=2,
            ),
        ],
    )
    payload = _simulate_worker_incremental_filter(
        payload,
        keep_source_keys={"handoff-fallback-sales"},
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        handoff = db.get(HandoffEvent, handoff_id)
        assert conversation.status == "waiting_sales_reply"
        assert handoff.status == "created"
        assert handoff.closed_at is None


def test_open_handoff_same_frame_respects_customer_sales_customer_order():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("同屏接管顺序客户", "13896676692")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    _, handoff_id = _seed_open_handoff(
        binding,
        paused=False,
        trigger_source_key="handoff-customer-before-sales",
        trigger_content="这条发生在销售接管前",
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-handoff-customer-sales-customer",
        messages=[
            _v3_message(
                "handoff-customer-before-sales",
                role="customer",
                message_type="text",
                    content="这条发生在销售接管前",
                    screen_order=1,
                    order_source="visual_top",
            ),
            _v3_message(
                "handoff-human-sales-middle",
                role="self",
                message_type="text",
                    content="销售已接手",
                    screen_order=2,
                    order_source="visual_top",
            ),
            _v3_message(
                "handoff-customer-after-sales",
                role="customer",
                message_type="text",
                    content="这条发生在销售回复后",
                    screen_order=3,
                    order_source="visual_top",
            ),
        ],
    )
    payload = _simulate_worker_incremental_filter(
        payload,
        keep_source_keys={
            "handoff-human-sales-middle",
            "handoff-customer-after-sales",
        },
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"].get("message_batch") is not None
    with SessionLocal() as db:
        handoff = db.get(HandoffEvent, handoff_id)
        conversation = db.get(Conversation, binding["conversation_id"])
        batch = (
            db.query(MessageBatch)
            .filter(MessageBatch.id != handoff.batch_id)
            .one()
        )
        assert handoff.status == "sales_replied"
        assert handoff.closed_at is not None
        assert conversation.status == "ai_active"
        assert batch.message_count == 1
        assert len(batch.message_event_ids) == 1
        customer_after = db.get(MessageEvent, batch.message_event_ids[0])
        assert customer_after.content == "这条发生在销售回复后"


def test_delayed_history_does_not_move_recent_message_times_backwards():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("历史消息时间不倒退客户", "13896676689")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    current_time = utcnow()
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.last_inbound_at = current_time
        conversation.last_outbound_at = current_time
        db.commit()

    old_time = (current_time - timedelta(hours=3)).isoformat()
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-delayed-history-monotonic",
        messages=[
            _v3_message(
                "delayed-history-customer",
                role="customer",
                message_type="text",
                content="三小时前的客户历史消息",
                screen_order=1,
                occurred_at=old_time,
            ),
            _v3_message(
                "delayed-history-sales",
                role="self",
                message_type="text",
                content="三小时前的销售历史消息",
                screen_order=2,
                occurred_at=old_time,
            ),
        ],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        inbound_at = conversation.last_inbound_at
        outbound_at = conversation.last_outbound_at
        if inbound_at.tzinfo is None:
            inbound_at = inbound_at.replace(tzinfo=timezone.utc)
        if outbound_at.tzinfo is None:
            outbound_at = outbound_at.replace(tzinfo=timezone.utc)
        assert inbound_at >= current_time
        assert outbound_at >= current_time


def test_sqlite_rolls_back_insert_when_batch_creation_rejects(monkeypatch):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("事务原子性客户", "13896676691")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]

    def reject_batch(*_args, **_kwargs):
        raise AppError(
            "CONVERSATION_NOT_ELIGIBLE",
            "模拟消息插入后的批次拒绝",
            409,
        )

    monkeypatch.setattr(
        c3_service,
        "collect_customer_message_batch",
        reject_batch,
    )
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-sqlite-rollback",
        messages=[
            _v3_message(
                "sqlite-rollback-message",
                role="customer",
                message_type="text",
                content="这条消息必须随 409 一起回滚",
                screen_order=1,
            )
        ],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CONVERSATION_NOT_ELIGIBLE"
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_ai_active_requires_exact_batch_continuation_to_advance_flow():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("批次续行客户", "13896676683")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_sales_reply"
        db.commit()

    first_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-continuation-first",
        messages=[
            _v3_message(
                "continuation-first",
                role="customer",
                message_type="text",
                content="第一条客户消息",
                screen_order=1,
            )
        ],
    )
    second_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-continuation-second",
        messages=[
            _v3_message(
                "continuation-second",
                role="customer",
                message_type="text",
                content="同一轮新增客户消息",
                screen_order=1,
            )
        ],
    )
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=first_payload,
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200, first.text
    first_message_batch = first.json()["data"]["message_batch"]
    first_batch_id = first_message_batch["batch_id"]
    ingest_continuation = first_message_batch["continuation"]
    assert ingest_continuation["batch_id"] == first_batch_id
    assert ingest_continuation["token"]
    assert (
        ingest_continuation["authorization_revision"]
        == first_payload["authorization_revision"]
    )
    assert (
        ingest_continuation["read_reason"]
        == first_payload["evidence"]["authorization_read_reason"]
    )

    global_authorization = client.get(
        (
            f"/api/workers/{worker['id']}/wechat/conversations/"
            f"{binding['conversation_id']}/read-authorization"
        ),
        headers=_worker_headers(worker),
    ).json()["data"]
    assert global_authorization["allowed"] is False

    batch_status = client.get(
        f"/api/workers/{worker['id']}/wechat/message-batches/{first_batch_id}",
        headers=_worker_headers(worker),
    )
    assert batch_status.status_code == 200
    continuation = batch_status.json()["data"]["authorization"]
    assert continuation["allowed"] is True
    assert continuation["authorization_scope"] == "batch_continuation"
    assert continuation["batch_id"] == first_batch_id
    assert continuation["continuation_token"]
    assert continuation["continuation_token"] == ingest_continuation["token"]

    lightweight = client.get(
        (
            f"/api/workers/{worker['id']}/wechat/conversations/"
            f"{binding['conversation_id']}/read-authorization"
        ),
        params={
            "continuation_batch_id": first_batch_id,
        },
        headers={
            **_worker_headers(worker),
            "X-C2-Continuation-Token": continuation["continuation_token"],
        },
    )
    assert lightweight.status_code == 200
    assert lightweight.json()["data"]["allowed"] is True

    second_payload["authorization_revision"] = continuation["authorization_revision"]
    second_payload["evidence"]["authorization_read_reason"] = continuation["read_reason"]
    second_payload["evidence"]["read_reason"] = continuation["read_reason"]
    second_payload["evidence"]["continuation_batch_id"] = first_batch_id
    second_payload["evidence"]["continuation_token"] = continuation["continuation_token"]
    second = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=second_payload,
        headers=_worker_headers(worker),
    )

    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["state_transition_applied"] is True
    assert data["state_transition_reason"] == "batch_continuation_matches"
    assert data["message_batch"]["batch_id"] != first_batch_id
    with SessionLocal() as db:
        first_batch = db.get(MessageBatch, first_batch_id)
        assert first_batch.status == "superseded"
        assert first_batch.active is False
        assert db.query(MessageBatch).count() == 2


def test_old_outbox_without_batch_continuation_cannot_replace_active_brain_batch():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("旧请求保护客户", "13896676684")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_sales_reply"
        db.commit()

    current_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-current-brain",
        messages=[
            _v3_message(
                "current-brain-message",
                role="customer",
                message_type="text",
                content="当前真正的新消息",
                screen_order=1,
            )
        ],
    )
    delayed_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-old-outbox-ai-active",
        messages=[
            _v3_message(
                "old-outbox-message",
                role="customer",
                message_type="text",
                content="之前未送达的旧事实",
                screen_order=1,
            )
        ],
    )
    current = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=current_payload,
        headers=_worker_headers(worker),
    )
    assert current.status_code == 200, current.text
    current_batch_id = current.json()["data"]["message_batch"]["batch_id"]

    delayed = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=delayed_payload,
        headers=_worker_headers(worker),
    )

    assert delayed.status_code == 200, delayed.text
    data = delayed.json()["data"]
    assert data["ingested_count"] == 1
    assert data["state_transition_applied"] is False
    assert data["state_transition_reason"] == "authorization_read_reason_changed"
    assert data.get("message_batch") is None
    with SessionLocal() as db:
        assert db.query(MessageBatch).count() == 1
        batch = db.get(MessageBatch, current_batch_id)
        assert batch.status != "superseded"
        assert batch.superseded_by_batch_id is None
        assert db.query(MessageEvent).count() == 2


def test_message_ingest_rejects_oversized_raw_evidence_before_persistence():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("大证据客户", "13896676688")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-oversized-evidence",
        messages=[
            _v3_message(
                "oversized-evidence-message",
                role="customer",
                message_type="text",
                content="正常正文",
                screen_order=1,
                raw_extra={"diagnostic_blob": "x" * 300_000},
            )
        ],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["data"]["retryable"] is False
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0


def test_worker_compacts_300kb_raw_evidence_to_shared_backend_limit():
    limits = c2_contract_v3()["message_limits"]
    assert C2_MESSAGE_CONTENT_MAX_CHARS == limits["content_max_chars"]
    assert C2_MESSAGE_RAW_PAYLOAD_MAX_BYTES == limits[
        "raw_payload_max_bytes"
    ]
    assert C2_MESSAGE_BATCH_MAX_ITEMS == limits["batch_max_items"]
    assert C2_MESSAGE_INGEST_MAX_BYTES == limits["ingest_max_bytes"]

    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("大证据压缩客户", "13896676687")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-compact-oversized-evidence",
        messages=[
            _v3_message(
                "compact-oversized-evidence-message",
                role="customer",
                message_type="text",
                content="正常正文",
                screen_order=1,
                raw_extra={"diagnostic_blob": "x" * 300_000},
            )
        ],
    )

    parts = split_ingest_payload(payload)

    assert len(parts) == 1
    prepared_raw = parts[0]["messages"][0]["raw_payload"]
    assert "diagnostic_blob" not in prepared_raw
    assert (
        len(
            json.dumps(
                prepared_raw,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= C2_MESSAGE_RAW_PAYLOAD_MAX_BYTES
    )
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=parts[0],
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        event = db.query(MessageEvent).one()
        assert event.content == "正常正文"


def test_message_ingest_rejects_request_body_larger_than_global_limit():
    response = client.post(
        "/api/workers/not-used/wechat/messages/ingest",
        content=b'{"padding":"' + (b"x" * 2_100_000) + b'"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "MESSAGE_INGEST_REQUEST_TOO_LARGE"
    assert response.json()["data"]["retryable"] is False


def test_worker_split_batch_is_atomic_for_brain_and_completes_once():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("拆批客户", "13896676689")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    templates = [
        _v3_message(
            f"partition-message-{index}",
            role="customer",
            message_type="text",
            content=f"第 {index} 条消息",
            screen_order=index,
        )
        for index in range(1, 31)
    ]
    observations = [
        _committed_test_observation(
            template["raw_payload"]["observation"],
            worker_sequence=index,
            commit_basis=MessageCommitBasis.NEW_SUFFIX,
            proof={
                "alignment_status": "not_required",
                "old_tail_fully_consumed": True,
                "new_suffix_observation_id": template["raw_payload"][
                    "observation"
                ]["observation_id"],
            },
        )
        for index, template in enumerate(templates, start=1)
    ]
    payload = _production_worker_payload_for_test(
        binding=binding,
        remark_code=remark_code,
        read_run_id="read-partition-atomic",
        observations=observations,
        read_reason="waiting_user_reply",
    )
    for message in payload["messages"]:
        message["raw_payload"]["voice_transcription_meta"] = {
            "transport_padding": "x" * 80_000,
        }
    parts = split_ingest_payload(payload)

    assert len(parts) >= 2
    required_identity_fields = {
        "business_projection",
        "strong_boundary_tokens",
        "strong_boundary_anchor",
        "message_identity_commit_record",
        "message_identity_runtime_evidence",
    }
    assert all(
        required_identity_fields.issubset(
            message["raw_payload"]
        )
        for part in parts
        for message in part["messages"]
    )
    assert {
        "message_commit_evidence",
        "image_sha256",
    }.issubset(
        set(c2_contract_v3()["message_limits"]["raw_payload_transport_fields"])
    )
    assert len(parts[-1]["evidence"]["observations"]) == len(
        payload["evidence"]["observations"]
    )
    assert all(
        len(part["evidence"]["observations"])
        < len(payload["evidence"]["observations"])
        for part in parts[:-1]
    )
    assert len(parts[-1]["evidence"]["slot_ledger_states"]) == 30
    assert {
        part["read_run_id"] for part in parts
    } == {"read-partition-atomic"}
    for index, part in enumerate(parts, start=1):
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=part,
            headers=_worker_headers(worker),
        )
        assert response.status_code == 200, response.text
        partition = response.json()["data"]["ingest_partition"]
        assert partition["index"] == index
        assert partition["complete"] is (index == len(parts))
        with SessionLocal() as db:
            if index < len(parts):
                assert db.query(MessageBatch).count() == 0

    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 30
        batches = db.query(MessageBatch).all()
        assert len(batches) == 1
        assert len(batches[0].message_event_ids) == 30
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        context = c3_service._build_ai_context(
            db,
            persisted_binding,
            conversation,
            batches[0],
        )
        checkpoint = context["pre_send_fact_checkpoint"]
        assert checkpoint["tail_complete"] is True
        assert len(checkpoint["committed_tail"]) == 30
        comparison = worker_compare_checkpoint(
            checkpoint,
            payload["evidence"]["observations"],
            before_frame_id="checkpoint:partitioned-complete-frame",
            after_frame_id="frame:partitioned-pre-send",
            current_tail_complete=True,
        )
        assert comparison["comparison_result"] == "checkpoint_equal"
        assert comparison["old_tail_fully_consumed"] is True


def test_worker_refuses_split_when_complete_frame_evidence_cannot_fit():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("完整画面超限客户", "13896676699")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-unsplittable-complete-frame",
        messages=[
            _v3_message(
                f"unsplittable-frame-{index}",
                role="customer",
                message_type="text",
                content=f"第 {index} 条：" + ("完整画面正文" * 2_000),
                screen_order=index,
            )
            for index in range(1, 31)
        ],
    )

    with pytest.raises(
        ValueError,
        match="C2_INGEST_SINGLE_ITEM_TOO_LARGE",
    ):
        split_ingest_payload(payload)

    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_unbound_active_read_requires_explicit_fact_settlement():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("解绑客户", "13896676690")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-unbound-terminal",
        messages=[
            _v3_message(
                "unbound-message",
                role="customer",
                message_type="text",
                content="解绑后的旧事实",
                screen_order=1,
            )
        ],
    )
    with SessionLocal() as db:
        row = db.get(WechatSessionBinding, binding["id"])
        row.allow_listening = False
        db.commit()

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_CONVERSATION_NOT_BOUND"
    assert response.json()["data"]["recovery_action"] == "target_terminated"
    assert "terminal_confirmed" not in response.json()["data"]


def test_identity_conflict_does_not_create_handoff_or_fake_terminal():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("身份冲突客户", "13896676691")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-identity-terminal",
        messages=[
            _v3_message(
                "identity-conflict-message",
                role="customer",
                message_type="text",
                content="身份冲突",
                screen_order=1,
            )
        ],
    )
    payload["remark_code"] = "CJWRONG01"

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_TARGET_IDENTITY_MISMATCH"
    assert response.json()["data"]["recovery_action"] == "conversation_terminated"
    assert "terminal_confirmed" not in response.json()["data"]
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        assert conversation.status == "waiting_sales_reply"
        assert db.query(HandoffEvent).count() == 0


def test_fact_settlement_persists_full_failed_fact_without_state_side_effects():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("崩溃恢复客户", "13896676693")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    source_key = "recovery-text-menu-image"
    transaction_id = "image-ledger-before-outbox-crash"
    authorization = _authorize_fact_settlement(
        worker,
        binding,
        transaction_id=transaction_id,
        source_keys=[source_key],
    )
    assert authorization["settlement_mode"] == "fact_only"
    payload = _fact_settlement_payload(
        binding,
        remark_code,
        transaction_id=transaction_id,
        source_keys=[source_key],
        settlement_mode="fact_only",
        messages=[
            _v3_failed_image_message(
                source_key,
                role="customer",
                screen_order=1,
                reason="C2_IMAGE_SOURCE_INVALID",
            )
        ],
    )
    payload["messages"][0]["raw_payload"]["reason_detail"] = (
        "text_context_menu_rejected"
    )
    payload["messages"][0]["raw_payload"]["observation"][
        "reason_detail"
    ] = "text_context_menu_rejected"

    rejected = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers={
            **_worker_headers(worker),
            "X-C2-Settlement-Token": "wrong-token",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "C2_SETTLEMENT_TOKEN_INVALID"
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers={
            **_worker_headers(worker),
            "X-C2-Settlement-Token": authorization["settlement_token"],
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["state_transition_applied"] is False
    assert data["state_transition_reason"] == "fact_settlement"
    assert data["results"][0]["source_message_key"] == source_key
    assert data["results"][0]["ingest_result"] == "ingested"
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        message = db.query(MessageEvent).one()
        settlement = db.query(WechatRecoverySettlement).one()
        assert conversation.status == "waiting_sales_reply"
        assert message.source_message_key == source_key
        assert message.item_state == "failed"
        assert message.error_code == "C2_IMAGE_SOURCE_INVALID"
        assert message.raw_payload["reason_detail"] == (
            "text_context_menu_rejected"
        )
        assert settlement.status == "settled"
        assert db.query(MessageBatch).count() == 0
        assert db.query(HandoffEvent).count() == 0
        assert db.query(ReplyAction).count() == 0

    repeated = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers={
            **_worker_headers(worker),
            "X-C2-Settlement-Token": authorization["settlement_token"],
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["state_transition_reason"] == (
        "fact_settlement_idempotent"
    )
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 1


def test_fact_settlement_preserves_mixed_media_sequence_order():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("混合媒体恢复客户", "13896676695")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    source_keys = ["recovery-voice-first", "recovery-image-second"]
    transaction_id = "mixed-media-ledger-before-outbox-crash"
    authorization = _authorize_fact_settlement(
        worker,
        binding,
        transaction_id=transaction_id,
        source_keys=source_keys,
        action_kind="voice",
    )
    payload = _fact_settlement_payload(
        binding,
        remark_code,
        transaction_id=transaction_id,
        source_keys=source_keys,
        settlement_mode="fact_only",
        action_kind="voice",
        messages=[
            _v3_failed_voice_message(
                source_keys[0],
                role="customer",
                screen_order=1,
                reason="C2_VOICE_TRANSCRIBE_FAILED",
            ),
            _v3_failed_image_message(
                source_keys[1],
                role="customer",
                screen_order=2,
                reason="C2_IMAGE_SOURCE_INVALID",
            ),
        ],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers={
            **_worker_headers(worker),
            "X-C2-Settlement-Token": authorization["settlement_token"],
        },
    )

    assert response.status_code == 200, response.text
    assert [
        item["source_message_key"]
        for item in response.json()["data"]["results"]
    ] == source_keys
    with SessionLocal() as db:
        messages = (
            db.query(MessageEvent)
            .order_by(MessageEvent.observation_order.asc())
            .all()
        )
        assert [message.message_type for message in messages] == [
            "voice",
            "image",
        ]
        assert [message.source_message_key for message in messages] == (
            source_keys
        )
        assert db.query(MessageBatch).count() == 0
        assert db.query(ReplyAction).count() == 0


def test_fact_settlement_technical_terminal_confirms_keys_without_fake_message():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("失去身份客户", "13896676694")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    other_worker = _create_worker()
    with SessionLocal() as db:
        row = db.get(WechatSessionBinding, binding["id"])
        row.worker_id = other_worker["id"]
        db.commit()
    source_key = "recovery-identity-untrusted"
    transaction_id = "image-identity-untrusted"
    authorization = _authorize_fact_settlement(
        worker,
        binding,
        transaction_id=transaction_id,
        source_keys=[source_key],
    )
    assert authorization["settlement_mode"] == "technical_terminal"
    payload = _fact_settlement_payload(
        binding,
        remark_code,
        transaction_id=transaction_id,
        source_keys=[source_key],
        settlement_mode="technical_terminal",
        messages=[],
    )

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers={
            **_worker_headers(worker),
            "X-C2-Settlement-Token": authorization["settlement_token"],
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()["data"]["results"][0]
    assert result == {
        "source_message_key": source_key,
        "ingest_result": "technical_terminal",
        "error_code": "C2_RECOVERY_IDENTITY_UNTRUSTED",
    }
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(HandoffEvent).count() == 0
        assert db.query(ReplyAction).count() == 0


def test_safety_gate_after_human_sales_reply_is_not_cleared():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("销售后门禁客户", "13896676682")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-gate-after-sales",
        messages=[
            _v3_message(
                "customer-before-sales",
                role="customer",
                message_type="text",
                content="前面的客户消息",
                screen_order=1,
                order_source="visual_top",
            ),
            _v3_message(
                "human-sales-middle",
                role="self",
                message_type="text",
                content="销售已经回复",
                screen_order=2,
                order_source="visual_top",
            ),
        ],
    )
    payload["evidence"]["flow_gate_errors"] = ["C2_MESSAGE_HISTORY_GAP"]
    payload["evidence"]["flow_gate_details"] = [
        {
            "error_code": "C2_MESSAGE_HISTORY_GAP",
            "position_source": "slot_ledger_visual_top",
            "min_screen_order": 3,
            "max_screen_order": 3,
        }
    ]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["message_batch"]["batch_status"] == (
        "recoverable_hold"
    )
    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert conversation.status == "waiting_user_reply"
        assert persisted_binding.recovery_hold["status"] == "active"
        assert db.query(HandoffEvent).count() == 0
        assert db.query(MessageBatch).count() == 0

    endpoint = f"/api/workers/{worker['id']}/wechat/messages/ingest"
    for attempt in (1, 2):
        authorization = client.get(
            f"/api/workers/{worker['id']}/wechat/conversations/"
            f"{binding['conversation_id']}/read-authorization",
            headers=_worker_headers(worker),
        )
        assert authorization.status_code == 200
        reread = _v3_ingest_payload(
            binding,
            remark_code,
            read_run_id=f"read-gate-after-sales-reread-{attempt}",
            messages=[],
            read_reason="waiting_user_reply",
        )
        reread["authorization_revision"] = authorization.json()["data"][
            "authorization_revision"
        ]
        reread["evidence"]["flow_gate_errors"] = [
            "C2_MESSAGE_HISTORY_GAP"
        ]
        reread["evidence"]["flow_gate_details"] = [
            {
                "error_code": "C2_MESSAGE_HISTORY_GAP",
                "position_source": "slot_ledger_visual_top",
                "gate_scope": "conversation_identity",
                "min_screen_order": 3,
                "max_screen_order": 3,
                "boundary_relation": "unknown",
            }
        ]
        reread["evidence"]["recovery_attempt_kind"] = "stable_reread"
        retry = client.post(
            endpoint,
            json=reread,
            headers=_worker_headers(worker),
        )
        assert retry.status_code == 200, retry.text

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        persisted_binding = db.get(WechatSessionBinding, binding["id"])
        assert conversation.status == "waiting_sales_reply"
        assert persisted_binding.recovery_hold["status"] == "escalated"
        assert db.query(HandoffEvent).count() == 1


def test_voice_transcription_dict_metadata_does_not_become_message_content():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-voice-dict-meta",
            messages=[
                _v3_message(
                    "sales-voice-dict-meta",
                    role="self",
                    message_type="voice",
                    content="你中午回家吃饭不？",
                    screen_order=1,
                    raw_extra={
                        "voice_transcription": {
                            "state": "voice_transcribe_completed",
                            "attempt_count": 1,
                            "raw": {"transcribed_messages": [], "after_screenshot_path": "C:/tmp/after.png"},
                        }
                    },
                )
            ],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    assert response.json()["data"]["ingested_count"] == 1
    with SessionLocal() as db:
        message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
        assert message.message_type == "voice"
        assert message.content == "你中午回家吃饭不？"
        assert "voice_transcribe_completed" not in message.content


def test_voice_transcription_failures_are_rejected_and_create_no_reply_action():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    error_codes = [
        "VOICE_TRANSCRIBE_FAILED",
        "VOICE_TRANSCRIBE_CLICK_FAILED",
        "VOICE_TRANSCRIBE_LOCK_TIMEOUT",
        "VOICE_TRANSCRIBE_EMPTY",
        "VOICE_MESSAGE_UNCONFIRMED",
        "TARGET_NOT_CONFIRMED_FOR_VOICE_TRANSCRIBE",
    ]
    for index, error_code in enumerate(error_codes):
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=_v3_ingest_payload(
                binding,
                remark_code,
                read_run_id=f"read-voice-failed-{index}",
                messages=[
                    _v3_message(
                        f"voice-failed-{index}",
                        role="customer",
                        message_type="voice",
                        content="未完成语音",
                        screen_order=1,
                        raw_extra={"error_code": error_code, "voice_duration_seconds": 5},
                    )
                ],
            ),
            headers=_worker_headers(worker),
        )
        assert response.status_code == 409
        assert response.json()["code"] == error_code

    empty = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-voice-empty-duration-only",
            messages=[
                _v3_message(
                    "voice-empty",
                    role="customer",
                    message_type="voice",
                    content='5"',
                    screen_order=1,
                )
            ],
        ),
        headers=_worker_headers(worker),
    )
    assert empty.status_code == 409
    assert empty.json()["code"] == "VOICE_TRANSCRIBE_INVALID_CONTENT"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0
        assert db.query(MessageBatch).count() == 0
        assert db.query(ReplyAction).count() == 0


def test_invalid_committed_voice_requires_outbox_quarantine_without_fact_rewrite():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("失败语音重建客户", "13896676692")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    invalid = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-rebuilt-failed-voice",
        messages=[
            _v3_message(
                "preserved-valid-voice",
                role="customer",
                message_type="voice",
                content="这条语音已经成功转写。",
                screen_order=1,
                order_source="visual_top",
            ),
            _v3_message(
                "rebuilt-failed-voice",
                role="customer",
                message_type="voice",
                content='5"',
                screen_order=2,
                order_source="visual_top",
            )
        ],
    )
    rejected = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=invalid,
        headers=_worker_headers(worker),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "VOICE_TRANSCRIBE_INVALID_CONTENT"
    assert (
        rejected.json()["data"]["source_message_key"]
        == "rebuilt-failed-voice"
    )
    assert (
        rejected.json()["data"]["recovery_action"]
        == "identity_quarantined"
    )
    assert invalid["messages"][0]["item_state"] == "completed"
    assert invalid["messages"][1]["item_state"] == "completed"
    assert invalid["messages"][1]["content"] == '5"'
    with SessionLocal() as db:
        assert db.query(MessageEvent).count() == 0
        assert db.query(HandoffEvent).count() == 0


def test_repeated_scan_after_worker_restart_keeps_single_binding_and_returns_already_bound():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    assert first.json()["data"]["bindings"][0]["bind_status"] == "bound"

    restarted_heartbeat = client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        json={"client_instance_id": "client-a", "run_status": "running", "rpa_component_status": "ready", "running_status": "idle"},
        headers={"X-Worker-Token": worker["worker_token"]},
    )
    assert restarted_heartbeat.status_code == 200

    repeated_payload = _scan_payload(remark_code)
    repeated_payload["scan_id"] = "scan-002"
    repeated = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=repeated_payload, headers=_worker_headers(worker))
    assert repeated.status_code == 200
    assert repeated.json()["data"]["bindings"][0]["bind_status"] == "already_bound"
    assert repeated.json()["data"]["bound_count"] == 1


def test_duplicate_scan_id_returns_first_result_without_rebinding():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    first_payload = _scan_payload(remark_code, rpa_session_key="wx-row-1")
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]

    duplicate_payload = _scan_payload(remark_code, rpa_session_key="wx-row-2")
    duplicate_payload["sessions"][0]["display_name"] = "不应覆盖"
    duplicate = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=duplicate_payload, headers=_worker_headers(worker))
    assert duplicate.status_code == 200
    duplicate_data = duplicate.json()["data"]
    assert duplicate_data["bindings"][0]["id"] == first_binding["id"]
    assert duplicate_data["bindings"][0]["rpa_session_key"] == "wx-row-1"

    with SessionLocal() as db:
        bindings = db.query(WechatSessionBinding).filter(WechatSessionBinding.lead_id == first_binding["lead_id"]).all()
        assert len(bindings) == 1


def test_duplicate_scan_id_cannot_reuse_another_workers_response_snapshot():
    worker_a = _create_worker()
    _create_sales(worker_a["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker_a)

    first = client.post(
        f"/api/workers/{worker_a['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker_a),
    )
    assert first.status_code == 200

    worker_b = _create_worker()
    conflict = client.post(
        f"/api/workers/{worker_b['id']}/wechat/sessions/scan-result",
        json=_scan_payload(None),
        headers=_worker_headers(worker_b),
    )

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "SESSION_SCAN_ID_CONFLICT"
    assert conflict.json()["trace_id"]
    assert "bindings" not in (conflict.json().get("data") or {})


def test_scan_failed_returns_error_code_and_trace_id():
    worker = _create_worker()
    payload = _scan_payload(None)
    payload["sessions"] = []
    payload["scan_failed"] = True
    payload["error_code"] = "WECHAT_WINDOW_NOT_READY"

    response = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=payload, headers=_worker_headers(worker))
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["error_code"] == "WECHAT_WINDOW_NOT_READY"
    assert body["trace_id"]


def test_read_targets_excludes_closed_and_rejected_conversations_but_allows_degraded_state_targets():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "closed"
        db.commit()
    targets = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert targets.status_code == 200
    assert targets.json()["data"]["targets"] == []

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = None
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.listen_status = "degraded"
        binding_row.unread_hint = False
        db.commit()
    degraded = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert degraded.status_code == 200
    assert degraded.json()["data"]["targets"][0]["read_reason"] == "waiting_user_reply"

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "rejected"
        db.commit()
    rejected = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert rejected.status_code == 200
    assert rejected.json()["data"]["targets"] == []


def test_read_targets_only_returns_v06_state_machine_reasons_and_recall_precheck_creates_no_follow_up():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    allowed = {"recall_precheck", "visible_unread", "recent_ai_sent", "waiting_user_reply", "waiting_sales_reply"}

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "recall_precheck"
        db.commit()

    response = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert response.status_code == 200
    targets = response.json()["data"]["targets"]
    assert targets[0]["read_reason"] == "recall_precheck"
    assert all(item["read_reason"] in allowed for item in targets)

    with SessionLocal() as db:
        assert db.query(Task).filter(Task.task_type == "follow_up").count() == 0


def test_read_targets_degrades_bound_binding_without_remark_code():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.remark_code = None
        binding_row.bind_status = "bound"
        binding_row.listen_status = "listening"
        binding_row.allow_listening = True
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "recall_precheck"
        db.commit()

    response = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert response.status_code == 200
    assert response.json()["data"]["targets"] == []

    admin_binding = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS)
    assert admin_binding.status_code == 200
    data = admin_binding.json()["data"]
    assert data["bind_status"] == "needs_review"
    assert data["listen_status"] == "degraded"
    assert data["allow_listening"] is False
    assert data["error_code"] == "C2_TARGET_REMARK_CODE_MISSING"


def test_read_targets_allows_missing_row_fingerprint_and_omits_optional_field():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    payload = _scan_payload(remark_code)
    del payload["sessions"][0]["row_fingerprint"]
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=payload, headers=_worker_headers(worker))
    assert scan.status_code == 200
    binding = scan.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "waiting_user_reply"
        conversation.last_ai_reply_at = utcnow()
        db.commit()

    response = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert response.status_code == 200
    target = response.json()["data"]["targets"][0]
    assert target["conversation_id"] == binding["conversation_id"]
    assert target["remark_code"] == remark_code
    assert target["rpa_session_key"] == "wx-row-1"
    assert target["display_name"] == remark_code
    assert "row_fingerprint" not in target


def test_read_targets_uses_conversation_and_remark_code_even_without_local_locator():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.rpa_session_key = ""
        binding_row.display_name = ""
        binding_row.bind_status = "bound"
        binding_row.listen_status = "listening"
        binding_row.allow_listening = True
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "recall_precheck"
        db.commit()

    response = client.get(f"/api/workers/{worker['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker))
    assert response.status_code == 200
    targets = response.json()["data"]["targets"]
    assert len(targets) == 1
    assert targets[0]["conversation_id"] == binding["conversation_id"]
    assert targets[0]["remark_code"] == remark_code
    assert targets[0]["rpa_session_key"] == ""
    assert targets[0]["display_name"] == ""
    assert targets[0]["read_reason"] == "recall_precheck"

    admin_binding = client.get(f"/api/conversations/{binding['conversation_id']}/wechat-binding", headers=HEADERS)
    data = admin_binding.json()["data"]
    assert data["bind_status"] == "bound"
    assert data["listen_status"] == "listening"
    assert data["allow_listening"] is True
    assert data["error_code"] is None


def test_message_ingest_allows_changed_or_empty_rpa_session_key_and_dedupes_by_conversation():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    changed_locator_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-changed-locator",
        rpa_session_key="wx-row-after-search",
        messages=[_v3_message("msg-locator-change", role="customer", message_type="text", content="短码搜索后读到的新消息", screen_order=1)],
    )
    first = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=changed_locator_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    assert first.json()["data"]["ingested_count"] == 1

    empty_locator_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-empty-locator",
        rpa_session_key="",
        messages=[_v3_message("msg-empty-locator", role="customer", message_type="text", content="没有稳定本地定位键", screen_order=1)],
    )
    second = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=empty_locator_payload, headers=_worker_headers(worker))
    assert second.status_code == 200
    assert second.json()["data"]["ingested_count"] == 1

    duplicated = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=changed_locator_payload, headers=_worker_headers(worker))
    assert duplicated.status_code == 200
    assert duplicated.json()["data"]["duplicated_count"] == 1
    assert duplicated.json()["data"]["results"][0]["error_code"] == "MESSAGE_INGEST_DUPLICATED"
    with SessionLocal() as db:
        messages = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).order_by(MessageEvent.dedupe_key).all()
        assert [message.dedupe_key for message in messages] == ["msg-empty-locator", "msg-locator-change"]
        assert [message.rpa_session_key for message in messages] == ["", "wx-row-after-search"]


def test_message_ingest_rejects_mismatched_observed_remark_code():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]

    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-wrong-target",
        messages=[_v3_message("wrong-target-message", role="customer", message_type="text", content="不应写入错误会话", screen_order=1)],
    )
    payload["remark_code"] = "ANOTHER_CHAT"
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_TARGET_IDENTITY_MISMATCH"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0


def test_message_ingest_read_target_failures_are_rejected_and_do_not_trigger_ai():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    for failure in ["target_not_confirmed", "search_not_found", "search_ambiguous"]:
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=_v3_ingest_payload(
                binding,
                remark_code,
                read_run_id=f"read-{failure}",
                rpa_session_key="wx-row-maybe-stale",
                messages=[
                    _v3_message(
                        f"msg-{failure}",
                        role="customer",
                        message_type="text",
                        content="这条不能触发 AI",
                        screen_order=1,
                        raw_extra={"read_result": failure},
                    )
                ],
            ),
            headers=_worker_headers(worker),
        )
        assert response.status_code == 409
        assert response.json()["code"] == failure.upper()

    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0
        assert db.query(MessageBatch).count() == 0


def test_message_ingest_duplicate_key_is_conversation_scoped_across_worker_change():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]
    payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="read-001",
        messages=[_v3_message("msg-cross-worker", role="customer", message_type="text", content="你好", screen_order=1)],
    )
    first = client.post(f"/api/workers/{worker['id']}/wechat/messages/ingest", json=payload, headers=_worker_headers(worker))
    assert first.status_code == 200

    worker_b = _create_worker()
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        binding_row.worker_id = worker_b["id"]
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.worker_id = worker_b["id"]
        db.commit()

    duplicated = client.post(f"/api/workers/{worker_b['id']}/wechat/messages/ingest", json=payload, headers=_worker_headers(worker_b))
    assert duplicated.status_code == 200
    data = duplicated.json()["data"]
    assert data["duplicated_count"] == 1
    assert data["results"][0]["ingest_result"] == "duplicated"
    assert data["results"][0]["error_code"] == "MESSAGE_INGEST_DUPLICATED"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 1


def test_message_ingest_rejects_unknown_sender_and_closed_conversation():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("王先生", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
    binding = scan.json()["data"]["bindings"][0]

    unknown = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json={
            **_v3_ingest_payload(
                binding,
                remark_code,
                read_run_id="read-unknown",
                messages=[_v3_message("msg-unknown", role="customer", message_type="text", content="?", screen_order=1)],
            ),
            "messages": [
                {
                    **_v3_message("msg-unknown", role="customer", message_type="text", content="?", screen_order=1),
                    "sender_role_hint": "unknown",
                }
            ],
        },
        headers=_worker_headers(worker),
    )
    assert unknown.status_code == 409
    assert unknown.json()["code"] == "MESSAGE_ROW_SENDER_ROLE_MISMATCH"

    with SessionLocal() as db:
        conversation = db.get(Conversation, binding["conversation_id"])
        conversation.status = "closed"
        db.commit()

    closed = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="read-closed",
            messages=[_v3_message("msg-closed", role="customer", message_type="text", content="还在吗", screen_order=1)],
        ),
        headers=_worker_headers(worker),
    )
    assert closed.status_code == 409
    assert closed.json()["code"] == "CONVERSATION_STATUS_NOT_LISTENABLE"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).count() == 0


def test_only_contract_self_role_can_pause_ai_for_sales_side_message():
    for role in ["self", "sales", "sales_candidate"]:
        setup_function()
        worker = _create_worker()
        _create_sales(worker["id"])
        _create_lead("王先生", "13896676678")
        remark_code = _pull_remark_code(worker)
        scan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=_scan_payload(remark_code), headers=_worker_headers(worker))
        binding = scan.json()["data"]["bindings"][0]

        payload = _v3_ingest_payload(
            binding,
            remark_code,
            read_run_id=f"read-{role}",
            messages=[
                {
                    **_v3_message(
                        f"msg-{role}",
                        role="self",
                        message_type="text",
                        content="我是销售，稍后联系您",
                        screen_order=1,
                        raw_extra={"sender_role_confidence": 0.87},
                    ),
                    "sender_role_hint": role,
                }
            ],
        )
        payload["evidence"]["sender_role_evidence"] = {"source": "omniauto_v3_contract"}
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=payload,
            headers=_worker_headers(worker),
        )

        if role != "self":
            assert response.status_code == 409
            assert response.json()["code"] == "MESSAGE_ROW_SENDER_ROLE_MISMATCH"
            continue
        assert response.status_code == 200
        assert response.json()["data"]["ingested_count"] == 1
        with SessionLocal() as db:
            message = db.query(MessageEvent).filter(MessageEvent.conversation_id == binding["conversation_id"]).one()
            conversation = db.get(Conversation, binding["conversation_id"])
            assert message.sender_role == role
            assert message.raw_payload["sender_role_confidence"] == 0.87
            assert message.evidence["sender_role_evidence"]["source"] == "omniauto_v3_contract"
            assert conversation.status == "sales_replied_waiting_user"
            assert conversation.ai_enabled is True
            assert db.query(MessageBatch).count() == 0


def test_same_worker_same_remark_code_updates_existing_binding_even_display_name_or_session_key_changes():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-old")
    first_payload["sessions"][0]["display_name"] = "CJTEST01 许聪"
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]
    assert first_binding["bind_status"] == "bound"

    renamed_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-new")
    renamed_payload["scan_id"] = "scan-002"
    renamed_payload["sessions"][0]["display_name"] = "CJTEST01许聪"
    renamed_payload["sessions"][0]["row_fingerprint"] = "fingerprint-renamed"
    renamed = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=renamed_payload, headers=_worker_headers(worker))
    assert renamed.status_code == 200
    renamed_binding = renamed.json()["data"]["bindings"][0]
    assert renamed_binding["bind_status"] == "already_bound"
    assert renamed_binding["id"] == first_binding["id"]
    assert renamed_binding["conversation_id"] == first_binding["conversation_id"]
    assert renamed_binding["display_name"] == "CJTEST01许聪"
    assert renamed_binding["rpa_session_key"] == "wx-row-new"
    assert "reason_code" not in renamed_binding
    assert "conversation_status" not in renamed_binding
    assert "ai_enabled" not in renamed_binding

    bindings = client.get(f"/api/leads/{first_binding['lead_id']}/wechat-bindings", headers=HEADERS)
    assert bindings.status_code == 200
    assert len(bindings.json()["data"]["items"]) == 1


def test_same_remark_code_session_key_change_retires_stale_binding_with_messages_instead_of_deleting():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-old")
    first_payload["sessions"][0]["display_name"] = "CJTEST01 许聪"
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        stale = WechatSessionBinding(
            worker_id=worker["id"],
            display_name="旧行残留",
            rpa_session_key="wx-row-new",
            row_fingerprint="stale-fingerprint",
            bind_status="bound",
            listen_status="listening",
            allow_listening=True,
            remark_code="CJSTALE",
        )
        db.add(stale)
        db.flush()
        db.add(
            MessageEvent(
                conversation_id=stale.conversation_id,
                binding_id=stale.id,
                worker_id=worker["id"],
                rpa_session_key=stale.rpa_session_key,
                read_run_id="read-stale",
                dedupe_key="msg-stale",
                sender_role="customer",
                message_type="text",
                content="旧 binding 已经被消息引用",
            )
        )
        db.commit()
        stale_id = stale.id

    renamed_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-new")
    renamed_payload["scan_id"] = "scan-002"
    renamed_payload["sessions"][0]["display_name"] = "CJTEST01许聪"
    renamed = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=renamed_payload, headers=_worker_headers(worker))
    assert renamed.status_code == 200
    renamed_binding = renamed.json()["data"]["bindings"][0]
    assert renamed_binding["id"] == first_binding["id"]
    assert renamed_binding["rpa_session_key"] == "wx-row-new"

    with SessionLocal() as db:
        stale_row = db.get(WechatSessionBinding, stale_id)
        message = db.query(MessageEvent).filter(MessageEvent.binding_id == stale_id).one()
        assert stale_row is not None
        assert stale_row.bind_status == "disabled"
        assert stale_row.listen_status == "disabled"
        assert stale_row.deleted_at is not None
        assert stale_row.rpa_session_key.startswith("wx-row-new#retired#")
        assert stale_row.error_code == "SESSION_BINDING_REPLACED_BY_REMARK_CODE"
        assert message.content == "旧 binding 已经被消息引用"


def test_scan_result_moves_unproven_disabled_binding_to_review_and_restores_safely():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-old")
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        row = db.get(WechatSessionBinding, first_binding["id"])
        assert row is not None
        row.bind_status = "disabled"
        row.listen_status = "disabled"
        row.allow_listening = False
        row.error_code = "MANUAL_TEST_DISABLED"
        db.add(
            MessageEvent(
                conversation_id=row.conversation_id,
                binding_id=row.id,
                worker_id=worker["id"],
                rpa_session_key=row.rpa_session_key,
                read_run_id="read-disabled",
                dedupe_key="msg-disabled",
                sender_role="customer",
                message_type="text",
                content="已有消息引用这条 binding",
            )
        )
        db.commit()

    rescan_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-new")
    rescan_payload["scan_id"] = "scan-disabled-rescan"
    rescan_payload["sessions"][0]["display_name"] = "CJTEST01许聪"
    rescan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=rescan_payload, headers=_worker_headers(worker))

    assert rescan.status_code == 200
    binding = rescan.json()["data"]["bindings"][0]
    assert binding["id"] == first_binding["id"]
    assert binding["bind_status"] == "needs_review"
    assert binding["listen_status"] == "paused"
    assert binding["error_code"] == "SESSION_BINDING_STATE_INCONSISTENT"
    assert binding["recovery_state"] == "needs_review"
    assert binding["can_ingest_messages"] is False

    with SessionLocal() as db:
        rows = db.query(WechatSessionBinding).filter(WechatSessionBinding.worker_id == worker["id"], WechatSessionBinding.remark_code == "CJTEST01").all()
        assert len(rows) == 1
        message = db.query(MessageEvent).filter(MessageEvent.binding_id == first_binding["id"]).one()
        assert message.content == "已有消息引用这条 binding"

    restored = client.post(
        f"/api/conversations/{first_binding['conversation_id']}/wechat-binding/restore",
        json={"reason": "历史状态缺少永久停用证据，人工核实客户仍有效"},
        headers=HEADERS,
    )
    assert restored.status_code == 200, restored.text
    restored_binding = restored.json()["data"]
    assert restored_binding["id"] == first_binding["id"]
    assert restored_binding["bind_status"] == "bound"
    assert restored_binding["listen_status"] == "paused"
    assert restored_binding["allow_listening"] is False
    assert restored_binding["error_code"] == "SESSION_BINDING_RESTORE_PENDING_SCAN"
    assert restored_binding["recovery_state"] == "paused_waiting_worker"

    final_scan_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-new")
    final_scan_payload["scan_id"] = "scan-disabled-after-manual-restore"
    final_scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=final_scan_payload,
        headers=_worker_headers(worker),
    )
    assert final_scan.status_code == 200
    final_binding = final_scan.json()["data"]["bindings"][0]
    assert final_binding["id"] == first_binding["id"]
    assert final_binding["listen_status"] == "listening"
    assert final_binding["can_ingest_messages"] is True
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(
            MessageEvent.binding_id == first_binding["id"]
        ).one().content == "已有消息引用这条 binding"


def test_scan_keeps_complete_permanent_disable_evidence_blocked():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("明确永久停用客户", "13896676679")
    remark_code = _pull_remark_code(worker)
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = first.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        row = db.get(WechatSessionBinding, binding["id"])
        assert row is not None
        row.bind_status = "disabled"
        row.listen_status = "disabled"
        row.allow_listening = False
        row.disable_reason = "admin_disabled"
        row.disabled_at = utcnow()
        row.disabled_by = "operator:admin-disable"
        db.commit()

    rescan_payload = _scan_payload(remark_code)
    rescan_payload["scan_id"] = "scan-complete-permanent-disable"
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=rescan_payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200
    blocked = response.json()["data"]["bindings"][0]
    assert blocked["id"] == binding["id"]
    assert blocked["bind_status"] == "disabled"
    assert blocked["error_code"] == "SESSION_BINDING_DISABLED"
    assert blocked["recovery_state"] == "permanently_disabled"
    assert blocked["can_ingest_messages"] is False


def test_same_remark_code_duplicate_active_binding_is_rejected_and_history_stays_canonical():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-history")
    first_payload["sessions"][0]["display_name"] = "CJTEST01 许聪"
    first = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=first_payload, headers=_worker_headers(worker))
    assert first.status_code == 200
    canonical = first.json()["data"]["bindings"][0]

    with SessionLocal() as db:
        canonical_row = db.get(WechatSessionBinding, canonical["id"])
        assert canonical_row is not None
        db.add(
            MessageEvent(
                conversation_id=canonical_row.conversation_id,
                binding_id=canonical_row.id,
                worker_id=worker["id"],
                rpa_session_key=canonical_row.rpa_session_key,
                read_run_id="read-history",
                dedupe_key="msg-history",
                sender_role="customer",
                message_type="text",
                content="这条消息决定 canonical conversation",
                ingested_at=utcnow(),
            )
        )
        canonical_row.last_ingested_at = utcnow()
        db.commit()
        duplicate = WechatSessionBinding(
            worker_id=worker["id"],
            lead_id=canonical_row.lead_id,
            sales_id=canonical_row.sales_id,
            display_name="CJTEST01 许聪",
            rpa_session_key="wx-row-empty",
            row_fingerprint="fingerprint-empty",
            bind_status="bound",
            listen_status="listening",
            allow_listening=True,
            remark_code="CJTEST01",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    rescan_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-empty")
    rescan_payload["scan_id"] = "scan-duplicate-empty"
    rescan_payload["sessions"][0]["display_name"] = "CJTEST01许聪"
    rescan = client.post(f"/api/workers/{worker['id']}/wechat/sessions/scan-result", json=rescan_payload, headers=_worker_headers(worker))

    assert rescan.status_code == 200
    binding = rescan.json()["data"]["bindings"][0]
    assert binding["id"] == canonical["id"]
    assert binding["conversation_id"] == canonical["conversation_id"]
    assert binding["bind_status"] == "already_bound"

    with SessionLocal() as db:
        canonical_row = db.get(WechatSessionBinding, canonical["id"])
        assert canonical_row is not None
        assert canonical_row.conversation_id == canonical["conversation_id"]
        assert canonical_row.rpa_session_key == "wx-row-empty"
        messages = db.query(MessageEvent).filter(MessageEvent.binding_id == canonical["id"]).all()
        assert [message.content for message in messages] == ["这条消息决定 canonical conversation"]


def test_same_remark_code_reuses_canonical_binding_when_sales_moves_to_another_worker():
    worker_a = _create_worker()
    sales_id = _create_sales(worker_a["id"])
    _create_lead("许聪", "13896676680", {"remark_code": "CJTEST01"})

    first = client.post(
        f"/api/workers/{worker_a['id']}/wechat/sessions/scan-result",
        json=_scan_payload("CJTEST01", rpa_session_key="wx-row-worker-a"),
        headers=_worker_headers(worker_a),
    )
    assert first.status_code == 200
    first_binding = first.json()["data"]["bindings"][0]

    worker_b = _create_worker()
    with SessionLocal() as db:
        sales = db.get(Sales, sales_id)
        assert sales is not None
        sales.worker_id = worker_b["id"]
        db.commit()

    migrated_payload = _scan_payload("CJTEST01", rpa_session_key="wx-row-worker-b")
    migrated_payload["scan_id"] = "scan-worker-b"
    migrated = client.post(
        f"/api/workers/{worker_b['id']}/wechat/sessions/scan-result",
        json=migrated_payload,
        headers=_worker_headers(worker_b),
    )

    assert migrated.status_code == 200
    migrated_binding = migrated.json()["data"]["bindings"][0]
    assert migrated_binding["id"] == first_binding["id"]
    assert migrated_binding["conversation_id"] == first_binding["conversation_id"]
    assert migrated_binding["worker_id"] == worker_b["id"]
    assert migrated_binding["rpa_session_key"] == "wx-row-worker-b"

    targets_a = client.get(f"/api/workers/{worker_a['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker_a))
    targets_b = client.get(f"/api/workers/{worker_b['id']}/wechat/sessions/read-targets", headers=_worker_headers(worker_b))
    assert targets_a.status_code == 200
    assert targets_b.status_code == 200
    assert targets_a.json()["data"]["targets"] == []


def test_server_identity_checkpoint_restores_worker_sequence_across_worker_change():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("身份恢复客户", "13896676671")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    old_worker = _create_worker()
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert binding_row is not None
        assert conversation is not None
        binding_row.unread_hint = False
        binding_row.last_read_conversation_status = "waiting_sales_reply"
        binding_row.next_read_due_at = utcnow() - timedelta(seconds=1)
        conversation.status = "waiting_sales_reply"
        db.add(
            MessageEvent(
                conversation_id=binding["conversation_id"],
                binding_id=binding["id"],
                lead_id=binding["lead_id"],
                sales_id=binding["sales_id"],
                worker_id=old_worker["id"],
                rpa_session_key="old-worker-row",
                read_run_id="old-worker-read",
                contract_version=3,
                source_message_key="old-worker-source-7",
                dedupe_key="old-worker-dedupe-7",
                sender_role="customer",
                message_type="text",
                content="服务端保存的第七条消息",
                raw_payload={
                    "dedupe_basis": {
                        "source": "worker_cross_round_sequence",
                        "worker_stable_id": "worker-message-7",
                    }
                },
                evidence={},
                item_state="completed",
                flow_state="completed",
            )
        )
        db.commit()

    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    authorization = client.get(
        f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/read-authorization",
        headers=_worker_headers(worker),
    )

    assert targets.status_code == 200
    assert authorization.status_code == 200
    target_checkpoint = targets.json()["data"]["targets"][0][
        "identity_checkpoint"
    ]
    authorization_checkpoint = authorization.json()["data"][
        "identity_checkpoint"
    ]
    assert target_checkpoint == authorization_checkpoint
    assert target_checkpoint["version"] == 3
    assert target_checkpoint["next_sequence_floor"] == 8
    assert target_checkpoint["recent_messages"] == [
        {
            "stable_id": "worker-message-7",
            "source_message_key": "old-worker-source-7",
            "origin_read_run_id": "old-worker-read",
            "dedupe_key": "old-worker-dedupe-7",
            "sender_role": "customer",
            "message_type": "text",
            "normalized_content_hash": hashlib.sha256(
                "服务端保存的第七条消息".encode("utf-8")
            ).hexdigest(),
            "media_identity_hash": "",
            "alignment_signature": target_checkpoint["recent_messages"][0][
                "alignment_signature"
            ],
                "native_source_message_id": "",
                "frame_visual_id": "",
                "business_projection": {},
                "strong_boundary_tokens": [],
                "message_identity_commit_record": {},
                "message_identity_runtime_evidence": {},
            }
        ]


@pytest.mark.parametrize(
    ("collision_kind", "incoming_role", "incoming_content"),
    [
        ("different_content", "customer", "同编号但正文不同"),
        ("different_sender", "self", "原始正文"),
    ],
)
def test_duplicate_key_identity_collision_rolls_back_without_consuming_or_brain(
    collision_kind,
    incoming_role,
    incoming_content,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"身份碰撞-{collision_kind}", "13896676672")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    first_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="identity-original-read",
        messages=[
            _v3_message(
                "worker-message-collision",
                role="customer",
                message_type="text",
                content="原始正文",
                screen_order=1,
            )
        ],
    )
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=first_payload,
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200

    unread_scan = _scan_payload(remark_code)
    unread_scan["scan_id"] = f"identity-collision-scan-{collision_kind}"
    refreshed = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=unread_scan,
        headers=_worker_headers(worker),
    )
    assert refreshed.status_code == 200
    collision_payload = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id=f"identity-collision-read-{collision_kind}",
        read_reason="visible_unread",
        messages=[
            _v3_message(
                "worker-message-collision",
                role=incoming_role,
                message_type="text",
                content=incoming_content,
                screen_order=1,
            )
        ],
    )
    before_revision = _binding_authorization_revision(binding["id"])
    with SessionLocal() as db:
        before_conversation = db.get(Conversation, binding["conversation_id"])
        assert before_conversation is not None
        before_status = before_conversation.status
        before_batch_count = db.query(MessageBatch).count()

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=collision_payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_IDENTITY_COLLISION"
    assert response.json()["data"]["recovery_action"] == (
        "identity_quarantined"
    )
    assert response.json()["data"]["source_message_key"]
    assert response.json()["data"]["next_sequence_floor"] >= 1
    assert response.json()["trace_id"]
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert binding_row is not None
        assert conversation is not None
        assert binding_row.unread_hint is True
        assert wechat_service._authorization_revision(binding_row) == before_revision
        assert binding_row.last_read_run_id == "identity-original-read"
        assert conversation.status == before_status
        assert db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).count() == 1
        assert db.query(MessageBatch).count() == before_batch_count


def test_integrity_error_duplicate_branch_rechecks_full_message_identity(monkeypatch):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("并发身份碰撞", "13896676676")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    original = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="concurrent-original-read",
        messages=[
            _v3_message(
                "concurrent-dedupe-key",
                role="customer",
                message_type="text",
                content="并发前原始正文",
                screen_order=1,
            )
        ],
    )
    first = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=original,
        headers=_worker_headers(worker),
    )
    assert first.status_code == 200

    incoming_message = _v3_message(
        "concurrent-new-source",
        role="customer",
        message_type="text",
        content="并发时不同正文",
        screen_order=1,
    )
    incoming_message["dedupe_key"] = "concurrent-dedupe-key"
    payload_dict = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="concurrent-collision-read",
        read_reason="waiting_sales_reply",
        messages=[incoming_message],
    )
    payload = WechatMessageIngestRequest.model_validate(payload_dict)

    with SessionLocal() as db:
        worker_row = db.get(Worker, worker["id"])
        assert worker_row is not None
        original_scalar = db.scalar
        skipped_precheck = False
        seen_scalar_sql = []

        def race_scalar(statement, *args, **kwargs):
            nonlocal skipped_precheck
            sql = str(statement).lower()
            seen_scalar_sql.append(sql)
            where_clause = sql.partition("\nwhere ")[2]
            if (
                not skipped_precheck
                and "message_events" in sql
                and "dedupe_key" in where_clause
                and "read_run_id" not in where_clause
            ):
                skipped_precheck = True
                return None
            return original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(db, "scalar", race_scalar)
        with pytest.raises(AppError) as exc:
            wechat_service.ingest_messages(db, worker_row, payload)
        db.rollback()

    assert skipped_precheck is True, seen_scalar_sql
    assert exc.value.code == "MESSAGE_IDENTITY_COLLISION"
    assert exc.value.status_code == 409
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).count() == 1


def test_duplicate_voice_key_with_different_media_anchor_is_identity_collision():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("语音媒体身份碰撞", "13896676677")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    original_message = _v3_message(
        "voice-anchor-one",
        role="customer",
        message_type="voice",
        content="相同语音正文",
        screen_order=1,
    )
    original = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="voice-anchor-original",
            messages=[original_message],
        ),
        headers=_worker_headers(worker),
    )
    assert original.status_code == 200, original.text

    incoming_message = _v3_message(
        "voice-anchor-two",
        role="customer",
        message_type="voice",
        content="相同语音正文",
        screen_order=1,
    )
    incoming_message["dedupe_key"] = original_message["dedupe_key"]
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="voice-anchor-collision",
            messages=[incoming_message],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MESSAGE_IDENTITY_COLLISION"
    collision = response.json()["data"]
    assert collision["source_message_key"] == "voice-anchor-two"
    existing_media_hash = collision["existing_identity"][
        "media_identity_hash"
    ]
    incoming_media_hash = collision["incoming_identity"][
        "media_identity_hash"
    ]
    assert existing_media_hash
    assert incoming_media_hash
    assert existing_media_hash != incoming_media_hash


def test_same_voice_identity_is_duplicated_when_only_frame_anchor_moves():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("语音位置变化不改身份", "13896676678")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    original_message = _v3_message(
        "voice-stable-source",
        role="customer",
        message_type="voice",
        content="同一条语音转写",
        screen_order=1,
        raw_extra={
            "dedupe_basis": {
                "source": "worker_cross_round_sequence",
                "worker_stable_id": "worker-message-31",
            }
        },
    )
    original = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="voice-position-original",
            messages=[original_message],
        ),
        headers=_worker_headers(worker),
    )
    assert original.status_code == 200, original.text

    moved_message = copy.deepcopy(original_message)
    moved_observation = moved_message["raw_payload"]["observation"]
    moved_observation["parent_voice_anchor_key"] = "anchor:moved-row"
    moved_observation["source_message"][
        "voice_anchor_stable_key"
    ] = "anchor:moved-row"
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="voice-position-reread",
            messages=[moved_message],
        ),
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    result = response.json()["data"]["results"][0]
    assert result["ingest_result"] == "duplicated"
    with SessionLocal() as db:
        assert db.query(MessageEvent).filter(
            MessageEvent.conversation_id == binding["conversation_id"]
        ).count() == 1


@pytest.mark.parametrize("previous_status", ["ai_active", ""])
def test_entering_waiting_sales_reply_preserves_first_two_minute_cooldown(
    previous_status: str,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("转人工首次冷却客户", "13896676672")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    _seed_open_handoff(binding, paused=False)
    handoff_at = utcnow()
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert binding_row is not None
        assert conversation is not None
        binding_row.last_read_conversation_status = previous_status
        binding_row.next_read_due_at = None
        conversation.status = "waiting_sales_reply"
        conversation.handoff_at = handoff_at
        db.commit()

    response = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["targets"] == []
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        assert binding_row.last_read_conversation_status == "waiting_sales_reply"
        assert binding_row.next_read_due_at is not None
        due_at = binding_row.next_read_due_at
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        assert abs(
            (
                due_at.astimezone(timezone.utc)
                - handoff_at.astimezone(timezone.utc)
            ).total_seconds()
            - 120
        ) < 1


def test_empty_reads_back_off_and_same_unread_generation_does_not_wake_early():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("读取退避客户", "13896676673")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert binding_row is not None
        assert conversation is not None
        binding_row.unread_hint = False
        conversation.status = "waiting_sales_reply"
        db.commit()

    for index, expected_seconds in enumerate((120, 300, 600), start=1):
        payload = _v3_ingest_payload(
            binding,
            remark_code,
            read_run_id=f"empty-read-{index}",
            messages=[],
            read_reason="waiting_sales_reply",
        )
        response = client.post(
            f"/api/workers/{worker['id']}/wechat/messages/ingest",
            json=payload,
            headers=_worker_headers(worker),
        )
        assert response.status_code == 200, response.text
        completion = response.json()["data"]["read_completion"]
        assert completion["result"] == "no_change"
        assert completion["no_change_read_count"] == index
        completed_at = datetime.fromisoformat(completion["completed_at"])
        due_at = datetime.fromisoformat(completion["next_read_due_at"])
        assert abs((due_at - completed_at).total_seconds() - expected_seconds) < 1

        blocked = client.get(
            f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
            headers=_worker_headers(worker),
        )
        assert blocked.status_code == 200
        assert blocked.json()["data"]["targets"] == []
        authorization = client.get(
            f"/api/workers/{worker['id']}/wechat/conversations/{binding['conversation_id']}/read-authorization",
            headers=_worker_headers(worker),
        )
        assert authorization.status_code == 200
        assert authorization.json()["data"]["allowed"] is False
        assert authorization.json()["data"]["identity_checkpoint"]["version"] == 3
        if index < 3:
            with SessionLocal() as db:
                binding_row = db.get(WechatSessionBinding, binding["id"])
                assert binding_row is not None
                binding_row.next_read_due_at = utcnow() - timedelta(seconds=1)
                db.commit()

    same_unread_scan = _scan_payload(remark_code)
    same_unread_scan["scan_id"] = "scan-same-unread-generation"
    same_unread = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=same_unread_scan,
        headers=_worker_headers(worker),
    )
    assert same_unread.status_code == 200
    targets = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert targets.status_code == 200
    assert targets.json()["data"]["targets"] == []
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        assert binding_row.unread_generation == 1
        assert binding_row.consumed_unread_generation == 1
        assert binding_row.no_change_read_count == 3
        assert binding_row.next_read_due_at is not None


def test_new_facts_keep_success_cooldown_until_new_unread_transition():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("成功读取冷却客户", "13896676679")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert binding_row is not None
        assert conversation is not None
        binding_row.unread_hint = False
        conversation.status = "waiting_sales_reply"
        db.commit()

    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="successful-read-cooldown",
            messages=[
                _v3_message(
                    "successful-read-new-fact",
                    role="customer",
                    message_type="text",
                    content="这是一次成功读取的新消息",
                    screen_order=1,
                )
            ],
            read_reason="waiting_sales_reply",
        ),
        headers=_worker_headers(worker),
    )
    assert response.status_code == 200, response.text
    completion = response.json()["data"]["read_completion"]
    assert completion["result"] == "new_facts"
    completed_at = datetime.fromisoformat(completion["completed_at"])
    due_at = datetime.fromisoformat(completion["next_read_due_at"])
    assert abs((due_at - completed_at).total_seconds() - 120) < 1

    no_unread_scan = _scan_payload(remark_code)
    no_unread_scan["scan_id"] = "scan-success-cooldown-no-unread"
    no_unread_scan["sessions"][0]["unread_hint"] = False
    no_unread = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=no_unread_scan,
        headers=_worker_headers(worker),
    )
    assert no_unread.status_code == 200
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        assert binding_row.next_read_due_at is not None

    new_unread_scan = _scan_payload(remark_code)
    new_unread_scan["scan_id"] = "scan-success-cooldown-new-unread"
    new_unread = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=new_unread_scan,
        headers=_worker_headers(worker),
    )
    assert new_unread.status_code == 200
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        assert binding_row.next_read_due_at is None
        assert binding_row.unread_generation == 2
        assert binding_row.consumed_unread_generation == 1


def test_changed_preview_creates_new_unread_generation_without_row_position_identity():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("未读代次稳定证据客户", "13896676680")
    remark_code = _pull_remark_code(worker)
    initial_scan = _scan_payload(remark_code)
    initial_scan["sessions"][0]["last_message_preview_time"] = "14:17"
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=initial_scan,
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200, scan.text
    binding = scan.json()["data"]["bindings"][0]
    assert binding["unread_generation"] == 1

    completed = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding,
            remark_code,
            read_run_id="unread-generation-one",
            messages=[],
            read_reason="waiting_sales_reply",
        ),
        headers=_worker_headers(worker),
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["read_completion"][
        "consumed_unread_generation"
    ] == 1

    moved_row = copy.deepcopy(initial_scan)
    moved_row["scan_id"] = "scan-row-moved-same-preview"
    moved_row["sessions"][0]["row_fingerprint"] = "different-row-position"
    moved = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=moved_row,
        headers=_worker_headers(worker),
    )
    assert moved.status_code == 200, moved.text
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        assert binding_row.unread_generation == 1
        assert binding_row.next_read_due_at is not None

    changed_preview = copy.deepcopy(moved_row)
    changed_preview["scan_id"] = "scan-new-semantic-preview"
    changed_preview["sessions"][0]["last_message_preview"] = "新的客户消息"
    changed_preview["sessions"][0]["last_message_preview_time"] = "14:19"
    changed = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=changed_preview,
        headers=_worker_headers(worker),
    )
    assert changed.status_code == 200, changed.text
    target_response = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert target_response.status_code == 200, target_response.text
    target = target_response.json()["data"]["targets"][0]
    assert target["unread_generation"] == 2
    assert target["consumed_unread_generation"] == 1


def test_read_completion_consumes_only_generation_authorized_at_read_start():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("读取期间新增代次客户", "13896676681")
    remark_code = _pull_remark_code(worker)
    first_scan = _scan_payload(remark_code)
    first_scan["sessions"][0]["last_message_preview"] = "代次十"
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=first_scan,
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200, scan.text
    binding_generation_one = scan.json()["data"]["bindings"][0]
    assert binding_generation_one["unread_generation"] == 1

    second_scan = copy.deepcopy(first_scan)
    second_scan["scan_id"] = "scan-generation-arrived-during-read"
    second_scan["sessions"][0]["last_message_preview"] = "代次十一"
    advanced = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=second_scan,
        headers=_worker_headers(worker),
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["data"]["bindings"][0]["unread_generation"] == 2

    completion = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=_v3_ingest_payload(
            binding_generation_one,
            remark_code,
            read_run_id="read-started-at-generation-one",
            messages=[],
            read_reason="visible_unread",
            unread_generation=1,
        ),
        headers=_worker_headers(worker),
    )
    assert completion.status_code == 200, completion.text
    settled = completion.json()["data"]["read_completion"]
    assert settled["unread_generation"] == 2
    assert settled["consumed_unread_generation"] == 1

    pending = client.get(
        f"/api/workers/{worker['id']}/wechat/sessions/read-targets",
        headers=_worker_headers(worker),
    )
    assert pending.status_code == 200, pending.text
    pending_target = pending.json()["data"]["targets"][0]
    assert pending_target["unread_generation"] == 2
    assert pending_target["consumed_unread_generation"] == 1


def test_ingest_rejects_unread_generation_never_issued_by_backend():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("伪造未读代次客户", "13896676682")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    assert scan.status_code == 200, scan.text
    binding = scan.json()["data"]["bindings"][0]
    forged = _v3_ingest_payload(
        binding,
        remark_code,
        read_run_id="forged-unread-generation",
        messages=[],
        read_reason="visible_unread",
        unread_generation=int(binding["unread_generation"]) + 1,
    )
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/messages/ingest",
        json=forged,
        headers=_worker_headers(worker),
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "MESSAGE_UNREAD_GENERATION_INVALID"


def test_temporary_paused_binding_restore_is_audited_and_requires_rescan():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("临时暂停恢复客户", "13896676674")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        binding_row.bind_status = "bound"
        binding_row.listen_status = "paused"
        binding_row.allow_listening = False
        original_revision = int(binding_row.authorization_revision)
        db.commit()

    restored = client.post(
        f"/api/conversations/{binding['conversation_id']}/wechat-binding/restore",
        json={"reason": "联调误暂停，已核实客户仍有效"},
        headers=HEADERS,
    )

    assert restored.status_code == 200, restored.text
    data = restored.json()["data"]
    assert data["bind_status"] == "bound"
    assert data["listen_status"] == "paused"
    assert data["allow_listening"] is False
    assert data["recovery_state"] == "paused_waiting_worker"
    assert data["authorization_revision"] > original_revision
    with SessionLocal() as db:
        log = db.query(OperationLog).filter(
            OperationLog.event_type == "wechat_binding_restored"
        ).one()
        assert log.operator_name_snapshot == "Ops Tester"
        assert log.extra_metadata["reason"] == "联调误暂停，已核实客户仍有效"
        assert log.before_data["listen_status"] == "paused"
        assert log.after_data == {
            "bind_status": "bound",
            "listen_status": "paused",
            "allow_listening": False,
            "authorization_revision": data["authorization_revision"],
            "disable_reason": None,
        }

    rescan_payload = _scan_payload(remark_code)
    rescan_payload["scan_id"] = "scan-after-binding-restore"
    rescan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=rescan_payload,
        headers=_worker_headers(worker),
    )
    assert rescan.status_code == 200
    assert rescan.json()["data"]["bindings"][0]["listen_status"] == "listening"
    assert rescan.json()["data"]["bindings"][0]["can_ingest_messages"] is True


def test_legacy_disabled_paused_binding_is_not_permanently_skipped_on_scan():
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead("历史暂停恢复客户", "13896676670")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        assert binding_row is not None
        binding_row.bind_status = "disabled"
        binding_row.listen_status = "paused"
        binding_row.allow_listening = False
        binding_row.disable_reason = None
        binding_row.disabled_at = None
        binding_row.disabled_by = None
        binding_row.replacement_binding_id = None
        db.commit()

    rescan_payload = _scan_payload(remark_code)
    rescan_payload["scan_id"] = "scan-legacy-disabled-paused"
    response = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=rescan_payload,
        headers=_worker_headers(worker),
    )

    assert response.status_code == 200, response.text
    recovered = response.json()["data"]["bindings"][0]
    assert recovered["id"] == binding["id"]
    assert recovered["bind_status"] == "already_bound"
    assert recovered["listen_status"] == "listening"
    assert recovered["can_ingest_messages"] is True


@pytest.mark.parametrize(
    ("blocked_case", "expected_code"),
    [
        ("hard_opt_out", "WECHAT_BINDING_RESTORE_CONVERSATION_TERMINATED"),
        ("closed", "WECHAT_BINDING_RESTORE_CONVERSATION_TERMINATED"),
        ("remark_removed", "WECHAT_BINDING_RESTORE_PERMANENTLY_DISABLED"),
        ("admin_disabled", "WECHAT_BINDING_RESTORE_PERMANENTLY_DISABLED"),
        ("wrong_worker", "WECHAT_BINDING_RESTORE_WORKER_MISMATCH"),
        ("conflict", "WECHAT_BINDING_RESTORE_CONFLICT"),
        ("history", "WECHAT_BINDING_RESTORE_HISTORY_FORBIDDEN"),
    ],
)
def test_permanent_conflicting_and_historical_bindings_cannot_restore(
    blocked_case,
    expected_code,
):
    worker = _create_worker()
    _create_sales(worker["id"])
    _create_lead(f"禁止恢复-{blocked_case}", "13896676675")
    remark_code = _pull_remark_code(worker)
    scan = client.post(
        f"/api/workers/{worker['id']}/wechat/sessions/scan-result",
        json=_scan_payload(remark_code),
        headers=_worker_headers(worker),
    )
    binding = scan.json()["data"]["bindings"][0]
    other_worker = _create_worker() if blocked_case == "wrong_worker" else None
    with SessionLocal() as db:
        binding_row = db.get(WechatSessionBinding, binding["id"])
        conversation = db.get(Conversation, binding["conversation_id"])
        assert binding_row is not None
        assert conversation is not None
        binding_row.bind_status = "bound"
        binding_row.listen_status = "paused"
        binding_row.allow_listening = False
        if blocked_case == "hard_opt_out":
            conversation.status = "rejected"
            conversation.ai_enabled = False
        elif blocked_case == "closed":
            conversation.status = "closed"
            conversation.close_reason = "人工关闭"
        elif blocked_case == "remark_removed":
            binding_row.bind_status = "disabled"
            binding_row.disable_reason = "remark_code_removed_confirmed"
            binding_row.disabled_at = utcnow()
            binding_row.disabled_by = "operator:remark-removal"
        elif blocked_case == "admin_disabled":
            binding_row.bind_status = "disabled"
            binding_row.disable_reason = "admin_disabled"
            binding_row.disabled_at = utcnow()
            binding_row.disabled_by = "operator:admin-disable"
        elif blocked_case == "wrong_worker":
            assert other_worker is not None
            conversation.worker_id = other_worker["id"]
        elif blocked_case == "conflict":
            db.add(
                WechatSessionBinding(
                    worker_id=worker["id"],
                    display_name="冲突会话",
                    rpa_session_key="conflicting-row",
                    row_fingerprint="conflicting-row",
                    remark_code=remark_code,
                    bind_status="needs_review",
                    listen_status="paused",
                    allow_listening=False,
                )
            )
        else:
            binding_row.deleted_at = utcnow()
            binding_row.replacement_binding_id = binding_row.id
        db.commit()

    response = client.post(
        f"/api/conversations/{binding['conversation_id']}/wechat-binding/restore",
        json={"reason": "尝试恢复"},
        headers=HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["code"] == expected_code
    with SessionLocal() as db:
        assert db.query(OperationLog).filter(
            OperationLog.event_type == "wechat_binding_restored"
        ).count() == 0
